# core/comfy_image.py — 로컬 ComfyUI(자기 GPU) 이미지 백엔드 (tier-0, 무제한·무타임아웃·비공개)
"""프롬프트 → PNG 를 **로컬에서 도는 ComfyUI**(RTX GPU)로 생성. 클라우드 의존 0.

왜 tier-0 인가: NVIDIA/Pollinations 같은 무료 클라우드는 언제든 큐 폭주로 통째로 죽어
'이미지 0장'이 난다(2026-07 실측). 자기 GPU에서 도는 ComfyUI 는 무제한·무타임아웃·비공개.
켜져 있으면 최우선으로 쓰고, **안 켜져 있으면 즉시 클라우드로 폴백**(무해).

동작:
- ComfyUI HTTP API(기본 http://127.0.0.1:8188): POST /prompt → GET /history/{id} 폴링 →
  GET /view 로 PNG 회수. (라우트는 설치본 server.py 에서 실확인: /prompt·/history·/view)
- 워크플로우는 **사용자가 ComfyUI 에서 만든 API-format JSON** 을 진실원본으로 쓴다
  (모델별 노드 그래프가 달라 하드코딩하면 깨짐). 파일: settings.IMAGE_COMFY_WORKFLOW 또는
  templates/comfy_workflow.json. 런타임에 **프롬프트·시드·크기를 그래프에 자동 주입**하므로
  사용자는 워크플로우를 '내보내기(API)'만 하면 된다(수동 편집 불필요).
    · 프롬프트: 샘플러의 positive/negative 로 연결된 텍스트 노드를 추적해 주입
      (FLUX/Krea 처럼 positive 하나뿐이면 그 노드). `%PROMPT%`/`%NEG%` 토큰이 있으면 그걸 우선.
    · 시드: seed/noise_seed 입력을 가진 노드에 주입(0 이면 프롬프트 해시로 변주=결정론).
    · 크기: EmptyLatent 계열 노드의 width/height 주입.
- HTTP 클라이언트는 주입 가능(단위 테스트에서 실서버 없이 검증).

보안: 완전 로컬(외부 전송 0). 워크플로우 JSON 만 읽고, 자격증명과 무관.
"""
from __future__ import annotations

import io
import json
import random
import time
import zlib
from pathlib import Path
from typing import Any, Callable, Optional

from config.logger import get_logger

log = get_logger(__name__)

_DEFAULT_URL = "http://127.0.0.1:8188"
_PKG_ROOT = Path(__file__).resolve().parent.parent          # naver_blog_v3/
_DEFAULT_WORKFLOW = _PKG_ROOT / "templates" / "comfy_workflow.json"


def _cfg(name: str, default: Any) -> Any:
    try:
        from config import settings as s
        return getattr(s, name, default)
    except Exception:  # noqa: BLE001
        return default


def base_url() -> str:
    return str(_cfg("IMAGE_COMFY_URL", "") or _DEFAULT_URL).rstrip("/")


def workflow_path() -> Path:
    p = str(_cfg("IMAGE_COMFY_WORKFLOW", "") or "").strip()
    return Path(p) if p else _DEFAULT_WORKFLOW


def _workflow_text() -> Optional[str]:
    """워크플로우 JSON 텍스트. 없으면 None(→ 이 백엔드 미사용)."""
    try:
        fp = workflow_path()
        if fp.is_file():
            return fp.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    return None


# ── HTTP 클라이언트(주입 가능) ─────────────────────────────────────────
class _RequestsComfyClient:
    def __init__(self, base: str, ping_timeout: float, io_timeout: float) -> None:
        self._base = base.rstrip("/")
        self._ping_t = ping_timeout
        self._io_t = io_timeout

    def ping(self) -> bool:
        import requests
        try:
            r = requests.get(f"{self._base}/system_stats", timeout=self._ping_t)
            return r.status_code == 200
        except Exception:  # noqa: BLE001
            return False

    def submit(self, graph: dict) -> str:
        import requests
        r = requests.post(f"{self._base}/prompt", json={"prompt": graph},
                          timeout=self._io_t)
        if r.status_code != 200:
            raise RuntimeError(f"prompt_http_{r.status_code}")
        return str((r.json() or {}).get("prompt_id", ""))

    def history(self, prompt_id: str) -> dict:
        import requests
        r = requests.get(f"{self._base}/history/{prompt_id}", timeout=self._io_t)
        return r.json() if r.status_code == 200 else {}

    def view(self, filename: str, subfolder: str, ftype: str) -> bytes:
        import requests
        r = requests.get(f"{self._base}/view",
                         params={"filename": filename, "subfolder": subfolder, "type": ftype},
                         timeout=self._io_t)
        return r.content if r.status_code == 200 else b""


