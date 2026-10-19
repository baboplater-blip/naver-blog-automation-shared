# core/blog_service.py
# 🔌 블로그 서비스 경계 — GUI 독립 조회/이미지 채움 API (2026-09-09)
# ─────────────────────────────────────────────────────────────────────────────
# 목적: 쇼츠스튜디오 같은 외부 파이썬 프로세스가 나중에 import 또는 CLI 로 이 블로그
# 자동화의 보관본(글)·이미지·메타데이터를 읽어갈 수 있는 최소 경계. **지금은 아무 것도
# 연결돼 있지 않다** — 이 모듈은 표면만 만든다(호출자 0). `core/shorts_studio_client.py`
# 는 방향이 반대(블로그 → 쇼츠 서버 호출)인 기존 클라이언트이며 이 모듈과 무관하다.
#
# 설계 원칙(전부 하드 제약):
# - import 부작용 0. 이 파일의 최상단 import 는 표준 라이브러리(+ `from __future__`)만.
#   draft_store·draft_image_fill·config.settings·config.profile 은 전부 **함수 안에서
#   지연 임포트**한다 — import 만으로 tkinter·playwright·selenium·서버·LLM·서브프로세스가
#   뜨면 안 된다(테스트 `test_blog_service.py::test_import_has_no_side_effects` 가 고정).
# - fail-open: 이 모듈의 어떤 공개 함수도 예외를 밖으로 던지지 않는다. 조회 함수는 빈
#   결과(빈 리스트·None)로, 실행 함수는 reason 이 담긴 dict 로 실패를 알린다.
# - 발행 경로 없음: 이 모듈은 절대 업로드하지 않는다. 업로드 진입점을 추가하지 않는다.
# - 판정 로직 복제 금지: 이미지 채움 게이트(켜짐/엔진) 판정은 반드시
#   `core.draft_image_fill.image_fill_gate()` 에 위임한다. 그 함수가 아직 없거나(구버전)
#   임포트에 실패하면 "gate_unavailable" 로 안전 수렴한다(enabled=False) — 판정 로직을
#   여기 다시 베끼지 않는다(과거 `gui/tabs/home.py:_image_fill_enabled_setting` 이 게이트
#   로직을 복제해 두 곳이 어긋나던 사고 클래스를 이 모듈로 흡수해 없앤다).
from __future__ import annotations

import argparse
import inspect
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Union


# ─────────────────────────────────────────────────────────────────────────────
# 공개 데이터 계약
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class BlogDraftRef:
    """보관본 목록용 요약(가벼움 — 본문·전체 이미지 경로는 담지 않는다)."""

    slug: str
    title: str
    category: Optional[str]
    status: str
    created_at: Optional[str]
    schedule_at: Optional[str]
    url: Optional[str]
    # ⚠️ 아래 두 숫자는 **파일 장수**다 — "발행 준비 완료"가 아니다.
    #    준비 여부는 반드시 `images_ready` 를 볼 것(단일 판정 기준).
    image_count: int          # 실제로 파일이 존재하는 이미지 수(디스크 기준)
    missing_image_count: int  # image_paths 에 있으나 파일이 사라진 수
    # ── 발행 게이트와 **같은 기준**의 판정 (2026-09-23) ────────────────────
    # 위 두 숫자는 "파일이 디스크에 있는가"만 본다. 발행은 거기에 더해 **출처**를
    # 보므로, 파일이 4장 다 있어도 발행이 막히는 글이 있다. 두 기준을 한 계층에서
    # 나란히 돌려줘, 이 API 를 쓰는 쪽이 "이미지 4장 = 발행 준비 완료"라고 잘못
    # 읽지 않게 한다. 판정은 `core.draft_image_fill.images_complete` 한 곳에만 있다.
    # 기본값을 둔 이유는 **계약이 additive** 라서다 — 이 dataclass 를 직접 만드는
    # 기존 코드/테스트는 한 줄도 고치지 않아도 되고, 그때 값은 "판정 안 함"이다.
    images_ready: bool = False        # 지금 이대로 발행할 수 있는가(슬롯 전부 확인됨)
    unverified_image_count: int = 0   # 파일은 있으나 출처를 확인할 수 없는 수
    image_block_reason: str = "unknown"   # "" | no_plan | unverified_assets | ...
    image_block_detail: str = ""      # 사유 세부(unverifiable=기록 손상 / reference_capture …)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class BlogDraft(BlogDraftRef):
    """보관본 단건 상세(본문·태그·이미지 경로 포함)."""

    # 부모가 기본값을 갖게 되면서 여기도 전부 기본값이 필요하다(dataclass 규칙).
    # 실제 생성은 전부 키워드 인자라 동작은 그대로다.
    content: str = ""          # 보관본 본문(HTML 또는 마크다운, 그대로)
    tags: List[str] = field(default_factory=list)
    image_paths: List[str] = field(default_factory=list)   # 실제로 존재하는 것만
    missing_image_paths: List[str] = field(default_factory=list)
    image_dir: Optional[str] = None
    force_image_insert: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


