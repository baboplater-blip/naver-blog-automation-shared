# core/naver_api_uploader.py
# ③ 네이버 오픈API 방식 업로드 (Selenium 불필요)
# 네이버 개발자센터에서 앱 등록 후 사용 가능
# https://developers.naver.com/apps/

import json, os, sys, time, urllib.request, urllib.parse, urllib.error
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.logger import get_logger

try:
    from config.settings import HTTP_REQUEST_TIMEOUT
except (ImportError, AttributeError):
    HTTP_REQUEST_TIMEOUT = 10
log = get_logger(__name__)
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
# OAuth 토큰은 **계정 자격증명** — 프로필별 격리(2026-07-19 격리 감사).
# 공유 위치에 두면 프로필 A 의 토큰으로 B 가 API 발행하는 교차오염이 난다.
try:
    from config import profile as _nbaprofile
    TOKEN_FILE = _nbaprofile.config_dir() / "naver_token.json"
except Exception:  # noqa: BLE001 — 프로필 레이어 부재 시 레거시
    TOKEN_FILE = BASE_DIR / "config" / "naver_token.json"


# ─────────────────────────────────────────────────────
# OAuth2 토큰 관리
# ─────────────────────────────────────────────────────
class NaverAuth:
    """네이버 OAuth2 인증 및 토큰 갱신 관리"""

    def __init__(self, client_id: str, client_secret: str):
        self.client_id     = client_id
        self.client_secret = client_secret
        self._token        = self._load_token()

    def _load_token(self) -> dict:
        from config import safe_load_json
        return safe_load_json(TOKEN_FILE, default=dict)

    def _save_token(self, token: dict):
        from config import atomic_write_json
        atomic_write_json(TOKEN_FILE, token)
        self._token = token

    def get_auth_url(self, state: str = "blog_auto") -> str:
        """브라우저에서 열어야 하는 인증 URL 반환"""
        params = urllib.parse.urlencode({
            "response_type": "code",
            "client_id":     self.client_id,
            "redirect_uri":  "https://localhost",
            "state":         state,
        })
        return f"https://nid.naver.com/oauth2.0/authorize?{params}"

    def exchange_code(self, code: str) -> bool:
        """인증 코드를 액세스 토큰으로 교환"""
        try:
            params = urllib.parse.urlencode({
                "grant_type":    "authorization_code",
                "client_id":     self.client_id,
                "client_secret": self.client_secret,
                "code":          code,
                "state":         "blog_auto",
            })
            req = urllib.request.Request(
                f"https://nid.naver.com/oauth2.0/token?{params}")
            with urllib.request.urlopen(req, timeout=HTTP_REQUEST_TIMEOUT) as resp:
                data = json.loads(resp.read())
                if "access_token" in data:
                    data["expires_at"] = time.time() + int(data.get("expires_in", 3600))
                    self._save_token(data)
                    log.info("  ✅ 네이버 API 토큰 발급 완료")
                    return True
        except urllib.error.URLError as e:
            log.error(f"  ❌ 토큰 교환 실패 (네트워크): {e}")
        except json.JSONDecodeError as e:
            log.error(f"  ❌ 토큰 교환 실패 (응답 파싱): {e}")
        return False

    def refresh(self) -> bool:
        """액세스 토큰 갱신"""
        refresh_token = self._token.get("refresh_token")
        if not refresh_token:
            return False
        try:
            params = urllib.parse.urlencode({
                "grant_type":    "refresh_token",
                "client_id":     self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": refresh_token,
            })
            req = urllib.request.Request(
                f"https://nid.naver.com/oauth2.0/token?{params}")
            with urllib.request.urlopen(req, timeout=HTTP_REQUEST_TIMEOUT) as resp:
                data = json.loads(resp.read())
                if "access_token" in data:
                    data["refresh_token"] = refresh_token  # 기존 refresh 유지
                    data["expires_at"]    = time.time() + int(data.get("expires_in", 3600))
                    self._save_token(data)
                    log.info("  🔄 토큰 갱신 완료")
                    return True
        except urllib.error.URLError as e:
            log.error(f"  ❌ 토큰 갱신 실패 (네트워크): {e}")
        except json.JSONDecodeError as e:
            log.error(f"  ❌ 토큰 갱신 실패 (응답 파싱): {e}")
        return False

    @property
    def access_token(self) -> str:
        # 만료 5분 전에 자동 갱신
        if time.time() > self._token.get("expires_at", 0) - 300:
            self.refresh()
        return self._token.get("access_token", "")

    @property
    def is_valid(self) -> bool:
        return bool(self._token.get("access_token"))


