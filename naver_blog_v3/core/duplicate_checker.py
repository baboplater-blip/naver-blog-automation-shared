# core/duplicate_checker.py
# ② 중복 포스팅 감지 & 차단
# 이미 발행된 글과 유사도 80% 이상이면 자동 Skip
from __future__ import annotations

import json, os, re, sys, threading
from datetime import datetime
from pathlib import Path

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE_DIR  = Path(__file__).parent.parent
from config import profile as _nbaprofile
_DATA_DIR = _nbaprofile.app_data_root()

DB_PATH   = _DATA_DIR / "logs" / "post_history.json"
try:
    from config.settings import DUPLICATE_THRESHOLD as THRESHOLD, POST_HISTORY_MAX
except (ImportError, AttributeError):
    THRESHOLD = 0.80
    POST_HISTORY_MAX = 500

# 중복검사 인덱스(post_history.json) 트림 안전 밸브의 하한. POST_HISTORY_MAX 가
# 이보다 작아도(옛 기본 500) 최소 이만큼은 보존해 "501편 사각지대"를 없앤다.
# 이 상한은 파일이 병리적으로 비대해질 때만 발동하는 방어선이다(정상 볼륨 무영향).
_HISTORY_CAP_FLOOR = 100_000

from config.logger import get_logger
log = get_logger(__name__)

_db_lock  = threading.Lock()

# 본문에서 추출할 키워드 최대 개수 — 히스토리 레코드 비대화 방지 상한.
# 정렬(빈도 무관, set) 후 앞쪽만 저장하면 되므로 상한만 두면 충분하다.
_BODY_KW_LIMIT = 200

# 한국어 단어 끝의 흔한 조사·어미 (긴 것부터 매칭). quality_validator._TRAIL_PATTERNS
# 와 동일 목적의 경량 사본 — 모듈 간 결합을 피하려 임포트하지 않고 이 파일에 둔다.
_TRAIL_PATTERNS = sorted([
    "이라면", "다면서", "라면서", "이라도", "라도", "이면", "다면", "라면",
    "에서", "까지", "부터", "보다", "처럼", "으로", "이라는", "라는",
    "을까", "할까", "될까", "일까", "까요", "어요", "아요", "네요", "지요",
    "이가", "이는", "이를", "이의",
    "이", "가", "을", "를", "은", "는", "에", "의", "와", "과", "도", "만", "면", "로",
], key=len, reverse=True)

# 골격어(불용어) — 어떤 정부지원 글에나 공통으로 나오는 뼈대 단어. 본문끼리 비교할 때
# 이 단어들이 유사도를 인위적으로 부풀려(모든 글이 '신청·자격·금액'을 말함) 서로 다른
# 제도 글도 겹쳐 보이거나, 반대로 임계를 못 넘게 만든다. 본문 Jaccard 전에 제거해
# 제도 고유어(에너지바우처·긴급생계지원 등)만 신호로 남긴다. (개선안 #3)
_SKELETON_STOPWORDS = frozenset({
    "신청", "자격", "금액", "방법", "대상", "지원", "조건", "기준", "서류",
    "혜택", "안내", "정리", "총정리", "확인", "발급", "접수", "지급", "기간",
    "한도", "소득", "가구", "온라인", "방문", "정부", "지원금", "제도", "경우",
    "필요", "가능", "이용", "준비", "관련", "이상", "이하", "문의", "홈페이지",
})


def _strip_josa(word: str) -> str:
    """한국어 단어 끝의 조사·어미를 제거한다 (단순 휴리스틱).

    예: '지원금이'→'지원금', '월세를'→'월세'.
    과도한 stemming 을 피하려고 접미 조사 1개만 제거하며,
    제거 후 결과가 2자 미만이면 원본을 유지한다.
    """
    for trail in _TRAIL_PATTERNS:
        if word.endswith(trail) and len(word) - len(trail) >= 2:
            return word[:-len(trail)]
    return word


# ─────────────────────────────────────────────────────
# 히스토리 DB 로드 / 저장
# ─────────────────────────────────────────────────────
def _load_db() -> list:
    from config import safe_load_json
    DB_PATH.parent.mkdir(exist_ok=True)
    return safe_load_json(DB_PATH, default=list)

def _save_db(records: list):
    from config import atomic_write_json
    atomic_write_json(DB_PATH, records)


