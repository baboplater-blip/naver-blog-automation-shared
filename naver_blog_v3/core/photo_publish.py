# core/photo_publish.py — 📸 사진 인제스트 런 단건 발행 (P0-② Phase 2 마지막 갭)
#
# 목적: experience_writer 가 만든 사진 런(runs/{slug})의 data dict 하나를 사용자가
#       버튼으로 **실제 네이버에 발행**한다. 실계정에 글·사진이 올라가는 첫 코드라
#       안전 게이트가 최우선이다.
#
# 하드 게이트(이 모듈이 지키는 불변식):
#   • 자동/배치 호출 경로 0 — 이 함수는 GUI 사용자 클릭으로만 호출된다.
#     스케줄러(auto_scheduler)·post_refresher 어디서도 부르지 않는다.
#   • run_pipeline(엑셀 큐 배치)을 재사용하지 않는다 — 별도 얇은 단건 함수.
#   • auto_scheduler 의 발행 프리미티브(chrome_session·attach_to_session·init_driver·
#     naver_login·_release_driver)만 **재사용**한다(그 모듈은 수정하지 않는다).
#   • 전역 SKIP_IMAGE_INSERT 는 불변 — per-post ``_force_image_insert=True`` 로만
#     그 발행 경로에서 실사진 업로드를 켠다.
#   • attach 경로 신원 게이트는 upload_post 내부(_assert_publish_identity →
#     _identity_gate_mode)가 PUBLISH_MODE=="attach" 를 보고 자동으로 strict 상향한다
#     (오계정 발행 방지). 이 모듈은 그 계약을 존중만 한다.
#   • QA 미통과(_qa_failed/_failed) 초안은 발행을 거부한다(초안 미통과분 실발행 차단).
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from config.logger import get_logger

log = get_logger(__name__)

# 사진 런 data["category"] 기본 플레이스홀더(experience_writer._PHOTO_CATEGORY).
# 발행 시 이 라벨은 반드시 사용자가 고른 실제 블로그 메뉴 라벨로 대체돼야 한다.
_PLACEHOLDER_CATEGORY = "사진후기"


# ──────────────────────────────────────────────────────────────────────────────
# settings / 상태 헬퍼 (순수·네트워크 없음)
# ──────────────────────────────────────────────────────────────────────────────
def _setting(name: str, default: Any) -> Any:
    """settings 값을 안전하게 읽는다(미로드/미정의면 기본값)."""
    try:
        from config import settings as s
        return getattr(s, name, default)
    except (ImportError, AttributeError):
        return default


def _publish_mode() -> str:
    """현재 발행 모드('spawn' | 'attach'). 미정의면 'spawn'(현행 기본)."""
    return str(_setting("PUBLISH_MODE", "spawn")).lower()


def _is_stopped(stop_flag) -> bool:
    """stop_flag(threading.Event 유사 객체)가 set 됐는지 안전하게 판정."""
    if stop_flag is None:
        return False
    try:
        return bool(stop_flag.is_set())
    except AttributeError:
        return False


def _stop_switch_hold() -> str:
    """운영 정지 신호. 계속 가능하면 "", 아니면 "estop" | "pause".

    `publish_state_bridge.guard_new_item` 과 **같은 판정**을 쓴다(파일 기반이라 GUI 가
    떠 있지 않아도 듣는다). 브리지·kill_switch 가 없는 설치에서는 "" — 모듈 부재로
    기존 사용자의 수동 발행을 막지 않는다(기존 fail-open 계약 보존).
    """
    try:
        from core import publish_state_bridge as _psb
        return _psb.guard_new_item() or ""
    except Exception:  # noqa: BLE001 — 모듈 부재/판정 실패 = 기존 동작
        return ""


def _bridge():
    """publish_state_bridge 지연 임포트. 없으면 None(기존 동작 — fail-open)."""
    try:
        from core import publish_state_bridge as _psb
        return _psb
    except Exception:  # noqa: BLE001 — 모듈 부재 = 상태 기록 없이 예전대로
        return None


