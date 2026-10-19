# core/search_volume.py
"""검색량 진실원본(single source of truth) — 보유 엑셀 검색량 데이터 조회.

무료 네이버 API로는 절대 검색량(월 조회수)을 얻을 수 없고 데이터랩은 0~100
상대지수만 준다. 그래서 파생/발행 키워드가 실제 검색 수요에 앵커돼 있는지는
사용자가 관리하는 '*키워드*검색량*.xlsx' 파일들을 인덱싱해 판정한다.

용도:
  - keyword_expander: 파생 키워드가 볼륨 하한 미달이면 폐기
  - series_planner:   광역 키워드 볼륨 기반으로 편수(팬아웃) 상한 결정
  - auto_scheduler:   발행 직전 볼륨 게이트(2차 방어선)

설계 원칙: 엑셀이 아예 없으면(인덱스 비면) 게이트를 무력화하지 않도록
meets_threshold 가 True(fail-open)를 돌려준다 — 데이터 부재로 전체 발행을
막지 않는다.
"""
from __future__ import annotations

import glob
import re
from functools import lru_cache
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent   # naver_blog_v3/

# 엑셀 상 최소 유효 키워드 길이(공백 제거 후) — 잡음 토큰 배제
_MIN_KW_LEN = 4


def _despace(s: str) -> str:
    """공백·기호 제거 + 소문자화(영문). 한글은 그대로."""
    return re.sub(r"[^가-힣A-Za-z0-9]", "", (s or "")).lower()


def _scan_dir() -> Path:
    """검색량 엑셀을 스캔할 디렉토리.

    named 프로필이면 profiles/<name>/keywords 를 우선(브랜드별 검색량 엑셀).
    default 프로필(또는 프로필 폴더 미존재)이면 기존 BASE_DIR(naver_blog_v3/).
    한 프로세스=한 프로필이라 volume_index 의 lru_cache 와 함께 안전하게 고정된다.
    profile import/조회 실패 시 BASE_DIR 로 폴백(동작 무변화).
    """
    try:
        from config import profile as _profile  # local import — 지연·순환 회피
        kd = _profile.keywords_dir()
        if kd is not None and kd.is_dir():
            return kd
    except (ImportError, AttributeError, OSError) as exc:
        # 프로필 레이어 부재/조회 실패 — 레거시 경로로 폴백(로깅 최소, 라이브 무중단).
        del exc
    return BASE_DIR


@lru_cache(maxsize=1)
def volume_index() -> dict:
    """{공백제거 키워드: 최대 검색량}. 엑셀이 없거나 openpyxl 미설치면 빈 dict.

    '*키워드*검색량*.xlsx' 이름의 파일만 스캔하고 bak/OLD 백업은 제외한다.
    각 시트에서 인접 열쌍 (키워드, 숫자)을 검색량 쌍으로 수집한다.
    스캔 소스는 활성 프로필 기준(_scan_dir) — default 는 기존 BASE_DIR.
    """
    idx: dict = {}
    try:
        from openpyxl import load_workbook
    except ImportError:
        return idx

    for f in glob.glob(str(_scan_dir() / "*.xlsx")):
        name = Path(f).name
        low = name.lower()
        if "bak" in low or "old" in low:
            continue
        if "검색량" not in name:
            continue
        try:
            wb = load_workbook(f, read_only=True, data_only=True)
        except Exception:
            continue
        try:
            for ws in wb.worksheets:
                for row in ws.iter_rows(values_only=True):
                    for c in range(len(row) - 1):
                        k, v = row[c], row[c + 1]
                        if isinstance(k, str) and isinstance(v, (int, float)):
                            kk = _despace(k)
                            if len(kk) >= 3 and v > 0:
                                idx[kk] = max(idx.get(kk, 0), int(v))
        finally:
            wb.close()
    return idx


