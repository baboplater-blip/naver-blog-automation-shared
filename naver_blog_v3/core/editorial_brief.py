# core/editorial_brief.py
# 🧭 한 번의 집필 호출에서 받는 '편집 브리프' — 본문 초점 + 썸네일 메시지 + 이미지 역할
# ─────────────────────────────────────────────────────────────────────────────
# 이 모듈이 존재하는 이유
#   ① 본문과 썸네일이 **같은 질문**을 겨냥하게 한다. 예전에는 본문은 집필 프롬프트가,
#      썸네일 문구는 "제목 앞 16자 기계 절단"이 각각 정해 서로 다른 말을 했다.
#   ② 그 조율을 위해 **기획 LLM 을 따로 부르지 않는다.** 집필 호출의 같은 JSON 에
#      `editorial` 키 하나를 얹어 받는다(추가 호출 0회 — 2026-09-10 설계 계승).
#   ③ 받은 브리프는 글·이미지 계획과 함께 additive 로 영속돼, 재생성·저장·이미지 채움·
#      무인 운영이 **같은 브리프**를 본다.
#
# 설계 원칙
# - 순수 로컬: 이 모듈은 LLM·네트워크·서브프로세스를 호출하지 않는다.
# - 사실을 만들지 않는다: 로컬 폴백은 **제목에서 유도할 수 있는 것만** 채우고, 모르는
#   칸은 빈 문자열로 남긴다(모델이 준 답변 요약을 우리가 지어내지 않는다).
# - 인젝션 안전: 모든 문자열은 프롬프트에 들어가기 전에 정규화한다.
# - fail-open: 공개 함수는 예외를 밖으로 던지지 않는다.
# - 레거시 읽기 호환: 브리프가 없는 옛 글도 그대로 읽히고, 없으면 없는 대로 동작한다.
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, List, Optional

try:
    from config.logger import get_logger
    log = get_logger(__name__)
except Exception:  # noqa: BLE001
    import logging
    log = logging.getLogger(__name__)


BRIEF_VERSION: int = 1

LIMIT_QUESTION: int = 120
LIMIT_ANSWER: int = 160
LIMIT_ACTION: int = 120
LIMIT_MESSAGE: int = 40          # 참고 레이아웃의 질문·혜택 문구를 담는 상한
LIMIT_FOCUS: int = 60

FAQ_COUNT_MAX: int = 8

_WS_RE = re.compile(r"\s+")
# 쉼표를 남긴다 — 예전에는 여기서 지워져 천단위 구분자(1,918)가 공백이 되면서
# "1 918원" 처럼 수치가 두 어절로 쪼개졌다(아래 어절 경계 절단이 "1" 만 남기는 원인).
_DECOR_RE = re.compile(r"[^0-9A-Za-z가-힣\s·:%~\-?,.]+")
_DATE_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\s*[—\-]\s*")
_YEAR_RE = re.compile(r"(?<!\d)(19|20)\d{2}년?(?!\d)")
# 구분자 쉼표만 자른다 — 숫자 사이 쉼표(1,918)는 수치 한 덩어리의 일부라 자르지 않는다.
_SEGMENT_SPLIT_RE = re.compile(r"[·/|]+|(?<!\d),|,(?!\d)")
# 절단으로 단위를 잃고 홀로 남은 수치 토큰("… 시간당 1"). 숫자만 남으면 금액을 오해시킨다.
_ORPHAN_NUMBER_RE = re.compile(r"\s\d[\d,.]*$")
_NUMBER_WITH_UNIT_RE = re.compile(r"\d[\d,.]*(?:\s?(?:만원|천원|원|억원|%|퍼센트|개월|주|일|시간|명|개|회|배|km|m|kg|g))?")
_RANGE_AMOUNT_RE = re.compile(
    r"\d[\d,.]*(?:만|천)?\s*[~\-–]\s*\d[\d,.]*(?:만|천)?"
    r"\s*(?:만원|천원|원|억원|%|퍼센트|개월|주|일|시간|명|개|회|배|km|m|kg|g)?"
)
_GOOD_HOOK_RE = re.compile(
    r"(?:까$|나요$|인가요$|할까$|일까$|될까$|가능할까$|"
    r"확인|체크|준비|계산|차이|얼마|누가|어디|어떻게|나도|선택할까|비교할까|"
    r"(?:비용|요금|가격)은?$)"
)
_BENEFIT_RE = re.compile(r"지원|환급|혜택|수급|보조|장려|급여|감면|할인|신청")
_RISK_ACTION_RE = re.compile(r"부정수급|부정청구|환수|불이익|사후대응|주의|처벌|제재|반환|취소|위반")


