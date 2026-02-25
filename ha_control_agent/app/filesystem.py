"""Filesystem operations for local and host namespace paths."""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
import os
from pathlib import Path
import shutil
from typing import Any

from .config import settings
from .fastapi_compat import HTTPException, status
from .host_exec import run_simple_host_command


def _deny_special_path(path: str, *, host_namespace: bool) -> None:
    if not host_namespace or settings.unsafe_allow_special_paths:
        return

    blocked = ("/proc", "/sys", "/dev")
    for prefix in blocked:
        if path == prefix or path.startswith(prefix + "/"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Path '{path}' is blocked by safety policy",
            )


def _validate_abs_path(path: str) -> Path:
    p = Path(path)
    if not p.is_absolute():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Path must be absolute",
        )
    return p


def _hash_bytes(data: bytes) -> str:
    return sha256(data).hexdigest()


def _snapshot_name(path: str) -> str:
    stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%S")
    base = os.path.basename(path.rstrip("/")) or "root"
    return f"{base}.{stamp}.bak"


def _backup_local_path(path: Path) -> str | None:
    if not path.exists():
        return None

    settings.backup_root.mkdir(parents=True, exist_ok=True)
    backup_path = settings.backup_root / _snapshot_name(str(path))

    try:
        if path.is_dir():
            shutil.copytree(path, backup_path)
        else:
            shutil.copy2(path, backup_path)
    except OSError:
        return None

    return str(backup_path)


def _backup_host_path(path: str) -> str | None:
    backup_dir = settings.backup_root.as_posix()
    run_simple_host_command(["mkdir", "-p", backup_dir], timeout_s=30)
    backup_path = f"{backup_dir}/{_snapshot_name(path)}"
    copy = run_simple_host_command(["cp", "-a", path, backup_path], timeout_s=30)
    if copy.returncode != 0:
        return None
    return backup_path


def _hash_local_file(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    try:
        return _hash_bytes(path.read_bytes())
    except OSError:
        return None


def _hash_host_file(path: str) -> str | None:
    result = run_simple_host_command(
        [
            "sh",
            "-c",
            'if [ -f "$1" ]; then cat "$1"; fi',
            "sh",
            path,
        ],
        timeout_s=30,
    )
    if result.returncode != 0 or not result.stdout:
        return None
    return _hash_bytes(result.stdout)


def read_file(path: str, *, host_namespace: bool) -> dict[str, Any]:
    _validate_abs_path(path)
    _deny_special_path(path, host_namespace=host_namespace)

    if host_namespace:
        result = run_simple_host_command(["cat", path], timeout_s=30)
        if result.returncode != 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=result.stderr.decode("utf-8", errors="replace")
                or "Failed to read file",
            )
        content = result.stdout
    else:
        file_path = Path(path)
        if not file_path.exists() or not file_path.is_file():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="File not found",
            )
        try:
            content = file_path.read_bytes()
        except OSError as err:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(err),
            ) from err

    return {
        "path": path,
        "content": content.decode("utf-8", errors="replace"),
        "sha256": _hash_bytes(content),
        "size": len(content),
    }


def write_file(
    *,
    path: str,
    content: str,
    mode: str | None,
    create_dirs: bool,
    host_namespace: bool,
) -> dict[str, Any]:
    target = _validate_abs_path(path)
    _deny_special_path(path, host_namespace=host_namespace)

    payload = content.encode("utf-8")
    payload_hash = _hash_bytes(payload)

    before_hash = _hash_host_file(path) if host_namespace else _hash_local_file(target)

    if before_hash is not None and before_hash == payload_hash:
        return {
            "path": path,
            "bytes_written": 0,
            "backup_path": None,
            "before_hash": before_hash,
            "after_hash": before_hash,
            "sha256": payload_hash,
            "result": "no_change",
        }

    if host_namespace:
        if create_dirs:
            run_simple_host_command(["mkdir", "-p", str(target.parent)], timeout_s=30)

        backup_path = _backup_host_path(path)

        write = run_simple_host_command(
            ["sh", "-c", 'cat > "$1"', "sh", path],
            timeout_s=30,
            stdin=payload,
        )
        if write.returncode != 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=write.stderr.decode("utf-8", errors="replace")
                or "Failed to write file",
            )

        if mode:
            chmod = run_simple_host_command(["chmod", mode, path], timeout_s=30)
            if chmod.returncode != 0:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=chmod.stderr.decode("utf-8", errors="replace")
                    or "chmod failed",
                )

        after_hash = _hash_host_file(path)
    else:
        if create_dirs:
            target.parent.mkdir(parents=True, exist_ok=True)

        backup_path = _backup_local_path(target)

        try:
            target.write_bytes(payload)
            if mode:
                os.chmod(target, int(mode, 8))
        except OSError as err:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(err),
            ) from err

        after_hash = _hash_local_file(target)

    return {
        "path": path,
        "bytes_written": len(payload),
        "backup_path": backup_path,
        "before_hash": before_hash,
        "after_hash": after_hash,
        "sha256": payload_hash,
        "result": "written",
    }