# ─────────────────────────────────────────────────────────────────────────────
# 내부 헬퍼(조회) — 전부 fail-open
# ─────────────────────────────────────────────────────────────────────────────
def _load_draft_store() -> Any:
    """core.draft_store 지연 임포트. 실패하면 None(호출자는 빈 결과로 수렴)."""
    try:
        from core import draft_store
        return draft_store
    except Exception:  # noqa: BLE001 — 임포트 실패는 조회 0건으로 수렴
        return None


def _split_valid_missing(image_paths: Any) -> tuple:
    """image_paths 를 (실존 파일, 유실 경로)로 가른다. 어떤 경로 예외도 유실로 처리."""
    valid: List[str] = []
    missing: List[str] = []
    for raw in list(image_paths or []):
        text = str(raw or "").strip()
        if not text:
            continue
        try:
            exists = Path(text).is_file()
        except (OSError, ValueError):
            exists = False
        (valid if exists else missing).append(text)
    return valid, missing


def _resolve_image_dir(paths: List[str]) -> Optional[str]:
    """유효/유실 경로 중 첫 항목의 부모 폴더를 이미지 폴더로 삼는다(안내용, 최선 노력)."""
    for p in paths:
        try:
            return str(Path(p).parent)
        except (TypeError, ValueError, OSError):
            continue
    return None


def _entry_image_paths(entry: dict) -> list:
    post = entry.get("post_data") or {}
    return list(post.get("image_paths") or [])


def image_readiness(post_data: dict) -> dict:
    """저장된 글 하나의 **발행 기준** 이미지 상태(부작용 0·네트워크 0).

    판정은 `core.draft_image_fill.images_complete()` 에 **전부 위임**한다 — 이 파일에
    두 번째 판정 규칙을 만들지 않는다(두 기준이 갈리는 것이 애초에 이 결함이었다).
    그 모듈을 못 불러오면 판정을 지어내지 않고 ``reason="unknown"`` 으로 남긴다.

    Returns:
        {"ready": bool, "unverified": int, "reason": str, "detail": str}
    """
    out = {"ready": False, "unverified": 0, "reason": "unknown", "detail": ""}
    dif = _load_draft_image_fill()
    if dif is None or not callable(getattr(dif, "images_complete", None)):
        return out
    try:
        complete, detail = dif.images_complete(post_data if isinstance(post_data, dict) else {})
    except Exception:  # noqa: BLE001 — 판정 실패는 "모름"(추측 금지)
        return out
    detail = detail if isinstance(detail, dict) else {}
    reasons = detail.get("unverified_reasons")
    out["ready"] = bool(complete)
    out["unverified"] = int(detail.get("unverified") or 0)
    out["reason"] = "" if complete else str(detail.get("reason") or "")
    if isinstance(reasons, dict) and reasons:
        # 가장 많은 사유 하나를 대표로 — 사람이 할 조치가 사유마다 다르다.
        out["detail"] = str(max(reasons.items(), key=lambda kv: kv[1])[0])
    return out


