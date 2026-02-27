# Recorder Visual Control

Controla visualmente el `recorder` por entidad/sensor desde un panel sencillo.

## Qué hace

- Lista entidades de Home Assistant.
- Filtros y ordenación tipo panel de entidades:
  - búsqueda
  - filtro por dominio
  - filtro incluidas/excluidas
  - orden por columnas
- Permite excluir o incluir entidades para recorder desde UI.
- Guarda la lista gestionada por la app en:
  - `/homeassistant/recorder_exclude_entities/recorder_visual_control.yaml`
- Muestra estado real del recorder usando `recorder/info`.
- Muestra métricas de actividad por entidad:
  - cambios en ventana de tiempo
  - cambios por hora
  - última escritura detectada
- Incluye botón para aplicar cambios solicitando reinicio de Core.
- Incluye panel de detalle por entidad con métricas de 1h, 24h y 7d y acceso directo a la entidad en Home Assistant.

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

## Métricas con MariaDB

Si usas SQLite local (`home-assistant_v2.db`), la app usa consulta directa a SQLite.
Si usas MariaDB/MySQL en `recorder.db_url`, la app consulta esa base directamente.

### Detección de DB (estilo dbstats)

La detección de `recorder.db_url` sigue el enfoque de `hass-dbstats`:
- parseo recursivo de YAML
- resolución de `!include*`
- reemplazo de `!secret` con `secrets.yaml`

Además puedes forzar conexión en opciones del add-on:
- `connection_string`

## Instalación

1. En Home Assistant, abre `Settings` -> `Add-ons` -> `Add-on Store`.
2. Menú superior derecho -> `Repositories`.
3. Añade tu repositorio: `https://github.com/Ivaj1/ha-control-addons`
4. Instala **Recorder Visual Control**.
5. Inicia el add-on y abre **Open Web UI** (ingress).
