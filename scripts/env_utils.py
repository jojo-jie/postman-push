"""Shared .env parsing helpers for postman-push scripts."""

from __future__ import annotations

import os
import re
from pathlib import Path


ENV_REF_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")


def strip_inline_comment(value: str) -> str:
    quote: str | None = None
    for idx, char in enumerate(value):
        if quote:
            if char == quote and value[idx - 1 : idx] != "\\":
                quote = None
            continue
        if char in ("'", '"'):
            quote = char
        elif char == "#" and (idx == 0 or value[idx - 1].isspace()):
            return value[:idx].rstrip()
    return value.strip()


def expand_env_refs(value: str, values: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(2)
        return values.get(name, os.environ.get(name, match.group(0)))

    return ENV_REF_PATTERN.sub(replace, value)


def parse_env_text(text: str, values: dict[str, str] | None = None) -> dict[str, str]:
    parsed: dict[str, str] = {}
    combined = dict(values or {})
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
            continue
        value = strip_inline_comment(value.strip()).strip("'\"")
        parsed[key] = expand_env_refs(value, {**combined, **parsed})
    return parsed


def load_env_files(repo: Path, explicit_env: str | Path | None = None) -> dict[str, str]:
    candidates = [Path(explicit_env).resolve()] if explicit_env else [repo / ".env", repo / ".env.local", repo / ".env.development"]
    values: dict[str, str] = {}
    for path in candidates:
        if not path.exists():
            continue
        values.update(parse_env_text(path.read_text(), values))
    return values
