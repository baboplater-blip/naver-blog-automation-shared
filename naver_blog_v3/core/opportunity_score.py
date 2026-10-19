# core/opportunity_score.py — 키워드 기회 점수 (Phase 2 · L2)
"""무엇을 다음에 발행할 것인가? — 추측이 아니라 점수로 결정한다.

기회점수 = 수요(검색량) × 승산(경쟁 역수) × 권위적합(우리 카테고리 축적) ×
           신선도(트렌드) × 실측수요(Phase 1 유입) − 자기잠식 페널티.

무수요 초-롱테일 과잉 세분화(과거 59% 문제)를 구조적으로 차단하고, 수요·승산·권위가
정렬된 키워드를 발행 큐 상단으로 올린다. Phase 1 유입 데이터가 없어도 보유 엑셀
검색량 + 232편 카탈로그 + 트렌드로 동작하며, 유입 데이터가 도착하면 정밀해진다.

- 순수 함수 중심(외부 데이터 주입 가능) → 오프라인 테스트 가능. fail-open.
"""
from __future__ import annotations

import math
import os
import sys
from pathlib import Path

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.logger import get_logger

log = get_logger(__name__)
BASE_DIR = Path(__file__).parent.parent

try:
    from config.settings import MIN_SEARCH_VOLUME
except (ImportError, AttributeError):
    MIN_SEARCH_VOLUME = 500

# 가중치(합 100). 자기잠식은 곱셈 페널티로 별도 적용.
W_DEMAND    = 35   # 검색량
W_WINNABLE  = 25   # 승산(경쟁 역수)
W_AUTHORITY = 15   # 우리 카테고리 축적
W_TREND     = 10   # 트렌드 신선도
W_REALDEMAND = 15  # Phase 1 실측 유입 인접

# 토큰 수 → 승산 곡선 (2~3어절 롱테일이 스윗스팟, 1어절 헤드/5+어절 초니치는 낮음)
_WIN_BY_TOKENS = {1: 0.30, 2: 0.90, 3: 1.00, 4: 0.70, 5: 0.45}


def _tokens(kw: str) -> int:
    return len([t for t in kw.split() if t])


def _specificity_factor(kw: str) -> float:
    """토큰이 깊을수록 앵커 볼륨의 실수요는 줄어든다 — 과분해를 수요축에서도 감점."""
    tok = _tokens(kw)
    return {1: 1.0, 2: 1.0, 3: 1.0, 4: 0.8, 5: 0.6}.get(tok, 0.45)


def _demand_score(volume: int, kw: str = "") -> float:
    """검색량 → 0..1 (로그 스케일, 100k+ 포화). 하한 미달·과분해는 급감."""
    if volume <= 0:
        return 0.0
    spec = _specificity_factor(kw) if kw else 1.0
    if volume < MIN_SEARCH_VOLUME:
        return 0.15 * (volume / MIN_SEARCH_VOLUME) * spec   # 무수요 근처 강한 감점
    return min(1.0, math.log10(volume) / 5.0) * spec        # 10^5=100k → 1.0


def _winnability(volume: int, kw: str) -> float:
    """승산 = 토큰 곡선 × 볼륨 경쟁계수(초고볼륨 헤드는 경쟁 과다)."""
    tok = _tokens(kw)
    base = _WIN_BY_TOKENS.get(tok, 0.35 if tok >= 5 else 0.3)
    if volume >= 200_000 and tok <= 1:
        base *= 0.5     # 초대형 단일어 헤드 = 상위노출 난이도 과다
    elif volume >= 50_000 and tok <= 1:
        base *= 0.7
    return max(0.0, min(1.0, base))


def _authority_fit(category: str, cat_counts: dict) -> float:
    """우리가 그 카테고리에 글이 많을수록 C-Rank 맥락 축적 → 새 글도 잘 뜬다.

    단, 과포화(too many)면 한계효용 감소. 8~30편 구간이 스윗(맥락 충분, 잠식 낮음).
    """
    n = cat_counts.get(category, 0)
    if n <= 0:
        return 0.35                     # 신규 카테고리 — 중립(개척 가치)
    if n < 8:
        return 0.5 + 0.05 * n           # 축적 중
    if n <= 40:
        return 1.0                      # 권위 충분
    return max(0.5, 1.0 - (n - 40) * 0.01)   # 과포화 감점


