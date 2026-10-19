# core/series_planner.py
# 시리즈 글 자동 구성 시스템
# 관련 키워드를 묶어서 연재형 콘텐츠를 자동으로 기획하고
# 각 글에 이전편/다음편 네비게이션을 자동으로 삽입합니다.
# 네이버는 시리즈 콘텐츠를 알고리즘으로 우대합니다.
from __future__ import annotations

import json, os, re, sys, subprocess, uuid
from datetime import datetime, date
from pathlib import Path

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.logger import get_logger
from core.search_volume import recommend_episode_count
try:
    from config.settings import SUBPROCESS_TIMEOUT
except (ImportError, AttributeError):
    SUBPROCESS_TIMEOUT = 180
try:
    from config.settings import SERIES_MAX_EPISODES
except (ImportError, AttributeError):
    SERIES_MAX_EPISODES = 3   # 시리즈 팬아웃 상한 기본값(광역 대형 키워드도 최대 3편)
log = get_logger(__name__)

BASE_DIR     = Path(__file__).parent.parent
from config import profile as _nbaprofile
_DATA_DIR = _nbaprofile.app_data_root()

SERIES_DB    = _DATA_DIR / "logs" / "series_data.json"
HISTORY_PATH = _DATA_DIR / "logs" / "post_history.json"


# ──────────────────────────────────────────────────────
# DB 관리
# ──────────────────────────────────────────────────────
def _load_series_db() -> dict:
    """
    {
      "sid_abc123": {
        "id":          "sid_abc123",
        "title":       "2026 청년 지원 완전 정복",
        "description": "시리즈 설명",
        "category":    "청년",
        "created_at":  "2026-03-05",
        "episodes": [
          {
            "ep":       1,
            "keyword":  "청년도약계좌 신청방법",
            "title":    "발행된 최종 제목",
            "url":      "",
            "posted_at":"",
            "status":   "pending"|"published"
          }, ...
        ]
      }
    }
    """
    from config import safe_load_json
    return safe_load_json(SERIES_DB, default=dict)

def _save_series_db(data: dict):
    from config import atomic_write_json
    atomic_write_json(SERIES_DB, data)


