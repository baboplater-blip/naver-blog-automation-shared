# gui/tabs/profiles.py — 멀티 블로그 프로필 탭 (Phase 3, 2026-07-05 / GUI 개편 2026-09-02)
"""여러 블로그(다른 계정·다른 브랜드)를 GUI 에서 관리·전환.

한 프로세스 = 한 프로필 → '전환'은 그 프로필로 앱을 새 인스턴스로 띄운다(멀티 창).
'새 블로그 추가' 마법사는 생성+자격증명(암호화)+브랜드 스캐폴드를 한 흐름으로.
로직은 gui/profile_actions.py(Tk 비의존)에 있고 여기선 화면만.

auto-dev 8호 Phase 2(2026-09-03, `docs/auto-dev/gui-design-system-v3.md`) —
상용 서비스급 리스킨. 이모지를 걷어내고 `gui.icons` 글리프로 교체, 프로필 행
컨테이너를 `theme.Card`(HAS_CTK 면 corner_radius 10 실제 카드)로 바꿨다.
"""
import tkinter as tk
from tkinter import messagebox

from gui import icons
from gui import theme as _theme
from gui.theme import (C, FB, FS, FH, SP, F, L, Sep, Card, DangerBtn,
                        SecondaryBtn, PrimaryBtn, GhostBtn)
from gui.widgets import SectionCard, Header, ScrollArea, StatusPill, FormField
from gui import profile_actions as pa
from config import profile as pf
from config import brand as br

# auto-dev 9호 Phase 1 고DPI — SP 토큰이 없는 하드코딩 px 는 여기를 거친다.
# 코더 A 의 theme.px 가 아직 없으면 항등함수 폴백(settings.py 와 동일 패턴).
px = getattr(_theme, "px", lambda n: n)