def _default_client() -> _RequestsComfyClient:
    return _RequestsComfyClient(
        base_url(),
        ping_timeout=float(_cfg("IMAGE_COMFY_PING_TIMEOUT", 2)),
        io_timeout=float(_cfg("IMAGE_COMFY_TIMEOUT", 180)),
    )


def available(client: Optional[Any] = None) -> bool:
    """로컬 ComfyUI 로 생성 가능한가 — 플래그 ON + 워크플로우 존재 + 서버 도달."""
    if not bool(_cfg("IMAGE_COMFY_ENABLED", True)):
        return False
    if _workflow_text() is None:
        return False
    client = client or _default_client()
    try:
        return bool(client.ping())
    except Exception:  # noqa: BLE001
        return False


# ── 워크플로우 그래프에 프롬프트·시드·크기 자동 주입 ───────────────────
def _fill_workflow(prompt: str, negative: str, width: int, height: int,
                   seed: int) -> Optional[dict]:
    text = _workflow_text()
    if text is None:
        return None
    if not seed:  # 0 이면 프롬프트 해시로 변주(같은 프롬프트+seed0 = 동일 이미지 방지)
        seed = zlib.crc32(prompt.encode("utf-8", "ignore")) & 0x7FFFFFFF
    tokened = "%PROMPT%" in text
    if tokened:
        text = (text.replace("%PROMPT%", _json_inner(prompt))
                    .replace("%NEG%", _json_inner(negative)))
    try:
        graph = json.loads(text)
    except Exception as exc:  # noqa: BLE001
        log.warning(f"[comfy_image] 워크플로우 JSON 파싱 실패: {exc}")
        return None
    if not isinstance(graph, dict):
        return None
    _autofill(graph, prompt, negative, width, height, seed, inject_prompt=not tokened)
    return graph


def _json_inner(s: str) -> str:
    """문자열을 JSON 문자열 리터럴 '내부'로 안전 삽입(따옴표·개행 이스케이프)."""
    return json.dumps(str(s), ensure_ascii=False)[1:-1]


_SAMPLER_HINTS = ("KSampler", "SamplerCustom")
_LATENT_HINTS = ("EmptyLatent", "EmptySD3", "SD3LatentImage")


def _autofill(graph: dict, prompt: str, negative: str, width: int, height: int,
              seed: int, *, inject_prompt: bool) -> None:
    for node in graph.values():
        if not isinstance(node, dict):
            continue
        ins = node.get("inputs")
        if not isinstance(ins, dict):
            continue
        ct = str(node.get("class_type", ""))
        for k in ("seed", "noise_seed"):
            if k in ins and isinstance(ins[k], (int, float)):
                ins[k] = seed
        if any(h in ct for h in _LATENT_HINTS):
            if "width" in ins:
                ins["width"] = width
            if "height" in ins:
                ins["height"] = height
    if inject_prompt:
        _inject_prompt_text(graph, prompt, negative)


def _inject_prompt_text(graph: dict, prompt: str, negative: str) -> None:
    """샘플러의 positive/negative 참조를 따라 텍스트 노드를 찾아 프롬프트를 넣는다.
    실패 시(추적 불가) 텍스트 노드가 하나뿐이면 그걸 positive 로 간주."""
    text_nodes = {
        nid: n for nid, n in graph.items()
        if isinstance(n, dict) and isinstance(n.get("inputs"), dict)
        and isinstance(n["inputs"].get("text"), str)
    }
    if not text_nodes:
        return
    pos_id = neg_id = None
    for n in graph.values():
        if not isinstance(n, dict):
            continue
        ins = n.get("inputs", {})
        if not isinstance(ins, dict):
            continue
        for key, is_pos in (("positive", True), ("negative", False)):
            ref = ins.get(key)
            if isinstance(ref, list) and ref and str(ref[0]) in text_nodes:
                if is_pos:
                    pos_id = str(ref[0])
                else:
                    neg_id = str(ref[0])
    if pos_id is None and len(text_nodes) == 1:
        pos_id = next(iter(text_nodes))          # FLUX/Krea: positive 하나뿐
    if pos_id and pos_id in graph:
        graph[pos_id]["inputs"]["text"] = prompt
    if neg_id and neg_id in graph:
        graph[neg_id]["inputs"]["text"] = negative


