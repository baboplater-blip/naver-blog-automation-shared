# core/welfare_reference.py
# 보건복지부 2026년 확정 복지 수치 참조표 + 재생성 앵커 힌트 생성기.
# ─────────────────────────────────────────────────────
# fact_gate 가 '허구 수치'·'수치 모순'으로 발행을 막은 복지/기초생활수급/급여 글을
# 재생성할 때, 모델이 다시 지어내지 않도록 **공식 확정값**을 프롬프트에 앵커로 꽂는다.
#
# 핵심 교정 포인트(레드팀·현장 오류 1순위):
#   '선정기준'(지원 대상을 가르는 소득 상한, 예: 주거급여 1인 가구 123만원)과
#   '실제 지급액'(매달 받는 금액, 예: 주거급여 기준임대료 서울 1인 월 최대 36.9만원)은
#   전혀 다른 개념인데, 모델이 선정기준 금액을 지급액인 것처럼 쓰는 사고가 잦다.
#
# 설계:
#   · 순수 함수(파일 I/O·네트워크·Claude 호출 0) — 결정적·오프라인.
#   · 값은 하드코딩하되 year/source 를 명시(유지보수용). 갱신 시 복지로·보건복지부 확인.
#   · anchor_block 은 카테고리·실패 사유가 복지/급여 관련일 때만 블록을 낸다.
#     관련 없거나 앵커가 없으면 **빈 문자열**을 반환한다(날조 유도 금지).
from __future__ import annotations

from typing import List, Optional

# ─────────────────────────────────────────────────────
# 2026년 확정 앵커값 (출처: 보건복지부 고시, 갱신 시 복지로/보건복지부 재확인)
# ─────────────────────────────────────────────────────
# ⚠️ 2026 기준 — 매년 8월경 다음 연도 기준 중위소득이 고시되면 이 표를 갱신한다.
_YEAR = 2026
_SOURCE = "보건복지부"

# 기준 중위소득(1인 가구) — 급여별 선정기준의 모수.
_STANDARD_MEDIAN_INCOME_1P = 2_564_238  # 월, 1인 가구

# 급여별 선정기준(1인 가구, 소득 상한). '선정기준'이지 '지급액'이 아니다.
#   won: 월 소득 상한(원), pct: 기준 중위소득 대비 비율.
WELFARE_ANCHORS_2026 = {
    "기준중위소득": {
        "won": _STANDARD_MEDIAN_INCOME_1P,
        "label": "기준 중위소득(1인 가구)",
        "suffix": "",
        "keywords": ("기준 중위소득", "기준중위소득", "중위소득"),
        "year": _YEAR,
        "source": _SOURCE,
    },
    "생계급여": {
        "won": 820_556,             # 기준 중위소득 32%
        "label": "생계급여 선정기준(1인 가구, 기준 중위소득 32%)",
        "suffix": " 이하(소득 상한)",
        "keywords": ("생계급여",),
        "year": _YEAR,
        "source": _SOURCE,
    },
    "의료급여": {
        "won": 1_025_695,           # 기준 중위소득 40%
        "label": "의료급여 선정기준(1인 가구, 기준 중위소득 40%)",
        "suffix": " 이하(소득 상한)",
        "keywords": ("의료급여",),
        "year": _YEAR,
        "source": _SOURCE,
    },
    "주거급여": {
        "won": 1_230_834,           # 기준 중위소득 48%
        "label": "주거급여 선정기준(1인 가구, 기준 중위소득 48%)",
        "suffix": " 이하(소득 상한)",
        "keywords": ("주거급여",),
        "year": _YEAR,
        "source": _SOURCE,
    },
    "교육급여": {
        "won": 1_282_119,           # 기준 중위소득 50%
        "label": "교육급여 선정기준(1인 가구, 기준 중위소득 50%)",
        "suffix": " 이하(소득 상한)",
        "keywords": ("교육급여",),
        "year": _YEAR,
        "source": _SOURCE,
    },
}

# 주거급여 '실제 지급액'(기준임대료) — 선정기준(위 123만원)과 완전히 다른 개념.
#   1급지(서울) 1인 가구 월 최대치. 지급액은 급지·가구원수로 달라지므로 대표값만.
HOUSING_BENEFIT_PAYOUT_2026 = {
    "won": 369_000,                 # 1급지(서울), 1인 가구 월 최대
    "label": "주거급여 실제 지급액(기준임대료, 1인 가구·1급지 서울)",
    "note": "급지·가구원수에 따라 달라짐 — 이 값은 서울 1인 월 최대치",
    "year": _YEAR,
    "source": _SOURCE,
}

