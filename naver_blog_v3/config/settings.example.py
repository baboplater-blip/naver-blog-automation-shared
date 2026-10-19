# config/settings.py — GUI 자동 생성
import os as _os
from pathlib import Path as _Path
from config.crypto import load_secret
try:
    from config.crypto import load_shared_secret
except ImportError:  # 낡은 프로세스가 crypto 를 재로드 못한 경우 등 — 임포트가 절대 안 깨지게
    def load_shared_secret(key, default=""):   # graceful fallback(활성 프로필 값만)
        return load_secret(key, default)

_BASE_DIR = _Path(__file__).parent.parent

NAVER_ID = load_secret("naver_id", "")
NAVER_PW               = load_secret("naver_pw")
# 무비번(쿠키) 로그인 모드 — 켜면 비밀번호를 저장/입력하지 않고 사용자가 브라우저에서
# 직접 로그인한다(관문 ③ 자격증명 투명성). 대신 무인 자동발행 시 쿠키 만료 후
# 재로그인이 안 되어 수동 로그인이 필요하다(신뢰↔무인 trade-off). 기본 False=현행.
NAVER_NO_PASSWORD_LOGIN = False
# ── 발행 경로(P0-① Phase 1) ─────────────────────────────────────────
# "spawn"(기본) = 매 발행마다 새 크롬 + naver_login (현행·무인 발행).
# "attach"      = 사용자가 직접 로그인해 둔 세션 크롬에 CDP 로 붙어 naver_login 을
#                 건너뛴다(로그인 개편·쿠키 24h·2FA·캡차 비적용). GUI 토글로 전환.
#                 attach 실패 시 발행은 fail-closed 로 중단(오발행 방지).
PUBLISH_MODE            = "spawn"
CHROME_ATTACH_PORT      = 9222          # 세션 크롬 원격 디버깅 포트(attach 모드).
CHROME_ATTACH_AUTOSTART = False         # 공유본: 사용자가 필요할 때 직접 켠다.
CHROME_SESSION_PROFILE_DIR = None       # 세션 전용 user-data-dir 오버라이드. None=프로필 규약.
DAILY_POST_LIMIT       = 20
POSTS_PER_DAY          = 3
POST_INTERVAL_SECONDS  = 180
SCHEDULE_HOURS         = ["06:00", "11:30"]
USE_RESERVATION        = True
RESERVATION_TIME_SLOTS = ["07:00", "08:30", "10:00", "12:00", "13:30", "17:00", "19:30", "21:00"]
KEYWORD_EXCEL_PATH = ""
if not _os.path.isabs(KEYWORD_EXCEL_PATH):
    KEYWORD_EXCEL_PATH = ""
KEYWORD_SHEET_NAME     = "메인키워드"
KEYWORD_LONGTAIL_SHEET = "롱테일"
KEYWORD_PUBLISH_POOL_SHEET = "발행후보_풀"
USE_NAVER_API           = False
# DataLab·검색 API 앱 키 — 계정 격리 불필요(어느 키워드든 조회). named 프로필에 없으면
# default 프로필 키로 폴백 → 새 프로필도 트렌드/검색이 바로 동작(401 방지).
NAVER_API_CLIENT_ID     = load_shared_secret("api_client_id", "your_client_id")
NAVER_API_CLIENT_SECRET = load_shared_secret("api_client_secret")
NAVER_API_PROVIDER      = "legacy"  # legacy | api_hub; applies to news/trend collection only
NAVER_API_HUB_KEY_ID    = load_shared_secret("api_hub_key_id")
NAVER_API_HUB_KEY       = load_shared_secret("api_hub_key")
TOURAPI_SERVICE_KEY     = load_shared_secret("tourapi_service_key")
SEOUL_API_KEY           = load_shared_secret("seoul_api_key")  # retained; direct source stays disabled until HTTPS is verified
USE_TREND_UPDATE        = False
ENABLE_CAFE_DISPATCH    = False
USE_KAKAO_NOTIFY       = False
KAKAO_ACCESS_TOKEN     = load_secret("kakao_token")
SAVE_PREVIEW_HTML      = True
PREVIEW_DIR            = "previews"
SKIP_IMAGE_INSERT      = True
# ── 이미지 자동 생성 (Codex CLI) ─────────────────────────────────
# True 면 content_generator 가 본문에 figure placeholder 6장(디지털 8장)을
# 삽입하고, auto_scheduler 가 ImageDispatcher 로 PNG 를 자동 생성한다.
# False 면 기존 SKIP_IMAGE_INSERT 흐름과 동일(이미지 미부착).
# 생성 한도: Codex Plus 5h 30회 — codex_cli_bot 의 usage_log 가 자동 추적.
IMAGE_INJECT_ENABLED         = False    # 이미지 기능 자체의 ON/OFF. 실제 생성 여부는 SKIP_IMAGE_INSERT
                                       # (='이미지 건너뛰기' 체크박스)가 최종 제어 — 체크 시 생성·삽입 모두 OFF (2026-07-14)
# 1편당 기본 슬롯 (thumb 1 + body 2 + info 3 = 6). 디지털 카테고리만 8장 허용.
IMAGE_PER_POST_DEFAULT       = 6
IMAGE_PER_POST_DIGITAL       = 8
# Tier 1(자동 codex) 실패 시 Tier 2(수동 폴백, 클립보드+인박스 폴더 오픈) 허용 여부.
IMAGE_DISPATCH_TIER2_ON_FAIL = True
# 카테고리별 단계적 활성화 (2026-05-25 추가). dict 이면 화이트리스트로 동작.
#   None — 전역 IMAGE_INJECT_ENABLED 만 반영 (= 기존 동작).
#   {"디지털": True, "default": False} — 디지털만 자동 생성, 그 외는 비활성.
# Codex Plus 5h 30회 한도가 있어 무조건 전역 활성화는 위험. 첫 도입 시 한 카테고리부터
# 점진 전환 → 한도·품질 관찰 후 default: True 로 전환 권장.
IMAGE_INJECT_BY_CATEGORY = None

