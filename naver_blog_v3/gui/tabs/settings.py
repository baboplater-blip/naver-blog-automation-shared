# gui/tabs/settings.py — 설정 탭 (GUI 개편 2026-09-02)
"""좌측 세그먼트 5개(계정·연결/발행/품질/외부 API/자동화·진단) + 우측 스크롤 영역.

공개 계약(설계서 §0.2 — 절대 이름 바꾸지 않는다): ``self.vars``(38키 — 35 → 37,
2026-09-09 Codex 이미지 엔진 전환으로 "이미지 채움" 카드가 엔진 중립으로 바뀌며
``genspark_infocard_budget`` 1개 제거 + ``image_fill_enabled``·``image_fill_engine``·
``image_fill_cards`` 3개 추가(``genspark_enabled`` 는 레거시 엔진 전용 토글로 유지),
2026-09-10 제한 병렬 ``image_fill_parallel`` 1개 추가로 37 → 38,
`tests/test_gui_settings.py::test_settings_vars_preserves_38_keys` 참고),
``_preset_btns``(dict 3개), ``_preset_var``, ``_apply_preset_button(name)``,
``_run_support_report``, ``_run_multichannel_build``, ``_qv_code_defaults``,
``_save()``, ``_collect_risky_warnings``, ``_confirm_risky_settings``.

auto-dev 5호(2026-09-02) — 설정 찾기 검색창·라이트 테마 토글(잔여)·멀티채널
standalone 을 ``gui.multichannel_service`` 로 위임. 두 신규 모듈
(``gui.ui_prefs``·``gui.multichannel_service``)은 다른 코더가 동시 작업
중이라 **모듈 상단에서 import 실패를 흡수하고 폴백**한다 — 아직 없어도
이 탭은 정상 동작해야 한다(전자는 카드 비활성화, 후자는 기존 본체로 폴백).

auto-dev 8호 Phase 2(2026-09-03, `docs/auto-dev/gui-design-system-v3.md`) —
상용 서비스급 리스킨. 이모지를 전부 걷어내고 `gui.icons`(Segoe Fluent Icons
글리프)로 교체했다. 버튼류의 ``icon=`` 은 아이콘 **이름**(`gui.icons.ICONS`
키, 예: "save")을 받아 내부에서 글리프로 resolve 하고, `Header`/`SectionCard`
의 ``icon=`` 은 이미 resolve 된 **글리프 문자**를 직접 받는다(둘의 계약이
다르므로 혼동 금지 — 후자는 ``icons.glyph(name)`` 를 직접 호출해 넘긴다).
운영 프리셋 3버튼은 ``_preset_btns`` dict 계약(키 3개·선택 시 ``["bg"]``
차이)을 지키기 위해 `Segmented` 대신 순정 tk 인 `theme.PlainBtn` 을 그대로
쓴다(설계서 §7-4 예외 지침).
"""
import logging
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from gui import glossary, icons
from gui import theme as _theme
from gui.async_utils import UiLifecycle
from gui.config_io import save_cfg
from gui.theme import (C, FB, FS, FT, SP, DangerBtn, GhostBtn, L,
                        PlainBtn, PrimaryBtn, SecondaryBtn, Sep, SubtleBtn)
from gui.widgets import (EmptyState, FormField, Header, ScrollArea, SectionCard,
                         StatusPill, Tooltip)

# auto-dev 9호 Phase 1(2026-09-03, 고DPI) — 코더 A 가 theme.px(n)(SP/R 과
# 별개로 이 파일이 직접 쓰는 "표준 토큰이 없는" 하드코딩 px 값을 스케일하는
# 헬퍼)·SP/R 자체의 자동 확대를 만드는 중이다. 아직 없으면 항등함수로
# 폴백해 이 라운드 이전과 완전히 동일하게 동작한다(임무 지침 그대로).
# 이름은 항상 모듈 레벨에서 이 함수 "객체"만 참조하고, 실제 배율 계산은
# 매번 호출 시점에 위임한다 — 클래스 속성에 ``px(200)`` 을 미리 구워두면
# (import 시점에 1회 평가) NBA_UI_SCALE 을 나중에 바꿔도(테스트의
# monkeypatch 포함) 반영되지 않는다. 그래서 아래 모든 px 값은 인스턴스
# 메서드(``_build``/``_on_body_configure`` 등) 안에서 호출 시점에 구한다.
px = getattr(_theme, "px", lambda n: n)

try:
    from gui import ui_prefs
except ImportError:  # noqa: BLE001 — 코더 A 작업 완료 전에는 항상 이 경로(폴백)
    ui_prefs = None

try:
    from gui.multichannel_service import CANDIDATE_FILES as _MC_CANDIDATE_FILES
    from gui.multichannel_service import list_candidate_slugs as _mc_list_candidate_slugs
    from gui.multichannel_service import start_build as _mc_start_build
    _MULTICHANNEL_SERVICE_AVAILABLE = True
except ImportError:  # noqa: BLE001 — 코더 B 작업 완료 전에는 항상 이 경로(폴백)
    _MC_CANDIDATE_FILES = None
    _mc_list_candidate_slugs = None
    _mc_start_build = None
    _MULTICHANNEL_SERVICE_AVAILABLE = False

log = logging.getLogger(__name__)

# ── 운영 프리셋 정의 ─────────────────────────────────────
# DAILY_POST_LIMIT(daily_limit)은 예약 발행 수이므로 프리셋에서 변경하지 않음
PRESETS = {
    "안전 모드": {
        "interval_sec": 300,
        "use_reservation": True,
        "use_kakao": False,
        "enable_cafe_dispatch": False,
        "qv_min_chars": 2000,
    },
    "균형 모드": {
        "interval_sec": 180,
        "use_reservation": True,
        "use_kakao": True,
        "enable_cafe_dispatch": False,
        "qv_min_chars": 1500,
    },
    "성장 모드": {
        "interval_sec": 120,
        "use_reservation": True,
        "use_kakao": True,
        "enable_cafe_dispatch": True,
        "qv_min_chars": 1500,
    },
}
PRESET_NAMES = list(PRESETS.keys())
CUSTOM_LABEL = "사용자 정의"

# ── 엑셀 발행 라인 프리셋 ────────────────────────────────
# 도메인별로 미리 만들어둔 키워드 엑셀 파일을 한 번에 전환.
# 파일은 naver_blog_v3/ 루트에 위치 (상대경로 — settings.py가 절대경로로 변환).
EXCEL_PRESETS = {
    "정부지원":         "네이버 키워드별 검색량.xlsx",
    "비즈니스·경제":    "비즈니스경제 키워드별 검색량.xlsx",
    "노후 준비·은퇴":   "노후준비 키워드별 검색량.xlsx",
    "시니어 디지털":    "시니어디지털 키워드별 검색량.xlsx",
}
EXCEL_PRESET_NAMES = list(EXCEL_PRESETS.keys())
EXCEL_CUSTOM_LABEL = "사용자 정의"

EXCEL_PRESET_DESC = {
    "정부지원":         "정부지원금·바우처·복지 — 청년/주거/복지/바우처/취업/의료/교육 7개 카테고리",
    "비즈니스·경제":    "부동산·절세·금융상품 — 시장·세법·금융 비교 정보",
    "노후 준비·은퇴":   "국민연금·퇴직연금IRP·주택연금·노후자금설계",
    "시니어 디지털":    "스마트폰·메신저·간편결제·정부 모바일 앱 — 디지털 약자 보조",
    EXCEL_CUSTOM_LABEL: "사용자가 직접 지정한 엑셀 파일 경로예요.",
}

# ── 이미지 채움 엔진 선택(2026-09-09 Codex 전환) ────────────────────────
# ttk.Combobox 는 textvariable 을 그대로 화면에 찍기 때문에, 사용자에게는 한글
# 라벨을 보여주고 self.vars["image_fill_engine"] 에는 실제 값("codex"/"genspark")을
# 저장하려면 표시용 var 와 저장용 var 를 분리해야 한다 — 엑셀 도메인 프리셋 콤보
# (``_excel_preset_var``)와 같은 라벨↔값 분리 idiom을 그대로 따른다.
IMAGE_FILL_ENGINE_LABELS = {"codex": "Codex (권장)", "genspark": "젠스파크 (브라우저)"}
IMAGE_FILL_ENGINE_LABEL_TO_VALUE = {v: k for k, v in IMAGE_FILL_ENGINE_LABELS.items()}

_SEGMENTS = ["계정·연결", "발행", "품질", "외부 API", "자동화·진단"]

# 멀티채널 팩 후보 판정 파일명 — 이 중 하나라도 있으면 runs/<대상>/ 을 후보로 본다.
# auto-dev 5호부터는 gui.multichannel_service.CANDIDATE_FILES 가 단일 진실원본이고
# 여기선 alias 만 한다(테스트 호환 — 기존 이름을 계속 참조할 수 있게). 서비스가 아직
# 없으면(코더 B 작업 완료 전) 이전 리터럴로 폴백한다.
MULTICHANNEL_CANDIDATE_FILES = _MC_CANDIDATE_FILES or (
    "post_data.json", "draft.md", "draft_path.txt", "publish_brief.yaml",
)


