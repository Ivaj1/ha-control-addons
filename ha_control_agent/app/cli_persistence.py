"""Persistent CLI runtime layout and managed tool bootstrap."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

DEFAULT_CLI_PERSISTENCE_ROOT = Path("/share/ha-control/cli")
MIGRATION_VERSION = 1


@dataclass(slots=True, frozen=True)
class CLIRuntimePaths:
    root: Path
    home: Path
    state: Path
    xdg_config_home: Path
    xdg_cache_home: Path
    xdg_data_home: Path
    bin_dir: Path
    npm_prefix: Path
    npm_bin_dir: Path
    pipx_home: Path
    pipx_bin_dir: Path
    history_file: Path
    shell_rc_file: Path
    bootstrap_status_file: Path
    bootstrap_manifest_file: Path
    bootstrap_lock_file: Path
    migration_marker: Path

    def as_dict(self) -> dict[str, str]:
        return {
            "cli_root": str(self.root),
            "cli_home": str(self.home),
            "cli_state": str(self.state),
            "cli_bin_dir": str(self.bin_dir),
            "cli_npm_prefix": str(self.npm_prefix),
            "cli_pipx_home": str(self.pipx_home),
            "cli_pipx_bin_dir": str(self.pipx_bin_dir),
            "cli_history_file": str(self.history_file),
        }


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


def _is_allowed_root(path: Path) -> bool:
    value = str(path)
    return (
        value == "/share"
        or value.startswith("/share/")
        or value == "/data"
        or value.startswith("/data/")
    )


def _normalize_root(root_value: str) -> Path:
    raw = (root_value or "").strip()
    candidate = Path(raw) if raw else DEFAULT_CLI_PERSISTENCE_ROOT
    if not candidate.is_absolute():
        return DEFAULT_CLI_PERSISTENCE_ROOT
    return candidate if _is_allowed_root(candidate) else DEFAULT_CLI_PERSISTENCE_ROOT


def _build_paths(root: Path) -> CLIRuntimePaths:
    state = root / "state"
    npm_prefix = root / "npm-global"
    pipx_home = root / "pipx" / "home"
    pipx_bin_dir = root / "pipx" / "bin"
    return CLIRuntimePaths(
        root=root,
        home=root / "home",
        state=state,
        xdg_config_home=root / "xdg" / "config",
        xdg_cache_home=root / "xdg" / "cache",
        xdg_data_home=root / "xdg" / "data",
        bin_dir=root / "bin",
        npm_prefix=npm_prefix,
        npm_bin_dir=npm_prefix / "bin",
        pipx_home=pipx_home,
        pipx_bin_dir=pipx_bin_dir,
        history_file=state / "shell_history",
        shell_rc_file=(root / "home" / ".shrc"),
        bootstrap_status_file=state / "bootstrap-status.json",
        bootstrap_manifest_file=state / "bootstrap-manifest.json",
        bootstrap_lock_file=state / "bootstrap-tools.lock",
        migration_marker=state / "migrations" / f".migrated_v{MIGRATION_VERSION}",
    )


def _write_if_missing(path: Path, content: str) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _normalize_package_list(packages: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for item in packages:
        value = str(item).strip()
        if not value or value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    return normalized


def _npm_presence_key(spec: str) -> str:
    value = spec.strip()
    if value.startswith("@"):
        at_index = value.rfind("@")
        return value[:at_index] if at_index > 0 else value
    head, _, _tail = value.partition("@")
    return head or value


def _pipx_presence_key(spec: str) -> str:
    value = spec.strip()
    stop_tokens = ("==", ">=", "<=", "~=", "!=", "[", " ", "@")
    index = len(value)
    for token in stop_tokens:
        pos = value.find(token)
        if pos != -1 and pos < index:
            index = pos
    key = value[:index].strip()
    return key or value


def ensure_cli_runtime_layout(*, root_value: str, persist_history: bool) -> CLIRuntimePaths:
    root = _normalize_root(root_value)
    runtime = _build_paths(root)

    dirs = [
        runtime.root,
        runtime.home,
        runtime.state,
        runtime.xdg_config_home,
        runtime.xdg_cache_home,
        runtime.xdg_data_home,
        runtime.bin_dir,
        runtime.npm_prefix,
        runtime.npm_bin_dir,
        runtime.pipx_home,
        runtime.pipx_bin_dir,
        runtime.migration_marker.parent,
    ]
    for directory in dirs:
        directory.mkdir(parents=True, exist_ok=True)

    if persist_history and not runtime.history_file.exists():
        runtime.history_file.touch()

    _write_if_missing(
        runtime.shell_rc_file,
        (
            "# Persistent shell runtime for HA Control Agent.\n"
            'export PATH="$HOME/.local/bin:$PATH"\n'
            'export HISTFILE="${HISTFILE:-$HOME/.history}"\n'
            'export HISTSIZE="${HISTSIZE:-5000}"\n'
            'export HISTFILESIZE="${HISTFILESIZE:-10000}"\n'
        ),
    )
    _write_if_missing(
        runtime.home / ".profile",
        (
            "# Loaded by login shells\n"
            '[ -f "$HOME/.shrc" ] && . "$HOME/.shrc"\n'
        ),
    )
    _write_if_missing(
        runtime.home / ".bashrc",
        (
            "# Loaded by interactive bash shells\n"
            '[ -f "$HOME/.shrc" ] && . "$HOME/.shrc"\n'
            "shopt -s histappend 2>/dev/null || true\n"
            'PROMPT_COMMAND="history -a;${PROMPT_COMMAND:-:}"\n'
        ),
    )
    _write_if_missing(runtime.bootstrap_lock_file, "")
    _write_if_missing(runtime.migration_marker, _now_iso() + "\n")
    return runtime


def apply_cli_runtime_env(
    *,
    base_env: dict[str, str],
    runtime: CLIRuntimePaths,
    persist_history: bool,
) -> dict[str, str]:
    env = base_env.copy()
    env["HOME"] = str(runtime.home)
    env["ENV"] = str(runtime.shell_rc_file)
    env["XDG_CONFIG_HOME"] = str(runtime.xdg_config_home)
    env["XDG_CACHE_HOME"] = str(runtime.xdg_cache_home)
    env["XDG_DATA_HOME"] = str(runtime.xdg_data_home)
    env["NPM_CONFIG_PREFIX"] = str(runtime.npm_prefix)
    env["PIPX_HOME"] = str(runtime.pipx_home)
    env["PIPX_BIN_DIR"] = str(runtime.pipx_bin_dir)
    if persist_history:
        env["HISTFILE"] = str(runtime.history_file)

    current_path = env.get("PATH", "")
    parts = [
        str(runtime.bin_dir),
        str(runtime.npm_bin_dir),
        str(runtime.pipx_bin_dir),
        current_path,
    ]
    env["PATH"] = ":".join(part for part in parts if part)
    return env


def _run_cmd(
    argv: list[str],
    *,
    env: dict[str, str],
    timeout_s: int = 1200,
) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(  # noqa: S603
            argv,
            check=False,
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as err:
        return 124, "", f"timeout: {err}"
    return proc.returncode, proc.stdout, proc.stderr


def _npm_installed_packages(runtime: CLIRuntimePaths, env: dict[str, str]) -> set[str]:
    code, stdout, _stderr = _run_cmd(
        ["npm", "ls", "-g", "--depth=0", "--json", "--prefix", str(runtime.npm_prefix)],
        env=env,
        timeout_s=180,
    )
    if code not in (0, 1):
        return set()
    try:
        payload = json.loads(stdout or "{}")
    except json.JSONDecodeError:
        return set()
    deps = payload.get("dependencies")
    return set(deps.keys()) if isinstance(deps, dict) else set()


def _pipx_installed_packages(runtime: CLIRuntimePaths, env: dict[str, str]) -> set[str]:
    code, stdout, _stderr = _run_cmd(
        [sys.executable, "-m", "pipx", "list", "--json"],
        env=env,
        timeout_s=180,
    )
    if code != 0:
        return set()
    try:
        payload = json.loads(stdout or "{}")
    except json.JSONDecodeError:
        return set()
    venvs = payload.get("venvs")
    return set(venvs.keys()) if isinstance(venvs, dict) else set()


def bootstrap_cli_tools(
    *,
    runtime: CLIRuntimePaths,
    enabled: bool,
    npm_packages: list[str],
    pipx_packages: list[str],
    persist_history: bool,
) -> dict[str, Any]:
    manifest = {
        "generated_at": _now_iso(),
        "enabled": enabled,
        "npm_packages": _normalize_package_list(npm_packages),
        "pipx_packages": _normalize_package_list(pipx_packages),
    }
    _write_json(runtime.bootstrap_manifest_file, manifest)

    status: dict[str, Any] = {
        "generated_at": _now_iso(),
        "enabled": enabled,
        "ok": True,
        "root": str(runtime.root),
        "persist_history": persist_history,
        "npm": {"requested": manifest["npm_packages"], "installed": [], "failed": []},
        "pipx": {"requested": manifest["pipx_packages"], "installed": [], "failed": []},
    }
    if not enabled:
        _write_json(runtime.bootstrap_status_file, status)
        return status

    env = apply_cli_runtime_env(base_env=os.environ.copy(), runtime=runtime, persist_history=persist_history)

    if manifest["npm_packages"]:
        installed = _npm_installed_packages(runtime, env)
        for package in manifest["npm_packages"]:
            presence_key = _npm_presence_key(package)
            if presence_key in installed:
                continue
            code, _stdout, stderr = _run_cmd(
                ["npm", "install", "-g", "--prefix", str(runtime.npm_prefix), package],
                env=env,
            )
            if code == 0:
                status["npm"]["installed"].append(package)
            else:
                status["ok"] = False
                status["npm"]["failed"].append({"package": package, "error": stderr.strip()[:500]})

    if manifest["pipx_packages"]:
        code, _stdout, stderr = _run_cmd([sys.executable, "-m", "pipx", "--version"], env=env, timeout_s=30)
        if code != 0:
            status["ok"] = False
            status["pipx"]["failed"].append({"package": "_runtime", "error": (stderr or "pipx unavailable").strip()[:500]})
        else:
            installed = _pipx_installed_packages(runtime, env)
            for package in manifest["pipx_packages"]:
                presence_key = _pipx_presence_key(package)
                if presence_key in installed:
                    continue
                code, _stdout, stderr = _run_cmd([sys.executable, "-m", "pipx", "install", package], env=env)
                if code == 0:
                    status["pipx"]["installed"].append(package)
                else:
                    status["ok"] = False
                    status["pipx"]["failed"].append({"package": package, "error": stderr.strip()[:500]})

    lock_lines = [
        f"timestamp={_now_iso()}",
        f"ok={status['ok']}",
        f"npm_requested={','.join(manifest['npm_packages'])}",
        f"pipx_requested={','.join(manifest['pipx_packages'])}",
    ]
    runtime.bootstrap_lock_file.write_text("\n".join(lock_lines) + "\n", encoding="utf-8")
    _write_json(runtime.bootstrap_status_file, status)
    return status
