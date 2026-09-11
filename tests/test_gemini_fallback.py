import asyncio
import time
from types import MethodType, SimpleNamespace

import pytest

from archbro.backend.core.contracts import AgentAction, AgentActionType, Architecture, Project, ProjectContext, ProjectEvent, ProjectEventType
from archbro.backend.core.evaluation import DriftClassification, DriftEvaluation, DriftRecommendedAction
from archbro.backend.llm.gemini import (
    DEFAULT_GEMINI_CHAIN,
    DEFAULT_GEMINI_GOAL_CHAIN,
    DEFAULT_GEMINI_ROUTINE_CHAIN,
    GeminiArchitectureWire,
    GeminiBootstrapWire,
    GeminiComponentWire,
    GeminiDecisionWire,
    GeminiProvider,
    GeminiPlannerRootWire,
    GeminiScopeDeltaWire,
    GeminiSystemMapWire,
    InitialArchitecturePlannerSnapshot,
    _compact_context_facts,
    _compact_event_facts,
    _compact_json,
)


def _aligned_evaluation() -> DriftEvaluation:
    return DriftEvaluation(
        classification=DriftClassification.ALIGNED,
        summary="No architecture drift in fallback-routing fixture.",
        recommended_action=DriftRecommendedAction.NO_ACTION,
    )

def _provider_with_chain() -> GeminiProvider:
    provider = object.__new__(GeminiProvider)
    provider.model_id = "gemini-3.8-flash"
    provider.last_model_id = provider.model_id
    provider.last_usage = None
    provider._base_url = None
    provider.fallback_model_ids = (
        "gemini-3.6-flash",
        "gemini-3.5-flash",
        "gemini-3.5-flash-lite",
    )
    provider.routine_model_id = "gemini-3.5-flash-lite"
    provider.routine_fallback_model_ids = (
        "gemini-3.1-flash-lite",
        "gemini-3.5-flash",
        "gemini-3.6-flash",
        "gemini-3.8-flash",
    )
    provider.routine_model_timeout_seconds = 0.5
    provider.interaction_model_timeout_seconds = 0.5
    provider.interaction_total_timeout_seconds = 2.0
    provider.architecture_model_timeout_seconds = 0.5
    provider.architecture_phase_timeout_seconds = 1.0
    provider.architecture_total_timeout_seconds = 2.0
    provider.system_map_model_id = None
    provider.bootstrap_fallback_model_ids = (
        "gemini-3.5-flash-lite",
        "gemini-3.6-flash",
        "gemini-3.5-flash",
    )
    return provider


def test_default_architecture_chain_starts_with_gemini_38():
    assert DEFAULT_GEMINI_CHAIN[0] == "gemini-3.8-flash"


def test_temporary_unavailable_classifier_reads_provider_status_from_exception_chain():
    class ProviderStatusError(RuntimeError):
        def __init__(self, status: str, message: str) -> None:
            self.status = status
            super().__init__(message)

    unavailable = RuntimeError("provider request failed")
    unavailable.__cause__ = ProviderStatusError("UNAVAILABLE", "Service temporarily unavailable")
    assert GeminiProvider._is_temporary_unavailable(unavailable) is True

    quota = RuntimeError("provider request failed")
    quota.__cause__ = ProviderStatusError("RESOURCE_EXHAUSTED", "quota exhausted")
    assert GeminiProvider._is_temporary_unavailable(quota) is False


def test_temporary_unavailable_classifier_accepts_message_only_service_unavailable():
    assert GeminiProvider._is_temporary_unavailable(RuntimeError("upstream unavailable")) is True
    assert GeminiProvider._is_temporary_unavailable(RuntimeError("Service temporarily unavailable")) is True
    assert GeminiProvider._is_temporary_unavailable(RuntimeError("429 RESOURCE_EXHAUSTED: quota exceeded")) is False