# ── 📸 사진 인제스트(P0-② Phase 2) ─────────────────────────────────
# 여행·맛집 사진 폴더 → 사진마다 그 사진에 맞는 문단을 쓰고 순서대로 배치 →
# 실사진이 곧 본문 이미지가 되는 "사진으로 글쓰기". 전역 SKIP_IMAGE_INSERT 는
# 불변이고, 사진 글 경로에서만 per-post 신호(_force_image_insert)로 삽입을 켠다.
PHOTO_INGEST_ENABLED   = False
# 비전(사진 분석) 백엔드. "claude" = Claude CLI Read 도구로 로컬 이미지 분석(추가 비용·키 0).
# 그 외 값(예: "gemini")은 아직 배선되지 않아 파일명/EXIF 기반 최소 캡션으로 fail-open.
PHOTO_VISION_MODEL     = "claude"
# 사진 드롭 인박스 폴더(기본 naver_blog_v3/photo_inbox/). 프로필별 격리는 하위 {slug}/ 로.
PHOTO_INBOX_DIR        = str(_BASE_DIR / "photo_inbox")
# 사진 1장당 비전 분석 타임아웃(초). Read 왕복 지연이 커도 파이프라인은 fail-open.
PHOTO_VISION_TIMEOUT   = 180

# ── 🛒 쇼핑커넥트 제휴 리뷰(상품 기반 콘텐츠 소스, 2026-07-20) ──────────
# 쇼핑커넥트 제휴/상품 링크 → 상품 데이터 추출 → 제휴 리뷰 글 + 대가성 표시 + 발행.
# 데이터 소스 = C 하이브리드(공식 노출 메타 우선 → 쇼핑 검색 API 보강 → 수동 폴백).
# 실측(2026-07-20): 쇼핑 검색 API(search/shop.json)는 기존 자격증명으로 200 OK,
# 스마트스토어 상품 URL 직접 fetch 는 로그인 벽으로 막힘 → API 우선, 메타는 기회적.
# 인포카드 codex 이미지 생성 모델(2026-07-23). 계정·CLI 버전에 따라 지원 모델이
# 다르므로 실측 후 조정. 빈 값이면 codex 기본 모델.
# 2026-09-09 실측 기준 지원 모델: gpt-5.5 / gpt-5.3-codex-spark ("gpt-5.6" 은 400 거절).
IMAGE_CODEX_MODEL = "gpt-5.5"

SHOPPING_CONNECT_ENABLED = False
# 프로필별 제휴 링크(쇼핑커넥트 파트너 링크). 글에 공개되는 값이라 비밀이 아니지만,
# 프로필별 격리를 위해 자격증명 저장소(user_settings.json)를 재사용한다(GOV24_API_KEY
# 와 동급 — 평문 무해). 개별 발행 때 사용자가 붙인 링크가 우선이고, 이 값은 폴백 기본.
SHOPPING_AFFILIATE_LINK = ""
# 공정위 대가성 표시 문구 — 제휴 리뷰 글에 결정론적으로 항상 삽입(모델이 빼도 코드가 강제).
# 문구를 바꾸려면 이 값만 고치면 전 발행에 반영된다. 빈 값으로 두어도 코드가 기본 문구로 폴백.
SHOPPING_DISCLOSURE_TEXT = (
    "이 글은 네이버 쇼핑커넥트 활동의 일환으로, 구매 시 일정액의 수수료를 "
    "제공받을 수 있습니다."
)
# 상품 1건당 다운로드·삽입할 상품 이미지 최대 장수(원격 URL → 로컬 저장 → 본문 삽입).
PRODUCT_IMAGE_MAX        = 8

# ── 🎨 본문 분석형 2단계 인포카드(카드뉴스, 2026-07-23) ────────────────
# 본문 전체를 Claude 로 읽어 카드뉴스 세트를 기획(Stage1)한 뒤, 카드별 GPT-Image
# 프롬프트로 한글을 이미지에 직접 새긴 인포그래픽을 만든다(Stage2). 기존 고정 슬롯
# (thumb/body/info) + FLUX 무텍스트 일러스트 경로와 완전히 분리된 옵트인 기능이라,
# 기본 OFF 로 두면 기존 이미지 동작이 하나도 바뀌지 않는다.
IMAGE_INFOCARD_ENABLED   = False
# 한 편당 생성할 카드 최대 장수(hero 표지 + 정보 카드 + closing). 카드당 GPT-Image 1회.
IMAGE_INFOCARD_BUDGET    = 5
# 카드 캔버스 크기(세로형 카드뉴스). "가로x세로" 픽셀 표기.
IMAGE_INFOCARD_SIZE      = "1024x1280"
# 일관된 모델(예: 네이비 정장 한국 여성) 참조 이미지시트 경로. 비우면 텍스트 묘사만 사용.
IMAGE_MODEL_SHEET_PATH   = ""

# ── 로컬 ComfyUI 백엔드 (자기 GPU·무제한·무타임아웃·비공개) — 무텍스트 일러스트 유일 백엔드 ──
# 클라우드 무료 API(NVIDIA FLUX·Pollinations)는 큐 폭주로 통째로 죽고(2026-07 실측 504
# 타임아웃) 하드코딩 키까지 필요해 2026-07-24 폐기했다. 이제 무텍스트 일러스트는 자기
# RTX GPU 에서 도는 로컬 ComfyUI **단 하나**로만 생성한다(codex CLI 는 레거시 폴백).
# ComfyUI 가 켜져 있고 워크플로우 파일이 있으면 생성하고, 미가동이면 이미지 생성이 실패해
# 수동(Tier2) 폴백으로 넘어간다.
# 활성 조건 3가지: ENABLED=True + 서버 도달(127.0.0.1:8188) + 워크플로우 JSON 존재.
# 워크플로우는 ComfyUI 에서 '내보내기(API)' 한 JSON 을 아래 경로(또는 templates/comfy_workflow.json)에
# 두면 되고, 프롬프트·시드·크기는 하네스가 런타임에 자동 주입한다(수동 편집 불필요).
# 설정 가이드: docs/로컬_ComfyUI_이미지_백엔드.md (Krea 2 Turbo 권장).
IMAGE_COMFY_ENABLED       = False                     # 로컬 ComfyUI 사용(꺼지면 이미지 생성 실패→수동 폴백)
IMAGE_COMFY_URL           = "http://127.0.0.1:8188"  # ComfyUI 서버 주소
IMAGE_COMFY_WORKFLOW      = ""                        # 비우면 templates/comfy_workflow.json
IMAGE_COMFY_TIMEOUT       = 360                       # 생성 완료 대기(초) — 콜드 모델 로드(13GB 부분
                                                     # 오프로드)+LoRA 패칭 첫 장이 180초를 넘겨 폴백
                                                     # 캐스케이드가 나던 문제 → 넉넉히(2026-07-07)