def _to_ref(entry: dict) -> BlogDraftRef:
    post = entry.get("post_data") or {}
    valid, missing = _split_valid_missing(_entry_image_paths(entry))
    category = post.get("category")
    ready = image_readiness(post)
    return BlogDraftRef(
        slug=str(entry.get("slug") or ""),
        title=str(entry.get("title") or post.get("title") or ""),
        category=category if isinstance(category, str) and category.strip() else None,
        status=str(entry.get("status") or ""),
        created_at=entry.get("created_at"),
        schedule_at=entry.get("schedule_at"),
        url=entry.get("url"),
        image_count=len(valid),
        missing_image_count=len(missing),
        images_ready=bool(ready["ready"]),
        unverified_image_count=int(ready["unverified"]),
        image_block_reason=str(ready["reason"]),
        image_block_detail=str(ready["detail"]),
    )


# ─────────────────────────────────────────────────────────────────────────────
# 공개 API — 조회(읽기 전용)
# ─────────────────────────────────────────────────────────────────────────────
def list_drafts(
    *,
    status: Union[str, List[str], None] = "draft_ready",
    needs_images: Optional[bool] = None,
    limit: Optional[int] = None,
) -> List[BlogDraftRef]:
    """보관본 목록 요약.

    Args:
        status: draft_store.load_drafts 와 동일 — str | list[str] | None(전체).
        needs_images: **발행 게이트와 같은 기준**(``images_ready``)으로 가른다 —
            True 면 "지금 이대로는 발행할 수 없는"(이미지 작업이 남은) 보관본만,
            False 면 "그대로 발행할 수 있는" 보관본만, None(기본) 이면 필터 없음.
            ⚠️ 파일 장수(``image_count``)로 가르지 **않는다**. 파일이 4장 다 있어도
            출처를 확인할 수 없거나 계획 슬롯이 남았으면 발행이 막히므로,
            "파일 N장 = 이미지 준비 완료"라고 답하면 거짓이 된다.
            판정을 못 하는 경우(``images_complete`` 미로드 등)는 **준비 안 됨**으로
            본다 — 모르는 것을 "발행 가능"이라 말하지 않는다.
        limit: 앞에서부터 N개만(생성 시각 오름차순 유지).

    Returns:
        BlogDraftRef 리스트. 어떤 실패도 빈 리스트로 수렴(fail-open).
    """
    draft_store = _load_draft_store()
    if draft_store is None:
        return []
    try:
        raw = draft_store.load_drafts(status_filter=status)
    except Exception:  # noqa: BLE001 — 스캔 실패는 빈 목록
        return []

    refs: List[BlogDraftRef] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        try:
            refs.append(_to_ref(entry))
        except Exception:  # noqa: BLE001 — 손상된 항목 1건은 건너뛴다
            continue

    # 단일 판정 기준: `core.draft_image_fill.images_complete` → `images_ready`.
    # (파일 장수는 `image_count` 로 그대로 노출하되 **가르는 데는 쓰지 않는다**.)
    if needs_images is True:
        refs = [r for r in refs if not r.images_ready]
    elif needs_images is False:
        refs = [r for r in refs if r.images_ready]

    if limit is not None:
        try:
            limit_n = int(limit)
        except (TypeError, ValueError):
            limit_n = None
        if limit_n is not None and limit_n >= 0:
            refs = refs[:limit_n]
    return refs


