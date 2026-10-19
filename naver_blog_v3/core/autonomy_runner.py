"""autonomy_runner — 계정 하나의 **무인 운영 한 사이클** (2026-09-18).

작업 스케줄러(또는 GUI, 또는 `autonomy_cli.py`)가 예약 준비 시각마다 이 함수를 부른다.
한 사이클은 이렇게 돈다.

    정지 스위치 확인 → 계정 정책 로드 → **중단분 원격 대조** → 오늘 계획
      → 슬롯마다: 예산 확보 → 점유(lease) → 브라우저 중재 → 기존 파이프라인 1편 실행
      → 결과 상태 기록 → 재고 점검

설계상 **글감 선별·집필·이미지·QA·예약 등록은 전부 기존 파이프라인**
(`scheduler.auto_scheduler.run_pipeline`)이 한다. 이 모듈은 그 바깥 껍데기 —
"언제, 몇 편, 어떤 범위로, 멈춰도 되는지, 어디까지 갔는지"만 책임진다. 파이프라인을
다시 구현하지 않는 것이 요점이다(이미지 준비·발행 전 이미지 검증·배치 락 전부 보존).

**절대 하지 않는 것**
- 결말이 미상인 글의 자동 재발행 (`reserving`/`unconfirmed`/`missing`)
- 증거 없는 `reserved` 기록
- 보관본 1,000편대 일괄 이미지 생성 (한 사이클에 슬롯 수만큼만)
"""
from __future__ import annotations

from datetime import datetime, date
from typing import Callable, Optional

from config.logger import get_logger

log = get_logger(__name__)


def _now() -> datetime:
    """현지 **naive** 현재 시각.

    슬롯 계산(`autonomy_policy.slots_for_day`)이 naive 현지시각을 만들므로 여기도
    naive 로 맞춘다. 둘을 섞으면 비교에서 TypeError 가 나고, 그 예외가 조용히
    "슬롯 건너뜀"으로 둔갑한다(무인 운영이 아무 것도 안 하는 사고).
    """
    return datetime.now()


def _as_naive(dt: datetime) -> datetime:
    """aware/naive 가 섞여 들어와도 비교할 수 있게 현지 naive 로 정규화."""
    if dt.tzinfo is not None:
        return dt.astimezone().replace(tzinfo=None)
    return dt


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _active_profile() -> str:
    try:
        from config import profile as _pf
        return _pf.active_profile()
    except Exception:  # noqa: BLE001
        return ""


def _resolve_profile(requested: Optional[str]) -> tuple:
    """(사용할 계정, 거절 사유). 요청 계정이 이 프로세스의 활성 계정과 다르면 거절한다.

    정책·정지 스위치·비용 원장은 `profile` 인자를 받지만 **작업큐는 활성 프로필
    경로에 묶여 있다**(`config.profile.logs_dir()`). 그래서 한 프로세스에서 다른
    계정을 지정해 돌리면 정책은 B 것을 읽고 큐는 A 에 쓰는 **반쪽 상태**가 된다.
    조용히 섞느니 거절하고 "별도 실행으로 돌리라"고 말하는 편이 안전하다
    (이 저장소의 '한 프로세스 = 한 프로필' 규약과도 같다).
    """
    active = _active_profile()
    if requested and active and requested != active:
        return active, (
            f"이 프로세스는 '{active}' 계정으로 고정돼 있어 '{requested}' 계정을 "
            "대신 처리할 수 없습니다. 그 계정은 별도 실행으로 돌려 주세요 "
            "(autonomy_cli.py --profile <계정>).")
    return (requested or active), ""


def _check_budget_exhausted(profile: str) -> Optional[dict]:
    """예산 잔량 사전 점검(2026-09-18 마감 결함 B) — 예약이 아니라 **조회**다.

    `_process_slot` 은 항상 `autonomy_guard.session()` 안에서만 불리므로
    (`run_cycle` 이 계획~처리 구간 전체를 세션으로 감싼다), 원장을 읽지 못하는
    것은 "설정이 꺼져 있다"가 아니라 "안전장치가 고장났다"는 뜻이다 — 조용히
    계속 가지 않고 사이클을 멈춘다(fail-closed).

    Returns:
        여유가 있으면 None. 막혀야 하면 `{"reason": str, "stop_cycle": True}`.
    """
    try:
        from core import cost_ledger as _cl
        write_left = _cl.remaining("write", profile=profile)
        image_left = _cl.remaining("image", profile=profile)
    except Exception as exc:  # noqa: BLE001
        log.warning(f"[autonomy] 비용 원장 조회 실패 — 안전을 위해 중단({type(exc).__name__})")
        return {"reason": f"비용 원장을 읽지 못해 안전을 위해 중단합니다({type(exc).__name__})",
                "stop_cycle": True}

    def _exhausted(remaining_map: dict) -> bool:
        for key in ("profile_left", "global_left"):
            value = remaining_map.get(key)
            if value is not None and value <= 0:
                return True
        return False

    if _exhausted(write_left) or _exhausted(image_left):
        return {"reason": "오늘 호출 상한에 도달해 더 만들지 않습니다", "stop_cycle": True}
    return None


def blog_id_for_stats() -> str:
    """공개 API 조회용 블로그 ID(마스킹되지 않은 값만) — GUI '지금 다시 확인'
    버튼 등 무인 운영 밖에서도 같은 판정을 재사용하도록 공개한다."""
    return _blog_id()


