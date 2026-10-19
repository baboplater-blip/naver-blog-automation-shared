# core/autonomy_policy.py — 무인 운영 계정 정책 저장소 (M1, 2026-09-18)
"""계정(프로필)별 무인 운영 설정을 `autonomy.json` 에 저장·로드한다.

⚠️ 신규 기능은 기본 OFF다. `settings.AUTONOMY_ENABLED` 마스터 스위치와
계정 정책 파일의 `enabled` 가 **둘 다** 참이어야 `is_active()` 가 True 를
돌려준다 — 어느 한쪽만 켜져 있으면 무인 운영은 절대 작동하지 않는다.

경로는 항상 `config.profile.app_data_root()` 를 함수 호출 시점에 읽는다
(모듈 상수로 굳히지 않는다 — 테스트가 monkeypatch 로 격리할 수 있어야 한다).

파일 쓰기는 `core/draft_store.py:_atomic_write_json` 과 같은 tempfile→os.replace
패턴을 이 모듈 안에 자체 구현한다(draft_store 를 import 하지 않는다 — 순환·부작용 회피).
"""
from __future__ import annotations

import copy
import json
import os
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from config.logger import get_logger

log = get_logger(__name__)

SCHEMA = "nba/autonomy@1"

DEFAULTS: dict = {
    "schema": SCHEMA,
    "enabled": False,
    "scope": "draft_only",
    "activated_at": None,
    "activated_by": "",
    "topic": {"categories": [], "exclude_keywords": []},
    "audience": "",
    "persona_preset": "balanced",
    "style_notes": "",
    "image_style": {"thumb_label_max": 16, "body_text_free": True},
    "daily_posts": 2,
    "windows": ["07:30", "19:00"],
    "publish_lead_minutes": 60,
    "inventory_days": 5,
    "budget": {"write_calls": 8, "image_calls": 12},
    "catchup_max": 2,
    "pause": False,
    "pause_reason": "",
}

VALID_SCOPES = ("draft_only", "publish")

_FILE_NAME = "autonomy.json"
_ROUND_MINUTES = 5


# ─────────────────────────────────────────────────────
# 경로 · 원자 쓰기
# ─────────────────────────────────────────────────────
def policy_path(profile: Optional[str] = None) -> Path:
    """계정 정책 파일 경로. 호출 시점에 `config.profile` 을 읽는다."""
    from config import profile as _profile

    return _profile.app_data_root(profile) / _FILE_NAME


