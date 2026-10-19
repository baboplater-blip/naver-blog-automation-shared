# core/channel_dispatcher.py
# 외부 유입 채널 통합 디스패처
# ① 카카오 채널/오픈채팅 자동 발행 알림
# ② 네이버 카페 반자동 배포 (승인 큐 방식)
#
# 카카오 알림 방식:
#   A. 나에게 보내기  — 무료, 즉시, KAKAO_ACCESS_TOKEN만 있으면 됨
#   B. 카카오 채널    — 비즈니스 채널 구독자 전체에 브로드캐스트 (채널 개설 필요)
#   C. 오픈채팅 메시지 — 클립보드+pyautogui 방식 (KakaoTalk 설치 필요)
#
# 네이버 카페 배포:
#   1. Claude가 카페 맞춤 소개글 초안 자동 생성
#   2. GUI 승인 큐에서 미리보기 → 승인/거부
#   3. 승인 시 Selenium으로 카페 게시판에 자동 등록
from __future__ import annotations

import json, os, random, re, sys, subprocess, urllib.request, urllib.parse, urllib.error, time
from datetime import datetime, date
from pathlib import Path
from typing import Optional

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.logger import get_logger
try:
    from config.settings import HTTP_REQUEST_TIMEOUT, SUBPROCESS_TIMEOUT, MAX_DISPATCH_QUEUE
except (ImportError, AttributeError):
    HTTP_REQUEST_TIMEOUT = 10
    SUBPROCESS_TIMEOUT   = 180
    MAX_DISPATCH_QUEUE   = 200
log = get_logger(__name__)

BASE_DIR      = Path(__file__).parent.parent
from config import profile as _nbaprofile
_DATA_DIR = _nbaprofile.app_data_root()

CHANNEL_DB    = _DATA_DIR / "logs" / "channel_config.json"
DISPATCH_QUEUE = _DATA_DIR / "logs" / "dispatch_queue.json"
DISPATCH_LOG  = _DATA_DIR / "logs" / "dispatch_log.json"


# ──────────────────────────────────────────────────────
# DB 관리
# ──────────────────────────────────────────────────────
# 카카오 토큰은 파일 저장 시 Fernet 암호화("ENC:" 접두) 상태로 보관하고,
# 메모리로 로드할 때만 복호화한다. 아래 두 키가 암호화 대상.
_KAKAO_SECRET_KEYS = ("access_token", "channel_token")


def _decrypt_kakao_tokens(cfg: dict) -> dict:
    """cfg["kakao"] 의 토큰 필드를 복호화한 복사본 반환(하위호환 fail-open).

    - "ENC:" 접두 암호문 → 평문 복호화.
    - 접두가 없으면(기존 평문 저장 파일) crypto.decrypt 가 값을 그대로 돌려줘 호환.
    - crypto 모듈이 없거나 복호화 실패 시 원본 cfg 를 그대로 반환(fail-open).
    """
    kakao = cfg.get("kakao")
    if not isinstance(kakao, dict):
        return cfg
    try:
        from config.crypto import decrypt
    except ImportError:
        log.debug("crypto 모듈 없음 — 카카오 토큰 복호화 건너뜀", exc_info=True)
        return cfg
    kakao_out = dict(kakao)
    for key in _KAKAO_SECRET_KEYS:
        val = kakao_out.get(key, "")
        if isinstance(val, str) and val:
            kakao_out[key] = decrypt(val)
    out = dict(cfg)
    out["kakao"] = kakao_out
    return out


def _encrypt_kakao_tokens(cfg: dict) -> dict:
    """cfg["kakao"] 의 토큰 필드를 암호화한 복사본 반환(저장 직전 호출).

    - 평문 토큰 → "ENC:" 암호문.
    - 이미 "ENC:" 접두면 이중 암호화하지 않음.
    - crypto 미가용 시 원본 반환(fail-open) — 저장은 되게 하되 평문 위험은
      _save_channel_cfg 호출 측 책임이 아니라 crypto 부재라는 예외 상황.
    """
    kakao = cfg.get("kakao")
    if not isinstance(kakao, dict):
        return cfg
    try:
        from config.crypto import encrypt
    except ImportError:
        log.debug("crypto 모듈 없음 — 카카오 토큰 암호화 건너뜀", exc_info=True)
        return cfg
    kakao_out = dict(kakao)
    for key in _KAKAO_SECRET_KEYS:
        val = kakao_out.get(key, "")
        if isinstance(val, str) and val and not val.startswith("ENC:"):
            kakao_out[key] = encrypt(val)
    out = dict(cfg)
    out["kakao"] = kakao_out
    return out


