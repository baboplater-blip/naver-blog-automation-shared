"""draft_store — 글 생성과 발행을 분리하기 위한 디스크 보관소.

auto_scheduler.run_pipeline(save_only=True) 가 호출되면 본문·이미지·예약시각을
runs/{slug}/post_data.json + _status.json 에 보관한다. 차후 별도 업로드 단계
(Stage 3 — 미구현)가 이 보관소를 읽어 발행한다.

설계 원칙:
- 경로 베이스는 harness_emitter 와 동일 (runs/ 공유) — 단 파일명이 달라
  qa.json, publish_brief.yaml 등 기존 산출물과 충돌하지 않는다.
- 모든 파일 쓰기는 원자적(tempfile + os.replace)으로 수행해 GUI/스케줄러
  실행 중 부분 쓰기로 깨진 JSON을 남기지 않는다.
- 파일 I/O 예외는 잡아 warning 후 빈 결과/False 반환 — draft_store 가 죽어도
  발행 파이프라인은 계속 돈다.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional, Union

from config.logger import get_logger

log = get_logger(__name__)

# ── 경로 (harness_emitter.py L27-30 동일 패턴) ─────────────
_BASE_DIR = Path(__file__).resolve().parent.parent          # naver_blog_v3/
_HARNESS_ROOT = _BASE_DIR.parent                             # naver_blog_automation_v3/
from config import profile as _nbaprofile
_DATA_HARNESS = _nbaprofile.harness_data_root()

_RUNS_DIR = _DATA_HARNESS / "runs"

_POST_DATA_FILE = "post_data.json"
_STATUS_FILE = "_status.json"

# 허용 status — Stage 3+ 가 추가될 때 여기만 갱신
# frozen = 선별 소진(draft_triage)으로 발행 큐에서 제외한 저수요 보관본.
#   발행 대상은 draft_ready 만이라 frozen 은 자동 제외되고, draft_ready 로 되돌리면 복구된다
#   (post_data.json 보존 — uploaded 아님). 되돌릴 수 있는 '동결'.
# needs_fix = fact_gate/QA 실패로 발행이 차단됐지만 본문(≈2,000자)은 살아 있는 보관본.
#   find_draft_for_title(draft_ready 만 반환)과 draft_triage(draft_ready 만 분류)에서
#   자동 제외되므로 재드레인·자동 발행되지 않는다 — 사람/후속 수정 대기용.
#   error 필드에 실패 사유(어떤 수치가 문제였는지)를 남겨 수정 지점을 알 수 있게 한다.
_ALLOWED_STATUS = {"draft_ready", "queued", "uploading", "uploaded", "failed",
                   "frozen", "needs_fix"}

# ── 발행 진행 상태(publish_state) — 2026-09-18 무인 운영 M1, additive ──────
# 기존 `status` 의 **의미를 바꾸지 않는다**. find_draft_for_title·draft_triage·
# fill_targets 가 전부 draft_ready 로 필터하므로, 거기에 새 의미를 얹으면 조용한
# 회귀가 난다(이 저장소의 반복 사고 유형). 대신 _status.json 에 필드를 하나 더 얹어
# "이 글이 발행 시도 중인가 / 결말이 났는가"를 **디스크에** 남긴다.
# 부재 시 "none" 으로 읽으므로 기존 _status.json 파일은 변환이 필요 없다.
_ALLOWED_PUBLISH_STATE = {
    "none", "selected", "reserving", "unconfirmed", "reserved",
    "verifying", "live", "missing", "failed",
}
# 자동 재발행이 **절대 금지**되는 상태 — 결말은 사람 확인으로만 닫힌다.
_NO_AUTO_REPUBLISH = {"reserving", "unconfirmed", "missing", "reserved", "live"}

# examplebrand 카테고리 (templates/prompt_strategies.json 키와 1:1).
# settings.CLAUDE_MODEL_BY_CATEGORY 와 동기화되며, 그 dict 가 비어 있거나 누락된
# 카테고리가 있어도 이 폴백을 사용해 동일 키 형태를 보장한다. detect_category 가
# 반환하는 "default" 는 examplebrand 카테고리에 포함되지 않으므로 _UNCATEGORIZED 로 묶는다.
# (2026-05-26) 13번째 카테고리 "환경" 추가 — 에너지효율·전기차·그린리모델링 등.
_examplebrand_CATEGORIES = (
    "청년", "복지", "교육", "취업", "주거", "의료",
    "바우처", "부동산", "절세", "금융", "노후", "디지털",
    "환경",
)
_UNCATEGORIZED = "_uncategorized"

# load_drafts 메모리 캐시 — status_filter 별로 결과를 보관해 재호출 시 디스크 재스캔 회피.
# save_draft / mark_status / purge_uploaded 가 _invalidate_cache() 로 명시적 무효화.
# 외부에서 runs/ 를 직접 수정하면 stale 가능 — draft_store API 만으로 다루는 흐름 가정.
_DRAFT_CACHE: dict = {}


def _invalidate_cache() -> None:
    """draft 라이프사이클에 영향 주는 모든 변경 후 호출."""
    _DRAFT_CACHE.clear()


# ─────────────────────────────────────────────────────
# 내부 헬퍼
# ─────────────────────────────────────────────────────

def _now_iso() -> str:
    """timezone-aware ISO 8601 (초 단위)."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _atomic_write_json(path: Path, data: dict) -> None:
    """tempfile → os.replace 패턴으로 원자 쓰기. 한글 보존, 들여쓰기 2."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=".draft_", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except (OSError, TypeError, ValueError):
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise


def _read_json(path: Path) -> Optional[dict]:
    """JSON 읽기. 존재하지 않거나 깨졌으면 None 반환."""
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as e:
        log.warning(f"[draft_store] read failed {path.name}: {e}")
        return None


def _image_plan_persist_enabled() -> bool:
    """이미지 기획 영속 게이트. 끄면 예전처럼 계획 키가 payload 에 실리지 않는다."""
    try:
        from config import settings as _s
        return bool(getattr(_s, "IMAGE_PLAN_ENABLED", True))
    except Exception:  # noqa: BLE001 — settings 미로드면 기본 ON
        return True


def _attach_editorial_brief(payload: dict, post_data: dict) -> None:
    """편집 브리프를 payload 에 additive 로 싣는다(2026-09-18).

    ★여기도 **공통 저장 경계**다. 브리프를 들고 오지 않는 집필 진입점(experience·product·
    restyle·top_upgrade·triage)에는 로컬 폴백(제목 기반 썸네일 메시지만)을 붙여, 어느
    경로로 저장된 글이든 이미지 채움이 같은 모양의 브리프를 읽는다.

    브리프가 없으면 키를 만들지 않는다 → 그때 payload 는 예전과 바이트 동일.
    """
    try:
        brief = post_data.get("editorial_brief")
        if isinstance(brief, dict) and brief:
            payload["editorial_brief"] = brief
            return
        try:
            from config import settings as _s
            if not bool(getattr(_s, "EDITORIAL_BRIEF_ENABLED", True)):
                return
        except Exception:  # noqa: BLE001 — settings 미로드면 기본 ON
            pass
        from core import editorial_brief as eb
        brief = eb.local_brief(title=payload.get("title"),
                               category=payload.get("category") or "")
        # 이미 이미지 계획이 실려 있으면 그 썸네일 문구를 브리프 메시지로 쓴다 —
        # 브리프 없이 계획만 만들어 오는 집필 진입점에서 본문·썸네일 문구가 갈라지는
        # 것을 막는다(단일 진실 원본 = 계획의 썸네일 슬롯).
        plan = post_data.get("image_plan")
        if isinstance(plan, dict):
            thumb = next((s for s in (plan.get("slots") or [])
                          if isinstance(s, dict) and s.get("role") == "thumbnail"), None)
            label = str((thumb or {}).get("label") or "").strip()
            if label:
                brief["thumbnail_message"] = label
        payload["editorial_brief"] = brief
    except Exception as exc:  # noqa: BLE001 — 브리프는 부가 정보, 저장을 막지 않는다
        log.warning(f"[draft_store] editorial_brief 첨부 생략({type(exc).__name__})")


def _attach_image_plan(payload: dict, post_data: dict) -> None:
    """이미지 기획(계획)·슬롯 자산 map 을 payload 에 additive 로 싣는다(2026-09-10).

    ★여기가 **공통 저장 경계**다. 집필 진입점이 여럿(content_generator·experience_writer·
    product_writer·draft_restyle·draft_top_upgrade…)이라 각자 계획을 만들게 하면 반드시
    한쪽이 빠진다. 그래서 계획이 실려 오면 그대로 보존하고, 없으면 **로컬 결정론 계획**
    (LLM 0회 — 제목·소제목·카테고리만 사용)을 여기서 한 번 만들어 붙인다.

    전 경로 fail-open: core.image_plan 이 없거나 어떤 예외가 나도 조용히 건너뛴다
    (그때의 payload 는 예전과 바이트 동일 — 계획 키가 아예 없다).
    """
    try:
        slots = post_data.get("image_slots")
        if isinstance(slots, dict) and slots:
            payload["image_slots"] = {str(k): str(v) for k, v in slots.items() if v}

        plan = post_data.get("image_plan")
        if isinstance(plan, dict) and plan.get("slots"):
            payload["image_plan"] = plan
            return
        if not _image_plan_persist_enabled():
            return
        from core import image_plan as ip
        body_count = ip.DEFAULT_BODY_COUNT
        try:
            from config import settings as _s
            body_count = int(getattr(_s, "IMAGE_FILL_CARDS", ip.DEFAULT_BODY_COUNT) or 0)
        except Exception:  # noqa: BLE001 — settings 미로드면 코드 기본값
            pass
        # 썸네일 문구는 (있으면) 편집 브리프의 메시지를 쓴다 — 본문과 썸네일이 같은 말을
        # 하게 하는 지점. 브리프가 없으면 local_plan 이 제목 기반 폴백을 만든다.
        message = ""
        brief = payload.get("editorial_brief")
        if isinstance(brief, dict):
            message = str(brief.get("thumbnail_message") or "").strip()
        payload["image_plan"] = ip.local_plan(
            title=payload.get("title"), content=payload.get("content"),
            category=payload.get("category") or "", body_count=body_count,
            label=message or None)
    except Exception as exc:  # noqa: BLE001 — 계획은 부가 정보, 저장을 막지 않는다
        log.warning(f"[draft_store] image_plan 첨부 생략({type(exc).__name__})")


# ─────────────────────────────────────────────────────
# 공개 API
# ─────────────────────────────────────────────────────

def save_draft(
    slug: str,
    post_data: dict,
    image_paths: list,
    schedule_at: Optional[str] = None,
    *,
    status: str = "draft_ready",
    error: Optional[str] = None,
    publish_state: Optional[str] = None,
) -> Path:
    """초안과 메타를 runs/{slug}/ 아래에 보관한다.

    Args:
        slug: harness 슬러그(make_slug 산출 또는 폴백). 빈 문자열 불가.
        post_data: {"title": str, "content": str(HTML),
                    "tags": list[str], "category": str | None}
            추가로 `image_plan`(이미지 기획)·`image_slots`(슬롯→파일 map)가 실려 있으면
            그대로 영속한다. 계획이 없으면 **여기서** 로컬 결정론 계획을 한 번 만들어
            붙인다(LLM 0회 — `_attach_image_plan` 참조). 여러 집필 진입점이 각자 계획을
            만들지 않아도 되게 하는 공통 저장 경계다.
        image_paths: 절대경로 문자열 리스트. 그대로 보관, 검증하지 않음.
        schedule_at: "YYYY-MM-DD HH:MM" 또는 None. _status.json 에 그대로 저장.
        status: 초기 status. 기본 'draft_ready'(정상 보관). fact_gate/QA 실패 본문을
            보관할 때는 'needs_fix' 로 지정해 발행 경로(draft_ready)에서 자동 제외한다.
            _ALLOWED_STATUS 밖 값이면 ValueError.
        error: status='needs_fix' 등에서 실패 사유(어떤 수치가 문제였는지) 요약.
            _status.json 의 error 필드에 그대로 저장돼 수정 지점을 알 수 있게 한다.

    Returns:
        runs/{slug}/post_data.json 의 Path.

    Raises:
        ValueError: slug 가 비었거나 status 가 허용 밖일 때.
        OSError: 파일 쓰기 실패 (호출자가 처리).
    """
    if not slug or not isinstance(slug, str):
        raise ValueError("slug must be a non-empty string")
    if status not in _ALLOWED_STATUS:
        raise ValueError(
            f"invalid status {status!r}, expected one of {sorted(_ALLOWED_STATUS)}"
        )

    target_dir = _RUNS_DIR / slug
    target_dir.mkdir(parents=True, exist_ok=True)

    payload = {
        "title": post_data.get("title", ""),
        "content": post_data.get("content", ""),
        "tags": list(post_data.get("tags", []) or []),
        "category": post_data.get("category"),
        "image_paths": list(image_paths or []),
    }
    # per-post 이미지 삽입 신호(젠스파크 자동 채움 등)만 additive 로 영속한다. 참일 때만
    # 키를 추가하므로, 이 신호가 없는 기존 초안 payload 는 바이트 그대로 유지된다(회귀 0).
    # load_drafts 가 post_data 안에 그대로 실어 반환 → 발행 경로가 _should_insert_images 로 읽는다.
    if post_data.get("_force_image_insert"):
        payload["_force_image_insert"] = True
    # 브리프를 먼저 싣는다 — 이미지 기획이 그 썸네일 메시지를 읽어 쓴다(순서 의존).
    _attach_editorial_brief(payload, post_data)
    _attach_image_plan(payload, post_data)
    post_path = target_dir / _POST_DATA_FILE
    _atomic_write_json(post_path, payload)

    status_path = target_dir / _STATUS_FILE
    status_record = {
        # title 을 _status.json 에도 복사 — uploaded 후 post_data.json 이 정리돼도
        # 슬러그·제목·시각·url 만으로 추적 가능 (사용자 요구: "제목만 남기거나 삭제처리").
        "title": post_data.get("title", ""),
        "status": status,
        "created_at": _now_iso(),
        "schedule_at": schedule_at,
        "uploaded_at": None,
        "url": None,
        "error": error,
    }
    # publish_state 는 **전달됐을 때만** 키를 추가한다 — 미전달이면 기존 _status.json
    # 페이로드와 바이트 동일(무인 운영을 켜지 않은 설치의 회귀 0).
    if publish_state is not None:
        if publish_state not in _ALLOWED_PUBLISH_STATE:
            raise ValueError(
                f"invalid publish_state {publish_state!r}, "
                f"expected one of {sorted(_ALLOWED_PUBLISH_STATE)}")
        status_record["publish_state"] = publish_state
    _atomic_write_json(status_path, status_record)

    _invalidate_cache()
    return post_path


def load_drafts(
    status_filter: Optional[Union[str, list]] = None,
) -> list:
    """runs/* 를 스캔해 draft_store 가 만든 항목만 반환.

    Args:
        status_filter: None | str | list[str]. None 이면 전부.

    Returns:
        created_at 오름차순 정렬된 dict 리스트:
        {"slug", "post_data", "status", "schedule_at", "created_at",
         "uploaded_at", "url", "error"}.
        runs/ 자체가 없거나 비어 있으면 빈 리스트.
    """
    if not _RUNS_DIR.exists():
        return []

    allow: Optional[set] = None
    if status_filter is not None:
        if isinstance(status_filter, str):
            allow = {status_filter}
        else:
            allow = set(status_filter)

    # 캐시 키: status_filter 정규화. 같은 필터 반복 호출 시 디스크 스캔 회피.
    cache_key = "all" if allow is None else tuple(sorted(allow))
    cached = _DRAFT_CACHE.get(cache_key)
    if cached is not None:
        # 호출자가 결과를 mutate 해도 캐시가 오염되지 않도록 얕은 복사로 반환.
        return list(cached)

    drafts: list = []
    try:
        children = list(_RUNS_DIR.iterdir())
    except OSError as e:
        log.warning(f"[draft_store] scan failed: {e}")
        return []

    for child in children:
        if not child.is_dir():
            continue
        status_path = child / _STATUS_FILE
        if not status_path.exists():
            # draft_store 가 만든 슬러그가 아님 (harness_emitter 단독 산출물 등)
            continue

        # 1) _status.json 만 먼저 로드 — status_filter 적용을 post_data 로드 전으로 이동.
        #    누적된 uploaded 슬러그가 많아도 본문은 안 읽으므로 lookup I/O 가 일정.
        status = _read_json(status_path)
        if status is None:
            log.warning(f"[draft_store] skipping {child.name}: _status.json 손상")
            continue

        if allow is not None and status.get("status") not in allow:
            continue

        # 2) status_filter 통과한 슬러그만 post_data.json 로드.
        #    uploaded 후 정리된(post_data.json 없는) 슬러그도 항목 반환은 해야
        #    추적/감사가 가능하다 — 본문 자리는 빈 dict 로 둔다.
        post_path = child / _POST_DATA_FILE
        post: dict = {}
        if post_path.exists():
            loaded = _read_json(post_path)
            if loaded is None:
                log.warning(f"[draft_store] {child.name}: post_data.json 손상 — 본문 비움")
            else:
                post = loaded

        drafts.append({
            "slug": child.name,
            "post_data": post,
            "title": status.get("title", post.get("title", "")),
            "status": status.get("status"),
            "schedule_at": status.get("schedule_at"),
            "created_at": status.get("created_at"),
            "uploaded_at": status.get("uploaded_at"),
            "url": status.get("url"),
            "error": status.get("error"),
            # additive(2026-09-18) — 부재하면 "none". 기존 소비자는 이 키를 읽지 않는다.
            "publish_state": status.get("publish_state") or "none",
            "log_no": status.get("log_no") or "",
        })

    drafts.sort(key=lambda d: d.get("created_at") or "")
    _DRAFT_CACHE[cache_key] = drafts
    return list(drafts)


def mark_status(
    slug: str,
    status: str,
    error: Optional[str] = None,
    url: Optional[str] = None,
    *,
    prune_on_uploaded: bool = True,
    publish_state: Optional[str] = None,
) -> bool:
    """슬러그의 _status.json 을 갱신한다.

    Args:
        slug: 대상 슬러그.
        status: _ALLOWED_STATUS 중 하나.
        error: 실패 사유. 전달되면 status.error 에 기록.
        url: 발행 URL. status="uploaded" 와 함께 전달되는 게 정상.
        prune_on_uploaded: True(기본)면 status='uploaded' 시 post_data.json 을
            삭제해 lookup 부하를 줄인다. _status.json 은 보존되므로 슬러그·제목·
            시각·url 추적 가능. False 면 본문도 보존(검증/감사용).

    Returns:
        성공 True, 슬러그 폴더가 없거나 _status.json 이 없으면 False.

    Raises:
        ValueError: 허용되지 않은 status.
    """
    if status not in _ALLOWED_STATUS:
        raise ValueError(
            f"invalid status {status!r}, expected one of {sorted(_ALLOWED_STATUS)}"
        )
    if not slug:
        log.warning("[draft_store] mark_status: empty slug")
        return False

    status_path = _RUNS_DIR / slug / _STATUS_FILE
    if not status_path.exists():
        log.warning(f"[draft_store] mark_status: not found — {slug}")
        return False

    current = _read_json(status_path)
    if current is None:
        log.warning(f"[draft_store] mark_status: 읽기 실패 — {slug}")
        return False

    current["status"] = status
    if error is not None:
        current["error"] = error
    if url is not None:
        current["url"] = url
    if publish_state is not None:
        if publish_state not in _ALLOWED_PUBLISH_STATE:
            raise ValueError(
                f"invalid publish_state {publish_state!r}, "
                f"expected one of {sorted(_ALLOWED_PUBLISH_STATE)}")
        current["publish_state"] = publish_state
    if status == "uploaded":
        current["uploaded_at"] = _now_iso()

    try:
        _atomic_write_json(status_path, current)
    except OSError as e:
        log.warning(f"[draft_store] mark_status write failed {slug}: {e}")
        return False

    # 본문 자동 정리 — uploaded 된 글은 lookup 후보가 아니므로 post_data.json 을 즉시 제거.
    # 보관함 누적 시 매 발행마다 모든 슬러그를 재로드하던 비용을 차단.
    if status == "uploaded" and prune_on_uploaded:
        post_path = _RUNS_DIR / slug / _POST_DATA_FILE
        if post_path.exists():
            try:
                post_path.unlink()
                log.info(
                    f"[draft_store] post_data.json 정리: {slug} "
                    f"(보관함 lookup 부하 감소)"
                )
            except OSError as _pe:
                log.warning(
                    f"[draft_store] post_data.json 삭제 실패 {slug}: {_pe}"
                )
    _invalidate_cache()
    return True


def set_publish_state(slug: str, publish_state: str, **fields) -> bool:
    """발행 진행 상태를 _status.json 에 **write-ahead** 로 기록한다 (2026-09-18).

    `status`(원고 상태)는 건드리지 않는다 — 두 축은 독립이다. 이 함수는 발행 시도
    직전/직후에 불려 "이 글이 어디까지 갔는지"를 프로세스 밖(디스크)에 남기는 것이
    유일한 목적이다. 크래시·전원 차단 후 재시작하면 이 값이 대조의 입력이 된다.

    Args:
        slug: 대상 슬러그.
        publish_state: `_ALLOWED_PUBLISH_STATE` 중 하나.
        **fields: 함께 기록할 추가 필드(log_no, url, attempt_at, publish_at,
            reason, evidence 등). None 값은 무시한다.

    Returns:
        기록 성공 True. 슬러그 폴더/_status.json 이 없거나 쓰기 실패면 False.

    Raises:
        ValueError: 허용되지 않은 publish_state.
    """
    if publish_state not in _ALLOWED_PUBLISH_STATE:
        raise ValueError(
            f"invalid publish_state {publish_state!r}, "
            f"expected one of {sorted(_ALLOWED_PUBLISH_STATE)}")
    if not slug:
        return False

    status_path = _RUNS_DIR / slug / _STATUS_FILE
    if not status_path.exists():
        log.debug(f"[draft_store] set_publish_state: not found — {slug}")
        return False
    current = _read_json(status_path)
    if current is None:
        log.warning(f"[draft_store] set_publish_state: 읽기 실패 — {slug}")
        return False

    current["publish_state"] = publish_state
    for key, value in fields.items():
        if value is not None:
            current[key] = value
    current["publish_state_at"] = _now_iso()

    try:
        _atomic_write_json(status_path, current)
    except OSError as exc:
        log.warning(f"[draft_store] set_publish_state write 실패 {slug}: {exc}")
        return False
    _invalidate_cache()
    return True


def publish_state_of(slug: str) -> str:
    """슬러그의 발행 진행 상태. 기록이 없으면 'none'."""
    if not slug:
        return "none"
    current = _read_json(_RUNS_DIR / slug / _STATUS_FILE)
    if not isinstance(current, dict):
        return "none"
    value = current.get("publish_state") or "none"
    return value if value in _ALLOWED_PUBLISH_STATE else "none"


def blocks_auto_republish(slug: str) -> bool:
    """이 슬러그를 **자동으로 다시 발행하면 안 되는가**.

    `reserving`/`unconfirmed`/`missing` 은 결말이 미상이므로 재발행이 곧 중복 발행이다.
    `reserved`/`live` 는 이미 올라갔다. 어느 쪽도 자동 재시도 대상이 아니다.
    """
    return publish_state_of(slug) in _NO_AUTO_REPUBLISH


# ─────────────────────────────────────────────────────
# 키워드 매칭 lookup (auto_scheduler 가 글 생성 직전 보관본 활용 시 사용)
# ─────────────────────────────────────────────────────

# raw_title 의 'YYYY-MM-DD — ' prefix 패턴 (harness_emitter._SLUG_STRIP_RE 와 동일).
_TITLE_DATE_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\s*[—\-]\s*")
# 슬러그의 날짜 prefix (정확히 'YYYY-MM-DD_').
_SLUG_DATE_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_")
# 충돌 회피 접미사 '_2', '_3', ... (harness_emitter.make_slug 가 같은 키워드 중복 시 부여).
_SLUG_DUP_SUFFIX_RE = re.compile(r"_\d+$")


def _normalize_title_to_key(raw_title: str) -> Optional[str]:
    """raw_title 에서 날짜 prefix 를 떼고, harness_emitter 와 동일한 슬러그 키를 만든다.

    예:
      "2026-05-09 — 시니어용 '간편 모드' 갤럭시·아이폰 설정 단계별"
      → "시니어용-간편-모드-갤럭시아이폰-설정-단계별"

    실패하면 None — 호출자는 lookup 을 포기하고 신규 생성으로 폴백한다.
    """
    if not raw_title or not isinstance(raw_title, str):
        return None
    try:
        clean = _TITLE_DATE_PREFIX_RE.sub("", raw_title).strip()
        if not clean:
            return None
        # harness_emitter 의 슬러그 정규화를 그대로 쓰면 키 부분이 100% 일치한다.
        from core.harness_emitter import _slug_from_title  # type: ignore
    except (AttributeError, ImportError) as e:
        log.warning(f"[draft_store] harness_emitter import 실패 — lookup 비활성: {e}")
        return None
    try:
        key = _slug_from_title(clean)
    except (TypeError, ValueError, re.error) as e:
        log.warning(f"[draft_store] slug 정규화 실패 ({raw_title!r}): {e}")
        return None
    return key or None


def _key_from_slug(slug: str) -> Optional[str]:
    """슬러그에서 키워드 부분만 떼어낸다 ('YYYY-MM-DD_' prefix + '_N' dup suffix 제거).

    예: '2026-05-08_시니어용-간편-모드-갤럭시아이폰-설정-단계별_2'
        → '시니어용-간편-모드-갤럭시아이폰-설정-단계별'

    날짜 prefix 가 없으면 슬러그 전체를 키로 간주(임의 보관본 호환).
    """
    if not slug:
        return None
    body = _SLUG_DATE_PREFIX_RE.sub("", slug, count=1)
    if not body:
        return None
    return _SLUG_DUP_SUFFIX_RE.sub("", body)


def find_draft_for_title(
    raw_title: str,
    max_age_days: Optional[int] = None,
) -> Optional[dict]:
    """raw_title 에 대응하는 status='draft_ready' 보관본을 찾는다.

    같은 키워드의 보관본이 여러 개 있으면 created_at 가장 최근 것을 반환한다.
    슬러그의 날짜 prefix 는 무시한다 — 5/8 에 만든 보관본이 5/10 에 발행돼도 매칭된다.

    Args:
        raw_title: '2026-05-09 — 키워드' 또는 그냥 '키워드'.
        max_age_days: created_at 이 이 일수 이내인 보관본만 반환.
            None(기본)이면 settings.DRAFT_MAX_AGE_DAYS(미정의 시 30)를 따른다 —
            하드코딩 30일이 5월 백로그 전체를 매칭에서 가리던 문제의 단일 조절점.

    Returns:
        매칭된 draft dict (load_drafts 와 동일 형식) 또는 None.
        예외는 모두 잡아 None 반환 — 발행 흐름을 깨뜨리지 않는다.
    """
    if max_age_days is None:
        try:
            from config import settings as _settings
            max_age_days = int(getattr(_settings, "DRAFT_MAX_AGE_DAYS", 30))
        except (ImportError, TypeError, ValueError):
            max_age_days = 30

    target_key = _normalize_title_to_key(raw_title)
    if not target_key:
        return None

    try:
        candidates = load_drafts(status_filter="draft_ready")
    except (OSError, ValueError) as e:
        log.warning(f"[draft_store] find_draft_for_title load 실패: {e}")
        return None
    if not candidates:
        return None

    now = datetime.now().astimezone()
    matched: list = []
    for entry in candidates:
        slug = entry.get("slug") or ""
        entry_key = _key_from_slug(slug)
        if entry_key != target_key:
            continue

        # post_data 가 비어 있으면(uploaded 후 정리됐거나 손상) 매칭 후보에서 제외.
        # status_filter 로 막혀야 정상이지만 중간에 status 가 잘못 바뀐 경우의 안전망.
        post = entry.get("post_data") or {}
        if not post.get("content"):
            log.debug(f"[draft_store] {slug}: post_data 비어있어 매칭 제외")
            continue

        created_raw = entry.get("created_at")
        if created_raw:
            try:
                created_ts = datetime.fromisoformat(created_raw)
            except (TypeError, ValueError):
                log.debug(
                    f"[draft_store] bad created_at on {slug}: {created_raw!r} — 매칭 후보에서 제외"
                )
                continue
            age_days = (now - created_ts).total_seconds() / 86400.0
            if age_days > max_age_days:
                continue
        matched.append(entry)

    if not matched:
        return None

    # load_drafts 가 created_at 오름차순이므로 마지막이 가장 최근.
    return matched[-1]


def purge_uploaded(older_than_days: int = 30, dry_run: bool = False) -> int:
    """status=uploaded 이고 uploaded_at + days < now 인 슬러그 폴더 삭제.

    Args:
        older_than_days: 이 일수보다 오래된 업로드만 삭제. 음수면 즉시 만료.
        dry_run: True 면 삭제하지 않고 대상 개수만 반환.

    Returns:
        삭제된(또는 dry_run 에서 삭제 대상이었던) 개수.
    """
    drafts = load_drafts(status_filter="uploaded")
    if not drafts:
        return 0

    now = datetime.now().astimezone()
    targets: list = []
    for d in drafts:
        uploaded_at = d.get("uploaded_at")
        if not uploaded_at:
            continue
        try:
            ts = datetime.fromisoformat(uploaded_at)
        except (TypeError, ValueError):
            log.warning(
                f"[draft_store] purge: bad uploaded_at on {d['slug']}: {uploaded_at!r}"
            )
            continue
        age_days = (now - ts).total_seconds() / 86400.0
        if age_days >= older_than_days:
            targets.append(d["slug"])

    if dry_run:
        return len(targets)

    deleted = 0
    for slug in targets:
        target_dir = _RUNS_DIR / slug
        try:
            shutil.rmtree(target_dir)
            deleted += 1
        except OSError as e:
            log.warning(f"[draft_store] purge rmtree failed {slug}: {e}")
    if deleted > 0:
        _invalidate_cache()
    return deleted


# ─────────────────────────────────────────────────────
# 카테고리별 보관함 API (2026-05-16)
# examplebrand 12 카테고리 단위로 보관본을 카운트·필터링하고, 발행 후보 리스트와의
# 매칭 여부를 분석한다. GUI 대시보드 "보관함 현황" 카드와 pre-flight 팝업이 사용.
# 모든 함수는 load_drafts 의 in-memory 결과 위에서 동작 — 디스크 추가 I/O 0.
# ─────────────────────────────────────────────────────


def _brand_categories() -> Optional[List[str]]:
    """활성 프로필의 브랜드 카테고리 목록. default(브랜드팩 없음)이면 None.

    default 프로필은 config.brand.categories() 가 None 을 돌려주므로 이 함수도
    None → 호출측이 기존 examplebrand 하드코딩 목록을 그대로 쓴다(동작 무변화).
    named 프로필(예: exampleprofile=영어교육)은 자기 분야 카테고리 리스트를 돌려준다.
    import/조회 실패도 None 으로 폴백(라이브 경로 무중단).
    """
    try:
        from config import brand as _brand  # local import — circular 회피
        cats = _brand.categories()
        if isinstance(cats, list) and cats:
            return [str(c) for c in cats]
    except (ImportError, AttributeError, OSError, ValueError) as exc:
        log.debug(f"[draft_store] brand 카테고리 로드 실패 — examplebrand 폴백: {exc}")
    return None


def _examplebrand_categories() -> List[str]:
    """활성 프로필의 카테고리 목록.

    named 프로필이 브랜드 카테고리를 정의하면 그 목록을 그대로 사용한다.
    default(브랜드팩 없음)는 기존 로직 유지 — settings.CLAUDE_MODEL_BY_CATEGORY 의
    키를 우선 사용하고, 실패하면 모듈 상수 _examplebrand_CATEGORIES 로 폴백한다.
    항상 13-키 정확도를 유지하기 위해 settings 가 부분적으로 비어 있을 때도
    빠진 키를 모듈 상수로 보충한다.
    """
    brand_cats = _brand_categories()
    if brand_cats is not None:
        return brand_cats                       # named 프로필 — 자기 분야 카테고리
    try:
        import config.settings as _settings  # local import — circular 회피
        mapping = getattr(_settings, "CLAUDE_MODEL_BY_CATEGORY", None) or {}
        if isinstance(mapping, dict) and mapping:
            keys = [k for k in mapping.keys() if isinstance(k, str) and k.strip()]
            if keys:
                # settings 가 일부 카테고리를 빠뜨려도 13 키를 보장
                merged: List[str] = list(keys)
                for fallback_key in _examplebrand_CATEGORIES:
                    if fallback_key not in merged:
                        merged.append(fallback_key)
                return merged
    except (ImportError, AttributeError) as exc:
        log.debug(f"[draft_store] settings 카테고리 키 로드 실패 — 폴백: {exc}")
    return list(_examplebrand_CATEGORIES)


# 활성 카테고리 멤버십 집합 — 한 프로세스=한 프로필이라 프로세스 수명 동안 불변.
#   per-draft 루프(_resolve_category)에서 매번 brand.json/settings 를 재조회하지 않도록
#   최초 1회만 계산해 캐시한다(search_volume.volume_index 의 lru_cache 와 동일 근거).
#   default 프로필은 _examplebrand_categories() 가 기존 examplebrand 목록을 그대로 돌려주므로
#   이 집합도 기존 13 카테고리와 동일 — 멤버십 판정 결과 무변화.
_ACTIVE_CATEGORY_SET: Optional[frozenset] = None


def _active_category_set() -> frozenset:
    """활성 프로필의 카테고리 멤버십 집합(캐시). _resolve_category 등 판정 전용."""
    global _ACTIVE_CATEGORY_SET
    if _ACTIVE_CATEGORY_SET is None:
        _ACTIVE_CATEGORY_SET = frozenset(_examplebrand_categories())
    return _ACTIVE_CATEGORY_SET


def _resolve_category(entry: dict) -> str:
    """draft entry 의 카테고리 결정 — post_data.category 우선, 없으면 제목으로 감지.

    detect_category 가 "default" 를 반환하거나 활성 카테고리에 없는 값이면
    _UNCATEGORIZED 로 정규화. 감지 모듈 import 실패 시에도 _UNCATEGORIZED.

    default 프로필은 활성 카테고리 = examplebrand 13종이라 기존 판정과 동일.
    named 프로필(exampleprofile 등)은 자기 분야 카테고리를 인식하므로 post_data.category
    가 그 목록에 있으면 그대로 채택된다(전부 _UNCATEGORIZED 되던 문제 해소).
    """
    post = entry.get("post_data") or {}
    category = post.get("category")
    if isinstance(category, str) and category.strip() in _active_category_set():
        return category.strip()

    # post_data.category 가 None/빈 문자열/모르는 값이면 제목 기반으로 감지
    title = entry.get("title") or post.get("title") or ""
    if not title:
        return _UNCATEGORIZED
    try:
        from core.content_generator import detect_category as _detect
    except (ImportError, AttributeError):
        return _UNCATEGORIZED
    try:
        # 제목에 'YYYY-MM-DD — ' prefix 가 남아 있어도 detect_category 는
        # 한국어 키워드 매칭이라 영향 받지 않지만, 안전을 위해 prefix 제거.
        clean = re.sub(r"^\s*\d{4}-\d{2}-\d{2}\s*[—–\-]?\s*", "", title)
        detected = _detect(clean)
    except (TypeError, ValueError) as exc:
        log.debug(f"[draft_store] detect_category 예외: {exc}")
        return _UNCATEGORIZED
    if isinstance(detected, str) and detected in _active_category_set():
        return detected
    return _UNCATEGORIZED


def _normalize_status_filter(
    status: Union[str, Iterable[str], None],
) -> Optional[Union[str, list]]:
    """count_by_category / find_drafts_by_category 의 status 인자를
    load_drafts 가 받는 형식(str | list | None)으로 변환한다.
    """
    if status is None:
        return None
    if isinstance(status, str):
        return status
    # set/tuple/list 모두 지원
    return list(status)


def count_by_category(
    status: Union[str, Iterable[str], None] = "draft_ready",
) -> dict:
    """카테고리별 보관본 카운트를 dict 로 반환.

    Args:
        status: 'draft_ready' | 'uploaded' | str list | None(전체).

    Returns:
        12 카테고리 + '_uncategorized' 가 항상 포함된 dict.
        값이 0 인 카테고리도 빠지지 않는다 (GUI 그리드 표시 안정성).
        예: {"청년": 0, "복지": 0, ..., "디지털": 30, "_uncategorized": 0}.
        예외 발생 시 12 키 0 dict (+ _uncategorized=0) 반환 — 호출 측이 안전하게 표시 가능.
    """
    categories = _examplebrand_categories()
    counts: dict = {cat: 0 for cat in categories}
    counts[_UNCATEGORIZED] = 0

    try:
        drafts = load_drafts(status_filter=_normalize_status_filter(status))
    except (OSError, ValueError) as exc:
        log.warning(f"[draft_store] count_by_category load 실패: {exc}")
        return counts

    for entry in drafts:
        cat = _resolve_category(entry)
        if cat in counts:
            counts[cat] += 1
        else:
            counts[_UNCATEGORIZED] += 1
    return counts


def find_drafts_by_category(
    category: str,
    status: str = "draft_ready",
) -> list:
    """특정 카테고리의 보관본 리스트를 created_at 오름차순으로 반환.

    Args:
        category: 12 카테고리 중 하나. 빈 문자열·None·범위 밖이면 빈 리스트.
            특수값 '_uncategorized' 는 카테고리 미정 항목을 반환.
        status: 단일 status 문자열 (기본 'draft_ready').

    Returns:
        load_drafts 와 동일 형식의 dict 리스트.
    """
    if not category or not isinstance(category, str):
        return []
    cat_normalized = category.strip()
    if cat_normalized not in _examplebrand_categories() and cat_normalized != _UNCATEGORIZED:
        return []

    try:
        drafts = load_drafts(status_filter=status)
    except (OSError, ValueError) as exc:
        log.warning(f"[draft_store] find_drafts_by_category load 실패: {exc}")
        return []

    return [entry for entry in drafts if _resolve_category(entry) == cat_normalized]


def check_coverage(candidates: list) -> dict:
    """발행 후보 리스트에 대한 카테고리별 매칭 가능 여부 분석.

    Args:
        candidates: [{"title": str, "category": str | None, ...}, ...]
            category 가 None/빈 문자열/12 외 값이면 detect_category(title) 로
            즉석 계산해 정규화.

    Returns:
        {
            "by_category": {
                "복지": {"needed": 3, "available": 0, "missing": 3, "covered": False},
                "디지털": {"needed": 5, "available": 30, "missing": 0, "covered": True},
                ...
            },
            "missing_categories": ["복지", "교육", "주거"],   # needed > available > 0 등
            "total_candidates": N,
            "fully_covered": True/False,
        }
        후보 리스트가 비어 있으면 fully_covered=True, total_candidates=0.
    """
    summary: dict = {
        "by_category": {},
        "missing_categories": [],
        "total_candidates": 0,
        "fully_covered": True,
    }
    if not candidates:
        return summary

    # 1) 후보별 카테고리 정규화 → needed 집계
    needed_counts: dict = {}
    for cand in candidates:
        if not isinstance(cand, dict):
            continue
        title = (cand.get("title") or "").strip()
        cat = (cand.get("category") or "").strip()
        if cat not in _examplebrand_categories():
            # title 기반 즉석 감지 (post_history 등 외부 데이터 동일 패턴)
            cat = _resolve_category({
                "title": title,
                "post_data": {"category": None, "title": title},
            })
        # _resolve_category 가 "_uncategorized" 를 반환할 수 있다 — 후보 단계의
        # 미정 카테고리는 매칭 불가능 카테고리로 따로 카운트한다.
        needed_counts[cat] = needed_counts.get(cat, 0) + 1
    summary["total_candidates"] = sum(needed_counts.values())

    # 2) 보관본 카운트 (draft_ready 상태만)
    available_counts = count_by_category(status="draft_ready")

    # 3) 카테고리별 매칭 매트릭스
    missing: List[str] = []
    by_category: dict = {}
    for cat, needed in needed_counts.items():
        available = available_counts.get(cat, 0)
        missing_n = max(0, needed - available)
        covered = missing_n == 0
        by_category[cat] = {
            "needed": needed,
            "available": available,
            "missing": missing_n,
            "covered": covered,
        }
        if not covered:
            missing.append(cat)

    summary["by_category"] = by_category
    summary["missing_categories"] = missing
    summary["fully_covered"] = (len(missing) == 0)
    return summary


def migrate_missing_categories(*, dry_run: bool = False) -> dict:
    """post_data.json 의 category 필드가 null/누락된 항목을 detect_category 로 백필.

    이미 카테고리가 있는 항목은 건드리지 않는다. 손상된 JSON·읽기 실패는
    failed 로 카운트하고 다음 항목으로 진행한다.

    Args:
        dry_run: True 면 디스크 쓰기 없이 카운트만 반환.

    Returns:
        {
          "scanned": N, "updated": M, "failed": K, "dry_run": bool,
          "by_category": {"디지털": 2, "복지": 1, ...},   # detect 결과 카운트
          "unmatched":   L,                              # detect 미상으로 남은 항목 수
        }
    """
    result = {
        "scanned": 0, "updated": 0, "failed": 0, "dry_run": bool(dry_run),
        "by_category": {}, "unmatched": 0,
    }
    if not _RUNS_DIR.exists():
        return result

    try:
        from core.content_generator import detect_category as _detect
    except (ImportError, AttributeError) as exc:
        log.warning(f"[draft_store] migrate_missing_categories: detect_category import 실패: {exc}")
        return result

    try:
        children = list(_RUNS_DIR.iterdir())
    except OSError as exc:
        log.warning(f"[draft_store] migrate_missing_categories scan 실패: {exc}")
        return result

    for child in children:
        if not child.is_dir():
            continue
        post_path = child / _POST_DATA_FILE
        if not post_path.exists():
            continue
        result["scanned"] += 1

        payload = _read_json(post_path)
        if payload is None:
            result["failed"] += 1
            continue

        existing = payload.get("category")
        if isinstance(existing, str) and existing.strip() in _active_category_set():
            continue  # 이미 정상 카테고리 — 건너뜀

        title = payload.get("title") or ""
        if not title:
            continue
        try:
            clean = re.sub(r"^\s*\d{4}-\d{2}-\d{2}\s*[—–\-]?\s*", "", title)
            detected = _detect(clean)
        except (TypeError, ValueError) as exc:
            log.debug(f"[draft_store] migrate detect 예외 {child.name}: {exc}")
            result["failed"] += 1
            continue
        if not isinstance(detected, str) or detected not in _active_category_set():
            # default 또는 모르는 값 — 백필하지 않는다 (남겨두면 _uncategorized 로 표시)
            result["unmatched"] += 1
            continue

        # 미리보기·로그용 카테고리 카운트 집계 (dry_run 여부와 무관)
        result["by_category"][detected] = result["by_category"].get(detected, 0) + 1

        if dry_run:
            result["updated"] += 1
            continue

        payload["category"] = detected
        try:
            _atomic_write_json(post_path, payload)
            result["updated"] += 1
        except OSError as exc:
            log.warning(f"[draft_store] migrate write 실패 {child.name}: {exc}")
            result["failed"] += 1

    if not dry_run and result["updated"] > 0:
        _invalidate_cache()
    return result


__all__ = [
    # 기존 API
    "save_draft", "load_drafts", "mark_status", "find_draft_for_title",
    "purge_uploaded",
    # 발행 진행 상태 (2026-09-18 무인 운영 M1, additive)
    "set_publish_state", "publish_state_of", "blocks_auto_republish",
    # 신규 카테고리 API (2026-05-16)
    "count_by_category", "find_drafts_by_category", "check_coverage",
    "migrate_missing_categories",
]
