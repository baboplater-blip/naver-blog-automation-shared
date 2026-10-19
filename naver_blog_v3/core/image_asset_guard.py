"""발행 이미지 자산 판정 — "이 파일을 블로그에 올려도 되는가"의 단일 진실 원본.

2026-09-23 사고 대응. 사용자가 실제 발행글에서 ①내용이 안 채워진 카드
("정보를 불러오는 중입니다… / 잠시 후 다시 확인해주세요.")와 ②글 주제와 무관한
공공기관 **홈페이지 캡처**(국민연금 대표홈/개인민원/기금운용 선택 화면)를 발견했다.
두 자산 모두 **파일은 실재하고 PNG 로 정상 디코드된다** — 그래서 기존 게이트
(`Path.is_file()` + 에디터 이미지 개수 대조)를 전부 통과했다.

이 모듈은 그 간극만 메운다. 판정 범위를 정직하게 적는다:

판정할 수 있는 것
    · ``missing``            — 경로가 비었거나 파일이 없다.
    · ``undecodable``        — 이미지로 열리지 않는다(깨진 파일).
    · ``placeholder``        — `image_generator` 가 내용 없이 찍어낸 자리표시 카드.
      **재렌더 바이트 대조**로 판정한다(파일명·크기 추측이 아니라, 같은 제목·카테고리로
      자리표시 카드를 다시 그려 sha256 이 일치하는지 본다 → 오탐 0, 미탐은 렌더러가
      바뀐 경우에 한함).
    · ``reference_capture``  — 참고용 공공사이트 캡처. 기록된 출처(provenance) 또는
      **캡처 전용 디렉터리(screenshots/) 소속**이라는 구조적 사실로 판정한다.

판정할 수 **없는** 것 (과대주장 금지)
    · 이미지가 글 내용과 의미상 맞는지. 여기서는 판단하지 않는다 — 이 모듈은
      "의미 검증기"가 아니다. AI 가 그린 그림의 내용 적합성은 사람 확인 몫이다.
    · 출처 기록이 없는 **옛 보관 자산**(``unverified_legacy``)이 정상인지 아닌지.
      **모른다는 것이 판정이다** — 자동으로 폐기·재생성하지 않고, 확정 게시도 하지
      않는다(보류).

2026-09-23 마감 보완 — **미확인은 통과가 아니다(fail-closed).**
처음 판에서는 ``unverified_legacy`` 를 "경고만 남기고 통과"시켰다. 그러면 이번 사고와
똑같은 부류(출처를 모르는 자산)가 그대로 발행된다. 사용자 요구는 "미확인 자산은
경고만 하고 통과시키지 말 것"이므로 이제 **모르는 것은 전부 막는다**:

    · 출처 기록 없음(``unverified_legacy``)            → 보류
    · 출처 기록 손상·읽기 불능(``unverifiable``)       → 보류
    · 검사 자체가 불가능(PIL 없음·해시 계산 실패)      → 보류
    · 기록된 내용과 현재 파일이 다름(``provenance_mismatch``) → 보류

출처 기록은 **경로가 아니라 내용(sha256)에 묶인다.** 경로만 믿으면 같은 이름으로
파일을 바꿔치기한 자산이 옛 기록을 타고 통과한다 — 기록 시점 해시와 현재 해시가
다르면 기록은 무효다.

기록을 남기는 쪽은 "정말로 그 자산을 만든/고른 경로"뿐이다.
    · 생성물(``KIND_GENERATED``) — 생산자가 **검증을 마친 직후** 그 파일에 대해.
      파일명·단순 존재로 승격하지 않는다.
    · 사용자 사진(``KIND_USER``) — 사용자가 **직접 고른 명시 선택 경로**에서만.
      post_data 의 플래그 같은 외부 메타데이터는 근거로 쓰지 않는다.
    · 참고 캡처(``KIND_REFERENCE``) — 발행 금지 표시.

**옛 자산을 일괄 승인하거나 자동 재생성하는 경로는 이 모듈에 없다**(의도).
그래서 출처 기록이 없는 보관 자산으로는 확정 게시가 되지 않는다 — 그 범위가
얼마나 되는지는 여기서 추정하지 않으며, 되살릴지는 사용자 결정이다.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from config.logger import get_logger
    log = get_logger(__name__)
except Exception:  # noqa: BLE001 — 로거 없이도 판정은 동작해야 한다
    import logging
    log = logging.getLogger(__name__)


# ── 출처(provenance) 종류 ────────────────────────────────────────────────
KIND_GENERATED = "generated"           # 이 글을 위해 만든 발행용 이미지
KIND_REFERENCE = "reference_capture"   # 참고용 외부 사이트 캡처 — 발행 금지
KIND_USER = "user_supplied"            # 사용자가 직접 고른 이미지 — 항상 발행 허용

# ── 판정 결과 ───────────────────────────────────────────────────────────
VERDICT_OK = "ok"
VERDICT_MISSING = "missing"
VERDICT_UNDECODABLE = "undecodable"
VERDICT_PLACEHOLDER = "placeholder"
VERDICT_REFERENCE = "reference_capture"
VERDICT_LEGACY = "unverified_legacy"
VERDICT_UNVERIFIABLE = "unverifiable"       # 검사 자체가 불가능(도구·기록 손상)
VERDICT_TAMPERED = "provenance_mismatch"    # 기록된 내용과 현재 파일이 다름

# 발행을 막는 판정 (fail-closed) — ``ok`` 가 아닌 모든 판정이 막는다.
# "미확인"은 통과가 아니다. 새 판정을 추가하면 자동으로 막히는 쪽에 들어간다.
BLOCKING_VERDICTS = frozenset({
    VERDICT_MISSING, VERDICT_UNDECODABLE, VERDICT_PLACEHOLDER, VERDICT_REFERENCE,
    VERDICT_LEGACY, VERDICT_UNVERIFIABLE, VERDICT_TAMPERED,
})

# 사람이 읽는 사유·조치 — GUI/로그가 그대로 보여 준다.
_VERDICT_TEXT = {
    VERDICT_MISSING: (
        "이미지 파일이 없습니다",
        "보관함에서 이 글의 이미지를 다시 채운 뒤 발행하세요."),
    VERDICT_UNDECODABLE: (
        "이미지 파일이 깨져 열리지 않습니다",
        "해당 이미지를 지우고 다시 채운 뒤 발행하세요."),
    VERDICT_PLACEHOLDER: (
        "내용이 채워지지 않은 자리표시 카드입니다"
        "('정보를 불러오는 중입니다…'가 찍힌 이미지)",
        "이미지를 다시 만든 뒤 발행하세요. 이 카드는 발행하지 않습니다."),
    VERDICT_REFERENCE: (
        "참고용으로 저장한 외부 사이트 캡처입니다(글 이미지가 아님)",
        "본문용 이미지를 따로 채운 뒤 발행하세요."),
    VERDICT_LEGACY: (
        "출처 기록이 없는 이미지입니다(정상 여부를 확인할 수 없음)",
        "보관함에서 이 글의 이미지를 다시 채운 뒤 발행하세요. "
        "직접 고른 사진이라면 사진 글 경로로 다시 선택해 주세요. "
        "파일은 지우지 않았습니다."),
    VERDICT_UNVERIFIABLE: (
        "이미지를 검사할 수 없습니다(검사 도구 또는 출처 기록 문제)",
        "프로그램을 다시 시작한 뒤에도 같으면 이미지를 다시 채워 주세요. "
        "확인되지 않은 이미지는 발행하지 않습니다."),
    VERDICT_TAMPERED: (
        "기록된 이미지와 현재 파일의 내용이 다릅니다(바뀌었거나 덮어써짐)",
        "이 이미지를 다시 채운 뒤 발행하세요."),
}

_MANIFEST_NAME = "image_provenance.json"
_lock = threading.Lock()
_cache: Dict[str, Dict[str, Any]] = {}
_cache_key: Optional[str] = None
_cache_status: str = "absent"

# 출처 기록의 상태 — 판정이 이 값을 그대로 쓴다.
PROV_OK = "ok"                # 기록이 있고 현재 파일 내용과 일치한다
PROV_NONE = "none"            # 기록이 없다
PROV_UNBOUND = "unbound"      # 기록은 있으나 내용(sha256)에 묶여 있지 않다 → 신뢰 불가
PROV_MISMATCH = "mismatch"    # 기록된 내용과 현재 파일이 다르다
PROV_CORRUPT = "corrupt"      # 기록 파일 자체가 깨졌다 → 아무 기록도 신뢰할 수 없다


# ─────────────────────────────────────────────────────────────────────────
# 출처 기록 저장소
# ─────────────────────────────────────────────────────────────────────────
def _manifest_path() -> Path:
    """출처 기록 파일 경로(프로필 격리). 실패 시 레거시 output/ 폴백."""
    try:
        from config import profile as _profile
        return Path(_profile.output_dir()) / _MANIFEST_NAME
    except Exception:  # noqa: BLE001
        base = Path(__file__).resolve().parent.parent / "output"
        return base / _MANIFEST_NAME


def _norm(path: Any) -> str:
    """경로 정규화 — 대소문자·구분자 차이로 기록을 놓치지 않게."""
    text = str(path or "").strip()
    if not text:
        return ""
    try:
        return os.path.normcase(os.path.abspath(text))
    except (OSError, ValueError, TypeError):
        return text


def _load() -> Tuple[Dict[str, Any], str]:
    """출처 기록 로드(경로 단위 캐시).

    Returns:
        (기록, 상태) — 상태는 ``ok``(읽었음, 파일이 없으면 빈 기록) 또는
        ``corrupt``(파일은 있는데 못 읽음/형식이 깨짐).

    ⚠️ 깨진 기록을 **빈 기록으로 뭉개지 않는다.** 예전에는 어떤 실패도 ``{}`` 로
    처리해서, 기록 파일이 깨지면 모든 자산이 조용히 "기록 없음"이 됐다. 이제는
    깨졌다는 사실이 그대로 판정(보류)으로 올라간다.
    """
    global _cache, _cache_key, _cache_status
    mp = _manifest_path()
    key = _norm(mp)
    if _cache_key == key and (_cache or _cache_status == PROV_CORRUPT):
        return _cache, _cache_status
    data: Dict[str, Any] = {}
    status = PROV_OK
    try:
        with mp.open(encoding="utf-8") as fh:
            raw = json.load(fh)
        if isinstance(raw, dict):
            data = {str(k): v for k, v in raw.items() if isinstance(v, dict)}
        else:
            status = PROV_CORRUPT
    except FileNotFoundError:
        data, status = {}, PROV_OK          # 아직 아무것도 기록하지 않았을 뿐이다
    except (OSError, ValueError, TypeError) as exc:
        log.warning("[image_asset_guard] 출처 기록을 읽을 수 없습니다(%s) — "
                    "확인되지 않은 이미지는 발행하지 않습니다", type(exc).__name__)
        data, status = {}, PROV_CORRUPT
    _cache, _cache_key, _cache_status = data, key, status
    return data, status


def mark(path: Any, kind: str, *, producer: str = "", slug: str = "") -> None:
    """자산의 출처를 **현재 내용(sha256)에 묶어** 기록한다.

    호출 규약 — 생산자가 **검증을 마친 그 파일**에 대해서만 부른다. 목록에 실려
    있다는 이유로 부르지 않는다(경로·파일명만으로 ``generated`` 승격 금지).
    실패해도 예외를 올리지 않는다. 다만 해시를 못 구하면 **기록하지 않는다** —
    내용에 묶이지 않은 기록은 바꿔치기를 막지 못하므로 남길 가치가 없다.
    """
    key = _norm(path)
    if not key or kind not in (KIND_GENERATED, KIND_REFERENCE, KIND_USER):
        return
    digest = _sha256(path)
    if not digest:
        log.debug("[image_asset_guard] 해시를 구하지 못해 출처 기록 생략: %s", key)
        return
    with _lock:
        data = dict(_load()[0])
        data[key] = {"kind": kind, "producer": str(producer or ""),
                     "slug": str(slug or ""), "sha256": digest}
        mp = _manifest_path()
        try:
            mp.parent.mkdir(parents=True, exist_ok=True)
            tmp = mp.with_suffix(".json.tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, mp)
        except (OSError, ValueError, TypeError) as exc:
            log.debug(f"[image_asset_guard] 출처 기록 실패({type(exc).__name__}) — 판정은 계속")
            return
        global _cache, _cache_key, _cache_status
        _cache, _cache_key, _cache_status = data, _norm(mp), PROV_OK


def mark_many(paths, kind: str, *, producer: str = "", slug: str = "") -> None:
    for p in (paths or []):
        mark(p, kind, producer=producer, slug=slug)


def provenance(path: Any, *, digest: Optional[str] = None) -> Tuple[str, str]:
    """(기록된 출처, 상태) — 상태가 ``ok`` 일 때만 그 출처를 신뢰한다.

    ``digest`` 를 넘기면 그 값을 현재 내용으로 쓴다(같은 파일을 두 번 읽지 않기 위해).
    """
    data, mstatus = _load()
    if mstatus == PROV_CORRUPT:
        return "", PROV_CORRUPT
    entry = data.get(_norm(path))
    if not isinstance(entry, dict):
        return "", PROV_NONE
    kind = str(entry.get("kind") or "")
    if kind not in (KIND_GENERATED, KIND_REFERENCE, KIND_USER):
        return "", PROV_NONE
    recorded = str(entry.get("sha256") or "").strip().lower()
    if not recorded:
        # 내용에 묶이지 않은 옛 형식 기록 — 바꿔치기를 배제할 수 없으므로 신뢰하지 않는다.
        return kind, PROV_UNBOUND
    actual = digest if digest is not None else _sha256(path)
    if not actual:
        return kind, PROV_UNBOUND
    return (kind, PROV_OK) if actual == recorded else (kind, PROV_MISMATCH)


def recorded_kind(path: Any) -> str:
    """**신뢰할 수 있는** 기록의 출처. 없거나 내용이 어긋나면 빈 문자열."""
    kind, status = provenance(path)
    return kind if status == PROV_OK else ""


# ─────────────────────────────────────────────────────────────────────────
# 구조적 신호
# ─────────────────────────────────────────────────────────────────────────
def _screenshot_roots() -> List[str]:
    """참고 캡처 전용 디렉터리 목록(정규화)."""
    roots: List[str] = []
    try:
        from config import profile as _profile
        roots.append(_norm(_profile.screenshots_dir()))
    except Exception:  # noqa: BLE001
        pass
    roots.append(_norm(Path(__file__).resolve().parent.parent / "screenshots"))
    return [r for r in roots if r]


def _under_screenshot_dir(path: Any) -> bool:
    """캡처 전용 디렉터리 안에 있는가(파일명 추측이 아니라 소속 디렉터리로 판정)."""
    key = _norm(path)
    if not key:
        return False
    for root in _screenshot_roots():
        if key == root or key.startswith(root + os.sep):
            return True
    return False


def _sha256(path: Any) -> str:
    try:
        h = hashlib.sha256()
        with open(str(path), "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except (OSError, ValueError, TypeError):
        return ""


def _decodes(path: Any) -> Optional[bool]:
    """실제로 이미지로 열리는가.

    Returns:
        True=열린다 / False=깨졌다 / **None=검사할 수 없다**.

    ⚠️ 예전에는 PIL 이 없으면 ``True``(=정상)를 돌려줬다. 검사를 못 한 것과 검사해서
    정상인 것은 다르다 — 그 혼동이 "확인되지 않은 자산이 통과"하는 구멍이었다.
    이제 검사 불능은 ``None`` 이고, 판정은 이를 보류(차단)로 다룬다.
    """
    try:
        from PIL import Image
    except Exception:  # noqa: BLE001 — 검사 도구가 없다 = 정상이라는 뜻이 아니다
        return None
    try:
        with Image.open(str(path)) as img:
            img.verify()
        return True
    except (OSError, ValueError) as exc:   # 깨진 파일 — PIL 이 보고하는 실제 디코드 실패
        log.debug("[image_asset_guard] 디코드 실패(%s): %s", type(exc).__name__, path)
        return False
    except Exception:  # noqa: BLE001 — 그 밖의 예외는 '깨졌다'가 아니라 '모른다'
        return None


def placeholder_hashes(title: str, category: str) -> frozenset:
    """이 제목·카테고리로 만들어지는 **자리표시 카드**들의 sha256 집합.

    `image_generator` 가 같은 입력으로 자리표시 카드를 다시 그려 해시를 낸다.
    렌더가 결정론적이므로 일치 = 그 자산이 자리표시 카드라는 **증명**이다.
    렌더에 실패하면 빈 집합(=판정 보류)을 돌려준다 — 추측하지 않는다.
    """
    try:
        from core import image_generator as _ig
        return frozenset(_ig.placeholder_card_hashes(title, category))
    except Exception as exc:  # noqa: BLE001 — 판정 보조 수단이므로 실패는 보류
        log.debug(f"[image_asset_guard] 자리표시 해시 계산 실패({type(exc).__name__})")
        return frozenset()


# ─────────────────────────────────────────────────────────────────────────
# 판정
# ─────────────────────────────────────────────────────────────────────────
def classify(path: Any, *, title: str = "", category: str = "",
             _placeholders: Optional[frozenset] = None) -> Dict[str, str]:
    """자산 하나를 판정한다. 부작용 0 — 파일을 지우거나 고치지 않는다.

    Returns:
        {"path", "verdict", "reason", "action", "blocking": "1"|""}
    """
    text = str(path or "").strip()
    verdict = VERDICT_OK
    if not text or not Path(text).is_file():
        verdict = VERDICT_MISSING
    else:
        digest = _sha256(text)
        kind, pstatus = provenance(text, digest=digest)
        if kind == KIND_REFERENCE or _under_screenshot_dir(text):
            # 참고 캡처는 기록이 어긋나도 참고 캡처다(보수적으로 차단 쪽을 택한다).
            verdict = VERDICT_REFERENCE
        elif not digest:
            verdict = VERDICT_UNVERIFIABLE      # 파일을 읽지 못해 아무것도 확인 못 함
        elif pstatus == PROV_CORRUPT:
            verdict = VERDICT_UNVERIFIABLE      # 출처 기록이 깨졌다 → 전부 보류
        elif pstatus == PROV_MISMATCH:
            verdict = VERDICT_TAMPERED          # 같은 경로, 다른 내용 = 바꿔치기
        else:
            decoded = _decodes(text)
            if decoded is False:
                verdict = VERDICT_UNDECODABLE
            elif decoded is None:
                verdict = VERDICT_UNVERIFIABLE  # 검사 불능 ≠ 정상
            elif pstatus == PROV_OK and kind == KIND_USER:
                # 사용자가 명시적으로 고른 사진 — 그 선택이 근거다.
                verdict = VERDICT_OK
            else:
                ph = (_placeholders if _placeholders is not None
                      else placeholder_hashes(title, category))
                if ph and digest in ph:
                    verdict = VERDICT_PLACEHOLDER
                elif pstatus == PROV_OK and kind == KIND_GENERATED:
                    # 검증을 마친 생산자가 이 내용 그대로 남긴 기록이 있다.
                    # (자리표시 재렌더가 실패해 ``ph`` 가 비어도, 이 기록은 그 자체로
                    #  신뢰 가능한 근거다 — 재렌더 실패를 '정상 증거'로 쓰는 게 아니다.)
                    verdict = VERDICT_OK
                else:
                    # 기록 없음 / 내용에 묶이지 않은 옛 기록 → 정상 여부 미확인.
                    verdict = VERDICT_LEGACY
    reason, action = _VERDICT_TEXT.get(verdict, ("", ""))
    return {"path": text, "verdict": verdict, "reason": reason, "action": action,
            "blocking": "1" if verdict in BLOCKING_VERDICTS else ""}


def inventory_block_reason(path: Any) -> str:
    """**값싼** 사전 판정 — 재고 집계·채움 대상 선별용. "" 면 후보, 아니면 차단 사유.

    발행 직전 판정(`classify`)과 **같은 방향**이되, 파일 내용을 읽지 않는다
    (sha256·PIL 디코드·자리표시 재렌더 없음 — 수천 편을 세는 자리에서 쓰기 위함).
    따라서 여기서 "" 가 나와도 **발행이 보장되지는 않는다**. 최종 판정은 언제나
    발행 직전의 `verify_publishable` 이다.

    걸러 내는 것(전부 기록/경로만으로 알 수 있는 것):
      · 참고 캡처 전용 디렉터리 소속        → ``reference_capture``
      · 출처 기록 자체가 깨짐                → ``unverifiable``
      · 기록 없음 / 내용에 묶이지 않은 옛 기록 → ``unverified_legacy``

    2026-09-23 — 예전에는 재고 집계(`draft_image_fill.images_complete`)가 "파일이
    실재하는가"만 보고 발행 게이트는 출처까지 봤다. 두 기준이 갈려서 **GUI 가
    "완성"이라 보고한 글이 발행 단계에서 전부 막히는** 거짓 안내가 났다.
    """
    text = str(path or "").strip()
    if not text:
        return VERDICT_MISSING
    if _under_screenshot_dir(text):
        return VERDICT_REFERENCE
    data, mstatus = _load()
    if mstatus == PROV_CORRUPT:
        return VERDICT_UNVERIFIABLE
    entry = data.get(_norm(text))
    if not isinstance(entry, dict):
        return VERDICT_LEGACY
    kind = str(entry.get("kind") or "")
    if kind == KIND_REFERENCE:
        return VERDICT_REFERENCE
    if kind not in (KIND_GENERATED, KIND_USER):
        return VERDICT_LEGACY
    if not str(entry.get("sha256") or "").strip():
        # 내용에 묶이지 않은 기록 — 바꿔치기를 배제할 수 없다.
        return VERDICT_LEGACY
    return ""


def verify_publishable(image_paths, *, title: str = "",
                       category: str = "") -> Tuple[bool, List[Dict[str, str]], List[str]]:
    """발행 직전 일괄 판정.

    Returns:
        (ok, findings, publishable)
        · ok          = 막는 판정이 **하나도** 없는가(fail-closed 판단용)
        · findings    = 자산별 판정 전체
        · publishable = ``ok`` 판정을 받은 자산 경로 목록

    ⚠️ 확인되지 않은 자산은 ``publishable`` 에 들어가지 않는다 — "경고만 하고 통과"는
    없다. 호출자는 하나라도 막히면 **전체를 보류**한다(부분 업로드 금지).
    """
    paths = [str(p) for p in (image_paths or [])]
    placeholders = placeholder_hashes(title, category) if paths else frozenset()
    findings = [classify(p, title=title, category=category, _placeholders=placeholders)
                for p in paths]
    blocked = [f for f in findings if f["blocking"]]
    publishable = [f["path"] for f in findings if not f["blocking"]]
    return (not blocked), findings, publishable


def describe(findings) -> str:
    """로그 한 줄 요약 — 어떤 자산이 왜 막혔는지."""
    parts = []
    for f in (findings or []):
        if not f.get("blocking"):
            continue
        parts.append(f"{os.path.basename(f.get('path') or '?')}: {f.get('reason')}")
    return " / ".join(parts)
