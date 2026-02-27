# Changelog

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
