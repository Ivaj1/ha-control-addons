"""FastAPI application for HA Control Agent."""

from __future__ import annotations

import asyncio
import base64
from collections import deque
from datetime import UTC, datetime
from email.utils import formatdate
import fcntl
import json
import os
import posixpath
from pathlib import Path
import pty
import signal
import shutil
import struct
import termios
from typing import Any
from urllib.parse import quote, unquote, urlparse

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect, status
from fastapi.responses import HTMLResponse
import httpx

from .audit import write_audit_entry
from .codex_setup import ensure_codex_home
from .config import settings
from .filesystem import delete_path, move_path, read_file, tree, write_file
from .host_exec import run_command
from .host_exec import run_simple_host_command
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

AGENT_VERSION = "0.2.11"

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
  <title>cmd</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.min.css" />
  <style>
    html, body { height: 100%; margin: 0; background: #0c0c0c; overflow: hidden; }
    #term { width: 100vw; height: 100vh; }
    #term .xterm-viewport { scrollbar-width: none; background: #0c0c0c !important; }
    #term .xterm-viewport::-webkit-scrollbar { width: 0 !important; height: 0 !important; }
  </style>
</head>
<body>
  <div id="term"></div>

  <script src="https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.min.js"></script>
  <script>
    let ws = null;
    const sessionKey = "ha_console_client_id";
    let clientId = localStorage.getItem(sessionKey);
    if (!clientId) {
      clientId = (window.crypto && crypto.randomUUID) ? crypto.randomUUID() : String(Date.now());
      localStorage.setItem(sessionKey, clientId);
    }
    const qs = new URLSearchParams(window.location.search);
    const incomingToken = qs.get("token");
    if (incomingToken) {
      localStorage.setItem("ha_control_session", incomingToken);
    }

    const term = new Terminal({
      cursorBlink: true,
      convertEol: true,
      fontSize: 16,
      fontFamily: '"Cascadia Mono", "Consolas", ui-monospace, SFMono-Regular, Menlo, monospace',
      scrollback: 10000,
      theme: {
        background: "#0c0c0c",
        foreground: "#cccccc",
        cursor: "#cccccc",
        selectionBackground: "rgba(255,255,255,0.25)"
      }
    });
    const fitAddon = new FitAddon.FitAddon();
    term.loadAddon(fitAddon);
    term.open(document.getElementById("term"));

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
      const token = localStorage.getItem("ha_control_session") || "";
      const query = new URLSearchParams();
      query.set("client_id", clientId);
      if (token) query.set("token", token);
      const proto = window.location.protocol === "https:" ? "wss://" : "ws://";
      ws = new WebSocket(proto + window.location.host + wsPath() + "?" + query.toString());

      ws.onopen = () => {
        sendResize();
      };
      ws.onclose = () => {
        setTimeout(connect, 1000);
      };
      ws.onerror = () => {
        ws.close();
      };
      ws.onmessage = (event) => {
        if (typeof event.data === "string") term.write(event.data);
      };
    }

    term.onData((data) => send({ type: "input", data }));
    window.addEventListener("resize", sendResize);
    sendResize();
    connect();
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


CONSOLE_SESSION_TTL_SECONDS = 1800
_console_sessions: dict[str, "ConsoleSession"] = {}
_console_sessions_lock = asyncio.Lock()


