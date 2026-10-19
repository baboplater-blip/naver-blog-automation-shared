"""image_dispatcher

Tier 1 (codex 자동) ↔ Tier 2 (수동 폴백) 컨트롤러.

흐름:
1. ImageDispatcher.dispatch(slug, title, subtitle, category, ...)
2. force_tier="manual" 또는 자동 트리거(아래) → Tier 2.
3. Tier 1:
   - CodexCLIBot.generate_post_images(...) 호출
   - 후속으로 ImageInbox.reconcile(slug)로 draft.html 동기화
   - GenerationResult.failed_roles가 절반 이상이면 Tier 2 폴백
4. Tier 2:
   - codex 미호출. build_image_prompt()로 6장 프롬프트만 생성해 클립보드에 일괄 복사.
   - 인박스 폴더를 OS 파일 탐색기로 자동 오픈.
   - notifier 콜백("자동 실패 — 수동 모드 전환, 인박스 폴더 열림") 호출.
   - 사용자가 ChatGPT/Genspark 등에서 직접 6장 받아 인박스에 놓으면 ImageInbox watchdog이
     알아서 draft.html 갱신.

Tier 2 다운그레이드 트리거:
- bot.generate_post_images()에서 RuntimeError (한도 초과)
- FileNotFoundError (codex 미설치)
- failed_roles >= ceil(전체 슬롯 / 2)
- 호출자가 force_tier="manual"
"""

from __future__ import annotations

import math
import os
import shutil
import subprocess
import sys
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal, Optional

_PKG_ROOT = Path(__file__).resolve().parent.parent
_PROJECT_ROOT = _PKG_ROOT.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from naver_blog_v3.core.codex_cli_bot import (  # noqa: E402
    BodyRecipe,
    CodexCLIBot,
    DIGITAL_CATEGORY,
    GenerationResult,
    InfoRecipe,
    RoleName,
)
from naver_blog_v3.core.image_inbox import (  # noqa: E402
    ImageInbox,
    ReconcileReport,
    DEFAULT_EXPECTED_SLOTS,
    DIGITAL_EXPECTED_SLOTS,
)
from naver_blog_v3.core.image_prompt_builder import build_image_prompt  # noqa: E402

# ──────────────────────────────────────────────────────────────────────────────
# 상수
# ──────────────────────────────────────────────────────────────────────────────

TierName = Literal["tier1_auto", "tier2_manual"]

# Tier 2 시 자동으로 열 웹 URL (ChatGPT, Genspark) — 사용자가 환경 변수로 끌 수도 있다.
_DEFAULT_FALLBACK_URLS: tuple[str, ...] = (
    "https://chatgpt.com/",
    "https://www.genspark.ai/",
)
_FALLBACK_OPEN_WEB_ENV: str = "examplebrand_FALLBACK_OPEN_WEB"


# ──────────────────────────────────────────────────────────────────────────────
# 데이터 클래스
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class DispatchResult:
    slug: str
    tier_used: TierName
    generation: Optional[GenerationResult]
    reconcile: ReconcileReport
    fallback_reason: Optional[str]


# ──────────────────────────────────────────────────────────────────────────────
# ClipboardHandler
# ──────────────────────────────────────────────────────────────────────────────


