---
name: postman-push
description: Detect the current AI host's Postman MCP configuration, discover APIs from the active project, and push full or incremental API documentation into the configured Postman workspace and collection. Use this whenever the user wants to sync routes, controllers, OpenAPI specs, or recent API changes to Postman from Codex, Cursor, Claude, or similar tools, even if they only mention "update Postman docs", "sync collection", or "push APIs to Postman".
---

# Postman Push

## Overview

Push project APIs into Postman with the least manual bookkeeping possible:

1. Detect how the current host tool exposes Postman MCP.
2. Read the project's `.env` and find Postman workspace / collection configuration.
3. Discover APIs from OpenAPI specs first, then from framework routes as a fallback.
   - OpenAPI discovery should resolve local `$ref`, path-level parameters, composed schemas, and nested request body fields.
   - Route discovery should use referenced DTOs / validators / schema models when available, not only fields directly accessed inside the handler.
4. Default to incremental sync from Git diff; switch to full sync when the user asks or incremental detection is too weak.
5. Update the matching Postman collection, using the host MCP directly when the session exposes it and using the helper script for Postman HTTP API fallback.

## Triggering Scenarios

Use this skill when the user asks to:

- push or sync APIs to Postman
- update a Postman collection from current routes
- sync only recently changed APIs
- generate Postman requests from project routes/controllers
- map internal vs external APIs into different Postman collections
- refresh Postman docs after API code changes

## Required Inputs

Expect the target project to expose at least:

- `POSTMAN_WORKSPACE_ID`
- one or more `POSTMAN_COLLECTION_ID_*`
- matching `POSTMAN_COLLECTION_ID_*_HOST`

Optional fallback auth:

- `POSTMAN_API_KEY`

## Collection Mapping Rules

Determine the target collection in this order:

1. If the user explicitly names a collection key, use it.
2. If the API set is clearly external/public, prefer keys containing `open`, `public`, or `external`.
3. If the API set is clearly internal/admin/private, prefer keys containing `internal`, `admin`, or `private`.
4. If no strong match exists, prefer the first open/public collection; otherwise use the first configured collection and state that the choice was heuristic.

Treat matching `POSTMAN_COLLECTION_ID_*_HOST` as the base host for APIs routed into that collection.

## Required Workflow

### 1. Detect Postman MCP for the current host

Run:

```bash
python3 scripts/detect_postman_config.py --json
```

Detection priority:

1. current session / host-integrated MCP if directly available
2. current host's config files
3. environment variables

Use the current host as the source of truth. Do not blindly reuse another tool's config unless the current host cannot provide a usable Postman path and you clearly state the fallback.

### 2. Read Postman env config from the project

Inspect the project's `.env`, `.env.local`, `.env.development`, or user-specified env file for:

- `POSTMAN_WORKSPACE_ID`
- `POSTMAN_COLLECTION_ID_*`
- `POSTMAN_COLLECTION_ID_*_HOST`
- `POSTMAN_API_KEY` if API fallback may be needed

### 3. Discover APIs

Prefer structured sources in this order:

1. OpenAPI / Swagger JSON or YAML
2. framework route definitions
3. handler/controller comments and validation schemas

Use the discovery script:

```bash
python3 scripts/discover_apis.py --repo "$PWD" --mode incremental --json
```

For full sync:

```bash
python3 scripts/discover_apis.py --repo "$PWD" --mode full --json
```

If the user specifies modules, route files, or path prefixes, pass them through with `--include`.

### 4. Push to Postman

Run:

```bash
python3 scripts/postman_push.py \
  --repo "$PWD" \
  --mode incremental \
  --host-tool auto
```
Behavior:

- If the current session already exposes a Postman MCP tool, use that MCP tool directly for the actual write.
- Use `scripts/postman_push.py` as the Postman HTTP API fallback helper.
- Do not treat arbitrary MCP bearer tokens as interchangeable with Postman API keys.
- Only update requests whose normalized signature matches the same `METHOD + path`.
- Treat `:id`, `{id}`, and `{{id}}` as the same path parameter form for matching.
- Append new requests after existing collection content; preserve unrelated existing requests, saved responses, and collection-wide metadata.
- Keep descriptions, parameter explanations, and auth headers grounded in project evidence when available.
- Mark inferred descriptions as inferred if the project does not provide enough metadata.

## Documentation Rules

For every API pushed, include:

- method
- normalized path
- host/base URL from the selected collection host
- query params
- path params
- headers
- body fields when discoverable
- short endpoint description
- parameter descriptions

Prioritize real project sources:

- OpenAPI `summary`, `description`, `parameters`, `requestBody`
- route comments
- validator/schema descriptions
- auth middleware or client wrapper defaults

If metadata is missing, infer briefly and label the description as inferred in the generated text.

## Incremental Sync Rules

Default incremental logic:

1. use explicitly requested files/modules/routes if provided
2. otherwise use Git diff to identify changed API-relevant files
3. if Git does not yield enough route/spec evidence, fall back to full scan and say so

API-relevant files include:

- route/router files
- controller/handler files
- request/validator/schema/DTO files
- OpenAPI spec files

## Scripts

- `scripts/detect_postman_config.py` — detect Postman MCP and reusable credentials from the current host
- `scripts/discover_apis.py` — discover APIs and emit a normalized JSON document
- `scripts/postman_push.py` — merge discovered APIs into the selected Postman collection

## References

- `references/mcp-detection.md` — host-specific MCP detection strategy
- `references/api-discovery.md` — API discovery order, heuristics, and limits

## Cursor MCP Direct Mode (No Local Scripts)

When the repository does **not** contain `scripts/detect_postman_config.py`, `scripts/discover_apis.py`, or `scripts/postman_push.py`, switch to this direct workflow instead of failing:

1. Read project env (`.env*`) and resolve:
   - `POSTMAN_WORKSPACE_ID`
   - target `POSTMAN_COLLECTION_ID_*`
   - matching `POSTMAN_COLLECTION_ID_*_HOST`
2. Prefer `openapi/openapi.yaml` (or other OpenAPI files) as the source of truth.
3. Use Postman MCP tools directly:
   - `getCollections` to verify target collection
   - `createCollectionRequest` / `updateCollectionRequest` to upsert endpoints
4. Request naming and URL normalization rules:
   - request name: OpenAPI `summary` (fallback: `METHOD path`)
   - URL: `<COLLECTION_HOST_VAR>/<normalized_path>`
   - path params: `{id}` -> `{{id}}`
   - include auth header (`Authorization: Bearer {{token}}`) when endpoint is secured
5. Include metadata in each request (as available):
   - method, path, query params, headers, body example, concise description

### Incremental behavior in Direct Mode

- If git history/diff is available and trustworthy, sync changed endpoints only.
- If repository has no commits, dirty bootstrap state, or weak route evidence, do full sync and explicitly state fallback reason.

### Cleanup rule after generated artifacts

If a temporary Spec/Collection is created for conversion:

1. Attempt to remove temp artifacts in the same run.
2. If current MCP toolset does not expose delete capabilities, report this clearly and provide exact IDs for manual cleanup.
3. Always tag temporary assets with a clear prefix, e.g. `tmp-sync-<project>-<date>`, so they are easy to find and delete.
