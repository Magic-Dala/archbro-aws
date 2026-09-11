import pytest
from fastapi.testclient import TestClient

from archbro.platform.runtime.app import build_app
from archbro.backend.llm.fake import FakeModelProvider
from archbro.platform.persistence.postgres import PostgresProjectRepository
from conftest import requires_database

pytestmark = requires_database


def make_client(dsn):
    repo = PostgresProjectRepository(dsn)
    return repo, TestClient(build_app(repo, FakeModelProvider()))


def test_goal_is_required_before_project_creation(dsn):
    _, client = make_client(dsn)
    response = client.post("/projects", json={"name": "Issue Tracker", "goal": "   ", "description": ""})
    assert response.status_code == 422


def test_ask_conversation_drafts_goal_before_project_creation(dsn):
    _, client = make_client(dsn)

    first = client.post("/onboarding/goal", json={
        "messages": [
            {"role": "user", "content": "I want to build an issue tracker."},
        ],
        "current_goal": "",
    })
    assert first.status_code == 200
    assert first.json()["ready"] is False
    assert first.json()["goal"]
    assert first.json()["missing_information"]

    second = client.post("/onboarding/goal", json={
        "messages": [
            {"role": "user", "content": "I want to build an issue tracker."},
            {"role": "assistant", "content": first.json()["assistant_message"]},
            {
                "role": "user",
                "content": (
                    "The first milestone lets a small engineering team create, view, and update issues. "
                    "Use React for the frontend, FastAPI for the backend, and PostgreSQL for persistence."
                ),
            },
        ],
        "current_goal": first.json()["goal"],
    })
    assert second.status_code == 200
    body = second.json()
    assert body["ready"] is True
    assert body["suggested_project_name"] == "Issue Tracker"
    assert "PostgreSQL" in body["goal"]

    created = client.post("/projects", json={
        "name": body["suggested_project_name"],
        "goal": body["goal"],
        "description": "Goal drafted through Ask conversation.",
    })
    assert created.status_code == 200
    assert created.json()["architecture_version"] == 0


def test_manual_goal_can_be_used_without_ask(dsn):
    _, client = make_client(dsn)
    goal = (
        "Build an issue tracker for a small engineering team. Users can create, view, and update issues. "
        "Use React, FastAPI, and PostgreSQL for the first local milestone."
    )
    reviewed = client.post("/onboarding/goal", json={"messages": [], "current_goal": goal})
    assert reviewed.status_code == 200
    assert reviewed.json()["goal"] == goal
    assert reviewed.json()["ready"] is True

    created = client.post("/projects", json={"name": "Issue Tracker", "goal": goal, "description": "Manual Goal."})
    assert created.status_code == 200
    assert created.json()["goal"] == goal


def test_project_list_edit_select_contract_and_goal_boundary(dsn):
    repo, client = make_client(dsn)
    first = client.post("/projects", json={
        "name": "First Project",
        "goal": "Build the first project with React and FastAPI.",
        "description": "Initial description",
    }).json()
    second = client.post("/projects", json={
        "name": "Second Project",
        "goal": "Build the second project.",
        "description": "",
    }).json()

    listed = client.get("/projects")
    assert listed.status_code == 200
    assert [project["id"] for project in listed.json()] == [second["id"], first["id"]]

    edited = client.patch(f"/projects/{first['id']}", json={
        "name": "Renamed Project",
        "goal": "Build the first project with React, FastAPI, and PostgreSQL.",
        "description": "Edited before Architecture v1",
    })
    assert edited.status_code == 200
    assert edited.json()["name"] == "Renamed Project"
    assert "PostgreSQL" in edited.json()["goal"]

    bootstrap = client.post(f"/projects/{first['id']}/events", json={
        "type": "USER_MESSAGE",
        "source": "FRONTEND",
        "payload": {"intent": "INITIAL_ARCHITECTURE"},
    })
    assert bootstrap.status_code == 200
    assert bootstrap.json()["result"] == "SUCCESS"

    metadata_edit = client.patch(f"/projects/{first['id']}", json={
        "name": "Renamed Again",
        "description": "Metadata remains directly editable",
    })
    assert metadata_edit.status_code == 200
    assert metadata_edit.json()["name"] == "Renamed Again"

    blocked_goal_edit = client.patch(f"/projects/{first['id']}", json={
        "goal": "Replace PostgreSQL with Firestore directly.",
    })
    assert blocked_goal_edit.status_code == 409
    assert "must go through the Agent" in blocked_goal_edit.json()["detail"]
    assert "PostgreSQL" in repo.get_project(first["id"]).goal


