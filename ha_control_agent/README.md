# HA Control Agent

Privileged Home Assistant OS add-on that exposes a local control-plane API for full CLI automation.

## UI Console

- Home Assistant panel via Ingress: open the add-on and click `Open Web UI`.
- Console routes: `/` and `/console`.
- WebSocket shell bridge: `/console/ws`.
- Direct LAN access supports `Authorization: Bearer <session_token>` or `?token=<session_token>`.

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
