from __future__ import annotations

import json
from dataclasses import dataclass

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from archbro.backend.agent.context_manifest import (
    AGENT_CONTEXT_EVENT_SCAN_LIMIT,
    AgentContextExecutionRequest,
    AgentContextManifestRequest,
    AgentContextPreviewStaleError,
    MAX_CONTEXT_CHARS,
    MAX_ESTIMATED_INPUT_TOKENS,
    build_agent_context_manifest,
    prepare_agent_context_event_payload,
)
from archbro.backend.agent.node_context import StaleArchitectureVersionError
from archbro.backend.agent.orchestration import _context_telemetry, _provider_usage
from archbro.backend.api.agent_surface import build_agent_surface_router
from archbro.backend.api.routes import build_router
from archbro.backend.core.contracts import (
    AgentContextSnapshot,
    Architecture,
    ArchitectureChangeProposal,
    ArchitectureOption,
    Component,
    Project,
    ProjectContext,
    ProjectEvent,
    ProjectEventSource,
    ProjectEventType,
    ProposalStatus,
    Relationship,
    Task,
)
from archbro.backend.llm.gemini import _compact_context_facts, _compact_event_facts
from archbro.backend.llm.fake import FakeModelProvider


class ManifestRepository:
    def __init__(self) -> None:
        self.project = Project(
            id="project-context",
            name="Context Project",
            goal="Keep agent reads bounded to a selected architecture area.",
            architecture_version=7,
            owner_user_id="local-demo",
        )
        self.architecture = Architecture(
            version=7,
            summary="Backend, data, and unrelated external search boundaries.",
            components=[
                Component(
                    id="backend",
                    name="Backend",
                    type="system",
                    responsibility="Own request processing.",
                    children=[
                        Component(
                            id="api",
                            name="API",
                            type="service",
                            responsibility="Serve requests.",
                        ),
                        Component(
                            id="worker",
                            name="Worker",
                            type="service",
                            responsibility="Run jobs.",
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
                            id="db",
                            name="Database",
                            type="database",
                            responsibility="Persist state.",
                        )
                    ],
                ),
                Component(
                    id="external",
                    name="External",
                    type="system",
                    responsibility="Own unrelated providers.",
                    children=[
                        Component(
                            id="search",
                            name="Unrelated Provider",
                            type="api",
                            responsibility="Remain outside the selected context.",
                        )
                    ],
                ),
            ],
            relationships=[
                Relationship(
                    source="api",
                    target="db",
                    relationship_type="SQL",
                    description="Persist request state.",
                ),
                Relationship(
                    source="worker",
                    target="api",
                    relationship_type="CALL",
                    description="Submit work.",
                ),
            ],
        )
        self.tasks = [
            Task(id="task-api", title="Tighten API validation", related_component="api"),
            Task(id="task-db", title="Verify persistence", related_component="db"),
            Task(id="task-unrelated", title="Reindex provider", related_component="search"),
        ]
        self.proposals = [
            ArchitectureChangeProposal(
                id="proposal-api",
                project_id=self.project.id,
                base_architecture_version=7,
                reason="Review API split.",
                evidence=["API load increased."],
                observed_change="API responsibilities broadened.",
                affected_components=["api"],
                proposed_changes=[{"component_id": "api", "change": "split"}],
                impact="Requires human review.",
                recommended_option=ArchitectureOption.ACCEPT_PROPOSED_CHANGE,
                status=ProposalStatus.PENDING,
            ),
            ArchitectureChangeProposal(
                id="proposal-unrelated",
                project_id=self.project.id,
                base_architecture_version=7,
                reason="Review provider.",
                evidence=["Provider changed."],
                observed_change="External contract changed.",
                affected_components=["search"],
                proposed_changes=[],
                impact="Unrelated.",
                recommended_option=ArchitectureOption.KEEP_CURRENT,
                status=ProposalStatus.PENDING,
            ),
        ]
        self.events = [
            ProjectEvent(
                id="event-api-evidence",
                project_id=self.project.id,
                type=ProjectEventType.MANUAL_NOTE,
                source=ProjectEventSource.SYSTEM,
                payload={
                    "summary": "API latency evidence",
                    "evidence": ["p95 increased by 12 ms"],
                    "related_components": ["api"],
                },
            ),
            ProjectEvent(
                id="event-unrelated-evidence",
                project_id=self.project.id,
                type=ProjectEventType.MANUAL_NOTE,
                source=ProjectEventSource.SYSTEM,
                payload={
                    "summary": "Provider evidence",
                    "evidence": ["unrelated provider changed"],
                    "related_components": ["search"],
                },
            ),
            ProjectEvent(
                id="event-code",
                project_id=self.project.id,
                type=ProjectEventType.CODE_ARCHITECTURE_SNAPSHOT,
                source=ProjectEventSource.SYSTEM,
                payload={
                    "request": {
                        "repository": "owner/repo",
                        "revision": "a" * 40,
                        "summary": "Pinned implementation evidence.",
                        "components": [
                            {
                                "id": "api-implementation",
                                "name": "API implementation",
                                "type": "module",
                                "responsibility": "Implement API requests.",
                                "source_evidence_ids": ["source-api"],
                            }
                        ],
                        "relationships": [],
                        "source_evidence": [
                            {
                                "id": "source-api",
                                "path": "src/api.py",
                                "line_start": 10,
                                "line_end": 10,
                                "excerpt": "def handle_request(): pass",
                                "symbol": "handle_request",
                            }
                        ],
                        "code_truth": {
                            "extractor": "fixture",
                            "extractor_version": "1",
                            "revision_verified": True,
                            "symbols": [
                                {
                                    "id": "symbol-api",
                                    "qualified_name": "api.handle_request",
                                    "kind": "function",
                                    "path": "src/api.py",
                                    "line_start": 10,
                                    "line_end": 10,
                                    "source_evidence_id": "source-api",
                                    "architecture_component_id": "api",
                                }
                            ],
                            "relationships": [],
                        },
                    }
                },
            ),
        ]

    def load_agent_context_snapshot(
        self,
        project_id: str,
        *,
        event_scan_limit: int,
    ) -> AgentContextSnapshot:
        if project_id != self.project.id:
            raise KeyError(project_id)
        evidence_events = [
            event
            for event in self.events
            if event.type != ProjectEventType.CODE_ARCHITECTURE_SNAPSHOT
        ]
        latest_code_architecture_event = next(
            (
                event
                for event in reversed(self.events)
                if event.type == ProjectEventType.CODE_ARCHITECTURE_SNAPSHOT
            ),
            None,
        )
        return AgentContextSnapshot(
            project=self.project,
            architecture=self.architecture,
            tasks=list(self.tasks),
            proposals=list(self.proposals),
            events=list(evidence_events[-event_scan_limit:]),
            event_history_truncated=len(evidence_events) > event_scan_limit,
            latest_code_architecture_event=latest_code_architecture_event,
        )

    def get_project(self, project_id: str) -> Project:
        if project_id != self.project.id:
            raise KeyError(project_id)
        return self.project

    def get_architecture(self, project_id: str) -> Architecture:
        self.get_project(project_id)
        return self.architecture

    def list_tasks(self, project_id: str) -> list[Task]:
        self.get_project(project_id)
        return list(self.tasks)

    def list_proposals(self, project_id: str) -> list[ArchitectureChangeProposal]:
        self.get_project(project_id)
        return list(self.proposals)

    def list_events(self, project_id: str, limit: int = 100) -> list[ProjectEvent]:
        self.get_project(project_id)
        return list(self.events[-limit:])

    def get_latest_event_by_type(
        self,
        project_id: str,
        event_type: ProjectEventType,
    ) -> ProjectEvent | None:
        self.get_project(project_id)
        return next(
            (event for event in reversed(self.events) if event.type == event_type),
            None,
        )


