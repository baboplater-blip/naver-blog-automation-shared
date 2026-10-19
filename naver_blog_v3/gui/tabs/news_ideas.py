"""프로필별 자동 주제 설정과 수동 글감 검토 패널."""
from __future__ import annotations

import ipaddress
import logging
from pathlib import Path
import threading
import webbrowser
from typing import Any, Callable, Optional
from tkinter import filedialog, messagebox, ttk
from urllib.parse import urlsplit
import tkinter as tk

from gui.async_utils import UiLifecycle
from gui.theme import C, FB, FS, SP, GhostBtn, PrimaryBtn, SecondaryBtn, L
from gui.widgets import SectionCard

log = logging.getLogger(__name__)

NASA_JPL_FEED = {
    "name": "NASA JPL 뉴스",
    "url": "https://www.jpl.nasa.gov/feeds/news/",
    "enabled": False,
}
GOOGLE_TRENDS_HELP = "Google Trends RSS는 사용자가 공식 내보내기 URL을 입력한 뒤 등록할 수 있어요."
MAX_CSV_BYTES = 2 * 1024 * 1024


def _service() -> Any:
    """뉴스 서비스는 패널이 실제 작업을 시작할 때만 가져온다."""
    from core import news_topics
    return news_topics


def _active_profile() -> str:
    from config import profile
    return profile.active_profile()


