import asyncio
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

app = FastAPI(title="Recorder Visual Control", version="0.7.2")
_REGISTRY_CACHE: dict[str, Any] = {"ts": 0.0, "data": None}
_STATES_CACHE: dict[str, Any] = {"ts": 0.0, "data": None}
_ACTIVITY_CACHE: dict[str, Any] = {}
_DBSTATS_CACHE: dict[str, Any] = {}


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


async def _ws_run_commands(commands: list[dict[str, Any]]) -> list[Any]:
    """Run multiple WS commands in one authenticated session."""
    try:
        async with websockets.connect(CORE_WS_URL) as ws:
            hello = json.loads(await ws.recv())
            if hello.get("type") != "auth_required":
                raise RuntimeError("Unexpected WebSocket handshake")
            await ws.send(json.dumps({"type": "auth", "access_token": SUPERVISOR_TOKEN}))
            auth_response = json.loads(await ws.recv())
            if auth_response.get("type") != "auth_ok":
                raise RuntimeError("WebSocket auth failed")

            pending: dict[int, int] = {}
            results: list[Any] = [None] * len(commands)
            msg_id = 1
            for index, cmd in enumerate(commands):
                payload = {"id": msg_id, **cmd}
                pending[msg_id] = index
                await ws.send(json.dumps(payload))
                msg_id += 1

            while pending:
                incoming = json.loads(await ws.recv())
                incoming_id = incoming.get("id")
                if incoming_id not in pending:
                    continue
                idx = pending.pop(incoming_id)
                if incoming.get("success", False):
                    results[idx] = incoming.get("result")
                else:
                    results[idx] = None
            return results
    except Exception:
        return [None] * len(commands)


async def _get_registry_metadata() -> dict[str, Any]:
    """Fetch area/device/entity registry and map entity -> area/manufacturer/model/platform."""
    now = time.time()
    cache_data = _REGISTRY_CACHE.get("data")
    if cache_data and (now - float(_REGISTRY_CACHE.get("ts", 0.0)) < 30):
        return cache_data

    area_list, device_list, entity_list = await _ws_run_commands(
        [
            {"type": "config/area_registry/list"},
            {"type": "config/device_registry/list"},
            {"type": "config/entity_registry/list"},
        ]
    )
    area_map: dict[str, str] = {}
    device_map: dict[str, dict[str, Any]] = {}
    entity_map: dict[str, dict[str, Any]] = {}

    if isinstance(area_list, list):
        for area in area_list:
            if not isinstance(area, dict):
                continue
            area_id = str(area.get("area_id", "")).strip()
            name = str(area.get("name", "")).strip()
            if area_id and name:
                area_map[area_id] = name

    if isinstance(device_list, list):
        for device in device_list:
            if not isinstance(device, dict):
                continue
            device_id = str(device.get("id", "")).strip()
            if not device_id:
                continue
            device_map[device_id] = {
                "area_id": str(device.get("area_id", "")).strip(),
                "manufacturer": str(device.get("manufacturer", "")).strip(),
                "model": str(device.get("model", "")).strip(),
                "name": str(device.get("name_by_user") or device.get("name") or "").strip(),
            }

    if isinstance(entity_list, list):
        for entity in entity_list:
            if not isinstance(entity, dict):
                continue
            entity_id = str(entity.get("entity_id", "")).strip().lower()
            if not entity_id:
                continue
            labels = entity.get("labels") if isinstance(entity.get("labels"), list) else []
            entity_map[entity_id] = {
                "area_id": str(entity.get("area_id", "")).strip(),
                "device_id": str(entity.get("device_id", "")).strip(),
                "platform": str(entity.get("platform", "")).strip(),
                "name": str(entity.get("name") or "").strip(),
                "labels": [str(item).strip() for item in labels if str(item).strip()],
            }

    payload = {"areas": area_map, "devices": device_map, "entities": entity_map}
    _REGISTRY_CACHE["ts"] = now
    _REGISTRY_CACHE["data"] = payload
    return payload


async def _list_states() -> list[dict[str, Any]]:
    cache_data = _STATES_CACHE.get("data")
    if cache_data and (time.time() - float(_STATES_CACHE.get("ts", 0.0)) < 20):
        return cache_data
    url = f"{CORE_HTTP_BASE}/api/states"
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        response = await client.get(url, headers=_auth_headers())
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail=response.text)
    states = response.json()
    if not isinstance(states, list):
        raise HTTPException(status_code=502, detail="Invalid /api/states response")
    _STATES_CACHE["ts"] = time.time()
    _STATES_CACHE["data"] = states
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
    cache_key = f"{hours}:{limit}"
    cache_item = _ACTIVITY_CACHE.get(cache_key)
    if cache_item and (time.time() - float(cache_item.get("ts", 0.0)) < 45):
        return cache_item["data"]

    sqlite_metrics = await asyncio.to_thread(
        _query_activity_metrics_sqlite, hours, limit
    )
    if sqlite_metrics.get("available"):
        _ACTIVITY_CACHE[cache_key] = {"ts": time.time(), "data": sqlite_metrics}
        return sqlite_metrics
    mariadb_metrics = await asyncio.to_thread(
        _query_activity_metrics_mariadb, hours, limit
    )
    if mariadb_metrics.get("available"):
        _ACTIVITY_CACHE[cache_key] = {"ts": time.time(), "data": mariadb_metrics}
        return mariadb_metrics
    result = {
        "available": False,
        "reason": "No se pudo obtener metricas de SQLite ni MariaDB.",
        "window_hours": hours,
        "items": [],
        "by_entity_id": {},
        "source": "none",
    }
    _ACTIVITY_CACHE[cache_key] = {"ts": time.time(), "data": result}
    return result