def test_project_delete_removes_project_owned_state(dsn):
    repo, client = make_client(dsn)
    created = client.post("/projects", json={
        "name": "Delete Me",
        "goal": "Build an issue tracker using React, FastAPI, and PostgreSQL.",
        "description": "Disposable project",
    }).json()
    project_id = created["id"]
    bootstrap = client.post(f"/projects/{project_id}/events", json={
        "type": "USER_MESSAGE",
        "source": "FRONTEND",
        "payload": {"intent": "INITIAL_ARCHITECTURE"},
    })
    assert bootstrap.json()["result"] == "SUCCESS"
    repo.add_note(project_id, "Disposable note")
    assert repo.list_tasks(project_id)

    deleted = client.delete(f"/projects/{project_id}")
    assert deleted.status_code == 204
    assert client.get(f"/projects/{project_id}").status_code == 404
    assert client.get("/projects").json() == []
    assert repo.list_tasks(project_id) == []
    assert repo.list_proposals(project_id) == []
    assert repo.list_notes(project_id) == []
    with pytest.raises(KeyError):
        repo.get_project(project_id)


def test_planner_recovery_api_requires_exact_attempt_revision_and_explicit_action(dsn):
    repo, client = make_client(dsn)
    project = client.post(
        "/projects",
        json={"name": "Planner Recovery API", "goal": "Recover a staged planner safely."},
    ).json()
    checkpoint = repo.put_planner_checkpoint(
        project_id=project["id"],
        plan_id="plan-api-recovery",
        phase_key="SYSTEM_MAP",
        data={
            "schema": "archbro.initial_planner_phase.v1",
            "plan_id": "plan-api-recovery",
            "project_id": project["id"],
            "phase_key": "SYSTEM_MAP",
            "attempt_id": "attempt-api",
            "delivery_stage": "PREPARED",
            "status": "STARTED",
        },
    )

    inspected = client.get(
        f"/projects/{project['id']}/planner/checkpoints/plan-api-recovery/SYSTEM_MAP"
    )
    assert inspected.status_code == 200
    assert inspected.json()["attempt_id"] == "attempt-api"

    bootstrap = client.get(f"/projects/{project['id']}/workspace-bootstrap")
    assert bootstrap.status_code == 200
    assert bootstrap.json()["latest_agent_run"] is None
    assert bootstrap.json()["planner_recovery"] == {
        "plan_id": "plan-api-recovery",
        "phase_key": "SYSTEM_MAP",
        "attempt_id": "attempt-api",
        "revision": checkpoint["revision"],
        "status": "STARTED",
        "delivery_stage": "PREPARED",
        "action": "RECLAIM_PREPARED",
        "requires_paid_call_confirmation": False,
    }

    recovered = client.post(
        f"/projects/{project['id']}/planner/checkpoints/plan-api-recovery/SYSTEM_MAP/recover",
        json={
            "expected_attempt_id": "attempt-api",
            "expected_revision": checkpoint["revision"],
            "action": "RECLAIM_PREPARED",
            "request_id": "request-api-recovery",
        },
    )
    assert recovered.status_code == 200
    assert recovered.json()["status"] == "RETRYABLE"
    retryable_bootstrap = client.get(f"/projects/{project['id']}/workspace-bootstrap")
    assert retryable_bootstrap.json()["planner_recovery"]["action"] == "RETRY_EVENT"
    assert retryable_bootstrap.json()["planner_recovery"]["revision"] == recovered.json()["revision"]

    stale = client.post(
        f"/projects/{project['id']}/planner/checkpoints/plan-api-recovery/SYSTEM_MAP/recover",
        json={
            "expected_attempt_id": "attempt-api",
            "expected_revision": checkpoint["revision"],
            "action": "RECLAIM_PREPARED",
            "request_id": "different-request",
        },
    )
    assert stale.status_code == 409
    assert "revision changed" in stale.json()["detail"]


