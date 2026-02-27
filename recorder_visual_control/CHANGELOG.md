# Changelog

## 0.7.0

- Pixel-focused redesign to replicate Home Assistant Devices panel layout:
  - top navigation tabs
  - left filter sidebar sections
  - search + group/order toolbar
  - dark table styling with grouped rows
  - floating action button style
  - column customization modal style
  - entity details right drawer
- Added client-side multi-filter behavior by:
  - areas
  - integrations
  - state
  - tags

## 0.6.0

- Database detection aligned with `hass-dbstats` approach:
  - recursive YAML loading
  - `!include*` traversal
  - `!secret` replacement with nearest/upper `secrets.yaml`
  - recorder lookup via deep config search
- Added DB connection override option:
  - `connection_string` in add-on options (dbstats-like fallback)
- Improved MySQL/MariaDB connection parsing:
  - supports `unix_socket` query parameter in `db_url`

## 0.5.0

- New professional dark UI with full control dashboard.
- Added entities-style filtering and sorting:
  - search
  - domain filter
  - included/excluded filter
  - sort field + direction
  - clickable sortable table headers
- Added entity details panel:
  - current state and key attributes
  - detailed recorder activity windows (1h, 24h, 7d)
  - direct button to open entity page in Home Assistant.
- Added entity details API endpoint: `/api/entity/{entity_id}/details`.

## 0.4.1

- Reworked metrics backend to avoid heavy logbook queries that can overload Home Assistant Core.
- Metrics now follow `dbstats` style:
  - SQLite direct queries when local DB is present.
  - MariaDB/MySQL direct queries using recorder `db_url`.
- Added recorder service controls in UI:
  - enable/disable
  - global purge
  - purge by selected entities/domains/globs

## 0.4.0

- Added visual recorder controls:
  - `recorder.enable`
  - `recorder.disable`
- Added visual purge controls:
  - Global `recorder.purge` with `keep_days`, `repack`, `apply_filter`
  - `recorder.purge_entities` for selected entities, domains, or globs
- Added table multi-select to launch purge for selected entities.
- Updated UI styling to align better with Home Assistant look and theme variables.

## 0.3.0

- Added activity metrics per entity: changes in selected window, changes/hour and last write.
- Added metrics window selector (1h, 6h, 24h, 72h, 7d).
- Added dual metrics backend:
  - SQLite direct query when `home-assistant_v2.db` is local.
  - Logbook API fallback for external DB setups (e.g., MariaDB).
- Sorted entity list by activity to identify high-write entities quickly.

## 0.2.0

- Reworked add-on to manage recorder inclusion/exclusion by entity.
- Added entity browser with search and exclude/include toggles.
- Added managed list file at `/homeassistant/recorder_exclude_entities/recorder_visual_control.yaml`.
- Added setup check for `!include_dir_merge_list recorder_exclude_entities`.
- Added "Apply" action that requests Home Assistant Core restart.

## 0.1.0

- Initial release.
- Added visual ingress app to enable/disable Home Assistant recorder.
- Added live status read from `recorder/info` over Home Assistant WebSocket API.
