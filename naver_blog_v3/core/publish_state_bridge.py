"""publish_state_bridge — 발행 파이프라인과 무인 운영 상태 사이의 **단일 접점** (2026-09-18).

`scheduler/auto_scheduler.py` 는 3,300줄짜리 라이브 발행 경로다. 거기에 큐·정지 스위치·
상태 기록을 직접 박으면 (a) 그 파일이 무인 운영 모듈에 강하게 묶이고 (b) 무인 운영을
끄는 순간 죽은 코드가 곳곳에 남는다. 그래서 **얇은 브리지 하나**만 심고, 모든 판단을
여기로 모은다. 브리지가 없거나 깨져도 발행 파이프라인은 예전 그대로 돈다(fail-open).

세 가지 일을 한다.

1. **write-ahead 기록** — 발행 버튼을 누르기 *전에* `reserving` 을 디스크에 남긴다.
   프로세스가 죽어도 "이 글이 어디까지 갔는지"가 남아, 재시작 후 대조의 입력이 된다.
2. **정지 스위치 확인** — 글 경계(`guard_new_item`)와 외부 확정 행동 직전
   (`guard_external_action`). estop/pause 플래그는 **파일**이라 GUI 가 떠 있지 않아도 듣는다.
3. **중복 발행 차단** — 결말이 미상인 제목(`reserving`/`unconfirmed`/`missing`)은
   다음 실행에서 다시 집히지 않게 `blocked_reason()` 이 사유를 돌려준다.

⚠️ 불변식: 이 모듈은 **절대 발행하지 않고, `reserved` 를 증거 없이 적지 않는다.**
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from config.logger import get_logger

log = get_logger(__name__)

# 실행 중인 무인 운영 항목 컨텍스트. 러너(autonomy_runner)가 항목 하나를 시작할 때
# 설정하고 끝나면 지운다. 스레드별로 갖는다 — GUI 가 워커 스레드에서 파이프라인을
# 돌리는 동안 다른 스레드의 컨텍스트와 섞이지 않게.
_CTX = threading.local()


# ─────────────────────────────────────────────────────
# 컨텍스트
# ─────────────────────────────────────────────────────

def set_context(*, item_id: str = "", publish_at: str = "",
                scope: str = "", profile: str = "") -> None:
    """무인 운영 항목 하나의 실행 컨텍스트를 연다(러너 전용)."""
    _CTX.item_id = item_id or ""
    _CTX.publish_at = publish_at or ""
    _CTX.scope = scope or ""
    _CTX.profile = profile or ""


def clear_context() -> None:
    for attr in ("item_id", "publish_at", "scope", "profile"):
        if hasattr(_CTX, attr):
            delattr(_CTX, attr)


def context() -> dict:
    return {
        "item_id": getattr(_CTX, "item_id", ""),
        "publish_at": getattr(_CTX, "publish_at", ""),
        "scope": getattr(_CTX, "scope", ""),
        "profile": getattr(_CTX, "profile", ""),
    }


def autonomy_enabled() -> bool:
    """무인 운영 마스터(머신 공용 파일·레거시 settings) 또는 필수 보호 세션 안인가.

    비용 원장·브라우저 중재와 같은 이유로, 발행 상태 write-ahead 기록도 선택
    기능이 아니라 무인 운영의 필수 보호다(2026-09-18 마감 결함 B). 함수 이름은
    호출부(`recording()`) 호환을 위해 그대로 두고 내부 판정만 바꾼다.
    """
    try:
        from core import autonomy_guard as _guard
        return bool(_guard.master_enabled() or _guard.guards_active())
    except Exception:  # noqa: BLE001 — guard 모듈 미로드 환경이면 레거시 settings 로 폴백
        try:
            from config import settings as _s
            return bool(getattr(_s, "AUTONOMY_ENABLED", False))
        except Exception:  # noqa: BLE001 — settings 미로드면 기존 동작
            return False


def recording() -> bool:
    """지금 상태를 기록해야 하는가 = 마스터 ON + 러너가 연 컨텍스트 있음."""
    return bool(autonomy_enabled() and context()["item_id"])


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ─────────────────────────────────────────────────────
# 1) write-ahead 상태 기록
# ─────────────────────────────────────────────────────

def mark(publish_state: str, *, slug: str = "", title: str = "",
         **fields) -> bool:
    """발행 진행 상태를 **행동 전에** 기록한다(write-ahead).

    두 곳에 적는다 — 작업큐(항목 단위)와 `_status.json`(보관본 단위). 둘 중 하나가
    없어도 나머지는 기록된다. 어느 쪽 실패도 발행을 막지 않는다(fail-open).

    Returns:
        한 곳이라도 기록됐으면 True.
    """
    wrote = False
    ctx = context()
    payload = {k: v for k, v in fields.items() if v is not None}
    payload.setdefault("updated_at", _now_iso())

    if autonomy_enabled() and ctx["item_id"]:
        try:
            from core import work_queue as _wq
            _wq.set_state(ctx["item_id"], publish_state,
                          title=title or None, slug=slug or None, **payload)
            wrote = True
        except Exception as exc:  # noqa: BLE001 — 큐 기록 실패가 발행을 막지 않는다
            log.warning(f"[bridge] work_queue 기록 실패({type(exc).__name__}) — {publish_state}")

    if slug:
        try:
            from core import draft_store as _ds
            if _ds.set_publish_state(slug, publish_state, **payload):
                wrote = True
        except Exception as exc:  # noqa: BLE001
            log.warning(f"[bridge] _status.json 기록 실패({type(exc).__name__}) — {slug}")
    return wrote


def classify_upload_result(result) -> tuple:
    """`upload_post_ex` 결과를 (publish_state, reason) 으로 번역한다.

    설계 §2.2 그대로:
      - 성공 + **검증된 예약 증거** → reserved
      - 성공(증거 없음)            → unconfirmed  ← 허위 성공 금지
      - 실패 + 발행 버튼 누른 뒤   → unconfirmed  ← 재발행 금지, 사람 확인
      - 실패 + 발행 버튼 누른 뒤   → unconfirmed  ← 재발행 금지, 사람 확인
      - 실패 + 버튼 누르기 전      → failed

    **bool 결과(구 계약)**: 무인 운영을 켜지 않은 기존 경로는 `upload_post` 의 bool 만
    받는다. 그 True 는 앱이 예전부터 써 온 **fail-closed 성공 판정**
    (`_verify_reservation_success` — 에러 토스트 없음 + 에디터 이탈/성공 메시지)의
    결과다. 이것을 `unconfirmed` 로 적으면 **정상 발행된 글이 전부 수동 확인 큐에
    쌓여** 큐가 무의미해진다. 그래서 `reserved`(등록됨, 공개 확인은 아직)로 적는다 —
    `live` 로 올리지는 않으므로 "공개 확인됨"을 참칭하지 않고, 재발행 대상도 아니다.
    """
    if isinstance(result, bool):
        return (("reserved", "legacy_verified_success") if result
                else ("failed", "legacy_bool_failure"))
    if not isinstance(result, dict):
        return "failed", "unknown_result"

    ok = bool(result.get("ok"))
    phase = str(result.get("phase") or "")
    signal = str(result.get("signal") or "none")
    evidence = result.get("reservation_evidence")
    if ok:
        if isinstance(evidence, dict) and evidence:
            return "reserved", f"evidence:{signal}"
        return "unconfirmed", f"no_evidence:{signal}"
    if phase == "post_submit":
        return "unconfirmed", f"post_submit_no_signal:{signal}"
    return "failed", f"pre_submit:{signal or 'none'}"


# ─────────────────────────────────────────────────────
# 2) 정지 스위치
# ─────────────────────────────────────────────────────

def guard_new_item(*, profile: Optional[str] = None) -> str:
    """글 경계에서 호출. 계속 가능하면 "", 아니면 "estop" | "pause".

    ⚠️ 무인 운영 마스터 스위치와 **무관하게** 동작한다 — 비상정지 파일은 사용자가
    직접 만든 명시적 의사표시이므로, 수동 실행에서도 듣는 것이 맞다.
    `kill_switch` 모듈 자체를 못 불러오면 차단하지 않는다(모듈 부재로 기존 사용자의
    발행을 막는 것이 더 큰 사고다).
    """
    try:
        from core import kill_switch as _ks
    except Exception:  # noqa: BLE001 — 모듈 부재 = 기존 동작
        return ""
    try:
        return _ks.check_before_new_item(profile=profile) or ""
    except Exception as exc:  # noqa: BLE001
        log.warning(f"[bridge] 정지 스위치 확인 실패(무시): {type(exc).__name__}")
        return ""


def guard_external_action(*, profile: Optional[str] = None) -> str:
    """**다음 외부 확정 행동(예약 등록·발행 제출) 직전**에 호출.

    비상정지면 "estop" 을 돌려준다(호출자는 제출하지 않고 멈춘다). soft pause 는
    여기서 막지 않는다 — 진행 중인 글은 끝까지 간다.
    이미 제출 버튼이 실행 중인 원자 구간 안에서는 호출하지 않는다(중간 정지가
    곧 `unconfirmed` 를 만든다).
    """
    try:
        from core import kill_switch as _ks
    except Exception:  # noqa: BLE001
        return ""
    try:
        _ks.check_before_external_action(profile=profile)
        return ""
    except Exception as exc:  # noqa: BLE001 — Stopped 포함
        if type(exc).__name__ == "Stopped":
            return "estop"
        log.warning(f"[bridge] 외부 행동 확인 실패(무시): {type(exc).__name__}")
        return ""


# ─────────────────────────────────────────────────────
# 3) 중복 발행 차단
# ─────────────────────────────────────────────────────

_OPEN_STATES = ("reserving", "unconfirmed", "missing")


def blocked_reason(title: str = "", slug: str = "") -> str:
    """이 글을 **다시 발행 시도하면 안 되는** 사유. 없으면 "".

    결말이 미상인 글(`reserving`/`unconfirmed`/`missing`)을 다음 실행이 다시 집으면
    그게 곧 중복 발행이다. 엑셀 셀은 성공했을 때만 완료 표시되므로, 미확정으로 끝난
    제목은 다음 회차에 그대로 후보로 돌아온다 — 그 경로를 여기서 끊는다.

    큐·보관본 기록이 없으면 "" (무인 운영을 쓰지 않는 설치는 영향 0).
    """
    if slug:
        try:
            from core import draft_store as _ds
            state = _ds.publish_state_of(slug)
            if state in _OPEN_STATES:
                return f"publish_state={state}"
        except Exception:  # noqa: BLE001
            pass
    if not title:
        return ""
    try:
        from core import work_queue as _wq
        for item in _wq.list_items(states=_OPEN_STATES):
            if (item.get("title") or "").strip() == title.strip():
                return f"publish_state={item.get('publish_state')}"
    except Exception:  # noqa: BLE001 — 큐 부재/손상은 차단하지 않는다
        return ""
    return ""


# ─────────────────────────────────────────────────────
# 4) 단건(사용자 클릭) 발행의 상태 원장 — 사진·상품 경로 (2026-09-23)
# ─────────────────────────────────────────────────────
#
# 사진/상품 단건 발행은 무인 운영 항목이 아니라 **사용자 클릭**이라 작업큐 item_id 가
# 없고, 보관본(`runs/{slug}/_status.json`)도 아니라 `draft_store.set_publish_state`
# 가 적을 자리가 없다(그 함수는 _status.json 이 이미 있을 때만 쓴다). 그래서 위의
# 두 저장소 **어느 쪽에도 적히지 않은 채** 발행이 나갔고, 결말이 미상이어도 다음
# 클릭이 그대로 다시 올렸다.
#
# 여기서 세 번째 저장소를 **이 모듈 안에** 둔다 — 상태 기록·중복 차단 판정을 이
# 파일 하나로 모으는 기존 설계를 지키기 위해서다(호출부는 판정을 복제하지 않는다).
# 프로필 격리 `logs/manual_publish_state.json`, 원자 쓰기, 계정 귀속 기록.
#
# ⚠️ 불변식은 위와 같다 — **여기서 발행하지 않고, 증거 없이 `reserved` 를 적지 않는다.**

_LEDGER_FILE = "manual_publish_state.json"
_LEDGER_LOCK = threading.Lock()

# 다시 올리면 안 되는 상태. 위 `_OPEN_STATES`(결말 미상) 에 더해 **확정된 것**도
# 넣는다 — 단건 경로에서 `reserved`/`live` 를 다시 누르면 그건 그냥 중복 발행이다.
# `failed`(제출 전 실패)와 `none` 만 재시도 가능하다.
_MANUAL_BLOCK_STATES = ("reserving", "unconfirmed", "missing", "reserved",
                        "verifying", "live")

_MANUAL_BLOCK_TEXT = {
    "reserving": "발행 버튼을 누른 기록은 있는데 결과를 확인하지 못했어요",
    "unconfirmed": "이미 한 번 올렸는데 성공 신호를 읽지 못했어요",
    "missing": "올린 흔적은 있는데 블로그에서 찾지 못했어요",
    "reserved": "이미 예약/발행이 등록된 글이에요",
    "verifying": "발행 결과를 확인하는 중이에요",
    "live": "이미 공개된 글이에요",
}


def _current_profile() -> str:
    """활성 프로필 이름(모르면 "")."""
    try:
        from config import profile as _p
        return str(_p.active_profile() or "")
    except Exception:  # noqa: BLE001 — 프로필 라우터 부재면 이름 없음
        return ""


def _ledger_path() -> Optional[Path]:
    try:
        from config import profile as _p
        return Path(_p.logs_dir()) / _LEDGER_FILE
    except Exception:  # noqa: BLE001 — 경로를 모르면 원장을 쓰지 않는다(fail-open)
        return None


def _read_ledger() -> dict:
    path = _ledger_path()
    if path is None:
        return {}
    try:
        with path.open(encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        return {}
    except Exception as exc:  # noqa: BLE001 — 손상은 빈 원장이 아니라 사실대로 남긴다
        log.warning(f"[bridge] 단건 발행 원장을 읽지 못했습니다({type(exc).__name__}) "
                    f"— 중복 차단 판정이 이번엔 동작하지 않습니다")
        return {}
    return {str(k): v for k, v in raw.items() if isinstance(v, dict)} \
        if isinstance(raw, dict) else {}


def _write_ledger(data: dict) -> bool:
    path = _ledger_path()
    if path is None:
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".manualpub_", suffix=".tmp",
                                   dir=str(path.parent))
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except Exception as exc:  # noqa: BLE001 — 기록 실패가 사용자 발행을 막지 않는다
        log.warning(f"[bridge] 단건 발행 원장 기록 실패({type(exc).__name__})")
        return False


def manual_run_id(*, title: str = "", content: str = "", slug: str = "") -> str:
    """단건 발행 한 건의 **안정 id**. 같은 글이면 앱을 다시 켜도 같은 값.

    내용(제목+본문)의 해시라서:
      · GUI 재클릭·앱 재시작 → **같은 id** → 이미 올라간 글로 판정돼 막힌다.
      · 사진을 바꾸거나 본문을 고친 **새 글** → 다른 id → 정상 발행된다.

    슬러그는 있으면 앞에 붙여 사람이 원장을 읽을 수 있게 한다(판정은 해시가 한다).
    """
    digest = hashlib.sha256(
        f"{title.strip()}\x00{str(content or '').strip()}".encode("utf-8")
    ).hexdigest()[:16]
    prefix = str(slug or "").strip()
    return f"{prefix}:{digest}" if prefix else digest


def manual_state(run_id: str) -> dict:
    """단건 발행 한 건의 기록. 없으면 {}.

    **다른 계정이 남긴 기록은 없는 것으로 본다** — 원장 파일 자체가 프로필 격리
    경로에 있지만, 프로필을 복사해 쓰는 설치에서 남의 계정 결말이 이 계정의
    발행을 막는 일이 없게 한 겹 더 본다.
    """
    if not run_id:
        return {}
    entry = _read_ledger().get(str(run_id))
    if not isinstance(entry, dict):
        return {}
    recorded = str(entry.get("profile") or "")
    current = _current_profile()
    if recorded and current and recorded != current:
        return {}
    return dict(entry)


def mark_manual(run_id: str, publish_state: str, *, title: str = "",
                slug: str = "", **fields) -> bool:
    """단건 발행 상태를 원장에 기록한다(write-ahead).

    `mark()` 와 같은 의미의 기록이지만 저장소가 다르다. **두 곳에 같은 예약을
    두 번 적지 않게** 하려고, 작업큐/보관본에 적을 자리가 있는 경로(무인 운영·
    보관본 발행)는 `mark()` 를, 자리가 없는 단건 경로는 이 함수를 쓴다.

    Returns:
        기록 성공 True.
    """
    if not run_id or not publish_state:
        return False
    with _LEDGER_LOCK:
        data = _read_ledger()
        entry = data.get(str(run_id))
        entry = dict(entry) if isinstance(entry, dict) else {}
        entry["publish_state"] = str(publish_state)
        entry["updated_at"] = _now_iso()
        entry["profile"] = _current_profile()
        if title:
            entry["title"] = title
        if slug:
            entry["slug"] = slug
        for key, value in fields.items():
            if value is not None:
                entry[key] = value
        if publish_state == "reserving":
            entry["attempts"] = int(entry.get("attempts") or 0) + 1
        data[str(run_id)] = entry
        return _write_ledger(data)


def manual_blocked_reason(run_id: str = "", *, title: str = "",
                          slug: str = "") -> str:
    """이 단건 발행을 **다시 하면 안 되는** 사유 코드. 없으면 "".

    원장(단건 클릭 이력) → 보관본/작업큐(`blocked_reason`) 순으로 본다. 어느 쪽이든
    결말이 미상이거나 이미 올라간 글이면 사유를 돌려주고, 호출자는 발행하지 않는다.
    """
    entry = manual_state(run_id) if run_id else {}
    state = str(entry.get("publish_state") or "")
    if state in _MANUAL_BLOCK_STATES:
        return f"publish_state={state}"
    return blocked_reason(title=title, slug=slug)


def manual_block_message(reason: str) -> str:
    """차단 사유 코드 → 사람이 읽는 한 줄(GUI 안내용)."""
    state = str(reason or "").split("=", 1)[-1].strip()
    return _MANUAL_BLOCK_TEXT.get(state, "이미 발행을 시도한 기록이 있어요")


__all__ = [
    "set_context", "clear_context", "context", "autonomy_enabled", "recording",
    "mark", "classify_upload_result",
    "guard_new_item", "guard_external_action", "blocked_reason",
    "manual_run_id", "manual_state", "mark_manual", "manual_blocked_reason",
    "manual_block_message",
]
