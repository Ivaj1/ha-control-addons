# Safety Rules

## Enforce confirmation

Require confirmation for:
- file writes/moves/deletes
- automation deletes
- dashboard deletes
- OS updates
- host reboot/shutdown

## Minimize blast radius

- Prefer typed API commands over raw or shell commands.
- Scope path operations to exact files first, then directories if needed.
- Avoid special paths (`/proc`, `/sys`, `/dev`) unless explicitly authorized.
- Do not run broad host-level commands before confirming `ha-control capabilities`.

## Verify after mutation

- Run an immediate read-back/API fetch after every mutation.
- Confirm UI-visible state where applicable (entity name, dashboard existence, add-on state).
- For filesystem mutations, verify `backup_path` and hash fields in response.

## Preserve rollback path

- Capture before-state payload/file before mutating.
- Keep rollback command ready before executing high-impact changes.

## Audit expectations

- Ensure mutating operations write entries to `/share/ha-control/audit.log`.
- Include actor, action, target, and success/failure in review output.
- If response contains `fallback_mode: mapped_local`, include that explicitly in change report.
