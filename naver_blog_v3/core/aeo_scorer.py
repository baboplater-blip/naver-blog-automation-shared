"""core/aeo_scorer.py — AEO (Answer Engine Optimization) 점수기 (2026-05-25).

네이버가 검색 결과 상단에 AI 생성 요약을 띄우는 변화에 맞춰, 글이 AI 답변에
인용되기 좋은 구조를 가졌는지 정량 평가한다. D.I.A.+ 와 나란히 사용 — D.I.A.+
는 검색 순위 가산을, AEO 는 AI 답변 인용 가능성을 본다.

평가 신호 (총 100점):
1. TL;DR / 한 줄 결론        (10점)  — 첫 단락에 ✅·📌·👉·"결론부터"·"한 줄로" 같은 요약 마커
2. 첫 문장 정의·답변          (15점)  — 도입부 첫 200자에 핵심 질문에 대한 직접 답변 흔적
3. 숫자·단위 명시            (15점)  — 본문 100자당 N개 이상 구체 수치 (만원·%·년 등)
4. 단계·절차 표현            (15점)  — "1단계"·"STEP 1"·"①"·"첫째" 5+
5. FAQ Q&A 구조              (15점)  — Q1/Q2 또는 ❓ 또는 의문문 H2/H3 5+
6. 비교/대비 표현            (10점)  — "vs"·"비교"·"차이" 마커 또는 비교 표
7. 결론 박스                  (10점)  — 본문 끝부분 "정리"·"핵심"·"마치며"·"한 줄로" 표현
8. 출처·근거                  (10점)  — 정부24/복지로/국세청 등 1차 출처 명시

각 신호는 임계값까지 비례 점수, 권장값 도달 시 만점. 임계 미달은 약점 항목으로
지목해 quality_validator 의 자동 재작성 프롬프트에 끼워 넣는다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List

DEFAULT_THRESHOLDS = {
    "tldr":        {"min": 1, "good": 1, "weight": 10},
    "lead_answer": {"min": 1, "good": 1, "weight": 15},
    "stats_density": {"min": 5, "good": 12, "weight": 15},
    "steps":       {"min": 3, "good": 5, "weight": 15},
    "faq":         {"min": 3, "good": 5, "weight": 15},
    "comparison":  {"min": 1, "good": 2, "weight": 10},
    "conclusion":  {"min": 1, "good": 1, "weight": 10},
    "sources":     {"min": 1, "good": 3, "weight": 10},
}

PASS_THRESHOLD = 70


@dataclass
class AEOResult:
    score: float
    grades: dict = field(default_factory=dict)
    weaknesses: List[str] = field(default_factory=list)
    counts: dict = field(default_factory=dict)

    def passed(self, threshold: int = PASS_THRESHOLD) -> bool:
        return self.score >= threshold

    def to_dict(self) -> dict:
        return {
            "score": round(self.score, 1),
            "passed": self.passed(),
            "grades": dict(self.grades),
            "weaknesses": list(self.weaknesses),
            "counts": dict(self.counts),
        }


# ─── 시그널 정규식 ────────────────────────────────────


_TLDR_RE = re.compile(
    r"(?:✅|📌|👉|🔥|⭐|▶️|💡)\s*"
    # 줄머리(^)·인용(>)·문장부호 뒤([.?!]) 모두 인정 — 도입 훅 뒤 "결론부터"도 잡히게.
    r"|(?:^|>|[.?!]\s*)\s*(?:한\s*줄(?:로|\s*결론)|결론부터|요약하자면|TL;DR|핵심만|"
    r"한\s*줄\s*요약)",
    re.MULTILINE,
)

_LEAD_QUESTION_RE = re.compile(
    r"(?:인가요|할까요|일까요|있나요|얼마|어떻게|언제|어디|왜|무엇|누가)\??",
)
# 도입부 내 키워드 + 단정 표현: "X는 Y입니다/좋습니다/됩니다…"
# ⚠️ "입니다/이다"만 인정하면 "~좋습니다·됩니다·받습니다" 같은 자연스러운 한국어 직접
#    답변을 놓친다(정부 문체 편향). 정중 종결 "~습니다" 일반형까지 인정한다.
_LEAD_DEFINITION_RE = re.compile(
    r"(?:은|는|이|가)\s+[^\.\n]{2,40}(?:입니다|이다|이며|으로\s+나뉘|에\s+해당|됩니다|습니다)",
)

# 통계 수치 (D.I.A.+ 와 별개로 AEO 가중치)
_STAT_NUMBER_RE = re.compile(
    r"\d[\d,\.]*\s*(?:만원|천원|억원|원|%|퍼센트|명|건|회|일|개월|년|배|점|%p)"
)

# 단계 표현 — 1단계, 1) , ①~⑩, 첫째/둘째/셋째
_STEP_MARKERS = (
    re.compile(r"(?:^|>)\s*(?:STEP\s*\d+|Step\s*\d+|\d+\s*단계)", re.IGNORECASE),
    re.compile(r"[①②③④⑤⑥⑦⑧⑨⑩]"),
    re.compile(r"(?:^|>)\s*(?:첫째|둘째|셋째|넷째|다섯째|여섯째|일곱째)"),
    # 마크다운 번호 리스트 - 안전한 매칭(맨 처음 또는 줄 시작)
    re.compile(r"(?:^|\n)\s*\d+\.\s+\S", re.MULTILINE),
)

# Q&A — Q1·Q2·❓·의문문 헤딩
# ⚠️ 줄머리 매칭은 반드시 (?:^|\n|>) + re.MULTILINE 로 — steps 마커와 동일 방식.
#    (과거 `(?:^|>)` + MULTILINE 없음 → 마크다운 "Q1."을 영원히 못 잡던 false-negative)
_FAQ_PATTERNS = (
    re.compile(r"(?:^|\n|>)\s*(?:Q\d*\.?|질문\s*\d*\.?|❓)", re.MULTILINE),
    re.compile(r"\?\s*</(?:h2|h3|h4|li|p|strong)>", re.IGNORECASE),
    # 마크다운 의문문 헤딩(## 얼마나 받나요?) — 도크스트링의 "의문문 H2/H3" 신호.
    re.compile(r"^#{2,4}\s+.*\?\s*$", re.MULTILINE),
)

_COMPARISON_RE = re.compile(
    r"\bvs\b|\bVS\b|비교|차이|반면|대조|어느\s*쪽|뭐가\s*다|뭐가\s*좋"
)

_CONCLUSION_RE = re.compile(
    r"(?:정리|마치며|핵심\s*정리|결론|한\s*줄\s*결론|"
    r"끝으로|요약하자면)",
)

_SOURCE_PATTERNS = (
    re.compile(r"data-source\s*=", re.IGNORECASE),
    re.compile(r"출처\s*[:：]"),
    re.compile(r"근거\s*[:：]"),
    re.compile(r"(?:정부24|복지로|국세청|국민연금공단|건강보험공단|"
               r"보건복지부|고용노동부|행정안전부|국토교통부|"
               r"중소벤처기업부|교육부|보훈처)"),
)


# ─── 개별 시그널 카운터 ────────────────────────────────────


def _strip_tags(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html or "")


def _count_signals(html: str) -> dict:
    if not isinstance(html, str):
        html = ""
    text = html
    plain = _strip_tags(html)
    plain_len = max(1, len(plain))

    # TL;DR
    tldr = len(_TLDR_RE.findall(text[:1500]))  # 도입부 위주

    # Lead answer — 도입부 첫 600자. ⚠️ HTML(text) 은 장황한 인라인 스타일이 창을 다
    # 먹어 실제 도입 문장이 밀려난다 → 태그 제거한 plain 기준으로 도입부를 본다.
    lead_chunk = plain[:600]
    lead_answer = (
        len(_LEAD_QUESTION_RE.findall(lead_chunk))
        + len(_LEAD_DEFINITION_RE.findall(lead_chunk))
    )

    # Stats density — 본문 1000자당 통계 수치 개수
    stat_count = len(_STAT_NUMBER_RE.findall(plain))
    # 정규화: per-1000 chars; min/good 임계값과 비교
    stats_per_1000 = stat_count * 1000 / plain_len if plain_len else 0
    # 임계값은 절대 카운트 기준이라 plain_len 보정 안 함 — 그냥 raw count

    # Steps
    steps = sum(len(pat.findall(text)) for pat in _STEP_MARKERS)

    # FAQ
    faq = sum(len(pat.findall(text)) for pat in _FAQ_PATTERNS)

    # Comparison
    comparison = len(_COMPARISON_RE.findall(text))

    # Conclusion (본문 끝 1500자 내)
    tail_chunk = text[-1500:] if len(text) > 1500 else text
    conclusion = len(_CONCLUSION_RE.findall(tail_chunk))

    # Sources
    sources = sum(len(pat.findall(text)) for pat in _SOURCE_PATTERNS)

    return {
        "tldr": tldr,
        "lead_answer": lead_answer,
        "stats_density": stat_count,
        "steps": steps,
        "faq": faq,
        "comparison": comparison,
        "conclusion": conclusion,
        "sources": sources,
        # 보조 메트릭 (디버깅용)
        "_stats_per_1000": round(stats_per_1000, 1),
    }


def _grade_signal(name: str, count: int, thresh: dict) -> tuple[float, str]:
    weight = thresh["weight"]
    min_n = thresh["min"]
    good_n = thresh["good"]

    if count <= 0:
        return 0.0, "absent"
    if count < max(1, min_n):
        denom = max(1, min_n)
        return weight * 0.4 * (count / denom), "below_min"
    if count >= good_n:
        return float(weight), "strong"
    if good_n > min_n:
        span = good_n - min_n
        pct = (count - min_n) / span
        return weight * (0.4 + 0.6 * pct), "good"
    return weight * 0.7, "good"


_WEAKNESS_MESSAGES = {
    "tldr": "도입부에 한 줄 결론 부재 — '✅ 한 줄로:', '결론부터:', '📌 핵심:' 같은 요약 박스 1개 추가",
    "lead_answer": "도입부 첫 600자에 직접 답변 부재 — '제도명은 ~입니다' 또는 '월 최대 ~원 받습니다' 형태로 한 문장 명시",
    "stats_density": "본문 통계 수치 {good}개 이상 권장 (현재 {count}개) — 구체 금액·%·기간 등 추가",
    "steps": "절차 단계 마커 {good}개 이상 권장 (현재 {count}개) — '1단계 / 2단계 / 3단계' 또는 '① ② ③' 형태로 명확화",
    "faq": "Q&A 구조 {good}개 이상 권장 (현재 {count}개) — 'Q1.', 'Q2.' 패턴으로 자주 묻는 질문 분명히",
    "comparison": "비교·대비 표현 {good}개 이상 권장 (현재 {count}개) — 'A vs B', '둘의 차이' 등 비교 섹션 추가",
    "conclusion": "결론 박스 부재 — 본문 끝에 '## 정리' 또는 '## 한 줄 결론' 섹션으로 핵심 1~2 문장 정리",
    "sources": "1차 출처 {good}개 이상 권장 (현재 {count}개) — 정부24·복지로 등 기관명 + 링크 명시",
}


_PROCEDURE_SKELETONS = {"step_by_step", "prep_and_do", "procedure", "procedure_completion"}


def _is_procedure(blueprint: dict) -> bool:
    """절차형 골격인가 — howto 장르이거나 skeleton.type 이 단계형이면 단계 마커가 유효."""
    if not isinstance(blueprint, dict):
        return False
    if blueprint.get("genre") == "howto":
        return True
    stype = str(blueprint.get("skeleton", {}).get("type", "")).lower()
    return stype in _PROCEDURE_SKELETONS or "step" in stype or "절차" in stype


def _applicable_signals(blueprint: dict) -> set:
    """설계도(장르)에 맞게 채점·약점 대상으로 삼을 AEO 신호 집합.

    비-info 장르(맛집·일상·리뷰)에 FAQ·정부출처·통계·단계·고정 결론헤더를 강제하면
    AI 티를 되살린다. genre_rules/skeleton 을 읽어 부적용 신호를 뺀다.

    항상 유지: tldr·lead_answer(answer-first)·comparison(전 장르 유용). 조건부:
      - sources:       force_sources 일 때만
      - stats_density: ymyl 일 때만
      - steps:         ymyl 이거나 절차형 골격일 때만
      - faq:           skeleton.faq_count>0 일 때만
      - conclusion:    ymyl 일 때만(비-info 는 골격이 마무리 형태를 소유·고정 헤더 금지)
    """
    keep = {"tldr", "lead_answer", "comparison"}
    gr = blueprint.get("genre_rules", {}) if isinstance(blueprint, dict) else {}
    sk = blueprint.get("skeleton", {}) if isinstance(blueprint, dict) else {}
    if gr.get("force_sources", True):
        keep.add("sources")
    if gr.get("ymyl", True):
        keep.add("stats_density")
        keep.add("conclusion")
    # steps(1단계/②③): **절차형 골격일 때만** 적용한다. 예전엔 모든 ymyl(info)에 일괄
    # 적용해, 비교형(B2)·답변선언형·오해교정 같은 비절차 info 골격이 "단계 없다"고 부당
    # 감점→자동보완 헛돎→AEO 하락(라이브 실측)했다. howto 등 절차형(_is_procedure)만 유지.
    if _is_procedure(blueprint):
        keep.add("steps")
    if int(sk.get("faq_count", 1) or 0) > 0:
        keep.add("faq")
    return keep


def score(html: str, *, thresholds: dict | None = None,
          blueprint: dict | None = None) -> AEOResult:
    """본문 HTML 의 AEO 점수를 계산. D.I.A.+ 와 별도 차원.

    blueprint 가 주어지면 장르에 맞는 신호만 채점하고 점수를 원래 만점 스케일로
    재정규화한다. **None(기본)이면 기존 동작과 바이트 동일**(회귀 0).
    """
    base_th = thresholds or DEFAULT_THRESHOLDS
    th = base_th
    if blueprint:
        keep = _applicable_signals(blueprint)
        th = {k: v for k, v in base_th.items() if k in keep}
    counts = _count_signals(html)

    total = 0.0
    grades: dict = {}
    weaknesses: List[str] = []

    for name, spec in th.items():
        cnt = counts.get(name, 0)
        pts, grade = _grade_signal(name, cnt, spec)
        total += pts
        grades[name] = grade
        if grade in ("absent", "below_min"):
            msg = _WEAKNESS_MESSAGES.get(name, f"{name} 보강 필요")
            weaknesses.append(msg.format(count=cnt, good=spec["good"], min=spec["min"]))

    # 신호를 뺀 경우 원래 만점(100) 스케일로 재정규화 — PASS_THRESHOLD(70) 의미 보존.
    if blueprint:
        max_all = sum(spec["weight"] for spec in base_th.values())
        max_eff = sum(spec["weight"] for spec in th.values())
        if 0 < max_eff < max_all:
            total = total * max_all / max_eff

    return AEOResult(score=total, grades=grades, weaknesses=weaknesses, counts=counts)


def format_weaknesses_for_prompt(result: AEOResult, *, limit: int = 5) -> str:
    """AEO 약점 리스트를 재작성 프롬프트에 끼울 한국어 문장으로 변환."""
    if not result.weaknesses:
        return ""
    items = result.weaknesses[:limit]
    bullet = "\n".join(f"- {w}" for w in items)
    # #6: "확률이 높아집니다"라는 검증 안 된 단정 대신, 실측 인용 관측이 쌓였으면 그 상관
    #     기반 안내를, 없으면 중립 문구를 붙인다(순환논증·허위 확신 제거). 관측 모듈 부재/
    #     실패 시 조용히 중립으로 폴백(라이브 무중단).
    try:
        from core.aeo_observability import citation_guidance
        guidance = citation_guidance()
    except Exception:  # noqa: BLE001
        guidance = ("AI요약 인용 상관은 실측 관측으로 검증됩니다(현재 축적 중).")
    return (
        f"[AEO 사전 점수 {result.score:.0f}/100 — AI 답변 인용 가능성 보강]\n"
        f"{bullet}\n"
        f"{guidance}"
    )


__all__ = [
    "AEOResult",
    "DEFAULT_THRESHOLDS",
    "PASS_THRESHOLD",
    "score",
    "format_weaknesses_for_prompt",
]