def request(policy: str = "ASK_ALL") -> AgentContextManifestRequest:
    return AgentContextManifestRequest(
        node_id="node:api",
        direction="both",
        expansion_policy=policy,
        expected_architecture_version=7,
    )


def test_manifest_is_deterministic_bounded_and_source_filtered() -> None:
    repository = ManifestRepository()
    architecture_before = repository.architecture.model_dump(mode="json")
    first = build_agent_context_manifest(repository, repository.project.id, request())
    second = build_agent_context_manifest(repository, repository.project.id, request())

    assert first == second
    assert len(first["manifest_hash"]) == 64
    assert first["usage"]["context_chars"] <= MAX_CONTEXT_CHARS
    assert first["usage"]["estimated_input_tokens"] <= 6_000
    assert first["sections"]["architecture"]["origin"]["component_id"] == "api"
    assert [item["component_id"] for item in first["sections"]["architecture"]["lineage"]] == [
        "backend",
        "api",
    ]
    assert {item["id"] for item in first["sections"]["tasks"]} == {"task-api", "task-db"}
    assert [item["id"] for item in first["sections"]["pending_proposals"]] == ["proposal-api"]
    assert [item["event_id"] for item in first["sections"]["evidence"]] == [
        "event-api-evidence"
    ]
    assert first["sections"]["code_truth"]["status"] == "MATCHED"
    assert first["sections"]["code_truth"]["chunks"][0]["symbol"]["id"] == "symbol-api"
    serialized = json.dumps(first, sort_keys=True)
    assert "task-unrelated" not in serialized
    assert "event-unrelated-evidence" not in serialized
    assert "proposal-unrelated" not in serialized
    assert repository.architecture.model_dump(mode="json") == architecture_before


