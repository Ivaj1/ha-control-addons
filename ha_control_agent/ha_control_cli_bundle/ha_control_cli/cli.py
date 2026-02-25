"""ha-control command line interface."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time
from typing import Any
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "ha-control" / "config.json"
RETRYABLE_HTTP = {502, 503, 504}


class HAControlError(RuntimeError):
    """CLI-level operational error."""


class APIClient:
    """HTTP client for HA Control Agent with basic retries on idempotent calls."""

    def __init__(self, *, agent: str, token: str | None = None) -> None:
        self.agent = agent.rstrip("/")
        self.token = token

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | list[Any] | str | None = None,
        query: dict[str, str] | None = None,
        auth: bool = True,
        retries: int = 3,
    ) -> Any:
        url = f"{self.agent}/{path.lstrip('/')}"
        if query:
            url = f"{url}?{urlparse.urlencode(query, doseq=True)}"

        headers = {"Accept": "application/json"}

        body_bytes: bytes | None = None
        if payload is not None:
            if isinstance(payload, (dict, list)):
                headers["Content-Type"] = "application/json"
                body_bytes = json.dumps(payload).encode("utf-8")
            elif isinstance(payload, str):
                headers["Content-Type"] = "text/plain"
                body_bytes = payload.encode("utf-8")
            else:
                raise HAControlError("Unsupported payload type")

        if auth:
            if not self.token:
                raise HAControlError("No session token available. Run 'auth login'.")
            headers["Authorization"] = f"Bearer {self.token}"

        method_upper = method.upper()
        retryable_method = method_upper in {"GET", "HEAD", "OPTIONS", "PUT", "DELETE"}

        last_error: Exception | None = None
        attempts = max(1, retries)

        for attempt in range(1, attempts + 1):
            req = urlrequest.Request(url, data=body_bytes, method=method_upper, headers=headers)
            try:
                with urlrequest.urlopen(req, timeout=120) as resp:  # noqa: S310
                    raw = resp.read()
                    content_type = resp.headers.get("Content-Type", "")
                    return _decode_response(raw, content_type)
            except urlerror.HTTPError as err:
                raw = err.read()
                content_type = err.headers.get("Content-Type", "") if err.headers else ""
                decoded = _decode_response(raw, content_type)

                error_message = _format_error_payload(decoded)

                if retryable_method and err.code in RETRYABLE_HTTP and attempt < attempts:
                    time.sleep(0.2 * attempt)
                    continue

                if err.code == 409:
                    raise HAControlError(f"Conflict (409): {error_message}") from err
                if err.code == 401:
                    raise HAControlError(
                        "Unauthorized (401). Re-run 'ha-control auth login' to refresh session."
                    ) from err
                if err.code == 403:
                    raise HAControlError(f"Forbidden (403): {error_message}") from err
                if err.code == 404:
                    raise HAControlError(f"Not found (404): {error_message}") from err
                raise HAControlError(f"HTTP {err.code}: {error_message}") from err
            except urlerror.URLError as err:
                last_error = err
                if retryable_method and attempt < attempts:
                    time.sleep(0.2 * attempt)
                    continue
                raise HAControlError(f"Network error: {err.reason}") from err

        if last_error:
            raise HAControlError(str(last_error))
        raise HAControlError("Request failed")


def _decode_response(raw: bytes, content_type: str) -> Any:
    if "application/json" in content_type:
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return raw.decode("utf-8", errors="replace")
    return raw.decode("utf-8", errors="replace")


def _format_error_payload(decoded: Any) -> str:
    if isinstance(decoded, dict):
        detail = decoded.get("detail")
        if isinstance(detail, str):
            return detail
        if detail is not None:
            return json.dumps(detail, ensure_ascii=True)

        message = decoded.get("message")
        if isinstance(message, str):
            return message

        return json.dumps(decoded, ensure_ascii=True)

    if isinstance(decoded, list):
        return json.dumps(decoded, ensure_ascii=True)

    return str(decoded)


def _yaml_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if text == "" or any(ch in text for ch in [":", "#", "\n", "\t", "-"]):
        return json.dumps(text)
    return text


def _to_yaml(value: Any, indent: int = 0) -> str:
    prefix = " " * indent
    if isinstance(value, dict):
        lines: list[str] = []
        for key, val in value.items():
            if isinstance(val, (dict, list)):
                lines.append(f"{prefix}{key}:")
                lines.append(_to_yaml(val, indent + 2))
            else:
                lines.append(f"{prefix}{key}: {_yaml_scalar(val)}")
        return "\n".join(lines)

    if isinstance(value, list):
        lines = []
        for item in value:
            if isinstance(item, (dict, list)):
                lines.append(f"{prefix}-")
                lines.append(_to_yaml(item, indent + 2))
            else:
                lines.append(f"{prefix}- {_yaml_scalar(item)}")
        return "\n".join(lines)

    return f"{prefix}{_yaml_scalar(value)}"


def _print_table(data: Any) -> None:
    if isinstance(data, dict):
        rows = [("key", "value")]
        rows.extend((str(k), json.dumps(v, ensure_ascii=True)) for k, v in data.items())
    elif isinstance(data, list):
        if data and all(isinstance(item, dict) for item in data):
            keys: list[str] = []
            seen: set[str] = set()
            for item in data:
                for key in item.keys():
                    if key not in seen:
                        seen.add(key)
                        keys.append(key)
            rows = [tuple(keys)]
            for item in data:
                rows.append(tuple(json.dumps(item.get(key, ""), ensure_ascii=True) for key in keys))
        else:
            rows = [("index", "value")]
            rows.extend((str(i), json.dumps(v, ensure_ascii=True)) for i, v in enumerate(data))
    else:
        rows = [("value",), (str(data),)]

    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    for idx, row in enumerate(rows):
        rendered = " | ".join(col.ljust(widths[i]) for i, col in enumerate(row))
        print(rendered)
        if idx == 0:
            print("-+-".join("-" * w for w in widths))


def _print_output(data: Any, output: str) -> None:
    if output == "raw":
        if isinstance(data, str):
            print(data)
        else:
            print(json.dumps(data, ensure_ascii=True))
        return

    if output == "json":
        print(json.dumps(data, indent=2, ensure_ascii=True))
        return

    if output == "yaml":
        print(_to_yaml(data))
        return

    if output == "table":
        _print_table(data)
        return

    raise HAControlError(f"Unsupported output format: {output}")


def _load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_config(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def _resolve_connection(
    args: argparse.Namespace,
    *,
    allow_missing_token: bool = False,
) -> tuple[str, str | None, Path, dict[str, Any]]:
    config_path = Path(args.config)
    config = _load_config(config_path)

    agent = args.agent or config.get("agent")
    token = args.token or config.get("token")

    if not agent:
        raise HAControlError("Agent URL missing. Pass --agent or run 'auth login'.")

    if not allow_missing_token and not token:
        raise HAControlError("Session token missing. Run 'auth login'.")

    return agent, token, config_path, config


def _client_from_args(args: argparse.Namespace, *, allow_missing_token: bool = False) -> APIClient:
    agent, token, _, _ = _resolve_connection(args, allow_missing_token=allow_missing_token)
    return APIClient(agent=agent, token=token)


def _parse_json_value(raw: str | None, file_path: str | None) -> Any:
    if raw and file_path:
        raise HAControlError("Use either --json or --file, not both")

    if file_path:
        text = Path(file_path).read_text(encoding="utf-8")
        return json.loads(text)

    if raw:
        return json.loads(raw)

    return None


def _dry_run_result(method: str, path: str, payload: Any, query: dict[str, str] | None = None) -> dict[str, Any]:
    return {
        "dry_run": True,
        "method": method.upper(),
        "path": path,
        "query": query or {},
        "payload": payload,
    }


def _require_confirm(args: argparse.Namespace, *, message: str) -> None:
    if args.yes or args.confirm:
        return
    raise HAControlError(f"{message} requires --confirm or --yes")


def _supervisor_request(
    args: argparse.Namespace,
    method: str,
    path: str,
    *,
    body: Any = None,
    query: dict[str, str] | None = None,
    destructive: bool = False,
    dry_run_supported: bool = True,
) -> Any:
    if destructive:
        _require_confirm(args, message=f"Destructive operation ({method} /{path})")

    full_path = f"/v1/supervisor/{path.lstrip('/')}" if path else "/v1/supervisor"
    if args.dry_run and dry_run_supported:
        return _dry_run_result(method, full_path, body, query=query)

    client = _client_from_args(args)
    return client.request(method, full_path, payload=body, query=query)


def _core_rest_request(
    args: argparse.Namespace,
    method: str,
    path: str,
    *,
    body: Any = None,
    query: dict[str, str] | None = None,
    destructive: bool = False,
    dry_run_supported: bool = True,
) -> Any:
    if destructive:
        _require_confirm(args, message=f"Destructive operation ({method} /{path})")

    full_path = f"/v1/core/rest/{path.lstrip('/')}" if path else "/v1/core/rest"
    if args.dry_run and dry_run_supported:
        return _dry_run_result(method, full_path, body, query=query)

    client = _client_from_args(args)
    return client.request(method, full_path, payload=body, query=query)


def _core_ws(
    args: argparse.Namespace,
    *,
    ws_type: str,
    payload: dict[str, Any] | None = None,
    timeout_s: int = 30,
    message_id: int | None = None,
    destructive: bool = False,
    dry_run_supported: bool = True,
) -> Any:
    if destructive:
        _require_confirm(args, message=f"Destructive WebSocket operation ({ws_type})")

    body: dict[str, Any] = {
        "type": ws_type,
        "payload": payload or {},
        "timeout_s": timeout_s,
    }
    if message_id is not None:
        body["id"] = message_id

    if args.dry_run and dry_run_supported:
        return _dry_run_result("POST", "/v1/core/ws", body)

    client = _client_from_args(args)
    return client.request("POST", "/v1/core/ws", payload=body)


def cmd_auth_login(args: argparse.Namespace) -> None:
    agent, _, config_path, config = _resolve_connection(args, allow_missing_token=True)
    client = APIClient(agent=agent)

    payload: dict[str, Any] = {"long_lived_token": args.long_lived_token}
    if args.session_ttl_seconds is not None:
        payload["session_ttl_seconds"] = args.session_ttl_seconds

    result = client.request("POST", "/v1/auth/token", payload=payload, auth=False)
    config.update(
        {
            "agent": agent,
            "token": result["access_token"],
            "expires_at": result.get("expires_at"),
        }
    )
    _save_config(config_path, config)
    _print_output({"status": "ok", "agent": agent, "expires_at": result.get("expires_at")}, args.output)


def cmd_auth_me(args: argparse.Namespace) -> None:
    client = _client_from_args(args)
    _print_output(client.request("GET", "/v1/auth/me"), args.output)


def cmd_capabilities(args: argparse.Namespace) -> None:
    client = _client_from_args(args)
    _print_output(client.request("GET", "/v1/capabilities"), args.output)


def cmd_fs_tree(args: argparse.Namespace) -> None:
    client = _client_from_args(args)
    query = {
        "path": args.path,
        "max_depth": str(args.max_depth),
        "host_namespace": str(args.host_namespace).lower(),
    }
    _print_output(client.request("GET", "/v1/fs/tree", query=query), args.output)


def cmd_fs_read(args: argparse.Namespace) -> None:
    client = _client_from_args(args)
    query = {
        "path": args.path,
        "host_namespace": str(args.host_namespace).lower(),
    }
    _print_output(client.request("GET", "/v1/fs/read", query=query), args.output)


def cmd_fs_write(args: argparse.Namespace) -> None:
    _require_confirm(args, message=f"Write file '{args.path}'")

    if args.content is None and args.file is None:
        raise HAControlError("Provide --content or --file")
    content = args.content if args.content is not None else Path(args.file).read_text(encoding="utf-8")

    body = {
        "path": args.path,
        "content": content,
        "mode": args.mode,
        "create_dirs": args.create_dirs,
        "host_namespace": args.host_namespace,
    }

    if args.dry_run:
        _print_output(_dry_run_result("PUT", "/v1/fs/write", body), args.output)
        return

    client = _client_from_args(args)
    _print_output(client.request("PUT", "/v1/fs/write", payload=body), args.output)


def cmd_fs_move(args: argparse.Namespace) -> None:
    _require_confirm(args, message=f"Move '{args.src}' to '{args.dst}'")

    body = {"src": args.src, "dst": args.dst, "host_namespace": args.host_namespace}
    if args.dry_run:
        _print_output(_dry_run_result("POST", "/v1/fs/move", body), args.output)
        return

    client = _client_from_args(args)
    _print_output(client.request("POST", "/v1/fs/move", payload=body), args.output)


def cmd_fs_delete(args: argparse.Namespace) -> None:
    _require_confirm(args, message=f"Delete '{args.path}'")

    body = {
        "path": args.path,
        "recursive": args.recursive,
        "host_namespace": args.host_namespace,
    }
    if args.dry_run:
        _print_output(_dry_run_result("DELETE", "/v1/fs/delete", body), args.output)
        return

    client = _client_from_args(args)
    _print_output(client.request("DELETE", "/v1/fs/delete", payload=body), args.output)


def cmd_exec(args: argparse.Namespace) -> None:
    command = list(args.cmd)
    if command and command[0] == "--":
        command = command[1:]

    if not command:
        raise HAControlError("No command provided")

    body = {
        "cmd": command if not args.shell else " ".join(command),
        "timeout_s": args.timeout,
        "host_namespace": args.host_namespace,
        "shell": args.shell,
        "stdin": args.stdin,
        "cwd": args.cwd,
    }

    if args.dry_run:
        _print_output(_dry_run_result("POST", "/v1/exec", body), args.output)
        return

    client = _client_from_args(args)
    _print_output(client.request("POST", "/v1/exec", payload=body), args.output)


def cmd_entity_rename(args: argparse.Namespace) -> None:
    result = _core_ws(
        args,
        ws_type="config/entity_registry/update",
        payload={"entity_id": args.entity_id, "name": args.new_name},
        timeout_s=args.timeout,
        destructive=False,
        dry_run_supported=True,
    )
    _print_output(result, args.output)


def cmd_entity_update(args: argparse.Namespace) -> None:
    payload = _parse_json_value(args.json, args.file)
    if not isinstance(payload, dict):
        raise HAControlError("Entity update payload must be a JSON object")
    payload["entity_id"] = args.entity_id
    result = _core_ws(
        args,
        ws_type="config/entity_registry/update",
        payload=payload,
        timeout_s=args.timeout,
        dry_run_supported=True,
    )
    _print_output(result, args.output)


def cmd_automation_get(args: argparse.Namespace) -> None:
    _print_output(_core_rest_request(args, "GET", f"config/automation/config/{args.automation_id}"), args.output)


def cmd_automation_apply(args: argparse.Namespace) -> None:
    _require_confirm(args, message=f"Apply automation '{args.automation_id}'")
    payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
    _print_output(
        _core_rest_request(
            args,
            "POST",
            f"config/automation/config/{args.automation_id}",
            body=payload,
            destructive=True,
            dry_run_supported=True,
        ),
        args.output,
    )


def cmd_automation_delete(args: argparse.Namespace) -> None:
    _print_output(
        _core_rest_request(
            args,
            "DELETE",
            f"config/automation/config/{args.automation_id}",
            destructive=True,
            dry_run_supported=True,
        ),
        args.output,
    )


def cmd_automation_reload(args: argparse.Namespace) -> None:
    _print_output(_core_rest_request(args, "POST", "services/automation/reload", body={}), args.output)


def cmd_script_get(args: argparse.Namespace) -> None:
    _print_output(_core_rest_request(args, "GET", f"config/script/config/{args.script_id}"), args.output)


def cmd_script_apply(args: argparse.Namespace) -> None:
    _require_confirm(args, message=f"Apply script '{args.script_id}'")
    payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
    _print_output(
        _core_rest_request(
            args,
            "POST",
            f"config/script/config/{args.script_id}",
            body=payload,
            destructive=True,
            dry_run_supported=True,
        ),
        args.output,
    )


def cmd_script_delete(args: argparse.Namespace) -> None:
    _print_output(
        _core_rest_request(
            args,
            "DELETE",
            f"config/script/config/{args.script_id}",
            destructive=True,
            dry_run_supported=True,
        ),
        args.output,
    )


def cmd_script_reload(args: argparse.Namespace) -> None:
    _print_output(_core_rest_request(args, "POST", "services/script/reload", body={}), args.output)


def cmd_scene_get(args: argparse.Namespace) -> None:
    _print_output(_core_rest_request(args, "GET", f"config/scene/config/{args.scene_id}"), args.output)


def cmd_scene_apply(args: argparse.Namespace) -> None:
    _require_confirm(args, message=f"Apply scene '{args.scene_id}'")
    payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
    _print_output(
        _core_rest_request(
            args,
            "POST",
            f"config/scene/config/{args.scene_id}",
            body=payload,
            destructive=True,
            dry_run_supported=True,
        ),
        args.output,
    )


def cmd_scene_delete(args: argparse.Namespace) -> None:
    _print_output(
        _core_rest_request(
            args,
            "DELETE",
            f"config/scene/config/{args.scene_id}",
            destructive=True,
            dry_run_supported=True,
        ),
        args.output,
    )


def cmd_scene_reload(args: argparse.Namespace) -> None:
    _print_output(_core_rest_request(args, "POST", "services/scene/reload", body={}), args.output)


def cmd_dashboard_list(args: argparse.Namespace) -> None:
    _print_output(
        _core_ws(args, ws_type="lovelace/dashboards/list", timeout_s=args.timeout),
        args.output,
    )


def cmd_dashboard_create(args: argparse.Namespace) -> None:
    payload = {
        "url_path": args.url_path,
        "title": args.title,
        "icon": args.icon,
        "show_in_sidebar": args.show_in_sidebar,
        "require_admin": args.require_admin,
    }
    _print_output(
        _core_ws(
            args,
            ws_type="lovelace/dashboards/create",
            payload=payload,
            timeout_s=args.timeout,
            destructive=True,
            dry_run_supported=True,
        ),
        args.output,
    )


def cmd_dashboard_update(args: argparse.Namespace) -> None:
    payload = _parse_json_value(args.json, args.file)
    if not isinstance(payload, dict):
        raise HAControlError("Dashboard update payload must be a JSON object")
    payload["dashboard_id"] = args.dashboard_id
    _print_output(
        _core_ws(
            args,
            ws_type="lovelace/dashboards/update",
            payload=payload,
            timeout_s=args.timeout,
            destructive=True,
            dry_run_supported=True,
        ),
        args.output,
    )


def cmd_dashboard_delete(args: argparse.Namespace) -> None:
    _print_output(
        _core_ws(
            args,
            ws_type="lovelace/dashboards/delete",
            payload={"dashboard_id": args.dashboard_id},
            timeout_s=args.timeout,
            destructive=True,
            dry_run_supported=True,
        ),
        args.output,
    )


def cmd_dashboard_get_config(args: argparse.Namespace) -> None:
    payload: dict[str, Any] = {"force": args.force}
    if args.url_path is not None:
        payload["url_path"] = args.url_path
    _print_output(
        _core_ws(args, ws_type="lovelace/config", payload=payload, timeout_s=args.timeout),
        args.output,
    )


def cmd_dashboard_save_config(args: argparse.Namespace) -> None:
    config_payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
    payload: dict[str, Any] = {"config": config_payload}
    if args.url_path is not None:
        payload["url_path"] = args.url_path
    _print_output(
        _core_ws(
            args,
            ws_type="lovelace/config/save",
            payload=payload,
            timeout_s=args.timeout,
            destructive=True,
            dry_run_supported=True,
        ),
        args.output,
    )


def cmd_dashboard_resources(args: argparse.Namespace) -> None:
    ws_type = {
        "list": "lovelace/resources/list",
        "create": "lovelace/resources/create",
        "update": "lovelace/resources/update",
        "delete": "lovelace/resources/delete",
    }[args.action]

    payload = _parse_json_value(args.json, args.file)
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise HAControlError("Resources payload must be a JSON object")

    destructive = args.action in {"create", "update", "delete"}
    result = _core_ws(
        args,
        ws_type=ws_type,
        payload=payload,
        timeout_s=args.timeout,
        destructive=destructive,
        dry_run_supported=True,
    )
    _print_output(result, args.output)


def cmd_addon_list(args: argparse.Namespace) -> None:
    _print_output(_supervisor_request(args, "GET", "addons"), args.output)


def cmd_addon_info(args: argparse.Namespace) -> None:
    _print_output(_supervisor_request(args, "GET", f"addons/{args.slug}/info"), args.output)


def cmd_addon_install(args: argparse.Namespace) -> None:
    _print_output(
        _supervisor_request(
            args,
            "POST",
            f"store/addons/{args.slug}/install",
            body={},
            destructive=True,
            dry_run_supported=True,
        ),
        args.output,
    )


def cmd_addon_update(args: argparse.Namespace) -> None:
    body: dict[str, Any] = {}
    if args.version:
        # Endpoint also supports /update/{version}, but body version keeps CLI stable.
        body["version"] = args.version
    _print_output(
        _supervisor_request(
            args,
            "POST",
            f"store/addons/{args.slug}/update",
            body=body,
            destructive=True,
            dry_run_supported=True,
        ),
        args.output,
    )


def cmd_addon_start(args: argparse.Namespace) -> None:
    _print_output(_supervisor_request(args, "POST", f"addons/{args.slug}/start", body={}), args.output)


def cmd_addon_stop(args: argparse.Namespace) -> None:
    _print_output(_supervisor_request(args, "POST", f"addons/{args.slug}/stop", body={}), args.output)


def cmd_addon_logs(args: argparse.Namespace) -> None:
    query = {"lines": str(args.lines)} if args.lines is not None else None
    _print_output(_supervisor_request(args, "GET", f"addons/{args.slug}/logs", query=query), args.output)


def cmd_addon_options(args: argparse.Namespace) -> None:
    options = _parse_json_value(args.json, args.file)
    if options is None:
        raise HAControlError("Provide --json or --file for options payload")

    _print_output(
        _supervisor_request(
            args,
            "POST",
            f"addons/{args.slug}/options",
            body={"options": options},
            destructive=True,
            dry_run_supported=True,
        ),
        args.output,
    )


def cmd_os_info(args: argparse.Namespace) -> None:
    _print_output(_supervisor_request(args, "GET", "os/info"), args.output)


def cmd_os_update(args: argparse.Namespace) -> None:
    body: dict[str, Any] = {}
    if args.version:
        body["version"] = args.version
    _print_output(
        _supervisor_request(
            args,
            "POST",
            "os/update",
            body=body,
            destructive=True,
            dry_run_supported=True,
        ),
        args.output,
    )


def cmd_os_datadisk_list(args: argparse.Namespace) -> None:
    _print_output(_supervisor_request(args, "GET", "os/datadisk/list"), args.output)


def cmd_os_datadisk_move(args: argparse.Namespace) -> None:
    body = {"device": args.device}
    _print_output(
        _supervisor_request(
            args,
            "POST",
            "os/datadisk/move",
            body=body,
            destructive=True,
            dry_run_supported=True,
        ),
        args.output,
    )


def cmd_os_datadisk_wipe(args: argparse.Namespace) -> None:
    body = {"device": args.device} if args.device else {}
    _print_output(
        _supervisor_request(
            args,
            "POST",
            "os/datadisk/wipe",
            body=body,
            destructive=True,
            dry_run_supported=True,
        ),
        args.output,
    )


def cmd_os_swap_info(args: argparse.Namespace) -> None:
    _print_output(_supervisor_request(args, "GET", "os/config/swap"), args.output)


def cmd_os_swap_options(args: argparse.Namespace) -> None:
    payload = _parse_json_value(args.json, args.file)
    if payload is None:
        raise HAControlError("Provide --json or --file for swap options")
    _print_output(
        _supervisor_request(
            args,
            "POST",
            "os/config/swap",
            body=payload,
            destructive=True,
            dry_run_supported=True,
        ),
        args.output,
    )


def cmd_os_boot_slot(args: argparse.Namespace) -> None:
    _print_output(
        _supervisor_request(
            args,
            "POST",
            "os/boot-slot",
            body={"slot": args.slot},
            destructive=True,
            dry_run_supported=True,
        ),
        args.output,
    )


def cmd_host_info(args: argparse.Namespace) -> None:
    _print_output(_supervisor_request(args, "GET", "host/info"), args.output)


def cmd_host_reboot(args: argparse.Namespace) -> None:
    _print_output(
        _supervisor_request(
            args,
            "POST",
            "host/reboot",
            body={},
            destructive=True,
            dry_run_supported=True,
        ),
        args.output,
    )


def cmd_host_shutdown(args: argparse.Namespace) -> None:
    _print_output(
        _supervisor_request(
            args,
            "POST",
            "host/shutdown",
            body={},
            destructive=True,
            dry_run_supported=True,
        ),
        args.output,
    )


def cmd_host_services(args: argparse.Namespace) -> None:
    _print_output(_supervisor_request(args, "GET", "host/services"), args.output)


def cmd_host_logs(args: argparse.Namespace) -> None:
    query: dict[str, str] = {}
    if args.lines is not None:
        query["lines"] = str(args.lines)
    if args.identifier:
        path = f"host/logs/identifiers/{args.identifier}"
    else:
        path = "host/logs"
    _print_output(_supervisor_request(args, "GET", path, query=query or None), args.output)


def cmd_host_network_info(args: argparse.Namespace) -> None:
    _print_output(_supervisor_request(args, "GET", "network/info"), args.output)


def cmd_host_network_update(args: argparse.Namespace) -> None:
    payload = _parse_json_value(args.json, args.file)
    if payload is None or not isinstance(payload, dict):
        raise HAControlError("Provide --json or --file with network interface update payload")
    _print_output(
        _supervisor_request(
            args,
            "POST",
            f"network/interface/{args.interface}/update",
            body=payload,
            destructive=True,
            dry_run_supported=True,
        ),
        args.output,
    )


def cmd_raw_supervisor(args: argparse.Namespace) -> None:
    payload = _parse_json_value(args.json, args.file)
    _print_output(
        _supervisor_request(
            args,
            args.method,
            args.path,
            body=payload,
            destructive=args.destructive,
            dry_run_supported=True,
        ),
        args.output,
    )


def cmd_raw_core(args: argparse.Namespace) -> None:
    payload = _parse_json_value(args.json, args.file)
    _print_output(
        _core_rest_request(
            args,
            args.method,
            args.path,
            body=payload,
            destructive=args.destructive,
            dry_run_supported=True,
        ),
        args.output,
    )


def cmd_raw_ws(args: argparse.Namespace) -> None:
    payload = _parse_json_value(args.json, args.file)
    if not isinstance(payload, dict):
        raise HAControlError("WebSocket payload must be a JSON object")

    if _looks_like_agent_ws_envelope(payload):
        request_payload = payload
    else:
        request_payload = {"message": payload}

    if args.dry_run:
        _print_output(_dry_run_result("POST", "/v1/core/ws", request_payload), args.output)
        return

    client = _client_from_args(args)
    _print_output(client.request("POST", "/v1/core/ws", payload=request_payload), args.output)


def _looks_like_agent_ws_envelope(payload: dict[str, Any]) -> bool:
    if "message" in payload:
        return True

    if "type" not in payload:
        return False

    return set(payload.keys()).issubset({"type", "payload", "id", "timeout_s"})


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ha-control", description="Full-control Home Assistant OS CLI")
    parser.add_argument("--agent", help="Agent base URL, e.g. http://192.168.1.10:9123")
    parser.add_argument("--token", help="Session token (overrides saved config)")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Config file path")
    parser.add_argument("--output", default="json", choices=["json", "yaml", "table", "raw"], help="Output format")
    parser.add_argument("--yes", action="store_true", help="Approve destructive operations")
    parser.add_argument("--confirm", action="store_true", help="Confirm destructive operation execution")
    parser.add_argument("--dry-run", action="store_true", help="Show planned mutation without executing it")

    sub = parser.add_subparsers(dest="command", required=True)

    # auth
    auth = sub.add_parser("auth", help="Authentication commands")
    auth_sub = auth.add_subparsers(dest="auth_cmd", required=True)

    auth_login = auth_sub.add_parser("login", help="Create a session token from a long-lived token")
    auth_login.add_argument("--long-lived-token", required=True, help="Home Assistant long-lived access token")
    auth_login.add_argument("--session-ttl-seconds", type=int, default=None)
    auth_login.set_defaults(func=cmd_auth_login)

    auth_me = auth_sub.add_parser("me", help="Show current session subject")
    auth_me.set_defaults(func=cmd_auth_me)

    # capabilities
    caps = sub.add_parser("capabilities", help="Show discovered agent capabilities")
    caps.set_defaults(func=cmd_capabilities)

    # fs
    fs = sub.add_parser("fs", help="Filesystem operations")
    fs_sub = fs.add_subparsers(dest="fs_cmd", required=True)

    fs_tree = fs_sub.add_parser("tree", help="List path tree")
    fs_tree.add_argument("path")
    fs_tree.add_argument("--max-depth", type=int, default=2)
    fs_tree.add_argument("--host-namespace", action=argparse.BooleanOptionalAction, default=True)
    fs_tree.set_defaults(func=cmd_fs_tree)

    fs_read = fs_sub.add_parser("read", help="Read a file")
    fs_read.add_argument("path")
    fs_read.add_argument("--host-namespace", action=argparse.BooleanOptionalAction, default=True)
    fs_read.set_defaults(func=cmd_fs_read)

    fs_write = fs_sub.add_parser("write", help="Write a file")
    fs_write.add_argument("path")
    fs_write.add_argument("--content")
    fs_write.add_argument("--file")
    fs_write.add_argument("--mode")
    fs_write.add_argument("--create-dirs", action=argparse.BooleanOptionalAction, default=True)
    fs_write.add_argument("--host-namespace", action=argparse.BooleanOptionalAction, default=True)
    fs_write.set_defaults(func=cmd_fs_write)

    fs_move = fs_sub.add_parser("move", help="Move or rename path")
    fs_move.add_argument("src")
    fs_move.add_argument("dst")
    fs_move.add_argument("--host-namespace", action=argparse.BooleanOptionalAction, default=True)
    fs_move.set_defaults(func=cmd_fs_move)

    fs_delete = fs_sub.add_parser("delete", help="Delete file or directory")
    fs_delete.add_argument("path")
    fs_delete.add_argument("--recursive", action="store_true")
    fs_delete.add_argument("--host-namespace", action=argparse.BooleanOptionalAction, default=True)
    fs_delete.set_defaults(func=cmd_fs_delete)

    # exec
    exec_cmd = sub.add_parser("exec", help="Execute a host/container command")
    exec_cmd.add_argument("--timeout", type=int, default=120)
    exec_cmd.add_argument("--host-namespace", action=argparse.BooleanOptionalAction, default=True)
    exec_cmd.add_argument("--shell", action="store_true", help="Interpret command as shell string")
    exec_cmd.add_argument("--stdin", help="Optional stdin content")
    exec_cmd.add_argument("--cwd", help="Optional working directory")
    exec_cmd.add_argument("cmd", nargs=argparse.REMAINDER)
    exec_cmd.set_defaults(func=cmd_exec)

    # entity
    entity = sub.add_parser("entity", help="Entity operations")
    entity_sub = entity.add_subparsers(dest="entity_cmd", required=True)

    entity_rename = entity_sub.add_parser("rename", help="Rename an entity")
    entity_rename.add_argument("entity_id")
    entity_rename.add_argument("new_name")
    entity_rename.add_argument("--timeout", type=int, default=30)
    entity_rename.set_defaults(func=cmd_entity_rename)

    entity_update = entity_sub.add_parser("update", help="Update entity registry entry")
    entity_update.add_argument("entity_id")
    entity_update.add_argument("--json")
    entity_update.add_argument("--file")
    entity_update.add_argument("--timeout", type=int, default=30)
    entity_update.set_defaults(func=cmd_entity_update)

    # automation
    automation = sub.add_parser("automation", help="Automation create/update/delete/reload")
    automation_sub = automation.add_subparsers(dest="automation_cmd", required=True)

    automation_get = automation_sub.add_parser("get", help="Get automation config by id")
    automation_get.add_argument("automation_id")
    automation_get.set_defaults(func=cmd_automation_get)

    automation_apply = automation_sub.add_parser("apply", help="Create/update automation from JSON file")
    automation_apply.add_argument("automation_id")
    automation_apply.add_argument("--file", required=True)
    automation_apply.set_defaults(func=cmd_automation_apply)

    automation_delete = automation_sub.add_parser("delete", help="Delete automation by id")
    automation_delete.add_argument("automation_id")
    automation_delete.set_defaults(func=cmd_automation_delete)

    automation_reload = automation_sub.add_parser("reload", help="Reload automations")
    automation_reload.set_defaults(func=cmd_automation_reload)

    # script
    script = sub.add_parser("script", help="Script create/update/delete/reload")
    script_sub = script.add_subparsers(dest="script_cmd", required=True)

    script_get = script_sub.add_parser("get", help="Get script config by id")
    script_get.add_argument("script_id")
    script_get.set_defaults(func=cmd_script_get)

    script_apply = script_sub.add_parser("apply", help="Create/update script from JSON file")
    script_apply.add_argument("script_id")
    script_apply.add_argument("--file", required=True)
    script_apply.set_defaults(func=cmd_script_apply)

    script_delete = script_sub.add_parser("delete", help="Delete script by id")
    script_delete.add_argument("script_id")
    script_delete.set_defaults(func=cmd_script_delete)

    script_reload = script_sub.add_parser("reload", help="Reload scripts")
    script_reload.set_defaults(func=cmd_script_reload)

    # scene
    scene = sub.add_parser("scene", help="Scene create/update/delete/reload")
    scene_sub = scene.add_subparsers(dest="scene_cmd", required=True)

    scene_get = scene_sub.add_parser("get", help="Get scene config by id")
    scene_get.add_argument("scene_id")
    scene_get.set_defaults(func=cmd_scene_get)

    scene_apply = scene_sub.add_parser("apply", help="Create/update scene from JSON file")
    scene_apply.add_argument("scene_id")
    scene_apply.add_argument("--file", required=True)
    scene_apply.set_defaults(func=cmd_scene_apply)

    scene_delete = scene_sub.add_parser("delete", help="Delete scene by id")
    scene_delete.add_argument("scene_id")
    scene_delete.set_defaults(func=cmd_scene_delete)

    scene_reload = scene_sub.add_parser("reload", help="Reload scenes")
    scene_reload.set_defaults(func=cmd_scene_reload)

    # dashboard
    dashboard = sub.add_parser("dashboard", help="Lovelace dashboard operations")
    dashboard_sub = dashboard.add_subparsers(dest="dashboard_cmd", required=True)

    dashboard_list = dashboard_sub.add_parser("list", help="List dashboards")
    dashboard_list.add_argument("--timeout", type=int, default=30)
    dashboard_list.set_defaults(func=cmd_dashboard_list)

    dashboard_create = dashboard_sub.add_parser("create", help="Create dashboard")
    dashboard_create.add_argument("--url-path", required=True)
    dashboard_create.add_argument("--title", required=True)
    dashboard_create.add_argument("--icon", default="mdi:view-dashboard")
    dashboard_create.add_argument("--show-in-sidebar", action=argparse.BooleanOptionalAction, default=True)
    dashboard_create.add_argument("--require-admin", action=argparse.BooleanOptionalAction, default=False)
    dashboard_create.add_argument("--timeout", type=int, default=30)
    dashboard_create.set_defaults(func=cmd_dashboard_create)

    dashboard_update = dashboard_sub.add_parser("update", help="Update dashboard metadata")
    dashboard_update.add_argument("dashboard_id")
    dashboard_update.add_argument("--json")
    dashboard_update.add_argument("--file")
    dashboard_update.add_argument("--timeout", type=int, default=30)
    dashboard_update.set_defaults(func=cmd_dashboard_update)

    dashboard_delete = dashboard_sub.add_parser("delete", help="Delete dashboard")
    dashboard_delete.add_argument("dashboard_id")
    dashboard_delete.add_argument("--timeout", type=int, default=30)
    dashboard_delete.set_defaults(func=cmd_dashboard_delete)

    dashboard_get_config = dashboard_sub.add_parser("get-config", help="Get Lovelace config")
    dashboard_get_config.add_argument("--url-path")
    dashboard_get_config.add_argument("--force", action="store_true")
    dashboard_get_config.add_argument("--timeout", type=int, default=30)
    dashboard_get_config.set_defaults(func=cmd_dashboard_get_config)

    dashboard_save_config = dashboard_sub.add_parser("save-config", help="Save Lovelace config from file")
    dashboard_save_config.add_argument("--file", required=True)
    dashboard_save_config.add_argument("--url-path")
    dashboard_save_config.add_argument("--timeout", type=int, default=30)
    dashboard_save_config.set_defaults(func=cmd_dashboard_save_config)

    dashboard_resources = dashboard_sub.add_parser("resources", help="Manage Lovelace resources")
    dashboard_resources.add_argument("action", choices=["list", "create", "update", "delete"])
    dashboard_resources.add_argument("--json")
    dashboard_resources.add_argument("--file")
    dashboard_resources.add_argument("--timeout", type=int, default=30)
    dashboard_resources.set_defaults(func=cmd_dashboard_resources)

    # addon
    addon = sub.add_parser("addon", help="Add-on lifecycle and config")
    addon_sub = addon.add_subparsers(dest="addon_cmd", required=True)

    addon_list = addon_sub.add_parser("list", help="List installed add-ons")
    addon_list.set_defaults(func=cmd_addon_list)

    addon_info = addon_sub.add_parser("info", help="Get add-on info")
    addon_info.add_argument("slug")
    addon_info.set_defaults(func=cmd_addon_info)

    addon_install = addon_sub.add_parser("install", help="Install add-on")
    addon_install.add_argument("slug")
    addon_install.set_defaults(func=cmd_addon_install)

    addon_update = addon_sub.add_parser("update", help="Update add-on")
    addon_update.add_argument("slug")
    addon_update.add_argument("--version")
    addon_update.set_defaults(func=cmd_addon_update)

    addon_start = addon_sub.add_parser("start", help="Start add-on")
    addon_start.add_argument("slug")
    addon_start.set_defaults(func=cmd_addon_start)

    addon_stop = addon_sub.add_parser("stop", help="Stop add-on")
    addon_stop.add_argument("slug")
    addon_stop.set_defaults(func=cmd_addon_stop)

    addon_logs = addon_sub.add_parser("logs", help="Get add-on logs")
    addon_logs.add_argument("slug")
    addon_logs.add_argument("--lines", type=int)
    addon_logs.set_defaults(func=cmd_addon_logs)

    addon_options = addon_sub.add_parser("options", help="Update add-on options")
    addon_options.add_argument("slug")
    addon_options.add_argument("--json")
    addon_options.add_argument("--file")
    addon_options.set_defaults(func=cmd_addon_options)

    # os
    os_cmd = sub.add_parser("os", help="Operating system operations")
    os_sub = os_cmd.add_subparsers(dest="os_cmd", required=True)

    os_info = os_sub.add_parser("info", help="Get OS info")
    os_info.set_defaults(func=cmd_os_info)

    os_update = os_sub.add_parser("update", help="Start OS update")
    os_update.add_argument("--version")
    os_update.set_defaults(func=cmd_os_update)

    os_datadisk = os_sub.add_parser("datadisk", help="Data disk operations")
    os_datadisk_sub = os_datadisk.add_subparsers(dest="os_datadisk_cmd", required=True)

    os_datadisk_list = os_datadisk_sub.add_parser("list", help="List available data disks")
    os_datadisk_list.set_defaults(func=cmd_os_datadisk_list)

    os_datadisk_move = os_datadisk_sub.add_parser("move", help="Move data disk")
    os_datadisk_move.add_argument("--device", required=True)
    os_datadisk_move.set_defaults(func=cmd_os_datadisk_move)

    os_datadisk_wipe = os_datadisk_sub.add_parser("wipe", help="Wipe data disk")
    os_datadisk_wipe.add_argument("--device")
    os_datadisk_wipe.set_defaults(func=cmd_os_datadisk_wipe)

    os_swap = os_sub.add_parser("swap", help="Swap config operations")
    os_swap_sub = os_swap.add_subparsers(dest="os_swap_cmd", required=True)

    os_swap_info = os_swap_sub.add_parser("info", help="Get swap config")
    os_swap_info.set_defaults(func=cmd_os_swap_info)

    os_swap_options = os_swap_sub.add_parser("set", help="Set swap options with JSON payload")
    os_swap_options.add_argument("--json")
    os_swap_options.add_argument("--file")
    os_swap_options.set_defaults(func=cmd_os_swap_options)

    os_boot_slot = os_sub.add_parser("boot-slot", help="Set next boot slot")
    os_boot_slot.add_argument("slot")
    os_boot_slot.set_defaults(func=cmd_os_boot_slot)

    # host
    host = sub.add_parser("host", help="Host/network/services/logs operations")
    host_sub = host.add_subparsers(dest="host_cmd", required=True)

    host_info = host_sub.add_parser("info", help="Get host info")
    host_info.set_defaults(func=cmd_host_info)

    host_reboot = host_sub.add_parser("reboot", help="Reboot host")
    host_reboot.set_defaults(func=cmd_host_reboot)

    host_shutdown = host_sub.add_parser("shutdown", help="Shutdown host")
    host_shutdown.set_defaults(func=cmd_host_shutdown)

    host_services = host_sub.add_parser("services", help="List host services")
    host_services.set_defaults(func=cmd_host_services)

    host_logs = host_sub.add_parser("logs", help="Fetch host logs")
    host_logs.add_argument("--identifier")
    host_logs.add_argument("--lines", type=int)
    host_logs.set_defaults(func=cmd_host_logs)

    host_network_info = host_sub.add_parser("network-info", help="Get network info")
    host_network_info.set_defaults(func=cmd_host_network_info)

    host_network_update = host_sub.add_parser("network-update", help="Update network interface")
    host_network_update.add_argument("--interface", required=True)
    host_network_update.add_argument("--json")
    host_network_update.add_argument("--file")
    host_network_update.set_defaults(func=cmd_host_network_update)

    # raw
    raw = sub.add_parser("raw", help="Raw passthrough commands")
    raw_sub = raw.add_subparsers(dest="raw_cmd", required=True)

    raw_supervisor = raw_sub.add_parser("supervisor", help="Raw Supervisor API call")
    raw_supervisor.add_argument("method")
    raw_supervisor.add_argument("path")
    raw_supervisor.add_argument("--json")
    raw_supervisor.add_argument("--file")
    raw_supervisor.add_argument("--destructive", action="store_true")
    raw_supervisor.set_defaults(func=cmd_raw_supervisor)

    raw_core = raw_sub.add_parser("core", help="Raw Core REST API call")
    raw_core.add_argument("method")
    raw_core.add_argument("path")
    raw_core.add_argument("--json")
    raw_core.add_argument("--file")
    raw_core.add_argument("--destructive", action="store_true")
    raw_core.set_defaults(func=cmd_raw_core)

    raw_ws = raw_sub.add_parser("ws", help="Raw Core WebSocket call")
    raw_ws.add_argument("--json")
    raw_ws.add_argument("--file")
    raw_ws.set_defaults(func=cmd_raw_ws)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        args.func(args)
    except HAControlError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("error: interrupted", file=sys.stderr)
        return 130

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
