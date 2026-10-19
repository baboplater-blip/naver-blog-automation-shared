# core/engagement_scorer.py — 체류·경험 신호 스코어러 (Phase 4 · L4)
"""추천피드 레버 = 체류시간. DIA(검색)·AEO(AI요약)에 이어 세 번째 품질 축.

네이버 추천/홈 피드는 체류시간·완독률 같은 '경험 신호'를 본다. 이 스코어러는
글(HTML)이 독자를 얼마나 붙잡고 끝까지 읽히는지를 8개 신호로 채점한다.

- aeo_scorer 와 동일 인터페이스: score(html) → EngagementResult(.score/.grades/.passed()).
- 부수효과 없음(순수). fail-open.

신호(가중 합 100):
  hook 18 · image_rhythm 15 · firsthand 12 · scroll_anchor 13 · readability 13
  · stat_density 11 · judgment 10 · subscribe_cta 8

judgment(판단 흔적, 2026-08-01 신설)
  네이버 AI 요약은 뻔한 정보 나열을 스스로 더 잘 요약한다. 인용·체류를 만드는 건
  "직접 따져본 사람의 판단"이 남아 있는 글이다. 확인·비교 과정, 주관 판단(단점·주의),
  상황별 추천 마커를 센다. firsthand 와 일부 표현이 겹치지만(예: "확인해보니"),
  firsthand 는 '경험의 존재', judgment 는 '판단·유보·단점의 존재'를 본다.
  가중치 10은 보수적으로 잡았다 — 이 스코어러는 발행 게이트가 아니라 리포트·분류용
  지표(growth_dashboard 합격률, draft_triage 의 참고 필드 `eng` — 버킷 판정은
  수요·aeo 로만 하므로 이 점수는 분류를 바꾸지 않는다)다.

  ⚠️ 실측(2026-08-02, draft_store 보관본 1,807편): judgment 평균 0.34회, 71%가 0회.
  (같은 날 "헷갈리"→"헷갈" 어간 교정 전 기준선은 0.32회·72.2%였다 — 활용형 오탐 교정으로
   +0.02 움직인 것뿐이라 이 기준선의 해석은 그대로다.)
  판단 흔적 프롬프트가 들어가기 **전에** 생성된 보관본이라 당연한 결과이고, 이게 정직한
  기준선이다. (같은 코퍼스에서 접속어 "다만"을 판단으로 세던 초판 정규식은 평균 1.42회를
  보고했다 — 68.6%의 글이 "다만" 하나로 점수를 받던 허영 지표였다. H1 감사에서 제거.)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List

PASS_THRESHOLD = 70

# hook(도입 훅) 판정 창 — 본문 앞 N자.
# 2026-08-02: 350 → 700 확대. P1-④b 로 도입부가 [인사말 + 훅 + 3줄 요약(3문장)] 구성이
# 되면서, 훅 뒤에 오던 판단 흔적·본문 첫 문단이 350자 밖으로 밀려 훅이 있는 글도 absent
# 로 잡혔다(라이브 10편 중 3편이 이 이유로 오탐). 3줄 요약 3문장이 대략 300~350자를
# 차지하므로, 그만큼을 더한 700자가 "독자가 도입부로 인식하는 구간"에 맞는다.
HOOK_WINDOW_CHARS = 700

# 클릭베이트 감점 판정 창 — hook 과 **일부러 다른 값**(확대하지 않음).
# 이 페널티가 모델링하는 것은 "독자가 이탈을 결정하기 전에 보는 낚시성 도입"이고,
# 그 구간은 3줄 요약이 들어와도 변하지 않는다. 반대로 700 까지 넓히면 본문 초반의
# 정상 서술("신청 기한을 놓치면", "최악의 경우")까지 낚시로 감점한다 —
# 실측(보관본 1,807편)에서 350~700 구간에만 과장어가 있는 글이 28편(1.5%) 있었고,
# 창을 넓혀도 체류 PASS 수는 동일(143편)했다. 즉 확대 이득은 0, 오탐 위험만 늘어난다.
CLICKBAIT_WINDOW_CHARS = 350

# 도입부에 낚시·과장·공포 유도어가 있으면 이만큼 hook 점수를 감점한다(포인트 단위).
# 네이버 D.I.A.는 클릭베이트 도입부를 감점 신호로 본다(korean-seo-rules "낚시성 분류").
# 스코어러가 만점 주는 방향이 검색 감점 방향과 반대가 되지 않도록, 과장어는
# 가점 대상에서 제외하고 여기서 별도 페널티를 부과한다. 감점은 hook 점수 이하로만
# 적용돼 총점이 음수가 되지 않는다.
CLICKBAIT_PENALTY = 8.0

# 가중치 합 = 100 (강제 방식은 아래 주석 참조). judgment 10점은 다음에서 갹출했다:
#   firsthand -4  … judgment 가 옛 firsthand 의 '판단·해석' 몫을 떼어 나가므로 최대 갹출
#   subscribe_cta -2 … 한 줄이면 만점인 이진 신호라 2점 삭감의 실질 손실이 가장 작다
#   image_rhythm/scroll_anchor/readability/stat_density 각 -1 … 나머지 비례 삭감
#   hook 18 유지 … 도입 이탈 방어는 체류의 1차 관문이라 손대지 않는다
_WEIGHTS = {
    "hook": 18, "image_rhythm": 15, "firsthand": 12, "scroll_anchor": 13,
    "readability": 13, "stat_density": 11, "judgment": 10, "subscribe_cta": 8,
}
_LABEL = {
    "hook": "도입 훅", "image_rhythm": "이미지 리듬", "firsthand": "1차 자료·경험",
    "scroll_anchor": "스크롤 유인", "readability": "문단 가독성",
    "stat_density": "수치 밀도", "judgment": "판단 흔적", "subscribe_cta": "구독 유도",
}
# ⚠️ 총점 100 스케일은 PASS_THRESHOLD(70)·리포트 합격률의 전제다. 신호를 더하거나
#    가중치를 조정할 때는 합 100을 유지할 것(회귀 가드는
#    tests/test_judgment_signal.py::test_weights_sum_to_100 — 임포트 시점에 raise 하면
#    ImportError 만 잡는 호출부(draft_triage·growth_dashboard)의 fail-open 이 깨지므로
#    런타임 assert 대신 테스트로 강제한다).


@dataclass
class EngagementResult:
    score: float
    grades: dict = field(default_factory=dict)
    weaknesses: List[str] = field(default_factory=list)
    counts: dict = field(default_factory=dict)

    def passed(self, threshold: int = PASS_THRESHOLD) -> bool:
        return self.score >= threshold

    def to_dict(self) -> dict:
        return {"score": round(self.score, 1), "passed": self.passed(),
                "grades": dict(self.grades), "weaknesses": list(self.weaknesses),
                "counts": dict(self.counts)}


# ─── 신호 정규식 ───────────────────────────────────────
# hook 가점 신호 = 정상적 호기심·질문형 훅만. 과장·공포 유도어(놓치면·낭패·후회…)는
# 여기서 제외하고 _CLICKBAIT_RE 로 별도 감점한다(가점 방향이 D.I.A. 감점 방향과
# 반대가 되는 것을 방지 — korean-seo-rules "낚시성 분류").
# ⚠️ 한국어 활용형 주의 — "헷갈리"로 쓰면 "헷갈린다/헷갈려/헷갈림"을 못 잡는다.
#    받침 ㄴ 이 붙으면 "리"가 "린"이라는 **다른 음절**로 합쳐지기 때문(부분 문자열이 아님).
#    실측(라이브 post_01): 첫 문장에 "헷갈린다는"이 있는데도 hook=0 → absent 오탐.
#    아래 _JUDGMENT_RE 의 "다만…" 분기는 이미 어간 "헷갈"을 쓰고 있어, 여기만 활용형에
#    걸려 있던 것이다. 두 곳 모두 어간으로 통일한다.
_HOOK_RE = re.compile(
    r"혹시|아시나요|아세요|이것만|모르면|한\s*번쯤|"
    r"들어보셨|막막|헷갈|고민이라면|\?\s*$", re.MULTILINE)
# 클릭베이트(과장·공포·충격) 유도어 — 도입부에 있으면 hook 감점.
# 정상 질문형 훅("혹시 ~아시나요")과 겹치지 않는, 자극·과장 표현만 모은다.
_CLICKBAIT_RE = re.compile(
    r"놓치면|낭패|후회|충격|소름|경악|대박|헉|실화|미쳤|"
    r"반전|절대\s*하지|폭탄|공포|끔찍|최악|믿기지")
_FIRSTHAND_RE = re.compile(
    r"직접|해보니|해봤|후기|실제로|사례|경험상|캡처|스크린샷|따라\s*하|"
    r"제가|저는|필자|체크리스트")
# judgment(판단 흔적) — "AI가 대신 요약해버리면 그만인 문서"와 갈리는 차별 신호.
# 세 갈래를 한 정규식으로 센다:
#   ① 확인·비교 과정("확인해보니", "찾아보니")  ② 주관 판단·단점·주의("헷갈", "까다로")
#   ③ 상황별 추천/유보("추천드리는 분", "이런 경우라면")
# ⚠️ 지어낸 1인칭 경험담을 가점하지 않도록, 사건·수치를 주장하는 표현("받았는데" 등)은
#    일부러 넣지 않았다 — 여기서 보는 건 '판단·유보'이지 '겪은 사건'이 아니다.
# ⚠️ 허영 지표 방지(2026-08-02 감사 H1) — 아래 두 부류는 단독 매칭에서 제외했다:
#   · "다만"·"개인적으로": 순수 사실 나열문에도 흔한 접속·완충어라 판단이 0인 글이
#     judgment 2히트를 얻었다(실측 재현). "다만"은 뒤에 실제 판단어가 붙은 형태만 인정.
#   · "이런 분은": "이런 분은 대상이 아닙니다" 같은 자격 배제 문장과도 매칭됐다.
#     뒤에 추천·권유어가 따라오는 형태(=상황별 추천)만 인정한다.
#   절 안(마침표·개행 전)으로 창을 제한해 문장을 건너뛴 오매칭도 막는다.
_JUDGMENT_RE = re.compile(
    r"확인해\s*보니|확인해\s*봤|찾아보니|알아보니|비교해\s*보니|따져보니|살펴보니|"
    r"읽어보니|헷갈|까다로|번거로|주의할|주의해야|아쉬운|아쉽|"
    r"놓치기\s*쉬운|틀리기\s*쉬운|추천드리는\s*분|추천하는\s*분|"
    r"이런\s*분(?:에게|은|이라면|이시라면)[^.\n]{0,15}(?:추천|권해|권하|권장|적합|어울|낫)|"
    r"이런\s*경우라면|"
    r"다만[^.\n]{0,15}(?:헷갈|까다|번거|아쉬|주의|한계|단점|어렵)")
_STAT_RE = re.compile(r"\d[\d,\.]*\s*(?:만원|천원|억원|원|%|퍼센트|명|건|회|일|개월|년|배|점|%p|㎡|평)")
_SCROLL_RE = re.compile(
    r"목차|아래에서|아래\s*표|자세히|이어서|다음\s*단계|계속해서|정리하면|"
    r"먼저.*그다음|순서대로")
# "이웃 추가"(띄어쓰기)도 인정 — 실제 마무리 CTA는 띄어 쓰는 경우가 흔함.
_SUBSCRIBE_RE = re.compile(r"이웃\s*추가|서로이웃|이웃\s*신청|구독|알림\s*설정|팔로우|이웃\s*맺")
_TAG_RE = re.compile(r"<[^>]+>")
# 이미지: HTML <img> · 마크다운 ![]() · 발행 전 <!-- IMG: --> 자리 마커 모두 인정
# (리프레시는 마크다운을 채점하므로 <img> 만 세면 image_rhythm 이 항상 0으로 과소평가됨)
_IMG_RE = re.compile(r"<img\b|!\[|<!--\s*IMG", re.IGNORECASE)
# 표: HTML <table> · 마크다운 구분행(|---|---|) 인정
_TABLE_RE = re.compile(r"<table\b|^\s*\|?[ \t:|-]*-{3,}[ \t:|-]*$",
                       re.IGNORECASE | re.MULTILINE)
_P_RE = re.compile(r"<p\b[^>]*>(.*?)</p>", re.IGNORECASE | re.DOTALL)


def _text(html: str) -> str:
    return _TAG_RE.sub(" ", html or "")


def _count(rx, s) -> int:
    return len(rx.findall(s or ""))


def _grade(points: float, weight: float) -> str:
    if points <= 0:
        return "absent"
    if points >= weight * 0.95:
        return "strong"
    if points >= weight * 0.5:
        return "good"
    return "below_min"


def score(html: str) -> EngagementResult:
    html = html or ""
    text = _text(html)
    tlen = max(len(text), 1)
    counts, grades, pts = {}, {}, {}

    # 1) hook — 도입부(앞 HOOK_WINDOW_CHARS 자)에 호기심·스테이크 유발
    #    과장·공포 유도어(클릭베이트)는 가점 대상이 아니라 감점: D.I.A.가 낚시성
    #    도입부를 감점하므로 스코어러도 같은 방향으로 페널티를 준다. 감점은 hook
    #    포인트 이하로만 적용해 총점이 음수로 새지 않는다.
    #    ⚠️ 두 창은 의도적으로 다르다 — 상수 정의부의 근거 참고.
    hook_hits = _count(_HOOK_RE, text[:HOOK_WINDOW_CHARS])
    clickbait_hits = _count(_CLICKBAIT_RE, text[:CLICKBAIT_WINDOW_CHARS])
    counts["hook"] = hook_hits
    counts["clickbait"] = clickbait_hits
    hook_pts = _WEIGHTS["hook"] * min(1.0, hook_hits / 2)
    if clickbait_hits:
        hook_pts = max(0.0, hook_pts - CLICKBAIT_PENALTY)
    pts["hook"] = hook_pts

    # 2) image_rhythm — 이미지가 본문에 고르게(500자당 ~1장 목표)
    imgs = _count(_IMG_RE, html)
    counts["images"] = imgs
    target = max(1, tlen // 500)
    pts["image_rhythm"] = _WEIGHTS["image_rhythm"] * min(1.0, imgs / target) if target else 0

    # 3) firsthand — 직접 경험·캡처·표(E-E-A-T 경험)
    fh = _count(_FIRSTHAND_RE, text) + _count(_TABLE_RE, html)
    counts["firsthand"] = fh
    pts["firsthand"] = _WEIGHTS["firsthand"] * min(1.0, fh / 3)

    # 4) scroll_anchor — 목차·중간요약·"자세히" 등 스크롤 유인
    sa = _count(_SCROLL_RE, text)
    counts["scroll_anchor"] = sa
    pts["scroll_anchor"] = _WEIGHTS["scroll_anchor"] * min(1.0, sa / 3)

    # 5) readability — 문단이 벽처럼 길지 않은가(짧은 문단 비율)
    paras = [_TAG_RE.sub(" ", p).strip() for p in _P_RE.findall(html)]
    paras = [p for p in paras if p]
    if not paras:  # <p> 없으면 개행 단락으로 근사
        paras = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
    if paras:
        short = sum(1 for p in paras if len(p) <= 280)
        ratio = short / len(paras)
        counts["paragraphs"] = len(paras)
        counts["short_paragraphs"] = short
        pts["readability"] = _WEIGHTS["readability"] * ratio
    else:
        counts["paragraphs"] = 0
        pts["readability"] = 0

    # 6) stat_density — 수치 밀도(1000자당 ~3개 목표)
    stats = _count(_STAT_RE, text)
    counts["stats"] = stats
    stat_target = max(3, tlen // 1000 * 3)
    pts["stat_density"] = _WEIGHTS["stat_density"] * min(1.0, stats / stat_target) if stat_target else 0

    # 7) judgment — 판단 흔적(계기·직접 확인·주관 판단·상황별 추천)
    #    3개를 만점 기준으로 둔다: 프롬프트가 요구하는 최소 구성(확인 과정 1 + 주관
    #    판단/단점 1 + 마무리 상황별 추천 1)이 정확히 3개이므로, 그 구성을 갖춘 글만
    #    strong 을 받는다.
    jg = _count(_JUDGMENT_RE, text)
    counts["judgment"] = jg
    pts["judgment"] = _WEIGHTS["judgment"] * min(1.0, jg / 3)

    # 8) subscribe_cta — 이웃/구독 유도(추천피드→구독 전환)
    sc = _count(_SUBSCRIBE_RE, text)
    counts["subscribe_cta"] = sc
    pts["subscribe_cta"] = _WEIGHTS["subscribe_cta"] * min(1.0, sc / 1)

    total = 0.0
    weaknesses = []
    for k, w in _WEIGHTS.items():
        p = pts.get(k, 0.0)
        total += p
        grades[k] = _grade(p, w)
        if grades[k] in ("absent", "below_min"):
            weaknesses.append(_LABEL[k])

    return EngagementResult(score=round(total, 1), grades=grades,
                            weaknesses=weaknesses, counts=counts)


def weakness_hint(result: EngagementResult) -> str:
    """약점 → 개선 지시 한 줄(리프레시/재작성 프롬프트용)."""
    tips = {
        "도입 훅": "도입부 첫 문단에 독자 질문 paraphrase·구체 수치로 호기심 유발(과장·공포·낚시어 금지)",
        "이미지 리듬": "본문 2~4문단마다 이미지 1장(스크린샷·표) 고르게 배치",
        "1차 자료·경험": "직접 캡처·실제 사례·체크리스트·표로 경험 신호 추가",
        "스크롤 유인": "목차·중간 요약·'아래에서 자세히' 앵커로 스크롤 유도",
        "문단 가독성": "긴 문단을 3~4줄로 쪼개 벽 텍스트 제거",
        "수치 밀도": "구체적 금액·비율·기간 등 수치를 문단마다 삽입",
        # 이 힌트는 재작성·리프레시 프롬프트로 흘러간다. 그 턴에 조회 도구가 있다는
        # 보장이 없으므로 "직접 확인했다"는 행위 주장을 유도하지 않는다(감사 C1 과 같은 이유).
        "판단 흔적": "도입부 직후 2~4문장으로 계기·자료에서 헷갈리거나 까다로운 지점을, "
                     "마무리에 '이런 분에게 추천' 1줄 추가(겪지 않은 경험·수치 창작 금지)",
        "구독 유도": "마무리에 이웃추가·구독 유도 문구 1줄",
    }
    return " · ".join(tips.get(w, w) for w in result.weaknesses[:3])


__all__ = ["EngagementResult", "PASS_THRESHOLD", "CLICKBAIT_PENALTY",
           "HOOK_WINDOW_CHARS", "CLICKBAIT_WINDOW_CHARS",
           "score", "weakness_hint"]
