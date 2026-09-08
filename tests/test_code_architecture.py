from __future__ import annotations

import copy

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from archbro.backend.agent.code_architecture import (
    CODE_ARCHITECTURE_SCHEMA,
    CodeArchitectureSnapshotRequest,
    build_code_architecture_snapshot,
)
from archbro.backend.api.agent_surface import build_agent_surface_router
from archbro.backend.core.contracts import Architecture, Component, ProjectEvent, ProjectEventType
from archbro.backend.llm.fake import FakeModelProvider
from archbro.platform.persistence.postgres import PostgresProjectRepository
from archbro.platform.runtime.app import build_app
from conftest import requires_database


REVISION = "0123456789abcdef0123456789abcdef01234567"


def snapshot_payload() -> dict:
    return {
        "repository": "Magic-Dala/archbro",
        "revision": REVISION,
        "summary": "Browser workspace calls the project API; the API persists project state.",
        "components": [
            {
                "id": "web",
                "name": "Browser Workspace",
                "type": "frontend",
                "responsibility": "Render the project workspace.",
                "kind": "UI",
                "source_evidence_ids": ["web-entry"],
            },
            {
                "id": "backend",
                "name": "Backend Runtime",
                "type": "backend",
                "responsibility": "Own product APIs and state transitions.",
                "kind": "SYSTEM",
                "source_evidence_ids": ["api-entry"],
                "children": [
                    {
                        "id": "api",
                        "name": "Project API",
                        "type": "service",
                        "responsibility": "Serve project and architecture requests.",
                        "kind": "SERVICE",
                        "source_evidence_ids": ["api-entry"],
                    },
                    {
                        "id": "repo",
                        "name": "Project Repository",
                        "type": "data-access",
                        "responsibility": "Persist canonical project state.",
                        "kind": "DATA_STORE",
                        "source_evidence_ids": ["repo-entry"],
                    },
                ],
            },
        ],
        "relationships": [
            {
                "source": "web",
                "target": "api",
                "relationship_type": "HTTPS",
                "description": "The browser invokes project endpoints.",
                "source_evidence_ids": ["web-entry", "api-entry"],
            },
            {
                "source": "api",
                "target": "repo",
                "relationship_type": "CALL",
                "description": "API handlers delegate persistence to the project repository.",
                "source_evidence_ids": ["api-entry", "repo-entry"],
            },
        ],
        "source_evidence": [
            {
                "id": "web-entry",
                "path": "frontend/web/app.js",
                "line_start": 1,
                "line_end": 2,
                "excerpt": "const state = {};\nasync function api(path) {}",
                "symbol": "api",
            },
            {
                "id": "api-entry",
                "path": "src/archbro/backend/api/routes.py",
                "line_start": 10,
                "line_end": 11,
                "excerpt": "def build_router():\n    pass",
                "symbol": "build_router",
            },
            {
                "id": "repo-entry",
                "path": "src/archbro/platform/persistence/postgres.py",
                "line_start": 86,
                "line_end": 87,
                "excerpt": "class PostgresProjectRepository:\n    \"\"\"PostgreSQL implementation of Jim's ProjectRepositoryPort.",
                "symbol": "PostgresProjectRepository",
            },
        ],
    }


def canonical_architecture() -> Architecture:
    return Architecture(
        version=4,
        summary="Accepted product boundaries",
        components=[
            Component(id="web", name="Web", type="ui", responsibility="Own user interaction."),
            Component(
                id="backend",
                name="Backend",
                type="backend",
                responsibility="Own product runtime.",
                children=[
                    Component(id="api", name="API", type="service", responsibility="Serve requests."),
                    Component(id="repo", name="Repository", type="data-access", responsibility="Persist state."),
                ],
            ),
        ],
    )


