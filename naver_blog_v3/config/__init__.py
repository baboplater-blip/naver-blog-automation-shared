import json
import os
import shutil
import logging
from pathlib import Path

_log = logging.getLogger(__name__)


def atomic_write_json(path: Path, data, _skip_bak: bool = False, **kwargs):
    """JSON 파일을 원자적으로 저장 (.tmp 에 쓴 뒤 rename).

    중간에 crash 가 발생해도 기존 파일이 손상되지 않습니다.
    기존 파일이 있으면 .bak으로 백업합니다 (최근 1개만 유지).

    Args:
        _skip_bak: True이면 .bak 백업을 건너뜁니다.
                   safe_load_json에서 .bak 복원 후 호출할 때 사용합니다.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    # ── T-06: 기존 파일을 .bak으로 백업 (최근 1개만 유지) ──
    if not _skip_bak and path.is_file():
        bak = path.with_suffix(".bak")
        try:
            shutil.copy2(str(path), str(bak))
        except OSError as e:
            _log.debug(f".bak 백업 실패: {path.name} — {e}")

    tmp = path.with_suffix(".tmp")
    kwargs.setdefault("ensure_ascii", False)
    kwargs.setdefault("indent", 2)
    tmp.write_text(json.dumps(data, **kwargs), encoding="utf-8")
    os.replace(str(tmp), str(path))


def safe_load_json(path: Path, default=None):
    """JSON 파일을 안전하게 로드. 손상 시 .bak 백업에서 복원 시도.

    성공하면 파싱 결과를, 실패하면 default를 반환합니다.
    """
    path = Path(path)
    bak = path.with_suffix(".bak")

    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            # 성공 시 백업 갱신
            try:
                shutil.copy2(str(path), str(bak))
            except OSError:
                pass
            return data
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            _log.warning(f"JSON 손상 감지: {path.name} — {e}")
            # 백업에서 복원 시도
            if bak.exists():
                try:
                    data = json.loads(bak.read_text(encoding="utf-8"))
                    _log.info(f"백업에서 복원 성공: {bak.name}")
                    # 복원된 데이터로 원본 파일 재저장
                    # _skip_bak=True: 손상된 원본이 정상 .bak을 덮어쓰는 것을 방지
                    atomic_write_json(path, data, _skip_bak=True)
                    return data
                except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                    _log.error(f"백업도 손상됨: {bak.name}")
        except FileNotFoundError:
            pass

    return default() if callable(default) else default


def reload_settings():
    """`config.settings` 를 디스크에서 다시 읽어 그 모듈을 돌려준다.

    **제품 계약**: GUI 설정 저장은 `settings.py` 를 다시 쓴다. 이미 떠 있는 프로세스가
    다음 판정부터 그 값을 쓰도록(= "저장 후 즉시 반영") 호출부는 값을 읽기 직전에
    이 함수를 부른다. 지금까지 네 곳이 각자 `importlib.reload(config.settings)` 를
    직접 불렀는데, 그 네 곳을 여기로 모았다 — 재로드 정책이 한 곳에만 있게.

    ⚠️ 이 호출의 부작용은 **프로세스 전역**이다. `importlib.reload` 는 모듈 전체를
    다시 실행하므로, 키 하나를 읽으려고 불러도 누군가 런타임에 올려둔 값이 전부
    디스크 값으로 되돌아간다. 운영에서는 그게 의도한 동작이지만(디스크가 정답),
    테스트·진단에서는 격리를 통째로 무너뜨린다 — 실측: `tests/test_shopping_connect.py`
    가 고정한 `PUBLISH_MODE="spawn"` 이 이 재로드에 씻겨나가 라이브 값(attach)으로
    돌아가 실 CDP 접속을 시도했다. 그래서 `NBA_NO_SETTINGS_RELOAD=1` 이면 재로드를
    건너뛰고 현재 모듈을 그대로 돌려준다. 그 환경변수는 테스트 부트스트랩
    (`tests/_isolation_bootstrap.py`)만 켜며, **운영에는 존재하지 않으므로 동작이
    이전과 동일하다**.

    Returns:
        `config.settings` 모듈. 임포트조차 못 하면 None(호출부는 폴백).
    """
    import sys as _sys

    mod = _sys.modules.get("config.settings")
    if mod is None:
        try:
            import config.settings as mod  # noqa: F811
        except Exception:  # noqa: BLE001 — settings 미로드 환경이면 호출부가 폴백
            return None
    if os.environ.get("NBA_NO_SETTINGS_RELOAD") == "1":
        return mod
    try:
        import importlib
        importlib.reload(mod)
    except Exception:  # noqa: BLE001 — 재로드 실패해도 현재 모듈을 그대로 쓴다
        pass
    return mod
