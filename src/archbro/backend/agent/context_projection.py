from __future__ import annotations

from collections import Counter
from typing import Any

from archbro.backend.core.contracts import ProposalStatus, TaskStatus
from archbro.backend.core.repository import ProjectRepositoryPort


def _one_line(value: object, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _goal_lines(value: object, limit: int = 600) -> list[str]:
    """Bound the projection without collapsing canonical paragraphs or lists."""
    text = str(value or "")
    bounded = text[:limit]
    return [
        "- goal: |",
        *("  " + line for line in bounded.split("\n")),
        f"- goal_truncated: {str(len(text) > limit).lower()}",
    ]


def build_agent_context(
    repository: ProjectRepositoryPort,
    project_id: str,
    *,
    connected_sources: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Project the canonical state into a bounded, agent-oriented Markdown map."""

    project = repository.get_project(project_id)
    architecture = repository.get_architecture(project_id)
    tasks = repository.list_tasks(project_id)
    proposals = repository.list_proposals(project_id)
    pending = [proposal for proposal in proposals if proposal.status == ProposalStatus.PENDING]
    sources = connected_sources or []

    counts = Counter(task.status.value for task in tasks)
    focus = [
        task
        for status in (TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED, TaskStatus.TODO)
        for task in tasks
        if task.status == status
    ][:8]

    lines = [
        "# ARCHBRO_AGENT_CONTEXT v1",
        "",
        "## Project",
        f"- id: {project.id}",
        f"- name: {_one_line(project.name, 120)}",
        f"- status: {project.status.value}",
        *_goal_lines(project.goal),
        "",
        "## Architecture",
        f"- accepted_version: {architecture.version}",
        f"- summary: {_one_line(architecture.summary, 400) or '(none yet)'}",
        f"- pending_reviews: {len(pending)}",
    ]
    if architecture.components:
        lines.append("- top_level_components:")
        for component in architecture.components[:8]:
            lines.append(
                f"  - {component.id}: {_one_line(component.name, 100)} ({component.kind.value})"
            )

    lines.extend(
        [
            "",
            "## Execution",
            (
                "- counts: "
                f"TODO={counts.get('TODO', 0)} "
                f"IN_PROGRESS={counts.get('IN_PROGRESS', 0)} "
                f"BLOCKED={counts.get('BLOCKED', 0)} "
                f"DONE={counts.get('DONE', 0)}"
            ),
        ]
    )
    if focus:
        lines.append("- current_focus:")
        for task in focus:
            lines.append(
                "  - "
                f"{task.id} [{task.status.value}] {_one_line(task.title, 160)}"
                + (f" -> {task.related_component}" if task.related_component else "")
            )
    else:
        lines.append("- current_focus: none")

    if pending:
        lines.extend(["", "## Pending Human Review"])
        for proposal in pending[:3]:
            lines.append(f"- {proposal.id}: {_one_line(proposal.reason, 240)}")

    lines.extend(["", "## External Sources"])
    if sources:
        for source in sources[:12]:
            lines.append(
                f"- {source.get('id')}: {_one_line(source.get('name'), 100)}"
                f" — {_one_line(source.get('description'), 180) or 'connected MCP source'}"
            )
    else:
        lines.append("- none configured")

    lines.extend(
        [
            "",
            "## Routing",
            "- L0 MAP -> use this bounded projection first; canonical project state before external reads",
            "- L1 SELECTIVE -> read only the needed architecture node/path/decision or current task",
            "- L2 EVIDENCE -> code truth or connected MCP only when L1 cannot answer the evidence gap",
            "- reuse existing evidence before gathering new evidence",
            "- implementation progress -> Task/Event, not Architecture",
            "- material conflict with accepted architecture -> Proposal -> Human Review",
            "",
            "## Rules",
            "- ArchBro canonical state is the source of truth; this Markdown is a projection only.",
            "- External MCP output is evidence, not canonical project state.",
            "- Use the smallest sufficient read or action; do not repeat a read unless a new evidence gap appears.",
            "- For non-trivial changes, run one focused check that can falsify the change; broaden only if it exposes a new boundary.",
            "- Stop when the requested outcome and focused acceptance are proven.",
            "- Never silently replace accepted architecture.",
            "- Human approval remains authoritative for material architecture changes.",
        ]
    )

    return {
        "version": "1",
        "format": "markdown",
        "project_id": project.id,
        "architecture_version": architecture.version,
        "task_counts": {
            "todo": counts.get("TODO", 0),
            "in_progress": counts.get("IN_PROGRESS", 0),
            "blocked": counts.get("BLOCKED", 0),
            "done": counts.get("DONE", 0),
        },
        "pending_review_count": len(pending),
        "connected_source_count": len(sources),
        "context_protocol": "L0_MAP_L1_SELECTIVE_L2_EVIDENCE",
        "content": "\n".join(lines),
    }
