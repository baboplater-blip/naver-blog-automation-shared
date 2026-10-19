# core/sibling_merge.py — 사촌 글 자동 병합 실행 (개선안 #5)
"""같은 제도에 몰린 '사촌 글'을 필러 1편으로 통합하는 오케스트레이션.

배경(개선안 #3에서 이어짐):
    institution_dedup 이 제도-단위 중복을 '판정'하고 병합 '브리프'를 만드는 데까지
    갔다. 하지만 실제 통합은 실행되지 않았다 — 에너지바우처 ~7편, 긴급생계지원 ~6편이
    여전히 서로의 검색 유입을 갉아먹는(자기잠식) 상태로 흩어져 있다.

이 모듈이 하는 일(순수/결정적 우선 · 폐기·삭제·동결 없음):
    1. detect_sibling_groups(): 발행/초안 카탈로그를 제도명으로 군집 → 임계 초과 그룹만.
    2. select_pillar(): 각 그룹에서 '최강 글'을 필러(허브)로 선정.
       (우선순위: 실측 views → best rank → demand 프록시 → 링크 축적=오래됨)
    3. build_merge_plan(): 사촌들을 필러로 내부링크 연결(리다이렉트 성격) + 각 사촌에
       '필러와 겹치지 않게 차별화' 리프레시 브리프(institution_dedup.build_merge_brief).
    4. integrate_links(): topic_cluster 내부링크 캐시를 갱신하고, 사촌→필러 링크
       오버레이(logs/sibling_merge_map.json)를 배선한다. 라이브 반영은 url 보정·리프레시 시.
    5. run_sibling_merge_report(): reports/{date}_sibling_merge.md 산출.

**병합 ≠ 폐기의 보장(설계 불변식):**
    - 어떤 사촌도 삭제·비공개·동결하지 않는다(draft_triage.apply_freeze 미사용).
    - 각 사촌은 살아남아 '필러를 가리키는 내부링크 + 차별화 각도'로 재생된다.
      리다이렉트는 물리 삭제가 아니라 내부링크로 권위를 필러에 몰아주는 '성격'일 뿐.
    - 실제 재작성·재발행은 사용자 트리거. 이 모듈은 계획·링크맵·리포트까지만 산출한다.
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.logger import get_logger  # noqa: E402
from config import profile as _nbaprofile  # noqa: E402

log = get_logger(__name__)

BASE_DIR = Path(__file__).parent.parent
_DATA_DIR = _nbaprofile.app_data_root()

# 사촌→필러 내부링크 오버레이 캐시. topic_cluster 의 클러스터 그래프와 별개로,
# '같은 제도 사촌' 관계만 명시적으로 담는다(리다이렉트 성격의 링크맵).
LINK_MAP_PATH = _DATA_DIR / "logs" / "sibling_merge_map.json"

# 프로필별 reports/ (topic_report 와 동일 규약, 2026-07-19 격리 감사).
try:
    HARNESS_REPORTS = _nbaprofile.reports_dir()
except Exception:  # noqa: BLE001
    HARNESS_REPORTS = BASE_DIR.parent / "reports"

# ─────────────────────────────────────────────────────────────────────────────
# 설정 — 임계는 institution_dedup 의 MAX_PER_WINDOW 를 상속해 정합성을 맞춘다.
# 같은 제도 글이 '허용 편수(+1)'를 넘으면(즉 차단이 걸리는 규모) 병합 후보로 본다.
# ─────────────────────────────────────────────────────────────────────────────
def _min_group_size() -> int:
    """병합 후보가 되는 최소 사촌 편수(초과가 아니라 '이상')."""
    try:
        from config import settings as _s
        explicit = getattr(_s, "SIBLING_MERGE_MIN_GROUP", None)
        if explicit is not None:
            return max(2, int(explicit))
    except (ImportError, AttributeError, ValueError):
        pass
    # institution_dedup 의 허용 편수 초과 지점 = 허용 + 1.
    try:
        from core import institution_dedup as idd
        return max(2, int(idd._MAX_PER_WINDOW) + 1)
    except (ImportError, AttributeError, ValueError):
        return 4


# ─────────────────────────────────────────────────────────────────────────────
# 카탈로그 로드 — performance_db 를 진실 원본으로(views·rank 신호가 여기 있다).
# 순수 테스트를 위해 posts 를 주입할 수 있게 모든 공개 함수가 records/posts 인자를 받는다.
# ─────────────────────────────────────────────────────────────────────────────
def _load_catalog() -> List[dict]:
    """performance_db 의 글 카탈로그 → 정규화 레코드 리스트(실패 시 빈 리스트).

    각 레코드에 필러 선정용 신호(views7·best_rank·posted_at)를 미리 계산해 넣는다.
    """
    try:
        from core import performance_db as pdb
    except ImportError as exc:
        log.warning(f"[sibling_merge] performance_db 로드 실패 — 빈 카탈로그: {exc}")
        return []
    try:
        data = pdb.load()
    except (OSError, ValueError) as exc:
        log.warning(f"[sibling_merge] performance_db.load 실패: {exc}")
        return []

    posts: List[dict] = []
    for key, rec in (data.get("posts") or {}).items():
        if not isinstance(rec, dict):
            continue
        title = rec.get("clean_title") or rec.get("title") or ""
        if not title:
            continue
        best_rank, _ = _safe_best_rank(pdb, rec)
        posts.append({
            "key": key,
            "title": title,
            "url": rec.get("url", "") or "",
            "category": rec.get("category", "") or "",
            "posted_at": rec.get("posted_at", "") or "",
            "views7": _safe_recent_views(pdb, rec),
            "best_rank": best_rank,
        })
    return posts


def _safe_recent_views(pdb, rec: dict) -> int:
    """performance_db._recent_views 를 안전 호출(실패 시 0)."""
    try:
        return int(pdb._recent_views(rec, 7))
    except (AttributeError, TypeError, ValueError):
        return 0


def _safe_best_rank(pdb, rec: dict) -> Tuple[Optional[int], str]:
    """performance_db._best_rank 를 안전 호출(실패 시 (None, ''))."""
    try:
        return pdb._best_rank(rec)
    except (AttributeError, TypeError, ValueError):
        return None, ""


# ─────────────────────────────────────────────────────────────────────────────
# 1) 사촌 그룹 탐지 — 제도명으로 군집, 임계 이상 그룹만 후보.
# ─────────────────────────────────────────────────────────────────────────────
def detect_sibling_groups(
    posts: Optional[List[dict]] = None,
    *,
    min_group_size: Optional[int] = None,
) -> List[dict]:
    """카탈로그를 제도명으로 군집 → 임계 이상인 그룹만 병합 후보로 반환.

    Args:
        posts:          글 레코드 리스트. 생략 시 performance_db 카탈로그 로드.
                        각 레코드는 최소 {"title"} 을 가져야 하며 url·views7·
                        best_rank·posted_at·category 는 선택(없으면 프록시로 보정).
        min_group_size: 후보가 되는 최소 편수. 생략 시 설정/기본값.

    Returns:
        [{"institution": str, "size": int, "members": [rec, ...]}, ...]
        institution 정규화 실패 레코드는 어느 그룹에도 넣지 않는다(오탐 방지).
        size 내림차순 정렬(자기잠식 큰 것부터).
    """
    from core.institution_dedup import normalize_institution

    if posts is None:
        posts = _load_catalog()
    threshold = min_group_size if min_group_size is not None else _min_group_size()

    grouped: Dict[str, List[dict]] = {}
    for rec in posts:
        if not isinstance(rec, dict):
            continue
        title = rec.get("clean_title") or rec.get("title") or ""
        institution = normalize_institution(title)
        if not institution:
            continue
        grouped.setdefault(institution, []).append(rec)

    groups: List[dict] = []
    for institution, members in grouped.items():
        if len(members) < threshold:
            continue
        groups.append({
            "institution": institution,
            "size": len(members),
            "members": members,
        })
    # 큰 그룹(자기잠식 심한 제도)부터. 동률이면 제도명 사전순(결정성).
    groups.sort(key=lambda g: (-g["size"], g["institution"]))
    return groups


# ─────────────────────────────────────────────────────────────────────────────
# 2) 필러 선정 — '최강 글' 우선순위.
# ─────────────────────────────────────────────────────────────────────────────
def _parse_posted(value: str) -> datetime:
    """posted_at 파싱(실패 시 datetime.max — '가장 최신'으로 취급해 필러에서 밀려남).

    필러는 '오래돼 링크가 축적된' 글을 선호하므로, 시각 미상 글은 최신처럼 다뤄
    동률 최종 타이브레이크에서 후순위가 되게 한다.
    """
    from core.institution_dedup import _parse_posted_at
    parsed = _parse_posted_at(value or "")
    return parsed if parsed is not None else datetime.max


def _pillar_sort_key(rec: dict) -> tuple:
    """필러 선정 정렬키(작을수록 강함).

    우선순위(모두 결정적):
      1. 실측 views7 (많을수록 강함)        → -views7
      2. 검색 best_rank (숫자 작을수록 강함) → rank(없으면 큰 값)
      3. demand 프록시 (제목 answer-fit)    → -demand
      4. 링크 축적 = 오래된 글              → posted_at 오름차순
      5. 결정적 최종 타이브레이크           → 제목(url 아님, url 은 없을 수 있음)
    """
    views7 = int(rec.get("views7") or 0)
    best_rank = rec.get("best_rank")
    rank_key = best_rank if isinstance(best_rank, int) and best_rank > 0 else 10 ** 6
    demand = _demand_proxy(rec.get("clean_title") or rec.get("title") or "")
    posted = _parse_posted(rec.get("posted_at", ""))
    title = rec.get("clean_title") or rec.get("title") or ""
    return (-views7, rank_key, -demand, posted, title)


def _demand_proxy(title: str) -> int:
    """신호 없을 때의 결정적 수요 프록시(draft_triage._demand_score 재사용)."""
    try:
        from core.draft_triage import _demand_score
        return int(_demand_score(title))
    except (ImportError, AttributeError, TypeError, ValueError):
        return 0


def select_pillar(members: List[dict]) -> Optional[dict]:
    """사촌 그룹에서 필러(허브)로 삼을 '최강 글' 1편을 결정적으로 고른다.

    실측 신호(views→rank)가 있으면 그걸 우선하고, 없으면 결정적 프록시
    (수요 적합 제목 → 가장 오래돼 링크가 축적된 글)로 선정한다.

    Returns:
        필러 레코드(dict) 또는 members 가 비면 None.
    """
    if not members:
        return None
    return sorted(members, key=_pillar_sort_key)[0]


# ─────────────────────────────────────────────────────────────────────────────
# 3) 병합 계획 — 필러 + 사촌별 리다이렉트 링크 + 차별화 리프레시 브리프.
# ─────────────────────────────────────────────────────────────────────────────
def build_merge_plan(
    posts: Optional[List[dict]] = None,
    *,
    min_group_size: Optional[int] = None,
) -> List[dict]:
    """제도별 병합 계획을 생성한다(폐기 없음 — 통합·차별화·링크 배선만).

    Returns:
        [{
          "institution": str,
          "size": int,
          "pillar": rec,                 # 허브로 승격할 최강 글
          "pillar_reason": str,          # 선정 근거 태그(신호 기반/프록시)
          "siblings": [                  # 필러를 가리킬 나머지 글
             {"post": rec,
              "link_to_pillar": {"key","title","url","role":"pillar_redirect"},
              "differentiation": str},   # 필러와 겹치지 않게 각도 좁히기(리프레시 힌트)
             ...
          ],
          "merge_brief": dict | None,    # institution_dedup.build_merge_brief 결과
        }, ...]
    """
    groups = detect_sibling_groups(posts, min_group_size=min_group_size)
    plans: List[dict] = []
    for group in groups:
        members = group["members"]
        pillar = select_pillar(members)
        if pillar is None:
            continue
        pillar_key = pillar.get("key")
        pillar_ref = {
            "key": pillar_key,
            "title": pillar.get("clean_title") or pillar.get("title") or "",
            "url": pillar.get("url", "") or "",
            "role": "pillar_redirect",
        }
        siblings: List[dict] = []
        for rec in members:
            if rec.get("key") == pillar_key and pillar_key:
                continue
            # key 가 비어 동일성 판정이 안 되면 제목으로 필러 자신 제외.
            if not pillar_key and (rec.get("title") == pillar.get("title")):
                continue
            siblings.append({
                "post": rec,
                "link_to_pillar": dict(pillar_ref),
                "differentiation": _differentiation_hint(
                    group["institution"], rec, pillar,
                ),
            })
        plans.append({
            "institution": group["institution"],
            "size": group["size"],
            "pillar": pillar,
            "pillar_reason": _pillar_reason(pillar),
            "siblings": siblings,
            "merge_brief": _safe_merge_brief(pillar, members),
        })
    return plans


def _pillar_reason(pillar: dict) -> str:
    """필러 선정 근거를 사람이 읽을 태그로(리포트·로그용)."""
    if int(pillar.get("views7") or 0) > 0:
        return f"실측유입 {int(pillar['views7'])}회/7일"
    br = pillar.get("best_rank")
    if isinstance(br, int) and br > 0:
        return f"검색순위 {br}위"
    demand = _demand_proxy(pillar.get("clean_title") or pillar.get("title") or "")
    if demand > 0:
        return f"수요적합 프록시(점수 {demand})"
    return "링크축적 프록시(최고참 글)"


def _differentiation_hint(institution: str, sibling: dict, pillar: dict) -> str:
    """사촌이 필러와 겹치지 않게 좁힐 '차별화 각도' 힌트(리프레시 브리프용).

    사촌 제목에서 제도명을 뺀 잔여 각도(예: '난방비', '동절기')를 살려,
    필러가 포괄 허브라면 사촌은 그 세부 각도의 심화 글로 재정의하도록 유도한다.
    폐기가 아니라 '역할 재배치'다.
    """
    from core.institution_dedup import _NON_WORD_RE
    title = sibling.get("clean_title") or sibling.get("title") or ""
    residual = _NON_WORD_RE.sub(" ", title).strip()
    # 제도명 조각을 잔여에서 제거해 '고유 각도'만 남긴다.
    for chunk in (institution, *institution.split()):
        residual = residual.replace(chunk, " ")
    angle = " ".join(w for w in residual.split() if len(w) >= 2)[:60].strip()
    if angle:
        return (
            f"'{institution}' 총론은 필러로 통합. 이 글은 '{angle}' 세부 각도만 "
            f"심화(필러와 중복 문단 제거·필러로 링크). 폐기 아님·역할 재배치."
        )
    return (
        f"'{institution}' 총론은 필러로 통합. 이 글은 필러가 안 다루는 하위 사례/"
        f"질문 하나로 좁혀 심화(필러로 링크). 폐기 아님·역할 재배치."
    )


def _safe_merge_brief(pillar: dict, members: List[dict]) -> Optional[dict]:
    """institution_dedup.build_merge_brief 를 카탈로그 레코드로 안전 호출.

    build_merge_brief 는 post_history 형식(clean_title/title/posted_at/url)을 기대하므로
    카탈로그 레코드를 그 형식으로 얇게 변환해 넘긴다. 실패는 fail-open(None).
    """
    try:
        from core import institution_dedup as idd
        history_like = [{
            "clean_title": r.get("clean_title") or r.get("title") or "",
            "title": r.get("title") or "",
            "url": r.get("url", "") or "",
            "posted_at": r.get("posted_at", "") or "",
        } for r in members]
        pillar_title = pillar.get("clean_title") or pillar.get("title") or ""
        return idd.build_merge_brief(
            pillar_title, records=history_like,
            category=pillar.get("category", "") or "",
        )
    except (ImportError, AttributeError, OSError, ValueError, KeyError) as exc:
        log.debug(f"[sibling_merge] merge_brief 생략: {exc}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# 4) 내부링크 통합 실행 — 사촌→필러 오버레이 캐시 + topic_cluster 갱신.
# ─────────────────────────────────────────────────────────────────────────────
def build_link_overlay(plans: List[dict]) -> dict:
    """병합 계획 → 사촌 key 별 '필러로 향하는 링크' 오버레이 맵.

    반환 구조(topic_cluster graph 와 병용 가능한 형태):
    {
      "<sibling_key>": [{"key","title","url","role":"pillar_redirect"}],
      ...
      "_pillars": {"<institution>": {"key","title","url"}},
    }
    key 가 없는 레코드는 오버레이에서 제외(안정 키 없이는 배선 불가).
    """
    overlay: Dict[str, list] = {}
    pillars: Dict[str, dict] = {}
    for plan in plans:
        pillar = plan["pillar"]
        pillars[plan["institution"]] = {
            "key": pillar.get("key", ""),
            "title": pillar.get("clean_title") or pillar.get("title") or "",
            "url": pillar.get("url", "") or "",
        }
        for sib in plan["siblings"]:
            skey = sib["post"].get("key")
            if not skey:
                continue
            overlay[skey] = [dict(sib["link_to_pillar"])]
    overlay["_pillars"] = pillars
    return overlay


def integrate_links(
    plans: List[dict],
    *,
    write: bool = True,
    refresh_topic_cache: bool = True,
) -> dict:
    """사촌→필러 링크 오버레이를 파일로 배선하고 topic_cluster 캐시를 갱신한다.

    Args:
        plans:                병합 계획(build_merge_plan 결과).
        write:                False 면 파일 미기록(dry-run — 오버레이만 계산해 반환).
        refresh_topic_cache:  topic_cluster.build_and_cache 재계산 여부(url 반영 후).

    Returns:
        {"overlay": dict, "written": bool, "path": str,
         "topic_cache": "ok"|"skipped"|"failed", "siblings_linked": int}

    부수효과: 링크맵 파일 쓰기 + (선택)topic_cluster 캐시 파일 쓰기뿐.
    발행·재작성·삭제 없음. 라이브 글 반영은 url 보정·리프레시 시점에 일어난다.
    """
    overlay = build_link_overlay(plans)
    siblings_linked = sum(1 for k in overlay if k != "_pillars")

    written = False
    if write:
        try:
            from config import atomic_write_json
            LINK_MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(LINK_MAP_PATH, {
                "overlay": overlay,
                "siblings_linked": siblings_linked,
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            })
            written = True
            log.info(f"[sibling_merge] 링크 오버레이 저장 — 사촌 {siblings_linked}편 → 필러")
        except (OSError, ValueError, TypeError) as exc:
            log.warning(f"[sibling_merge] 링크 오버레이 저장 실패: {exc}")

    topic_cache = "skipped"
    if refresh_topic_cache:
        try:
            from core import topic_cluster as tc
            tc.build_and_cache()
            topic_cache = "ok"
        except (ImportError, OSError, ValueError, KeyError) as exc:
            log.debug(f"[sibling_merge] topic_cluster 캐시 갱신 생략: {exc}")
            topic_cache = "failed"

    return {
        "overlay": overlay,
        "written": written,
        "path": str(LINK_MAP_PATH),
        "topic_cache": topic_cache,
        "siblings_linked": siblings_linked,
    }


def load_link_overlay() -> dict:
    """저장된 사촌→필러 링크 오버레이를 로드(없으면 빈 dict).

    생성/리프레시 경로가 topic_cluster 링크에 더해 이 오버레이를 병합하면
    사촌→필러 리다이렉트 링크가 실제 본문에 배선된다(url 보정 후 활성).
    """
    try:
        from config import safe_load_json
        data = safe_load_json(LINK_MAP_PATH, default=dict)
        if isinstance(data, dict) and isinstance(data.get("overlay"), dict):
            return data["overlay"]
    except (OSError, ValueError) as exc:
        log.debug(f"[sibling_merge] 링크 오버레이 로드 생략: {exc}")
    return {}


# ─────────────────────────────────────────────────────────────────────────────
# 5) 리포트 — reports/{date}_sibling_merge.md.
# ─────────────────────────────────────────────────────────────────────────────
def render_report(plans: List[dict], *, link_result: Optional[dict] = None) -> str:
    """제도별 필러·사촌·병합 액션 표(한국어 마크다운)."""
    today = date.today().strftime("%Y-%m-%d")
    total_siblings = sum(len(p["siblings"]) for p in plans)
    lines: List[str] = [
        "# 사촌 글 자동 병합 실행 계획 (개선안 #5)\n",
        f"> 생성: {today} · 병합 대상 제도 {len(plans)}개 · "
        f"필러로 통합할 사촌 {total_siblings}편\n",
        "## 원리 (폐기 아님 · 통합)\n",
        "같은 제도에 사촌 글이 몰리면 서로의 검색 유입을 갉아먹는다(자기잠식). "
        "**최강 글 1편을 필러(허브)로 세우고 나머지 사촌은 필러를 가리키는 내부링크 + "
        "겹치지 않는 세부 각도로 재정의**하면, 권위가 필러에 모여 클러스터 전체가 함께 "
        "상위로 끌려 올라간다.\n",
        "> ⚠️ 어떤 글도 삭제·비공개·동결하지 않는다. 사촌은 살아남아 '역할 재배치'된다. "
        "실제 재작성·재발행은 사용자 트리거 — 아래는 실행 계획 + 링크 배선표.\n",
    ]

    if not plans:
        lines.append("## 결과\n")
        lines.append("병합 대상 제도 없음 — 임계 이상으로 몰린 사촌 그룹이 없다(분포 양호).")
        return "\n".join(lines)

    lines.append("## 요약 — 제도별 병합 규모\n")
    lines.append("| 제도 | 사촌 편수 | 필러(허브) | 필러 선정 근거 |")
    lines.append("|---|---:|---|---|")
    for plan in plans:
        pillar_title = (plan["pillar"].get("clean_title")
                        or plan["pillar"].get("title") or "")
        lines.append(
            f"| {plan['institution']} | {plan['size']} | "
            f"{pillar_title[:34]} | {plan['pillar_reason']} |"
        )

    for plan in plans:
        pillar = plan["pillar"]
        pillar_title = pillar.get("clean_title") or pillar.get("title") or ""
        lines.append(f"\n## 🏛️ {plan['institution']} ({plan['size']}편)\n")
        lines.append(f"- **필러(허브) 지정:** {pillar_title}")
        if pillar.get("url"):
            lines.append(f"  - URL: {pillar['url']}")
        else:
            lines.append("  - ⚠️ URL 미보정 — 공개 포스트목록/통계에서 url 채운 뒤 링크 활성")
        lines.append(f"  - 선정 근거: {plan['pillar_reason']}")
        lines.append(
            f"- **작업:** 아래 {len(plan['siblings'])}편을 필러로 내부링크 연결"
            "(리다이렉트 성격) + 각 글은 필러와 겹치지 않게 세부 각도로 차별화 리프레시."
        )
        lines.append("\n| 사촌 글 | 필러 링크 | 차별화 각도(리프레시) |")
        lines.append("|---|---|---|")
        for sib in plan["siblings"]:
            post = sib["post"]
            stitle = (post.get("clean_title") or post.get("title") or "")[:30]
            link = sib["link_to_pillar"]
            arrow = ("→ " + (link["title"][:20]) if link.get("url")
                     else "→ 필러(url 대기)")
            diff = sib["differentiation"]
            lines.append(f"| {stitle} | {arrow} | {diff[:52]} |")

    lines.append("\n---\n## 링크 통합 상태\n")
    if link_result is not None:
        lines.append(
            f"- 사촌→필러 링크 오버레이: **{link_result['siblings_linked']}편** 배선"
            f"{' (파일 저장됨)' if link_result.get('written') else ' (dry-run)'}"
        )
        lines.append(f"  - 오버레이 파일: `{link_result['path']}`")
        cache = link_result.get("topic_cache")
        cache_msg = {"ok": "갱신됨", "skipped": "생략", "failed": "갱신 실패(url 미보정 등)"}
        lines.append(f"- topic_cluster 내부링크 캐시: {cache_msg.get(cache, cache)}")
    else:
        lines.append("- 링크 통합 미실행(리포트 전용 호출).")

    lines.append("\n## 실행 체크리스트\n")
    lines.append("1. url 보정: `python -m core.stats_fetcher` 또는 토픽리포트(공개 포스트목록 자동 보정)")
    lines.append("2. 사촌 글 리프레시 시 필러 링크 자동삽입(오버레이 + topic_cluster 캐시)")
    lines.append("3. 사촌은 '차별화 각도'로만 재작성 — 필러와 중복 문단 제거(폐기 아님)")
    lines.append("4. 신규 발행은 이 제도 자제(institution_dedup 게이트가 이미 차단)")
    lines.append("\n> 실제 재작성·재발행·삭제는 사용자 트리거. 이 리포트는 계획·링크 배선표만 산출.")
    return "\n".join(lines)


def run_sibling_merge_report(
    *,
    min_group_size: Optional[int] = None,
    integrate: bool = True,
) -> Optional[Path]:
    """병합 계획 산출 → 링크 통합(선택) → reports/{date}_sibling_merge.md.

    Args:
        min_group_size: 병합 후보 최소 편수(생략 시 설정/기본).
        integrate:      True 면 링크 오버레이·topic_cluster 캐시를 실제 배선.

    Returns:
        생성한 리포트 경로 또는 카탈로그가 비면 None.
    """
    plans = build_merge_plan(min_group_size=min_group_size)
    if not plans:
        # 대상이 없어도 '분포 양호' 리포트는 남긴다(운영 가시성).
        log.info("[sibling_merge] 병합 대상 제도 없음 — 분포 양호 리포트 생성")

    link_result = None
    if integrate and plans:
        try:
            link_result = integrate_links(plans)
        except (OSError, ValueError, TypeError) as exc:
            log.warning(f"[sibling_merge] 링크 통합 실패(fail-open): {exc}")
            link_result = None

    HARNESS_REPORTS.mkdir(parents=True, exist_ok=True)
    today = date.today().strftime("%Y-%m-%d")
    out = HARNESS_REPORTS / f"{today}_sibling_merge.md"
    try:
        out.write_text(render_report(plans, link_result=link_result), encoding="utf-8")
    except OSError as exc:
        log.error(f"[sibling_merge] 리포트 저장 실패: {exc}", exc_info=True)
        return None
    log.info(
        f"[sibling_merge] 리포트 생성: {out.name} · 제도 {len(plans)}개 · "
        f"사촌 {sum(len(p['siblings']) for p in plans)}편"
    )
    return out


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO)
    path = run_sibling_merge_report()
    if path:
        print(f"사촌 병합 리포트 생성: {path}")
    else:
        print("카탈로그가 비어 리포트를 생성하지 못했습니다 "
              "(performance_db.sync_from_history 먼저).")
