# ha-control CLI

Task-oriented CLI for full Home Assistant OS control through `ha-control-agent`.

## Install and run

```bash
cd ha-control-cli
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
ha-control --help
```

## Bootstrap auth

```bash
ha-control auth login --agent http://HA_IP:9123 --long-lived-token <LLAT>
ha-control auth me
ha-control capabilities
```

## Examples

```bash
ha-control entity rename sensor.temp "Living Room Temperature"
ha-control automation apply 1771289770451 --file ./automation.json --yes
ha-control dashboard create --url-path ops --title "Ops Dashboard" --yes
ha-control addon install ssh --yes
ha-control os update --yes
ha-control host reboot --yes
ha-control fs read /mnt/data/supervisor/homeassistant/configuration.yaml
ha-control fs write /mnt/data/supervisor/homeassistant/configuration.yaml --file ./configuration.yaml --yes
```

## Safety and output

- Use `--confirm` or `--yes` for destructive operations.
- Use `--dry-run` to preview supported mutation requests.
- Use `--output json|yaml|table|raw` for machine/human-friendly output.
