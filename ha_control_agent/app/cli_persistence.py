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
    apk_state_file: Path
    apk_wrapper_file: Path
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
            "cli_apk_state_file": str(self.apk_state_file),
            "cli_apk_wrapper_file": str(self.apk_wrapper_file),
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
        apk_state_file=state / "apk-packages.txt",
        apk_wrapper_file=(root / "bin" / "apk"),
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


def _write_executable(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    current_mode = path.stat().st_mode
    path.chmod(current_mode | 0o111)


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


def _apk_presence_key(spec: str) -> str:
    value = spec.strip()
    stop_tokens = ("<", ">", "=", "~", "!", "@", " ")
    index = len(value)
    for token in stop_tokens:
        pos = value.find(token)
        if pos != -1 and pos < index:
            index = pos
    key = value[:index].strip()
    return key or value


def _build_apk_wrapper(state_file: Path) -> str:
    escaped_state = str(state_file).replace('"', '\\"')
    return (
        "#!/bin/sh\n"
        "set -eu\n"
        "\n"
        'REAL_APK="/sbin/apk"\n'
        f'STATE_FILE="${{HACTRL_APK_STATE_FILE:-{escaped_state}}}"\n'
        "\n"
        "ensure_state_file() {\n"
        '  state_dir="$(dirname "$STATE_FILE")"\n'
        '  mkdir -p "$state_dir"\n'
        '  [ -f "$STATE_FILE" ] || : > "$STATE_FILE"\n'
        "}\n"
        "\n"
        "collect_packages() {\n"
        "  expect_value=0\n"
        "  for arg in \"$@\"; do\n"
        "    if [ \"$expect_value\" -eq 1 ]; then\n"
        "      expect_value=0\n"
        "      continue\n"
        "    fi\n"
        "    case \"$arg\" in\n"
        "      --virtual|-t|--repository|--repositories-file|--root|--keys-dir|--cache-dir|--wait|--timeout|--arch|--allow-untrusted)\n"
        "        expect_value=1\n"
        "        ;;\n"
        "      --*=*)\n"
        "        ;;\n"
        "      -*)\n"
        "        ;;\n"
        "      *)\n"
        "        printf '%s\\n' \"$arg\"\n"
        "        ;;\n"
        "    esac\n"
        "  done\n"
        "}\n"
        "\n"
        "update_state_add() {\n"
        "  ensure_state_file\n"
        '  tmp="${STATE_FILE}.tmp.$$"\n'
        '  cp "$STATE_FILE" "$tmp" 2>/dev/null || : > "$tmp"\n'
        "  while IFS= read -r pkg; do\n"
        "    [ -n \"$pkg\" ] || continue\n"
        "    if ! grep -qxF \"$pkg\" \"$tmp\" 2>/dev/null; then\n"
        "      printf '%s\\n' \"$pkg\" >> \"$tmp\"\n"
        "    fi\n"
        "  done\n"
        "  sort -u \"$tmp\" > \"$STATE_FILE\"\n"
        '  rm -f "$tmp"\n'
        "}\n"
        "\n"
        "update_state_del() {\n"
        "  ensure_state_file\n"
        '  tmp="${STATE_FILE}.tmp.$$"\n'
        '  cp "$STATE_FILE" "$tmp" 2>/dev/null || : > "$tmp"\n'
        "  while IFS= read -r pkg; do\n"
        "    [ -n \"$pkg\" ] || continue\n"
        '    grep -vxF "$pkg" "$tmp" > "${tmp}.next" 2>/dev/null || true\n'
        '    mv "${tmp}.next" "$tmp"\n'
        "  done\n"
        "  sort -u \"$tmp\" > \"$STATE_FILE\"\n"
        '  rm -f "$tmp"\n'
        "}\n"
        "\n"
        'sub="${1:-}"\n'
        "case \"$sub\" in\n"
        "  add)\n"
        '    "$REAL_APK" "$@"\n'
        '    rc="$?"\n'
        "    if [ \"$rc\" -eq 0 ]; then\n"
        "      shift\n"
        "      collect_packages \"$@\" | update_state_add\n"
        "    fi\n"
        '    exit "$rc"\n'
        "    ;;\n"
        "  del)\n"
        '    "$REAL_APK" "$@"\n'
        '    rc="$?"\n'
        "    if [ \"$rc\" -eq 0 ]; then\n"
        "      shift\n"
        "      collect_packages \"$@\" | update_state_del\n"
        "    fi\n"
        '    exit "$rc"\n'
        "    ;;\n"
        "  *)\n"
        '    exec "$REAL_APK" "$@"\n'
        "    ;;\n"
        "esac\n"
    )


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
    _write_if_missing(runtime.apk_state_file, "")
    _write_executable(runtime.apk_wrapper_file, _build_apk_wrapper(runtime.apk_state_file))
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
    env["HACTRL_APK_STATE_FILE"] = str(runtime.apk_state_file)
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


def _read_apk_state(runtime: CLIRuntimePaths) -> list[str]:
    if not runtime.apk_state_file.exists():
        return []
    packages: list[str] = []
    seen: set[str] = set()
    try:
        lines = runtime.apk_state_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        value = line.strip()
        if not value or value.startswith("#") or value in seen:
            continue
        seen.add(value)
        packages.append(value)
    return packages


def _missing_apk_packages(packages: list[str], env: dict[str, str]) -> list[str]:
    missing: list[str] = []
    for spec in packages:
        key = _apk_presence_key(spec)
        code, _stdout, _stderr = _run_cmd(["/sbin/apk", "info", "-e", key], env=env, timeout_s=30)
        if code != 0:
            missing.append(spec)
    return missing


def bootstrap_cli_tools(
    *,
    runtime: CLIRuntimePaths,
    enabled: bool,
    npm_packages: list[str],
    pipx_packages: list[str],
    persist_history: bool,
) -> dict[str, Any]:
    tracked_apk_packages = _read_apk_state(runtime)
    manifest = {
        "generated_at": _now_iso(),
        "enabled": enabled,
        "apk_packages_tracked": tracked_apk_packages,
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
        "apk": {"tracked": tracked_apk_packages, "installed": [], "failed": []},
        "npm": {"requested": manifest["npm_packages"], "installed": [], "failed": []},
        "pipx": {"requested": manifest["pipx_packages"], "installed": [], "failed": []},
    }
    if not enabled:
        _write_json(runtime.bootstrap_status_file, status)
        return status

    env = apply_cli_runtime_env(base_env=os.environ.copy(), runtime=runtime, persist_history=persist_history)

    if tracked_apk_packages:
        missing_apk = _missing_apk_packages(tracked_apk_packages, env)
        if missing_apk:
            code, _stdout, stderr = _run_cmd(["/sbin/apk", "add", "--no-cache", *missing_apk], env=env, timeout_s=1800)
            if code == 0:
                status["apk"]["installed"].extend(missing_apk)
            else:
                status["ok"] = False
                status["apk"]["failed"].append({"packages": missing_apk, "error": stderr.strip()[:500]})

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
        f"apk_tracked={','.join(tracked_apk_packages)}",
        f"npm_requested={','.join(manifest['npm_packages'])}",
        f"pipx_requested={','.join(manifest['pipx_packages'])}",
    ]
    runtime.bootstrap_lock_file.write_text("\n".join(lock_lines) + "\n", encoding="utf-8")
    _write_json(runtime.bootstrap_status_file, status)
    return status
