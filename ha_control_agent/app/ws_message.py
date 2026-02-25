"""Core WebSocket message normalization helpers."""

from __future__ import annotations

from typing import Any
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import CoreWSRequest


def build_ws_message(body: "CoreWSRequest | Any") -> dict[str, Any]:
    if body.message is not None:
        message = dict(body.message)
        if "id" not in message:
            message["id"] = body.id or 1
        return message

    if not body.type:
        raise ValueError("core/ws requires either 'message' or ('type' + optional 'payload')")

    message: dict[str, Any] = {"id": body.id or 1, "type": body.type}
    payload = body.payload or {}
    if "id" in payload or "type" in payload:
        raise ValueError("payload cannot contain 'id' or 'type'")
    message.update(payload)
    return message