def get_draft(slug: str) -> Optional[BlogDraft]:
    """보관본 단건 상세(본문·태그·이미지 경로 포함). 없거나 실패하면 None."""
    if not slug or not isinstance(slug, str):
        return None
    draft_store = _load_draft_store()
    if draft_store is None:
        return None
    try:
        drafts = draft_store.load_drafts(status_filter=None)
    except Exception:  # noqa: BLE001
        return None
    entry = next((d for d in drafts if isinstance(d, dict) and d.get("slug") == slug), None)
    if entry is None:
        return None
    try:
        ref = _to_ref(entry)
    except Exception:  # noqa: BLE001
        return None

    post = entry.get("post_data") or {}
    valid, missing = _split_valid_missing(_entry_image_paths(entry))
    tags_raw = post.get("tags") or []
    tags = [str(t) for t in tags_raw] if isinstance(tags_raw, (list, tuple)) else []

    return BlogDraft(
        slug=ref.slug, title=ref.title, category=ref.category, status=ref.status,
        created_at=ref.created_at, schedule_at=ref.schedule_at, url=ref.url,
        image_count=ref.image_count, missing_image_count=ref.missing_image_count,
        images_ready=ref.images_ready,
        unverified_image_count=ref.unverified_image_count,
        image_block_reason=ref.image_block_reason,
        image_block_detail=ref.image_block_detail,
        content=str(post.get("content") or ""),
        tags=tags,
        image_paths=valid,
        missing_image_paths=missing,
        image_dir=_resolve_image_dir(valid or missing),
        force_image_insert=bool(post.get("_force_image_insert")),
    )


def get_assets(slug: str) -> dict:
    """보관본의 이미지 자산만 뽑은 얇은 dict. 실패해도 항상 계약된 키를 갖는다."""
    empty = {"slug": str(slug or ""), "image_paths": [], "missing_image_paths": [],
             "image_dir": None}
    draft = get_draft(slug)
    if draft is None:
        return empty
    return {
        "slug": draft.slug,
        "image_paths": list(draft.image_paths),
        "missing_image_paths": list(draft.missing_image_paths),
        "image_dir": draft.image_dir,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 공개 API — 이미지 채움 게이트/실행
# ─────────────────────────────────────────────────────────────────────────────
def _load_draft_image_fill() -> Any:
    try:
        from core import draft_image_fill
        return draft_image_fill
    except Exception:  # noqa: BLE001
        return None


def _supported_kwargs(fn: Any, names: tuple) -> set:
    """``fn`` 이 실제로 받아들이는 키워드 이름만 골라 돌려준다(호출 **전** 호환성 판별).

    "일단 부르고 TypeError 나면 인자를 빼서 다시 부른다"는 재시도 패턴을 쓰지 않기 위한
    헬퍼다 — 그 패턴은 함수가 **내부에서** 던진 TypeError 까지 시그니처 불일치로 오인해
    이미 이미지를 생성해 놓고 **한 번 더 생성**할 수 있다(비용·중복 산출물). 시그니처를
    읽을 수 없는 호출 가능 객체(C 확장·일부 목)는 보수적으로 "전부 지원"으로 본다 — 이
    저장소의 현재 시그니처가 그 이름들을 전부 갖고 있기 때문이다.
    """
    try:
        params = list(inspect.signature(fn).parameters.values())
    except (TypeError, ValueError):
        return set(names)
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params):
        return set(names)
    allowed = {p.name for p in params
               if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD,
                             inspect.Parameter.KEYWORD_ONLY)}
    return {n for n in names if n in allowed}


def fill_target_count(*, topup: bool = False) -> int:
    """이미지 채움 대상 편수 — ``core.draft_image_fill.fill_targets(topup=)`` 길이.

    ★카운트와 실행이 **같은 선별 규칙**을 쓰게 하는 단일 창구다. GUI 는 실행에 넘길
    ``topup`` 과 **같은 값**으로 이 함수를 불러야 안내 숫자와 실제 처리 편수가 어긋나지
    않는다(부분 이미지 보충 대상이 안내에서만 빠지던 문제).

    ``fill_targets`` 가 없는 구버전에서만 ``list_drafts(needs_images=True)`` 로 폴백한다
    — 그 폴백은 "발행 게이트를 통과하지 못하는 글"을 세므로 자동 채움 대상이 아닌
    글(출처 미확인이라 사람이 골라야 하는 글)까지 포함될 수 있다. 즉 **근사치**이며
    ``fill_targets`` 가 있으면 그쪽이 항상 우선이다.
    어떤 실패도 예외로 새어 나가지 않는다(fail-open, 실패 시 0).
    """
    dif = _load_draft_image_fill()
    if dif is not None:
        fn = getattr(dif, "fill_targets", None)
        if callable(fn):
            kw = {"topup": topup} if "topup" in _supported_kwargs(fn, ("topup",)) else {}
            try:
                return len(list(fn(**kw) or []))
            except Exception:  # noqa: BLE001 — 대상 계산 실패는 폴백으로
                pass
    try:
        return len(list_drafts(needs_images=True))
    except Exception:  # noqa: BLE001
        return 0


