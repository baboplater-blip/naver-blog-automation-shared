# gui/tabs/home.py — 오늘 (UX P0, 2026-07-13 / GUI 전면 개편 2026-09-02 / v3 2026-09-03)
"""사용자의 하루가 시작되고 끝나는 화면. 규칙(사용감 계약):

1. 큰 버튼은 하나 — 누르기 전에 "무엇이 얼마나, 얼마 걸리는지"를 버튼이 먼저 말한다.
2. 신호등 3개 고정(발행 준비 · 시스템 · 어제 결과). 상태가 나쁘면 반드시
   "~하세요" 한 줄 + [해결] 버튼. 진단명만 던지고 끝내지 않는다.
3. 실행이 끝나면 요약 카드 자동 갱신 — 나간 글·쓴 보관본·안 나간 글과
   이유(사람 말). 조용한 실패 0.
수치·그래프·로그는 전부 기존 탭(대시보드·성장 관제탑·기록)에 그대로 —
여기는 결론과 다음 행동만 놓는다.

스레드 위생(2026-09-02 개편): `gui/async_utils.py` 의 `UiLifecycle` 믹스인을
쓴다. 워커 스레드는 Tk 위젯을 절대 직접 만지지 않고 `self.ui_after(...)` 로만
반영한다. `self.spawn(...)` 으로 띄운 워커는 `_threads` 에 핸들이 남는다.

P0 수리(신호등 "확인 중…" 영구 멈춤): `_load()` 가 어떤 이유로든(예외·
지연) `_apply(d)` 까지 도달하지 못하면, `refresh()` 가 함께 예약한 8초
타임아웃 가드(`_check_stuck`)가 아직 "확인 중…" 상태인 항목만 골라
"확인하지 못했어요 · [다시 확인]" 으로 바꾼다 — 버튼을 눌러 다시 시도할 수
있게(영구 멈춤 금지). 실제 결과가 늦게 도착하면 `_apply` 가 그 위에 정상
값을 덮어써 자연 치유된다.

v3 상용급 디자인(auto-dev 8호, 2026-09-03, `docs/auto-dev/gui-design-system-v3.md`):
이모지를 전부 걷어내고 Segoe Fluent Icons 글리프(`gui.icons.glyph`)로
바꿨다. 상태(성공/경고/실패)는 항상 "색(StatusPill kind) + 아이콘(글리프) +
단어" 3중으로 표시한다(§2-4). 발행 방식은 버튼 토글 대신 `Segmented`
2택으로, "더 할 일"은 이미지 채움·알아서 운영·발행 방식을 한 데 묶은
SectionCard 하나(기본 접힘)로 재구성했다. `widgets.SectionCard`/`Header`
의 `icon=` 은 이미 해석된 글리프 **문자**를 받고, `theme.PrimaryBtn` 류의
`icon=` 은 `gui.icons.ICONS` 의 **이름**을 받아 내부에서 해석한다 — 계약이
다르므로 헷갈리지 않게 각 호출부에 주석을 남겼다. 코더 A 가 widgets.py
내부를 동시에 CTk 로 옮기는 중이라, `icon=` 지원이 일시적으로 빠질 가능성에
대비해 `_section_card`/`_header` 헬퍼가 TypeError 시 icon 없이 재시도한다.
"""
from __future__ import annotations

import logging
import shutil
import threading
import time
import tkinter as tk
from tkinter import messagebox
from datetime import date, datetime, timedelta
from pathlib import Path

from config import safe_load_json
from gui import glossary, icons
from gui import theme as _theme
from gui.async_utils import UiLifecycle
from gui.humanize import (estimate_run_minutes, format_minutes,
                          summarize_log_entries)
from gui.theme import (C, F, FB, FH, FS, FT, SP, Card, DangerBtn, GhostBtn, L,
                        PrimaryBtn, SecondaryBtn, Sep, SubtleBtn)
from gui.widgets import (Banner, Header, IconButton, InlineConfirm, ProgressRow,
                         ScrollArea, SectionCard, Segmented, StatusPill)

log = logging.getLogger(__name__)

from config import profile as _nbaprofile
_DATA_DIR = _nbaprofile.app_data_root()


def _pa_badge_text() -> str:
    """활동 바 배지와 **같은 함수**로 "프로필 · 계정" 문자열을 얻는다
    (지연 임포트 — 순환 임포트 방지, 실패는 호출부가 폴백)."""
    from gui import profile_actions as _pa
    return _pa.profile_badge_text()

_WEEKDAYS = ["월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일"]
_LOADING = "확인 중…"
_LOADING_SUB = "실행 정보를 확인하는 중…"

# 상태(kind) → 아이콘 이름(§2-4 상태색 의미 규칙). neutral 은 아이콘 없음.
_KIND_ICON_NAME = {"ok": "success", "warn": "warning", "error": "close"}

# 발행 방식 Segmented 라벨 ↔ 내부 모드 값(§8-2 마이크로카피 예시와 동일 문구).
_PUB_MODE_LABEL = {
    "spawn": glossary.label("publish_spawn"),
    "attach": glossary.label("publish_attach"),
}
_PUB_LABEL_TO_MODE = {v: k for k, v in _PUB_MODE_LABEL.items()}

# 세션 상태 주기 폴링 간격(ms)·로그인 판정 캐시 유효기간(초). attach 세션
# 브라우저에 Selenium 으로 붙어 로그인 계정을 읽는 작업은 chromedriver 를
# 기동하는 비용이 있어(수백ms~수초) 5초마다 다시 하면 안 된다 — 포트 프로브
# (urllib 요청 하나, 가벼움)는 매 폴링마다, 로그인 판정은 캐시가 만료됐을
# 때만 새로 한다(대시보드 수정 A, 2026-09-10).
_SESSION_POLL_MS = 5000
_SESSION_AUTH_CACHE_S = 60.0


def px(n: int) -> int:
    """고DPI 스케일 헬퍼(9호 Phase 1) — 코더 A 가 `theme.px(n)`(NBA_UI_SCALE
    반영)을 만드는 중이라, 있으면 그걸 쓰고 없으면(아직 없거나 시그니처가
    다르면) 항등 함수로 폴백한다. 하드코딩 px 값(카드 padx/pady·wraplength·
    간격 보정치)은 전부 이 함수를 거친다 — SP 토큰 자체도 코더 A 가 자동
    확대하는 중이라 SP[...] 는 그대로 쓰고, SP 에 없는 순수 숫자만 여기로."""
    fn = getattr(_theme, "px", None)
    if callable(fn):
        try:
            return fn(n)
        except Exception:  # noqa: BLE001 — px() 구현이 아직 불안정해도 화면은 떠야 한다
            return n
    return n


def _iconed(kind: str, label: str) -> str:
    """상태 라벨 앞에 아이콘 폰트 글리프를 붙인다(이모지 대체, v3 §2-4).

    아이콘 폰트가 없는 환경이면 `icons.glyph()` 가 "•" 로 폴백하므로 항상
    안전하다. neutral 상태는 아이콘 없이 단어만(활성 상태와 시각적으로
    구분하기 위해 의도적으로 비워 둠).
    """
    name = _KIND_ICON_NAME.get(kind)
    if not name:
        return label
    g = icons.glyph(name)
    return f"{g} {label}" if g else label


def _section_card(parent, title: str, subtitle=None, icon=None, **kw):
    """SectionCard 생성 방어 래퍼 — 코더 A 가 widgets.py 를 CTk 로 옮기는
    동안 `icon=` 지원이 일시적으로 빠지면(TypeError) 아이콘 없이 재시도한다."""
    try:
        return SectionCard(parent, title, subtitle=subtitle, icon=icon, **kw)
    except TypeError:
        return SectionCard(parent, title, subtitle=subtitle, **kw)


def _header(parent, title: str, subtitle=None, icon=None, **kw):
    """Header 생성 방어 래퍼 — 위와 동일한 이유."""
    try:
        return Header(parent, title, subtitle=subtitle, icon=icon, **kw)
    except TypeError:
        return Header(parent, title, subtitle=subtitle, **kw)


def _hero_primary_btn(parent, text, cmd, icon=None):
    """히어로 CTA 버튼 — 코더 A 가 `PrimaryBtn(..., size="lg")`(높이 44·폰트
    14)를 추가하는 중이라 있으면 쓰고, 없으면(TypeError) 기존 크기로 재시도
    한다(수정 라운드 🔴1)."""
    try:
        return PrimaryBtn(parent, text, cmd, icon=icon, size="lg")
    except TypeError:
        return PrimaryBtn(parent, text, cmd, icon=icon)


def _wrap_for_width(width: int, extra_pad: int | None = None) -> int:
    """카드 폭(px)에서 좌우 여백을 뺀 wraplength(px). 최소 80px(스케일 반영)로
    바닥을 둔다.

    Tk 없이도 단위 테스트 가능한 순수 함수 — `_bind_dynamic_wrap` 의 계산
    로직만 분리했다(수정 라운드 🔴1). `extra_pad` 기본값은 **호출 시점에**
    `px(28)` 로 해석한다(9호 Phase 1) — 함수 정의의 기본 인자로 박아두면
    모듈 최초 import 시점의 스케일에 영구히 고정돼, 그 뒤 스케일이 바뀌어도
    (예: 테스트가 NBA_UI_SCALE 을 바꾸고 모듈을 리로드하는 경우) 반영이
    안 된다.
    """
    if extra_pad is None:
        extra_pad = px(28)
    return max(px(80), int(width) - extra_pad)


_IMAGE_FILL_ENGINE_LABEL = {"codex": "Codex", "genspark": "젠스파크"}


# 홈의 "이미지 채움"은 이미지가 **하나도 없는** 글뿐 아니라 목표 장수에 **못 미치는**
# 글도 대상으로 삼는다(부분 보충). 대상 카운트와 실제 배치 실행이 **같은 규칙**을 쓰도록
# 두 호출 모두 이 상수 하나만 본다 — 한쪽만 바뀌어 안내 숫자와 처리 편수가 어긋나던
# 사고 클래스(계약 숫자가 흩어져 한쪽만 고쳐짐)를 막는다.
_FILL_TOPUP = True


def _image_fill_engine_label() -> str:
    """현재 선택된 이미지 채움 엔진의 사람 말 이름(안내 문구용). 모르는 값은
    "Codex"(기본 엔진)로 안전하게 수렴한다."""
    try:
        from config import settings as _s
        name = str(getattr(_s, "IMAGE_FILL_ENGINE", "codex") or "codex").strip().lower()
    except Exception:  # noqa: BLE001 — settings 미로드는 기본 엔진 표기
        name = "codex"
    return _IMAGE_FILL_ENGINE_LABEL.get(name, "Codex")


def _image_fill_enabled_setting() -> bool:
    """이미지 자동 채움 마스터 스위치.

    단일 진실 원본은 ``core.draft_image_fill.image_fill_gate()`` 다(2026-09-09
    ``core/blog_service.py`` 신설 — 이 함수가 게이트 판정 로직을 손으로 복제해
    두 곳이 어긋나던 사고 클래스를 없앤다, `.auto-dev/blog-service-plan.md` D7).
    ``blog_service.image_fill_status()`` 가 그 게이트에 위임하며, 게이트가 아직
    배선되지 않은 설치(짝을 이루는 core 작업이 아직 반영되지 않은 상태)에서는
    ``reason=="gate_unavailable"`` 로 안전 수렴해 반환한다. 그 경우에만 이 함수가
    예전 우선순위 규칙(새 키 우선, 없으면 ``GENSPARK_ENABLED``)으로 임시 보강한다
    — 게이트가 배선되는 순간 이 보강 분기는 더 이상 실행되지 않는다."""
    try:
        from core import blog_service
        status = blog_service.image_fill_status()
        if status.get("reason") != "gate_unavailable":
            return bool(status.get("enabled"))
    except Exception:  # noqa: BLE001 — blog_service 미로드도 아래 폴백으로 계속
        pass
    try:
        from config import settings as _s
    except Exception:  # noqa: BLE001 — settings 미로드는 '꺼짐'으로 간주
        return False
    value = getattr(_s, "IMAGE_FILL_ENABLED", None)
    if value is None:
        return bool(getattr(_s, "GENSPARK_ENABLED", False))
    return bool(value)


