from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
import threading
from dataclasses import asdict
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import Browser, BrowserContext, Page, Route, sync_playwright

from playwright_diagnostics import diagnostic_scope, failure_details

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from archbro.backend.agent.node_context import find_architecture_path
from archbro.backend.core.contracts import Architecture
from archbro.backend.core.diagram import map_edge_ids, project_diagram, project_scoped_diagram
from archbro.backend.core.diagram_layout import layout_canvas_diagram, layout_diagram


BASE_URL = os.getenv("ARCHBRO_BASE_URL", "http://127.0.0.1:8012/")
ART = Path("qa/playwright_artifacts")
ART.mkdir(parents=True, exist_ok=True)
REPORT_PATH = ART / "final_fix_report.json"
SURFACE_REPORT_PATH = ART / "surface_sweep_report.json"


def project(project_id: str, name: str) -> dict:
    return {
        "id": project_id,
        "name": name,
        "goal": f"Build {name} with reliable human-agent collaboration.",
        "description": "Complete browser fixture for final interaction review.",
        "status": "ACTIVE",
    }


def architecture(project_id: str) -> dict:
    return {
        "project_id": project_id,
        "version": 1,
        "summary": f"Accepted architecture for {project_id}.",
        "components": [{
            "id": f"{project_id}-experience",
            "name": "Workspace Experience",
            "type": "Frontend",
            "kind": "UI",
            "responsibility": "Keep project context visible and operable.",
            "status": "ACCEPTED",
            "children": [{
                "id": f"{project_id}-composer",
                "name": "Agent Composer",
                "type": "Interface",
                "kind": "UI",
                "responsibility": "Submit instructions with selected context.",
                "status": "ACCEPTED",
                "children": [],
            }],
        }],
        "relationships": [],
        "decisions": ["Keep the browser prototype framework-free."],
        "risks": [],
        "assumptions": [],
    }


def canvas_architecture(project_id: str) -> dict:
    return {
        "project_id": project_id,
        "version": 7,
        "summary": "Complete deterministic architecture-canvas interaction fixture.",
        "components": [
            {
                "id": f"{project_id}-core",
                "name": "Core Platform",
                "type": "platform",
                "kind": "SYSTEM",
                "responsibility": "Own application processing.",
                "status": "ACCEPTED",
                "children": [
                    {
                        "id": f"{project_id}-api",
                        "name": "API",
                        "type": "service",
                        "kind": "SERVICE",
                        "responsibility": "Serve canonical requests.",
                        "status": "ACCEPTED",
                        "children": [
                            {
                                "id": f"{project_id}-validator",
                                "name": "Validator",
                                "type": "service",
                                "kind": "SERVICE",
                                "responsibility": "Validate request contracts.",
                                "status": "ACCEPTED",
                                "children": [],
                            }
                        ],
                    },
                    {
                        "id": f"{project_id}-worker",
                        "name": "Worker",
                        "type": "service",
                        "kind": "SERVICE",
                        "responsibility": "Run unrelated background work.",
                        "status": "ACCEPTED",
                        "children": [],
                    },
                ],
            },
            {
                "id": f"{project_id}-data",
                "name": "Data Plane",
                "type": "data_plane",
                "kind": "SYSTEM",
                "responsibility": "Own durable data.",
                "status": "ACCEPTED",
                "children": [
                    {
                        "id": f"{project_id}-catalog",
                        "name": "Catalog",
                        "type": "database",
                        "kind": "DATA_STORE",
                        "responsibility": "Persist validated records.",
                        "status": "ACCEPTED",
                        "children": [],
                    }
                ],
            },
            {
                "id": f"{project_id}-experience",
                "name": "Experience",
                "type": "experience",
                "kind": "SYSTEM",
                "responsibility": "Own an unrelated user surface.",
                "status": "ACCEPTED",
                "children": [
                    {
                        "id": f"{project_id}-viewer",
                        "name": "Viewer",
                        "type": "ui",
                        "kind": "UI",
                        "responsibility": "Render the user experience.",
                        "status": "ACCEPTED",
                        "children": [],
                    }
                ],
            },
        ],
        "relationships": [
            {
                "source": f"{project_id}-validator",
                "target": f"{project_id}-catalog",
                "relationship_type": "SQL",
                "description": "Persist validated records.",
            },
            {
                "source": f"{project_id}-viewer",
                "target": f"{project_id}-worker",
                "relationship_type": "HTTPS",
                "description": "Submit unrelated background work.",
            },
        ],
        "decisions": ["Keep canonical layout owned by the backend."],
        "risks": [],
        "assumptions": [],
    }


def task(project_id: str, task_id: str, title: str, status: str = "BLOCKED") -> dict:
    return {
        "id": task_id,
        "project_id": project_id,
        "title": title,
        "description": f"{title} with complete project context.",
        "owner": "HUMAN",
        "source": "ARCHITECTURE",
        "status": status,
        "related_component": f"{project_id}-composer",
    }


def proposal(project_id: str, proposal_id: str) -> dict:
    return {
        "id": proposal_id,
        "project_id": project_id,
        "status": "PENDING",
        "base_architecture_version": 1,
        "reason": "Review the agent boundary",
        "observed_change": "A new external provider was requested.",
        "evidence": ["The project goal now names an external provider."],
        "impact": "The accepted architecture boundary would change.",
        "affected_components": [f"{project_id}-experience"],
        "proposed_changes": [{"component_id": f"{project_id}-experience"}],
        "recommended_option": "ACCEPT_PROPOSED_CHANGE",
    }