def _blog_id() -> str:
    """공개 목록 조회용 블로그 ID. 자격증명 **값**은 읽지 않는다(ID 만)."""
    try:
        from config import profile as _pf
        desc = _pf.describe()
        for key in ("naver_id", "blog_id"):
            value = str(desc.get(key) or "").strip()
            if value and "*" not in value:
                return value
    except Exception:  # noqa: BLE001
        pass
    try:
        from config import settings as _s
        return str(getattr(_s, "NAVER_ID", "") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


# ─────────────────────────────────────────────────────
# 1) 중단분 원격 대조
# ─────────────────────────────────────────────────────

def reconcile_open_items(*, blog_id: str = "", post_list_fn: Optional[Callable] = None,
                         now: Optional[datetime] = None,
                         reconcile_fn: Optional[Callable] = None) -> list:
    """`reserving`/`unconfirmed`/`verifying` 로 남은 항목의 결말을 **시도**한다.

    결말이 안 나면 그대로 둔다. **어떤 항목도 다시 발행하지 않는다.**
    반환: [{"item_id","before","after","reason","log_no"}].
    """
    out: list = []
    try:
        from core import work_queue as _wq
    except Exception as exc:  # noqa: BLE001 — 큐 없으면 대조할 것도 없다
        log.debug(f"[autonomy] work_queue 없음 — 대조 생략: {type(exc).__name__}")
        return out

    try:
        items = _wq.stuck_items(now=now)
    except Exception as exc:  # noqa: BLE001
        log.warning(f"[autonomy] 중단분 조회 실패: {type(exc).__name__}")
        return out
    if not items:
        return out

    if reconcile_fn is None:
        try:
            from core.publish_reconciler import reconcile as reconcile_fn  # type: ignore
        except Exception as exc:  # noqa: BLE001
            log.warning(f"[autonomy] 대조기 로드 실패 — 상태 유지: {type(exc).__name__}")
            return out

    resolved_list = None
    if post_list_fn is None:
        # 목록 조회는 한 사이클에 1회만 하고 모든 항목이 재사용한다.
        try:
            from core.stats_fetcher import NaverBlogStats
            _stats = NaverBlogStats(blog_id or _blog_id())
            resolved_list = _stats.get_post_list(count=50)
        except Exception as exc:  # noqa: BLE001 — 조회 실패는 판정 보류
            log.info(f"[autonomy] 공개 목록 조회 실패 — 판정 보류({type(exc).__name__})")
            resolved_list = None
        post_list_fn = (lambda: list(resolved_list)) if resolved_list is not None else None

    for item in items:
        before = item.get("publish_state") or "none"
        try:
            verdict = reconcile_fn(item, blog_id=blog_id or _blog_id(),
                                   post_list_fn=post_list_fn, now=now)
        except Exception as exc:  # noqa: BLE001 — 판정 실패는 상태 유지
            log.warning(f"[autonomy] 대조 예외(상태 유지): {type(exc).__name__}")
            continue
        after = (verdict or {}).get("publish_state") or before
        # ⚠️ 대조는 reserved 를 만들 수 없다(공개 목록은 예약 글을 보여주지 않는다).
        if after == "reserved":
            log.warning("[autonomy] 대조가 reserved 를 반환 — 무시하고 상태 유지")
            after = before
        if after != before:
            try:
                _wq.set_state(item.get("item_id", ""), after,
                              log_no=(verdict or {}).get("log_no"),
                              url=(verdict or {}).get("url"),
                              evidence=(verdict or {}).get("evidence"),
                              reason="reconcile")
            except Exception as exc:  # noqa: BLE001
                log.warning(f"[autonomy] 대조 결과 기록 실패: {type(exc).__name__}")
            slug = item.get("slug") or ""
            if slug:
                try:
                    from core import draft_store as _ds
                    _ds.set_publish_state(slug, after,
                                          log_no=(verdict or {}).get("log_no"),
                                          url=(verdict or {}).get("url"))
                except Exception:  # noqa: BLE001
                    pass
        out.append({
            "item_id": item.get("item_id", ""), "title": item.get("title", ""),
            "before": before, "after": after,
            "log_no": (verdict or {}).get("log_no") or "",
            "reason": ((verdict or {}).get("evidence") or {}).get("match", ""),
        })
    return out


# ─────────────────────────────────────────────────────
# 2) 재고 점검
# ─────────────────────────────────────────────────────

def inventory_status(*, policy: Optional[dict] = None) -> dict:
    """완성 재고(= 이미지까지 갖춘 발행 가능 보관본) 현황.

    "오늘 나갈 글 없음"을 **조용히 넘기지 않기 위한** 신호다. 이미지가 없는 보관본은
    발행 직전 게이트에서 막히므로 재고로 세지 않는다.
    """
    result = {"ready_with_images": 0, "ready_total": 0, "days_left": 0.0,
              "target_days": 0, "warning": "",
              "partial_images": 0, "no_plan": 0, "judged_by": ""}
    try:
        from core import draft_store as _ds
        from core import draft_image_fill as _dif
        drafts = _ds.load_drafts(status_filter="draft_ready")
    except Exception as exc:  # noqa: BLE001
        result["warning"] = f"재고 조회 실패({type(exc).__name__})"
        return result

    # ★ 2026-09-18 수리: 예전에는 "적혀 있는 image_paths 가 전부 실재하면 완성"으로
    #   셌다. 계획 슬롯이 10개인데 경로가 4개만 적힌 글도 '완성 재고'가 돼, 발행
    #   직전 게이트에서 막힐 글을 "오늘 나갈 수 있는 글"로 보고했다. 이제 발행
    #   게이트와 **같은 판정**(draft_image_fill.images_complete)을 쓰고, 이미지가
    #   일부만 있는 글은 `partial_images` 로 **따로** 센다.
    ready = 0
    partial = 0
    no_plan = 0
    for entry in drafts:
        post = entry.get("post_data") or {}
        try:
            complete, detail = _dif.images_complete(post)
        except Exception:  # noqa: BLE001 — 판정 실패는 미완으로 본다
            complete, detail = False, {"existing": 0, "reason": "judge_error"}
        if complete:
            ready += 1
            continue
        if int(detail.get("existing") or 0) > 0:
            partial += 1
        if detail.get("reason") == "no_plan":
            no_plan += 1
    result["ready_total"] = len(drafts)
    result["ready_with_images"] = ready
    result["partial_images"] = partial
    result["no_plan"] = no_plan
    result["judged_by"] = "draft_image_fill.images_complete"

    policy = policy or {}
    per_day = max(1, int(policy.get("daily_posts") or 1))
    target_days = int(policy.get("inventory_days") or 0)
    result["target_days"] = target_days
    result["days_left"] = round(ready / per_day, 1)
    if ready == 0:
        extra = (f" 이미지가 일부만 있는 글은 {partial}편입니다 — 빠진 자리를 채우면 "
                 f"재고가 됩니다." if partial else "")
        result["warning"] = (
            "완성 재고가 0편입니다 — 오늘 나갈 글이 없습니다. "
            "보관함 채우기로 이미지까지 갖춘 글을 먼저 만들어 주세요." + extra)
    elif target_days and result["days_left"] < target_days:
        result["warning"] = (
            f"완성 재고 {ready}편(약 {result['days_left']}일치)으로 "
            f"목표 {target_days}일치보다 적습니다.")
    return result


# ─────────────────────────────────────────────────────
# 2-1) 지정 1편 계약 (파일럿 — "어느 글인가"까지 못박는 제한 경로)
# ─────────────────────────────────────────────────────
#
# 배경: 정책의 `daily_posts=1` 은 *몇 편인가*만 정한다. `autonomy_cli --run` 은
# 그날의 후보 중 하나를 고를 뿐 특정 슬러그에 묶이지 않는다. 파일럿처럼 "이 글
# 한 편만" 준비·검증하려면 후보 자체를 지정해야 한다 — 그 계약이 여기 있다.

#: 지정 1편 경로가 스스로 쓰는 상한(정책 상한과 별개로 **항상** 1편).
TARGET_MAX_POSTS: int = 1

#: 최신성 근거를 사람 검토 대신 댈 수 있는 기록 파일(안내 문구용).
#: 판정 자체는 `core.source_verification.verify` 가 한다.
_SV_RECORD_HINT: str = "runs/{slug}/source_verification.json"


def _target_draft(slug: str):
    """슬러그로 보관본 하나를 찾는다. 없으면 None."""
    try:
        from core import draft_store as _ds
        for entry in _ds.load_drafts(status_filter=None) or []:
            if isinstance(entry, dict) and entry.get("slug") == slug:
                return entry
    except Exception as exc:  # noqa: BLE001
        log.warning(f"[autonomy] 보관본 조회 실패({type(exc).__name__})")
    return None


def target_preview(slug: str, *, profile: Optional[str] = None,
                   freshness_reviewed: bool = False) -> dict:
    """지정 1편 실행의 **미리보기**(외부 행동 0회 — 아무것도 제출·생성하지 않는다).

    계정·슬러그·최대 편수·외부 확정 차단 여부·이미지/집필 호출 상한을 한 번에
    보여 준다. `run_target` 이 실제로 무엇을 할지(`route`)도 같은 규칙으로 계산한다.
    """
    profile, mismatch = _resolve_profile(profile)
    out = {
        "ok": False, "profile": profile, "slug": str(slug or ""), "title": "",
        "max_posts": TARGET_MAX_POSTS, "scope": "", "route": "none",
        "external_confirm_blocked": True, "publish_allowed": False,
        "caps": {}, "images": {}, "blockers": [], "notes": [],
        "freshness_reviewed": bool(freshness_reviewed),
    }
    if mismatch:
        out["blockers"].append({"code": "profile_mismatch", "detail": mismatch})
        return out

    entry = _target_draft(out["slug"])
    if entry is None:
        out["blockers"].append({
            "code": "slug_not_found",
            "detail": "그 슬러그의 보관본을 찾지 못했습니다.",
            "fix": "python -m core.blog_service list 로 슬러그를 확인하세요."})
        return out
    post = entry.get("post_data") or {}
    out["title"] = str(post.get("title") or "")
    out["draft_status"] = str(entry.get("status") or "")
    # 파이프라인에 넘길 후보 식별자 — 최종 제목이 아니라 슬러그다(둘은 다를 수 있다).
    out["pipeline_selector"] = out["slug"]

    # 이미지 완전성 — 발행 게이트와 같은 판정
    try:
        from core import draft_image_fill as _dif
        complete, detail = _dif.images_complete(post)
        out["images"] = {"complete": bool(complete), **detail}
    except Exception as exc:  # noqa: BLE001
        out["images"] = {"complete": False, "reason": f"judge_error:{type(exc).__name__}"}
        complete = False

    # 정책·상한
    try:
        from core import autonomy_policy as _pol
        policy = _pol.load_policy(profile)
        out["scope"] = str(policy.get("scope") or "draft_only")
        budget = policy.get("budget") or {}
        out["caps"] = {"write_calls": budget.get("write_calls"),
                       "image_calls": budget.get("image_calls")}
        out["enabled"] = bool(_pol.is_active(policy, profile=profile))
    except Exception as exc:  # noqa: BLE001
        out["blockers"].append({"code": "policy_error",
                                "detail": f"정책을 읽지 못했습니다({type(exc).__name__})"})
        return out
    try:
        from core import cost_ledger as _cl
        out["caps"]["write_left"] = _cl.remaining("write", profile=profile)
        out["caps"]["image_left"] = _cl.remaining("image", profile=profile)
    except Exception as exc:  # noqa: BLE001
        out["caps"]["ledger_error"] = type(exc).__name__

    # 준비 상태(필수 보호 포함)
    try:
        from core import autonomy_guard as _guard
        # probe=False — 미리보기는 **부수효과 0**이어야 한다(원장 왕복 예약 금지).
        ready = _guard.readiness(profile=profile, probe=False)
        out["readiness"] = ready
        if not ready.get("ok"):
            out["blockers"].extend(ready.get("blockers") or [])
    except Exception as exc:  # noqa: BLE001
        out["blockers"].append({"code": "guard_missing",
                                "detail": f"안전장치 모듈을 불러오지 못했습니다({type(exc).__name__})"})

    # ★ 콘텐츠 최신성 — 지정 글은 오래전에 쓴 제도 안내일 수 있다. 사람이 내용의
    #   최신성을 확인하기 전에는 **실제 예약을 하지 않는다**(보관·이미지 준비는 가능).
    #   근거는 둘 중 하나면 된다 — ⓐ 사람이 확인했다고 명시하거나(기존 경로),
    #   ⓑ 그 글에 **공식 출처 검증 기록**이 붙어 있고 지금도 유효하거나.
    #   ⓑ 는 전역 스위치가 아니라 **이 슬러그 한 편**에만 걸리고, 본문·이미지가
    #   바뀌거나 시효가 지나면 스스로 닫힌다(`core.source_verification` 참조).
    if freshness_reviewed:
        out["freshness_basis"] = "human_review"
        freshness_ok = True
    else:
        try:
            from core import source_verification as _sv
            src = _sv.verify(out["slug"], post)
        except Exception as exc:  # noqa: BLE001 — 판정 실패는 '통과'가 아니다
            src = {"ok": False, "reason": f"verify_error:{type(exc).__name__}",
                   "detail": {}}
        out["source_verification"] = src
        freshness_ok = bool(src.get("ok"))
        out["freshness_basis"] = (
            "official_source_verification" if freshness_ok else "")
        if not freshness_ok:
            out["blockers"].append({
                "code": "content_freshness_unreviewed",
                "detail": "콘텐츠 최신성 검토 전에는 실제 예약을 하지 않습니다 "
                          "(오래된 제도·금액·기한이 그대로 나갈 수 있습니다). "
                          f"공식 출처 검증 기록: {src.get('reason') or 'no_record'}",
                "fix": "본문의 수치·기한·제도명을 사람이 확인한 뒤 "
                       "--freshness-reviewed 를 붙이거나, 공식 원문을 대조한 "
                       f"{_SV_RECORD_HINT} 기록을 그 글에 남기세요."})

    publish_scope = out["scope"] == "publish"
    out["publish_allowed"] = bool(publish_scope and freshness_ok)
    # 외부 확정(네이버 제출)은 범위가 publish 이고 최신성 검토가 끝났을 때만 열린다.
    out["external_confirm_blocked"] = not out["publish_allowed"]

    if not complete:
        out["route"] = "image_topup"
        out["notes"].append(
            "이미지가 미완이라 이번 실행은 '빠진 자리만 채우기'까지입니다 "
            "(발행하지 않습니다).")
    elif out["publish_allowed"]:
        out["route"] = "pipeline_publish"
    else:
        out["route"] = "hold"
        out["notes"].append(
            "이미지는 완성됐지만 범위·최신성 조건이 아직 열리지 않아 대기합니다.")

    out["ok"] = out["route"] != "none" and not any(
        b.get("code") in ("slug_not_found", "profile_mismatch", "guard_missing")
        for b in out["blockers"])
    # ★ `ok` 는 "지목을 해석했다"일 뿐이다. 실제로 돌려도 되는지는 준비 상태가
    #   정한다 — 마스터·계정이 꺼져 있으면 이미지 채움조차 하지 않는다(구독 소모).
    out["can_run"] = bool(
        out["ok"] and out["route"] in ("image_topup", "pipeline_publish")
        and (out.get("readiness") or {}).get("ok"))
    return out


def run_target(slug: str, *, profile: Optional[str] = None,
               freshness_reviewed: bool = False, dry_run: bool = False,
               fill_fn: Optional[Callable] = None,
               pipeline_fn: Optional[Callable] = None) -> dict:
    """지정한 **한 편만** 준비(또는 예약)한다. 미리보기와 같은 규칙으로 움직인다.

    · `route="image_topup"` — 그 글의 **빠진 이미지 자리만** 채운다(발행 없음).
    · `route="pipeline_publish"` — 범위가 `publish` 이고 최신성 검토가 끝났을 때만,
      후보를 그 제목 하나로 못박아 파이프라인을 **1편** 돌린다.
    · 그 밖에는 아무 외부 행동도 하지 않고 사유를 돌려준다.
    """
    preview = target_preview(slug, profile=profile,
                             freshness_reviewed=freshness_reviewed)
    result = {"preview": preview, "ran": "", "ok": False, "reason": "",
              "dry_run": bool(dry_run), "detail": {}}
    if not preview.get("can_run"):
        codes = [b.get("code") for b in (preview.get("blockers") or [])]
        result["reason"] = ("지금은 실행할 수 없습니다 — 막는 것: "
                            + (", ".join(c for c in codes if c) or "route_none"))
        return result

    profile = preview["profile"]
    try:
        from core import kill_switch as _ks
        if _ks.estop_active():
            result["reason"] = "전체 비상정지 상태입니다."
            return result
        if _ks.pause_active(profile=profile):
            result["reason"] = "이 계정이 일시중지 상태입니다."
            return result
    except Exception as exc:  # noqa: BLE001
        log.debug(f"[autonomy] 정지 스위치 확인 생략: {type(exc).__name__}")

    if dry_run:
        result.update(ok=True, ran="dry_run",
                      reason=f"실행하지 않았습니다 — 예정 경로: {preview['route']}")
        return result

    try:
        from core import autonomy_guard as _guard
    except Exception as exc:  # noqa: BLE001
        result["reason"] = f"안전장치 모듈을 불러오지 못했습니다({type(exc).__name__})"
        return result

    route = preview["route"]
    with _guard.session(profile):
        if route == "image_topup":
            fn = fill_fn
            if fn is None:
                from core.blog_service import fill_images as fn  # type: ignore
            result["detail"] = fn(slug=preview["slug"], topup=True) or {}
            result["ran"] = "image_topup"
            result["ok"] = not result["detail"].get("error")
        elif route == "pipeline_publish":
            fn2 = pipeline_fn
            if fn2 is None:
                from scheduler.auto_scheduler import run_pipeline as fn2  # type: ignore
            # 후보 지정은 **슬러그**로 한다 — 보관본의 발행 큐 제목은 슬러그 키라
            # 최종 제목과 다를 수 있다(파일럿 대상 글이 실제로 그렇다).
            result["detail"] = fn2(only_titles=[preview["pipeline_selector"]],
                                   max_count=TARGET_MAX_POSTS) or {}
            result["ran"] = "pipeline_publish"
            result["ok"] = bool(result["detail"].get("success"))
        else:
            result["reason"] = "지금 할 수 있는 일이 없습니다(대기)."
    return result


# ─────────────────────────────────────────────────────
# 3) 한 사이클
# ─────────────────────────────────────────────────────

def run_cycle(*, profile: Optional[str] = None, now: Optional[datetime] = None,
              dry_run: bool = False,
              pipeline_fn: Optional[Callable] = None,
              post_list_fn: Optional[Callable] = None,
              reconcile_fn: Optional[Callable] = None,
              blog_id: str = "",
              progress_cb: Optional[Callable] = None,
              prep_fn: Optional[Callable] = None) -> dict:
    """계정 하나의 무인 운영 한 사이클 + **현황 기록**.

    본체는 `_run_cycle_impl` 이고, 여기서는 어느 경로로 끝나든(꺼짐·정지·오류
    포함) 그 결과를 `autonomy_overview.record_cycle` 로 남긴다 — 다른 계정의
    화면이 읽을 값이 이것뿐이라, 실패한 사이클이 기록되지 않으면 전체 현황이
    "아무 일도 없었음"처럼 보인다.
    """
    result = _run_cycle_impl(
        profile=profile, now=now, dry_run=dry_run, pipeline_fn=pipeline_fn,
        post_list_fn=post_list_fn, reconcile_fn=reconcile_fn, blog_id=blog_id,
        progress_cb=progress_cb, prep_fn=prep_fn)
    if not dry_run:
        # 테스트 모드(dry_run)는 실제 운영 기록이 아니다 — 마지막 실행 표시를
        # 덮어써 사용자가 "돌았다"고 오해하게 만들지 않는다.
        try:
            from core import autonomy_overview as _ov
            _ov.record_cycle(result, profile=result.get("profile") or profile,
                             now=_as_naive(now or _now()))
        except Exception as exc:  # noqa: BLE001 — 기록 실패가 사이클 결과를 바꾸지 않는다
            log.debug(f"[autonomy] 현황 기록 생략({type(exc).__name__})")
    return result


def _save_only_pipeline(pipeline_fn: Callable) -> Callable:
    """준비 단계가 쓰는 파이프라인 래퍼 — **보관 전용을 한 번 더 못박는다.**

    준비 경로에서 `save_only=False` 가 새어 들어가면 그 순간 발행 경로가 된다.
    래퍼가 그 인자를 강제로 True 로 덮고, 후보 지정(`only_titles`)도 허용하지
    않는다(준비는 '어떤 글을 올릴지'를 정하는 자리가 아니다).
    """
    def _wrapped(**kwargs):
        kwargs["save_only"] = True
        kwargs.pop("only_titles", None)
        return pipeline_fn(**kwargs)
    return _wrapped


def _run_prep_stage(*, policy: dict, profile: str, now: datetime,
                    done_today: int, reserve_slots: int, dry_run: bool,
                    prep_fn: Optional[Callable],
                    pipeline_fn: Optional[Callable] = None) -> dict:
    """완성 재고 준비 — 재고 부족 경고를 **실제 준비 행동**으로 잇는 자리.

    이 단계는 절대 발행하지 않는다(`autonomy_prep.run_prep` 는 보관 전용 경로만
    쓴다). 실패는 사이클을 죽이지 않고 사유만 남긴다.

    ⚠️ 호출자가 파이프라인을 주입했으면(`run_cycle(pipeline_fn=...)`) **준비도 그
    파이프라인을 쓴다.** 안 그러면 "가짜 파이프라인을 넣었는데 준비 단계가 진짜
    파이프라인을 돌리는" 사고가 난다(테스트가 실제 생성을 태우는 경로).
    """
    fn = prep_fn
    if fn is None:
        try:
            from core.autonomy_prep import run_prep as fn  # type: ignore
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "skipped": "module_missing",
                    "reason": f"준비 모듈을 불러오지 못했습니다({type(exc).__name__})"}
    kwargs = {"profile": profile, "policy": policy, "now": now,
              "done_today": done_today, "reserve_slots": reserve_slots,
              "dry_run": dry_run}
    if pipeline_fn is not None:
        kwargs["pipeline_fn"] = _save_only_pipeline(pipeline_fn)
    try:
        return fn(**kwargs) or {}
    except Exception as exc:  # noqa: BLE001
        log.warning(f"[autonomy] 재고 준비 실패({type(exc).__name__})")
        return {"ok": False, "skipped": "prep_error",
                "reason": f"재고 준비 중 오류({type(exc).__name__})"}


