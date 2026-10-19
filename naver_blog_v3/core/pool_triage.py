# core/pool_triage.py — 발행풀(엑셀) 선별 정리 (2026-07-04)
"""엑셀 발행풀의 pending 키워드 제목을 선별해 니치 이탈 저가치 항목을 제외한다.

draft_triage 는 '생성된 본문'을 품질×수요로 분류하지만, 발행풀은 아직 제목만 있으므로
(본문 없음) 수요·니치 적합만으로 판단한다. 실측 결과 발행풀은 draft 백로그(50% 폐기)와
달리 대체로 정상 복합 answer 제목이라 위기가 아니다 — 진짜 폐기 대상은 블로그의
복지·금융 권위 니치를 벗어난 **앱·기기 트러블슈팅 how-to**(카카오톡 친구추가 안될때,
블루투스 페어링, 유튜브 설정 등) 소수(~1%)뿐.

⚠️ 수요 heuristic(draft_triage._demand_score)은 발행풀에 그대로 못 쓴다:
   '잔액조회·총정리·안될때'를 감점하는데 문화누리카드엔 이게 14만 고수요 쿼리다
   (실제로 그 heuristic은 큐레이션 픽 '문화누리카드 잔액조회'를 -4점 처리했다).
   그래서 임계값 sweep 대신 **명시적 니치이탈 패턴**만 정밀 폐기한다.

제외 메커니즘 = excluded_titles.json (엑셀 무변경, 셀 단위, is_excluded 가 pending 사전 차감).
완전 되돌림: 원장 logs/pool_retired.json + restore_pool_retired() (remove_excluded).
실제 폐기(add_excluded)는 dry_run=False 명시 필요. 읽기 전용이 기본.
"""
from __future__ import annotations

import os
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.logger import get_logger

log = get_logger(__name__)
BASE_DIR = Path(__file__).parent.parent
from config import profile as _nbaprofile
_DATA_DIR = _nbaprofile.app_data_root()

RETIRE_LEDGER = _DATA_DIR / "logs" / "pool_retired.json"
_DATE_PREFIX = re.compile(r"^\s*\d{4}-\d{2}-\d{2}\s*[—–\-]?\s*")

# ── 니치 이탈 저가치 패턴 (블로그 복지·금융 권위 밖) ──
# 트러블슈팅 how-to = "X 안 될 때/실패/오류" 장르(지속 수요 낮음, 권위 희석)
_TROUBLE = ("안 될 때", "안될 때", "안될때", "인식 안", "등록 안", "적립 안", "로그인 안",
            "결제 실패", "결제실패", "오류코드", "친구 추가 안", "접속 안", "실행 안",
            "페어링 안", "한쪽만", "임시 해결", "캐시 삭제", "조회 안", "연결 안", "충전 안")
# 순수 기기·앱 주제(니치 완전 이탈)
_DEVICE = ("블루투스", "와이파이", "와이 파이", "유튜브", "틱톡", "인스타", "스마트폰 설정",
           "알림 정리", "알림 끄", "구독 끄", "자동충전 끄", "자동 연결 끄", "화면 밝기",
           "글자 크기 키우", "공공 와이파이", "지문인식")
# 폐기 보류(보존) 신호 — 금융 비교·대출 등 정상 고수요
_KEEP = ("vs", "비교", "대출", "한도", "금리", "비상금", "환율", "신용")


def _strip_date(t: str) -> str:
    return _DATE_PREFIX.sub("", t or "").strip()


def _is_core(t: str) -> bool:
    from core.draft_triage import _CORE_TOPICS
    tt = t.replace(" ", "")
    return any(c.replace(" ", "") in tt for c in _CORE_TOPICS)


def _protected_titles() -> set:
    """폐기에서 항상 보호할 제목(정규화 키) — 큐레이션 주입분."""
    keys: set = set()
    try:
        from core.queue_injector import load_curated_titles
        from core.performance_db import normalize_key
        for r in load_curated_titles():
            keys.add(normalize_key(r["title"]))
    except (ImportError, OSError, ValueError, KeyError):
        pass
    return keys


