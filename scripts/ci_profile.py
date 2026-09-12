#!/usr/bin/env python3
"""Small, fail-closed helper for ArchBro's tiered GitHub Actions CI.

The workflow intentionally keeps only six coarse profiles.  This helper owns
path classification, the corresponding pytest target sets, dependency
preflights, and the stable aggregate gate so those rules can be exercised
locally without duplicating them in YAML.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import sys
from typing import Iterable


GROUPS = ("smoke", "frontend", "backend-core", "persistence", "integrations", "release")
SELECTIVE_PR_BASES = {"dev", "dev2"}

GLOBAL_SENSITIVE_EXACT = {
    ".dockerignore",
    "Dockerfile",
    "pyproject.toml",
    "scripts/ci_profile.py",
    "tests/conftest.py",
    "src/archbro/backend/core/contracts.py",
    "src/archbro/backend/core/repository.py",
    "src/archbro/platform/runtime/app.py",
}

GLOBAL_SENSITIVE_PREFIXES = (
    ".github/actions/",
    ".github/workflows/",
    "scripts/ci_",
)

# Patterns stay deliberately coarse.  A changed test file is also added
# directly when it still exists, so new tests are not lost behind this table.
PYTEST_PATTERNS = {
    "smoke": (
        "tests/test_runtime_health.py",
        "tests/test_ui_shell_routing.py",
        "tests/test_github_change_contract.py",
        "tests/test_firebase_browser_auth.py",
        "tests/test_ci_profile.py",
    ),
    "frontend": (
        "tests/test_canvas_*.py",
        "tests/test_diagram_*.py",
        "tests/test_*_ui.py",
        "tests/test_ui_*.py",
        "tests/test_project_bindings_ui.py",
        "tests/test_firebase_browser_auth.py",
        "tests/test_firebase_csp.py",
        "tests/test_firebase_github_auth.py",
        "tests/test_agent_context_mcp_gateway.py",
        "tests/test_api_contract.py",
        "tests/test_provider_oauth.py",
        "tests/test_webmcp_*.py",
    ),
    "backend-core": (
        "tests/test_acceptance_reconciliation.py",
        "tests/test_agent_*.py",
        "tests/test_api_contract.py",
        "tests/test_canvas_*.py",
        "tests/test_code_architecture.py",
        "tests/test_context_*.py",
        "tests/test_diagram_*.py",
        "tests/test_drift_*.py",
        "tests/test_gemini_fallback.py",
        "tests/test_google_genai_client.py",
        "tests/test_hierarchical_planner.py",
        "tests/test_initial_relationship_reconciliation.py",
        "tests/test_observation_trace.py",
        "tests/test_connector_sync.py",
        "tests/test_pipeline_runner.py",
        "tests/test_project_authorization.py",
        "tests/test_project_repository_*.py",
        "tests/test_runtime_health.py",
        "tests/test_signal_pipeline.py",
        "tests/test_sync_cursor.py",
        "tests/test_walking_skeleton.py",
    ),
    "persistence": (
        "tests/test_postgres_foundation.py",
        "tests/test_*repository.py",
        "tests/test_project_repository_*.py",
        "tests/test_secret_cipher.py",
        "tests/test_sqlite_migration.py",
        "tests/test_sync_cursor.py",
        "tests/test_slack_event_inbox.py",
        "tests/test_slack_event_recovery.py",
    ),
    "integrations": (
        "tests/test_agent_context_mcp_gateway.py",
        "tests/test_connector_*.py",
        "tests/test_firebase_*.py",
        "tests/test_github_*.py",
        "tests/test_google_drive_*.py",
        "tests/test_microsoft_teams_integration.py",
        "tests/test_pipeline_runner.py",
        "tests/test_provider_*.py",
        "tests/test_project_repository_*.py",
        "tests/test_slack_*.py",
        "tests/test_webmcp_*.py",
    ),
    "release": (
        "tests/test_archbro_release_*.py",
        "tests/test_deploy_*.py",
        "tests/test_local_release_entrypoint.py",
        "tests/test_pr48_release_contract_regressions.py",
        "tests/test_playwright_diagnostics.py",
    ),
}


def _normalise(path: str) -> str:
    return path.strip().replace("\\", "/").removeprefix("./")


def _changed_paths(lines: Iterable[str]) -> list[str]:
    return [path for raw in lines if (path := _normalise(raw))]


def _all_flags() -> dict[str, bool]:
    return {group: True for group in GROUPS}


def _classify_test(path: str, flags: dict[str, bool]) -> None:
    name = Path(path).name.lower()
    matched = False

    if any(token in name for token in ("canvas", "diagram", "_ui", "ui_", "browser_auth")):
        flags["frontend"] = True
        matched = True
    if any(token in name for token in ("postgres", "repository", "sqlite", "secret_cipher", "sync_cursor")):
        flags["persistence"] = True
        matched = True
    if any(
        token in name
        for token in (
            "connector",
            "firebase",
            "github",
            "google_drive",
            "microsoft_teams",
            "provider_",
            "slack",
            "webmcp",
        )
    ):
        flags["integrations"] = True
        matched = True
    if any(token in name for token in ("deploy_", "archbro_release", "local_release", "release_contract")):
        flags["release"] = True
        matched = True

    # Each unknown test fails closed on its own.  Do not let an earlier changed
    # path's classification hide a new test that has not been grouped yet.
    if not matched:
        flags["backend-core"] = True


def classify(event_name: str, base_ref: str, paths: list[str]) -> dict[str, bool]:
    flags = {group: False for group in GROUPS}
    flags["smoke"] = True

    # Pushes are deployment gates.  PRs into main are intentionally main-like.
    # Nightly/manual compatibility runs are also full by definition.
    if event_name != "pull_request" or base_ref not in SELECTIVE_PR_BASES:
        return {"full": True, **_all_flags()}

    for path in paths:
        if path in GLOBAL_SENSITIVE_EXACT or path.startswith(GLOBAL_SENSITIVE_PREFIXES):
            return {"full": True, **_all_flags()}

        if path.startswith("frontend/") or path.startswith(("tests/fixtures/", "examples/reference-projects/")):
            flags["frontend"] = True

        if path == "docs/CODEX_WEBMCP_ACCEPTANCE.md":
            flags["integrations"] = True

        if path.startswith("src/archbro/platform/persistence/"):
            flags["persistence"] = True
            persistence_name = Path(path).name
            if persistence_name in {"postgres.py", "secrets.py", "slack_inbox.py"}:
                flags["integrations"] = True
            if persistence_name == "postgres.py":
                # PostgreSQL is the sole repository implementation, so core API
                # and integration tests exercise it directly too.
                flags["backend-core"] = True
        elif path.startswith("src/archbro/integrations/"):
            flags["integrations"] = True
        elif path.startswith("src/archbro/backend/mcp/"):
            flags["backend-core"] = True
            flags["integrations"] = True
        elif path == "src/archbro/platform/runtime/release_identity.py":
            flags["release"] = True
        elif path == "src/archbro/platform/runtime/connector_sync.py":
            flags["backend-core"] = True
            flags["integrations"] = True
        elif path.startswith("src/archbro/platform/runtime/"):
            flags["backend-core"] = True
            if Path(path).name == "__init__.py":
                flags["integrations"] = True
        elif path.startswith("src/archbro/backend/api/"):
            flags["backend-core"] = True
            api_name = Path(path).name.lower()
            if (
                any(token in api_name for token in ("provider", "slack", "agent_surface"))
                or api_name in {"routes.py", "__init__.py"}
            ):
                flags["integrations"] = True
        elif path.startswith("src/archbro/backend/"):
            flags["backend-core"] = True
            backend_name = Path(path).name.lower()
            if path == "src/archbro/backend/llm/fake.py":
                # The deterministic fake is shared test infrastructure across
                # otherwise independent groups. Run every Python group, but do
                # not force the Docker image for this ordinary source change.
                for group in GROUPS:
                    flags[group] = True
            else:
                if backend_name in {"authorization.py", "evaluation.py", "gemini.py"}:
                    flags["integrations"] = True
                if backend_name in {"action_executor.py", "observation.py"}:
                    flags["persistence"] = True
        elif path.startswith("src/archbro/platform/pipeline/"):
            flags["backend-core"] = True
            flags["integrations"] = True
            if Path(path).name in {"contracts.py", "cursor.py"}:
                flags["persistence"] = True
        elif path.startswith("src/archbro/"):
            flags["backend-core"] = True

        if path.startswith("deploy/") or path in {"docker-compose.yml", ".env.example"}:
            flags["release"] = True

        if path.startswith("qa/"):
            lowered = path.lower()
            qa_classified = False
            if path.endswith(".mjs") or any(
                token in lowered for token in ("frontend", "canvas", "playwright", "reference_project")
            ):
                flags["frontend"] = True
                qa_classified = True
            if any(
                token in lowered
                for token in ("archbro_release", "verify_archbro_release", "release_acceptance", "playwright_diagnostics")
            ):
                flags["release"] = True
                qa_classified = True
            if not qa_classified:
                return {"full": True, **_all_flags()}

        if path.startswith("tests/test_") and path.endswith(".py"):
            _classify_test(path, flags)

        # Unknown executable source is never treated as docs-only.  This keeps
        # new code fail-closed without turning normal documentation PRs full.
        if path.endswith((".py", ".js", ".mjs")) and not (
            path.startswith("tests/") or path.startswith("qa/") or path.startswith("frontend/") or path.startswith("src/archbro/")
        ):
            flags["backend-core"] = True

    return {"full": False, **flags}


def _print_outputs(values: dict[str, bool]) -> None:
    for key in ("full", *GROUPS):
        output_key = key.replace("-", "_")
        print(f"{output_key}={'true' if values[key] else 'false'}")


def _expand_targets(root: Path, groups: list[str], changed: list[str]) -> list[str]:
    selected: set[str] = set()
    for group in groups:
        if group not in PYTEST_PATTERNS:
            raise SystemExit(f"unknown test group: {group}")
        for pattern in PYTEST_PATTERNS[group]:
            for match in root.glob(pattern):
                if match.is_file():
                    selected.add(match.relative_to(root).as_posix())

    for path in changed:
        if path.startswith("tests/test_") and path.endswith(".py") and (root / path).is_file():
            selected.add(path)

    if not selected:
        raise SystemExit("selector produced no pytest targets")
    return sorted(selected)


def preflight(root: Path) -> None:
    node = shutil.which("node")
    if node is None:
        raise SystemExit("Node.js is required: browser-auth tests would otherwise skip")

    browser_auth = sorted((root / "qa").glob("test_firebase_*_auth.mjs"))
    if len(browser_auth) < 2:
        raise SystemExit(f"browser-auth preflight found too few suites: {browser_auth}")

    database_url = (os.environ.get("DATABASE_URL") or "").strip()
    if not database_url:
        raise SystemExit("DATABASE_URL is required: PostgreSQL tests would otherwise skip")

    try:
        import psycopg

        with psycopg.connect(database_url, connect_timeout=5) as connection:
            row = connection.execute("SELECT 1").fetchone()
            if row != (1,):
                raise RuntimeError(f"unexpected PostgreSQL preflight result: {row!r}")
    except Exception as exc:  # pragma: no cover - exercised by CI environment
        raise SystemExit(f"PostgreSQL preflight failed: {exc}") from exc

    print(f"preflight ok: node={Path(node).name}, browser_auth_suites={len(browser_auth)}, postgres=reachable")


def _expect(actual: str, expected: str, label: str, errors: list[str]) -> None:
    if actual != expected:
        errors.append(f"{label}: expected {expected}, got {actual}")


def gate(args: argparse.Namespace) -> None:
    errors: list[str] = []
    _expect(args.classify, "success", "classify", errors)
    _expect(args.node, "success", "node", errors)

    if args.event_name == "pull_request" and args.base_ref in SELECTIVE_PR_BASES:
        _expect(args.pr_python, "success", "pr-python", errors)
        _expect(args.full_python, "skipped", "full-python", errors)
        _expect(args.compat_python, "skipped", "compat-python", errors)
        _expect(args.image, "success" if args.full_sensitive else "skipped", "image", errors)
    elif args.event_name == "pull_request" and args.base_ref == "main":
        _expect(args.pr_python, "skipped", "pr-python", errors)
        _expect(args.full_python, "success", "full-python", errors)
        _expect(args.compat_python, "skipped", "compat-python", errors)
        _expect(args.image, "success", "image", errors)
    elif args.event_name == "push":
        _expect(args.pr_python, "skipped", "pr-python", errors)
        _expect(args.full_python, "success", "full-python", errors)
        _expect(args.compat_python, "skipped", "compat-python", errors)
        _expect(args.image, "success", "image", errors)
    elif args.event_name in {"schedule", "workflow_dispatch"}:
        _expect(args.pr_python, "skipped", "pr-python", errors)
        _expect(args.full_python, "skipped", "full-python", errors)
        _expect(args.compat_python, "success", "compat-python", errors)
        _expect(args.image, "skipped", "image", errors)
    else:
        errors.append(f"unsupported CI event/base combination: {args.event_name}/{args.base_ref}")

    if errors:
        raise SystemExit("CI aggregate gate failed:\n- " + "\n- ".join(errors))
    print(f"CI aggregate gate passed for {args.event_name}/{args.base_ref or '-'}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    classify_parser = subparsers.add_parser("classify")
    classify_parser.add_argument("--event-name", required=True)
    classify_parser.add_argument("--base-ref", default="")

    targets_parser = subparsers.add_parser("targets")
    targets_parser.add_argument("--group", action="append", required=True, choices=GROUPS)
    targets_parser.add_argument("--root", default=".")

    preflight_parser = subparsers.add_parser("preflight")
    preflight_parser.add_argument("--root", default=".")

    gate_parser = subparsers.add_parser("gate")
    gate_parser.add_argument("--event-name", required=True)
    gate_parser.add_argument("--base-ref", default="")
    gate_parser.add_argument("--classify", required=True)
    gate_parser.add_argument("--node", required=True)
    gate_parser.add_argument("--pr-python", required=True)
    gate_parser.add_argument("--full-python", required=True)
    gate_parser.add_argument("--compat-python", required=True)
    gate_parser.add_argument("--image", required=True)
    gate_parser.add_argument("--full-sensitive", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    root = Path(getattr(args, "root", ".")).resolve()

    if args.command == "classify":
        _print_outputs(classify(args.event_name, args.base_ref, _changed_paths(sys.stdin)))
    elif args.command == "targets":
        for target in _expand_targets(root, args.group, _changed_paths(sys.stdin)):
            print(target)
    elif args.command == "preflight":
        preflight(root)
    elif args.command == "gate":
        gate(args)
    else:  # pragma: no cover
        raise AssertionError(args.command)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
