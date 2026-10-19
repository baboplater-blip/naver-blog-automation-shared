"""machine_paths — 계정(프로필)을 가로지르는 **머신 공용** 상태 경로 (2026-09-18).

프로필 격리 경로(`config.profile.logs_dir()` 등)는 계정마다 다르다. 그런데 크롬 실행,
전체 비상정지, 머신 전역 비용 원장처럼 **머신 하나에 하나뿐인 자원**은 계정별 폴더에
두면 의미가 없다(계정 A 의 락이 계정 B 를 막지 못한다).

경로 결정 순서:
  1) 환경변수 ``NBA_MACHINE_ROOT`` (테스트·이식 상황에서 최우선)
  2) ``settings.AUTONOMY_MACHINE_ROOT`` (비어 있지 않을 때만)
  3) ``%LOCALAPPDATA%\\NaverBlogAuto`` (Windows 기본 — 전역 설정·권한을 바꾸지 않는
     사용자 쓰기 가능 경로)
  4) ``~/.naver_blog_auto`` (LOCALAPPDATA 가 없는 환경 폴백)

⚠️ 모듈 최상위에서 상수로 굳히지 않는다 — 호출 시점에 계산해야 테스트가 환경변수로
격리할 수 있다(이 저장소의 "임포트 시점 고정 상수" 사고 클래스 회피).
"""
from __future__ import annotations

import os
from pathlib import Path

_DIR_NAME = "NaverBlogAuto"


def _settings_root() -> str:
    try:
        from config import settings as _s
        return str(getattr(_s, "AUTONOMY_MACHINE_ROOT", "") or "").strip()
    except Exception:  # noqa: BLE001 — settings 미로드 환경이면 기본 경로
        return ""


def machine_root() -> Path:
    """머신 공용 상태 루트. 디렉터리를 만들지는 않는다(쓰기 측이 mkdir)."""
    env = (os.environ.get("NBA_MACHINE_ROOT") or "").strip()
    if env:
        return Path(env)
    cfg = _settings_root()
    if cfg:
        return Path(cfg)
    local = (os.environ.get("LOCALAPPDATA") or "").strip()
    if local:
        return Path(local) / _DIR_NAME
    return Path.home() / ".naver_blog_auto"


def machine_file(name: str) -> Path:
    """머신 공용 파일 경로. 부모 디렉터리를 보장한다(실패해도 경로는 반환)."""
    root = machine_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return root / name


__all__ = ["machine_root", "machine_file"]
