"""하네스 관측 어댑터 — auto_scheduler 파이프라인의 각 단계에서
naver_blog_automation_v3/{briefs,runs,reports}/ 에 표준화된 brief/run/report
파일을 떨어뜨린다.

설계 원칙:
- 관측 전용. 기존 발행 동작은 1바이트도 바꾸지 않는다.
- stdlib 만 사용 (pyyaml 등 외부 의존성 없음).
- 모든 emit_* 함수는 _safe 데코레이터로 감싸 어떤 예외도 호출 사이트로
  전파되지 않는다. emitter 가 죽어도 GUI 발행은 정상 진행.
- HARNESS_EMIT_ENABLED=False 면 즉시 no-op.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from datetime import date, datetime
from functools import wraps
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger(__name__)

_KST = "+09:00"
_BASE_DIR = Path(__file__).resolve().parent.parent           # naver_blog_v3/
_HARNESS_ROOT = _BASE_DIR.parent                              # naver_blog_automation_v3/
from config import profile as _nbaprofile
_DATA_HARNESS = _nbaprofile.harness_data_root()

_BRIEFS_DIR = _DATA_HARNESS / "briefs"
_RUNS_DIR = _DATA_HARNESS / "runs"
_REPORTS_DIR = _DATA_HARNESS / "reports"

_SLUG_STRIP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\s*[—\-]\s*")
_SLUG_DEADLINE_RE = re.compile(r"\s*\(D-\d+\s*마감임박\)\s*$")
_SLUG_SAFE_RE = re.compile(r"[^\w가-힣\-]", re.UNICODE)


# ─────────────────────────────────────────────────────
# 인프라
# ─────────────────────────────────────────────────────

def _safe(fn):
    """emitter 함수가 어떤 예외를 던져도 호출 사이트로 전파되지 않게 한다."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            if not is_enabled():
                return None
            return fn(*args, **kwargs)
        except Exception as e:
            log.warning(f"[harness_emit] {fn.__name__} skipped: {e}")
            return None
    return wrapper


def is_enabled() -> bool:
    """settings.HARNESS_EMIT_ENABLED 플래그 확인. import 실패해도 기본 True."""
    try:
        from config import settings as s
        return bool(getattr(s, "HARNESS_EMIT_ENABLED", True))
    except Exception:
        return True


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S") + _KST


def _today() -> str:
    return date.today().strftime("%Y-%m-%d")


def _slug_from_title(title: str) -> str:
    """'2026-05-08 — 청년 주거지원금 (D-3 마감임박)' → '청년-주거지원금'

    한글은 보존, 공백→하이픈, 안전하지 않은 문자 제거. 80자 cap.
    """
    s = _SLUG_STRIP_RE.sub("", title or "").strip()
    s = _SLUG_DEADLINE_RE.sub("", s).strip()
    s = re.sub(r"\s+", "-", s)
    s = _SLUG_SAFE_RE.sub("", s)
    s = s.strip("-")
    return s[:80] if s else "untitled"


def make_slug(title: str, target_date: str | None = None) -> str:
    """'{date}_{kebab-keyword}' 형식의 식별자 — 하루 같은 키워드 중복 시 _2,_3 접미."""
    d = target_date or _today()
    base = f"{d}_{_slug_from_title(title)}"
    if not _BRIEFS_DIR.exists():
        return base
    candidate = base
    n = 2
    while (_BRIEFS_DIR / f"{candidate}.yaml").exists():
        candidate = f"{base}_{n}"
        n += 1
        if n > 99:
            break
    return candidate


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".harness_", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise


# ─────────────────────────────────────────────────────
# 미니 YAML 직렬화 (flat dict + list[str] + list[dict] 1단)
# ─────────────────────────────────────────────────────

_YAML_NEEDS_QUOTE_RE = re.compile(r"[:#\[\]\{\},&*!|>'\"%@`\t]|^\s|\s$|^[-?]\s|^$")


