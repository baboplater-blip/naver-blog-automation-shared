# gui/run_bus.py — 실행 상태의 단일 게시판 (신규, GUI 개편 2026-09-02)
"""`App` 이 1개 소유하는 실행 상태 브로드캐스터.

워커 스레드는 여기에만 말하고(status/progress/log/auth/begin/end), 화면을
그리는 쪽(전역 하단 활동 바·포스팅 탭 등)은 `subscribe()` 로 구독해 반영한다.
`_run`/`_run_fill_drafts` 의 `progress_cb`/`auth_cb` 는 이 버스의 메서드만
호출하고, 포스팅 탭이 다른 탭(app.pages["posting"])을 직접 만지는 결합은
여기서 끊는다.

모든 발행 메서드는 워커 스레드에서 호출해도 안전 — 메인 스레드가 아니면
app 의 `ui_after`(있으면) 또는 `app.root.after(0, ...)` 로 재위임한다.

auto-dev 5호(업그레이드1) — `stats` 에 실행 통계를 누적해 활동 바 팝오버가
"지금 N편째 · 완료/실패/건너뜀 · 최근 실패 3건 · 예상 남은 시간"을 그릴 수
있게 한다. `post_event()`/`progress()` 가 갱신하고, `estimate_remaining_seconds()`
는 그 dict 하나만으로 계산하는 순수 함수라 Tk 없이도 단위 테스트한다.
"""
from __future__ import annotations

import threading
import time
import tkinter as tk
from typing import Any, Callable, Dict, List, Optional

from gui.theme import strip_emoji