def _record(psb, run_id: str, state: str, **fields) -> bool:
    """단건 발행 상태 기록(브리지 없으면 아무 것도 하지 않는다).

    기록 실패는 **발행을 막지 않는다** — 다만 그 경우 다음 클릭의 중복 차단이
    동작하지 않으므로 경고를 남긴다.
    """
    if psb is None or not run_id:
        return False
    try:
        return bool(psb.mark_manual(run_id, state, **fields))
    except Exception:  # noqa: BLE001
        log.warning("단건 발행 상태 기록 실패(%s) — 중복 차단이 이번엔 동작하지 않습니다",
                    state)
        return False


def _classify(psb, raw) -> tuple:
    """업로드 결과 → (publish_state, 사유). 판정기는 브리지 것을 그대로 쓴다."""
    if psb is not None:
        try:
            return psb.classify_upload_result(raw)
        except Exception:  # noqa: BLE001 — 판정 실패는 아래 보수적 폴백
            log.debug("업로드 결과 판정 실패", exc_info=True)
    ok = bool(raw.get("ok")) if isinstance(raw, dict) else bool(raw)
    return ("reserved", "fallback_ok") if ok else ("failed", "fallback_fail")


def _notify(progress_cb: Optional[Callable[[str], None]], phase: str) -> None:
    """진행 콜백을 안전하게 호출(콜백 예외가 발행을 깨지 않게)."""
    if progress_cb is None:
        return
    try:
        progress_cb(phase)
    except Exception:  # noqa: BLE001 — 잘못된 콜백이 발행 흐름을 막지 않게
        log.debug("progress_cb 호출 실패(무시)", exc_info=True)


# ──────────────────────────────────────────────────────────────────────────────
# post_data 조립 (순수 함수 — 테스트 직접 대상)
# ──────────────────────────────────────────────────────────────────────────────
def build_post_data(data: Dict[str, Any], target_category: str) -> Dict[str, Any]:
    """사진 런 data → upload_post 용 post_data 조립.

    - 본문은 ``content_html`` 을 그대로 사용(로컬 img src 0 인 네이버 본문).
    - ``category`` 는 반드시 **사용자가 고른 실제 메뉴 라벨**(``target_category``)로
      넣는다 — data 의 플레이스홀더("사진후기")를 대체한다.
    - ``_force_image_insert=True`` 를 실어 전역 SKIP_IMAGE_INSERT 를 우회, 실사진을
      업로드한다(전역은 불변).

    Args:
        data: experience_writer.generate_experience_post 반환 dict.
        target_category: 사용자가 선택한 블로그 메뉴 라벨(비어 있으면 안 됨).

    Returns:
        upload_post 계약을 만족하는 post_data dict.
    """
    return {
        "title": str(data.get("title") or "").strip(),
        "content_html": data.get("content_html") or "",
        "tags": list(data.get("tags") or []),
        "category": str(target_category or "").strip(),
        "_force_image_insert": True,
    }


def existing_image_paths(data: Dict[str, Any]) -> List[str]:
    """data["image_paths"] 중 **실제 존재하는 파일만** 순서 유지로 필터(방어).

    비-list image_paths · 비문자열 항목 · 경로 판독 실패에 모두 견딘다(예외를 절대
    던지지 않는다 — 이 함수 결과가 publish_photo_run 반환 계약을 깨지 않게).
    """
    result: List[str] = []
    raw_list = (data or {}).get("image_paths") or []
    if not isinstance(raw_list, (list, tuple)):
        return result
    for raw in raw_list:
        if not raw or not isinstance(raw, (str, Path)):
            continue
        try:
            if Path(raw).is_file():
                result.append(str(raw))
        except (OSError, ValueError, TypeError):
            # 경로 판독 실패(권한·인코딩·형식 등)는 조용히 제외 — 발행을 막지 않는다.
            log.debug("이미지 경로 확인 실패(제외): %r", raw, exc_info=True)
    return result


