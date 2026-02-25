"""Runtime configuration for HA Control Agent."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

OPTIONS_PATH = Path("/data/options.json")
S6_ENV_DIR = Path("/run/s6/container_environment")
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


def _read_s6_env(name: str) -> str:
    path = S6_ENV_DIR / name
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8").strip("\x00\r\n ")
    except OSError:
        return ""


def _read_supervisor_token(options: dict[str, Any]) -> str:
    token_sources = [
        os.getenv("HACTRL_SUPERVISOR_TOKEN"),
        options.get("supervisor_token"),
        os.getenv("SUPERVISOR_TOKEN"),
        os.getenv("HASSIO_TOKEN"),
        _read_s6_env("SUPERVISOR_TOKEN"),
        _read_s6_env("HASSIO_TOKEN"),
    ]
    for token in token_sources:
        if token:
            normalized = str(token).strip()
            if normalized:
                return normalized
    return ""


@dataclass(slots=True, frozen=True)
class Settings:
    api_port: int
    supervisor_token: str
    trusted_cidrs: list[str]
    session_ttl_seconds: int
    allow_unverified_bootstrap: bool
    unsafe_allow_exec: bool
    unsafe_allow_special_paths: bool
    openai_api_key: str
    codex_home: str
    webdav_enabled: bool
    webdav_username: str
    webdav_password: str
    webdav_root: str
    webdav_host_namespace: bool
    webdav_read_only: bool
    webdav_https_enabled: bool
    webdav_https_port: int
    webdav_https_cert: str
    webdav_https_key: str
    console_shell: str
    console_cwd: str
    console_host_namespace: bool
    audit_log_path: Path
    backup_root: Path

    @classmethod
    def load(cls) -> "Settings":
        options = _read_options()

        trusted_cidrs = options.get("trusted_cidrs")
        if not isinstance(trusted_cidrs, list) or not trusted_cidrs:
            trusted_cidrs = DEFAULT_TRUSTED_CIDRS

        supervisor_token = _read_supervisor_token(options)

        return cls(
            api_port=_to_int(os.getenv("HACTRL_PORT", options.get("port", 9123)), 9123),
            supervisor_token=supervisor_token,
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
            openai_api_key=str(os.getenv("OPENAI_API_KEY", options.get("openai_api_key", ""))).strip(),
            codex_home=str(os.getenv("CODEX_HOME", options.get("codex_home", "/share/codex"))).strip(),
            webdav_enabled=_to_bool(os.getenv("HACTRL_WEBDAV_ENABLED", options.get("webdav_enabled", True)), True),
            webdav_username=str(os.getenv("HACTRL_WEBDAV_USERNAME", options.get("webdav_username", "admin"))).strip(),
            webdav_password=str(os.getenv("HACTRL_WEBDAV_PASSWORD", options.get("webdav_password", ""))).strip(),
            webdav_root=str(os.getenv("HACTRL_WEBDAV_ROOT", options.get("webdav_root", "/"))).strip() or "/",
            webdav_host_namespace=_to_bool(
                os.getenv("HACTRL_WEBDAV_HOST_NAMESPACE", options.get("webdav_host_namespace", True)),
                True,
            ),
            webdav_read_only=_to_bool(
                os.getenv("HACTRL_WEBDAV_READ_ONLY", options.get("webdav_read_only", False)),
                False,
            ),
            webdav_https_enabled=_to_bool(
                os.getenv("HACTRL_WEBDAV_HTTPS_ENABLED", options.get("webdav_https_enabled", False)),
                False,
            ),
            webdav_https_port=_to_int(
                os.getenv("HACTRL_WEBDAV_HTTPS_PORT", options.get("webdav_https_port", 9443)),
                9443,
            ),
            webdav_https_cert=str(
                os.getenv("HACTRL_WEBDAV_HTTPS_CERT", options.get("webdav_https_cert", "/ssl/fullchain.pem"))
            ).strip(),
            webdav_https_key=str(
                os.getenv("HACTRL_WEBDAV_HTTPS_KEY", options.get("webdav_https_key", "/ssl/privkey.pem"))
            ).strip(),
            console_shell=str(os.getenv("HACTRL_CONSOLE_SHELL", options.get("console_shell", "/bin/sh"))).strip(),
            console_cwd=str(os.getenv("HACTRL_CONSOLE_CWD", options.get("console_cwd", "/homeassistant"))).strip(),
            console_host_namespace=_to_bool(
                os.getenv("HACTRL_CONSOLE_HOST_NAMESPACE", options.get("console_host_namespace", False)),
                False,
            ),
            audit_log_path=Path(os.getenv("HACTRL_AUDIT_LOG", "/share/ha-control/audit.log")),
            backup_root=Path(os.getenv("HACTRL_BACKUP_ROOT", "/backup/ha-control")),
        )


settings = Settings.load()