def test_temporary_unavailable_classifier_protected_message_signals_win():
    assert GeminiProvider._is_temporary_unavailable(RuntimeError("429 RESOURCE_EXHAUSTED: service temporarily unavailable")) is False
    assert GeminiProvider._is_temporary_unavailable(RuntimeError("401 UNAUTHENTICATED: service temporarily unavailable")) is False
    assert GeminiProvider._is_temporary_unavailable(RuntimeError("403 PERMISSION_DENIED: upstream unavailable")) is False


def test_temporary_unavailable_classifier_nested_protected_status_wins():
    class ProviderStatusError(RuntimeError):
        def __init__(self, status: str, message: str) -> None:
            self.status = status
            super().__init__(message)

    outer = ProviderStatusError("UNAVAILABLE", "Service temporarily unavailable")
    outer.__cause__ = ProviderStatusError("RESOURCE_EXHAUSTED", "quota exhausted")
    assert GeminiProvider._is_temporary_unavailable(outer) is False

    branched = RuntimeError("provider request failed")
    branched.__cause__ = ProviderStatusError("UNAVAILABLE", "Service temporarily unavailable")
    branched.__context__ = ProviderStatusError("PERMISSION_DENIED", "forbidden")
    assert GeminiProvider._is_temporary_unavailable(branched) is False


def test_temporary_unavailable_classifier_conservative_message_only_policy():
    assert GeminiProvider._is_temporary_unavailable(RuntimeError("Request failed with status 503")) is True
    assert GeminiProvider._is_temporary_unavailable(RuntimeError("Request failed with status 500")) is False
    assert GeminiProvider._is_temporary_unavailable(RuntimeError("provider request failed")) is False


def test_temporary_unavailable_classifier_exception_graph_is_cycle_safe():
    first = RuntimeError("provider request failed")
    second = RuntimeError("another wrapper")
    first.__cause__ = second
    second.__context__ = first
    assert GeminiProvider._is_temporary_unavailable(first) is False


def test_temporary_unavailable_classifier_accepts_installed_google_genai_server_error_503():
    from google.genai import errors as genai_errors

    error = genai_errors.ServerError(503, {"error": {"code": 503, "message": "temporary outage"}}, None)
    assert type(error).__module__.startswith("google.genai")
    assert error.code == 503
    assert GeminiProvider._is_temporary_unavailable(error) is True


def test_provider_uses_custom_gateway_transport_when_configured(monkeypatch):
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "false")
    monkeypatch.setenv("GEMINI_BASE_URL", "http://127.0.0.1:8080/gemini/")
    monkeypatch.setenv("GEMINI_API_KEY", "gateway-test-key")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    provider = GeminiProvider()

    assert provider.model_id == "gemini-3.8-flash"
    assert provider._base_url == "http://127.0.0.1:8080/gemini"
    assert provider._api_key == "gateway-test-key"


def test_provider_accepts_existing_google_gemini_base_url_for_gateway(monkeypatch):
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "false")
    monkeypatch.delenv("GEMINI_BASE_URL", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_GEMINI_BASE_URL", "http://host.docker.internal:8080/gemini/")
    monkeypatch.setenv("GOOGLE_API_KEY", "existing-gateway-key")

    provider = GeminiProvider()

    assert provider._base_url == "http://host.docker.internal:8080/gemini"
    assert provider._api_key == "existing-gateway-key"


def test_provider_accepts_vertex_adc_without_an_api_key(monkeypatch):
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "magic-dala")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "global")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_BASE_URL", raising=False)
    monkeypatch.delenv("GOOGLE_GEMINI_BASE_URL", raising=False)

    provider = GeminiProvider()

    assert provider._api_key is None
    assert provider._base_url is None
    assert provider._google_client_factory.use_vertex_ai is True
    assert provider._transport_name() == "vertex"


