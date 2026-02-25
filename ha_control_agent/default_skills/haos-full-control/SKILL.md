---
name: haos-full-control
description: Execute full Home Assistant OS control workflows through the ha-control agent and CLI. Use when tasks require end-to-end CLI control of HAOS over LAN, including entity/automation/dashboard edits, add-on lifecycle, OS/host operations, filesystem edits, raw Supervisor/Core API calls, and recovery/rollback.
---

# HAOS Full Control

Use this skill to operate Home Assistant OS from CLI with `ha-control` and `ha-control-agent`.

## Runtime Baseline

- Target add-on version: `0.1.12` or newer.
- Add-on should run with `protected: false` for full-control mode.
- If Supervisor token is not auto-injected, set add-on option `supervisor_token`.
- Validate with:
  - `ha-control capabilities` -> `supervisor_token_available: true`

## Execute Workflow

1. Verify prerequisites.
2. Ensure add-on runtime mode is correct.
3. Authenticate and establish session.
4. Check capabilities before mutating state.
5. Run task-specific command set.
6. Verify state and audit logs.
7. Roll back when needed.

## Verify Prerequisites

- Confirm agent is reachable: `curl http://<ha-ip>:9123/health`.
- Confirm CLI config path exists or pass `--agent` and `--token` explicitly.
- Assume trusted LAN deployment only.

## Verify Add-on Mode

- In Home Assistant Add-on UI:
  - `protected` disabled
  - add-on started
  - (optional fallback) `supervisor_token` configured
- If runtime behavior is stale, force refresh:
  - update add-on in UI
  - restart add-on
  - if needed, reinstall add-on from repository

## Authenticate

- Create session with LLAT:
  - `ha-control auth login --agent http://<ha-ip>:9123 --long-lived-token <llat>`
- Validate session:
  - `ha-control auth me`

## Check Capabilities

- Run `ha-control capabilities` before operations.
- If required feature is unavailable, stop and troubleshoot before mutation.

## Run Operations

Use task recipes from [references/command-matrix.md](references/command-matrix.md).

Priority order for control paths:
1. Use typed commands (`entity`, `automation`, `dashboard`, `addon`, `os`, `host`, `fs`).
2. Use `raw` commands for unsupported/advanced endpoints.
3. Use `exec` only when API-level control is insufficient.
4. For filesystem host paths, prefer `/mnt/data/supervisor/...`; if namespace access is blocked, agent can fall back to mapped mounted paths and return `fallback_mode: mapped_local`.

## Enforce Safety

- Require explicit confirmation for destructive operations unless user supplied `--yes`.
- Snapshot/backup before high-impact filesystem or OS changes.
- Write and inspect audit entries after mutating actions.

Use [references/safety.md](references/safety.md) for policy details.

## Recover

- Use runbook-style troubleshooting for failed auth, proxy errors, filesystem failures, or WS command errors.
- Restore previous config/files when post-change verification fails.

Use [references/troubleshooting.md](references/troubleshooting.md).

## Output Rules

- Prefer JSON output for machine-checkable steps.
- For each completed task, report:
  - exact command(s) executed
  - affected resource IDs/paths
  - verification result
  - rollback status if applied