def code_truth_payload() -> dict:
    payload = snapshot_payload()
    payload["code_truth"] = {
        "extractor": "tree-sitter-test",
        "extractor_version": "1.0",
        "revision_verified": True,
        "base_revision": "f" * 40,
        "changed_paths": [
            "frontend/web/app.js",
            "src/archbro/backend/api/routes.py",
        ],
        "symbols": [
            {
                "id": "sym:web.api",
                "qualified_name": "frontend.web.api",
                "kind": "FUNCTION",
                "path": "frontend/web/app.js",
                "line_start": 1,
                "line_end": 2,
                "source_evidence_id": "web-entry",
                "architecture_component_id": "web",
            },
            {
                "id": "sym:api.build_router",
                "qualified_name": "archbro.backend.api.routes.build_router",
                "kind": "FUNCTION",
                "path": "src/archbro/backend/api/routes.py",
                "line_start": 10,
                "line_end": 11,
                "source_evidence_id": "api-entry",
                "architecture_component_id": "api",
            },
            {
                "id": "sym:repo.repository",
                "qualified_name": "archbro.platform.persistence.postgres.PostgresProjectRepository",
                "kind": "CLASS",
                "path": "src/archbro/platform/persistence/postgres.py",
                "line_start": 86,
                "line_end": 87,
                "source_evidence_id": "repo-entry",
                "architecture_component_id": "repo",
            },
        ],
        "relationships": [
            {
                "source": "sym:web.api",
                "target": "sym:api.build_router",
                "relationship_type": "ROUTE",
                "source_evidence_ids": ["web-entry", "api-entry"],
            },
            {
                "source": "sym:api.build_router",
                "target": "sym:repo.repository",
                "relationship_type": "CALL",
                "source_evidence_ids": ["api-entry", "repo-entry"],
            },
        ],
    }
    return payload


class InMemoryCodeArchitectureRepository:
    def __init__(self) -> None:
        self.project_id = "project-code-memory"
        self.architecture = canonical_architecture()
        self.events: list[ProjectEvent] = []

    def get_project(self, project_id: str):
        if project_id != self.project_id:
            raise KeyError(project_id)
        return {"id": project_id}

    def get_architecture(self, project_id: str) -> Architecture:
        self.get_project(project_id)
        return self.architecture

    def save_event(self, event: ProjectEvent) -> ProjectEvent:
        self.events.append(event)
        return event

    def get_latest_event_by_type(
        self,
        project_id: str,
        event_type: ProjectEventType,
    ) -> ProjectEvent | None:
        self.get_project(project_id)
        return next((event for event in reversed(self.events) if event.type == event_type), None)


def test_invalid_code_truth_binding_is_rejected_before_persist_and_latest_stays_readable() -> None:
    repository = InMemoryCodeArchitectureRepository()

    async def authorize(_request, project_id, _permission):
        return repository.get_project(project_id)

    app = FastAPI()
    app.include_router(build_agent_surface_router(repository, authorize))
    client = TestClient(app)

    valid = client.post(
        f"/projects/{repository.project_id}/code-architecture/snapshots",
        json=code_truth_payload(),
    )
    assert valid.status_code == 200, valid.text
    valid_event_id = valid.json()["event_id"]
    assert len(repository.events) == 1

    invalid_payload = copy.deepcopy(code_truth_payload())
    invalid_payload["code_truth"]["symbols"][0]["architecture_component_id"] = "nonexistent-component"
    rejected = client.post(
        f"/projects/{repository.project_id}/code-architecture/snapshots",
        json=invalid_payload,
    )
    assert rejected.status_code == 422, rejected.text
    assert "unknown canonical architecture components" in rejected.text
    assert len(repository.events) == 1

    latest = client.get(f"/projects/{repository.project_id}/code-architecture/latest")
    assert latest.status_code == 200, latest.text
    assert latest.json()["event_id"] == valid_event_id


def test_corrupted_stored_code_truth_snapshot_returns_server_error_not_retryable_conflict() -> None:
    repository = InMemoryCodeArchitectureRepository()
    repository.events.append(
        ProjectEvent(
            project_id=repository.project_id,
            type=ProjectEventType.CODE_ARCHITECTURE_SNAPSHOT,
            payload={"request": {"repository": "corrupted"}},
        )
    )

    async def authorize(_request, project_id, _permission):
        return repository.get_project(project_id)

    app = FastAPI()
    app.include_router(build_agent_surface_router(repository, authorize))
    client = TestClient(app)

    latest = client.get(f"/projects/{repository.project_id}/code-architecture/latest")
    response = client.get(
        f"/projects/{repository.project_id}/code-architecture/symbols/sym:any/impact"
    )

    assert latest.status_code == 500
    assert latest.json()["detail"] == "stored Code Architecture snapshot is invalid"
    assert response.status_code == 500
    assert response.json()["detail"] == "stored Code Architecture snapshot is invalid"