def test_strands_agent_uses_prebuilt_client_and_closes_it(monkeypatch):
    import strands
    import strands.models.gemini as strands_gemini

    captured: dict[str, object] = {}

    class FakeAio:
        async def aclose(self):
            captured["closed"] = int(captured.get("closed", 0)) + 1

    class FakeClient:
        def __init__(self):
            self.aio = FakeAio()

        def close(self):
            captured["sync_closed"] = True

    client = FakeClient()

    class FakeFactory:
        transport = "vertex"

        def create_client(self, *, http_timeout_ms):
            captured["http_timeout_ms"] = http_timeout_ms
            return client

    class FakeGeminiModel:
        def __init__(self, *, client, model_id, params):
            captured["client"] = client
            captured["model_id"] = model_id
            captured["params"] = params

    class FakeAgent:
        def __init__(self, *, model, callback_handler):
            captured["model"] = model
            captured["callback_handler"] = callback_handler

        async def invoke_async(self, prompt, **kwargs):
            captured["prompt"] = prompt
            return "completed"

    monkeypatch.setattr(strands, "Agent", FakeAgent)
    monkeypatch.setattr(strands_gemini, "GeminiModel", FakeGeminiModel)
    monkeypatch.setenv("GEMINI_HTTP_TIMEOUT_MS", "4321")

    provider = object.__new__(GeminiProvider)
    provider._google_client_factory = FakeFactory()
    managed_agent = provider._build_agent("gemini-test")

    result = asyncio.run(managed_agent.invoke_async("hello"))

    assert result == "completed"
    assert captured["client"] is client
    assert captured["model_id"] == "gemini-test"
    assert captured["http_timeout_ms"] == 4321
    assert captured["closed"] == 1
    assert "sync_closed" not in captured


def test_client_cleanup_failure_does_not_mask_the_provider_error(monkeypatch):
    import strands
    import strands.models.gemini as strands_gemini

    class FakeAio:
        async def aclose(self):
            raise RuntimeError("cleanup transport detail")

    class FakeClient:
        aio = FakeAio()

        def close(self):
            pass

    class FakeFactory:
        transport = "vertex"

        def create_client(self, *, http_timeout_ms):
            return FakeClient()

    class FakeGeminiModel:
        def __init__(self, *, client, model_id, params):
            pass

    class FakeAgent:
        def __init__(self, *, model, callback_handler):
            pass

        async def invoke_async(self, prompt, **kwargs):
            raise RuntimeError("503 UNAVAILABLE: original provider failure")

    monkeypatch.setattr(strands, "Agent", FakeAgent)
    monkeypatch.setattr(strands_gemini, "GeminiModel", FakeGeminiModel)

    provider = object.__new__(GeminiProvider)
    provider._google_client_factory = FakeFactory()
    managed_agent = provider._build_agent("gemini-test")

    with pytest.raises(RuntimeError, match="original provider failure"):
        asyncio.run(managed_agent.invoke_async("hello"))


def test_system_map_model_override_only_affects_system_map_phase():
    provider = _provider_with_chain()
    provider.system_map_model_id = "gemini-3.8-flash-medium"

    assert provider._planner_model_chain("_invoke_system_map") == (
        "gemini-3.8-flash-medium",
        "gemini-3.8-flash",
        "gemini-3.5-flash-lite",
        "gemini-3.6-flash",
        "gemini-3.5-flash",
    )
    assert provider._planner_model_chain("_invoke_scope_delta") == provider.bootstrap_model_chain
    assert provider._planner_model_chain("_invoke_reconcile") == provider.bootstrap_model_chain


def test_provider_records_normalized_strands_usage():
    provider = object.__new__(GeminiProvider)
    provider._base_url = "http://127.0.0.1:8080/gemini"
    provider.last_usage = None
    result = SimpleNamespace(
        metrics=SimpleNamespace(
            accumulated_usage={
                "inputTokens": 286,
                "outputTokens": 14,
                "totalTokens": 407,
                "cacheReadInputTokens": 23,
                "cacheWriteInputTokens": 5,
            }
        )
    )

    provider._record_usage("gemini-3.8-flash", result, time.perf_counter() - 0.01)

    assert provider.last_usage is not None
    assert provider.last_usage.model_id == "gemini-3.8-flash"
    assert provider.last_usage.transport == "gateway"
    assert provider.last_usage.input_tokens == 286
    assert provider.last_usage.output_tokens == 14
    assert provider.last_usage.total_tokens == 407
    assert provider.last_usage.cache_read_input_tokens == 23
    assert provider.last_usage.cache_write_input_tokens == 5
    assert provider.last_usage.latency_ms >= 0