def _probe_engine_ready(engine: str) -> tuple:
    """probe=True 일 때만 호출 — 실제 서브프로세스를 실행해 엔진 준비 상태를 본다.

    Returns: (ready: bool|None, detail: str).
    """
    if engine != "codex":
        return None, f"'{engine}' 엔진은 자동 점검을 지원하지 않아요 — 수동으로 확인하세요."
    try:
        from core.codex_image import probe as _probe
    except Exception as exc:  # noqa: BLE001
        return None, f"점검 모듈을 불러오지 못했어요: {type(exc).__name__}"
    try:
        state = _probe() or {}
    except Exception as exc:  # noqa: BLE001 — probe() 자체는 fail-open 이지만 이중 방어
        return None, f"점검 중 오류가 있었어요: {type(exc).__name__}"
    return bool(state.get("ok")), str(state.get("detail") or state.get("reason") or "")


def image_fill_status(*, probe: bool = False, topup: bool = False) -> dict:
    """이미지 채움 기능의 현재 상태.

    Args:
        probe: False(기본)면 서브프로세스를 절대 실행하지 않는다(``ready`` 는 None).
            True 일 때만 ``core.codex_image.probe()`` 를 호출한다 — GUI 의 기본 경로
            (버튼 노출·대상 카운트)에서는 절대 True 로 부르지 말 것(수십 초 블록 위험).
        topup: ``targets`` 를 셀 때의 선별 규칙. 실행에 넘길 값과 같아야 안내와 실제
            처리 편수가 어긋나지 않는다(``fill_target_count`` 참조).

    Returns:
        {"enabled": bool, "engine": str, "reason": str, "targets": int,
         "ready": bool|None, "detail": str}.
    """
    result: Dict[str, Any] = {
        "enabled": False, "engine": "codex", "reason": "gate_unavailable",
        "targets": 0, "ready": None, "detail": "",
    }

    dif = _load_draft_image_fill()
    gate: Any = None
    if dif is not None:
        gate_fn = getattr(dif, "image_fill_gate", None)
        if callable(gate_fn):
            try:
                gate = gate_fn()
            except Exception:  # noqa: BLE001 — 게이트 호출 실패도 gate_unavailable 로 수렴
                gate = None
    if isinstance(gate, dict):
        result["enabled"] = bool(gate.get("enabled", False))
        result["engine"] = str(gate.get("engine") or "codex") or "codex"
        result["reason"] = str(gate.get("reason") or "")

    result["targets"] = fill_target_count(topup=topup)

    if probe:
        ready, detail = _probe_engine_ready(result["engine"])
        result["ready"] = ready
        result["detail"] = detail

    return result


def _fill_summary_base(status: dict, **overrides: Any) -> dict:
    base: Dict[str, Any] = {
        "filled": 0, "scanned": 0, "targets": 0, "processed": 0,
        "skipped": {}, "stopped_early": False,
        "engine": status.get("engine"), "enabled": status.get("enabled"),
    }
    base.update(overrides)
    return base


