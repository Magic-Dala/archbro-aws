import asyncio
import json
import threading
import time
from types import MethodType, SimpleNamespace

import pytest
from pydantic import ValidationError

from archbro.backend.agent.orchestration import AgentOrchestrator
from archbro.backend.core.contracts import (
    Architecture,
    Component,
    Project,
    ProjectContext,
    ProjectEvent,
    ProjectEventType,
    Relationship,
    TaskProposal,
)
from archbro.backend.llm.gemini import (
    ArchitectureNeedsFactError,
    GeminiArchitectureWire,
    GeminiPlannerRootWire,
    GeminiProvider,
    GeminiReconcileWire,
    GeminiScopeDeltaWire,
    GeminiSystemMapWire,
    GeminiComponentWire,
    InitialArchitecturePlannerSnapshot,
)
from archbro.platform.persistence.postgres import PostgresProjectRepository
from conftest import requires_database


def _provider() -> GeminiProvider:
    provider = object.__new__(GeminiProvider)
    provider.model_id = "gemini-primary"
    provider.last_model_id = provider.model_id
    provider.last_usage = None
    provider._base_url = None
    provider._checkpoint_repository = None
    provider.system_map_model_id = None
    provider.bootstrap_fallback_model_ids = ("gemini-rescue",)
    provider.fallback_model_ids = ()
    provider.routine_model_id = "gemini-routine"
    provider.routine_fallback_model_ids = ()
    provider.routine_model_timeout_seconds = 0.5
    provider.architecture_model_timeout_seconds = 0.5
    provider.architecture_phase_timeout_seconds = 1.5
    provider.architecture_total_timeout_seconds = 8.0
    provider.architecture_max_output_tokens = 65536
    provider.system_map_max_output_tokens = 4096
    provider.scope_max_output_tokens = 8192
    provider.reconcile_max_output_tokens = 16384
    provider.architecture_thinking_level = "medium"
    provider.system_map_thinking_level = "low"
    provider.scope_thinking_level = "low"
    provider.reconcile_thinking_level = "medium"
    provider.architecture_retry_attempts = 5
    provider.retry_initial_delay_seconds = 1.0
    provider.retry_max_delay_seconds = 8.0
    provider.retry_exp_base = 2.0
    provider.retry_jitter = 1.0
    provider.architecture_max_concurrency = 1
    provider.architecture_queue_timeout_seconds = 1.0
    return provider


def _bootstrap_context(root_count: int = 4) -> tuple[ProjectContext, ProjectEvent]:
    project = Project(
        name="outside-in-fixture",
        goal=(
            "Build a collaborative product with a user workspace, domain APIs, "
            "agent coordination, durable project state, and external integrations."
        ),
    )
    context = ProjectContext(
        project=project,
        architecture=Architecture(),
        tasks=[],
        pending_proposals=[],
    )
    event = ProjectEvent(
        project_id=project.id,
        type=ProjectEventType.USER_MESSAGE,
        payload={"intent": "INITIAL_ARCHITECTURE", "message": project.goal},
    )
    return context, event


def _roots() -> list[GeminiPlannerRootWire]:
    return [
        GeminiPlannerRootWire(id="experience", name="Experience", type="system", responsibility="Own user interaction"),
        GeminiPlannerRootWire(id="domain", name="Domain", type="system", responsibility="Own product behavior"),
        GeminiPlannerRootWire(id="coordination", name="Coordination", type="system", responsibility="Own agent coordination"),
        GeminiPlannerRootWire(id="state", name="State", type="system", responsibility="Own durable state and external data"),
    ]


def _wire_provider(provider: GeminiProvider, calls: list[str], *, fail_scope: str | None = None, fail_reconcile: bool = False) -> None:
    async def system_map(self, model_id: str, prompt: str):
        calls.append("SYSTEM_MAP")
        return GeminiSystemMapWire(summary="Four truthful boundaries", roots=_roots())

    async def scope_delta(self, model_id: str, prompt: str):
        scope_id = prompt.split("scope_id=", 1)[1].split(".", 1)[0]
        calls.append(f"EXPAND_SCOPE:{scope_id}")
        if scope_id == fail_scope:
            raise RuntimeError("scope failed permanently")
        child_id = f"{scope_id}_capability"
        return GeminiScopeDeltaWire(
            scope_id=scope_id,
            components=[
                GeminiComponentWire(
                    id=child_id,
                    name=f"{scope_id.title()} Capability",
                    type="service",
                    responsibility=f"Implement the {scope_id} capability",
                    parent_id=scope_id,
                )
            ],
        )

    async def reconcile(self, model_id: str, prompt: str):
        calls.append("RECONCILE")
        if fail_reconcile:
            raise RuntimeError("reconcile failed permanently")
        return GeminiReconcileWire(
            summary="Outside-in architecture ready",
            relationships=[
                Relationship(source="experience_capability", target="domain_capability", relationship_type="HTTPS"),
                Relationship(source="domain_capability", target="coordination_capability", relationship_type="CALLS"),
                Relationship(source="coordination_capability", target="state_capability", relationship_type="WRITES"),
                Relationship(source="domain_capability", target="state_capability", relationship_type="STATE"),
            ],
            tasks=[TaskProposal(title="Build domain capability", related_component="domain_capability")],
        )

    provider._invoke_system_map = MethodType(system_map, provider)
    provider._invoke_scope_delta = MethodType(scope_delta, provider)
    provider._invoke_reconcile = MethodType(reconcile, provider)


def _architecture_from_decision(decision) -> Architecture:
    note = next(action.payload["note"] for action in decision.actions if action.payload.get("note", "").startswith("INITIAL_ARCHITECTURE:"))
    return Architecture.model_validate_json(note.removeprefix("INITIAL_ARCHITECTURE:"))


class _CheckpointStore:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict] = {}
        self.lock = threading.Lock()

    def get_planner_checkpoint(self, plan_id: str, phase_key: str):
        value = self.rows.get((plan_id, phase_key))
        return json.loads(json.dumps(value)) if value is not None else None

    def list_planner_checkpoints(self, project_id: str, limit: int = 100):
        values = [
            value
            for value in reversed(list(self.rows.values()))
            if value.get("project_id") == project_id
        ][:limit]
        return json.loads(json.dumps(values))

    def put_planner_checkpoint(
        self,
        *,
        project_id: str,
        plan_id: str,
        phase_key: str,
        data: dict,
        expected_revision: int | None = None,
        expected_owner_generation: int | None = None,
    ) -> dict:
        with self.lock:
            key = (plan_id, phase_key)
            existing = self.rows.get(key)
            if existing is None:
                if expected_revision is not None or expected_owner_generation is not None:
                    raise RuntimeError("checkpoint disappeared")
                persisted = json.loads(json.dumps(data))
                persisted.setdefault("revision", 1)
                persisted.setdefault("owner_generation", 1)
                self.rows[key] = persisted
                return json.loads(json.dumps(persisted))
            if existing.get("project_id") != project_id:
                raise RuntimeError("planner checkpoint project identity mismatch")
            if expected_revision is not None and existing.get("revision") != expected_revision:
                raise RuntimeError("planner checkpoint revision changed before fenced update")
            if (
                expected_owner_generation is not None
                and existing.get("owner_generation") != expected_owner_generation
            ):
                raise RuntimeError("planner checkpoint owner generation changed before fenced update")
            persisted = json.loads(json.dumps(data))
            persisted["revision"] = int(existing.get("revision", 0)) + 1
            persisted["owner_generation"] = int(existing.get("owner_generation", 0))
            self.rows[key] = persisted
            return json.loads(json.dumps(persisted))

    def claim_planner_checkpoint(
        self,
        *,
        project_id: str,
        plan_id: str,
        phase_key: str,
        data: dict,
        retry_statuses: tuple[str, ...] = (),
    ):
        with self.lock:
            key = (plan_id, phase_key)
            existing = self.rows.get(key)
            if existing is not None:
                if existing.get("status") not in retry_statuses:
                    return False, json.loads(json.dumps(existing))
                if existing.get("input_sha256") != data.get("input_sha256"):
                    return False, json.loads(json.dumps(existing))
            claimed = json.loads(json.dumps(data))
            if existing is None:
                claimed["revision"] = 1
                claimed["owner_generation"] = 1
            else:
                claimed["revision"] = int(existing.get("revision", 0)) + 1
                claimed["owner_generation"] = int(existing.get("owner_generation", 0)) + 1
                claimed["previous_attempt_id"] = existing.get("attempt_id")
            claimed.setdefault("delivery_stage", "PREPARED")
            self.rows[key] = claimed
            return True, json.loads(json.dumps(claimed))

    def recover_planner_checkpoint(
        self,
        *,
        project_id: str,
        plan_id: str,
        phase_key: str,
        expected_attempt_id: str,
        expected_revision: int,
        action: str,
        request_id: str,
    ) -> dict:
        with self.lock:
            key = (plan_id, phase_key)
            existing = self.rows[key]
            last = existing.get("last_recovery")
            if isinstance(last, dict) and last.get("request_id") == request_id:
                if last.get("action") != action:
                    raise RuntimeError("recovery request reused")
                return json.loads(json.dumps(existing))
            if existing.get("project_id") != project_id:
                raise RuntimeError("project mismatch")
            if existing.get("attempt_id") != expected_attempt_id:
                raise RuntimeError("attempt changed")
            if existing.get("revision") != expected_revision:
                raise RuntimeError("revision changed")
            stage = existing.get("delivery_stage", "PREPARED")
            if action == "RECLAIM_PREPARED" and stage != "PREPARED":
                raise ValueError("PREPARED reclaim is forbidden after provider dispatch")
            if action == "REPROCESS_RESPONSE" and stage != "RESPONSE_RECORDED":
                raise ValueError("response reprocessing requires a recorded provider response")
            if action == "REPROCESS_RESPONSE":
                provider = existing.get("provider")
                if not isinstance(provider, dict) or provider.get("response_reprocessable") is not True:
                    raise ValueError("recorded provider response is not safe for local reprocessing")
                if not isinstance(provider.get("raw_model_output"), str) or not provider.get("raw_model_output"):
                    raise ValueError("recorded provider response is missing raw model output")
            if action == "AUTHORIZE_NEW_ATTEMPT" and stage == "PREPARED":
                raise ValueError("PREPARED work must be reclaimed")
            recovered = json.loads(json.dumps(existing))
            recovered["status"] = "REPROCESSABLE" if action == "REPROCESS_RESPONSE" else "RETRYABLE"
            recovered["revision"] = expected_revision + 1
            recovered["owner_generation"] = int(existing.get("owner_generation", 0)) + 1
            recovered["last_recovery"] = {"request_id": request_id, "action": action}
            self.rows[key] = recovered
            return json.loads(json.dumps(recovered))


def test_rich_bootstrap_runs_ordered_outside_in_passes_and_emits_once():
    provider = _provider()
    calls: list[str] = []
    _wire_provider(provider, calls)
    context, event = _bootstrap_context()

    decision = asyncio.run(provider.generate(event=event, context=context, system_prompt="unused"))
    architecture = _architecture_from_decision(decision)

    assert calls == [
        "SYSTEM_MAP",
        "EXPAND_SCOPE:experience",
        "EXPAND_SCOPE:domain",
        "EXPAND_SCOPE:coordination",
        "EXPAND_SCOPE:state",
        "RECONCILE",
    ]
    assert len(calls) == 6
    assert [component.id for component in architecture.components] == ["experience", "domain", "coordination", "state"]
    assert architecture.find_component("domain_capability") is not None
    assert architecture.parent_component_id_for("domain_capability") == "domain"
    assert architecture.root_component_id_for("domain_capability") == "domain"


