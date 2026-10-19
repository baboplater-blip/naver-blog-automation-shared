"""scheduler/refresh_trigger.py — 30일+ 글 자동 리프레시 큐 적재.

매일 한 번 (Windows 작업 스케줄러) 호출되면:
1) post_refresher.get_refresh_candidates() 로 30일+ 글 N개 후보 선정.
2) 각 후보에 대해 refresh-curator 브리프 파일 생성:
     {harness_root}/briefs/refresh/{YYYY-MM-DD}_{slug}.yaml
3) 그날의 통합 큐 파일도 함께 생성:
     {harness_root}/briefs/refresh/{YYYY-MM-DD}_queue.yaml

실제 본문 재생성·발행은 트리거하지 않는다 — 사용자가 GUI/CLI 에서 큐를 보고
취사선택해 post_refresher.run_refresh_pipeline() 을 호출한다.

이 분리는 자동 발행 금지선(blog-orchestrator 의 핵심 안전 룰)을 지키기 위함.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import date, datetime
from pathlib import Path

_PKG_ROOT = Path(__file__).resolve().parent.parent  # naver_blog_v3/
_HARNESS_ROOT = _PKG_ROOT.parent
# ⚠️ sys.path 삽입은 config import **이전**이어야 한다 — 종전엔 뒤에 있어
# schtasks 직접 실행이 ImportError 로 죽던 잠복 버그(2026-07-19 격리 감사에서 발견).
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))
if __name__ == "__main__":  # schtasks 직접 실행 — 임포트 전 프로필 고정
    from config.profile_boot import pin_profile_from_argv
    pin_profile_from_argv()
from config import profile as _nbaprofile
_DATA_HARNESS = _nbaprofile.harness_data_root()


if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

log = logging.getLogger(__name__)

REFRESH_BRIEFS_DIR = _DATA_HARNESS / "briefs" / "refresh"
DEFAULT_MAX_CANDIDATES = 5


_SLUG_SAFE_RE = re.compile(r"[^0-9A-Za-z가-힣\-_]+")


def _slugify(title: str, max_len: int = 60) -> str:
    """제목을 파일명에 안전한 slug 로 변환."""
    cleaned = title.strip()
    # 날짜 prefix 제거
    cleaned = re.sub(r"^\s*\d{4}-\d{2}-\d{2}\s*[—–\-]?\s*", "", cleaned)
    cleaned = _SLUG_SAFE_RE.sub("-", cleaned)
    cleaned = re.sub(r"-+", "-", cleaned).strip("-")
    return cleaned[:max_len] or "untitled"


def _detect_category_from_title(title: str) -> str:
    """가벼운 detect_category 호출. 실패하면 'default'."""
    try:
        from core.content_generator import detect_category
        clean = re.sub(r"^\s*\d{4}-\d{2}-\d{2}\s*[—–\-]?\s*", "", title or "")
        cat = detect_category(clean)
        return cat or "default"
    except (ImportError, AttributeError, TypeError, ValueError):
        return "default"


def _write_yaml(path: Path, payload: dict) -> None:
    """yaml 의존성 없이 안전한 한정 YAML 작성기. 모든 값은 단순 스칼라/리스트."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []

    def _scalar(v):
        if v is None:
            return "null"
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return str(v)
        s = str(v)
        if any(ch in s for ch in ":#\n\"'") or s.startswith(("-", "?", "&", "*")):
            return json.dumps(s, ensure_ascii=False)
        return s

    def _emit(key, val, indent=0):
        pad = "  " * indent
        if isinstance(val, list):
            lines.append(f"{pad}{key}:")
            for item in val:
                if isinstance(item, dict):
                    lines.append(f"{pad}  -")
                    for k2, v2 in item.items():
                        _emit(k2, v2, indent + 2)
                else:
                    lines.append(f"{pad}  - {_scalar(item)}")
        elif isinstance(val, dict):
            lines.append(f"{pad}{key}:")
            for k2, v2 in val.items():
                _emit(k2, v2, indent + 1)
        else:
            lines.append(f"{pad}{key}: {_scalar(val)}")

    for k, v in payload.items():
        _emit(k, v)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_refresh_brief(candidate: dict, *, today: date) -> dict:
    """단일 후보의 refresh-curator 브리프 dict 를 만든다.

    형식은 blog-orchestrator 가 읽을 수 있도록 라우팅 브리프와 동일한 구조 +
    refresh 전용 필드(reason, change_hints).
    """
    title = candidate.get("clean_title") or candidate.get("title") or ""
    slug = f"{today.isoformat()}_refresh_{_slugify(title)}"
    category = _detect_category_from_title(title)
    return {
        "slug": slug,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "intent": "refresh",
        "keyword": title,
        "category": category,
        "persona_preset": "balanced",
        "original_url": candidate.get("url", ""),
        "original_posted_at": candidate.get("posted_at", ""),
        "age_days": candidate.get("age_days", 0),
        "refresh_count": candidate.get("refresh_count", 0),
        "change_hints": [
            "2026 기준 금액·소득 기준 갱신 확인",
            "최신 정책 변경 사항 1~2건 추가",
            "도입부 첫 문장에 '2026 최신판' 표시",
            "FAQ 1~2 문항을 최신 케이스로 교체",
        ],
        "human_approval_required": True,
    }