IMAGE_COMFY_PING_TIMEOUT  = 2                         # 서버 도달 확인(빠르게, 없으면 즉시 폴백)
IMAGE_COMFY_POLL_INTERVAL = 1.0                       # /history 폴링 간격(초)
# 로컬 생성 해상도. 13GB fp8 모델이 12GB VRAM 을 넘어 부분 오프로드되는 상황에서 768 이
# 1024 대비 ~30% 빠름(실측 39.8s→27.4s). 삽화 배경 + 한글 오버레이(표시 ~800px)라 768 로
# 화질 충분. 화질 최우선이면 1024, 더 빠르게는 640.
IMAGE_COMFY_WIDTH         = 768
IMAGE_COMFY_HEIGHT        = 768
# GUI 시작 시 로컬 ComfyUI 자동 기동 — 기본 OFF(2026-07-24). 켜면 GUI 를 열 때마다 13GB
# 모델을 자동 로드해 시작이 느려지고 GPU/RAM 을 상시 점유한다. 필요할 때 Pinokio 로 직접
# 기동하는 편이 가벼워 기본 False. True 로 켜면 안 떠 있을 때만 detached 로 자동 기동한다.
# APP_DIR 이 비면 표준 Pinokio 경로(C:/pinokio/api/comfy-mirror.git/app 등)를 자동 탐색.
IMAGE_COMFY_AUTOSTART     = False
IMAGE_COMFY_APP_DIR       = ""        # 비우면 자동 탐색

# ── 젠스파크 「AI 이미지」(GPT Image 2) 클라이언트 (2026-08-29, Phase 1 이식) ──
# core/genspark_client.py 는 젠스파크 웹을 CDP attach 로 조종해 한글 baked 이미지를
# 만든다(계정 레지스트리=활성 프로필 logs/genspark_accounts.json, 기본 포트 9223 —
# 네이버 발행 attach 포트 CHROME_ATTACH_PORT=9222 와 분리). 이 플래그는 **파이프라인
# 자동 배선(Phase 2)** 게이트일 뿐이라 기본 False. 수동 스모크(python -m core.genspark_client)
# 는 이 값과 무관하게 동작한다(사용자 명시 실행). 로그인은 사람이 창에서 1회.
GENSPARK_ENABLED          = False

# ── 젠스파크 이미지 자동 채움(Phase 2a, 2026-08-29) ─────────────────────────────
# core/draft_image_fill.py 가 보관본(draft_ready)에 한글 baked 썸네일 + 카드뉴스를
# 채운다. 전부 GENSPARK_ENABLED 게이트 뒤 옵트인. 아래는 세부 파라미터(안전 기본값).
GENSPARK_THUMB_RATIO      = "1:1"    # 썸네일 비율(정사각 블로그 커버)
# 카드(세로) 비율. ★Phase 0 실측: "3:4" 선택 시 출력이 2:3 으로 나왔다 → 세로 카드가
# 확실히 나오는 "2:3" 을 기본으로 둔다. 썸네일(1:1)과 비율이 달라 반드시 별도 스레드.
GENSPARK_CARD_RATIO       = "2:3"
GENSPARK_INFOCARD_BUDGET  = 3        # 편당 카드 최대 장수(hero+정보+closing). 카드당 GPT-Image 1회
# 한 배치 실행당 처리할 최대 편수. 근거: 젠스파크 5시간 한도 ~70장/계정 ÷ ~4장/편 ≈ 17편,
# 2계정이면 ~34편이나 검증 재생성 여유를 두어 보수적으로 25편.
GENSPARK_MAX_POSTS_PER_RUN = 25
GENSPARK_AUTO_SWITCH      = True     # 한도 도달 시 account_order 로 다른 계정 자동 전환
GENSPARK_VERIFY_MAX_REGEN = 2        # 비전 검증 실패 시 같은 스레드 교정 재생성 최대 횟수
GENSPARK_VISION_MODEL     = "claude" # baked 한글 검증 백엔드(Claude CLI Read). 그 외는 미배선
# 이미지 품질 — '자동'=크레딧 무소모(사용자 확정). high 는 크레딧 확인창을 띄운다.
GENSPARK_QUALITY          = "자동"

# ── 🖼 이미지 채움(엔진 중립) ───────────────────────────────────────────────
IMAGE_FILL_ENABLED = False          # 이미지 자동 채움 마스터 스위치
IMAGE_FILL_ENGINE = "codex"        # "codex" | "genspark"
IMAGE_FILL_CARDS = 3               # 편당 본문 이미지 수 (0이면 썸네일만)
IMAGE_FILL_MAX_POSTS_PER_RUN = 25  # 배치 1회 최대 글 수
IMAGE_FILL_THUMB_RATIO = "1:1"
IMAGE_FILL_CARD_RATIO = "2:3"
# 한 편 안에서 동시에 만들 이미지 수. 1이면 순차, 상한 4. 워커마다 엔진을 따로 열고
# (세션 분리) 호출 예산·요청 저널은 하나를 공유한다.
IMAGE_FILL_PARALLEL = 2

# ── 🗺 이미지 기획(계획) — 2026-09-10 ────────────────────────────────────────
# True 면 글을 만들 때 **같은 집필 호출**에서 이미지 기획(썸네일 짧은 문구 + 본문 이미지
# 소재)을 함께 받아 보관본에 저장한다(기획 전용 LLM 호출 0). 채움은 그 계획대로 생성만 한다.
IMAGE_PLAN_ENABLED = False
# True(기본) 면 채움이 저장된 계획을 쓰는 새 경로로 간다 — 본문은 글자 없는 관련 이미지라
# 비전(OCR) 검증을 하지 않는다. False 면 예전 인포카드 경로(카드 기획 LLM + 카드마다 비전).
IMAGE_FILL_PLAN_MODE = True
# 썸네일에 새긴 짧은 문구만 기존 비전 검증으로 확인(편당 Claude 최대 1회). False 면 0회.
# ※ 아래 THUMB_LOCAL_TEXT=True로 되돌리면 모델이 글자를 쓰지 않으므로 이 검증은 일어나지
#   않는다(검증할 '모델이 쓴 글자'가 없다 → 편당 비전 호출 0회).
IMAGE_FILL_VERIFY_THUMB = True

