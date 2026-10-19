# core/keyword_expander.py
# 롱테일 키워드 자동 파생 시스템
# 메인 키워드 1개 → 롱테일 최대 10개 파생 (검색량 하한 게이트는 실측으로)
# Claude CLI + 네이버 자동완성 API 활용 (비용 0원)
# ⚠️ 검색량은 LLM이 모른다 — 볼륨/경쟁도 판정은 오직 core.search_volume 실측만 사용.
from __future__ import annotations

import json, re, os, sys, subprocess, threading, urllib.request, urllib.parse, urllib.error, time
from pathlib import Path
from datetime import datetime
from typing import Optional

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.logger import get_logger
from core.search_volume import best_volume, meets_threshold_live
try:
    from config.settings import HTTP_REQUEST_TIMEOUT, SUBPROCESS_TIMEOUT
except (ImportError, AttributeError):
    HTTP_REQUEST_TIMEOUT = 10
    SUBPROCESS_TIMEOUT   = 180
try:
    from config.settings import SEARCH_VOLUME_GATE_ENABLED, MIN_SEARCH_VOLUME
except (ImportError, AttributeError):
    SEARCH_VOLUME_GATE_ENABLED = True   # 설정 없으면 게이트 활성(엑셀 없으면 fail-open)
    MIN_SEARCH_VOLUME          = 500
log = get_logger(__name__)

BASE_DIR      = Path(__file__).parent.parent
from config import profile as _nbaprofile
_DATA_DIR = _nbaprofile.app_data_root()

EXPAND_DB     = _DATA_DIR / "logs" / "expanded_keywords.json"
EXCEL_CACHE   = _DATA_DIR / "logs" / "keyword_queue.json"   # GUI → 스케줄러 전달용


# ──────────────────────────────────────────────────────
# DB 관리
# ──────────────────────────────────────────────────────
def _load_db() -> dict:
    """{ "기초연금": { "expanded": [...], "created_at": "..." } }"""
    from config import safe_load_json
    return safe_load_json(EXPAND_DB, default=dict)

def _save_db(data: dict):
    from config import atomic_write_json
    atomic_write_json(EXPAND_DB, data)


# ──────────────────────────────────────────────────────
# 경쟁도(difficulty) — LLM 추측이 아니라 실측 검색량으로만 판정
# ──────────────────────────────────────────────────────
def _difficulty_from_volume(text: str) -> str:
    """실측 검색량(search_volume) 기준 경쟁도. 볼륨 클수록 유리(low).

    검색량은 오직 엑셀 실측(best_volume)에서만 온다 — LLM이 지어낸 값을 쓰지 않는다.
    매칭 없으면(볼륨 0) 수요 미확인이므로 보수적으로 'high'.
    """
    volume = best_volume(text)
    if volume >= 5000:
        return "low"
    if volume >= 500:
        return "mid"
    return "high"


# ──────────────────────────────────────────────────────
# ① 네이버 자동완성 스크래핑
# ──────────────────────────────────────────────────────
def _naver_autocomplete(keyword: str) -> list[str]:
    """
    네이버 검색 자동완성 API로 연관 키워드 수집.
    실제 사람들이 검색하는 키워드이므로 신뢰도가 높습니다.
    """
    suggestions = []
    try:
        encoded = urllib.parse.quote(keyword)
        url = (
            f"https://ac.search.naver.com/nx/ac"
            f"?q={encoded}&con=1&frm=nv&ans=2&bgp=0&limit=10"
        )
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://search.naver.com/",
        })
        with urllib.request.urlopen(req, timeout=HTTP_REQUEST_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            # 응답 형식: {"items": [["키워드1", ...], ["키워드2", ...]]}
            items = data.get("items", [])
            for item in items:
                if isinstance(item, list) and item:
                    kw = str(item[0]).strip()
                    if kw and kw != keyword and 4 <= len(kw) <= 30:
                        suggestions.append(kw)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        log.warning(f"자동완성 오류 ({keyword}): {e}")

    # 변형 쿼리로 추가 수집 (신청방법 / 자격 접미어)
    for suffix in ["신청방법", "자격", "조건", "금액", "기간"]:
        if len(suggestions) >= 15:
            break
        try:
            encoded2 = urllib.parse.quote(f"{keyword} {suffix}")
            url2 = (
                f"https://ac.search.naver.com/nx/ac"
                f"?q={encoded2}&con=1&frm=nv&ans=2&bgp=0&limit=5"
            )
            req2 = urllib.request.Request(url2, headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://search.naver.com/",
            })
            with urllib.request.urlopen(req2, timeout=HTTP_REQUEST_TIMEOUT) as resp2:
                data2 = json.loads(resp2.read().decode("utf-8"))
                for item in data2.get("items", []):
                    if isinstance(item, list) and item:
                        kw = str(item[0]).strip()
                        if kw and kw not in suggestions and 4 <= len(kw) <= 35:
                            suggestions.append(kw)
            time.sleep(0.3)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            pass

    return suggestions[:20]


