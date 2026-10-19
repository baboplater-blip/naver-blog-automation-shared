# config/profile_boot.py — 스크립트 직접 실행용 프로필 고정 부트스트랩 (2026-07-19 격리 감사)
"""schtasks 가 `pythonw <script>.py --profile <name>` 로 실행하는 스크립트의 진입 훅.

한 프로세스=한 프로필 원칙을 스크립트 실행에도 강제한다:
  1) argv 의 --profile 을 **소비(제거)** 하고 NBA_PROFILE 로 고정 — 이후 스크립트
     자체 argparse(parse_args 엄격 모드)가 모르는 인자로 죽지 않는다.
  2) --profile 미지정이면 기동 시점의 .active 를 env 로 못박는다 — 실행 중
     .active 가 바뀌어도 이 프로세스는 흔들리지 않는다(gui_app/run_cli 와 동일).

⚠️ 반드시 **config.logger 등 다른 config/core import 이전**, 그리고
`if __name__ == "__main__":` 가드 안에서 호출할 것 — 모듈로 import 될 때
호출자(GUI 등)의 argv 를 건드리면 안 된다.
"""
from __future__ import annotations

import os
import sys


def pin_profile_from_argv(argv: list | None = None) -> str:
    """argv 에서 --profile 을 소비하고 NBA_PROFILE 을 고정. 반환: 확정 프로필명."""
    args = sys.argv if argv is None else argv
    name = ""
    kept: list = []
    i = 1
    while i < len(args):
        a = args[i]
        if a == "--profile" and i + 1 < len(args):
            name = args[i + 1]
            i += 2
            continue
        if isinstance(a, str) and a.startswith("--profile="):
            name = a.split("=", 1)[1]
            i += 1
            continue
        kept.append(a)
        i += 1
    if argv is None:
        sys.argv = [args[0]] + kept
    if name:
        os.environ["NBA_PROFILE"] = name
    else:
        try:
            from config import profile as _pf
            os.environ.setdefault("NBA_PROFILE", _pf.active_profile())
        except Exception:  # noqa: BLE001 — 프로필 레이어 부재 시 기존 동작
            pass
    return os.environ.get("NBA_PROFILE", "default")