def is_retire_target(title: str, *, protected: set | None = None) -> bool:
    """title 이 니치이탈 저가치(정밀 폐기 대상)인가.

    폐기 = (트러블슈팅 or 기기·앱 주제) AND 보존신호 없음 AND (기기주제 or 비코어)
           AND 큐레이션 주입 아님.
    """
    from core.performance_db import normalize_key
    t = _strip_date(title)
    if not t:
        return False
    if protected is None:
        protected = _protected_titles()
    if normalize_key(t) in protected:
        return False                       # 큐레이션 픽 보호
    if any(k in t for k in _KEEP):
        return False                       # 금융 비교·대출 등 보존
    hit_trouble = any(k in t for k in _TROUBLE)
    hit_device = any(k in t for k in _DEVICE)
    if not (hit_trouble or hit_device):
        return False
    if _is_core(t) and not hit_device:
        return False                       # 니치 내 오류대응(바우처 등)은 보존
    return True


# ─────────────────────────────────────────────────────
# 발행풀 읽기 + 분류
# ─────────────────────────────────────────────────────
def read_pool_pending(excel_path: str | None = None,
                      sheet_name: str | None = None) -> list[str]:
    """발행풀 pending(노란색=완료 제외) 날짜형 제목 리스트(원본 셀값)."""
    try:
        from openpyxl import load_workbook
        from config.settings import KEYWORD_EXCEL_PATH, KEYWORD_PUBLISH_POOL_SHEET
        from config import brand as _brand
    except ImportError:
        return []
    xp = excel_path or _brand.excel_path() or KEYWORD_EXCEL_PATH
    sh = sheet_name or KEYWORD_PUBLISH_POOL_SHEET
    if not os.path.exists(xp):
        return []
    out: list[str] = []
    wb = load_workbook(xp, read_only=True)
    if sh not in wb.sheetnames:
        wb.close()
        return []
    for row in wb[sh].iter_rows():
        for cell in row:
            v = str(cell.value or "").strip()
            if len(v) < 12 or v[4:5] != "-":
                continue
            try:
                rgb = cell.fill.fgColor.rgb if cell.fill and cell.fill.fgColor else ""
                if "FFFF00" in str(rgb):
                    continue
            except (AttributeError, TypeError):
                pass
            out.append(v)
    wb.close()
    return out


def triage_pool(*, titles: list[str] | None = None) -> dict:
    """발행풀을 정밀 분류. 반환: {total, retire:[제목], keep, sample}."""
    pend = titles if titles is not None else read_pool_pending()
    protected = _protected_titles()
    retire = sorted({_strip_date(t) for t in pend
                     if is_retire_target(t, protected=protected)})
    return {"total": len(pend), "retire": retire, "keep": len(pend) - len(retire),
            "protected": len(protected)}


# ─────────────────────────────────────────────────────
# 폐기(제외) 적용 + 복구 — 되돌림 가능
# ─────────────────────────────────────────────────────
def apply_pool_retire(result: dict | None = None, *, dry_run: bool = True,
                      limit: int | None = None) -> dict:
    """retire 제목을 excluded_titles 에 제외(엑셀 무변경). dry_run 이면 미적용.

    적용 시 원장(logs/pool_retired.json)에 기록 → restore_pool_retired 로 복구.
    """
    result = result or triage_pool()
    retire = result.get("retire", [])
    if limit:
        retire = retire[:limit]
    if dry_run:
        return {"retired": len(retire), "dry_run": True, "titles": retire}

    from core.excluded_titles import add_excluded
    from config import atomic_write_json, safe_load_json
    done = []
    for t in retire:
        add_excluded(t, reason="pool_off_niche")
        done.append(t)
    # 원장 병합 저장(복구용)
    prev = safe_load_json(RETIRE_LEDGER, default=dict)
    prev_titles = set(prev.get("titles", []) if isinstance(prev, dict) else [])
    prev_titles.update(done)
    try:
        atomic_write_json(RETIRE_LEDGER, {
            "titles": sorted(prev_titles), "count": len(prev_titles),
            "updated_at": datetime.now().isoformat(timespec="seconds")})
    except OSError as e:
        log.warning(f"[pool_triage] 원장 저장 실패: {e}")
    log.info(f"[pool_triage] 발행풀 니치이탈 {len(done)}편 제외(복구 가능)")
    return {"retired": len(done), "dry_run": False, "titles": done}


