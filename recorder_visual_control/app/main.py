import json
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import httpx
import pymysql
import websockets
import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")
CORE_HTTP_BASE = os.environ.get("CORE_HTTP_BASE", "http://supervisor/core")
CORE_WS_URL = os.environ.get("CORE_WS_URL", "ws://supervisor/core/websocket")
REQUEST_TIMEOUT = 25.0

HA_CONFIG_DIR = Path("/homeassistant")
CONFIG_YAML = HA_CONFIG_DIR / "configuration.yaml"
RECORDER_EXCLUDE_DIR = HA_CONFIG_DIR / "recorder_exclude_entities"
MANAGED_LIST_FILE = RECORDER_EXCLUDE_DIR / "recorder_visual_control.yaml"
SETUP_PATTERN = "!include_dir_merge_list recorder_exclude_entities"

app = FastAPI(title="Recorder Visual Control", version="0.6.0")


class TogglePayload(BaseModel):
    exclude: bool = Field(..., description="True to exclude from recorder")


class RecorderPurgePayload(BaseModel):
    keep_days: int | None = Field(default=None, ge=1, le=3650)
    repack: bool = False
    apply_filter: bool = False


class RecorderPurgeEntitiesPayload(BaseModel):
    entity_ids: list[str] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)
    entity_globs: list[str] = Field(default_factory=list)
    keep_days: int = Field(default=0, ge=0, le=3650)


def _auth_headers() -> dict[str, str]:
    if not SUPERVISOR_TOKEN:
        raise HTTPException(status_code=500, detail="Missing SUPERVISOR_TOKEN")
    return {
        "Authorization": f"Bearer {SUPERVISOR_TOKEN}",
        "Content-Type": "application/json",
    }


def _ensure_storage() -> None:
    RECORDER_EXCLUDE_DIR.mkdir(parents=True, exist_ok=True)
    if not MANAGED_LIST_FILE.exists():
        MANAGED_LIST_FILE.write_text("[]\n", encoding="utf-8")


def _load_excluded_entities() -> set[str]:
    _ensure_storage()
    raw = MANAGED_LIST_FILE.read_text(encoding="utf-8")
    data = yaml.safe_load(raw) if raw.strip() else []
    if not isinstance(data, list):
        raise HTTPException(
            status_code=500,
            detail=f"Invalid managed file format: {MANAGED_LIST_FILE}",
        )
    return {str(item).strip() for item in data if str(item).strip()}


def _save_excluded_entities(entity_ids: set[str]) -> None:
    _ensure_storage()
    ordered = sorted(entity_ids)
    MANAGED_LIST_FILE.write_text(
        yaml.safe_dump(ordered, sort_keys=False, allow_unicode=False),
        encoding="utf-8",
    )


def _setup_status() -> dict[str, Any]:
    if not CONFIG_YAML.exists():
        return {
            "configured": False,
            "detail": "No se encontro configuration.yaml en /homeassistant",
        }
    conf_text = CONFIG_YAML.read_text(encoding="utf-8", errors="replace")
    configured = SETUP_PATTERN in conf_text
    snippet = (
        "recorder:\n"
        "  exclude:\n"
        "    entities: !include_dir_merge_list recorder_exclude_entities\n"
    )
    return {
        "configured": configured,
        "detail": "OK" if configured else "Falta enlace en configuration.yaml",
        "snippet": snippet,
        "managed_file": str(MANAGED_LIST_FILE),
    }


async def _recorder_info() -> dict[str, Any]:
    try:
        async with websockets.connect(CORE_WS_URL) as ws:
            hello = json.loads(await ws.recv())
            if hello.get("type") != "auth_required":
                raise RuntimeError("Unexpected WebSocket handshake")

            await ws.send(
                json.dumps({"type": "auth", "access_token": SUPERVISOR_TOKEN})
            )
            auth_response = json.loads(await ws.recv())
            if auth_response.get("type") != "auth_ok":
                raise RuntimeError("WebSocket auth failed")

            await ws.send(json.dumps({"id": 1, "type": "recorder/info"}))
            info_message = json.loads(await ws.recv())
            if not info_message.get("success", False):
                raise RuntimeError("recorder/info request failed")
            return info_message.get("result", {})
    except HTTPException:
        raise
    except Exception as err:
        raise HTTPException(status_code=502, detail=f"Status read failed: {err}") from err


async def _list_states() -> list[dict[str, Any]]:
    url = f"{CORE_HTTP_BASE}/api/states"
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        response = await client.get(url, headers=_auth_headers())
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail=response.text)
    states = response.json()
    if not isinstance(states, list):
        raise HTTPException(status_code=502, detail="Invalid /api/states response")
    return states


async def _get_state(entity_id: str) -> dict[str, Any] | None:
    url = f"{CORE_HTTP_BASE}/api/states/{entity_id}"
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        response = await client.get(url, headers=_auth_headers())
    if response.status_code == 404:
        return None
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail=response.text)
    data = response.json()
    if not isinstance(data, dict):
        return None
    return data


async def _restart_core() -> None:
    url = f"{CORE_HTTP_BASE}/api/services/homeassistant/restart"
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        response = await client.post(url, headers=_auth_headers(), json={})
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail=response.text)


async def _call_recorder_service(service: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    url = f"{CORE_HTTP_BASE}/api/services/recorder/{service}"
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        response = await client.post(url, headers=_auth_headers(), json=payload or {})
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail=response.text)
    try:
        result = response.json()
    except Exception:
        result = {"raw": response.text}
    return {"ok": True, "service": service, "response": result}


def _detect_sqlite_db_path() -> Path | None:
    db_url = _extract_recorder_db_url_from_config()
    if db_url and db_url.startswith("sqlite"):
        sqlite_part = db_url.split("://", 1)[1] if "://" in db_url else ""
        sqlite_path = "/" + sqlite_part.lstrip("/") if sqlite_part else ""
        if sqlite_path:
            parsed_path = Path(sqlite_path)
            if parsed_path.exists():
                return parsed_path
    default_db = HA_CONFIG_DIR / "home-assistant_v2.db"
    return default_db if default_db.exists() else None


def _read_connection_string_override() -> str | None:
    for env_key in ("RECORDER_DB_URL", "DB_CONNECT_STRING", "CONNECTION_STRING"):
        value = os.environ.get(env_key, "").strip()
        if value:
            return value
    options_file = Path("/data/options.json")
    if options_file.exists():
        try:
            options = json.loads(options_file.read_text(encoding="utf-8"))
            if isinstance(options, dict):
                for key in ("connectionString", "connection_string", "db_url"):
                    value = str(options.get(key, "")).strip()
                    if value:
                        return value
        except Exception:
            return None
    return None