def _effective_history_cap() -> int:
    """트림에 실제로 적용할 상한. 0(또는 음수)이면 무제한(트림 안 함).

    옛 기본값(POST_HISTORY_MAX=500)은 중복 비교 인덱스를 잘라 사각지대를 만들었다.
    이제 그 값을 '표시용'이 아니라 '안전 밸브'로 재해석해, 최소 _HISTORY_CAP_FLOOR
    까지는 항상 보존한다. 사용자가 명시적으로 0 이하로 설정하면 완전 무제한.
    """
    try:
        cfg = int(POST_HISTORY_MAX)
    except (TypeError, ValueError):
        cfg = 500
    if cfg <= 0:
        return 0                      # 무제한
    return max(cfg, _HISTORY_CAP_FLOOR)


# ─────────────────────────────────────────────────────
# 키워드 추출 (한국어 형태소 근사)
# ─────────────────────────────────────────────────────
def _keywords(text: str) -> set:
    """2글자 이상 한국어/영어 단어 추출 (흔한 조사·어미는 접미 제거).

    '지원금이/지원금을/지원금은'처럼 조사만 다른 토큰이 서로 다른 단어로
    잡히지 않도록 _strip_josa 로 접미 조사를 떼어낸 뒤 집합을 만든다.
    조사 제거 후 다시 2글자 미만이 되는 토큰은 제외한다.
    """
    text = re.sub(r'[\d\s\W]+', ' ', text)
    words = text.split()
    result = set()
    for word in words:
        if len(word) < 2:
            continue
        stemmed = _strip_josa(word)
        if len(stemmed) >= 2:
            result.add(stemmed)
    return result


# ─────────────────────────────────────────────────────
# 유사도 계산 (Jaccard similarity on keywords)
# ─────────────────────────────────────────────────────
def _jaccard(kw_a: set, kw_b: set) -> float:
    """두 키워드 집합의 Jaccard 유사도. 한쪽이라도 비면 0.0."""
    if not kw_a or not kw_b:
        return 0.0
    return len(kw_a & kw_b) / len(kw_a | kw_b)


def _similarity(title_a: str, title_b: str) -> float:
    return _jaccard(_keywords(title_a), _keywords(title_b))


# ─────────────────────────────────────────────────────
# 중복 검사 메인 함수
# ─────────────────────────────────────────────────────
def check_duplicate(title: str, content: str = "") -> tuple[bool, str, float]:
    """
    제목(+선택적 본문)을 히스토리와 비교해 중복 여부를 판정한다.

    Args:
        title:   비교할 글 제목.
        content: (선택) 본문. 주어지면 본문 키워드까지 함께 비교해
                 "제목만 다르고 본문이 거의 같은" 중복도 잡는다. 생략하면
                 기존과 동일하게 제목만 비교한다(하위 호환).

    비교 방식:
        content 가 주어지고 레코드에 body_kw 가 있으면
        (제목 키워드 ∪ 본문 키워드) 대 (레코드 clean_title 키워드 ∪ body_kw)
        의 Jaccard 를 계산한다. 단순 합집합만 쓰면 긴 본문이 제목 신호를
        희석하므로, 제목만 Jaccard 와 결합 Jaccard 중 **max** 를 최종 점수로
        삼아 어느 한쪽이 강하게 겹쳐도 잡히게 한다. content 가 없거나
        레코드에 body_kw 가 없으면 자연스럽게 제목-기반 비교로 폴백한다.

    Returns:
        (is_duplicate: bool, matched_title: str, similarity: float)
    """
    clean = re.sub(r'^\d{4}-\d{2}-\d{2} — ', '', title)
    title_kw = _keywords(clean)
    body_kw = _keywords(content) if content else set()
    # 본문끼리 비교할 때는 골격어를 제외한 집합을 쓴다(제도 고유어만 남김).
    body_kw_lean = body_kw - _SKELETON_STOPWORDS

    with _db_lock:
        records = _load_db()

    # DB 가 비어 있는데 파일이 실제로 존재하면 로딩 실패/손상 가능성 →
    # 중복검사가 조용히 무력화되는 상황이므로 경고를 남긴다.
    # (파일이 없는 정상 초기 상태와는 구분한다.)
    if not records and DB_PATH.exists():
        log.warning(
            "⚠️ 중복검사 DB가 비어있음/로딩 실패 — 중복검사가 무력화될 수 "
            "있음 (post_history 점검 필요)"
        )

    best_match = ""
    best_score = 0.0

    for rec in records:
        rec_title_kw = _keywords(rec.get("clean_title", ""))
        title_score = _jaccard(title_kw, rec_title_kw)

        # 본문 비교: 양쪽 모두 본문 키워드가 있을 때만 의미가 있다.
        rec_body_kw = rec.get("body_kw")
        if body_kw and rec_body_kw:
            # 본문끼리 직접 Jaccard — "제목만 다르고 본문이 거의 같은" 중복을
            # 잡는 핵심 신호(본문 동일이면 1.0). 결합(제목∪본문) 방식은 서로
            # 다른 제목 단어가 union 을 키워 동일 본문도 희석시키므로 단독으론
            # 부족하다. 제목·본문·결합 세 신호 중 가장 강한 쪽을 채택한다.
            # 본문끼리는 골격어를 뺀 lean 집합으로 비교해, 모든 정부지원 글이
            # 공유하는 뼈대어(신청·자격·금액…)가 유사도를 왜곡하지 않게 한다.
            rec_body_set = set(rec_body_kw)
            rec_body_lean = rec_body_set - _SKELETON_STOPWORDS
            body_score = _jaccard(body_kw_lean, rec_body_lean)
            combined_score = _jaccard(
                title_kw | body_kw,
                rec_title_kw | rec_body_set,
            )
            score = max(title_score, body_score, combined_score)
        else:
            score = title_score

        if score > best_score:
            best_score = score
            best_match = rec.get("title", "")

    is_dup = best_score >= THRESHOLD
    return is_dup, best_match, best_score


