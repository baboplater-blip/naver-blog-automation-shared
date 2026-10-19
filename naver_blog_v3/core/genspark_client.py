# core/genspark_client.py — 젠스파크 「AI 이미지」(GPT Image 2) 범용 생성 클라이언트
"""젠스파크 웹 「AI 이미지」(GPT Image 2)를 CDP attach 로 조종해 한글 baked 이미지를 만든다.

출처(이식):
    원본 = novel-writing/_lib/engine/genspark_cover.py (772줄)
    이식일 = 2026-08-29
    이식 범위 = **범용 코어만**. 책표지 전용 로직(권별 명세 파싱 parse_specs·
    단권 재생성 regenerate_and_collect/regen_failures·'조민우 지음/올스토리 띠지'
    배치 지시 상수)은 **제외**한다. 블로그는 저자/출판사 띠지가 없고, 배치 프롬프트는
    호출자가 통째로 넘긴다(send_prompt). novel-writing 에 대한 import·경로 의존 0.

핵심 설계(원본 계승):
    젠스파크 「AI 이미지」(/ai_image)는 프롬프트 입력창(textarea)에 긴 지시를 그대로 받아
    GPT Image 2 로 한글 텍스트가 정확히 새겨진 이미지를 생성한다. 조종용 Chrome 을
    원격 디버깅 포트로 띄우고(계정 프로필로 로그인 1회) Playwright 가 CDP 로 붙어
    모델·비율을 고르고 프롬프트를 보낸 뒤, 생성된 <img> 를 세션 쿠키로 다운로드한다.

    로그인(구글 OAuth·2FA)은 **사람이 그 창에서 1회** — 자격증명 자동입력 코드는 없다.

프로필/포트 격리(블로그화):
    · 계정 프로필 = ~/.genspark_pw_profile__<slug> (원본과 **동일** — novel-writing 과
      같은 계정이 같은 Chrome 프로필을 공유해 로그인 세션 재사용).
    · 계정 레지스트리 = 활성 프로필의 logs/genspark_accounts.json (프로필 격리).
    · 기본 디버깅 포트 = 9223 (★네이버 발행 attach 포트 settings.CHROME_ATTACH_PORT=9222 와
      분리 — 두 자동화가 같은 포트를 두고 충돌하지 않게). 계정별 포트는 레지스트리가
      관리한다(자동 배정: exampleaccount=9223, sampleaccount=9224 …).

opt-in / fail-open:
    파이프라인 자동 배선(Phase 2)은 settings.GENSPARK_ENABLED(기본 False)로 게이트한다.
    이 모듈의 저수준 함수는 순수 도구라 예외를 삼키고(fail-open) 흐름을 막지 않는다.
    수동 스모크(`python -m core.genspark_client ...`)는 사용자의 명시적 실행이라 게이트와
    무관하게 동작한다(라이브 검증용).

수동 확인:
    python -m core.genspark_client --list-accounts
    python -m core.genspark_client --add-account user@example.com
    python -m core.genspark_client --check-login
    python -m core.genspark_client --gen "<프롬프트>" --ratio 1:1 --expected 1 --out ./_out
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys
import time
import urllib.request
from typing import Callable, Dict, List, Optional

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:  # 프로필 격리 로거(있으면 사용) — 없으면 print 폴백.
    from config.logger import get_logger
    _logger = get_logger(__name__)
except Exception:  # noqa: BLE001 — 로거 미로드 환경에서도 동작
    _logger = None

try:
    from config import profile as _nbaprofile
except Exception:  # noqa: BLE001 — profile 미로드 환경(순수 단위 테스트) 방어
    _nbaprofile = None


IMAGE_TOOL_URL = "https://www.genspark.ai/ai_image"
GENSPARK_HOME = "https://www.genspark.ai"
MODEL_GPT_IMAGE_2 = "GPT Image 2"

# ★네이버 발행 attach 포트(settings.CHROME_ATTACH_PORT=9222)와 분리한 젠스파크 기본 포트.
DEFAULT_PORT = 9223

# 조종용 Chrome 실행 파일 후보(윈도우).
_CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]


def log(msg: str) -> None:
    """진행 로그 — 프로필 격리 로거가 있으면 그쪽, 없으면 print."""
    if _logger is not None:
        _logger.info("[genspark] %s", msg)
    else:
        print(f"[genspark] {msg}", flush=True)


def _settings_path() -> pathlib.Path:
    """계정 레지스트리 파일 경로(활성 프로필의 logs/genspark_accounts.json).

    profile 라우터가 있으면 프로필 격리 경로, 없으면 앱 루트 logs/ 폴백.
    (테스트는 이 모듈의 _SETTINGS_PATH 를 monkeypatch 해 tmp 로 우회한다.)
    """
    if _nbaprofile is not None:
        try:
            return _nbaprofile.logs_dir() / "genspark_accounts.json"
        except Exception:  # noqa: BLE001 — 라우터 실패 시 폴백
            pass
    return pathlib.Path(__file__).resolve().parent.parent / "logs" / "genspark_accounts.json"


# 모듈 전역(테스트 monkeypatch 대상). 함수는 항상 이 전역을 참조한다.
_SETTINGS_PATH = _settings_path()


# ── 계정 레지스트리 (비밀번호 없음 — 이메일·프로필·포트·chrome 만) ──────────────
def _slug(email: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "", (email or "acct").split("@")[0]) or "acct"


def _default_profile(email: str) -> str:
    # ★원본과 동일한 홈 경로 — novel-writing 과 같은 계정이 같은 로그인 세션(프로필)을 공유.
    # ⚠️ 슬러그(이메일 로컬부) 기준 **전역 공유**라, 서로 다른 NBA 프로필(examplebrand·exampleprofile…)이
    #    같은 이메일을 등록하면 물리적으로 동일한 user-data-dir 를 가리킨다(의도된 세션 공유).
    return str(pathlib.Path(__file__).resolve().parents[1] / ".local" / "genspark_profiles" / _slug(email))


def _read_raw() -> Dict:
    """레지스트리 원본 로드 — 이 레포 표준 safe_load_json(.bak 자동 복원). 항상 dict.

    쓰기 도중 크래시로 파일이 잘려도 .bak 에서 자동 복원한다. config 헬퍼 미로드/권한
    오류 등 예기치 못한 상황에서는 직접 읽기로 폴백(기존 fail-open 유지).
    """
    data = None
    try:
        from config import safe_load_json
        data = safe_load_json(_SETTINGS_PATH, default=dict)
    except Exception:  # noqa: BLE001 — config 미로드/권한 오류 → 직접 읽기 폴백
        try:
            if _SETTINGS_PATH.exists():
                data = json.loads(_SETTINGS_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
    return data if isinstance(data, dict) else {}


def _normalize(data: Dict) -> Dict:
    """구 평면 형식({id,profile,port,chrome})이면 다계정 형식으로 마이그레이션."""
    if not isinstance(data, dict):
        return {"active": "", "accounts": {}}
    if "accounts" in data:
        if not isinstance(data.get("accounts"), dict):
            data["accounts"] = {}   # 손상 케이스(accounts 가 dict 아님) 방어
        data.setdefault("active", "")
        return data
    accts: Dict = {}
    if data.get("id"):
        accts[data["id"]] = {
            "profile": data.get("profile") or _default_profile(data["id"]),
            "port": int(data.get("port") or DEFAULT_PORT),
            "chrome": data.get("chrome") or "",
        }
    return {"active": data.get("id") or "", "accounts": accts}


def _write_raw(data: Dict) -> None:
    """레지스트리 원자적 저장 — 이 레포 표준 atomic_write_json(임시파일+rename+.bak).

    쓰기 도중 크래시가 나도 원본이 잘리지 않는다. config 헬퍼 미로드 시 직접 쓰기 폴백.
    """
    _SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        from config import atomic_write_json
        atomic_write_json(_SETTINGS_PATH, data)
    except Exception:  # noqa: BLE001 — config 미로드 환경 → 직접 쓰기 폴백
        _SETTINGS_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def list_accounts() -> Dict:
    """{"active": email, "accounts": {email: {profile,port,chrome}}} 반환."""
    return _normalize(_read_raw())


def save_account(email: str, profile: Optional[str] = None, port: Optional[int] = None,
                 chrome: Optional[str] = None, make_active: bool = True) -> Dict:
    """계정 추가/갱신(비밀번호 없음). 미지정 필드는 기본값(프로필=이메일 슬러그, 포트 자동)."""
    data = _normalize(_read_raw())
    accts = data["accounts"]
    a = accts.get(email, {})
    a["profile"] = profile or a.get("profile") or _default_profile(email)
    if port is not None:
        a["port"] = int(port)
    elif "port" not in a:
        used = {v.get("port") for v in accts.values()}
        p = DEFAULT_PORT
        while p in used:
            p += 1
        a["port"] = p
    a["chrome"] = chrome if chrome is not None else a.get("chrome", "")
    accts[email] = a
    if make_active or not data.get("active"):
        data["active"] = email
    _write_raw(data)
    log(f"계정 저장: {email} (profile={a['profile']}, port={a['port']})"
        f"{' · 활성' if data['active'] == email else ''}")
    return data


def set_active(email: str) -> Dict:
    data = _normalize(_read_raw())
    if email not in data["accounts"]:
        raise ValueError(f"저장되지 않은 계정: {email} — 먼저 --add-account 로 등록")
    data["active"] = email
    _write_raw(data)
    log(f"활성 계정 → {email}")
    return data


def account_order(prefer: Optional[str] = None, include_others: bool = False) -> List[str]:
    """사용할 계정 순서. prefer(또는 활성)를 앞에, include_others면 나머지 계정도 뒤에."""
    data = _normalize(_read_raw())
    first = prefer or data.get("active") or ""
    order = [first] if first else []
    if include_others:
        for em in data.get("accounts", {}):
            if em not in order:
                order.append(em)
    return [e for e in order if e]


def load_settings(email: Optional[str] = None) -> Dict:
    """활성(또는 지정) 계정의 설정 {id,profile,port,chrome}. 계정 없으면 기본값."""
    data = _normalize(_read_raw())
    email = email or data.get("active") or ""
    a = data["accounts"].get(email, {})
    return {
        "id": email,
        "profile": a.get("profile") or _default_profile(email),
        "port": int(a.get("port") or DEFAULT_PORT),
        "chrome": a.get("chrome") or "",
    }


def save_settings(**kw) -> Dict:
    """(호환) 활성 계정을 갱신. id 가 오면 그 계정으로."""
    email = kw.get("id") or (list_accounts().get("active") or "")
    save_account(email, profile=kw.get("profile"), port=kw.get("port"),
                 chrome=kw.get("chrome"), make_active=True)
    return load_settings()


# ── 브라우저 자동 기동 + 로그인 감지 ───────────────────────────────────────
def _debug_port_up(port: int) -> bool:
    try:
        with urllib.request.urlopen(
                f"http://localhost:{port}/json/version", timeout=2) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001 — 포트 미개방/네트워크 오류 = 안 떠 있음
        return False


def _chrome_exe() -> Optional[str]:
    saved = load_settings().get("chrome")
    if saved and pathlib.Path(saved).exists():
        return saved
    for p in _CHROME_PATHS:
        if pathlib.Path(p).exists():
            return p
    return None


def ensure_browser(port: Optional[int] = None, profile: Optional[str] = None,
                   wait_s: int = 25) -> bool:
    """조종용 Chrome 이 없으면 원격 디버깅 포트 + 저장된 로그인 프로필로 자동 기동.

    이미 떠 있으면 아무 것도 안 함. 저장된 프로필(genspark_accounts.json)을 쓰므로 한 번
    로그인해 두면 계속 그 세션으로 뜬다. 로그인 입력은 최초 1회만 사람이 한다.
    """
    st = load_settings()
    port = st["port"] if port is None else port
    profile = st["profile"] if profile is None else profile
    if _debug_port_up(port):
        return True
    exe = _chrome_exe()
    if not exe:
        log("Chrome 실행 파일 못 찾음 — 수동으로 디버깅 포트 Chrome 을 띄우세요")
        return False
    pathlib.Path(profile).mkdir(parents=True, exist_ok=True)
    log(f"조종용 Chrome 자동 기동 (port {port}, profile {profile})")
    subprocess.Popen(
        [exe, f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
         "--no-first-run", "--no-default-browser-check", GENSPARK_HOME],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(wait_s):
        if _debug_port_up(port):
            time.sleep(2)
            return True
        time.sleep(1)
    log("Chrome 기동 대기 시간 초과")
    return False


# 5시간 한도/요청 제한 배너 감지(휴리스틱).
_LIMIT_RE = re.compile(
    r"(5\s*시간|한도.*(초과|도달|소진)|사용량.*(한도|초과)|일일 한도|무료 한도|크레딧.*(소진|부족)|"
    r"잠시 후 다시|요청이 (너무 )?많|rate.?limit|quota|usage limit|limit reached|"
    r"try again (later|in)|too many requests)",
    re.I,
)


def is_rate_limited(pg) -> bool:
    """현재 페이지에 5시간 한도/요청 제한 표시가 있는지 best-effort 감지."""
    try:
        body = pg.inner_text("body")
    except Exception:  # noqa: BLE001 — 페이지 접근 실패는 '알 수 없음'=False
        return False
    return bool(_LIMIT_RE.search(body or ""))


def check_login(pg) -> bool:
    """/ai_image 에서 로그인 상태 감지(입력창 + '+' 첨부 존재 = 로그인됨)."""
    try:
        if "/ai_image" not in (pg.url or ""):
            pg.goto(IMAGE_TOOL_URL, wait_until="domcontentloaded")
            pg.wait_for_timeout(6000)
        ok = pg.locator("textarea").count() > 0 and pg.locator(".add-entry-btn").count() > 0
        if not ok:
            log("★로그인 필요 — 열린 Chrome 창에서 젠스파크에 1회 로그인하세요(구글 OAuth·2FA).")
        return ok
    except Exception as e:  # noqa: BLE001 — 감지 실패는 로그인 안 됨으로 취급
        log(f"로그인 감지 실패: {repr(e)[:100]}")
        return False


# ── 연결 ──────────────────────────────────────────────────────────────────
def connect(playwright, port: Optional[int] = None, autolaunch: bool = True,
            profile: Optional[str] = None):
    """원격 디버깅 Chrome 에 CDP 로 붙어 (browser, context) 반환. autolaunch면 없을 때 자동 기동.

    port/profile 미지정 시 저장된 설정(활성 계정)을 따른다.
    """
    st = load_settings()
    port = st["port"] if port is None else port
    if autolaunch and not _debug_port_up(port):
        ensure_browser(port, profile)
    b = playwright.chromium.connect_over_cdp(f"http://localhost:{port}")
    if not b.contexts:
        raise RuntimeError("연결된 브라우저 컨텍스트 없음 — 조종용 Chrome 이 떠 있는지 확인")
    return b, b.contexts[0]


def open_image_tool(ctx, reuse: bool = False):
    """「AI 이미지」 전용 화면(/ai_image)을 새 탭으로 연다. reuse=True면 기존 이미지 탭 재사용.

    ★반드시 /ai_image — 여기에 긴 프롬프트를 받는 textarea 와 '+'(로컬 파일) 첨부가 있다.
    """
    if reuse:
        for pg in ctx.pages:
            if "/ai_image" in (pg.url or ""):
                return pg
    pg = ctx.new_page()
    pg.goto(IMAGE_TOOL_URL, wait_until="domcontentloaded")
    pg.wait_for_timeout(7000)
    return pg


# ── 스레드 연속성 (같은 스레드에 이어 보내면 앞 이미지를 레퍼런스로 스타일 유지) ──
_THREAD_RE = re.compile(r"/agents\?id=")


def current_thread_url(pg) -> Optional[str]:
    """현재 페이지가 생성 스레드(/agents?id=...)면 그 URL 반환."""
    u = pg.url or ""
    return u if _THREAD_RE.search(u) else None


def wait_for_thread_url(pg, timeout_s: int = 40) -> Optional[str]:
    """전송 후 /ai_image → /agents?id=<thread> 로 이동하기를 기다려 스레드 URL 반환."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        u = current_thread_url(pg)
        if u:
            return u
        pg.wait_for_timeout(1000)
    return None


