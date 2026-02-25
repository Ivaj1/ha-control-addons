"""FastAPI application for HA Control Agent."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import pty
import shutil
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect, status
from fastapi.responses import HTMLResponse
import httpx

from .audit import write_audit_entry
from .config import settings
from .filesystem import delete_path, move_path, read_file, tree, write_file
from .host_exec import run_command
from .models import (
    AuthTokenRequest,
    AuthTokenResponse,
    CapabilityModel,
    CoreWSRequest,
    ExecRequest,
    FSDeleteRequest,
    FSMoveRequest,
    FSWriteRequest,
)
from .proxy import request_core_rest, request_supervisor
from .security import SessionInfo, is_trusted_ip, require_session, require_trusted_network, session_store, validate_bearer_token
from .ws_message import build_ws_message
from .ws_bridge import WebSocketBridgeError, send_core_ws

AGENT_VERSION = "0.2.2"

app = FastAPI(title="HA Control Agent", version=AGENT_VERSION)


def _source_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _require_supervisor_token() -> None:
    if settings.supervisor_token:
        return
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Supervisor token is unavailable in add-on runtime (SUPERVISOR_TOKEN/HASSIO_TOKEN missing)",
    )


CONSOLE_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>HA Control Console</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.min.css" />
  <style>
    html, body { height: 100%; margin: 0; background: #0f1419; color: #d5dde5; font-family: ui-monospace, monospace; }
    .bar { padding: 8px 12px; background: #182029; border-bottom: 1px solid #2b3540; display: flex; gap: 8px; align-items: center; }
    .bar input { flex: 1; min-width: 180px; background: #0f1419; color: #d5dde5; border: 1px solid #394654; padding: 6px 8px; border-radius: 4px; }
    .bar button { background: #1f6feb; border: 0; color: white; border-radius: 4px; padding: 7px 10px; cursor: pointer; }
    #term { height: calc(100% - 50px); width: 100%; }
  </style>
</head>
<body>
  <div class="bar">
    <strong>HA Control Console</strong>
    <input id="token" placeholder="Session token (optional in Ingress)" />
    <button id="connect">Connect</button>
  </div>
  <div id="term"></div>
  <script src="https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.min.js"></script>
  <script>
    const term = new Terminal({cursorBlink: true, fontSize: 14, theme: {background: "#0f1419"}});
    term.open(document.getElementById("term"));
    term.writeln("HA Control Agent console");
    const tokenInput = document.getElementById("token");
    tokenInput.value = localStorage.getItem("ha_control_session") || "";
    let ws = null;

    function connect() {
      if (ws && ws.readyState <= 1) return;
      const path = window.location.pathname.replace(/\\/$/, "");
      const base = path.endsWith("/console") ? path.slice(0, -8) : path;
      const wsPath = (base === "" ? "" : base) + "/console/ws";
      const token = tokenInput.value.trim();
      if (token) localStorage.setItem("ha_control_session", token);
      const query = token ? ("?token=" + encodeURIComponent(token)) : "";
      const proto = window.location.protocol === "https:" ? "wss://" : "ws://";
      ws = new WebSocket(proto + window.location.host + wsPath + query);
      ws.onopen = () => term.writeln("\\r\\n[connected]");
      ws.onclose = () => term.writeln("\\r\\n[disconnected]");
      ws.onerror = () => term.writeln("\\r\\n[connection error]");
      ws.onmessage = (event) => {
        if (typeof event.data === "string") term.write(event.data);
      };
    }

    document.getElementById("connect").addEventListener("click", connect);
    term.onData(data => {
      if (!ws || ws.readyState !== 1) return;
      ws.send(data);
    });
  </script>
</body>
</html>"""


def _is_ingress(headers: dict[str, str]) -> bool:
    lower = {k.lower(): v for k, v in headers.items()}
    return bool(lower.get("x-ingress-path") or lower.get("x-hassio-key"))