def _safe_http_url(value: str) -> str:
    """사용자가 명시적으로 여는 원문 URL에서 로컬·비 HTTP 대상을 거른다."""
    if not isinstance(value, str) or not value.strip() or len(value) > 2048:
        raise ValueError("원문 URL이 비어 있거나 너무 깁니다.")
    parsed = urlsplit(value.strip())
    host = (parsed.hostname or "").rstrip(".").lower()
    if parsed.scheme.lower() not in {"http", "https"} or not host or parsed.username or parsed.password:
        raise ValueError("http 또는 https 원문 URL만 열 수 있어요.")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("원문 URL 포트가 올바르지 않아요.") from exc
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".test")):
        raise ValueError("로컬 주소는 열 수 없어요.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and (not address.is_global):
        raise ValueError("공개 IP 주소만 열 수 있어요.")
    return value.strip()


def parse_interests(text: str) -> list:
    """입력된 관심 주제를 쉼표·줄바꿈 기준으로 정리한다."""
    if not isinstance(text, str):
        return []
    return [part.strip() for line in text.splitlines() for part in line.split(",")
            if part.strip()]


def create_draft_for_card(service: Any, card: dict, title: str,
                          reader_question: str, profile: str) -> dict:
    """사용자가 검토한 한 카드만 명시 인자로 초안 서비스에 전달한다."""
    if not isinstance(card, dict) or not card.get("id"):
        raise ValueError("선택한 글감에 식별자가 없어요.")
    if not title.strip() or not reader_question.strip():
        raise ValueError("제목과 독자 질문을 모두 확인해 주세요.")
    return service.create_draft(card, title=title.strip(),
                                reader_question=reader_question.strip(),
                                profile=profile)


def _date_label(card: dict) -> str:
    """게시 시각과 행사·시행 날짜를 혼동하지 않도록 각각 표기한다."""
    provider_time = card.get("provider_time") or "게시 시각 미확인"
    if card.get("kind") == "event":
        start = card.get("event_start") or "시작 미확인"
        end = card.get("event_end") or "종료 미확인"
        return f"게시 {provider_time} · 행사 {start} ~ {end}"
    if card.get("effective_date"):
        return f"게시 {provider_time} · 시행 {card['effective_date']}"
    return f"게시 {provider_time} · 시행일 미확인"


class NewsIdeasPanel(UiLifecycle, tk.Frame):
    """수동 수집·검토·단건 보관 UI."""

    def __init__(self, parent: tk.Misc, app: Any) -> None:
        tk.Frame.__init__(self, parent, bg=C["card"])
        self.app = app
        self._init_lifecycle()
        self._busy_lock = threading.Lock()
        self._busy = False
        self._cards: list = []
        self._feeds: list = []
        self._selection_stale = False
        self._ignore_selection_id = None
        self._profile = _active_profile()
        self._build()
        self._load_cache()

    def _build(self) -> None:
        self._interests = tk.StringVar()
        self._region = tk.StringVar()
        self._recent_days = tk.StringVar(value="1")
        self._naver_enabled = tk.BooleanVar(value=False)
        self._tour_enabled = tk.BooleanVar(value=False)
        self._seoul_enabled = tk.BooleanVar(value=False)
        self._auto_enabled = tk.BooleanVar(value=False)
        self._feed_name = tk.StringVar()
        self._feed_url = tk.StringVar()
        self._manual_url = tk.StringVar()
        self._title = tk.StringVar()
        self._question = tk.StringVar()

        prefs = SectionCard(self, "오늘의 글감", "자동 주제를 한 번 설정하면 공통 자동 글쓰기에서 분야에 맞는 글감을 골라요. 카드는 선택 검토용입니다.")
        prefs.pack(fill="x", padx=SP["md"], pady=(SP["md"], SP["sm"]))
        L(prefs.body, "관심 주제 (쉼표 또는 줄바꿈으로 구분)", FS, C["text"]).pack(anchor="w")
        self._interest_entry = tk.Text(prefs.body, height=2, wrap="word", font=FS,
                                       bg=C["surface"], fg=C["text"], relief="flat")
        self._interest_entry.pack(fill="x", pady=(2, SP["sm"]))
        row = tk.Frame(prefs.body, bg=C["card"])
        row.pack(fill="x")
        L(row, "지역", FS, C["text"]).pack(side="left")
        ttk.Entry(row, textvariable=self._region, width=18).pack(side="left", padx=(6, 16))
        L(row, "최근", FS, C["text"]).pack(side="left")
        ttk.Combobox(row, textvariable=self._recent_days, values=("1", "7"),
                     width=4, state="readonly").pack(side="left", padx=6)
        L(row, "일", FS, C["text"]).pack(side="left")
        self._switch(row, "Naver API", self._naver_enabled).pack(side="left", padx=(16, 0))
        self._switch(row, "TourAPI", self._tour_enabled).pack(side="left", padx=(8, 0))
        self._seoul_switch = self._switch(row, "서울 API(보류)", self._seoul_enabled)
        self._seoul_switch.configure(state="disabled")
        self._seoul_switch.pack(side="left", padx=(8, 0))
        self._switch(row, "자동 주제 선정 사용", self._auto_enabled).pack(side="left", padx=(8, 0))
        L(prefs.body, "서울 문화행사 API 보류 · 공식 CSV 가져오기를 이용하세요.",
          FS, C["yellow"]).pack(anchor="w", pady=(2, 0))

        feed_row = tk.Frame(prefs.body, bg=C["card"])
        feed_row.pack(fill="x", pady=(SP["sm"], 2))
        ttk.Entry(feed_row, textvariable=self._feed_name, width=20).pack(side="left", padx=(0, 5))
        ttk.Entry(feed_row, textvariable=self._feed_url).pack(side="left", fill="x", expand=True)
        SecondaryBtn(feed_row, "피드 등록/수정", self._save_feed).pack(side="left", padx=(5, 0))
        self._feed_tree = ttk.Treeview(prefs.body, columns=("name", "url", "enabled"),
                                       show="headings", height=3, selectmode="browse")
        self._feed_tree.bind("<<TreeviewSelect>>", self._select_feed)
        for col, title, width in (("name", "공개 피드", 150), ("url", "URL", 400),
                                  ("enabled", "수집", 55)):
            self._feed_tree.heading(col, text=title)
            self._feed_tree.column(col, width=width, anchor="w")
        self._feed_tree.pack(fill="x", pady=(0, 4))
        feed_actions = tk.Frame(prefs.body, bg=C["card"])
        feed_actions.pack(fill="x")
        GhostBtn(feed_actions, "선택 피드 켜기/끄기", self._toggle_feed).pack(side="left")
        GhostBtn(feed_actions, "선택 피드 삭제", self._delete_feed).pack(side="left", padx=5)
        GhostBtn(feed_actions, "NASA JPL 공식 피드 추가(꺼짐)", self._add_nasa_feed).pack(side="left")
        L(prefs.body, GOOGLE_TRENDS_HELP, FS, C["muted"]).pack(anchor="w", pady=(4, 0))

        controls = tk.Frame(self, bg=C["card"])
        controls.pack(fill="x", padx=SP["md"], pady=(0, SP["sm"]))
        self._collect_btn = PrimaryBtn(controls, "오늘의 글감 수집", self._collect)
        self._collect_btn.pack(side="left")
        SecondaryBtn(controls, "자동 선정 설정 저장", self._save_auto_settings).pack(side="left", padx=6)
        SecondaryBtn(controls, "Google Trends/서울 CSV 가져오기", self._import_csv).pack(side="left", padx=6)
        ttk.Entry(controls, textvariable=self._manual_url, width=42).pack(side="left", padx=(8, 4))
        SecondaryBtn(controls, "원문 URL 등록", self._add_url).pack(side="left")
        self._status = L(self, "캐시 확인 중", FS, C["muted"])
        self._status.pack(fill="x", padx=SP["md"], pady=(0, 4))

        cards = SectionCard(self, "수집한 글감", "선택한 자료만 제목과 질문을 다듬어 보관할 수 있어요.")
        cards.pack(fill="both", expand=True, padx=SP["md"], pady=(0, SP["md"]))
        self._card_tree = ttk.Treeview(cards.body, columns=("kind", "title", "date"),
                                       show="headings", height=5, selectmode="browse")
        for col, title, width in (("kind", "종류", 75), ("title", "제목", 350), ("date", "시각 / 일정", 390)):
            self._card_tree.heading(col, text=title)
            self._card_tree.column(col, width=width, anchor="w")
        self._card_tree.pack(fill="both", expand=True)
        self._card_tree.bind("<<TreeviewSelect>>", self._select_card)
        self._detail = tk.Text(cards.body, height=5, wrap="word", font=FS, state="disabled",
                               bg=C["surface"], fg=C["text"], relief="flat")
        self._detail.pack(fill="x", pady=(5, 4))
        edit = tk.Frame(cards.body, bg=C["card"])
        edit.pack(fill="x")
        ttk.Entry(edit, textvariable=self._title).pack(fill="x", pady=(0, 4))
        ttk.Entry(edit, textvariable=self._question).pack(fill="x")
        actions = tk.Frame(cards.body, bg=C["card"])
        actions.pack(fill="x", pady=(5, 0))
        GhostBtn(actions, "선택 원문 열기", self._open_source).pack(side="left")
        self._draft_btn = PrimaryBtn(actions, "초안 작성·보관", self._create_draft)
        self._draft_btn.pack(side="right")

    @staticmethod
    def _switch(parent: tk.Misc, text: str,
                variable: tk.BooleanVar) -> tk.Checkbutton:
        return tk.Checkbutton(parent, text=text, variable=variable, bg=C["card"],
                              fg=C["text"], activebackground=C["card"], selectcolor=C["surface"])

    def _collect_preferences(self) -> dict:
        text = self._interest_entry.get("1.0", "end-1c")
        interests = parse_interests(text)
        return {"interests": interests, "region": self._region.get().strip(),
                "feeds": [dict(feed) for feed in self._feeds],
                "naver_enabled": self._naver_enabled.get(),
                "tour_enabled": self._tour_enabled.get(),
                # HTTP-only provider would expose its key in plaintext. Never allow a saved
                # legacy preference to re-enable the adapter through this GUI collection path.
                "seoul_enabled": False,
                "recent_days": int(self._recent_days.get()), "upcoming_days": 30,
                "max_articles": 120, "auto_enabled": self._auto_enabled.get()}

    def _load_cache(self) -> None:
        try:
            svc = _service()
            preferences = svc.load_preferences(profile=self._profile)
            self._set_preferences(preferences)
            snapshot = svc.load_snapshot(profile=self._profile)
            self._show_result(snapshot if isinstance(snapshot, dict) else {})
            from core import auto_topics
            last = auto_topics.last_selection(self._profile)
            if last:
                basis = "상시 주제" if last.get("basis") == "evergreen" else "최신 자료"
                self._status.config(text=f"최근 자동 선정: {last.get('title', '')} · {basis} · {last.get('selected_because', '')}")
        except Exception as exc:
            log.error("오늘의 글감 캐시 로드 실패", exc_info=True)
            self._status.config(text=f"캐시를 읽지 못했어요: {exc}", fg=C["red"])

    def _set_preferences(self, preferences: dict) -> None:
        self._interest_entry.delete("1.0", "end")
        self._interest_entry.insert("1.0", "\n".join(preferences.get("interests", [])))
        self._region.set(preferences.get("region", ""))
        self._recent_days.set(str(preferences.get("recent_days", 1)))
        self._naver_enabled.set(bool(preferences.get("naver_enabled", False)))
        self._tour_enabled.set(bool(preferences.get("tour_enabled", False)))
        # Preserve the stored value until an explicit preference save, but never present or
        # collect through the source while HTTPS support remains unverified.
        self._seoul_enabled.set(False)
        self._auto_enabled.set(bool(preferences.get("auto_enabled", False)))
        self._feeds = [dict(feed) for feed in preferences.get("feeds", [])]
        self._render_feeds()

    def _save_preferences(self, preferences: Optional[dict] = None,
                          profile: Optional[str] = None) -> str:
        profile = profile or self._capture_profile()
        values = preferences if preferences is not None else self._collect_preferences()
        result = _service().save_preferences(values, profile=profile)
        if not result.get("saved"):
            raise RuntimeError(result.get("reason") or "설정을 저장하지 못했어요.")
        return profile

    def _capture_profile(self) -> str:
        current = _active_profile()
        if current != self._profile:
            raise RuntimeError("프로필이 바뀌었어요. 화면을 다시 열어 주세요.")
        return current

    def _start(self, button: Any, label: str, work: Callable[[], Any]) -> None:
        with self._busy_lock:
            if self._busy or self.is_destroyed():
                return
            self._busy = True
        original = button.cget("text")
        button.configure(state="disabled", text=label)
        self._status.config(text="작업 중…", fg=C["muted"])

        def worker() -> None:
            try:
                result = work()
                self.ui_after(self._finish, button, original, result, None)
            except Exception as exc:
                log.error("오늘의 글감 작업 실패", exc_info=True)
                self.ui_after(self._finish, button, original, None, str(exc))

        self.spawn(worker, name="news-ideas-operation")

    def _finish(self, button: Any, original: str, result: Any,
                error: Optional[str]) -> None:
        if self.is_destroyed():
            return
        with self._busy_lock:
            self._busy = False
        try:
            button.configure(state="normal", text=original)
        except tk.TclError:
            return
        if error is None:
            try:
                if _active_profile() != self._profile:
                    self._status.config(text="프로필이 바뀌어 이전 결과는 표시하지 않았어요.", fg=C["yellow"])
                    return
            except (ImportError, OSError, RuntimeError) as exc:
                log.error("글감 결과 프로필 확인 실패", exc_info=True)
                self._status.config(text=f"프로필을 확인하지 못했어요: {exc}", fg=C["red"])
                return
        if error:
            self._status.config(text=f"실패: {error}", fg=C["red"])
        elif isinstance(result, dict):
            if "items" in result:
                self._show_result(result)
            else:
                slug = result.get("slug")
                status = result.get("reason") or (f"초안을 보관했어요: {slug}" if slug else "완료")
                self._status.config(text=status, fg=C["green"] if result.get("saved") else C["red"])

    def _collect(self) -> None:
        try:
            profile = self._capture_profile()
            preferences = self._collect_preferences()
        except RuntimeError as exc:
            self._status.config(text=str(exc), fg=C["red"])
            return
        def work():
            self._save_preferences(preferences, profile)
            return _service().collect_topics(preferences=preferences, profile=profile)
        self._start(self._collect_btn, "수집 중…", work)

    def _save_auto_settings(self) -> None:
        try:
            profile = self._capture_profile()
            preferences = self._collect_preferences()
            self._save_preferences(preferences, profile)
        except (RuntimeError, ValueError, TypeError) as exc:
            self._status.config(text=f"설정을 저장하지 못했어요: {exc}", fg=C["red"])
            return
        state = "켜짐" if preferences["auto_enabled"] else "꺼짐"
        self._status.config(text=f"자동 주제 선정 설정을 저장했어요 · {state} · 매일 카드 선택 없이 자동 글쓰기에서 사용됩니다.",
                            fg=C["green"])

    def _show_result(self, result: dict) -> None:
        prior_card_id = None
        prior_title, prior_question = self._title.get(), self._question.get()
        prior_selection = self._card_tree.selection()
        if prior_selection:
            prior_card_id = self._cards[int(prior_selection[0])].get("id")
        self._cards = [card for card in result.get("items", []) if isinstance(card, dict)]
        self._card_tree.delete(*self._card_tree.get_children())
        for index, card in enumerate(self._cards):
            stale_card = bool(card.get("stale") or result.get("stale"))
            kind_label = "행사" if card.get("kind") == "event" else "뉴스"
            if stale_card:
                kind_label += " · 오래됨"
            self._card_tree.insert("", "end", iid=str(index), values=(
                kind_label, card.get("title", "제목 없음"),
                _date_label(card)))
        errors = result.get("errors") or []
        collected_at = result.get("collected_at") or "수집 시각 미확인"
        attempted_at = result.get("attempted_at")
        suffix = f" · 오류 {len(errors)}건" if errors else ""
        stale = " · 오래된 캐시" if result.get("stale") else ""
        age_seconds = result.get("cache_age_seconds")
        age_label = ""
        if isinstance(age_seconds, (int, float)) and age_seconds >= 0:
            if age_seconds >= 86400:
                age_label = f" · 캐시 {int(age_seconds // 86400)}일 전"
            elif age_seconds >= 3600:
                age_label = f" · 캐시 {int(age_seconds // 3600)}시간 전"
        detail = " / ".join(
            f"{error.get('source', '소스')}: {error.get('message', '오류')}"
            for error in errors[:2] if isinstance(error, dict))
        if len(errors) > 2:
            detail += f" 외 {len(errors) - 2}건"
        text = f"마지막 수집: {collected_at} · 글감 {len(self._cards)}건{stale}{age_label}{suffix}"
        if attempted_at and attempted_at != collected_at:
            text += f" · 최근 시도: {attempted_at}"
        if detail:
            text += f" · {detail}"
        self._status.config(text=text,
                            fg=C["yellow"] if errors or result.get("stale") else C["muted"])
        if self._cards:
            selected_index = next((str(index) for index, card in enumerate(self._cards)
                                   if prior_card_id and card.get("id") == prior_card_id), None)
            if prior_card_id and selected_index is None:
                self._selection_stale = True
                self._draft_btn.configure(state="disabled")
                self._status.config(
                    text=f"{text} · 편집 중 글감이 목록에서 사라져 선택을 해제했어요. 초안 전에 다시 선택해 주세요.",
                    fg=C["yellow"])
            else:
                selected_index = selected_index or "0"
                self._selection_stale = False
                self._card_tree.selection_set(selected_index)
                self._select_card()
                # selection_set may queue a virtual event; ignore its stale rewrite after restoring edits.
                self._ignore_selection_id = selected_index
            if prior_card_id and selected_index is not None and self._cards[int(selected_index)].get("id") == prior_card_id:
                self._title.set(prior_title)
                self._question.set(prior_question)

    def _select_card(self, _event: Any = None) -> None:
        selection = self._card_tree.selection()
        if not selection:
            return
        selected_id = selection[0]
        if self._ignore_selection_id == selected_id:
            self._ignore_selection_id = None
            return
        self._ignore_selection_id = None
        self._selection_stale = False
        self._draft_btn.configure(state="normal")
        card = self._cards[int(selected_id)]
        self._title.set(card.get("title", ""))
        self._question.set(card.get("reader_question", ""))
        sources = card.get("sources") or [{"name": card.get("source_name", "원문"), "url": card.get("url", "")}]
        lines = [_date_label(card), f"지금 다룰 이유: {card.get('why_now') or '확인 필요'}",
                 f"독자 질문: {card.get('reader_question') or '질문 미작성'}",
                 f"수집 시각: {card.get('collected_at') or '미확인'}"]
        lines.extend(f"출처: {source.get('name', '출처')} · {source.get('url', '')}" for source in sources)
        self._detail.configure(state="normal")
        self._detail.delete("1.0", "end")
        self._detail.insert("1.0", "\n".join(lines))
        self._detail.configure(state="disabled")

    def _selected_card(self) -> dict:
        selection = self._card_tree.selection()
        if not selection:
            raise ValueError("먼저 글감을 선택해 주세요.")
        return self._cards[int(selection[0])]

    def _open_source(self) -> None:
        try:
            card = self._selected_card()
            sources = card.get("sources") or [{"url": card.get("url", "")}]
            url = _safe_http_url(sources[0].get("url", ""))
            if not webbrowser.open(url, new=2):
                self._status.config(text="기본 브라우저를 열지 못했어요.", fg=C["red"])
        except (ValueError, OSError) as exc:
            messagebox.showerror("원문 열기", str(exc), parent=self)

    def _create_draft(self) -> None:
        if self._selection_stale:
            messagebox.showwarning("초안 작성", "목록에서 글감을 다시 선택한 뒤 작성해 주세요.", parent=self)
            return
        try:
            card = dict(self._selected_card())
            profile = self._capture_profile()
            title = self._title.get().strip()
            question = self._question.get().strip()
            if not title or not question:
                raise ValueError("제목과 독자 질문을 모두 확인해 주세요.")
        except (ValueError, RuntimeError) as exc:
            messagebox.showwarning("초안 작성", str(exc), parent=self)
            return
        def work():
            return create_draft_for_card(_service(), card, title, question, profile)
        self._start(self._draft_btn, "초안 작성 중…", work)

    def _add_url(self) -> None:
        value = self._manual_url.get().strip()
        if not value:
            return
        try:
            profile = self._capture_profile()
            preferences = self._collect_preferences()
            self._save_preferences(preferences, profile)
        except Exception as exc:
            log.error("원문 URL 등록 전 설정 저장 실패", exc_info=True)
            self._status.config(text=f"설정 저장 실패: {exc}", fg=C["red"])
            return
        def work():
            result = _service().add_manual_url(value, profile=profile)
            if isinstance(result, dict) and "items" in result:
                return result
            snapshot = _service().load_snapshot(profile=profile)
            return snapshot if isinstance(snapshot, dict) else {
                "items": [], "errors": [result], "collected_at": None}
        self._start(self._collect_btn, "원문 확인 중…", work)

    def _import_csv(self) -> None:
        path = filedialog.askopenfilename(parent=self, filetypes=[("CSV", "*.csv"), ("모든 파일", "*.*")])
        if not path:
            return
        try:
            with Path(path).open("rb") as handle:
                raw = handle.read(MAX_CSV_BYTES + 1)
            if len(raw) > MAX_CSV_BYTES:
                raise ValueError("CSV는 2MB 이하 파일만 가져올 수 있어요.")
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = raw.decode("cp949")
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            messagebox.showerror("CSV 가져오기", str(exc), parent=self)
            return
        kind = messagebox.askquestion("CSV 종류", "Google Trends 형식이면 예, 서울 문화행사 형식이면 아니요를 선택하세요.", parent=self)
        csv_kind = "google_trends" if kind == "yes" else "seoul"
        try:
            profile = self._capture_profile()
            preferences = self._collect_preferences()
            self._save_preferences(preferences, profile)
        except Exception as exc:
            log.error("CSV 가져오기 전 설정 저장 실패", exc_info=True)
            self._status.config(text=f"설정 저장 실패: {exc}", fg=C["red"])
            return
        def work():
            result = _service().import_csv(text, kind=csv_kind, profile=profile)
            if isinstance(result, dict) and "items" in result:
                return result
            snapshot = _service().load_snapshot(profile=profile)
            return snapshot if isinstance(snapshot, dict) else {
                "items": [], "errors": [result], "collected_at": None}
        self._start(self._collect_btn, "CSV 가져오는 중…", work)

    def _render_feeds(self) -> None:
        self._feed_tree.delete(*self._feed_tree.get_children())
        for index, feed in enumerate(self._feeds):
            self._feed_tree.insert("", "end", iid=str(index), values=(
                feed.get("name", ""), feed.get("url", ""), "켜짐" if feed.get("enabled") else "꺼짐"))

    def _select_feed(self, _event: Any = None) -> None:
        selection = self._feed_tree.selection()
        if not selection:
            return
        feed = self._feeds[int(selection[0])]
        self._feed_name.set(feed.get("name", ""))
        self._feed_url.set(feed.get("url", ""))

    def _save_feed(self) -> None:
        name, url = self._feed_name.get().strip(), self._feed_url.get().strip()
        if not name or not url:
            messagebox.showwarning("공개 피드", "피드 이름과 URL을 입력해 주세요.", parent=self)
            return
        previous = [dict(feed) for feed in self._feeds]
        selection = self._feed_tree.selection()
        if selection:
            index = int(selection[0])
            feed = {"name": name, "url": url,
                    "enabled": bool(self._feeds[index].get("enabled", True))}
            self._feeds[index] = feed
        else:
            feed = {"name": name, "url": url, "enabled": True}
            self._feeds.append(feed)
        self._render_feeds()
        self._persist_feed_changes(previous)

    def _persist_feed_changes(self, previous: list) -> bool:
        try:
            self._save_preferences()
        except Exception as exc:  # 서비스 저장 경계에서 실패 원인을 남기고 화면 값을 되돌린다.
            log.error("피드 설정 저장 실패", exc_info=True)
            self._feeds = previous
            self._render_feeds()
            self._status.config(text=f"피드 저장 실패: {exc}", fg=C["red"])
            return False
        return True

    def _toggle_feed(self) -> None:
        selection = self._feed_tree.selection()
        if not selection:
            return
        previous = [dict(feed) for feed in self._feeds]
        feed = self._feeds[int(selection[0])]
        feed["enabled"] = not bool(feed.get("enabled"))
        self._render_feeds()
        self._persist_feed_changes(previous)

    def _delete_feed(self) -> None:
        selection = self._feed_tree.selection()
        if not selection:
            return
        previous = [dict(feed) for feed in self._feeds]
        del self._feeds[int(selection[0])]
        self._render_feeds()
        self._persist_feed_changes(previous)

    def _add_nasa_feed(self) -> None:
        if not any(feed.get("url") == NASA_JPL_FEED["url"] for feed in self._feeds):
            previous = [dict(feed) for feed in self._feeds]
            self._feeds.append(dict(NASA_JPL_FEED))
            self._render_feeds()
            self._persist_feed_changes(previous)