def sanitize(text: Any, *, limit: int) -> str:
    """제어문자·백틱·큰따옴표 제거 + 공백 접기 + 상한 절단(image_plan.sanitize 와 동일 규칙)."""
    if not isinstance(text, str) or not text:
        return ""
    cleaned = "".join(" " if (ord(c) < 0x20 or ord(c) == 0x7F) else c for c in text)
    cleaned = cleaned.replace("`", "")
    for quote in ('"', "“", "”"):
        cleaned = cleaned.replace(quote, "'")
    return _WS_RE.sub(" ", cleaned).strip()[:limit]


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ─────────────────────────────────────────────────────────────────────────────
# 썸네일 메시지 — '해결할 일' 중심의 안전한 로컬 폴백
# ─────────────────────────────────────────────────────────────────────────────
def _title_message_seed(title: Any, *, limit: int = LIMIT_MESSAGE) -> str:
    """제목에서 썸네일 메시지를 만든다 — **기계적 앞자리 절단을 하지 않는다.**

    예전 규칙(`image_plan.thumb_label_from_title`)은 제목 앞 16자를 잘라 "2026 청년월세지원
    신청" 처럼 연도가 절반을 먹고 정작 독자가 궁금한 부분이 잘려 나갔다. 여기서는:

      ① 발행풀 날짜 접두와 장식 문자를 떼고,
      ② **연도 토큰을 지운다**(썸네일에서 '2026'은 정보량이 거의 없다),
      ③ 제목을 구분자(`·`, `,`, `/`)로 나눠 **핵심 어구 + 마지막 한정 어구**를 쓰고,
      ④ 상한을 넘으면 **어절 경계**에서 끊는다(글자 중간 절단 금지).

    주제 어휘 사전을 쓰지 않으므로 어떤 프로필·분야에서도 같은 규칙으로 동작한다.
    """
    text = _DATE_PREFIX_RE.sub("", str(title or "")).strip()
    text = sanitize(text, limit=200)
    text = _YEAR_RE.sub(" ", text)
    text = _DECOR_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text).strip()
    if not text:
        return ""

    segments = [s.strip() for s in _SEGMENT_SPLIT_RE.split(text) if s.strip()]
    if not segments:
        return ""

    head = segments[0]
    candidate = head
    if len(segments) > 1:
        tail = segments[-1]
        joined = f"{head} {tail}".strip()
        if tail != head and len(joined) <= limit:
            candidate = joined
    if len(candidate) <= limit:
        return candidate

    # 어절 경계 절단 — 한 어절도 못 담을 만큼 길면 그때만 글자 단위로 자른다.
    words = candidate.split(" ")
    picked: List[str] = []
    for word in words:
        trial = " ".join(picked + [word])
        if picked and len(trial) > limit:
            break
        picked.append(word)
    out = " ".join(picked).strip()
    # 잘려 나간 부분이 있는데 끝이 수치면(단위를 잃은 "… 시간당 1") 그 토큰은 버린다.
    if len(picked) < len(words):
        trimmed = _ORPHAN_NUMBER_RE.sub("", out).strip()
        if trimmed:
            out = trimmed
    return out[:limit] if out else candidate[:limit]


def _whole_word_prefix(text: str, limit: int) -> str:
    """상한 안에서 어절과 숫자·단위 토큰을 온전히 유지한다."""
    picked: List[str] = []
    for word in text.split():
        trial = " ".join((*picked, word))
        if len(trial) > limit:
            break
        picked.append(word)
    return " ".join(picked).strip()


