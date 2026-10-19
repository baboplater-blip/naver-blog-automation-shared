# core/draft_image_fill.py
# 🎨 이미지 자동 채움 — 핵심 모듈 + 스캔 배치 CLI
# ─────────────────────────────────────────────────────────────────────────────
# 보관본(draft_ready) 글에 썸네일 + 본문 이미지를 생성해 image_paths 를 채운다.
#
# ★2026-09-10 — 두 경로가 있고 **기본은 계획 경로**다(settings.IMAGE_FILL_PLAN_MODE).
#   · 계획 경로(기본): 글을 만들 때 집필 호출에서 함께 저장해 둔 `post_data["image_plan"]`
#     (core/image_plan.py 스키마)대로 **생성만** 한다. 기획 LLM 호출 0회.
#     본문 이미지는 **글자·숫자·표·로고·문서·UI 가 없는** 관련 이미지라 비전 전사(OCR)
#     검증을 하지 않고, 썸네일에 새긴 **짧은 문구 하나만** 기존 검증을 최소 적용한다.
#     계획이 없거나 망가진 글은 **로컬 결정론 계획**을 그 자리에서 한 번 만들어 저장한다
#     (LLM 재기획 폴백은 없다 — 전체 기존 글 사전 마이그레이션도 하지 않는다).
#     한 편 안의 슬롯은 IMAGE_FILL_PARALLEL(기본 2, 상한 4)만큼 **제한 병렬** 생성한다.
#   · 레거시 인포카드 경로(IMAGE_FILL_PLAN_MODE=False): Stage1(본문→카드 구성안)을
#     image_prompt_builder.plan_infocards 로 만들고 카드마다 비전 검증한다(예전 동작).
#
# 생성 엔진은 settings.IMAGE_FILL_ENGINE 으로 고른다(2026-09-09 전환):
#   · "codex"(기본)  — core.codex_image.CodexImageEngine. codex CLI 내장 이미지 도구를
#                      쓰고, 산출물을 **요청별로 수확·검증**한다(위조 대체본 차단).
#   · "genspark"     — 레거시 브라우저 자동화(_GensparkEngine). 옵트인 — 이 엔진을 고르고
#                      GENSPARK_ENABLED=True 여야만 브라우저가 뜬다.
# 두 엔진은 같은 메서드 계약(open/generate/regenerate/카드 세션/close)을 구현해 교체된다.
#
# 설계 원칙(전 경로 준수):
# - 게이트: settings.IMAGE_FILL_ENABLED. 꺼지면 완전 no-op(바이트 불변).
#   ⚠️ 폴백 범위를 정확히 적는다 — 새 키가 없는 기존 설치는 **ON/OFF 만** 예전
#   GENSPARK_ENABLED 로 폴백한다. **엔진 선택은 폴백하지 않는다**(IMAGE_FILL_ENGINE 이
#   없으면 무조건 "codex"). 즉 젠스파크로 운영하던 설치를 업그레이드하면 채움은 계속
#   켜진 채 **엔진이 codex 로 바뀐다** — 이는 2026-09-09 전환의 의도된 동작이다.
#   젠스파크를 계속 쓰려면 IMAGE_FILL_ENGINE="genspark" + GENSPARK_ENABLED=True 를 명시한다.
# - fail-open: 어떤 예외도 전파하지 않는다. 실패 시 빈/부분 결과를 돌려주고 흐름을 잇는다.
# - 발행 금지: 이 모듈은 image_paths 를 채워 보관할 뿐, 절대 발행하지 않는다. 실제 발행
#   배선은 Phase 2b/3. auto_scheduler·naver_uploader·draft_store·content_generator 는
#   호출만 하고 수정하지 않는다.
# - YMYL 안전: 오타·날조(명세 밖 기관명·수치·날짜) 이미지는 비전 검증에서 걸러 image_paths
#   에 절대 넣지 않는다. 검증을 통과한 이미지만 채운다.
# - 프로필 격리·범용: 주제·계정 하드코딩 0. 계정 레지스트리·프로필 경로는 genspark_client·
#   config.profile 이 관리한다.
#
# 주입식 의존(테스트/커스텀):
#   genspark  — 이미지 생성 엔진(기본: core.genspark_client 위의 브라우저 구동 어댑터).
#   planner   — Stage1 카드 기획(기본: image_prompt_builder.plan_infocards).
#   verifier  — baked 한글 비전 검증(기본: 내부 Claude CLI Read 검증).
#   셋 다 주입하면 라이브(브라우저·CLI) 없이 단위 테스트된다.
#
# Phase 2a(이 모듈) ↔ 발행측 배선(Phase 2b/3)의 경계 — 2026-08-31 배선 완료 반영:
#   · 이 모듈은 이미지를 **생성·검증·보관**까지만 한다(발행 안 함 원칙 불변).
#   · `_force_image_insert=True` 는 draft_store.save_draft 가 additive 로 영속한다
#     (참일 때만 키 추가 — 신호 없는 기존 payload 는 바이트 그대로, draft_store.py
#     save_draft 참조). load_drafts 는 post_data 안에 실어 반환한다.
#   · 발행측(Phase 2b/3)은 auto_scheduler 에 배선 완료: 보관본 발행 경로가
#     `draft_hit["post_data"]["image_paths"]` 를 읽고(과거 최상위 키를 읽어 항상 빈
#     리스트가 되던 버그는 같은 배선에서 수정됨), [HOOK G] 가 IMAGE_FILL_ENABLED 게이트
#     뒤에서 이 모듈을 호출하며, upload 경로에 `_force_image_insert` 를 전달해 전역
#     SKIP_IMAGE_INSERT 를 per-post 로 우회한다(naver_uploader._should_insert_images).
#   · 계약 회귀는 tests/test_draft_image_publish_wiring.py 가 고정한다.
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

try:  # 프로필 격리 로거(있으면 사용) — 없으면 표준 로깅 폴백.
    from config.logger import get_logger
    log = get_logger(__name__)
except Exception:  # noqa: BLE001 — config 미로드 환경(경량 테스트 스텁)
    import logging
    log = logging.getLogger(__name__)


# ── 설정 접근(전부 getattr 기본값 — settings.py 에 줄이 없어도 안전) ───────────
def _setting(name: str, default: Any) -> Any:
    try:
        from config import settings as _s
        return getattr(_s, name, default)
    except Exception:  # noqa: BLE001 — settings 미로드면 기본값
        return default


def _genspark_enabled() -> bool:
    return bool(_setting("GENSPARK_ENABLED", False))


# ── 엔진 중립 게이트(2026-09-09 Codex 전환) ───────────────────────────────────
# 기본 엔진이 젠스파크(브라우저 자동화) → Codex CLI 로 바뀌었다. 기존 사용자 설정을 깨지
# 않도록 새 키가 없으면 예전 GENSPARK_* 값으로 폴백한다.
def _image_fill_enabled() -> bool:
    """이미지 자동 채움 마스터 스위치. 새 키 우선, 없으면 예전 GENSPARK_ENABLED."""
    value = _setting("IMAGE_FILL_ENABLED", None)
    if value is None:
        return _genspark_enabled()
    return bool(value)


def _image_fill_engine() -> str:
    """사용할 엔진 이름 — "codex" | "genspark"."""
    name = str(_setting("IMAGE_FILL_ENGINE", "codex") or "codex").strip().lower()
    return name if name in ("codex", "genspark") else "codex"


def _fill_setting(new_key: str, legacy_key: str, default: Any) -> Any:
    """엔진 중립 키 우선, 없으면 예전 GENSPARK_* 키, 그것도 없으면 기본값."""
    value = _setting(new_key, None)
    if value is None:
        value = _setting(legacy_key, None)
    return default if value is None else value


def _mark_generated(path: Any, *, slug: str, producer: str) -> None:
    """방금 **만들어서 검증을 마친** 자산의 출처를 기록한다(2026-09-23).

    발행 경계(`core.image_asset_guard`)는 출처를 모르는 이미지를 올리지 않는다.
    기록은 파일 내용(sha256)에 묶이므로, 나중에 같은 이름으로 다른 파일이 놓이면
    기록이 무효가 된다. **여기 말고 다른 곳에서 부르지 말 것** — 목록에 실려
    있다는 이유로 기록하면 '출처 있음'이 거짓이 된다.
    """
    if not path:
        return
    try:
        from core import image_asset_guard as _guard
        _guard.mark(path, _guard.KIND_GENERATED, producer=producer, slug=str(slug or ""))
    except Exception as exc:  # noqa: BLE001 — 기록 실패가 생성을 되돌리지 않는다
        log.debug(f"[draft_image_fill] 출처 기록 실패({type(exc).__name__})")


def _verified_body_source(path: Any, body_slot: Any, *, slug: str) -> Tuple[str, str]:
    """Return a SHA only for a real same-post generated no-text body slot."""
    from core import image_plan as ip
    if not isinstance(body_slot, dict) or body_slot.get("role") != ip.ROLE_BODY:
        return "", "source_not_body_slot"
    if body_slot.get("text_policy") != ip.TEXT_POLICY_NO_TEXT:
        return "", "source_not_no_text"
    source = Path(str(path or ""))
    if not source.is_file():
        return "", "source_missing"
    try:
        from core import image_asset_guard as guard
        kind, state = guard.provenance(source)
        if kind != guard.KIND_GENERATED or state != guard.PROV_OK:
            return "", "source_provenance_unverified"
        records, manifest_state = guard._load()
        entry = records.get(guard._norm(source), {})
        if manifest_state != guard.PROV_OK or entry.get("slug") != str(slug or ""):
            return "", "source_slug_unverified"
        expected_producer = f"draft_image_fill:slot:{body_slot.get('id')}"
        if entry.get("producer") != expected_producer:
            return "", "source_role_unverified"
        hasher = hashlib.sha256()
        with source.open("rb") as source_file:
            for chunk in iter(lambda: source_file.read(1024 * 1024), b""):
                hasher.update(chunk)
        digest = hasher.hexdigest()
        if digest != str(entry.get("sha256") or "").lower():
            return "", "source_sha_mismatch"
        return digest, ""
    except (OSError, ValueError, TypeError) as exc:
        log.warning("[draft_image_fill] 본문 사진 출처 확인 실패(%s)", type(exc).__name__)
        return "", f"source_provenance_error:{type(exc).__name__}"


def _has_verified_mapped_thumbnail(post_data: Any, *, slug: str) -> bool:
    """Require a real thumbnail slot mapping plus SHA-bound same-post provenance."""
    if not isinstance(post_data, dict):
        return False
    plan = post_data.get("image_plan")
    mapping = post_data.get("image_slots")
    if not isinstance(plan, dict) or not isinstance(mapping, dict):
        return False
    thumbnail = next((slot for slot in plan.get("slots") or []
                      if isinstance(slot, dict) and slot.get("role") == "thumbnail"), None)
    if not thumbnail:
        return False
    path = Path(str(mapping.get(str(thumbnail.get("id") or "")) or ""))
    if not path.is_file():
        return False
    try:
        from core import image_asset_guard as guard
        kind, state = guard.provenance(path)
        if kind != guard.KIND_GENERATED or state != guard.PROV_OK:
            return False
        records, manifest_state = guard._load()
        entry = records.get(guard._norm(path), {})
        if manifest_state != guard.PROV_OK or entry.get("slug") != str(slug or ""):
            return False
        allowed = {f"draft_image_fill:slot:{thumbnail.get('id')}",
                   "draft_image_fill:thumb"}
        return entry.get("producer") in allowed
    except (OSError, ValueError, TypeError):
        return False