def _query_dbstats_sqlite(limit: int = 2000) -> dict[str, Any]:
    db_path = _detect_sqlite_db_path()
    if db_path is None:
        return {"available": False, "source": "sqlite", "by_entity_id": {}}

    by_entity: dict[str, dict[str, Any]] = {}
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=8)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        metadata_ids: list[int] = []
        entity_ids: list[str] = []
        try:
            cur.execute(
                """
                SELECT s.metadata_id AS metadata_id, sm.entity_id AS entity_id, COUNT(*) AS cnt
                FROM states s
                JOIN states_meta sm ON sm.metadata_id = s.metadata_id
                GROUP BY s.metadata_id, sm.entity_id
                ORDER BY cnt DESC
                LIMIT ?
                """,
                (max(1, limit),),
            )
            rows = cur.fetchall()
            for row in rows:
                metadata_id = int(row["metadata_id"] or 0)
                entity_id = str(row["entity_id"]).strip().lower()
                if not entity_id:
                    continue
                if metadata_id > 0:
                    metadata_ids.append(metadata_id)
                by_entity.setdefault(entity_id, {})["db_rows_total"] = int(row["cnt"] or 0)
        except sqlite3.DatabaseError:
            cur.execute(
                """
                SELECT s.entity_id AS entity_id, COUNT(*) AS cnt
                FROM states s
                GROUP BY s.entity_id
                ORDER BY cnt DESC
                LIMIT ?
                """,
                (max(1, limit),),
            )
            rows = cur.fetchall()
            for row in rows:
                entity_id = str(row["entity_id"]).strip().lower()
                if entity_id:
                    entity_ids.append(entity_id)
                    by_entity.setdefault(entity_id, {})["db_rows_total"] = int(row["cnt"] or 0)

        # dbstats-like attr sizing but scoped to selected entities for speed.
        try:
            if metadata_ids:
                placeholders = ",".join("?" for _ in metadata_ids)
                cur.execute(
                    f"""
                    SELECT sm.entity_id AS entity_id, SUM(COALESCE(attr.attr_size, 0))/1024.0/1024.0 AS size_mb
                    FROM (
                      SELECT s.metadata_id AS metadata_id, s.attributes_id AS attributes_id
                      FROM states s
                      WHERE s.metadata_id IN ({placeholders})
                      GROUP BY s.metadata_id, s.attributes_id
                    ) entity_attrs
                    JOIN states_meta sm ON sm.metadata_id = entity_attrs.metadata_id
                    LEFT JOIN (
                      SELECT attributes_id, LENGTH(shared_attrs) AS attr_size
                      FROM state_attributes
                    ) attr ON attr.attributes_id = entity_attrs.attributes_id
                    GROUP BY sm.entity_id
                    ORDER BY size_mb DESC
                    LIMIT ?
                    """,
                    [*metadata_ids, max(1, limit)],
                )
                rows = cur.fetchall()
            elif entity_ids:
                placeholders = ",".join("?" for _ in entity_ids)
                cur.execute(
                    f"""
                    SELECT entity_attrs.entity_id AS entity_id, SUM(COALESCE(attr.attr_size, 0))/1024.0/1024.0 AS size_mb
                    FROM (
                      SELECT s.entity_id AS entity_id, s.attributes_id AS attributes_id
                      FROM states s
                      WHERE s.entity_id IN ({placeholders})
                      GROUP BY s.entity_id, s.attributes_id
                    ) entity_attrs
                    LEFT JOIN (
                      SELECT attributes_id, LENGTH(shared_attrs) AS attr_size
                      FROM state_attributes
                    ) attr ON attr.attributes_id = entity_attrs.attributes_id
                    GROUP BY entity_attrs.entity_id
                    ORDER BY size_mb DESC
                    LIMIT ?
                    """,
                    [*entity_ids, max(1, limit)],
                )
                rows = cur.fetchall()
            else:
                rows = []

            for row in rows:
                entity_id = str(row["entity_id"]).strip().lower()
                if entity_id:
                    by_entity.setdefault(entity_id, {})["db_attrs_mb"] = round(
                        float(row["size_mb"] or 0.0), 4
                    )
        except sqlite3.DatabaseError:
            pass
        conn.close()
    except Exception:
        return {"available": False, "source": "sqlite", "by_entity_id": {}}

    return {"available": True, "source": "sqlite", "by_entity_id": by_entity}


def _query_dbstats_mariadb(limit: int = 2000) -> dict[str, Any]:
    db_url = _extract_recorder_db_url_from_config()
    if not db_url:
        return {"available": False, "source": "mariadb", "by_entity_id": {}}
    parsed = urlparse(db_url)
    if parsed.scheme not in {"mysql", "mysql+pymysql", "mariadb", "mariadb+pymysql"}:
        return {"available": False, "source": "mariadb", "by_entity_id": {}}

    host = parsed.hostname or "core-mariadb"
    port = parsed.port or 3306
    user = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    database = (parsed.path or "/").lstrip("/") or "homeassistant"
    query_params = parse_qs(parsed.query)
    unix_socket = query_params.get("unix_socket", [None])[0]
    by_entity: dict[str, dict[str, Any]] = {}
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
            metadata_ids: list[int] = []
            entity_ids: list[str] = []
            try:
                cur.execute(
                    """
                    SELECT s.metadata_id AS metadata_id, sm.entity_id AS entity_id, COUNT(*) AS cnt
                    FROM states s
                    JOIN states_meta sm ON sm.metadata_id = s.metadata_id
                    GROUP BY s.metadata_id, sm.entity_id
                    ORDER BY cnt DESC
                    LIMIT %s
                    """,
                    (max(1, limit),),
                )
                rows = cur.fetchall()
                for row in rows:
                    entity_id = str(row.get("entity_id") or "").strip().lower()
                    if not entity_id:
                        continue
                    try:
                        metadata_id = int(row.get("metadata_id") or 0)
                    except (TypeError, ValueError):
                        metadata_id = 0
                    if metadata_id > 0:
                        metadata_ids.append(metadata_id)
                    by_entity.setdefault(entity_id, {})["db_rows_total"] = int(
                        row.get("cnt") or 0
                    )
            except Exception:
                cur.execute(
                    """
                    SELECT s.entity_id AS entity_id, COUNT(*) AS cnt
                    FROM states s
                    GROUP BY s.entity_id
                    ORDER BY cnt DESC
                    LIMIT %s
                    """,
                    (max(1, limit),),
                )
                rows = cur.fetchall()
                for row in rows:
                    entity_id = str(row.get("entity_id") or "").strip().lower()
                    if entity_id:
                        entity_ids.append(entity_id)
                        by_entity.setdefault(entity_id, {})["db_rows_total"] = int(
                            row.get("cnt") or 0
                        )

            try:
                if metadata_ids:
                    placeholders = ",".join(["%s"] * len(metadata_ids))
                    cur.execute(
                        f"""
                        SELECT sm.entity_id AS entity_id, SUM(COALESCE(attr.attr_size, 0))/1024.0/1024.0 AS size_mb
                        FROM (
                          SELECT s.metadata_id AS metadata_id, s.attributes_id AS attributes_id
                          FROM states s
                          WHERE s.metadata_id IN ({placeholders})
                          GROUP BY s.metadata_id, s.attributes_id
                        ) entity_attrs
                        JOIN states_meta sm ON sm.metadata_id = entity_attrs.metadata_id
                        LEFT JOIN (
                          SELECT attributes_id, CHAR_LENGTH(shared_attrs) AS attr_size
                          FROM state_attributes
                        ) attr ON attr.attributes_id = entity_attrs.attributes_id
                        GROUP BY sm.entity_id
                        ORDER BY size_mb DESC
                        LIMIT %s
                        """,
                        [*metadata_ids, max(1, limit)],
                    )
                    rows = cur.fetchall()
                elif entity_ids:
                    placeholders = ",".join(["%s"] * len(entity_ids))
                    cur.execute(
                        f"""
                        SELECT entity_attrs.entity_id AS entity_id, SUM(COALESCE(attr.attr_size, 0))/1024.0/1024.0 AS size_mb
                        FROM (
                          SELECT s.entity_id AS entity_id, s.attributes_id AS attributes_id
                          FROM states s
                          WHERE s.entity_id IN ({placeholders})
                          GROUP BY s.entity_id, s.attributes_id
                        ) entity_attrs
                        LEFT JOIN (
                          SELECT attributes_id, CHAR_LENGTH(shared_attrs) AS attr_size
                          FROM state_attributes
                        ) attr ON attr.attributes_id = entity_attrs.attributes_id
                        GROUP BY entity_attrs.entity_id
                        ORDER BY size_mb DESC
                        LIMIT %s
                        """,
                        [*entity_ids, max(1, limit)],
                    )
                    rows = cur.fetchall()
                else:
                    rows = []

                for row in rows:
                    entity_id = str(row.get("entity_id") or "").strip().lower()
                    if entity_id:
                        by_entity.setdefault(entity_id, {})["db_attrs_mb"] = round(
                            float(row.get("size_mb") or 0.0), 4
                        )
            except Exception:
                pass
        conn.close()
    except Exception:
        return {"available": False, "source": "mariadb", "by_entity_id": {}}
    return {"available": True, "source": "mariadb", "by_entity_id": by_entity}