def _question_hook(question: Any, *, source: str) -> str:
    """본문 질문을 주제에 맞는 짧은 접미 hook으로 압축한다."""
    text = sanitize(question, limit=LIMIT_QUESTION).lower()
    risk_context = f"{source} {text}"
    if _RISK_ACTION_RE.search(risk_context):
        if re.search(r"대응|처리|조치", text):
            return " 어떻게 대응할까?"
        return " 확인 기준은?"
    if re.search(r"받을\s*수|수급|지원받|환급받", text):
        return " 받을 수 있을까?"
    if re.search(r"신청\s*(?:할\s*수|가능)", text):
        return " 신청 가능할까?"
    if re.search(r"대상|자격|누가", text):
        return " 대상일까?"
    if re.search(r"얼마|금액|비용", text):
        return " 얼마일까?"
    if re.search(r"언제|기간|마감", text):
        return " 언제까지일까?"
    if re.search(r"비교|차이", text):
        return " 차이는?"
    if re.search(r"확인|어떻게", text):
        return " 확인할까?"
    if _BENEFIT_RE.search(source):
        return " 가능할까?"
    if re.search(r"비용|요금|가격", source):
        return " 비용 차이는?"
    return " 먼저 확인할 점은?"


def _compact_range_fallback(title_seed: str, *, limit: int) -> str:
    """제목 폴백의 범위 수치를 보존해 주제와 짧은 질문으로 줄인다."""
    match = _RANGE_AMOUNT_RE.search(title_seed)
    if not match:
        return ""
    before = title_seed[:match.start()].split()
    after = title_seed[match.end():].split()
    if not before:
        return ""
    # 질문이 비용을 다루면 제목에서 같은 단어를 반복하지 않는다.
    has_cost_topic = before[-1] in {"비용", "요금", "가격"} or any(
        word in {"비용", "요금", "가격"} for word in after
    )
    if before[-1] in {"비용", "요금", "가격"}:
        before.pop()
    topic = " ".join(before[:2] or before)
    amount = match.group().strip()
    suffix = " 비용 차이는?" if has_cost_topic else " 차이는?"
    result = f"{topic} {amount}{suffix}".strip()
    return result if len(result) <= limit else ""


def _has_title_facts(candidate: str, title_seed: str) -> bool:
    """이미 좋은 후보에 제목의 주제어와 숫자·단위가 모두 남았는지 확인한다."""
    title_words = title_seed.split()
    anchors = [word for word in title_words
               if not _NUMBER_WITH_UNIT_RE.search(word)
               and word not in {"최대", "최고", "월", "연", "총", "약"}]
    topic_present = not anchors or any(anchor in candidate for anchor in anchors[:2])
    amounts = _title_amount_facts(title_seed)
    return topic_present and all(amount in candidate for amount in amounts)


def _title_anchor_words(title_seed: str) -> List[str]:
    return [word for word in title_seed.split()
            if not _NUMBER_WITH_UNIT_RE.search(word)
            and word not in {"최대", "최고", "월", "연", "총", "약"}][:2]


def _title_amount_facts(title_seed: str) -> List[str]:
    words = title_seed.split()
    facts: List[str] = []
    for index, word in enumerate(words):
        if not _NUMBER_WITH_UNIT_RE.search(word):
            continue
        if index > 0 and words[index - 1] in {"최대", "약", "총"}:
            facts.append(f"{words[index - 1]} {word}")
        else:
            facts.append(word)
    return facts


