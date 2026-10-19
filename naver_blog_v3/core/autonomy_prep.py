"""autonomy_prep — 완성 재고 **자동 준비** (2026-09-19).

무인 운영의 빈 자리 하나: `autonomy_runner.inventory_status()` 는 "완성 재고가
모자라다"를 **계산만** 하고 경고 문구로 끝났다. 그 경고를 실제 준비 행동으로
잇는 것이 이 모듈이다.

**우선순위(설계 고정)**

1. 오늘 예약 목표를 채우는 준비가 먼저다(background 재고 적립보다 우선).
2. 준비의 첫 수단은 **기존 글의 빠진 이미지만 채우기**다 — 이미지 없는 보관본이
   이미 쌓여 있는데 새 글을 더 만드는 것은 해결이 아니다.
3. 채운 뒤 완성 여부를 **다시 검사**하고, 그래도 모자랄 때만 새 글을 **보관 전용**
   으로 준비한다.

**절대 하지 않는 것**

- 발행·예약 등록 (`run_pipeline(save_only=True)` 만 쓴다 — 업로드 경로 0회)
- 기존 완성 이미지 재생성 / 기존 본문 덮어쓰기
- 결말이 미상이거나 이미 확정된 글(`reserving`/`unconfirmed`/`missing`/
  `reserved`/`live`/`verifying`)을 재료로 삼기
- 정책·원장 상한 초과 (계획 단계에서 미리 깎고, 실제 차감은 엔진 호출 지점이 한다)

한 편이 계속 실패해 무한 재시도하거나 뒤의 글을 굶기지 않도록 슬러그별
시도횟수·사유·다음 시도 시각을 `logs/autonomy_prep_state.json` 에 남긴다.
"""
from __future__ import annotations

import copy
import json
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional, Sequence

from config.logger import get_logger

log = get_logger(__name__)

SCHEMA = "nba/autonomy_prep@1"
_FILE_NAME = "autonomy_prep_state.json"

#: 한 번의 준비 실행에서 손대는 보관본 상한(계정 정책과 별개의 안전 상한).
MAX_TOPUP_PER_RUN = 5
MAX_NEW_DRAFTS_PER_RUN = 3

#: 같은 슬러그를 몇 번까지 다시 시도하는가(넘으면 사람이 볼 목록으로 뺀다).
MAX_ATTEMPTS = 3
#: 시도 횟수별 재시도 대기(시간). 마지막 값이 상한이다.
RETRY_BACKOFF_HOURS = (1, 6, 24)

#: 재료로 삼으면 안 되는 발행 상태 — 결말 미상 + 이미 확정.
PROTECTED_PUBLISH_STATES = (
    "reserving", "unconfirmed", "missing", "reserved", "live", "verifying",
)

#: 정책 값 정수 검증 상한(GUI 스핀 범위와 같은 값).
_MAX_DAILY_POSTS = 20
_MAX_INVENTORY_DAYS = 30


# ─────────────────────────────────────────────────────
# 상태 파일 (슬러그별 시도 기록)
# ─────────────────────────────────────────────────────
def state_path(profile: Optional[str] = None) -> Path:
    from config import profile as _pf

    return _pf.logs_dir(profile) / _FILE_NAME


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".autonomy_prep_", suffix=".tmp", dir=str(path.parent))
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


def load_state(profile: Optional[str] = None) -> dict:
    """{"schema", "slugs": {slug: {...}}, "last_run": {...}}. 없거나 깨졌으면 빈 상태."""
    empty = {"schema": SCHEMA, "slugs": {}, "last_run": {}}
    try:
        path = state_path(profile)
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return empty
    except (OSError, json.JSONDecodeError) as exc:
        log.warning(f"[autonomy_prep] 상태 파일을 읽지 못했습니다({type(exc).__name__}) — 새로 시작")
        return empty
    if not isinstance(data, dict):
        return empty
    data.setdefault("schema", SCHEMA)
    if not isinstance(data.get("slugs"), dict):
        data["slugs"] = {}
    if not isinstance(data.get("last_run"), dict):
        data["last_run"] = {}
    return data