# 정렬 근거가 "오래됐다"고 알릴 기준(분). 이보다 최근이면 아무 말도 하지
# 않는다 — 방금 계산했다는 사실은 사용자가 할 일이 없는 정보다.
_STALE_SORT_MINUTES = 60


def _short_issue(message: str) -> str:
    """신호등 칸에 들어갈 짧은 형태 — 첫 괄호 앞까지만.

    Tk 없이 테스트 가능한 순수 함수. 괄호가 없으면 원문 그대로다(정보를
    지어내거나 잘라 없애지 않는다 — 전문은 "지금 다루는 블로그" 카드가
    그대로 보여준다).
    """
    text = str(message or "").strip()
    head = text.split(" (", 1)[0].strip()
    return head or text


def _format_cache_age_hint(age_min: int | None) -> str:
    """정렬 캐시(core.queue_sort_cache) 나이(분)를 발행 순서 한 줄 끝에 붙일
    짧은 힌트로 바꾼다. Tk 없이도 단위 테스트 가능한 순수 함수.

    v4(2026-09-21): 예전엔 항상 "캐시 방금 계산"/"캐시 N분 전 계산" 을 붙여
    첫 화면에 내부 구현 용어(캐시)를 상시 노출했다. 이제 **오래됐을 때만**,
    그것도 사람 말로 알린다(최신이면 빈 문자열).
    """
    if age_min is None or age_min < _STALE_SORT_MINUTES:
        return ""
    hours = age_min // 60
    return f" · 순서 기준이 {hours}시간 전 값이에요"


def light_wrap_pad() -> int:
    """신호등 3열의 wraplength 여백(px).

    v4(2026-09-21) 로 신호등이 "카드 3장"에서 "카드 하나 안의 3열"로 바뀌며
    열 자체의 안쪽 여백이 거의 없어졌다(예전 카드 padx 14 × 2 → 0). 코드와
    테스트가 같은 값을 보도록 함수로 노출한다 — 숫자가 두 곳에 흩어져
    한쪽만 고쳐지는 이 저장소의 단골 사고를 막는다. 배율이 바뀔 수 있으므로
    상수가 아니라 **호출 시점에** 계산한다.
    """
    return px(4)


def _bind_dynamic_wrap(widget, label, extra_pad: int | None = None) -> None:
    """widget(카드) 폭이 바뀔 때마다 label 의 wraplength 를 실측 폭에 맞춰
    갱신한다. 고정 wraplength 는 900×620 처럼 좁은 창에서 신호등 카드가
    3분할될 때 문구가 좌우로 잘리는 원인이었다(수정 라운드 🔴1).

    v4(2026-09-21): ``label`` 에 **여러 라벨(리스트/튜플)** 을 줄 수 있다 —
    같은 컨테이너의 여러 줄이 다 같이 접혀야 하는데, Tk 의 ``bind`` 는
    같은 이벤트에 두 번 걸면 앞의 콜백을 덮어써서(add="+" 없이) 한 줄만
    반응하던 문제가 있었다(900×620 에서 히어로 부제 아래 두 줄이 오른쪽
    으로 잘림 — 중간 캡처 실측).
    """
    labels = list(label) if isinstance(label, (list, tuple)) else [label]

    def _on_configure(event):
        new_wrap = _wrap_for_width(event.width, extra_pad)
        for lbl in labels:
            try:
                if lbl.cget("wraplength") != new_wrap:
                    lbl.config(wraplength=new_wrap)
            except tk.TclError:
                continue
    widget.bind("<Configure>", _on_configure)


