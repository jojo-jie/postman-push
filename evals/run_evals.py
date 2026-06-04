#!/usr/bin/env python3
"""Small executable regression checks for the postman-push skill."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_REPO_SOURCE = ROOT / "evals" / "fixtures" / "sample-api"


@contextmanager
def fixture_repo():
    with tempfile.TemporaryDirectory(prefix="postman-push-eval-") as tmp:
        target = Path(tmp) / "sample-api"
        shutil.copytree(FIXTURE_REPO_SOURCE, target)
        yield target


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"Could not load module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def run_json(*args: str) -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, *args, "--json"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def run_command_json(*args: str) -> dict[str, Any]:
    env = dict(os.environ)
    env.pop("POSTMAN_API_KEY", None)
    completed = subprocess.run(
        [sys.executable, *args],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def assert_default_incremental_is_conservative() -> None:
    with fixture_repo() as repo:
        result = run_json("scripts/discover_apis.py", "--repo", str(repo), "--mode", "incremental")
    assert result["api_count"] == 0, result
    assert result["scan_scope"] == "incremental", result
    assert "without --allow-full-fallback" in result["fallback_reason"], result


def assert_full_fallback_discovers_fixture_routes() -> None:
    with fixture_repo() as repo:
        result = run_json(
            "scripts/discover_apis.py",
            "--repo",
            str(repo),
            "--mode",
            "incremental",
            "--allow-full-fallback",
        )
    signatures = {f"{api['method']} {api['path']}" for api in result["apis"]}
    assert result["scan_scope"] == "full-fallback", result
    assert {"GET /users/{id}", "POST /users", "GET /admin/audit"} <= signatures, result

    create_user = next(api for api in result["apis"] if api["method"] == "POST" and api["path"] == "/users")
    body_names = {param["name"] for param in create_user["body_params"]}
    assert {"email", "profile", "profile.displayName"} <= body_names, create_user


def assert_postman_merge_updates_and_preserves_examples() -> None:
    with fixture_repo() as repo:
        discover = run_json(
            "scripts/discover_apis.py",
            "--repo",
            str(repo),
            "--mode",
            "incremental",
            "--include",
            "openapi",
        )
    postman_push = load_module("postman_push_eval", ROOT / "scripts" / "postman_push.py")
    collection = json.loads((ROOT / "evals" / "fixtures" / "collections" / "existing-open.json").read_text())
    merged = postman_push.merge_collection(collection, discover["apis"], "https://api.example.test")

    users_folder = next(item for item in merged["item"] if item["name"] == "Users")
    get_user = next(item for item in users_folder["item"] if item["request"]["method"] == "GET")
    assert get_user["response"][0]["name"] == "Saved example", get_user
    assert "Get a user" in get_user["request"]["description"], get_user

    new_folders = [item for item in merged["item"] if item["name"] == "users"]
    assert new_folders, merged
    assert any(item["request"]["method"] == "POST" for item in new_folders[0]["item"]), merged


def assert_push_env_parser_and_target_host_validation_are_consistent() -> None:
    with fixture_repo() as repo:
        (repo / ".env").write_text(
            "\n".join(
                [
                    "export API_HOST=https://api.env.test",
                    "POSTMAN_WORKSPACE_ID=workspace-fixture",
                    "POSTMAN_COLLECTION_ID_OPEN=collection-open",
                    "POSTMAN_COLLECTION_ID_OPEN_HOST=$API_HOST # expanded by shared parser",
                    "POSTMAN_COLLECTION_ID_INTERNAL=collection-internal",
                    "",
                ]
            )
        )
        result = run_command_json("scripts/postman_push.py", "--repo", str(repo), "--mode", "incremental", "--include", "openapi")
    assert result["result"] == "dry-run", result
    assert result["collection_key"] == "OPEN", result
    assert result["host"] == "https://api.env.test", result


def assert_preview_without_api_key_does_not_fetch_remote() -> None:
    with fixture_repo() as repo:
        result = run_command_json(
            "scripts/postman_push.py",
            "--repo",
            str(repo),
            "--mode",
            "incremental",
            "--include",
            "openapi",
            "--preview",
        )
    assert result["result"] == "preview", result
    assert result["auth_source"] == "missing", result
    assert "not fetched" in result["warning"], result


def main() -> int:
    checks = [
        assert_default_incremental_is_conservative,
        assert_full_fallback_discovers_fixture_routes,
        assert_postman_merge_updates_and_preserves_examples,
        assert_push_env_parser_and_target_host_validation_are_consistent,
        assert_preview_without_api_key_does_not_fetch_remote,
    ]
    for check in checks:
        check()
        print(f"ok - {check.__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
