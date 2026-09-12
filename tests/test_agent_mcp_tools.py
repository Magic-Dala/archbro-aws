from __future__ import annotations

import asyncio
from types import MethodType, SimpleNamespace

import pytest

from archbro.backend.agent.orchestration import AgentOrchestrator
from archbro.backend.api.provider_connections import ProviderMcpRuntimeRegistry
from archbro.backend.core.authorization import TrustedPrincipal
from archbro.backend.core.contracts import (
    AgentAction,
    AgentActionType,
    AgentDecision,
    Architecture,
    ObservationClaim,
    ObservationClaimState,
    Project,
    ProjectContext,
    ProjectEvent,
    ProjectEventType,
)
from archbro.backend.core.github_repository import GitHubRepositoryBinding
from archbro.backend.core.evaluation import (
    DriftClassification,
    DriftEvaluation,
    DriftRecommendedAction,
)
from archbro.backend.llm.gemini import GeminiDecisionWire, GeminiProvider
from archbro.backend.llm.provider import ModelProvider
from archbro.backend.mcp.agent_tools import (
    AgentMcpToolSession,
    repository_evidence_requested,
)

PRIVATE_MARKER = "PRIVATE_REPOSITORY_SENTINEL"


class _FakeGitHubGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.list_connections_calls = 0
        self.list_tools_calls = 0

    def list_connections(self):
        self.list_connections_calls += 1
        return [
            {
                "id": "mcp_private",
                "name": "GitHub",
                "provider": "github",
                "authorization_pending": False,
            }
        ]

    def list_tools(self, connection_id: str):
        assert connection_id == "mcp_private"
        self.list_tools_calls += 1
        return {
            "tool_count": 4,
            "tools": [
                {
                    "name": "get_file_contents",
                    "description": "Read a file from a GitHub repository.",
                    "annotations": {"readOnlyHint": True},
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "owner": {"type": "string"},
                            "repo": {"type": "string"},
                            "path": {"type": "string"},
                            "ref": {"type": "string"},
                        },
                        "required": ["owner", "repo", "path"],
                    },
                },
                {
                    "name": "issue_read",
                    "description": "Read an issue.",
                    "annotations": {"readOnlyHint": True},
                    "inputSchema": {"type": "object", "properties": {}},
                },
                {
                    "name": "create_or_update_file",
                    "annotations": {"readOnlyHint": False},
                    "inputSchema": {"type": "object", "properties": {}},
                },
                {
                    "name": "unexpected_read_tool",
                    "annotations": {"readOnlyHint": True},
                    "inputSchema": {"type": "object", "properties": {}},
                },
            ],
        }

    def call_tool(self, connection_id: str, tool_name: str, arguments: dict):
        self.calls.append((connection_id, tool_name, dict(arguments)))
        return {
            "connection_id": connection_id,
            "tool_name": tool_name,
            "external_evidence": {
                "content": f"# ArchBro\n{PRIVATE_MARKER}",
                "sha": "17f39913dfb62466bf205c49d42939a6b0a860a8",
            },
            "canonical_state_mutated": False,
        }


class _ToolErrorGitHubGateway(_FakeGitHubGateway):
    def call_tool(self, connection_id: str, tool_name: str, arguments: dict):
        self.calls.append((connection_id, tool_name, dict(arguments)))
        return {
            "connection_id": connection_id,
            "tool_name": tool_name,
            "external_evidence": {
                "content": [
                    {
                        "type": "text",
                        "text": "Repository lookup failed for Bearer secret-value-123456",
                    }
                ],
                "isError": True,
            },
            "canonical_state_mutated": False,
        }


class _NoConnectionGateway:
    def list_connections(self):
        return []


class _BrokenDiscoveryGateway:
    def list_connections(self):
        raise RuntimeError("Bearer github_pat_abcdefghijklmnopqrstuvwxyz123456")


class _MalformedConnectionGateway:
    def list_connections(self):
        return [
            {
                "id": "",
                "name": "GitHub",
                "provider": "github",
                "authorization_pending": False,
            }
        ]


def _binding(name: str = "Magic-Dala/archbro", branch: str | None = "dev2") -> GitHubRepositoryBinding:
    return GitHubRepositoryBinding(
        full_name=name,
        branch=branch,
        selected_by_user_id="owner",
    )


def _verification_event(project_id: str = "project_test") -> ProjectEvent:
    return ProjectEvent(
        project_id=project_id,
        type=ProjectEventType.USER_MESSAGE,
        payload={
            "message": (
                "Use GitHub MCP to read the private Magic-Dala/archbro README.md "
                "on dev2 and verify the repository evidence."
            )
        },
    )