# ─────────────────────────────────────────────────────
# 네이버 블로그 API 클라이언트
# ─────────────────────────────────────────────────────
class NaverBlogAPI:
    BASE = "https://openapi.naver.com/blog"

    def __init__(self, auth: NaverAuth):
        self.auth = auth

    def _headers(self) -> dict:
        return {
            "Authorization":  f"Bearer {self.auth.access_token}",
            "Content-Type":   "application/json; charset=UTF-8",
        }

    def write_post(self,
                   title:        str,
                   content_html: str,
                   tags:         list,
                   category_no:  str  = None,
                   publish_date: str  = None) -> dict:
        """
        블로그 글 등록
        publish_date: "2026-03-05T07:00:00" → 예약발행 (None이면 즉시)
        Returns: {"post_id": "...", "url": "..."}
        """
        body = {
            "title":       title,
            "content":     content_html,
            "tags":        ",".join(str(t) for t in tags[:30]),
            "allowComment": True,
            "openType":    "all",
        }
        if category_no:
            body["categoryNo"] = str(category_no)
        if publish_date:
            body["publishDate"] = publish_date   # 예약발행 시각

        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req  = urllib.request.Request(
            f"{self.BASE}/write",
            data=data,
            headers=self._headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=HTTP_REQUEST_TIMEOUT) as resp:
                result = json.loads(resp.read())
                post_id = result.get("postId", "")
                url     = result.get("postUrl", "")
                log.info(f"  ✅ API 발행 완료 | postId={post_id}")
                return {"post_id": post_id, "url": url, "success": True}
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8")
            if e.code in (401, 403):
                log.error(f"  ❌ 인증 오류 ({e.code}) — 토큰 갱신 시도")
                if self.auth.refresh():
                    # 토큰 갱신 후 1회 재시도
                    req2 = urllib.request.Request(
                        f"{self.BASE}/write", data=data,
                        headers=self._headers(), method="POST")
                    try:
                        with urllib.request.urlopen(req2, timeout=HTTP_REQUEST_TIMEOUT) as resp2:
                            result2 = json.loads(resp2.read())
                            post_id = result2.get("postId", "")
                            url     = result2.get("postUrl", "")
                            log.info(f"  ✅ 토큰 갱신 후 발행 완료 | postId={post_id}")
                            return {"post_id": post_id, "url": url, "success": True}
                    except urllib.error.HTTPError as e2:
                        body2 = e2.read().decode("utf-8")
                        log.error(f"  ❌ 재시도 실패 {e2.code}: {body2[:200]}")
                        return {"success": False, "error": f"{e2.code}: {body2[:100]}"}
                return {"success": False, "error": f"auth_{e.code}: {body[:100]}"}
            elif e.code == 429:
                log.warning(f"  ⚠️  레이트 리밋 (429) — 30초 대기 후 재시도")
                time.sleep(30)
                req3 = urllib.request.Request(
                    f"{self.BASE}/write", data=data,
                    headers=self._headers(), method="POST")
                try:
                    with urllib.request.urlopen(req3, timeout=HTTP_REQUEST_TIMEOUT) as resp3:
                        result3 = json.loads(resp3.read())
                        post_id = result3.get("postId", "")
                        url     = result3.get("postUrl", "")
                        log.info(f"  ✅ 레이트 리밋 대기 후 발행 완료 | postId={post_id}")
                        return {"post_id": post_id, "url": url, "success": True}
                except urllib.error.HTTPError as e3:
                    body3 = e3.read().decode("utf-8")
                    log.error(f"  ❌ 재시도 실패 {e3.code}: {body3[:200]}")
                    return {"success": False, "error": f"{e3.code}: {body3[:100]}"}
            else:
                log.error(f"  ❌ API 오류 {e.code}: {body[:200]}")
                return {"success": False, "error": f"{e.code}: {body[:100]}"}
        except urllib.error.URLError as e:
            log.error(f"  ❌ API 요청 실패 (네트워크): {e}")
            return {"success": False, "error": str(e)}

    def upload_image(self, image_path: str) -> str:
        """이미지 업로드 후 삽입 URL 반환"""
        filename = os.path.basename(image_path)
        try:
            with open(image_path, "rb") as f:
                img_data = f.read()
            boundary = "----WebKitFormBoundary"
            body  = f"--{boundary}\r\n"
            body += f'Content-Disposition: form-data; name="image"; filename="{filename}"\r\n'
            body += "Content-Type: image/png\r\n\r\n"
            body  = body.encode() + img_data + f"\r\n--{boundary}--\r\n".encode()

            headers = self._headers()
            headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
            req = urllib.request.Request(
                f"{self.BASE}/image/upload",
                data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=HTTP_REQUEST_TIMEOUT) as resp:
                result = json.loads(resp.read())
                return result.get("imageUrl", "")
        except urllib.error.HTTPError as e:
            if e.code == 401:
                log.info("이미지 업로드 토큰 만료, 갱신 시도")
                self.auth.refresh()
                # 1회 재시도 (재귀 방지를 위해 플래그 사용)
                if not getattr(self, '_image_retry', False):
                    self._image_retry = True
                    try:
                        result = self.upload_image(image_path)
                        return result
                    finally:
                        self._image_retry = False
            log.warning(f"  이미지 업로드 HTTP 오류 ({filename}): {e.code}")
            return ""
        except (urllib.error.URLError, OSError) as e:
            log.warning(f"  이미지 업로드 실패 ({filename}): {e}")
            return ""

    def get_blog_info(self) -> dict:
        """내 블로그 정보 조회"""
        req = urllib.request.Request(
            f"{self.BASE}/info",
            headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=HTTP_REQUEST_TIMEOUT) as resp:
                return json.loads(resp.read())
        except urllib.error.URLError as e:
            log.debug(f"  블로그 정보 조회 실패: {e}")
            return {}


