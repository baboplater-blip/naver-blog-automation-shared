# core/photo_analyst.py
# 📸 사진 인제스트(P0-② Phase 2) — 사진 폴더를 시간순으로 정렬하고, 사진마다
# "실제로 보이는 것만" 구조화 분석한다. 비전(이미지 인식)은 Claude CLI 의 Read
# 도구로 수행한다(추가 비용·API 키 0 — Claude Max 구독). 코드베이스 유일의 순수
# 신규 두뇌.
#
# 설계 원칙:
# - fail-open: 사진 한 장 분석이 실패해도(비전 타임아웃·CLI 부재·JSON 깨짐) 그
#   사진은 파일명/EXIF 기반 최소 캡션으로 대체하고 파이프라인은 계속 간다.
# - 무근거 배제: 비전 프롬프트가 "사진에 실제로 보이는 것만 기술, 없는 내용을
#   지어내지 말 것"을 강제한다(개떡찰떡 핵심 원칙 = 우리 fact_gate 철학).
# - 프라이버시(두 층위를 구분):
#   (구조화 필드) EXIF 촬영시각은 정렬에만 쓰고 값은 로그·brief 에 남기지 않는다. GPS
#     좌표는 구조화 필드로 추출하지 않는다(텍스트/분석 결과에 위치가 새지 않음).
#   (파일 메타데이터) 원본 파일 자체에는 GPS EXIF 가 남아 있다 — 미리보기용 사진을
#     복사할 때 experience_writer._copy_photo_privacy 가 EXIF 를 제거하며 재저장한다
#     (이 모듈은 원본을 읽기만 하고 복사/제거는 하지 않는다).
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Sequence, Union

from config.logger import get_logger

log = get_logger(__name__)

# 비전 백엔드 "claude" 로 실제 호출할 Claude 모델 별칭(비전 지원). settings 의
# PHOTO_VISION_MODEL 은 백엔드 선택자("claude")이고, 아래는 그 백엔드의 모델.
_CLAUDE_VISION_MODEL = "sonnet"

# HEIC/HEIF(아이폰 기본 포맷) 지원 — pillow-heif 가 있으면 opener 를 등록해 Pillow 가
# 열 수 있게 한다(EXIF 읽기·JPEG 변환 모두 가능). 없으면 _HEIF_OK=False 로 두고
# _list_images 가 해당 파일을 조용히 파손시키지 않고 명확히 제외한다.
try:
    from pillow_heif import register_heif_opener  # type: ignore
    register_heif_opener()
    _HEIF_OK = True
except Exception:  # noqa: BLE001 — 미설치/등록 실패면 HEIC 미지원으로 폴백
    _HEIF_OK = False

_HEIF_EXTS: frozenset = frozenset({".heic", ".heif"})
# 지원 이미지 확장자(소문자). HEIC/HEIF 는 _HEIF_OK 일 때만 실제로 통과(_list_images 에서 게이트).
_IMAGE_EXTS: frozenset = frozenset(
    {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".bmp", ".tif", ".tiff", ".gif"}
)


def preview_ext(suffix: str) -> str:
    """미리보기·복사본 확장자 매핑 — HEIC/HEIF 는 브라우저 미지원이라 .jpg 로 변환 대상.

    write_run(사진 복사)과 _photo_rel_name(미리보기 src)이 같은 규칙을 써야 경로가 맞는다.
    """
    return ".jpg" if (suffix or "").lower() in _HEIF_EXTS else (suffix or ".jpg").lower()

# EXIF 태그 번호(값이 아니라 태그 상수만 사용 — 시각 값은 로그 미기록).
_EXIF_DATETIME_ORIGINAL = 36867  # DateTimeOriginal (Exif sub-IFD)
_EXIF_DATETIME_DIGITIZED = 36868  # DateTimeDigitized
_EXIF_DATETIME = 306             # DateTime (base IFD)
_EXIF_IFD_POINTER = 0x8769       # Exif sub-IFD pointer