def move_path(*, src: str, dst: str, host_namespace: bool) -> dict[str, Any]:
    src_path = _validate_abs_path(src)
    dst_path = _validate_abs_path(dst)
    _deny_special_path(src, host_namespace=host_namespace)
    _deny_special_path(dst, host_namespace=host_namespace)

    src_before_hash = _hash_host_file(src) if host_namespace else _hash_local_file(src_path)

    if host_namespace:
        backup_path = _backup_host_path(src)
        move = run_simple_host_command(["mv", src, dst], timeout_s=30)
        if move.returncode != 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=move.stderr.decode("utf-8", errors="replace") or "Move failed",
            )
        dst_after_hash = _hash_host_file(dst)
    else:
        backup_path = _backup_local_path(src_path)
        try:
            src_path.rename(dst_path)
        except OSError as err:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(err),
            ) from err
        dst_after_hash = _hash_local_file(dst_path)

    return {
        "src": src,
        "dst": dst,
        "backup_path": backup_path,
        "src_before_hash": src_before_hash,
        "dst_after_hash": dst_after_hash,
        "moved": True,
        "result": "moved",
    }


def delete_path(*, path: str, recursive: bool, host_namespace: bool) -> dict[str, Any]:
    target = _validate_abs_path(path)
    _deny_special_path(path, host_namespace=host_namespace)

    before_hash = _hash_host_file(path) if host_namespace else _hash_local_file(target)
    backup_path = _backup_host_path(path) if host_namespace else _backup_local_path(target)

    if host_namespace:
        argv = ["rm", "-rf", path] if recursive else ["rm", "-f", path]
        delete = run_simple_host_command(argv, timeout_s=30)
        if delete.returncode != 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=delete.stderr.decode("utf-8", errors="replace")
                or "Delete failed",
            )
    else:
        try:
            if target.is_dir() and recursive:
                shutil.rmtree(target)
            elif target.is_file() or target.is_symlink():
                target.unlink(missing_ok=True)
            elif target.is_dir():
                target.rmdir()
        except OSError as err:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(err),
            ) from err

    return {
        "path": path,
        "deleted": True,
        "backup_path": backup_path,
        "before_hash": before_hash,
        "result": "deleted",
    }


def tree(*, path: str, max_depth: int, host_namespace: bool) -> dict[str, Any]:
    _validate_abs_path(path)
    _deny_special_path(path, host_namespace=host_namespace)

    if max_depth < 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="max_depth must be >= 0",
        )

    if host_namespace:
        result = run_simple_host_command(
            ["find", path, "-maxdepth", str(max_depth), "-print"],
            timeout_s=60,
        )
        if result.returncode != 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=result.stderr.decode("utf-8", errors="replace")
                or "find failed",
            )

        paths = [
            line.strip()
            for line in result.stdout.decode("utf-8", errors="replace").splitlines()
            if line.strip()
        ]
        entries = []
        for item in paths:
            kind_res = run_simple_host_command(
                [
                    "sh",
                    "-c",
                    'if [ -d "$1" ]; then echo dir; elif [ -f "$1" ]; then echo file; else echo other; fi',
                    "sh",
                    item,
                ],
                timeout_s=5,
            )
            kind = kind_res.stdout.decode("utf-8", errors="replace").strip() or "other"
            entries.append({"path": item, "type": kind})

        return {"root": path, "entries": entries}

    root = Path(path)
    if not root.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Path not found")

    entries: list[dict[str, str]] = []
    root_depth = len(root.parts)

    for current, dirs, files in os.walk(root):
        current_path = Path(current)
        depth = len(current_path.parts) - root_depth
        if depth > max_depth:
            continue
        entries.append({"path": str(current_path), "type": "dir"})

        if depth == max_depth:
            dirs[:] = []
            continue

        for filename in files:
            entries.append({"path": str(current_path / filename), "type": "file"})

    return {"root": path, "entries": entries}
