# core/editorial_profile.py
# ✍ 계정별 편집 방향(대상 독자·해결할 문제·문체·썸네일 시각 정체성) — 2026-09-18
# ─────────────────────────────────────────────────────────────────────────────
# 이 모듈이 존재하는 이유
#   글과 썸네일이 "누구에게 무엇을 해결해 주는가"를 계정마다 다르게 정할 수 있어야 한다.
#   기존에는 그 정보가 카테고리 페르소나(`templates/prompt_strategies.json`)와 브랜드팩
#   (`config/brand.py`)에 흩어져 있었고, **GUI 에서 편집할 수 있는 자리는 없었다.**
#
# 설계 결정
#   · 저장 위치 = `config.profile.config_dir(<프로필>)/editorial.json`.
#     - 프로필 라우터를 그대로 타므로 **계정 간 누출이 구조적으로 불가능**하다
#       (named 프로필 → `profiles/<name>/config/`, default → 앱 config 디렉터리).
#     - `settings.py` 를 건드리지 않는다 → `config_io` 33/38 키 계약·GUI vars 계약 불변.
#     - `brand/` 디렉터리를 새로 만들지 않는다 — `profile.brand_dir()` 의 존재 여부가
#       `brand.categories()` 같은 다른 판정을 뒤집기 때문(부작용 차단).
#   · **설정이 없는 계정은 아무것도 덮어쓰지 않는다.** `prompt_block()` 이 빈 문자열을
#     돌려주므로 생성 프롬프트가 바이트 그대로다. 기본값은 "카테고리 페르소나에서 읽어온
#     보수적 값"이며 화면 안내용이고, 저장하지 않는 한 프롬프트에 들어가지 않는다.
#   · 임포트 부작용 0(config/core 전부 지연 임포트), 전 경로 fail-open.
from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

try:  # 프로필 격리 로거(있으면 사용) — 없으면 표준 로깅 폴백.
    from config.logger import get_logger
    log = get_logger(__name__)
except Exception:  # noqa: BLE001 — config 미로드 환경(경량 테스트 스텁)
    import logging
    log = logging.getLogger(__name__)


SCHEMA_VERSION: int = 1
FILENAME: str = "editorial.json"

# 입력 길이 상한 — 프롬프트 예산을 지키고 장문 붙여넣기로 프롬프트가 밀리는 것을 막는다.
LIMIT_AUDIENCE: int = 120
LIMIT_PROBLEM: int = 200
LIMIT_VOICE: int = 200
LIMIT_MOOD: int = 60
LIMIT_MESSAGE_HINT: int = 40
TOPIC_MODES = ("auto", "general", "government_policy", "it", "travel", "review", "learning")

# 썸네일 시각 정체성 기본값 — 기존 `core/image_plan._THUMB_STYLE` 의 색과 같은 값이라
# 설정하지 않은 계정의 썸네일 톤이 바뀌지 않는다(딥네이비 + 골드 + 오프화이트).
DEFAULT_THUMB_BG: str = "#12213f"
DEFAULT_THUMB_ACCENT: str = "#e7b64b"
DEFAULT_THUMB_TEXT: str = "#f7f7f2"

_HEX_RE = re.compile(r"^#(?:[0-9A-Fa-f]{3}|[0-9A-Fa-f]{6})$")
_WS_RE = re.compile(r"\s+")

_TEXT_FIELDS: Dict[str, int] = {
    "audience": LIMIT_AUDIENCE,
    "reader_problem": LIMIT_PROBLEM,
    "voice_note": LIMIT_VOICE,
}
_THUMB_TEXT_FIELDS: Dict[str, int] = {"mood": LIMIT_MOOD}
_THUMB_COLOR_FIELDS: Dict[str, str] = {
    "bg": DEFAULT_THUMB_BG,
    "accent": DEFAULT_THUMB_ACCENT,
    "text": DEFAULT_THUMB_TEXT,
}


# ─────────────────────────────────────────────────────────────────────────────
# 텍스트 정규화(인젝션 안전 — 이 값들은 생성 프롬프트에 그대로 들어간다)
# ─────────────────────────────────────────────────────────────────────────────
def sanitize(text: Any, *, limit: int) -> str:
    """제어문자·백틱·큰따옴표를 제거하고 공백을 접어 상한까지 자른다.

    사용자가 직접 입력하는 값이지만 프롬프트 표면이므로 `image_plan.sanitize` 와 같은
    규칙을 쓴다(따옴표로 감싸 넣을 때 구획이 깨지지 않게).
    """
    if not isinstance(text, str) or not text:
        return ""
    cleaned = "".join(" " if (ord(c) < 0x20 or ord(c) == 0x7F) else c for c in text)
    cleaned = cleaned.replace("`", "")
    for quote in ('"', "“", "”"):
        cleaned = cleaned.replace(quote, "'")
    return _WS_RE.sub(" ", cleaned).strip()[:limit]


def normalize_color(value: Any, default: str) -> str:
    """`#rrggbb`(또는 `#rgb`)만 통과. 그 외는 기본값 — 잘못된 값이 렌더를 깨지 않게."""
    text = str(value or "").strip()
    if _HEX_RE.match(text):
        return text.lower() if len(text) == 7 else text.lower()
    return default


