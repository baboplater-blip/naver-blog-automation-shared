"""autonomy_overview — **전체 계정 운영 현황** (2026-09-19).

무인 운영은 계정마다 별도 프로세스로 돈다. 그래서 "지금 내 계정들이 어떤 상태인가"를
한 화면에서 보려면, **활성 프로필을 바꾸지 않고** 각 계정의 저장된 상태만 읽어야 한다.
이 모듈이 그 경계다.

**원칙**

- 브라우저·LLM·네트워크 호출 **0회**. 파일만 읽는다.
- 전역 활성 프로필(`profiles/.active`)을 **절대 바꾸지 않는다**. 프로필 이름을 받는
  경로(`autonomy_policy.load_policy(profile=)`·`kill_switch.status(profile=)`·
  `cost_ledger.remaining(profile=)`·`work_queue.list_items(profile=)`)만 쓴다.
- 계산할 수 없는 값은 `0` 이 아니라 **None** 이고, 화면에 "집계된 적 없음"으로
  나타난다. 가짜 '정상'·가짜 '다음 예약'을 만들지 않는다.
- 계정 ID 는 프로필 이름과 **마스킹된 값**만 싣는다(자격증명 값은 읽지 않는다).

재고(이미지까지 완성한 보관본 수)는 활성 계정만 **실시간**으로 센다 —
`draft_store` 는 임포트 시점에 활성 프로필 경로로 고정되기 때문이다. 다른 계정은
그 계정이 마지막으로 남긴 스냅샷(`logs/autonomy_state.json`)을 읽고, **언제 집계된
값인지**를 함께 보여 준다.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

from config.logger import get_logger

log = get_logger(__name__)

SCHEMA = "nba/autonomy_state@1"
_FILE_NAME = "autonomy_state.json"

#: 스냅샷이 이보다 오래되면 "오래된 값"으로 표시한다.
STALE_AFTER_HOURS = 26

#: 사람이 직접 확인해야 하는 발행 상태(자동 회복되지 않는다).
_MANUAL_STATES = ("unconfirmed", "missing", "reserving")


# ─────────────────────────────────────────────────────
# 계정별 상태 스냅샷
# ─────────────────────────────────────────────────────
def state_path(profile: Optional[str] = None) -> Path:
    from config import profile as _pf

    return _pf.logs_dir(profile) / _FILE_NAME


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".autonomy_state_", suffix=".tmp", dir=str(path.parent))
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


def load_account_state(profile: Optional[str] = None) -> dict:
    """저장된 계정 상태. 없으면 빈 dict(=집계된 적 없음)."""
    try:
        with state_path(profile).open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        log.warning(f"[autonomy_overview] 상태 파일을 읽지 못했습니다({type(exc).__name__})")
        return {}
    return data if isinstance(data, dict) else {}


def record_cycle(summary: Optional[dict] = None, *, profile: Optional[str] = None,
                 now: Optional[datetime] = None,
                 inventory: Optional[dict] = None) -> bool:
    """한 사이클(또는 준비 실행)이 끝난 뒤 현황을 남긴다 — 다른 계정 화면이 읽을 값.

    ⚠️ 실패해도 예외를 던지지 않는다. 기록 실패가 사이클 결과를 바꾸면 안 된다.
    """
    now = now or datetime.now()
    summary = summary or {}
    inv = inventory if inventory is not None else (summary.get("inventory") or {})
    payload = {
        "schema": SCHEMA,
        "updated_at": now.isoformat(timespec="seconds"),
        "profile": summary.get("profile") or (profile or ""),
        "last_run": {
            "at": summary.get("at") or now.isoformat(timespec="seconds"),
            "ok": bool(summary.get("ok", True)),
            "skipped": str(summary.get("skipped") or ""),
            "reason": str(summary.get("reason") or ""),
            "scope": str(summary.get("scope") or ""),
            "planned": len(summary.get("planned") or []),
            "processed": len(summary.get("processed") or []),
            "succeeded": sum(1 for p in (summary.get("processed") or [])
                             if (p or {}).get("ok")),
        },
        "inventory": {
            "ready_with_images": inv.get("ready_with_images"),
            "partial_images": inv.get("partial_images"),
            "no_plan": inv.get("no_plan"),
            "ready_total": inv.get("ready_total"),
            "target_days": inv.get("target_days"),
            "days_left": inv.get("days_left"),
            "at": now.isoformat(timespec="seconds"),
        },
        "prep": summary.get("prep") or {},
    }
    try:
        _atomic_write_json(state_path(profile), payload)
        return True
    except (OSError, TypeError, ValueError) as exc:
        log.warning(f"[autonomy_overview] 상태 기록 실패({type(exc).__name__})")
        return False


# ─────────────────────────────────────────────────────
# 다음 실행 시각 (순수 함수)
# ─────────────────────────────────────────────────────
def _parse_iso(raw) -> Optional[datetime]:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt


def _coerce_date(raw) -> Optional[date]:
    if not raw:
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    try:
        return date.fromisoformat(str(raw).strip())
    except (TypeError, ValueError):
        return None


def next_run_at(policy: dict, *, now: datetime, lookahead_days: int = 8) -> Optional[datetime]:
    """다음 준비 시각. 오늘 남은 슬롯이 없으면 **다음 날**로 넘어간다(자정 처리).

    정책에 `not_before`(첫 실행 날짜)가 있으면 그 날 이전은 건너뛴다 — 작업
    스케줄러 등록의 '첫 실행일 미루기'와 화면 표시가 어긋나지 않게 한다.
    설정된 준비 시각이 하나도 없거나 전부 잘못된 형식이면 **None**(= 표시하지
    않음)이다. 가짜 '다음 예약'을 만들지 않는다.
    """
    try:
        from core import autonomy_policy as _pol
    except Exception:  # noqa: BLE001
        return None
    floor = _coerce_date((policy or {}).get("not_before"))
    for offset in range(max(1, int(lookahead_days))):
        day = now.date() + timedelta(days=offset)
        if floor is not None and day < floor:
            continue
        try:
            slots = _pol.slots_for_day(policy or {}, day)
        except Exception:  # noqa: BLE001
            return None
        for slot in slots:
            if slot > now:
                return slot
    return None


# ─────────────────────────────────────────────────────
# 계정 1개 현황
# ─────────────────────────────────────────────────────
def distinct_accounts(profiles: Optional[list] = None) -> list:
    """**같은 데이터 폴더를 가리키는 계정 이름은 하나로 합친다.**

    이 저장소의 `default` 프로필은 `profiles/.default_data` 로 재배치될 수 있고,
    그게 named 프로필을 가리키면 `default` 와 그 이름은 **같은 블로그**다
    (실측: 이 설치에서 `default` → `examplebrand`). 둘을 따로 세면
    ① 현황 화면이 같은 계정을 두 번 보여 주고 ② 다계정 실행이 **같은 데이터에 두
    프로세스**를 띄운다. 이름이 있는 쪽을 남기고 `default` 를 접는다.

    경로를 확인할 수 없는 이름은 합치지 않고 그대로 둔다(조용히 빠뜨리지 않는다).
    """
    try:
        from config import profile as _pf
        names = list(profiles) if profiles is not None else list(_pf.list_profiles())
    except Exception as exc:  # noqa: BLE001
        log.warning(f"[autonomy_overview] 계정 목록 조회 실패({type(exc).__name__})")
        return sorted(set(profiles or []))

    keep_by_root: dict = {}
    for name in sorted(set(n for n in names if n)):
        try:
            key = str(_pf.app_data_root(name).resolve()).casefold()
        except Exception:  # noqa: BLE001 — 경로 확인 불가는 이름 자체를 키로(합치지 않음)
            key = f"\0name\0{name}"
        current = keep_by_root.get(key)
        if current is None or (current == "default" and name != "default"):
            keep_by_root[key] = name
    return sorted(keep_by_root.values())


def _same_root_name(name: str, candidates: list) -> str:
    """`name` 과 **같은 데이터 폴더**를 쓰는 후보 이름(없으면 "")."""
    try:
        from config import profile as _pf
        key = str(_pf.app_data_root(name).resolve()).casefold()
        for other in candidates:
            if str(_pf.app_data_root(other).resolve()).casefold() == key:
                return other
    except Exception:  # noqa: BLE001
        pass
    return ""


def _policy_for(name: str) -> tuple:
    try:
        from core import autonomy_policy as _pol
        return _pol.load_policy(name), ""
    except Exception as exc:  # noqa: BLE001
        return None, f"설정을 읽지 못했습니다({type(exc).__name__})"


def _masked_id(name: str) -> str:
    try:
        from config import profile as _pf
        info = _pf.describe(name) or {}
        return str(info.get("naver_id_masked") or "")
    except Exception:  # noqa: BLE001
        return ""


def _queue_counts(name: str, today: str) -> tuple:
    """(오늘 상태별 건수, 수동 확인 필요 건수, 오류문구)."""
    try:
        from core import work_queue as _wq
        counts: dict = {}
        for item in _wq.list_items(day=today, profile=name):
            key = (item or {}).get("publish_state") or "none"
            counts[key] = counts.get(key, 0) + 1
        manual = len(_wq.list_items(states=_MANUAL_STATES, profile=name))
        return counts, manual, ""
    except Exception as exc:  # noqa: BLE001
        return {}, None, f"작업큐를 읽지 못했습니다({type(exc).__name__})"


def _budget_for(name: str) -> dict:
    out = {"write_left": None, "image_left": None}
    try:
        from core import cost_ledger as _cl
    except Exception:  # noqa: BLE001
        return out
    for kind, key in (("write", "write_left"), ("image", "image_left")):
        try:
            remaining = _cl.remaining(kind, profile=name) or {}
        except Exception:  # noqa: BLE001
            continue
        lefts = [v for v in (remaining.get("profile_left"), remaining.get("global_left"))
                 if v is not None]
        if lefts:
            out[key] = max(0, min(lefts))
    return out


def _stats_hold(name: str) -> dict:
    """성과 수집 보류 상태 — 저장된 증거 파일만 읽는다(네트워크 0회)."""
    out = {"proven": None, "reason": "", "checked_at": ""}
    try:
        from config import profile as _pf
        path = _pf.logs_dir(name) / "stats_capability.json"
        with path.open("r", encoding="utf-8") as fh:
            cap = json.load(fh)
    except FileNotFoundError:
        out["reason"] = "아직 확인한 적 없음"
        return out
    except (OSError, json.JSONDecodeError) as exc:
        out["reason"] = f"확인 기록을 읽지 못함({type(exc).__name__})"
        return out
    if not isinstance(cap, dict):
        out["reason"] = "확인 기록 형식이 올바르지 않음"
        return out
    out["checked_at"] = str(cap.get("checked_at") or cap.get("at") or "")
    failed = [str((c or {}).get("name") or "?")
              for c in (cap.get("checks") or [])
              if (c or {}).get("result") not in (True, "ok", "pass", "success")]
    out["proven"] = bool(cap.get("ok")) and not failed
    if failed:
        out["reason"] = "자동 수집 미완: " + ", ".join(failed[:3])
    elif not out["proven"]:
        out["reason"] = "자동 수집이 아직 증명되지 않음"
    return out


def _inventory_block(name: str, *, is_active: bool, policy: dict,
                     now: datetime, live_fn: Optional[Callable]) -> dict:
    """재고 현황. 활성 계정만 실시간, 나머지는 저장된 스냅샷 + 집계 시각."""
    block = {"ready": None, "partial": None, "no_plan": None,
             "source": "unknown", "as_of": "", "stale": False,
             "note": "아직 집계된 적이 없습니다 — 그 계정의 무인 실행이 한 번 돌면 채워집니다."}
    if is_active:
        fn = live_fn
        if fn is None:
            try:
                from core import autonomy_runner as _ar
                fn = lambda: _ar.inventory_status(policy=policy)  # noqa: E731
            except Exception:  # noqa: BLE001
                fn = None
        if fn is not None:
            try:
                inv = fn() or {}
                return {"ready": inv.get("ready_with_images"),
                        "partial": inv.get("partial_images"),
                        "no_plan": inv.get("no_plan"),
                        "source": "live", "as_of": now.isoformat(timespec="seconds"),
                        "stale": False, "note": inv.get("warning") or ""}
            except Exception as exc:  # noqa: BLE001
                block["note"] = f"재고를 세지 못했습니다({type(exc).__name__})"
                return block

    snap = (load_account_state(name) or {}).get("inventory") or {}
    if snap.get("ready_with_images") is None and not snap.get("at"):
        return block
    at = _parse_iso(snap.get("at"))
    block.update(ready=snap.get("ready_with_images"),
                 partial=snap.get("partial_images"),
                 no_plan=snap.get("no_plan"),
                 source="snapshot", as_of=str(snap.get("at") or ""),
                 stale=bool(at is None or (now - at) > timedelta(hours=STALE_AFTER_HOURS)),
                 note="")
    if block["stale"]:
        block["note"] = "마지막 집계가 오래됐습니다 — 표시된 시각 기준 값입니다."
    return block


def account_overview(name: str, *, now: Optional[datetime] = None,
                     active_profile: str = "", master_on: bool = False,
                     estop: bool = False,
                     live_inventory_fn: Optional[Callable] = None) -> dict:
    """계정 하나의 현황(읽기 전용). 오류는 그 계정 항목 안에만 담는다."""
    now = now or datetime.now()
    today = now.date().strftime("%Y-%m-%d")
    row = {
        "profile": name, "masked_id": _masked_id(name),
        "is_active_profile": name == active_profile,
        "enabled": False, "scope": "", "paused": False,
        "eligible": False, "block_reason": "",
        "next_run": "", "next_run_note": "",
        "inventory": {}, "inventory_target": None, "inventory_deficit": None,
        "daily_posts": None, "today": {}, "manual_checks": None,
        "budget": {}, "stats": {}, "last_run": {}, "prep": {},
        "user_action": "", "auto_recovers": "",
        "errors": [],
    }

    policy, err = _policy_for(name)
    if policy is None:
        row["errors"].append(err)
        row["block_reason"] = "policy_error"
        row["user_action"] = "이 계정의 무인 운영 설정이 손상됐습니다 — 무인 운영 탭에서 다시 저장하세요."
        return row

    row["enabled"] = bool(policy.get("enabled"))
    row["scope"] = str(policy.get("scope") or "")
    row["daily_posts"] = policy.get("daily_posts")

    try:
        from core import kill_switch as _ks
        st = _ks.status(profile=name) or {}
        row["paused"] = bool(st.get("pause"))
    except Exception as exc:  # noqa: BLE001
        row["errors"].append(f"정지 상태를 읽지 못했습니다({type(exc).__name__})")
        row["paused"] = bool(policy.get("pause"))

    if estop:
        row["block_reason"] = "estop"
    elif not master_on:
        row["block_reason"] = "master_off"
    elif not row["enabled"]:
        row["block_reason"] = "account_off"
    elif row["paused"]:
        row["block_reason"] = "pause"
    row["eligible"] = not row["block_reason"]

    # 다음 실행 — 켜져 있고 막히지 않았을 때만 표시한다(가짜 '다음 예약' 금지).
    if row["eligible"]:
        nxt = next_run_at(policy, now=now)
        if nxt is None:
            row["next_run_note"] = "예약 준비 시각이 설정되지 않았습니다."
        else:
            row["next_run"] = nxt.isoformat(timespec="seconds")
    else:
        row["next_run_note"] = {
            "estop": "전체 비상정지 중이라 예정된 실행이 없습니다.",
            "master_off": "무인 운영 마스터가 꺼져 있습니다.",
            "account_off": "이 계정의 무인 운영이 꺼져 있습니다.",
            "pause": "이 계정이 일시중지 상태입니다.",
            "policy_error": "설정을 읽지 못했습니다.",
        }.get(row["block_reason"], "")

    row["inventory"] = _inventory_block(
        name, is_active=row["is_active_profile"], policy=policy, now=now,
        live_fn=live_inventory_fn)
    try:
        from core import autonomy_prep as _prep
        targets = _prep.normalize_targets(policy)
        row["inventory_target"] = targets["target_inventory"]
        ready = row["inventory"].get("ready")
        row["inventory_deficit"] = (None if ready is None
                                    else max(0, targets["target_inventory"] - int(ready)))
        row["prep"] = _prep.snapshot(profile=name)
    except Exception as exc:  # noqa: BLE001
        row["errors"].append(f"준비 상태를 읽지 못했습니다({type(exc).__name__})")

    counts, manual, qerr = _queue_counts(name, today)
    row["today"] = counts
    row["manual_checks"] = manual
    if qerr:
        row["errors"].append(qerr)

    row["budget"] = _budget_for(name)
    row["stats"] = _stats_hold(name)
    row["last_run"] = (load_account_state(name) or {}).get("last_run") or {}

    # ── 사람이 할 일 / 저절로 회복되는 일 구분 ──────────────────────
    if row["block_reason"] == "policy_error":
        pass  # 위에서 이미 채웠다
    elif manual:
        row["user_action"] = (
            f"네이버에서 직접 확인이 필요한 글 {manual}편이 있습니다 "
            "(무인 운영 탭 '문제 해결' 에서 결말을 닫아 주세요).")
    elif row["block_reason"] == "master_off":
        row["user_action"] = "무인 운영 탭에서 '무인 운영 켜기'를 눌러 주세요."
    elif row["block_reason"] == "account_off":
        row["user_action"] = "이 계정의 운영 범위를 '보관만' 또는 '예약 발행'으로 골라 주세요."
    elif row["block_reason"] == "pause":
        row["user_action"] = "일시중지를 해제하면 다음 준비 시각부터 다시 돕니다."
    elif row["block_reason"] == "estop":
        row["user_action"] = "전체 비상정지를 해제해야 모든 계정이 다시 돕니다."

    needs = (row.get("prep") or {}).get("needs_attention") or []
    if needs and not row["user_action"]:
        row["user_action"] = (
            f"여러 번 실패해 자동 재시도를 멈춘 글 {len(needs)}편이 있습니다 — 내용을 확인해 주세요.")

    if row["eligible"]:
        bits = []
        if (row["inventory_deficit"] or 0) > 0:
            bits.append("재고가 모자라면 다음 준비 시각에 빠진 이미지부터 자동으로 채웁니다.")
        waiting = (row.get("prep") or {}).get("waiting") or 0
        if waiting:
            bits.append(f"실패 후 대기 중인 글 {waiting}편은 대기 시간이 지나면 자동으로 다시 시도합니다.")
        row["auto_recovers"] = " ".join(bits)
    return row


# ─────────────────────────────────────────────────────
# 전체
# ─────────────────────────────────────────────────────
def overview(*, now: Optional[datetime] = None, profiles: Optional[list] = None,
             live_inventory_fn: Optional[Callable] = None) -> dict:
    """모든 계정의 현황. 네트워크·브라우저·LLM 호출 **0회**, 활성 프로필 변경 **0회**."""
    now = now or datetime.now()
    out = {"at": now.isoformat(timespec="seconds"), "master": False, "estop": False,
           "active_profile": "", "accounts": [], "totals": {}, "errors": []}

    try:
        from config import profile as _pf
        out["active_profile"] = _pf.active_profile()
    except Exception as exc:  # noqa: BLE001
        out["errors"].append(f"활성 계정을 읽지 못했습니다({type(exc).__name__})")
    # 같은 데이터 폴더를 가리키는 이름은 하나로 — 같은 블로그가 두 줄로 보이지 않게.
    names = distinct_accounts(profiles)
    # 활성 계정이 접힌 쪽 이름이면(예: active="default" → 남은 이름 "examplebrand"),
    # 그 남은 이름을 활성으로 본다 — 안 그러면 실시간 재고를 아무도 못 쓴다.
    if out["active_profile"] and out["active_profile"] not in names:
        folded = _same_root_name(out["active_profile"], names)
        if folded:
            out["active_profile"] = folded

    try:
        from core import autonomy_guard as _guard
        out["master"] = bool(_guard.master_enabled())
    except Exception as exc:  # noqa: BLE001
        out["errors"].append(f"마스터 스위치를 읽지 못했습니다({type(exc).__name__})")
    try:
        from core import kill_switch as _ks
        out["estop"] = bool(_ks.estop_active())
    except Exception as exc:  # noqa: BLE001
        out["errors"].append(f"비상정지 상태를 읽지 못했습니다({type(exc).__name__})")

    for name in names:
        try:
            out["accounts"].append(account_overview(
                name, now=now, active_profile=out["active_profile"],
                master_on=out["master"], estop=out["estop"],
                live_inventory_fn=live_inventory_fn))
        except Exception as exc:  # noqa: BLE001 — 한 계정 실패가 나머지를 가리지 않는다
            out["accounts"].append({"profile": name, "errors": [
                f"현황을 읽지 못했습니다({type(exc).__name__})"], "eligible": False,
                "block_reason": "overview_error"})

    accounts = out["accounts"]
    ready_values = [a.get("inventory", {}).get("ready") for a in accounts]
    known_ready = [v for v in ready_values if isinstance(v, int)]
    out["totals"] = {
        "accounts": len(accounts),
        "enabled": sum(1 for a in accounts if a.get("enabled")),
        "eligible": sum(1 for a in accounts if a.get("eligible")),
        "paused": sum(1 for a in accounts if a.get("paused")),
        "manual_checks": sum(int(a.get("manual_checks") or 0) for a in accounts),
        "ready_known": sum(known_ready) if known_ready else None,
        "ready_unknown_accounts": len(ready_values) - len(known_ready),
        "needs_user_action": sum(1 for a in accounts if a.get("user_action")),
    }
    return out


__all__ = [
    "SCHEMA", "STALE_AFTER_HOURS", "state_path", "load_account_state",
    "record_cycle", "next_run_at", "account_overview", "overview",
    "distinct_accounts",
]
