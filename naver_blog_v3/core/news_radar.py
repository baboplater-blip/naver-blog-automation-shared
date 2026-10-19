# core/news_radar.py — 뉴스 선행지표 레이더 (뉴스 API P0, 2026-07-19)
"""뉴스 = 검색 수요의 선행지표. 워치 키워드의 뉴스 신호를 매일 재서
발행 큐 가산점·홈 탭 한 줄·카카오 묶음의 재료를 만든다.

설계 원칙 — **주제 무지(topic-agnostic)**: 이 모듈은 운영 주제가 무엇인지 모른다.
주제 지식은 전부 활성 프로필의 기존 자산에서 자동 파생된다:
  ① rank_watchlist 키워드(사용자가 이미 고른 승부 키워드)
  ② brand/templates/category_keywords.json (프로필별 카테고리 키워드)
  ③ question_seeds 의 anchor 헤드 키워드(검색량 큰 순)
→ 새 프로필을 어떤 주제로 만들든 설정 0줄로 그 주제의 레이더가 켜진다.

신호 2종(범용 — 도메인 어휘 하드코딩 없음):
  · spike  — 최근 72시간 기사 수가 자기 기준선(EMA) 대비 급증.
             첫 측정(기준선 없음)은 판단 보류(rank_alerts 오탐 0 원칙과 동일).
  · change — 기사 제목·요약에 범용 변경 어휘(개정·변경·발표·인상·개편·시행…)
             등장. brand.json "news_lexicon" 으로 프로필별 확장 가능(선택).

원칙:
- fail-open: 자격증명 없음/네트워크 오류 → 해당 키워드만 스킵, 파이프라인 무영향.
- 쿼터 가드: 검색 API 앱 쿼터(25,000/일)는 블로그 검색(순위 추적)과 공유 —
  NEWS_DAILY_BUDGET(기본 800)으로 상한. 사용량은 상태 파일에 날짜별 기록.
- ToS: 뉴스 본문 전재 없음 — 제목·요약·링크 메타데이터만 신호로 사용.
- 가산점만: 발행 큐에서 뉴스는 부스트(가산)일 뿐, 실측 검색량 1순위 불변.
- 프로필 격리: 상태는 logs/news_radar.json (활성 프로필 라우팅).

- 수동:  python -m core.news_radar
- 일간:  scheduler/rank_monitor 의 ①c 스텝 (매일 09:10)
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.logger import get_logger

log = get_logger(__name__)

from config import profile as _nbaprofile

_DATA_DIR = _nbaprofile.app_data_root()
STATE_PATH = _DATA_DIR / "logs" / "news_radar.json"

NEWS_URL = "https://openapi.naver.com/v1/search/news.json"
DISPLAY = 30                 # 최신순 30건 — 72h 카운트는 30에서 포화(스파이크 판정엔 충분)
WINDOW_HOURS = 72
_TAG = re.compile(r"<[^>]+>")

# 범용 변경 어휘 — 제도·시험·정책·요금 무엇이든 "바뀐다"는 신호(주제 무관).
_CHANGE_WORDS = [
    "개정", "변경", "발표", "인상", "인하", "폐지", "개편",
    "시행", "연장", "확대", "축소", "신설", "도입", "전면",
]

try:
    from config.settings import API_REQUEST_TIMEOUT
except (ImportError, AttributeError):
    API_REQUEST_TIMEOUT = 10


# ── 설정 (settings 미등록 시 안전 기본값 — GUI 재생성에도 생존) ──────────
def _setting(name: str, default):
    try:
        from config import settings as _s
        return getattr(_s, name, default)
    except ImportError:
        return default


def _enabled() -> bool:
    return bool(_setting("NEWS_RADAR_ENABLED", True))


def _daily_budget() -> int:
    return int(_setting("NEWS_DAILY_BUDGET", 800))


def _max_keywords_default() -> int:
    return int(_setting("NEWS_RADAR_MAX_KEYWORDS", 150))


def _creds() -> tuple[str, str]:
    """검색 API 자격증명(순위 추적과 동일 앱). 없으면 빈 문자열."""
    try:
        from core.naver_api import runtime_credentials
        return runtime_credentials()
    except (ImportError, AttributeError, ValueError):
        return "", ""


def _lexicon() -> list[str]:
    """범용 변경 어휘 + 프로필 브랜드팩 news_lexicon(선택 확장)."""
    words = list(_CHANGE_WORDS)
    try:
        from config import brand as _brand
        extra = _brand._brand_json().get("news_lexicon")
        if isinstance(extra, (list, tuple)):
            words += [str(w) for w in extra if str(w).strip()]
    except Exception:  # noqa: BLE001 — 브랜드팩 없음/오류는 범용 사전만
        pass
    return words


# ── 워치 키워드 자동 파생 (주제 지식은 전부 프로필 자산에서) ──────────────
def watch_keywords(limit: int | None = None) -> list[str]:
    """①워치리스트 → ②브랜드 카테고리 키워드 → ③question_seeds 앵커(검색량순).
    순서 보존 중복 제거, limit 캡. 어떤 소스가 없어도 나머지로 동작."""
    limit = limit or _max_keywords_default()
    out: list[str] = []
    seen: set[str] = set()

    def _add(kw: str):
        kw = (kw or "").strip()
        if kw and kw not in seen and len(out) < limit:
            seen.add(kw)
            out.append(kw)

    # ① 워치리스트 — 사용자가 이미 고른 승부 키워드가 최우선
    try:
        from core import rank_watchlist
        for kw in rank_watchlist.keywords():
            _add(kw)
    except Exception as e:  # noqa: BLE001
        log.debug(f"[news_radar] 워치리스트 파생 실패(무시): {e}")

    # ② 브랜드 카테고리 키워드 (프로필 라우팅 — exampleprofile이면 화상영어·유학…)
    try:
        from config import brand as _brand
        from config import safe_load_json
        path = _brand.template_path("category_keywords.json")
        data = safe_load_json(path, default=dict)
        for kws in (data or {}).values():
            if isinstance(kws, list):
                for kw in kws[:5]:      # 카테고리당 상위 5개 — 변형 표기 폭주 방지
                    _add(kw)
    except Exception as e:  # noqa: BLE001
        log.debug(f"[news_radar] 카테고리 키워드 파생 실패(무시): {e}")

    # ③ question_seeds 앵커 — 검색량 큰 순으로 남은 자리 충전
    try:
        from config import safe_load_json
        seeds = safe_load_json(_DATA_DIR / "logs" / "question_seeds.json",
                               default=list)
        anchors: dict[str, int] = {}
        for s in seeds or []:
            if isinstance(s, dict) and s.get("anchor"):
                a = str(s["anchor"]).strip()
                v = int(s.get("anchor_volume") or 0)
                if a and v > 0:
                    anchors[a] = max(anchors.get(a, 0), v)
        for a in sorted(anchors, key=lambda k: anchors[k], reverse=True):
            _add(a)
    except Exception as e:  # noqa: BLE001
        log.debug(f"[news_radar] question_seeds 파생 실패(무시): {e}")

    return out


# ── 뉴스 검색 ──────────────────────────────────────────
def _search_news(keyword: str, cid: str, secret: str,
                 display: int = DISPLAY) -> tuple[list, str]:
    """뉴스 검색(최신순) → (items, status). status: ok | auth | error."""
    from core.naver_api import news_request
    try:
        request = news_request(keyword, cid, secret, display=display)
        req = urllib.request.Request(request["url"], headers=request["headers"])
        with urllib.request.urlopen(req, timeout=API_REQUEST_TIMEOUT) as resp:
            data = json.loads(resp.read())
        return (data.get("items", []) or []), "ok"
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            log.warning(f"[news_radar] 검색 API 인증 실패 {e.code}")
            return [], "auth"
        log.debug(f"[news_radar] HTTP {e.code} ({keyword[:20]})")
        return [], "error"
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        log.debug(f"[news_radar] 조회 실패 ({keyword[:20]}): {e}")
        return [], "error"


def _parse_pubdate(s: str) -> datetime | None:
    """RFC 형식 pubDate('Sat, 19 Jul 2026 10:30:00 +0900') → aware datetime.
    tz 누락 시 KST 가정(naive-aware 비교 크래시 방지)."""
    try:
        dt = parsedate_to_datetime(s)
    except (TypeError, ValueError):
        return None
    if dt is not None and dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone(timedelta(hours=9)))
    return dt


def _plain(s: str) -> str:
    return _TAG.sub("", s or "")


# ── 상태 (프로필 격리 저장소) ─────────────────────────
def _load_state() -> dict:
    from config import safe_load_json
    st = safe_load_json(STATE_PATH, default=dict)
    if not isinstance(st, dict):
        st = {}
    st.setdefault("keywords", {})
    st.setdefault("budget", {})
    st.setdefault("signals", [])
    return st


def _save_state(st: dict) -> None:
    from config import atomic_write_json
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(str(STATE_PATH), st)


def budget_left(st: dict | None = None, today: str = "") -> int:
    """오늘 남은 뉴스 호출 예산(0 이하 = 소진)."""
    st = st or _load_state()
    today = today or datetime.now().strftime("%Y-%m-%d")
    used = int((st.get("budget") or {}).get(today, 0))
    return _daily_budget() - used


def consume_budget(n: int, st: dict | None = None, *, save: bool = True) -> None:
    """뉴스 호출 n건을 오늘 예산에서 차감(마감 4호기 등 다른 소비자도 합산)."""
    own = st is None
    st = st if st is not None else _load_state()
    today = datetime.now().strftime("%Y-%m-%d")
    b = st.setdefault("budget", {})
    b[today] = int(b.get(today, 0)) + n
    for k in [k for k in b if k != today]:      # 과거 날짜 정리
        del b[k]
    if own and save:
        _save_state(st)


# ── 일간 레이더 실행 ──────────────────────────────────
def run_news_radar(*, max_keywords: int | None = None, sleep: float = 0.15,
                   now: datetime | None = None) -> dict:
    """워치 키워드 뉴스 신호 측정 → 상태 저장.

    반환 {checked, spikes, changes, budget_used, reason?}.
    자격증명 없음/비활성/예산 소진은 fail-open 스킵(reason 명시).
    """
    if not _enabled():
        return {"checked": 0, "spikes": 0, "changes": 0, "reason": "disabled"}
    cid, secret = _creds()
    if not (cid and secret):
        return {"checked": 0, "spikes": 0, "changes": 0,
                "reason": "no_credentials"}

    kws = watch_keywords(limit=max_keywords)
    if not kws:
        return {"checked": 0, "spikes": 0, "changes": 0, "reason": "no_keywords"}

    st = _load_state()
    left = budget_left(st)
    if left <= 0:
        return {"checked": 0, "spikes": 0, "changes": 0,
                "reason": "budget_exhausted"}
    kws = kws[:max(0, left)]

    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=WINDOW_HOURS)
    lex = _lexicon()
    signals = []
    checked = 0

    for kw in kws:
        items, status = _search_news(kw, cid, secret)
        checked += 1
        if status == "auth":
            consume_budget(checked, st, save=False)
            _save_state(st)
            return {"checked": checked, "spikes": 0, "changes": 0,
                    "reason": "auth_failed"}
        if status == "error":
            time.sleep(sleep)
            continue        # 해당 키워드만 스킵 — 기준선·seen 미변경

        rec = st["keywords"].setdefault(kw, {})
        recent = []
        for it in items:
            dt = _parse_pubdate(it.get("pubDate", ""))
            if dt is not None and dt >= cutoff:
                recent.append(it)
        count = len(recent)

        # 신규 기사(이전 실행에서 못 본 링크)
        seen_prev = set(rec.get("seen") or [])
        links = [(it.get("originallink") or it.get("link") or "")
                 for it in recent]
        new_links = [ln for ln in links if ln and ln not in seen_prev]

        # spike — 기준선(EMA) 대비 급증. 첫 측정은 판단 보류(오탐 0).
        baseline = rec.get("baseline")
        spike = (baseline is not None
                 and count >= 3
                 and count >= 2.0 * max(baseline, 1.0))

        # change — 72h 기사 **제목**에 변경 어휘 (요약까지 보면 '확대' 같은 흔한
        # 단어가 남발 매칭됨 — 라이브 실측 15건 중 9건 신호로 확인, 제목 한정으로 조임)
        changes = []
        for it in recent:
            text = _plain(it.get("title", ""))
            hit = next((w for w in lex if w in text), "")
            if hit:
                changes.append({
                    "title": _plain(it.get("title", ""))[:80],
                    "link": it.get("originallink") or it.get("link") or "",
                    "pubDate": it.get("pubDate", ""),
                    "word": hit,
                })
            if len(changes) >= 3:
                break

        rec["baseline"] = (float(count) if baseline is None
                           else round(0.7 * float(baseline) + 0.3 * count, 2))
        rec["last_count"] = count
        rec["seen"] = (links + [ln for ln in seen_prev if ln not in links])[:60]

        if spike or changes:
            signals.append({
                "keyword": kw, "count72h": count,
                "baseline": baseline, "spike": bool(spike),
                "new_count": len(new_links), "changes": changes,
            })
        time.sleep(sleep)

    # 스파이크 → 변경 다발 → 기사 수 순
    signals.sort(key=lambda s: (s["spike"], len(s["changes"]), s["count72h"]),
                 reverse=True)
    st["signals"] = signals
    st["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    consume_budget(checked, st, save=False)
    _save_state(st)

    n_spike = sum(1 for s in signals if s["spike"])
    n_change = sum(1 for s in signals if s["changes"])
    log.info(f"[news_radar] 측정 {checked}건 — 스파이크 {n_spike} · "
             f"변경 감지 {n_change} · 신호 {len(signals)}건")
    for s in signals[:5]:
        tag = "🔥스파이크" if s["spike"] else "🔧변경"
        w = (f"·{s['changes'][0]['word']}" if s["changes"] else "")
        log.info(f"    {tag} {s['keyword']} — 72h 기사 {s['count72h']}건{w}")
    return {"checked": checked, "spikes": n_spike, "changes": n_change,
            "signals": len(signals), "budget_used": checked}


# ── 소비자 API (발행 큐·홈 탭·카카오) ──────────────────
def signal_map(max_age_hours: int = 30) -> dict:
    """{keyword: signal} — 상태가 신선할 때만(기본 30h). 스테일/없음 → {}."""
    st = _load_state()
    upd = st.get("updated") or ""
    try:
        age = datetime.now() - datetime.strptime(upd, "%Y-%m-%d %H:%M")
        if age > timedelta(hours=max_age_hours):
            return {}
    except ValueError:
        return {}
    return {s["keyword"]: s for s in st.get("signals") or []
            if s.get("keyword")}


def boost_for(keyword: str, sig_map: dict | None = None) -> float:
    """발행 큐 복합점수 가산점. spike +8 · change +6 · 둘 다 +10 (캡).
    부분 매칭(마감 축과 동일 규약). 신호 없음/스테일 → 0."""
    m = sig_map if sig_map is not None else signal_map()
    if not m or not keyword:
        return 0.0
    sig = m.get(keyword)
    if sig is None:
        for k, v in m.items():
            if k in keyword or keyword in k:
                sig = v
                break
    if not sig:
        return 0.0
    pts = (8.0 if sig.get("spike") else 0.0) + \
          (6.0 if sig.get("changes") else 0.0)
    return min(pts, 10.0)


def summary_line() -> str:
    """홈 탭 한 줄. 신호 없으면 빈 문자열(조용히)."""
    m = signal_map()
    if not m:
        return ""
    sigs = list(m.values())
    top = sigs[0]
    why = ("기사 급증" if top.get("spike")
           else f"'{top['changes'][0]['word']}' 보도" if top.get("changes")
           else "")
    return (f"📰 오늘 뜨는 주제 {len(sigs)}건 · "
            f"{top['keyword']} ({why} · 72h {top['count72h']}건)")


def kakao_lines(max_lines: int = 2) -> list[str]:
    """카카오 일간 묶음(rank_monitor)에 편승할 뉴스 한 줄들. 없으면 []."""
    m = signal_map()
    lines = []
    for s in list(m.values())[:max_lines]:
        tag = "🔥" if s.get("spike") else "📰"
        why = (f"'{s['changes'][0]['word']}' 보도" if s.get("changes")
               else "기사 급증")
        lines.append(f"{tag} {s['keyword']} — {why} (72h {s['count72h']}건)")
    return lines


def verify() -> dict:
    """진단: 자격증명·키워드 파생·예산·최근 신호."""
    cid, secret = _creds()
    kws = watch_keywords()
    st = _load_state()
    return {
        "enabled": _enabled(),
        "credentials": bool(cid and secret),
        "watch_keywords": len(kws),
        "watch_sample": kws[:8],
        "budget_left": budget_left(st),
        "signals": len(st.get("signals") or []),
        "updated": st.get("updated") or "",
    }


if __name__ == "__main__":
    from config.profile_boot import pin_profile_from_argv
    pin_profile_from_argv()
    print(json.dumps(verify(), ensure_ascii=False, indent=2))
    if "--run" in sys.argv:
        n = 20
        for a in sys.argv:
            if a.startswith("--max="):
                n = int(a.split("=", 1)[1])
        print(json.dumps(run_news_radar(max_keywords=n), ensure_ascii=False,
                         indent=2))