class ConsoleSession:
    def __init__(self, *, key: str, subject: str, source_ip: str) -> None:
        self.key = key
        self.subject = subject
        self.source_ip = source_ip
        self.clients: set[WebSocket] = set()
        self.master_fd: int | None = None
        self.process: asyncio.subprocess.Process | None = None
        self.reader_task: asyncio.Task[None] | None = None
        self.cleanup_task: asyncio.Task[None] | None = None
        self.closed = False
        self.last_detached_at = asyncio.get_running_loop().time()
        self.history: deque[str] = deque()
        self.history_chars = 0
        self.max_history_chars = 200_000

    def _push_history(self, chunk: str) -> None:
        self.history.append(chunk)
        self.history_chars += len(chunk)
        while self.history and self.history_chars > self.max_history_chars:
            removed = self.history.popleft()
            self.history_chars -= len(removed)

    async def start(self) -> None:
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
        self.master_fd = master_fd
        self.process = process
        self.reader_task = asyncio.create_task(self._reader_loop(), name=f"console_reader:{self.key}")

        write_audit_entry(
            actor=self.subject,
            source_ip=self.source_ip,
            action="console.session.start",
            details={"key": self.key, "shell": settings.console_shell, "cwd": cwd, "cmd": shell_cmd},
        )

    async def _reader_loop(self) -> None:
        if self.master_fd is None:
            return
        while not self.closed:
            try:
                chunk = await asyncio.to_thread(os.read, self.master_fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            text = chunk.decode("utf-8", errors="replace")
            self._push_history(text)
            stale: list[WebSocket] = []
            for ws in self.clients:
                try:
                    await ws.send_text(text)
                except Exception:
                    stale.append(ws)
            for ws in stale:
                self.clients.discard(ws)

    async def attach(self, ws: WebSocket) -> None:
        if self.cleanup_task and not self.cleanup_task.done():
            self.cleanup_task.cancel()
        self.clients.add(ws)
        if self.history:
            try:
                await ws.send_text("".join(self.history))
            except Exception:
                self.clients.discard(ws)

    async def detach(self, ws: WebSocket) -> None:
        self.clients.discard(ws)
        if not self.clients:
            self.last_detached_at = asyncio.get_running_loop().time()
            self.cleanup_task = asyncio.create_task(_cleanup_console_session(self.key, self))

    async def write_input(self, text: str) -> None:
        if self.master_fd is None:
            return
        await asyncio.to_thread(os.write, self.master_fd, text.encode("utf-8"))

    async def resize(self, cols: int, rows: int) -> None:
        if self.master_fd is None:
            return
        cols = max(20, min(cols, 500))
        rows = max(8, min(rows, 200))
        await asyncio.to_thread(_set_pty_size, self.master_fd, cols, rows)
        if self.process and self.process.pid:
            try:
                os.kill(self.process.pid, signal.SIGWINCH)
            except ProcessLookupError:
                pass

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True

        if self.reader_task and not self.reader_task.done():
            self.reader_task.cancel()

        if self.process:
            try:
                self.process.terminate()
            except ProcessLookupError:
                pass
            try:
                await self.process.wait()
            except Exception:
                pass

        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = None

        write_audit_entry(
            actor=self.subject,
            source_ip=self.source_ip,
            action="console.session.stop",
            details={"key": self.key},
        )


async def _cleanup_console_session(key: str, runtime: ConsoleSession) -> None:
    await asyncio.sleep(CONSOLE_SESSION_TTL_SECONDS)
    async with _console_sessions_lock:
        current = _console_sessions.get(key)
        if current is not runtime:
            return
        if current.clients:
            return
        elapsed = asyncio.get_running_loop().time() - current.last_detached_at
        if elapsed < CONSOLE_SESSION_TTL_SECONDS:
            return
        _console_sessions.pop(key, None)
    await runtime.close()


def _webdav_root_norm() -> str:
    root = settings.webdav_root.strip() or "/"
    root = posixpath.normpath(root)
    if not root.startswith("/"):
        root = f"/{root}"
    return root


def _resolve_webdav_target(dav_path: str) -> str:
    root = _webdav_root_norm()
    normalized = posixpath.normpath("/" + (dav_path or ""))
    target = posixpath.normpath(posixpath.join(root, normalized.lstrip("/")))
    if root != "/" and not (target == root or target.startswith(root + "/")):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Path escapes WebDAV root")
    return target


def _target_to_virtual(target: str) -> str:
    root = _webdav_root_norm()
    if root == "/":
        return target
    if target == root:
        return "/"
    suffix = target[len(root) :]
    return suffix if suffix.startswith("/") else f"/{suffix}"


def _webdav_href(request: Request, target: str, *, is_dir: bool) -> str:
    virtual = _target_to_virtual(target)
    encoded = quote(virtual.lstrip("/"), safe="/")
    base = request.url.path.rstrip("/")
    if "/webdav" not in base:
        base = "/webdav"
    else:
        base = base[: base.find("/webdav") + len("/webdav")]
    href = f"{base}/{encoded}" if encoded else f"{base}/"
    if is_dir and not href.endswith("/"):
        href += "/"
    return href


def _run_path_command(
    argv: list[str],
    *,
    host_namespace: bool,
    timeout_s: int = 30,
    stdin: bytes | None = None,
) -> tuple[int, bytes, str]:
    if host_namespace:
        res = run_simple_host_command(argv, timeout_s=timeout_s, stdin=stdin)
        return res.returncode, res.stdout, res.stderr.decode("utf-8", errors="replace")

    import subprocess

    try:
        res = subprocess.run(  # noqa: S603
            argv,
            input=stdin,
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as err:
        raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail="Command timed out") from err
    return res.returncode, res.stdout, res.stderr.decode("utf-8", errors="replace")


def _webdav_stat(path: str, *, host_namespace: bool) -> dict[str, Any] | None:
    rc, stdout, _stderr = _run_path_command(
        ["sh", "-c", 'if [ -d "$1" ]; then echo dir; elif [ -f "$1" ]; then echo file; else echo none; fi', "sh", path],
        host_namespace=host_namespace,
        timeout_s=10,
    )
    if rc != 0:
        return None
    kind = stdout.decode("utf-8", errors="replace").strip()
    if kind == "none":
        return None

    size = 0
    mtime = int(datetime.now(tz=UTC).timestamp())
    rc_stat, out_stat, _ = _run_path_command(
        ["sh", "-c", 'stat -c "%s|%Y" "$1" 2>/dev/null || echo "0|0"', "sh", path],
        host_namespace=host_namespace,
        timeout_s=10,
    )
    if rc_stat == 0 and out_stat:
        raw = out_stat.decode("utf-8", errors="replace").strip().split("|", 1)
        if len(raw) == 2:
            try:
                size = int(raw[0]) if kind == "file" else 0
                mtime = int(raw[1]) if int(raw[1]) > 0 else mtime
            except ValueError:
                pass

    return {"path": path, "type": kind, "size": size, "mtime": mtime}


def _webdav_list_dir(path: str, *, host_namespace: bool) -> list[dict[str, Any]]:
    rc, stdout, stderr = _run_path_command(
        ["find", path, "-mindepth", "1", "-maxdepth", "1", "-print0"],
        host_namespace=host_namespace,
        timeout_s=30,
    )
    if rc != 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=stderr or "Failed to list directory")
    result: list[dict[str, Any]] = []
    for raw in stdout.split(b"\x00"):
        candidate = raw.decode("utf-8", errors="replace").strip()
        if not candidate:
            continue
        stat = _webdav_stat(candidate, host_namespace=host_namespace)
        if stat:
            result.append(stat)
    return result


