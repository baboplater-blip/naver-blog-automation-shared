# core/publish_reconciler.py
# 원격(네이버 공개 목록) 상태 대조 — 중복 발행 방지의 핵심(2026-09-18, 계약 §2.6/설계 §3.3).
#
# ⚠️ 이 모듈은 절대 발행하지 않는다. `reserved` 를 절대 만들지 않는다.
# 공개 목록은 이미 공개된 글만 보여주므로, 예약이 성공했는지 실패했는지를 여기서
# 확정할 수 없다(§11 — "원격 대조는 예약 글을 볼 수 없다"). `reserved` 는 예약 등록
# 경로(naver_uploader.upload_post_ex)가 읽은 검증된 증거로만 기록된다.
#
# 판정 규칙(설계 §3.3, 사전 고정 — 바꾸지 않는다):
#   1) 원격 log_no 를 이미 알고 있고 그것이 이번 목록에 단일 후보로 그대로 있으면 → live.
#      (이것이 live 로 승격되는 유일한 경로다. 제목 일치는 절대 이 경로로 쓰지 않는다.)
#   2) 제목 일치(정확 포함)·토큰 퍼지 일치(임계 0.6, 2위와 margin 0.08)는 candidates 에만
#      담고 입력 상태를 그대로 유지한다(자동 재발행 억제). 동일 제목 후보 2건 이상이면
#      evidence["duplicate_suspect"]=True 로 표시한다(중복 발행 의심 → 수동 확인 큐).
#   3) 후보가 전혀 없고 공개 예약 시각이 아직 안 왔으면 → 입력 상태 유지.
#   4) 후보가 전혀 없고 공개 예약 시각 + 유예(기본 90분)가 지났으면 → missing(확인 필요,
#      실패가 아니다).
#   5) 목록 조회 자체가 실패하면 → 판정 보류(입력 그대로 반환). failed 로 절대 내리지 않는다.
#
# 정직한 한계: 실제 무인증 공개 목록(``stats_fetcher.NaverBlogStats.get_post_list``)은
# 글마다 등록 날짜를 주지 않는다. 그래서 evidence["dated"] 는 이 구현에서 항상 False이고,
# "시도 시각 이후 등록" 이라는 날짜 근거는 현재 아무 경로에서도 만들어지지 않는다(설계
# §11에 이미 기록된 한계). live 승격은 오직 이미 알고 있는 log_no 의 직접 재확인으로만
# 일어난다 — 이는 날짜 추론이 필요 없는 가장 강한 증거이기 때문이다.

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Callable, Optional

from config.logger import get_logger

log = get_logger(__name__)

DEFAULT_GRACE_MINUTES = 90

# 토큰 퍼지 매칭 — stats_fetcher._fuzzy_best_key 와 같은 계열(임계값만 설계 §3.3 대로 0.6).
_FUZZY_THRESHOLD = 0.6
_FUZZY_MARGIN = 0.08
_TOKEN_SPLIT = re.compile(r"[\s·,\.\?!()\[\]'\"“”‘’%~\-/:;·|]+")


def _tokenize(text: str) -> set:
    out = set()
    for tok in _TOKEN_SPLIT.split((text or "").lower()):
        tok = tok.strip()
        if len(tok) >= 2 or tok.isdigit():
            out.add(tok)
    return out


def _default_post_list_fn(blog_id: str) -> Callable[[], list]:
    """기본 목록 조회 함수 — 무인증 공개 목록(PostTitleListAsync). 지연 임포트로
    core.stats_fetcher 에 대한 하드 의존을 피한다(호출 시점에만 필요)."""
    def _fn() -> list:
        from core.stats_fetcher import NaverBlogStats
        return NaverBlogStats(blog_id).get_post_list()
    return _fn


def _post_url(blog_id: str, log_no) -> Optional[str]:
    if not blog_id or not log_no:
        return None
    return f"https://blog.naver.com/{blog_id}/{log_no}"


def _parse_iso(value) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None


def _source_error_evidence(now: datetime, err: BaseException) -> dict:
    return {
        "source": "PostTitleListAsync", "collected_at": now.isoformat(),
        "pages_scanned": 0, "match": "none", "match_score": None,
        "dated": False, "source_error": f"{type(err).__name__}: {str(err)[:200]}",
    }


