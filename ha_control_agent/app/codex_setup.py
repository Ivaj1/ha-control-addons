"""Codex runtime bootstrap for persistent skills/config in Home Assistant add-on."""

from __future__ import annotations

from pathlib import Path
import shutil

from .config import settings

DEFAULT_SKILLS_DIR = Path("/app/default_skills")


def ensure_codex_home() -> dict[str, str | bool]:
    codex_home = Path(settings.codex_home or "/share/codex")
    skills_dir = codex_home / "skills"
    agents_file = codex_home / "AGENTS.md"

    codex_home.mkdir(parents=True, exist_ok=True)
    skills_dir.mkdir(parents=True, exist_ok=True)

    default_copied = False
    if DEFAULT_SKILLS_DIR.exists():
        for item in DEFAULT_SKILLS_DIR.iterdir():
            if not item.is_dir():
                continue
            dest = skills_dir / item.name
            if dest.exists():
                continue
            shutil.copytree(item, dest)
            default_copied = True

    if not agents_file.exists():
        agents_file.write_text(
            "# AGENTS\n\n"
            "- Skills path: `$CODEX_HOME/skills`\n"
            "- This folder is persistent on Home Assistant (`/share/codex`).\n",
            encoding="utf-8",
        )

    return {
        "codex_home": str(codex_home),
        "skills_dir": str(skills_dir),
        "default_skills_copied": default_copied,
    }

