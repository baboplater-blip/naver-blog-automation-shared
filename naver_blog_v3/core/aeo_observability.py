# core/aeo_observability.py — AEO 신호가 '실제 AI요약 인용'과 상관 있는지 관측·검증 (#6)
"""aeo_scorer 는 이모지·"결론부터"·"Q1." 같은 표면 마커 존재 여부만 세면서 "AI 요약 상단
노출 확률이 높아진다"고 단정해 왔다 — 실제 인용 여부를 한 번도 관측하지 않은 순환논증.

이 모듈은 그 고리를 닫는다:
  1) 관측: 특정 키워드의 네이버 통합검색 SERP 에서 우리 블로그가 **AI요약/스마트블록에
     인용됐는지**를 best-effort 로 판정(주입식 fetcher, 실패 시 None=unknown, fail-open).
  2) 저장: (keyword, url, cited, signals) 를 append-only JSONL 로 기록(스키마 변경 0).
  3) 상관: 저장된 (신호값, cited) 쌍으로 **신호별 점이연 상관**을 계산 →
     어떤 신호가 인용과 상관 있고(유지) 어떤 게 무상관인지(가중치↓ 후보) 리포트.
  4) 안내: 관측 데이터가 있으면 '실측 상관 상위 신호'로 재작성 안내, 없으면 중립(허위 확신 제거).

⚠️ 한계: 네이버 AI요약 영역은 공식 API 로 안 나오고 SERP HTML 구조가 자주 바뀐다.
   observe_citation 은 취약한 best-effort 이며, 판정 불가 시 None 을 돌려 무해하게 넘어간다.
   실제 가치는 (2)(3)(4)의 관측→상관→정직화 프레임이며, 관측 소스는 언제든 교체 가능하다.
"""
from __future__ import annotations

import json
import math
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Iterable, Optional

from config.logger import get_logger

log = get_logger(__name__)

_BASE_DIR = Path(__file__).resolve().parent.parent
# 관측 로그는 계정별 측정 데이터 — 프로필별 격리(2026-07-19 격리 감사).
try:
    from config import profile as _nbaprofile
    _LOG_DIR = _nbaprofile.logs_dir()
except Exception:  # noqa: BLE001 — 프로필 레이어 부재 시 레거시
    _LOG_DIR = _BASE_DIR / "logs"
_OBS_PATH = _LOG_DIR / "aeo_citations.jsonl"

_SERP_URL = "https://search.naver.com/search.naver"
# AI요약/스마트블록 영역을 가리키는 휴리스틱 마커(구조 변경에 대비해 여러 개를 OR).
_AI_MARKERS = ("ai 요약", "ai요약", "ai_smart", "smart_block", "cue:", "ai-summary",
               "ainews", "생성형", "요약 정보")


# ── 관측 저장(append-only JSONL — performance_db 스키마 불변) ────────────────────
def _now_iso(now: Optional[float] = None) -> str:
    if now is None:
        return ""                          # 호출자가 타임스탬프를 넘기지 않으면 공란(결정적 테스트)
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now))


def record_observation(keyword: str, url: str, cited: Optional[bool], *,
                       signals: Optional[dict] = None, observed_at: str = "",
                       path: Optional[Path] = None) -> None:
    """관측 1건 append. cited=None(판정불가)도 기록하되 상관에서는 제외된다."""
    p = Path(path) if path else _OBS_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "keyword": (keyword or "").strip(),
        "url": (url or "").strip(),
        "cited": (None if cited is None else bool(cited)),
        "signals": {k: int(v) for k, v in (signals or {}).items()},
        "observed_at": observed_at,
    }
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def load_observations(path: Optional[Path] = None) -> list:
    p = Path(path) if path else _OBS_PATH
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except (ValueError, json.JSONDecodeError):
            continue
    return out