def open_thread(ctx, url: str):
    """지정한 생성 스레드로 이동(이어가기). 이미 그 스레드 탭이 열려 있으면 재사용."""
    for pg in ctx.pages:
        if url.split("id=")[-1] in (pg.url or ""):
            try:
                pg.bring_to_front()
            except Exception:  # noqa: BLE001 — 포커스 실패는 무시
                pass
            return pg
    pg = ctx.new_page()
    pg.goto(url, wait_until="domcontentloaded")
    pg.wait_for_timeout(6000)
    return pg


# ── 컨트롤 ────────────────────────────────────────────────────────────────
def _model_pill(pg):
    # 모델 pill 은 현재 모델명을 텍스트로 노출(예: "GPT Image 2" 또는 "Nano Banana Pro").
    for name in ("GPT Image 2", "Nano Banana", "Seedream", "Flux", "Imagen", "Qwen", "Ideogram"):
        loc = pg.get_by_text(name, exact=False)
        if loc.count():
            return loc.first
    return None


def set_model(pg, model: str = MODEL_GPT_IMAGE_2) -> bool:
    """모델을 GPT Image 2 로 설정(한글 텍스트 렌더링). 이미 설정돼 있으면 통과."""
    pill = _model_pill(pg)
    if pill is not None:
        try:
            if model.lower() in (pill.inner_text() or "").lower():
                log(f"모델 이미 {model}")
                return True
        except Exception:  # noqa: BLE001 — 텍스트 읽기 실패 시 클릭 경로로
            pass
    if pill is None:
        log("모델 pill 못 찾음")
        return False
    pill.click()
    pg.wait_for_timeout(1200)
    opt = pg.get_by_text(model, exact=True)
    if not opt.count():
        log(f"모델 옵션 '{model}' 없음")
        return False
    opt.first.click()
    pg.wait_for_timeout(1000)
    log(f"모델 → {model}")
    return True