class ProfilesTab(tk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, bg=C["bg"])
        self.app = app
        self._build()

    def _build(self):
        header = Header(self, "블로그 프로필", icon=icons.glyph("profile"))
        header.pack(fill="x", padx=SP["lg"], pady=(SP["lg"], SP["sm"]))
        PrimaryBtn(header.actions, "새 블로그 추가", self._open_wizard,
                  icon="add").pack(side="right")
        Sep(self).pack(fill="x", padx=SP["lg"])

        scroll = ScrollArea(self, bg=C["bg"])
        scroll.pack(fill="both", expand=True, padx=SP["lg"], pady=SP["sm"])

        self._list_frame = tk.Frame(scroll.inner, bg=C["bg"])
        self._list_frame.pack(fill="x")
        self._render_list()

        info = SectionCard(scroll.inner, "한 프로세스 = 한 프로필",
                           icon=icons.glyph("info"))
        info.pack(fill="x", pady=(SP["sm"], SP["lg"]))
        for t in ["전환을 누르면 이 창이 그 계정으로 바뀌어요(현재 창은 닫히고 새로 열려요).",
                  "계정·로그인 정보·데이터·브랜드가 프로필마다 완전히 분리돼요.",
                  "좌측 상단 배지에 현재 발행 대상 계정이 항상 표시돼요(오발행 방지).",
                  "여러 블로그를 자동 발행하려면 스케줄러에 '멀티 프로필' 작업(순차)을 등록하세요."]:
            L(info.body, t, FS, C["text_2"]).pack(anchor="w")

    def _render_list(self):
        for w in self._list_frame.winfo_children():
            w.destroy()
        active = pf.active_profile()
        for name in pf.list_profiles():
            d = pf.describe(name)
            b = br.summary(name)
            is_active = (name == active)

            row = Card(self._list_frame)
            row.pack(fill="x", pady=(0, SP["sm"]))

            head = tk.Frame(row, bg=C["card"])
            head.pack(fill="x", padx=SP["lg"], pady=(SP["md"], 0))
            L(head, name, FH, C["text"]).pack(side="left")
            actions = tk.Frame(head, bg=C["card"])
            actions.pack(side="right")

            if is_active:
                StatusPill(actions, "현재", kind="ok").pack(side="left", padx=(0, SP["sm"]))
                if getattr(self.app, "_running", False):
                    StatusPill(actions, "실행 중 · 전환 불가",
                              kind="warn").pack(side="left", padx=(0, SP["sm"]))

            if pf.is_default(name):
                if not is_active:
                    L(actions, "계정은 설정 탭에서 변경", FS,
                      C["text_2"]).pack(side="left", padx=(0, SP["sm"]))
                    SecondaryBtn(actions, "전환",
                                lambda n=name: self._switch(n)).pack(side="left")
            else:
                if not is_active:
                    SecondaryBtn(actions, "전환",
                                lambda n=name: self._switch(n)).pack(
                        side="left", padx=(0, SP["xs"]))
                SecondaryBtn(actions, "수정",
                            lambda n=name: self._edit(n)).pack(side="left",
                                                                padx=(0, SP["xs"]))
                if not is_active:
                    DangerBtn(actions, "삭제", lambda n=name: self._delete(n),
                             icon="delete").pack(side="left")

            body = tk.Frame(row, bg=C["card"])
            body.pack(fill="x", padx=SP["lg"], pady=(SP["xs"], SP["md"]))
            acct = d["naver_id_masked"] or ("계정 없음" if not d["has_credentials"] else "-")
            L(body, f"계정 {acct}", FB, C["text_2"]).pack(anchor="w")
            L(body,
              f"브랜드 {b['display']} · 진행자 {'있음' if b['presenter_dir'] else '없음'}"
              f" · 키워드 엑셀 {'있음' if b['excel'] else '없음'}"
              f" · 로그인 정보 {'있음' if d['has_cookies'] else '없음'}",
              FS, C["text_2"]).pack(anchor="w", pady=(px(2), 0))

    def _switch(self, name: str):
        """한 창에서 계정만 바꿔 끼우기 — 선택 계정으로 새 창을 띄운 뒤 현재 창을 닫는다.

        한 프로세스=한 프로필이라 실행 중 경로를 바꿔 낄 수 없어, 재기동이 유일하게
        안전한 방법이다. 새 프로세스가 죽지 않았는지 확인한 뒤에만 현재 창을 닫는다.

        ⚠️ 발행·생성 워커가 도는 중(``app._running``)이면 전환을 **차단**한다. 전환은
        1초 뒤 ``root.destroy()`` 로 현재 창을 파괴하는데, 워커가 살아 있으면 드라이버·
        스레드가 정리되지 않은 채 창이 사라진다(다른 파괴 버튼과 동일한 가드).
        """
        if getattr(self.app, "_running", False):
            messagebox.showwarning(
                "실행 중",
                "발행·생성 등 작업이 진행 중이에요.\n"
                "완료된 뒤에 계정 전환을 다시 시도해 주세요.\n"
                "(진행 중 전환은 실행 중인 작업을 강제로 끊어요.)")
            return
        acct = pf.describe(name)["naver_id_masked"] or "-"
        if not messagebox.askyesno(
                "계정 전환",
                f"이 창을 '{name}' 계정({acct})으로 바꿔요.\n"
                f"전환하면 이 창이 닫히고 그 계정으로 다시 열려요.\n"
                f"진행 중인 작업이 있으면 먼저 멈춰 주세요. 계속할까요?"):
            return
        try:
            pf.set_active(name)
            proc = pa.relaunch_with_profile(name)
        except (OSError, RuntimeError, ValueError) as e:
            messagebox.showerror("전환 실패", f"계정을 전환하지 못했어요: {e}")
            return
        root = getattr(self.app, "root", None) or self.winfo_toplevel()

        def _finish():
            code = proc.poll()
            if code is not None and code != 0:   # 새 창이 곧바로 실패 → 현재 창 유지
                messagebox.showerror(
                    "전환 실패",
                    f"새 창이 시작되지 못했어요(코드 {code}).\n현재 창을 유지해요.")
                return
            root.destroy()                       # 새 창이 살아있으면 현재 창 닫기
        root.after(1000, _finish)

    def _edit(self, name: str):
        _EditDialog(self, name, on_done=self._render_list)

    def _delete(self, name: str):
        d = pf.describe(name)
        acct = d["naver_id_masked"] or "-"
        if not messagebox.askyesno(
                "프로필 삭제",
                f"'{name}' 블로그(계정 {acct})를 삭제해요.\n"
                f"계정·로그인 정보·글·통계 데이터가 모두 지워지며 되돌릴 수 없어요.\n\n"
                f"정말 삭제할까요?", icon="warning"):
            return
        res = pa.delete_blog(name)
        if not res.get("ok"):
            messagebox.showerror("삭제 실패", res.get("error", "알 수 없는 오류예요."))
            return
        messagebox.showinfo("삭제 완료", f"'{name}' 프로필을 삭제했어요.")
        self._render_list()

    def _open_wizard(self):
        _WizardDialog(self, on_done=self._render_list)


