#!/usr/bin/env python3
"""Push discovered APIs into a configured Postman collection."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib import error, request
from urllib.parse import urlsplit


SCRIPT_DIR = Path(__file__).resolve().parent


def run_python_json(script: str, *args: str) -> dict[str, Any]:
    command = [sys.executable, str(SCRIPT_DIR / script), *args, "--json"]
    completed = subprocess.run(command, check=True, capture_output=True, text=True)
    return json.loads(completed.stdout)


def load_env(repo: Path, explicit_env: str | None) -> dict[str, str]:
    candidates = [Path(explicit_env).resolve()] if explicit_env else [repo / ".env", repo / ".env.local", repo / ".env.development"]
    values: dict[str, str] = {}
    for path in candidates:
        if not path.exists():
            continue
        for raw in path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("'\"")
    if "POSTMAN_API_KEY" in os.environ:
        values["POSTMAN_API_KEY"] = os.environ["POSTMAN_API_KEY"]
    return values


@dataclass
class CollectionTarget:
    key: str
    collection_id: str
    host: str | None


def collection_targets(env: dict[str, str]) -> list[CollectionTarget]:
    targets: list[CollectionTarget] = []
    prefix = "POSTMAN_COLLECTION_ID_"
    for key, value in env.items():
        if not key.startswith(prefix) or key.endswith("_HOST"):
            continue
        suffix = key[len(prefix) :]
        targets.append(
            CollectionTarget(
                key=suffix,
                collection_id=value,
                host=env.get(f"{prefix}{suffix}_HOST"),
            )
        )
    return sorted(targets, key=lambda item: item.key)


def classify_audience(apis: list[dict[str, Any]]) -> str:
    joined = " ".join(
        part.lower()
        for api in apis
        for part in ([api.get("path", "")] + [tag for tag in api.get("tags", []) if isinstance(tag, str)])
    )
    if any(token in joined for token in ("admin", "internal", "private", "backoffice")):
        return "internal"
    return "open"


def choose_target(targets: list[CollectionTarget], explicit_key: str | None, audience: str) -> tuple[CollectionTarget, str]:
    if explicit_key:
        for target in targets:
            if target.key.lower() == explicit_key.lower():
                return target, "explicit"
        raise SystemExit(f"Configured collections do not contain key {explicit_key!r}")

    if audience == "open":
        preferred = ("open", "public", "external")
    else:
        preferred = ("internal", "admin", "private")
    for token in preferred:
        for target in targets:
            if token in target.key.lower():
                return target, f"heuristic:{token}"
    if audience == "internal":
        for target in targets:
            if any(token in target.key.lower() for token in ("open", "public", "external")):
                continue
            return target, "heuristic:first-non-open"
    return targets[0], "heuristic:first"


def to_description(api: dict[str, Any]) -> str:
    lines = [api.get("description") or f"{api['method']} {api['path']}"]
    if api.get("inferred_description"):
        lines.append("")
        lines.append("Note: this description was inferred from project structure because no explicit summary was found.")

    for label, key in (
        ("Path Params", "path_params"),
        ("Query Params", "query_params"),
        ("Headers", "headers"),
        ("Body Params", "body_params"),
    ):
        params = api.get(key) or []
        if not params:
            continue
        lines.append("")
        lines.append(label)
        for param in params:
            line = f"- {param['name']}"
            if param.get("required"):
                line += " (required)"
            if param.get("description"):
                line += f": {param['description']}"
            if param.get("example"):
                line += f" Example: {param['example']}"
            lines.append(line)
    return "\n".join(lines)


def raw_url(host: str | None, path: str) -> tuple[str, list[str]]:
    base = (host or "{{baseUrl}}").rstrip("/")
    clean_path = path if path.startswith("/") else f"/{path}"
    postman_path = to_postman_variable_path(clean_path)
    return f"{base}{postman_path}", [segment for segment in postman_path.strip("/").split("/") if segment]


def normalize_signature_path(path: str) -> str:
    normalized = path.strip()
    if not normalized:
        return "/"
    if "://" in normalized:
        parsed = urlsplit(normalized)
        normalized = parsed.path or "/"
    else:
        normalized = re.sub(r"^\{\{[^}]+\}\}", "", normalized)
        if not normalized.startswith("/") and "/" in normalized and "." in normalized.split("/", 1)[0]:
            normalized = "/" + normalized.split("/", 1)[1]
    normalized = normalized.split("?", 1)[0].split("#", 1)[0]
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    normalized = re.sub(r"\{\{([A-Za-z0-9_.:-]+)\}\}", r"{\1}", normalized)
    normalized = re.sub(r":([A-Za-z0-9_]+)", r"{\1}", normalized)
    normalized = re.sub(r"//+", "/", normalized)
    if normalized != "/" and normalized.endswith("/"):
        normalized = normalized.rstrip("/")
    return normalized


def to_postman_variable_path(path: str) -> str:
    return re.sub(r"\{([A-Za-z0-9_.:-]+)\}", r"{{\1}}", path)


def path_variables(path: str, params: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_name = {
        param.get("name"): param
        for param in params
        if isinstance(param, dict) and isinstance(param.get("name"), str)
    }
    names = re.findall(r"\{([A-Za-z0-9_.:-]+)\}", path)
    variables = []
    seen: set[str] = set()
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        param = by_name.get(name, {})
        variables.append(
            {
                "key": name,
                "value": param.get("example") or "",
                "description": param.get("description") or "",
            }
        )
    return variables


def sample_value(item: dict[str, Any]) -> Any:
    example = item.get("example")
    if example is None:
        return ""
    if not isinstance(example, str):
        return example
    try:
        return json.loads(example)
    except json.JSONDecodeError:
        return example


def insert_sample_field(target: dict[str, Any], field_name: str, value: Any) -> None:
    if not field_name:
        return
    current: dict[str, Any] = target
    parts = field_name.split(".")
    for idx, raw_part in enumerate(parts):
        is_last = idx == len(parts) - 1
        is_array = raw_part.endswith("[]")
        key = raw_part[:-2] if is_array else raw_part
        if not key:
            continue
        if is_array:
            if is_last:
                current[key] = [value]
                return
            existing = current.get(key)
            if not isinstance(existing, list) or not existing or not isinstance(existing[0], dict):
                existing = [{}]
                current[key] = existing
            current = existing[0]
            continue
        if is_last:
            if isinstance(current.get(key), (dict, list)) and value == "":
                return
            current[key] = value
            return
        if not isinstance(current.get(key), dict):
            current[key] = {}
        current = current[key]


def body_media_type(body_params: list[dict[str, Any]]) -> str:
    for item in body_params:
        location = item.get("location")
        if isinstance(location, str) and location.startswith("body:"):
            return location.split(":", 1)[1].lower()
    return "application/json"


def to_postman_body(body_params: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not body_params:
        return None
    media_type = body_media_type(body_params)
    if "x-www-form-urlencoded" in media_type:
        return {
            "mode": "urlencoded",
            "urlencoded": [
                {
                    "key": item["name"],
                    "value": item.get("example") or "",
                    "description": item.get("description") or "",
                    "disabled": not item.get("required", False),
                }
                for item in body_params
            ],
        }
    if "multipart/form-data" in media_type:
        return {
            "mode": "formdata",
            "formdata": [
                {
                    "key": item["name"],
                    "value": item.get("example") or "",
                    "type": "text",
                    "description": item.get("description") or "",
                    "disabled": not item.get("required", False),
                }
                for item in body_params
            ],
        }
    sample: dict[str, Any] = {}
    for item in sorted(body_params, key=lambda param: (str(param.get("name", "")).count("."), str(param.get("name", "")))):
        insert_sample_field(sample, str(item.get("name", "")), sample_value(item))
    return {"mode": "raw", "raw": json.dumps(sample, indent=2, ensure_ascii=False), "options": {"raw": {"language": "json"}}}


def to_postman_item(api: dict[str, Any], host: str | None) -> dict[str, Any]:
    normalized_path = normalize_signature_path(api["path"])
    raw, path_segments = raw_url(host, normalized_path)
    headers = []
    for item in api.get("headers", []):
        headers.append(
            {
                "key": item["name"],
                "value": item.get("example") or "",
                "description": item.get("description") or "",
                "disabled": not item.get("required", False),
            }
        )

    query = []
    for item in api.get("query_params", []):
        query.append(
            {
                "key": item["name"],
                "value": item.get("example") or "",
                "description": item.get("description") or "",
                "disabled": not item.get("required", False),
            }
        )

    body_params = api.get("body_params") or []
    body = to_postman_body(body_params)
    variables = path_variables(normalized_path, api.get("path_params") or [])

    return {
        "name": f"{api['method']} {api['path']}",
        "request": {
            "method": api["method"],
            "header": headers,
            "description": to_description(api),
            "url": {
                "raw": raw,
                "host": [host] if host else ["{{baseUrl}}"],
                "path": path_segments,
                "query": query,
                **({"variable": variables} if variables else {}),
            },
            **({"body": body} if body else {}),
        },
        "response": [],
    }


def extract_item_path(item: dict[str, Any]) -> str:
    url = item.get("request", {}).get("url", {})
    raw = url.get("raw")
    if isinstance(raw, str):
        return normalize_signature_path(raw)
    path = url.get("path")
    if isinstance(path, list):
        return normalize_signature_path("/" + "/".join(path))
    variable_path = url.get("variable")
    if isinstance(variable_path, list):
        names = [entry.get("key") for entry in variable_path if isinstance(entry, dict) and entry.get("key")]
        if names:
            return normalize_signature_path("/" + "/".join(f"{{{name}}}" for name in names))
    return "/"


def endpoint_signature(method: str, path: str) -> str:
    return f"{method.upper()} {normalize_signature_path(path)}"


def grouped_new_items(apis: list[dict[str, Any]], host: str | None) -> list[dict[str, Any]]:
    folders: dict[str, list[dict[str, Any]]] = {}
    for api in apis:
        tag = (api.get("tags") or ["ungrouped"])[0]
        folders.setdefault(tag, []).append(to_postman_item(api, host))
    return [{"name": key, "item": value} for key, value in sorted(folders.items(), key=lambda item: item[0])]


def replace_item_in_tree(items: list[dict[str, Any]], replacements: dict[str, dict[str, Any]]) -> tuple[list[dict[str, Any]], set[str]]:
    used: set[str] = set()
    updated: list[dict[str, Any]] = []
    for item in items:
        request_payload = item.get("request")
        if isinstance(request_payload, dict) and request_payload.get("method"):
            signature = endpoint_signature(request_payload["method"], extract_item_path(item))
            if signature in replacements:
                updated.append(merge_existing_item(item, replacements[signature]))
                used.add(signature)
            else:
                updated.append(item)
            continue
        if "item" in item and isinstance(item["item"], list):
            nested, nested_used = replace_item_in_tree(item["item"], replacements)
            copied = dict(item)
            copied["item"] = nested
            updated.append(copied)
            used.update(nested_used)
            continue
        updated.append(item)
    return updated, used


def merge_existing_item(existing: dict[str, Any], replacement: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    for key, value in replacement.items():
        if key in {"request", "response"}:
            continue
        merged[key] = value
    existing_request = existing.get("request") if isinstance(existing.get("request"), dict) else {}
    preserved_request = {
        key: existing_request[key]
        for key in ("auth", "certificate", "proxy")
        if key in existing_request and key not in replacement.get("request", {})
    }
    merged["request"] = {**preserved_request, **replacement.get("request", {})}
    merged["response"] = existing.get("response", replacement.get("response", []))
    return merged


def merge_collection(collection: dict[str, Any], apis: list[dict[str, Any]], host: str | None) -> dict[str, Any]:
    replacements = {endpoint_signature(api["method"], api["path"]): to_postman_item(api, host) for api in apis}
    updated_items, used = replace_item_in_tree(collection.get("item", []), replacements)
    remaining_apis = [api for api in apis if endpoint_signature(api["method"], api["path"]) not in used]
    new_items = grouped_new_items(remaining_apis, host)
    merged = dict(collection)
    merged["item"] = updated_items + new_items
    return merged


def api_request(method: str, url: str, api_key: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    req = request.Request(url, data=payload, method=method)
    req.add_header("X-Api-Key", api_key)
    req.add_header("Content-Type", "application/json")
    try:
        with request.urlopen(req) as response:
            return json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:  # pragma: no cover
        message = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"Postman API {method} {url} failed: {exc.code} {message}") from exc


def resolve_api_key(env: dict[str, str], detection: dict[str, Any]) -> tuple[str | None, str]:
    if env.get("POSTMAN_API_KEY"):
        return env["POSTMAN_API_KEY"], "env:POSTMAN_API_KEY"
    chosen = detection.get("chosen") or {}
    auth_value = chosen.get("auth_value")
    auth_env_var = chosen.get("auth_env_var")
    if isinstance(auth_env_var, str) and auth_env_var:
        candidate = os.environ.get(auth_env_var)
        if isinstance(candidate, str) and candidate:
            normalized = candidate.replace("Bearer ", "").strip()
            if normalized.startswith("PMAK-"):
                return normalized, f"host-mcp:env:{auth_env_var}"
    if isinstance(auth_value, str) and auth_value:
        normalized = auth_value.replace("Bearer ", "").strip()
        if normalized.startswith("PMAK-"):
            return normalized, "host-mcp:api-key"
    return None, "missing"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".", help="target repository path")
    parser.add_argument("--mode", choices=("full", "incremental"), default="incremental")
    parser.add_argument("--host-tool", choices=("auto", "codex", "cursor", "claude"), default="auto")
    parser.add_argument("--collection-key", help="explicit collection key suffix to target")
    parser.add_argument("--env-file", help="explicit env file")
    parser.add_argument("--include", action="append", default=[], help="substring filter for routes/modules/files")
    parser.add_argument("--discovery-json", help="precomputed discovery JSON file")
    parser.add_argument("--dry-run", action="store_true", help="print payload summary without pushing")
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    detection = run_python_json("detect_postman_config.py", "--host", args.host_tool)
    discovery_args = ["--repo", str(repo), "--mode", args.mode]
    if args.env_file:
        discovery_args.extend(["--env-file", args.env_file])
    for include in args.include:
        discovery_args.extend(["--include", include])
    discovery = (
        json.loads(Path(args.discovery_json).read_text())
        if args.discovery_json
        else run_python_json("discover_apis.py", *discovery_args)
    )
    env = load_env(repo, args.env_file)
    targets = collection_targets(env)
    if not targets:
        raise SystemExit("No POSTMAN_COLLECTION_ID_* variables were found in project env.")
    if not discovery.get("apis"):
        raise SystemExit("No APIs were discovered to push.")

    audience = classify_audience(discovery["apis"])
    target, selection_reason = choose_target(targets, args.collection_key, audience)
    summary = {
        "host_tool": detection["host"],
        "mcp_usable_in_current_host": detection["usable_via_current_host"],
        "selection_reason": selection_reason,
        "workspace_id": env.get("POSTMAN_WORKSPACE_ID"),
        "collection_key": target.key,
        "collection_id": target.collection_id,
        "host": target.host,
        "api_count": len(discovery["apis"]),
    }

    if args.dry_run:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return 0

    api_key, auth_source = resolve_api_key(env, detection)
    if not api_key:
        raise SystemExit("No usable Postman API key was found. Set POSTMAN_API_KEY, or use the host MCP directly from the current session when available.")

    base_url = os.environ.get("POSTMAN_API_BASE_URL", "https://api.getpostman.com").rstrip("/")
    current = api_request("GET", f"{base_url}/collections/{target.collection_id}", api_key)
    collection = current.get("collection")
    if not isinstance(collection, dict):
        raise SystemExit("Postman API response did not include a collection document.")

    merged = merge_collection(collection, discovery["apis"], target.host)
    response = api_request("PUT", f"{base_url}/collections/{target.collection_id}", api_key, {"collection": merged})

    print(
        json.dumps(
            {
                **summary,
                "auth_source": auth_source,
                "result": "updated",
                "response_keys": sorted(response.keys()),
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