def set_ratio(pg, ratio: str = "1:1") -> bool:
    """설정 패널에서 종횡비 선택 후 패널 닫기. 블로그 기본=1:1(썸네일), 세로 카드는 3:4/2:3."""
    gear = pg.get_by_text("설정", exact=True)
    if not gear.count():
        log("설정 버튼 없음")
        return False
    gear.first.click()
    pg.wait_for_timeout(1200)
    opt = pg.get_by_text(ratio, exact=True)
    ok = False
    if opt.count():
        opt.first.click()
        pg.wait_for_timeout(600)
        ok = True
        log(f"종횡비 → {ratio}")
    else:
        log(f"종횡비 '{ratio}' 없음")
    try:  # 패널 닫기(토글)
        pg.get_by_text("설정", exact=True).first.click()
        pg.wait_for_timeout(400)
    except Exception:  # noqa: BLE001 — 닫기 실패는 흐름에 무해
        pass
    return ok


def set_quality(pg, quality: str = "자동") -> bool:
    """이미지 품질을 '자동'으로 설정한다 — 자동=크레딧 무소모(사용자 확정, 2026-08-29).

    품질을 명시하지 않으면 젠스파크가 high 로 잡아 "quality=high consumes credits" 확인창을
    띄운다. 여기서 품질을 '자동'으로 고정하면 크레딧을 쓰지 않는다.

    ★DOM 주의: 설정 패널에는 '자동'이 **이미지 크기**와 **품질** 양쪽에 존재해 모호하다.
    '품질' 라벨을 앵커로 그 인접 옵션 컨테이너 안의 '자동'을 먼저 시도하고(스코프),
    못 찾으면 마지막 '자동' 후보를 폴백 선택한다(품질이 크기보다 아래 배치되는 관찰 가정).
    DOM 구조가 불확실하므로 **best-effort·fail-open** — 못 찾으면 로그만 남기고 예외를
    전파하지 않는다. ⚠️ 실제 셀렉터·순서는 라이브 검증 필요.
    """
    gear = pg.get_by_text("설정", exact=True)
    if not gear.count():
        log("설정 버튼 없음 — 품질 설정 스킵")
        return False
    ok = False
    try:
        gear.first.click()
        pg.wait_for_timeout(1200)
        # 시도1: '품질' 라벨 근처 옵션으로 스코프.
        try:
            label = pg.get_by_text("품질", exact=False)
            if label.count():
                container = label.first.locator(
                    "xpath=ancestor::*[.//*[normalize-space(text())='자동']][1]")
                opt = container.get_by_text(quality, exact=True)
                if opt.count():
                    opt.first.click()
                    pg.wait_for_timeout(500)
                    ok = True
                    log(f"품질 → {quality} (품질 라벨 스코프)")
        except Exception:  # noqa: BLE001 — 스코프 탐색 실패는 폴백으로
            pass
        # 시도2: 폴백 — 전체 '자동' 후보 중 마지막(품질이 크기보다 아래 가정).
        if not ok:
            opt = pg.get_by_text(quality, exact=True)
            if opt.count():
                opt.last.click()
                pg.wait_for_timeout(500)
                ok = True
                log(f"품질 → {quality} (폴백: 마지막 '{quality}' 후보)")
            else:
                log(f"품질 옵션 '{quality}' 못 찾음 — 크레딧 확인창 방어망에 의존")
    except Exception as e:  # noqa: BLE001 — 품질 설정 실패는 흐름을 막지 않는다
        log(f"품질 설정 실패(무시): {repr(e)[:100]}")
    try:  # 패널 닫기(토글)
        pg.get_by_text("설정", exact=True).first.click()
        pg.wait_for_timeout(400)
    except Exception:  # noqa: BLE001 — 닫기 실패는 흐름에 무해
        pass
    return ok


