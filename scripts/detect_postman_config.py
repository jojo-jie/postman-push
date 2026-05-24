#!/usr/bin/env python3
"""Detect Postman MCP configuration across common AI hosts."""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None


HOSTS = ("codex", "cursor", "claude")


@dataclass
class Candidate:
    host: str
    source: str
    server_name: str
    transport: str
    location: str | None
    auth_kind: str | None
    auth_value: str | None
    auth_source: str | None
    auth_env_var: str | None = None


def mask_secret(value: str | None) -> str | None:
    if not value:
        return value
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


def load_json_object(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def resolve_auth_value(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None

    def replace_env(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(2)
        return os.environ.get(name, match.group(0))

    resolved = re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)", replace_env, value).strip()
    if resolved in os.environ:
        return os.environ[resolved]
    return resolved


def extract_env_var_refs(value: str) -> set[str]:
    return {match.group(1) or match.group(2) for match in re.finditer(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)", value)}


def detect_host(explicit: str) -> str:
    if explicit != "auto":
        return explicit
    env = os.environ
    if env.get("AI_HOST_TOOL"):
        host = env["AI_HOST_TOOL"].strip().lower()
        if host in HOSTS:
            return host
    if env.get("CODEX_SANDBOX") or env.get("CODEX_SESSION_ID") or env.get("CODEX_HOME"):
        return "codex"
    if env.get("CURSOR_TRACE_ID") or env.get("CURSOR_AGENT"):
        return "cursor"
    if env.get("CLAUDECODE") or env.get("CLAUDE_CODE_ENTRYPOINT"):
        return "claude"
    return "codex"


def looks_like_postman(name: str, payload: dict[str, Any]) -> bool:
    haystacks = [name]
    for key in ("url", "command"):
        value = payload.get(key)
        if isinstance(value, str):
            haystacks.append(value)
    args = payload.get("args")
    if isinstance(args, list):
        haystacks.extend(str(arg) for arg in args)
    joined = " ".join(haystacks).lower()
    return "postman" in joined


def parse_cursor(home: Path) -> list[Candidate]:
    path = home / ".cursor" / "mcp.json"
    if not path.exists():
        return []
    data = load_json_object(path)
    if data is None:
        return []
    servers = data.get("mcpServers", {})
    found: list[Candidate] = []
    for name, payload in servers.items():
        if not isinstance(payload, dict) or not looks_like_postman(name, payload):
            continue
        headers = payload.get("headers", {}) if isinstance(payload.get("headers"), dict) else {}
        raw_auth = headers.get("Authorization")
        auth_value = resolve_auth_value(raw_auth)
        auth_env_var = None
        if isinstance(raw_auth, str):
            refs = extract_env_var_refs(raw_auth)
            if len(refs) == 1:
                auth_env_var = next(iter(refs))
        found.append(
            Candidate(
                host="cursor",
                source=str(path),
                server_name=name,
                transport="http" if payload.get("url") else "stdio",
                location=payload.get("url") or payload.get("command"),
                auth_kind="header" if auth_value else None,
                auth_value=auth_value,
                auth_source="headers.Authorization" if auth_value else None,
                auth_env_var=auth_env_var,
            )
        )
    return found


def parse_codex(home: Path) -> list[Candidate]:
    path = home / ".codex" / "config.toml"
    if not path.exists() or tomllib is None:
        return []
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return []
    servers = data.get("mcp_servers", {})
    found: list[Candidate] = []
    for name, payload in servers.items():
        if not isinstance(payload, dict) or not looks_like_postman(name, payload):
            continue
        auth_var = payload.get("bearer_token_env_var")
        headers = payload.get("headers", {}) if isinstance(payload.get("headers"), dict) else {}
        raw_auth = headers.get("Authorization")
        header_auth = resolve_auth_value(raw_auth)
        header_auth_env = None
        if isinstance(raw_auth, str):
            refs = extract_env_var_refs(raw_auth)
            if len(refs) == 1:
                header_auth_env = next(iter(refs))
        auth_value = os.environ.get(auth_var) if auth_var else header_auth
        found.append(
            Candidate(
                host="codex",
                source=str(path),
                server_name=name,
                transport="http" if payload.get("url") else "stdio",
                location=payload.get("url") or payload.get("command"),
                auth_kind="env_var" if auth_var else ("header" if header_auth else None),
                auth_value=auth_value,
                auth_source=auth_var or ("headers.Authorization" if header_auth else None),
                auth_env_var=auth_var or header_auth_env,
            )
        )
    return found


def parse_claude(home: Path) -> list[Candidate]:
    candidates: list[Candidate] = []
    patterns = [
        home / ".claude" / ".claude.json",
        home / ".claude.json",
        home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json",
    ]
    for path in patterns:
        if not path.exists():
            continue
        data = load_json_object(path)
        if data is None:
            continue
        servers = data.get("mcpServers") or data.get("mcp_servers") or {}
        if not isinstance(servers, dict):
            continue
        for name, payload in servers.items():
            if not isinstance(payload, dict) or not looks_like_postman(name, payload):
                continue
            env_map = payload.get("env", {}) if isinstance(payload.get("env"), dict) else {}
            auth_value = None
            auth_source = None
            env_key = env_map.get("POSTMAN_API_KEY")
            if isinstance(env_key, str):
                auth_source = "env.POSTMAN_API_KEY"
                auth_value = resolve_auth_value(env_key)
                auth_env_var = env_key if env_key in os.environ else None
            command = payload.get("command")
            if isinstance(command, list):
                command = " ".join(command)
            candidates.append(
                Candidate(
                    host="claude",
                    source=str(path),
                    server_name=name,
                    transport="http" if payload.get("url") else "stdio",
                    location=payload.get("url") or command,
                    auth_kind="env_var" if auth_value else None,
                    auth_value=auth_value,
                    auth_source=auth_source,
                    auth_env_var=auth_env_var,
                )
            )
    return candidates


def detect_all(home: Path) -> dict[str, list[Candidate]]:
    return {
        "codex": parse_codex(home),
        "cursor": parse_cursor(home),
        "claude": parse_claude(home),
    }


def serialize_candidate(candidate: Candidate, include_secrets: bool) -> dict[str, Any]:
    payload = asdict(candidate)
    payload["auth_value"] = candidate.auth_value if include_secrets else mask_secret(candidate.auth_value)
    return payload


def as_output(host: str, all_candidates: dict[str, list[Candidate]], include_secrets: bool) -> dict[str, Any]:
    current = all_candidates.get(host, [])
    fallback = [c for other_host, entries in all_candidates.items() if other_host != host for c in entries]
    chosen = current[0] if current else (fallback[0] if fallback else None)
    return {
        "host": host,
        "current_host_candidates": [serialize_candidate(candidate, include_secrets) for candidate in current],
        "fallback_candidates": [serialize_candidate(candidate, include_secrets) for candidate in fallback],
        "chosen": serialize_candidate(chosen, include_secrets) if chosen else None,
        "usable_via_current_host": bool(current),
        "notes": [
            "Current host is the preferred source of truth.",
            "Fallback candidates are shown only for recovery when the current host has no usable Postman MCP.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", choices=("auto", *HOSTS), default="auto")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument("--include-secrets", action="store_true", help="include raw auth values in JSON output")
    args = parser.parse_args()

    home = Path.home()
    host = detect_host(args.host)
    result = as_output(host, detect_all(home), include_secrets=args.include_secrets)

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0

    print(f"Current host: {result['host']}")
    chosen = result["chosen"]
    if not chosen:
        print("No Postman MCP configuration detected.")
        return 1

    print(f"Chosen source: {chosen['source']}")
    print(f"Server: {chosen['server_name']}")
    print(f"Transport: {chosen['transport']}")
    if chosen.get("location"):
        print(f"Location: {chosen['location']}")
    if chosen.get("auth_kind"):
        print(f"Auth: {chosen['auth_kind']} -> {chosen['auth_value']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
