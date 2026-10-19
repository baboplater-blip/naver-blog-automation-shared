# core/post_refresher.py
# 상위 글 30일 자동 리프레시 시스템
# 발행 30일 이후 순위가 떨어지기 전에 최신 내용으로 업데이트해서
# 네이버 알고리즘에 "최신 글" 신호를 다시 전달합니다.
from __future__ import annotations

import json, os, re, sys, time, urllib.error
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    # 타입 힌트 전용 — selenium 을 런타임에 import 하지 않는다(이 모듈은
    # 무거운/순환 의존을 피하려 selenium 은 함수 내부에서만 지연 import).
    from selenium import webdriver

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.logger import get_logger
from config import atomic_write_json, safe_load_json
log = get_logger(__name__)

BASE_DIR      = Path(__file__).parent.parent
from config import profile as _nbaprofile
_DATA_DIR = _nbaprofile.app_data_root()

HISTORY_PATH  = _DATA_DIR / "logs" / "post_history.json"
REFRESH_LOG   = _DATA_DIR / "logs" / "refresh_log.json"

# ── 리프레시 기준 ─────────────────────────────────────
REFRESH_DAYS     = 30    # 이 일수 이상 지난 글 대상
MAX_PER_RUN      = 5     # 하루 최대 리프레시 수 (Claude 호출 부하 관리)
RE_REFRESH_DAYS  = 25    # 리프레시 후 이 일수가 지나야 재리프레시 가능


# ──────────────────────────────────────────────────────
# DB 관리
# ──────────────────────────────────────────────────────
def _load_history() -> list:
    return safe_load_json(HISTORY_PATH, default=list)

def _save_history(records: list):
    atomic_write_json(HISTORY_PATH, records)

def _load_refresh_log() -> list:
    return safe_load_json(REFRESH_LOG, default=list)

def _save_refresh_log(logs: list):
    REFRESH_LOG.parent.mkdir(exist_ok=True)
    atomic_write_json(REFRESH_LOG, logs)


# ──────────────────────────────────────────────────────
# 리프레시 후보 선정
# ──────────────────────────────────────────────────────
def get_refresh_candidates(max_count: int = MAX_PER_RUN) -> list[dict]:
    """
    리프레시 대상 글 목록을 반환합니다.

    선정 기준:
    1. 발행 후 REFRESH_DAYS(30일) 이상 경과
    2. 마지막 리프레시 후 RE_REFRESH_DAYS(25일) 이상 경과
    3. URL이 있는 글 (실제 발행된 글만)
    4. 오래된 순(발행일 오름차순)으로 정렬 → 가장 오래된 글 먼저
    """
    records   = _load_history()
    ref_log   = {r["original_title"]: r for r in _load_refresh_log()}
    today     = date.today()
    candidates = []

    # 실측 순위·url 보강(performance_db). 발행 시 url 미기록분을 채우고, 리프레시
    # 우선순위를 '순위 좋은 글'(검색 권위 有 → AI요약 인용 시 유입 확률 최고)로.
    perf = {}
    try:
        from core import performance_db as _pdb
        _pd = _pdb.load()
        for _rec in _pd.get("posts", {}).values():
            # performance_db._best_rank 재사용 — blog_specific(구체 롱테일) 이력 제외
            # 가드를 물려받는다. 구체쿼리는 저경쟁이라 쉽게 상위에 들어, 인라인 복제로는
            # 리프레시 우선순위(하루 5편 Sonnet 슬롯)를 오염시켜 '성과 있는 글 우선'
            # 원칙을 깬다. 헤드 순위 기준으로만 정렬돼야 한다.
            best_rank, _ = _pdb._best_rank(_rec)
            perf[_pdb.normalize_key(_rec.get("clean_title") or _rec.get("title") or "")] = {
                "url": _rec.get("url", ""), "rank": best_rank}
    except (ImportError, OSError, ValueError, KeyError):
        perf = {}

    def _pkey(t):
        try:
            from core.performance_db import normalize_key
            return normalize_key(t)
        except (ImportError, AttributeError):
            return (t or "").replace(" ", "").lower()

    for rec in records:
        url         = rec.get("url", "")
        posted_at   = rec.get("posted_at", "")
        clean_title = rec.get("clean_title", rec.get("title", ""))

        # post_history url 이 비었으면 performance_db 에서 보강
        pinfo = perf.get(_pkey(clean_title), {})
        if not url:
            url = pinfo.get("url", "")
        rec_rank = pinfo.get("rank")

        if not url or not posted_at:
            continue

        # 발행일 파싱
        try:
            posted_date = datetime.strptime(posted_at[:10], "%Y-%m-%d").date()
        except ValueError:
            log.warning(f"날짜 형식 오류, 포스트 스킵: posted_at='{posted_at}'")
            continue

        age_days = (today - posted_date).days
        if age_days < REFRESH_DAYS:
            continue

        # 마지막 리프레시 확인
        last_refresh = ref_log.get(clean_title, {}).get("refreshed_at", "")
        if last_refresh:
            try:
                last_date = datetime.strptime(last_refresh[:10], "%Y-%m-%d").date()
                if (today - last_date).days < RE_REFRESH_DAYS:
                    continue
            except ValueError:
                pass

        candidates.append({
            **rec,
            "url":          url,          # 보강된 url 반영
            "age_days":     age_days,
            "rank":         rec_rank,     # 실측 순위(없으면 None)
            "refresh_count": ref_log.get(clean_title, {}).get("count", 0),
        })

    # 우선순위: 순위 좋은 글 먼저, 동률이면 AEO 개선 여지 큰(정보성) 글 먼저.
    # (샘플 검증: '메시지 템플릿'류 좁은 how-to 는 재생성해도 AEO 40→46 그침 —
    #  자격·금액·조건 정보성 글이라야 AEO 크게 상승.)
    for c in candidates:
        c["aeo_fit"] = _aeo_fit(c.get("clean_title", c.get("title", "")))
    candidates.sort(key=lambda x: (x.get("rank") is None, x.get("rank") or 999,
                                   -x.get("aeo_fit", 0), -x["age_days"]))
    return candidates[:max_count]


