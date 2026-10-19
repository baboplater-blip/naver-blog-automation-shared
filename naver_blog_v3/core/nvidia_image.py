# core/nvidia_image.py — 무텍스트 일러스트 백엔드 (이제 로컬 ComfyUI 전용)
"""프롬프트 → PNG 를 **로컬 ComfyUI**(자기 GPU)로 생성. 클라우드 의존 0.

⚠️ 이름에 대한 정직한 고지: 모듈명(`nvidia_image`)·함수명(`generate_image`)·봇
(`NvidiaImageBot`)에는 아직 'nvidia' 가 남아 있지만, **더 이상 NVIDIA/Pollinations
같은 클라우드 백엔드를 쓰지 않는다**. 2026-07-24 에 NVIDIA FLUX(504 큐 타임아웃·실패에
121초 낭비·하드코딩 키)와 Pollinations 를 둘 다 폐기하고, 무텍스트 일러스트 경로의
유일한 백엔드로 **로컬 ComfyUI(comfy_image)** 만 남겼다. 이름은 호출자(auto_scheduler·
image_dispatcher 의 isinstance 정체성 검사·GUI)가 의존해 리플이 커서 유지한다 —
리네임은 별도 후속 과제.

동작:
- 로컬 ComfyUI 가 켜져 있고 도달 가능하면(available()) `comfy_image.try_generate` 로 생성.
- 실패하거나 미가동이면 예외(RuntimeError) — 호출측(봇)이 failed_roles 로 처리하고,
  절반 이상 실패 시 dispatcher 가 Tier2(수동)로 폴백한다(기존 흐름 무변경).

보안: 완전 로컬(외부 전송 0). 자격증명과 무관하다.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Optional

from config.logger import get_logger

log = get_logger(__name__)

# 백엔드 중립 기본 생성 크기. 로컬 ComfyUI 는 자체적으로 IMAGE_COMFY_WIDTH/HEIGHT(기본 768)로
# 오버라이드하므로 이 값은 호출자가 크기를 안 줬을 때의 폴백일 뿐이다.
_DEFAULT_WIDTH = 1024
_DEFAULT_HEIGHT = 1024


def _cfg(name: str, default: Any) -> Any:
    try:
        from config import settings as s
        return getattr(s, name, default)
    except Exception:  # noqa: BLE001
        return default


def available() -> bool:
    """이 백엔드로 이미지를 만들 수 있는가 — 로컬 ComfyUI 활성(플래그 ON) + 도달 가능하면 True.

    IMAGE_COMFY_ENABLED 가 꺼져 있으면 즉시 False. 켜져 있으면 comfy_image.available()
    (워크플로우 존재 + 서버 ping)로 실제 도달까지 확인한다."""
    if not bool(_cfg("IMAGE_COMFY_ENABLED", True)):
        return False
    try:
        from naver_blog_v3.core import comfy_image as _comfy
        return bool(_comfy.available())
    except Exception:  # noqa: BLE001 — 임포트/설정 오류는 '미사용'으로 간주
        return False


def generate_image(
    prompt: str,
    out_path: Path,
    *,
    width: Optional[int] = None,
    height: Optional[int] = None,
    seed: int = 0,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> Path:
    """프롬프트로 이미지 1장 생성 → out_path(PNG) 저장 후 경로 반환.

    로컬 ComfyUI 로만 생성한다. 미가동/실패 시 예외(RuntimeError) — 호출측(봇)이
    failed_roles 로 처리한다(클라우드 폴백은 2026-07-24 폐기).
    """
    if not (prompt and prompt.strip()):
        raise ValueError("prompt 비어있음")

    width = int(width or _DEFAULT_WIDTH)
    height = int(height or _DEFAULT_HEIGHT)

    # 로컬 ComfyUI (자기 GPU·무제한·무타임아웃·비공개) — 켜져 있고 도달 가능할 때만 시도.
    # 미가동이면 available()=False 로 즉시 실패(수동 폴백으로 넘어감).
    if bool(_cfg("IMAGE_COMFY_ENABLED", True)):
        try:
            from naver_blog_v3.core import comfy_image as _comfy
            if _comfy.available():
                ok, status = _comfy.try_generate(
                    prompt, out_path, width=width, height=height,
                    seed=seed, sleep_fn=sleep_fn)
                if ok:
                    return out_path
                reason = f"comfy={status}"
            else:
                reason = "comfy=unavailable"
        except Exception as exc:  # noqa: BLE001 — ComfyUI 경로 문제는 실패로 처리
            reason = f"comfy=exc:{type(exc).__name__}"
    else:
        reason = "comfy=disabled"

    log.warning(f"[nvidia_image] 이미지 생성 실패 — {reason} (로컬 ComfyUI 전용)")
    raise RuntimeError(f"이미지 생성 실패({reason})")
