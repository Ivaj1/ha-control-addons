# Changelog

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
