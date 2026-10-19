# gui_app.py — v3.0  네이버 블로그 자동화 대시보드 (런처)
import sys
from pathlib import Path

# ⚠️ 이미지 체인(image_dispatcher)은 isinstance(bot, naver_blog_v3.core.codex_cli_bot.*)로
# 검사하므로 봇을 **절대 임포트(naver_blog_v3.core.*)**로 만들어야 한다. 그러려면 부모
# (naver_blog_automation_v3/)도 path 에 있어야 `import naver_blog_v3.*` 가 된다. gui/·core/
# 상대 임포트는 naver_blog_v3/(PKG_ROOT)가 index 0 이라 그대로 동작(이중 모듈 정체성 함정
# 회피 = nvidia_image_bot 과 동일 패턴). [[reference_naver_blog_dual_identity]]
_PKG_ROOT = Path(__file__).parent                 # naver_blog_v3/
_PROJECT_ROOT = _PKG_ROOT.parent                  # naver_blog_automation_v3/
for _p in (_PROJECT_ROOT, _PKG_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# ── 프로필 고정(한 프로세스=한 프로필, 2026-07-19 격리 감사) ─────────────
# GUI 를 그냥 켜면 NBA_PROFILE 이 없어 active_profile() 이 profiles/.active 를
# **호출 때마다** 다시 읽는다 → 실행 도중 다른 창/전환이 .active 를 바꾸면
# 한 프로세스 안에서 두 계정 경로가 뒤섞인다(18:12 examplebrand→exampleprofile 실측).
# 기동 시점의 활성 프로필을 env 로 못박아 이후 .active 변경이 이 프로세스에
# 영향을 못 주게 한다. 프로필 전환은 relaunch_with_profile(새 프로세스+env)이
# 정본이며 setdefault 라 그 env 를 덮지 않는다. 자식 프로세스도 env 상속으로 고정.
import os as _os
try:
    from config import profile as _pf
    _os.environ.setdefault("NBA_PROFILE", _pf.active_profile())
except Exception:  # noqa: BLE001 — 프로필 레이어 부재 시 기존 동작
    pass

from gui.app import App

if __name__ == "__main__":
    App().run()
