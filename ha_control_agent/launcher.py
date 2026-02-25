"""Launcher for HA Control Agent with optional HTTPS WebDAV endpoint."""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

OPTIONS_PATH = Path("/data/options.json")
STUNNEL_CONF = Path("/tmp/stunnel.conf")


def _read_options() -> dict:
    if not OPTIONS_PATH.exists():
        return {}
    try:
        return json.loads(OPTIONS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _as_bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _as_int(value: object, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _start_uvicorn() -> subprocess.Popen:
    return subprocess.Popen(  # noqa: S603
        [
            "python",
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "0.0.0.0",
            "--port",
            "9123",
        ]
    )


def _start_stunnel(port: int, cert: str, key: str) -> subprocess.Popen | None:
    cert_path = Path(cert)
    key_path = Path(key)
    if not cert_path.exists() or not key_path.exists():
        print(
            f"[launcher] HTTPS requested but cert/key missing ({cert_path}, {key_path}); skipping stunnel",
            flush=True,
        )
        return None

    STUNNEL_CONF.write_text(
        "\n".join(
            [
                "foreground = yes",
                "debug = 4",
                f"cert = {cert_path}",
                f"key = {key_path}",
                "[webdav_tls]",
                f"accept = 0.0.0.0:{port}",
                "connect = 127.0.0.1:9123",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return subprocess.Popen(["stunnel", str(STUNNEL_CONF)])  # noqa: S603


def main() -> int:
    options = _read_options()
    https_enabled = _as_bool(options.get("webdav_https_enabled"), False)
    https_port = _as_int(options.get("webdav_https_port"), 9443)
    https_cert = str(options.get("webdav_https_cert", "/ssl/fullchain.pem"))
    https_key = str(options.get("webdav_https_key", "/ssl/privkey.pem"))

    uvicorn = _start_uvicorn()
    stunnel = _start_stunnel(https_port, https_cert, https_key) if https_enabled else None

    children = [proc for proc in (uvicorn, stunnel) if proc is not None]

    def _terminate(_signum: int, _frame: object) -> None:
        for proc in children:
            if proc.poll() is None:
                proc.terminate()

    signal.signal(signal.SIGTERM, _terminate)
    signal.signal(signal.SIGINT, _terminate)

    try:
        while True:
            for proc in children:
                code = proc.poll()
                if code is not None:
                    for other in children:
                        if other is not proc and other.poll() is None:
                            other.terminate()
                    return code
            time.sleep(0.3)
    finally:
        for proc in children:
            if proc.poll() is None:
                proc.terminate()
        for proc in children:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        try:
            STUNNEL_CONF.unlink(missing_ok=True)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())

