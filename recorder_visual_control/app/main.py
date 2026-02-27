import json
import os
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

app = FastAPI(title="Recorder Visual Control", version="0.2.0")


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


@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True}


@app.get("/api/setup")
def api_setup() -> dict[str, Any]:
    return _setup_status()


@app.get("/api/status")
async def api_status() -> dict[str, Any]:
    recorder = await _recorder_info()
    return {
        "recorder_recording": bool(recorder.get("recording", False)),
        "recorder": recorder,
        "setup": _setup_status(),
        "excluded_count": len(_load_excluded_entities()),
    }


@app.get("/api/entities")
async def api_entities(search: str = "", limit: int = 500) -> dict[str, Any]:
    states = await _list_states()
    excluded = _load_excluded_entities()
    needle = search.strip().lower()
    entities: list[dict[str, Any]] = []

    for item in states:
        entity_id = str(item.get("entity_id", ""))
        if not entity_id:
            continue
        friendly_name = str(item.get("attributes", {}).get("friendly_name", ""))
        domain = entity_id.split(".", 1)[0] if "." in entity_id else "unknown"

        if needle and needle not in entity_id.lower() and needle not in friendly_name.lower():
            continue

        entities.append(
            {
                "entity_id": entity_id,
                "friendly_name": friendly_name,
                "domain": domain,
                "excluded_by_app": entity_id in excluded,
            }
        )

    entities.sort(key=lambda x: (x["domain"], x["entity_id"]))
    if limit > 0:
        entities = entities[:limit]

    return {"items": entities, "total": len(entities), "managed_file": str(MANAGED_LIST_FILE)}


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
      </div>
      <div class="controls">
        <input id="search" type="text" placeholder="Buscar por entity_id o nombre..." />
        <button onclick="loadEntities()">Buscar</button>
        <button class="ghost" onclick="refreshAll()">Actualizar</button>
        <button class="warn" onclick="applyChanges()">Aplicar (reiniciar HA)</button>
      </div>
      <div id="setupHelp" class="small muted" style="margin-top:8px;"></div>
    </section>

    <section class="card">
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Entidad</th>
              <th>Nombre</th>
              <th>Estado en app</th>
              <th>Acción</th>
            </tr>
          </thead>
          <tbody id="tbody">
            <tr><td colspan="4" class="muted">Cargando...</td></tr>
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
    const tbody = document.getElementById("tbody");
    const searchInput = document.getElementById("search");

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
      tbody.innerHTML = "<tr><td colspan='4' class='muted'>Cargando entidades...</td></tr>";
      const search = encodeURIComponent(searchInput.value || "");
      const data = await api(`./api/entities?search=${search}&limit=600`);
      const items = data.items || [];

      if (items.length === 0) {
        tbody.innerHTML = "<tr><td colspan='4' class='muted'>Sin resultados.</td></tr>";
        return;
      }

      tbody.innerHTML = items.map(item => {
        const excluded = Boolean(item.excluded_by_app);
        return `
          <tr>
            <td class="mono">${esc(item.entity_id)}</td>
            <td>${esc(item.friendly_name || "-")}</td>
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
        tbody.innerHTML = `<tr><td colspan="4" class="muted">Error: ${esc(err.message)}</td></tr>`;
      }
    }

    searchInput.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") loadEntities();
    });

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
