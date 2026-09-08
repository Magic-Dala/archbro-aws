from __future__ import annotations

from typing import Any, Protocol

from archbro.backend.core.contracts import (
    AgentRunResult,
    Architecture,
    ArchitectureChangeProposal,
    ObservationClaim,
    Project,
    ProjectContext,
    ProjectEvent,
    ProjectEventType,
    ProposalStatus,
    Task,
)
from archbro.backend.core.observation import ObservationMutationPlan


class ConcurrentStateError(ValueError):
    """A deterministic mutation plan is stale relative to current persisted state."""


class IdempotencyConflictError(ValueError):
    """An idempotency key was reused for a different semantic request."""


class ProjectRepositoryPort(Protocol):
    """Backend-owned persistence contract implemented by Platform.

    Jim's backend code depends on this interface, not on PostgreSQL details.
    Max can replace the concrete persistence implementation without changing the
    Agent, API, or domain contracts.
    """

    def save_project(self, project: Project) -> None: ...
    def get_project(self, project_id: str) -> Project: ...
    def list_projects(self) -> list[Project]: ...
    def delete_project(self, project_id: str) -> bool: ...

    def save_architecture(self, project_id: str, architecture: Architecture) -> None: ...
    def get_architecture(self, project_id: str) -> Architecture: ...

    def save_task(self, project_id: str, task: Task) -> None: ...
    def get_task(self, task_id: str) -> Task: ...
    def list_tasks(self, project_id: str) -> list[Task]: ...

    def save_proposal(self, proposal: ArchitectureChangeProposal) -> None: ...
    def get_proposal(self, proposal_id: str) -> ArchitectureChangeProposal: ...
    def list_proposals(self, project_id: str) -> list[ArchitectureChangeProposal]: ...

    def save_acceptance_state(
        self,
        *,
        project_id: str,
        expected_architecture_version: int,
        expected_task_updated_at: dict[str, str],
        project: Project,
        architecture: Architecture,
        tasks: list[Task],
        proposal: ArchitectureChangeProposal,
    ) -> None:
        """Persist one accepted architecture transition atomically."""
        ...

    def save_proposal_decision(
        self,
        *,
        project_id: str,
        proposal: ArchitectureChangeProposal,
        expected_status: ProposalStatus,
    ) -> None:
        """Persist a human proposal decision only if its current status still matches."""
        ...

    def save_event(self, event: ProjectEvent) -> None: ...
    def commit_event_actions(
        self,
        *,
        event: ProjectEvent,
        project: Project | None,
        architecture: Architecture | None,
        tasks: list[Task],
        proposals: list[ArchitectureChangeProposal],
        notes: list[str],
        expected_project_updated_at: str | None = None,
        expected_architecture_version: int | None = None,
        expected_task_updated_at: dict[str, str] | None = None,
    ) -> ProjectEvent:
        """Persist one event and its materialized state mutations atomically.

        Returns the canonical persisted event. When ``source_event_id`` identifies
        an already-committed equivalent semantic request, implementations return
        that original event without re-applying mutations.
        """
        ...
    def get_event(self, event_id: str) -> ProjectEvent: ...
    def list_events(self, project_id: str, limit: int = 100) -> list[ProjectEvent]: ...
    def get_latest_event_by_type(
        self,
        project_id: str,
        event_type: ProjectEventType,
    ) -> ProjectEvent | None: ...
    def list_agent_runs(self, project_id: str, limit: int = 100) -> list[AgentRunResult]: ...

    def get_planner_checkpoint(self, plan_id: str, phase_key: str) -> dict[str, Any] | None:
        """Return one durable initial-architecture planner phase checkpoint."""
        ...

    def put_planner_checkpoint(
        self,
        *,
        project_id: str,
        plan_id: str,
        phase_key: str,
        data: dict[str, Any],
        expected_revision: int | None = None,
        expected_owner_generation: int | None = None,
    ) -> dict[str, Any]:
        """CAS-persist one durable planner phase checkpoint.

        ``expected_revision`` plus ``expected_owner_generation`` fence late
        workers from overwriting a newer attempt/recovery decision.
        """
        ...

    def claim_planner_checkpoint(
        self,
        *,
        project_id: str,
        plan_id: str,
        phase_key: str,
        data: dict[str, Any],
        retry_statuses: tuple[str, ...] = (),
    ) -> tuple[bool, dict[str, Any]]:
        """Atomically claim one planner phase or return its durable competing state.

        A retryable checkpoint may only be reclaimed for the exact same
        ``input_sha256`` identity.  Identity drift must remain observable to
        the planner instead of being overwritten by a retry attempt.
        """
        ...

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
    ) -> dict[str, Any]:
        """Apply one explicit idempotent planner recovery decision."""
        ...

    def claim_observation(self, event: ProjectEvent, *, run_id: str) -> ObservationClaim:
        """Atomically register/dedupe an observation and claim it for evaluation."""
        ...

    def commit_observation_result(
        self,
        *,
        event: ProjectEvent,
        run_id: str,
        plan: ObservationMutationPlan,
        result: AgentRunResult,
    ) -> None:
        """Atomically persist a successful AgentRun and its materialized state effect."""
        ...

    def fail_observation(
        self,
        *,
        event: ProjectEvent,
        run_id: str,
        result: AgentRunResult,
    ) -> None:
        """Persist a failed AgentRun without mutating accepted project state."""
        ...

    def add_note(self, project_id: str, note: str) -> None: ...
    def list_notes(self, project_id: str, limit: int = 20) -> list[str]: ...

    def load_context(self, project_id: str) -> ProjectContext: ...
    def snapshot(self, project_id: str) -> str: ...