def restore_pool_retired(*, limit: int | None = None) -> dict:
    """원장의 제외 제목을 되돌린다(excluded_titles 에서 제거)."""
    from core.excluded_titles import remove_excluded
    from config import atomic_write_json, safe_load_json
    ledger = safe_load_json(RETIRE_LEDGER, default=dict)
    titles = list(ledger.get("titles", []) if isinstance(ledger, dict) else [])
    if limit:
        titles = titles[:limit]
    restored = [t for t in titles if remove_excluded(t)]
    remain = sorted(set((ledger.get("titles", []) if isinstance(ledger, dict) else []))
                    - set(restored))
    try:
        atomic_write_json(RETIRE_LEDGER, {
            "titles": remain, "count": len(remain),
            "updated_at": datetime.now().isoformat(timespec="seconds")})
    except OSError as e:
        log.warning(f"[pool_triage] 원장 갱신 실패: {e}")
    log.info(f"[pool_triage] 발행풀 제외 {len(restored)}편 복구")
    return {"restored": len(restored), "remaining": len(remain)}


# ─────────────────────────────────────────────────────
# 리포트
# ─────────────────────────────────────────────────────
def render_report(result: dict, applied: dict | None = None, date_str: str = "") -> str:
    lines = [f"# 발행풀 선별 정리 {date_str}".rstrip(), ""]
    pct = (len(result["retire"]) * 100 // result["total"]) if result["total"] else 0
    lines += [
        f"> 발행풀 pending **{result['total']}편** · 니치이탈 폐기후보 "
        f"**{len(result['retire'])}편({pct}%)** · 보호(큐레이션) {result.get('protected',0)}키",
        "> ⚠️ 발행풀은 draft 백로그와 달리 대체로 정상 — heuristic 임계값 sweep은 큐레이션 "
        "픽(문화누리카드 등)을 오폐기하므로 **명시적 니치이탈 패턴만** 정밀 폐기.", "",
        "## 폐기 후보 (앱·기기 트러블슈팅 = 복지·금융 니치 이탈)", "",
    ]
    for t in result["retire"]:
        lines.append(f"- {t}")
    if applied is not None:
        mode = "🟡 미리보기" if applied.get("dry_run") else "✅ 제외 적용"
        lines += ["", f"## 적용: {mode} — {applied.get('retired',0)}편",
                  "> excluded_titles 로 pending 사전 차감(엑셀 무변경). "
                  "복구: `pool_triage.restore_pool_retired()`."]
    lines += ["", "## 결론",
              "- 발행풀은 정리 위기가 아님 — 폐기는 니치이탈 소수만(권위 희석·생성낭비 방지).",
              "- 나머지 정상 제목은 유지, 큐레이션 주입 개선 신규가 최상단에서 먼저 발행.", ""]
    return "\n".join(lines)


def run_pool_triage(*, dry_run: bool = True) -> dict:
    """원커맨드: 분류 → (옵션)제외 → 리포트 저장."""
    result = triage_pool()
    applied = apply_pool_retire(result, dry_run=dry_run)
    try:
        from datetime import date
        today = date.today().isoformat()
        try:  # 프로필별 reports/ (2026-07-19 격리 감사)
            from config import profile as _nbaprofile
            reports = _nbaprofile.reports_dir()
        except Exception:  # noqa: BLE001
            reports = BASE_DIR / "reports"
        reports.mkdir(parents=True, exist_ok=True)
        (reports / f"{today}_pool_triage.md").write_text(
            render_report(result, applied, date_str=f"({today})"), encoding="utf-8")
    except OSError as e:
        log.warning(f"[pool_triage] 리포트 저장 실패: {e}")
    return {"result": result, "applied": applied}


if __name__ == "__main__":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    out = run_pool_triage(dry_run=True)
    r = out["result"]
    print(f"발행풀 pending {r['total']} | 니치이탈 폐기후보 {len(r['retire'])}편 "
          f"| 보호 {r['protected']}키")
    for t in r["retire"]:
        print("  ·", t[:56])
