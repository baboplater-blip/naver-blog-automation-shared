# core/ai_tell_scorer.py
# "AI 티(AI writing tell) 스코어러" — Phase A(관측 전용, 발행 차단 없음).
#
# 자동 생성 글이 "AI가 쓴 티"가 나는지 정량 채점하는 순수 함수 검사기.
# avoid-ai-writing(conorbronsdon) 레포의 세 통찰을 한국어로 각색:
#   ① 구조·리듬을 수치로 측정(문장 길이 CV·종결어미 균일성)
#   ② 기계 지문 하드 플래그(미기입 플레이스홀더·챗봇 인용 마크업·AI 툴 URL 파라미터)
#   ③ 용량(dosage) 기반 플래깅(같은 신호가 몇 번 나오느냐로 감점)
#
# ★ 순수 함수: 입력 텍스트만 읽고 dict 를 반환한다. 전역/파일을 쓰지 않는다
#   (lexicon JSON 은 읽기만 하고, 없거나 깨져도 빈 사전으로 fail-open).
# ★ 네트워크 0, 표준 라이브러리만(json·re·math·statistics).
# ★ fail-open: 최상단 try/except 로 감싸 어떤 예외도 밖으로 전파하지 않는다.
#
# ⚠️ CALIBRATION-PENDING — 아래 임계·가중 상수는 전부 Phase B 실측 캘리브레이션
#    전의 합리적 placeholder 다. 실 코퍼스로 재조정하기 전까지는 관측 지표로만
#    쓰고, 발행 게이트(차단)로 승격하지 말 것.
from __future__ import annotations

import json
import math
import re
import statistics
from pathlib import Path
from typing import Optional

_LEXICON_PATH = Path(__file__).parent.parent / "templates" / "ai_tell_lexicon.json"

# ── 레벨 구간 (CALIBRATION-PENDING) ──────────────────────────────────
_LEVEL_MID_MIN = 30.0   # score < 30 → low
_LEVEL_HIGH_MIN = 60.0  # 30~60 → mid, > 60 → high

# ── 점수 환산 (Phase B 밀도 기반, CALIBRATION-PENDING) ────────────────
# ⚠️ 예전 공식 `raw / log2(어절/50 + 1)` 은 영어 단문 탐지기용이라 장문 한글에서
#    log2 분모가 커져 tell 이 있어도 score 를 0 근처로 눌렀다(활성 프로필 1,080편
#    실측 결과 코퍼스 90%가 정확히 0.0 으로 붕괴). 이를 "본문 길이 100어절당 가중
#    tell 밀도"로 교체한다 — 장문이라도 tell 이 실제로 밀집하면 점수가 오르고, 희박하면
#    낮게. 여전히 FN-biased(애매하면 낮게).
_DENSITY_WORDS = 100.0  # 밀도 기준 단위(어절). density = soft_raw / max(1, 어절/이 값).
_DENSITY_K = 44.0       # 밀도 → 점수 스케일 계수. 활성 프로필 1,080편 old 코퍼스가
                        # low 81%·mid 12%·high 6% 로 갈라지도록 실측 튜닝(2026-08-29).
                        # (66%는 진짜 tell 0 = 변주 시스템이 이미 정리한 글, 15%는 단일
                        #  경미 tell 로 15~30 밴드에 low 로 남음 = FN-bias.)
_SOFT_SCORE_CAP = 100.0  # 구조·리듬·어휘(soft) 기여 상한.
# 기계 지문(S-1, P0)은 길이 정규화 대상에서 제외 — 절대 개수 기반 고정 환산.
# 한 편에 utm/citeturn/placeholder 지문 1개 = 길이 무관 즉시 high(>= _LEVEL_HIGH_MIN).
# 가장 약한 지문(_W_DATE_PH=8) × _FP_K(7.5) = 60 → high 하한을 정확히 넘긴다.
_FP_K = 7.5