def _run_cycle_impl(*, profile: Optional[str] = None, now: Optional[datetime] = None,
                    dry_run: bool = False,
                    pipeline_fn: Optional[Callable] = None,
                    post_list_fn: Optional[Callable] = None,
                    reconcile_fn: Optional[Callable] = None,
                    blog_id: str = "",
                    progress_cb: Optional[Callable] = None,
                    prep_fn: Optional[Callable] = None) -> dict:
    """한 사이클 본체. 반환은 사람이 읽을 수 있는 요약 dict."""
    now = _as_naive(now or _now())
    profile, _mismatch = _resolve_profile(profile)
    today = now.date().strftime("%Y-%m-%d")
    result = {
        "ok": True, "profile": profile, "day": today, "at": _iso(now),
        "skipped": "", "reason": "", "scope": "",
        "reconciled": [], "planned": [], "processed": [],
        "open_items": 0, "inventory": {}, "budget": {}, "messages": [],
        "prep": {},
    }

    if _mismatch:
        result.update(ok=False, skipped="profile_mismatch", reason=_mismatch)
        log.warning(f"[autonomy] {_mismatch}")
        return result

    # ── 정지 스위치 (파일 기반 — GUI 가 꺼져 있어도 듣는다) ──────────
    try:
        from core import kill_switch as _ks
        if _ks.estop_active():
            result.update(skipped="estop",
                          reason="전체 비상정지 상태입니다. 해제 전까지 실행하지 않습니다.")
            log.warning("[autonomy] 전체 비상정지 — 사이클을 시작하지 않습니다")
            return result
        paused = _ks.pause_active(profile=profile)
    except Exception as exc:  # noqa: BLE001 — 모듈 부재는 기존 동작
        log.debug(f"[autonomy] 정지 스위치 확인 생략: {type(exc).__name__}")
        paused = False

    # ── 계정 정책 ────────────────────────────────────────────────
    try:
        from core import autonomy_policy as _pol
        policy = _pol.load_policy(profile)
        active = _pol.is_active(policy, profile=profile)
    except Exception as exc:  # noqa: BLE001
        result.update(ok=False, skipped="policy_error",
                      reason=f"정책을 읽지 못했습니다({type(exc).__name__})")
        return result
    result["scope"] = str(policy.get("scope") or "draft_only")

    # 대조는 **읽기 전용**이라 비활성·일시중지 상태에서도 수행한다 —
    # 중단분을 방치하면 사람이 확인할 목록조차 갱신되지 않는다.
    result["reconciled"] = reconcile_open_items(
        blog_id=blog_id, post_list_fn=post_list_fn, now=now,
        reconcile_fn=reconcile_fn)

    try:
        from core import work_queue as _wq
        result["open_items"] = len(_wq.list_items(states=_wq.OPEN_STATES))
    except Exception:  # noqa: BLE001
        pass

    if not active:
        result.update(skipped="disabled",
                      reason="이 계정의 무인 운영이 꺼져 있습니다(설정에서 켤 수 있습니다).")
        return result
    if paused or policy.get("pause"):
        result.update(skipped="pause",
                      reason="이 계정이 일시중지 상태입니다. 새 글을 시작하지 않습니다.")
        return result

    # ── 준비 상태(필수 보호 포함) 확인 — 막힌 것이 있으면 슬롯을 처리하지 않는다.
    # 2026-09-18 마감 결함 B: 비용 원장·브라우저 중재·중복 발행 차단·상태 기록은
    # 선택 기능이 아니라 안전장치다. 가드 모듈 자체를 못 불러오면 그 사실 자체가
    # "준비되지 않음"이다(조용히 계속 가지 않는다 — fail-closed).
    try:
        from core import autonomy_guard as _guard
    except Exception as exc:  # noqa: BLE001
        result.update(ok=False, skipped="not_ready",
                      reason=f"무인 운영 안전장치 모듈을 불러오지 못했습니다({type(exc).__name__})",
                      blockers=[{"code": "guard_missing",
                                 "detail": f"core.autonomy_guard 를 불러오지 못했습니다"
                                           f"({type(exc).__name__})",
                                 "fix": "설치가 손상됐을 수 있습니다."}])
        log.error(f"[autonomy] 안전장치 모듈 부재로 사이클을 시작하지 않습니다: {exc}")
        return result

    ready = _guard.readiness(profile=profile, policy=policy)
    if not ready.get("ok"):
        blockers = ready.get("blockers") or []
        reason = " / ".join(b.get("detail", "") for b in blockers if b.get("detail")) \
            or "무인 운영을 시작할 준비가 되지 않았습니다."
        result.update(ok=False, skipped="not_ready", reason=reason, blockers=blockers)
        log.warning(f"[autonomy] 준비 상태 미달로 사이클을 시작하지 않습니다: {reason}")
        return result

    # ── 계획 수립부터 슬롯 처리까지 — 필수 보호 세션 안에서 돈다 ─────────
    # (파이프라인 안쪽의 엔진 호출까지 이 세션이 열려 있어야 cost_ledger 등이
    # 설정과 무관하게 실제로 강제된다. §guards_active().)
    with _guard.session(profile):
        result["inventory"] = inventory_status(policy=policy)
        if result["inventory"].get("warning"):
            result["messages"].append(result["inventory"]["warning"])
            log.warning(f"[autonomy] {result['inventory']['warning']}")

        # ── 오늘 계획 ────────────────────────────────────────────────
        try:
            from core import work_queue as _wq
        except Exception as exc:  # noqa: BLE001
            result.update(ok=False, skipped="queue_error",
                          reason=f"작업큐를 열지 못했습니다({type(exc).__name__})")
            return result

        done_today = 0
        try:
            for item in _wq.list_items(day=today):
                if (item.get("publish_state") or "") in (
                        "reserved", "live", "unconfirmed", "missing", "verifying"):
                    done_today += 1
        except Exception:  # noqa: BLE001
            done_today = 0

        budget_left = None
        try:
            from core import cost_ledger as _cl
            # 계정 정책의 하루 상한을 원장에 반영한다 — 정책만 바꾸고 원장이 모르면
            # 계정 한도가 "미설정"으로 남아 판정되지 않는다.
            _budget = policy.get("budget") or {}

            def _limit(raw):
                """0 은 '무제한'이 아니라 '오늘은 더 만들지 않음'이다 — None 과 구분한다."""
                try:
                    value = int(raw)
                except (TypeError, ValueError):
                    return None
                return value if value >= 0 else None

            try:
                _cl.set_limits(profile=profile,
                               write=_limit(_budget.get("write_calls")),
                               image=_limit(_budget.get("image_calls")))
            except Exception:  # noqa: BLE001 — 한도 반영 실패가 사이클을 막지 않는다
                pass
            remaining = _cl.remaining("write", profile=profile)
            result["budget"] = remaining
            lefts = [v for v in (remaining.get("profile_left"),
                                 remaining.get("global_left")) if v is not None]
            if lefts:
                budget_left = max(0, min(lefts))
        except Exception:  # noqa: BLE001 — 원장 없으면 per-run 상한만 적용(기존)
            pass

        try:
            planned = _wq.plan_today(policy=policy, now=now, done_today=done_today,
                                     budget_left=budget_left)
        except Exception as exc:  # noqa: BLE001
            result.update(ok=False, skipped="plan_error",
                          reason=f"오늘 계획을 세우지 못했습니다({type(exc).__name__})")
            return result
        result["planned"] = planned

        # ── 완성 재고 준비 ───────────────────────────────────────────
        # 범위가 `publish` 면 슬롯이 재고를 **소비**하므로 준비가 **먼저**다
        # (당일 예약 목표 > background 재고). `draft_only` 면 슬롯 자체가 보관본을
        # 만들므로 같은 일을 두 번 하지 않도록 슬롯 뒤에 background 로 돌린다.
        prep_first = result["scope"] == "publish"
        if prep_first:
            result["prep"] = _run_prep_stage(
                policy=policy, profile=profile, now=now, done_today=done_today,
                reserve_slots=len(planned), dry_run=dry_run, prep_fn=prep_fn,
                pipeline_fn=pipeline_fn)
            after = (result["prep"] or {}).get("inventory_after") or {}
            if after:
                result["inventory"] = after

        if not planned:
            result["reason"] = (
                "지금 처리할 슬롯이 없습니다"
                f"(오늘 처리 {done_today}편 / 하루 {policy.get('daily_posts')}편"
                + (f" · 남은 호출 {budget_left}" if budget_left is not None else "")
                + ").")
            if not prep_first:
                result["prep"] = _run_prep_stage(
                    policy=policy, profile=profile, now=now, done_today=done_today,
                    reserve_slots=0, dry_run=dry_run, prep_fn=prep_fn,
                    pipeline_fn=pipeline_fn)
                after = (result["prep"] or {}).get("inventory_after") or {}
                if after:
                    result["inventory"] = after
            return result

        for slot in planned:
            outcome = _process_slot(slot, policy=policy, profile=profile, now=now,
                                    dry_run=dry_run, pipeline_fn=pipeline_fn,
                                    progress_cb=progress_cb)
            result["processed"].append(outcome)
            if outcome.get("stop_cycle"):
                result["reason"] = outcome.get("reason", "")
                break

        # `draft_only` 는 슬롯이 보관본을 만든 **뒤에** 남은 재고만 채운다.
        if not prep_first and not any(p.get("stop_cycle") for p in result["processed"]):
            result["prep"] = _run_prep_stage(
                policy=policy, profile=profile, now=now, done_today=done_today,
                reserve_slots=0, dry_run=dry_run, prep_fn=prep_fn,
                pipeline_fn=pipeline_fn)
            after = (result["prep"] or {}).get("inventory_after") or {}
            if after:
                result["inventory"] = after

    try:
        from core import work_queue as _wq
        result["open_items"] = len(_wq.list_items(states=_wq.OPEN_STATES))
    except Exception:  # noqa: BLE001
        pass
    return result


