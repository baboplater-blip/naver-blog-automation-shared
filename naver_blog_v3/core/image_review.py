"""Hash-bound human review notes for images attached to a saved draft.

The record is advisory: it does not change image provenance, Korean-text checks,
image-completeness gates, or publishing decisions.
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import List

log = logging.getLogger(__name__)
REVIEW_FILE = "image_review.json"
REVIEW_VERSION = 2
_FIELDS = ("confirmed_review", "topic_relevance", "title_legible", "body_match")


def _runs_dir() -> Path:
    from core import draft_store
    return draft_store._RUNS_DIR


def _image_records(post: dict) -> List[dict]:
    images = []
    for raw in post.get("image_paths") or []:
        path = Path(str(raw or ""))
        try:
            if path.is_file():
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                images.append({"name": path.name, "path": str(path.resolve()),
                               "sha256": digest})
        except (OSError, ValueError) as exc:
            log.debug("이미지 검토 지문 계산 실패(%s)", type(exc).__name__)
    return images


def content_binding(post: dict) -> dict:
    """Bind the human review to current title, article text, and image bytes."""
    post = post or {}
    images = _image_records(post)
    missing_images = []
    for raw in post.get("image_paths") or []:
        path = Path(str(raw or ""))
        try:
            if not path.is_file():
                missing_images.append(str(raw))
        except (OSError, ValueError) as exc:
            log.debug("이미지 검토 경로 확인 실패(%s)", type(exc).__name__)
            missing_images.append(str(raw))
    canonical = json.dumps({"title": str(post.get("title") or ""),
                            "content": str(post.get("content") or ""),
                            "images": images, "missing_images": missing_images}, ensure_ascii=False,
                       sort_keys=True, separators=(",", ":"))
    return {"sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            "images": images,
            "missing_images": missing_images}


def _path(slug: str) -> Path:
    return _runs_dir() / str(slug or "") / REVIEW_FILE


def save_review(slug: str, post: dict, decisions: List[dict], *,
                reviewed_at: str = "") -> dict:
    """Persist individual human decisions without changing the draft or its assets."""
    if not str(slug or "").strip():
        raise ValueError("저장 글 식별자가 비어 있습니다")
    binding = content_binding(post or {})
    current_paths = {entry["path"] for entry in binding["images"]}
    normalized = []
    for item in decisions or []:
        if not isinstance(item, dict):
            continue
        path = str(Path(str(item.get("path") or "")).resolve())
        image = next((entry for entry in binding["images"] if entry["path"] == path), None)
        if image is None:
            raise ValueError("검토 대상 이미지가 현재 글에 없습니다")
        if item.get("confirmed_review") is not True:
            raise ValueError("각 이미지를 직접 확인한 뒤 검토 완료를 표시해야 합니다")
        normalized.append({"path": path, "name": image["name"],
                           "sha256": image["sha256"],
                           **{field: bool(item.get(field)) for field in _FIELDS}})
    if not current_paths or not normalized:
        raise ValueError("현재 글에 확인 가능한 이미지가 없습니다")
    record = {"version": REVIEW_VERSION, "slug": str(slug),
              "reviewed_at": str(reviewed_at or ""),
              "binding": binding["sha256"], "images": normalized,
              "advisory_only": True}
    target = _path(slug)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(record, handle, ensure_ascii=False, indent=2)
        temporary.replace(target)
    except OSError:
        log.error("이미지 검토 기록 저장 실패", exc_info=True)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            log.warning("임시 이미지 검토 기록 정리 실패", exc_info=True)
        raise
    return record


def load_review(slug: str, post: dict) -> dict:
    """Read the review and report stale/missing bindings without reusing them."""
    result = {"status": "missing", "reason": "no_review", "record": None,
              "binding": content_binding(post or {})}
    target = _path(slug)
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return result
    except (OSError, ValueError) as exc:
        log.warning("이미지 검토 기록 읽기 실패(%s)", type(exc).__name__)
        result.update(status="unavailable", reason="record_unreadable")
        return result
    if not isinstance(raw, dict) or raw.get("version") != REVIEW_VERSION:
        result.update(status="stale", reason="unsupported_record")
        return result
    if raw.get("binding") != result["binding"]["sha256"]:
        result.update(status="stale", reason="article_or_image_changed")
        return result
    reviewed = {str(item.get("path")): item for item in raw.get("images", [])
                if isinstance(item, dict)}
    for image in result["binding"]["images"]:
        item = reviewed.get(image["path"])
        if not item or item.get("sha256") != image["sha256"]:
            result.update(status="stale", reason="image_set_changed")
            return result
    confirmed = {path: bool(item.get("confirmed_review"))
                 for path, item in reviewed.items()}
    reviewed_count = sum(1 for image in result["binding"]["images"]
                         if confirmed.get(image["path"], False))
    if reviewed_count < len(result["binding"]["images"]):
        result.update(status="partial", reason="some_images_not_reviewed", record=raw,
                      reviewed_images=reviewed_count,
                      total_images=len(result["binding"]["images"]))
        return result
    result.update(status="current", reason="current_review", record=raw)
    return result


__all__ = ["content_binding", "save_review", "load_review", "REVIEW_FILE"]