def test_completed_planner_checkpoints_resume_without_replaying_model_calls():
    store = _CheckpointStore()
    context, event = _bootstrap_context()

    first = _provider()
    first._checkpoint_repository = store
    first_calls: list[str] = []
    _wire_provider(first, first_calls)
    first_decision = asyncio.run(first.generate(event=event, context=context, system_prompt="unused"))
    assert _architecture_from_decision(first_decision).version == 1
    assert len(store.rows) == 6
    assert all(row["status"] == "COMPLETED" for row in store.rows.values())

    resumed = _provider()
    resumed._checkpoint_repository = store
    resumed_calls: list[str] = []
    _wire_provider(resumed, resumed_calls)
    resumed_decision = asyncio.run(resumed.generate(event=event, context=context, system_prompt="unused"))

    assert resumed_calls == []
    assert _architecture_from_decision(resumed_decision) == _architecture_from_decision(first_decision)
    assert resumed.last_usage["completed"] is True
    assert len(resumed.last_usage["phases"]) == 6
    assert all(phase["replayed_from_checkpoint"] is True for phase in resumed.last_usage["phases"])


@pytest.mark.parametrize(
    "failed_phase",
    [
        "SYSTEM_MAP",
        "EXPAND_SCOPE:experience",
        "EXPAND_SCOPE:domain",
        "EXPAND_SCOPE:coordination",
        "EXPAND_SCOPE:state",
        "RECONCILE",
    ],
)
def test_explicit_429_is_retryable_in_every_phase_and_resume_starts_at_failure(
    failed_phase: str,
):
    class StructuredProviderError(RuntimeError):
        def __init__(self, code: int, status: str, message: str) -> None:
            self.code = code
            self.status = status
            super().__init__(message)

    phase_order = [
        "SYSTEM_MAP",
        "EXPAND_SCOPE:experience",
        "EXPAND_SCOPE:domain",
        "EXPAND_SCOPE:coordination",
        "EXPAND_SCOPE:state",
        "RECONCILE",
    ]
    failed_index = phase_order.index(failed_phase)
    store = _CheckpointStore()
    context, event = _bootstrap_context()
    first = _provider()
    first._checkpoint_repository = store
    first_calls: list[str] = []
    _wire_provider(first, first_calls)
    successful_system_map = first._invoke_system_map
    successful_scope = first._invoke_scope_delta
    successful_reconcile = first._invoke_reconcile

    def reject(self: GeminiProvider, model_id: str) -> None:
        metadata = self._current_invocation_metadata()
        assert metadata is not None
        provider_metadata = {
            "requested_model": model_id,
            "transport": "vertex",
            "thinking_level": (
                "medium" if failed_phase == "RECONCILE" else "low"
            ),
            "max_output_tokens": (
                4096
                if failed_phase == "SYSTEM_MAP"
                else (16384 if failed_phase == "RECONCILE" else 8192)
            ),
            "retry_attempt_limit": 5,
            "provider_attempt_count": 5,
            "provider_rejections": [
                {
                    "attempt": attempt,
                    "error_type": "ClientError",
                    "http_status_code": 429,
                    "provider_status": "RESOURCE_EXHAUSTED",
                    "explicit_response": True,
                    "retryable": True,
                }
                for attempt in range(1, 6)
            ],
            "retry_delays_seconds": [1.0, 2.0, 4.0, 8.0],
            "error_type": "ClientError",
            "provider_response_received": True,
            "retryable_provider_error": True,
            "http_status_code": 429,
            "provider_status": "RESOURCE_EXHAUSTED",
            "retry_after_seconds": 2.0,
        }
        self._transition_active_planner_checkpoint(
            "IN_FLIGHT",
            provider=provider_metadata,
        )
        metadata["planner_request_started"] = True
        metadata["planner_request_in_flight"] = False
        metadata["planner_dispatch"] = provider_metadata
        raise StructuredProviderError(
            429,
            "RESOURCE_EXHAUSTED",
            "429 RESOURCE_EXHAUSTED: shared capacity temporarily exhausted",
        )

    async def maybe_reject_system_map(
        self: GeminiProvider,
        model_id: str,
        prompt: str,
    ):
        if failed_phase == "SYSTEM_MAP":
            first_calls.append("SYSTEM_MAP")
            reject(self, model_id)
        return await successful_system_map(model_id, prompt)

    async def maybe_reject_scope(
        self: GeminiProvider,
        model_id: str,
        prompt: str,
    ):
        scope_id = self._scope_id_from_prompt(prompt)
        phase = f"EXPAND_SCOPE:{scope_id}"
        if failed_phase == phase:
            first_calls.append(phase)
            reject(self, model_id)
        return await successful_scope(model_id, prompt)

    async def maybe_reject_reconcile(
        self: GeminiProvider,
        model_id: str,
        prompt: str,
    ):
        if failed_phase == "RECONCILE":
            first_calls.append("RECONCILE")
            reject(self, model_id)
        return await successful_reconcile(model_id, prompt)

    first._invoke_system_map = MethodType(maybe_reject_system_map, first)
    first._invoke_scope_delta = MethodType(maybe_reject_scope, first)
    first._invoke_reconcile = MethodType(maybe_reject_reconcile, first)

    with pytest.raises(RuntimeError, match="retryable provider error"):
        asyncio.run(
            first.generate(event=event, context=context, system_prompt="unused")
        )

    assert first_calls == phase_order[: failed_index + 1]
    plan_id = first._planner_plan_id(context)
    failed = store.get_planner_checkpoint(plan_id, failed_phase)
    assert failed is not None
    assert failed["status"] == "RETRYABLE"
    assert failed["delivery_stage"] == "PROVIDER_REJECTED"
    assert failed["validation"]["status"] == "RETRYABLE"
    assert failed["provider"]["http_status_code"] == 429
    assert failed["provider"]["provider_status"] == "RESOURCE_EXHAUSTED"
    assert failed["provider"]["retryable_provider_error"] is True
    assert failed["provider"]["provider_attempt_count"] == 5
    assert first.last_usage["phases"][-1]["phase"] == failed_phase

    resumed = _provider()
    resumed._checkpoint_repository = store
    resumed_calls: list[str] = []
    _wire_provider(resumed, resumed_calls)
    decision = asyncio.run(
        resumed.generate(event=event, context=context, system_prompt="unused")
    )

    assert _architecture_from_decision(decision).version == 1
    assert resumed_calls == phase_order[failed_index:]
    phases = resumed.last_usage["phases"]
    assert [phase["phase"] for phase in phases] == phase_order
    assert all(
        phase["replayed_from_checkpoint"] is True
        for phase in phases[:failed_index]
    )
    assert all(
        phase["replayed_from_checkpoint"] is False
        for phase in phases[failed_index:]
    )
    assert phases[failed_index]["status"] == "COMPLETED"


def test_new_planner_contract_fences_legacy_unknown_until_explicit_authorization():
    store = _CheckpointStore()
    context, event = _bootstrap_context()
    legacy = _provider()
    legacy._checkpoint_repository = store
    legacy._planner_plan_id = MethodType(
        lambda self, _context: "plan_legacy_v4_fixture",
        legacy,
    )
    legacy_calls: list[str] = []
    _wire_provider(legacy, legacy_calls)

    async def ambiguous_legacy_reconcile(self, model_id: str, prompt: str):
        legacy_calls.append("RECONCILE")
        metadata = self._current_invocation_metadata()
        assert metadata is not None
        self._transition_active_planner_checkpoint(
            "IN_FLIGHT",
            provider={"requested_model": model_id},
        )
        metadata["planner_request_started"] = True
        metadata["planner_dispatch"] = {"requested_model": model_id}
        raise TimeoutError("legacy reconcile outcome unknown")

    legacy._invoke_reconcile = MethodType(ambiguous_legacy_reconcile, legacy)
    with pytest.raises(RuntimeError, match="timed out"):
        asyncio.run(legacy.generate(event=event, context=context, system_prompt="unused"))

    assert legacy_calls == [
        "SYSTEM_MAP",
        "EXPAND_SCOPE:experience",
        "EXPAND_SCOPE:domain",
        "EXPAND_SCOPE:coordination",
        "EXPAND_SCOPE:state",
        "RECONCILE",
    ]
    legacy_reconcile = store.get_planner_checkpoint(
        "plan_legacy_v4_fixture",
        "RECONCILE",
    )
    assert legacy_reconcile["status"] == "UNKNOWN"

    current = _provider()
    current._checkpoint_repository = store
    current_calls: list[str] = []
    _wire_provider(current, current_calls)
    with pytest.raises(RuntimeError, match="ambiguous paid-call outcome"):
        asyncio.run(
            current.generate(event=event, context=context, system_prompt="unused")
        )
    assert current_calls == []

    current_plan_id = current._planner_plan_id(context)
    # The cross-contract safety preflight runs before every provider call or
    # checkpoint migration. An ambiguous paid request therefore cannot be
    # hidden behind apparently harmless topology work.
    assert store.get_planner_checkpoint(current_plan_id, "SYSTEM_MAP") is None

    authorized = store.recover_planner_checkpoint(
        project_id=context.project.id,
        plan_id="plan_legacy_v4_fixture",
        phase_key="RECONCILE",
        expected_attempt_id=legacy_reconcile["attempt_id"],
        expected_revision=legacy_reconcile["revision"],
        action="AUTHORIZE_NEW_ATTEMPT",
        request_id="authorize-cross-contract-reconcile",
    )
    assert authorized["status"] == "RETRYABLE"

    decision = asyncio.run(
        current.generate(event=event, context=context, system_prompt="unused")
    )
    assert _architecture_from_decision(decision).version == 1
    assert current_calls == ["RECONCILE"]
    imported_system_map = store.get_planner_checkpoint(current_plan_id, "SYSTEM_MAP")
    assert imported_system_map["delivery_stage"] == "MIGRATED_CHECKPOINT"
    assert imported_system_map["migrated_from_plan_id"] == "plan_legacy_v4_fixture"
    assert current.last_usage["phases"][-1]["phase"] == "RECONCILE"


def test_cross_contract_unknown_fence_is_not_limited_to_recent_100_checkpoints():
    store = _CheckpointStore()
    context, event = _bootstrap_context()
    legacy = _provider()
    legacy._checkpoint_repository = store
    legacy._planner_plan_id = MethodType(
        lambda self, _context: "plan_old_unknown_beyond_page",
        legacy,
    )
    calls: list[str] = []
    _wire_provider(legacy, calls)

    async def ambiguous_reconcile(self, model_id: str, prompt: str):
        metadata = self._current_invocation_metadata()
        assert metadata is not None
        self._transition_active_planner_checkpoint(
            "IN_FLIGHT",
            provider={"requested_model": model_id},
        )
        metadata["planner_request_started"] = True
        metadata["planner_dispatch"] = {"requested_model": model_id}
        raise TimeoutError("old reconciliation outcome is unknown")

    legacy._invoke_reconcile = MethodType(ambiguous_reconcile, legacy)
    with pytest.raises(RuntimeError, match="timed out"):
        asyncio.run(legacy.generate(event=event, context=context, system_prompt="unused"))

    for index in range(110):
        store.rows[(f"plan_noise_{index}", f"NOISE:{index}")] = {
            "schema": "archbro.initial_planner_phase.v1",
            "plan_id": f"plan_noise_{index}",
            "project_id": context.project.id,
            "phase_key": f"NOISE:{index}",
            "attempt_id": f"attempt_noise_{index}",
            "status": "COMPLETED",
            "delivery_stage": "RESPONSE_RECORDED",
            "validation": {"status": "PASS"},
            "validated_output": {},
            "revision": 1,
            "owner_generation": 1,
        }

    current = _provider()
    current._checkpoint_repository = store
    current_calls: list[str] = []
    _wire_provider(current, current_calls)
    with pytest.raises(RuntimeError, match="ambiguous paid-call outcome"):
        asyncio.run(
            current.generate(event=event, context=context, system_prompt="unused")
        )
    assert current_calls == []


