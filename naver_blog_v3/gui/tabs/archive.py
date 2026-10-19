# gui/tabs/archive.py — 저장 글(보관함) 전용 화면 (2026-09-21 GUI 개편)
""""오늘 발행할 재료가 지금 몇 편 있고, 그중 바로 나갈 수 있는 건 어느
것인가" 하나만 답하는 화면.

예전에는 저장 글을 보려면 **발행·진행 탭 아래쪽 미리보기 콤보박스**를
열어야 했다 — 한 줄짜리 드롭다운이라 제목이 잘리고, 이미지가 준비됐는지
목록에서 알 수 없었으며, 화면 이름 어디에도 "저장 글"이 없었다. 이 탭은
그 목록을 표로 펼치고 제목·저장일·이미지 상태·미리보기를 한눈에 준다.

**데이터 소스는 발행·진행 탭과 완전히 같다** — 둘 다 `gui.draft_library.
list_drafts()` 하나만 부른다(목록 규칙이 두 벌로 갈라지는 사고 차단).

읽기 전용 — 이 화면에는 발행 버튼이 없다. 목록 새로고침과 미리보기(파일
열기)만 한다.
"""
from __future__ import annotations

import logging
import os
import tkinter as tk
import webbrowser
from datetime import datetime
from pathlib import Path
from tkinter import messagebox

from gui import draft_library, icons
from gui.async_utils import UiLifecycle
from gui.data_table import DataTable
from gui.theme import (C, FB, FM, FS, FT, SP, L, PrimaryBtn, SecondaryBtn,
                        Sep, px)
from gui.widgets import EmptyState, Header, IconButton, StatusPill

log = logging.getLogger(__name__)

_LIMIT = 60
_TITLE_CELL = 52   # 표 셀에 넣을 제목 최대 글자 수(그 이상은 말줄임)


def _short(text: str, n: int = _TITLE_CELL) -> str:
    """긴 제목을 셀 폭에 맞게 말줄임 — 원문은 상세 패널이 통째로 보여준다."""
    clean = " ".join(str(text or "").split())
    return clean if len(clean) <= n else clean[: n - 1] + "…"


# 채움이 아무 것도 못 만들었을 때의 사유 → 사람 말 + 다음 행동.
_REFILL_FAIL_TEXT = {
    "already_running": "다른 이미지 작업이 돌고 있어요 — 끝난 뒤 다시 눌러주세요",
    "publish_in_progress": "발행이 진행 중이라 이미지를 만들지 않았어요",
    "disabled": "이미지 채움이 꺼져 있어요 — 설정에서 켠 뒤 다시 시도하세요",
    "slug_not_found": "이 글을 보관함 목록에서 찾지 못했어요",
    "slug_not_a_target": "이 글은 지금 다시 만들 이미지가 없어요",
    "slug_filter_unsupported": "이 버전에서는 글 하나만 고르는 채움을 지원하지 않아요",
    "replace_unverified_unsupported": "이 버전에서는 미검증 이미지 교체를 지원하지 않아요",
}


def _refill_failure_text(reason: str) -> str:
    """실패 사유 코드를 사람 말로 — 모르는 코드는 원문을 보존한다(정보 손실 0)."""
    known = _REFILL_FAIL_TEXT.get(reason)
    if known:
        return known
    return f"이미지를 다시 만들지 못했어요 ({reason})" if reason \
        else "이미지를 다시 만들지 못했어요"