# 크레딧 소모 확인창 감지(영/한 휴리스틱).
_CREDIT_RE = re.compile(
    r"(consume[sd]?\s+credits|uses?\s+credits|will\s+use\s+credits|credit.?s?\s+will|"
    r"크레딧.*(소모|사용|차감|필요)|크레딧이\s*(소모|사용|차감))",
    re.I,
)
# 확인창에서 크레딧을 **안** 쓰는 쪽 버튼 후보(취소·품질 변경). ★절대 Continue/계속 아님.
_CREDIT_CANCEL_TEXTS = (
    "품질 변경", "품질을 변경", "자동으로 변경", "Change quality", "Change to Auto",
    "취소", "Cancel", "닫기",
)


def _dismiss_credit_confirm(pg) -> bool:
    """'크레딧 소모' 확인창이 뜨면 취소/품질변경을 눌러 크레딧을 절대 쓰지 않는다.

    ★Continue/계속 류는 절대 클릭하지 않는다(그게 크레딧을 소모). 창이 없으면 무동작.
    best-effort·fail-open — 예외를 전파하지 않는다. 취소 버튼을 못 찾으면 크레딧 방지 실패
    가능성을 로그로 남긴다(그 경우 생성이 진행되지 않아 이미지 없음=fail-open).
    """
    try:
        body = pg.inner_text("body")
    except Exception:  # noqa: BLE001 — 페이지 접근 실패는 '창 없음'
        return False
    if not _CREDIT_RE.search(body or ""):
        return False
    for txt in _CREDIT_CANCEL_TEXTS:
        try:
            btn = pg.get_by_text(txt, exact=False)
            if btn.count():
                btn.first.click()
                pg.wait_for_timeout(600)
                log(f"크레딧 확인창 감지 → '{txt}' 클릭(크레딧 미사용)")
                return True
        except Exception:  # noqa: BLE001 — 이 후보 클릭 실패는 다음 후보로
            continue
    log("⚠ 크레딧 확인창 감지했으나 취소 버튼 못 찾음 — 품질을 '자동'으로 설정했는지 확인")
    return False


