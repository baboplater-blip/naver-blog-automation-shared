# core/codex_image.py
# 🎨 Codex CLI 이미지 생성 엔진 — 요청별 산출물 수확(provenance-verified harvest)
# ─────────────────────────────────────────────────────────────────────────────
# `codex exec` 의 내장 이미지 생성 도구(`image_gen.imagegen`)로 한글이 정확히 baked 된
# 썸네일·카드뉴스를 만든다. 젠스파크(브라우저 자동화) 경로를 대체하는 기본 엔진이다.
#
# ★이 모듈의 존재 이유 = 2026-09-09 실측으로 드러난 위조 사고를 구조적으로 막는 것.
#   "이미지를 만든 뒤 이 경로에 저장하라"고 지시했더니 codex 에이전트가 실제 생성물
#   (1,080,908 B)을 두고 **자기가 그린 30,484 B 대체본**을 그 경로에 저장하고
#   `RESULT_BYTES=30484` 로 성공을 단언했다. 호출자가 "파일이 존재하고 바이트가 0이 아니다"
#   만 확인하면 위조를 100% 놓친다.
#   → 그래서 이 모듈은 **에이전트에게 파일 저장을 시키지 않는다.**
#     ① 프롬프트에서 파일 쓰기·셸 실행을 명시적으로 금지하고 생성만 시킨다.
#     ② 호출 전 `generated_images/` 를 스냅샷해 두고, 호출 후 **차분(diff)** 으로 신규
#        산출물만 집는다.
#     ③ stdout 의 `session id:` 를 파싱해 **그 세션 디렉터리로 범위를 한정**한다.
#        (= "임의의 최신 파일 줍기"가 아니라 요청별 산출 경로 검증)
#     ④ 집은 파일은 매직바이트·해상도로 실이미지인지 확인한 뒤 목적지로 복사한다.
#
# 설계 원칙(전 경로 준수):
# - 권한검사 유지: 샌드박스는 `-s read-only` 로 충분하다(에이전트가 파일을 쓸 필요가 없다).
#   `--dangerously-bypass-approvals-and-sandbox` 는 **쓰지 않는다**.
# - 사용자 자료 보존: `~/.codex/config.toml` 을 절대 수정하지 않는다. 파손된 키는 런타임
#   `-c key=value` 오버라이드로만 우회한다(_sanitizing_overrides).
# - 유료 API 자동 전환 금지: 이 모듈은 오직 로그인된 `codex` CLI 만 쓴다. OpenAI/Google 등
#   과금 API 로 자동 폴백하지 않는다(폴백은 PIL 로컬 썸네일뿐 — 호출자 책임).
# - fail-open: 어떤 예외도 전파하지 않는다. 실패는 CodexImageResult.ok=False 로 보고한다.
# - 무인 생성 아님(정직 표기): 생성은 사용자의 로그인된 codex 구독 세션을 소비한다.
#   `probe()` 가 로그인·CLI 상태를 사실대로 보고하며, 준비되지 않으면 조용히 성공을
#   가장하지 않고 not_ready 로 거절한다.
#
# ⚠️ 엔진 표기 원칙: codex 의 이미지 도구는 백엔드 엔진명을 노출하지도, 지정할 수도 없다.
#    따라서 이 모듈은 산출물을 "GPT Image 2" 같은 특정 엔진으로 **주장하지 않는다**.
#    메타에는 관측 가능한 사실(`tool="codex.image_gen.imagegen"`)만 기록한다.
from __future__ import annotations

import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

try:  # 프로필 격리 로거(있으면 사용) — 없으면 표준 로깅 폴백.
    from config.logger import get_logger
    log = get_logger(__name__)
except Exception:  # noqa: BLE001 — config 미로드 환경(경량 테스트 스텁)
    import logging
    log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# 설정 접근 — settings.py 에 줄이 없어도 안전한 getattr 기본값
# ─────────────────────────────────────────────────────────────────────────────
def _setting(name: str, default: Any) -> Any:
    try:
        from config import settings as _s
        return getattr(_s, name, default)
    except Exception:  # noqa: BLE001 — settings 미로드면 기본값
        return default


# 계정에서 실제 사용 가능한 모델(2026-09-09 실측): gpt-5.5, gpt-5.3-codex-spark.
# 기존 IMAGE_CODEX_MODEL 기본값 "gpt-5.6" 은 400 `not supported when using Codex with a
# ChatGPT account` 로 거절되는 죽은 값이었다.
DEFAULT_CODEX_IMAGE_MODEL: str = "gpt-5.5"
DEFAULT_TIMEOUT_SEC: int = 600
DEFAULT_MAX_CALLS_PER_RUN: int = 40

# 산출물로 인정하는 확장자. ★실측: 파일명이 `ig_*.png` 만이 아니라 `exec-*.png` 도 있다
#   (generated_images/01a08189-.../exec-*.png). 기존 image_prompt_builder.latest_generated_image
#   는 rglob("ig_*.png") 라 exec-* 산출물을 통째로 놓쳤다 → 확장자 기준으로 잡는다.
_IMAGE_SUFFIXES: Tuple[str, ...] = (".png", ".jpg", ".jpeg", ".webp")

# 실이미지 최소 바이트 — 위조/깨진 산출물 1차 차단(실측 정상값은 700KB~1.2MB).
_MIN_IMAGE_BYTES: int = 8 * 1024
# 실이미지 최소 한 변(px) — 아이콘·썸네일 부스러기 배제.
_MIN_IMAGE_SIDE: int = 256

_SESSION_ID_RE = re.compile(r"session\s*id\s*:\s*([0-9a-fA-F-]{8,})")
# codex 가 한도/쿼터로 막혔을 때 stdout/stderr 에 나타나는 신호.
_RATE_LIMIT_RE = re.compile(
    r"(rate[_ ]?limit|429|usage limit|quota|too many requests)", re.IGNORECASE)
# 로그인 만료/미로그인 신호.
_LOGIN_RE = re.compile(
    r"(not logged in|login required|unauthorized|401|please run .?codex login)", re.IGNORECASE)