# ─────────────────────────────────────────────────────
# 고수준 업로드 함수 (파이프라인에서 호출)
# ─────────────────────────────────────────────────────
def _images_expected(post_data: dict) -> bool:
    """이 글이 **이미지와 함께** 발행돼야 하는가.

    `core.naver_uploader._images_expected` 와 같은 의미론을 쓴다(설정을 두 벌로
    해석하지 않도록 그 함수를 그대로 빌려 쓰고, 못 불러오면 보수적으로 판정한다).
    """
    try:
        from core.naver_uploader import _images_expected as _impl
        return bool(_impl(post_data))
    except Exception:  # noqa: BLE001 — 업로더를 못 불러오면 설정을 직접 본다
        if (post_data or {}).get("_force_image_insert"):
            return True
        try:
            from config.settings import SKIP_IMAGE_INSERT
            return not SKIP_IMAGE_INSERT
        except Exception:  # noqa: BLE001
            return False


def _image_gate_error(post_data: dict, image_paths: list) -> str:
    """API 발행 전 이미지 자산 판정. 통과면 "", 막아야 하면 사유 코드.

    Selenium 경로와 **같은 판정기**(`core.image_asset_guard`)를 쓴다. 판정기가
    죽으면 보수적으로 막는다(fail-closed) — 확인되지 않은 이미지를 올리는 것보다
    발행을 한 번 거르는 쪽이 싸다.
    """
    paths = [p for p in (image_paths or []) if p]
    if not paths:
        if _images_expected(post_data):
            return ("이미지가 0장이라 발행을 보류합니다 — 이 글은 이미지와 함께 "
                    "발행하도록 설정돼 있습니다")
        return ""
    missing = [p for p in paths if not os.path.exists(str(p))]
    if missing:
        return "이미지 파일이 누락되어 발행을 보류합니다"
    try:
        from core import image_asset_guard as _guard
        ok, findings, _pub = _guard.verify_publishable(
            paths,
            title=str((post_data or {}).get("title") or ""),
            category=str((post_data or {}).get("category") or ""))
        if not ok:
            return ("발행할 수 없는/확인되지 않은 이미지가 있어 발행을 보류합니다 — "
                    + _guard.describe(findings))
    except Exception:  # noqa: BLE001 — 판정기 자체가 죽으면 막는다
        log.error("이미지 자산 판정 실패로 발행 보류", exc_info=True)
        return "이미지 자산 판정 실패로 발행을 보류합니다"
    return ""


