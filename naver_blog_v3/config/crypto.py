# config/crypto.py — 자격증명 암호화/복호화 (Fernet + PBKDF2)

import base64, os, getpass, platform
from pathlib import Path

_SALT_FILE = Path(__file__).parent / ".salt"

# 민감 필드 목록
# searchad_* : 네이버 검색광고 API 자격증명(2026-07-15). customer_id 는 계정번호라
# 비밀은 아니지만 api_key/secret_key 는 반드시 암호화. customer_id 는 평문 허용(목록 제외).
SENSITIVE_KEYS = ("naver_pw", "kakao_token", "api_client_secret", "naver_id", "api_client_id",
                  "searchad_api_key", "searchad_secret_key", "tourapi_service_key",
                  "seoul_api_key", "api_hub_key_id", "api_hub_key")


def _get_key() -> bytes:
    """머신별 고유 암호화 키 파생 (PBKDF2)."""
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes

    if not _SALT_FILE.exists():
        salt = os.urandom(16)
        _SALT_FILE.write_bytes(salt)
    else:
        salt = _SALT_FILE.read_bytes()

    seed = f"{getpass.getuser()}@{platform.node()}".encode()
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32,
                      salt=salt, iterations=100_000)
    return base64.urlsafe_b64encode(kdf.derive(seed))


def encrypt(plaintext: str) -> str:
    """평문 → 'ENC:암호문'. 빈 문자열이면 그대로 반환."""
    if not plaintext:
        return ""
    from cryptography.fernet import Fernet
    return "ENC:" + Fernet(_get_key()).encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    """'ENC:암호문' → 평문. ENC: 접두사 없으면 그대로 반환."""
    if not ciphertext or not ciphertext.startswith("ENC:"):
        return ciphertext or ""
    from cryptography.fernet import Fernet, InvalidToken
    try:
        return Fernet(_get_key()).decrypt(ciphertext[4:].encode()).decode()
    except (ValueError, TypeError, InvalidToken) as e:
        import logging
        logging.getLogger(__name__).debug(f"복호화 실패: {e}")
        return ""


def encrypt_dict(d: dict) -> dict:
    """딕셔너리의 민감 필드를 암호화한 복사본 반환."""
    out = dict(d)
    for key in SENSITIVE_KEYS:
        val = out.get(key, "")
        if val and not val.startswith("ENC:"):
            out[key] = encrypt(val)
    return out


def decrypt_dict(d: dict) -> dict:
    """딕셔너리의 암호화된 민감 필드를 복호화한 복사본 반환."""
    out = dict(d)
    for key in SENSITIVE_KEYS:
        val = out.get(key, "")
        if val and val.startswith("ENC:"):
            out[key] = decrypt(val)
    return out


def load_secret(key, default=""):
    """user_settings.json에서 암호화된 민감 값을 복호화해서 반환.

    평문 민감 필드가 발견되면 자동으로 암호화 마이그레이션을 수행한다.
    """
    import json
    from cryptography.fernet import InvalidToken
    try:
        # 활성 프로필별 자격증명(계정 격리). default 는 config/user_settings.json 동일.
        try:
            from config import profile as _p
            cfg_path = _p.credentials_path()
        except Exception:  # noqa: BLE001 — 프로필 미가용 시 레거시 경로
            cfg_path = Path(__file__).parent / "user_settings.json"
        if not cfg_path.exists():
            return default
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        # 평문 민감 필드가 있으면 자동 마이그레이션 (암호화 재저장)
        if has_plaintext_secrets(raw):
            migrated = encrypt_dict(raw)
            _atomic_write_cfg(cfg_path, migrated)
            raw = migrated
        val = raw.get(key, default)
        if val and isinstance(val, str) and val.startswith("ENC:"):
            return decrypt(val)
        return val or default
    except (json.JSONDecodeError, FileNotFoundError, KeyError, OSError,
            InvalidToken) as e:
        import logging
        logging.getLogger(__name__).debug(f"시크릿 로드 실패 ({key}): {e}")
        return default


_PLACEHOLDER_SECRETS = ("your_client_id", "your_client_secret", "your_naver_id")


def _default_credentials_path() -> Path:
    """default 프로필의 user_settings.json 경로(공유 앱 키 폴백용). 테스트에서 monkeypatch.

    default 데이터가 profiles/<x>/ 로 재배치된 경우(profiles/.default_data) 그쪽을
    따른다 — named 프로필의 공유 키 폴백이 재배치 이전의 스테일 레거시 파일을 읽고
    401/403 을 내는 것을 방지(2026-07-19 exampleprofile 403 실측). 재배치 없으면 기존 경로.
    """
    try:
        from config import profile as _profile
        return _profile.credentials_path(_profile.DEFAULT_PROFILE)
    except Exception:  # noqa: BLE001 — 프로필 레이어 부재 시 레거시 경로
        return Path(__file__).parent / "user_settings.json"


def load_shared_secret(key, default=""):
    """앱 레벨 시크릿(DataLab·검색 API 앱 키 등)을 로드 — 계정 격리가 불필요한 값.

    활성 프로필에 값이 없거나 placeholder면 **default 프로필**(config/user_settings.json)로
    폴백한다. 개발자 앱 키는 블로그 계정과 무관(어느 키워드든 조회)하므로 프로필 간 공유해도
    안전하다. ⚠️ blog naver_id/naver_pw 는 절대 이 함수를 쓰지 않는다(크로스포스팅 방지).
    """
    import json
    from cryptography.fernet import InvalidToken

    # 1) 활성 프로필 자체 값 우선 (프로필이 자기 앱 키를 지정했으면 그것)
    val = load_secret(key, "")
    if val and val not in _PLACEHOLDER_SECRETS:
        return val

    # 2) default 프로필(config/user_settings.json)로 폴백
    try:
        legacy = _default_credentials_path()
        if legacy.exists():
            raw = json.loads(legacy.read_text(encoding="utf-8"))
            v = raw.get(key, "")
            if v and isinstance(v, str) and v.startswith("ENC:"):
                return decrypt(v)
            if v and v not in _PLACEHOLDER_SECRETS:
                return v
    except (json.JSONDecodeError, FileNotFoundError, KeyError, OSError, InvalidToken) as e:
        import logging
        logging.getLogger(__name__).debug(f"공유 시크릿 폴백 실패 ({key}): {e}")
    return default


def _atomic_write_cfg(path: Path, data: dict) -> None:
    """user_settings.json 원자적 저장 (config.atomic_write_json 순환 임포트 방지)."""
    import json
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(str(tmp), str(path))


def has_plaintext_secrets(d: dict) -> bool:
    """평문 민감 필드가 있는지 확인 (마이그레이션 필요 여부)."""
    for key in SENSITIVE_KEYS:
        val = d.get(key, "")
        if val and not val.startswith("ENC:"):
            return True
    return False