def test_manifest_uses_one_repository_snapshot_instead_of_torn_independent_reads() -> None:
    class SnapshotOnlyRepository(ManifestRepository):
        def get_project(self, _project_id: str):
            raise AssertionError("manifest must not perform a second project read")

        def get_architecture(self, _project_id: str):
            raise AssertionError("manifest must not perform a second architecture read")

        def list_tasks(self, _project_id: str):
            raise AssertionError("manifest must not perform a second task read")

        def list_proposals(self, _project_id: str):
            raise AssertionError("manifest must not perform a second proposal read")

        def list_events(self, _project_id: str, limit: int = 100):
            raise AssertionError("manifest must not perform a second event read")

    repository = SnapshotOnlyRepository()
    manifest = build_agent_context_manifest(repository, repository.project.id, request())

    assert manifest["project_id"] == repository.project.id
    assert manifest["architecture_version"] == 7


def test_manifest_filters_scope_before_evidence_limit_so_older_relevant_events_survive() -> None:
    repository = ManifestRepository()
    relevant = [
        ProjectEvent(
            id=f"event-relevant-{index}",
            project_id=repository.project.id,
            type=ProjectEventType.MANUAL_NOTE,
            source=ProjectEventSource.SYSTEM,
            payload={"summary": f"relevant {index}", "related_components": ["api"]},
        )
        for index in range(5)
    ]
    unrelated = [
        ProjectEvent(
            id=f"event-unrelated-newer-{index}",
            project_id=repository.project.id,
            type=ProjectEventType.MANUAL_NOTE,
            source=ProjectEventSource.SYSTEM,
            payload={"summary": f"unrelated {index}", "related_components": ["search"]},
        )
        for index in range(60)
    ]
    repository.events = [*relevant, *unrelated]

    manifest = build_agent_context_manifest(repository, repository.project.id, request())

    assert {item["event_id"] for item in manifest["sections"]["evidence"]} == {
        event.id for event in relevant
    }
    assert "EVIDENCE_COUNT_LIMIT" not in manifest["usage"]["limit_reasons"]


def test_manifest_bounds_event_history_scan_and_reports_possible_older_evidence() -> None:
    repository = ManifestRepository()
    relevant = [
        ProjectEvent(
            id=f"event-relevant-old-{index}",
            project_id=repository.project.id,
            type=ProjectEventType.MANUAL_NOTE,
            source=ProjectEventSource.SYSTEM,
            payload={"summary": f"relevant {index}", "related_components": ["api"]},
        )
        for index in range(5)
    ]
    unrelated = [
        ProjectEvent(
            id=f"event-unrelated-new-{index}",
            project_id=repository.project.id,
            type=ProjectEventType.MANUAL_NOTE,
            source=ProjectEventSource.SYSTEM,
            payload={"summary": f"unrelated {index}", "related_components": ["search"]},
        )
        for index in range(AGENT_CONTEXT_EVENT_SCAN_LIMIT + 4)
    ]
    repository.events = [*relevant, *unrelated]

    manifest = build_agent_context_manifest(repository, repository.project.id, request())

    assert manifest["sections"]["evidence"] == []
    assert manifest["budget"]["max_event_scan_records"] == AGENT_CONTEXT_EVENT_SCAN_LIMIT
    assert manifest["usage"]["truncated"] is True
    assert "EVENT_HISTORY_SCAN_LIMIT" in manifest["usage"]["limit_reasons"]