def _session(project_id: str = "project_test") -> tuple[AgentMcpToolSession, _FakeGitHubGateway]:
    gateway = _FakeGitHubGateway()
    session = AgentMcpToolSession.discover(
        gateway,
        project_id=project_id,
        event=_verification_event(project_id),
        repository_scope=_binding(),
    )
    assert session is not None
    return session, gateway


def _aligned_wire(summary: str) -> GeminiDecisionWire:
    return GeminiDecisionWire(
        summary=summary,
        evaluation=DriftEvaluation(
            classification=DriftClassification.ALIGNED,
            summary="The accepted architecture remains aligned.",
            evidence=[],
            affected_components=[],
            affected_tasks=[],
            architecture_change_required=False,
            recommended_action=DriftRecommendedAction.NO_ACTION,
        ),
        actions=[AgentAction(type=AgentActionType.NO_ACTION)],
    )


def _project_context() -> ProjectContext:
    project = Project(
        name="Agent MCP test",
        goal="Use authenticated repository evidence safely.",
        architecture_version=1,
    )
    return ProjectContext(
        project=project,
        architecture=Architecture(version=1, summary="Accepted architecture"),
        tasks=[],
        pending_proposals=[],
    )


def _gemini_provider() -> GeminiProvider:
    provider = object.__new__(GeminiProvider)
    provider.model_id = "gemini-test"
    provider.last_model_id = provider.model_id
    provider.last_usage = None
    provider._base_url = None
    provider.fallback_model_ids = ()
    provider.routine_model_id = "gemini-routine-test"
    provider.routine_fallback_model_ids = ()
    provider.routine_model_timeout_seconds = 1.0
    provider.interaction_model_timeout_seconds = 1.0
    provider.interaction_total_timeout_seconds = 3.0
    provider.architecture_model_timeout_seconds = 1.0
    provider.architecture_total_timeout_seconds = 2.0
    return provider


def test_repository_evidence_intent_is_conservative_and_honors_opt_out() -> None:
    assert repository_evidence_requested(_verification_event()) is True

    ordinary = ProjectEvent(
        project_id="project_test",
        type=ProjectEventType.USER_MESSAGE,
        payload={"message": "Let's discuss the repository architecture."},
    )
    assert repository_evidence_requested(ordinary) is False
    gateway = _FakeGitHubGateway()
    assert AgentMcpToolSession.discover(
        gateway,
        project_id="project_test",
        event=ordinary,
    ) is None
    assert gateway.list_tools_calls == 0

    report_status = ProjectEvent(
        project_id="project_test",
        type=ProjectEventType.USER_MESSAGE,
        payload={"message": "Review the report task status and summarize progress."},
    )
    assert repository_evidence_requested(report_status) is False

    explicit_repo = ProjectEvent(
        project_id="project_test",
        type=ProjectEventType.USER_MESSAGE,
        payload={"message": "Review the repo status and verify the latest commit."},
    )
    assert repository_evidence_requested(explicit_repo) is True

    explicit_pr = ProjectEvent(
        project_id="project_test",
        type=ProjectEventType.USER_MESSAGE,
        payload={"message": "Show PR #70 and summarize the changes."},
    )
    assert repository_evidence_requested(explicit_pr) is True

    opted_out = ProjectEvent(
        project_id="project_test",
        type=ProjectEventType.USER_MESSAGE,
        payload={"message": "Do not use GitHub; only review the repo idea from current context."},
    )
    assert repository_evidence_requested(opted_out) is False


