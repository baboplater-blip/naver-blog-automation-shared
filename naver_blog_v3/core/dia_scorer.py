"""core/dia_scorer.py — 네이버 D.I.A.+ 사전 점수 계산기 (2026-05-25).

네이버 검색 상위 노출 가산 신호를 점수화한다. quality_validator 가 글자수·표·
FAQ 기본 검증을 끝낸 뒤, 이 모듈이 추가로 D.I.A.+ 가산 영역을 평가해 약점을
지목한다. 점수 < THRESHOLD 면 quality-reviewer 가 재작성 사유로 활용한다.

평가 신호 (총 90점):
1. 이미지 수            (figure 또는 img 태그)            20점
2. 표 개수              (<table>)                          15점
3. 리스트 다양성        (<ul>, <ol>, <li>)                 15점
4. 통계 수치 밀도       (% / 만원 / 원 / 단위 숫자)        15점
5. 외부 출처            (data-source / 출처: / source)     15점
6. FAQ 개수             ("자주 묻는" / "FAQ" / Q&A)         10점

각 신호의 점수는 임계값과 단조 증가 함수로 산출 — 최소 기준을 충족하면
부분 점수, 권장값 도달 시 만점. 임계값 미달이면 약점 리스트에 항목 추가.

⚠️ 동영상 임베드(2026-08-02 제거)
  원래 7번째 신호(10점)로 `<iframe>/<video>` 를 봤으나, **우리 발행 파이프라인은
  동영상 임베드를 만들지 않는다**(네이버 에디터 삽입 경로 자체가 없음). 그래서 이
  신호는 영원히 0점이면서 "동영상 임베드 1개 권장" 약점만 매번 재생성 프롬프트로
  흘려보냈고, 모델이 그 지시에 반응해 아무도 처리하지 않는 `<!-- VIDEO: ... -->`
  주석을 본문에 심었다(라이브 10편 중 4편 실측).
  가중치 0 대신 **항목째 삭제**를 택한 이유: 가중치만 0으로 두면 grade "absent" 가
  남아 약점 문구가 계속 나가므로 두 곳을 손봐야 하고, 죽은 항목이 남는다.
  점수 영향: 동영상이 없는 글은 이 항목에서 원래 0점이었으므로 **점수 변화 0**
  (보관본 1,807편 실측 videos>0 = 0편). 만점만 100 → 90 으로 줄어 PASS_THRESHOLD(70)
  가 상대적으로만 엄격해진다(70/100 → 70/90). 과거 qa.json 의 dia_score 와도
  숫자가 그대로라 weekly_report 평균이 튀지 않는다.
  counts["videos"] 는 관측용으로 계속 집계한다(채점·약점에는 미반영).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List

# 기본 임계값 — 카테고리별 오버라이드는 향후 추가 가능
DEFAULT_THRESHOLDS = {
    "images": {"min": 1, "good": 6, "weight": 20},
    "tables": {"min": 1, "good": 2, "weight": 15},
    "lists": {"min": 1, "good": 3, "weight": 15},
    "stats": {"min": 3, "good": 8, "weight": 15},  # 본문 내 수치 등장 횟수
    "sources": {"min": 1, "good": 3, "weight": 15},
    # "videos" 는 2026-08-02 제거 — 모듈 docstring 참고(발행 파이프라인 미지원).
    "faqs": {"min": 1, "good": 3, "weight": 10},
}

PASS_THRESHOLD = 70  # 70점 이상이면 D.I.A.+ PASS (만점 MAX_SCORE 기준)

# 만점 — 로그·프롬프트가 "N/만점" 을 하드코딩하지 않게 가중치 합에서 파생시킨다.
# (신호를 더하거나 빼면 여기서 자동으로 따라온다.)
MAX_SCORE = sum(spec["weight"] for spec in DEFAULT_THRESHOLDS.values())


@dataclass
class DIAResult:
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


# ─── 개별 시그널 카운터 ────────────────────────────────────────


_IMG_RE = re.compile(r"<(?:figure[\s>]|img\b)", re.IGNORECASE)
_TABLE_RE = re.compile(r"<table\b", re.IGNORECASE)
# 리스트: <ul>/<ol> 래퍼뿐 아니라 개별 <li> 도 센다 — markdown_to_naver_html 은 리스트를
# 스타일된 <li>(래퍼 없이)로 만들어, ul/ol 만 찾으면 리스트 15점을 통째로 놓쳤다.
_LIST_RE = re.compile(r"<(?:ul|ol|li)\b", re.IGNORECASE)
_IFRAME_RE = re.compile(r"<(?:iframe|video)\b", re.IGNORECASE)

# 통계 수치: 숫자 + 단위 (만원, 원, %, 명, 건, 회 등)
_STAT_NUMBER_RE = re.compile(
    r"\d[\d,\.]*\s*(?:만원|천원|억원|원|%|퍼센트|명|건|회|일|개월|년|배)"
)

# 외부 출처 표기 휴리스틱 — 정부기관뿐 아니라 일반 인용(링크·참고·자료)도 인정해
# 비정부 분야(영어교육·요리 등) 글도 출처 신호를 받을 수 있게 한다.
_SOURCE_PATTERNS = (
    re.compile(r"data-source\s*=", re.IGNORECASE),
    re.compile(r"출처\s*[:：]"),
    re.compile(r"source\s*[:：]", re.IGNORECASE),
    re.compile(r"https?://"),                              # 인용 링크
    re.compile(r"(?:참고|자료|근거|기준)\s*[:：]"),         # 일반 인용 표기
    re.compile(r"(?:정부24|복지로|국세청|국민연금|보건복지부|고용노동부|행정안전부)"),
)

# FAQ 섹션 휴리스틱 — 질문 패턴(Q. / "?")
_FAQ_QUESTION_RE = re.compile(r"(?:^|>)\s*(?:Q\d*\.?|질문\s*\d*\.?|❓)")
_QMARK_END_RE = re.compile(r"\?\s*</(?:h2|h3|h4|li|p)>", re.IGNORECASE)


def _count_signals(html: str) -> dict:
    """본문 HTML 에서 각 신호 카운트."""
    if not isinstance(html, str):
        html = ""
    text = html

    counts = {
        "images": len(_IMG_RE.findall(text)),
        "tables": len(_TABLE_RE.findall(text)),
        "lists": len(_LIST_RE.findall(text)),
        # videos 는 채점 대상이 아니다(DEFAULT_THRESHOLDS 에서 제거) — 관측용으로만 집계.
        "videos": len(_IFRAME_RE.findall(text)),
        "stats": len(_STAT_NUMBER_RE.findall(text)),
        "sources": sum(len(p.findall(text)) for p in _SOURCE_PATTERNS),
    }
    # FAQ 개수 — 질문 패턴 + 의문문 종결
    counts["faqs"] = len(_FAQ_QUESTION_RE.findall(text)) + len(_QMARK_END_RE.findall(text))
    return counts


def _grade_signal(name: str, count: int, thresh: dict) -> tuple[float, str]:
    """단일 신호의 부분 점수와 등급 라벨을 반환.

    Returns:
        (점수, 등급) — 등급은 "absent"/"min"/"good"/"strong" 중 하나.
    """
    weight = thresh["weight"]
    min_n = thresh["min"]
    good_n = thresh["good"]

    # 0개는 항상 absent (점수 0). min=0 인 옵셔널 신호도 동일하게 취급.
    if count <= 0:
        return 0.0, "absent"
    if count < max(1, min_n):
        # min 미달 — 비례 (0~weight*0.4)
        denom = max(1, min_n)
        return weight * 0.4 * (count / denom), "below_min"
    if count >= good_n:
        return float(weight), "strong"
    # min~good 사이 — 비례
    if good_n > min_n:
        span = good_n - min_n
        pct = (count - min_n) / span
        return weight * (0.4 + 0.6 * pct), "good"
    return weight * 0.7, "good"


_WEAKNESS_MESSAGES = {
    "images": "이미지 {good}장 이상 권장 (현재 {count}장) — figure 슬롯 보강",
    "tables": "표 {good}개 이상 권장 (현재 {count}개) — 수치 비교 표 추가",
    # ⚠️ "체크리스트"를 권하지 않는다 — 생성 프롬프트의 [금지 사항]이 체크리스트(□ ☑)를
    #    AI 티·장식으로 금지하고 있어, 두 시스템이 정반대를 지시하던 충돌이었다.
    #    번호 절차("1단계/2단계")는 절차 안내 글에서 실제로 읽기 편해 그대로 권한다.
    "lists": "리스트 {good}개 이상 권장 (현재 {count}개) — 번호 절차(1단계/2단계)·항목 나열 추가",
    "stats": "통계 수치 {good}개 이상 권장 (현재 {count}개) — 금액·% 등 구체 수치",
    "sources": "외부 출처 {good}개 이상 권장 (현재 {count}개) — 정부24/복지로 등 명시",
    # "videos" 약점 문구는 신호와 함께 제거(2026-08-02) — 처리 불가능한 요구였다.
    "faqs": "FAQ {good}문항 이상 권장 (현재 {count}문항) — Q&A 섹션 보강",
}


def _applicable_signals(blueprint: dict) -> set:
    """설계도(장르)에 맞게 채점·약점 대상으로 삼을 D.I.A.+ 신호 집합.

    비-info 장르에 정부글 기준(출처·통계·표·FAQ)을 강제하면 맛집·일상 글에 AI 티를
    되살린다(라이브 실측). genre_rules/skeleton 을 읽어 부적용 신호를 뺀다.
    blueprint 가 없거나 파싱 실패면 호출측이 전체 신호를 쓴다(이 함수는 호출 안 됨).

    항상 유지: images·lists(전 장르 유용). 조건부:
      - sources: force_sources 일 때만(비-info 는 정부 출처 강제 부적절)
      - stats:   ymyl 일 때만(카페 후기에 통계 강제는 장르 오염)
      - tables:  skeleton.table_allowed 일 때만
      - faqs:    skeleton.faq_count>0 일 때만(FAQ 비요구 골격은 감점 안 함)
    """
    keep = {"images", "lists"}
    gr = blueprint.get("genre_rules", {}) if isinstance(blueprint, dict) else {}
    sk = blueprint.get("skeleton", {}) if isinstance(blueprint, dict) else {}
    if gr.get("force_sources", True):
        keep.add("sources")
    if gr.get("ymyl", True):
        keep.add("stats")
    if sk.get("table_allowed", True):
        keep.add("tables")
    if int(sk.get("faq_count", 1) or 0) > 0:
        keep.add("faqs")
    return keep


def score(html: str, *, thresholds: dict | None = None,
          blueprint: dict | None = None) -> DIAResult:
    """본문 HTML 의 D.I.A.+ 가산 신호 점수를 계산.

    Args:
        html: 본문 HTML 텍스트.
        thresholds: 임계값 dict 오버라이드. 기본 DEFAULT_THRESHOLDS 사용.
        blueprint: 설계도 주도 변주 dict(선택). 주어지면 장르에 맞는 신호만 채점하고
            점수를 원래 만점 스케일로 재정규화한다. **None(기본)이면 기존 동작과
            바이트 동일**(회귀 0).

    Returns:
        DIAResult — score, grades, weaknesses, counts.
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

    # 신호를 뺀 경우 남은 만점이 줄어드므로 원래 만점 스케일로 재정규화한다 —
    # PASS_THRESHOLD(70) 이 장르와 무관하게 같은 의미를 갖게(적용 신호를 다 채우면 만점).
    if blueprint:
        max_all = sum(spec["weight"] for spec in base_th.values())
        max_eff = sum(spec["weight"] for spec in th.values())
        if 0 < max_eff < max_all:
            total = total * max_all / max_eff

    return DIAResult(score=total, grades=grades, weaknesses=weaknesses, counts=counts)


def format_weaknesses_for_prompt(result: DIAResult, *, limit: int = 5) -> str:
    """약점 리스트를 재작성 프롬프트에 끼울 한국어 문장으로 변환."""
    if not result.weaknesses:
        return ""
    items = result.weaknesses[:limit]
    bullet = "\n".join(f"- {w}" for w in items)
    return (
        f"[D.I.A.+ 사전 점수 {result.score:.0f}/{MAX_SCORE} — 약점 보완]\n"
        f"{bullet}\n"
        f"위 항목을 본문에 자연스럽게 반영하면 네이버 상위 노출 가산이 증가합니다."
    )


__all__ = [
    "DIAResult",
    "DEFAULT_THRESHOLDS",
    "PASS_THRESHOLD",
    "MAX_SCORE",
    "score",
    "format_weaknesses_for_prompt",
]