def _query_dbstats_metrics(limit: int = 2000) -> dict[str, Any]:
    cache_key = f"limit:{limit}"
    cache_item = _DBSTATS_CACHE.get(cache_key)
    if cache_item and (time.time() - float(cache_item.get("ts", 0.0)) < 240):
        return cache_item["data"]
    sqlite_stats = _query_dbstats_sqlite(limit=limit)
    if sqlite_stats.get("available"):
        _DBSTATS_CACHE[cache_key] = {"ts": time.time(), "data": sqlite_stats}
        return sqlite_stats
    mariadb_stats = _query_dbstats_mariadb(limit=limit)
    if mariadb_stats.get("available"):
        _DBSTATS_CACHE[cache_key] = {"ts": time.time(), "data": mariadb_stats}
        return mariadb_stats
    result = {"available": False, "source": "none", "by_entity_id": {}}
    _DBSTATS_CACHE[cache_key] = {"ts": time.time(), "data": result}
    return result


def _metric_sort_key(item: dict[str, Any], field: str) -> Any:
    if field in {
        "state_changes",
        "changes_per_hour",
        "last_updated_ts",
        "battery",
        "db_rows_total",
        "db_attrs_mb",
    }:
        return float(item.get(field, 0))
    return str(item.get(field, "")).lower()


