# core/queue_sort_cache.py — 발행 큐 정렬 결과 캐시 (2026-09-02)
"""``scheduler.auto_scheduler._sort_by_trend_priority`` 결과 캐시.

그 함수는 발행 큐(실측 1,797건)마다 검색량 인덱스(엑셀 스캔)·데이터랩
(trend_data.json)·마감(deadline_data.json)·뉴스 레이더 신호를 전부 다시 읽고
계산해 홈 탭 '발행 준비' 신호등이 ~19초 걸리는 원인이 된다. 이 모듈은

    키 = sha1( 정렬 대상 제목 튜플(순서 포함) + 입력 데이터 지문 )

으로 캐시를 만들어, 같은 큐 + 같은 소스 데이터면 재계산 없이 이전 정렬
순서를 재사용한다. 지문에는 정렬이 실제로 읽는 파일들의 (경로, mtime_ns,
size)가 들어가므로 소스 파일이 하나라도 바뀌면 자동으로 미스 처리된다.

``_sort_by_trend_priority`` 는 실발행 경로(``get_pending_posts``)도 타는
라이브 코드라 캐시 히트 결과는 캐시 없는 전체 재계산 결과와 **반드시
바이트 동일**해야 한다 — 이 모듈은 값을 만들지 않고 호출부가 이미 계산한
정렬 순서 + 근거를 그대로 저장/재생할 뿐이다.

전 경로 fail-open: 조회·저장 어느 단계든 예외가 나면 캐시가 없었던 것처럼
동작한다(호출부가 항상 예외를 잡고 기존 전체 재계산 경로로 진행).
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Optional

from config import atomic_write_json, safe_load_json
from config.logger import get_logger

log = get_logger(__name__)

TTL_SECONDS = 6 * 60 * 60          # 6시간 — 이보다 오래된 캐시는 신선도 방어로 미스 처리
MIN_QUEUE_SIZE = 50                # 이 미만 큐는 캐시 미사용(테스트·소규모 실행은 항상 실계산)
_CACHE_FILENAME = "queue_sort_cache.json"


def _enabled() -> bool:
    """settings.QUEUE_SORT_CACHE_ENABLED (기본 True). 로드 실패는 활성으로 취급."""
    try:
        import config.settings as _settings
        return bool(getattr(_settings, "QUEUE_SORT_CACHE_ENABLED", True))
    except Exception:  # noqa: BLE001
        return True


def _cache_path() -> Path:
    from config import profile as _profile
    return _profile.logs_dir() / _CACHE_FILENAME


def _fp_file(path: Path) -> list:
    """(경로문자열, mtime_ns, size). 파일 부재는 정상 상태(0, 0) — '알 수 없음'과 구분."""
    try:
        if path.exists():
            st = path.stat()
            return [str(path), int(st.st_mtime_ns), int(st.st_size)]
        return [str(path), 0, 0]
    except OSError:
        return [str(path), -1, -1]


def _fingerprint() -> list:
    """정렬 입력으로 쓰이는 소스 파일 전부의 지문.

    소스 하나를 못 구해도(임포트 실패 등) 그 항목만 'unknown:<name>' 문자열로
    남기고 나머지는 계속 수집한다 — 그 소스가 바뀌어도 캐시가 못 알아채는
    위험은 TTL(6시간)이 방어한다.
    """
    fp: list = []

    try:
        from config import profile as _profile
        logs = _profile.logs_dir()
        fp.append(_fp_file(logs / "trend_data.json"))
        # ⚠️ 점검(2026-09-02): _load_deadline_days() 는 deadline_data.json 에 이미
        # 계산돼 저장된 정적 "days_left" 필드를 그대로 읽을 뿐 date.today() 로
        # 재계산하지 않는다(그 재계산은 deadline_tracker 수집 시점에 끝나 파일에
        # 박힌다) — 즉 이 값이 바뀌는 유일한 경로는 파일 자체가 다시 쓰이는 것뿐이라
        # 위 mtime/size 지문이 이미 완전히 커버한다. news_radar.signal_map() 처럼
        # "파일은 그대로인데 datetime.now() 만으로 결과가 뒤집히는" 시간 드리프트가
        # 없어 date.today() 를 별도로 지문에 넣지 않는다.
        fp.append(_fp_file(logs / "deadline_data.json"))
    except Exception:  # noqa: BLE001
        fp.append("unknown:trend_deadline")

    try:
        from core import news_radar as _nr
        fp.append(_fp_file(Path(_nr.STATE_PATH)))
    except Exception:  # noqa: BLE001
        fp.append("unknown:news_radar")

    try:
        # news_radar.signal_map() 은 news_radar.json 의 mtime 이 전혀 안 바뀌어도
        # 시간 경과만으로(기본 30시간 스테일 판정, datetime.now() 기준) {} 로
        # 뒤집힐 수 있다 — 그 순간 발행 큐의 뉴스 가산점(+8/+6, boost_for())이
        # 사라져 정렬 순서가 바뀌는데 파일 (mtime, size) 지문만으론 이 전환을
        # 못 잡는다(캐시 TTL 6h 안에서 30h 경계를 넘는 경우가 실제로 있다).
        # 판정 로직을 재구현하지 않고 news_radar 자신의 signal_map() 을 그대로
        # 호출해 "지금 신선한 신호가 있는가"를 지문에 넣는다 — signal_map() 이
        # 빈 dict 를 돌려주면(신호 없음·스테일 둘 다) boost_for() 결과가 항상
        # 0 이라 두 경우를 구분할 필요가 없다.
        from core import news_radar as _nr2
        fp.append(("news_fresh", bool(_nr2.signal_map())))
    except Exception:  # noqa: BLE001
        fp.append("unknown:news_fresh")

    try:
        from core import excluded_titles as _et
        fp.append(_fp_file(Path(_et.EXCLUDED_TITLES_PATH)))
    except Exception:  # noqa: BLE001
        fp.append("unknown:excluded_titles")

    try:
        # search_volume.volume_index() 가 실제로 스캔하는 파일 목록을 그대로
        # 재현한다(같은 디렉토리 해석 + 같은 이름 필터) — 새 검색량 엑셀이
        # 추가/갱신되면 지문이 바뀌어 자동으로 미스 처리된다.
        from core import search_volume as _sv
        scan_dir = _sv._scan_dir()
        entries = []
        for f in sorted(scan_dir.glob("*.xlsx")):
            name = f.name
            low = name.lower()
            if "bak" in low or "old" in low or "검색량" not in name:
                continue
            entries.append(_fp_file(f))
        fp.append(entries)
    except Exception:  # noqa: BLE001
        fp.append("unknown:search_volume")

    return fp


def _cache_key(titles: list) -> str:
    payload = json.dumps([list(titles), _fingerprint()], ensure_ascii=False,
                          sort_keys=False, default=str)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _titles_of(pending: list) -> list:
    return [item.get("title", "") for item in pending]


def try_get(pending: list) -> Optional[dict]:
    """캐시 히트 시 ``{"order": [...제목], "rationale": {...}, "age_s": float}``.

    미스·비활성·소규모 큐·손상 캐시·TTL 만료는 전부 None(호출부는 기존
    전체 재계산 경로로 진행).
    """
    if not _enabled() or len(pending) < MIN_QUEUE_SIZE:
        return None
    try:
        key = _cache_key(_titles_of(pending))
        raw = safe_load_json(_cache_path(), default=dict)
        if not isinstance(raw, dict) or raw.get("key") != key:
            return None
        created = raw.get("created")
        if not isinstance(created, (int, float)):
            return None
        age = time.time() - created
        if age < 0 or age > TTL_SECONDS:
            return None
        order = raw.get("order")
        rationale = raw.get("rationale")
        if not isinstance(order, list) or not isinstance(rationale, dict):
            return None
        return {"order": order, "rationale": rationale, "age_s": age}
    except Exception as exc:  # noqa: BLE001
        log.debug(f"  정렬 캐시 조회 실패(무시): {exc}")
        return None


def store(pending: list, order_titles: list, rationale: dict) -> None:
    """정렬 결과를 캐시에 저장한다. ``pending`` 은 정렬 *이전*(제외 처리 후) 리스트여야
    ``try_get`` 이 만드는 키와 일치한다. 실패해도 무시(fail-open) — 다음 호출은 재계산."""
    if not _enabled() or len(pending) < MIN_QUEUE_SIZE:
        return
    try:
        key = _cache_key(_titles_of(pending))
        atomic_write_json(_cache_path(), {
            "key": key,
            "order": list(order_titles),
            "rationale": rationale,
            "created": time.time(),
        })
    except Exception as exc:  # noqa: BLE001
        log.debug(f"  정렬 캐시 저장 실패(무시): {exc}")


def reorder(pending: list, order: list) -> list:
    """캐시된 제목 순서(``order``)대로 ``pending`` 항목을 재배열.

    캐시에 없는 제목(새 후보)은 원래 순서 그대로 뒤에 붙이고, 캐시에만
    있고 지금은 없는 제목(이미 발행·제외됨)은 버린다.
    """
    by_title: dict = {}
    for item in pending:
        by_title.setdefault(item.get("title", ""), []).append(item)

    result = []
    used_ids = set()
    for title in order:
        bucket = by_title.get(title)
        if not bucket:
            continue
        item = bucket.pop(0)
        result.append(item)
        used_ids.add(id(item))

    for item in pending:
        if id(item) not in used_ids:
            result.append(item)

    return result
