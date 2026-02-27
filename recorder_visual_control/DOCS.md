# Recorder Visual Control

Controla visualmente el `recorder` por entidad/sensor desde un panel sencillo.

## Qué hace

- Lista entidades de Home Assistant.
- Permite excluir o incluir entidades para recorder desde UI.
- Guarda la lista gestionada por la app en:
  - `/homeassistant/recorder_exclude_entities/recorder_visual_control.yaml`
- Muestra estado real del recorder usando `recorder/info`.
- Incluye botón para aplicar cambios solicitando reinicio de Core.

## Configuración necesaria (una vez)

Añade en `configuration.yaml`:

```yaml
recorder:
  exclude:
    entities: !include_dir_merge_list recorder_exclude_entities
```

Esto permite que el add-on escriba su lista de exclusión sin pisar otras listas.

## Nota importante

Los cambios de filtros de `recorder` se aplican al reiniciar Home Assistant Core.

## Instalación

1. En Home Assistant, abre `Settings` -> `Add-ons` -> `Add-on Store`.
2. Menú superior derecho -> `Repositories`.
3. Añade tu repositorio: `https://github.com/Ivaj1/ha-control-addons`
4. Instala **Recorder Visual Control**.
5. Inicia el add-on y abre **Open Web UI** (ingress).