# ── 신호별 가중치 (CALIBRATION-PENDING) ──────────────────────────────
# 기계 지문(S-1, P0 하드) — 고가중.
_W_PLACEHOLDER = 10.0   # 미기입 플레이스홀더 [여기에 …]
_W_DATE_PH = 8.0        # 날짜 플레이스홀더 XXXX-XX-XX
_W_CITATION = 15.0      # 챗봇 인용 마크업 citeturn / oaicite / contentReference
_W_UTM = 12.0           # AI 툴 URL 파라미터 utm_source=chatgpt 등

# 구조·리듬(P1) — 중가중.
_W_SENT_CV = 8.0        # 문장 길이 균일(어절 CV 낮음)
_W_ENDING_RUN = 7.0     # 종결어미 동일 연속 run
_W_ENDING_ENTROPY = 5.0 # 종결형 분포 엔트로피 낮음(다양성 부족)
_W_INVERSION = 6.0      # "~가 아니라 ~입니다" 반전 구문 밀도
_W_DOUBLE_HEDGE = 6.0   # 이중 헤지

# 병렬·어휘(P2) — 저가중.
_W_TRIPLE_PARALLEL = 4.0  # 강박적 3연 병렬
_W_LEX_T1A = 6.0          # 어휘 t1a (강한 AI 티, 1회로 플래그)
_W_LEX_T1B = 2.0          # 어휘 t1b (약한 감점 — 저자성 증거 아님)
_W_LEX_T2 = 3.0           # 어휘 t2 (같은 문단 2개+)
_W_LEX_T3 = 3.0           # 어휘 t3 (전체 고밀도 3개+)

# ── 임계값 (CALIBRATION-PENDING) ─────────────────────────────────────
_SENT_CV_THRESHOLD = 0.30   # 어절 CV 이 값 미만이면 균일(감점)
_SENT_MIN_COUNT = 4         # 문장 이 개수 이상일 때만 CV 판정
_SENT_MIN_AVG_WORDS = 6.0   # 평균 어절 이 값 이상일 때만(짧은 목록 오탐 방지)
_ENDING_RUN_THRESHOLD = 4   # 동일 종결형 연속 이 횟수 이상이면 플래그
_ENDING_ENTROPY_MIN_SENT = 6  # 엔트로피 판정 최소 문장 수
_ENDING_ENTROPY_THRESHOLD = 1.0  # shannon 엔트로피(bit) 이 값 미만이면 다양성 부족
# ── 카운트 기반 신호는 이진 게이트가 아니라 "등급형"으로 raw 에 기여한다 ──────
# (2026-08-29 Phase B) 실측: 활성 코퍼스 1,080편의 종결어미/문장 리듬은 이미 충분히
# 다양(sentence_cv p50=0.64·ending_entropy p50=4.9)해 균일성 게이트는 거의 안 걸린다.
# 반면 반전 구문(244편)·어휘 티어(t2 121편) 같은 카운트형 신호는 존재하지만 예전
# "2회 이상이면 1회 가점" 이진 게이트가 대부분을 버려 raw 가 0 으로 붕괴했다. 이를
# "출현 횟수만큼(상한까지) 가점"으로 바꿔 raw 가 tell 밀도를 반영하게 하고, 최종은
# 밀도 정규화가 장문 희석을 담당한다(단일 출현은 density 로 자연히 low 유지 = FN-bias).
_INVERSION_CAP = 3          # 반전 구문 가점 상한 횟수
_DOUBLE_HEDGE_CAP = 3       # 이중 헤지 가점 상한 횟수
_TRIPLE_PARALLEL_CAP = 3    # 3연 병렬 가점 상한 횟수
_LEX_T2_CAP = 3             # t2 어휘 가점 상한 개수(문서 전체)
_LEX_T3_CAP = 3             # t3 어휘 가점 상한 개수(문서 전체)