def test_session_discovers_bounded_read_only_tools_calls_private_evidence_and_redacts() -> None:
    session, gateway = _session()

    assert session.requires_successful_call is True
    assert [descriptor.name for descriptor in session.descriptors] == [
        "get_file_contents",
        "issue_read",
    ]

    tool = session.strands_tools()[0]
    assert tool.tool_spec["name"] == "get_file_contents"
    result = tool(
        owner="Magic-Dala",
        repo="archbro",
        path="README.md",
        ref="refs/heads/dev2",
    )
    assert PRIVATE_MARKER in str(result["external_evidence"])
    assert result["external_evidence"]["content"].endswith(PRIVATE_MARKER)
    assert "connection_id" not in result["source"]
    assert result["canonical_state_mutated"] is False
    assert gateway.calls == [
        (
            "mcp_private",
            "get_file_contents",
            {
                "owner": "Magic-Dala",
                "repo": "archbro",
                "path": "README.md",
                "ref": "refs/heads/dev2",
            },
        )
    ]

    # Strands invokes dynamically-schema'd **kwargs tools with one additional
    # ``arguments`` envelope. The adapter must remove that implementation
    # wrapper before dispatching the provider-native schema.
    wrapped = tool(
        arguments={
            "owner": "Magic-Dala",
            "repo": "archbro",
            "path": "docs/README.md",
            "ref": "refs/heads/dev2",
        }
    )
    assert PRIVATE_MARKER in str(wrapped["external_evidence"])
    assert gateway.calls[-1] == (
        "mcp_private",
        "get_file_contents",
        {
            "owner": "Magic-Dala",
            "repo": "archbro",
            "path": "docs/README.md",
            "ref": "refs/heads/dev2",
        },
    )

    session.call(
        "get_file_contents",
        {
            "owner": "Magic-Dala",
            "repo": "archbro",
            "path": "README.md",
            "query": "Bearer github_pat_abcdefghijklmnopqrstuvwxyz123456",
            "access_token": "github_pat_abcdefghijklmnopqrstuvwxyz123456",
        },
    )
    telemetry = session.telemetry()
    assert telemetry["successful_call_count"] == 3
    assert telemetry["dispatched_call_count"] == 3
    assert PRIVATE_MARKER not in str(telemetry)
    assert "github_pat_abcdefghijklmnopqrstuvwxyz123456" not in str(telemetry)
    assert "<redacted>" in str(telemetry)
    assert session.evidence_references()[0].startswith(
        "GitHub MCP get_file_contents: Magic-Dala/archbro"
    )

    with pytest.raises(ValueError, match="scope_mismatch"):
        tool(arguments={"repo": "other", "path": "README.md"})
    with pytest.raises(ValueError, match="no fallback"):
        tool(path="README.md")
    assert len(gateway.calls) == 3
    assert session.evidence_references() == []


def test_provider_runtime_registry_reuses_only_the_authenticated_principals_gateway(
    monkeypatch,
) -> None:
    registry = ProviderMcpRuntimeRegistry()
    owner = TrustedPrincipal(user_id="owner")
    other = TrustedPrincipal(user_id="other")
    owner_gateway, _ = registry.runtime_for(owner)
    fake = _FakeGitHubGateway()
    monkeypatch.setattr(owner_gateway, "list_connections", fake.list_connections)
    monkeypatch.setattr(owner_gateway, "list_tools", fake.list_tools)
    monkeypatch.setattr(owner_gateway, "call_tool", fake.call_tool)

    session = registry.agent_tool_session(
        owner,
        project_id="project_test",
        event=_verification_event(),
        repository_scope=_binding(),
    )

    assert session is not None
    assert session.gateway is owner_gateway
    other_session = registry.agent_tool_session(
        other,
        project_id="project_test",
        event=_verification_event(),
        repository_scope=_binding(),
    )
    assert other_session is not None
    assert other_session.gateway is not owner_gateway
    assert other_session.has_tools is False
    assert "No ready GitHub MCP connection" in str(other_session.discovery_error)
    assert "could not be completed" in other_session.prompt_context()


def test_session_enforces_distinct_dispatch_budget(monkeypatch) -> None:
    monkeypatch.setenv("ARCHBRO_AGENT_MCP_MAX_CALLS", "1")
    session, gateway = _session()

    session.call(
        "get_file_contents",
        {"owner": "Magic-Dala", "repo": "archbro", "path": "README.md"},
    )
    with pytest.raises(RuntimeError, match="call budget exhausted"):
        session.call(
            "issue_read",
            {"owner": "Magic-Dala", "repo": "archbro", "issue_number": 68},
        )

    assert len(gateway.calls) == 1
    assert session.telemetry()["dispatched_call_count"] == 1


def test_mcp_is_error_result_is_not_successful_cached_or_usable_as_evidence() -> None:
    gateway = _ToolErrorGitHubGateway()
    session = AgentMcpToolSession.discover(
        gateway,
        project_id="project_test",
        event=_verification_event(),
        repository_scope=_binding(),
    )
    assert session is not None
    arguments = {
        "owner": "Magic-Dala",
        "repo": "archbro",
        "path": "README.md",
        "ref": "refs/heads/dev2",
    }

    with pytest.raises(RuntimeError, match="returned isError=true") as first_error:
        session.call("get_file_contents", arguments)

    assert "secret-value-123456" not in str(first_error.value)
    telemetry = session.telemetry()
    assert telemetry["successful_call_count"] == 0
    assert telemetry["dispatched_call_count"] == 1
    assert telemetry["calls"][0]["status"] == "ERROR"
    assert session.evidence_references() == []

    # A protocol-level tool error must not enter the success cache. Repeating the
    # exact invocation dispatches again and still cannot satisfy verification.
    with pytest.raises(RuntimeError, match="returned isError=true"):
        session.call("get_file_contents", arguments)
    assert len(gateway.calls) == 2
    assert session.successful_call_count == 0