def _process_slot(slot: dict, *, policy: dict, profile: str, now: datetime,
                  dry_run: bool, pipeline_fn: Optional[Callable],
                  progress_cb: Optional[Callable]) -> dict:
    """슬롯 하나 — 예산·점유·브라우저 중재를 통과하면 파이프라인 1편을 돌린다."""
    from core import work_queue as _wq

    item_id = slot.get("item_id") or ""
    publish_at = slot.get("publish_at") or ""
    scope = str(policy.get("scope") or "draft_only")
    out = {"item_id": item_id, "slot_at": slot.get("slot_at"),
           "publish_at": publish_at, "scope": scope,
           "ok": False, "reason": "", "stop_cycle": False, "pipeline": {}}

    # 글 경계 정지 확인
    try:
        from core import publish_state_bridge as _psb
        hold = _psb.guard_new_item(profile=profile)
    except Exception:  # noqa: BLE001
        _psb, hold = None, ""
    if hold:
        out.update(reason=("전체 비상정지" if hold == "estop" else "계정 일시중지"),
                   stop_cycle=True)
        return out

    # 과거 시각 공개 예약 금지 — 계획 단계에서 걸러지지만 한 번 더 막는다.
    if not publish_at:
        out.update(reason="유효한 공개 예약 시각을 잡지 못해 이 슬롯을 건너뜁니다")
        return out
    try:
        if _as_naive(datetime.fromisoformat(publish_at)) <= now:
            out.update(reason="공개 예약 시각이 이미 지나 이 슬롯을 건너뜁니다")
            return out
    except (TypeError, ValueError):
        out.update(reason="공개 예약 시각 형식이 올바르지 않아 건너뜁니다")
        return out

    # 예산 사전 점검 — **예약이 아니라 잔량 확인**이다(2026-09-18 마감 결함 B 수리).
    # 예전에는 여기서 슬롯마다 1회씩 미리 원장을 차감(가예약)했는데, 그 수만으로는
    # 실제 사용량을 대표하지 못한다(글감 재사용 시 나중에 되돌리는 식). 실제 차감은
    # 이제 엔진(집필·이미지) 호출 지점이 한다 — 여기서는 시작해도 되는지만 본다.
    budget_stop = _check_budget_exhausted(profile)
    if budget_stop is not None:
        out.update(budget_stop)
        return out

    # 점유하기 전에 항목이 큐에 있어야 한다(없는 항목은 점유할 수 없다).
    # ⚠️ 저장 실패와 경합을 **구분해서** 보고한다 — 둘 다 "다른 실행이 처리 중"으로
    #    적으면 디스크·권한 문제를 운영자가 경합으로 오진단한다.
    leased = False
    lease_error = ""
    try:
        _wq.set_state(item_id, "selected", slot_at=slot.get("slot_at"),
                      publish_at=publish_at, day=now.date().strftime("%Y-%m-%d"),
                      reason="planned")
        leased = _wq.lease(item_id, ttl_seconds=3600)
    except Exception as exc:  # noqa: BLE001
        lease_error = type(exc).__name__
        log.warning(f"[autonomy] 슬롯 기록·점유 실패({lease_error})")
        leased = False
    if not leased:
        out.update(reason=(
            f"작업 기록을 저장하지 못했습니다({lease_error}) — 저장 공간·권한을 확인하세요"
            if lease_error else "다른 실행이 이 슬롯을 처리 중입니다"))
        return out

    started = datetime.now()
    try:
        if _psb is not None:
            _psb.set_context(item_id=item_id, publish_at=publish_at,
                             scope=scope, profile=profile)

        run = _run_pipeline_for_slot(scope=scope, dry_run=dry_run,
                                     pipeline_fn=pipeline_fn,
                                     progress_cb=progress_cb)
        out["pipeline"] = run
        if run.get("error") == "browser_busy":
            out.update(reason="다른 계정이 브라우저를 쓰고 있어 이 슬롯을 양보했습니다",
                       stop_cycle=True)
        elif run.get("error"):
            out.update(reason=f"파이프라인 오류: {run.get('error')}")
        else:
            out["ok"] = bool(run.get("success") or run.get("saved_drafts"))
            if not out["ok"]:
                out["reason"] = "이번 슬롯에서 발행·보관된 글이 없습니다"

        try:
            from core import cost_ledger as _cl2
            _cl2.record_observation(
                "write", seconds=(datetime.now() - started).total_seconds(),
                profile=profile)
        except Exception:  # noqa: BLE001 — 관측 실패는 슬롯 결과를 바꾸지 않는다
            pass
    finally:
        if _psb is not None:
            try:
                _psb.clear_context()
            except Exception:  # noqa: BLE001
                pass
        try:
            _wq.release(item_id)
        except Exception:  # noqa: BLE001
            pass

    # 파이프라인이 상태를 남겼으면 그것이 최종 판정이다.
    try:
        item = _wq.get_item(item_id) or {}
        out["publish_state"] = item.get("publish_state") or "selected"
    except Exception:  # noqa: BLE001
        pass
    return out