@dataclass
class PhotoAnalysis:
    """사진 1장의 구조화 분석 결과.

    Attributes:
        path: 원본 이미지 절대 경로.
        order: 서사 순서(1-based, EXIF 촬영시각 오름차순).
        scene: 장면 한 줄 설명(보이는 것만).
        signs: 간판·상호 등 읽히는 텍스트 목록.
        menu: 메뉴·가격 등 목록.
        text_detected: 사진에서 읽히는 글자 목록.
        caption: 한국어 1문장 캡션(figure figcaption 용).
        vision_ok: 비전 분석 성공 여부. False 면 파일명 기반 폴백 캡션.
        taken_at: EXIF 촬영시각(정렬용, 로그·brief 미기록). None 가능.
    """

    path: str
    order: int
    scene: str = ""
    signs: List[str] = field(default_factory=list)
    menu: List[str] = field(default_factory=list)
    text_detected: List[str] = field(default_factory=list)
    caption: str = ""
    vision_ok: bool = False
    # 프라이버시: brief/report 로 내보낼 때 제외되도록 to_public() 에서 뺀다.
    taken_at: Optional[str] = None

    def to_public(self) -> dict:
        """외부 산출물(runs/qa.json 등)에 실을 안전한 dict — 촬영시각 제외."""
        data = asdict(self)
        data.pop("taken_at", None)
        return data


# ──────────────────────────────────────────────────────────────────────────────
# 입력 정규화 · EXIF 정렬
# ──────────────────────────────────────────────────────────────────────────────


def _list_images(source: Union[str, Path, Sequence[Union[str, Path]]]) -> List[Path]:
    """폴더 경로 또는 경로 목록을 이미지 Path 리스트로 정규화한다.

    Args:
        source: 폴더 경로(str/Path) 또는 이미지 경로들의 시퀀스.

    Returns:
        존재하는 이미지 파일 Path 리스트(정렬 전).
    """
    def _accept(candidate: Path) -> bool:
        if not (candidate.is_file() and candidate.suffix.lower() in _IMAGE_EXTS):
            return False
        # HEIC/HEIF 는 pillow-heif 없으면 조용히 파손시키지 말고 제외 + 명확한 경고.
        if candidate.suffix.lower() in _HEIF_EXTS and not _HEIF_OK:
            log.warning(
                f"[photo] HEIC/HEIF 제외: {candidate.name} — pillow-heif 미설치라 열 수 없음. "
                "`pip install pillow-heif` 후 다시 시도하면 자동 변환됩니다.")
            return False
        return True

    paths: List[Path] = []
    if isinstance(source, (str, Path)):
        root = Path(source)
        if root.is_dir():
            for child in root.iterdir():
                if _accept(child):
                    paths.append(child)
        elif _accept(root):
            paths.append(root)
    elif isinstance(source, Sequence):
        for item in source:
            candidate = Path(item)
            if _accept(candidate):
                paths.append(candidate)
    return paths


def _exif_datetime(path: Path) -> Optional[datetime]:
    """EXIF 촬영시각을 읽어 datetime 으로 반환(정렬 전용).

    값은 절대 로그에 남기지 않는다(프라이버시). 읽기 실패·미탑재는 None.
    """
    try:
        from PIL import Image  # 지연 임포트 — Pillow 는 기존 의존성.
    except ImportError:
        return None
    try:
        with Image.open(path) as image:
            exif = image.getexif()
            if not exif:
                return None
            raw = exif.get(_EXIF_DATETIME)
            if not raw:
                try:
                    sub_ifd = exif.get_ifd(_EXIF_IFD_POINTER)
                except (AttributeError, KeyError, ValueError):
                    sub_ifd = {}
                raw = (sub_ifd.get(_EXIF_DATETIME_ORIGINAL)
                       or sub_ifd.get(_EXIF_DATETIME_DIGITIZED))
            if not raw:
                return None
            return datetime.strptime(str(raw).strip(), "%Y:%m:%d %H:%M:%S")
    except (OSError, ValueError, TypeError, KeyError):
        # 손상 파일·비표준 EXIF·파싱 실패 — 정렬은 파일명 폴백으로.
        return None


def _sort_key(entry: "tuple[Path, Optional[datetime]]") -> "tuple[int, str]":
    """정렬 키 — EXIF 시각이 있으면 시각 우선, 없으면 뒤로 밀어 파일명순.

    (0, ISO시각) 은 (1, ...) 보다 항상 앞서므로 촬영시각이 있는 사진이 먼저,
    시각이 같으면 파일명으로 안정 정렬. EXIF 없는 사진은 파일명 오름차순으로 뒤에.
    """
    path, taken = entry
    if taken is not None:
        return (0, taken.strftime("%Y%m%d%H%M%S") + path.name.lower())
    return (1, path.name.lower())


# ──────────────────────────────────────────────────────────────────────────────
# 비전 분석(Claude CLI Read)
# ──────────────────────────────────────────────────────────────────────────────