def resolve_thumbnail_message(candidate: Any, *, title: Any = "", question: Any = "",
                               limit: int = LIMIT_MESSAGE) -> str:
    """주제와 독자의 판단 지점을 담는 썸네일 문구를 추가 호출 없이 정한다.

    입력에 없는 숫자나 혜택은 만들지 않는다. 제목을 그대로 되풀이한 문구에는
    독자가 얻을 판단 지점이나 먼저 확인할 항목을 보태고, 이미 유용한 질문은 보존한다.
    """
    limit = max(1, min(LIMIT_MESSAGE, int(limit or LIMIT_MESSAGE)))
    title_seed = _title_message_seed(title, limit=200)
    candidate_text = sanitize(candidate, limit=200)
    candidate_text = _DECOR_RE.sub(" ", candidate_text).replace("?", "?")
    candidate_text = _WS_RE.sub(" ", candidate_text).strip()
    source = candidate_text or title_seed
    source = _DECOR_RE.sub(" ", source).replace("?", "?")
    source = _WS_RE.sub(" ", source).strip()
    if not source:
        return ""
    if (not candidate_text and not question and title_seed
            and not _GOOD_HOOK_RE.search(title_seed)
            and not _RISK_ACTION_RE.search(title_seed)):
        compact_range = _compact_range_fallback(title_seed, limit=limit)
        if compact_range:
            return compact_range
    source_core = source.rstrip("? ").strip()
    source_hooked = bool(_GOOD_HOOK_RE.search(source_core))
    if not source_hooked:
        # A trailing question mark alone does not make a title a useful reader hook.
        # Drop it before adding the more actionable fallback question below.
        source = source_core
    if (len(source) <= limit and source_hooked
            and _has_title_facts(source, title_seed)):
        return source
    # 후보가 제목의 핵심 주제/금액을 빠뜨렸다면 제목을 바탕으로 복원하고,
    # 이미 좋은 후보나 reader_question은 접미 hook만 제공한다.
    hook_source = question if question else (candidate_text if source_hooked else "")
    if title_seed and not _has_title_facts(source, title_seed):
        source = title_seed
    suffix = _question_hook(hook_source, source=source)
    # 공백 하나도 예산에 포함한다. 지나치게 긴 앞부분은 완전한 어절 단위로 줄인다.
    prefix = _whole_word_prefix(source, limit - len(suffix))
    words = source.split()
    if not any(_NUMBER_WITH_UNIT_RE.search(word) for word in prefix.split()):
        numeric_index = next((index for index, word in enumerate(words)
                              if _NUMBER_WITH_UNIT_RE.search(word)), -1)
        if numeric_index >= 0:
            numeric_words = [words[numeric_index]]
            if numeric_index > 0 and words[numeric_index - 1] in {"최대", "약", "월", "연", "총"}:
                numeric_words.insert(0, words[numeric_index - 1])
            numeric_phrase = " ".join(numeric_words)
            topic = words[0] if words else ""
            numeric_prefix = " ".join(dict.fromkeys((topic, numeric_phrase))).strip()
            if len(numeric_prefix + suffix) <= limit:
                prefix = numeric_prefix
    if not prefix:
        # 주제 한 어절이 길면 먼저 온전한 숫자+단위 토큰을 찾고, 없으면 어절 경계에서만 자른다.
        words = source.split()
        numeric_word = next((w for w in words if _NUMBER_WITH_UNIT_RE.search(w)), "")
        if numeric_word and len(numeric_word) < limit:
            prefix = numeric_word
        elif words:
            prefix = words[0][:limit - len(suffix)]
    if not prefix:
        return ""
    # 숫자 토큰을 붙인 채 둘 수 없는 경우 해당 토큰은 생략하고 앞의 주제를 보존한다.
    if _NUMBER_WITH_UNIT_RE.search(prefix.split()[-1]) and len(prefix + suffix) > limit:
        prefix = " ".join(prefix.split()[:-1]).strip()

    # A useful model message such as "내가 대상인지 확인" still needs its topic.
    # Keep that phrase as the hook only when the title topic and any amount also fit.
    anchors = _title_anchor_words(title_seed)
    if (source_hooked and candidate_text and anchors
            and not any(anchor in candidate_text for anchor in anchors)):
        candidate_suffix = " " + candidate_text
        available = limit - len(candidate_suffix)
        candidate_prefix = _whole_word_prefix(title_seed, available)
        title_numbers = [word for word in title_seed.split()
                         if _NUMBER_WITH_UNIT_RE.search(word)]
        if title_numbers and not all(number in candidate_prefix for number in title_numbers):
            index = next((i for i, word in enumerate(title_seed.split())
                          if _NUMBER_WITH_UNIT_RE.search(word)), -1)
            title_words = title_seed.split()
            if index >= 0:
                numeric_words = [title_words[index]]
                if index > 0 and title_words[index - 1] in {"최대", "약", "월", "연", "총"}:
                    numeric_words.insert(0, title_words[index - 1])
                candidate_prefix = " ".join((anchors[0], *numeric_words))
        if candidate_prefix and len(candidate_prefix + candidate_suffix) <= limit:
            prefix, suffix = candidate_prefix, candidate_suffix
    result = (prefix + suffix).strip()
    return result if len(result) <= limit else _whole_word_prefix(prefix, limit)


