# HA Control Agent

High-privilege local control plane for Home Assistant OS.

## Features

- LLAT bootstrap -> local session token
- Full local filesystem CRUD for host namespace
- Host command execution via `nsenter`
- Supervisor and Core REST passthrough
- Core WebSocket command bridge
- Audit log and backup snapshots

## API

- `POST /v1/auth/token`
- `GET /v1/auth/me`
- `GET /v1/capabilities`
- `GET /v1/fs/tree`
- `GET /v1/fs/read`
- `PUT /v1/fs/write`
- `POST /v1/fs/move`
- `DELETE /v1/fs/delete`
- `POST /v1/exec`
- `/v1/supervisor/{path}`
- `/v1/core/rest/{path}`
- `POST /v1/core/ws`

## Configuration

- `trusted_cidrs` (list of CIDR strings)
- `session_ttl_seconds` (60..604800)
- `allow_unverified_bootstrap` (bool)
- `unsafe_allow_exec` (bool)
- `unsafe_allow_special_paths` (bool)

## Security warning

This add-on is intentionally high privilege (`full_access: true`, `protected: false`).
Run only on trusted LAN segments.