class ArchiveTab(UiLifecycle, tk.Frame):
    """저장 글 목록 + 선택한 글 상세."""

    def __init__(self, parent, app):
        tk.Frame.__init__(self, parent, bg=C["bg"])
        self._init_lifecycle()
        self.app = app
        self._entries: list = []
        self._loading = False
        self._refilling = False      # 이미지 다시 만들기 진행 중(중복 클릭 차단)
        self._build()
        self.ui_after(self.refresh, delay=200)

    # ── 화면 ────────────────────────────────────────────
    def _build(self) -> None:
        hdr = Header(self, "저장 글", "만들어 두고 아직 발행하지 않은 글이에요",
                     icon="folder")
        hdr.pack(fill="x", padx=SP["xl"], pady=(SP["lg"], 0))
        self.refresh_btn = SecondaryBtn(hdr.actions, "새로고침", self.refresh,
                                        icon="refresh")
        self.refresh_btn.pack(side="left")

        # ── 요약 줄 — 총 편수 / 바로 나갈 수 있는 편수 ──
        summary = tk.Frame(self, bg=C["bg"])
        summary.pack(fill="x", padx=SP["xl"], pady=(SP["md"], SP["sm"]))
        # 버튼을 **먼저** pack 해 폭을 선점한다 — 나중에 pack 하면 좁은 창
        # (900×620)에서 왼쪽 문구가 폭을 다 먹고 버튼이 잘린다(중간 캡처 실측).
        SecondaryBtn(summary, "발행·진행 열기",
                     lambda: self.app.show_tab("posting"), icon="posting").pack(
            side="right", padx=(SP["md"], 0))
        self.count_pill = StatusPill(summary, "확인 중…", kind="neutral")
        self.count_pill.pack(side="left")
        self.summary_lbl = L(summary, "", FS, C["muted"], anchor="w")
        self.summary_lbl.pack(side="left", fill="x", expand=True,
                              padx=(SP["sm"], 0))

        Sep(self).pack(fill="x", padx=SP["xl"])

        # ── 목록 ──
        list_wrap = tk.Frame(self, bg=C["bg"])
        list_wrap.pack(fill="both", expand=True, padx=SP["xl"],
                       pady=(SP["md"], SP["sm"]))

        self.table = DataTable(list_wrap, columns=("title", "saved", "images", "publish"),
                               height=10)
        self.table.heading("title", text="제목")
        self.table.heading("saved", text="저장일")
        self.table.heading("images", text="이미지")
        self.table.heading("publish", text="발행 상태")
        self.table.column("title", width=px(430), minwidth=px(180), anchor="w")
        self.table.column("saved", width=px(110), anchor="center", stretch=False)
        self.table.column("images", width=px(150), anchor="w", stretch=False)
        self.table.column("publish", width=px(115), anchor="w", stretch=False)
        # 표는 셀 단위 색이 없어 태그 색이 **행 전체**에 걸린다 — 초록/노랑을
        # 행에 칠하면 목록이 알록달록해져 오히려 안 읽힌다(중간 캡처 실측).
        # 그래서 색은 "아직 못 나가는 글"을 흐리게 하는 데만 쓰고, 상태 구분은
        # 이미지 칸의 모양 표식(●/◐/○)이 담당한다.
        self.table.tag_configure("complete", foreground=C["text"])
        self.table.tag_configure("partial", foreground=C["text"])
        self.table.tag_configure("unverified", foreground=C["text"])
        self.table.tag_configure("none", foreground=C["muted"])
        self.table.tag_configure("unknown", foreground=C["muted"])
        self.table.bind("<<TreeviewSelect>>", self._on_select)

        # 빈 상태 / 로딩 / 실패는 표와 **상호 배타**로 같은 자리에 놓는다.
        self._list_wrap = list_wrap
        self._empty = EmptyState(
            list_wrap, "folder", "아직 저장해 둔 글이 없어요",
            hint="발행·진행 탭의 '보관함 채우기'를 누르면 글이 여기 쌓여요.",
            action_text="발행·진행 열기",
            action_cmd=lambda: self.app.show_tab("posting"), bg=C["bg"])
        self._notice = L(list_wrap, "", FB, C["text_2"], anchor="w", justify="left")
        self._show_notice("목록을 읽는 중이에요…")

        # ── 상세(선택한 글) ──
        Sep(self).pack(fill="x", padx=SP["xl"])
        detail = tk.Frame(self, bg=C["bg"], height=px(150))
        detail.pack(fill="x", padx=SP["xl"], pady=(SP["sm"], SP["md"]))
        detail.pack_propagate(False)

        title_row = tk.Frame(detail, bg=C["bg"])
        title_row.pack(fill="x")
        self.review_btn = SecondaryBtn(title_row, "이미지 사람 검토", self._review_images,
                                       icon="image")
        self.review_btn.pack(side="right", padx=(0, SP["sm"]))
        self.review_btn.config(state="disabled")
        self.open_btn = SecondaryBtn(title_row, "미리보기 열기", self._open_selected,
                                     icon="folder")
        self.open_btn.pack(side="right")
        self.open_btn.config(state="disabled")
        # 선택한 글의 미검증 교체 또는 누락 슬롯 보충. 서비스 slug/topup 경로를 공유한다.
        self.refill_btn = SecondaryBtn(title_row, "이미지 채우기",
                                       self._on_refill_unverified, icon="image")
        self.refill_btn.pack(side="right", padx=(0, SP["sm"]))
        self.refill_btn.config(state="disabled")
        self.detail_title = L(title_row, "목록에서 글을 고르면 여기 보여요",
                              FT, C["text"], anchor="w", justify="left",
                              wraplength=px(620))
        self.detail_title.pack(side="left", fill="x", expand=True)

        self.detail_meta = L(detail, "", FS, C["muted"], anchor="w", justify="left")
        self.detail_meta.pack(fill="x", pady=(SP["xs"], 0))
        self.detail_body = L(detail, "", FS, C["text_2"], anchor="nw",
                             justify="left", wraplength=px(760))
        self.detail_body.pack(fill="both", expand=True, pady=(SP["xs"], 0))
        self._selected = None

        # 창 폭이 바뀌면 상세 문구 줄바꿈 폭도 따라간다(긴 제목 잘림 방지).
        detail.bind("<Configure>", self._on_detail_resize)

    def _on_detail_resize(self, event) -> None:
        try:
            wrap = max(px(200), int(event.width) - px(180))
            self.detail_title.config(wraplength=wrap)
            self.detail_body.config(wraplength=max(px(200), int(event.width) - px(24)))
        except tk.TclError:
            pass

    # ── 목록/빈 상태/안내 토글 ───────────────────────────
    def _show_table(self) -> None:
        self._empty.hide()
        try:
            self._notice.pack_forget()
        except tk.TclError:
            pass
        if not self.table.winfo_manager():
            self.table.pack(fill="both", expand=True)

    def _show_empty(self) -> None:
        try:
            self.table.pack_forget()
            self._notice.pack_forget()
        except tk.TclError:
            pass
        self._empty.show()

    def _show_notice(self, text: str) -> None:
        """로딩·실패처럼 "표도 빈 상태도 아닌" 중간 상태 한 줄."""
        self._empty.hide()
        try:
            self.table.pack_forget()
        except tk.TclError:
            pass
        self._notice.config(text=text)
        if not self._notice.winfo_manager():
            self._notice.pack(fill="x", pady=(SP["lg"], 0))

    # ── 데이터 ──────────────────────────────────────────
    def refresh(self) -> None:
        """목록을 백그라운드에서 다시 읽는다(중복 호출은 무시)."""
        if self._loading:
            return
        self._loading = True
        try:
            self.count_pill.set("확인 중…", "neutral")
            self.refresh_btn.config(state="disabled")
        except tk.TclError:
            pass
        self.spawn(self._load)

    def _load(self) -> None:
        try:
            entries = draft_library.list_drafts(limit=_LIMIT)
        except Exception as exc:  # noqa: BLE001 — 조회 실패도 화면엔 사실대로
            log.debug("[archive] 목록 조회 실패", exc_info=True)
            self.ui_after(self._apply_error, f"{type(exc).__name__}")
            return
        self.ui_after(self._apply, entries)

    def _apply_error(self, reason: str) -> None:
        self._loading = False
        try:
            self.refresh_btn.config(state="normal")
            self.count_pill.set("읽지 못했어요", "error")
            self.summary_lbl.config(text=f"사유: {reason} · 새로고침을 눌러 다시 시도하세요")
            self._show_notice("저장 글 목록을 읽지 못했어요. 새로고침을 눌러 주세요.")
        except tk.TclError:
            pass

    def _apply(self, entries: list) -> None:
        self._loading = False
        self._entries = entries or []
        try:
            self.refresh_btn.config(state="normal")
            stats = draft_library.counts(self._entries)
            ready = stats["ready"]
            self.count_pill.set(f"저장 글 {stats['total']}편",
                                "ok" if stats["total"] else "neutral")
            summary_text = (f"바로 나갈 수 있는 글 {ready}편 · 부족 {stats['partial']}편 "
                            f"· 없음 {stats['none']}편")
            if stats.get("unverified"):
                # 장수는 찼지만 출처를 확인할 수 없어 막힌 글 — 자동으로 풀리지 않으므로
                # 숫자와 할 일을 함께 보여 준다(이 탭의 버튼이 그 출구다).
                summary_text += f" · 확인 필요 {stats['unverified']}편"
            self.summary_lbl.config(text=summary_text)

            for iid in self.table.get_children():
                self.table.delete(iid)
            # 목록을 다시 읽으면 선택이 풀린다 — 상세 패널이 **옛 숫자**를 계속
            # 보여 주지 않게 같이 비운다(다시 만들기 버튼도 함께 잠긴다).
            self._clear_detail()
            if not self._entries:
                self._show_empty()
                return

            for i, e in enumerate(self._entries):
                stamp = datetime.fromtimestamp(e["ts"]).strftime("%m월 %d일")
                self.table.insert(
                    "", "end", iid=f"row{i}",
                    values=(_short(e["title"]), stamp, draft_library.image_badge(e),
                            draft_library.publish_state_label(e)),
                    tags=(str(e.get("image_state") or "unknown"),))
            self._show_table()
        except tk.TclError:
            pass

    # ── 선택·상세 ───────────────────────────────────────
    def _selected_entry(self):
        try:
            sel = self.table.selection()
        except tk.TclError:
            return None
        if not sel:
            return None
        try:
            idx = int(str(sel[0]).replace("row", ""))
        except ValueError:
            return None
        if 0 <= idx < len(self._entries):
            return self._entries[idx]
        return None

    def _clear_detail(self) -> None:
        self._selected = None
        try:
            self.detail_title.config(text="목록에서 글을 고르면 여기 보여요")
            self.detail_meta.config(text="")
            self.detail_body.config(text="")
            self.open_btn.config(state="disabled")
            self.refill_btn.config(state="disabled")
            self.review_btn.config(state="disabled")
        except tk.TclError:
            pass

    def _on_select(self, _event=None) -> None:
        entry = self._selected_entry()
        if entry is None:
            self._clear_detail()
            return
        self._selected = entry
        post = entry.get("post") or {}
        body = str(post.get("summary") or post.get("content") or "").strip()
        body = " ".join(body.split())[:300]
        saved = datetime.fromtimestamp(entry["ts"]).strftime("%Y년 %m월 %d일 %H:%M")
        tags = [str(t) for t in (post.get("tags") or [])[:6]]
        meta = f"저장 {saved} · {draft_library.image_badge(entry, long=True)}"
        if tags:
            meta += " · " + " ".join("#" + t for t in tags)
        n_unver = int(entry.get("image_unverified") or 0)
        try:
            n_missing = max(0, int(entry.get("image_want") or 0)
                            - int(entry.get("image_have") or 0))
        except (TypeError, ValueError):
            n_missing = 0
        if n_unver:
            cause = draft_library.block_cause_text(str(entry.get("image_cause") or ""))
            meta += (f"\n이미지 {n_unver}장의 출처를 확인할 수 없어 발행이 보류돼요"
                     f"({cause}). 확인된 이미지는 보존하고 미검증 슬롯만 다시 만들 수 있어요.")
        publish_label = draft_library.publish_state_label(entry)
        meta += f"\n발행 상태: {publish_label}"
        if publish_label == "확인 필요":
            meta += " — 네이버에서 등록 여부를 먼저 확인하세요. 다시 발행하지 않습니다."
        review_status = "확인 불가"
        try:
            from core import image_review
            review_data = image_review.load_review(entry["slug"], post)
            review_status = {"missing": "아직 검토 안 함", "partial": "일부 이미지 검토",
                             "current": "현재 이미지와 일치", "stale": "글 또는 이미지 변경으로 무효",
                             "unavailable": "기록 읽기 실패"}.get(
                                 review_data.get("status"), review_data.get("status", "확인 불가"))
        except (ImportError, AttributeError, OSError, TypeError, ValueError) as exc:
            log.debug("[archive] 이미지 검토 상태 확인 실패(%s)", type(exc).__name__)
        meta += f"\n사람 이미지 검토: {review_status} (출처·한글·발행 검증과 별개)"
        try:
            from core import source_verification
            source_result = source_verification.verify(entry["slug"], post)
            source_detail = source_result.get("detail") or {}
            coverage = source_detail.get("coverage") or {}
            if source_result.get("ok"):
                meta += "\n출처 대조: 선언된 주장 연결 확인"
            else:
                meta += f"\n출처 대조: 확인 필요 ({source_result.get('reason') or 'unknown'})"
                problems = source_detail.get("problems") or []
                if problems:
                    meta += " · " + ", ".join(str(problem) for problem in problems[:3])
            if coverage:
                cov_text = (f"숫자·날짜 {coverage.get('numeric_values_covered', 0)}"
                            f"/{coverage.get('numeric_value_candidates', 0)} 대조")
                uncovered = coverage.get("uncovered_values") or []
                if uncovered:
                    cov_text += " · 미대조: " + ", ".join(str(value) for value in uncovered[:5])
                meta += f" · {cov_text}; 의미 전체 검증 아님"
        except (ImportError, AttributeError, OSError, TypeError, ValueError) as exc:
            log.debug("[archive] 출처 대조 상태 확인 실패(%s)", type(exc).__name__)
        try:
            self.detail_title.config(text=entry["title"])
            self.detail_meta.config(text=meta)
            self.detail_body.config(text=body or "(본문 미리보기 없음)")
            self.open_btn.config(state="normal")
            self.refill_btn.config(
                text=("미검증 이미지 다시 만들기" if n_unver else "누락 이미지 채우기"),
                state=("normal" if ((n_unver or n_missing) and not self._refilling)
                       else "disabled"))
            self.review_btn.config(state=("normal" if int(entry.get("image_have") or 0) else "disabled"))
        except tk.TclError:
            pass

    def _review_images(self) -> None:
        """Open a human review of the selected draft's actual image files."""
        entry = self._selected
        if entry is None:
            return
        try:
            from gui.image_review_dialog import ImageReviewDialog
            dialog = ImageReviewDialog(self, entry["slug"], entry.get("post") or {})
            dialog.grab_set()
            self.wait_window(dialog)
            self.refresh()
        except (ImportError, KeyError, OSError, tk.TclError, ValueError) as exc:
            log.warning("[archive] 이미지 검토 화면을 열지 못했습니다(%s)", type(exc).__name__)
            self._toast("이미지 검토 화면을 열지 못했어요", kind="warn")

    # ── 미검증 이미지 회복(사용자가 고른 글 1편만) ───────────────
    def _on_refill_unverified(self) -> None:
        """고른 글의 **확인되지 않은 이미지 슬롯만** 다시 만든다.

        · 자동으로는 절대 돌지 않는다 — 이 버튼을 누른 그 글 하나만 대상이다.
        · 확인된 기존 이미지는 재생성하지 않는다(그대로 재사용).
        · 실제 실행은 `core.blog_service.fill_images` → `run_fill_batch` 로 가며,
          거기서 프로필 격리 락이 중복 실행을 막는다(다른 채움이 돌면 거절).
        """
        entry = self._selected
        if entry is None or self._refilling:
            return
        n_unver = int(entry.get("image_unverified") or 0)
        replace_unverified = n_unver > 0
        try:
            n_missing = max(0, int(entry.get("image_want") or 0)
                            - int(entry.get("image_have") or 0))
        except (TypeError, ValueError):
            n_missing = 0
        target_count = n_unver if replace_unverified else n_missing
        if not target_count:
            self._toast("이 글에는 다시 만들 이미지가 없어요", kind="info")
            return
        slug = str(entry.get("slug") or "")
        if not slug:
            self._toast("글을 식별하지 못했어요", kind="warn")
            return
        have = int(entry.get("image_have") or 0)
        keep = max(0, have - n_unver) if replace_unverified else have
        cause = (draft_library.block_cause_text(str(entry.get("image_cause") or ""))
                 if replace_unverified else "누락 슬롯만 보충")
        action = "미검증 이미지 다시 만들기" if replace_unverified else "누락 이미지 채우기"
        if not messagebox.askyesno(
                action,
                f"'{_short(entry.get('title') or '', 40)}'\n\n"
                f"· 대상: {target_count}장 ({cause})\n"
                f"· 보존: 기존 이미지 {keep}장\n\n"
                "선택한 글 하나에만 적용하고 다른 글은 건드리지 않아요.\n"
                "이미지 생성에는 시간이 걸릴 수 있어요. 계속할까요?"):
            return
        self._refilling = True
        try:
            self.refill_btn.config(state="disabled")
            self.count_pill.set("이미지 다시 만드는 중…", "neutral")
        except tk.TclError:
            pass
        self._toast(f"이미지 {target_count}장을 처리하는 중이에요…", kind="info")
        self.spawn(self._refill_worker, slug, target_count, replace_unverified)

    def _refill_worker(self, slug: str, expected: int, replace_unverified: bool = True) -> None:
        """백그라운드 — 단건 채움 실행. Tk 는 건드리지 않는다(결과만 ui_after)."""
        try:
            from core import blog_service
            summary = blog_service.fill_images(
                slug=slug, topup=True, replace_unverified=replace_unverified)
        except Exception as exc:  # noqa: BLE001 — 워커는 죽지 않는다
            log.debug("[archive] 이미지 다시 만들기 실패", exc_info=True)
            summary = {"skipped": {f"exception_{type(exc).__name__}": 1}}
        self.ui_after(self._refill_done, summary if isinstance(summary, dict) else {},
                      expected)

    def _refill_done(self, summary: dict, expected: int) -> None:
        self._refilling = False
        filled = int(summary.get("filled") or 0)
        skipped = summary.get("skipped") or {}
        if filled:
            self._toast(f"이미지를 다시 만들었어요 ({filled}편 갱신)", kind="ok")
        else:
            reason = next(iter(skipped), "") if isinstance(skipped, dict) else ""
            self._toast(_refill_failure_text(str(reason)), kind="warn")
        # 화면 사유를 갱신 — 성공/실패 어느 쪽이든 목록을 다시 읽어 상태를 맞춘다.
        self.refresh()

    def _open_selected(self) -> None:
        """선택한 글을 HTML 미리보기로 굳혀 기본 브라우저로 연다.

        미리보기 HTML 생성은 발행·진행 탭이 이미 갖고 있는 `_write_draft_view`
        를 그대로 재사용한다 — 포맷을 두 벌로 만들지 않는다.
        """
        entry = self._selected
        if entry is None:
            return
        posting = getattr(self.app, "pages", {}).get("posting")
        writer = getattr(posting, "_write_draft_view", None)
        path = None
        if callable(writer):
            try:
                path = writer(entry)
            except Exception:  # noqa: BLE001 — 생성 실패는 아래에서 안내
                log.debug("[archive] 미리보기 생성 실패", exc_info=True)
        if not path:
            self._toast("미리보기를 만들지 못했어요", kind="warn")
            return
        try:
            webbrowser.open(f"file:///{str(Path(path)).replace(os.sep, '/')}")
        except Exception:  # noqa: BLE001
            self._toast("브라우저를 열지 못했어요", kind="warn")

    def _toast(self, text: str, kind: str = "info") -> None:
        toast = getattr(self.app, "toast", None)
        if toast is None:
            return
        try:
            toast.show(text, kind=kind)
        except Exception:  # noqa: BLE001
            pass