def _context_and_event(event_type: ProjectEventType = ProjectEventType.MANUAL_NOTE):
    project = Project(name="fallback test", goal="verify provider fallback")
    context = ProjectContext(project=project, architecture=Architecture(version=1), tasks=[], pending_proposals=[])
    if event_type == ProjectEventType.TASK_UPDATED:
        payload = {"task_id": "task_test", "status": "DONE", "message": "Task completed."}
    elif event_type == ProjectEventType.USER_MESSAGE:
        payload = {"message": "We may need to change the architecture."}
    else:
        payload = {"note": "No state change."}
    event = ProjectEvent(project_id=project.id, type=event_type, payload=payload)
    return context, event


def test_503_falls_through_full_real_gemini_chain():
    provider = _provider_with_chain()
    attempts: list[str] = []

    async def fake_invoke(self, model_id: str, prompt: str):
        attempts.append(model_id)
        if model_id != "gemini-3.5-flash-lite":
            raise RuntimeError("503 UNAVAILABLE: model currently experiencing high demand")
        return GeminiDecisionWire(
            summary="No change.",
            evaluation=_aligned_evaluation(),
            actions=[AgentAction(type=AgentActionType.NO_ACTION)],
        )

    provider._invoke = MethodType(fake_invoke, provider)
    context, event = _context_and_event()
    decision = asyncio.run(provider.generate(event=event, context=context, system_prompt="test"))

    assert attempts == [
        "gemini-3.8-flash",
        "gemini-3.6-flash",
        "gemini-3.5-flash",
        "gemini-3.5-flash-lite",
    ]
    assert provider.last_model_id == "gemini-3.5-flash-lite"
    assert decision.actions[0].type == AgentActionType.NO_ACTION


def test_wrapped_unavailable_status_falls_through_to_next_model():
    provider = _provider_with_chain()
    attempts: list[str] = []

    class ProviderStatusError(RuntimeError):
        def __init__(self, status: str, message: str) -> None:
            self.status = status
            super().__init__(message)

    async def fake_invoke(self, model_id: str, prompt: str):
        attempts.append(model_id)
        if len(attempts) == 1:
            wrapped = RuntimeError("provider request failed")
            wrapped.__cause__ = ProviderStatusError("UNAVAILABLE", "Service temporarily unavailable")
            raise wrapped
        return GeminiDecisionWire(
            summary="Recovered on fallback.",
            evaluation=_aligned_evaluation(),
            actions=[AgentAction(type=AgentActionType.NO_ACTION)],
        )

    provider._invoke = MethodType(fake_invoke, provider)
    context, event = _context_and_event()
    decision = asyncio.run(provider.generate(event=event, context=context, system_prompt="test"))

    assert attempts == ["gemini-3.8-flash", "gemini-3.6-flash"]
    assert provider.last_model_id == "gemini-3.6-flash"
    assert decision.actions[0].type == AgentActionType.NO_ACTION


def test_429_does_not_fallback():
    provider = _provider_with_chain()
    attempts: list[str] = []

    async def fake_invoke(self, model_id: str, prompt: str):
        attempts.append(model_id)
        raise RuntimeError("429 RESOURCE_EXHAUSTED: quota exceeded")

    provider._invoke = MethodType(fake_invoke, provider)
    context, event = _context_and_event()

    with pytest.raises(RuntimeError, match="429"):
        asyncio.run(provider.generate(event=event, context=context, system_prompt="test"))

    assert attempts == ["gemini-3.8-flash"]
    assert provider.last_model_id == "gemini-3.8-flash"