# ── 🖼 썸네일 한글 로컬 합성 — 2026-09-18 ───────────────────────────────────
# True(되돌리기): 이미지 생성 엔진은 **글자 없는 배경**만 만들고, 한글은 Pillow 로 우리가
#   직접 합성한다(`core/thumbnail_compose.py`). 모델이 한글을 잘못 새기는 오류가 없어지고
#   (받은 문구를 그대로 렌더한다) 비전(OCR) 검증 호출이 사라진다. 계정별 색·메시지는
#   '편집 방향' 설정에서 온다. ⚠️ 문구 자체가 틀렸거나 글꼴에 없는 글자는 그대로 나온다.
#   ⚠️ 시스템에 한글 폰트(맑은 고딕·Noto Sans KR·나눔고딕 등)가 있어야 한다. 없으면
#   썸네일 슬롯이 **실패로 남아 발행이 막힌다**(글자 없는 썸네일을 완성으로 넘기지 않음).
#   폰트를 직접 지정하려면 환경변수 NBA_THUMB_FONT 에 .ttf/.otf 경로를 준다.
# False(기본): 모델이 실제 주제 장면과 24자 이내 한글을 함께 디자인하고 비전으로 확인한다.
# GUI 비관리 키: 저장 시 명시한 값이 유지되며, 키가 없어도 False 기본값을 사용한다.
THUMB_LOCAL_TEXT = False

# ── 🧭 편집 브리프(본문·썸네일 공동 기획) — 2026-09-18 ──────────────────────
# True(기본): 집필 호출의 **같은 JSON** 에 `editorial`(독자 질문·답 요약·다음 행동·썸네일
#   메시지·표/FAQ 구성 선언)을 함께 받아 보관본에 저장한다(기획 전용 LLM 호출 0).
#   본문과 썸네일이 같은 질문을 겨냥하게 되고, 품질 검사도 그 구성 선언과 정합해 동작한다
#   (표·FAQ 를 쓰지 않겠다고 선언한 글만 해당 요구가 해제된다).
# False: 예전 경로 — 이미지 소재(image_plan)만 받고 표·FAQ 는 전역 규칙으로 강제한다.
EDITORIAL_BRIEF_ENABLED = True

# ── 🤖 Codex 이미지 엔진 ────────────────────────────────────────────────────
CODEX_IMAGE_MODEL = "gpt-5.5"          # 계정 실측 지원 모델(gpt-5.5 / gpt-5.3-codex-spark)
CODEX_IMAGE_TIMEOUT = 600              # 1장 생성 타임아웃(초)
CODEX_IMAGE_MAX_CALLS_PER_RUN = 40     # 폭주 방지 상한
CODEX_IMAGE_CONFIG_OVERRIDES = ""      # 탈출구: "key=value,key2=value2" (보통 비워둠)

# ── D.I.A.+ / AEO 자동 보완 (2026-05-25) ─────────────────
# quality_validator.validate_and_fix(enable_dia_aeo=...) 옵션을 카테고리별로
# 결정한다. None 이면 비활성(전체 OFF) — 기존 동작 유지(하위 호환).
# dict 이면 화이트리스트: {"디지털": True, "default": False} 처럼 운영 가능.
#   - 디지털 카테고리부터 점진 도입 권장. 보강 재작성은 Claude CLI 1회 추가 호출
#     이라 한도/속도에 영향을 준다. 한 카테고리 검증 후 default: True 로 확장.
# (2026-07-03) 네이버 AI 요약 노출이 키워드 순위보다 중요해져 AEO 를 전 카테고리로
# 확대. default: True. 특정 카테고리만 끄려면 그 키에 False 를 명시.
DIA_AEO_AUTOFIX_BY_CATEGORY = {"default": True}