def message_from_title(title: Any, *, limit: int = LIMIT_MESSAGE) -> str:
    """제목에서 주제와 독자의 판단 질문을 담은 짧은 문구를 만든다."""
    return resolve_thumbnail_message(None, title=title, limit=limit)


def thumbnail_message(brief: Any, *, title: Any = "") -> str:
    """브리프와 제목을 공통 로컬 resolver로 보내 썸네일 문구를 정한다."""
    if isinstance(brief, dict):
        msg = sanitize(brief.get("thumbnail_message"), limit=200)
        question = sanitize(brief.get("reader_question"), limit=LIMIT_QUESTION)
        if msg:
            return resolve_thumbnail_message(msg, title=title, question=question)
    return message_from_title(title)


# ─────────────────────────────────────────────────────────────────────────────
# 정규화 · 로컬 폴백
# ─────────────────────────────────────────────────────────────────────────────
def _structure(raw: Any) -> Dict[str, Any]:
    """구조 선언을 정규화한다 — **선언하지 않은 항목은 키를 만들지 않는다.**

    키가 없으면 품질 게이트가 예전 전역 규칙을 그대로 쓴다(완화는 명시적 선언에만).
    """
    out: Dict[str, Any] = {}
    if not isinstance(raw, dict):
        return out
    if "table" in raw:
        value = raw.get("table")
        if isinstance(value, bool):
            out["table"] = value
        elif isinstance(value, (int, float)):
            out["table"] = bool(value)
        elif isinstance(value, str) and value.strip().lower() in ("true", "false", "yes", "no"):
            out["table"] = value.strip().lower() in ("true", "yes")
    for key in ("faq_count", "faq"):
        if key in raw:
            try:
                count = int(raw.get(key))
            except (TypeError, ValueError):
                continue
            out["faq_count"] = max(0, min(FAQ_COUNT_MAX, count))
            break
    return out


def normalize(raw: Any, *, title: Any = "", category: Any = "",
              source: str = "llm") -> Optional[Dict[str, Any]]:
    """집필 응답의 `editorial` 조각을 정규화한다. 쓸 만한 재료가 없으면 None.

    받는 형태(집필 프롬프트가 요구하는 최소 스키마)::

        {"reader_question": "...", "answer_summary": "...", "reader_action": "...",
         "thumbnail_message": "...", "structure": {"table": true, "faq_count": 5}}
    """
    if not isinstance(raw, dict):
        return None

    question = sanitize(raw.get("reader_question") or raw.get("question"), limit=LIMIT_QUESTION)
    answer = sanitize(raw.get("answer_summary") or raw.get("answer"), limit=LIMIT_ANSWER)
    action = sanitize(raw.get("reader_action") or raw.get("action"), limit=LIMIT_ACTION)
    # Preserve the complete candidate until the shared resolver chooses its 24-char hook.
    message = sanitize(raw.get("thumbnail_message") or raw.get("thumbnail_label"), limit=200)
    focus = sanitize(raw.get("focus"), limit=LIMIT_FOCUS)
    structure = _structure(raw.get("structure"))
    topic_context = raw.get("topic_context")

    if not any((question, answer, action, message, focus)) and not structure and not isinstance(topic_context, dict):
        return None

    message = resolve_thumbnail_message(message or None, title=title, question=question)

    normalized = {
        "version": BRIEF_VERSION,
        "source": source,
        "created_at": _now_iso(),
        "category": sanitize(category, limit=30),
        "reader_question": question,
        "answer_summary": answer,
        "reader_action": action,
        "thumbnail_message": message,
        "focus": focus,
        "structure": structure,
    }
    if isinstance(topic_context, dict):
        fields = ("id", "title", "kind", "source_name", "url", "location", "provider_time", "event_start",
                  "event_end", "effective_date", "collected_at", "reader_question", "why_now",
                  "editorial_title")
        context = {key: sanitize(topic_context.get(key), limit=2048 if key == "url" else 300)
                   for key in fields if isinstance(topic_context.get(key), str)}
        sources = topic_context.get("sources")
        if isinstance(sources, list):
            context["sources"] = [
                {key: sanitize(item.get(key), limit=2048 if key == "url" else 120)
                 for key in ("name", "url", "provider_time") if isinstance(item.get(key), str)}
                for item in sources[:6] if isinstance(item, dict)]
        context["verified"] = False
        normalized["topic_context"] = context
    return normalized


