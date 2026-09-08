from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from archbro.backend.agent.code_architecture import CodeArchitectureSnapshotRequest
from archbro.backend.agent.node_context import build_node_context
from archbro.backend.core.contracts import (
    Architecture,
    Component,
    ProjectEvent,
    ProjectEventType,
    ProposalStatus,
    Task,
)
from archbro.backend.core.repository import ProjectRepositoryPort


ContextExpansionPolicy = Literal["ASK_ALL", "ALLOW_NEIGHBORHOOD", "AUTO_BOUNDED"]
ContextDirection = Literal["upstream", "downstream", "both"]

MANIFEST_SCHEMA = "archbro.agent_context_manifest.v1"
MAX_CONTEXT_CHARS = 24_000
MAX_ESTIMATED_INPUT_TOKENS = 6_000
AGENT_CONTEXT_EVENT_SCAN_LIMIT = 256
_POLICY_LIMITS: dict[str, tuple[int, int]] = {
    "ASK_ALL": (1, 8),
    "ALLOW_NEIGHBORHOOD": (2, 14),
    "AUTO_BOUNDED": (3, 20),
}


class AgentContextManifestRequest(BaseModel):
    node_id: str = Field(min_length=6, max_length=240)
    direction: ContextDirection = "both"
    expansion_policy: ContextExpansionPolicy = "ASK_ALL"
    expected_architecture_version: int = Field(ge=1)

    @field_validator("node_id")
    @classmethod
    def normalize_node_id(cls, value: str) -> str:
        value = value.strip()
        if not value.startswith("node:") or not value[5:]:
            raise ValueError("agent context node_id must use node:<component_id>")
        return value


class AgentContextExecutionRequest(AgentContextManifestRequest):
    preview_manifest_hash: str = Field(min_length=64, max_length=64)

    @field_validator("preview_manifest_hash")
    @classmethod
    def validate_preview_hash(cls, value: str) -> str:
        value = value.strip().lower()
        if any(character not in "0123456789abcdef" for character in value):
            raise ValueError("preview_manifest_hash must be a 64-character hexadecimal digest")
        return value


class AgentContextPreviewStaleError(ValueError):
    def __init__(self, expected_hash: str, current_hash: str) -> None:
        super().__init__("agent context preview changed before execution")
        self.expected_hash = expected_hash
        self.current_hash = current_hash


