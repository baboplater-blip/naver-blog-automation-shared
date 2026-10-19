"""공식 출처 검증 기록 — 사람 검토를 **대체**하는 두 번째 최신성 근거(2026-09-21).

배경
----
`autonomy_runner.target_preview` 는 지정 1편 경로에서 `content_freshness_unreviewed`
로 실제 예약을 막는다. 오래전에 쓴 제도 안내가 옛 금액·기한 그대로 나가는 것을
막는 자물쇠다. 지금까지 그 자물쇠를 여는 방법은 **사람이 봤다고 선언하는 것**
(`--freshness-reviewed`) 하나뿐이었다.

사람이 볼 수 없는 운영 상황에서는, 선언을 거짓으로 세우는 대신 **글의 주장을 공식
원문과 대조한 기록**으로 대체한다. 이 모듈이 그 기록의 형식과 **판정**을 갖는다.

이 기록이 사람 검토와 같은 무게를 가지려면 네 가지가 필요하다.

1. **글에 묶여야 한다.** 제목·본문·실제 사용 이미지의 바이트를 함께 해싱한다.
   본문을 한 글자라도 고치면 기록은 그 순간 무효다(`content_changed`).
2. **시효가 있어야 한다.** 제도 금액은 해마다 바뀐다. `expires_at` 이 지나면
   무효다(`stale`).
3. **실제로 가져왔어야 한다.** URL 문자열만 적힌 출처는 거부한다 — 조회 시각과
   **받아온 문서의 sha256**, 그리고 인용한 문장이 함께 있어야 한다(`url_only`).
4. **주장마다 근거가 붙어야 한다.** 검증/수정/삭제 중 하나로 분류되고, 남긴
   주장에는 출처 URL과 인용문이 있어야 한다(`unsupported_claim`).

판정은 **fail-closed** 다 — 위 중 하나라도 불확실하면 열지 않는다. 그리고 이 기록은
**슬러그 하나에만** 적용된다. 전역 불리언으로 게이트를 여는 경로는 만들지 않는다.

사람 검토 경로(`freshness_reviewed=True`)는 그대로 살아 있다 — 이 모듈은 그것을
**대체하지 않고 병렬로 추가**한다.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

#: 기록 파일 이름 — 보관본과 같은 `runs/{slug}/` 아래에 둔다.
#: `draft_store.save_draft` 는 post_data.json·_status.json 만 쓰므로 이 파일은 살아남는다.
RECORD_FILE = "source_verification.json"

#: 기록 스키마 버전. 올라가면 옛 기록은 **통과시키지 않는다**(fail-closed).
RECORD_VERSION = 3

#: 공식 출처로 인정하는 호스트 접미사. 한국 정부·공공기관 도메인만.
_OFFICIAL_SUFFIXES = ("gov.kr", "go.kr")

#: 스테이징·미리보기 도메인 라벨 — 최종 출처로 인정하지 않는다.
#: (`renewal.idolbom.go.kr`, `userstg.…` 처럼 원문과 다를 수 있는 사본)
_STAGING_LABELS = frozenset({
    "renewal", "userstg", "stg", "staging", "dev", "test", "tst", "beta", "sandbox",
})

#: 주장 분류 — 남긴 주장(verified/corrected)에는 근거가 필수다.
_CLAIM_STATUS = frozenset({"verified", "corrected", "removed"})
_CLAIM_NEEDS_EVIDENCE = frozenset({"verified", "corrected"})


# ─────────────────────────────────────────────────────
# 경로
# ─────────────────────────────────────────────────────

def _runs_dir() -> Path:
    """보관본 루트 — `draft_store` 와 **같은** 경로를 쓴다(프로필 격리 포함)."""
    from core import draft_store as _ds
    return _ds._RUNS_DIR  # noqa: SLF001 — 단일 진실 원본을 복제하지 않기 위함


def record_path(slug: str) -> Path:
    """그 슬러그의 검증 기록 경로."""
    return _runs_dir() / str(slug or "") / RECORD_FILE


# ─────────────────────────────────────────────────────
# 지문 — 글(제목+본문)과 실제 이미지 바이트를 함께 묶는다
# ─────────────────────────────────────────────────────

def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def content_fingerprint(post: dict) -> dict:
    """제목·본문·사용 이미지를 한 덩어리로 묶은 지문.

    이미지는 **경로 문자열이 아니라 내용(sha256)** 으로 묶는다 — 같은 파일명으로
    다른 그림이 덮이면 검증 기록이 무효가 되어야 하기 때문이다.

    Returns:
        {"hash": str, "title_len": int, "content_len": int,
         "images": [{"name": str, "sha256": str}], "missing_images": [str]}
    """
    post = post or {}
    title = str(post.get("title") or "")
    content = str(post.get("content") or "")

    images, missing = [], []
    for raw in list(post.get("image_paths") or []):
        p = Path(str(raw))
        try:
            if p.is_file():
                images.append({"name": p.name, "sha256": _sha256_file(p)})
            else:
                missing.append(p.name or str(raw))
        except OSError as exc:  # noqa: PERF203 — 파일 하나의 실패가 판정을 죽이지 않는다
            log.debug(f"[source_verification] 이미지 해시 실패({type(exc).__name__}): {p.name}")
            missing.append(p.name or str(raw))

    # 순서에 흔들리지 않도록 이름으로 정렬해 정규화한다.
    images.sort(key=lambda d: (d["name"], d["sha256"]))
    canonical = json.dumps(
        {"title": title, "content": content, "images": images, "missing": sorted(missing)},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "hash": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "title_len": len(title),
        "content_len": len(content),
        "images": images,
        "missing_images": sorted(missing),
    }


# ─────────────────────────────────────────────────────
# 출처 판정
# ─────────────────────────────────────────────────────

def is_official_url(url: str) -> bool:
    """공식 출처 URL 인가 — 정부 도메인이면서 스테이징 사본이 아닌가."""
    try:
        parts = urlsplit(str(url or ""))
    except ValueError:
        return False
    if parts.scheme not in ("http", "https"):
        return False
    host = (parts.hostname or "").lower()
    if not host or not any(host == suffix or host.endswith("." + suffix)
                            for suffix in _OFFICIAL_SUFFIXES):
        return False
    labels = host.split(".")
    return not any(lbl in _STAGING_LABELS for lbl in labels)


def _plain_text(value: str) -> str:
    """Normalize markup and spacing for conservative literal evidence matching."""
    from html import unescape
    text = re.sub(r"<[^>]+>", " ", str(value or ""))
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip().casefold()


def _fact_values(value: str) -> list:
    """Extract numeric/date-like values; this is a limited coverage signal, not NLP."""
    pattern = r"(?<![\w])\d[\d,.]*(?:\s*(?:원|만원|%|퍼센트|일|개월|년|시|분|명|회))?"
    return [re.sub(r"\s+", "", item).casefold()
            for item in re.findall(pattern, _plain_text(value))]


def _paragraph_spans(text: str) -> list:
    """Return complete non-empty paragraph offsets in operator-captured text."""
    spans = []
    cursor = 0
    for separator in re.finditer(r"\r?\n[ \t]*\r?\n+", str(text or "")):
        start, end = cursor, separator.start()
        while start < end and text[start].isspace():
            start += 1
        while end > start and text[end - 1].isspace():
            end -= 1
        if start < end:
            spans.append((start, end))
        cursor = separator.end()
    end = len(text or "")
    start = cursor
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    if start < end:
        spans.append((start, end))
    return spans


def _is_complete_context_unit(original: str, excerpt: str,
                              start: int, end: int) -> bool:
    """Require an excerpt to occupy a complete captured paragraph boundary."""
    return any(start == unit_start and end == unit_end
               and _plain_text(original[unit_start:unit_end]) == _plain_text(excerpt)
               for unit_start, unit_end in _paragraph_spans(original))


def capture_source(*, url: str, original_text: str, fetched_at: str,
                   excerpts: list) -> dict:
    """Build a source record from text the operator actually captured.

    This helper performs no fetching. Every excerpt is located in the supplied
    original text and its character offsets are retained. Verification requires each
    excerpt to equal one complete paragraph in that capture. The operator must supply
    a context-bounded original; this helper cannot prove completeness against a live page.
    """
    original = str(original_text or "")
    if not original.strip():
        raise ValueError("원문 텍스트가 비어 있습니다")
    if not _parse_dt(fetched_at):
        raise ValueError("fetched_at 형식이 올바르지 않습니다")
    captured = []
    for excerpt in excerpts or []:
        quote = str(excerpt or "").strip()
        position = original.find(quote) if quote else -1
        if position < 0:
            raise ValueError("발췌문이 제공된 원문에 없습니다")
        captured.append({"text": quote, "start": position,
                         "end": position + len(quote)})
    if not captured:
        raise ValueError("원문에서 발췌문을 하나 이상 지정해야 합니다")
    digest = hashlib.sha256(original.encode("utf-8")).hexdigest()
    return {"url": str(url or "").strip(), "fetched_at": str(fetched_at),
            "content_sha256": digest, "original_text": original,
            "excerpts": captured}


def build_record(*, slug: str, post: dict, sources: list, claims: list,
                 verified_at: str, expires_at: str,
                 claims_complete: bool = False) -> dict:
    """Create a fingerprint-bound record from operator-supplied captures.

    Sparse declarations are explicitly marked as partial; this function does not
    infer that all prose claims have been reviewed.
    """
    if not isinstance(claims_complete, bool):
        raise TypeError("claims_complete must be bool")
    captured = []
    for source in sources or []:
        if not isinstance(source, dict):
            raise ValueError("출처는 객체여야 합니다")
        excerpt_values = [entry.get("text", "") if isinstance(entry, dict) else entry
                          for entry in (source.get("excerpts") or [])]
        captured.append(capture_source(
            url=source.get("url", ""), original_text=source.get("original_text", ""),
            fetched_at=source.get("fetched_at", ""), excerpts=excerpt_values))
    return {"version": RECORD_VERSION, "slug": str(slug or ""),
            "verified_at": str(verified_at or ""), "expires_at": str(expires_at or ""),
            "content_fingerprint": content_fingerprint(post), "sources": captured,
            "claims": list(claims or []),
            "coverage": {"scope": "operator_attested_claim_set_and_numeric_values"
                         if claims_complete else "declared_claims_and_numeric_values",
                         "claims_complete_attested": claims_complete,
                         "complete_article_semantics": False,
                         "limitation": "작성자가 주장 목록 완전성을 확인해야 하며 나머지 문장 의미는 자동 검증하지 않음"}}


def _check_sources(sources, *, require_official: bool) -> list:
    """출처 목록의 결함을 문자열 사유로 돌려준다(빈 리스트 = 이상 없음)."""
    problems = []
    if not isinstance(sources, list) or not sources:
        return ["source_missing"]
    for idx, src in enumerate(sources):
        tag = f"sources[{idx}]"
        if not isinstance(src, dict):
            problems.append(f"{tag}:not_an_object")
            continue
        url = str(src.get("url") or "").strip()
        if not url:
            problems.append(f"{tag}:url_missing")
            continue
        if require_official and not is_official_url(url):
            problems.append(f"{tag}:nonofficial_source:{url}")
        elif not _is_safe_http_url(url):
            problems.append(f"{tag}:invalid_url:{url}")
        # URL 문자열만 있는 출처는 거부한다 — 실제로 가져왔다는 증거가 필요하다.
        if not str(src.get("fetched_at") or "").strip():
            problems.append(f"{tag}:url_only:fetched_at_missing")
        original = str(src.get("original_text") or "")
        digest = hashlib.sha256(original.encode("utf-8")).hexdigest() if original else ""
        if not digest or str(src.get("content_sha256") or "") != digest:
            problems.append(f"{tag}:original_hash_mismatch")
        excerpts = src.get("excerpts")
        if not isinstance(excerpts, list) or not excerpts:
            problems.append(f"{tag}:url_only:excerpts_missing")
        else:
            for excerpt_index, excerpt in enumerate(excerpts):
                if not isinstance(excerpt, dict):
                    problems.append(f"{tag}:excerpt[{excerpt_index}]:no_original_position")
                    continue
                quote = str(excerpt.get("text") or "")
                start, end = excerpt.get("start"), excerpt.get("end")
                if (not isinstance(start, int) or not isinstance(end, int)
                        or start < 0 or end <= start or original[start:end] != quote):
                    problems.append(f"{tag}:excerpt[{excerpt_index}]:original_mismatch")
                elif not _is_complete_context_unit(original, quote, start, end):
                    problems.append(
                        f"{tag}:excerpt[{excerpt_index}]:excerpt_context_incomplete")
    return problems


def _is_safe_http_url(url: str) -> bool:
    try:
        parts = urlsplit(str(url or ""))
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return (parts.scheme == "https" and bool(host) and not parts.username
            and not parts.password and host not in {"localhost", "127.0.0.1", "::1"})


def _check_claims(claims, sources_by_url: dict, post: dict) -> tuple:
    """주장 목록의 결함을 사유로 돌려준다."""
    problems = []
    outcomes = []
    if not isinstance(claims, list) or not claims:
        return ["claims_missing"], outcomes
    for idx, claim in enumerate(claims):
        tag = f"claims[{idx}]"
        if not isinstance(claim, dict):
            problems.append(f"{tag}:not_an_object")
            outcomes.append({"index": idx, "status": "unknown", "reason": "not_an_object"})
            continue
        if not str(claim.get("claim") or "").strip():
            problems.append(f"{tag}:claim_text_missing")
        status = str(claim.get("status") or "").strip()
        if status in {"unknown", "contradicted"}:
            problems.append(f"{tag}:{status}")
            outcomes.append({"index": idx, "status": status,
                             "reason": str(claim.get("reason") or status)})
            continue
        if status not in _CLAIM_STATUS:
            problems.append(f"{tag}:bad_status:{status or 'empty'}")
            outcomes.append({"index": idx, "status": "unknown", "reason": "bad_status"})
            continue
        if status not in _CLAIM_NEEDS_EVIDENCE:
            if _plain_text(claim.get("claim", "")) in _plain_text(post.get("content", "")):
                problems.append(f"{tag}:removed_claim_still_in_article")
                outcomes.append({"index": idx, "status": "contradicted",
                                 "reason": "claim_still_in_current_article"})
            else:
                outcomes.append({"index": idx, "status": "removed", "reason": "removed"})
            continue  # removed — 본문에서 지운 주장이라 근거가 필요 없다
        url = str(claim.get("source_url") or "").strip()
        source = sources_by_url.get(url)
        if not url:
            problems.append(f"{tag}:unsupported_claim:source_url_missing")
        elif source is None:
            problems.append(f"{tag}:unsupported_claim:source_not_listed")
        evidence = str(claim.get("evidence") or "").strip()
        if not evidence:
            problems.append(f"{tag}:unsupported_claim:evidence_missing")
        claim_text = str(claim.get("claim") or "").strip()
        source_text = str((source or {}).get("original_text") or "")
        article_text = f"{post.get('title', '')} {post.get('content', '')}"
        if (not claim_text or _plain_text(claim_text) not in _plain_text(article_text)):
            problems.append(f"{tag}:claim_not_in_current_article")
        source_excerpts = ((source or {}).get("excerpts") or [])
        evidence_in_excerpt = any(
            isinstance(excerpt, dict)
            and _plain_text(evidence) == _plain_text(excerpt.get("text", ""))
            for excerpt in source_excerpts)
        if source is not None and (not evidence or not evidence_in_excerpt):
            problems.append(f"{tag}:evidence_not_in_original")
        if source is not None and evidence and claim_text:
            if _plain_text(claim_text) not in _plain_text(source_text):
                problems.append(f"{tag}:claim_not_in_original")
            if _plain_text(claim_text) != _plain_text(evidence):
                problems.append(f"{tag}:claim_evidence_context_mismatch")
            if not set(_fact_values(claim_text)).issubset(set(_fact_values(evidence))):
                problems.append(f"{tag}:claim_values_not_supported")
            claim_negated = bool(re.search(r"아니|않|없|불가|제외|못하", _plain_text(claim_text)))
            evidence_negated = bool(re.search(r"아니|않|없|불가|제외|못하", _plain_text(evidence)))
            if claim_negated != evidence_negated:
                problems.append(f"{tag}:negation_mismatch")
        outcomes.append({"index": idx, "status": status,
                         "reason": "matched" if not any(p.startswith(tag + ":") for p in problems)
                         else "binding_failed"})
    return problems, outcomes


# ─────────────────────────────────────────────────────
# 읽기·쓰기
# ─────────────────────────────────────────────────────

def load(slug: str) -> Optional[dict]:
    """검증 기록을 읽는다. 없거나 깨졌으면 None(fail-closed 로 이어진다)."""
    path = record_path(slug)
    try:
        if not path.is_file():
            return None
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError) as exc:
        log.warning(f"[source_verification] 기록을 읽지 못했습니다({type(exc).__name__})")
        return None


def save(slug: str, record: dict) -> Path:
    """검증 기록을 원자적으로 쓴다."""
    path = record_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(record, fh, ensure_ascii=False, indent=2)
    tmp.replace(path)
    return path


def save_capture(*, slug: str, post: dict, sources: list, claims: list,
                 verified_at: str, expires_at: str,
                 claims_complete: bool = False) -> Path:
    """Operator entry point to save captured originals and claim bindings."""
    record = build_record(slug=slug, post=post, sources=sources, claims=claims,
                          verified_at=verified_at, expires_at=expires_at,
                          claims_complete=claims_complete)
    return save(slug, record)


# ─────────────────────────────────────────────────────
# 판정
# ─────────────────────────────────────────────────────

def _parse_dt(raw) -> Optional[datetime]:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt
    # 시각대가 붙어 있으면 **로컬 시각으로 환산한 뒤** naive 로 맞춘다.
    # (tzinfo 를 그냥 떼면 `+00:00` 로 적힌 기록이 9시간 어긋난다.)
    return dt.astimezone().replace(tzinfo=None)


def verify(slug: str, post: dict, *, now: Optional[datetime] = None) -> dict:
    """이 글의 공식 출처 검증 기록이 **지금** 유효한가.

    Returns:
        {"ok": bool, "reason": str, "detail": {...}}
        `ok=True` 일 때만 호출자가 최신성 게이트를 열어도 된다.
    """
    out = {"ok": False, "reason": "", "detail": {}, "slug": str(slug or "")}
    record = load(slug)
    if record is None:
        out["reason"] = "no_record"
        out["detail"]["fix"] = (
            f"공식 출처 검증 기록({RECORD_FILE})이 없습니다 — "
            "사람이 검토했다면 --freshness-reviewed 를 쓰세요.")
        return out

    if record.get("version") != RECORD_VERSION:
        out["reason"] = "unsupported_version"
        out["detail"]["found"] = record.get("version")
        return out

    if str(record.get("slug") or "") != str(slug or ""):
        out["reason"] = "record_mismatch"
        out["detail"]["record_slug"] = record.get("slug")
        return out

    # ① 글에 묶였는가 — 제목·본문·이미지가 검증 당시 그대로인가.
    current = content_fingerprint(post)
    stored = record.get("content_fingerprint") or {}
    if str(stored.get("hash") or "") != current["hash"]:
        out["reason"] = "content_changed"
        out["detail"] = {
            "stored_hash": stored.get("hash"), "current_hash": current["hash"],
            "stored_lens": [stored.get("title_len"), stored.get("content_len")],
            "current_lens": [current["title_len"], current["content_len"]],
            "fix": "본문·제목·이미지가 검증 이후 바뀌었습니다 — 다시 검증해 기록을 갱신하세요.",
        }
        return out

    # ② 시효 — 제도 금액은 해마다 바뀐다.
    at = _parse_dt(record.get("verified_at"))
    until = _parse_dt(record.get("expires_at"))
    if at is None or until is None:
        out["reason"] = "stale"
        out["detail"]["fix"] = "verified_at·expires_at 이 없거나 형식이 잘못됐습니다."
        return out
    ref = now or datetime.now()
    if ref > until:
        out["reason"] = "stale"
        out["detail"] = {"verified_at": record.get("verified_at"),
                         "expires_at": record.get("expires_at"),
                         "fix": "검증 유효기간이 지났습니다 — 공식 원문을 다시 대조하세요."}
        return out

    # ③ 실제 조회 증거 — URL, 캡처한 원문, 실제 해시, 발췌 오프셋이 모두 묶여야 한다.
    sources = record.get("sources")
    try:
        from core.topic_policy import is_government_policy
        require_official = is_government_policy(post.get("title", ""),
                                                post.get("category", ""))
    except (ImportError, AttributeError, TypeError, ValueError):
        require_official = False
    problems = _check_sources(sources, require_official=require_official)
    verified_at_for_sources = _parse_dt(record.get("verified_at"))
    for index, source in enumerate(sources if isinstance(sources, list) else []):
        if not isinstance(source, dict):
            continue
        fetched_at = _parse_dt(source.get("fetched_at"))
        if fetched_at is None or (verified_at_for_sources and fetched_at > verified_at_for_sources):
            problems.append(f"sources[{index}]:capture_time_invalid")
    if problems:
        if problems == ["source_missing"]:
            out["reason"] = "source_missing"
        elif any("nonofficial_source" in p for p in problems):
            out["reason"] = "nonofficial_source"
        elif any("excerpt_context_incomplete" in p for p in problems):
            out["reason"] = "context_incomplete"
        elif any("url_only" in p or "original_" in p or "excerpt" in p for p in problems):
            out["reason"] = "url_only"
        else:
            out["reason"] = "source_unverified"
        out["detail"]["problems"] = problems
        return out

    # ④ 주장마다 근거 — 출처만 붙인 미검증 문장을 통과시키지 않는다.
    urls = {str(s.get("url") or "").strip() for s in sources}
    sources_by_url = {str(s.get("url") or "").strip(): s
                      for s in sources if isinstance(s, dict)}
    problems, claim_outcomes = _check_claims(record.get("claims"), sources_by_url, post)
    if problems:
        out["reason"] = ("claims_missing" if problems == ["claims_missing"]
                         else "unsupported_claim")
        out["detail"]["problems"] = problems
        out["detail"]["claim_outcomes"] = claim_outcomes
        return out

    claims = record["claims"]
    claimed_value_counts = Counter(
        value for claim in claims
        if isinstance(claim, dict) and claim.get("status") in _CLAIM_NEEDS_EVIDENCE
        for value in _fact_values(str(claim.get("claim") or "")))
    body_values = _fact_values(str(post.get("title") or "") + " "
                               + str(post.get("content") or ""))
    body_value_counts = Counter(body_values)
    uncovered_values = [value for value, count in body_value_counts.items()
                        for _ in range(max(0, count - claimed_value_counts[value]))]
    values_covered = len(body_values) - len(uncovered_values)
    stored_coverage = record.get("coverage")
    claims_complete_attested = (isinstance(stored_coverage, dict)
                                and stored_coverage.get("claims_complete_attested") is True)
    coverage = {
        "scope": ("operator_attested_claim_set_and_numeric_values"
                  if claims_complete_attested else "declared_claims_and_numeric_values"),
        "claims_complete_attested": claims_complete_attested,
        "complete_article_semantics": False,
        "numeric_value_candidates": len(body_values),
        "numeric_values_covered": values_covered,
        "uncovered_values": uncovered_values,
        "limitation": "완전성은 작성자 확인에 의존하며 의미·나머지 문장 사실을 자동 검증하지 않음",
    }
    if not claims_complete_attested or uncovered_values:
        coverage["freshness_eligible"] = False
        out["reason"] = "coverage_incomplete"
        out["detail"] = {
            "problems": (["claim_set_completeness_not_attested"]
                         if not claims_complete_attested else [])
                        + (["uncovered_numeric_or_date_values"] if uncovered_values else []),
            "claim_outcomes": claim_outcomes,
            "coverage": coverage,
            "fix": "현재 글의 모든 사실 주장을 원문에 연결하고 모든 숫자·날짜를 포함해 다시 확인하세요.",
        }
        return out

    coverage["freshness_eligible"] = True
    out["ok"] = True
    out["reason"] = "source_claims_bound_operator_attested"
    out["detail"] = {
        "verified_at": record.get("verified_at"),
        "expires_at": record.get("expires_at"),
        "sources": sorted(urls),
        "claims": {
            "total": len(claims),
            "verified": sum(1 for c in claims if c.get("status") == "verified"),
            "corrected": sum(1 for c in claims if c.get("status") == "corrected"),
            "removed": sum(1 for c in claims if c.get("status") == "removed"),
        },
        "claim_outcomes": claim_outcomes,
        "coverage": coverage,
        "content_hash": current["hash"],
    }
    return out
