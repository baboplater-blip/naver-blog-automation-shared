# core/queue_injector.py — 큐→엑셀 발행 풀 브릿지 (2026-07-04)
"""기회 큐(수요 레이어)를 실제 발행 풀(엑셀)로 잇는 다리.

지금까지 `opportunity_score.rank_publish_queue` / 큐레이션 세트는 '무엇을 써야 하나'를
점수로 알려주는 추천 리스트일 뿐, 실제 발행기(`auto_scheduler.get_pending_posts`)는
엑셀 발행 풀에서만 제목을 읽었다. 두 트랙이 자동으로 안 이어져 있었다.

이 모듈이 그 다리를 놓는다:
  큐레이션 세트(사용자 승인 픽) 또는 rank_publish_queue
    → 3중 중복 제거(엑셀 풀·발행완료·보관본)
    → cadence 날짜 배정(하루 N편·주말 스킵)
    → 엑셀 발행 풀 하단에 'YYYY-MM-DD — 제목' 주입

원칙:
- 주입은 '발행 준비'까지만. 실제 발행(naver_uploader.upload)은 여전히 사용자 트리거.
- dry_run 기본 True — 명시적으로 dry_run=False 해야 엑셀을 건드린다.
- 순수 계획(build_injection_plan)과 엑셀 I/O(inject_to_excel) 분리 → 오프라인 테스트 가능.
- fail-open: 소스/네트워크 실패 시 빈 계획 반환, 발행 흐름 무영향.
"""
from __future__ import annotations

import csv
import os
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.logger import get_logger

log = get_logger(__name__)
BASE_DIR = Path(__file__).parent.parent
from config import profile as _nbaprofile
_DATA_DIR = _nbaprofile.app_data_root()


# 큐레이션 세트 CSV(사용자 승인 픽). _curate_keywords.py 산출물.
CURATED_CSV = _DATA_DIR / "logs" / "new_keyword_set_2026-07.csv"


# ─────────────────────────────────────────────────────
# 소스 로더 — 후보 [{title, keyword, score, category, priority, source}]
# ─────────────────────────────────────────────────────
def load_curated_titles(csv_path: Path | None = None) -> list[dict]:
    """큐레이션 세트 CSV → 복합 AEO 제목 후보. 없으면 빈 리스트(fail-open)."""
    path = Path(csv_path) if csv_path else CURATED_CSV
    if not path.exists():
        return []
    out: list[dict] = []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                title = (row.get("제안제목") or "").strip()
                if not title:
                    continue
                try:
                    pri = int(row.get("우선순위") or 3)
                except (TypeError, ValueError):
                    pri = 3
                try:
                    score = float(row.get("기회점수") or 0)
                except (TypeError, ValueError):
                    score = 0.0
                out.append({"title": title, "keyword": (row.get("핵심키워드") or "").strip(),
                            "score": score, "category": (row.get("주제군") or "").strip(),
                            "priority": pri, "source": "curated"})
    except (OSError, ValueError) as e:
        log.warning(f"[queue_injector] 큐레이션 CSV 로드 실패: {e}")
        return []
    # 우선순위(1<2<3) → 점수 내림차순
    out.sort(key=lambda r: (r["priority"], -r["score"]))
    return out


def load_queue_titles(*, min_score: float = 55, top_n: int = 40) -> list[dict]:
    """rank_publish_queue(자동 발굴) → 복합 제목(question_seeds) 매핑 후보.

    큐 항목의 keyword 를 question_seeds 의 'question'(복합 AEO 제목)으로 승격.
    매핑이 없으면 keyword 그대로 사용.
    """
    try:
        from core.opportunity_score import rank_publish_queue
        from core.performance_db import normalize_key
    except ImportError:
        return []
    try:
        queue = rank_publish_queue(top_n=max(top_n * 2, top_n), use_miner=False)
    except (OSError, ValueError, RuntimeError) as e:
        log.warning(f"[queue_injector] rank_publish_queue 실패: {e}")
        return []
    title_map = _seed_title_map()
    out: list[dict] = []
    for r in queue:
        if r.get("score", 0) < min_score:
            continue
        kw = r.get("keyword", "")
        if not kw:
            continue
        title = title_map.get(normalize_key(kw), kw)
        out.append({"title": title, "keyword": kw, "score": r.get("score", 0),
                    "category": r.get("category", ""), "priority": 2, "source": "queue"})
    return out[:top_n]