# ── 정규식 (모듈 스코프 컴파일) ──────────────────────────────────────
# S-1 기계 지문 — 원문 전체에서 검사(인용/URL 제거 전).
_RE_PLACEHOLDER = re.compile(r"\[(여기에|당신의|본인|삽입|입력|이름|날짜|링크)[^\]]{0,30}\]")
_RE_DATE_PH = re.compile(r"\d{4}-XX-XX|XXXX-\d")
_RE_CITATION = re.compile(r"citeturn\d|oaicite|contentReference|:contentReference")
_RE_UTM = re.compile(r"utm_source=(chatgpt|claude|perplexity|copilot|openai|gemini|bing|you)")

# 전처리 — 계산 텍스트에서 제거(구조는 AI 티로 감점하지 않는다).
_RE_CODEBLOCK = re.compile(r"```.*?```", re.DOTALL)
_RE_URL = re.compile(r"https?://\S+")
_RE_QUOTED = re.compile(r"《[^》]*》|“[^”]*”|\"[^\"]*\"|‘[^’]*’|'[^']*'")
# FAQ 라인(마크다운 관례 Q1./A1.) — 답변이 대개 동일 종결어미라 짧은 글에서
# S-3(종결어미 균일성) 오탐을 유발한다. 표·코드처럼 계산 텍스트에서 제외한다.
# (선택적 마크다운 강조 **·-·> 접두 허용.)
_RE_FAQ_LINE = re.compile(r"^\s*(?:[-*>]\s*)?(?:\*\*)?[QA]\d+\.")

# S-2 문장 분리(근사).
_RE_SENT_SPLIT = re.compile(r"(?:다\.|요\.|[!?]|…)\s*|\n+")

# S-6 반전 구문 "~(이|가|은|는) 아니라 … (종결)".
_RE_INVERSION = re.compile(r"(?:이|가|은|는)?\s*아니라[^.!?\n]{0,40}?(?:다|요|죠|습니다|입니다)")

# S-7 이중 헤지 — 이중 양태 조합.
_DOUBLE_HEDGE_PATTERNS = (
    "수도 있을 것",
    "일 수 있을",
    "것으로 보입니다",
    "것으로 보인다",
    "지 않을까 싶",
    "수 있지 않을까",
    "수도 있지 않",
)

# S-8 강박적 3연 병렬 — "~고, ~며," 골격.
_RE_TRIPLE_PARALLEL = re.compile(r"\S+고,\s*\S+며,")