def test_manifest_reports_every_fixed_count_limit_instead_of_silent_truncation() -> None:
    repository = ManifestRepository()
    repository.tasks = [
        Task(id=f"task-api-{index:02d}", title=f"API task {index}", related_component="api")
        for index in range(25)
    ]
    repository.proposals = [
        ArchitectureChangeProposal(
            id=f"proposal-api-{index:02d}",
            project_id=repository.project.id,
            base_architecture_version=7,
            reason="Review API boundary.",
            evidence=["bounded evidence"],
            observed_change="API boundary changed.",
            affected_components=["api"],
            proposed_changes=[],
            impact="Review required.",
            recommended_option=ArchitectureOption.KEEP_CURRENT,
            status=ProposalStatus.PENDING,
        )
        for index in range(10)
    ]
    evidence_events = [
        ProjectEvent(
            id=f"event-api-{index:02d}",
            project_id=repository.project.id,
            type=ProjectEventType.MANUAL_NOTE,
            source=ProjectEventSource.SYSTEM,
            payload={"summary": f"API evidence {index}", "related_components": ["api"]},
        )
        for index in range(15)
    ]
    code_event = next(
        event for event in repository.events if event.type == ProjectEventType.CODE_ARCHITECTURE_SNAPSHOT
    ).model_copy(deep=True)
    code_event.payload["request"]["code_truth"]["symbols"] = [
        {
            "id": f"symbol-api-{index:02d}",
            "qualified_name": f"api.handle_request_{index}",
            "kind": "function",
            "path": "src/api.py",
            "line_start": 10,
            "line_end": 10,
            "source_evidence_id": "source-api",
            "architecture_component_id": "api",
        }
        for index in range(15)
    ]
    repository.events = [*evidence_events, code_event]

    manifest = build_agent_context_manifest(repository, repository.project.id, request())

    assert len(manifest["sections"]["tasks"]) == 20
    assert len(manifest["sections"]["pending_proposals"]) == 8
    assert len(manifest["sections"]["evidence"]) == 12
    assert len(manifest["sections"]["code_truth"]["chunks"]) == 12
    assert manifest["usage"]["truncated"] is True
    assert {
        "TASK_COUNT_LIMIT",
        "PROPOSAL_COUNT_LIMIT",
        "EVIDENCE_COUNT_LIMIT",
        "CODE_TRUTH_COUNT_LIMIT",
    } <= set(manifest["usage"]["limit_reasons"])


def test_manifest_clips_to_hard_budget_with_explicit_reason_and_stable_hash() -> None:
    repository = ManifestRepository()
    repository.tasks = [
        Task(
            id=f"task-heavy-{index:02d}",
            title=f"Heavy bounded task {index}",
            description="description-" + ("x" * 2_000),
            related_component="api",
            acceptance_criteria=[
                f"criterion-{criterion}-" + ("y" * 500)
                for criterion in range(6)
            ],
        )
        for index in range(20)
    ]
    architecture_before = repository.architecture.model_dump(mode="json")

    first = build_agent_context_manifest(repository, repository.project.id, request())
    second = build_agent_context_manifest(repository, repository.project.id, request())

    assert first == second
    assert first["usage"]["truncated"] is True
    assert "TASK_BUDGET" in first["usage"]["limit_reasons"]
    assert first["usage"]["context_chars"] <= MAX_CONTEXT_CHARS
    assert first["usage"]["estimated_input_tokens"] <= MAX_ESTIMATED_INPUT_TOKENS
    assert len(first["sections"]["tasks"]) < 20
    assert repository.architecture.model_dump(mode="json") == architecture_before