# ─────────────────────────────────────────────────────────────────────────────
# 경로
# ─────────────────────────────────────────────────────────────────────────────
def path(profile: Optional[str] = None) -> Optional[Path]:
    """이 계정의 편집 방향 파일 경로. profile 라우터를 못 쓰면 None(저장 생략)."""
    try:
        from config import profile as _profile
        return Path(_profile.config_dir(profile)) / FILENAME
    except Exception as exc:  # noqa: BLE001 — 라우터 미로드면 이 기능만 비활성
        log.debug(f"[editorial_profile] 경로 확보 실패({type(exc).__name__}) — 기능 비활성")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# 기본값(보수적) — 저장하지 않는 한 프롬프트에 들어가지 않는다
# ─────────────────────────────────────────────────────────────────────────────
def defaults_for(category: Any = "", profile: Optional[str] = None) -> Dict[str, Any]:
    """설정이 없는 계정에 보여줄 보수적 기본값.

    카테고리 페르소나(`prompt_strategies.json` 의 `persona`/`tone`)를 그대로 읽어 온다 —
    이 저장소가 이미 그 글에 쓰고 있는 값이라 "임의로 주제를 덮어쓰지 않는다"는 요건을
    만족한다. 페르소나를 못 읽으면 분야 무관 중립 문구로 떨어진다.
    """
    audience = ""
    voice = ""
    try:
        from core.content_generator import _load_prompt_strategy
        strategy = _load_prompt_strategy(str(category or "")) or {}
        audience = sanitize(strategy.get("persona"), limit=LIMIT_AUDIENCE)
        voice = sanitize(strategy.get("tone"), limit=LIMIT_VOICE)
    except Exception as exc:  # noqa: BLE001 — 전략 로드 실패는 중립 기본값
        log.debug(f"[editorial_profile] 페르소나 기본값 조회 실패({type(exc).__name__})")

    return {
        "version": SCHEMA_VERSION,
        "configured": False,
        "source": "default",
        "updated_at": "",
        "audience": audience,
        "reader_problem": "",
        "voice_note": voice,
        "topic_mode": "auto",
        "thumbnail": {
            "bg": DEFAULT_THUMB_BG,
            "accent": DEFAULT_THUMB_ACCENT,
            "text": DEFAULT_THUMB_TEXT,
            "mood": "",
        },
    }


def _normalize(raw: Any, *, category: Any = "", profile: Optional[str] = None,
               source: str = "saved") -> Dict[str, Any]:
    """저장된(또는 입력된) dict 를 현재 스키마로 정규화한다. 어떤 형태든 dict 를 돌려준다."""
    base = defaults_for(category, profile)
    if not isinstance(raw, dict):
        return base

    out = dict(base)
    for field, limit in _TEXT_FIELDS.items():
        if field in raw:
            out[field] = sanitize(raw.get(field), limit=limit)

    thumb = dict(base["thumbnail"])
    raw_thumb = raw.get("thumbnail")
    if isinstance(raw_thumb, dict):
        for field, fallback in _THUMB_COLOR_FIELDS.items():
            if field in raw_thumb:
                thumb[field] = normalize_color(raw_thumb.get(field), fallback)
        for field, limit in _THUMB_TEXT_FIELDS.items():
            if field in raw_thumb:
                thumb[field] = sanitize(raw_thumb.get(field), limit=limit)
    out["thumbnail"] = thumb
    topic_mode = str(raw.get("topic_mode") or "auto").strip().lower()
    out["topic_mode"] = topic_mode if topic_mode in TOPIC_MODES else "auto"

    out["version"] = SCHEMA_VERSION
    out["updated_at"] = sanitize(raw.get("updated_at"), limit=40)
    # "설정됨" 판정은 **사람이 적은 내용이 하나라도 있는가** — 색만 기본값이면 미설정이다.
    out["configured"] = (any(bool(out.get(f)) for f in _TEXT_FIELDS)
                         or bool(thumb.get("mood")) or out["topic_mode"] != "auto")
    out["source"] = source if out["configured"] else "default"
    return out


# ─────────────────────────────────────────────────────────────────────────────
# 읽기 · 쓰기
# ─────────────────────────────────────────────────────────────────────────────
def load(profile: Optional[str] = None, *, category: Any = "") -> Dict[str, Any]:
    """이 계정의 편집 방향. 파일이 없거나 깨졌으면 보수적 기본값(configured=False)."""
    target = path(profile)
    if target is None:
        return defaults_for(category, profile)
    try:
        if not target.is_file():
            return defaults_for(category, profile)
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning(f"[editorial_profile] 읽기 실패({type(exc).__name__}) — 기본값 사용")
        return defaults_for(category, profile)
    return _normalize(raw, category=category, profile=profile)