def build_queue_yaml(briefs: list[dict], *, today: date) -> dict:
    return {
        "date": today.isoformat(),
        "total": len(briefs),
        "briefs": [
            {
                "slug": b["slug"],
                "keyword": b["keyword"],
                "category": b["category"],
                "age_days": b["age_days"],
                "original_url": b["original_url"],
            }
            for b in briefs
        ],
    }


def run_refresh_trigger(
    *,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
    today: date | None = None,
) -> dict:
    """30일+ 글 후보 N개 → 브리프 + 큐 파일 생성. 발행/재생성 트리거 없음.

    Returns:
        {"date": today, "candidates": N, "briefs": [paths], "queue": Path}
    """
    today = today or date.today()
    try:
        from core.post_refresher import get_refresh_candidates
    except ImportError as exc:
        log.error(f"[refresh_trigger] post_refresher 임포트 실패: {exc}")
        return {"date": today, "candidates": 0, "briefs": [], "queue": None}

    candidates = get_refresh_candidates(max_count=max_candidates)
    if not candidates:
        log.info(f"[refresh_trigger] {today} 리프레시 후보 0건")
        return {"date": today, "candidates": 0, "briefs": [], "queue": None}

    REFRESH_BRIEFS_DIR.mkdir(parents=True, exist_ok=True)
    written_briefs: list[Path] = []
    brief_payloads: list[dict] = []
    for cand in candidates:
        payload = build_refresh_brief(cand, today=today)
        brief_path = REFRESH_BRIEFS_DIR / f"{payload['slug']}.yaml"
        _write_yaml(brief_path, payload)
        written_briefs.append(brief_path)
        brief_payloads.append(payload)

    queue_path = REFRESH_BRIEFS_DIR / f"{today.isoformat()}_queue.yaml"
    _write_yaml(queue_path, build_queue_yaml(brief_payloads, today=today))
    log.info(f"[refresh_trigger] {today} 후보 {len(candidates)}건 → 큐 {queue_path}")

    return {
        "date": today,
        "candidates": len(candidates),
        "briefs": written_briefs,
        "queue": queue_path,
    }


def _setup_cli_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="30일+ 글 리프레시 큐 적재")
    parser.add_argument("--max", type=int, default=DEFAULT_MAX_CANDIDATES,
                        help="하루 최대 후보 수 (기본 5)")
    parser.add_argument("--date", help="기준일 YYYY-MM-DD (테스트용)")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    if not args.quiet:
        _setup_cli_logging()

    today = date.fromisoformat(args.date) if args.date else date.today()
    result = run_refresh_trigger(max_candidates=args.max, today=today)
    if not args.quiet:
        print(f"date:        {result['date']}")
        print(f"candidates:  {result['candidates']}")
        if result["queue"]:
            print(f"queue:       {result['queue']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