def test_manifest_bounds_large_relevant_proposals_and_never_leaks_scope_external_ids() -> None:
    repository = ManifestRepository()
    component_ids = ["api"] + [f"component-{index:02d}-" + ("x" * 120) for index in range(39)]
    roots = []
    cursor = 0
    for root_index in range(8):
        root_id = component_ids[cursor]
        cursor += 1
        child_count = 4 if root_index < 7 else 3
        children = [
            Component(
                id=component_ids[cursor + offset],
                name=f"Child {root_index}-{offset}",
                type="service",
                responsibility="Bounded dependency target.",
            )
            for offset in range(child_count)
        ]
        cursor += child_count
        roots.append(
            Component(
                id=root_id,
                name=f"Root {root_index}",
                type="system",
                responsibility="Architecture boundary.",
                children=children,
            )
        )
    flat_ids = [component.id for root in roots for component in [root, *root.children]]
    dependency_ids = flat_ids[1:20]
    repository.architecture = Architecture(
        version=7,
        summary="Large but valid bounded-context fixture.",
        components=roots,
        relationships=[
            Relationship(
                source="api",
                target=component_id,
                relationship_type="CALLS",
            )
            for component_id in dependency_ids
        ],
    )
    repository.tasks = []
    repository.events = []
    repository.proposals = [
        ArchitectureChangeProposal(
            id=f"proposal-heavy-{index}",
            project_id=repository.project.id,
            base_architecture_version=7,
            reason="reason-" + ("r" * 2_000),
            evidence=["bounded evidence"],
            observed_change="observed-" + ("o" * 2_000),
            affected_components=list(dependency_ids),
            proposed_changes=[],
            impact="impact-" + ("i" * 2_000),
            recommended_option=ArchitectureOption.KEEP_CURRENT,
            status=ProposalStatus.PENDING,
        )
        for index in range(8)
    ]

    manifest = build_agent_context_manifest(
        repository,
        repository.project.id,
        request("AUTO_BOUNDED").model_copy(update={"node_id": "node:api"}),
    )

    assert manifest["usage"]["context_chars"] <= MAX_CONTEXT_CHARS
    assert manifest["usage"]["estimated_input_tokens"] <= MAX_ESTIMATED_INPUT_TOKENS
    assert "PROPOSAL_BUDGET" in manifest["usage"]["limit_reasons"]
    assert len(manifest["sections"]["pending_proposals"]) < 8
    allowed = {
        "api",
        *(
            node["component_id"]
            for node in manifest["sections"]["architecture"]["dependency_context"]["nodes"]
        ),
    }
    assert all(
        set(proposal["affected_components"]) <= allowed
        for proposal in manifest["sections"]["pending_proposals"]
    )


def test_manifest_preserves_in_scope_component_binding_from_proposed_changes() -> None:
    repository = ManifestRepository()
    repository.proposals = [
        ArchitectureChangeProposal(
            id="proposal-change-only-binding",
            project_id=repository.project.id,
            base_architecture_version=7,
            reason="The API implementation boundary changed.",
            evidence=["Observed implementation evidence."],
            observed_change="The proposed change targets the API.",
            affected_components=["search"],
            proposed_changes=[{"component_id": "api", "change": "split"}],
            impact="Review the API boundary.",
            recommended_option=ArchitectureOption.KEEP_CURRENT,
            status=ProposalStatus.PENDING,
        )
    ]

    manifest = build_agent_context_manifest(
        repository,
        repository.project.id,
        request(),
    )

    assert manifest["sections"]["pending_proposals"] == [
        {
            "id": "proposal-change-only-binding",
            "reason": "The API implementation boundary changed.",
            "observed_change": "The proposed change targets the API.",
            "affected_components": ["api"],
            "impact": "Review the API boundary.",
            "status": "PENDING",
        }
    ]


def test_finalized_manifest_never_grows_past_hard_context_budget() -> None:
    from archbro.backend.agent import context_manifest as context_manifest_module

    manifest = {
        "schema": "archbro.agent_context_manifest.v1",
        "sections": {
            "project": {"goal": "", "architecture_summary": ""},
            "architecture": {
                "dependency_context": {
                    "nodes": [],
                    "relationships": [],
                    "counts": {"nodes": 0, "relationships": 0, "max_hop": 0},
                    "truncated": False,
                    "limit_reason": None,
                }
            },
            "tasks": [],
            "pending_proposals": [],
            "evidence": [],
            "code_truth": {"chunks": []},
        },
        "budget": {
            "max_chars": MAX_CONTEXT_CHARS,
            "max_estimated_input_tokens": MAX_ESTIMATED_INPUT_TOKENS,
        },
        "usage": {
            "context_chars": 0,
            "estimated_input_tokens": 0,
            "task_count": 0,
            "evidence_count": 0,
            "code_truth_chunk_count": 0,
            "truncated": False,
            "limit_reasons": [],
        },
        "manifest_hash": "0" * 64,
    }
    base_size = len(context_manifest_module._canonical_json(manifest))
    manifest["sections"]["project"]["goal"] = "x" * (MAX_CONTEXT_CHARS - base_size)
    assert len(context_manifest_module._canonical_json(manifest)) == MAX_CONTEXT_CHARS

    finalized = context_manifest_module._finalize_manifest(manifest)
    final_size = len(context_manifest_module._canonical_json(finalized))

    assert final_size <= MAX_CONTEXT_CHARS
    assert finalized["usage"]["context_chars"] == final_size
    assert finalized["usage"]["estimated_input_tokens"] == (final_size + 3) // 4


