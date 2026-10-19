"""image_inbox

인박스 폴더(watchdog) → draft.html figure src 주입 파이프라인.

운영 모델:
- CodexCLIBot이 `{slug}_{role}{n}.png`로 인박스에 PNG를 떨어뜨린다.
- ImageInbox는 watchdog Observer로 그 폴더를 감시하며 새 파일이 들어오면
  파일명 → (slug, role, n)로 파싱하고, `runs/{slug}/draft.html`의 placeholder
  `<figure data-role="..." data-n="..."> ...</figure>` 안의 `<img src="">`에
  상대 경로를 주입한다.
- placeholder가 없으면 안전한 폴백: 본문 끝(`</body>` 또는 문서 끝)에 새 figure를 append.
- 매칭 성공 PNG는 `_processed/`로, 미매칭(슬러그 불일치 등)은 `_unmatched/`로 격리.

watchdog 미설치 환경에서도 import 자체는 성공하도록 옵셔널 import 처리. 단,
`start_watching()`은 watchdog 없으면 RuntimeError.
"""

from __future__ import annotations

import re
import shutil
import threading
import time
from dataclasses import dataclass, field
from html import escape as html_escape
from pathlib import Path
from typing import Callable, Literal, Optional

# watchdog은 옵셔널 — 단위 테스트에서는 process_file/reconcile만 검증.
try:
    from watchdog.events import FileSystemEvent, FileSystemEventHandler
    from watchdog.observers import Observer
    _WATCHDOG_AVAILABLE = True
except ImportError:  # pragma: no cover - 환경 의존
    FileSystemEvent = object  # type: ignore[assignment,misc]
    FileSystemEventHandler = object  # type: ignore[assignment,misc]
    Observer = None  # type: ignore[assignment]
    _WATCHDOG_AVAILABLE = False

# ──────────────────────────────────────────────────────────────────────────────
# 상수
# ──────────────────────────────────────────────────────────────────────────────

ValidRole = Literal["thumb", "body", "og", "info"]
_VALID_ROLES: tuple[str, ...] = ("thumb", "body", "og", "info")

# {slug}_{role}{n}.png   (thumb/og는 n 생략 또는 1)
_FILENAME_RE = re.compile(
    r"^(?P<slug>.+?)_(?P<role>thumb|body|og|info)(?P<n>\d+)?\.png$",
    re.IGNORECASE,
)

# 본문 placeholder: <figure data-role="body" data-n="2"> ... </figure>
# img의 src 속성만 교체. data-* 순서 무관.
_FIGURE_BLOCK_TEMPLATE = (
    '<figure data-role="{role}" data-n="{n}">'
    '<img src="{src}" alt="{alt}">'
    '<figcaption>{caption}</figcaption>'
    '</figure>'
)

PROCESSED_DIR_NAME: str = "_processed"
UNMATCHED_DIR_NAME: str = "_unmatched"

# watchdog: 디스크 쓰기 완료를 기다리는 안정성 체크
_WRITE_STABILIZE_DELAY_SEC: float = 0.1
_WRITE_STABILIZE_ATTEMPTS: int = 30
_WRITE_STABILIZE_INTERVAL_SEC: float = 0.1

DEFAULT_EXPECTED_SLOTS: int = 6
DIGITAL_EXPECTED_SLOTS: int = 8


# ──────────────────────────────────────────────────────────────────────────────
# 데이터 클래스
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class ParsedFilename:
    slug: str
    role: ValidRole
    n: int  # thumb/og가 번호 생략 시 1로 보정


@dataclass
class ProcessResult:
    png_path: Path
    matched: bool
    target_html: Optional[Path]
    figure_replaced: bool
    moved_to: Path


@dataclass
class ReconcileReport:
    slug: str
    expected_slots: int
    matched_count: int = 0
    unmatched_files: list[Path] = field(default_factory=list)
    missing_slots: list[str] = field(default_factory=list)