def compose_photo_backed_thumbnail(
    source_path: Any, body_slot: Dict[str, Any], *, slug: str,
    thumbnail_slot: Dict[str, Any], out_dir: Any, category: str = "",
    cta: str = "", date_label: str = "",
) -> Dict[str, Any]:
    """Copy a verified body photo and compose the confirmed thumbnail message onto it.

    The original body file and saved plan are never modified. Callers may use this helper
    after body slots are collected or to repair one explicitly selected same-post photo.
    """
    from core import image_plan as ip
    digest, reason = _verified_body_source(source_path, body_slot, slug=slug)
    if reason:
        return {"ok": False, "path": None, "reason": reason}
    spec = ip.overlay_spec(thumbnail_slot)
    if spec is None and isinstance(thumbnail_slot, dict):
        message = str(thumbnail_slot.get("label") or "").strip()
        if message:
            spec = {"message": message, "style": ip.thumb_style()}
    if spec is None:
        return {"ok": False, "path": None, "reason": "thumbnail_message_missing"}
    try:
        from core import thumbnail_compose as tc
        if tc.is_composed(source_path):
            return {"ok": False, "path": None, "reason": "source_is_composed_thumbnail"}
        target_dir = Path(out_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        slot_id = re.sub(r"[^0-9A-Za-z_-]+", "-", str(body_slot.get("id") or "body"))
        background = target_dir / f"thumb_from_{slot_id}_{digest[:12]}_{uuid.uuid4().hex[:8]}.png"
        shutil.copy2(str(source_path), str(background))
        output = target_dir / f"{background.stem}_titled.png"
        result = tc.compose(background, output, message=spec["message"],
                            style=spec.get("style"), cta=cta, category=category,
                            date_label=date_label)
        if not result.get("ok") or not result.get("path"):
            return {"ok": False, "path": None, "reason": str(result.get("reason") or "compose_failed"),
                    "source_body_slot": body_slot.get("id"), "source_sha256": digest,
                    "background": str(background)}
        result_path = Path(result["path"])
        _mark_generated(result_path, slug=slug,
                        producer=f"draft_image_fill:slot:{ip.SLOT_THUMB}")
        return {"ok": True, "path": str(result_path), "reason": "",
                "source_body_slot": body_slot.get("id"), "source_sha256": digest,
                "background": str(background), "compose": result}
    except (OSError, ValueError, TypeError) as exc:
        log.warning("[draft_image_fill] 본문 사진 썸네일 합성 실패(%s)", type(exc).__name__)
        return {"ok": False, "path": None, "reason": f"photo_compose_error:{type(exc).__name__}"}


def _build_engine(*, out_dir: Path, account: Optional[str],
                  journal: Optional[Any] = None, budget: Optional[Any] = None) -> Any:
    """설정에 맞는 이미지 엔진 인스턴스. 두 엔진은 같은 메서드 계약을 구현한다.

    Args:
        journal/budget: 병렬 워커가 **공유**하는 요청 저널·호출 예산(codex 엔진 전용).
            주지 않으면 예전처럼 엔진마다 자기 저널을 만들고 자기 카운터로 상한을 센다.
    """
    if _image_fill_engine() == "genspark":
        return _GensparkEngine(out_dir=out_dir, account=account)
    from core.codex_image import CodexImageEngine, RequestJournal
    return CodexImageEngine(out_dir=out_dir,
                            journal=journal if journal is not None else RequestJournal(),
                            budget=budget)


# ── 신규 경로(2026-09-10) 설정 ────────────────────────────────────────────────
# 계획 기반 채움: 기획은 집필 때 이미 저장돼 있고, 채움은 **생성만** 한다.
#   · 본문 이미지 = 글자 없는 관련 이미지(비전 OCR 검증 없음)
#   · 썸네일 = 짧은 문구 1개(기존 비전 검증을 그 라벨에만 최소 적용)
# 끄면(False) 예전 인포카드 경로(plan_infocards + 카드마다 비전 검증)로 돌아간다.
def _plan_mode_enabled() -> bool:
    return bool(_setting("IMAGE_FILL_PLAN_MODE", True))


def plan_mode_enabled() -> bool:
    """계획 경로가 켜져 있는가(공개 접근자 — 설정만 읽고 부작용 0).

    배선하는 쪽(예: auto_scheduler 의 save_only 이미지 생성 주체 판정)이 이 판정을
    손으로 복제하지 않게 하는 단일 진실 원본이다. ``image_fill_gate()`` 와 짝이다.
    """
    return _plan_mode_enabled()


def images_complete(post_data: dict) -> tuple:
    """저장된 글 하나의 **이미지 완전성 정적 판정**(채움 시도 없이, 부작용 0).

    발행 직전 게이트(`auto_scheduler._archive_images_complete`)가 계획 경로에서 쓰는
    판정과 **같은 규칙**을 쓴다 — "계획된 슬롯 전부에 실재하는 파일이 붙어 있는가".
    재고 집계처럼 채움 결과(meta)가 없는 자리에서 쓰라고 분리해 둔 것이다.

    Returns:
        (complete, detail)
        · complete = 계획된 슬롯이 전부 채워졌는가(불명이면 False — fail-closed)
        · detail   = {"mode": "plan"|"legacy", "slots": int, "filled": int,
                      "existing": int, "missing": int, "reason": str}
          `reason` 이 비어 있지 않으면 **완성으로 셀 수 없는 사유**다.
    """
    src = post_data if isinstance(post_data, dict) else {}
    raw_paths = [str(p) for p in (src.get("image_paths") or []) if str(p or "").strip()]
    existing = 0
    for path in raw_paths:
        try:
            if Path(path).is_file():
                existing += 1
        except (OSError, TypeError, ValueError):
            continue
    detail = {"mode": "legacy", "slots": 0, "filled": 0,
              "existing": existing, "missing": len(raw_paths) - existing,
              "reason": ""}

    if not plan_mode_enabled():
        # 계획 경로가 꺼져 있으면 기대 슬롯 수를 알 수 없다 — 예전 규칙(경로가 있고
        # 전부 실재하면 채워진 것)을 그대로 쓴다.
        complete = bool(raw_paths) and existing == len(raw_paths)
        if not complete:
            detail["reason"] = "no_images" if not raw_paths else "missing_files"
        return complete, detail

    plan = src.get("image_plan") or {}
    slots = (plan.get("slots") or []) if isinstance(plan, dict) else []
    detail["mode"] = "plan"
    detail["slots"] = len(slots)
    if not slots:
        # 계획이 없는 글(레거시 보관본 포함)은 **완성 여부를 확인할 수 없다.**
        # 이미지가 몇 장 있어도 "발행 가능한 완성본"으로 세지 않는다.
        detail["reason"] = "no_plan"
        return False, detail

    try:
        from core import image_plan as _ip
        assets, missing, _extras = _ip.slot_assets(plan, src)
    except Exception as exc:  # noqa: BLE001 — 판정 불가는 미완으로 본다(fail-closed)
        detail["reason"] = f"plan_error:{type(exc).__name__}"
        return False, detail

    # 발행 직전 게이트가 막을 자산은 **재고로도 세지 않는다**(2026-09-23).
    # 예전에는 여기서 "파일이 실재하는가"만 봐서, 출처를 확인할 수 없는 옛 자산이
    # 붙은 글이 GUI·무인 운영에 "완성 재고"로 보고된 뒤 발행 단계에서 전부 막혔다.
    # 판정은 값싼 사전 판정(기록·경로만, 내용 미열람)이라 수천 편을 세도 느리지 않다.
    unverified_reasons = _unverified_slot_reasons(slots, assets)
    unverified = set(unverified_reasons)
    filled = [s for s in slots if isinstance(s, dict)
              and s.get("id") in assets and s.get("id") not in unverified]
    detail["filled"] = len(filled)
    detail["missing"] = len(missing)
    detail["unverified"] = len(unverified)
    # 사유별 건수 — 화면이 "출처 기록 손상"과 "옛 이미지"를 구분해 말할 수 있게.
    if unverified_reasons:
        counts: Dict[str, int] = {}
        for verdict in unverified_reasons.values():
            counts[verdict] = counts.get(verdict, 0) + 1
        detail["unverified_reasons"] = counts
    complete = len(filled) == len(slots)
    if not complete:
        # 빈 슬롯이 하나라도 있으면 그건 평범한 '채우다 만 글'이다(자동 보충 대상).
        # **모든 슬롯에 자산이 붙어 있는데** 그중 확인 안 된 것 때문에 미완인 경우에만
        # `unverified_assets` — 이 경우는 자동 재생성 대상이 아니라 사용자 판단 몫이다.
        only_unverified = unverified and (len(filled) + len(unverified) == len(slots))
        detail["reason"] = "unverified_assets" if only_unverified else "slots_unfilled"
    return complete, detail


def slots_of_plan(plan) -> list:
    """계획에서 슬롯 목록만 안전하게 꺼낸다(계획 형식이 달라도 예외 없음)."""
    if not isinstance(plan, dict):
        return []
    return [s for s in (plan.get("slots") or []) if isinstance(s, dict)]


def _unverified_slot_reasons(slots, assets) -> dict:
    """슬롯에 붙었지만 **발행 게이트가 막을** 자산 → ``{슬롯 id: 사유 코드}``.

    사유를 함께 돌려주는 이유는 화면 때문이다(2026-09-23). 예전에는 막힌다는 사실만
    알고 **왜** 막히는지는 몰라서, GUI 가 "출처 기록이 깨졌다"와 "옛날 이미지라
    출처를 모른다"를 같은 문구로 묶어 보여 줬다 — 사용자가 할 조치가 전혀 다른데도.

    판정기를 못 불러오면 빈 dict(기존 동작 보존 — 판정 불능이 재고를 0으로 만들지 않는다).
    """
    try:
        from core import image_asset_guard as _guard
    except Exception:  # noqa: BLE001 — 모듈 부재 = 예전 규칙
        return {}
    out: Dict[str, str] = {}
    for slot in slots:
        if not isinstance(slot, dict):
            continue
        sid = slot.get("id")
        path = assets.get(sid) if isinstance(assets, dict) else None
        if not path:
            continue
        try:
            verdict = _guard.inventory_block_reason(path)
        except Exception:  # noqa: BLE001 — 판정 실패는 통과(최종 게이트가 다시 본다)
            continue
        if verdict:
            out[sid] = str(verdict)
    return out


def _unverified_slot_ids(slots, assets) -> set:
    """`_unverified_slot_reasons` 의 id 집합만(기존 호출 계약 보존)."""
    return set(_unverified_slot_reasons(slots, assets))


def _verify_thumb_enabled() -> bool:
    """썸네일 라벨 비전 검증 여부(편당 Claude 호출 최대 1회). 본문은 언제나 검증 없음."""
    return bool(_setting("IMAGE_FILL_VERIFY_THUMB", True))


# 한 편 안에서 동시에 생성할 슬롯 수. 1이면 순차(예전과 동일한 호출 순서).
MAX_PARALLEL_WORKERS: int = 4


def _parallel_workers(explicit: Optional[int] = None) -> int:
    """안전한 제한 병렬 수 — 기본 2, 1이면 순차, 합리적 상한 4."""
    value = explicit if explicit is not None else _setting("IMAGE_FILL_PARALLEL", 2)
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = 2
    return max(1, min(MAX_PARALLEL_WORKERS, n))


# ── 프롬프트 상수 ─────────────────────────────────────────────────────────────
# ★Phase 0 실측: 모델이 본문에 없는 '국토교통부' 같은 기관명을 임의로 그려 넣었다(YMYL
#   위험). 모든 프롬프트에 이 금지 조항을 명시해 "명세에 없는 사실"을 원천 차단한다.
_NO_EXTRA_FACTS_CLAUSE: str = (
    "절대 규칙 — 이 카드/썸네일 명세에 명시적으로 적힌 한글 텍스트만 이미지에 새기세요. "
    "명세에 없는 기관명·숫자·금액·퍼센트·날짜·통계를 절대 추가하거나 지어내지 마세요. "
    "STRICT RULE: bake ONLY the Korean text explicitly listed in this spec. Do NOT add or "
    "invent any institution name, number, amount, percentage, date, or statistic that is not "
    "listed here."
)

# 젠스파크 모델(한글 렌더링용). genspark_client 상수를 재사용하되 미로드 시 폴백.
try:
    from core.genspark_client import MODEL_GPT_IMAGE_2 as _MODEL_GPT_IMAGE_2
except Exception:  # noqa: BLE001
    _MODEL_GPT_IMAGE_2 = "GPT Image 2"


# ─────────────────────────────────────────────────────────────────────────────
# 본문 평문화
# ─────────────────────────────────────────────────────────────────────────────
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _to_plain_text(content: Any) -> str:
    """post_data.content(마크다운 또는 HTML)를 평문으로 확보.

    보관본은 HTML 이 content 에 올 수 있어 태그를 제거하고 공백을 접는다. 마크다운은
    그대로도 fact-grounding·기획에 무해하지만, HTML 태그가 섞이면 토큰이 오염되므로
    방어적으로 태그를 제거한다.
    """
    if not isinstance(content, str) or not content.strip():
        return ""
    text = _TAG_RE.sub(" ", content)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return _WS_RE.sub(" ", text).strip()


# ─────────────────────────────────────────────────────────────────────────────
# 프롬프트 조립
# ─────────────────────────────────────────────────────────────────────────────
def _build_thumbnail_prompt(title: str) -> str:
    """예전 채움 경로도 현재 사진형 썸네일 빌더를 사용한다."""
    from core import image_plan as ip
    label = ip.thumb_label_from_title(title)
    return ip.build_thumb_prompt(label, title=title) + _NO_EXTRA_FACTS_CLAUSE + "\n"


def _build_card_prompt(card: Any, *, title: str, category: str) -> str:
    """카드(세로) 프롬프트 — image_prompt_builder.build_infocard_prompt 재사용 + 금지 조항.

    build_infocard_prompt 는 headline/subhead/bullet(label·value)를 정확히 baked 하고
    인젝션 안전화(따옴표·개행 정규화)를 이미 수행한다. 그 위에 '명세 밖 사실 금지'
    조항을 명시적으로 덧붙인다(Phase 0 기관명 날조 실측 대응).
    """
    base = ""
    try:
        from core.image_prompt_builder import build_infocard_prompt
        base = build_infocard_prompt(card, title=title, category=category)
    except Exception as exc:  # noqa: BLE001 — 빌더 실패 시 최소 프롬프트로 폴백
        log.warning(f"[draft_image_fill] build_infocard_prompt 실패({type(exc).__name__}) — 최소 프롬프트")
        headline = _sanitize_prompt_text(getattr(card, "headline", "") or "")
        base = (
            "Design a single professional Korean card-news infographic (vertical poster-style "
            "card), studio-quality, clean and legible.\n"
            f"CRITICAL — bake this Korean headline exactly as given: \"{headline}\"\n"
        )
    return base.rstrip() + "\n" + _NO_EXTRA_FACTS_CLAUSE + "\n"


def _sanitize_prompt_text(text: Any) -> str:
    """프롬프트에 따옴표로 감싸 넣을 텍스트를 인젝션 안전하게 정규화.

    카드/제목 텍스트는 리서치 캐시 등 외부 웹텍스트를 요약한 값일 수 있어 간접 프롬프트
    인젝션 표면이다. 제어문자·백틱 제거, 큰따옴표를 작은따옴표로 치환, 공백 접기.
    """
    if not isinstance(text, str) or not text:
        return ""
    cleaned = "".join(" " if (ord(c) < 0x20 or ord(c) == 0x7F) else c for c in text)
    cleaned = cleaned.replace("`", "")
    for quote in ('"', "“", "”"):
        cleaned = cleaned.replace(quote, "'")
    return _WS_RE.sub(" ", cleaned).strip()


# ─────────────────────────────────────────────────────────────────────────────
# 카드 → 기대 baked 텍스트(비전 검증 대조 기준)
# ─────────────────────────────────────────────────────────────────────────────
def _card_expected_texts(card: Any) -> List[str]:
    """카드가 이미지에 새길 한글 텍스트 목록(검증 기대값).

    build_infocard_prompt 가 headline·subhead·bullet label·value 를 baked 하므로 그
    전부를 기대값으로 삼는다(빈 문자열 제외).
    """
    texts: List[str] = []
    for attr in ("headline", "subhead"):
        val = str(getattr(card, attr, "") or "").strip()
        if val:
            texts.append(val)
    for bullet in list(getattr(card, "bullets", []) or []):
        for attr in ("label", "value"):
            val = str(getattr(bullet, attr, "") or "").strip()
            if val:
                texts.append(val)
    # 중복 제거(순서 보존)
    seen = set()
    out: List[str] = []
    for t in texts:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _build_correction_prompt(expected_texts: Sequence[str], missing: Sequence[str],
                             extra_facts: Sequence[str]) -> str:
    """검증 실패 시 같은 스레드에 보낼 짧은 교정 프롬프트.

    철자 오류·명세 밖 사실을 지목해 재생성을 유도한다(레이아웃·팔레트는 유지).
    """
    lines = ["이전 이미지를 같은 디자인·구도로 다시 그리되 다음을 정확히 고쳐 주세요."]
    if missing:
        joined = " / ".join(_sanitize_prompt_text(m) for m in missing if m)
        lines.append(f"- 다음 한글을 정확한 철자로 다시 새기세요: {joined}")
    if extra_facts:
        joined = " / ".join(_sanitize_prompt_text(x) for x in extra_facts if x)
        lines.append(f"- 명세에 없는 다음 내용을 이미지에서 제거하세요(절대 넣지 마세요): {joined}")
    lines.append(_NO_EXTRA_FACTS_CLAUSE)
    return "\n".join(lines) + "\n"


# ─────────────────────────────────────────────────────────────────────────────
# 기본 비전 검증기(Claude CLI Read 로 전사 → 코드로 결정론 판정)
# ─────────────────────────────────────────────────────────────────────────────
# ★라이브 실측(2026-08-29): ① `--json-schema` 를 주면 모델이 Read 를 건너뛰고 빈 JSON 을
#   뱉는다(이미지 안 봄). ② `--json-schema` 를 빼면 Read 는 복구되지만, Read 도구를 쓴
#   뒤엔 "순수 JSON 출력" 지시를 무시하고 산문으로 답한다(전사는 정확). plan_infocards 가
#   JSON 강제로 됐던 건 **도구를 안 써서**다. 따라서 검증기는 claude 에 **전사만** 시키고
#   (구조 강제 없음) missing·extra_facts 판정은 코드에서 결정론적으로 한다 — 라이브 없이
#   단위 테스트 가능(_parse_transcription·_judge_transcription).
_CLAUDE_VISION_MODEL: str = "sonnet"
_VERIFY_TIMEOUT_SEC: int = 180


def _claude_cmd() -> str:
    """content_generator 와 동일한 단일 경로로 claude 실행 파일을 찾는다."""
    try:
        from core.content_generator import _find_claude_cmd
        return _find_claude_cmd()
    except (ImportError, AttributeError):
        import shutil
        return shutil.which("claude") or ""


def _resolve_vision_model() -> str:
    """GENSPARK_VISION_MODEL(백엔드 선택자)을 실제 claude 모델 별칭으로 해석.

    "claude" → 비전 지원 모델 별칭. 그 외는 미배선 → 빈 문자열(검증 스킵 신호).
    """
    backend = str(_setting("GENSPARK_VISION_MODEL", "claude") or "claude").strip().lower()
    if backend == "claude":
        return _CLAUDE_VISION_MODEL
    log.warning(f"[draft_image_fill] 비전 백엔드 '{backend}' 미배선 — 검증 스킵")
    return ""


def _build_verify_prompt(name: str) -> str:
    """검증 프롬프트 — **전사 전용**. Read 로 이미지를 열어 보이는 텍스트만 옮겨 적게 한다.

    ★라이브 실측(2026-08-29): Read 도구를 쓴 뒤엔 "순수 JSON 출력" 지시가 신뢰 불가하다
    (모델이 이미지를 완벽히 읽고도 산문으로 답해 JSON 파싱 실패). plan_infocards 가 됐던 건
    도구를 안 써서다. 그래서 claude 에는 **전사만** 시키고(구조 강제 없음) 판정(missing·
    extra_facts)은 코드에서 결정론적으로 한다. 기대 텍스트는 프롬프트에 넣지 않는다 —
    모델이 기대값을 그대로 베껴 적어 전사를 오염시키는 것을 막기 위함.
    """
    return (
        f"Read 도구로 현재 폴더의 '{name}' 이미지 파일을 반드시 여세요. 그런 다음 이미지 "
        "안에 **실제로 보이는 모든 한글·숫자 텍스트**를, 한 줄에 하나씩 `- ` 접두를 붙여 "
        "본 그대로 옮겨 적으세요.\n"
        "- 이미지에 없는 내용을 추측하거나 지어내지 마세요.\n"
        "- 설명·요약·해석을 덧붙이지 말고, 보이는 텍스트만 나열하세요.\n"
        "- 금액·퍼센트·날짜·기관명도 보이는 그대로 적으세요."
    )


# ── 전사 파싱 · 정규화 · 판정(결정론, 코드) ─────────────────────────────────
_BULLET_LINE_RE = re.compile(r"^\s*(?:[-*•·▪●○]|\d+[.)])\s+(.+\S)\s*$")
_BOLD_WRAP_RE = re.compile(r"^\*\*(.+?)\*\*$|^__(.+?)__$")
# _norm 대상 제거 문자(공백·구두점·쉼표). %·원 등 단위는 보존(판정에 쓰임).
_NORM_STRIP_RE = re.compile(r"[\s,.\-·:;!?~…'\"“”‘’`()\[\]{}/\\|]+")
# 단위 있는 숫자(금액·퍼센트·기간·수량 등).
_UNIT_NUMBER_RE = re.compile(
    r"\d[\d,\.]*\s*(?:만원|억원|천원|원|억|만|천|％|%|퍼센트|개월|일|년|세|명|위|회|건|가구)")

# 기관명 검출 — 접미사(부·청·처·원·과) 정규식은 근본적으로 과탐한다(정부·지원·병원·
# 학원·결과·전부…, 라이브에서 "주거지원"의 "원"이 org 로 오탐돼 완벽한 썸네일을 불합격).
# 그래서 **실제 정부기관 고유명사 목록**과의 부분문자열 매칭으로 바꾼다(오탐 0). 새 기관이
# 필요하면 이 리스트에 풀네임을 추가한다(확장 가능).
_KNOWN_AGENCIES: tuple = (
    "국토교통부", "보건복지부", "고용노동부", "기획재정부", "행정안전부", "교육부",
    "여성가족부", "중소벤처기업부", "문화체육관광부", "산업통상자원부", "국방부",
    "법무부", "외교부", "통일부", "농림축산식품부", "해양수산부", "환경부", "과학기술정보통신부",
    "국세청", "병무청", "관세청", "조달청", "통계청", "경찰청", "소방청", "산림청", "특허청",
    "국민연금공단", "근로복지공단", "국민건강보험공단", "건강보험심사평가원",
    "주택도시보증공사", "한국토지주택공사", "소상공인시장진흥공단", "서민금융진흥원",
    "국민체육진흥공단", "한국장학재단", "신용보증기금", "기술보증기금",
    "주거복지지원과", "국민연금", "건강보험공단",
)


def _norm(text: str) -> str:
    """판정용 정규화 — 공백·구두점·쉼표 제거, 소문자. 단위 문자(%·원 등)는 보존."""
    return _NORM_STRIP_RE.sub("", str(text or "")).lower()


def _clean_transcription_line(line: str) -> str:
    """한 전사 라인에서 불릿/번호/굵게 접두를 관대하게 벗겨 텍스트만 남긴다."""
    text = line.strip()
    bullet = _BULLET_LINE_RE.match(text)
    if bullet:
        text = bullet.group(1).strip()
    bold = _BOLD_WRAP_RE.match(text)
    if bold:
        text = (bold.group(1) or bold.group(2) or "").strip()
    return text


def _parse_transcription(stdout: str) -> List[str]:
    """claude 전사 stdout 에서 텍스트 라인들을 뽑는다.

    불릿(`- `·`* `·`1. ` 등)이 있는 라인을 우선 채택(설명 산문 배제). 불릿이 하나도
    없으면(모델이 평문 나열) 비어있지 않은 라인 전부를 폴백 채택.
    """
    raw_lines = [ln for ln in (stdout or "").splitlines() if ln.strip()]
    bulleted = [_clean_transcription_line(ln) for ln in raw_lines
                if _BULLET_LINE_RE.match(ln.strip())]
    bulleted = [t for t in bulleted if t]
    if bulleted:
        return bulleted
    # 폴백 — 불릿 없이 평문 나열한 경우.
    return [_clean_transcription_line(ln) for ln in raw_lines
            if _clean_transcription_line(ln)]


def _judge_transcription(texts_found: Sequence[str],
                         expected_texts: Sequence[str]) -> dict:
    """전사 결과를 기대값과 대조해 결정론적으로 판정한다(코드, 라이브 0으로 테스트 가능).

    - missing: 각 기대 문자열의 정규화형이 전사 결합본(정규화)에 부분문자열로 없으면 누락
      (철자 오류는 자연히 누락으로 잡힘).
    - extra_facts: ①전사의 단위 숫자(금액·%·기간) 중 기대에 없는 것 + ②전사에 등장하는
      _KNOWN_AGENCIES(정부기관 고유명사) 중 기대에 없는 것(명세 밖 날조). 기관명은 접미사
      정규식이 아니라 큐레이트 목록 매칭이라 "주거지원"·"정부"·"지원" 같은 일반어 오탐 0.
    - ok = 누락 없음 and extra 없음 and 전사 비어있지 않음.
    - 전사가 비면 reason="unread"(모델이 이미지를 못 읽은 정황) — 보수적 미채택.
    """
    found = [str(t).strip() for t in texts_found if str(t).strip()]
    expected = [str(e).strip() for e in expected_texts if str(e).strip()]
    if not found:
        return {"ok": False, "missing": list(expected), "extra_facts": [],
                "texts_found": [], "reason": "unread"}

    combined = _norm(" ".join(found))
    expected_combined = _norm(" ".join(expected))
    missing = [e for e in expected if _norm(e) and _norm(e) not in combined]

    extra: List[str] = []
    seen: set = set()
    # ① 단위 숫자(금액·%·기간) — 정규식(YMYL 핵심).
    for line in found:
        for token in _UNIT_NUMBER_RE.finditer(line):
            _collect_extra(token.group(0), expected_combined, extra, seen)
    # ② 기관명 — 큐레이트 목록(접미사 정규식 과탐 제거). 전사 결합본에 부분문자열로
    #    등장하는데 어떤 기대 문자열에도 없으면 명세 밖 기관 날조.
    for agency in _KNOWN_AGENCIES:
        if _norm(agency) in combined:
            _collect_extra(agency, expected_combined, extra, seen)

    ok = (not missing) and (not extra) and bool(found)
    return {"ok": ok, "missing": missing, "extra_facts": extra, "texts_found": found}


def _collect_extra(token: str, expected_combined: str, extra: List[str], seen: set) -> None:
    """토큰의 정규화형이 기대 결합본에 없으면 extra_facts 에 추가(중복 제거)."""
    token = (token or "").strip()
    if not token:
        return
    norm = _norm(token)
    if not norm or norm in expected_combined:
        return
    if norm in seen:
        return
    seen.add(norm)
    extra.append(token)


def _default_vision_verifier(image_path: Path, expected_texts: Sequence[str],
                             *, runner: Optional[Callable[..., Any]] = None) -> dict:
    """이미지의 baked 한글을 Claude CLI(Read)로 **전사**시키고, 판정은 코드로 한다.

    claude 는 전사 전용(JSON 강제 없음 — 라이브에서 산문 반환 확인). stdout 을
    _parse_transcription → _judge_transcription 으로 결정론 판정한다.

    Args:
        runner: subprocess.run 주입(테스트용). None 이면 subprocess.run.

    Returns:
        {"ok": bool, "missing": [...], "extra_facts": [...], "texts_found": [...]}.
        비전 미가동(모델 미배선·CLI 부재·타임아웃·호출 실패) 시 ok=False(보수적 미채택).
    """
    path = Path(image_path)
    model = _resolve_vision_model()
    if not model:
        return {"ok": False, "missing": list(expected_texts), "extra_facts": [], "texts_found": [],
                "reason": "vision_backend_unavailable"}
    cmd = _claude_cmd()
    if not cmd or not path.is_file():
        return {"ok": False, "missing": list(expected_texts), "extra_facts": [], "texts_found": [],
                "reason": "claude_cli_or_file_missing"}

    for var in ("CLAUDECODE", "CLAUDE_CODE"):
        os.environ.pop(var, None)
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)

    run = runner or subprocess.run
    prompt = _build_verify_prompt(path.name)
    # ★`--json-schema` 없음(Read 스킵 유발). `--tools Read` 로 이미지 열람만 허용.
    #   stdin=DEVNULL 로 "no stdin data 3s" 경고·지연 제거(전사에 stdin 불필요).
    args = [
        cmd, "-p", prompt,
        "--output-format", "text",
        "--model", model,
        "--tools", "Read",
    ]
    try:
        proc = run(
            args, capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=env, timeout=_VERIFY_TIMEOUT_SEC, cwd=str(path.parent),
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        log.warning(f"[draft_image_fill] 비전 전사 타임아웃 — 미채택: {path.name}")
        return {"ok": False, "missing": [], "extra_facts": [], "texts_found": [], "reason": "timeout"}
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        log.warning(f"[draft_image_fill] 비전 전사 호출 실패({type(exc).__name__}) — 미채택: {path.name}")
        return {"ok": False, "missing": [], "extra_facts": [], "texts_found": [], "reason": "call_failed"}

    if getattr(proc, "returncode", 1) != 0:
        return {"ok": False, "missing": [], "extra_facts": [], "texts_found": [], "reason": "rc"}

    texts_found = _parse_transcription(getattr(proc, "stdout", "") or "")
    result = _judge_transcription(texts_found, expected_texts)
    if result.get("reason") == "unread":
        log.warning(f"[draft_image_fill] 비전이 텍스트를 못 읽음(unread) — 미채택: {path.name}")
    return result


def _safe_verify(verifier: Callable[..., dict], image_path: Path,
                 expected_texts: Sequence[str]) -> dict:
    """검증기 호출을 감싸 예외를 삼킨다. 실패 시 ok=False(보수적 미채택)."""
    try:
        result = verifier(image_path, list(expected_texts))
        if isinstance(result, dict):
            result.setdefault("ok", False)
            return result
        return {"ok": bool(result), "missing": [], "extra_facts": []}
    except Exception as exc:  # noqa: BLE001 — 검증 실패는 미채택으로 안전 처리
        log.warning(f"[draft_image_fill] 검증기 예외({type(exc).__name__}) — 미채택")
        return {"ok": False, "missing": [], "extra_facts": [], "reason": "verifier_exception"}


# ─────────────────────────────────────────────────────────────────────────────
# 기본 젠스파크 엔진(core.genspark_client + playwright 구동) — 주입 가능
# ─────────────────────────────────────────────────────────────────────────────
class _GensparkEngine:
    """genspark_client 저수준 API + playwright 세션을 감싼 이미지 생성 엔진.

    open() → generate()/regenerate() → close() 수명주기. 어떤 메서드도 예외를 전파하지
    않는다(전 경로 fail-open). 테스트는 같은 메서드를 가진 fake 엔진을 주입해 라이브 없이
    검증한다.

    ★Phase 0 실측 제약: 썸네일(1:1)과 카드(세로)는 비율이 달라 반드시 별도 스레드에서
    만들어야 한다(스레드 페이지에선 set_ratio 불가). generate() 는 매번 fresh /ai_image
    페이지를 열어(open_image_tool reuse=False) set_ratio 를 적용하고, regenerate() 는
    직전 generate() 의 스레드 페이지를 이어 써 교정본을 받는다.

    ★카드 배치(2026-08-31 실측 수정): 카드(2:3)는 장마다 fresh /ai_image 를 여는 대신
    open_cards_session() 으로 **한 세션/스레드를 1회만 열고**(set_model/ratio/quality 1회)
    generate_in_session()/regenerate_in_session() 으로 그 한 스레드에 N장을 이어 생성한다.
    → 세션이 (썸네일 1 + 카드 1)로 줄고, 카드들이 한 스레드라 팔레트·스타일이 일관된다
    (앞 이미지가 레퍼런스로 이어져 카드마다 팔레트가 튀던 문제 해소). 카드 교정(재생성)도
    같은 세션 페이지에서 이뤄진다(새 세션 열지 않음).
    """

    def __init__(self, *, out_dir: Path, account: Optional[str] = None,
                 client: Optional[Any] = None) -> None:
        self.out_dir = Path(out_dir)
        self.account = account
        self.last_thread_url: Optional[str] = None
        self._client = client
        self._pw = None
        self._ctx = None
        self._browser = None
        self._last_page = None
        self._last_known: set = set()
        self._cards_page = None          # 카드 배치 전용 세션 페이지(한 스레드)
        self._cards_known: set = set()   # 그 세션에서 이미 본 이미지 URL
        self._login_failed = False

    def _resolve_client(self) -> Any:
        if self._client is None:
            from core import genspark_client as _gs
            self._client = _gs
        return self._client

    def open(self) -> str:
        """세션 준비(playwright→connect→로그인/한도 확인, 필요 시 계정 전환).

        Returns: "ok" | "login_required" | "rate_limited" | "error".
        """
        try:
            gs = self._resolve_client()
        except Exception as exc:  # noqa: BLE001
            log.warning(f"[draft_image_fill] genspark_client 로드 실패: {type(exc).__name__}")
            return "error"
        try:
            from playwright.sync_api import sync_playwright
        except Exception:  # noqa: BLE001 — playwright 미설치
            log.warning("[draft_image_fill] playwright 미설치 — 젠스파크 생성 스킵")
            return "error"
        try:
            self._pw = sync_playwright().start()
        except Exception as exc:  # noqa: BLE001
            log.warning(f"[draft_image_fill] playwright 기동 실패: {type(exc).__name__}")
            return "error"

        auto_switch = bool(_setting("GENSPARK_AUTO_SWITCH", True))
        try:
            order = gs.account_order(prefer=self.account, include_others=auto_switch)
        except Exception:  # noqa: BLE001
            order = []
        if not order:
            order = [self.account]  # 활성 계정(None 이면 load_settings 가 활성 사용)

        saw_rate_limit = False
        for acct in order:
            try:
                st = gs.load_settings(acct) if acct else gs.load_settings()
                self._browser, self._ctx = gs.connect(
                    self._pw, port=st.get("port"), profile=st.get("profile"), autolaunch=True)
                pg = gs.open_image_tool(self._ctx)
            except Exception as exc:  # noqa: BLE001 — 이 계정 연결 실패 → 다음 계정
                log.warning(f"[draft_image_fill] 계정 연결 실패({acct}): {type(exc).__name__}")
                continue
            try:
                if not gs.check_login(pg):
                    self._login_failed = True
                    if auto_switch:
                        continue
                    return "login_required"
                if gs.is_rate_limited(pg):
                    saw_rate_limit = True
                    if auto_switch:
                        continue
                    return "rate_limited"
            except Exception:  # noqa: BLE001 — 상태 감지 실패는 이 계정 스킵
                continue
            self.account = acct or getattr(st, "get", lambda *_: None)("id") or self.account
            log.info(f"[draft_image_fill] 젠스파크 세션 준비 완료(account={self.account})")
            return "ok"

        if saw_rate_limit:
            return "rate_limited"
        if self._login_failed:
            return "login_required"
        return "error"

    def generate(self, prompt: str, *, ratio: str, prefix: str) -> Optional[Path]:
        """fresh /ai_image 스레드에서 1장 생성 후 durable 경로로 다운로드."""
        gs = self._client
        if gs is None or self._ctx is None:
            return None
        try:
            pg = gs.open_image_tool(self._ctx, reuse=False)
            gs.set_model(pg, _MODEL_GPT_IMAGE_2)
            gs.set_ratio(pg, ratio)
            # ★크레딧 소모 방지 — 품질을 '자동'으로(자동=무료, 사용자 확정). fail-open.
            quality = str(_setting("GENSPARK_QUALITY", "자동") or "자동")
            _call_optional(gs, "set_quality", pg, quality)
            known = set(gs.collect_image_urls(pg))
            if not gs.send_prompt(pg, prompt):
                return None
            # 그래도 크레딧 확인창이 뜨면 취소를 눌러 크레딧을 절대 쓰지 않는다(fail-open).
            _call_optional(gs, "_dismiss_credit_confirm", pg)
            gs.wait_for_thread_url(pg)
            url = gs.wait_for_new_image(pg, known)
            if not url:
                return None
            saved = gs.download(self._ctx, [url], self.out_dir, prefix=prefix)
            self._last_page = pg
            self._last_known = set(gs.collect_image_urls(pg))
            self.last_thread_url = getattr(pg, "url", None)
            return saved[0] if saved else None
        except Exception as exc:  # noqa: BLE001 — 생성 실패는 None(호출자 폴백)
            log.warning(f"[draft_image_fill] 생성 실패: {type(exc).__name__}")
            return None

    def regenerate(self, correction_prompt: str, *, prefix: str) -> Optional[Path]:
        """직전 스레드에서 교정 프롬프트로 재생성."""
        gs = self._client
        pg = self._last_page
        if gs is None or pg is None:
            return None
        try:
            known = self._last_known or set(gs.collect_image_urls(pg))
            if not gs.send_prompt(pg, correction_prompt):
                return None
            _call_optional(gs, "_dismiss_credit_confirm", pg)
            url = gs.wait_for_new_image(pg, known)
            if not url:
                return None
            saved = gs.download(self._ctx, [url], self.out_dir, prefix=prefix)
            self._last_known = set(gs.collect_image_urls(pg))
            return saved[0] if saved else None
        except Exception as exc:  # noqa: BLE001
            log.warning(f"[draft_image_fill] 재생성 실패: {type(exc).__name__}")
            return None

    # ── 카드 배치 세션(한 스레드에서 N장) ───────────────────────────────────
    def open_cards_session(self, *, ratio: str) -> bool:
        """카드 전용 세션을 1회만 연다 — fresh /ai_image 1회 + set_model/ratio/quality 1회.

        이후 generate_in_session()/regenerate_in_session() 은 이 한 페이지(첫 전송 시
        /agents 스레드로 전이)를 이어 써 카드 N장을 한 스레드에서 생성한다. 비율은 여기서
        1회만 적용한다(스레드 페이지에선 set_ratio 불가 — Phase 0). fail-open(False 반환).
        """
        gs = self._client
        if gs is None or self._ctx is None:
            return False
        try:
            pg = gs.open_image_tool(self._ctx, reuse=False)
            gs.set_model(pg, _MODEL_GPT_IMAGE_2)
            gs.set_ratio(pg, ratio)
            quality = str(_setting("GENSPARK_QUALITY", "자동") or "자동")
            _call_optional(gs, "set_quality", pg, quality)
            self._cards_page = pg
            self._cards_known = set(gs.collect_image_urls(pg))
            log.info("[draft_image_fill] 카드 배치 세션 오픈(한 스레드에서 N장 생성)")
            return True
        except Exception as exc:  # noqa: BLE001 — 세션 오픈 실패는 False(호출자 폴백)
            log.warning(f"[draft_image_fill] 카드 세션 오픈 실패: {type(exc).__name__}")
            self._cards_page = None
            return False

    def _send_in_cards_session(self, prompt: str, *, prefix: str) -> Optional[Path]:
        """열린 카드 세션(한 스레드)에서 프롬프트 1개 전송 → 새 이미지 1장 다운로드.

        모델·비율은 재적용하지 않는다(open_cards_session 에서 1회 고정). 새 세션을 열지
        않으므로 카드끼리 한 스레드라 팔레트가 일관된다.
        """
        gs = self._client
        pg = self._cards_page
        if gs is None or pg is None:
            return None
        try:
            known = self._cards_known if self._cards_known else set(gs.collect_image_urls(pg))
            if not gs.send_prompt(pg, prompt):
                return None
            _call_optional(gs, "_dismiss_credit_confirm", pg)
            _call_optional(gs, "wait_for_thread_url", pg)  # 첫 전송 시 /agents 스레드 확보
            url = gs.wait_for_new_image(pg, known)
            if not url:
                return None
            saved = gs.download(self._ctx, [url], self.out_dir, prefix=prefix)
            self._cards_known = set(gs.collect_image_urls(pg))
            self.last_thread_url = getattr(pg, "url", None)
            return saved[0] if saved else None
        except Exception as exc:  # noqa: BLE001 — 생성 실패는 None(호출자 폴백)
            log.warning(f"[draft_image_fill] 카드 세션 생성 실패: {type(exc).__name__}")
            return None

    def generate_in_session(self, prompt: str, *, prefix: str) -> Optional[Path]:
        """카드 세션에서 1장 생성(첫/후속 카드 공통)."""
        return self._send_in_cards_session(prompt, prefix=prefix)

    def regenerate_in_session(self, correction_prompt: str, *, prefix: str) -> Optional[Path]:
        """카드 세션(같은 스레드)에서 교정 프롬프트로 재생성 — 새 세션 열지 않음."""
        return self._send_in_cards_session(correction_prompt, prefix=prefix)

    def close(self) -> None:
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:  # noqa: BLE001 — 정리 실패는 무해
            pass
        self._pw = None


# ─────────────────────────────────────────────────────────────────────────────
# 엔진 풀 — 워커마다 **별도 엔진**(세션·임시 작업 디렉터리 분리), 예산·저널은 공유
# ─────────────────────────────────────────────────────────────────────────────
class _EnginePool:
    """제한 병렬 생성을 위한 엔진 풀.

    ★왜 엔진을 워커마다 따로 두는가: 엔진 인스턴스는 **가변 세션 상태**(codex 세션 id,
    임시 작업 디렉터리, 젠스파크 페이지 핸들)를 갖는다. 한 인스턴스를 두 스레드가 함께
    쓰면 세션이 서로 덮여 산출물 귀속이 깨진다(다른 요청의 이미지를 이 글에 싣는 사고).
    그래서 워커 수만큼 엔진을 만들고, **호출 예산과 요청 저널만** 공유한다.

    풀 크기는 `factory` 가 있을 때만 1보다 커진다 — 엔진이 **주입**된 경우(테스트·기존
    호출)에는 우리가 같은 종류의 엔진을 하나 더 만들 방법이 없으므로 순차(1)로 내려간다.
    이 규칙 덕분에 엔진을 주입하는 기존 코드·테스트의 동작이 그대로 유지된다.
    """

    def __init__(self, *, factory: Optional[Callable[[], Any]] = None,
                 primary: Optional[Any] = None, primary_ready: bool = False,
                 size: int = 1, close_primary: bool = True,
                 out_dir: Optional[Path] = None) -> None:
        self._factory = factory
        self._primary = primary
        self._primary_ready = bool(primary_ready)
        self._close_primary = bool(close_primary)
        self._size = max(1, int(size)) if factory is not None else 1
        self._q: "queue.Queue" = queue.Queue()
        self._engines: List[Any] = []
        self._reserved = 0
        self._lock = threading.Lock()
        self._active = 0
        self.max_active = 0
        self.out_dir: Optional[Path] = Path(out_dir) if out_dir is not None else None
        self.open_status: str = ""

    @property
    def opened_count(self) -> int:
        """실제로 열린(살아 있는) 엔진 수 — 배치 요약의 engine_opens 가 읽는다."""
        with self._lock:
            return len(self._engines)

    @property
    def primary(self) -> Optional[Any]:
        """첫 엔진(계정 표기 등 관측용). 아직 열지 않았으면 None 일 수 있다."""
        return self._primary

    # ── 수명주기 ───────────────────────────────────────────────────────────
    def open(self) -> str:
        """첫 엔진(primary)을 확보한다. Returns: "ok"|"login_required"|... |"error"."""
        eng = self._primary
        if eng is None:
            if self._factory is None:
                self.open_status = "error"
                return "error"
            try:
                eng = self._factory()
            except Exception as exc:  # noqa: BLE001 — 생성 실패는 오류로 안전 처리
                log.warning(f"[draft_image_fill] 엔진 생성 실패({type(exc).__name__})")
                self.open_status = "error"
                return "error"
            self._primary = eng
            self._primary_ready = False
        status = "ok" if self._primary_ready else _safe_engine_open(eng)
        self.open_status = status
        if status != "ok":
            # 열지 못한 엔진도 자원을 잡았을 수 있다(임시 디렉터리·브라우저) — 닫는다.
            # 주입 엔진의 정리 책임이 호출자에게 있으면(close_primary=False) 건드리지 않는다.
            if self._close_primary:
                _safe_engine_close(eng)
            return status
        self._apply_out_dir(eng)
        with self._lock:
            self._engines.append(eng)
        self._q.put(eng)
        return "ok"

    def set_out_dir(self, out_dir: Path) -> None:
        """이 글의 durable 저장 경로로 갈아끼운다(배치가 엔진을 여러 글에 재사용)."""
        self.out_dir = Path(out_dir)
        with self._lock:
            engines = list(self._engines)
        for eng in engines:
            self._apply_out_dir(eng)

    def _apply_out_dir(self, engine: Any) -> None:
        if self.out_dir is None or not hasattr(engine, "out_dir"):
            return
        try:
            engine.out_dir = self.out_dir
        except Exception:  # noqa: BLE001 — 교체 실패는 생성 실패로 귀결(무해)
            log.warning("[draft_image_fill] 엔진 out_dir 교체 실패 — 기존 경로로 진행")

    def close(self) -> None:
        with self._lock:
            engines = list(self._engines)
            self._engines = []
        # 큐를 새 객체로 갈아끼우지 않는다 — 혹시 대기 중인 워커가 있으면 영영 깨어나지
        # 못한다. 남은 엔진 핸들만 비워 낸다(close 는 모든 슬롯이 끝난 뒤에만 호출된다).
        while True:
            try:
                self._q.get_nowait()
            except queue.Empty:
                break
        for eng in engines:
            if eng is self._primary and not self._close_primary:
                continue
            _safe_engine_close(eng)

    # ── 대여 ───────────────────────────────────────────────────────────────
    @contextlib.contextmanager
    def lease(self):
        engine = self._acquire()
        with self._lock:
            self._active += 1
            self.max_active = max(self.max_active, self._active)
        try:
            yield engine
        finally:
            with self._lock:
                self._active -= 1
            self._q.put(engine)      # 실패·예외와 무관하게 반드시 반납(누수 금지)

    def _acquire(self) -> Any:
        try:
            engine = self._q.get_nowait()
            self._apply_out_dir(engine)
            return engine
        except queue.Empty:
            pass
        engine = self._grow()
        if engine is not None:
            return engine
        engine = self._q.get()        # 다른 워커가 반납할 때까지 대기
        self._apply_out_dir(engine)
        return engine

    def _grow(self) -> Optional[Any]:
        """상한 안에서 워커 엔진을 하나 더 만든다. 못 만들면 None(기존 엔진 대기)."""
        with self._lock:
            if self._factory is None or (len(self._engines) + self._reserved) >= self._size:
                return None
            self._reserved += 1
        engine = None
        status = "error"
        try:
            engine = self._factory()
            status = _safe_engine_open(engine)
        except Exception as exc:  # noqa: BLE001
            log.warning(f"[draft_image_fill] 워커 엔진 생성 실패({type(exc).__name__})")
        with self._lock:
            self._reserved -= 1
            if status == "ok" and engine is not None:
                self._engines.append(engine)
        if status == "ok" and engine is not None:
            self._apply_out_dir(engine)
            return engine
        if engine is not None:
            _safe_engine_close(engine)
        return None


def _make_pool(*, out_dir: Path, account: Optional[str], engine: Optional[Any],
               engine_ready: bool, engine_factory: Optional[Callable[[], Any]],
               workers: int, close_primary: bool) -> _EnginePool:
    """이 실행에서 쓸 엔진 풀 — 예산·저널은 풀 전체가 공유한다(상한이 곱해지지 않게)."""
    factory = engine_factory
    if factory is None and engine is None:
        shared: Dict[str, Any] = {}
        try:
            from core.codex_image import CallBudget, RequestJournal
            shared["journal"] = RequestJournal()
            shared["budget"] = CallBudget(
                _coerce_int(_setting("CODEX_IMAGE_MAX_CALLS_PER_RUN", 40), 40))
        except Exception:  # noqa: BLE001 — codex 미로드(젠스파크 경로)면 공유 없이 진행
            shared = {}

        def factory() -> Any:  # noqa: D401 — 지역 팩토리
            return _build_engine(out_dir=out_dir, account=account,
                                 journal=shared.get("journal"), budget=shared.get("budget"))

    return _EnginePool(factory=factory, primary=engine, primary_ready=engine_ready,
                       size=workers, close_primary=close_primary, out_dir=out_dir)


# ─────────────────────────────────────────────────────────────────────────────
# 비용 원장 — 슬롯 1개를 실제로 생성하기 직전에만 차감한다(가예약 아님).
# ─────────────────────────────────────────────────────────────────────────────
def _acquire_image_budget() -> bool:
    """`_generate_verified` 가 `engine.generate*` 를 실제로 부르기 직전에 호출한다.
    원장 게이트가 꺼져 있으면(기본값) `cost_ledger.acquire` 가 무조건 True 를 반환해
    기존 동작과 완전히 같다. 재사용(reuse) 경로는 이 함수를 거치지 않는다 — 실제
    생성이 아니라서 차감 대상이 아니다."""
    try:
        from core import cost_ledger as _cl
        return bool(_cl.acquire("image", 1))
    except Exception as exc:  # noqa: BLE001 — 원장 판단 불가 시 생성은 fail-closed
        log.error("[cost_ledger] image 예산 조회 실패(%s) — 생성을 보류합니다",
                  type(exc).__name__)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# 검증 포함 단일 이미지 생성(재생성 루프)
# ─────────────────────────────────────────────────────────────────────────────
def _generate_verified(engine: Any, prompt: str, *, ratio: str, prefix: str,
                       expected_texts: Sequence[str],
                       verifier: Callable[..., dict], max_regen: int,
                       gen_fn: Optional[Callable[[], Optional[Path]]] = None,
                       regen_fn: Optional[Callable[[str, str], Optional[Path]]] = None,
                       verify: bool = True, thumbnail: bool = False
                       ) -> Dict[str, Any]:
    """1장 생성 → 비전 검증 → 실패 시 같은 스레드 교정(≤max_regen). 통과분만 채택.

    gen_fn/regen_fn 을 주면 그 콜러블로 생성/재생성한다(카드 배치 세션 경로 — 한 스레드에
    이어 생성). 안 주면 기본 engine.generate/engine.regenerate(썸네일 = fresh 세션).

    Args:
        gen_fn: 0-인자 생성 콜러블(카드 세션에서 engine.generate_in_session 을 감싼다).
        regen_fn: (correction, prefix) → Path|None 재생성 콜러블(engine.regenerate_in_session).
        verify: False 면 **검증기를 한 번도 호출하지 않고** 생성물을 그대로 채택한다
            (계획 기반 경로의 무문자 본문 이미지 — 읽을 글자가 없어 전사 검증이 무의미하고
            편당 Claude 호출을 없애는 것이 이 인자의 존재 이유다). 파일 유효성·요청 귀속
            검사는 엔진(codex_image) 안에 그대로 남아 있다.

    Returns:
        {"path": Path|None, "regen": int, "verified": bool}. path=None 이면 미채택
        (오타/날조 이미지는 절대 채택하지 않는다). verify=False 면 verified=False 이고
        "unverified": True 가 함께 실린다(정직 표기 — 검증했다고 주장하지 않는다).
    """
    do_gen = gen_fn if gen_fn is not None else (
        lambda: engine.generate(prompt, ratio="1:1" if thumbnail else ratio, prefix=prefix))
    do_regen = regen_fn if regen_fn is not None else (
        lambda corr, pfx: engine.regenerate(corr, prefix=pfx))

    if not _acquire_image_budget():
        log.warning(f"[draft_image_fill] 비용 원장 image 상한 도달 — 생성 생략({prefix})")
        return {"path": None, "regen": 0, "verified": False, "reason": "budget_exhausted"}

    try:
        path = do_gen()
    except Exception as exc:  # noqa: BLE001
        log.warning(f"[draft_image_fill] engine.generate 예외({type(exc).__name__})")
        return {"path": None, "regen": 0, "verified": False}

    if thumbnail:
        path, reason = _normalize_thumbnail_path(path)
        if reason:
            return {"path": None, "regen": 0, "verified": False, "reason": reason}

    if not verify:
        return {"path": Path(path) if path is not None else None,
                "regen": 0, "verified": False, "unverified": True}

    regen = 0
    while True:
        if path is None:
            return {"path": None, "regen": regen, "verified": False}
        vr = _safe_verify(verifier, path, expected_texts)
        if vr.get("ok"):
            return {"path": Path(path), "regen": regen, "verified": True}
        if regen >= max_regen:
            log.warning(
                f"[draft_image_fill] 검증 실패 {regen}회 재생성 후에도 불합격 — 미채택({prefix})")
            return {"path": None, "regen": regen, "verified": False,
                    "reason": "text_verification_failed"}
        correction = _build_correction_prompt(
            expected_texts, vr.get("missing") or [], vr.get("extra_facts") or [])
        if not _acquire_image_budget():
            log.warning("[draft_image_fill] 비용 원장 image 상한 도달 — 재생성 생략(%s)",
                        prefix)
            return {"path": None, "regen": regen, "verified": False,
                    "reason": "budget_exhausted"}
        regen += 1
        try:
            path = do_regen(correction, f"{prefix}_r{regen}")
        except Exception as exc:  # noqa: BLE001
            log.warning(f"[draft_image_fill] engine.regenerate 예외({type(exc).__name__})")
            return {"path": None, "regen": regen, "verified": False}
        if thumbnail:
            path, reason = _normalize_thumbnail_path(path)
            if reason:
                return {"path": None, "regen": regen, "verified": False,
                        "reason": reason}


def _normalize_thumbnail_path(path: Optional[Path]) -> Tuple[Optional[Path], str]:
    """Square a generated thumbnail before OCR or overlay; preserve honest failures."""
    if path is None:
        return None, "thumbnail_generation_empty"
    try:
        from core import thumbnail_compose as tc
        result = tc.normalize_thumbnail_square(path)
    except (ImportError, OSError, TypeError, ValueError) as exc:
        return None, f"thumbnail_normalization_failed:{type(exc).__name__}"
    if not result.get("ok") or not result.get("path"):
        return None, str(result.get("reason") or "thumbnail_normalization_failed")
    return Path(result["path"]), ""


# ─────────────────────────────────────────────────────────────────────────────
# 경로·유틸
# ─────────────────────────────────────────────────────────────────────────────
_SLUG_SAFE_RE = re.compile(r"[^0-9A-Za-z가-힣_\-]+")


def _images_dir_for(slug: str) -> Path:
    """이미지 durable 저장 경로 = runs/{slug}/images (프로필 격리 runs 아래).

    사진 글이 runs/{slug}/photos 를 쓰는 관례와 정합. fail-open 폴백은 하네스 루트.

    ⚠️ 정상 경로는 draft_store 와 동일한 profile 라우터(runs_dir=harness_data_root/runs)를
    쓴다. profile 로드가 실패하는 예외 상황에서만 하네스 루트로 폴백하는데, 이때 이미지
    경로와 draft_store 의 저장 경로가 갈릴 수 있다(둘 다 같은 라우터를 쓰므로 정상 운영엔
    발생하지 않음). 이미지는 image_paths 에 절대경로로 실려 소비되므로 발행 참조엔 무해하다.
    """
    try:
        from config import profile as _profile
        runs_root = _profile.runs_dir()
    except Exception:  # noqa: BLE001 — profile 미로드(극단 상황)만 하네스 루트 폴백
        runs_root = Path(__file__).resolve().parent.parent.parent / "runs"
    return Path(runs_root) / str(slug) / "images"


def _safe_post_id(slug: str) -> str:
    """create_thumbnail(post_id) 용 파일 안전 식별자."""
    cleaned = _SLUG_SAFE_RE.sub("-", str(slug or "draft")).strip("-")
    return cleaned or "draft"


def _now_iso() -> str:
    """timezone-aware ISO 8601(초 단위) — draft_store._now_iso 와 동일 포맷."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ─────────────────────────────────────────────────────────────────────────────
# B1 — 이미지 자산 재사용 판정(존재/유실 분류)
# ─────────────────────────────────────────────────────────────────────────────
def split_existing_images(paths: Optional[Sequence[Any]]) -> Tuple[List[str], List[str]]:
    """post_data.image_paths 를 존재(파일 있음)/유실(파일 없음)로 가른다.

    ⚠️ 판정 불가(stat 자체가 예외를 던지는 권한 문제 등)는 **유실**로 취급한다 —
    "존재함"으로 낙관 처리하면 죽은 참조가 계속 살아남아 이미지 없는 발행이 반복된다
    (D3 — 경로가 사라진 이미지를 유효 자산으로 취급하던 결함의 재발 방지).

    Returns:
        (존재하는 경로 목록, 사라진/판정불가 경로 목록). 둘 다 원래 순서를 보존한다.
    """
    existing: List[str] = []
    missing: List[str] = []
    for raw in (paths or []):
        text = str(raw or "").strip()
        if not text:
            continue
        try:
            is_file = Path(text).is_file()
        except OSError as exc:  # noqa: BLE001 — 판정 불가는 유실로(보수적 처리)
            log.warning(f"[draft_image_fill] 이미지 경로 확인 실패({type(exc).__name__}): {text}")
            missing.append(text)
            continue
        if is_file:
            existing.append(text)
        else:
            missing.append(text)
    return existing, missing


# ─────────────────────────────────────────────────────────────────────────────
# 공개 API 1 — 단건 채움
# ─────────────────────────────────────────────────────────────────────────────
def fill_images_for_draft(
    post_data: dict,
    *,
    slug: str,
    category: Optional[str] = None,
    account: Optional[str] = None,
    genspark: Optional[Any] = None,
    engine: Optional[Any] = None,
    planner: Optional[Callable[..., Any]] = None,
    verifier: Optional[Callable[..., dict]] = None,
    thumb: bool = True,
    cards: bool = True,
    reuse: bool = True,
    replace_unverified: bool = False,
    engine_ready: bool = False,
    close_engine: bool = True,
    engine_factory: Optional[Callable[[], Any]] = None,
    parallel: Optional[int] = None,
    pool: Optional["_EnginePool"] = None,
) -> dict:
    """보관본 1편에 이미지(썸네일 + 본문 이미지)를 채운다.

    2026-09-10 부터 기본은 **계획 경로**(`IMAGE_FILL_PLAN_MODE=True`)다 — 집필 때 저장해 둔
    `post_data["image_plan"]` 대로 생성만 하고(기획 LLM 0회), 본문 이미지는 글자가 없어
    비전 전사 검증을 하지 않으며(썸네일 라벨만 최소 검증), 슬롯 단위로 제한 병렬 생성한다.
    `IMAGE_FILL_PLAN_MODE=False` 면 아래 레거시 인포카드 경로(planner + 카드마다 비전)를 탄다.
    `planner` 인자는 레거시 경로 전용이다(계획 경로에서는 호출되지 않는다).

    Args:
        post_data: {"title","content"(마크다운/HTML),"category",...}. 성공 시 image_paths·
            _force_image_insert 를 이 dict 에 세팅한다(저장/발행은 호출자 몫).
        slug: 보관본 슬러그(이미지 durable 경로 runs/{slug}/images 에 사용).
        category: 카테고리(문맥용). None 이면 post_data.category 또는 "default".
        account: 젠스파크 엔진일 때 쓸 계정 이메일. None 이면 활성 계정(codex 는 무시).
        genspark: (레거시 이름) 주입 엔진. `engine` 과 같은 뜻 — 기존 호출·테스트 호환용.
        engine: 주입 엔진. 없으면 IMAGE_FILL_ENGINE 설정에 따라 CodexImageEngine(기본)
            또는 _GensparkEngine 을 만든다. `out_dir` 속성이 있으면 이 글의 durable 경로
            (runs/{slug}/images)로 갈아끼운다(배치가 엔진 하나를 여러 글에 재사용할 때 필수).
        planner: Stage1 기획(기본: image_prompt_builder.plan_infocards).
        verifier: 비전 검증(기본: 내부 Claude CLI Read).
        thumb: 썸네일 생성 여부.
        cards: 카드뉴스 생성 여부.
        reuse: True(기본, B1)면 post_data 의 기존 image_paths 중 **실재하는 파일만**
            재사용한다 — 유효 썸네일이 있으면 재생성을 건너뛰고, 카드는 예산에서 재사용분을
            뺀 만큼만 만든다. 유실(파일 없는) 경로는 결과에서 제거된다(meta.dropped_missing).
            False 면 예전처럼 파일 존재를 검사하지 않고 기존 image_paths 를 그대로 뒤에
            이어붙인다(레거시 무조건 보존 — 하위호환 전용 경로).
        engine_ready: True 면 `engine.open()` 을 다시 부르지 않는다(배치가 이미 열었다).
        close_engine: False 면 `engine.close()` 를 부르지 않는다(배치가 마지막에 닫는다).
        engine_factory: 워커 엔진을 더 만들 때 쓰는 0-인자 팩토리(계획 경로 전용).
            **엔진을 주입하면서 팩토리를 주지 않으면 병렬은 1로 내려간다** — 같은 종류의
            엔진을 하나 더 만들 방법이 없기 때문이다(주입 호출·기존 테스트의 동작 보존).
        parallel: 한 편 안에서 동시에 생성할 슬롯 수(기본 설정값 2, 1이면 순차, 상한 4).
        pool: 배치가 만들어 둔 엔진 풀(내부 사용). 주면 engine/engine_factory 대신 쓴다.

    Returns:
        {"image_paths": [str...], "image_meta": {..., "reused": int, "generated": int,
        "dropped_missing": int}}. IMAGE_FILL_ENABLED False 이거나 본문이 비면 image_paths=[]
        로 no-op(post_data 무변경). 전 경로 fail-open.

    ★기존 이미지 보존: reuse=True(기본)에서는 실재하는 기존 이미지만 재사용·보존하고
      유실된 경로는 조용히 버리지 않고 카운트한다(D3). 생성이 전부 실패해도 재사용 후보로
      확인된 유효 이미지는 그대로 남는다 — post_data["image_paths"] 를 비우지 않는다.
    """
    meta: Dict[str, Any] = {"account": account, "verified": 0, "regen_count": 0,
                            "engine": _image_fill_engine(),
                            "reused": 0, "generated": 0, "dropped_missing": 0}

    if not _image_fill_enabled():
        meta["skipped"] = "image_fill_disabled"
        return {"image_paths": [], "image_meta": meta}
    # 젠스파크는 이제 옵트인 레거시 엔진 — 자체 게이트가 꺼져 있으면 브라우저를 띄우지 않는다.
    if meta["engine"] == "genspark" and not _genspark_enabled():
        meta["skipped"] = "genspark_disabled"
        return {"image_paths": [], "image_meta": meta}

    if not isinstance(post_data, dict):
        meta["skipped"] = "bad_post_data"
        return {"image_paths": [], "image_meta": meta}

    title = str(post_data.get("title") or "").strip()
    article_text = _to_plain_text(post_data.get("content"))
    category = str(category or post_data.get("category") or "default").strip() or "default"

    if not article_text or not title:
        meta["skipped"] = "no_article" if not article_text else "no_title"
        return {"image_paths": [], "image_meta": meta}

    plan_fn = planner or _default_planner
    verify_fn = verifier or _default_vision_verifier
    max_regen = _coerce_int(_setting("GENSPARK_VERIFY_MAX_REGEN", 2), 2)
    budget = _coerce_int(_fill_setting("IMAGE_FILL_CARDS", "GENSPARK_INFOCARD_BUDGET", 3), 3)
    # Per-account legacy ratio preferences are retained but never affect generated thumbs.
    thumb_ratio = "1:1"
    card_ratio = str(_fill_setting("IMAGE_FILL_CARD_RATIO", "GENSPARK_CARD_RATIO", "2:3") or "2:3")

    out_dir = _images_dir_for(slug)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.warning(f"[draft_image_fill] 이미지 폴더 생성 실패({exc}) — 스킵")
        meta["skipped"] = "outdir_failed"
        return {"image_paths": [], "image_meta": meta}

    # ── 계획 기반 경로(2026-09-10 기본) ────────────────────────────────────
    # 저장된 계획을 그대로 쓰고, 없으면 **로컬 결정론 계획**을 한 번 만들어 저장한다.
    # 기획 LLM 호출 0 · 본문 비전 호출 0(썸네일 라벨만 최소 검증).
    if _plan_mode_enabled():
        return _fill_with_plan(
            post_data, slug=slug, title=title, article_text=article_text, category=category,
            out_dir=out_dir, meta=meta, account=account, engine=engine,
            engine_ready=engine_ready, close_engine=close_engine,
            engine_factory=engine_factory, pool=pool, parallel=parallel,
            verifier=verify_fn, thumb=thumb, cards=cards, reuse=reuse,
            replace_unverified=replace_unverified,
            body_count=budget, thumb_ratio=thumb_ratio, body_ratio=card_ratio,
            max_regen=max_regen)

    # ── B1: 기존 이미지 재사용 판정 ────────────────────────────────────────
    # 실재하는 기존 이미지는 재사용(첫 장=썸네일로 간주 → 썸네일 재생성 skip, 나머지는
    # 카드로 간주해 예산에서 차감), 유실 경로는 결과에서 제거하고 카운트만 남긴다(D2·D3).
    existing_raw = [str(p) for p in (post_data.get("image_paths") or []) if str(p).strip()]
    valid_existing: List[str] = []
    missing_existing: List[str] = []
    skip_thumb_gen = False
    reused_thumb_path: Optional[Path] = None
    reused_card_paths: List[Path] = []
    gen_card_budget = budget
    if reuse and existing_raw:
        valid_existing, missing_existing = split_existing_images(existing_raw)
        if missing_existing:
            meta["dropped_missing"] = len(missing_existing)
        if valid_existing:
            reused_thumb_path = Path(valid_existing[0])
            reused_card_paths = [Path(p) for p in valid_existing[1:]]
            skip_thumb_gen = True
            gen_card_budget = max(0, budget - len(reused_card_paths))
            meta["reused"] = len(valid_existing)

    # Stage1 — 카드 구성안(빈 cards 는 폴백 신호). 카드 비활성/재사용으로 예산 소진이면 스킵.
    # ★C1(YMYL): plan_infocards 는 본문에 없는 수치·날짜를 환각할 수 있다(예: "만 19~34세"
    #   → "19~39세"). 비전 검증은 이미지↔스펙만 대조하므로 환각을 못 잡는다. 따라서
    #   같은 레포 image_prompt_builder.validate_image_plan(fact-grounding — 카드 텍스트의
    #   단위 있는 숫자·날짜·%·나이·기간 토큰이 본문에 실제로 있어야 통과)을 프롬프트 빌드
    #   전에 반드시 호출해, 근거 없는 카드는 제외하고 grounded 카드만 생성한다.
    raw_card_count = 0
    card_list: List[Any] = []
    if cards and gen_card_budget > 0:   # 예산 0 = 재사용으로 충분하거나 사용자 설정
        plan = None
        try:
            plan = plan_fn(article_text=article_text, title=title, category=category,
                           card_budget=gen_card_budget)
        except Exception as exc:  # noqa: BLE001 — 기획 실패는 카드 없이 진행(썸네일만)
            log.warning(f"[draft_image_fill] 카드 기획 실패({type(exc).__name__}) — 썸네일만")
        raw_cards = list(getattr(plan, "cards", []) or []) if plan is not None else []
        raw_card_count = len(raw_cards)
        if raw_cards:
            card_list = _ground_cards(plan, article_text=article_text, card_budget=gen_card_budget)
    meta["cards_planned"] = raw_card_count
    meta["cards_grounded"] = len(card_list)
    if raw_card_count and len(card_list) < raw_card_count:
        meta["cards_flagged"] = raw_card_count - len(card_list)

    # 엔진 준비 — 주입 > 설정(codex 기본). 두 엔진은 같은 메서드 계약을 구현한다.
    # 주입 엔진에 out_dir 속성이 있으면 이 글의 durable 경로로 갈아끼운다(B3 — 배치가
    # 엔진 하나를 여러 슬러그에 재사용할 때, 매 글마다 올바른 저장 폴더를 쓰기 위해 필수).
    injected = engine if engine is not None else genspark
    if injected is not None:
        engine = injected
        if hasattr(engine, "out_dir"):
            try:
                engine.out_dir = out_dir
            except Exception:  # noqa: BLE001 — out_dir 갈아끼우기 실패는 무해(생성 실패로 귀결)
                log.warning("[draft_image_fill] 주입 엔진 out_dir 교체 실패 — 기존 경로로 진행")
    else:
        engine = _build_engine(out_dir=out_dir, account=account)
    thumb_path: Optional[Path] = None
    card_paths: List[Path] = []
    total_regen = 0
    try:
        status = "ok" if engine_ready else _safe_engine_open(engine)
        if status != "ok":
            # 미로그인/한도/오류 → fail-open 빈 결과(발행 절대 금지).
            meta["skipped"] = status
            return {"image_paths": [], "image_meta": meta}
        meta["account"] = getattr(engine, "account", account)

        # 썸네일(1:1) — thumb=False 면 아예 관여하지 않는다(재사용도 포함).
        if thumb and skip_thumb_gen:
            thumb_path = reused_thumb_path
        elif thumb and not skip_thumb_gen:
            from core.image_plan import thumb_label_from_title
            res = _generate_verified(
                engine, _build_thumbnail_prompt(title), ratio="1:1", prefix="thumb",
                expected_texts=[thumb_label_from_title(title)], verifier=verify_fn,
                max_regen=0, thumbnail=True)
            total_regen += res.get("regen", 0)
            if res.get("path"):
                thumb_path = res["path"]
                meta["verified"] = int(meta["verified"]) + 1
                meta["generated"] = int(meta["generated"]) + 1
                _mark_generated(thumb_path, slug=slug, producer="draft_image_fill:thumb")
            else:
                meta["thumb_baked_failed"] = str(res.get("reason") or "text_verification_failed")
            meta["thread_url_thumb"] = getattr(engine, "last_thread_url", None)

        # 카드(세로) — ★한 세션/스레드에서 배치 생성(팔레트 일관 + 세션 1개).
        #   장마다 fresh /ai_image 를 열던 걸(카드마다 새 세션·팔레트 튐) open_cards_session
        #   1회로 바꾼다. 엔진이 세션 API 를 지원할 때만(하위호환: 없으면 legacy fresh 경로).
        if card_list:
            supports_session = (callable(getattr(engine, "open_cards_session", None))
                                and callable(getattr(engine, "generate_in_session", None))
                                and callable(getattr(engine, "regenerate_in_session", None)))
            session_ok = False
            if supports_session:
                session_ok = _safe_open_cards_session(engine, ratio=card_ratio)
                meta["cards_session"] = "opened" if session_ok else "open_failed"
            else:
                meta["cards_session"] = "legacy_per_card"

            for idx, card in enumerate(card_list, start=1):
                expected = _card_expected_texts(card)
                if not expected:
                    continue
                card_prefix = f"card_{idx:02d}"
                prompt = _build_card_prompt(card, title=title, category=category)
                if session_ok:
                    gen_fn = (lambda p=prompt, pf=card_prefix:
                              engine.generate_in_session(p, prefix=pf))
                    regen_fn = (lambda corr, pfx: engine.regenerate_in_session(corr, prefix=pfx))
                else:
                    gen_fn = None      # 세션 미지원/오픈 실패 → legacy fresh 경로(fail-open)
                    regen_fn = None
                res = _generate_verified(
                    engine, prompt, ratio=card_ratio, prefix=card_prefix,
                    expected_texts=expected, verifier=verify_fn, max_regen=max_regen,
                    gen_fn=gen_fn, regen_fn=regen_fn)
                total_regen += res.get("regen", 0)
                if res.get("path"):
                    card_paths.append(res["path"])
                    meta["verified"] = int(meta["verified"]) + 1
                    meta["generated"] = int(meta["generated"]) + 1
                    _mark_generated(res["path"], slug=slug,
                                    producer="draft_image_fill:card")
            meta["thread_url_cards"] = getattr(engine, "last_thread_url", None)

        # Legacy cards contain text and cannot stand in as a body photograph. Once that
        # work is done, make at most one clean background call through the same budget.
        if (thumb and thumb_path is None and not skip_thumb_gen
                and _thumbnail_compose_ready()):
            from core import image_plan as ip
            message = _thumbnail_fallback_message(title)
            temporary_plan = ip.local_plan(title=title, content=article_text,
                                           category=category, body_count=0, label=message)
            temporary_thumb = next(
                slot for slot in temporary_plan["slots"]
                if slot.get("role") == ip.ROLE_THUMBNAIL)
            legacy_pool = _EnginePool(primary=engine, primary_ready=True,
                                      close_primary=False, out_dir=out_dir)
            if legacy_pool.open() == "ok":
                background_result = _generate_photo_background(
                    legacy_pool, temporary_thumb, title=title, category=category,
                    slug=slug, out_dir=out_dir, verifier=verify_fn,
                    cta="내용 확인", date_label=_seoul_month())
                if background_result.get("path"):
                    thumb_path = Path(background_result["path"])
                    meta["generated"] = int(meta["generated"]) + 1
                    meta["thumb_photo_fallback"] = {
                        "source": "generated_background",
                        "background": Path(str(background_result.get("background") or "")).name,
                        "lines": list((background_result.get("compose") or {}).get("lines") or []),
                        "font": Path(str((background_result.get("compose") or {}).get("font") or "")).name,
                    }
                else:
                    meta["thumb_baked_failed"] = str(background_result.get("reason") or "no_image")
            else:
                meta["thumb_baked_failed"] = "engine_unavailable_for_background"
    except Exception as exc:  # noqa: BLE001 — 어떤 실패도 전파 금지
        log.warning(f"[draft_image_fill] fill 흐름 예외({type(exc).__name__}) — 부분 결과 반환")
    finally:
        if close_engine:
            _safe_engine_close(engine)

    meta["regen_count"] = total_regen
    meta["cards_verified"] = len(card_paths)

    ordered: List[Path] = []
    if thumb_path is not None:
        ordered.append(thumb_path)
    ordered.extend(reused_card_paths)
    ordered.extend(card_paths)
    image_paths = [str(p) for p in ordered]

    if reuse:
        # B1 — 유실 경로는 이미 dropped_missing 으로 카운트됐고 ordered 에 실리지 않는다.
        #   유효 재사용분은 ordered 에 이미 포함돼 있으므로 그대로 저장한다.
        if image_paths:
            post_data["image_paths"] = image_paths
            post_data["_force_image_insert"] = True
        else:
            meta.setdefault("skipped", "no_verified_images")
            if valid_existing:
                # 생성이 전부 실패해도 재사용 후보(유효 기존)는 그대로 보존한다 — 비우지 않는다.
                post_data["image_paths"] = list(valid_existing)
                post_data["_force_image_insert"] = True
                image_paths = list(valid_existing)
    else:
        # ★레거시 무조건 보존(하위호환, reuse=False 전용) — 파일 존재를 검사하지 않고
        #   post_data 에 이미 실려 있던 이미지를 뒤에 이어 붙인다(예전 동작 그대로).
        if image_paths:
            merged = list(image_paths)
            for prev in existing_raw:
                if prev not in merged:
                    merged.append(prev)
            if existing_raw:
                meta["preserved_existing"] = len(existing_raw)
            post_data["image_paths"] = merged
            post_data["_force_image_insert"] = True
            image_paths = merged
        else:
            meta.setdefault("skipped", "no_verified_images")
            if existing_raw:
                # 생성 실패 — 기존 이미지는 그대로 둔다(무변경).
                meta["preserved_existing"] = len(existing_raw)

    return {"image_paths": image_paths, "image_meta": meta}


# ─────────────────────────────────────────────────────────────────────────────
# 계획 기반 채움(2026-09-10) — 기획 0회, 본문 비전 0회, 슬롯 단위 제한 병렬
# ─────────────────────────────────────────────────────────────────────────────
def _fill_with_plan(
    post_data: dict, *, slug: str, title: str, article_text: str, category: str,
    out_dir: Path, meta: Dict[str, Any], account: Optional[str], engine: Optional[Any],
    engine_ready: bool, close_engine: bool, engine_factory: Optional[Callable[[], Any]],
    pool: Optional[_EnginePool], parallel: Optional[int],
    verifier: Callable[..., dict], thumb: bool, cards: bool, reuse: bool,
    replace_unverified: bool,
    body_count: int, thumb_ratio: str, body_ratio: str, max_regen: int,
) -> dict:
    """저장된 계획대로 **생성만** 한다 — 기획 LLM 0회, 본문 비전(OCR) 0회.

    · 계획이 없거나 망가졌으면 로컬 결정론 계획을 **한 번** 만들어 post_data 에 실어
      돌려준다(호출자가 저장). LLM 재기획 폴백은 존재하지 않는다.
    · 슬롯 단위로 제한 병렬 생성한다(워커마다 별도 엔진 — 세션 공유 금지).
    · 부분 실패해도 성공한 슬롯은 즉시 결과에 실린다. 슬롯 상태(`image_slots`)를 남겨
      다음 재시도가 **빠진 슬롯만** 다시 만들게 한다(중복 생성 방지).
    """
    from core import image_plan as ip

    existing_raw = [str(p) for p in (post_data.get("image_paths") or []) if str(p).strip()]

    # 편집 브리프의 썸네일 메시지를 계획에 전달한다 — 계획이 없어 새로 만들 때도 본문이
    # 답하는 질문과 같은 말이 썸네일에 얹힌다(요건 2). 브리프가 없으면 제목 기반 폴백.
    brief_message = ""
    brief: Dict[str, Any] = {}
    try:
        from core import editorial_brief as _eb
        brief = _eb.ensure_brief(post_data, title=title, category=category)
        brief_message = str(brief.get("thumbnail_message") or "").strip()
        meta["brief_source"] = brief.get("source")
    except Exception as exc:  # noqa: BLE001 — 브리프 조회 실패는 제목 폴백
        log.debug(f"[draft_image_fill] 편집 브리프 조회 실패({type(exc).__name__})")

    preserve_completed_thumbnail = _has_verified_mapped_thumbnail(post_data, slug=slug)
    plan, changed = ip.ensure_plan(
        post_data, title=title, content=post_data.get("content"), category=category,
        body_count=max(0, _coerce_int(body_count, ip.DEFAULT_BODY_COUNT)),
        thumb_ratio=thumb_ratio, body_ratio=body_ratio,
        reserved_slot_ids=list((post_data.get("image_slots") or {}).keys())
        if isinstance(post_data.get("image_slots"), dict) else (),
        preserve_completed_thumbnail=preserve_completed_thumbnail,
        message=brief_message or None)
    post_data["image_plan"] = plan
    meta["plan_source"] = plan.get("source")
    meta["plan_saved"] = bool(changed)
    meta["plan"] = ip.plan_summary(plan)

    # ── 기존 자산 재사용(슬롯 map 우선, 없으면 레거시 image_paths 최소 호환 매핑) ──
    slot_map: Dict[str, str] = {}
    extras: List[str] = []
    # replace_unverified 로 떼어 낸 원본 — 재생성 실패 시 되돌릴 안전망.
    replaced_originals: Dict[str, str] = {}
    if reuse:
        slot_map, missing, extras = ip.slot_assets(plan, post_data)
        if missing:
            meta["dropped_missing"] = len(missing)
        if replace_unverified:
            # 사용자가 **이 글을 골라** 명시적으로 요청했을 때만 — 발행 게이트가 막을
            # 자산(출처 미확인·참고 캡처 등)을 재사용 목록에서 떼어 그 슬롯을 다시
            # 만들게 한다. 기본값 False 이므로 일괄 자동 재생성은 일어나지 않는다.
            dropped = _unverified_slot_ids(slots_of_plan(plan), slot_map)
            if dropped:
                for sid in dropped:
                    # ★ 원본 경로를 **기억**해 둔다. 새로 만들기에 실패하면 되돌린다 —
                    #   실패가 기존 자산까지 날려 글이 빈손이 되는 것을 막는다.
                    #   (파일 자체는 지우지 않는다. 여기서 떼는 것은 '슬롯 연결'뿐.)
                    replaced_originals[sid] = slot_map.pop(sid)
                meta["replaced_unverified"] = len(dropped)
    if thumb:
        thumb_slot = next((slot for slot in plan.get("slots", [])
                           if isinstance(slot, dict)
                           and slot.get("role") == ip.ROLE_THUMBNAIL), None)
        thumb_id = str((thumb_slot or {}).get("id") or "")
        if thumb_id and thumb_id not in slot_map:
            try:
                message = _eb.thumbnail_message(brief, title=title)
                if ip.refresh_thumbnail_message(plan, title=title, message=message):
                    post_data["image_plan"] = plan
                    meta["plan_saved"] = True
                    meta["plan"] = ip.plan_summary(plan)
            except Exception as exc:  # noqa: BLE001 — message refresh stays fail-open
                log.debug(f"[draft_image_fill] 미완 썸네일 문구 갱신 실패({type(exc).__name__})")
        meta["reused"] = len(slot_map) + len(extras)

    slots = [s for s in (plan.get("slots") or []) if isinstance(s, dict)]
    todo: List[Dict[str, Any]] = []
    for slot in slots:
        role = slot.get("role")
        if role == ip.ROLE_THUMBNAIL and not thumb:
            continue
        if role == ip.ROLE_BODY and not cards:
            continue
        if slot.get("id") in slot_map:
            continue                      # 이미 유효 자산이 붙어 있다 — 재생성 금지
        todo.append(slot)
    meta["slots_total"] = len(slots)
    meta["slots_todo"] = len(todo)

    workers = _parallel_workers(parallel)
    owns_pool = pool is None
    if owns_pool:
        pool = _make_pool(out_dir=out_dir, account=account, engine=engine,
                          engine_ready=engine_ready, engine_factory=engine_factory,
                          workers=workers, close_primary=close_engine)
    else:
        pool.set_out_dir(out_dir)

    failures: Dict[str, str] = {}
    total_regen = 0
    generated = 0
    try:
        if todo:
            status = "ok" if not owns_pool else pool.open()
            if status != "ok":
                meta["skipped"] = status
                return {"image_paths": [], "image_meta": meta}
            meta["account"] = getattr(pool.primary, "account", account)
            results = _run_slot_tasks(
                todo, pool=pool, verifier=verifier, max_regen=max_regen,
                workers=workers, category=category,
                cta=_thumbnail_cta(brief), date_label=_seoul_month())
            for slot_id, res in results.items():
                total_regen += _coerce_int(res.get("regen"), 0)
                path = res.get("path")
                if res.get("composed"):
                    # 합성 성공 — 무엇을 얹었는지 남긴다(관측·사후 검증용, 경로는 상대명만).
                    compose_meta = res.get("compose") or {}
                    meta["thumb_composed"] = {
                        "lines": list(compose_meta.get("lines") or []),
                        "font_size": compose_meta.get("font_size"),
                        "font": Path(str(compose_meta.get("font") or "")).name,
                        "background": Path(str(res.get("background") or "")).name,
                    }
                elif res.get("compose"):
                    meta["thumb_compose_failed"] = str((res.get("compose") or {}).get("reason") or "")
                if path:
                    slot_map[slot_id] = str(path)
                    generated += 1
                    # 슬롯 생성이 실제로 성공한 그 파일에만 출처를 남긴다.
                    _mark_generated(path, slug=slug,
                                    producer=f"draft_image_fill:slot:{slot_id}")
                    if res.get("verified"):
                        meta["verified"] = int(meta.get("verified") or 0) + 1
                else:
                    failures[slot_id] = str(res.get("reason") or "no_image")
            # A baked-text thumbnail that fails OCR stays pending until every body result is
            # collected. Reuse only a generated, SHA-bound body slot from this same slug.
            thumb_slot = next((slot for slot in slots
                               if slot.get("role") == ip.ROLE_THUMBNAIL), None)
            thumb_id = str((thumb_slot or {}).get("id") or "")
            if (thumb and thumb_slot is not None and thumb_id in failures
                    and failures[thumb_id] != "budget_exhausted"):
                photo_result = _compose_from_body_slots(
                    plan, slot_map, thumb_slot, slug=slug, out_dir=out_dir,
                    category=category, cta=_thumbnail_cta(brief),
                    date_label=_seoul_month())
                if photo_result.get("ok"):
                    slot_map[thumb_id] = str(photo_result["path"])
                    generated += 1
                    failures.pop(thumb_id, None)
                    meta["thumb_photo_fallback"] = {
                        "source_body_slot": photo_result.get("source_body_slot"),
                        "source_sha256": photo_result.get("source_sha256"),
                        "background": Path(str(photo_result.get("background") or "")).name,
                        "lines": list((photo_result.get("compose") or {}).get("lines") or []),
                        "font": Path(str((photo_result.get("compose") or {}).get("font") or "")).name,
                    }
                elif photo_result.get("source_verified"):
                    failures[thumb_id] = f"thumb_compose:{photo_result.get('reason')}"
                else:
                    background_result = {}
                    if _thumbnail_compose_ready():
                        background_result = _generate_photo_background(
                            pool, thumb_slot, title=title, category=category, slug=slug,
                            out_dir=out_dir, verifier=verifier, cta=_thumbnail_cta(brief),
                            date_label=_seoul_month())
                    else:
                        failures[thumb_id] = "thumb_compose:font_missing"
                    if background_result.get("path"):
                        slot_map[thumb_id] = str(background_result["path"])
                        generated += 1
                        failures.pop(thumb_id, None)
                        meta["thumb_photo_fallback"] = {
                            "source": "generated_background",
                            "background": Path(str(background_result.get("background") or "")).name,
                            "lines": list((background_result.get("compose") or {}).get("lines") or []),
                            "font": Path(str((background_result.get("compose") or {}).get("font") or "")).name,
                        }
                    elif background_result:
                        failures[thumb_id] = str(background_result.get("reason") or "no_image")
            meta["max_active"] = pool.max_active
    except Exception as exc:  # noqa: BLE001 — 어떤 실패도 전파 금지(부분 결과 반환)
        log.warning(f"[draft_image_fill] 계획 채움 예외({type(exc).__name__}) — 부분 결과")
    finally:
        if owns_pool and close_engine:
            pool.close()

    # 사진 배경 또는 글자 합성에 실패하면 썸네일은 미완으로 보존한다.
    thumb_slot = next((s for s in slots if s.get("role") == ip.ROLE_THUMBNAIL), None)
    thumb_id = str((thumb_slot or {}).get("id") or "")
    if thumb_id in failures and "font_missing" in str(failures.get(thumb_id, "")):
        meta["thumb_fallback_skipped"] = "font_missing"

    # ★ 교체 실패분 원상 복구 — 새로 만들지 못한 슬롯에는 **원래 자산을 되돌린다.**
    #   되돌린 자산은 여전히 출처 미확인이라 발행은 계속 막히지만(판정은 그대로),
    #   "다시 만들기를 눌렀다가 있던 이미지까지 사라지는" 손실은 일어나지 않는다.
    if replaced_originals:
        restored = [sid for sid in replaced_originals if sid not in slot_map]
        for sid in restored:
            slot_map[sid] = replaced_originals[sid]
        if restored:
            meta["replace_unverified_restored"] = len(restored)
            log.info("[draft_image_fill] 교체 실패 슬롯 %d개 — 기존 이미지를 그대로 둡니다"
                     "(발행은 계속 보류)", len(restored))
        meta["replace_unverified_done"] = len(replaced_originals) - len(restored)

    meta["generated"] = generated
    meta["regen_count"] = total_regen
    meta["parallel"] = workers
    meta["slots_filled"] = len(slot_map)
    meta["slots_failed"] = len(failures)
    if failures:
        meta["failed_slots"] = dict(failures)

    image_paths = ip.ordered_paths(plan, slot_map, extras)
    if not reuse:
        # 레거시 무조건 보존(하위호환 전용) — 파일 존재를 검사하지 않고 기존 경로를 뒤에.
        for prev in existing_raw:
            if prev not in image_paths:
                image_paths.append(prev)
        if existing_raw:
            meta["preserved_existing"] = len(existing_raw)

    if image_paths:
        post_data["image_paths"] = image_paths
        post_data["image_slots"] = dict(slot_map)
        post_data["_force_image_insert"] = True
    else:
        meta.setdefault("skipped", "no_verified_images")

    return {"image_paths": image_paths, "image_meta": meta}


class _SlotRunner:
    """슬롯 하나를 엔진 하나로 생성하는 실행기(스레드에서 호출된다).

    같은 엔진으로 본문 슬롯을 여러 장 만들 때는 그 엔진의 배치 세션을 **한 번만** 열어
    이어 쓴다(팔레트 일관). 세션은 엔진 인스턴스에 딸린 가변 상태라 엔진 밖으로 새지
    않는다 — 워커가 서로 다른 엔진을 쓰므로 세션이 겹치지 않는다.
    """

    def __init__(self, *, verifier: Callable[..., dict], max_regen: int,
                 body_ratio_default: str = "2:3", category: str = "",
                 cta: str = "", date_label: str = "") -> None:
        self._verifier = verifier
        self._max_regen = max(0, int(max_regen or 0))
        self._sessions: set = set()
        self._lock = threading.Lock()
        self._body_ratio_default = body_ratio_default
        self._category = category
        self._cta = cta
        self._date_label = date_label

    def _ensure_session(self, engine: Any, ratio: str) -> bool:
        """이 엔진의 본문 배치 세션을 1회만 연다. 지원 안 하면 False(단건 경로)."""
        if not (callable(getattr(engine, "open_cards_session", None))
                and callable(getattr(engine, "generate_in_session", None))):
            return False
        key = id(engine)
        with self._lock:
            if key in self._sessions:
                return True
        ok = _safe_open_cards_session(engine, ratio=ratio)
        if ok:
            with self._lock:
                self._sessions.add(key)
        return ok

    def run(self, engine: Any, slot: Dict[str, Any]) -> Dict[str, Any]:
        from core import image_plan as ip
        slot_id = str(slot.get("id") or "slot")
        prompt = str(slot.get("prompt") or "")
        is_thumbnail = slot.get("role") == ip.ROLE_THUMBNAIL
        ratio = "1:1" if is_thumbnail else str(slot.get("ratio") or self._body_ratio_default)
        expected = ip.expected_texts(slot)
        # 썸네일(짧은 라벨)만 기존 비전 검증을 최소 적용. 본문은 읽을 글자가 없어 검증 없음.
        # ★ 로컬 합성 썸네일(overlay)은 모델이 글자를 안 쓰므로 expected 가 비어 있다 →
        #   비전 검증 0회. 철자 정확성은 우리가 그리는 것으로 보장한다.
        do_verify = bool(expected) and _verify_thumb_enabled()

        gen_fn = regen_fn = None
        if slot.get("role") == ip.ROLE_BODY and self._ensure_session(engine, ratio):
            gen_fn = (lambda p=prompt, pf=slot_id:
                      engine.generate_in_session(p, prefix=pf))
            regen_fn = (lambda corr, pfx: engine.regenerate_in_session(corr, prefix=pfx))

        res = _generate_verified(
            engine, prompt, ratio=ratio, prefix=slot_id, expected_texts=expected,
            verifier=self._verifier, max_regen=self._max_regen if do_verify else 0,
            gen_fn=gen_fn, regen_fn=regen_fn, verify=do_verify,
            thumbnail=is_thumbnail)
        if not res.get("path"):
            res.setdefault("reason", getattr(engine, "last_reason", "") or "no_image")
            return res
        render_slot = dict(slot)
        render_slot["_render_category"] = self._category
        render_slot["_render_cta"] = self._cta
        render_slot["_render_date_label"] = self._date_label
        res = _apply_overlay(res, render_slot)
        if is_thumbnail and res.get("path"):
            path, reason = _normalize_thumbnail_path(Path(res["path"]))
            if reason:
                return {**res, "path": None, "verified": False, "reason": reason}
            res["path"] = path
        return res


def _thumb_note() -> str:
    """합성 썸네일 하단에 넣을 보조 문구(브랜드 표기). 설정이 없으면 빈 문자열."""
    try:
        from config import brand as _brand
        return str(_brand.thumbnail_logo() or "").strip()[:40]
    except Exception:  # noqa: BLE001 — 브랜드팩 미로드면 표기 없음
        return ""


def _apply_overlay(res: Dict[str, Any], slot: Dict[str, Any]) -> Dict[str, Any]:
    """썸네일 슬롯이면 생성된 **배경** 위에 한글을 로컬 합성해 그 결과를 슬롯 자산으로 삼는다.

    · 합성 대상이 아니면(본문 슬롯·레거시 baked 썸네일) 입력을 그대로 돌려준다.
    · 합성 실패는 **성공으로 속이지 않는다** — path=None + 조치 가능한 사유를 남겨
      그 슬롯이 미완으로 남게 한다(발행 게이트가 막는다).
    · 원본 배경은 덮어쓰지 않고 `<배경>_titled.png` 로 새로 저장한다.
    """
    from core import image_plan as ip

    spec = ip.overlay_spec(slot)
    if not spec:
        return res
    base = res.get("path")
    if not base:
        return res

    try:
        from core import thumbnail_compose as tc
    except Exception as exc:  # noqa: BLE001 — 모듈 부재는 합성 불가(정직한 실패)
        log.warning(f"[draft_image_fill] 썸네일 합성 모듈 로드 실패({type(exc).__name__})")
        return {**res, "path": None, "reason": f"thumb_compose_unavailable:{type(exc).__name__}"}

    base_path = Path(base)
    out_path = base_path.with_name(f"{base_path.stem}_titled.png")
    outcome = tc.compose(base_path, out_path, message=spec["message"],
                         style=spec.get("style"),
                         cta=str(slot.get("_render_cta") or "자세히 보기"),
                         category=str(slot.get("_render_category") or ""),
                         date_label=str(slot.get("_render_date_label") or ""))
    if not outcome.get("ok"):
        log.warning("[draft_image_fill] 썸네일 한글 합성 실패(%s) — 이 슬롯은 미완으로 둔다",
                    outcome.get("reason"))
        return {**res, "path": None, "verified": False,
                "reason": f"thumb_compose:{outcome.get('reason') or 'unknown'}",
                "compose": outcome}
    return {**res, "path": Path(outcome["path"]), "background": str(base_path),
            "composed": True, "compose": outcome}


def _compose_from_body_slots(plan: Dict[str, Any], slot_map: Dict[str, str],
                             thumbnail_slot: Dict[str, Any], *, slug: str,
                             out_dir: Path, category: str, cta: str,
                             date_label: str) -> Dict[str, Any]:
    """Select the first provenance-verified same-post body asset and compose onto a copy."""
    from core import image_plan as ip
    for body_slot in plan.get("slots") or []:
        if not isinstance(body_slot, dict) or body_slot.get("role") != ip.ROLE_BODY:
            continue
        source_path = slot_map.get(str(body_slot.get("id") or ""))
        digest, reason = _verified_body_source(source_path, body_slot, slug=slug)
        if reason:
            continue
        result = compose_photo_backed_thumbnail(
            source_path, body_slot, slug=slug, thumbnail_slot=thumbnail_slot,
            out_dir=out_dir, category=category, cta=cta, date_label=date_label)
        result["source_verified"] = True
        result["source_sha256"] = digest
        return result
    return {"ok": False, "path": None, "reason": "no_verified_body_photo",
            "source_verified": False}


def _generate_photo_background(pool: _EnginePool, thumbnail_slot: Dict[str, Any], *,
                               title: str, category: str, slug: str, out_dir: Path,
                               verifier: Callable[..., dict], cta: str,
                               date_label: str) -> Dict[str, Any]:
    """Use one shared-budget background-only call when no eligible body photo exists."""
    from core import image_plan as ip
    spec = ip.overlay_spec(thumbnail_slot)
    message = str((spec or {}).get("message") or thumbnail_slot.get("label") or "").strip()
    if not message:
        return {"path": None, "reason": "thumbnail_message_missing"}
    style = (spec or {}).get("style") or ip.thumb_style()
    prompt = ip.build_thumb_bg_prompt(message=message, title=title, style=style)
    runtime_slot = dict(thumbnail_slot)
    runtime_slot.update({
        "text_policy": ip.TEXT_POLICY_OVERLAY,
        "overlay": {"message": message, "style": style},
        "_render_category": category,
        "_render_cta": cta,
        "_render_date_label": date_label,
    })
    try:
        with pool.lease() as engine:
            generated = _generate_verified(
                engine, prompt, ratio="1:1", prefix="thumb_background",
                expected_texts=(), verifier=verifier, max_regen=0,
                verify=False, thumbnail=True)
            if not generated.get("path"):
                return {"path": None, "reason": str(generated.get("reason") or "background_generation_failed")}
            composed = _apply_overlay(generated, runtime_slot)
    except Exception as exc:  # noqa: BLE001 — partial failure keeps body assets
        log.warning("[draft_image_fill] 사진 배경 1회 생성 실패(%s)", type(exc).__name__)
        return {"path": None, "reason": f"background_generation_error:{type(exc).__name__}"}
    if not composed.get("path"):
        return {"path": None, "reason": str(composed.get("reason") or "thumb_compose_failed"),
                "compose": composed.get("compose")}
    _mark_generated(composed["path"], slug=slug,
                    producer=f"draft_image_fill:slot:{ip.SLOT_THUMB}")
    return {"path": str(composed["path"]), "background": str(composed.get("background") or ""),
            "compose": composed.get("compose") or {}}


def _thumbnail_cta(brief: Any) -> str:
    """Use a short confirmed reader action, or a neutral CTA without adding facts."""
    action = str(brief.get("reader_action") or "").strip() if isinstance(brief, dict) else ""
    action = re.sub(r"\s+", " ", action).strip(" .。!?！？")
    if action and len(action) <= 14:
        return action
    for token, short in (("신청", "신청 조건 확인"), ("대상", "대상 확인"),
                         ("비교", "차이 확인"), ("차이", "차이 확인"),
                         ("금액", "금액 확인"), ("얼마", "금액 확인")):
        if token in action:
            return short
    return "내용 확인"


def _seoul_month() -> str:
    """Return the production month used as a non-factual visual date marker."""
    try:
        return datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y.%m")
    except (ValueError, OSError):
        return ""


def _thumbnail_compose_ready() -> bool:
    """Check local Pillow and Hangul font before spending a background generation call."""
    try:
        from core import thumbnail_compose as tc
        font, _source = tc.find_font(48)
        return font is not None
    except (ImportError, OSError, TypeError, ValueError):
        return False


def _run_slot_tasks(slots: Sequence[Dict[str, Any]], *, pool: _EnginePool,
                    verifier: Callable[..., dict], max_regen: int,
                    workers: int, category: str = "", cta: str = "",
                    date_label: str = "") -> Dict[str, Dict[str, Any]]:
    """슬롯들을 제한 병렬로 생성한다. 동시 실행 수는 절대 `workers` 를 넘지 않는다.

    executor 를 중첩하지 않는다(배치 루프는 순차 — 이 함수만 스레드를 만든다). 어떤
    슬롯이 예외로 죽어도 다른 슬롯 결과는 그대로 살아남고, 엔진은 반드시 반납된다.
    """
    runner = _SlotRunner(verifier=verifier, max_regen=max_regen,
                         category=category, cta=cta, date_label=date_label)
    results: Dict[str, Dict[str, Any]] = {}

    def _work(slot: Dict[str, Any]) -> Dict[str, Any]:
        try:
            with pool.lease() as engine:
                return runner.run(engine, slot)
        except Exception as exc:  # noqa: BLE001 — 한 슬롯 실패가 나머지를 막지 않는다
            log.warning(f"[draft_image_fill] 슬롯 생성 예외({type(exc).__name__}): "
                        f"{slot.get('id')}")
            return {"path": None, "regen": 0, "verified": False,
                    "reason": f"exception:{type(exc).__name__}"}

    limit = max(1, min(int(workers or 1), len(slots)))
    if limit <= 1:
        for slot in slots:
            results[str(slot.get("id"))] = _work(slot)
        return results

    with ThreadPoolExecutor(max_workers=limit, thread_name_prefix="imgfill") as ex:
        futures = {ex.submit(_work, slot): str(slot.get("id")) for slot in slots}
        for fut in as_completed(futures):
            slot_id = futures[fut]
            try:
                results[slot_id] = fut.result()
            except Exception as exc:  # noqa: BLE001 — future 자체 실패도 흡수
                results[slot_id] = {"path": None, "regen": 0, "verified": False,
                                    "reason": f"exception:{type(exc).__name__}"}
    return results


def _default_planner(*, article_text: str, title: str, category: str, card_budget: int) -> Any:
    """기본 Stage1 기획 — image_prompt_builder.plan_infocards 로 위임."""
    from core.image_prompt_builder import plan_infocards
    return plan_infocards(article_text=article_text, title=title, category=category,
                          card_budget=card_budget)


def _ground_cards(plan: Any, *, article_text: str, card_budget: int) -> List[Any]:
    """fact-grounding 게이트 — 본문에 근거 있는 카드만 반환(C1, YMYL).

    image_prompt_builder.validate_image_plan 을 재사용한다(카드의 단위 있는 숫자·날짜·%·
    나이·기간 토큰이 본문에 실제로 등장해야 통과). 게이트 자체가 로드/실행 불가하면
    보수적으로 카드를 전부 버린다(썸네일은 영향 없음) — 환각 수치 이미지를 만드느니
    카드를 안 만드는 쪽이 YMYL 안전.
    """
    try:
        from core.image_prompt_builder import validate_image_plan
    except Exception as exc:  # noqa: BLE001 — 게이트 미로드 → 보수적 제외
        log.warning(f"[draft_image_fill] validate_image_plan 미로드({type(exc).__name__}) — 카드 제외")
        return []
    try:
        report = validate_image_plan(plan, article_text=article_text, card_budget=card_budget)
    except Exception as exc:  # noqa: BLE001 — 게이트 예외 → 보수적 제외
        log.warning(f"[draft_image_fill] fact-grounding 예외({type(exc).__name__}) — 카드 제외")
        return []
    grounded = list(getattr(report, "grounded_cards", []) or [])
    flagged = list(getattr(report, "flagged", []) or [])
    if flagged:
        reasons = ", ".join(sorted({getattr(f, "reason", "?") for f in flagged}))
        log.warning(
            f"[draft_image_fill] fact-grounding 제외 {len(flagged)}장(근거 없는 수치/날짜) — {reasons}")
    return grounded


def _thumbnail_fallback_message(title: str, slot: Optional[Dict[str, Any]] = None) -> str:
    """폴백 썸네일에 쓸 확정 슬롯 문구 또는 레거시 제목 기반 문구를 돌려준다."""
    try:
        from core import editorial_brief as eb
        if isinstance(slot, dict):
            from core import image_plan as ip
            overlay = ip.overlay_spec(slot)
            message = (overlay or {}).get("message") or slot.get("label")
            if message:
                return str(message).strip()
        return eb.message_from_title(title)
    except Exception as exc:  # noqa: BLE001 — resolver 미로드면 전체 제목으로 안전 폴백
        log.debug(f"[draft_image_fill] PIL 폴백 문구 조회 실패({type(exc).__name__})")
        return str(title or "").strip()


def _safe_engine_open(engine: Any) -> str:
    try:
        status = engine.open()
        return str(status or "error")
    except Exception as exc:  # noqa: BLE001 — open 실패는 오류로 안전 처리
        log.warning(f"[draft_image_fill] engine.open 예외({type(exc).__name__})")
        return "error"


def _safe_open_cards_session(engine: Any, *, ratio: str) -> bool:
    """카드 배치 세션 오픈을 감싼다(fail-open). 실패 시 False(호출자가 legacy 폴백)."""
    try:
        return bool(engine.open_cards_session(ratio=ratio))
    except Exception as exc:  # noqa: BLE001 — 세션 오픈 실패는 오류로 안전 처리
        log.warning(f"[draft_image_fill] open_cards_session 예외({type(exc).__name__})")
        return False


def _safe_engine_close(engine: Any) -> None:
    try:
        engine.close()
    except Exception:  # noqa: BLE001 — 정리 실패는 무해
        pass


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _call_optional(obj: Any, name: str, *args) -> Any:
    """obj 에 name 메서드가 있으면 호출(없으면 무시). fail-open — 예외 무전파.

    genspark_client 의 선택적 헬퍼(set_quality·_dismiss_credit_confirm)를 구버전/스텁에서도
    안전하게 호출하기 위한 얇은 래퍼.
    """
    fn = getattr(obj, name, None)
    if not callable(fn):
        return None
    try:
        return fn(*args)
    except Exception as exc:  # noqa: BLE001 — 선택적 호출 실패는 흐름 무해
        log.warning(f"[draft_image_fill] {name} 호출 실패(무시): {type(exc).__name__}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# B2 — 대상 선별 단일화 + 게이트 노출
# ─────────────────────────────────────────────────────────────────────────────
def image_fill_gate() -> dict:
    """이미지 채움 게이트 판정 — 서브프로세스·네트워크 0, 설정만 읽는다.

    GUI·CLI·배치가 전부 이 함수 하나로 켜짐/엔진/사유를 판정한다(D7 — GUI 가 이 로직을
    손으로 복제하던 문제의 재발 방지).

    Returns:
        {"enabled": bool, "engine": "codex"|"genspark",
         "reason": "" | "image_fill_disabled" | "genspark_disabled"}.
    """
    engine_name = _image_fill_engine()
    if not _image_fill_enabled():
        return {"enabled": False, "engine": engine_name, "reason": "image_fill_disabled"}
    if engine_name == "genspark" and not _genspark_enabled():
        return {"enabled": False, "engine": engine_name, "reason": "genspark_disabled"}
    return {"enabled": True, "engine": engine_name, "reason": ""}


def fill_targets(*, topup: bool = False,
                 include_unverified: bool = False) -> List[dict]:
    """draft_ready 보관본 중 이미지 채움 대상을 단일 기준으로 선별한다(D2·D3).

    Args:
        topup: False(기본) — 유효 이미지가 0장인 글만 대상(경로가 사라진 글도 유효
            이미지 0장이면 다시 대상이 된다 — D3). True — 위에 더해, **아직 완성되지
            않은** 글도 포함한다. 그 '완성' 판정은 계획 경로가 켜져 있으면
            `images_complete`(= 발행 직전 게이트와 같은 규칙)를 그대로 쓰고, 꺼져
            있으면 예전 규칙(썸네일 1 + IMAGE_FILL_CARDS 카드 목표치)을 쓴다.

            ⚠️ 2026-09-18 실측 결함: 계획 경로가 켜진 상태에서 목표치 개수만 세면,
            레거시 이미지 4장이 붙어 있지만 `image_plan` 이 없는 글이 채움에서는
            "이미 충족"으로 제외되고 발행 게이트에서는 `no_plan` 으로 막혀 **영원히
            채워지지도 발행되지도 않는다.** 두 자리가 같은 함수를 보게 해 그 교착을 없앤다.

    Returns:
        load_drafts 항목 dict 그대로 + "valid_images"(int) + "missing_images"(int).
        예외는 삼키고 빈 리스트를 반환한다(fail-open — 대상 선별 실패가 배치를 막지 않는다).
    """
    try:
        from core import draft_store
        drafts = draft_store.load_drafts(status_filter="draft_ready")
    except Exception as exc:  # noqa: BLE001
        log.warning(f"[draft_image_fill] fill_targets: load_drafts 실패: {type(exc).__name__}")
        return []

    want_total = 1 + _coerce_int(
        _fill_setting("IMAGE_FILL_CARDS", "GENSPARK_INFOCARD_BUDGET", 3), 3)
    plan_on = plan_mode_enabled()
    out: List[dict] = []
    try:
        for entry in drafts:
            post = entry.get("post_data") or {}
            valid, missing = split_existing_images(post.get("image_paths") or [])
            item = dict(entry)
            item["valid_images"] = len(valid)
            item["missing_images"] = len(missing)
            if len(valid) == 0:
                out.append(item)
            elif topup and plan_on:
                # 계획 경로에서는 발행 게이트와 **같은 함수**로 판정한다.
                complete, _detail = images_complete(post)
                if not complete:
                    # ⚠️ 출처를 확인할 수 없는 **옛 자산이 붙어 있을 뿐**인 글은
                    #    자동 보충 대상이 아니다(2026-09-23). 재고에서는 정직하게
                    #    빼지만, 여기에 넣으면 사용자가 "이미지 채움"을 한 번 누를 때
                    #    수백~수천 편의 기존 이미지를 **말없이 다시 만들어** 버린다
                    #    (유료 생성 + 기존 자산 교체). 되살리려면 사용자가 글을 골라
                    #    명시적으로 요청해야 한다 — `run_fill_batch(only_slugs=...,
                    #    replace_unverified=True)`.
                    if (_detail.get("reason") == "unverified_assets"
                            and not include_unverified):
                        item["unverified_images"] = _detail.get("unverified", 0)
                        continue
                    out.append(item)
            elif topup and len(valid) < want_total:
                out.append(item)
    except Exception as exc:  # noqa: BLE001 — 선별 실패는 빈 목록(배치는 다음 실행 재시도)
        log.warning(f"[draft_image_fill] fill_targets 예외: {type(exc).__name__}")
        return []
    return out


# ─────────────────────────────────────────────────────────────────────────────
# B5 — 중복 실행 차단(프로필 격리 락) + 실패 사유 영속
# ─────────────────────────────────────────────────────────────────────────────
def _fill_lock_path() -> Path:
    from config import profile as _profile
    return Path(_profile.logs_dir()) / "image_fill.lock"


class _ImageFillLock:
    """프로필 격리 배치 락(PID 파일) — 살아 있는 다른 프로세스가 잡고 있으면 거절.

    락 파일 조작 자체의 실패(권한 등 판정 불가)는 fail-open(락 없이 진행)하되 로그를
    남긴다 — 락 메커니즘의 결함이 배치 자체를 영원히 막아선 안 된다.
    """

    def __init__(self) -> None:
        self._path: Optional[Path] = None
        self._acquired = False

    def acquire(self) -> bool:
        try:
            self._path = _fill_lock_path()
        except Exception as exc:  # noqa: BLE001
            log.warning(f"[draft_image_fill] 락 경로 확인 실패({type(exc).__name__}) — 락 없이 진행")
            self._acquired = False
            return True
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # ★원자적 생성(O_CREAT|O_EXCL) — "존재 확인 후 쓰기"는 두 프로세스가 동시에
            #   시작하면 둘 다 락을 잡는 TOCTOU 창이 있다. 파일 생성 자체를 배타적으로
            #   시도하고, 이미 있을 때만 소유자 PID 를 확인해 죽은 락을 회수한다.
            for _attempt in (1, 2):
                try:
                    fd = os.open(str(self._path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                except FileExistsError:
                    raw = ""
                    try:
                        raw = self._path.read_text(encoding="utf-8").strip()
                    except OSError:
                        pass
                    try:
                        pid = int(raw) if raw else 0
                    except ValueError:
                        pid = 0
                    if pid == os.getpid():
                        # 같은 프로세스가 이미 잡고 있다 — 진행하되 해제 책임은 바깥 것.
                        return True
                    if pid and _pid_alive(pid):
                        return False
                    # 죽은 프로세스/빈 락 → 회수하고 한 번만 재시도.
                    try:
                        self._path.unlink()
                    except OSError:
                        return False
                    continue
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(str(os.getpid()))
                self._acquired = True
                return True
            return False
        except (OSError, ValueError) as exc:
            log.warning(f"[draft_image_fill] 락 파일 조작 실패({type(exc).__name__}) — 락 없이 진행")
            self._acquired = False
            return True

    def release(self) -> None:
        if not self._acquired or self._path is None:
            return
        try:
            if self._path.exists():
                raw = self._path.read_text(encoding="utf-8").strip()
                if not raw or int(raw) == os.getpid():
                    self._path.unlink()
        except (OSError, ValueError) as exc:  # noqa: BLE001 — 해제 실패는 무해(락은 PID 기반)
            log.warning(f"[draft_image_fill] 락 해제 실패({type(exc).__name__})")


def _fill_state_path() -> Path:
    from config import profile as _profile
    return Path(_profile.logs_dir()) / "image_fill_state.json"


def _load_fill_state() -> Dict[str, Any]:
    try:
        from config import safe_load_json
        data = safe_load_json(_fill_state_path(), default={})
    except Exception:  # noqa: BLE001
        try:
            data = json.loads(_fill_state_path().read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            data = {}
    return data if isinstance(data, dict) else {}


def _save_fill_state(data: Dict[str, Any]) -> None:
    try:
        from config import atomic_write_json
        path = _fill_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, data)
    except Exception as exc:  # noqa: BLE001 — 상태 기록 실패가 배치를 막아선 안 된다
        log.warning(f"[draft_image_fill] 상태 기록 실패({type(exc).__name__}) — 무시")


def _persist_fill_state(slug: str, *, ok: bool, reason: str, produced: int, reused: int) -> None:
    """슬러그별 마지막 채움 시도를 image_fill_state.json 에 원자적으로 기록한다(D6).

    실패해도 배치를 멈추지 않는다 — 기록은 관측용이지 게이트가 아니다.
    """
    try:
        data = _load_fill_state()
        prior = data.get(slug) if isinstance(data.get(slug), dict) else {}
        attempts = _coerce_int(prior.get("attempts"), 0) + 1
        data[slug] = {
            "last_attempt": _now_iso(),
            "reason": reason if reason else ("ok" if ok else ""),
            "attempts": attempts,
            "produced": _coerce_int(produced, 0),
            "reused": _coerce_int(reused, 0),
        }
        _save_fill_state(data)
    except Exception as exc:  # noqa: BLE001
        log.warning(f"[draft_image_fill] 상태 갱신 실패({slug}): {type(exc).__name__}")


def last_fill_state(slug: Optional[str] = None) -> dict:
    """슬러그별 마지막 채움 시도 기록을 조회한다. slug=None 이면 전체 dict."""
    data = _load_fill_state()
    if slug is None:
        return data
    rec = data.get(slug)
    return rec if isinstance(rec, dict) else {}


# ─────────────────────────────────────────────────────────────────────────────
# 공개 API 2 — 스캔 배치
# ─────────────────────────────────────────────────────────────────────────────
def run_fill_batch(
    *,
    limit: Optional[int] = None,
    account: Optional[str] = None,
    genspark: Optional[Any] = None,
    engine: Optional[Any] = None,
    planner: Optional[Callable[..., Any]] = None,
    verifier: Optional[Callable[..., dict]] = None,
    topup: bool = False,
    slug: Optional[str] = None,
    only_slugs: Optional[Sequence[str]] = None,
    replace_unverified: bool = False,
    engine_factory: Optional[Callable[[], Any]] = None,
    parallel: Optional[int] = None,
    progress_cb: Optional[Callable[[dict], None]] = None,
) -> dict:
    """draft_ready 보관본을 스캔해 이미지를 생성·검증·보관한다(멱등).

    limit(기본 IMAGE_FILL_MAX_POSTS_PER_RUN)까지 fill_targets(topup=) 로 선별한 각
    보관본에 fill_images_for_draft 를 호출하고, 검증 통과분은 save_draft 로 image_paths
    를 갱신한다(status·schedule_at 보존). 저장 직전 슬러그 status 를 재조회해 배치 도중
    발행된 슬러그의 부활을 막는다.

    Args:
        slug: 주면 선별된 대상 중 **그 슬러그 1편만** 처리한다(단건 채움). 게이트·
            발행중 거부·락·저장 직전 status 재조회·실패 사유 영속을 배치와 **똑같이**
            거친다 — 단건 경로가 이 보호막들을 우회하지 않게 하는 것이 이 인자의
            존재 이유다. 대상에 없으면 아무것도 생성하지 않고 사유를 돌려준다
            (`slug_not_found`=draft_ready 목록에 그 슬러그가 없음,
            `slug_not_a_target`=있지만 이미 이미지 조건을 충족해 대상이 아님).
        only_slugs: 주면 선별된 대상 중 **이 목록에 있는 슬러그만** 처리한다(무인
            운영의 준비 경로가 "결말 미상·확정된 글"을 재료에서 빼고 고른 목록을
            그대로 넘기는 용도). 대상 선별 규칙은 `fill_targets` 그대로이고 여기서
            교집합만 취한다 — 목록에 있어도 대상이 아니면 처리하지 않는다.
            None(기본)이면 예전과 동일하게 전체 대상을 본다.
        engine_factory: 워커 엔진 팩토리(주입 없으면 설정대로 만든다). 엔진을 주입하면
            병렬은 1로 내려간다(같은 종류의 엔진을 더 만들 수 없으므로).
        parallel: 한 편 안에서 동시에 생성할 슬롯 수(기본 IMAGE_FILL_PARALLEL=2).
        progress_cb: 한 편이 끝날 때마다 호출되는 콜백(진행 표시용).
            `{"done": int, "total": int, "failed": int, "slug": str, "ok": bool}`.
            콜백에서 난 예외는 삼킨다 — 진행 표시가 배치를 죽이지 않는다.

    ★B3(엔진 1회 개통): 엔진을 배치당 **한 번만** 만들고 **한 번만** open 해 전 슬러그에
    재사용하고, 마지막에 한 번만 close 한다(개선 전엔 글마다 새 엔진을 만들고 열어 codex
    엔진 기준 글 1편당 `codex login status` 서브프로세스 1회 + 임시디렉터리 생성/삭제
    1회가 들었다 — 25편이면 낭비 호출 24회). 각 글의 이미지 저장 경로는
    fill_images_for_draft 내부에서 주입 엔진의 `out_dir` 을 그 글의 durable 경로로
    갈아끼워 해결한다.

    ★B5(중복 실행 차단): 프로필 격리 `image_fill.lock` 을 다른 살아있는 프로세스가 쥐고
    있으면 즉시 `already_running` 으로 반환하고 draft_store 를 건드리지 않는다.
    ⚠️ 이 락이 막는 범위는 **이 함수를 거치는 실행끼리**다(GUI·CLI·blog_service·단건
    slug 호출). `fill_images_for_draft` 를 직접 부르는 코드 — 특히 auto_scheduler 의
    초안 생성 인라인 채움([HOOK G]) — 은 이 락을 잡지 않는다. 그 방향은 위의
    `_publish_in_progress()` 검사가 반대로 막는다(발행 파이프라인이 도는 동안 배치가
    시작되지 않음).

    ⚠️ "filled" 은 **이미지 생성·검증·저장 완료**를 뜻할 뿐, **발행에 삽입됨을 뜻하지
    않는다**. 발행측 삽입 배선은 Phase 3 필요(모듈 docstring 참조 — auto_scheduler 가
    image_paths 를 올바른 위치에서 읽고 _force_image_insert 를 전달해야 함). 이 배치는
    절대 발행하지 않는다.

    Returns:
        {"filled": N, "scanned": M, "targets": K, "processed": N, "skipped": {reason: count},
         "stopped_early": bool, "engine_opens": int, "images_reused": int,
         "results": [{"slug","ok","reason","produced","reused"}, ...]}.
        filled=이미지 저장 완료 편수(≠발행). stale_no_longer_ready=배치 중 발행돼 부활
        방지로 저장 스킵된 편수.
    """
    summary: Dict[str, Any] = {
        "filled": 0, "scanned": 0, "targets": 0, "processed": 0,
        "skipped": {}, "stopped_early": False,
        "engine_opens": 0, "images_reused": 0, "results": [],
    }

    def _bump(reason: str) -> None:
        summary["skipped"][reason] = summary["skipped"].get(reason, 0) + 1

    gate = image_fill_gate()
    if not gate["enabled"]:
        _bump(gate["reason"])
        return summary

    if _publish_in_progress():
        _bump("publish_in_progress")
        log.info("[draft_image_fill] 발행 파이프라인 실행 중 — 배치 시작 거부")
        return summary

    lock = _ImageFillLock()
    # The publish check precedes acquisition; another thread can enter in between.
    if not lock.acquire() or not lock._acquired:
        _bump("already_running")
        log.info("[draft_image_fill] 이미지 채움 잠금 확보 불가 — 이번 실행 거부")
        return summary

    try:
        return _run_fill_batch_locked(
            summary, _bump, limit=limit, account=account, genspark=genspark, engine=engine,
            planner=planner, verifier=verifier, topup=topup, slug=slug,
            only_slugs=only_slugs, replace_unverified=replace_unverified,
            engine_factory=engine_factory, parallel=parallel, progress_cb=progress_cb)
    finally:
        lock.release()


def _emit_progress(progress_cb: Optional[Callable[[dict], None]], payload: dict) -> None:
    """진행 콜백 호출 — 어떤 예외도 삼킨다(표시 코드가 배치를 죽이지 않는다)."""
    if progress_cb is None:
        return
    try:
        progress_cb(dict(payload))
    except Exception as exc:  # noqa: BLE001
        log.debug(f"[draft_image_fill] progress_cb 예외 무시: {type(exc).__name__}")


def _run_fill_batch_locked(
    summary: Dict[str, Any], bump: Callable[[str], None], *,
    limit: Optional[int], account: Optional[str], genspark: Optional[Any],
    engine: Optional[Any], planner: Optional[Callable[..., Any]],
    verifier: Optional[Callable[..., dict]], topup: bool, slug: Optional[str] = None,
    only_slugs: Optional[Sequence[str]] = None,
    replace_unverified: bool = False,
    engine_factory: Optional[Callable[[], Any]] = None,
    parallel: Optional[int] = None,
    progress_cb: Optional[Callable[[dict], None]] = None,
) -> dict:
    """run_fill_batch 의 락 보유 구간 본체(가독성 분리 — 락 자체는 호출자가 관리)."""
    try:
        from core import draft_store
    except Exception as exc:  # noqa: BLE001
        log.warning(f"[draft_image_fill] draft_store 로드 실패: {type(exc).__name__}")
        bump("draft_store_unavailable")
        return summary

    resolved_limit = _coerce_int(
        limit if limit is not None
        else _fill_setting("IMAGE_FILL_MAX_POSTS_PER_RUN", "GENSPARK_MAX_POSTS_PER_RUN", 25), 25)
    if resolved_limit <= 0:
        return summary

    try:
        all_drafts = draft_store.load_drafts(status_filter="draft_ready")
    except Exception as exc:  # noqa: BLE001
        log.warning(f"[draft_image_fill] load_drafts 실패: {type(exc).__name__}")
        bump("load_failed")
        return summary
    summary["scanned"] = len(all_drafts)

    # 차단된 옛 자산을 **다시 만드는** 것은 사용자가 글을 골라 명시적으로 요청했을
    # 때뿐이다. 선택 없이 이 깃발만 켜지면 수천 편 일괄 재생성이 되므로 무시한다.
    _explicit = bool(slug) or bool(only_slugs)
    _replace = bool(replace_unverified) and _explicit
    if replace_unverified and not _explicit:
        log.warning("[draft_image_fill] replace_unverified 는 slug/only_slugs 로 "
                    "글을 고른 경우에만 적용됩니다 — 이번 실행에서는 무시합니다")
    targets = fill_targets(topup=topup, include_unverified=_replace)
    if only_slugs is not None:
        # 호출자가 고른 목록과의 **교집합**만 처리한다 — 선별 규칙(fill_targets)을
        # 우회해 목록에 있다고 무조건 만들지 않는다.
        allow = {str(s) for s in only_slugs if s}
        dropped = len(targets)
        targets = [t for t in targets if t.get("slug") in allow]
        dropped -= len(targets)
        if dropped:
            summary["filtered_out"] = summary.get("filtered_out", 0) + dropped
    if slug:
        # 단건 요청 — 대상 선별 규칙은 배치와 같은 fill_targets 를 그대로 쓰고, 그 결과에서
        # 슬러그만 고른다(선별 로직 복제 금지). 없으면 "목록에 없음"과 "대상이 아님"을
        # 구분해 정직하게 보고한다.
        targets = [t for t in targets if t.get("slug") == slug]
        if not targets:
            known = any(isinstance(d, dict) and d.get("slug") == slug for d in all_drafts)
            reason = "slug_not_a_target" if known else "slug_not_found"
            bump(reason)
            summary["results"].append(
                {"slug": slug, "ok": False, "reason": reason, "produced": 0, "reused": 0})
            return summary
    summary["targets"] = len(targets)
    if not targets:
        return summary

    # B3 — 엔진을 배치당 1회만 개통해 전 슬러그에 재사용하고 마지막에 한 번만 닫는다.
    #      제한 병렬(2026-09-10)에서는 그 엔진이 풀의 첫 엔진(primary)이 되고, 워커
    #      엔진은 필요할 때만 상한까지 추가로 열린다(예산·저널은 공유).
    injected_engine = engine if engine is not None else genspark
    workers = _parallel_workers(parallel)
    pool = _make_pool(out_dir=_images_dir_for("_image_fill_batch"), account=account,
                      engine=injected_engine, engine_ready=False,
                      engine_factory=engine_factory, workers=workers, close_primary=True)
    summary["parallel"] = workers
    summary["engine_opens"] = 1
    status = pool.open()
    if status != "ok":
        bump(status)
        summary["stopped_early"] = True
        summary["engine_opens"] = 1
        log.info(f"[draft_image_fill] 배치 엔진 개통 실패({status}) — 배치 시작 안 함")
        pool.close()
        return summary

    planned = targets[:resolved_limit]
    total = len(planned)
    done = 0
    failed = 0
    try:
        for entry in planned:
            slug = entry.get("slug") or ""
            post = entry.get("post_data") or {}
            if not slug or not post.get("content"):
                bump("no_article")
                continue
            summary["processed"] += 1
            before_filled = summary["filled"]
            try:
                _process_one_fill_target(
                    summary, bump, draft_store=draft_store, entry=entry, post=post, slug=slug,
                    account=account, genspark=genspark, eng=pool.primary, planner=planner,
                    verifier=verifier, pool=pool,
                    replace_unverified=_replace)
            except _StopFillBatch:
                done += 1
                stopped_ok = summary["filled"] > before_filled
                if not stopped_ok:
                    failed += 1
                _emit_progress(progress_cb, {"done": done, "total": total,
                                             "failed": failed, "slug": slug,
                                             "ok": stopped_ok})
                break
            done += 1
            ok = summary["filled"] > before_filled
            if not ok:
                failed += 1
            _emit_progress(progress_cb, {"done": done, "total": total,
                                         "failed": failed, "slug": slug, "ok": ok})
    finally:
        summary["engine_opens"] = pool.opened_count or 1
        pool.close()

    return summary


class _StopFillBatch(Exception):
    """배치 조기 종료 신호(내부 전용) — engine.close()/락 해제가 finally 로 보장되게 한다."""


def _process_one_fill_target(
    summary: Dict[str, Any], bump: Callable[[str], None], *, draft_store: Any, entry: dict,
    post: dict, slug: str, account: Optional[str], genspark: Optional[Any], eng: Any,
    planner: Optional[Callable[..., Any]], verifier: Optional[Callable[..., dict]],
    pool: Optional[_EnginePool] = None, replace_unverified: bool = False,
) -> None:
    """단일 슬러그 채움 — run_fill_batch 루프 본문(엔진은 배치가 이미 열어 둔 것을 공유).

    계획 경로에서는 배치가 만든 **엔진 풀**을 그대로 넘겨 워커 엔진까지 재사용한다.
    레거시 경로는 예전처럼 풀의 첫 엔진 하나만 쓴다(pool 인자를 무시).
    """
    result = fill_images_for_draft(
        post, slug=slug, category=post.get("category"), account=account,
        genspark=genspark, engine=eng, engine_ready=True, close_engine=False,
        planner=planner, verifier=verifier, pool=pool,
        replace_unverified=replace_unverified)
    meta = result.get("image_meta") or {}
    image_paths = result.get("image_paths") or []
    skip_reason = str(meta.get("skipped") or "")
    produced = _coerce_int(meta.get("generated"), 0)
    reused_n = _coerce_int(meta.get("reused"), 0)
    summary["images_reused"] += reused_n

    ok = False
    reason = skip_reason
    if image_paths:
        # C2: 저장 직전 현재 status 를 재조회한다. 편당 수 분이 걸려, 배치 스냅샷 이후
        #     정규 발행 루프가 같은 슬러그를 uploaded 로 바꾸고 post_data.json 을 prune
        #     했을 수 있다. 이때 save_draft(status="draft_ready") 를 부르면 _status.json
        #     이 새로 쓰여 발행기록(uploaded_at·url)이 지워지고 슬러그가 '부활'해 다음
        #     발행 루프가 같은 글을 중복 발행한다. 여전히 draft_ready 이고 본문이 남아
        #     있을 때만 저장한다(부활 방지).
        if not _draft_still_fillable(draft_store, slug):
            bump("stale_no_longer_ready")
            reason = "stale_no_longer_ready"
            log.warning(
                f"[draft_image_fill] {slug}: 배치 도중 상태 변경 감지 — 저장 스킵(부활 방지)")
        elif _persist_draft_images(draft_store, entry, post, image_paths):
            summary["filled"] += 1
            ok = True
            reason = "ok"
        else:
            bump("save_failed")
            reason = "save_failed"
    else:
        reason = skip_reason or "no_images"
        bump(reason)

    summary["results"].append(
        {"slug": slug, "ok": ok, "reason": reason, "produced": produced, "reused": reused_n})
    _persist_fill_state(slug, ok=ok, reason=reason, produced=produced, reused=reused_n)

    # 한도·미로그인·codex 미설치·config 파손처럼 **다음 편에서도 똑같이 실패할** 사유면
    # 나머지 보관본은 건드리지 않고 다음 실행으로 넘긴다(멱등 재개). 배치가 이미 엔진을
    # 한 번만 열기 때문에 이 사유들은 대개 개통 단계에서 걸러지지만, 개별 호출이 도중에
    # 같은 사유를 반환할 가능성에 대비해 방어적으로 유지한다.
    if skip_reason in ("rate_limited", "login_required", "codex_not_found", "config_broken"):
        summary["stopped_early"] = True
        log.info(f"[draft_image_fill] {skip_reason} — 배치 조기 종료(나머지 보관본 보존)")
        raise _StopFillBatch()


def _draft_still_fillable(draft_store: Any, slug: str) -> bool:
    """저장 직전 슬러그의 현재 status 를 디스크에서 fresh 로 재조회한다(C2 부활 방지).

    여전히 draft_ready 이고 post_data.json 이 남아 있으면 True. 배치 도중 정규 발행
    루프가 uploaded 로 바꾸고 post_data.json 을 prune 했으면 False → 저장 스킵. 캐시가
    아니라 파일을 직접 읽어 동시 변경을 반영한다. 판정 불가(경로/파싱 실패)면 보수적으로
    False(부활 방지 우선 — 이번 이미지는 다음 실행에서 재시도).
    """
    try:
        runs_dir = Path(getattr(draft_store, "_RUNS_DIR"))
        status_name = getattr(draft_store, "_STATUS_FILE", "_status.json")
        post_name = getattr(draft_store, "_POST_DATA_FILE", "post_data.json")
    except Exception:  # noqa: BLE001
        return False
    sdir = runs_dir / str(slug)
    status_path = sdir / status_name
    if not status_path.exists():
        return False
    reader = getattr(draft_store, "_read_json", None)
    try:
        data = reader(status_path) if callable(reader) else json.loads(
            status_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return False
    if not isinstance(data, dict) or data.get("status") != "draft_ready":
        return False
    # 발행 완료 시 mark_status(uploaded) 가 post_data.json 을 삭제한다 → 본문 없으면 부활 금지.
    return (sdir / post_name).exists()


def _persist_draft_images(draft_store: Any, entry: dict, post: dict,
                          image_paths: List[str]) -> bool:
    """성공한 이미지 경로를 save_draft 로 보관(status·schedule_at 보존). 발행 안 함."""
    try:
        draft_store.save_draft(
            entry.get("slug"),
            post,
            image_paths,
            schedule_at=entry.get("schedule_at"),
            status="draft_ready",
        )
        return True
    except Exception as exc:  # noqa: BLE001 — 저장 실패는 배치 지속(다음 실행 재시도)
        log.warning(f"[draft_image_fill] save_draft 실패({entry.get('slug')}): {type(exc).__name__}")
        return False


def _publish_in_progress() -> bool:
    """발행 파이프라인 배타 락(pipeline.lock)이 살아있는 프로세스 소유면 True.

    auto_scheduler.PipelineLock 이 쓰는 lock 파일을 직접 확인한다(모듈 import 없이).
    락 파일이 없거나 소유 PID 가 죽었으면 False. 어떤 오류도 False(fail-open — 판정
    실패가 배치를 막지 않는다).
    """
    try:
        from config import profile as _profile
        lock_path = _profile.logs_dir() / "pipeline.lock"
    except Exception:  # noqa: BLE001
        return False
    try:
        if not lock_path.exists():
            return False
        raw = lock_path.read_text(encoding="utf-8").strip()
        pid = int(raw) if raw else 0
    except (OSError, ValueError):
        return False
    return _pid_alive(pid)


def _pid_alive(pid: int) -> bool:
    """PID 생존 확인(Windows/POSIX). 판정 불가 시 보수적으로 True(락 존중)."""
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        except Exception:  # noqa: BLE001
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# ─────────────────────────────────────────────────────────────────────────────
# CLI 진입점
# ─────────────────────────────────────────────────────────────────────────────
def _cli(argv: Optional[List[str]] = None) -> int:
    import argparse

    try:  # 윈도우 콘솔 한글/이모지 안전
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    ap = argparse.ArgumentParser(
        prog="python -m core.draft_image_fill",
        description="이미지 자동 채움 — 보관본 스캔 배치/단건(기본 엔진 Codex CLI).")
    ap.add_argument("--run", nargs="?", type=int, const=-1, default=None, metavar="N",
                    help="스캔 배치 실행(N=최대 편수, 생략 시 설정값)")
    ap.add_argument("--dry-run", action="store_true",
                    help="대상 편수·슬러그만 출력(생성·저장 없음)")
    ap.add_argument("--slug", default=None, help="단건 실행할 보관본 슬러그")
    ap.add_argument("--account", default=None, help="젠스파크 엔진일 때 사용할 계정 이메일")
    ap.add_argument("--probe", action="store_true",
                    help="선택된 엔진의 준비 상태만 점검(생성 없음)")
    args = ap.parse_args(argv)

    engine_name = _image_fill_engine()

    if args.probe:
        return _cli_probe(engine_name)

    if not _image_fill_enabled():
        print("IMAGE_FILL_ENABLED=False — 이미지 자동 채움이 비활성입니다"
              "(설정 → 이미지 채움에서 켜세요).")
        return 0
    if engine_name == "genspark" and not _genspark_enabled():
        print("IMAGE_FILL_ENGINE=genspark 인데 GENSPARK_ENABLED=False 입니다"
              "(젠스파크를 쓰려면 둘 다 켜세요).")
        return 0

    if args.dry_run:
        return _cli_dry_run()

    if args.slug:
        return _cli_single(args.slug, args.account)

    if args.run is not None:
        limit = None if args.run in (None, -1) else args.run
        summary = run_fill_batch(limit=limit, account=args.account)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    ap.print_help()
    return 0


def _cli_probe(engine_name: str) -> int:
    """선택된 엔진의 준비 상태를 사실대로 출력한다(생성·발행 없음)."""
    print(f"이미지 채움 : {'ON' if _image_fill_enabled() else 'OFF'}")
    print(f"엔진        : {engine_name}")
    if engine_name == "genspark":
        print(f"젠스파크    : {'ON' if _genspark_enabled() else 'OFF'}")
        print("젠스파크 연결 점검은 설정 탭의 '연결 테스트' 버튼을 쓰세요.")
        return 0 if _genspark_enabled() else 1
    try:
        from core.codex_image import probe as _probe
        state = _probe()
    except Exception as exc:  # noqa: BLE001
        print(f"codex_image 로드 실패: {type(exc).__name__}")
        return 1
    print(f"codex 실행  : {state.get('executable')}")
    print(f"모델        : {state.get('model')}")
    print(f"준비 상태   : {'[OK] ' if state.get('ok') else '[FAIL] '}{state.get('reason')}")
    print(f"설명        : {state.get('detail')}")
    return 0 if state.get("ok") else 1


def _cli_dry_run() -> int:
    try:
        from core import draft_store
        scanned = len(draft_store.load_drafts(status_filter="draft_ready"))
    except Exception as exc:  # noqa: BLE001
        print(f"draft 로드 실패: {type(exc).__name__}")
        return 1
    targets = fill_targets()   # B2: 대상 선별 단일 진실(fill_targets 와 배치가 항상 일치)
    print(f"draft_ready {scanned}편 중 이미지 없는 대상 {len(targets)}편:")
    for d in targets:
        print(f"  - {d.get('slug')}")
    return 0


def _cli_single(slug: str, account: Optional[str]) -> int:
    try:
        from core import draft_store
        drafts = draft_store.load_drafts(status_filter="draft_ready")
    except Exception as exc:  # noqa: BLE001
        print(f"draft 로드 실패: {type(exc).__name__}")
        return 1
    entry = next((d for d in drafts if d.get("slug") == slug), None)
    if entry is None:
        print(f"슬러그를 찾지 못했습니다: {slug}")
        return 1
    post = entry.get("post_data") or {}
    result = fill_images_for_draft(post, slug=slug, category=post.get("category"),
                                   account=account)
    image_paths = result.get("image_paths") or []
    if image_paths:
        from core import draft_store as _ds
        _persist_draft_images(_ds, entry, post, image_paths)
        print(f"채움 완료: {len(image_paths)}장")
    print(json.dumps(result.get("image_meta") or {}, ensure_ascii=False, indent=2))
    return 0 if image_paths else 1


if __name__ == "__main__":
    raise SystemExit(_cli())
