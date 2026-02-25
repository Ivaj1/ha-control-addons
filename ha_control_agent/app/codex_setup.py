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
            "# AGENTS.md\n\n"
            "You are working inside a Home Assistant environment.\n\n"
            "Rules:\n"
            "- Always check and use skills from `$CODEX_HOME/skills` first.\n"
            "- Prefer the `haos-full-control` skill for Home Assistant OS tasks.\n"
            "- Assume the active project context is Home Assistant and related add-ons.\n"
            "- Keep changes safe, reversible, and auditable.\n",
            encoding="utf-8",
        )
    agent_file = codex_home / "agent.md"
    if not agent_file.exists():
        agent_file.write_text(agents_file.read_text(encoding="utf-8"), encoding="utf-8")

    return {
        "codex_home": str(codex_home),
        "skills_dir": str(skills_dir),
        "default_skills_copied": default_copied,
    }
