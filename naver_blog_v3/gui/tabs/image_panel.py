# gui/tabs/image_panel.py — AI 이미지 자동 생성 패널
"""Day 3: GUI에서 1편 6장 자동 생성 흐름을 시각화하는 탭.

운영 원칙:
- 백그라운드 작업은 별도 스레드. tk 위젯 갱신은 항상 self.after(0, ...)로 메인 루프 위임.
- 6장 진행률은 image_inbox 폴더 폴링(0.6초)으로 추적 — watchdog과 무관하게 동작.
- 백엔드 상태 확인(ComfyUI ping·codex login)은 짧은 timeout으로만(GUI 응답성 보존).
- 절대 이미지 생성을 자동으로 시작하지 않는다. 사용자가 "시작" 버튼을 눌러야만 실행.

이미지 백엔드(2026-07-24 정리 — 클라우드 폐기):
- 무텍스트 일러스트 백엔드는 **로컬 ComfyUI(무제한·무료) 단 하나**다. NVIDIA FLUX·
  Pollinations 클라우드는 큐 타임아웃·하드코딩 키 문제로 제거했다. codex CLI 는
  ComfyUI 마저 불가할 때만 쓰이는 레거시 폴백이다(주 경로 아님).
- 이 탭은 `detect_image_backend()`로 현재 활성 티어를 판정해 상태 카드에 표시하고,
  헤더/확인 팝업 문구도 활성 티어에 맞춰(로컬 ComfyUI 무제한 vs codex 한도) 정정한다.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Callable, Optional

log = logging.getLogger(__name__)

from gui import glossary, icons
from gui import theme as _theme_mod
from gui.async_utils import UiLifecycle
from gui.theme import (
    C,
    FB,
    FH,
    FS,
    FT,
    SP,
    Btn,
    Card,
    Ent,
    GhostBtn,
    L,
    PrimaryBtn,
    SecondaryBtn,
    Sep,
)

# 9호 Phase 1(고DPI) — theme.px() 가 아직 없을 수 있는 병렬 개발 상황을
# 대비한 방어적 폴백(코더 A 작업 중). 있으면 UI_SCALE 배율 적용, 없으면 항등.
px = getattr(_theme_mod, "px", lambda n: n)
from gui.widgets import (
    FormField,
    Header,
    HelpIcon,
    ProgressRow,
    StatusPill,
    Switch,
)

# Pillow는 썸네일 표시에 사용. 없으면 텍스트 폴백.
try:
    from PIL import Image, ImageTk  # type: ignore[import-not-found]
    _PILLOW_AVAILABLE = True
except ImportError:  # pragma: no cover - 환경 의존
    Image = None  # type: ignore[assignment]
    ImageTk = None  # type: ignore[assignment]
    _PILLOW_AVAILABLE = False

# ──────────────────────────────────────────────────────────────────────────────
# 상수
# ──────────────────────────────────────────────────────────────────────────────

# 12개 카테고리 — image_prompt_builder의 _PERSONA_DIRECTIVES와 일치(examplebrand 기본).
_DEFAULT_CATEGORIES: tuple[str, ...] = (
    "주거",
    "청년",
    "복지",
    "교육",
    "취업",
    "의료",
    "바우처",
    "부동산",
    "절세",
    "금융",
    "노후",
    "디지털",
)
# 활성 프로필 카테고리로 라우팅(default=위 하드코딩, named=브랜드팩). 이미지 프롬프트는
# 미지 카테고리를 default directive 로 폴백하므로 안전.
try:
    from config import brand as _brand
    CATEGORIES: tuple[str, ...] = tuple(_brand.categories() or _DEFAULT_CATEGORIES)
except Exception:  # noqa: BLE001
    CATEGORIES = _DEFAULT_CATEGORIES

# 카테고리 기본값 — 활성 프로필 첫 카테고리(default=examplebrand '주거').
# CATEGORIES 가 비어 있어도(오설정 named) 콤보/시작 흐름이 깨지지 않게 폴백.
DEFAULT_CATEGORY: str = CATEGORIES[0] if CATEGORIES else _DEFAULT_CATEGORIES[0]

# 레시피 키 → glossary 항목(체크박스 라벨·HelpIcon 설명의 단일 출처).
_RECIPE_GLOSSARY_KEY: dict[str, str] = {
    "five_step": "recipe_five_step",
    "before_after": "recipe_before_after",
    "self_diagnosis": "recipe_self_check",
    "faq": "recipe_faq",
    "compare": "recipe_compare",
    "checklist": "recipe_checklist",
}

BODY_KINDS: tuple[tuple[str, str], ...] = (
    ("five_step", glossary.label("recipe_five_step")),
    ("before_after", glossary.label("recipe_before_after")),
    ("self_diagnosis", glossary.label("recipe_self_check")),
)
INFO_KINDS: tuple[tuple[str, str], ...] = (
    ("faq", glossary.label("recipe_faq")),
    ("compare", glossary.label("recipe_compare")),
    ("checklist", glossary.label("recipe_checklist")),
)

EXPECTED_SLOTS_DEFAULT: int = 6
EXPECTED_SLOTS_DIGITAL: int = 8
DIGITAL_CATEGORY: str = "디지털"

# 인박스 폴링 주기
_INBOX_POLL_INTERVAL_SEC: float = 0.6
# 썸네일 표시 크기 (정사각형)
_THUMB_PREVIEW_PX: int = 110

# codex login status 호출 timeout
_LOGIN_STATUS_TIMEOUT_SEC: int = 6

_NAVER_BLOG_V3_DIR: Path = Path(__file__).resolve().parent.parent.parent  # .../naver_blog_v3
DEFAULT_INBOX_DIR: Path = _NAVER_BLOG_V3_DIR / "image_inbox"
DEFAULT_RUNS_ROOT: Path = _NAVER_BLOG_V3_DIR.parent / "runs"


# ──────────────────────────────────────────────────────────────────────────────
# 순수 헬퍼 (Tk 없이 단위 테스트 가능)
# ──────────────────────────────────────────────────────────────────────────────


def format_progress_text(
    matched: int, total: int, slot_label: str, elapsed_sec: float
) -> str:
    """진행률 표시줄용 텍스트.

    Args:
        matched: 인박스에 떨어진 PNG 수.
        total: 기대 슬롯 수 (6 또는 8).
        slot_label: 현재 처리 중인 슬롯 식별자 (예: "body2"). 없으면 빈 문자열.
        elapsed_sec: 시작 시점부터 경과한 초 수.

    Returns:
        "[3/6] body2 — 1분 23초" 형태 문자열.
    """
    if not isinstance(matched, int) or matched < 0:
        matched = 0
    if not isinstance(total, int) or total <= 0:
        total = EXPECTED_SLOTS_DEFAULT
    minutes, seconds = divmod(max(0, int(elapsed_sec)), 60)
    elapsed_text = f"{minutes}분 {seconds:02d}초" if minutes else f"{seconds}초"
    parts = [f"[{matched}/{total}]"]
    if slot_label:
        parts.append(slot_label)
    parts.append(f"— {elapsed_text}")
    return " ".join(parts)


def select_recipes_from_form(
    body_flags: dict[str, bool],
    info_flags: dict[str, bool],
    *,
    body_keys: tuple[str, ...] = tuple(k for k, _ in BODY_KINDS),
    info_keys: tuple[str, ...] = tuple(k for k, _ in INFO_KINDS),
) -> tuple[list[str], list[str]]:
    """체크박스 상태(dict)에서 활성화된 레시피 키 목록을 추출.

    Args:
        body_flags: {"five_step": True, "before_after": False, ...}
        info_flags: {"faq": True, "compare": True, "checklist": True}
        body_keys/info_keys: 결정적 순서 보장용. 기본값은 모듈 상수.

    Returns:
        (선택된 body kind 키 리스트, 선택된 info kind 키 리스트). 순서는 인자 순.
        body는 최대 2개, info는 최대 3개로 잘라낸다 (1편 슬롯 한계).
    """
    if not isinstance(body_flags, dict) or not isinstance(info_flags, dict):
        raise TypeError("body_flags와 info_flags는 dict[str, bool]여야 함")
    body_selected = [k for k in body_keys if bool(body_flags.get(k))]
    info_selected = [k for k in info_keys if bool(info_flags.get(k))]
    return body_selected[:2], info_selected[:3]


def count_inbox_pngs_for_slug(inbox_dir: Path, slug: str) -> int:
    """인박스 폴더 직속(서브폴더 제외)에서 `{slug}_*.png` 개수.

    _processed/_unmatched는 무시 — 진행률 폴링용 신뢰 가능한 카운트.
    """
    if not isinstance(inbox_dir, Path) or not isinstance(slug, str):
        return 0
    if not slug or not inbox_dir.exists():
        return 0
    try:
        return sum(
            1
            for child in inbox_dir.iterdir()
            if child.is_file()
            and child.suffix.lower() == ".png"
            and child.name.startswith(f"{slug}_")
        )
    except OSError:
        return 0


def expected_slots_for_category(category: str) -> int:
    """카테고리별 기대 슬롯 수 — 디지털만 8, 그 외 6."""
    return EXPECTED_SLOTS_DIGITAL if category == DIGITAL_CATEGORY else EXPECTED_SLOTS_DEFAULT


# 슬러그 자동 파생용 — 한글·영숫자만 남기고 나머지는 제거(공백·특수문자 배제).
_SLUG_UNSAFE_CHARS_RE = re.compile(r"[^0-9A-Za-z가-힣]+")


def derive_slug_from_title(title: str, today: Optional[str] = None) -> str:
    """제목에서 글 식별자(내부용)를 자동으로 만든다.

    형식은 "{YYYY-MM-DD}_{제목 앞 8자(안전 문자만)}". 화면에는 더 이상 이
    식별자를 직접 입력받지 않고(고급 설정에서만 편집 가능) 제목에서 자동으로
    채운다 — 사용자가 "글 고유 이름을 뭐라고 지어야 하나" 고민할 필요가 없게.

    Args:
        title: 사용자가 입력한 제목(빈 문자열이어도 안전).
        today: 테스트용 날짜 오버라이드(``YYYY-MM-DD``). 생략 시 오늘 날짜.

    Returns:
        제목이 있으면 "2026-09-02_청년월세지" 형태, 없으면 날짜만("2026-09-02").
    """
    day = today or datetime.now().strftime("%Y-%m-%d")
    safe = _SLUG_UNSAFE_CHARS_RE.sub("", title or "")[:8]
    return f"{day}_{safe}" if safe else day


# ──────────────────────────────────────────────────────────────────────────────
# 이미지 백엔드 판정 (실제 생성 체인과 일치 — 순수, 헤드리스 테스트 가능)
# ──────────────────────────────────────────────────────────────────────────────

# 활성 티어 식별자 — nvidia_image.generate_image 의 시도 순서와 동일(로컬 ComfyUI → codex).
BACKEND_COMFY: str = "comfy"          # 로컬 ComfyUI (무텍스트 일러스트 유일 백엔드·무제한·무료)
BACKEND_CODEX: str = "codex"          # codex CLI (레거시 폴백 — ComfyUI 불가할 때만)
BACKEND_UNKNOWN: str = "unknown"      # 판정 실패 시 중립 표기(fail-open)

# 티어별 사람이 읽는 라벨 — 상태 카드/문구에 재사용.
_BACKEND_LABELS: dict[str, str] = {
    BACKEND_COMFY: "로컬 ComfyUI (무제한·무료)",
    BACKEND_CODEX: "예비 생성기 (레거시 폴백)",
    BACKEND_UNKNOWN: "판정 불가",
}


def backend_label(tier: str) -> str:
    """티어 식별자 → 사람이 읽는 라벨(미지 티어는 중립 표기)."""
    return _BACKEND_LABELS.get(tier, _BACKEND_LABELS[BACKEND_UNKNOWN])


def uses_quota_limited_backend(tier: str) -> bool:
    """이 티어가 '외부 한도(codex 5시간 한도)'를 소비하는가.

    codex 만 한도 제약이 있고, ComfyUI(로컬 무제한)는 5시간 한도 개념이 없다.
    헤더/확인 팝업의 '한도 소모' 경고를 이 판정으로 게이트한다.
    """
    return tier == BACKEND_CODEX


def detect_image_backend(*, probe_comfy: bool = True) -> dict:
    """현재 이미지 생성에 실제로 쓰일 활성 백엔드 티어를 판정한다.

    실제 체인(core/nvidia_image.generate_image)의 시도 순서를 그대로 반영:
        로컬 ComfyUI → (불가 시) codex 레거시.
    (NVIDIA FLUX·Pollinations 클라우드 백엔드는 2026-07-24 제거됐다.)

    Args:
        probe_comfy: True 면 ComfyUI 서버 도달을 실제 ping 으로 확인(짧은 timeout).
            False 면 ping 없이 '설정상 켜짐' 여부만 본다(테스트/오프라인 판정용).

    Returns:
        {
            "active": 활성 티어 식별자(BACKEND_*),
            "comfy": bool,          # 로컬 ComfyUI 로 지금 생성 가능한가
            "comfy_url": str,       # ComfyUI 서버 주소(표시용, 자격증명 아님)
        }

    판정 실패(모듈 import 오류 등)는 절대 예외를 던지지 않고 active=BACKEND_UNKNOWN 으로
    중립 반환한다(fail-open — GUI 무중단). 상위 UI 는 codex 프로브를 별도로 유지하므로
    UNKNOWN 이어도 codex 경로 안내가 사라지지 않는다.
    """
    result = {
        "active": BACKEND_UNKNOWN,
        "comfy": False,
        "comfy_url": "",
    }
    try:
        from config import settings as _settings
    except Exception:  # noqa: BLE001 — 설정 로드 실패 시 중립 표기
        log.debug("이미지 백엔드 판정: settings 로드 실패", exc_info=True)
        return result

    def _cfg(name: str, default):  # type: ignore[no-untyped-def]
        return getattr(_settings, name, default)

    # 로컬 ComfyUI 는 무텍스트 일러스트 유일 백엔드. probe_comfy=True 면 실제 도달(ping)까지 확인.
    comfy_enabled = bool(_cfg("IMAGE_COMFY_ENABLED", True))
    if comfy_enabled:
        try:
            from core import comfy_image as _comfy
        except ImportError:
            try:
                from naver_blog_v3.core import comfy_image as _comfy  # type: ignore[no-redef]
            except Exception:  # noqa: BLE001
                _comfy = None  # type: ignore[assignment]
        except Exception:  # noqa: BLE001
            _comfy = None  # type: ignore[assignment]
        if _comfy is not None:
            try:
                result["comfy_url"] = _comfy.base_url()
            except Exception:  # noqa: BLE001
                result["comfy_url"] = ""
            if probe_comfy:
                try:
                    # available()=ENABLED + 워크플로우 존재 + 서버 ping(짧은 timeout, fail-open).
                    result["comfy"] = bool(_comfy.available())
                except Exception:  # noqa: BLE001 — ping/설정 오류는 '미가동'으로 간주
                    log.debug("이미지 백엔드 판정: ComfyUI available() 실패", exc_info=True)
                    result["comfy"] = False

    # 활성 티어 결정 — 실제 체인 우선순위(ComfyUI → codex 레거시).
    if result["comfy"]:
        result["active"] = BACKEND_COMFY
    else:
        # 로컬 ComfyUI 가 불가 — 이미지 생성의 실제 경로는 codex 레거시뿐.
        result["active"] = BACKEND_CODEX
    return result


# ──────────────────────────────────────────────────────────────────────────────
# ImagePanelTab
# ──────────────────────────────────────────────────────────────────────────────


class ImagePanelTab(UiLifecycle, tk.Frame):
    """AI 이미지 자동 생성 탭 (로컬 ComfyUI → 예비 생성기 폴백)."""

    def __init__(self, parent: tk.Misc, app) -> None:  # type: ignore[no-untyped-def]
        super().__init__(parent, bg=C["bg"])
        self.app = app

        # 상태
        self._inbox_dir: Path = DEFAULT_INBOX_DIR
        self._runs_root: Path = DEFAULT_RUNS_ROOT
        self._cancel_event: threading.Event = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._polling_active: bool = False
        self._start_ts: float = 0.0
        self._current_slug: str = ""
        self._expected_slots: int = EXPECTED_SLOTS_DEFAULT

        # 마지막으로 판정된 활성 이미지 백엔드 티어(확인 팝업 문구 게이트에 사용).
        # 판정 전/실패 시 UNKNOWN — 이 경우 팝업은 예비 생성기 한도 경고를 붙이지
        # 않는다(과잉경고 방지).
        self._active_backend: str = BACKEND_UNKNOWN

        # 미리보기 이미지 참조 보관 (gc 방지)
        self._thumb_refs: list = []
        self._preview_buttons: list[tk.Button] = []

        # "환경 상태" 카드의 예비 생성기 상세(설치·로그인)는 기본 접힘 — 실제
        # 주 경로(로컬 ComfyUI)보다 화면 비중이 커지지 않게(2026-09-02 UX 감사).
        self._backend_details_open: bool = False

        # 입력 변수 — 글 고유 이름(slug)은 더 이상 직접 입력받지 않고 제목에서
        # 자동 파생한다("고급" 접힘 안에서만 손으로 고칠 수 있다).
        self.title_var = tk.StringVar()
        self.slug_var = tk.StringVar(value=derive_slug_from_title(""))
        self._slug_user_edited: bool = False
        self.subtitle_var = tk.StringVar()
        self.category_var = tk.StringVar(value=DEFAULT_CATEGORY)
        self.body_flag_vars: dict[str, tk.BooleanVar] = {
            "five_step": tk.BooleanVar(value=True),
            "before_after": tk.BooleanVar(value=True),
            "self_diagnosis": tk.BooleanVar(value=False),
        }
        self.info_flag_vars: dict[str, tk.BooleanVar] = {
            "faq": tk.BooleanVar(value=True),
            "compare": tk.BooleanVar(value=True),
            "checklist": tk.BooleanVar(value=True),
        }
        self.title_var.trace_add("write", self._on_title_changed_for_slug)

        self._build()
        self._init_lifecycle()
        self.ui_after(self._refresh_status, delay=150)

    # ──────────────────────────────────────────────────────────────────────
    # UI 구성
    # ──────────────────────────────────────────────────────────────────────

    def _build(self) -> None:
        # 헤더 — 탭 최상단 고정 행(photo_post/product_post 와 동일하게 self 의
        # 직계 자식으로 pack, 스크롤 캔버스 안에 들어가지 않는다 — 디자이너
        # 심사 🔴6: 이전에는 F() 로 한 겹 더 감싸서 만들었는데, 그 중간 프레임이
        # 실측(winfo) 자체는 정상이어도 캡처 파이프라인에서 헤더가 안 보인다는
        # 지적을 받아 다른 두 탭과 똑같은 얕은 구조로 맞췄다). 부제는 활성
        # 백엔드에 따라 _refresh_status 가 갱신하므로(로컬 생성기 무제한 vs
        # 예비 생성기 한도) Header 의 고정 subtitle 대신 별도 Label 을 만들어
        # 참조를 들고 있는다(초기값은 판정 전 중립 문구).
        Header(self, "AI 이미지 자동 생성", icon=icons.glyph("image")).pack(
            fill="x", padx=SP["xl"], pady=(px(10), SP["xs"]))
        self._header_sub_lbl = L(
            self,
            "1편 6장 자동 (썸네일 1 + 본문 2 + 정보 3) — 백엔드 확인 중…",
            FS,
            C["muted"],
        )
        self._header_sub_lbl.pack(anchor="w", padx=SP["xl"], pady=(0, SP["xs"]))
        Sep(self).pack(fill="x")

        # 스크롤 영역
        canvas = tk.Canvas(self, bg=C["bg"], bd=0, highlightthickness=0)
        vsb = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        sf = tk.Frame(canvas, bg=C["bg"])
        sf.bind(
            "<Configure>",
            lambda _e: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.create_window((0, 0), window=sf, anchor="nw")
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        # 마우스 휠 (이 탭에 들어왔을 때만 바인딩)
        def _on_wheel(e):  # type: ignore[no-untyped-def]
            canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")

        canvas.bind("<Enter>", lambda _e: canvas.bind_all("<MouseWheel>", _on_wheel))
        canvas.bind("<Leave>", lambda _e: canvas.unbind_all("<MouseWheel>"))

        self._build_status_card(sf)
        self._build_form_card(sf)
        self._build_run_card(sf)
        self._build_preview_card(sf)
        self._build_manual_card(sf)

    def _build_status_card(self, parent: tk.Misc) -> None:
        """환경 상태 카드 — 주 경로(로컬 이미지 생성기) 한 줄을 앞세우고,

        예비(레거시) 생성기의 설치·로그인 상세는 기본 접어 둔다(2026-09-02 UX
        감사: "실제 사용 빈도 낮은 경로가 시각적으로 더 부각됨" 지적 반영).
        """
        card = tk.Frame(parent, bg=C["card"], padx=px(20), pady=px(14))
        card.pack(fill="x", padx=SP["xl"], pady=(SP["lg"], 0))

        head = tk.Frame(card, bg=C["card"])
        head.pack(fill="x", pady=(0, SP["sm"]))
        L(head, "환경 상태", FT, C["accent"]).pack(side="left")
        GhostBtn(head, "새로고침", self._refresh_status, 12,
                icon=icons.glyph("refresh")).pack(side="right")

        # 주 경로 한 줄 — "지금 어느 생성기로 이미지가 나오는지"만 즉시 보여준다.
        pill_row = tk.Frame(card, bg=C["card"])
        pill_row.pack(anchor="w")
        L(pill_row, "이미지 생성기:", FB, C["muted"]).pack(side="left", padx=(0, SP["sm"]))
        self._backend_pill = StatusPill(pill_row, "확인 중…", kind="neutral")
        self._backend_pill.pack(side="left")
        self._backend_lbl = L(pill_row, "", FS, C["muted"])
        self._backend_lbl.pack(side="left", padx=(SP["sm"], 0))

        # 이미지 저장 폴더 — 절대경로 텍스트 대신 [폴더 열기] 버튼만 노출.
        folder_row = tk.Frame(card, bg=C["card"])
        folder_row.pack(anchor="w", pady=(px(6), 0))
        L(folder_row, "이미지 저장 폴더:", FS, C["muted"]).pack(side="left", padx=(0, px(6)))
        SecondaryBtn(folder_row, "폴더 열기", self._open_inbox_folder,
                    icon=icons.glyph("folder")).pack(side="left")

        # 예비(레거시) 생성기 상세 — 기본 접힘.
        self._details_toggle_btn = GhostBtn(
            card, "예비 생성기 정보 ▾", self._toggle_backend_details, 16)
        self._details_toggle_btn.pack(anchor="w", pady=(SP["sm"], 0))

        self._details_frame = tk.Frame(card, bg=C["card"])
        self._codex_status_lbl = L(self._details_frame, "예비 이미지 생성기: 확인 중...",
                                   FS, C["muted"])
        self._codex_status_lbl.pack(anchor="w", pady=(px(6), 0))
        self._login_status_lbl = L(self._details_frame, "로그인 상태: 확인 중...",
                                   FS, C["muted"])
        self._login_status_lbl.pack(anchor="w", pady=(px(2), 0))
        # 기본 숨김 — _toggle_backend_details 가 pack/pack_forget 으로 관리.

    def _toggle_backend_details(self) -> None:
        self._backend_details_open = not self._backend_details_open
        if self._backend_details_open:
            self._details_frame.pack(fill="x", anchor="w")
            self._details_toggle_btn.config(text="예비 생성기 정보 ▴")
        else:
            self._details_frame.pack_forget()
            self._details_toggle_btn.config(text="예비 생성기 정보 ▾")

    def _build_form_card(self, parent: tk.Misc) -> None:
        card = tk.Frame(parent, bg=C["card"], padx=px(20), pady=px(14))
        card.pack(fill="x", padx=SP["xl"], pady=(SP["md"], 0))

        L(card, "글 정보", FT, C["accent"]).pack(anchor="w", pady=(0, SP["sm"]))

        FormField(card, "제목", self.title_var, kind="entry",
                  width=44).pack(fill="x", pady=(0, px(6)))
        FormField(card, "부제", self.subtitle_var, kind="entry",
                  width=44).pack(fill="x", pady=(0, px(6)))
        FormField(card, "카테고리", self.category_var, kind="combo",
                  values=list(CATEGORIES), width=14).pack(fill="x", pady=(0, SP["sm"]))

        # body / info 레시피 토글
        rec_row = tk.Frame(card, bg=C["card"])
        rec_row.pack(fill="x", pady=(px(6), 0))

        body_frame = tk.Frame(rec_row, bg=C["card"])
        body_frame.pack(side="left", anchor="nw", padx=(0, SP["xl"]))
        L(body_frame, "본문 카드 (최대 2개)", FS, C["muted"]).pack(anchor="w")
        for key, label in BODY_KINDS:
            self._build_recipe_checkbox(body_frame, key, label, self.body_flag_vars[key])

        info_frame = tk.Frame(rec_row, bg=C["card"])
        info_frame.pack(side="left", anchor="nw")
        L(info_frame, "정보 카드 (최대 3개)", FS, C["muted"]).pack(anchor="w")
        for key, label in INFO_KINDS:
            self._build_recipe_checkbox(info_frame, key, label, self.info_flag_vars[key])

        self._build_advanced_section(card)

    def _build_recipe_checkbox(self, parent: tk.Misc, key: str, label: str,
                               var: tk.BooleanVar) -> None:
        """레시피 토글 한 줄(Switch) + 설명(HelpIcon) — glossary 가 라벨·설명의 단일 출처."""
        row = tk.Frame(parent, bg=C["card"])
        row.pack(anchor="w", pady=(px(2), 0))
        Switch(row, var=var, text=label).pack(side="left")
        glossary_key = _RECIPE_GLOSSARY_KEY.get(key)
        if glossary_key:
            HelpIcon(row, glossary.help(glossary_key)).pack(side="left", padx=(px(2), 0))

    def _build_advanced_section(self, card: tk.Misc) -> None:
        """"고급" 접힘 — 자동으로 채워지는 글 식별자를 직접 고치고 싶을 때만 편다."""
        self._advanced_open = False
        self._advanced_toggle_btn = GhostBtn(
            card, "고급 ▾", self._toggle_advanced_section, 10)
        self._advanced_toggle_btn.pack(anchor="w", pady=(px(10), 0))

        self._advanced_frame = tk.Frame(card, bg=C["card"])
        row = tk.Frame(self._advanced_frame, bg=C["card"])
        row.pack(fill="x", pady=(px(6), 0))
        label_key = glossary.label("slug")
        L(row, f"{label_key}:", FB, C["muted"], width=8, anchor="w").pack(side="left")
        slug_entry = Ent(row, self.slug_var, w=32)
        slug_entry.pack(side="left", padx=(SP["xs"], 0), ipady=3)
        slug_entry.bind("<KeyRelease>", self._mark_slug_user_edited)
        HelpIcon(row, glossary.help("slug")).pack(side="left", padx=(SP["xs"], 0))
        GhostBtn(row, "자동으로 되돌리기", self._reset_slug_to_auto, 14).pack(
            side="left", padx=(SP["sm"], 0))
        # 기본 숨김 — _toggle_advanced_section 이 pack/pack_forget 으로 관리.

    def _toggle_advanced_section(self) -> None:
        self._advanced_open = not self._advanced_open
        if self._advanced_open:
            self._advanced_frame.pack(fill="x", anchor="w")
            self._advanced_toggle_btn.config(text="고급 ▴")
        else:
            self._advanced_frame.pack_forget()
            self._advanced_toggle_btn.config(text="고급 ▾")

    def _on_title_changed_for_slug(self, *_args) -> None:
        """제목이 바뀔 때마다 글 식별자를 자동으로 다시 채운다(사용자가 고급에서

        직접 고친 뒤에는 더 이상 덮어쓰지 않는다 — `_mark_slug_user_edited` 참고.
        """
        if self._slug_user_edited:
            return
        try:
            self.slug_var.set(derive_slug_from_title(self.title_var.get()))
        except tk.TclError:
            pass

    def _mark_slug_user_edited(self, _event=None) -> None:
        self._slug_user_edited = True

    def _reset_slug_to_auto(self) -> None:
        self._slug_user_edited = False
        self.slug_var.set(derive_slug_from_title(self.title_var.get()))

    def _build_run_card(self, parent: tk.Misc) -> None:
        card = tk.Frame(parent, bg=C["card"], padx=px(20), pady=px(14))
        card.pack(fill="x", padx=SP["xl"], pady=(SP["md"], 0))

        L(card, "실행", FT, C["accent"]).pack(anchor="w", pady=(0, SP["sm"]))

        btn_row = tk.Frame(card, bg=C["card"])
        btn_row.pack(fill="x", pady=(0, SP["sm"]))
        self._start_btn = PrimaryBtn(
            btn_row, "6장 자동 생성 시작", self._on_start_clicked, 22,
            icon=icons.glyph("run")
        )
        self._start_btn.pack(side="left")
        self._cancel_btn = GhostBtn(btn_row, "취소", self._on_cancel_clicked, 10,
                                    icon=icons.glyph("stop"))
        self._cancel_btn.pack(side="left", padx=SP["sm"])
        self._cancel_btn.configure(state="disabled")

        # 진행률
        self._progress = ProgressRow(card)
        self._progress.pack(fill="x", pady=(0, SP["xs"]))
        self._progress.set(0, 0, "대기 중")

        # 로그
        L(card, "로그", FS, C["muted"]).pack(anchor="w", pady=(SP["sm"], px(2)))
        self._log_text = tk.Text(
            card,
            bg=C["surface"],
            fg=C["text"],
            insertbackground=C["accent"],
            height=7,
            font=("Consolas", 9),
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground=C["border"],
            wrap="word",
        )
        self._log_text.pack(fill="x")
        self._log_text.tag_configure("info", foreground=C["text"])
        self._log_text.tag_configure("ok", foreground=C["green"])
        self._log_text.tag_configure("warn", foreground=C["orange"])
        self._log_text.tag_configure("err", foreground=C["red"])
        self._log_text.tag_configure("muted", foreground=C["muted"])
        self._log_text.configure(state="disabled")

    def _build_preview_card(self, parent: tk.Misc) -> None:
        card = tk.Frame(parent, bg=C["card"], padx=px(20), pady=px(14))
        card.pack(fill="x", padx=SP["xl"], pady=(SP["md"], 0))
        L(card, "결과 미리보기 (클릭하면 저장 폴더가 열려요)", FT, C["accent"]).pack(
            anchor="w", pady=(0, SP["sm"])
        )
        self._preview_grid = tk.Frame(card, bg=C["card"])
        self._preview_grid.pack(fill="x")
        self._render_preview_grid(matched_paths={})

    def _build_manual_card(self, parent: tk.Misc) -> None:
        card = tk.Frame(parent, bg=C["card"], padx=px(20), pady=px(14))
        card.pack(fill="x", padx=SP["xl"], pady=(SP["md"], px(20)))
        L(card, "자동 생성이 실패했을 때", FT, C["accent"]).pack(anchor="w")
        L(
            card,
            "자동 생성에 실패하면 프롬프트가 클립보드에 복사돼요. "
            "이미지 저장 폴더를 열어 PNG 파일을 그대로 넣으면 자동으로 반영돼요.",
            FS,
            C["muted"],
            wraplength=px(720),
            justify="left",
        ).pack(anchor="w", pady=(SP["xs"], SP["sm"]))
        GhostBtn(card, "이미지 저장 폴더 열기", self._open_inbox_folder, 18,
                icon=icons.glyph("folder")).pack(side="left")

    # ──────────────────────────────────────────────────────────────────────
    # 상태 점검 (활성 이미지 백엔드 + 예비 생성기 설치/로그인)
    # ──────────────────────────────────────────────────────────────────────

    def _refresh_status(self) -> None:
        """활성 이미지 백엔드 + 예비 생성기 상태를 백그라운드 스레드에서 확인."""
        self.spawn(self._refresh_status_worker, name="image-status")

    def _refresh_status_worker(self) -> None:
        # 1) 활성 이미지 백엔드 판정(로컬 ComfyUI ping 포함, 전부 fail-open).
        #    ComfyUI ping 은 짧은 timeout(IMAGE_COMFY_PING_TIMEOUT≈2s)이라 이 스레드만 잠깐 블록.
        self._detect_and_show_backend()

        # 2) 예비 생성기는 이제 레거시 폴백 — 설치·로그인 프로브는 참고용으로 유지.
        codex_path = self._locate_codex_executable()
        if codex_path:
            self.ui_after(
                lambda: self._codex_status_lbl.config(
                    text="예비 이미지 생성기: 설치됨", fg=C["muted"]
                ),
            )
        else:
            self.ui_after(
                lambda: self._codex_status_lbl.config(
                    text="예비 이미지 생성기: 설치 안 됨 (주 경로 아님 — 없어도 괜찮아요)",
                    fg=C["muted"],
                ),
            )

        login_text, color = self._probe_codex_login(codex_path)
        self.ui_after(
            lambda t=login_text, c=color: self._login_status_lbl.config(text=t, fg=c)
        )

    def _detect_and_show_backend(self) -> None:
        """활성 이미지 백엔드를 판정해 상태 카드·헤더 문구를 갱신한다(스레드 컨텍스트).

        판정은 detect_image_backend()에 위임(순수·fail-open). UNKNOWN 이어도 예외 없이
        중립 문구로 표기하고, 위젯 갱신은 항상 ui_after 로 메인 루프에 위임한다.
        """
        info = detect_image_backend(probe_comfy=True)
        active = str(info.get("active") or BACKEND_UNKNOWN)
        self._active_backend = active

        if active == BACKEND_COMFY:
            pill_text, pill_kind = "내 PC 준비됨", "ok"
        elif active == BACKEND_CODEX:
            pill_text, pill_kind = "예비 도구 사용 중", "warn"
        else:
            pill_text, pill_kind = "확인 못 함", "neutral"

        # 로컬 ComfyUI 활성이면 서버 주소를 곁들인다(표시용 — 자격증명 아님).
        detail = ""
        if active == BACKEND_COMFY and info.get("comfy_url"):
            detail = str(info["comfy_url"])

        def _apply() -> None:
            try:
                self._backend_pill.set(pill_text, pill_kind)
                self._backend_lbl.config(text=detail)
                self._header_sub_lbl.config(text=self._header_subtitle_for(active))
            except tk.TclError:
                # 탭이 닫히는 중이면 위젯이 사라졌을 수 있음 — 무해하게 무시.
                pass

        self.ui_after(_apply)

    @staticmethod
    def _header_subtitle_for(active: str) -> str:
        """활성 백엔드에 맞는 헤더 부제(한도 문구 정정)."""
        base = "1편 6장 자동 (썸네일 1 + 본문 2 + 정보 3)"
        if active == BACKEND_COMFY:
            return f"{base} — 내 PC에서 무제한으로 만들어요 (무료·횟수 제한 없음)"
        if active == BACKEND_CODEX:
            return f"{base} — 예비 생성기 사용 중 (5시간마다 {EXPECTED_SLOTS_DEFAULT}회 제한)"
        return f"{base} — 확인 중…"

    def _backend_confirm_line(self, slots: int) -> str:
        """확인 팝업에 넣을 백엔드 안내 1줄 — 횟수 제한 경고는 예비 생성기일 때만.

        마지막 판정(_active_backend)을 신뢰한다. UNKNOWN/판정 전이면 횟수 제한 경고를
        붙이지 않고 실제 체인만 안내한다(과잉경고 방지 — 대부분 내 PC로 나감).
        """
        active = self._active_backend
        if active == BACKEND_COMFY:
            return f"내 PC에서 {slots}장을 만들어요. 무제한·무료라 횟수 제한이 없어요."
        if uses_quota_limited_backend(active):  # BACKEND_CODEX
            return (
                f"내 PC 생성기를 쓸 수 없어 예비 생성기로 만들어요.\n"
                f"예비 생성기의 5시간 제한 횟수가 {slots}회 소모돼요."
            )
        # UNKNOWN — 판정 실패. 체인만 안내하고 횟수 제한 경고는 생략(fail-open).
        return f"내 PC에서 {slots}장을 만들어요."

    @staticmethod
    def _locate_codex_executable() -> Optional[str]:
        """패키지 내 _resolve_codex_executable과 동일 로직을 호출 (Day 3 작업 1 결과)."""
        try:
            # 이미지 체인은 절대 임포트로 통일(codex_cli_bot 이중 로드 방지). ImportError 시 which 폴백.
            from naver_blog_v3.core.codex_cli_bot import _resolve_codex_executable

            return _resolve_codex_executable("codex")
        except ImportError:
            return shutil.which("codex")

    @staticmethod
    def _probe_codex_login(codex_path: Optional[str]) -> tuple[str, str]:
        """예비 생성기의 로그인 상태 1회 확인. (표시 텍스트, 색상) 반환."""
        if not codex_path:
            return ("로그인 상태: 예비 생성기가 설치돼 있지 않아요", C["red"])
        try:
            completed = subprocess.run(
                [codex_path, "login", "status"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=_LOGIN_STATUS_TIMEOUT_SEC,
            )
        except subprocess.TimeoutExpired:
            return ("로그인 상태: 응답이 없어요(시간 초과)", C["yellow"])
        except (OSError, subprocess.SubprocessError) as exc:
            log.debug("예비 생성기 로그인 상태 확인 실패", exc_info=True)
            return (f"로그인 상태: 확인하지 못했어요 ({type(exc).__name__})", C["red"])

        merged = (completed.stdout or "") + "\n" + (completed.stderr or "")
        lower = merged.lower()
        if completed.returncode == 0 and ("logged in" in lower or "signed in" in lower):
            # 사용자명 추출 시도 — "Logged in as foo@bar" 패턴 (없어도 OK).
            who = ""
            for line in merged.splitlines():
                if "logged in as" in line.lower():
                    who = line.strip().split("as", 1)[-1].strip(" :@")
                    break
            label = f"로그인 상태: {who}" if who else "로그인 상태: 로그인됨"
            return (label, C["green"])
        # not logged in
        return ("로그인 상태: 로그인이 필요해요 — 설정 담당자에게 문의하세요", C["yellow"])

    # ──────────────────────────────────────────────────────────────────────
    # 실행 흐름
    # ──────────────────────────────────────────────────────────────────────

    def _on_start_clicked(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            messagebox.showinfo("실행 중", "이미 이미지 생성이 진행 중입니다.")
            return

        slug = self.slug_var.get().strip()
        title = self.title_var.get().strip()
        subtitle = self.subtitle_var.get().strip()
        category = self.category_var.get().strip() or DEFAULT_CATEGORY

        if not slug:
            messagebox.showwarning(
                "입력 필요",
                f"{glossary.label('slug')}가 비어 있어요. "
                "제목을 입력하거나 고급 설정에서 직접 채워주세요.")
            return
        if not title:
            messagebox.showwarning("입력 필요", "제목을 입력하세요.")
            return
        if not subtitle:
            messagebox.showwarning("입력 필요", "부제를 입력하세요.")
            return
        if category not in CATEGORIES:
            messagebox.showwarning(
                "카테고리 오류", f"지원되지 않는 카테고리: {category}"
            )
            return

        body_keys, info_keys = select_recipes_from_form(
            {k: v.get() for k, v in self.body_flag_vars.items()},
            {k: v.get() for k, v in self.info_flag_vars.items()},
        )
        if len(body_keys) < 1 or len(info_keys) < 1:
            messagebox.showwarning(
                "레시피 부족",
                "body 1개 이상, info 1개 이상을 선택하세요.\n"
                "(권장: body 2 + info 3)",
            )
            return

        slots = expected_slots_for_category(category)
        if not messagebox.askyesno(
            "이미지 생성 시작",
            f"이 글: {title}\n"
            f"카테고리: {category}\n"
            f"이미지 {slots}장을 만들어요\n\n"
            f"{self._backend_confirm_line(slots)}\n진행할까요?",
        ):
            return

        self._cancel_event.clear()
        self._current_slug = slug
        self._expected_slots = expected_slots_for_category(category)
        self._start_ts = time.monotonic()
        self._thumb_refs.clear()

        # UI 상태 전환
        self._start_btn.configure(state="disabled", text="진행 중...")
        self._cancel_btn.configure(state="normal")
        self._progress.reset()
        self._render_preview_grid(matched_paths={})
        self._clear_log()
        self._log(f"━━ {slug} 시작 ({self._expected_slots}장 예정) ━━", "info")
        self._log(f"카테고리: {category} / body: {body_keys} / info: {info_keys}", "muted")

        # 폴링 시작
        self._schedule_poll()

        # 워커 스레드 — is_alive() 로 재진입 가드/취소 판단을 하므로 raw Thread 로
        # 핸들을 직접 들고 있는다(UiLifecycle._threads 에도 등록해 수명 추적만 겸함).
        self._worker = threading.Thread(
            target=self._run_dispatch,
            args=(slug, title, subtitle, category, body_keys, info_keys),
            daemon=True,
        )
        self._threads.append(self._worker)
        self._worker.start()

    def _on_cancel_clicked(self) -> None:
        if self._worker is None or not self._worker.is_alive():
            return
        if not messagebox.askyesno(
            "취소 확인",
            "진행 중인 현재 이미지 생성은 중단되지 않지만, "
            "다음 슬롯 진입을 막습니다. 계속할까요?",
        ):
            return
        self._cancel_event.set()
        self._log("취소 요청 — 현재 호출 완료 후 중단됩니다.", "warn")

    def _run_dispatch(
        self,
        slug: str,
        title: str,
        subtitle: str,
        category: str,
        body_keys: list[str],
        info_keys: list[str],
    ) -> None:
        """ImageDispatcher.dispatch를 백그라운드에서 호출. 실패도 GUI에 보고."""
        try:
            # ⚠️ 이미지 체인은 반드시 naver_blog_v3.core.* (절대 패키지) 로 임포트한다.
            # ImageDispatcher.__init__ 이 naver_blog_v3.core.codex_cli_bot.CodexCLIBot 로
            # isinstance 검사를 하므로, core.* (상대) 로 만든 bot 을 넘기면 '같은 파일·다른
            # 클래스 객체'(이중 모듈 정체성)가 되어 TypeError 로 dispatch 가 실패한다.
            # auto_scheduler·e2e 와 동일하게 절대 임포트로 통일. (2026-07-08 정합화)
            from naver_blog_v3.core.codex_cli_bot import (
                BodyRecipe,
                CodexCLIBot,
                InfoRecipe,
            )
            from naver_blog_v3.core.image_dispatcher import ImageDispatcher
            from naver_blog_v3.core.image_inbox import ImageInbox

            self._inbox_dir.mkdir(parents=True, exist_ok=True)
            self._runs_root.mkdir(parents=True, exist_ok=True)

            body_recipes = [
                BodyRecipe(kind=k, items=[], illustration_hint=None)  # type: ignore[arg-type]
                for k in body_keys
            ]
            info_recipes = [
                InfoRecipe(  # type: ignore[arg-type]
                    kind=k,
                    items=[
                        {"q": f"Q{i + 1}", "a": f"A{i + 1}"} for i in range(3)
                    ],
                )
                for k in info_keys
            ]

            # 봇 선택 — auto_scheduler 와 동일 규칙: 로컬 ComfyUI 가 사용 가능하면
            # NvidiaImageBot(이름은 레거시, 실제 백엔드는 로컬 ComfyUI·예비 생성기 한도
            # 미소비), 불가할 때만 예비 생성기 폴백. 이래야 상태 카드의 '활성 백엔드'
            # 표기와 실제 생성 경로가 일치한다(이전엔 항상 예비 경로를 써 표기와 어긋났다).
            bot = CodexCLIBot(inbox_dir=self._inbox_dir)
            bot_label = "예비 이미지 생성기"
            try:
                from naver_blog_v3.core import nvidia_image as _nvimg
                if _nvimg.available():
                    from naver_blog_v3.core.nvidia_image_bot import NvidiaImageBot
                    bot = NvidiaImageBot(inbox_dir=self._inbox_dir)
                    bot_label = "내 PC 이미지 생성기"
            except Exception as _nv_exc:  # noqa: BLE001 — 실패 시 예비 생성기 폴백(무해)
                log.debug("내 PC 이미지 생성기 미사용 — 예비 생성기 폴백: %s", _nv_exc)
            inbox = ImageInbox(inbox_dir=self._inbox_dir, runs_root=self._runs_root)

            def _notify(msg: str) -> None:
                self.ui_after(lambda m=msg: self._log(m, "info"))

            self.ui_after(lambda lbl=bot_label: self._log(f"생성기: {lbl}", "muted"))
            dispatcher = ImageDispatcher(bot=bot, inbox=inbox, notifier=_notify)

            result = dispatcher.dispatch(
                slug=slug,
                title=title,
                subtitle=subtitle,
                category=category,
                body_recipes=body_recipes,
                info_recipes=info_recipes,
            )
        except FileNotFoundError as exc:
            # 예비 생성기 경로에서만 발생(내 PC 생성기 경로는 이 실행파일이 필요 없음).
            log.debug("예비 이미지 생성기 실행 파일을 찾지 못했습니다: %s", exc)
            self.ui_after(lambda: self._log(
                "예비 이미지 생성기가 설치돼 있지 않아요.", "err"))
            self.ui_after(self._finalize_run, False)
            return
        except RuntimeError as exc:
            # 내 PC 생성기 실패 또는 예비 생성기 한도 초과 — 원인에 따라 의미가 다르므로 사유를 그대로.
            self.ui_after(lambda e=exc: self._log(f"이미지 생성에 실패했어요: {e}", "err"))
            self.ui_after(self._finalize_run, False)
            return
        except Exception as exc:  # noqa: BLE001 - 사용자 화면에 모든 실패 노출
            log.error("이미지 생성 중 예기치 못한 오류", exc_info=True)
            self.ui_after(lambda e=exc: self._log(f"오류가 났어요: {e}", "err"))
            self.ui_after(self._finalize_run, False)
            return

        # 결과 보고
        tier = result.tier_used
        matched = result.reconcile.matched_count
        total = result.reconcile.expected_slots
        success = tier == "tier1_auto" and matched == total

        def _report() -> None:
            if tier == "tier1_auto":
                self._log(f"자동 생성 완료 — {matched}/{total}장", "ok")
            else:
                reason = result.fallback_reason or "사유 미상"
                self._log(f"자동 생성이 안 돼 수동 모드로 넘어갔어요 — {reason}", "warn")
            if result.reconcile.missing_slots:
                self._log(
                    f"빠진 이미지: {result.reconcile.missing_slots}", "warn"
                )

        self.ui_after(_report)
        self.ui_after(self._finalize_run, success)

    def _finalize_run(self, success: bool) -> None:
        """UI 상태 복귀 + 폴링 종료 + 미리보기 최종 갱신."""
        self._polling_active = False

        self._start_btn.configure(state="normal", text="6장 자동 생성 시작")
        self._cancel_btn.configure(state="disabled")

        # 최종 1회 폴링 — 늦게 도착한 PNG 반영
        self._poll_inbox(final=True)
        self._cancel_event.clear()

        if success:
            self._log("━━ 완료 ━━", "ok")
        else:
            self._log("━━ 종료 (일부 실패 또는 수동 모드) ━━", "warn")

    # ──────────────────────────────────────────────────────────────────────
    # 인박스 폴링 + 미리보기
    # ──────────────────────────────────────────────────────────────────────

    def _schedule_poll(self) -> None:
        self._polling_active = True
        self._poll_inbox(final=False)

    def _poll_inbox(self, *, final: bool) -> None:
        """0.6초마다 저장 폴더를 확인하고 진행률/미리보기 갱신.

        `ui_after(..., delay=600)` 로 스스로를 재예약하는 체인 — 탭이 파괴되면
        `UiLifecycle.ui_after` 가 조용히 무시해 자동으로 멈춘다(§0.3).
        """
        matched_paths = self._collect_inbox_paths(self._current_slug)
        matched = len(matched_paths)
        elapsed = time.monotonic() - self._start_ts if self._start_ts else 0.0

        slot_label = self._next_slot_label(matched_paths)
        progress_text = format_progress_text(matched, self._expected_slots, slot_label, elapsed)
        self._progress.set(matched, self._expected_slots, progress_text)
        self._render_preview_grid(matched_paths)

        if final or not self._polling_active:
            return
        if self._cancel_event.is_set() and (
            self._worker is None or not self._worker.is_alive()
        ):
            return
        # 워커가 살아있을 때만 폴링 지속
        if self._worker is not None and self._worker.is_alive():
            self.ui_after(
                lambda: self._poll_inbox(final=False),
                delay=int(_INBOX_POLL_INTERVAL_SEC * 1000),
            )

    def _collect_inbox_paths(self, slug: str) -> dict[str, Path]:
        """인박스에서 `{slug}_{slot}.png`를 슬롯명 키로 모은다."""
        result: dict[str, Path] = {}
        if not slug or not self._inbox_dir.exists():
            return result
        try:
            for child in self._inbox_dir.iterdir():
                if not child.is_file() or child.suffix.lower() != ".png":
                    continue
                name = child.stem
                if not name.startswith(f"{slug}_"):
                    continue
                slot = name[len(slug) + 1:]
                # thumb는 _processed로 이동될 수 있음 — 두 경로 모두 인정.
                result[slot] = child
        except OSError:
            return result

        # _processed 폴더도 추가 (matched 후 이동된 파일)
        processed = self._inbox_dir / "_processed"
        if processed.exists():
            try:
                for child in processed.iterdir():
                    if not child.is_file() or child.suffix.lower() != ".png":
                        continue
                    name = child.stem
                    if not name.startswith(f"{slug}_"):
                        continue
                    slot = name[len(slug) + 1:]
                    result.setdefault(slot, child)
            except OSError:
                pass
        return result

    @staticmethod
    def _next_slot_label(matched: dict[str, Path]) -> str:
        """다음 진행할 슬롯명 추정 (UI 안내용)."""
        order = ("thumb", "body1", "body2", "info1", "info2", "info3", "info4", "info5")
        for slot in order:
            if slot not in matched:
                return slot
        return ""

    def _render_preview_grid(self, matched_paths: dict[str, Path]) -> None:
        """6/8개 슬롯 그리드 갱신. 클릭 시 인박스 폴더 열림."""
        for child in self._preview_grid.winfo_children():
            child.destroy()
        self._preview_buttons.clear()

        slots: tuple[str, ...]
        if self._expected_slots >= EXPECTED_SLOTS_DIGITAL:
            slots = ("thumb", "body1", "body2", "info1", "info2", "info3", "info4", "info5")
            cols = 4
        else:
            slots = ("thumb", "body1", "body2", "info1", "info2", "info3")
            cols = 3

        for idx, slot in enumerate(slots):
            row, col = divmod(idx, cols)
            cell = Card(self._preview_grid)
            cell.grid(row=row, column=col, padx=px(6), pady=px(6), sticky="nw")
            inner = tk.Frame(cell, bg=C["card"], padx=px(6), pady=px(6))
            inner.pack(fill="both", expand=True)
            path = matched_paths.get(slot)
            img_widget = self._make_thumb_widget(inner, path)
            img_widget.pack()
            L(inner, slot, FS, C["accent"] if path else C["muted"]).pack(pady=(4, 0))

    def _make_thumb_widget(self, parent: tk.Misc, path: Optional[Path]) -> tk.Widget:
        """썸네일 이미지(또는 플레이스홀더) 위젯 생성."""
        if path is not None and path.exists() and _PILLOW_AVAILABLE:
            try:
                with Image.open(path) as raw:  # type: ignore[union-attr]
                    raw.thumbnail((px(_THUMB_PREVIEW_PX), px(_THUMB_PREVIEW_PX)))
                    photo = ImageTk.PhotoImage(raw)  # type: ignore[union-attr]
                self._thumb_refs.append(photo)
                btn = tk.Button(
                    parent,
                    image=photo,
                    bg=C["card"],
                    bd=0,
                    relief="flat",
                    cursor="hand2",
                    command=lambda p=path: self._open_path_in_explorer(p.parent),
                )
                self._preview_buttons.append(btn)
                return btn
            except (OSError, ValueError) as exc:
                log.debug("썸네일 로드 실패: %s", path, exc_info=True)
                # 폴백 — 텍스트 박스
                _ = exc

        # 미생성 / 폴백
        placeholder_icon = icons.glyph("image" if path is None else "warning")
        placeholder_font = icons.icon_font(22) or ("맑은 고딕", 22)
        return tk.Label(
            parent,
            text=placeholder_icon,
            width=int(px(_THUMB_PREVIEW_PX) / 8),
            height=int(px(_THUMB_PREVIEW_PX) / 18),
            bg=C["card"],
            fg=C["muted"],
            font=placeholder_font,
            relief="flat",
        )

    # ──────────────────────────────────────────────────────────────────────
    # 로그 + 폴더 열기
    # ──────────────────────────────────────────────────────────────────────

    def _clear_log(self) -> None:
        self._log_text.configure(state="normal")
        self._log_text.delete("1.0", "end")
        self._log_text.configure(state="disabled")

    def _log(self, message: str, tag: str = "info") -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{stamp}] {message}\n"
        self._log_text.configure(state="normal")
        self._log_text.insert("end", line, tag)
        self._log_text.see("end")
        self._log_text.configure(state="disabled")

    def _open_inbox_folder(self) -> None:
        self._open_path_in_explorer(self._inbox_dir)

    @staticmethod
    def _open_path_in_explorer(path: Path) -> None:
        """OS 파일 탐색기로 경로 열기."""
        try:
            path = Path(path)
            path.mkdir(parents=True, exist_ok=True)
            startfile = getattr(os, "startfile", None)
            if callable(startfile):
                startfile(str(path))
                return
            if sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except (OSError, subprocess.SubprocessError):
            log.debug("폴더 열기 실패: %s", path, exc_info=True)