class FakeBackend:
    def __init__(self, projects: list[dict] | None = None):
        self.projects = copy.deepcopy(projects or [])
        self.contexts = {
            item["id"]: {
                "project": copy.deepcopy(item),
                "tasks": [],
                "architecture": architecture(item["id"]),
                "proposals": [],
            }
            for item in self.projects
        }
        self.fail_once: dict[tuple[str, str], int] = {}
        self.event_requests: list[dict] = []
        self.requests: list[dict[str, str]] = []
        self.context_manifest_requests: list[dict] = []
        self.event_result = "SUCCESS"
        self.decision_requests: list[dict[str, str]] = []
        self.drop_next_decision_response = False
        self.fail_decision_readback = False

    def fail_next(self, method: str, path: str) -> None:
        self.fail_once[(method, path)] = self.fail_once.get((method, path), 0) + 1

    def json(self, route: Route, payload, status: int = 200) -> None:
        route.fulfill(status=status, content_type="application/json", body=json.dumps(payload))

    def scoped_diagram_payload(self, project_id: str, scope: str | None = None) -> dict:
        context = self.contexts[project_id]
        model = Architecture.model_validate(context["architecture"])
        projection = project_scoped_diagram(model, scope_component_id=scope)
        return {
            "schema": projection.schema,
            "project_id": project_id,
            "architecture_version": projection.architecture_version,
            "scope": projection.scope.model_dump(mode="json"),
            "diagram": projection.diagram.model_dump(mode="json"),
            "positioned_graph": asdict(layout_diagram(projection.diagram)),
        }

    def full_canvas_payload(self, project_id: str, reading_mode: str = "FULL") -> dict:
        context = self.contexts[project_id]
        model = Architecture.model_validate(context["architecture"])
        full_diagram = project_diagram(model)
        route_edge_ids = map_edge_ids(full_diagram) if reading_mode == "MAP" else None
        canvas_layout = layout_canvas_diagram(full_diagram, route_edge_ids=route_edge_ids)
        diagram = (
            full_diagram.model_copy(
                update={"edges": [edge for edge in full_diagram.edges if edge.id in route_edge_ids]}
            )
            if route_edge_ids is not None
            else full_diagram
        )
        return {
            "schema": "archbro.full_canvas.v1",
            "project_id": project_id,
            "architecture_version": model.version,
            "diagram": diagram.model_dump(mode="json"),
            "positioned_graph": asdict(canvas_layout.graph),
            "group_frames": [asdict(frame) for frame in canvas_layout.group_frames],
        }

    def context_manifest_payload(self, project_id: str, request: dict) -> dict:
        context = self.contexts[project_id]
        architecture_payload = context["architecture"]
        component_id = str(request["node_id"]).removeprefix("node:")
        lineage: list[dict] = []

        def compact(node: dict) -> dict:
            return {
                "node_id": f"node:{node['id']}",
                "component_id": node["id"],
                "name": node["name"],
                "type": node["type"],
                "kind": node.get("kind", "SYSTEM"),
                "responsibility": node["responsibility"],
                "status": node.get("status", "ACCEPTED"),
            }

        def find(nodes: list[dict], trail: list[dict]) -> dict | None:
            for node in nodes:
                current = [*trail, node]
                if node["id"] == component_id:
                    lineage.extend(compact(item) for item in current)
                    return node
                nested = find(node.get("children", []), current)
                if nested is not None:
                    return nested
            return None

        origin = find(architecture_payload.get("components", []), [])
        if origin is None:
            raise AssertionError(f"Unknown context manifest node: {component_id}")
        by_id: dict[str, dict] = {}

        def collect(nodes: list[dict]) -> None:
            for node in nodes:
                by_id[node["id"]] = node
                collect(node.get("children", []))

        collect(architecture_payload.get("components", []))
        limits = {
            "ASK_ALL": (1, 8),
            "ALLOW_NEIGHBORHOOD": (2, 14),
            "AUTO_BOUNDED": (3, 20),
        }
        max_hops, max_results = limits[request["expansion_policy"]]
        relationships = architecture_payload.get("relationships", [])
        reached: dict[str, int] = {}
        queue = [(component_id, 0)]
        while queue:
            current, hop = queue.pop(0)
            if hop >= max_hops:
                continue
            for relationship in relationships:
                peer = None
                if relationship["source"] == current:
                    peer = relationship["target"]
                elif relationship["target"] == current:
                    peer = relationship["source"]
                if peer is None or peer == component_id or peer in reached:
                    continue
                reached[peer] = hop + 1
                queue.append((peer, hop + 1))
        kept = sorted(reached, key=lambda item: (reached[item], item))[:max_results]
        dependency_nodes = [{**compact(by_id[item]), "hop": reached[item]} for item in kept]
        allowed = {component_id, *(child["id"] for child in origin.get("children", [])), *kept}
        dependency_relationships = [
            {
                "id": f"relationship:{index}",
                "source": f"node:{item['source']}",
                "target": f"node:{item['target']}",
                "semantic_type": item["relationship_type"],
                "description": item.get("description", ""),
            }
            for index, item in enumerate(relationships)
            if item["source"] in {component_id, *kept} and item["target"] in {component_id, *kept}
        ]
        tasks = [
            {
                "id": item["id"],
                "title": item["title"],
                "description": item.get("description", ""),
                "status": item["status"],
                "owner": item.get("owner", "HUMAN"),
                "source": item.get("source", "ARCHITECTURE"),
                "related_component": item.get("related_component"),
                "dependencies": item.get("dependencies", []),
                "acceptance_criteria": item.get("acceptance_criteria", []),
            }
            for item in context.get("tasks", [])
            if item.get("related_component") in allowed
        ]
        manifest = {
            "schema": "archbro.agent_context_manifest.v1",
            "project_id": project_id,
            "architecture_version": architecture_payload["version"],
            "selection": {
                **request,
                "effective_max_hops": max_hops,
                "effective_max_results": max_results,
            },
            "sections": {
                "project": {
                    "id": project_id,
                    "name": context["project"]["name"],
                    "status": context["project"]["status"],
                    "goal": context["project"]["goal"],
                    "architecture_summary": architecture_payload["summary"],
                },
                "architecture": {
                    "origin": compact(origin),
                    "lineage": lineage,
                    "children": [compact(child) for child in origin.get("children", [])],
                    "dependency_context": {
                        "origin": compact(origin),
                        "nodes": dependency_nodes,
                        "relationships": dependency_relationships,
                        "counts": {
                            "nodes": len(dependency_nodes),
                            "relationships": len(dependency_relationships),
                            "max_hop": max(reached.values(), default=0),
                        },
                        "truncated": len(reached) > max_results,
                        "limit_reason": "MAX_RESULTS" if len(reached) > max_results else None,
                    },
                },
                "tasks": tasks,
                "pending_proposals": [],
                "evidence": [],
                "code_truth": {"status": "NO_SNAPSHOT", "chunks": []},
                "mcp_refs": [],
            },
            "budget": {
                "max_chars": 24000,
                "max_estimated_input_tokens": 6000,
                "max_evidence_records": 12,
                "max_code_truth_chunks": 12,
            },
            "usage": {
                "selected_node_count": 1,
                "context_chars": 0,
                "estimated_input_tokens": 0,
                "task_count": len(tasks),
                "evidence_count": 0,
                "mcp_result_count": 0,
                "code_truth_chunk_count": 0,
                "expansion_count": max(0, max(reached.values(), default=0) - 1),
                "truncated": len(reached) > max_results,
                "limit_reasons": ["MAX_RESULTS"] if len(reached) > max_results else [],
            },
            "manifest_hash": "0" * 64,
        }
        manifest["usage"]["context_chars"] = len(json.dumps(manifest, sort_keys=True))
        manifest["usage"]["estimated_input_tokens"] = (manifest["usage"]["context_chars"] + 3) // 4
        digest_payload = {key: value for key, value in manifest.items() if key != "manifest_hash"}
        manifest["manifest_hash"] = hashlib.sha256(
            json.dumps(digest_payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return manifest

    def handle(self, route: Route) -> None:
        request = route.request
        method = request.method
        path = urlsplit(request.url).path
        self.requests.append({"method": method, "path": path, "url": request.url})
        key = (method, path)
        if self.fail_once.get(key, 0):
            self.fail_once[key] -= 1
            self.json(route, {"detail": "Deliberate final-fix browser failure"}, 503)
            return

        if path == "/projects" and method == "GET":
            self.json(route, self.projects)
            return
        if path == "/projects" and method == "POST":
            body = request.post_data_json
            created = project("created-project", body["name"])
            created.update({"goal": body["goal"], "description": body.get("description", "")})
            self.projects.append(created)
            self.contexts[created["id"]] = {
                "project": copy.deepcopy(created),
                "tasks": [],
                "architecture": {**architecture(created["id"]), "version": 0, "components": []},
                "proposals": [],
            }
            self.json(route, created, 201)
            return
        if path == "/onboarding/goal" and method == "POST":
            body = request.post_data_json
            current_goal = body.get("current_goal", "")
            self.json(route, {
                "goal": current_goal,
                "suggested_project_name": "Recovered Project",
                "ready": True,
                "missing_information": [],
                "assistant_message": "Your complete Goal is preserved.",
            })
            return

        parts = [part for part in path.split("/") if part]
        if len(parts) < 2 or parts[0] != "projects":
            route.continue_()
            return
        project_id = parts[1]
        context = self.contexts.get(project_id)
        if not context:
            self.json(route, {"detail": "Project not found"}, 404)
            return

        if len(parts) == 2 and method == "GET":
            self.json(route, context["project"])
            return
        if parts[2:] == ["workspace-bootstrap"] and method == "GET":
            architecture_version = int(context["architecture"].get("version", 0))
            self.json(route, {
                "schema": "archbro.workspace-bootstrap.v2",
                "project": copy.deepcopy(context["project"]),
                "tasks": copy.deepcopy(context["tasks"]),
                "architecture": copy.deepcopy(context["architecture"]),
                "proposals": copy.deepcopy(context["proposals"]),
                "activity": copy.deepcopy(context.get("activity", [])),
                "resources": {
                    "canvas": {
                        "status": "DEFERRED",
                        "href": f"/projects/{project_id}/architecture/canvas?expected_architecture_version={architecture_version}&reading_mode=FULL",
                    },
                    "project_diagram": {
                        "status": "DEFERRED",
                        "href": f"/projects/{project_id}/architecture/diagram?expected_architecture_version={architecture_version}&reading_mode=MAP",
                    },
                },
                "built_in_model_called": False,
            })
            return
        if len(parts) == 2 and method == "PATCH":
            body = request.post_data_json
            context["project"].update(body)
            next(item for item in self.projects if item["id"] == project_id).update(body)
            self.json(route, context["project"])
            return
        if parts[2:] == ["tasks"] and method == "GET":
            self.json(route, context["tasks"])
            return
        if parts[2:] == ["architecture"] and method == "GET":
            if self.fail_decision_readback:
                self.json(route, {"detail": "Deliberate read-back failure"}, 503)
                return
            self.json(route, context["architecture"])
            return
        if parts[2:] == ["architecture", "diagram"] and method == "GET":
            query = parse_qs(urlsplit(request.url).query)
            scope = query.get("scope", [None])[0]
            expected = query.get("expected_architecture_version", [None])[0]
            current_version = int(context["architecture"].get("version", 0))
            if expected is not None and int(expected) != current_version:
                self.json(route, {"detail": {"code": "stale_architecture_version", "expected_architecture_version": int(expected), "current_architecture_version": current_version}}, 409)
                return
            self.json(route, self.scoped_diagram_payload(project_id, scope))
            return
        if parts[2:] == ["architecture", "canvas"] and method == "GET":
            query = parse_qs(urlsplit(request.url).query)
            expected = query.get("expected_architecture_version", [None])[0]
            reading_mode = query.get("reading_mode", ["FULL"])[0]
            current_version = int(context["architecture"].get("version", 0))
            if expected is not None and int(expected) != current_version:
                self.json(route, {"detail": {"code": "stale_architecture_version", "expected_architecture_version": int(expected), "current_architecture_version": current_version}}, 409)
                return
            self.json(route, self.full_canvas_payload(project_id, reading_mode))
            return
        if parts[2:] == ["architecture", "path"] and method == "GET":
            query = parse_qs(urlsplit(request.url).query)
            source_id = query.get("source_id", [""])[0]
            target_id = query.get("target_id", [""])[0]
            max_hops = int(query.get("max_hops", ["8"])[0])
            expected = query.get("expected_architecture_version", [None])[0]
            current_version = int(context["architecture"].get("version", 0))
            if expected is not None and int(expected) != current_version:
                self.json(route, {"detail": {"error": "stale_architecture_version", "expected_architecture_version": int(expected), "current_architecture_version": current_version}}, 409)
                return
            payload = find_architecture_path(
                Architecture.model_validate(context["architecture"]),
                project_id,
                source_id,
                target_id,
                max_hops=max_hops,
                expected_architecture_version=current_version,
            )
            self.json(route, payload)
            return
        if parts[2:] == ["architecture", "proposals"] and method == "GET":
            if self.fail_decision_readback:
                self.json(route, {"detail": "Deliberate read-back failure"}, 503)
                return
            self.json(route, context["proposals"])
            return
        if (
            len(parts) == 6
            and parts[2:4] == ["architecture", "proposals"]
            and parts[5] == "acceptance-preview"
            and method == "GET"
        ):
            proposal_id = parts[4]
            candidate = next((item for item in context["proposals"] if item["id"] == proposal_id), None)
            if not candidate or candidate["status"] != "PENDING" or candidate.get("base_architecture_version") != context["architecture"]["version"]:
                self.json(route, {"detail": "proposal is not pending for this project"}, 409)
                return
            after = copy.deepcopy(context["architecture"])
            after["version"] += 1
            self.json(route, {
                "proposal_id": proposal_id,
                "proposal_status": "PENDING",
                "actionable": True,
                "current_architecture_version": context["architecture"]["version"],
                "resulting_architecture_version": after["version"],
                "components_before": context["architecture"]["components"],
                "components_after": after["components"],
                "relationships_before": context["architecture"].get("relationships", []),
                "relationships_after": after.get("relationships", []),
                "decisions_added": [f"Accepted proposal {proposal_id}"],
                "task_updates": [],
                "created_tasks": [],
                "blocked_task_ids": [],
                "remapped_tasks": [],
                "superseded_proposals": [
                    {
                        "proposal_id": peer["id"],
                        "base_architecture_version": peer.get("base_architecture_version"),
                        "superseded_at_architecture_version": after["version"],
                        "reason": f"Superseded by accepted proposal {proposal_id}",
                    }
                    for peer in context["proposals"]
                    if peer["id"] != proposal_id and peer["status"] == "PENDING"
                ],
                "warnings": [],
            })
            return
        if (
            len(parts) == 6
            and parts[2:4] == ["architecture", "proposals"]
            and parts[5] in {"accept", "reject"}
            and method == "POST"
        ):
            proposal_id, decision = parts[4], parts[5]
            candidate = next((item for item in context["proposals"] if item["id"] == proposal_id), None)
            self.decision_requests.append({"proposal_id": proposal_id, "decision": decision})
            if not candidate or candidate["status"] != "PENDING":
                self.json(route, {"detail": "proposal is not pending for this project"}, 409)
                return
            candidate["status"] = "ACCEPTED" if decision == "accept" else "REJECTED"
            if decision == "accept":
                context["architecture"]["version"] += 1
                context["project"]["architecture_version"] = context["architecture"]["version"]
                for peer in context["proposals"]:
                    if peer["id"] != proposal_id and peer["status"] == "PENDING":
                        peer["status"] = "SUPERSEDED"
                        peer["resolution_reason"] = f"Superseded by accepted proposal {proposal_id}"
                        peer["superseded_by_proposal_id"] = proposal_id
                        peer["superseded_at_architecture_version"] = context["architecture"]["version"]
            if self.drop_next_decision_response:
                self.drop_next_decision_response = False
                route.abort("failed")
                return
            self.json(route, candidate)
            return
        if parts[2:] == ["agent-context", "manifest"] and method == "POST":
            body = request.post_data_json
            manifest = self.context_manifest_payload(project_id, body)
            self.context_manifest_requests.append({"request": copy.deepcopy(body), "manifest": copy.deepcopy(manifest)})
            self.json(route, manifest)
            return
        if parts[2:] == ["code-architecture", "latest"] and method == "GET":
            route.fulfill(status=204, body="")
            return
        if parts[2:] == ["events"] and method == "GET":
            self.json(route, context.get("activity", []))
            return
        if parts[2:] == ["events"] and method == "POST":
            body = request.post_data_json
            if body.get("type") == "TASK_UPDATED":
                payload = body.get("payload", {})
                changed = next((item for item in context["tasks"] if item["id"] == payload.get("task_id")), None)
                if changed is not None:
                    changed["status"] = payload.get("status", changed["status"])
            context_request = body.get("payload", {}).get("agent_context_request")
            manifest = None
            if context_request:
                manifest_request = {
                    key: value
                    for key, value in context_request.items()
                    if key != "preview_manifest_hash"
                }
                manifest = self.context_manifest_payload(project_id, manifest_request)
                if manifest["manifest_hash"] != context_request["preview_manifest_hash"]:
                    self.json(route, {
                        "detail": {
                            "error": "agent_context_preview_stale",
                            "expected_manifest_hash": context_request["preview_manifest_hash"],
                            "current_manifest_hash": manifest["manifest_hash"],
                        }
                    }, 409)
                    return
            self.event_requests.append({"path": path, "body": copy.deepcopy(body), "server_manifest": copy.deepcopy(manifest)})
            if body.get("payload", {}).get("intent") == "INITIAL_ARCHITECTURE":
                context["architecture"] = architecture(project_id)
            result = {
                "result": self.event_result,
                "summary": "Fixture agent response.",
                "provider": "fixture",
                "model": "fixture-model",
                "actions": [],
                "architecture_review_required": False,
                "error": "Fixture agent error." if self.event_result == "ERROR" else None,
                "context_telemetry": (
                    {**manifest["usage"], "manifest_hash": manifest["manifest_hash"], "architecture_version": manifest["architecture_version"], "selection": manifest["selection"]}
                    if manifest is not None
                    else None
                ),
                "provider_usage": {"input_tokens": 1234, "output_tokens": 56} if manifest is not None else None,
            }
            self.json(route, result)
            return
        route.continue_()


def profile(identity: str, name: object = "Review User", *, complete: bool = True, lens: object = "software", notifications: object | None = None) -> dict:
    email = identity.removeprefix("email:")
    return {
        "id": identity,
        "provider": "password",
        "email": email,
        "name": name,
        "onboardingComplete": complete,
        "defaultLens": lens,
        "notifications": notifications if notifications is not None else {"architectureApprovals": True, "blockedTasks": True},
    }


def add_storage(context: BrowserContext, *, identity: str | None = None, profiles: object | None = None, pending_goal: str | None = None, project_id: str | None = None) -> None:
    values = {}
    if identity:
        session_profile = profile(identity)
        values["archbro-demo-session"] = json.dumps({key: session_profile[key] for key in ["id", "provider", "email", "name"]})
        values["archbro-demo-profiles"] = json.dumps(profiles if profiles is not None else {identity: session_profile})
    elif profiles is not None:
        values["archbro-demo-profiles"] = json.dumps(profiles)
    if pending_goal is not None:
        values["archbro-pending-goal"] = pending_goal
    if project_id:
        values["archbro-project-id"] = project_id
    encoded = json.dumps(values).replace("</", "<\\/")
    context.add_init_script(
        f"""
        (() => {{
          if (sessionStorage.getItem('archbro-final-fix-seeded') === 'true') return;
          Object.entries({encoded}).forEach(([key, value]) => localStorage.setItem(key, value));
          sessionStorage.setItem('archbro-final-fix-seeded', 'true');
        }})();
        """
    )


def open_page(browser: Browser, backend: FakeBackend, *, viewport: dict | None = None, identity: str | None = None, profiles: object | None = None, pending_goal: str | None = None, project_id: str | None = None) -> tuple[BrowserContext, Page, list[str]]:
    context = browser.new_context(viewport=viewport or {"width": 1440, "height": 1000})
    add_storage(context, identity=identity, profiles=profiles, pending_goal=pending_goal, project_id=project_id)
    context.route("**/projects**", backend.handle)
    context.route("**/onboarding/goal", backend.handle)
    errors: list[str] = []
    page = context.new_page()
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(BASE_URL, wait_until="networkidle")
        assert not errors, errors
    except Exception:
        context.close()
        raise
    return context, page, errors


def sign_up(page: Page, email: str, name: str = "First Reviewer") -> None:
    page.locator("#landingLoginBtn").click()
    page.locator("#authModeToggle").click()
    page.locator("#authName").fill(name)
    page.locator("#authEmail").fill(email)
    page.locator("#authPassword").fill("prototype-pass")
    page.locator("#authConfirmPassword").fill("prototype-pass")
    page.locator("#authSubmitBtn").click()


def case_landing_authentication_teaser(browser: Browser) -> None:
    backend = FakeBackend()
    context, page, errors = open_page(browser, backend)
    with diagnostic_scope(context.close):
        page.locator("#landingAuthSend").click()
        assert page.locator("#authView").is_visible()
        assert page.evaluate("() => localStorage.getItem('archbro-pending-goal')") is None
        assert not errors, errors


def case_progressive_project_creation(browser: Browser) -> None:
    goal = "Keep this exact goal through project refinement and generation."
    identity = "email:pending@example.com"
    incomplete = profile(identity, complete=False, lens=None)
    backend = FakeBackend()
    context, page, errors = open_page(browser, backend, identity=identity, profiles={identity: incomplete}, pending_goal=goal)
    with diagnostic_scope(context.close):
        assert page.locator("#preferenceView").is_visible()
        assert page.evaluate("() => localStorage.getItem('archbro-pending-goal')") is None
        page.locator('[data-project-lens="design"]').click()
        page.locator("#preferenceContinueBtn").click()
        page.locator("#workspaceHome").wait_for(state="visible")
        page.locator("#newProjectBtn").click()
        page.locator("#newProjectNameDialog").wait_for(state="visible")
        page.locator("#newProjectName").fill("   ")
        page.locator("#newProjectNameDialog button[type='submit']").click()
        assert page.locator("#newProjectName").input_value() == "   "
        assert "name" in page.locator("#newProjectNameError").inner_text().lower()
        page.locator("#newProjectName").fill("Durable Goal")
        page.locator("#newProjectNameDialog button[type='submit']").click()
        page.locator("#initialGoalStage").wait_for(state="visible")
        page.locator("#initialGoal").fill("   ")
        page.locator("#initialGoalForm button[type='submit']").click()
        assert page.locator("#initialGoal").input_value() == "   "
        assert "goal" in page.locator("#initialGoalError").inner_text().lower()
        page.locator("#initialGoal").fill(goal)
        page.locator("#initialGoalForm button[type='submit']").click()
        page.locator("#refineGoalStage").wait_for(state="visible")
        assert page.locator("#goalDraftText").input_value() == goal
        assert page.locator("#onboardingConversation").is_hidden()
        ask = page.locator("#onboardingAsk")
        ask.click()
        ask.press_sequentially("Add a clear success metric to the goal.")
        page.wait_for_function("() => document.querySelector('#onboardingAsk')?.closest('.onboarding-ask')?.classList.contains('rainbow-active')")
        ask.evaluate("element => element.blur()")
        page.wait_for_function("() => !document.querySelector('#onboardingAsk')?.closest('.onboarding-ask')?.classList.contains('rainbow-active')")
        ask.focus()
        assert not page.locator("#onboardingAsk").evaluate("element => element.closest('.onboarding-ask')?.classList.contains('rainbow-active')")
        ask.press_sequentially(" Keep it measurable.")
        page.wait_for_function("() => document.querySelector('#onboardingAsk')?.closest('.onboarding-ask')?.classList.contains('rainbow-active')")
        ask.fill("")
        page.wait_for_function("() => !document.querySelector('#onboardingAsk')?.closest('.onboarding-ask')?.classList.contains('rainbow-active')")
        page.locator("#editOnboardingProjectName").click()
        page.locator("#newProjectName").fill("Durable Goal Edited")
        page.locator("#newProjectNameDialog button[type='submit']").click()
        assert "Durable Goal Edited" in page.locator("#onboardingProjectName").inner_text()
        backend.fail_next("POST", "/projects")
        page.locator("#useGoalBtn").click()
        page.locator("#toast.error").wait_for(state="visible")
        assert page.locator("#goalDraftText").input_value() == goal
        page.locator("#useGoalBtn").click()
        page.wait_for_function("() => localStorage.getItem('archbro-project-id') === 'created-project'")
        assert page.locator('[data-project-id="created-project"] [data-project-toggle]').get_attribute("aria-expanded") == "true"
        assert page.locator('[data-project-id="created-project"] [data-project-view="overview"]').is_visible()
        page.wait_for_function("() => document.querySelector('#agentStatus')?.textContent.includes('Agent ready')")
        page.locator("#newProjectBtn").click()
        assert page.locator("#newProjectNameDialog").is_visible()
        page.keyboard.press("Escape")
        page.locator("#workspace").wait_for(state="visible")
        assert page.locator("#welcomeTitle").inner_text() == "Durable Goal Edited"
        assert not errors, errors


def case_empty_workspace_cancel(browser: Browser) -> None:
    identity = "email:empty@example.com"
    backend = FakeBackend()
    context, page, errors = open_page(browser, backend, identity=identity, profiles={identity: profile(identity)})
    with diagnostic_scope(context.close):
        assert page.locator("#workspaceHome").is_visible()
        for width in (1051, 1050, 901, 900, 860, 761, 390):
            page.set_viewport_size({"width": width, "height": 844})
            page.wait_for_timeout(60)
            assert page.locator("#pageTitle").inner_text() == "Project workspace"
            title_metrics = page.locator("#pageTitle").evaluate(
                """node => { const style=getComputedStyle(node); const rect=node.getBoundingClientRect(); return {height:rect.height,fontSize:parseFloat(style.fontSize),whiteSpace:style.whiteSpace}; }"""
            )
            assert title_metrics["whiteSpace"] == "nowrap", f"Project workspace can wrap at {width}px"
            assert title_metrics["height"] <= title_metrics["fontSize"] * 1.5, f"Project workspace wrapped at {width}px"
            topbar = page.locator(".topbar").bounding_box()
            heading = page.locator(".page-heading").bounding_box()
            actions = page.locator(".top-actions").bounding_box()
            assert topbar and heading and actions
            assert heading["y"] >= topbar["y"] - 1
            assert heading["y"] + heading["height"] <= topbar["y"] + topbar["height"] + 1
            assert heading["x"] + heading["width"] <= actions["x"] + 1
            subtitle_display = page.locator("#pageSubtitle").evaluate("node => getComputedStyle(node).display")
            if 761 <= width <= 1050:
                assert subtitle_display == "none"
            if width <= 900:
                assert not page.locator("#workspaceLens").is_visible()
            elif width >= 901:
                assert page.locator("#workspaceLens").is_visible()
        page.set_viewport_size({"width": 1440, "height": 1000})
        page.wait_for_timeout(60)
        page.locator("#newProjectBtn").click()
        page.locator("#newProjectNameDialog").wait_for(state="visible")
        page.keyboard.press("Escape")
        page.locator("#newProjectNameDialog").wait_for(state="hidden")
        assert page.locator("#workspaceHome").is_visible()
        assert page.locator("#workspaceHomeEmpty").is_visible()
        page.locator("#newProjectBtn").click()
        page.locator("#newProjectNameDialog").wait_for(state="visible")
        page.locator("[data-new-project-name-cancel]").last.click()
        page.locator("#newProjectNameDialog").wait_for(state="hidden")
        assert page.locator("#workspaceHome").is_visible()
        assert not errors, errors


def case_storage_recovery(browser: Browser) -> None:
    identity = "email:shape@example.com"
    backend = FakeBackend()
    context, page, errors = open_page(browser, backend, identity=identity, profiles=None)
    with diagnostic_scope(context.close):
        page.evaluate("() => localStorage.setItem('archbro-demo-profiles', 'null')")
        page.reload(wait_until="networkidle")
        assert page.locator("#landingView").is_visible()
        assert page.evaluate("() => localStorage.getItem('archbro-demo-session')") is None
        assert page.evaluate("() => localStorage.getItem('archbro-demo-profiles')") == "null"
        assert not errors, errors

    malformed = profile(identity, name={"wrong": True}, complete=True, lens="", notifications={"architectureApprovals": "yes", "blockedTasks": False})
    context, page, errors = open_page(browser, backend, identity=identity, profiles={identity: malformed})
    with diagnostic_scope(context.close):
        assert page.locator("#preferenceView").is_visible()
        assert not errors, errors


def case_logout_reset(browser: Browser) -> None:
    backend = FakeBackend([project("alpha", "Shared Alpha")])
    backend.contexts["alpha"]["tasks"] = [task("alpha", "task-a", "Resolve shared dependency")]
    context, page, errors = open_page(browser, backend)
    with diagnostic_scope(context.close):
        sign_up(page, "first@example.com")
        page.locator('[data-project-lens="engineering"]').click()
        page.locator("#preferenceContinueBtn").click()
        page.locator("#workspaceShell").wait_for(state="visible")
        page.locator("#authPassword").evaluate("input => input.type = 'text'")
        page.locator('[data-password-target="authPassword"]').evaluate("button => { button.textContent = 'Hide'; button.setAttribute('aria-label', 'Hide password'); }")
        page.locator("#accountBtn").click()
        page.locator("#logoutBtn").click()
        assert page.locator("#landingView").is_visible()
        assert page.locator("#authEmail").input_value() == ""
        assert page.locator("#authPassword").input_value() == ""
        assert page.locator("#authConfirmPassword").input_value() == ""
        assert page.locator("#authPassword").get_attribute("type") == "password"
        assert page.locator('[data-password-target="authPassword"]').get_attribute("aria-label") == "Show password"
        sign_up(page, "second@example.com", "Second Reviewer")
        assert page.locator("#preferenceView").is_visible()
        assert page.locator('[data-project-lens][aria-checked="true"]').count() == 0
        assert page.locator("#preferenceContinueBtn").is_disabled()
        page.locator('[data-project-lens="software"]').click()
        page.locator("#preferenceContinueBtn").click()
        page.locator('[data-project-id="alpha"]').wait_for(state="visible")
        assert page.locator('[data-project-open]').first.inner_text().strip() == "Shared Alpha"
        assert not errors, errors


def case_transactional_project_selection(browser: Browser) -> None:
    backend = FakeBackend([project("alpha", "Alpha Project"), project("beta", "Beta Project")])
    backend.contexts["alpha"]["tasks"] = [task("alpha", "task-a", "Alpha task", "TODO")]
    backend.contexts["beta"]["tasks"] = [task("beta", "task-b", "Beta task", "TODO")]
    identity = "email:transaction@example.com"
    backend.fail_next("GET", "/projects/beta/workspace-bootstrap")
    context, page, errors = open_page(browser, backend, identity=identity, project_id="alpha")
    with diagnostic_scope(context.close):
        page.locator('[data-project-id="beta"] [data-project-open]').click()
        page.locator("#toast.error").wait_for(state="visible")
        assert page.evaluate("() => localStorage.getItem('archbro-project-id')") == "alpha"
        assert page.locator('[data-project-id="alpha"] [data-project-open]').get_attribute("aria-pressed") == "true"
        assert page.locator("#welcomeTitle").inner_text() == "Alpha Project"
        page.locator("#instruction").fill("Keep this event on Alpha.")
        page.locator("#instructionForm button[type='submit']").click()
        page.locator("#globalAgentReply").wait_for(state="visible")
        assert backend.event_requests[-1]["path"] == "/projects/alpha/events"
        assert not errors, errors


def case_notifications_and_context(browser: Browser) -> None:
    backend = FakeBackend([project("alpha", "Attention Project")])
    backend.contexts["alpha"]["tasks"] = [task("alpha", "blocked-a", "Choose data provider")]
    backend.contexts["alpha"]["proposals"] = [proposal("alpha", "proposal-a")]
    identity = "email:attention@example.com"
    context, page, errors = open_page(browser, backend, identity=identity, project_id="alpha")
    with diagnostic_scope(context.close):
        assert page.locator("#needsCount").inner_text() == "2 items need you ↗"
        page.locator("#newProjectBtn").click()
        page.locator("#newProjectName").fill("Temporary context review")
        page.locator("#newProjectNameDialog button[type='submit']").click()
        page.locator("#initialGoal").fill("Review the current project context before generating architecture.")
        page.locator("#initialGoalForm button[type='submit']").click()
        page.locator("#refineGoalStage").wait_for(state="visible")
        assert page.locator("#notificationBadge").is_hidden()
        page.evaluate("() => document.querySelector('#notificationBtn').click()")
        assert "nothing needs" in page.locator("#notificationList").inner_text().lower()
        page.keyboard.press("Escape")
        page.locator("#onboardingBackBtn").click()
        page.locator("#workspace").wait_for(state="visible")
        page.locator("#notificationBtn").click()
        page.locator('[data-attention-kind="task"]').click()
        assert page.locator("#view-tasks").evaluate("node => node.classList.contains('active')")
        task_context = page.locator('#view-tasks [data-task-open="blocked-a"]')
        assert task_context.get_attribute("aria-expanded") == "true"
        assert page.locator("#taskDetailPanel").is_visible()
        page.wait_for_function("() => document.activeElement?.id === 'taskDetailClose'")
        page.locator("#notificationBtn").click()
        page.locator('[data-attention-kind="proposal"]').click()
        assert page.locator("#workspaceTabReviewPanel").is_visible()
        assert page.locator("#proposalList [data-proposal-select=\"proposal-a\"]").get_attribute("aria-pressed") == "true"
        page.wait_for_function("() => document.activeElement?.dataset.proposalSelect === 'proposal-a'")
        page.locator("#notificationBtn").click()
        page.locator('[data-attention-kind="task"]').evaluate("button => button.dataset.attentionId = 'missing-task'")
        page.locator('[data-attention-kind="task"]').click()
        page.locator("#notificationMenu").wait_for(state="visible")
        assert "no longer available" in page.locator("#notificationList").inner_text().lower()
        page.wait_for_function("() => document.activeElement?.textContent.includes('no longer available')")
        assert not errors, errors


def case_overview_navigation_and_attention(browser: Browser) -> None:
    backend = FakeBackend([project("overview", "Overview Project")])
    backend.contexts["overview"]["tasks"] = [
        task("overview", "task-ready", "Prepare release notes", "TODO"),
        task("overview", "task-blocked", "Choose deployment provider", "BLOCKED"),
    ]
    backend.contexts["overview"]["proposals"] = [proposal("overview", "proposal-overview")]
    identity = "email:overview@example.com"
    context, page, errors = open_page(browser, backend, identity=identity, project_id="overview")
    with diagnostic_scope(context.close):
        assert page.locator("#view-overview").evaluate("node => node.classList.contains('active')")
        assert page.locator("#needsCount").inner_text() == "2 items need you ↗"
        assert page.locator("#readyCount").inner_text() == "1 ready task ↗"

        section_order = page.locator("#view-overview .overview-section").evaluate_all(
            "nodes => nodes.map(node => [...node.classList].find(name => name.startsWith('overview-') && name !== 'overview-section'))"
        )
        assert section_order == [
            "overview-status",
            "overview-goal",
            "overview-review",
            "overview-architecture",
            "overview-next-tasks",
            "overview-activity",
        ]

        # Remember Review, return to Overview, then prove every task-oriented Overview
        # shortcut explicitly owns the Tasks tab rather than restoring that memory.
        page.locator('[data-project-id="overview"] [data-project-view="tasks"]').click()
        page.locator("#workspaceTabReview").click()
        assert page.locator("#workspaceTabReview").get_attribute("aria-selected") == "true"
        page.locator('[data-project-id="overview"] [data-project-view="overview"]').click()
        page.locator("#view-overview").wait_for(state="visible")
        history_before = page.evaluate("() => history.length")
        page.locator("#readySummary").click()
        page.locator("#workspaceTabTasksPanel").wait_for(state="visible")
        assert page.locator("#workspaceTabTasks").get_attribute("aria-selected") == "true"
        assert page.evaluate("() => window.ArchBroWebBridge.getCommittedNavigation().workspace_tab") == "tasks"
        assert page.evaluate("() => history.length") == history_before + 1

        page.locator('[data-project-id="overview"] [data-project-view="overview"]').click()
        page.locator("#architectureSummaryButton").click()
        assert page.locator("#view-architecture").evaluate("node => node.classList.contains('active')")
        assert not errors, errors


def case_keyboard_and_mobile_layers(browser: Browser) -> None:
    backend = FakeBackend([project("alpha", "Keyboard Project")])
    backend.contexts["alpha"]["tasks"] = [task("alpha", "task-a", "Keyboard task", "TODO")]
    backend.contexts["alpha"]["proposals"] = [proposal("alpha", "proposal-a")]
    identity = "email:keyboard@example.com"
    context, page, errors = open_page(browser, backend, identity=identity, project_id="alpha")
    with diagnostic_scope(context.close):
        assert page.locator("#projectTree").get_attribute("role") is None
        project_button = page.locator('[data-project-id="alpha"] [data-project-open]')
        assert project_button.get_attribute("aria-pressed") == "true"
        project_toggle = page.locator('[data-project-id="alpha"] [data-project-toggle]')
        project_toggle.focus()
        page.keyboard.press("Space")
        page.wait_for_function("() => document.querySelector('[data-project-id=\"alpha\"] [data-project-toggle]')?.getAttribute('aria-expanded') === 'false' && document.activeElement?.hasAttribute('data-project-toggle')")
        page.keyboard.press("Space")
        page.wait_for_function("() => document.querySelector('[data-project-id=\"alpha\"] [data-project-toggle]')?.getAttribute('aria-expanded') === 'true'")
        task_view = page.locator('[data-project-id="alpha"] [data-project-view="tasks"]')
        task_view.focus()
        page.keyboard.press("Enter")
        task_context = page.locator('#view-tasks [data-task-open="task-a"]')
        task_context.focus()
        page.keyboard.press("Space")
        page.wait_for_function("() => document.querySelector('#view-tasks [data-task-open=\"task-a\"]')?.getAttribute('aria-expanded') === 'true'")
        assert task_context.get_attribute("aria-expanded") == "true"
        assert page.locator("#taskDetailPanel").is_visible()
        assert "Keyboard task" in page.locator("#instructionContext").inner_text()

        graph_view = page.locator('[data-project-id="alpha"] [data-project-view="architecture"]')
        graph_view.focus()
        page.keyboard.press("Enter")
        graph_control = page.locator('[data-component="alpha-experience"][data-node-action="drill"]')
        graph_control.focus()
        page.keyboard.press("Enter")
        assert "selected" in (graph_control.get_attribute("class") or "")
        assert page.locator('[data-component="alpha-experience"][data-projection-role="SCOPE"]').count() == 0
        page.wait_for_function("() => document.activeElement?.dataset.component === 'alpha-experience'")
        page.keyboard.press("ArrowRight")
        scoped_anchor = page.locator('[data-component="alpha-experience"][data-projection-role="SCOPE"]')
        page.locator(".graph-scope-bar strong", has_text="Workspace Experience").wait_for(state="visible")
        assert scoped_anchor.count() == 0
        page.wait_for_function("() => document.activeElement?.hasAttribute('data-graph-back')")
        page.keyboard.press("Enter")
        root_node = page.locator('[data-component="alpha-experience"][data-node-action="drill"]')
        root_node.wait_for(state="visible")
        page.wait_for_function("() => document.activeElement?.dataset.component === 'alpha-experience'")

        page.locator("#accountBtn").focus()
        page.keyboard.press("Enter")
        page.keyboard.press("End")
        assert page.evaluate("() => document.activeElement?.id") == "logoutBtn"
        page.keyboard.press("Home")
        assert page.evaluate("() => document.activeElement?.dataset.accountSection") == "profile"
        page.keyboard.press("Escape")
        assert page.evaluate("() => document.activeElement?.id") == "accountBtn"
        assert "Keyboard Project" not in (page.locator("#accountBtn").get_attribute("aria-label") or "")
        assert "Review User" in page.locator("#accountBtn").get_attribute("aria-label")

        page.set_viewport_size({"width": 390, "height": 844})
        page.wait_for_timeout(100)
        assert page.locator("#workspaceSidebar").get_attribute("aria-hidden") == "true"
        assert page.locator("#workspaceSidebar").evaluate("node => node.inert")
        page.locator("#mobileSidebarBtn").focus()
        page.keyboard.press("Enter")
        page.wait_for_function("() => document.activeElement?.id === 'newProjectBtn'")
        assert page.locator("main").evaluate("node => node.inert")
        page.keyboard.press("Escape")
        page.wait_for_function("() => document.activeElement?.id === 'mobileSidebarBtn'")
        assert page.locator("#workspaceSidebar").evaluate("node => node.inert")
        page.locator("#mobileSidebarBtn").click()
        for selector in ["#mobileSidebarBtn", '[data-project-toggle]', '[data-project-menu]', '[data-project-view="overview"]']:
            box = page.locator(selector).first.bounding_box()
            assert box and box["width"] >= 44 and box["height"] >= 44, (selector, box)
        assert not errors, errors


def case_task_architecture_navigation(browser: Browser) -> None:
    backend = FakeBackend([project("task-nav", "Task Navigation")])
    linked = task("task-nav", "task-linked", "Open the agent composer architecture", "TODO")
    linked["related_component"] = "task-nav-composer"
    unlinked = task("task-nav", "task-unlinked", "Keep this task in the queue", "TODO")
    unlinked["related_component"] = None
    backend.contexts["task-nav"]["tasks"] = [linked, unlinked]
    identity = "email:task-nav@example.com"
    context, page, errors = open_page(browser, backend, identity=identity, project_id="task-nav")
    with diagnostic_scope(context.close):
        page.locator('[data-project-id="task-nav"] [data-project-view="tasks"]').click()
        page.locator("#view-tasks").wait_for(state="visible")

        linked_row = page.locator('#view-tasks [data-task-open="task-linked"]')
        assert linked_row.count() == 1
        unlinked_row = page.locator('#view-tasks [data-task-open="task-unlinked"]')
        assert unlinked_row.count() == 1

        linked_row.click()
        assert page.locator("#view-tasks").evaluate("node => node.classList.contains('active')")
        page.locator("#taskDetailPanel").wait_for(state="visible")
        assert page.locator("#taskDetailTitle").inner_text() == "Open the agent composer architecture"
        component_open = page.locator("#taskDetailPanel [data-task-component-open]")
        assert component_open.count() == 1
        page.locator("#taskDetailClose").click()
        page.wait_for_function("() => document.querySelector('#taskDetailPanel')?.hidden === true")

        linked_row.click()
        page.locator("#taskDetailPanel").wait_for(state="visible")
        component_open = page.locator("#taskDetailPanel [data-task-component-open]")
        component_open.click()
        page.locator("#view-architecture").wait_for(state="visible")
        target = page.locator('[data-component="task-nav-composer"]')
        target.wait_for(state="visible")
        page.wait_for_function("() => document.querySelector('[data-component=\"task-nav-composer\"]')?.classList.contains('selected')")
        assert "selected" in (target.get_attribute("class") or "")
        assert page.locator('[data-component="task-nav-experience"][data-projection-role="SCOPE"]').count() == 0
        assert page.locator(".graph-scope-bar strong", has_text="Workspace Experience").is_visible()

        page.locator('[data-project-id="task-nav"] [data-project-view="tasks"]').click()
        linked_row = page.locator('#view-tasks [data-task-open="task-linked"]')
        linked_row.focus()
        page.keyboard.press("Enter")
        page.locator("#taskDetailPanel").wait_for(state="visible")
        assert page.locator("#taskDetailTitle").inner_text() == "Open the agent composer architecture"
        assert page.locator("#view-tasks").evaluate("node => node.classList.contains('active')")
        assert not errors, errors


def case_architecture_inspector_disclosure(browser: Browser) -> None:
    project_id = "inspector"
    backend = FakeBackend([project(project_id, "Inspector Disclosure")])
    fixture = architecture(project_id)
    leaf = fixture["components"][0]["children"][0]
    leaf["id"] = "inspector-collaboration_and_notification_schema_with_a_deliberately_long_canonical_identifier"
    leaf["name"] = "Collaboration and Notification Schema Boundary"
    leaf["responsibility"] = (
        "Persist durable collaboration cursors, notification inbox entries, audit-relevant event metadata, "
        "reconnect recovery state, and deliberately verbose ownership details without escaping the inspector card."
    )
    fixture["decisions"] = [
        "Keep durable collaboration state independently evolvable from realtime transport while preserving explicit audit ownership."
    ]
    fixture["risks"] = [
        "A deliberately long risk description verifies that populated inspector cards grow naturally instead of overlapping adjacent panels."
    ]
    fixture["assumptions"] = [
        "Canonical identifiers may be substantially longer than their human-facing component labels."
    ]
    backend.contexts[project_id]["architecture"] = fixture
    linked = [
        task(project_id, "T11", "Implement durable outbox dispatch and ordered realtime fanout", "TODO"),
        task(project_id, "T16", "Generate in-app notifications from committed events", "TODO"),
    ]
    for item in linked:
        item["related_component"] = leaf["id"]
    backend.contexts[project_id]["tasks"] = linked
    identity = "email:inspector@example.com"
    context, page, errors = open_page(browser, backend, viewport={"width": 1440, "height": 900}, identity=identity, project_id=project_id)

    def assert_inspector_geometry() -> None:
        result = page.evaluate("""
        () => {
          const visible = (node) => {
            if (!node) return false;
            const style = getComputedStyle(node);
            const rect = node.getBoundingClientRect();
            return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
          };
          const panels = [...document.querySelectorAll('.graph-side > .graph-side-panel')].filter(visible);
          const overlap = [];
          for (let i = 0; i < panels.length; i += 1) {
            const a = panels[i].getBoundingClientRect();
            for (let j = i + 1; j < panels.length; j += 1) {
              const b = panels[j].getBoundingClientRect();
              const x = Math.min(a.right, b.right) - Math.max(a.left, b.left);
              const y = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
              if (x > 1 && y > 1) overlap.push([panels[i].id, panels[j].id, Math.round(x), Math.round(y)]);
            }
          }
          const overflow = [];
          const scrollOverflow = [];
          for (const panel of panels) {
            const owned = panel.getBoundingClientRect();
            for (const child of panel.querySelectorAll('*')) {
              if (!visible(child)) continue;
              const rect = child.getBoundingClientRect();
              if (rect.left < owned.left - 1 || rect.right > owned.right + 1 || rect.top < owned.top - 1 || rect.bottom > owned.bottom + 1) {
                overflow.push({panel: panel.id, child: child.id || child.className || child.tagName, right: Math.round(rect.right - owned.right), bottom: Math.round(rect.bottom - owned.bottom)});
              }
              if (child.clientWidth > 0 && child.scrollWidth > child.clientWidth + 1) {
                scrollOverflow.push({panel: panel.id, child: child.id || child.className || child.tagName, scrollWidth: child.scrollWidth, clientWidth: child.clientWidth});
              }
            }
          }
          return {overlap, overflow, scrollOverflow, mode: document.querySelector('.graph-side')?.dataset.readingMode || null};
        }
        """)
        assert result["overlap"] == [], result
        assert result["overflow"] == [], result
        assert result["scrollOverflow"] == [], result

    with diagnostic_scope(context.close):
        page.locator(f'[data-project-id="{project_id}"] [data-project-view="architecture"]').click()
        page.locator("#view-architecture").wait_for(state="visible")
        drill_node = page.locator(f'[data-component="{project_id}-experience"][data-node-action="drill"]')
        drill_node.click()
        assert "selected" in (drill_node.get_attribute("class") or "")
        assert page.locator(f'[data-component="{project_id}-experience"][data-projection-role="SCOPE"]').count() == 0
        drill_node.dblclick()
        leaf_node = page.locator('[data-projection-role="PRIMARY"][data-node-action="inspect"]').first
        leaf_node.wait_for(state="visible")
        leaf_node.click()
        page.locator("#graphEvidencePanel").wait_for(state="visible")
        assert page.locator(".graph-side > #selectedNode").count() == 1
        assert page.locator(".graph-panel > #selectedNode").count() == 0
        evidence_box = page.locator("#graphEvidencePanel").bounding_box()
        selected_box = page.locator("#selectedNode").bounding_box()
        assert evidence_box and selected_box and selected_box["y"] >= evidence_box["y"] + evidence_box["height"] - 1
        assert page.locator(".inspector-status-label").evaluate("el => getComputedStyle(el).fontSize") == "12px"
        task_rows = page.locator(".inspector-task-row")
        assert task_rows.count() == 2
        assert "Implement durable outbox dispatch" in task_rows.nth(0).inner_text()
        assert "Generate in-app notifications" in task_rows.nth(1).inner_text()
        for index in range(task_rows.count()):
            style = task_rows.nth(index).evaluate("el => ({border: getComputedStyle(el).borderTopWidth, radius: getComputedStyle(el).borderRadius})")
            assert style["border"] == "1px"
            assert style["radius"] == "10px"

        viewports = [
            {"width": 1600, "height": 900},
            {"width": 1280, "height": 800},
            {"width": 1024, "height": 768},
            {"width": 768, "height": 900},
            {"width": 390, "height": 844},
        ]
        for mode in ["MAP", "READ", "FULL"]:
            page.locator(f'button[data-reading-mode="{mode}"]').click()
            page.wait_for_function(
                "expected => document.querySelector('.graph-side')?.dataset.readingMode === expected",
                arg=mode,
            )
            assert page.locator(".graph-side").get_attribute("data-reading-mode") == mode
            if mode == "MAP":
                assert page.locator("#graphDecisionPanel").is_hidden()
                assert page.locator("#graphRiskPanel").is_hidden()
                assert page.locator(".inspector-read").is_hidden()
                assert page.locator(".inspector-full").is_hidden()
            else:
                assert page.locator("#graphDecisionPanel").is_visible()
                assert page.locator("#graphRiskPanel").is_visible()
                assert page.locator(".inspector-read").is_visible()
                assert page.locator(".inspector-full").is_visible() if mode == "FULL" else page.locator(".inspector-full").is_hidden()
            for viewport in viewports:
                page.set_viewport_size(viewport)
                page.wait_for_timeout(80)
                assert_inspector_geometry()
        assert "inspector-collaboration_and_notification" in page.locator("#nodeEvidence").inner_text()
        assert not errors, errors


def case_canvas_committed_navigation(browser: Browser) -> None:
    project_id = "canvas-route"
    backend = FakeBackend([project(project_id, "Canvas Committed Navigation")])
    backend.contexts[project_id]["architecture"] = canvas_architecture(project_id)
    context, page, errors = open_page(browser, backend, identity="email:canvas-route@example.com", project_id=project_id)
    with diagnostic_scope(context.close):
        page.goto(
            f"{BASE_URL}?canvas=architecture&project={project_id}&node={project_id}-api&tab=dependencies",
            wait_until="networkidle",
        )
        page.locator(f'[data-component="{project_id}-api"].selected').wait_for(state="visible")
        page.locator(f'[data-component="{project_id}-validator"]').dispatch_event("click")
        page.locator('[data-inspector-tab="tasks"]').click()
        page.wait_for_function("() => new URL(location.href).searchParams.get('node') === 'canvas-route-validator'")
        page.locator("#architectureCanvasBtn").click()
        page.wait_for_function("() => document.body.dataset.architectureCanvasMode === 'false'")
        page.locator("#architectureCanvasBtn").click()
        page.wait_for_function("() => document.body.dataset.architectureCanvasMode === 'true'")
        page.locator('[data-component="canvas-route-validator"].selected').wait_for(state="visible")
        assert page.locator('[data-inspector-tab="tasks"]').get_attribute("aria-pressed") == "true"
        assert parse_qs(urlsplit(page.url).query)["node"] == ["canvas-route-validator"]
        assert parse_qs(urlsplit(page.url).query)["tab"] == ["tasks"]

        # Graph-kind presentation is not a navigation intent. Returning to the
        # Living graph must restore the same committed Living selection.
        page.locator('[data-architecture-graph-kind="code"]').click()
        page.wait_for_function("() => document.querySelector('#graphCanvas')?.dataset.graphKind === 'code'")
        page.locator('[data-architecture-graph-kind="living"]').click()
        page.wait_for_function("() => document.querySelector('#graphCanvas')?.dataset.graphKind === 'living'")
        page.locator('[data-component="canvas-route-validator"].selected').wait_for(state="visible")
        committed = page.evaluate("() => window.ArchBroWebBridge.getCommittedNavigation()")
        assert committed["node_id"] == "canvas-route-validator"
        assert committed["inspector_tab"] == "tasks"

        # Collapsing a selected descendant promotes the visible group owner and
        # synchronizes that selection through the navigation authority.
        core_fold = page.locator('.canvas-group-fold[data-fold-group="node:canvas-route-core"]')
        core_fold.click()
        page.wait_for_function("() => new URL(location.href).searchParams.get('node') === 'canvas-route-core'")
        page.locator('[data-component="canvas-route-core"].selected').wait_for(state="visible")
        committed = page.evaluate("() => window.ArchBroWebBridge.getCommittedNavigation()")
        assert committed["node_id"] == "canvas-route-core"

        # A history route back to a descendant hidden by that collapse must
        # expand its ancestors before deriving the visible projection.
        page.evaluate("""() => {
            const url = new URL(location.href);
            url.searchParams.set('node', 'canvas-route-validator');
            url.searchParams.set('tab', 'tasks');
            history.pushState({}, '', url);
            window.dispatchEvent(new PopStateEvent('popstate'));
        }""")
        page.locator('[data-component="canvas-route-validator"].selected').wait_for(state="visible")
        assert page.locator('.canvas-group-summary[data-fold-group="node:canvas-route-core"]').count() == 0
        committed = page.evaluate("() => window.ArchBroWebBridge.getCommittedNavigation()")
        assert committed["node_id"] == "canvas-route-validator"

        # An explicit Canvas history entry without a node owns the empty
        # selection. It must not inherit the previous entry's local selection.
        page.evaluate("""() => {
            const url = new URL(location.href);
            url.searchParams.delete('node');
            url.searchParams.delete('tab');
            history.pushState({}, '', url);
            window.dispatchEvent(new PopStateEvent('popstate'));
        }""")
        page.wait_for_function("""() =>
            window.ArchBroWebBridge.getCommittedNavigation().node_id === null &&
            !document.querySelector('.node-card.selected')
        """)
        assert page.locator("#view-architecture .graph-layout").evaluate("node => !node.classList.contains('has-canvas-inspector')")
        assert "node" not in parse_qs(urlsplit(page.url).query)
        assert "tab" not in parse_qs(urlsplit(page.url).query)

        page.evaluate("""() => {
            const url = new URL(location.href);
            url.searchParams.set('node', 'missing-node');
            url.searchParams.set('tab', 'code');
            history.pushState({}, '', url);
            window.dispatchEvent(new PopStateEvent('popstate'));
        }""")
        page.wait_for_function("() => !new URL(location.href).searchParams.has('node')")
        assert page.locator(".node-card.selected").count() == 0
        assert not errors, errors


def case_architecture_canvas_interactions(browser: Browser) -> None:
    project_id = "canvas-flow"
    backend = FakeBackend([project(project_id, "Architecture Canvas Flow")])
    backend.contexts[project_id]["architecture"] = canvas_architecture(project_id)
    identity = "email:canvas-flow@example.com"
    context, page, errors = open_page(
        browser,
        backend,
        viewport={"width": 1440, "height": 900},
        identity=identity,
        project_id=project_id,
    )
    with diagnostic_scope(context.close):
        api_id = f"{project_id}-api"
        validator_id = f"{project_id}-validator"
        core_id = f"{project_id}-core"
        catalog_id = f"{project_id}-catalog"
        data_id = f"{project_id}-data"
        viewer_id = f"{project_id}-viewer"
        worker_id = f"{project_id}-worker"
        page.goto(
            f"{BASE_URL}?canvas=architecture&project={project_id}&node={api_id}&tab=dependencies",
            wait_until="networkidle",
        )
        page.locator("#view-architecture").wait_for(state="visible")
        page.locator(".living-graph-svg").wait_for(state="visible")

        api_node = page.locator(f'[data-component="{api_id}"]')
        api_node.wait_for(state="visible")
        assert "selected" in (api_node.get_attribute("class") or "")
        assert page.locator('[data-inspector-tab="dependencies"]').get_attribute("aria-pressed") == "true"
        assert page.locator(".node-card[data-node]").count() == 8

        # Leave full-system Canvas through the selected child, then reopen it.
        # Project may remember a scoped view, but Canvas must always restore the
        # full-system projection and the same canonical selection.
        page.locator("#architectureCanvasBtn").click()
        page.wait_for_function("() => document.body.dataset.architectureCanvasMode === 'false'")
        assert page.locator(".node-card[data-node]").count() < 8
        page.locator("#architectureCanvasBtn").click()
        page.wait_for_function("() => document.body.dataset.architectureCanvasMode === 'true'")
        assert page.locator(".node-card[data-node]").count() == 8
        assert "selected" in (page.locator(f'[data-component="{api_id}"]').get_attribute("class") or "")
        centered = page.evaluate(
            f"""
            () => {{
              const node = document.querySelector('[data-component="{api_id}"]')?.getBoundingClientRect();
              const svgElement = document.querySelector('.living-graph-svg');
              const svg = svgElement?.getBoundingClientRect();
              const graphNode = document.querySelector('[data-component="{api_id}"]')?.getBBox();
              const viewBox = svgElement?.viewBox?.baseVal;
              if (!node || !svg || !graphNode || !viewBox) return null;
              return {{dx:Math.abs((node.left+node.width/2)-(svg.left+svg.width/2)),dy:Math.abs((node.top+node.height/2)-(svg.top+svg.height/2)),width:svg.width,height:svg.height,graphDx:Math.abs((graphNode.x+graphNode.width/2)-(viewBox.x+viewBox.width/2)),graphDy:Math.abs((graphNode.y+graphNode.height/2)-(viewBox.y+viewBox.height/2)),viewBox:svgElement.getAttribute('viewBox'),fitViewBox:svgElement.dataset.fitViewBox}};
            }}
            """
        )
        assert centered and centered["graphDx"] < 1 and centered["graphDy"] < 1, centered

        # Viewport controls are browser behavior, not source assertions. Fit
        # returns to the backend-authored graph bounds; 100% maps one graph unit
        # to one CSS pixel; +/- and wheel change disclosure without topology;
        # background drag pans by moving only the local viewBox.
        canvas = page.locator("#graphCanvas")
        viewport_svg = page.locator(".living-graph-svg")

        def current_view_box() -> list[float]:
            return viewport_svg.evaluate(
                "svg => { const box=svg.viewBox.baseVal; return [box.x,box.y,box.width,box.height]; }"
            )

        fit_view_box = [float(value) for value in (viewport_svg.get_attribute("data-fit-view-box") or "").split()]
        def assert_fit_view_box() -> None:
            rendered = current_view_box()
            svg_rect = viewport_svg.bounding_box()
            assert svg_rect and svg_rect["width"] > 0 and svg_rect["height"] > 0
            assert rendered[0] <= fit_view_box[0] + 0.01
            assert rendered[1] <= fit_view_box[1] + 0.01
            assert rendered[0] + rendered[2] >= fit_view_box[0] + fit_view_box[2] - 0.01
            assert rendered[1] + rendered[3] >= fit_view_box[1] + fit_view_box[3] - 0.01
            assert abs((rendered[2] / rendered[3]) - (svg_rect["width"] / svg_rect["height"])) < 0.001
        page.locator('[data-graph-viewport="fit"]').click()
        assert_fit_view_box()
        page.locator('[data-graph-viewport="actual"]').click()
        page.wait_for_function("() => document.querySelector('[data-graph-zoom]')?.textContent === '100%'")
        assert canvas.get_attribute("data-zoom-tier") == "detail"
        # Visibility alone ignores opacity: assert the actual rendered detail
        # for selected and unselected nodes, not just the zoom-tier attribute.
        page.wait_for_function(
            """() => [...document.querySelectorAll('.node-card .graph-detail-read')]
              .every(node => getComputedStyle(node).opacity === '1') &&
              [...document.querySelectorAll('.node-card .graph-detail-full')]
              .every(node => getComputedStyle(node).opacity === '0')""",
            timeout=2000,
        )
        page.locator('[data-graph-viewport="zoom-in"]').click()
        assert canvas.get_attribute("data-zoom-tier") == "full"
        page.wait_for_function(
            """() => [...document.querySelectorAll('.node-card .graph-detail-read, .node-card .graph-detail-full')]
              .every(node => getComputedStyle(node).opacity === '1')""",
            timeout=2000,
        )
        for _ in range(4):
            page.locator('[data-graph-viewport="zoom-out"]').click()
        assert canvas.get_attribute("data-zoom-tier") == "overview"
        page.wait_for_function(
            """() => [...document.querySelectorAll('.node-card .graph-detail-read, .node-card .graph-detail-full')]
              .every(node => getComputedStyle(node).opacity === '0' && getComputedStyle(node).pointerEvents === 'none')""",
            timeout=2000,
        )

        page.locator('[data-graph-viewport="actual"]').click()
        wheel_before = current_view_box()
        svg_box = viewport_svg.bounding_box()
        assert svg_box
        page.mouse.move(svg_box["x"] + svg_box["width"] / 2, svg_box["y"] + svg_box["height"] / 2)
        page.mouse.wheel(0, -240)
        wheel_after = current_view_box()
        assert wheel_after[2] < wheel_before[2], (wheel_before, wheel_after)

        pan_before = current_view_box()
        page.mouse.move(svg_box["x"] + 8, svg_box["y"] + 8)
        page.mouse.down()
        page.mouse.move(svg_box["x"] + 88, svg_box["y"] + 48)
        page.mouse.up()
        pan_after = current_view_box()
        assert pan_after[:2] != pan_before[:2], (pan_before, pan_after)
        page.locator('[data-graph-viewport="fit"]').click()
        assert_fit_view_box()

        # Holding Space explicitly enables panning even when drag begins over
        # an interactive node; the synthetic click after movement is suppressed.
        api_node = page.locator(f'[data-component="{api_id}"]')
        api_node.focus()
        node_box = api_node.bounding_box()
        assert node_box
        node_pan_before = current_view_box()
        page.keyboard.down("Space")
        assert "Drag anywhere to pan" in page.locator('[data-graph-pan-hint]').inner_text()
        page.mouse.move(node_box["x"] + node_box["width"] / 2, node_box["y"] + node_box["height"] / 2)
        page.mouse.down()
        page.mouse.move(node_box["x"] + node_box["width"] / 2 + 72, node_box["y"] + node_box["height"] / 2 + 36)
        page.mouse.up()
        page.keyboard.up("Space")
        assert "Hold Space + drag to pan" in page.locator('[data-graph-pan-hint]').inner_text()
        node_pan_after = current_view_box()
        assert node_pan_after[:2] != node_pan_before[:2], (node_pan_before, node_pan_after)
        assert "selected" in (page.locator(f'[data-component="{api_id}"]').get_attribute("class") or "")
        page.locator('[data-graph-viewport="fit"]').click()
        assert_fit_view_box()

        context_tray = page.locator("#agentContextTray")
        context_tray.wait_for(state="visible")
        page.wait_for_function(
            "() => document.querySelector('#agentContextTrayBody')?.textContent.includes('Within budget')"
        )
        assert page.locator("#agentContextTrayTitle").inner_text() == "API"
        tray_text = page.locator("#agentContextTrayBody").inner_text()
        for expected in [
            "SELECTION",
            "API",
            "PARENT",
            "Core Platform",
            "CHILDREN",
            "Validator",
            "DEPENDENCY NEIGHBORHOOD",
            "CODE TRUTH",
            "NO_SNAPSHOT",
            "Not included until explicitly gathered",
            "ASK_ALL",
        ]:
            assert expected in tray_text, (expected, tray_text)
        assert backend.context_manifest_requests[-1]["request"] == {
            "node_id": f"node:{api_id}",
            "direction": "both",
            "expansion_policy": "ASK_ALL",
            "expected_architecture_version": 7,
        }

        preview_count = len(backend.context_manifest_requests)
        page.locator("#agentContextPolicy").select_option("ALLOW_NEIGHBORHOOD")
        page.wait_for_function(
            "() => document.querySelector('#agentContextTrayBody')?.textContent.includes('ALLOW_NEIGHBORHOOD')",
            arg=preview_count,
        )
        assert len(backend.context_manifest_requests) > preview_count
        assert backend.context_manifest_requests[-1]["request"]["expansion_policy"] == "ALLOW_NEIGHBORHOOD"
        page.locator("#agentContextTelemetryToggle").click()
        assert page.locator(".agent-context-telemetry").count() == 0
        page.locator("#agentContextTelemetryToggle").click()
        assert page.locator(".agent-context-telemetry").count() == 1

        # The UI sends only request + preview hash. A project fact changed after
        # preview must fail closed before the fake provider is accepted, preserve
        # the message, and rebuild the preview for a deliberate retry.
        stale_preview_hash = backend.context_manifest_requests[-1]["manifest"]["manifest_hash"]
        new_context_task = task(project_id, "task-context-new", "New bounded API fact", "TODO")
        new_context_task["related_component"] = api_id
        backend.contexts[project_id]["tasks"].append(new_context_task)
        bounded_message = "Review the selected API boundary using only the previewed context."
        page.locator("#instruction").fill(bounded_message)
        page.locator("#instructionForm button[type='submit']").click()
        page.wait_for_function(
            "() => document.querySelector('#toast')?.textContent.includes('agent_context_preview_stale')"
        )
        assert page.locator("#instruction").input_value() == bounded_message
        assert backend.event_requests == []
        page.wait_for_function(
            "oldHash => document.querySelector('#agentContextTrayBody code')?.textContent !== oldHash.slice(0, 12)",
            arg=stale_preview_hash,
        )

        page.locator("#instructionForm button[type='submit']").click()
        page.wait_for_function("() => document.querySelector('#instruction')?.value === ''")
        assert len(backend.event_requests) == 1
        sent = backend.event_requests[0]
        sent_payload = sent["body"]["payload"]
        assert sent_payload["message"] == bounded_message
        assert "agent_context_manifest" not in sent_payload
        assert sent_payload["agent_context_request"]["node_id"] == f"node:{api_id}"
        assert sent_payload["agent_context_request"]["expansion_policy"] == "ALLOW_NEIGHBORHOOD"
        assert sent_payload["agent_context_request"]["preview_manifest_hash"] == sent["server_manifest"]["manifest_hash"]
        page.wait_for_function(
            "() => document.querySelector('#agentContextTrayBody')?.textContent.includes('1,234 tokens')"
        )
        assert "New bounded API fact" in page.locator("#agentContextTrayBody").inner_text()

        # Explore from here uses the same exact server-owned manifest contract as
        # Ask Agent: selected node + architecture version + preview hash only.
        with page.expect_response(
            lambda response: response.request.method == "GET"
            and urlsplit(response.url).path == f"/projects/{project_id}/architecture/canvas"
        ) as explore_refresh_response:
            with page.expect_response(
                lambda response: response.request.method == "POST"
                and urlsplit(response.url).path == f"/projects/{project_id}/events"
            ) as explore_response:
                page.locator('[data-agent-explore]').click()
        assert explore_response.value.status == 200
        assert explore_refresh_response.value.status == 200
        assert len(backend.event_requests) == 2
        explored = backend.event_requests[-1]
        explored_payload = explored["body"]["payload"]
        assert explored_payload["message"] == "Explore from here: API"
        assert explored_payload["agent_context_request"]["node_id"] == f"node:{api_id}"
        assert explored_payload["agent_context_request"]["expected_architecture_version"] == 7
        assert explored_payload["agent_context_request"]["preview_manifest_hash"] == explored["server_manifest"]["manifest_hash"]
        assert "agent_context_manifest" not in explored_payload

        canvas_reads = lambda: len([item for item in backend.requests if item["method"] == "GET" and item["path"] == f"/projects/{project_id}/architecture/canvas"])
        reads_before_tabs = canvas_reads()
        for tab in ("tasks", "evidence", "code", "decisions", "overview", "dependencies"):
            page.locator(f'[data-inspector-tab="{tab}"]').click()
            assert page.locator(f'[data-inspector-tab="{tab}"]').get_attribute("aria-pressed") == "true"
        assert canvas_reads() == reads_before_tabs

        validator_node = page.locator(f'[data-component="{validator_id}"]')
        validator_node.click(force=True)
        assert "selected" in (validator_node.get_attribute("class") or "")
        assert page.locator(".graph-edge.selected").count() == 0

        # Trace Path consumes the backend-authored directed-path endpoint. The
        # browser only highlights returned node/relationship IDs; it never walks
        # the graph to invent a route.
        path_reads_before = len([
            item for item in backend.requests
            if item["method"] == "GET" and item["path"] == f"/projects/{project_id}/architecture/path"
        ])
        page.locator('[data-trace-target]').select_option(f"node:{catalog_id}")
        page.locator('[data-trace-path]').click()
        page.wait_for_function(
            "() => document.querySelector('[data-trace-status]')?.textContent.includes('FOUND')"
        )
        path_reads = [
            item for item in backend.requests
            if item["method"] == "GET" and item["path"] == f"/projects/{project_id}/architecture/path"
        ]
        assert len(path_reads) == path_reads_before + 1
        trace_query = parse_qs(urlsplit(path_reads[-1]["url"]).query)
        assert trace_query == {
            "source_id": [f"node:{validator_id}"],
            "target_id": [f"node:{catalog_id}"],
            "max_hops": ["8"],
            "expected_architecture_version": ["7"],
        }
        assert "1 hop" in page.locator('[data-trace-status]').inner_text()
        assert "is-dimmed" not in (page.locator(f'[data-component="{validator_id}"]').get_attribute("class") or "")
        assert "is-dimmed" not in (page.locator(f'[data-component="{catalog_id}"]').get_attribute("class") or "")
        assert "is-dimmed" in (page.locator(f'[data-component="{viewer_id}"]').get_attribute("class") or "")

        page.locator(f'[data-component="{catalog_id}"]').click(force=True)
        page.locator('[data-trace-target]').select_option(f"node:{validator_id}")
        page.locator('[data-trace-path]').click()
        page.wait_for_function(
            "() => document.querySelector('[data-trace-status]')?.textContent.includes('No authored directed path')"
        )
        page.locator(f'[data-component="{validator_id}"]').click(force=True)

        selected_edge = page.locator('.graph-edge[data-edge][data-summary-id=""]').first
        selected_edge.dispatch_event("click")
        assert page.locator(".graph-edge.selected").count() == 1
        assert page.locator(".node-card.selected").count() == 0
        assert "SELECTED RELATIONSHIP" in page.locator("#selectedNode").inner_text()
        page.locator('[data-inspector-tab="dependencies"]').click()
        page.locator("[data-inspect-component]").first.click()
        assert page.locator(".node-card.selected").count() == 1
        assert page.locator(".graph-edge.selected").count() == 0

        api_node = page.locator(f'[data-component="{api_id}"]')
        api_node.click(force=True)
        page.locator('[data-graph-focus="hierarchy"]').click()
        assert page.locator(f'[data-component="{validator_id}"]').is_visible()
        assert page.locator(f'[data-component="{viewer_id}"]').is_visible()
        assert "is-dimmed" in (page.locator(f'[data-component="{viewer_id}"]').get_attribute("class") or "")

        page.locator('[data-graph-focus="isolate"]').click()
        for visible_component in (api_id, validator_id, core_id, catalog_id, data_id):
            assert page.locator(f'[data-component="{visible_component}"]').is_visible(), visible_component
        for hidden_component in (viewer_id, worker_id):
            assert page.locator(f'[data-component="{hidden_component}"]').is_hidden(), hidden_component
        assert "is-boundary-context" in (page.locator(f'[data-component="{catalog_id}"]').get_attribute("class") or "")
        assert "is-boundary-context" in (page.locator(f'[data-component="{data_id}"]').get_attribute("class") or "")
        assert page.locator(".graph-edge[data-edge]:visible").count() == 1

        page.locator('[data-graph-focus="clear"]').click()
        assert page.locator(".node-card[data-node]:visible").count() == 8
        assert page.locator(".graph-edge[data-edge]:visible").count() == 2
        assert canvas_reads() == reads_before_tabs

        validator_geometry = page.locator(f'[data-component="{validator_id}"]').evaluate(
            "el => { const box=el.getBBox(); return [box.x,box.y,box.width,box.height]; }"
        )
        page.locator(f'[data-component="{api_id}"]').click(force=True)
        page.locator('[data-toggle-collapse]').click()
        api_node = page.locator(f'[data-component="{api_id}"]')
        assert api_node.get_attribute("data-collapsed") == "true"
        assert page.locator(f'[data-component="{validator_id}"]').count() == 0
        assert page.locator('[data-expand-all]').is_visible()

        # Stable-ID picker uses the same local Canvas reveal path: expand only
        # the target ancestors and never refetch a scoped Project diagram.
        picker_reads_before = canvas_reads()
        picker = page.locator('[data-component-picker]')
        picker.fill(validator_id)
        picker.press("Enter")
        validator_node = page.locator(f'[data-component="{validator_id}"]')
        validator_node.wait_for(state="visible")
        assert "selected" in (validator_node.get_attribute("class") or "")
        assert page.locator(f'[data-component="{api_id}"]').get_attribute("data-collapsed") == "false"
        assert canvas_reads() == picker_reads_before

        page.locator(f'[data-component="{api_id}"]').dispatch_event("click")
        page.locator('[data-toggle-collapse]').click()
        assert page.locator(f'[data-component="{validator_id}"]').count() == 0
        collapsed_sql = page.locator('.graph-edge.relationship-data')
        assert collapsed_sql.is_visible()
        assert "projection-collapsed" in (collapsed_sql.get_attribute("class") or "")
        collapsed_sql.focus()
        page.keyboard.press("Enter")
        relationship_heading = page.locator("#selectedNode h3").inner_text()
        assert "Validator" in relationship_heading and "Catalog" in relationship_heading, relationship_heading

        # Inspecting a hidden canonical endpoint expands only its collapsed
        # ancestor, then selects the original canonical node.
        page.locator('[data-inspector-tab="dependencies"]').click()
        page.locator(f'[data-inspect-component="{validator_id}"]').click()
        validator_node = page.locator(f'[data-component="{validator_id}"]')
        assert validator_node.is_visible()
        assert "selected" in (validator_node.get_attribute("class") or "")
        assert page.locator(f'[data-component="{api_id}"]').get_attribute("data-collapsed") == "false"
        assert validator_node.evaluate(
            "el => { const box=el.getBBox(); return [box.x,box.y,box.width,box.height]; }"
        ) == validator_geometry

        # Expand all is the explicit reset and restores exact backend geometry.
        page.locator(f'[data-component="{api_id}"]').click(force=True)
        page.locator('[data-toggle-collapse]').click()
        assert page.locator('[data-expand-all]').is_visible()
        page.locator('[data-expand-all]').click()
        assert page.locator('[data-expand-all]').count() == 0
        assert page.locator(f'[data-component="{validator_id}"]').evaluate(
            "el => { const box=el.getBBox(); return [box.x,box.y,box.width,box.height]; }"
        ) == validator_geometry

        # Collapse composes after canonical projection and before Isolate:
        # the hidden descendant stays hidden while its crossing dependency and
        # external boundary context remain inspectable.
        page.locator(f'[data-component="{api_id}"]').click(force=True)
        page.locator('[data-toggle-collapse]').click()
        page.locator('[data-graph-focus="isolate"]').click()
        for visible_component in (api_id, core_id, catalog_id, data_id):
            assert page.locator(f'[data-component="{visible_component}"]').is_visible(), visible_component
        for hidden_component in (validator_id, viewer_id, worker_id):
            assert page.locator(f'[data-component="{hidden_component}"]').count() == 0 or page.locator(f'[data-component="{hidden_component}"]').is_hidden(), hidden_component
        assert page.locator('.graph-edge.relationship-data:visible').count() == 1
        page.locator('[data-graph-focus="clear"]').click()
        assert page.locator(".node-card[data-node]:visible").count() == 7
        page.locator('[data-expand-all]').click()
        assert page.locator(".node-card[data-node]:visible").count() == 8
        assert canvas_reads() == reads_before_tabs

        page.goto(
            f"{BASE_URL}?canvas=architecture&project={project_id}&node=missing-node&tab=code",
            wait_until="networkidle",
        )
        page.locator("#view-architecture").wait_for(state="visible")
        assert page.locator(".node-card.selected").count() == 0
        assert page.locator(".node-card[data-node]:visible").count() == 8
        assert page.locator('[data-collapsed="true"]').count() == 0
        assert "SYSTEM ARCHITECTURE" in page.locator("#selectedNode").inner_text()
        normalized_query = parse_qs(urlsplit(page.url).query)
        assert "node" not in normalized_query and "tab" not in normalized_query
        assert len(backend.event_requests) == 2
        architecture_mutations = [
            item
            for item in backend.requests
            if "/architecture" in item["path"] and item["method"] != "GET"
        ]
        assert architecture_mutations == []
        assert not errors, errors


def case_instruction_failure(browser: Browser) -> None:
    backend = FakeBackend([project("alpha", "Instruction Project")])
    backend.contexts["alpha"]["tasks"] = [task("alpha", "task-a", "Preserve this context", "TODO")]
    backend.fail_next("POST", "/projects/alpha/events")
    identity = "email:instruction@example.com"
    context, page, errors = open_page(browser, backend, identity=identity, project_id="alpha")
    with diagnostic_scope(context.close):
        page.locator('[data-project-view="tasks"]').click()
        page.locator('#taskList [data-task-open="task-a"]').click()
        message = "Do not erase this detailed instruction after a transient failure."
        page.locator("#instruction").fill(message)
        page.locator("#instructionForm button[type='submit']").click()
        page.locator("#toast.error").wait_for(state="visible")
        assert page.locator("#instruction").input_value() == message
        assert page.evaluate("() => document.activeElement?.id") == "instruction"
        assert "Preserve this context" in page.locator("#instructionContext").inner_text()
        backend.event_result = "ERROR"
        page.locator("#instructionForm button[type='submit']").click()
        page.wait_for_function("() => document.querySelector('#globalAgentReply')?.classList.contains('error')")
        assert page.locator("#instruction").input_value() == message
        assert page.evaluate("() => document.activeElement?.id") == "instruction"
        assert "Preserve this context" in page.locator("#instructionContext").inner_text()
        assert not errors, errors


def case_inline_rename_and_account(browser: Browser) -> None:
    backend = FakeBackend([project("alpha", "Rename Project")])
    identity = "email:rename@example.com"
    context, page, errors = open_page(browser, backend, identity=identity, project_id="alpha")
    with diagnostic_scope(context.close):
        menu_trigger = page.locator('[data-project-id="alpha"] [data-project-menu]')
        menu_trigger.click()
        rename = page.locator('[data-project-id="alpha"] [data-project-action="rename"]')
        rename.click()
        page.keyboard.press("Escape")
        page.wait_for_function("() => document.activeElement?.hasAttribute('data-project-menu')")
        menu_trigger.click()
        page.locator('[data-project-id="alpha"] [data-project-action="rename"]').click()
        page.locator("[data-project-rename-input]").fill("   ")
        page.keyboard.press("Enter")
        assert "Enter a project name" in page.locator("[data-project-rename-error]").inner_text()
        backend.fail_next("PATCH", "/projects/alpha")
        page.locator("[data-project-rename-input]").fill("Still Editable")
        page.keyboard.press("Enter")
        page.wait_for_function("() => document.querySelector('[data-project-rename-error]')?.textContent.includes('503')")
        assert page.locator("[data-project-rename-input]").input_value() == "Still Editable"
        assert "503" in page.locator("[data-project-rename-error]").inner_text()
        page.locator("[data-project-rename-input]").fill("Renamed Inline")
        page.keyboard.press("Enter")
        page.wait_for_function("() => document.querySelector('[data-project-open]')?.textContent.trim() === 'Renamed Inline'")

        page.evaluate("() => document.querySelector('#accountBtn').click()")
        page.locator('[data-account-section="profile"]').click()
        page.locator("#settingsName").fill("Updated Reviewer")
        page.locator("#accountSettingsForm button[type='submit']").click()
        assert "Updated Reviewer" in page.locator("#accountBtn").get_attribute("aria-label")
        assert page.locator("#workspaceSidebar .sidebar-footer").count() == 0
        page.locator("#accountBtn").click()
        page.locator('[data-account-section="settings"]').click()
        page.locator("#settingsBlockedNotifications").uncheck()
        page.locator("#accountSettingsForm button[type='submit']").click()
        page.locator("#accountBtn").click()
        page.locator('[data-account-section="settings"]').click()
        assert not page.locator("#settingsBlockedNotifications").is_checked()
        assert not errors, errors


def case_project_row_action_menu(browser: Browser) -> None:
    backend = FakeBackend([project("alpha", "Alpha Project"), project("beta", "Beta Project")])
    identity = "email:menu@example.com"
    context, page, errors = open_page(browser, backend, identity=identity, project_id="alpha")
    with diagnostic_scope(context.close):
        trigger = page.locator('[data-project-id="alpha"] [data-project-menu]')
        trigger.wait_for(state="visible", timeout=5000)
        assert trigger.get_attribute("aria-haspopup") == "menu"
        assert trigger.get_attribute("aria-expanded") == "false"

        trigger.click()
        menu = page.locator('[data-project-id="alpha"] [data-project-menu-panel]')
        menu.wait_for(state="visible", timeout=5000)
        assert trigger.get_attribute("aria-expanded") == "true"
        menu_text = menu.inner_text().lower()
        for label in ["edit project", "rename project", "delete project"]:
            assert label in menu_text

        page.mouse.click(8, 8)
        page.wait_for_function(
            "id => !document.querySelector(`[data-project-id=\"${id}\"] [data-project-menu-panel]`) && document.querySelector(`[data-project-id=\"${id}\"] [data-project-menu]`)?.getAttribute('aria-expanded') === 'false'",
            arg="alpha",
        )
        page.wait_for_function("() => document.activeElement?.dataset.projectMenu !== undefined")

        trigger.click()
        page.locator('[data-project-id="beta"] [data-project-toggle]').click()
        page.wait_for_function("() => document.querySelector('[data-project-id=\"beta\"] [data-project-toggle]')?.getAttribute('aria-expanded') === 'true'")
        trigger = page.locator('[data-project-id="alpha"] [data-project-menu]')
        trigger.wait_for(state="visible", timeout=5000)

        trigger.click()
        menu.locator('[data-project-action="edit"]').click()
        page.locator("#editProjectDialog").wait_for(state="visible", timeout=5000)
        page.locator('[data-close-dialog="editProjectDialog"]').first.click()
        page.wait_for_function("() => !document.querySelector('#editProjectDialog')?.open")
        page.wait_for_function("() => document.activeElement?.matches('[data-project-id=\"alpha\"] [data-project-menu]')")
        trigger.click()
        menu.locator('[data-project-action="delete"]').click()
        page.locator("#deleteProjectDialog").wait_for(state="visible", timeout=5000)
        assert "alpha project" in page.locator("#deleteProjectDialog").inner_text().lower()
        page.keyboard.press("Escape")
        page.wait_for_function(
            "id => !document.querySelector(`[data-project-id=\"${id}\"] [data-project-menu-panel]`)",
            arg="alpha",
        )

        trigger.click()
        page.locator("#accountBtn").click()
        page.wait_for_function("() => !document.querySelector('[data-project-id=\"alpha\"] [data-project-menu-panel]')")
        page.wait_for_function("() => document.activeElement?.dataset.accountSection === 'profile'")
        page.keyboard.press("Escape")
        page.wait_for_function("() => document.activeElement?.id === 'accountBtn'")

        trigger.click()
        page.locator("#notificationBtn").click()
        page.wait_for_function("() => !document.querySelector('[data-project-id=\"alpha\"] [data-project-menu-panel]')")
        page.wait_for_function("() => document.activeElement?.id === 'notificationCloseBtn'")
        assert not errors, errors


def case_autonomous_surface_sweep(browser: Browser) -> None:
    """Visit core pages and important UI states, then record objective visual/runtime failures."""
    identity = "email:surface-sweep@example.com"
    backend = FakeBackend([project("sweep", "Autonomous Surface Sweep")])
    backend.contexts["sweep"]["tasks"] = [
        task("sweep", "task-todo", "Review fixture TODO with a deliberately long task title that must wrap without clipping", status="TODO"),
        task("sweep", "task-progress", "Review fixture progress", status="IN_PROGRESS"),
        task("sweep", "task-blocked", "Review fixture blocker", status="BLOCKED"),
        task("sweep", "task-done", "Review fixture completion", status="DONE"),
    ]
    # Start with Needs You empty; later phases add a pending proposal to cover review states.
    backend.contexts["sweep"]["proposals"] = []
    backend.contexts["sweep"]["architecture"] = {**architecture("sweep"), "components": []}

    context = browser.new_context(viewport={"width": 1440, "height": 900})
    sweep_profile = profile(identity, notifications={"architectureApprovals": True, "blockedTasks": False})
    add_storage(context, identity=identity, profiles={identity: sweep_profile}, project_id="sweep")
    context.route("**/projects**", backend.handle)
    context.route("**/onboarding/goal", backend.handle)

    def handle_mcp(route: Route) -> None:
        path = urlsplit(route.request.url).path
        if path == "/mcp/connections":
            route.fulfill(status=200, content_type="application/json", body="[]")
            return
        if path.startswith("/mcp/auth/github/status"):
            payload = {"name": "GitHub", "configured": False, "connected": False, "message": "Fixture status"}
        elif path.startswith("/mcp/auth/google-drive/status"):
            payload = {"name": "Google Drive", "configured": False, "connected": False, "message": "Fixture status", "prerequisites": {"ready": False}}
        elif path.startswith("/mcp/oauth/") and path.endswith("/status"):
            provider_id = path.split("/")[3]
            payload = {"name": provider_id.replace("-", " ").title(), "configured": False, "connected": False, "missing_configuration": ["fixture"]}
        else:
            route.continue_()
            return
        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

    context.route("**/mcp/**", handle_mcp)
    console_errors: list[str] = []
    page_errors: list[str] = []
    http_errors: list[dict] = []
    page = context.new_page()
    page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    page.on("response", lambda response: http_errors.append({"status": response.status, "url": response.url}) if response.status >= 400 else None)

    surface_report: dict = {
        "schema": "archbro.frontend_surface_sweep.v1",
        "base_url": BASE_URL,
        "coverage": {
            "scope": "core-pages-and-important-states",
            "exhaustive_components": False,
        },
        "surfaces": [],
        "runtime": {"status": "RUNNING", "console_errors": [], "page_errors": [], "http_errors": []},
        "fatal": None,
        "result": "RUNNING",
    }
    fatal_error: Exception | None = None

    def set_scroll_to_end() -> None:
        page.evaluate("""
        () => {
          const main = document.querySelector('#workspaceMain');
          if (main && main.scrollHeight > main.clientHeight + 2) {
            main.scrollTop = main.scrollHeight;
          } else {
            window.scrollTo(0, document.documentElement.scrollHeight);
          }
        }
        """)

    def inspect_surface(expect_scroll_reset: bool) -> dict:
        result = page.evaluate("""
        async ({expectScrollReset}) => {
          const issues = [];
          const add = (type, message, detail = {}) => issues.push({type, message, ...detail});
          const visible = (node) => {
            if (!node || node.closest('.hidden')) return false;
            const style = getComputedStyle(node);
            const rect = node.getBoundingClientRect();
            return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
          };
          const roundedRect = (rect) => ({
            top: Math.round(rect.top),
            right: Math.round(rect.right),
            bottom: Math.round(rect.bottom),
            left: Math.round(rect.left),
            width: Math.round(rect.width),
            height: Math.round(rect.height),
          });

          const width = innerWidth;
          const height = innerHeight;
          const overflowElements = () => [...document.querySelectorAll('body *')]
            .filter(visible)
            .map(node => ({node, rect: node.getBoundingClientRect()}))
            .filter(({rect}) => rect.right > width + 2 || rect.left < -2)
            .sort((a, b) => Math.max(b.rect.right - width, -b.rect.left) - Math.max(a.rect.right - width, -a.rect.left))
            .slice(0, 8)
            .map(({node, rect}) => ({
              element: node.id ? `#${node.id}` : node.classList.length ? `${node.tagName.toLowerCase()}.${[...node.classList].slice(0, 3).join('.')}` : node.tagName.toLowerCase(),
              rect: roundedRect(rect),
            }));
          if (document.body.scrollWidth > width + 2) {
            add('horizontal-overflow', 'Body is wider than the viewport.', {scroll_width: document.body.scrollWidth, viewport_width: width, offenders: overflowElements()});
          }
          if (document.documentElement.scrollWidth > width + 2) {
            add('horizontal-overflow', 'Document root is wider than the viewport.', {scroll_width: document.documentElement.scrollWidth, viewport_width: width, offenders: overflowElements()});
          }

          const ids = [...document.querySelectorAll('[id]')].map(node => node.id).filter(Boolean);
          const duplicateIds = [...new Set(ids.filter((id, index) => ids.indexOf(id) !== index))];
          if (duplicateIds.length) {
            add('duplicate-id', 'Duplicate DOM ids are present.', {ids: duplicateIds});
          }

          const main = document.querySelector('#workspaceMain');
          const scroll = {
            window_y: Math.round(window.scrollY),
            workspace_main: Math.round(main?.scrollTop || 0),
          };
          if (expectScrollReset && (Math.abs(scroll.window_y) > 2 || Math.abs(scroll.workspace_main) > 2)) {
            add('stale-scroll', 'Project view did not return to the safe top position after navigation.', scroll);
          }

          const topbar = document.querySelector('.topbar');
          const active = document.querySelector('.view.active');
          if (visible(topbar) && visible(active)) {
            const heading = active.querySelector('.section-intro h2, .welcome h2, h2, h1');
            if (visible(heading)) {
              const topRect = topbar.getBoundingClientRect();
              const headingRect = heading.getBoundingClientRect();
              const overlap = Math.round(topRect.bottom - headingRect.top);
              if (overlap > 1 && headingRect.bottom > topRect.top) {
                add('topbar-occlusion', 'The active view heading is covered by the top bar.', {
                  overlap_px: overlap,
                  topbar: roundedRect(topRect),
                  heading: roundedRect(headingRect),
                });
              }
            }
          }

          const dialogs = [...document.querySelectorAll('dialog[open], [role="dialog"]')].filter(visible);
          for (const dialog of dialogs) {
            const rect = dialog.getBoundingClientRect();
            if (rect.top < -1 || rect.left < -1 || rect.right > width + 1 || rect.bottom > height + 1) {
              add('dialog-outside-viewport', 'A visible dialog extends outside the viewport.', {
                id: dialog.id || null,
                rect: roundedRect(rect),
                viewport: {width, height},
              });
            }
          }

          const sidebarOpen = document.querySelector('#mobileSidebarBtn')?.getAttribute('aria-expanded') === 'true';
          const dock = document.querySelector('#globalAgentDock');
          if (!sidebarOpen && visible(dock) && visible(active)) {
            const previous = {
              window_y: window.scrollY,
              workspace_main: main?.scrollTop || 0,
            };
            if (main && main.scrollHeight > main.clientHeight + 2) {
              main.scrollTop = main.scrollHeight;
            } else {
              window.scrollTo(0, document.documentElement.scrollHeight);
            }
            await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));

            const dockRect = dock.getBoundingClientRect();
            const meaningful = [...active.querySelectorAll(
              'button, a[href], input, textarea, select, [tabindex], h1, h2, h3, h4, p, li'
            )].filter(node => {
              if (!visible(node) || dock.contains(node)) return false;
              if (node.matches('[tabindex="-1"]')) return false;
              const text = (node.innerText || node.getAttribute('aria-label') || '').trim();
              return node.matches('button, a[href], input, textarea, select, [tabindex]') || Boolean(text);
            });

            const viewportMeaningful = meaningful.filter(node => {
              const rect = node.getBoundingClientRect();
              const style = getComputedStyle(node);
              if (style.position === 'fixed' || style.position === 'sticky') return false;
              return rect.bottom > 0 && rect.top < height;
            });
            const blocked = viewportMeaningful.filter(node => {
              const rect = node.getBoundingClientRect();
              return rect.top < dockRect.bottom && rect.bottom > dockRect.top + 1;
            });

            if (blocked.length) {
              const worst = blocked
                .map(node => ({node, rect: node.getBoundingClientRect()}))
                .sort((a, b) => b.rect.bottom - a.rect.bottom)[0];
              const overlap = Math.round(Math.min(worst.rect.bottom, dockRect.bottom) - Math.max(worst.rect.top, dockRect.top));
              add('agent-dock-occlusion', 'Meaningful end-of-page content cannot be fully revealed above the fixed Agent composer at the reachable scroll limit.', {
                overlap_px: overlap,
                element: worst.node.id || worst.node.getAttribute('aria-label') || worst.node.tagName.toLowerCase(),
                element_rect: roundedRect(worst.rect),
                dock: roundedRect(dockRect),
              });
            }

            if (main) main.scrollTop = previous.workspace_main;
            window.scrollTo(0, previous.window_y);
            await new Promise(resolve => requestAnimationFrame(resolve));
          }

          return {width, height, scroll, issues};
        }
        """, {"expectScrollReset": expect_scroll_reset})
        return result

    def capture_surface(name: str, *, expect_scroll_reset: bool = False) -> None:
        layout = inspect_surface(expect_scroll_reset)
        screenshot_name = f"surface_sweep_{name}.png"
        page.screenshot(path=str(ART / screenshot_name), full_page=True)
        surface_report["surfaces"].append({
            "name": name,
            "viewport": {"width": layout["width"], "height": layout["height"]},
            "status": "FAIL" if layout["issues"] else "PASS",
            "issues": layout["issues"],
            "screenshot": screenshot_name,
        })

    def switch_project_view(view_name: str, selector: str, prefix: str) -> None:
        set_scroll_to_end()
        page.locator(f'[data-project-id="sweep"] [data-project-view="{view_name}"]').click()
        page.locator(selector).wait_for(state="visible")
        assert page.locator(".view.active").count() == 1
        capture_surface(f"{prefix}_{view_name}", expect_scroll_reset=True)

    try:
        page.goto(BASE_URL, wait_until="networkidle")
        page.locator("#workspaceShell").wait_for(state="visible")
        page.locator("#workspace").wait_for(state="visible")
        assert page.locator("#notificationCount").inner_text().startswith("0")

        core_surfaces = {
            "overview": "#view-overview",
            "tasks": "#view-tasks",
            "architecture": "#view-architecture",
        }
        for name, selector in core_surfaces.items():
            switch_project_view(name, selector, "desktop_1440")

        page.evaluate("() => { const main = document.querySelector('#workspaceMain'); if (main) main.scrollTop = 0; window.scrollTo(0, 0); }")
        page.locator("#notificationBtn").click()
        page.locator("#notificationMenu").wait_for(state="visible")
        assert "Nothing needs your approval" in page.locator("#notificationMenu").inner_text()
        capture_surface("desktop_1440_notifications_empty")
        page.locator("#notificationCloseBtn").click()

        for section in ["profile", "preferences", "settings"]:
            page.locator("#accountBtn").click()
            page.locator(f'[data-account-section="{section}"]').click()
            page.locator("#accountSettingsDialog").wait_for(state="visible")
            assert page.locator("#settingsPanel").is_visible()
            capture_surface(f"desktop_1440_account_{section}")
            page.locator('#accountSettingsDialog [data-close-dialog="accountSettingsDialog"]').first.click()

        page.locator("#accountBtn").click()
        page.locator("#accountMcpConnectionsBtn").wait_for(state="visible")
        page.locator("#accountMcpConnectionsBtn").click()
        page.locator("#mcpConnectionsDialog").wait_for(state="visible")
        page.locator("#mcpSearch").fill("google")
        assert page.locator('[data-mcp-preset="google-drive"]').is_visible()
        capture_surface("desktop_1440_mcp_search_google")
        page.locator('[data-mcp-preset="google-drive"]').click()
        capture_surface("desktop_1440_mcp_browse")
        page.locator('[data-mcp-tab="connected"]').click()
        page.locator("#mcpConnectedPane").wait_for(state="visible")
        assert "No MCPs connected" in page.locator("#mcpConnectedPane").inner_text()
        capture_surface("desktop_1440_mcp_connected")
        page.locator('#mcpConnectionsDialog [data-close-dialog="mcpConnectionsDialog"]').first.click()

        page.set_viewport_size({"width": 1280, "height": 800})
        for name, selector in core_surfaces.items():
            switch_project_view(name, selector, "desktop_1280")

        page.set_viewport_size({"width": 375, "height": 812})
        page.evaluate("() => { const main = document.querySelector('#workspaceMain'); if (main) main.scrollTop = 0; window.scrollTo(0, 0); }")
        page.evaluate("() => document.querySelector('#mobileSidebarBtn').click()")
        page.locator("#workspaceSidebar").wait_for(state="visible")
        assert page.locator("#mobileSidebarBtn").get_attribute("aria-expanded") == "true"
        page.wait_for_function("() => Math.abs(document.querySelector('#workspaceSidebar')?.getBoundingClientRect().left || 0) < 1")
        capture_surface("mobile_375_sidebar")

        for name, selector in core_surfaces.items():
            set_scroll_to_end()
            if page.locator("#mobileSidebarBtn").get_attribute("aria-expanded") != "true":
                page.locator("#mobileSidebarBtn").click()
                page.locator("#workspaceSidebar").wait_for(state="visible")
            page.locator(f'[data-project-id="sweep"] [data-project-view="{name}"]').click()
            page.locator(selector).wait_for(state="visible")
            capture_surface(f"mobile_375_{name}", expect_scroll_reset=True)

        # Important product states are sampled at the primary desktop review viewport instead of
        # multiplying every state across every breakpoint.
        page.set_viewport_size({"width": 1440, "height": 900})
        page.evaluate("() => { const main = document.querySelector('#workspaceMain'); if (main) main.scrollTop = 0; window.scrollTo(0, 0); }")
        page.locator('[data-project-id="sweep"] [data-project-view="tasks"]').click()
        page.locator("#view-tasks").wait_for(state="visible")
        blocked_task = page.locator('#view-tasks [data-task-open="task-blocked"]')
        blocked_task.click()
        assert blocked_task.get_attribute("aria-expanded") == "true"
        capture_surface("desktop_1440_tasks_blocked_selected")

        backend.contexts["sweep"]["proposals"] = [proposal("sweep", "proposal-review")]
        page.reload(wait_until="networkidle")
        page.locator("#workspace").wait_for(state="visible")
        assert page.locator("#needsCount").inner_text() == "1 item needs you ↗"
        page.locator("#notificationBtn").click()
        page.locator("#notificationMenu").wait_for(state="visible")
        assert page.locator('[data-attention-kind="proposal"]').is_visible()
        capture_surface("desktop_1440_notifications_attention")
        page.locator('[data-attention-kind="proposal"]').click()
        page.locator("#workspaceTabReviewPanel").wait_for(state="visible")
        capture_surface("desktop_1440_architecture_review_pending")

        backend.projects = []
        page.evaluate("() => localStorage.removeItem('archbro-project-id')")
        # Home is a navigation intent: clearing the startup fallback alone must
        # not override the explicit project still present in the current URL.
        page.goto(BASE_URL, wait_until="networkidle")
        page.locator("#workspaceHome").wait_for(state="visible")
        assert "project" not in parse_qs(urlsplit(page.url).query)
        assert page.evaluate("() => window.ArchBroWebBridge.getCommittedNavigation().project_id") is None
        page.locator("#workspaceHomeEmpty").wait_for(state="visible")
        capture_surface("desktop_1440_empty_workspace")

        page.locator("#workspaceHomeNewProjectBtn").click()
        page.locator("#newProjectNameDialog").wait_for(state="visible")
        capture_surface("desktop_1440_new_project_dialog")
        page.locator("#newProjectName").fill("   ")
        page.locator("#newProjectNameDialog button[type='submit']").click()
        assert "name" in page.locator("#newProjectNameError").inner_text().lower()
        capture_surface("desktop_1440_new_project_validation_error")
    except Exception as exc:
        fatal_error = exc
        surface_report["fatal"] = {"type": type(exc).__name__, "message": str(exc)}
    finally:
        surface_report["runtime"] = {
            "status": "FAIL" if console_errors or page_errors or http_errors else "PASS",
            "console_errors": console_errors,
            "page_errors": page_errors,
            "http_errors": http_errors,
        }
        has_surface_failures = any(item["status"] == "FAIL" for item in surface_report["surfaces"])
        surface_report["result"] = "FAIL" if fatal_error or has_surface_failures or surface_report["runtime"]["status"] == "FAIL" else "PASS"
        SURFACE_REPORT_PATH.write_text(json.dumps(surface_report, indent=2), encoding="utf-8")
        context.close()

    if fatal_error is not None:
        raise fatal_error
    if surface_report["result"] != "PASS":
        failed_surfaces = [item["name"] for item in surface_report["surfaces"] if item["status"] == "FAIL"]
        raise AssertionError(
            f"Autonomous frontend surface sweep found objective failures: {failed_surfaces}; "
            f"runtime={surface_report['runtime']['status']}"
        )


def case_workspace_review_state_and_outcomes(browser: Browser) -> None:
    def configured_backend(project_id: str) -> FakeBackend:
        backend = FakeBackend([project(project_id, "Review State")])
        backend.contexts[project_id]["tasks"] = [task(project_id, "task-a", "Open task context", "TODO")]
        backend.contexts[project_id]["proposals"] = [
            proposal(project_id, "proposal-a"),
            proposal(project_id, "proposal-b"),
        ]
        return backend

    # Mouse acceptance starts from a real task drawer, requires the server preview,
    # survives a dropped mutation response via read-back, and never sends twice.
    backend = configured_backend("review-accept")
    context, page, errors = open_page(
        browser, backend, identity="email:review-accept@example.com", project_id="review-accept"
    )
    with diagnostic_scope(context.close):
        page.locator('[data-project-id="review-accept"] [data-project-view="tasks"]').click()
        page.locator('#taskList [data-task-open="task-a"]').click()
        page.locator("#taskDetailPanel").wait_for(state="visible")
        assert "Open task context" in page.locator("#instructionContext").inner_text()
        page.locator("#workspaceTabReview").click()
        page.locator("#workspaceTabReviewPanel").wait_for(state="visible")
        assert page.locator("#taskDetailPanel").is_hidden()
        assert "Proposal" in page.locator("#instructionContext").inner_text()
        assert "Open task context" not in page.locator("#instructionContext").inner_text()
        page.locator('[data-preview-version="1"]').wait_for(state="visible")
        accept = page.locator('[data-proposal-decision="accept"]')
        assert accept.is_enabled()
        backend.drop_next_decision_response = True
        accept.click()
        page.wait_for_function("() => document.querySelectorAll('.status-pill.SUPERSEDED').length === 1")
        assert backend.decision_requests == [{"proposal_id": "proposal-a", "decision": "accept"}]
        assert backend.contexts["review-accept"]["architecture"]["version"] == 2
        assert [item["status"] for item in backend.contexts["review-accept"]["proposals"]] == ["ACCEPTED", "SUPERSEDED"]
        accept.click(force=True) if accept.count() and accept.is_visible() else None
        assert len(backend.decision_requests) == 1
        assert not errors, errors

    # Keep Current follows the same state transition and sends exactly one request.
    backend = configured_backend("review-reject")
    backend.contexts["review-reject"]["proposals"] = [proposal("review-reject", "proposal-a")]
    context, page, errors = open_page(
        browser, backend, identity="email:review-reject@example.com", project_id="review-reject"
    )
    with diagnostic_scope(context.close):
        page.locator('[data-project-id="review-reject"] [data-project-view="tasks"]').click()
        page.locator('#taskList [data-task-open="task-a"]').click()
        page.locator("#workspaceTabReview").click()
        page.locator('[data-proposal-decision="reject"]').click()
        page.wait_for_function("() => document.querySelector('.status-pill.REJECTED')")
        assert backend.decision_requests == [{"proposal_id": "proposal-a", "decision": "reject"}]
        assert backend.contexts["review-reject"]["architecture"]["version"] == 1
        page.locator("#workspaceTabTasks").click()
        page.locator('#taskList [data-task-open="task-a"]').click()
        detail_action = page.locator("#taskDetailAction")
        assert detail_action.inner_text() == "Start task"
        detail_action.click()
        page.wait_for_function("() => document.querySelector('#taskDetailAction')?.textContent === 'Mark done'")
        assert page.locator("#taskDetailPanel").is_visible()
        detail_action.click()
        page.wait_for_function("() => document.querySelector('#taskDetailAction')?.textContent === 'Reopen'")
        detail_action.click()
        page.wait_for_function("() => document.querySelector('#taskDetailAction')?.textContent === 'Start task'")
        assert backend.contexts["review-reject"]["tasks"][0]["status"] == "TODO"
        assert not errors, errors

    # Keyboard semantics and committed URL state survive Back, Forward, and reload.
    backend = configured_backend("review-route")
    context, page, errors = open_page(
        browser, backend, identity="email:review-route@example.com", project_id="review-route"
    )
    with diagnostic_scope(context.close):
        page.locator('[data-project-id="review-route"] [data-project-view="tasks"]').click()
        page.locator("#workspaceTabTasks").focus()
        page.keyboard.press("ArrowRight")
        assert page.locator("#workspaceTabReview").get_attribute("aria-selected") == "true"
        assert parse_qs(urlsplit(page.url).query)["workspace"] == ["review"]
        page.go_back(wait_until="networkidle")
        page.wait_for_function("() => document.querySelector('#workspaceTabTasks')?.getAttribute('aria-selected') === 'true'")
        assert parse_qs(urlsplit(page.url).query)["workspace"] == ["tasks"]
        page.go_forward(wait_until="networkidle")
        page.wait_for_function("() => document.querySelector('#workspaceTabReview')?.getAttribute('aria-selected') === 'true'")
        page.reload(wait_until="networkidle")
        assert page.locator("#workspaceTabReviewPanel").is_visible()
        page.locator("#workspaceTabReview").focus()
        page.keyboard.press("Home")
        assert page.locator("#workspaceTabTasks").get_attribute("aria-selected") == "true"
        page.locator("#workspaceTabReview").focus()
        page.keyboard.press("Space")
        assert page.locator("#workspaceTabReview").get_attribute("aria-selected") == "true"
        assert not errors, errors

    # An unresolvable dropped response remains UNKNOWN and controls stay locked.
    backend = configured_backend("review-unknown")
    backend.contexts["review-unknown"]["proposals"] = [proposal("review-unknown", "proposal-a")]
    context, page, errors = open_page(
        browser, backend, identity="email:review-unknown@example.com", project_id="review-unknown"
    )
    with diagnostic_scope(context.close):
        page.locator('[data-project-id="review-unknown"] [data-project-view="tasks"]').click()
        page.locator("#workspaceTabReview").click()
        page.locator('[data-preview-version="1"]').wait_for(state="visible")
        backend.drop_next_decision_response = True
        backend.fail_decision_readback = True
        page.locator('[data-proposal-decision="accept"]').click()
        page.locator("#proposalDecisionNotice.unknown").wait_for(state="visible")
        assert "could not be verified" in page.locator("#proposalDecisionNotice").inner_text()
        assert page.locator('[data-proposal-decision="accept"]').is_disabled()
        assert page.locator('[data-proposal-decision="reject"]').is_disabled()
        page.locator('[data-proposal-decision="accept"]').click(force=True)
        assert len(backend.decision_requests) == 1
        assert not errors, errors

    # Escape closes only the visible top layer; a drawer underneath is not consumed.
    backend = configured_backend("review-escape")
    context, page, errors = open_page(
        browser, backend, viewport={"width": 375, "height": 812},
        identity="email:review-escape@example.com", project_id="review-escape"
    )
    with diagnostic_scope(context.close):
        page.locator("#mobileSidebarBtn").click()
        page.locator('[data-project-id="review-escape"] [data-project-view="tasks"]').click()
        page.locator('#taskList [data-task-open="task-a"]').click()
        page.evaluate("() => document.querySelector('#notificationBtn').click()")
        page.keyboard.press("Escape")
        assert page.locator("#notificationMenu").is_hidden()
        assert page.locator("#taskDetailPanel").is_visible()
        page.evaluate("() => document.querySelector('#accountBtn').click()")
        page.keyboard.press("Escape")
        assert page.locator("#accountMenu").is_hidden()
        assert page.locator("#taskDetailPanel").is_visible()
        page.evaluate("() => document.querySelector('#mobileSidebarBtn').click()")
        page.keyboard.press("Escape")
        assert page.locator("#taskDetailPanel").is_visible()
        page.keyboard.press("Escape")
        assert page.locator("#taskDetailPanel").is_hidden()
        assert not errors, errors


CASES = [
    ("workspace_review_state_and_outcomes", case_workspace_review_state_and_outcomes),
    ("canvas_committed_navigation", case_canvas_committed_navigation),
    ("autonomous_surface_sweep", case_autonomous_surface_sweep),
    ("landing_authentication_teaser", case_landing_authentication_teaser),
    ("progressive_project_creation", case_progressive_project_creation),
    ("empty_workspace_cancel", case_empty_workspace_cancel),
    ("storage_recovery", case_storage_recovery),
    ("logout_reset", case_logout_reset),
    ("transactional_project_selection", case_transactional_project_selection),
    ("notifications_and_context", case_notifications_and_context),
    ("overview_navigation_and_attention", case_overview_navigation_and_attention),
    ("keyboard_and_mobile_layers", case_keyboard_and_mobile_layers),
    ("task_architecture_navigation", case_task_architecture_navigation),
    ("architecture_inspector_disclosure", case_architecture_inspector_disclosure),
    ("architecture_canvas_interactions", case_architecture_canvas_interactions),
    ("instruction_failure", case_instruction_failure),
    ("inline_rename_and_account", case_inline_rename_and_account),
    ("project_row_action_menu", case_project_row_action_menu),
]


report = {"result": "RUNNING", "base_url": BASE_URL, "cases": [], "failures": []}


def run_cases(requested: set[str] | None = None) -> dict:
    global report
    requested = requested or set()
    report = {"result": "RUNNING", "base_url": BASE_URL, "cases": [], "failures": []}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            for name, case in CASES:
                if requested and name not in requested:
                    continue
                print("CASE", name, flush=True)
                try:
                    case(browser)
                    report["cases"].append({"name": name, "result": "PASS"})
                    print("CASE_PASS", name, flush=True)
                except Exception as exc:
                    details = getattr(exc, "archbro_failure_details", None) or failure_details(exc)
                    failure = f"{details['type']}: {details['message']}"
                    entry = {"name": name, "result": "FAIL", "failure": failure, **details}
                    report["cases"].append(entry)
                    report["failures"].append(entry.copy())
                    print("CASE_FAIL", name, failure, flush=True)
                    print(
                        "CASE_DIAGNOSTIC",
                        json.dumps({key: details[key] for key in ["file", "line", "assertion", "values"]}),
                        flush=True,
                    )
                    print(details["traceback"], flush=True)
        finally:
            browser.close()
    report["result"] = "PASS" if not report["failures"] else "FAIL"
    REPORT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def run_case_with_static_server(case) -> None:
    global BASE_URL
    original_base_url = BASE_URL
    web_root = Path(__file__).resolve().parents[1] / "frontend" / "web"
    class LaneStaticHandler(SimpleHTTPRequestHandler):
        def do_GET(self) -> None:
            # Supply the local runtime configuration when no application server
            # is running. Project data is still owned by each case's FakeBackend.
            path = urlsplit(self.path).path
            if path == "/runtime-config.js":
                content_type = "application/javascript"
                body = 'window.__ARCHBRO_RUNTIME_CONFIG__ = {"auth_mode":"local","firebase":null};'
            else:
                super().do_GET()
                return
            encoded = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def translate_path(self, path: str) -> str:
            if path.startswith("/static/"):
                path = path[len("/static/"):]
            return super().translate_path(path)

    handler = partial(LaneStaticHandler, directory=str(web_root))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    BASE_URL = f"http://127.0.0.1:{server.server_port}/"
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                case(browser)
            finally:
                browser.close()
    finally:
        BASE_URL = original_base_url
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_s2_04_architecture_canvas_interactions_browser() -> None:
    run_case_with_static_server(case_architecture_canvas_interactions)


def test_s2_05_architecture_inspector_disclosure_browser() -> None:
    run_case_with_static_server(case_architecture_inspector_disclosure)

def test_canvas_committed_navigation_browser() -> None:
    run_case_with_static_server(case_canvas_committed_navigation)


def test_scoped_keyboard_focus_browser() -> None:
    run_case_with_static_server(case_keyboard_and_mobile_layers)


def test_task_architecture_navigation_browser() -> None:
    run_case_with_static_server(case_task_architecture_navigation)


def test_workspace_review_state_and_outcomes_browser() -> None:
    run_case_with_static_server(case_workspace_review_state_and_outcomes)


def test_autonomous_surface_sweep_browser() -> None:
    run_case_with_static_server(case_autonomous_surface_sweep)


def main() -> None:
    requested = {name for name in os.getenv("ARCHBRO_FINAL_FIX_CASES", "").split(",") if name}
    final_report = run_cases(requested)
    if final_report["failures"]:
        raise AssertionError(f"{len(final_report['failures'])} final-fix browser case(s) failed")
    print("FINAL_FIX_PLAYWRIGHT_PASS", json.dumps({"cases": len(final_report["cases"])}), flush=True)


if __name__ == "__main__":
    main()
