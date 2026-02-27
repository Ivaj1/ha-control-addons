# HA Control Add-ons

Home Assistant add-on repository for full LAN control-plane workflows.

## Add-ons

### `ha_control_agent`

Privileged API agent for full CLI control of Home Assistant OS.

### `recorder_visual_control`

Visual ingress app to enable/disable recorder (and therefore new history capture) with one click.

## Install in Home Assistant

1. Open `Settings` -> `Add-ons` -> `Add-on Store`.
2. Open the menu (top-right) -> `Repositories`.
3. Add this repository URL:
   - `https://github.com/Ivaj1/ha-control-addons`
4. Install the add-on you need:
   - **HA Control Agent**
   - **Recorder Visual Control**

## Security note

This repository contains a high-privilege add-on (`ha_control_agent`, `protected: false`, `full_access: true`).
Use only on isolated trusted LAN environments.
