# gui/tabs/photo_post.py — 사진으로 글쓰기 탭 (P0-② Phase 2, GUI 개편 2026-09-02)
# 폴더 선택 → 백그라운드 분석/집필 → draft 미리보기 → 사용자 클릭 발행(단건).
# 발행은 오직 사용자 클릭 + 확인 모달 승인 후에만 실계정에 올라간다(하드 게이트).
import logging
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox

from gui import icons
from gui import theme as _theme_mod
from gui.async_utils import UiLifecycle
from gui.humanize import publish_error_message
from gui.theme import Ent, C, FS, SP, SecondaryBtn
from gui.tabs._publish_section import PublishSection, notify, status_kind_for_color
from gui.widgets import Header, ScrollArea, SectionCard, StatusPill, StepIndicator

# 9호 Phase 1(고DPI) — theme.px() 가 아직 없을 수 있는 병렬 개발 상황을
# 대비한 방어적 폴백(코더 A 작업 중). 있으면 UI_SCALE 배율 적용, 없으면 항등.
px = getattr(_theme_mod, "px", lambda n: n)

log = logging.getLogger(__name__)


def _photo_setting(name: str, default):
    """settings 값을 안전하게 읽는다(미로드/미정의면 기본값)."""
    try:
        from config import settings as s
        return getattr(s, name, default)
    except (ImportError, AttributeError):
        return default


def _photo_inbox_dir() -> str:
    """폴더 선택 대화상자의 기본 경로(PHOTO_INBOX_DIR). 없으면 생성(실패해도 무해)."""
    path = str(_photo_setting("PHOTO_INBOX_DIR", "") or "")
    if path:
        try:
            Path(path).mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
    return path