def test_goal_and_routine_chains_use_high_quota_flash_lite_models_first():
    assert DEFAULT_GEMINI_GOAL_CHAIN[:2] == (
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
    )
    assert DEFAULT_GEMINI_ROUTINE_CHAIN[:2] == (
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
    )


def test_strands_agent_instances_are_not_shared_between_invocations():
    provider = object.__new__(GeminiProvider)

    def fake_build(self, model_id: str):
        return object()

    provider._build_agent = MethodType(fake_build, provider)
    first = provider._agent_for("gemini-3.5-flash-lite")
    second = provider._agent_for("gemini-3.5-flash-lite")

    assert first is not second


def test_task_updated_uses_routine_chain_before_architecture_models():
    provider = _provider_with_chain()
    attempts: list[str] = []

    async def fake_invoke(self, model_id: str, prompt: str):
        attempts.append(model_id)
        if model_id == "gemini-3.5-flash-lite":
            raise RuntimeError("503 UNAVAILABLE: model currently experiencing high demand")
        if model_id == "gemini-3.1-flash-lite":
            return GeminiDecisionWire(
                summary="Routine task update handled.",
                evaluation=_aligned_evaluation(),
                actions=[AgentAction(type=AgentActionType.NO_ACTION)],
            )
        raise AssertionError(f"unexpected model reached: {model_id}")

    provider._invoke = MethodType(fake_invoke, provider)
    context, event = _context_and_event(ProjectEventType.TASK_UPDATED)
    decision = asyncio.run(provider.generate(event=event, context=context, system_prompt="test"))

    assert attempts == ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]
    assert provider.last_model_id == "gemini-3.1-flash-lite"
    assert decision.actions[0].type == AgentActionType.NO_ACTION


def test_user_message_stays_on_architecture_chain():
    provider = _provider_with_chain()
    attempts: list[str] = []

    async def fake_invoke(self, model_id: str, prompt: str):
        attempts.append(model_id)
        return GeminiDecisionWire(
            summary="Architecture-sensitive message handled.",
            evaluation=_aligned_evaluation(),
            actions=[AgentAction(type=AgentActionType.NO_ACTION)],
        )

    provider._invoke = MethodType(fake_invoke, provider)
    context, event = _context_and_event(ProjectEventType.USER_MESSAGE)
    asyncio.run(provider.generate(event=event, context=context, system_prompt="test"))

    assert attempts == ["gemini-3.8-flash"]
    assert provider.last_model_id == "gemini-3.8-flash"


def test_bootstrap_phase_uses_fast_rescue_chain():
    provider = _provider_with_chain()
    attempts: list[str] = []

    async def fake_system_map(self, model_id: str, prompt: str):
        attempts.append(model_id)
        if model_id == "gemini-3.8-flash":
            raise RuntimeError("503 UNAVAILABLE: model currently experiencing high demand")
        return GeminiSystemMapWire(
            summary="Initial system map.",
            roots=[GeminiPlannerRootWire(id="product", name="Product", type="system", responsibility="Own the product boundary")],
        )

    provider._invoke_system_map = MethodType(fake_system_map, provider)
    wire = asyncio.run(
        provider._run_planner_phase(
            "_invoke_system_map",
            "system map prompt",
            global_deadline=__import__("time").perf_counter() + 2,
        )
    )

    assert attempts == ["gemini-3.8-flash", "gemini-3.5-flash-lite"]
    assert provider.last_model_id == "gemini-3.5-flash-lite"
    assert [root.id for root in wire.roots] == ["product"]