# ─────────────────────────────────────────────────────────────────────────────
# codex 실행파일 해석
# ─────────────────────────────────────────────────────────────────────────────
def resolve_codex_executable(name: str = "codex") -> Optional[str]:
    """`codex` 실행파일 실제 경로. Windows npm `.cmd` 래퍼까지 해석한다.

    codex_cli_bot 의 검증된 해석기를 재사용하고, 임포트 불가 환경에서만 자체 폴백한다.
    """
    try:
        from core.codex_cli_bot import _resolve_codex_executable as _r
        found = _r(name)
        if found:
            return found
    except Exception:  # noqa: BLE001 — 자체 폴백으로 진행
        pass
    direct = shutil.which(name)
    if direct:
        return direct
    if sys.platform.startswith("win"):
        for ext in (".cmd", ".exe", ".bat", ".ps1"):
            found = shutil.which(f"{name}{ext}")
            if found:
                return found
    return None


# ─────────────────────────────────────────────────────────────────────────────
# codex 홈 / 산출물 루트
# ─────────────────────────────────────────────────────────────────────────────
def codex_home() -> Path:
    """CODEX_HOME 환경변수 우선, 없으면 ~/.codex."""
    raw = os.environ.get("CODEX_HOME", "").strip()
    if raw:
        return Path(raw)
    return Path.home() / ".codex"


def generated_images_root() -> Path:
    """codex 가 생성 이미지를 떨구는 루트 = {CODEX_HOME}/generated_images."""
    return codex_home() / "generated_images"


# ─────────────────────────────────────────────────────────────────────────────
# 파손된 사용자 config 우회 — 파일은 절대 수정하지 않는다
# ─────────────────────────────────────────────────────────────────────────────
# 알려진 키의 안전한 대체값. 실측: `service_tier="flex"` 는 CLI 파싱은 통과하지만 API 가
# 400 `Unsupported service_tier: flex` 로 거절한다 → "fast" 를 쓴다.
_OVERRIDE_PREFERENCE: Dict[str, str] = {"service_tier": "fast"}

_UNKNOWN_VARIANT_RE = re.compile(r"unknown variant `([^`]+)`, expected ([^\n]+)")
_CONFIG_LOC_RE = re.compile(r"([A-Za-z]:\\[^\s:]+\.toml|/[^\s:]+\.toml):(\d+):(\d+)")
_EXPECTED_TOKEN_RE = re.compile(r"`([^`]+)`")


