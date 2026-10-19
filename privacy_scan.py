"""Heuristic pre-share check. Reports locations, never credential values."""
from __future__ import annotations
import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache"}
PRIVATE_DIRS = {"profiles", "logs", "output", "previews", "screenshots", "image_inbox", "runs", "reports", "briefs", "_runtime", ".auto-dev", ".local"}
TEXT_SUFFIXES = {".py", ".json", ".txt", ".md", ".bat", ".ps1", ".yaml", ".yml", ".toml", ".ini"}
CREDENTIAL = re.compile(
    r'''(?i)(?<![\w])["']?((?:[a-z0-9_]+_)?(?:api_key|client_secret|access_token|refresh_token|auth_token|password|passwd|secret_key))["']?\s*[:=]\s*["']([^"'\r\n]{8,})["']'''
)
FIELD_NAMES = {"api_key", "api_client_id", "api_client_secret", "access_token", "refresh_token", "kakao_token", "nvidia_api_key", "searchad_api_key", "searchad_secret_key", "api_hub_key", "client_secret", "naver_pw"}
PATTERNS = {
    "private_windows_path": re.compile(r"(?i)\b[A-Z]:[\\/]+Users[\\/]+(?!Public[\\/]|Default[\\/])[^\\/\s\"']+"),
    "email_address": re.compile(r"(?i)\b[A-Z0-9._%+-]+@(?!example\.(?:com|org|net)\b)[A-Z0-9.-]+\.[A-Z]{2,}\b"),
    "provider_token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|sk-(?:proj-|ant-)?[A-Za-z0-9_-]{24,}|nvapi-[A-Za-z0-9_-]{24,}|AIza[0-9A-Za-z_-]{30,}|AKIA[0-9A-Z]{16})\b"),
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    "encrypted_credentials": re.compile(r"\bgAAAAA[A-Za-z0-9_-]{30,}"),
    "fixed_blog_account": re.compile(r"(?i)https?://(?:m\.)?blog\.naver\.com/(?!Post|NVisit|mylog|category|rego|example|sample|your_|\{)[a-z0-9_-]{2,30}"),
}

def is_forbidden_path(path: Path) -> bool:
    name = path.name.lower()
    parts = {part.lower() for part in path.parts}
    live_settings = name == "settings.py" and path.as_posix() != "naver_blog_v3/gui/tabs/settings.py"
    return (
        live_settings or bool(parts & PRIVATE_DIRS)
        or name.startswith("user_settings") or name == ".salt" or name.startswith(".env")
        or ".bak" in name or ("cookie" in name and path.suffix.lower() != ".py")
        or path.suffix.lower() in {".key", ".pem", ".pfx", ".p12", ".xlsx", ".xls", ".db", ".sqlite", ".sqlite3"}
    )

def line_categories(line: str) -> set[str]:
    categories = {category for category, pattern in PATTERNS.items() if pattern.search(line)}
    for match in CREDENTIAL.finditer(line):
        value = match.group(2)
        if value.lower() in FIELD_NAMES or value.lower().startswith(("your_", "example_", "sample_", "placeholder", "changeme", "dummy_", "test_", "load_secret(", "load_shared_secret(", "발급받은_")):
            continue
        categories.add("credential_literal")
    return categories

def publishable_files() -> list[Path]:
    if (ROOT / ".git").exists():
        result = subprocess.run(["git", "-C", str(ROOT), "ls-files", "--cached", "--others", "--exclude-standard", "-z"], check=True, capture_output=True)
        return sorted({Path(os.fsdecode(name)) for name in result.stdout.split(b"\0") if name})
    files = []
    for directory, dirs, names in os.walk(ROOT, followlinks=False):
        dirs[:] = [name for name in dirs if name not in SKIP_DIRS]
        for name in names:
            if not name.endswith((".pyc", ".pyo")):
                files.append((Path(directory) / name).relative_to(ROOT))
    return sorted(files)

def main() -> int:
    findings: list[tuple[str, int, str]] = []
    try:
        files = publishable_files()
        manifest = ROOT / "shared_release_manifest.txt"
        expected = set(manifest.read_text(encoding="utf-8").splitlines())
    except (OSError, subprocess.SubprocessError):
        print("scan configuration unavailable")
        return 1
    actual = {path.as_posix() for path in files if path.name != manifest.name}
    findings.extend((name, 0, "unlisted_file") for name in sorted(actual - expected))
    findings.extend((name, 0, "manifest_missing_file") for name in sorted(expected - actual))
    for relative in files:
        path = ROOT / relative
        label = relative.as_posix()
        try:
            path.resolve().relative_to(ROOT)
        except ValueError:
            findings.append((label, 0, "path_escape"))
            continue
        ancestors = [path, *list(path.parents)[:len(relative.parts) - 1]]
        if any(item.is_symlink() or getattr(item, "is_junction", lambda: False)() for item in ancestors):
            findings.append((label, 0, "linked_path"))
            continue
        if is_forbidden_path(relative):
            findings.append((label, 0, "forbidden_file"))
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES and path.name != ".gitignore":
            continue
        try:
            for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
                findings.extend((label, number, category) for category in sorted(line_categories(line)))
        except (OSError, UnicodeError):
            findings.append((label, 0, "unreadable_text"))
    for label, number, category in findings:
        print(f"{label}:{number}:{category}")
    if not findings:
        print("privacy scan: clean")
    return int(bool(findings))

if __name__ == "__main__":
    raise SystemExit(main())

