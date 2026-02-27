import json
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
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

app = FastAPI(title="Recorder Visual Control", version="0.3.0")


class TogglePayload(BaseModel):
    exclude: bool = Field(..., description="True to exclude from recorder")


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


async def _restart_core() -> None:
    url = f"{CORE_HTTP_BASE}/api/services/homeassistant/restart"
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        response = await client.post(url, headers=_auth_headers(), json={})
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail=response.text)


def _detect_sqlite_db_path() -> Path | None:
    default_db = HA_CONFIG_DIR / "home-assistant_v2.db"
    if default_db.exists():
        return default_db
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


async def _query_activity_metrics_logbook(hours: int = 24, limit: int = 1200) -> dict[str, Any]:
    now_utc = datetime.now(timezone.utc)
    start_utc = now_utc - timedelta(hours=max(1, hours))
    start_iso = start_utc.isoformat()
    end_iso = now_utc.isoformat()
    url = f"{CORE_HTTP_BASE}/api/logbook/{start_iso}"

    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            response = await client.get(
                url,
                headers=_auth_headers(),
                params={"end_time": end_iso},
            )
        if response.status_code >= 400:
            raise RuntimeError(f"HTTP {response.status_code}: {response.text}")
        entries = response.json()
        if not isinstance(entries, list):
            raise RuntimeError("Respuesta no valida de /api/logbook")
    except Exception as err:
        return {
            "available": False,
            "reason": f"No se pudieron calcular metricas por logbook: {err}",
            "window_hours": hours,
            "items": [],
            "by_entity_id": {},
            "source": "logbook_api",
        }

    counts: dict[str, int] = {}
    last_seen: dict[str, float] = {}

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        entity_id = entry.get("entity_id") or entry.get("context_entity_id")
        if not entity_id or not isinstance(entity_id, str) or "." not in entity_id:
            continue
        entity_id = entity_id.strip().lower()
        counts[entity_id] = counts.get(entity_id, 0) + 1

        when = entry.get("when")
        ts = 0.0
        if isinstance(when, (int, float)):
            ts = float(when)
        elif isinstance(when, str):
            try:
                ts = datetime.fromisoformat(when.replace("Z", "+00:00")).timestamp()
            except ValueError:
                ts = 0.0
        if ts > last_seen.get(entity_id, 0.0):
            last_seen[entity_id] = ts

    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[: max(1, limit)]
    items: list[dict[str, Any]] = []
    for entity_id, count in ranked:
        items.append(
            {
                "entity_id": entity_id,
                "state_changes": count,
                "changes_per_hour": round(count / max(1, hours), 2),
                "last_updated_ts": float(last_seen.get(entity_id, 0.0)),
                "first_updated_ts": 0.0,
            }
        )

    return {
        "available": True,
        "reason": "OK",
        "window_hours": hours,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "items": items,
        "by_entity_id": {item["entity_id"]: item for item in items},
        "source": "logbook_api",
    }


async def _query_activity_metrics(hours: int = 24, limit: int = 1200) -> dict[str, Any]:
    sqlite_metrics = _query_activity_metrics_sqlite(hours=hours, limit=limit)
    if sqlite_metrics.get("available"):
        return sqlite_metrics
    return await _query_activity_metrics_logbook(hours=hours, limit=limit)


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
    return {
        "recorder_recording": bool(recorder.get("recording", False)),
        "recorder": recorder,
        "setup": _setup_status(),
        "excluded_count": len(_load_excluded_entities()),
        "metrics_mode": "sqlite" if db_path else "logbook_api",
        "metrics_reason": "SQLite local detectada" if db_path else "Usando logbook API (compatible con MariaDB)",
        "metrics_db_path": str(db_path) if db_path else None,
    }