# ──────────────────────────────────────────────────────────────────────────────
# ImageInbox
# ──────────────────────────────────────────────────────────────────────────────


class ImageInbox:
    """인박스 감시 + 슬러그 매칭 + draft.html figure 주입."""

    def __init__(
        self,
        inbox_dir: Path,
        runs_root: Path,
        unmatched_dir: Optional[Path] = None,
        processed_dir: Optional[Path] = None,
        title_resolver: Optional[Callable[[str], str]] = None,
    ) -> None:
        """초기화.

        Args:
            inbox_dir: PNG가 떨어지는 인박스 폴더.
            runs_root: `runs/{slug}/draft.html` 루트.
            unmatched_dir: 미매칭 PNG 격리 폴더 (기본 inbox_dir/_unmatched).
            processed_dir: 매칭 성공 PNG 보관 폴더 (기본 inbox_dir/_processed).
            title_resolver: slug → 한국어 제목 변환 콜백 (alt 텍스트용). None이면 slug 사용.
        """
        if not isinstance(inbox_dir, Path):
            raise TypeError("inbox_dir은 pathlib.Path여야 함")
        if not isinstance(runs_root, Path):
            raise TypeError("runs_root는 pathlib.Path여야 함")

        self.inbox_dir: Path = inbox_dir
        self.runs_root: Path = runs_root
        self.unmatched_dir: Path = unmatched_dir or (inbox_dir / UNMATCHED_DIR_NAME)
        self.processed_dir: Path = processed_dir or (inbox_dir / PROCESSED_DIR_NAME)

        self.inbox_dir.mkdir(parents=True, exist_ok=True)
        self.unmatched_dir.mkdir(parents=True, exist_ok=True)
        self.processed_dir.mkdir(parents=True, exist_ok=True)
        self.runs_root.mkdir(parents=True, exist_ok=True)

        self._title_resolver: Callable[[str], str] = title_resolver or (lambda s: s)
        self._observer: Optional[object] = None
        self._observer_lock = threading.Lock()

    # ──────────────────────────────────────────────────────────────────────
    # 파일명 파싱
    # ──────────────────────────────────────────────────────────────────────

    @staticmethod
    def parse_filename(name: str) -> Optional[ParsedFilename]:
        """`{slug}_{role}{n}.png` → ParsedFilename. 미일치 시 None."""
        if not isinstance(name, str) or not name.lower().endswith(".png"):
            return None
        m = _FILENAME_RE.match(name)
        if m is None:
            return None
        slug = m.group("slug").strip()
        role_raw = m.group("role").lower()
        if role_raw not in _VALID_ROLES:
            return None
        n_raw = m.group("n")
        n = int(n_raw) if n_raw else 1
        if not slug:
            return None
        return ParsedFilename(slug=slug, role=role_raw, n=n)  # type: ignore[arg-type]

    # ──────────────────────────────────────────────────────────────────────
    # draft.html 매칭
    # ──────────────────────────────────────────────────────────────────────

    def _draft_path(self, slug: str) -> Path:
        return self.runs_root / slug / "draft.html"

    @staticmethod
    def _replace_figure_in_html(
        html: str,
        role: str,
        n: int,
        new_src: str,
        new_alt: str,
        new_caption: str = "",
    ) -> tuple[str, bool]:
        """`<figure data-role="..." data-n="...">…</figure>` 안의 <img src>를 교체.

        Returns:
            (updated_html, replaced).
        """
        # data-role / data-n이 어느 순서로 와도 잡힘. role은 정규식 이스케이프 처리.
        pattern = re.compile(
            r"<figure\s+[^>]*?data-role\s*=\s*[\"']"
            + re.escape(role)
            + r"[\"'][^>]*?data-n\s*=\s*[\"']"
            + str(int(n))
            + r"[\"'][^>]*?>(?P<inner>.*?)</figure>",
            re.IGNORECASE | re.DOTALL,
        )
        alt_pattern = re.compile(
            r"<figure\s+[^>]*?data-n\s*=\s*[\"']"
            + str(int(n))
            + r"[\"'][^>]*?data-role\s*=\s*[\"']"
            + re.escape(role)
            + r"[\"'][^>]*?>(?P<inner>.*?)</figure>",
            re.IGNORECASE | re.DOTALL,
        )

        replacement = _FIGURE_BLOCK_TEMPLATE.format(
            role=html_escape(role, quote=True),
            n=int(n),
            src=html_escape(new_src, quote=True),
            alt=html_escape(new_alt, quote=True),
            caption=html_escape(new_caption, quote=False),
        )

        new_html, count = pattern.subn(replacement, html, count=1)
        if count > 0:
            return new_html, True
        new_html, count2 = alt_pattern.subn(replacement, html, count=1)
        return new_html, count2 > 0

    def _append_figure_to_html(
        self,
        html: str,
        role: str,
        n: int,
        new_src: str,
        new_alt: str,
        new_caption: str = "",
    ) -> str:
        """placeholder가 없을 때 안전한 폴백 — 본문 끝에 figure 추가."""
        figure = _FIGURE_BLOCK_TEMPLATE.format(
            role=html_escape(role, quote=True),
            n=int(n),
            src=html_escape(new_src, quote=True),
            alt=html_escape(new_alt, quote=True),
            caption=html_escape(new_caption, quote=False),
        )
        lowered = html.lower()
        body_close = lowered.rfind("</body>")
        if body_close >= 0:
            return html[:body_close] + figure + "\n" + html[body_close:]
        return html + "\n" + figure + "\n"

    def _build_alt_text(self, slug: str, role: str, n: int) -> str:
        """한국어 ALT 텍스트 — slug 또는 title_resolver 결과 + 역할."""
        title = self._title_resolver(slug)
        role_label = {
            "thumb": "썸네일",
            "body": f"본문 이미지 {n}",
            "og": "공유 카드",
            "info": f"정보 카드 {n}",
        }.get(role, role)
        return f"{title} {role_label}"

    def _compute_relative_src(self, png_in_processed: Path, draft_html: Path) -> str:
        """draft.html 기준의 상대 경로(POSIX 슬래시)로 src 생성.

        draft.html은 보통 `runs/{slug}/draft.html`이라 인박스 `_processed/`는
        상위 폴더로 거슬러 올라가야 한다. 윈도우 백슬래시는 브라우저 안전상 슬래시로 변환.
        """
        try:
            rel = Path(__import__("os").path.relpath(png_in_processed, draft_html.parent))
        except ValueError:
            # 다른 드라이브 등으로 상대화 실패 시 절대 경로 file:// URL
            return png_in_processed.resolve().as_uri()
        return rel.as_posix()

    # ──────────────────────────────────────────────────────────────────────
    # 단일 PNG 처리
    # ──────────────────────────────────────────────────────────────────────

    def process_file(self, png_path: Path) -> ProcessResult:
        """단일 PNG를 처리한다 (watchdog과 동기 호출 양쪽에서 공용).

        절대 예외를 던지지 않는다 — 실패해도 ProcessResult로 보고.
        """
        png_path = Path(png_path)

        # 아직 디스크 쓰기 안 끝났을 수 있음 — 안정화 대기
        self._wait_for_write_complete(png_path)

        if not png_path.exists() or not png_path.is_file():
            return ProcessResult(
                png_path=png_path,
                matched=False,
                target_html=None,
                figure_replaced=False,
                moved_to=png_path,
            )

        parsed = self.parse_filename(png_path.name)
        if parsed is None:
            moved = self._move_safely(png_path, self.unmatched_dir)
            return ProcessResult(
                png_path=png_path,
                matched=False,
                target_html=None,
                figure_replaced=False,
                moved_to=moved,
            )

        draft = self._draft_path(parsed.slug)
        if not draft.exists():
            moved = self._move_safely(png_path, self.unmatched_dir)
            return ProcessResult(
                png_path=png_path,
                matched=False,
                target_html=draft,
                figure_replaced=False,
                moved_to=moved,
            )

        # 매칭 성공 — 먼저 _processed/로 옮긴 뒤 draft.html 갱신.
        moved_to_processed = self._move_safely(png_path, self.processed_dir)

        alt_text = self._build_alt_text(parsed.slug, parsed.role, parsed.n)
        rel_src = self._compute_relative_src(moved_to_processed, draft)

        try:
            current_html = draft.read_text(encoding="utf-8")
        except OSError:
            return ProcessResult(
                png_path=png_path,
                matched=True,
                target_html=draft,
                figure_replaced=False,
                moved_to=moved_to_processed,
            )

        updated_html, replaced = self._replace_figure_in_html(
            current_html,
            role=parsed.role,
            n=parsed.n,
            new_src=rel_src,
            new_alt=alt_text,
        )
        if not replaced:
            updated_html = self._append_figure_to_html(
                current_html,
                role=parsed.role,
                n=parsed.n,
                new_src=rel_src,
                new_alt=alt_text,
            )
            replaced = True  # append는 항상 성공

        try:
            self._atomic_write_text(draft, updated_html)
        except OSError:
            return ProcessResult(
                png_path=png_path,
                matched=True,
                target_html=draft,
                figure_replaced=False,
                moved_to=moved_to_processed,
            )

        return ProcessResult(
            png_path=png_path,
            matched=True,
            target_html=draft,
            figure_replaced=replaced,
            moved_to=moved_to_processed,
        )

    # ──────────────────────────────────────────────────────────────────────
    # reconcile (배치 동기화)
    # ──────────────────────────────────────────────────────────────────────

    def reconcile(self, slug: str, expected_slots: Optional[int] = None) -> ReconcileReport:
        """슬러그 1개에 대해 인박스에 있는 모든 PNG를 일괄 처리한다.

        watchdog 안 거치고 동기 호출하기 위한 진입점. 디지털 카테고리 등으로
        expected_slots를 7~8로 명시할 수 있다.
        """
        slots = expected_slots or DEFAULT_EXPECTED_SLOTS
        report = ReconcileReport(slug=slug, expected_slots=slots)

        # inbox_dir 직속(서브폴더 _processed/_unmatched 제외)의 PNG만 스캔
        for png in sorted(self.inbox_dir.iterdir()):
            if not png.is_file() or png.suffix.lower() != ".png":
                continue
            parsed = self.parse_filename(png.name)
            if parsed is None or parsed.slug != slug:
                continue
            result = self.process_file(png)
            if result.matched and result.figure_replaced:
                report.matched_count += 1
            else:
                report.unmatched_files.append(result.moved_to)

        # missing_slots 계산
        expected_tags = self._expected_slot_tags(slots)
        present_tags = self._present_slot_tags_in_processed(slug)
        for tag in expected_tags:
            if tag not in present_tags:
                report.missing_slots.append(tag)
        return report

    @staticmethod
    def _expected_slot_tags(total: int) -> list[str]:
        """기본 시퀀스 [thumb, body1, body2, info1, info2, info3] (디지털은 body3 또는 info4 추가)."""
        tags = ["thumb", "body1", "body2", "info1", "info2", "info3"]
        if total > DEFAULT_EXPECTED_SLOTS:
            extra = total - DEFAULT_EXPECTED_SLOTS
            # 디지털 폴백: info4부터 채워 넣는다.
            for k in range(extra):
                tags.append(f"info{4 + k}")
        return tags

    def _present_slot_tags_in_processed(self, slug: str) -> set[str]:
        tags: set[str] = set()
        for p in self.processed_dir.iterdir():
            if not p.is_file() or p.suffix.lower() != ".png":
                continue
            parsed = self.parse_filename(p.name)
            if parsed is None or parsed.slug != slug:
                continue
            tag = parsed.role if parsed.role == "thumb" else f"{parsed.role}{parsed.n}"
            tags.add(tag)
        return tags

    # ──────────────────────────────────────────────────────────────────────
    # watchdog 라이프사이클
    # ──────────────────────────────────────────────────────────────────────

    def start_watching(self) -> None:
        """daemon thread로 watchdog Observer 시작. 이미 가동 중이면 no-op."""
        if not _WATCHDOG_AVAILABLE or Observer is None:
            raise RuntimeError(
                "watchdog 미설치 — `pip install watchdog` 후 다시 시도하세요."
            )
        with self._observer_lock:
            if self._observer is not None:
                return
            handler = _InboxEventHandler(self)
            observer = Observer()
            observer.schedule(handler, str(self.inbox_dir), recursive=False)
            observer.daemon = True
            observer.start()
            self._observer = observer

    def stop_watching(self) -> None:
        """Observer 정상 종료. 미가동이면 no-op."""
        with self._observer_lock:
            observer = self._observer
            if observer is None:
                return
            try:
                observer.stop()  # type: ignore[attr-defined]
                observer.join(timeout=5)  # type: ignore[attr-defined]
            finally:
                self._observer = None

    # ──────────────────────────────────────────────────────────────────────
    # 내부 유틸
    # ──────────────────────────────────────────────────────────────────────

    @staticmethod
    def _wait_for_write_complete(path: Path) -> None:
        """파일 크기가 일정 시간 변하지 않을 때까지 짧게 polling."""
        if not path.exists():
            return
        previous = -1
        for _ in range(_WRITE_STABILIZE_ATTEMPTS):
            try:
                size = path.stat().st_size
            except OSError:
                return
            if size == previous and size > 0:
                return
            previous = size
            time.sleep(_WRITE_STABILIZE_INTERVAL_SEC)

    @staticmethod
    def _move_safely(src: Path, dest_dir: Path) -> Path:
        """이름 충돌 시 `_1`, `_2` 접미사로 회피하며 dest로 이동."""
        dest_dir.mkdir(parents=True, exist_ok=True)
        target = dest_dir / src.name
        counter = 1
        while target.exists():
            stem, suffix = src.stem, src.suffix
            target = dest_dir / f"{stem}_{counter}{suffix}"
            counter += 1
        try:
            shutil.move(str(src), str(target))
        except (OSError, shutil.Error):
            # 이동 실패해도 호출자가 다음 단계에서 처리할 수 있도록 원본 경로 반환
            return src
        return target

    @staticmethod
    def _atomic_write_text(path: Path, content: str) -> None:
        """tmp → rename 원자적 텍스트 쓰기."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fp:
            fp.write(content)
        tmp.replace(path)


# ──────────────────────────────────────────────────────────────────────────────
# watchdog 핸들러
# ──────────────────────────────────────────────────────────────────────────────


class _InboxEventHandler(FileSystemEventHandler):  # type: ignore[misc,valid-type]
    """on_created / on_moved에서 ImageInbox.process_file 호출."""

    def __init__(self, inbox: ImageInbox) -> None:
        super().__init__()
        self._inbox = inbox

    def on_created(self, event: FileSystemEvent) -> None:  # type: ignore[override]
        self._handle(event)

    def on_moved(self, event: FileSystemEvent) -> None:  # type: ignore[override]
        # watchdog FileMovedEvent는 dest_path 사용
        path = getattr(event, "dest_path", None) or getattr(event, "src_path", None)
        if path:
            self._dispatch(Path(path))

    def _handle(self, event: FileSystemEvent) -> None:
        if getattr(event, "is_directory", False):
            return
        path = getattr(event, "src_path", None)
        if not path:
            return
        self._dispatch(Path(path))

    def _dispatch(self, path: Path) -> None:
        if path.suffix.lower() != ".png":
            return
        try:
            self._inbox.process_file(path)
        except (OSError, ValueError):
            # 핸들러는 절대 죽지 않는다 — 다음 이벤트를 계속 처리.
            pass