def _seed_title_map() -> dict:
    """question_seeds.json: normalize_key(seed) → question(복합 제목)."""
    from config import safe_load_json
    from core.performance_db import normalize_key
    data = safe_load_json(_DATA_DIR / "logs" / "question_seeds.json", default=list)
    out: dict[str, str] = {}
    if isinstance(data, list):
        for d in data:
            if not isinstance(d, dict):
                continue
            seed = d.get("seed") or ""
            q = d.get("question") or ""
            if seed and q:
                out[normalize_key(seed)] = q
    return out


# ─────────────────────────────────────────────────────
# 중복 제거 — 엑셀 풀 · 발행완료 · 보관본
# ─────────────────────────────────────────────────────
def _collect_existing_keys(excel_path: str | None = None,
                           sheet_name: str | None = None) -> set:
    """이미 파이프라인에 있는 제목 키(엑셀 풀 + 발행완료). 실패 시 가능한 만큼."""
    from core.performance_db import normalize_key
    keys: set = set()
    # 1) 엑셀 발행 풀
    try:
        from openpyxl import load_workbook
        from config.settings import KEYWORD_EXCEL_PATH, KEYWORD_PUBLISH_POOL_SHEET
        from config import brand as _brand
        xp = excel_path or _brand.excel_path() or KEYWORD_EXCEL_PATH
        sh = sheet_name or KEYWORD_PUBLISH_POOL_SHEET
        if os.path.exists(xp):
            wb = load_workbook(xp, read_only=True)
            if sh in wb.sheetnames:
                for row in wb[sh].iter_rows(values_only=True):
                    for val in row:
                        if val and isinstance(val, str):
                            k = normalize_key(val)
                            if k:
                                keys.add(k)
            wb.close()
    except (ImportError, OSError, ValueError, KeyError) as e:
        log.debug(f"[queue_injector] 엑셀 풀 키 수집 실패: {e}")
    # 2) 발행완료(performance_db)
    try:
        from core import performance_db as pdb
        data = pdb.load()
        for rec in data.get("posts", {}).values():
            for f in ("clean_title", "title", "keyword"):
                v = rec.get(f)
                if v:
                    k = normalize_key(v)
                    if k:
                        keys.add(k)
    except (ImportError, OSError, ValueError, KeyError) as e:
        log.debug(f"[queue_injector] 발행완료 키 수집 실패: {e}")
    return keys


def _is_drafted(title: str) -> bool:
    """이미 보관본(draft_ready)이 있는 제목인가."""
    try:
        from core.draft_store import find_draft_for_title
        return find_draft_for_title(title) is not None
    except (ImportError, OSError, ValueError):
        return False


# ─────────────────────────────────────────────────────
# cadence 날짜 배정
# ─────────────────────────────────────────────────────
def _walk_dates(count: int, *, per_day: int = 4, skip_weekends: bool = True,
                start_date: str | None = None) -> list[str]:
    """count 개 슬롯에 날짜 배정(하루 per_day, 주말 스킵). 'YYYY-MM-DD' 리스트."""
    if count <= 0:
        return []
    if start_date:
        cur = date.fromisoformat(start_date)
    else:
        cur = date.today() + timedelta(days=1)   # 기본 내일부터
    out: list[str] = []
    while len(out) < count:
        if skip_weekends and cur.weekday() >= 5:   # 5=토, 6=일
            cur += timedelta(days=1)
            continue
        for _ in range(per_day):
            if len(out) >= count:
                break
            out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


# ─────────────────────────────────────────────────────
# 계획 수립(순수) — 소스 → 중복제거 → 날짜배정
# ─────────────────────────────────────────────────────
def build_injection_plan(*, source: str = "curated", min_score: float = 55,
                         top_n: int = 40, per_day: int = 4, skip_weekends: bool = True,
                         start_date: str | None = None,
                         candidates: list[dict] | None = None,
                         existing_keys: set | None = None) -> dict:
    """주입 계획 수립. 엑셀 미변경(순수).

    source: 'curated'(큐레이션 CSV) | 'queue'(자동 발굴) | 'both'
    candidates/existing_keys: 주입 가능(테스트용). None 이면 라이브 로드.
    반환: {"plan":[{date,title,keyword,score,category,source}], "skipped":[...],
           "total": n, "sources": {...}}
    """
    from core.performance_db import normalize_key

    if candidates is None:
        cands: list[dict] = []
        if source in ("curated", "both"):
            cands += load_curated_titles()
        if source in ("queue", "both"):
            cands += load_queue_titles(min_score=min_score, top_n=top_n)
    else:
        cands = list(candidates)

    if existing_keys is None:
        existing_keys = _collect_existing_keys()

    kept: list[dict] = []
    skipped: list[dict] = []
    seen: set = set()
    for c in cands:
        title = (c.get("title") or "").strip()
        if not title:
            continue
        key = normalize_key(title)
        if not key or key in seen:
            continue
        seen.add(key)
        reason = None
        if key in existing_keys:
            reason = "이미 발행풀/발행완료에 있음"
        elif _is_drafted(title):
            reason = "이미 보관본 있음"
        if reason:
            skipped.append({**c, "reason": reason})
            continue
        kept.append(dict(c))

    kept = kept[:top_n]
    dates = _walk_dates(len(kept), per_day=per_day, skip_weekends=skip_weekends,
                        start_date=start_date)
    for item, d in zip(kept, dates):
        item["date"] = d
        item["entry"] = f"{d} — {item['title']}"

    src_counts: dict[str, int] = {}
    for k in kept:
        src_counts[k.get("source", "?")] = src_counts.get(k.get("source", "?"), 0) + 1

    return {"plan": kept, "skipped": skipped, "total": len(kept),
            "sources": src_counts}