def _feedback_factor(category: str, weights: dict | None) -> float:
    """측정 기반 카테고리 가중치 환류(Phase 1 measurement.feedback_weights()).

    `weights` 가 비어 있으면(=표본/커버리지 게이트 미달로 held) **1.0(무변화)** —
    호출자가 held_reason 이 있을 때 weights={} 를 넘기는 것으로 이 함수가 현행과
    바이트 동일하게 동작하도록 강제한다. weights 가 있어도 해당 카테고리가 없으면
    (신규·미측정 카테고리) 역시 1.0. 과도한 쏠림을 막기 위해 평균 대비 배율을
    ±15% 로 클램프한다.
    """
    if not weights or not category or category not in weights:
        return 1.0
    n = len(weights)
    if n <= 0:
        return 1.0
    avg = 1.0 / n
    if avg <= 0:
        return 1.0
    ratio = weights[category] / avg
    return max(0.85, min(1.15, ratio))


def _cannibalization(kw_key: str, targeted_keys: set) -> float:
    """이미 거의 같은 키워드로 발행한 글이 있으면 자기잠식 → 페널티 배수(0.25~1.0)."""
    if not kw_key:
        return 1.0
    if kw_key in targeted_keys:
        return 0.25                     # 사실상 중복 — 강한 감점
    # 부분 포함(한쪽이 다른쪽을 포함) = 근접 잠식
    for t in targeted_keys:
        if t and (kw_key in t or t in kw_key) and abs(len(kw_key) - len(t)) <= 3:
            return 0.55
    return 1.0


def _resolve_winnability(keyword: str, volume: int, *,
                         real_win: float | None) -> tuple[float, bool]:
    """승산과 검증 여부를 반환.

    real_win(실측 SERP 승산)이 주어지면 그 값을 쓰고 verified=True.
    없으면 기존 어절 휴리스틱(_winnability)으로 폴백하고 verified=False(unverified).
    """
    if real_win is not None:
        return max(0.0, min(1.0, float(real_win))), True
    return _winnability(volume, keyword), False


def score_one(keyword: str, *, volume: int | None = None, category: str = "",
              cat_counts: dict | None = None, targeted_keys: set | None = None,
              trend_set: set | None = None, realdemand_set: set | None = None,
              real_win: float | None = None, feedback_weights: dict | None = None) -> dict:
    """단일 키워드 기회점수 + 근거 분해.

    real_win: 실측 SERP 승산(serp_competition.real_winnability 결과). 주어지면
              어절 상수 대신 이 값을 승산으로 쓰고 breakdown 에 winnable_verified=True.
              None 이면 기존 어절 휴리스틱으로 폴백(winnable_verified=False, fail-open).
    feedback_weights: measurement.feedback_weights()["weights"] — held_reason 이
              있으면(=표본/커버리지 게이트 미달) 호출자가 반드시 {} 를 넘겨야 하고,
              그러면 _feedback_factor 가 1.0 을 돌려줘 점수가 한 톨도 바뀌지 않는다
              (계약: 측정이 서지 않은 상태에서 자동으로 기획을 바꾸지 않는다).
    """
    from core.performance_db import normalize_key

    cat_counts = cat_counts or {}
    targeted_keys = targeted_keys or set()
    trend_set = trend_set or set()
    realdemand_set = realdemand_set or set()
    feedback_weights = feedback_weights or {}

    if volume is None:
        try:
            # effective_volume: 엑셀 미등록이어도 자동완성 실검색어면 바닥 볼륨 부여
            # → 실검색 온-브랜드 주제가 볼륨 0 으로 맨 뒤에 밀리는 문제 완화(2026-07-15).
            from core.search_volume import effective_volume
            volume = int(effective_volume(keyword) or 0)
        except (ImportError, AttributeError, TypeError, ValueError):
            volume = 0
    if not category:
        try:
            from core.content_generator import detect_category
            category = detect_category(keyword)
        except (ImportError, AttributeError, TypeError, ValueError):
            category = ""

    key = normalize_key(keyword)
    d = _demand_score(volume, keyword)
    w, w_verified = _resolve_winnability(keyword, volume, real_win=real_win)
    a = _authority_fit(category, cat_counts)
    t = 1.0 if key in trend_set else 0.0
    r = 1.0 if key in realdemand_set else 0.0

    raw = (W_DEMAND * d + W_WINNABLE * w + W_AUTHORITY * a
           + W_TREND * t + W_REALDEMAND * r)
    penalty = _cannibalization(key, targeted_keys)
    feedback_x = _feedback_factor(category, feedback_weights)
    score = round(raw * penalty * feedback_x, 1)

    return {
        "keyword": keyword, "score": score, "volume": volume, "category": category,
        "breakdown": {"demand": round(d, 2), "winnable": round(w, 2),
                      "winnable_verified": w_verified,
                      "authority": round(a, 2), "trend": t, "realdemand": r,
                      "cannibal_x": penalty, "feedback_x": round(feedback_x, 3)},
        "verdict": _verdict(score, penalty, volume),
    }


