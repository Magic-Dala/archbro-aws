from __future__ import annotations

import asyncio
from types import MethodType, SimpleNamespace

import pytest

from archbro.backend.core.contracts import (
    AgentAction,
    AgentActionType,
    Architecture,
    Project,
    ProjectContext,
    ProjectEvent,
    ProjectEventType,
)
from archbro.backend.core.evaluation import (
    DriftClassification,
    DriftEvaluation,
    DriftRecommendedAction,
)
from archbro.backend.llm.gemini import GeminiDecisionWire, GeminiProvider


def _wire(summary: str) -> GeminiDecisionWire:
    return GeminiDecisionWire(
        summary=summary,
        evaluation=DriftEvaluation(
            classification=DriftClassification.ALIGNED,
            summary=summary,
            recommended_action=DriftRecommendedAction.NO_ACTION,
        ),
        actions=[AgentAction(type=AgentActionType.NO_ACTION)],
    )


def _provider() -> GeminiProvider:
    provider = object.__new__(GeminiProvider)
    provider.model_id = "arch-model"
    provider.last_model_id = provider.model_id
    provider.last_usage = None
    provider._base_url = None
    provider.fallback_model_ids = ()
    provider.routine_model_id = "routine-model"
    provider.routine_fallback_model_ids = ()
    provider.routine_model_timeout_seconds = 1.0
    provider.interaction_model_timeout_seconds = 1.0
    provider.interaction_total_timeout_seconds = 2.0
    provider.tool_interaction_model_timeout_seconds = 2.0
    provider.tool_interaction_total_timeout_seconds = 3.0
    provider.architecture_model_timeout_seconds = 1.0
    provider.architecture_total_timeout_seconds = 2.0
    return provider


def _context_and_events() -> tuple[ProjectContext, ProjectEvent, ProjectEvent]:
    project = Project(name="provider isolation", goal="verify provider invocation isolation")
    context = ProjectContext(
        project=project,
        architecture=Architecture(version=1),
        tasks=[],
        pending_proposals=[],
    )
    architecture_event = ProjectEvent(
        project_id=project.id,
        type=ProjectEventType.MANUAL_NOTE,
        payload={"note": "architecture path"},
    )
    routine_event = ProjectEvent(
        project_id=project.id,
        type=ProjectEventType.TASK_UPDATED,
        payload={"task_id": "task-test", "status": "DONE", "message": "routine path"},
    )
    return context, architecture_event, routine_event


def test_success_then_failure_clears_usage_before_next_transport() -> None:
    provider = _provider()

    class ControlledAgent:
        def __init__(self) -> None:
            self.calls = 0

        async def invoke_async(self, prompt: str, *, structured_output_model):
            self.calls += 1
            if self.calls == 1:
                return SimpleNamespace(
                    metrics=SimpleNamespace(accumulated_usage={"inputTokens": 2, "outputTokens": 1}),
                    structured_output=_wire("first call succeeded"),
                )
            raise RuntimeError("provider failed before producing usage")

    agent = ControlledAgent()
    provider._agent_for = MethodType(lambda self, model_id: agent, provider)

    async def scenario() -> tuple[str, object | None]:
        await provider._invoke("arch-model", "first")
        first_model = provider.last_usage.model_id if provider.last_usage is not None else ""
        with pytest.raises(RuntimeError, match="provider failed"):
            await provider._invoke("arch-model", "second")
        return first_model, provider.last_usage

    first_model, usage_after_failure = asyncio.run(scenario())
    assert first_model == "arch-model"
    assert usage_after_failure is None


def test_concurrent_generate_invocations_keep_model_and_usage_metadata_isolated() -> None:
    provider = _provider()
    context, architecture_event, routine_event = _context_and_events()

    entered = {"arch-model": asyncio.Event(), "routine-model": asyncio.Event()}
    release = {"arch-model": asyncio.Event(), "routine-model": asyncio.Event()}
    returned = {"arch-model": asyncio.Event(), "routine-model": asyncio.Event()}
    read_gate = asyncio.Event()

    async def fake_invoke(self, model_id: str, prompt: str):
        entered[model_id].set()
        await release[model_id].wait()
        self.last_usage = SimpleNamespace(model_id=model_id)
        return _wire(f"{model_id} completed")

    provider._invoke = MethodType(fake_invoke, provider)

    async def worker(event: ProjectEvent, expected_model: str) -> tuple[str, str | None]:
        await provider.generate(event=event, context=context, system_prompt="test")
        returned[expected_model].set()
        await read_gate.wait()
        usage_model = getattr(provider.last_usage, "model_id", None)
        return provider.last_model_id, usage_model

    async def scenario() -> tuple[tuple[str, str | None], tuple[str, str | None]]:
        architecture_task = asyncio.create_task(worker(architecture_event, "arch-model"))
        routine_task = asyncio.create_task(worker(routine_event, "routine-model"))
        await entered["arch-model"].wait()
        await entered["routine-model"].wait()

        release["arch-model"].set()
        await returned["arch-model"].wait()
        release["routine-model"].set()
        await returned["routine-model"].wait()
        read_gate.set()
        return await architecture_task, await routine_task

    architecture_metadata, routine_metadata = asyncio.run(scenario())
    assert architecture_metadata == ("arch-model", "arch-model")
    assert routine_metadata == ("routine-model", "routine-model")