# ── 예약 이월 (2026-05-26) ──────────────────────────────
# runs/*/publish_brief.yaml 에 success=True 로 기록된 미래 예약을 감지해
# `assign_reservation_times` 의 시작점을 "마지막 예약일 + 1" 로 자동 이월한다.
# 같은 날 두 번 run_pipeline 을 돌리거나, save_only 로 쌓아둔 초안을 며칠 뒤
# 일괄 발행할 때 발생하던 시간 슬롯 중복 충돌을 방지.
# False 면 옛 동작(target_date+1 고정) 유지 — 하위 호환.
RESERVATION_RESPECT_EXISTING = True
# 발행 큐 정렬 결과 캐시(2026-09-02) — 홈 탭 '발행 준비' 신호등이 큐 1,797건
# 재계산(검색량·데이터랩·마감·뉴스)으로 ~19초 걸리던 것을 단축. 같은 큐 +
# 같은 소스 데이터(검색량 엑셀·trend_data.json·deadline_data.json·뉴스 레이더
# 상태·excluded_titles.json)면 이전 정렬 순서를 재사용, 하나라도 바뀌면 자동
# 미스(재계산). False 면 옛 동작(매번 전체 재계산) 복귀.
QUEUE_SORT_CACHE_ENABLED = True
# 발행 후 자동 쇼츠 영상 생성(Phase②) — ON 이면 실발행 완료 후 detached
# 백그라운드로 영상 없는 최근 글 AUTO_VIDEO_LIMIT 편의 멀티채널 팩을 생성(발행 흐름 비차단).
# (2026-07-12) shorts_studio 연동과 함께 다시 ON — 렌더는 아래 SHORTS_STUDIO_* 참조.
AUTO_VIDEO_AFTER_PUBLISH = False
AUTO_VIDEO_LIMIT         = 1
# (2026-07-19) 발행 후 배치는 캡션 4종만 생성 — 쇼츠 영상은 shorts_studio 자동 운행이
# 실발행(예약 시각) 후 공개 글 URL로 만든다(사용자가 직접 넣은 이미지 포함 최종본).
# 예약 등록 직후의 이미지 없는 중간본으로 영상을 미리 만들던 이중 생성을 차단.
# False 면 옛 동작(캡션+영상 렌더까지) 복귀.
AUTO_VIDEO_CAPTION_ONLY  = True
# ── 쇼츠 독립 서비스(shorts_studio :8788) 연동 (2026-07-12) ──
# build_multichannel_pack 의 렌더 단계가 이 서버를 우선 사용(Qwen3-TTS·말투 프리셋·
# SRT 자막·라우드니스 정규화). 미가동·타임아웃·실패 시 기존 로컬 렌더러로 자동 폴백.
# 서버 기동: 하네스\shorts_studio\쇼츠스튜디오_시작.bat (또는 python server.py)
SHORTS_STUDIO_ENABLED  = False
SHORTS_STUDIO_AUTOSTART = False    # GUI 켜면 쇼츠 서버 자동 기동(이미 떠 있으면 그대로)
SHORTS_STUDIO_URL      = "http://127.0.0.1:8788"
SHORTS_STUDIO_STYLE    = "info"   # 말투: webnovel|info|kids_story|docu 또는 "custom:<지시>"
SHORTS_STUDIO_VOICE    = ""       # 빈 값 = 서버 기본(Qwen Sohee). GET /v1/voices 로 목록 확인
SHORTS_STUDIO_DURATION = 30       # 목표 길이(초): 15|30|60|90
SHORTS_STUDIO_TIMEOUT  = 900      # 잡 완료 대기 상한(초) — 초과 시 로컬 폴백
SHORTS_STUDIO_API_KEY  = ""       # 서버에 키 인증 걸었을 때만 채움
# 발행 대상 신원 게이트(오발행 방지, 멀티 계정/프로필) — 발행 직전 브라우저 로그인 계정이
# NAVER_ID 와 일치하는지 검증. 'off'/'warn'/'strict'. 기본 'warn'(확실한 불일치는 항상 차단,
# 판독불가는 경고 후 진행). 멀티계정 운영이면 판독 신뢰 확인 후 'strict' 권장(판독불가도 차단).
PUBLISH_IDENTITY_GATE    = "warn"
# 정부24/정책포털 마감 공고 추적·엑셀 주입(deadline_tracker) 사용 여부. 예시브랜드(정부지원)
# 전용 기능 — 영어교육 등 다른 분야 프로필에선 False 로 꺼서 정부 공고가 발행 큐에
# 주입되지 않게 한다. 기본 True(기존 동작 유지, default 프로필 무변화).
DEADLINE_TRACKER_ENABLED = False
HEADLESS               = False
MAX_RETRY_COUNT        = 2
RETRY_WAIT_SECONDS     = 30
DUPLICATE_THRESHOLD    = 0.80
CLAUDE_CLI_TIMEOUT     = 420   # 웹검색 ON 시 자동으로 +60되어 480s가 됨. (2026-07-12) 300→420: 전 카테고리 sonnet 전환 여유분.
API_REQUEST_TIMEOUT    = 10
HTTP_REQUEST_TIMEOUT   = API_REQUEST_TIMEOUT   # 호환성 별칭
SUBPROCESS_TIMEOUT     = 180
MAX_DISPATCH_QUEUE     = 200
POST_HISTORY_MAX       = 500
# ── 품질 규칙 ──
# [정책 소개형] YMYL(복지·지원금) 글을 '확정 수치 확인 글'이 아니라 '제도 소개 +
# 공식 창구 안내' 로 쓴다. 정확한 금액·지원율·소득기준·수치표를 생략하고 제도 목적·
# 대상 상황·신청 경로·공식 링크 중심(목표 1,200~1,800자). 검증 대상 주장 수가 줄어
# 한 편당 사실 확인 비용이 크게 내려간다. False 로 되돌리면 예전 동작(2,200~3,000자,
# 수치 포함)으로 완전히 복귀한다. 판정은 fact_gate 의 YMYL 카테고리 집합을 그대로 쓴다.
POLICY_BRIEF_MODE  = True

QV_MIN_CHARS       = 1800
# (2026-08-02 P1-④a) 2500 → 3200. 보관본 1,807편 실측에서 다수가 이미 상한에 눌려
# _trim_content 가 ## 섹션째 절삭 중이었다. 판단 흔적·3줄 요약·FAQ 5개가 얹히면
# 새 차별 신호가 절삭 1순위가 되므로 상한을 올린다. 하한(1800)은 불변.
QV_MAX_CHARS       = 3200
QV_REQUIRE_TABLE   = True
QV_REQUIRE_FAQ     = True
QV_MIN_H2_COUNT    = 3
QV_MIN_TAGS        = 10
QV_MAX_RETRIES     = 2
# ── 검색량 게이트 (무수요 초-롱테일 과잉 세분화 방지, 2026-07-03) ──
# core/search_volume.py 가 보유 엑셀('*키워드*검색량*.xlsx')을 진실원본으로 사용.
# 발행 232건 분석 결과 59%가 검색 수요 없는 소주제였고(방문자 하락 원인),
# 광역 키워드 1개가 최대 12편으로 파쇄됨 → 아래 게이트로 차단.
SEARCH_VOLUME_GATE_ENABLED = True   # False 면 게이트 전면 비활성(엑셀 없으면 자동 fail-open)
MIN_SEARCH_VOLUME          = 500    # 파생/발행 키워드가 앵커해야 할 최소 월 검색량
# 엑셀 검색량 미달이어도 네이버 자동완성에 뜨는 '실검색어'면 통과(키워드 목록 자동 확장).
# 좁은 엑셀 화이트리스트가 실제 검색되는 온-브랜드 주제(주거급여·보금자리론 등)를 오차단하던
# 문제 해소(2026-07-15). core/live_demand.py. False 면 기존 엑셀-단독 게이트로 복귀.
LIVE_DEMAND_GATE_ENABLED   = True
# 엑셀에 볼륨은 없지만 자동완성 실검색어로 확인된 키워드에 부여할 '바닥 볼륨'.
# 랭킹(opportunity_score)·파생 게이트(keyword_expander)에서 0 취급돼 맨 뒤로 밀리거나
# 폐기되는 걸 막는다(랭킹 하한선). 실측 볼륨이 아니라 '최소 발행선' 표식(2026-07-15).
LIVE_DEMAND_FLOOR_VOLUME   = 500

