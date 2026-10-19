# core/blueprint_engine.py — 설계도 주도 변주 배정 엔진 (2026-08-25)
"""매 글마다 "훅·골격·어법·판단흔적"을 **결정론적으로** 배정해 모든 글이 같은
도입·구조·말투로 찍혀 나오는 AI 티를 근원에서 없앤다.

핵심 원칙:
- **범용**: 장르 데이터는 전부 templates/variation_banks.json — 정부지원·맛집·상품
  어떤 분야든 코드 수정 0. 장르에 따라 어법·안전선·출처강제가 달라진다.
- **결정론**: LLM 호출 0, random/시각 의존 0. 슬러그(제목) 해시 + 회전 이력으로만
  선택 — 같은 입력은 항상 같은 블루프린트(재생성이 같은 골격을 자연 재현).
- **fail-open**: banks 부재/파손/어떤 예외든 None 을 반환 → 호출측이 기존 고정
  프롬프트로 폴백. 생성 경로를 절대 막지 않는다.
- **프로필 격리**: 회전 상태는 활성 프로필의 logs/blueprint_rotation.json
  (research_cache·news_radar 와 동일한 app_data_root 라우팅).

수동 확인: python -m core.blueprint_engine "청년 월세 지원금 2026"
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import datetime
from typing import Optional

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.logger import get_logger

log = get_logger(__name__)

# 회전 이력 상한 — 최근 창(WINDOW)의 몇 배 여유.
_MAX_ROTATION = 60
# "최근 N편" 회전 판단 창.
_WINDOW = 10
# 한 훅이 창 안에서 이 횟수 이상이면 회전상 차단(같은 훅 2회까지만 허용).
_HOOK_REPEAT_CAP = 2
_DATE_FMT = "%Y-%m-%d %H:%M:%S"

# 비교형 훅 요건 판정에 쓰는 제목 토큰.
_COMPARISON_TOKENS = ("vs", "비교", "차이", "대비", "장단점", "어떤게", "뭐가")
# 캘린더/시점형 훅 요건 판정에 쓰는 날짜성 토큰(정규식).
_DATE_RE = re.compile(r"\d{4}|\d{1,2}\s*월|\d{1,2}\s*일|마감|신청\s*기간|기한|접수|시행")

_DEFAULT_GENRE = "info"


# ─────────────────────────────────────────────────────
# 데이터 로드 (variation_banks.json)
# ─────────────────────────────────────────────────────
def _banks() -> dict:
    """variation_banks.json 로드(프로필 오버라이드 우선). 실패 시 {}.

    캐시하지 않는다 — 파일 편집이 즉시 반영되고, 프로필 전환에도 안전하다.
    호출 빈도(글당 1회)가 낮아 비용 문제 없음.
    """
    try:
        from config import brand as _brand
        path = _brand.template_path("variation_banks.json")
    except Exception:  # noqa: BLE001 — 브랜드 로드 실패는 공유 템플릿으로 폴백
        from pathlib import Path
        path = Path(__file__).resolve().parent.parent / "templates" / "variation_banks.json"
    try:
        from config import safe_load_json
        data = safe_load_json(path, default=None)
    except Exception as exc:  # noqa: BLE001
        log.debug("[blueprint] banks 로드 실패(무시): %s", exc)
        return {}
    return data if isinstance(data, dict) else {}


# ─────────────────────────────────────────────────────
# 장르 해석
# ─────────────────────────────────────────────────────
def resolve_genre(title: str, category: str = "",
                  profile: Optional[str] = None) -> str:
    """이 글의 장르를 결정한다.

    우선순위:
      ① brand.json category_genre[category] → genre (전역 강제)  ← 명시
      ② variation_banks.json auto_detect 키워드 매칭(제목+카테고리) ← 자동
      ③ info (안전 기본값)

    default 프로필은 ①이 항상 빈값이라 ②/③으로 귀결한다(정부지원 제목→info).
    어떤 예외도 info 로 폴백(fail-open).
    """
    try:
        return _resolve_genre_impl(title, category, profile)
    except Exception as exc:  # noqa: BLE001
        log.debug("[blueprint] 장르 해석 실패 → info: %s", exc)
        return _DEFAULT_GENRE


def _effective_category(title: str, category: str) -> str:
    """전달된 카테고리(없으면 제목에서 detect_category)로 실효 카테고리를 구한다.

    YMYL 판정(안티할루시네이션 안전선)의 입력이 되므로, category 가 비면 제목에서
    보수적으로 감지한다. detect_category 로드 실패는 "" 로 폴백(무해).
    """
    if category:
        return category
    try:
        from core.content_generator import detect_category
        return detect_category(title or "") or ""
    except Exception:  # noqa: BLE001 — 감지 실패는 비YMYL 취급(자동 감지로 진행)
        return ""


def _is_ymyl(title: str, category: str) -> bool:
    """이 글이 YMYL(재정·건강 직결) 카테고리인가 — fact_gate 판정 재사용(하드코딩 중복 X).

    ⚠️ H2: 안전 게이트는 실패 시 **보수적으로 YMYL 취급**한다(경험 날조 차단 쪽으로 폴백).
    생성 자체를 막는 게 아니라 안전선만 보수화하므로 fail-open 원칙과 무충돌.
    """
    try:
        from core.fact_gate import is_ymyl_category
        return bool(is_ymyl_category(_effective_category(title, category)))
    except Exception:  # noqa: BLE001 — 판정 실패 시 YMYL 취급(안전 방향)
        return True


def _resolve_genre_impl(title: str, category: str, profile: Optional[str]) -> str:
    banks = _banks()
    genres = banks.get("genres") if isinstance(banks, dict) else None
    valid = set(genres.keys()) if isinstance(genres, dict) else set()

    # ① 명시 오버라이드 — brand.json (사용자가 대놓고 선언한 장르는 최우선, YMYL 강제보다 앞).
    try:
        from config import brand as _brand
        cat_map = _brand.category_genre(profile)
        if category and isinstance(cat_map, dict) and cat_map.get(category):
            g = cat_map[category]
            if not valid or g in valid:
                return g
        forced = _brand.genre(profile)
        if forced and (not valid or forced in valid):
            return forced
    except Exception as exc:  # noqa: BLE001 — 브랜드 미로드면 자동 감지로
        log.debug("[blueprint] 브랜드 장르 오버라이드 생략: %s", exc)

    # ② YMYL 카테고리면 info 강제 (C1 안티할루시네이션) — 자동감지가 '후기/다녀온/방문/
    #    신청/방법' 키워드로 정부지원(YMYL) 글을 place·howto 로 오분류하면 그 장르의
    #    allow_experience=true 가 "지어낸 1인칭 경험 권장"을 프롬프트에 주입해 날조를
    #    유도한다. fact_gate YMYL 판정을 우선 신호로 써서 그 경로를 원천 차단한다.
    if _is_ymyl(title, category):
        return "info" if (not valid or "info" in valid) else _DEFAULT_GENRE

    # ③ 자동 감지 — auto_detect distinctive 키워드(범용어는 데이터에서 제거됨).
    matched = _auto_detect_genre(banks, title, category, valid)
    if matched:
        return matched

    # ④ 미분류('default')·미매칭 → info 보수 강제 (C1 비대칭 보수성).
    #    detect_category 가 13 카테고리에 못 넣은 'default' 는 무엇이든(정부 포함) 될 수 있다.
    #    experiential 오분류(false-experiential)는 경험 날조 위험, info 오분류(false-info)는
    #    안전(사실만 씀). 그래서 distinctive 신호가 없으면 무조건 info 로 귀결한다.
    #    → "OO시 반값등록금 참여기"(distinctive 없음)는 여기서 info 로 차단된다.
    return _DEFAULT_GENRE


def _auto_detect_genre(banks: dict, title: str, category: str,
                       valid: set) -> Optional[str]:
    """auto_detect distinctive 키워드로 장르 매칭. 매칭 없으면 None(→ 호출측이 info 로 귀결).

    ⚠️ auto_detect 데이터에는 정부·YMYL 글과 겹치는 범용어('신청·방법·절차·등록·설정·가입·
    하는법·준비물')를 넣지 않는다(variation_banks._safety). 여기서 매칭된다는 것 자체가
    'distinctive 신호가 있다'는 뜻이라, 미분류 주제라도 이 매칭만 experiential 을 허용한다.
    """
    auto = banks.get("auto_detect") if isinstance(banks, dict) else None
    if not isinstance(auto, dict):
        return None
    haystack = f"{title or ''} {category or ''}".lower()
    order = auto.get("order")
    keys = order if isinstance(order, list) else [
        k for k in auto if k not in ("order", "_note", "_safety")
    ]
    for g in keys:
        kws = auto.get(g)
        if not isinstance(kws, list):
            continue
        if any(str(kw).lower() in haystack for kw in kws):
            if not valid or g in valid:
                return g
    return None


# ─────────────────────────────────────────────────────
# 결정론 선택 헬퍼
# ─────────────────────────────────────────────────────
def _slug_key(title: str) -> str:
    """제목→회전/해시 키. 날짜 접두("YYYY-MM-DD — ") 제거 + 공백 정규화."""
    t = re.sub(r"^\d{4}-\d{2}-\d{2}\s*—\s*", "", title or "")
    return re.sub(r"\s+", " ", t).strip().lower()


def _rank(key: str, kind: str, cand_id: str) -> str:
    """(키, 종류, 후보) 의 결정론적 정렬 순위 — sha1 16진(파이썬 hash 랜덤화 회피)."""
    return hashlib.sha1(f"{key}|{kind}|{cand_id}".encode("utf-8")).hexdigest()


def _ordered(candidates: list, key: str, kind: str, id_of) -> list:
    """후보를 결정론적 순위로 정렬해 반환(입력 리스트 비파괴)."""
    return sorted(candidates, key=lambda c: _rank(key, kind, id_of(c)))


def _pick(ordered: list, blocked_ids: set, id_of):
    """정렬된 후보에서 회전 미차단 첫 번째를 고른다. 전부 차단이면 첫 번째(폴백)."""
    if not ordered:
        return None
    for c in ordered:
        if id_of(c) not in blocked_ids:
            return c
    return ordered[0]


def _title_has_comparison(title: str) -> bool:
    low = (title or "").lower()
    return any(tok in low for tok in _COMPARISON_TOKENS)


def _title_has_date(title: str, intent: Optional[str]) -> bool:
    if _DATE_RE.search(title or ""):
        return True
    return bool(intent and _DATE_RE.search(intent))


def _hook_eligible(hook: dict, *, has_research: bool, title: str,
                   intent: Optional[str]) -> bool:
    """훅 요건(facts/조사/비교/날짜) 충족 여부. 미달이면 강등 대상."""
    req = str(hook.get("requires") or "none").lower()
    if req in ("", "none"):
        return True
    if req == "research":
        return bool(has_research)
    if req == "comparison":
        return _title_has_comparison(title)
    if req == "date":
        return _title_has_date(title, intent)
    return True


# ─────────────────────────────────────────────────────
# 회전 이력 (프로필 격리)
# ─────────────────────────────────────────────────────
def _rotation_path():
    """활성 프로필의 회전 상태 파일 경로(지연 계산 — 프로필 전환·테스트 격리 반영)."""
    from config import profile as _pf
    return _pf.app_data_root() / "logs" / "blueprint_rotation.json"


def _load_rotation() -> list:
    from config import safe_load_json
    data = safe_load_json(_rotation_path(), default=list)
    return data if isinstance(data, list) else []


def _save_rotation(records: list) -> None:
    # L6: 락 없는 read-modify-write. 발행 파이프라인은 프로필당 **순차 실행**(글 하나씩
    # 생성→검증→다음)이라 경합이 없다는 가정에 기댄다. 병렬 생성이 도입되면 파일 락
    # (또는 append-only 저널)로 바꿔야 한다. 어떤 손상도 회전 품질만 낮출 뿐 생성은 안 막힌다.
    from config import atomic_write_json
    path = _rotation_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(str(path), records[-_MAX_ROTATION:])


def _recent_window(records: list, exclude_key: str) -> list:
    """자기 슬러그를 뺀 최근 창(WINDOW). 자기 것을 빼야 재생성이 같은 결과를 재현한다."""
    others = [r for r in records
              if isinstance(r, dict) and r.get("slug") != exclude_key]
    return others[-_WINDOW:]


# ─────────────────────────────────────────────────────
# 블루프린트 배정
# ─────────────────────────────────────────────────────
def build_blueprint(title: str, category: str = "", *,
                    genre: Optional[str] = None,
                    intent: Optional[str] = None,
                    has_research: bool = False,
                    profile: Optional[str] = None) -> Optional[dict]:
    """이 글의 블루프린트를 결정론적으로 배정. 실패/데이터 부재 시 None(폴백).

    Args:
        title: 글 제목(회전/해시 키의 원천).
        category: 감지된 카테고리(장르 해석·핵심어 보정).
        genre: 장르를 강제(테스트/특수 경로). None 이면 resolve_genre.
        intent: 의도 힌트(날짜성 훅 요건 판정 보조).
        has_research: 이 글에 조사 근거가 있는가(조사형 훅·판단 안전선 분기).
        profile: 프로필명(None=활성).

    Returns:
        블루프린트 dict(스키마는 모듈 독스트링/CLAUDE 참조) 또는 None.
    """
    try:
        return _build_impl(title, category, genre=genre, intent=intent,
                           has_research=has_research, profile=profile)
    except Exception as exc:  # noqa: BLE001 — 어떤 실패도 생성 경로를 막지 않는다
        log.debug("[blueprint] build 실패 → 폴백(None): %s", exc)
        return None


def _build_impl(title: str, category: str, *, genre, intent,
                has_research: bool, profile) -> Optional[dict]:
    banks = _banks()
    genres = banks.get("genres") if isinstance(banks, dict) else None
    if not isinstance(genres, dict) or not genres:
        return None

    g = genre or resolve_genre(title, category, profile)
    gd = genres.get(g)
    if not isinstance(gd, dict):
        # 강제 장르가 banks 에 없으면 기본값으로.
        g = _DEFAULT_GENRE
        gd = genres.get(g)
    if not isinstance(gd, dict):
        return None

    hooks = [h for h in (gd.get("hooks") or []) if isinstance(h, dict) and h.get("id")]
    skeletons = [s for s in (gd.get("skeletons") or [])
                 if isinstance(s, dict) and s.get("id")]
    traces = [t for t in (gd.get("judgment_traces") or [])
              if isinstance(t, dict) and t.get("id")]
    if not (hooks and skeletons):
        return None

    key = _slug_key(title)
    window = _recent_window(_load_rotation(), key)
    win_hooks = [r.get("hook") for r in window]
    win_pairs = {(r.get("hook"), r.get("skeleton")) for r in window}
    win_palettes = [r.get("palette") for r in window]
    win_traces = [r.get("judgment") for r in window]
    win_titles = [r.get("title") for r in window]

    # ── 훅: 요건 충족(eligible) 우선, 회전 차단 회피 ──────────────
    eligible = [h for h in hooks
                if _hook_eligible(h, has_research=has_research,
                                  title=title, intent=intent)]
    hook_pool = eligible if eligible else hooks
    blocked_hooks = {hid for hid in set(win_hooks)
                     if hid and win_hooks.count(hid) >= _HOOK_REPEAT_CAP}
    hook = _pick(_ordered(hook_pool, key, "hook", lambda h: h["id"]),
                 blocked_hooks, lambda h: h["id"])

    # ── 골격: (훅×골격) 쌍 재사용 회피 ─────────────────────────
    blocked_skels = {sid for (hid, sid) in win_pairs if hid == hook["id"] and sid}
    skel = _pick(_ordered(skeletons, key, "skeleton", lambda s: s["id"]),
                 blocked_skels, lambda s: s["id"])

    # ── 어법 팔레트: 최근 사용분 회피 ─────────────────────────
    voice = gd.get("voice") if isinstance(gd.get("voice"), dict) else {}
    palette_ids = [p for p in (voice.get("palettes") or []) if p]
    all_palettes = banks.get("voice_palettes") if isinstance(banks, dict) else {}
    if not isinstance(all_palettes, dict):
        all_palettes = {}
    palette_ids = [p for p in palette_ids if p in all_palettes] or list(all_palettes.keys())
    last_palette = win_palettes[-1] if win_palettes else None
    palette_id = _pick(_ordered(palette_ids, key, "palette", lambda p: p),
                       {last_palette} if last_palette else set(),
                       lambda p: p) if palette_ids else None
    pdef = all_palettes.get(palette_id, {}) if palette_id else {}

    # ── 판단 흔적: 같은 트레이스 최근 사용 회피 ────────────────
    trace = None
    if traces:
        blocked_traces = {tid for tid in set(win_traces)
                          if tid and win_traces.count(tid) >= _HOOK_REPEAT_CAP}
        trace = _pick(_ordered(traces, key, "trace", lambda t: t["id"]),
                      blocked_traces, lambda t: t["id"])

    # ── 제목 아키타입: hook 과 독립 축으로 회전 ────────────────
    #   훅(본문 도입)과 제목 스타일을 따로 돌려 "2026 X 조건은?" 획일화를 깬다.
    title_arcs = [t for t in (gd.get("title_archetypes") or [])
                  if isinstance(t, dict) and t.get("id")]
    title_arc = None
    if title_arcs:
        blocked_titles = {tid for tid in set(win_titles)
                          if tid and win_titles.count(tid) >= _HOOK_REPEAT_CAP}
        title_arc = _pick(_ordered(title_arcs, key, "title", lambda t: t["id"]),
                          blocked_titles, lambda t: t["id"])

    # ── 앵글 힌트(선택) ────────────────────────────────────────
    angles = [a for a in (gd.get("angle_hints") or []) if a]
    angle_hint = ""
    if angles:
        angle_hint = _ordered(angles, key, "angle", lambda a: a)[0]

    # ── 블랙리스트 = 전역 + 장르 ──────────────────────────────
    blacklist = list(banks.get("blacklist_global") or [])
    blacklist += [b for b in (gd.get("blacklist") or []) if b]

    genre_rules = gd.get("genre_rules") if isinstance(gd.get("genre_rules"), dict) else {}
    # ★ YMYL 안전 잠금(C1 방어층) — 실효 카테고리가 YMYL 이면 장르가 무엇이든
    #   (브랜드가 place/howto 를 강제한 예외 포함) 경험 날조·무근거를 금지하도록
    #   genre_rules 를 강제로 굳힌다. genre_rules.ymyl 이 실제로 소비되는 지점.
    ymyl = bool(genre_rules.get("ymyl", True)) or _is_ymyl(title, category)
    force_year = bool(genre_rules.get("force_year", True))
    force_sources = bool(genre_rules.get("force_sources", True))
    allow_experience = bool(genre_rules.get("allow_experience", False))
    if ymyl:
        force_year = True
        force_sources = True
        allow_experience = False

    bp = {
        "_key": key,
        "genre": g,
        "hook": {"id": hook["id"], "type": hook.get("type", ""),
                 "directive": hook.get("directive", "")},
        "skeleton": {
            "id": skel["id"], "type": skel.get("type", ""),
            "h2_count": int(skel.get("h2_count", 2) or 0),
            "faq_count": int(skel.get("faq_count", 0) or 0),
            "table_allowed": bool(skel.get("table_allowed", False)),
            "closing": skel.get("closing", "one_line"),
            "image_hint": skel.get("image_hint", ""),
        },
        "voice": {
            "palette_id": palette_id or "",
            "palette_label": pdef.get("label", ""),
            "endings": list(pdef.get("endings") or []),
            "register_rule": voice.get("register_rule", ""),
            "register_directive": voice.get("register_directive", ""),
            "rules": list(banks.get("hard_rules") or []),
        },
        "judgment_trace": {
            "id": trace["id"] if trace else "",
            "type": trace.get("type", "") if trace else "",
            "directive": trace.get("directive", "") if trace else "",
            "position": trace.get("position", "after_summary") if trace else "after_summary",
            "safety_line": trace.get("safety_line", "") if trace else "",
        } if trace else {},
        "title": {
            "id": title_arc["id"] if title_arc else "",
            "label": title_arc.get("label", "") if title_arc else "",
            "directive": title_arc.get("directive", "") if title_arc else "",
            "example": title_arc.get("example", "") if title_arc else "",
        } if title_arc else {},
        "blacklist": blacklist,
        "genre_rules": {
            "ymyl": ymyl,
            "force_year": force_year,
            "force_sources": force_sources,
            "allow_experience": allow_experience,
        },
        "angle_hint": angle_hint,
    }
    return bp


def record_blueprint(slug_or_title: str, blueprint: Optional[dict]) -> bool:
    """배정된 블루프린트를 회전 이력에 적립(상한·프로필 격리·같은 슬러그 중복 방지).

    같은 슬러그가 이미 있으면 교체한다(재생성 이중 기록 방지 — build_blueprint 는
    자기 슬러그를 창에서 제외하므로 이력이 갱신돼도 결정론이 유지된다).
    어떤 실패도 조용히 무시(생성 경로 무영향).
    """
    if not isinstance(blueprint, dict):
        return False
    try:
        key = blueprint.get("_key") or _slug_key(slug_or_title)
        rec = {
            "slug": key,
            "hook": blueprint.get("hook", {}).get("id", ""),
            "skeleton": blueprint.get("skeleton", {}).get("id", ""),
            "palette": blueprint.get("voice", {}).get("palette_id", ""),
            "judgment": blueprint.get("judgment_trace", {}).get("id", ""),
            "title": blueprint.get("title", {}).get("id", ""),
            "genre": blueprint.get("genre", ""),
            "at": datetime.now().strftime(_DATE_FMT),
        }
        records = [r for r in _load_rotation()
                   if isinstance(r, dict) and r.get("slug") != key]
        records.append(rec)
        _save_rotation(records)
        return True
    except Exception as exc:  # noqa: BLE001 — 적립 실패는 생성 경로 무영향
        log.debug("[blueprint] 적립 실패(무시): %s", exc)
        return False


def stats() -> dict:
    """진단: 로드된 장르·회전 이력 크기·경로."""
    banks = _banks()
    genres = banks.get("genres") if isinstance(banks, dict) else {}
    return {
        "genres": sorted(genres.keys()) if isinstance(genres, dict) else [],
        "rotation_entries": len(_load_rotation()),
        "rotation_path": str(_rotation_path()),
    }


if __name__ == "__main__":
    try:
        from config.profile_boot import pin_profile_from_argv
        pin_profile_from_argv()
    except Exception:  # noqa: BLE001
        pass
    _title = sys.argv[1] if len(sys.argv) > 1 else "청년 월세 지원금 2026 신청 방법"
    print("stats:", json.dumps(stats(), ensure_ascii=False, indent=2))
    print("genre:", resolve_genre(_title))
    print("blueprint:", json.dumps(build_blueprint(_title), ensure_ascii=False, indent=2))