def _find_key_recursive(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for value in obj.values():
            found = _find_key_recursive(value, key)
            if found is not None:
                return found
    if isinstance(obj, list):
        for value in obj:
            found = _find_key_recursive(value, key)
            if found is not None:
                return found
    return None


def _replace_secrets_in_text(text: str, secrets: dict[str, str]) -> str:
    result = text
    for secret_name, secret_value in secrets.items():
        result = result.replace(f"!secret {secret_name}", str(secret_value))
    return result


def _load_secrets(start_dir: Path, home_dir: Path, cache: dict[str, dict[str, str]]) -> dict[str, str]:
    key = str(start_dir.resolve())
    if key in cache:
        return cache[key]
    current = start_dir.resolve()
    home_resolved = home_dir.resolve()
    while True:
        secret_file = current / "secrets.yaml"
        if secret_file.exists():
            try:
                loaded = yaml.safe_load(secret_file.read_text(encoding="utf-8")) or {}
                if isinstance(loaded, dict):
                    secrets = {str(k): str(v) for k, v in loaded.items()}
                    cache[key] = secrets
                    return secrets
            except Exception:
                break
        if current == home_resolved or len(str(current)) <= len(str(home_resolved)):
            break
        current = current.parent
    cache[key] = {}
    return {}


def _fix_yaml_like_dbstats(text: str, secrets: dict[str, str]) -> tuple[str, list[str]]:
    no_comments = "\n".join(
        line for line in text.splitlines() if not line.strip().startswith("#")
    )
    no_unknown_tags = no_comments.replace("!env_var", "env_var").replace("!input", "input")
    include_tags = [
        "include_dir_merge_list",
        "include_dir_list",
        "include_dir_named",
        "include_dir_merge_named",
        "include",
    ]
    found_files: list[str] = []
    kept_lines: list[str] = []
    for line in no_unknown_tags.splitlines():
        processed = line
        for tag in include_tags:
            token = f"!{tag}"
            if token in processed:
                parts = processed.split(token, 1)
                include_target = parts[1].strip()
                if include_target:
                    found_files.append(include_target)
                processed = ""
                break
        if processed:
            kept_lines.append(processed)
    merged = "\n".join(kept_lines)
    merged = _replace_secrets_in_text(merged, secrets)
    # Keep parsing resilient when secret is missing.
    merged = merged.replace("!secret", "secret")
    return merged, found_files


def _load_yaml_like_dbstats(abs_path: Path, home_dir: Path, secret_cache: dict[str, dict[str, str]]) -> Any:
    if not abs_path.exists():
        return {}
    if abs_path.is_dir():
        values: list[Any] = []
        for file in sorted(abs_path.rglob("*.yaml")):
            values.append(_load_yaml_like_dbstats(file, home_dir, secret_cache))
        return values
    raw = abs_path.read_text(encoding="utf-8", errors="replace")
    secrets = _load_secrets(abs_path.parent, home_dir, secret_cache)
    fixed_text, include_refs = _fix_yaml_like_dbstats(raw, secrets)
    try:
        loaded = yaml.safe_load(fixed_text) if fixed_text.strip() else {}
    except Exception:
        loaded = {}
    additional: dict[str, Any] = {}
    for include_ref in include_refs:
        ref_path = (abs_path.parent / include_ref).resolve()
        additional[Path(include_ref).stem] = _load_yaml_like_dbstats(
            ref_path, home_dir, secret_cache
        )
    if isinstance(loaded, dict):
        if additional:
            loaded = {**loaded, "additional": additional}
        return loaded
    if additional:
        return {"value": loaded, "additional": additional}
    return loaded


def _extract_recorder_db_url_from_config() -> str | None:
    override = _read_connection_string_override()
    if override:
        return override
    if not CONFIG_YAML.exists():
        return None
    secret_cache: dict[str, dict[str, str]] = {}
    parsed = _load_yaml_like_dbstats(CONFIG_YAML, HA_CONFIG_DIR, secret_cache)
    recorder = _find_key_recursive(parsed, "recorder")
    if isinstance(recorder, dict):
        db_url = recorder.get("db_url")
        if isinstance(db_url, str) and db_url.strip():
            return db_url.strip()
    return None


def _query_activity_metrics_sqlite(hours: int = 24, limit: int = 1200) -> dict[str, Any]:
    db_path = _detect_sqlite_db_path()
    if db_path is None:
        return {
            "available": False,
            "reason": "No se encontro home-assistant_v2.db (puede ser DB externa).",
            "window_hours": hours,
            "items": [],
            "by_entity_id": {},
            "source": "sqlite",
        }

    since_ts = time.time() - max(1, hours) * 3600
    items: list[dict[str, Any]] = []
    query_used = ""

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # HA schema with states_meta + metadata_id
        try:
            cur.execute(
                """
                SELECT
                    sm.entity_id AS entity_id,
                    COUNT(*) AS state_changes,
                    MAX(s.last_updated_ts) AS last_updated_ts,
                    MIN(s.last_updated_ts) AS first_updated_ts
                FROM states s
                JOIN states_meta sm ON sm.metadata_id = s.metadata_id
                WHERE s.last_updated_ts >= ?
                GROUP BY sm.entity_id
                ORDER BY state_changes DESC
                LIMIT ?
                """,
                (since_ts, max(1, limit)),
            )
            rows = cur.fetchall()
            query_used = "states_meta"
            for row in rows:
                count = int(row["state_changes"] or 0)
                items.append(
                    {
                        "entity_id": str(row["entity_id"]),
                        "state_changes": count,
                        "changes_per_hour": round(count / max(1, hours), 2),
                        "last_updated_ts": float(row["last_updated_ts"] or 0),
                        "first_updated_ts": float(row["first_updated_ts"] or 0),
                    }
                )
        except sqlite3.DatabaseError:
            # Legacy schema fallback (states.entity_id)
            cur.execute(
                """
                SELECT
                    s.entity_id AS entity_id,
                    COUNT(*) AS state_changes,
                    MAX(s.last_updated_ts) AS last_updated_ts,
                    MIN(s.last_updated_ts) AS first_updated_ts
                FROM states s
                WHERE s.last_updated_ts >= ?
                GROUP BY s.entity_id
                ORDER BY state_changes DESC
                LIMIT ?
                """,
                (since_ts, max(1, limit)),
            )
            rows = cur.fetchall()
            query_used = "states_legacy"
            for row in rows:
                count = int(row["state_changes"] or 0)
                items.append(
                    {
                        "entity_id": str(row["entity_id"]),
                        "state_changes": count,
                        "changes_per_hour": round(count / max(1, hours), 2),
                        "last_updated_ts": float(row["last_updated_ts"] or 0),
                        "first_updated_ts": float(row["first_updated_ts"] or 0),
                    }
                )
        finally:
            conn.close()
    except Exception as err:
        return {
            "available": False,
            "reason": f"No se pudieron calcular metricas: {err}",
            "window_hours": hours,
            "items": [],
            "by_entity_id": {},
            "source": "sqlite",
        }

    by_entity_id = {item["entity_id"]: item for item in items}
    return {
        "available": True,
        "reason": "OK",
        "window_hours": hours,
        "db_path": str(db_path),
        "db_size_mb": round(db_path.stat().st_size / (1024 * 1024), 2),
        "query_used": query_used,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "items": items,
        "by_entity_id": by_entity_id,
        "source": "sqlite",
    }


def _query_activity_metrics_mariadb(hours: int = 24, limit: int = 1200) -> dict[str, Any]:
    db_url = _extract_recorder_db_url_from_config()
    if not db_url:
        return {
            "available": False,
            "reason": "No se detecto db_url.",
            "window_hours": hours,
            "items": [],
            "by_entity_id": {},
            "source": "mariadb",
        }
    parsed = urlparse(db_url)
    if parsed.scheme not in {"mysql", "mysql+pymysql", "mariadb", "mariadb+pymysql"}:
        return {
            "available": False,
            "reason": "db_url no es MariaDB/MySQL.",
            "window_hours": hours,
            "items": [],
            "by_entity_id": {},
            "source": "mariadb",
        }

    since_ts = time.time() - max(1, hours) * 3600
    host = parsed.hostname or "core-mariadb"
    port = parsed.port or 3306
    user = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    database = (parsed.path or "/").lstrip("/") or "homeassistant"
    query_params = parse_qs(parsed.query)
    unix_socket = query_params.get("unix_socket", [None])[0]
    items: list[dict[str, Any]] = []
    query_used = ""

    try:
        conn = pymysql.connect(
            host=host,
            port=port,
            user=user,
            password=password,
            database=database,
            unix_socket=unix_socket,
            connect_timeout=5,
            read_timeout=25,
            write_timeout=25,
            cursorclass=pymysql.cursors.DictCursor,
        )
        with conn.cursor() as cur:
            try:
                cur.execute(
                    """
                    SELECT
                        sm.entity_id AS entity_id,
                        COUNT(*) AS state_changes,
                        MAX(s.last_updated_ts) AS last_updated_ts,
                        MIN(s.last_updated_ts) AS first_updated_ts
                    FROM states s
                    JOIN states_meta sm ON sm.metadata_id = s.metadata_id
                    WHERE s.last_updated_ts >= %s
                    GROUP BY sm.entity_id
                    ORDER BY state_changes DESC
                    LIMIT %s
                    """,
                    (since_ts, max(1, limit)),
                )
                rows = cur.fetchall()
                query_used = "states_meta"
            except Exception:
                cur.execute(
                    """
                    SELECT
                        s.entity_id AS entity_id,
                        COUNT(*) AS state_changes,
                        MAX(s.last_updated_ts) AS last_updated_ts,
                        MIN(s.last_updated_ts) AS first_updated_ts
                    FROM states s
                    WHERE s.last_updated_ts >= %s
                    GROUP BY s.entity_id
                    ORDER BY state_changes DESC
                    LIMIT %s
                    """,
                    (since_ts, max(1, limit)),
                )
                rows = cur.fetchall()
                query_used = "states_legacy"
        conn.close()
        for row in rows:
            count = int(row.get("state_changes") or 0)
            items.append(
                {
                    "entity_id": str(row.get("entity_id") or "").strip().lower(),
                    "state_changes": count,
                    "changes_per_hour": round(count / max(1, hours), 2),
                    "last_updated_ts": float(row.get("last_updated_ts") or 0),
                    "first_updated_ts": float(row.get("first_updated_ts") or 0),
                }
            )
    except Exception as err:
        return {
            "available": False,
            "reason": f"Error consultando MariaDB/MySQL: {err}",
            "window_hours": hours,
            "items": [],
            "by_entity_id": {},
            "source": "mariadb",
        }

    return {
        "available": True,
        "reason": "OK",
        "window_hours": hours,
        "db_host": host,
        "db_name": database,
        "db_socket": unix_socket,
        "query_used": query_used,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "items": items,
        "by_entity_id": {item["entity_id"]: item for item in items},
        "source": "mariadb",
    }


async def _query_activity_metrics(hours: int = 24, limit: int = 1200) -> dict[str, Any]:
    sqlite_metrics = _query_activity_metrics_sqlite(hours=hours, limit=limit)
    if sqlite_metrics.get("available"):
        return sqlite_metrics
    mariadb_metrics = _query_activity_metrics_mariadb(hours=hours, limit=limit)
    if mariadb_metrics.get("available"):
        return mariadb_metrics
    return {
        "available": False,
        "reason": "No se pudo obtener metricas de SQLite ni MariaDB.",
        "window_hours": hours,
        "items": [],
        "by_entity_id": {},
        "source": "none",
    }


def _metric_sort_key(item: dict[str, Any], field: str) -> Any:
    if field in {"state_changes", "changes_per_hour", "last_updated_ts", "battery"}:
        return float(item.get(field, 0))
    return str(item.get(field, "")).lower()


async def _query_entity_detail_stats(entity_id: str) -> dict[str, Any]:
    windows = [1, 24, 168]
    summary: dict[str, Any] = {"windows": {}, "source": "none"}
    for hours in windows:
        metrics = await _query_activity_metrics(hours=hours, limit=10000)
        data = metrics.get("by_entity_id", {}).get(entity_id, {})
        summary["windows"][str(hours)] = {
            "state_changes": int(data.get("state_changes", 0)),
            "changes_per_hour": float(data.get("changes_per_hour", 0)),
            "last_updated_ts": float(data.get("last_updated_ts", 0)),
        }
        summary["source"] = metrics.get("source", "none")
    return summary


@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True}


