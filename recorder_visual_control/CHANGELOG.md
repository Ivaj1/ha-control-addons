# Changelog

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
