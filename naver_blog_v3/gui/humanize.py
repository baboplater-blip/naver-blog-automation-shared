# gui/humanize.py — 기계어 → 사람 말 번역 (UX P0, 2026-07-13)
"""파이프라인이 남기는 기술 사유(detail 문자열)를 사용자 언어로 바꾼다.

원칙: 진단명(retries_exceeded, Jaccard 0.8)이 아니라 "무슨 일이 있었고,
무엇을 하면 되는지"를 말한다. 모르는 사유는 원문을 그대로 보여준다(정보 손실 0).
write_log(detail=...) 의 실제 문자열 패턴이 진실원본이다 — 새 사유가 생기면
여기에 한 줄 추가.
"""
from __future__ import annotations

import re

# (정규식, 사람 말) — 위에서부터 첫 매칭 사용
_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"중복\((\d+)%?\)"),
     "이미 발행한 글과 거의 같은 내용이라 건너뛰었어요"),
    (re.compile(r"검색량미달"),
     "찾는 사람이 적은 키워드라 건너뛰었어요"),
    (re.compile(r"제도포화|institution"),
     "같은 제도의 글이 이미 충분해서 건너뛰었어요"),
    (re.compile(r"quality_gate_blocked.*(금지|forbidden)|retries_exceeded"),
     "금지 표현을 반복해서 고치지 못해 발행하지 않았어요"),
    (re.compile(r"quality_gate_blocked"),
     "품질 기준에 못 미쳐 발행하지 않았어요"),
    (re.compile(r"retry_quality_gate_blocked"),
     "재시도했지만 품질 기준에 못 미쳐 발행하지 않았어요"),
    (re.compile(r"retry_success"), "재시도해서 발행에 성공했어요"),
    (re.compile(r"retry_failed"), "재시도했지만 실패했어요"),
    (re.compile(r"upload_failed"),
     "네이버에 올리는 중 문제가 생겼어요 (다음 실행에서 재시도)"),
    (re.compile(r"timeout|타임아웃"),
     "생성 시간이 너무 오래 걸려 건너뛰었어요"),
    (re.compile(r"login|로그인"), "네이버 로그인에 실패했어요"),
    (re.compile(r"dry_run"), "테스트 실행이라 실제로 올리지 않았어요"),
    (re.compile(r"^https?://"), "발행 완료"),
]


def humanize_detail(detail: str) -> str:
    """write_log 의 detail 문자열을 사람 말로. 모르는 사유는 원문 그대로."""
    d = (detail or "").strip()
    if not d:
        return "발행 완료"
    for pat, msg in _RULES:
        if pat.search(d):
            return msg
    return d


def estimate_run_minutes(new_posts: int, draft_posts: int) -> int:
    """실행 예상 소요(분). 실측 근거: 새 글 ≈ 생성+QA+이미지+간격 ~7분,
    보관본 ≈ 업로드+간격 ~2분. '약'을 붙여 표시할 것."""
    return max(1, round(new_posts * 7 + draft_posts * 2))


def format_minutes(minutes: int) -> str:
    """75 → '약 1시간 15분', 40 → '약 40분'."""
    h, m = divmod(max(0, int(minutes)), 60)
    if h and m:
        return f"약 {h}시간 {m}분"
    if h:
        return f"약 {h}시간"
    return f"약 {m}분"


# ── 발행 실패 코드 → 사람 말 (GUI 개편 2026-09-02) ──────────────────────
# gui/tabs/photo_post.py·product_post.py 의 _PUBLISH_ERROR_MSG 를 여기로
# 합치면서 "원인 + 다음 행동" 톤으로 다듬었다. 두 탭은 이 사전을
# publish_error_message() 로만 참조하게 될 예정(코더 F, gui/tabs/_publish_section.py).
PUBLISH_ERROR_MSG: dict = {
    "invalid_data": "발행할 글 데이터가 올바르지 않아요. 처음부터 다시 만들어 주세요.",
    "qa_failed": "품질 기준을 통과하지 못한 글이라 발행을 막았어요. 내용을 보완한 뒤 다시 시도하세요.",
    "no_category": "발행할 블로그 메뉴를 고르지 않았어요. 메뉴를 선택한 뒤 다시 시도하세요.",
    "category_unmapped": ("고른 메뉴를 블로그 메뉴 목록과 연결하지 못했어요. "
                          "엉뚱한 메뉴에 올라가지 않도록, 목록에서 메뉴를 직접 골라 다시 시도하세요."),
    "empty_title": "제목이 비어 있어요. 제목을 입력한 뒤 다시 시도하세요.",
    "empty_content": "본문이 비어 있어요. 내용을 채운 뒤 다시 시도하세요.",
    "stopped": "발행을 중지했어요.",
    "attach_failed": ("발행용 브라우저에 연결하지 못했어요. 세션 브라우저를 열고 "
                      "네이버 로그인을 확인한 뒤 다시 시도하세요."),
    "login_failed": "네이버 로그인에 실패했어요. 로그인 상태를 확인한 뒤 다시 시도하세요.",
    "upload_failed": "발행 중 문제가 생겨 올라가지 못했어요. 잠시 후 다시 시도하세요.",
    # 운영 정지 스위치 — 사용자가 직접 세워 둔 상태라 "실패"가 아니라 "막았다"로 말한다.
    "pause": ("이 계정은 지금 일시중지 상태라 발행하지 않았어요. "
              "무인 운영 화면에서 일시중지를 푼 뒤 다시 시도하세요."),
    "estop": ("전체 비상정지가 걸려 있어 발행하지 않았어요. "
              "비상정지를 해제한 뒤 다시 시도하세요."),
    # 같은 글을 두 번 올리는 것을 막은 경우 — 실패가 아니라 "이미 했을 수 있다".
    "publish_blocked": ("이 글은 이미 발행을 시도한 기록이 있어요. "
                        "블로그에서 올라갔는지 먼저 확인하세요."),
}


def publish_error_message(code: str, default: str = None) -> str:
    """발행 실패 코드(예: "qa_failed")를 사람 말로 바꾼다.

    모르는 코드는 default 가 주어지면 default, 없으면 원문 코드를 보존한
    문구를 돌려준다(정보 손실 0 — humanize_detail 과 같은 원칙).
    """
    msg = PUBLISH_ERROR_MSG.get(code)
    if msg is not None:
        return msg
    if default is not None:
        return default
    return f"발행에 실패했어요: {code}"


def summarize_log_entries(entries: list) -> dict:
    """logs/{date}.json 항목 → {"published", "skipped", "reasons": [(사람말, 건수)]}.

    reasons 는 실패 사유를 사람 말로 묶어 많은 순 정렬 — 요약 카드가
    "안 나간 글 2편(금지어 반복 1 · 중복 1)" 식으로 그대로 쓴다.
    """
    published = 0
    reasons: dict[str, int] = {}
    for e in entries or []:
        if not isinstance(e, dict):
            continue
        if e.get("success"):
            if (e.get("detail") or "") == "dry_run":
                continue   # 테스트는 발행 수에 안 센다
            published += 1
        else:
            msg = humanize_detail(e.get("detail") or "")
            reasons[msg] = reasons.get(msg, 0) + 1
    ordered = sorted(reasons.items(), key=lambda kv: -kv[1])
    return {"published": published,
            "skipped": sum(reasons.values()),
            "reasons": ordered}