def save(values: Dict[str, Any], profile: Optional[str] = None) -> Dict[str, Any]:
    """편집 방향을 저장한다(원자 쓰기). 저장된 정규화 결과를 돌려준다.

    반환 dict 의 `saved` 가 False 면 디스크에 쓰지 못한 것이다(호출자가 사용자에게
    알려야 한다 — 조용히 성공으로 보고하지 않는다).
    """
    data = dict(values or {})
    if "topic_mode" not in data:
        current_path = path(profile)
        if current_path is not None:
            try:
                current = json.loads(current_path.read_text(encoding="utf-8"))
                if isinstance(current, dict) and "topic_mode" in current:
                    data["topic_mode"] = current["topic_mode"]
            except (OSError, ValueError):
                pass
    normalized = _normalize(data, profile=profile)
    normalized["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    target = path(profile)
    if target is None:
        normalized["saved"] = False
        normalized["reason"] = "no_profile_path"
        return normalized

    payload = {k: v for k, v in normalized.items() if k not in ("saved", "reason")}
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        fd, tmp = tempfile.mkstemp(prefix=".editorial-", suffix=".tmp",
                                   dir=str(target.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp, target)
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        normalized["saved"] = True
        normalized["reason"] = ""
        log.info("[editorial_profile] 저장 완료 — configured=%s", normalized["configured"])
    except (OSError, ValueError, TypeError) as exc:
        normalized["saved"] = False
        normalized["reason"] = f"write_error:{type(exc).__name__}"
        log.warning(f"[editorial_profile] 저장 실패({type(exc).__name__})")
    return normalized


# ─────────────────────────────────────────────────────────────────────────────
# 소비 — 생성 프롬프트 · 썸네일 합성
# ─────────────────────────────────────────────────────────────────────────────
def prompt_block(category: Any = "", profile: Optional[str] = None) -> str:
    """집필 프롬프트에 넣을 '이 블로그의 편집 방향' 블록.

    **설정이 없으면 빈 문자열** — 호출자가 append 하지 않아 프롬프트가 바이트 그대로다.
    카테고리 페르소나를 대체하지 않고 그 위에 독자·문제·문체를 **한 겹 더** 얹는다.
    """
    try:
        data = load(profile, category=category)
    except Exception as exc:  # noqa: BLE001 — 조회 실패는 블록 없음
        log.debug(f"[editorial_profile] prompt_block 조회 실패({type(exc).__name__})")
        return ""
    if not data.get("configured"):
        return ""

    lines = ["[이 블로그의 편집 방향 — 카테고리 전략 다음 우선순위]"]
    if data.get("topic_mode") and data["topic_mode"] != "auto":
        lines.append(f"- 계정에서 선택한 주제 분야: {data['topic_mode']}")
    if data.get("audience"):
        lines.append(f"- 이 글을 읽는 사람: {data['audience']}")
    if data.get("reader_problem"):
        lines.append(f"- 그 사람이 해결하려는 문제: {data['reader_problem']}")
        lines.append("- 도입부는 이 문제에 대한 답부터 제시하세요(배경 설명·의미 부여로 시작하지 말 것).")
    if data.get("voice_note"):
        lines.append(f"- 문체 지침: {data['voice_note']}")
    lines.append(
        "- 위 내용은 '누구에게 무엇을 해결해 주는가'만 정합니다. 여기에 없는 사실·수치·"
        "기관명을 만들어 내지 마세요.")
    return "\n".join(lines) + "\n"


def thumbnail_style(profile: Optional[str] = None) -> Dict[str, str]:
    """썸네일 로컬 합성·배경 프롬프트가 쓰는 색·분위기. 항상 유효한 값만 돌려준다."""
    try:
        data = load(profile)
        thumb = data.get("thumbnail") or {}
    except Exception as exc:  # noqa: BLE001
        log.debug(f"[editorial_profile] thumbnail_style 조회 실패({type(exc).__name__})")
        thumb = {}
    return {
        "bg": normalize_color(thumb.get("bg"), DEFAULT_THUMB_BG),
        "accent": normalize_color(thumb.get("accent"), DEFAULT_THUMB_ACCENT),
        "text": normalize_color(thumb.get("text"), DEFAULT_THUMB_TEXT),
        "mood": sanitize(thumb.get("mood"), limit=LIMIT_MOOD),
    }


def summary(profile: Optional[str] = None) -> Dict[str, Any]:
    """로그·화면 표기용 요약(민감정보 없음)."""
    data = load(profile)
    return {
        "configured": bool(data.get("configured")),
        "updated_at": data.get("updated_at") or "",
        "has_audience": bool(data.get("audience")),
        "has_problem": bool(data.get("reader_problem")),
        "has_voice": bool(data.get("voice_note")),
        "thumbnail": thumbnail_style(profile),
    }


__all__ = [
    "SCHEMA_VERSION", "FILENAME",
    "LIMIT_AUDIENCE", "LIMIT_PROBLEM", "LIMIT_VOICE", "LIMIT_MOOD", "LIMIT_MESSAGE_HINT",
    "DEFAULT_THUMB_BG", "DEFAULT_THUMB_ACCENT", "DEFAULT_THUMB_TEXT",
    "sanitize", "normalize_color", "path", "defaults_for", "load", "save",
    "prompt_block", "thumbnail_style", "summary",
]
