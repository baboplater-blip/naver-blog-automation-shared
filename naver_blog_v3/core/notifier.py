# core/notifier.py
# 📲 카카오톡 나에게 보내기 알림 (선택 기능)
from __future__ import annotations

import os, sys, json, urllib.request, urllib.parse, urllib.error
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.logger import get_logger
log = get_logger(__name__)
try:
    from config.settings import (
        USE_KAKAO_NOTIFY, KAKAO_ACCESS_TOKEN, NAVER_ID, HTTP_REQUEST_TIMEOUT,
    )
except (ImportError, AttributeError):
    USE_KAKAO_NOTIFY    = False
    KAKAO_ACCESS_TOKEN  = ""
    NAVER_ID            = ""
    HTTP_REQUEST_TIMEOUT = 10


def send_kakao(title: str, message: str, link: str = ""):
    """카카오톡 나에게 보내기 알림 발송"""
    if not USE_KAKAO_NOTIFY or not KAKAO_ACCESS_TOKEN:
        return

    try:
        template = {
            "object_type": "text",
            "text": f"[블로그 자동화]\n{message}",
            "link": {
                "web_url": link or f"https://blog.naver.com/{NAVER_ID}",
                "mobile_web_url": link or ""
            },
            "button_title": "블로그 확인"
        }
        data = urllib.parse.urlencode({
            "template_object": json.dumps(template, ensure_ascii=False)
        }).encode("utf-8")

        req = urllib.request.Request(
            "https://kapi.kakao.com/v2/api/talk/memo/default/send",
            data=data,
            headers={
                "Authorization": f"Bearer {KAKAO_ACCESS_TOKEN}",
                "Content-Type": "application/x-www-form-urlencoded"
            }
        )
        with urllib.request.urlopen(req, timeout=HTTP_REQUEST_TIMEOUT) as resp:
            result = json.loads(resp.read())
            if result.get("result_code") == 0:
                log.info("카카오 알림 전송 완료")
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as e:
        log.warning(f"카카오 알림 실패: {e}")


def notify_success(title: str, reserve_time: str, post_count: int, total: int):
    send_kakao(
        title,
        f"✅ 예약발행 등록 완료 ({post_count}/{total})\n"
        f"📌 제목: {title[:30]}...\n"
        f"⏰ 발행 예정: {reserve_time}"
    )


def notify_daily_summary(success: int, total: int, date: str,
                         drafts_used: int = 0, skipped_detail: str = "",
                         publication_outcomes: dict | None = None):
    """실행 종료 한 줄 요약 (UX P1 — 사람 말, 조용한 실패 0).

    skipped_detail: "금지어 1 · 중복 1" 형태의 사람 말 사유 요약(없으면 생략).
    기존 호출부(위치 인자 3개)와 하위 호환.
    """
    if publication_outcomes is not None:
        registered = publication_outcomes.get("registered", success)
        parts = [f"예약 등록 {registered}편"]
        if drafts_used:
            parts[0] += f" (보관본 {drafts_used})"
        for key, label in (("failed", "실제 실패"), ("unconfirmed", "확인 필요"),
                           ("duplicate", "중복 건너뜀"),
                           ("other_skipped", "기타 보류/미시도")):
            count = publication_outcomes.get(key, 0)
            if count:
                parts.append(f"{label} {count}편")
        line = " · ".join(parts)
        if skipped_detail and publication_outcomes.get("other_skipped", 0):
            line += f" ({skipped_detail})"
        send_kakao("오늘 요약", f"📊 [{date}]\n{line}")
        return
    skipped = max(total - success, 0)
    line = f"✅ {success}편 발행"
    if drafts_used:
        line += f" (보관본 {drafts_used})"
    line += f" · 안 나간 글 {skipped}편" if skipped else " · 문제 없음"
    if skipped and skipped_detail:
        line += f" ({skipped_detail})"
    send_kakao("오늘 요약", f"📊 [{date}]\n{line}")


def notify_failure(title: str, reason: str):
    send_kakao(
        title,
        f"❌ 업로드 실패\n"
        f"📌 제목: {title[:30]}...\n"
        f"🔎 이유: {reason[:50]}"
    )