@app.get("/api/entities")
async def api_entities(
    search: str = "", limit: int = 500, hours: int = 24
) -> dict[str, Any]:
    states = await _list_states()
    excluded = _load_excluded_entities()
    metrics = await _query_activity_metrics(hours=hours, limit=4000)
    metrics_map = metrics["by_entity_id"]
    needle = search.strip().lower()
    entities: list[dict[str, Any]] = []

    for item in states:
        entity_id = str(item.get("entity_id", ""))
        if not entity_id:
            continue
        normalized_entity_id = entity_id.strip().lower()
        friendly_name = str(item.get("attributes", {}).get("friendly_name", ""))
        domain = entity_id.split(".", 1)[0] if "." in entity_id else "unknown"
        m = metrics_map.get(normalized_entity_id, {})

        if needle and needle not in normalized_entity_id and needle not in friendly_name.lower():
            continue

        entities.append(
            {
                "entity_id": normalized_entity_id,
                "friendly_name": friendly_name,
                "domain": domain,
                "excluded_by_app": normalized_entity_id in excluded,
                "state_changes": int(m.get("state_changes", 0)),
                "changes_per_hour": float(m.get("changes_per_hour", 0)),
                "last_updated_ts": float(m.get("last_updated_ts", 0)),
            }
        )

    entities.sort(
        key=lambda x: (-x["state_changes"], x["domain"], x["entity_id"])
    )
    if limit > 0:
        entities = entities[:limit]

    return {
        "items": entities,
        "total": len(entities),
        "managed_file": str(MANAGED_LIST_FILE),
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
  <title>Recorder Visual Control</title>
  <style>
    :root {
      --bg: #f2f5fa;
      --card: #ffffff;
      --ink: #1f2937;
      --muted: #5f6c7b;
      --ok: #1d8f4e;
      --warn: #b7681b;
      --danger: #b23b3b;
      --action: #1f5fcc;
      --line: #d6deea;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      padding: 18px;
      background: linear-gradient(160deg, #e9eef7, var(--bg));
      color: var(--ink);
      font-family: "Segoe UI", "Noto Sans", sans-serif;
    }
    .wrap { max-width: 980px; margin: 0 auto; display: grid; gap: 14px; }
    .card {
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 14px;
    }
    h1 { margin: 0 0 8px; font-size: 1.45rem; }
    .muted { color: var(--muted); }
    .status { display: flex; gap: 10px; align-items: center; margin-top: 10px; flex-wrap: wrap; }
    .chip {
      border-radius: 999px;
      padding: 5px 10px;
      font-size: .88rem;
      border: 1px solid var(--line);
      background: #f7f9fd;
    }
    .chip.ok { color: var(--ok); border-color: #bfe7cf; background: #edf9f1; }
    .chip.warn { color: var(--warn); border-color: #efd8ba; background: #fff7ec; }
    .controls { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 12px; }
    input[type="text"] {
      flex: 1;
      min-width: 220px;
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 10px;
      font-size: .95rem;
    }
    button {
      border: 0;
      border-radius: 10px;
      padding: 9px 12px;
      cursor: pointer;
      color: #fff;
      background: var(--action);
    }
    button.warn { background: var(--warn); }
    button.ghost { background: #6b7280; }
    .table-wrap { max-height: 60vh; overflow: auto; border: 1px solid var(--line); border-radius: 10px; margin-top: 10px; }
    table { width: 100%; border-collapse: collapse; font-size: .92rem; }
    th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #edf1f7; }
    tr:last-child td { border-bottom: 0; }
    .ex-on { color: var(--danger); font-weight: 600; }
    .ex-off { color: var(--ok); font-weight: 600; }
    .mono { font-family: ui-monospace, Menlo, Consolas, monospace; font-size: .86rem; }
    .small { font-size: .85rem; }
    pre {
      margin: 8px 0 0;
      padding: 10px;
      background: #f8fafc;
      border: 1px dashed var(--line);
      border-radius: 10px;
      overflow: auto;
    }
  </style>
</head>
<body>
  <main class="wrap">
    <section class="card">
      <h1>Recorder Visual Control (por entidad)</h1>
      <p class="muted">Selecciona qué entidades excluir del recorder. El cambio se guarda al instante y se aplica tras reiniciar Home Assistant.</p>
      <div class="status">
        <span id="setupChip" class="chip warn">Comprobando configuración...</span>
        <span id="recChip" class="chip">Comprobando recorder...</span>
        <span id="countChip" class="chip">Excluidas por app: 0</span>
        <span id="metricChip" class="chip">Métricas: comprobando...</span>
      </div>
      <div class="controls">
        <input id="search" type="text" placeholder="Buscar por entity_id o nombre..." />
        <select id="windowHours">
          <option value="1">1h</option>
          <option value="6">6h</option>
          <option value="24" selected>24h</option>
          <option value="72">72h</option>
          <option value="168">7d</option>
        </select>
        <button onclick="loadEntities()">Buscar</button>
        <button class="ghost" onclick="refreshAll()">Actualizar</button>
        <button class="warn" onclick="applyChanges()">Aplicar (reiniciar HA)</button>
      </div>
      <div id="setupHelp" class="small muted" style="margin-top:8px;"></div>
      <div id="metricsHelp" class="small muted" style="margin-top:6px;"></div>
    </section>

    <section class="card">
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Entidad</th>
              <th>Nombre</th>
              <th>Cambios</th>
              <th>Cambios/h</th>
              <th>Última escritura</th>
              <th>Estado en app</th>
              <th>Acción</th>
            </tr>
          </thead>
          <tbody id="tbody">
            <tr><td colspan="7" class="muted">Cargando...</td></tr>
          </tbody>
        </table>
      </div>
    </section>
  </main>

  <script>
    const setupChip = document.getElementById("setupChip");
    const recChip = document.getElementById("recChip");
    const countChip = document.getElementById("countChip");
    const setupHelp = document.getElementById("setupHelp");
    const metricsHelp = document.getElementById("metricsHelp");
    const tbody = document.getElementById("tbody");
    const searchInput = document.getElementById("search");
    const windowHours = document.getElementById("windowHours");
    const metricChip = document.getElementById("metricChip");

    async function api(path, opts = {}) {
      const res = await fetch(path, opts);
      if (!res.ok) throw new Error(await res.text());
      return await res.json();
    }

    function esc(value) {
      return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;");
    }

    function fmtTs(ts) {
      if (!ts || Number(ts) <= 0) return "-";
      const d = new Date(Number(ts) * 1000);
      return d.toLocaleString();
    }

    async function refreshStatus() {
      const data = await api("./api/status");
      const setup = data.setup || {};
      setupChip.className = "chip " + (setup.configured ? "ok" : "warn");
      setupChip.textContent = setup.configured
        ? "Configuración recorder detectada"
        : "Falta configurar include en configuration.yaml";

      const recording = Boolean(data.recorder_recording);
      recChip.className = "chip " + (recording ? "ok" : "warn");
      recChip.textContent = recording
        ? "Recorder activo"
        : "Recorder no está grabando";

      countChip.textContent = "Excluidas por app: " + (data.excluded_count ?? 0);
      const mode = String(data.metrics_mode || "unknown");
      const sqlite = mode === "sqlite";
      metricChip.className = "chip " + (sqlite ? "ok" : "warn");
      metricChip.textContent = sqlite
        ? "Métricas por SQLite"
        : "Métricas por Logbook API";

      if (!setup.configured) {
        setupHelp.innerHTML =
          "Añade este bloque en <span class='mono'>configuration.yaml</span> para activar el modo por entidad:<pre>"
          + esc(setup.snippet || "")
          + "</pre>";
      } else {
        setupHelp.textContent = "";
      }
    }

    async function loadEntities() {
      tbody.innerHTML = "<tr><td colspan='7' class='muted'>Cargando entidades...</td></tr>";
      const search = encodeURIComponent(searchInput.value || "");
      const hours = encodeURIComponent(windowHours.value || "24");
      const data = await api(`./api/entities?search=${search}&limit=600&hours=${hours}`);
      const items = data.items || [];
      const metrics = data.metrics || {};

      if (metrics.available) {
        if (metrics.source === "sqlite") {
          metricsHelp.textContent =
            `Ventana: ${metrics.window_hours}h | Fuente: SQLite | DB: ${metrics.db_path} (${metrics.db_size_mb} MB)`;
        } else {
          metricsHelp.textContent =
            `Ventana: ${metrics.window_hours}h | Fuente: Logbook API (compatible con MariaDB)`;
        }
      } else {
        metricsHelp.textContent = "Métricas no disponibles: " + (metrics.reason || "desconocido");
      }

      if (items.length === 0) {
        tbody.innerHTML = "<tr><td colspan='7' class='muted'>Sin resultados.</td></tr>";
        return;
      }

      tbody.innerHTML = items.map(item => {
        const excluded = Boolean(item.excluded_by_app);
        return `
          <tr>
            <td class="mono">${esc(item.entity_id)}</td>
            <td>${esc(item.friendly_name || "-")}</td>
            <td>${Number(item.state_changes || 0)}</td>
            <td>${Number(item.changes_per_hour || 0).toFixed(2)}</td>
            <td>${esc(fmtTs(item.last_updated_ts))}</td>
            <td class="${excluded ? "ex-on" : "ex-off"}">${excluded ? "Excluida" : "Incluida"}</td>
            <td>
              <button onclick="toggleEntity('${esc(item.entity_id)}', ${excluded ? "false" : "true"})">
                ${excluded ? "Incluir" : "Excluir"}
              </button>
            </td>
          </tr>
        `;
      }).join("");
    }

    async function toggleEntity(entityId, exclude) {
      await api("./api/entities/" + encodeURIComponent(entityId), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ exclude })
      });
      await refreshAll();
    }

    async function applyChanges() {
      const ok = confirm("Esto reiniciará Home Assistant Core para aplicar cambios de recorder. ¿Continuar?");
      if (!ok) return;
      await api("./api/apply", { method: "POST" });
      alert("Reinicio solicitado. Espera a que Home Assistant vuelva a estar disponible.");
    }

    async function refreshAll() {
      try {
        await refreshStatus();
        await loadEntities();
      } catch (err) {
        tbody.innerHTML = `<tr><td colspan="7" class="muted">Error: ${esc(err.message)}</td></tr>`;
      }
    }

    searchInput.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") loadEntities();
    });
    windowHours.addEventListener("change", () => loadEntities());

    refreshAll();
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