def _atomic_write_json(path: Path, data: dict) -> None:
    """tempfile → os.replace 패턴. 부분 쓰기로 깨진 JSON 을 남기지 않는다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".autonomy_", suffix=".tmp", dir=str(path.parent))
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
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        log.warning(f"[autonomy_policy] read failed {path.name}: {exc}")
        return None


def _is_sane(loaded: dict) -> bool:
    """구조적으로 신뢰 가능한 정책 문서인지 — 스키마·scope 검사."""
    if not isinstance(loaded, dict):
        return False
    if loaded.get("schema") != SCHEMA:
        return False
    scope = loaded.get("scope", DEFAULTS["scope"])
    if scope not in VALID_SCOPES:
        return False
    return True


def _deep_merge_defaults(loaded: dict) -> dict:
    """DEFAULTS 사본에 loaded 를 덧씌우되, 알 수 없는(신규) 키는 보존한다."""
    merged = copy.deepcopy(DEFAULTS)
    for key, value in loaded.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return merged


# ─────────────────────────────────────────────────────
# 로드 · 저장
# ─────────────────────────────────────────────────────
def load_policy(profile: Optional[str] = None) -> dict:
    """없으면 DEFAULTS 사본(enabled=False). 손상/알 수 없는 스키마면 DEFAULTS + 경고 로그.
    구조적으로 신뢰 가능하면 알 수 없는 키를 보존한 채 DEFAULTS 위에 병합한다.
    """
    path = policy_path(profile)
    loaded = _read_json(path)
    if loaded is None:
        return copy.deepcopy(DEFAULTS)
    if not _is_sane(loaded):
        log.warning(f"[autonomy_policy] 손상되었거나 알 수 없는 스키마 — 기본값 사용: {path}")
        return copy.deepcopy(DEFAULTS)
    return _deep_merge_defaults(loaded)


def save_policy(policy: dict, *, profile: Optional[str] = None) -> Path:
    """원자 쓰기. 검증 실패(scope 오류 등)면 ValueError."""
    if not isinstance(policy, dict):
        raise ValueError("policy 는 dict 여야 합니다")
    scope = policy.get("scope", DEFAULTS["scope"])
    if scope not in VALID_SCOPES:
        raise ValueError(f"알 수 없는 scope: {scope!r} (허용: {VALID_SCOPES})")
    to_write = dict(policy)
    to_write.setdefault("schema", SCHEMA)
    path = policy_path(profile)
    _atomic_write_json(path, to_write)
    return path


# ─────────────────────────────────────────────────────
# 활성화 · 정지
# ─────────────────────────────────────────────────────
def activate(*, scope: str, by: str = "", profile: Optional[str] = None, **overrides) -> dict:
    """enabled=True + scope 확정 + activated_at 기록 후 저장 + 마스터 스위치 켜기.

    ⚠️ 사용자가 이미 설정한 daily_posts/windows/topic/persona 등 기존 운영값은
    `overrides` 로 명시 전달된 키만 바뀐다 — 조용히 덮지 않는다.

    계정 정책 저장과는 별개로 무인 운영 **마스터 스위치**(머신 공용 파일)도 켠다 —
    이게 없으면 GUI 화면엔 "켜짐"인데 실제로는 재시작·CLI 어디서도 돌지 않는
    상태가 된다(2026-09-18 마감 결함 A). 계정 정책 저장 자체는 항상 성공한
    상태로 반환하되, 마스터 스위치 기록에 실패하면 반환 dict 에 `master_ok=False`
    + `master_error` 를 실어 호출자(GUI)가 알 수 있게 한다 — 여기서 새 예외를
    던지지는 않는다(그러면 정책은 저장됐는데 GUI 가 죽어버린다).
    """
    if scope not in VALID_SCOPES:
        raise ValueError(f"알 수 없는 scope: {scope!r} (허용: {VALID_SCOPES})")

    policy = load_policy(profile=profile)
    policy["enabled"] = True
    policy["scope"] = scope
    policy["activated_at"] = datetime.now().isoformat(timespec="seconds")
    policy["activated_by"] = by

    for key, value in overrides.items():
        if key in policy and isinstance(policy[key], dict) and isinstance(value, dict):
            policy[key] = {**policy[key], **value}
        else:
            policy[key] = value

    save_policy(policy, profile=profile)

    master_ok, master_error = True, ""
    try:
        from core import autonomy_guard as _guard

        if not _guard.set_master(True, by=by or "gui"):
            master_ok = False
            master_error = "마스터 스위치 파일을 쓰지 못했습니다."
    except Exception as exc:  # noqa: BLE001 — guard 모듈 부재도 실패로 정직하게 보고
        master_ok = False
        master_error = f"마스터 스위치 모듈을 불러오지 못했습니다({type(exc).__name__})"
    if not master_ok:
        log.warning(f"[autonomy_policy] 마스터 스위치를 켜지 못했습니다: {master_error}")

    result = dict(policy)
    result["master_ok"] = master_ok
    if master_error:
        result["master_error"] = master_error
    return result


def deactivate(*, profile: Optional[str] = None) -> dict:
    """enabled=False 로 저장하고, **다른 어떤 계정도 켜져 있지 않으면** 마스터도 끈다.

    프로필 열거(`config.profile.list_profiles`)가 실패하면 마스터는 그대로 둔다
    (이 계정 하나는 이미 꺼졌으므로 안전 — 잘못 끄는 것보다 낫다).
    """
    policy = load_policy(profile=profile)
    policy["enabled"] = False
    save_policy(policy, profile=profile)

    master_ok = True
    try:
        from config import profile as _profile_mod

        names = _profile_mod.list_profiles()
    except Exception as exc:  # noqa: BLE001
        names = None
        log.info(f"[autonomy_policy] 프로필 목록 열거 실패 — 마스터 스위치 유지"
                 f"({type(exc).__name__})")

    if names is not None:
        any_other_enabled = False
        for name in names:
            try:
                if load_policy(profile=name).get("enabled"):
                    any_other_enabled = True
                    break
            except Exception:  # noqa: BLE001 — 개별 계정 조회 실패는 건너뛴다
                continue
        if not any_other_enabled:
            try:
                from core import autonomy_guard as _guard

                master_ok = _guard.set_master(False, by="gui")
            except Exception as exc:  # noqa: BLE001
                master_ok = False
                log.warning(f"[autonomy_policy] 마스터 스위치를 끄지 못했습니다: "
                           f"{type(exc).__name__}")

    result = dict(policy)
    result["master_ok"] = master_ok
    return result


def set_pause(paused: bool, *, reason: str = "", profile: Optional[str] = None) -> dict:
    policy = load_policy(profile=profile)
    policy["pause"] = bool(paused)
    policy["pause_reason"] = reason
    save_policy(policy, profile=profile)
    return policy


def is_active(policy: Optional[dict] = None, *, profile: Optional[str] = None) -> bool:
    """무인 운영 마스터 AND policy['enabled'] 둘 다 참일 때만 True.

    마스터는 `core.autonomy_guard.master_enabled()`(머신 공용 파일 — GUI 가 계정을
    켜면 여기 기록된다) **또는** 레거시 `settings.AUTONOMY_ENABLED=True` 다. 예전에는
    후자만 봤는데, 그러면 GUI 로 계정을 켜도 사용자가 손으로 `config/settings.py` 를
    고치기 전까지 재시작·CLI 어디서도 돌지 않는 결함이 있었다(2026-09-18 마감 결함 A).
    `autonomy_guard` 모듈 자체를 못 불러오면 레거시 settings 값만으로 판정한다
    (둘 다 못 읽으면 기본값은 여전히 **꺼짐**).
    """
    try:
        from core import autonomy_guard as _guard

        master_on = _guard.master_enabled()
    except Exception:  # noqa: BLE001 — guard 모듈 미로드 환경이면 레거시 경로로 폴백
        try:
            from config import settings as _settings

            master_on = bool(getattr(_settings, "AUTONOMY_ENABLED", False))
        except ImportError:
            master_on = False
    if not master_on:
        return False
    if policy is None:
        policy = load_policy(profile=profile)
    return bool(policy.get("enabled", False))


# ─────────────────────────────────────────────────────
# 슬롯 · 예약 시각
# ─────────────────────────────────────────────────────
def slots_for_day(policy: dict, day: date) -> list:
    """policy['windows'] 를 그날의 예약 준비 시각 datetime 리스트로. 잘못된 문자열은 건너뛴다."""
    result = []
    for raw in policy.get("windows", []) or []:
        try:
            hh, mm = str(raw).strip().split(":", 1)
            t_hour, t_minute = int(hh), int(mm)
            result.append(datetime(day.year, day.month, day.day, t_hour, t_minute))
        except (ValueError, TypeError):
            log.warning(f"[autonomy_policy] 잘못된 windows 항목 건너뜀: {raw!r}")
            continue
    result.sort()
    return result


def _round_up_5min(dt: datetime) -> datetime:
    """다음(또는 현재, 이미 정렬돼 있으면) 5분 경계로 올림."""
    aligned = dt.replace(second=0, microsecond=0)
    if dt.second == 0 and dt.microsecond == 0 and aligned.minute % _ROUND_MINUTES == 0:
        return aligned
    remainder = aligned.minute % _ROUND_MINUTES
    add_minutes = _ROUND_MINUTES - remainder
    return aligned + timedelta(minutes=add_minutes)


def publish_at_for(slot: datetime, policy: dict, *, now: datetime) -> Optional[datetime]:
    """공개 예약 시각 = slot + publish_lead_minutes.
    그 값이 now 보다 과거면 다음 정각(now+lead 이후의 가장 가까운 5분 경계)으로 미룬다.
    미룰 수 없으면 None. **과거 시각을 절대 반환하지 않는다.**
    """
    try:
        lead_minutes = int(policy.get("publish_lead_minutes", 0))
    except (TypeError, ValueError):
        log.warning("[autonomy_policy] publish_lead_minutes 값이 올바르지 않음 — None 반환")
        return None

    try:
        candidate = slot + timedelta(minutes=lead_minutes)
    except (OverflowError, TypeError):
        return None

    if candidate >= now:
        return candidate

    try:
        base = now + timedelta(minutes=lead_minutes)
        fallback = _round_up_5min(base)
    except (OverflowError, TypeError):
        return None

    # 방어적 상한 — 비정상 입력으로 인한 무한 루프를 막는다.
    guard = 0
    while fallback < now and guard < 1000:
        fallback += timedelta(minutes=_ROUND_MINUTES)
        guard += 1
    if fallback < now:
        return None
    return fallback


__all__ = [
    "SCHEMA",
    "DEFAULTS",
    "VALID_SCOPES",
    "policy_path",
    "load_policy",
    "save_policy",
    "activate",
    "deactivate",
    "set_pause",
    "is_active",
    "slots_for_day",
    "publish_at_for",
]