def _run_pipeline_for_slot(*, scope: str, dry_run: bool,
                           pipeline_fn: Optional[Callable],
                           progress_cb: Optional[Callable]) -> dict:
    """기존 파이프라인을 **1편만** 돌린다. 범위에 따라 보관까지/예약까지.

    브라우저를 실제로 띄우는 것은 `scope=publish` 경로뿐이므로, 그 경로에만
    머신 전역 중재 락을 건다(두 계정이 동시에 크롬을 띄우는 것을 막는다).
    """
    save_only = (scope != "publish")

    def _call() -> dict:
        fn = pipeline_fn
        if fn is None:
            from scheduler.auto_scheduler import run_pipeline as fn  # type: ignore
        return fn(save_only=save_only, max_count=1, dry_run=dry_run,
                  progress_cb=progress_cb) or {}

    if save_only or dry_run:
        return _call()

    try:
        from core.machine_arbiter import browser_slot, BrowserBusy
    except Exception:  # noqa: BLE001 — 중재 모듈 부재는 기존 동작
        return _call()
    try:
        with browser_slot(timeout_seconds=0, owner="autonomy"):
            return _call()
    except BrowserBusy:
        log.warning("[autonomy] 브라우저가 사용 중 — 이 슬롯을 양보합니다")
        return {"total": 0, "success": 0, "error": "browser_busy"}