# ─────────────────────────────────────────────────────
# 엑셀 I/O — 발행 풀 하단에 주입
# ─────────────────────────────────────────────────────
def inject_to_excel(plan: list[dict], *, excel_path: str | None = None,
                    sheet_name: str | None = None, dry_run: bool = True,
                    position: str = "top", backup: bool = True) -> dict:
    """계획의 제목을 엑셀 발행 풀에 'YYYY-MM-DD — 제목' 셀로 주입.

    position='top'(기본): 최상단 삽입 — 발행기 우선순위 정렬에서 앞서 픽업(개선 신규를
        기존 백로그보다 먼저 발행). 마감키워드 주입(inject_urgent_to_excel)과 같은 규약.
    position='bottom': 하단 append(우선순위 낮음).
    노란색(완료) 아닌 기본 셀로 넣어 발행기가 pending 으로 픽업. dry_run 이면 미기록.
    backup=True: 실주입 전 logs/backup/ 에 타임스탬프 사본(되돌림 보장).
    """
    if not plan:
        return {"injected": 0, "dry_run": dry_run, "error": None}
    try:
        from openpyxl import load_workbook
        from config.settings import KEYWORD_EXCEL_PATH, KEYWORD_PUBLISH_POOL_SHEET
        from config import brand as _brand
    except ImportError as e:
        return {"injected": 0, "dry_run": dry_run, "error": f"import:{e}"}

    xp = excel_path or _brand.excel_path() or KEYWORD_EXCEL_PATH
    sh = sheet_name or KEYWORD_PUBLISH_POOL_SHEET
    if not os.path.exists(xp):
        return {"injected": 0, "dry_run": dry_run, "error": f"엑셀 없음:{xp}"}

    if dry_run:
        return {"injected": len(plan), "dry_run": True, "error": None,
                "position": position, "entries": [p.get("entry") for p in plan]}

    backup_path = None
    if backup:
        backup_path = _backup_excel(xp)

    try:
        wb = load_workbook(xp)
        if sh not in wb.sheetnames:
            return {"injected": 0, "dry_run": False, "error": f"시트 없음:{sh}"}
        ws = wb[sh]
        if position == "bottom":
            start_row = ws.max_row + 1
            for i, p in enumerate(plan):
                ws.cell(row=start_row + i, column=1, value=p["entry"])
        else:   # top — 기존 행을 아래로 밀고 최상단에 삽입
            ws.insert_rows(1, amount=len(plan))
            for i, p in enumerate(plan, 1):
                ws.cell(row=i, column=1, value=p["entry"])
        wb.save(xp)
    except (OSError, ValueError, KeyError) as e:
        return {"injected": 0, "dry_run": False, "error": f"쓰기 실패:{e}",
                "backup": backup_path}

    log.info(f"[queue_injector] 발행 풀에 {len(plan)}개 주입 ({position})")
    return {"injected": len(plan), "dry_run": False, "error": None,
            "position": position, "backup": backup_path,
            "entries": [p.get("entry") for p in plan]}


def _backup_excel(xp: str) -> str | None:
    """엑셀을 logs/backup/ 에 타임스탬프 사본으로 백업. 경로 반환(실패 시 None)."""
    try:
        import shutil
        from datetime import datetime
        src = Path(xp)
        bdir = _DATA_DIR / "logs" / "backup"
        bdir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = bdir / f"{src.stem}_inject_{ts}{src.suffix}"
        shutil.copy2(src, dest)
        return str(dest)
    except OSError as e:
        log.warning(f"[queue_injector] 엑셀 백업 실패: {e}")
        return None


