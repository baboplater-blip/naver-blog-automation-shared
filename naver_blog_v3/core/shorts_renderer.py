# core/shorts_renderer.py
# examplebrand 멀티채널 Shorts 렌더링 — TTS + STT + Remotion + 썸네일.
#
# 흐름:
#   1) edge-tts 로 TTS mp3 생성 (ko-KR-SunHiNeural)
#   2) faster-whisper large-v3 (CUDA) 로 word-level timestamp 추출
#   3) inputProps.json 작성 → Remotion CLI 로 렌더 → out.mp4
#   4) FFmpeg 로 썸네일 추출 (첫 프레임)
#   5) _meta.json 기록
#
# examplebrand 일 발행 회귀 위험 0 — 이 모듈은 _lib/build_multichannel_pack.py
# 또는 GUI 신규 버튼만 호출하며, auto_scheduler 와 무관.
from __future__ import annotations

# ⚠️ Windows OpenMP 중복 라이브러리 충돌 회피 — faster-whisper 모듈 import 직전에 설정.
# 원인: faster-whisper(ctranslate2) + PIL(numpy) 가 각자 다른 OMP DLL 로드.
# 다른 환경 변수 의존성보다 먼저 set 되어야 함.
import os
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")

import asyncio
import datetime as dt
import difflib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.logger import get_logger

log = get_logger(__name__)

# ─────────────────────────────────────────────────────
# 경로
# ─────────────────────────────────────────────────────
APP_DIR = Path(__file__).parent.parent          # naver_blog_v3/
HARNESS_ROOT = APP_DIR.parent                   # naver_blog_automation_v3/
MULTICHANNEL_DIR = HARNESS_ROOT / "multichannel"
DEFAULT_BGM_DIR = MULTICHANNEL_DIR / "assets" / "bgm"
WHISPER_CACHE = Path.home() / ".cache" / "faster-whisper"

# ─────────────────────────────────────────────────────
# 상수
# ─────────────────────────────────────────────────────
TTS_VOICE_DEFAULT = "ko-KR-SunHiNeural"
TTS_RATE_DEFAULT = "+0%"
TTS_PITCH_DEFAULT = "+0Hz"
TARGET_DURATION_SEC = 30.0

# ── MeloTTS (로컬 뉴럴, 격리 venv) ──
# edge-tts 보다 자연스러운 한국어 음성. 격리 venv 를 subprocess 로 호출하고,
# 없거나 실패하면 edge-tts 로 자동 폴백(무중단). 숫자 고유어 정규화는 synth.py 내부.
_MELO_DIR = APP_DIR / "_tts_melo"
MELO_VENV_PY = _MELO_DIR / "venv" / "Scripts" / "python.exe"
MELO_SYNTH = _MELO_DIR / "synth.py"
MELO_TTS_SPEED = 1.0          # 사용자 채택 속도(1.0=기본)
USE_MELO_TTS = True           # 로컬 뉴럴 우선. False 면 edge-tts 만.
MELO_TTS_TIMEOUT_SEC = 300    # 첫 실행 모델 로딩 여유(이후 ~15~20s)
# TTS 는 GPU 불필요(VITS 는 CPU 로도 빠름). CPU 강제 + GPU 숨김으로
# 런타임 파이썬(STT용 CUDA)과의 중첩 subprocess DLL 충돌(rc=0xC0000409) 회피.
MELO_TTS_DEVICE = "cpu"
RENDER_TIMEOUT_SEC = 900    # 15분 — Remotion 렌더 최악 케이스
TTS_TIMEOUT_SEC = 60
STT_TIMEOUT_SEC = 180
THUMBNAIL_TIMEOUT_SEC = 30


class RenderError(RuntimeError):
    """shorts_renderer 단계별 실패. exit code 매핑은 호출 측에서."""


# ─────────────────────────────────────────────────────
# TTS
# ─────────────────────────────────────────────────────
async def _tts_async(text: str, out_path: Path,
                     voice: str, rate: str, pitch: str) -> None:
    """edge-tts 음성 합성. 타임아웃은 호출 측 asyncio.run 에서."""
    import edge_tts  # 지연 import — caption_generator 만 쓸 때 부담 줄임

    communicate = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch)
    await communicate.save(str(out_path))


def _run_tts(text: str, out_path: Path,
             voice: str = TTS_VOICE_DEFAULT,
             rate: str = TTS_RATE_DEFAULT,
             pitch: str = TTS_PITCH_DEFAULT) -> float:
    """동기 wrapper. 소요 시간(초) 반환."""
    if not text or not text.strip():
        raise RenderError("TTS 입력 텍스트가 비어 있음")

    t0 = time.time()
    try:
        # 60초 타임아웃 — edge-tts 는 보통 1~3초
        asyncio.run(asyncio.wait_for(
            _tts_async(text, out_path, voice, rate, pitch),
            timeout=TTS_TIMEOUT_SEC,
        ))
    except asyncio.TimeoutError as e:
        raise RenderError(f"TTS 타임아웃 ({TTS_TIMEOUT_SEC}s)") from e
    except (OSError, RuntimeError) as e:
        raise RenderError(f"TTS 실패: {e}") from e

    if not out_path.exists() or out_path.stat().st_size < 1024:
        raise RenderError(f"TTS 출력 파일이 비정상: {out_path}")

    elapsed = time.time() - t0
    log.info(f"TTS 완료: {out_path.name} ({out_path.stat().st_size / 1024:.1f} KB, {elapsed:.1f}s)")
    return elapsed