def test_architecture_budget_trim_recomputes_expansion_count_after_node_removal(monkeypatch) -> None:
    from archbro.backend.agent import context_manifest as context_manifest_module

    manifest = {
        "schema": "archbro.agent_context_manifest.v1",
        "sections": {
            "project": {"goal": "", "architecture_summary": ""},
            "architecture": {
                "dependency_context": {
                    "nodes": [
                        {"node_id": "node:near", "hop": 1, "padding": "near"},
                        {"node_id": "node:deep", "hop": 3, "padding": "x" * 5_000},
                    ],
                    "relationships": [],
                    "counts": {"nodes": 2, "relationships": 0, "max_hop": 3},
                    "truncated": False,
                    "limit_reason": None,
                }
            },
            "tasks": [],
            "pending_proposals": [],
            "evidence": [],
            "code_truth": {"chunks": []},
        },
        "budget": {"max_chars": MAX_CONTEXT_CHARS, "max_estimated_input_tokens": MAX_ESTIMATED_INPUT_TOKENS},
        "usage": {
            "context_chars": 0,
            "estimated_input_tokens": 0,
            "task_count": 0,
            "evidence_count": 0,
            "code_truth_chunk_count": 0,
            "expansion_count": 2,
            "truncated": False,
            "limit_reasons": [],
        },
        "manifest_hash": "0" * 64,
    }
    without_deep = json.loads(json.dumps(manifest))
    without_deep["sections"]["architecture"]["dependency_context"]["nodes"].pop()
    target_budget = len(context_manifest_module._canonical_json(without_deep)) + 200
    monkeypatch.setattr(context_manifest_module, "MAX_CONTEXT_CHARS", target_budget)

    context_manifest_module._trim_to_budget(manifest)

    dependency = manifest["sections"]["architecture"]["dependency_context"]
    assert [node["node_id"] for node in dependency["nodes"]] == ["node:near"]
    assert dependency["counts"]["max_hop"] == 1
    assert manifest["usage"]["expansion_count"] == 0
    assert "ARCHITECTURE_BUDGET" in manifest["usage"]["limit_reasons"]


def test_policy_changes_only_the_bounded_query_contract() -> None:
    repository = ManifestRepository()
    ask = build_agent_context_manifest(repository, repository.project.id, request("ASK_ALL"))
    neighborhood = build_agent_context_manifest(
        repository,
        repository.project.id,
        request("ALLOW_NEIGHBORHOOD"),
    )
    automatic = build_agent_context_manifest(
        repository,
        repository.project.id,
        request("AUTO_BOUNDED"),
    )

    assert ask["selection"]["effective_max_hops"] == 1
    assert ask["selection"]["effective_max_results"] == 8
    assert neighborhood["selection"]["effective_max_hops"] == 2
    assert neighborhood["selection"]["effective_max_results"] == 14
    assert automatic["selection"]["effective_max_hops"] == 3
    assert automatic["selection"]["effective_max_results"] == 20
    assert len(
        {
            ask["manifest_hash"],
            neighborhood["manifest_hash"],
            automatic["manifest_hash"],
        }
    ) == 3
    assert automatic["usage"]["selected_node_count"] == 1


def test_execution_requires_the_exact_current_preview_hash() -> None:
    repository = ManifestRepository()
    manifest = build_agent_context_manifest(repository, repository.project.id, request())
    execution = AgentContextExecutionRequest(
        **request().model_dump(),
        preview_manifest_hash=manifest["manifest_hash"],
    )
    prepared = prepare_agent_context_event_payload(
        repository,
        repository.project.id,
        {"message": "Review this boundary.", "agent_context_request": execution.model_dump()},
    )
    assert prepared["agent_context_manifest"] == manifest

    repository.tasks.append(
        Task(id="task-api-new", title="New bounded fact", related_component="api")
    )
    with pytest.raises(AgentContextPreviewStaleError):
        prepare_agent_context_event_payload(
            repository,
            repository.project.id,
            {"message": "Review this boundary.", "agent_context_request": execution.model_dump()},
        )

    with pytest.raises(ValueError, match="server-owned"):
        prepare_agent_context_event_payload(
            repository,
            repository.project.id,
            {"agent_context_manifest": manifest},
        )