def test_reconcile_missing_relationships_gets_one_bounded_repair_phase():
    store = _CheckpointStore()
    context, event = _bootstrap_context()
    provider = _provider()
    provider._checkpoint_repository = store
    calls: list[str] = []
    _wire_provider(provider, calls)

    async def reconcile_with_repair(self, model_id: str, prompt: str):
        is_repair = "RECONCILE REPAIR phase" in prompt
        calls.append("RECONCILE_REPAIR:1" if is_repair else "RECONCILE")
        wire = (
            GeminiReconcileWire(
                summary="Repaired connected architecture",
                relationships=[
                    Relationship(
                        source="experience_capability",
                        target="domain_capability",
                        relationship_type="HTTPS",
                    ),
                    Relationship(
                        source="domain_capability",
                        target="coordination_capability",
                        relationship_type="CALLS",
                    ),
                    Relationship(
                        source="coordination_capability",
                        target="state_capability",
                        relationship_type="WRITES",
                    ),
                ],
                tasks=[
                    TaskProposal(
                        title="Build connected workflow",
                        related_component="domain_capability",
                    )
                ],
            )
            if is_repair
            else GeminiReconcileWire(
                summary="Complete response that omitted relationships",
                tasks=[
                    TaskProposal(
                        title="Build disconnected workflow",
                        related_component="domain_capability",
                    )
                ],
            )
        )
        metadata = self._current_invocation_metadata()
        assert metadata is not None
        provider_metadata = {
            "requested_model": model_id,
            "observed_model_version": model_id,
            "finish_reason": "STOP",
            "provider_response_received": True,
            "response_reprocessable": True,
            "raw_model_output": wire.model_dump_json(),
        }
        self._transition_active_planner_checkpoint(
            "RESPONSE_RECORDED",
            provider=provider_metadata,
        )
        metadata["planner_request_started"] = True
        metadata["planner_request_in_flight"] = False
        metadata["planner_response"] = provider_metadata
        return wire

    provider._invoke_reconcile = MethodType(reconcile_with_repair, provider)
    decision = asyncio.run(
        provider.generate(event=event, context=context, system_prompt="unused")
    )

    architecture = _architecture_from_decision(decision)
    assert architecture.version == 1
    assert len(architecture.relationships) == 3
    assert calls == [
        "SYSTEM_MAP",
        "EXPAND_SCOPE:experience",
        "EXPAND_SCOPE:domain",
        "EXPAND_SCOPE:coordination",
        "EXPAND_SCOPE:state",
        "RECONCILE",
        "RECONCILE_REPAIR:1",
    ]
    plan_id = provider._planner_plan_id(context)
    initial = store.get_planner_checkpoint(plan_id, "RECONCILE")
    repaired = store.get_planner_checkpoint(plan_id, "RECONCILE_REPAIR:1")
    assert initial["status"] == "REPAIR_REQUIRED"
    assert initial["validation"]["status"] == "REPAIR_REQUIRED"
    assert initial["validated_output"]["relationships"] == []
    assert repaired["status"] == "COMPLETED"
    assert repaired["validation"]["status"] == "PASS"


def test_explicit_permanent_provider_rejection_is_failed_not_unknown():
    class StructuredProviderError(RuntimeError):
        def __init__(self, code: int, status: str, message: str) -> None:
            self.code = code
            self.status = status
            super().__init__(message)

    store = _CheckpointStore()
    provider = _provider()
    provider._checkpoint_repository = store
    context, event = _bootstrap_context()
    calls: list[str] = []

    async def rejected_system_map(self, model_id: str, prompt: str):
        calls.append(model_id)
        metadata = self._current_invocation_metadata()
        assert metadata is not None
        dispatch = {
            "requested_model": model_id,
            "transport": "vertex",
            "error_type": "ClientError",
            "provider_response_received": True,
            "retryable_provider_error": False,
            "http_status_code": 400,
            "provider_status": "INVALID_ARGUMENT",
        }
        self._transition_active_planner_checkpoint("IN_FLIGHT", provider=dispatch)
        metadata["planner_request_started"] = True
        metadata["planner_dispatch"] = dispatch
        raise StructuredProviderError(400, "INVALID_ARGUMENT", "400 invalid schema")

    provider._invoke_system_map = MethodType(rejected_system_map, provider)
    with pytest.raises(RuntimeError, match="invalid schema"):
        asyncio.run(provider.generate(event=event, context=context, system_prompt="unused"))

    assert calls == ["gemini-primary"]
    checkpoint = next(iter(store.rows.values()))
    assert checkpoint["status"] == "FAILED"
    assert checkpoint["delivery_stage"] == "PROVIDER_REJECTED"
    assert checkpoint["validation"]["status"] == "FAIL"
    assert checkpoint["provider"]["http_status_code"] == 400


def test_architecture_admission_timeout_sends_no_provider_request():
    provider = _provider()
    provider.architecture_queue_timeout_seconds = 0.01
    calls: list[str] = []
    _wire_provider(provider, calls)
    context, event = _bootstrap_context()

    async def scenario() -> None:
        semaphore = provider._get_architecture_admission_semaphore()
        await semaphore.acquire()
        try:
            with pytest.raises(RuntimeError, match="generation is busy"):
                await provider.generate(
                    event=event,
                    context=context,
                    system_prompt="unused",
                )
        finally:
            semaphore.release()

    asyncio.run(scenario())
    assert calls == []
    assert provider.last_usage["admission"]["status"] == "TIMEOUT"
    assert provider.last_usage["admission"]["max_concurrency"] == 1
    assert provider.last_usage["phases"] == []


def test_started_planner_checkpoint_refuses_automatic_paid_replay():
    store = _CheckpointStore()
    provider = _provider()
    provider._checkpoint_repository = store
    context, event = _bootstrap_context()
    plan_id = provider._planner_plan_id(context)
    prompt = provider._system_map_prompt(event=event, context=context)
    identity = provider._planner_checkpoint_identity(
        phase_key="SYSTEM_MAP",
        prompt=prompt,
        snapshot_before=None,
        invoke_name="_invoke_system_map",
    )
    store.put_planner_checkpoint(
        project_id=context.project.id,
        plan_id=plan_id,
        phase_key="SYSTEM_MAP",
        data={
            "schema": "archbro.initial_planner_phase.v1",
            "plan_id": plan_id,
            "project_id": context.project.id,
            **identity,
            "status": "STARTED",
        },
    )
    calls: list[str] = []
    _wire_provider(provider, calls)

    with pytest.raises(RuntimeError, match="refusing automatic paid-call replay"):
        asyncio.run(provider.generate(event=event, context=context, system_prompt="unused"))
    assert calls == []


def test_planner_timeout_is_unknown_and_never_falls_back():
    store = _CheckpointStore()
    provider = _provider()
    provider._checkpoint_repository = store
    context, event = _bootstrap_context()
    calls: list[str] = []

    async def timed_out_system_map(self, model_id: str, prompt: str):
        calls.append(model_id)
        raise TimeoutError("provider completion state is unknown")

    provider._invoke_system_map = MethodType(timed_out_system_map, provider)

    with pytest.raises(RuntimeError, match="timed out"):
        asyncio.run(provider.generate(event=event, context=context, system_prompt="unused"))

    assert calls == ["gemini-primary"]
    checkpoint = next(iter(store.rows.values()))
    assert checkpoint["status"] == "UNKNOWN"

    calls.clear()
    with pytest.raises(RuntimeError, match="refusing automatic paid-call replay"):
        asyncio.run(provider.generate(event=event, context=context, system_prompt="unused"))
    assert calls == []


def test_temporary_unavailable_after_dispatch_is_unknown_and_never_falls_back():
    store = _CheckpointStore()
    provider = _provider()
    provider._checkpoint_repository = store
    provider._begin_invocation_metadata()
    context, event = _bootstrap_context()
    plan_id = provider._planner_plan_id(context)
    prompt = provider._system_map_prompt(event=event, context=context)
    calls: list[str] = []

    async def unavailable_after_dispatch(self, model_id: str, _prompt: str):
        calls.append(model_id)
        metadata = self._current_invocation_metadata()
        assert metadata is not None
        metadata["planner_request_started"] = True
        self._transition_active_planner_checkpoint(
            "IN_FLIGHT",
            provider={"requested_model": model_id},
        )
        raise RuntimeError("503 UNAVAILABLE: dispatch outcome is unknown")

    provider._invoke_system_map = MethodType(unavailable_after_dispatch, provider)
    with pytest.raises(RuntimeError, match="retryable provider error"):
        asyncio.run(
            provider._run_checkpointed_planner_phase(
                plan_id=plan_id,
                project_id=context.project.id,
                phase_key="SYSTEM_MAP",
                invoke_name="_invoke_system_map",
                prompt=prompt,
                output_model=GeminiSystemMapWire,
                snapshot_before=None,
                global_deadline=time.perf_counter() + 1.0,
                validate=lambda _wire: {},
            )
        )

    assert calls == ["gemini-primary"]
    durable = store.get_planner_checkpoint(plan_id, "SYSTEM_MAP")
    assert durable is not None
    assert durable["status"] == "UNKNOWN"
    assert durable["delivery_stage"] == "IN_FLIGHT"


def test_explicit_recovery_fences_late_owner_and_is_idempotent():
    store = _CheckpointStore()
    context, _event = _bootstrap_context()
    started = {
        "schema": "archbro.initial_planner_phase.v1",
        "plan_id": "plan-fenced-recovery",
        "project_id": context.project.id,
        "phase_key": "SYSTEM_MAP",
        "input_sha256": "a" * 64,
        "attempt_id": "attempt-old",
        "delivery_stage": "PREPARED",
        "status": "STARTED",
    }
    claimed, owner = store.claim_planner_checkpoint(
        project_id=context.project.id,
        plan_id="plan-fenced-recovery",
        phase_key="SYSTEM_MAP",
        data=started,
    )
    assert claimed is True

    recovered = store.recover_planner_checkpoint(
        project_id=context.project.id,
        plan_id="plan-fenced-recovery",
        phase_key="SYSTEM_MAP",
        expected_attempt_id="attempt-old",
        expected_revision=owner["revision"],
        action="RECLAIM_PREPARED",
        request_id="recovery-1",
    )
    replay = store.recover_planner_checkpoint(
        project_id=context.project.id,
        plan_id="plan-fenced-recovery",
        phase_key="SYSTEM_MAP",
        expected_attempt_id="attempt-old",
        expected_revision=owner["revision"],
        action="RECLAIM_PREPARED",
        request_id="recovery-1",
    )
    assert replay == recovered
    assert recovered["status"] == "RETRYABLE"
    assert recovered["owner_generation"] > owner["owner_generation"]

    with pytest.raises(RuntimeError, match="revision changed|owner generation changed"):
        store.put_planner_checkpoint(
            project_id=context.project.id,
            plan_id="plan-fenced-recovery",
            phase_key="SYSTEM_MAP",
            data={**owner, "status": "COMPLETED"},
            expected_revision=owner["revision"],
            expected_owner_generation=owner["owner_generation"],
        )
    assert store.get_planner_checkpoint("plan-fenced-recovery", "SYSTEM_MAP") == recovered