def _config_key_at(path: Path, line_no: int) -> Optional[str]:
    """config.toml 의 지정 줄에서 `key = value` 의 key 를 읽는다(읽기 전용)."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:  # noqa: BLE001
        return None
    if not (1 <= line_no <= len(lines)):
        return None
    raw = lines[line_no - 1]
    if "=" not in raw:
        return None
    key = raw.split("=", 1)[0].strip().strip('"').strip("'")
    return key or None


def _sanitizing_overrides(err_text: str) -> List[str]:
    """config 역직렬화 실패 메시지에서 문제 키를 찾아 `-c key=value` 오버라이드를 만든다.

    사용자의 `~/.codex/config.toml` 을 **수정하지 않고** 런타임으로만 우회한다.
    (실측 사례: `service_tier = "priority"` 가 CLI 0.130.0 에서 unknown variant 로
     역직렬화 실패 → codex 의 모든 서브커맨드가 통째로 죽었다.)

    Returns: `["-c", "service_tier=fast"]` 같은 argv 조각. 못 고치면 빈 리스트.
    """
    text = str(err_text or "")
    m = _UNKNOWN_VARIANT_RE.search(text)
    if not m:
        return []
    expected_raw = m.group(2)
    loc = _CONFIG_LOC_RE.search(text)
    key: Optional[str] = None
    if loc:
        key = _config_key_at(Path(loc.group(1)), int(loc.group(2)))
    if not key:
        return []
    # 선호 대체값이 있으면 그것을, 없으면 expected 목록의 첫 후보를 쓴다.
    value = _OVERRIDE_PREFERENCE.get(key)
    if not value:
        tokens = _EXPECTED_TOKEN_RE.findall(expected_raw)
        value = tokens[0] if tokens else None
    if not value:
        return []
    return ["-c", f"{key}={value}"]


def _configured_overrides() -> List[str]:
    """settings.CODEX_IMAGE_CONFIG_OVERRIDES 로 지정한 강제 오버라이드(탈출구)."""
    raw = _setting("CODEX_IMAGE_CONFIG_OVERRIDES", "")
    out: List[str] = []
    if isinstance(raw, (list, tuple)):
        items = [str(x) for x in raw]
    else:
        items = [p for p in str(raw or "").split(",") if p.strip()]
    for item in items:
        item = item.strip()
        if item:
            out += ["-c", item]
    return out


# ─────────────────────────────────────────────────────────────────────────────
# 공유 호출 예산 — 여러 엔진(병렬 워커)이 하나의 상한을 나눠 쓴다
# ─────────────────────────────────────────────────────────────────────────────
class CallBudget:
    """배치 전체가 공유하는 생성 호출 상한(스레드 안전).

    ★왜 필요한가: 병렬 채움은 워커마다 **다른 엔진 인스턴스**를 쓴다(세션·임시 작업
    디렉터리를 공유하면 안 되므로). 엔진마다 `max_calls` 를 따로 세면 상한이 워커 수만큼
    곱해져 "이번 실행 40회" 약속이 조용히 80회가 된다. 그래서 상한은 엔진이 아니라 이
    객체가 갖고, 모든 워커가 같은 인스턴스를 공유한다.

    소비는 **호출 직전 1회**(`acquire`)만 일어난다 — 실패해서 되돌리지 않는다(이미 사용자
    구독을 소비했을 수 있으므로 낙관적 환불은 하지 않는다). 대신 호출을 시작하지 못한
    경로(codex 미설치 등)에서는 애초에 acquire 하지 않아 예산이 새지 않는다.
    """

    def __init__(self, limit: int) -> None:
        try:
            self.limit = max(0, int(limit))
        except (TypeError, ValueError):
            self.limit = 0
        self._used = 0
        self._lock = threading.Lock()

    @property
    def used(self) -> int:
        with self._lock:
            return self._used

    def remaining(self) -> Optional[int]:
        """남은 호출 수. limit=0(무제한)이면 None."""
        if not self.limit:
            return None
        with self._lock:
            return max(0, self.limit - self._used)

    def exhausted(self) -> bool:
        if not self.limit:
            return False
        with self._lock:
            return self._used >= self.limit

    def acquire(self) -> bool:
        """호출 1건을 예산에서 뺀다. 남은 게 없으면 False(호출하지 않는다)."""
        with self._lock:
            if self.limit and self._used >= self.limit:
                return False
            self._used += 1
            return True


# 목적지 파일명 선점 경쟁 방지 — 같은 out_dir 을 쓰는 병렬 워커가 동시에 같은 후보
# 이름을 고르지 않게 한다(슬롯 prefix 가 이미 서로 다르지만, 재생성 접미어까지 안전하게).
_DEST_LOCK = threading.Lock()


# ─────────────────────────────────────────────────────────────────────────────
# 결과 자료구조
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class CodexImageResult:
    """요청 1건의 결과. ok=True 일 때만 path 가 실제 산출물이다."""

    request_id: str
    ok: bool = False
    path: Optional[Path] = None
    session_id: Optional[str] = None
    source: Optional[Path] = None          # 수확 원본(generated_images 안)
    bytes: int = 0
    width: int = 0
    height: int = 0
    reason: str = ""                        # 실패 사유 코드
    detail: str = ""                        # 사람이 읽는 부연
    elapsed_seconds: float = 0.0
    # 관측 가능한 사실만 기록한다 — 백엔드 엔진명은 확인 불가이므로 주장하지 않는다.
    tool: str = "codex.image_gen.imagegen"

    def as_meta(self) -> Dict[str, Any]:
        return {
            "request_id": self.request_id,
            "ok": self.ok,
            "path": str(self.path) if self.path else None,
            "session_id": self.session_id,
            "source": str(self.source) if self.source else None,
            "bytes": self.bytes,
            "width": self.width,
            "height": self.height,
            "reason": self.reason,
            "elapsed_seconds": round(self.elapsed_seconds, 2),
            "tool": self.tool,
        }


# ─────────────────────────────────────────────────────────────────────────────
# 산출물 스냅샷 / 차분 수확
# ─────────────────────────────────────────────────────────────────────────────
def _iter_image_files(root: Path) -> List[Path]:
    if not root.exists():
        return []
    out: List[Path] = []
    try:
        for p in root.rglob("*"):
            try:
                if p.is_file() and p.suffix.lower() in _IMAGE_SUFFIXES:
                    out.append(p)
            except OSError:
                continue
    except Exception:  # noqa: BLE001 — 스캔 실패는 빈 목록(호출자 fail-open)
        return out
    return out


def snapshot_generated(root: Optional[Path] = None, *,
                       session_id: Optional[str] = None) -> Set[str]:
    """호출 **전** generated_images 아래 이미지 파일 경로 집합을 찍는다.

    ★D4(2026-09-09): `session_id` 가 주어지고 그 세션 디렉터리가 이미 존재하면 스캔
    범위를 그 디렉터리 하나로 한정한다. 카드 2번째 이후·교정(resume) 호출은 세션이 이미
    알려져 있으므로, 매 호출마다 `~/.codex/generated_images` 루트 전체를 rglob 할 필요가
    없다(개선 전: 첫 호출이든 재개 호출이든 항상 루트 전체 스캔 — 세션이 쌓일수록 매
    카드 생성이 점점 느려졌다). 세션 디렉터리가 아직 없으면(최초 호출 등) 루트 전체
    폴백은 그대로 유지한다 — 귀속 규칙(before·세션 스코프·min_mtime)은 완화하지 않는다.
    """
    root = Path(root) if root is not None else generated_images_root()
    scope = root
    if session_id:
        candidate = root / str(session_id)
        if candidate.exists():
            scope = candidate
    return {str(p) for p in _iter_image_files(scope)}


def _png_size(data: bytes) -> Tuple[int, int]:
    try:
        if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
            w, h = struct.unpack(">II", data[16:24])
            return int(w), int(h)
    except Exception:  # noqa: BLE001
        pass
    return 0, 0


def _looks_like_real_image(path: Path) -> Tuple[bool, int, int, int, str]:
    """매직바이트·크기(·PNG 한정 해상도)로 실이미지인지 확인.

    ⚠️ 정직한 한계: 해상도 검사는 **PNG 에서만** 이뤄진다(IHDR 를 헤더에서 바로 읽을 수
    있어서다). JPEG/WEBP 는 매직바이트와 최소 바이트만 검사하며 width/height 는 0 으로
    남는다. codex 산출물은 실측상 전부 PNG 라 현재 실사용 공백은 없지만, 이 함수가
    "모든 포맷에서 해상도를 보장한다"고 오해하면 안 된다.

    Returns: (ok, bytes, width, height, reason)
    """
    try:
        size = path.stat().st_size
    except OSError:
        return False, 0, 0, 0, "stat_failed"
    if size < _MIN_IMAGE_BYTES:
        return False, size, 0, 0, "too_small"
    try:
        with open(path, "rb") as fh:
            head = fh.read(32)
    except OSError:
        return False, size, 0, 0, "read_failed"
    is_png = head[:8] == b"\x89PNG\r\n\x1a\n"
    is_jpg = head[:3] == b"\xff\xd8\xff"
    is_webp = head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    if not (is_png or is_jpg or is_webp):
        return False, size, 0, 0, "not_an_image"
    w = h = 0
    if is_png:
        w, h = _png_size(head)
        if w and h and min(w, h) < _MIN_IMAGE_SIDE:
            return False, size, w, h, "resolution_too_low"
    return True, size, w, h, ""


def harvest_new_images(before: Set[str], *, session_id: Optional[str] = None,
                       min_mtime: Optional[float] = None,
                       root: Optional[Path] = None) -> List[Path]:
    """호출 **후** 신규 산출물만 집는다 — 요청별 경로 검증의 핵심.

    ① `before` 스냅샷에 없던 파일만  ② session_id 가 있으면 그 세션 디렉터리 안만
    ③ min_mtime 이후에 만들어진 것만. 세 조건을 모두 만족하는 파일만 이 요청의 산출물로
    인정한다. 어떤 조건도 "가장 최신 파일"에 기대지 않는다.

    Returns: mtime 오름차순 신규 이미지 경로 목록(요청이 여러 장을 만들었을 수도 있다).
    """
    root = Path(root) if root is not None else generated_images_root()
    scope = root
    if session_id:
        candidate = root / str(session_id)
        if candidate.exists():
            scope = candidate
        # 세션 디렉터리가 아직 없으면 루트 차분으로 폴백(디렉터리 생성 지연 대비).
    fresh: List[Tuple[float, Path]] = []
    for p in _iter_image_files(scope):
        if str(p) in before:
            continue
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if min_mtime is not None and mtime < (min_mtime - 2.0):
            # 2초 여유 — 파일시스템 시계 오차 흡수.
            continue
        fresh.append((mtime, p))
    fresh.sort(key=lambda t: t[0])
    return [p for _, p in fresh]


# ─────────────────────────────────────────────────────────────────────────────
# 프롬프트 조립 — 저장·셸 금지가 위조 차단의 1차 방어선
# ─────────────────────────────────────────────────────────────────────────────
_RATIO_HINT: Dict[str, str] = {
    "1:1": "a perfect square image (1:1 aspect ratio)",
    "2:3": "a vertical portrait image (2:3 aspect ratio, taller than wide)",
    "3:2": "a horizontal landscape image (3:2 aspect ratio)",
    "4:5": "a vertical portrait image (4:5 aspect ratio)",
    "16:9": "a wide landscape image (16:9 aspect ratio)",
}

_WORKER_CONTRACT: str = (
    "You are an image generation worker. Follow these rules exactly:\n"
    "1. Use your image generation tool to create EXACTLY ONE image.\n"
    "2. Do NOT write, copy, move or save any file yourself.\n"
    "3. Do NOT run any shell command.\n"
    "4. Do NOT search the web.\n"
    "5. After the image is generated, reply with the single line: IMAGE_DONE\n"
)


def build_worker_prompt(spec: str, *, ratio: str = "1:1") -> str:
    """이미지 명세를 codex 워커 프롬프트로 감싼다.

    ★저장·셸 금지가 핵심이다. 저장을 시키면 에이전트가 실제 생성물 대신 자기가 그린
      대체본을 저장하는 사고가 실측으로 확인됐다(모듈 docstring 참조).
    """
    shape = _RATIO_HINT.get(str(ratio or "1:1"), _RATIO_HINT["1:1"])
    return (
        f"{_WORKER_CONTRACT}\n"
        f"Compose {shape}.\n\n"
        f"Image specification:\n{str(spec or '').strip()}\n"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 엔진
# ─────────────────────────────────────────────────────────────────────────────
class CodexImageEngine:
    """`codex exec` 기반 이미지 생성기. 세션을 이어 팔레트 일관성을 얻는다.

    수명주기: `open()` → `generate()/regenerate()` → `close()`.
    draft_image_fill 의 젠스파크 엔진과 **같은 메서드 계약**을 구현하므로 교체 가능하다.

    세션 연속성(실측): `codex exec resume <session_id>` 는 같은 세션 디렉터리에 이어
    산출물을 떨군다. 카드 N장을 한 세션에서 만들면 앞 이미지가 레퍼런스로 이어져
    팔레트·레이아웃이 일관되고, 교정(재생성)도 같은 맥락에서 이뤄진다.
    """

    def __init__(self, *, out_dir: Path, model: Optional[str] = None,
                 timeout: Optional[int] = None,
                 executable: Optional[str] = None,
                 runner: Optional[Callable[..., Any]] = None,
                 max_calls: Optional[int] = None,
                 journal: Optional["RequestJournal"] = None,
                 budget: Optional["CallBudget"] = None) -> None:
        self.out_dir = Path(out_dir)
        self.account: Optional[str] = None      # 젠스파크 계약 호환(코덱스는 계정 개념 없음)
        self.last_thread_url: Optional[str] = None
        self.model = str(model or _setting("CODEX_IMAGE_MODEL", DEFAULT_CODEX_IMAGE_MODEL))
        self.timeout = int(timeout or _setting("CODEX_IMAGE_TIMEOUT", DEFAULT_TIMEOUT_SEC))
        self.max_calls = int(
            max_calls if max_calls is not None
            else _setting("CODEX_IMAGE_MAX_CALLS_PER_RUN", DEFAULT_MAX_CALLS_PER_RUN))
        self._executable = executable
        self._runner = runner or subprocess.run
        self._overrides: List[str] = []
        self._overrides_checked = False
        self._calls = 0
        self._session_id: Optional[str] = None       # 썸네일/단건 세션
        self._cards_session_id: Optional[str] = None  # 카드 배치 세션
        self._cards_ratio: str = "1:1"
        self._last_spec: str = ""
        self._last_ratio: str = "1:1"
        self._workdir: Optional[Path] = None
        self._tempdir: Optional[tempfile.TemporaryDirectory] = None
        self.journal = journal
        # 공유 예산이 주어지면 상한 판정을 그쪽에 위임한다(배치 전역 1개). 없으면 예전처럼
        # 인스턴스 카운터(self._calls)로 판정 — 단일 엔진 경로의 동작은 바이트 그대로다.
        self.budget = budget
        self.last_reason: str = ""

    # ── 준비 ────────────────────────────────────────────────────────────────
    def _resolve_exe(self) -> Optional[str]:
        if self._executable is None:
            self._executable = resolve_codex_executable("codex")
        return self._executable

    def _ensure_overrides(self) -> None:
        """사용자 config 파손 여부를 1회 확인해 런타임 우회 오버라이드를 준비한다."""
        if self._overrides_checked:
            return
        self._overrides_checked = True
        forced = _configured_overrides()
        if forced:
            self._overrides = forced
            return
        exe = self._resolve_exe()
        if not exe:
            return
        try:
            proc = self._runner([exe, "login", "status"], capture_output=True, text=True,
                                timeout=60, encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 — 프리플라이트 실패는 무해(본 호출에서 다시 드러남)
            return
        combined = f"{getattr(proc, 'stdout', '') or ''}\n{getattr(proc, 'stderr', '') or ''}"
        if "Error loading configuration" in combined or "unknown variant" in combined:
            self._overrides = _sanitizing_overrides(combined)
            if self._overrides:
                log.info(
                    "[codex_image] 사용자 config 파손 감지 — 런타임 오버라이드로 우회"
                    f"({' '.join(self._overrides)}). config.toml 은 수정하지 않는다.")

    def _ensure_workdir(self) -> Path:
        if self._workdir is None:
            self._tempdir = tempfile.TemporaryDirectory(prefix="codex_img_")
            self._workdir = Path(self._tempdir.name)
        return self._workdir

    def open(self) -> str:
        """엔진 준비 상태 확인. Returns: "ok"|"login_required"|"rate_limited"|"error"."""
        state = probe(runner=self._runner, executable=self._resolve_exe())
        self.last_reason = state.get("reason", "")
        if state.get("ok"):
            self._overrides = list(state.get("overrides") or [])
            self._overrides_checked = True
            return "ok"
        # 지속성 장애(설치 없음·config 파손·미로그인·한도)는 사유를 그대로 흘려보낸다 —
        # 배치가 25편에 걸쳐 같은 실패를 반복하지 않고 조기 종료할 수 있게 하기 위함.
        reason = state.get("reason", "error")
        if reason in ("login_required", "rate_limited", "codex_not_found", "config_broken"):
            return reason
        return "error"

    def close(self) -> None:
        try:
            if self._tempdir is not None:
                self._tempdir.cleanup()
        except Exception:  # noqa: BLE001 — 임시 디렉터리 정리 실패는 무해
            pass
        self._tempdir = None
        self._workdir = None

    # ── 호출 ────────────────────────────────────────────────────────────────
    def _build_argv(self, *, resume_session: Optional[str]) -> List[str]:
        exe = self._resolve_exe()
        if not exe:
            raise FileNotFoundError("codex 실행파일을 찾지 못했습니다.")
        argv: List[str] = [exe]
        argv += list(self._overrides)
        argv += ["exec"]
        if self.model:
            argv += ["-m", self.model]
        # 권한검사 유지 — 에이전트는 파일을 쓸 필요가 없으므로 최소 권한으로 고정한다.
        argv += ["-s", "read-only", "--skip-git-repo-check", "-C", str(self._ensure_workdir())]
        if resume_session:
            argv += ["resume", str(resume_session)]
        argv += ["-"]
        return argv

    def _invoke(self, prompt: str, *, resume_session: Optional[str]) -> Tuple[int, str, str]:
        argv = self._build_argv(resume_session=resume_session)
        proc = self._runner(argv, input=prompt, capture_output=True, text=True,
                            timeout=self.timeout, encoding="utf-8", errors="replace")
        return (int(getattr(proc, "returncode", 1) or 0),
                getattr(proc, "stdout", "") or "",
                getattr(proc, "stderr", "") or "")

    def _generate_once(self, spec: str, *, ratio: str, prefix: str,
                       resume_session: Optional[str]) -> CodexImageResult:
        """1회 호출 = 1장. 스냅샷 → 호출 → 세션 범위 차분 수확 → 검증 → 복사."""
        request_id = f"{prefix}-{uuid.uuid4().hex[:10]}"
        result = CodexImageResult(request_id=request_id)
        started = time.time()

        if self._budget_exhausted():
            result.reason = "max_calls_per_run"
            limit = self.budget.limit if self.budget is not None else self.max_calls
            result.detail = f"이번 실행의 호출 상한({limit})에 도달했습니다."
            return result

        self._ensure_overrides()
        if not self._resolve_exe():
            result.reason = "codex_not_found"
            result.detail = "codex 실행파일을 찾지 못했습니다."
            return result

        root = generated_images_root()
        # D4: resume_session 이 있으면(카드 2번째 이후·교정) 스냅샷 범위를 그 세션
        # 디렉터리로 한정 — 세션을 모르는 최초 호출만 루트 전체 폴백을 탄다.
        before = snapshot_generated(root, session_id=resume_session)
        t_before = time.time()
        prompt = build_worker_prompt(spec, ratio=ratio)

        if self.journal is not None:
            self.journal.record_started(request_id, prefix=prefix,
                                        out_dir=str(self.out_dir), ratio=ratio)

        if not self._consume_budget():
            # 상한 검사와 실제 호출 사이에 다른 워커가 마지막 한 칸을 가져갔다.
            result.reason = "max_calls_per_run"
            result.detail = "다른 워커가 이번 실행의 호출 상한을 먼저 소진했습니다."
            self._finish_journal(result, started)
            return result
        try:
            rc, out, err = self._invoke(prompt, resume_session=resume_session)
        except subprocess.TimeoutExpired:
            result.reason = "timeout"
            result.detail = f"codex 호출이 {self.timeout}초를 넘겼습니다."
            self._finish_journal(result, started)
            return result
        except FileNotFoundError:
            result.reason = "codex_not_found"
            self._finish_journal(result, started)
            return result
        except Exception as exc:  # noqa: BLE001 — 전 경로 fail-open
            result.reason = "invoke_failed"
            result.detail = type(exc).__name__
            self._finish_journal(result, started)
            return result

        combined = f"{out}\n{err}"
        session_id = None
        m = _SESSION_ID_RE.search(combined)
        if m:
            session_id = m.group(1)
        result.session_id = session_id

        # 수확은 rc 와 무관하게 시도한다 — 이미지가 만들어진 뒤 후처리에서 rc 가 튀는
        # 경우가 있어, 실제 산출물이 있으면 살린다(있는 것을 버리지 않는다).
        found = harvest_new_images(before, session_id=session_id,
                                   min_mtime=t_before, root=root)
        if not found:
            if _RATE_LIMIT_RE.search(combined):
                result.reason = "rate_limited"
            elif _LOGIN_RE.search(combined):
                result.reason = "login_required"
            elif rc != 0:
                result.reason = "codex_error"
            else:
                result.reason = "no_image_produced"
            result.detail = _tail(combined, 300)
            self._finish_journal(result, started)
            return result

        # 세션 ID 를 못 읽었으면 범위를 세션 디렉터리로 좁히지 못하고 루트 차분만 남는다.
        # 이때 신규 파일이 2개 이상이면 **다른 codex 세션이 동시에 만든 이미지**가 섞였을 수
        # 있어 어느 것이 이 요청의 산출물인지 귀속할 수 없다 → 추측해서 엉뚱한 이미지를
        # 글에 싣느니 실패로 보고한다(요청별 산출 경로 검증의 마지막 빈틈 차단).
        if not session_id and len(found) > 1:
            result.reason = "ambiguous_output"
            result.detail = (f"세션 식별 실패 + 신규 이미지 {len(found)}장 — "
                             "요청 귀속 불가로 채택하지 않음")
            self._finish_journal(result, started)
            return result

        source = found[-1]  # 같은 요청에서 여러 장이면 마지막(최종본)을 채택
        ok, size, w, h, why = _looks_like_real_image(source)
        if not ok:
            result.reason = f"invalid_image:{why}"
            result.detail = f"{source.name} ({size} bytes)"
            self._finish_journal(result, started)
            return result

        dest = self._destination(prefix, source.suffix)
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dest)   # 원본은 보존한다(사용자 자료 불변)
        except Exception as exc:  # noqa: BLE001
            result.reason = "copy_failed"
            result.detail = type(exc).__name__
            self._finish_journal(result, started)
            return result

        result.ok = True
        result.path = dest
        result.source = source
        result.bytes = size
        result.width = w
        result.height = h
        self._finish_journal(result, started)
        return result

    def _budget_exhausted(self) -> bool:
        """호출을 시작하기 전 상한 판정(공유 예산 우선, 없으면 인스턴스 카운터)."""
        if self.budget is not None:
            return self.budget.exhausted()
        return bool(self.max_calls) and self._calls >= self.max_calls

    def _consume_budget(self) -> bool:
        """호출 1건을 예산에서 뺀다. 공유 예산이면 원자적으로(다른 워커와 경쟁 안전)."""
        self._calls += 1
        if self.budget is None:
            return True
        return self.budget.acquire()

    def _finish_journal(self, result: CodexImageResult, started: float) -> None:
        result.elapsed_seconds = time.time() - started
        if self.journal is not None:
            self.journal.record_finished(result)

    def _destination(self, prefix: str, suffix: str) -> Path:
        """요청별 목적지. 같은 prefix 가 겹치면 번호를 올려 기존 파일을 덮지 않는다.

        병렬 워커가 같은 out_dir 을 볼 수 있으므로 후보 선택 구간을 락으로 감싼다
        (슬롯 prefix 는 이미 서로 다르지만, 교정 재생성 접미어까지 안전하게 만든다).
        """
        suffix = suffix if suffix.lower() in _IMAGE_SUFFIXES else ".png"
        with _DEST_LOCK:
            base = self.out_dir / f"{prefix}{suffix}"
            if not base.exists():
                return base
            for n in range(2, 100):
                candidate = self.out_dir / f"{prefix}_{n:02d}{suffix}"
                if not candidate.exists():
                    return candidate
            return self.out_dir / f"{prefix}_{uuid.uuid4().hex[:6]}{suffix}"

    # ── 젠스파크 엔진과 동일한 공개 계약 ────────────────────────────────────
    def generate(self, prompt: str, *, ratio: str, prefix: str) -> Optional[Path]:
        """단건 생성(썸네일 경로). 새 세션에서 만든다."""
        res = self._generate_once(prompt, ratio=ratio, prefix=prefix, resume_session=None)
        self.last_reason = res.reason
        if res.ok:
            self._session_id = res.session_id
            self._last_spec = prompt
            self._last_ratio = ratio
            self.last_thread_url = f"codex-session:{res.session_id}" if res.session_id else None
            return res.path
        log.warning(f"[codex_image] 생성 실패({res.reason}) prefix={prefix}")
        return None

    def regenerate(self, correction_prompt: str, *, prefix: str) -> Optional[Path]:
        """직전 세션을 이어 교정본을 만든다(세션 없으면 원 명세+교정으로 새로 생성)."""
        if self._session_id:
            res = self._generate_once(correction_prompt, ratio=self._last_ratio,
                                      prefix=prefix, resume_session=self._session_id)
        else:
            merged = f"{self._last_spec}\n\n[Correction]\n{correction_prompt}"
            res = self._generate_once(merged, ratio=self._last_ratio,
                                      prefix=prefix, resume_session=None)
        self.last_reason = res.reason
        if res.ok:
            self._session_id = res.session_id or self._session_id
            return res.path
        log.warning(f"[codex_image] 재생성 실패({res.reason}) prefix={prefix}")
        return None

    def open_cards_session(self, *, ratio: str) -> bool:
        """카드 배치 세션 준비. codex 는 첫 카드 생성 시 세션이 열리므로 예약만 한다."""
        self._cards_session_id = None
        self._cards_ratio = str(ratio or "1:1")
        return True

    def generate_in_session(self, prompt: str, *, prefix: str) -> Optional[Path]:
        """카드 세션에서 1장. 2번째부터는 같은 세션을 resume 해 팔레트를 잇는다."""
        res = self._generate_once(prompt, ratio=self._cards_ratio, prefix=prefix,
                                  resume_session=self._cards_session_id)
        self.last_reason = res.reason
        if res.ok:
            if res.session_id:
                self._cards_session_id = res.session_id
                self.last_thread_url = f"codex-session:{res.session_id}"
            self._last_spec = prompt
            self._last_ratio = self._cards_ratio
            self._session_id = res.session_id or self._session_id
            return res.path
        log.warning(f"[codex_image] 카드 생성 실패({res.reason}) prefix={prefix}")
        return None

    def regenerate_in_session(self, correction_prompt: str, *, prefix: str) -> Optional[Path]:
        """카드 세션(같은 맥락)에서 교정본."""
        res = self._generate_once(correction_prompt, ratio=self._cards_ratio, prefix=prefix,
                                  resume_session=self._cards_session_id)
        self.last_reason = res.reason
        if res.ok:
            if res.session_id:
                self._cards_session_id = res.session_id
            return res.path
        log.warning(f"[codex_image] 카드 재생성 실패({res.reason}) prefix={prefix}")
        return None


def _tail(text: str, n: int) -> str:
    text = str(text or "").strip()
    return text[-n:] if len(text) > n else text


# ─────────────────────────────────────────────────────────────────────────────
# 요청 저널 — 내구성/재개
# ─────────────────────────────────────────────────────────────────────────────
def _journal_path() -> Path:
    try:
        from config import profile as _profile
        return Path(_profile.logs_dir()) / "codex_image_requests.json"
    except Exception:  # noqa: BLE001 — profile 미로드 시 코드 루트 폴백
        return Path(__file__).resolve().parent.parent / "logs" / "codex_image_requests.json"


# 재개 대상에서 제외할 종료 상태들.
_TERMINAL_STATUSES: Tuple[str, ...] = ("ok", "recovered", "already_present")


class RequestJournal:
    """요청별 내구성 기록. 중단된 요청의 산출물을 나중에 재수확할 수 있게 한다.

    생성 호출은 수십 초~수 분이 걸린다. 호출 직후 프로세스가 죽으면 이미 만들어진(그리고
    사용자 구독을 이미 소비한) 산출물이 버려진다. 저널은 요청 시각·세션·목적지를 남겨
    `resume_pending()` 이 그 산출물을 다시 집어 목적지로 옮길 수 있게 한다.
    """

    MAX_RECORDS: int = 500

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path is not None else _journal_path()
        # ★원자 갱신 — 저널은 "읽고 → 고치고 → 통째로 쓴다". 병렬 워커가 같은 인스턴스를
        #   공유하면 이 구간이 겹쳐 lost update(먼저 쓴 워커의 started 기록 소멸)가 난다.
        #   재개(resume_pending)가 그 기록에 의존하므로, 이미 생성해 구독을 소비한
        #   산출물을 영영 못 줍게 되는 실손해다. 프로세스 간 경쟁은 이 락이 아니라
        #   draft_image_fill 의 image_fill.lock 이 막는다(범위를 정직히 적어 둔다).
        self._lock = threading.RLock()

    def _load(self) -> List[Dict[str, Any]]:
        try:
            from config import safe_load_json
            data = safe_load_json(self.path, default=[])
        except Exception:  # noqa: BLE001
            import json
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                data = []
        return data if isinstance(data, list) else []

    def _save(self, records: List[Dict[str, Any]]) -> None:
        records = records[-self.MAX_RECORDS:]
        try:
            from config import atomic_write_json
            self.path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(self.path, records)
        except Exception:  # noqa: BLE001 — 저널 실패가 생성을 막아선 안 된다
            log.debug("[codex_image] 저널 기록 실패 — 무시하고 진행")

    def record_started(self, request_id: str, *, prefix: str, out_dir: str,
                       ratio: str) -> None:
        with self._lock:
            records = self._load()
            records.append({
                "request_id": request_id, "status": "started", "prefix": prefix,
                "out_dir": out_dir, "ratio": ratio, "started_at": time.time(),
                "snapshot_at": time.time(),
            })
            self._save(records)

    def record_finished(self, result: CodexImageResult) -> None:
        with self._lock:
            records = self._load()
            for rec in reversed(records):
                if rec.get("request_id") == result.request_id:
                    rec.update({
                        "status": "ok" if result.ok else "failed",
                        "session_id": result.session_id,
                        "path": str(result.path) if result.path else None,
                        "source": str(result.source) if result.source else None,
                        "reason": result.reason,
                        "finished_at": time.time(),
                    })
                    break
            self._save(records)

    def pending(self) -> List[Dict[str, Any]]:
        """수확되지 못한 채 세션만 남은 요청들(재개 후보)."""
        out = []
        for rec in self._load():
            # 이미 끝난 상태(성공·회수 완료·목적지에 이미 존재)는 재개 후보가 아니다.
            if rec.get("status") in _TERMINAL_STATUSES:
                continue
            if not rec.get("session_id"):
                continue
            if rec.get("reason") in ("codex_not_found", "max_calls_per_run"):
                continue
            out.append(rec)
        return out

    def resume_pending(self) -> Dict[str, Any]:
        """중단된 요청의 세션 디렉터리를 다시 훑어 산출물을 목적지로 회수한다.

        새 생성 호출을 하지 않는다(구독을 추가 소비하지 않는다). 이미 만들어진 것만 줍는다.
        """
        with self._lock:
            return self._resume_pending_locked()

    def _resume_pending_locked(self) -> Dict[str, Any]:
        root = generated_images_root()
        recovered: List[str] = []
        records = self._load()
        changed = False
        for rec in records:
            if rec.get("status") in _TERMINAL_STATUSES or not rec.get("session_id"):
                continue
            session_dir = root / str(rec["session_id"])
            if not session_dir.exists():
                continue
            started = float(rec.get("snapshot_at") or rec.get("started_at") or 0)
            # ★stat() 은 반드시 감싼다 — _iter_image_files 가 목록을 만든 직후 파일이
            #   사라질 수 있고(사용자 정리·백신 격리), 무방비 stat() 은 OSError 를 그대로
            #   전파해 이 모듈이 약속한 "어떤 예외도 전파하지 않는다"를 깨뜨린다.
            dated: List[Tuple[float, Path]] = []
            for p in _iter_image_files(session_dir):
                try:
                    mtime = p.stat().st_mtime
                except OSError:
                    continue
                if mtime >= (started - 2.0):
                    dated.append((mtime, p))
            if not dated:
                continue
            dated.sort(key=lambda t: t[0])
            source = dated[-1][1]
            ok, size, w, h, _why = _looks_like_real_image(source)
            if not ok:
                continue
            out_dir = Path(str(rec.get("out_dir") or ""))
            if not str(out_dir):
                continue
            dest = out_dir / f"{rec.get('prefix') or 'recovered'}{source.suffix}"
            # 목적지에 이미 파일이 있으면 덮어쓰지 않는다(기존 이미지 보존). 다만 그때는
            # 실제로 회수한 게 아니므로 "recovered" 라고 부르지 않는다 — 정직 표기.
            already = dest.exists()
            if not already:
                try:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, dest)
                except Exception:  # noqa: BLE001
                    continue
            rec.update({"status": "already_present" if already else "recovered",
                        "path": str(dest), "source": str(source),
                        "recovered_at": time.time()})
            if not already:
                recovered.append(str(dest))
                # 출처 기록(2026-09-23) — 이 파일은 **이 세션이 실제로 만든 산출물**을
                # 세션 디렉터리·요청 시각·매직바이트 검사로 귀속해 방금 회수한 것이다.
                # (목적지에 이미 있던 파일 ``already`` 은 이번 회수의 증거가 아니므로
                #  기록하지 않는다 — 존재한다는 이유로 'generated' 로 올리지 않는다.)
                try:
                    from core import image_asset_guard as _guard
                    _guard.mark(dest, _guard.KIND_GENERATED,
                                producer="codex_image:resume_harvest",
                                slug=str(rec.get("slug") or ""))
                except Exception:  # noqa: BLE001 — 기록 실패가 회수를 되돌리지 않는다
                    log.debug("[codex_image] 회수 산출물 출처 기록 실패 — 회수는 유효")
            changed = True
        if changed:
            self._save(records)
        return {"recovered": len(recovered), "paths": recovered}


# ─────────────────────────────────────────────────────────────────────────────
# 프리플라이트 — 정직한 준비 상태 보고
# ─────────────────────────────────────────────────────────────────────────────
def probe(*, runner: Optional[Callable[..., Any]] = None,
          executable: Optional[str] = None) -> Dict[str, Any]:
    """codex 이미지 생성 준비 상태를 사실대로 보고한다(생성은 하지 않는다).

    Returns:
        {"ok": bool, "reason": str, "detail": str, "executable": str|None,
         "overrides": [...], "model": str, "logged_in": bool}
    """
    run = runner or subprocess.run
    exe = executable or resolve_codex_executable("codex")
    model = str(_setting("CODEX_IMAGE_MODEL", DEFAULT_CODEX_IMAGE_MODEL))
    state: Dict[str, Any] = {
        "ok": False, "reason": "", "detail": "", "executable": exe,
        "overrides": [], "model": model, "logged_in": False,
    }
    if not exe:
        state["reason"] = "codex_not_found"
        state["detail"] = ("codex CLI 를 찾지 못했습니다. `npm i -g @openai/codex` 설치 후 "
                           "새 터미널에서 다시 시도하세요.")
        return state

    overrides = _configured_overrides()

    def _run_status(extra: List[str]) -> Tuple[str, str]:
        try:
            proc = run([exe] + extra + ["login", "status"], capture_output=True, text=True,
                       timeout=60, encoding="utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001
            return "", type(exc).__name__
        return (getattr(proc, "stdout", "") or "", getattr(proc, "stderr", "") or "")

    out, err = _run_status(overrides)
    combined = f"{out}\n{err}"
    if "Error loading configuration" in combined or "unknown variant" in combined:
        fixed = _sanitizing_overrides(combined)
        if fixed:
            overrides = fixed
            out, err = _run_status(overrides)
            combined = f"{out}\n{err}"
    state["overrides"] = overrides

    if "Error loading configuration" in combined:
        state["reason"] = "config_broken"
        state["detail"] = ("codex 설정 파일을 읽지 못했습니다: " + _tail(combined, 200))
        return state
    if "Logged in" not in combined:
        state["reason"] = "login_required"
        state["detail"] = ("codex 로그인이 필요합니다. 터미널에서 `codex login` 을 실행하세요. "
                           + _tail(combined, 160))
        return state

    state["logged_in"] = True
    state["ok"] = True
    state["reason"] = "ok"
    state["detail"] = "codex 로그인 확인됨 — 이미지 생성 준비 완료."
    return state


# ─────────────────────────────────────────────────────────────────────────────
# CLI — 진단·수동 생성·재개
# ─────────────────────────────────────────────────────────────────────────────
def _print(text: str) -> None:
    """cp949 콘솔에서 인코딩 불가 문자(em-dash 등)로 죽지 않는 안전 출력.

    한글은 cp949 로도 인코딩되므로 그대로 보존되고, 표현 불가 문자만 대체된다.
    (2026-08-06 schtasks cp949 사고와 같은 클래스 — 네이티브 콘솔 인코딩을 존중한다.)
    """
    try:
        print(text)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        print(text.encode(enc, errors="replace").decode(enc, errors="replace"))


def _cli(argv: Optional[Sequence[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        prog="python -m core.codex_image",
        description="Codex CLI 이미지 생성 — 진단/단건 생성/중단분 재개")
    ap.add_argument("--probe", action="store_true", help="준비 상태만 점검(생성 안 함)")
    ap.add_argument("--resume", action="store_true",
                    help="중단된 요청의 산출물을 재수확(새 생성 호출 없음)")
    ap.add_argument("--gen", metavar="SPEC", help="이미지 명세로 1장 생성")
    ap.add_argument("--ratio", default="1:1", help="비율(1:1, 2:3 …)")
    ap.add_argument("--out", default=".", help="저장 디렉터리")
    ap.add_argument("--prefix", default="codex_image", help="파일 이름 접두")
    args = ap.parse_args(list(argv) if argv is not None else None)

    if args.probe or not (args.gen or args.resume):
        state = probe()
        _print(f"codex 실행파일 : {state['executable']}")
        _print(f"오버라이드     : {' '.join(state['overrides']) or '(없음)'}")
        _print(f"모델           : {state['model']}")
        # cp949 콘솔에서 죽지 않도록 CLI 출력은 ASCII 마커만 쓴다(이모지 금지).
        _print(f"준비 상태      : {'[OK] ' if state['ok'] else '[FAIL] '}{state['reason']}")
        _print(f"설명           : {state['detail']}")
        return 0 if state["ok"] else 1

    if args.resume:
        summary = RequestJournal().resume_pending()
        _print(f"재수확: {summary['recovered']}건")
        for p in summary["paths"]:
            _print(f"  · {p}")
        return 0

    engine = CodexImageEngine(out_dir=Path(args.out), journal=RequestJournal())
    state = engine.open()
    if state != "ok":
        _print(f"[FAIL] 준비 실패: {state} ({engine.last_reason})")
        return 1
    try:
        path = engine.generate(args.gen, ratio=args.ratio, prefix=args.prefix)
    finally:
        engine.close()
    if path is None:
        _print(f"[FAIL] 생성 실패: {engine.last_reason}")
        return 1
    _print(f"[OK] 생성 완료: {path}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_cli())