_VISION_SCHEMA = json.dumps(
    {
        "type": "object",
        "properties": {
            "scene": {"type": "string"},
            "signs": {"type": "array", "items": {"type": "string"}},
            "menu": {"type": "array", "items": {"type": "string"}},
            "text_detected": {"type": "array", "items": {"type": "string"}},
            "caption": {"type": "string"},
        },
        "required": ["scene", "caption"],
    },
    ensure_ascii=False,
)

_VISION_PROMPT = (
    "현재 폴더의 '{name}' 이미지를 Read 도구로 열어, 사진에 **실제로 보이는 것만** "
    "기술하세요. 사진에 없는 내용(상호·메뉴·가격·장소·상황)을 절대 지어내지 마세요. "
    "확실하지 않으면 비워 두세요.\n\n"
    "아래 JSON 하나만 출력하세요:\n"
    "- scene: 장면 한 줄 설명(보이는 것만)\n"
    "- signs: 간판·상호 등 읽히는 텍스트 배열(없으면 빈 배열)\n"
    "- menu: 메뉴·가격 배열(없으면 빈 배열)\n"
    "- text_detected: 사진에서 읽히는 글자 배열(없으면 빈 배열)\n"
    "- caption: 한국어 1문장 캡션(사진 아래 넣을 담백한 설명)"
)


def _claude_cmd() -> str:
    """content_generator 와 동일한 단일 경로로 claude 실행 파일을 찾는다."""
    try:
        from core.content_generator import _find_claude_cmd
        return _find_claude_cmd()
    except (ImportError, AttributeError):
        import shutil
        return shutil.which("claude") or ""


def _extract_json_object(text: str) -> Optional[dict]:
    """모델 응답에서 첫 균형 잡힌 JSON 객체를 추출한다.

    ``--json-schema`` 는 순수 JSON 을 강제하지 않아 응답이 ```json 코드펜스 +
    뒤에 설명 산문이 붙어 나온다. 문자열 리터럴 내부의 중괄호를 세지 않도록
    상태를 추적하며 첫 `{` 부터 균형이 맞는 지점까지 잘라 파싱한다.
    """
    if not text:
        return None
    stripped = re.sub(r"^```(?:json)?\s*", "", text.strip())
    start = stripped.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(stripped)):
        char = stripped[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    parsed = json.loads(stripped[start:index + 1])
                    return parsed if isinstance(parsed, dict) else None
                except ValueError:
                    return None
    return None


def _coerce_str_list(value: object) -> List[str]:
    """모델이 문자열/배열/None 어느 것으로 줘도 안전하게 문자열 리스트로."""
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _run_vision(path: Path, model: str, timeout: int,
                stop_flag=None) -> Optional[dict]:
    """단일 이미지를 Claude CLI(Read 도구)로 분석해 dict 반환. 실패 시 None.

    절대 예외를 전파하지 않는다(어떤 실패도 None → 호출자가 폴백).
    """
    if stop_flag is not None and getattr(stop_flag, "is_set", lambda: False)():
        return None
    cmd = _claude_cmd()
    if not cmd:
        log.warning("[photo] claude CLI 없음 — 비전 분석 스킵, 폴백 캡션 사용")
        return None

    # content_generator._call_claude 와 동일한 서브프로세스 규약:
    # 중첩 Claude 세션 방지 + API 키 제거(과금 방지).
    for var in ("CLAUDECODE", "CLAUDE_CODE"):
        os.environ.pop(var, None)
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)

    prompt = _VISION_PROMPT.format(name=path.name)
    # cwd 를 이미지 폴더로 두고 파일명으로 참조 → 워크스페이스 신뢰 프롬프트 회피.
    # --tools Read 로 사용 가능한 도구를 Read 하나로 제한(Bash/Edit/Write 원천 차단).
    args = [
        cmd, "-p", prompt,
        "--output-format", "text",
        "--model", model,
        "--json-schema", _VISION_SCHEMA,
        "--tools", "Read",
    ]
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=timeout,
            cwd=str(path.parent),
        )
    except subprocess.TimeoutExpired:
        log.warning(f"[photo] 비전 타임아웃({timeout}s) — 폴백 캡션: {path.name}")
        return None
    except (OSError, ValueError) as exc:
        log.warning(f"[photo] 비전 호출 실패({type(exc).__name__}) — 폴백 캡션: {path.name}")
        return None

    if proc.returncode != 0:
        log.warning(f"[photo] 비전 rc={proc.returncode} — 폴백 캡션: {path.name}")
        return None
    return _extract_json_object(proc.stdout)


