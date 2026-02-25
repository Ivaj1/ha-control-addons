"""Security helpers (session token and network checks)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import ipaddress
import secrets
from threading import Lock

from fastapi import Depends, Header, HTTPException, Request, status

from .config import settings


@dataclass(slots=True, frozen=True)
class SessionInfo:
    subject: str
    expires_at: datetime


class SessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, SessionInfo] = {}
        self._lock = Lock()

    def create(self, subject: str, ttl_seconds: int) -> tuple[str, datetime]:
        token = secrets.token_urlsafe(48)
        expires_at = datetime.now(tz=UTC) + timedelta(seconds=ttl_seconds)
        with self._lock:
            self._sessions[token] = SessionInfo(subject=subject, expires_at=expires_at)
        return token, expires_at

    def validate(self, token: str) -> SessionInfo | None:
        now = datetime.now(tz=UTC)
        with self._lock:
            info = self._sessions.get(token)
            if info is None:
                return None
            if info.expires_at <= now:
                self._sessions.pop(token, None)
                return None
            return info


session_store = SessionStore()


def _extract_client_ip(request: Request) -> ipaddress._BaseAddress:
    raw_ip = request.client.host if request.client else "127.0.0.1"
    try:
        return ipaddress.ip_address(raw_ip)
    except ValueError as err:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid client IP: {raw_ip}",
        ) from err


def is_trusted_ip(raw_ip: str) -> bool:
    try:
        client_ip = ipaddress.ip_address(raw_ip)
    except ValueError:
        return False

    for cidr in settings.trusted_cidrs:
        try:
            network = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            continue
        if client_ip in network:
            return True
    return False


def require_trusted_network(request: Request) -> None:
    client_ip = _extract_client_ip(request)

    if is_trusted_ip(str(client_ip)):
        return

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=f"Client IP {client_ip} is outside trusted CIDRs",
    )


def validate_bearer_token(authorization: str | None) -> SessionInfo | None:
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        return None
    return session_store.validate(token)


def require_session(
    request: Request,
    authorization: str | None = Header(default=None),
) -> SessionInfo:
    require_trusted_network(request)
    info = validate_bearer_token(authorization)
    if info is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing, invalid, or expired session token",
        )

    return info


SessionDependency = Depends(require_session)
