from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import archbro.backend.api.routes as routes_module
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

    def list_agent_runs(self, project_id: str, limit: int = 100):
        self.get_project(project_id)
        return []

    def list_planner_checkpoints(self, project_id: str, limit: int = 100):
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


def test_full_canvas_cache_reuses_identical_state_and_invalidates_on_change(
    monkeypatch,
) -> None:
    repository = CanvasRepository()
    calls = 0
    original = routes_module.layout_canvas_diagram

    def counted_layout(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(routes_module, "layout_canvas_diagram", counted_layout)
    app = FastAPI()
    app.include_router(build_router(repository, FakeModelProvider()))
    client = TestClient(app)
    path = "/projects/project_canvas_unit/architecture/canvas"

    first = client.get(path, params={"reading_mode": "FULL"})
    second = client.get(path, params={"reading_mode": "FULL"})
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert calls == 1

    repository.architecture = repository.architecture.model_copy(
        update={"summary": "Changed accepted architecture state"}
    )
    changed = client.get(path, params={"reading_mode": "FULL"})
    assert changed.status_code == 200
    assert calls == 2


def test_full_canvas_cache_allows_distinct_keys_to_build_concurrently(monkeypatch) -> None:
    repository = CanvasRepository()
    rendezvous = threading.Barrier(2, timeout=1.0)
    original = routes_module.layout_canvas_diagram

    def synchronized_layout(*args, **kwargs):
        rendezvous.wait()
        return original(*args, **kwargs)

    monkeypatch.setattr(routes_module, "layout_canvas_diagram", synchronized_layout)
    app = FastAPI()
    app.include_router(build_router(repository, FakeModelProvider()))
    path = "/projects/project_canvas_unit/architecture/canvas"

    with TestClient(app) as client, ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(client.get, path, params={"reading_mode": reading_mode})
            for reading_mode in ("FULL", "MAP")
        ]
        responses = [future.result(timeout=3.0) for future in futures]

    assert [response.status_code for response in responses] == [200, 200]


@pytest.mark.parametrize("cancel_waiter", [False, True])
def test_same_key_cache_build_is_shared_and_survives_waiter_cancellation(monkeypatch, cancel_waiter):
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def controlled_threadpool(function, *args, **kwargs):
            nonlocal calls
            if function.__name__ == "build_canvas_projection_payload":
                calls += 1
                started.set()
                await release.wait()
            return function(*args, **kwargs)

        monkeypatch.setattr(routes_module, "run_in_threadpool", controlled_threadpool)
        app = FastAPI()
        app.include_router(build_router(CanvasRepository(), FakeModelProvider()))
        path = "/projects/project_canvas_unit/architecture/canvas"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            first = asyncio.create_task(client.get(path))
            second = asyncio.create_task(client.get(path))
            try:
                await asyncio.wait_for(started.wait(), timeout=2)
                await asyncio.sleep(0)
                assert calls == 1
                if cancel_waiter:
                    first.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await first
            finally:
                release.set()
            response = await asyncio.wait_for(second, timeout=2)
            assert response.status_code == 200
            if not cancel_waiter:
                assert (await first).json() == response.json()
            assert (await client.get(path)).json() == response.json()
            assert calls == 1

    asyncio.run(scenario())