def test_cache_identity_uses_complete_arguments_not_bounded_telemetry() -> None:
    session, gateway = _session()
    shared_prefix = "x" * 2100

    session.call(
        "get_file_contents",
        {
            "owner": "Magic-Dala",
            "repo": "archbro",
            "path": "README.md",
            "query": shared_prefix + "first",
        },
    )
    session.call(
        "get_file_contents",
        {
            "owner": "Magic-Dala",
            "repo": "archbro",
            "path": "README.md",
            "query": shared_prefix + "second",
        },
    )

    assert len(gateway.calls) == 2
    assert session.telemetry()["dispatched_call_count"] == 2


def test_discovery_failures_stay_fail_closed_and_redact_secrets(monkeypatch) -> None:
    broken = AgentMcpToolSession.discover(
        _BrokenDiscoveryGateway(),
        project_id="project_test",
        event=_verification_event(),
        repository_scope=_binding(),
    )
    assert broken is not None
    assert broken.has_tools is False
    assert "<redacted>" in str(broken.discovery_error)
    assert "github_pat_abcdefghijklmnopqrstuvwxyz123456" not in str(broken.discovery_error)

    malformed = AgentMcpToolSession.discover(
        _MalformedConnectionGateway(),
        project_id="project_test",
        event=_verification_event(),
        repository_scope=_binding(),
    )
    assert malformed is not None
    assert malformed.has_tools is False
    assert "no usable connection id" in str(malformed.discovery_error)

    monkeypatch.setenv("ARCHBRO_AGENT_MCP_MAX_CALLS", "999")
    bounded, _ = _session()
    assert bounded.max_calls == 6


def test_gemini_invoke_passes_supplied_tools_to_agent() -> None:
    provider = _gemini_provider()
    sentinel_tool = object()
    captured: dict[str, object] = {}

    class FakeAgent:
        async def invoke_async(self, prompt: str, *, structured_output_model):
            captured["prompt"] = prompt
            captured["structured_output_model"] = structured_output_model
            return SimpleNamespace(
                structured_output=_aligned_wire("Verified through the supplied tool."),
                metrics=None,
            )

    def fake_agent_for(self, model_id: str, *, tools=None):
        captured["model_id"] = model_id
        captured["tools"] = tools
        return FakeAgent()

    provider._agent_for = MethodType(fake_agent_for, provider)

    wire = asyncio.run(
        provider._invoke("gemini-test", "verify repository", tools=[sentinel_tool])
    )

    assert captured["model_id"] == "gemini-test"
    assert captured["tools"] == [sentinel_tool]
    assert captured["prompt"] == "verify repository"
    assert captured["structured_output_model"] is GeminiDecisionWire
    assert wire.summary == "Verified through the supplied tool."


def test_gemini_retries_when_first_structured_answer_skips_required_mcp_call() -> None:
    provider = _gemini_provider()
    session, gateway = _session()
    context = _project_context()
    event = _verification_event(context.project.id)
    invocations: list[tuple[str, list]] = []

    async def fake_invoke(self, model_id: str, prompt: str, *, tools=None):
        invocations.append((prompt, list(tools or [])))
        if len(invocations) == 1:
            return _aligned_wire("ALIGNED / NO_ACTION")
        session.call(
            "get_file_contents",
            {
                "owner": "Magic-Dala",
                "repo": "archbro",
                "path": "README.md",
                "ref": "refs/heads/dev2",
            },
        )
        return _aligned_wire(f"The private README contains {PRIVATE_MARKER}.")

    provider._invoke = MethodType(fake_invoke, provider)

    decision = asyncio.run(
        provider.generate_with_external_tools(
            event=event,
            context=context,
            system_prompt="test system prompt",
            external_tools=session,
        )
    )

    assert len(invocations) == 2
    assert "CONNECTED READ-ONLY MCP EVIDENCE" in invocations[0][0]
    assert "REQUIRED VERIFICATION RETRY" in invocations[1][0]
    assert [tool.tool_spec["name"] for tool in invocations[0][1]] == [
        "get_file_contents",
        "issue_read",
    ]
    assert gateway.calls[0][1] == "get_file_contents"
    assert session.successful_call_count == 1
    assert PRIVATE_MARKER in decision.summary