def save_state(data: dict, *, profile: Optional[str] = None) -> bool:
    try:
        _atomic_write_json(state_path(profile), data)
        return True
    except (OSError, TypeError, ValueError) as exc:
        log.warning(f"[autonomy_prep] 상태 저장 실패({type(exc).__name__})")
        return False


def _parse_iso(raw) -> Optional[datetime]:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt


def _backoff_for(attempts: int) -> timedelta:
    idx = min(max(0, attempts - 1), len(RETRY_BACKOFF_HOURS) - 1)
    return timedelta(hours=RETRY_BACKOFF_HOURS[idx])


def note_attempt(slug: str, *, ok: bool, reason: str = "", now: Optional[datetime] = None,
                 profile: Optional[str] = None, state: Optional[dict] = None,
                 persist: bool = True) -> dict:
    """슬러그 한 편의 준비 결과를 기록한다. 성공하면 시도 기록을 지운다.

    반환은 갱신된 전체 상태 dict(호출자가 여러 건을 모아 한 번에 저장할 수 있게).
    """
    now = now or datetime.now()
    data = state if state is not None else load_state(profile)
    slugs = data.setdefault("slugs", {})
    if ok:
        slugs.pop(slug, None)
    else:
        entry = slugs.get(slug) or {}
        attempts = int(entry.get("attempts") or 0) + 1
        entry.update({
            "attempts": attempts,
            "reason": str(reason or "unknown"),
            "last_attempt": now.isoformat(timespec="seconds"),
            "next_attempt": (now + _backoff_for(attempts)).isoformat(timespec="seconds"),
            "exhausted": attempts >= MAX_ATTEMPTS,
        })
        slugs[slug] = entry
    if persist:
        save_state(data, profile=profile)
    return data


def slug_blocked(slug: str, state: dict, *, now: Optional[datetime] = None) -> str:
    """지금 이 슬러그를 다시 시도해도 되는가. 막혔으면 사유 문자열, 아니면 ""."""
    entry = ((state or {}).get("slugs") or {}).get(slug)
    if not entry:
        return ""
    now = now or datetime.now()
    if int(entry.get("attempts") or 0) >= MAX_ATTEMPTS:
        return "attempts_exhausted"
    nxt = _parse_iso(entry.get("next_attempt"))
    if nxt is not None and now < nxt:
        return "retry_waiting"
    return ""