# ── SERP AI요약 인용 관측(best-effort, fail-open) ───────────────────────────────
def _default_fetcher(keyword: str, timeout: float = 10.0) -> str:
    qs = urllib.parse.urlencode({"query": keyword})
    req = urllib.request.Request(f"{_SERP_URL}?{qs}",
                                 headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _detect_citation(html: str, blog_id: str, url_hint: str = "") -> Optional[bool]:
    """SERP HTML 에서 AI요약 영역을 찾아 우리 블로그 인용 여부 판정.

    반환: True(인용) / False(AI영역 있으나 우리 없음) / None(AI영역 판별 불가=unknown).
    """
    if not html:
        return None
    low = html.lower()
    marker_pos = -1
    for m in _AI_MARKERS:
        i = low.find(m)
        if i >= 0:
            marker_pos = i
            break
    if marker_pos < 0:
        return None                        # AI요약 영역 자체를 못 찾음 → 판정 불가
    # 마커 주변 윈도우 안에 우리 블로그 식별자가 있으면 인용으로 본다(보수적 창).
    window = low[marker_pos: marker_pos + 4000]
    needles = [n for n in ((blog_id or "").lower(), (url_hint or "").lower()) if n]
    if not needles:
        return None
    return any(n in window for n in needles)


def observe_citation(keyword: str, *, blog_id: str = "", url_hint: str = "",
                     fetcher: Optional[Callable[[str], str]] = None) -> Optional[bool]:
    """키워드 SERP 에서 우리 블로그의 AI요약 인용 여부. 실패/판별불가 시 None(fail-open)."""
    if not keyword:
        return None
    fetch = fetcher or _default_fetcher
    try:
        html = fetch(keyword)
    except Exception as exc:  # noqa: BLE001 — 스크랩 실패는 무해하게 unknown
        log.debug(f"[aeo_observability] SERP 조회 실패({keyword[:20]}): {exc}")
        return None
    try:
        return _detect_citation(html, blog_id, url_hint)
    except Exception as exc:  # noqa: BLE001
        log.debug(f"[aeo_observability] 파싱 실패: {exc}")
        return None


# ── 상관 분석(신호 ↔ 실제 인용) ──────────────────────────────────────────────────
def _point_biserial(values: list, labels: list) -> Optional[float]:
    """연속 신호값(values) 과 이진 라벨(labels: 1=cited) 의 점이연 상관. 표본 부족·무분산 시 None."""
    n = len(values)
    if n < 4 or len(labels) != n:
        return None
    ones = [v for v, l in zip(values, labels) if l]
    zeros = [v for v, l in zip(values, labels) if not l]
    if not ones or not zeros:
        return None
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / n
    if var <= 1e-12:
        return None                        # 신호가 전 글에서 동일 → 상관 정의 불가
    s = math.sqrt(var)
    m1, m0 = sum(ones) / len(ones), sum(zeros) / len(zeros)
    p = len(ones) / n
    q = 1 - p
    return round(((m1 - m0) / s) * math.sqrt(p * q), 3)


def correlate(observations: Optional[Iterable[dict]] = None, *,
              min_samples: int = 4) -> dict:
    """저장된 (signals, cited) 관측으로 신호별 상관을 계산.

    반환: {
      "sample_size": 라벨 있는 관측 수,
      "cited": 인용 수, "uncited": 비인용 수,
      "signals": {name: {"corr": float|None, "cited_mean":.., "uncited_mean":.., "verdict": str}},
      "ranked": [(name, corr), ...] 상관 내림차순,
      "status": "ok" | "insufficient",
    }
    verdict: matters(상관≥0.2) | weak(0<상관<0.2) | no_signal(≤0) | insufficient
    """
    rows = [o for o in (observations if observations is not None else load_observations())
            if isinstance(o, dict) and o.get("cited") is not None]
    n = len(rows)
    cited_n = sum(1 for o in rows if o.get("cited"))
    if n < min_samples or cited_n == 0 or cited_n == n:
        return {"status": "insufficient", "sample_size": n,
                "cited": cited_n, "uncited": n - cited_n, "signals": {}, "ranked": []}

    names = sorted({k for o in rows for k in (o.get("signals") or {})})
    labels = [1 if o.get("cited") else 0 for o in rows]
    signals: dict = {}
    for name in names:
        vals = [int((o.get("signals") or {}).get(name, 0)) for o in rows]
        corr = _point_biserial(vals, labels)
        ones = [v for v, l in zip(vals, labels) if l]
        zeros = [v for v, l in zip(vals, labels) if not l]
        if corr is None:
            verdict = "insufficient"
        elif corr >= 0.2:
            verdict = "matters"
        elif corr > 0.0:
            verdict = "weak"
        else:
            verdict = "no_signal"
        signals[name] = {
            "corr": corr,
            "cited_mean": round(sum(ones) / len(ones), 2) if ones else 0.0,
            "uncited_mean": round(sum(zeros) / len(zeros), 2) if zeros else 0.0,
            "verdict": verdict,
        }
    ranked = sorted(((k, v["corr"]) for k, v in signals.items() if v["corr"] is not None),
                    key=lambda t: t[1], reverse=True)
    return {"status": "ok", "sample_size": n, "cited": cited_n, "uncited": n - cited_n,
            "signals": signals, "ranked": ranked}


def citation_guidance(corr: Optional[dict] = None, *, limit: int = 3) -> str:
    """상관 결과로 정직한 재작성 안내를 만든다. 데이터 부족이면 중립(허위 확신 없음)."""
    c = corr if corr is not None else correlate()
    if c.get("status") != "ok":
        return ("아직 실측 인용 데이터가 부족해 어떤 신호가 AI요약 인용을 좌우하는지 "
                "검증되지 않았습니다(관측 축적 중).")
    top = [name for name, cc in c.get("ranked", []) if cc is not None and cc >= 0.2][:limit]
    n = c.get("sample_size", 0)
    if not top:
        return (f"실측 {n}건 기준, 현재 마커 신호와 AI요약 인용의 상관이 확인되지 않았습니다 "
                f"— 마커 채우기보다 원본성·최신성에 집중하세요.")
    return (f"실측 {n}건 기준, AI요약 인용과 상관이 확인된 신호: {', '.join(top)} "
            f"(무상관 신호는 채워도 인용에 기여하지 않습니다).")


__all__ = [
    "record_observation", "load_observations", "observe_citation",
    "correlate", "citation_guidance",
]