def _run_tts_melo(text: str, out_path: Path,
                  speed: float = MELO_TTS_SPEED) -> float:
    """MeloTTS(로컬 뉴럴) — 격리 venv 를 subprocess 로 호출해 mp3 생성. 소요 시간(초).

    실패하면 RenderError 를 던져 호출 측이 edge-tts 로 폴백하게 한다.
    한글 텍스트는 파일로 전달(Windows CLI 인코딩 깨짐 회피).
    """
    if not (MELO_VENV_PY.exists() and MELO_SYNTH.exists()):
        raise RenderError("MeloTTS venv/스크립트 없음")
    if not text or not text.strip():
        raise RenderError("MeloTTS 입력 텍스트가 비어 있음")

    t0 = time.time()
    tf = tempfile.NamedTemporaryFile("w", suffix=".txt", encoding="utf-8",
                                     delete=False)
    try:
        tf.write(text)
        tf.close()
        # 중첩 subprocess CUDA/DLL 충돌 방지 — GPU 숨김(CUDA_VISIBLE_DEVICES="") + CPU.
        # PYTHONIOENCODING=utf-8: Windows 기본 cp949 stdout → JSON 한글(spoken) 깨짐 방지.
        env = dict(os.environ,
                   HF_HUB_DISABLE_SYMLINKS_WARNING="1",
                   CUDA_VISIBLE_DEVICES="",
                   KMP_DUPLICATE_LIB_OK="TRUE",
                   PYTHONIOENCODING="utf-8")
        proc = subprocess.run(
            [str(MELO_VENV_PY), str(MELO_SYNTH),
             "--text-file", tf.name, "--out", str(out_path),
             "--speed", str(speed), "--device", MELO_TTS_DEVICE],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=MELO_TTS_TIMEOUT_SEC, env=env,
        )
        ok = False
        spoken = ""
        for line in reversed(proc.stdout.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    obj = json.loads(line)
                    ok = bool(obj.get("ok"))
                    spoken = obj.get("spoken") or ""
                except (ValueError, TypeError):
                    ok = False
                break
        if not ok or not out_path.exists() or out_path.stat().st_size < 1024:
            tail = (proc.stdout or proc.stderr or "")[-300:]
            raise RenderError(f"MeloTTS 실패(rc={proc.returncode}): {tail}")
    except subprocess.TimeoutExpired as e:
        raise RenderError(f"MeloTTS 타임아웃 ({MELO_TTS_TIMEOUT_SEC}s)") from e
    finally:
        try:
            os.unlink(tf.name)
        except OSError:
            pass

    elapsed = time.time() - t0
    log.info(f"TTS(MeloTTS) 완료: {out_path.name} "
             f"({out_path.stat().st_size / 1024:.1f} KB, {elapsed:.1f}s)")
    return elapsed, spoken


def _synthesize_tts(text: str, out_path: Path, *,
                    voice: str = TTS_VOICE_DEFAULT,
                    rate: str = TTS_RATE_DEFAULT,
                    pitch: str = TTS_PITCH_DEFAULT,
                    speed: float = MELO_TTS_SPEED) -> tuple[float, str, str]:
    """TTS 합성 — MeloTTS(로컬 뉴럴) 우선, 실패 시 edge-tts 폴백.

    Returns (소요초, 엔진명, 실제 발화 텍스트). 발화 텍스트는 STT initial_prompt 용
    (melo=정규화된 텍스트, edge=원문). 프롬프트를 발화와 일치시켜 오인식을 줄인다.
    """
    if USE_MELO_TTS and MELO_VENV_PY.exists():
        try:
            el, spoken = _run_tts_melo(text, out_path, speed=speed)
            return el, "melo", (spoken or text)
        except (RenderError, OSError, subprocess.SubprocessError) as e:
            log.warning(f"MeloTTS 실패 — edge-tts 폴백: {e}")
    el = _run_tts(text, out_path, voice=voice, rate=rate, pitch=pitch)
    return el, "edge", text


# ─────────────────────────────────────────────────────
# STT (word-level timestamps)
# ─────────────────────────────────────────────────────
# 도메인 용어 사전 — whisper initial_prompt 로 주입해 고유명사 철자를 바로잡는다
# (예: '청년도약계좌'를 '청년도약계자'로 오인식하던 문제). 발화 텍스트도 힌트로 덧댄다.
_STT_GLOSSARY = (
    "청년도약계좌, 청년희망적금, 국민행복카드, 첫만남이용권, 부모급여, 아동수당, "
    "주거급여, 근로장려금, 자녀장려금, 국민취업지원제도, 구직촉진수당, 실업급여, "
    "기초연금, 기초생활수급, 긴급복지, 에너지바우처, 문화누리카드, 평생교육바우처, "
    "국가장학금, 전세보증금, 버팀목대출, 디딤돌대출, 청년월세 특별지원, "
    "지원금, 보조금, 바우처, 복지로, 정부24, 홈택스, 납입, 만기, 연장, 재개, "
    "신청, 자격, 소득, 가구, 세대주, 무주택, 전역, 예시브랜드."
)

# STT 후보정 — 자주 나오는 오인식을 '정확 일치 단어'로만 치환(부분일치 오탐 방지).
_STT_CORRECTIONS = {
    "계자": "계좌", "계자는": "계좌는", "계자를": "계좌를", "계자가": "계좌가",
    "계자에": "계좌에", "계자로": "계좌로", "계자와": "계좌와", "계자도": "계좌도",
    "도약계자": "도약계좌",
}


def _build_stt_prompt(hint: str = "") -> str:
    """initial_prompt = 도메인 사전 + 발화 텍스트(있으면). 길이는 넉넉히 캡."""
    text = _STT_GLOSSARY + (" " + hint.strip() if hint else "")
    return text[:800]


def _clean_stt_words(words: list[dict]) -> list[dict]:
    """STT 단어 정제 — 깨진 문자(U+FFFD) 제거.

    faster-whisper 가 word timestamp 를 만들 때 한글 한 글자의 UTF-8 바이트를
    단어 경계에서 쪼개 U+FFFD(�)가 섞이는 현상('군 입대�를')을 제거한다.
    빈 문자열이 된 단어는 드롭(타임스탬프만 남은 껍데기 방지).
    """
    out: list[dict] = []
    for w in words:
        t = (w.get("word") or "").replace("�", "")
        # 제어문자·비출력 문자 제거(공백은 유지)
        t = "".join(ch for ch in t if ch.isprintable() or ch == " ").strip()
        if t:
            w["word"] = t
            out.append(w)
    return out


_WORD_CORE_RE = re.compile(r"[가-힣A-Za-z0-9]+")


def _align_captions_to_spoken(words: list[dict], spoken: str) -> list[dict]:
    """캡션을 '정답 발화 텍스트'로 재구성하고 whisper 타임스탬프에 정렬.

    핵심: whisper 의 철자를 신뢰하지 않는다. 우리는 무슨 말을 하는지 정확히 안다
    (spoken=정규화된 발화 텍스트). whisper 는 '타이밍'만 쓰고, 캡션 '글자'는 정답에서
    가져온다 → 철자(계좌↔계절)·단어경계(청년도/약계좌)·숫자(1,000,000) 오류가 통째로 사라진다.

    방법: whisper 단어의 문자별 시각(단어 내 선형보간)을 만들고, 정답 문자열과
    문자 단위로 정렬(difflib)해 정답 어절마다 시각 구간을 매긴다. 시각은 단조 증가 보정.
    정렬 불가(정답/음성 비었음)면 원본 words 반환(안전).
    """
    sp_words = (spoken or "").split()
    if not sp_words or not words:
        return words

    # ① whisper 문자 스트림 + 문자별 시각(단어 내 균등 보간)
    w_core_chars: list[str] = []
    w_char_time: list[float] = []
    for w in words:
        core = "".join(_WORD_CORE_RE.findall(w.get("word", "")))
        if not core:
            continue
        a = float(w.get("start", 0.0))
        b = float(w.get("end", a))
        n = len(core)
        for i, c in enumerate(core):
            w_core_chars.append(c)
            w_char_time.append(a + (b - a) * (i / n) if n else a)
    if not w_core_chars:
        return words
    w_core = "".join(w_core_chars)

    # ② 정답 어절 → core(구두점 제거) + 전체 정답 core 문자열
    sp_cores = ["".join(_WORD_CORE_RE.findall(x)) for x in sp_words]
    sp_core = "".join(sp_cores)
    if not sp_core:
        return words

    # ③ 문자 정렬: 정답 core 위치 → whisper core 위치
    sm = difflib.SequenceMatcher(None, sp_core, w_core, autojunk=False)
    sp2w: list[Optional[int]] = [None] * len(sp_core)
    for a, b, size in sm.get_matching_blocks():
        for k in range(size):
            sp2w[a + k] = b + k
    # None(불일치)은 직전 유효 인덱스로 채움
    last = 0
    for i in range(len(sp2w)):
        if sp2w[i] is None:
            sp2w[i] = last
        else:
            last = sp2w[i]

    # ④ 각 정답 어절에 시각 구간 부여(단조 증가 보정)
    out: list[dict] = []
    pos = 0
    prev_end = 0.0
    for word, core in zip(sp_words, sp_cores):
        if not core:
            continue
        js = [sp2w[pos + k] for k in range(len(core))]
        ts = [w_char_time[j] for j in js if 0 <= j < len(w_char_time)]
        start = min(ts) if ts else prev_end
        end = max(ts) if ts else prev_end
        start = max(start, prev_end)             # 겹침 방지(단조)
        end = max(end, start + 0.08)
        out.append({"word": word, "start": round(start, 3),
                    "end": round(end, 3)})
        prev_end = end
        pos += len(core)
    return out or words


def _apply_stt_corrections(words: list[dict]) -> list[dict]:
    """단어 단위 정확 일치 후보정(타임스탬프 보존) — 스냅이 못 잡는 잔여 케이스 안전망."""
    for w in words:
        t = (w.get("word") or "").strip()
        if t in _STT_CORRECTIONS:
            w["word"] = _STT_CORRECTIONS[t]
    return words


def _register_cuda_dll_dirs() -> None:
    """nvidia pip 패키지(cublas/cudnn)의 bin 을 Windows DLL 검색 경로에 등록.

    ctranslate2 4.x 는 추론 시 cublas64_12.dll·cudnn 을 LoadLibrary 로 찾는데,
    pip 로 설치한 nvidia-cublas-cu12/nvidia-cudnn-cu12 는 site-packages/nvidia/*/bin
    에 있어 기본 검색경로에 없다. 여기서 add_dll_directory 로 등록해 CUDA STT 를 살린다.
    """
    try:
        import importlib.util
        for pkg in ("nvidia.cublas", "nvidia.cudnn"):
            spec = importlib.util.find_spec(pkg)
            if not (spec and spec.submodule_search_locations):
                continue
            bin_dir = Path(list(spec.submodule_search_locations)[0]) / "bin"
            if not bin_dir.exists():
                continue
            # ① add_dll_directory (파이썬 확장 로딩용)
            if hasattr(os, "add_dll_directory"):
                os.add_dll_directory(str(bin_dir))
            # ② PATH prepend — ctranslate2 의 의존 DLL(cublas/cudnn) 로더가 확실히 찾도록.
            #    add_dll_directory 는 서드파티 LoadLibrary 에 안 잡히는 경우가 있어 PATH 병행.
            p = str(bin_dir)
            if p not in os.environ.get("PATH", ""):
                os.environ["PATH"] = p + os.pathsep + os.environ.get("PATH", "")
    except (ImportError, OSError, ValueError):
        pass   # 등록 실패해도 CPU 폴백이 있으므로 치명적 아님


def _run_stt(mp3_path: Path, *, prompt: str = "") -> tuple[list[dict], float, float]:
    """faster-whisper large-v3 GPU 추론.

    prompt: 발화 텍스트(정답) — 도메인 사전과 합쳐 initial_prompt 로 주입해
            고유명사 오인식을 줄인다. 잔여 오류는 _apply_stt_corrections 로 후보정.

    Returns:
        (words, audio_duration_sec, elapsed_sec)
        words = [{"word": str, "start": float, "end": float}, ...]
    """
    if not mp3_path.exists():
        raise RenderError(f"STT 입력 mp3 없음: {mp3_path}")

    initial_prompt = _build_stt_prompt(prompt)

    # 지연 import — TTS 만 쓰는 케이스에서 부하 회피
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        raise RenderError(
            "faster-whisper 미설치 — pip install faster-whisper"
        ) from e

    _register_cuda_dll_dirs()   # nvidia pip 패키지 cuBLAS/cuDNN DLL 경로 등록
    t0 = time.time()

    def _load_and_transcribe(device: str, compute_type: str):
        model = WhisperModel(
            "large-v3", device=device, compute_type=compute_type,
            download_root=str(WHISPER_CACHE),
        )
        segments, info = model.transcribe(
            str(mp3_path), language="ko", word_timestamps=True,
            vad_filter=True, beam_size=5, initial_prompt=initial_prompt,
        )
        out: list[dict] = []
        for seg in segments:           # 실제 추론은 여기서 발생 (cublas 에러도 여기)
            for w in (seg.words or []):
                wt = (w.word or "").strip()
                if wt:
                    out.append({"word": wt, "start": float(w.start),
                                "end": float(w.end)})
        return out, float(info.duration)

    # CUDA 우선 — 로드/추론 어디서 실패하든(cublas·cudnn 누락 등) CPU 로 폴백.
    for device, compute_type in (("cuda", "float16"), ("cpu", "int8")):
        try:
            words, audio_dur = _load_and_transcribe(device, compute_type)
            words = _clean_stt_words(words)         # 깨진 문자(U+FFFD) 제거
            # 캡션 글자=정답 발화 텍스트로 재구성(철자·경계·숫자 오류 근본 제거),
            # whisper 는 타이밍만. 정답 없으면(edge 등) 후보정 맵으로 폴백.
            if prompt.strip():
                words = _align_captions_to_spoken(words, prompt)
            words = _apply_stt_corrections(words)   # 잔여 케이스 안전망
            elapsed = time.time() - t0
            log.info(f"STT 완료({device}): {len(words)}개 단어, "
                     f"audio {audio_dur:.1f}s, 전체 {elapsed:.1f}s")
            return words, audio_dur, elapsed
        except (RuntimeError, OSError) as e:
            if device == "cuda":
                log.warning(f"STT CUDA 실패 — CPU 폴백: {e}")
                continue
            raise RenderError(f"STT 실패 (CUDA+CPU 모두): {e}") from e
    raise RenderError("STT 실패: 사용 가능한 디바이스 없음")


# ─────────────────────────────────────────────────────
# BGM 선택
# ─────────────────────────────────────────────────────
def _pick_bgm(bgm_dir: Path) -> Optional[Path]:
    """assets/bgm/*.mp3 중 첫 트랙 (사전순)."""
    if not bgm_dir.exists():
        log.warning(f"BGM 디렉토리 없음 — BGM 없이 진행: {bgm_dir}")
        return None
    candidates = sorted(bgm_dir.glob("*.mp3"))
    if not candidates:
        log.warning(f"BGM 파일 없음 ({bgm_dir}/*.mp3) — BGM 없이 진행")
        return None
    chosen = candidates[0]
    log.info(f"BGM 선택: {chosen.name}")
    return chosen


# ─────────────────────────────────────────────────────
# Remotion 렌더
# ─────────────────────────────────────────────────────
def _run_remotion(input_props_path: Path, out_mp4: Path,
                  *, multichannel_dir: Path = MULTICHANNEL_DIR) -> float:
    """npx remotion render — out.mp4 생성. 소요 시간(초) 반환."""
    if not multichannel_dir.exists():
        raise RenderError(f"multichannel 디렉토리 없음: {multichannel_dir}")
    entry = multichannel_dir / "src" / "index.ts"
    if not entry.exists():
        raise RenderError(f"Remotion entry 없음: {entry}")

    # npx 명령 탐색 — Windows 의 npx.cmd 도 처리
    npx_cmd = shutil.which("npx") or shutil.which("npx.cmd")
    if not npx_cmd:
        raise RenderError("npx 명령을 찾을 수 없음 — Node.js 설치 확인")

    args = [
        npx_cmd, "remotion", "render",
        "src/index.ts", "Shorts30s",
        str(out_mp4.resolve()),
        f"--props={input_props_path.resolve()}",
        "--gl=angle",
        "--concurrency=8",
        "--codec=h264",
        "--log=info",
    ]
    log.info(f"Remotion 렌더 시작: {' '.join(args[:6])} ...")

    t0 = time.time()
    # Windows 에서 .cmd 호출 시 shell=False 로도 동작. PATH 의 npx 가 .cmd 면
    # subprocess 가 자동으로 처리 — shutil.which 가 .cmd 를 반환하기 때문.
    use_shell = npx_cmd.lower().endswith(".cmd")

    try:
        proc = subprocess.run(
            args if not use_shell else " ".join(f'"{a}"' for a in args),
            cwd=str(multichannel_dir),
            timeout=RENDER_TIMEOUT_SEC,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=use_shell,
        )
    except subprocess.TimeoutExpired as e:
        raise RenderError(
            f"Remotion 렌더 타임아웃 ({RENDER_TIMEOUT_SEC}s)"
        ) from e
    except (OSError, FileNotFoundError) as e:
        raise RenderError(f"Remotion 실행 실패: {e}") from e

    elapsed = time.time() - t0

    # stdout/stderr 마지막 줄들 로그 (디버깅용)
    if proc.stdout:
        tail = "\n".join(proc.stdout.strip().splitlines()[-5:])
        log.debug(f"Remotion stdout(tail):\n{tail}")
    if proc.stderr:
        tail = "\n".join(proc.stderr.strip().splitlines()[-10:])
        if proc.returncode != 0:
            log.error(f"Remotion stderr(tail):\n{tail}")
        else:
            log.debug(f"Remotion stderr(tail):\n{tail}")

    if proc.returncode != 0:
        raise RenderError(
            f"Remotion 렌더 실패 (exit {proc.returncode}). "
            f"stderr 마지막 줄: {(proc.stderr or '').strip().splitlines()[-1:] }"
        )

    if not out_mp4.exists() or out_mp4.stat().st_size < 10_000:
        raise RenderError(f"Remotion 출력 mp4 비정상: {out_mp4}")

    log.info(
        f"Remotion 렌더 완료: {out_mp4.name} "
        f"({out_mp4.stat().st_size / 1024 / 1024:.1f} MB, {elapsed:.1f}s)"
    )
    return elapsed


# ─────────────────────────────────────────────────────
# 썸네일 추출
# ─────────────────────────────────────────────────────
def _extract_thumbnail(mp4_path: Path, out_jpg: Path,
                       seek: str = "00:00:12") -> None:
    """FFmpeg 로 특정 시점 프레임 jpg 추출.

    기본 seek=12초 — 콘텐츠 카드가 완전히 뜬 지점(t=0 은 훅 페이드인 전이라 검음).
    """
    ffmpeg = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
    if not ffmpeg:
        log.warning("ffmpeg 명령을 찾을 수 없음 — 썸네일 스킵")
        return

    args = [ffmpeg, "-y", "-ss", seek, "-i", str(mp4_path),
            "-frames:v", "1", "-q:v", "2", str(out_jpg)]
    try:
        proc = subprocess.run(
            args, timeout=THUMBNAIL_TIMEOUT_SEC,
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        log.warning(f"썸네일 ffmpeg 타임아웃 ({THUMBNAIL_TIMEOUT_SEC}s) — 스킵")
        return
    except OSError as e:
        log.warning(f"썸네일 ffmpeg 실패 — 스킵: {e}")
        return

    if proc.returncode != 0:
        log.warning(
            f"썸네일 ffmpeg 비정상 종료 (exit {proc.returncode}) — 스킵: "
            f"{(proc.stderr or '').strip().splitlines()[-1:]}"
        )
        return

    if not out_jpg.exists():
        log.warning(f"썸네일 출력 누락 — 스킵: {out_jpg}")
        return

    log.info(f"썸네일 추출 완료: {out_jpg.name} "
             f"({out_jpg.stat().st_size / 1024:.1f} KB)")


# ─────────────────────────────────────────────────────
# inputProps.json 빌드
# ─────────────────────────────────────────────────────
def _stage_public_asset(src: Path, multichannel_dir: Path, name: str) -> str:
    """에셋을 multichannel/public/render_tmp/<name> 로 복사하고 staticFile 상대경로 반환.

    Remotion 은 file:// 의 percent-encoded 한글 경로(하네스 폴더)를 못 읽으므로,
    public/ 밑 ASCII 경로로 옮겨 staticFile 로 http 서빙되게 한다.
    """
    pub = multichannel_dir / "public" / "render_tmp"
    pub.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, pub / name)
    return f"render_tmp/{name}"


def _resolve_image_path(value) -> Optional[Path]:
    """카드 screenshot 값(로컬 경로 / file:// URI)을 실제 파일 Path 로 해석.

    - "render_tmp/..." 처럼 이미 스테이징된 값은 그대로 통과(None 반환 → 호출측이 유지).
    - 존재하는 파일이면 Path, 아니면 None.
    """
    if not value or not isinstance(value, str):
        return None
    if value.startswith("render_tmp/"):
        return None  # 이미 스테이징됨 — 호출측에서 원문 유지
    if value.startswith("file://"):
        try:
            from urllib.parse import urlparse, unquote
            p = Path(unquote(urlparse(value).path.lstrip("/")))
        except (ValueError, OSError):
            return None
    else:
        p = Path(value)
    return p if p.exists() and p.is_file() else None


def _stage_card_image(value, multichannel_dir: Path, idx: int) -> Optional[str]:
    """카드 이미지 screenshot 값을 렌더 가능한 staticFile 상대경로로 변환.

    한글 경로 file:// 버그를 회피하기 위해 public/render_tmp 로 복사한다.
    - 이미 render_tmp/ 면 그대로 반환.
    - 로컬 파일(또는 file://)이면 스테이징 후 상대경로 반환.
    - 그 외/누락이면 None(텍스트 카드로 폴백).
    """
    if isinstance(value, str) and value.startswith("render_tmp/"):
        return value
    src = _resolve_image_path(value)
    if src is None:
        return None
    try:
        return _stage_public_asset(src, multichannel_dir, f"card_{idx}.png")
    except OSError as e:
        log.warning(f"카드 이미지 스테이징 실패(idx={idx}) — 텍스트 카드 폴백: {e}")
        return None


def _stage_named_image(value, multichannel_dir: Path, name: str) -> Optional[str]:
    """임의 이름으로 이미지를 render_tmp 로 스테이징(진행자 플레이트 등). 실패 시 None."""
    if isinstance(value, str) and value.startswith("render_tmp/"):
        return value
    src = _resolve_image_path(value)
    if src is None:
        return None
    try:
        return _stage_public_asset(src, multichannel_dir, name)
    except OSError as e:
        log.warning(f"이미지 스테이징 실패({name}) — 폴백: {e}")
        return None


def _build_input_props(script: dict, audio_path: Path,
                       words: list[dict],
                       bgm_path: Optional[Path],
                       *, category: str = "",
                       duration_sec: float = 0.0,
                       multichannel_dir: Path = MULTICHANNEL_DIR) -> dict:
    """Remotion 에 넘길 props (inputProps.json 의 내용)."""
    # 자막 하이라이트가 썸네일 강조어와 같은 프리미엄 accent 를 쓰도록 색을 넘긴다.
    try:
        from core.video_visual import thumb_accent_hex
        accent = thumb_accent_hex(category or "default")
    except Exception:  # noqa: BLE001 - 색 계산 실패해도 렌더 무중단
        accent = "#FBBF24"
    # 브랜드(프로필별) — 화면 로고·CTA 채널명. default 는 내장 examplebrand.
    try:
        from config import brand as _brand
        brand_display, brand_channel = _brand.display(), _brand.channel()
    except Exception:  # noqa: BLE001
        brand_display, brand_channel = "examplebrand", "examplebrand"
    h2_cards = []
    raw_cards = script.get("h2_cards", [])
    for i, card in enumerate(raw_cards):
        if isinstance(card, dict):
            # 카드 이미지(screenshot)를 render_tmp 로 스테이징 — 한글경로 버그 회피
            shot = _stage_card_image(card.get("screenshot"), multichannel_dir, i + 1)
            h2_cards.append({
                "text": card.get("text", ""),
                "stat": card.get("stat") or "",
                "caption": card.get("caption") or "",
                "icon": card.get("icon") or "",
                "screenshot": shot,
            })
        elif isinstance(card, str):
            h2_cards.append({"text": card, "stat": "", "caption": "",
                             "icon": "", "screenshot": None})

    # 정확히 3개 보장
    while len(h2_cards) < 3:
        h2_cards.append({"text": "핵심 절차", "stat": "", "caption": "",
                         "icon": "", "screenshot": None})
    h2_cards = h2_cards[:3]

    # 한글 경로 file:// 회피 — public/ 로 스테이징 후 staticFile 상대경로 사용.
    audio_url = (_stage_public_asset(audio_path, multichannel_dir, "tts.mp3")
                 if audio_path.exists() else "")
    bgm_url = (_stage_public_asset(bgm_path, multichannel_dir, "bgm.mp3")
               if (bgm_path and bgm_path.exists()) else None)
    # 훅/CTA 공용 배경(테마 통일) — 있으면 스테이징
    backdrop_url = _stage_card_image(script.get("_backdrop"), multichannel_dir, 0)
    # 진행자(브랜드 모델) 플레이트 — 훅/CTA 앵커. 없으면 None(배경 폴백).
    hook_presenter = _stage_named_image(script.get("_hook_presenter"),
                                        multichannel_dir, "presenter_hook.png")
    cta_presenter = _stage_named_image(script.get("_cta_presenter"),
                                       multichannel_dir, "presenter_cta.png")

    return {
        "title": script.get("hook") or script.get("title") or "",
        "hook_keyword": script.get("hook_keyword") or "#시니어",
        "h2_cards": h2_cards,
        "cta_text": script.get("cta") or f"자세한 절차는\n{brand_channel} 블로그에서",
        "audio_url": audio_url,
        "bgm_url": bgm_url,
        "backdrop_url": backdrop_url,
        "hook_presenter": hook_presenter,
        "cta_presenter": cta_presenter,
        "brand": brand_display,
        "channel": brand_channel,
        "accent": accent,
        "duration_sec": round(duration_sec, 2) if duration_sec else 0.0,
        "captions": words or [],
    }


# ─────────────────────────────────────────────────────
# 메인 진입점
# ─────────────────────────────────────────────────────
def _generate_card_images(script: dict, output_dir: Path,
                          category: str) -> int:
    """카드 인포그래픽 이미지를 생성하고 script['h2_cards'][i]['screenshot'] 에 배선.

    실패는 삼켜서 렌더를 막지 않는다(텍스트 카드로 자연 폴백). 생성 장수 반환.
    """
    try:
        from core.video_visual import build_card_images
    except ImportError as e:
        log.warning(f"video_visual 미가용 — 이미지 없이 진행: {e}")
        return 0
    try:
        result = build_card_images(script, category or "default", output_dir,
                                   slug=output_dir.parent.name)
    except Exception as e:  # noqa: BLE001 — 어떤 실패든 렌더는 계속
        log.warning(f"카드 이미지 생성 실패 — 텍스트 카드로 진행: {e}")
        return 0

    cards = script.get("h2_cards") or []
    for i, path in enumerate(result.get("cards", [])):
        if i < len(cards) and isinstance(cards[i], dict) and path:
            cards[i]["screenshot"] = path
    # 훅/CTA 공용 배경(테마 통일용) — script 에 실어 props 빌더가 스테이징
    if result.get("backdrop"):
        script["_backdrop"] = result["backdrop"]
    return int(result.get("count", 0))


def _generate_presenter_plates(script: dict, output_dir: Path,
                               category: str) -> None:
    """훅/CTA 진행자 플레이트를 생성해 script['_hook_presenter']·['_cta_presenter']에 배선.

    브랜드 모델 이미지(multichannel/assets/예시브랜드 모델)가 없거나 어떤 실패든
    조용히 넘어간다(진행자 없이 기존 배경으로 렌더 — 발행·영상 무중단).
    """
    try:
        from core.video_visual import build_presenter_plates
    except ImportError as e:
        log.warning(f"video_visual 미가용 — 진행자 없이 진행: {e}")
        return
    try:
        r = build_presenter_plates(category or "default", output_dir)
    except Exception as e:  # noqa: BLE001 — 어떤 실패든 렌더는 계속
        log.warning(f"진행자 플레이트 생성 실패 — 진행자 없이 진행: {e}")
        return
    if r.get("hook"):
        script["_hook_presenter"] = r["hook"]
    if r.get("cta"):
        script["_cta_presenter"] = r["cta"]


def _build_cover_thumbnail(script: dict, output_dir: Path, category: str,
                           thumbnail_jpg: Path, out_mp4: Path) -> None:
    """전용 썸네일 커버(세로+16:9) 생성. 실패 시 mp4 콘텐츠 프레임으로 폴백.

    - 성공: thumbnail.jpg(세로 1080×1920) + thumbnail_wide.jpg(16:9 1280×720)
    - 실패: ffmpeg 로 12초 지점 프레임(검은 t=0 회피) → thumbnail.jpg
    """
    made = None
    try:
        from core.video_visual import build_thumbnail
        r = build_thumbnail(script, category or "default", output_dir,
                            slug=output_dir.parent.name)
        made = r.get("vertical")
    except Exception as e:  # noqa: BLE001 — 썸네일 실패가 렌더 성공을 막지 않게
        log.warning(f"전용 썸네일 실패 — ffmpeg 프레임 폴백: {e}")
        made = None
    if not made:
        _extract_thumbnail(out_mp4, thumbnail_jpg)


def render_shorts(script: dict, output_dir: Path,
                  *, bgm_dir: Optional[Path] = None,
                  voice: str = TTS_VOICE_DEFAULT,
                  rate: str = TTS_RATE_DEFAULT,
                  pitch: str = TTS_PITCH_DEFAULT,
                  category: str = "",
                  multichannel_dir: Path = MULTICHANNEL_DIR) -> dict:
    """30초 Shorts mp4 + 썸네일 + _meta.json 산출.

    Args:
        script: caption_generator 의 shorts_script — hook/h2_cards/cta/tts_text.
        output_dir: runs/{slug}/multichannel/ 디렉토리.
        bgm_dir: assets/bgm/. None 이면 DEFAULT_BGM_DIR.
        voice/rate/pitch: edge-tts 파라미터.
        multichannel_dir: Remotion 프로젝트 루트.

    Returns:
        {
            "mp4": Path,
            "thumbnail": Path | None,
            "tts_voice": str,
            "bgm_track": str | None,    # 파일명만 (절대경로 노출 안 함)
            "duration_sec": 30.0,
            "render_time_sec": float,   # 전체 (TTS+STT+Remotion+썸네일)
            "stages": {"tts": float, "stt": float, "remotion": float, "thumbnail": float},
            "word_count": int,
        }

    Raises:
        RenderError: 단계별 실패. caller 가 exit code 4(TTS/STT) 또는 5(Remotion) 매핑.
    """
    if not isinstance(script, dict):
        raise RenderError("script 는 dict 여야 합니다")

    tts_text = script.get("tts_text") or ""
    if not tts_text.strip():
        raise RenderError("script['tts_text'] 가 비어 있습니다")

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # 임시 산출물
    tts_mp3 = output_dir / "tts.mp3"
    words_json = output_dir / "tts_words.json"
    input_props_json = output_dir / "inputProps.json"
    out_mp4 = output_dir / "shorts.mp4"
    thumbnail_jpg = output_dir / "thumbnail.jpg"
    meta_json = output_dir / "_meta.json"

    bgm_dir = bgm_dir or DEFAULT_BGM_DIR

    overall_t0 = time.time()
    stages: dict[str, float] = {"tts": 0.0, "stt": 0.0, "remotion": 0.0, "thumbnail": 0.0}
    tts_engine = "edge"
    spoken_text = tts_text   # STT initial_prompt 용 실제 발화 텍스트

    # 1) TTS — MeloTTS(로컬 뉴럴) 우선, 실패 시 edge-tts 폴백
    try:
        stages["tts"], tts_engine, spoken_text = _synthesize_tts(
            tts_text, tts_mp3, voice=voice, rate=rate, pitch=pitch)
    except RenderError:
        raise
    except Exception as e:  # noqa: BLE001 — 외부 라이브러리 어떤 예외든 RenderError 로 묶어 caller 에 위임
        log.error(f"TTS 단계 실패: {e}", exc_info=True)
        raise RenderError(f"TTS 실패: {e}") from e

    # 2) STT word timestamps — 실제 발화 텍스트(정규화 반영)를 힌트로 넘겨 오인식 감소
    try:
        words, audio_dur, stt_elapsed = _run_stt(tts_mp3, prompt=spoken_text)
        stages["stt"] = stt_elapsed
        words_json.write_text(
            json.dumps({"audio_sec": audio_dur, "words": words},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except RenderError:
        raise
    except Exception as e:  # noqa: BLE001
        log.error(f"STT 단계 실패: {e}", exc_info=True)
        raise RenderError(f"STT 실패: {e}") from e

    # 2.5) 카드 인포그래픽 이미지 생성 (실패해도 텍스트 카드로 폴백)
    img_count = _generate_card_images(script, output_dir, category)
    log.info(f"카드 이미지: {img_count}장 배선"
             + (" (없음 — 텍스트 카드)" if img_count == 0 else ""))

    # 2.6) 진행자(브랜드 모델) 플레이트 — 훅/CTA 앵커. 모델 이미지 없으면 조용히 스킵.
    _generate_presenter_plates(script, output_dir, category)

    # 3) BGM 선택
    bgm_path = _pick_bgm(bgm_dir)
    if bgm_path is not None:
        try:
            # 30초 미만이면 경고 (Remotion 이 자르긴 하지만 뒷부분 무음)
            if bgm_path.stat().st_size < 30 * 1024:
                log.warning(f"BGM 파일이 매우 작음 — 길이 부족 가능: {bgm_path.name}")
        except OSError:
            pass

    # 4) inputProps.json 작성
    props = _build_input_props(script, tts_mp3, words, bgm_path,
                              category=category, duration_sec=audio_dur,
                              multichannel_dir=multichannel_dir)
    input_props_json.write_text(
        json.dumps(props, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    log.info(f"inputProps.json 작성: {input_props_json.name} "
             f"(captions={len(words)}, bgm={'O' if bgm_path else 'X'})")

    # 5) Remotion 렌더
    try:
        stages["remotion"] = _run_remotion(input_props_json, out_mp4,
                                           multichannel_dir=multichannel_dir)
    except RenderError:
        raise
    except Exception as e:  # noqa: BLE001
        log.error(f"Remotion 단계 실패: {e}", exc_info=True)
        raise RenderError(f"Remotion 실패: {e}") from e

    # 6) 썸네일 — 전용 커버 생성(세로+16:9), 실패 시 mp4 콘텐츠 프레임 폴백
    t0 = time.time()
    _build_cover_thumbnail(script, output_dir, category, thumbnail_jpg, out_mp4)
    stages["thumbnail"] = time.time() - t0

    overall = time.time() - overall_t0

    # 7) _meta.json (NAVER_ID 등 자격증명 절대 포함 안 함)
    meta = {
        "slug": output_dir.parent.name,
        "rendered_at": dt.datetime.now(
            dt.timezone(dt.timedelta(hours=9))
        ).isoformat(timespec="seconds"),
        "duration_sec": TARGET_DURATION_SEC,
        "audio_duration_sec": round(audio_dur, 2),
        "tts_engine": tts_engine,
        "tts_voice": voice if tts_engine == "edge" else "MeloTTS-Korean",
        "tts_rate": rate,
        "tts_pitch": pitch,
        "tts_speed": MELO_TTS_SPEED if tts_engine == "melo" else None,
        "bgm_track": bgm_path.name if bgm_path else None,
        "word_count": len(words),
        "stages_sec": {k: round(v, 2) for k, v in stages.items()},
        "total_render_sec": round(overall, 2),
        "artifacts": {
            "shorts_mp4": str(out_mp4.name),
            "thumbnail_jpg": str(thumbnail_jpg.name) if thumbnail_jpg.exists() else None,
            "thumbnail_wide_jpg": ("thumbnail_wide.jpg"
                                   if (output_dir / "thumbnail_wide.jpg").exists() else None),
            "tts_mp3": str(tts_mp3.name),
            "words_json": str(words_json.name),
            "input_props_json": str(input_props_json.name),
        },
    }
    meta_json.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    log.info(
        f"Shorts 렌더 완료: {out_mp4.name} | "
        f"TTS {stages['tts']:.1f}s + STT {stages['stt']:.1f}s + "
        f"Remotion {stages['remotion']:.1f}s + Thumb {stages['thumbnail']:.1f}s "
        f"= 총 {overall:.1f}s"
    )

    return {
        "mp4": out_mp4,
        "thumbnail": thumbnail_jpg if thumbnail_jpg.exists() else None,
        "tts_voice": voice,
        "bgm_track": bgm_path.name if bgm_path else None,
        "duration_sec": TARGET_DURATION_SEC,
        "render_time_sec": overall,
        "stages": stages,
        "word_count": len(words),
    }


__all__ = ["render_shorts", "RenderError"]