def _webdav_xml_multistatus(request: Request, entries: list[dict[str, Any]]) -> str:
    items: list[str] = []
    for entry in entries:
        href = _webdav_href(request, entry["path"], is_dir=entry["type"] == "dir")
        is_dir = entry["type"] == "dir"
        resourcetype = "<D:collection/>" if is_dir else ""
        content_type = "<D:getcontenttype>httpd/unix-directory</D:getcontenttype>" if is_dir else "<D:getcontenttype>application/octet-stream</D:getcontenttype>"
        content_len = "" if is_dir else f"<D:getcontentlength>{entry['size']}</D:getcontentlength>"
        modified = formatdate(entry["mtime"], usegmt=True)
        items.append(
            "<D:response>"
            f"<D:href>{href}</D:href>"
            "<D:propstat><D:prop>"
            f"<D:resourcetype>{resourcetype}</D:resourcetype>"
            f"{content_type}"
            f"{content_len}"
            f"<D:getlastmodified>{modified}</D:getlastmodified>"
            "</D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat>"
            "</D:response>"
        )
    return '<?xml version="1.0" encoding="utf-8"?><D:multistatus xmlns:D="DAV:">' + "".join(items) + "</D:multistatus>"


def _require_webdav_auth(request: Request, authorization: str | None) -> str:
    require_trusted_network(request)
    bearer = validate_bearer_token(authorization)
    if bearer:
        return bearer.subject

    if not settings.webdav_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="WebDAV disabled")

    if not settings.webdav_username or not settings.webdav_password:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Set webdav_username/webdav_password in add-on options")

    if not authorization or not authorization.lower().startswith("basic "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing WebDAV credentials",
            headers={"WWW-Authenticate": 'Basic realm="ha-control-webdav"'},
        )

    try:
        decoded = base64.b64decode(authorization.split(" ", 1)[1]).decode("utf-8")
    except Exception as err:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid WebDAV credentials",
            headers={"WWW-Authenticate": 'Basic realm="ha-control-webdav"'},
        ) from err

    username, _, password = decoded.partition(":")
    if username != settings.webdav_username or password != settings.webdav_password:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid WebDAV credentials",
            headers={"WWW-Authenticate": 'Basic realm="ha-control-webdav"'},
        )
    return f"webdav:{username}"


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

    client_id = (websocket.query_params.get("client_id") or "default").strip()[:128]
    session_key = f"{subject}:{client_id}"

    async with _console_sessions_lock:
        runtime = _console_sessions.get(session_key)
        if runtime is None or runtime.closed:
            runtime = ConsoleSession(key=session_key, subject=subject, source_ip=client_ip)
            await runtime.start()
            _console_sessions[session_key] = runtime

    await websocket.accept()
    await runtime.attach(websocket)

    write_audit_entry(
        actor=subject,
        source_ip=client_ip,
        action="console.ws.attach",
        details={"session_key": session_key},
    )

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
                await runtime.resize(
                    int(payload.get("cols", 120)),
                    int(payload.get("rows", 40)),
                )
                continue

            text = payload.get("data") if payload and payload.get("type") == "input" else data
            await runtime.write_input(str(text))
    except WebSocketDisconnect:
        pass
    finally:
        await runtime.detach(websocket)