def test_failed_cache_build_is_removed_and_can_be_retried(monkeypatch):
    calls = 0
    original = routes_module.layout_canvas_diagram

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("controlled layout failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(routes_module, "layout_canvas_diagram", fail_once)
    app = FastAPI()
    app.include_router(build_router(CanvasRepository(), FakeModelProvider()))
    with TestClient(app, raise_server_exceptions=False) as client:
        path = "/projects/project_canvas_unit/architecture/canvas"
        assert client.get(path).status_code == 500
        assert client.get(path).status_code == 200
        assert client.get(path).status_code == 200
    assert calls == 2


def test_distinct_canvas_builds_are_admitted_and_same_key_waiters_still_share(monkeypatch):
    async def scenario():
        monkeypatch.setattr(routes_module, "CANVAS_PROJECTION_ACTIVE_BUILD_LIMIT", 1)
        monkeypatch.setattr(routes_module, "CANVAS_PROJECTION_ADMITTED_BUILD_LIMIT", 2)
        started = asyncio.Event()
        release = asyncio.Event()
        build_calls: list[str] = []

        async def controlled_threadpool(function, *args, **kwargs):
            if function.__name__ == "build_canvas_projection_payload":
                build_calls.append(args[4])
                started.set()
                await release.wait()
            return function(*args, **kwargs)

        monkeypatch.setattr(routes_module, "run_in_threadpool", controlled_threadpool)
        app = FastAPI()
        app.include_router(build_router(CanvasRepository(), FakeModelProvider()))
        path = "/projects/project_canvas_unit/architecture/canvas"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            full = asyncio.create_task(client.get(path, params={"reading_mode": "FULL"}))
            await asyncio.wait_for(started.wait(), timeout=1)
            map_request = asyncio.create_task(client.get(path, params={"reading_mode": "MAP"}))
            await asyncio.sleep(0.05)
            same_full = asyncio.create_task(client.get(path, params={"reading_mode": "FULL"}))
            read = await asyncio.wait_for(client.get(path, params={"reading_mode": "READ"}), timeout=0.5)
            assert read.status_code == 503
            assert read.json()["detail"] == {
                "code": "canvas_projection_busy",
                "active_limit": 1,
                "admitted_limit": 2,
            }
            release.set()
            full_response, map_response, same_full_response = await asyncio.gather(full, map_request, same_full)
            assert full_response.status_code == map_response.status_code == same_full_response.status_code == 200
            assert full_response.json() == same_full_response.json()
            assert build_calls.count("FULL") == 1
            assert build_calls.count("MAP") == 1

    asyncio.run(scenario())


def test_static_complexity_422_precedes_busy_503_without_consuming_admission(monkeypatch):
    async def scenario():
        monkeypatch.setattr(routes_module, "CANVAS_PROJECTION_ACTIVE_BUILD_LIMIT", 1)
        monkeypatch.setattr(routes_module, "CANVAS_PROJECTION_ADMITTED_BUILD_LIMIT", 1)
        started = asyncio.Event()
        release = asyncio.Event()
        build_calls: list[str] = []
        restrictive_budget = False

        def budget_factory():
            return CanvasWorkBudget(max_edges=0) if restrictive_budget else CanvasWorkBudget()

        async def controlled_threadpool(function, *args, **kwargs):
            if function.__name__ == "build_canvas_projection_payload":
                build_calls.append(args[4])
                started.set()
                await release.wait()
            return function(*args, **kwargs)

        monkeypatch.setattr(routes_module, "CanvasWorkBudget", budget_factory)
        monkeypatch.setattr(routes_module, "run_in_threadpool", controlled_threadpool)
        app = FastAPI()
        app.include_router(build_router(CanvasRepository(), FakeModelProvider()))
        path = "/projects/project_canvas_unit/architecture/canvas"

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            full = asyncio.create_task(client.get(path, params={"reading_mode": "FULL"}))
            await asyncio.wait_for(started.wait(), timeout=1)

            restrictive_budget = True
            invalid = await asyncio.wait_for(client.get(path, params={"reading_mode": "MAP"}), timeout=0.5)
            assert invalid.status_code == 422
            assert invalid.json()["detail"] == {
                "code": "canvas_complexity_exceeded",
                "dimension": "edges",
                "observed": 1,
                "limit": 0,
            }
            assert build_calls == ["FULL"]

            restrictive_budget = False
            release.set()
            assert (await asyncio.wait_for(full, timeout=2)).status_code == 200

    asyncio.run(scenario())


def test_workspace_bootstrap_returns_one_read_only_first_paint_contract() -> None:
    client = make_client()
    response = client.get(
        "/projects/project_canvas_unit/workspace-bootstrap",
        params={"reading_mode": "FULL"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["schema"] == "archbro.workspace-bootstrap.v2"
    assert body["project"]["id"] == "project_canvas_unit"
    assert body["architecture"]["version"] == 4
    assert body["tasks"] == []
    assert body["proposals"] == []
    assert body["activity"] == []
    assert body["resources"]["canvas"] == {
        "status": "DEFERRED",
        "href": "/projects/project_canvas_unit/architecture/canvas?expected_architecture_version=4&reading_mode=FULL",
    }
    assert body["resources"]["project_diagram"] == {
        "status": "DEFERRED",
        "href": "/projects/project_canvas_unit/architecture/diagram?expected_architecture_version=4&reading_mode=MAP",
    }
    assert body["built_in_model_called"] is False
    direct_project_view = client.get(
        "/projects/project_canvas_unit/architecture/diagram",
        params={"reading_mode": "MAP", "expected_architecture_version": 4},
    )
    direct_canvas = client.get(
        "/projects/project_canvas_unit/architecture/canvas",
        params={"reading_mode": "FULL", "expected_architecture_version": 4},
    )
    assert direct_project_view.status_code == direct_canvas.status_code == 200
    assert direct_project_view.json()["architecture_version"] == body["architecture"]["version"]
    assert direct_canvas.json()["architecture_version"] == body["architecture"]["version"]


def test_workspace_bootstrap_is_not_blocked_by_saturated_canvas_projection(monkeypatch):
    async def scenario():
        monkeypatch.setattr(routes_module, "CANVAS_PROJECTION_ACTIVE_BUILD_LIMIT", 1)
        monkeypatch.setattr(routes_module, "CANVAS_PROJECTION_ADMITTED_BUILD_LIMIT", 1)
        started = asyncio.Event()
        release = asyncio.Event()

        async def controlled_threadpool(function, *args, **kwargs):
            if function.__name__ == "build_canvas_projection_payload":
                started.set()
                await release.wait()
            return function(*args, **kwargs)

        monkeypatch.setattr(routes_module, "run_in_threadpool", controlled_threadpool)
        app = FastAPI()
        app.include_router(build_router(CanvasRepository(), FakeModelProvider()))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            canvas = asyncio.create_task(
                client.get("/projects/project_canvas_unit/architecture/canvas", params={"reading_mode": "FULL"})
            )
            await asyncio.wait_for(started.wait(), timeout=1)
            bootstrap = await asyncio.wait_for(
                client.get("/projects/project_canvas_unit/workspace-bootstrap", params={"reading_mode": "FULL"}),
                timeout=0.5,
            )
            assert bootstrap.status_code == 200
            assert bootstrap.json()["schema"] == "archbro.workspace-bootstrap.v2"
            release.set()
            assert (await canvas).status_code == 200

    asyncio.run(scenario())


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


@pytest.mark.parametrize("surface", ["architecture/diagram", "architecture/canvas"])
def test_projection_stale_version_is_rejected_after_cache_warmup(surface):
    client = make_client()
    prefix = "/projects/project_canvas_unit"
    assert client.get(prefix + "/workspace-bootstrap").status_code == 200
    response = client.get(prefix + "/" + surface, params={"expected_architecture_version": 0})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "stale_architecture_version"


@pytest.mark.parametrize("surface", ["architecture/diagram", "architecture/canvas", "workspace-bootstrap"])
def test_projection_cache_never_substitutes_for_current_authorization(surface):
    repository = CanvasRepository()
    app = FastAPI()
    app.include_router(build_router(repository, FakeModelProvider()))
    client = TestClient(app)
    prefix = "/projects/project_canvas_unit"
    assert client.get(prefix + "/workspace-bootstrap").status_code == 200
    repository.project = repository.project.model_copy(update={"owner_user_id": "different-owner"})
    response = client.get(prefix + "/" + surface)
    assert response.status_code == 404
    assert response.json() == {"detail": "project not found"}
