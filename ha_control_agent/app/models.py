"""Pydantic models for API payloads."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class AuthTokenRequest(BaseModel):
    long_lived_token: str = Field(min_length=10)
    session_ttl_seconds: int | None = Field(default=None, ge=60, le=604800)


class AuthTokenResponse(BaseModel):
    access_token: str
    expires_at: str
    token_type: str = "bearer"


class ExecRequest(BaseModel):
    cmd: list[str] | str
    timeout_s: int = Field(default=120, ge=1, le=3600)
    host_namespace: bool = True
    shell: bool = False
    stdin: str | None = None
    cwd: str | None = None


class ExecResponse(BaseModel):
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int


class FSWriteRequest(BaseModel):
    path: str
    content: str
    mode: str | None = None
    create_dirs: bool = True
    host_namespace: bool = True


class FSMoveRequest(BaseModel):
    src: str
    dst: str
    host_namespace: bool = True


class FSDeleteRequest(BaseModel):
    path: str
    recursive: bool = False
    host_namespace: bool = True


class CoreWSRequest(BaseModel):
    # Preferred envelope shape.
    type: str | None = None
    payload: dict[str, Any] | None = None
    id: int | None = None

    # Backward-compatible shape.
    message: dict[str, Any] | None = None

    timeout_s: int = Field(default=30, ge=1, le=300)


class ProxyBodyRequest(BaseModel):
    method: str = "GET"
    query: dict[str, str] | None = None
    body: dict[str, Any] | list[Any] | str | None = None
    headers: dict[str, str] | None = None


class AuditMetadata(BaseModel):
    action: str
    target: str | None = None
    details: dict[str, Any] | None = None


class CapabilityFeatures(BaseModel):
    filesystem: bool
    host_exec: bool
    core_rest_proxy: bool
    core_ws_bridge: bool
    supervisor_proxy: bool
    retries: bool
    dry_run_support: bool
    web_console: bool = True
    codex_cli: bool = False
    cli_persistence: bool = False
    cli_bootstrap: bool = False


class CapabilityModel(BaseModel):
    agent_version: str
    api_version: str
    supervisor_token_available: bool
    trusted_cidrs: list[str]
    features: CapabilityFeatures


class AuditEntry(BaseModel):
    ts: datetime
    actor: str
    source_ip: str
    action: str
    target: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    success: bool