class _MemoryRepository:
    def __init__(self, context: ProjectContext) -> None:
        self.context = context
        self.committed = None
        self.failed = None

    def get_project(self, project_id: str):
        assert project_id == self.context.project.id
        return self.context.project

    def get_architecture(self, project_id: str):
        assert project_id == self.context.project.id
        return self.context.architecture

    def load_context(self, project_id: str):
        assert project_id == self.context.project.id
        return self.context

    def list_tasks(self, project_id: str):
        assert project_id == self.context.project.id
        return list(self.context.tasks)

    def claim_observation(self, event: ProjectEvent, *, run_id: str):
        return ObservationClaim(
            state=ObservationClaimState.CLAIMED,
            event=event,
            run_id=run_id,
        )

    def commit_observation_result(self, *, event, run_id, plan, result):
        self.committed = result

    def fail_observation(self, *, event, run_id, result):
        self.failed = result


class _ToolAwareProvider(ModelProvider):
    name = "tool-aware-test"
    model_id = "tool-aware-test-model"

    async def generate(self, *, event, context, system_prompt):
        raise AssertionError("the tool-aware provider path must be used")

    async def generate_with_external_tools(
        self,
        *,
        event,
        context,
        system_prompt,
        external_tools,
    ) -> AgentDecision:
        evidence = external_tools.call(
            "get_file_contents",
            {
                "owner": "Magic-Dala",
                "repo": "archbro",
                "path": "README.md",
                "ref": "refs/heads/dev2",
            },
        )
        assert PRIVATE_MARKER in str(evidence)
        return AgentDecision(
            summary=f"Verified with GitHub MCP: the private README contains {PRIVATE_MARKER}.",
            actions=[AgentAction(type=AgentActionType.NO_ACTION)],
            evaluation=DriftEvaluation(
                classification=DriftClassification.ALIGNED,
                summary="Repository evidence does not require an architecture change.",
                evidence=[],
                affected_components=[],
                affected_tasks=[],
                architecture_change_required=False,
                recommended_action=DriftRecommendedAction.NO_ACTION,
            ),
        )


def test_orchestrator_persists_mcp_call_receipt_and_answers_even_with_no_action() -> None:
    context = _project_context()
    repository = _MemoryRepository(context)
    session, _ = _session(context.project.id)
    orchestrator = AgentOrchestrator(repository, _ToolAwareProvider())

    result = asyncio.run(
        orchestrator.observe_event(
            _verification_event(context.project.id),
            external_tools=session,
        )
    )

    assert result.result == "SUCCESS"
    assert result.actions == [AgentAction(type=AgentActionType.NO_ACTION)]
    assert PRIVATE_MARKER in result.summary
    assert "Verified sources: GitHub MCP get_file_contents" in result.summary
    assert result.evaluation is not None
    assert result.evaluation.evidence[0].startswith(
        "GitHub MCP get_file_contents: Magic-Dala/archbro"
    )
    assert result.context_telemetry is not None
    assert result.context_telemetry["external_mcp"]["successful_call_count"] == 1
    assert repository.committed == result
    assert repository.failed is None


def test_orchestrator_fails_closed_without_a_ready_github_connection() -> None:
    context = _project_context()
    repository = _MemoryRepository(context)
    session = AgentMcpToolSession.discover(
        _NoConnectionGateway(),
        project_id=context.project.id,
        event=_verification_event(context.project.id),
        repository_scope=_binding(),
    )
    assert session is not None
    assert session.has_tools is False
    orchestrator = AgentOrchestrator(repository, _ToolAwareProvider())

    result = asyncio.run(
        orchestrator.observe_event(
            _verification_event(context.project.id),
            external_tools=session,
        )
    )

    assert result.result == "SUCCESS"
    assert result.provider == "deterministic"
    assert result.model == "github-mcp-unavailable"
    assert "verification could not be completed" in result.summary
    assert PRIVATE_MARKER not in result.summary
    assert result.evaluation is not None
    assert result.evaluation.classification == DriftClassification.INSUFFICIENT_EVIDENCE
    assert result.evaluation.recommended_action == DriftRecommendedAction.KEEP_CURRENT
    assert result.actions == [AgentAction(type=AgentActionType.NO_ACTION)]
    assert result.context_telemetry is not None
    assert result.context_telemetry["external_mcp"]["discovery_status"] == "UNAVAILABLE"
    assert result.context_telemetry["external_mcp"]["successful_call_count"] == 0
    assert repository.committed == result
    assert repository.failed is None