class _WizardDialog(tk.Toplevel):
    """새 블로그 추가 마법사 — 생성 + 자격증명(암호화) + 브랜드 스캐폴드."""

    def __init__(self, parent, on_done=None):
        super().__init__(parent)
        self.on_done = on_done
        self.title("새 블로그 추가")
        self.configure(bg=C["bg"])
        self.geometry(f"{px(480)}x{px(480)}")
        self.transient(parent)
        self._vars = {k: tk.StringVar() for k in ("name", "naver_id", "naver_pw", "brand")}
        self._migrate = tk.BooleanVar(value=False)
        self._build()

    def _build(self):
        pad = F(self, padx=px(22), pady=px(18))
        pad.pack(fill="both", expand=True)
        L(pad, "새 블로그 추가", FH, C["accent"]).pack(anchor="w", pady=(0, px(10)))

        FormField(pad, "블로그 이름", self._vars["brand"], kind="entry",
                  hint="한글도 가능해요. 예: 민쌤영어", width=32).pack(fill="x", pady=SP["xs"])
        FormField(pad, "네이버 아이디", self._vars["naver_id"], kind="entry",
                  width=32).pack(fill="x", pady=SP["xs"])
        FormField(pad, "네이버 비밀번호", self._vars["naver_pw"], kind="password",
                  show_toggle=True,
                  hint="Fernet로 암호화 저장돼요(평문 미저장).", width=32).pack(fill="x", pady=SP["xs"])
        FormField(pad, "폴더 이름", self._vars["name"], kind="entry",
                  hint="영문·숫자만 가능해요. 비우면 아이디로 자동 생성돼요.",
                  width=32).pack(fill="x", pady=SP["xs"])

        tk.Checkbutton(
            pad, text="기존(첫 블로그) 데이터를 복사해서 시작", variable=self._migrate,
            font=FS, fg=C["text"], bg=C["bg"], selectcolor=C["surface"],
            activebackground=C["bg"], activeforeground=C["text"],
            relief="flat", cursor="hand2").pack(anchor="w", pady=(SP["md"], 0))

        bar = F(pad)
        bar.pack(fill="x", pady=(SP["lg"], 0))
        PrimaryBtn(bar, "생성", self._submit).pack(side="right")
        GhostBtn(bar, "취소", self.destroy, 12).pack(side="right", padx=px(6))

    def _submit(self):
        v = {k: var.get().strip() for k, var in self._vars.items()}
        res = pa.add_blog(v["name"], v["naver_id"], v["naver_pw"],
                          migrate=self._migrate.get(), brand_name=v["brand"])
        if not res.get("ok"):
            messagebox.showerror("생성 실패", res.get("error", "알 수 없는 오류예요."))
            return
        folder = res["folder"]
        # 발행 준비 점검 리포트(무엇이 준비됐고 무엇이 남았는지 — 오류 사전 차단)
        health = res.get("health_report", "")
        messagebox.showinfo(
            "생성 완료",
            f"'{res['display']}' 생성 완료 (계정 {res['account']})\n"
            f"   폴더 이름: {folder}\n\n"
            f"다음 단계:\n"
            f"  · 이 프로필의 키워드 엑셀 폴더에 파일을 넣어 주세요.\n"
            f"  · 진행자 사진(선택)을 브랜드 폴더에 넣어 주세요.\n"
            f"  · 프로필 목록에서 '전환'으로 그 블로그 창을 여세요.\n"
            f"  · 그 창의 '네이버 로그인'으로 로그인 정보를 확보하세요.\n\n"
            f"{health}")
        if self.on_done:
            self.on_done()
        self.destroy()