def _fallback_caption(path: Path, order: int) -> str:
    """비전 실패 시 파일명 기반 최소 캡션(무근거 서술 없이 담백하게)."""
    return f"직접 찍은 사진 {order}"


def analyze_photo(path: Path, order: int, model: str, timeout: int,
                  taken_at: Optional[datetime] = None,
                  stop_flag=None) -> PhotoAnalysis:
    """사진 1장을 분석해 PhotoAnalysis 반환. 실패해도 예외 없이 폴백."""
    path = Path(path)
    taken_iso = taken_at.isoformat() if isinstance(taken_at, datetime) else None
    vision = _run_vision(path, model, timeout, stop_flag=stop_flag) if model else None

    if not vision:
        return PhotoAnalysis(
            path=str(path.resolve()),
            order=order,
            caption=_fallback_caption(path, order),
            vision_ok=False,
            taken_at=taken_iso,
        )

    scene = str(vision.get("scene") or "").strip()
    caption = str(vision.get("caption") or "").strip() or scene or _fallback_caption(path, order)
    return PhotoAnalysis(
        path=str(path.resolve()),
        order=order,
        scene=scene,
        signs=_coerce_str_list(vision.get("signs")),
        menu=_coerce_str_list(vision.get("menu")),
        text_detected=_coerce_str_list(vision.get("text_detected")),
        caption=caption,
        vision_ok=True,
        taken_at=taken_iso,
    )


def _resolve_model(model: Optional[str]) -> str:
    """PHOTO_VISION_MODEL(백엔드 선택자)을 실제 claude 모델 별칭으로 해석.

    "claude" → 비전 지원 모델 별칭. 그 외(예: "gemini")는 아직 배선되지 않아
    빈 문자열 반환 → 호출부가 비전을 건너뛰고 폴백 캡션만 쓴다.
    """
    backend = (model or "claude").strip().lower()
    if backend == "claude":
        return _CLAUDE_VISION_MODEL
    log.warning(f"[photo] 비전 백엔드 '{backend}' 미배선 — 폴백 캡션만 사용")
    return ""


def analyze_folder(source: Union[str, Path, Sequence[Union[str, Path]]],
                   model: Optional[str] = None,
                   timeout: Optional[int] = None,
                   stop_flag=None) -> List[PhotoAnalysis]:
    """사진 폴더/경로목록을 EXIF 시간순 정렬 후 사진마다 분석한다.

    Args:
        source: 사진 폴더 경로 또는 이미지 경로 시퀀스.
        model: 비전 백엔드 선택자. None 이면 settings.PHOTO_VISION_MODEL.
        timeout: 사진 1장당 비전 타임아웃(초). None 이면 settings.PHOTO_VISION_TIMEOUT.
        stop_flag: threading.Event 유사 객체. is_set() 이면 조기 종료.

    Returns:
        서사 순서(EXIF 오름차순)로 정렬된 PhotoAnalysis 리스트. 사진이 없으면 빈 리스트.
    """
    images = _list_images(source)
    if not images:
        log.info("[photo] 분석할 이미지 없음")
        return []

    if model is None or timeout is None:
        try:
            from config import settings as _s
            if model is None:
                model = getattr(_s, "PHOTO_VISION_MODEL", "claude")
            if timeout is None:
                timeout = int(getattr(_s, "PHOTO_VISION_TIMEOUT", 180))
        except (ImportError, AttributeError, TypeError, ValueError):
            model = model or "claude"
            timeout = timeout or 180

    resolved_model = _resolve_model(model)

    # EXIF 시각으로 시간순 정렬(값은 로그 미기록).
    with_time = [(img, _exif_datetime(img)) for img in images]
    with_time.sort(key=_sort_key)

    log.info(f"[photo] {len(with_time)}장 분석 시작 (백엔드={model}, "
             f"비전={'ON' if resolved_model else 'OFF(폴백)'})")

    results: List[PhotoAnalysis] = []
    for order, (img, taken) in enumerate(with_time, start=1):
        if stop_flag is not None and getattr(stop_flag, "is_set", lambda: False)():
            log.info("[photo] 중지 요청 — 분석 조기 종료")
            break
        analysis = analyze_photo(
            img, order=order, model=resolved_model, timeout=int(timeout),
            taken_at=taken, stop_flag=stop_flag,
        )
        results.append(analysis)

    ok = sum(1 for a in results if a.vision_ok)
    log.info(f"[photo] 분석 완료: {len(results)}장 (비전 성공 {ok} · 폴백 {len(results) - ok})")
    return results