# ─────────────────────────────────────────────────────
# 정책 값 정규화
# ─────────────────────────────────────────────────────
def _clamp_int(raw, *, default: int, low: int, high: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(low, min(value, high))


def normalize_targets(policy: Optional[dict]) -> dict:
    """정책의 편수·재고 일수를 검증된 정수로. `inventory_days=0` 은 **유효한 값**이며
    "background 재고를 쌓지 않는다"는 뜻이다(당일 목표는 그대로 유효)."""
    policy = policy or {}
    daily = _clamp_int(policy.get("daily_posts"), default=0, low=0, high=_MAX_DAILY_POSTS)
    days = _clamp_int(policy.get("inventory_days"), default=0, low=0, high=_MAX_INVENTORY_DAYS)
    return {"daily_posts": daily, "inventory_days": days,
            "target_inventory": daily * days}


# ─────────────────────────────────────────────────────
# 계획 (순수 함수 — 부수효과 0)
# ─────────────────────────────────────────────────────
def plan_prep(*, policy: dict, inventory: dict, now: Optional[datetime] = None,
              done_today: int = 0, reserve_slots: int = 0,
              write_left: Optional[int] = None, image_left: Optional[int] = None,
              topup_candidates: Optional[Sequence[dict]] = None,
              protected_slugs: Sequence[str] = (),
              state: Optional[dict] = None) -> dict:
    """준비 계획을 계산한다. **아무것도 실행하지 않는다.**

    Args:
        inventory: `autonomy_runner.inventory_status()` 결과.
        done_today: 오늘 이미 확정된 편수(작업큐 기준).
        reserve_slots: 이번 사이클이 슬롯 처리에 쓸 집필 호출 수 — 준비가 그만큼
            남겨 두고 계획한다(준비가 슬롯 예산을 먹어 치우지 않게).
        topup_candidates: 이미지 보충 후보(`draft_image_fill.fill_targets(topup=True)`
            형태의 항목). None 이면 계산하지 않고 0으로 둔다(미리보기 전용 호출).
        protected_slugs: 재료로 쓰면 안 되는 슬러그.

    Returns:
        {"target_inventory","ready","partial","today_need","today_shortfall",
         "inventory_deficit","steps":[{"action","count","slugs","reason"}],
         "caps":{...},"blocked":[{"slug","reason"}],"notes":[...]}.
    """
    now = now or datetime.now()
    state = state or {"slugs": {}}
    targets = normalize_targets(policy)
    inventory = inventory or {}

    ready = max(0, int(inventory.get("ready_with_images") or 0))
    partial = max(0, int(inventory.get("partial_images") or 0))

    daily = targets["daily_posts"]
    today_need = max(0, daily - max(0, int(done_today or 0)))
    today_shortfall = max(0, today_need - ready)
    inventory_deficit = max(0, targets["target_inventory"] - ready)

    # ★ 당일 목표가 background 재고보다 우선 — 필요한 편수는 둘 중 큰 쪽이지만,
    #   "왜 만드는가"의 우선순위는 today 가 먼저다(아래 steps 의 reason 으로 남는다).
    total_need = max(today_shortfall, inventory_deficit)

    caps = {
        "write_left": write_left, "image_left": image_left,
        "reserve_slots": max(0, int(reserve_slots or 0)),
        "max_topup_per_run": MAX_TOPUP_PER_RUN,
        "max_new_per_run": MAX_NEW_DRAFTS_PER_RUN,
    }
    out = {
        "at": now.isoformat(timespec="seconds"),
        "target_inventory": targets["target_inventory"],
        "daily_posts": daily, "inventory_days": targets["inventory_days"],
        "ready": ready, "partial": partial,
        "today_need": today_need, "today_shortfall": today_shortfall,
        "inventory_deficit": inventory_deficit,
        "total_need": total_need,
        "steps": [], "caps": caps, "blocked": [], "notes": [],
    }

    if total_need <= 0:
        out["notes"].append("재고가 목표를 채우고 있어 준비할 것이 없습니다.")
        return out

    # ── ① 기존 글의 빠진 이미지만 채우기 ───────────────────────────
    protected = {s for s in (protected_slugs or []) if s}
    chosen: list = []
    for entry in (topup_candidates or []):
        slug = str((entry or {}).get("slug") or "")
        if not slug:
            continue
        if slug in protected:
            out["blocked"].append({"slug": slug, "reason": "publish_state_protected"})
            continue
        if str((entry or {}).get("publish_state") or "none") in PROTECTED_PUBLISH_STATES:
            out["blocked"].append({"slug": slug, "reason": "publish_state_protected"})
            continue
        why = slug_blocked(slug, state, now=now)
        if why:
            out["blocked"].append({"slug": slug, "reason": why})
            continue
        chosen.append(slug)

    topup_cap = min(total_need, MAX_TOPUP_PER_RUN)
    if image_left is not None:
        topup_cap = min(topup_cap, max(0, int(image_left)))
    topup_slugs = chosen[:max(0, topup_cap)]
    if topup_slugs:
        out["steps"].append({
            "action": "image_topup", "count": len(topup_slugs),
            "slugs": topup_slugs,
            "reason": "today" if today_shortfall > 0 else "inventory",
        })
    elif chosen:
        out["notes"].append("이미지 보충 후보는 있으나 이번 실행 상한·예산이 남지 않았습니다.")

    # ── ② 그래도 모자라면 새 글을 **보관 전용**으로 ────────────────
    still_need = max(0, total_need - len(topup_slugs))
    new_cap = min(still_need, MAX_NEW_DRAFTS_PER_RUN)
    if write_left is not None:
        new_cap = min(new_cap, max(0, int(write_left) - caps["reserve_slots"]))
    if new_cap > 0:
        out["steps"].append({
            "action": "new_draft", "count": new_cap, "slugs": [],
            "reason": "today" if today_shortfall > len(topup_slugs) else "inventory",
        })
    elif still_need > 0:
        out["notes"].append(
            "새 글 준비가 필요하지만 남은 집필 호출이 없어 이번에는 만들지 않습니다.")

    if not out["steps"]:
        out["notes"].append("이번 실행에서 할 수 있는 준비가 없습니다.")
    return out


# ─────────────────────────────────────────────────────
# 실행
# ─────────────────────────────────────────────────────
def _topup_candidates() -> list:
    try:
        from core import draft_image_fill as _dif
        return list(_dif.fill_targets(topup=True) or [])
    except Exception as exc:  # noqa: BLE001 — 후보 선별 실패는 빈 목록
        log.warning(f"[autonomy_prep] 이미지 보충 후보 조회 실패({type(exc).__name__})")
        return []


def protected_slugs_now(*, profile: Optional[str] = None) -> set:
    """작업큐에서 결말 미상/확정 상태인 항목의 슬러그 — 재료로 쓰면 안 된다."""
    out: set = set()
    try:
        from core import work_queue as _wq
        for item in _wq.list_items(states=PROTECTED_PUBLISH_STATES, profile=profile):
            slug = str((item or {}).get("slug") or "")
            if slug:
                out.add(slug)
    except Exception as exc:  # noqa: BLE001 — 조회 실패는 보호 집합 없음(후보의
        # publish_state 검사가 2차 방어로 남는다)
        log.warning(f"[autonomy_prep] 보호 슬러그 조회 실패({type(exc).__name__})")
    return out


def _inventory(policy: dict) -> dict:
    from core import autonomy_runner as _ar
    return _ar.inventory_status(policy=policy)


def _budget_left(kind: str, profile: str) -> Optional[int]:
    try:
        from core import cost_ledger as _cl
        remaining = _cl.remaining(kind, profile=profile) or {}
    except Exception:  # noqa: BLE001 — 원장 없으면 상한 미지(None)
        return None
    lefts = [v for v in (remaining.get("profile_left"), remaining.get("global_left"))
             if v is not None]
    return max(0, min(lefts)) if lefts else None


def prep_preview(*, profile: Optional[str] = None, policy: Optional[dict] = None,
                 now: Optional[datetime] = None, done_today: int = 0,
                 reserve_slots: int = 0) -> dict:
    """준비 계획 미리보기 — **공개·예약·생성 부작용 0회**.

    CLI(`autonomy_cli.py --prep --plan`)와 GUI 가 같은 함수를 쓴다.
    """
    now = now or datetime.now()
    if policy is None:
        try:
            from core import autonomy_policy as _pol
            policy = _pol.load_policy(profile)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": f"정책을 읽지 못했습니다({type(exc).__name__})",
                    "plan": {}}
    try:
        inventory = _inventory(policy)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"재고를 계산하지 못했습니다({type(exc).__name__})",
                "plan": {}}

    prof = profile or ""
    plan = plan_prep(
        policy=policy, inventory=inventory, now=now, done_today=done_today,
        reserve_slots=reserve_slots,
        write_left=_budget_left("write", prof), image_left=_budget_left("image", prof),
        topup_candidates=_topup_candidates(),
        protected_slugs=protected_slugs_now(profile=profile),
        state=load_state(profile))
    return {"ok": True, "reason": "", "profile": prof, "plan": plan,
            "inventory": inventory, "dry_run": True}


