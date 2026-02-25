"""Audit logging utilities."""

from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any

from .config import settings
from .models import AuditEntry


def write_audit_entry(
    *,
    actor: str,
    source_ip: str,
    action: str,
    target: str | None = None,
    details: dict[str, Any] | None = None,
    success: bool = True,
) -> None:
    payload = AuditEntry(
        ts=datetime.now(tz=UTC),
        actor=actor,
        source_ip=source_ip,
        action=action,
        target=target,
        details=details or {},
        success=success,
    ).model_dump(mode="json")

    log_path: Path = settings.audit_log_path
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=True) + "\n")