def local_brief(*, title: Any = "", category: Any = "") -> Dict[str, Any]:
    """LLM 0회 로컬 폴백 — **제목에서 유도할 수 있는 것만** 채운다.

    독자 질문·답변 요약·다음 행동은 **비워 둔다**: 그것을 우리가 지어내면 근거 없는
    문장이 본문 기획에 섞인다(요건 3 — 출처 없는 사실·허구 질문 금지). 구조 선언도
    비워 둬서 품질 게이트가 예전 전역 규칙을 그대로 쓰게 한다.
    """
    return {
        "version": BRIEF_VERSION,
        "source": "local",
        "created_at": _now_iso(),
        "category": sanitize(category, limit=30),
        "reader_question": "",
        "answer_summary": "",
        "reader_action": "",
        "thumbnail_message": resolve_thumbnail_message(None, title=title),
        "focus": "",
        "structure": {},
    }


def ensure_brief(post_data: Any, *, title: Any = None, category: Any = "") -> Dict[str, Any]:
    """이 글에 쓸 브리프를 확보한다 — 저장된 것 우선, 없으면 로컬 폴백(LLM 0회)."""
    src = post_data if isinstance(post_data, dict) else {}
    title = src.get("title") if title is None else title
    stored = src.get("editorial_brief")
    if isinstance(stored, dict) and stored:
        normalized = normalize(stored, title=title, category=category or src.get("category") or "",
                               source=str(stored.get("source") or "stored"))
        if normalized is not None:
            # 저장돼 있던 생성 시각을 보존한다(관측 필드 — 언제 기획된 글인지).
            normalized["created_at"] = sanitize(stored.get("created_at"), limit=40) or normalized["created_at"]
            return normalized
    return local_brief(title=title, category=category or src.get("category") or "")


# ─────────────────────────────────────────────────────────────────────────────
# 품질 게이트 정합 — 구조 선언을 QA 규칙 완화 입력으로 번역
# ─────────────────────────────────────────────────────────────────────────────
def structure_rules(brief: Any) -> Dict[str, Any]:
    """브리프의 구조 선언 → 품질 규칙 완화 입력.

    **선언된 항목만** 돌려준다. 표를 쓰지 않겠다고 선언한 글에만 `require_table=False`,
    FAQ 0개를 선언한 글에만 `require_faq=False` 가 실린다. 요구를 **올리는 방향으로는
    절대 쓰지 않는다**(호출자 `quality_validator._apply_editorial_rules` 가 강제).
    """
    out: Dict[str, Any] = {}
    if not isinstance(brief, dict):
        return out
    structure = brief.get("structure")
    if not isinstance(structure, dict) or not structure:
        return out
    if structure.get("table") is False:
        out["require_table"] = False
    if "faq_count" in structure:
        try:
            if int(structure["faq_count"]) <= 0:
                out["require_faq"] = False
        except (TypeError, ValueError):
            pass
    return out