def run_prep(*, profile: Optional[str] = None, policy: Optional[dict] = None,
             now: Optional[datetime] = None, done_today: int = 0,
             reserve_slots: int = 0, dry_run: bool = False,
             fill_fn: Optional[Callable] = None,
             pipeline_fn: Optional[Callable] = None,
             inventory_fn: Optional[Callable] = None) -> dict:
    """계획을 실제 준비 행동으로 옮긴다. **업로드·예약 호출 0회.**

    순서: 이미지 보충 → 완성 여부 **재검사** → 그래도 모자라면 새 글 보관 전용.
    """
    now = now or datetime.now()
    prof = profile or ""
    result = {"ok": True, "profile": prof, "at": now.isoformat(timespec="seconds"),
              "skipped": "", "reason": "", "plan": {}, "ran": [],
              "filled": 0, "saved": 0, "inventory_before": {}, "inventory_after": {},
              "dry_run": bool(dry_run)}

    # 정지 스위치 — 준비도 구독·디스크를 쓰는 행동이다.
    try:
        from core import kill_switch as _ks
        if _ks.estop_active():
            result.update(ok=True, skipped="estop", reason="전체 비상정지 상태입니다.")
            return result
        if _ks.pause_active(profile=profile):
            result.update(ok=True, skipped="pause", reason="이 계정이 일시중지 상태입니다.")
            return result
    except Exception as exc:  # noqa: BLE001
        log.debug(f"[autonomy_prep] 정지 스위치 확인 생략({type(exc).__name__})")

    if policy is None:
        try:
            from core import autonomy_policy as _pol
            policy = _pol.load_policy(profile)
        except Exception as exc:  # noqa: BLE001
            result.update(ok=False, skipped="policy_error",
                          reason=f"정책을 읽지 못했습니다({type(exc).__name__})")
            return result

    inv_fn = inventory_fn or (lambda: _inventory(policy))
    try:
        inventory = inv_fn() or {}
    except Exception as exc:  # noqa: BLE001
        result.update(ok=False, skipped="inventory_error",
                      reason=f"재고를 계산하지 못했습니다({type(exc).__name__})")
        return result
    result["inventory_before"] = inventory

    state = load_state(profile)
    plan = plan_prep(
        policy=policy, inventory=inventory, now=now, done_today=done_today,
        reserve_slots=reserve_slots,
        write_left=_budget_left("write", prof), image_left=_budget_left("image", prof),
        topup_candidates=_topup_candidates(),
        protected_slugs=protected_slugs_now(profile=profile), state=state)
    result["plan"] = plan

    if not plan.get("steps"):
        result["reason"] = "; ".join(plan.get("notes") or []) or "준비할 것이 없습니다."
        return result
    if dry_run:
        result.update(skipped="dry_run",
                      reason="계획만 확인했습니다 — 아무것도 만들지 않았습니다.")
        return result

    steps = {s["action"]: s for s in plan["steps"]}

    # ── ① 이미지 보충(기존 글의 빠진 자리만) ────────────────────────
    topup = steps.get("image_topup")
    if topup:
        fn = fill_fn
        if fn is None:
            from core.blog_service import fill_images as fn  # type: ignore
        want = set(topup["slugs"])
        try:
            summary = fn(limit=topup["count"], topup=True,
                         only_slugs=sorted(want)) or {}
        except Exception as exc:  # noqa: BLE001
            summary = {"error": type(exc).__name__}
            log.warning(f"[autonomy_prep] 이미지 보충 실패({type(exc).__name__})")
        result["ran"].append({"action": "image_topup", "summary": summary})
        result["filled"] = int(summary.get("filled") or 0)
        for row in (summary.get("results") or []):
            slug = str((row or {}).get("slug") or "")
            if not slug:
                continue
            note_attempt(slug, ok=bool(row.get("ok")),
                         reason=str(row.get("reason") or ""), now=now,
                         state=state, persist=False)
        # 대상이었는데 결과에 아예 없는 슬러그도 실패로 센다(조용한 누락 방지).
        seen = {str((r or {}).get("slug") or "") for r in (summary.get("results") or [])}
        for slug in want - seen:
            note_attempt(slug, ok=False, reason=_missing_reason(summary), now=now,
                         state=state, persist=False)
        save_state(state, profile=profile)

        # ── ② 완성 여부 재검사 ─────────────────────────────────────
        try:
            inventory = inv_fn() or {}
        except Exception as exc:  # noqa: BLE001
            log.warning(f"[autonomy_prep] 재검사 실패({type(exc).__name__}) — 이전 값 유지")
        result["inventory_after"] = inventory

    # ── ③ 그래도 모자라면 새 글(보관 전용) ──────────────────────────
    new_step = steps.get("new_draft")
    if new_step:
        recomputed = plan_prep(
            policy=policy, inventory=inventory, now=now, done_today=done_today,
            reserve_slots=reserve_slots,
            write_left=_budget_left("write", prof), image_left=_budget_left("image", prof),
            topup_candidates=[], protected_slugs=(), state=state)
        want_new = 0
        for step in recomputed.get("steps") or []:
            if step["action"] == "new_draft":
                want_new = int(step["count"])
        want_new = min(want_new, int(new_step["count"]))
        result["recomputed_new_draft"] = want_new
        if want_new <= 0:
            result["ran"].append({"action": "new_draft", "count": 0,
                                  "reason": "재검사 결과 재고가 채워져 새 글은 만들지 않았습니다."})
        else:
            fn2 = pipeline_fn
            if fn2 is None:
                from scheduler.auto_scheduler import run_pipeline as fn2  # type: ignore
            try:
                # ★ save_only=True 고정 — 준비 경로에는 업로드·예약이 없다.
                summary = fn2(save_only=True, max_count=want_new) or {}
            except Exception as exc:  # noqa: BLE001
                summary = {"error": type(exc).__name__}
                log.warning(f"[autonomy_prep] 새 글 준비 실패({type(exc).__name__})")
            result["ran"].append({"action": "new_draft", "count": want_new,
                                  "summary": summary})
            result["saved"] = int(summary.get("saved_drafts") or 0)

    if not result["inventory_after"]:
        try:
            result["inventory_after"] = inv_fn() or {}
        except Exception:  # noqa: BLE001
            result["inventory_after"] = inventory

    state["last_run"] = {
        "at": now.isoformat(timespec="seconds"),
        "filled": result["filled"], "saved": result["saved"],
        "ready_after": int((result["inventory_after"] or {}).get("ready_with_images") or 0),
    }
    save_state(state, profile=profile)
    return result


