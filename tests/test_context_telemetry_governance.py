from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from archbro.backend.agent.orchestration import _context_telemetry, _provider_usage
from archbro.backend.core.contracts import (
    AgentAction,
    AgentActionType,
    AgentDecision,
    ArchitectureChangeProposal,
    ArchitectureOption,
    ProjectEvent,
    ProjectEventType,
    ProposalStatus,
)
from archbro.backend.llm.gemini import GeminiProvider


def test_context_telemetry_keeps_bounded_counts_ids_and_hash_only() -> None:
    event = ProjectEvent(
        project_id="project-telemetry",
        type=ProjectEventType.USER_MESSAGE,
        payload={
            "agent_context_manifest": {
                "manifest_hash": "a" * 64,
                "architecture_version": 9,
                "selection": {"node_id": "node:api", "expansion_policy": "ASK_ALL"},
                "usage": {
                    "selected_node_count": 1,
                    "context_chars": 1200,
                    "estimated_input_tokens": 300,
                    "evidence_count": 2,
                    "mcp_result_count": 1,
                    "code_truth_chunk_count": 3,
                    "expansion_count": 0,
                    "truncated": True,
                    "limit_reasons": ["EVIDENCE_BUDGET"],
                },
                "sections": {"evidence": ["must-not-be-copied"]},
            }
        },
    )

    telemetry = _context_telemetry(event)

    assert telemetry is not None
    assert telemetry["manifest_hash"] == "a" * 64
    assert telemetry["selection"]["node_id"] == "node:api"
    assert telemetry["selected_node_count"] == 1
    assert telemetry["context_chars"] == 1200
    assert telemetry["estimated_input_tokens"] == 300
    assert telemetry["evidence_count"] == 2
    assert telemetry["mcp_result_count"] == 1
    assert telemetry["code_truth_chunk_count"] == 3
    assert telemetry["expansion_count"] == 0
    assert telemetry["limit_reasons"] == ["EVIDENCE_BUDGET"]
    assert "sections" not in telemetry


def test_provider_usage_failure_is_unavailable_instead_of_failing_agent_flow() -> None:
    class ExplodingUsageProvider:
        @property
        def last_usage(self):
            raise RuntimeError("telemetry backend failed")

    assert _provider_usage(ExplodingUsageProvider()) is None


def test_gemini_usage_never_fabricates_zero_for_missing_provider_metrics() -> None:
    provider = object.__new__(GeminiProvider)
    provider._base_url = None
    provider.last_usage = None

    missing = SimpleNamespace(metrics=SimpleNamespace(accumulated_usage=None))
    provider._record_usage("gemini-test", missing, time.perf_counter())
    assert provider.last_usage is None

    empty = SimpleNamespace(metrics=SimpleNamespace(accumulated_usage={}))
    provider._record_usage("gemini-test", empty, time.perf_counter())
    assert provider.last_usage is None

    partial = SimpleNamespace(
        metrics=SimpleNamespace(accumulated_usage={"inputTokens": 0, "outputTokens": 7})
    )
    provider._record_usage("gemini-test", partial, time.perf_counter())
    assert provider.last_usage is not None
    assert provider.last_usage.input_tokens == 0
    assert provider.last_usage.output_tokens == 7
    assert provider.last_usage.total_tokens is None
    assert provider.last_usage.cache_read_input_tokens is None
    assert provider.last_usage.cache_write_input_tokens is None


def test_structural_recommendation_defaults_pending_and_requires_human_review_gate() -> None:
    proposal = ArchitectureChangeProposal(
        project_id="project-governance",
        reason="Observed structural mismatch.",
        evidence=["source-backed evidence"],
        observed_change="Service boundary differs from accepted Architecture.",
        affected_components=["api"],
        proposed_changes=[{"component_id": "api", "change": "split"}],
        impact="Human review required before accepted Architecture changes.",
        recommended_option=ArchitectureOption.ACCEPT_PROPOSED_CHANGE,
    )
    assert proposal.status == ProposalStatus.PENDING

    action = AgentAction(
        type=AgentActionType.PROPOSE_ARCHITECTURE_CHANGE,
        payload={"proposal": proposal.model_dump(mode="json")},
    )
    with pytest.raises(ValueError, match="architecture proposal requires architecture_review_required=true"):
        AgentDecision(
            summary="Structural mismatch detected.",
            actions=[action],
            architecture_review_required=False,
            evaluation=None,
        )
