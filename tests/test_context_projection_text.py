from types import SimpleNamespace

import pytest

from archbro.backend.agent.context_projection import build_agent_context


@pytest.mark.parametrize(
    "goal",
    [
        "First line\nSecond line",
        "First paragraph\n\nSecond paragraph\n",
        "- first\n- second\n  - nested",
        "Very long paragraph " * 80,
        "",
    ],
)
def test_agent_projection_preserves_goal_lines_and_bounds_without_mutating_source(goal):
    project = SimpleNamespace(
        id="project",
        name="Project",
        goal=goal,
        status=SimpleNamespace(value="ACTIVE"),
    )
    architecture = SimpleNamespace(version=1, summary="Accepted", components=[])
    repository = SimpleNamespace(
        get_project=lambda _: project,
        get_architecture=lambda _: architecture,
        list_tasks=lambda _: [],
        list_proposals=lambda _: [],
    )

    for _ in range(2):
        result = build_agent_context(repository, "project")
        block = result["content"].split("- goal: |\n", 1)[1].split("\n- goal_truncated:", 1)[0]
        projected = "\n".join(line[2:] for line in block.split("\n"))
        assert projected == goal[:600]
        assert f"- goal_truncated: {str(len(goal) > 600).lower()}" in result["content"]
        assert project.goal == goal
