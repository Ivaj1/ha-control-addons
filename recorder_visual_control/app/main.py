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
<!doctype html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Recorder Studio</title>
<style>
:root{--bg:#0f1117;--panel:#171b23;--line:#2a3240;--ink:#e6edf7;--muted:#9aa5b5;--accent:#4dabf7;--ok:#27ae60;--warn:#f39c12;--danger:#e74c3c}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 20% 0%,#1a2030,#0f1117 45%);color:var(--ink);font-family:Roboto,"Segoe UI",sans-serif}
.wrap{max-width:1280px;margin:0 auto;padding:14px;display:grid;gap:12px}.card{background:linear-gradient(180deg,#1b2230,#171b23);border:1px solid var(--line);border-radius:12px;padding:12px}
h1,h2{margin:0}.muted{color:var(--muted)}.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.chips{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px}
.chip{border:1px solid var(--line);border-radius:999px;padding:4px 10px;font-size:.82rem;background:#141925}.ok{color:var(--ok)}.warn{color:var(--warn)}
input,select{background:#111622;color:var(--ink);border:1px solid var(--line);border-radius:8px;padding:8px 10px}.grow{flex:1;min-width:180px}
button{border:1px solid transparent;border-radius:8px;padding:8px 10px;color:#fff;background:var(--accent);cursor:pointer}.ghost{background:#64748b}.danger{background:var(--danger)}.okb{background:var(--ok)}.warnb{background:var(--warn)}
.layout{display:grid;grid-template-columns:1.7fr 1fr;gap:12px}.table-wrap{max-height:58vh;overflow:auto;border:1px solid var(--line);border-radius:10px}
table{width:100%;border-collapse:collapse;font-size:.9rem}th,td{padding:8px;border-bottom:1px solid #232a37}th{position:sticky;top:0;background:#151b27;text-align:left;cursor:pointer}
.mono{font-family:ui-monospace,Menlo,Consolas,monospace}.clickable{color:#8bc4ff;cursor:pointer}.side dt{font-size:.78rem;color:var(--muted)}.side dd{margin:0 0 8px}
@media (max-width:1100px){.layout{grid-template-columns:1fr}}
</style></head><body>
<main class="wrap">
  <section class="card">
    <h1>Recorder Studio</h1><div class="muted">Gestión visual de recorder: filtros por entidad, purge y métricas.</div>
    <div class="chips"><span id="setupChip" class="chip">setup</span><span id="recChip" class="chip">recorder</span><span id="modeChip" class="chip">metrics</span><span id="countChip" class="chip">0</span></div>
  </section>
  <section class="card row">
    <input id="search" class="grow" placeholder="Buscar entidad o nombre">
    <select id="domainFilter"><option value="all">Todos los dominios</option></select>
    <select id="excludedFilter"><option value="all">Incluidas + Excluidas</option><option value="included">Solo incluidas</option><option value="excluded">Solo excluidas</option></select>
    <select id="windowHours"><option value="1">1h</option><option value="6">6h</option><option value="24" selected>24h</option><option value="72">72h</option><option value="168">7d</option></select>
    <select id="sortBy"><option value="state_changes">Cambios</option><option value="changes_per_hour">Cambios/h</option><option value="last_updated_ts">Última escritura</option><option value="entity_id">Entidad</option><option value="friendly_name">Nombre</option><option value="domain">Dominio</option></select>
    <select id="sortDir"><option value="desc">Desc</option><option value="asc">Asc</option></select>
    <button onclick="loadEntities()">Aplicar</button><button class="ghost" onclick="refreshAll()">Actualizar</button>
    <button class="warnb" onclick="applyChanges()">Aplicar configuración (reiniciar HA)</button>
  </section>
  <section class="layout">
    <div class="card">
      <div class="row" style="margin-bottom:8px">
        <button class="okb" onclick="setRecorder(true)">Activar recorder</button><button class="danger" onclick="setRecorder(false)">Desactivar recorder</button>
        <input id="purgeKeepDays" type="number" min="1" max="3650" value="10"><label><input id="purgeRepack" type="checkbox"> repack</label><label><input id="purgeApplyFilter" type="checkbox"> apply_filter</label>
        <button class="warnb" onclick="purgeGlobal()">Purge global</button>
      </div>
      <div class="row" style="margin-bottom:8px">
        <input id="purgeEntitiesKeepDays" type="number" min="0" max="3650" value="0"><input id="purgeDomains" class="grow" placeholder="domains separados por coma"><input id="purgeGlobs" class="grow" placeholder="entity_globs separados por coma">
        <button class="danger" onclick="purgeSelected()">Purge selección</button><button class="ghost" onclick="clearSelection()">Limpiar selección</button>
      </div>
      <div class="table-wrap"><table><thead><tr><th>Sel</th><th data-sort="entity_id">Entidad</th><th data-sort="friendly_name">Nombre</th><th data-sort="domain">Dom</th><th data-sort="state_changes">Cambios</th><th data-sort="changes_per_hour">/h</th><th data-sort="last_updated_ts">Última</th><th>Recorder</th><th>Acción</th></tr></thead><tbody id="tbody"><tr><td colspan="9" class="muted">Cargando...</td></tr></tbody></table></div>
    </div>
    <aside class="card side">
      <h2>Detalle entidad</h2><div id="detailEmpty" class="muted">Haz click en una entidad para ver detalle.</div>
      <div id="detail" style="display:none">
        <div id="dEntity" class="mono"></div><div id="dName"></div><div id="dState" class="row" style="margin:8px 0"></div>
        <dl><dt>1h</dt><dd id="d1h"></dd><dt>24h</dt><dd id="d24h"></dd><dt>7d</dt><dd id="d7d"></dd><dt>Último cambio</dt><dd id="dLastChanged"></dd><dt>Última actualización</dt><dd id="dLastUpdated"></dd></dl>
        <div class="row"><button id="dOpenHA">Abrir en Home Assistant</button><button class="ghost" id="dToggle">Toggle incluir/excluir</button></div>
      </div>
    </aside>
  </section>
</main>
<script>
const $=id=>document.getElementById(id);const selected=new Set();let currentItems=[];let currentDetail=null;
async function api(path,opts={}){const r=await fetch(path,opts);if(!r.ok)throw new Error(await r.text());return r.json();}
const esc=v=>String(v??"").replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;");
const ts=v=>!v?"-":new Date(Number(v)*1000).toLocaleString();
const splitCsv=t=>String(t||"").split(",").map(s=>s.trim()).filter(Boolean);
function updateChips(s){$("setupChip").textContent=s.setup.configured?"Setup OK":"Setup pendiente";$("setupChip").className="chip "+(s.setup.configured?"ok":"warn");$("recChip").textContent=s.recorder_recording?"Recorder activo":"Recorder inactivo";$("countChip").textContent="Excluidas: "+(s.excluded_count||0);$("modeChip").textContent="Métricas: "+(s.metrics_mode||"none")}
async function refreshStatus(){updateChips(await api("./api/status"))}
function markSelected(id,checked){checked?selected.add(id):selected.delete(id)}
async function loadEntities(){
  $("tbody").innerHTML="<tr><td colspan='9' class='muted'>Cargando...</td></tr>";
  const q=new URLSearchParams({search:$("search").value||"",hours:$("windowHours").value||"24",limit:"700",domain:$("domainFilter").value||"all",excluded:$("excludedFilter").value||"all",sort_by:$("sortBy").value||"state_changes",sort_dir:$("sortDir").value||"desc"});
  const data=await api("./api/entities?"+q.toString());currentItems=data.items||[];
  const domSel=$("domainFilter");if(domSel.options.length<=1){for(const d of (data.domains||[])){const o=document.createElement("option");o.value=d;o.textContent=d;domSel.appendChild(o)}}
  if(!currentItems.length){$("tbody").innerHTML="<tr><td colspan='9' class='muted'>Sin resultados</td></tr>";return;}
  $("tbody").innerHTML=currentItems.map(it=>`<tr>
    <td><input type="checkbox" ${selected.has(it.entity_id)?"checked":""} onchange="markSelected('${esc(it.entity_id)}',this.checked)"></td>
    <td class="mono clickable" onclick="openDetail('${esc(it.entity_id)}')">${esc(it.entity_id)}</td>
    <td>${esc(it.friendly_name||"-")}</td><td>${esc(it.domain)}</td><td>${Number(it.state_changes||0)}</td><td>${Number(it.changes_per_hour||0).toFixed(2)}</td><td>${esc(ts(it.last_updated_ts))}</td>
    <td style="color:${it.excluded_by_app?'#e74c3c':'#27ae60'}">${it.excluded_by_app?'Excluida':'Incluida'}</td>
    <td><button onclick="toggleEntity('${esc(it.entity_id)}',${it.excluded_by_app?"false":"true"})">${it.excluded_by_app?"Incluir":"Excluir"}</button></td></tr>`).join("");
}
async function openDetail(entityId){
  const d=await api("./api/entity/"+encodeURIComponent(entityId)+"/details");currentDetail=d;
  $("detailEmpty").style.display="none";$("detail").style.display="block";$("dEntity").textContent=d.entity_id;$("dName").textContent=d.friendly_name||"-";
  $("dState").textContent="Estado: "+(d.state??"-")+" "+(d.unit_of_measurement||"");
  const w=d.statistics?.windows||{};$("d1h").textContent=`${w["1"]?.state_changes||0} cambios (${(w["1"]?.changes_per_hour||0).toFixed(2)}/h)`;
  $("d24h").textContent=`${w["24"]?.state_changes||0} cambios (${(w["24"]?.changes_per_hour||0).toFixed(2)}/h)`;
  $("d7d").textContent=`${w["168"]?.state_changes||0} cambios (${(w["168"]?.changes_per_hour||0).toFixed(2)}/h)`;
  $("dLastChanged").textContent=d.last_changed||"-";$("dLastUpdated").textContent=d.last_updated||"-";
  $("dOpenHA").onclick=()=>window.open(d.ha_entity_url,"_blank");
  const row=currentItems.find(x=>x.entity_id===d.entity_id);const excluded=Boolean(row?.excluded_by_app);
  $("dToggle").textContent=excluded?"Incluir en recorder":"Excluir de recorder";
  $("dToggle").onclick=async()=>{await toggleEntity(d.entity_id,!excluded);}
}
async function toggleEntity(entityId,exclude){await api("./api/entities/"+encodeURIComponent(entityId),{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({exclude})});await refreshAll()}
async function setRecorder(enable){await api(enable?"./api/recorder/enable":"./api/recorder/disable",{method:"POST"});await refreshStatus()}
async function purgeGlobal(){if(!confirm("Lanzar purge global?"))return;await api("./api/recorder/purge",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({keep_days:Number($("purgeKeepDays").value||10),repack:Boolean($("purgeRepack").checked),apply_filter:Boolean($("purgeApplyFilter").checked)})});alert("Purge lanzado")}
async function purgeSelected(){const entities=[...selected],domains=splitCsv($("purgeDomains").value),globs=splitCsv($("purgeGlobs").value);if(!entities.length&&!domains.length&&!globs.length){alert("Selecciona algo");return}
if(!confirm("Lanzar purge_entities?"))return;await api("./api/recorder/purge_entities",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({entity_ids:entities,domains:domains,entity_globs:globs,keep_days:Number($("purgeEntitiesKeepDays").value||0)})});alert("Purge_entities lanzado")}
function clearSelection(){selected.clear();loadEntities()}
async function applyChanges(){if(!confirm("Reiniciar Core para aplicar filtros?"))return;await api("./api/apply",{method:"POST"});alert("Reinicio solicitado")}
async function refreshAll(){try{await refreshStatus();await loadEntities()}catch(e){$("tbody").innerHTML=`<tr><td colspan="9">${esc(e.message)}</td></tr>`}}
["search","windowHours","domainFilter","excludedFilter","sortBy","sortDir"].forEach(id=>$(id).addEventListener("change",loadEntities));$("search").addEventListener("keydown",e=>{if(e.key==="Enter")loadEntities()});
document.querySelectorAll("th[data-sort]").forEach(th=>th.addEventListener("click",()=>{$("sortBy").value=th.dataset.sort;$("sortDir").value=$("sortDir").value==="asc"?"desc":"asc";loadEntities()}));
refreshAll();
</script></body></html>
        """
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(_, exc: HTTPException) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"ok": False, "error": str(exc.detail)},
    )