class RunBus:
    """App 이 1개 소유. 워커가 여기에만 말하고, 구독자가 화면을 그린다."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self._listeners: List[Any] = []
        # 마지막(또는 현재) 실행의 통계 — 실행 전엔 None("아직 실행 전이에요").
        # begin() 에서 새로 만들고, end() 이후에도 "마지막 실행 요약"으로
        # 남겨 둔다(다음 begin() 이 다시 초기화할 때까지).
        #
        # 스레드 안전(리뷰 🟡2): progress_cb 는 워커 스레드에서 호출되고,
        # 팝오버(메인 스레드)는 그와 동시에 self.stats 를 읽을 수 있다.
        # dict 를 **in-place mutate** 하면(예: stats["fail"] += 1 후
        # stats["failures"].append(...) 를 별도 줄로) 그 사이를 읽는 리더가
        # "fail 은 늘었는데 failures 는 아직 안 늘어난" 찢어진 상태를 볼 수
        # 있다. 그래서 모든 갱신은 `_stats_lock` 안에서 **완전히 새 dict 를
        # 만든 뒤 self.stats 에 통째로 재할당**한다 — 참조 재할당은 GIL 하
        # 원자적이라, 락 없이 그냥 `self.stats` 를 읽는 코드도 항상 "갱신
        # 전 dict 전체" 또는 "갱신 후 dict 전체" 둘 중 하나만 보고 찢어진
        # 중간 상태를 볼 일이 없다. `snapshot()` 은 그 원자적 읽기를 감싸는
        # 공식 진입점이다.
        self._stats_lock = threading.Lock()
        self.stats: Optional[Dict[str, Any]] = None

    # ── 구독 ──────────────────────────────────────────
    def subscribe(self, listener: Any) -> None:
        """listener 는 on_status/on_progress/on_log/on_auth/on_run_state 중
        구현한 메서드만 있으면 된다 — 없는 메서드는 조용히 스킵."""
        if listener not in self._listeners:
            self._listeners.append(listener)

    def unsubscribe(self, listener: Any) -> None:
        try:
            self._listeners.remove(listener)
        except ValueError:
            pass

    # ── 발행(워커 스레드 안전) ───────────────────────────
    def status(self, text: str, color: str = None) -> None:
        """app.set_status 로 위임 + on_status 구독자에게 fan-out.

        캡처 심사 3차 — `_strip_emoji`(현 `theme.strip_emoji`)가
        `App.set_status` 안에만 있어, 여기서 fan-out 되는 `on_status`
        구독자(활동 바 이외의 잠재적 청자)는 원문 그대로 받던 사각지대가
        있었다. 이 메서드가 실제 렌더 경계이므로 fan-out 전에 한 번만
        씻어 모든 구독자가 같은 정제된 문구를 받게 한다(`app.set_status`
        내부의 정제는 그대로 남아 있어 이중 적용이지만, 이미 정제된
        문자열에 다시 적용해도 결과는 같다 — 멱등).
        """
        clean = strip_emoji(text)
        set_status = getattr(self.app, "set_status", None)
        if callable(set_status):
            self._dispatch(set_status, clean, color)
        self._fanout("on_status", clean, color)

    def progress(self, cur: int, total: int, text: str = "") -> None:
        with self._stats_lock:
            if self.stats is not None:
                self.stats = {**self.stats, "cur": cur, "total": total}
        self._fanout("on_progress", cur, total, text)

    def log(self, msg: str, tag: str = "") -> None:
        """on_log 구독자(포스팅 탭 로그 박스 등)에게 fan-out 하기 전에
        이모지를 씻어낸다(캡처 심사 3차 — 실제로 걸린 렌더 경계: 활동 바는
        `set_status` 가 이미 막았지만 포스팅 로그 박스는 이 fan-out 을
        그대로 받아 그려 이모지가 그대로 나갔다)."""
        self._fanout("on_log", strip_emoji(msg), tag)

    def auth(self, state: str) -> None:
        """state: "waiting" | "done"."""
        self._fanout("on_auth", state)

    def begin(self, label: str) -> None:
        """실행 시작 — 활동 바에 "🔄 label" 표시 + 통계 dict 새로 초기화."""
        with self._stats_lock:
            self.stats = {
                "label": label,
                "started_at": time.time(),
                "finished_at": None,
                "cur": 0,
                "total": 0,
                "ok": 0,
                "fail": 0,
                "skip": 0,
                "last_title": "",
                "failures": [],   # list[tuple[str, str]] — (제목, 사유)
            }
        self._fanout("on_run_state", True, label)

    def post_event(self, title: str, status: str, reason: str = "") -> None:
        """항목 하나의 처리 결과를 통계에 누적한다(업그레이드1 — 실행 요약 팝오버).

        `progress_cb` 가 기존 `log()`/`progress()` 호출과 별도로 부른다.
        `begin()` 이전(= `self.stats is None`)이면 조용히 무시한다(호출
        순서가 어긋나도 죽지 않게 — fail-open). 락 안에서 새 dict(와 새
        `failures` 리스트)를 만들어 통째로 재할당한다(찢어진 스냅샷 방지 —
        위 `__init__` 주석 참조).

        Args:
            title: 처리 중인 글 제목.
            status: "start"(집계 변화 없음, last_title 만 갱신) | "ok" |
                "fail"(실패 사유를 failures 에 적립) | "duplicate"/"skip".
            reason: status="fail" 일 때의 사람이 읽을 사유(비어 있으면 팝오버가
                기본 문구로 대체한다). progress_cb 는 파이프라인에서 상세
                사유를 받지 못하므로 대개 빈 문자열이다.
        """
        with self._stats_lock:
            old = self.stats
            if old is None:
                return
            new = dict(old)
            new["last_title"] = title
            new["failures"] = list(old["failures"])  # 새 리스트 — 이전 스냅샷 불변 유지
            if status == "ok":
                new["ok"] = old["ok"] + 1
            elif status == "fail":
                new["fail"] = old["fail"] + 1
                new["failures"].append((title, reason))
            elif status in ("duplicate", "skip"):
                new["skip"] = old["skip"] + 1
            # status == "start" 는 집계 변화 없음 — 완료 시점에만 센다.
            self.stats = new

    def end(self, summary_text: str = "", kind: str = "ok") -> None:
        """실행 종료 — 활동 바 갱신 + (있으면) app.toast 로 요약 토스트.

        `stats` 는 지우지 않는다 — 유휴 상태의 팝오버가 "마지막 실행 요약"
        으로 보여준다(다음 `begin()` 이 새로 초기화할 때까지 유지).

        캡처 심사 3차 — `summary_text` 는 `on_run_state` fan-out(활동 바
        상태 문구가 됨)과 토스트 두 렌더 경계로 동시에 나가므로, 둘 다
        받기 전에 여기서 한 번만 씻는다. 이후 `stats`/팝오버가 재사용하는
        `last_summary` 도 이미 정제된 값이 저장된다.
        """
        clean_summary = strip_emoji(summary_text)
        with self._stats_lock:
            if self.stats is not None:
                self.stats = {**self.stats, "finished_at": time.time(),
                              "last_summary": clean_summary, "last_kind": kind}
        self._fanout("on_run_state", False, clean_summary)
        toast = getattr(self.app, "toast", None)
        if toast is not None and clean_summary:
            try:
                toast.show(clean_summary, kind=kind)
            except Exception:  # noqa: BLE001 — 토스트 실패가 실행 결과 통지를 막으면 안 된다
                pass

    def snapshot(self) -> Optional[Dict[str, Any]]:
        """`stats` 의 읽기 전용 복사본(리뷰 🟡2) — 읽는 쪽(팝오버)은 이걸
        쓴다. 락 안에서 참조만 캡처하고 락 밖에서 얕은 복사하므로 락 보유
        시간이 짧다 — `self.stats` 자체는 매 갱신마다 새 dict 로 통째로
        재할당되므로(위 `__init__` 주석), 이 시점 이후 그 dict 의 내용이
        바뀔 일은 없다(다음 갱신은 또 다른 새 dict 를 만들 뿐). 그래도
        `failures` 리스트까지 얕은 복사해 완전한 스냅샷을 보장한다.
        """
        with self._stats_lock:
            stats = self.stats
        if stats is None:
            return None
        return {**stats, "failures": list(stats.get("failures", []))}

    # ── 내부: 스레드 안전 디스패치 ────────────────────────
    def _fanout(self, method_name: str, *args) -> None:
        for listener in list(self._listeners):
            fn = getattr(listener, method_name, None)
            if callable(fn):
                self._dispatch(fn, *args)

    def _dispatch(self, fn: Callable, *args) -> None:
        """메인 스레드면 즉시 실행, 아니면 app 을 통해 메인 스레드로 재위임."""
        try:
            on_main = threading.current_thread() is threading.main_thread()
        except RuntimeError:
            on_main = True
        if on_main:
            self._call_safely(fn, *args)
            return
        ui_after = getattr(self.app, "ui_after", None)
        if callable(ui_after):
            ui_after(self._call_safely, fn, *args)
            return
        root = getattr(self.app, "root", None)
        if root is not None:
            try:
                root.after(0, self._call_safely, fn, *args)
            except (tk.TclError, RuntimeError):
                pass

    @staticmethod
    def _call_safely(fn: Callable, *args) -> None:
        try:
            fn(*args)
        except (tk.TclError, RuntimeError):
            pass


def estimate_remaining_seconds(stats: Optional[Dict[str, Any]]) -> Optional[int]:
    """평균 편당 소요 시간 × 남은 편수로 예상 잔여 시간(초)을 추정한다.

    Tk 의존 0 인 순수 함수 — `RunBus.stats` dict 하나만 받아 계산하므로
    App 없이도 단위 테스트할 수 있다(설계서 업그레이드1).

    다음 경우엔 추정할 수 없어 ``None`` 을 돌려준다: `stats` 자체가 없음
    (아직 실행 전) · `cur<=0`(아직 한 편도 안 끝남 — 평균을 낼 표본 없음) ·
    `total<=0`(전체 편수 미상) · `cur>=total`(이미 다 끝남 — 남은 게 없음) ·
    `started_at` 없음 · 경과 시간이 0 이하(시계 오차 등 이상치 방어).

    실행이 이미 끝났으면(`finished_at` 있음) 그 시각 기준 평균을 쓴다 —
    끝난 실행에 "지금부터"의 잔여 시간을 묻는 상황(예: 유휴 팝오버가 마지막
    실행 통계를 재사용)에서 `time.time()` 이 계속 흘러 평균이 왜곡되는
    것을 막는다.
    """
    if not stats:
        return None
    cur = stats.get("cur", 0)
    total = stats.get("total", 0)
    started_at = stats.get("started_at")
    if cur <= 0 or total <= 0 or cur >= total or not started_at:
        return None
    finished_at = stats.get("finished_at")
    now = finished_at if finished_at else time.time()
    elapsed = now - started_at
    if elapsed <= 0:
        return None
    avg_per_item = elapsed / cur
    remaining_items = total - cur
    return max(0, round(avg_per_item * remaining_items))