def _console_subject_from_auth(
    authorization: str | None,
    token: str | None,
    headers: dict[str, str],
) -> str:
    info = validate_bearer_token(authorization)
    if info is None and token:
        info = session_store.validate(token.strip())
    if info:
        return info.subject
    if _is_ingress(headers):
        return "ingress"
    return "trusted_lan"


def _console_shell_cmd() -> list[str]:
    shell = settings.console_shell or "/bin/sh"
    shell = shell if shell.startswith("/") else f"/bin/{shell}"
    if settings.console_host_namespace and Path("/proc/1/ns/mnt").exists() and shutil.which("nsenter"):
        return [
            "nsenter",
            "--target", "1",
            "--mount",
            "--uts",
            "--ipc",
            "--net",
            "--pid",
            shell,
            "-l",
        ]
    return [shell, "-l"]


async def _verify_long_lived_token(token: str) -> tuple[bool, str]:
    if settings.allow_unverified_bootstrap:
        return True, "Validation bypassed by settings"

    urls = [
        "http://homeassistant:8123/api/",
        "http://127.0.0.1:8123/api/",
    ]

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    errors: list[str] = []
    async with httpx.AsyncClient(timeout=8) as client:
        for url in urls:
            try:
                resp = await client.get(url, headers=headers)
            except httpx.HTTPError as err:
                errors.append(f"{url}: {err}")
                continue

            if resp.status_code == 200:
                return True, f"Validated against {url}"
            if resp.status_code in {401, 403}:
                errors.append(f"{url}: unauthorized")
                continue
            errors.append(f"{url}: status {resp.status_code}")

    return False, "; ".join(errors) if errors else "Token validation failed"


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "time": datetime.now(tz=UTC).isoformat()}


@app.get("/", response_class=HTMLResponse)
@app.get("/console", response_class=HTMLResponse)
async def console_page(
    request: Request,
    authorization: str | None = Header(default=None),
    token: str | None = Query(default=None),
) -> HTMLResponse:
    require_trusted_network(request)
    _console_subject_from_auth(
        authorization=authorization,
        token=token,
        headers=dict(request.headers),
    )
    return HTMLResponse(content=CONSOLE_HTML)


@app.websocket("/console/ws")
async def console_ws(websocket: WebSocket) -> None:
    client_ip = websocket.client.host if websocket.client else "127.0.0.1"
    if not is_trusted_ip(client_ip):
        await websocket.close(code=1008, reason="Untrusted network")
        return

    try:
        subject = _console_subject_from_auth(
            authorization=websocket.headers.get("authorization"),
            token=websocket.query_params.get("token"),
            headers=dict(websocket.headers),
        )
    except HTTPException:
        await websocket.close(code=1008, reason="Unauthorized")
        return

    await websocket.accept()

    shell_cmd = _console_shell_cmd()
    cwd = settings.console_cwd if Path(settings.console_cwd).exists() else "/"
    env = os.environ.copy()
    if settings.openai_api_key:
        env["OPENAI_API_KEY"] = settings.openai_api_key
    master_fd, slave_fd = pty.openpty()
    process = await asyncio.create_subprocess_exec(
        *shell_cmd,
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        cwd=cwd,
        env=env,
    )
    os.close(slave_fd)

    async def _pty_to_ws() -> None:
        try:
            while True:
                chunk = await asyncio.to_thread(os.read, master_fd, 4096)
                if not chunk:
                    break
                await websocket.send_text(chunk.decode("utf-8", errors="replace"))
        except Exception:
            pass

    async def _ws_to_pty() -> None:
        try:
            while True:
                data = await websocket.receive_text()
                await asyncio.to_thread(os.write, master_fd, data.encode("utf-8"))
        except WebSocketDisconnect:
            pass
        except Exception:
            pass

    write_audit_entry(
        actor=subject,
        source_ip=client_ip,
        action="console.open",
        details={"shell": settings.console_shell, "cwd": cwd, "cmd": shell_cmd},
    )

    try:
        await asyncio.wait(
            [
                asyncio.create_task(_pty_to_ws()),
                asyncio.create_task(_ws_to_pty()),
            ],
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        await process.wait()
        try:
            os.close(master_fd)
        except OSError:
            pass


@app.post("/v1/auth/token", response_model=AuthTokenResponse)
async def auth_token(request: Request, body: AuthTokenRequest) -> AuthTokenResponse:
    require_trusted_network(request)

    valid, message = await _verify_long_lived_token(body.long_lived_token)
    if not valid:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=message)

    ttl = body.session_ttl_seconds or settings.session_ttl_seconds
    token, expires_at = session_store.create("llat", ttl)

    write_audit_entry(
        actor="bootstrap",
        source_ip=_source_ip(request),
        action="auth.token.create",
        details={"ttl": ttl, "message": message},
    )

    return AuthTokenResponse(access_token=token, expires_at=expires_at.isoformat())