# ──────────────────────────────────────────────────────
# Claude로 시리즈 계획 생성
# ──────────────────────────────────────────────────────
def create_series_plan(base_keyword: str,
                       episode_count: int | None = None,
                       existing_keywords: list[str] = None,
                       progress_cb=None) -> dict | None:
    """
    메인 키워드를 중심으로 N편짜리 시리즈 계획을 Claude로 생성합니다.

    Args:
        base_keyword:      시리즈의 핵심 주제 키워드(광역 키워드)
        episode_count:     총 편수. None 이면 광역 키워드 검색량 볼륨으로
                           자동 결정(대형→3·중형→2·소형/무매칭→1). 명시적으로
                           넘긴 값도 SERIES_MAX_EPISODES 로 상한 캡한다.
        existing_keywords: 이미 엑셀에 있는 관련 키워드 (재활용)
    Returns:
        { "id": ..., "title": ..., "episodes": [...] }  or None
    """
    # 편수 결정: 미지정 시 광역 키워드 볼륨 기반 자동, 지정 시에도 상한 캡.
    # (과잉 분해로 검색 수요 없는 소주제가 양산되는 것을 원천 차단)
    if episode_count is None:
        episode_count = recommend_episode_count(base_keyword, max_ep=SERIES_MAX_EPISODES)
        log.info(f"시리즈 편수 자동 결정: '{base_keyword}' → {episode_count}편 (상한 {SERIES_MAX_EPISODES})")
    else:
        capped = min(episode_count, SERIES_MAX_EPISODES)
        if capped != episode_count:
            log.info(f"시리즈 편수 상한 적용: {episode_count} → {capped}편 (상한 {SERIES_MAX_EPISODES})")
        episode_count = capped

    from datetime import date as _date
    year = _date.today().year

    existing_str = ""
    if existing_keywords:
        existing_str = f"""
[기존 엑셀에 있는 관련 키워드 — 가능하면 이것들을 편에 포함]
{chr(10).join(f'- {k}' for k in existing_keywords[:10])}
"""

    # 편수에 맞춰 상위 수요 앵글만 기획하도록 골격을 적응화한다.
    # (편수 채우기용 무수요 소주제 생성을 막고, 검색 수요가 큰 앵글부터 담는다)
    if episode_count <= 1:
        structure_hint = (
            "이 시리즈는 단 1편입니다. 편수를 억지로 늘리지 마세요.\n"
            f"'{base_keyword}'에서 사람들이 가장 많이 검색하는 핵심 수요"
            "(보통 자격·신청방법·금액/혜택)를 한 편에 종합하는 허브 글로 기획하세요."
        )
    else:
        structure_hint = (
            f"이 시리즈는 총 {episode_count}편입니다. 편수를 억지로 채우지 말고, "
            f"'{base_keyword}'에서 검색 수요가 큰 앵글부터 상위 {episode_count}개만 고르세요.\n"
            "수요 우선순위(위에서부터): 자격/조건 → 신청방법 단계별 → 금액/혜택 상세.\n"
            "주의사항·FAQ·변경사항·유사 제도 비교 같은 저수요 앵글은 "
            "상위 앵글이 부족할 때만, 그것도 실제 검색 수요가 있을 때만 넣으세요."
        )

    prompt = f"""당신은 한국 블로그 콘텐츠 전략가입니다.
아래 주제로 {episode_count}편짜리 연재 시리즈를 기획해주세요.
{existing_str}
메인 주제: {base_keyword}

[시리즈 기획 원칙]
1. 각 편은 독립적으로 읽을 수 있지만 연결성이 있어야 함
2. {year}년 현재 사람들이 실제로 검색하는 구체적 키워드만 다룸
3. 각 편의 키워드는 롱테일 (3단어 이상, 검색량 500~3,000)
4. 검색 수요가 없는 소주제로 편수를 채우지 말 것 (무수요 앵글 금지)
5. 시리즈 제목은 "2026 {base_keyword} 완전 정복" 스타일

[편성 지침]
{structure_hint}

정확히 {episode_count}개의 편만 생성하세요.
반드시 JSON 형식으로만 응답 (코드블록 없이):
{{
  "series_title": "시리즈 전체 제목 (예: 2026 {base_keyword} 완전 정복 시리즈)",
  "description":  "시리즈 한 줄 소개 (60자 이내)",
  "episodes": [
    {{
      "ep":      1,
      "keyword": "롱테일 키워드 (포스팅 제목으로 바로 사용 가능)",
      "angle":   "이 편에서 다루는 핵심 포인트 (30자 이내)"
    }},
    ...
  ]
}}"""

    if progress_cb:
        progress_cb("Claude로 시리즈 계획 생성 중...", 1, 3)

    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)
    try:
        result = subprocess.run(
            ["claude", "-p", prompt, "--output-format", "text"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=SUBPROCESS_TIMEOUT, env=env,
        )
        if result.returncode != 0 or not result.stdout.strip():
            log.warning(f"Claude 오류: {result.stderr[:80]}")
            return None

        raw = result.stdout.strip()
        raw = re.sub(r'^```[a-z]*\n?', '', raw, flags=re.MULTILINE)
        raw = re.sub(r'\n?```$',       '', raw, flags=re.MULTILINE).strip()
        m   = re.search(r'\{[\s\S]*\}', raw)
        plan = json.loads(m.group()) if m else json.loads(raw)
    except (subprocess.TimeoutExpired, FileNotFoundError, subprocess.CalledProcessError) as e:
        log.warning(f"시리즈 계획 생성 오류: {e}")
        return None
    except json.JSONDecodeError as e:
        log.warning(f"시리즈 계획 JSON 파싱 오류: {e}")
        return None

    if progress_cb:
        progress_cb("시리즈 DB에 저장 중...", 2, 3)

    # DB에 저장
    sid = "s" + uuid.uuid4().hex[:8]
    from core.content_generator import detect_category
    episodes = []
    for ep_data in plan.get("episodes", []):
        episodes.append({
            "ep":       ep_data.get("ep", len(episodes)+1),
            "keyword":  ep_data.get("keyword", ""),
            "angle":    ep_data.get("angle", ""),
            "title":    "",        # 발행 후 채워짐
            "url":      "",
            "posted_at":"",
            "status":   "pending",
        })

    series = {
        "id":          sid,
        "title":       plan.get("series_title", f"2026 {base_keyword} 완전 정복"),
        "description": plan.get("description", ""),
        "category":    detect_category(base_keyword),
        "base_keyword":base_keyword,
        "created_at":  date.today().strftime("%Y-%m-%d"),
        "episodes":    episodes,
    }

    db = _load_series_db()
    db[sid] = series
    _save_series_db(db)

    if progress_cb:
        progress_cb("완료", 3, 3)

    log.info(f"시리즈 생성: {series['title']} ({len(episodes)}편)")
    return series


# ──────────────────────────────────────────────────────
# 시리즈 편 매칭
# ──────────────────────────────────────────────────────
def find_episode_in_series(title: str) -> tuple[dict | None, dict | None]:
    """
    발행할 글 제목이 어느 시리즈의 몇 편인지 찾습니다.
    Returns: (series_dict, episode_dict) or (None, None)
    """
    from core.duplicate_checker import _similarity

    clean = re.sub(r'^\d{4}-\d{2}-\d{2} — ', '', title)
    db    = _load_series_db()

    best_sim    = 0.0
    best_series = None
    best_ep     = None

    for series in db.values():
        for ep in series.get("episodes", []):
            kw  = ep.get("keyword", "")
            sim = _similarity(clean, kw)
            if sim > best_sim and sim >= 0.35:   # 35% 이상 유사하면 같은 편으로 인식
                best_sim    = sim
                best_series = series
                best_ep     = ep

    return best_series, best_ep


# ──────────────────────────────────────────────────────
# 시리즈 네비게이션 HTML 생성
# ──────────────────────────────────────────────────────
def build_series_nav_html(series: dict, current_ep: dict) -> str:
    """
    현재 편 기준 이전편/다음편 링크 + 전체 목차 박스를 HTML로 생성합니다.
    발행된 편만 링크를 활성화하고, 미발행편은 "준비 중"으로 표시합니다.
    """
    episodes  = series.get("episodes", [])
    cur_ep_no = current_ep.get("ep", 1)
    total     = len(episodes)
    title     = series.get("title", "시리즈")

    # 이전/다음 편 찾기
    prev_ep = next((e for e in episodes if e["ep"] == cur_ep_no - 1), None)
    next_ep = next((e for e in episodes if e["ep"] == cur_ep_no + 1), None)

    # 이전/다음 네비게이션 버튼
    nav_parts = []
    if prev_ep:
        prev_title = prev_ep.get("title") or prev_ep.get("keyword", "이전 편")
        if prev_ep.get("url"):
            nav_parts.append(
                f'<a href="{prev_ep["url"]}" style="color:#1a73e8;text-decoration:none;'
                f'padding:8px 16px;border:1px solid #1a73e8;border-radius:4px;'
                f'font-size:13px;display:inline-block;">'
                f'◀ 이전편: {prev_title[:20]}</a>'
            )
        else:
            nav_parts.append(
                f'<span style="color:#aaa;padding:8px 16px;border:1px solid #ddd;'
                f'border-radius:4px;font-size:13px;">◀ 이전편 준비 중</span>'
            )

    nav_parts.append(
        f'<span style="font-size:12px;color:#666;padding:8px 16px;">'
        f'{cur_ep_no} / {total}편</span>'
    )

    if next_ep:
        next_title = next_ep.get("title") or next_ep.get("keyword", "다음 편")
        if next_ep.get("url"):
            nav_parts.append(
                f'<a href="{next_ep["url"]}" style="color:#1a73e8;text-decoration:none;'
                f'padding:8px 16px;border:1px solid #1a73e8;border-radius:4px;'
                f'font-size:13px;display:inline-block;">'
                f'다음편: {next_title[:20]} ▶</a>'
            )
        else:
            nav_parts.append(
                f'<span style="color:#aaa;padding:8px 16px;border:1px solid #ddd;'
                f'border-radius:4px;font-size:13px;">다음편 준비 중 ▶</span>'
            )

    nav_html = f"""
<div style="background:#f8f9fa;border:1px solid #e0e0e0;border-radius:10px;
            padding:16px 20px;margin:24px 0;font-family:sans-serif;">
  <div style="font-size:11px;font-weight:700;color:#666;
              letter-spacing:1px;text-transform:uppercase;margin-bottom:12px;">
    📚 시리즈: {title}
  </div>
  <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
    {'  '.join(nav_parts)}
  </div>
</div>"""

    # 전체 목차 박스
    toc_items = ""
    for ep in episodes:
        ep_no     = ep.get("ep", 0)
        ep_kw     = ep.get("title") or ep.get("keyword", "")
        ep_url    = ep.get("url", "")
        is_cur    = (ep_no == cur_ep_no)
        ep_status = ep.get("status", "pending")

        if is_cur:
            toc_items += (
                f'<li style="margin:6px 0;font-weight:700;color:#1a73e8;">'
                f'<span style="background:#1a73e8;color:white;padding:1px 6px;'
                f'border-radius:3px;font-size:11px;margin-right:6px;">{ep_no}편</span>'
                f'📍 {ep_kw[:30]} ← 현재 글</li>\n'
            )
        elif ep_url:
            toc_items += (
                f'<li style="margin:6px 0;">'
                f'<span style="background:#e8f0fe;color:#1a73e8;padding:1px 6px;'
                f'border-radius:3px;font-size:11px;margin-right:6px;">{ep_no}편</span>'
                f'<a href="{ep_url}" style="color:#333;text-decoration:none;">'
                f'{ep_kw[:30]}</a></li>\n'
            )
        else:
            toc_items += (
                f'<li style="margin:6px 0;color:#aaa;">'
                f'<span style="background:#f5f5f5;color:#aaa;padding:1px 6px;'
                f'border-radius:3px;font-size:11px;margin-right:6px;">{ep_no}편</span>'
                f'{ep_kw[:30]} <small>(준비 중)</small></li>\n'
            )

    toc_html = f"""
<div style="background:#f0f7ff;border:1px solid #c5d9f7;border-radius:10px;
            padding:18px 22px;margin:0 0 28px 0;">
  <p style="font-weight:800;color:#1a4d8f;font-size:14px;margin:0 0 14px 0;">
    📚 {title} — 전체 목차
  </p>
  <ol style="list-style:none;margin:0;padding:0;">
{toc_items}  </ol>
</div>"""

    # 상단: 목차, 하단: 이전/다음 네비
    return toc_html + nav_html


# ──────────────────────────────────────────────────────
# 발행 후 시리즈 DB 업데이트
# ──────────────────────────────────────────────────────
def mark_episode_published(series_id: str, ep_no: int,
                            title: str, url: str):
    """글이 발행됐을 때 시리즈 DB의 해당 편을 업데이트합니다."""
    db = _load_series_db()
    if series_id not in db:
        return
    for ep in db[series_id]["episodes"]:
        if ep["ep"] == ep_no:
            ep["title"]     = title
            ep["url"]       = url
            ep["posted_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            ep["status"]    = "published"
            break
    _save_series_db(db)
    log.info(f"시리즈 업데이트: {db[series_id]['title']} {ep_no}편 발행 완료")


# ──────────────────────────────────────────────────────
# 시리즈 편 엑셀 자동 추가
# ──────────────────────────────────────────────────────
def add_series_to_excel(series: dict, excel_path: str,
                         sheet_name: str,
                         start_date: str = None) -> int:
    """
    시리즈의 모든 미발행 편을 엑셀에 추가합니다.
    날짜는 start_date부터 하루씩 순서대로 배정합니다.
    Returns: 추가된 편 수
    """
    from openpyxl import load_workbook
    from openpyxl.styles import PatternFill, Font
    from datetime import date as _date, timedelta

    SERIES_FILL = PatternFill(start_color="E8F0FE", end_color="E8F0FE", fill_type="solid")
    SERIES_FONT = Font(color="1A4D8F")

    if not os.path.exists(excel_path):
        log.error(f"엑셀 없음: {excel_path}")
        return 0

    wb = load_workbook(excel_path)
    if sheet_name not in wb.sheetnames:
        return 0
    ws = wb[sheet_name]

    # 기존 셀 값 수집
    existing = set()
    for row in ws.iter_rows():
        for cell in row:
            if cell.value:
                existing.add(str(cell.value).strip())

    if start_date:
        try:
            cur_date = datetime.strptime(start_date, "%Y-%m-%d").date()
        except ValueError:
            cur_date = _date.today()
    else:
        cur_date = _date.today()

    added = 0
    for ep in series.get("episodes", []):
        if ep.get("status") == "published":
            continue
        kw    = ep.get("keyword", "").strip()
        ep_no = ep.get("ep", 1)
        if not kw:
            continue
        entry = f"{cur_date.strftime('%Y-%m-%d')} — {series['title']} {ep_no}편: {kw}"
        if entry in existing:
            cur_date += timedelta(days=1)
            continue

        next_row = ws.max_row + 1
        cell     = ws.cell(row=next_row, column=1, value=entry)
        cell.fill = SERIES_FILL
        cell.font = SERIES_FONT
        existing.add(entry)
        added += 1
        log.info(f"엑셀 추가: {entry[:52]}")
        cur_date += timedelta(days=1)

    wb.save(excel_path)
    return added


# ──────────────────────────────────────────────────────
# 통계 & 조회 (GUI용)
# ──────────────────────────────────────────────────────
def get_all_series() -> list[dict]:
    """모든 시리즈를 최신 생성 순으로 반환합니다."""
    db = _load_series_db()
    series_list = list(db.values())
    series_list.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return series_list

def get_series_progress(series_id: str) -> dict:
    """시리즈 진행 현황 반환"""
    db = _load_series_db()
    if series_id not in db:
        return {}
    s  = db[series_id]
    eps = s.get("episodes", [])
    published = [e for e in eps if e.get("status") == "published"]
    return {
        "title":     s["title"],
        "total":     len(eps),
        "published": len(published),
        "pending":   len(eps) - len(published),
        "progress":  round(len(published) / len(eps) * 100) if eps else 0,
    }

def delete_series(series_id: str) -> bool:
    db = _load_series_db()
    if series_id in db:
        del db[series_id]
        _save_series_db(db)
        return True
    return False