# ── md 첨부 (블로그는 보통 send_prompt 인라인만 쓰지만 도구로 남긴다) ──────────
# '+'(.add-entry-btn) → "로컬 파일 찾기" → set_files 경로. 상단 📎(이미지 전용)는 md 드롭.
_LOCAL_FILE_MENU = "로컬 파일 찾기"


def attach_md(pg, md_path) -> bool:
    """'+' → '로컬 파일 찾기'로 파일을 첨부한다. 칩 생성 확인."""
    md_path = str(md_path)
    plus = pg.locator(".add-entry-btn").first
    if not plus.count():
        log("'+'(.add-entry-btn) 없음 — /ai_image 페이지인지 확인")
        return False
    try:  # 방식 A: '+' 클릭이 바로 다이얼로그를 여는 경우
        with pg.expect_file_chooser(timeout=4000) as fc:
            plus.click()
        fc.value.set_files(md_path)
    except Exception:  # noqa: BLE001 — 방식 B: '+' → 메뉴 → "로컬 파일 찾기"
        pg.wait_for_timeout(800)
        item = pg.get_by_text(_LOCAL_FILE_MENU, exact=False)
        if not item.count():
            item = pg.get_by_text("로컬", exact=False)
        if not item.count():
            log("메뉴에 '로컬 파일 찾기' 없음")
            return False
        with pg.expect_file_chooser(timeout=6000) as fc2:
            item.first.click()
        fc2.value.set_files(md_path)
    pg.wait_for_timeout(4000)
    body = pg.inner_text("body")
    name = pathlib.Path(md_path).name
    ok = name in body or name.rsplit(".", 1)[0] in body
    log(f"파일 첨부 {'OK' if ok else '실패(칩 미확인)'}: {name}")
    return ok