class PhotoPostTab(UiLifecycle, tk.Frame):
    """사진 폴더 → 실경험 글 초안 → 사용자 클릭 발행. 진입점·진행표시·발행 게이트만."""

    def __init__(self, parent, app):
        super().__init__(parent, bg=C["bg"])
        self.app = app
        self._busy = False                      # 분석/발행 워커 재진입 가드(탭 로컬)
        self._can_publish = False               # QA PASS 초안이 준비됐는가
        self._force_republish = False           # 중복 차단을 사람이 1회 해제했는가
        self._stop = threading.Event()
        self._last_preview: "Path | None" = None
        self._last_data: "dict | None" = None   # 발행 대상 data dict(QA PASS 시 보관)
        self._build()
        self._init_lifecycle()

    def _build(self):
        Header(self, "사진으로 글쓰기", icon=icons.glyph("photo"),
               subtitle="사진마다 문단을 써서 시간순 후기 초안을 만들어요.").pack(
            fill="x", padx=SP["xl"], pady=(px(10), SP["xs"]))

        # 입력→미리보기→발행 3단계 진행 표시(설계 v3 §7-6) — 폴더를 고르면
        # 2단계, 초안이 완성되면 3단계로 넘어간다(_on_pick/_run 에서 갱신).
        # 스크롤 영역 밖(항상 보임)에 고정한다.
        self.steps = StepIndicator(self, ["입력", "미리보기", "발행"])
        self.steps.pack(fill="x", padx=SP["xl"], pady=(0, SP["xs"]))

        # 최소 창(900×620)에서도 3단계 "네이버에 발행하기" 버튼이 화면 밖으로
        # 잘리지 않도록 본문을 ScrollArea 로 감싼다(마우스휠 동작·2026-09-02
        # 디자이너 캡처 심사 — 패딩도 최대한 줄여 보통은 스크롤 없이 다
        # 보이게 하되, 카드 내용이 늘어나는 경우(메뉴 목록 등)에도 도달 가능.
        scroll = ScrollArea(self)
        scroll.pack(fill="both", expand=True, padx=SP["xl"], pady=(0, px(6)))
        body = scroll.inner

        # ── 1. 사진 선택 ──────────────────────────────────────────────
        card1 = SectionCard(body, "1. 사진 선택",
                            subtitle="여행·맛집 사진 폴더를 고르면 사진마다 문단을 써요.",
                            pady=px(4))
        card1.pack(fill="x", pady=(0, px(6)))

        row = tk.Frame(card1.body, bg=C["card"])
        row.pack(fill="x", pady=(0, px(6)))
        tk.Label(row, text="제목 힌트(선택):", font=FS, fg=C["text_2"],
                 bg=C["card"]).pack(side="left", padx=(0, SP["sm"]))
        self.title_var = tk.StringVar()
        Ent(row, self.title_var, w=44).pack(side="left", padx=(SP["sm"], 0))  # v3: 테두리 있는 입력(CTkEntry)

        actions = tk.Frame(card1.body, bg=C["card"])
        actions.pack(fill="x")
        self.pick_btn = SecondaryBtn(actions, "사진 폴더 선택하기", self._on_pick,
                                     icon=icons.glyph("folder"))
        self.pick_btn.pack(side="left", padx=(0, SP["sm"]))

        # ── 2. 미리보기 ────────────────────────────────────────────────
        card2 = SectionCard(body, "2. 미리보기", subtitle="분석이 끝나면 초안을 확인해요.",
                            pady=px(4))
        card2.pack(fill="x", pady=(0, px(6)))

        self.status_pill = StatusPill(card2.body, "대기 중", kind="neutral")
        self.status_pill.pack(anchor="w")
        self.status_lbl = tk.Label(card2.body, text="폴더를 선택하세요.", font=FS,
                                   fg=C["text_2"], bg=C["card"], anchor="w",
                                   justify="left", wraplength=px(640))
        self.status_lbl.pack(anchor="w", pady=(px(2), SP["xs"]))

        self.preview_btn = SecondaryBtn(card2.body, "미리보기 열기", self._open_preview,
                                        icon=icons.glyph("search"))
        self.preview_btn.pack(anchor="w")
        self.preview_btn.config(state="disabled")

        # ── 3. 발행 ───────────────────────────────────────────────────
        self.publish = PublishSection(body, self.app, self._on_publish, pady=px(4))
        self.publish.pack(fill="x")
        # 설계서 §0.2 — 소유 탭이 기존 속성명을 그대로 노출.
        self.cat_var = self.publish.cat_var
        self.cat_combo = self.publish.cat_combo
        self.publish_btn = self.publish.publish_btn
        self.cat_hint_lbl = self.publish.cat_hint_lbl

        # PHOTO_INGEST_ENABLED=False 면 기능 자체를 잠근다(설정으로 끌 수 있게).
        if not _photo_setting("PHOTO_INGEST_ENABLED", True):
            self.pick_btn.config(state="disabled")
            self.status_lbl.config(
                text="사진으로 글쓰기 기능이 꺼져 있어요. 설정에서 켜주세요.",
                fg=C["text_2"])
            self.status_pill.set("기능 꺼짐", "neutral")

    # ── UI 헬퍼(메인 스레드 재위임 + TclError 가드) ──────────────────────
    def _set_status(self, text: str, color: str = None):
        kind = status_kind_for_color(color)

        def _apply():
            try:
                self.status_lbl.config(text=text, fg=color or C["text_2"])
            except tk.TclError:
                pass
            try:
                self.status_pill.set(_short_status(text), kind)
            except tk.TclError:
                pass
        self.ui_after(_apply)
        # 앱 공용 상태바에도(있으면) — 이 싱크는 스레드 안전.
        try:
            self.app.set_status(text, color)
        except (AttributeError, tk.TclError):
            pass

    def _refresh_actions(self):
        """분석/발행 버튼·콤보 상태를 _busy / _can_publish 로 일괄 갱신(메인 스레드)."""
        def _apply():
            busy = self._busy
            can = self._can_publish
            try:
                self.pick_btn.config(state="disabled" if busy else "normal")
            except tk.TclError:
                pass
            try:
                self.preview_btn.config(
                    state="normal" if self._last_preview else "disabled")
            except tk.TclError:
                pass
            self.publish.set_state(can, busy)
        self.ui_after(_apply)

    def _apply_menus(self, menus):
        """발행 카테고리 콤보에 활성 프로필 메뉴 목록을 채운다(메인 스레드)."""
        self.ui_after(self.publish.load_categories, list(menus))

    def _enable_preview(self):
        self.ui_after(lambda: self.preview_btn.config(state="normal"))

    # ── 분석·집필 ────────────────────────────────────────────────────
    def _on_pick(self):
        if self._busy:
            return
        if not _photo_setting("PHOTO_INGEST_ENABLED", True):
            self._set_status("사진으로 글쓰기 기능이 꺼져 있어요. 설정을 확인하세요.", C["text_2"])
            return
        folder = filedialog.askdirectory(
            title="사진 폴더 선택", initialdir=_photo_inbox_dir())
        if not folder:
            return
        self._busy = True
        self._can_publish = False           # 새 분석 시작 → 이전 발행 대상 무효화
        self._last_data = None
        self._stop.clear()
        self._refresh_actions()
        self.steps.set_active(1)             # 폴더 선택 완료 → "미리보기" 단계로
        self._set_status("사진 분석 중… (사진 1장당 수십 초 걸릴 수 있어요)", C["yellow"])
        title_hint = self.title_var.get().strip() or None
        worker = threading.Thread(
            target=self._run, args=(folder, title_hint), daemon=True)
        self._threads.append(worker)
        worker.start()

    def _run(self, folder: str, title_hint):
        """백그라운드 — 분석·집필·runs 저장. 어떤 예외도 UI 를 죽이지 않게 가드."""
        try:
            from core import experience_writer as ew
            data = ew.generate_experience_post(
                folder, title_hint=title_hint, stop_flag=self._stop)

            if data.get("_stopped"):
                self._set_status("중지했어요.", C["text_2"])
                return
            if data.get("_failed_reason") == "no_photos":
                self._set_status("이미지가 없는 폴더예요. (jpg/png 등을 넣어주세요)", C["red"])
                return

            run_dir = ew.write_run(data)
            self._last_preview = Path(run_dir) / "draft.html"
            self._enable_preview()

            n = len(data.get("image_paths") or [])
            if data.get("_qa_failed"):
                reason = str(data.get("_qa_failed_reason") or "")[:80]
                self._can_publish = False
                self._last_data = None
                self._set_status(
                    f"초안을 만들었지만(사진 {n}장) 품질 기준을 못 맞춰 발행을 보류했어요: {reason}",
                    C["orange"])
            else:
                # QA PASS — 발행 대상으로 보관하고 대상 메뉴 목록을 채운다.
                self._last_data = data
                self._can_publish = True
                try:
                    from core.photo_publish import list_target_menus
                    self._apply_menus(list_target_menus())
                except Exception:  # noqa: BLE001 — 목록 로드 실패해도 발행은 직접 입력으로 가능
                    log.debug("메뉴 목록 로드 실패", exc_info=True)
                    self._apply_menus([])
                self.ui_after(self.publish.set_status, "발행 준비됨", "neutral")
                self.ui_after(self.steps.set_active, 2)   # 초안 완료 → "발행" 단계로
                self._set_status(
                    f"초안 완성 — 사진 {n}장. 대상 메뉴를 고른 뒤 '네이버에 발행하기'를 눌러주세요.",
                    C["green"])
        except Exception as exc:  # noqa: BLE001 — 백그라운드 스레드는 절대 죽지 않음
            log.error("사진 글쓰기 실패", exc_info=True)
            self._set_status(f"실패했어요: {type(exc).__name__}: {str(exc)[:80]}", C["red"])
        finally:
            self._busy = False
            self._refresh_actions()

    def _open_preview(self):
        if self._last_preview and self._last_preview.is_file():
            try:
                webbrowser.open(self._last_preview.as_uri())
            except (OSError, ValueError) as exc:
                self._set_status(f"미리보기를 열지 못했어요: {exc}", C["red"])
        else:
            self._set_status("아직 만들어진 미리보기가 없어요.", C["text_2"])

    # ── 발행(사용자 클릭 전용, 확인 모달 게이트) ─────────────────────────
    def _on_publish(self):
        """발행 버튼 — (a) 카테고리 확인 → (b) 확인 모달 → (c) 백그라운드 발행.

        메인 스레드에서만 호출된다(버튼 콜백). 확인 '예' 직후 원자적으로 슬롯을
        점유(app._running=True + self._busy=True)해 파이프라인 동시 실행을 막는다.
        """
        if self._busy:
            return
        app = self.app
        if getattr(app, "_running", False):
            notify(app, "실행 중", "다른 작업이 진행 중이에요. 끝나면 다시 시도해 주세요.",
                  kind="warn")
            return

        data = self._last_data
        if not data or not self._can_publish \
                or data.get("_qa_failed") or data.get("_failed"):
            self._set_status(
                "발행할 초안이 없거나 품질 기준을 못 맞춘 상태예요. 먼저 초안을 만들어 주세요.",
                C["red"])
            return

        # 재진입 차단 — 확인 모달 노출 **전에** busy 선점(모달이 메인 루프를 재진입
        # 시키므로 그 사이 두 번째 클릭이 두 번째 모달을 띄우는 걸 막는다). 워커로
        # 넘기지 못한(취소·경고·실패) 모든 경로는 아래 finally 에서 busy 를 되돌린다.
        self._busy = True
        self._refresh_actions()
        handed_off = False
        try:
            from core.photo_publish import existing_image_paths

            # (a) 대상 카테고리 — 반드시 사용자가 선택/입력해야 한다.
            category = self.cat_var.get().strip()
            if not category:
                messagebox.showwarning(
                    "카테고리 선택 필요",
                    "먼저 이 글을 올릴 블로그 메뉴(카테고리)를 선택하세요.\n"
                    "선택하지 않으면 여행 글이 엉뚱한 기본 메뉴로 올라갈 수 있습니다.")
                self._set_status("발행을 취소했어요 — 대상 메뉴를 먼저 선택하세요.", C["orange"])
                return

            # 학습된 메뉴가 있는데 선택 라벨이 그 목록에 없으면 오발행 위험 → 즉시 차단
            # (드롭다운은 readonly 라 정상 경로에선 발생하지 않는 방어선). publish_photo_run
            # 도 category_unmapped 로 재차 막는다(이중 안전).
            known_menus = self.publish.known_menus()
            if known_menus and category not in known_menus:
                messagebox.showwarning(
                    "카테고리 매핑 불가",
                    f"'{category}'는 블로그 메뉴 목록에 없습니다.\n"
                    f"아래 목록에서 메뉴를 골라 주세요:\n· " + "\n· ".join(known_menus))
                self._set_status(
                    f"발행을 취소했어요 — '{category}'는 알려진 메뉴가 아니에요.", C["red"])
                return

            # (b) 확인 모달 — 되돌릴 수 없는 실발행. 모달에 표시하는 사진 장수는
            # 원본 리스트가 아니라 실제 업로드될(존재하는) 파일 수로.
            n_img = len(existing_image_paths(data))
            title = str(data.get("title") or "")[:40]
            if known_menus:
                body = (f"'{title}'\n"
                        f"사진 {n_img}장을 '[{category}]' 메뉴에 실제로 발행합니다.\n\n"
                        f"실제로 네이버에 올라가며 되돌릴 수 없습니다. 계속할까요?")
            else:
                # 학습된 메뉴 정보가 없음(첫 발행) — 기본 메뉴로 올라갈 수 있음을 명시 고지.
                body = (f"'{title}'\n"
                        f"사진 {n_img}장을 발행합니다.\n\n"
                        f"블로그 메뉴 정보가 아직 없어 이 글이 네이버 '기본 메뉴'로 "
                        f"올라갈 수 있습니다.\n발행 후 블로그에서 카테고리를 확인/이동하세요.\n\n"
                        f"실제로 네이버에 올라가며 되돌릴 수 없습니다. 계속할까요?")
            if not messagebox.askyesno(
                    "네이버에 발행 — 되돌릴 수 없음", body, icon="warning"):
                self._set_status("발행을 취소했어요.", C["text_2"])
                return

            # (c) 슬롯 점유(확인 '예' 직후 메인 스레드) → 백그라운드 워커.
            # 확인 모달이 메인 루프를 재진입시켜 그 사이 다른 진입점이 슬롯을 선점했을
            # 수 있으니, 앱의 원자적 슬롯 점유(_acquire_run_slot)로 재확인한다.
            acquire = getattr(app, "_acquire_run_slot", None)
            if callable(acquire):
                if not acquire(warn=False):
                    self._set_status("다른 작업이 시작돼 발행을 건너뛰었어요.", C["yellow"])
                    return
            else:
                app._running = True

            self._stop.clear()
            try:
                worker = threading.Thread(
                    target=self._publish_worker, args=(data, category),
                    daemon=True)
                self._threads.append(worker)
                worker.start()
                handed_off = True                   # busy 반납은 워커 finally 가 담당
            except Exception:
                app._running = False
                log.error("발행 워커 시작 실패", exc_info=True)
                raise
        finally:
            if not handed_off:
                # 워커로 넘기지 못한 모든 경로(취소·경고·슬롯실패·예외) → busy 반납.
                self._busy = False
                self._refresh_actions()

    def _publish_worker(self, data: dict, category: str):
        """백그라운드 — publish_photo_run 단건 발행. 보안 인증은 포스팅 탭 UI 재사용."""
        app = self.app
        bus = getattr(app, "bus", None)
        pt = None
        if bus is None:
            try:
                pt = app.pages.get("posting")
            except (AttributeError, KeyError, TypeError):
                pt = None

        app.auth_event.clear()
        self.ui_after(self.publish.set_status, "발행 중…", "info")

        def auth_cb(state):
            # spawn 로그인 중 보안 인증(2FA)이 뜨면 포스팅 탭의 인증완료 UI 로 유도.
            if bus is not None:
                bus.auth(state)
                if state == "waiting":
                    self._set_status(
                        "보안 인증 대기 중이에요 — 브라우저에서 인증한 뒤 "
                        "'발행·진행' 탭의 '인증 완료'를 눌러주세요.", C["orange"])
                return
            if pt is None:
                return
            if state == "waiting":
                self.ui_after(lambda: app.show_tab("posting"))
                self.ui_after(pt.show_auth_waiting)
                self._set_status(
                    "보안 인증 대기 중이에요 — 브라우저에서 인증한 뒤 "
                    "'발행·진행' 탭의 '인증 완료'를 눌러주세요.", C["orange"])
            elif state == "done":
                self.ui_after(pt.hide_auth_waiting)

        try:
            n_img = len(data.get("image_paths") or [])
            self._set_status(
                f"네이버 발행 중이에요… (사진 {n_img}장) 브라우저가 뜹니다. 잠시만요.",
                C["accent"])
            from core.photo_publish import publish_photo_run
            result = publish_photo_run(
                data, category,
                reserve_datetime=None,          # MVP — 즉시 발행(예약은 후속)
                auth_event=app.auth_event, auth_cb=auth_cb,
                stop_flag=self._stop,
                force_republish=bool(getattr(self, "_force_republish", False)))
            self._show_publish_result(result, category)
        except Exception as exc:  # noqa: BLE001 — 워커는 절대 죽지 않음
            log.error("사진 발행 워커 실패", exc_info=True)
            self._set_status(
                f"발행에 실패했어요: {type(exc).__name__}: {str(exc)[:80]}", C["red"])
            self.ui_after(self.publish.set_status, "발행 실패", "error")
        finally:
            # 1회용 재발행 확인 — 다음 클릭이 의도치 않게 중복 차단을 건너뛰지 않게
            # 시도 직후 반드시 되돌린다.
            self._force_republish = False
            app._running = False
            app.auth_event.clear()
            self._busy = False
            if pt is not None:
                self.ui_after(pt.hide_auth_waiting)
            self._refresh_actions()

    def _show_publish_result(self, result: dict, category: str):
        """발행 결과를 사람 말로 표시(성공/실패 사유)."""
        if not isinstance(result, dict):
            self._set_status("발행 결과를 확인할 수 없어요.", C["red"])
            return
        if result.get("ok"):
            n = result.get("image_count", 0)
            when = "예약 등록" if result.get("reserved") else "발행"
            # 발행 성공 → 같은 초안 재발행(중복) 방지로 버튼 잠금.
            self._can_publish = False
            self.ui_after(self.publish.set_status, "발행 완료", "ok")
            if result.get("category_resolved"):
                # 카테고리가 실제 메뉴로 해석됨 → 메뉴명 단정.
                self._set_status(
                    f"네이버에 {when} 완료 — '[{category}]' 메뉴, 사진 {n}장 업로드했어요.",
                    C["green"])
            else:
                # 카테고리 미해석 → 기본 메뉴 가능성을 정직히 고지(허위 확인 금지).
                self._set_status(
                    f"네이버에 {when} 완료 — 사진 {n}장 업로드했어요. "
                    f"카테고리가 '기본 메뉴'일 수 있으니 블로그에서 확인/이동하세요.",
                    C["orange"])
            return
        code = str(result.get("error") or "")
        msg = publish_error_message(code)
        if code == "qa_failed" and result.get("reason"):
            msg += f" (사유: {str(result['reason'])[:60]})"
        if code == "publish_blocked":
            # 실패가 아니라 "이미 올라갔을 수 있어서 막았다" — 색도 문구도 다르게 하고,
            # 사람이 블로그를 확인한 뒤 다시 올릴 수 있는 출구를 준다(막다른 길 금지).
            detail = str(result.get("message") or "")
            self.ui_after(self.publish.set_status, "발행 보류", "warn")
            self._set_status(f"{msg} ({detail})" if detail else msg, C["orange"])
            self.ui_after(self._offer_republish, result)
            return
        self.ui_after(self.publish.set_status, "발행 실패", "error")
        self._set_status(msg, C["red"])

    def _offer_republish(self, result: dict) -> None:
        """중복 차단된 글을 **사람이 확인한 뒤** 다시 올릴지 묻는다(UI 스레드)."""
        detail = str((result or {}).get("message") or "이미 발행을 시도한 기록이 있어요")
        title = str((result or {}).get("title") or "")[:40]
        if not messagebox.askyesno(
                "이미 발행을 시도한 글 — 중복 발행 주의",
                f"'{title}'\n\n{detail}.\n\n"
                "먼저 블로그에서 이 글이 올라갔는지 확인하세요.\n"
                "확인했는데도 올라가지 않았다면 '예'를 눌러 다시 발행할 수 있어요.\n"
                "이미 올라간 글이라면 '예'를 누르는 순간 같은 글이 한 번 더 올라갑니다.",
                icon="warning"):
            self._set_status("다시 발행하지 않았어요. 블로그에서 먼저 확인하세요.",
                             C["text_2"])
            return
        self._force_republish = True
        self._on_publish()


def _short_status(text: str) -> str:
    """상태 알약(짧은 태그)에 넣을 요약 — 긴 상세 문장에서 앞부분만 쓴다."""
    return text[:14] + "…" if len(text) > 14 else text
