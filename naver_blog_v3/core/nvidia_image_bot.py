# core/nvidia_image_bot.py — 로컬 ComfyUI 무텍스트 일러스트 봇 (codex CLI 대체)
"""CodexCLIBot 의 슬롯·프롬프트·인박스 로직을 그대로 재사용하고, 실제 '프롬프트→PNG'
생성만 로컬 ComfyUI 백엔드(nvidia_image)로 교체한다.

⚠️ 이름에 'Nvidia' 가 남아 있지만 실제 백엔드는 로컬 ComfyUI 단 하나다(NVIDIA/Pollinations
클라우드는 2026-07-24 폐기). 클래스·모듈명은 호출자(auto_scheduler·dispatcher 정체성
검사·GUI)가 의존해 유지 — 리네임은 후속 과제. 자세한 배경은 nvidia_image 모듈 docstring.

이렇게 하면 image_dispatcher 의 Tier1/Tier2 폴백·reconcile 흐름이 전혀 바뀌지 않는다:
per-image 실패는 failed_roles 로 누적되고, 절반 이상 실패하면 기존대로 Tier2(수동)로
폴백한다. codex 미설치여도 무방(생성 경로를 오버라이드).
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

# ⚠️ 이미지 체인은 반드시 naver_blog_v3.core.* (절대 패키지) 로 임포트한다.
# image_dispatcher 가 isinstance(bot, naver_blog_v3.core.codex_cli_bot.CodexCLIBot) 로
# 검사하므로, 여기서 core.* (상대) 로 CodexCLIBot 을 상속하면 '같은 파일·다른 클래스 객체'
# (이중 모듈 정체성)가 되어 TypeError 로 dispatcher 초기화가 실패한다. (2026-07-05 버그픽스)
_PKG_ROOT = Path(__file__).resolve().parent.parent            # naver_blog_v3/  (config.* 용)
_PROJECT_ROOT = _PKG_ROOT.parent                              # naver_blog_v3.* 절대 패키지용
for _p in (_PROJECT_ROOT, _PKG_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from config.logger import get_logger  # noqa: E402
from naver_blog_v3.core.codex_cli_bot import CodexCLIBot, _body_kind_to_hint  # noqa: E402
from naver_blog_v3.core import nvidia_image  # noqa: E402
from naver_blog_v3.core import text_overlay  # noqa: E402
from naver_blog_v3.core.image_prompt_builder import build_illustration_prompt  # noqa: E402

log = get_logger(__name__)


class NvidiaImageBot(CodexCLIBot):
    """codex 대신 로컬 ComfyUI 로 무텍스트 일러스트를 생성하는 봇 (동일 인터페이스).

    (클래스명의 'Nvidia' 는 레거시 — 실제 백엔드는 로컬 ComfyUI 다. 리네임은 후속 과제.)"""

    def __init__(self, inbox_dir: Path, **kwargs) -> None:
        # codex 실행파일이 없어도 생성 경로를 오버라이드하므로 문제없다.
        super().__init__(inbox_dir=inbox_dir, **kwargs)
        self._tmp_dir: Path = Path(tempfile.gettempdir()) / "nvidia_flux"
        self._tmp_dir.mkdir(parents=True, exist_ok=True)
        self._last_png: Path | None = None
        self._img_n: int = 0

    # codex 30회/5h 한도는 로컬 ComfyUI 에 해당 없음(무제한·무타임아웃).
    def _check_quota_before_call(self) -> None:  # noqa: D401
        return

    def _invoke_codex(
        self, prompt: str, cwd: Optional[str] = None, model: Optional[str] = None
    ) -> subprocess.CompletedProcess:
        """codex 호출을 로컬 ComfyUI 생성으로 대체. 실패 시 SubprocessError(치명 아님).

        cwd/model 은 codex 경로용 인자로, 로컬 ComfyUI 생성 경로에선 쓰이지 않는다
        (베이스 시그니처 호환을 위해 받되 무시). 기존 호출 무변경.
        """
        self._img_n += 1
        out = self._tmp_dir / f"comfy_{self._img_n}.png"
        try:
            nvidia_image.generate_image(prompt, out, sleep_fn=self._sleep)
        except Exception as exc:  # noqa: BLE001 — per-image 실패는 failed_roles 로
            # 사유를 로그에 남긴다(dispatcher 요약만으론 원인을 못 봄).
            log.warning(f"[nvidia_image_bot] 로컬 ComfyUI 이미지 생성 실패: {exc}")
            raise subprocess.SubprocessError(f"nvidia_image 실패: {exc}") from exc
        self._last_png = out
        return subprocess.CompletedProcess(
            args=["comfy-image"], returncode=0, stdout="", stderr="")

    def _pickup_image(self, newer_than: Optional[float] = None) -> Path:
        # newer_than 은 codex 파일 픽업 경로용 신호 — 로컬 ComfyUI 는 방금 만든 _last_png 가
        # 곧 신규 파일이므로 시그니처만 호환하고 무시한다(스테일 채택 위험 없음).
        if self._last_png and self._last_png.exists():
            return self._last_png
        raise FileNotFoundError("nvidia_image: 생성된 PNG 없음")

    # ComfyUI(FLUX/Krea)는 한글을 못 그린다 → 프롬프트는 '글자 없는 일러스트'만 요구하고,
    # 한글(제목·부제·배지·Q&A)은 _postprocess_image 가 코드로 덧입힌다.
    def _compose_prompt(  # type: ignore[override]
        self, *, role: str, title: str, subtitle: str, category: str,
        recipe: Optional[Any],
    ) -> str:
        if role == "thumb":
            hint = getattr(recipe, "illustration_hint", None) if recipe is not None else None
        elif role == "body":
            if recipe is None:
                raise ValueError("body 역할에는 BodyRecipe가 필요")
            hint = _body_kind_to_hint(recipe)
        elif role == "info":
            if recipe is None:
                raise ValueError("info 역할에는 InfoRecipe가 필요")
            hint = getattr(recipe, "illustration_hint", None)
        else:
            raise ValueError(f"지원되지 않는 role: {role}")
        return build_illustration_prompt(
            role=role, title=title, subtitle=subtitle,
            category=category, illustration_hint=hint)

    def _postprocess_image(  # type: ignore[override]
        self, png_path: Path, *, role: str, n: int, title: str, subtitle: str,
        category: str, recipe: Optional[Any],
    ) -> Path:
        """글자 없는 FLUX 일러스트 위에 한글을 코드로 합성(text_overlay). 실패=원본."""
        items = getattr(recipe, "items", None) if recipe is not None else None
        return text_overlay.overlay_card(
            png_path, png_path, role=role, title=title, subtitle=subtitle,
            category=category, items=items)
