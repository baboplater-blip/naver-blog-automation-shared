# config/logger.py — 구조화된 로깅 (콘솔 + 파일 로테이션)
#
# 정책: 핸들러는 root logger("naver_blog_v3")에 단 한 번만 부착한다.
# 모듈에서 호출하는 get_logger(name)은 child logger를 돌려주며, 핸들러는
# 추가하지 않고 propagate로 root에 위임한다. 같은 파일을 여러 핸들러가
# 동시에 잡으면 RotatingFileHandler의 회전(os.rename)이 Windows에서
# WinError 32(파일 사용 중)로 실패하기 때문이다.

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


def _resolve_log_dir() -> Path:
    """app.log 디렉토리 — 프로필 라우팅(2026-07-19 격리 감사).

    우선순위: NBA_LOG_DIR 환경변수(테스트가 실서비스 로그 오염을 막는 격리 훅)
    > 활성 프로필 logs_dir()(named=profiles/<name>/logs, default=재배치 대상)
    > 레거시 앱루트 logs(프로필 레이어 부재 시).
    import 시 1회 확정 — 실행 중 .active 가 바뀌어도 로그 파일은 갈리지 않는다
    (한 프로세스=한 프로필·한 로그).
    """
    override = os.environ.get("NBA_LOG_DIR", "").strip()
    if override:
        return Path(override)
    try:
        from config import profile as _profile
        return _profile.logs_dir()
    except Exception:  # noqa: BLE001 — 부트스트랩/순환 실패 시 레거시
        return Path(__file__).parent.parent / "logs"


LOG_DIR = _resolve_log_dir()
LOG_DIR.mkdir(parents=True, exist_ok=True)

_ROOT_NAME = "naver_blog_v3"
_configured = False


class SafeRotatingFileHandler(RotatingFileHandler):
    """회전 실패(WinError 32 등)를 silent하게 넘기는 RotatingFileHandler.

    Windows에서 같은 파일을 다른 핸들/프로세스가 잡고 있으면 os.rename이
    PermissionError를 던진다. 표준 핸들러는 그때마다 stderr에 traceback을
    출력하는데, 발행 파이프라인 중에 노이즈가 심하므로 회전 실패는 무시하고
    기존 파일에 그대로 append를 계속한다. 다음 회전 기회에 정상화된다.
    """

    def doRollover(self) -> None:
        try:
            super().doRollover()
        except (PermissionError, OSError):
            try:
                if self.stream is None:
                    self.stream = self._open()
            except Exception:
                pass


def _configure_root_once() -> None:
    global _configured
    if _configured:
        return

    root = logging.getLogger(_ROOT_NAME)
    root.setLevel(logging.DEBUG)
    root.propagate = False

    # 이전 실행에서 남아있을 수 있는 핸들러 제거 (중복 부착 방지)
    for h in list(root.handlers):
        root.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(ch)

    fh = SafeRotatingFileHandler(
        LOG_DIR / "app.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
        delay=True,
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    ))
    root.addHandler(fh)

    _configured = True


def get_logger(name: str) -> logging.Logger:
    _configure_root_once()

    # 호출자가 어떤 이름을 주든 root("naver_blog_v3")의 자식으로 묶는다.
    if name == _ROOT_NAME or name.startswith(_ROOT_NAME + "."):
        full = name
    else:
        full = f"{_ROOT_NAME}.{name}"

    logger = logging.getLogger(full)
    logger.setLevel(logging.DEBUG)
    logger.propagate = True

    # 과거 버전이 추가했을 수 있는 자식 핸들러는 모두 정리한다.
    if logger is not logging.getLogger(_ROOT_NAME) and logger.handlers:
        for h in list(logger.handlers):
            logger.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass

    return logger