def _verdict(score: float, penalty: float, volume: int) -> str:
    if penalty <= 0.25:
        return "잠식위험(스킵)"
    if volume < MIN_SEARCH_VOLUME:
        return "무수요(스킵)"
    if score >= 70:
        return "발행 1순위"
    if score >= 55:
        return "발행 후보"
    if score >= 40:
        return "보류(약)"
    return "스킵"


# ─────────────────────────────────────────────────────
# 컨텍스트 로더 (카탈로그·트렌드·Phase1 유입)
# ─────────────────────────────────────────────────────
def _load_feedback_weights() -> dict:
    """Use the latest selected, same-window search-inflow cohort when eligible.

    이렇게 하면 아래 소비자(score_one/_feedback_factor)는 "게이트 미달"과
    "measurement 모듈 부재"를 구분할 필요 없이 항상 같은 무변화(1.0) 경로를 탄다.
    fail-open — measurement 모듈이 없거나 읽기가 실패해도 후보 점수 산정 자체는
    막지 않는다.
    """
    try:
        from core import measurement
        fw = measurement.selected_feedback_weights()
    except (ImportError, AttributeError, TypeError, ValueError, KeyError):
        return {}
    if fw.get("held_reason"):
        return {}
    return fw.get("weights") or {}


def _build_context() -> dict:
    """카탈로그 카테고리 분포 + 이미 타깃한 키워드 + 트렌드 + Phase1 기회 + 측정 환류."""
    from core.performance_db import normalize_key
    ctx = {"cat_counts": {}, "targeted_keys": set(),
           "trend_set": set(), "realdemand_set": set(), "feedback_weights": {},
           "feedback_provenance": {}, "feedback_held_reason": ""}
    try:
        from core import performance_db as pdb
        data = pdb.load()
        for rec in data.get("posts", {}).values():
            cat = rec.get("category") or ""
            if cat:
                ctx["cat_counts"][cat] = ctx["cat_counts"].get(cat, 0) + 1
            kw = rec.get("keyword") or rec.get("clean_title") or ""
            if kw:
                ctx["targeted_keys"].add(normalize_key(kw))
        for o in pdb.adjacent_keyword_opportunities(n=100, data=data):
            ctx["realdemand_set"].add(normalize_key(o["keyword"]))
    except (ImportError, OSError, ValueError, KeyError):
        pass
    try:
        from core.trend_updater import get_top_keywords
        for t in get_top_keywords(30):
            ctx["trend_set"].add(normalize_key(t.get("keyword", "")))
    except (ImportError, AttributeError, KeyError):
        pass
    try:
        from core import measurement
        feedback = measurement.selected_feedback_weights()
        ctx["feedback_weights"] = (feedback.get("weights") or {}) if not feedback.get("held_reason") else {}
        ctx["feedback_provenance"] = feedback.get("provenance") or {}
        ctx["feedback_held_reason"] = str(feedback.get("held_reason") or "")
    except (ImportError, AttributeError, TypeError, ValueError, KeyError):
        ctx["feedback_weights"] = _load_feedback_weights()
    return ctx


def score_candidates(candidates, *, context: dict | None = None) -> list[dict]:
    """후보 리스트(문자열 또는 {'keyword':...} dict) → 점수 내림차순 정렬."""
    ctx = context or _build_context()
    out = []
    seen = set()
    from core.performance_db import normalize_key
    for c in candidates:
        kw = c if isinstance(c, str) else c.get("keyword", "")
        if not kw:
            continue
        k = normalize_key(kw)
        if k in seen:
            continue
        seen.add(k)
        vol = c.get("volume") if isinstance(c, dict) else None
        scored = score_one(kw, volume=vol, cat_counts=ctx["cat_counts"],
                             targeted_keys=ctx["targeted_keys"],
                             trend_set=ctx["trend_set"],
                             realdemand_set=ctx["realdemand_set"],
                             feedback_weights=ctx.get("feedback_weights"))
        scored["feedback_provenance"] = dict(ctx.get("feedback_provenance") or {})
        scored["feedback_held_reason"] = str(ctx.get("feedback_held_reason") or "")
        out.append(scored)
    out.sort(key=lambda r: r["score"], reverse=True)
    return out