# anchor 가 적용되는 카테고리 — 기초생활보장 급여가 등장할 수 있는 복지 계열만.
# 이 목록 밖(디지털·환경 등)에서는 anchor_block 이 항상 빈 문자열을 낸다.
_WELFARE_CATEGORIES = frozenset({
    "복지", "주거", "의료", "교육", "노후", "청년", "바우처",
})


def is_welfare_category(category: Optional[str]) -> bool:
    """카테고리가 복지/기초생활수급/급여 계열인지 여부(앵커 적용 대상)."""
    if not category or not isinstance(category, str):
        return False
    return category.strip() in _WELFARE_CATEGORIES


def _matched_anchor_keys(haystack: str) -> List[str]:
    """본문/실패 사유 텍스트에서 언급된 앵커 키를 WELFARE_ANCHORS_2026 순서대로 반환.

    바른 앵커만 꽂기 위해 **구체 급여명**(생계급여·주거급여 등)으로만 매칭한다.
    (바른 카테고리 안이라도 '교육'·'주거' 같은 광의어로 매칭하면 국가장학금 글에
    교육급여 수치를 잘못 주입할 수 있으므로 구체어만 트리거로 쓴다.)
    """
    if not haystack:
        return []
    matched: List[str] = []
    for key, anchor in WELFARE_ANCHORS_2026.items():
        if any(kw in haystack for kw in anchor["keywords"]):
            matched.append(key)
    return matched


def anchor_block(
    category: str,
    failures: Optional[List[str]] = None,
    content: Optional[str] = None,
) -> str:
    """재생성 프롬프트에 꽂을 '공식 수치로 정정하라' 앵커 블록을 만든다.

    Args:
        category: 글 카테고리. 복지/급여 계열이 아니면 빈 문자열 반환.
        failures: fact_gate/QA 실패 사유 문자열 목록(어떤 수치가 문제였는지 단서).
        content: (선택) 본문. 어떤 급여가 다뤄지는지 판별하는 보조 단서.

    Returns:
        해당 급여의 공식 확정값 + '선정기준 vs 실지급액' 구분 지침을 담은 한국어
        프롬프트 블록. 카테고리가 무관하거나 관련 앵커가 하나도 없으면 **빈 문자열**
        (없는 근거를 지어내게 유도하지 않는다).
    """
    if not is_welfare_category(category):
        return ""

    parts = list(failures or [])
    if content:
        parts.append(content)
    haystack = " ".join(p for p in parts if p)
    if not haystack.strip():
        return ""

    matched = _matched_anchor_keys(haystack)
    if not matched:
        return ""

    lines = [
        f"[공식 수치 — 아래 {_SOURCE} {_YEAR}년 확정값으로 반드시 정정하세요]",
        "지어낸 환산·모의계산 값을 쓰지 말고, 관련 수치는 아래 고시값과 정확히 일치시키세요.",
        "",
    ]
    for key in matched:
        anchor = WELFARE_ANCHORS_2026[key]
        lines.append(f"- {anchor['label']}: 월 {anchor['won']:,}원{anchor['suffix']}")

    lines.append("")
    lines.append("★ '선정기준'과 '실제 지급액'은 다른 개념입니다 — 절대 혼동하지 마세요:")
    lines.append("  · 선정기준 = 지원 대상을 가르는 소득 상한선입니다(위 금액은 소득 기준).")
    if "주거급여" in matched:
        payout = HOUSING_BENEFIT_PAYOUT_2026
        lines.append(
            f"  · 실제 지급액 = 매달 받는 금액입니다. 예: 주거급여(기준임대료)는 "
            f"1인 가구·1급지(서울) 기준 월 최대 {payout['won']:,}원입니다."
        )
        lines.append(
            f"  · 주거급여 선정기준({WELFARE_ANCHORS_2026['주거급여']['won']:,}원, 소득 상한)을 "
            f"매달 받는 지급액인 것처럼 쓰지 마세요."
        )
    else:
        lines.append(
            "  · 실제 지급액 = 매달 받는 금액이며 선정기준(소득 상한)과 다릅니다. "
            "선정기준 금액을 지급액인 것처럼 쓰지 마세요."
        )
    return "\n".join(lines)


__all__ = [
    "WELFARE_ANCHORS_2026",
    "HOUSING_BENEFIT_PAYOUT_2026",
    "is_welfare_category",
    "anchor_block",
]