def _hold(publish_state: str, log_no, url, candidates: list, evidence: dict) -> dict:
    """판정 보류/유지 — 입력 상태를 그대로 돌려준다(§3.3 규칙 3·5의 공통 반환 모양)."""
    return {
        "publish_state": publish_state, "log_no": log_no, "url": url,
        "candidates": candidates, "evidence": evidence,
    }


def _find_title_candidates(posts: list, title: str) -> list:
    """정확 일치 + 토큰 퍼지 일치(임계 0.6) 후보를 점수 내림차순으로 반환한다.

    ⚠️ 이 함수의 결과는 candidates 로만 쓰인다 — live 판정에는 절대 쓰지 않는다
    (설계 §3.3 부정 사례: 같은 제목의 옛 글이 live 로 오판되는 것을 막기 위해서다).
    """
    norm_title = (title or "").strip()
    q_tokens = _tokenize(norm_title)
    scored = []
    for post in posts:
        post_title = (post.get("title") or "").strip()
        if not post_title:
            continue
        if norm_title and post_title == norm_title:
            scored.append({
                "title": post_title, "log_no": post.get("log_no"),
                "match_score": 1.0, "match": "exact",
            })
            continue
        post_tokens = _tokenize(post_title)
        if not q_tokens or not post_tokens:
            continue
        inter = len(q_tokens & post_tokens)
        if not inter:
            continue
        score = inter / len(q_tokens | post_tokens)
        if score >= _FUZZY_THRESHOLD:
            scored.append({
                "title": post_title, "log_no": post.get("log_no"),
                "match_score": round(score, 4), "match": "fuzzy",
            })
    scored.sort(key=lambda c: c["match_score"], reverse=True)
    return scored


def _judge_missing(input_state: str, publish_at, now: datetime, grace_minutes: int) -> str:
    """후보가 전혀 없을 때만 호출된다 — 공개 예약 시각 + 유예 경과 여부로
    입력 상태 유지 vs missing(확인 필요) 을 가른다(§3.3 규칙 3·4)."""
    publish_dt = _parse_iso(publish_at)
    if publish_dt is None:
        # 공개 예약 시각을 모르면 판단 근거가 없다 — 입력 상태 유지.
        return input_state
    if now < publish_dt:
        # 아직 공개 예약 시각 전 — 미발견은 실패의 증거가 아니다.
        return input_state
    if now < publish_dt + timedelta(minutes=grace_minutes):
        # 예약 시각은 지났지만 유예 안 — 아직은 그대로 둔다.
        return input_state
    # 유예까지 경과했는데도 전혀 안 보임 → 확인 필요(실패가 아니다).
    return "missing"


def _reconcile_with_posts(item: dict, posts: list, *, blog_id: str,
                          now: datetime, grace_minutes: int) -> dict:
    """이미 조회된 ``posts`` 리스트를 대상으로 단일 항목을 대조한다(목록 재조회 없음).
    ``reconcile``/``reconcile_all`` 공용 — 목록 조회 횟수를 결정하는 것은 호출자다."""
    input_state = item.get("publish_state", "none")
    title = item.get("title", "")
    known_log_no = item.get("log_no")
    url = item.get("url")
    publish_at = item.get("publish_at")
    collected_at = now.isoformat()

    # ── 규칙 1: 이미 알고 있는 log_no 를 직접 재확인 — live 승격의 유일한 경로 ──
    if known_log_no:
        direct = [p for p in posts if str(p.get("log_no", "")) == str(known_log_no)]
        if len(direct) == 1:
            evidence = {
                "source": "PostTitleListAsync", "collected_at": collected_at,
                "pages_scanned": 1, "match": "remote_id", "match_score": 1.0,
                "dated": False,
            }
            return {
                "publish_state": "live", "log_no": str(known_log_no),
                "url": url or _post_url(blog_id, known_log_no),
                "candidates": [], "evidence": evidence,
            }
        # 이번 목록에 없다고 바로 실패로 내리지 않는다(페이지네이션/캐시 지연일 수
        # 있음) — 아래 제목 기반 후보 탐색으로 계속 진행한다.

    # ── 규칙 2: 제목 후보 — 절대 live 로 승격하지 않는다 ──
    candidates = _find_title_candidates(posts, title)
    if candidates:
        exact_title_hits = sum(
            1 for p in posts
            if (p.get("title") or "").strip() == (title or "").strip() and (title or "").strip()
        )
        evidence = {
            "source": "PostTitleListAsync", "collected_at": collected_at,
            "pages_scanned": 1,
            "match": candidates[0]["match"], "match_score": candidates[0]["match_score"],
            "dated": False,
        }
        if exact_title_hits >= 2:
            evidence["duplicate_suspect"] = True
            log.warning("publish_reconciler: 동일 제목 후보 %d건 — 중복 발행 의심(live 승격 안 함): %r",
                       exact_title_hits, title)
        return _hold(input_state, known_log_no, url, candidates, evidence)

    # ── 규칙 3·4: 후보 전혀 없음 — 공개 예약 시각 기준으로 유지 vs missing ──
    evidence = {
        "source": "PostTitleListAsync", "collected_at": collected_at,
        "pages_scanned": 1, "match": "none", "match_score": None, "dated": False,
    }
    new_state = _judge_missing(input_state, publish_at, now, grace_minutes)
    return _hold(new_state, known_log_no, url, [], evidence)