# ─────────────────────────────────────────────────────
# 리포트 + 원커맨드
# ─────────────────────────────────────────────────────
def render_report(result: dict, io_result: dict, date_str: str = "") -> str:
    plan = result["plan"]
    lines = [f"# 발행 큐 → 엑셀 주입 계획 {date_str}".rstrip(), ""]
    mode = "🟡 미리보기(dry-run)" if io_result.get("dry_run") else "✅ 실주입 완료"
    lines += [f"> 모드: **{mode}** · 주입 {result['total']}편 · 제외 {len(result['skipped'])}편",
              "> 발행 풀에 넣기만 함 — **실제 발행은 사용자 트리거(발행기 다음 실행에 픽업)**.", ""]
    if result.get("sources"):
        src = " · ".join(f"{k} {v}" for k, v in result["sources"].items())
        lines += [f"소스: {src}", ""]

    lines += ["## 주입 계획(날짜순)", "",
              "| 발행일 | 제목 | 키워드 | 점수 | 소스 |", "|---|---|---|---|---|"]
    for p in plan:
        lines.append(f"| {p.get('date','')} | {p['title']} | {p.get('keyword','')} "
                     f"| {p.get('score',0):.0f} | {p.get('source','')} |")

    if result["skipped"]:
        lines += ["", "## 제외(중복)", "", "| 제목 | 사유 |", "|---|---|"]
        for s in result["skipped"][:30]:
            lines.append(f"| {s.get('title','')} | {s.get('reason','')} |")

    lines += ["", "## 다음",
              "1. dry-run 확인 후 `inject_to_excel(plan, dry_run=False)` 로 실주입.",
              "2. 발행기(run_pipeline)가 다음 실행에 pending 으로 픽업 → 생성·QA.",
              "3. 실발행은 사용자가 GUI ▶ 또는 승인으로 트리거.", ""]
    return "\n".join(lines)


def run_queue_injection(*, source: str = "curated", per_day: int = 4,
                        dry_run: bool = True, top_n: int = 40,
                        start_date: str | None = None) -> dict:
    """원커맨드: 계획 수립 → (옵션)주입 → 리포트 저장. 기본 dry_run."""
    result = build_injection_plan(source=source, per_day=per_day, top_n=top_n,
                                  start_date=start_date)
    io_result = inject_to_excel(result["plan"], dry_run=dry_run)
    try:
        today = date.today().isoformat()
        try:  # 프로필별 reports/ (2026-07-19 격리 감사)
            from config import profile as _nbaprofile
            reports = _nbaprofile.reports_dir()
        except Exception:  # noqa: BLE001
            reports = BASE_DIR / "reports"
        reports.mkdir(parents=True, exist_ok=True)
        md = render_report(result, io_result, date_str=f"({today})")
        (reports / f"{today}_queue_injection.md").write_text(md, encoding="utf-8")
    except OSError as e:
        log.warning(f"[queue_injector] 리포트 저장 실패: {e}")
    return {"result": result, "io": io_result}


# ─────────────────────────────────────────────────────
# 보관본 백로그 → 엑셀 발행 풀 (2026-07-12)
# ─────────────────────────────────────────────────────
# 배경: 7/4 발행 계획(run_publish_plan, elite 497편)은 reports/ 에 md 보고서만
# 만들고 엑셀 풀에는 주입하지 않아, 발행기(get_pending_posts)가 그 제목들을
# 영영 픽업하지 못했다(2026-07-12 실측: 오늘 큐 20개 중 보관본 매칭 0).
# 이 경로가 그 다리를 놓는다: triage 발행 버킷 → cadence 날짜 → 엑셀 주입.
# 주입된 제목은 run_pipeline 의 보관본 lookup(find_draft_for_title)에 걸려
# 신규 생성 없이 보관본 그대로 발행 준비된다. 실제 발행은 여전히 사용자 트리거.
def _draft_slug_for(title: str) -> str | None:
    """title 에 매칭되는 draft_ready 보관본 슬러그. 없으면 None."""
    try:
        from core.draft_store import find_draft_for_title
        hit = find_draft_for_title(title)
        return hit.get("slug") if hit else None
    except (ImportError, OSError, ValueError):
        return None