def _one_line(value: object, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _component_payload(component: Component) -> dict[str, object]:
    kind = component.kind.value if hasattr(component.kind, "value") else str(component.kind)
    return {
        "node_id": f"node:{component.id}",
        "component_id": component.id,
        "name": _one_line(component.name, 240),
        "type": _one_line(component.type, 120),
        "kind": kind,
        "responsibility": _one_line(component.responsibility, 600),
        "status": _one_line(component.status, 120),
    }


def _lineage(architecture: Architecture, component_id: str) -> list[dict[str, object]]:
    ids: list[str] = []
    cursor: str | None = component_id
    while cursor is not None:
        ids.append(cursor)
        cursor = architecture.parent_component_id_for(cursor)
    result: list[dict[str, object]] = []
    for item in reversed(ids):
        component = architecture.find_component(item)
        if component is not None:
            result.append(_component_payload(component))
    return result


def _compact_node_context(payload: dict[str, object]) -> dict[str, object]:
    result = copy.deepcopy(payload)
    for node in [result.get("origin"), *(result.get("nodes") or [])]:
        if isinstance(node, dict):
            node["responsibility"] = _one_line(node.get("responsibility"), 600)
    for relationship in result.get("relationships") or []:
        if not isinstance(relationship, dict):
            continue
        relationship["description"] = _one_line(relationship.get("description"), 500)
        provenance = relationship.get("provenance")
        if isinstance(provenance, dict):
            provenance["supporting_text"] = _one_line(provenance.get("supporting_text"), 500)
    return result


def _task_payload(task: Task) -> dict[str, object]:
    return {
        "id": task.id,
        "title": _one_line(task.title, 240),
        "description": _one_line(task.description, 500),
        "status": task.status.value,
        "owner": task.owner.value,
        "source": task.source.value,
        "related_component": task.related_component,
        "dependencies": list(task.dependencies[:12]),
        "acceptance_criteria": [_one_line(item, 320) for item in task.acceptance_criteria[:6]],
    }


def _related_component_ids(event: ProjectEvent, task_by_id: dict[str, Task]) -> set[str]:
    payload = event.payload
    related: set[str] = set()
    values = payload.get("related_components")
    if isinstance(values, list):
        related.update(str(value).strip() for value in values if str(value).strip())
    for key in ("related_component", "architecture_component_id"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            related.add(value.strip())
    ui_context = payload.get("ui_context")
    if isinstance(ui_context, dict):
        value = ui_context.get("architecture_node_id") or ui_context.get("related_component")
        if isinstance(value, str) and value.strip():
            related.add(value.removeprefix("node:").strip())
    related_task_id = payload.get("related_task_id")
    if isinstance(related_task_id, str) and related_task_id in task_by_id:
        component_id = task_by_id[related_task_id].related_component
        if component_id:
            related.add(component_id)
    return related


def _event_payload(event: ProjectEvent) -> dict[str, object]:
    payload = event.payload
    raw_evidence = payload.get("evidence")
    evidence = (
        [_one_line(item, 500) for item in raw_evidence[:5]]
        if isinstance(raw_evidence, list)
        else []
    )
    summary = payload.get("summary") or payload.get("message") or payload.get("note") or event.type.value
    return {
        "event_id": event.id,
        "type": event.type.value,
        "source": event.source.value,
        "summary": _one_line(summary, 700),
        "evidence": evidence,
        "timestamp": event.timestamp.isoformat(),
    }


def _code_truth_section(
    event: ProjectEvent | None,
    allowed_component_ids: set[str],
) -> dict[str, object]:
    if event is None or not isinstance(event.payload.get("request"), dict):
        return {"status": "NO_SNAPSHOT", "chunks": []}
    try:
        request = CodeArchitectureSnapshotRequest.model_validate(event.payload["request"])
    except ValueError:
        return {"status": "INVALID_SNAPSHOT", "chunks": []}
    if request.code_truth is None:
        return {
            "status": "SNAPSHOT_WITHOUT_CODE_TRUTH",
            "repository": request.repository,
            "revision": request.revision,
            "chunks": [],
        }

    evidence_by_id = {item.id: item for item in request.source_evidence}
    chunks: list[dict[str, object]] = []
    matching_count = 0
    for symbol in sorted(request.code_truth.symbols, key=lambda item: item.id):
        if symbol.architecture_component_id not in allowed_component_ids:
            continue
        matching_count += 1
        if len(chunks) >= 12:
            continue
        evidence = evidence_by_id.get(symbol.source_evidence_id or "")
        chunks.append(
            {
                "symbol": {
                    "id": symbol.id,
                    "qualified_name": _one_line(symbol.qualified_name, 500),
                    "kind": symbol.kind,
                    "path": symbol.path,
                    "line_start": symbol.line_start,
                    "line_end": symbol.line_end,
                    "architecture_component_id": symbol.architecture_component_id,
                },
                "source": (
                    {
                        "evidence_id": evidence.id,
                        "path": evidence.path,
                        "line_start": evidence.line_start,
                        "line_end": evidence.line_end,
                        "excerpt": evidence.excerpt[:1_600],
                    }
                    if evidence is not None
                    else None
                ),
            }
        )
    return {
        "status": "MATCHED" if matching_count else "NO_MATCHING_SYMBOLS",
        "repository": request.repository,
        "revision": request.revision,
        "chunks": chunks,
        "truncated": matching_count > len(chunks),
        "matching_symbol_count": matching_count,
    }


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _record_limit_reason(usage: dict[str, Any], reason: str) -> None:
    reasons = usage["limit_reasons"]
    if reason not in reasons:
        reasons.append(reason)


def _refresh_usage_metrics(manifest: dict[str, Any]) -> int:
    usage = manifest["usage"]
    sections = manifest["sections"]
    dependency_context = sections["architecture"]["dependency_context"]
    usage["task_count"] = len(sections["tasks"])
    usage["evidence_count"] = len(sections["evidence"])
    usage["code_truth_chunk_count"] = len(sections["code_truth"]["chunks"])
    usage["expansion_count"] = max(
        0,
        int(dependency_context["counts"].get("max_hop", 0)) - 1,
    )
    usage["truncated"] = bool(usage["limit_reasons"])

    for _ in range(8):
        context_chars = len(_canonical_json(manifest))
        estimated_tokens = (context_chars + 3) // 4
        if (
            usage["context_chars"] == context_chars
            and usage["estimated_input_tokens"] == estimated_tokens
        ):
            return context_chars
        usage["context_chars"] = context_chars
        usage["estimated_input_tokens"] = estimated_tokens
    return len(_canonical_json(manifest))


def _trim_to_budget(manifest: dict[str, Any]) -> None:
    sections = manifest["sections"]
    usage = manifest["usage"]
    placeholder_hash = "0" * 64
    manifest["manifest_hash"] = placeholder_hash

    def current_size() -> int:
        return _refresh_usage_metrics(manifest)

    while current_size() > MAX_CONTEXT_CHARS and sections["code_truth"]["chunks"]:
        sections["code_truth"]["chunks"].pop()
        _record_limit_reason(usage, "CODE_TRUTH_BUDGET")
    while current_size() > MAX_CONTEXT_CHARS and sections["evidence"]:
        sections["evidence"].pop()
        _record_limit_reason(usage, "EVIDENCE_BUDGET")
    while current_size() > MAX_CONTEXT_CHARS and sections["tasks"]:
        sections["tasks"].pop()
        _record_limit_reason(usage, "TASK_BUDGET")
    while current_size() > MAX_CONTEXT_CHARS and sections["pending_proposals"]:
        sections["pending_proposals"].pop()
        _record_limit_reason(usage, "PROPOSAL_BUDGET")

    dependency_context = sections["architecture"]["dependency_context"]
    while current_size() > MAX_CONTEXT_CHARS and dependency_context["nodes"]:
        removed = dependency_context["nodes"].pop()
        removed_node_id = removed["node_id"]
        dependency_context["relationships"] = [
            relationship
            for relationship in dependency_context["relationships"]
            if relationship["source"] != removed_node_id and relationship["target"] != removed_node_id
        ]
        dependency_context["counts"]["nodes"] = len(dependency_context["nodes"])
        dependency_context["counts"]["relationships"] = len(dependency_context["relationships"])
        dependency_context["counts"]["max_hop"] = max(
            (int(node["hop"]) for node in dependency_context["nodes"]),
            default=0,
        )
        dependency_context["truncated"] = True
        dependency_context["limit_reason"] = "CONTEXT_BUDGET"
        _record_limit_reason(usage, "ARCHITECTURE_BUDGET")

    if current_size() > MAX_CONTEXT_CHARS:
        _record_limit_reason(usage, "PROJECT_TEXT_BUDGET")
        project = sections["project"]
        for key in ("goal", "architecture_summary"):
            while current_size() > MAX_CONTEXT_CHARS and project.get(key):
                text = str(project[key])
                overflow = current_size() - MAX_CONTEXT_CHARS
                keep = max(0, len(text) - max(1, overflow))
                if keep >= len(text):
                    keep = len(text) - 1
                project[key] = text[:keep].rstrip()
    if current_size() > MAX_CONTEXT_CHARS:
        raise ValueError("bounded agent context cannot fit the server context budget")


def _finalize_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    _trim_to_budget(manifest)
    final_size = _refresh_usage_metrics(manifest)
    if final_size > MAX_CONTEXT_CHARS:
        raise ValueError("finalized agent context exceeds the server context budget")

    digest_payload = copy.deepcopy(manifest)
    digest_payload.pop("manifest_hash", None)
    manifest["manifest_hash"] = hashlib.sha256(
        _canonical_json(digest_payload).encode("utf-8")
    ).hexdigest()
    if len(_canonical_json(manifest)) != final_size:
        raise ValueError("finalized agent context size changed after hashing")
    return manifest


def build_agent_context_manifest(
    repository: ProjectRepositoryPort,
    project_id: str,
    request: AgentContextManifestRequest,
) -> dict[str, Any]:
    snapshot = repository.load_agent_context_snapshot(
        project_id,
        event_scan_limit=AGENT_CONTEXT_EVENT_SCAN_LIMIT,
    )
    project = snapshot.project
    architecture = snapshot.architecture
    max_hops, max_results = _POLICY_LIMITS[request.expansion_policy]
    node_context = _compact_node_context(
        build_node_context(
            architecture,
            project_id,
            request.node_id,
            direction=request.direction,
            max_hops=max_hops,
            max_results=max_results,
            expected_architecture_version=request.expected_architecture_version,
        )
    )
    component_id = request.node_id[5:]
    origin = architecture.find_component(component_id)
    if origin is None:
        raise ValueError(f"architecture component not found: {component_id}")
    children = [_component_payload(child) for child in sorted(origin.children, key=lambda item: item.id)]
    dependency_component_ids = {
        node["component_id"]
        for node in node_context["nodes"]
        if isinstance(node, dict) and isinstance(node.get("component_id"), str)
    }
    allowed_component_ids = {component_id, *(child.id for child in origin.children), *dependency_component_ids}

    all_tasks = snapshot.tasks
    task_by_id = {task.id: task for task in all_tasks}
    task_order = {"BLOCKED": 0, "IN_PROGRESS": 1, "TODO": 2, "DONE": 3}
    tasks = [task for task in all_tasks if task.related_component in allowed_component_ids]
    tasks.sort(key=lambda item: (task_order.get(item.status.value, 9), item.id))

    proposals = []
    for proposal in snapshot.proposals:
        if proposal.status != ProposalStatus.PENDING:
            continue
        changed_ids = {
            str(change.get("component_id", "")).strip()
            for change in proposal.proposed_changes
            if isinstance(change, dict)
        }
        if not (set(proposal.affected_components) | changed_ids).intersection(allowed_component_ids):
            continue
        bounded_affected_components = sorted(
            (set(proposal.affected_components) | changed_ids).intersection(
                allowed_component_ids
            )
        )
        proposals.append(
            {
                "id": proposal.id,
                "reason": _one_line(proposal.reason, 600),
                "observed_change": _one_line(proposal.observed_change, 600),
                "affected_components": bounded_affected_components,
                "impact": _one_line(proposal.impact, 600),
                "status": proposal.status.value,
            }
        )
    proposals.sort(key=lambda item: item["id"])

    evidence: list[dict[str, object]] = []
    evidence_matching_count = 0
    events = sorted(
        snapshot.events,
        key=lambda item: (item.timestamp, item.id),
        reverse=True,
    )
    for event in events:
        if event.type == ProjectEventType.CODE_ARCHITECTURE_SNAPSHOT:
            continue
        if not _related_component_ids(event, task_by_id).intersection(allowed_component_ids):
            continue
        evidence_matching_count += 1
        if len(evidence) < 12:
            evidence.append(_event_payload(event))

    code_truth = _code_truth_section(
        snapshot.latest_code_architecture_event,
        allowed_component_ids,
    )

    max_hop = int(node_context["counts"]["max_hop"])
    limit_reasons: list[str] = []
    if node_context.get("truncated") and node_context.get("limit_reason"):
        limit_reasons.append(str(node_context["limit_reason"]))
    if len(tasks) > 20:
        limit_reasons.append("TASK_COUNT_LIMIT")
    if len(proposals) > 8:
        limit_reasons.append("PROPOSAL_COUNT_LIMIT")
    if evidence_matching_count > 12:
        limit_reasons.append("EVIDENCE_COUNT_LIMIT")
    if snapshot.event_history_truncated:
        limit_reasons.append("EVENT_HISTORY_SCAN_LIMIT")
    if code_truth.get("truncated"):
        limit_reasons.append("CODE_TRUTH_COUNT_LIMIT")
    manifest: dict[str, Any] = {
        "schema": MANIFEST_SCHEMA,
        "project_id": project_id,
        "architecture_version": architecture.version,
        "selection": {
            "node_id": request.node_id,
            "direction": request.direction,
            "expansion_policy": request.expansion_policy,
            "effective_max_hops": max_hops,
            "effective_max_results": max_results,
        },
        "sections": {
            "project": {
                "id": project.id,
                "name": _one_line(project.name, 240),
                "status": project.status.value,
                "goal": _one_line(project.goal, 800),
                "architecture_summary": _one_line(architecture.summary, 500),
            },
            "architecture": {
                "origin": _component_payload(origin),
                "lineage": _lineage(architecture, component_id),
                "children": children,
                "dependency_context": node_context,
            },
            "tasks": [_task_payload(task) for task in tasks[:20]],
            "pending_proposals": proposals[:8],
            "evidence": evidence,
            "code_truth": code_truth,
            "mcp_refs": [],
        },
        "budget": {
            "max_chars": MAX_CONTEXT_CHARS,
            "max_estimated_input_tokens": MAX_ESTIMATED_INPUT_TOKENS,
            "max_evidence_records": 12,
            "max_event_scan_records": AGENT_CONTEXT_EVENT_SCAN_LIMIT,
            "max_code_truth_chunks": 12,
        },
        "usage": {
            "selected_node_count": 1,
            "context_chars": 0,
            "estimated_input_tokens": 0,
            "task_count": 0,
            "evidence_count": 0,
            "mcp_result_count": 0,
            "code_truth_chunk_count": 0,
            "expansion_count": max(0, max_hop - 1),
            "truncated": False,
            "limit_reasons": limit_reasons,
        },
        "manifest_hash": "0" * 64,
    }
    return _finalize_manifest(manifest)


def prepare_agent_context_event_payload(
    repository: ProjectRepositoryPort,
    project_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    if "agent_context_manifest" in payload:
        raise ValueError("agent_context_manifest is server-owned")
    raw_request = payload.get("agent_context_request")
    if raw_request is None:
        return dict(payload)
    if not isinstance(raw_request, dict):
        raise ValueError("agent_context_request must be an object")
    execution = AgentContextExecutionRequest.model_validate(raw_request)
    preview_hash = execution.preview_manifest_hash
    manifest_request = AgentContextManifestRequest.model_validate(
        execution.model_dump(exclude={"preview_manifest_hash"})
    )
    manifest = build_agent_context_manifest(repository, project_id, manifest_request)
    if manifest["manifest_hash"] != preview_hash:
        raise AgentContextPreviewStaleError(preview_hash, manifest["manifest_hash"])
    result = dict(payload)
    result["agent_context_request"] = execution.model_dump(mode="json")
    result["agent_context_manifest"] = manifest
    return result