def test_code_architecture_snapshot_is_deterministic_revision_pinned_implementation_evidence():
    request = CodeArchitectureSnapshotRequest.model_validate(snapshot_payload())

    first = build_code_architecture_snapshot("project-code", request)
    second = build_code_architecture_snapshot("project-code", request)

    assert first == second
    assert first["schema"] == CODE_ARCHITECTURE_SCHEMA
    assert first["classification"] == "IMPLEMENTATION_EVIDENCE"
    assert first["canonical_state_mutated"] is False
    assert first["repository"] == {
        "provider": "github",
        "slug": "Magic-Dala/archbro",
        "url": "https://github.com/Magic-Dala/archbro",
        "revision": REVISION,
        "revision_pinned": True,
    }
    assert first["evidence_verification"]["repository_checkout_verified"] is False

    nodes = {node["component_id"]: node for node in first["diagram"]["nodes"]}
    assert set(nodes) == {"web", "backend", "api", "repo"}
    assert nodes["backend"]["id"] == "code-node:backend"
    assert nodes["api"]["parent_id"] == "code-node:backend"
    assert nodes["backend"]["child_count"] == 2
    assert nodes["api"]["sources"][0]["href"].startswith(
        f"https://github.com/Magic-Dala/archbro/blob/{REVISION}/"
    )
    assert nodes["api"]["sources"][0]["href"].endswith("#L10-L11")

    assert all(edge["id"].startswith("code-edge:") for edge in first["diagram"]["edges"])
    assert {node["id"] for node in first["diagram"]["nodes"]} == {
        node["node_id"] for node in first["positioned_graph"]["nodes"]
    }
    assert {edge["id"] for edge in first["diagram"]["edges"]} == {
        edge["edge_id"] for edge in first["positioned_graph"]["edges"]
    }
    assert first["positioned_graph"]["architecture_version"] is None


def test_verified_code_truth_is_compact_revision_pinned_and_bound_to_canonical_architecture():
    request = CodeArchitectureSnapshotRequest.model_validate(code_truth_payload())
    snapshot = build_code_architecture_snapshot(
        "project-code",
        request,
        architecture=canonical_architecture(),
    )

    truth = snapshot["code_truth"]
    assert truth["schema"] == "archbro.code_truth.v1"
    assert truth["classification"] == "VERIFIED_CODE_TRUTH"
    assert truth["revision"] == REVISION
    assert truth["symbol_count"] == 3
    assert truth["relationship_counts"] == {"CALL": 1, "ROUTE": 1}
    assert truth["mapped_symbol_count"] == 3
    assert truth["mapped_architecture_component_ids"] == ["api", "repo", "web"]
    assert truth["incremental_update"]["base_revision"] == "f" * 40
    assert truth["incremental_update"]["changed_path_count"] == 2
    assert truth["full_symbol_graph_embedded"] is False
    assert truth["query_mode"] == "BOUNDED_ON_DEMAND"
    assert "symbols" not in truth


def test_code_truth_impact_uses_direction_depth_and_canonical_bindings_without_runtime_claims():
    from archbro.backend.agent.code_architecture import build_code_truth_impact

    request = CodeArchitectureSnapshotRequest.model_validate(code_truth_payload())
    impact = build_code_truth_impact(
        "project-code",
        request,
        "sym:repo.repository",
        direction="callers",
        max_hops=2,
        architecture=canonical_architecture(),
    )

    assert impact["classification"] == "STRUCTURAL_BLAST_RADIUS"
    assert impact["runtime_breakage_claimed"] is False
    assert [item["id"] for item in impact["symbols"]] == [
        "sym:api.build_router",
        "sym:web.api",
    ]
    assert [item["hop"] for item in impact["symbols"]] == [1, 2]
    assert impact["affected_architecture_node_ids"] == ["node:api", "node:repo", "node:web"]
    assert {(item["source"], item["target"], item["relationship_type"]) for item in impact["relationships"]} == {
        ("sym:web.api", "sym:api.build_router", "ROUTE"),
        ("sym:api.build_router", "sym:repo.repository", "CALL"),
    }
    assert impact["truncated"] is False


