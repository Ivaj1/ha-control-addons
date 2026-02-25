"""FastAPI application for HA Control Agent."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import fcntl
import json
import os
from pathlib import Path
import pty
import signal
import shutil
import struct
import termios
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect, status
from fastapi.responses import HTMLResponse
import httpx

from .audit import write_audit_entry
from .codex_setup import ensure_codex_home
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

AGENT_VERSION = "0.2.5"

app = FastAPI(title="HA Control Agent", version=AGENT_VERSION)
codex_runtime: dict[str, str | bool] = {}


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
    :root {
      --bg: #0b1016;
      --panel: #121a24;
      --line: #223041;
      --text: #d8e0ea;
      --muted: #8ea0b4;
      --accent: #2f81f7;
      --accent-2: #1f6feb;
    }
    * { box-sizing: border-box; }
    html, body { height: 100%; margin: 0; background: radial-gradient(1200px 700px at 10% -20%, #1a2a3d 0, var(--bg) 50%); color: var(--text); font-family: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace; }
    .app { height: 100%; display: grid; grid-template-rows: auto auto 1fr; }
    .topbar { border-bottom: 1px solid var(--line); background: linear-gradient(180deg, #182333 0%, #121a24 100%); padding: 10px 12px; display: grid; grid-template-columns: 1fr auto; gap: 10px; align-items: center; }
    .title { font-size: 14px; font-weight: 700; letter-spacing: 0.2px; }
    .title small { color: var(--muted); font-weight: 500; margin-left: 8px; }
    .status { font-size: 12px; color: #7ee787; border: 1px solid #294733; background: #0f1d14; padding: 4px 8px; border-radius: 999px; }
    .toolbar { border-bottom: 1px solid var(--line); background: #101823; padding: 8px 10px; display: grid; gap: 8px; grid-template-columns: 1fr auto; align-items: center; }
    .left, .right { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
    .token { width: min(520px, 100%); background: #0c131d; border: 1px solid #2e3b4d; color: var(--text); padding: 7px 10px; border-radius: 6px; }
    .btn { border: 1px solid #35465c; background: #172233; color: var(--text); border-radius: 6px; padding: 7px 10px; cursor: pointer; font: inherit; font-size: 12px; }
    .btn:hover { border-color: #4c6380; background: #1b2a3e; }
    .btn.primary { border-color: #2f6ed3; background: linear-gradient(180deg, var(--accent), var(--accent-2)); color: #fff; }
    .hint { font-size: 11px; color: var(--muted); }
    .terminal-wrap { padding: 10px; height: 100%; min-height: 0; }
    #term { width: 100%; height: 100%; border: 1px solid var(--line); border-radius: 8px; overflow: hidden; box-shadow: 0 10px 24px rgba(0,0,0,0.35); }
  </style>
</head>
<body>
  <div class="app">
    <div class="topbar">
      <div class="title">HA Control Console <small>Modern terminal</small></div>
      <div id="status" class="status">Disconnected</div>
    </div>
    <div class="toolbar">
      <div class="left">
        <input id="token" class="token" placeholder="Session token (optional in Ingress)" />
        <button id="connect" class="btn primary">Connect</button>
        <button id="disconnect" class="btn">Disconnect</button>
        <button id="interrupt" class="btn">Ctrl+C</button>
        <button id="clear" class="btn">Clear</button>
      </div>
      <div class="right">
        <button id="copy" class="btn">Copy</button>
        <button id="paste" class="btn">Paste</button>
        <button id="fontDec" class="btn">A-</button>
        <button id="fontInc" class="btn">A+</button>
        <span class="hint">Shortcuts: Ctrl+Shift+C / Ctrl+Shift+V</span>
      </div>
    </div>
    <div class="terminal-wrap">
      <div id="term"></div>
    </div>
  </div>

  <script src="https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.min.js"></script>
  <script>
    const statusEl = document.getElementById("status");
    const tokenInput = document.getElementById("token");
    const connectBtn = document.getElementById("connect");
    const disconnectBtn = document.getElementById("disconnect");
    const interruptBtn = document.getElementById("interrupt");
    const clearBtn = document.getElementById("clear");
    const copyBtn = document.getElementById("copy");
    const pasteBtn = document.getElementById("paste");
    const fontDecBtn = document.getElementById("fontDec");
    const fontIncBtn = document.getElementById("fontInc");

    let ws = null;
    let fontSize = Number(localStorage.getItem("ha_console_font") || "14");

    const term = new Terminal({
      cursorBlink: true,
      convertEol: true,
      fontSize,
      fontFamily: '"JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace',
      scrollback: 10000,
      rightClickSelectsWord: true,
      theme: {
        background: "#0b1016",
        foreground: "#d8e0ea",
        cursor: "#7ee787",
        selectionBackground: "rgba(63, 131, 248, 0.35)"
      }
    });
    const fitAddon = new FitAddon.FitAddon();
    term.loadAddon(fitAddon);
    term.open(document.getElementById("term"));
    term.writeln("HA Control Agent console ready.");

    tokenInput.value = localStorage.getItem("ha_control_session") || "";

    function setStatus(message, connected) {
      statusEl.textContent = message;
      statusEl.style.color = connected ? "#7ee787" : "#f0c674";
      statusEl.style.borderColor = connected ? "#294733" : "#5a471f";
      statusEl.style.background = connected ? "#0f1d14" : "#22180f";
    }

    function wsPath() {
      const path = window.location.pathname.replace(/\\/$/, "");
      const base = path.endsWith("/console") ? path.slice(0, -8) : path;
      return (base === "" ? "" : base) + "/console/ws";
    }

    function send(payload) {
      if (!ws || ws.readyState !== 1) return;
      ws.send(JSON.stringify(payload));
    }

    function sendResize() {
      fitAddon.fit();
      send({ type: "resize", cols: term.cols, rows: term.rows });
    }

    function connect() {
      if (ws && (ws.readyState === 0 || ws.readyState === 1)) return;
      const token = tokenInput.value.trim();
      if (token) localStorage.setItem("ha_control_session", token);
      const query = token ? ("?token=" + encodeURIComponent(token)) : "";
      const proto = window.location.protocol === "https:" ? "wss://" : "ws://";
      ws = new WebSocket(proto + window.location.host + wsPath() + query);

      ws.onopen = () => {
        setStatus("Connected", true);
        term.writeln("\\r\\n[connected]");
        sendResize();
      };
      ws.onclose = () => {
        setStatus("Disconnected", false);
        term.writeln("\\r\\n[disconnected]");
      };
      ws.onerror = () => {
        setStatus("Connection error", false);
        term.writeln("\\r\\n[connection error]");
      };
      ws.onmessage = (event) => {
        if (typeof event.data === "string") term.write(event.data);
      };
    }

    function disconnect() {
      if (ws) ws.close();
      ws = null;
    }

    async function copySelection() {
      const text = term.getSelection();
      if (!text) return;
      try { await navigator.clipboard.writeText(text); } catch (_) {}
    }

    async function pasteClipboard() {
      try {
        const text = await navigator.clipboard.readText();
        if (!text) return;
        send({ type: "input", data: text });
      } catch (_) {}
    }

    function adjustFont(delta) {
      fontSize = Math.max(10, Math.min(26, fontSize + delta));
      localStorage.setItem("ha_console_font", String(fontSize));
      term.options.fontSize = fontSize;
      sendResize();
    }

    connectBtn.addEventListener("click", connect);
    disconnectBtn.addEventListener("click", disconnect);
    interruptBtn.addEventListener("click", () => send({ type: "input", data: "\\u0003" }));
    clearBtn.addEventListener("click", () => term.clear());
    copyBtn.addEventListener("click", copySelection);
    pasteBtn.addEventListener("click", pasteClipboard);
    fontDecBtn.addEventListener("click", () => adjustFont(-1));
    fontIncBtn.addEventListener("click", () => adjustFont(1));

    tokenInput.addEventListener("keydown", (event) => {
      if (event.key === "Enter") connect();
    });

    term.onData((data) => send({ type: "input", data }));
    term.attachCustomKeyEventHandler((event) => {
      if (!event.ctrlKey || !event.shiftKey) return true;
      if (event.key.toLowerCase() === "c") { copySelection(); return false; }
      if (event.key.toLowerCase() === "v") { pasteClipboard(); return false; }
      return true;
    });
    term.textarea?.addEventListener("paste", (event) => {
      const text = event.clipboardData?.getData("text") || "";
      if (text) send({ type: "input", data: text });
      event.preventDefault();
    });
    window.addEventListener("resize", sendResize);

    setStatus("Disconnected", false);
    sendResize();
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
        ]
    return [shell]


def _set_pty_size(fd: int, cols: int, rows: int) -> None:
    winsize = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)


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
    return {
        "status": "ok",
        "time": datetime.now(tz=UTC).isoformat(),
        "codex_home": str(codex_runtime.get("codex_home", settings.codex_home)),
    }


@app.on_event("startup")
async def _startup_tasks() -> None:
    global codex_runtime
    codex_runtime = ensure_codex_home()


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
    env["CODEX_HOME"] = str(codex_runtime.get("codex_home", settings.codex_home))
    master_fd, slave_fd = pty.openpty()
    _set_pty_size(master_fd, 120, 40)
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
                payload: dict[str, Any] | None = None
                try:
                    parsed = json.loads(data)
                    if isinstance(parsed, dict):
                        payload = parsed
                except json.JSONDecodeError:
                    payload = None

                if payload and payload.get("type") == "resize":
                    cols = int(payload.get("cols", 120))
                    rows = int(payload.get("rows", 40))
                    cols = max(20, min(cols, 500))
                    rows = max(8, min(rows, 200))
                    await asyncio.to_thread(_set_pty_size, master_fd, cols, rows)
                    if process.pid:
                        try:
                            os.kill(process.pid, signal.SIGWINCH)
                        except ProcessLookupError:
                            pass
                    continue

                text = payload.get("data") if payload and payload.get("type") == "input" else data
                await asyncio.to_thread(os.write, master_fd, str(text).encode("utf-8"))
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