def verify_serp_winnability(scored: list[dict], *, ctx: dict, verify_top: int,
                            fetcher=None) -> list[dict]:
    """상위 verify_top 후보만 실측 SERP 승산으로 재점수화 후 재정렬.

    배치 SERP 조회는 비싸므로(키워드당 1 HTTP + 파싱) 전체가 아니라 상위 후보만
    검증한다. 못 이길 헤드(공식/광고 지배)는 실측 승산이 급락해 자동 후순위로 밀린다.
    실측 실패(None)면 해당 후보는 기존 정적 점수를 그대로 유지(fail-open).

    - scored: 이미 정적 점수로 정렬된 결과 리스트(내림차순).
    - ctx: score_one 재계산에 쓸 컨텍스트(_build_context 산출).
    - verify_top: 실측할 상위 개수.
    - fetcher: 주입식 SERP fetcher(테스트용). 미지정 시 실 통합검색 조회.
    """
    if verify_top <= 0 or not scored:
        return scored
    try:
        from core import serp_competition
    except ImportError:
        return scored  # 모듈 없으면 정적 점수 유지(fail-open)

    reranked: list[dict] = []
    for idx, rec in enumerate(scored):
        if idx >= verify_top:
            reranked.append(rec)
            continue
        try:
            real_win = serp_competition.real_winnability(rec["keyword"], fetcher=fetcher)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            log.debug("[opportunity_score] SERP 검증 실패(%s): %s",
                      rec.get("keyword", "")[:20], exc)
            real_win = None
        if real_win is None:
            reranked.append(rec)          # 실측 불가 → 정적 점수 유지
            continue
        rescored = score_one(
            rec["keyword"], volume=rec.get("volume"), category=rec.get("category", ""),
            cat_counts=ctx["cat_counts"], targeted_keys=ctx["targeted_keys"],
            trend_set=ctx["trend_set"], realdemand_set=ctx["realdemand_set"],
            real_win=real_win, feedback_weights=ctx.get("feedback_weights"),
        )
        reranked.append(rescored)

    reranked.sort(key=lambda r: r["score"], reverse=True)
    return reranked


def rank_publish_queue(*, top_n: int = 30, use_miner: bool = True,
                       verify_serp: bool = False, verify_top: int = 0,
                       fetcher=None) -> list[dict]:
    """발행 큐 생성: 질문형 시드 + 자동완성 발굴 + Phase1 기회를 점수화해 상위 N.

    잠식/무수요는 자동 배제. Phase 2 발행 파이프라인의 입력.

    verify_serp: True 면 상위 후보를 실측 SERP 승산으로 재검증해 못 이길 헤드를
                 후순위로 강등한다(배치 조회 비용 때문에 상위 verify_top 개만).
                 verify_top 이 0 이면 top_n 을 검증 상한으로 쓴다(정렬 뒤 상위만).
                 fail-open: 조회 실패/미설정이면 기존 정적 점수 그대로.
    fetcher:     주입식 SERP fetcher(테스트용). verify_serp=True 일 때만 사용.
    """
    ctx = _build_context()
    candidates: list = []

    try:
        from core import demand_miner as dm
        seeds = dm.load_question_seeds()
        for s in seeds:
            candidates.append({"keyword": s})
        if use_miner:
            candidates.extend(dm.mine_from_opportunities(max_seeds=15, per_seed=8))
    except (ImportError, OSError):
        pass

    scored = score_candidates(candidates, context=ctx)
    if verify_serp:
        limit = verify_top if verify_top > 0 else top_n
        scored = verify_serp_winnability(scored, ctx=ctx, verify_top=limit,
                                         fetcher=fetcher)
    publishable = [r for r in scored if r["verdict"] in ("발행 1순위", "발행 후보")]
    return publishable[:top_n]


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO)
    q = rank_publish_queue(top_n=15)
    print(f"발행 큐 상위 {len(q)}:")
    for r in q:
        b = r["breakdown"]
        print(f"  {r['score']:5.1f} [{r['verdict']}] {r['keyword']}  "
              f"(수요{b['demand']} 승산{b['winnable']} 권위{b['authority']} vol={r['volume']})")