def test_manifest_fails_closed_on_stale_architecture_version() -> None:
    repository = ManifestRepository()
    stale = request().model_copy(update={"expected_architecture_version": 6})
    with pytest.raises(StaleArchitectureVersionError):
        build_agent_context_manifest(repository, repository.project.id, stale)


def test_gemini_context_uses_manifest_instead_of_full_project_state() -> None:
    repository = ManifestRepository()
    manifest = build_agent_context_manifest(repository, repository.project.id, request())
    context = ProjectContext(
        project=repository.project,
        architecture=repository.architecture,
        tasks=repository.tasks,
        pending_proposals=repository.proposals,
        recent_notes=["unbounded project note"],
    )
    compact = _compact_context_facts(context, agent_context_manifest=manifest)
    assert compact == {"agent_context_manifest": manifest}
    assert compact["agent_context_manifest"] == manifest
    assert "project" not in compact
    assert "architecture" not in compact
    assert "tasks" not in compact
    assert "pending_proposals" not in compact
    assert "recent_notes" not in compact
    assert "task-unrelated" not in json.dumps(compact, sort_keys=True)

    event = ProjectEvent(
        project_id=repository.project.id,
        type=ProjectEventType.USER_MESSAGE,
        payload={"message": "Review this.", "agent_context_manifest": manifest},
    )
    compact_event = _compact_event_facts(event)
    assert "agent_context_manifest" not in compact_event["payload"]
    assert compact_event["payload"]["agent_context_manifest_hash"] == manifest["manifest_hash"]


@dataclass(frozen=True)
class UsageFixture:
    input_tokens: int = 321
    output_tokens: int = 42


class ProviderFixture:
    last_usage = UsageFixture()


class RecordingProvider(FakeModelProvider):
    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, **kwargs):
        self.calls += 1
        return await super().generate(**kwargs)


def test_context_and_provider_telemetry_are_serializable() -> None:
    repository = ManifestRepository()
    manifest = build_agent_context_manifest(repository, repository.project.id, request())
    event = ProjectEvent(
        project_id=repository.project.id,
        type=ProjectEventType.USER_MESSAGE,
        payload={"agent_context_manifest": manifest},
    )
    context_telemetry = _context_telemetry(event)
    assert context_telemetry is not None
    assert context_telemetry["manifest_hash"] == manifest["manifest_hash"]
    assert context_telemetry["selected_node_count"] == 1
    assert _provider_usage(ProviderFixture()) == {"input_tokens": 321, "output_tokens": 42}


def test_preview_endpoint_uses_read_authorization_and_stale_contract() -> None:
    repository = ManifestRepository()

    async def authorize(_request, project_id, _permission):
        return repository.get_project(project_id)

    app = FastAPI()
    app.include_router(build_agent_surface_router(repository, authorize))
    client = TestClient(app)

    preview = client.post(
        f"/projects/{repository.project.id}/agent-context/manifest",
        json=request().model_dump(),
    )
    assert preview.status_code == 200
    assert preview.json()["schema"] == "archbro.agent_context_manifest.v1"

    stale = client.post(
        f"/projects/{repository.project.id}/agent-context/manifest",
        json=request().model_copy(update={"expected_architecture_version": 6}).model_dump(),
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["error"] == "stale_architecture_version"


def test_stale_preview_fails_before_provider_and_preserves_architecture_without_database() -> None:
    repository = ManifestRepository()
    provider = RecordingProvider()
    app = FastAPI()
    app.include_router(build_router(repository, provider))
    client = TestClient(app)

    preview = build_agent_context_manifest(repository, repository.project.id, request())
    architecture_before = repository.architecture.model_dump(mode="json")
    repository.tasks.append(
        Task(id="task-api-after-preview", title="New bounded fact", related_component="api")
    )

    response = client.post(
        f"/projects/{repository.project.id}/events",
        json={
            "type": "USER_MESSAGE",
            "source": "FRONTEND",
            "payload": {
                "message": "Review this boundary.",
                "agent_context_request": {
                    **request().model_dump(),
                    "preview_manifest_hash": preview["manifest_hash"],
                },
            },
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "agent_context_preview_stale"
    assert provider.calls == 0
    assert repository.architecture.model_dump(mode="json") == architecture_before