# AEO 상승 여지 = 제목이 정보성(자격·금액·조건 등)인가. 좁은 how-to·오류대응은 낮음.
_AEO_FIT_HI = ("자격", "금액", "조건", "얼마", "대상", "지원", "기준", "vs", "비교",
               "차이", "신청", "혜택", "수급", "급여", "바우처", "계좌", "대출", "연금")
_AEO_FIT_LO = ("오류", "안될", "안 될", "템플릿", "메시지", "대응", "문의", "고객센터",
               "실패", "삭제", "변경하는", "끄는", "켜는")


def _aeo_fit(title: str) -> int:
    """제목 기반 AEO 개선 여지 점수(정보성↑). 좁은 how-to·오류대응은 감점."""
    t = (title or "").replace(" ", "")
    hi = sum(1 for w in _AEO_FIT_HI if w.replace(" ", "") in t)
    lo = sum(1 for w in _AEO_FIT_LO if w.replace(" ", "") in t)
    return hi - 2 * lo


def preview_refresh_targets(max_count: int = 15) -> list[dict]:
    """리프레시 대상 미리보기 — Claude 호출 없이 '어느 글을 왜' 고르는지만.

    실제 재생성(refresh_post)은 글당 Claude CLI 를 쓰므로, 대상 검토는 이 함수로.
    반환: [{title, rank, age_days, url, reason}, ...]
    """
    cands = get_refresh_candidates(max_count)
    out = []
    for c in cands:
        rank = c.get("rank")
        fit = c.get("aeo_fit", _aeo_fit(c.get("clean_title", c.get("title", ""))))
        fit_note = "정보성(AEO 상승 큼)" if fit >= 2 else ("보통" if fit >= 0 else "좁은 how-to(AEO 상승 작음)")
        if rank and rank <= 3:
            reason = f"검색 {rank}위 · {fit_note}"
        elif rank and rank <= 10:
            reason = f"검색 {rank}위(TOP10) · {fit_note}"
        elif rank:
            reason = f"검색 {rank}위 · {fit_note}"
        else:
            reason = f"{c['age_days']}일 경과 · 순위 미측정 · {fit_note}"
        out.append({
            "title": c.get("clean_title", c.get("title", "")),
            "rank": rank, "age_days": c.get("age_days", 0), "aeo_fit": fit,
            "url": c.get("url", ""), "refresh_count": c.get("refresh_count", 0),
            "reason": reason,
        })
    return out


# ──────────────────────────────────────────────────────
# Claude로 업데이트 콘텐츠 생성
# ──────────────────────────────────────────────────────
def _refresh_genre(clean_title: str):
    """리프레시 대상의 장르/블루프린트 — 원글 제목으로 resolve. 실패 시 (None, None)."""
    try:
        from core.content_generator import _blueprint_enabled
        if not _blueprint_enabled():
            return None, None
        from core.blueprint_engine import build_blueprint, resolve_genre
        genre = resolve_genre(clean_title, "")
        bp = build_blueprint(clean_title, "")
        return genre, bp
    except Exception:  # noqa: BLE001 — 실패는 기존 프롬프트로 진행
        return None, None


def _apply_refresh_blueprint(prompt: str, clean_title: str, greeting: str,
                             year: int) -> str:
    """리프레시 프롬프트에 AI 티 제거 코어를 적용(BLUEPRINT ON 일 때만).

    ①인사말 강제 도입부 → 훅 도입부로 치환(안녕하세요 고정 제거) ②[제목 규칙] → 제목
    아키타입 치환 ③JSON title/도입 힌트의 인사말 언급 정리 ④판단 흔적·voice·블랙리스트
    덧붙임. OFF/실패면 원본 그대로(fail-open·바이트 동일). 각 replace 는 대상 없으면 no-op.
    """
    _genre, bp = _refresh_genre(clean_title)
    if not bp:
        return prompt
    try:
        from core.content_generator import _bp_title_block, blueprint_core_block
        from core.blueprint_engine import record_blueprint
    except Exception:  # noqa: BLE001
        return prompt
    try:
        applied: list = []
        missed: list = []

        def _sub(p: str, old: str, new: str, label: str) -> str:
            """치환 성공 추적 + 실패 시 경고(M3 — 조용한 no-op 감시). 실패해도 진행."""
            if old and old in p:
                applied.append(label)
                return p.replace(old, new)
            missed.append(label)
            log.warning("[post_refresher] 리프레시 프롬프트 치환 실패(no-op): %s "
                        "— 원본 문구 드리프트 가능(append 코어와 모순 공존 위험)", label)
            return p

        first_kw = clean_title.split()[0] if clean_title.split() else clean_title
        # ① 인사말 강제 도입부 → 훅 도입부(answer-first 는 유지).
        greeting_old = (
            f'- ★ 도입부: 첫 줄은 "{greeting}" 인사로 시작하고(딱 한 줄로 짧게), 곧바로 짧은 \'훅\' 한 문장\n'
            "  (막막한 지점·놓치면 손해인 이유·헷갈리는 부분) + \"결론부터 말하면, …입니다.\" 직접 답변을 이어 붙인다\n"
            "  (인사+훅+답변 모두 첫 400자 안에 — 인사말이 답변을 뒤로 밀지 않게 짧게).\n"
            f'  예: "{greeting} …중 뭘 먼저 신청할지 막막하신가요? 결론부터 말하면, {first_kw}은(는) …입니다."'
        )
        hook = bp.get("hook", {})
        greeting_new = (
            "- ★ 도입부: 인사말(\"안녕하세요…\")·예고문 없이 곧바로 훅으로 시작하세요.\n"
            f"  훅 방식({hook.get('type', '')}): {hook.get('directive', '')}\n"
            "  이어서 \"결론부터 말하면, …입니다.\" answer-first 첫 문장(핵심 답변)을 첫 400자 안에."
        )
        prompt = _sub(prompt, greeting_old, greeting_new, "greeting")

        # ② [제목 규칙] → 제목 아키타입.
        title_old = (
            "[제목 규칙]\n"
            f'- "{year}" 연도 포함, 광역/핵심 키워드를 제목 맨 앞에 배치\n'
            "- 22~32자, 짧을수록 좋음. 가능하면 질문형(사람들이 실제로 검색하는 질문)\n"
            '- ❌ "총정리/핵심정리/완벽정리" 금지'
        )
        tb = _bp_title_block(bp)
        if tb:
            prompt = _sub(prompt, title_old, tb, "title_rules")

        # ③ JSON 스키마의 인사말·연도맨앞 언급 정리.
        prompt = _sub(
            prompt, "업데이트된 SEO 제목 (22~32자, 광역 키워드 맨 앞, 질문형 우선)",
            "업데이트된 SEO 제목 (22~32자, 위 제목 스타일, 핵심 키워드 앞쪽)", "title_schema")
        prompt = _sub(prompt, f"도입: '{greeting}' 인사+훅+answer-first 첫 문장",
                      "도입: 훅+answer-first 첫 문장(인사말 없이)", "content_hint")

        # ④ 판단 흔적·voice·블랙리스트 덧붙임(훅·제목은 위에서 치환했으니 제외).
        core = blueprint_core_block(bp, include_judgment=True, include_title=False,
                                    include_hook=False)
        if core:
            prompt += "\n\n" + core
        try:
            record_blueprint(clean_title, bp)
        except Exception:  # noqa: BLE001
            pass
        return prompt
    except Exception:  # noqa: BLE001 — 어떤 치환 오류도 원본 프롬프트로 폴백
        return prompt