# ── 순위 추적: 구체 롱테일 쿼리 병행(2026-08-01) ─────────────────────────
# rank_tracker 가 각 글에서 헤드 키워드(초경쟁·천장, tab="blog")만 재던 한계 보강.
# 글이 실제 노출·트래픽을 얻는 구체 쿼리(예 '주거급여 지급일')를 query_evidence
# (검색광고 실측 하위검색어)로 뽑아 별 tab("blog_specific")에 함께 기록 →
# performance_db.near_miss_posts 가 #11~30위(1페이지 문턱) 리프레시 타깃을 산출한다.
# 기존 헤드 경로·소비자(rank_alerts·rank_watchlist·get_summary)는 불변.
# False 면 헤드-only(기존 동작)로 복귀. 글당 조회 최대 2배(구체=헤드거나 없으면 1회).
RANK_TRACK_SPECIFIC      = True
RANK_SPECIFIC_MIN_VOLUME = 500      # 구체 쿼리 수요 하한(이 밑은 무수요라 스킵)
RANK_SPECIFIC_MAX_VOLUME = 3000     # 구체 쿼리 수요 상한(이 위는 헤드급 경쟁 → 후순위)

# ── 경쟁 블로그 분석 → 글쓰기 고도화(2026-08-02) ─────────────────────────
# 상위노출 전문 경쟁 블로그의 제목·주제 공식을 '신호'로만 학습해(표현 모방 0) 생성
# 프롬프트에 주입한다(competitor_analyzer). 옵트인 — True 여야 content_generator 가
# prompt_hint 를 붙인다(기본 False = 기존 생성 완전 무영향). 발굴·리포트는 플래그와
# 무관하게 수동/운영 루프로 실행 가능하고, 캐시(logs/competitor_profiles.json, TTL 7일)만
# 읽어 주입하므로 생성 경로에 네트워크가 걸리지 않는다.
#   ⚠️ 주제 관련성 필터 필수: 상위노출 최상위가 잡블로그(오프토픽)일 때가 많아, 후보의
#   최근 글 제목이 우리 도메인 어휘와 겹치는 비율이 임계 이상인 전문 블로그만 채택한다.
COMPETITOR_ANALYSIS_ENABLED = False
COMPETITOR_MIN_RELEVANCE    = 0.40   # 전문 블로그 판정 임계(온브랜드 제목 비율)
COMPETITOR_MAX_BLOGS        = 12     # 분류·분석할 후보 상한(SERP 등장 빈도 상위)
COMPETITOR_POSTS_PER_BLOG   = 20     # 후보당 조회할 최근 글 수

# ── 설계도 주도 변주(Blueprint-Driven Variation, 2026-08-25) ────────────
# 글마다 훅·골격·어법·판단흔적을 결정론적으로 배정해 "같은 시작·같은 구조·같은 말투"
# AI 티를 제거. 장르(정보/맛집/상품/일상/방법)에 따라 어법·안전선·출처강제가 달라진다.
# 데이터는 templates/variation_banks.json(코드 수정 없이 어떤 주제에서도 작동).
# 기본 OFF — OFF 이면 생성 프롬프트가 기존과 바이트 동일. 라이브 검증 후 켤 것.
BLUEPRINT_ENABLED = False

# ── AI 티 스코어러 Phase A(관측 전용, 2026-08-29) ──────────────────────
# 자동 생성 글의 "AI가 쓴 티"를 정량 채점(0~100)해 data["ai_tell"] 에 기록만 한다.
# Phase A 는 발행 게이트(차단)를 절대 하지 않는다. OFF 면 생성 경로가 기존과 바이트 동일.
AI_TELL_SCORE_ENABLED = False

# ── 안티-AI-티 규칙 프롬프트 이식 Phase C(2026-08-29) ──────────────────
# avoid-ai-writing 방법론의 한국어 안티-AI-티 규칙을 글 생성 프롬프트에 주입하는 옵트인 플래그.
# 라이브 데이터(variation_banks.json)는 안 건드린다. OFF(기본) 이면 프롬프트가 기존과 바이트 동일.
ANTI_AI_TELL_PROMPT = False

# ── 리스타일 배치 AI 티 우선순위 정렬 Phase D(2026-08-29) ──────────────
# ON 이면 draft_restyle 배치가 AI 티 최악 보관본부터 리스타일한다(정렬만·발행 차단 아님).
# OFF(기본) 이면 정렬이 기존 수요 우선 그대로라 배치가 기존과 바이트 동일(스코어링 0).
RESTYLE_ORDER_BY_AI_TELL = False

# ── 네이버 검색광고 API — 실측 월간검색수(2026-07-15) ──────────────────
# 무료 절대검색량 소스. 켜지면 effective_volume 이 엑셀 미등록 키워드에 대해
# '자동완성 floor(500)' 대신 '진짜 월간검색수(PC+모바일)'를 조회해 랭킹 정확도↑.
# 자격증명은 searchad.naver.com → 도구 → API 사용관리에서 발급(개인 가능, 무료).
# 저장은 Fernet 암호화(_lib/set_searchad_creds.py 로 안전 입력). 키 없으면 available()=False
# 로 자동 우회(무영향). ENABLED=True + 3개 키 모두 있어야 실사용.
SEARCHAD_API_ENABLED   = False
SEARCHAD_API_KEY       = load_shared_secret("searchad_api_key")       # 액세스 라이선스(암호화)
SEARCHAD_SECRET_KEY    = load_shared_secret("searchad_secret_key")    # 비밀키(암호화)
SEARCHAD_CUSTOMER_ID   = load_shared_secret("searchad_customer_id")   # 고객 ID(계정번호, 평문)
SEARCHAD_CACHE_TTL_DAYS = 7   # 월간검색수 캐시 유효기간(월 단위 갱신이라 7일 충분)
# 2026-07-19 — 보조금24(공공데이터포털 data.go.kr) 인증키(선택). 정부 지원사업
# '신청기한'을 매일 수집해 마감 임박 글감을 발행 큐 최상단으로 올린다(deadline_tracker).
# 공개 데이터 조회 전용 키라 평문 무해(검색광고 customer_id 와 동급). 비면 휴면 —
# 복지로 키리스 수집은 키 없이도 항상 동작한다.
GOV24_API_KEY = load_shared_secret("gov24_api_key")

