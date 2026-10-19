from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "naver_blog_v3"
SETTINGS_EXAMPLE = APP / "config" / "settings.example.py"
SETTINGS = APP / "config" / "settings.py"

def main() -> None:
    if not SETTINGS_EXAMPLE.is_file():
        raise SystemExit("Neutral settings template is missing; setup stopped.")
    if SETTINGS.exists():
        print("Local settings already exist; left them unchanged.")
    else:
        SETTINGS.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SETTINGS_EXAMPLE, SETTINGS)
        print("Created local settings from the neutral example.")
    for directory in (
        ROOT / "profiles" / "public-share",
        ROOT / "profiles" / "public-share" / "config",
        ROOT / "profiles" / "public-share" / "logs",
        ROOT / "profiles" / "public-share" / "output",
        APP / ".local" / "genspark_profiles",
    ):
        directory.mkdir(parents=True, exist_ok=True)
    print("Created an empty, checkout-local public-share profile.")

if __name__ == "__main__":
    main()