def test_workspace_bootstrap_requires_explicit_paid_call_authorization_for_unknown_planner(dsn):
    repo, client = make_client(dsn)
    project = client.post(
        "/projects",
        json={"name": "Unknown Planner", "goal": "Recover an ambiguous model dispatch safely."},
    ).json()
    checkpoint = repo.put_planner_checkpoint(
        project_id=project["id"],
        plan_id="plan-api-unknown",
        phase_key="SYSTEM_MAP",
        data={
            "schema": "archbro.initial_planner_phase.v1",
            "plan_id": "plan-api-unknown",
            "project_id": project["id"],
            "phase_key": "SYSTEM_MAP",
            "attempt_id": "attempt-unknown",
            "delivery_stage": "IN_FLIGHT",
            "status": "UNKNOWN",
            "provider": {
                "requested_model": "gemini-3.8-flash",
                "http_timeout_ms": 90_000,
                "raw_model_output": "must never cross the bootstrap boundary",
            },
        },
    )

    response = client.get(f"/projects/{project['id']}/workspace-bootstrap")
    assert response.status_code == 200
    recovery = response.json()["planner_recovery"]
    assert recovery == {
        "plan_id": "plan-api-unknown",
        "phase_key": "SYSTEM_MAP",
        "attempt_id": "attempt-unknown",
        "revision": checkpoint["revision"],
        "status": "UNKNOWN",
        "delivery_stage": "IN_FLIGHT",
        "action": "AUTHORIZE_NEW_ATTEMPT",
        "requires_paid_call_confirmation": True,
    }
    assert "raw_model_output" not in response.text
    assert "must never cross" not in response.text

    initialized = repo.get_architecture(project["id"]).model_copy(update={"version": 1})
    repo.save_architecture(project["id"], initialized)
    initialized_project = repo.get_project(project["id"]).model_copy(
        update={"architecture_version": 1}
    )
    repo.save_project(initialized_project)
    initialized_bootstrap = client.get(f"/projects/{project['id']}/workspace-bootstrap")
    assert initialized_bootstrap.status_code == 200
    assert initialized_bootstrap.json()["planner_recovery"] is None


def test_ask_merges_with_current_goal_instead_of_replacing_it(dsn):
    _, client = make_client(dsn)
    current_goal = (
        "Build an issue tracker for a small engineering team. "
        "Users can create and view issues. Use React for the frontend, FastAPI for the backend, "
        "and PostgreSQL for persistence. The first milestone runs locally."
    )
    response = client.post("/onboarding/goal", json={
        "current_goal": current_goal,
        "messages": [
            {"role": "user", "content": "Also let users export the issue list to CSV."},
        ],
    })
    assert response.status_code == 200
    merged = response.json()["goal"]
    assert "PostgreSQL" in merged
    assert "FastAPI" in merged
    assert "React" in merged
    assert "CSV" in merged
    assert len(merged) > len("Also let users export the issue list to CSV.")