# ── 🆓 무료 데이터 소스 확장 (2026-08-16) — 전부 opt-in·fail-open, 추가 비용 0 ──
# 각 소스는 사전 수집한 신호를 생성 프롬프트에 한 블록씩 주입한다(네트워크는 수집
# 단계에서만, 생성 경로엔 안 걸림). 소비자는 getattr 로 읽어 줄이 없어도 안전(기본
# OFF). 라이브 품질 검증 전까지 전부 기본 OFF(옵트인).
# ① 지식iN·백과·책 — 같은 네이버 검색 자격증명(키 추가 0). 실측 사용자 질문 →
#    FAQ·질문형 제목 근거 + 백과 정의 사실 근거.
KNOWLEDGE_SOURCES_ENABLED   = False
KNOWLEDGE_SOURCES_TTL_DAYS  = 14
KNOWLEDGE_REFRESH_PER_RUN   = 30      # 일간 루프에서 새로 채울 미수집 키워드 상한(로테이션)
# ② data.go.kr 보조금24 공식 사실 근거(지원내용·대상·기한). GOV24_API_KEY 재사용.
GOV_FACTS_ENABLED           = False
GOV_FACTS_INDEX_PAGES       = 4       # 목록 페이지 예산(×300건). 늘리면 커버리지↑
GOV_FACTS_INDEX_TTL_HOURS   = 24
# ③ YouTube Data API v3 — 트렌드·경쟁 각도·질문. 무료 키(하루 10,000 units).
YOUTUBE_RADAR_ENABLED       = False
YOUTUBE_API_KEY             = load_shared_secret("youtube_api_key")
YOUTUBE_DAILY_BUDGET        = 40      # 일일 search 콜 상한(×100 units)
YOUTUBE_RADAR_TTL_DAYS      = 14
# ④ 한국은행 ECOS + 통계청 KOSIS — 공식 통계 권위(금리·물가·환율 등). 무료 키.
STATS_AUTHORITY_ENABLED     = False
ECOS_API_KEY                = load_shared_secret("ecos_api_key")
KOSIS_API_KEY               = load_shared_secret("kosis_api_key")
STATS_AUTHORITY_TTL_HOURS   = 24
# ⑤ Google Trends(pytrends·비공식·보조) — 계절성·급상승. 키 0, 미설치 시 자동 무효.
GOOGLE_TRENDS_ENABLED       = False
GOOGLE_TRENDS_TTL_DAYS      = 7