# ─────────────────────────────────────────────────────
# 4) 재시작 복구
# ─────────────────────────────────────────────────────

def recover(*, profile: Optional[str] = None, now: Optional[datetime] = None,
            post_list_fn: Optional[Callable] = None,
            reconcile_fn: Optional[Callable] = None,
            blog_id: str = "") -> dict:
    """부팅·재시작 후 중단분 정리. **어떤 항목도 자동 재발행하지 않는다.**

    반환 `{"reconciled": [...], "open": [...], "skipped": str}`.
    `open` 이 비어 있지 않으면 사람 확인이 필요한 항목이 남아 있다는 뜻이다.
    """
    now = _as_naive(now or _now())
    profile, _mismatch = _resolve_profile(profile)
    out = {"profile": profile, "at": _iso(now), "skipped": "",
           "reconciled": [], "open": []}
    if _mismatch:
        out.update(skipped="profile_mismatch", reason=_mismatch)
        log.warning(f"[autonomy] {_mismatch}")
        return out

    try:
        from core import kill_switch as _ks
        if _ks.estop_active():
            out["skipped"] = "estop"
            return out
        if _ks.pause_active(profile=profile):
            out["skipped"] = "pause"
    except Exception:  # noqa: BLE001
        pass

    out["reconciled"] = reconcile_open_items(
        blog_id=blog_id, post_list_fn=post_list_fn, now=now,
        reconcile_fn=reconcile_fn)
    try:
        from core import work_queue as _wq
        out["open"] = [
            {"item_id": i.get("item_id"), "title": i.get("title"),
             "publish_state": i.get("publish_state"),
             "publish_at": i.get("publish_at")}
            for i in _wq.list_items(states=_wq.OPEN_STATES + ("missing",))
        ]
    except Exception:  # noqa: BLE001
        pass
    return out