def _generate_refreshed_content(clean_title: str, original_content: str = "") -> dict:
    """
    기존 글을 2026년 최신 기준으로 업데이트한 새 콘텐츠를 생성합니다.
    original_content가 있으면 Claude에게 참고자료로 제공합니다.
    """
    from datetime import date as _date
    year  = _date.today().year
    month = _date.today().month

    # 기존 내용 요약 (프롬프트 길이 제한)
    orig_snippet = ""
    if original_content and len(original_content) > 200:
        orig_snippet = f"""
[기존 글 요약 — 이 내용을 기반으로 최신 정보로 업데이트하세요]
{original_content[:600]}
...
"""

    # 브랜드 팩 오버라이드(named 프로필) — default 는 빈 문자열이라 기존 리터럴로 폴백.
    try:
        from config import brand as _brand
        role = _brand.writer_role() or "대한민국 정부지원 정보 전문 블로그 작가"
        sources = _brand.primary_sources() or "정부24·복지로·국세청"
        blog_nm = _brand.blog_name() or "예시브랜드"
    except Exception:  # noqa: BLE001 — 브랜드 로드 실패 시 기존 동작 유지
        role = "대한민국 정부지원 정보 전문 블로그 작가"
        sources = "정부24·복지로·국세청"
        blog_nm = "예시브랜드"
    # 인사말은 메인 새 글 생성기(content_generator intro_greeting)와 동일. AEO 무손실:
    # aeo_scorer 의 lead_answer 는 도입부 '첫 600자'를 보는데 인사말(~13자)은 답변을
    # 그 창 밖으로 밀어내지 않으므로, 인사→훅→결론 순서면 점수 영향이 없다.
    greeting = f"안녕하세요, {blog_nm}입니다."

    prompt = f"""당신은 {role}입니다.
아래 주제의 블로그 글을 {year}년 {month}월 최신 기준으로 완전히 새롭게 업데이트해서 작성해주세요.
{orig_snippet}
주제: {clean_title}

[AI 요약 최적화 (AEO) — 네이버 AI 요약에 이 글이 인용되게 하는 게 최우선]
- ★ 도입부: 첫 줄은 "{greeting}" 인사로 시작하고(딱 한 줄로 짧게), 곧바로 짧은 '훅' 한 문장
  (막막한 지점·놓치면 손해인 이유·헷갈리는 부분) + "결론부터 말하면, …입니다." 직접 답변을 이어 붙인다
  (인사+훅+답변 모두 첫 400자 안에 — 인사말이 답변을 뒤로 밀지 않게 짧게).
  예: "{greeting} …중 뭘 먼저 신청할지 막막하신가요? 결론부터 말하면, {clean_title.split()[0] if clean_title.split() else clean_title}은(는) …입니다."
- ★ 자격·금액·기한 등 핵심 사실은 "그 한 문장만 읽어도 이해되게" 자기완결적으로(누가·얼마·언제가 한 문장에).
- ★ 소제목(H2) 2~3개는 실제 검색 질문 형태로: "누가 받을 수 있나요?", "얼마나 받나요?",
  "언제까지 신청하나요?", "무슨 서류가 필요한가요?"
- ★ 신청 방법은 반드시 '1. / 2. / 3.' 번호 목록으로 단계를 3개 이상 나눠 명확히(문장 뭉치 금지).
- ★ 자주 묻는 질문은 'Q1. / Q2. / Q3.' 형식으로 3개 이상, 각 답변의 결론을 첫 문장에 먼저.
- ★ 마지막은 '마치며' 문단으로 핵심 사실 2~3개를 다시 짚는다(★ '마치며' 또는 '결론'이라는 말로 시작 —
  단, "총정리/핵심정리/완벽정리"라는 단어는 쓰지 말 것).
- ★ {sources} 등 1차 출처를 본문에 자연스럽게 명시(신뢰 신호).

[체류·경험 최적화 (추천피드 — 끝까지 읽히게 하는 신호)]
- ★ 1차 경험: "직접 확인해보니", 실제 사례, 마크다운 표(수치 비교)로 경험 신호를 넣어 체류↑.
- ★ 짧은 문단: 한 문단 3~4줄 이내. 벽 텍스트 금지(스크롤 이탈 방지).
- ★ 산문형 스크롤 유인: "아래 표에서 자세히", "먼저 A, 그다음 B"로 다음 문단을 예고(목차/체크리스트 위젯은 금지).
- ★ 이미지 자리 마커: 도입부 직후·표 직전·마무리 직전에 <!-- IMG: 구체 캡션 --> 2~3개(사용자가 사진 첨부).
  본문에 넣을 수 있는 주석 마커는 이 IMG 하나뿐 — `<!-- VIDEO: ... -->`·`<iframe>`·`<video>` 금지
  (발행 파이프라인에 동영상 임베드 경로가 없어 본문에 그대로 남는다).
- ★ 마무리 1줄: 도움이 됐다면 이웃추가·구독을 과하지 않게 유도.

[업데이트 작성 규칙 — 반드시 준수]
1. {year}년 최신 기준 (변경된 금액·자격·절차 반드시 반영)
2. 글 길이: 2,000~2,500자 (2,500자를 넘기지 말 것 — 표·FAQ·번호목록을 이 상한 안에 담는다)
3. 소제목(H2)은 위 질문형 위주 + 아래를 자연스럽게 포함:
   신청 자격 / 신청 방법(번호 목록) / 지원 금액 / {year}년 변경사항(전년도와 달라진 점, 반드시) / 자주 묻는 질문(Q&A)
   → ❌ 이모지 헤딩(## 📌, ## ✅) 금지. 소제목은 텍스트만.
4. 마크다운 표 1개 이상, FAQ 3개 이상, 해시태그 15개
5. 변경사항 섹션에 전년도와 달라진 점 명시

[제목 규칙]
- "{year}" 연도 포함, 광역/핵심 키워드를 제목 맨 앞에 배치
- 22~32자, 짧을수록 좋음. 가능하면 질문형(사람들이 실제로 검색하는 질문)
- ❌ "총정리/핵심정리/완벽정리" 금지

반드시 JSON 형식으로만 응답 (코드블록 없이):
{{
  "title": "업데이트된 SEO 제목 (22~32자, 광역 키워드 맨 앞, 질문형 우선)",
  "content": "마크다운 본문 전체, 총 2,000~2,500자(2,500자 초과 금지) (도입: '{greeting}' 인사+훅+answer-first 첫 문장 / 신청 방법 1.2.3. 번호목록 / Q1~Q3 / '마치며' 마무리 / 이웃추가 1줄)",
  "summary": "한 줄 요약",
  "tags": ["태그1","태그2","태그3","태그4","태그5","태그6","태그7","태그8","태그9","태그10","태그11","태그12","태그13","태그14","태그15"],
  "changes": "이번 업데이트에서 변경된 주요 내용 한 줄"
}}"""

    # [설계도 주도 변주] 리프레시도 AI 티 제거 코어를 적용. 원글 category 로 장르 해석
    #   (정부 YMYL→info 강제 → 지어낸 경험 금지 유지·연도 포함). OFF/실패면 프롬프트
    #   바이트 동일. 웹검색 근거 승계·게이트(_aeo_fit·min_gain)·YMYL 안전은 이 주입과 무관.
    prompt = _apply_refresh_blueprint(prompt, clean_title, greeting, year)

    # [안티-AI-티 규칙 Phase C] 리프레시 재생성 프롬프트에도 규칙 블록을 덧붙인다.
    #   ANTI_AI_TELL_PROMPT OFF(기본)/실패면 "" → append 하지 않아 프롬프트 바이트 동일.
    try:
        from core.content_generator import _anti_ai_tell_block as _atb_fn
        _atb = _atb_fn()
        if _atb:
            prompt += "\n\n" + _atb
    except Exception:  # noqa: BLE001 — 블록 조회 실패는 기존 프롬프트 유지
        pass

    try:
        from core.content_generator import _call_claude, _parse_json_response
        raw = _call_claude(prompt, max_retries=2)
        if not raw:
            log.warning("Claude 응답 없음 (빈 결과)")
            return {}
        try:
            result = _parse_json_response(raw, clean_title, "")
        except ValueError as e:
            log.warning(f"콘텐츠 JSON 필수 키 누락: {e}")
            return {}
        # AEO 점수 로그 — 리프레시가 실제로 AI 요약 친화 구조로 개선됐는지 확인용
        if result and result.get("content"):
            try:
                from core.aeo_scorer import score as _aeo_score
                _r = _aeo_score(result["content"])
                log.info(f"  🤖 리프레시 AEO 점수: {_r.score:.0f}/100 "
                         f"({'PASS' if _r.passed() else 'FAIL'})")
            except (ImportError, OSError, ValueError, TypeError):
                pass
        return result if result else {}
    except (ImportError, OSError) as e:
        log.warning(f"콘텐츠 생성 오류: {e}")
        return {}


