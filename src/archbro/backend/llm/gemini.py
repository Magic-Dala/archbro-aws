from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from uuid import uuid4
from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Literal

from dotenv import load_dotenv
from pydantic import BaseModel, Field, model_validator

from archbro.backend.core.contracts import (
    AgentAction,
    AgentActionType,
    AgentDecision,
    Architecture,
    ArchitectureNodeKind,
    ArchitectureChangeProposal,
    ArchitectureOption,
    Component,
    ProjectContext,
    ProjectEvent,
    ProjectEventType,
    Relationship,
    TaskProposal,
)
from archbro.backend.core.evaluation import DriftEvaluation
from archbro.backend.core.architecture_validation import validate_architecture_relationship_connectivity
from archbro.backend.core.repository import ProjectRepositoryPort
from archbro.backend.llm.google_genai_client import GoogleGenAIClientFactory
from archbro.backend.llm.provider import GoalConversationMessage, GoalDraft, ModelProvider

load_dotenv()
logger = logging.getLogger("archbro")


DEFAULT_GEMINI_CHAIN = (
    "gemini-3.8-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
)

DEFAULT_GEMINI_GOAL_CHAIN = (
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-3.5-flash",
    "gemini-3.6-flash",
    "gemini-3.8-flash",
)

DEFAULT_GEMINI_ROUTINE_CHAIN = (
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-3.5-flash",
    "gemini-3.6-flash",
    "gemini-3.8-flash",
)


class _PlannerSafeRetryError(RuntimeError):
    """A planner phase failed before any ambiguous provider effect occurred."""


@dataclass(frozen=True)
class GeminiUsageTelemetry:
    model_id: str
    transport: str
    latency_ms: int
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    cache_read_input_tokens: int | None
    cache_write_input_tokens: int | None


async def _close_google_client(client) -> None:
    """Best-effort cleanup must never replace the provider's real outcome."""

    try:
        await client.aio.aclose()
    except Exception:
        # Close failures can otherwise mask the model/provider exception that
        # drives retry and fail-closed classification. Do not log exception text:
        # an SDK transport error can contain endpoint or credential metadata.
        logger.warning("Google Gen AI client cleanup failed")


class _ManagedStrandsAgent:
    """Keep one preconfigured Google client alive for one Strands invocation."""

    def __init__(self, *, agent, client) -> None:
        self._agent = agent
        self._client = client

    async def invoke_async(self, *args, **kwargs):
        try:
            return await self._agent.invoke_async(*args, **kwargs)
        finally:
            await _close_google_client(self._client)


def _compact_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _normalize_responsibility_alias(value):
    """Losslessly accept provider wording aliases for one responsibility field."""

    if not isinstance(value, Mapping) or "responsibility" in value:
        return value
    aliases = [
        (key, candidate.strip())
        for key in ("summary", "description")
        if isinstance((candidate := value.get(key)), str) and candidate.strip()
    ]
    if not aliases:
        return value
    distinct = {candidate for _, candidate in aliases}
    if len(distinct) != 1:
        raise ValueError("conflicting summary/description aliases for responsibility")
    normalized = dict(value)
    normalized["responsibility"] = aliases[0][1]
    for key, _ in aliases:
        normalized.pop(key, None)
    return normalized


def _bootstrap_project_facts(context: ProjectContext) -> dict[str, object]:
    """Return only user-authored facts needed by the initial planner."""

    project = context.project
    facts: dict[str, object] = {"name": project.name, "goal": project.goal}
    if project.description.strip():
        facts["description"] = project.description
    return facts


def _compact_context_facts(
    context: ProjectContext,
    *,
    agent_context_manifest: dict[str, object] | None = None,
) -> dict[str, object]:
    """Project durable state into bounded semantic facts for the model."""

    project = context.project
    project_facts: dict[str, object] = {
        "name": project.name,
        "goal": project.goal,
        "status": project.status.value,
        "architecture_version": project.architecture_version,
    }
    if project.description.strip():
        project_facts["description"] = project.description
    if (
        isinstance(agent_context_manifest, dict)
        and agent_context_manifest.get("schema") == "archbro.agent_context_manifest.v1"
    ):
        # The server rebuilt and hash-verified this exact manifest immediately
        # before execution. The manifest already contains bounded project facts;
        # do not append raw Goal/description, full Architecture, or all tasks or
        # the visual context boundary would be cosmetic rather than real.
        return {"agent_context_manifest": agent_context_manifest}
    return {
        "project": project_facts,
        "architecture": context.architecture.model_dump(mode="json", exclude_none=True),
        "tasks": [
            task.model_dump(
                mode="json",
                exclude={"created_at", "updated_at"},
                exclude_none=True,
            )
            for task in context.tasks
        ],
        "pending_proposals": [
            proposal.model_dump(
                mode="json",
                exclude={"created_at", "updated_at"},
                exclude_none=True,
            )
            for proposal in context.pending_proposals
        ],
        "recent_notes": [note[:1000] for note in context.recent_notes[-8:]],
    }


def _compact_event_facts(event: ProjectEvent) -> dict[str, object]:
    payload = dict(event.payload)
    manifest = payload.pop("agent_context_manifest", None)
    if isinstance(manifest, dict):
        payload["agent_context_manifest_hash"] = manifest.get("manifest_hash")
    return {
        "type": event.type.value,
        "source": event.source.value,
        "payload": payload,
    }


class GeminiArchitectureProposalWire(BaseModel):
    """Provider-only proposal payload. Server-owned identity/state fields are added deterministically."""

    reason: str
    evidence: list[str] = Field(min_length=1, max_length=5)
    observed_change: str
    affected_components: list[str] = Field(default_factory=list, max_length=7)
    proposed_changes: list[dict[str, object]] = Field(default_factory=list, max_length=7)
    impact: str
    recommended_option: ArchitectureOption


class GeminiComponentWire(BaseModel):
    """Flat provider-only architecture node.

    The product domain keeps recursive Component.children, but recursive Pydantic
    schemas are not safe to hand directly to Strands/Google structured output.
    parent_id encodes the hierarchy without recursion and is rebuilt
    deterministically after validation.
    """

    id: str
    name: str
    type: str
    responsibility: str
    status: str = "PLANNED"
    kind: ArchitectureNodeKind = ArchitectureNodeKind.SYSTEM
    parent_id: str | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_responsibility_alias(cls, value):
        return _normalize_responsibility_alias(value)


class ArchitectureNeedsFactError(RuntimeError):
    """Initial architecture cannot be truthful without concrete project facts."""

    def __init__(self, missing_facts: list[str]) -> None:
        facts = [fact.strip() for fact in missing_facts if fact.strip()]
        if not facts:
            facts = ["unspecified architecture fact"]
        self.missing_facts = facts
        super().__init__("Architecture needs fact: " + "; ".join(facts))


class GeminiPlannerRootWire(BaseModel):
    id: str
    name: str
    type: str = "Architecture boundary"
    responsibility: str
    status: str = "PLANNED"

    @model_validator(mode="before")
    @classmethod
    def normalize_responsibility_alias(cls, value):
        return _normalize_responsibility_alias(value)

    def as_component(self) -> GeminiComponentWire:
        return GeminiComponentWire(
            id=self.id,
            name=self.name,
            type=self.type,
            responsibility=self.responsibility,
            status=self.status,
            kind=ArchitectureNodeKind.SYSTEM,
            parent_id=None,
        )


class GeminiSystemMapWire(BaseModel):
    status: Literal["READY", "NEEDS_FACT"] = "READY"
    missing_facts: list[str] = Field(default_factory=list, max_length=5)
    summary: str = Field(default="", max_length=600)
    # Intentionally required in the provider JSON schema. With a default_factory
    # Google may legally omit roots entirely, producing READY + prose that only
    # fails after the paid call. NEEDS_FACT must explicitly return roots=[].
    roots: list[GeminiPlannerRootWire] = Field(max_length=6)

    @model_validator(mode="after")
    def validate_status(self) -> "GeminiSystemMapWire":
        if self.status == "NEEDS_FACT":
            if not self.missing_facts:
                raise ValueError("NEEDS_FACT requires missing_facts")
            if self.roots:
                raise ValueError("NEEDS_FACT cannot carry topology")
        elif not self.roots:
            raise ValueError("SYSTEM_MAP requires at least one root")
        return self


class GeminiScopeDeltaWire(BaseModel):
    status: Literal["READY", "NEEDS_FACT"] = "READY"
    missing_facts: list[str] = Field(default_factory=list, max_length=5)
    scope_id: str
    components: list[GeminiComponentWire] = Field(default_factory=list, max_length=6)

    @model_validator(mode="after")
    def validate_status(self) -> "GeminiScopeDeltaWire":
        if not self.scope_id.strip():
            raise ValueError("scope_id must not be empty")
        if self.status == "NEEDS_FACT":
            if not self.missing_facts:
                raise ValueError("NEEDS_FACT requires missing_facts")
            if self.components:
                raise ValueError("NEEDS_FACT cannot carry a scoped delta")
        elif any(component.parent_id is None for component in self.components):
            raise ValueError("scope deltas may only add descendants")
        return self