def test_in_flight_checkpoint_requires_explicit_new_attempt_authorization():
    store = _CheckpointStore()
    context, _event = _bootstrap_context()
    claimed, owner = store.claim_planner_checkpoint(
        project_id=context.project.id,
        plan_id="plan-in-flight",
        phase_key="SYSTEM_MAP",
        data={
            "schema": "archbro.initial_planner_phase.v1",
            "plan_id": "plan-in-flight",
            "project_id": context.project.id,
            "phase_key": "SYSTEM_MAP",
            "input_sha256": "a" * 64,
            "attempt_id": "attempt-in-flight",
            "delivery_stage": "IN_FLIGHT",
            "status": "UNKNOWN",
        },
    )
    assert claimed is True
    with pytest.raises(ValueError, match="forbidden after provider dispatch"):
        store.recover_planner_checkpoint(
            project_id=context.project.id,
            plan_id="plan-in-flight",
            phase_key="SYSTEM_MAP",
            expected_attempt_id="attempt-in-flight",
            expected_revision=owner["revision"],
            action="RECLAIM_PREPARED",
            request_id="bad-reclaim",
        )
    recovered = store.recover_planner_checkpoint(
        project_id=context.project.id,
        plan_id="plan-in-flight",
        phase_key="SYSTEM_MAP",
        expected_attempt_id="attempt-in-flight",
        expected_revision=owner["revision"],
        action="AUTHORIZE_NEW_ATTEMPT",
        request_id="new-attempt-1",
    )
    assert recovered["status"] == "RETRYABLE"


def test_recorded_response_can_be_reprocessed_without_provider_replay():
    store = _CheckpointStore()
    provider = _provider()
    provider._checkpoint_repository = store
    context, event = _bootstrap_context()
    plan_id = provider._planner_plan_id(context)
    prompt = provider._system_map_prompt(event=event, context=context)
    identity = provider._planner_checkpoint_identity(
        phase_key="SYSTEM_MAP",
        prompt=prompt,
        snapshot_before=None,
        invoke_name="_invoke_system_map",
    )
    raw_output = GeminiSystemMapWire(
        summary="Four truthful boundaries",
        roots=_roots(),
    ).model_dump_json()
    stored = store.put_planner_checkpoint(
        project_id=context.project.id,
        plan_id=plan_id,
        phase_key="SYSTEM_MAP",
        data={
            "schema": "archbro.initial_planner_phase.v1",
            "plan_id": plan_id,
            "project_id": context.project.id,
            "phase_key": "SYSTEM_MAP",
            **identity,
            "attempt_id": "attempt-response",
            "delivery_stage": "RESPONSE_RECORDED",
            "status": "FAILED",
            "provider": {
                "requested_model": "gemini-primary",
                "response_reprocessable": True,
                "raw_model_output": raw_output,
            },
        },
    )
    store.recover_planner_checkpoint(
        project_id=context.project.id,
        plan_id=plan_id,
        phase_key="SYSTEM_MAP",
        expected_attempt_id="attempt-response",
        expected_revision=stored["revision"],
        action="REPROCESS_RESPONSE",
        request_id="reprocess-1",
    )

    calls: list[str] = []
    _wire_provider(provider, calls)
    decision = asyncio.run(provider.generate(event=event, context=context, system_prompt="unused"))
    assert _architecture_from_decision(decision).version == 1
    assert "SYSTEM_MAP" not in calls
    assert calls == [
        "EXPAND_SCOPE:experience",
        "EXPAND_SCOPE:domain",
        "EXPAND_SCOPE:coordination",
        "EXPAND_SCOPE:state",
        "RECONCILE",
    ]
    assert store.get_planner_checkpoint(plan_id, "SYSTEM_MAP")["status"] == "COMPLETED"


def test_recorded_nonterminal_response_cannot_be_reprocessed():
    store = _CheckpointStore()
    context, _event = _bootstrap_context()
    stored = store.put_planner_checkpoint(
        project_id=context.project.id,
        plan_id="plan-nonterminal-response",
        phase_key="SYSTEM_MAP",
        data={
            "schema": "archbro.initial_planner_phase.v1",
            "plan_id": "plan-nonterminal-response",
            "project_id": context.project.id,
            "phase_key": "SYSTEM_MAP",
            "attempt_id": "attempt-nonterminal",
            "delivery_stage": "RESPONSE_RECORDED",
            "status": "FAILED",
            "provider": {"response_reprocessable": False, "raw_model_output": "{}"},
        },
    )
    with pytest.raises(ValueError, match="not safe for local reprocessing"):
        store.recover_planner_checkpoint(
            project_id=context.project.id,
            plan_id="plan-nonterminal-response",
            phase_key="SYSTEM_MAP",
            expected_attempt_id="attempt-nonterminal",
            expected_revision=stored["revision"],
            action="REPROCESS_RESPONSE",
            request_id="reprocess-bad",
        )


def test_scope_id_parser_preserves_dotted_server_owned_id():
    prompt = (
        "EXPAND_SCOPE phase for root scope_id=api.gateway. "
        "Return only NEW descendants inside this named root; never regenerate or edit accepted nodes."
    )
    assert GeminiProvider._scope_id_from_prompt(prompt) == "api.gateway"


@pytest.mark.parametrize("reconcile", [False, True])
def test_real_planner_transport_keeps_sdk_single_attempt_and_uses_phase_budget(monkeypatch, reconcile):
    from google import genai

    provider = _provider()
    provider._api_key = "transport-test-key"
    captured: dict[str, object] = {}

    class FakeModels:
        async def generate_content(self, *, model, contents, config):
            captured["model"] = model
            captured["contents"] = contents
            captured["config"] = config
            output = (
                GeminiReconcileWire(summary="Transport contract", tasks=[TaskProposal(title="Implement counter")])
                if reconcile else GeminiSystemMapWire(summary="Transport contract", roots=_roots())
            ).model_dump_json()
            return SimpleNamespace(
                candidates=[
                    SimpleNamespace(
                        finish_reason="STOP",
                        content=SimpleNamespace(parts=[SimpleNamespace(text=output, thought=False)]),
                    )
                ],
                usage_metadata=None,
                model_version="gemini-transport-test",
                response_id="response-1",
            )

    class FakeAio:
        def __init__(self):
            self.models = FakeModels()

        async def aclose(self):
            return None

    class FakeClient:
        def __init__(self, *, api_key, http_options):
            captured["api_key"] = api_key
            captured["http_options"] = http_options
            self.aio = FakeAio()

    monkeypatch.setattr(genai, "Client", FakeClient)
    result = asyncio.run(
        provider._invoke_planner_structured(
            "gemini-transport",
            "SYSTEM_MAP transport test",
            GeminiReconcileWire if reconcile else GeminiSystemMapWire,
        )
    )

    assert result.summary == "Transport contract"
    assert captured["model"] == "gemini-transport"
    config = captured["config"]
    expected_schema = (GeminiReconcileWire if reconcile else GeminiSystemMapWire).model_json_schema()
    if reconcile:
        assert expected_schema["properties"]["relationships"].pop("maxItems") == 80
        required = list(expected_schema.get("required") or [])
        for field in ("relationships", "tasks"):
            if field not in required:
                required.append(field)
        expected_schema["required"] = required
    assert config.response_json_schema == expected_schema
    assert "$defs" in json.dumps(config.response_json_schema)
    assert config.temperature is None
    assert config.max_output_tokens == (16384 if reconcile else 4096)
    assert config.thinking_config.thinking_level.value.lower() == ("medium" if reconcile else "low")
    http_options = captured["http_options"]
    assert http_options.retry_options.attempts == 1


@pytest.mark.parametrize(
    ("phase_name", "output_model", "prompt", "invoke_kwargs", "expected_budgets"),
    [
        (
            "SYSTEM_MAP",
            GeminiSystemMapWire,
            "SYSTEM_MAP output escalation fixture",
            {},
            [4096, 8192],
        ),
        (
            "EXPAND_SCOPE",
            GeminiScopeDeltaWire,
            "EXPAND_SCOPE output escalation fixture",
            {"expected_scope_id": "experience"},
            [8192, 16384],
        ),
        (
            "RECONCILE",
            GeminiReconcileWire,
            "RECONCILE output escalation fixture",
            {"reconcile": True},
            [16384, 32768],
        ),
    ],
)
def test_max_tokens_expands_only_the_current_phase_until_stop(
    phase_name,
    output_model,
    prompt,
    invoke_kwargs,
    expected_budgets,
):
    provider = _provider()
    provider._begin_invocation_metadata()
    observed_budgets: list[int] = []

    if output_model is GeminiSystemMapWire:
        completed_output = GeminiSystemMapWire(
            summary="System map completed after bounded expansion.",
            roots=_roots(),
        ).model_dump_json()
    elif output_model is GeminiScopeDeltaWire:
        completed_output = GeminiScopeDeltaWire(
            scope_id="experience",
            components=[],
        ).model_dump_json()
    else:
        completed_output = GeminiReconcileWire(
            summary="Reconciliation completed after bounded expansion.",
            tasks=[TaskProposal(title="Implement bounded planner output")],
        ).model_dump_json()

    class FakeModels:
        async def generate_content(self, *, model, contents, config):
            observed_budgets.append(config.max_output_tokens)
            if len(observed_budgets) == 1:
                return SimpleNamespace(
                    candidates=[
                        SimpleNamespace(
                            finish_reason="MAX_TOKENS",
                            content=SimpleNamespace(
                                parts=[
                                    SimpleNamespace(
                                        text='{"status":"READY","partial":',
                                        thought=False,
                                    )
                                ]
                            ),
                        )
                    ],
                    usage_metadata=None,
                    model_version="gemini-output-escalation-test",
                    response_id="response-truncated",
                )
            return SimpleNamespace(
                candidates=[
                    SimpleNamespace(
                        finish_reason="STOP",
                        content=SimpleNamespace(
                            parts=[SimpleNamespace(text=completed_output, thought=False)]
                        ),
                    )
                ],
                usage_metadata=None,
                model_version="gemini-output-escalation-test",
                response_id="response-complete",
            )

    class FakeAio:
        def __init__(self):
            self.models = FakeModels()

        async def aclose(self):
            return None

    class FakeClient:
        def __init__(self):
            self.aio = FakeAio()

    class FakeFactory:
        transport = "vertex"

        def create_client(self, *, http_timeout_ms):
            return FakeClient()

    provider._google_client_factory = FakeFactory()
    result = asyncio.run(
        provider._invoke_planner_structured(
            "gemini-3.8-flash",
            prompt,
            output_model,
            **invoke_kwargs,
        )
    )

    assert result is not None, phase_name
    assert observed_budgets == expected_budgets
    metadata = provider._current_invocation_metadata()
    assert metadata is not None
    response = metadata["planner_response"]
    assert response["sdk_retry_attempt_limit"] == 1
    assert response["provider_attempt_count"] == 2
    assert response["output_token_ceiling"] == 65536
    assert [attempt["outcome"] for attempt in response["provider_attempts"]] == [
        "TRUNCATED",
        "COMPLETED",
    ]
    assert response["provider_rejections"] == []
    assert response["retry_delays_seconds"] == []
    assert response["retryable_generation"] is False