# 프롬프트가 아니라 사람에게 하는 메모인 꼬리 섹션(팁·후속·워크플로 등) 패턴.
_META_HEADING = re.compile(
    r"^\s*#{1,6}\s*(팁|참고|메모|노트|후속|주의|워크플로|사용법|TODO|Tips?|Notes?)\b",
    re.I | re.M,
)


def strip_notes(md_text: str) -> str:
    """프롬프트가 아닌 꼬리 메타 섹션(## 팁 등)을 잘라낸다.

    첫 메타 헤딩 위치에서 자르고, 바로 앞의 수평선(---)도 함께 제거한다.
    """
    m = _META_HEADING.search(md_text)
    if not m:
        return md_text.rstrip()
    cut = md_text[: m.start()]
    cut = re.sub(r"\n\s*-{3,}\s*\n?\s*$", "\n", cut)
    return cut.rstrip()


def send_prompt(pg, prompt: str) -> bool:
    """프롬프트를 입력창에 넣고 전송(/ai_image 전송 버튼 = .enter-icon)."""
    box = pg.locator("textarea").first
    box.click()
    box.fill(prompt)
    pg.wait_for_timeout(1000)
    got = box.input_value()
    if len(got) < len(prompt) * 0.9:
        log(f"⚠ 입력창 텍스트 유실: {len(got)}/{len(prompt)}자")
    # 전송 버튼 클릭 우선(멀티라인이라 Enter 가 줄바꿈일 수 있음)
    btn = pg.locator(".enter-icon").first
    if btn.count():
        btn.click()
        pg.wait_for_timeout(1500)
        if not box.input_value().strip():
            log("전송(.enter-icon) OK")
            return True
    box.click()
    pg.keyboard.press("Enter")
    pg.wait_for_timeout(1500)
    if not box.input_value().strip():
        log("전송(Enter) OK")
        return True
    log("⚠ 전송 여부 불확실(입력창에 텍스트 잔존)")
    return False


# ── 생성물 수집·다운로드 ──────────────────────────────────────────────────
_UI_HINT = re.compile(r"(avatar|icon|logo|emoji|sprite|/static/|placeholder)", re.I)