@app.get("/api/setup")
def api_setup() -> dict[str, Any]:
    return _setup_status()


@app.get("/api/status")
async def api_status() -> dict[str, Any]:
    recorder = await _recorder_info()
    db_path = _detect_sqlite_db_path()
    db_url = _extract_recorder_db_url_from_config()
    mariadb_mode = bool(db_url and urlparse(db_url).scheme in {"mysql", "mysql+pymysql", "mariadb", "mariadb+pymysql"})
    metrics_mode = "sqlite" if db_path else ("mariadb" if mariadb_mode else "none")
    return {
        "recorder_recording": bool(recorder.get("recording", False)),
        "recorder": recorder,
        "setup": _setup_status(),
        "excluded_count": len(_load_excluded_entities()),
        "metrics_mode": metrics_mode,
        "metrics_reason": (
            "SQLite local detectada"
            if db_path
            else "MariaDB/MySQL detectada por db_url"
            if mariadb_mode
            else "No se detecto fuente de metricas"
        ),
        "metrics_db_path": str(db_path) if db_path else None,
        "recorder_backlog": recorder.get("backlog"),
        "recorder_thread_running": recorder.get("thread_running"),
        "recorder_migration_in_progress": recorder.get("migration_in_progress"),
    }


@app.get("/api/entities")
async def api_entities(
    search: str = "",
    limit: int = 500,
    hours: int = 24,
    domain: str = "all",
    excluded: str = "all",
    sort_by: str = "state_changes",
    sort_dir: str = "desc",
) -> dict[str, Any]:
    states = await _list_states()
    excluded_entities = _load_excluded_entities()
    metrics = await _query_activity_metrics(hours=hours, limit=4000)
    metrics_map = metrics["by_entity_id"]
    needle = search.strip().lower()
    domain_filter = domain.strip().lower()
    excluded_filter = excluded.strip().lower()
    entities: list[dict[str, Any]] = []

    for item in states:
        entity_id = str(item.get("entity_id", ""))
        if not entity_id:
            continue
        normalized_entity_id = entity_id.strip().lower()
        attributes = item.get("attributes", {})
        if not isinstance(attributes, dict):
            attributes = {}
        friendly_name = str(attributes.get("friendly_name", ""))
        domain_name = entity_id.split(".", 1)[0] if "." in entity_id else "unknown"
        m = metrics_map.get(normalized_entity_id, {})
        area_name = (
            str(attributes.get("area_name") or attributes.get("area") or attributes.get("suggested_area") or "").strip()
        )
        integration_name = str(attributes.get("integration") or domain_name).strip()
        manufacturer = str(attributes.get("manufacturer") or attributes.get("device_manufacturer") or "").strip()
        model = str(attributes.get("model") or attributes.get("device_model") or "").strip()
        battery_raw = attributes.get("battery_level", attributes.get("battery"))
        try:
            battery = float(battery_raw) if battery_raw is not None else None
        except (TypeError, ValueError):
            battery = None
        state_value = str(item.get("state", ""))
        tags_attr = attributes.get("labels") or attributes.get("tags") or []
        if isinstance(tags_attr, list):
            tags = [str(tag).strip() for tag in tags_attr if str(tag).strip()]
        else:
            tags = []
        icon = str(attributes.get("icon", "")).strip()

        if needle and needle not in normalized_entity_id and needle not in friendly_name.lower():
            continue
        if domain_filter != "all" and domain_filter != domain_name:
            continue
        is_excluded = normalized_entity_id in excluded_entities
        if excluded_filter == "excluded" and not is_excluded:
            continue
        if excluded_filter == "included" and is_excluded:
            continue

        entities.append(
            {
                "entity_id": normalized_entity_id,
                "friendly_name": friendly_name,
                "domain": domain_name,
                "excluded_by_app": is_excluded,
                "state_changes": int(m.get("state_changes", 0)),
                "changes_per_hour": float(m.get("changes_per_hour", 0)),
                "last_updated_ts": float(m.get("last_updated_ts", 0)),
                "area": area_name,
                "integration": integration_name,
                "manufacturer": manufacturer,
                "model": model,
                "battery": battery,
                "state": state_value,
                "last_changed": str(item.get("last_changed", "")),
                "last_updated": str(item.get("last_updated", "")),
                "icon": icon,
                "tags": tags,
            }
        )
    sort_key = sort_by if sort_by in {
        "entity_id",
        "friendly_name",
        "domain",
        "state_changes",
        "changes_per_hour",
        "last_updated_ts",
        "area",
        "integration",
        "manufacturer",
        "model",
        "battery",
        "state",
    } else "state_changes"
    reverse = sort_dir.strip().lower() != "asc"
    entities.sort(key=lambda x: _metric_sort_key(x, sort_key), reverse=reverse)
    if limit > 0:
        entities = entities[:limit]

    all_domains = sorted(
        {
            str(item.get("entity_id", "")).split(".", 1)[0]
            for item in states
            if "." in str(item.get("entity_id", ""))
        }
    )

    return {
        "items": entities,
        "total": len(entities),
        "managed_file": str(MANAGED_LIST_FILE),
        "domains": all_domains,
        "metrics": {
            "available": metrics["available"],
            "reason": metrics["reason"],
            "window_hours": metrics["window_hours"],
            "source": metrics.get("source"),
            "db_path": metrics.get("db_path"),
            "db_size_mb": metrics.get("db_size_mb"),
            "query_used": metrics.get("query_used"),
        },
    }


