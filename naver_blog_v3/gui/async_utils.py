# gui/async_utils.py — 스레드 위생 키트 (GUI 개편 2026-09-02, 8호 재설계 2026-09-03)
"""워커 스레드가 Tk 를 만지지 않게 하는 수명 관리 믹스인.

설계(8호 재설계):
- **워커 스레드는 Tk 를 단 한 번도 직접 호출하지 않는다.** 워커의 `ui_after()` 는
  스레드 안전 deque 에 (fn, args, delay) 를 넣기만 하고, 메인 스레드가 50ms 간격의
  펌프(`after` 타이머)로 꺼내 실행한다. 크로스 스레드 `after()` 가 만들던 두 가지
  사고 — ① mainloop 시작 전 `RuntimeError` 로 콜백 증발(기록 탭 빈 표 고착)
  ② 워커가 락을 쥔 채 Tcl 락을 기다리고 메인은 Tcl 콜백 안에서 같은 락을
  기다리는 데드락(8호 실측) — 를 구조적으로 없앤다.
- 메인 스레드의 `ui_after()` 는 예전처럼 `after()` 로 즉시 예약한다(지연 예약은
  `_pending_after` 에 after-id 를 보관해 `destroy()` 가 취소).
- `destroy()` 는 플래그를 세우고 펌프·지연 예약을 취소한 뒤 실제 파괴.
"""
from __future__ import annotations

import collections
import logging
import threading
import tkinter as tk
from typing import Callable, Deque, List, Optional, Tuple

log = logging.getLogger(__name__)

_PUMP_INTERVAL_MS = 50
_Job = Tuple[Callable, tuple, int]


def _is_main_thread() -> bool:
    return threading.current_thread() is threading.main_thread()


class _LifecycleCore:
    """UiLifecycle/AppLifecycle 공통 구현. `_tk()` 가 after/after_cancel 을 가진 객체를 돌려준다."""

    _destroyed: bool
    _lifecycle_lock: threading.Lock
    _threads: List[threading.Thread]
    _pending_after: set
    _ui_queue: Deque[_Job]
    _pump_after_id: Optional[str]

    def _tk(self):  # pragma: no cover — 서브클래스가 정의
        raise NotImplementedError

    def _core_init(self) -> None:
        self._destroyed = False
        self._lifecycle_lock = threading.Lock()
        self._threads = []
        self._pending_after = set()
        self._ui_queue = collections.deque()
        self._pump_after_id = None
        self._arm_pump()

    # ── 펌프(메인 스레드 전용) ──────────────────────────────────
    def _arm_pump(self) -> None:
        if self._destroyed:
            return
        try:
            self._pump_after_id = self._tk().after(_PUMP_INTERVAL_MS, self._pump_ui_queue)
        except (tk.TclError, RuntimeError):
            self._pump_after_id = None

    def _pump_ui_queue(self) -> None:
        """큐에 쌓인 워커 콜백을 메인 스레드에서 실행하고 펌프를 재예약한다."""
        self._pump_after_id = None
        if self._destroyed:
            return
        drained = 0
        while self._ui_queue and drained < 200:
            fn, args, delay = self._ui_queue.popleft()
            drained += 1
            if self._destroyed:
                return
            if delay <= 0:
                try:
                    fn(*args)
                except tk.TclError:
                    pass
                except Exception:  # noqa: BLE001 — 콜백 예외가 펌프를 죽이면 안 된다
                    log.error("[lifecycle] 큐 콜백 예외", exc_info=True)
            else:
                self._schedule_delayed(fn, args, delay)
        self._arm_pump()

    # ── 공개 API ─────────────────────────────────────────────────
    def ui_after(self, fn: Callable, *args, delay: int = 0) -> None:
        """메인 스레드 재위임. 파괴됐으면 무시.

        - 워커 스레드: Tk 호출 없이 큐에 넣는다(펌프가 ≤50ms 안에 실행).
        - 메인 스레드: `after()` 로 즉시 예약. `delay>0` 은 `_pending_after` 에
          after-id 를 보관해 `destroy()` 가 취소한다(파괴 후 "invalid command name"
          방지, 2026-09-02 검증조 재현).
        """
        if self._destroyed:
            return
        if not _is_main_thread():
            self._ui_queue.append((fn, args, delay))
            return
        if delay <= 0:
            try:
                self._tk().after(0, fn, *args)
            except (tk.TclError, RuntimeError):
                pass
            return
        self._schedule_delayed(fn, args, delay)

    def _schedule_delayed(self, fn: Callable, args: tuple, delay: int) -> None:
        box: List[Optional[str]] = [None]

        def _fire() -> None:
            after_id = box[0]
            if after_id is not None:
                self._pending_after.discard(after_id)
            if self._destroyed:
                return
            fn(*args)

        try:
            after_id = self._tk().after(delay, _fire)
        except (tk.TclError, RuntimeError):
            return
        box[0] = after_id
        self._pending_after.add(after_id)

    def spawn(self, target: Callable, *args, name: Optional[str] = None,
              daemon: bool = True, **kwargs) -> threading.Thread:
        """백그라운드 스레드를 만들어 `_threads` 에 핸들을 보관한 뒤 시작한다."""
        th = threading.Thread(target=target, args=args, kwargs=kwargs,
                               name=name, daemon=daemon)
        self._threads.append(th)
        th.start()
        return th

    def _core_destroy(self) -> None:
        """플래그 → 펌프·지연 예약 취소 → 큐 폐기. 실제 위젯 파괴는 서브클래스가."""
        with self._lifecycle_lock:
            self._destroyed = True
            pending = list(self._pending_after)
            self._pending_after.clear()
            pump_id = self._pump_after_id
            self._pump_after_id = None
            self._ui_queue.clear()
        for after_id in ([pump_id] if pump_id else []) + pending:
            try:
                self._tk().after_cancel(after_id)
            except (tk.TclError, RuntimeError, ValueError):
                pass

    def is_destroyed(self) -> bool:
        return self._destroyed