class GeminiReconcileWire(BaseModel):
    status: Literal["READY", "NEEDS_FACT"] = "READY"
    missing_facts: list[str] = Field(default_factory=list, max_length=5)
    summary: str = ""
    relationships: list[Relationship] = Field(default_factory=list, max_length=80)
    tasks: list[TaskProposal] = Field(default_factory=list, max_length=6)
    decisions: list[str] = Field(default_factory=list, max_length=3)
    assumptions: list[str] = Field(default_factory=list, max_length=3)
    risks: list[str] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def validate_status(self) -> "GeminiReconcileWire":
        if self.status == "NEEDS_FACT":
            if not self.missing_facts:
                raise ValueError("NEEDS_FACT requires missing_facts")
            if self.relationships or self.tasks or self.decisions or self.assumptions or self.risks:
                raise ValueError("NEEDS_FACT cannot carry reconciliation output")
        elif not self.tasks:
            raise ValueError("RECONCILE requires at least one implementation task")
        return self


@dataclass(frozen=True)
class InitialArchitecturePlannerSnapshot:
    roots: tuple[str, ...]
    components: tuple[GeminiComponentWire, ...]


class GeminiArchitectureWire(BaseModel):
    """Non-recursive provider wire for a hierarchical Architecture v1."""

    version: int = 1
    summary: str = ""
    components: list[GeminiComponentWire] = Field(min_length=1, max_length=40)
    relationships: list[Relationship] = Field(default_factory=list, max_length=80)
    decisions: list[str] = Field(default_factory=list, max_length=3)
    assumptions: list[str] = Field(default_factory=list, max_length=3)
    risks: list[str] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def validate_flat_hierarchy(self) -> "GeminiArchitectureWire":
        if self.version != 1:
            raise ValueError("bootstrap architecture.version must be 1")

        by_id = {component.id: component for component in self.components}
        if len(by_id) != len(self.components):
            raise ValueError("bootstrap architecture node ids must be unique")

        top_level = [component for component in self.components if component.parent_id is None]
        if not 1 <= len(top_level) <= 8:
            raise ValueError("bootstrap requires 1-8 top-level components")

        child_counts: dict[str, int] = {}
        depths: dict[str, int] = {}

        for component in self.components:
            if component.parent_id is not None:
                if component.parent_id == component.id:
                    raise ValueError("architecture node cannot parent itself")
                if component.parent_id not in by_id:
                    raise ValueError(f"unknown parent_id for architecture node: {component.parent_id}")
                child_counts[component.parent_id] = child_counts.get(component.parent_id, 0) + 1

            depth = 1
            cursor = component
            seen = {component.id}
            while cursor.parent_id is not None:
                if cursor.parent_id in seen:
                    raise ValueError("architecture hierarchy cannot contain cycles")
                seen.add(cursor.parent_id)
                cursor = by_id[cursor.parent_id]
                depth += 1
                if depth > 3:
                    raise ValueError("architecture depth is capped at 3 levels")
            depths[component.id] = depth

        for parent_id, count in child_counts.items():
            parent_depth = depths[parent_id]
            if parent_depth == 1 and count > 7:
                raise ValueError("top-level architecture nodes allow at most 7 children")
            if parent_depth == 2 and count > 6:
                raise ValueError("level-2 architecture nodes allow at most 6 children")
            if parent_depth >= 3 and count:
                raise ValueError("level-3 architecture nodes cannot have children")

        component_ids = set(by_id)
        if any(rel.source not in component_ids or rel.target not in component_ids for rel in self.relationships):
            raise ValueError("bootstrap relationships must reference component ids")
        return self

    def component_ids(self) -> set[str]:
        return {component.id for component in self.components}

    def to_domain(self) -> Architecture:
        nodes = {
            wire.id: Component(
                id=wire.id,
                name=wire.name,
                type=wire.type,
                responsibility=wire.responsibility,
                status=wire.status,
                kind=wire.kind,
                children=[],
            )
            for wire in self.components
        }
        roots: list[Component] = []
        for wire in self.components:
            node = nodes[wire.id]
            if wire.parent_id is None:
                roots.append(node)
            else:
                nodes[wire.parent_id].children.append(node)
        return Architecture(
            version=self.version,
            summary=self.summary,
            components=roots,
            relationships=self.relationships,
            decisions=self.decisions,
            assumptions=self.assumptions,
            risks=self.risks,
        )


class GeminiDecisionWire(BaseModel):
    """Provider-only structured output. It is converted into the shared AgentDecision contract."""

    summary: str
    evaluation: DriftEvaluation
    architecture_review_required: bool = False
    architecture_proposal: GeminiArchitectureProposalWire | None = None
    actions: list[AgentAction] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_proposal_shape(self) -> "GeminiDecisionWire":
        if self.architecture_review_required != (self.architecture_proposal is not None):
            raise ValueError("architecture_review_required must match architecture_proposal presence")
        return self


class GeminiBootstrapWire(BaseModel):
    """Small provider-only schema for initial V0 architecture generation."""

    summary: str
    architecture: GeminiArchitectureWire
    tasks: list[TaskProposal] = Field(min_length=1, max_length=6)

    @model_validator(mode="after")
    def validate_small_bootstrap(self) -> "GeminiBootstrapWire":
        component_ids = self.architecture.component_ids()
        if any(task.related_component and task.related_component not in component_ids for task in self.tasks):
            raise ValueError("bootstrap task.related_component must reference a component id")
        return self