def build_draft_backlog_plan(*, per_day: int = 4, skip_weekends: bool = True,
                             schedule: list[dict] | None = None,
                             existing_keys: set | None = None) -> dict:
    """triage 발행 버킷(draft_ready elite)을 엑셀 주입 계획으로 변환. 엑셀 미변경(순수).

    build_injection_plan 과 반대로 '보관본이 있는 제목'만 살린다 — 주입 후 발행기가
    보관본을 그대로 쓰는 것이 목적이므로, 매칭 안 되는 제목을 넣으면 신규 생성으로
    빠져 취지가 무너진다(매칭 실패는 skipped 로 보고).

    schedule/existing_keys: 주입 가능(테스트용). None 이면 라이브 로드.
    반환 형식은 build_injection_plan 과 동일.
    """
    from core.performance_db import normalize_key

    if schedule is None:
        try:
            from core.draft_triage import triage_drafts, build_publish_plan
            plan = build_publish_plan(triage_drafts(), per_day=per_day,
                                      skip_weekends=skip_weekends)
            schedule = plan.get("schedule", [])
        except (ImportError, OSError, ValueError, KeyError) as e:
            log.warning(f"[queue_injector] 발행 계획 수립 실패(fail-open): {e}")
            schedule = []

    if existing_keys is None:
        existing_keys = _collect_existing_keys()

    from core import draft_store as _ds

    kept: list[dict] = []
    skipped: list[dict] = []
    seen: set = set()
    for s in schedule:
        title = (s.get("title") or "").strip()
        # 큐 제목은 post_data 제목이 아니라 **슬러그 키 복원본**을 쓴다.
        # 보관본 슬러그는 원래 엑셀 키워드로 만들어졌고 post_data 제목은 생성된
        # 최종 제목이라 서로 다르다(실측: 1,164편 중 키 일치 0). 발행기 lookup
        # (find_draft_for_title)은 큐 제목을 슬러그 키로 정규화해 비교하므로,
        # 키를 공백 제목으로 복원해 넣어야 라운드트립이 성립한다. 발행 시 실제
        # 제목은 어차피 보관본 post_data["title"](최종 제목)이 쓰인다.
        slug_key = _ds._key_from_slug(s.get("slug") or "") or ""
        queue_title = slug_key.replace("-", " ").strip() or title
        if not queue_title:
            continue
        key = normalize_key(queue_title)
        if not key or key in seen:
            continue
        seen.add(key)
        item = {"title": queue_title, "display_title": title, "keyword": "",
                "score": s.get("score", 0), "category": s.get("category", ""),
                "priority": 1, "source": "draft_backlog",
                "date": s.get("date", ""), "slug": s.get("slug", "")}
        if key in existing_keys or (title and normalize_key(title) in existing_keys):
            skipped.append({**item, "reason": "이미 발행풀/발행완료에 있음"})
            continue
        matched_slug = _draft_slug_for(queue_title)
        if not matched_slug:
            skipped.append({**item, "reason": "보관본 매칭 실패(주입 시 신규 생성으로 빠짐)"})
            continue
        item["slug"] = matched_slug
        item["entry"] = f"{item['date']} — {queue_title}" if item["date"] else queue_title
        kept.append(item)

    return {"plan": kept, "skipped": skipped, "total": len(kept),
            "sources": {"draft_backlog": len(kept)}}


def run_draft_backlog_injection(*, per_day: int = 4, dry_run: bool = True,
                                position: str = "bottom") -> dict:
    """원커맨드: 보관본 백로그 계획 → (옵션)엑셀 주입 → 리포트 저장. 기본 dry_run.

    position 기본 'bottom' — 마감키워드·개선 신규의 상단 주입 규약(top)을 침범하지
    않는다. 실제 픽업 순서는 어차피 트렌드 정렬이 결정한다.
    """
    result = build_draft_backlog_plan(per_day=per_day)
    io_result = inject_to_excel(result["plan"], dry_run=dry_run, position=position)
    try:
        today = date.today().isoformat()
        try:  # 프로필별 reports/ (2026-07-19 격리 감사)
            from config import profile as _nbaprofile
            reports = _nbaprofile.reports_dir()
        except Exception:  # noqa: BLE001
            reports = BASE_DIR / "reports"
        reports.mkdir(parents=True, exist_ok=True)
        md = render_report(result, io_result, date_str=f"({today})")
        (reports / f"{today}_draft_backlog_injection.md").write_text(md, encoding="utf-8")
    except OSError as e:
        log.warning(f"[queue_injector] 리포트 저장 실패: {e}")
    return {"result": result, "io": io_result}


if __name__ == "__main__":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    out = run_queue_injection(dry_run=True)
    r, io_r = out["result"], out["io"]
    print(f"주입 계획: {r['total']}편 (제외 {len(r['skipped'])}) · 모드 "
          f"{'dry-run' if io_r['dry_run'] else '실주입'} · 소스 {r['sources']}")
    for p in r["plan"]:
        print(f"  {p.get('date','')}  [{p.get('source','')}] {p['title'][:60]}")
