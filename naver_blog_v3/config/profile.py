# config/profile.py — 멀티 블로그 프로필 경로 라우터 (Phase 0, 2026-07-05)
"""여러 블로그를 한 코드베이스로 운영하기 위한 프로필 레이어.

설계 원칙 — **한 프로세스 = 한 프로필**:
    프로필은 프로세스 시작 시 확정되고(환경변수 NBA_PROFILE 또는 profiles/.active),
    런타임 중에는 바뀌지 않는다. 모든 쓰기 경로가 그 프로필 밑으로 고정되므로
    실행 중 엉뚱한 계정으로 새는 경로가 생길 수 없다(오발행 방지의 근본).
    GUI 에서 블로그 전환 = 그 프로필로 앱 재기동(또는 프로필별 프로세스 기동).

레이아웃(named 프로필):
    profiles/<name>/
      ├─ config/  user_settings.json(계정 암호화) · session_cookies.json · chrome_profile/
      ├─ logs/    (post_history·performance_db·blog_stats·reservations·ab_experiments…)
      ├─ runs/  output/  reports/  previews/  screenshots/
      ├─ keywords/  (키워드 엑셀)
      └─ brand/   (persona.json · presenter/ · themes.json · brand.json · logo)

**기본 프로필("default")은 하위호환**을 위해 레거시 경로를 그대로 가리킨다:
    logs/output/config/screenshots/previews → naver_blog_v3/ 밑(앱 루트)
    runs/reports                            → naver_blog_automation_v3/ 밑(하네스 루트)
    쿠키                                     → naver_blog_v3/_runtime/session_cookies.json
프로필을 쓰지 않던 기존 설치는 아무 것도 바뀌지 않는다(경로·동작 동일).

Phase 0 는 이 라우터 + 레지스트리 + (비파괴)이관 도구만 제공한다. 기존 모듈을 실제로
이 경로들로 갈아끼우는 작업은 Phase 1 이후에서 수행한다(그때까지 기존 상수 그대로).
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

# ── 루트(테스트에서 monkeypatch 가능하도록 모듈 전역) ──────────────
#   config/profile.py → config/ → naver_blog_v3/(앱 루트) → naver_blog_automation_v3/(하네스 루트)
_APP_ROOT = Path(__file__).resolve().parent.parent
_HARNESS_ROOT = _APP_ROOT.parent
_PROFILES_DIR = _HARNESS_ROOT / "profiles"
_ACTIVE_FILE = _PROFILES_DIR / ".active"
# default 프로필의 데이터를 profiles/<x>/ 로 재배치할 때 그 대상 프로필명을 담는 파일.
# 있으면 default 의 **데이터 경로만** 그 폴더로 라우팅(정체성·brand·is_default 는 불변 →
# examplebrand 동작 바이트 동일). 파일을 지우면 즉시 레거시(앱루트)로 복귀(완전 되돌림).
_DEFAULT_DATA_FILE = _PROFILES_DIR / ".default_data"

DEFAULT_PROFILE = "default"
# 프로필 이름 규칙 — 폴더/파일명 안전(영문·숫자·하이픈·언더스코어, 1~40자).
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")


# ─────────────────────────────────────────────────────
# 활성 프로필 해석 (환경변수 > .active 파일 > default)
# ─────────────────────────────────────────────────────
def active_profile() -> str:
    """현재 프로세스의 활성 프로필 이름. 유효하지 않으면 default."""
    name = os.environ.get("NBA_PROFILE", "").strip()
    if not name:
        try:
            if _ACTIVE_FILE.exists():
                name = _ACTIVE_FILE.read_text(encoding="utf-8").strip()
        except OSError:
            name = ""
    name = name or DEFAULT_PROFILE
    return name if is_valid_name(name) else DEFAULT_PROFILE


def is_default(name: Optional[str] = None) -> bool:
    return (name or active_profile()) == DEFAULT_PROFILE


def is_valid_name(name: str) -> bool:
    return bool(name) and (name == DEFAULT_PROFILE or bool(_NAME_RE.match(name)))


def profile_root(name: Optional[str] = None) -> Optional[Path]:
    """named 프로필의 루트(profiles/<name>). default 는 레거시 분산이라 None."""
    name = name or active_profile()
    if is_default(name):
        return None
    return _PROFILES_DIR / name


def _default_data_dir() -> Optional[Path]:
    """default 프로필의 데이터 재배치 대상(profiles/<x>/). `.default_data` 있으면 그 경로.

    ⚠️ **데이터 경로에만** 쓴다. 정체성 함수(is_default·profile_root·brand_dir)는 이걸
    참조하지 않으므로 default 는 여전히 examplebrand 코드 기본값을 쓴다(동작 바이트 동일).
    파일 없으면 None → 레거시 위치.
    """
    try:
        if _DEFAULT_DATA_FILE.exists():
            tgt = _DEFAULT_DATA_FILE.read_text(encoding="utf-8").strip()
            if tgt and tgt != DEFAULT_PROFILE and is_valid_name(tgt):
                return _PROFILES_DIR / tgt
    except OSError:
        pass
    return None


def _reloc_app_root() -> Path:
    """default 데이터의 앱루트 대체(재배치 시 profiles/<x>/, 아니면 레거시 앱루트)."""
    return _default_data_dir() or _APP_ROOT


def _reloc_harness_root() -> Path:
    """default 데이터의 하네스루트 대체(재배치 시 profiles/<x>/, 아니면 레거시 하네스루트)."""
    return _default_data_dir() or _HARNESS_ROOT


def default_data_target() -> Optional[str]:
    """default 프로필 데이터가 재배치된 대상 폴더명(profiles/<x>/). 재배치 안 됐으면 None."""
    dd = _default_data_dir()
    return dd.name if dd is not None else None


def set_default_data_target(name: Optional[str]) -> None:
    """default 데이터 재배치 활성/해제. name=폴더명이면 활성(profiles/<name>/로 데이터 라우팅),
    None 이면 `.default_data` 삭제(레거시로 즉시 복귀). ⚠️ 다음 프로세스 기동부터 반영."""
    _PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    if name and is_valid_name(name) and name != DEFAULT_PROFILE:
        _DEFAULT_DATA_FILE.write_text(name, encoding="utf-8")
    elif _DEFAULT_DATA_FILE.exists():
        _DEFAULT_DATA_FILE.unlink()


# ─────────────────────────────────────────────────────
# 경로 헬퍼 — default 는 레거시 위치, named 는 프로필 밑.
#   (부수효과 없음: 디렉토리를 만들지 않는다. 쓰기 측이 mkdir 하거나 ensure_dirs 사용.)
# ─────────────────────────────────────────────────────
def app_data_root(name: Optional[str] = None) -> Path:
    """**데이터** 루트(logs·output·screenshots·previews·config data) — default=앱루트(또는
    재배치 대상), named=프로필.

    ⚠️ 코드 자산(templates·_runtime·_tts_melo·settings.py)은 여기 두지 말 것 — 그건 항상
    앱 루트(_APP_ROOT)에 있는 공유 코드다. 각 모듈은 데이터 경로만 이 루트로 라우팅하고
    코드 경로는 기존 BASE_DIR(=_APP_ROOT) 유지(data_root 단일 스위치 원칙).
    """
    return profile_root(name) or _reloc_app_root()


def harness_data_root(name: Optional[str] = None) -> Path:
    """**데이터** 루트(runs·reports·briefs) — default=하네스루트(또는 재배치 대상), named=프로필."""
    return profile_root(name) or _reloc_harness_root()


def code_root() -> Path:
    """공유 코드/런타임 루트(templates·_runtime·_tts_melo·settings.py). 프로필 무관 상수."""
    return _APP_ROOT


def _app_or_profile(sub: str, name: Optional[str] = None) -> Path:
    """앱 루트(naver_blog_v3) 밑 하위폴더 — default=레거시(또는 재배치), named=프로필."""
    root = profile_root(name)
    return (root / sub) if root is not None else (_reloc_app_root() / sub)


def _harness_or_profile(sub: str, name: Optional[str] = None) -> Path:
    """하네스 루트 밑 하위폴더 — default=레거시(또는 재배치), named=프로필."""
    root = profile_root(name)
    return (root / sub) if root is not None else (_reloc_harness_root() / sub)


def logs_dir(name: Optional[str] = None) -> Path:
    return _app_or_profile("logs", name)


def output_dir(name: Optional[str] = None) -> Path:
    return _app_or_profile("output", name)


def screenshots_dir(name: Optional[str] = None) -> Path:
    return _app_or_profile("screenshots", name)


def previews_dir(name: Optional[str] = None) -> Path:
    return _app_or_profile("previews", name)


def config_dir(name: Optional[str] = None) -> Path:
    return _app_or_profile("config", name)


def runs_dir(name: Optional[str] = None) -> Path:
    return _harness_or_profile("runs", name)


def reports_dir(name: Optional[str] = None) -> Path:
    return _harness_or_profile("reports", name)


def briefs_dir(name: Optional[str] = None) -> Path:
    return _harness_or_profile("briefs", name)


def credentials_path(name: Optional[str] = None) -> Path:
    """프로필 자격증명·설정(user_settings.json). 암호화 키는 머신 기반이라 위치 무관."""
    return config_dir(name) / "user_settings.json"


def cookie_path(name: Optional[str] = None) -> Path:
    """세션 쿠키 — 계정 격리. default 는 레거시 _runtime/session_cookies.json
    (재배치 시 profiles/<x>/config/session_cookies.json)."""
    if is_default(name):
        dd = _default_data_dir()
        if dd is not None:
            return dd / "config" / "session_cookies.json"
        return _APP_ROOT / "_runtime" / "session_cookies.json"
    return config_dir(name) / "session_cookies.json"


def chrome_profile_dir(name: Optional[str] = None) -> Optional[Path]:
    """프로필별 Chrome user-data-dir(계정 교차 로그인 차단). default 는 None(기존 동작)."""
    root = profile_root(name)
    return (root / "config" / "chrome_profile") if root is not None else None


def brand_dir(name: Optional[str] = None) -> Optional[Path]:
    """브랜드 팩(페르소나·진행자 모델·테마·브랜드명). default 는 None(내장 examplebrand 기본)."""
    root = profile_root(name)
    return (root / "brand") if root is not None else None


def keywords_dir(name: Optional[str] = None) -> Optional[Path]:
    """프로필별 키워드 엑셀 폴더. named=프로필/keywords, default=None(레거시 settings 경로).
    단 default 데이터 재배치 시 profiles/<x>/keywords 로 라우팅(일관성)."""
    root = profile_root(name)
    if root is not None:
        return root / "keywords"
    dd = _default_data_dir()
    return (dd / "keywords") if dd is not None else None


# ── 네트워크(프록시·핑거프린트) — 다계정 링킹 방지(1계정=1IP=1지문) ──
# 페이블5 벤치마크 결론: 다른 네이버 계정을 같은 PC·같은 IP·같은 브라우저 지문으로
# 돌리면 네이버가 하나의 실체로 링킹(다계정 밴 1순위 유발). config/network.json 으로
# 프로필별 전용 프록시·지문 시드를 실어 Selenium 층에서 격리한다.
def network_config(name: Optional[str] = None) -> dict:
    """프로필 네트워크 설정(config/network.json). 없거나 파싱 실패 시 {}.

    형식: {"proxy": "http://user:pass@host:port", "fingerprint_seed": "임의문자열"}
    """
    path = config_dir(name) / "network.json"
    try:
        if path.exists():
            import json
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        pass
    return {}


def proxy_url(name: Optional[str] = None) -> str:
    """프로필 전용 아웃바운드 프록시 URL. 없으면 ''(프록시 미사용). 값은 로그에 노출 금지."""
    return str(network_config(name).get("proxy") or "").strip()


def fingerprint_seed(name: Optional[str] = None) -> str:
    """프로필별 브라우저 지문 시드(안티디텍트 층에서 일관된 지문 생성용). 없으면 프로필명."""
    seed = str(network_config(name).get("fingerprint_seed") or "").strip()
    return seed or (name or active_profile())


def has_proxy(name: Optional[str] = None) -> bool:
    return bool(proxy_url(name))


# ─────────────────────────────────────────────────────
# 레지스트리 — 생성/목록/전환
# ─────────────────────────────────────────────────────
# named 프로필의 표준 하위 디렉토리(생성 시 스캐폴드).
_PROFILE_SUBDIRS = ("config", "logs", "runs", "output", "reports",
                    "previews", "screenshots", "keywords", "brand")


def list_profiles() -> list[str]:
    """존재하는 프로필 이름(항상 default 포함, 정렬)."""
    names = {DEFAULT_PROFILE}
    try:
        if _PROFILES_DIR.exists():
            for d in _PROFILES_DIR.iterdir():
                if d.is_dir() and is_valid_name(d.name) and d.name != DEFAULT_PROFILE:
                    names.add(d.name)
    except OSError:
        pass
    return sorted(names)


def profile_exists(name: str) -> bool:
    if is_default(name):
        return True
    return is_valid_name(name) and (_PROFILES_DIR / name).is_dir()


def ensure_dirs(name: Optional[str] = None) -> None:
    """named 프로필의 표준 디렉토리를 생성(멱등). default 는 아무 것도 안 함."""
    root = profile_root(name)
    if root is None:
        return
    for sub in _PROFILE_SUBDIRS:
        (root / sub).mkdir(parents=True, exist_ok=True)


def create_profile(name: str) -> Path:
    """새 프로필 디렉토리 스캐폴드 생성. 반환: 프로필 루트. 이름 규칙 위반 시 ValueError."""
    if not is_valid_name(name) or is_default(name):
        raise ValueError(
            f"프로필 이름 규칙 위반(영문·숫자·하이픈·언더스코어 1~40자, 'default' 불가): {name!r}")
    root = _PROFILES_DIR / name
    ensure_dirs(name)
    return root


def set_active(name: str) -> None:
    """활성 프로필 포인터(.active) 갱신. named 은 존재해야 함. default 는 포인터 제거."""
    if not is_valid_name(name):
        raise ValueError(f"프로필 이름 규칙 위반: {name!r}")
    _PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    if is_default(name):
        _ACTIVE_FILE.unlink(missing_ok=True)
        return
    if not profile_exists(name):
        raise ValueError(f"존재하지 않는 프로필: {name!r} — create_profile 먼저")
    tmp = _ACTIVE_FILE.with_suffix(".tmp")
    tmp.write_text(name, encoding="utf-8")
    os.replace(str(tmp), str(_ACTIVE_FILE))


def delete_profile(name: str) -> None:
    """named 프로필 디렉토리를 통째로 삭제(계정·쿠키·데이터 포함). default 는 불가.

    ⚠️ 되돌릴 수 없다 — 호출측(GUI/CLI)이 사용자 확인을 받은 뒤에만 호출한다.
    .active 가 이 프로필을 가리키면 포인터를 해제(default 로 복귀).
    """
    if is_default(name):
        raise ValueError("default 프로필은 삭제할 수 없습니다")
    root = profile_root(name)
    if root is None or not root.is_dir():
        raise ValueError(f"존재하지 않는 프로필: {name!r}")
    import shutil
    shutil.rmtree(root)
    try:
        if _ACTIVE_FILE.exists() and _ACTIVE_FILE.read_text(encoding="utf-8").strip() == name:
            _ACTIVE_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def set_account(name: str, naver_id: str, naver_pw: str, **extra) -> Path:
    """프로필 자격증명을 **암호화** 저장(user_settings.json). 계정 격리의 핵심.

    기존 값과 병합(다른 설정 보존). 평문은 절대 저장하지 않는다(encrypt_dict).
    반환: 저장된 credentials 경로. named 프로필 전용(default 는 기존 config_io 로).
    """
    if not is_valid_name(name) or is_default(name):
        raise ValueError(f"named 프로필만 대상: {name!r}")
    import json
    from config.crypto import encrypt_dict
    cp = credentials_path(name)
    cp.parent.mkdir(parents=True, exist_ok=True)
    existing: dict = {}
    if cp.exists():
        try:
            existing = json.loads(cp.read_text(encoding="utf-8"))
            if not isinstance(existing, dict):
                existing = {}
        except (OSError, ValueError):
            existing = {}
    existing.update({"naver_id": naver_id, "naver_pw": naver_pw, **extra})
    enc = encrypt_dict(existing)                      # 민감 필드 Fernet 암호화
    tmp = cp.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(enc, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(str(tmp), str(cp))
    return cp


def describe(name: Optional[str] = None) -> dict:
    """프로필 요약(로그·GUI 표기용). ⚠️자격증명 값은 절대 노출하지 않음 — 존재/마스킹만."""
    name = name or active_profile()
    creds = credentials_path(name)
    has_creds = creds.exists()
    masked_id = ""
    if has_creds:
        try:
            import json
            from config.crypto import decrypt
            raw = json.loads(creds.read_text(encoding="utf-8"))
            nid = raw.get("naver_id", "")
            if isinstance(nid, str) and nid.startswith("ENC:"):
                nid = decrypt(nid)
            if nid:
                masked_id = _mask(nid)
        except Exception:  # noqa: BLE001 — 표기 실패가 흐름을 막지 않게
            masked_id = "(읽기 실패)"
    return {
        "profile": name,
        "is_default": is_default(name),
        "root": str(profile_root(name) or _APP_ROOT),
        "has_credentials": has_creds,
        "naver_id_masked": masked_id,
        "has_cookies": cookie_path(name).exists(),
    }


def _mask(s: str) -> str:
    """아이디 마스킹 — 앞 2 + *** + 뒤 1 (오발행 확인용, 원문 비노출)."""
    s = str(s)
    if len(s) <= 3:
        return s[:1] + "**"
    return f"{s[:2]}{'*' * max(3, len(s) - 3)}{s[-1]}"


# ─────────────────────────────────────────────────────
# 레거시(default) → named 프로필 무손실 이관
# ─────────────────────────────────────────────────────
# (레거시 절대경로, 프로필 내 상대경로, 종류). 이관 순서 = 목록 순서.
def _legacy_migration_map() -> list[tuple[Path, str, str]]:
    return [
        (_APP_ROOT / "config" / "user_settings.json", "config/user_settings.json", "file"),
        (_APP_ROOT / "_runtime" / "session_cookies.json", "config/session_cookies.json", "file"),
        (_APP_ROOT / "logs", "logs", "dir"),
        (_APP_ROOT / "output", "output", "dir"),
        (_APP_ROOT / "screenshots", "screenshots", "dir"),
        (_APP_ROOT / "previews", "previews", "dir"),
        (_HARNESS_ROOT / "runs", "runs", "dir"),
        (_HARNESS_ROOT / "reports", "reports", "dir"),
    ]


def plan_migration(name: str) -> list[dict]:
    """레거시 데이터를 profiles/<name> 로 옮길 계획(dry). 실제 복사/이동 없음.

    반환: [{src, dst, kind, exists}] — 존재하는 항목만 실제 이관 대상.
    """
    root = _PROFILES_DIR / name
    plan: list[dict] = []
    for src, rel, kind in _legacy_migration_map():
        plan.append({
            "src": str(src), "dst": str(root / rel), "kind": kind,
            "exists": src.exists(),
        })
    return plan


def migrate_legacy_to_profile(name: str, *, move: bool = False,
                              overwrite: bool = False) -> dict:
    """레거시(default) 데이터를 profiles/<name> 으로 **복사**(기본)하거나 이동.

    - 기본 move=False: 원본 보존(복사) — 안전. move=True 는 원본을 옮긴다(파괴적).
    - overwrite=False: 대상이 이미 있으면 스킵(멱등·덮어쓰기 방지).
    - 존재하는 항목만 처리. 반환: {copied, skipped, missing, moved}.

    ⚠️ 실제 실행은 호출측(사용자 트리거)에서. Phase 1 컷오버 시 이 함수로 이관한다.
    """
    import shutil
    if not is_valid_name(name) or is_default(name):
        raise ValueError(f"이관 대상은 named 프로필이어야 함: {name!r}")
    root = _PROFILES_DIR / name
    # ⚠️ ensure_dirs 로 빈 스캐폴드를 먼저 만들면 아래 "이미 존재→스킵"에 걸린다.
    #    빈 디렉토리는 미리 만들지 않는다 — 각 항목의 dst.parent 만 필요 시 생성.
    copied, skipped, missing, moved = [], [], [], []

    def _already_present(dst: Path, kind: str) -> bool:
        # 파일은 존재만으로, 디렉토리는 '내용이 있을 때'만 이미 이관된 것으로 본다.
        if kind == "dir":
            try:
                return dst.exists() and any(dst.iterdir())
            except OSError:
                return False
        return dst.exists()

    for src, rel, kind in _legacy_migration_map():
        dst = root / rel
        if not src.exists():
            missing.append(str(src))
            continue
        if _already_present(dst, kind) and not overwrite:
            skipped.append(str(dst))
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        if kind == "dir":
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dst)
        if move:
            (shutil.rmtree(src, ignore_errors=True) if kind == "dir"
             else src.unlink(missing_ok=True))
            moved.append(str(src))
        else:
            copied.append(str(dst))
    return {"copied": copied, "skipped": skipped, "missing": missing, "moved": moved}


__all__ = [
    "DEFAULT_PROFILE", "active_profile", "is_default", "is_valid_name",
    "profile_root", "app_data_root", "harness_data_root", "code_root",
    "logs_dir", "output_dir", "screenshots_dir", "previews_dir",
    "config_dir", "runs_dir", "reports_dir", "briefs_dir", "credentials_path",
    "cookie_path", "chrome_profile_dir", "brand_dir", "keywords_dir",
    "network_config", "proxy_url", "fingerprint_seed", "has_proxy",
    "list_profiles", "profile_exists", "ensure_dirs", "create_profile",
    "set_active", "set_account", "describe", "plan_migration",
    "migrate_legacy_to_profile",
]
