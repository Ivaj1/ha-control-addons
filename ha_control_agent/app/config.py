"""Runtime configuration for HA Control Agent."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

OPTIONS_PATH = Path("/data/options.json")
DEFAULT_TRUSTED_CIDRS = [
    "127.0.0.1/32",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
]


def _to_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _to_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _read_options() -> dict[str, Any]:
    if not OPTIONS_PATH.exists():
        return {}

    try:
        return json.loads(OPTIONS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


@dataclass(slots=True, frozen=True)
class Settings:
    api_port: int
    supervisor_token: str
    trusted_cidrs: list[str]
    session_ttl_seconds: int
    allow_unverified_bootstrap: bool
    unsafe_allow_exec: bool
    unsafe_allow_special_paths: bool
    audit_log_path: Path
    backup_root: Path

    @classmethod
    def load(cls) -> "Settings":
        options = _read_options()

        trusted_cidrs = options.get("trusted_cidrs")
        if not isinstance(trusted_cidrs, list) or not trusted_cidrs:
            trusted_cidrs = DEFAULT_TRUSTED_CIDRS

        return cls(
            api_port=_to_int(os.getenv("HACTRL_PORT", options.get("port", 9123)), 9123),
            supervisor_token=os.getenv("SUPERVISOR_TOKEN", ""),
            trusted_cidrs=[str(c) for c in trusted_cidrs],
            session_ttl_seconds=_to_int(
                os.getenv("HACTRL_SESSION_TTL", options.get("session_ttl_seconds", 43200)),
                43200,
            ),
            allow_unverified_bootstrap=_to_bool(
                os.getenv("HACTRL_ALLOW_UNVERIFIED_BOOTSTRAP", options.get("allow_unverified_bootstrap", False)),
                False,
            ),
            unsafe_allow_exec=_to_bool(
                os.getenv("HACTRL_UNSAFE_ALLOW_EXEC", options.get("unsafe_allow_exec", False)),
                False,
            ),
            unsafe_allow_special_paths=_to_bool(
                os.getenv("HACTRL_UNSAFE_ALLOW_SPECIAL_PATHS", options.get("unsafe_allow_special_paths", False)),
                False,
            ),
            audit_log_path=Path(os.getenv("HACTRL_AUDIT_LOG", "/share/ha-control/audit.log")),
            backup_root=Path(os.getenv("HACTRL_BACKUP_ROOT", "/backup/ha-control")),
        )


settings = Settings.load()
