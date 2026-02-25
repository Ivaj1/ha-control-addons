"""Bridge Home Assistant Core WebSocket commands."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import websockets

from .config import settings


class WebSocketBridgeError(RuntimeError):
    """Raised when WS bridge cannot complete an operation."""


async def send_core_ws(message: dict[str, Any], timeout_s: int) -> dict[str, Any]:
    payload = dict(message)
    if "id" not in payload:
        payload["id"] = 1

    ws_url = "ws://supervisor/core/websocket"

    try:
        async with websockets.connect(ws_url, open_timeout=10, close_timeout=5, max_size=64 * 1024 * 1024) as ws:
            # auth_required
            greeting = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout_s))
            if greeting.get("type") not in {"auth_required", "auth_ok"}:
                raise WebSocketBridgeError(f"Unexpected greeting: {greeting}")

            if greeting.get("type") == "auth_required":
                await ws.send(json.dumps({"type": "auth", "access_token": settings.supervisor_token}))
                auth = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout_s))
                if auth.get("type") != "auth_ok":
                    raise WebSocketBridgeError(f"Authentication failed: {auth}")

            await ws.send(json.dumps(payload))

            while True:
                frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout_s))
                if frame.get("id") == payload["id"]:
                    return frame

    except TimeoutError as err:
        raise WebSocketBridgeError("Timed out waiting for WebSocket response") from err
    except websockets.WebSocketException as err:
        raise WebSocketBridgeError(str(err)) from err