def _missing_reason(summary: dict) -> str:
    """결과 행이 없는 슬러그의 사유 — 배치가 남긴 skipped 사유를 그대로 물려준다."""
    skipped = (summary or {}).get("skipped") or {}
    if isinstance(skipped, dict) and skipped:
        return sorted(skipped)[0]
    if (summary or {}).get("error"):
        return str(summary["error"])
    return "not_processed"


def pending_attention(*, profile: Optional[str] = None,
                      state: Optional[dict] = None) -> list:
    """시도 상한에 도달해 자동 회복되지 않는 항목 — 사람이 볼 목록."""
    data = state if state is not None else load_state(profile)
    out = []
    for slug, entry in sorted((data.get("slugs") or {}).items()):
        if int((entry or {}).get("attempts") or 0) >= MAX_ATTEMPTS:
            out.append({"slug": slug, "attempts": entry.get("attempts"),
                        "reason": entry.get("reason") or "",
                        "last_attempt": entry.get("last_attempt") or ""})
    return out


def clear_slug(slug: str, *, profile: Optional[str] = None) -> bool:
    """사람이 손본 뒤 시도 기록을 지운다(다음 실행에서 다시 후보가 된다)."""
    data = load_state(profile)
    if slug not in (data.get("slugs") or {}):
        return False
    data["slugs"].pop(slug, None)
    return save_state(data, profile=profile)


def snapshot(*, profile: Optional[str] = None) -> dict:
    """표시용 요약(부수효과 0 — 파일만 읽는다)."""
    data = load_state(profile)
    return {
        "last_run": copy.deepcopy(data.get("last_run") or {}),
        "waiting": len([s for s in (data.get("slugs") or {}).values()
                        if int((s or {}).get("attempts") or 0) < MAX_ATTEMPTS]),
        "needs_attention": pending_attention(state=data),
    }


__all__ = [
    "SCHEMA", "MAX_ATTEMPTS", "MAX_TOPUP_PER_RUN", "MAX_NEW_DRAFTS_PER_RUN",
    "PROTECTED_PUBLISH_STATES",
    "state_path", "load_state", "save_state", "note_attempt", "slug_blocked",
    "normalize_targets", "plan_prep", "prep_preview", "run_prep",
    "protected_slugs_now", "pending_attention", "clear_slug", "snapshot",
]
