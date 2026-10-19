# core/draft_restyle.py — 보관본을 새 유전자(블루프린트)로 제자리 교체 (2026-08-26)
"""draft_ready 보관본(구 스타일: "안녕하세요"·"2026 X 조건은?" 획일화)을 블루프린트
제목·본문으로 **제자리 재생성**하는 안전한 배치 도구.

기존 upgrade_draft 가 부적합했던 이유를 전부 해소한다:
  · min_gain 게이트 없음 — 목표가 '스타일 교체'라 점수가 안 올라도(같거나 낮아도)
    _failed 만 아니면 새 유전자를 채택한다.
  · upgrade 버킷만이 아니라 **전체 draft_ready** 를 대상으로(publish 버킷 옛글 포함).
  · **성공했을 때만 처리됨 마커** — 재생성이 성공(_failed 아님)한 경우에만 옛 post_data.json
    을 post_data.prestyle.json 으로 1회 복사하고 새 본문을 저장한다(되돌리기 가능).
    백업 존재 = "성공적으로 리스타일됨". 실패(fact_gate/QA/인증만료/쿼터소진/빈응답/예외)면
    백업을 만들지 않아 다음 배치에서 **자동 재시도**된다(조용한 유실 0).
  · **영속 실패 상한** — 슬러그별 연속 실패 횟수를 프로필 격리 상태(logs/restyle_fails.json)
    에 기록, 연속 N회(_FAIL_CAP, 기본 3) 도달 시 hard_skipped 로 영구 제외(옛 원본 안전).
    성공하면 그 슬러그 카운트를 삭제(리셋).

안전 원칙:
  · BLUEPRINT_ENABLED OFF 면 리스타일이 무의미 → 경고+abort(force 로 강제 가능).
  · _failed/_stopped/빈 본문은 저장하지 않고 옛 원본을 유지(발행 차단 콘텐츠로 안 덮음).
  · 슬러그·image_paths·schedule_at·category 보존.
  · 멱등·재개 가능(resume). atomic write(draft_store._atomic_write_json 재사용).
  · draft_store/generate_blog_post/blueprint 기존 코드 무수정(재사용만).

수동: python -m core.draft_restyle            # 상태
      python -m core.draft_restyle --run 5    # 상위 5편 리스타일(블루프린트 ON 필요)
      python -m core.draft_restyle --restore  # 전체 원복
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

# schtasks 가 이 파일을 직접(python core/draft_restyle.py) 실행할 수 있게 패키지 루트를
# sys.path 에 올리고, **무거운 임포트 전에** 프로필을 고정한다(logger·draft_store 경로가
# 임포트 시점에 활성 프로필로 확정되므로 — run_ops.py 패턴, 격리 감사 2026-07-19).
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if __name__ == "__main__":
    try:
        from config.profile_boot import pin_profile_from_argv
        pin_profile_from_argv()
    except Exception:  # noqa: BLE001
        pass

from config.logger import get_logger
from config.win_console import CONSOLE_ENCODING
from config import profile as _nbaprofile
from core import draft_store

log = get_logger(__name__)

# 옛 원본 백업 파일명(= 처리됨 재개 마커). runs/{slug}/ 아래에 둔다.
_PRESTYLE_FILE = "post_data.prestyle.json"

# schtasks 등록용 상수(run_ops/rank_monitor 미러 — 정적 이름 + 러너에 --profile).
_BASE_DIR = Path(__file__).resolve().parent.parent          # naver_blog_v3/
_TASK_NAME = "NaverBlogRestyle"


# ─────────────────────────────────────────────────────
# 경로 헬퍼 — draft_store 의 런타임 값을 그대로 참조(테스트가 _RUNS_DIR 격리 가능).
# ─────────────────────────────────────────────────────
def _run_dir(slug: str) -> Path:
    return draft_store._RUNS_DIR / slug


def _post_path(slug: str) -> Path:
    return _run_dir(slug) / draft_store._POST_DATA_FILE


def _status_path(slug: str) -> Path:
    return _run_dir(slug) / draft_store._STATUS_FILE


def _backup_path(slug: str) -> Path:
    return _run_dir(slug) / _PRESTYLE_FILE


def has_backup(slug: str) -> bool:
    """이 슬러그가 이미 리스타일 처리됐는지(원본 백업 존재)."""
    try:
        return _backup_path(slug).exists()
    except OSError:
        return False


# ─────────────────────────────────────────────────────
# 보조
# ─────────────────────────────────────────────────────
def _blueprint_on() -> bool:
    try:
        from config import settings as _s
        return bool(getattr(_s, "BLUEPRINT_ENABLED", False))
    except Exception:  # noqa: BLE001
        return False


def _order_by_ai_tell() -> bool:
    """RESTYLE_ORDER_BY_AI_TELL ON 이면 리스타일 배치가 AI 티 최악부터 처리(옵트인).

    기본 OFF — OFF 면 run_restyle_batch 정렬이 기존 수요 우선 그대로라 배치가 바이트
    동일하게 동작한다(스코어링 자체를 안 함). 설정 접근 실패는 안전하게 OFF 로 폴백.
    """
    try:
        from config import settings as _s
        return bool(getattr(_s, "RESTYLE_ORDER_BY_AI_TELL", False))
    except Exception:  # noqa: BLE001
        return False


# ── 연속 실패 상한(무한 재시도 방지) — 프로필 격리 상태 ─────────────
_FAIL_CAP_DEFAULT = 3


def _fail_cap() -> int:
    try:
        from config import settings as _s
        return max(1, int(getattr(_s, "RESTYLE_FAIL_CAP", _FAIL_CAP_DEFAULT)))
    except Exception:  # noqa: BLE001
        return _FAIL_CAP_DEFAULT


def _fails_path() -> Path:
    return _nbaprofile.app_data_root() / "logs" / "restyle_fails.json"


def _load_fails() -> dict:
    """슬러그별 연속 실패 횟수 {slug: int}. 파싱 실패는 빈 dict(fail-open·isinstance 가드)."""
    try:
        from config import safe_load_json
        data = safe_load_json(_fails_path(), default=dict)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _save_fails(data: dict) -> None:
    try:
        from config import atomic_write_json
        p = _fails_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(str(p), data)
    except Exception as exc:  # noqa: BLE001 — 상태 기록 실패는 재시도만 약화(치명 아님)
        log.debug("[draft_restyle] fail 상태 저장 실패(무시): %s", exc)


def _fail_count(slug: str) -> int:
    try:
        return int(_load_fails().get(slug, 0) or 0)
    except (TypeError, ValueError):
        return 0


def _record_fail(slug: str) -> int:
    """슬러그 연속 실패 +1, 새 카운트 반환."""
    data = _load_fails()
    n = int(data.get(slug, 0) or 0) + 1
    data[slug] = n
    _save_fails(data)
    return n


def _clear_fail(slug: str) -> None:
    """성공 시 그 슬러그 실패 카운트 리셋(삭제)."""
    data = _load_fails()
    if slug in data:
        del data[slug]
        _save_fails(data)


def is_hard_skipped(slug: str) -> bool:
    """연속 실패 상한 도달로 영구 제외 상태인가(옛 원본은 안전 유지)."""
    return _fail_count(slug) >= _fail_cap()


def _demand(title: str) -> int:
    """수요 신호 — draft_triage._demand_score 재사용. 실패 시 간단 프록시(길이 기반)."""
    try:
        from core.draft_triage import _demand_score
        return int(_demand_score(title or ""))
    except Exception:  # noqa: BLE001 — 수요 산출 실패는 정렬만 약화(치명 아님)
        return len((title or "").split())


def _aeo(html: str) -> Optional[float]:
    """AEO 점수(best-effort, 보고용). 실패하면 None."""
    try:
        from core.aeo_scorer import score as _score
        return round(float(_score(html or "").score), 1)
    except Exception:  # noqa: BLE001
        return None


def _html_to_prose(body: str) -> str:
    """저장된 HTML 본문을 평문으로 근사(AI 티 스코어링 입력용).

    ai_tell_calibrate._html_to_prose 를 재사용(태그→공백 + 엔티티 언이스케이프)하고,
    임포트 실패 시 간단 태그 strip 으로 폴백한다(fail-open).
    """
    if not isinstance(body, str) or not body:
        return ""
    try:
        from core.ai_tell_calibrate import _html_to_prose as _conv
        return _conv(body)
    except Exception:  # noqa: BLE001 — 폴백: 태그 제거 + 엔티티 언이스케이프
        import html as _html
        import re as _re
        return _html.unescape(_re.sub(r"<[^>]+>", " ", body))


def _ai_tell_score(draft: Optional[dict]) -> float:
    """보관본 본문의 AI 티 점수(0~100). 실패는 -1.0(최저 우선순위)로 fail-open.

    본문은 post_data.content(HTML) 우선, 없으면 상위 content. 평문 근사 후
    score_ai_tell 로 채점한다. 어떤 실패(본문 없음·임포트·예외·스코어러 error)든
    -1.0 을 돌려 정렬에서 최저 우선순위로 밀되 배치는 계속된다.
    """
    try:
        if not isinstance(draft, dict):
            return -1.0
        post = draft.get("post_data")
        body = post.get("content") if isinstance(post, dict) else None
        if not isinstance(body, str) or not body.strip():
            body = draft.get("content")
        if not isinstance(body, str) or not body.strip():
            return -1.0
        prose = _html_to_prose(body)
        if not prose.strip():
            return -1.0
        from core.ai_tell_scorer import score_ai_tell
        result = score_ai_tell(prose)
        if not isinstance(result, dict) or result.get("error"):
            return -1.0
        return float(result.get("score", 0.0))
    except Exception as exc:  # noqa: BLE001 — 스코어 실패는 최저 우선순위(배치 지속)
        log.debug("[draft_restyle] AI 티 스코어 실패(최저 우선순위): %s", exc)
        return -1.0


def _log_ai_tell_order(rescored: list) -> None:
    """AI 티 우선 정렬 ON 일 때 요약 한 줄 + 상위 3편 점수를 로그로 남긴다.

    Args:
        rescored: (score, demand, slug) 튜플 리스트(이미 AI 티 내림차순 정렬됨).
    """
    scored_ok = sum(1 for r in rescored if r and r[0] >= 0)
    top3 = rescored[:3]
    top_desc = ", ".join(f"{slug}({score:.0f})" for score, _dem, slug in top3) or "(없음)"
    log.info("[draft_restyle] AI 티 최악 상위 %d편 우선 리스타일 - 상위 3: %s",
             scored_ok, top_desc)


def _generate(title: str) -> dict:
    """블루프린트 ON 본문 재생성 — content_generator 를 모듈 속성으로 호출(테스트 목킹 가능)."""
    from core import content_generator as _cg
    return _cg.generate_blog_post(title)


# ─────────────────────────────────────────────────────
# 단건 리스타일
# ─────────────────────────────────────────────────────
def restyle_draft(slug: str, *, dry_run: bool = False, force: bool = False) -> dict:
    """draft_ready 보관본 1편을 블루프린트로 제자리 재생성.

    Returns:
        {ok, slug, old_title, new_title, before_aeo, after_aeo, skipped_reason}
        ok=False 면 저장하지 않았다(skipped_reason 참조). 옛 원본은 항상 안전하다.
    """
    out = {"ok": False, "slug": slug, "old_title": None, "new_title": None,
           "before_aeo": None, "after_aeo": None, "skipped_reason": None,
           "fail_count": 0, "hard_skipped": False}

    if not _blueprint_on() and not force:
        log.warning("[draft_restyle] BLUEPRINT_ENABLED OFF — 리스타일 무의미(abort). "
                    "force=True 로 강제할 수 있습니다.")
        out["skipped_reason"] = "blueprint_off"
        return out

    post = draft_store._read_json(_post_path(slug))
    status = draft_store._read_json(_status_path(slug))
    if not isinstance(post, dict) or not isinstance(status, dict):
        out["skipped_reason"] = "missing"
        return out
    if status.get("status") != "draft_ready":
        out["skipped_reason"] = f"not_draft_ready:{status.get('status')}"
        return out
    if not post.get("content") or not post.get("title"):
        out["skipped_reason"] = "empty_original"
        return out

    old_title = post.get("title")
    category = post.get("category")
    image_paths = list(post.get("image_paths") or [])
    schedule_at = status.get("schedule_at")
    out["old_title"] = old_title
    out["before_aeo"] = _aeo(post.get("content", ""))

    if dry_run:
        out["ok"] = True
        out["skipped_reason"] = "dry_run"
        return out

    def _fail(reason: str) -> dict:
        """실패 경로 — 백업/저장을 만들지 않아 다음 배치에서 자동 재시도된다.
        연속 실패 카운트를 올리고, 상한 도달 시 hard_skipped 로 표시(옛 원본은 안전)."""
        n = _record_fail(slug)
        cap = _fail_cap()
        out["skipped_reason"] = reason
        out["fail_count"] = n
        out["hard_skipped"] = n >= cap
        if n >= cap:
            log.warning("[draft_restyle] %s 연속 실패 %d회(상한 %d) — hard_skipped 영구 제외"
                        " (옛 원본 안전). --reset-fails 로 재시도 가능.", slug, n, cap)
        else:
            log.info("[draft_restyle] %s 실패(%s) — 백업 안 만듦, 다음 배치 재시도 예정 (%d/%d)",
                     slug, reason, n, cap)
        return out

    # ── 재생성 먼저(블루프린트 ON) — 성공했을 때만 백업/저장한다 ──────
    try:
        new = _generate(old_title)
    except Exception as exc:  # noqa: BLE001 — 생성 실패는 옛 원본 유지·마커 안 남김·재시도
        log.warning("[draft_restyle] 재생성 예외 — 옛 원본 유지(재시도): %s", exc)
        return _fail("generate_error")

    if not isinstance(new, dict) or new.get("_failed") or new.get("_stopped") \
            or not new.get("content"):
        # QA/fact_gate/인증만료/쿼터소진 등 — 발행 차단 본문으로 덮지 않고 옛 원본 유지.
        reason = "generate_stopped" if (isinstance(new, dict) and new.get("_stopped")) \
            else (new.get("_failed_reason") if isinstance(new, dict) else "no_content")
        return _fail(f"regen_failed:{reason or 'unknown'}")

    # ── 성공: 백업(옛 원본 1회만) → 저장. min_gain 게이트 없음(스타일 교체가 목표) ──────
    try:
        if not _backup_path(slug).exists():
            shutil.copy2(_post_path(slug), _backup_path(slug))
    except OSError as exc:
        # 백업 실패면 새 본문으로 덮지 않는다(되돌리기 불가 방지) → 실패로 처리·재시도.
        log.warning("[draft_restyle] 백업 실패 — 저장 보류(원본 보존·재시도): %s", exc)
        return _fail("backup_failed")

    # category 는 옛 값 보존(생성 결과가 detect_category 로 흔들려도 유지).
    new["category"] = category if category is not None else new.get("category")
    draft_store.save_draft(slug, new, image_paths=image_paths,
                           schedule_at=schedule_at, status="draft_ready")
    _clear_fail(slug)                                    # 성공 → 실패 카운트 리셋
    out["ok"] = True
    out["new_title"] = new.get("title")
    out["after_aeo"] = _aeo(new.get("content", ""))
    log.info("[draft_restyle] %s 리스타일 완료: '%s' → '%s' (AEO %s→%s)",
             slug, str(old_title)[:24], str(out['new_title'])[:24],
             out["before_aeo"], out["after_aeo"])
    return out


# ─────────────────────────────────────────────────────
# 배치
# ─────────────────────────────────────────────────────
def run_restyle_batch(max_count: int = 10, *, min_demand: int = 0,
                      resume: bool = True, force: bool = False) -> dict:
    """전체 draft_ready 를 수요 우선으로 상위 max_count 편 리스타일.

    정렬은 기본 수요(_demand) 우선이나, settings.RESTYLE_ORDER_BY_AI_TELL ON(옵트인)
    이면 AI 티 점수 최악 보관본부터(내림차순, 동점은 수요→슬러그) 처리한다. OFF(기본)
    이면 스코어링을 아예 안 해 기존 정렬과 바이트 동일(매일 도는 배치 무영향).

    Args:
        max_count: 실제로 재생성 시도할 최대 편수(각 생성=수 분). resume 스킵분은 포함 안 함.
        min_demand: 이 수요 미만 보관본은 대상에서 제외.
        resume: True 면 이미 백업(처리됨) 있는 슬러그는 건너뛴다(세션 간 재개).
        force: BLUEPRINT_ENABLED OFF 여도 강제 실행.

    Returns:
        {targets, restyled, retryable_failed, hard_skipped, skipped_done,
         results, aborted?}
          · restyled         — 이번에 성공 교체
          · retryable_failed — 이번에 실패했지만 다음 배치에서 재시도됨(마커 없음)
          · hard_skipped     — 연속 실패 상한 도달로 이번에 건너뜀 + 이번에 상한 도달
          · skipped_done     — 이미 성공(백업 존재)이라 건너뜀
    """
    if not _blueprint_on() and not force:
        log.warning("[draft_restyle] BLUEPRINT_ENABLED OFF — 배치 abort(force 로 강제 가능)")
        return {"targets": 0, "restyled": 0, "retryable_failed": 0,
                "hard_skipped": 0, "skipped_done": 0, "results": [],
                "aborted": "blueprint_off"}

    drafts = draft_store.load_drafts("draft_ready")
    # 수요 우선 정렬(제목 기반 결정론). 동수는 슬러그로 안정 정렬.
    scored = []
    for d in drafts:
        title = d.get("title") or (d.get("post_data") or {}).get("title") or ""
        dem = _demand(title)
        if dem < min_demand:
            continue
        scored.append((dem, d.get("slug")))

    if _order_by_ai_tell():
        # 옵트인(Phase D): AI 티 최악 보관본부터 리스타일. 각 보관본 본문을 채점해
        # 점수 내림차순(최악 먼저) → 동점 수요 내림차순 → 슬러그 오름차순으로 정렬한다.
        # ★ 효율: resume 로 어차피 스킵될 항목(has_backup=완료 / is_hard_skipped=영구제외)
        #   은 채점하지 않고 최저 우선순위 sentinel(-1.0)로 밑에 깐다 — 잔여가 몇 편뿐이어도
        #   나머지 수백~수천 편을 매 배치 헛채점하는 지연을 없앤다. 스킵 항목은 scored 에
        #   그대로 남겨 아래 처리 루프의 skipped_done/hard_skipped 카운팅은 훼손하지 않는다.
        #   스코어 실패 draft 도 _ai_tell_score 가 -1.0 을 반환해 최저 우선순위로 밀리고
        #   배치는 계속된다(fail-open). ★ OFF 면 이 블록을 통째로 건너뛰므로 스코어링
        #   비용 0 + 아래 else 의 기존 수요 정렬과 바이트 동일.
        by_slug = {d.get("slug"): d for d in drafts}

        def _order_score(slug: Optional[str]) -> float:
            # resume 로 스킵될(백업 존재/영구 제외) 슬러그는 채점 자체를 건너뛴다.
            if resume and slug and (has_backup(slug) or is_hard_skipped(slug)):
                return -1.0
            return _ai_tell_score(by_slug.get(slug))

        rescored = [(_order_score(slug), dem, slug) for dem, slug in scored]
        rescored.sort(key=lambda x: (-x[0], -x[1], x[2] or ""))
        scored = [(dem, slug) for _score, dem, slug in rescored]
        _log_ai_tell_order(rescored)
    else:
        scored.sort(key=lambda x: (-x[0], x[1] or ""))

    restyled = retryable_failed = hard_skipped = skipped_done = 0
    results: list = []
    processed = 0
    for dem, slug in scored:
        if processed >= max_count:
            break
        if resume and has_backup(slug):        # 이미 성공 → skip
            skipped_done += 1
            continue
        if resume and is_hard_skipped(slug):    # 연속 실패 상한 도달 → skip(시도 안 함)
            hard_skipped += 1
            continue
        processed += 1
        log.info("[draft_restyle] (%d/%d) 리스타일: %s (수요 %d)",
                 processed, max_count, slug, dem)
        res = restyle_draft(slug, force=force)
        results.append(res)
        if res.get("ok"):
            restyled += 1
        elif res.get("hard_skipped"):           # 이번 실패로 상한 도달
            hard_skipped += 1
        else:
            retryable_failed += 1

    return {"targets": len(scored), "restyled": restyled,
            "retryable_failed": retryable_failed, "hard_skipped": hard_skipped,
            "skipped_done": skipped_done, "results": results}


def restyle_status() -> dict:
    """진단: 조용한 유실이 없게 무엇이 왜 빠졌는지 드러낸다.

    Returns:
        {total_draft_ready, restyled, hard_skipped, pending_retry, remaining}
          · restyled      — 성공(백업 존재)
          · hard_skipped  — 연속 실패 상한 도달로 영구 제외(백업 없음)
          · pending_retry — 실패했으나 상한 미만(다음 배치에서 재시도됨)
          · remaining     — 아직 성공 못 함(= total − restyled − hard_skipped)
    """
    drafts = draft_store.load_drafts("draft_ready")
    total = len(drafts)
    cap = _fail_cap()
    fails = _load_fails()
    restyled = hard_skipped = pending_retry = 0
    for d in drafts:
        slug = d.get("slug")
        if has_backup(slug):
            restyled += 1
            continue
        cnt = int(fails.get(slug, 0) or 0) if isinstance(fails, dict) else 0
        if cnt >= cap:
            hard_skipped += 1
        elif cnt > 0:
            pending_retry += 1
    return {"total_draft_ready": total, "restyled": restyled,
            "hard_skipped": hard_skipped, "pending_retry": pending_retry,
            "remaining": max(0, total - restyled - hard_skipped)}


def restore_restyle(slug: Optional[str] = None, *, limit: Optional[int] = None) -> dict:
    """post_data.prestyle.json 백업으로 post_data.json 을 원복(되돌리기).

    Args:
        slug: 지정 시 그 1편만. None 이면 백업 있는 전체(limit 까지).
        limit: 전체 원복 시 상한.

    Returns:
        {restored: [slug...], count, failed: [slug...]}
    """
    restored: list = []
    failed: list = []

    if slug is not None:
        targets = [slug]
    else:
        try:
            targets = [c.name for c in draft_store._RUNS_DIR.iterdir()
                       if c.is_dir() and (c / _PRESTYLE_FILE).exists()]
        except OSError:
            targets = []
        targets.sort()
        if limit is not None:
            targets = targets[:limit]

    for sl in targets:
        backup = _backup_path(sl)
        if not backup.exists():
            failed.append(sl)
            continue
        data = draft_store._read_json(backup)
        if not isinstance(data, dict):
            failed.append(sl)
            continue
        try:
            draft_store._atomic_write_json(_post_path(sl), data)
            backup.unlink()                     # 원복 완료 → 마커 제거(재실행 가능)
            restored.append(sl)
        except OSError as exc:
            log.warning("[draft_restyle] 원복 실패 %s: %s", sl, exc)
            failed.append(sl)

    if restored:
        draft_store._invalidate_cache()         # 직접 쓴 post_data.json 캐시 무효화
    return {"restored": restored, "count": len(restored), "failed": failed}


def reset_fails(slug: Optional[str] = None) -> dict:
    """연속 실패 카운트 리셋 — hard_skipped 슬러그를 다음 배치에서 재시도하게 한다.

    Args:
        slug: 지정 시 그 1편만, None 이면 전체.

    Returns:
        {reset: [slug...], count}
    """
    data = _load_fails()
    if slug is not None:
        removed = [slug] if slug in data else []
        for s in removed:
            del data[s]
    else:
        removed = list(data.keys())
        data = {}
    if removed:
        _save_fails(data)
    return {"reset": removed, "count": len(removed)}


# ─────────────────────────────────────────────────────
# 매일 자동 리스타일 — Windows 작업 스케줄러 등록(run_ops/rank_monitor 패턴 미러)
# ─────────────────────────────────────────────────────
def _pythonw() -> str:
    p = Path(sys.executable).parent / "pythonw.exe"
    return str(p) if p.exists() else sys.executable


def register_restyle_task(daily_count: int = 12, *, at: str = "03:30") -> tuple:
    """매일 지정 시각에 보관본 리스타일 배치를 무인 실행하도록 등록. 코드만 — 호출은 사용자 몫.

    실행 명령 = `python core/draft_restyle.py --run {daily_count} --profile {active}`.
    daily_count 는 발행에도 쓰는 Max 쿼터를 굶기지 않게 보수적(기본 12). BLUEPRINT_ENABLED
    OFF 면 배치가 자체 abort 하므로 무해. 완료분은 resume 으로 다음 날 자동 skip.

    Returns:
        (성공 bool, 메시지 str).
    """
    try:  # 러너 문자열 주입 방지 + GUI 잘못된 입력에 크래시 대신 거부.
        n = int(daily_count)
    except (TypeError, ValueError):
        return False, f"daily_count 는 정수여야 합니다: {daily_count!r}"
    if n < 1:
        return False, f"daily_count 는 1 이상이어야 합니다: {n}"
    runner = (f'"{_pythonw()}" "{_BASE_DIR / "core" / "draft_restyle.py"}" '
              f'--run {n}')
    try:  # 등록 시점 프로필 고정(격리 감사) — 발화 시점 .active 표류 방지
        runner += f' --profile {_nbaprofile.active_profile()}'
    except Exception:  # noqa: BLE001
        pass
    cmd = ["schtasks", "/create", "/tn", _TASK_NAME, "/tr", runner,
           "/sc", "daily", "/st", at, "/f", "/rl", "LIMITED"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding=CONSOLE_ENCODING, errors="replace", timeout=30,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return (r.returncode == 0), (f"등록: 매일 {at} (하루 {n}편)"
                                     if r.returncode == 0
                                     else f"실패: {r.stderr.strip()}")
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, f"오류: {e}"


def check_restyle_task() -> tuple:
    """리스타일 작업 등록 여부. (registered: bool, message: str) — GUI 토글용."""
    try:
        r = subprocess.run(["schtasks", "/query", "/tn", _TASK_NAME],
                           capture_output=True, text=True,
                           encoding=CONSOLE_ENCODING, errors="replace", timeout=30,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return (r.returncode == 0), ("등록됨" if r.returncode == 0 else "미등록")
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, f"오류: {e}"


def unregister_restyle_task() -> tuple:
    try:
        r = subprocess.run(["schtasks", "/delete", "/tn", _TASK_NAME, "/f"],
                           capture_output=True, text=True,
                           encoding=CONSOLE_ENCODING, errors="replace", timeout=30,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return (r.returncode == 0), ("해제 완료" if r.returncode == 0 else "미등록")
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, f"오류: {e}"


# ─────────────────────────────────────────────────────
if __name__ == "__main__":
    # 프로필은 무거운 임포트 전(파일 상단)에서 이미 고정됨.
    # cp949 콘솔에서 결과 JSON·로그의 em대시/화살표(→) 출력이 UnicodeEncodeError 로
    # exit 1(가짜 실패)을 내던 것을 방지 — stdout/stderr 를 UTF-8·errors=replace 로 고정.
    # 로깅 핸들러가 참조하는 스트림 객체를 제자리 재설정하므로 로그 노이즈도 함께 사라진다.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    import json as _json
    args = sys.argv[1:]
    if "--reset-fails" in args:
        i = args.index("--reset-fails")
        nxt = args[i + 1] if i + 1 < len(args) else None
        target = nxt if (nxt and not nxt.startswith("--")) else None
        print(_json.dumps(reset_fails(target), ensure_ascii=False, indent=2))
    elif "--restore" in args:
        print(_json.dumps(restore_restyle(), ensure_ascii=False, indent=2))
    elif "--run" in args:
        i = args.index("--run")
        n = int(args[i + 1]) if i + 1 < len(args) else 10
        print(_json.dumps(run_restyle_batch(max_count=n), ensure_ascii=False, indent=2))
    else:
        print(_json.dumps(restyle_status(), ensure_ascii=False, indent=2))