# ──────────────────────────────────────────────────────
# ② Claude로 롱테일 파생
# ──────────────────────────────────────────────────────
def _claude_expand(keyword: str, autocomplete_hints: list[str]) -> list[dict]:
    """
    Claude CLI로 롱테일 키워드 10개 파생.
    autocomplete_hints: 네이버 자동완성 결과 (Claude에게 힌트로 제공)
    Returns: [{"keyword": str, "intent": str, "reason": str}, ...]

    검색량·경쟁도(difficulty)는 여기서 받지 않는다 — LLM은 검색량을 모른다.
    difficulty 는 expand_keyword 가 실측 볼륨(search_volume)으로 채운다.
    """
    hints_str = ", ".join(autocomplete_hints[:10]) if autocomplete_hints else "없음"

    from datetime import date
    year = date.today().year

    # ⚠️ 검색량은 LLM이 모른다(환각 볼륨). 프롬프트에서 "월 예상 검색량 N~M"
    #    같은 지어내기 지시를 절대 넣지 않는다. 검색량 판정·경쟁도(difficulty)는
    #    오직 실측(core.search_volume.best_volume/meets_threshold)으로만 코드가
    #    건다(expand_keyword). LLM에는 '키워드 아이디어'와 '검색 의도'만 요청한다.
    #    (keyword-research 스킬 §10 "검색량 데이터 없이 추측 출력 금지")
    prompt = f"""당신은 한국 블로그 SEO 전문가입니다.
다음 메인 키워드에서 실제 사람들이 검색할 법한 구체적 롱테일 키워드 10개를 파생해주세요.

메인 키워드: {keyword}
네이버 자동완성 참고 키워드: {hints_str}

[파생 규칙 — 반드시 준수]
1. 구체적 조건·금액·절차·대상을 담은 롱테일 (3단어 이상)
2. {year}년 현재 실제로 사람들이 검색할 법한 자연스러운 표현
3. 검색 의도 분류: 정보형(info) / 신청방법형(how) / 자격확인형(check) / 비교형(compare)

※ 검색량이나 경쟁도는 추측해서 적지 마세요 — 시스템이 실측 데이터로 별도 판정합니다.

좋은 예시:
- "기초연금" → "2026 기초연금 수급자격 소득인정액 기준"
- "청년도약계좌" → "청년도약계좌 중도해지 환급금 얼마"
- "국가장학금" → "국가장학금 1유형 2유형 차이 비교"

반드시 아래 JSON 배열 형식으로만 응답 (마크다운 코드블록 없이):
[
  {{"keyword": "롱테일 키워드", "intent": "info|how|check|compare", "reason": "이 키워드를 선택한 이유 한줄"}},
  ...
]"""

    try:
        import subprocess
        env = os.environ.copy()
        env.pop("ANTHROPIC_API_KEY", None)
        result = subprocess.run(
            ["claude", "-p", prompt, "--output-format", "text"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=SUBPROCESS_TIMEOUT, env=env,
        )
        if result.returncode != 0 or not result.stdout.strip():
            log.warning(f"Claude CLI 오류: {result.stderr[:80]}")
            return []

        raw = result.stdout.strip()
        raw = re.sub(r'^```[a-z]*\n?', '', raw, flags=re.MULTILINE)
        raw = re.sub(r'\n?```$', '', raw, flags=re.MULTILINE).strip()

        # JSON 배열 추출
        m = re.search(r'\[[\s\S]*\]', raw)
        if m:
            return json.loads(m.group())
        return json.loads(raw)

    except (subprocess.TimeoutExpired, FileNotFoundError, subprocess.CalledProcessError) as e:
        log.warning(f"Claude 파생 오류: {e}")
        return []
    except json.JSONDecodeError as e:
        log.warning(f"Claude 응답 JSON 파싱 오류: {e}")
        return []


# ──────────────────────────────────────────────────────
# ③ 핵심 공개 함수: expand_keyword
# ──────────────────────────────────────────────────────
def expand_keyword(keyword: str, force: bool = False,
                   progress_cb=None,
                   stop_flag: Optional[threading.Event] = None) -> list[dict]:
    """
    메인 키워드 1개 → 롱테일 키워드 최대 10개 파생.

    Returns:
        [
          {
            "keyword":    "2026 기초연금 소득인정액 기준",
            "intent":     "check",
            "difficulty": "low",
            "reason":     "...",
            "source":     "claude" | "autocomplete",
            "added_to_excel": False,
          },
          ...
        ]
    """
    db = _load_db()

    # 캐시 확인 (오늘 이미 파생한 키워드는 재사용)
    if not force and keyword in db:
        cached = db[keyword]
        age_days = (datetime.now() - datetime.fromisoformat(
            cached.get("created_at", "2000-01-01"))).days
        if age_days < 7:
            log.info(f"캐시 사용 ({keyword}) — {len(cached['expanded'])}개")
            return cached["expanded"]

    results = []

    # Step 1: 네이버 자동완성
    if progress_cb:
        progress_cb("네이버 자동완성 수집 중...", 1, 4)
    log.info(f"네이버 자동완성 수집: {keyword}")
    autocomplete = _naver_autocomplete(keyword)
    log.info(f"  → {len(autocomplete)}개 힌트 수집")

    # 자동완성 결과를 직접 롱테일로 추가 (가장 신뢰도 높음)
    from datetime import date
    year = str(date.today().year)
    autocomplete_dropped = 0
    for kw in autocomplete[:5]:
        if len(kw) >= 8:    # 너무 짧은 건 제외
            final_kw = kw if year in kw else f"{year} {kw}"
            # 검색량 하한 게이트: 무수요 초-세부 키워드 제외 (엑셀 없으면 fail-open).
            # meets_threshold_live: 엑셀 미등록이어도 자동완성 실검색어면 통과 → 자동완성으로
            # 뽑은 실검색 키워드를 엑셀에 없다고 폐기하던 문제 해소(2026-07-15).
            if SEARCH_VOLUME_GATE_ENABLED and not meets_threshold_live(final_kw, MIN_SEARCH_VOLUME):
                autocomplete_dropped += 1
                continue
            # 경쟁도는 길이가 아니라 실제 검색량 기준 (볼륨 클수록 유리 = low)
            entry = {
                "keyword":       final_kw,
                "intent":        _guess_intent(kw),
                "difficulty":    _difficulty_from_volume(final_kw),
                "reason":        "네이버 실제 검색 자동완성",
                "source":        "autocomplete",
                "added_to_excel": False,
            }
            results.append(entry)
    if autocomplete_dropped:
        log.info(f"  🔎 검색량 하한 미달로 제외: {autocomplete_dropped}개")

    # Step 2: Claude 파생
    if stop_flag and stop_flag.is_set():
        return []
    if progress_cb:
        progress_cb("Claude로 롱테일 파생 중...", 2, 4)
    log.info("Claude 롱테일 파생 중...")
    claude_results = _claude_expand(keyword, autocomplete)
    claude_dropped = 0
    for item in claude_results:
        if isinstance(item, dict) and item.get("keyword"):
            kw = item["keyword"].strip()
            # 중복 제거
            existing = {r["keyword"] for r in results}
            if kw not in existing and 6 <= len(kw) <= 40:
                # 검색량 하한 게이트: 무수요 초-세부 키워드 제외 (엑셀 없으면 fail-open).
                # meets_threshold_live: 엑셀 미등록이어도 자동완성 실검색어면 통과(2026-07-15).
                if SEARCH_VOLUME_GATE_ENABLED and not meets_threshold_live(kw, MIN_SEARCH_VOLUME):
                    claude_dropped += 1
                    continue
                # 경쟁도는 LLM 추측이 아니라 실측 검색량으로 덮어쓴다(환각 볼륨 차단).
                item["difficulty"] = _difficulty_from_volume(kw)
                item["source"] = "claude"
                item.setdefault("added_to_excel", False)
                results.append(item)
    if claude_dropped:
        log.info(f"  🔎 검색량 하한 미달로 제외: {claude_dropped}개")

    # 기회점수 재정렬 (2026-07-04, Phase 2) — 수요×승산×권위적합×신선도×실측수요로
    # 발행 ROI 가 높은 순으로 정렬. 자기잠식·무수요는 자동 후순위. 실패 시 의도 정렬 폴백.
    try:
        from core.opportunity_score import score_candidates
        scored = {s["keyword"]: s["score"]
                  for s in score_candidates([r["keyword"] for r in results])}
        for r in results:
            r["opportunity"] = scored.get(r["keyword"], 0.0)
        results.sort(key=lambda e: e.get("opportunity", 0.0), reverse=True)
    except (ImportError, KeyError, TypeError, ValueError) as e:
        log.debug(f"  기회점수 정렬 폴백(의도 정렬): {e}")
        _INTENT_RANK = {"info": 0, "how": 0, "check": 1, "compare": 2}
        results.sort(key=lambda e: _INTENT_RANK.get((e.get("intent") or "").lower(), 3))

    results = results[:10]   # 최대 10개

    # DB 저장
    if progress_cb:
        progress_cb("결과 저장 중...", 3, 4)
    db[keyword] = {
        "expanded":   results,
        "created_at": datetime.now().isoformat(),
        "base_keyword": keyword,
    }
    _save_db(db)

    if progress_cb:
        progress_cb("완료", 4, 4)
    log.info(f"파생 완료: {len(results)}개 롱테일 키워드")
    return results


# ──────────────────────────────────────────────────────
# ③-b 파생어(맥락 신호) — 네트워크·LLM 호출 없는 로컬 전용 경로
# ──────────────────────────────────────────────────────
# QRA(네이버 검색 추천)는 "단어 반복보다 맥락"을 본다. 그래서 품질 게이트가
# 핵심어 정확 반복 횟수만 세면 오히려 기계적 반복(=AI 티)을 강제하게 된다.
# 아래 local_variants 는 **파일 캐시와 형태 분해만으로** 핵심어의 파생어/문맥어를
# 만들어, 게이트가 반복 대신 맥락을 인정할 수 있게 한다.
#
# ⚠️ 이 경로는 QA(검증) 안에서 호출된다 — 네트워크·subprocess 를 절대 타지 않는다
#    (expand_keyword 는 자동완성 HTTP + Claude CLI 를 쓰므로 여기서 부르지 않는다).
#    캐시가 없으면 빈 리스트를 돌려주고, 호출자는 그걸 fail-open 으로 해석한다.

# 파생어 후보에서 제외할 범용 어휘 — 주제와 무관하게 어느 글에나 나오는 말이라
# 맥락 신호로 인정하면 게이트가 무의미해진다(주제·계정 하드코딩 아님).
_VARIANT_STOP = frozenset({
    "지원", "신청", "방법", "정보", "안내", "기준", "조건", "대상", "금액",
    "혜택", "제도", "사업", "정책", "확인", "내용", "경우", "이상", "이하",
    "올해", "내년", "작년", "관련", "필요", "사용", "가능", "총정리", "정리",
})
_VARIANT_MAX = 12          # 게이트에 넘길 파생어 상한(스캔 비용·과다 인정 방지)
_VARIANT_MIN_LEN = 2       # 1글자 토큰은 오탐이 너무 많다


def _despace_kw(text: str) -> str:
    """공백 제거 — 본문이 '영어 공부'로 띄어 써도 '영어공부'로 매칭하기 위함."""
    return re.sub(r"\s+", "", text or "")


def _morph_variants(keyword: str) -> list[str]:
    """핵심어 자체의 복합어 분해. 예: '청년도약계좌' → 청년 / 도약계좌 / 청년도약 / 계좌.

    한국어 복합명사는 앞 2음절 또는 뒤 2음절에서 갈리는 경우가 대부분이라
    **분리점 2곳(앞 2음절 / 뒤 2음절)** 에서만 자른다. 그러면 잘린 두 조각이
    서로의 나머지가 되어 비단어("약계좌" 같은 중간 절단)가 잘 생기지 않는다 —
    이 목록은 생성 프롬프트에 "이렇게 변주해도 좋다"는 힌트로도 나가므로
    비단어를 최소화하는 게 중요하다.

    Args:
        keyword: 핵심 키워드(공백 포함 가능).

    Returns:
        중복 없는 조각 리스트. 4글자 미만이면 빈 리스트(더 쪼갤 게 없음).
    """
    base = _despace_kw(keyword)
    if len(base) < 4:
        return []
    variants: list[str] = []
    for cut in (2, len(base) - 2):          # 앞에서 2음절 / 뒤에서 2음절
        if cut < _VARIANT_MIN_LEN or cut > len(base) - _VARIANT_MIN_LEN:
            continue
        for part in (base[:cut], base[cut:]):
            if part and part != base and part not in variants and part not in _VARIANT_STOP:
                variants.append(part)
    return variants


def _cached_variants(keyword: str) -> list[str]:
    """이미 파생해 둔 롱테일 캐시에서 문맥 토큰을 뽑는다(파일 읽기만).

    expanded_keywords.json 은 expand_keyword 가 적립한 캐시다. 여기서는
    핵심어를 포함/피포함하는 캐시 키의 롱테일 문구를 토큰으로 쪼개, 핵심어
    자체가 아닌 **함께 검색되는 말**(예: '중도해지', '환급금')만 남긴다.

    Args:
        keyword: 핵심 키워드.

    Returns:
        문맥 토큰 리스트. 캐시 파일이 없거나 형식이 깨졌으면 빈 리스트.
    """
    base = _despace_kw(keyword)
    if not base:
        return []
    try:
        db = _load_db()
    except (OSError, ValueError, TypeError) as exc:
        log.debug(f"파생어 캐시 로드 실패(건너뜀): {exc}")
        return []
    if not isinstance(db, dict):
        return []

    try:
        from core.quality_validator import _strip_josa as _strip
    except (ImportError, AttributeError):
        def _strip(word: str) -> str:
            return word

    variants: list[str] = []
    for db_key, record in db.items():
        key_ds = _despace_kw(str(db_key))
        if not key_ds or (key_ds not in base and base not in key_ds):
            continue
        expanded = (record or {}).get("expanded") if isinstance(record, dict) else None
        for item in expanded or []:
            phrase = item.get("keyword") if isinstance(item, dict) else None
            if not phrase:
                continue
            for token in re.findall(r"[가-힣]{2,}|[A-Za-z]{3,}", str(phrase)):
                token = _strip(token)
                if len(token) < _VARIANT_MIN_LEN:
                    continue
                if token in _VARIANT_STOP or token in variants:
                    continue
                # 핵심어의 일부(또는 핵심어 자체)는 형태 분해가 이미 다룬다.
                token_ds = _despace_kw(token)
                if token_ds in base or base in token_ds:
                    continue
                variants.append(token)
    return variants


def local_variants(keyword: str, limit: int = _VARIANT_MAX) -> list[str]:
    """핵심어의 파생어·문맥어를 로컬에서만 만들어 돌려준다(네트워크 호출 없음).

    소스 두 가지를 순서대로 합친다:
      1) 형태 분해(_morph_variants) — 항상 가능, 오프라인 안전
      2) 파생 캐시(_cached_variants) — expand_keyword 가 적립해 둔 롱테일 문맥어

    Args:
        keyword: 핵심 키워드. 빈 값이면 빈 리스트.
        limit:   반환 상한(기본 12).

    Returns:
        중복 제거된 파생어 리스트. 하나도 못 만들면 빈 리스트 —
        호출자(품질 게이트)는 이를 "맥락 판정 불가"로 보고 관대하게 처리해야 한다.
    """
    if not keyword or not keyword.strip() or limit <= 0:
        return []
    merged: list[str] = []
    for source in (_morph_variants, _cached_variants):
        try:
            candidates = source(keyword)
        except Exception as exc:  # noqa: BLE001 — 파생어 실패가 검증을 막으면 안 된다
            log.warning(f"파생어 생성 실패({source.__name__}): {exc}", exc_info=True)
            candidates = []
        for cand in candidates:
            if cand not in merged:
                merged.append(cand)
            if len(merged) >= limit:
                return merged
    return merged


def _guess_intent(kw: str) -> str:
    """키워드에서 검색 의도 추정"""
    if any(w in kw for w in ["방법","하는법","신청","어떻게","절차","단계"]):
        return "how"
    if any(w in kw for w in ["자격","조건","해당","가능","여부","대상"]):
        return "check"
    if any(w in kw for w in ["차이","비교","vs","versus","뭐가","어떤"]):
        return "compare"
    return "info"


# ──────────────────────────────────────────────────────
# ④ 엑셀에 파생 키워드 자동 추가
# ──────────────────────────────────────────────────────
# 롱테일 시트의 표준 헤더 (excel_migrate.py와 동일 스키마)
_LONGTAIL_HEADER = [
    "parent_keyword", "longtail", "search_volume", "category",
    "added_at", "scheduled_at", "added_to_excel", "published_url",
]


def _add_to_legacy_sheet(ws, keywords: list[dict], target_date: str) -> int:
    """레거시 동작: 1열 누적 모드로 sheet_name에 'YYYY-MM-DD — kw' 형식 추가."""
    existing: set[str] = set()
    for row in ws.iter_rows():
        for cell in row:
            if cell.value:
                existing.add(str(cell.value).strip())

    added = 0
    for kw_info in keywords:
        kw = kw_info.get("keyword", "").strip()
        if not kw:
            continue
        entry = f"{target_date} — {kw}"
        if entry in existing or kw in existing:
            log.info(f"이미 있음: {kw[:30]}")
            continue

        next_row = ws.max_row + 1
        ws.cell(row=next_row, column=1, value=entry)
        existing.add(entry)
        added += 1
        log.info(f"추가됨: {entry[:46]}")
        kw_info["added_to_excel"] = True

    return added


def _add_to_longtail_sheet(ws, keywords: list[dict], target_date: str) -> int:
    """롱테일 시트 모드: 표준 8열 스키마(parent_keyword/longtail/...)로 행 추가.

    헤더가 비어있거나 1행이 비정상이면 헤더를 자동 보정한다.
    중복 판정은 longtail(2열) 컬럼 기준.
    """
    # 헤더 보정 (idempotent)
    header_row = [ws.cell(row=1, column=c).value for c in range(1, len(_LONGTAIL_HEADER) + 1)]
    if header_row != _LONGTAIL_HEADER:
        for col_idx, header in enumerate(_LONGTAIL_HEADER, start=1):
            ws.cell(row=1, column=col_idx, value=header)

    # 기존 longtail 키워드 수집 (중복 방지)
    existing: set[str] = set()
    for row_idx in range(2, ws.max_row + 1):
        val = ws.cell(row=row_idx, column=2).value  # longtail 컬럼
        if val:
            existing.add(str(val).strip())

    added = 0
    next_row = ws.max_row + 1 if ws.max_row >= 1 else 2
    if next_row < 2:
        next_row = 2

    for kw_info in keywords:
        longtail = kw_info.get("keyword", "").strip()
        if not longtail or longtail in existing:
            if longtail:
                log.info(f"이미 있음(롱테일): {longtail[:30]}")
            continue

        parent = kw_info.get("parent_keyword", "") or ""
        category = kw_info.get("category", "") or ""
        search_volume = kw_info.get("search_volume", "") or ""
        # 값이 비면 엑셀 검색량 진실원본에서 실제 볼륨을 채운다 (리뷰 시 볼륨 가시화)
        if not search_volume:
            resolved_volume = best_volume(longtail) or best_volume(parent)
            if resolved_volume:
                search_volume = resolved_volume

        ws.cell(row=next_row, column=1, value=parent)
        ws.cell(row=next_row, column=2, value=longtail)
        ws.cell(row=next_row, column=3, value=search_volume)
        ws.cell(row=next_row, column=4, value=category)
        ws.cell(row=next_row, column=5, value=target_date)
        ws.cell(row=next_row, column=6, value="")          # scheduled_at
        ws.cell(row=next_row, column=7, value=True)         # added_to_excel
        ws.cell(row=next_row, column=8, value="")          # published_url

        existing.add(longtail)
        next_row += 1
        added += 1
        log.info(f"추가됨(롱테일): {longtail[:46]} (parent={parent[:20]})")
        kw_info["added_to_excel"] = True

    return added


def add_to_excel(keywords: list[dict], excel_path: str, sheet_name: str,
                 target_date: Optional[str] = None,
                 longtail_sheet: Optional[str] = None) -> int:
    """파생 키워드를 엑셀에 추가합니다.

    Args:
        keywords:       파생 키워드 dict 리스트. 각 dict는 최소 'keyword' 키 필수,
                        선택적으로 'parent_keyword', 'category', 'search_volume' 가능.
        excel_path:     엑셀 파일 경로.
        sheet_name:     레거시 모드에서 사용할 시트 (longtail_sheet가 None일 때).
        target_date:    "YYYY-MM-DD" 형식. None이면 내일 날짜 사용.
        longtail_sheet: 지정되면 이 시트에 표준 8열 스키마로 행 추가.
                        None이면 기존 동작(sheet_name에 1열 누적).

    Returns:
        실제로 추가된 행 수.
    """
    from openpyxl import load_workbook
    from datetime import date, timedelta

    if not os.path.exists(excel_path):
        log.error(f"엑셀 없음: {excel_path}")
        return 0

    if not target_date:
        tomorrow = date.today() + timedelta(days=1)
        target_date = tomorrow.strftime("%Y-%m-%d")

    wb = load_workbook(excel_path)

    # 모드 분기: longtail_sheet 우선
    if longtail_sheet:
        if longtail_sheet not in wb.sheetnames:
            log.error(f"롱테일 시트 없음: {longtail_sheet}")
            return 0
        ws = wb[longtail_sheet]
        added = _add_to_longtail_sheet(ws, keywords, target_date)
    else:
        if sheet_name not in wb.sheetnames:
            log.error(f"시트 없음: {sheet_name}")
            return 0
        ws = wb[sheet_name]
        added = _add_to_legacy_sheet(ws, keywords, target_date)

    wb.save(excel_path)

    # DB 업데이트 (added_to_excel 플래그)
    db = _load_db()
    for base_kw, record in db.items():
        for item in record.get("expanded", []):
            for kw_info in keywords:
                if item.get("keyword") == kw_info.get("keyword"):
                    item["added_to_excel"] = kw_info.get("added_to_excel", False)
    _save_db(db)

    return added


# ──────────────────────────────────────────────────────
# ⑤ 엑셀 전체 메인 키워드 일괄 파생
# ──────────────────────────────────────────────────────
def batch_expand_from_excel(excel_path: str, sheet_name: str,
                             max_base_keywords: int = 5,
                             progress_cb=None) -> dict:
    """
    엑셀에서 메인 키워드(날짜 없는 셀)를 읽어 일괄 파생합니다.
    하루 최대 max_base_keywords개만 처리 (Claude 호출 부하 관리).
    Returns: {"processed": int, "total_expanded": int, "results": {...}}
    """
    from openpyxl import load_workbook
    import re as _re

    if not os.path.exists(excel_path):
        return {"processed": 0, "total_expanded": 0, "results": {}}

    wb = load_workbook(excel_path)
    if sheet_name not in wb.sheetnames:
        return {"processed": 0, "total_expanded": 0, "results": {}}
    ws = wb[sheet_name]

    # 날짜 없는 셀 = 메인 키워드 후보
    db = _load_db()
    candidates = []
    for row in ws.iter_rows():
        for cell in row:
            if not cell.value:
                continue
            val = str(cell.value).strip()
            if _re.match(r'^\d{4}-\d{2}-\d{2}', val):
                continue    # 날짜 달린 셀은 이미 처리된 키워드
            if val not in db and len(val) >= 3:
                candidates.append(val)

    candidates = candidates[:max_base_keywords]
    total_expanded = 0
    all_results = {}

    for i, kw in enumerate(candidates):
        if progress_cb:
            progress_cb(f"[{i+1}/{len(candidates)}] {kw}", i+1, len(candidates))
        expanded = expand_keyword(kw)
        all_results[kw] = expanded
        total_expanded += len(expanded)
        if i < len(candidates) - 1:
            time.sleep(1)    # API 과호출 방지

    return {
        "processed":      len(candidates),
        "total_expanded": total_expanded,
        "results":        all_results,
    }