@app.api_route(
    "/webdav",
    methods=["OPTIONS", "PROPFIND", "GET", "HEAD", "PUT", "DELETE", "MKCOL", "MOVE"],
)
@app.api_route(
    "/webdav/{dav_path:path}",
    methods=["OPTIONS", "PROPFIND", "GET", "HEAD", "PUT", "DELETE", "MKCOL", "MOVE"],
)
async def webdav_dispatch(
    request: Request,
    dav_path: str = "",
    authorization: str | None = Header(default=None),
    destination: str | None = Header(default=None),
) -> Response:
    actor = _require_webdav_auth(request, authorization)
    if not settings.webdav_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="WebDAV disabled")

    method = request.method.upper()
    path = unquote(dav_path or "")
    target = _resolve_webdav_target(path)
    host_namespace = settings.webdav_host_namespace

    if settings.webdav_read_only and method in {"PUT", "DELETE", "MKCOL", "MOVE"}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="WebDAV is read-only")

    if method == "OPTIONS":
        response = Response(status_code=status.HTTP_200_OK)
        response.headers["DAV"] = "1,2"
        response.headers["MS-Author-Via"] = "DAV"
        response.headers["Allow"] = "OPTIONS, PROPFIND, GET, HEAD, PUT, DELETE, MKCOL, MOVE"
        return response

    if method == "PROPFIND":
        depth = request.headers.get("depth", "1").strip().lower()
        current = _webdav_stat(target, host_namespace=host_namespace)
        if current is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Path not found")
        entries = [current]
        if current["type"] == "dir" and depth != "0":
            entries.extend(_webdav_list_dir(target, host_namespace=host_namespace))
        xml = _webdav_xml_multistatus(request, entries)
        return Response(content=xml, status_code=207, media_type="application/xml; charset=utf-8")

    if method in {"GET", "HEAD"}:
        info = _webdav_stat(target, host_namespace=host_namespace)
        if info is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Path not found")
        if info["type"] == "dir":
            raise HTTPException(status_code=status.HTTP_405_METHOD_NOT_ALLOWED, detail="Cannot GET a directory")
        code, stdout, stderr = _run_path_command(["cat", target], host_namespace=host_namespace, timeout_s=120)
        if code != 0:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=stderr or "Failed to read file")
        body = b"" if method == "HEAD" else stdout
        response = Response(content=body, status_code=status.HTTP_200_OK, media_type="application/octet-stream")
        response.headers["Content-Length"] = str(len(stdout))
        return response

    if method == "PUT":
        existed = _webdav_stat(target, host_namespace=host_namespace) is not None
        payload = await request.body()
        parent = posixpath.dirname(target) or "/"
        code_mkdir, _out_mkdir, err_mkdir = _run_path_command(["mkdir", "-p", parent], host_namespace=host_namespace)
        if code_mkdir != 0:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=err_mkdir or "Failed to create parent directory")
        code, _stdout, stderr = _run_path_command(
            ["sh", "-c", 'cat > "$1"', "sh", target],
            host_namespace=host_namespace,
            timeout_s=120,
            stdin=payload,
        )
        if code != 0:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=stderr or "Failed to write file")
        write_audit_entry(
            actor=actor,
            source_ip=_source_ip(request),
            action="webdav.put",
            target=target,
            details={"bytes": len(payload), "host_namespace": host_namespace},
            success=True,
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT if existed else status.HTTP_201_CREATED)

    if method == "MKCOL":
        if _webdav_stat(target, host_namespace=host_namespace) is not None:
            raise HTTPException(status_code=status.HTTP_405_METHOD_NOT_ALLOWED, detail="Path already exists")
        code, _stdout, stderr = _run_path_command(["mkdir", "-p", target], host_namespace=host_namespace)
        if code != 0:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=stderr or "Failed to create directory")
        write_audit_entry(
            actor=actor,
            source_ip=_source_ip(request),
            action="webdav.mkcol",
            target=target,
            details={"host_namespace": host_namespace},
            success=True,
        )
        return Response(status_code=status.HTTP_201_CREATED)

    if method == "DELETE":
        info = _webdav_stat(target, host_namespace=host_namespace)
        if info is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Path not found")
        code, _stdout, stderr = _run_path_command(["rm", "-rf", target], host_namespace=host_namespace, timeout_s=120)
        if code != 0:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=stderr or "Failed to delete path")
        write_audit_entry(
            actor=actor,
            source_ip=_source_ip(request),
            action="webdav.delete",
            target=target,
            details={"host_namespace": host_namespace, "type": info["type"]},
            success=True,
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    if method == "MOVE":
        if not destination:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing Destination header")
        parsed = urlparse(destination)
        dst_path = parsed.path or ""
        webdav_prefix = "/webdav"
        if webdav_prefix in dst_path:
            dst_rel = dst_path.split(webdav_prefix, 1)[1].lstrip("/")
        else:
            dst_rel = dst_path.lstrip("/")
        dst_target = _resolve_webdav_target(unquote(dst_rel))

        src_info = _webdav_stat(target, host_namespace=host_namespace)
        if src_info is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Source path not found")
        dst_existed = _webdav_stat(dst_target, host_namespace=host_namespace) is not None
        parent = posixpath.dirname(dst_target) or "/"
        code_mkdir, _out_mkdir, err_mkdir = _run_path_command(["mkdir", "-p", parent], host_namespace=host_namespace)
        if code_mkdir != 0:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=err_mkdir or "Failed to create target directory")
        code, _stdout, stderr = _run_path_command(["mv", target, dst_target], host_namespace=host_namespace, timeout_s=120)
        if code != 0:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=stderr or "Failed to move path")
        write_audit_entry(
            actor=actor,
            source_ip=_source_ip(request),
            action="webdav.move",
            target=f"{target} -> {dst_target}",
            details={"host_namespace": host_namespace},
            success=True,
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT if dst_existed else status.HTTP_201_CREATED)

    raise HTTPException(status_code=status.HTTP_405_METHOD_NOT_ALLOWED, detail="Method not allowed")


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