def test_max_tokens_resume_uses_saved_larger_budget_without_replaying_phase_input():
    store = _CheckpointStore()
    context, event = _bootstrap_context()
    prompt = GeminiProvider._system_map_prompt(event=event, context=context)
    first = _provider()
    first._checkpoint_repository = store
    first.architecture_retry_attempts = 1
    first._begin_invocation_metadata()
    first_budgets: list[int] = []

    class TruncatedModels:
        async def generate_content(self, *, model, contents, config):
            first_budgets.append(config.max_output_tokens)
            return SimpleNamespace(
                candidates=[
                    SimpleNamespace(
                        finish_reason="MAX_TOKENS",
                        content=SimpleNamespace(
                            parts=[SimpleNamespace(text='{"status":', thought=False)]
                        ),
                    )
                ],
                usage_metadata=None,
                model_version="gemini-resume-test",
                response_id="response-truncated",
            )

    class TruncatedAio:
        def __init__(self):
            self.models = TruncatedModels()

        async def aclose(self):
            return None

    class TruncatedFactory:
        transport = "vertex"

        def create_client(self, *, http_timeout_ms):
            return SimpleNamespace(aio=TruncatedAio())

    first._google_client_factory = TruncatedFactory()
    plan_id = first._planner_plan_id(context)
    with pytest.raises(RuntimeError, match="saved phase will resume"):
        asyncio.run(
            first._run_checkpointed_planner_phase(
                plan_id=plan_id,
                project_id=context.project.id,
                phase_key="SYSTEM_MAP",
                invoke_name="_invoke_system_map",
                prompt=prompt,
                output_model=GeminiSystemMapWire,
                snapshot_before=None,
                global_deadline=time.perf_counter() + 2.0,
                validate=lambda _wire: {},
            )
        )

    assert first_budgets == [4096]
    truncated = store.get_planner_checkpoint(plan_id, "SYSTEM_MAP")
    assert truncated["status"] == "RETRYABLE"
    assert truncated["delivery_stage"] == "TRUNCATED_RESPONSE"
    assert truncated["resume_max_output_tokens"] == 8192
    assert truncated["provider"]["retryable_generation"] is True
    assert truncated["provider"]["next_max_output_tokens"] == 8192

    resumed = _provider()
    resumed._checkpoint_repository = store
    resumed._begin_invocation_metadata()
    resumed_budgets: list[int] = []

    class CompletedModels:
        async def generate_content(self, *, model, contents, config):
            resumed_budgets.append(config.max_output_tokens)
            output = GeminiSystemMapWire(
                summary="Resumed from the persisted larger budget.",
                roots=_roots(),
            ).model_dump_json()
            return SimpleNamespace(
                candidates=[
                    SimpleNamespace(
                        finish_reason="STOP",
                        content=SimpleNamespace(
                            parts=[SimpleNamespace(text=output, thought=False)]
                        ),
                    )
                ],
                usage_metadata=None,
                model_version="gemini-resume-test",
                response_id="response-complete",
            )

    class CompletedAio:
        def __init__(self):
            self.models = CompletedModels()

        async def aclose(self):
            return None

    class CompletedFactory:
        transport = "vertex"

        def create_client(self, *, http_timeout_ms):
            return SimpleNamespace(aio=CompletedAio())

    resumed._google_client_factory = CompletedFactory()
    result = asyncio.run(
        resumed._run_checkpointed_planner_phase(
            plan_id=plan_id,
            project_id=context.project.id,
            phase_key="SYSTEM_MAP",
            invoke_name="_invoke_system_map",
            prompt=prompt,
            output_model=GeminiSystemMapWire,
            snapshot_before=None,
            global_deadline=time.perf_counter() + 2.0,
            validate=lambda _wire: {},
        )
    )

    assert result.summary == "Resumed from the persisted larger budget."
    assert resumed_budgets == [8192]
    completed = store.get_planner_checkpoint(plan_id, "SYSTEM_MAP")
    assert completed["status"] == "COMPLETED"
    assert "resume_max_output_tokens" not in completed


def test_max_tokens_at_hard_ceiling_is_failed_not_unknown():
    store = _CheckpointStore()
    context, event = _bootstrap_context()
    provider = _provider()
    provider._checkpoint_repository = store
    provider.system_map_max_output_tokens = 65536
    provider._begin_invocation_metadata()
    calls: list[int] = []

    class CeilingModels:
        async def generate_content(self, *, model, contents, config):
            calls.append(config.max_output_tokens)
            return SimpleNamespace(
                candidates=[
                    SimpleNamespace(
                        finish_reason="MAX_TOKENS",
                        content=SimpleNamespace(
                            parts=[SimpleNamespace(text='{"partial":', thought=False)]
                        ),
                    )
                ],
                usage_metadata=None,
                model_version="gemini-ceiling-test",
                response_id="response-at-ceiling",
            )

    class CeilingAio:
        def __init__(self):
            self.models = CeilingModels()

        async def aclose(self):
            return None

    class CeilingFactory:
        transport = "vertex"

        def create_client(self, *, http_timeout_ms):
            return SimpleNamespace(aio=CeilingAio())

    provider._google_client_factory = CeilingFactory()
    prompt = provider._system_map_prompt(event=event, context=context)
    plan_id = provider._planner_plan_id(context)
    with pytest.raises(RuntimeError, match="hard output token ceiling"):
        asyncio.run(
            provider._run_checkpointed_planner_phase(
                plan_id=plan_id,
                project_id=context.project.id,
                phase_key="SYSTEM_MAP",
                invoke_name="_invoke_system_map",
                prompt=prompt,
                output_model=GeminiSystemMapWire,
                snapshot_before=None,
                global_deadline=time.perf_counter() + 2.0,
                validate=lambda _wire: {},
            )
        )

    assert calls == [65536]
    checkpoint = store.get_planner_checkpoint(plan_id, "SYSTEM_MAP")
    assert checkpoint["status"] == "FAILED"
    assert checkpoint["delivery_stage"] == "TRUNCATED_RESPONSE"
    assert checkpoint["validation"]["status"] == "FAIL"
    assert checkpoint["provider"]["truncated_response"] is True
    assert checkpoint["provider"]["retryable_generation"] is False


def test_planner_retries_only_explicit_429_and_honors_retry_delay():
    from google.genai import errors as genai_errors

    provider = _provider()
    provider._begin_invocation_metadata()
    provider.retry_jitter = 0.0
    captured: dict[str, object] = {"calls": 0, "closed": False}
    delays: list[float] = []

    class FakeModels:
        async def generate_content(self, *, model, contents, config):
            captured["calls"] = int(captured["calls"]) + 1
            if captured["calls"] == 1:
                raise genai_errors.ClientError(
                    429,
                    {
                        "error": {
                            "code": 429,
                            "status": "RESOURCE_EXHAUSTED",
                            "message": "shared capacity exhausted",
                            "details": [
                                {
                                    "@type": "type.googleapis.com/google.rpc.RetryInfo",
                                    "retryDelay": "2.5s",
                                }
                            ],
                        }
                    },
                    None,
                )
            if captured["calls"] == 2:
                raise genai_errors.ClientError(
                    429,
                    {
                        "error": {
                            "code": 429,
                            "status": "RESOURCE_EXHAUSTED",
                            "message": "shared capacity still exhausted",
                        }
                    },
                    None,
                )
            output = GeminiSystemMapWire(
                summary="Recovered after explicit capacity rejection",
                roots=_roots(),
            ).model_dump_json()
            return SimpleNamespace(
                candidates=[
                    SimpleNamespace(
                        finish_reason="STOP",
                        content=SimpleNamespace(
                            parts=[SimpleNamespace(text=output, thought=False)]
                        ),
                    )
                ],
                usage_metadata=None,
                model_version="gemini-retry-test",
                response_id="response-after-retry",
            )

    class FakeAio:
        def __init__(self):
            self.models = FakeModels()

        async def aclose(self):
            captured["closed"] = True

    class FakeClient:
        def __init__(self):
            self.aio = FakeAio()

    class FakeFactory:
        transport = "vertex"

        def create_client(self, *, http_timeout_ms):
            captured["http_timeout_ms"] = http_timeout_ms
            return FakeClient()

    async def record_sleep(self, delay_seconds: float) -> None:
        delays.append(delay_seconds)

    provider._google_client_factory = FakeFactory()
    provider._sleep_for_planner_retry = MethodType(record_sleep, provider)

    result = asyncio.run(
        provider._invoke_planner_structured(
            "gemini-3.8-flash",
            "SYSTEM_MAP retry fixture",
            GeminiSystemMapWire,
        )
    )

    assert result.summary == "Recovered after explicit capacity rejection"
    assert captured["calls"] == 3
    assert captured["closed"] is True
    assert delays == [2.5, 2.0]
    metadata = provider._current_invocation_metadata()
    assert metadata is not None
    response = metadata["planner_response"]
    assert response["retry_attempt_limit"] == 5
    assert response["provider_attempt_count"] == 3
    assert response["retry_delays_seconds"] == [2.5, 2.0]
    assert [item["http_status_code"] for item in response["provider_rejections"]] == [429, 429]
    assert all(item["explicit_response"] is True for item in response["provider_rejections"])
    assert metadata["planner_request_in_flight"] is False


def test_explicit_429_is_durably_retryable_before_backoff_sleep():
    from google.genai import errors as genai_errors

    store = _CheckpointStore()
    context, event = _bootstrap_context()
    provider = _provider()
    provider._checkpoint_repository = store
    provider.retry_jitter = 0.0
    provider._begin_invocation_metadata()
    prompt = provider._system_map_prompt(event=event, context=context)
    plan_id = provider._planner_plan_id(context)
    calls: list[int] = []
    observed_during_sleep: list[dict] = []

    class FakeModels:
        async def generate_content(self, *, model, contents, config):
            calls.append(config.max_output_tokens)
            if len(calls) == 1:
                raise genai_errors.ClientError(
                    429,
                    {
                        "error": {
                            "code": 429,
                            "status": "RESOURCE_EXHAUSTED",
                            "message": "shared capacity exhausted",
                        }
                    },
                    None,
                )
            output = GeminiSystemMapWire(
                summary="Recovered after durable retry checkpoint.",
                roots=_roots(),
            ).model_dump_json()
            return SimpleNamespace(
                candidates=[
                    SimpleNamespace(
                        finish_reason="STOP",
                        content=SimpleNamespace(
                            parts=[SimpleNamespace(text=output, thought=False)]
                        ),
                    )
                ],
                usage_metadata=None,
                model_version="gemini-durable-retry-test",
                response_id="response-after-durable-retry",
            )

    class FakeAio:
        def __init__(self):
            self.models = FakeModels()

        async def aclose(self):
            return None

    class FakeFactory:
        transport = "vertex"

        def create_client(self, *, http_timeout_ms):
            return SimpleNamespace(aio=FakeAio())

    async def inspect_checkpoint_before_sleep(
        self: GeminiProvider,
        delay_seconds: float,
    ) -> None:
        checkpoint = store.get_planner_checkpoint(plan_id, "SYSTEM_MAP")
        assert checkpoint is not None
        observed_during_sleep.append(checkpoint)
        assert checkpoint["status"] == "RETRYABLE"
        assert checkpoint["delivery_stage"] == "PROVIDER_REJECTED"
        assert checkpoint["validation"]["status"] == "RETRYABLE"
        assert checkpoint["provider"]["provider_response_received"] is True
        assert checkpoint["provider"]["retryable_provider_error"] is True
        assert checkpoint["provider"]["http_status_code"] == 429
        assert checkpoint["provider"]["provider_attempt_count"] == 1
        assert len(checkpoint["provider"]["provider_rejections"]) == 1
        assert delay_seconds == 1.0

    provider._google_client_factory = FakeFactory()
    provider._sleep_for_planner_retry = MethodType(
        inspect_checkpoint_before_sleep,
        provider,
    )

    result = asyncio.run(
        provider._run_checkpointed_planner_phase(
            plan_id=plan_id,
            project_id=context.project.id,
            phase_key="SYSTEM_MAP",
            invoke_name="_invoke_system_map",
            prompt=prompt,
            output_model=GeminiSystemMapWire,
            snapshot_before=None,
            global_deadline=time.perf_counter() + 2.0,
            validate=lambda _wire: {},
        )
    )

    assert result.summary == "Recovered after durable retry checkpoint."
    assert calls == [4096, 4096]
    assert len(observed_during_sleep) == 1
    completed = store.get_planner_checkpoint(plan_id, "SYSTEM_MAP")
    assert completed is not None
    assert completed["status"] == "COMPLETED"
    assert completed["delivery_stage"] == "RESPONSE_RECORDED"
    assert completed["validation"]["status"] == "PASS"
    assert completed["provider"]["provider_attempt_count"] == 2