@app.get("/api/metrics")
async def api_metrics(hours: int = 24, limit: int = 1000) -> dict[str, Any]:
    metrics = await _query_activity_metrics(hours=hours, limit=limit)
    metrics.pop("by_entity_id", None)
    return metrics


@app.get("/api/entity/{entity_id:path}/details")
async def api_entity_details(entity_id: str) -> dict[str, Any]:
    normalized = entity_id.strip().lower()
    if "." not in normalized:
        raise HTTPException(status_code=400, detail="Invalid entity_id")
    state = await _get_state(normalized)
    stats = await _query_entity_detail_stats(normalized)
    attrs = (state or {}).get("attributes", {}) if state else {}
    return {
        "entity_id": normalized,
        "exists": state is not None,
        "friendly_name": str(attrs.get("friendly_name", "")),
        "state": (state or {}).get("state"),
        "unit_of_measurement": attrs.get("unit_of_measurement"),
        "device_class": attrs.get("device_class"),
        "state_class": attrs.get("state_class"),
        "last_changed": (state or {}).get("last_changed"),
        "last_updated": (state or {}).get("last_updated"),
        "statistics": stats,
        "ha_entity_url": f"/config/entities/entity/{normalized}",
    }


@app.post("/api/recorder/enable")
async def api_recorder_enable() -> dict[str, Any]:
    return await _call_recorder_service("enable")


@app.post("/api/recorder/disable")
async def api_recorder_disable() -> dict[str, Any]:
    return await _call_recorder_service("disable")


@app.post("/api/recorder/purge")
async def api_recorder_purge(payload: RecorderPurgePayload) -> dict[str, Any]:
    data: dict[str, Any] = {
        "repack": payload.repack,
        "apply_filter": payload.apply_filter,
    }
    if payload.keep_days is not None:
        data["keep_days"] = payload.keep_days
    return await _call_recorder_service("purge", data)


@app.post("/api/recorder/purge_entities")
async def api_recorder_purge_entities(payload: RecorderPurgeEntitiesPayload) -> dict[str, Any]:
    data: dict[str, Any] = {
        "entity_id": [item.strip().lower() for item in payload.entity_ids if item.strip()],
        "domains": [item.strip().lower() for item in payload.domains if item.strip()],
        "entity_globs": [item.strip().lower() for item in payload.entity_globs if item.strip()],
        "keep_days": payload.keep_days,
    }
    if not data["entity_id"] and not data["domains"] and not data["entity_globs"]:
        raise HTTPException(
            status_code=400,
            detail="Debes indicar al menos entity_id, domains o entity_globs",
        )
    return await _call_recorder_service("purge_entities", data)


@app.post("/api/entities/{entity_id:path}")
def api_toggle_entity(entity_id: str, payload: TogglePayload) -> dict[str, Any]:
    normalized = entity_id.strip().lower()
    if "." not in normalized:
        raise HTTPException(status_code=400, detail="Invalid entity_id")

    excluded = _load_excluded_entities()
    if payload.exclude:
        excluded.add(normalized)
    else:
        excluded.discard(normalized)
    _save_excluded_entities(excluded)

    return {
        "ok": True,
        "entity_id": normalized,
        "excluded_by_app": payload.exclude,
        "excluded_count": len(excluded),
    }


