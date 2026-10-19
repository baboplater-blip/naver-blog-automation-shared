# core/topic_cluster.py — 필러-클러스터 구조 + 내부링크 그래프 (Phase 2 · L3)
"""평면·파편화된 232편을 토픽 권위(필러-클러스터) 구조로 재편한다.

네이버 C-Rank 는 토픽 집중(한 주제에 깊고 연결된 글 묶음)을 보상한다. 이 모듈은
발행 카탈로그를 주제별 클러스터로 군집하고, 각 글이 어느 필러 아래 어떤 형제 글과
연결돼야 하는지(내부링크 그래프)를 산출한다.

- 필러: 클러스터에서 가장 광역(고검색량 or 최단 키워드) 글 = 허브.
- 클러스터 멤버: 같은 토픽 토큰을 공유하는 글들.
- 고아(orphan): 어느 클러스터에도 못 붙는 글 = 무수요 파편 후보(비공개/통합 검토).

- 순수 함수 중심(posts 주입 가능) → 오프라인 테스트. 부수효과 없음(리포트는 별도).
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.logger import get_logger

log = get_logger(__name__)
BASE_DIR = Path(__file__).parent.parent
from config import profile as _nbaprofile
_DATA_DIR = _nbaprofile.app_data_root()


# 토픽 앵커가 아닌 일반 동작/수식 토큰 — 클러스터 키에서 제외(안 그러면 '신청'이 전부 연결)
_STOP_TOKENS = {
    "신청", "방법", "절차", "발급", "설정", "등록", "가입", "확인", "조회", "정리",
    "총정리", "완벽", "가이드", "안내", "하는법", "하는", "받는", "쓰는", "사용",
    "차이", "비교", "장단점", "종류", "대상", "자격", "조건", "기준", "서류",
    "얼마", "금액", "한도", "기간", "언제", "경우", "이상", "이하", "관련",
    "단계", "가지", "무엇", "어떻게", "위한", "그리고", "또는", "바로", "지금",
    "2024", "2025", "2026", "vs",
}
_JOSA = ("으로", "로", "은", "는", "이", "가", "을", "를", "와", "과", "의", "에",
         "도", "만", "부터", "까지", "에서", "에게", "한테")


def _strip_josa(tok: str) -> str:
    for j in sorted(_JOSA, key=len, reverse=True):
        if tok.endswith(j) and len(tok) > len(j) + 1:
            return tok[:-len(j)]
    return tok


def _tokenize(text: str) -> set:
    """제목/키워드 → 토픽 토큰 집합(2+ char, josa 제거, 불용 동작어 제외)."""
    if not text:
        return set()
    text = re.sub(r"^\d{4}-\d{2}-\d{2}\s*[—\-]\s*", "", str(text))
    raw = re.split(r"[\s·,./|~\-()\[\]{}'\"!?:;]+", text)
    out = set()
    for t in raw:
        t = re.sub(r"[^0-9A-Za-z가-힣]", "", t).strip()
        if len(t) < 2:
            continue
        t = _strip_josa(t)
        if len(t) < 2 or t in _STOP_TOKENS:
            continue
        out.add(t)
    return out


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / len(a | b) if inter else 0.0


def _shared_anchor(a: set, b: set) -> bool:
    """두 글이 같은 토픽 앵커(3+char 공통 토큰)를 공유하는가."""
    for t in (a & b):
        if len(t) >= 3:
            return True
    return False


def build_clusters(posts: list, *, sim_threshold: float = 0.34) -> dict:
    """posts([{key,title,keyword,category,url,volume}]) → 클러스터·고아 구조.

    같은 카테고리 안에서 토픽 앵커/유사도로 연결(union-find 연결요소).
    """
    n = len(posts)
    tokens = [_tokenize(p.get("keyword") or p.get("title") or "") for p in posts]
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[rx] = ry

    for i in range(n):
        for j in range(i + 1, n):
            if posts[i].get("category") and posts[i].get("category") != posts[j].get("category"):
                continue
            if _shared_anchor(tokens[i], tokens[j]) or _jaccard(tokens[i], tokens[j]) >= sim_threshold:
                union(i, j)

    groups: dict[int, list] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)

    clusters, orphans = [], []
    for members in groups.values():
        if len(members) <= 1:
            orphans.append(posts[members[0]])
            continue
        # 필러 = 최고 볼륨, 동률이면 최단 키워드(광역)
        def _pillar_key(idx):
            p = posts[idx]
            return (-(p.get("volume") or 0), len(p.get("keyword") or p.get("title") or ""))
        members_sorted = sorted(members, key=_pillar_key)
        pillar_idx = members_sorted[0]
        # 앵커 = 멤버 전체에서 가장 자주 등장하는 토픽 토큰(최빈). 대형 클러스터에 강건.
        from collections import Counter
        freq: Counter = Counter()
        for m in members:
            for t in tokens[m]:
                if len(t) >= 2:
                    freq[t] += 1
        # 동률이면 더 긴(구체적) 토큰 우선
        anchor = ""
        if freq:
            top = freq.most_common()
            best = max(top, key=lambda kv: (kv[1], len(kv[0])))
            anchor = best[0]
        if not anchor:
            anchor = max(tokens[pillar_idx], key=len, default=posts[pillar_idx].get("category", ""))
        clusters.append({
            "anchor": anchor,
            "category": posts[pillar_idx].get("category", ""),
            "pillar": posts[pillar_idx],
            "members": [posts[m] for m in members_sorted],
            "member_idx": members_sorted,
            "size": len(members),
            "volume": posts[pillar_idx].get("volume") or 0,
        })
    clusters.sort(key=lambda c: (c["size"], c["volume"]), reverse=True)
    return {"clusters": clusters, "orphans": orphans, "tokens": tokens}


def build_link_graph(cluster_result: dict, posts: list, *, k: int = 4) -> dict:
    """각 글 → 추천 내부링크(필러 + 유사 형제 상위 k). {key: [{key,title,url,role}]}."""
    tokens = cluster_result["tokens"]
    idx_by_key = {p.get("key"): i for i, p in enumerate(posts)}
    graph: dict[str, list] = {}
    for cl in cluster_result["clusters"]:
        pillar = cl["pillar"]
        p_key = pillar.get("key")
        member_idxs = cl["member_idx"]
        for mi in member_idxs:
            post = posts[mi]
            key = post.get("key")
            links = []
            if key != p_key:   # 멤버 → 필러
                links.append({"key": p_key, "title": pillar.get("title"),
                              "url": pillar.get("url", ""), "role": "pillar"})
            # 형제 유사도 상위 k
            sibs = []
            for mj in member_idxs:
                if mj == mi or posts[mj].get("key") == p_key:
                    continue
                sibs.append((_jaccard(tokens[mi], tokens[mj]), mj))
            sibs.sort(reverse=True)
            for _s, mj in sibs[:k]:
                sp = posts[mj]
                links.append({"key": sp.get("key"), "title": sp.get("title"),
                              "url": sp.get("url", ""), "role": "sibling"})
            if key == p_key:   # 필러 → 대표 자식 상위 k
                children = [(_jaccard(tokens[mi], tokens[mj]), mj) for mj in member_idxs if mj != mi]
                children.sort(reverse=True)
                links = [{"key": posts[mj].get("key"), "title": posts[mj].get("title"),
                          "url": posts[mj].get("url", ""), "role": "child"}
                         for _s, mj in children[:k]]
            graph[key] = links
    return graph


def _load_catalog() -> list:
    """performance_db 카탈로그 → posts 리스트(볼륨 채움)."""
    from core import performance_db as pdb
    try:
        from core.search_volume import best_volume
    except (ImportError, AttributeError):
        best_volume = lambda x: 0  # noqa: E731
    data = pdb.load()
    posts = []
    for key, rec in data.get("posts", {}).items():
        kw = rec.get("keyword") or rec.get("clean_title") or rec.get("title") or ""
        try:
            vol = int(best_volume(kw) or 0)
        except (TypeError, ValueError):
            vol = 0
        posts.append({
            "key": key, "title": rec.get("clean_title") or rec.get("title") or "",
            "keyword": kw, "category": rec.get("category") or "",
            "url": rec.get("url", ""), "volume": vol,
        })
    return posts


def run_topic_map(*, sim_threshold: float = 0.34, k: int = 4) -> dict:
    """카탈로그 로드 → 클러스터 + 링크 그래프 산출."""
    posts = _load_catalog()
    if not posts:
        return {"posts": 0, "clusters": [], "orphans": [], "graph": {}}
    cr = build_clusters(posts, sim_threshold=sim_threshold)
    graph = build_link_graph(cr, posts, k=k)
    return {"posts": len(posts), "clusters": cr["clusters"],
            "orphans": cr["orphans"], "graph": graph}


CACHE_PATH = _DATA_DIR / "logs" / "internal_link_map.json"


def build_and_cache(*, k: int = 4) -> Path:
    """토픽맵을 계산해 내부링크 캐시(logs/internal_link_map.json)로 저장.

    매 글 생성마다 O(n²) 군집을 재계산하지 않도록 캐시. content_generator/refresh 가
    이 캐시를 읽어 필러-클러스터 내부링크를 삽입한다. 리프레시/토픽리포트가 갱신.
    """
    from config import atomic_write_json
    from datetime import datetime

    posts = _load_catalog()
    cr = build_clusters(posts)
    graph = build_link_graph(cr, posts, k=k)
    tokens = cr["tokens"]
    idx = {p["key"]: i for i, p in enumerate(posts)}
    clusters_lite = []
    for c in cr["clusters"]:
        p = c["pillar"]
        match_tokens = set()
        for m in c["member_idx"]:
            match_tokens |= tokens[m]
        clusters_lite.append({
            "anchor": c["anchor"], "category": c["category"],
            "pillar": {"key": p["key"], "title": p["title"], "url": p.get("url", "")},
            "members": [{"key": posts[i]["key"], "title": posts[i]["title"],
                         "url": posts[i].get("url", "")} for i in c["member_idx"]],
            "match_tokens": sorted(match_tokens),
        })
    atomic_write_json(CACHE_PATH, {
        "graph": graph, "clusters": clusters_lite,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    })
    log.info(f"[topic_cluster] 내부링크 캐시 저장 — 클러스터 {len(clusters_lite)}")
    return CACHE_PATH


def _load_cache() -> dict | None:
    from config import safe_load_json
    data = safe_load_json(CACHE_PATH, default=lambda: None)
    return data if isinstance(data, dict) and data.get("graph") is not None else None


def links_for(title_or_key: str, *, k: int = 4, use_cache: bool = True) -> list:
    """이미 발행된 글의 추천 내부링크 조회(캐시 우선)."""
    from core.performance_db import normalize_key
    cache = _load_cache() if use_cache else None
    graph = cache["graph"] if cache else run_topic_map(k=k)["graph"]
    key = normalize_key(title_or_key)
    if key in graph:
        return graph[key]
    for gk, links in graph.items():
        if key and (key in gk or gk in key):
            return links
    return []


def links_for_new(title: str, category: str = "", *, k: int = 4) -> list:
    """아직 발행 안 된 신규 글이 소속될 클러스터를 찾아 필러+형제 링크 반환.

    신규 글 제목 토큰을 각 클러스터의 토큰과 대조해 최적 클러스터 선택 →
    그 필러(허브)로 먼저 링크(C-Rank 권위 집중) + 대표 형제.
    """
    cache = _load_cache()
    clusters = cache["clusters"] if cache else \
        [{"anchor": c["anchor"], "category": c["category"],
          "pillar": {"key": c["pillar"]["key"], "title": c["pillar"]["title"],
                     "url": c["pillar"].get("url", "")},
          "members": [{"key": m["key"], "title": m["title"], "url": m.get("url", "")}
                      for m in c["members"]],
          "match_tokens": sorted(set().union(*[_tokenize(m["title"]) for m in c["members"]]) if c["members"] else set())}
         for c in run_topic_map(k=k)["clusters"]]
    if not clusters:
        return []
    new_tokens = _tokenize(title)
    if not new_tokens:
        return []

    best, best_score = None, 0.0
    for c in clusters:
        if category and c["category"] and c["category"] != category:
            continue
        ctoks = set(c["match_tokens"])
        score = _jaccard(new_tokens, ctoks)
        if c["anchor"] and c["anchor"] in title.replace(" ", ""):
            score += 0.5   # 앵커 포함 = 강한 소속 신호
        if score > best_score:
            best, best_score = c, score
    if not best or best_score < 0.12:
        return []

    from core.performance_db import normalize_key
    self_key = normalize_key(title)
    links = []
    pil = best["pillar"]
    if pil.get("url") and normalize_key(pil["title"]) != self_key:
        links.append({"key": pil["key"], "title": pil["title"],
                      "url": pil["url"], "role": "pillar"})
    for m in best["members"]:
        if len(links) >= k + 1:
            break
        if m.get("url") and normalize_key(m["title"]) != self_key \
                and m["key"] != pil.get("key"):
            links.append({"key": m["key"], "title": m["title"],
                          "url": m["url"], "role": "sibling"})
    return links


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO)
    tm = run_topic_map()
    print(f"카탈로그 {tm['posts']}편 → 클러스터 {len(tm['clusters'])} · 고아 {len(tm['orphans'])}")
    for cl in tm["clusters"][:12]:
        print(f"  [{cl['category']}] 필러:{(cl['pillar']['title'] or '')[:22]} "
              f"· 앵커={cl['anchor']} · {cl['size']}편")
