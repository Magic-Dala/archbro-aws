import pytest
from pydantic import ValidationError

from archbro.backend.api.routes import InteractiveInitialArchitectureRequest
from archbro.backend.core.diagram import project_diagram, project_scoped_diagram


def initial_plan(root_count=2, relationships=None):
    roots = []
    evaluations = []
    for index in range(root_count):
        root_id, leaf_id = f"system-{index}", f"service-{index}"
        leaf = {"id": leaf_id, "name": leaf_id, "type": "service", "responsibility": "Handle this system's requests"}
        roots.append({"id": root_id, "name": root_id, "type": "system", "responsibility": "Own service boundary", "children": [leaf]})
        evaluations.extend([
            {"scope_component_id": root_id, "decomposition": "EXPANDED", "child_ids": [leaf_id]},
            {"scope_component_id": leaf_id, "decomposition": "JUSTIFIED_LEAF", "child_ids": [], "leaf_reason": "This service owns one atomic request handling boundary."},
        ])
    return {
        "architecture": {"version": 1, "summary": "Services cooperate on one project", "components": roots, "relationships": relationships or []},
        "tasks": [{"title": "Implement request handling", "related_component": "service-0"}],
        "reasoning": "Author service interactions during reconciliation.",
        "planning_trace": {"system_map_root_ids": [root["id"] for root in roots], "scope_evaluations": evaluations, "reconciled": True},
    }


@pytest.mark.parametrize("relationships", [[], [{"source": "service-0", "target": "service-0", "relationship_type": "CALLS"}]])
def test_multiple_modules_cannot_claim_reconciliation_without_interactions(relationships):
    with pytest.raises(ValidationError, match="authored relationships between distinct components"):
        InteractiveInitialArchitectureRequest.model_validate(initial_plan(relationships=relationships))


def test_authored_leaf_interaction_reaches_full_and_root_diagrams():
    request = InteractiveInitialArchitectureRequest.model_validate(initial_plan(relationships=[
        {"source": "service-0", "target": "service-1", "relationship_type": "CALLS"},
    ]))
    full = project_diagram(request.architecture)
    root = project_scoped_diagram(request.architecture)
    assert len(full.edges) == len(root.diagram.edges) == 1
    assert (full.edges[0].source, full.edges[0].target) == ("node:service-0", "node:service-1")
    assert (root.diagram.edges[0].source, root.diagram.edges[0].target) == ("node:system-0", "node:system-1")


def test_single_atomic_service_does_not_need_a_fabricated_relationship():
    request = InteractiveInitialArchitectureRequest.model_validate(initial_plan(root_count=1))
    assert request.architecture.relationships == []