# ─────────────────────────────────────────────────────────────────────────────
# 집필 프롬프트 블록 — 브리프 + 이미지 소재를 한 JSON 으로 함께 요구
# ─────────────────────────────────────────────────────────────────────────────
def prompt_block(body_count: int) -> str:
    """집필 응답에 `editorial` + `image_plan` 두 키를 함께 요구하는 블록.

    ★비용: 이 글의 본문을 가장 잘 아는 호출이 지금 이 집필 호출이다 — 기획을 위해
      모델을 다시 부르지 않는다(추가 LLM 0회).
    ★안전: 여기서 받는 것은 **소재·초점**이지 프롬프트 문장이 아니다. 최종 이미지
      프롬프트는 `core.image_plan` 이 우리 문장으로 만든다(인젝션·무문자 규칙 누락 차단).
    """
    body_count = max(0, int(body_count or 0))
    lines = [
        "[편집 브리프 — 위 JSON 에 \"editorial\" 키를 하나 더 넣으세요]",
        "- 이 글이 어떤 질문에 답하는지 먼저 정하고, 본문·썸네일이 **같은 질문**을 겨냥하게 합니다.",
        "- \"reader_question\": 이 글을 검색한 사람이 실제로 알고 싶은 것 한 문장.",
        "  · ⚠️ 이건 글의 초점을 잡기 위한 메모입니다. 본문에 "
        "\"한 독자분이 이렇게 물어보셨어요\" 같은 **허구의 사연으로 쓰지 마세요.**",
        "- \"answer_summary\": 그 질문에 대한 답을 한 문장으로(본문에 실제로 쓴 사실만).",
        "- \"reader_action\": 글을 읽은 사람이 바로 할 수 있는 다음 행동 한 문장.",
        f"- \"thumbnail_message\": 썸네일에 크게 얹을 **짧은 한글 메시지**({LIMIT_MESSAGE}자 이내). "
        "핵심 혜택이나 독자가 겪는 문제와, 본문에서 답할 독자의 질문 또는 판단을 함께 담으세요. "
        "키워드만 나열하지 말고 자연스러운 질문·확인 기준으로 표현하며, 본문이 실제로 "
        "답하는 내용만 압축하세요. 본문 근거 없는 액수·마감·혜택 확약이나 숫자·기관명을 "
        "지어내지 말고, 불안·공포를 부추기는 낚시 문구는 쓰지 마세요.",
        "- \"structure\": 이 주제에 **실제로 필요한** 구성만 선언하세요 — "
        "{\"table\": true/false, \"faq_count\": 0~5}.",
        "  · 비교할 값이 있을 때만 표를 쓰고, 정말 물어볼 게 있을 때만 FAQ 를 넣습니다. "
        "분량을 채우려고 표·FAQ 를 억지로 만들지 마세요.",
        "  · 여기서 선언한 대로 본문을 쓰세요(선언과 본문이 어긋나면 검사에서 걸립니다).",
    ]
    if body_count:
        lines += [
            "",
            "[이미지 소재 — 위 JSON 에 \"image_plan\" 키를 하나 더 넣으세요]",
            "- 이미지는 나중에 자동 생성됩니다. 여기서는 **소재만** 적습니다.",
            f"- \"body_images\": 본문 이미지 {body_count}개. 각 항목은 그 문단이 "
            "무엇을 다루는지에 맞춰 아래 네 칸을 채웁니다:",
            "  {\"subject\": \"누가/무엇이\", \"setting\": \"어디서\", "
            "\"action\": \"무엇을 하는지\", \"purpose\": \"그 문단에서 이 이미지가 하는 역할\"}",
            "  · 소제목의 단어를 그대로 옮기지 말고, **그 문단이 설명하는 상황**을 적으세요.",
            "  · 본문 이미지에는 글자·숫자·표·로고·서류·화면 UI 가 들어가지 않습니다 — "
            "문서·광고·화면을 그대로 보여주는 소재는 적지 마세요.",
            "  · 설명해야 하는 수치·절차는 이미지가 아니라 본문 문장으로 쓰고, "
            "비교에 도움이 될 때만 표를 사용합니다.",
            "  · 좋은 예는 이 글의 주제와 실제 문단에 맞는 대상·장소·행동·시각적 역할을 "
            "구체적으로 적되, 정책 신청 장면이나 사용 경험을 임의로 가정하지 마세요.",
            "- 형식: \"image_plan\": {\"body_images\": [ {...}, {...} ]}",
        ]
    return "\n".join(lines) + "\n"


def summary(brief: Any) -> Dict[str, Any]:
    """로그·메타에 남길 얇은 요약(본문 문장은 싣지 않는다)."""
    if not isinstance(brief, dict):
        return {}
    structure = brief.get("structure") if isinstance(brief.get("structure"), dict) else {}
    return {
        "version": brief.get("version"),
        "source": brief.get("source"),
        "has_question": bool(brief.get("reader_question")),
        "has_answer": bool(brief.get("answer_summary")),
        "has_action": bool(brief.get("reader_action")),
        "message_chars": len(str(brief.get("thumbnail_message") or "")),
        "structure": dict(structure),
    }


__all__ = [
    "BRIEF_VERSION", "LIMIT_QUESTION", "LIMIT_ANSWER", "LIMIT_ACTION", "LIMIT_MESSAGE",
    "FAQ_COUNT_MAX", "sanitize", "message_from_title", "thumbnail_message",
    "normalize", "local_brief", "ensure_brief", "structure_rules", "prompt_block", "summary",
]