def fill_images(
    *,
    limit: Optional[int] = None,
    slug: Optional[str] = None,
    topup: bool = False,
    progress_cb: Optional[Any] = None,
    **kwargs: Any,
) -> dict:
    """이미지 채움을 실행한다(발행 아님).

    단건(``slug``)이든 배치든 **언제나** ``core.draft_image_fill.run_fill_batch`` 하나로
    간다. 예전에는 단건만 ``fill_images_for_draft`` + ``save_draft`` 를 직접 조합했는데,
    그 경로는 배치가 가진 보호막 — 게이트 판정·발행 중 거부·프로필 격리 락
    (``logs/image_fill.lock``)·저장 직전 status 재조회(발행된 글 부활 방지)·실패 사유
    영속(``logs/image_fill_state.json``) — 을 전부 우회했다. 이제 단건은
    ``run_fill_batch(slug=...)`` 로 같은 락·같은 검사를 공유한다.

    ⚠️ 락이 막는 범위: ``run_fill_batch`` 를 거치는 실행끼리다(GUI·CLI·이 서비스·
    스케줄러의 배치 훅). ``fill_images_for_draft`` 를 직접 부르는 코드(발행 파이프라인의
    인라인 채움 등)는 이 락 밖에 있다 — 그쪽은 ``_publish_in_progress()`` 검사가 반대
    방향으로 막는다.

    Args:
        progress_cb: 한 편이 끝날 때마다 `{"done","total","failed","slug","ok"}` 를 받는
            콜백(GUI 진행 표시). 워커 스레드에서 호출되므로 GUI 는 그 안에서 Tk 를 직접
            건드리지 말고 `ui_after` 로 넘겨야 한다.

    한 번 결정한 호출은 **한 번만** 실행한다 — 인자 호환성은 호출 전에
    ``_supported_kwargs`` 로 판별하며, TypeError 를 잡아 다시 부르지 않는다(내부에서 난
    TypeError 를 시그니처 불일치로 오인해 이미 생성한 이미지를 재생성하는 사고 방지).

    Returns:
        run_fill_batch 요약과 같은 형태의 dict + "engine"/"enabled" 보강.
        어떤 실패도 예외를 던지지 않고 skipped 사유가 담긴 dict 를 돌려준다.
    """
    status = image_fill_status(probe=False, topup=topup)
    dif = _load_draft_image_fill()
    if dif is None:
        return _fill_summary_base(status, skipped={"draft_image_fill_unavailable": 1})

    run_fn = getattr(dif, "run_fill_batch", None)
    if not callable(run_fn):
        return _fill_summary_base(status, skipped={"run_fill_batch_unavailable": 1})

    call_kwargs: Dict[str, Any] = dict(kwargs)
    if limit is not None:
        call_kwargs["limit"] = limit

    supported = _supported_kwargs(
        run_fn, ("topup", "slug", "progress_cb", "only_slugs", "replace_unverified"))
    if "topup" in supported:
        call_kwargs["topup"] = topup
    if call_kwargs.get("replace_unverified") and "replace_unverified" not in supported:
        # 구버전에는 '확인되지 않은 자산만 다시 만들기'가 없다. 그냥 부르면 기존
        # 자산을 그대로 재사용한 채 아무 것도 고치지 않고 "완료"로 보이므로,
        # 전체 배치로 대신 돌리지 않고 정직하게 거절한다(회복 경로 보호).
        return _fill_summary_base(status, skipped={"replace_unverified_unsupported": 1})
    if "only_slugs" in call_kwargs and "only_slugs" not in supported:
        # 구버전에는 목록 제한이 없다. 전체 배치로 대신 돌리면 요청하지 않은 글까지
        # 만들게 되므로, 아무것도 하지 않고 정직하게 보고한다(무인 준비 경로 보호).
        return _fill_summary_base(status, skipped={"only_slugs_unsupported": 1})
    if progress_cb is not None and "progress_cb" in supported:
        # 진행 표시는 **부가 기능**이다 — 구버전 run_fill_batch 에는 이 인자가 없으므로
        # 조용히 빼고 실행한다(진행률만 안 보일 뿐 채움은 정상 동작).
        call_kwargs["progress_cb"] = progress_cb
    if slug:
        if "slug" not in supported:
            # 구버전 run_fill_batch 에는 슬러그 필터가 없다. 전체 배치로 대신 돌리면
            # 요청하지 않은 글까지 생성하므로, 아무것도 하지 않고 정직하게 보고한다.
            return _fill_summary_base(status, skipped={"slug_filter_unsupported": 1})
        call_kwargs["slug"] = slug

    try:
        summary = run_fn(**call_kwargs)
    except Exception as exc:  # noqa: BLE001
        return _fill_summary_base(status, skipped={f"batch_exception_{type(exc).__name__}": 1})

    if not isinstance(summary, dict):
        return _fill_summary_base(status, skipped={"batch_bad_result": 1})
    summary.setdefault("engine", status.get("engine"))
    summary.setdefault("enabled", status.get("enabled"))
    return summary