def list_target_menus() -> List[str]:
    """활성 프로필의 category_map.json 에 학습된 블로그 메뉴 라벨 목록.

    GUI 카테고리 드롭다운의 소스. 어떤 실패(맵 없음·손상·프로필 계층 부재)에도
    빈 리스트를 돌려준다 — GUI 는 빈 리스트면 직접 입력 UI 로 폴백한다.
    맵 경로 해석은 uploader 의 프로필 규칙(``_category_map_path``)을 재사용한다.
    """
    try:
        from core.naver_uploader import _category_map_path
        from config import safe_load_json
    except ImportError:
        return []
    try:
        path = _category_map_path()
        if path is None or not path.exists():
            return []
        cmap = safe_load_json(path)
        if not isinstance(cmap, dict):
            return []
        menus = cmap.get("menus") or cmap.get("examplebrand_menus") or {}
        if not isinstance(menus, dict):
            return []
        return [str(label) for label in menus.keys() if str(label).strip()]
    except (OSError, ValueError) as exc:
        log.debug("메뉴 목록 로드 실패(무시): %s", exc)
        return []


def _resolve_target_no(label: str):
    """target 카테고리 라벨을 블로그 메뉴 번호로 **발행 전** 사전 해석.

    naver_uploader._resolve_category_no(label, None) 을 그대로 재사용한다(그 파일
    무수정). outer fallback 을 None 으로 주어 "번호가 안 나오는 미매핑"을 None 으로
    드러낸다. None=미매핑(또는 해석 실패 — fail-safe 로 미매핑 취급, 오발행보다 안전).
    """
    try:
        from core.naver_uploader import _resolve_category_no
        return _resolve_category_no(str(label or "").strip(), None)
    except Exception:  # noqa: BLE001 — 해석 실패는 미매핑으로 간주
        log.debug("카테고리 사전 해석 실패(미매핑 처리)", exc_info=True)
        return None


# ──────────────────────────────────────────────────────────────────────────────
# 드라이버 생명주기 (PUBLISH_MODE 존중 — auto_scheduler:1868-1928 재현, 그 모듈 무수정)
# ──────────────────────────────────────────────────────────────────────────────
def _acquire_attach_driver():
    """attach 모드 드라이버 획득 — 세션 크롬 자동 기동 후 원격 디버깅 포트에 붙는다.

    auto_scheduler 의 attach 블록과 동일 로직(ensure_session → started 면 포트 오픈
    대기 → attach_to_session). naver_login 은 호출하지 않는다(세션이 이미 로그인).

    Returns:
        webdriver.Chrome — attach 성공. None — 실패(발행은 fail-closed 로 중단).
    """
    port = int(_setting("CHROME_ATTACH_PORT", 9222))
    if bool(_setting("CHROME_ATTACH_AUTOSTART", True)):
        try:
            from core import chrome_session
            status = chrome_session.ensure_session(port)
            # 방금 띄운 크롬('started')만 디버깅 포트가 열릴 때까지 잠깐 폴링해
            # 콜드 스타트 레이스를 흡수한다('already'/기타는 대기 불필요).
            if status == "started":
                if not chrome_session.wait_until_running(port, timeout=10.0):
                    log.warning("  ⚠️  발행 세션 크롬 디버깅 포트가 제때 열리지 않음 — "
                                "attach 를 시도합니다(실패 시 fail-closed)")
        except Exception as exc:  # noqa: BLE001 — 자동 기동 실패는 무해(attach 시도로 판정)
            log.warning("  ⚠️  발행 세션 자동 기동 실패(무시): %s", exc)
    from core.naver_uploader import attach_to_session
    return attach_to_session(port)


def _release(driver, mode: str) -> None:
    """발행 드라이버 정리 — auto_scheduler._release_driver 재사용(attach 는 quit 생략).

    권위 프리미티브를 재사용해 attach=세션 유지 / spawn=quit 불변식을 auto_scheduler
    와 동기화한다. 임포트 실패 시에도 spawn 드라이버를 누수시키지 않게 로컬 폴백을 둔다.
    """
    if driver is None:
        return
    try:
        from scheduler.auto_scheduler import _release_driver
        _release_driver(driver, mode)
        return
    except Exception:  # noqa: BLE001 — 임포트/실행 실패는 로컬 폴백으로
        log.debug("auto_scheduler._release_driver 재사용 실패 — 로컬 폴백", exc_info=True)
    # 로컬 폴백: attach 는 세션 유지(닫지 않음), spawn 만 quit.
    if str(mode).lower() == "attach":
        return
    try:
        driver.quit()
    except Exception:  # noqa: BLE001
        log.debug("드라이버 종료 실패(무시)", exc_info=True)