def resolve_manual(item_id: str, *, outcome: str, note: str = "",
                   by: str = "", profile: Optional[str] = None) -> dict:
    """사람이 네이버에서 확인한 결말을 닫는다 — **수동 확인 큐의 유일한 출구**.

    자동 재발행을 금지하는 대신, 사람이 확인한 뒤 닫을 길은 반드시 있어야 한다.
    길이 없으면 한 번 미확정이 된 글은 영원히 막힌 채 남는다.

    Args:
        outcome:
            - `"published"`  — 실제로 올라가 있었다 → `live` 로 닫는다(근거=사람 확인).
              보관본은 `uploaded` 로 정리해 재드레인 대상에서 뺀다.
            - `"not_published"` — 올라가지 않았다 → `failed` 로 닫고 보관본을
              `draft_ready` 로 되돌려 **사람이 원하면 다시 시도할 수 있게** 한다.
              (여전히 자동 재발행은 아니다 — 사람이 이 버튼을 눌렀을 때만이다.)
        note: 사람이 남기는 확인 메모.

    Returns:
        {"ok": bool, "item_id": str, "publish_state": str, "reason": str}
    """
    if outcome not in ("published", "not_published"):
        return {"ok": False, "item_id": item_id, "publish_state": "",
                "reason": f"알 수 없는 처리 값: {outcome}"}
    try:
        from core import work_queue as _wq
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "item_id": item_id, "publish_state": "",
                "reason": f"작업큐를 열지 못했습니다({type(exc).__name__})"}

    item = None
    try:
        item = _wq.get_item(item_id)
    except Exception:  # noqa: BLE001
        pass
    if not item:
        return {"ok": False, "item_id": item_id, "publish_state": "",
                "reason": "해당 항목을 찾지 못했습니다"}

    target = "live" if outcome == "published" else "failed"
    evidence = {"source": "manual_check", "by": by or "user",
                "note": note, "checked_at": _iso(_now())}
    try:
        _wq.set_state(item_id, target, reason="manual_resolve", evidence=evidence)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "item_id": item_id, "publish_state": "",
                "reason": f"상태 기록 실패({type(exc).__name__})"}

    slug = item.get("slug") or ""
    if slug:
        try:
            from core import draft_store as _ds
            _ds.set_publish_state(slug, target, evidence=evidence)
            if outcome == "published":
                _ds.mark_status(slug, "uploaded", url=item.get("url") or None)
            else:
                # 사람이 "안 올라갔다"고 확인한 글만 다시 후보로 돌린다.
                _ds.mark_status(slug, "draft_ready",
                                error="manual_check: 미발행 확인 — 다시 시도 가능")
        except Exception as exc:  # noqa: BLE001
            log.warning(f"[autonomy] 보관본 상태 정리 실패({type(exc).__name__}) — {slug}")

    return {"ok": True, "item_id": item_id, "publish_state": target,
            "reason": "사람 확인으로 결말을 닫았습니다"}


