# gui/onboarding_wizard.py — 시작하기 체크리스트 (온보딩 관문 ②④ 제거)
"""처음 앱을 켠 사용자가 "이제 뭘 하지?" 로 헤매지 않도록, 발행에 필요한 선행조건을
한 화면 체크리스트로 보여주고 각 항목을 원클릭으로 해결하게 한다.

설계(사용감 계약):
  1. 검은 CMD 없음 — 'Claude 연결' 은 앱 안 버튼 하나로(창 없는 로그인 + 완료 폴링).
     완료가 안 잡히면 검증된 수동 로그인(claude_login.bat) 폴백을 제시한다.
  2. 진단은 새로 만들지 않는다 — core.onboarding_status.check_onboarding() 이 이미
     선행조건을 판정한다. 이 마법사는 그 결과를 렌더하고 '해결' 액션을 배선만 한다.
  3. 신뢰 — 네이버 계정 옆에 "이 PC에만 암호화 저장·외부 전송 없음" 을 명시(실제
     config.crypto Fernet·로컬 저장이 근거).

스레드 규약: 상태 적재는 백그라운드 → after(0) 재위임 → TclError 가드
(home/dashboard 와 동일 패턴).

시각(GUI 개편 auto-dev 5호, 2026-09-02): SectionCard/StatusPill/Primary·
SecondaryBtn/간격 토큰으로 재도장 — 필수 2항목(claude·account, _CORE_KEYS
기준) 카드 → 선택 항목 카드(나머지 _ORDER 전부, 기본 접힘) → 신뢰 패널 →
하단 주 버튼(▶ 홈으로 가서 시작) 순. **스레드 수명·Claude 연결·상태 적재
로직은 손대지 않았다** — 바뀐 건 이 항목들을 어디에 어떤 위젯으로 그리느냐
뿐이다(_load/_safe_after/_apply/_render_rows 의 판단 로직·onboarding_status·
claude_connect 호출부는 이전과 동일).
"""
from __future__ import annotations

import logging
import threading
import tkinter as tk

from gui import icons
from gui import theme as _theme_mod
from gui.theme import (C, FB, FS, FT, SP, Btn, PrimaryBtn, SecondaryBtn,
                        SubtleBtn)
from gui.widgets import Header, SectionCard, StatusPill

# 9호 Phase 1(고DPI) — theme.px() 가 아직 없을 수 있는 병렬 개발 상황을
# 대비한 방어적 폴백(코더 A 작업 중). 있으면 UI_SCALE 배율 적용, 없으면 항등.
px = getattr(_theme_mod, "px", lambda n: n)

log = logging.getLogger(__name__)

# 핵심(필수) 항목 — 이게 채워지면 '준비 완료'. 나머지는 선택.
_CORE_KEYS = ("claude", "account")
# 마법사가 다루는 표시 순서. onboarding_status 의 key 와 일치.
_ORDER = ("claude", "account", "excel", "cookie", "search_api")
_OPTIONAL_KEYS = ("cookie", "search_api")

# 각 항목의 '해결' 버튼 라벨(없으면 탭 이동만).
_ACTION_LABEL = {
    "claude": "연결하기",
    "account": "설정에서 입력",
    "excel": "발행 라인 고르기",
    "cookie": "네이버 로그인",
    "search_api": "설정에서 입력",
}