def test_code_truth_impact_reports_hop_and_result_limits_when_both_are_hit():
    from archbro.backend.agent.code_architecture import build_code_truth_impact

    payload = code_truth_payload()
    payload["code_truth"]["symbols"].extend(
        [
            {
                "id": "sym:worker",
                "qualified_name": "worker.persist",
                "kind": "FUNCTION",
                "path": "src/worker.py",
                "line_start": 1,
                "line_end": 1,
                "architecture_component_id": "api",
            },
            {
                "id": "sym:deep",
                "qualified_name": "deep.entry",
                "kind": "FUNCTION",
                "path": "src/deep.py",
                "line_start": 1,
                "line_end": 1,
                "architecture_component_id": "web",
            },
        ]
    )
    payload["code_truth"]["relationships"].extend(
        [
            {
                "source": "sym:worker",
                "target": "sym:repo.repository",
                "relationship_type": "CALL",
                "source_evidence_ids": [],
            },
            {
                "source": "sym:deep",
                "target": "sym:api.build_router",
                "relationship_type": "CALL",
                "source_evidence_ids": [],
            },
        ]
    )
    request = CodeArchitectureSnapshotRequest.model_validate(payload)

    impact = build_code_truth_impact(
        "project-code",
        request,
        "sym:repo.repository",
        direction="callers",
        max_hops=1,
        max_results=1,
        architecture=canonical_architecture(),
    )

    assert impact["truncated"] is True
    assert impact["limit_reason"] == "MAX_RESULTS"
    assert impact["limit_reasons"] == ["MAX_HOPS", "MAX_RESULTS"]


def test_verified_code_truth_does_not_require_symbol_excerpts_in_the_index():
    payload = code_truth_payload()
    for symbol in payload["code_truth"]["symbols"]:
        symbol.pop("source_evidence_id")
    for relationship in payload["code_truth"]["relationships"]:
        relationship["source_evidence_ids"] = []

    request = CodeArchitectureSnapshotRequest.model_validate(payload)
    snapshot = build_code_architecture_snapshot(
        "project-code",
        request,
        architecture=canonical_architecture(),
    )

    assert snapshot["code_truth"]["symbol_count"] == 3
    assert snapshot["code_truth"]["full_symbol_graph_embedded"] is False


def test_code_truth_fails_closed_on_unverified_index_dangling_symbols_and_unknown_architecture_binding():
    payload = code_truth_payload()
    payload["code_truth"]["revision_verified"] = False
    with pytest.raises(ValidationError, match="revision-verified deterministic extractor"):
        CodeArchitectureSnapshotRequest.model_validate(payload)

    payload = code_truth_payload()
    payload["code_truth"]["relationships"][0]["target"] = "sym:missing"
    with pytest.raises(ValidationError, match="existing symbol ids"):
        CodeArchitectureSnapshotRequest.model_validate(payload)

    payload = code_truth_payload()
    payload["code_truth"]["symbols"][0]["architecture_component_id"] = "missing"
    request = CodeArchitectureSnapshotRequest.model_validate(payload)
    with pytest.raises(ValueError, match="unknown canonical architecture components"):
        build_code_architecture_snapshot(
            "project-code",
            request,
            architecture=canonical_architecture(),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("revision", "abc123"),
        ("repository", "https://example.com/not-github/repo"),
    ],
)
def test_code_architecture_rejects_unpinned_or_non_github_repository(field: str, value: str):
    payload = snapshot_payload()
    payload[field] = value
    with pytest.raises(ValidationError):
        CodeArchitectureSnapshotRequest.model_validate(payload)


