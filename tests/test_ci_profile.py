from __future__ import annotations

import argparse
import ast
import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("archbro_ci_profile", ROOT / "scripts" / "ci_profile.py")
assert SPEC is not None and SPEC.loader is not None
CI = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CI)


def _classify(*paths: str, base_ref: str = "dev") -> dict[str, bool]:
    return CI.classify("pull_request", base_ref, list(paths))


def test_dev2_uses_the_same_selective_profile_as_dev() -> None:
    paths = ("frontend/web/app.js", "docs/DEVELOPMENT.md")
    assert _classify(*paths, base_ref="dev2") == _classify(*paths, base_ref="dev")


@pytest.mark.parametrize(
    "path",
    [
        "pyproject.toml",
        "Dockerfile",
        ".dockerignore",
        "scripts/ci_profile.py",
        "scripts/ci_future_helper.py",
        "tests/conftest.py",
        "src/archbro/backend/core/contracts.py",
        "src/archbro/backend/core/repository.py",
        "src/archbro/platform/runtime/app.py",
        ".github/workflows/ci.yml",
        ".github/actions/python/action.yml",
    ],
)
def test_global_sensitive_paths_force_the_full_profile(path: str) -> None:
    result = _classify(path)

    assert result["full"] is True
    assert all(result[group] is True for group in CI.GROUPS)


def test_docs_only_pr_runs_only_smoke() -> None:
    result = _classify("docs/DEVELOPMENT.md", "README.md")

    assert result == {
        "full": False,
        "smoke": True,
        "frontend": False,
        "backend-core": False,
        "persistence": False,
        "integrations": False,
        "release": False,
    }


@pytest.mark.parametrize(
    ("path", "group"),
    [
        ("frontend/web/app.js", "frontend"),
        ("tests/fixtures/canvas_four_source_geometry.json", "frontend"),
        ("examples/reference-projects/v1/01.json", "frontend"),
        ("qa/reference_projects.py", "frontend"),
        ("docs/CODEX_WEBMCP_ACCEPTANCE.md", "integrations"),
        ("src/archbro/backend/agent/orchestration.py", "backend-core"),
        ("src/archbro/backend/api/routes.py", "backend-core"),
        ("src/archbro/platform/persistence/postgres.py", "persistence"),
        ("src/archbro/platform/persistence/provider_credentials.py", "persistence"),
        ("src/archbro/integrations/slack/events.py", "integrations"),
        ("src/archbro/backend/mcp/gateway.py", "integrations"),
        ("deploy/deploy-stack.sh", "release"),
        ("qa/playwright_diagnostics.py", "release"),
    ],
)
def test_representative_paths_select_their_coarse_group(path: str, group: str) -> None:
    result = _classify(path)

    assert result["full"] is False
    assert result[group] is True


@pytest.mark.parametrize(
    "path",
    [
        "src/archbro/backend/api/provider_connections.py",
        "src/archbro/backend/mcp/provider_gateway.py",
        "src/archbro/backend/mcp/provider_oauth.py",
    ],
)
def test_provider_source_selects_integration_regressions(path: str) -> None:
    result = _classify(path)

    assert result["backend-core"] is True
    assert result["integrations"] is True


def test_model_provider_abstraction_stays_in_backend_core() -> None:
    result = _classify("src/archbro/backend/llm/provider.py")

    assert result["backend-core"] is True
    assert result["integrations"] is False


def test_cross_layer_groups_include_their_direct_regressions() -> None:
    frontend = set(CI._expand_targets(ROOT, ["frontend"], []))
    backend = set(CI._expand_targets(ROOT, ["backend-core"], []))
    integrations = set(CI._expand_targets(ROOT, ["integrations"], []))

    assert {
        "tests/test_agent_context_mcp_gateway.py",
        "tests/test_api_contract.py",
        "tests/test_firebase_github_auth.py",
        "tests/test_provider_oauth.py",
        "tests/test_webmcp_integration.py",
        "tests/test_webmcp_schema_budget.py",
    } <= frontend
    assert {
        "tests/test_connector_sync.py",
        "tests/test_sync_cursor.py",
        "tests/test_hierarchical_planner.py",
        "tests/test_project_authorization.py",
    } <= backend
    assert {
        "tests/test_agent_context_mcp_gateway.py",
        "tests/test_pipeline_runner.py",
        "tests/test_provider_credential_persistence.py",
    } <= integrations
    persistence = set(CI._expand_targets(ROOT, ["persistence"], []))
    assert "tests/test_provider_credential_persistence.py" in persistence


def test_every_test_file_is_grouped_or_explicitly_full_only() -> None:
    all_tests = {
        path.relative_to(ROOT).as_posix()
        for path in ROOT.glob("tests/test_*.py")
    }
    grouped: set[str] = set()
    for group in CI.GROUPS:
        grouped.update(CI._expand_targets(ROOT, [group], []))

    assert all_tests - grouped == {"tests/test_real_gemini.py"}