def test_initial_architecture_requires_explicit_goal_bootstrap(dsn):
    _, client = make_client(dsn)
    created = client.post("/projects", json={
        "name": "Issue Tracker",
        "goal": "Build an issue tracker using React, FastAPI, and PostgreSQL.",
        "description": "Users can create, view, and update issues.",
    })
    project_id = created.json()["id"]

    accidental = client.post(f"/projects/{project_id}/events", json={
        "type": "USER_MESSAGE",
        "source": "FRONTEND",
        "payload": {"message": "The backend skeleton is done."},
    })
    assert accidental.status_code == 200
    assert accidental.json()["result"] == "ERROR"
    assert "Generate initial architecture" in accidental.json()["error"]
    assert client.get(f"/projects/{project_id}/architecture").json()["version"] == 0
    assert client.get(f"/projects/{project_id}/tasks").json() == []

    bootstrap = client.post(f"/projects/{project_id}/events", json={
        "type": "USER_MESSAGE",
        "source": "FRONTEND",
        "payload": {"intent": "INITIAL_ARCHITECTURE", "message": "Ignore this temporary text."},
    })
    assert bootstrap.status_code == 200
    assert bootstrap.json()["result"] == "SUCCESS"
    architecture = client.get(f"/projects/{project_id}/architecture").json()
    assert architecture["version"] == 1
    assert any(component.get("children") for component in architecture["components"])


def test_minimal_api_contract_end_to_end(dsn):
    _, client = make_client(dsn)

    created = client.post("/projects", json={
        "name": "Archbro",
        "goal": "Build a collaborative project-management app with a React frontend, FastAPI backend, and PostgreSQL database where an AI agent maintains architecture and actionable human tasks.",
        "description": "V0 walking skeleton",
    })
    assert created.status_code == 200
    project_id = created.json()["id"]

    first = client.post(f"/projects/{project_id}/events", json={
        "type": "USER_MESSAGE",
        "source": "HUMAN",
        "payload": {"intent": "INITIAL_ARCHITECTURE"},
    })
    assert first.status_code == 200
    assert first.json()["result"] == "SUCCESS"

    architecture = client.get(f"/projects/{project_id}/architecture")
    assert architecture.status_code == 200
    assert architecture.json()["version"] == 1
    assert any(c["name"] == "PostgreSQL" for c in architecture.json()["components"])

    tasks = client.get(f"/projects/{project_id}/tasks").json()
    backend = next(t for t in tasks if t["related_component"] == "backend")
    updated = client.post(f"/projects/{project_id}/events", json={
        "type": "TASK_UPDATED",
        "source": "HUMAN",
        "payload": {"task_id": backend["id"], "status": "DONE", "message": "Backend API skeleton completed."},
    })
    assert updated.status_code == 200
    assert client.get(f"/projects/{project_id}/architecture").json()["version"] == 1

    change = client.post(f"/projects/{project_id}/events", json={
        "type": "USER_MESSAGE",
        "source": "HUMAN",
        "payload": {"message": "We decided to replace PostgreSQL with Firestore."},
    })
    assert change.status_code == 200
    body = change.json()
    assert body["architecture_review_required"] is True
    proposal_id = body["proposal_ids"][0]

    pending_arch = client.get(f"/projects/{project_id}/architecture").json()
    assert pending_arch["version"] == 1
    assert any(c["name"] == "PostgreSQL" for c in pending_arch["components"])
    proposal = next(p for p in client.get(f"/projects/{project_id}/architecture/proposals").json() if p["id"] == proposal_id)
    assert proposal["status"] == "PENDING"

    preview = client.get(
        f"/projects/{project_id}/architecture/proposals/{proposal_id}/acceptance-preview"
    )
    assert preview.status_code == 200, preview.text
    preview_body = preview.json()
    assert preview_body["actionable"] is True
    assert preview_body["current_architecture_version"] == 1
    assert preview_body["resulting_architecture_version"] == 2
    assert preview_body["components_before"] == pending_arch["components"]
    assert client.get(f"/projects/{project_id}/architecture").json() == pending_arch
    assert next(
        p for p in client.get(f"/projects/{project_id}/architecture/proposals").json()
        if p["id"] == proposal_id
    )["status"] == "PENDING"

    accepted = client.post(f"/projects/{project_id}/architecture/proposals/{proposal_id}/accept")
    assert accepted.status_code == 200
    final_arch = client.get(f"/projects/{project_id}/architecture").json()
    assert final_arch["version"] == 2
    assert any(c["name"] == "Firestore" for c in final_arch["components"])
    assert final_arch["components"] == preview_body["components_after"]

    reconciled_tasks = client.get(f"/projects/{project_id}/tasks").json()
    database_task = next(t for t in reconciled_tasks if t["related_component"] == "database" and t["title"] == "Prepare PostgreSQL persistence")
    assert database_task["status"] == "BLOCKED"
    assert "PostgreSQL to Firestore" in database_task["description"]
    migration_task = next(t for t in reconciled_tasks if t["title"] == "Migrate PostgreSQL to Firestore")
    assert migration_task["status"] == "TODO"
    assert migration_task["owner"] == "HUMAN"
    assert migration_task["source"] == "ARCHITECTURE"
    assert migration_task["related_component"] == "database"
    completed_backend = next(t for t in reconciled_tasks if t["id"] == backend["id"])
    assert completed_backend["status"] == "DONE"


