# core/ab_test.py — 성장 실험 루프 (Phase 3 · L6)
"""추측 대신 실험으로 결정한다 — 제목 유형·발행 시각·썸네일을 A/B 로 학습.

각 실험(experiment)은 여러 변형(arm)을 가지고, 발행된 글에 변형을 배정한 뒤
Phase 1 실측 유입(performance_db)을 보상으로 회수한다. UCB1 밴딧으로 다음에 어떤
변형을 쓸지 결정 — 탐색(안 써본 변형)과 활용(성과 좋은 변형)을 자동 균형.

- UCB1: 무작위 없이 결정론적 → 재현·테스트 가능. 보상은 [0,1] 정규화(주간 유입/REWARD_CAP).
- fail-open: 데이터 없으면 탐색 우선(모든 변형 균등).
- 부수효과: logs/ab_experiments.json 쓰기만.
"""
from __future__ import annotations

import math
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.logger import get_logger

log = get_logger(__name__)

BASE_DIR = Path(__file__).parent.parent
from config import profile as _nbaprofile
_DATA_DIR = _nbaprofile.app_data_root()

DB_PATH = _DATA_DIR / "logs" / "ab_experiments.json"

REWARD_CAP = 100.0        # 주간 유입 100 = 보상 1.0 (정규화 상한)
UCB_C = math.sqrt(2)      # 탐색 계수

# 기본 실험 정의 — 발행 파이프라인이 배정할 수 있는 축
DEFAULT_EXPERIMENTS = {
    "title_style": {
        "description": "제목 유형 (AI요약·클릭률 영향)",
        "metric": "visits_7d",
        "arms": ["question", "info", "number", "howto"],
        "arm_desc": {
            "question": "질문형 — '청년도약계좌 조건 뭔가요?'",
            "info": "정보형 — '청년도약계좌 조건 총정리'",
            "number": "숫자형 — '청년도약계좌 조건 5가지'",
            "howto": "방법형 — '청년도약계좌 신청하는 법'",
        },
    },
    "publish_time": {
        "description": "발행 시각대 (노출·체류 영향)",
        "metric": "visits_7d",
        "arms": ["dawn", "morning", "lunch", "evening", "night"],
        "arm_desc": {"dawn": "06~08", "morning": "09~11", "lunch": "12~14",
                     "evening": "17~19", "night": "20~22"},
    },
    "thumbnail": {
        "description": "썸네일 스타일 (클릭률 영향)",
        "metric": "visits_7d",
        "arms": ["text_heavy", "photo", "illustration", "chart"],
        "arm_desc": {"text_heavy": "텍스트 강조", "photo": "실사진",
                     "illustration": "일러스트", "chart": "표·차트"},
    },
}


def _load() -> dict:
    from config import safe_load_json
    data = safe_load_json(DB_PATH, default=lambda: {"experiments": {}, "updated_at": ""})
    if not isinstance(data, dict):
        data = {"experiments": {}, "updated_at": ""}
    data.setdefault("experiments", {})
    return data


def _save(data: dict) -> None:
    from config import atomic_write_json
    data["updated_at"] = datetime.now().isoformat(timespec="seconds")
    atomic_write_json(DB_PATH, data)


def _empty_arm() -> dict:
    return {"n": 0, "total_reward": 0.0, "posts": {}}   # posts: {post_key: reward}


def ensure_defaults(data: dict | None = None) -> dict:
    """기본 실험이 없으면 시드. 기존 데이터는 보존."""
    own = data is None
    if own:
        data = _load()
    for exp, spec in DEFAULT_EXPERIMENTS.items():
        e = data["experiments"].setdefault(exp, {
            "description": spec["description"], "metric": spec["metric"],
            "arm_desc": spec.get("arm_desc", {}), "arms": {},
        })
        for arm in spec["arms"]:
            e["arms"].setdefault(arm, _empty_arm())
    if own:
        _save(data)
    return data


def _mean(arm: dict) -> float:
    return (arm["total_reward"] / arm["n"]) if arm["n"] else 0.0


def pick_variant(experiment: str, *, data: dict | None = None) -> str:
    """UCB1 로 다음 사용할 변형 선택. 안 써본 변형 우선(탐색), 그다음 상한신뢰 최대."""
    data = data or ensure_defaults()
    exp = data["experiments"].get(experiment)
    if not exp or not exp["arms"]:
        return ""
    arms = exp["arms"]
    total_n = sum(a["n"] for a in arms.values())
    # 미탐색 변형이 있으면 그중 사전순 첫 번째(결정론)
    unexplored = sorted([name for name, a in arms.items() if a["n"] == 0])
    if unexplored:
        return unexplored[0]
    best, best_score = "", -1.0
    for name in sorted(arms):
        a = arms[name]
        ucb = _mean(a) + UCB_C * math.sqrt(math.log(total_n) / a["n"])
        if ucb > best_score:
            best, best_score = name, ucb
    return best


