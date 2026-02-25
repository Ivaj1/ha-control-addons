# Changelog

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
