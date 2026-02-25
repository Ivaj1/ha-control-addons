"""Launcher for HA Control Agent with optional HTTPS WebDAV and SMB endpoint."""

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
SMB_CONF = Path("/tmp/smb.conf")


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


def _run_quiet(argv: list[str], *, input_text: str | None = None) -> int:
    proc = subprocess.run(  # noqa: S603
        argv,
        input=input_text.encode("utf-8") if input_text is not None else None,
        capture_output=True,
        check=False,
    )
    return proc.returncode


def _init_smb_user(username: str, password: str) -> None:
    # Ignore failures for already-existing principals.
    _run_quiet(["addgroup", username])
    _run_quiet(["adduser", "-D", "-H", "-G", username, "-s", "/bin/false", username])
    _run_quiet(["smbpasswd", "-a", "-s", username], input_text=f"{password}\n{password}\n")


def _build_smb_conf(
    *,
    share_name: str,
    username: str,
    share_path: str,
    read_only: bool,
    allow_hosts: list[str],
) -> str:
    allow_hosts_line = " ".join(["127.0.0.1", *allow_hosts]).strip()
    writable = "no" if read_only else "yes"
    return "\n".join(
        [
            "[global]",
            "   workgroup = WORKGROUP",
            "   server string = HA Control SMB",
            "   security = user",
            "   map to guest = never",
            "   hosts allow = " + allow_hosts_line,
            "   bind interfaces only = yes",
            "   interfaces = lo",
            "   load printers = no",
            "   disable spoolss = yes",
            "   log level = 1",
            "",
            f"[{share_name}]",
            f"   path = {share_path}",
            "   browseable = yes",
            f"   writeable = {writable}",
            f"   valid users = {username}",
            "   force user = root",
            "   force group = root",
            "",
        ]
    )


def _start_smb(options: dict) -> subprocess.Popen | None:
    enabled = _as_bool(options.get("smb_enabled"), True)
    if not enabled:
        return None

    username = str(options.get("smb_username", "haos")).strip()
    password = str(options.get("smb_password", "")).strip()
    share_name = str(options.get("smb_share_name", "haos-root")).strip() or "haos-root"
    read_only = _as_bool(options.get("smb_read_only"), False)
    share_path = str(options.get("smb_root", "/proc/1/root")).strip() or "/proc/1/root"
    allow_hosts = options.get("smb_allow_hosts")
    if not isinstance(allow_hosts, list) or not allow_hosts:
        allow_hosts = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]

    if not username or not password:
        print("[launcher] SMB enabled but username/password missing; skipping SMB startup", flush=True)
        return None

    if not Path(share_path).exists():
        print(f"[launcher] SMB share path '{share_path}' does not exist; fallback to '/'", flush=True)
        share_path = "/"

    Path("/var/lib/samba").mkdir(parents=True, exist_ok=True)
    for db in ("account_policy.tdb", "registry.tdb", "winbindd_idmap.tdb"):
        Path("/var/lib/samba", db).touch(exist_ok=True)
    Path("/etc/samba/lmhosts").touch(exist_ok=True)

    _init_smb_user(username, password)

    SMB_CONF.write_text(
        _build_smb_conf(
            share_name=share_name,
            username=username,
            share_path=share_path,
            read_only=read_only,
            allow_hosts=[str(x) for x in allow_hosts],
        ),
        encoding="utf-8",
    )

    # Keep single-process foreground mode for launcher supervision.
    return subprocess.Popen(["smbd", "--foreground", "--no-process-group", "-s", str(SMB_CONF)])  # noqa: S603


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
    smbd = _start_smb(options)

    children = [proc for proc in (uvicorn, stunnel, smbd) if proc is not None]

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
        try:
            SMB_CONF.unlink(missing_ok=True)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