async def _query_entity_detail_stats(entity_id: str) -> dict[str, Any]:
    windows = [1, 24, 168]
    summary: dict[str, Any] = {"windows": {}, "source": "none"}
    metrics_list = await asyncio.gather(
        *(_query_activity_metrics(hours=hours, limit=10000) for hours in windows)
    )
    for hours, metrics in zip(windows, metrics_list):
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
    states, registry, metrics, dbstats = await asyncio.gather(
        _list_states(),
        _get_registry_metadata(),
        _query_activity_metrics(hours=hours, limit=4000),
        asyncio.to_thread(_query_dbstats_metrics, 1800),
    )
    reg_areas = registry.get("areas", {})
    reg_devices = registry.get("devices", {})
    reg_entities = registry.get("entities", {})
    excluded_entities = _load_excluded_entities()
    metrics_map = metrics["by_entity_id"]
    dbstats_map = dbstats["by_entity_id"]
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
        m_db = dbstats_map.get(normalized_entity_id, {})
        reg_entity = reg_entities.get(normalized_entity_id, {})
        reg_device = reg_devices.get(reg_entity.get("device_id", ""), {})
        area_name = (
            str(attributes.get("area_name") or attributes.get("area") or attributes.get("suggested_area") or "").strip()
        )
        if not area_name:
            area_id = str(reg_entity.get("area_id") or reg_device.get("area_id") or "").strip()
            area_name = str(reg_areas.get(area_id, "")).strip()
        integration_name = str(
            attributes.get("integration")
            or reg_entity.get("platform")
            or domain_name
        ).strip()
        manufacturer = str(
            attributes.get("manufacturer")
            or attributes.get("device_manufacturer")
            or reg_device.get("manufacturer")
            or ""
        ).strip()
        model = str(
            attributes.get("model")
            or attributes.get("device_model")
            or reg_device.get("model")
            or ""
        ).strip()
        battery_raw = attributes.get("battery_level", attributes.get("battery"))
        try:
            battery = float(battery_raw) if battery_raw is not None else None
        except (TypeError, ValueError):
            battery = None
        state_value = str(item.get("state", ""))
        tags = []
        tags_attr = attributes.get("labels") or attributes.get("tags") or []
        if isinstance(tags_attr, list):
            tags.extend([str(tag).strip() for tag in tags_attr if str(tag).strip()])
        reg_labels = reg_entity.get("labels") if isinstance(reg_entity.get("labels"), list) else []
        tags.extend([str(tag).strip() for tag in reg_labels if str(tag).strip()])
        tags = sorted({tag for tag in tags if tag})
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
                "db_rows_total": int(m_db.get("db_rows_total", 0)),
                "db_attrs_mb": float(m_db.get("db_attrs_mb", 0.0)),
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
        "db_rows_total",
        "db_attrs_mb",
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
    all_areas = sorted(
        {
            name
            for name in reg_areas.values()
            if isinstance(name, str) and name.strip()
        }
    )

    return {
        "items": entities,
        "total": len(entities),
        "managed_file": str(MANAGED_LIST_FILE),
        "domains": all_domains,
        "areas": all_areas,
        "metrics": {
            "available": metrics["available"],
            "reason": metrics["reason"],
            "window_hours": metrics["window_hours"],
            "source": metrics.get("source"),
            "db_path": metrics.get("db_path"),
            "db_size_mb": metrics.get("db_size_mb"),
            "query_used": metrics.get("query_used"),
            "dbstats_source": dbstats.get("source"),
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
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg:        #080b10;
      --panel:     #0d1018;
      --panel-2:   #111520;
      --sidebar-bg:#090c12;
      --line:      #1a2030;
      --line-soft: #131825;
      --text:      #dce4ef;
      --muted:     #5a6880;
      --muted-2:   #8898b0;
      --accent:    #10b981;
      --accent-dim:#064032;
      --accent-glow: rgba(16,185,129,.15);
      --danger:    #f87171;
      --danger-dim:#3b0f0f;
      --warn:      #fbbf24;
      --radius-sm: 6px;
      --radius:    10px;
      --radius-lg: 16px;
    }
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background: var(--bg);
      color: var(--text);
      font-family: "IBM Plex Sans", system-ui, sans-serif;
      font-size: 13px;
      min-height: 100vh;
      -webkit-font-smoothing: antialiased;
    }

    /* ── TOPBAR ─────────────────────────────────── */
    .topbar {
      height: 52px;
      background: var(--panel);
      border-bottom: 1px solid var(--line);
      display: grid;
      grid-template-columns: 52px 1fr 52px;
      align-items: center;
      position: sticky;
      top: 0;
      z-index: 100;
    }
    .topbar-back {
      justify-self: center;
      color: var(--muted-2);
      font-size: 18px;
      cursor: pointer;
      width: 32px;
      height: 32px;
      display: grid;
      place-items: center;
      border-radius: var(--radius-sm);
      transition: background .15s, color .15s;
    }
    .topbar-back:hover { background: var(--line); color: var(--text); }
    .topbar-title {
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 10px;
    }
    .topbar-logo {
      width: 22px;
      height: 22px;
      border-radius: 6px;
      background: var(--accent);
      display: grid;
      place-items: center;
      flex-shrink: 0;
    }
    .topbar-logo svg { width: 13px; height: 13px; fill: #fff; }
    .topbar-name {
      font-size: 13px;
      font-weight: 600;
      color: var(--text);
      letter-spacing: .02em;
    }
    .topbar-badge {
      font-size: 10px;
      font-weight: 500;
      font-family: "IBM Plex Mono", monospace;
      background: var(--accent-dim);
      color: var(--accent);
      border: 1px solid var(--accent);
      border-radius: 4px;
      padding: 1px 5px;
      opacity: .9;
    }
    .topbar-menu-btn {
      justify-self: center;
      color: var(--muted-2);
      font-size: 18px;
      cursor: pointer;
      width: 32px;
      height: 32px;
      display: grid;
      place-items: center;
      border-radius: var(--radius-sm);
    }

    /* ── LAYOUT ─────────────────────────────────── */
    .shell {
      display: grid;
      grid-template-columns: 200px 1fr;
      min-height: calc(100vh - 52px);
    }

    /* ── SIDEBAR ────────────────────────────────── */
    .sidebar {
      background: var(--sidebar-bg);
      border-right: 1px solid var(--line);
      display: flex;
      flex-direction: column;
    }
    .sidebar-header {
      padding: 12px 12px 8px;
      border-bottom: 1px solid var(--line-soft);
    }
    .sidebar-label {
      font-size: 10px;
      font-weight: 600;
      letter-spacing: .1em;
      text-transform: uppercase;
      color: var(--muted);
      padding: 0 4px;
      margin-bottom: 4px;
    }
    .filters-btn {
      width: 100%;
      height: 32px;
      border: 1px solid var(--line);
      border-radius: var(--radius-sm);
      background: var(--panel-2);
      color: var(--muted-2);
      display: flex;
      align-items: center;
      gap: 7px;
      padding: 0 10px;
      font-size: 12px;
      font-family: inherit;
      cursor: pointer;
      transition: border-color .15s, color .15s;
    }
    .filters-btn:hover { border-color: var(--accent); color: var(--text); }
    .sidebar-content { flex: 1; overflow-y: auto; }
    .section { border-bottom: 1px solid var(--line-soft); }
    .section-head {
      width: 100%;
      height: 36px;
      background: transparent;
      color: var(--muted-2);
      border: 0;
      text-align: left;
      padding: 0 12px;
      display: flex;
      align-items: center;
      gap: 6px;
      font-size: 11px;
      font-weight: 600;
      font-family: inherit;
      letter-spacing: .06em;
      text-transform: uppercase;
      cursor: pointer;
      transition: color .15s;
    }
    .section-head:hover { color: var(--text); }
    .section-head .chevron {
      margin-left: auto;
      font-size: 9px;
      transition: transform .2s;
      color: var(--muted);
    }
    .section.open .chevron { transform: rotate(180deg); }
    .section-count {
      font-size: 10px;
      background: var(--accent-dim);
      color: var(--accent);
      border-radius: 3px;
      padding: 0 4px;
      font-weight: 500;
      display: none;
    }
    .section-count.visible { display: inline; }
    .section-items { display: none; padding: 4px 8px 8px; }
    .section.open .section-items { display: block; }
    .section-item {
      display: flex;
      align-items: center;
      gap: 7px;
      font-size: 12px;
      color: var(--muted-2);
      padding: 4px 4px;
      border-radius: var(--radius-sm);
      cursor: pointer;
      transition: color .1s, background .1s;
    }
    .section-item:hover { color: var(--text); background: var(--line-soft); }
    .section-item input[type="checkbox"] { accent-color: var(--accent); width: 13px; height: 13px; }

    /* ── MAIN ───────────────────────────────────── */
    .main {
      display: flex;
      flex-direction: column;
      min-width: 0;
      background: var(--bg);
    }

    /* ── TOOLBAR ────────────────────────────────── */
    .toolbar {
      border-bottom: 1px solid var(--line);
      background: var(--panel);
      padding: 8px 12px;
      display: flex;
      align-items: center;
      gap: 6px;
      position: relative;
      flex-shrink: 0;
    }
    .icon-btn {
      width: 32px;
      height: 32px;
      border-radius: var(--radius-sm);
      border: 1px solid var(--line);
      background: var(--panel-2);
      color: var(--muted-2);
      display: grid;
      place-items: center;
      font-size: 14px;
      cursor: pointer;
      transition: border-color .15s, color .15s, background .15s;
      flex-shrink: 0;
    }
    .icon-btn:hover { border-color: var(--accent); color: var(--accent); background: var(--accent-dim); }
    .search-wrap {
      flex: 1;
      position: relative;
      min-width: 0;
    }
    .search-icon {
      position: absolute;
      left: 10px;
      top: 50%;
      transform: translateY(-50%);
      color: var(--muted);
      font-size: 13px;
      pointer-events: none;
    }
    .search {
      width: 100%;
      height: 32px;
      border: 1px solid var(--line);
      border-radius: var(--radius-sm);
      background: var(--panel-2);
      color: var(--text);
      padding: 0 10px 0 30px;
      font-size: 13px;
      font-family: inherit;
      outline: none;
      transition: border-color .15s;
    }
    .search:focus { border-color: var(--accent); }
    .search::placeholder { color: var(--muted); }
    .menu-btn {
      height: 32px;
      border-radius: var(--radius-sm);
      border: 1px solid var(--line);
      background: var(--panel-2);
      color: var(--muted-2);
      padding: 0 10px;
      font-size: 12px;
      font-family: inherit;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      white-space: nowrap;
      cursor: pointer;
      transition: border-color .15s, color .15s;
    }
    .menu-btn:hover { border-color: var(--accent); color: var(--text); }
    .toolbar-sep {
      width: 1px;
      height: 20px;
      background: var(--line);
      flex-shrink: 0;
    }

    /* ── DROPDOWN MENUS ─────────────────────────── */
    .menu {
      position: absolute;
      top: 44px;
      z-index: 200;
      min-width: 180px;
      border: 1px solid var(--line);
      border-radius: var(--radius);
      background: var(--panel-2);
      box-shadow: 0 12px 32px rgba(0,0,0,.6);
      padding: 4px;
      display: none;
    }
    .menu.show { display: block; }
    .menu-item {
      width: 100%;
      height: 32px;
      border-radius: var(--radius-sm);
      border: 0;
      background: transparent;
      color: var(--muted-2);
      text-align: left;
      padding: 0 10px;
      font-size: 13px;
      font-family: inherit;
      cursor: pointer;
      display: flex;
      align-items: center;
      gap: 8px;
      transition: background .1s, color .1s;
    }
    .menu-item:hover { background: var(--line); color: var(--text); }
    .menu-item.active { background: var(--accent-dim); color: var(--accent); }
    .menu-item.active::before { content: "✓"; font-size: 11px; margin-right: 2px; }

    /* ── QUICK ACTIONS MENU ─────────────────────── */
    .quick {
      position: absolute;
      right: 12px;
      top: 44px;
      min-width: 220px;
      background: var(--panel-2);
      border: 1px solid var(--line);
      border-radius: var(--radius);
      padding: 4px;
      display: none;
      z-index: 200;
      box-shadow: 0 12px 32px rgba(0,0,0,.6);
    }
    .quick.show { display: block; }
    .quick-section-label {
      font-size: 10px;
      font-weight: 600;
      letter-spacing: .08em;
      text-transform: uppercase;
      color: var(--muted);
      padding: 6px 10px 2px;
    }
    .quick button {
      width: 100%;
      border: 0;
      border-radius: var(--radius-sm);
      background: transparent;
      color: var(--muted-2);
      text-align: left;
      padding: 7px 10px;
      font-size: 13px;
      font-family: inherit;
      cursor: pointer;
      transition: background .1s, color .1s;
    }
    .quick button:hover { background: var(--line); color: var(--text); }
    .quick button.danger:hover { background: var(--danger-dim); color: var(--danger); }
    .quick-sep { height: 1px; background: var(--line-soft); margin: 4px 8px; }

    /* ── TABLE ──────────────────────────────────── */
    .table-wrap {
      flex: 1;
      overflow: auto;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      min-width: 900px;
    }
    thead th {
      position: sticky;
      top: 0;
      background: var(--panel);
      color: var(--muted-2);
      border-bottom: 1px solid var(--line);
      text-align: left;
      font-weight: 500;
      font-size: 11px;
      letter-spacing: .06em;
      text-transform: uppercase;
      height: 36px;
      padding: 0 12px;
      white-space: nowrap;
      user-select: none;
    }
    thead th:first-child { padding-left: 16px; }
    tbody td {
      border-bottom: 1px solid var(--line-soft);
      height: 42px;
      padding: 0 12px;
      color: var(--muted-2);
      white-space: nowrap;
    }
    tbody td:first-child { padding-left: 16px; }
    tbody tr { transition: background .1s; }
    tbody tr:hover { background: var(--line-soft); }
    tbody tr:hover td { color: var(--text); }
    .col-check { width: 36px; }
    .col-icon { width: 36px; text-align: center; }
    .col-device { min-width: 240px; }
    .device-name {
      color: var(--text);
      cursor: pointer;
      font-weight: 500;
      transition: color .1s;
    }
    .device-name:hover { color: var(--accent); }
    .device-id {
      font-family: "IBM Plex Mono", monospace;
      font-size: 11px;
      color: var(--muted);
      margin-top: 1px;
    }
    .badge {
      display: inline-flex;
      align-items: center;
      height: 18px;
      padding: 0 6px;
      border-radius: 3px;
      font-size: 10px;
      font-weight: 500;
      font-family: "IBM Plex Mono", monospace;
    }
    .badge-excluded { background: var(--danger-dim); color: var(--danger); }
    .battery-bar {
      display: inline-flex;
      align-items: center;
      gap: 5px;
      font-size: 12px;
    }
    .battery-track {
      width: 28px;
      height: 8px;
      background: var(--line);
      border-radius: 2px;
      overflow: hidden;
    }
    .battery-fill {
      height: 100%;
      border-radius: 2px;
      background: var(--accent);
      transition: width .3s;
    }
    .battery-fill.low { background: var(--danger); }
    .battery-fill.mid { background: var(--warn); }
    .icon-dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: var(--muted);
      display: inline-block;
    }
    .group-row td {
      height: 28px;
      background: var(--panel);
      color: var(--muted);
      font-size: 10px;
      font-weight: 600;
      letter-spacing: .08em;
      text-transform: uppercase;
      border-top: 1px solid var(--line);
      border-bottom: 1px solid var(--line);
    }
    .empty-state {
      text-align: center;
      padding: 60px 20px;
      color: var(--muted);
    }
    .empty-state .empty-icon { font-size: 32px; margin-bottom: 12px; opacity: .4; }
    .empty-state p { font-size: 13px; }

    /* ── MODAL ──────────────────────────────────── */
    .modal-bg {
      position: fixed;
      inset: 0;
      background: rgba(0,0,0,.7);
      display: none;
      align-items: center;
      justify-content: center;
      z-index: 400;
      backdrop-filter: blur(4px);
    }
    .modal-bg.show { display: flex; }
    .modal {
      width: 420px;
      max-width: calc(100vw - 24px);
      background: var(--panel-2);
      border-radius: var(--radius-lg);
      border: 1px solid var(--line);
      box-shadow: 0 24px 64px rgba(0,0,0,.7);
      overflow: hidden;
    }
    .modal-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 16px 18px 12px;
      border-bottom: 1px solid var(--line-soft);
    }
    .modal-header h3 {
      font-size: 15px;
      font-weight: 600;
      color: var(--text);
    }
    .modal-close {
      width: 28px;
      height: 28px;
      border-radius: var(--radius-sm);
      border: 0;
      background: transparent;
      color: var(--muted-2);
      font-size: 16px;
      cursor: pointer;
      display: grid;
      place-items: center;
      transition: background .1s, color .1s;
    }
    .modal-close:hover { background: var(--line); color: var(--text); }
    .modal-body { padding: 8px 0; }
    .col-item {
      height: 44px;
      display: grid;
      grid-template-columns: 36px 1fr 44px;
      align-items: center;
      color: var(--text);
      border-bottom: 1px solid var(--line-soft);
      padding: 0 18px;
      font-size: 13px;
    }
    .col-item:last-child { border-bottom: 0; }
    .col-drag { color: var(--muted); font-size: 12px; cursor: grab; }
    .col-toggle {
      justify-self: center;
      cursor: pointer;
      width: 36px;
      height: 20px;
      background: var(--line);
      border-radius: 10px;
      position: relative;
      transition: background .2s;
    }
    .col-toggle.on { background: var(--accent); }
    .col-toggle::after {
      content: "";
      position: absolute;
      top: 2px;
      left: 2px;
      width: 16px;
      height: 16px;
      border-radius: 50%;
      background: #fff;
      transition: transform .2s;
    }
    .col-toggle.on::after { transform: translateX(16px); }
    .modal-footer {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 12px 18px;
      border-top: 1px solid var(--line-soft);
    }
    .btn-ghost {
      border: 0;
      background: transparent;
      color: var(--accent);
      font-size: 12px;
      font-weight: 500;
      font-family: inherit;
      cursor: pointer;
      padding: 0;
    }
    .btn-ghost:hover { text-decoration: underline; }
    .btn-primary {
      height: 34px;
      border-radius: var(--radius-sm);
      border: 0;
      background: var(--accent);
      color: #fff;
      padding: 0 16px;
      font-size: 13px;
      font-weight: 600;
      font-family: inherit;
      cursor: pointer;
      transition: opacity .15s;
    }
    .btn-primary:hover { opacity: .88; }

    /* ── DRAWER ─────────────────────────────────── */
    .drawer {
      position: fixed;
      top: 52px;
      right: 0;
      width: 360px;
      max-width: 92vw;
      bottom: 0;
      background: var(--panel-2);
      border-left: 1px solid var(--line);
      transform: translateX(100%);
      transition: transform .24s cubic-bezier(.4,0,.2,1);
      z-index: 300;
      display: flex;
      flex-direction: column;
      overflow: hidden;
    }
    .drawer.show { transform: translateX(0); }
    .drawer-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 14px 16px;
      border-bottom: 1px solid var(--line);
      flex-shrink: 0;
    }
    .drawer-title {
      font-size: 14px;
      font-weight: 600;
      color: var(--text);
    }
    .drawer-close {
      width: 28px;
      height: 28px;
      border-radius: var(--radius-sm);
      border: 0;
      background: transparent;
      color: var(--muted-2);
      font-size: 15px;
      cursor: pointer;
      display: grid;
      place-items: center;
      transition: background .1s, color .1s;
    }
    .drawer-close:hover { background: var(--line); color: var(--text); }
    .drawer-body { flex: 1; overflow-y: auto; padding: 16px; }
    .drawer-entity-id {
      font-family: "IBM Plex Mono", monospace;
      font-size: 11px;
      color: var(--accent);
      background: var(--accent-dim);
      border-radius: var(--radius-sm);
      padding: 6px 10px;
      margin-bottom: 16px;
      word-break: break-all;
    }
    .drawer-field { margin-bottom: 12px; }
    .drawer-field-label {
      font-size: 10px;
      font-weight: 600;
      letter-spacing: .08em;
      text-transform: uppercase;
      color: var(--muted);
      margin-bottom: 3px;
    }
    .drawer-field-value {
      font-size: 13px;
      color: var(--text);
    }
    .drawer-stats {
      background: var(--panel);
      border: 1px solid var(--line-soft);
      border-radius: var(--radius);
      overflow: hidden;
      margin: 16px 0;
    }
    .drawer-stat-row {
      display: grid;
      grid-template-columns: 1fr 1fr 1fr;
      border-bottom: 1px solid var(--line-soft);
    }
    .drawer-stat-row:last-child { border-bottom: 0; }
    .drawer-stat {
      padding: 10px 12px;
      border-right: 1px solid var(--line-soft);
    }
    .drawer-stat:last-child { border-right: 0; }
    .drawer-stat-label { font-size: 10px; color: var(--muted); margin-bottom: 3px; }
    .drawer-stat-num {
      font-size: 18px;
      font-weight: 600;
      color: var(--text);
      font-family: "IBM Plex Mono", monospace;
    }
    .drawer-stat-sub { font-size: 10px; color: var(--muted-2); margin-top: 1px; }
    .drawer-actions {
      display: flex;
      flex-direction: column;
      gap: 8px;
      margin-top: 8px;
    }
    .drawer-btn {
      height: 36px;
      border-radius: var(--radius-sm);
      border: 1px solid var(--line);
      background: var(--panel);
      color: var(--text);
      padding: 0 14px;
      font-size: 13px;
      font-family: inherit;
      cursor: pointer;
      text-align: left;
      transition: border-color .15s, background .15s;
    }
    .drawer-btn:hover { border-color: var(--accent); background: var(--accent-dim); }
    .drawer-btn.exclude-btn { color: var(--danger); border-color: var(--danger-dim); }
    .drawer-btn.exclude-btn:hover { background: var(--danger-dim); border-color: var(--danger); }

    /* ── STATUS BAR ─────────────────────────────── */
    .statusbar {
      height: 26px;
      background: var(--panel);
      border-top: 1px solid var(--line);
      display: flex;
      align-items: center;
      gap: 16px;
      padding: 0 14px;
      flex-shrink: 0;
    }
    .status-chip {
      display: flex;
      align-items: center;
      gap: 5px;
      font-size: 11px;
      color: var(--muted-2);
      font-family: "IBM Plex Mono", monospace;
    }
    .status-dot {
      width: 6px;
      height: 6px;
      border-radius: 50%;
      background: var(--muted);
    }
    .status-dot.ok { background: var(--accent); }
    .status-dot.err { background: var(--danger); }

    /* ── SCROLLBAR ──────────────────────────────── */
    ::-webkit-scrollbar { width: 6px; height: 6px; }
    ::-webkit-scrollbar-track { background: transparent; }
    ::-webkit-scrollbar-thumb { background: var(--line); border-radius: 3px; }
    ::-webkit-scrollbar-thumb:hover { background: var(--muted); }

    /* ── RESPONSIVE ─────────────────────────────── */
    @media (max-width: 860px) {
      .shell { grid-template-columns: 1fr; }
      .sidebar {
        position: fixed;
        z-index: 50;
        top: 52px;
        bottom: 0;
        width: 220px;
        transform: translateX(-100%);
        transition: transform .22s cubic-bezier(.4,0,.2,1);
        box-shadow: 4px 0 24px rgba(0,0,0,.5);
      }
      .sidebar.open { transform: translateX(0); }
      .menu-btn.hide-sm { display: none; }
    }
  </style>