def test_web_surface_is_served_from_same_app(dsn):
    repo = PostgresProjectRepository(dsn)
    client = TestClient(build_app(repo, FakeModelProvider()))

    page = client.get("/")
    assert page.status_code == 200
    assert "Archbro" in page.text
    assert "Living Graph" in page.text
    assert "Needs You" in page.text
    assert "Write a goal or describe your project." in page.text
    assert "Ask the Agent" in page.text
    assert "GOAL DRAFT" in page.text
    assert "Nothing is persisted until you confirm." in page.text
    assert "Ask updates the Goal Draft without replacing it." in page.text
    assert "Use this goal &amp; generate architecture" in page.text
    assert "Human Start/Done clicks are authoritative task state" in page.text
    # The signed-in shell exposes project navigation and approvals as
    # direct, accessible destinations rather than a global project selector.
    assert 'id="projectTree"' in page.text
    assert 'id="notificationBtn"' in page.text
    assert 'id="notificationMenu"' in page.text
    assert 'id="workspaceTabs"' in page.text
    assert 'id="workspaceTabReviewPanel"' in page.text
    assert 'id="editProjectBtn"' not in page.text
    assert 'id="deleteProjectBtn"' not in page.text
    assert 'id="editProjectDialog"' in page.text
    assert 'id="deleteProjectDialog"' in page.text

    css = client.get("/static/styles.css")
    js = client.get("/static/app.js")
    assert css.status_code == 200
    assert js.status_code == 200
    assert "/onboarding/goal" in js.text
    assert "current_goal" in js.text
    assert "confirmGoalAndGenerate" in js.text
    assert "INITIAL_ARCHITECTURE" in js.text
    assert "loadProjects" in js.text
    assert "selectProject" in js.text
    assert "saveProjectEdits" in js.text
    assert "data-project-menu" in js.text
    assert "Edit project" in js.text
    assert "Rename project" in js.text
    assert "Delete project" in js.text
    # Overview uses one explicit Architecture action instead of making the
    # whole section a nested persistent link. This keeps navigation ownership
    # singular and prevents repeated render wiring from stacking history writes.
    assert 'id="architectureSummaryButton"' in page.text
    assert 'data-go="architecture"' in page.text
    assert 'data-go-card="architecture"' not in page.text
    assert "overview-architecture" in page.text
    assert 'id="overviewArchitectureMap"' in page.text
    assert "document.querySelectorAll('[data-go-card]')" in js.text
    assert "if (name === 'architecture') renderGraph();" in js.text
    assert "deleteCurrentProject" in js.text
