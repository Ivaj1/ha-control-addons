"""Compatibility layer for FastAPI exception/status symbols.

The runtime add-on always includes FastAPI. This fallback exists so local unit
tests can run in minimal environments where FastAPI is unavailable.
"""

from __future__ import annotations

try:
    from fastapi import HTTPException, status
except ModuleNotFoundError:

    class _Status:
        HTTP_400_BAD_REQUEST = 400
        HTTP_401_UNAUTHORIZED = 401
        HTTP_403_FORBIDDEN = 403
        HTTP_404_NOT_FOUND = 404
        HTTP_502_BAD_GATEWAY = 502
        HTTP_504_GATEWAY_TIMEOUT = 504

    class HTTPException(Exception):
        def __init__(self, *, status_code: int, detail: str) -> None:
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    status = _Status()

__all__ = ["HTTPException", "status"]