def upload_post_api(post_data: dict,
                    image_paths: list,
                    reserve_datetime: str = None) -> dict:
    """
    네이버 API로 글 + 이미지를 업로드합니다.
    Selenium 불필요 — 네트워크 호출만으로 완전 처리.

    Returns: {"success": bool, "url": str, "post_id": str}
    """
    try:
        from config.settings import NAVER_API_CLIENT_ID, NAVER_API_CLIENT_SECRET, BLOG_CATEGORY_NO
    except ImportError:
        log.error("  ❌ settings.py에 NAVER_API_CLIENT_ID / NAVER_API_CLIENT_SECRET 없음")
        log.info("     네이버 개발자센터(https://developers.naver.com)에서 앱 등록 후 설정하세요.")
        return {"success": False, "error": "API 키 미설정"}

    auth = NaverAuth(NAVER_API_CLIENT_ID, NAVER_API_CLIENT_SECRET)
    if not auth.is_valid:
        log.error("  ❌ 네이버 API 인증 토큰 없음")
        log.info(f"     브라우저에서 아래 URL 열고 코드를 config/naver_token.json에 저장하세요:")
        log.info(f"     {auth.get_auth_url()}")
        return {"success": False, "error": "인증 필요"}

    api = NaverBlogAPI(auth)

    # ── 0. 이미지 자산 게이트 (2026-09-23) ────────────────────────────────
    # Selenium 경로(`naver_uploader._prepare_publish_images`)가 막는 것들 —
    # 자리표시 카드·참고용 외부 사이트 캡처·깨진 파일·출처 미확인 자산·
    # "이미지가 필요한 글인데 0장" — 을 이 경로는 하나도 보지 않고 있었다.
    # `USE_NAVER_API=True` 로 바꾸는 순간 그 방어가 통째로 사라지던 우회를 막는다.
    # 판정은 같은 단일 진실 원본(`core.image_asset_guard`)을 쓴다.
    gate_error = _image_gate_error(post_data, image_paths)
    if gate_error:
        log.error("  ⛔ %s", gate_error)
        return {"success": False, "error": gate_error}

    # 1. 이미지 업로드 → URL 치환
    content_html = post_data.get("content_html", "")
    log.info(f"  📸 이미지 {len(image_paths)}장 업로드 중...")
    uploaded = 0
    for i, img_path in enumerate(image_paths):
        if os.path.exists(img_path):
            img_url = api.upload_image(img_path)
            if img_url:
                content_html += f'\n<img src="{img_url}" style="width:100%;max-width:800px;margin:8px 0;">'
                uploaded += 1
    # 부분 업로드로 조용히 발행하지 않는다 — 요구한 장수가 다 올라가야 글을 등록한다.
    if image_paths and uploaded != len(image_paths):
        log.error("  ⛔ 이미지 %s장 중 %s장만 업로드돼 발행을 보류합니다",
                  len(image_paths), uploaded)
        return {"success": False, "error": "image_upload_incomplete"}

    # 2. 예약발행 시각 포맷 변환
    publish_date = None
    if reserve_datetime:
        try:
            dt = datetime.strptime(reserve_datetime, "%Y-%m-%d %H:%M")
            publish_date = dt.strftime("%Y-%m-%dT%H:%M:%S")
        except ValueError:
            publish_date = None

    # 3. 글 등록
    result = api.write_post(
        title        = post_data.get("title", ""),
        content_html = content_html,
        tags         = post_data.get("tags", []),
        category_no  = BLOG_CATEGORY_NO,
        publish_date = publish_date,
    )

    return result


# ─────────────────────────────────────────────────────
# API 연동 설정 가이드 출력
# ─────────────────────────────────────────────────────
def print_setup_guide():
    log.info("""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 네이버 오픈API 설정 방법
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

 1. https://developers.naver.com 접속
 2. Application → 애플리케이션 등록
 3. API 선택: [블로그] 체크
 4. Callback URL: https://localhost
 5. Client ID / Client Secret 복사

 6. config/settings.py에 추가:
    NAVER_API_CLIENT_ID     = "발급받은_Client_ID"
    NAVER_API_CLIENT_SECRET = "발급받은_Client_Secret"
    USE_NAVER_API           = True

 7. GUI 설정 탭 → "API 연동" → "토큰 발급" 클릭

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
""")