class GeminiProvider(ModelProvider):
    name = "gemini"

    def _current_invocation_metadata(self) -> dict[str, object] | None:
        context = getattr(self, "_invocation_metadata_context", None)
        return None if context is None else context.get()

    def _planner_checkpoint_control(self) -> dict[str, object] | None:
        metadata = self._current_invocation_metadata()
        control = None if metadata is None else metadata.get("planner_checkpoint_control")
        return control if isinstance(control, dict) else None

    def _persist_active_planner_checkpoint(self, data: dict[str, object]) -> dict[str, object]:
        repository = getattr(self, "_checkpoint_repository", None)
        control = self._planner_checkpoint_control()
        if repository is None or control is None:
            return data
        persisted = repository.put_planner_checkpoint(
            project_id=str(control["project_id"]),
            plan_id=str(control["plan_id"]),
            phase_key=str(control["phase_key"]),
            data=data,
            expected_revision=int(control["revision"]),
            expected_owner_generation=int(control["owner_generation"]),
        )
        control["revision"] = int(persisted["revision"])
        control["owner_generation"] = int(persisted["owner_generation"])
        return persisted

    def _transition_active_planner_checkpoint(
        self,
        delivery_stage: str,
        *,
        provider: dict[str, object] | None = None,
    ) -> dict[str, object] | None:
        repository = getattr(self, "_checkpoint_repository", None)
        control = self._planner_checkpoint_control()
        if repository is None or control is None:
            return None
        current = repository.get_planner_checkpoint(str(control["plan_id"]), str(control["phase_key"]))
        if current is None:
            raise RuntimeError("planner checkpoint disappeared while phase was running")
        next_checkpoint = dict(current)
        next_checkpoint["delivery_stage"] = delivery_stage
        if provider is not None:
            next_checkpoint["provider"] = provider
        return self._persist_active_planner_checkpoint(next_checkpoint)

    @staticmethod
    def _scope_id_from_prompt(prompt: str) -> str:
        marker = "EXPAND_SCOPE phase for root scope_id="
        terminator = ". Return only NEW descendants"
        if marker not in prompt:
            raise ValueError("EXPAND_SCOPE prompt is missing its server-owned scope target")
        remainder = prompt.split(marker, 1)[1]
        if terminator not in remainder:
            raise ValueError("EXPAND_SCOPE prompt has an invalid server-owned scope target")
        scope_id = remainder.split(terminator, 1)[0].strip()
        if not scope_id:
            raise ValueError("EXPAND_SCOPE prompt has an empty server-owned scope target")
        return scope_id

    def _parse_recorded_planner_output(self, invoke_name: str, prompt: str, output_model, raw_output: str):
        normalized, _format_normalization = self._strip_json_fence(raw_output)
        payload, _normalizations = self._normalize_planner_payload(
            normalized,
            expected_scope_id=(
                self._scope_id_from_prompt(prompt)
                if invoke_name == "_invoke_scope_delta"
                else None
            ),
            reconcile=invoke_name == "_invoke_reconcile",
        )
        return output_model.model_validate(payload)

    def _begin_invocation_metadata(self) -> None:
        context = getattr(self, "_invocation_metadata_context", None)
        if context is None:
            context = ContextVar(f"gemini_invocation_metadata_{id(self)}", default=None)
            self._invocation_metadata_context = context
        context.set(
            {
                "model_id": self.model_id,
                "usage": None,
                "planner_response": None,
                "planner_dispatch": None,
                "planner_request_started": False,
                "planner_phases": [],
            }
        )

    @property
    def last_model_id(self) -> str:
        metadata = self._current_invocation_metadata()
        if metadata is not None:
            model_id = metadata.get("model_id")
            if isinstance(model_id, str):
                return model_id
        return getattr(self, "_last_model_id", self.model_id)

    @last_model_id.setter
    def last_model_id(self, model_id: str) -> None:
        self._last_model_id = model_id
        metadata = self._current_invocation_metadata()
        if metadata is not None:
            metadata["model_id"] = model_id

    @property
    def last_usage(self):
        metadata = self._current_invocation_metadata()
        if metadata is not None:
            return metadata.get("usage")
        return getattr(self, "_last_usage", None)

    @last_usage.setter
    def last_usage(self, usage) -> None:
        self._last_usage = usage
        metadata = self._current_invocation_metadata()
        if metadata is not None:
            metadata["usage"] = usage

    def __init__(
        self,
        model_id: str = "gemini-3.8-flash",
        *,
        checkpoint_repository: ProjectRepositoryPort | None = None,
    ) -> None:
        self.model_id = model_id
        self._checkpoint_repository = checkpoint_repository
        self.last_model_id = model_id
        self.last_usage: GeminiUsageTelemetry | dict[str, object] | None = None
        self.fallback_model_ids = self._load_fallback_models(model_id)
        self.goal_model_id = os.getenv("GEMINI_GOAL_MODEL", "gemini-3.5-flash-lite").strip() or "gemini-3.5-flash-lite"
        self.goal_fallback_model_ids = self._load_goal_fallback_models(self.goal_model_id)
        self.goal_model_timeout_seconds = float(os.getenv("GEMINI_GOAL_MODEL_TIMEOUT_SECONDS", "8"))
        if self.goal_model_timeout_seconds <= 0:
            raise ValueError("GEMINI_GOAL_MODEL_TIMEOUT_SECONDS must be greater than zero")
        self.routine_model_id = os.getenv("GEMINI_ROUTINE_MODEL", "gemini-3.5-flash-lite").strip() or "gemini-3.5-flash-lite"
        self.routine_fallback_model_ids = self._load_routine_fallback_models(self.routine_model_id)
        self.routine_model_timeout_seconds = float(os.getenv("GEMINI_ROUTINE_MODEL_TIMEOUT_SECONDS", "8"))
        if self.routine_model_timeout_seconds <= 0:
            raise ValueError("GEMINI_ROUTINE_MODEL_TIMEOUT_SECONDS must be greater than zero")
        self.interaction_model_timeout_seconds = float(os.getenv("GEMINI_INTERACTION_MODEL_TIMEOUT_SECONDS", "12"))
        self.interaction_total_timeout_seconds = float(os.getenv("GEMINI_INTERACTION_TOTAL_TIMEOUT_SECONDS", "36"))
        self.architecture_model_timeout_seconds = float(os.getenv("GEMINI_ARCHITECTURE_MODEL_TIMEOUT_SECONDS", "90"))
        self.architecture_phase_timeout_seconds = float(os.getenv("GEMINI_ARCHITECTURE_PHASE_TIMEOUT_SECONDS", "120"))
        self.architecture_total_timeout_seconds = float(os.getenv("GEMINI_ARCHITECTURE_TOTAL_TIMEOUT_SECONDS", "900"))
        self.architecture_max_output_tokens = int(os.getenv("GEMINI_ARCHITECTURE_MAX_OUTPUT_TOKENS", "65536"))
        self.architecture_thinking_level = os.getenv("GEMINI_ARCHITECTURE_THINKING_LEVEL", "high").strip().lower()
        self.system_map_model_id = os.getenv("GEMINI_SYSTEM_MAP_MODEL", "").strip() or None
        if (
            self.interaction_model_timeout_seconds <= 0
            or self.interaction_total_timeout_seconds <= 0
            or self.architecture_model_timeout_seconds <= 0
            or self.architecture_phase_timeout_seconds <= 0
            or self.architecture_total_timeout_seconds <= 0
            or self.architecture_max_output_tokens <= 0
        ):
            raise ValueError("Gemini architecture timeouts/output budget must be greater than zero")
        if self.architecture_thinking_level not in {"minimal", "low", "medium", "high"}:
            raise ValueError("GEMINI_ARCHITECTURE_THINKING_LEVEL must be minimal, low, medium, or high")
        bootstrap_fallbacks = os.getenv(
            "GEMINI_BOOTSTRAP_FALLBACK_MODELS",
            "gemini-3.5-flash-lite,gemini-3.6-flash,gemini-3.5-flash",
        )
        self.bootstrap_fallback_model_ids = tuple(
            candidate
            for candidate in (item.strip() for item in bootstrap_fallbacks.split(","))
            if candidate and candidate != self.model_id
        )
        self._google_client_factory = GoogleGenAIClientFactory.from_env()
        # Compatibility attributes remain available for existing diagnostics and
        # object-level unit fixtures. Vertex mode deliberately carries no API key.
        self._base_url = self._google_client_factory.base_url
        self._api_key = self._google_client_factory.api_key
        # Strands Agent instances are invocation-scoped. Reusing one Agent across HTTP
        # requests raises ConcurrencyException because concurrent invocations are unsupported.

    def _effective_architecture_http_timeout_ms(self) -> int:
        minimum_ms = max(1, round(self.architecture_model_timeout_seconds * 1000))
        configured_text = os.getenv("GEMINI_ARCHITECTURE_HTTP_TIMEOUT_MS", "").strip()
        if not configured_text:
            return minimum_ms
        try:
            configured_ms = int(configured_text)
        except ValueError as exc:
            raise ValueError(
                "GEMINI_ARCHITECTURE_HTTP_TIMEOUT_MS must be an integer"
            ) from exc
        if configured_ms <= 0:
            raise ValueError("GEMINI_ARCHITECTURE_HTTP_TIMEOUT_MS must be greater than zero")
        if configured_ms < minimum_ms:
            logger.warning(
                "GEMINI_ARCHITECTURE_HTTP_TIMEOUT_MS=%s is shorter than the model budget; using %s",
                configured_ms,
                minimum_ms,
            )
        return max(configured_ms, minimum_ms)

    @staticmethod
    def _load_fallback_models(primary_model_id: str) -> tuple[str, ...]:
        configured = os.getenv("GEMINI_FALLBACK_MODELS", "").strip()
        legacy = os.getenv("GEMINI_FALLBACK_MODEL", "").strip()

        if configured:
            requested = [item.strip() for item in configured.split(",") if item.strip()]
        elif legacy:
            requested = [legacy]
        else:
            requested = list(DEFAULT_GEMINI_CHAIN)

        deduped: list[str] = []
        for candidate in requested:
            if candidate != primary_model_id and candidate not in deduped:
                deduped.append(candidate)
        return tuple(deduped)

    @staticmethod
    def _load_goal_fallback_models(primary_model_id: str) -> tuple[str, ...]:
        configured = os.getenv("GEMINI_GOAL_FALLBACK_MODELS", "").strip()
        requested = (
            [item.strip() for item in configured.split(",") if item.strip()]
            if configured
            else list(DEFAULT_GEMINI_GOAL_CHAIN)
        )
        deduped: list[str] = []
        for candidate in requested:
            if candidate != primary_model_id and candidate not in deduped:
                deduped.append(candidate)
        return tuple(deduped)

    @staticmethod
    def _load_routine_fallback_models(primary_model_id: str) -> tuple[str, ...]:
        configured = os.getenv("GEMINI_ROUTINE_FALLBACK_MODELS", "").strip()
        requested = (
            [item.strip() for item in configured.split(",") if item.strip()]
            if configured
            else list(DEFAULT_GEMINI_ROUTINE_CHAIN)
        )
        deduped: list[str] = []
        for candidate in requested:
            if candidate != primary_model_id and candidate not in deduped:
                deduped.append(candidate)
        return tuple(deduped)

    @property
    def model_chain(self) -> tuple[str, ...]:
        return (self.model_id, *self.fallback_model_ids)

    @property
    def goal_model_chain(self) -> tuple[str, ...]:
        return (self.goal_model_id, *self.goal_fallback_model_ids)

    @property
    def routine_model_chain(self) -> tuple[str, ...]:
        return (self.routine_model_id, *self.routine_fallback_model_ids)

    @property
    def bootstrap_model_chain(self) -> tuple[str, ...]:
        deduped: list[str] = []
        for candidate in (self.model_id, *self.bootstrap_fallback_model_ids):
            if candidate not in deduped:
                deduped.append(candidate)
        return tuple(deduped)

    def _planner_model_chain(self, invoke_name: str) -> tuple[str, ...]:
        preferred = self.system_map_model_id if invoke_name == "_invoke_system_map" else None
        if not preferred:
            return self.bootstrap_model_chain
        return (preferred, *(model_id for model_id in self.bootstrap_model_chain if model_id != preferred))

    def _client_factory_for_invocation(self) -> GoogleGenAIClientFactory:
        factory = getattr(self, "_google_client_factory", None)
        if factory is None:
            # Some focused tests construct the provider without __init__. Preserve
            # their legacy API-key transport while production uses from_env().
            factory = GoogleGenAIClientFactory.for_developer_api(
                api_key=getattr(self, "_api_key", None),
                base_url=getattr(self, "_base_url", None),
            )
            self._google_client_factory = factory
        return factory

    def _transport_name(self) -> str:
        factory = getattr(self, "_google_client_factory", None)
        if factory is not None:
            return factory.transport
        return "gateway" if getattr(self, "_base_url", None) else "google"

    def _build_agent(self, model_id: str):
        from strands import Agent
        from strands.models.gemini import GeminiModel

        http_timeout_ms = int(os.getenv("GEMINI_HTTP_TIMEOUT_MS", "12000"))
        if http_timeout_ms <= 0:
            raise ValueError("GEMINI_HTTP_TIMEOUT_MS must be greater than zero")
        client = self._client_factory_for_invocation().create_client(
            http_timeout_ms=http_timeout_ms
        )
        try:
            model = GeminiModel(
                client=client,
                model_id=model_id,
                params={"temperature": 0.1, "max_output_tokens": 4096},
            )
            agent = Agent(model=model, callback_handler=None)
        except Exception:
            try:
                client.close()
            except Exception:
                pass
            raise
        return _ManagedStrandsAgent(agent=agent, client=client)

    def _agent_for(self, model_id: str):
        return self._build_agent(model_id)

    @staticmethod
    def _is_temporary_unavailable(exc: BaseException) -> bool:
        pending: list[BaseException] = [exc]
        seen: set[int] = set()
        chain: list[BaseException] = []
        while pending:
            current = pending.pop()
            if id(current) in seen:
                continue
            seen.add(id(current))
            chain.append(current)
            for linked in (current.__cause__, current.__context__):
                if linked is not None and id(linked) not in seen:
                    pending.append(linked)

        protected_names = {"RESOURCE_EXHAUSTED", "UNAUTHENTICATED", "PERMISSION_DENIED"}
        availability_names = {"UNAVAILABLE"}
        protected_codes = {401, 403, 429}
        availability_codes = {503}
        saw_structured_status = False
        saw_structured_availability = False

        def status_name(value: object) -> str:
            if value is None or callable(value):
                return ""
            candidate = getattr(value, "name", value)
            text = str(candidate).strip().upper()
            return text.rsplit(".", 1)[-1]

        def status_code(value: object) -> int | None:
            if value is None or callable(value) or isinstance(value, bool):
                return None
            candidate = getattr(value, "value", value)
            if isinstance(candidate, bool) or isinstance(candidate, tuple):
                return None
            try:
                return int(candidate)
            except (TypeError, ValueError):
                return None

        for item in chain:
            response = getattr(item, "response", None)
            structured_values = (
                getattr(item, "status", None),
                getattr(item, "status_code", None),
                getattr(item, "code", None),
                getattr(response, "status_code", None),
            )
            for value in structured_values:
                if value is None or callable(value):
                    continue
                name = status_name(value)
                code = status_code(value)
                if name or code is not None:
                    saw_structured_status = True
                if name in protected_names or code in protected_codes:
                    return False
                if name in availability_names or code in availability_codes:
                    saw_structured_availability = True

        def has_explicit_http_code(message: str, code: int) -> bool:
            text = message.strip().lower()
            token = str(code)
            return (
                text.startswith(token + " ")
                or f"http {token}" in text
                or f"status {token}" in text
                or f"status={token}" in text
                or f"status_code={token}" in text
                or f"status code {token}" in text
            )

        messages = [str(item).lower() for item in chain]
        for message in messages:
            if (
                "resource_exhausted" in message
                or "resource exhausted" in message
                or "unauthenticated" in message
                or "permission_denied" in message
                or "permission denied" in message
                or any(has_explicit_http_code(message, code) for code in protected_codes)
            ):
                return False

        # Reliable structured status always wins over message-only heuristics.
        # Unknown structured statuses fail closed instead of becoming retries.
        if saw_structured_status:
            return saw_structured_availability

        for message in messages:
            if has_explicit_http_code(message, 503):
                return True
            if "503" in message and ("unavailable" in message or "high demand" in message):
                return True
            if (
                "temporarily unavailable" in message
                or "service unavailable" in message
                or "upstream unavailable" in message
            ):
                return True
        return False

    @staticmethod
    def _usage_int(usage: Mapping[str, object], key: str) -> int | None:
        value = usage.get(key)
        return None if value is None else int(value)

    def _record_usage(self, model_id: str, result, started_at: float) -> None:
        usage = getattr(getattr(result, "metrics", None), "accumulated_usage", None)
        if not isinstance(usage, Mapping) or not usage:
            self.last_usage = None
            return
        self.last_usage = GeminiUsageTelemetry(
            model_id=model_id,
            transport=self._transport_name(),
            latency_ms=max(0, round((time.perf_counter() - started_at) * 1000)),
            input_tokens=self._usage_int(usage, "inputTokens"),
            output_tokens=self._usage_int(usage, "outputTokens"),
            total_tokens=self._usage_int(usage, "totalTokens"),
            cache_read_input_tokens=self._usage_int(usage, "cacheReadInputTokens"),
            cache_write_input_tokens=self._usage_int(usage, "cacheWriteInputTokens"),
        )

    async def _invoke(self, model_id: str, prompt: str) -> GeminiDecisionWire:
        self.last_usage = None
        started_at = time.perf_counter()
        result = await self._agent_for(model_id).invoke_async(prompt, structured_output_model=GeminiDecisionWire)
        self._record_usage(model_id, result, started_at)
        if result.structured_output is None:
            raise RuntimeError("Strands returned no structured GeminiDecisionWire")
        return GeminiDecisionWire.model_validate(result.structured_output)

    async def _invoke_goal(self, model_id: str, prompt: str) -> GoalDraft:
        self.last_usage = None
        started_at = time.perf_counter()
        result = await self._agent_for(model_id).invoke_async(prompt, structured_output_model=GoalDraft)
        self._record_usage(model_id, result, started_at)
        if result.structured_output is None:
            raise RuntimeError("Strands returned no structured GoalDraft")
        return GoalDraft.model_validate(result.structured_output)

    @staticmethod
    def _finish_reason_text(value: object) -> str | None:
        if value is None:
            return None
        candidate = getattr(value, "value", value)
        text = str(candidate).strip()
        return text.rsplit(".", 1)[-1] if text else None

    @staticmethod
    def _strip_json_fence(text: str) -> tuple[str, str | None]:
        normalized = text.strip()
        if normalized.startswith("```json\n") and normalized.endswith("\n```"):
            return normalized[8:-4], "removed enclosing Markdown JSON fence"
        if normalized.startswith("```\n") and normalized.endswith("\n```"):
            return normalized[4:-4], "removed enclosing Markdown fence"
        return normalized, None

    @staticmethod
    def _normalize_planner_payload(
        normalized_output: str,
        *,
        expected_scope_id: str | None = None,
        reconcile: bool = False,
    ) -> tuple[object, list[dict[str, object]]]:
        payload = json.loads(normalized_output)
        normalizations: list[dict[str, object]] = []
        if expected_scope_id is not None:
            if not isinstance(payload, dict):
                raise ValueError("EXPAND_SCOPE provider output must be a JSON object")
            if "scope_id" not in payload:
                payload = dict(payload)
                payload["scope_id"] = expected_scope_id
                normalizations.append(
                    {
                        "field": "scope_id",
                        "operation": "filled_from_server_phase_target",
                        "value": expected_scope_id,
                    }
                )
            components = payload.get("components")
            if isinstance(components, list):
                normalized_components: list[object] = []
                for index, component in enumerate(components):
                    if isinstance(component, dict) and "type" not in component:
                        component = dict(component)
                        component["type"] = "Architecture component"
                        normalizations.append(
                            {
                                "field": f"components[{index}].type",
                                "operation": "filled_server_display_default",
                                "value": "Architecture component",
                            }
                        )
                    normalized_components.append(component)
                payload = dict(payload)
                payload["components"] = normalized_components
        if reconcile:
            if not isinstance(payload, dict):
                raise ValueError("RECONCILE provider output must be a JSON object")

            relationships = payload.get("relationships")
            if isinstance(relationships, list):
                normalized_relationships: list[object] = []
                for index, relationship in enumerate(relationships):
                    if isinstance(relationship, dict) and "type" in relationship:
                        relationship = dict(relationship)
                        alias = relationship.get("type")
                        canonical = relationship.get("relationship_type")
                        if canonical is not None and canonical != alias:
                            raise ValueError(
                                f"conflicting relationships[{index}].type/relationship_type aliases"
                            )
                        if canonical is None:
                            if not isinstance(alias, str) or not alias.strip():
                                raise ValueError(
                                    f"relationships[{index}].type alias must be a non-empty string"
                                )
                            relationship["relationship_type"] = alias.strip()
                        relationship.pop("type", None)
                        normalizations.append(
                            {
                                "field": f"relationships[{index}].relationship_type",
                                "operation": "renamed_provider_alias",
                                "source_field": "type",
                            }
                        )
                    normalized_relationships.append(relationship)
                payload = dict(payload)
                payload["relationships"] = normalized_relationships

            def normalize_structured_strings(
                field: str,
                expected_keys: tuple[str, ...],
            ) -> None:
                nonlocal payload
                values = payload.get(field)
                if not isinstance(values, list):
                    return
                normalized_values: list[object] = []
                for index, value in enumerate(values):
                    if not isinstance(value, dict):
                        normalized_values.append(value)
                        continue
                    if set(value) != set(expected_keys) or any(
                        not isinstance(value.get(key), str) or not value[key].strip()
                        for key in expected_keys
                    ):
                        raise ValueError(
                            f"unsupported structured {field}[{index}] provider shape"
                        )
                    canonical = "\n".join(
                        f"{key.replace('_', ' ').title()}: {value[key].strip()}"
                        for key in expected_keys
                    )
                    normalized_values.append(canonical)
                    normalizations.append(
                        {
                            "field": f"{field}[{index}]",
                            "operation": "canonicalized_structured_text",
                            "source_fields": list(expected_keys),
                        }
                    )
                payload = dict(payload)
                payload[field] = normalized_values

            normalize_structured_strings("decisions", ("title", "decision", "rationale"))
            normalize_structured_strings("risks", ("risk", "mitigation"))
        return payload, normalizations

    async def _invoke_planner_structured(
        self,
        model_id: str,
        prompt: str,
        output_model,
        *,
        expected_scope_id: str | None = None,
        reconcile: bool = False,
    ):
        from google.genai import types as genai_types

        http_timeout_ms = self._effective_architecture_http_timeout_ms()
        client = self._client_factory_for_invocation().create_client(
            http_timeout_ms=http_timeout_ms
        )
        started_at = time.perf_counter()
        invocation = self._current_invocation_metadata()
        dispatch_metadata = {
            "requested_model": model_id,
            "http_timeout_ms": http_timeout_ms,
            "transport": self._transport_name(),
            "thinking_level": self.architecture_thinking_level,
        }
        try:
            if invocation is not None:
                self._transition_active_planner_checkpoint(
                    "IN_FLIGHT",
                    provider=dispatch_metadata,
                )
                invocation["planner_dispatch"] = dict(dispatch_metadata)
                invocation["planner_request_started"] = True
            response = await client.aio.models.generate_content(
                model=model_id,
                contents=prompt,
                config=genai_types.GenerateContentConfig(
                    temperature=0.1,
                    max_output_tokens=self.architecture_max_output_tokens,
                    response_mime_type="application/json",
                    response_json_schema=output_model.model_json_schema(),
                    thinking_config=genai_types.ThinkingConfig(
                        thinking_level=self.architecture_thinking_level,
                        include_thoughts=False,
                    ),
                ),
            )
        except Exception as exc:
            if invocation is not None and invocation.get("planner_request_started") is True:
                dispatch_metadata.update(
                    {
                        "latency_ms": max(0, round((time.perf_counter() - started_at) * 1000)),
                        "error_type": type(exc).__name__,
                    }
                )
                invocation["planner_dispatch"] = dict(dispatch_metadata)
            raise
        finally:
            await _close_google_client(client)

        candidates = response.candidates or []
        if not candidates:
            raise RuntimeError("Gemini planner returned no candidates")
        candidate = candidates[0]
        finish_reason = self._finish_reason_text(candidate.finish_reason)
        usage = (
            response.usage_metadata.model_dump(mode="json", by_alias=True, exclude_none=True)
            if response.usage_metadata is not None
            else None
        )
        response_metadata = {
            "requested_model": model_id,
            "observed_model_version": response.model_version,
            "finish_reason": finish_reason,
            "response_id": response.response_id,
            "usage": usage,
            "latency_ms": max(0, round((time.perf_counter() - started_at) * 1000)),
            "transport": self._transport_name(),
            "thinking_level": self.architecture_thinking_level,
            "http_timeout_ms": http_timeout_ms,
            "response_reprocessable": finish_reason == "STOP",
        }
        invocation = self._current_invocation_metadata()
        if invocation is not None:
            invocation["planner_response"] = response_metadata

        parts = candidate.content.parts if candidate.content and candidate.content.parts else []
        output = "".join(part.text or "" for part in parts if not part.thought)
        response_metadata["raw_model_output"] = output
        self._transition_active_planner_checkpoint(
            "RESPONSE_RECORDED",
            provider=response_metadata,
        )
        normalized, format_normalization = self._strip_json_fence(output)
        payload, normalizations = self._normalize_planner_payload(
            normalized,
            expected_scope_id=expected_scope_id,
            reconcile=reconcile,
        )
        if format_normalization:
            normalizations.insert(0, {"operation": format_normalization})
        response_metadata["normalization"] = normalizations or None
        response_metadata["model_output"] = normalized
        response_metadata["output_sha256"] = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        response_metadata["normalized_payload_sha256"] = self._planner_payload_sha256(payload)
        if finish_reason != "STOP":
            raise RuntimeError(f"Gemini planner generation did not finish cleanly: {finish_reason or 'UNKNOWN'}")
        return output_model.model_validate(payload)

    async def _invoke_system_map(self, model_id: str, prompt: str) -> GeminiSystemMapWire:
        return await self._invoke_planner_structured(model_id, prompt, GeminiSystemMapWire)

    async def _invoke_scope_delta(self, model_id: str, prompt: str) -> GeminiScopeDeltaWire:
        expected_scope_id = self._scope_id_from_prompt(prompt)
        return await self._invoke_planner_structured(
            model_id,
            prompt,
            GeminiScopeDeltaWire,
            expected_scope_id=expected_scope_id,
        )

    async def _invoke_reconcile(self, model_id: str, prompt: str) -> GeminiReconcileWire:
        return await self._invoke_planner_structured(
            model_id,
            prompt,
            GeminiReconcileWire,
            reconcile=True,
        )

    @staticmethod
    def _require_ready(wire: GeminiSystemMapWire | GeminiScopeDeltaWire | GeminiReconcileWire) -> None:
        if wire.status == "NEEDS_FACT":
            raise ArchitectureNeedsFactError(wire.missing_facts)

    @staticmethod
    def _planner_snapshot_payload(snapshot: InitialArchitecturePlannerSnapshot | None) -> dict[str, object] | None:
        if snapshot is None:
            return None
        return {
            "roots": list(snapshot.roots),
            "components": [
                component.model_dump(mode="json", exclude_none=True)
                for component in snapshot.components
            ],
        }

    @staticmethod
    def _planner_payload_sha256(value: object) -> str:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _planner_plan_id(self, context: ProjectContext) -> str:
        identity = {
            "planner_contract": "archbro.initial_planner.v4",
            "project_id": context.project.id,
            "brief": _bootstrap_project_facts(context),
            "model_id": self.model_id,
            "system_map_model_id": self.system_map_model_id,
            "bootstrap_model_chain": self.bootstrap_model_chain,
            "thinking_level": self.architecture_thinking_level,
            "max_output_tokens": self.architecture_max_output_tokens,
        }
        return "plan_" + self._planner_payload_sha256(identity)[:32]

    def _set_planner_usage(self, *, plan_id: str, completed: bool) -> None:
        metadata = self._current_invocation_metadata()
        phases = [] if metadata is None else list(metadata.get("planner_phases") or [])
        self.last_usage = {
            "schema": "archbro.gemini_initial_planner_usage.v1",
            "transport": self._transport_name(),
            "requested_model": self.model_id,
            "thinking_level": self.architecture_thinking_level,
            "plan_id": plan_id,
            "completed": completed,
            "model_timeout_ms": round(self.architecture_model_timeout_seconds * 1000),
            "phase_timeout_ms": round(self.architecture_phase_timeout_seconds * 1000),
            "total_timeout_ms": round(self.architecture_total_timeout_seconds * 1000),
            "http_timeout_ms": self._effective_architecture_http_timeout_ms(),
            "phases": phases,
        }

    def _append_planner_phase_usage(
        self,
        checkpoint: dict[str, object],
        *,
        replayed_from_checkpoint: bool,
    ) -> None:
        provider = checkpoint.get("provider")
        if isinstance(provider, Mapping):
            provider = {
                key: provider.get(key)
                for key in (
                    "requested_model",
                    "observed_model_version",
                    "finish_reason",
                    "response_id",
                    "usage",
                    "latency_ms",
                    "transport",
                    "thinking_level",
                    "http_timeout_ms",
                    "error_type",
                    "response_reprocessable",
                    "normalization",
                    "output_sha256",
                )
            }
        compact = {
            "phase": checkpoint.get("phase_key"),
            "status": checkpoint.get("status"),
            "attempt_id": checkpoint.get("attempt_id"),
            "revision": checkpoint.get("revision"),
            "owner_generation": checkpoint.get("owner_generation"),
            "delivery_stage": checkpoint.get("delivery_stage"),
            "requested_model": checkpoint.get("requested_model"),
            "thinking_level": checkpoint.get("thinking_level"),
            "input_sha256": checkpoint.get("input_sha256"),
            "snapshot_before_sha256": checkpoint.get("snapshot_before_sha256"),
            "snapshot_after_sha256": checkpoint.get("snapshot_after_sha256"),
            "validation": checkpoint.get("validation"),
            "provider": provider,
            "replayed_from_checkpoint": replayed_from_checkpoint,
        }
        metadata = self._current_invocation_metadata()
        if metadata is not None:
            phases = metadata.setdefault("planner_phases", [])
            phases.append(compact)

    def _planner_checkpoint_identity(
        self,
        *,
        phase_key: str,
        prompt: str,
        snapshot_before: InitialArchitecturePlannerSnapshot | None,
        invoke_name: str,
    ) -> dict[str, object]:
        snapshot_payload = self._planner_snapshot_payload(snapshot_before)
        prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        requested_model = self._planner_model_chain(invoke_name)[0]
        identity = {
            "phase_key": phase_key,
            "prompt_sha256": prompt_sha256,
            "snapshot_before_sha256": (
                self._planner_payload_sha256(snapshot_payload)
                if snapshot_payload is not None
                else None
            ),
            "requested_model": requested_model,
            "thinking_level": self.architecture_thinking_level,
            "max_output_tokens": self.architecture_max_output_tokens,
        }
        return {
            **identity,
            "input_sha256": self._planner_payload_sha256(identity),
        }

    async def _run_checkpointed_planner_phase(
        self,
        *,
        plan_id: str,
        project_id: str,
        phase_key: str,
        invoke_name: str,
        prompt: str,
        output_model,
        snapshot_before: InitialArchitecturePlannerSnapshot | None,
        global_deadline: float,
        validate,
    ):
        identity = self._planner_checkpoint_identity(
            phase_key=phase_key,
            prompt=prompt,
            snapshot_before=snapshot_before,
            invoke_name=invoke_name,
        )
        repository = getattr(self, "_checkpoint_repository", None)
        started_checkpoint: dict[str, object] = {
            "schema": "archbro.initial_planner_phase.v1",
            "plan_id": plan_id,
            "project_id": project_id,
            **identity,
            "status": "STARTED",
            "attempt_id": uuid4().hex,
            "delivery_stage": "PREPARED",
            "input": {
                "prompt": prompt,
                "snapshot_before": self._planner_snapshot_payload(snapshot_before),
            },
            "validation": {"status": "PENDING"},
            "provider": None,
        }
        claimed = True
        existing = None
        if repository is not None:
            claimed, existing = repository.claim_planner_checkpoint(
                project_id=project_id,
                plan_id=plan_id,
                phase_key=phase_key,
                data=started_checkpoint,
                retry_statuses=("RETRYABLE",),
            )
            if claimed:
                started_checkpoint = existing

        if not claimed and existing is not None:
            mismatches = [
                key
                for key in (
                    "input_sha256",
                    "requested_model",
                    "thinking_level",
                    "snapshot_before_sha256",
                )
                if existing.get(key) != identity.get(key)
            ]
            if mismatches:
                raise RuntimeError(
                    f"planner checkpoint identity mismatch for {phase_key}: {', '.join(mismatches)}"
                )
            if existing.get("status") == "REPROCESSABLE":
                provider = existing.get("provider")
                raw_output = provider.get("raw_model_output") if isinstance(provider, dict) else None
                if not isinstance(raw_output, str) or not raw_output:
                    raise RuntimeError(f"planner phase {phase_key} has no recorded response to reprocess")
                result = self._parse_recorded_planner_output(invoke_name, prompt, output_model, raw_output)
                snapshot_after = validate(result)
                completed_checkpoint = {
                    **existing,
                    "status": "COMPLETED",
                    "validation": {"status": "PASS", "local_reprocess": True},
                    "validated_output": result.model_dump(mode="json", exclude_none=True),
                    "snapshot_after_sha256": (
                        self._planner_payload_sha256(snapshot_after)
                        if snapshot_after is not None
                        else None
                    ),
                }
                if repository is not None:
                    completed_checkpoint = repository.put_planner_checkpoint(
                        project_id=project_id,
                        plan_id=plan_id,
                        phase_key=phase_key,
                        data=completed_checkpoint,
                        expected_revision=int(existing.get("revision", 0)),
                        expected_owner_generation=int(existing.get("owner_generation", 0)),
                    )
                self._append_planner_phase_usage(completed_checkpoint, replayed_from_checkpoint=True)
                self._set_planner_usage(plan_id=plan_id, completed=False)
                return result
            if existing.get("status") != "COMPLETED":
                raise RuntimeError(
                    f"planner phase {phase_key} has durable {existing.get('status', 'UNKNOWN')} state; "
                    "refusing automatic paid-call replay; "
                    f"attempt_id={existing.get('attempt_id')} revision={existing.get('revision')} "
                    f"delivery_stage={existing.get('delivery_stage', 'UNKNOWN')}"
                )
            validated_output = existing.get("validated_output")
            if not isinstance(validated_output, dict):
                raise RuntimeError(f"completed planner checkpoint {phase_key} is missing validated_output")
            result = output_model.model_validate(validated_output)
            validate(result)
            self._append_planner_phase_usage(existing, replayed_from_checkpoint=True)
            self._set_planner_usage(plan_id=plan_id, completed=False)
            return result

        invocation = self._current_invocation_metadata()
        if invocation is not None:
            invocation["planner_response"] = None
            invocation["planner_request_started"] = False
            invocation["planner_checkpoint_control"] = {
                "project_id": project_id,
                "plan_id": plan_id,
                "phase_key": phase_key,
                "attempt_id": started_checkpoint.get("attempt_id"),
                "revision": int(started_checkpoint.get("revision", 0)),
                "owner_generation": int(started_checkpoint.get("owner_generation", 0)),
            }
        phase_result_returned = False
        try:
            result = await self._run_planner_phase(
                invoke_name,
                prompt,
                global_deadline=global_deadline,
            )
            phase_result_returned = True
            snapshot_after = validate(result)
            provider_metadata = None if invocation is None else invocation.get("planner_response")
            checkpoint_base = (
                repository.get_planner_checkpoint(plan_id, phase_key)
                if repository is not None
                else started_checkpoint
            ) or started_checkpoint
            completed_checkpoint = {
                **checkpoint_base,
                "status": "COMPLETED",
                "validation": {"status": "PASS"},
                "provider": provider_metadata,
                "validated_output": result.model_dump(mode="json", exclude_none=True),
                "snapshot_after_sha256": (
                    self._planner_payload_sha256(snapshot_after)
                    if snapshot_after is not None
                    else None
                ),
            }
            if repository is not None:
                completed_checkpoint = self._persist_active_planner_checkpoint(completed_checkpoint)
            self._append_planner_phase_usage(completed_checkpoint, replayed_from_checkpoint=False)
            self._set_planner_usage(plan_id=plan_id, completed=False)
            return result
        except Exception as exc:
            provider_metadata = None
            if invocation is not None:
                provider_metadata = (
                    invocation.get("planner_response")
                    or invocation.get("planner_dispatch")
                )
            cursor: BaseException | None = exc
            has_timeout = False
            seen: set[int] = set()
            while cursor is not None and id(cursor) not in seen:
                seen.add(id(cursor))
                if isinstance(cursor, TimeoutError):
                    has_timeout = True
                    break
                cursor = cursor.__cause__ or cursor.__context__
            provider_request_started = bool(
                invocation is not None
                and invocation.get("planner_request_started") is True
            )
            failed_before_provider = bool(
                invocation is not None
                and not provider_request_started
                and not phase_result_returned
            )
            checkpoint_base = (
                repository.get_planner_checkpoint(plan_id, phase_key)
                if repository is not None
                else started_checkpoint
            ) or started_checkpoint
            if provider_metadata is None:
                provider_metadata = checkpoint_base.get("provider")
            delivery_stage = str(checkpoint_base.get("delivery_stage") or "PREPARED")
            retryable = not has_timeout and failed_before_provider and delivery_stage == "PREPARED"
            failure_status = (
                "UNKNOWN"
                if has_timeout or delivery_stage == "IN_FLIGHT"
                else ("RETRYABLE" if retryable else "FAILED")
            )
            failed_checkpoint = {
                **checkpoint_base,
                "status": failure_status,
                "validation": {
                    "status": "UNKNOWN" if failure_status == "UNKNOWN" else ("RETRYABLE" if retryable else "FAIL"),
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                },
                "provider": provider_metadata,
            }
            if repository is not None:
                failed_checkpoint = self._persist_active_planner_checkpoint(failed_checkpoint)
            self._append_planner_phase_usage(failed_checkpoint, replayed_from_checkpoint=False)
            self._set_planner_usage(plan_id=plan_id, completed=False)
            raise

    async def _run_planner_phase(
        self,
        invoke_name: str,
        prompt: str,
        *,
        global_deadline: float,
    ):
        phase_deadline = min(
            global_deadline,
            time.perf_counter() + self.architecture_phase_timeout_seconds,
        )
        invocation = self._current_invocation_metadata()
        if invocation is not None:
            invocation["planner_response"] = None
            invocation["planner_dispatch"] = None
            invocation["planner_request_started"] = False
        timed_out: list[str] = []
        unavailable: list[str] = []
        last_unavailable: Exception | None = None
        invoke = getattr(self, invoke_name)

        for candidate in self._planner_model_chain(invoke_name):
            remaining = min(phase_deadline, global_deadline) - time.perf_counter()
            if remaining <= 0:
                break
            self.last_model_id = candidate
            try:
                return await asyncio.wait_for(
                    invoke(candidate, prompt),
                    timeout=min(self.architecture_model_timeout_seconds, remaining),
                )
            except TimeoutError as exc:
                timed_out.append(candidate)
                last_unavailable = exc
                # A provider-side timeout is an unknown-effect boundary: the
                # upstream may have completed or billed the request after the
                # client stopped waiting. Never issue a fallback paid call in
                # the same phase after that ambiguity; the durable checkpoint
                # is marked UNKNOWN by the caller and requires explicit review.
                break
            except Exception as exc:
                # Once a provider response exists, parsing/schema/semantic
                # validation failures are local deterministic failures. They
                # must never be interpreted as availability and retried on a
                # second paid candidate merely because their text mentions a
                # status code such as 503.
                if invocation is not None and invocation.get("planner_response") is not None:
                    raise
                if not self._is_temporary_unavailable(exc):
                    raise
                unavailable.append(candidate)
                last_unavailable = exc
                # Once this phase crossed the dispatch boundary, a transport
                # failure is an unknown-effect result. A lease/retryable HTTP
                # classification does not prove the provider did not receive
                # or bill the request, so never fall through to another model.
                if invocation is not None and invocation.get("planner_request_started") is True:
                    break

        details: list[str] = []
        if timed_out:
            details.append("timed out: " + ", ".join(timed_out))
        if unavailable:
            details.append("503 unavailable: " + ", ".join(unavailable))
        reason = "; ".join(details) or "phase/global reasoning deadline reached"
        message = (
            f"Gemini planner phase {invoke_name} could not complete ({reason}). "
            "No project state was changed; retry the event."
        )
        if unavailable and not timed_out:
            raise _PlannerSafeRetryError(message) from last_unavailable
        if not unavailable and not timed_out:
            # The phase/global deadline can expire before the first provider
            # request starts. Persisting FAILED would poison a request that is
            # known to have had no paid/provider side effect.
            raise _PlannerSafeRetryError(message)
        raise RuntimeError(message) from last_unavailable

    @staticmethod
    def _apply_scope_delta(
        snapshot: InitialArchitecturePlannerSnapshot,
        delta: GeminiScopeDeltaWire,
        *,
        expected_scope_id: str,
    ) -> InitialArchitecturePlannerSnapshot:
        GeminiProvider._require_ready(delta)
        if delta.scope_id != expected_scope_id:
            raise ValueError("scope delta returned a different scope_id")
        if expected_scope_id not in snapshot.roots:
            raise ValueError("scope delta must target an existing SYSTEM_MAP root")

        existing_ids = {component.id for component in snapshot.components}
        new_ids = [component.id for component in delta.components]
        if len(set(new_ids)) != len(new_ids):
            raise ValueError("scope delta node ids must be unique")
        reused = existing_ids.intersection(new_ids)
        if reused:
            raise ValueError("scope delta cannot redefine existing node ids: " + ", ".join(sorted(reused)))

        combined = [*snapshot.components, *delta.components]
        validated = GeminiArchitectureWire(version=1, components=combined)
        by_id = {component.id: component for component in validated.components}
        for component in delta.components:
            cursor = component
            while cursor.parent_id is not None:
                cursor = by_id[cursor.parent_id]
            if cursor.id != expected_scope_id:
                raise ValueError("scope delta may only extend its named root")

        return InitialArchitecturePlannerSnapshot(
            roots=snapshot.roots,
            components=tuple(validated.components),
        )

    @staticmethod
    def _system_map_prompt(*, event: ProjectEvent, context: ProjectContext) -> str:
        return (
            "SYSTEM_MAP phase for ArchBro initial architecture. Return ONLY root system boundaries; do not return descendants, relationships, or tasks. "
            "The roots JSON key is mandatory. READY requires 1-6 roots; NEEDS_FACT must explicitly use roots=[]. Keep summary concise (at most 600 characters). "
            "Each root must use the exact fields id, name, type, responsibility, and optional status; do not substitute description or summary for responsibility. "
            "Normally use 3-6 truthful major boundaries for a rich project, but allow 1-2 for a genuinely simple system. Never return more than 6 roots. "
            "Roots must be independently meaningful architecture responsibilities, not files/classes/functions or technology leaves promoted merely because they are easy to name. "
            "Do not hardcode a category taxonomy; derive boundaries from the confirmed Goal. If concrete missing or contradictory facts make a truthful map impossible, return NEEDS_FACT with those facts and no roots. "
            "Use stable short lowercase IDs that later scoped passes can reference."
            "\n\nCONFIRMED PROJECT BRIEF:\n" + _compact_json(_bootstrap_project_facts(context))
        )

    @staticmethod
    def _scope_prompt(
        *,
        event: ProjectEvent,
        context: ProjectContext,
        snapshot: InitialArchitecturePlannerSnapshot,
        scope_id: str,
    ) -> str:
        scope_root = next(component for component in snapshot.components if component.id == scope_id)
        reserved_ids = sorted(component.id for component in snapshot.components)
        scope_index = snapshot.roots.index(scope_id)
        remaining_scopes = len(snapshot.roots) - scope_index
        remaining_node_slots = max(0, 40 - len(snapshot.components))
        max_new_nodes = min(6, remaining_node_slots // max(1, remaining_scopes))
        return (
            f"EXPAND_SCOPE phase for root scope_id={scope_id}. Return only NEW descendants inside this named root; never regenerate or edit accepted nodes. "
            f"Include scope_id={scope_id} exactly in the JSON response and add at most {max_new_nodes} new descendants in this phase. "
            "Use parent_id to attach each new node to the named root or another new descendant. Depth is capped at 3 canonical levels. "
            "For kind use ONLY one canonical value from SYSTEM, UI, SERVICE, AGENT, TOOL, DATA_STORE, STATE, EXTERNAL_SERVICE, INFRASTRUCTURE. Never use C4 labels such as CONTAINER. "
            "Stop at independently addressable architecture responsibility/boundary/capability detail; do not emit files, classes, functions, methods, local code paths, arbitrary helpers, tables, or columns by default. "
            "An empty READY delta is valid when the root is already an architecture-level leaf. If a truthful expansion requires concrete missing/contradictory facts, return NEEDS_FACT and no components."
            "\n\nACCEPTED SCOPE ROOT JSON:\n" + _compact_json(scope_root.model_dump(mode="json", exclude_none=True))
            + "\n\nRESERVED COMPONENT IDS:\n" + _compact_json(reserved_ids)
            + "\n\nCONFIRMED PROJECT BRIEF:\n" + _compact_json(_bootstrap_project_facts(context))
        )

    @staticmethod
    def _reconcile_prompt(
        *,
        event: ProjectEvent,
        context: ProjectContext,
        snapshot: InitialArchitecturePlannerSnapshot,
        system_summary: str,
    ) -> str:
        accepted = [component.model_dump(mode="json") for component in snapshot.components]
        return (
            "RECONCILE phase for ArchBro initial architecture. The topology below is immutable. Do not rename, reparent, replace, or emit topology nodes. "
            "Return only the final concise summary, authored relationships, 1-6 critical implementation tasks, and bounded decisions/assumptions/risks. "
            "Every relationship endpoint and task.related_component must reference an accepted component ID. Do not create containment edges merely to restate hierarchy. "
            "Author enough genuine directed interactions to cover every leaf architecture component and connect the architecture's real end-to-end workflows. "
            "A leaf may be incoming-only when that is truthful (for example a data store), but no leaf may be isolated. Do not invent reciprocal edges merely for coverage. "
            "Use clear relationship directions from caller/producer toward callee/consumer, and avoid duplicate source/target/type relationships. "
            "If concrete missing/contradictory facts make truthful reconciliation impossible, return NEEDS_FACT and no reconciliation output."
            "\n\nSYSTEM MAP SUMMARY:\n" + system_summary
            + "\n\nIMMUTABLE TOPOLOGY JSON:\n" + _compact_json(accepted)
            + "\n\nCONFIRMED PROJECT BRIEF:\n" + _compact_json(_bootstrap_project_facts(context))
        )

    @staticmethod
    def _validate_reconciled_architecture(architecture: GeminiArchitectureWire) -> dict[str, object]:
        domain = architecture.to_domain()
        validate_architecture_relationship_connectivity(domain, label="RECONCILE")
        return architecture.model_dump(mode="json", exclude_none=True)

    async def _plan_initial_architecture(
        self,
        *,
        event: ProjectEvent,
        context: ProjectContext,
    ) -> GeminiBootstrapWire:
        global_deadline = time.perf_counter() + self.architecture_total_timeout_seconds
        plan_id = self._planner_plan_id(context)

        def validate_system_map(wire: GeminiSystemMapWire) -> dict[str, object]:
            self._require_ready(wire)
            if len(wire.roots) > 6:
                raise ValueError("SYSTEM_MAP allows at most 6 roots")
            root_components = tuple(root.as_component() for root in wire.roots)
            if len({component.id for component in root_components}) != len(root_components):
                raise ValueError("SYSTEM_MAP root ids must be unique")
            candidate = InitialArchitecturePlannerSnapshot(
                roots=tuple(component.id for component in root_components),
                components=root_components,
            )
            GeminiArchitectureWire(version=1, components=list(candidate.components))
            return self._planner_snapshot_payload(candidate) or {}

        system_map = await self._run_checkpointed_planner_phase(
            plan_id=plan_id,
            project_id=context.project.id,
            phase_key="SYSTEM_MAP",
            invoke_name="_invoke_system_map",
            prompt=self._system_map_prompt(event=event, context=context),
            output_model=GeminiSystemMapWire,
            snapshot_before=None,
            global_deadline=global_deadline,
            validate=validate_system_map,
        )
        root_components = tuple(root.as_component() for root in system_map.roots)
        snapshot = InitialArchitecturePlannerSnapshot(
            roots=tuple(component.id for component in root_components),
            components=root_components,
        )

        for scope_id in snapshot.roots:
            phase_snapshot = snapshot

            def validate_scope(wire: GeminiScopeDeltaWire) -> dict[str, object]:
                updated = self._apply_scope_delta(
                    phase_snapshot,
                    wire,
                    expected_scope_id=scope_id,
                )
                return self._planner_snapshot_payload(updated) or {}

            delta = await self._run_checkpointed_planner_phase(
                plan_id=plan_id,
                project_id=context.project.id,
                phase_key=f"EXPAND_SCOPE:{scope_id}",
                invoke_name="_invoke_scope_delta",
                prompt=self._scope_prompt(
                    event=event,
                    context=context,
                    snapshot=phase_snapshot,
                    scope_id=scope_id,
                ),
                output_model=GeminiScopeDeltaWire,
                snapshot_before=phase_snapshot,
                global_deadline=global_deadline,
                validate=validate_scope,
            )
            snapshot = self._apply_scope_delta(
                phase_snapshot,
                delta,
                expected_scope_id=scope_id,
            )

        final_snapshot = snapshot

        def validate_reconcile(wire: GeminiReconcileWire) -> dict[str, object]:
            self._require_ready(wire)
            candidate = GeminiArchitectureWire(
                version=1,
                summary=wire.summary or system_map.summary,
                components=list(final_snapshot.components),
                relationships=wire.relationships,
                decisions=wire.decisions,
                assumptions=wire.assumptions,
                risks=wire.risks,
            )
            component_ids = candidate.component_ids()
            if any(
                task.related_component and task.related_component not in component_ids
                for task in wire.tasks
            ):
                raise ValueError("RECONCILE task.related_component must reference accepted topology")
            return self._validate_reconciled_architecture(candidate)

        reconcile = await self._run_checkpointed_planner_phase(
            plan_id=plan_id,
            project_id=context.project.id,
            phase_key="RECONCILE",
            invoke_name="_invoke_reconcile",
            prompt=self._reconcile_prompt(
                event=event,
                context=context,
                snapshot=final_snapshot,
                system_summary=system_map.summary,
            ),
            output_model=GeminiReconcileWire,
            snapshot_before=final_snapshot,
            global_deadline=global_deadline,
            validate=validate_reconcile,
        )
        architecture = GeminiArchitectureWire(
            version=1,
            summary=reconcile.summary or system_map.summary,
            components=list(final_snapshot.components),
            relationships=reconcile.relationships,
            decisions=reconcile.decisions,
            assumptions=reconcile.assumptions,
            risks=reconcile.risks,
        )
        self._validate_reconciled_architecture(architecture)
        result = GeminiBootstrapWire(
            summary=reconcile.summary or system_map.summary or "Initial architecture created.",
            architecture=architecture,
            tasks=reconcile.tasks,
        )
        self._set_planner_usage(plan_id=plan_id, completed=True)
        return result

    @staticmethod
    def _bootstrap_to_domain_decision(wire: GeminiBootstrapWire) -> AgentDecision:
        architecture = wire.architecture.to_domain()
        actions = [
            AgentAction(
                type=AgentActionType.ADD_PROJECT_NOTE,
                payload={"note": "INITIAL_ARCHITECTURE:" + architecture.model_dump_json()},
            )
        ]
        actions.extend(
            AgentAction(
                type=AgentActionType.CREATE_TASK,
                payload={"task": task.model_dump(mode="json")},
            )
            for task in wire.tasks
        )
        return AgentDecision(summary=wire.summary, actions=actions, architecture_review_required=False)

    async def draft_goal(
        self,
        *,
        messages: list[GoalConversationMessage],
        current_goal: str = "",
    ) -> GoalDraft:
        self._begin_invocation_metadata()
        baseline = current_goal.strip()
        has_user_ask = any(message.role == "user" and message.content.strip() for message in messages)
        if not has_user_ask and not baseline:
            raise ValueError("a current Goal or at least one user Ask message is required")

        conversation = [message.model_dump(mode="json") for message in messages]
        prompt = (
            "You are the project-briefing stage of Archbro. The project does not exist yet. "
            "The user can work in TWO equivalent ways: directly edit the Goal draft, or use Ask to refine it. "
            "Your job is to MERGE the current Goal and the Ask conversation into one canonical Goal / Project Brief. "
            "Do NOT design an architecture, create tasks, choose infrastructure without evidence, or pretend project state already exists.\n\n"
            "NON-DESTRUCTIVE GOAL MERGE CONTRACT - mandatory:\n"
            "- CURRENT GOAL is an authoritative baseline written or previously accepted by the user.\n"
            "- Preserve all still-compatible requirements, constraints, outcomes, technologies, milestones, and scope already present in CURRENT GOAL.\n"
            "- Treat new Ask messages as additions, refinements, corrections, or explicit changes to that baseline.\n"
            "- Never replace the whole Goal merely because the latest Ask is shorter or discusses one detail.\n"
            "- Never return an empty Goal when CURRENT GOAL is non-empty.\n"
            "- Remove or replace an existing Goal requirement only when the user's Ask explicitly says that requirement changed, is no longer wanted, or should be rewritten.\n"
            "- If the Ask conflicts with CURRENT GOAL, resolve only the conflicting part and preserve unrelated Goal content.\n"
            "- The resulting goal field must be self-contained; a later architecture agent should not need the conversation transcript.\n\n"
            "For every turn return a GoalDraft. Set ready=true when the product outcome and first usable milestone are sufficiently clear. "
            "Explicit technical requirements or constraints must be preserved when the user gave them. "
            "Do not force the user to choose technologies if they intentionally leave those choices to the agent. "
            "If something material is still missing, set ready=false, list only important missing_information, and ask one focused natural follow-up in assistant_message. "
            "If ready=true, assistant_message should briefly summarize what changed in the Goal and say it may still be refined. "
            "suggested_project_name should be short and derived from the combined Goal and Ask.\n\n"
            "CURRENT GOAL:\n"
            + (baseline or "<empty>")
            + "\n\nASK CONVERSATION JSON:\n"
            + json.dumps(conversation, ensure_ascii=False)
        )

        unavailable: list[str] = []
        timed_out: list[str] = []
        last_unavailable: Exception | None = None
        for candidate in self.goal_model_chain:
            self.last_model_id = candidate
            try:
                draft = await asyncio.wait_for(
                    self._invoke_goal(candidate, prompt),
                    timeout=self.goal_model_timeout_seconds,
                )
                if baseline and not draft.goal.strip():
                    raise ValueError("Goal merge attempted to clear a non-empty current Goal")
                return draft
            except TimeoutError as exc:
                timed_out.append(candidate)
                last_unavailable = exc
                continue
            except Exception as exc:
                if not self._is_temporary_unavailable(exc):
                    raise
                unavailable.append(candidate)
                last_unavailable = exc

        details: list[str] = []
        if timed_out:
            details.append("timed out: " + ", ".join(timed_out))
        if unavailable:
            details.append("503 unavailable: " + ", ".join(unavailable))
        reason = "; ".join(details) or "no model completed"
        raise RuntimeError(
            f"Goal drafting could not complete within the provider deadline ({reason}). "
            "The current Goal and Ask were not changed; retry the Ask."
        ) from last_unavailable

    @staticmethod
    def _to_domain_decision(
        wire: GeminiDecisionWire,
        *,
        event: ProjectEvent,
        context: ProjectContext,
    ) -> AgentDecision:
        if any(action.type == AgentActionType.PROPOSE_ARCHITECTURE_CHANGE for action in wire.actions):
            raise ValueError("use architecture_proposal provider field instead of a free-form proposal action")

        actions = list(wire.actions)
        if wire.architecture_proposal is not None:
            proposal = ArchitectureChangeProposal(
                project_id=context.project.id,
                **wire.architecture_proposal.model_dump(mode="json"),
            )
            actions.append(
                AgentAction(
                    type=AgentActionType.PROPOSE_ARCHITECTURE_CHANGE,
                    payload={"proposal": proposal.model_dump(mode="json")},
                )
            )
        return AgentDecision(
            summary=wire.summary,
            actions=actions,
            architecture_review_required=wire.architecture_review_required,
            evaluation=wire.evaluation,
        )

    async def generate(self, *, event: ProjectEvent, context: ProjectContext, system_prompt: str) -> AgentDecision:
        self._begin_invocation_metadata()
        is_routine_update = event.type == ProjectEventType.TASK_UPDATED
        is_bootstrap = (
            context.architecture.version == 0
            and event.type == ProjectEventType.USER_MESSAGE
            and event.payload.get("intent") == "INITIAL_ARCHITECTURE"
        )

        if is_bootstrap:
            wire = await self._plan_initial_architecture(event=event, context=context)
            return self._bootstrap_to_domain_decision(wire)

        raw_manifest = event.payload.get("agent_context_manifest")
        agent_context_manifest = raw_manifest if isinstance(raw_manifest, dict) else None
        prompt = (
            system_prompt
            + "\n\nPROJECT CONTEXT (bounded JSON):\n"
            + _compact_json(
                _compact_context_facts(
                    context,
                    agent_context_manifest=agent_context_manifest,
                )
            )
            + "\n\nOBSERVED EVENT:\n"
            + _compact_json(_compact_event_facts(event))
        )
        candidate_chain = self.routine_model_chain if is_routine_update else self.model_chain
        per_model_timeout = (
            self.routine_model_timeout_seconds
            if is_routine_update
            else self.interaction_model_timeout_seconds
        )
        total_timeout = (
            per_model_timeout * max(1, len(candidate_chain))
            if is_routine_update
            else max(per_model_timeout, self.interaction_total_timeout_seconds)
        )
        started = time.perf_counter()
        unavailable: list[str] = []
        timed_out: list[str] = []
        last_unavailable: Exception | None = None

        for candidate in candidate_chain:
            remaining = total_timeout - (time.perf_counter() - started)
            if remaining <= 0:
                break
            self.last_model_id = candidate
            try:
                wire = await asyncio.wait_for(
                    self._invoke(candidate, prompt),
                    timeout=min(per_model_timeout, remaining),
                )
                return self._to_domain_decision(wire, event=event, context=context)
            except TimeoutError as exc:
                timed_out.append(candidate)
                last_unavailable = exc
                continue
            except Exception as exc:
                if not self._is_temporary_unavailable(exc):
                    raise
                unavailable.append(candidate)
                last_unavailable = exc

        details: list[str] = []
        if timed_out:
            details.append("timed out: " + ", ".join(timed_out))
        if unavailable:
            details.append("503 unavailable: " + ", ".join(unavailable))
        models = ", ".join(candidate_chain)
        reason = "; ".join(details) or "overall reasoning deadline reached"
        raise RuntimeError(
            f"Gemini models {models} could not complete within the bounded reasoning window ({reason}). "
            "No project state was changed; retry the event."
        ) from last_unavailable