def _load_channel_cfg() -> dict:
    """
    {
      "kakao": {
        "enabled":       true,
        "access_token":  "ENC:...",   # 저장은 암호문, 로드 시 복호화됨
        "channel_token": "ENC:...",   # 카카오 채널 발송용 (선택)
        "send_to_me":    true,       # 나에게 보내기
        "send_channel":  false,      # 채널 브로드캐스트
        "template": "📢 새 글 발행!\n\n{title}\n\n{summary}\n\n👉 {url}"
      },
      "cafe": {
        "enabled": true,
        "targets": [
          {
            "id":      "cafe_001",
            "name":    "복지넷",
            "cafe_url":"https://cafe.naver.com/bokji",
            "board_id":"1",
            "categories": ["복지","바우처","의료"],
            "intro":   "안녕하세요! 정부지원 정보를 공유합니다."
          }
        ],
        "auto_approve": false,
        "min_delay_sec": 3600
      }
    }

    ⚠️ 반환 dict 의 카카오 토큰은 **복호화된 평문**이다(발송에 필요). 파일 자체는
    항상 암호문으로 보관한다. 이 반환값을 그대로 파일에 다시 쓰지 말 것 —
    저장은 반드시 _save_channel_cfg(암호화 적용)를 거친다.
    """
    from config import safe_load_json
    data = safe_load_json(CHANNEL_DB)
    if data is None:
        return _default_channel_cfg()
    return _decrypt_kakao_tokens(data)

def _save_channel_cfg(cfg: dict):
    """채널 설정 저장 — 카카오 토큰은 암호화(ENC:)한 뒤 원자적 기록."""
    from config import atomic_write_json
    atomic_write_json(CHANNEL_DB, _encrypt_kakao_tokens(cfg))

def _default_channel_cfg() -> dict:
    return {
        "kakao": {
            "enabled":      False,
            "access_token": "",
            "channel_token":"",
            "send_to_me":   True,
            "send_channel": False,
            "template": "📢 새 정보 발행!\n\n📌 {title}\n\n{summary}\n\n👉 {url}\n\n#정부지원 #복지정보",
        },
        "cafe": {
            "enabled":       False,
            "targets":       [],
            "auto_approve":  False,
            "min_delay_sec": 3600,
        }
    }

def _load_queue() -> list:
    from config import safe_load_json
    return safe_load_json(DISPATCH_QUEUE, default=list)

def _save_queue(queue: list):
    from config import atomic_write_json
    atomic_write_json(DISPATCH_QUEUE, queue)

def _load_log() -> list:
    from config import safe_load_json
    return safe_load_json(DISPATCH_LOG, default=list)

