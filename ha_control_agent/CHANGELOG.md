# Changelog

## 0.2.14

- Added persistent CLI runtime root configuration (`cli_persistence_root`, default `/share/ha-control/cli`).
- Console sessions now run with persistent HOME/XDG/history environment so shell state survives restart/update.
- Added managed CLI bootstrap options:
  - `cli_bootstrap_enabled`
  - `cli_bootstrap_npm_packages`
  - `cli_bootstrap_pipx_packages`
  - `cli_persist_history`
- Added startup bootstrap status/manifest files under persistent CLI state.
- Added `git` and `pipx` to runtime image to support persistent npm/pipx tool workflows.

## 0.2.13

- Added optional HTTPS listener for WebDAV using stunnel and `/ssl` certs.
- Added HTTPS options: `webdav_https_enabled`, `webdav_https_port`, `webdav_https_cert`, `webdav_https_key`.
- Keeps existing HTTP API/console on `9123` while enabling TLS WebDAV compatibility path for Windows.
- Updated console UI to a Windows-like terminal style (Cascadia/Consolas theme and palette).
- Simplified console to a minimal top bar and keyboard-driven controls.
- Hid visible scrollbar rail in terminal viewport for cleaner look.
- Removed custom clipboard/shortcut interception to behave like a normal terminal session.
- Changed console to full-screen plain CMD-like view (no top bars/titles).
- Added persistent backend console sessions keyed by client id, so page reload/navigation reconnects to the same shell session.
- Added native SMB server in add-on (Samba) with configurable full-host share.
- New SMB options: `smb_enabled`, `smb_username`, `smb_password`, `smb_share_name`, `smb_root`, `smb_read_only`, `smb_allow_hosts`.
- Default SMB root is `/proc/1/root` for complete host filesystem access.
- Fixed SMB listen configuration: no longer binds only to loopback (`lo`), making Windows LAN access possible.
- Added `codex_seed_defaults` option (`true` by default). Set it to `false` to prevent startup seeding of Codex skills and `AGENTS.md`/`agent.md`.

## 0.2.7

- Added WebDAV server endpoints under `/webdav` for Windows Explorer-style file browsing/editing.
- Added WebDAV add-on options: `webdav_enabled`, `webdav_username`, `webdav_password`, `webdav_root`, `webdav_host_namespace`, `webdav_read_only`.
- Added Codex bootstrap `AGENTS.md` and `agent.md` in `$CODEX_HOME` with Home Assistant + skill usage instructions.

## 0.2.6

- Bundled and installed `ha-control-cli` inside the add-on image.
- `ha-control` command is now available directly in the web console shell.

## 0.2.5

- Redesigned web console UI with a modern terminal toolbar (connect/disconnect, copy, paste, clear, Ctrl+C, font sizing).
- Improved copy/paste reliability and keyboard shortcuts (`Ctrl+Shift+C`, `Ctrl+Shift+V`).
- Improved terminal sizing with FitAddon-based resizing for better interactive CLI behavior.

## 0.2.4

- Fixed web console TUI behavior (including `codex`) by adding terminal resize support over WebSocket.
- Added PTY window-size initialization and resize signal forwarding (`SIGWINCH`).

## 0.2.3

- Added persistent Codex home support with new `codex_home` option (default `/share/codex`).
- Added startup bootstrap to create `$CODEX_HOME/skills` and seed bundled default skills.
- Removed login-shell startup warning in console (`/bin/sh: can't access tty`) by adjusting shell launch mode.

## 0.2.2

- Fixed console behavior for Codex CLI: default console now runs in add-on container namespace, where `codex` is installed.
- Added `console_host_namespace` option (`false` by default). Set `true` only when you explicitly want host shell.

## 0.2.1

- Fixed add-on `build.yaml` image references to fully-qualified names required by Supervisor validation.

## 0.2.0

- Added Home Assistant Ingress panel (`ingress: true`) with embedded web console at `/` and `/console`.
- Added terminal WebSocket bridge at `/console/ws` with trusted-LAN checks and session/ingress auth.
- Added add-on options for console runtime (`console_shell`, `console_cwd`) and OpenAI key (`openai_api_key`).
- Added Codex CLI install in image (`@openai/codex`) and capability flag (`features.codex_cli`).

## 0.1.12

- Added additional Linux capabilities (`SYS_PTRACE`, `DAC_READ_SEARCH`) for host namespace control in `protected: false` mode.
- Reduced `nsenter` namespaces to mount+pid for better compatibility.
- Added filesystem fallback: if `nsenter` is blocked, operations automatically fall back to mapped mounted paths (`/homeassistant`, `/addons`, `/share`, `/backup`, `/media`, `/ssl`, `/addon_configs`).

## 0.1.11

- Switched runtime image to `python:3.12-alpine` without s6 overlay, eliminating `s6-overlay-suexec: can only run as pid 1` when running with `host_pid: true` and `protected: false`.

## 0.1.10

- Fixed build on Home Assistant base image by explicitly installing `python3` and `py3-pip` before dependency install.

## 0.1.9

- Fixed build failure on Home Assistant base image by using `pip3`/`python3`.
- Restored official Home Assistant base images in `build.yaml`/`Dockerfile` for consistent Supervisor local builds.

## 0.1.8

- Switched add-on runtime base image to `python:3.12-alpine` (no s6 overlay), avoiding `s6-overlay-suexec: can only run as pid 1` when `host_pid: true` + `protected: false`.

## 0.1.7

- Removed `with-contenv` runtime wrapper to avoid `s6-overlay-suexec: can only run as pid 1` restart loops.
- Added fallback token discovery from s6 container environment files (`/run/s6/container_environment/SUPERVISOR_TOKEN` and `HASSIO_TOKEN`).

## 0.1.6

- Added `SYS_ADMIN` capability to enable host namespace operations (`nsenter`) for full CLI control.
- Added optional `supervisor_token` add-on setting as manual fallback when Supervisor env token is not injected.

## 0.1.5

- Launch agent with `with-contenv` so s6-provided environment variables (`SUPERVISOR_TOKEN` / `HASSIO_TOKEN`) are available to Uvicorn process.

## 0.1.4

- Added proxy-level guard to reject Supervisor/Core proxy calls when Supervisor token is missing, preventing invalid `Bearer ` headers.

## 0.1.3

- Read Supervisor token from `SUPERVISOR_TOKEN` or legacy `HASSIO_TOKEN`.
- Return clear `503` error when Supervisor token is missing instead of sending an empty Authorization header.

## 0.1.2

- Disabled AppArmor profile for this add-on (`apparmor: false`) to allow required host namespace operations (`nsenter`).

## 0.1.1

- Switched to local build mode for custom repository installs (removed `image` field).
- Updated add-on URL metadata to `Ivaj1/ha-control-addons`.

## 0.1.0

- Initial release.
- Added full LAN control-plane API for HAOS.
- Added filesystem CRUD with backup snapshots.
- Added host namespace command execution with policy checks.
- Added Supervisor/Core REST passthrough and Core WS bridge.
- Added session auth, capability discovery, and mutation audit logging.