def _probe_genspark_connection() -> dict:
    """젠스파크 로그인·사용 한도 상태를 1회 확인한다(라이브 — playwright 필요).

    ``core/genspark_client.py`` 의 CLI ``--check-login`` 경로와 정확히 같은
    시퀀스(``connect``→``open_image_tool``→``check_login``/``is_rate_limited``)
    를 재현한다 — genspark_client.py 자체는 읽기 전용(파일 경계)이라 이 조합
    로직은 이 파일에 둔다.

    반드시 이 함수 하나로 "실 브라우저를 부를 수도 있는 경계"를 좁혀 둔다 —
    ``gui/tabs/settings.py`` 의 다른 코드는 이 함수를 호출만 하고, 테스트는
    이 함수 자체를 monkeypatch 해 playwright 를 아예 임포트하지 않게 만들 수
    있다(``tests/conftest.py`` 가 ``genspark_client._debug_port_up``/
    ``ensure_browser`` 를 막아 두지만, 그건 크롬 기동만 막을 뿐 playwright
    구동 자체를 막지는 않는다 — 이 함수를 직접 갈아치우는 편이 더 확실하다).

    반환: {"ok": bool, "state": "ok"|"limited"|"login_required"|"error", "reason": str}.
    어떤 예외도 밖으로 던지지 않는다(fail-open) — 사람 말 사유만 돌려준다.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:  # noqa: BLE001 — 선택 의존성 미설치는 흔한 정상 경로
        log.error("젠스파크 연결 테스트 실패(playwright 미설치): %s", e, exc_info=True)
        return {"ok": False, "state": "error",
                "reason": "브라우저 자동화 도구가 설치돼 있지 않아요."}
    try:
        from core import genspark_client as gs
        with sync_playwright() as pw:
            _browser, ctx = gs.connect(pw, autolaunch=True)
            pg = gs.open_image_tool(ctx, reuse=True)
            logged = gs.check_login(pg)
            limited = gs.is_rate_limited(pg)
    except Exception as e:  # noqa: BLE001 — playwright/CDP 예외는 종류가 다양해
        # 특정 타입으로 못 좁힌다(core/genspark_client.py 자체도 같은 경계에서
        # 동일하게 넓게 잡는다 — is_rate_limited/check_login/ensure_browser 참고).
        log.error("젠스파크 연결 테스트 실패: %s", e, exc_info=True)
        return {"ok": False, "state": "error",
                "reason": "연결에 실패했어요 — 크롬이 열려 있는지 확인하세요."}
    if limited:
        return {"ok": False, "state": "limited",
                "reason": "사용 한도 제한이 감지됐어요 — 잠시 후 다시 시도하세요."}
    if not logged:
        return {"ok": False, "state": "login_required",
                "reason": "로그인이 필요해요 — 열린 크롬 창에서 1회 로그인하세요."}
    return {"ok": True, "state": "ok", "reason": "로그인 정상 · 사용 한도 정상이에요."}


def _probe_codex_connection() -> dict:
    """Codex CLI 로그인·준비 상태를 1회 확인한다(백그라운드 — ``codex login status``
    를 셸 아웃해 수 초 걸릴 수 있다).

    ``core.codex_image.probe()`` 를 그대로 감싼다 — 실제 생성은 하지 않고 상태만
    본다. 반드시 이 함수 하나로 "실 subprocess 를 부를 수도 있는 경계"를 좁혀
    둔다(``_probe_genspark_connection`` 과 같은 이유) — 테스트는 이 함수 자체를
    monkeypatch 해 실 codex CLI 를 전혀 타지 않게 만들 수 있다.

    반환: ``core.codex_image.probe()`` 의 dict 그대로
    (``{"ok": bool, "reason": str, "detail": str, ...}``). 어떤 예외도 밖으로
    던지지 않는다(fail-open) — 실패도 사람 말 사유로 돌려준다.
    """
    try:
        from core.codex_image import probe as _codex_probe
    except ImportError as e:  # noqa: BLE001 — 선택 모듈 미탑재 환경 방어
        log.error("Codex 연결 테스트 실패(모듈을 불러오지 못했어요): %s", e, exc_info=True)
        return {"ok": False, "reason": "error",
                "detail": "Codex 연동 모듈을 불러오지 못했어요."}
    try:
        return _codex_probe()
    except Exception as e:  # noqa: BLE001 — probe() 자체는 fail-open 설계지만
        # 예방 차원의 최후 방어선이다(core/genspark_client.py 호출부와 동일 관례
        # — 넓게 잡아 어떤 실패도 GUI 를 죽이지 않는다).
        log.error("Codex 연결 테스트 실패: %s", e, exc_info=True)
        return {"ok": False, "reason": "error", "detail": "연결 확인에 실패했어요."}


class SettingsTab(UiLifecycle, tk.Frame):
    def __init__(self, parent, app):
        tk.Frame.__init__(self, parent, bg=C["bg"])
        self.app = app
        self._init_lifecycle()
        self.vars = {}
        # 뉴스 데이터 API 옵션은 기존 공개 self.vars(38키) 계약과 분리한다.
        self._news_api_vars = {}
        self._preset_applying = False        # 프리셋 적용 중 변경 감지 억제
        self._excel_preset_applying = False  # 엑셀 프리셋 적용 중 변경 감지 억제
        # 품질 규칙 기본값 — cfg에 아직 없을 때 사용.
        # ⚠️ 여기에 숫자를 다시 박으면 settings.py 를 상향해도 이 화면이 옛 값을 씨앗으로
        #    깔고, 저장 한 번에 그 옛 값이 settings.py 로 되돌아간다(2026-08-02 감사 C1).
        #    반드시 코드 기본값(settings.py)에서 읽어온다.
        _qv_defaults = self._qv_code_defaults()
        for k, v in _qv_defaults.items():
            self.app.cfg.setdefault(k, v)
        # 젠스파크(레거시 엔진) 사용 허용 토글도 같은 이유로 코드 기본값에서 씨앗을
        # 심는다 — setdefault 없이 _toggle_field(..., False) 의 하드코딩 기본값만
        # 믿으면, 사용자가 이미 CLI 로 GENSPARK_ENABLED=True 를 켜 둔 라이브
        # 상태인데도 화면엔 '꺼짐'으로 잘못 보인다(2026-08-02 감사 C1과 같은
        # 클래스의 사고 — qv_code_defaults 참고).
        _genspark_defaults = self._genspark_code_defaults()
        for k, v in _genspark_defaults.items():
            self.app.cfg.setdefault(k, v)
        # 이미지 채움(엔진 중립) 세 필드도 같은 이유로 코드 기본값에서 씨앗을
        # 심는다(2026-09-09 Codex 전환 — 이 화면이 생기기 전엔 GUI 미노출이라
        # self.vars/user_settings.json 어디에도 이 키가 없었다).
        _image_fill_defaults = self._image_fill_code_defaults()
        for k, v in _image_fill_defaults.items():
            self.app.cfg.setdefault(k, v)
        self._genspark_accounts_cache: dict = {}
        self._segment_frames = {}
        self._segment_btns = {}
        self._segment_stripes = {}         # 활성 세그먼트 좌측 3px 스트라이프
        self._active_segment = _SEGMENTS[0]
        self._current_segment = None       # _card/_text_field 등이 등록 시 참조
        self._current_card_title = ""      # 검색 색인용 — 필드가 속한 카드 제목
        self._search_index: list = []      # [{segment, widget, label, hint, keys, text}]
        self._search_matches: list = []
        self._search_match_idx = -1
        self._search_debounce_id = None
        self._search_placeholder_active = False
        self._build()

    def destroy(self) -> None:
        self._cancel_search_debounce()
        # 창 높이 변화에 반응하려고 toplevel 에 직접 bind 했다(자기 자신이
        # 아니라 조상 위젯이라 파괴돼도 자동으로 안 풀린다) — 여기서 명시
        # 해제해 죽은 위젯을 계속 만지는 콜백이 남지 않게 한다.
        toplevel = getattr(self, "_toplevel_ref", None)
        bind_id = getattr(self, "_toplevel_configure_bind_id", None)
        if toplevel is not None and bind_id:
            try:
                toplevel.unbind("<Configure>", bind_id)
            except tk.TclError:
                pass
        UiLifecycle.destroy(self)

    @staticmethod
    def _qv_code_defaults() -> dict:
        """품질 규칙의 코드 기본값(settings.py). 로드 실패 시에만 리터럴 폴백."""
        try:
            from config import settings as _s
        except ImportError:  # noqa: BLE001 — settings 손상 시에도 화면은 떠야 한다
            _s = None
        def _g(name: str, fallback):
            return getattr(_s, name, fallback) if _s is not None else fallback
        return {
            "qv_min_chars":     _g("QV_MIN_CHARS", 1800),
            "qv_max_chars":     _g("QV_MAX_CHARS", 3200),
            "qv_min_h2":        _g("QV_MIN_H2_COUNT", 3),
            "qv_min_tags":      _g("QV_MIN_TAGS", 10),
            "qv_max_retries":   _g("QV_MAX_RETRIES", 2),
            "qv_require_table": _g("QV_REQUIRE_TABLE", True),
            "qv_require_faq":   _g("QV_REQUIRE_FAQ", True),
        }

    @staticmethod
    def _genspark_code_defaults() -> dict:
        """젠스파크(레거시 엔진) 사용 허용 토글의 코드 기본값(settings.py).

        2026-09-09 Codex 전환 — 편당 카드 수는 엔진 중립 필드(image_fill_cards)로
        옮겨갔고, 이 토글만 레거시 엔진 전용으로 남았다. _qv_code_defaults 와
        동일한 이유로 존재한다 — 로드 실패 시에만 리터럴 폴백."""
        try:
            from config import settings as _s
        except ImportError:  # noqa: BLE001 — settings 손상 시에도 화면은 떠야 한다
            _s = None
        def _g(name: str, fallback):
            return getattr(_s, name, fallback) if _s is not None else fallback
        return {
            "genspark_enabled": bool(_g("GENSPARK_ENABLED", False)),
        }

    @staticmethod
    def _image_fill_code_defaults() -> dict:
        """이미지 채움(엔진 중립) 네 필드의 코드 기본값(settings.py).

        2026-09-09 Codex 전환 — 기본 엔진이 젠스파크(브라우저 자동화)에서 Codex
        CLI 로 바뀌었다. 2026-09-10 — 제한 병렬(``image_fill_parallel``) 추가.
        _qv_code_defaults 와 동일한 이유로 존재한다 — 로드 실패 시에만 리터럴 폴백."""
        try:
            from config import settings as _s
        except ImportError:  # noqa: BLE001 — settings 손상 시에도 화면은 떠야 한다
            _s = None
        def _g(name: str, fallback):
            return getattr(_s, name, fallback) if _s is not None else fallback
        engine = str(_g("IMAGE_FILL_ENGINE", "codex") or "codex").strip().lower()
        if engine not in IMAGE_FILL_ENGINE_LABELS:
            engine = "codex"
        try:
            parallel = int(_g("IMAGE_FILL_PARALLEL", 2))
        except (TypeError, ValueError):
            parallel = 2
        return {
            "image_fill_enabled":  bool(_g("IMAGE_FILL_ENABLED", True)),
            "image_fill_engine":   engine,
            "image_fill_cards":    int(_g("IMAGE_FILL_CARDS", 3)),
            "image_fill_parallel": max(1, min(4, parallel)),
        }

    # ── 정보성 알림 — app.toast 있으면 토스트, 없으면(FakeApp 등) messagebox 폴백 ──
    def _notify(self, text: str, kind: str = "ok") -> None:
        toast = getattr(self.app, "toast", None)
        if toast is not None:
            try:
                toast.show(text, kind=kind)
                return
            except (tk.TclError, RuntimeError):
                pass
        messagebox.showinfo("알림", text)

    # ══════════════════════════════════════════════════════
    # 최상위 레이아웃 — 헤더 + 좌측 세그먼트 5개 + 우측 스크롤 + 하단 저장 바
    # ══════════════════════════════════════════════════════
    _SUBTITLE_FULL = "바꾼 뒤 아래 저장을 눌러야 적용돼요"
    # 아래 4개는 "논리 px"(스케일 1.0 기준) — 실제 사용은 항상 인스턴스
    # 메서드 안에서 px() 로 감싼 뒤에 한다(auto-dev 9호 Phase 1, 파일 상단
    # 주석 참조 — 클래스 속성에 미리 구우면 NBA_UI_SCALE 변경이 반영 안 됨).
    _SUBTITLE_HEIGHT_THRESHOLD_BASE = 640  # 이보다 창이 낮으면 부제를 접어 상단 스택을 줄인다
    _FORM_MAX_WIDTH_BASE = 640             # 설계서 §7-4 — 넓은 창에서도 폼은 이 폭에서 고정
    _NAV_WIDTH_BASE = 200
    _LABEL_COL_WIDTH_BASE = 160            # 설계서 §7-4 "라벨 열 160px" — FormField 를
                                            # 안 쓰는 수동 정렬 행(_label_col)에 적용

    def _build(self):
        # 이 인스턴스 수명 동안 쓸 스케일 px 값 — 생성 시점에 한 번 구해
        # 재사용한다(리사이즈 이벤트마다 다시 계산할 필요는 없다. 인스턴스
        # 메서드 호출 시점이라 monkeypatch 한 px() 도 정확히 반영된다).
        self._nav_width_px = px(self._NAV_WIDTH_BASE)
        self._form_max_width_px = px(self._FORM_MAX_WIDTH_BASE)
        self._subtitle_height_threshold_px = px(self._SUBTITLE_HEIGHT_THRESHOLD_BASE)
        self._label_col_width_px = px(self._LABEL_COL_WIDTH_BASE)

        header = Header(self, "설정", icon=icons.glyph("settings"))
        header.pack(fill="x", padx=SP["lg"], pady=(SP["lg"], SP["xs"]))
        # 화면당 Primary 1개 규칙(v3 §1) — 하단 고정 바의 "설정 저장"이 Primary, 헤더는 Secondary
        SecondaryBtn(header.actions, "저장", self._save, icon="save").pack(side="right")

        # 부제는 Header 내장 기능 대신 직접 소유한 라벨로 뺐다 — 창 높이가
        # 작을 때(디자이너 심사 지적①) 이 한 줄만 접어 상단 고정 스택 높이를
        # 줄이기 위해서다(Header 는 내부 라벨을 다시 노출하지 않는다).
        self._subtitle_lbl = L(self, self._SUBTITLE_FULL, FS, C["text_2"])
        self._subtitle_lbl.pack(fill="x", padx=SP["lg"], pady=(0, SP["sm"]),
                                anchor="w")
        Sep(self).pack(fill="x", padx=SP["lg"])

        self._build_search_bar(self)

        # 하단 고정 저장 바 — body 보다 먼저 pack 해야 항상 자리를 확보한다.
        bottom = tk.Frame(self, bg=C["surface"])
        bottom.pack(side="bottom", fill="x")
        tk.Frame(bottom, height=px(1), bg=C["border"]).pack(fill="x")
        bar_inner = tk.Frame(bottom, bg=C["surface"])
        bar_inner.pack(fill="x", padx=SP["lg"], pady=SP["sm"])
        self._save_btn_footer = PrimaryBtn(bar_inner, "설정 저장", self._save,
                                           icon="save")
        self._save_btn_footer.pack(side="left")
        L(bar_inner, "저장 즉시 적용돼요", FS, C["text_2"]).pack(side="left",
                                                              padx=(SP["md"], 0))
        bottom.update_idletasks()
        self._bottom_bar_height = max(bottom.winfo_reqheight(), px(44))

        body = tk.Frame(self, bg=C["bg"])
        body.pack(fill="both", expand=True, padx=SP["lg"], pady=(SP["sm"], 0))
        body.bind("<Configure>", self._on_body_configure)

        # 좌측 세그먼트 목차 — 설계서 §7-4 는 200px 세로 CTkSegmentedButton 을
        # 권장하지만 `Segmented` 위젯은 아직 orientation="vertical" 을 지원하지
        # 않아(가로형만) 세로 버튼 목록으로 근사한다(문서 §7-4 "없으면 CTk 버튼
        # 5개 세로" 대안 경로).
        nav = tk.Frame(body, bg=C["surface"], width=self._nav_width_px)
        nav.pack(side="left", fill="y")
        nav.pack_propagate(False)

        # 우측 폼 영역 — 디자이너 심사 지적③: 640 고정 대신
        # min(640, 가용폭-세그먼트 200-여백) 로 반응형(좁은 창에서 프리셋
        # 버튼 등이 밀려나가지 않게). 실제 폭 갱신은 _on_body_configure 가
        # body 크기 변화마다 다시 계산한다 — 초기값은 640 로 씨앗만 둔다.
        self._content_holder = tk.Frame(body, bg=C["bg"], width=self._form_max_width_px)
        self._content_holder.pack(side="left", fill="y", padx=(SP["md"], 0))
        self._content_holder.pack_propagate(False)
        content_holder = self._content_holder

        builders = {
            "계정·연결": self._build_account_segment,
            "발행": self._build_publish_segment,
            "품질": self._build_quality_segment,
            "외부 API": self._build_api_segment,
            "자동화·진단": self._build_ops_segment,
        }
        for name in _SEGMENTS:
            # 캡처 실측 수정(2026-09-03) — 활성 항목을 좌측 3px 스트라이프로
            # 표시(설계서 §4-13 사이드바 항목 관용구 재사용). 폰트는 선택
            # 여부와 무관하게 항상 F_BODY(13) — 굵기 전환 대신 스트라이프+
            # accent_soft 배경만으로 위계를 표현한다(원칙 #2: 크기 아닌
            # 명도·간격으로 위계).
            item = tk.Frame(nav, bg=C["surface"])
            item.pack(fill="x")
            stripe = tk.Frame(item, width=px(3), bg=C["surface"])
            stripe.pack(side="left", fill="y")
            self._segment_stripes[name] = stripe

            btn = tk.Button(item, text=name, font=FB, anchor="w", justify="left",
                            relief="flat", cursor="hand2", bd=0,
                            highlightthickness=0, padx=SP["md"], pady=px(10),
                            command=lambda n=name: self._show_segment(n))
            btn.pack(side="left", fill="x", expand=True)
            self._segment_btns[name] = btn

            scroll = ScrollArea(content_holder, bg=C["bg"])
            self._segment_frames[name] = scroll
            self._current_segment = name
            builders[name](scroll.inner)
            # 디자이너 심사 디자이너 지적① — 세그먼트 최하단 스페이서. 하단 고정 저장
            # 바 높이만큼 여유를 둬 마지막 카드가 저장 바에 가려 보이지
            # 않게 한다(스크롤 끝까지 내려도 카드 전체가 드러난다).
            tk.Frame(scroll.inner, height=self._bottom_bar_height + SP["lg"],
                    bg=C["bg"]).pack(fill="x")

        # ── 프리셋 변경 감지 trace (세그먼트 구성 후 — vars 가 모두 만들어진 뒤) ──
        _preset_keys = {"interval_sec", "use_reservation", "use_kakao",
                        "enable_cafe_dispatch", "qv_min_chars"}
        for k in _preset_keys:
            var = self.vars.get(k)
            if var is not None:
                var.trace_add("write", self._on_var_changed)

        self._show_segment(_SEGMENTS[0])

        try:
            toplevel = self.winfo_toplevel()
            self._toplevel_configure_bind_id = toplevel.bind(
                "<Configure>", self._on_toplevel_configure, add="+")
            self._toplevel_ref = toplevel
        except tk.TclError:
            self._toplevel_configure_bind_id = None
            self._toplevel_ref = None
        self._on_toplevel_configure()

    def _on_body_configure(self, event) -> None:
        """우측 폼 영역 반응형 폭 — min(640, 가용폭-세그먼트 200-여백)
        (디자이너 심사 디자이너 지적③). 화면이 넓어도 640 을 넘지 않고, 창이 좁으면
        프리셋 버튼 등이 밀려나가지 않도록 그만큼 줄어든다."""
        holder = getattr(self, "_content_holder", None)
        if holder is None:
            return
        available = event.width - self._nav_width_px - SP["md"] - SP["xs"]
        target = max(min(self._form_max_width_px, available), self._nav_width_px)
        try:
            holder.config(width=target)
        except tk.TclError:
            pass

    def _on_toplevel_configure(self, event=None) -> None:
        """창 높이가 작으면 부제를 접어 상단 고정 스택 높이를 줄인다
        (디자이너 심사 디자이너 지적①)."""
        lbl = getattr(self, "_subtitle_lbl", None)
        if lbl is None:
            return
        try:
            h = self.winfo_toplevel().winfo_height()
        except tk.TclError:
            return
        if h <= 1:
            return  # 아직 실측 전(초기 geometry 계산 중)
        text = "" if h < self._subtitle_height_threshold_px else self._SUBTITLE_FULL
        try:
            lbl.config(text=text)
        except tk.TclError:
            pass

    def _show_segment(self, name: str) -> None:
        if name not in self._segment_frames:
            return
        for seg, frame in self._segment_frames.items():
            if seg != name:
                frame.pack_forget()
        self._active_segment = name
        self._segment_frames[name].pack(fill="both", expand=True)
        for seg in self._segment_btns:
            self._style_segment_btn(seg)

    def _style_segment_btn(self, name: str) -> None:
        """선택된 세그먼트는 accent_soft 배경 + 좌측 3px accent 스트라이프
        (설계서 §4-13/§7-4). 폰트는 항상 F_BODY(13) 고정 — 굵기가 아니라
        스트라이프+배경 명도로만 선택 상태를 표현한다(캡처 실측 수정
        2026-09-03, 원칙 #2)."""
        btn = self._segment_btns[name]
        stripe = self._segment_stripes.get(name)
        selected = name == self._active_segment
        try:
            btn.config(
                bg=C["accent_soft"] if selected else C["surface"],
                fg=C["accent"] if selected else C["muted"],
                font=FB,
                activebackground=C["accent_soft"] if selected else C["card_hover"],
                activeforeground=C["accent"] if selected else C["text_2"])
            if stripe is not None:
                stripe.config(bg=C["accent"] if selected else C["surface"])
        except tk.TclError:
            pass

    # ── 공용 카드/필드 빌더 ──────────────────────────────
    def _card(self, parent, title, subtitle=None, icon=None, help_text=None):
        card = SectionCard(parent, title, subtitle=subtitle, icon=icon,
                           help_text=help_text)
        card.pack(fill="x", pady=(0, SP["md"]))
        self._current_card_title = title or ""
        return card

    def _label_col(self, parent, text):
        """FormField 를 안 쓰는 수동 정렬 행(Claude/스케줄 상태·엑셀 프리셋)의
        라벨 열 — 문자수 ``width=`` 대신 진짜 픽셀 폭 px(160) 으로 맞춘다
        (설계서 §7-4 "라벨 열 160px", auto-dev 9호 Phase 1 고DPI — 문자수
        기반 폭은 px() 스케일을 못 받는다). FormField 자신의 라벨 열은
        widgets.py(코더 A) 소관이라 여기서 건드리지 않는다."""
        col = tk.Frame(parent, width=self._label_col_width_px, bg=C["card"])
        col.pack(side="left", fill="y")  # fill="y" — 옆 위젯(StatusPill 등)과
                                          # 같은 행 높이로 늘어나게(propagate(False)
                                          # 는 "폭"만 고정하려는 의도이지 세로까지
                                          # 눌러 라벨을 잘라내려는 게 아니다).
        col.pack_propagate(False)
        L(col, text, FB, C["text_2"], anchor="e").pack(side="right", fill="x")
        return col

    def _text_field(self, parent, key, label, *, password=False, hint=None,
                    width=32, browse_cmd=None):
        var = tk.StringVar(value=str(self.app.cfg.get(key, "") or ""))
        self.vars[key] = var
        kind = "path" if browse_cmd else ("password" if password else "entry")
        ff = FormField(parent, label, var, kind=kind, hint=hint, width=width,
                       show_toggle=password, browse_cmd=browse_cmd)
        ff.pack(fill="x", pady=SP["xs"])
        self._register_search(ff, label, hint, (key,))
        return var

    def _num_field(self, parent, key, label, mn, mx, *, hint=None):
        var = tk.IntVar(value=self.app.cfg.get(key, mn))
        self.vars[key] = var
        ff = FormField(parent, label, var, kind="spin", values=(mn, mx),
                       hint=hint, width=8)
        ff.pack(fill="x", pady=SP["xs"])
        self._register_search(ff, label, hint, (key,))
        return var

    def _toggle_field(self, parent, key, label, *, hint=None):
        var = tk.BooleanVar(value=bool(self.app.cfg.get(key, False)))
        self.vars[key] = var
        ff = FormField(parent, label, var, kind="check", hint=hint)
        ff.pack(fill="x", pady=px(2))
        self._register_search(ff, label, hint, (key,))
        return var

    def _register_search(self, widget, label: str, hint, keys) -> None:
        """설정 찾기 검색 색인에 필드 하나를 등록한다(라벨·힌트·설정 키·카드 제목이
        검색 대상 — "카카오"처럼 필드 라벨엔 없어도 카드 제목엔 있는 경우를 잡는다)."""
        keys = tuple(keys or ())
        parts = [label or "", hint or "", self._current_card_title or "",
                 " ".join(keys)]
        self._search_index.append({
            "segment": self._current_segment,
            "widget": widget,
            "label": label or "",
            "hint": hint or "",
            "keys": keys,
            "text": " ".join(parts).lower(),
        })

    # ── 설정 찾기 검색창(업그레이드 2) ─────────────────────
    _SEARCH_PLACEHOLDER = "설정 찾기 — 예: 비밀번호, 글자 수, 카카오"

    def _build_search_bar(self, parent) -> None:
        """헤더 아래 고정 검색 행(ScrollArea 밖 — 세그먼트를 바꿔도 항상 보인다).

        캡처 실측 수정(2026-09-03) — 이전 버전은 FormField(라벨+입력+힌트 3단
        스택)를 그대로 써서 헤더 바로 아래 여백 없이 붙어 있었고, 실제 입력
        행이 눌려 힌트 문구만 보이는 것처럼 잘렸다. 이제 ①헤더와 SP.lg 만큼
        띄우고 ②아이콘+입력창 행을 height=36 으로 고정(pack_propagate(False)
        로 내용에 밀려 줄지 않게)하고 ③매치 결과 문구는 그 아래 별도 줄로 뺐다.
        """
        wrap = tk.Frame(parent, bg=C["bg"])
        wrap.pack(fill="x", padx=SP["lg"], pady=(SP["lg"], SP["sm"]))

        row = tk.Frame(wrap, bg=C["surface_2"], height=px(36))
        row.pack(fill="x")
        row.pack_propagate(False)

        icon_font = icons.icon_font(14) or FB
        tk.Label(row, text=icons.glyph("search"), font=icon_font,
                fg=C["muted"], bg=C["surface_2"]).pack(side="left",
                                                        padx=(SP["sm"], SP["xs"]))

        self._search_var = tk.StringVar()
        entry = tk.Entry(row, textvariable=self._search_var, font=FB,
                         fg=C["text"], bg=C["surface_2"],
                         insertbackground=C["accent"], relief="flat", bd=0,
                         highlightthickness=1, highlightbackground=C["border_strong"],
                         highlightcolor=C["focus"])
        entry.pack(side="left", fill="both", expand=True, padx=(0, SP["sm"]))
        self._search_entry = entry

        self._search_result_lbl = L(wrap, "", FS, C["text_2"])
        self._search_result_lbl.pack(anchor="w", pady=(SP["xs"], 0))

        self._search_var.trace_add("write", self._on_search_changed)
        entry.bind("<Return>", self._on_search_next)
        entry.bind("<Escape>", self._on_search_reset)
        entry.bind("<FocusIn>", self._on_search_focus_in)
        entry.bind("<FocusOut>", self._on_search_focus_out)
        self._set_search_placeholder()

    def _set_search_placeholder(self) -> None:
        self._search_placeholder_active = True
        try:
            self._search_var.set(self._SEARCH_PLACEHOLDER)
            self._search_entry.config(fg=C["muted"])
        except tk.TclError:
            pass
        finally:
            self._search_placeholder_active = False

    def _on_search_focus_in(self, _event=None) -> None:
        if self._search_var.get() != self._SEARCH_PLACEHOLDER:
            return
        self._search_placeholder_active = True
        try:
            self._search_var.set("")
            self._search_entry.config(fg=C["text"])
        except tk.TclError:
            pass
        finally:
            self._search_placeholder_active = False

    def _on_search_focus_out(self, _event=None) -> None:
        if not self._search_var.get().strip():
            self._set_search_placeholder()

    def _on_search_changed(self, *_args) -> None:
        if self._search_placeholder_active:
            return
        self._cancel_search_debounce()
        try:
            self._search_debounce_id = self.after(300, self._fire_search_debounce)
        except tk.TclError:
            self._search_debounce_id = None

    def _fire_search_debounce(self) -> None:
        self._search_debounce_id = None
        self._run_search()

    def _cancel_search_debounce(self) -> None:
        if self._search_debounce_id is not None:
            try:
                self.after_cancel(self._search_debounce_id)
            except (tk.TclError, ValueError):
                pass
            self._search_debounce_id = None

    def _flush_search_debounce(self) -> None:
        """대기 중인 디바운스를 취소하고 검색을 즉시 실행한다(테스트·즉시반영용)."""
        self._cancel_search_debounce()
        self._run_search()

    def _set_search_result_text(self, text: str) -> None:
        try:
            self._search_result_lbl.config(text=text)
        except tk.TclError:
            pass

    def _run_search(self) -> None:
        query = self._search_var.get().strip()
        if self._search_placeholder_active or not query or query == self._SEARCH_PLACEHOLDER:
            self._search_matches = []
            self._search_match_idx = -1
            self._set_search_result_text("")
            return
        q = query.lower()
        matches = [e for e in self._search_index if q in e["text"]]
        self._search_matches = matches
        if not matches:
            self._search_match_idx = -1
            self._set_search_result_text("찾는 설정이 없어요")
            return
        self._search_match_idx = 0
        self._set_search_result_text(f"{len(matches)}개 일치")
        self._jump_to_match(matches[0])

    def _on_search_next(self, _event=None):
        if not self._search_matches:
            return "break"
        self._search_match_idx = (self._search_match_idx + 1) % len(self._search_matches)
        self._jump_to_match(self._search_matches[self._search_match_idx])
        return "break"

    def _on_search_reset(self, _event=None):
        self._cancel_search_debounce()
        self._search_matches = []
        self._search_match_idx = -1
        self._set_search_result_text("")
        self._set_search_placeholder()
        return "break"

    def _jump_to_match(self, entry: dict) -> None:
        segment = entry["segment"]
        if segment:
            self._show_segment(segment)
        self._scroll_and_highlight(segment, entry["widget"])

    def _scroll_and_highlight(self, segment, widget) -> None:
        scroll = self._segment_frames.get(segment) if segment else None
        if scroll is not None:
            try:
                scroll.inner.update_idletasks()
                total_h = max(scroll.inner.winfo_height(), 1)
                frac = max(0.0, min(1.0, widget.winfo_y() / total_h))
                scroll._canvas.yview_moveto(frac)  # noqa: SLF001 — ScrollArea 내부 캔버스
            except tk.TclError:
                pass
        self._highlight_widget(widget)

    def _highlight_widget(self, widget) -> None:
        """search-jump 하이라이트 — highlightthickness 는 순정 tk 전용 옵션이라
        FormField 내부가 CTk 위젯(CTkSwitch/CTkComboBox 등)이면 ``cget``/
        ``config`` 가 TclError 대신 ValueError 를 던진다(customtkinter 6.0
        실측). 그런 필드는 하이라이트를 조용히 건너뛴다 — 검색 자체(세그먼트
        전환·매치 카운트)는 여전히 동작하므로 기능 손실은 없다."""
        try:
            prev_bg = widget.cget("highlightbackground")
            prev_thick = widget.cget("highlightthickness")
            widget.config(highlightthickness=2, highlightbackground=C["focus"])
        except (tk.TclError, ValueError):
            return
        self.ui_after(self._restore_highlight, widget, prev_bg, prev_thick, delay=1500)

    def _restore_highlight(self, widget, prev_bg, prev_thick) -> None:
        try:
            widget.config(highlightbackground=prev_bg, highlightthickness=prev_thick)
        except (tk.TclError, ValueError):
            pass

    # ══════════════════════════════════════════════════════
    # 세그먼트 ① 계정·연결
    # ══════════════════════════════════════════════════════
    def _build_account_segment(self, parent):
        self._preset_section(parent)
        self._claude_section(parent)
        acct = self._card(parent, "네이버 계정", icon="🆔")
        self._text_field(acct.body, "naver_id", "아이디")
        self._text_field(acct.body, "naver_pw", "비밀번호", password=True)
        self._no_password_section(parent)

    # ── 프리셋 관련 ──────────────────────────────────────────
    def _preset_section(self, parent):
        """프리셋 3버튼을 최상단에 배치(UX P1 — 원클릭).

        개별 설정을 만지면 어느 버튼도 켜지지 않은 '사용자 정의' 상태가 된다.
        """
        card = self._card(parent, "운영 프리셋", "버튼 하나로 전체 설정을 바꿔요")
        self._preset_var = tk.StringVar(value=self._detect_current_preset())

        row = tk.Frame(card.body, bg=C["card"])
        row.pack(fill="x", pady=px(3))
        self._preset_btns = {}
        # 디자이너 심사 🟡2 — 3열 균등 grid(uniform). pack(side="left") 는
        # 버튼 각각이 자기 폭을 고집해 좁은 창에서 3번째("성장 모드")가
        # 화면 밖으로 밀려났다 — grid+uniform 은 좁아지면 3버튼이 같이
        # 줄어들어(sticky="ew") 항상 폼 폭 안에 들어온다.
        for col in range(len(PRESET_NAMES)):
            row.grid_columnconfigure(col, weight=1, uniform="preset")
        for i, name in enumerate(PRESET_NAMES):
            # 계약(설계서 §0.2·§7-4 예외 지침): _preset_btns 는 dict 3개·선택
            # 시 ["bg"] 차이를 테스트가 직접 단언한다 — Segmented 로 바꾸면
            # 이 계약이 깨지므로 순정 tk 인 PlainBtn 을 그대로 쓰고, 패딩만
            # 조여 알약(pill)에 가깝게 보이도록 마감한다(진짜 라운드는 순정
            # tk 로 못 그린다 — 설계서 §4 "R.pill" 은 CTk 전용).
            # w=None — PlainBtn 기본(문자 14폭) 대신 grid 가 셀 폭을
            # 정하게 한다(고정 문자폭이면 uniform 컬럼이 있어도 안 줄어든다).
            b = PlainBtn(row, name, lambda n=name: self._apply_preset_button(n),
                        w=None)
            # 캡처 실측 수정(2026-09-03) — 좌우 패딩 14·높이 ≈30px(pady 5) 로
            # 알약(pill) 형태에 근접시킨다(진짜 radius 999 는 순정 tk 로 못
            # 그린다 — border 없이 색 대비만으로 pill 인상을 낸다).
            b.config(padx=px(14), pady=px(5), bd=0, relief="flat", cursor="hand2",
                    highlightthickness=0)
            b.grid(row=0, column=i, sticky="ew",
                  padx=(0, px(8)) if i < len(PRESET_NAMES) - 1 else 0)
            self._preset_btns[name] = b

        self._preset_desc_label = L(
            card.body, self._preset_description(self._preset_var.get()),
            FS, C["text_2"])
        self._preset_desc_label.pack(anchor="w", pady=(6, 0))
        self._refresh_preset_buttons()

    def _apply_preset_button(self, name: str):
        self._preset_var.set(name)
        self._on_preset_selected()

    def _refresh_preset_buttons(self):
        """활성 프리셋 버튼만 강조 — '사용자 정의'면 전부 비강조.

        캡처 실측 수정(2026-09-03) — 활성 accent_soft 배경+accent 글자,
        비활성 surface_2 배경(설계서 §7-4 pill 색 규칙)."""
        if not hasattr(self, "_preset_btns"):
            return
        active = self._preset_var.get()
        for name, b in self._preset_btns.items():
            try:
                if name == active:
                    b.config(fg=C["accent"], bg=C["accent_soft"],
                             activebackground=C["accent_soft"],
                             activeforeground=C["accent"])
                else:
                    b.config(fg=C["text_2"], bg=C["surface_2"],
                             activebackground=C["card_hover"],
                             activeforeground=C["text"])
            except tk.TclError:
                pass

    def _detect_current_preset(self) -> str:
        """현재 cfg 값이 어떤 프리셋과 일치하는지 확인합니다."""
        cfg = self.app.cfg
        for name, vals in PRESETS.items():
            match = True
            for key, expected in vals.items():
                current = cfg.get(key)
                if current is None:
                    continue
                if current != expected:
                    match = False
                    break
            if match:
                return name
        return CUSTOM_LABEL

    def _preset_description(self, preset_name: str) -> str:
        descs = {
            "안전 모드": "간격 5분 · 예약발행 · 카카오 알림 꺼짐 · 최소 2000자 — 안전 우선",
            "균형 모드": "간격 3분 · 예약발행 · 카카오 알림 켜짐 · 최소 1500자 — 균형 잡힌 기본값",
            "성장 모드": "간격 2분 · 예약발행 · 카카오 알림 켜짐 · 카페 배포 켜짐 · 최소 1500자",
            CUSTOM_LABEL: "사용자가 직접 설정한 값이에요.",
        }
        return descs.get(preset_name, "")

    def _on_preset_selected(self, event=None):
        """프리셋 선택 시 해당 설정값을 GUI 위젯에 자동 반영합니다."""
        name = self._preset_var.get()
        if name == CUSTOM_LABEL or name not in PRESETS:
            self._update_preset_desc()
            return
        self._preset_applying = True
        try:
            vals = PRESETS[name]
            for key, value in vals.items():
                var = self.vars.get(key)
                if var is not None:
                    var.set(value)
        finally:
            self._preset_applying = False
        self._update_preset_desc()

    def _on_var_changed(self, *_args):
        """개별 설정이 수정되면 프리셋을 '사용자 정의'로 전환합니다."""
        if self._preset_applying:
            return
        if hasattr(self, '_preset_var') and self._preset_var.get() != CUSTOM_LABEL:
            self._preset_var.set(CUSTOM_LABEL)
            self._update_preset_desc()

    def _update_preset_desc(self):
        if hasattr(self, '_preset_desc_label'):
            self._preset_desc_label.config(
                text=self._preset_description(self._preset_var.get()))
        self._refresh_preset_buttons()

    # ── Claude 연결 (인앱 로그인) ────────────────────────
    def _claude_section(self, parent):
        """검은 CMD 없이 앱 안에서 Claude(글 생성 AI)를 연결하는 섹션."""
        card = self._card(parent, "Claude 연결", "글을 써 주는 AI를 연결해요")
        row = tk.Frame(card.body, bg=C["card"])
        row.pack(fill="x", pady=px(3))
        self._label_col(row, "현재 상태")
        self._claude_status_pill = StatusPill(row, "확인 중…", kind="info")
        self._claude_status_pill.pack(side="left", padx=(SP["sm"], 0))

        btn_row = tk.Frame(card.body, bg=C["card"])
        btn_row.pack(fill="x", pady=(SP["sm"], 4))
        self._claude_connect_btn = PrimaryBtn(btn_row, "연결하기",
                                              self._on_claude_connect)
        self._claude_connect_btn.pack(side="left", padx=(0, 8))
        SubtleBtn(btn_row, "수동 로그인", self._on_claude_manual).pack(side="left")

        L(card.body, "검은 창 없이 앱 안에서 로그인해요. 버튼을 누르면 브라우저가 열려요.",
          FS, C["text_2"]).pack(anchor="w", pady=(4, 0))

        self._claude_connecting = False
        self._claude_poll_left = 0
        self.ui_after(self._refresh_claude_status, delay=400)

    def _refresh_claude_status(self):
        """claude_connect.status()로 연결 상태를 갱신(백그라운드)."""
        self.spawn(self._check_claude_status, name="claude-status")

    def _check_claude_status(self) -> None:
        try:
            from core.claude_connect import status as _st
            st = _st()
        except ImportError:  # noqa: BLE001
            st = "unknown"
        text, kind = {
            "connected": ("연결됨", "ok"),
            "installed": ("로그인이 필요해요 — [연결하기]", "warn"),
            "missing":   ("설치가 안 돼 있어요 — 다시 설치해 보세요", "error"),
        }.get(st, ("확인할 수 없어요", "neutral"))
        self.ui_after(self._set_claude_status, text, kind)

    def _set_claude_status(self, text: str, kind: str) -> None:
        try:
            self._claude_status_pill.set(text, kind)
        except tk.TclError:
            pass

    def _on_claude_connect(self):
        if getattr(self, "_claude_connecting", False):
            return
        try:
            from core import claude_connect
        except ImportError:
            messagebox.showerror("Claude 연결", "연결 모듈을 불러오지 못했어요.")
            return
        if claude_connect.is_authenticated():
            self._refresh_claude_status()
            self._notify("이미 연결돼 있어요.", kind="info")
            return
        if not claude_connect.is_installed():
            self._notify("Claude가 설치돼 있지 않아요. '수동 로그인'을 눌러 주세요.",
                         kind="warn")
            return
        proc = claude_connect.start_login()
        if proc is None:
            self._notify("연결 실행에 실패했어요. '수동 로그인'을 시도해 주세요.",
                         kind="warn")
            return
        self._claude_connecting = True
        self._claude_poll_left = 60  # 60 × 2s = 최대 120초
        self._set_claude_status("브라우저에서 로그인해 주세요 · 확인 중…", "info")
        self._poll_claude_login()

    def _poll_claude_login(self):
        try:
            from core.claude_connect import is_authenticated
            if is_authenticated():
                self._claude_connecting = False
                self._refresh_claude_status()
                self._notify("연결됐어요!", kind="ok")
                return
            self._claude_poll_left -= 1
            if self._claude_poll_left <= 0:
                self._claude_connecting = False
                self._set_claude_status("완료를 확인하지 못했어요 — 로그인을 마쳤다면 다시 확인해 주세요",
                                        "warn")
                return
            self.ui_after(self._poll_claude_login, delay=2000)
        except tk.TclError:
            self._claude_connecting = False

    def _on_claude_manual(self):
        try:
            from core.claude_connect import open_manual_login
            if not open_manual_login():
                self._notify("로그인 도구를 찾지 못했어요. 프로그램을 다시 설치해 보세요.",
                             kind="warn")
        except (ImportError, OSError) as e:
            log.error("수동 로그인 실행 실패: %s", e, exc_info=True)
            messagebox.showerror("수동 로그인", "실행에 실패했어요.")

    # ── 무비번 로그인 (관문 ③ 자격증명 투명성) ─────────────
    def _no_password_section(self, parent):
        """무비번(쿠키) 로그인 토글. 켜면 비밀번호를 저장하지 않고 사용자가 직접 로그인.

        신뢰↔무인 trade-off 를 문구로 명확히 고지한다. 켤 때는 저장된 비밀번호를
        실제로 비워 '비번 저장 0' 약속을 지킨다.
        """
        card = self._card(parent, "무비번 로그인", "비밀번호를 저장하지 않고 싶을 때")
        var = tk.BooleanVar(value=bool(self.app.cfg.get("no_password_login", False)))
        self.vars["no_password_login"] = var
        hint = "켜면 비밀번호를 저장하지 않고, 세션이 끊기면 직접 로그인해요."
        ff = FormField(card.body, "직접 로그인", var, kind="check", hint=hint)
        ff.pack(fill="x", pady=px(2))
        self._register_search(ff, "직접 로그인", hint, ("no_password_login",))
        var.trace_add("write", lambda *_a: self._on_no_password_toggle())

        L(card.body,
          "켜면: 네이버 비밀번호를 저장하지 않아요(로그인 정보만 보관).\n"
          "대신 무인 자동발행 중 세션이 끊기면 직접 다시 로그인해야 해요.\n"
          "끄면(기본): 비밀번호를 암호화 저장해 아침 자동발행 등 무인 운영을 지속해요.",
          FS, C["text_2"], justify="left").pack(anchor="w", pady=(4, 0))

    def _on_no_password_toggle(self):
        """무비번 ON 시 저장된 비밀번호 필드를 비워 '비번 저장 0'을 실제로 지킨다."""
        try:
            v = self.vars.get("no_password_login")
            if v is not None and v.get():
                pwvar = self.vars.get("naver_pw")
                if pwvar is not None:
                    pwvar.set("")
        except tk.TclError:
            pass

    # ══════════════════════════════════════════════════════
    # 세그먼트 ② 발행
    # ══════════════════════════════════════════════════════
    def _build_publish_segment(self, parent):
        self._excel_preset_section(parent)
        excel_card = self._card(parent, "엑셀 파일", icon=icons.glyph("folder"))
        self._text_field(excel_card.body, "excel_path", "파일 경로",
                         browse_cmd=self._browse_excel)
        self._text_field(excel_card.body, "sheet_name", "시트 이름")
        excel_var = self.vars.get("excel_path")
        if excel_var is not None:
            excel_var.trace_add("write", self._on_excel_path_changed)

        posting = self._card(parent, "포스팅 설정")
        self._num_field(posting.body, "daily_limit", "하루 최대 포스팅 수", 1, 20)
        self._num_field(posting.body, "posts_per_day", "하루 예약 발행 개수", 1, 50)
        self._num_field(posting.body, "interval_sec", "포스팅 준비 간격(초)", 30, 600)
        self._num_field(posting.body, "max_retry", "실패 재시도 횟수", 0, 5)

        toggles = self._card(parent, "기능 설정", icon=icons.glyph("settings"))
        for key, label in [
            ("use_reservation", "예약발행 사용"),
            ("save_preview", "업로드 전 미리보기 저장"),
            ("headless", "백그라운드 실행(화면 숨김)"),
            ("skip_image_insert", "이미지 자동삽입 건너뛰기"),
            ("use_kakao", "카카오톡 알림"),
            ("enable_cafe_dispatch", "카페 자동 배포"),
            ("use_trend_update", "매주 트렌드 자동 갱신"),
            ("auto_video_after_publish", "발행 후 쇼츠 영상 자동 생성(예약발행 성공 시)"),
        ]:
            self._toggle_field(toggles.body, key, label)

        video = self._card(parent, "발행 후 영상")
        self._num_field(video.body, "auto_video_limit", "한 번에 생성할 최대 편수", 1, 20)

    def _browse_excel(self):
        path = filedialog.askopenfilename(filetypes=[("Excel", "*.xlsx *.xls")])
        if path:
            var = self.vars.get("excel_path")
            if var is not None:
                var.set(path)

    # ── 엑셀 프리셋 (도메인 전환) ─────────────────────────
    @staticmethod
    def _is_default_profile() -> bool:
        """활성 프로필이 default(첫 블로그)인지. 실패 시 True(레거시 동작 보존)."""
        try:
            from config import profile as _profile
            return _profile.is_default()
        except (ImportError, AttributeError):  # noqa: BLE001 — 프로필 모듈 부재 시 레거시 동작
            return True

    def _excel_preset_section(self, parent):
        """엑셀 파일 섹션 위에 도메인 프리셋 드롭다운을 배치합니다.

        default 프로필은 기존 정부지원 도메인 프리셋을 그대로 렌더한다.
        named 프로필(예: 영어교육)은 도메인 프리셋을 노출하지 않고,
        해당 프로필의 실제 키워드 엑셀 경로만 간소하게 표시한다.
        """
        if not self._is_default_profile():
            self._excel_preset_section_named(parent)
            return

        card = self._card(parent, "엑셀 발행 라인", icon=icons.glyph("folder"))
        row = tk.Frame(card.body, bg=C["card"])
        row.pack(fill="x", pady=px(3))
        self._label_col(row, "도메인 프리셋")

        self._excel_preset_var = tk.StringVar(
            value=self._detect_current_excel_preset())
        combo = ttk.Combobox(
            row, textvariable=self._excel_preset_var,
            values=EXCEL_PRESET_NAMES + [EXCEL_CUSTOM_LABEL],
            state="readonly", font=FB, width=22)
        combo.pack(side="left", padx=(SP["sm"], 0), ipady=4)
        combo.bind("<<ComboboxSelected>>", self._on_excel_preset_selected)
        self._register_search(
            combo, "도메인 프리셋",
            "정부지원·비즈니스·경제·노후 준비·은퇴·시니어 디지털 중에서 골라 "
            "엑셀 파일을 한 번에 바꿔요.",
            ("excel_preset",))

        self._excel_preset_desc = L(
            card.body, EXCEL_PRESET_DESC.get(self._excel_preset_var.get(), ""),
            FS, C["text_2"])
        self._excel_preset_desc.pack(anchor="w", pady=(4, 0))

    def _excel_preset_section_named(self, parent):
        """named 프로필용 간소 엑셀 섹션 — default 도메인 프리셋을 숨긴다.

        도메인 프리셋 드롭다운 대신 이 프로필의 실제 키워드 엑셀 경로만 표시한다.
        (brand.excel_path() 우선, 그다음 keywords_dir 의 *.xlsx 목록.)
        """
        card = self._card(parent, "엑셀 발행 라인", icon=icons.glyph("folder"))
        excel_path = ""
        try:
            from config import brand as _brand
            excel_path = _brand.excel_path() or ""
        except (ImportError, AttributeError):  # noqa: BLE001 — 헬퍼 부재 시 cfg 폴백
            excel_path = ""
        if not excel_path:
            excel_path = self.app.cfg.get("excel_path", "") or ""

        display = Path(excel_path).name if excel_path else "(엑셀 파일 미지정)"
        row = tk.Frame(card.body, bg=C["card"])
        row.pack(fill="x", pady=px(3))
        self._label_col(row, "현재 엑셀")
        L(row, display, FB, C["text"]).pack(side="left", padx=(SP["sm"], 0))

        xlsx_names = self._named_profile_xlsx_names()
        if xlsx_names:
            L(card.body, "이 프로필 키워드 엑셀: " + ", ".join(xlsx_names),
              FS, C["text_2"]).pack(anchor="w", pady=(4, 0))
        else:
            L(card.body, "아래 '엑셀 파일' 경로로 이 프로필의 키워드 엑셀을 지정하세요.",
              FS, C["text_2"]).pack(anchor="w", pady=(4, 0))

    @staticmethod
    def _named_profile_xlsx_names() -> list:
        """named 프로필 keywords 폴더의 *.xlsx 파일명 목록. 실패 시 빈 리스트."""
        try:
            from config import profile as _profile
            kd = _profile.keywords_dir()
            if kd is not None and kd.is_dir():
                return sorted(p.name for p in kd.glob("*.xlsx"))
        except (ImportError, AttributeError, OSError):  # noqa: BLE001 — 목록 실패는 화면만 비움
            pass
        return []

    def _detect_current_excel_preset(self) -> str:
        """현재 입력된 excel_path 의 파일명을 프리셋과 매칭합니다.

        var가 이미 만들어졌으면 var 값을, 아니면 cfg 값을 본다.
        """
        var = self.vars.get("excel_path")
        if var is not None:
            try:
                path = var.get() or ""
            except tk.TclError:
                path = ""
        else:
            path = self.app.cfg.get("excel_path", "") or ""
        try:
            fname = Path(path).name if path else ""
        except (TypeError, ValueError):
            fname = ""
        for name, preset_fname in EXCEL_PRESETS.items():
            if fname == preset_fname:
                return name
        return EXCEL_CUSTOM_LABEL

    def _on_excel_preset_selected(self, event=None):
        """프리셋 선택 시 excel_path 변수를 해당 파일명으로 갱신합니다."""
        name = self._excel_preset_var.get()
        if name == EXCEL_CUSTOM_LABEL:
            self._update_excel_preset_desc()
            return
        target_fname = EXCEL_PRESETS.get(name)
        if not target_fname:
            return
        var = self.vars.get("excel_path")
        if var is not None:
            self._excel_preset_applying = True
            try:
                var.set(target_fname)
            finally:
                self._excel_preset_applying = False
        self._update_excel_preset_desc()

    def _on_excel_path_changed(self, *_args):
        """엑셀 경로 직접 편집 시 프리셋 라벨을 '사용자 정의'로 전환합니다."""
        if getattr(self, "_excel_preset_applying", False):
            return
        if not hasattr(self, "_excel_preset_var"):
            return
        detected = self._detect_current_excel_preset()
        if self._excel_preset_var.get() != detected:
            self._excel_preset_var.set(detected)
            self._update_excel_preset_desc()

    def _update_excel_preset_desc(self):
        if hasattr(self, "_excel_preset_desc"):
            self._excel_preset_desc.config(
                text=EXCEL_PRESET_DESC.get(self._excel_preset_var.get(), ""))

    # ══════════════════════════════════════════════════════
    # 세그먼트 ③ 품질
    # ══════════════════════════════════════════════════════
    def _build_quality_segment(self, parent):
        self._editorial_section(parent)
        card = self._card(parent, "품질 규칙", "발행 전 꼭 맞춰야 하는 최소 기준이에요")
        num_fields = [
            ("qv_min_chars",   500, 3000),
            ("qv_max_chars",   2000, 6000),
            ("qv_min_h2",      1, 10),
            ("qv_min_tags",    5, 20),
            ("qv_max_retries", 0, 5),
        ]
        for key, mn, mx in num_fields:
            self._num_field(card.body, key, glossary.label(key), mn, mx,
                            hint=glossary.help(key))
        Sep(card.body).pack(fill="x", pady=(SP["sm"], SP["xs"]))
        for key in ("qv_require_table", "qv_require_faq"):
            self._toggle_field(card.body, key, glossary.label(key),
                               hint=glossary.help(key))

    # ── 이 계정의 편집 방향(프로필 전용 저장 — self.vars 38키 계약 밖) ────────
    def _editorial_section(self, parent):
        """대상 독자·해결할 문제·문체·썸네일 색을 이 계정에만 저장하는 카드.

        ⚠️ 이 카드의 값은 ``self.vars``/``user_settings.json``/``settings.py`` 에 **들어가지
        않는다.** 계정(프로필)마다 달라야 하는 값이라 ``core.editorial_profile`` 이
        프로필 폴더의 별도 파일에 저장한다 → 38키 계약 불변, 계정 간 누출 없음.
        저장 버튼도 하단 공용 저장과 분리돼 있다(이 카드만 따로 저장).
        """
        card = self._card(parent, "이 계정의 편집 방향",
                          "글과 썸네일이 겨냥할 독자 · 선택",
                          icon=icons.glyph("content"),
                          help_text="누구에게 무엇을 해결해 주는 블로그인지 적어 두면 "
                                    "글의 도입부와 썸네일 문구가 그 방향으로 만들어져요.")
        self._editorial_vars = {}
        self._editorial_loaded = {}

        try:
            from core import editorial_profile as ep
            data = ep.load(category="")
        except Exception as exc:  # noqa: BLE001 — 조회 실패 시 편집 불가 안내만
            log.warning("편집 방향 조회 실패: %s", exc)
            EmptyState(card.body, "편집 방향을 불러올 수 없어요",
                       "프로그램을 다시 시작한 뒤에도 같으면 알려주세요.").pack(
                fill="x", pady=SP["sm"])
            return
        self._editorial_loaded = dict(data)

        thumb = data.get("thumbnail") or {}
        rows = (
            ("audience", "읽는 사람", str(data.get("audience") or ""), 44,
             "예: 60대 이상 어르신과 그 가족. 비워 두면 카테고리 기본값을 써요."),
            ("reader_problem", "해결할 문제", str(data.get("reader_problem") or ""), 44,
             "예: 제도가 있는 건 아는데 내가 대상인지 모른다. 도입부가 이 문제부터 답해요."),
            ("voice_note", "문체 지침", str(data.get("voice_note") or ""), 44,
             "예: 높임말, 행정 용어는 풀어서. 비워 두면 카테고리 톤을 그대로 써요."),
        )
        for key, label, value, width, hint in rows:
            var = tk.StringVar(value=value)
            self._editorial_vars[key] = var
            ff = FormField(card.body, label, var, kind="entry", hint=hint, width=width)
            ff.pack(fill="x", pady=SP["xs"])
            self._register_search(ff, label, hint, (key,))

        Sep(card.body).pack(fill="x", pady=(SP["sm"], SP["xs"]))
        L(card.body, "썸네일 색 (#RRGGBB)", FB, C["text_2"]).pack(anchor="w")
        for key, label, hint in (
            ("bg", "배경색", "썸네일 아래쪽 글자 띠의 색이에요."),
            ("accent", "강조색", "글자 위 짧은 강조 막대 색이에요."),
            ("text", "글자색", "썸네일 문구 색이에요. 배경색과 밝기 차이가 커야 읽혀요."),
        ):
            var = tk.StringVar(value=str(thumb.get(key) or ""))
            self._editorial_vars[f"thumb_{key}"] = var
            ff = FormField(card.body, label, var, kind="entry", hint=hint, width=14)
            ff.pack(fill="x", pady=px(2))
            self._register_search(ff, label, hint, (f"thumb_{key}",))
        mood_var = tk.StringVar(value=str(thumb.get("mood") or ""))
        self._editorial_vars["thumb_mood"] = mood_var
        mood_hint = "예: 차분하고 신뢰감 있는 분위기. 배경 이미지를 만들 때 참고해요."
        mood_ff = FormField(card.body, "분위기 메모", mood_var, kind="entry",
                            hint=mood_hint, width=44)
        mood_ff.pack(fill="x", pady=SP["xs"])
        self._register_search(mood_ff, "분위기 메모", mood_hint, ("thumb_mood",))

        btn_row = tk.Frame(card.body, bg=C["card"])
        btn_row.pack(fill="x", pady=(SP["sm"], 4))
        SecondaryBtn(btn_row, "편집 방향 저장", self._on_editorial_save,
                     icon="save").pack(side="left", padx=(0, 8))
        saved_at = str(data.get("updated_at") or "")[:16].replace("T", " ")
        initial = (f"저장됨 · {saved_at}" if data.get("configured") and saved_at
                   else ("저장됨" if data.get("configured") else "아직 설정하지 않았어요"))
        self._editorial_pill = StatusPill(
            btn_row, initial, kind="ok" if data.get("configured") else "neutral")
        self._editorial_pill.pack(side="left")

        for text in ("이 값은 지금 쓰는 계정에만 저장돼요 — 다른 계정에는 영향이 없어요.",
                     "저장한 뒤 새로 만드는 글부터 적용돼요. 이미 만들어 둔 글은 그대로예요.",
                     self._thumb_font_note()):
            if text:
                L(card.body, text, FS, C["text_2"]).pack(anchor="w")

    def _thumb_font_note(self) -> str:
        """썸네일 한글 합성 가능 여부 한 줄(조치 문구 포함). 실패는 조용히 넘기지 않는다."""
        try:
            from core import thumbnail_compose as tc
            status = tc.availability()
        except Exception:  # noqa: BLE001 — 진단 실패는 안내 생략
            return ""
        if status.get("ok"):
            return ("썸네일 글자는 이 컴퓨터의 한글 글꼴로 직접 그려요 — "
                    "AI가 글자를 흘려 쓰거나 잘못 새기는 일이 없어요(적어 준 문구 그대로 그려요).")
        return ("⚠️ 이 컴퓨터에 한글 글꼴이 없어 썸네일 문구를 그릴 수 없어요. "
                "맑은 고딕·나눔고딕 같은 한글 글꼴을 설치한 뒤 다시 시도해 주세요.")

    def _on_editorial_save(self):
        """편집 방향만 따로 저장한다(공용 저장과 분리 — 프로필 파일 계약)."""
        values = {}
        try:
            values = {key: var.get() for key, var in
                      (self._editorial_vars or {}).items()}
        except tk.TclError:
            return
        payload = {
            "audience": values.get("audience", ""),
            "reader_problem": values.get("reader_problem", ""),
            "voice_note": values.get("voice_note", ""),
            "thumbnail": {
                "bg": values.get("thumb_bg", ""),
                "accent": values.get("thumb_accent", ""),
                "text": values.get("thumb_text", ""),
                "mood": values.get("thumb_mood", ""),
            },
        }
        try:
            from core import editorial_profile as ep
            result = ep.save(payload)
        except Exception as exc:  # noqa: BLE001 — 저장 실패를 성공으로 보이지 않게
            log.error("편집 방향 저장 실패: %s", exc, exc_info=True)
            self._set_editorial_status("저장하지 못했어요. 잠시 뒤 다시 시도해 주세요.", "error")
            return
        if not result.get("saved"):
            self._set_editorial_status(
                "저장하지 못했어요 — 계정 폴더에 쓸 수 없어요.", "error")
            return
        # 색은 형식이 틀리면 기본값으로 정리되므로 화면 값을 정리된 값으로 되돌려 준다.
        try:
            thumb = result.get("thumbnail") or {}
            for key in ("bg", "accent", "text"):
                var = (self._editorial_vars or {}).get(f"thumb_{key}")
                if var is not None:
                    var.set(str(thumb.get(key) or ""))
        except tk.TclError:
            pass
        self._set_editorial_status("저장했어요 — 새로 만드는 글부터 적용돼요.", "ok")
        toast = getattr(self.app, "toast", None)
        if callable(toast):
            toast("편집 방향을 저장했어요")

    def _set_editorial_status(self, text: str, kind: str) -> None:
        try:
            self._editorial_pill.set(text, kind)
        except (AttributeError, tk.TclError):
            pass

    # ══════════════════════════════════════════════════════
    # 세그먼트 ④ 외부 API
    # ══════════════════════════════════════════════════════
    def _build_api_segment(self, parent):
        open_api = self._card(parent, "네이버 오픈 API", "선택 — 없어도 발행은 동작해요",
                              help_text=glossary.help("open_api"))
        self._text_field(open_api.body, "api_client_id", "기존 API Client ID")
        self._text_field(open_api.body, "api_client_secret", "기존 API Client Secret",
                         password=True)
        provider_var = tk.StringVar(value=str(self.app.cfg.get("naver_api_provider", "legacy")))
        self._news_api_vars["naver_api_provider"] = provider_var
        FormField(open_api.body, "검색 API 제공자", provider_var, kind="combo",
                  values=("legacy", "api_hub"),
                  hint="뉴스·검색 트렌드 수집에만 적용돼요. 저장하면 다음 수집부터 바로 반영되고, 기존 기능의 Client ID/Secret은 유지됩니다.").pack(fill="x")
        self._news_secret_field(open_api.body, "api_hub_key_id", "API HUB Key ID")
        self._news_secret_field(open_api.body, "api_hub_key", "API HUB Key")
        self._news_secret_field(open_api.body, "tourapi_service_key", "TourAPI 서비스 키")
        self._news_secret_field(
            open_api.body, "seoul_api_key", "서울 문화행사 API 키(현재 보류)", disabled=True,
            hint="공식 주소가 HTTP만 확인돼 API 키의 평문 전송 위험이 있어요. 키 입력과 직접 호출을 보류하고 CSV를 이용하세요.")
        L(open_api.body,
          "NAVER API HUB 안내: developers.naver.com/notice/article/32530 · 기존 API 종료 예정 2027-06-30\n"
          "TourAPI: api.visitkorea.or.kr · 서울 문화행사 API는 HTTPS 미확인으로 연결을 보류합니다.",
          FS, C["text_2"]).pack(anchor="w", pady=(2, SP["sm"]))
        self._toggle_field(open_api.body, "use_naver_api",
                           "자동 브라우저 대신 API 방식으로 업로드")
        for t in ["1. developers.naver.com 접속 → 애플리케이션 등록",
                  "2. API 선택: [블로그] + [검색] 모두 체크 · Callback URL은 https://localhost",
                  "   '검색'을 등록하지 않으면 순위·유입 조회가 실패해요.",
                  "3. Client ID·Client Secret을 복사해 위에 입력",
                  "4. 기존 Client ID/Secret 변경은 저장 후 프로그램을 다시 시작해야 적용돼요. 뉴스 제공자 선택은 다음 수집부터 반영돼요."]:
            L(open_api.body, t, FS, C["text_2"]).pack(anchor="w")

        self._searchad_section(parent)
        self._gov24_section(parent)
        self._image_fill_section(parent)

        kakao = self._card(parent, "카카오톡 알림",
                           help_text=glossary.help("kakao_notify"))
        self._text_field(kakao.body, "kakao_token", "액세스 토큰", password=True)

    def _news_secret_field(self, parent, key, label, *, disabled=False, hint=None):
        """뉴스 API 키 입력. 저장 계약의 기존 38개 vars 목록에는 섞지 않는다."""
        value = self.app.cfg.get(key, "")
        variable = tk.StringVar(value=str(value or ""))
        self._news_api_vars[key] = variable
        field = FormField(parent, label, variable, kind="password", show_toggle=not disabled,
                          hint=hint)
        if disabled:
            field.widget.configure(state="disabled")
        field.pack(fill="x")

    # ── 네이버 검색광고 API (실측 검색량 · 선택) ────────────
    def _searchad_section(self, parent):
        """검색광고 키워드도구 API 자격증명 입력 + 저장 전 연결 테스트.

        api_key·secret 는 마스킹 입력받아 encrypt_dict 로 암호화 저장되고,
        customer_id 는 계정번호라 평문. 세 값이 모두 채워지면 저장 시
        SEARCHAD_API_ENABLED 가 자동 True 가 된다(config_io._build_managed_values).
        연결 테스트는 저장 없이 입력값 그대로 1건 실측 조회해 즉시 사유를 보여준다.
        """
        card = self._card(parent, "네이버 검색광고 API", "실측 검색량 · 선택",
                          icon=icons.glyph("search"), help_text=glossary.help("searchad"))
        self._text_field(card.body, "searchad_api_key", "액세스 라이선스", password=True)
        self._text_field(card.body, "searchad_secret_key", "비밀키", password=True)
        self._text_field(card.body, "searchad_customer_id", "고객 ID(숫자)")

        btn_row = tk.Frame(card.body, bg=C["card"])
        btn_row.pack(fill="x", pady=(SP["sm"], 4))
        SecondaryBtn(btn_row, "연결 테스트", self._on_searchad_verify,
                    icon="search").pack(
            side="left", padx=(0, 8))
        self._searchad_status_pill = StatusPill(btn_row, "저장 전에 눌러서 확인해요",
                                                kind="neutral")
        self._searchad_status_pill.pack(side="left")

        for t in ["엑셀에 없는 키워드의 실제 월간검색수로 발행 우선순위를 정해요(선택 기능).",
                  "발급: searchad.naver.com → 도구 → 'API 사용 관리' → 라이선스 발급(무료).",
                  "액세스 라이선스·비밀키는 암호화 저장되고, 고객 ID는 평문이에요.",
                  "세 값을 넣고 저장하면 자동으로 켜지고, 비우면 꺼져요."]:
            L(card.body, t, FS, C["text_2"]).pack(anchor="w")

    def _on_searchad_verify(self):
        """입력된 3값 그대로 1건 실측 조회 — 저장 없이 즉시 결과 표시(백그라운드)."""
        try:
            api = self.vars["searchad_api_key"].get().strip()
            sec = self.vars["searchad_secret_key"].get().strip()
            cust = self.vars["searchad_customer_id"].get().strip()
        except (KeyError, tk.TclError):
            return
        if not (api and sec and cust):
            self._set_searchad_status("액세스 라이선스·비밀키·고객 ID를 모두 입력하세요.", "warn")
            return
        self._set_searchad_status("확인 중…", "info")
        self.spawn(self._check_searchad, api, sec, cust, name="searchad-verify")

    def _check_searchad(self, api: str, sec: str, cust: str) -> None:
        try:
            from core.searchad_volume import verify_with
            r = verify_with(api, sec, cust)
        except (ImportError, RuntimeError, OSError) as e:  # noqa: BLE001
            log.error("검색광고 연결 테스트 실패: %s", e, exc_info=True)
            r = {"ok": False, "reason": "검증 실행에 실패했어요."}
        kind = "ok" if r.get("ok") else "error"
        self.ui_after(self._set_searchad_status, r.get("reason", ""), kind)

    def _set_searchad_status(self, text: str, kind: str) -> None:
        """검색광고 상태 알약 갱신. 파괴 후(TclError)는 조용히 무시한다."""
        try:
            self._searchad_status_pill.set(text, kind)
        except tk.TclError:
            pass

    # ── 보조금24 API (마감 임박 감지 · 선택) ────────────────
    def _gov24_section(self, parent):
        """공공데이터포털 보조금24 인증키 입력 + 저장 전 연결 테스트.

        정부 지원사업 '신청기한'을 매일 수집해 마감 임박 글감을 발행 큐 최상단으로
        올린다. 키는 공개 데이터 조회 전용이라 평문(검색광고 customer_id 와 동급).
        비면 휴면 — 복지로 키리스 수집은 항상 동작.
        """
        card = self._card(parent, "보조금24 API", "마감 임박 감지 · 선택",
                          help_text=glossary.help("gov24"))
        self._text_field(card.body, "gov24_api_key", "일반 인증키", width=44)

        btn_row = tk.Frame(card.body, bg=C["card"])
        btn_row.pack(fill="x", pady=(SP["sm"], 4))
        SecondaryBtn(btn_row, "연결 테스트", self._on_gov24_verify,
                    icon="search").pack(
            side="left", padx=(0, 8))
        self._gov24_status_pill = StatusPill(btn_row, "저장 전에 눌러서 확인해요",
                                             kind="neutral")
        self._gov24_status_pill.pack(side="left")

        for t in ["정부 지원사업 '신청기한'을 매일 수집해 마감 임박 글을 발행 큐 최상단으로 올려요(선택).",
                  "발급: data.go.kr 가입 → '보조금24 서비스 목록' 검색 → 활용신청(무료·자동승인) → 일반 인증키 복사.",
                  "키를 넣고 저장하면 자동으로 켜지고, 비우면 꺼져요.",
                  "키가 없어도 복지로 수집은 항상 동작해요 — 이 키는 신청기한 정밀도를 올리는 선택 기능이에요."]:
            L(card.body, t, FS, C["text_2"]).pack(anchor="w")

    def _on_gov24_verify(self):
        """입력된 키 그대로 1건 조회 — 저장 없이 즉시 결과 표시(백그라운드)."""
        try:
            key = self.vars["gov24_api_key"].get().strip()
        except (KeyError, tk.TclError):
            return
        if not key:
            self._set_gov24_status("인증키를 입력하세요.", "warn")
            return
        self._set_gov24_status("확인 중…", "info")
        self.spawn(self._check_gov24, key, name="gov24-verify")

    def _check_gov24(self, key: str) -> None:
        try:
            from core.deadline_tracker import verify_gov24_with
            r = verify_gov24_with(key)
        except (ImportError, RuntimeError, OSError) as e:  # noqa: BLE001
            log.error("보조금24 연결 테스트 실패: %s", e, exc_info=True)
            r = {"ok": False, "reason": "검증 실행에 실패했어요."}
        kind = "ok" if r.get("ok") else "error"
        self.ui_after(self._set_gov24_status, r.get("reason", ""), kind)

    def _set_gov24_status(self, text: str, kind: str) -> None:
        """보조금24 상태 알약 갱신. 파괴 후(TclError)는 조용히 무시한다."""
        try:
            self._gov24_status_pill.set(text, kind)
        except tk.TclError:
            pass

    # ── 이미지 채움(엔진 중립, 2026-09-09 Codex 전환) ────────────────────────
    def _image_fill_section(self, parent):
        """보관본에 썸네일·정보 카드 이미지를 자동으로 채우는 카드(엔진 선택 가능).

        기능 자체(``core/draft_image_fill.py``)는 이미 완성돼 있었지만 이 화면이
        없어 사용자가 켤 방법이 없었다(홈 탭 '이미지 채움' 버튼이 꺼져 있을 때
        "설정에서 켠 뒤 사용하세요" 라고 안내하던 그 설정). 기본 엔진은
        Codex CLI(``core/codex_image.py`` — 구독 로그인만 있으면 바로 됨)이고,
        젠스파크(브라우저 자동화, ``core/genspark_client.py``)는 레거시 opt-in
        으로 남긴다. 두 코어 모듈 다 이 파일에 새 생성 로직을 추가하지 않는다
        (파일 경계 — 읽기 전용, 조합 로직만 이 파일에 둔다).
        """
        card = self._card(parent, "이미지 채움", "보관본에 이미지를 자동으로 채워요 · 선택",
                          icon=icons.glyph("image"), help_text=glossary.help("image_fill"))

        toggle_hint = ("켜면 발행 실행 중에도 보관본에 이미지를 자동으로 채워요"
                       "(편당 생성 시간·선택한 엔진의 로그인·사용 한도를 써요). "
                       "수동으로만 쓰고 싶으면 꺼 두세요.")
        self._toggle_field(card.body, "image_fill_enabled", "이미지 자동 채움",
                           hint=toggle_hint)

        Sep(card.body).pack(fill="x", pady=(SP["sm"], SP["xs"]))
        self._image_fill_engine_row(card.body)

        self._num_field(card.body, "image_fill_cards", "편당 본문 이미지 수", 0, 6,
                        hint="한 편에 자동으로 채울 본문 이미지 수예요(썸네일은 별도예요). "
                             "본문 이미지에는 글자를 넣지 않고 내용과 어울리는 장면만 담아요.")
        self._num_field(card.body, "image_fill_parallel", "동시에 만들 이미지 수", 1, 4,
                        hint="한 편 안에서 한꺼번에 만들 이미지 수예요. 2면 두 장을 함께 "
                             "만들어 더 빨라요. 1로 두면 한 장씩 차례로 만들어요.")

        Sep(card.body).pack(fill="x", pady=(SP["sm"], SP["xs"]))
        self._codex_test_section(card.body)

        for t in ["기본 엔진은 Codex — 구독 로그인만 돼 있으면 별도 브라우저 없이 바로 채워요.",
                  "발행 실행 중에도 함께 돌아가요 — 지금 당장은 끄고 싶다면 위 토글만 꺼 두세요."]:
            L(card.body, t, FS, C["text_2"]).pack(anchor="w")

        Sep(card.body).pack(fill="x", pady=(SP["md"], SP["xs"]))
        L(card.body, "젠스파크(레거시 · 브라우저 자동화)", FT, C["text"]).pack(
            anchor="w", pady=(0, SP["xs"]))
        L(card.body, "위 '이미지 엔진'을 '젠스파크'로 선택했을 때만 아래 설정이 쓰여요.",
          FS, C["text_2"]).pack(anchor="w", pady=(0, SP["sm"]))

        legacy_hint = ("레거시 브라우저 엔진을 허용할지 결정해요 — 엔진 선택이 "
                       "'젠스파크'일 때만 실제로 쓰여요. 평소엔 꺼 둬도 괜찮아요"
                       "(기본 엔진은 Codex예요).")
        self._toggle_field(card.body, "genspark_enabled", "젠스파크 사용 허용(레거시)",
                           hint=legacy_hint)

        Sep(card.body).pack(fill="x", pady=(SP["sm"], SP["xs"]))
        self._genspark_account_section(card.body)

        Sep(card.body).pack(fill="x", pady=(SP["sm"], SP["xs"]))
        self._genspark_test_section(card.body)

        L(card.body, "실제 젠스파크 계정으로 열어 둔 크롬 창에서 1회 로그인해 둬야 동작해요.",
          FS, C["text_2"]).pack(anchor="w", pady=(SP["xs"], 0))

    def _image_fill_engine_row(self, parent) -> None:
        """엔진 선택 — 사용자에게는 한글 라벨을, ``self.vars["image_fill_engine"]``
        에는 실제 값("codex"/"genspark")을 저장한다. 엑셀 도메인 프리셋 콤보
        (``_excel_preset_var``/``_on_excel_preset_selected``)와 같은 라벨↔값
        분리 idiom — ttk.Combobox 는 textvariable 을 그대로 화면에 찍으므로,
        저장용 var 와 표시용 var 를 분리해 둔다."""
        current = str(self.app.cfg.get("image_fill_engine", "codex") or "codex").strip().lower()
        if current not in IMAGE_FILL_ENGINE_LABELS:
            current = "codex"
        self.vars["image_fill_engine"] = tk.StringVar(value=current)

        row = tk.Frame(parent, bg=C["card"])
        row.pack(fill="x", pady=px(3))
        self._label_col(row, "이미지 엔진")
        self._image_fill_engine_display_var = tk.StringVar(
            value=IMAGE_FILL_ENGINE_LABELS[current])
        self._image_fill_engine_combo = ttk.Combobox(
            row, textvariable=self._image_fill_engine_display_var,
            values=list(IMAGE_FILL_ENGINE_LABELS.values()),
            state="readonly", font=FB, width=22)
        self._image_fill_engine_combo.pack(side="left", padx=(SP["sm"], 0), ipady=4)
        self._image_fill_engine_combo.bind(
            "<<ComboboxSelected>>", self._on_image_fill_engine_selected)

        hint = ("이미지를 생성할 엔진이에요. Codex 는 구독 로그인만 있으면 바로 되고, "
               "젠스파크는 브라우저 로그인이 필요해요(레거시).")
        self._register_search(self._image_fill_engine_combo, "이미지 엔진", hint,
                              ("image_fill_engine",))
        L(parent, hint, FS, C["text_2"]).pack(anchor="w", pady=(4, 0))

    def _on_image_fill_engine_selected(self, event=None) -> None:
        """엔진 콤보 선택 시 표시 라벨을 실제 값으로 변환해 ``self.vars`` 에 반영."""
        label = self._image_fill_engine_display_var.get()
        self.vars["image_fill_engine"].set(
            IMAGE_FILL_ENGINE_LABEL_TO_VALUE.get(label, "codex"))

    # ── Codex 연결 테스트(core/codex_image.py 소비) ──────────────────────────
    _CODEX_STATE_KIND = {"ok": "ok", "login_required": "warn", "config_broken": "warn"}

    def _codex_test_section(self, parent) -> None:
        """codex 로그인 상태 확인 버튼 — 실제 확인 로직은 백그라운드 전용
        ``_probe_codex_connection()`` 에 격리한다(테스트가 이 함수 하나만
        monkeypatch 하면 실 codex CLI 를 전혀 타지 않는다). 젠스파크 연결
        테스트(``_genspark_test_section``)와 같은 disable→재활성 관례."""
        row = tk.Frame(parent, bg=C["card"])
        row.pack(fill="x", pady=(0, SP["xs"]))
        self._codex_test_btn = SecondaryBtn(
            row, "연결 테스트", self._on_codex_test, icon="search")
        self._codex_test_btn.pack(side="left", padx=(0, SP["sm"]))
        self._codex_test_pill = StatusPill(
            row, "눌러서 Codex 로그인 상태를 확인해요", kind="neutral")
        self._codex_test_pill.pack(side="left")
        self._register_search(
            row, "Codex 연결 테스트",
            "Codex CLI 로그인 상태를 확인해요.", ("codex_test",))

    def _on_codex_test(self) -> None:
        try:
            self._codex_test_btn.config(state="disabled")
        except tk.TclError:
            pass
        self._set_codex_test_status("확인 중… (몇 초 걸릴 수 있어요)", "info")
        self.spawn(self._check_codex_connection, name="codex-verify")

    def _check_codex_connection(self) -> None:
        result = _probe_codex_connection()
        kind = self._CODEX_STATE_KIND.get(result.get("reason"),
                                          "ok" if result.get("ok") else "error")
        self.ui_after(self._finish_codex_test, result.get("detail", ""), kind)

    def _finish_codex_test(self, text: str, kind: str) -> None:
        try:
            self._codex_test_btn.config(state="normal")
        except tk.TclError:
            pass
        self._set_codex_test_status(text, kind)

    def _set_codex_test_status(self, text: str, kind: str) -> None:
        try:
            self._codex_test_pill.set(text, kind)
        except tk.TclError:
            pass

    def _genspark_account_section(self, parent) -> None:
        """활성 계정 표시·전환 — ``core.genspark_client.list_accounts``/``set_active``."""
        row = tk.Frame(parent, bg=C["card"])
        row.pack(fill="x", pady=px(3))
        self._label_col(row, "활성 계정")
        self._genspark_account_var = tk.StringVar(value="불러오는 중…")
        self._genspark_account_combo = ttk.Combobox(
            row, textvariable=self._genspark_account_var, values=[],
            state="disabled", font=FB, width=26)
        self._genspark_account_combo.pack(side="left", padx=(SP["sm"], 0), ipady=4)
        self._register_search(
            self._genspark_account_combo, "젠스파크 활성 계정",
            "이미지를 생성할 젠스파크 계정을 전환해요.", ("genspark_account",))

        self._genspark_empty = EmptyState(
            parent, icons.glyph("user"), "등록된 계정이 없어요",
            "CLI 로 계정을 등록해요: python -m core.genspark_client --add-account <이메일>")

        btn_row = tk.Frame(parent, bg=C["card"])
        btn_row.pack(fill="x", pady=(SP["xs"], 0))
        self._genspark_apply_btn = SecondaryBtn(
            btn_row, "계정 적용", self._on_genspark_account_apply, icon="refresh")
        self._genspark_apply_btn.pack(side="left", padx=(0, SP["sm"]))
        self._genspark_apply_btn.config(state="disabled")
        self._genspark_account_status = StatusPill(btn_row, "", kind="neutral")
        self._genspark_account_status.pack(side="left")

        # Claude 연결 섹션(_claude_section)과 같은 지연 기동 관례 — 화면 구성 도중
        # 곧바로 스레드를 띄우지 않고 400ms 뒤로 미룬다(대부분의 빠른 단위
        # 테스트는 이 지연이 만료되기 전에 끝나 실 파일 I/O 스레드를 아예
        # 타지 않는다 — destroy() 가 지연 예약을 취소한다).
        self.ui_after(self._kick_off_genspark_account_load, delay=400)

    def _kick_off_genspark_account_load(self) -> None:
        self.spawn(self._load_genspark_accounts, name="genspark-accounts")

    def _load_genspark_accounts(self) -> None:
        """등록 계정 목록 조회(파일 I/O) — 가벼워도 백그라운드에서 돈다."""
        try:
            from core.genspark_client import list_accounts
            data = list_accounts()
        except (ImportError, OSError, ValueError) as e:  # noqa: BLE001
            log.error("젠스파크 계정 목록을 불러오지 못했어요: %s", e, exc_info=True)
            data = {"active": "", "accounts": {}}
        self.ui_after(self._apply_genspark_accounts, data)

    def _apply_genspark_accounts(self, data: dict) -> None:
        """조회된 계정 목록을 콤보에 반영한다(메인 스레드). 파괴 후는 조용히 무시."""
        accounts = (data or {}).get("accounts") or {}
        active = (data or {}).get("active") or ""
        self._genspark_accounts_cache = data or {}
        emails = sorted(accounts.keys())
        try:
            if emails:
                self._genspark_empty.hide()
                self._genspark_account_combo.config(values=emails, state="readonly")
                self._genspark_account_var.set(active if active in emails else emails[0])
                self._genspark_apply_btn.config(state="normal")
            else:
                self._genspark_account_combo.config(values=[], state="disabled")
                self._genspark_account_var.set("(등록된 계정 없음)")
                self._genspark_apply_btn.config(state="disabled")
                self._genspark_empty.show()
        except tk.TclError:
            pass

    def _on_genspark_account_apply(self) -> None:
        """콤보에서 고른 계정으로 즉시 전환한다(저장 버튼과 무관한 라이브 동작 —
        Claude 연결·화면 테마와 같은 부류)."""
        try:
            email = self._genspark_account_var.get().strip()
        except tk.TclError:
            return
        accounts = (self._genspark_accounts_cache or {}).get("accounts") or {}
        if not email or email not in accounts:
            self._set_genspark_account_status("등록된 계정을 선택하세요.", "warn")
            return
        self._set_genspark_account_status("전환 중…", "info")
        # 연타 방지 — 동시에 여러 워커가 set_active 를 read-modify-write 하면
        # "마지막 쓰기 승리" 레이스가 된다(2026-09-03 리뷰 🔵). 연결 테스트
        # 버튼과 같은 disable→재활성 패턴.
        self._set_genspark_apply_enabled(False)
        self.spawn(self._switch_genspark_account, email, name="genspark-switch")

    def _switch_genspark_account(self, email: str) -> None:
        try:
            from core.genspark_client import set_active
            set_active(email)
        except (ImportError, ValueError, OSError) as e:  # noqa: BLE001
            log.error("젠스파크 활성 계정 전환 실패: %s", e, exc_info=True)
            self.ui_after(self._set_genspark_account_status, "전환에 실패했어요.", "error")
            self.ui_after(self._set_genspark_apply_enabled, True)
            return
        self.ui_after(lambda: self._set_genspark_account_status(f"활성 계정: {email}", "ok"))
        self.ui_after(self._set_genspark_apply_enabled, True)

    def _set_genspark_apply_enabled(self, enabled: bool) -> None:
        """'계정 적용' 버튼 활성/비활성(메인 스레드 전용). 파괴 후에도 안전."""
        try:
            self._genspark_apply_btn.config(
                state="normal" if enabled else "disabled")
        except (tk.TclError, AttributeError):
            pass

    def _set_genspark_account_status(self, text: str, kind: str) -> None:
        try:
            self._genspark_account_status.set(text, kind)
        except tk.TclError:
            pass

    def _genspark_test_section(self, parent) -> None:
        """로그인·사용 한도 상태 확인 버튼 — 실제 확인 로직은 백그라운드 전용
        ``_probe_genspark_connection()`` 에 격리한다(테스트가 이 함수 하나만
        monkeypatch 하면 실 브라우저 자동화를 전혀 타지 않는다)."""
        row = tk.Frame(parent, bg=C["card"])
        row.pack(fill="x", pady=(0, SP["xs"]))
        self._genspark_test_btn = SecondaryBtn(
            row, "연결 테스트", self._on_genspark_test, icon="search")
        self._genspark_test_btn.pack(side="left", padx=(0, SP["sm"]))
        self._genspark_test_pill = StatusPill(
            row, "눌러서 로그인·사용 한도를 확인해요", kind="neutral")
        self._genspark_test_pill.pack(side="left")
        self._register_search(
            row, "젠스파크 연결 테스트",
            "활성 계정의 로그인·사용 한도 상태를 확인해요.", ("genspark_test",))

    def _on_genspark_test(self) -> None:
        try:
            self._genspark_test_btn.config(state="disabled")
        except tk.TclError:
            pass
        self._set_genspark_test_status("확인 중… (크롬 창이 열릴 수 있어요)", "info")
        self.spawn(self._check_genspark_connection, name="genspark-verify")

    _GENSPARK_STATE_KIND = {"ok": "ok", "limited": "warn", "login_required": "warn"}

    def _check_genspark_connection(self) -> None:
        result = _probe_genspark_connection()
        kind = self._GENSPARK_STATE_KIND.get(result.get("state"), "error")
        self.ui_after(self._finish_genspark_test, result.get("reason", ""), kind)

    def _finish_genspark_test(self, text: str, kind: str) -> None:
        try:
            self._genspark_test_btn.config(state="normal")
        except tk.TclError:
            pass
        self._set_genspark_test_status(text, kind)

    def _set_genspark_test_status(self, text: str, kind: str) -> None:
        try:
            self._genspark_test_pill.set(text, kind)
        except tk.TclError:
            pass

    # ══════════════════════════════════════════════════════
    # 세그먼트 ⑤ 자동화·진단
    # ══════════════════════════════════════════════════════
    def _build_ops_segment(self, parent):
        self._schedule_section(parent)
        self._theme_section(parent)
        self._tour_section(parent)
        self._diagnostic_tools_section(parent)

    # ── 처음 안내(코치마크 투어) 다시 보기(9호 Phase 3) ─────────────
    def _tour_section(self, parent) -> None:
        """홈 화면 등을 짚어 주던 첫 실행 안내를 다시 띄운다. ``app.start_tour``
        (없으면 ``ui_prefs.tour_done`` 만 초기화 — 다음에 켤 때 자동으로 뜬다)."""
        card = self._card(parent, "처음 안내", "핵심 화면을 다시 짚어 드려요",
                          icon=icons.glyph("help"))
        SecondaryBtn(card.body, "처음 안내 다시 보기", self._on_tour_replay,
                    icon="help").pack(anchor="w")
        self._register_search(card.body, "처음 안내",
                              "핵심 화면을 다시 짚어 드려요.", ("tour",))

    def _on_tour_replay(self) -> None:
        if ui_prefs is not None:
            try:
                ui_prefs.set("tour_done", False)
            except (OSError, RuntimeError) as e:
                log.error("처음 안내 재설정 실패: %s", e, exc_info=True)
        start_tour = getattr(self.app, "start_tour", None)
        if callable(start_tour):
            try:
                start_tour()
                return
            except Exception as e:  # noqa: BLE001 — 투어 시작 실패가 저장 자체를 막지 않는다
                log.error("처음 안내 다시 보기 실패: %s", e, exc_info=True)
        self._notify("다음에 켤 때 처음 안내가 다시 나와요", kind="info")

    # ── 화면 테마(잔여 P2) + 사이드바 개인화 ────────────────────
    def _theme_section(self, parent):
        """다크/라이트 테마 선택 + 사이드바 자주 쓰는 탭 자동 정렬.

        테마는 ``app.set_theme``(코더 B, 라이브 전환)가 있으면 선택 즉시 그쪽에
        위임하고(토스트는 App 이 띄운다 — 여기서 중복 알림 없음), 없으면 기존
        ``gui.ui_prefs`` 저장 + 재시작 안내 경로를 그대로 쓴다. 개인화 체크는
        ``config_io``·``self.vars`` 와 무관 — 38키 계약 불변. ``ui_prefs`` 가
        아직 없으면(코더 A 작업 완료 전) 카드를 비활성 안내로만 보여준다(예외
        없이 폴백).
        """
        hint_text = self._theme_hint_text()
        card = self._card(parent, "화면 테마", hint_text, icon=icons.glyph("theme"))
        if ui_prefs is None:
            L(card.body, "이 기능은 아직 사용할 수 없어요(업데이트 후 이용 가능해요).",
              FS, C["text_2"]).pack(anchor="w")
            return

        try:
            current = ui_prefs.get("theme", "dark")
        except (OSError, RuntimeError) as e:
            log.error("테마 설정 읽기 실패: %s", e, exc_info=True)
            current = "dark"
        self._theme_var = tk.StringVar(value=current)

        row = tk.Frame(card.body, bg=C["card"])
        row.pack(fill="x", pady=px(3))
        for value, label in (("dark", "어두운"), ("light", "밝은")):
            tk.Radiobutton(
                row, text=label, value=value, variable=self._theme_var,
                command=self._on_theme_changed, font=FB, fg=C["text"],
                bg=C["card"], activebackground=C["card"], activeforeground=C["text"],
                selectcolor=C["surface"], relief="flat",
                cursor="hand2").pack(side="left", padx=(0, SP["md"]))
        self._register_search(row, "화면 테마", "어두운 또는 밝은 화면으로 바꿔요.",
                              ("theme",))
        L(card.body, hint_text, FS, C["text_2"]).pack(anchor="w", pady=(4, 0))

        Sep(card.body).pack(fill="x", pady=(SP["sm"], SP["xs"]))
        self._sidebar_personalize_section(card.body)

    def _theme_hint_text(self) -> str:
        """app.set_theme(라이브 전환)가 있으면 '바로', 없으면 '다시 켜면'."""
        return ("바로 적용돼요" if callable(getattr(self.app, "set_theme", None))
                else "다시 켜면 적용돼요")

    def _on_theme_changed(self) -> None:
        if ui_prefs is None:
            return
        try:
            value = self._theme_var.get()
        except tk.TclError:
            return

        set_theme = getattr(self.app, "set_theme", None)
        if callable(set_theme):
            # 라이브 전환 경로 — App 이 적용·저장·토스트까지 담당한다.
            # ui_prefs.set 을 여기서 또 부르면 같은 값을 두 번 쓰게 된다.
            try:
                set_theme(value)
            except (OSError, RuntimeError) as e:
                log.error("테마 실시간 적용 실패: %s", e, exc_info=True)
                self._notify("테마를 적용하지 못했어요.", kind="error")
            return

        try:
            ui_prefs.set("theme", value)
        except (OSError, RuntimeError) as e:
            log.error("테마 설정 저장 실패: %s", e, exc_info=True)
            self._notify("테마 설정을 저장하지 못했어요.", kind="error")
            return
        label = "밝은" if value == "light" else "어두운"
        self._notify(f"다음에 켤 때 {label} 테마로 열려요", kind="info")

    # ── 사이드바 개인화(자주 쓰는 탭 자동 정렬) ─────────────────
    def _sidebar_personalize_section(self, parent) -> None:
        """ui_prefs 키 sidebar_personalize(기본 True) — 화면 테마 카드에 이어붙인다.

        ui_prefs 가 없으면 이 메서드는 호출되지 않는다(_theme_section 이 카드
        전체를 비활성 안내로 대체하고 조기 반환하므로).
        """
        try:
            current = bool(ui_prefs.get("sidebar_personalize", True))
        except (OSError, RuntimeError) as e:
            log.error("사이드바 개인화 설정 읽기 실패: %s", e, exc_info=True)
            current = True
        self._sidebar_personalize_var = tk.BooleanVar(value=current)

        label_text = "사이드바 자주 쓰는 탭을 내가 많이 쓰는 순서로 정렬"
        hint = "다음에 켤 때 반영돼요 · 총 20번 이상 열어야 시작돼요."
        ff = FormField(parent, label_text, self._sidebar_personalize_var,
                       kind="check", hint=hint)
        ff.pack(fill="x", pady=px(2))
        self._register_search(ff, label_text, hint, ("sidebar_personalize",))
        self._sidebar_personalize_var.trace_add(
            "write", self._on_sidebar_personalize_changed)

    def _on_sidebar_personalize_changed(self, *_args) -> None:
        if ui_prefs is None:
            return
        try:
            value = self._sidebar_personalize_var.get()
        except tk.TclError:
            return
        try:
            ui_prefs.set("sidebar_personalize", value)
        except (OSError, RuntimeError) as e:
            log.error("사이드바 개인화 설정 저장 실패: %s", e, exc_info=True)
            self._notify("설정을 저장하지 못했어요.", kind="error")

    # ── Windows 자동 스케줄 ─────────────────────────────────
    def _schedule_section(self, parent):
        """Windows 작업 스케줄러 등록/해제 UI를 구성합니다."""
        card = self._card(parent, "자동 실행 스케줄", "매일 정해진 시간에 저절로 실행해요",
                          icon=icons.glyph("clock"))
        row = tk.Frame(card.body, bg=C["card"])
        row.pack(fill="x", pady=px(3))
        self._label_col(row, "현재 상태")
        self._sched_status_pill = StatusPill(row, "확인 중…", kind="info")
        self._sched_status_pill.pack(side="left", padx=(SP["sm"], 0))

        btn_row = tk.Frame(card.body, bg=C["card"])
        btn_row.pack(fill="x", pady=(SP["sm"], 4))
        SecondaryBtn(btn_row, "스케줄 등록", self._on_schedule_register).pack(
            side="left", padx=(0, 8))
        DangerBtn(btn_row, "스케줄 해제", self._on_schedule_unregister).pack(side="left")

        L(card.body, "설정한 시간에 매일 자동으로 실행돼요.",
          FS, C["text_2"]).pack(anchor="w", pady=(4, 0))

        self.ui_after(self._refresh_schedule_status, delay=500)

    def _refresh_schedule_status(self):
        """작업 스케줄러 등록 상태를 확인하여 상태 알약에 반영합니다."""
        self.spawn(self._check_schedule_status, name="schedule-status")

    def _check_schedule_status(self) -> None:
        try:
            from scheduler.task_register import check_task
            registered, msg = check_task()
            kind = "ok" if registered else "neutral"
            text = msg
        except (ImportError, OSError, RuntimeError) as e:
            log.error("스케줄 상태 확인 실패: %s", e, exc_info=True)
            kind, text = "error", "상태를 확인하지 못했어요."
        self.ui_after(self._set_schedule_status, text, kind)

    def _set_schedule_status(self, text: str, kind: str) -> None:
        try:
            self._sched_status_pill.set(text, kind)
        except tk.TclError:
            pass

    def _on_schedule_register(self):
        self.spawn(self._register_schedule, name="schedule-register")

    def _register_schedule(self) -> None:
        try:
            from scheduler.task_register import register_task
            ok, msg = register_task()
        except (ImportError, OSError, RuntimeError) as e:
            log.error("스케줄 등록 실패: %s", e, exc_info=True)
            ok, msg = False, "등록에 실패했어요."
        self.ui_after(self._after_schedule_register, ok, msg)

    def _after_schedule_register(self, ok: bool, msg: str) -> None:
        if ok:
            self._set_schedule_status(msg, "ok")
            self._notify(msg, kind="ok")
        else:
            self._set_schedule_status("등록 실패", "error")
            messagebox.showerror("스케줄 등록 실패", msg)
        self._refresh_schedule_status()

    def _on_schedule_unregister(self):
        self.spawn(self._unregister_schedule, name="schedule-unregister")

    def _unregister_schedule(self) -> None:
        try:
            from scheduler.task_register import unregister_task
            ok, msg = unregister_task()
        except (ImportError, OSError, RuntimeError) as e:
            log.error("스케줄 해제 실패: %s", e, exc_info=True)
            ok, msg = False, "해제에 실패했어요."
        self.ui_after(self._after_schedule_unregister, ok, msg)

    def _after_schedule_unregister(self, ok: bool, msg: str) -> None:
        if ok:
            self._set_schedule_status("미등록", "neutral")
            self._notify(msg, kind="ok")
        else:
            messagebox.showerror("스케줄 해제 실패", msg)
        self._refresh_schedule_status()

    # ── 진단 도구 ──────────────────────────────────────────
    def _diagnostic_tools_section(self, parent):
        """하네스 헬퍼 + 보관본 정리 + 확장 도구를 아이콘 버튼 2열 그리드로
        모은다(설계서 §7-4 — 8버튼, 이전의 3단 서브그룹+설명 텍스트 나열 대신
        SecondaryBtn(icon=) + 호버 툴팁으로 설명을 옮겼다)."""
        card = self._card(parent, "진단 도구", icon=icons.glyph("help"))

        tools = [
            ("로그 요약", "최근 실행 로그 요약을 새 창에 보여줘요.",
             self._run_inspect_logs, "folder"),
            ("발행 완료 보관본 정리", "이미 발행된 보관본을 정리해요.",
             self._run_purge_uploaded, "delete"),
            ("원격 지원 리포트",
             "문제 해결용 진단 리포트를 만들어요(비밀번호 등은 들어있지 않아요).",
             self._run_support_report, "help"),
            ("카테고리 매핑 추출", "블로그 메뉴 목록을 자동으로 읽어와요.",
             self._run_category_extract, "search"),
            ("멀티채널 팩 생성", "이 글로 쇼츠 영상과 자막을 만들어요.",
             self._run_multichannel_build, "publish"),
            ("발행 글 영상 일괄생성", "영상 없는 최근 발행 글을 한 번에 만들어요.",
             self._run_multichannel_batch, "publish"),
            ("엑셀 마이그레이션", "키워드 엑셀 형식을 최신 버전으로 정리해요.",
             self._run_excel_migrate, "folder"),
            ("슬러그 마이그레이션", "보관본 파일 이름과 제목이 어긋난 걸 고쳐요.",
             self._run_slug_migrate, "refresh"),
        ]

        grid = tk.Frame(card.body, bg=C["card"])
        grid.pack(fill="x")
        grid.grid_columnconfigure(0, weight=1, uniform="diag_tool")
        grid.grid_columnconfigure(1, weight=1, uniform="diag_tool")
        for i, (label, desc, callback, icon_name) in enumerate(tools):
            btn = SecondaryBtn(grid, label, callback, icon=icon_name)
            btn.grid(row=i // 2, column=i % 2, sticky="ew",
                    padx=(0, SP["sm"]) if i % 2 == 0 else (SP["sm"], 0),
                    pady=(0, SP["sm"]))
            Tooltip(btn, desc)

        Sep(card.body).pack(fill="x", pady=(SP["sm"], SP["xs"]))
        self._spawn_status_label = L(card.body, "별도 콘솔 도구 상태: 대기 중", FS,
                                     C["text_2"])
        self._spawn_status_label.pack(anchor="w", pady=(2, 0))

    def _spawn_helper(self, args: list, cwd: Path):
        """하네스 헬퍼 스크립트를 별도 콘솔로 실행합니다.

        자식 프로세스에 UTF-8 강제 환경변수를 주입해 한글 print 시 cp949
        콘솔에서 UnicodeEncodeError 가 나지 않게 한다.

        Returns:
            성공 시 subprocess.Popen 객체, 실패 시 None. 호출부가 산출 폴더
            mtime 폴링 등으로 완료를 감지하고 싶을 때 이 핸들을 쓸 수 있다.
        """
        import os
        import subprocess
        import sys

        child_env = os.environ.copy()
        child_env["PYTHONUTF8"] = "1"
        child_env["PYTHONIOENCODING"] = "utf-8"

        try:
            creationflags = (subprocess.CREATE_NEW_CONSOLE
                             if sys.platform == "win32" else 0)
            return subprocess.Popen(
                [sys.executable] + args,
                cwd=str(cwd),
                env=child_env,
                creationflags=creationflags)
        except OSError as exc:
            log.error("헬퍼 실행 실패: %s", exc, exc_info=True)
            messagebox.showerror("실행 실패", "도구 실행에 실패했어요.")
            return None

    def _poll_helper_output(
            self, proc, out_path: Path, baseline_mtime: float, label: str, *,
            attempts: int = 600, interval_ms: int = 1000):
        """헬퍼 프로세스 완료를 산출 경로 mtime 변화로 감지해 상태를 표기한다.

        폴링은 메인스레드 `ui_after` 재예약으로만 돌려 Tkinter thread-safety 를
        지킨다(스레드 미사용). 프로세스가 끝났거나 산출물이 새로 생기면 완료로
        간주하고, attempts 를 소진하면 조용히 멈춘다.
        """
        if proc is None:
            return
        self._set_spawn_status(label, finished=False, produced=False)

        def _current_mtime() -> float:
            try:
                return out_path.stat().st_mtime
            except OSError:
                return 0.0

        def _tick(remaining: int) -> None:
            finished = proc.poll() is not None
            produced = _current_mtime() > baseline_mtime
            if finished or produced or remaining <= 0:
                self._set_spawn_status(label, finished, produced)
                return
            self.ui_after(_tick, remaining - 1, delay=interval_ms)

        self.ui_after(_tick, attempts)

    def _set_spawn_status(self, label: str, finished: bool, produced: bool) -> None:
        """폴링 결과를 상태 라벨(있으면)에 반영. 파괴 후 TclError 는 삼킨다."""
        if produced:
            text = f"{label}: 산출물 생성 확인 — 완료"
        elif finished:
            text = f"{label}: 콘솔이 닫혔어요 — 콘솔의 완료 메시지를 확인하세요"
        else:
            text = f"{label}: 진행 중"
        status = getattr(self, "_spawn_status_label", None)
        if status is None:
            return
        try:
            status.config(text=text)
        except tk.TclError:
            pass

    def _harness_root(self) -> Path:
        """naver_blog_automation_v3/ 디렉토리 경로."""
        return Path(__file__).resolve().parents[3]

    def _run_inspect_logs(self):
        root = self._harness_root()
        helper = root / "_lib" / "inspect_logs.py"
        if not helper.exists():
            log.error("헬퍼 스크립트 없음: %s", helper)
            messagebox.showerror("진단 도구", "필요한 도구를 찾지 못했어요. 프로그램을 다시 설치해 보세요.")
            return
        self._spawn_helper([str(helper)], cwd=root)

    def _run_excel_migrate(self):
        root = self._harness_root()
        helper = root / "_lib" / "excel_migrate.py"
        if not helper.exists():
            log.error("헬퍼 스크립트 없음: %s", helper)
            messagebox.showerror("진단 도구", "필요한 도구를 찾지 못했어요. 프로그램을 다시 설치해 보세요.")
            return
        excel_path = filedialog.askopenfilename(
            title="마이그레이션할 엑셀 파일 선택",
            filetypes=[("Excel", "*.xlsx *.xls")])
        if not excel_path:
            return
        self._spawn_helper([str(helper), "--excel", excel_path], cwd=root)

    def _run_slug_migrate(self):
        root = self._harness_root()
        helper = root / "_lib" / "migrate_status_title.py"
        if not helper.exists():
            log.error("헬퍼 스크립트 없음: %s", helper)
            messagebox.showerror("진단 도구", "필요한 도구를 찾지 못했어요. 프로그램을 다시 설치해 보세요.")
            return
        if not messagebox.askyesno(
                "보관본 정리",
                "보관본 파일 이름과 제목을 일괄로 맞춰요.\n"
                "기존 보관본 폴더가 바뀔 수 있어요.\n\n"
                "계속할까요?"):
            return
        self._spawn_helper([str(helper)], cwd=root)

    def _run_purge_uploaded(self):
        if not messagebox.askyesno(
                "발행 완료 보관본 정리",
                "이미 발행된 보관본을 즉시 삭제해요.\n"
                "이 작업은 되돌릴 수 없어요.\n\n"
                "계속할까요?",
                icon="warning"):
            return
        try:
            from core.draft_store import purge_uploaded
            n = purge_uploaded(older_than_days=0, dry_run=False)
        except (ImportError, OSError) as exc:
            log.error("보관본 정리 실패: %s", exc, exc_info=True)
            messagebox.showerror("정리 실패", "보관본 정리에 실패했어요.")
            return
        self._notify(f"{n}편 정리했어요.", kind="ok")

    def _run_support_report(self):
        """비밀 값 없는 원격 지원 진단 리포트를 생성해 연다(운영자 전송용).

        support_report 는 계정을 마스킹하고 쿠키·토큰의 값을 담지 않으며,
        check_onboarding 은 네트워크를 타지 않으므로 동기 실행이 안전하다.
        """
        import os
        try:
            from core.support_report import save_support_report
            path = save_support_report()
        except (ImportError, OSError) as exc:  # noqa: BLE001 — 생성 실패는 알리고 흡수
            log.error("원격 지원 리포트 생성 실패: %s", exc, exc_info=True)
            messagebox.showerror("원격 지원 리포트", "리포트 생성에 실패했어요.")
            return
        try:
            os.startfile(str(path))  # noqa: S606 (Windows 전용 GUI)
            self._notify("리포트를 만들어 열었어요(비밀번호 등은 들어있지 않아요).", kind="ok")
        except OSError as exc:
            log.error("원격 지원 리포트 열기 실패: %s", exc, exc_info=True)
            self._notify("리포트를 만들었지만 자동으로 열지 못했어요.", kind="warn")

    @staticmethod
    def _brand_display() -> str:
        """활성 프로필의 화면 표기명. 실패 시 'examplebrand'."""
        try:
            from config import brand as _brand
            return _brand.display()
        except (ImportError, AttributeError):  # noqa: BLE001 — 브랜드 헬퍼 부재 시 레거시 표기
            return "examplebrand"

    def _run_category_extract(self):
        """활성 프로필 글쓰기 페이지의 카테고리 매핑 자동 추출 — 별도 콘솔에서 실행.

        🚨 실 발행 안 함 — 발행 패널만 열어 메뉴 목록을 읽고 종료한다.
        """
        root = self._harness_root()
        helper = root / "_lib" / "extract_category_map.py"
        if not helper.exists():
            log.error("헬퍼 스크립트 없음: %s", helper)
            messagebox.showerror("진단 도구", "필요한 도구를 찾지 못했어요. 프로그램을 다시 설치해 보세요.")
            return
        display = self._brand_display()
        if not messagebox.askyesno(
                "카테고리 매핑 추출",
                f"{display} 글쓰기 페이지에 들어가 메뉴 목록을 자동으로 읽어와요.\n\n"
                "실제 발행은 절대 일어나지 않아요.\n"
                "    · 별도 브라우저 창이 열리고 자동으로 닫혀요.\n"
                f"    · 진행 중에는 {display} 자동 발행과 충돌할 수 있으니\n"
                "      자동 발행이 진행 중이지 않은 시간대에 실행하세요.\n\n"
                "계속할까요?"):
            return
        self._spawn_helper([str(helper)], cwd=root)

    def _run_multichannel_build(self):
        """멀티채널 팩 생성 — App 서비스가 있으면 위임(설계서 §1.5), 없으면 자체 실행.

        🚨 자동 업로드 안 함 — 영상 + 자막만 폴더에 생성한다.
        """
        delegate = getattr(self.app, "run_multichannel_build", None)
        if callable(delegate):
            delegate()
            return
        self._run_multichannel_build_standalone()

    def _run_multichannel_build_standalone(self):
        """App 서비스가 없을 때의 자체 실행.

        가능하면 ``gui.multichannel_service`` 로 후보 수집·헬퍼 spawn·폴링을
        위임하고(auto-dev 5호 — 로직 단일화), 그 모듈 임포트에 실패했을 때만
        (코더 B 작업 완료 전 등) 기존 본체(``_run_multichannel_build_legacy``)로
        폴백한다.
        """
        if _MULTICHANNEL_SERVICE_AVAILABLE:
            self._run_multichannel_build_via_service()
        else:
            self._run_multichannel_build_legacy()

    def _run_multichannel_build_via_service(self):
        """gui.multichannel_service 위임 경로.

        ``start_build`` 의 콜백은 워커 스레드에서 오므로 반드시 ``ui_after``
        로 재위임한 뒤 위젯을 만진다.
        """
        root = self._harness_root()
        try:
            candidates = list(_mc_list_candidate_slugs(root))
        except (OSError, RuntimeError) as e:
            log.error("멀티채널 후보 조회 실패: %s", e, exc_info=True)
            messagebox.showerror("멀티채널 팩 생성", "처리할 글 목록을 불러오지 못했어요.")
            return

        if not candidates:
            self._notify("처리할 수 있는 글이 아직 없어요.", kind="info")
            return

        chosen = self._pick_slug_dialog(candidates)
        if not chosen:
            return

        def _on_line(text: str) -> None:
            self.ui_after(self._set_spawn_status_text, str(text))

        def _on_done(produced: bool = False, out_dir=None) -> None:
            self.ui_after(self._on_multichannel_service_done, produced, out_dir)

        try:
            _mc_start_build(chosen, root, on_line=_on_line, on_done=_on_done)
        except (OSError, RuntimeError) as e:
            log.error("멀티채널 팩 생성 시작 실패: %s", e, exc_info=True)
            messagebox.showerror("멀티채널 팩 생성", "실행에 실패했어요.")
            return

        self._notify(
            "별도 콘솔에서 영상을 만들고 있어요. 완료되면 콘솔에 안내가 떠요"
            "(자동 업로드는 하지 않아요).", kind="info")

    def _on_multichannel_service_done(self, produced: bool = False, out_dir=None) -> None:
        """start_build 의 on_done(produced, out_dir) 콜백 — ui_after 로 재위임된 뒤 호출됨."""
        text = ("멀티채널 팩: 산출물 생성 확인 — 완료" if produced
                else "멀티채널 팩: 완료를 확인하지 못했어요 — 콘솔의 안내를 확인해 주세요")
        self._set_spawn_status_text(text)

    def _set_spawn_status_text(self, text: str) -> None:
        """진단 도구 하단 상태 라벨을 그대로 갱신(서비스 콜백 전용, 파괴 후 무시)."""
        status = getattr(self, "_spawn_status_label", None)
        if status is None:
            return
        try:
            status.config(text=text)
        except tk.TclError:
            pass

    def _run_multichannel_build_legacy(self):
        """gui.multichannel_service 가 없을 때의 기존 본체(폴백, 로직 무변경)."""
        root = self._harness_root()
        helper = root / "_lib" / "build_multichannel_pack.py"
        if not helper.exists():
            log.error("헬퍼 스크립트 없음: %s", helper)
            messagebox.showerror("멀티채널 팩 생성", "필요한 도구를 찾지 못했어요.")
            return

        runs_dir = root / "runs"
        if not runs_dir.exists():
            messagebox.showerror("멀티채널 팩 생성", "아직 만든 글이 없어요.")
            return

        # 후보 슬러그 — MULTICHANNEL_CANDIDATE_FILES 중 하나라도 있으면 후보
        candidates: list[str] = []
        for child in sorted(runs_dir.iterdir(), reverse=True):
            if not child.is_dir():
                continue
            if any((child / fname).exists() for fname in MULTICHANNEL_CANDIDATE_FILES):
                candidates.append(child.name)
            if len(candidates) >= 20:
                break

        if not candidates:
            self._notify("처리할 수 있는 글이 아직 없어요.", kind="info")
            return

        chosen = self._pick_slug_dialog(candidates)
        if not chosen:
            return

        out_dir = runs_dir / chosen / "multichannel"
        if out_dir.exists() and any(out_dir.iterdir()):
            if not messagebox.askyesno(
                    "멀티채널 팩 생성",
                    "이미 만들어 둔 영상이 있어요.\n\n덮어쓸까요?"):
                return

        try:
            baseline_mtime = out_dir.stat().st_mtime
        except OSError:
            baseline_mtime = 0.0

        proc = self._spawn_helper([str(helper), "--slug", chosen], cwd=root)
        self._notify(
            "별도 콘솔에서 영상을 만들고 있어요. 완료되면 콘솔에 안내가 떠요"
            "(자동 업로드는 하지 않아요).", kind="info")
        self._poll_helper_output(proc, out_dir, baseline_mtime, "멀티채널 팩")

    def _run_multichannel_batch(self):
        """영상 없는 최근 발행 글 N편의 멀티채널 팩을 일괄 생성(멱등).

        🚨 자동 업로드 안 함 — 영상 + 자막만 폴더에 생성한다.
        """
        root = self._harness_root()
        helper = root / "_lib" / "build_multichannel_pack.py"
        if not helper.exists():
            log.error("헬퍼 스크립트 없음: %s", helper)
            messagebox.showerror("영상 일괄생성", "필요한 도구를 찾지 못했어요.")
            return
        try:
            import sys as _sys
            _sys.path.insert(0, str(root / "naver_blog_v3"))
            from core.video_batch import find_pending_video_slugs
            pending = find_pending_video_slugs(50)
        except (ImportError, OSError, RuntimeError) as e:  # noqa: BLE001
            log.error("영상 일괄생성 대상 조회 실패: %s", e, exc_info=True)
            messagebox.showerror("영상 일괄생성", "대상을 조회하지 못했어요.")
            return
        if not pending:
            self._notify("영상이 없는 발행 글이 없어요(모두 만들어져 있어요).", kind="info")
            return
        n = simpledialog.askinteger(
            "발행 글 영상 일괄생성",
            f"영상 없는 발행 글 {len(pending)}편 중 몇 편을 만들까요?\n"
            f"(글 1편당 약 1분 소요 · 최신 발행분부터)",
            initialvalue=min(5, len(pending)), minvalue=1, maxvalue=len(pending))
        if not n:
            return
        self._spawn_helper([str(helper), "--pending", str(n)], cwd=root)
        self._notify(f"별도 콘솔에서 {n}편을 만들고 있어요(자동 업로드는 하지 않아요).",
                     kind="info")

    def _pick_slug_dialog(self, candidates: list) -> str:
        """간단한 Toplevel + Listbox 로 처리할 글을 선택하는 다이얼로그.

        Returns:
            선택된 슬러그 문자열, 취소 시 빈 문자열.
        """
        dialog = tk.Toplevel(self)
        dialog.title("멀티채널 팩 — 글 선택")
        dialog.geometry(f"{px(680)}x{px(420)}")
        dialog.transient(self.winfo_toplevel())
        dialog.grab_set()

        L(dialog, "처리할 글을 선택하세요(최근 20개)",
          FB, C["text"]).pack(anchor="w", padx=SP["lg"], pady=(px(14), px(6)))

        list_frame = tk.Frame(dialog, bg=C["card"])
        list_frame.pack(fill="both", expand=True, padx=SP["lg"], pady=SP["xs"])

        scrollbar = tk.Scrollbar(list_frame, orient="vertical")
        scrollbar.pack(side="right", fill="y")

        listbox = tk.Listbox(
            list_frame, font=FB, height=14,
            yscrollcommand=scrollbar.set,
            bg=C["card"], fg=C["text"],
            selectbackground=C["accent_soft"], selectforeground=C["text"],
            activestyle="none")
        for s in candidates:
            listbox.insert("end", s)
        listbox.pack(side="left", fill="both", expand=True)
        listbox.selection_set(0)
        scrollbar.config(command=listbox.yview)

        result = {"slug": ""}

        def _on_ok():
            sel = listbox.curselection()
            if sel:
                result["slug"] = candidates[sel[0]]
            dialog.destroy()

        def _on_cancel():
            dialog.destroy()

        btn_frame = tk.Frame(dialog, bg=C["card"])
        btn_frame.pack(fill="x", padx=SP["lg"], pady=px(14))
        PrimaryBtn(btn_frame, "선택", _on_ok).pack(side="right", padx=(px(8), 0))
        GhostBtn(btn_frame, "취소", _on_cancel, 14).pack(side="right")

        listbox.bind("<Double-Button-1>", lambda _e: _on_ok())
        dialog.bind("<Return>", lambda _e: _on_ok())
        dialog.bind("<Escape>", lambda _e: _on_cancel())

        dialog.wait_window()
        return result["slug"]

    # ── 위험 설정 경고 ─────────────────────────────────────
    @staticmethod
    def _collect_risky_warnings(cfg: dict) -> list:
        """위험한 설정 값을 진단해 경고 문구 리스트를 반환한다(순수 함수, 부수효과 없음).

        저장 전 사전 게이트와 정보성 표시가 같은 판정을 쓰도록 판정 로직을
        여기 한 곳으로 모은다. 값 자체는 게이트 문구로만 노출하고 콘솔엔 찍지 않는다.
        """
        warnings = []
        interval = cfg.get("interval_sec", 180)
        try:
            interval = int(interval)
        except (TypeError, ValueError):
            interval = 180
        if interval < 120:
            warnings.append(
                "포스팅 간격이 너무 짧습니다 ({0}초). "
                "봇 탐지 위험이 높습니다.".format(interval)
            )

        qv_min = cfg.get("qv_min_chars", 1800)
        try:
            qv_min = int(qv_min)
        except (TypeError, ValueError):
            qv_min = 1800
        if qv_min < 1000:
            warnings.append(
                "최소 글자수가 너무 적습니다 ({0}자). "
                "콘텐츠 품질이 저하될 수 있습니다.".format(qv_min)
            )

        use_reserve = cfg.get("use_reservation", True)
        if isinstance(use_reserve, str):
            use_reserve = use_reserve.lower() in ("true", "1", "yes")
        if not use_reserve:
            warnings.append(
                "예약발행을 사용하지 않으면 즉시 발행되어 "
                "일관성이 떨어질 수 있습니다."
            )
        return warnings

    def _confirm_risky_settings(self, cfg: dict) -> bool:
        """위험 설정이 있으면 저장 전에 확인 게이트를 띄운다.

        Returns:
            저장을 진행해도 되면 True. 위험값에 사용자가 '아니오'를 누르면 False
            (호출부는 저장을 취소해 원상 유지). 위험이 없으면 조용히 True.
        """
        warnings = self._collect_risky_warnings(cfg)
        if not warnings:
            return True
        detail = "다음 설정이 위험 범위입니다:\n\n" + "\n\n".join(
            "- " + w for w in warnings
        ) + "\n\n이대로 저장하시겠습니까?"
        return messagebox.askyesno("위험 설정 — 그래도 저장?", detail, icon="warning")

    # ── 저장 ───────────────────────────────────────────────
    def _save(self):
        new = dict(self.app.cfg)
        for k, v in self.vars.items():
            new[k] = v.get()
        for key, variable in self._news_api_vars.items():
            new[key] = variable.get()

        # 위험 설정 사전 게이트 — '아니오'면 저장 자체를 취소(원상 유지).
        if not self._confirm_risky_settings(new):
            return

        # 저장 직후 diff 알림을 위해 콜백에 결과를 적재
        diff_report: dict = {}

        def _capture(payload: dict) -> None:
            diff_report.update(payload)

        save_cfg(new, notify=_capture)

        rolled_back = diff_report.get("rolled_back", False)
        missing = diff_report.get("missing_keys", [])
        backup = diff_report.get("backup")

        # 자동 롤백된 경우: settings.py 는 저장 전 상태로 원복됐다. 앱 캐시(cfg)도
        # 갱신하지 않아야 저장 실패가 실제로 반영되지 않는다.
        if rolled_back:
            messagebox.showerror(
                "설정 저장 실패",
                "핵심 설정이 사라질 위험이 있어 저장을 취소하고\n"
                "이전 상태로 되돌렸어요.\n\n"
                "누락 위험 항목:\n• " + "\n• ".join(missing)
                + "\n\n설정이 반영되지 않았으니 값을 확인한 뒤 다시 시도해 주세요.",
            )
            return

        self.app.cfg = new

        changed = diff_report.get("changed_keys", [])
        n = len(changed)
        msg = f"설정 {n}개를 저장했어요" if n else "저장했어요(바뀐 항목 없음)"
        if backup is not None:
            try:
                msg += f" · 백업 만들어 둠({backup.name})"
            except AttributeError:
                msg += " · 백업 만들어 둠"

        if missing:
            # 롤백도 실패한 최후수단 경로 — 수동 복구 안내.
            messagebox.showwarning(
                "설정 저장 경고",
                "다음 핵심 설정이 누락됐고 자동 복구도 실패했어요:\n• "
                + "\n• ".join(missing)
                + f"\n\n백업({backup.name if backup else '없음'})에서 수동으로 복구해 주세요.",
            )
        else:
            self._notify(msg, kind="ok")

        self.app.dashboard.refresh()
