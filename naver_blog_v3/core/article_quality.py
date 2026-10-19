"""Topic-specific reader-goal checks for article drafting and QA rewrites.

These checks report visible signals only. They do not establish that a claim is
true, that a procedure works, or that the article is semantically complete.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from core.topic_policy import topic_mode_for


_CHECKS = {
    "general": (
        ("reader_question", "독자 질문", r"\?|어떻게|무엇|왜|언제|어디|누가|얼마나",
         "독자가 실제로 해결하려는 질문과 답을 대조하세요."),
        ("conditions", "적용 조건", r"경우|조건|상황|다만|반면|예외",
         "적용되는 조건과 예외를 근거에 맞게 구분하세요."),
        ("next_step", "다음 판단", r"다음|먼저|확인|선택|준비|문의|신청",
         "독자가 이어서 할 판단이나 행동을 구체적으로 적으세요."),
    ),
    "government_policy": (
        ("eligibility", "대상·자격", r"대상|자격|요건|해당|제외|가구|소득",
         "대상과 제외 조건을 공식 원문에서 확인하세요."),
        ("amount_deadline", "금액·기한", r"\d[\d,.]*\s*(?:원|만원|%|일|개월|년)|기한|마감",
         "금액과 날짜는 적용 기준일을 붙여 원문과 대조하세요."),
        ("application", "신청 절차", r"신청|접수|제출|방문|온라인|서류",
         "신청 경로와 필요한 절차를 공식 원문에서 확인하세요."),
    ),
    "it": (
        ("environment", "제품·버전·환경", r"버전|환경|운영체제|윈도우|macOS|리눅스|기기|모델",
         "적용 제품과 버전·실행 환경을 독자가 식별할 수 있게 적으세요."),
        ("reproduction", "재현 순서", r"재현|단계|순서|설정|실행|설치|입력|명령",
         "독자가 같은 조건으로 따라 할 절차를 확인하세요."),
        ("exceptions", "예외·실패 조건", r"오류|예외|실패|안 되|되지 않|제한|주의|해결",
         "실패 조건과 확인하지 못한 부분을 분리해 적으세요."),
    ),
    "travel": (
        ("place", "장소·위치", r"주소|위치|역|정류장|지역|주차|지도",
         "방문 장소와 접근 위치를 확인 가능한 수준으로 적으세요."),
        ("route", "이동 동선", r"이동|동선|도보|환승|출발|경로|먼저|다음",
         "출발점과 이동 순서 또는 그 정보가 없는 이유를 밝히세요."),
        ("time_cost", "운영 시점·비용", r"\d[\d,.]*\s*(?:원|만원|시|분|일)|운영시간|휴무|요금|예약",
         "운영·요금은 기준 시점과 변동 가능성을 원문에서 확인하세요."),
    ),
    "review": (
        ("experience_basis", "경험 근거", r"사용|이용|방문|써 보|경험|제공받|직접",
         "직접 경험은 실제 제공된 근거가 있을 때만 1인칭 사실로 적으세요."),
        ("comparison_basis", "비교 기준", r"비교|차이|기준|장점|단점|반면|대비",
         "비교 대상과 판단 기준을 분명히 하세요."),
        ("reader_fit", "적합한 독자", r"추천|적합|어울|잘 맞|불편|필요한 사람|경우",
         "누구에게 맞거나 맞지 않는지 관찰 가능한 이유와 함께 밝히세요."),
    ),
    "learning": (
        ("level", "현재 수준·목표", r"초급|기초|중급|고급|수준|목표|처음|경험",
         "독자가 어느 수준에서 시작하고 무엇을 익힐지 명시하세요."),
        ("prerequisites", "선행 조건", r"준비|선행|필요한 것|설치|기본|먼저 알아야",
         "필요한 도구나 사전 지식을 먼저 확인하세요."),
        ("sequence", "실행·확인 순서", r"단계|순서|연습|따라|확인|점검|풀어|복습",
         "실행 순서와 학습 결과를 스스로 확인하는 방법을 적으세요."),
    ),
}


def criteria(mode: str = "") -> List[Dict[str, str]]:
    """Return concrete reader-goal checks for the selected topic family."""
    selected = mode if mode in _CHECKS else (mode or "general")
    return [{"id": key, "label": label, "action": action}
            for key, label, _pattern, action in _CHECKS.get(selected, _CHECKS["general"])]


def prompt_block(title: Any = "", category: Any = "", *, content: Any = "",
                 mode: str = "") -> str:
    """Render topic checks and evidence safeguards for a writer prompt."""
    selected = mode or topic_mode_for(title, category)
    checks = criteria(selected)
    lines = ["[주제별 독자 질문 검수]"]
    for item in checks:
        lines.append(f"- {item['label']}: {item['action']}")
    if content:
        review = evaluate(title, content, category, mode=selected)
        if review["missing_actions"]:
            lines.append("[현재 본문에서 추가 확인이 필요한 항목]")
            lines.extend(f"- 확인 필요: {action}" for action in review["missing_actions"])
    lines.extend([
        "- 아래 확인 항목은 독자 관점의 점검표이며, 키워드나 생성 모델의 자기평가만으로 충족했다고 단정하지 마세요.",
        "- 확인되지 않은 경험·시험 결과·시간·가격·조건을 만들지 말고, 모르면 확인 필요 또는 정보 없음으로 표시하세요.",
    ])
    return "\n".join(lines)


def evaluate(title: Any, content: Any, category: Any = "", *, mode: str = "") -> Dict[str, Any]:
    """Report regex-visible signals and missing review actions without claiming truth."""
    selected = mode or topic_mode_for(title, category)
    # A title can identify a topic but cannot satisfy an article's reader needs.
    # Only inspect the body so a keyword-heavy heading does not hide omissions.
    body = str(content or "")
    specs = _CHECKS.get(selected, _CHECKS["general"])
    checks = []
    for key, label, pattern, action in specs:
        found = bool(re.search(pattern, body, flags=re.IGNORECASE))
        checks.append({"id": key, "label": label,
                       "status": "signal_present" if found else "needs_review",
                       "action": action})
    return {
        "topic": selected,
        "checks": checks,
        "signals_present": sum(item["status"] == "signal_present" for item in checks),
        "signals_total": len(checks),
        "missing_actions": [item["action"] for item in checks
                            if item["status"] == "needs_review"],
        "coverage": "visible_signals_only",
        "semantic_verified": False,
    }


__all__ = ["criteria", "prompt_block", "evaluate"]
