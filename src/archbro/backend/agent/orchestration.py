from __future__ import annotations

import logging
import time
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from archbro.backend.agent.evaluation import DriftPolicy
from archbro.backend.agent.prompts import SYSTEM_PROMPT
from archbro.backend.core.contracts import (
    AgentAction,
    AgentActionType,
    AgentDecision,
    AgentRunResult,
    GitHubChangePayload,
    ObservationClaimState,
    ProjectEvent,
    ProjectEventSource,
    ProjectEventType,
    TaskStatus,
)
from archbro.backend.core.action_executor import ActionExecutor
from archbro.backend.core.evaluation import (
    DriftClassification,
    DriftEvaluation,
    DriftRecommendedAction,
)
from archbro.backend.llm.provider import ModelProvider
from archbro.backend.core.observation import ObservationInProgressError
from archbro.backend.core.repository import ProjectRepositoryPort

logger = logging.getLogger("archbro")


def _context_telemetry(event: ProjectEvent) -> dict[str, object] | None:
    manifest = event.payload.get("agent_context_manifest")
    if not isinstance(manifest, dict):
        return None
    usage = manifest.get("usage")
    if not isinstance(usage, dict):
        return None
    return {
        **usage,
        "manifest_hash": manifest.get("manifest_hash"),
        "architecture_version": manifest.get("architecture_version"),
        "selection": manifest.get("selection"),
    }


def _execution_context_telemetry(
    event: ProjectEvent,
    external_tools: Any | None,
) -> dict[str, object] | None:
    telemetry = _context_telemetry(event) or {}
    if external_tools is not None:
        try:
            mcp_telemetry = external_tools.telemetry()
        except Exception:
            logger.warning("agent MCP telemetry unavailable", exc_info=True)
        else:
            if isinstance(mcp_telemetry, dict):
                telemetry["external_mcp"] = mcp_telemetry
    return telemetry or None


def _external_mcp_counts(external_tools: Any | None) -> tuple[int, int]:
    if external_tools is None:
        return 0, 0
    try:
        telemetry = external_tools.telemetry()
        if not isinstance(telemetry, dict):
            return 0, 0
        return (
            int(telemetry.get("call_count", 0)),
            int(telemetry.get("successful_call_count", 0)),
        )
    except Exception:
        logger.warning("agent MCP counters unavailable", exc_info=True)
        return 0, 0


def _provider_usage(provider: ModelProvider) -> dict[str, object] | None:
    try:
        usage = getattr(provider, "last_usage", None)
        if usage is None:
            return None
        if is_dataclass(usage):
            return asdict(usage)
        if hasattr(usage, "model_dump"):
            return usage.model_dump(mode="json")
        if isinstance(usage, dict):
            return dict(usage)
    except Exception:
        logger.warning("provider usage telemetry unavailable", exc_info=True)
    return None