def status(*, profile: Optional[str] = None, now: Optional[datetime] = None) -> dict:
    """GUI·CLI 표시용 요약(부수효과 없음)."""
    now = _as_naive(now or _now())
    profile, _mismatch = _resolve_profile(profile)
    today = now.date().strftime("%Y-%m-%d")
    out = {"profile": profile, "day": today, "enabled": False, "scope": "",
           "pause": False, "estop": False, "today": {}, "open": [],
           "inventory": {}, "budget": {}, "next_slot": "", "capability": {},
           "master": False, "readiness": {"ok": False, "blockers": []},
           "error": ""}
    if _mismatch:
        out["error"] = _mismatch
        return out

    policy = {}
    try:
        from core import autonomy_policy as _pol
        policy = _pol.load_policy(profile)
        out["enabled"] = _pol.is_active(policy, profile=profile)
        out["scope"] = policy.get("scope", "")
        out["pause"] = bool(policy.get("pause"))
        slots = [s for s in _pol.slots_for_day(policy, now.date()) if s > now]
        if slots:
            out["next_slot"] = _iso(slots[0])
    except Exception:  # noqa: BLE001
        pass

    try:
        from core import kill_switch as _ks
        st = _ks.status(profile=profile)
        out["estop"] = bool(st.get("estop"))
        out["pause"] = bool(st.get("pause")) or out["pause"]
    except Exception:  # noqa: BLE001
        pass

    try:
        from core import work_queue as _wq
        counts: dict = {}
        for item in _wq.list_items(day=today):
            key = item.get("publish_state") or "none"
            counts[key] = counts.get(key, 0) + 1
        out["today"] = counts
        out["open"] = [
            {"item_id": i.get("item_id"), "title": i.get("title"),
             "publish_state": i.get("publish_state"),
             "publish_at": i.get("publish_at"), "reason": i.get("reason")}
            for i in _wq.list_items(states=_wq.OPEN_STATES + ("missing",))
        ]
    except Exception:  # noqa: BLE001
        pass

    out["inventory"] = inventory_status(policy=policy)
    try:
        from core import autonomy_prep as _prep
        out["prep"] = _prep.snapshot(profile=profile)
    except Exception:  # noqa: BLE001
        out["prep"] = {}
    try:
        from core import cost_ledger as _cl
        out["budget"] = _cl.snapshot(profile=profile)
    except Exception:  # noqa: BLE001
        pass
    try:
        from core import autonomy_guard as _guard
        out["master"] = _guard.master_enabled()
        # probe=False — status() 는 반복 호출되는 표시 전용 경로라 원장 왕복
        # 예약·취소(부수효과)를 매번 만들지 않는다.
        out["readiness"] = _guard.readiness(profile=profile, policy=policy or None,
                                            probe=False)
    except Exception:  # noqa: BLE001
        pass
    try:
        import json
        from config import profile as _pf
        path = _pf.logs_dir(profile) / "stats_capability.json"
        if path.exists():
            out["capability"] = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pass
    return out


__all__ = ["run_cycle", "recover", "status", "reconcile_open_items",
           "resolve_manual",
           "inventory_status", "blog_id_for_stats",
           "target_preview", "run_target", "TARGET_MAX_POSTS"]
