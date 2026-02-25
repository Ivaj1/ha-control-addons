# HA Control Agent

Privileged Home Assistant OS add-on that exposes a local control-plane API for full CLI automation.

## UI Console

- Home Assistant panel via Ingress: open the add-on and click `Open Web UI`.
- Console routes: `/` and `/console`.
- WebSocket shell bridge: `/console/ws`.
- Modern terminal toolbar with connect/disconnect, copy/paste, clear, Ctrl+C, and font-size controls.
- Direct LAN access supports `Authorization: Bearer <session_token>` or `?token=<session_token>`.
- `console_host_namespace: false` (default) runs console inside add-on container (recommended for `codex`).
- `console_host_namespace: true` switches console to host shell via `nsenter`.

## API surface

- `POST /v1/auth/token`
- `GET /v1/auth/me`
- `GET /v1/capabilities`
- `GET /v1/fs/tree`
- `GET /v1/fs/read`
- `PUT /v1/fs/write`
- `POST /v1/fs/move`
- `DELETE /v1/fs/delete`
- `POST /v1/exec`
- `/v1/supervisor/{path}` method passthrough
- `/v1/core/rest/{path}` method passthrough
- `POST /v1/core/ws` typed envelope or raw `message`

## Codex CLI

- Installed inside the add-on image as global command: `codex`.
- Configure `openai_api_key` in add-on options.
- `codex_home` defaults to `/share/codex` (persistent).
- On startup, the add-on creates `$codex_home/skills` and seeds bundled default skills.
- Set `codex_seed_defaults: false` to fully disable startup seeding of `skills`, `AGENTS.md`, and `agent.md`.
- `ha-control` CLI is bundled in the image and available directly in the add-on console.
- On startup, `$codex_home/AGENTS.md` and `$codex_home/agent.md` are created to instruct Codex to use skills and Home Assistant context.

## CLI Persistence

- CLI runtime state is persisted under `cli_persistence_root` (default `/share/ha-control/cli`).
- Persistent shell state includes HOME, history, XDG config/cache/data, and user bin paths.
- Managed startup bootstrap can install missing CLI tools into persistent locations:
  - `cli_bootstrap_npm_packages` (installed with npm global prefix under `/share`)
  - `cli_bootstrap_pipx_packages` (installed with pipx home/bin under `/share`)
- Useful options:
  - `cli_bootstrap_enabled: true|false`
  - `cli_persist_history: true|false`
- Non-persistent by design: direct system package changes inside the container (for example `apk add ...` in an interactive shell).

## WebDAV (Windows Explorer)

- Endpoint: `http://<HA_IP>:9123/webdav/`
- Authentication: Basic auth using add-on options `webdav_username` + `webdav_password`.
- Full filesystem exposure when `webdav_host_namespace: true` and `webdav_root: /`.
- Optional safety mode: `webdav_read_only: true`.
- Optional HTTPS endpoint for better Windows compatibility:
  - Enable `webdav_https_enabled: true`
  - Use `https://<HA_IP>:<webdav_https_port>/webdav/` (default port `9443`)
  - Cert/key options: `webdav_https_cert`, `webdav_https_key`

## SMB (Windows native)

- Native SMB server is available in this add-on for Windows Explorer compatibility.
- Configure:
  - `smb_enabled: true`
  - `smb_username` / `smb_password`
  - `smb_share_name` (default `haos-root`)
  - `smb_root` (default `/proc/1/root`, full host filesystem)
  - `smb_read_only` optional
- Connect from Windows:
  - `\\\\<HA_IP>\\<smb_share_name>`
  - Example: `\\\\192.168.1.44\\haos-root`

## Security posture

- High privilege by design (`full_access: true`, `protected: false`).
- Trusted-LAN CIDR gate on all endpoints.
- Session tokens minted from Home Assistant long-lived access token (LLAT).
- Mutating operations audited to `/share/ha-control/audit.log`.
- Destructive file operations create backups under `/backup/ha-control`.

## Development

```bash
cd ha-control-agent
python3 -m compileall app
```
