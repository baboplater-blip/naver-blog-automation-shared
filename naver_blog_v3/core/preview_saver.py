# core/preview_saver.py
# 📄 업로드 전 HTML 미리보기 파일 저장 — 내용 확인용
from __future__ import annotations

import os, sys
from datetime import datetime
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.logger import get_logger
log = get_logger(__name__)
try:
    from config.settings import SAVE_PREVIEW_HTML, PREVIEW_DIR
except (ImportError, AttributeError):
    SAVE_PREVIEW_HTML = True
    PREVIEW_DIR       = "previews"

try:
    from config import profile as _profile
    PREVIEW_PATH = str(_profile.previews_dir())
except Exception:  # noqa: BLE001 — 프로필 라우터 실패 시 레거시 하드코딩 경로로 폴백
    PREVIEW_PATH = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        PREVIEW_DIR
    )
os.makedirs(PREVIEW_PATH, exist_ok=True)


def save_preview(post_data: dict, reserve_datetime: str, post_id: str) -> str:
    """업로드 전 HTML 미리보기 저장. 저장 경로 반환."""
    if not SAVE_PREVIEW_HTML:
        return ""

    title   = post_data.get("title", "제목없음")
    content = post_data.get("content_html", post_data.get("content", ""))
    tags    = post_data.get("tags", [])
    summary = post_data.get("summary", "")

    tag_html = " ".join(f'<span class="tag">#{t}</span>' for t in tags[:15])

    html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<title>[미리보기] {title}</title>
<style>
  body{{font-family:'Malgun Gothic',sans-serif;max-width:780px;margin:40px auto;
       padding:0 20px;color:#333;line-height:1.8;}}
  .meta{{background:#f0f4ff;border-radius:8px;padding:16px 20px;margin-bottom:28px;
         font-size:13px;color:#555;}}
  .meta strong{{color:#1a73e8;}}
  .reserve-badge{{display:inline-block;background:#e94560;color:white;
                  padding:4px 12px;border-radius:4px;font-weight:700;font-size:13px;}}
  .tag{{background:#e8f0fe;color:#1a73e8;padding:3px 10px;border-radius:999px;
        font-size:12px;margin:2px;display:inline-block;}}
  h2{{background:linear-gradient(90deg,#e8f0fe,#fff);padding:12px 16px;
      border-left:5px solid #1a73e8;border-radius:0 8px 8px 0;}}
  h3{{color:#1a73e8;padding-left:8px;border-left:3px solid #1a73e8;}}
  table{{width:100%;border-collapse:collapse;margin:16px 0;}}
  th{{background:#1a73e8;color:#fff;padding:10px;text-align:center;}}
  td{{padding:9px;border:1px solid #e0e0e0;text-align:center;}}
  .footer{{margin-top:40px;padding-top:16px;border-top:1px solid #eee;
           font-size:12px;color:#999;}}
</style>
</head>
<body>
<div class="meta">
  <div style="margin-bottom:8px;">
    <span class="reserve-badge">📅 예약발행</span>
    <strong style="margin-left:12px;">발행 예정: {reserve_datetime}</strong>
  </div>
  <div><strong>제목:</strong> {title}</div>
  <div><strong>요약:</strong> {summary}</div>
  <div style="margin-top:8px;">{tag_html}</div>
</div>

<article>
{content}
</article>

<div class="footer">
  미리보기 생성: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | ID: {post_id}
</div>
</body>
</html>"""

    filename = f"preview_{post_id}_{datetime.now().strftime('%H%M%S')}.html"
    filepath = os.path.join(PREVIEW_PATH, filename)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(html)

    # 실제 저장 경로를 그대로 출력한다(프로필별 격리 경로 — named 프로필이 examplebrand
    # previews 를 뒤지지 않게). 기존엔 "previews/{filename}" 하드코딩이라 위치를 오인케 했다.
    log.info(f"미리보기 저장: {filepath}")
    return filepath