def _yaml_scalar(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    if "\n" in s:
        # 멀티라인은 | 블록으로 (들여쓰기는 호출 측에서 부여)
        return "|"
    if _YAML_NEEDS_QUOTE_RE.search(s) or s.lower() in {"null", "true", "false", "yes", "no"}:
        # 안전하게 더블쿼트로 감싸고 백슬래시·더블쿼트 이스케이프
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


def _yaml_block_lines(v: str, indent: str) -> list[str]:
    return [indent + line for line in v.splitlines()]


def _dump_yaml(data: dict, _indent: int = 0) -> str:
    """flat dict 우선. 값은 scalar | list[scalar] | list[dict] | dict(1단) 지원."""
    lines: list[str] = []
    pad = "  " * _indent
    for k, v in data.items():
        key = str(k)
        if isinstance(v, dict):
            lines.append(f"{pad}{key}:")
            lines.append(_dump_yaml(v, _indent + 1))
        elif isinstance(v, (list, tuple)):
            seq = list(v)
            if not seq:
                lines.append(f"{pad}{key}: []")
                continue
            if all(isinstance(x, dict) for x in seq):
                lines.append(f"{pad}{key}:")
                for item in seq:
                    item_lines = _dump_yaml(item, _indent + 1).splitlines()
                    if not item_lines:
                        continue
                    first, rest = item_lines[0], item_lines[1:]
                    lines.append(pad + "  - " + first.lstrip())
                    for r in rest:
                        lines.append(pad + "    " + r.lstrip() if r.strip() else r)
            else:
                lines.append(f"{pad}{key}:")
                for item in seq:
                    lines.append(f"{pad}  - {_yaml_scalar(item)}")
        elif isinstance(v, str) and "\n" in v:
            lines.append(f"{pad}{key}: |")
            lines.extend(_yaml_block_lines(v, pad + "  "))
        else:
            lines.append(f"{pad}{key}: {_yaml_scalar(v)}")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────
# 카테고리 → 페르소나 프리셋 매핑
# ─────────────────────────────────────────────────────

_PERSONA_BY_CATEGORY = {
    "청년": "balanced",
    "주거": "balanced",
    "복지": "safe",
    "바우처": "balanced",
    "취업": "balanced",
    "의료": "safe",
    "교육": "balanced",
    "default": "balanced",
}


def _persona_for(category: str) -> str:
    return _PERSONA_BY_CATEGORY.get(category, "balanced")


def _clean_title(title: str) -> str:
    return _SLUG_STRIP_RE.sub("", title or "").strip()


# ─────────────────────────────────────────────────────
# Public API — emit_*
# ─────────────────────────────────────────────────────

@_safe
def emit_batch_brief(target_date: str | None,
                     posts: list[dict],
                     reserve_times: list[str]) -> Path | None:
    """하루 배치 요약 brief — 모든 post의 slug/title/category/예약시각."""
    d = target_date or _today()
    items = []
    for i, p in enumerate(posts):
        title = p.get("title", "")
        clean = _clean_title(title)
        cat = _detect_category_safely(clean)
        rt = reserve_times[i] if i < len(reserve_times) else ""
        items.append({
            "slug": make_slug_for_existing(title, d, occupied_slugs={x["slug"] for x in items}),
            "title": clean,
            "category": cat,
            "reserve_time": rt,
        })
    payload = {
        "slug": f"{d}_batch",
        "created_at": _now_iso(),
        "intent": "write_post_batch",
        "target_date": d,
        "total": len(items),
        "posts": items,
    }
    out = _BRIEFS_DIR / f"{d}_batch.yaml"
    _atomic_write_text(out, _dump_yaml(payload) + "\n")
    return out


def make_slug_for_existing(title: str, target_date: str,
                           occupied_slugs: set[str]) -> str:
    """배치 brief 빌드 시 in-memory 충돌 회피 (디스크 검사 + 같은 배치 안의 슬러그 셋)."""
    base = f"{target_date}_{_slug_from_title(title)}"
    if base not in occupied_slugs and not (_BRIEFS_DIR / f"{base}.yaml").exists():
        return base
    n = 2
    while True:
        cand = f"{base}_{n}"
        if cand not in occupied_slugs and not (_BRIEFS_DIR / f"{cand}.yaml").exists():
            return cand
        n += 1
        if n > 99:
            return cand


@_safe
def emit_post_brief(slug: str,
                    title: str,
                    category: str,
                    persona_preset: str | None = None,
                    deadline: str | None = None,
                    dup_status: dict | None = None,
                    notes: str = "") -> Path | None:
    """글 1편 단위 brief — 중복 검사 결과·페르소나·카테고리 기록."""
    d = slug.split("_", 1)[0] if "_" in slug else _today()
    persona = persona_preset or _persona_for(category)
    dup = dup_status or {"is_dup": False}
    payload = {
        "slug": slug,
        "created_at": _now_iso(),
        "intent": "write_post",
        "keyword": _clean_title(title),
        "title": _clean_title(title),
        "category": category or "default",
        "persona_preset": persona,
        "deadline": deadline,
        "duplicate_check": {
            "is_dup": bool(dup.get("is_dup", False)),
            "matched": dup.get("matched"),
            "score": dup.get("score"),
        },
        "notes": notes or "auto_scheduler hook 자동 생성",
    }
    out = _BRIEFS_DIR / f"{slug}.yaml"
    _atomic_write_text(out, _dump_yaml(payload) + "\n")
    return out


@_safe
def emit_run_record(slug: str,
                    post_data: dict,
                    qa_meta: dict | None = None,
                    image_count: int = 0,
                    status: str = "generated",
                    preview_path: str | None = None) -> Path | None:
    """글 생성 직후 — 본문은 복사하지 않고 메타와 미리보기 경로만 JSON 으로 저장."""
    qa_meta = qa_meta or {}
    content = post_data.get("content", "") or ""
    record = {
        "slug": slug,
        "recorded_at": _now_iso(),
        "status": status,
        "title": post_data.get("title", ""),
        "category": post_data.get("category", "default"),
        "summary": post_data.get("summary", ""),
        "tags": list(post_data.get("tags", []) or []),
        "content_chars": len(content),
        "image_count": int(image_count),
        "qa": {
            "quality_warning": post_data.get("_quality_warning"),
            # 2026-05-25 — D.I.A.+ / AEO 자동 보완 결과 점수 (None 이면 비활성)
            "dia_score": post_data.get("_dia_score"),
            "aeo_score": post_data.get("_aeo_score"),
            "extra": qa_meta,
        },
        "preview_path": preview_path,
    }
    out_dir = _RUNS_DIR / slug
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "qa.json"
    _atomic_write_text(out, json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    if preview_path:
        try:
            (out_dir / "draft_path.txt").write_text(str(preview_path), encoding="utf-8")
        except OSError:
            pass
    return out


@_safe
def emit_publish_brief(slug: str,
                       title: str,
                       category: str,
                       tags: Iterable[str] | None = None,
                       schedule_at: str | None = None,
                       qa_status: str = "UNKNOWN",
                       quality_score: float | int | None = None,
                       dup_jaccard: float | None = None,
                       success: bool = False,
                       url: str = "",
                       error: str | None = None,
                       publish_method: str = "selenium",
                       dia_score: float | int | None = None,
                       aeo_score: float | int | None = None) -> Path | None:
    """publish_brief.yaml 발행. (2026-05-26) dia_score/aeo_score 옵션 추가 —
    quality_validator.validate_and_fix 가 post_data 에 채워주는 값을
    호출자가 그대로 전달하면 운영 리포트에서 추세 추적 가능. None 이면 키는
    기록되지만 값은 null."""
    payload = {
        "slug": slug,
        "title": title,
        "category": category or "default",
        "tags": list(tags or []),
        "schedule_at": schedule_at,
        "qa_status": qa_status,
        "quality_score": quality_score,
        "duplicate_max_jaccard": dup_jaccard,
        "dia_score": dia_score,
        "aeo_score": aeo_score,
        "publish_method": publish_method,
        "success": bool(success),
        "url": url or "",
        "error": error,
        "human_approval_required": False,
        "recorded_at": _now_iso(),
    }
    out_dir = _RUNS_DIR / slug
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "publish_brief.yaml"
    _atomic_write_text(out, _dump_yaml(payload) + "\n")
    return out


@_safe
def emit_daily_report(target_date: str | None,
                      pipeline_status: dict | None,
                      total: int,
                      success: int,
                      generated: int,
                      skipped_dup: int,
                      failed: list[dict] | None = None,
                      published: list[dict] | None = None) -> Path | None:
    """사람이 읽는 일일 다이제스트 — 발행 목록·파이프라인 단계 결과."""
    d = target_date or _today()
    pipeline_status = pipeline_status or {}
    failed = failed or []
    published = published or []

    lines: list[str] = []
    lines.append(f"# {d} 발행 리포트")
    lines.append("")
    lines.append("## 요약")
    lines.append(f"- 총 시도: {total}")
    lines.append(f"- 성공 발행: {success}")
    lines.append(f"- 중복 스킵: {skipped_dup}")
    lines.append(f"- 글 생성됨: {generated}")
    lines.append(f"- 실패: {len(failed)}")
    lines.append("")

    if pipeline_status:
        lines.append("## 파이프라인 단계")
        for step, info in pipeline_status.items():
            ok = info.get("success") if isinstance(info, dict) else None
            msg = info.get("message", "") if isinstance(info, dict) else str(info)
            mark = "✅" if ok else ("❌" if ok is False else "•")
            lines.append(f"- {mark} {step} — {msg}")
        lines.append("")

    if published:
        lines.append("## 발행 목록")
        lines.append("| 시각 | 카테고리 | 제목 | 상태 | URL |")
        lines.append("|---|---|---|---|---|")
        for p in published:
            lines.append("| {time} | {cat} | {title} | {status} | {url} |".format(
                time=p.get("reserve_time", ""),
                cat=p.get("category", ""),
                title=(p.get("title", "") or "")[:50],
                status="✅" if p.get("success") else "❌",
                url=p.get("url", "") or "",
            ))
        lines.append("")

    if failed:
        lines.append("## 실패 항목")
        for f in failed:
            lines.append(f"- {f.get('title', '')} — {f.get('reason', '')}")
        lines.append("")

    lines.append(f"_생성: {_now_iso()}_")

    out = _REPORTS_DIR / f"{d}_pipeline.md"
    _atomic_write_text(out, "\n".join(lines) + "\n")
    return out


# ─────────────────────────────────────────────────────
# 내부 헬퍼
# ─────────────────────────────────────────────────────

def _detect_category_safely(clean_title: str) -> str:
    try:
        from core.content_generator import detect_category
        return detect_category(clean_title) or "default"
    except Exception:
        return "default"
