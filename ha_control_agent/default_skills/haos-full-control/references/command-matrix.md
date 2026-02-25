# Command Matrix

## Auth and capability

- Login: `ha-control auth login --agent http://<ha-ip>:9123 --long-lived-token <llat>`
- Session inspect: `ha-control auth me`
- Capability map: `ha-control capabilities`
- Health check: `curl http://<ha-ip>:9123/health`
- Required capability state for full control: `supervisor_token_available: true`

## Entity and registries

- Rename entity: `ha-control entity rename <entity_id> "<new name>"`
- Registry update: `ha-control entity update <entity_id> --json '{"name":"<new name>"}'`
- Raw entity registry list: `ha-control raw ws --json '{"id":1,"type":"config/entity_registry/list"}'`

## Automations, scripts, scenes

- Get: `ha-control automation get <id>`
- Apply: `ha-control automation apply <id> --file <automation.json> --yes`
- Delete: `ha-control automation delete <id> --yes`
- Reload: `ha-control automation reload`
- Get: `ha-control script get <id>`
- Apply: `ha-control script apply <id> --file <script.json> --yes`
- Delete: `ha-control script delete <id> --yes`
- Reload: `ha-control script reload`
- Get: `ha-control scene get <id>`
- Apply: `ha-control scene apply <id> --file <scene.json> --yes`
- Delete: `ha-control scene delete <id> --yes`
- Reload: `ha-control scene reload`

## Dashboards and panels

- List dashboards: `ha-control dashboard list`
- Create dashboard: `ha-control dashboard create --url-path <slug> --title "<title>" --yes`
- Update dashboard: `ha-control dashboard update <dashboard_id> --json '{"title":"<new title>"}' --yes`
- Get dashboard config: `ha-control dashboard get-config --url-path <slug>`
- Save dashboard config: `ha-control dashboard save-config --url-path <slug> --file <dashboard.json> --yes`
- Manage resources: `ha-control dashboard resources <list|create|update|delete> --json '<payload>' [--yes]`

## Add-ons

- List add-ons: `ha-control addon list`
- Add-on info: `ha-control addon info <slug>`
- Install add-on: `ha-control addon install <slug> --yes`
- Update add-on: `ha-control addon update <slug> [--version <version>] --yes`
- Start add-on: `ha-control addon start <slug>`
- Stop add-on: `ha-control addon stop <slug>`
- Add-on logs: `ha-control addon logs <slug> [--lines 200]`
- Update options: `ha-control addon options <slug> --file <options.json> --yes`

## OS and host

- OS info: `ha-control os info`
- OS update: `ha-control os update [--version <version>] --yes`
- Datadisk list: `ha-control os datadisk list`
- Datadisk move: `ha-control os datadisk move --device <device> --yes`
- Datadisk wipe: `ha-control os datadisk wipe [--device <device>] --yes`
- Swap info: `ha-control os swap info`
- Swap set: `ha-control os swap set --json '<payload>' --yes`
- Boot slot: `ha-control os boot-slot <A|B> --yes`
- Host info: `ha-control host info`
- Host reboot: `ha-control host reboot --yes`
- Host shutdown: `ha-control host shutdown --yes`
- Host services: `ha-control host services`
- Host logs: `ha-control host logs [--identifier <name>] [--lines 200]`
- Network info: `ha-control host network-info`
- Network update: `ha-control host network-update --interface <ifname> --file <iface.json> --yes`

## Filesystem and host command execution

- Tree: `ha-control fs tree <abs-path> --max-depth 2`
- Read: `ha-control fs read <abs-path>`
- Write: `ha-control fs write <abs-path> --file <local-file> --yes`
- Move: `ha-control fs move <src> <dst> --yes`
- Delete: `ha-control fs delete <abs-path> [--recursive] --yes`
- Host namespace command: `ha-control exec -- <cmd> <args...>`
- Detect mapped fallback: responses may include `fallback_mode: mapped_local` when host namespace is blocked.

## Add-on runtime checks

- If Supervisor proxy fails with token issues:
  - Set add-on option `supervisor_token`.
  - Restart add-on and recheck `ha-control capabilities`.
- If add-on startup is unstable:
  - Reinstall/refresh from `https://github.com/Ivaj1/ha-control-addons`.

## Raw passthrough

- Supervisor: `ha-control raw supervisor <METHOD> <path> [--json <payload>]`
- Core REST: `ha-control raw core <METHOD> <path> [--json <payload>]`
- Core WS typed envelope: `ha-control raw ws --json '{"id":1,"type":"config/entity_registry/list"}'`
- Core WS raw frame passthrough:  
  `ha-control raw ws --json '{"id":2,"type":"config/entity_registry/update","entity_id":"sensor.temp","name":"Temp"}'`