def test_planner_never_retries_ambiguous_transport_timeout():
    provider = _provider()
    provider._begin_invocation_metadata()
    captured: dict[str, object] = {"calls": 0, "closed": False}
    delays: list[float] = []

    class FakeModels:
        async def generate_content(self, *, model, contents, config):
            captured["calls"] = int(captured["calls"]) + 1
            raise TimeoutError("transport ended without a provider response")

    class FakeAio:
        def __init__(self):
            self.models = FakeModels()

        async def aclose(self):
            captured["closed"] = True

    class FakeClient:
        def __init__(self):
            self.aio = FakeAio()

    class FakeFactory:
        transport = "vertex"

        def create_client(self, *, http_timeout_ms):
            return FakeClient()

    async def record_sleep(self, delay_seconds: float) -> None:
        delays.append(delay_seconds)

    provider._google_client_factory = FakeFactory()
    provider._sleep_for_planner_retry = MethodType(record_sleep, provider)

    with pytest.raises(TimeoutError, match="without a provider response"):
        asyncio.run(
            provider._invoke_planner_structured(
                "gemini-3.8-flash",
                "SYSTEM_MAP ambiguous timeout fixture",
                GeminiSystemMapWire,
            )
        )

    assert captured["calls"] == 1
    assert captured["closed"] is True
    assert delays == []
    metadata = provider._current_invocation_metadata()
    assert metadata is not None
    dispatch = metadata["planner_dispatch"]
    assert dispatch["provider_attempt_count"] == 1
    assert dispatch["provider_response_received"] is False
    assert dispatch["retryable_provider_error"] is False


def test_model_deadline_during_explicit_rejection_backoff_stays_retryable():
    store = _CheckpointStore()
    provider = _provider()
    provider._checkpoint_repository = store
    provider.architecture_model_timeout_seconds = 0.01
    provider.architecture_phase_timeout_seconds = 0.05
    context, event = _bootstrap_context()

    async def explicit_rejection_then_backoff(
        self: GeminiProvider,
        model_id: str,
        prompt: str,
    ):
        metadata = self._current_invocation_metadata()
        assert metadata is not None
        dispatch = {
            "requested_model": model_id,
            "error_type": "ClientError",
            "provider_response_received": True,
            "retryable_provider_error": True,
            "http_status_code": 429,
            "provider_status": "RESOURCE_EXHAUSTED",
            "retry_attempt_limit": 5,
            "provider_attempt_count": 1,
        }
        self._transition_active_planner_checkpoint(
            "IN_FLIGHT",
            provider=dispatch,
        )
        metadata["planner_request_started"] = True
        metadata["planner_request_in_flight"] = False
        metadata["planner_dispatch"] = dispatch
        await asyncio.sleep(1)
        raise AssertionError("deadline should cancel the retry wait")

    provider._invoke_system_map = MethodType(
        explicit_rejection_then_backoff,
        provider,
    )

    with pytest.raises(RuntimeError, match="retry deadline after explicit provider rejection"):
        asyncio.run(
            provider.generate(event=event, context=context, system_prompt="unused")
        )

    checkpoint = next(iter(store.rows.values()))
    assert checkpoint["status"] == "RETRYABLE"
    assert checkpoint["delivery_stage"] == "PROVIDER_REJECTED"
    assert checkpoint["validation"]["status"] == "RETRYABLE"
    assert checkpoint["provider"]["http_status_code"] == 429


def test_reconcile_still_rejects_more_than_80_relationships():
    relationship = {"source": "counter_ui", "target": "counter_state", "relationship_type": "uses"}
    payload = {"relationships": [relationship] * 80, "tasks": [{"title": "Implement counter"}]}
    assert len(GeminiReconcileWire.model_validate(payload).relationships) == 80
    payload["relationships"].append(relationship)
    with pytest.raises(ValidationError, match="at most 80"):
        GeminiReconcileWire.model_validate(payload)


def test_planner_uses_the_provider_client_factory(monkeypatch):
    provider = _provider()
    provider._begin_invocation_metadata()
    captured: dict[str, object] = {}

    class FakeModels:
        async def generate_content(self, *, model, contents, config):
            output = GeminiSystemMapWire(
                summary="Factory transport contract",
                roots=_roots(),
            ).model_dump_json()
            return SimpleNamespace(
                candidates=[
                    SimpleNamespace(
                        finish_reason="STOP",
                        content=SimpleNamespace(
                            parts=[SimpleNamespace(text=output, thought=False)]
                        ),
                    )
                ],
                usage_metadata=None,
                model_version="gemini-factory-test",
                response_id="response-factory-1",
            )

    class FakeAio:
        def __init__(self):
            self.models = FakeModels()

        async def aclose(self):
            captured["closed"] = True

    class FakeClient:
        def __init__(self):
            self.aio = FakeAio()

    class FakeFactory:
        transport = "vertex"

        def create_client(self, *, http_timeout_ms, **retry_options):
            captured["http_timeout_ms"] = http_timeout_ms
            captured["retry_options"] = retry_options
            return FakeClient()

    provider._google_client_factory = FakeFactory()
    monkeypatch.delenv("GEMINI_ARCHITECTURE_HTTP_TIMEOUT_MS", raising=False)

    result = asyncio.run(
        provider._invoke_planner_structured(
            "gemini-factory",
            "SYSTEM_MAP factory test",
            GeminiSystemMapWire,
        )
    )

    assert result.summary == "Factory transport contract"
    assert captured["http_timeout_ms"] == 500
    assert captured["retry_options"] == {}
    assert captured["closed"] is True
    metadata = provider._current_invocation_metadata()
    assert metadata is not None
    assert metadata["planner_response"]["transport"] == "vertex"
    assert metadata["planner_response"]["http_timeout_ms"] == 500
    assert metadata["planner_response"]["retry_attempt_limit"] == 5
    assert metadata["planner_response"]["provider_attempt_count"] == 1


def test_architecture_http_timeout_never_undercuts_the_model_budget(monkeypatch):
    provider = _provider()
    provider.architecture_model_timeout_seconds = 90.0

    monkeypatch.setenv("GEMINI_ARCHITECTURE_HTTP_TIMEOUT_MS", "10000")
    assert provider._effective_architecture_http_timeout_ms() == 90_000

    monkeypatch.setenv("GEMINI_ARCHITECTURE_HTTP_TIMEOUT_MS", "120000")
    assert provider._effective_architecture_http_timeout_ms() == 120_000

    monkeypatch.setenv("GEMINI_ARCHITECTURE_HTTP_TIMEOUT_MS", "not-an-integer")
    with pytest.raises(ValueError, match="must be an integer"):
        provider._effective_architecture_http_timeout_ms()

    monkeypatch.setenv("GEMINI_ARCHITECTURE_HTTP_TIMEOUT_MS", "0")
    with pytest.raises(ValueError, match="greater than zero"):
        provider._effective_architecture_http_timeout_ms()


def test_planner_dispatch_failure_records_effective_deadline_without_model_output(monkeypatch):
    provider = _provider()
    provider.architecture_model_timeout_seconds = 90.0
    provider._begin_invocation_metadata()
    captured: dict[str, object] = {}

    class FakeModels:
        async def generate_content(self, *, model, contents, config):
            captured["model"] = model
            raise RuntimeError("504 DEADLINE_EXCEEDED: provider deadline")

    class FakeAio:
        def __init__(self):
            self.models = FakeModels()

        async def aclose(self):
            captured["closed"] = True

    class FakeClient:
        def __init__(self):
            self.aio = FakeAio()

    class FakeFactory:
        transport = "vertex"

        def create_client(self, *, http_timeout_ms, **retry_options):
            captured["http_timeout_ms"] = http_timeout_ms
            captured["retry_options"] = retry_options
            return FakeClient()

    provider._google_client_factory = FakeFactory()
    monkeypatch.setenv("GEMINI_ARCHITECTURE_HTTP_TIMEOUT_MS", "10000")

    with pytest.raises(RuntimeError, match="504 DEADLINE_EXCEEDED"):
        asyncio.run(
            provider._invoke_planner_structured(
                "gemini-3.8-flash",
                "SYSTEM_MAP deadline fixture",
                GeminiSystemMapWire,
            )
        )

    assert captured["http_timeout_ms"] == 90_000
    assert captured["retry_options"] == {}
    assert captured["closed"] is True
    metadata = provider._current_invocation_metadata()
    assert metadata is not None
    assert metadata["planner_response"] is None
    dispatch = metadata["planner_dispatch"]
    assert dispatch["requested_model"] == "gemini-3.8-flash"
    assert dispatch["http_timeout_ms"] == 90_000
    assert dispatch["transport"] == "vertex"
    assert dispatch["error_type"] == "RuntimeError"
    assert dispatch["latency_ms"] >= 0


def test_retryable_503_checkpoint_can_retry_without_relaxing_unknown_timeout_boundary():
    store = _CheckpointStore()
    provider = _provider()
    provider._checkpoint_repository = store
    context, event = _bootstrap_context()
    calls: list[str] = []

    async def unavailable(self, model_id: str, prompt: str):
        calls.append(model_id)
        raise RuntimeError("503 UNAVAILABLE: temporary outage")

    provider._invoke_system_map = MethodType(unavailable, provider)
    with pytest.raises(RuntimeError, match="retryable provider error"):
        asyncio.run(provider.generate(event=event, context=context, system_prompt="unused"))
    assert next(iter(store.rows.values()))["status"] == "RETRYABLE"

    calls.clear()
    _wire_provider(provider, calls)
    decision = asyncio.run(provider.generate(event=event, context=context, system_prompt="unused"))
    assert _architecture_from_decision(decision).version == 1
    assert calls[0] == "SYSTEM_MAP"


def test_retryable_checkpoint_identity_drift_fails_closed_before_provider_call():
    store = _CheckpointStore()
    provider = _provider()
    provider._checkpoint_repository = store
    context, event = _bootstrap_context()
    plan_id = provider._planner_plan_id(context)
    original_prompt = provider._system_map_prompt(event=event, context=context)
    identity = provider._planner_checkpoint_identity(
        phase_key="SYSTEM_MAP",
        prompt=original_prompt,
        snapshot_before=None,
        invoke_name="_invoke_system_map",
    )
    store.put_planner_checkpoint(
        project_id=context.project.id,
        plan_id=plan_id,
        phase_key="SYSTEM_MAP",
        data={
            "schema": "archbro.initial_planner_phase.v1",
            "plan_id": plan_id,
            "project_id": context.project.id,
            **identity,
            "status": "RETRYABLE",
        },
    )
    calls: list[str] = []

    async def should_not_run(self, model_id: str, prompt: str):
        calls.append(model_id)
        return GeminiSystemMapWire(summary="unexpected", roots=_roots())

    provider._invoke_system_map = MethodType(should_not_run, provider)
    with pytest.raises(RuntimeError, match="checkpoint identity mismatch"):
        asyncio.run(
            provider._run_checkpointed_planner_phase(
                plan_id=plan_id,
                project_id=context.project.id,
                phase_key="SYSTEM_MAP",
                invoke_name="_invoke_system_map",
                prompt=original_prompt + "\nchanged-input",
                output_model=GeminiSystemMapWire,
                snapshot_before=None,
                global_deadline=time.perf_counter() + 1.0,
                validate=lambda _wire: {},
            )
        )

    assert calls == []
    durable = store.get_planner_checkpoint(plan_id, "SYSTEM_MAP")
    assert durable is not None
    assert durable["status"] == "RETRYABLE"
    assert durable["input_sha256"] == identity["input_sha256"]


