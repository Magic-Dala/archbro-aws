from __future__ import annotations

import json
from datetime import datetime, timezone

from archbro.backend.core.contracts import (
    AcceptanceTaskChange,
    AgentAction,
    AgentActionType,
    Architecture,
    ArchitectureAcceptancePreview,
    ArchitectureChangeProposal,
    ProjectStatus,
    ProposalStatus,
    SupersededProposalPreview,
    Task,
    TaskProposal,
)
from archbro.backend.core.observation import ObservationMutationPlan
from archbro.backend.core.reconciliation import ArchitectureAcceptanceReconciler
from archbro.backend.core.repository import ProjectRepositoryPort


class ActionExecutor:
    _TASK_MUTABLE_FIELDS = {
        "title",
        "description",
        "status",
        "owner",
        "related_component",
        "dependencies",
        "acceptance_criteria",
    }

    def __init__(self, repository: ProjectRepositoryPort) -> None:
        self.repository = repository

    def _task_update_from(self, task: Task, changes: dict) -> Task:
        unsupported = set(changes).difference(self._TASK_MUTABLE_FIELDS)
        if unsupported:
            raise ValueError(
                "task update contains immutable or unsupported fields: "
                + ", ".join(sorted(unsupported))
            )

        candidate = task.model_dump(mode="python")
        candidate.update(changes)
        candidate["id"] = task.id
        candidate["source"] = task.source
        candidate["created_at"] = task.created_at
        candidate["updated_at"] = datetime.now(timezone.utc)
        return Task.model_validate(candidate)

    def validate_all(self, project_id: str, actions: list[AgentAction]) -> None:
        project_tasks = {task.id: task for task in self.repository.list_tasks(project_id)}
        for action in actions:
            if action.type == AgentActionType.UPDATE_TASK:
                task_id = str(action.payload["task_id"])
                task = project_tasks.get(task_id)
                if task is None:
                    raise ValueError("task does not belong to project")
                self._task_update_from(task, dict(action.payload["changes"]))
            elif action.type == AgentActionType.CREATE_TASK:
                TaskProposal.model_validate(action.payload["task"])
            elif action.type == AgentActionType.UPDATE_PROJECT_STATUS:
                ProjectStatus(action.payload["status"])
            elif action.type == AgentActionType.PROPOSE_ARCHITECTURE_CHANGE:
                proposal = ArchitectureChangeProposal.model_validate(action.payload["proposal"])
                if proposal.project_id != project_id:
                    raise ValueError("proposal project_id mismatch")
                if not proposal.evidence:
                    raise ValueError("architecture change proposal requires evidence")
                # Validate the exact deterministic acceptance semantics before a
                # proposal is persisted. A human must never be shown a review item
                # that cannot be applied atomically if they accept it.
                ArchitectureAcceptanceReconciler().build_plan(
                    architecture=self.repository.get_architecture(project_id),
                    proposal=proposal,
                    tasks=list(project_tasks.values()),
                )

    def build_plan(
        self,
        project_id: str,
        actions: list[AgentAction],
        *,
        evidence_event_id: str | None = None,
    ) -> ObservationMutationPlan:
        """Materialize validated actions and the state preconditions they depend on."""

        self.validate_all(project_id, actions)
        current_architecture = self.repository.get_architecture(project_id)
        current_project = self.repository.get_project(project_id)
        working_project = current_project
        project_changed = False
        architecture: Architecture | None = None
        task_state = {task.id: task for task in self.repository.list_tasks(project_id)}
        changed_task_ids: list[str] = []
        expected_task_updated_at: dict[str, str] = {}
        proposals: list[ArchitectureChangeProposal] = []
        notes: list[str] = []

        for action in actions:
            if action.type == AgentActionType.CREATE_TASK:
                task_proposal = TaskProposal.model_validate(action.payload["task"])
                task = Task(**task_proposal.model_dump())
                task_state[task.id] = task
                changed_task_ids.append(task.id)
            elif action.type == AgentActionType.UPDATE_TASK:
                task_id = str(action.payload["task_id"])
                task = task_state.get(task_id)
                if task is None:
                    raise ValueError("task does not belong to project")
                expected_task_updated_at.setdefault(task_id, task.updated_at.isoformat())
                task_state[task_id] = self._task_update_from(
                    task,
                    dict(action.payload["changes"]),
                )
                if task_id not in changed_task_ids:
                    changed_task_ids.append(task_id)
            elif action.type == AgentActionType.ADD_PROJECT_NOTE:
                note = action.payload["note"]
                if note.startswith("INITIAL_ARCHITECTURE:"):
                    if current_architecture.version != 0 or architecture is not None:
                        raise ValueError("initial architecture may only be set once")
                    architecture = Architecture.model_validate(json.loads(note.split(":", 1)[1]))
                    working_project = working_project.model_copy(
                        update={
                            "architecture_version": architecture.version,
                            "updated_at": datetime.now(timezone.utc),
                        }
                    )
                    project_changed = True
                else:
                    notes.append(note)
            elif action.type == AgentActionType.UPDATE_PROJECT_STATUS:
                working_project = working_project.model_copy(
                    update={
                        "status": ProjectStatus(action.payload["status"]),
                        "updated_at": datetime.now(timezone.utc),
                    }
                )
                project_changed = True
            elif action.type == AgentActionType.PROPOSE_ARCHITECTURE_CHANGE:
                candidate = ArchitectureChangeProposal.model_validate(action.payload["proposal"])
                proposal = ArchitectureChangeProposal(
                    project_id=project_id,
                    base_architecture_version=current_architecture.version,
                    reason=candidate.reason,
                    evidence=list(candidate.evidence),
                    evidence_event_ids=[evidence_event_id] if evidence_event_id else [],
                    observed_change=candidate.observed_change,
                    affected_components=list(candidate.affected_components),
                    proposed_changes=[dict(change) for change in candidate.proposed_changes],
                    impact=candidate.impact,
                    recommended_option=candidate.recommended_option,
                )
                action.payload["proposal"] = proposal.model_dump(mode="json")
                proposals.append(proposal)
            elif action.type == AgentActionType.NO_ACTION:
                continue

        return ObservationMutationPlan(
            project=working_project if project_changed else None,
            architecture=architecture,
            tasks=[task_state[task_id] for task_id in changed_task_ids],
            proposals=proposals,
            notes=notes,
            expected_project_updated_at=(
                current_project.updated_at.isoformat() if project_changed else None
            ),
            expected_architecture_version=(
                current_architecture.version if architecture is not None or proposals else None
            ),
            expected_task_updated_at=expected_task_updated_at,
        )

    def apply(self, project_id: str, actions: list[AgentAction]) -> list[str]:
        plan = self.build_plan(project_id, actions)
        self.apply_plan(project_id, plan)
        return plan.proposal_ids

    def apply_plan(self, project_id: str, plan: ObservationMutationPlan) -> None:
        """Persist one already-built deterministic mutation plan.

        Semantic API surfaces that need to return generated task identifiers can
        build a plan once, inspect the materialized task, and persist that exact
        plan without regenerating ids on a second build.
        """
        if plan.architecture is not None:
            self.repository.save_architecture(project_id, plan.architecture)
        if plan.project is not None:
            self.repository.save_project(plan.project)
        for task in plan.tasks:
            self.repository.save_task(project_id, task)
        for proposal in plan.proposals:
            self.repository.save_proposal(proposal)
        for note in plan.notes:
            self.repository.add_note(project_id, note)

    def preview_proposal_acceptance(
        self, project_id: str, proposal_id: str
    ) -> ArchitectureAcceptancePreview:
        snapshot = self.repository.load_agent_context_snapshot(project_id, event_scan_limit=1)
        proposal = next((item for item in snapshot.proposals if item.id == proposal_id), None)
        if proposal is None:
            raise KeyError(proposal_id)
        if proposal.project_id != project_id or proposal.status != ProposalStatus.PENDING:
            raise ValueError("proposal is not pending for this project")

        architecture = snapshot.architecture
        if proposal.base_architecture_version != architecture.version:
            raise ValueError(
                "stale architecture proposal: expected accepted architecture "
                f"v{proposal.base_architecture_version}, current is v{architecture.version}"
            )
        project = snapshot.project
        if project.architecture_version != architecture.version:
            raise ValueError(
                "project architecture version is inconsistent with accepted architecture: "
                f"project=v{project.architecture_version}, architecture=v{architecture.version}"
            )
        tasks = snapshot.tasks
        plan = ArchitectureAcceptanceReconciler().build_plan(
            architecture=architecture,
            proposal=proposal,
            tasks=tasks,
        )

        tasks_by_id = {task.id: task for task in tasks}
        task_updates: list[AcceptanceTaskChange] = []
        for updated in plan.updated_tasks:
            before = tasks_by_id[updated.id]
            changed_fields = [
                field
                for field in ("status", "title", "description", "related_component")
                if getattr(before, field) != getattr(updated, field)
            ]
            task_updates.append(
                AcceptanceTaskChange(
                    task_id=updated.id,
                    before=before,
                    after=updated,
                    changed_fields=changed_fields,
                    blocked=(
                        before.status != updated.status
                        and updated.status.value == "BLOCKED"
                    ),
                    remapped=before.related_component != updated.related_component,
                )
            )

        superseded_reason = (
            f"Superseded by accepted proposal {proposal.id} when accepted architecture "
            f"advanced to v{plan.architecture.version}."
        )
        superseded = [
            SupersededProposalPreview(
                proposal_id=peer.id,
                base_architecture_version=peer.base_architecture_version,
                superseded_at_architecture_version=plan.architecture.version,
                reason=superseded_reason,
            )
            for peer in snapshot.proposals
            if peer.id != proposal.id
            and peer.status == ProposalStatus.PENDING
            and peer.base_architecture_version != plan.architecture.version
        ]
        previous_decisions = len(architecture.decisions)
        return ArchitectureAcceptancePreview(
            proposal_id=proposal.id,
            proposal_status=proposal.status,
            actionable=True,
            current_architecture_version=architecture.version,
            resulting_architecture_version=plan.architecture.version,
            components_before=architecture.components,
            components_after=plan.architecture.components,
            relationships_before=architecture.relationships,
            relationships_after=plan.architecture.relationships,
            decisions_added=plan.architecture.decisions[previous_decisions:],
            task_updates=task_updates,
            created_tasks=list(plan.created_tasks),
            blocked_task_ids=[change.task_id for change in task_updates if change.blocked],
            remapped_tasks=[
                {
                    "task_id": change.task_id,
                    "from_component": change.before.related_component,
                    "to_component": change.after.related_component,
                }
                for change in task_updates
                if change.remapped
            ],
            superseded_proposals=superseded,
            warnings=[],
        )

    def accept_proposal(self, project_id: str, proposal_id: str) -> ArchitectureChangeProposal:
        proposal = self.repository.get_proposal(proposal_id)
        if proposal.project_id != project_id or proposal.status != ProposalStatus.PENDING:
            raise ValueError("proposal is not pending for this project")
        architecture = self.repository.get_architecture(project_id)
        if proposal.base_architecture_version != architecture.version:
            raise ValueError(
                "stale architecture proposal: expected accepted architecture "
                f"v{proposal.base_architecture_version}, current is v{architecture.version}"
            )
        project = self.repository.get_project(project_id)
        if project.architecture_version != architecture.version:
            raise ValueError(
                "project architecture version is inconsistent with accepted architecture: "
                f"project=v{project.architecture_version}, architecture=v{architecture.version}"
            )
        tasks = self.repository.list_tasks(project_id)
        plan = ArchitectureAcceptanceReconciler().build_plan(
            architecture=architecture,
            proposal=proposal,
            tasks=tasks,
        )

        project = project.model_copy(
            update={
                "architecture_version": plan.architecture.version,
                "updated_at": datetime.now(timezone.utc),
            }
        )
        resolved_at = datetime.now(timezone.utc)
        accepted_proposal = proposal.model_copy(
            update={
                "status": ProposalStatus.ACCEPTED,
                "resolved_at": resolved_at,
                "resolution_reason": (
                    f"Accepted against architecture v{architecture.version}; "
                    f"advanced accepted architecture to v{plan.architecture.version}."
                ),
            }
        )
        updated_task_ids = {task.id for task in plan.updated_tasks}

        self.repository.save_acceptance_state(
            project_id=project_id,
            expected_architecture_version=architecture.version,
            expected_task_updated_at={
                task.id: task.updated_at.isoformat()
                for task in tasks
                if task.id in updated_task_ids
            },
            project=project,
            architecture=plan.architecture,
            tasks=[*plan.updated_tasks, *plan.created_tasks],
            proposal=accepted_proposal,
        )
        return accepted_proposal

    def reject_proposal(self, project_id: str, proposal_id: str) -> ArchitectureChangeProposal:
        proposal = self.repository.get_proposal(proposal_id)
        if proposal.project_id != project_id or proposal.status != ProposalStatus.PENDING:
            raise ValueError("proposal is not pending for this project")
        rejected_proposal = proposal.model_copy(
            update={
                "status": ProposalStatus.REJECTED,
                "resolved_at": datetime.now(timezone.utc),
                "resolution_reason": "Human reviewer chose to keep the current architecture.",
            }
        )
        self.repository.save_proposal_decision(
            project_id=project_id,
            proposal=rejected_proposal,
            expected_status=ProposalStatus.PENDING,
        )
        return rejected_proposal