class ClipboardHandler:
    """Windows 클립보드에 텍스트 묶음을 복사한다.

    1순위: pyperclip (있으면 가장 안정적)
    2순위: PowerShell `Set-Clipboard` (Windows 표준)
    3순위: `clip.exe` (기본 명령)
    """

    def __init__(self, runner: Optional[Callable[..., subprocess.CompletedProcess]] = None) -> None:
        self._runner = runner or subprocess.run

    def copy(self, text: str) -> bool:
        """클립보드에 text를 복사. 성공 여부 반환."""
        if not isinstance(text, str):
            raise TypeError("text는 문자열이어야 함")
        # 1) pyperclip
        try:
            import pyperclip  # type: ignore[import-not-found]
            pyperclip.copy(text)
            return True
        except (ImportError, Exception):  # noqa: BLE001 - pyperclip 자체 예외 다양
            pass

        # 2) PowerShell Set-Clipboard (here-string)
        try:
            ps_script = "$input | Set-Clipboard"
            completed = self._runner(
                ["powershell", "-NoProfile", "-Command", ps_script],
                input=text,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
            if completed.returncode == 0:
                return True
        except (OSError, subprocess.SubprocessError):
            pass

        # 3) clip.exe (cp949 한계 — pyperclip이나 PowerShell 권장)
        try:
            completed = self._runner(
                ["clip"],
                input=text,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
            return completed.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False


# ──────────────────────────────────────────────────────────────────────────────
# ImageDispatcher
# ──────────────────────────────────────────────────────────────────────────────


class ImageDispatcher:
    """Tier 1/2 컨트롤러. dispatch() 한 번으로 6장 전체 흐름 수행."""

    def __init__(
        self,
        bot: CodexCLIBot,
        inbox: ImageInbox,
        clipboard_handler: Optional[ClipboardHandler] = None,
        notifier: Optional[Callable[[str], None]] = None,
        folder_opener: Optional[Callable[[Path], None]] = None,
        browser_opener: Optional[Callable[[str], None]] = None,
    ) -> None:
        if not isinstance(bot, CodexCLIBot):
            raise TypeError("bot은 CodexCLIBot 인스턴스여야 함")
        if not isinstance(inbox, ImageInbox):
            raise TypeError("inbox는 ImageInbox 인스턴스여야 함")
        self._bot: CodexCLIBot = bot
        self._inbox: ImageInbox = inbox
        self._clipboard: ClipboardHandler = clipboard_handler or ClipboardHandler()
        self._notifier: Callable[[str], None] = notifier or (lambda _msg: None)
        self._folder_opener: Callable[[Path], None] = folder_opener or _open_folder_default
        self._browser_opener: Callable[[str], None] = browser_opener or webbrowser.open

    def dispatch(
        self,
        *,
        slug: str,
        title: str,
        subtitle: str,
        category: str,
        body_recipes: Optional[list[BodyRecipe]] = None,
        info_recipes: Optional[list[InfoRecipe]] = None,
        thumb_illustration_hint: Optional[str] = None,
        extra_recipes: Optional[list[tuple[RoleName, Any]]] = None,
        force_tier: Optional[Literal["auto", "manual"]] = None,
    ) -> DispatchResult:
        """전체 6장(또는 디지털 7~8장) 발행 흐름 오케스트레이션."""
        if not isinstance(slug, str) or not slug.strip():
            raise ValueError("slug는 비어있지 않은 문자열이어야 함")

        expected = (
            DIGITAL_EXPECTED_SLOTS if category == DIGITAL_CATEGORY else DEFAULT_EXPECTED_SLOTS
        )

        if force_tier == "manual":
            return self._run_tier2(
                slug=slug,
                title=title,
                subtitle=subtitle,
                category=category,
                body_recipes=body_recipes,
                info_recipes=info_recipes,
                thumb_illustration_hint=thumb_illustration_hint,
                expected=expected,
                fallback_reason="force_tier=manual",
            )

        # Tier 1 시도
        try:
            gen = self._bot.generate_post_images(
                slug=slug,
                title=title,
                subtitle=subtitle,
                category=category,
                body_recipes=body_recipes,
                info_recipes=info_recipes,
                thumb_illustration_hint=thumb_illustration_hint,
                extra_recipes=extra_recipes,
            )
        except FileNotFoundError as exc:
            return self._run_tier2(
                slug=slug,
                title=title,
                subtitle=subtitle,
                category=category,
                body_recipes=body_recipes,
                info_recipes=info_recipes,
                thumb_illustration_hint=thumb_illustration_hint,
                expected=expected,
                fallback_reason=f"codex 미설치: {exc}",
            )
        except RuntimeError as exc:
            return self._run_tier2(
                slug=slug,
                title=title,
                subtitle=subtitle,
                category=category,
                body_recipes=body_recipes,
                info_recipes=info_recipes,
                thumb_illustration_hint=thumb_illustration_hint,
                expected=expected,
                fallback_reason=f"한도 초과: {exc}",
            )

        # failed_roles 절반 이상 → Tier 2
        total_slots = max(len(gen.success_paths) + len(gen.failed_roles), expected)
        half = math.ceil(total_slots / 2)
        if len(gen.failed_roles) >= half:
            reconcile_partial = self._inbox.reconcile(slug, expected_slots=expected)
            tier2 = self._run_tier2(
                slug=slug,
                title=title,
                subtitle=subtitle,
                category=category,
                body_recipes=body_recipes,
                info_recipes=info_recipes,
                thumb_illustration_hint=thumb_illustration_hint,
                expected=expected,
                fallback_reason=(
                    f"실패 슬롯 {len(gen.failed_roles)}/{total_slots} ≥ 절반 — 자동 폴백"
                ),
            )
            # Tier 1에서 부분 성공한 PNG도 reconcile에 누적되므로 generation은 보존.
            tier2.generation = gen
            tier2.reconcile = reconcile_partial
            return tier2

        # Tier 1 정상 — reconcile만 추가 수행 (대부분 watchdog과 무관하게 안전망 역할)
        reconcile = self._inbox.reconcile(slug, expected_slots=expected)
        return DispatchResult(
            slug=slug,
            tier_used="tier1_auto",
            generation=gen,
            reconcile=reconcile,
            fallback_reason=None,
        )

    # ──────────────────────────────────────────────────────────────────────
    # Tier 2 — 수동 폴백
    # ──────────────────────────────────────────────────────────────────────

    def _run_tier2(
        self,
        *,
        slug: str,
        title: str,
        subtitle: str,
        category: str,
        body_recipes: Optional[list[BodyRecipe]],
        info_recipes: Optional[list[InfoRecipe]],
        thumb_illustration_hint: Optional[str],
        expected: int,
        fallback_reason: str,
    ) -> DispatchResult:
        """Tier 2: codex 미호출. 6장 프롬프트 클립보드 + 인박스 폴더 오픈 + 알림."""
        prompts = self._compose_all_prompts(
            title=title,
            subtitle=subtitle,
            category=category,
            body_recipes=body_recipes,
            info_recipes=info_recipes,
            thumb_illustration_hint=thumb_illustration_hint,
            expected=expected,
        )
        bundle = self._format_prompts_for_clipboard(slug=slug, prompts=prompts)
        clipboard_ok = self._clipboard.copy(bundle)

        # 인박스 폴더 자동 오픈
        try:
            self._folder_opener(self._inbox.inbox_dir)
        except OSError:
            pass

        # 옵션: 브라우저 자동 오픈 (환경변수로 끌 수 있음)
        if os.environ.get(_FALLBACK_OPEN_WEB_ENV, "1") != "0":
            for url in _DEFAULT_FALLBACK_URLS:
                try:
                    self._browser_opener(url)
                except OSError:
                    pass

        msg_parts = ["자동 실패 — 수동 모드 전환, 인박스 폴더 열림"]
        if clipboard_ok:
            msg_parts.append(f"프롬프트 {len(prompts)}개 클립보드 복사 완료")
        else:
            msg_parts.append("클립보드 복사 실패 — 인박스 폴더에서 수동 진행")
        msg_parts.append(f"사유: {fallback_reason}")
        self._notifier(" / ".join(msg_parts))

        # Tier 2에서도 reconcile은 빈 상태로라도 반환 (생성된 게 없으면 missing_slots가 가득)
        reconcile = self._inbox.reconcile(slug, expected_slots=expected)
        return DispatchResult(
            slug=slug,
            tier_used="tier2_manual",
            generation=None,
            reconcile=reconcile,
            fallback_reason=fallback_reason,
        )

    @staticmethod
    def _compose_all_prompts(
        *,
        title: str,
        subtitle: str,
        category: str,
        body_recipes: Optional[list[BodyRecipe]],
        info_recipes: Optional[list[InfoRecipe]],
        thumb_illustration_hint: Optional[str],
        expected: int,
    ) -> list[tuple[str, str]]:
        """Tier 2에서 사용자에게 던질 프롬프트 묶음 (label, prompt)."""
        results: list[tuple[str, str]] = []

        # thumb
        try:
            results.append((
                "thumb",
                build_image_prompt(
                    role="thumb",
                    title=title,
                    subtitle=subtitle,
                    category=category,
                    illustration_hint=thumb_illustration_hint,
                ),
            ))
        except (ValueError, FileNotFoundError) as exc:
            results.append(("thumb", f"[프롬프트 생성 실패: {exc}]"))

        # body x2 (정보 없으면 빈 슬롯 안내)
        for idx in range(2):
            label = f"body{idx + 1}"
            if body_recipes and idx < len(body_recipes):
                recipe = body_recipes[idx]
                hint = _body_recipe_to_hint(recipe)
                try:
                    results.append((
                        label,
                        build_image_prompt(
                            role="body",
                            title=title,
                            subtitle=subtitle,
                            category=category,
                            illustration_hint=hint,
                        ),
                    ))
                except (ValueError, FileNotFoundError) as exc:
                    results.append((label, f"[프롬프트 생성 실패: {exc}]"))
            else:
                results.append((label, "[body 레시피 미제공 — 수동으로 채워주세요]"))

        # info x3 (또는 디지털 +)
        info_count = expected - 3  # 6 - 3(thumb+body*2) = 3, 8 - 3 = 5
        for idx in range(info_count):
            label = f"info{idx + 1}"
            if info_recipes and idx < len(info_recipes):
                recipe = info_recipes[idx]
                try:
                    results.append((
                        label,
                        build_image_prompt(
                            role="info",
                            title=title,
                            subtitle=subtitle,
                            category=category,
                            body_items=recipe.items,
                            illustration_hint=recipe.illustration_hint,
                        ),
                    ))
                except (ValueError, FileNotFoundError) as exc:
                    results.append((label, f"[프롬프트 생성 실패: {exc}]"))
            else:
                results.append((label, "[info 레시피 미제공 — 수동으로 채워주세요]"))

        return results

    @staticmethod
    def _format_prompts_for_clipboard(
        *, slug: str, prompts: list[tuple[str, str]]
    ) -> str:
        """6개 프롬프트를 보기 좋은 단일 문자열로 묶는다."""
        header = (
            f"=== examplebrand 이미지 수동 발급 ({slug}) ===\n"
            f"총 {len(prompts)}장. 각 프롬프트를 ChatGPT/Genspark에 차례로 던지고\n"
            f"결과 PNG를 {slug}_{{role}}{{n}}.png 규칙으로 인박스 폴더에 저장하세요.\n"
        )
        sections: list[str] = [header]
        for label, prompt in prompts:
            sections.append(f"\n--- {label} ({slug}_{label}.png) ---\n{prompt}")
        return "\n".join(sections)


# ──────────────────────────────────────────────────────────────────────────────
# 헬퍼
# ──────────────────────────────────────────────────────────────────────────────


def _body_recipe_to_hint(recipe: BodyRecipe) -> str:
    """body 역할 BodyRecipe → 영문 일러스트 힌트 (codex_cli_bot과 동일 규칙)."""
    base = (recipe.illustration_hint or "").strip()
    kind_map = {
        "five_step": "Render a horizontal 5-column step sequence numbered 1 to 5",
        "before_after": "Render a left/right Before vs After comparison split",
        "self_diagnosis": "Render a self-diagnosis checklist with check icons",
    }
    kind_clause = kind_map.get(recipe.kind, "")
    label_hints: list[str] = []
    for it in (recipe.items or [])[:5]:
        if "step" in it and "desc" in it:
            label_hints.append(f"{it['step']}: {it['desc']}")
        elif "q" in it and "a" in it:
            label_hints.append(f"Q: {it['q']}")
    label_clause = "; ".join(label_hints)
    parts = [p for p in (kind_clause, label_clause, base) if p]
    return ". ".join(parts)


def _open_folder_default(folder: Path) -> None:
    """Windows 우선 — os.startfile, 실패 시 explorer.exe."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    startfile = getattr(os, "startfile", None)
    if callable(startfile):
        try:
            startfile(str(folder))
            return
        except OSError:
            pass
    # 폴백
    try:
        subprocess.Popen(
            ["explorer.exe", str(folder)],
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
    except (OSError, subprocess.SubprocessError):
        # 비 윈도우 폴백
        opener = shutil.which("xdg-open") or shutil.which("open")
        if opener:
            try:
                subprocess.Popen([opener, str(folder)])
            except (OSError, subprocess.SubprocessError):
                pass
