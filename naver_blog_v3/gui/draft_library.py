# gui/draft_library.py — 저장 글(보관함) 단일 데이터 소스 (2026-09-21 GUI 개편)
"""화면 두 곳("저장 글" 탭 · 발행·진행 탭의 미리보기 목록)이 **같은 목록**을
보도록 만드는 얇은 조회 계층.

예전엔 발행·진행 탭(`gui/tabs/posting.py`)이 `runs/` 스캔·정렬·이미지 장수
계산을 자기 안에 들고 있었다. 저장 글 전용 화면을 새로 만들면서 그 로직을
복제하면 "한쪽만 고쳐져 숫자가 어긋나는" 이 저장소의 단골 사고가 그대로
재현되므로, 스캔 로직을 여기로 **옮기고** 두 화면이 모두 이 함수를 부른다.

설계 규칙:
- **읽기 전용.** 발행·생성·이미지 채움 등 어떤 부작용도 없다.
- Tk 의존 0 — 순수 함수라 단위 테스트가 쉽다.
- 어떤 파일이 깨져 있어도 그 글 하나만 건너뛰고 나머지는 보여준다(fail-open).
- 이미지 완성 판정은 `core.draft_image_fill.images_complete()` **단일 진실
  원본**에 위임한다(발행 직전 게이트·재고 집계와 같은 함수). 그 모듈을 못
  불러오면 판정을 지어내지 않고 ``"unknown"`` 으로 남긴다.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

# 이미지 상태 코드 → (사람이 읽는 라벨, StatusPill kind)
IMAGE_STATE_LABEL = {
    "complete": ("이미지 완료", "ok"),
    "partial":  ("이미지 부족", "warn"),
    "none":     ("이미지 없음", "warn"),
    # 장수는 다 찼지만 **출처를 확인할 수 없어** 발행이 막히는 글. 예전에는 이 상태가
    # "부족"으로 뭉뚱그려져 "4/4장인데 부족"이라는 앞뒤 안 맞는 표시가 났다.
    "unverified": ("이미지 확인 필요", "warn"),
    "unknown":  ("확인 불가", "neutral"),
}

# 출처 판정 코드 → 사람이 읽는 원인(각각 사용자가 할 일이 다르다).
_BLOCK_CAUSE = {
    "unverifiable": "출처 기록 손상",
    "reference_capture": "참고용 캡처",
    "unverified_legacy": "출처 미확인",
    "missing": "파일 없음",
}


def block_cause_text(detail: str) -> str:
    """``images_complete`` 의 사유 코드를 사람 말로. 모르는 코드는 원문 보존."""
    code = str(detail or "").strip()
    if not code:
        return "출처 미확인"
    return _BLOCK_CAUSE.get(code, code)

# 목록에서 제외할 상태 — 발행이 끝난 글은 본문이 정리돼 보관함 의미가 없다.
_EXCLUDED_STATUS = {"uploaded"}


def _runs_dir() -> Optional[Path]:
    try:
        from config import profile as _profile
        return _profile.runs_dir()
    except Exception:  # noqa: BLE001 — 프로필 라우터 부재 시 목록 없음
        log.debug("[draft_library] runs_dir 확인 실패", exc_info=True)
        return None


def _read_json(path: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _image_state(post: dict) -> Dict[str, Any]:
    """이미지 상태 판정 — `core.draft_image_fill.images_complete()` 에 위임.

    Returns:
        ``{"state": "complete"|"partial"|"unverified"|"none"|"unknown",
        "have": int, "want": int|None, "unverified": int, "cause": str}``
        — ``want`` 는 계획된 슬롯 수(모르면 None), ``cause`` 는 막힌 원인 코드
        (``unverifiable``/``reference_capture``/``unverified_legacy``, 없으면 "").
    """
    have = 0
    for raw in (post.get("image_paths") or []):
        text = str(raw or "").strip()
        if not text:
            continue
        try:
            if Path(text).is_file():
                have += 1
        except (OSError, ValueError):
            continue

    try:
        from core import draft_image_fill
        complete, detail = draft_image_fill.images_complete(post)
    except Exception:  # noqa: BLE001 — core 미로드는 "모름"(추측 금지)
        return {"state": "unknown", "have": have, "want": None,
                "unverified": 0, "cause": ""}

    detail = detail if isinstance(detail, dict) else {}
    want = detail.get("slots") or None
    unverified = int(detail.get("unverified") or 0)
    causes = detail.get("unverified_reasons")
    cause = ""
    if isinstance(causes, dict) and causes:
        cause = str(max(causes.items(), key=lambda kv: kv[1])[0])
    base = {"have": have, "want": want, "unverified": unverified, "cause": cause}
    if complete:
        return {"state": "complete", **base}
    reason = str(detail.get("reason") or "")
    if reason == "unverified_assets":
        # 슬롯은 다 찼는데 출처를 확인할 수 없는 자산 때문에 발행이 막힌 글.
        # 자동 보충 대상이 아니므로 '부족'이라 말하면 안 된다 — 사용자가 골라
        # '미검증 이미지 다시 만들기'를 눌러야 풀린다.
        return {"state": "unverified", **base}
    if reason == "no_plan":
        # 계획이 없는 레거시 글 — 완성/미완을 판정할 수 없다(0장이라 단정 금지).
        return {"state": ("none" if have == 0 else "unknown"), **base}
    if have == 0:
        return {"state": "none", **base}
    return {"state": "partial", **base}


def list_drafts(limit: int = 60) -> List[Dict[str, Any]]:
    """활성 프로필 ``runs/`` 의 저장 글을 **최근 순**으로 돌려준다.

    정렬 기준은 슬러그에 박힌 옛 날짜가 아니라 실제 기록 시각
    (``_status.json`` / ``post_data.json`` mtime 중 더 최근 것) — 슬러그가
    ``2026-05-17_…`` 이어도 9월에 손댔으면 9월로 보인다.

    Args:
        limit: 최대 편수(1 미만이면 빈 목록).

    Returns:
        각 항목은 dict::

            {"slug", "dir", "post", "status", "ts", "title",
             "image_state", "image_have", "image_want", "publish_state"}
    """
    if limit < 1:
        return []
    runs = _runs_dir()
    if runs is None or not runs.exists():
        return []

    try:
        cands = []
        for child in runs.iterdir():
            if not child.is_dir():
                continue
            st = child / "_status.json"
            try:
                cands.append((st.stat().st_mtime, child))
            except OSError:
                continue
    except OSError:
        return []
    cands.sort(key=lambda t: t[0], reverse=True)

    out: List[Dict[str, Any]] = []
    # 상태가 깨졌거나 발행 완료인 글을 건너뛰어도 limit 을 채울 수 있게 넉넉히 훑는다.
    for mtime, child in cands[: max(limit * 3, limit)]:
        if len(out) >= limit:
            break
        status = _read_json(child / "_status.json")
        if status is None or status.get("status") in _EXCLUDED_STATUS:
            continue
        post_path = child / "post_data.json"
        post = _read_json(post_path)
        if post is None:
            continue
        try:
            body_mtime = post_path.stat().st_mtime
        except OSError:
            body_mtime = mtime
        img = _image_state(post)
        out.append({
            "slug": child.name,
            "dir": child,
            "post": post,
            "status": status,
            "ts": max(mtime, body_mtime),
            "title": str(post.get("title") or status.get("title") or child.name),
            "image_state": img["state"],
            "image_have": img["have"],
            "image_want": img["want"],
            "image_unverified": img.get("unverified", 0),
            "image_cause": img.get("cause", ""),
            "publish_state": str(status.get("publish_state") or ""),
        })
    return out


# 상태 표식 — 표는 셀 단위 색을 지원하지 않으므로(행 전체가 같은 색), 색
# 대신 **모양**으로 구분한다. 일반 글꼴에 있는 문자만 쓴다(아이콘 폰트가
# 없는 환경에서도 두부(□)가 안 나온다).
_STATE_MARK = {"complete": "●", "partial": "◐", "none": "○",
               "unverified": "!", "unknown": "·"}


def image_badge(entry: Dict[str, Any], long: bool = False) -> str:
    """이미지 상태 한 줄.

    Args:
        entry: :func:`list_drafts` 항목.
        long: True 면 "이미지 완료" 처럼 주어까지 붙인다(상세 패널용).
            False(기본)면 "완료" — 표의 '이미지' 열 안에서는 주어가 열
            제목에 이미 있어 중복이고, 좁은 창에서 셀이 잘리는 원인이었다.
    """
    state = str(entry.get("image_state") or "unknown")
    label = IMAGE_STATE_LABEL.get(state, IMAGE_STATE_LABEL["unknown"])[0]
    if not long:
        label = label.replace("이미지 ", "")
    mark = _STATE_MARK.get(state, "·")
    have = int(entry.get("image_have") or 0)
    want = entry.get("image_want")
    if state == "complete":
        return f"{mark} {label} · {have}장"
    if state == "unverified":
        # "4/4장인데 부족"처럼 앞뒤가 안 맞게 말하지 않는다 — 장수는 찼고 **원인**이 다르다.
        cause = block_cause_text(str(entry.get("image_cause") or ""))
        return f"{mark} {label} · {have}장({cause})"
    if state == "partial" and want:
        return f"{mark} {label} · {have}/{want}장"
    if state == "partial":
        return f"{mark} {label} · {have}장"
    if state == "none":
        return f"{mark} {label}"
    return f"{mark} {label} · {have}장" if have else f"{mark} {label}"


_PUBLISH_STATE_LABEL = {
    "none": "미시도", "selected": "발행 준비", "reserving": "확인 필요",
    "unconfirmed": "확인 필요", "missing": "확인 필요", "verifying": "확인 필요",
    "reserved": "예약 완료", "live": "발행 완료", "failed": "발행 실패",
}


def publish_state_label(entry: Dict[str, Any]) -> str:
    """Show the persisted publishing state, keeping uncertain outcomes explicit."""
    state = str(entry.get("publish_state") or "none")
    return _PUBLISH_STATE_LABEL.get(state, "상태 확인 필요")


def is_publish_ready(entry: Dict[str, Any]) -> bool:
    """Count only complete-image drafts whose saved publishing state is retry-safe."""
    return (entry.get("image_state") == "complete"
            and str(entry.get("publish_state") or "none") in {"none", "selected", "failed"}
            and str((entry.get("status") or {}).get("status") or "")
            in {"draft_ready", "queued"})


def counts(entries: List[Dict[str, Any]]) -> Dict[str, int]:
    """상태별 편수 요약 — ``{"total", "complete", "partial", "unverified", "none", "unknown"}``."""
    out = {"total": len(entries), "complete": 0, "ready": 0, "partial": 0,
           "unverified": 0, "none": 0, "unknown": 0}
    for e in entries:
        key = str(e.get("image_state") or "unknown")
        if key in out:
            out[key] += 1
        else:
            out["unknown"] += 1
        if is_publish_ready(e):
            out["ready"] += 1
    return out
