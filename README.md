# HA Control Add-ons

Home Assistant add-on repository for full LAN control-plane workflows.

## Add-ons

### `ha_control_agent`

Privileged API agent for full CLI control of Home Assistant OS.

## Install in Home Assistant

1. Open `Settings` -> `Add-ons` -> `Add-on Store`.
2. Open the menu (top-right) -> `Repositories`.
3. Add this repository URL:
   - `https://github.com/ivaj/ha-control-addons`
4. Install **HA Control Agent**.
5. Start the add-on and check `http://<ha-ip>:9123/health`.

## Security note

This repository contains a high-privilege add-on (`protected: false`, `full_access: true`).
Use only on isolated trusted LAN environments.
