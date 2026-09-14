from __future__ import annotations

from fastapi.testclient import TestClient

from archbro.backend.core.contracts import (
    Architecture,
    Component,
    Project,
    ProjectEvent,
    ProjectEventSource,
    ProjectEventType,
    Relationship,
    Task,
)
from archbro.backend.llm.fake import FakeModelProvider
from archbro.platform.persistence.postgres import PostgresProjectRepository
from archbro.platform.runtime.app import create_app
from conftest import requires_database


pytestmark = requires_database


class ContextRecordingProvider(FakeModelProvider):
    def __init__(self) -> None:
        self.calls = 0
        self.last_event = None
        self.last_context = None
        self.last_usage = None

    async def generate(self, **kwargs):
        self.calls += 1
        self.last_event = kwargs["event"]
        self.last_context = kwargs["context"]
        decision = await super().generate(**kwargs)
        self.last_usage = {
            "input_tokens": 432,
            "output_tokens": 31,
            "total_tokens": 463,
        }
        return decision


def build_project(repo: PostgresProjectRepository) -> Project:
    project = Project(
        name="Bounded Context Integration",
        goal="Review one selected architecture boundary without preloading unrelated project state.",
        architecture_version=3,
    )
    repo.save_project(project)
    repo.save_architecture(
        project.id,
        Architecture(
            version=3,
            summary="API and durable data with an unrelated external surface.",
            components=[
                Component(
                    id="platform",
                    name="Platform",
                    type="system",
                    responsibility="Own application processing.",
                    children=[
                        Component(
                            id="api",
                            name="API",
                            type="service",
                            responsibility="Serve bounded requests.",
                        ),
                        Component(
                            id="worker",
                            name="Worker",
                            type="service",
                            responsibility="Run background work.",
                        ),
                    ],
                ),
                Component(
                    id="data",
                    name="Data",
                    type="system",
                    responsibility="Own durable state.",
                    children=[
                        Component(
                            id="database",
                            name="Database",
                            type="database",
                            responsibility="Persist request state.",
                        )
                    ],
                ),
                Component(
                    id="unrelated",
                    name="Unrelated Surface",
                    type="external",
                    responsibility="Remain outside the selected context.",
                ),
            ],
            relationships=[
                Relationship(
                    source="api",
                    target="database",
                    relationship_type="SQL",
                    description="Persist request state.",
                ),
                Relationship(
                    source="worker",
                    target="api",
                    relationship_type="CALL",
                    description="Submit background work.",
                ),
            ],
        ),
    )
    repo.save_task(
        project.id,
        Task(id="task-api", title="Review API validation", related_component="api"),
    )
    repo.save_task(
        project.id,
        Task(id="task-unrelated", title="Unrelated work", related_component="unrelated"),
    )
    return project


def manifest_request() -> dict:
    return {
        "node_id": "node:api",
        "direction": "both",
        "expansion_policy": "ASK_ALL",
        "expected_architecture_version": 3,
    }


def event_request(preview_hash: str) -> dict:
    return {
        "type": "USER_MESSAGE",
        "source": "FRONTEND",
        "payload": {
            "message": "Internal refactor only; accepted responsibilities are unchanged.",
            "ui_context": {
                "view": "architecture",
                "architecture_node_id": "api",
            },
            "agent_context_request": {
                **manifest_request(),
                "preview_manifest_hash": preview_hash,
            },
        },
    }


def test_preview_hash_is_rebuilt_before_provider_and_exact_manifest_reaches_model(dsn) -> None:
    repo = PostgresProjectRepository(dsn)
    project = build_project(repo)
    provider = ContextRecordingProvider()
    client = TestClient(create_app(repository=repo, provider=provider))

    preview = client.post(
        f"/projects/{project.id}/agent-context/manifest",
        json=manifest_request(),
    )
    assert preview.status_code == 200
    first_manifest = preview.json()
    assert first_manifest["sections"]["architecture"]["origin"]["component_id"] == "api"
    assert {task["id"] for task in first_manifest["sections"]["tasks"]} == {"task-api"}
    assert "task-unrelated" not in preview.text

    # Change an included fact after Preview. Execution must stop before claim,
    # provider invocation, event persistence, or AgentRun persistence.
    repo.save_task(
        project.id,
        Task(id="task-api-new", title="New API fact", related_component="api"),
    )
    stale = client.post(
        f"/projects/{project.id}/events",
        json=event_request(first_manifest["manifest_hash"]),
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["error"] == "agent_context_preview_stale"
    assert provider.calls == 0
    assert repo.list_events(project.id) == []
    assert repo.list_agent_runs(project.id) == []

    current_preview = client.post(
        f"/projects/{project.id}/agent-context/manifest",
        json=manifest_request(),
    )
    assert current_preview.status_code == 200
    current_manifest = current_preview.json()
    assert current_manifest["manifest_hash"] != first_manifest["manifest_hash"]

    executed = client.post(
        f"/projects/{project.id}/events",
        json=event_request(current_manifest["manifest_hash"]),
    )
    assert executed.status_code == 200
    result = executed.json()
    assert provider.calls == 1
    assert provider.last_event is not None
    assert provider.last_event.payload["agent_context_manifest"] == current_manifest
    assert provider.last_event.payload["agent_context_request"]["preview_manifest_hash"] == current_manifest["manifest_hash"]
    assert result["context_telemetry"]["manifest_hash"] == current_manifest["manifest_hash"]
    assert result["context_telemetry"]["selected_node_count"] == 1
    assert result["provider_usage"]["input_tokens"] == 432
    assert len(repo.list_events(project.id)) == 1
    assert len(repo.list_agent_runs(project.id)) == 1


def test_public_event_api_rejects_client_supplied_manifest_before_provider(dsn) -> None:
    repo = PostgresProjectRepository(dsn)
    project = build_project(repo)
    provider = ContextRecordingProvider()
    client = TestClient(create_app(repository=repo, provider=provider))

    response = client.post(
        f"/projects/{project.id}/events",
        json={
            "type": "USER_MESSAGE",
            "source": "FRONTEND",
            "payload": {
                "message": "Attempt to spoof context.",
                "agent_context_manifest": {"schema": "forged"},
            },
        },
    )

    assert response.status_code == 422
    assert "server-owned" in response.text
    assert provider.calls == 0
    assert repo.list_events(project.id) == []
    assert repo.list_agent_runs(project.id) == []


def test_repository_agent_context_snapshot_bounds_event_scan_and_keeps_latest_code_snapshot(dsn) -> None:
    repo = PostgresProjectRepository(dsn)
    project = build_project(repo)
    for index in range(4):
        repo.save_event(
            ProjectEvent(
                id=f"event-evidence-{index}",
                project_id=project.id,
                type=ProjectEventType.MANUAL_NOTE,
                source=ProjectEventSource.SYSTEM,
                payload={"summary": f"evidence {index}", "related_components": ["api"]},
            )
        )
    repo.save_event(
        ProjectEvent(
            id="event-code-latest",
            project_id=project.id,
            type=ProjectEventType.CODE_ARCHITECTURE_SNAPSHOT,
            source=ProjectEventSource.SYSTEM,
            payload={"request": {}},
        )
    )

    snapshot = repo.load_agent_context_snapshot(project.id, event_scan_limit=3)

    assert snapshot.event_history_truncated is True
    assert [event.id for event in snapshot.events] == [
        "event-evidence-1",
        "event-evidence-2",
        "event-evidence-3",
    ]
    assert snapshot.latest_code_architecture_event is not None
    assert snapshot.latest_code_architecture_event.id == "event-code-latest"
