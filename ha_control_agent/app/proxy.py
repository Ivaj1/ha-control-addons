"""Supervisor/Core proxy helpers."""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import HTTPException, status

from .config import settings

_RETRYABLE_STATUS = {502, 503, 504}
_MAX_ATTEMPTS = 3


def _compose_url(base: str, path: str, query: dict[str, str] | None) -> str:
    normalized = path.lstrip("/")
    url = f"{base.rstrip('/')}/{normalized}" if normalized else base.rstrip("/")
    if query:
        url = f"{url}?{urlencode(query, doseq=True)}"
    return url


async def request_supervisor(
    *,
    method: str,
    path: str,
    body: dict[str, Any] | list[Any] | str | bytes | None,
    headers: dict[str, str] | None,
    query: dict[str, str] | None,
) -> httpx.Response:
    if not settings.supervisor_token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Supervisor token unavailable in add-on runtime",
        )
    url = _compose_url("http://supervisor", path, query)

    req_headers = {"Authorization": f"Bearer {settings.supervisor_token}"}
    if headers:
        req_headers.update(headers)

    async with httpx.AsyncClient(timeout=120) as client:
        return await _request_with_retry(
            client=client,
            method=method,
            url=url,
            headers=req_headers,
            body=body,
        )


async def request_core_rest(
    *,
    method: str,
    path: str,
    body: dict[str, Any] | list[Any] | str | bytes | None,
    headers: dict[str, str] | None,
    query: dict[str, str] | None,
) -> httpx.Response:
    if not settings.supervisor_token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Supervisor token unavailable in add-on runtime",
        )
    url = _compose_url("http://supervisor/core/api", path, query)

    req_headers = {"Authorization": f"Bearer {settings.supervisor_token}"}
    if headers:
        req_headers.update(headers)

    async with httpx.AsyncClient(timeout=120) as client:
        return await _request_with_retry(
            client=client,
            method=method,
            url=url,
            headers=req_headers,
            body=body,
        )


async def _request_with_retry(
    *,
    client: httpx.AsyncClient,
    method: str,
    url: str,
    headers: dict[str, str],
    body: dict[str, Any] | list[Any] | str | bytes | None,
) -> httpx.Response:
    method_upper = method.upper()
    retryable_method = method_upper in {"GET", "HEAD", "OPTIONS", "PUT", "DELETE"}

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            response = await client.request(
                method=method_upper,
                url=url,
                headers=headers,
                json=body if isinstance(body, (dict, list)) else None,
                content=body if isinstance(body, (str, bytes)) else None,
            )
        except httpx.HTTPError:
            if not retryable_method or attempt >= _MAX_ATTEMPTS:
                raise
            await asyncio.sleep(0.2 * attempt)
            continue

        if (
            retryable_method
            and response.status_code in _RETRYABLE_STATUS
            and attempt < _MAX_ATTEMPTS
        ):
            await asyncio.sleep(0.2 * attempt)
            continue

        return response

    # Loop always returns or raises.
    raise RuntimeError("Unreachable retry loop")
