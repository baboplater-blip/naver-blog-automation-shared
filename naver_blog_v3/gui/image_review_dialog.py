"""Human-only review dialog for images in one saved draft."""
from __future__ import annotations

import logging
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk

from core import image_review

log = logging.getLogger(__name__)


class ImageReviewDialog(tk.Toplevel):
    """Show the current article beside mobile-size image previews and save notes."""

    _FIELDS = (("confirmed_review", "이 이미지를 확인했어요"),
               ("topic_relevance", "주제에 맞음"),
               ("title_legible", "썸네일 크기에서 제목이 읽힘"),
               ("body_match", "본문 내용과 어울림"))

    def __init__(self, parent, slug: str, post: dict):
        super().__init__(parent)
        self.slug = str(slug)
        self.post = post if isinstance(post, dict) else {}
        self.binding = image_review.content_binding(self.post)
        self.paths = [entry["path"] for entry in self.binding["images"]]
        self.decisions = {}
        self._photo = None
        self._loaded = image_review.load_review(self.slug, self.post)
        self._restore_current_decisions()
        self.title("저장 글 이미지 사람 검토")
        self.geometry("760x660")
        self.minsize(620, 520)
        self.transient(parent.winfo_toplevel())
        self._build()

    def _restore_current_decisions(self) -> None:
        """Restore decisions only from a review already bound to current content."""
        record = self._loaded.get("record")
        if self._loaded.get("status") not in {"current", "partial"} or not isinstance(record, dict):
            return
        for item in record.get("images", []):
            if not isinstance(item, dict):
                continue
            try:
                path = str(Path(str(item.get("path") or "")).resolve())
            except (OSError, ValueError):
                continue
            self.decisions[path] = {
                key: bool(item.get(key)) for key, _label in self._FIELDS
            }

    def _build(self) -> None:
        root = ttk.Frame(self, padding=14)
        root.pack(fill="both", expand=True)
        ttk.Label(root, text=str(self.post.get("title") or "제목 없음"),
                  font=("맑은 고딕", 13, "bold")).pack(anchor="w")
        content = " ".join(str(self.post.get("content") or "").split())
        ttk.Label(root, text=content[:600] or "본문 미리보기 없음",
                  wraplength=710, justify="left").pack(anchor="w", pady=(5, 8))
        freshness = ("저장된 검토: 현재 글·이미지와 일치"
                    if self._loaded["status"] == "current"
                    else f"저장된 검토: {self._loaded['status']} ({self._loaded['reason']})")
        ttk.Label(root, text=f"{freshness} · 사람 판단 기록이며 출처·한글·발행 검증을 대신하지 않음",
                  wraplength=710).pack(anchor="w", pady=(0, 10))
        body = ttk.Frame(root)
        body.pack(fill="both", expand=True)
        self.image_list = tk.Listbox(body, width=28, exportselection=False)
        self.image_list.pack(side="left", fill="y", padx=(0, 12))
        self.image_list.bind("<<ListboxSelect>>", self._on_select)
        pane = ttk.Frame(body)
        pane.pack(side="left", fill="both", expand=True)
        self.preview = ttk.Label(pane, text="모바일 크기 썸네일 미리보기",
                                 anchor="center", width=36)
        self.preview.pack(fill="x", pady=(0, 8))
        self.slot_label = ttk.Label(pane, text="본문 이미지 위치: 확인되지 않음")
        self.slot_label.pack(anchor="w", pady=(0, 10))
        self.variables = {key: tk.BooleanVar(value=False) for key, _ in self._FIELDS}
        for key, label in self._FIELDS:
            ttk.Checkbutton(pane, text=label, variable=self.variables[key],
                            command=self._capture_current).pack(anchor="w", pady=3)
        footer = ttk.Frame(root)
        footer.pack(fill="x", pady=(12, 0))
        ttk.Button(footer, text="닫기", command=self.destroy).pack(side="right")
        ttk.Button(footer, text="검토 저장", command=self.save).pack(side="right", padx=(0, 8))
        for path in self.paths:
            self.image_list.insert("end", Path(path).name)
        if self.paths:
            self.image_list.selection_set(0)
            self._on_select()
        else:
            self.image_list.insert("end", "확인 가능한 이미지 없음")

    def _capture_current(self) -> None:
        selection = self.image_list.curselection()
        if not selection or selection[0] >= len(self.paths):
            return
        path = self.paths[selection[0]]
        self.decisions[path] = {key: variable.get() for key, variable in self.variables.items()}

    def _on_select(self, _event=None) -> None:
        selection = self.image_list.curselection()
        if not selection or selection[0] >= len(self.paths):
            return
        index = selection[0]
        path = self.paths[index]
        for key, _label in self._FIELDS:
            self.variables[key].set(self.decisions.get(path, {}).get(key, False))
        self._capture_current()
        slots = self.post.get("image_slots") or {}
        slot_names = []
        for slot, raw in slots.items():
            try:
                if str(Path(str(raw)).resolve()) == path:
                    slot_names.append(str(slot))
            except (OSError, ValueError):
                continue
        self.slot_label.config(text="본문 이미지 위치: " + (", ".join(slot_names) or "본문 연결 정보 없음"))
        self._render_preview(path)

    def _render_preview(self, path: str) -> None:
        try:
            from PIL import Image, ImageTk
            with Image.open(path) as source:
                image = source.convert("RGB")
                image.thumbnail((180, 320))
                self._photo = ImageTk.PhotoImage(image)
            self.preview.configure(image=self._photo, text="")
        except (OSError, ValueError, ImportError) as exc:
            self._photo = None
            self.preview.configure(image="", text=f"이미지를 표시하지 못했어요 ({type(exc).__name__})")
            log.debug("이미지 검토 미리보기 표시 실패", exc_info=True)

    def save(self) -> None:
        """Save only the user's current per-image judgments."""
        self._capture_current()
        records = [{"path": path, **values} for path, values in self.decisions.items()]
        if not records:
            messagebox.showwarning("검토 저장", "이미지를 하나 선택하고 항목을 확인해 주세요.", parent=self)
            return
        try:
            image_review.save_review(self.slug, self.post, records,
                                     reviewed_at=datetime.now().astimezone().isoformat())
        except (OSError, ValueError) as exc:
            log.warning("이미지 검토 저장 실패(%s)", type(exc).__name__)
            messagebox.showerror("검토 저장", f"검토를 저장하지 못했어요 ({type(exc).__name__}).", parent=self)
            return
        self.destroy()