class OnboardingWizard(tk.Toplevel):
    def __init__(self, parent, app):
        super().__init__(parent, bg=C["bg"])
        self.app = app
        self._connecting = False
        self._poll_left = 0
        # ⚠️ 스레드 수명 관리(2026-09-02, 실측 faulthandler 크래시 수리) — refresh()가
        # 띄우는 _load 백그라운드 스레드의 핸들을 보관해 호출자(테스트 등)가 파괴 전에
        # join 할 수 있게 하고, 파괴 여부를 플래그로 남겨 파괴 후에는 그 스레드가
        # self.after()로 Tk 를 재위임하지 않도록(크로스 스레드 Tcl 접근 회피) 막는다.
        self._load_thread = None
        self._destroyed = False
        # _destroyed 세팅(destroy)과 _safe_after 의 체크→after() 사이 TOCTOU 레이스
        # 봉합용 락 — 파괴가 시작되면 이후 어떤 백그라운드 재위임도 통과 못 한다.
        self._lifecycle_lock = threading.Lock()
        self.title("시작하기")
        self.geometry(f"{px(620)}x{px(700)}")
        self.minsize(px(560), px(600))
        try:
            self.transient(parent)
        except tk.TclError:
            pass
        self._build_ui()
        self._refresh_after_id = self.after(200, self.refresh)

    # ── UI 골격 ─────────────────────────────────────────
    def _build_ui(self):
        body = tk.Frame(self, bg=C["bg"])
        body.pack(fill="both", expand=True)

        header = Header(body, "시작하기", subtitle="두 가지만 연결하면 바로 써요")
        header.pack(fill="x", padx=SP["xl"], pady=(SP["lg"], SP["sm"]))

        # 준비 완료 배너(코어 충족 시 노출) — 자리는 여기서 만들되 기본은 숨김.
        # required 카드 바로 위에 재삽입할 수 있도록 self._required_card 를
        # 앵커로 쓴다(창 자식 인덱스에 의존하지 않는 안정적 위치 지정).
        self._done_banner = tk.Frame(body, bg=C["success_bg"])
        tk.Frame(self._done_banner, width=px(4), bg=C["green"]).pack(side="left", fill="y")
        done_body = tk.Frame(self._done_banner, bg=C["success_bg"])
        done_body.pack(side="left", fill="both", expand=True,
                       padx=SP["sm"], pady=SP["sm"])
        self._done_title = tk.Label(done_body, text="", font=FT, fg=C["green"],
                                    bg=C["success_bg"], anchor="w")
        self._done_title.pack(anchor="w")
        self._done_body = tk.Label(done_body, text="", font=FB, fg=C["text_2"],
                                   bg=C["success_bg"], anchor="w", justify="left",
                                   wraplength=px(460))
        self._done_body.pack(anchor="w", pady=(SP["xs"], 0))
        # 처음엔 pack 하지 않는다 — winfo_ismapped() 가 그대로 표시 상태 신호.

        # 필수 항목(claude·account) — _CORE_KEYS 기준.
        self._required_card = SectionCard(
            body, "필수 항목", subtitle="이 두 가지는 발행에 꼭 필요해요",
            icon=icons.glyph("success"))
        self._required_card.pack(fill="x", padx=SP["xl"], pady=(SP["sm"], 0))
        self._rows_wrap = self._required_card.body
        self._row_widgets = {}   # key -> dict(card, pill)

        # 선택 항목(나머지 _ORDER 전부) — 기본 접힘.
        self._optional_card = SectionCard(
            body, "선택 항목", subtitle="없어도 되지만 있으면 더 정확해져요",
            icon=icons.glyph("settings"))
        self._optional_card.pack(fill="x", padx=SP["xl"], pady=(SP["sm"], 0))
        self._optional_rows_wrap = self._optional_card.body
        self._optional_open = False
        self._optional_toggle_btn = SubtleBtn(
            self._optional_card.actions, "펼치기 ▾", self._toggle_optional)
        self._optional_toggle_btn.pack(side="right")
        self._optional_rows_wrap.pack_forget()   # 기본 접힘

        # 신뢰 패널
        trust = SectionCard(body, "안심하세요")
        trust.pack(fill="x", padx=SP["xl"], pady=(SP["sm"], 0))
        tk.Label(trust.body,
                text="네이버 아이디·비밀번호는 이 PC에만 암호화 저장되고, 외부 서버로 전송되지 않아요.",
                font=FS, fg=C["text_2"], bg=C["card"], justify="left",
                wraplength=px(520)).pack(anchor="w")

        # 연결 진행 상태 라벨(Claude 로그인 폴링용)
        self._conn_status = tk.Label(body, text="", font=FS, fg=C["muted"], bg=C["bg"])
        self._conn_status.pack(anchor="w", padx=SP["xl"], pady=(SP["sm"], 0))

        # 하단 버튼 — 화면당 주 버튼(Primary) 1개 원칙, 나머지는 SubtleBtn.
        bf = tk.Frame(body, bg=C["bg"])
        bf.pack(fill="x", padx=SP["xl"], pady=SP["lg"])
        SubtleBtn(bf, "나중에 하기", self._close).pack(side="left")
        SubtleBtn(bf, "새로고침", self.refresh, icon=icons.glyph("refresh")).pack(
            side="left", padx=(SP["sm"], 0))
        self._start_btn = PrimaryBtn(bf, "홈으로", self._finish, icon=icons.glyph("run"))
        self._start_btn.pack(side="right")

    def _toggle_optional(self):
        """선택 항목 카드 접힘/펼침 — 위젯을 파괴하지 않고 pack/pack_forget 만."""
        self._optional_open = not self._optional_open
        if self._optional_open:
            self._optional_rows_wrap.pack(fill="both", expand=True,
                                          padx=SP["lg"], pady=(SP["sm"], SP["md"]))
            self._optional_toggle_btn.config(text="접기 ▴")
        else:
            self._optional_rows_wrap.pack_forget()
            self._optional_toggle_btn.config(text="펼치기 ▾")

    # ── 상태 적재 ───────────────────────────────────────
    def refresh(self):
        self._load_thread = threading.Thread(target=self._load, daemon=True)
        self._load_thread.start()

    def _load(self):
        try:
            from core.onboarding_status import check_onboarding
            data = check_onboarding()
        except Exception as e:  # noqa: BLE001
            log.debug("[onboarding] check 실패: %s", e)
            data = None
        self._safe_after(self._apply, data)

    def _safe_after(self, fn, *args):
        # 파괴 후에는 Tk 호출 자체를 시도하지 않는다 — 백그라운드 스레드에서
        # 파괴된(또는 파괴 중인) 위젯의 self.after()를 부르면 크로스 스레드 Tcl
        # 접근이 되어 네이티브 크래시로 이어질 수 있다(실측 확인, 2026-09-02).
        with self._lifecycle_lock:
            if self._destroyed:
                return
            try:
                self.after(0, fn, *args)
            except (tk.TclError, RuntimeError):
                pass

    # ── 화면 반영(메인 스레드) ──────────────────────────
    def _apply(self, data):
        try:
            self._render_rows(data)
            self._render_done_banner(data)
        except tk.TclError:
            pass

    def _render_rows(self, data):
        for w in self._rows_wrap.winfo_children():
            w.destroy()
        for w in self._optional_rows_wrap.winfo_children():
            w.destroy()
        self._row_widgets = {}
        items = {it["key"]: it for it in (data or {}).get("items", [])}
        for key in _ORDER:
            it = items.get(key)
            if it is None:
                continue
            wrap = self._rows_wrap if key in _CORE_KEYS else self._optional_rows_wrap
            self._make_row(wrap, key, it)

    def _make_row(self, wrap, key: str, it: dict):
        ok = it.get("ok")
        optional = key in _OPTIONAL_KEYS
        card = tk.Frame(wrap, bg=C["card"])
        card.pack(fill="x", pady=(0, SP["sm"]))

        top = tk.Frame(card, bg=C["card"])
        top.pack(fill="x")
        kind = "ok" if ok is True else ("neutral" if ok is None else "warn")
        pill_text = "완료" if ok is True else ("확인 불가" if ok is None else "확인 필요")
        pill = StatusPill(top, pill_text, kind=kind)
        pill.pack(side="left")
        title = it.get("label", key) + ("  (선택)" if optional else "")
        tk.Label(top, text=title, font=FT, fg=C["text"], bg=C["card"]).pack(
            side="left", padx=(SP["sm"], 0))

        # 오른쪽 액션 버튼 — 이미 충족(ok=True)이면 버튼 없음(완료 알약이 대신함).
        if ok is not True:
            label = _ACTION_LABEL.get(key, "설정 열기")
            cmd = self._action_for(key, it)
            SecondaryBtn(top, label, cmd).pack(side="right")

        hint = it.get("hint", "")
        if hint and ok is not True:
            tk.Label(card, text=hint, font=FS, fg=C["text_2"], bg=C["card"],
                    justify="left", wraplength=px(440)).pack(anchor="w", pady=(SP["xs"], 0))

        self._row_widgets[key] = {"card": card, "pill": pill}

    def _action_for(self, key: str, it: dict):
        if key == "claude":
            return self._connect_claude
        tab = it.get("tab", "settings")
        return lambda t=tab: self._go_tab(t)

    def _render_done_banner(self, data):
        items = {it["key"]: it for it in (data or {}).get("items", [])}
        core_ok = all(items.get(k, {}).get("ok") is True for k in _CORE_KEYS)
        # winfo_ismapped() 는 조상 체인(Toplevel 포함)까지 실제 화면에 매핑돼야
        # True 라 withdraw() 된 창(테스트·초기 생성 직후)에서는 항상 False —
        # "이미 pack 돼 있는가"는 winfo_manager() 로 물어야 정확하다.
        already_shown = self._done_banner.winfo_manager() == "pack"
        if core_ok:
            self._done_title.config(text="준비 완료!")
            self._done_body.config(
                text="이제 홈의 '오늘 실행' 버튼 하나면 발행이 시작돼요.")
            if not already_shown:
                self._done_banner.pack(fill="x", padx=SP["xl"], pady=(0, SP["sm"]),
                                       before=self._required_card)
            self._start_btn.config(text="홈으로 가서 시작", bg=C["green"])
        else:
            if already_shown:
                self._done_banner.pack_forget()
            self._start_btn.config(text="홈으로", bg=C["accent"])

    # ── Claude 연결(창 없는 로그인 + 완료 폴링) ─────────
    def _connect_claude(self):
        if self._connecting:
            return
        try:
            from core import claude_connect
        except Exception:  # noqa: BLE001
            self._set_conn("연결 모듈을 불러오지 못했어요.", C["red"])
            return

        # 이미 로그인돼 있으면 즉시 반영
        if claude_connect.is_authenticated():
            self._set_conn("이미 연결돼 있어요", C["green"])
            self.refresh()
            return

        if not claude_connect.is_installed():
            # CLI 미설치 → Claude Max 가입/설치 안내
            self._set_conn(
                "Claude가 설치돼 있지 않아요. 설치(setup) 후 다시 시도하세요.", C["yellow"])
            self._offer_manual(claude_connect)
            return

        proc = claude_connect.start_login()
        if proc is None:
            self._set_conn("연결 실행에 실패했어요. 수동 로그인을 시도하세요.", C["yellow"])
            self._offer_manual(claude_connect)
            return

        self._connecting = True
        self._poll_left = 60   # 60 × 2s = 최대 120초
        self._set_conn("브라우저에서 로그인하세요 (자동으로 열려요) · 확인 중…",
                       C["accent"])
        self._poll_connect(claude_connect, proc)

    def _poll_connect(self, claude_connect, proc):
        try:
            if claude_connect.is_authenticated():
                self._connecting = False
                self._set_conn("연결됐어요!", C["green"])
                self.refresh()
                return
            self._poll_left -= 1
            if self._poll_left <= 0:
                self._connecting = False
                self._set_conn(
                    "아직 완료가 확인되지 않았어요. 브라우저 로그인을 마쳤다면 새로고침, "
                    "안 열렸다면 수동 로그인을 눌러주세요.", C["yellow"])
                self._offer_manual(claude_connect)
                return
            # 폴링 after-id 를 보관해 destroy() 가 취소할 수 있게 한다(2026-09-02 5호
            # 검증조 발견: 연결 폴링 중 창을 닫으면 타이머가 파괴된 위젯에서 발화).
            self._poll_after_id = self.after(2000, self._poll_connect, claude_connect, proc)
        except tk.TclError:
            self._connecting = False

    def _offer_manual(self, claude_connect):
        """수동 로그인(claude_login.bat) 폴백 버튼을 연결 상태 줄 옆에 붙인다."""
        try:
            bar = self._conn_status.master
            btn = Btn(bar, "수동 로그인", lambda: claude_connect.open_manual_login(),
                      12, color=C["yellow"])
            btn.pack(anchor="w", padx=SP["xl"], pady=(SP["xs"], 0))
        except tk.TclError:
            pass

    def _set_conn(self, text: str, color: str):
        try:
            self._conn_status.config(text=text, fg=color)
        except tk.TclError:
            pass

    # ── 탐색·종료 ───────────────────────────────────────
    def _go_tab(self, tab: str):
        try:
            if hasattr(self.app, "show_tab"):
                self.app.show_tab(tab)
        except Exception:  # noqa: BLE001
            pass

    def _finish(self):
        self._go_tab("home")
        self._close()

    def _close(self):
        try:
            self.destroy()
        except tk.TclError:
            pass

    def destroy(self):
        """위젯 파괴 — 잔존 _load 스레드가 파괴 후 Tk 를 건드리지 않게 먼저 표시.

        예약된 초기 refresh 타이머(self._refresh_after_id)도 취소해, 파괴 이후에
        (어떤 경로로든 이벤트 루프가 다시 도는 경우) refresh()가 새 스레드를 또
        띄우는 일이 없게 한다. 라이브 동작은 기존과 동일(그냥 창을 닫는 것) —
        추가된 건 파괴 후 안전장치뿐이다.
        """
        with self._lifecycle_lock:
            self._destroyed = True
        for attr in ("_refresh_after_id", "_poll_after_id"):
            try:
                self.after_cancel(getattr(self, attr))
            except (tk.TclError, ValueError, AttributeError):
                pass
        try:
            super().destroy()
        except tk.TclError:
            pass