class AgentOrchestrator:
    def __init__(self, repository: ProjectRepositoryPort, provider: ModelProvider) -> None:
        self.repository = repository
        self.provider = provider
        self.executor = ActionExecutor(repository)

    async def observe_event(
        self,
        event: ProjectEvent,
        *,
        external_tools: Any | None = None,
    ) -> AgentRunResult:
        run_id = f"run_{uuid4().hex}"
        started = time.perf_counter()
        started_at = datetime.now(timezone.utc)

        project = self.repository.get_project(event.project_id)
        context = self.repository.load_context(event.project_id)

        # Canonicalize external provider input before durable observation registration so
        # replayed deliveries compare against exactly the same persisted payload.
        try:
            if event.type == ProjectEventType.GITHUB_CHANGE and event.source == ProjectEventSource.GITHUB:
                if not event.source_event_id:
                    raise ValueError("GITHUB_CHANGE events require source_event_id")
                normalized = GitHubChangePayload.model_validate(event.payload)
                event = event.model_copy(
                    update={"payload": normalized.model_dump(mode="json", exclude_none=True)}
                )

            if context.architecture.version == 0:
                if not project.goal.strip():
                    raise ValueError("project goal is required before generating the initial architecture")
                if event.type != ProjectEventType.USER_MESSAGE or event.payload.get("intent") != "INITIAL_ARCHITECTURE":
                    raise ValueError(
                        "initial architecture has not been generated; use the project Goal/Brief and Generate initial architecture first"
                    )

                brief = project.goal.strip()
                if project.description.strip():
                    brief += "\n\nAdditional project context:\n" + project.description.strip()
                event = event.model_copy(
                    update={"payload": {"intent": "INITIAL_ARCHITECTURE", "message": brief}}
                )
            elif event.payload.get("intent") == "INITIAL_ARCHITECTURE":
                raise ValueError("initial architecture already exists")
        except ValueError as exc:
            used_model = getattr(self.provider, "last_model_id", self.provider.model_id)
            result = AgentRunResult(
                project_id=event.project_id,
                event_id=event.id,
                agent_run_id=run_id,
                summary="Agent run failed before observation registration.",
                actions=[],
                architecture_review_required=False,
                proposal_ids=[],
                evaluation=None,
                provider=self.provider.name,
                model=used_model,
                result="ERROR",
                error=f"{type(exc).__name__}: {exc}",
                context_telemetry=_execution_context_telemetry(event, external_tools),
                started_at=started_at,
                completed_at=datetime.now(timezone.utc),
            )
            return result

        claim = self.repository.claim_observation(event, run_id=run_id)
        if claim.state == ObservationClaimState.REPLAY:
            if claim.existing_result is None:
                raise RuntimeError("completed observation is missing its durable AgentRun")
            return claim.existing_result.model_copy(update={"replayed": True})
        if claim.state == ObservationClaimState.IN_PROGRESS:
            raise ObservationInProgressError(
                f"observation {claim.event.id} is already being evaluated"
            )

        event = claim.event
        run_id = claim.run_id
        context = self.repository.load_context(event.project_id)

        try:
            # A human clicking Start/Done is authoritative project state, not a model
            # suggestion. Apply the explicit transition deterministically, persist the
            # observation, and let later agent/project signals evaluate implications.
            if event.type == ProjectEventType.TASK_UPDATED:
                if event.source not in {ProjectEventSource.HUMAN, ProjectEventSource.FRONTEND}:
                    raise ValueError("authoritative TASK_UPDATED transitions require HUMAN or FRONTEND provenance")
                task_id = str(event.payload.get("task_id", "")).strip()
                if not task_id or not any(task.id == task_id for task in context.tasks):
                    raise ValueError("TASK_UPDATED requires a task_id from this project")
                status = TaskStatus(str(event.payload.get("status", "")))
                action = AgentAction(
                    type=AgentActionType.UPDATE_TASK,
                    payload={"task_id": task_id, "changes": {"status": status.value}},
                )
                plan = self.executor.build_plan(
                    event.project_id,
                    [action],
                    evidence_event_id=event.id,
                )
                result = AgentRunResult(
                    project_id=event.project_id,
                    event_id=event.id,
                    agent_run_id=run_id,
                    summary=f"Human task state accepted: {status.value}.",
                    actions=[action],
                    architecture_review_required=False,
                    proposal_ids=[],
                    evaluation=None,
                    provider="deterministic",
                    model="human-task-transition",
                    result="SUCCESS",
                    started_at=started_at,
                    completed_at=datetime.now(timezone.utc),
                )
                self.repository.commit_observation_result(
                    event=event,
                    run_id=run_id,
                    plan=plan,
                    result=result,
                )
                latency_ms = round((time.perf_counter() - started) * 1000)
                logger.info(
                    "agent_run project_id=%s event_id=%s agent_run_id=%s provider=%s model=%s latency_ms=%s action_count=%s result=%s",
                    event.project_id,
                    event.id,
                    run_id,
                    result.provider,
                    result.model,
                    latency_ms,
                    len(result.actions),
                    result.result,
                )
                return result

            decision_provider = self.provider.name
            decision_model: str | None = None
            verification_requested = bool(
                external_tools is not None
                and getattr(external_tools, "verification_required", False)
            )
            tools_ready = bool(
                external_tools is not None
                and getattr(external_tools, "has_tools", False)
            )
            if verification_requested and not tools_ready:
                reason = str(
                    getattr(external_tools, "discovery_error", None)
                    or "No approved GitHub MCP read-only tool is available."
                ).strip()[:500]
                decision = AgentDecision(
                    summary=f"GitHub repository verification could not be completed: {reason}",
                    evaluation=DriftEvaluation(
                        classification=DriftClassification.INSUFFICIENT_EVIDENCE,
                        summary=(
                            "The requested repository evidence is unavailable, so the current "
                            "architecture is preserved without claiming alignment."
                        ),
                        evidence=[f"GitHub MCP unavailable: {reason}"],
                        affected_components=[],
                        affected_tasks=[],
                        architecture_change_required=False,
                        recommended_action=DriftRecommendedAction.KEEP_CURRENT,
                    ),
                    actions=[AgentAction(type=AgentActionType.NO_ACTION)],
                )
                provider_usage = None
                decision_provider = "deterministic"
                decision_model = "github-mcp-unavailable"
            elif external_tools is None:
                decision = await self.provider.generate(
                    event=event,
                    context=context,
                    system_prompt=SYSTEM_PROMPT,
                )
            else:
                decision = await self.provider.generate_with_external_tools(
                    event=event,
                    context=context,
                    system_prompt=SYSTEM_PROMPT,
                    external_tools=external_tools,
                )
                provider_usage = _provider_usage(self.provider)
            if external_tools is None:
                provider_usage = _provider_usage(self.provider)

            if (
                external_tools is not None
                and bool(getattr(external_tools, "requires_successful_call", False))
                and int(getattr(external_tools, "successful_call_count", 0)) <= 0
            ):
                raise RuntimeError(
                    "Repository verification was explicitly requested, but the built-in agent completed without a successful GitHub MCP call"
                )

            source_references: list[str] = []
            if external_tools is not None:
                try:
                    references = external_tools.evidence_references(limit=5)
                except Exception:
                    logger.warning("agent MCP evidence references unavailable", exc_info=True)
                else:
                    if isinstance(references, list):
                        source_references = [
                            str(reference).strip()[:1000]
                            for reference in references
                            if str(reference).strip()
                        ][:5]
            if source_references:
                if decision.evaluation is not None:
                    evidence: list[str] = []
                    for item in [*source_references, *decision.evaluation.evidence]:
                        normalized = str(item).strip()
                        if normalized and normalized not in evidence:
                            evidence.append(normalized)
                        if len(evidence) >= 5:
                            break
                    decision = decision.model_copy(
                        update={
                            "evaluation": decision.evaluation.model_copy(
                                update={"evidence": evidence}
                            )
                        }
                    )
                missing_summary_references = [
                    reference
                    for reference in source_references
                    if reference not in decision.summary
                ]
                if missing_summary_references:
                    decision = decision.model_copy(
                        update={
                            "summary": (
                                decision.summary.rstrip()
                                + "\n\nVerified sources: "
                                + "; ".join(missing_summary_references)
                            )
                        }
                    )

            # M4 safety boundary: provider output must be fully validated before the
            # observed event or any model-derived product mutation is persisted.
            DriftPolicy.validate(context, decision)
            if event.source not in {ProjectEventSource.HUMAN, ProjectEventSource.FRONTEND} and any(
                action.type == AgentActionType.UPDATE_PROJECT_STATUS for action in decision.actions
            ):
                raise ValueError("external observations cannot directly change project status")
            self.executor.validate_all(event.project_id, decision.actions)
            plan = self.executor.build_plan(
                event.project_id,
                decision.actions,
                evidence_event_id=event.id,
            )
            used_model = decision_model or getattr(
                self.provider,
                "last_model_id",
                self.provider.model_id,
            )
            result = AgentRunResult(
                project_id=event.project_id,
                event_id=event.id,
                agent_run_id=run_id,
                summary=decision.summary,
                actions=decision.actions,
                architecture_review_required=decision.architecture_review_required,
                proposal_ids=plan.proposal_ids,
                evaluation=decision.evaluation,
                provider=decision_provider,
                model=used_model,
                result="SUCCESS",
                context_telemetry=_execution_context_telemetry(event, external_tools),
                provider_usage=provider_usage,
                started_at=started_at,
                completed_at=datetime.now(timezone.utc),
            )
            self.repository.commit_observation_result(
                event=event,
                run_id=run_id,
                plan=plan,
                result=result,
            )
        except Exception as exc:
            used_model = getattr(self.provider, "last_model_id", self.provider.model_id)
            result = AgentRunResult(
                project_id=event.project_id,
                event_id=event.id,
                agent_run_id=run_id,
                summary="Agent run failed before state mutation.",
                actions=[],
                architecture_review_required=False,
                proposal_ids=[],
                evaluation=None,
                provider=self.provider.name,
                model=used_model,
                result="ERROR",
                error=f"{type(exc).__name__}: {exc}",
                context_telemetry=_execution_context_telemetry(event, external_tools),
                provider_usage=_provider_usage(self.provider),
                started_at=started_at,
                completed_at=datetime.now(timezone.utc),
            )
            self.repository.fail_observation(event=event, run_id=run_id, result=result)
        latency_ms = round((time.perf_counter() - started) * 1000)
        mcp_call_count, mcp_success_count = _external_mcp_counts(external_tools)
        logger.info(
            "agent_run project_id=%s event_id=%s agent_run_id=%s provider=%s model=%s latency_ms=%s action_count=%s result=%s mcp_call_count=%s mcp_success_count=%s",
            event.project_id,
            event.id,
            run_id,
            result.provider,
            result.model,
            latency_ms,
            len(result.actions),
            result.result,
            mcp_call_count,
            mcp_success_count,
        )
        return result