def reconcile(item: dict, *, blog_id: str = "", post_list_fn: Optional[Callable[[], list]] = None,
              now: Optional[datetime] = None,
              grace_minutes: int = DEFAULT_GRACE_MINUTES) -> dict:
    """네이버 공개 상태와 로컬 기록(``item``)을 대조해 결말을 정한다. **절대 발행하지 않는다.**

    Args:
        item: work_queue 항목 모양의 dict — 최소 ``title``, ``publish_state`` 를 읽고
            있으면 ``log_no``, ``url``, ``publish_at``(공개 예약 시각, ISO) 도 읽는다.
        blog_id: ``post_list_fn`` 미주입 시 기본 조회 함수 생성에 쓰인다.
        post_list_fn: 목록 조회 함수(인자 없이 호출, ``[{"log_no","title"}, ...]`` 반환
            기대). 기본은 ``stats_fetcher.NaverBlogStats(blog_id).get_post_list``(무인증
            공개 목록). 테스트는 항상 주입해 실제 네트워크 호출을 0회로 만든다.
        now: 판정 기준 시각(테스트 주입용). 기본 ``datetime.now()``.
        grace_minutes: 공개 예약 시각 이후 ``missing`` 판정까지의 유예(분).

    Returns:
        {"publish_state": str, "log_no": str|None, "url": str|None,
         "candidates": list[dict], "evidence": dict}
    """
    now = now or datetime.now()
    fetch = post_list_fn or _default_post_list_fn(blog_id)
    try:
        posts = fetch()
    except Exception as e:  # noqa: BLE001 — 주입된 임의 콜러블의 임의 실패를 전부 판정 보류로.
        log.warning("publish_reconciler: 목록 조회 실패 — 판정 보류: %s", e, exc_info=True)
        return _hold(item.get("publish_state", "none"), item.get("log_no"), item.get("url"),
                    [], _source_error_evidence(now, e))
    if not isinstance(posts, list):
        log.warning("publish_reconciler: post_list_fn 이 list 를 반환하지 않음(%r) — 판정 보류",
                   type(posts).__name__)
        return _hold(item.get("publish_state", "none"), item.get("log_no"), item.get("url"),
                    [], _source_error_evidence(now, ValueError("post_list_fn returned non-list")))

    return _reconcile_with_posts(item, posts, blog_id=blog_id, now=now, grace_minutes=grace_minutes)


def reconcile_all(items: list, *, blog_id: str = "", post_list_fn: Optional[Callable[[], list]] = None,
                  now: Optional[datetime] = None,
                  grace_minutes: int = DEFAULT_GRACE_MINUTES) -> list:
    """여러 항목을 대조한다 — 목록 조회는 1회만 하고 전 항목이 재사용한다."""
    now = now or datetime.now()
    fetch = post_list_fn or _default_post_list_fn(blog_id)
    try:
        posts = fetch()
        if not isinstance(posts, list):
            raise ValueError("post_list_fn returned non-list")
        fetch_error: Optional[BaseException] = None
    except Exception as e:  # noqa: BLE001 — 위 reconcile() 과 동일한 이유.
        log.warning("publish_reconciler: 목록 조회 실패(reconcile_all) — 전체 판정 보류: %s",
                   e, exc_info=True)
        posts = []
        fetch_error = e

    results = []
    for item in items:
        if fetch_error is not None:
            results.append(_hold(item.get("publish_state", "none"), item.get("log_no"),
                                 item.get("url"), [], _source_error_evidence(now, fetch_error)))
            continue
        results.append(_reconcile_with_posts(item, posts, blog_id=blog_id, now=now,
                                             grace_minutes=grace_minutes))
    return results
