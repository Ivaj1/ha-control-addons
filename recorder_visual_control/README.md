# Recorder Visual Control

Home Assistant add-on that provides a visual panel to include/exclude entities from recorder.

- Ingress UI inside Home Assistant.
- Search entities from `/api/states`.
- Advanced table filters and sorting (domain, inclusion state, sort fields).
- Exclude/include per entity, managed in a dedicated YAML list file.
- Activity metrics per entity (changes, changes/hour, last write) with selectable time window.
- Entity detail panel with 1h/24h/7d statistics and direct link to HA entity page.
- Recorder controls: enable/disable from UI.
- Purge controls: global purge and purge by selected entities/domains/globs.
- Apply changes with Home Assistant Core restart.
