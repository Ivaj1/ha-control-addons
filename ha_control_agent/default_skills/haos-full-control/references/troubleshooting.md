# Troubleshooting

## Auth failures

- Symptom: `HTTP 401` on `auth login`.
- Actions:
  - verify LLAT validity in Home Assistant profile
  - verify agent and Home Assistant reachability
  - retry login and refresh saved token

## Session failures

- Symptom: `Invalid or expired session token`.
- Actions:
  - run `ha-control auth login` again
  - verify CLI is reading the expected config file

## Capability mismatch

- Symptom: command fails because feature unavailable.
- Actions:
  - run `ha-control capabilities`
  - disable unsupported workflow branch
  - use raw fallback only if safe

## Proxy failures

- Symptom: `HTTP 502` or upstream errors on raw/core/supervisor.
- Actions:
  - check supervisor/core health from host
  - verify `ha-control capabilities` and confirm `supervisor_token_available: true`
  - if false, set add-on option `supervisor_token` and restart add-on
  - retry minimal GET endpoint before mutation

## Empty bearer token failures

- Symptom: traceback contains `Illegal header value b'Bearer '`.
- Actions:
  - ensure add-on is updated to current repository version
  - open add-on configuration and set `supervisor_token`
  - restart add-on and retest `raw supervisor GET info`

## Filesystem failures

- Symptom: write/move/delete fails with permissions/path errors.
- Actions:
  - validate absolute path
  - check host namespace mode selection
  - verify parent directory exists or use create-dir path
  - if host namespace is blocked, use mapped paths under `/mnt/data/supervisor/...` and verify response `fallback_mode: mapped_local`

## Host namespace failures

- Symptom: `nsenter: reassociate to namespaces failed: Operation not permitted`.
- Actions:
  - confirm add-on runs with `protected: false`
  - ensure add-on is current version (includes compatibility namespace settings)
  - rely on mapped filesystem fallback for file operations if host namespace remains restricted

## WebSocket command failures

- Symptom: WS response error or timeout.
- Actions:
  - verify command type and payload schema
  - set explicit `id` in raw WS payload
  - retry with minimal known-good command

## Recovery

- Restore previous file/content or API payload snapshot.
- Re-check affected component state.
- Stop further mutations until verification is clean.

## Add-on build/startup failures

- Symptom: add-on fails to build/start after repo updates.
- Actions:
  - remove and re-add repository URL in Add-on Store
  - reinstall add-on to force fresh local build
  - disable watchdog during debugging to avoid restart loops masking root cause