@app.post("/api/apply")
async def api_apply() -> dict[str, Any]:
    await _restart_core()
    return {
        "ok": True,
        "message": "Reinicio de Home Assistant solicitado para aplicar cambios de recorder.",
    }


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse(
        """
<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Recorder Control</title>
  <style>
    :root {
      --bg: #111317;
      --panel: #171a1f;
      --panel-2: #1a1d22;
      --line: #2a2f38;
      --line-soft: #20252d;
      --text: #e6e8eb;
      --muted: #a8b0bb;
      --accent: #00b8ff;
      --accent-bg: #073547;
      --danger: #d35050;
      --ok: #31b16b;
      --radius: 12px;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: Roboto, "Segoe UI", sans-serif;
      min-height: 100vh;
    }
    .topbar {
      height: 56px;
      background: #14171c;
      border-bottom: 1px solid var(--line);
      display: grid;
      grid-template-columns: 64px 1fr 64px;
      align-items: center;
    }
    .topbar .back {
      justify-self: center;
      color: #cfd5dd;
      font-size: 24px;
      line-height: 1;
      user-select: none;
    }
    .tabs {
      display: flex;
      justify-content: center;
      gap: 36px;
      align-items: center;
    }
    .tab {
      height: 56px;
      display: flex;
      align-items: center;
      color: #cdd4dc;
      font-size: 14px;
      border-bottom: 2px solid transparent;
      cursor: default;
    }
    .tab.active {
      color: #9ee7ff;
      border-bottom-color: var(--accent);
    }
    .shell {
      display: grid;
      grid-template-columns: 170px 1fr;
      min-height: calc(100vh - 56px);
    }
    .sidebar {
      border-right: 1px solid var(--line);
      background: #0f1216;
    }
    .sidebar-content {
      padding-top: 8px;
    }
    .filters-btn {
      margin: 8px;
      width: calc(100% - 16px);
      height: 40px;
      border: 1px solid #1b4b5d;
      border-radius: 14px;
      background: #063345;
      color: #ddf6ff;
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      font-weight: 500;
    }
    .section {
      border-top: 1px solid var(--line-soft);
    }
    .section-head {
      width: 100%;
      height: 58px;
      background: transparent;
      color: #e5e9ef;
      border: 0;
      border-bottom: 1px solid var(--line-soft);
      text-align: left;
      padding: 0 18px;
      display: flex;
      align-items: center;
      gap: 10px;
      font-size: 36px;
      font-weight: 500;
      cursor: pointer;
    }
    .section-head small {
      font-size: 14px;
      margin-left: auto;
      color: var(--muted);
      font-weight: 400;
    }
    .section-items {
      display: none;
      padding: 8px 10px 10px 22px;
    }
    .section.open .section-items { display: block; }
    .section-item {
      display: block;
      font-size: 13px;
      color: #d6dbe2;
      margin: 7px 0;
    }
    .main {
      display: grid;
      grid-template-rows: auto 1fr;
      min-width: 0;
    }
    .toolbar {
      border-bottom: 1px solid var(--line);
      background: #13171d;
      padding: 8px 10px;
      display: flex;
      align-items: center;
      gap: 8px;
      position: relative;
    }
    .chip-icon {
      width: 34px;
      height: 34px;
      border-radius: 9px;
      border: 1px solid var(--line);
      background: #1a1f27;
      color: #d0d7df;
      display: grid;
      place-items: center;
    }
    .search {
      flex: 1;
      height: 34px;
      border: 1px solid var(--line);
      border-radius: 9px;
      background: #191d24;
      color: var(--text);
      padding: 0 13px;
      font-size: 14px;
    }
    .menu-btn {
      height: 34px;
      border-radius: 11px;
      border: 1px solid var(--line);
      background: #22262d;
      color: #dde4ec;
      padding: 0 14px;
      font-size: 14px;
      display: inline-flex;
      align-items: center;
      gap: 8px;
    }
    .menu {
      position: absolute;
      top: 48px;
      z-index: 20;
      width: 188px;
      border: 1px solid #4b515d;
      border-radius: 12px;
      background: #1b1f25;
      box-shadow: 0 8px 24px rgba(0,0,0,.5);
      padding: 6px 0;
      display: none;
    }
    .menu.show { display: block; }
    .menu-item {
      width: calc(100% - 12px);
      margin: 2px 6px;
      height: 34px;
      border-radius: 6px;
      border: 1px solid transparent;
      background: transparent;
      color: #dbe1e8;
      text-align: left;
      padding: 0 10px;
      font-size: 15px;
      cursor: pointer;
    }
    .menu-item.active {
      border-color: #009edb;
      background: #022f43;
      color: #4ad4ff;
    }
    .table-wrap {
      min-width: 0;
      overflow: auto;
      height: calc(100vh - 104px);
    }
    table {
      width: 100%;
      border-collapse: collapse;
      min-width: 1100px;
      font-size: 13px;
    }
    thead th {
      position: sticky;
      top: 0;
      background: #161a20;
      color: #d6dde5;
      border-bottom: 1px solid var(--line);
      text-align: left;
      font-weight: 600;
      height: 40px;
      padding: 0 10px;
      white-space: nowrap;
    }
    tbody td {
      border-bottom: 1px solid #1f242d;
      height: 44px;
      padding: 0 10px;
      color: #d8dde4;
      white-space: nowrap;
    }
    tbody tr:hover { background: #181d24; }
    .col-check { width: 34px; }
    .col-icon { width: 32px; text-align: center; color: #7fd5ff; }
    .col-device { min-width: 260px; }
    .device-link { color: #e8edf3; cursor: pointer; }
    .device-link:hover { color: #87dfff; }
    .battery { text-align: right; min-width: 70px; }
    .group-row td {
      height: 30px;
      background: #10151c;
      color: #8bc2d8;
      font-weight: 600;
      border-top: 1px solid #2a3340;
      border-bottom: 1px solid #2a3340;
    }
    .fab {
      position: fixed;
      right: 16px;
      bottom: 14px;
      height: 42px;
      border-radius: 22px;
      border: 0;
      background: #0aa9e9;
      color: #dff8ff;
      padding: 0 18px;
      font-weight: 600;
      font-size: 22px;
      display: inline-flex;
      align-items: center;
      gap: 8px;
      box-shadow: 0 8px 20px rgba(0,0,0,.35);
    }
    .fab span { font-size: 22px; line-height: 1; }
    .fab small { font-size: 14px; }
    .modal-bg {
      position: fixed;
      inset: 0;
      background: rgba(0,0,0,.55);
      display: none;
      align-items: center;
      justify-content: center;
      z-index: 40;
    }
    .modal-bg.show { display: flex; }
    .modal {
      width: 440px;
      max-width: calc(100vw - 22px);
      background: #1a1d22;
      border-radius: 24px;
      border: 1px solid #2c333d;
      padding: 18px 20px;
    }
    .modal h3 {
      margin: 0;
      font-size: 38px;
      font-weight: 500;
    }
    .modal-top {
      display: flex;
      gap: 12px;
      align-items: center;
      margin-bottom: 10px;
    }
    .col-item {
      height: 46px;
      display: grid;
      grid-template-columns: 28px 1fr 28px;
      align-items: center;
      color: #d8dde4;
      border-bottom: 1px solid #242a33;
      font-size: 32px;
    }
    .col-item .toggle {
      justify-self: end;
      cursor: pointer;
      opacity: .86;
    }
    .modal-actions {
      margin-top: 14px;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
    .link-btn {
      border: 0;
      background: transparent;
      color: #0cc2ff;
      padding: 0;
      font-weight: 600;
      font-size: 34px;
    }
    .done-btn {
      height: 42px;
      border-radius: 22px;
      border: 0;
      background: #0aa9e9;
      color: #ebfbff;
      padding: 0 18px;
      font-weight: 700;
      font-size: 34px;
    }
    .drawer {
      position: fixed;
      top: 56px;
      right: 0;
      width: 380px;
      max-width: 95vw;
      bottom: 0;
      background: #191d23;
      border-left: 1px solid #2d353f;
      padding: 14px;
      transform: translateX(100%);
      transition: transform .22s ease;
      z-index: 30;
      overflow: auto;
    }
    .drawer.show { transform: translateX(0); }
    .drawer h4 { margin: 0 0 8px; font-size: 34px; }
    .drawer .label { color: var(--muted); font-size: 32px; }
    .drawer .val { font-size: 33px; margin-bottom: 9px; }
    .drawer .actions { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 10px; }
    .drawer button {
      height: 34px;
      border-radius: 8px;
      border: 1px solid var(--line);
      background: #242a33;
      color: #dbe4ed;
      padding: 0 10px;
      font-size: 32px;
    }
    .quick {
      position: absolute;
      right: 42px;
      top: 48px;
      background: #1b1f25;
      border: 1px solid #444d59;
      border-radius: 12px;
      padding: 6px;
      display: none;
      z-index: 21;
      width: 230px;
    }
    .quick.show { display: block; }
    .quick button {
      width: 100%;
      border: 0;
      border-radius: 8px;
      background: transparent;
      color: #dbe3ec;
      text-align: left;
      padding: 9px 10px;
      font-size: 34px;
    }
    .quick button:hover { background: #283140; }
    @media (max-width: 900px) {
      .shell { grid-template-columns: 1fr; }
      .sidebar {
        position: fixed;
        z-index: 50;
        top: 56px;
        bottom: 0;
        width: 170px;
        transform: translateX(-100%);
        transition: transform .2s ease;
      }
      .sidebar.open { transform: translateX(0); }
    }
  </style>
</head>
<body>
  <header class="topbar">
    <div class="back">←</div>
    <nav class="tabs">
      <div class="tab">Integraciones</div>
      <div class="tab active">Dispositivos</div>
      <div class="tab">Entidades</div>
      <div class="tab">Ayudantes</div>
    </nav>
    <div style="justify-self:center;color:#cfd5dd;">⋮</div>
  </header>
  <div class="shell">
    <aside id="sidebar" class="sidebar">
      <div class="sidebar-content">
        <button id="mobileFiltersBtn" class="filters-btn">☰ Filtros</button>
        <div class="section" data-section="areas">
          <button class="section-head" onclick="toggleSection('areas')">⌄ Áreas <small id="countAreas"></small></button>
          <div class="section-items" id="itemsAreas"></div>
        </div>
        <div class="section" data-section="integrations">
          <button class="section-head" onclick="toggleSection('integrations')">⌄ Integraciones <small id="countIntegrations"></small></button>
          <div class="section-items" id="itemsIntegrations"></div>
        </div>
        <div class="section" data-section="states">
          <button class="section-head" onclick="toggleSection('states')">⌄ Estado <small id="countStates"></small></button>
          <div class="section-items" id="itemsStates"></div>
        </div>
        <div class="section" data-section="tags">
          <button class="section-head" onclick="toggleSection('tags')">⌄ Etiquetas <small id="countTags"></small></button>
          <div class="section-items" id="itemsTags"></div>
        </div>
      </div>
    </aside>
    <main class="main">
      <div class="toolbar">
        <button class="chip-icon" onclick="toggleSidebar()">☰</button>
        <input id="search" class="search" placeholder="Buscar dispositivos">
        <button id="groupBtn" class="menu-btn" onclick="toggleMenu('groupMenu')">Agrupar por <span>▾</span></button>
        <button id="sortBtn" class="menu-btn" onclick="toggleMenu('sortMenu')">Ordenar por Área <span>▾</span></button>
        <button class="chip-icon" onclick="openCustomize()">▦</button>
        <button class="chip-icon" onclick="toggleQuickMenu()">⋮</button>
        <div id="groupMenu" class="menu"></div>
        <div id="sortMenu" class="menu"></div>
        <div id="quickMenu" class="quick">
          <button onclick="setRecorder(true)">Activar recorder</button>
          <button onclick="setRecorder(false)">Desactivar recorder</button>
          <button onclick="purgeGlobal()">Purge global</button>
          <button onclick="purgeSelected()">Purge entidades seleccionadas</button>
          <button onclick="applyChanges()">Aplicar filtros (reiniciar HA)</button>
        </div>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr id="tableHead"></tr>
          </thead>
          <tbody id="tbody">
            <tr><td colspan="12">Cargando...</td></tr>
          </tbody>
        </table>
      </div>
    </main>
  </div>
  <button class="fab"><span>＋</span><small>Añadir dispositivo</small></button>

  <div id="customizeModalBg" class="modal-bg">
    <div class="modal">
      <div class="modal-top"><span style="font-size:38px;cursor:pointer" onclick="closeCustomize()">✕</span><h3>Personalizar</h3></div>
      <div id="columnsList"></div>
      <div class="modal-actions">
        <button class="link-btn" onclick="restoreColumns()">Restaurar los valores predeterminados</button>
        <button class="done-btn" onclick="closeCustomize()">Hecho</button>
      </div>
    </div>
  </div>

  <aside id="entityDrawer" class="drawer">
    <div style="display:flex;justify-content:space-between;align-items:center">
      <h4>Entidad</h4>
      <button onclick="closeDrawer()" style="background:transparent;border:0;color:#dbe4ed;font-size:34px;cursor:pointer">✕</button>
    </div>
    <div class="label">ID</div><div id="dEntityId" class="val mono"></div>
    <div class="label">Nombre</div><div id="dName" class="val"></div>
    <div class="label">Estado actual</div><div id="dState" class="val"></div>
    <div class="label">1h</div><div id="d1h" class="val"></div>
    <div class="label">24h</div><div id="d24h" class="val"></div>
    <div class="label">7d</div><div id="d7d" class="val"></div>
    <div class="label">Último cambio</div><div id="dLastChanged" class="val"></div>
    <div class="label">Última actualización</div><div id="dLastUpdated" class="val"></div>
    <div class="actions">
      <button id="dOpenHA">Abrir en Home Assistant</button>
      <button id="dToggleExclusion">Toggle recorder</button>
    </div>
  </aside>

  <script>
    const $ = (id) => document.getElementById(id);
    const esc = (v) => String(v ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
    const ts = (v) => !v ? "—" : new Date(Number(v) * 1000).toLocaleString();
    const splitCsv = (t) => String(t || "").split(",").map((s) => s.trim()).filter(Boolean);

    const columnsDefault = [
      { key: "icon", label: "Icono" },
      { key: "device", label: "Dispositivo" },
      { key: "area", label: "Área" },
      { key: "integration", label: "Integración" },
      { key: "manufacturer", label: "Fabricante" },
      { key: "model", label: "Modelo" },
      { key: "battery", label: "Batería" }
    ];
    const state = {
      all: [],
      rows: [],
      selected: new Set(),
      filters: { areas: new Set(), integrations: new Set(), states: new Set(), tags: new Set() },
      catalog: { areas: [], integrations: [], states: [], tags: [] },
      sectionsOpen: { areas: false, integrations: false, states: false, tags: false },
      groupBy: "none",
      sortBy: "area",
      sortDir: "asc",
      search: "",
      hours: 24,
      visibleColumns: columnsDefault.map((c) => c.key),
      currentDetail: null
    };

    const groupOptions = [
      ["none", "No agrupar"],
      ["area", "Área"],
      ["integration", "Integración"],
      ["manufacturer", "Fabricante"],
      ["state", "Estado"]
    ];
    const sortOptions = [
      ["device", "Dispositivo"],
      ["area", "Área"],
      ["integration", "Integración"],
      ["manufacturer", "Fabricante"],
      ["model", "Modelo"],
      ["battery", "Batería"],
      ["state", "Estado"],
      ["state_changes", "Cambios"],
      ["changes_per_hour", "Cambios/h"],
      ["last_updated_ts", "Modificado"]
    ];

    async function api(path, opts = {}) {
      const res = await fetch(path, opts);
      if (!res.ok) throw new Error(await res.text());
      return res.json();
    }

    function toggleSidebar() {
      $("sidebar").classList.toggle("open");
    }

    function toggleSection(name) {
      state.sectionsOpen[name] = !state.sectionsOpen[name];
      renderSidebar();
    }

    function toggleMenu(id) {
      ["groupMenu", "sortMenu"].forEach((m) => {
        if (m !== id) $(m).classList.remove("show");
      });
      $(id).classList.toggle("show");
      $("quickMenu").classList.remove("show");
    }

    function toggleQuickMenu() {
      $("quickMenu").classList.toggle("show");
      $("groupMenu").classList.remove("show");
      $("sortMenu").classList.remove("show");
    }

    function closeMenus() {
      $("groupMenu").classList.remove("show");
      $("sortMenu").classList.remove("show");
      $("quickMenu").classList.remove("show");
    }

    function buildMenus() {
      $("groupMenu").innerHTML = groupOptions.map(([key, label]) => `
        <button class="menu-item ${state.groupBy === key ? "active" : ""}" onclick="setGroupBy('${key}')">${label}</button>
      `).join("");
      $("sortMenu").innerHTML = sortOptions.map(([key, label]) => `
        <button class="menu-item ${state.sortBy === key ? "active" : ""}" onclick="setSortBy('${key}')">${label}</button>
      `).join("");
      const sortLabel = sortOptions.find(([k]) => k === state.sortBy)?.[1] || "Área";
      $("sortBtn").innerHTML = `Ordenar por ${sortLabel}<span>▾</span>`;
      const groupLabel = groupOptions.find(([k]) => k === state.groupBy)?.[1] || "No agrupar";
      $("groupBtn").innerHTML = `Agrupar por ${groupLabel}<span>▾</span>`;
    }

    function setGroupBy(key) {
      state.groupBy = key;
      buildMenus();
      closeMenus();
      renderTable();
    }

    function setSortBy(key) {
      if (state.sortBy === key) {
        state.sortDir = state.sortDir === "asc" ? "desc" : "asc";
      } else {
        state.sortBy = key;
        state.sortDir = "asc";
      }
      buildMenus();
      closeMenus();
      applyFilters();
    }

    function toNumber(v) {
      const n = Number(v);
      return Number.isFinite(n) ? n : 0;
    }

    function getField(item, key) {
      if (key === "device") return (item.friendly_name || item.entity_id || "").toLowerCase();
      if (key === "battery") return toNumber(item.battery ?? -1);
      if (key === "state_changes") return toNumber(item.state_changes);
      if (key === "changes_per_hour") return toNumber(item.changes_per_hour);
      if (key === "last_updated_ts") return toNumber(item.last_updated_ts);
      return String(item[key] ?? "").toLowerCase();
    }

    function applyFilters() {
      const q = state.search.toLowerCase();
      let items = state.all.filter((it) => {
        if (q) {
          const haystack = [
            it.entity_id,
            it.friendly_name,
            it.area,
            it.integration,
            it.manufacturer,
            it.model,
            it.state
          ].join(" ").toLowerCase();
          if (!haystack.includes(q)) return false;
        }
        if (state.filters.areas.size && !state.filters.areas.has(String(it.area || ""))) return false;
        if (state.filters.integrations.size && !state.filters.integrations.has(String(it.integration || ""))) return false;
        if (state.filters.states.size && !state.filters.states.has(String(it.state || ""))) return false;
        if (state.filters.tags.size) {
          const tags = Array.isArray(it.tags) ? it.tags : [];
          if (!tags.some((t) => state.filters.tags.has(String(t)))) return false;
        }
        return true;
      });
      items.sort((a, b) => {
        const va = getField(a, state.sortBy);
        const vb = getField(b, state.sortBy);
        if (va < vb) return state.sortDir === "asc" ? -1 : 1;
        if (va > vb) return state.sortDir === "asc" ? 1 : -1;
        return 0;
      });
      state.rows = items;
      renderTable();
    }

    function groupValue(item) {
      if (state.groupBy === "none") return "";
      if (state.groupBy === "area") return item.area || "Sin área";
      if (state.groupBy === "integration") return item.integration || "Sin integración";
      if (state.groupBy === "manufacturer") return item.manufacturer || "Sin fabricante";
      if (state.groupBy === "state") return item.state || "Sin estado";
      return "";
    }

    function renderHead() {
      const cols = columnsDefault.filter((c) => state.visibleColumns.includes(c.key));
      $("tableHead").innerHTML = `
        <th class="col-check"><input id="masterCheck" type="checkbox" onchange="toggleAllRows(this.checked)"></th>
        ${cols.map((c) => `<th>${c.label}</th>`).join("")}
      `;
    }

    function iconCell(item) {
      if (item.icon) return `<span title="${esc(item.icon)}">◆</span>`;
      return "✳";
    }

    function batteryCell(item) {
      if (item.battery === null || item.battery === undefined || Number.isNaN(Number(item.battery))) return "—";
      return `${Math.round(Number(item.battery))}% 🔋`;
    }

    function renderRow(item) {
      const cols = state.visibleColumns;
      const deviceLabel = item.friendly_name || item.entity_id || "sin nombre";
      const cells = [];
      if (cols.includes("icon")) cells.push(`<td class="col-icon">${iconCell(item)}</td>`);
      if (cols.includes("device")) cells.push(`<td class="col-device"><span class="device-link" onclick="openDetail('${esc(item.entity_id)}')">${esc(deviceLabel)}</span></td>`);
      if (cols.includes("area")) cells.push(`<td>${esc(item.area || "—")}</td>`);
      if (cols.includes("integration")) cells.push(`<td>${esc(item.integration || "—")}</td>`);
      if (cols.includes("manufacturer")) cells.push(`<td>${esc(item.manufacturer || "—")}</td>`);
      if (cols.includes("model")) cells.push(`<td>${esc(item.model || "—")}</td>`);
      if (cols.includes("battery")) cells.push(`<td class="battery">${batteryCell(item)}</td>`);
      return `
        <tr>
          <td class="col-check"><input type="checkbox" ${state.selected.has(item.entity_id) ? "checked" : ""} onchange="toggleRow('${esc(item.entity_id)}', this.checked)"></td>
          ${cells.join("")}
        </tr>
      `;
    }

    function renderTable() {
      renderHead();
      let html = "";
      let prevGroup = "__none__";
      for (const item of state.rows) {
        const grp = groupValue(item);
        if (state.groupBy !== "none" && grp !== prevGroup) {
          const colCount = 1 + state.visibleColumns.length;
          html += `<tr class="group-row"><td colspan="${colCount}">${esc(grp)}</td></tr>`;
          prevGroup = grp;
        }
        html += renderRow(item);
      }
      if (!html) {
        html = `<tr><td colspan="${1 + state.visibleColumns.length}">Sin resultados</td></tr>`;
      }
      $("tbody").innerHTML = html;
      const allVisibleIds = state.rows.map((r) => r.entity_id);
      const allSelected = allVisibleIds.length > 0 && allVisibleIds.every((id) => state.selected.has(id));
      const master = $("masterCheck");
      if (master) master.checked = allSelected;
    }

    function updateCatalog() {
      const uniq = (arr) => [...new Set(arr.map((v) => String(v || "").trim()).filter(Boolean))].sort((a,b) => a.localeCompare(b));
      state.catalog.areas = uniq(state.all.map((i) => i.area));
      state.catalog.integrations = uniq(state.all.map((i) => i.integration));
      state.catalog.states = uniq(state.all.map((i) => i.state));
      state.catalog.tags = uniq(state.all.flatMap((i) => Array.isArray(i.tags) ? i.tags : []));
    }

    function renderSectionItems(name, list) {
      const container = $(
        name === "areas" ? "itemsAreas" :
        name === "integrations" ? "itemsIntegrations" :
        name === "states" ? "itemsStates" : "itemsTags"
      );
      const selectedSet = state.filters[name];
      container.innerHTML = list.map((value) => `
        <label class="section-item">
          <input type="checkbox" ${selectedSet.has(value) ? "checked" : ""} onchange="toggleFilterValue('${name}', '${esc(value)}', this.checked)">
          ${esc(value)}
        </label>
      `).join("") || `<div class="section-item" style="color:#7f8a97">Sin valores</div>`;
      const countId = name === "areas" ? "countAreas" : name === "integrations" ? "countIntegrations" : name === "states" ? "countStates" : "countTags";
      $(countId).textContent = selectedSet.size ? `${selectedSet.size} sel.` : "";
    }

    function renderSidebar() {
      ["areas", "integrations", "states", "tags"].forEach((name) => {
        const section = document.querySelector(`.section[data-section="${name}"]`);
        if (!section) return;
        section.classList.toggle("open", state.sectionsOpen[name]);
      });
      renderSectionItems("areas", state.catalog.areas);
      renderSectionItems("integrations", state.catalog.integrations);
      renderSectionItems("states", state.catalog.states);
      renderSectionItems("tags", state.catalog.tags);
    }

    function toggleFilterValue(section, value, checked) {
      if (checked) state.filters[section].add(value);
      else state.filters[section].delete(value);
      renderSidebar();
      applyFilters();
    }

    function toggleRow(id, checked) {
      if (checked) state.selected.add(id);
      else state.selected.delete(id);
      renderTable();
    }

    function toggleAllRows(checked) {
      if (checked) state.rows.forEach((r) => state.selected.add(r.entity_id));
      else state.rows.forEach((r) => state.selected.delete(r.entity_id));
      renderTable();
    }

    async function openDetail(entityId) {
      const data = await api("./api/entity/" + encodeURIComponent(entityId) + "/details");
      state.currentDetail = data;
      $("dEntityId").textContent = data.entity_id;
      $("dName").textContent = data.friendly_name || "—";
      $("dState").textContent = `${data.state ?? "—"} ${data.unit_of_measurement || ""}`;
      const w = data.statistics?.windows || {};
      $("d1h").textContent = `${w["1"]?.state_changes || 0} cambios (${(w["1"]?.changes_per_hour || 0).toFixed(2)}/h)`;
      $("d24h").textContent = `${w["24"]?.state_changes || 0} cambios (${(w["24"]?.changes_per_hour || 0).toFixed(2)}/h)`;
      $("d7d").textContent = `${w["168"]?.state_changes || 0} cambios (${(w["168"]?.changes_per_hour || 0).toFixed(2)}/h)`;
      $("dLastChanged").textContent = data.last_changed || "—";
      $("dLastUpdated").textContent = data.last_updated || "—";
      const row = state.all.find((r) => r.entity_id === data.entity_id);
      const excluded = Boolean(row?.excluded_by_app);
      $("dToggleExclusion").textContent = excluded ? "Incluir en recorder" : "Excluir del recorder";
      $("dToggleExclusion").onclick = async () => {
        await toggleEntity(data.entity_id, !excluded);
      };
      $("dOpenHA").onclick = () => window.open(data.ha_entity_url, "_blank");
      $("entityDrawer").classList.add("show");
    }

    function closeDrawer() {
      $("entityDrawer").classList.remove("show");
    }

    async function toggleEntity(entityId, exclude) {
      await api("./api/entities/" + encodeURIComponent(entityId), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ exclude })
      });
      await loadData();
    }

    async function setRecorder(enable) {
      await api(enable ? "./api/recorder/enable" : "./api/recorder/disable", { method: "POST" });
      closeMenus();
    }

    async function purgeGlobal() {
      closeMenus();
      if (!confirm("Lanzar recorder.purge global?")) return;
      await api("./api/recorder/purge", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ keep_days: 10, repack: false, apply_filter: false })
      });
      alert("Purge global lanzado.");
    }

    async function purgeSelected() {
      closeMenus();
      const ids = [...state.selected];
      if (!ids.length) {
        alert("Selecciona una o más entidades.");
        return;
      }
      if (!confirm(`Lanzar purge para ${ids.length} entidades?`)) return;
      await api("./api/recorder/purge_entities", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ entity_ids: ids, domains: [], entity_globs: [], keep_days: 0 })
      });
      alert("Purge por entidades lanzado.");
    }

    async function applyChanges() {
      closeMenus();
      if (!confirm("Reiniciar Home Assistant Core para aplicar filtros?")) return;
      await api("./api/apply", { method: "POST" });
      alert("Reinicio solicitado.");
    }

    function openCustomize() {
      renderColumnsList();
      $("customizeModalBg").classList.add("show");
    }

    function closeCustomize() {
      $("customizeModalBg").classList.remove("show");
    }

    function restoreColumns() {
      state.visibleColumns = columnsDefault.map((c) => c.key);
      renderColumnsList();
      renderTable();
    }

    function renderColumnsList() {
      $("columnsList").innerHTML = columnsDefault.map((col) => {
        const visible = state.visibleColumns.includes(col.key);
        return `
          <div class="col-item">
            <span style="opacity:.55">☰</span>
            <span>${col.label}</span>
            <span class="toggle" onclick="toggleColumn('${col.key}')">${visible ? "👁" : "🙈"}</span>
          </div>
        `;
      }).join("");
    }

    function toggleColumn(key) {
      if (state.visibleColumns.includes(key)) {
        state.visibleColumns = state.visibleColumns.filter((k) => k !== key);
      } else {
        state.visibleColumns.push(key);
      }
      renderColumnsList();
      renderTable();
    }

    async function loadData() {
      try {
        const data = await api("./api/entities?limit=5000&hours=24&sort_by=entity_id&sort_dir=asc");
        state.all = data.items || [];
        updateCatalog();
        renderSidebar();
        applyFilters();
      } catch (err) {
        $("tbody").innerHTML = `<tr><td colspan="12">Error cargando datos: ${esc(err.message)}</td></tr>`;
      }
    }

    async function refreshStatusChips() {
      try {
        const status = await api("./api/status");
        const mode = status.metrics_mode || "none";
        const txt = mode === "sqlite" ? "SQLite" : mode === "mariadb" ? "MariaDB" : "Sin fuente";
        document.title = `Recorder Control (${txt})`;
      } catch (_) {}
    }

    $("search").addEventListener("input", () => {
      state.search = $("search").value || "";
      applyFilters();
    });
    document.addEventListener("click", (ev) => {
      const t = ev.target;
      if (!(t instanceof Element)) return;
      if (!t.closest(".menu-btn") && !t.closest(".menu")) {
        $("groupMenu").classList.remove("show");
        $("sortMenu").classList.remove("show");
      }
      if (!t.closest(".chip-icon") && !t.closest(".quick")) {
        $("quickMenu").classList.remove("show");
      }
      if (t.id === "customizeModalBg") closeCustomize();
    });

    buildMenus();
    loadData();
    refreshStatusChips();
  </script>
</body>
</html>
        """
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(_, exc: HTTPException) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"ok": False, "error": str(exc.detail)},
    )
