from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from archbro.backend.api.routes import build_router
from archbro.backend.core.canvas_budget import CanvasWorkBudget
from archbro.backend.core.contracts import Architecture, Component, Project, Relationship
from archbro.backend.llm.fake import FakeModelProvider


class CanvasRepository:
    def __init__(self) -> None:
        self.project = Project(
            id="project_canvas_unit",
            name="Canvas Unit",
            goal="Verify the complete Living Architecture canvas route without a database.",
            architecture_version=4,
            owner_user_id="local-demo",
        )
        self.architecture = Architecture(
            version=4,
            summary="Complete hierarchy fixture",
            components=[
                Component(
                    id="experience",
                    name="Experience",
                    type="domain",
                    responsibility="Own user experience",
                    children=[
                        Component(
                            id="viewer",
                            name="Viewer",
                            type="ui",
                            responsibility="Render playback",
                        )
                    ],
                ),
                Component(
                    id="platform",
                    name="Platform",
                    type="domain",
                    responsibility="Own platform services",
                    children=[
                        Component(
                            id="catalog",
                            name="Catalog",
                            type="service",
                            responsibility="Own video metadata",
                        )
                    ],
                ),
            ],
            relationships=[
                Relationship(
                    source="viewer",
                    target="catalog",
                    relationship_type="HTTPS",
                    description="Read video metadata",
                )
            ],
        )

    def get_project(self, project_id: str) -> Project:
        if project_id != self.project.id:
            raise KeyError(project_id)
        return self.project

    def get_architecture(self, project_id: str) -> Architecture:
        self.get_project(project_id)
        return self.architecture

    def list_tasks(self, project_id: str):
        self.get_project(project_id)
        return []

    def list_proposals(self, project_id: str):
        self.get_project(project_id)
        return []

    def list_events(self, project_id: str, limit: int = 100):
        self.get_project(project_id)
        return []


def make_client() -> TestClient:
    app = FastAPI()
    app.include_router(build_router(CanvasRepository(), FakeModelProvider()))
    return TestClient(app)


def test_full_canvas_route_is_live_and_contains_all_hierarchy_levels() -> None:
    response = make_client().get(
        "/projects/project_canvas_unit/architecture/canvas",
        params={"expected_architecture_version": 4, "reading_mode": "FULL"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["schema"] == "archbro.full_canvas.v1"
    assert {node["component_id"] for node in body["diagram"]["nodes"]} == {
        "experience",
        "viewer",
        "platform",
        "catalog",
    }
    assert len(body["diagram"]["edges"]) == 1
    assert body["diagram"]["edges"][0]["relationship_category"] == "FLOW"
    assert {frame["node_id"] for frame in body["group_frames"]} == {
        "node:experience",
        "node:platform",
    }
    assert body["positioned_graph"]["architecture_version"] == 4
    assert body["positioned_graph"]["layout_version"] == "archbro.canvas-layout.v10"
    assert body["connection_summaries"] == {"schema": "archbro.connection-summaries.v1", "routing_policy": "coverage-shared-trunk.v2", "architecture_version": 4, "groups": []}
    assert len(body["reading_views"]) == 1
    assert body["reading_views"][0]["id"] == "backbone"
    assert body["reading_views"][0]["edge_ids"] == [body["diagram"]["edges"][0]["id"]]


def test_full_canvas_route_fails_closed_on_stale_version() -> None:
    response = make_client().get(
        "/projects/project_canvas_unit/architecture/canvas",
        params={"expected_architecture_version": 3},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "stale_architecture_version",
        "expected_architecture_version": 3,
        "current_architecture_version": 4,
    }


def test_full_canvas_route_returns_typed_complexity_error(monkeypatch) -> None:
    import archbro.backend.api.routes as routes

    monkeypatch.setattr(
        routes,
        "CanvasWorkBudget",
        lambda: CanvasWorkBudget(max_edges=0),
    )
    response = make_client().get(
        "/projects/project_canvas_unit/architecture/canvas",
        params={"expected_architecture_version": 4, "reading_mode": "FULL"},
    )
    assert response.status_code == 422
    assert response.json()["detail"] == {
        "code": "canvas_complexity_exceeded",
        "dimension": "edges",
        "observed": 1,
        "limit": 0,
    }


def test_full_canvas_route_returns_typed_runtime_work_budget_error(monkeypatch) -> None:
    import archbro.backend.api.routes as routes

    monkeypatch.setattr(
        routes,
        "CanvasWorkBudget",
        lambda: CanvasWorkBudget(max_route_expansions=0),
    )
    response = make_client().get(
        "/projects/project_canvas_unit/architecture/canvas",
        params={"expected_architecture_version": 4, "reading_mode": "FULL"},
    )
    assert response.status_code == 422
    assert response.json()["detail"] == {
        "code": "canvas_complexity_exceeded",
        "dimension": "route_expansions",
        "observed": 1,
        "limit": 0,
    }






@pytest.mark.parametrize("reading_mode", ["MAP", "READ", "FULL"])
@pytest.mark.parametrize("scope", [None, "experience", "viewer"])
def test_shared_project_projection_preserves_scope_and_route_identity(reading_mode, scope):
    params = {"reading_mode": reading_mode, "expected_architecture_version": 4}
    if scope is not None:
        params["scope"] = scope
    response = make_client().get(
        "/projects/project_canvas_unit/architecture/diagram", params=params
    )
    assert response.status_code == 200
    body = response.json()
    assert body["scope"]["component_id"] == scope
    assert body["architecture_version"] == 4
    assert {edge["id"] for edge in body["diagram"]["edges"]} == {
        edge["edge_id"] for edge in body["positioned_graph"]["edges"]
    }


def test_shared_project_projection_keeps_unknown_scope_404():
    response = make_client().get(
        "/projects/project_canvas_unit/architecture/diagram",
        params={"scope": "missing-scope"},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == {
        "code": "architecture_node_not_found", "component_id": "missing-scope"
    }