@app.get("/v1/auth/me")
async def auth_me(session: SessionInfo = Depends(require_session)) -> dict[str, str]:
    return {
        "subject": session.subject,
        "expires_at": session.expires_at.isoformat(),
    }


@app.get("/v1/capabilities")
async def capabilities(
    session: SessionInfo = Depends(require_session),
) -> CapabilityModel:
    return CapabilityModel.model_validate(
        {
        "agent_version": AGENT_VERSION,
        "api_version": "v1",
        "supervisor_token_available": bool(settings.supervisor_token),
        "trusted_cidrs": settings.trusted_cidrs,
        "features": {
            "filesystem": True,
            "host_exec": True,
            "core_rest_proxy": True,
            "core_ws_bridge": True,
            "supervisor_proxy": True,
            "retries": True,
            "dry_run_support": True,
            "web_console": True,
            "codex_cli": bool(shutil.which("codex")),
        },
    }
    )


@app.get("/v1/fs/tree")
async def fs_tree(
    request: Request,
    path: str = Query(..., description="Absolute path"),
    max_depth: int = Query(2, ge=0, le=32),
    host_namespace: bool = Query(True),
    session: SessionInfo = Depends(require_session),
) -> dict[str, Any]:
    result = tree(path=path, max_depth=max_depth, host_namespace=host_namespace)
    return result


@app.get("/v1/fs/read")
async def fs_read(
    request: Request,
    path: str = Query(..., description="Absolute path"),
    host_namespace: bool = Query(True),
    session: SessionInfo = Depends(require_session),
) -> dict[str, Any]:
    return read_file(path=path, host_namespace=host_namespace)


@app.put("/v1/fs/write")
async def fs_write(
    request: Request,
    body: FSWriteRequest,
    session: SessionInfo = Depends(require_session),
) -> dict[str, Any]:
    result = write_file(
        path=body.path,
        content=body.content,
        mode=body.mode,
        create_dirs=body.create_dirs,
        host_namespace=body.host_namespace,
    )
    write_audit_entry(
        actor=session.subject,
        source_ip=_source_ip(request),
        action="fs.write",
        target=body.path,
        details={
            "host_namespace": body.host_namespace,
            "bytes": result["bytes_written"],
            "before_hash": result.get("before_hash"),
            "after_hash": result.get("after_hash"),
            "result": result.get("result"),
        },
        success=result.get("result") in {"written", "no_change"},
    )
    return result


@app.post("/v1/fs/move")
async def fs_move(
    request: Request,
    body: FSMoveRequest,
    session: SessionInfo = Depends(require_session),
) -> dict[str, Any]:
    result = move_path(src=body.src, dst=body.dst, host_namespace=body.host_namespace)
    write_audit_entry(
        actor=session.subject,
        source_ip=_source_ip(request),
        action="fs.move",
        target=f"{body.src} -> {body.dst}",
        details={
            "host_namespace": body.host_namespace,
            "src_before_hash": result.get("src_before_hash"),
            "dst_after_hash": result.get("dst_after_hash"),
            "result": result.get("result"),
        },
    )
    return result