def assign(experiment: str, arm: str, post_key: str, *, data: dict | None = None) -> None:
    """발행 글에 변형 배정(보상은 나중에 sync). 같은 글 재배정은 갱신."""
    own = data is None
    if own:
        data = ensure_defaults()
    exp = data["experiments"].get(experiment)
    if exp and arm in exp["arms"] and post_key:
        # None = 배정됐으나 보상 미기록(=미관측). 보상 회수 시 n 증가 대상.
        exp["arms"][arm]["posts"].setdefault(post_key, None)
    if own:
        _save(data)


def _normalize(reward: float) -> float:
    return max(0.0, min(1.0, reward / REWARD_CAP))


def record_reward(experiment: str, arm: str, post_key: str, reward: float,
                  *, data: dict | None = None) -> None:
    """배정된 글의 실측 보상(주간 유입 등) 기록. 정규화 후 누적(재기록은 갱신)."""
    own = data is None
    if own:
        data = ensure_defaults()
    exp = data["experiments"].get(experiment)
    if exp and arm in exp["arms"] and post_key:
        a = exp["arms"][arm]
        norm = _normalize(reward)
        prev = a["posts"].get(post_key)
        if prev is None:
            a["n"] += 1
            a["total_reward"] += norm
        else:
            a["total_reward"] += (norm - prev)   # 갱신 차분
        a["posts"][post_key] = norm
    if own:
        _save(data)


def sync_rewards_from_performance(*, data: dict | None = None) -> int:
    """배정된 글들의 주간 유입을 performance_db 에서 회수해 보상 갱신. 반환: 갱신 건수."""
    own = data is None
    if own:
        data = ensure_defaults()
    try:
        from core import performance_db as pdb
        pd = pdb.load()
    except (ImportError, OSError):
        return 0
    posts = pd.get("posts", {})
    updated = 0
    for exp in data["experiments"].values():
        for arm_name, a in exp["arms"].items():
            for post_key in list(a["posts"].keys()):
                rec = posts.get(post_key)
                if not rec:
                    continue
                visits = pdb._recent_views(rec, 7)
                norm = _normalize(visits)
                prev = a["posts"].get(post_key)
                if prev is None or abs(prev - norm) > 1e-9:
                    if prev is None:
                        a["n"] += 1
                        a["total_reward"] += norm
                    else:
                        a["total_reward"] += (norm - prev)
                    a["posts"][post_key] = norm
                    updated += 1
    if own:
        _save(data)
    log.info(f"[ab_test] 성과 동기화 — {updated}건 갱신")
    return updated


def leaderboard(experiment: str, *, data: dict | None = None) -> list[dict]:
    """변형별 평균 보상·표본수·UCB 상한. 평균 내림차순."""
    data = data or ensure_defaults()
    exp = data["experiments"].get(experiment)
    if not exp:
        return []
    total_n = sum(a["n"] for a in exp["arms"].values())
    rows = []
    for name, a in exp["arms"].items():
        mean = _mean(a)
        ucb = mean + (UCB_C * math.sqrt(math.log(total_n) / a["n"]) if a["n"] and total_n else 0.0)
        rows.append({"arm": name, "desc": exp.get("arm_desc", {}).get(name, ""),
                     "n": a["n"], "mean_reward": round(mean, 3),
                     "ucb": round(ucb, 3),
                     "avg_visits": round(mean * REWARD_CAP, 1)})
    rows.sort(key=lambda r: (r["mean_reward"], r["n"]), reverse=True)
    return rows


def recommend(*, data: dict | None = None) -> dict:
    """실험별 (다음 시도 변형, 현재 최고 변형, 신뢰도) 요약."""
    data = data or ensure_defaults()
    out = {}
    for exp_name, exp in data["experiments"].items():
        board = leaderboard(exp_name, data=data)
        total_n = sum(a["n"] for a in exp["arms"].values())
        best = board[0] if board and board[0]["n"] > 0 else None
        out[exp_name] = {
            "description": exp.get("description", ""),
            "next_try": pick_variant(exp_name, data=data),
            "current_best": best["arm"] if best else None,
            "best_avg_visits": best["avg_visits"] if best else 0,
            "samples": total_n,
            "confidence": "충분" if total_n >= 30 else ("탐색중" if total_n >= 8 else "데이터부족"),
            "leaderboard": board,
        }
    return out


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO)
    ensure_defaults()
    sync_rewards_from_performance()
    for name, r in recommend().items():
        print(f"[{name}] {r['description']} · 표본 {r['samples']}({r['confidence']}) · "
              f"다음 시도: {r['next_try']} · 현재 최고: {r['current_best']}")
