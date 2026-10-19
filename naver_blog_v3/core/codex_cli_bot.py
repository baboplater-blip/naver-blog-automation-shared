"""codex_cli_bot

`codex exec` 호출로 1편당 6장(또는 카테고리에 따라 7~8장) 이미지를 자동 생성한다.

운영 원칙:
- 모든 이미지 = 2048x2048 정사각형 (Day 1의 image_prompt_builder가 size를 박는다).
- 1편 기본 시퀀스: thumb x1, body x2, info x3 = 6장.
- 디지털 카테고리만 body 또는 info 1~2장 추가 허용 (`extra_recipes`로 확장).
- codex stdout 응답에 의존하지 않는다. `latest_generated_image()`가 진실의 단일 소스.
- Codex Plus 5시간 한도(25/30회)를 usage_log.json으로 추적해
  접근 임박 시 quota_warning, 초과 시 RuntimeError로 차단한다.

호출자(image_dispatcher)는 GenerationResult.failed_roles와 quota_warning만 보고
Tier 1 ↔ Tier 2 폴백을 결정한다.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Optional

# naver_blog_v3 패키지 절대 경로 import (Day 1 모듈 그대로 사용)
_PKG_ROOT = Path(__file__).resolve().parent.parent
_PROJECT_ROOT = _PKG_ROOT.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from naver_blog_v3.core.image_prompt_builder import (  # noqa: E402
    ImagePlan,
    build_image_prompt,
    build_infocard_prompt,
    latest_generated_image,
    plan_infocards,
    validate_image_plan,
)
from config.logger import get_logger  # noqa: E402

log = get_logger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# 상수
# ──────────────────────────────────────────────────────────────────────────────

DEFAULT_TIMEOUT_SEC: int = 240
DEFAULT_USAGE_LOG_PATH: Path = Path.home() / ".codex" / "usage_log.json"

QUOTA_WINDOW_HOURS: int = 5
QUOTA_WARNING_THRESHOLD: int = 25
QUOTA_HARD_LIMIT: int = 30

INTER_CALL_SLEEP_SEC: float = 1.5
DIGITAL_CATEGORY: str = "디지털"

# 인포카드 codex 이미지 생성에 명시할 모델. ChatGPT 계정에서 폐기된 모델
# (gpt-5.3-codex 등)이 config.toml/세션에 남아 400 을 내는 것을 우회한다. settings 로
# 오버라이드 가능(계정·CLI 버전에 따라 지원 모델이 다르므로 실측 후 조정).
# 2026-09-09 실측: 이 ChatGPT 계정에서 codex 가 실제로 받아주는 모델은 `gpt-5.5` 와
# `gpt-5.3-codex-spark` 뿐이다. 예전 기본값 "gpt-5.6" 은 400
# `not supported when using Codex with a ChatGPT account` 로 거절되는 죽은 값이었다.
_DEFAULT_CODEX_IMAGE_MODEL: str = "gpt-5.5"


def _resolve_codex_image_model() -> Optional[str]:
    """인포카드 codex 호출에 쓸 모델명. settings.IMAGE_CODEX_MODEL 우선, 없으면 기본값.

    빈 문자열/None 으로 설정하면 ``--model`` 을 붙이지 않아(codex 기본 모델 사용) 준다.
    """
    try:
        from config import settings as _s
        value = getattr(_s, "IMAGE_CODEX_MODEL", _DEFAULT_CODEX_IMAGE_MODEL)
    except Exception:  # noqa: BLE001 — settings 미로드면 기본값
        value = _DEFAULT_CODEX_IMAGE_MODEL
    text = str(value or "").strip()
    return text or None

# 참조 모델시트 복사 상한 — 사진/상품 기능의 10MB 상한과 동일 기준.
_MAX_MODEL_REF_BYTES: int = 10 * 1024 * 1024

# Windows에서 PATH 검색 시 우선 시도할 확장자 — PATHEXT 환경변수 미설정 시 폴백.
# 주의: 빈 문자열을 넣지 않는다. str.endswith(())는 빈 문자열을 항상 True로 매칭한다.
_WINDOWS_EXEC_EXTENSIONS: tuple[str, ...] = (".cmd", ".exe", ".bat", ".ps1")
_CODEX_NOT_FOUND_HINT: str = (
    "Windows 사용자는 npm 설치 후 새 셸 또는 명시적 .cmd 경로 권장 "
    "(예: %APPDATA%\\npm\\codex.cmd)"
)

BodyKind = Literal["five_step", "before_after", "self_diagnosis"]
InfoKind = Literal["faq", "compare", "checklist"]
RoleName = Literal["thumb", "body", "info"]


def _resolve_codex_executable(name: str) -> Optional[str]:
    """`name`이 단순 명칭이면 PATH에서 실제 실행파일을 해석한다.

    Windows에서 `shutil.which("codex")` 호출이 npm 설치된 `.cmd` 래퍼를 못 찾는
    회귀에 대비해 PATHEXT 우선순위 + 명시 폴백을 모두 시도한다.

    Args:
        name: codex 실행파일 명칭(또는 절대/상대 경로).

    Returns:
        해석된 실제 경로(문자열). 어떤 후보도 못 찾으면 None.
    """
    if not isinstance(name, str) or not name.strip():
        return None

    # 1) 그대로 시도 (절대경로 또는 PATH에 정확히 존재하는 케이스)
    direct = shutil.which(name)
    if direct:
        return direct

    # 2) Windows 폴백 — PATHEXT 환경변수에 기재된 확장자 + 보편 확장자를 차례로 시도
    if sys.platform.startswith("win") and not name.lower().endswith(
        _WINDOWS_EXEC_EXTENSIONS
    ):
        pathext_raw = os.environ.get("PATHEXT", "")
        pathext_exts = tuple(
            ext.strip().lower()
            for ext in pathext_raw.split(os.pathsep)
            if ext.strip()
        )
        # PATHEXT 우선, 그 뒤에 모듈 상수의 추가 폴백.
        seen: set[str] = set()
        ordered: list[str] = []
        for ext in pathext_exts + _WINDOWS_EXEC_EXTENSIONS:
            if ext and ext not in seen:
                seen.add(ext)
                ordered.append(ext)

        for ext in ordered:
            candidate = shutil.which(name + ext)
            if candidate:
                return candidate

    return None


# ──────────────────────────────────────────────────────────────────────────────
# 데이터 클래스
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class BodyRecipe:
    """body 역할 호출 1회분 데이터.

    Attributes:
        kind: 5단계 흐름 / Before-After / 자가 진단 중 선택.
        items: build_image_prompt(body_items=...)에 그대로 전달.
              body 역할은 body_items를 기본 빌더에선 안 받지만,
              illustration_hint로 변환해 합성한다.
        illustration_hint: 일러스트 자유 힌트(영문 권장).
    """
    kind: BodyKind
    items: list[dict[str, str]]
    illustration_hint: Optional[str] = None


@dataclass
class InfoRecipe:
    """info 역할 호출 1회분 데이터."""
    kind: InfoKind
    items: list[dict[str, str]]
    illustration_hint: Optional[str] = None


@dataclass
class GenerationResult:
    """1편 전체 생성 결과 — 호출자가 Tier 폴백 결정에 사용."""
    slug: str
    success_paths: list[Path] = field(default_factory=list)
    failed_roles: list[str] = field(default_factory=list)
    total_calls: int = 0
    elapsed_seconds: float = 0.0
    tokens_used_estimate: int = 0
    quota_warning: bool = False


@dataclass
class InfocardResult:
    """본문 분석형 인포카드 생성 결과.

    Attributes:
        slug: 글 슬러그.
        plan: Stage1 구성안(디버그·후속 소비용). 폴백이면 None 일 수 있다.
        image_paths: 생성 성공한 카드 PNG 경로(카드 순서).
        card_meta: 각 성공 카드의 메타(role/headline) — 후속 OCR·미리보기·승인 UI 소비.
        failed: 카드별 실패 사유("card2:timeout" 등).
        quota_warning: 한도 임박 여부.
        needs_fallback: True 면 인포카드 생성 불가 → 호출자가 옛 슬롯 경로 선택.
    """
    slug: str
    plan: Optional[ImagePlan] = None
    image_paths: list[Path] = field(default_factory=list)
    card_meta: list[dict] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    quota_warning: bool = False
    needs_fallback: bool = False


# ──────────────────────────────────────────────────────────────────────────────
# CodexCLIBot
# ──────────────────────────────────────────────────────────────────────────────


class CodexCLIBot:
    """`codex exec --skip-git-repo-check <prompt>` 호출 오케스트레이터."""

    def __init__(
        self,
        inbox_dir: Path,
        codex_executable: str = "codex",
        per_call_timeout: int = DEFAULT_TIMEOUT_SEC,
        usage_log_path: Optional[Path] = None,
        session_dir_root: Optional[Path] = None,
        runner: Optional[Callable[..., subprocess.CompletedProcess]] = None,
        image_picker: Optional[Callable[..., Path]] = None,
        sleep_fn: Optional[Callable[[float], None]] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        """초기화.

        Args:
            inbox_dir: 생성된 PNG를 리네임 복사할 인박스 폴더.
            codex_executable: PATH 또는 절대 경로의 codex 실행파일명.
            per_call_timeout: codex exec 1회 호출 timeout(초).
            usage_log_path: 한도 추적 JSON 경로. None이면 ~/.codex/usage_log.json.
            session_dir_root: latest_generated_image() 검색 루트. None이면 기본값.
            runner: subprocess.run 주입(테스트용).
            image_picker: latest_generated_image 주입(테스트용).
            sleep_fn: time.sleep 주입(테스트용).
            clock: datetime.now(tz=...) 주입(테스트용).
        """
        if not isinstance(inbox_dir, Path):
            raise TypeError("inbox_dir은 pathlib.Path여야 함")
        if per_call_timeout <= 0:
            raise ValueError("per_call_timeout은 양수여야 함")

        self.inbox_dir: Path = inbox_dir
        self.inbox_dir.mkdir(parents=True, exist_ok=True)

        self.codex_executable: str = codex_executable
        # 명시적 절대경로면 PATH 검색 없이 존재만 확인. 단순 명칭이면 PATHEXT 폴백 포함 해석.
        self._codex_resolved_path: Optional[str] = self._resolve_codex_path(
            codex_executable
        )
        self.per_call_timeout: int = per_call_timeout
        self.usage_log_path: Path = usage_log_path or DEFAULT_USAGE_LOG_PATH
        self.session_dir_root: Optional[Path] = session_dir_root

        self._runner: Callable[..., subprocess.CompletedProcess] = (
            runner or subprocess.run
        )
        self._image_picker: Callable[..., Path] = image_picker or latest_generated_image
        self._sleep: Callable[[float], None] = sleep_fn or time.sleep
        self._clock: Callable[[], datetime] = clock or (
            lambda: datetime.now(tz=timezone.utc)
        )

    # ──────────────────────────────────────────────────────────────────────
    # 실행파일 경로 해석
    # ──────────────────────────────────────────────────────────────────────

    @staticmethod
    def _resolve_codex_path(executable: str) -> Optional[str]:
        """codex 실행파일 경로를 1회 해석한다.

        - 절대/상대 경로 형식이면 존재 여부만 확인하고 그대로 통과.
        - 단순 명칭이면 _resolve_codex_executable로 PATH+PATHEXT 검색.

        Returns:
            해석 성공 시 실제 경로(str). 실패 시 None — 실제 호출 직전에
            FileNotFoundError를 던지도록 _invoke_codex에서 처리.
        """
        if not isinstance(executable, str) or not executable.strip():
            return None

        looks_like_path = (
            os.sep in executable
            or (os.altsep is not None and os.altsep in executable)
            or executable.lower().endswith(_WINDOWS_EXEC_EXTENSIONS)
        )
        if looks_like_path:
            # 명시 경로 — 존재만 검증, PATH 검색은 생략.
            if Path(executable).exists():
                return executable
            return None

        return _resolve_codex_executable(executable)

    # ──────────────────────────────────────────────────────────────────────
    # 한도 추적
    # ──────────────────────────────────────────────────────────────────────

    def _load_usage_log(self) -> dict[str, Any]:
        """usage_log.json 안전 로드. 파일 없거나 손상 시 빈 구조 반환."""
        if not self.usage_log_path.exists():
            return {"calls": []}
        try:
            with self.usage_log_path.open("r", encoding="utf-8") as fp:
                data = json.load(fp)
        except (OSError, json.JSONDecodeError):
            return {"calls": []}
        if not isinstance(data, dict) or not isinstance(data.get("calls"), list):
            return {"calls": []}
        return data

    def _save_usage_log(self, data: dict[str, Any]) -> None:
        """usage_log.json 원자적 저장 (tmp → rename)."""
        self.usage_log_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.usage_log_path.with_suffix(self.usage_log_path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fp:
            json.dump(data, fp, ensure_ascii=False, indent=2)
        tmp.replace(self.usage_log_path)

    def _count_recent_calls(self) -> int:
        """5시간 윈도우 내 호출 카운트."""
        data = self._load_usage_log()
        now = self._clock()
        threshold = now - timedelta(hours=QUOTA_WINDOW_HOURS)
        count = 0
        for entry in data["calls"]:
            ts_raw = entry.get("ts")
            if not isinstance(ts_raw, str):
                continue
            try:
                ts = datetime.fromisoformat(ts_raw)
            except ValueError:
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts > threshold:
                count += 1
        return count

    def _record_call(self, role: str, slug: str) -> None:
        """호출 1회를 usage_log에 추가."""
        data = self._load_usage_log()
        data["calls"].append({
            "ts": self._clock().isoformat(),
            "role": role,
            "slug": slug,
        })
        self._save_usage_log(data)

    def _check_quota_before_call(self) -> None:
        """호출 직전 한도 체크 — 30회 초과 시 RuntimeError."""
        recent = self._count_recent_calls()
        if recent >= QUOTA_HARD_LIMIT:
            raise RuntimeError(
                f"Codex CLI 5시간 한도 임박 — Tier 2 폴백 필요 "
                f"(최근 {recent}회 ≥ 한도 {QUOTA_HARD_LIMIT})"
            )

    # ──────────────────────────────────────────────────────────────────────
    # codex 호출
    # ──────────────────────────────────────────────────────────────────────

    def _invoke_codex(
        self, prompt: str, cwd: Optional[str] = None, model: Optional[str] = None
    ) -> subprocess.CompletedProcess:
        """codex exec 1회 호출. timeout/encoding 명시.

        Args:
            prompt: codex exec 프롬프트.
            cwd: 작업 디렉터리. None(기본)이면 지정하지 않아 기존 동작과 동일하다.
                인포카드 경로에서 참조 모델시트를 codex 가 파일명으로 Read 할 수 있도록
                모델시트가 있는 폴더를 넘긴다.
            model: codex 모델. None(기본)이면 ``--model`` 을 붙이지 않아 기존 동작과
                동일하다. ChatGPT 계정에서 폐기된 모델(gpt-5.3-codex 등)이 config.toml/
                세션에 남아 400 을 내는 것을 우회하려 인포카드 경로에서 명시한다.

        Raises:
            FileNotFoundError: codex 실행파일을 PATH에서 해석하지 못한 경우.
        """
        if self._codex_resolved_path is None:
            raise FileNotFoundError(
                f"codex CLI not found in PATH (입력 '{self.codex_executable}'). "
                f"{_CODEX_NOT_FOUND_HINT}"
            )
        cmd = [self._codex_resolved_path, "exec"]
        # ★2026-09-09 방어 심화: 샌드박스를 명시 고정한다. CLI 0.130.0 의 `codex exec`
        #   기본값이 마침 read-only 임을 실측했지만(approval: never / sandbox: read-only),
        #   그건 계약이 아니라 우연이다. 버전이 바뀌어 기본이 쓰기 허용으로 완화되면
        #   에이전트가 파일을 쓸 수 있게 되고, 그 순간 2026-09-09 에 실증한 "실제 생성물
        #   대신 자기가 그린 대체본을 저장" 위조 경로가 이 구 경로에서 열린다.
        #   이미지 생성에 쓰기 권한은 애초에 필요 없으므로 최소 권한으로 못 박는다.
        cmd += ["-s", "read-only"]
        if model:
            cmd += ["--model", model]
        cmd += ["--skip-git-repo-check", prompt]
        kwargs: dict[str, Any] = dict(
            capture_output=True,
            text=True,
            timeout=self.per_call_timeout,
            encoding="utf-8",
            errors="replace",
        )
        if cwd is not None:
            kwargs["cwd"] = cwd
        return self._runner(cmd, **kwargs)

    @staticmethod
    def _parse_tokens_used(stdout: str, stderr: str) -> int:
        """codex stdout/stderr에서 'tokens used N' 패턴 추출. 못 찾으면 0."""
        haystack = f"{stdout}\n{stderr}".lower()
        marker = "tokens used"
        idx = haystack.find(marker)
        if idx < 0:
            return 0
        tail = haystack[idx + len(marker):].lstrip(" :")
        digits: list[str] = []
        for ch in tail:
            if ch.isdigit():
                digits.append(ch)
            elif digits:
                break
        if not digits:
            return 0
        try:
            return int("".join(digits))
        except ValueError:
            return 0

    def _pickup_image(self, newer_than: Optional[float] = None) -> Path:
        """codex가 저장한 최신 PNG 회수. 인박스 폴더는 우회.

        Args:
            newer_than: 주어지면 그 시각(epoch) 이후 생성된 파일만 채택하도록
                picker 에 ``min_mtime`` 을 전달한다. 인포카드 경로에서 "이번 호출로
                새로 만든 파일"만 골라 과거·직전 카드 PNG 재채택을 막는다. None(기본)
                이면 기존과 동일하게 전역 최신 파일을 고른다.
        """
        kwargs: dict[str, Any] = {}
        if self.session_dir_root is not None:
            kwargs["session_dir_root"] = self.session_dir_root
        if newer_than is not None:
            kwargs["min_mtime"] = newer_than
        picked = self._image_picker(**kwargs)

        # ★2026-09-09: 집어 온 파일이 **진짜 이미지**인지 확인한다. 예전에는 파일명 글롭과
        #   mtime 만 보고 그대로 채택해, 0바이트·잘린 파일·PNG 아닌 무언가가 그대로 본문에
        #   실릴 수 있었다. 신규 core.codex_image 가 쓰는 것과 같은 검증기를 재사용해
        #   두 경로의 판정 기준을 일치시킨다(한쪽만 방어되는 상태를 없앤다).
        #   검증기를 못 불러오면 기존 동작대로 통과시킨다(fail-open — 회귀 0).
        try:
            from core.codex_image import _looks_like_real_image
        except Exception:  # noqa: BLE001 — 검증기 미로드면 기존 동작 유지
            return picked
        ok, size, _w, _h, why = _looks_like_real_image(Path(picked))
        if not ok:
            raise FileNotFoundError(
                f"픽업한 파일이 유효한 이미지가 아님({why}, {size} bytes): {picked}")
        return picked

    def _move_to_inbox(self, src: Path, slug: str, role: str, n: int) -> Path:
        """{slug}_{role}{n}.png 규칙으로 인박스에 복사 (원본 보존)."""
        # thumb은 n=1 고정이지만 파일명 규칙은 동일하게 '_thumb1' 대신 '_thumb'으로 표기.
        # 사양: "{slug}_{role}{n}.png" 예: "_thumb.png"(thumb은 n 생략).
        if role == "thumb":
            target_name = f"{slug}_{role}.png"
        else:
            target_name = f"{slug}_{role}{n}.png"
        target = self.inbox_dir / target_name
        shutil.copy2(src, target)
        return target.resolve()

    def _postprocess_image(
        self, png_path: Path, *, role: str, n: int, title: str, subtitle: str,
        category: str, recipe: Optional[Any],
    ) -> Path:
        """생성된 PNG 후처리 훅. 기본은 그대로 반환(codex=이미지에 한글이 이미 baked).

        서브클래스(NvidiaImageBot)가 '글자 없는 FLUX 일러스트' 위에 한글을 코드로
        덧입힐 때 오버라이드한다.
        """
        return png_path

    # ──────────────────────────────────────────────────────────────────────
    # 슬롯 시퀀스
    # ──────────────────────────────────────────────────────────────────────

    @staticmethod
    def _build_slots(
        body_recipes: Optional[list[BodyRecipe]],
        info_recipes: Optional[list[InfoRecipe]],
        category: str,
        thumb_illustration_hint: Optional[str],
        extra_recipes: Optional[list[tuple[RoleName, Any]]] = None,
    ) -> list[tuple[RoleName, int, Optional[Any]]]:
        """1편 생성 시퀀스 = [(role, n, recipe), ...].

        recipe가 None인 슬롯은 호출 시 failed_roles에 추가하고 skip.
        """
        slots: list[tuple[RoleName, int, Optional[Any]]] = []
        # thumb x1
        slots.append(("thumb", 1, BodyRecipe(
            kind="five_step",
            items=[],
            illustration_hint=thumb_illustration_hint,
        )))

        # body x2
        for idx in range(2):
            recipe: Optional[BodyRecipe] = (
                body_recipes[idx] if body_recipes and idx < len(body_recipes) else None
            )
            slots.append(("body", idx + 1, recipe))

        # info x3
        for idx in range(3):
            recipe2: Optional[InfoRecipe] = (
                info_recipes[idx] if info_recipes and idx < len(info_recipes) else None
            )
            slots.append(("info", idx + 1, recipe2))

        # 디지털 카테고리 + extra_recipes
        if category == DIGITAL_CATEGORY and extra_recipes:
            for role, rec in extra_recipes:
                if role == "body":
                    next_n = sum(1 for r, _, _ in slots if r == "body") + 1
                elif role == "info":
                    next_n = sum(1 for r, _, _ in slots if r == "info") + 1
                else:
                    continue
                slots.append((role, next_n, rec))

        return slots

    # ──────────────────────────────────────────────────────────────────────
    # 메인 진입점
    # ──────────────────────────────────────────────────────────────────────

    def generate_post_images(
        self,
        *,
        slug: str,
        title: str,
        subtitle: str,
        category: str,
        body_recipes: Optional[list[BodyRecipe]] = None,
        info_recipes: Optional[list[InfoRecipe]] = None,
        thumb_illustration_hint: Optional[str] = None,
        extra_recipes: Optional[list[tuple[RoleName, Any]]] = None,
    ) -> GenerationResult:
        """1편 분량의 모든 이미지를 순차 생성한다.

        Returns:
            GenerationResult — 일부 슬롯 실패도 정상 반환. 모두 실패해도 예외 안 던지고
            failed_roles에 누적. 단, 한도 RuntimeError와 codex 미설치는 즉시 예외.
        """
        if not isinstance(slug, str) or not slug.strip():
            raise ValueError("slug는 비어있지 않은 문자열이어야 함")

        slots = self._build_slots(
            body_recipes=body_recipes,
            info_recipes=info_recipes,
            category=category,
            thumb_illustration_hint=thumb_illustration_hint,
            extra_recipes=extra_recipes,
        )

        result = GenerationResult(slug=slug)
        start_ts = time.monotonic()

        for slot_idx, (role, n, recipe) in enumerate(slots):
            slot_tag = f"{role}{n}" if role != "thumb" else role

            # recipe 누락 슬롯 — body/info는 데이터가 없으면 건너뛴다.
            if role in ("body", "info") and recipe is None:
                result.failed_roles.append(f"{slot_tag}:no_recipe")
                continue

            # 한도 사전 체크 (30회 초과 시 RuntimeError)
            self._check_quota_before_call()

            # 프롬프트 합성
            try:
                prompt = self._compose_prompt(
                    role=role,
                    title=title,
                    subtitle=subtitle,
                    category=category,
                    recipe=recipe,
                )
            except (ValueError, FileNotFoundError) as exc:
                result.failed_roles.append(f"{slot_tag}:prompt_error:{exc}")
                continue

            # codex 호출
            # ★2026-09-09: 호출 직전 시각을 찍어 픽업을 **이번 호출 산출물로 한정**한다.
            #   예전엔 `_pickup_image()` 를 인자 없이 불러 "전체에서 가장 최신 PNG"를 집었다.
            #   codex 가 rc=0 인데 새 PNG 를 안 만든 경우(도구 미사용·거절) 과거의 무관한
            #   이미지나 직전 슬롯의 원본을 조용히 이 슬롯 결과로 채택했다 —
            #   generate_infocards 가 이미 min_mtime 으로 막아 둔 것과 같은 사고 클래스.
            _t_before = time.time()
            try:
                completed = self._invoke_codex(prompt)
            except FileNotFoundError as exc:
                # codex 자체 미설치. 호출 흐름 전체 중단.
                # _invoke_codex에서 던진 메시지가 더 친절하면 그대로 보존.
                msg = str(exc) or "codex CLI not found in PATH"
                if "Windows" not in msg:
                    msg = f"codex CLI not found in PATH ({msg}). {_CODEX_NOT_FOUND_HINT}"
                raise FileNotFoundError(msg) from exc
            except subprocess.TimeoutExpired:
                result.failed_roles.append(f"{slot_tag}:timeout")
                result.total_calls += 1
                self._record_call(role=role, slug=slug)
                self._sleep(INTER_CALL_SLEEP_SEC)
                continue
            except (OSError, subprocess.SubprocessError) as exc:
                result.failed_roles.append(f"{slot_tag}:subprocess:{exc}")
                continue

            result.total_calls += 1
            self._record_call(role=role, slug=slug)
            result.tokens_used_estimate += self._parse_tokens_used(
                completed.stdout or "", completed.stderr or ""
            )

            # 결과 PNG 픽업 (stdout 무시) — 이번 호출 이후 생성분만 인정.
            try:
                picked = self._pickup_image(newer_than=_t_before)
            except FileNotFoundError:
                result.failed_roles.append(f"{slot_tag}:no_png")
                self._sleep(INTER_CALL_SLEEP_SEC)
                continue

            # 후처리 훅 — 기본은 no-op(codex=텍스트 baked). NvidiaImageBot 은 여기서
            # 글자 없는 FLUX 일러스트 위에 한글을 코드로 덧입힌다. 실패해도 원본 진행.
            try:
                picked = self._postprocess_image(
                    picked, role=role, n=n, title=title, subtitle=subtitle,
                    category=category, recipe=recipe)
            except Exception as exc:  # noqa: BLE001 — 후처리 실패는 원본 이미지로 진행
                log.warning(f"[{slot_tag}] 이미지 후처리 실패({exc}) — 원본 사용")

            # The model and Nvidia's local ComfyUI can both ignore requested dimensions.
            # Normalize only the thumbnail, after baked text/overlay and before it is
            # moved into the inbox or reported as successful. Body pixels stay untouched.
            if role == "thumb":
                try:
                    from naver_blog_v3.core.thumbnail_compose import normalize_thumbnail_square
                    normalized = normalize_thumbnail_square(picked)
                except (ImportError, OSError, TypeError, ValueError) as exc:
                    normalized = {"ok": False,
                                  "reason": f"thumbnail_normalization_failed:{type(exc).__name__}"}
                if not normalized.get("ok") or not normalized.get("path"):
                    reason = str(normalized.get("reason") or "thumbnail_normalization_failed")
                    result.failed_roles.append(f"{slot_tag}:{reason}")
                    self._sleep(INTER_CALL_SLEEP_SEC)
                    continue
                picked = Path(normalized["path"])

            # 인박스 이동
            try:
                final_path = self._move_to_inbox(picked, slug=slug, role=role, n=n)
            except OSError as exc:
                result.failed_roles.append(f"{slot_tag}:copy_error:{exc}")
                self._sleep(INTER_CALL_SLEEP_SEC)
                continue

            result.success_paths.append(final_path)

            # 다음 호출까지 짧은 sleep (mtime 충돌 방지)
            if slot_idx < len(slots) - 1:
                self._sleep(INTER_CALL_SLEEP_SEC)

        result.elapsed_seconds = time.monotonic() - start_ts
        result.quota_warning = self._count_recent_calls() >= QUOTA_WARNING_THRESHOLD
        return result

    # ──────────────────────────────────────────────────────────────────────
    # 본문 분석형 인포카드 (Stage1 기획 → 검증 → Stage2 카드별 생성)
    # ──────────────────────────────────────────────────────────────────────

    def _prepare_model_ref(
        self, slug: str, model_ref_path: str
    ) -> "tuple[Optional[Path], Optional[str]]":
        """참조 모델시트를 codex 작업 폴더에 복사해 (작업폴더, 파일명)을 반환.

        codex 는 cwd 안의 파일을 파일명으로 Read 할 수 있어야 참조 이미지를 본다.
        어떤 실패도 (None, None) 로 폴백(모델시트 없이 텍스트 묘사만 사용).
        """
        try:
            src = Path(model_ref_path)
            if not src.is_file():
                log.warning(f"[infocard] 모델시트 없음: {model_ref_path} — 텍스트 묘사만")
                return None, None
            if src.stat().st_size > _MAX_MODEL_REF_BYTES:
                log.warning(
                    f"[infocard] 모델시트 {_MAX_MODEL_REF_BYTES // (1024 * 1024)}MB 초과"
                    f"({model_ref_path}) — 텍스트 묘사만"
                )
                return None, None
            work_dir = self.inbox_dir / f"_modelref_{slug}"
            work_dir.mkdir(parents=True, exist_ok=True)
            dst = work_dir / src.name
            shutil.copy2(src, dst)
            return work_dir, src.name
        except OSError as exc:
            log.warning(f"[infocard] 모델시트 준비 실패({exc}) — 텍스트 묘사만")
            return None, None

    def generate_infocards(
        self,
        *,
        slug: str,
        title: str,
        category: str,
        article_text: str,
        card_budget: int = 5,
        model_ref_path: Optional[str] = None,
        planner: Optional[Callable[..., ImagePlan]] = None,
    ) -> InfocardResult:
        """본문 전체를 읽어 카드뉴스 인포카드를 생성한다(옵트인 신규 경로).

        Stage1(기획) → 검증(fact-grounding) → Stage2(카드별 GPT-Image) 순서로 진행하며,
        어떤 단계 실패도 예외 없이 InfocardResult 로 반환한다. 구성안이 비거나 근거 있는
        카드가 하나도 없으면 needs_fallback=True(호출자가 옛 슬롯 경로 선택 가능).

        Args:
            slug: 글 슬러그(파일명·모델시트 격리 키).
            title: 글 제목(문맥·프롬프트용).
            category: 카테고리(문맥용).
            article_text: 글 전체 본문(평문) — 카드 사실의 유일한 근거.
            card_budget: 생성할 카드 최대 장수.
            model_ref_path: 일관 모델 참조 이미지시트 경로(선택).
            planner: plan_infocards 주입(테스트용). None 이면 기본 기획기.

        Returns:
            InfocardResult.
        """
        if not isinstance(slug, str) or not slug.strip():
            raise ValueError("slug는 비어있지 않은 문자열이어야 함")

        result = InfocardResult(slug=slug)
        plan_fn = planner or plan_infocards

        # Stage 1 — 구성안 기획(어떤 예외도 폴백).
        try:
            plan = plan_fn(
                article_text=article_text,
                title=title,
                category=category,
                card_budget=card_budget,
            )
        except Exception as exc:  # noqa: BLE001 — 기획 실패는 전부 폴백
            log.warning(f"[infocard] 기획 실패({exc}) — 옛 슬롯 경로 폴백")
            result.needs_fallback = True
            return result

        result.plan = plan
        if plan is None or not plan.cards:
            log.info("[infocard] 구성안 없음 — 옛 슬롯 경로 폴백")
            result.needs_fallback = True
            return result

        # 검증 — 근거 있는 카드만 통과.
        report = validate_image_plan(
            plan, article_text=article_text, card_budget=card_budget
        )
        for flagged in report.flagged:
            log.info(f"[infocard] 카드 드롭({flagged.reason}): {flagged.card.headline!r}")

        grounded = report.grounded_cards
        if not grounded:
            log.info("[infocard] 근거 있는 카드 0장 — 옛 슬롯 경로 폴백")
            result.needs_fallback = True
            return result

        # 참조 모델시트 준비(선택). codex 가 파일명으로 Read 하도록 cwd 를 그 폴더로.
        work_dir: Optional[Path] = None
        model_ref_name: Optional[str] = None
        if model_ref_path:
            work_dir, model_ref_name = self._prepare_model_ref(slug, model_ref_path)
        cwd = str(work_dir) if work_dir is not None else None
        codex_model = _resolve_codex_image_model()

        # 종료(정상·예외·break) 시 반드시 모델시트 작업 폴더를 정리한다.
        modelref_dir = self.inbox_dir / f"_modelref_{slug}"
        try:
            # Stage 2 — 카드별 생성(카드 격리 · fail-open).
            for card_index, card in enumerate(grounded, start=1):
                tag = f"card{card_index}"

                # 한도 사전 체크 — 초과면 중단(부분 결과 반환).
                try:
                    self._check_quota_before_call()
                except RuntimeError as exc:
                    log.warning(f"[infocard] {tag}: 한도 임박으로 중단 — {exc}")
                    result.quota_warning = True
                    result.failed.append(f"{tag}:quota")
                    break

                # 프롬프트 합성(실패해도 다음 카드로).
                try:
                    prompt = build_infocard_prompt(
                        card, title=title, category=category,
                        model_ref_name=model_ref_name,
                    )
                    # codex 내장 이미지 생성 도구를 명시 호출한다($imagegen). 없으면 codex 가
                    # 이미지 생성 의도를 스스로 추론해야 해, 코드 설명만 하고 파일 없이 끝날 수 있다.
                    prompt = "$imagegen\n" + prompt
                except Exception as exc:  # noqa: BLE001 — 프롬프트 합성 실패는 카드 스킵
                    result.failed.append(f"{tag}:prompt_error:{exc}")
                    continue

                # codex 호출 — 직전 시각을 기록해 "이번 호출로 생성된 파일"만 픽업한다.
                t_before = time.time()
                try:
                    completed = self._invoke_codex(prompt, cwd=cwd, model=codex_model)
                except FileNotFoundError:
                    # codex 미설치 — 인포카드 생성 불가. 폴백 신호 후 중단(예외 미전파).
                    log.warning(f"[infocard] {tag}: codex 미설치 — 옛 슬롯 경로 폴백")
                    result.needs_fallback = True
                    result.failed.append(f"{tag}:codex_missing")
                    break
                except subprocess.TimeoutExpired:
                    result.failed.append(f"{tag}:timeout")
                    self._record_call(role="card", slug=slug)
                    self._sleep(INTER_CALL_SLEEP_SEC)
                    continue
                except (OSError, subprocess.SubprocessError) as exc:
                    result.failed.append(f"{tag}:subprocess:{exc}")
                    continue

                self._record_call(role="card", slug=slug)

                # returncode 검증 — 400(폐기 모델·계정 미지원) 등 오류를 no_png 로 숨기지
                # 않고 그대로 노출한다(관측성). 오류면 픽업으로 넘어가지 않는다.
                rc = getattr(completed, "returncode", 0)
                if rc != 0:
                    err = (getattr(completed, "stderr", "") or
                           getattr(completed, "stdout", "") or "").strip()
                    result.failed.append(f"{tag}:codex_error:{err[:200]}")
                    log.error(f"[infocard] {tag}: codex rc={rc} — {err[:400]}")
                    self._sleep(INTER_CALL_SLEEP_SEC)
                    continue

                # 결과 PNG 픽업 — t_before 이후 신규 파일만(스테일·직전 카드 재채택 차단).
                try:
                    picked = self._pickup_image(newer_than=t_before)
                except FileNotFoundError:
                    result.failed.append(f"{tag}:no_png")
                    self._sleep(INTER_CALL_SLEEP_SEC)
                    continue

                # 인박스 이동 — role="card" 규칙.
                try:
                    final_path = self._move_to_inbox(
                        picked, slug=slug, role="card", n=card_index
                    )
                except OSError as exc:
                    result.failed.append(f"{tag}:copy_error:{exc}")
                    self._sleep(INTER_CALL_SLEEP_SEC)
                    continue

                result.image_paths.append(final_path)
                result.card_meta.append({"role": card.role, "headline": card.headline})

                if card_index < len(grounded):
                    self._sleep(INTER_CALL_SLEEP_SEC)
        finally:
            # 모델시트 작업 폴더 정리. 인박스 산출 PNG 는 별도 폴더라 무영향.
            shutil.rmtree(modelref_dir, ignore_errors=True)

        result.quota_warning = (
            result.quota_warning
            or self._count_recent_calls() >= QUOTA_WARNING_THRESHOLD
        )
        return result

    # ──────────────────────────────────────────────────────────────────────
    # 프롬프트 합성 (Day 1 빌더 위의 얇은 래퍼)
    # ──────────────────────────────────────────────────────────────────────

    @staticmethod
    def _compose_prompt(
        *,
        role: RoleName,
        title: str,
        subtitle: str,
        category: str,
        recipe: Optional[Any],
    ) -> str:
        """역할별로 build_image_prompt 인자를 정리해 호출한다.

        - thumb: title/subtitle/category + recipe.illustration_hint
        - body : title/subtitle/category + recipe.illustration_hint
                 (body_items는 빌더가 body 역할에선 안 받음 — kind/items는 hint로 변환)
        - info : title/subtitle/category + recipe.items (Q&A 또는 step)
        """
        if role == "thumb":
            hint = recipe.illustration_hint if recipe is not None else None
            return build_image_prompt(
                role="thumb",
                title=title,
                subtitle=subtitle,
                category=category,
                illustration_hint=hint,
            )

        if role == "body":
            if recipe is None:
                raise ValueError("body 역할에는 BodyRecipe가 필요")
            hint = _body_kind_to_hint(recipe)
            return build_image_prompt(
                role="body",
                title=title,
                subtitle=subtitle,
                category=category,
                illustration_hint=hint,
            )

        if role == "info":
            if recipe is None:
                raise ValueError("info 역할에는 InfoRecipe가 필요")
            if not recipe.items:
                raise ValueError("info 역할 InfoRecipe.items가 비어 있음")
            return build_image_prompt(
                role="info",
                title=title,
                subtitle=subtitle,
                category=category,
                body_items=recipe.items,
                illustration_hint=recipe.illustration_hint,
            )

        raise ValueError(f"지원되지 않는 role: {role}")


def _body_kind_to_hint(recipe: BodyRecipe) -> str:
    """BodyRecipe.kind를 '글자 없는' 영문 시각 힌트로 변환.

    ⚠️ 한글 item 라벨(step/desc, q/a)은 **절대 힌트에 넣지 않는다** — 그러면 이미지 모델이
    그 한글을 (깨진 글자로) 그림 안에 렌더한다(실측: body1 한글 배임의 원인). 라벨/숫자는
    text_overlay 가 코드로 그리므로, 여기서는 '어떤 장면인지'만 순수 영어로 준다.
    """
    base = (recipe.illustration_hint or "").strip()
    kind_map = {
        "five_step": "a left-to-right sequence of a few simple scenes showing progression",
        "before_after": "a left/right before-and-after split composition",
        "self_diagnosis": "a person calmly reviewing a few items with gentle checkmark motifs",
    }
    kind_clause = kind_map.get(recipe.kind, "")
    parts = [p for p in (kind_clause, base) if p]
    return ". ".join(parts) or base
