# Postman MCP detection

## Goal

Find the Postman integration that is usable from the current AI host, not just any Postman config on disk.

## Host priority

Check the host running the skill first:

- Codex: current session MCP exposure, then `~/.codex/config.toml`
- Cursor: `~/.cursor/mcp.json`
- Claude: common Claude local config files such as `.claude.json` or desktop config files

Only inspect another tool's config as a fallback when the current host cannot provide a usable Postman route.

## What counts as a Postman MCP entry

Treat an entry as Postman-related when:

- the server name contains `postman`
- or the URL contains `postman.com`
- or the command / args mention a Postman MCP package or binary

Extract these fields when available:

- server name
- transport type
- URL or command
- auth header env var
- inline bearer/header auth presence

Do not print full secrets in normal output. Mask tokens unless the user explicitly asks for the raw value.

## Fallback auth order

When MCP is not usable for the actual push:

1. prefer `POSTMAN_API_KEY`
2. only reuse current-host MCP auth when it is clearly a Postman API key, not just any bearer token
3. if neither exists, stop and tell the user what is missing
