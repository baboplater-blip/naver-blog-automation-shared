# gui/tabs/product_post.py — 상품으로 글쓰기 탭 (쇼핑커넥트 제휴 리뷰, GUI 개편 2026-09-02)
# 제휴/상품 링크 입력 → 상품 불러오기(추출) → 상품 카드 미리보기(실패 시 수동 입력)
# → 글 생성(제휴 리뷰 + 대가성 표시) → 사용자 클릭 발행(단건, photo_publish 재사용).
# 발행은 오직 사용자 클릭 + 확인 모달 승인 후에만 실계정에 올라간다(하드 게이트).
import logging
import re
import threading
import tkinter as tk
import uuid
import webbrowser
from datetime import date
from pathlib import Path
from tkinter import messagebox

from gui import icons
from gui import theme as _theme_mod
from gui.async_utils import UiLifecycle
from gui.humanize import publish_error_message
from gui.theme import C, FS, SP, SecondaryBtn
from gui.tabs._publish_section import PublishSection, notify, status_kind_for_color
from gui.widgets import Header, ScrollArea, SectionCard, StatusPill, StepIndicator

# 9호 Phase 1(고DPI) — theme.px() 가 아직 없을 수 있는 병렬 개발 상황을
# 대비한 방어적 폴백(코더 A 작업 중). 있으면 UI_SCALE 배율 적용, 없으면 항등.
px = getattr(_theme_mod, "px", lambda n: n)

log = logging.getLogger(__name__)

# 링크에서 상품 번호(/products/{id}·/catalog/{id}) 추출 — 이미지 폴더를 상품별로 격리.
_PRODUCT_ID_RE = re.compile(r"/(?:products|catalog)/(\d+)")


def _setting(name: str, default):
    try:
        from config import settings as s
        return getattr(s, name, default)
    except (ImportError, AttributeError):
        return default


def _valid_url(value) -> bool:
    """제휴 링크 무결성 판정 — core 의 단일 검증기를 재사용(상품명이 링크로 새는 것 방지)."""
    try:
        from core.product_extractor import is_valid_url
        return is_valid_url(value)
    except Exception:  # noqa: BLE001 — 판정 불가면 URL 아님으로(안전측)
        return False


def _provisional_run_dir(seed: str) -> Path:
    """추출 단계 이미지 저장용 임시 run 디렉토리(제목 확정 전). 실패해도 무해.

    H-1: URL 영숫자 앞자리만 쓰면 스마트스토어처럼 상품이 달라도 슬러그가 같아져
    (`httpssmartstorenaver…`) 다른 상품이 같은 이미지 폴더를 공유 → 01.jpg 덮어쓰기로
    B 사진이 A 글에 업로드된다. 상품 번호(있으면) + uuid 접미사로 **상품별·호출별 격리**한다.
    """
    m = _PRODUCT_ID_RE.search(seed or "")
    if m:
        stem = f"id{m.group(1)}"
    else:
        stem = "".join(ch for ch in (seed or "") if ch.isalnum())[:16] or "product"
    slug = f"{date.today().strftime('%Y-%m-%d')}_prod-{stem}-{uuid.uuid4().hex[:8]}"
    try:
        from config import profile as _p
        root = _p.runs_dir()
    except Exception:  # noqa: BLE001
        root = Path(__file__).resolve().parents[2] / "runs"
    return Path(root) / slug