def test_local_validation_error_that_mentions_503_is_failed_and_never_replays_provider():
    store = _CheckpointStore()
    provider = _provider()
    provider._checkpoint_repository = store
    provider._begin_invocation_metadata()
    context, event = _bootstrap_context()
    plan_id = provider._planner_plan_id(context)
    prompt = provider._system_map_prompt(event=event, context=context)
    calls: list[str] = []

    async def system_map(self, model_id: str, prompt: str):
        calls.append(model_id)
        return GeminiSystemMapWire(summary="valid provider response", roots=_roots())

    def reject_after_response(_wire):
        raise ValueError("503 unavailable: local relationship validation rejected output")

    provider._invoke_system_map = MethodType(system_map, provider)
    with pytest.raises(ValueError, match="local relationship validation"):
        asyncio.run(
            provider._run_checkpointed_planner_phase(
                plan_id=plan_id,
                project_id=context.project.id,
                phase_key="SYSTEM_MAP",
                invoke_name="_invoke_system_map",
                prompt=prompt,
                output_model=GeminiSystemMapWire,
                snapshot_before=None,
                global_deadline=time.perf_counter() + 1.0,
                validate=reject_after_response,
            )
        )

    assert calls == ["gemini-primary"]
    durable = store.get_planner_checkpoint(plan_id, "SYSTEM_MAP")
    assert durable is not None
    assert durable["status"] == "FAILED"

    provider._begin_invocation_metadata()
    with pytest.raises(RuntimeError, match="refusing automatic paid-call replay"):
        asyncio.run(
            provider._run_checkpointed_planner_phase(
                plan_id=plan_id,
                project_id=context.project.id,
                phase_key="SYSTEM_MAP",
                invoke_name="_invoke_system_map",
                prompt=prompt,
                output_model=GeminiSystemMapWire,
                snapshot_before=None,
                global_deadline=time.perf_counter() + 1.0,
                validate=reject_after_response,
            )
        )
    assert calls == ["gemini-primary"]


def test_post_response_parse_failure_never_falls_back_to_a_second_candidate():
    provider = _provider()
    provider._begin_invocation_metadata()
    calls: list[str] = []

    async def invalid_after_response(self, model_id: str, prompt: str):
        calls.append(model_id)
        metadata = self._current_invocation_metadata()
        assert metadata is not None
        metadata["planner_response"] = {"requested_model": model_id}
        raise ValueError("503 unavailable: invalid local schema after provider response")

    provider._invoke_system_map = MethodType(invalid_after_response, provider)
    with pytest.raises(ValueError, match="invalid local schema"):
        asyncio.run(
            provider._run_planner_phase(
                "_invoke_system_map",
                "immutable phase prompt",
                global_deadline=time.perf_counter() + 1.0,
            )
        )
    assert calls == ["gemini-primary"]


def test_expired_pre_provider_deadline_is_retryable_instead_of_poisoning_checkpoint():
    store = _CheckpointStore()
    provider = _provider()
    provider._checkpoint_repository = store
    provider._begin_invocation_metadata()
    context, event = _bootstrap_context()
    plan_id = provider._planner_plan_id(context)
    prompt = provider._system_map_prompt(event=event, context=context)
    calls: list[str] = []

    async def system_map(self, model_id: str, prompt: str):
        calls.append(model_id)
        return GeminiSystemMapWire(summary="retry succeeded", roots=_roots())

    provider._invoke_system_map = MethodType(system_map, provider)
    with pytest.raises(RuntimeError, match="deadline reached"):
        asyncio.run(
            provider._run_checkpointed_planner_phase(
                plan_id=plan_id,
                project_id=context.project.id,
                phase_key="SYSTEM_MAP",
                invoke_name="_invoke_system_map",
                prompt=prompt,
                output_model=GeminiSystemMapWire,
                snapshot_before=None,
                global_deadline=time.perf_counter() - 0.01,
                validate=lambda _wire: {},
            )
        )
    assert calls == []
    durable = store.get_planner_checkpoint(plan_id, "SYSTEM_MAP")
    assert durable is not None
    assert durable["status"] == "RETRYABLE"

    provider._begin_invocation_metadata()
    result = asyncio.run(
        provider._run_checkpointed_planner_phase(
            plan_id=plan_id,
            project_id=context.project.id,
            phase_key="SYSTEM_MAP",
            invoke_name="_invoke_system_map",
            prompt=prompt,
            output_model=GeminiSystemMapWire,
            snapshot_before=None,
            global_deadline=time.perf_counter() + 1.0,
            validate=lambda _wire: {},
        )
    )
    assert result.summary == "retry succeeded"
    assert calls == ["gemini-primary"]
    assert store.get_planner_checkpoint(plan_id, "SYSTEM_MAP")["status"] == "COMPLETED"


def test_concurrent_planner_workers_cross_provider_boundary_only_once():
    store = _CheckpointStore()
    context, event = _bootstrap_context()
    start = threading.Barrier(2)
    first_provider_entry = threading.Event()
    competing_finished = threading.Event()
    release_provider = threading.Event()
    provider_entries: list[str] = []
    outcomes: list[str] = []
    outcome_lock = threading.Lock()

    def worker(name: str) -> None:
        provider = _provider()
        provider._checkpoint_repository = store
        _wire_provider(provider, [])
        original = provider._invoke_system_map

        async def gated_system_map(self, model_id: str, prompt: str):
            provider_entries.append(name)
            first_provider_entry.set()
            release_provider.wait(timeout=5)
            return await original(model_id, prompt)

        provider._invoke_system_map = MethodType(gated_system_map, provider)
        start.wait(timeout=5)
        try:
            asyncio.run(provider.generate(event=event, context=context, system_prompt="unused"))
            outcome = "ok"
        except RuntimeError as exc:
            outcome = str(exc)
        with outcome_lock:
            outcomes.append(outcome)
        if not release_provider.is_set():
            competing_finished.set()

    threads = [threading.Thread(target=worker, args=(name,)) for name in ("a", "b")]
    for thread in threads:
        thread.start()
    assert first_provider_entry.wait(timeout=5)
    assert competing_finished.wait(timeout=5)
    assert len(provider_entries) == 1
    release_provider.set()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()

    assert len(provider_entries) == 1
    assert outcomes.count("ok") == 1
    assert sum("STARTED" in outcome for outcome in outcomes) == 1


def test_reconcile_validation_rejects_isolated_leaf():
    architecture = GeminiArchitectureWire(
        version=1,
        components=[
            GeminiComponentWire(id="a", name="A", type="system", responsibility="A"),
            GeminiComponentWire(id="b", name="B", type="system", responsibility="B"),
            GeminiComponentWire(id="c", name="C", type="system", responsibility="C"),
        ],
        relationships=[Relationship(source="a", target="b", relationship_type="CALLS")],
    )
    with pytest.raises(ValueError, match="isolated"):
        GeminiProvider._validate_reconciled_architecture(architecture)


def test_reconcile_relationship_invariants_apply_even_when_there_is_only_one_leaf():
    self_referential = GeminiArchitectureWire(
        version=1,
        components=[GeminiComponentWire(id="only", name="Only", type="service", responsibility="Only leaf")],
        relationships=[Relationship(source="only", target="only", relationship_type="CALLS")],
    )
    with pytest.raises(ValueError, match="self-referential"):
        GeminiProvider._validate_reconciled_architecture(self_referential)

    duplicated = GeminiArchitectureWire(
        version=1,
        components=[
            GeminiComponentWire(id="root", name="Root", type="system", responsibility="Root"),
            GeminiComponentWire(id="leaf", name="Leaf", type="service", responsibility="Leaf", parent_id="root"),
        ],
        relationships=[
            Relationship(source="root", target="leaf", relationship_type="CALLS"),
            Relationship(source="root", target="leaf", relationship_type="CALLS"),
        ],
    )
    with pytest.raises(ValueError, match="duplicate"):
        GeminiProvider._validate_reconciled_architecture(duplicated)