class UiLifecycle(_LifecycleCore):
    """tk.Frame/tk.Toplevel 서브클래스와 다중상속으로 섞어 쓰는 믹스인.

    서브클래스 `__init__` 마지막에 반드시 `self._init_lifecycle()` 을 호출한다.
    """

    def _tk(self):
        return self

    def _init_lifecycle(self) -> None:
        """수명 상태 초기화 — 위젯 __init__ 끝에서 1회 호출."""
        self._core_init()

    def destroy(self) -> None:
        """파괴 플래그를 먼저 세우고 펌프·지연 예약을 취소한 뒤 실제 위젯을 파괴한다."""
        self._core_destroy()
        try:
            super().destroy()  # type: ignore[misc]
        except tk.TclError:
            pass


def call_on_main(root, fn: Callable, *args) -> None:
    """`root.after(0, fn, *args)` 재위임 — 예외(TclError/RuntimeError)는 삼킨다.

    UiLifecycle/AppLifecycle 을 쓰지 않는 단발성 헬퍼가 필요할 때 쓴다(메인 스레드 전용 권장).
    """
    try:
        root.after(0, fn, *args)
    except (tk.TclError, RuntimeError):
        pass


class AppLifecycle(_LifecycleCore):
    """`App` 처럼 tk 위젯이 아니라 `root: tk.Tk` 를 속성으로 갖는 객체용.

    `UiLifecycle` 과 같은 인터페이스(`ui_after`/`spawn`/`destroy`/`is_destroyed`)를
    제공하되, `destroy()` 는 root 자체를 파괴하지 않고 플래그·예약 취소만 한다 —
    창을 실제로 닫는 것은 `root.protocol("WM_DELETE_WINDOW")` 핸들러의 몫이다.
    """

    _lifecycle_root: "tk.Misc"

    def _tk(self):
        return self._lifecycle_root

    def _init_lifecycle(self, root: "tk.Misc") -> None:
        """수명 상태 초기화 — root 참조를 보관한다(App 은 위젯이 아니므로)."""
        self._lifecycle_root = root
        self._core_init()

    def destroy(self) -> None:
        """WM_DELETE_WINDOW 핸들러 등에서 호출 — 이후 ui_after 는 무시된다."""
        self._core_destroy()