SERIES_MAX_EPISODES        = 3      # 시리즈 팬아웃 상한(광역 대형 키워드도 최대 3편)
# ── 보관본(draft_store) 발행 매칭 나이 상한 (2026-07-12) ──
# find_draft_for_title 이 발행 큐 제목과 draft_ready 보관본을 매칭할 때 허용하는
# created_at 최대 경과 일수. 기존 하드코딩 30일은 5월 대량 생성 백로그(elite 497편)
# 전체를 발행 경로에서 가려버렸다(2026-07-12 실측: 1,164편 중 1,163편이 30일 초과
# → 오늘 큐 매칭 0/20, 전부 신규 생성). 백로그 소진 계획(125일)이 끝날 때까지
# 보관본이 계속 보여야 하므로 연 단위로 설정. 소진 완료 후 30으로 되돌려도 된다.
DRAFT_MAX_AGE_DAYS = 365
# ── 보관함 우선 소진 (archive-first drain, 2026-07-13) ──
# run_pipeline 이 매 실행에서 발행 큐(엑셀 트렌드 정렬)의 상위 N편을 신규 생성하기
# 전에, 보관함(draft_ready elite)을 먼저 소진하도록 드레인 리스트를 큐 앞에 붙인다.
# True(기본)면 드레인 활성 — 보관함 우선 + 부족분만 신규 생성. False 면 기존 흐름
# 100% 보존(반응형 find_draft_for_title lookup 만). save_only/dry_run 은 드레인 대상이
# 아니다(항상 스킵). GUI 화이트리스트 밖 상수라 재생성에도 사라지지 않는다.
DRAFT_DRAIN_ENABLED = True
# ── 제도-단위 중복 게이트 (사촌 글 자기잠식 방지, 2026-07-08 · 개선안 #3) ──
# check_duplicate(title-only)를 통과하더라도, 같은 정규화 제도명(에너지바우처·
# 긴급생계지원 등) 발행 편수가 최근 WINDOW 일 안에 MAX 를 초과하면 신규 발행을
# 차단하고 기존 최약 사촌을 리프레시하도록 브리프를 남긴다(폐기 아님).
# MAX 근거: 필러 1 + 세부 2 = 제도당 3편이면 토픽 권위에 충분하고, 그 이상은
# 서로 검색 유입을 갉아먹는 자기잠식(에너지바우처 ~7편 사례). SERIES_MAX_EPISODES
# (시리즈 팬아웃 상한 3)와 같은 근거·같은 값으로 정렬했다.
INSTITUTION_DEDUP_ENABLED       = True   # False 면 게이트 비활성(title-only 동작 그대로)
INSTITUTION_DEDUP_WINDOW_DAYS   = 30     # 같은 제도 발행 편수를 세는 윈도우(일)
INSTITUTION_DEDUP_MAX_PER_WINDOW = 3     # 윈도우 내 허용 편수(초과 시 신규 차단)
INSTITUTION_BODY_SIM_THRESHOLD  = 0.55   # 골격어 제외 후 본문 Jaccard 차단 임계
# ── Claude CLI 모델 ──
# content_generator.py:269 이 import → 매핑 없는 카테고리·매핑 로드 실패 시 이 기본 모델을 사용.
# (2026-07-03) 그간 미정의라 import 가 매번 ImportError 로 떨어져 하드코딩 "sonnet" fallback 이
# 쓰였음 → 명시 변수로 승격해 GUI/설정에서 조정 가능하게 함. 값은 기존 fallback 과 동일.
CLAUDE_MODEL = "sonnet"
# ── Claude CLI 카테고리별 모델 매핑 ──
# content_generator.py:276 이 import → 매핑 없는 카테고리는 기본 CLAUDE_MODEL("sonnet") 사용.
# 키는 templates/prompt_strategies.json 의 12 카테고리와 1:1 매칭(default·forbidden_words_global 제외).
# (2026-05-15) 정부지원 전 카테고리를 haiku 로 — 240s 타임아웃 사이클 해소 목적.
# (2026-07-12) 전 카테고리 sonnet 복귀 — 하쿠는 금지어 QA 3연속 실패로 발행 차단이 반복됐고
# (retries_exceeded, 호출 3배 소모 후 발행 0), 품질 실측도 sonnet 우위(AEO 재생성 29→46 vs
# 40→100). 타임아웃 사이클은 CLAUDE_CLI_TIMEOUT 420s + 보관본 배선(생성량 절반)으로 해소.
# alias 거부 시 명시 ID("claude-sonnet-5" 등)로 교체.
CLAUDE_MODEL_BY_CATEGORY = {
    "청년":   "sonnet",
    "복지":   "sonnet",
    "교육":   "sonnet",
    "취업":   "sonnet",
    "주거":   "sonnet",
    "의료":   "sonnet",
    "바우처": "sonnet",
    "부동산": "sonnet",
    "절세":   "sonnet",
    "금융":   "sonnet",
    "노후":   "sonnet",
    "디지털": "sonnet",
    "환경":   "sonnet",
}
# ── 카테고리별 품질 룰 오버라이드 ──
# quality_validator.py:64 getattr(settings, "QV_CATEGORY_OVERRIDES", {}) 가 머지.
# 디지털 카테고리는 단계별 안내라 본문이 짧고 표/FAQ 형태가 달라 기본 룰에서 자주 FAIL → 완화.
# (2026-05-08 메모리에 "적용 완료" 기록 → 실제로는 사라졌었음. 2026-05-15 복원.)
QV_CATEGORY_OVERRIDES = {
    "디지털": {"min_chars": 1500, "require_table": False, "require_faq": False, "min_tags": 8},
    # 사진 인제스트(P0-② Phase 2) 실경험 글은 콘텐츠 타입 특성상 사진 10~14장 × 문단이라
    # 자연히 길다. 기본 상한(2500)이면 매번 초과→_trim_content 가 ## 헤딩 4개짜리 구조를
    # hard_trim(문자 절삭)으로 표/FAQ/마치며를 파괴한다. 사진 글에만 넉넉한 상한을 줘
    # 트림과 안 싸우게 한다(주제 하드코딩 아님 — 콘텐츠 타입 파라미터). 표/FAQ/헤딩/태그
    # 요건은 그대로 유지(실경험 글도 방문 요약 표·FAQ 3개를 갖춤).
    "사진후기": {"max_chars": 12000},
    # 🛒 쇼핑커넥트 제휴 리뷰(2026-07-20) — 상품 리뷰는 정부지원 가이드보다 자연히 짧다.
    # 하한을 낮춰 "1,800자 채우려 없는 사양·효능을 지어내는" 날조 압력을 줄인다(컴플라이언스).
    # 표(제품 요약)·FAQ·헤딩·태그 요건은 그대로 유지. 상한은 넉넉히.
    "상품리뷰": {"min_chars": 1200, "max_chars": 4000},
}
# ── 하네스 통합 ──
# True 면 auto_scheduler 실행 시 ../briefs ../runs ../reports 에
# 표준화된 brief/qa/publish_brief/리포트 파일을 자동 생성한다.
# 관측 전용이라 발행 동작에는 영향 없음.
HARNESS_EMIT_ENABLED   = True
BLOG_CATEGORY_NO       = None
PAGE_TIMEOUT           = 15
ELEMENT_WAIT           = 10
THUMBNAIL_WIDTH        = 800
THUMBNAIL_HEIGHT       = 800
CARD_WIDTH             = 800
IMAGE_THEMES = {
    "청년":{"bg":"#0d1b2a","accent":"#00b4d8","sub":"#90e0ef"},
    "주거":{"bg":"#1a0a2e","accent":"#f72585","sub":"#ff9ece"},
    "복지":{"bg":"#0a1628","accent":"#06d6a0","sub":"#80ffdb"},
    "바우처":{"bg":"#1a1100","accent":"#ffd60a","sub":"#ffe566"},
    "취업":{"bg":"#001524","accent":"#ff6b35","sub":"#ffb38a"},
    "의료":{"bg":"#1a0010","accent":"#e040fb","sub":"#f5a7ff"},
    "교육":{"bg":"#0d1f0d","accent":"#69f0ae","sub":"#b9ffd8"},
    "default":{"bg":"#111827","accent":"#4f9cf9","sub":"#93c5fd"},
}
WINDOWS_FONTS = {"regular":"C:/Windows/Fonts/malgun.ttf","bold":"C:/Windows/Fonts/malgunbd.ttf","fallback":"C:/Windows/Fonts/gulim.ttc"}
GOVERNMENT24_URL_MAP = {
    "청년도약계좌":"https://www.gov.kr/portal/rcvfvrSvc/dtlEx/415000000082",
    "청년내일저축계좌":"https://www.gov.kr/portal/rcvfvrSvc/dtlEx/415000000059",
    "국민취업지원제도":"https://www.gov.kr/portal/rcvfvrSvc/dtlEx/400098400006",
    "근로장려금":"https://www.gov.kr/portal/rcvfvrSvc/dtlEx/400000000002",
    "주거급여":"https://www.gov.kr/portal/rcvfvrSvc/dtlEx/400061700001",
    "기초연금":"https://www.gov.kr/portal/rcvfvrSvc/dtlEx/400000000018",
    "기초생활수급":"https://www.gov.kr/portal/rcvfvrSvc/dtlEx/400000000020",
    "에너지바우처":"https://www.gov.kr/portal/rcvfvrSvc/dtlEx/400000000062",
    "국가장학금":"https://www.gov.kr/portal/rcvfvrSvc/dtlEx/400000000045",
    "문화누리카드":"https://www.gov.kr/portal/rcvfvrSvc/dtlEx/400000000065",
    "디지털온누리":"https://www.gov.kr/portal/rcvfvrSvc/dtlEx/400000000150",
    "아이돌봄":"https://www.gov.kr/portal/rcvfvrSvc/dtlEx/400000000042",
}
DEFAULT_GOV24_URL = "https://www.gov.kr/portal/main/index"

# ── 무인 운영(autonomy) M1~M4 (2026-09-18) ────────────────────────────
# 전부 **기본 OFF**. 켜지 않으면 큐·대조·원장·정지 스위치·중재 어느 것도 기존
# 파이프라인에 개입하지 않는다(설계 §9 되돌림 표).
AUTONOMY_ENABLED = False
UPLOAD_RECONCILE_ON_UNCONFIRMED = False
COST_LEDGER_ENABLED = False
BROWSER_ARBITER_ENABLED = False
MEASUREMENT_V2_ENABLED = False
AUTONOMY_CATCHUP_MAX = 2
AUTONOMY_MACHINE_ROOT = str(_BASE_DIR / ".local" / "machine")
COST_LEDGER_GLOBAL_LIMITS = {}
MULTI_PROFILE_TIMEOUT = 3600
