#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SRC_DIR="${1:-$ROOT_DIR/../ha-control-agent}"
DST_DIR="$ROOT_DIR/ha_control_agent"

if [[ ! -d "$SRC_DIR" ]]; then
  echo "Source directory not found: $SRC_DIR" >&2
  exit 1
fi

mkdir -p "$DST_DIR"

cp -r "$SRC_DIR/app" "$DST_DIR/"
cp "$SRC_DIR/Dockerfile" "$SRC_DIR/requirements.txt" "$SRC_DIR/build.yaml" "$SRC_DIR/config.yaml" "$SRC_DIR/README.md" "$DST_DIR/"

CONFIG="$DST_DIR/config.yaml"

# Keep add-on repository metadata stable after source sync.
if grep -q '^url:' "$CONFIG"; then
  sed -i 's|^url:.*$|url: https://github.com/ivaj/ha-control-addons/tree/main/ha_control_agent|' "$CONFIG"
else
  printf '\nurl: https://github.com/ivaj/ha-control-addons/tree/main/ha_control_agent\n' >> "$CONFIG"
fi

if grep -q '^image:' "$CONFIG"; then
  sed -i 's|^image:.*$|image: ghcr.io/ivaj/{arch}-addon-ha-control-agent|' "$CONFIG"
else
  sed -i '/^version:/a image: ghcr.io/ivaj/{arch}-addon-ha-control-agent' "$CONFIG"
fi

echo "Synced HA Control Agent from: $SRC_DIR"
echo "Into add-on repo path: $DST_DIR"