def test_bootstrap_phase_never_expands_remaining_global_deadline(monkeypatch):
    provider = _provider_with_chain()
    provider.architecture_model_timeout_seconds = 5.0
    attempts: list[str] = []
    observed_timeouts: list[float] = []
    original_wait_for = asyncio.wait_for

    async def recording_wait_for(awaitable, *, timeout):
        observed_timeouts.append(timeout)
        return await original_wait_for(awaitable, timeout=timeout)

    async def fake_system_map(self, model_id: str, prompt: str):
        attempts.append(model_id)
        if len(attempts) == 1:
            raise RuntimeError("503 UNAVAILABLE: temporary outage")
        return GeminiSystemMapWire(
            summary="Initial system map.",
            roots=[GeminiPlannerRootWire(id="product", name="Product", type="system", responsibility="Own the product boundary")],
        )

    provider._invoke_system_map = MethodType(fake_system_map, provider)
    monkeypatch.setattr(asyncio, "wait_for", recording_wait_for)
    budget_seconds = 0.25
    global_deadline = __import__("time").perf_counter() + budget_seconds

    asyncio.run(
        provider._run_planner_phase(
            "_invoke_system_map",
            "system map prompt",
            global_deadline=global_deadline,
        )
    )

    assert attempts == ["gemini-3.8-flash", "gemini-3.5-flash-lite"]
    assert len(observed_timeouts) == 2
    assert all(0 < timeout <= budget_seconds for timeout in observed_timeouts)
    assert observed_timeouts[1] <= observed_timeouts[0]


def test_bootstrap_prompt_requests_outside_in_decomposition_without_hardcoded_taxonomy():
    provider = _provider_with_chain()
    project = Project(
        name="rental",
        goal="Build an agentic rental site with user UI, recommendations, search, and managed data on Google Cloud.",
    )
    context = ProjectContext(project=project, architecture=Architecture(), tasks=[], pending_proposals=[])
    event = ProjectEvent(
        project_id=project.id,
        type=ProjectEventType.USER_MESSAGE,
        payload={"intent": "INITIAL_ARCHITECTURE", "message": project.goal},
    )
    system_prompt = provider._system_map_prompt(event=event, context=context)
    snapshot = InitialArchitecturePlannerSnapshot(
        roots=("experience", "domain"),
        components=(
            GeminiComponentWire(id="experience", name="Experience", type="system", responsibility="Own user interaction"),
            GeminiComponentWire(id="domain", name="Domain", type="system", responsibility="Unrelated domain detail must not be repeated into another scope prompt"),
        ),
    )
    scope_prompt = provider._scope_prompt(
        event=event,
        context=context,
        snapshot=snapshot,
        scope_id="experience",
    )

    assert "SYSTEM_MAP phase" in system_prompt
    assert "ONLY root system boundaries" in system_prompt
    assert "Normally use 3-6 truthful major boundaries" in system_prompt
    assert "allow 1-2 for a genuinely simple system" in system_prompt
    assert "do not hardcode a category taxonomy" in system_prompt.lower()
    assert "EXPAND_SCOPE phase" in scope_prompt
    assert "only NEW descendants" in scope_prompt
    assert "never regenerate or edit accepted nodes" in scope_prompt
    assert "files, classes, functions, methods" in scope_prompt
    assert project.id not in system_prompt
    assert event.id not in system_prompt
    assert "created_at" not in system_prompt
    assert "Unrelated domain detail must not be repeated" not in scope_prompt
    assert '"domain"' in scope_prompt


def test_compact_runtime_facts_remove_transport_metadata_and_bound_notes():
    project = Project(name="compact", goal="Keep model context bounded.")
    context = ProjectContext(
        project=project,
        architecture=Architecture(),
        tasks=[],
        pending_proposals=[],
        recent_notes=[f"note-{index}-" + ("x" * 1500) for index in range(10)],
    )
    event = ProjectEvent(
        project_id=project.id,
        type=ProjectEventType.USER_MESSAGE,
        payload={"message": "Inspect only the current semantic facts."},
    )

    full = context.model_dump_json() + event.model_dump_json()
    compact_context = _compact_context_facts(context)
    compact_event = _compact_event_facts(event)
    compact = _compact_json(compact_context) + _compact_json(compact_event)

    assert len(compact) < len(full)
    assert project.id not in compact
    assert event.id not in compact
    assert "created_at" not in compact
    assert "updated_at" not in compact
    assert "received_at" not in compact
    assert len(compact_context["recent_notes"]) == 8
    assert all(len(note) <= 1000 for note in compact_context["recent_notes"])