@app.delete("/v1/fs/delete")
async def fs_delete(
    request: Request,
    body: FSDeleteRequest,
    session: SessionInfo = Depends(require_session),
) -> dict[str, Any]:
    result = delete_path(
        path=body.path,
        recursive=body.recursive,
        host_namespace=body.host_namespace,
    )
    write_audit_entry(
        actor=session.subject,
        source_ip=_source_ip(request),
        action="fs.delete",
        target=body.path,
        details={
            "recursive": body.recursive,
            "host_namespace": body.host_namespace,
            "before_hash": result.get("before_hash"),
            "result": result.get("result"),
        },
    )
    return result


@app.post("/v1/exec")
async def exec_command(
    request: Request,
    body: ExecRequest,
    session: SessionInfo = Depends(require_session),
) -> dict[str, Any]:
    result = run_command(
        cmd=body.cmd,
        timeout_s=body.timeout_s,
        host_namespace=body.host_namespace,
        shell=body.shell,
        stdin=body.stdin,
        cwd=body.cwd,
    )

    write_audit_entry(
        actor=session.subject,
        source_ip=_source_ip(request),
        action="exec",
        target=str(body.cmd),
        details={
            "host_namespace": body.host_namespace,
            "shell": body.shell,
            "cwd": body.cwd,
            "exit_code": result["exit_code"],
        },
        success=result["exit_code"] == 0,
    )

    return result


@app.post("/v1/core/ws")
async def core_ws(
    request: Request,
    body: CoreWSRequest,
    session: SessionInfo = Depends(require_session),
) -> dict[str, Any]:
    _require_supervisor_token()
    message = _build_ws_message(body)
    try:
        result = await send_core_ws(message, timeout_s=body.timeout_s)
    except WebSocketBridgeError as err:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(err)) from err

    write_audit_entry(
        actor=session.subject,
        source_ip=_source_ip(request),
        action="core.ws",
        target=message.get("type"),
        details={"id": message.get("id"), "payload_keys": list(message.keys())},
    )

    return result


def _response_from_upstream(resp: httpx.Response) -> Response:
    media_type = resp.headers.get("content-type", "application/json")
    response = Response(content=resp.content, status_code=resp.status_code, media_type=media_type)
    for header in ("cache-control", "mcp-session-id"):
        if header in resp.headers:
            response.headers[header] = resp.headers[header]
    return response


async def _proxy_body(request: Request) -> Any:
    if request.method in {"GET", "DELETE"}:
        return None

    raw = await request.body()
    if not raw:
        return None

    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return raw.decode("utf-8", errors="replace")

    return raw


@app.api_route("/v1/supervisor", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
@app.api_route("/v1/supervisor/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy_supervisor(
    request: Request,
    path: str = "",
    session: SessionInfo = Depends(require_session),
) -> Response:
    _require_supervisor_token()
    body = await _proxy_body(request)
    headers = {key: value for key, value in request.headers.items() if key.lower() in {"content-type", "accept"}}
    resp = await request_supervisor(
        method=request.method,
        path=path,
        body=body,
        headers=headers,
        query=dict(request.query_params),
    )
    write_audit_entry(
        actor=session.subject,
        source_ip=_source_ip(request),
        action="supervisor.proxy",
        target=f"{request.method} /{path}",
        details={"status_code": resp.status_code},
        success=resp.status_code < 400,
    )
    return _response_from_upstream(resp)


@app.api_route("/v1/core/rest", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
@app.api_route("/v1/core/rest/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy_core_rest(
    request: Request,
    path: str = "",
    session: SessionInfo = Depends(require_session),
) -> Response:
    _require_supervisor_token()
    body = await _proxy_body(request)
    headers = {key: value for key, value in request.headers.items() if key.lower() in {"content-type", "accept"}}
    resp = await request_core_rest(
        method=request.method,
        path=path,
        body=body,
        headers=headers,
        query=dict(request.query_params),
    )
    write_audit_entry(
        actor=session.subject,
        source_ip=_source_ip(request),
        action="core.rest.proxy",
        target=f"{request.method} /{path}",
        details={"status_code": resp.status_code},
        success=resp.status_code < 400,
    )
    return _response_from_upstream(resp)


def _build_ws_message(body: CoreWSRequest) -> dict[str, Any]:
    try:
        return build_ws_message(body)
    except ValueError as err:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(err),
        ) from err