def test_unclassified_qa_support_file_fails_closed() -> None:
    result = _classify("qa/setup_archbro_identity_platform.ps1")
    assert result["full"] is True


def test_selective_profiles_cover_direct_source_imports() -> None:
    """Every ordinary direct source consumer must be selected by its profile."""
    all_tests = {
        path.relative_to(ROOT).as_posix() for path in ROOT.glob("tests/test_*.py")
    }
    for test in sorted(ROOT.glob("tests/test_*.py")):
        if test.name == "test_real_gemini.py":
            continue
        tree = ast.parse(test.read_text(encoding="utf-8"))
        modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("archbro"):
                modules.add(node.module)
            elif isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names if alias.name.startswith("archbro"))

        for module in modules:
            relative = Path("src", *module.split("."))
            source = next(
                (
                    candidate
                    for candidate in (relative.with_suffix(".py"), relative / "__init__.py")
                    if (ROOT / candidate).is_file()
                ),
                None,
            )
            if source is None:
                continue
            profile = _classify(source.as_posix())
            selected = all_tests if profile["full"] else set(
                CI._expand_targets(
                    ROOT,
                    [group for group in CI.GROUPS if profile[group]],
                    [],
                )
            )
            assert test.relative_to(ROOT).as_posix() in selected, (
                f"{source.as_posix()} does not select direct importing test "
                f"{test.relative_to(ROOT).as_posix()}"
            )


def test_a_changed_full_only_test_is_still_selected_directly() -> None:
    selected = CI._expand_targets(
        ROOT,
        ["smoke"],
        ["tests/test_real_gemini.py"],
    )

    assert "tests/test_real_gemini.py" in selected


def _gate_args(**overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "event_name": "pull_request",
        "base_ref": "dev",
        "classify": "success",
        "node": "success",
        "pr_python": "success",
        "full_python": "skipped",
        "compat_python": "skipped",
        "image": "skipped",
        "full_sensitive": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


@pytest.mark.parametrize(
    "args",
    [
        _gate_args(),
        _gate_args(base_ref="dev2"),
        _gate_args(image="success", full_sensitive=True),
        _gate_args(
            base_ref="main",
            pr_python="skipped",
            full_python="success",
            image="success",
        ),
        _gate_args(
            event_name="push",
            base_ref="",
            pr_python="skipped",
            full_python="success",
            image="success",
        ),
        _gate_args(
            event_name="schedule",
            base_ref="",
            pr_python="skipped",
            compat_python="success",
        ),
        _gate_args(
            event_name="workflow_dispatch",
            base_ref="",
            pr_python="skipped",
            compat_python="success",
        ),
    ],
)
def test_python_aggregate_accepts_only_the_expected_job_shape(args: argparse.Namespace) -> None:
    CI.gate(args)


def test_python_aggregate_rejects_an_unexpected_skip() -> None:
    with pytest.raises(SystemExit, match="pr-python"):
        CI.gate(_gate_args(pr_python="skipped"))


def test_workflow_keeps_the_stable_gate_and_one_compatibility_sha() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    trigger = workflow.split("\njobs:\n", maxsplit=1)[0]

    assert "'Python compatibility' || 'Python'" in workflow
    assert "Full Python ${{ matrix.python-version }}" not in workflow
    assert "Dev compatibility Python ${{ matrix.python-version }}" not in workflow
    assert "\n    name: Full Python\n" in workflow
    assert "\n    name: Dev compatibility Python\n" in workflow
    assert "\n    paths:" not in trigger
    assert "branches: [main, dev, dev2]" in trigger
    assert "github.base_ref == 'dev2'" in workflow
    assert "target_sha: ${{ steps.profile.outputs.target_sha }}" in workflow
    assert workflow.count("ref: ${{ needs.classify.outputs.target_sha }}") == 3
    assert workflow.count("git diff --no-renames --name-only") == 2
    assert workflow.count("uses: actions/checkout@v4") == workflow.count(
        "persist-credentials: false"
    )


def test_deploy_waits_for_python_on_the_exact_push_sha() -> None:
    deploy = (ROOT / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")

    assert 'commits/$GITHUB_SHA/check-runs?per_page=100' in deploy
    assert 'select(.name == "Python")' in deploy
    assert 'if [ "$conclusion" = "success" ]' in deploy
    assert 'case "$REF_NAME" in' in deploy
    assert 'case "${{ github.ref_name }}" in' not in deploy
    assert '"$IMAGE_REPO:${{ github.ref_name }}"' not in deploy
    assert deploy.count("uses: actions/checkout@v4") == deploy.count(
        "persist-credentials: false"
    )
    assert "google-github-actions/auth@c200f3691d83b41bf9bbd8638997a462592937ed" in deploy
    assert (
        "google-github-actions/setup-gcloud@e427ad8a34f8676edf47cf7d7925499adf3eb74f"
        in deploy
    )