class ProductPostTab(UiLifecycle, tk.Frame):
    """제휴/상품 링크 → 상품 데이터 → 제휴 리뷰 초안 → 사용자 클릭 발행."""

    def __init__(self, parent, app):
        super().__init__(parent, bg=C["bg"])
        self.app = app
        self._busy = False
        self._can_publish = False
        self._force_republish = False           # 중복 차단을 사람이 1회 해제했는가
        self._stop = threading.Event()
        self._extracted: "dict | None" = None      # extract_product 결과
        self._affiliate = ""                        # 제휴 링크(URL 검증 통과분만 보존)
        self._affiliate_missing = False             # 유효 제휴 링크 없음(수수료 0 고지용)
        self._last_data: "dict | None" = None       # 발행 대상 data dict(QA PASS 시)
        self._last_preview: "Path | None" = None
        self._build()
        self._init_lifecycle()

    # ── UI 구성 ──────────────────────────────────────────────────────
    def _build(self):
        Header(self, "상품으로 글쓰기", icon=icons.glyph("product"),
               subtitle="제휴 링크를 붙이면 상품 정보를 불러와 정직한 리뷰 초안을 만들어요."
               ).pack(fill="x", padx=SP["xl"], pady=(px(10), SP["xs"]))

        # 입력→미리보기→발행 3단계 진행 표시(설계 v3 §7-6) — 상품을 불러오면
        # 2단계, 초안이 완성되면 3단계로 넘어간다(_on_load/_gen_worker 에서 갱신).
        self.steps = StepIndicator(self, ["입력", "미리보기", "발행"])
        self.steps.pack(fill="x", padx=SP["xl"], pady=(0, SP["xs"]))

        # 최소 창(900×620)에서도 3단계 "네이버에 발행하기" 버튼이 화면 밖으로
        # 잘리지 않도록 본문을 ScrollArea 로 감싼다(마우스휠 동작·2026-09-02
        # 디자이너 캡처 심사 — 패딩도 최대한 줄여 보통은 스크롤 없이 다
        # 보이게 하되, 상품 카드·수동 입력이 늘어나는 경우에도 도달 가능.
        scroll = ScrollArea(self)
        scroll.pack(fill="both", expand=True, padx=SP["xl"], pady=(0, px(6)))
        body = scroll.inner

        # ── 1. 상품 불러오기 ───────────────────────────────────────────
        card1 = SectionCard(body, "1. 상품 불러오기",
                            subtitle="대가성 표시는 항상 자동으로 들어가요.",
                            pady=px(4))
        card1.pack(fill="x", pady=(0, px(6)))

        row = tk.Frame(card1.body, bg=C["card"])
        row.pack(fill="x", pady=(0, px(6)))
        tk.Label(row, text="제휴/상품 링크:", font=FS, fg=C["text_2"],
                 bg=C["card"]).pack(side="left", padx=(0, SP["sm"]))
        self.link_var = tk.StringVar()
        tk.Entry(row, textvariable=self.link_var, font=FS, width=52,
                 bg=C["surface"], fg=C["text"], insertbackground=C["text"],
                 relief="flat").pack(side="left", ipady=4)
        self.load_btn = SecondaryBtn(row, "상품 불러오기", self._on_load,
                                     icon=icons.glyph("search"))
        self.load_btn.pack(side="left", padx=(px(10), 0))

        # 상품 카드(불러오기 후 채움)
        self.card = tk.Frame(card1.body, bg=C["surface"], padx=SP["md"], pady=px(6))
        self.card.pack(fill="x", pady=(SP["xs"], SP["xs"]))
        self.card_lbl = tk.Label(self.card, text="상품 정보가 여기에 나와요.",
                                 font=FS, fg=C["text_2"], bg=C["surface"],
                                 anchor="w", justify="left", wraplength=px(640))
        self.card_lbl.pack(anchor="w")

        # 수동 보완(추출 실패 시 노출) — 상품명/가격 직접 입력
        self.manual = tk.Frame(card1.body, bg=C["card"])
        tk.Label(self.manual, text="자동으로 못 불러왔어요 — 상품명을 입력하면 검색으로 보강해요:",
                 font=FS, fg=C["orange"], bg=C["card"]).pack(anchor="w")
        mrow = tk.Frame(self.manual, bg=C["card"])
        mrow.pack(fill="x", pady=(SP["xs"], 0))
        tk.Label(mrow, text="상품명:", font=FS, fg=C["text_2"],
                 bg=C["card"]).pack(side="left", padx=(0, px(6)))
        self.mname_var = tk.StringVar()
        tk.Entry(mrow, textvariable=self.mname_var, font=FS, width=34,
                 bg=C["surface"], fg=C["text"], insertbackground=C["text"],
                 relief="flat").pack(side="left", ipady=3, padx=(0, px(10)))
        tk.Label(mrow, text="가격(선택):", font=FS, fg=C["text_2"],
                 bg=C["card"]).pack(side="left", padx=(0, px(6)))
        self.mprice_var = tk.StringVar()
        tk.Entry(mrow, textvariable=self.mprice_var, font=FS, width=12,
                 bg=C["surface"], fg=C["text"], insertbackground=C["text"],
                 relief="flat").pack(side="left", ipady=3)
        self.manual.pack(fill="x", pady=(0, px(6)))
        self.manual.pack_forget()                    # 기본 숨김

        # ── 2. 미리보기(글 생성) ───────────────────────────────────────
        card2 = SectionCard(body, "2. 미리보기", subtitle="상품 정보로 리뷰 초안을 써요.",
                            pady=px(4))
        card2.pack(fill="x", pady=(0, px(6)))

        actions = tk.Frame(card2.body, bg=C["card"])
        actions.pack(fill="x", pady=(0, SP["xs"]))
        self.gen_btn = SecondaryBtn(actions, "글 생성하기", self._on_generate)
        self.gen_btn.pack(side="left", padx=(0, SP["sm"]))
        self.gen_btn.config(state="disabled")
        self.preview_btn = SecondaryBtn(actions, "미리보기 열기", self._open_preview,
                                        icon=icons.glyph("search"))
        self.preview_btn.pack(side="left")
        self.preview_btn.config(state="disabled")

        self.status_pill = StatusPill(card2.body, "대기 중", kind="neutral")
        self.status_pill.pack(anchor="w")
        self.status_lbl = tk.Label(card2.body,
                                   text="링크를 붙이고 '상품 불러오기'를 눌러주세요.",
                                   font=FS, fg=C["text_2"], bg=C["card"], anchor="w",
                                   justify="left", wraplength=px(640))
        self.status_lbl.pack(anchor="w", pady=(px(2), 0))

        # ── 3. 발행 ───────────────────────────────────────────────────
        self.publish = PublishSection(body, self.app, self._on_publish, pady=px(4))
        self.publish.pack(fill="x")
        # 설계서 §0.2 — 소유 탭이 기존 속성명을 그대로 노출.
        self.cat_var = self.publish.cat_var
        self.cat_combo = self.publish.cat_combo
        self.publish_btn = self.publish.publish_btn
        self.cat_hint_lbl = self.publish.cat_hint_lbl

        if not _setting("SHOPPING_CONNECT_ENABLED", True):
            self.load_btn.config(state="disabled")
            self.status_lbl.config(
                text="상품 글쓰기 기능이 꺼져 있어요. 설정에서 켜주세요.",
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
        try:
            self.app.set_status(text, color)
        except (AttributeError, tk.TclError):
            pass

    def _refresh_actions(self):
        def _apply():
            busy = self._busy
            can = self._can_publish
            has_product = self._extracted is not None
            try:
                self.load_btn.config(state="disabled" if busy else "normal")
            except tk.TclError:
                pass
            try:
                self.gen_btn.config(
                    state="normal" if (has_product and not busy) else "disabled")
            except tk.TclError:
                pass
            try:
                self.preview_btn.config(
                    state="normal" if self._last_preview else "disabled")
            except tk.TclError:
                pass
            self.publish.set_state(can, busy)
        self.ui_after(_apply)

    def _show_card(self, product: dict, warn: str = ""):
        def _apply():
            try:
                name = str(product.get("name") or "(이름 없음)")
                price = product.get("price")
                price_txt = f"{int(price):,}원" if price else "가격 확인 안 됨"
                imgs = len(product.get("image_paths") or [])
                mall = product.get("mall") or ""
                src = product.get("source") or ""
                text = (f"✅ {name}\n가격: {price_txt}"
                        + (f"  ·  판매처: {mall}" if mall else "")
                        + f"  ·  이미지 {imgs}장  ·  출처: {src}")
                if warn:
                    text += f"\n{warn}"
                self.card_lbl.config(text=text, fg=C["orange"] if warn else C["text"])
            except tk.TclError:
                pass
        self.ui_after(_apply)

    def _show_manual(self, show: bool, reason: str = ""):
        def _apply():
            try:
                if show:
                    self.manual.pack(fill="x", pady=(0, px(6)))
                    self.card_lbl.config(
                        text=("자동으로 못 불러왔어요 — 상품명을 입력한 뒤 '글 생성하기'를 눌러주세요."
                              + (f"\n({reason})" if reason else "")),
                        fg=C["orange"])
                else:
                    self.manual.pack_forget()
            except tk.TclError:
                pass
        self.ui_after(_apply)

    def _apply_menus(self, menus):
        self.ui_after(self.publish.load_categories, list(menus))

    # ── 상품 불러오기(추출) ──────────────────────────────────────────
    def _on_load(self):
        if self._busy:
            return
        if not _setting("SHOPPING_CONNECT_ENABLED", True):
            self._set_status("상품 글쓰기 기능이 꺼져 있어요. 설정을 확인하세요.", C["text_2"])
            return
        link = self.link_var.get().strip()
        if not link:
            self._set_status("제휴/상품 링크를 먼저 붙여 주세요.", C["orange"])
            return
        self._busy = True
        self._can_publish = False
        self._last_data = None
        self._extracted = None
        self._stop.clear()
        self._refresh_actions()
        self.steps.set_active(1)              # 불러오기 시작 → "미리보기" 단계로
        self._set_status("상품 정보를 불러오는 중이에요… (검색·이미지 다운로드에 잠시 걸려요)",
                         C["yellow"])
        self.spawn(self._load_worker, link, name="product-load")

    def _load_worker(self, link: str):
        try:
            from core import product_extractor as pe
            run_dir = _provisional_run_dir(link)
            result = pe.extract_product(link, run_dir=run_dir)
            # 제휴 링크는 URL 검증 통과분만 보존(상품명·비URL 이 링크로 새지 않게).
            self._affiliate = (result.get("affiliate_url")
                               or (link if _valid_url(link) else ""))
            if result.get("ok"):
                self._extracted = result
                self._show_manual(False)
                self._show_card(result)
                self._set_status(
                    "상품 정보를 불러왔어요. '글 생성하기'를 눌러주세요.", C["green"])
            else:
                # need_manual — 수동 입력 유도(링크는 보존, 상품명만 받으면 API 보강).
                self._extracted = None
                self._show_manual(True, str(result.get("reason") or ""))
                self._set_status(
                    "자동으로 못 불러왔어요 — 상품명을 입력한 뒤 '글 생성하기'를 눌러주세요.",
                    C["orange"])
        except Exception as exc:  # noqa: BLE001 — 워커는 절대 죽지 않음
            log.error("상품 불러오기 실패", exc_info=True)
            self._set_status(f"불러오기에 실패했어요: {type(exc).__name__}: {str(exc)[:80]}",
                             C["red"])
        finally:
            self._busy = False
            # 수동 입력이 보이면 '글 생성' 을 켜 준다(추출 실패여도 수동으로 진행 가능).
            self._refresh_actions()
            self.ui_after(self._enable_gen_if_ready)

    def _enable_gen_if_ready(self):
        try:
            manual_shown = self.manual.winfo_manager() != ""
            if manual_shown and not self._busy:
                self.gen_btn.config(state="normal")
        except tk.TclError:
            pass

    # ── 글 생성 ──────────────────────────────────────────────────────
    def _on_generate(self):
        if self._busy:
            return
        link = self.link_var.get().strip()
        manual_name = self.mname_var.get().strip()
        if self._extracted is None and not manual_name:
            self._set_status("상품명을 입력하거나 링크를 다시 불러와 주세요.", C["orange"])
            return
        self._busy = True
        self._can_publish = False
        self._last_data = None
        self._stop.clear()
        self._refresh_actions()
        self._set_status("제휴 리뷰 초안을 생성하는 중이에요… (수십 초 걸릴 수 있어요)",
                         C["yellow"])
        self.spawn(self._gen_worker, link, manual_name, name="product-gen")

    def _gen_worker(self, link: str, manual_name: str):
        try:
            from core import product_extractor as pe
            from core import product_writer as pw

            product = self._extracted
            # 수동 경로 — 상품명으로 API 재보강(가격·이미지 채우기 시도).
            # C-1: 상품명 텍스트를 link 인자로 넘기지 않는다(비URL 은 link 로 안 씀).
            if product is None:
                run_dir = _provisional_run_dir(manual_name or link)
                product = pe.extract_product(
                    link if _valid_url(link) else "",
                    product_name=manual_name, run_dir=run_dir)
                if not product.get("name"):
                    manual_price = self._parse_manual_price()
                    product = {"name": manual_name, "price": manual_price,
                               "images": [], "image_paths": [],
                               "affiliate_url": self._affiliate, "source": "manual"}
                self._extracted = product
                # 재보강이 유효 URL 을 새로 얻었을 때만 갱신(비URL 로 덮지 않음).
                new_aff = product.get("affiliate_url")
                if _valid_url(new_aff):
                    self._affiliate = new_aff

            # C-1: 제휴 링크 후보는 **URL 검증 통과분만**(원본 상품 URL·상품명 금지).
            # settings 폴백·missing 판정은 writer 가 담당한다.
            affiliate_candidate = self._affiliate
            if not _valid_url(affiliate_candidate):
                cand = product.get("affiliate_url")
                affiliate_candidate = cand if _valid_url(cand) else ""

            data = pw.generate_product_post(
                product, affiliate_url=affiliate_candidate, stop_flag=self._stop)

            if data.get("_stopped"):
                self._set_status("중지했어요.", C["text_2"])
                return

            self._write_preview(data)
            self._affiliate_missing = bool(data.get("affiliate_missing"))

            if data.get("_qa_failed") or data.get("_failed"):
                reason = str(data.get("_qa_failed_reason")
                             or data.get("_failed_reason") or "")[:80]
                self._can_publish = False
                self._last_data = None
                self._set_status(
                    f"초안을 만들었지만 품질 기준을 못 맞춰 발행을 보류했어요: {reason}",
                    C["orange"])
            else:
                self._last_data = data
                self._can_publish = True
                try:
                    from core.photo_publish import list_target_menus
                    self._apply_menus(list_target_menus())
                except Exception:  # noqa: BLE001
                    log.debug("메뉴 목록 로드 실패", exc_info=True)
                    self._apply_menus([])
                self.ui_after(self.publish.set_status, "발행 준비됨", "neutral")
                self.ui_after(self.steps.set_active, 2)   # 초안 완료 → "발행" 단계로
                if self._affiliate_missing:
                    # 정직한 고지 — 카드·상태 모두에 '수수료 0' 경고(조용한 실패 제거).
                    self._show_card(
                        self._extracted,
                        warn="구매(제휴) 링크가 없어요 — 이 글은 수수료가 발생하지 않아요")
                    self._set_status(
                        "초안 완성 — 유효한 구매 링크가 없어 수수료가 발생하지 않아요. "
                        "메뉴를 고른 뒤 발행하면 링크 없이 올라가요.", C["orange"])
                else:
                    self._set_status(
                        "초안 완성 — 대상 메뉴를 고른 뒤 '네이버에 발행하기'를 눌러주세요.",
                        C["green"])
        except Exception as exc:  # noqa: BLE001
            log.error("상품 글 생성 실패", exc_info=True)
            self._set_status(f"생성에 실패했어요: {type(exc).__name__}: {str(exc)[:80]}",
                             C["red"])
        finally:
            self._busy = False
            self._refresh_actions()

    def _parse_manual_price(self):
        raw = self.mprice_var.get().strip()
        digits = "".join(ch for ch in raw if ch.isdigit())
        return int(digits) if digits else None

    def _write_preview(self, data: dict):
        """content_html 을 미리보기 파일로 저장하고 열기 버튼 활성."""
        try:
            slug = str(data.get("slug") or "product-preview")
            run_dir = _provisional_run_dir(slug)
            run_dir.mkdir(parents=True, exist_ok=True)
            path = run_dir / "preview.html"
            html = (f"<meta charset='utf-8'><h1>{data.get('title','')}</h1>"
                    + (data.get("content_html") or ""))
            path.write_text(html, encoding="utf-8")
            self._last_preview = path
            self.ui_after(lambda: self.preview_btn.config(state="normal"))
        except OSError as exc:
            log.debug("미리보기 저장 실패: %s", exc)

    def _open_preview(self):
        if self._last_preview and self._last_preview.is_file():
            try:
                webbrowser.open(self._last_preview.as_uri())
            except (OSError, ValueError) as exc:
                self._set_status(f"미리보기를 열지 못했어요: {exc}", C["red"])
        else:
            self._set_status("아직 만들어진 미리보기가 없어요.", C["text_2"])

    # ── 발행(사용자 클릭 전용, 확인 모달 게이트) — photo_post 패턴 재사용 ────
    def _on_publish(self):
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

        self._busy = True
        self._refresh_actions()
        handed_off = False
        try:
            from core.photo_publish import existing_image_paths

            category = self.cat_var.get().strip()
            if not category:
                messagebox.showwarning(
                    "카테고리 선택 필요",
                    "먼저 이 글을 올릴 블로그 메뉴(카테고리)를 선택하세요.\n"
                    "선택하지 않으면 상품 글이 엉뚱한 기본 메뉴로 올라갈 수 있습니다.")
                self._set_status("발행을 취소했어요 — 대상 메뉴를 먼저 선택하세요.", C["orange"])
                return

            known_menus = self.publish.known_menus()
            if known_menus and category not in known_menus:
                messagebox.showwarning(
                    "카테고리 매핑 불가",
                    f"'{category}'는 블로그 메뉴 목록에 없습니다.\n"
                    f"아래 목록에서 메뉴를 골라 주세요:\n· " + "\n· ".join(known_menus))
                self._set_status(
                    f"발행을 취소했어요 — '{category}'는 알려진 메뉴가 아니에요.", C["red"])
                return

            n_img = len(existing_image_paths(data))
            title = str(data.get("title") or "")[:40]
            # C-1 정직 고지 — 유효 제휴 링크가 없으면 발행 결정 모달에 '수수료 0' 경고를 명시.
            aff_warn = ("\n\n⚠️ 유효한 구매(제휴) 링크가 없어 이 글에서는 수수료가 "
                        "발생하지 않습니다(링크 없이 올라감).") if self._affiliate_missing else ""
            if known_menus:
                confirm_body = (f"'{title}'\n"
                                f"제휴 리뷰(이미지 {n_img}장)를 '[{category}]' 메뉴에 발행합니다."
                                f"{aff_warn}\n\n"
                                f"대가성 표시가 포함되며, 실제로 네이버에 올라가고 되돌릴 수 없습니다. 계속할까요?")
            else:
                confirm_body = (f"'{title}'\n"
                                f"제휴 리뷰(이미지 {n_img}장)를 발행합니다.\n\n"
                                f"⚠️ 블로그 메뉴 정보가 아직 없어 '기본 메뉴'로 올라갈 수 있습니다.\n"
                                f"발행 후 블로그에서 카테고리를 확인/이동하세요."
                                f"{aff_warn}\n\n"
                                f"대가성 표시가 포함되며, 되돌릴 수 없습니다. 계속할까요?")
            if not messagebox.askyesno(
                    "네이버에 발행 — 되돌릴 수 없음", confirm_body, icon="warning"):
                self._set_status("발행을 취소했어요.", C["text_2"])
                return

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
                handed_off = True
            except Exception:
                app._running = False
                log.error("발행 워커 시작 실패", exc_info=True)
                raise
        finally:
            if not handed_off:
                self._busy = False
                self._refresh_actions()

    def _publish_worker(self, data: dict, category: str):
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
                f"네이버 발행 중이에요… (이미지 {n_img}장) 브라우저가 뜹니다. 잠시만요.",
                C["accent"])
            from core.photo_publish import publish_photo_run
            result = publish_photo_run(
                data, category, reserve_datetime=None,
                auth_event=app.auth_event, auth_cb=auth_cb, stop_flag=self._stop,
                force_republish=bool(getattr(self, "_force_republish", False)))
            self._show_publish_result(result, category)
        except Exception as exc:  # noqa: BLE001
            log.error("상품 발행 워커 실패", exc_info=True)
            self._set_status(
                f"발행에 실패했어요: {type(exc).__name__}: {str(exc)[:80]}", C["red"])
            self.ui_after(self.publish.set_status, "발행 실패", "error")
        finally:
            # 1회용 재발행 확인은 시도 직후 되돌린다(다음 클릭이 중복 차단을 건너뛰지 않게).
            self._force_republish = False
            app._running = False
            app.auth_event.clear()
            self._busy = False
            if pt is not None:
                self.ui_after(pt.hide_auth_waiting)
            self._refresh_actions()

    def _show_publish_result(self, result: dict, category: str):
        if not isinstance(result, dict):
            self._set_status("발행 결과를 확인할 수 없어요.", C["red"])
            return
        if result.get("ok"):
            n = result.get("image_count", 0)
            when = "예약 등록" if result.get("reserved") else "발행"
            self._can_publish = False
            self.ui_after(self.publish.set_status, "발행 완료", "ok")
            if result.get("category_resolved"):
                self._set_status(
                    f"네이버에 {when} 완료 — '[{category}]' 메뉴, 이미지 {n}장 올렸어요.",
                    C["green"])
            else:
                self._set_status(
                    f"네이버에 {when} 완료 — 이미지 {n}장 올렸어요. "
                    f"카테고리가 '기본 메뉴'일 수 있으니 블로그에서 확인/이동하세요.",
                    C["orange"])
            return
        code = str(result.get("error") or "")
        msg = publish_error_message(code)
        if code == "qa_failed" and result.get("reason"):
            msg += f" (사유: {str(result['reason'])[:60]})"
        if code == "publish_blocked":
            # 실패가 아니라 "이미 올라갔을 수 있어서 막았다".
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
