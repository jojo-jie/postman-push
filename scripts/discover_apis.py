#!/usr/bin/env python3
"""Discover APIs from a project using OpenAPI specs and route heuristics."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from env_utils import load_env_files

HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD")
METHOD_PATTERN = "|".join(method.lower() for method in HTTP_METHODS)
TEXT_EXTS = {
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".py",
    ".php",
    ".rb",
    ".go",
    ".java",
    ".kt",
    ".rs",
    ".json",
    ".yaml",
    ".yml",
}
ROUTE_EXTS = TEXT_EXTS - {".json", ".yaml", ".yml"}


@dataclass
class Parameter:
    name: str
    location: str
    required: bool = False
    description: str | None = None
    example: str | None = None


@dataclass
class ApiEndpoint:
    method: str
    path: str
    source: str
    description: str
    inferred_description: bool
    tags: list[str] = field(default_factory=list)
    headers: list[Parameter] = field(default_factory=list)
    query_params: list[Parameter] = field(default_factory=list)
    path_params: list[Parameter] = field(default_factory=list)
    body_params: list[Parameter] = field(default_factory=list)

    @property
    def signature(self) -> str:
        return f"{self.method} {self.path}"


def load_env_file(base_dir: Path, env_file: Path | None) -> dict[str, str]:
    return load_env_files(base_dir, env_file)


def git_changed_files(repo: Path) -> list[str]:
    commands = [
        ["git", "-C", str(repo), "diff", "--name-only"],
        ["git", "-C", str(repo), "diff", "--name-only", "--cached"],
        ["git", "-C", str(repo), "ls-files", "--others", "--exclude-standard"],
    ]
    files: list[str] = []
    for command in commands:
        try:
            completed = subprocess.run(command, check=False, capture_output=True, text=True)
        except FileNotFoundError:
            return []
        if completed.returncode != 0:
            continue
        files.extend(line.strip() for line in completed.stdout.splitlines() if line.strip())
    deduped = []
    seen = set()
    for item in files:
        if item not in seen:
            deduped.append(item)
            seen.add(item)
    return deduped


def iter_text_files(repo: Path, changed_only: list[str] | None, includes: list[str]) -> Iterable[Path]:
    changed_set = None if changed_only is None else {repo / item for item in changed_only}
    for path in repo.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in TEXT_EXTS:
            continue
        if any(part.startswith(".git") for part in path.parts):
            continue
        if changed_set is not None and path not in changed_set:
            continue
        rel = str(path.relative_to(repo))
        if includes and not any(token in rel for token in includes):
            continue
        yield path


def likely_spec_file(path: Path) -> bool:
    name = path.name.lower()
    return any(token in name for token in ("openapi", "swagger", "postman"))


def load_yaml_if_available(text: str, warnings: list[str]):
    try:
        import yaml  # type: ignore
    except ModuleNotFoundError:
        warnings.append("PyYAML is not installed; YAML OpenAPI files cannot be parsed.")
        return None
    return yaml.safe_load(text)


def openapi_endpoints(path: Path, warnings: list[str]) -> list[ApiEndpoint]:
    text = path.read_text()
    data = None
    if path.suffix.lower() == ".json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return []
    elif path.suffix.lower() in {".yaml", ".yml"}:
        data = load_yaml_if_available(text, warnings)
    if not isinstance(data, dict) or "paths" not in data:
        return []
    paths = data.get("paths", {})
    if not isinstance(paths, dict):
        return []

    endpoints: list[ApiEndpoint] = []
    for raw_path, methods in paths.items():
        if not isinstance(methods, dict):
            continue
        path_parameters_payload = methods.get("parameters", [])
        if not isinstance(path_parameters_payload, list):
            path_parameters_payload = []
        for method, payload in methods.items():
            upper_method = method.upper()
            if upper_method not in HTTP_METHODS or not isinstance(payload, dict):
                continue
            description = payload.get("summary") or payload.get("description") or f"{upper_method} {raw_path}"
            endpoint = ApiEndpoint(
                method=upper_method,
                path=normalize_path(raw_path),
                source=str(path),
                description=str(description),
                inferred_description=not bool(payload.get("summary") or payload.get("description")),
                tags=[str(tag) for tag in payload.get("tags", []) if isinstance(tag, str)],
            )
            operation_parameters = payload.get("parameters", [])
            if not isinstance(operation_parameters, list):
                operation_parameters = []
            for param in [*path_parameters_payload, *operation_parameters]:
                parsed = parse_openapi_parameter(param, data)
                if not parsed:
                    continue
                add_parameter(endpoint, parsed)
            request_body = payload.get("requestBody")
            if isinstance(request_body, dict):
                for body_param in extract_request_body(request_body, data):
                    endpoint.body_params.append(body_param)
            endpoint.path_params = unique_params(endpoint.path_params + path_parameters(endpoint.path))
            endpoints.append(endpoint)
    return endpoints


def resolve_openapi_ref(ref: str, root: dict[str, Any]) -> object | None:
    if not ref.startswith("#/"):
        return None
    node: object = root
    for raw_part in ref[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def resolve_openapi_node(payload: object, root: dict[str, Any], seen: set[str] | None = None) -> object:
    if not isinstance(payload, dict):
        return payload
    ref = payload.get("$ref")
    if not isinstance(ref, str):
        return payload
    seen = seen or set()
    if ref in seen:
        return payload
    resolved = resolve_openapi_ref(ref, root)
    if resolved is None:
        return payload
    return resolve_openapi_node(resolved, root, seen | {ref})


def merge_schema(schema: object, root: dict[str, Any], seen: set[str] | None = None) -> dict[str, Any]:
    resolved = resolve_openapi_node(schema, root, seen)
    if not isinstance(resolved, dict):
        return {}
    merged = dict(resolved)
    for keyword in ("allOf", "oneOf", "anyOf"):
        children = merged.get(keyword)
        if not isinstance(children, list):
            continue
        combined_properties: dict[str, Any] = {}
        combined_required: list[str] = []
        for child in children:
            child_schema = merge_schema(child, root, seen)
            child_props = child_schema.get("properties")
            if isinstance(child_props, dict):
                combined_properties.update(child_props)
            child_required = child_schema.get("required")
            if isinstance(child_required, list):
                combined_required.extend(str(item) for item in child_required)
        if combined_properties:
            merged["properties"] = {**merged.get("properties", {}), **combined_properties}
        if combined_required:
            merged["required"] = sorted(set([*merged.get("required", []), *combined_required]))
    return merged


def schema_example(schema: dict[str, Any]) -> str | None:
    for key in ("example", "default"):
        if schema.get(key) is not None:
            return str(schema[key])
    examples = schema.get("examples")
    if isinstance(examples, list) and examples:
        return str(examples[0])
    return None


def schema_description(schema: dict[str, Any]) -> str | None:
    description = schema.get("description") or schema.get("title")
    return str(description) if description else None


def parse_openapi_parameter(payload: object, root: dict[str, Any]) -> Parameter | None:
    payload = resolve_openapi_node(payload, root)
    if not isinstance(payload, dict):
        return None
    name = payload.get("name")
    location = payload.get("in")
    if not isinstance(name, str) or not isinstance(location, str):
        return None
    schema = merge_schema(payload.get("schema", {}), root)
    example = payload.get("example") or schema_example(schema)
    return Parameter(
        name=name,
        location=location,
        required=bool(payload.get("required")),
        description=str(payload.get("description")) if payload.get("description") else None,
        example=str(example) if example is not None else None,
    )


def extract_request_body(request_body: dict, root: dict[str, Any]) -> list[Parameter]:
    request_body = resolve_openapi_node(request_body, root)
    if not isinstance(request_body, dict):
        return []
    content = request_body.get("content", {})
    if not isinstance(content, dict):
        return []
    results: list[Parameter] = []
    for media_type, payload in content.items():
        if not isinstance(payload, dict):
            continue
        schema = merge_schema(payload.get("schema", {}), root)
        if not schema:
            continue
        results.extend(schema_to_params(schema, root, f"body:{media_type}"))
    return unique_params(results)


def schema_to_params(schema: dict[str, Any], root: dict[str, Any], location: str, prefix: str = "") -> list[Parameter]:
    schema = merge_schema(schema, root)
    schema_type = schema.get("type")
    if schema_type == "array" and isinstance(schema.get("items"), dict):
        return schema_to_params(schema["items"], root, location, f"{prefix}[]" if prefix else "[]")
    props = schema.get("properties", {})
    if not isinstance(props, dict):
        return []
    required = set(str(item) for item in schema.get("required", []) if isinstance(item, str)) if isinstance(schema.get("required"), list) else set()
    results: list[Parameter] = []
    for name, prop in props.items():
        prop_schema = merge_schema(prop, root)
        if not prop_schema:
            continue
        full_name = f"{prefix}.{name}" if prefix else str(name)
        results.append(
            Parameter(
                name=full_name,
                location=location,
                required=str(name) in required,
                description=schema_description(prop_schema),
                example=schema_example(prop_schema),
            )
        )
        prop_type = prop_schema.get("type")
        if prop_type == "object" or isinstance(prop_schema.get("properties"), dict):
            results.extend(schema_to_params(prop_schema, root, location, full_name))
        elif prop_type == "array" and isinstance(prop_schema.get("items"), dict):
            item_schema = merge_schema(prop_schema["items"], root)
            if item_schema.get("type") == "object" or isinstance(item_schema.get("properties"), dict):
                results.extend(schema_to_params(item_schema, root, location, f"{full_name}[]"))
    return results


def add_parameter(endpoint: ApiEndpoint, param: Parameter) -> None:
    location = param.location.lower()
    if location == "header":
        endpoint.headers.append(param)
    elif location == "query":
        endpoint.query_params.append(param)
    elif location == "path":
        endpoint.path_params.append(param)
    else:
        endpoint.body_params.append(param)


def normalize_path(path: str) -> str:
    path = path.strip()
    if not path.startswith("/"):
        path = "/" + path
    path = re.sub(r":([A-Za-z0-9_]+)", r"{\1}", path)
    return re.sub(r"//+", "/", path)


def infer_description(method: str, path: str, comment: str | None) -> tuple[str, bool]:
    if comment:
        return (comment, False)
    parts = [segment for segment in path.strip("/").split("/") if segment]
    label = "root" if not parts else " ".join(part.replace("-", " ") for part in parts)
    return (f"Inferred: {method} {label}", True)


EXPRESS_PATTERN = re.compile(
    rf"(?P<prefix>\b(?:router|app)\.(?P<method>{METHOD_PATTERN})\s*\(\s*[\"'`](?P<path>[^\"'`]+)[\"'`])",
    re.IGNORECASE,
)
NEST_CONTROLLER_PATTERN = re.compile(r"@Controller\((?P<quote>[\"'`])(?P<path>.+?)(?P=quote)\)")
NEST_METHOD_PATTERN = re.compile(rf"@(?P<method>{METHOD_PATTERN.title()})\((?P<args>.*?)\)", re.IGNORECASE)
PYTHON_ROUTE_PATTERN = re.compile(
    rf"@(?P<router>[A-Za-z0-9_\.]+)\.(?P<method>{METHOD_PATTERN})\((?P<args>.*?)\)", re.IGNORECASE
)
LARAVEL_PATTERN = re.compile(
    rf"Route::(?P<method>{METHOD_PATTERN})\(\s*[\"'](?P<path>[^\"']+)[\"']",
    re.IGNORECASE,
)


def extract_comments(lines: list[str], idx: int) -> str | None:
    comments: list[str] = []
    cursor = idx - 1
    while cursor >= 0:
        stripped = lines[cursor].strip()
        if not stripped:
            if comments:
                break
            cursor -= 1
            continue
        if stripped.startswith(("//", "#", "*", "/*", "/**")):
            cleaned = re.sub(r"^(/\*\*?|//|#|\*)\s?", "", stripped).strip("*/ ").strip()
            if cleaned:
                comments.append(cleaned)
            cursor -= 1
            continue
        break
    if not comments:
        return None
    comments.reverse()
    return " ".join(comments)


def extract_headers_from_context(text: str) -> list[Parameter]:
    headers: list[Parameter] = []
    if re.search(r"Authorization|Bearer|authMiddleware|jwt|token", text, re.IGNORECASE):
        headers.append(Parameter(name="Authorization", location="header", required=False, description="Bearer token"))
    if re.search(r"X-Request-Id", text, re.IGNORECASE):
        headers.append(Parameter(name="X-Request-Id", location="header", required=False, description="Trace request id"))
    return headers


REQUEST_SOURCE_PATTERN = r"(?:req|request|ctx\.request|context\.request)"
REQ_QUERY_PATTERN = re.compile(rf"{REQUEST_SOURCE_PATTERN}\.query\.([A-Za-z_][A-Za-z0-9_]*)")
REQ_BODY_PATTERN = re.compile(rf"{REQUEST_SOURCE_PATTERN}\.body\.([A-Za-z_][A-Za-z0-9_]*)")
REQ_PARAM_PATTERN = re.compile(rf"{REQUEST_SOURCE_PATTERN}\.params\.([A-Za-z_][A-Za-z0-9_]*)")
BRACKET_ACCESS_PATTERN = re.compile(rf"{REQUEST_SOURCE_PATTERN}\.(body|query|params)\s*\[\s*[\"']([A-Za-z_][A-Za-z0-9_]*)[\"']\s*\]")
DESTRUCTURED_PATTERN = re.compile(
    rf"(?:const|let|var)\s*\{{(?P<fields>[^}}]+)\}}\s*=\s*{REQUEST_SOURCE_PATTERN}\.(?P<location>body|query|params)",
    re.MULTILINE,
)
NEST_PARAM_DECORATOR_PATTERN = re.compile(r"@(Body|Query|Param|Headers?)\(\s*(?:[\"'`]([^\"'`]+)[\"'`])?\s*\)(?:\s+[A-Za-z_][A-Za-z0-9_]*)?\s*(?::\s*([A-Za-z_][A-Za-z0-9_<>]*))?")
SCHEMA_PARSE_PATTERN = re.compile(rf"\b([A-Za-z_][A-Za-z0-9_]*)\.(?:parse|safeParse|validate)\s*\(\s*{REQUEST_SOURCE_PATTERN}\.body")
VALIDATOR_SCHEMA_PATTERN = re.compile(
    r"\b(?:validate(?:Body|Request|Schema)?|bodyValidator|schemaValidator|zValidator)\s*\([^)]*\b([A-Za-z_][A-Za-z0-9_]*)\b",
    re.IGNORECASE,
)
FASTAPI_FUNCTION_PATTERN = re.compile(r"\b(?:async\s+)?def\s+[A-Za-z_][A-Za-z0-9_]*\s*\((?P<args>.*?)\)\s*:", re.DOTALL)
ZOD_FIELD_PATTERN = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*:\s*z\.")
JOI_FIELD_PATTERN = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*:\s*Joi\.")
CLASS_VALIDATOR_PATTERN = re.compile(r"@(Is[A-Za-z]+|ApiProperty(?:Optional)?)\([^)]*\)\s*\n\s*([A-Za-z_][A-Za-z0-9_]*)")


def unique_params(params: list[Parameter]) -> list[Parameter]:
    seen: set[tuple[str, str]] = set()
    result: list[Parameter] = []
    for param in params:
        key = (param.location, param.name)
        if key in seen:
            continue
        seen.add(key)
        result.append(param)
    return result


def parse_destructured_fields(raw: str) -> list[str]:
    fields: list[str] = []
    for item in split_top_level_commas(raw):
        cleaned = item.strip()
        if not cleaned or cleaned.startswith("..."):
            continue
        cleaned = cleaned.split("=", 1)[0].strip()
        cleaned = cleaned.split(":", 1)[0].strip()
        cleaned = cleaned.strip("'\"`")
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", cleaned):
            fields.append(cleaned)
    return fields


def split_top_level_commas(raw: str) -> list[str]:
    parts: list[str] = []
    start = 0
    depth = 0
    quote: str | None = None
    for idx, char in enumerate(raw):
        if quote:
            if char == quote and raw[idx - 1 : idx] != "\\":
                quote = None
            continue
        if char in ("'", '"', "`"):
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth = max(0, depth - 1)
        elif char == "," and depth == 0:
            parts.append(raw[start:idx])
            start = idx + 1
    parts.append(raw[start:])
    return parts


def extract_ts_schema_index(text: str) -> dict[str, list[Parameter]]:
    schemas: dict[str, list[Parameter]] = {}
    for match in re.finditer(r"\b(?:export\s+)?class\s+([A-Za-z_][A-Za-z0-9_]*)\b", text):
        name = match.group(1)
        body = extract_balanced_block(text, match.end(), "{", "}")
        if not body:
            continue
        fields = set()
        for field_match in re.finditer(r"(?:public\s+|private\s+|protected\s+|readonly\s+)?([A-Za-z_][A-Za-z0-9_]*)[?!]?\s*[:=]", body):
            field = field_match.group(1)
            if field not in {"constructor", "if", "for", "while", "switch", "return"}:
                fields.add(field)
        if fields:
            schemas[name] = [
                Parameter(name=field, location="body:json", required=False, description=f"Inferred body field `{field}` from `{name}`")
                for field in sorted(fields)
            ]
    python_class_pattern = re.compile(r"^class\s+([A-Za-z_][A-Za-z0-9_]*)\([^)]*\):\s*$", re.MULTILINE)
    for match in python_class_pattern.finditer(text):
        name = match.group(1)
        body = extract_python_class_body(text, match.end())
        fields = set(re.findall(r"^\s{4,}([A-Za-z_][A-Za-z0-9_]*)\s*:", body, re.MULTILINE))
        if fields:
            schemas[name] = [
                Parameter(name=field, location="body:json", required=False, description=f"Inferred body field `{field}` from `{name}`")
                for field in sorted(fields)
            ]
    for match in re.finditer(r"\b(?:export\s+)?(?:const|let|var)\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:z|Joi)\.object\s*\(", text):
        name = match.group(1)
        body = extract_balanced_block(text, match.end() - 1, "(", ")")
        if not body:
            continue
        fields = set(ZOD_FIELD_PATTERN.findall(body))
        fields.update(JOI_FIELD_PATTERN.findall(body))
        if fields:
            schemas[name] = [
                Parameter(name=field, location="body:json", required=False, description=f"Inferred body field `{field}` from `{name}`")
                for field in sorted(fields)
            ]
    return schemas


def extract_python_class_body(text: str, start_idx: int) -> str:
    lines = text[start_idx:].splitlines()
    body: list[str] = []
    for line in lines:
        if not line.strip():
            body.append(line)
            continue
        if not line.startswith((" ", "\t")):
            break
        body.append(line)
    return "\n".join(body)


def extract_balanced_block(text: str, start_idx: int, open_char: str, close_char: str) -> str | None:
    start = text.find(open_char, start_idx)
    if start == -1:
        return None
    depth = 0
    quote: str | None = None
    for idx in range(start, len(text)):
        char = text[idx]
        if quote:
            if char == quote and text[idx - 1 : idx] != "\\":
                quote = None
            continue
        if char in ("'", '"', "`"):
            quote = char
        elif char == open_char:
            depth += 1
        elif char == close_char:
            depth -= 1
            if depth == 0:
                return text[start + 1 : idx]
    return None


def merge_schema_indexes(paths: Iterable[Path]) -> dict[str, list[Parameter]]:
    index: dict[str, list[Parameter]] = {}
    for path in paths:
        if path.suffix.lower() not in {".js", ".jsx", ".ts", ".tsx", ".py"}:
            continue
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue
        for name, params in extract_ts_schema_index(text).items():
            index.setdefault(name, params)
    return index


def infer_params_from_context(
    text: str, schema_index: dict[str, list[Parameter]] | None = None, default_body_schema_names: Iterable[str] = ()
) -> tuple[list[Parameter], list[Parameter], list[Parameter], list[Parameter]]:
    query_names = set(REQ_QUERY_PATTERN.findall(text))
    body_names = set(REQ_BODY_PATTERN.findall(text))
    path_names = set(REQ_PARAM_PATTERN.findall(text))
    header_names = set(re.findall(rf"{REQUEST_SOURCE_PATTERN}\.header\s*\(\s*[\"']([A-Za-z0-9\-_]+)[\"']", text))
    header_names.update(re.findall(rf"{REQUEST_SOURCE_PATTERN}\.headers\s*\[\s*[\"']([A-Za-z0-9\-_]+)[\"']\s*\]", text))
    extra_headers: list[Parameter] = []
    extra_query_params: list[Parameter] = []
    extra_path_params: list[Parameter] = []
    extra_body_params: list[Parameter] = []

    for location, name in BRACKET_ACCESS_PATTERN.findall(text):
        if location == "query":
            query_names.add(name)
        elif location == "params":
            path_names.add(name)
        else:
            body_names.add(name)
    for match in DESTRUCTURED_PATTERN.finditer(text):
        fields = parse_destructured_fields(match.group("fields"))
        if match.group("location") == "query":
            query_names.update(fields)
        elif match.group("location") == "params":
            path_names.update(fields)
        else:
            body_names.update(fields)
    body_names.update(ZOD_FIELD_PATTERN.findall(text))
    body_names.update(JOI_FIELD_PATTERN.findall(text))
    body_names.update(field for _, field in CLASS_VALIDATOR_PATTERN.findall(text))

    if schema_index:
        referenced_schema_names = [
            *SCHEMA_PARSE_PATTERN.findall(text),
            *VALIDATOR_SCHEMA_PATTERN.findall(text),
            *default_body_schema_names,
        ]
        for schema_name in referenced_schema_names:
            for param in schema_index.get(schema_name, []):
                extra_body_params.append(param)
    for decorator, explicit_name, type_name in NEST_PARAM_DECORATOR_PATTERN.findall(text):
        decorator_lower = decorator.lower()
        if explicit_name:
            if decorator_lower.startswith("header"):
                header_names.add(explicit_name)
            elif decorator_lower == "query":
                query_names.add(explicit_name)
            elif decorator_lower == "param":
                path_names.add(explicit_name)
            else:
                body_names.add(explicit_name)
        elif schema_index and type_name:
            params = schema_index.get(type_name.replace("[]", ""))
            if params:
                if decorator_lower == "query":
                    extra_query_params.extend(
                        Parameter(name=param.name, location="query", required=param.required, description=param.description, example=param.example)
                        for param in params
                    )
                elif decorator_lower == "param":
                    extra_path_params.extend(
                        Parameter(name=param.name, location="path", required=True, description=param.description, example=param.example)
                        for param in params
                    )
                elif decorator_lower == "body":
                    extra_body_params.extend(params)
    for fastapi_match in FASTAPI_FUNCTION_PATTERN.finditer(text):
        fastapi_headers, fastapi_queries, fastapi_paths, fastapi_bodies = infer_fastapi_params(fastapi_match.group("args"), schema_index or {})
        extra_headers.extend(fastapi_headers)
        extra_query_params.extend(fastapi_queries)
        extra_path_params.extend(fastapi_paths)
        extra_body_params.extend(fastapi_bodies)

    headers = [
        Parameter(name=name, location="header", required=False, description=f"Inferred header `{name}`")
        for name in sorted(header_names)
    ]
    query_params = [
        Parameter(name=name, location="query", required=False, description=f"Inferred query parameter `{name}`")
        for name in sorted(query_names)
    ]
    path_params = [
        Parameter(name=name, location="path", required=True, description=f"Inferred path parameter `{name}`")
        for name in sorted(path_names)
    ]
    body_params = [
        Parameter(name=name, location="body:json", required=False, description=f"Inferred body field `{name}`")
        for name in sorted(body_names)
    ]
    return (
        unique_params(headers + extra_headers),
        unique_params(query_params + extra_query_params),
        unique_params(path_params + extra_path_params),
        unique_params(body_params + extra_body_params),
    )


def infer_fastapi_params(args: str, schema_index: dict[str, list[Parameter]]) -> tuple[list[Parameter], list[Parameter], list[Parameter], list[Parameter]]:
    headers: list[Parameter] = []
    queries: list[Parameter] = []
    paths: list[Parameter] = []
    bodies: list[Parameter] = []
    for arg in split_top_level_commas(args):
        if ":" not in arg:
            continue
        name_part, rest = arg.split(":", 1)
        name = name_part.strip()
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
            continue
        annotation = rest.split("=", 1)[0].strip()
        default = rest.split("=", 1)[1] if "=" in rest else ""
        if "Header(" in default:
            headers.append(
                Parameter(name=name.replace("_", "-"), location="header", required=fastapi_default_required(default), description=f"Inferred header `{name}`")
            )
        elif "Path(" in default:
            paths.append(Parameter(name=name, location="path", required=True, description=f"Inferred path parameter `{name}`"))
        elif "Body(" in default:
            bodies.extend(schema_index.get(annotation, []))
            if annotation not in schema_index:
                bodies.append(
                    Parameter(name=name, location="body:json", required=fastapi_default_required(default), description=f"Inferred body field `{name}`")
                )
        elif "Query(" in default or not default:
            if annotation in schema_index:
                bodies.extend(schema_index[annotation])
            else:
                queries.append(
                    Parameter(name=name, location="query", required=fastapi_default_required(default), description=f"Inferred query parameter `{name}`")
                )
    return headers, queries, paths, bodies


def fastapi_default_required(default: str) -> bool:
    if not default.strip():
        return True
    return bool(re.search(r"\(\s*(?:\.\.\.|Required)\b", default))


def route_context(lines: list[str], start_idx: int) -> str:
    chunk = [lines[start_idx]]
    for cursor in range(start_idx + 1, min(len(lines), start_idx + 40)):
        line = lines[cursor]
        stripped = line.strip()
        if cursor > start_idx and (
            EXPRESS_PATTERN.search(line)
            or PYTHON_ROUTE_PATTERN.search(line)
            or LARAVEL_PATTERN.search(line)
            or "@Get" in stripped
            or "@Post" in stripped
            or "@Patch" in stripped
            or "@Delete" in stripped
            or "@Put" in stripped
        ):
            break
        chunk.append(line)
    return "\n".join(chunk)


def route_endpoints(path: Path, schema_index: dict[str, list[Parameter]] | None = None) -> list[ApiEndpoint]:
    text = path.read_text()
    lines = text.splitlines()
    endpoints: list[ApiEndpoint] = []

    controller_prefix = ""
    controller_match = NEST_CONTROLLER_PATTERN.search(text)
    if controller_match:
        controller_prefix = controller_match.group("path")
    for idx, line in enumerate(lines):
        express_match = EXPRESS_PATTERN.search(line)
        if express_match:
            method = express_match.group("method").upper()
            raw_path = express_match.group("path")
            comment = extract_comments(lines, idx)
            local_context = route_context(lines, idx)
            inferred_headers, inferred_query, inferred_path, inferred_body = infer_params_from_context(local_context, schema_index)
            description, inferred = infer_description(method, raw_path, comment)
            endpoint = ApiEndpoint(
                method=method,
                path=normalize_path(raw_path),
                source=str(path),
                description=description,
                inferred_description=inferred,
                headers=unique_params(extract_headers_from_context(text) + inferred_headers),
                query_params=unique_params(list(inferred_query)),
                path_params=unique_params(path_parameters(raw_path) + inferred_path),
                body_params=unique_params(list(inferred_body)),
                tags=derive_tags(raw_path, path),
            )
            endpoints.append(endpoint)
            continue

        python_match = PYTHON_ROUTE_PATTERN.search(line)
        if python_match:
            method = python_match.group("method").upper()
            raw_path = extract_first_string(python_match.group("args")) or "/"
            comment = extract_comments(lines, idx)
            local_context = route_context(lines, idx)
            inferred_headers, inferred_query, inferred_path, inferred_body = infer_params_from_context(local_context, schema_index)
            description, inferred = infer_description(method, raw_path, comment)
            endpoint = ApiEndpoint(
                method=method,
                path=normalize_path(raw_path),
                source=str(path),
                description=description,
                inferred_description=inferred,
                headers=unique_params(extract_headers_from_context(text) + inferred_headers),
                query_params=unique_params(list(inferred_query)),
                path_params=unique_params(path_parameters(raw_path) + inferred_path),
                body_params=unique_params(list(inferred_body)),
                tags=derive_tags(raw_path, path),
            )
            endpoints.append(endpoint)
            continue

        laravel_match = LARAVEL_PATTERN.search(line)
        if laravel_match:
            method = laravel_match.group("method").upper()
            raw_path = laravel_match.group("path")
            comment = extract_comments(lines, idx)
            local_context = route_context(lines, idx)
            inferred_headers, inferred_query, inferred_path, inferred_body = infer_params_from_context(local_context, schema_index)
            description, inferred = infer_description(method, raw_path, comment)
            endpoint = ApiEndpoint(
                method=method,
                path=normalize_path(raw_path),
                source=str(path),
                description=description,
                inferred_description=inferred,
                headers=unique_params(extract_headers_from_context(text) + inferred_headers),
                query_params=unique_params(list(inferred_query)),
                path_params=unique_params(path_parameters(raw_path) + inferred_path),
                body_params=unique_params(list(inferred_body)),
                tags=derive_tags(raw_path, path),
            )
            endpoints.append(endpoint)
            continue

        if "@Controller" in line or "@Get" in line or "@Post" in line or "@Patch" in line or "@Delete" in line:
            nest_method = NEST_METHOD_PATTERN.search(line)
            if nest_method:
                method = nest_method.group("method").upper()
                raw_path = extract_first_string(nest_method.group("args")) or ""
                full_path = normalize_path("/".join(part.strip("/") for part in (controller_prefix, raw_path) if part))
                comment = extract_comments(lines, idx)
                local_context = route_context(lines, idx)
                default_schema_names = re.findall(r"@Body\(\s*\)\s+[A-Za-z_][A-Za-z0-9_]*\s*:\s*([A-Za-z_][A-Za-z0-9_]*)", local_context)
                inferred_headers, inferred_query, inferred_path, inferred_body = infer_params_from_context(local_context, schema_index, default_schema_names)
                description, inferred = infer_description(method, full_path, comment)
                endpoint = ApiEndpoint(
                    method=method,
                    path=full_path,
                    source=str(path),
                    description=description,
                    inferred_description=inferred,
                    headers=unique_params(extract_headers_from_context(text) + inferred_headers),
                    query_params=unique_params(list(inferred_query)),
                    path_params=unique_params(path_parameters(full_path) + inferred_path),
                    body_params=unique_params(list(inferred_body)),
                    tags=derive_tags(full_path, path),
                )
                endpoints.append(endpoint)
    return endpoints


def extract_first_string(raw: str) -> str | None:
    match = re.search(r"[\"'`](.*?)[\"'`]", raw)
    return match.group(1) if match else None


def path_parameters(path: str) -> list[Parameter]:
    names = re.findall(r"{([^}]+)}|:([A-Za-z0-9_]+)", path)
    params = []
    for first, second in names:
        name = first or second
        params.append(Parameter(name=name, location="path", required=True, description=f"Path parameter `{name}`"))
    return params


def derive_tags(path_text: str, source: Path) -> list[str]:
    first_segment = next((segment for segment in path_text.strip("/").split("/") if segment and not segment.startswith("{")), None)
    if first_segment:
        return [first_segment]
    return [source.parent.name]


def merge_endpoints(existing: ApiEndpoint, incoming: ApiEndpoint) -> ApiEndpoint:
    if existing.inferred_description and not incoming.inferred_description:
        primary, secondary = incoming, existing
    else:
        primary, secondary = existing, incoming
    return ApiEndpoint(
        method=primary.method,
        path=primary.path,
        source=primary.source,
        description=primary.description,
        inferred_description=primary.inferred_description,
        tags=sorted(set(primary.tags + secondary.tags)),
        headers=unique_params(primary.headers + secondary.headers),
        query_params=unique_params(primary.query_params + secondary.query_params),
        path_params=unique_params(primary.path_params + secondary.path_params),
        body_params=unique_params(primary.body_params + secondary.body_params),
    )


def dedupe(endpoints: list[ApiEndpoint]) -> list[ApiEndpoint]:
    selected: dict[str, ApiEndpoint] = {}
    for endpoint in endpoints:
        existing = selected.get(endpoint.signature)
        if not existing:
            selected[endpoint.signature] = endpoint
            continue
        selected[endpoint.signature] = merge_endpoints(existing, endpoint)
    return list(selected.values())


def discover_from_paths(candidate_paths: list[Path], schema_paths: list[Path], warnings: list[str]) -> list[ApiEndpoint]:
    schema_index = merge_schema_indexes(schema_paths)
    endpoints: list[ApiEndpoint] = []
    for path in candidate_paths:
        if likely_spec_file(path):
            endpoints.extend(openapi_endpoints(path, warnings))
    for path in candidate_paths:
        if path.suffix.lower() in ROUTE_EXTS:
            endpoints.extend(route_endpoints(path, schema_index))
    return endpoints


def build_output(
    repo: Path,
    endpoints: list[ApiEndpoint],
    mode: str,
    changed_files: list[str],
    env: dict[str, str],
    candidate_paths: list[Path],
    scan_scope: str,
    fallback_reason: str | None,
    warnings: list[str],
) -> dict:
    collections = []
    pattern = re.compile(r"^POSTMAN_COLLECTION_ID_(.+)$")
    for key, value in env.items():
        match = pattern.match(key)
        if not match or key.endswith("_HOST"):
            continue
        suffix = match.group(1)
        collections.append(
            {
                "key": suffix,
                "collection_id": value,
                "host": env.get(f"POSTMAN_COLLECTION_ID_{suffix}_HOST"),
            }
        )
    return {
        "repo": str(repo),
        "mode": mode,
        "scan_scope": scan_scope,
        "fallback_reason": fallback_reason,
        "changed_files": changed_files,
        "candidate_files": [str(path.relative_to(repo)) if path.is_relative_to(repo) else str(path) for path in candidate_paths],
        "workspace_id": env.get("POSTMAN_WORKSPACE_ID"),
        "collections": collections,
        "warnings": sorted(set(warnings)),
        "api_count": len(endpoints),
        "apis": [asdict(endpoint) for endpoint in sorted(endpoints, key=lambda item: item.signature)],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".", help="target repository path")
    parser.add_argument("--mode", choices=("full", "incremental"), default="incremental")
    parser.add_argument("--include", action="append", default=[], help="substring filter for routes/modules/files")
    parser.add_argument("--env-file", help="explicit env file to load")
    parser.add_argument(
        "--allow-full-fallback",
        action="store_true",
        help="allow incremental discovery to widen to a full scan when Git diff evidence is weak",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    env = load_env_file(repo, Path(args.env_file).resolve() if args.env_file else None)
    changed_files = git_changed_files(repo) if args.mode == "incremental" else []
    warnings: list[str] = []

    scan_scope = "incremental" if args.mode == "incremental" else "full"
    fallback_reason = None

    schema_paths = list(iter_text_files(repo, None, args.include))
    if args.mode == "incremental" and args.include:
        candidate_paths = list(iter_text_files(repo, None, args.include))
        scan_scope = "explicit-include"
    elif args.mode == "incremental":
        candidate_paths = list(iter_text_files(repo, changed_files if changed_files else [], args.include))
        if not candidate_paths:
            fallback_reason = "Git diff did not provide changed text API files; no full scan was run without --allow-full-fallback."
            if args.allow_full_fallback:
                candidate_paths = schema_paths
                scan_scope = "full-fallback"
                fallback_reason = "Git diff did not provide changed text API files, so --allow-full-fallback widened discovery to the requested scope."
    else:
        candidate_paths = schema_paths

    endpoints = discover_from_paths(candidate_paths, schema_paths, warnings)
    if args.mode == "incremental" and candidate_paths and not endpoints and set(candidate_paths) != set(schema_paths):
        candidate_paths = schema_paths
        fallback_reason = "Changed files did not contain discoverable API route/spec evidence; no full scan was run without --allow-full-fallback."
        if args.allow_full_fallback:
            scan_scope = "full-fallback"
            fallback_reason = "Changed files did not contain discoverable API route/spec evidence, so --allow-full-fallback widened discovery to the requested scope."
            endpoints = discover_from_paths(candidate_paths, schema_paths, warnings)
        else:
            candidate_paths = []

    output = build_output(repo, dedupe(endpoints), args.mode, changed_files, env, candidate_paths, scan_scope, fallback_reason, warnings)
    if args.json:
        print(json.dumps(output, indent=2, ensure_ascii=False))
    else:
        print(f"Discovered {output['api_count']} APIs from {output['repo']}")
        for api in output["apis"][:20]:
            print(f"- {api['method']} {api['path']} ({api['source']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