# ──────────────────────────────────────────────────────
# 네이버 API 글 수정
# ──────────────────────────────────────────────────────
def _edit_via_api(post_id: str, title: str, content_html: str,
                  tags: list) -> dict:
    """
    네이버 오픈 API로 기존 글을 수정합니다.
    post_id(logNo): record["post_id"] 필드에 저장된 값
    """
    try:
        import urllib.request, urllib.error
        from core.naver_api_uploader import NaverAuth
        from config import settings as s

        auth = NaverAuth(s.NAVER_API_CLIENT_ID, s.NAVER_API_CLIENT_SECRET)
        if not auth.access_token:
            return {"success": False, "error": "token_missing"}

        body = json.dumps({
            "logNo":   str(post_id),
            "title":   title,
            "content": content_html,
            "tags":    ",".join(str(t) for t in tags[:30]),
        }, ensure_ascii=False).encode("utf-8")

        req = urllib.request.Request(
            "https://openapi.naver.com/blog/editPost.json",
            data=body,
            headers={
                "Authorization": f"Bearer {auth.access_token}",
                "Content-Type":  "application/json; charset=UTF-8",
            },
            method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read())
            return {"success": True, "post_id": post_id,
                    "url": result.get("postUrl", "")}
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        log.warning(f"API 수정 실패: {e}")
        return {"success": False, "error": str(e)}
    except (ImportError, json.JSONDecodeError) as e:
        log.warning(f"API 수정 설정 오류: {e}")
        return {"success": False, "error": str(e)}