# ─────────────────────────────────────────────────────────────────────────────
# CLI 어댑터 — 쇼츠스튜디오 등 외부 프로세스가 쓸 최소 진입점
# python -m core.blog_service status|list|get <slug>|assets <slug>|fill [--limit N] [--topup]
# ─────────────────────────────────────────────────────────────────────────────
def _print_json(payload: Any) -> None:
    """Windows cp949 콘솔에서 인코딩 불가 문자로 죽지 않는 안전 JSON 출력
    (core/codex_image.py::_print 와 동일 패턴)."""
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    try:
        print(text)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        print(text.encode(enc, errors="replace").decode(enc, errors="replace"))


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m core.blog_service",
        description="블로그 보관본 조회 + 이미지 채움 서비스 경계(읽기 전용 + fill만, 발행 없음).")
    parser.add_argument("--json", action="store_true",
                        help="JSON 출력(기본 출력도 이미 JSON — 하위 호환용 무해 플래그)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_status = sub.add_parser("status", help="이미지 채움 게이트 상태")
    p_status.add_argument("--probe", action="store_true",
                          help="엔진 준비 상태까지 점검(서브프로세스 실행 — 느릴 수 있음)")

    p_list = sub.add_parser("list", help="보관본 목록 요약")
    p_list.add_argument("--status", default="draft_ready",
                        help="상태 필터(기본 draft_ready, 'all' 이면 전체)")
    p_list.add_argument("--needs-images", action="store_true",
                        help="지금 이대로는 발행할 수 없는(이미지 작업이 남은) 보관본만 "
                             "— 파일 장수가 아니라 발행 게이트와 같은 기준")
    p_list.add_argument("--limit", type=int, default=None)

    p_get = sub.add_parser("get", help="보관본 단건 상세(본문 포함)")
    p_get.add_argument("slug")

    p_assets = sub.add_parser("assets", help="보관본 이미지 자산만")
    p_assets.add_argument("slug")

    p_fill = sub.add_parser("fill", help="이미지 채움 실행(발행 아님)")
    p_fill.add_argument("--limit", type=int, default=None)
    p_fill.add_argument("--slug", default=None, help="이 슬러그 1편만 채운다")
    p_fill.add_argument("--topup", action="store_true", help="부분 누락만 보충")

    return parser


def _cli(argv: Optional[List[str]] = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    if args.command == "status":
        payload: Any = image_fill_status(probe=bool(getattr(args, "probe", False)))
    elif args.command == "list":
        status_filter: Union[str, None] = None if args.status == "all" else args.status
        needs = True if args.needs_images else None
        drafts = list_drafts(status=status_filter, needs_images=needs, limit=args.limit)
        payload = {"count": len(drafts), "drafts": [d.to_dict() for d in drafts]}
    elif args.command == "get":
        draft = get_draft(args.slug)
        payload = draft.to_dict() if draft is not None else {"error": "not_found", "slug": args.slug}
    elif args.command == "assets":
        payload = get_assets(args.slug)
    elif args.command == "fill":
        payload = fill_images(limit=args.limit, slug=args.slug, topup=bool(args.topup))
    else:  # pragma: no cover — argparse 의 subparsers(required=True) 가 선차단
        payload = {"error": "unknown_command"}

    _print_json(payload)
    return 1 if isinstance(payload, dict) and payload.get("error") else 0


__all__ = [
    "BlogDraftRef", "BlogDraft",
    "list_drafts", "get_draft", "get_assets",
    "image_fill_status", "fill_target_count", "fill_images",
]


if __name__ == "__main__":
    raise SystemExit(_cli())