def _append_log(entry: dict):
    logs = _load_log()
    logs.append({**entry, "logged_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
    logs = logs[-MAX_DISPATCH_QUEUE:]   # 최근 N건만 보관
    from config import atomic_write_json
    atomic_write_json(DISPATCH_LOG, logs)


# ──────────────────────────────────────────────────────
# ① 카카오 알림
# ──────────────────────────────────────────────────────
def _kakao_send_to_me(access_token: str, title: str,
                      message: str, url: str) -> bool:
    """카카오톡 나에게 보내기 (기존 기능 강화)"""
    try:
        template = {
            "object_type": "feed",
            "content": {
                "title": title[:50],
                "description": message[:180],
                "link": {
                    "web_url":        url,
                    "mobile_web_url": url,
                }
            },
            "buttons": [
                {"title": "블로그에서 보기", "link": {"web_url": url, "mobile_web_url": url}}
            ]
        }
        data = urllib.parse.urlencode({
            "template_object": json.dumps(template, ensure_ascii=False)
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://kapi.kakao.com/v2/api/talk/memo/default/send",
            data=data,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type":  "application/x-www-form-urlencoded",
            }
        )
        with urllib.request.urlopen(req, timeout=HTTP_REQUEST_TIMEOUT) as resp:
            result = json.loads(resp.read())
            ok = result.get("result_code") == 0
            if ok:
                log.info("카카오 나에게 보내기 완료")
            return ok
    except urllib.error.URLError as e:
        log.warning(f"카카오 나에게 보내기 실패 (네트워크): {e}")
        return False
    except json.JSONDecodeError as e:
        log.warning(f"카카오 나에게 보내기 실패 (응답 파싱): {e}")
        return False


def _kakao_channel_broadcast(channel_token: str, title: str,
                              message: str, url: str) -> bool:
    """카카오 채널 구독자 전체 브로드캐스트 메시지"""
    try:
        template = {
            "object_type": "feed",
            "content": {
                "title":       title[:50],
                "description": message[:180],
                "link": {
                    "web_url":        url,
                    "mobile_web_url": url,
                }
            },
            "buttons": [
                {"title": "자세히 보기", "link": {"web_url": url, "mobile_web_url": url}}
            ]
        }
        data = urllib.parse.urlencode({
            "template_object": json.dumps(template, ensure_ascii=False)
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://kapi.kakao.com/v2/api/talk/message/default/send",
            data=data,
            headers={
                "Authorization": f"Bearer {channel_token}",
                "Content-Type":  "application/x-www-form-urlencoded",
            }
        )
        with urllib.request.urlopen(req, timeout=HTTP_REQUEST_TIMEOUT) as resp:
            result = json.loads(resp.read())
            ok = result.get("result_code") == 0
            if ok:
                log.info("카카오 채널 브로드캐스트 완료")
            return ok
    except urllib.error.URLError as e:
        log.warning(f"카카오 채널 발송 실패 (네트워크): {e}")
        return False
    except json.JSONDecodeError as e:
        log.warning(f"카카오 채널 발송 실패 (응답 파싱): {e}")
        return False


def send_kakao_notification(post_data: dict, url: str) -> dict:
    """
    글 발행 후 카카오 알림을 발송합니다.
    설정에 따라 나에게 보내기 / 채널 브로드캐스트 를 수행합니다.

    Returns: {"sent_to_me": bool, "sent_channel": bool}
    """
    cfg = _load_channel_cfg().get("kakao", {})
    if not cfg.get("enabled"):
        return {"sent_to_me": False, "sent_channel": False}

    title   = post_data.get("title", "")
    summary = post_data.get("summary", "")
    tags    = post_data.get("tags", [])

    # 템플릿 적용
    template = cfg.get("template",
        "📢 새 정보 발행!\n\n📌 {title}\n\n{summary}\n\n👉 {url}")
    tag_str = " ".join(f"#{t}" for t in tags[:5])
    message = template.format(
        title   = title,
        summary = summary,
        url     = url,
        tags    = tag_str,
    )

    results = {"sent_to_me": False, "sent_channel": False}
    access_token  = cfg.get("access_token", "")
    channel_token = cfg.get("channel_token", "")

    if cfg.get("send_to_me") and access_token:
        results["sent_to_me"] = _kakao_send_to_me(
            access_token, title, message, url)

    # 연속 API 호출 사이 지터 (rate-limit 방지)
    if cfg.get("send_to_me") and cfg.get("send_channel"):
        time.sleep(random.uniform(1, 3))

    if cfg.get("send_channel") and channel_token:
        results["sent_channel"] = _kakao_channel_broadcast(
            channel_token, title, message, url)

    _append_log({
        "channel":  "kakao",
        "title":    title,
        "url":      url,
        "results":  results,
    })
    return results


def test_kakao_connection(access_token: str = "") -> dict:
    """카카오 토큰 유효성 검사 — 프로필 조회"""
    token = access_token
    if not token:
        token = _load_channel_cfg().get("kakao", {}).get("access_token", "")
    if not token:
        return {"ok": False, "error": "토큰 없음"}
    try:
        req = urllib.request.Request(
            "https://kapi.kakao.com/v2/user/me",
            headers={"Authorization": f"Bearer {token}"}
        )
        with urllib.request.urlopen(req, timeout=HTTP_REQUEST_TIMEOUT) as resp:
            data = json.loads(resp.read())
            nickname = (data.get("kakao_account", {})
                            .get("profile", {})
                            .get("nickname", "알 수 없음"))
            return {"ok": True, "nickname": nickname, "id": data.get("id")}
    except urllib.error.URLError as e:
        return {"ok": False, "error": f"네트워크 오류: {e}"}
    except json.JSONDecodeError as e:
        return {"ok": False, "error": f"응답 파싱 오류: {e}"}


# ──────────────────────────────────────────────────────
# ② 네이버 카페 반자동 배포
# ──────────────────────────────────────────────────────

# 카테고리 → 타겟 카페 자동 매칭 테이블 (기본값, GUI에서 커스터마이즈 가능)
_DEFAULT_CAFE_TARGETS = [
    {
        "id":         "bokji",
        "name":       "복지넷 카페",
        "cafe_url":   "https://cafe.naver.com/bokjikorea",
        "board_id":   "9",
        "categories": ["복지", "바우처", "의료", "default"],
        "intro":      "안녕하세요! 2026년 최신 정부지원 정보를 공유합니다. 도움이 되셨으면 합니다.",
    },
    {
        "id":         "jaetech",
        "name":       "재테크 슈퍼스타",
        "cafe_url":   "https://cafe.naver.com/moneystory",
        "board_id":   "18",
        "categories": ["청년", "취업", "교육"],
        "intro":      "청년 정부지원 혜택 정리했습니다. 신청기한 놓치지 마세요!",
    },
    {
        "id":         "juge",
        "name":       "주거정보 카페",
        "cafe_url":   "https://cafe.naver.com/jugejung",
        "board_id":   "5",
        "categories": ["주거"],
        "intro":      "주거 관련 정부지원 정보 공유합니다.",
    },
]


def _generate_cafe_draft(post_data: dict, cafe_target: dict) -> str:
    """
    Claude로 카페 게시글 초안을 생성합니다.
    카페 특성에 맞게 톤을 자동 조절합니다.
    """
    title   = post_data.get("title", "")
    summary = post_data.get("summary", "")
    url     = post_data.get("url", "#")
    tags    = post_data.get("tags", [])[:5]
    intro   = cafe_target.get("intro", "")
    cafe_nm = cafe_target.get("name", "")

    prompt = f"""다음 블로그 글을 네이버 카페에 공유하는 간결한 소개글을 작성해주세요.

카페명: {cafe_nm}
블로그 글 제목: {title}
블로그 글 요약: {summary}
카페 기본 소개: {intro}

[소개글 작성 규칙]
1. 3~5문장 (너무 길면 스팸으로 오해)
2. 커뮤니티 친화적 톤 (딱딱하지 않게)
3. 핵심 혜택 1~2가지 언급
4. 마지막에 "자세한 내용은 아래 링크에서!" 형태로 마무리
5. 해시태그 3개 이내
6. 광고성 문구 금지

소개글만 출력 (제목/URL 제외, 순수 본문 텍스트):"""

    try:
        env = os.environ.copy()
        env.pop("ANTHROPIC_API_KEY", None)
        result = subprocess.run(
            ["claude", "-p", prompt, "--output-format", "text"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=SUBPROCESS_TIMEOUT, env=env,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except subprocess.TimeoutExpired:
        log.warning(f"초안 생성 타임아웃 ({SUBPROCESS_TIMEOUT}초)")
    except FileNotFoundError:
        log.warning("Claude CLI 미설치 — 기본 템플릿 사용")
    except subprocess.CalledProcessError as e:
        log.warning(f"초안 생성 오류 (exit {e.returncode}): {e}")

    # 폴백: 기본 템플릿
    tag_str = " ".join(f"#{t}" for t in tags)
    return (f"{intro}\n\n"
            f"📌 {title}\n\n"
            f"{summary}\n\n"
            f"자세한 내용은 아래 링크에서 확인하세요!\n\n"
            f"{tag_str}")


def enqueue_cafe_posts(post_data: dict, url: str) -> int:
    """
    발행된 글을 해당 카테고리의 타겟 카페 배포 큐에 추가합니다.
    Returns: 큐에 추가된 카페 수
    """
    cfg      = _load_channel_cfg()
    cafe_cfg = cfg.get("cafe", {})
    if not cafe_cfg.get("enabled"):
        return 0

    category = post_data.get("category", "default")
    title    = post_data.get("title", "")
    targets  = cafe_cfg.get("targets", [])

    # 기본 타겟이 없으면 빌트인 기본값 사용
    if not targets:
        targets = _DEFAULT_CAFE_TARGETS

    matched = [t for t in targets
               if category in t.get("categories", []) or
               "default" in t.get("categories", [])]

    if not matched:
        log.info(f"카페 배포 대상 없음 (카테고리: {category})")
        return 0

    queue = _load_queue()

    # 이미 같은 글이 같은 카페에 있으면 건너뜀
    existing_keys = {(q["post_title"], q["cafe_id"]) for q in queue}

    added = 0
    for target in matched:
        key = (title, target["id"])
        if key in existing_keys:
            continue

        # Claude로 초안 생성
        draft = _generate_cafe_draft({**post_data, "url": url}, target)

        queue.append({
            "id":         f"q_{datetime.now().strftime('%m%d%H%M%S')}_{target['id']}",
            "post_title": title,
            "post_url":   url,
            "category":   category,
            "cafe_id":    target["id"],
            "cafe_name":  target["name"],
            "cafe_url":   target["cafe_url"],
            "board_id":   target["board_id"],
            "draft":      draft,
            "status":     "pending",     # pending / approved / rejected / sent / failed
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "approved_at":"",
            "sent_at":    "",
            "note":       "",
        })
        existing_keys.add(key)
        added += 1
        log.info(f"카페 배포 큐 추가: {target['name']} ← {title[:30]}")

        # 연속 초안 생성 사이 지터 (API rate-limit 방지)
        if added < len(matched):
            time.sleep(random.uniform(1, 3))

    _save_queue(queue)
    return added


def get_pending_queue() -> list:
    """승인 대기 중인 큐 항목 반환"""
    return [q for q in _load_queue() if q.get("status") == "pending"]

def get_queue_by_status(status: str) -> list:
    return [q for q in _load_queue() if q.get("status") == status]

def update_queue_item(item_id: str, updates: dict):
    queue = _load_queue()
    for item in queue:
        if item["id"] == item_id:
            item.update(updates)
            break
    _save_queue(queue)


def _resolve_naver_credentials() -> "tuple[str, str]":
    """활성 프로필의 네이버 자격증명(id, pw)을 복호화해 반환.

    GUI/호출자가 평문 PW 를 클로저로 붙잡아 전달하지 않도록, 게시 직전 이 함수가
    활성 프로필에서 직접 복호화한다. 값은 로그에 남기지 않는다(존재만 판단).
    """
    try:
        from config.crypto import load_secret
    except ImportError:
        log.debug("crypto 모듈 없음 — 자격증명 복호화 불가", exc_info=True)
        return "", ""
    naver_id = load_secret("naver_id", "")
    naver_pw = load_secret("naver_pw", "")
    return naver_id, naver_pw


def approve_and_post(item_id: str, edited_draft: str = None,
                     naver_id: Optional[str] = None,
                     naver_pw: Optional[str] = None) -> dict:
    """
    큐 항목을 승인하고 네이버 카페에 실제로 게시합니다.
    Selenium으로 카페 게시판에 자동 등록합니다.

    naver_id/naver_pw 를 명시하지 않으면(None) **활성 프로필에서 자체 복호화**한다.
    호출자(GUI)는 평문 PW 를 넘기지 말 것 — 자격증명이 스레드 클로저·예외 메시지로
    새지 않게 하기 위함이다.

    Returns: {"success": bool, "url": str, "error": str}
    """
    if naver_id is None or naver_pw is None:
        resolved_id, resolved_pw = _resolve_naver_credentials()
        naver_id = resolved_id if naver_id is None else naver_id
        naver_pw = resolved_pw if naver_pw is None else naver_pw

    queue = _load_queue()
    item  = next((q for q in queue if q["id"] == item_id), None)
    if not item:
        return {"success": False, "error": "항목을 찾을 수 없습니다"}

    draft = edited_draft or item.get("draft", "")
    title = item.get("post_title", "")
    url   = item.get("post_url", "")

    # 상태 업데이트
    update_queue_item(item_id, {
        "status":      "approved",
        "approved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "draft":       draft,
    })

    # Selenium으로 카페 게시
    try:
        result = _post_to_naver_cafe(
            cafe_url  = item["cafe_url"],
            board_id  = item["board_id"],
            title     = f"📌 {title}",
            body      = f"{draft}\n\n🔗 원문 링크: {url}",
            naver_id  = naver_id,
            naver_pw  = naver_pw,
        )
        if result.get("success"):
            update_queue_item(item_id, {
                "status":  "sent",
                "sent_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "cafe_post_url": result.get("post_url", ""),
            })
            _append_log({
                "channel":    "naver_cafe",
                "cafe_name":  item["cafe_name"],
                "post_title": title,
                "post_url":   url,
                "success":    True,
            })
            log.info(f"카페 등록 완료: {item['cafe_name']} ← {title[:30]}")
            return {"success": True, "url": result.get("post_url", "")}
        else:
            update_queue_item(item_id, {
                "status": "failed",
                "note":   result.get("error", ""),
            })
            return {"success": False, "error": result.get("error", "")}
    except (KeyError, ValueError, OSError) as e:
        update_queue_item(item_id, {"status": "failed", "note": str(e)})
        return {"success": False, "error": str(e)}


def reject_queue_item(item_id: str, reason: str = "") -> bool:
    queue = _load_queue()
    found = False
    for item in queue:
        if item["id"] == item_id:
            item["status"] = "rejected"
            item["note"]   = reason
            found = True
            break
    if found:
        _save_queue(queue)
    return found


# ──────────────────────────────────────────────────────
# Selenium 카페 게시
# ──────────────────────────────────────────────────────
def _post_to_naver_cafe(cafe_url: str, board_id: str,
                         title: str, body: str,
                         naver_id: str, naver_pw: str) -> dict:
    """
    Selenium으로 네이버 카페 게시판에 글을 등록합니다.
    네이버 로그인이 필요합니다.
    """
    try:
        from selenium import webdriver
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.chrome.options import Options
        from selenium.common.exceptions import NoSuchElementException as _NSE, TimeoutException as _TE, WebDriverException
        import time as _time

        opts = Options()
        # 헤드리스 모드 OFF — 카페 등록은 captcha 가능성 있어 보이게 실행
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--window-size=1280,900")
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        opts.add_experimental_option("useAutomationExtension", False)

        # 프로필 세션/IP 격리(2026-07-19 격리 감사 후속) — 발행 경로와 동일 정책.
        # user-data-dir 는 발행 드라이버와 겹치면 Chrome 이중 기동 거부 → 카페 전용 폴더.
        try:
            from core.naver_uploader import _apply_profile_isolation
            _root = _nbaprofile.profile_root()
            _cafe_udd = (_root / "config" / "chrome_profile_cafe") if _root else None
            _apply_profile_isolation(opts, user_data_dir=_cafe_udd)
        except Exception:  # noqa: BLE001 — 격리 실패가 카페 게시를 막지 않게
            pass

        driver = webdriver.Chrome(options=opts)
        wait   = WebDriverWait(driver, 15)

        try:
            # ── 로그인 ──────────────────────────────────
            driver.get("https://nid.naver.com/nidlogin.login")
            _time.sleep(1.5)

            # JavaScript로 로그인 정보 입력 (selenium 감지 우회)
            driver.execute_script(
                "document.querySelector('#id').value = arguments[0]", naver_id)
            driver.execute_script(
                "document.querySelector('#pw').value = arguments[0]", naver_pw)
            _time.sleep(0.8)
            from core.naver_uploader import _click_login_button
            if not _click_login_button(driver):
                return {"success": False,
                        "error": "로그인 버튼을 찾지 못함 (네이버 로그인 페이지 개편)"}
            _time.sleep(3)

            # 로그인 성공 확인
            if "nidlogin" in driver.current_url.lower():
                return {"success": False, "error": "로그인 실패 (2단계 인증 확인)"}

            # ── 카페 게시판 이동 ───────────────────────
            write_url = (
                f"{cafe_url.rstrip('/')}/ArticleWrite.nhn"
                f"?boardtype=L&menuid={board_id}"
            )
            driver.get(write_url)
            _time.sleep(2)

            # 글쓰기 iframe 진입
            try:
                iframe = wait.until(EC.presence_of_element_located(
                    (By.CSS_SELECTOR, "iframe#cafe_main")))
                driver.switch_to.frame(iframe)
            except _TE:
                pass   # 직접 접근인 경우 iframe 없을 수 있음

            # 제목 입력
            try:
                title_el = wait.until(EC.presence_of_element_located(
                    (By.CSS_SELECTOR, "input[name='subject'], #subject, .title_input")))
                title_el.clear()
                title_el.send_keys(title[:80])
                _time.sleep(0.5)
            except (_TE, _NSE) as e:
                return {"success": False, "error": f"제목 입력 실패: {e}"}

            # 본문 입력 (SmartEditor 대응)
            try:
                body_el = driver.find_element(
                    By.CSS_SELECTOR,
                    "#content, .se-content, .text_area, textarea[name='content']"
                )
                body_el.clear()
                body_el.send_keys(body[:1500])
                _time.sleep(0.5)
            except _NSE:
                # SmartEditor iframe 진입 시도
                try:
                    se_frame = driver.find_element(
                        By.CSS_SELECTOR, "iframe[id*='se'], iframe[title*='본문']")
                    driver.switch_to.frame(se_frame)
                    body_area = wait.until(EC.presence_of_element_located(
                        (By.CSS_SELECTOR, "body, .se-content")))
                    body_area.click()
                    body_area.send_keys(body[:1500])
                    driver.switch_to.default_content()
                    try:
                        driver.switch_to.frame(
                            driver.find_element(By.CSS_SELECTOR, "iframe#cafe_main"))
                    except _NSE:
                        pass
                except (_TE, _NSE) as e2:
                    return {"success": False, "error": f"본문 입력 실패: {e2}"}

            _time.sleep(0.5)

            # 등록 버튼 클릭
            try:
                submit_btn = driver.find_element(
                    By.CSS_SELECTOR,
                    "button.btn_complete, input[type='submit'], .submit, .btn_submit"
                )
                submit_btn.click()
                _time.sleep(3)
            except _NSE as e:
                return {"success": False, "error": f"등록 버튼 실패: {e}"}

            # 게시 후 URL 확보
            post_url = driver.current_url
            return {"success": True, "post_url": post_url}

        finally:
            try:
                driver.quit()
            except OSError:
                pass

    except ImportError:
        return {"success": False, "error": "selenium 미설치 (pip install selenium)"}
    except (WebDriverException, OSError) as e:
        return {"success": False, "error": str(e)}


# ──────────────────────────────────────────────────────
# 설정 업데이트 헬퍼 (GUI에서 사용)
# ──────────────────────────────────────────────────────
def update_kakao_settings(enabled: bool, access_token: str,
                          channel_token: str = "",
                          send_to_me: bool = True,
                          send_channel: bool = False,
                          template: str = ""):
    cfg = _load_channel_cfg()
    cfg["kakao"].update({
        "enabled":      enabled,
        "access_token": access_token.strip(),
        "channel_token":channel_token.strip(),
        "send_to_me":   send_to_me,
        "send_channel": send_channel,
    })
    if template:
        cfg["kakao"]["template"] = template
    _save_channel_cfg(cfg)

def update_cafe_settings(enabled: bool, targets: list,
                         auto_approve: bool = False,
                         min_delay_sec: int = 3600):
    cfg = _load_channel_cfg()
    cfg["cafe"].update({
        "enabled":       enabled,
        "targets":       targets,
        "auto_approve":  auto_approve,
        "min_delay_sec": min_delay_sec,
    })
    _save_channel_cfg(cfg)

def get_channel_stats() -> dict:
    logs     = _load_log()
    queue    = _load_queue()
    pending  = sum(1 for q in queue if q["status"] == "pending")
    sent     = sum(1 for q in queue if q["status"] == "sent")
    kakao_ok = sum(1 for l in logs if l.get("channel") == "kakao")
    cafe_ok  = sum(1 for l in logs if l.get("channel") == "naver_cafe" and l.get("success"))
    return {
        "kakao_total":   kakao_ok,
        "cafe_total":    cafe_ok,
        "queue_pending": pending,
        "queue_sent":    sent,
    }