def _load_lexicon() -> dict:
    """어휘 사전 로드. 파일 없거나 파싱 실패 시 빈 사전으로 fail-open."""
    try:
        with _LEXICON_PATH.open(encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return {}
        return data
    except (OSError, ValueError):
        return {}


def _strip_for_scoring(content: str) -> str:
    """구조(코드블록·표 행·FAQ 라인·인용·URL)를 계산 텍스트에서 제거.

    AEO 승자 구조(표·FAQ·코드·인용)를 'AI 티'로 감점하지 않기 위함이다.
    표 행은 '|' 로 시작하는 줄, FAQ 는 'Q1./A1.' 로 시작하는 줄(마크다운 관례)을
    통째로 버린다 — FAQ 답변은 대개 종결어미가 균일해 S-3 오탐을 유발하기 때문.
    """
    text = _RE_CODEBLOCK.sub(" ", content)
    kept_lines = []
    for line in text.split("\n"):
        if line.lstrip().startswith("|"):
            continue
        if _RE_FAQ_LINE.match(line):
            continue
        kept_lines.append(line)
    text = "\n".join(kept_lines)
    text = _RE_URL.sub(" ", text)
    text = _RE_QUOTED.sub(" ", text)
    return text


def _split_sentences(text: str) -> list:
    """문장 근사 분리 후 공백만인 조각 제거."""
    parts = _RE_SENT_SPLIT.split(text)
    return [p.strip() for p in parts if p and p.strip()]


def _graded_fp_biased(count: int, cap: int) -> float:
    """FN-biased 등급 계수: 첫 출현은 절반(정상 한국어일 수 있음), 반복분은 전액.

    반전 구문·이중 헤지·3연 병렬처럼 정상 문법으로도 1회 나올 수 있는 신호에 쓴다.
    count=1 → 0.5, count=2 → 1.5, count=3 → 2.5 … (cap 에서 포화). 이렇게 하면 단일
    출현은 density 정규화까지 겹쳐 low 로 남고, 반복될수록 mid→high 로 올라간다.
    """
    if count <= 0:
        return 0.0
    return max(min(count, cap) - 0.5, 0.0)


def _ending_form(sentence: str) -> str:
    """문장의 종결형 근사 — 마지막 한글 어절의 뒤 3음절."""
    words = sentence.split()
    if not words:
        return ""
    last = words[-1]
    tail = re.sub(r"[^가-힣]", "", last)
    return tail[-3:] if tail else ""


def _wordcount(sentence: str) -> int:
    return len([w for w in sentence.split() if w])


def _dedup_add(bucket: list, seen: set, sig: tuple, weight: float, label: str) -> float:
    """(type, 대표텍스트) 중복 제거 후 가중 합에 기여. 감점 값을 반환."""
    if sig in seen:
        return 0.0
    seen.add(sig)
    bucket.append(label)
    return weight


def _score_impl(content: str, category: str) -> dict:
    signals: dict = {}
    p0: list = []
    p1: list = []
    p2: list = []
    seen: set = set()
    # 기계 지문(S-1, P0)은 길이 정규화 대상에서 제외하므로 별도 누산.
    fp_raw = 0.0
    # 구조·리듬·어휘(soft)는 밀도 정규화 대상.
    raw = 0.0

    # ── S-1 기계 지문 — 원문 전체에서 검사(URL/인용 제거 전) ──────────
    ph_hits = _RE_PLACEHOLDER.findall(content)
    if ph_hits:
        signals["placeholder"] = len(ph_hits)
        for match in set(ph_hits):
            fp_raw += _dedup_add(p0, seen, ("placeholder", match), _W_PLACEHOLDER,
                                 f"미기입 플레이스홀더 [{match} …]")
    date_hits = _RE_DATE_PH.findall(content)
    if date_hits:
        signals["date_placeholder"] = len(date_hits)
        for match in set(date_hits):
            fp_raw += _dedup_add(p0, seen, ("date_ph", match), _W_DATE_PH,
                                 f"날짜 플레이스홀더 {match}")
    cite_hits = _RE_CITATION.findall(content)
    if cite_hits:
        signals["citation_markup"] = len(cite_hits)
        for match in set(cite_hits):
            fp_raw += _dedup_add(p0, seen, ("citation", match), _W_CITATION,
                                 f"챗봇 인용 마크업 {match}")
    utm_hits = _RE_UTM.findall(content)
    if utm_hits:
        signals["utm_source"] = len(utm_hits)
        for match in set(utm_hits):
            fp_raw += _dedup_add(p0, seen, ("utm", match), _W_UTM,
                                 f"AI 툴 URL 파라미터 utm_source={match}")

    # ── 전처리(구조 제거)된 텍스트로 리듬·어휘 신호 계산 ──────────────
    scored_text = _strip_for_scoring(content)
    sentences = _split_sentences(scored_text)
    words_total = sum(_wordcount(s) for s in sentences)
    signals["sentence_count"] = len(sentences)
    signals["word_count"] = words_total

    # ── S-2 문장 길이 균일성(어절 CV) ────────────────────────────────
    wordcounts = [_wordcount(s) for s in sentences]
    wordcounts = [w for w in wordcounts if w > 0]
    if len(wordcounts) >= _SENT_MIN_COUNT:
        mean_wc = statistics.mean(wordcounts)
        if mean_wc >= _SENT_MIN_AVG_WORDS:
            stdev_wc = statistics.pstdev(wordcounts)
            cv = (stdev_wc / mean_wc) if mean_wc else 0.0
            signals["sentence_cv"] = round(cv, 4)
            if cv < _SENT_CV_THRESHOLD:
                # 낮을수록 큰 감점(0 이면 만점 가중, 임계에서 0).
                severity = max(0.0, (_SENT_CV_THRESHOLD - cv) / _SENT_CV_THRESHOLD)
                gain = _W_SENT_CV * severity
                if gain > 0:
                    raw += gain
                    p1.append(f"문장 길이 균일(어절 CV {cv:.2f})")

    # ── S-3 종결어미 균일성 ──────────────────────────────────────────
    endings = [_ending_form(s) for s in sentences]
    endings = [e for e in endings if e]
    if endings:
        # (a) 동일 종결형 연속 run.
        max_run = 1
        cur_run = 1
        for i in range(1, len(endings)):
            if endings[i] == endings[i - 1]:
                cur_run += 1
                max_run = max(max_run, cur_run)
            else:
                cur_run = 1
        signals["ending_max_run"] = max_run
        if max_run >= _ENDING_RUN_THRESHOLD:
            raw += _W_ENDING_RUN
            p1.append(f"종결어미 동일 {max_run}연속")
        # (b) 종결형 분포 엔트로피(다양성 부족).
        if len(endings) >= _ENDING_ENTROPY_MIN_SENT:
            total = len(endings)
            counts: dict = {}
            for e in endings:
                counts[e] = counts.get(e, 0) + 1
            entropy = -sum((c / total) * math.log2(c / total) for c in counts.values())
            signals["ending_entropy"] = round(entropy, 4)
            if entropy < _ENDING_ENTROPY_THRESHOLD:
                raw += _W_ENDING_ENTROPY
                p1.append(f"종결형 다양성 부족(엔트로피 {entropy:.2f})")

    # ── S-6 반전 구문 "~가 아니라 ~입니다"(등급형) ──────────────────
    inversion_hits = _RE_INVERSION.findall(scored_text)
    if inversion_hits:
        count = len(inversion_hits)
        signals["inversion"] = count
        raw += _W_INVERSION * _graded_fp_biased(count, _INVERSION_CAP)
        p1.append(f"반전 구문 {count}회('~가 아니라 ~입니다')")

    # ── S-7 이중 헤지(등급형·FN-bias) ───────────────────────────────
    hedge_count = sum(scored_text.count(p) for p in _DOUBLE_HEDGE_PATTERNS)
    if hedge_count:
        signals["double_hedge"] = hedge_count
        raw += _W_DOUBLE_HEDGE * _graded_fp_biased(hedge_count, _DOUBLE_HEDGE_CAP)
        p1.append(f"이중 헤지 {hedge_count}회")

    # ── S-8 강박적 3연 병렬(등급형·FN-bias) ─────────────────────────
    parallel_hits = _RE_TRIPLE_PARALLEL.findall(scored_text)
    if parallel_hits:
        count = len(parallel_hits)
        signals["triple_parallel"] = count
        raw += _W_TRIPLE_PARALLEL * _graded_fp_biased(count, _TRIPLE_PARALLEL_CAP)
        p2.append(f"3연 병렬 {count}회('~하고, ~하며,')")

    # ── S-9 어휘 티어(lexicon) ───────────────────────────────────────
    lex = _load_lexicon()

    def _tier(key: str) -> list:
        # 티어 값이 list 가 아니면(null·dict·문자열 등 스키마 오류) 빈 리스트로 격리 —
        # 한 티어가 깨져도 나머지 신호·점수 전체가 살아남는다(부분 fail-open).
        val = lex.get(key)
        if not isinstance(val, list):
            return []
        return [w for w in val if isinstance(w, str) and w]

    t1a = _tier("t1a")
    t1b = _tier("t1b")
    t2 = _tier("t2")
    t3 = _tier("t3")

    # t1a — 1회로 P1 플래그.
    t1a_hit = 0
    for phrase in t1a:
        if phrase in scored_text:
            t1a_hit += 1
            raw += _dedup_add(p1, seen, ("t1a", phrase), _W_LEX_T1A,
                              f"AI 티 어휘 '{phrase}'")
    if t1a_hit:
        signals["lex_t1a"] = t1a_hit

    # t1b — 약한 감점(저자성 증거 아님, 집계만 별도).
    t1b_hit = 0
    for phrase in t1b:
        if phrase in scored_text:
            t1b_hit += 1
            raw += _dedup_add(p2, seen, ("t1b", phrase), _W_LEX_T1B,
                              f"약한 AI 티 어휘 '{phrase}'")
    if t1b_hit:
        signals["lex_t1b"] = t1b_hit

    # t2 — 문서 전체 출현 개수만큼(상한까지) 등급형 가점.
    paragraphs = [para for para in scored_text.split("\n\n") if para.strip()]
    if not paragraphs:
        paragraphs = [scored_text]
    t2_total = 0
    t2_para_max = 0
    for para in paragraphs:
        present = [w for w in t2 if w in para]
        t2_total += len(present)
        t2_para_max = max(t2_para_max, len(present))
    if t2_total:
        signals["lex_t2"] = t2_total
        raw += _W_LEX_T2 * min(t2_total, _LEX_T2_CAP)
        # 한 문단에 몰리면(집중) 더 강한 AI 티 — p2 라벨로 명시.
        if t2_para_max >= 2:
            p2.append(f"t2 어휘 문단 집중({t2_para_max}개)")
        else:
            p2.append(f"t2 어휘 {t2_total}개")

    # t3 — 문서 전체 출현 개수만큼(상한까지) 등급형 가점.
    t3_total = sum(scored_text.count(w) for w in t3)
    if t3_total:
        signals["lex_t3"] = t3_total
        raw += _W_LEX_T3 * min(t3_total, _LEX_T3_CAP)
        p2.append(f"t3 어휘 {t3_total}개")

    # ── 밀도 기반 환산 + 레벨 판정 ───────────────────────────────────
    # soft(구조·리듬·어휘)는 "100어절당 tell 밀도"로 환산해 장문이라고 0 으로 붕괴하지
    # 않게 한다. 기계 지문(fp)은 길이 무관 고정 환산 후 합산(지문 1개 = 즉시 high).
    units = max(1.0, words_total / _DENSITY_WORDS)
    density = raw / units
    soft_score = min(_SOFT_SCORE_CAP, density * _DENSITY_K)
    fp_score = min(100.0, fp_raw * _FP_K) if fp_raw else 0.0
    score = min(100.0, fp_score + soft_score)
    if score < _LEVEL_MID_MIN:
        level = "low"
    elif score < _LEVEL_HIGH_MIN:
        level = "mid"
    else:
        level = "high"

    signals["raw"] = round(raw, 3)          # soft raw(구조·리듬·어휘)
    signals["fp_raw"] = round(fp_raw, 3)    # 기계 지문 raw(길이 무관)
    signals["density"] = round(density, 4)  # 100어절당 soft tell 밀도
    return {
        "score": round(score, 2),
        "level": level,
        "signals": signals,
        "p0": p0,
        "p1": p1,
        "p2": p2,
    }


def score_ai_tell(content: str, *, category: str = "") -> dict:
    """글 본문의 'AI 티'를 정량 채점한다(관측 전용·순수 함수).

    Args:
        content: 검사할 본문(마크다운/HTML 혼용 허용).
        category: 카테고리 힌트(현재 스코어링에 직접 쓰지 않으나 시그니처 예약).

    Returns:
        {"score": 0~100 float, "level": "low|mid|high", "signals": {...},
         "p0": [...], "p1": [...], "p2": [...]}.
        어떤 예외든 fail-open 으로 감점 0 의 안전 dict 를 반환한다.
    """
    try:
        if not isinstance(content, str) or not content.strip():
            return {"score": 0.0, "level": "low", "signals": {}, "p0": [], "p1": [], "p2": []}
        return _score_impl(content, category or "")
    except Exception as exc:  # noqa: BLE001 — fail-open: 어떤 예외도 밖으로 전파 금지
        return {
            "score": 0.0,
            "level": "low",
            "error": str(exc),
            "signals": {},
            "p0": [],
            "p1": [],
            "p2": [],
        }