class _EditDialog(tk.Toplevel):
    """프로필 수정 — 표시명(브랜드) 그리고/또는 계정(암호화 재저장).

    아이디·비번은 보안상 채워 보이지 않는다(현재 계정은 마스킹으로만 안내).
    비우면 유지, 바꾸려면 아이디·비번을 함께 입력한다.
    """

    def __init__(self, parent, name, on_done=None):
        super().__init__(parent)
        self.name = name
        self.on_done = on_done
        self.title(f"프로필 수정 — {name}")
        self.configure(bg=C["bg"])
        self.geometry(f"{px(480)}x{px(440)}")
        self.transient(parent)
        self._vars = {k: tk.StringVar() for k in ("brand", "naver_id", "naver_pw")}
        self._vars["brand"].set(br.display(name))          # 현재 표시명 프리필
        self._build()

    def _build(self):
        d = pf.describe(self.name)
        acct = d["naver_id_masked"] or "계정 없음"
        pad = F(self, padx=px(22), pady=px(18))
        pad.pack(fill="both", expand=True)
        L(pad, f"프로필 수정 — {self.name}", FH, C["accent"]).pack(anchor="w", pady=(0, SP["xs"]))
        L(pad, f"폴더 이름: {self.name} · 현재 계정: {acct}", FS,
          C["text_2"]).pack(anchor="w", pady=(0, SP["sm"]))

        FormField(pad, "블로그 이름", self._vars["brand"], kind="entry",
                  width=32).pack(fill="x", pady=SP["xs"])
        FormField(pad, "새 네이버 아이디", self._vars["naver_id"], kind="entry",
                  hint="바꿀 때만 입력하세요.", width=32).pack(fill="x", pady=SP["xs"])
        FormField(pad, "새 네이버 비밀번호", self._vars["naver_pw"], kind="password",
                  show_toggle=True,
                  hint="아이디·비번은 비우면 기존 계정을 그대로 유지해요"
                       "(바꾸려면 둘 다 입력). Fernet로 암호화 저장돼요.",
                  width=32).pack(fill="x", pady=SP["xs"])

        bar = F(pad)
        bar.pack(fill="x", pady=(SP["lg"], 0))
        PrimaryBtn(bar, "저장", self._submit).pack(side="right")
        GhostBtn(bar, "취소", self.destroy, 12).pack(side="right", padx=px(6))

    def _submit(self):
        v = {k: var.get().strip() for k, var in self._vars.items()}
        res = pa.update_blog(self.name, display=v["brand"],
                             naver_id=v["naver_id"], naver_pw=v["naver_pw"])
        if not res.get("ok"):
            messagebox.showerror("수정 실패", res.get("error", "알 수 없는 오류예요."))
            return
        messagebox.showinfo(
            "수정 완료",
            f"'{self.name}' 수정 완료 ({' · '.join(res['changed'])} 변경)\n"
            f"   표시명: {res['display']} · 계정 {res['account']}")
        if self.on_done:
            self.on_done()
        self.destroy()