class HomeTab(UiLifecycle, tk.Frame):
    def __init__(self, parent, app):
        tk.Frame.__init__(self, parent, bg=C["bg"])
        self._init_lifecycle()
        self.app = app
        self._fill_running = False       # 이미지 채움 배치 재진입 방지
        self._load_seq = 0               # refresh() 세대 번호 — 낡은 타임아웃 무시
        self._pending_queue = False      # "발행 준비"(느린 preflight) 항목 대기 여부
        self._setup_banner = None
        self._more_expanded = False      # v3: "더 할 일"은 기본 접힘(설계서 §7-2)
        self._build_ui()
        self._init_bus()
        self.ui_after(self.refresh, delay=300)

    def destroy(self):
        bus = getattr(self.app, "bus", None)
        if bus is not None and hasattr(bus, "unsubscribe"):
            try:
                bus.unsubscribe(self)
            except Exception:  # noqa: BLE001 — 구독 해제 실패는 파괴를 막지 않는다
                pass
        super().destroy()

    # ─────────────────────────────────────────────
    # UI 골격
    # ─────────────────────────────────────────────
    def _build_ui(self):
        t = date.today()
        hdr = _header(self, "오늘",
                      f"{t.strftime('%Y년 %m월 %d일')} {_WEEKDAYS[t.weekday()]}",
                      icon=icons.glyph("home"))
        hdr.pack(fill="x", padx=SP["xl"], pady=(SP["md"], 0))
        IconButton(hdr.actions, "refresh", self.refresh,
                   tooltip="새로고침").pack(side="right")

        # 본문은 스크롤 영역으로 — 900×620 처럼 낮은 창에서 "더 할 일" 섹션이
        # 화면 밖으로 잘리는 문제(수정 라운드 🟡4). 제목·구분선만 고정.
        scroll = ScrollArea(self)
        scroll.pack(fill="both", expand=True)
        body = scroll.inner

        # ── 설정 미완/로그인 풀림 배너 자리 (기본 숨김, show_setup_banner 가 채움) ──
        self._banner_slot = tk.Frame(body, bg=C["bg"])
        self._banner_slot.pack(fill="x")

        # ── ① 지금 어느 블로그를 다루고 있는가(v4 신설) ─────────────────
        # 첫 화면에서 가장 먼저 확인해야 할 사실인데 예전엔 창 맨 아래 활동
        # 바 구석의 작은 배지 하나가 전부였다(개편 전 캡처 실증 — 잘못된
        # 계정으로 발행하는 사고의 입구).
        self._build_blog_card(body)

        # ── ② 히어로 — 오늘 할 일 요약 + 주 행동 1개 ────────────────────
        # v4: 예전엔 카드 폭을 꽉 채운 파란 버튼 하나가 화면 위쪽을 통째로
        # 차지하고 그 아래가 빈 공간이었다. 이제 "무엇을 얼마나" 는 왼쪽
        # 글줄이 말하고, 버튼은 오른쪽에 자연 폭으로 둔다.
        hero = Card(body, padx=SP["xl"], pady=SP["md"])
        hero.pack(fill="x", padx=SP["xl"], pady=(SP["sm"], SP["sm"]))
        hero_row = tk.Frame(hero, bg=C["card"])
        hero_row.pack(fill="x")

        btns = tk.Frame(hero_row, bg=C["card"])
        btns.pack(side="right", padx=(SP["lg"], 0))
        # PrimaryBtn 의 icon= 은 gui.icons.ICONS 의 "이름"(내부에서 glyph 해석) —
        # SectionCard/Header 의 icon=(이미 해석된 문자)과 계약이 다르다.
        self.run_btn = _hero_primary_btn(btns, "오늘 실행", self.app.run_today, icon="run")
        self.run_btn.pack(side="top", anchor="e")
        # FakeApp(테스트 계약)엔 run_test 가 없을 수 있어 getattr 로 방어한다.
        run_test = getattr(self.app, "run_test", None)
        if callable(run_test):
            SubtleBtn(btns, "테스트 실행", run_test, icon="test").pack(
                side="top", anchor="e", pady=(SP["xs"], 0))

        texts = tk.Frame(hero_row, bg=C["card"])
        texts.pack(side="left", fill="both", expand=True)
        L(texts, "오늘 할 일", FS, C["muted"]).pack(anchor="w")
        self.run_sub = L(texts, _LOADING_SUB, FT, C["text"], justify="left",
                         anchor="w")
        self.run_sub.pack(anchor="w", fill="x", pady=(px(2), 0))
        # 발행 순서 근거 한 줄 — "무슨 기준으로 이 순서인가"를 사용자가 볼 수 있게
        # (auto_scheduler._sort_by_trend_priority 가 남긴 queue_sort_rationale.json)
        self.sort_hint = L(texts, "", FS, C["muted"], anchor="w", justify="left")
        self.sort_hint.pack(anchor="w", fill="x", pady=(SP["xs"], 0))
        # 뉴스 선행지표 한 줄 — "오늘 뜨는 주제"(news_radar 캐시, 신호 없으면 숨김)
        self.news_hint = L(texts, "", FS, C["muted"], anchor="w", justify="left")
        self.news_hint.pack(anchor="w", fill="x")
        _bind_dynamic_wrap(texts, [self.run_sub, self.sort_hint, self.news_hint],
                           extra_pad=px(8))

        # ── ③ 저장 글(이미지 준비 현황) + 이미지 채움 ──────────────────
        # 위젯·콜백은 `_build_fill_section` 그대로 재사용한다(실행 경로·대상
        # 카운트 규칙 100% 동일, 버튼 복제 없음). v4 에서 "지금 바로 나갈 수
        # 있는 글이 몇 편인가" 한 줄이 위에 붙었다.
        fill_card = Card(body, padx=SP["lg"], pady=SP["md"])
        fill_card.pack(fill="x", padx=SP["xl"], pady=(0, SP["sm"]))
        self._build_fill_section(fill_card)

        # ── ④ 신호등 3개 ──
        # v4: 예전엔 카드 3장을 나란히 놓아 카드 테두리·여백이 세 번 겹치며
        # 1080×720 에서 세 번째 줄이 화면 밖으로 밀렸다. 이제 **카드 하나
        # 안의 3열**로 묶는다(세로 ~40px 절약, 위젯 계약은 그대로).
        lights_card = Card(body, padx=SP["lg"], pady=SP["md"])
        lights_card.pack(fill="x", padx=SP["xl"], pady=(0, SP["sm"]))
        lf = tk.Frame(lights_card, bg=C["card"])
        lf.pack(fill="x")
        lf.columnconfigure((0, 1, 2), weight=1, uniform="light")
        self._lights = {}
        for i, (key, title) in enumerate(
                [("queue", "발행 준비"), ("system", "시스템"), ("yesterday", "어제 결과")]):
            col = tk.Frame(lf, bg=C["card"])
            col.grid(row=0, column=i, sticky="nsew",
                     padx=(0 if i == 0 else SP["lg"], 0))
            L(col, title, FS, C["muted"]).pack(anchor="w")
            state = StatusPill(col, _LOADING, kind="neutral")
            state.pack(anchor="w", pady=(SP["xs"], 0))
            detail = L(col, "", FS, C["text_2"], anchor="w", justify="left",
                      wraplength=px(230))
            detail.pack(anchor="w", fill="x", pady=(SP["xs"], 0))
            _bind_dynamic_wrap(col, detail, extra_pad=light_wrap_pad())
            # action 은 조치가 있을 때만 _set_light 가 pack 한다(§3.1) —
            # 상태 정상일 때 내용 없는 상자가 보이던 문제(수정 라운드 🟡2).
            action = F(col, bg=C["card"])
            self._lights[key] = {"state": state, "detail": detail, "action": action}

        # ── 실행 요약 카드 ──
        self.summary_card = _section_card(body, "오늘 요약", icon=icons.glyph("info"))
        self.summary_card.pack(fill="x", padx=SP["xl"], pady=(SP["md"], SP["sm"]))
        self.summary_body = L(self.summary_card.body, "아직 오늘 실행 전이에요.",
                              FB, C["text_2"], justify="left", wraplength=px(760))
        self.summary_body.pack(anchor="w")

        # ── 더 할 일 (기본 접힘, v3 §7-2) — 알아서 운영 · 발행 방식을 한
        # SectionCard 로 묶는다(이미지 채움은 위로 승격돼 여기서 빠졌다,
        # 수정 대상 C). ──
        # 접힘 상태의 토글 행도 SectionCard 헤더처럼 보이게(아이콘 chevron
        # IconButton + F_SECTION 제목) — 작은 텍스트 한 줄로만 보이던 문제
        # (수정 라운드 🟡3). 카드 자체를 클릭 대상으로 삼는다.
        toggle_card = Card(body, padx=SP["lg"], pady=SP["md"])
        toggle_card.pack(fill="x", padx=SP["xl"], pady=(0, SP["sm"]))
        toggle_row = tk.Frame(toggle_card, bg=C["card"])
        toggle_row.pack(fill="x")
        self._more_chevron_btn = IconButton(
            toggle_row, "collapse" if self._more_expanded else "expand",
            self._toggle_more, tooltip="더 할 일 펼치기/접기")
        self._more_chevron_btn.pack(side="left")
        more_title_lbl = L(toggle_row, "더 할 일", FT, C["text"])
        more_title_lbl.pack(side="left", padx=(SP["xs"], 0))
        more_title_lbl.bind("<Button-1>", lambda _e: self._toggle_more())
        self._more_frame = tk.Frame(body, bg=C["bg"])
        if self._more_expanded:
            self._more_frame.pack(fill="x")

        # 제목은 위 toggle_card 헤더가 이미 담당하므로 여기는 본문 카드만
        # (SectionCard 로 또 감싸면 "더 할 일" 제목이 중복된다). 이미지 채움은
        # 더 이상 여기 없다 — 첫 화면으로 승격됐다(위 fill_card, 수정 대상 C).
        more_card = Card(self._more_frame, padx=SP["lg"], pady=SP["md"])
        more_card.pack(fill="x", padx=SP["xl"], pady=(0, SP["lg"]))
        self._build_auto_section(more_card)
        Sep(more_card).pack(fill="x", pady=SP["md"])
        self._build_publish_mode_section(more_card)

        # ── 자세히 보기 ──
        bf = F(body, padx=SP["xl"], pady=SP["sm"])
        bf.pack(fill="x")
        L(bf, "자세히 보기:", FS, C["muted"]).pack(side="left", padx=(0, SP["sm"]))
        SecondaryBtn(bf, "대시보드", lambda: self.app.show_tab("dashboard"),
                    icon="stats").pack(side="left", padx=(0, SP["sm"]))
        SecondaryBtn(bf, "성장 관제탑", lambda: self.app.show_tab("growth"),
                    icon="growth").pack(side="left", padx=(0, SP["sm"]))
        SecondaryBtn(bf, "기록", lambda: self.app.show_tab("logs"),
                    icon="log").pack(side="left", padx=(0, SP["sm"]))
        IconButton(bf, "refresh", self.refresh, tooltip="새로고침").pack(side="right")

    # ── ① 현재 블로그 카드(v4 신설) ────────────────────────────────
    def _build_blog_card(self, parent) -> None:
        """"지금 어느 블로그에, 어떤 방식으로 발행하는가" 한 줄.

        값 자체는 전부 기존 소스 그대로다 — 프로필/계정은
        ``gui.profile_actions.profile_badge_text()``(활동 바 배지와 같은 함수),
        발행 방식은 ``app.cfg["publish_mode"]``, 로그인·세션 준비 상태는
        기존 세션 프로브 결과(``_render_session_status``)를 재사용한다.
        여기서 새로 조회하거나 판정하지 않는다(네트워크 0).
        """
        card = Card(parent, padx=SP["lg"], pady=SP["md"])
        card.pack(fill="x", padx=SP["xl"], pady=(SP["sm"], 0))
        row = tk.Frame(card, bg=C["card"])
        row.pack(fill="x")

        left = tk.Frame(row, bg=C["card"])
        left.pack(side="left", fill="x", expand=True)
        L(left, "지금 다루는 블로그", FS, C["muted"]).pack(anchor="w")
        try:
            badge = _theme.strip_emoji(_pa_badge_text())
        except Exception:  # noqa: BLE001 — 배지 실패는 화면을 막지 않는다
            badge = "default"
        self.blog_name_lbl = L(left, badge, FT, C["text"], anchor="w")
        self.blog_name_lbl.pack(anchor="w", pady=(px(2), 0))

        right = tk.Frame(row, bg=C["card"])
        right.pack(side="right")
        SecondaryBtn(right, "블로그 바꾸기",
                     lambda: self.app.show_tab("profiles"), icon="profile").pack(
            side="right", padx=(SP["sm"], 0))
        self.ready_pill = StatusPill(right, _LOADING, kind="neutral")
        self.ready_pill.pack(side="right")

        # 이 한 줄은 "지금 무엇을 해야 하는가"(로그인 만료 등)를 말하는 문장이라
        # 보조 회색(muted)이 아니라 2차 본문색(text_2)으로 둔다 — 2026-09-22
        # 가독성 점검.
        self.ready_detail = L(card, "", FS, C["text_2"], anchor="w", justify="left")
        self.ready_detail.pack(anchor="w", fill="x", pady=(SP["xs"], 0))
        _bind_dynamic_wrap(card, self.ready_detail,
                           extra_pad=SP["lg"] * 2 + px(8))

    def _set_ready(self, kind: str, label: str, detail: str) -> None:
        """현재 블로그 카드의 준비 상태 알약 + 한 줄 설명을 갱신한다."""
        try:
            self.ready_pill.set(label, kind)
            self.ready_detail.config(text=detail)
        except (tk.TclError, AttributeError):
            pass

    def _toggle_more(self):
        self._more_expanded = not self._more_expanded
        try:
            # IconButton 은 HAS_CTK 면 순정 CTkButton 을 그대로 돌려준다(어댑터로
            # 안 감쌈) — CTk 위젯은 `.config()` 가 막혀 있어 `.configure()` 를
            # 써야 한다(tk.Button 도 동일 메서드를 별칭으로 지원해 양쪽 호환).
            if self._more_expanded:
                self._more_frame.pack(fill="x")
                self._more_chevron_btn.configure(text=icons.glyph("collapse"))
            else:
                self._more_frame.pack_forget()
                self._more_chevron_btn.configure(text=icons.glyph("expand"))
        except tk.TclError:
            pass

    # ── 설정 미완/로그인 풀림 배너 (코더 B 의 _env_check 가 호출) ──
    def show_setup_banner(self, text: str) -> None:
        def _do():
            try:
                if self._setup_banner is not None:
                    self._setup_banner.destroy()
                    self._setup_banner = None
                self._setup_banner = Banner(
                    self._banner_slot, text, kind="warn",
                    action_text="설정 열기",
                    action_cmd=lambda: self.app.show_tab("settings"))
                self._setup_banner.pack(fill="x", padx=SP["xl"], pady=(SP["sm"], 0))
            except tk.TclError:
                pass
        self.ui_after(_do)

    # ─────────────────────────────────────────────
    # 실행 중 상태 반영 — RunBus 구독(있으면) / 2초 폴링(없으면)
    # ─────────────────────────────────────────────
    def _init_bus(self):
        bus = getattr(self.app, "bus", None)
        if bus is not None and hasattr(bus, "subscribe"):
            bus.subscribe(self)
        else:
            self._poll_running_state()

    def on_run_state(self, running: bool, label: str = "") -> None:
        """RunBus 콜백 — 워커 스레드에서 와도 안전(버스가 이미 메인으로 재위임)."""
        self._apply_run_button_state(bool(running))

    def _apply_run_button_state(self, running: bool) -> None:
        try:
            if running:
                self.run_btn.config(
                    text="실행 중… (포스팅 탭에서 진행 확인 · 중지 가능)",
                    state="disabled")
            else:
                if self.run_btn["state"] == "disabled":
                    self.run_btn.config(text="오늘 실행", state="normal")
                    self.refresh()   # 실행이 막 끝났으면 요약·신호등 갱신
        except tk.TclError:
            pass

    def _poll_running_state(self):
        """bus 가 없는 FakeApp/구버전 App 대비 폴백 — 2초 간격 상태 확인."""
        try:
            self._apply_run_button_state(bool(getattr(self.app, "_running", False)))
        except tk.TclError:
            return
        self.ui_after(self._poll_running_state, delay=2000)

    # ─────────────────────────────────────────────
    # 데이터 적재 (백그라운드) — 빠른 항목 먼저 _apply, 느린 항목(발행 준비=
    # preflight)은 나중에 도착 · 8초 타임아웃 가드는 느린 항목에만 건다
    # (수정 라운드 🟡3 — preflight 가 대형 큐에서 실측 ~19초 걸려 신호등
    # 전체가 불필요하게 오래 "확인 중…"에 머물던 문제).
    # ─────────────────────────────────────────────
    def refresh(self):
        self._load_seq += 1
        seq = self._load_seq
        self._pending_queue = True
        self.spawn(self._load)
        self.ui_after(self._check_stuck, seq, delay=8000)

    def _check_stuck(self, seq: int) -> None:
        """8초가 지나도 '발행 준비'(느린 preflight) 의 _apply() 가 안 왔으면
        '확인 못함'으로 전환. 시스템·어제 결과·자동운영·요약은 로컬 파일
        I/O 수준이라 별도 타임아웃 가드 없이 빠르게 도착한다."""
        if seq != self._load_seq or not self._pending_queue:
            return
        self._mark_queue_stuck()

    def _mark_queue_stuck(self) -> None:
        try:
            lt = self._lights["queue"]
            if lt["state"]["text"] in (_LOADING, ""):
                lt["state"].set("확인하지 못했어요", "warn")
                lt["detail"].config(text="다시 확인해 보세요.")
                for w in lt["action"].winfo_children():
                    w.destroy()
                # icon= 은 여기서 쓰지 않는다 — CTk 아이콘 버튼은 내부적으로
                # CTkFrame(아이콘+버튼)로 감싸져 `winfo_children()[0].invoke()`
                # 로 직접 클릭하는 기존 계약(`tests/test_home_ux.py`)이 깨진다.
                SecondaryBtn(lt["action"], "다시 확인", self.refresh)\
                    .pack(anchor="w", pady=(SP["xs"] + px(2), 0))
                lt["action"].pack(anchor="w", fill="x")
            if self.run_sub["text"] in (_LOADING_SUB, ""):
                self.run_sub.config(text="확인하지 못했어요 · 다시 확인해 보세요")
        except tk.TclError:
            pass

    # ─────────────────────────────────────────────
    # 발행 방식 (P0-①) — spawn(무인) ↔ attach(내 브라우저로 이어서), v3: Segmented 2택
    # ─────────────────────────────────────────────
    def _build_publish_mode_section(self, parent):
        L(parent, "발행 방식", FT, C["text"]).pack(anchor="w")
        self._pub_desc = L(parent, "", FS, C["text_2"], justify="left",
                           wraplength=px(680))
        self._pub_desc.pack(anchor="w", pady=(SP["xs"], SP["sm"]))
        self._pub_segmented = Segmented(
            parent, [_PUB_MODE_LABEL["spawn"], _PUB_MODE_LABEL["attach"]],
            command=self._on_publish_mode_change)
        self._pub_segmented.pack(anchor="w")
        prow = tk.Frame(parent, bg=C["card"])
        prow.pack(fill="x", pady=(SP["sm"], 0))
        self._pub_session_btn = SecondaryBtn(
            prow, "발행 세션 열기", self._open_publish_session, icon="folder")
        self._pub_session_btn.pack(side="left")
        self._pub_session_status = L(prow, "", FS, C["muted"])
        self._pub_session_status.pack(side="left", padx=(SP["sm"], 0))
        self._render_publish_mode()
        # 세션 상태(포트+로그인)는 네트워크 프로브 — 구성 시점이 아니라 살아있는
        # 앱에서, 그리고 사용자가 나중에 그 창에서 로그인해도 반영되도록 주기
        # 폴링으로(대시보드 수정 A). 재진입 방지 플래그·캐시는 여기서 초기화.
        self._session_auth_cache = {"ts": 0.0, "verdict": None}
        self._session_probe_inflight = False
        self._session_open_inflight = False
        self.ui_after(self._session_poll_tick, delay=700)

    def _current_publish_mode(self) -> str:
        mode = str(self.app.cfg.get("publish_mode", "spawn")).lower()
        return "attach" if mode == "attach" else "spawn"

    def _render_publish_mode(self):
        """Segmented 선택·설명 라벨 갱신(메인스레드). 네트워크 프로브는 하지 않는다."""
        try:
            mode = self._current_publish_mode()
            self._pub_segmented.set(_PUB_MODE_LABEL[mode])
            if mode == "attach":
                self._pub_desc.config(
                    text=glossary.help("publish_attach")
                         + " 낮에 앉아서 검수 발행할 때 권장해요"
                           "(무인 자동 발행은 '아침 자동 발행'이 담당해요).")
                self._pub_session_btn.config(state="normal")
            else:
                self._pub_desc.config(text=glossary.help("publish_spawn"))
                self._pub_session_btn.config(state="disabled")
                self._pub_session_status.config(text="", fg=C["muted"])
        except tk.TclError:
            return

    def _session_poll_tick(self) -> None:
        """세션 상태 주기 폴링(5초 간격, attach 모드일 때만 프로브).

        `ui_after` 로만 스스로를 재예약하므로, 탭이 파괴되면(`_destroyed`)
        `ui_after` 가 예약을 거부해 폴링이 자연히 멈춘다(UiLifecycle 규약 —
        별도 타이머 정리 코드가 필요 없다).
        """
        try:
            if self._current_publish_mode() == "attach":
                self._trigger_session_probe()
        except tk.TclError:
            return
        self.ui_after(self._session_poll_tick, delay=_SESSION_POLL_MS)

    def _trigger_session_probe(self, force: bool = False) -> None:
        """세션 프로브를 백그라운드로 착수한다 — 이전 프로브가 진행 중이면 건너뛴다
        (중복 poll 방지). `force=True` 는 로그인 판정 캐시를 무시하고 다시 확인한다
        (세션 열기 직후·모드 전환 직후처럼 즉시 최신값이 필요할 때)."""
        if self._session_probe_inflight:
            return
        self._session_probe_inflight = True
        self.spawn(self._probe_session_worker, force)

    def _probe_session_worker(self, force: bool) -> None:
        """세션 포트+로그인 상태 확인(백그라운드).

        포트 프로브(가벼움)는 매번 하고, 로그인 판정(attach_to_session 이
        chromedriver 를 기동해 비쌈)은 ①포트가 열려 있고 ②발행이 진행 중이
        아니고 ③캐시가 만료됐을 때만 새로 한다. 발행 중에는 마지막 값을
        그대로 둔다(app 의 실행 상태 존중)."""
        from core import chrome_session
        from config import settings as s
        port = int(getattr(s, "CHROME_ATTACH_PORT", 9222))

        port_state = "unknown"
        try:
            status = chrome_session.session_status(port)
            port_state = status.get("state", "unknown")
        except Exception as e:  # noqa: BLE001 — 프로브 실패는 unknown(닫힘 단정 금지)
            log.debug(f"[home] 세션 포트 프로브 실패: {type(e).__name__}")

        auth_verdict = None
        checked = False
        if port_state == "open" and not getattr(self.app, "_running", False):
            cache = self._session_auth_cache
            age = time.time() - float(cache.get("ts", 0.0) or 0.0)
            if force or cache.get("verdict") is None or age >= _SESSION_AUTH_CACHE_S:
                auth_verdict = self._check_session_auth(port)
                checked = True

        self.ui_after(self._apply_session_probe_result, port_state, auth_verdict, checked)

    def _check_session_auth(self, port: int) -> str:
        """세션 크롬의 네이버 로그인 상태를 판정한다(읽기 전용).

        기존 구현을 그대로 재사용한다: `attach_to_session` → 로그인 blogId
        판독 → `verify_publish_identity` verdict. 페이지 이동·탭 조작·
        로그아웃·`quit()` 은 하지 않는다 — attach 는 새 창을 열지 않고 기존
        창에 Selenium 으로 붙기만 하고, 해제는 파이프라인과 같은 규약
        (`_release_driver(driver, "attach")` — attach 는 quit 생략)을 쓴다.
        다만 이 경로는 유휴 중에도 수 초마다 반복되므로 두 호출 모두
        `quiet=True` 로 **성공 안내 로그만** 끈다(실패·경고는 그대로 남는다).

        반환은 verdict 문자열만('match'|'mismatch'|'unknown') — reason 문구엔
        실제 로그인 계정 ID 가 들어있어(예: "불일치: 로그인=x ≠ 대상=y")
        화면·로그로 옮기지 않는다(자격증명·계정 값 비노출 원칙).
        """
        driver = None
        try:
            from core import naver_uploader as _nu
            from config import settings as s
            driver = _nu.attach_to_session(port, quiet=True)
            if driver is None:
                return "unknown"
            logged_in = _nu.read_logged_in_blog_id(driver)
            intended = getattr(s, "NAVER_ID", "") or ""
            current_url = ""
            try:
                current_url = driver.current_url or ""
            except Exception:  # noqa: BLE001 — url 판독 실패는 판정에 영향 없음
                pass
            _ok, _reason, verdict = _nu.verify_publish_identity(
                intended, logged_in, current_url)
            return verdict
        except Exception as e:  # noqa: BLE001 — attach/판독 실패는 unknown 으로 수렴
            log.debug(f"[home] 세션 인증 확인 실패: {type(e).__name__}")
            return "unknown"
        finally:
            if driver is not None:
                try:
                    from scheduler.auto_scheduler import _release_driver
                    _release_driver(driver, "attach", quiet=True)
                except Exception:  # noqa: BLE001 — 해제 실패는 무해(세션 브라우저 생존)
                    pass

    def _apply_session_probe_result(self, port_state: str, auth_verdict, checked: bool) -> None:
        """프로브 결과 반영(메인 스레드). 포트가 안 열려 있으면 로그인 캐시도
        비운다 — 사용자가 세션 창을 닫았다가 다른 계정으로 다시 열어도 옛
        판정이 남지 않게(요건 d)."""
        self._session_probe_inflight = False
        try:
            if port_state != "open":
                self._session_auth_cache = {"ts": 0.0, "verdict": None}
            elif checked:
                self._session_auth_cache = {"ts": time.time(), "verdict": auth_verdict}
            if self._current_publish_mode() != "attach":
                self._pub_session_status.config(text="", fg=C["muted"])
                return
            self._render_session_status(port_state, self._session_auth_cache.get("verdict"))
        except tk.TclError:
            pass

    def _render_session_status(self, port_state: str, verdict) -> None:
        """포트 상태×로그인 판정을 사실대로 한 줄로 옮긴다.

        CDP 접근 실패(port_state=='unknown')는 '닫힘'이 아니라 '확인 불가'로
        표시한다 — 예외를 전부 닫힘으로 뭉개던 구 버전의 핵심 결함(수정
        대상 A) 수리."""
        try:
            if port_state == "closed":
                self._pub_session_status.config(
                    text=_iconed("warn", "세션 닫힘 — 위 버튼으로 열어 주세요"),
                    fg=C["orange"])
            elif port_state == "unknown":
                self._pub_session_status.config(
                    text=_iconed("warn", "세션 상태 확인 불가"), fg=C["muted"])
            elif verdict == "match":
                self._pub_session_status.config(
                    text=_iconed("ok", "세션 열림 · 로그인됨"), fg=C["green"])
            elif verdict == "mismatch":
                self._pub_session_status.config(
                    text=_iconed("warn", "세션 열림 · 다른 계정으로 로그인됨"),
                    fg=C["orange"])
            elif verdict == "unknown":
                self._pub_session_status.config(
                    text=_iconed("warn", "세션 열림 · 로그인 필요(또는 확인 불가)"),
                    fg=C["orange"])
            else:  # 아직 한 번도 확인 못함(막 열었거나 발행 중이라 스킵)
                self._pub_session_status.config(
                    text="세션 열림 · 로그인 확인 중…", fg=C["muted"])
        except tk.TclError:
            pass

    def _on_publish_mode_change(self, label: str) -> None:
        """Segmented 선택 변경 콜백 — 라벨을 내부 모드로 변환해 저장한다."""
        new_mode = _PUB_LABEL_TO_MODE.get(label, "spawn")
        if new_mode == self._current_publish_mode():
            return
        try:
            from gui.config_io import save_cfg
            self.app.cfg["publish_mode"] = new_mode
            save_cfg(dict(self.app.cfg))
        except Exception as e:  # noqa: BLE001 — 저장 실패는 경고만(모드 미변경)
            log.warning(f"[home] 발행 방식 저장 실패: {e}")
            self._toast(f"발행 방식을 저장하지 못했어요. ({e})", kind="warn")
            self._render_publish_mode()   # 실제 cfg 값으로 세그먼트 되돌리기
            return
        self._render_publish_mode()
        if new_mode == "attach":
            self._trigger_session_probe(force=True)

    def _open_publish_session(self):
        """세션 크롬을 열고(백그라운드), 로그인이 필요하면 로그인 페이지까지 띄운다."""
        if getattr(self, "_session_open_inflight", False):
            return      # 빠른 중복 클릭 무시 — 크롬 2번 기동·로그인 탭 2개 방지
        self._session_open_inflight = True
        try:
            self._pub_session_btn.config(state="disabled")
            self._pub_session_status.config(text="여는 중…", fg=C["muted"])
        except tk.TclError:
            pass
        self.spawn(self._open_publish_session_worker)

    def _open_login_page(self, port: int) -> bool:
        """발행 세션 크롬에 네이버 로그인 페이지를 띄운다(성공 True).

        새 탭을 여는 실제 구현은 발행 파이프라인의 로그인 대기가 이미 쓰는
        `App._open_login_page_in_session` 하나뿐이다 — 여기서 같은 경로를
        재사용하고 결과만 옮긴다(로그인 URL·탭 규칙이 두 벌로 갈라지지 않게).
        그 메서드가 없는 앱에서는 조용히 실패로 보고 안내만 한다.
        """
        fn = getattr(self.app, "_open_login_page_in_session", None)
        if not callable(fn):
            log.warning("[home] 로그인 페이지 열기 경로 없음 — 안내만")
            return False
        try:
            return bool(fn(port))
        except Exception as e:  # noqa: BLE001 — 실패는 정직한 안내로 수렴
            log.warning(f"[home] 로그인 페이지 열기 실패: {type(e).__name__}: {e}")
            return False

    def _open_publish_session_worker(self):
        status = "error"
        ready = False
        verdict = None
        login = "skipped"
        try:
            from core import chrome_session
            from config import settings as s
            port = int(getattr(s, "CHROME_ATTACH_PORT", 9222))
            status = chrome_session.ensure_session(port)
            if status == "started":
                # 콜드 스타트 레이스 방지 — 방금 기동한 크롬이 디버깅 포트를
                # 열 때까지 기다린 뒤에야 상태를 갱신한다(요건 b). 'already'
                # 는 ensure_session 자체가 이미 열려있음을 확인했으므로 생략.
                ready = bool(chrome_session.wait_until_running(port))
            elif status == "already":
                ready = True
            if ready:
                # 사용자가 **명시적으로 누른** 경우에만 로그인 상태를 보고
                # 필요할 때 탐색한다(유휴 폴링은 지금도 읽기 전용 — 탭을 열거나
                # 페이지를 이동하지 않는다).
                verdict = self._check_session_auth(port)
                if verdict == "match":
                    login = "not_needed"     # 이미 대상 계정 — 로그인 강요·로그아웃 금지
                elif verdict == "mismatch":
                    login = "wrong_account"  # 다른 계정 — 우리가 로그아웃시키지 않는다
                else:
                    login = "opened" if self._open_login_page(port) else "open_failed"
        except Exception as e:  # noqa: BLE001 — 어떤 실패도 안내로 수렴
            log.warning(f"[home] 발행 세션 열기 실패: {e}")
            status = "error"
            ready = False
        self.ui_after(self._after_open_session, status, ready, verdict, login)

    def _after_open_session(self, status: str, ready: bool = True,
                            verdict=None, login: str = "skipped"):
        messages = {
            "started": "발행 세션 크롬을 열었어요. 그 창에서 네이버에 한 번 로그인하세요.",
            "already": "발행 세션이 이미 열려 있어요. 그 창의 네이버 로그인 상태를 확인하세요.",
            "not_found": "크롬(chrome.exe)을 찾지 못했어요. 크롬 설치를 확인하세요.",
            "error": "세션 크롬을 열지 못했어요. 다시 시도하거나 크롬을 직접 실행하세요.",
        }
        kind = "info"
        if status in ("not_found", "error"):
            text = messages[status]
            kind = "warn"
        elif not ready:
            text = ("발행 세션 크롬은 띄웠지만 아직 준비되지 않아(포트 응답 없음) "
                    "로그인 페이지를 열지 못했어요. 잠시 뒤 다시 눌러 주세요.")
            kind = "warn"
        elif login == "not_needed":
            text = "발행 세션이 이미 설정된 계정으로 로그인돼 있어요. 그대로 발행할 수 있어요."
        elif login == "wrong_account":
            text = ("발행 세션이 설정과 다른 네이버 계정으로 로그인돼 있어요. "
                    "그 창에서 로그아웃한 뒤 설정된 계정으로 로그인하세요.")
            kind = "warn"
        elif login == "opened":
            text = "발행 세션 크롬에 네이버 로그인 페이지를 열었어요. 그 창에서 로그인하세요."
        elif login == "open_failed":
            text = ("발행 세션 크롬은 열렸지만 로그인 페이지를 열지 못했어요. "
                    "그 창에서 직접 네이버에 로그인하세요.")
            kind = "warn"
        else:
            text = messages.get(status, f"상태: {status}")
        self._session_open_inflight = False
        try:
            self._pub_session_status.config(text="", fg=C["muted"])
        except tk.TclError:
            pass
        self._toast(text, kind=kind)
        self._render_publish_mode()      # 버튼 상태 복구(attach 면 다시 누를 수 있게)
        if self._current_publish_mode() == "attach":
            self._trigger_session_probe(force=True)

    def _load(self):
        """빠른 항목(시스템·어제 결과·자동운영·오늘 요약)을 먼저 _apply 로
        반영하고, 느린 항목(발행 준비=preflight)은 끝나는 대로 뒤이어
        _apply 로 반영한다. `_apply(dict)` 는 부분 dict(일부 키만 있는
        dict)를 받아도 그 키에 해당하는 화면만 갱신한다."""
        seq = self._load_seq   # 이 세대 번호 — 새 refresh() 가 이미 시작됐으면 낡은 결과 폐기

        # ── 빠른 항목 ────────────────────────────────
        fast: dict = {}

        # 시스템 — 가벼운 검사만(셀레늄·네트워크 대기 없음)
        issues = []            # (심각도 'red'|'yellow', 문구, 액션키)
        try:
            if not self.app.cfg.get("naver_id"):
                issues.append(("red", "네이버 계정을 설정하세요", "settings"))
        except Exception:  # noqa: BLE001
            pass
        if not shutil.which("claude"):
            issues.append(("red", "Claude 연결이 필요해요 — 글 생성 불가", "settings"))
        try:
            _pub_mode = str(self.app.cfg.get("publish_mode", "spawn")).lower()
            if _pub_mode == "attach":
                # attach 는 쿠키를 안 쓰고 세션 크롬에 붙는다 — 쿠키 mtime 은 상시
                # 오탐이므로 세션 크롬이 열려 있는지로 판정한다(순수 UX).
                from core import chrome_session
                from config import settings as _s
                if not chrome_session.is_running(getattr(_s, "CHROME_ATTACH_PORT", 9222)):
                    # ⚠️ 문구는 실제 동작과 같아야 한다 — '오늘 실행'은 이제
                    # 발행용 크롬을 스스로 열고 필요하면 로그인 창까지 띄운다.
                    # 먼저 [발행 세션 열기]를 누르라고 요구하지 않는다.
                    issues.append((
                        "yellow",
                        "발행용 크롬이 아직 안 열려 있어요 — 오늘 실행을 누르면 "
                        "자동으로 열고, 로그인이 필요하면 그때 알려드려요",
                        "open_session"))
            else:
                from config import profile as _p
                ck = _p.cookie_path()
                fresh = ck.exists() and (time.time() - ck.stat().st_mtime) < 24 * 3600
                if not fresh:
                    issues.append(("yellow",
                                   "네이버 로그인이 오래됐어요 (발행 시작할 때 자동으로 다시 로그인해요)",
                                   "login"))
        except Exception:  # noqa: BLE001
            pass
        try:
            from core.shorts_studio_client import is_alive
            if not is_alive():
                issues.append(("yellow", "쇼츠 서버가 꺼져 있어요 — 쇼츠는 기본 화질로 생성돼요", None))
        except Exception:  # noqa: BLE001
            pass
        fast["system"] = issues

        # 이미지 채움 게이트 — 첫 화면 카드(수정 대상 C)가 클릭 전에도 왜 못
        # 쓰는지 보이도록. 판정 로직은 그대로 `_image_fill_enabled_setting`
        # (blog_service 게이트 위임) — 여기서 새로 판정하지 않는다.
        try:
            fast["fill_enabled"] = _image_fill_enabled_setting()
        except Exception:  # noqa: BLE001 — 판정 실패는 '켜짐'으로 간주(안내 숨김,
                            # 기존 클릭 시 토스트가 실제 상태를 다시 확인한다)
            fast["fill_enabled"] = True

        # 저장 글 재고 — "지금 바로 나갈 수 있는 글이 몇 편인가"(v4).
        # 저장 글 탭과 **같은 조회 계층**을 쓴다(숫자가 갈라지지 않게).
        try:
            from gui import draft_library
            fast["stock"] = draft_library.counts(draft_library.list_drafts(limit=200))
        except Exception:  # noqa: BLE001 — 재고 조회 실패는 "모름"으로 표시
            log.debug("[home] 저장 글 재고 조회 실패", exc_info=True)
            fast["stock"] = None

        # 어제 결과 — 로그 + 방문자
        y = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
        y_entries = safe_load_json(_DATA_DIR / "logs" / f"{y}.json", default=list)
        fast["yesterday"] = summarize_log_entries(y_entries)
        try:
            from core import performance_db as pdb
            series = pdb.visitor_series(days=7) or []
            if series:
                last = series[-1]
                fast["visitors"] = int(last.get("count", last.get("cnt", 0)) or 0)
        except Exception:  # noqa: BLE001
            fast["visitors"] = None

        # 자동 운영 등록 상태 — schtasks 조회(각 ~0.1s, 백그라운드라 무해)
        try:
            from scheduler.task_register import check_task
            reg, _msg = check_task()
            fast["auto_daily"] = bool(reg)
        except Exception:  # noqa: BLE001
            fast["auto_daily"] = None
        try:
            from scheduler.run_ops import check_ops_task
            reg, _msg = check_ops_task()
            fast["auto_weekly"] = bool(reg)
        except Exception:  # noqa: BLE001
            fast["auto_weekly"] = None

        # 오늘 요약 — 실행 결과(세션) 우선, 없으면 오늘 로그
        today_entries = safe_load_json(
            _DATA_DIR / "logs" / f"{date.today().strftime('%Y-%m-%d')}.json", default=list)
        fast["today"] = summarize_log_entries(today_entries)
        fast["last_result"] = getattr(self.app, "last_run_result", None)

        if seq == self._load_seq:
            self.ui_after(self._apply, fast)

        # ── 느린 항목 — 발행 준비(preflight). 대형 큐에서 수~수십 초 걸릴
        # 수 있어(실측 ~19초) 8초 타임아웃 가드(_check_stuck) 대상이다. ──
        slow: dict = {}
        try:
            from scheduler.auto_scheduler import preflight_check
            pf = preflight_check() or {}
            cands = pf.get("candidates", []) or []
            total = int(pf.get("total_candidates", len(cands)))
            # 보관본 매칭은 카테고리 근사가 아니라 **제목 단위 실측** — 버튼이
            # 사용자에게 약속하는 숫자이므로 과대 표시하면 안 된다.
            # (load_drafts 는 캐시라 후보 20개 lookup 은 수십 ms.)
            drafts = 0
            try:
                from core.draft_store import find_draft_for_title
                drafts = sum(1 for c in cands
                             if find_draft_for_title(c.get("title", "")) is not None)
            except Exception:  # noqa: BLE001
                pass
            slow["queue"] = {"total": total, "drafts": min(drafts, total)}
        except Exception as e:  # noqa: BLE001
            log.debug(f"[home] preflight 실패: {e}")
            slow["queue"] = None
        # preflight 의 get_pending_posts 가 방금 갱신한 정렬 근거를 읽는다
        slow["sort_rationale"] = safe_load_json(
            _DATA_DIR / "logs" / "queue_sort_rationale.json", default=dict)
        # 정렬 캐시(core.queue_sort_cache) 나이 — 방금 preflight 가 위 큐를
        # 계산하며 이미 채웠거나 갱신했을 캐시 파일의 mtime 만 본다(캐시
        # 모듈을 직접 건드리지 않는다, 읽기 전용). 파일이 없으면(작은 큐라
        # 캐시가 아예 안 쓰이거나, 아직 한 번도 안 채워졌으면) None.
        slow["sort_cache_age_min"] = None
        try:
            cache_file = _DATA_DIR / "logs" / "queue_sort_cache.json"
            if cache_file.exists():
                age_s = time.time() - cache_file.stat().st_mtime
                slow["sort_cache_age_min"] = max(0, int(age_s // 60))
        except OSError:
            pass
        # 뉴스 선행지표 한 줄(캐시 읽기만 — 네트워크 0). 신호 없으면 빈 문자열.
        try:
            from core.news_radar import summary_line
            slow["news_line"] = summary_line()
        except Exception:  # noqa: BLE001
            slow["news_line"] = ""

        if seq == self._load_seq:
            self.ui_after(self._apply, slow)

    # ─────────────────────────────────────────────
    # 화면 반영 (메인 스레드)
    # ─────────────────────────────────────────────
    def _set_light(self, key: str, kind: str, label: str,
                   detail: str, fixes: list | None = None):
        """kind: "ok"|"warn"|"error"|"neutral". label 은 아이콘 없는 순수 단어를
        그대로 넘긴다 — `StatusPill.set()` 이 kind 에 맞는 글리프를 자체
        접두한다(수정 라운드 🔴2: 호출부에서 또 붙이면 "✓ ✓ 준비됨"처럼
        아이콘이 이중으로 찍혔다)."""
        try:
            lt = self._lights[key]
            lt["state"].set(label, kind)
            lt["detail"].config(text=detail)
            for w in lt["action"].winfo_children():
                w.destroy()
            fixes = fixes or []
            if fixes:
                urgent = kind == "error"
                factory = PrimaryBtn if urgent else SecondaryBtn
                # icon= 은 여기서 쓰지 않는다 — CTk 가 아이콘 버튼을 CTkFrame
                # (아이콘+버튼)으로 감싸 반환해 `winfo_children()[0].invoke()`
                # 로 직접 클릭하는 기존 계약(`tests/test_home_ux.py`)이 깨진다.
                for fix in fixes:
                    fix_label, cmd = fix[0], fix[1]
                    factory(lt["action"], fix_label, cmd)\
                        .pack(anchor="w", pady=(SP["xs"] + px(2), 0))
                lt["action"].pack(anchor="w", fill="x")
            else:
                # 조치할 게 없으면 action 컨테이너 자체를 숨긴다 — 내용 없는
                # 상자가 정상 상태에서도 보이던 문제(수정 라운드 🟡2).
                lt["action"].pack_forget()
        except tk.TclError:
            pass

    def _apply(self, d: dict):
        """부분 dict 를 받아도 안전 — `_load()` 가 빠른 항목과 느린 항목을
        나눠 두 번 호출하므로, 이번 호출에 실려 온 키에 해당하는 화면만
        갱신하고 나머지는 손대지 않는다(수정 라운드 🟡3)."""
        try:
            # ── 신호등 ① 발행 준비 + 큰 버튼 부제(느린 항목) ──
            if "queue" in d:
                self._pending_queue = False
                q = d.get("queue")
                if q and q["total"] > 0:
                    new = q["total"] - q["drafts"]
                    eta = format_minutes(estimate_run_minutes(new, q["drafts"]))
                    self.run_sub.config(
                        text=f"발행 {q['total']}편 예정 · 보관본 {q['drafts']}편 바로 사용 · {eta} 걸려요")
                elif q:
                    self.run_sub.config(text="지금은 발행할 후보가 없어요")
                else:
                    self.run_sub.config(text="발행 정보를 불러오지 못했어요 — 실행은 그대로 가능해요")

                r = d.get("sort_rationale") or {}
                cache_hint = _format_cache_age_hint(d.get("sort_cache_age_min"))
                if q and q["total"] > 0 and r.get("total"):
                    matched = (r.get("matched_volume", 0) + r.get("matched_datalab", 0)
                               + r.get("matched_deadline", 0) + r.get("matched_news", 0))
                    top = (r.get("top") or [{}])[0]
                    if matched:
                        lead = (top.get("keyword") or "")[:20]
                        vol = top.get("volume") or 0
                        # 2026-09-22 문구 점검 — "1순위 X (월 47,700회)" 처럼
                        # 단위가 생략된 내부 표기 대신 무엇을 센 숫자인지 적는다.
                        # 한 줄에 들어가도록 근거 편수는 첫 글을 못 밝혔을
                        # 때만 덧붙인다(1080px 에서 두 줄로 넘어가던 문제).
                        lead_s = (f" · 첫 글 ‘{lead}’ (한 달 {vol:,}번 검색)"
                                  if lead and vol
                                  else (f" · 첫 글 ‘{lead}’" if lead
                                        else f" · 검색량 확인 {matched}편"))
                        self.sort_hint.config(
                            text="먼저 나갈 순서: 많이 찾는 키워드·마감 임박 순"
                                 f"{lead_s}{cache_hint}")
                    else:
                        self.sort_hint.config(
                            text="먼저 나갈 순서: 참고할 검색량 자료가 없어 "
                                 f"목록에 적힌 순서대로 나가요{cache_hint}")
                else:
                    self.sort_hint.config(text="")
                self.news_hint.config(text=d.get("news_line") or "")

                if q is None:
                    self._set_light("queue", "warn", "확인 불가",
                                    "후보 점검에 실패했어요. 실행하면 다시 점검해요.")
                elif q["total"] == 0:
                    self._set_light("queue", "error", "후보 없음",
                                    "발행할 글감이 떨어졌어요. 키워드를 채워주세요.",
                                    [("키워드 채우러 가기",
                                      lambda: self.app.show_tab("keyword"))])
                else:
                    self._set_light("queue", "ok", "준비됨",
                                    f"후보 {q['total']}편 · 보관본 {q['drafts']}편 바로 사용 가능")

            # ── 신호등 ② 시스템(빠른 항목) ──
            if "system" in d:
                issues = d.get("system") or []
                reds = [i for i in issues if i[0] == "red"]
                tops = reds or issues
                if not issues:
                    self._set_light("system", "ok", "정상", "지금 할 일 없음")
                else:
                    sev, msg, act = tops[0]
                    fixes = []
                    if act == "settings":
                        fixes.append(("설정 열기", lambda: self.app.show_tab("settings")))
                    elif act == "login":
                        fixes.append(("지금 로그인", self._fix_login))
                    elif act == "open_session":
                        fixes.append(("발행 세션 열기", self._open_publish_session))
                    kind = "error" if sev == "red" else "warn"
                    label = "조치 필요" if sev == "red" else "참고"
                    more = f"  (+{len(issues)-1}건)" if len(issues) > 1 else ""
                    # 신호등 칸은 세 칸으로 나뉘어 좁다 — 괄호 안 부연은 위
                    # "지금 다루는 블로그" 카드가 전문(全文)으로 보여주므로
                    # 여기선 핵심 절만 남긴다(같은 문장이 두 번, 그것도 세 줄로
                    # 접혀 카드가 화면 밖으로 밀리던 문제).
                    self._set_light("system", kind, label,
                                    _short_issue(msg) + more, fixes)
                # 현재 블로그 카드의 "발행 준비" 알약도 같은 판정에서 파생한다
                # (두 곳이 다른 말을 하지 않게 — 소스는 system 신호등 하나).
                if not issues:
                    self._set_ready("ok", "발행 준비 완료",
                                    "로그인·계정 설정에 지금 할 일이 없어요")
                else:
                    sev0, msg0, _act0 = (reds or issues)[0]
                    self._set_ready("error" if sev0 == "red" else "warn",
                                    "로그인 필요" if sev0 == "red" else "확인 필요",
                                    msg0)

            # ── 저장 글 재고 한 줄(빠른 항목, v4) ──
            if "stock" in d:
                st = d.get("stock")
                if not st:
                    self.stock_lbl.config(
                        text="저장 글 현황을 읽지 못했어요 — 저장 글 탭에서 확인하세요.")
                elif st["total"] == 0:
                    self.stock_lbl.config(
                        text="저장해 둔 글이 없어요 — 발행·진행 탭의 '보관함 채우기'로 "
                             "글을 먼저 만들어 두면 발행이 빨라져요.")
                else:
                    text = (f"저장 글 {st['total']}편 · 이미지까지 끝나 바로 나갈 수 있는 글 "
                            f"{st['complete']}편 · 이미지 부족 {st['partial']}편 · "
                            f"이미지 없음 {st['none']}편")
                    # 장수는 찼지만 출처를 확인할 수 없어 발행이 막히는 글 — 자동으로
                    # 풀리지 않으니 숫자를 숨기지 않고 할 일까지 붙인다.
                    if st.get("unverified"):
                        text += (f" · 확인 필요 {st['unverified']}편"
                                 f"(저장 글 탭에서 '미검증 이미지 다시 만들기')")
                    self.stock_lbl.config(text=text)

            # ── 이미지 채움 게이트 안내(빠른 항목, 수정 대상 C) — 꺼져 있으면
            # 클릭 전에도 이유가 보이게. 켜져 있으면 문구를 비운다.
            if "fill_enabled" in d:
                if d.get("fill_enabled"):
                    self.set_fill_hint("")
                else:
                    self.set_fill_hint(
                        "지금은 꺼져 있어요 — 설정 탭의 '이미지 채움'에서 켠 뒤 "
                        "사용하세요.")

            # ── 신호등 ③ 어제 결과(빠른 항목) ──
            if "yesterday" in d:
                yy = d.get("yesterday") or {}
                v = d.get("visitors")
                if yy.get("published", 0) == 0 and yy.get("skipped", 0) == 0:
                    self._set_light("yesterday", "neutral", "실행 없음",
                                    "어제는 발행 실행이 없었어요"
                                    + (f" · 방문자 {v}명" if v else ""))
                else:
                    bad = yy.get("skipped", 0)
                    kind = "ok" if bad == 0 else "warn"
                    label = "좋음" if bad == 0 else "일부 스킵"
                    self._set_light(
                        "yesterday", kind, label,
                        f"발행 {yy.get('published', 0)} · 안 나간 글 {bad}"
                        + (f" · 방문자 {v}명" if v else ""))

            # ── 알아서 운영 토글(빠른 항목) ──
            if "auto_daily" in d:
                self._apply_auto_row("daily", d.get("auto_daily"),
                                     on="켜짐 — 매일 아침 GUI 없이 자동 발행해요",
                                     off="꺼짐 — 아침에 실행 버튼을 직접 누르세요")
            if "auto_weekly" in d:
                self._apply_auto_row("weekly", d.get("auto_weekly"),
                                     on="켜짐 — 매주 월요일 측정·리포트가 자동으로 나와요",
                                     off="꺼짐 — 리포트는 직접 실행해야 해요")

            # ── 요약 카드(빠른 항목) ──
            if "today" in d or "last_result" in d:
                self._apply_summary(d)
        except tk.TclError:
            pass

    def _apply_auto_row(self, key: str, registered, *, on: str, off: str):
        try:
            row = self._auto_rows[key]
            row["registered"] = registered
            if registered is None:
                row["status"].set("상태 확인 실패", "warn")
                row["btn"].config(text="…", state="disabled")
            elif registered:
                row["status"].set(on, "ok")
                row["btn"].config(text="끄기", state="normal",
                                  bg=C["card"], fg=C["muted"],
                                  command=lambda: self._toggle_auto(key))
            else:
                row["status"].set(off, "neutral")
                row["btn"].config(text="켜기", state="normal",
                                  bg=C["accent"], fg=C["bg"],
                                  command=lambda: self._toggle_auto(key))
        except tk.TclError:
            pass

    def _apply_summary(self, d: dict):
        today = d.get("today") or {}
        lr = d.get("last_result") or {}
        if lr.get("error"):
            message = ("다른 실행이 진행 중이어서 시작하지 못했습니다."
                       if lr["error"] == "already_running"
                       else f"실행하지 못했습니다: {lr['error']}")
            self.summary_body.config(text=message)
            return
        pub = today.get("published", 0)
        skipped = today.get("skipped", 0)
        if pub == 0 and skipped == 0:
            self.summary_body.config(text="아직 오늘 실행 전이에요. 위의 실행 버튼 하나면 됩니다.")
            return
        parts = [f"발행 {pub}편"]
        if lr.get("drafts_used"):
            parts[0] = (f"발행 {pub}편 (보관본 {lr['drafts_used']}"
                        f" · 새 글 {max(pub - int(lr['drafts_used']), 0)})")
        if skipped:
            reasons = " · ".join(f"{msg} {n}건" for msg, n in today.get("reasons", [])[:4])
            parts.append(f"안 나간 글 {skipped}편 — {reasons}")
        else:
            parts.append("안 나간 글 없음")
        ts = datetime.now().strftime("%H:%M")
        self.summary_body.config(text=f"(기준 {ts})  " + "  ·  ".join(parts))

    # ─────────────────────────────────────────────
    # 알아서 운영 토글 (P2) — "더 할 일" 카드 내부 서브그룹
    # ─────────────────────────────────────────────
    def _build_auto_section(self, parent):
        L(parent, "알아서 운영", FT, C["text"]).pack(anchor="w")
        self._auto_rows = {}
        for key, label in (("daily", "아침 자동 발행"),
                           ("weekly", "주간 성장 리뷰 (월요일)")):
            row = tk.Frame(parent, bg=C["card"])
            row.pack(fill="x", pady=(SP["xs"] + px(2), 0))
            L(row, label, FB, C["text"], width=20, anchor="w").pack(side="left")
            st = StatusPill(row, _LOADING, kind="neutral")
            st.pack(side="left", padx=(SP["xs"], 0))
            btn = tk.Button(row, text="…", font=FS, relief="flat",
                            cursor="hand2", padx=SP["md"], pady=px(2),
                            fg=C["bg"], bg=C["accent"], state="disabled",
                            command=lambda k=key: self._toggle_auto(k))
            btn.pack(side="right")
            self._auto_rows[key] = {"status": st, "btn": btn, "registered": None}

    def _toggle_auto(self, key: str):
        """작업 스케줄러 등록/해제 — 켤 때만 한 번 확인, 끌 때는 바로.

        스레드는 여기서만 순수 `threading.Thread` 로 직접 띄운다(테스트가
        `H.threading.Thread` 를 동기 대체품으로 monkeypatch 하는 계약,
        `tests/test_ux_p2.py::test_toggle_off_calls_unregister_without_confirm`).
        """
        row = self._auto_rows.get(key) or {}
        turning_on = not row.get("registered")
        if turning_on:
            if key == "daily":
                try:
                    from config import settings as _s
                    hours = " · ".join(getattr(_s, "SCHEDULE_HOURS", []) or ["06:00"])
                except Exception:  # noqa: BLE001
                    hours = "06:00"
                ok = messagebox.askyesno(
                    "아침 자동 발행 켜기",
                    f"매일 {hours} 에 GUI 없이 자동으로 발행(예약등록)해요.\n"
                    "네이버 로그인이 풀려 있는 날은 발행하지 못하고\n"
                    "카카오로 알려드려요.\n\n켤까요?")
            else:
                ok = messagebox.askyesno(
                    "주간 성장 리뷰 켜기",
                    "매주 월요일 아침, 측정·집계·리포트를 자동으로 만들어요.\n"
                    "(발행이나 설정 변경은 하지 않아요)\n\n켤까요?")
            if not ok:
                return
        try:
            row["btn"].config(state="disabled", text="…")
            row["status"].set("적용 중…", "neutral")
        except tk.TclError:
            pass

        def _work():
            try:
                if key == "daily":
                    from scheduler.task_register import register_task, unregister_task
                    ok, msg = (register_task() if turning_on else unregister_task())
                else:
                    from scheduler.run_ops import register_ops_task, unregister_ops_task
                    ok, msg = (register_ops_task() if turning_on else unregister_ops_task())
            except Exception as e:  # noqa: BLE001
                ok, msg = False, str(e)
            log.info(f"[home] 자동 운영 토글 {key} {'ON' if turning_on else 'OFF'}: {ok} {msg}")
            self.ui_after(self.refresh)

        threading.Thread(target=_work, daemon=True).start()

    # ─────────────────────────────────────────────
    # 이미지 채움 (보관해 둔 글에 이미지를 미리 붙여두기 · 발행 아님)
    # 엔진은 설정에서 고른다(기본 Codex, 옵트인 레거시 젠스파크) — UI 는 엔진 이름을
    # 늘어놓지 않고 "이미지 채우기" 라는 한 가지 일로만 보여준다.
    # 첫 화면 카드(수정 대상 C, 2026-09-10) — 예전엔 "더 할 일" 접힘 안에 있어
    # 안 보였다. 이 메서드 자체(위젯·콜백)는 그대로이고 호출부(`_build_ui`)만
    # 첫 화면 카드로 옮겼다 — 실행 경로 중복 없음.
    # ─────────────────────────────────────────────
    def _build_fill_section(self, parent):
        # v4(2026-09-21): 제목을 "이미지 채움"(기능 이름)에서 "저장 글"(사용자
        # 관심사)로 바꾸고, 세 줄짜리 설명 문단을 **실측 한 줄**로 대체했다.
        # 사용자가 여기서 알고 싶은 건 "지금 바로 나갈 수 있는 글이 몇 편인가"
        # 하나다. 긴 설명은 [자세히] 로 접어 둔다.
        title_row = tk.Frame(parent, bg=C["card"])
        title_row.pack(fill="x")
        L(title_row, "저장 글", FT, C["text"]).pack(side="left")
        SecondaryBtn(title_row, "저장 글 보기",
                     lambda: self.app.show_tab("archive"), icon="folder").pack(
            side="right")

        self.stock_lbl = L(parent, _LOADING, FB, C["text_2"], anchor="w",
                           justify="left")
        self.stock_lbl.pack(anchor="w", fill="x", pady=(px(2), SP["sm"]))

        row = tk.Frame(parent, bg=C["card"])
        row.pack(fill="x")
        self.fill_btn = SecondaryBtn(row, "이미지 채움 시작", self._fill_images, icon="add")
        self.fill_btn.pack(side="left")
        L(row, "편수", FS, C["text_2"]).pack(side="left", padx=(SP["md"], SP["xs"]))
        self._fill_n_var = tk.IntVar(value=5)
        self._fill_spin = tk.Spinbox(
            row, from_=1, to=50, textvariable=self._fill_n_var, width=4,
            font=FB, fg=C["text"], bg=C["surface"], relief="flat", bd=0,
            highlightthickness=1, highlightbackground=C["border_strong"],
            highlightcolor=C["focus"])
        self._fill_spin.pack(side="left")
        self.fill_status = L(row, "", FS, C["text_2"])
        self.fill_status.pack(side="left", padx=(SP["md"], 0))

        # 게이트가 꺼져 있으면 클릭해 보기 전에도 왜 못 쓰는지 보이게(요건 C —
        # "비활성이면 이유·설정 이동 안내"). `refresh()`(→ `_apply`) 가 매번
        # 최신 게이트 값으로 갱신하므로, 설정 탭에서 켠 뒤 홈으로 돌아와
        # 새로고침하면 문구가 사라진다. 빈 문자열이면 아무것도 안 보인다.
        # 긴 설명은 기본 접힘 — 화면에 상시 세 줄을 깔지 않는다(v4).
        # 토글 버튼은 같은 줄 오른쪽 끝에 둬서 세로 한 줄을 아낀다.
        self._fill_help_open = False
        self._fill_help_btn = SubtleBtn(row, "이미지 채움이 뭔가요",
                                        self._toggle_fill_help)
        self._fill_help_btn.pack(side="right")

        # 게이트가 꺼져 있을 때만 보이는 경고 한 줄 — 평소엔 자리도 차지하지
        # 않는다(빈 라벨이 상시 한 줄을 먹던 문제).
        self.fill_hint = L(parent, "", FS, C["orange"], justify="left",
                           anchor="w", wraplength=px(560))
        self._fill_help_lbl = L(
            parent,
            "글 만들기, 이미지 채우기, 발행 준비 중 '이미지 채우기' 단계예요. "
            "이미지가 없는 글부터, 장수가 모자란 글은 모자란 만큼만 채우고, "
            "지금 발행하지는 않아요.",
            FS, C["muted"], justify="left", anchor="w", wraplength=px(560))

        self._fill_confirm = InlineConfirm(parent)
        self._fill_progress = ProgressRow(parent)
        # 좁은 창(900×620)에서 재고 한 줄·안내 문구가 오른쪽으로 잘리지 않게.
        # parent 는 Card 어댑터라 <Configure> 폭에 카드 좌우 안쪽 여백
        # (padx=SP.lg 양쪽)이 포함돼 있다 — 그만큼 더 빼야 실제 본문 폭이다.
        _bind_dynamic_wrap(parent, [self.stock_lbl, self.fill_hint,
                                    self._fill_help_lbl],
                           extra_pad=SP["lg"] * 2 + px(8))

    def set_fill_hint(self, text: str) -> None:
        """이미지 채움 경고 문구 — 비어 있으면 라벨 자체를 숨긴다."""
        try:
            self.fill_hint.config(text=text)
            if text:
                if not self.fill_hint.winfo_manager():
                    self.fill_hint.pack(anchor="w", fill="x", pady=(SP["sm"], 0))
            else:
                self.fill_hint.pack_forget()
        except tk.TclError:
            pass

    def _toggle_fill_help(self):
        """이미지 채움 설명 문단 접기/펼치기."""
        self._fill_help_open = not self._fill_help_open
        try:
            if self._fill_help_open:
                self._fill_help_lbl.pack(anchor="w", fill="x", pady=(SP["sm"], 0))
            else:
                self._fill_help_lbl.pack_forget()
        except tk.TclError:
            pass

    def _fill_images(self):
        """이미지 없는 draft_ready 보관본을 이미지 채움 엔진으로 채운다(발행 아님).

        발행 중이면 거부, 이미지 자동 채움이 꺼져 있으면 안내 후 중단한다(2026-09-09
        Codex 전환 — 엔진 선택과 무관하게 ``IMAGE_FILL_ENABLED`` 마스터 스위치
        하나로 판정한다). 대상 카운트·배치 실행은 전부 백그라운드 스레드(UI
        무블록)이고, 확인은 InlineConfirm 1단으로 처리한다. 어떤 예외도 GUI 를
        죽이지 않는다.
        """
        if self._fill_running:
            return
        if getattr(self.app, "_running", False):
            self._toast("지금은 발행이 진행 중이에요. 발행이 끝난 뒤 다시 시도하세요.",
                       kind="warn")
            return
        if not _image_fill_enabled_setting():
            self._toast("이미지 자동 채움 기능이 꺼져 있어요. 설정 탭의 '이미지 채움'에서 "
                       "켠 뒤 사용하세요.", kind="info")
            return

        self._fill_running = True
        try:
            self.fill_btn.config(state="disabled")
            self.fill_status.config(text="대상 확인 중…", fg=C["text_2"])
        except tk.TclError:
            pass
        self.spawn(self._count_fill_targets)

    def _count_fill_targets(self):
        """이미지가 없거나 부족한 draft_ready 보관본 수를 센다(백그라운드) → 확인 배너로.

        ``core.blog_service.fill_target_count(topup=_FILL_TOPUP)`` 로 위임한다 —
        직접 draft_store 를 불러 "경로 문자열이 있으면 채워짐"으로 판정하던 예전
        로직은 파일이 실제로 사라진 경우도 채워진 것으로 오판했다. 실행
        (``fill_images(topup=_FILL_TOPUP)``)과 **같은 선별 규칙**을 쓰므로 안내
        숫자와 실제 처리 대상이 일치한다.
        """
        count = None
        try:
            from core import blog_service
            count = blog_service.fill_target_count(topup=_FILL_TOPUP)
        except Exception as e:  # noqa: BLE001 — 카운트 실패는 안내로 수렴
            log.warning(f"[home] 이미지 채움 대상 카운트 실패: {e}")
            count = None
        self.ui_after(self._after_fill_count, count)

    def _after_fill_count(self, count):
        """대상 수를 받아 InlineConfirm 으로 확인(메인 스레드) → 배치 실행 착수."""
        try:
            if count is None:
                self._toast("보관본을 불러오지 못했어요. 잠시 후 다시 시도하세요.", kind="warn")
                self._reset_fill_button()
                return
            if count <= 0:
                self._toast("이미지가 없거나 부족한 보관본이 없어요. 채울 대상이 없어요.",
                           kind="info")
                self._reset_fill_button()
                return
            try:
                from config import settings as _s
                cap = getattr(_s, "IMAGE_FILL_MAX_POSTS_PER_RUN", None)
                if cap is None:  # 구버전 settings.py 폴백(2026-09-09 Codex 전환)
                    cap = getattr(_s, "GENSPARK_MAX_POSTS_PER_RUN", 25)
                cap = int(cap or 25)
            except Exception:  # noqa: BLE001
                cap = 25
            try:
                want = int(self._fill_n_var.get())
            except (tk.TclError, ValueError):
                want = count
            batch = max(1, min(want, count, cap))
            try:
                self._fill_spin.config(to=max(1, min(count, cap)))
            except tk.TclError:
                pass
            self.fill_status.config(text=f"{count}편 중 {batch}편 채울 준비", fg=C["text_2"])
            self._fill_confirm.ask(
                f"이미지가 없거나 부족한 보관함 글 {count}편 중 {batch}편을 "
                f"{_image_fill_engine_label()} 엔진으로 채워요. "
                "로그인이 필요하고 계정당 한도가 있어요.",
                "채우기 시작", lambda: self._start_fill_batch(batch))
        except tk.TclError:
            pass
        finally:
            self._fill_running = False
            try:
                self.fill_btn.config(state="normal")
            except tk.TclError:
                pass

    def _start_fill_batch(self, n: int):
        self._fill_running = True
        try:
            self.fill_btn.config(state="disabled")
            self.fill_status.config(text=f"채우는 중… (최대 {n}편)", fg=C["accent"])
            self._fill_progress.indeterminate(f"이미지 채우는 중… (최대 {n}편)")
            self._fill_progress.pack(fill="x", pady=(SP["sm"], 0))
        except tk.TclError:
            pass
        self.spawn(self._run_fill_batch, n)

    def _run_fill_batch(self, limit: int):
        """blog_service.fill_images(limit) 를 백그라운드로 호출(발행 아님) → 요약 안내.

        core.draft_image_fill.run_fill_batch 를 직접 부르던 것을 서비스 경유로
        바꿨다(2026-09-09) — 엔진·게이트 판정이 blog_service 한 곳으로 모인다.
        2026-09-10 — 한 편이 끝날 때마다 진행(완료/전체·실패 수)을 받아 표시한다.
        콜백은 **워커 스레드**에서 오므로 Tk 는 ui_after 로만 건드린다.
        """
        summary = None
        error = None
        try:
            from core import blog_service
            summary = blog_service.fill_images(
                limit=limit, topup=_FILL_TOPUP, progress_cb=self._on_fill_progress)
        except Exception as e:  # noqa: BLE001 — 어떤 실패도 GUI 를 죽이지 않는다
            log.warning(f"[home] 이미지 채움 배치 실패: {e}")
            error = str(e)
        self.ui_after(self._after_fill, summary, error)

    def _on_fill_progress(self, event: dict):
        """채움 진행 이벤트(워커 스레드) → 메인 스레드 표시로 넘긴다."""
        try:
            done = int(event.get("done") or 0)
            total = int(event.get("total") or 0)
            failed = int(event.get("failed") or 0)
        except (TypeError, ValueError):
            return
        self.ui_after(self._apply_fill_progress, done, total, failed)

    def _apply_fill_progress(self, done: int, total: int, failed: int):
        """진행률 바·상태 라벨 갱신(메인 스레드). 위젯이 사라졌으면 조용히 무시."""
        text = f"이미지 채우는 중… {done}/{total}편"
        if failed:
            text += f" (실패 {failed}편)"
        try:
            self._fill_progress.set(done, total, text)
            self.fill_status.config(text=f"{done}/{total}편 완료"
                                         + (f" · 실패 {failed}편" if failed else ""),
                                    fg=C["accent"])
        except tk.TclError:
            pass

    # 배치 전체가 다른 프로세스의 진행 중인 채움 작업과 충돌해 거절된 사유
    # (C5 — logs/image_fill.lock). 개별 글 실패가 아니라 배치 자체가 시작조차
    # 못 한 경우이므로 일반 요약 대신 전용 안내를 준다.
    _ALREADY_RUNNING_REASONS = ("already_running", "lock_denied")

    def _after_fill(self, summary, error):
        """배치 완료 후 UI 갱신(메인 스레드) — 요약 토스트·버튼 원복·홈 새로고침."""
        self._reset_fill_button()
        try:
            self._fill_progress.pack_forget()
        except tk.TclError:
            pass
        if error is not None:
            self._toast(f"이미지 채움 중 문제가 있었어요. ({error})", kind="warn")
        else:
            summary = summary or {}
            skipped = summary.get("skipped") or {}
            if (isinstance(skipped, dict)
                    and any(skipped.get(r) for r in self._ALREADY_RUNNING_REASONS)):
                self._toast("다른 채움 작업이 진행 중이에요. 잠시 후 다시 시도하세요.",
                           kind="warn")
            else:
                self._toast(self._format_fill_summary(summary), kind="ok")
        self.refresh()

    def _reset_fill_button(self):
        """채움 버튼·상태 라벨을 기본 상태로 되돌리고 재진입 잠금을 푼다."""
        self._fill_running = False
        try:
            self.fill_btn.config(state="normal")
            self.fill_status.config(text="", fg=C["text_2"])
        except tk.TclError:
            pass

    # 사유 코드 → 사람 말. blog_service/draft_image_fill 어느 쪽이 채워도 같은
    # 사전을 쓴다(단일 진실 원본은 여전히 core — 여기는 표시 전용 번역).
    _FILL_REASON_KR = {
        "image_fill_disabled": "이미지 채움 비활성",
        "genspark_disabled": "젠스파크 비활성",
        "publish_in_progress": "발행 중이라 건너뜀",
        "rate_limited": "계정 한도 초과",
        "login_required": "로그인 필요",
        "codex_not_found": "Codex CLI를 찾을 수 없음",
        "config_broken": "Codex 설정 파일 오류",
        "no_image_produced": "생성된 이미지 없음",
        "max_calls_per_run": "이번 실행 호출 상한 도달",
        "timeout": "시간 초과",
        "no_article": "본문 없음",
        "no_images": "생성 이미지 없음",
        "no_verified_images": "검증 통과 이미지 없음",
        "save_failed": "저장 실패",
        "stale_no_longer_ready": "발행돼 건너뜀",
        "load_failed": "보관본 로드 실패",
        "draft_store_unavailable": "보관본 접근 불가",
        # C5(중복 실행 차단) — 동의어 2개, 둘 다 "다른 작업이 이미 하고 있었다"는 뜻.
        "already_running": "다른 채움 작업이 진행 중",
        "lock_denied": "다른 채움 작업이 진행 중",
        # dropped_missing 은 실패 사유가 아니라 정보(유실 경로 카운트)라 여기 넣지 않는다.
    }

    @classmethod
    def _format_fill_summary(cls, summary: dict) -> str:
        """run_fill_batch/blog_service.fill_images 요약 dict 를 사람 말로 옮긴다."""
        reason_kr = cls._FILL_REASON_KR
        filled = int(summary.get("filled", 0) or 0)
        targets = int(summary.get("targets", 0) or 0)
        lines = [f"이미지를 채운 글: {filled}편 (대상 {targets}편 중)"]
        skipped = summary.get("skipped") or {}
        if isinstance(skipped, dict) and skipped:
            parts = [f"{reason_kr.get(k, k)} {v}건" for k, v in skipped.items()]
            lines.append("건너뜀: " + " · ".join(parts))
        fail_line = cls._format_fill_failures(summary.get("results"), reason_kr)
        if fail_line:
            lines.append(fail_line)
        if summary.get("stopped_early"):
            lines.append("(계정 한도/로그인으로 조기 종료 — 나머지는 다음에 이어서)")
        return " · ".join(lines)

    @staticmethod
    def _format_fill_failures(results, reason_kr: dict) -> str:
        """개별 글 실패 사유를 최대 3건까지 요약한다(사람 말, 재시도 판단용).

        예: "실패: 3편 — 로그인 필요 2 · 시간 초과 1". ``results`` 가 없거나
        전부 성공이면 빈 문자열(추가 줄 없음).
        """
        if not isinstance(results, list) or not results:
            return ""
        failed = [r for r in results if isinstance(r, dict) and not r.get("ok", True)]
        if not failed:
            return ""
        counts: dict = {}
        for r in failed:
            reason = str(r.get("reason") or "unknown").strip() or "unknown"
            counts[reason] = counts.get(reason, 0) + 1
        top = sorted(counts.items(), key=lambda kv: -kv[1])[:3]
        shown = " · ".join(f"{reason_kr.get(k, k)} {v}건" for k, v in top)
        remainder = len(failed) - sum(v for _, v in top)
        tail = f" 외 {remainder}건" if remainder > 0 else ""
        return f"실패: {len(failed)}편 — {shown}{tail}"

    # ─────────────────────────────────────────────
    # 토스트(있으면) / messagebox(폴백)
    # ─────────────────────────────────────────────
    def _toast(self, text: str, kind: str = "info") -> None:
        toast = getattr(self.app, "toast", None)
        if toast is not None and hasattr(toast, "show"):
            try:
                toast.show(text, kind=kind)
                return
            except Exception:  # noqa: BLE001 — 토스트 실패는 messagebox 로 폴백
                pass
        try:
            if kind in ("error", "warn"):
                messagebox.showwarning("알림", text)
            else:
                messagebox.showinfo("알림", text)
        except tk.TclError:
            pass

    # ─────────────────────────────────────────────
    # [해결] 액션
    # ─────────────────────────────────────────────
    def _fix_login(self):
        """App 공용 로그인 갱신 서비스가 있으면 위임, 없으면 성장 탭 폴백."""
        start = getattr(self.app, "start_login_refresh", None)
        if callable(start):
            start(on_done=self.refresh)
            return
        growth = self.app.pages.get("growth") if hasattr(self.app, "pages") else None
        if growth is not None and hasattr(growth, "_refresh_login"):
            growth._refresh_login()
        else:
            self.app.show_tab("growth")
