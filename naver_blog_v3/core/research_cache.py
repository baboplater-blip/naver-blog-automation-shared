# core/research_cache.py — 리서치 캐시 (웹검색 1회 → 같은 앵커 글 전체 재사용, 2026-07-20)
"""웹검색을 글마다 반복하지 않기 위한 앵커 키워드 단위 조사 결과 저장소.

문제: 웹검색이 글 생성 CLI 호출 **안에서** 매번 돌아 편당 ~2분이 순수 검색
비용이었다. 그런데 발행 풀의 제목들은 앵커 키워드(예: 화상영어)를 공유한다 —
같은 조사를 같은 주제 글들이 각자 반복하는 구조.

해법 — write-through 캐시:
  · 캐시 미스: 기존대로 웹검색 생성 → 반환된 sources + research_digest 를
    앵커 키워드로 **적립** (이 글은 기존과 동일 속도)
  · 캐시 히트: 조사 자료를 프롬프트에 주입하고 **웹검색 OFF 로 생성**
    (검색 루프·타임아웃 여유분 없이 — 편당 ~2분 단축, 검색 지연 실패 0)

원칙:
- 주제 무지: 앵커는 search_volume.best_keyword(실측 검색량 엑셀)에서 파생 —
  도메인 하드코딩 0, 어떤 프로필이든 설정 0줄.
- fact_gate 정합: 캐시 히트 글도 sources 를 승계하고 web_search_done=True —
  본문은 (과거에 실제 수행된) 웹 조사에 근거한다. 무근거 아님.
- 신선도 가드: 수집 시점을 프롬프트에 명시하고 실시간 표현을 금지.
  TTL(기본 14일) 지난 항목은 히트로 치지 않는다(만료 시 자동 재조사).
- fail-open: 캐시 어떤 실패도 생성 경로를 막지 않는다(미스로 폴백).
- 프로필 격리: logs/research_cache.json (활성 프로필 라우팅).

- 수동 확인: python -m core.research_cache
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.logger import get_logger

log = get_logger(__name__)

from config import profile as _nbaprofile

_DATA_DIR = _nbaprofile.app_data_root()
CACHE_PATH = _DATA_DIR / "logs" / "research_cache.json"

MAX_ENTRIES = 200          # 오래된 항목부터 정리(발행 풀 앵커 ~50개 대비 여유)
MAX_DIGEST_CHARS = 1800
MAX_SOURCES = 6
_DATE_FMT = "%Y-%m-%d %H:%M"


def _setting(name: str, default):
    try:
        from config import settings as _s
        return getattr(_s, name, default)
    except ImportError:
        return default


def _enabled() -> bool:
    return bool(_setting("RESEARCH_CACHE_ENABLED", True))


def _ttl_days() -> int:
    return int(_setting("RESEARCH_CACHE_TTL_DAYS", 14))


def anchor_for(title: str) -> str:
    """제목의 앵커(헤드) 키워드 — 실측 검색량 엑셀 매칭. 없으면 ''(캐시 비대상).

    발행 큐 정렬·rank_tracker 와 같은 수술(best_keyword) — 캐시 공유 단위가
    파이프라인 전체와 일관된다.
    """
    try:
        from core.search_volume import best_keyword
        kw, vol = best_keyword(title or "")
        return kw if (kw and vol > 0) else ""
    except Exception:  # noqa: BLE001 — 앵커 실패는 캐시 비대상일 뿐
        return ""


def _load() -> dict:
    from config import safe_load_json
    data = safe_load_json(CACHE_PATH, default=dict)
    return data if isinstance(data, dict) else {}


def _save(data: dict) -> None:
    from config import atomic_write_json
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(str(CACHE_PATH), data)


def _age_days(collected_at: str) -> float | None:
    try:
        return (datetime.now()
                - datetime.strptime(collected_at, _DATE_FMT)).total_seconds() / 86400
    except (TypeError, ValueError):
        return None


def lookup(anchor: str) -> dict | None:
    """신선한 캐시 항목 반환 {digest, sources, collected_at, age_days}. 없으면 None."""
    if not (_enabled() and anchor):
        return None
    rec = _load().get(anchor)
    if not isinstance(rec, dict):
        return None
    age = _age_days(rec.get("collected_at", ""))
    if age is None or age > _ttl_days():
        return None
    if not (rec.get("digest") or rec.get("sources")):
        return None
    return {"digest": rec.get("digest", ""),
            "sources": list(rec.get("sources") or [])[:MAX_SOURCES],
            "collected_at": rec.get("collected_at", ""),
            "age_days": round(age, 1)}


def store(anchor: str, *, digest: str = "", sources: list | None = None) -> bool:
    """조사 결과 적립(write-through). digest·sources 둘 다 비면 저장 안 함."""
    if not (_enabled() and anchor):
        return False
    digest = (digest or "").strip()[:MAX_DIGEST_CHARS]
    srcs = [str(s).strip() for s in (sources or []) if str(s).strip()][:MAX_SOURCES]
    if not (digest or srcs):
        return False
    try:
        data = _load()
        data[anchor] = {"digest": digest, "sources": srcs,
                        "collected_at": datetime.now().strftime(_DATE_FMT)}
        # 상한 초과 시 오래된 항목부터 정리
        if len(data) > MAX_ENTRIES:
            def _key_age(k):
                a = _age_days(data[k].get("collected_at", ""))
                return a if a is not None else 10_000.0
            for k in sorted(data, key=_key_age, reverse=True)[:len(data) - MAX_ENTRIES]:
                del data[k]
        _save(data)
        log.info(f"[research_cache] 적립: {anchor} (요약 {len(digest)}자 · "
                 f"출처 {len(srcs)}건)")
        return True
    except Exception as e:  # noqa: BLE001 — 적립 실패는 생성 경로 무영향
        log.debug(f"[research_cache] 적립 실패(무시): {e}")
        return False


def prompt_block(entry: dict, anchor: str) -> str:
    """캐시 히트 시 생성 프롬프트에 주입할 조사 자료 블록."""
    age = entry.get("age_days", 0)
    lines = [f"[사전 조사 자료 — '{anchor}' 주제, {age:.0f}일 전 웹 조사 수집]"]
    if entry.get("digest"):
        lines.append(entry["digest"])
    if entry.get("sources"):
        lines.append("조사 출처(실재 URL — 본문 인용에 사용 가능):")
        lines += [f"- {s}" for s in entry["sources"]]
    lines += [
        "★ 위 자료의 사실·수치를 우선 활용해 작성할 것(별도 웹검색 없이).",
        "★ 자료에 없는 구체 수치·날짜를 지어내지 말 것.",
        f"★ 이 자료는 {age:.0f}일 전 조사 — '오늘 발표', '방금 확인' 같은 실시간 표현 금지.",
    ]
    return "\n".join(lines)


def digest_request_block() -> str:
    """캐시 미스(웹검색 생성) 시 JSON 에 조사 요약 필드를 추가로 요청하는 블록.

    웹검색 생성에서만 붙는다 — 비검색 생성의 JSON 계약은 기존 그대로.
    필드가 안 와도 무해(sources 만 적립).
    """
    return ("\n[조사 요약 적립 — 같은 주제 다음 글이 재사용]\n"
            "웹 검색을 수행했으므로, 위 JSON 에 \"research_digest\" 키를 추가로 포함할 것:\n"
            "조사에서 확인한 핵심 사실·수치·날짜·조건을 6~10줄 불릿 문자열로\n"
            "(예: \"- 2026년 응시료 XX,XXX원\\n- 접수는 시험 N주 전 마감\").\n"
            "본문에 안 쓴 사실도 조사에서 확인했다면 포함할 것.")


def stats() -> dict:
    """진단: 항목 수·신선 항목 수·최근 적립."""
    data = _load()
    fresh = sum(1 for k in data if lookup(k))
    latest = ""
    if data:
        latest = max((r.get("collected_at", "") for r in data.values()), default="")
    return {"enabled": _enabled(), "entries": len(data), "fresh": fresh,
            "ttl_days": _ttl_days(), "latest": latest,
            "path": str(CACHE_PATH)}


if __name__ == "__main__":
    from config.profile_boot import pin_profile_from_argv
    pin_profile_from_argv()
    print(json.dumps(stats(), ensure_ascii=False, indent=2))