</head>
<body>
  <header class="topbar">
    <div class="topbar-back" title="Volver">&#8592;</div>
    <div class="topbar-title">
      <div class="topbar-logo">
        <svg viewBox="0 0 16 16" xmlns="http://www.w3.org/2000/svg">
          <circle cx="8" cy="8" r="3.5"/>
          <path d="M8 1v2M8 13v2M1 8h2M13 8h2M3.05 3.05l1.41 1.41M11.54 11.54l1.41 1.41M3.05 12.95l1.41-1.41M11.54 4.46l1.41-1.41" stroke="#fff" stroke-width="1.4" fill="none" stroke-linecap="round"/>
        </svg>
      </div>
      <span class="topbar-name">Recorder Control</span>
      <span class="topbar-badge" id="dbBadge">—</span>
    </div>
    <div class="topbar-menu-btn">&#8942;</div>
  </header>

  <div class="shell">
    <aside id="sidebar" class="sidebar">
      <div class="sidebar-header">
        <div class="sidebar-label">Filtros</div>
        <button id="mobileFiltersBtn" class="filters-btn">
          <span>&#9776;</span> Cerrar panel
        </button>
      </div>
      <div class="sidebar-content">
        <div class="section" data-section="areas">
          <button class="section-head" onclick="toggleSection('areas')">
            Áreas <span class="section-count" id="countAreas"></span>
            <span class="chevron">&#9660;</span>
          </button>
          <div class="section-items" id="itemsAreas"></div>
        </div>
        <div class="section" data-section="integrations">
          <button class="section-head" onclick="toggleSection('integrations')">
            Integraciones <span class="section-count" id="countIntegrations"></span>
            <span class="chevron">&#9660;</span>
          </button>
          <div class="section-items" id="itemsIntegrations"></div>
        </div>
        <div class="section" data-section="states">
          <button class="section-head" onclick="toggleSection('states')">
            Estado <span class="section-count" id="countStates"></span>
            <span class="chevron">&#9660;</span>
          </button>
          <div class="section-items" id="itemsStates"></div>
        </div>
        <div class="section" data-section="tags">
          <button class="section-head" onclick="toggleSection('tags')">
            Etiquetas <span class="section-count" id="countTags"></span>
            <span class="chevron">&#9660;</span>
          </button>
          <div class="section-items" id="itemsTags"></div>
        </div>
      </div>
    </aside>

    <main class="main">
      <div class="toolbar">
        <button class="icon-btn" onclick="toggleSidebar()" title="Filtros">&#9776;</button>
        <div class="search-wrap">
          <span class="search-icon">&#128269;</span>
          <input id="search" class="search" placeholder="Buscar entidades, dispositivos, áreas…">
        </div>
        <div class="toolbar-sep"></div>
        <button id="groupBtn" class="menu-btn hide-sm" onclick="toggleMenu('groupMenu')">Agrupar <span>&#9662;</span></button>
        <button id="sortBtn" class="menu-btn hide-sm" onclick="toggleMenu('sortMenu')">Ordenar <span>&#9662;</span></button>
        <div class="toolbar-sep"></div>
        <button class="icon-btn" onclick="openCustomize()" title="Columnas">&#9638;</button>
        <button class="icon-btn" onclick="toggleQuickMenu()" title="Acciones">&#8942;</button>
        <div id="groupMenu" class="menu"></div>
        <div id="sortMenu" class="menu"></div>
        <div id="quickMenu" class="quick">
          <div class="quick-section-label">Recorder</div>
          <button onclick="setRecorder(true)">&#9654; Activar recorder</button>
          <button onclick="setRecorder(false)">&#9632; Desactivar recorder</button>
          <div class="quick-sep"></div>
          <div class="quick-section-label">Purge</div>
          <button onclick="purgeGlobal()" class="danger">&#128465; Purge global</button>
          <button onclick="purgeSelected()" class="danger">&#128465; Purge seleccionadas</button>
          <div class="quick-sep"></div>
          <button onclick="applyChanges()" class="danger">&#9888; Aplicar filtros (reiniciar HA)</button>
        </div>
      </div>

      <div class="table-wrap">
        <table>
          <thead>
            <tr id="tableHead"></tr>
          </thead>
          <tbody id="tbody">
            <tr><td colspan="12" style="text-align:center;padding:40px;color:var(--muted)">Cargando…</td></tr>
          </tbody>
        </table>
      </div>

      <div class="statusbar">
        <div class="status-chip">
          <span class="status-dot" id="statusDot"></span>
          <span id="statusText">—</span>
        </div>
        <div class="status-chip" id="rowCountChip"></div>
      </div>
    </main>
  </div>

  <!-- Customize columns modal -->
  <div id="customizeModalBg" class="modal-bg">
    <div class="modal">
      <div class="modal-header">
        <h3>Personalizar columnas</h3>
        <button class="modal-close" onclick="closeCustomize()">&#10005;</button>
      </div>
      <div class="modal-body">
        <div id="columnsList"></div>
      </div>
      <div class="modal-footer">
        <button class="btn-ghost" onclick="restoreColumns()">Restaurar valores predeterminados</button>
        <button class="btn-primary" onclick="closeCustomize()">Hecho</button>
      </div>
    </div>
  </div>

  <!-- Entity detail drawer -->
  <aside id="entityDrawer" class="drawer">
    <div class="drawer-header">
      <span class="drawer-title">Detalle de entidad</span>
      <button class="drawer-close" onclick="closeDrawer()">&#10005;</button>
    </div>
    <div class="drawer-body">
      <div id="dEntityId" class="drawer-entity-id"></div>

      <div class="drawer-field">
        <div class="drawer-field-label">Nombre</div>
        <div id="dName" class="drawer-field-value"></div>
      </div>
      <div class="drawer-field">
        <div class="drawer-field-label">Estado actual</div>
        <div id="dState" class="drawer-field-value"></div>
      </div>
      <div class="drawer-field">
        <div class="drawer-field-label">Último cambio</div>
        <div id="dLastChanged" class="drawer-field-value"></div>
      </div>
      <div class="drawer-field">
        <div class="drawer-field-label">Última actualización</div>
        <div id="dLastUpdated" class="drawer-field-value"></div>
      </div>

      <div class="drawer-stats">
        <div class="drawer-stat-row">
          <div class="drawer-stat">
            <div class="drawer-stat-label">1 hora</div>
            <div id="d1hNum" class="drawer-stat-num">—</div>
            <div id="d1hRate" class="drawer-stat-sub"></div>
          </div>
          <div class="drawer-stat">
            <div class="drawer-stat-label">24 horas</div>
            <div id="d24hNum" class="drawer-stat-num">—</div>
            <div id="d24hRate" class="drawer-stat-sub"></div>
          </div>
          <div class="drawer-stat">
            <div class="drawer-stat-label">7 días</div>
            <div id="d7dNum" class="drawer-stat-num">—</div>
            <div id="d7dRate" class="drawer-stat-sub"></div>
          </div>
        </div>
      </div>

      <div class="drawer-actions">
        <button class="drawer-btn" id="dOpenHA">&#8599; Abrir en Home Assistant</button>
        <button class="drawer-btn exclude-btn" id="dToggleExclusion">Toggle recorder</button>
      </div>
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
      $("sortBtn").innerHTML = `Ordenar: ${sortLabel} <span>&#9662;</span>`;
      const groupLabel = groupOptions.find(([k]) => k === state.groupBy)?.[1] || "No agrupar";
      $("groupBtn").innerHTML = `Agrupar: ${groupLabel} <span>&#9662;</span>`;
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
      return `<span class="icon-dot" title="${esc(item.icon || item.domain || "")}"></span>`;
    }

    function batteryCell(item) {
      if (item.battery === null || item.battery === undefined || Number.isNaN(Number(item.battery))) return "—";
      const pct = Math.round(Number(item.battery));
      const cls = pct < 20 ? "low" : pct < 50 ? "mid" : "";
      return `<div class="battery-bar"><div class="battery-track"><div class="battery-fill ${cls}" style="width:${pct}%"></div></div>${pct}%</div>`;
    }

    function renderRow(item) {
      const cols = state.visibleColumns;
      const deviceLabel = item.friendly_name || item.entity_id || "sin nombre";
      const cells = [];
      if (cols.includes("icon")) cells.push(`<td class="col-icon">${iconCell(item)}</td>`);
      if (cols.includes("device")) cells.push(`<td class="col-device">
        <div class="device-name" onclick="openDetail('${esc(item.entity_id)}')">${esc(deviceLabel)}${item.excluded_by_app ? ' <span class="badge badge-excluded">excluido</span>' : ''}</div>
        <div class="device-id">${esc(item.entity_id)}</div>
      </td>`);
      if (cols.includes("area")) cells.push(`<td>${esc(item.area || "—")}</td>`);
      if (cols.includes("integration")) cells.push(`<td>${esc(item.integration || "—")}</td>`);
      if (cols.includes("manufacturer")) cells.push(`<td>${esc(item.manufacturer || "—")}</td>`);
      if (cols.includes("model")) cells.push(`<td>${esc(item.model || "—")}</td>`);
      if (cols.includes("battery")) cells.push(`<td>${batteryCell(item)}</td>`);
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
          html += `<tr class="group-row"><td colspan="${colCount}" style="padding-left:16px">${esc(grp)}</td></tr>`;
          prevGroup = grp;
        }
        html += renderRow(item);
      }
      if (!html) {
        const colCount = 1 + state.visibleColumns.length;
        html = `<tr><td colspan="${colCount}"><div class="empty-state"><div class="empty-icon">&#128269;</div><p>Sin resultados</p></div></td></tr>`;
      }
      $("tbody").innerHTML = html;
      const allVisibleIds = state.rows.map((r) => r.entity_id);
      const allSelected = allVisibleIds.length > 0 && allVisibleIds.every((id) => state.selected.has(id));
      const master = $("masterCheck");
      if (master) master.checked = allSelected;
      const chip = $("rowCountChip");
      if (chip) chip.textContent = `${state.rows.length} / ${state.all.length} entidades`;
    }

    function updateCatalog() {
      const uniq = (arr) => [...new Set(arr.map((v) => String(v || "").trim()).filter(Boolean))].sort((a,b) => a.localeCompare(b));
      state.catalog.areas = uniq(state.all.map((i) => i.area));
      state.catalog.integrations = uniq(state.all.map((i) => i.integration));
      state.catalog.states = uniq(state.all.map((i) => i.state));
      state.catalog.tags = uniq(state.all.flatMap((i) => Array.isArray(i.tags) ? i.tags : []));
    }

    function renderSectionItems(name, list) {
      const idMap = { areas: "itemsAreas", integrations: "itemsIntegrations", states: "itemsStates", tags: "itemsTags" };
      const countMap = { areas: "countAreas", integrations: "countIntegrations", states: "countStates", tags: "countTags" };
      const container = $(idMap[name]);
      const selectedSet = state.filters[name];
      container.innerHTML = list.map((value) => `
        <label class="section-item">
          <input type="checkbox" ${selectedSet.has(value) ? "checked" : ""} onchange="toggleFilterValue('${name}', '${esc(value)}', this.checked)">
          ${esc(value)}
        </label>
      `).join("") || `<div style="padding:6px 4px;color:var(--muted);font-size:12px">Sin valores</div>`;
      const countEl = $(countMap[name]);
      if (selectedSet.size) {
        countEl.textContent = selectedSet.size;
        countEl.classList.add("visible");
      } else {
        countEl.textContent = "";
        countEl.classList.remove("visible");
      }
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
      $("d1hNum").textContent = w["1"]?.state_changes ?? "—";
      $("d1hRate").textContent = w["1"] ? `${(w["1"].changes_per_hour || 0).toFixed(2)}/h` : "";
      $("d24hNum").textContent = w["24"]?.state_changes ?? "—";
      $("d24hRate").textContent = w["24"] ? `${(w["24"].changes_per_hour || 0).toFixed(2)}/h` : "";
      $("d7dNum").textContent = w["168"]?.state_changes ?? "—";
      $("d7dRate").textContent = w["168"] ? `${(w["168"].changes_per_hour || 0).toFixed(2)}/h` : "";
      $("dLastChanged").textContent = data.last_changed || "—";
      $("dLastUpdated").textContent = data.last_updated || "—";
      const row = state.all.find((r) => r.entity_id === data.entity_id);
      const excluded = Boolean(row?.excluded_by_app);
      const toggleBtn = $("dToggleExclusion");
      toggleBtn.textContent = excluded ? "✓ Incluir en recorder" : "✕ Excluir del recorder";
      toggleBtn.className = "drawer-btn" + (excluded ? "" : " exclude-btn");
      toggleBtn.onclick = async () => {
        await toggleEntity(data.entity_id, !excluded);
        closeDrawer();
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
            <span class="col-drag">&#8942;&#8942;</span>
            <span>${col.label}</span>
            <span class="col-toggle ${visible ? "on" : ""}" onclick="toggleColumn('${col.key}')"></span>
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
        $("tbody").innerHTML = `<tr><td colspan="12" style="text-align:center;padding:40px;color:var(--danger)">Error cargando datos: ${esc(err.message)}</td></tr>`;
      }
    }

    async function refreshStatusChips() {
      try {
        const status = await api("./api/status");
        const mode = status.metrics_mode || "none";
        const txt = mode === "sqlite" ? "SQLite" : mode === "mariadb" ? "MariaDB" : "Sin fuente DB";
        const recording = Boolean(status.recorder_recording);
        document.title = `Recorder Control · ${txt}`;
        $("dbBadge").textContent = txt;
        $("statusDot").className = "status-dot " + (recording ? "ok" : "err");
        $("statusText").textContent = recording ? "Grabando" : "Detenido";
      } catch (_) {
        $("statusDot").className = "status-dot err";
        $("statusText").textContent = "Sin conexión";
      }
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
      if (!t.closest(".icon-btn") && !t.closest(".quick")) {
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