def best_keyword(text: str) -> tuple[str, int]:
    """text 안에 부분문자열로 포함된 알려진 키워드 중 최고 볼륨 (키워드, 볼륨).

    매칭 없으면 ('', 0). 예: '2026 청년도약계좌 군 복무…' → ('청년도약계좌', 357000).
    """
    nt = _despace(text)
    if not nt:
        return "", 0
    bk, bv = "", 0
    for kk, v in volume_index().items():
        if len(kk) >= _MIN_KW_LEN and kk in nt and v > bv:
            bk, bv = kk, v
    return bk, bv


def best_volume(text: str) -> int:
    """text 에 앵커된 최고 검색량. 매칭 없으면 0."""
    return best_keyword(text)[1]


def meets_threshold(text: str, min_volume: int = 500) -> bool:
    """text 가 검색량 하한 이상 키워드에 앵커돼 있는지.

    엑셀 데이터가 전혀 없으면(인덱스 빈) 게이트를 끄기 위해 True(fail-open).
    """
    if not volume_index():
        return True
    return best_volume(text) >= min_volume


def effective_volume(text: str, *, use_live_demand: bool = True) -> int:
    """엑셀 실측 볼륨. 엑셀에 없지만(0) 네이버 자동완성에 뜨는 '실검색어'면
    LIVE_DEMAND_FLOOR_VOLUME 로 바닥값을 부여해 랭킹·게이트에서 0 취급되는 걸 막는다.

    - 엑셀 볼륨이 있으면(>0) 그 실측값을 그대로 반환(floor 미적용).
    - 엑셀 0 + 자동완성 실검색 확인 시에만 floor. 조회 실패/미확인은 0 유지(과대평가 방지).
    - LIVE_DEMAND_GATE_ENABLED=False 면 순수 엑셀값(기존 동작).
    ⚠️ live_demand 는 네트워크(캐시)라 순수함수 아님 — 랭킹·파생 등 배치 경로에서만 쓴다.
    """
    v = best_volume(text)
    if v > 0 or not use_live_demand:
        return v
    # 1순위 폴백: 네이버 검색광고 API 실측 월간검색수(켜져 있고 키 있으면).
    #   floor(500) 근사가 아니라 '진짜 볼륨'이라 랭킹이 정확해진다. 미설정/실패면 None → 다음.
    try:
        from core.searchad_volume import available as _sa_avail, monthly_volume as _sa_vol
        if _sa_avail():
            real = _sa_vol(text)
            if real is not None and real > 0:
                return int(real)
    except Exception:  # noqa: BLE001 — 검색광고 실패는 자동완성 floor 로 폴백
        pass
    # 2순위 폴백: 자동완성 실검색 확인 시 바닥 볼륨(floor).
    try:
        from config import settings as _s
        if not getattr(_s, "LIVE_DEMAND_GATE_ENABLED", True):
            return v
        floor = int(getattr(_s, "LIVE_DEMAND_FLOOR_VOLUME", 500))
    except Exception:  # noqa: BLE001
        return v
    try:
        from core.live_demand import live_demand_ok
        if live_demand_ok(text) is True:
            return floor
    except Exception:  # noqa: BLE001 — 판정 실패는 바닥값 미부여(엑셀값 유지)
        pass
    return v


def meets_threshold_live(text: str, min_volume: int = 500) -> bool:
    """meets_threshold 의 수요-인지 버전 — 엑셀 미달이어도 자동완성 실검색어면 통과.

    엑셀 데이터가 전혀 없으면(인덱스 빈) fail-open(True) — meets_threshold 와 동일.
    """
    if not volume_index():
        return True
    return effective_volume(text) >= min_volume


def recommend_episode_count(text: str, max_ep: int = 3) -> int:
    """광역 키워드 볼륨 기반 권장 시리즈 편수(팬아웃 상한).

    대형(≥50k)→3, 중형(≥5k)→2, 소형/무매칭→1. max_ep 로 상한 캡.
    엑셀 데이터가 없으면 보수적으로 1(과잉 분해 방지)."""
    v = best_volume(text)
    if v >= 50_000:
        ep = 3
    elif v >= 5_000:
        ep = 2
    else:
        ep = 1
    return max(1, min(ep, max_ep))