def collect_image_urls(pg, min_side: int = 400) -> List[str]:
    """스레드에 렌더된 생성 이미지 <img>의 원본 URL 을 수집(큰 이미지만, UI 아이콘 제외)."""
    js = """(minSide) => {
        const out = [];
        for (const img of document.querySelectorAll('img')) {
            const w = img.naturalWidth || 0, h = img.naturalHeight || 0;
            if (w < minSide && h < minSide) continue;
            let u = img.currentSrc || img.src || '';
            if (!u || !/^https?:/.test(u)) continue;
            out.push(u);
        }
        return [...new Set(out)];
    }"""
    urls = pg.evaluate(js, min_side)
    return [u for u in urls if not _UI_HINT.search(u)]


def wait_and_collect(pg, expected: int, timeout_s: int = 900, poll_s: int = 15,
                     on_progress: Optional[Callable[[int], None]] = None) -> List[str]:
    """생성이 쌓이는 동안 폴링하며 이미지 URL 을 모은다. expected 도달 또는 timeout 까지."""
    deadline = time.time() + timeout_s
    seen: List[str] = []
    stable = 0
    last = -1
    while time.time() < deadline:
        try:  # 결과가 아래로 쌓이므로 스크롤로 lazy 이미지 강제 로드
            pg.mouse.wheel(0, 4000)
        except Exception:  # noqa: BLE001 — 스크롤 실패는 무해
            pass
        pg.wait_for_timeout(1500)
        for u in collect_image_urls(pg):
            if u not in seen:
                seen.append(u)
        n = len(seen)
        if on_progress:
            on_progress(n)
        log(f"수집 {n}/{expected} (경과 {int(timeout_s - (deadline - time.time()))}s)")
        if n >= expected:
            pg.wait_for_timeout(poll_s * 1000)  # 다 나온 뒤 한 번 더 안정화
            for u in collect_image_urls(pg):
                if u not in seen:
                    seen.append(u)
            break
        stable = stable + 1 if n == last else 0
        last = n
        pg.wait_for_timeout(poll_s * 1000)
    return seen


