# core/shorts_studio_client.py — 독립 쇼츠 서비스(shorts_studio :8788) 연동 클라이언트 (2026-07-12)
"""발행 후 쇼츠 생성을 개선 엔진(shorts_studio)으로 위임하는 얇은 HTTP 클라이언트.

shorts_studio 는 별도 폴더(하네스\\shorts_studio)의 독립 FastAPI 서비스로,
구 로컬 렌더러(core/shorts_renderer.py) 대비 다음이 개선돼 있다:
  · TTS 3단 체인: Qwen3-TTS(GPU 고품질, 웜 데몬) → MeloTTS → edge-tts
  · 말투 프리셋(webnovel/info/kids_story/docu) + 감정 + 배속
  · SRT/VTT 자막 파일 산출(유튜브 업로드용) · -14 LUFS 라우드니스 정규화
  · 상주 렌더서버로 매 잡 node 부팅 제거

설계 원칙 (fail-open):
  · 서버 미가동·타임아웃·잡 실패 등 어떤 문제든 호출자가 기존 로컬 렌더러로
    폴백할 수 있도록 None 반환(예외를 밖으로 던지지 않음).
  · 산출물은 기존 경로 규약(runs/{slug}/multichannel/shorts.mp4)에 내려받아
    find_pending_video_slugs 멱등 판별·GUI 버튼과 그대로 호환된다.
  · 의존성 0 — 표준 라이브러리 urllib 만 사용(블로그 앱 venv 오염 없음).
"""
from __future__ import annotations

import html as _html
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

from config.logger import get_logger

log = get_logger(__name__)

_TERMINAL = {"done", "failed", "canceled"}
_ARTIFACT_WANT = ("shorts.mp4", "thumbnail.jpg", "subtitles.srt", "subtitles.vtt",
                  "meta.json", "script.json")
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]+")
_NL_RE = re.compile(r"\n{3,}")
_IMG_MARKER_RE = re.compile(r"<!--\s*IMG:[^>]*-->")


def _cfg(name: str, default):
    try:
        from config import settings as _s
        return getattr(_s, name, default)
    except ImportError:
        return default


def base_url() -> str:
    return str(_cfg("SHORTS_STUDIO_URL", "http://127.0.0.1:8788")).rstrip("/")


def _request(url: str, *, payload: dict | None = None, timeout: float = 10.0):
    """GET(payload=None) 또는 JSON POST. 응답 JSON dict 반환."""
    data = None
    headers = {"Accept": "application/json"}
    api_key = str(_cfg("SHORTS_STUDIO_API_KEY", "") or "")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def is_alive(timeout: float = 3.0) -> bool:
    """GET /healthz — 서버 가동 여부. 어떤 예외든 False."""
    try:
        r = _request(f"{base_url()}/healthz", timeout=timeout)
        return bool(r.get("ok"))
    except Exception:  # noqa: BLE001 — 폴백 판단용이므로 광범위 캐치가 맞다
        return False


def to_plain_text(content: str) -> str:
    """본문(HTML 또는 마크다운)을 shorts_studio 입력용 플레인 텍스트로 정리."""
    if not content:
        return ""
    text = _IMG_MARKER_RE.sub("", content)
    text = _TAG_RE.sub(" ", text)
    text = _html.unescape(text)
    text = text.replace("**", "").replace("##", "").replace("|", " ")
    text = _WS_RE.sub(" ", text)
    text = _NL_RE.sub("\n\n", text)
    return text.strip()[:8000]   # ingest 상한과 동일


def _download(url: str, dest: Path, timeout: float = 120.0) -> bool:
    headers = {}
    api_key = str(_cfg("SHORTS_STUDIO_API_KEY", "") or "")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            dest.write_bytes(resp.read())
        return True
    except (urllib.error.URLError, OSError) as e:
        log.warning(f"[shorts_studio] 아티팩트 다운로드 실패 {dest.name}: {e}")
        return False


