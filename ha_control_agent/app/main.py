"""FastAPI application for HA Control Agent."""

from __future__ import annotations

from datetime import UTC, datetime
import json
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
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
from .security import SessionInfo, require_session, require_trusted_network, session_store
from .ws_message import build_ws_message
from .ws_bridge import WebSocketBridgeError, send_core_ws

app = FastAPI(title="HA Control Agent", version="0.1.0")


def _source_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _require_supervisor_token() -> None:
    if settings.supervisor_token:
        return
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Supervisor token is unavailable in add-on runtime (SUPERVISOR_TOKEN/HASSIO_TOKEN missing)",
    )


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
        "agent_version": "0.1.0",
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