@pytest.mark.parametrize(
    "path",
    [
        "../secret.py",
        "/etc/passwd",
        "C:/Windows/system32.txt",
        "C:\\Windows\\system32.txt",
        ".git/config",
        "src/.git/config",
    ],
)
def test_code_architecture_rejects_non_repository_source_paths(path: str):
    payload = snapshot_payload()
    payload["source_evidence"][0]["path"] = path
    with pytest.raises(ValidationError):
        CodeArchitectureSnapshotRequest.model_validate(payload)


def test_code_architecture_rejects_mismatched_line_excerpt():
    payload = snapshot_payload()
    payload["source_evidence"][0]["line_end"] = 3
    with pytest.raises(ValidationError, match="line count"):
        CodeArchitectureSnapshotRequest.model_validate(payload)


def test_code_architecture_evidence_href_encodes_each_git_path_segment():
    payload = snapshot_payload()
    payload["source_evidence"][0]["path"] = "frontend/weird #name%?.js"
    request = CodeArchitectureSnapshotRequest.model_validate(payload)
    snapshot = build_code_architecture_snapshot("project-code", request)
    web = next(node for node in snapshot["diagram"]["nodes"] if node["component_id"] == "web")
    href = web["sources"][0]["href"]
    assert "/frontend/weird%20%23name%25%3F.js#L1-L2" in href
    assert "weird #name%?.js" not in href


def test_code_architecture_rejects_unknown_evidence_and_dangling_relationships():
    payload = snapshot_payload()
    payload["components"][0]["source_evidence_ids"] = ["missing"]
    with pytest.raises(ValidationError, match="unknown source evidence"):
        CodeArchitectureSnapshotRequest.model_validate(payload)

    payload = snapshot_payload()
    payload["relationships"][0]["target"] = "missing"
    with pytest.raises(ValidationError, match="existing component ids"):
        CodeArchitectureSnapshotRequest.model_validate(payload)