def try_render_via_studio(post_data: dict, out_dir: Path, *,
                          slug: str = "", fallback_text: str = "") -> Optional[dict]:
    """shorts_studio 로 쇼츠 1편 생성 시도. 성공 시 결과 dict, 실패·미가동 시 None.

    fallback_text: 본문이 없거나 짧을 때 대신 쓸 소스(예: caption_generator 가
        이미 만든 shorts_script 대본). 발행 직후 슬러그는 runs/ 에
        publish_brief.yaml 만 있어 본문이 비는 경우가 실측으로 확인됨.

    반환 dict: {"mp4": Path, "thumbnail": Path|None, "elapsed_sec": float,
                "engine": "shorts_studio", "job_id": str}
    호출자는 None 이면 기존 로컬 렌더러로 폴백하면 된다(fail-open).
    """
    if not _cfg("SHORTS_STUDIO_ENABLED", False):
        return None
    if not is_alive():
        log.info("[shorts_studio] 서버 미가동(:8788) — 로컬 렌더러 폴백")
        return None

    title = (post_data.get("title") or "").strip()
    plain = to_plain_text(post_data.get("content") or post_data.get("content_html") or "")
    if len(plain) < 80:
        alt = to_plain_text(fallback_text)
        if len(alt) >= 80:
            log.info(f"[shorts_studio] 본문 없음 → 대본 폴백 소스 사용({len(alt)}자)")
            plain = alt
        else:
            log.warning(f"[shorts_studio] 소스 텍스트 부족(본문 {len(plain)}자/"
                        f"대본 {len(alt)}자) — 폴백")
            return None

    options: dict = {
        "duration": int(_cfg("SHORTS_STUDIO_DURATION", 30)),
        "name": (slug or "post")[:80],
        "style": str(_cfg("SHORTS_STUDIO_STYLE", "info")),
    }
    voice = str(_cfg("SHORTS_STUDIO_VOICE", "") or "").strip()
    if voice:
        options["voice"] = voice

    t0 = time.monotonic()
    try:
        r = _request(f"{base_url()}/v1/jobs", payload={
            "input": {"type": "text", "text": f"제목: {title}\n\n{plain}"},
            "options": options,
        }, timeout=15.0)
        job_id = r.get("job_id")
    except Exception as e:  # noqa: BLE001
        log.warning(f"[shorts_studio] 잡 생성 실패 — 폴백: {e}")
        return None
    if not job_id:
        log.warning(f"[shorts_studio] 잡 생성 응답에 job_id 없음 — 폴백: {r}")
        return None
    log.info(f"[shorts_studio] 잡 제출: {job_id} (style={options['style']}, "
             f"duration={options['duration']}s)")

    # 완료 폴링 — 워커는 동시 1잡이라 대기열 포함 넉넉히 기다린다.
    deadline = time.monotonic() + float(_cfg("SHORTS_STUDIO_TIMEOUT", 900))
    status = ""
    while time.monotonic() < deadline:
        try:
            job = _request(f"{base_url()}/v1/jobs/{job_id}", timeout=10.0)
        except Exception as e:  # noqa: BLE001
            log.warning(f"[shorts_studio] 상태 조회 실패 — 폴백: {e}")
            return None
        status = job.get("status") or ""
        if status in _TERMINAL:
            break
        time.sleep(5)
    if status != "done":
        log.warning(f"[shorts_studio] 잡 미완료(status={status or 'timeout'}) — 폴백")
        return None

    # 아티팩트 내려받기 — mp4 필수, 나머지는 있으면.
    try:
        listing = _request(f"{base_url()}/v1/jobs/{job_id}/artifacts", timeout=10.0)
    except Exception as e:  # noqa: BLE001
        log.warning(f"[shorts_studio] 아티팩트 목록 실패 — 폴백: {e}")
        return None
    by_name = {a.get("name"): a for a in listing.get("artifacts", [])}
    if "shorts.mp4" not in by_name:
        log.warning(f"[shorts_studio] shorts.mp4 미산출(잡 {job_id}) — 폴백")
        return None

    out_dir.mkdir(parents=True, exist_ok=True)
    saved: dict[str, Path] = {}
    for name in _ARTIFACT_WANT:
        art = by_name.get(name)
        if not art:
            continue
        # meta.json 은 로컬 _meta.json(구 렌더러 산출)과 구분해 저장
        dest = out_dir / ("_studio_meta.json" if name == "meta.json" else name)
        if _download(f"{base_url()}{art['url']}", dest):
            saved[name] = dest
    if "shorts.mp4" not in saved:
        return None

    elapsed = time.monotonic() - t0
    marker = {
        "engine": "shorts_studio",
        "job_id": job_id,
        "options": options,
        "elapsed_sec": round(elapsed, 1),
        "artifacts": sorted(p.name for p in saved.values()),
    }
    (out_dir / "_meta.json").write_text(
        json.dumps(marker, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info(f"[shorts_studio] 완료 {elapsed:.0f}s — {out_dir / 'shorts.mp4'}")
    return {"mp4": saved["shorts.mp4"], "thumbnail": saved.get("thumbnail.jpg"),
            "elapsed_sec": elapsed, "engine": "shorts_studio", "job_id": job_id}