def test_default_planner_budget_uses_bounded_phase_specific_reasoning(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.delenv("GEMINI_ARCHITECTURE_MODEL_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("GEMINI_ARCHITECTURE_PHASE_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("GEMINI_ARCHITECTURE_TOTAL_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("GEMINI_ARCHITECTURE_MAX_OUTPUT_TOKENS", raising=False)
    monkeypatch.delenv("GEMINI_ARCHITECTURE_THINKING_LEVEL", raising=False)
    monkeypatch.delenv("GEMINI_SYSTEM_MAP_MAX_OUTPUT_TOKENS", raising=False)
    monkeypatch.delenv("GEMINI_SCOPE_MAX_OUTPUT_TOKENS", raising=False)
    monkeypatch.delenv("GEMINI_RECONCILE_MAX_OUTPUT_TOKENS", raising=False)
    monkeypatch.delenv("GEMINI_SYSTEM_MAP_THINKING_LEVEL", raising=False)
    monkeypatch.delenv("GEMINI_SCOPE_THINKING_LEVEL", raising=False)
    monkeypatch.delenv("GEMINI_RECONCILE_THINKING_LEVEL", raising=False)
    monkeypatch.delenv("GEMINI_ARCHITECTURE_RETRY_ATTEMPTS", raising=False)
    monkeypatch.delenv("GEMINI_ARCHITECTURE_MAX_CONCURRENCY", raising=False)
    monkeypatch.delenv("GEMINI_ARCHITECTURE_QUEUE_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("GEMINI_INTERACTION_MODEL_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("GEMINI_INTERACTION_TOTAL_TIMEOUT_SECONDS", raising=False)
    provider = GeminiProvider(model_id="gemini-test")
    assert provider.interaction_model_timeout_seconds == 12
    assert provider.interaction_total_timeout_seconds == 36
    assert provider.architecture_model_timeout_seconds == 90
    assert provider.architecture_phase_timeout_seconds == 120
    assert provider.architecture_total_timeout_seconds == 900
    assert provider.architecture_max_output_tokens == 65536
    assert provider.system_map_max_output_tokens == 4096
    assert provider.scope_max_output_tokens == 8192
    assert provider.reconcile_max_output_tokens == 16384
    assert provider.architecture_thinking_level == "medium"
    assert provider.system_map_thinking_level == "low"
    assert provider.scope_thinking_level == "low"
    assert provider.reconcile_thinking_level == "medium"
    assert provider.architecture_retry_attempts == 5
    assert provider.architecture_max_concurrency == 1
    assert provider.architecture_queue_timeout_seconds == 120


def test_system_map_losslessly_accepts_provider_summary_as_responsibility():
    root = GeminiPlannerRootWire.model_validate(
        {"id": "browser", "name": "Browser", "summary": "Own the browser workspace."}
    )
    assert root.type == "Architecture boundary"
    assert root.responsibility == "Own the browser workspace."


def test_provider_description_alias_is_lossless_for_roots_and_scope_components():
    root = GeminiPlannerRootWire.model_validate(
        {"id": "api", "name": "API", "description": "Own authenticated API ingress."}
    )
    component = GeminiComponentWire.model_validate(
        {
            "id": "auth",
            "name": "Auth",
            "type": "service",
            "description": "Validate Firebase identity and project permissions.",
            "parent_id": "api",
        }
    )
    assert root.responsibility == "Own authenticated API ingress."
    assert component.responsibility == "Validate Firebase identity and project permissions."


def test_scope_payload_can_fill_only_the_server_owned_missing_scope_id():
    payload, normalizations = GeminiProvider._normalize_planner_payload(
        '{"status":"READY","components":[]}',
        expected_scope_id="frontend",
    )
    assert payload["scope_id"] == "frontend"
    assert normalizations == [
        {
            "field": "scope_id",
            "operation": "filled_from_server_phase_target",
            "value": "frontend",
        }
    ]

    conflicting, _ = GeminiProvider._normalize_planner_payload(
        '{"status":"READY","scope_id":"backend","components":[]}',
        expected_scope_id="frontend",
    )
    assert conflicting["scope_id"] == "backend"


def test_scope_payload_records_neutral_missing_type_normalization():
    payload, normalizations = GeminiProvider._normalize_planner_payload(
        '{"status":"READY","scope_id":"frontend","components":['
        '{"id":"canvas","name":"Canvas","responsibility":"Render graph",'
        '"kind":"UI","parent_id":"frontend"}]}',
        expected_scope_id="frontend",
    )
    assert payload["components"][0]["type"] == "Architecture component"
    assert normalizations == [
        {
            "field": "components[0].type",
            "operation": "filled_server_display_default",
            "value": "Architecture component",
        }
    ]
    wire = GeminiScopeDeltaWire.model_validate(payload)
    assert wire.components[0].kind.value == "UI"


def test_reconcile_payload_normalizes_observed_provider_aliases_losslessly():
    payload, normalizations = GeminiProvider._normalize_planner_payload(
        json.dumps(
            {
                "summary": "Ready",
                "relationships": [
                    {
                        "source": "a",
                        "target": "b",
                        "type": "CALLS",
                        "description": "A calls B.",
                    }
                ],
                "tasks": [{"title": "Build", "related_component": "a"}],
                "decisions": [
                    {
                        "title": "Human Review",
                        "decision": "Require explicit approval.",
                        "rationale": "Preserve human authority.",
                    }
                ],
                "risks": [
                    {
                        "risk": "Usage may be absent.",
                        "mitigation": "Persist null rather than zero.",
                    }
                ],
            }
        ),
        reconcile=True,
    )
    wire = GeminiReconcileWire.model_validate(payload)
    assert wire.relationships[0].relationship_type == "CALLS"
    assert wire.decisions == [
        "Title: Human Review\nDecision: Require explicit approval.\nRationale: Preserve human authority."
    ]
    assert wire.risks == [
        "Risk: Usage may be absent.\nMitigation: Persist null rather than zero."
    ]
    assert [item["operation"] for item in normalizations] == [
        "renamed_provider_alias",
        "canonicalized_structured_text",
        "canonicalized_structured_text",
    ]


def test_reconcile_payload_rejects_conflicting_relationship_type_aliases():
    with pytest.raises(ValueError, match="conflicting relationships"):
        GeminiProvider._normalize_planner_payload(
            '{"relationships":[{"source":"a","target":"b","type":"CALLS",'
            '"relationship_type":"READS"}]}',
            reconcile=True,
        )


def test_reconcile_payload_rejects_unknown_structured_decision_shape():
    with pytest.raises(ValueError, match="unsupported structured decisions"):
        GeminiProvider._normalize_planner_payload(
            '{"decisions":[{"decision":"Keep it"}]}',
            reconcile=True,
        )


def test_scope_prompt_enforces_node_budget_and_canonical_kind_enum():
    context, event = _bootstrap_context()
    snapshot = InitialArchitecturePlannerSnapshot(
        roots=("frontend", "backend"),
        components=(
            GeminiComponentWire(id="frontend", name="Frontend", type="system", responsibility="UI"),
            GeminiComponentWire(id="backend", name="Backend", type="system", responsibility="API"),
        ),
    )
    prompt = GeminiProvider._scope_prompt(
        event=event,
        context=context,
        snapshot=snapshot,
        scope_id="frontend",
    )
    assert "at most 6 new descendants" in prompt
    assert "Never use C4 labels such as CONTAINER" in prompt


def test_conflicting_responsibility_aliases_fail_closed():
    with pytest.raises(ValidationError, match="conflicting summary/description"):
        GeminiPlannerRootWire.model_validate(
            {
                "id": "api",
                "name": "API",
                "summary": "One meaning.",
                "description": "A different meaning.",
            }
        )


def test_system_map_provider_schema_requires_roots_key():
    schema = GeminiSystemMapWire.model_json_schema()
    assert "roots" in schema.get("required", [])
    needs_fact = GeminiSystemMapWire(
        status="NEEDS_FACT",
        missing_facts=["Which deployment boundary is authoritative?"],
        roots=[],
    )
    assert needs_fact.roots == []


def test_reconcile_wire_allows_more_than_twelve_real_relationships():
    relationships = [
        Relationship(source=f"s{index}", target=f"t{index}", relationship_type="CALLS")
        for index in range(13)
    ]
    wire = GeminiReconcileWire(
        relationships=relationships,
        tasks=[TaskProposal(title="Validate relationships", related_component="s0")],
    )
    assert len(wire.relationships) == 13


def test_system_map_accepts_simple_roots_but_rejects_more_than_six():
    one = GeminiSystemMapWire(roots=[GeminiPlannerRootWire(id="only", name="Only", type="system", responsibility="Whole simple product")])
    two = GeminiSystemMapWire(roots=[
        GeminiPlannerRootWire(id="a", name="A", type="system", responsibility="A"),
        GeminiPlannerRootWire(id="b", name="B", type="system", responsibility="B"),
    ])
    assert len(one.roots) == 1
    assert len(two.roots) == 2
    with pytest.raises(ValidationError):
        GeminiSystemMapWire(roots=[
            GeminiPlannerRootWire(id=str(index), name=str(index), type="system", responsibility=str(index))
            for index in range(7)
        ])


def test_scope_delta_rejects_existing_id_redefinition_and_cross_scope_parentage():
    snapshot = InitialArchitecturePlannerSnapshot(
        roots=("a", "b"),
        components=(
            GeminiComponentWire(id="a", name="A", type="system", responsibility="A"),
            GeminiComponentWire(id="b", name="B", type="system", responsibility="B"),
        ),
    )
    with pytest.raises(ValueError, match="redefine existing"):
        GeminiProvider._apply_scope_delta(
            snapshot,
            GeminiScopeDeltaWire(
                scope_id="a",
                components=[GeminiComponentWire(id="b", name="Other B", type="service", responsibility="bad", parent_id="a")],
            ),
            expected_scope_id="a",
        )
    with pytest.raises(ValueError, match="only extend"):
        GeminiProvider._apply_scope_delta(
            snapshot,
            GeminiScopeDeltaWire(
                scope_id="a",
                components=[GeminiComponentWire(id="x", name="X", type="service", responsibility="bad", parent_id="b")],
            ),
            expected_scope_id="a",
        )


    with pytest.raises(ValueError, match="unknown parent_id"):
        GeminiProvider._apply_scope_delta(
            snapshot,
            GeminiScopeDeltaWire(
                scope_id="a",
                components=[GeminiComponentWire(id="orphan", name="Orphan", type="service", responsibility="bad", parent_id="missing")],
            ),
            expected_scope_id="a",
        )


def test_scope_delta_fallback_reuses_identical_pre_phase_prompt():
    provider = _provider()
    prompts: list[tuple[str, str]] = []

    async def scope_delta(self, model_id: str, prompt: str):
        prompts.append((model_id, prompt))
        if model_id == "gemini-primary":
            raise RuntimeError("503 UNAVAILABLE: high demand")
        return GeminiScopeDeltaWire(scope_id="a", components=[])

    provider._invoke_scope_delta = MethodType(scope_delta, provider)
    wire = asyncio.run(
        provider._run_planner_phase(
            "_invoke_scope_delta",
            "same immutable phase prompt",
            global_deadline=time.perf_counter() + 5,
        )
    )
    assert wire.scope_id == "a"
    assert [model for model, _ in prompts] == ["gemini-primary", "gemini-rescue"]
    assert prompts[0][1] == prompts[1][1] == "same immutable phase prompt"


def test_needs_fact_is_typed_and_carries_no_partial_architecture():
    provider = _provider()

    async def system_map(self, model_id: str, prompt: str):
        return GeminiSystemMapWire(
            status="NEEDS_FACT",
            missing_facts=["Which external data authority is required?"],
            roots=[],
        )

    provider._invoke_system_map = MethodType(system_map, provider)
    context, event = _bootstrap_context()
    with pytest.raises(ArchitectureNeedsFactError, match="external data authority"):
        asyncio.run(provider.generate(event=event, context=context, system_prompt="unused"))


def _repo(dsn: str) -> tuple[PostgresProjectRepository, Project]:
    repo = PostgresProjectRepository(dsn)
    project = Project(name="planner", goal="Build an outside-in architecture with multiple responsibilities.")
    repo.save_project(project)
    repo.save_architecture(project.id, Architecture())
    return repo, project


def _repo_event(project: Project) -> ProjectEvent:
    return ProjectEvent(
        project_id=project.id,
        type=ProjectEventType.USER_MESSAGE,
        payload={"intent": "INITIAL_ARCHITECTURE", "message": project.goal},
    )


@requires_database
def test_mid_scope_failure_keeps_repository_snapshot_unchanged(dsn):
    repo, project = _repo(dsn)
    before = repo.snapshot(project.id)
    provider = _provider()
    calls: list[str] = []
    _wire_provider(provider, calls, fail_scope="domain")

    result = asyncio.run(AgentOrchestrator(repo, provider).observe_event(_repo_event(project)))

    assert result.result == "ERROR"
    assert "scope failed permanently" in result.error
    assert repo.snapshot(project.id) == before
    assert repo.get_architecture(project.id).version == 0


@requires_database
def test_reconcile_failure_keeps_repository_snapshot_unchanged(dsn):
    repo, project = _repo(dsn)
    before = repo.snapshot(project.id)
    provider = _provider()
    calls: list[str] = []
    _wire_provider(provider, calls, fail_reconcile=True)

    result = asyncio.run(AgentOrchestrator(repo, provider).observe_event(_repo_event(project)))

    assert result.result == "ERROR"
    assert "reconcile failed permanently" in result.error
    assert repo.snapshot(project.id) == before
    assert repo.get_architecture(project.id).version == 0


@requires_database
def test_ready_commits_one_serializable_hierarchical_architecture(dsn):
    repo, project = _repo(dsn)
    provider = _provider()
    calls: list[str] = []
    _wire_provider(provider, calls)

    result = asyncio.run(AgentOrchestrator(repo, provider).observe_event(_repo_event(project)))
    accepted = repo.get_architecture(project.id)
    round_trip = Architecture.model_validate_json(accepted.model_dump_json())

    assert result.result == "SUCCESS"
    assert accepted.version == 1
    assert round_trip == accepted
    assert accepted.child_component_ids_for("domain") == ["domain_capability"]
    assert accepted.parent_component_id_for("domain") is None
    assert accepted.parent_component_id_for("domain_capability") == "domain"
    assert len(repo.list_tasks(project.id)) == 1


def test_hierarchy_helpers_are_deterministic_for_three_levels_and_unknown_ids():
    architecture = Architecture(
        version=1,
        components=[
            Component(
                id="root",
                name="Root",
                type="system",
                responsibility="Root",
                children=[
                    Component(
                        id="child",
                        name="Child",
                        type="service",
                        responsibility="Child",
                        children=[Component(id="leaf", name="Leaf", type="tool", responsibility="Leaf")],
                    )
                ],
            )
        ],
    )
    assert architecture.root_component_id_for("leaf") == "root"
    assert architecture.parent_component_id_for("root") is None
    assert architecture.parent_component_id_for("child") == "root"
    assert architecture.parent_component_id_for("leaf") == "child"
    assert architecture.child_component_ids_for("root") == ["child"]
    assert architecture.child_component_ids_for("leaf") == []
    assert architecture.parent_component_id_for("missing") is None
    assert architecture.child_component_ids_for("missing") == []