@requires_database
def test_code_architecture_api_does_not_mutate_living_architecture(dsn):
    repository = PostgresProjectRepository(dsn)
    client = TestClient(build_app(repository, FakeModelProvider()))
    project = client.post(
        "/projects",
        json={"name": "Code Evidence", "goal": "Compare implementation with accepted architecture."},
    )
    assert project.status_code == 200
    project_id = project.json()["id"]
    bootstrap = client.post(
        f"/projects/{project_id}/interactive-initial-architecture",
        json={
            "architecture": {
                "version": 1,
                "summary": "Accepted intent",
                "components": [
                    {
                        "id": "product",
                        "name": "Product",
                        "type": "system",
                        "responsibility": "Own product intent.",
                        "children": [
                            {
                                "id": "workspace",
                                "name": "Workspace",
                                "type": "ui",
                                "responsibility": "Own human interaction.",
                            }
                        ],
                    }
                ],
                "relationships": [],
                "decisions": [],
                "assumptions": [],
                "risks": [],
            },
            "tasks": [{"title": "Build workspace", "related_component": "workspace"}],
            "planning_trace": {
                "system_map_root_ids": ["product"],
                "scope_evaluations": [
                    {"scope_component_id": "product", "decomposition": "EXPANDED", "child_ids": ["workspace"]},
                    {"scope_component_id": "workspace", "decomposition": "JUSTIFIED_LEAF", "child_ids": [], "leaf_reason": "Workspace owns one human interaction boundary with no independent architecture subsystem below it."},
                ],
                "reconciled": True,
            },
            "reasoning": "Fixture with recursive scope evaluation",
        },
    )
    assert bootstrap.status_code == 200

    before = client.get(f"/projects/{project_id}/architecture")
    assert before.status_code == 200

    response = client.post(
        f"/projects/{project_id}/code-architecture/snapshot",
        json=snapshot_payload(),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["classification"] == "IMPLEMENTATION_EVIDENCE"
    assert body["canonical_state_mutated"] is False

    after = client.get(f"/projects/{project_id}/architecture")
    assert after.status_code == 200
    assert after.json() == before.json()


@requires_database
def test_code_architecture_publish_is_durable_idempotent_and_latest_rebuilds_without_mutating_living_architecture(dsn):
    repository = PostgresProjectRepository(dsn)
    client = TestClient(build_app(repository, FakeModelProvider()))
    project = client.post(
        "/projects",
        json={"name": "Durable Code Evidence", "goal": "Keep implementation evidence separate from accepted intent."},
    )
    assert project.status_code == 200
    project_id = project.json()["id"]
    bootstrap = client.post(
        f"/projects/{project_id}/interactive-initial-architecture",
        json={
            "architecture": {
                "version": 1,
                "summary": "Accepted living intent",
                "components": [
                    {
                        "id": "product",
                        "name": "Product",
                        "type": "system",
                        "responsibility": "Own accepted product intent.",
                        "children": [{"id": "product-core", "name": "Product Core", "type": "application", "responsibility": "Own the accepted product implementation boundary."}],
                    }
                ],
                "relationships": [],
                "decisions": [],
                "assumptions": [],
                "risks": [],
            },
            "tasks": [{"title": "Keep code evidence reviewable", "related_component": "product-core"}],
            "planning_trace": {
                "system_map_root_ids": ["product"],
                "scope_evaluations": [
                    {"scope_component_id": "product", "decomposition": "EXPANDED", "child_ids": ["product-core"]},
                    {"scope_component_id": "product-core", "decomposition": "JUSTIFIED_LEAF", "child_ids": [], "leaf_reason": "Product Core is one implementation boundary with no independently addressable architecture subsystem below it."},
                ],
                "reconciled": True,
            },
            "reasoning": "Fixture with recursive scope evaluation",
        },
    )
    assert bootstrap.status_code == 200
    before = client.get(f"/projects/{project_id}/architecture").json()

    empty_latest = client.get(f"/projects/{project_id}/code-architecture/latest")
    assert empty_latest.status_code == 204
    assert empty_latest.content == b""

    first = client.post(f"/projects/{project_id}/code-architecture/snapshots", json=snapshot_payload())
    assert first.status_code == 200, first.text
    assert first.json()["derived_artifact_persisted"] is True
    assert first.json()["canonical_state_mutated"] is False
    event_id = first.json()["event_id"]

    second = client.post(f"/projects/{project_id}/code-architecture/snapshots", json=snapshot_payload())
    assert second.status_code == 200, second.text
    assert second.json()["event_id"] == event_id

    invalid_payload = snapshot_payload()
    invalid_payload["code_truth"] = {
        "extractor": "tree-sitter-test",
        "extractor_version": "1.0",
        "revision_verified": True,
        "symbols": [
            {
                "id": "sym:invalid-binding",
                "qualified_name": "frontend.web.api",
                "kind": "FUNCTION",
                "path": "frontend/web/app.js",
                "line_start": 1,
                "line_end": 2,
                "source_evidence_id": "web-entry",
                "architecture_component_id": "nonexistent-component",
            }
        ],
        "relationships": [],
    }
    code_events_before_rejection = [
        event
        for event in repository.list_events(project_id)
        if event.type == ProjectEventType.CODE_ARCHITECTURE_SNAPSHOT
    ]
    rejected = client.post(
        f"/projects/{project_id}/code-architecture/snapshots",
        json=invalid_payload,
    )
    assert rejected.status_code == 422, rejected.text
    assert "unknown canonical architecture components" in rejected.text
    code_events_after_rejection = [
        event
        for event in repository.list_events(project_id)
        if event.type == ProjectEventType.CODE_ARCHITECTURE_SNAPSHOT
    ]
    assert [event.id for event in code_events_after_rejection] == [
        event.id for event in code_events_before_rejection
    ]

    latest = client.get(f"/projects/{project_id}/code-architecture/latest")
    assert latest.status_code == 200, latest.text
    latest_body = latest.json()
    assert latest_body["event_id"] == event_id
    assert latest_body["repository"]["revision"] == REVISION
    assert latest_body["classification"] == "IMPLEMENTATION_EVIDENCE"
    assert latest_body["canonical_state_mutated"] is False
    assert {node["id"] for node in latest_body["diagram"]["nodes"]} == {
        "code-node:web",
        "code-node:backend",
        "code-node:api",
        "code-node:repo",
    }
    assert client.get(f"/projects/{project_id}/architecture").json() == before