# ─────────────────────────────────────────────────────
# 발행 완료 후 히스토리에 기록
# ─────────────────────────────────────────────────────
def record_post(title: str, url: str = "", account: str = "", content: str = ""):
    """발행 완료 글을 히스토리에 기록한다.

    Args:
        content: (선택) 본문. 주어지면 본문 키워드를 추출해 body_kw(정렬 list)
                 로 함께 저장한다. 이후 check_duplicate(..., content=...) 가
                 본문까지 비교할 수 있게 한다. 생략하면 기존 레코드와 동일한
                 형태(body_kw 없음)로 저장된다(하위 호환).
    """
    clean = re.sub(r'^\d{4}-\d{2}-\d{2} — ', '', title)
    record = {
        "title":       title,
        "clean_title": clean,
        "url":         url,
        "account":     account,
        "posted_at":   datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    if content:
        # set 은 JSON 직렬화 불가 → 정렬 list 로 저장(안정적 diff·상한 적용).
        record["body_kw"] = sorted(_keywords(content))[:_BODY_KW_LIMIT]

    with _db_lock:
        records = _load_db()
        records.append(record)
        # ⚠️ 중복검사 사각지대 방지 — post_history.json 이 곧 "전량 비교 인덱스"이므로
        # 여기서 트림하면 잘려나간 오래된 글은 check_duplicate 가 영영 비교할 수 없다
        # (수천 편 양산 시 최근 N편만 비교 → 사촌 글 자기잠식). 따라서 트림은 파일이
        # 병리적으로 비대해지는 것을 막는 '안전 밸브'로만 쓰고, 정상 운영 볼륨(수천 편)
        # 에서는 절대 잘라내지 않는다. POST_HISTORY_MAX <= 0 이면 무제한(권장).
        cap = _effective_history_cap()
        if cap and len(records) > cap:
            records = records[-cap:]
        _save_db(records)


# ─────────────────────────────────────────────────────
# 엑셀에서 중복 의심 항목 사전 스캔 (GUI 미리보기용)
# ─────────────────────────────────────────────────────
def scan_excel_duplicates(excel_path: str, sheet_name: str) -> list:
    """
    엑셀 파일 전체를 스캔해서 이미 발행된 글과 유사한 항목 목록 반환.
    GUI의 '중복 검사' 버튼에서 호출.
    """
    from openpyxl import load_workbook
    from openpyxl.styles import PatternFill

    ORANGE_FILL = PatternFill(start_color="FFA500", end_color="FFA500", fill_type="solid")
    results = []

    if not os.path.exists(excel_path):
        return results

    wb = load_workbook(excel_path)
    ws = wb[sheet_name]

    for row in ws.iter_rows():
        for cell in row:
            if not cell.value:
                continue
            title = str(cell.value)
            is_dup, matched, score = check_duplicate(title)
            if is_dup:
                results.append({
                    "title":   title,
                    "matched": matched,
                    "score":   round(score * 100),
                    "row":     cell.row,
                    "col":     cell.column,
                })
                # 엑셀에 주황색 표시
                cell.fill = ORANGE_FILL

    if results:
        wb.save(excel_path)

    return results


# ─────────────────────────────────────────────────────
# 통계
# ─────────────────────────────────────────────────────
def get_stats() -> dict:
    with _db_lock:
        records = _load_db()
    return {
        "total_posted": len(records),
        "accounts":     list({r.get("account","") for r in records if r.get("account")}),
        "recent":       records[-5:][::-1] if records else [],
    }