# ──────────────────────────────────────────────────────────────────────────────
# 단건 발행 진입점 (오직 사용자 클릭으로만 호출)
# ──────────────────────────────────────────────────────────────────────────────
def publish_photo_run(data: Dict[str, Any],
                      target_category: str,
                      reserve_datetime: Optional[str] = None,
                      auth_event=None,
                      auth_cb=None,
                      progress_cb: Optional[Callable[[str], None]] = None,
                      stop_flag=None,
                      force_republish: bool = False) -> Dict[str, Any]:
    """사진 런 data 하나를 네이버에 발행한다(단건). **사용자 클릭 전용.**

    자동/배치가 절대 부르지 않는다(하드 게이트). run_pipeline(엑셀 큐)을 재사용하지
    않고, upload_post 프리미티브를 PUBLISH_MODE(spawn/attach)에 맞춰 직접 호출한다.

    Args:
        data: experience_writer.generate_experience_post 반환 dict.
        target_category: 사용자가 선택한 블로그 메뉴 라벨(필수·비어 있으면 거부).
                         data 의 플레이스홀더("사진후기")가 아니라 실제 메뉴여야 한다.
        reserve_datetime: "YYYY-MM-DD HH:MM" 이면 예약, None 이면 즉시발행(MVP 기본).
        auth_event: spawn 로그인 시 GUI 보안인증 신호용(attach 는 미사용).
        auth_cb: spawn 로그인 보안인증 상태 콜백(attach 는 미사용).
        progress_cb: 진행 단계 문자열 콜백(선택). 예외는 삼킨다.
        stop_flag: threading.Event 유사 객체. set 되면 발행 전 단계에서 중단.
        force_republish: 이미 발행을 시도한 기록이 있는 글을 **사람이 확인한 뒤**
            그래도 다시 올릴 때만 True. 기본 False 에서는 결말이 미상이거나 이미
            올라간 글을 다시 올리지 않는다(`publish_blocked`). 자동·배치 경로는
            이 인자를 절대 쓰지 않는다 — 사용자가 경고 모달을 읽고 누른 경우 전용.

    Returns:
        {"ok": True, "title", "category", "category_resolved": bool,
         "image_count", "reserved"} 또는 {"ok": False, "error": <코드>, ...}.
        ``category_resolved`` — target_category 가 발행 전에 실제 블로그 메뉴 번호로
        해석됐는지. False 면 네이버 기본 메뉴로 올라갈 수 있으니 호출부(GUI)는 성공
        메시지를 "메뉴명 단정" 대신 "기본 메뉴 가능 — 블로그에서 확인"으로 정직히 표시해야 한다.
        발행을 시도조차 안 한 실패는 error ∈ {invalid_data, qa_failed, no_category,
        empty_content, empty_title, category_unmapped, stopped, attach_failed,
        login_failed}. 업로드 자체 실패는 upload_failed.

        ``category_unmapped`` — 학습된 블로그 메뉴가 있는데 선택 라벨이 그 목록에
        없어(오발행 위험) **발행을 차단**한 경우. 학습된 메뉴가 아직 없으면(첫 발행)
        차단하지 않되 category_resolved=False 로 표시한다(호출부가 사용자에게 고지).
    """
    # ── 1. 입력·QA 재검증 방어(발행 시도 전 fail-closed) ──────────────────
    if not isinstance(data, dict):
        return {"ok": False, "error": "invalid_data"}

    if data.get("_qa_failed") or data.get("_failed"):
        reason = str(data.get("_qa_failed_reason")
                     or data.get("_failed_reason") or "qa_failed")
        log.info("  ⛔ 사진 발행 거부 — QA 미통과 초안(%s)", reason[:80])
        return {"ok": False, "error": "qa_failed", "reason": reason}

    label = str(target_category or "").strip()
    if not label:
        # 카테고리 미선택 → 여행 글이 엉뚱한 기본 메뉴로 조용히 올라가는 사고 방지.
        log.info("  ⛔ 사진 발행 거부 — 대상 카테고리 미선택")
        return {"ok": False, "error": "no_category"}

    title = str(data.get("title") or "").strip()
    if not title:
        return {"ok": False, "error": "empty_title"}

    content_html = data.get("content_html") or ""
    if not str(content_html).strip():
        return {"ok": False, "error": "empty_content"}

    if _is_stopped(stop_flag):
        return {"ok": False, "error": "stopped"}

    # ── 1.5 운영 정지 스위치 (2026-09-23) ────────────────────────────────
    # 사용자 클릭 경로라도 **일시중지·비상정지는 듣는다.** 정지는 "이 계정으로는
    # 지금 아무 것도 내보내지 마라"는 명시적 의사표시이고, 여기서 예외를 두면
    # 사고 대응으로 계정을 세워 둔 동안에도 사진/상품 글이 그대로 나간다.
    # 브라우저를 띄우기 **전**에 막는다 — 로그인도 외부 행동이다.
    _hold = _stop_switch_hold()
    if _hold:
        log.info("  ⛔ 사진 발행 거부 — 운영 정지 상태(%s)", _hold)
        return {"ok": False, "error": _hold}

    # ── 1.6 중복 발행 차단 + 상태 기록 준비 (2026-09-23) ──────────────────
    # 단건 경로에는 작업큐 항목도 보관본 _status.json 도 없어서, 지금까지 **어디에도**
    # "이 글을 올렸다"가 남지 않았다. 그래서 같은 사진 런을 두 번 누르면 두 번
    # 올라갔고, 예약 응답이 애매하게 끝난 글도 다음 클릭에 그대로 다시 나갔다.
    # `publish_state_bridge` 의 단건 원장을 **유일 접점**으로 써서 둘 다 막는다.
    _psb = _bridge()
    run_id = ""
    slug = str(data.get("slug") or "").strip()
    if _psb is not None:
        try:
            run_id = _psb.manual_run_id(title=title,
                                        content=str(content_html), slug=slug)
            reason = _psb.manual_blocked_reason(run_id, title=title, slug=slug)
        except Exception:  # noqa: BLE001 — 판정 실패가 사용자 발행을 막지 않는다
            log.debug("단건 발행 중복 판정 실패(무시)", exc_info=True)
            reason = ""
        if reason and not force_republish:
            log.info("  ⛔ 사진 발행 거부 — 이미 발행 시도한 글(%s)", reason)
            return {"ok": False, "error": "publish_blocked", "reason": reason,
                    "message": _psb.manual_block_message(reason),
                    "run_id": run_id, "title": title}
        if reason and force_republish:
            log.warning("  ⚠️ 사용자 확인 후 재발행 — 이전 상태(%s) '%s'",
                        reason, title[:30])

    # ── 2. 드라이버 생명주기 — PUBLISH_MODE 존중(spawn/attach) ──────────────
    mode = _publish_mode()
    driver = None
    try:
        post_data = build_post_data(data, label)
        image_paths = existing_image_paths(data)

        # ── 2.5 카테고리 사전 해석 — 조용한 오발행 + 허위 성공 보고 차단 ────────
        # naver_uploader 를 안 고치고, 그 _resolve_category_no 를 발행 전에 재사용해
        # target 라벨이 실제 메뉴 번호로 풀리는지 먼저 확인한다.
        menus = list_target_menus()
        resolved_no = _resolve_target_no(label)
        if menus:
            # 학습된 메뉴가 있으면 라벨이 그 목록에 있고 실제 번호로 풀려야 한다.
            # 목록에 없거나 번호가 안 나오면 blind fallback 으로 엉뚱한 메뉴에 붙을 수
            # 있어 **발행을 차단**한다(GUI 는 목록에서만 고르게 하는 게 정석).
            if label not in menus or resolved_no is None:
                log.info("  ⛔ 사진 발행 거부 — 카테고리 '%s' 미매핑(학습 메뉴 %d개)",
                         label, len(menus))
                return {"ok": False, "error": "category_unmapped",
                        "category": label, "menus": menus}
            category_resolved = True
        else:
            # 학습된 메뉴 없음(첫 발행) — 차단하지 않되 기본 메뉴 가능성을 정직히 표시.
            # (GUI 확인 모달이 "기본 메뉴로 올라갈 수 있음"을 이미 고지한다.)
            category_resolved = resolved_no is not None

        if mode == "attach":
            _notify(progress_cb, "attach")
            driver = _acquire_attach_driver()
            if driver is None:
                # 세션 미기동/미로그인 — fail-closed(발행 시도 안 함).
                return {"ok": False, "error": "attach_failed"}
        else:
            _notify(progress_cb, "login")
            from core.naver_uploader import init_driver, naver_login
            driver = init_driver()
            if not naver_login(driver, auth_event=auth_event, auth_cb=auth_cb):
                # 로그인 실패 — 발행 시도 안 함. driver 정리는 finally 가 수행.
                return {"ok": False, "error": "login_failed"}

        # 로그인/attach 이후 중단 요청이 들어왔으면 발행 직전에 멈춘다.
        if _is_stopped(stop_flag):
            return {"ok": False, "error": "stopped"}

        # ── 3. 업로드 1회(재시도·팝업정리·신원게이트·카테고리·태그·예약은 내부 처리) ──
        # ★ write-ahead: 외부 확정 행동(발행/예약 버튼) **전에** `reserving` 을 디스크에
        #   적는다. 여기서 프로세스가 죽어도 "이 글은 버튼까지 갔다"가 남아, 다음 클릭이
        #   결말 미상인 글을 다시 올리지 않는다.
        _record(_psb, run_id, "reserving", title=title, slug=slug,
                category=label, publish_at=reserve_datetime or "",
                forced=bool(force_republish) or None)
        _notify(progress_cb, "uploading")
        from core import naver_uploader as _nu
        raw = (_nu.upload_post_ex(driver, post_data, image_paths, reserve_datetime)
               if callable(getattr(_nu, "upload_post_ex", None))
               else _nu.upload_post(driver, post_data, image_paths, reserve_datetime))
        ok = bool(raw.get("ok")) if isinstance(raw, dict) else bool(raw)

        # 결과 → 상태. 애매한 응답(확정 버튼은 눌렀는데 성공 신호 없음)은
        # `unconfirmed` 로 남는다 — 실패로 적어 다음에 다시 올리는 것이 중복 발행이다.
        state, why = _classify(_psb, raw)
        _record(_psb, run_id, state, title=title, slug=slug, reason=why,
                category=label, publish_at=reserve_datetime or "")

        if ok:
            log.info("  ✅ 사진 단건 발행 성공: '%s' [%s] 사진 %d장 %s (카테고리 해석=%s)",
                     title[:30], label, len(image_paths),
                     f"예약({reserve_datetime})" if reserve_datetime else "즉시",
                     "됨" if category_resolved else "미확정")
            return {
                "ok": True,
                "title": title,
                "category": label,
                "category_resolved": category_resolved,
                "image_count": len(image_paths),
                "reserved": bool(reserve_datetime),
                "publish_state": state,
                "run_id": run_id,
            }
        return {"ok": False, "error": "upload_failed",
                "publish_state": state, "reason": why, "run_id": run_id}
    except Exception as exc:  # noqa: BLE001 — 어떤 예외도 GUI 를 깨지 않게 dict 로 반환
        log.error("사진 단건 발행 예상외 오류", exc_info=True)
        return {"ok": False,
                "error": f"{type(exc).__name__}: {str(exc)[:100]}"}
    finally:
        # attach 세션은 유지(다음 발행 재사용), spawn 은 quit — _release_driver 규약.
        _release(driver, mode)
