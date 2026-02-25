"""Host namespace command execution helpers."""

from __future__ import annotations

from datetime import UTC, datetime
import shlex
import subprocess
from typing import Callable
from typing import Iterable

from .config import settings
from .fastapi_compat import HTTPException, status

_NSENTER_PREFIX = [
    "nsenter",
    "--target",
    "1",
    "--mount",
    "--pid",
    "--",
]


def _reject(message: str) -> None:
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=message,
    )


def _require_argv_len(command: list[str], minimum: int) -> None:
    if len(command) < minimum:
        _reject(f"Command '{command[0]}' requires at least {minimum - 1} argument(s)")


def _validate_ls(command: list[str]) -> None:
    _require_argv_len(command, 1)


def _validate_cat(command: list[str]) -> None:
    _require_argv_len(command, 2)


def _validate_cp(command: list[str]) -> None:
    _require_argv_len(command, 3)


def _validate_mv(command: list[str]) -> None:
    _require_argv_len(command, 3)


def _validate_mkdir(command: list[str]) -> None:
    _require_argv_len(command, 2)


def _validate_chmod(command: list[str]) -> None:
    _require_argv_len(command, 3)


def _validate_chown(command: list[str]) -> None:
    _require_argv_len(command, 3)


def _validate_find(command: list[str]) -> None:
    _require_argv_len(command, 2)


def _validate_stat(command: list[str]) -> None:
    _require_argv_len(command, 2)


def _validate_ha(command: list[str]) -> None:
    _require_argv_len(command, 2)


def _validate_docker(command: list[str]) -> None:
    # Keep default policy read-mostly for docker operations.
    allowed_subcommands = {
        "ps",
        "inspect",
        "logs",
        "images",
        "info",
        "stats",
    }
    if len(command) < 2 or command[1] not in allowed_subcommands:
        _reject(
            f"docker subcommand '{command[1] if len(command) > 1 else ''}' is not allowed"
        )


def _validate_systemctl(command: list[str]) -> None:
    allowed = {
        "status",
        "is-active",
        "show",
        "restart",
        "start",
        "stop",
        "reload",
    }
    if len(command) < 2 or command[1] not in allowed:
        _reject(
            f"systemctl subcommand '{command[1] if len(command) > 1 else ''}' is not allowed"
        )


def _validate_journalctl(command: list[str]) -> None:
    _require_argv_len(command, 1)
    disallowed_flags = {"--setup-keys", "--force", "--rotate", "--vacuum-size", "--vacuum-time"}
    for part in command[1:]:
        if part in disallowed_flags:
            _reject(f"journalctl flag '{part}' is not allowed")


def _validate_reboot(command: list[str]) -> None:
    if len(command) > 2:
        _reject("reboot accepts at most one flag")


def _validate_shutdown(command: list[str]) -> None:
    _require_argv_len(command, 1)


_EXEC_POLICY: dict[str, Callable[[list[str]], None]] = {
    "ls": _validate_ls,
    "cat": _validate_cat,
    "cp": _validate_cp,
    "mv": _validate_mv,
    "mkdir": _validate_mkdir,
    "chmod": _validate_chmod,
    "chown": _validate_chown,
    "find": _validate_find,
    "stat": _validate_stat,
    "ha": _validate_ha,
    "journalctl": _validate_journalctl,
    "docker": _validate_docker,
    "systemctl": _validate_systemctl,
    "reboot": _validate_reboot,
    "shutdown": _validate_shutdown,
    "hostnamectl": _validate_ls,
    "timedatectl": _validate_ls,
}


def _ensure_exec_allowed(command: list[str], shell: bool) -> None:
    if settings.unsafe_allow_exec:
        return

    if shell:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Shell mode is disabled",
        )

    if not command:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Command cannot be empty",
        )

    policy = _EXEC_POLICY.get(command[0])
    if policy is None:
        _reject(f"Command prefix '{command[0]}' is not allowed")

    policy(command)


def normalize_command(cmd: list[str] | str, *, shell: bool = False) -> list[str]:
    if isinstance(cmd, str):
        if shell:
            return ["sh", "-lc", cmd]
        return shlex.split(cmd)

    if shell:
        return ["sh", "-lc", " ".join(shlex.quote(part) for part in cmd)]

    return list(cmd)


def run_command(
    cmd: list[str] | str,
    *,
    timeout_s: int,
    host_namespace: bool,
    shell: bool,
    stdin: str | None,
    cwd: str | None,
) -> dict[str, int | str]:
    command = normalize_command(cmd, shell=shell)
    _ensure_exec_allowed(command, shell=shell)

    subprocess_cwd: str | None = cwd
    shell_command = command

    if cwd and host_namespace:
        # `cwd` in host namespace is not reliable with subprocess cwd in container,
        # so explicitly change directory in a host shell.
        shell_command = [
            "sh",
            "-lc",
            f"cd {shlex.quote(cwd)} && exec {' '.join(shlex.quote(arg) for arg in command)}",
        ]
        subprocess_cwd = None

    full_command = [*(_NSENTER_PREFIX if host_namespace else []), *shell_command]

    started = datetime.now(tz=UTC)

    process = subprocess.run(  # noqa: S603
        full_command,
        input=(stdin or "").encode("utf-8") if stdin is not None else None,
        capture_output=True,
        timeout=timeout_s,
        check=False,
        cwd=subprocess_cwd,
    )

    duration_ms = int((datetime.now(tz=UTC) - started).total_seconds() * 1000)

    return {
        "exit_code": process.returncode,
        "stdout": process.stdout.decode("utf-8", errors="replace"),
        "stderr": process.stderr.decode("utf-8", errors="replace"),
        "duration_ms": duration_ms,
    }


def run_simple_host_command(
    argv: Iterable[str],
    *,
    timeout_s: int = 30,
    stdin: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    command = [*_NSENTER_PREFIX, *list(argv)]
    try:
        return subprocess.run(  # noqa: S603
            command,
            input=stdin,
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as err:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="Host command timed out",
        ) from err