# ──────────────────────────────────────────────────────
# 히스토리 레코드 업데이트
# ──────────────────────────────────────────────────────
# 네이버에 **실제로 반영됐음이 확인된** method 값만 성공으로 친다.
# "repost"/"repost_fallback"/"unknown" 은 반영을 시도했거나 하려던 상태일 뿐이다.
_PUBLISHED_METHODS = frozenset({"api_edit", "repost_api", "repost_selenium"})


def _update_history_record(clean_title: str, new_title: str,
                            new_url: str, changes: str):
    """post_history.json 에서 해당 레코드의 refreshed_at 필드를 업데이트합니다."""
    records = _load_history()
    for rec in records:
        if rec.get("clean_title") == clean_title:
            rec["refreshed_at"]  = datetime.now().strftime("%Y-%m-%d %H:%M")
            rec["refresh_title"] = new_title
            rec["changes"]       = changes
            if new_url:
                rec["refresh_url"] = new_url
            break
    _save_history(records)

    # refresh_log 에도 기록
    logs = _load_refresh_log()
    existing = next((r for r in logs if r["original_title"] == clean_title), None)
    if existing:
        existing["refreshed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        existing["count"]       += 1
        existing["last_changes"] = changes
    else:
        logs.append({
            "original_title": clean_title,
            "new_title":      new_title,
            "refreshed_at":   datetime.now().strftime("%Y-%m-%d %H:%M"),
            "count":          1,
            "last_changes":   changes,
        })
    _save_refresh_log(logs)


# ──────────────────────────────────────────────────────
# 핵심 공개 함수: refresh_post
# ──────────────────────────────────────────────────────
def refresh_post(record: dict, dry_run: bool = False,
                 progress_cb=None,
                 driver: "Optional[webdriver.Chrome]" = None,
                 reserve_datetime: "Optional[str]" = None) -> dict:
    """
    글 1개를 리프레시합니다.

    Args:
        record:      리프레시 대상 레코드(clean_title/url/post_id 등).
        dry_run:     True 면 콘텐츠만 생성하고 네이버 반영은 하지 않음.
        progress_cb: 진행률 콜백(msg, cur, total).
        driver:      Selenium 재발행 시 재사용할 로그인된 WebDriver.
                     - 주어지면(배치 공유 드라이버): init_driver/naver_login/quit 없이
                       그 세션으로 업로드만 수행(글마다 재로그인 방지).
                     - None(단독 호출): 기존 동작 그대로 자체 드라이버를 생성·로그인하고
                       finally 에서 종료.
                     dry_run 또는 API(USE_NAVER_API) 경로에서는 사용되지 않음.
        reserve_datetime: "YYYY-MM-DD HH:MM" 예약 발행 시각. Selenium 재발행
                     경로에서만 upload_post 로 전달돼 예약 발행된다(None 이면
                     upload_post 기본 동작 = 즉시발행). 리프레시 배치는 항상 유효한
                     예약 슬롯을 배정해 넘기며(run_refresh_batch), 슬롯을 못 만들면
                     즉시발행으로 폴백하지 않고 그 글을 건너뛴다.
                     dry_run 또는 API(USE_NAVER_API) 경로에서는 사용되지 않음.

    Returns: {
        "success":   bool,
        "title":     str,   # 새 제목
        "url":       str,   # 수정/재발행 URL
        "changes":   str,   # 변경 요약
        "method":    str,   # "api_edit" | "repost" | "dry_run"
    }
    """
    clean_title = record.get("clean_title", record.get("title", ""))
    post_id     = record.get("post_id", "")
    orig_url    = record.get("url", "")

    log.info(f"리프레시: {clean_title[:46]}")
    log.info(f"  발행일: {record.get('posted_at','?')}  ({record.get('age_days',0)}일 경과)")

    # ── 운영 정지 스위치 (2026-09-23) ───────────────────────────────────
    # 리프레시는 **기존 글을 다시 내보내는 외부 행동**이다. 일시중지·비상정지 중에는
    # 수동 버튼으로도 나가지 않는다. dry_run(테스트로 보기)은 외부 행동이 아니라 통과.
    # 글 생성(유료 호출) **전**에 막아 헛돈도 쓰지 않는다.
    if not dry_run:
        try:
            from core import publish_state_bridge as _psb
            _hold = _psb.guard_new_item() or ""
        except Exception:  # noqa: BLE001 — 모듈 부재 = 기존 동작
            _hold = ""
        if _hold:
            log.warning("⏹ 운영 정지 상태라 리프레시를 하지 않습니다 (%s)", _hold)
            return {"success": False, "error": _hold, "title": clean_title,
                    "url": orig_url, "changes": "", "method": "blocked"}

    if progress_cb:
        progress_cb(f"콘텐츠 업데이트 중: {clean_title[:30]}", 1, 3)

    # Step 1: 업데이트 콘텐츠 생성
    new_data = _generate_refreshed_content(clean_title)
    if not new_data:
        return {"success": False, "error": "content_generation_failed"}

    new_title   = new_data.get("title",   clean_title)
    new_content = new_data.get("content", "")
    new_tags    = new_data.get("tags",    [clean_title.split()[0]])
    changes     = new_data.get("changes", "최신 정보로 업데이트")

    if dry_run:
        log.info(f"[테스트] 새 제목: {new_title}")
        log.info(f"[테스트] 변경사항: {changes}")
        return {
            "success": True, "title": new_title,
            "url": orig_url, "changes": changes, "method": "dry_run",
        }

    if progress_cb:
        progress_cb("HTML 변환 중...", 2, 3)

    # Step 2: HTML 변환
    from core.content_generator import markdown_to_naver_html, strengthen_title
    from core.content_generator import detect_category
    category     = detect_category(clean_title)
    # 블루프린트 ON 이면 연도 '맨 앞 강제'를 끄고(prepend_year=False), info(force_year)면
    # 연도가 빠졌을 때만 문장 뒤에 보강(append) — 제목 획일화 해소·신선도 유지.
    # OFF 면 기존 2-인자 호출 그대로(바이트 동일 — 기존 테스트 mock 시그니처 보존).
    _genre, _bp = _refresh_genre(clean_title)
    if _bp:
        _s_force_year = bool(_bp.get("genre_rules", {}).get("force_year", True))
        new_title = strengthen_title(new_title, category, force_year=_s_force_year,
                                     prepend_year=False,
                                     append_year_if_missing=_s_force_year)
    else:
        new_title = strengthen_title(new_title, category)
    content_html = markdown_to_naver_html(new_content, new_title, category)

    # '🔄 업데이트되었습니다' 상단 배너는 제거했다(AEO 무관 요소 — 발행 본문에서 제외).
    # changes 는 여전히 반환값·히스토리(refresh_log) 기록용으로만 사용한다.

    if progress_cb:
        progress_cb("네이버에 반영 중...", 3, 3)

    # Step 3: 네이버에 반영
    result_url = orig_url
    method     = "unknown"

    try:
        from config import settings as s
        if s.USE_NAVER_API and post_id:
            # API 수정 (선호)
            api_result = _edit_via_api(post_id, new_title, content_html, new_tags)
            if api_result.get("success"):
                result_url = api_result.get("url", orig_url) or orig_url
                method     = "api_edit"
                log.info(f"API 수정 완료: {new_title[:40]}")
            else:
                log.warning(f"API 수정 실패 → 재발행 전환: {api_result.get('error','')}")
                method = "repost_fallback"
        else:
            method = "repost"
    except ImportError as e:
        log.warning(f"설정 로드 오류: {e}")
        method = "repost"

    # 재발행 방식 (API 없거나 실패 시)
    if method in ("repost", "repost_fallback"):
        try:
            from config import settings as s
            post_data = {
                "title":        new_title,
                "content":      new_content,
                "content_html": content_html,
                "tags":         new_tags,
                "category":     category,
                "summary":      new_data.get("summary", f"{clean_title} 2026 업데이트"),
            }

            if s.USE_NAVER_API:
                from core.naver_api_uploader import upload_post_api
                api_result = upload_post_api(post_data, [], None)
                if api_result.get("success"):
                    result_url = api_result.get("url", orig_url)
                    method     = "repost_api"
                    log.info(f"재발행 완료 (API): {new_title[:40]}")
            elif driver is not None:
                # 배치가 공유 드라이버를 전달 — init_driver/naver_login/quit 없이
                # 이미 로그인된 세션으로 업로드만 수행(글마다 재로그인 방지).
                # reserve_datetime 을 넘겨 예약 발행한다(None 이면 즉시발행 — 단독 호출·
                # 폴백 경로에서만 발생하며, 배치는 항상 유효 슬롯을 배정해 넘긴다).
                from core.naver_uploader import upload_post
                if upload_post(driver, post_data, [], reserve_datetime):
                    method = "repost_selenium"
                    log.info(f"재발행 완료 (Selenium, 공유 세션): {new_title[:40]}")
            else:
                # 단독 호출 — 기존 동작 유지: 자체 드라이버 생성·로그인·종료.
                from core.naver_uploader import init_driver, naver_login, upload_post
                own_driver = init_driver()
                try:
                    if naver_login(own_driver):
                        if upload_post(own_driver, post_data, [], reserve_datetime):
                            method = "repost_selenium"
                            log.info(f"재발행 완료 (Selenium): {new_title[:40]}")
                finally:
                    own_driver.quit()
        except (ImportError, urllib.error.URLError, urllib.error.HTTPError) as e:
            log.error(f"재발행 실패: {e}")
            return {"success": False, "error": str(e)}

    # ── 실제로 네이버에 반영됐을 때만 성공이다 (2026-09-23) ──
    # 예전에는 업로드가 False 를 돌려줘도(로그인 실패·에디터 실패·이미지 게이트 보류 등)
    # method 가 "repost"/"repost_fallback" 인 채로 아래까지 내려와 **무조건 success=True**
    # 를 돌려줬다. 그 결과 ①GUI 가 "다시 쓰기가 끝났어요 N편"이라 보고하고
    # ②post_history 에 refreshed_at 이 찍혀 그 글이 **다시는 리프레시 후보로 잡히지 않았다**
    # — 아무것도 발행되지 않았는데 영구히 "갱신됨"으로 남는 상태 오판이다.
    if method not in _PUBLISHED_METHODS:
        log.error("리프레시 반영 실패 — 이력을 갱신하지 않습니다: %s (method=%s)",
                  clean_title[:40], method)
        return {
            "success": False,
            "title":   new_title,
            "url":     orig_url,
            "changes": changes,
            "method":  method,
            "error":   "publish_not_confirmed",
        }

    # Step 4: 히스토리 업데이트
    _update_history_record(clean_title, new_title, result_url, changes)

    return {
        "success": True,
        "title":   new_title,
        "url":     result_url,
        "changes": changes,
        "method":  method,
    }


# ──────────────────────────────────────────────────────
# 일괄 리프레시 (스케줄러에서 호출)
# ──────────────────────────────────────────────────────
# 예약 슬롯 형식: "YYYY-MM-DD HH:MM" (assign_reservation_times / upload_post 규약).
_SLOT_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")


def _slot_date(slot: "Optional[str]") -> "Optional[date]":
    """예약 슬롯 문자열('YYYY-MM-DD HH:MM')에서 날짜만 파싱. 형식 오류면 None."""
    if not slot or not isinstance(slot, str):
        return None
    try:
        return datetime.strptime(slot[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _assign_refresh_slots(candidates: list,
                          target_date: "Optional[str]",
                          after_slots: "Optional[list]",
                          cfg) -> list:
    """리프레시 후보에 예약 발행 시각을 배정한다(새 글 슬롯 뒤로 이어붙임).

    - after_slots(오늘 예약된 새 글 슬롯 "YYYY-MM-DD HH:MM" 목록)가 있으면 그 날짜들을
      assign_reservation_times 의 existing_reservations 로 주입한다 → 마지막 새 글 날짜
      다음 날부터 리프레시가 시작돼 "새 글 뒤에 이어서" 예약된다.
    - after_slots 가 없으면 assign_reservation_times 가 runs/*/publish_brief.yaml +
      예약 대장에서 기존 미래 예약을 스스로 로드해 그 뒤로 이어붙인다(옛 단독 호출 폴백).

    반환: 후보 수만큼의 "YYYY-MM-DD HH:MM" 문자열 리스트.
          모듈/함수 실패 시 **빈 리스트** → 호출부가 각 글을 fail-closed 로 skip
          (즉시발행 폴백 금지 — 자동 즉시발행 금지선).
    """
    try:
        from scheduler.auto_scheduler import assign_reservation_times
    except ImportError as exc:
        log.error(f"예약 배정 모듈 로드 실패 — 리프레시 예약 불가: {exc}")
        return []

    existing = None
    if after_slots:
        parsed = [{"date": d} for d in (_slot_date(x) for x in after_slots) if d]
        if parsed:
            existing = parsed

    # assign_reservation_times 는 카테고리별 시간대 배정을 위해 title 이 필요하다.
    refresh_posts = [
        {"title": c.get("clean_title") or c.get("title", "")}
        for c in candidates
    ]
    try:
        slots = assign_reservation_times(
            len(candidates), target_date, refresh_posts,
            cfg=cfg, existing_reservations=existing,
        )
        return list(slots) if slots else []
    except Exception as exc:  # noqa: BLE001 — 배정 실패는 fail-closed(skip)로 흡수
        log.error(f"예약 시각 배정 실패 — 리프레시 전부 skip: {exc}", exc_info=True)
        return []


def run_refresh_batch(max_count: int = MAX_PER_RUN,
                      dry_run: bool = False,
                      progress_cb=None,
                      target_date: "Optional[str]" = None,
                      after_slots: "Optional[list]" = None,
                      driver: "Optional[webdriver.Chrome]" = None,
                      auth_event=None,
                      auth_cb=None) -> dict:
    """
    오늘 리프레시할 글 목록을 가져와서 순서대로 처리합니다.

    실발행 경로(dry_run=False + USE_NAVER_API=False)에서는 예약 발행 메인 루프처럼
    드라이버를 **1회** 생성·로그인해서 루프 전체에서 재사용하고, 루프 종료 후 finally 에서
    **1회** 종료한다(글마다 브라우저를 새로 열고 재로그인하던 문제 방지).
    dry_run 또는 API 경로에서는 드라이버가 필요 없어 생성하지 않는다.

    공유 드라이버 주입(2026-07-09): 호출자(auto_scheduler '오늘 실행')가 이미 로그인한
    드라이버를 driver 인자로 주입하면, init_driver/naver_login/quit 를 **하지 않고** 그
    세션을 그대로 재사용한다 → '오늘 실행'당 로그인·GUI 인증이 딱 1회가 되고, 리프레시가
    별도 브라우저·별도 로그인·터미널 인증을 다시 띄우던 문제(재인증 2회)를 제거한다.
    driver 를 주지 않으면(단독 호출: GUI '전체 자동 리프레시' 버튼·CLI) 기존대로 자체
    드라이버를 1회 생성·로그인하고 finally 에서 1회 종료한다(하위호환).
    dry_run/API 경로에서는 driver 인자가 와도 사용하지 않는다(반영/예약 없음).

    예약 발행(2026-07-09): 실발행 Selenium 경로에서는 각 글을 **즉시발행하지 않고**
    예약 발행한다. after_slots(오늘 예약된 새 글 슬롯)를 받아 그 뒤로 이어붙여 슬롯을
    배정하며(assign_reservation_times), 유효한 슬롯을 배정하지 못한 글은 **건너뛴다**
    (reserve_datetime=None 즉시발행 폴백 금지 — 자동 즉시발행 금지선).

    Args:
        max_count:   리프레시 최대 편수.
        dry_run:     콘텐츠만 생성(네이버 반영·예약 없음).
        progress_cb: 진행률 콜백.
        target_date: 예약 시작 기준일(D). None 이면 오늘. 새 글 파이프라인의
                     target_date 를 그대로 넘겨 배정 기준을 일치시킨다.
        after_slots: 오늘 예약된 새 글 슬롯("YYYY-MM-DD HH:MM") 목록. 이 뒤로
                     리프레시를 이어 예약한다. run_pipeline 이 reserve_times 를 전달.
        driver:      호출자가 이미 로그인해 둔 공유 WebDriver.
                     - 주어지면(실발행 Selenium 경로): init_driver/naver_login 없이
                       그 세션으로 각 글을 재발행하고, **호출자 소유이므로 finally 에서
                       quit 하지 않는다**.
                     - None(단독 호출): 기존대로 자체 드라이버를 생성·로그인하고
                       finally 에서 종료한다.
                     dry_run/API 경로에서는 사용되지 않는다(주어져도 무시).
        auth_event:  GUI 보안 인증 완료 대기용 threading.Event. **단독 호출**(driver=None)
                     의 자체 로그인에만 전달된다. 공유 드라이버 주입 시에는 호출자가 이미
                     인증을 마쳤으므로 사용되지 않는다.
        auth_cb:     보안 인증 상태 알림 콜백. auth_event 와 동일한 조건에서만 사용.

    Returns: {"total": int, "success": int, "results": [...]}
             (로그인 실패 시 추가로 "error": "login_failed")
    """
    candidates = get_refresh_candidates(max_count)
    if not candidates:
        log.info("리프레시 대상 없음 (30일 미만 또는 모두 최근 갱신됨)")
        return {"total": 0, "success": 0, "results": []}

    log.info(f"{'─'*54}")
    log.info(f"리프레시 대상: {len(candidates)}개")
    for c in candidates:
        log.info(f"  · {c['clean_title'][:40]}  ({c['age_days']}일 경과)")
    log.info(f"{'─'*54}")

    # 실발행 Selenium 경로일 때만 공유 드라이버가 필요.
    # dry_run: 반영 없음 / USE_NAVER_API: HTTP API 경로 → 둘 다 드라이버 불필요.
    use_shared_driver = False
    cfg = None
    if not dry_run:
        try:
            from config import settings as s
            cfg = s
            use_shared_driver = not s.USE_NAVER_API
        except (ImportError, AttributeError):
            # 설정 로드 실패 시 refresh_post 폴백과 동일하게 Selenium 경로로 간주.
            use_shared_driver = True

    # 예약 슬롯 배정(실발행 Selenium 경로에서만). dry_run/API 경로는 reserve_datetime
    # 이 무의미하므로 배정하지 않는다(기존 동작 유지).
    slots: list = []
    if use_shared_driver:
        slots = _assign_refresh_slots(candidates, target_date, after_slots, cfg)
        if slots:
            log.info(f"  📅 리프레시 예약: {slots[0]} ~ {slots[-1]} (새 글 뒤 이어붙임)")
        else:
            log.warning("  ⛔ 리프레시 예약 슬롯 배정 실패 — 슬롯 없는 글은 skip(즉시발행 폴백 금지)")

    total = len(candidates)
    results: list = []
    success = 0
    shared_driver = None
    owns_driver = False   # 자체 생성한 드라이버만 finally 에서 quit(외부 공유는 호출자 소유)
    try:
        # 루프 전에 드라이버를 준비한다(예약 메인 루프와 동일하게 로그인 1회).
        if use_shared_driver:
            if driver is not None:
                # 호출자(auto_scheduler '오늘 실행')가 이미 로그인해 둔 공유 드라이버 —
                # init_driver/naver_login 을 호출하지 않고 그 세션을 그대로 재사용한다
                # ('오늘 실행'당 로그인·인증 1회, 재인증 방지). 이 드라이버는 호출자
                # 소유이므로 finally 에서 종료하지 않는다.
                shared_driver = driver
                owns_driver = False
            else:
                # 단독 호출(GUI '전체 자동 리프레시' 버튼·CLI) — 기존 동작 유지:
                # 자체 드라이버 1회 생성 + 로그인 1회, finally 에서 1회 종료.
                # auth_event/auth_cb 가 있으면 GUI 보안 인증 경로로, 없으면 기존 폴백.
                from core.naver_uploader import init_driver, naver_login
                shared_driver = init_driver()
                owns_driver = True
                if not naver_login(shared_driver, auth_event=auth_event,
                                   auth_cb=auth_cb):
                    log.error("리프레시 배치 중단 — 네이버 로그인 실패")
                    return {"total": total, "success": 0,
                            "results": results, "error": "login_failed"}

        for i, rec in enumerate(candidates):
            # fail-closed: 실발행 Selenium 경로에서 유효한 예약 슬롯이 없으면 그 글을
            # 건너뛴다. 절대 reserve_datetime=None 으로 즉시발행에 폴백하지 않는다.
            reserve_slot = None
            if use_shared_driver:
                reserve_slot = slots[i] if i < len(slots) else None
                if not (reserve_slot and _SLOT_RE.match(reserve_slot)):
                    log.warning(
                        f"  ⏭  리프레시 skip(예약 슬롯 없음): "
                        f"{rec.get('clean_title','')[:40]}")
                    results.append({
                        "success": False, "error": "no_reservation_slot",
                        "original": rec.get("clean_title", ""),
                    })
                    continue

            def _prog(msg, cur, tot, _i=i, _total=total):
                if progress_cb:
                    progress_cb(msg, _i * 3 + cur, _total * 3)
            # 배치 회복력: 한 글이 실패해도 배치를 멈추지 않고 다음 글을 계속 처리한다
            # (예약 발행 메인 루프와 동일한 패턴). 공유 드라이버는 여기서 종료하지 않는다.
            try:
                result = refresh_post(rec, dry_run=dry_run, progress_cb=_prog,
                                      driver=shared_driver,
                                      reserve_datetime=reserve_slot)
            except Exception as exc:  # noqa: BLE001 — 배치 전체 중단 방지, 로그로 추적
                log.error(f"리프레시 실패(건너뜀): {rec.get('clean_title','')[:40]} — {exc}",
                          exc_info=True)
                result = {"success": False, "error": str(exc)}
            results.append({**result, "original": rec.get("clean_title", "")})
            if result.get("success"):
                success += 1
            time.sleep(2)   # 발행 간격
    finally:
        # 자체 생성한 드라이버만 루프 종료 후 1회 종료한다(정상·중단·예외 모두).
        # 외부에서 주입된 공유 드라이버는 호출자(auto_scheduler)가 자신의 finally
        # 에서 종료하므로 여기서 quit 하지 않는다(이중 종료·조기 종료 방지).
        if owns_driver and shared_driver is not None:
            try:
                shared_driver.quit()
            except Exception:  # noqa: BLE001 — 종료 실패는 결과에 영향 없음
                log.debug("리프레시 배치 드라이버 종료 실패", exc_info=True)

    log.info(f"리프레시 완료: {success}/{total}개")
    return {"total": total, "success": success, "results": results}


# ──────────────────────────────────────────────────────
# 통계 (GUI용)
# ──────────────────────────────────────────────────────
def get_refresh_stats() -> dict:
    logs    = _load_refresh_log()
    records = _load_history()
    today   = date.today()

    total_posts   = len(records)
    need_refresh  = len(get_refresh_candidates(999))
    total_refreshed = len(logs)
    recent        = sorted(logs, key=lambda x: x.get("refreshed_at",""), reverse=True)[:5]

    return {
        "total_posts":     total_posts,
        "need_refresh":    need_refresh,
        "total_refreshed": total_refreshed,
        "recent":          recent,
    }