def wait_for_new_image(pg, known, timeout_s: int = 180, poll_s: int = 6) -> Optional[str]:
    """전송 후 known 집합에 없는 새 생성 이미지 URL 하나를 기다려 반환."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            pg.mouse.wheel(0, 4000)
        except Exception:  # noqa: BLE001 — 스크롤 실패는 무해
            pass
        pg.wait_for_timeout(1200)
        for u in collect_image_urls(pg):
            if u not in known:
                return u
        pg.wait_for_timeout(poll_s * 1000)
    return None


def download(ctx, urls: List[str], out_dir, prefix: str = "gen") -> List[pathlib.Path]:
    """세션 컨텍스트(로그인 쿠키)로 이미지 URL 을 다운로드. 저장 경로 목록 반환."""
    out = pathlib.Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    saved: List[pathlib.Path] = []
    for i, u in enumerate(urls, 1):
        try:
            resp = ctx.request.get(u, timeout=60000)
            if not resp.ok:
                log(f"  [{i}] HTTP {resp.status} — 건너뜀")
                continue
            body = resp.body()
            ext = ".png"
            ct = (resp.headers.get("content-type") or "").lower()
            if "jpeg" in ct or "jpg" in ct:
                ext = ".jpg"
            elif "webp" in ct:
                ext = ".webp"
            p = out / f"{prefix}_{i:02d}{ext}"
            p.write_bytes(body)
            saved.append(p)
            log(f"  [{i}] 저장 {p.name} ({len(body) // 1024}KB)")
        except Exception as e:  # noqa: BLE001 — 한 장 실패가 배치를 막지 않게
            log(f"  [{i}] 다운로드 실패: {repr(e)[:100]}")
    return saved


def montage(paths: List[pathlib.Path], out_path, cols: int = 5,
            thumb: int = 360) -> Optional[pathlib.Path]:
    """다운로드본을 번호 라벨 붙인 몽타주로 합쳐 육안 검증용으로 낸다(PIL 없으면 생략)."""
    try:
        from PIL import Image, ImageDraw
    except Exception:  # noqa: BLE001 — PIL 미설치면 몽타주만 생략
        log("PIL 없음 — 몽타주 생략")
        return None
    if not paths:
        return None
    imgs = []
    for p in paths:
        try:
            im = Image.open(p).convert("RGB")
            im.thumbnail((thumb, thumb))
            imgs.append((p, im))
        except Exception:  # noqa: BLE001 — 깨진 이미지는 건너뜀
            pass
    if not imgs:
        return None
    rows = (len(imgs) + cols - 1) // cols
    cw, ch = thumb, thumb + 28
    sheet = Image.new("RGB", (cols * cw, rows * ch), (250, 250, 250))
    d = ImageDraw.Draw(sheet)
    for idx, (p, im) in enumerate(imgs):
        r, c = divmod(idx, cols)
        x, y = c * cw, r * ch
        sheet.paste(im, (x + (cw - im.width) // 2, y + 28 + (thumb - im.height) // 2))
        d.text((x + 6, y + 6), f"{idx + 1:02d}  {p.name}", fill=(0, 0, 0))
    op = pathlib.Path(out_path)
    sheet.save(op)
    log(f"몽타주 저장 {op}")
    return op


# ── 스모크 진입점 (라이브 검증용 — 명시적 플래그로만 브라우저 기동) ─────────────
def _cli(argv: Optional[List[str]] = None) -> int:
    import argparse

    try:  # 윈도우 콘솔 한글/이모지 안전
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001 — 리다이렉트 등 reconfigure 불가 환경
        pass

    ap = argparse.ArgumentParser(
        prog="python -m core.genspark_client",
        description="젠스파크 「AI 이미지」(GPT Image 2) 스모크 — 계정 관리 + 1회 생성 테스트.")
    ap.add_argument("--list-accounts", action="store_true", help="등록 계정·활성 계정 출력")
    ap.add_argument("--add-account", metavar="EMAIL", help="계정 등록(비밀번호 없음)")
    ap.add_argument("--use", metavar="EMAIL", help="활성 계정 전환")
    ap.add_argument("--port", type=int, default=None, help="디버깅 포트 오버라이드")
    ap.add_argument("--account", metavar="EMAIL", default=None,
                    help="이 실행에 쓸 계정(미지정 시 활성 계정)")
    ap.add_argument("--check-login", action="store_true",
                    help="연결+로그인/한도 상태 리포트(종료코드 0=OK)")
    ap.add_argument("--gen", metavar="PROMPT", default=None,
                    help="1회 생성 테스트(라이브 — 브라우저 기동)")
    ap.add_argument("--ratio", default="1:1", help="종횡비(예: 1:1, 3:4, 2:3)")
    ap.add_argument("--expected", type=int, default=1, help="기대 생성 장수")
    ap.add_argument("--out", default="./_genspark_out", help="다운로드 폴더")
    args = ap.parse_args(argv)

    did_something = False

    if args.add_account:
        save_account(args.add_account)
        did_something = True
    if args.use:
        set_active(args.use)
        did_something = True
    if args.list_accounts:
        data = list_accounts()
        active = data.get("active") or "(없음)"
        print(f"활성 계정: {active}")
        for em, a in (data.get("accounts") or {}).items():
            mark = " *" if em == active else "  "
            print(f"{mark} {em}  port={a.get('port')}  profile={a.get('profile')}")
        did_something = True

    if not (args.check_login or args.gen):
        if did_something:
            return 0
        ap.print_help()
        return 0

    # 여기부터는 라이브 — playwright 필요.
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:  # noqa: BLE001
        print(f"playwright 미설치: {repr(e)[:120]}")
        return 2

    st = load_settings(args.account)
    port = args.port if args.port is not None else st["port"]
    profile = st["profile"]
    with sync_playwright() as pw:
        try:
            _browser, ctx = connect(pw, port=port, profile=profile, autolaunch=True)
        except Exception as e:  # noqa: BLE001
            print(f"연결 실패: {repr(e)[:160]}")
            return 2
        pg = open_image_tool(ctx)
        logged = check_login(pg)
        limited = is_rate_limited(pg)
        print(f"계정: {st['id'] or '(활성 미설정)'}  포트: {port}")
        print(f"로그인: {'OK' if logged else '필요(창에서 1회 로그인)'}")
        print(f"사용 한도: {'⚠ 제한 감지' if limited else '정상'}")

        if not args.gen:
            return 0 if (logged and not limited) else 1

        if not logged:
            print("로그인이 안 돼 생성 생략 — 열린 창에서 로그인 후 다시 실행하세요.")
            return 1
        set_model(pg, MODEL_GPT_IMAGE_2)
        set_ratio(pg, args.ratio)
        send_prompt(pg, args.gen)
        wait_for_thread_url(pg)
        urls = wait_and_collect(pg, args.expected)
        if not urls:
            print("생성 이미지 수집 실패(시간 초과 또는 미생성).")
            return 1
        saved = download(ctx, urls, args.out, prefix="smoke")
        montage(saved, pathlib.Path(args.out) / "montage.png")
        print(f"저장 {len(saved)}장 → {args.out}")
        return 0 if saved else 1


if __name__ == "__main__":
    raise SystemExit(_cli())