def _first_image(outputs: dict) -> Optional[dict]:
    if not isinstance(outputs, dict):
        return None
    for node_out in outputs.values():
        if isinstance(node_out, dict):
            imgs = node_out.get("images")
            if isinstance(imgs, list) and imgs and isinstance(imgs[0], dict):
                return imgs[0]
    return None


def _save_png(raw: bytes, out_path: Path) -> bool:
    if not raw or len(raw) < 8:
        return False
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(raw))
        img.load()
        img.convert("RGB").save(out_path, format="PNG")
        return True
    except Exception:  # noqa: BLE001
        if raw[:8] == b"\x89PNG\r\n\x1a\n":
            out_path.write_bytes(raw)
            return True
        return False


def try_generate(
    prompt: str,
    out_path: Path,
    *,
    width: int,
    height: int,
    seed: int = 0,
    negative: str = "",
    client: Optional[Any] = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> "tuple[bool, str]":
    """로컬 ComfyUI 로 생성 시도. 성공 시 (True,'ok'), 실패 시 (False,사유). 예외 없음."""
    # 로컬 전용 해상도 오버라이드(기본 768) — 클라우드 폴백(1024)과 독립. 13GB fp8 모델이
    # 12GB VRAM 을 넘어 부분 오프로드되는 상황에서 768 이 1024 대비 ~30% 빠름(실측).
    # 삽화 배경 + 한글 오버레이(표시 ~800px)라 768 로 화질 충분. 0 이면 호출 크기 유지.
    width = int(_cfg("IMAGE_COMFY_WIDTH", 768) or width)
    height = int(_cfg("IMAGE_COMFY_HEIGHT", 768) or height)
    # ⚠️ 시드는 호출마다 랜덤으로 고유화한다. 같은 슬롯(예: thumb)을 재생성하면 프롬프트도
    # 출력경로도 같아 시드까지 같아지는데, ComfyUI 는 **서버에 동일 그래프 결과를 캐시**해
    # 다음 런에서 0.00초로 즉시 반환한다. 캐시된 프롬프트는 /history 에 출력이 안 채워져
    # 폴링이 180초 타임아웃 → 폴백되던 실측 버그. 랜덤 시드면 캐시가 원천적으로 안 걸리고,
    # 블로그 삽화는 매번 다른 그림이 오히려 이득(재현성 불필요).
    if not seed:
        seed = random.randint(1, 0x7FFFFFFF)
    graph = _fill_workflow(prompt, negative, width, height, seed)
    if graph is None:
        return False, "no_workflow"
    client = client or _default_client()
    try:
        prompt_id = client.submit(graph)
    except Exception as exc:  # noqa: BLE001
        return False, f"submit_exc:{type(exc).__name__}"
    if not prompt_id:
        return False, "no_prompt_id"

    gen_timeout = float(_cfg("IMAGE_COMFY_TIMEOUT", 180))
    interval = float(_cfg("IMAGE_COMFY_POLL_INTERVAL", 1.0))
    waited = 0.0
    while waited < gen_timeout:
        try:
            hist = client.history(prompt_id)
        except Exception:  # noqa: BLE001
            hist = {}
        entry = hist.get(prompt_id) if isinstance(hist, dict) else None
        if isinstance(entry, dict):
            img = _first_image(entry.get("outputs") or {})
            if img:
                try:
                    raw = client.view(img.get("filename", ""), img.get("subfolder", ""),
                                      img.get("type", "output"))
                except Exception as exc:  # noqa: BLE001
                    return False, f"view_exc:{type(exc).__name__}"
                return (True, "ok") if _save_png(raw, out_path) else (False, "bad_image")
            # 이미 '완료'인데 이미지가 없으면(캐시/노-세이브) 즉시 폴백 — 180초 폴링 낭비 방지.
            status = entry.get("status") or {}
            if status.get("completed") or status.get("status_str") == "success":
                return False, "completed_no_image"
        sleep_fn(interval)
        waited += interval
    return False, "timeout"
