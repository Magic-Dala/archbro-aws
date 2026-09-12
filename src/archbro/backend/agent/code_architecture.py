from __future__ import annotations

import hashlib
import re
from collections import defaultdict, deque
from pathlib import PurePosixPath
from typing import Literal
from urllib.parse import quote

from pydantic import BaseModel, Field, field_validator, model_validator

from archbro.backend.core.contracts import Architecture, ArchitectureNodeKind
from archbro.backend.core.diagram_layout import layout_diagram
from archbro.backend.core.github_repository import normalize_github_repository as normalize_repository


CODE_ARCHITECTURE_SCHEMA = "archbro.code_architecture.v1"
CODE_DIAGRAM_VERSION = "archbro.code_diagram.v1"
_FULL_GIT_SHA = re.compile(r"^[0-9a-fA-F]{40}$")



class CodeSourceEvidence(BaseModel):
    id: str = Field(min_length=1, max_length=120)
    path: str = Field(min_length=1, max_length=500)
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    excerpt: str = Field(min_length=1, max_length=4000)
    symbol: str | None = Field(default=None, max_length=240)
    blob_sha: str | None = Field(default=None, max_length=64)

    @field_validator("path")
    @classmethod
    def validate_repo_relative_path(cls, value: str) -> str:
        value = value.strip().replace("\\", "/")
        path = PurePosixPath(value)
        if (
            not value
            or value.startswith("/")
            or path.is_absolute()
            or re.match(r"^[A-Za-z]:/", value)
            or ":" in path.parts[0]
            or ".." in path.parts
            or ".git" in path.parts
        ):
            raise ValueError("source path must be a safe repository-relative POSIX path")
        return value

    @field_validator("blob_sha")
    @classmethod
    def normalize_blob_sha(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().lower()
        if value and not re.fullmatch(r"[0-9a-f]{40,64}", value):
            raise ValueError("blob_sha must be a 40-64 character hexadecimal digest when supplied")
        return value or None

    @model_validator(mode="after")
    def validate_line_evidence(self) -> "CodeSourceEvidence":
        if self.line_end < self.line_start:
            raise ValueError("source evidence line_end must be >= line_start")
        expected_lines = self.line_end - self.line_start + 1
        actual_lines = len(self.excerpt.splitlines())
        if actual_lines != expected_lines:
            raise ValueError(
                "source evidence excerpt line count must exactly match line_start..line_end"
            )
        return self


class CodeArchitectureComponent(BaseModel):
    id: str = Field(min_length=1, max_length=160)
    name: str = Field(min_length=1, max_length=240)
    type: str = Field(min_length=1, max_length=120)
    responsibility: str = Field(min_length=1, max_length=1000)
    kind: ArchitectureNodeKind = ArchitectureNodeKind.SYSTEM
    source_evidence_ids: list[str] = Field(min_length=1, max_length=12)
    children: list["CodeArchitectureComponent"] = Field(default_factory=list, max_length=7)


class CodeArchitectureRelationship(BaseModel):
    source: str = Field(min_length=1, max_length=160)
    target: str = Field(min_length=1, max_length=160)
    relationship_type: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1000)
    source_evidence_ids: list[str] = Field(min_length=1, max_length=12)


class CodeTruthSymbol(BaseModel):
    id: str = Field(min_length=1, max_length=240)
    qualified_name: str = Field(min_length=1, max_length=500)
    kind: str = Field(min_length=1, max_length=80)
    path: str = Field(min_length=1, max_length=500)
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    source_evidence_id: str | None = Field(default=None, max_length=120)
    architecture_component_id: str | None = Field(default=None, max_length=160)

    @field_validator("path")
    @classmethod
    def validate_repo_relative_path(cls, value: str) -> str:
        return CodeSourceEvidence.validate_repo_relative_path(value)

    @model_validator(mode="after")
    def validate_range(self) -> "CodeTruthSymbol":
        if self.line_end < self.line_start:
            raise ValueError("code truth symbol line_end must be >= line_start")
        return self


class CodeTruthRelationship(BaseModel):
    source: str = Field(min_length=1, max_length=240)
    target: str = Field(min_length=1, max_length=240)
    relationship_type: Literal["CALL", "IMPORT", "ROUTE", "DEPENDENCY"]
    source_evidence_ids: list[str] = Field(default_factory=list, max_length=12)


class CodeTruthIndex(BaseModel):
    extractor: str = Field(min_length=1, max_length=120)
    extractor_version: str = Field(min_length=1, max_length=80)
    revision_verified: bool
    base_revision: str | None = Field(default=None, min_length=40, max_length=40)
    changed_paths: list[str] = Field(default_factory=list, max_length=500)
    symbols: list[CodeTruthSymbol] = Field(default_factory=list, max_length=5000)
    relationships: list[CodeTruthRelationship] = Field(default_factory=list, max_length=20000)

    @field_validator("base_revision")
    @classmethod
    def validate_base_revision(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().lower()
        if not _FULL_GIT_SHA.fullmatch(value):
            raise ValueError("code truth base_revision must be an exact full 40-character Git commit SHA")
        return value

    @field_validator("changed_paths")
    @classmethod
    def validate_changed_paths(cls, values: list[str]) -> list[str]:
        normalized = [CodeSourceEvidence.validate_repo_relative_path(value) for value in values]
        return list(dict.fromkeys(normalized))

    @model_validator(mode="after")
    def validate_index(self) -> "CodeTruthIndex":
        if not self.revision_verified:
            raise ValueError("Code Truth requires a revision-verified deterministic extractor")
        symbol_ids = [symbol.id for symbol in self.symbols]
        if len(symbol_ids) != len(set(symbol_ids)):
            raise ValueError("code truth symbol ids must be unique")
        known = set(symbol_ids)
        for relationship in self.relationships:
            if relationship.source not in known or relationship.target not in known:
                raise ValueError("code truth relationships must reference existing symbol ids")
        return self


class CodeArchitectureSnapshotRequest(BaseModel):
    repository: str = Field(min_length=3, max_length=300)
    revision: str = Field(min_length=40, max_length=40)
    summary: str = Field(min_length=1, max_length=2000)
    components: list[CodeArchitectureComponent] = Field(min_length=1, max_length=8)
    relationships: list[CodeArchitectureRelationship] = Field(default_factory=list, max_length=120)
    source_evidence: list[CodeSourceEvidence] = Field(min_length=1, max_length=160)
    code_truth: CodeTruthIndex | None = None

    @field_validator("repository")
    @classmethod
    def normalize_github_repository(cls, value: str) -> str:
        return normalize_repository(value)

    @field_validator("revision")
    @classmethod
    def require_full_revision(cls, value: str) -> str:
        value = value.strip().lower()
        if not _FULL_GIT_SHA.fullmatch(value):
            raise ValueError("revision must be an exact full 40-character Git commit SHA")
        return value

    @model_validator(mode="after")
    def validate_snapshot_graph(self) -> "CodeArchitectureSnapshotRequest":
        evidence_ids: set[str] = set()
        for evidence in self.source_evidence:
            if evidence.id in evidence_ids:
                raise ValueError(f"duplicate source evidence id: {evidence.id}")
            evidence_ids.add(evidence.id)

        component_ids: set[str] = set()
        total = 0

        def walk(nodes: list[CodeArchitectureComponent], depth: int) -> None:
            nonlocal total
            for node in nodes:
                total += 1
                if total > 40:
                    raise ValueError("code architecture allows at most 40 total nodes")
                if node.id in component_ids:
                    raise ValueError(f"duplicate code architecture component id: {node.id}")
                component_ids.add(node.id)
                unknown = set(node.source_evidence_ids) - evidence_ids
                if unknown:
                    raise ValueError(
                        f"component {node.id} references unknown source evidence: {sorted(unknown)}"
                    )
                if depth >= 3 and node.children:
                    raise ValueError("code architecture depth is capped at 3 levels")
                if depth == 2 and len(node.children) > 6:
                    raise ValueError("code architecture level-3 detail is capped at 6 children per node")
                walk(node.children, depth + 1)

        walk(self.components, 1)
        total_evidence_chars = sum(len(item.excerpt) for item in self.source_evidence)
        if total_evidence_chars > 200_000:
            raise ValueError("code architecture source evidence is capped at 200,000 characters")
        for relationship in self.relationships:
            if relationship.source not in component_ids or relationship.target not in component_ids:
                raise ValueError("code architecture relationships must reference existing component ids")
            unknown = set(relationship.source_evidence_ids) - evidence_ids
            if unknown:
                raise ValueError(
                    "relationship "
                    f"{relationship.source}->{relationship.target} references unknown source evidence: {sorted(unknown)}"
                )
        if self.code_truth is not None:
            evidence_by_id = {item.id: item for item in self.source_evidence}
            for symbol in self.code_truth.symbols:
                if symbol.source_evidence_id is None:
                    continue
                evidence = evidence_by_id.get(symbol.source_evidence_id)
                if evidence is None:
                    raise ValueError(
                        f"code truth symbol {symbol.id} references unknown source evidence: {symbol.source_evidence_id}"
                    )
                if symbol.path != evidence.path:
                    raise ValueError(f"code truth symbol {symbol.id} path must match its source evidence")
                if symbol.line_start < evidence.line_start or symbol.line_end > evidence.line_end:
                    raise ValueError(f"code truth symbol {symbol.id} range must be covered by its source evidence")
            for relationship in self.code_truth.relationships:
                unknown = set(relationship.source_evidence_ids) - evidence_ids
                if unknown:
                    raise ValueError(
                        "code truth relationship "
                        f"{relationship.source}->{relationship.target} references unknown source evidence: {sorted(unknown)}"
                    )
        return self


def _source_href(repository: str, revision: str, evidence: CodeSourceEvidence) -> str:
    line_fragment = f"#L{evidence.line_start}"
    if evidence.line_end != evidence.line_start:
        line_fragment += f"-L{evidence.line_end}"
    encoded_path = "/".join(quote(segment, safe="") for segment in evidence.path.split("/"))
    return f"https://github.com/{repository}/blob/{revision}/{encoded_path}{line_fragment}"


def _edge_id(
    *,
    repository: str,
    revision: str,
    relationship: CodeArchitectureRelationship,
    occurrence: int,
) -> str:
    payload = "\x1f".join(
        [
            repository,
            revision,
            relationship.source,
            relationship.target,
            relationship.relationship_type,
            relationship.description,
            ",".join(sorted(relationship.source_evidence_ids)),
            str(occurrence),
        ]
    )
    return "code-edge:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _validate_code_truth_architecture_bindings(
    request: CodeArchitectureSnapshotRequest,
    architecture: Architecture | None,
) -> None:
    if request.code_truth is None:
        return
    mapped = {
        symbol.architecture_component_id
        for symbol in request.code_truth.symbols
        if symbol.architecture_component_id is not None
    }
    if not mapped:
        return
    if architecture is None:
        raise ValueError("canonical Architecture is required to validate Code Truth bindings")
    unknown = mapped.difference(architecture.component_ids())
    if unknown:
        raise ValueError(
            "code truth references unknown canonical architecture components: "
            + ", ".join(sorted(unknown))
        )


def _code_truth_summary(
    request: CodeArchitectureSnapshotRequest,
    architecture: Architecture | None,
) -> dict | None:
    index = request.code_truth
    if index is None:
        return None
    _validate_code_truth_architecture_bindings(request, architecture)
    relationship_counts: dict[str, int] = defaultdict(int)
    for relationship in index.relationships:
        relationship_counts[relationship.relationship_type] += 1
    mapped_components = sorted(
        {
            symbol.architecture_component_id
            for symbol in index.symbols
            if symbol.architecture_component_id is not None
        }
    )
    fingerprint = hashlib.sha256(index.model_dump_json().encode("utf-8")).hexdigest()
    return {
        "schema": "archbro.code_truth.v1",
        "classification": "VERIFIED_CODE_TRUTH",
        "revision": request.revision,
        "extractor": index.extractor,
        "extractor_version": index.extractor_version,
        "revision_verified": True,
        "index_fingerprint": fingerprint,
        "symbol_count": len(index.symbols),
        "relationship_count": len(index.relationships),
        "relationship_counts": dict(sorted(relationship_counts.items())),
        "mapped_symbol_count": sum(
            1 for symbol in index.symbols if symbol.architecture_component_id is not None
        ),
        "mapped_architecture_component_ids": mapped_components,
        "incremental_update": {
            "base_revision": index.base_revision,
            "changed_path_count": len(index.changed_paths),
            "changed_paths": list(index.changed_paths),
        },
        "full_symbol_graph_embedded": False,
        "query_mode": "BOUNDED_ON_DEMAND",
    }


def _code_truth_relationship_id(relationship: CodeTruthRelationship) -> str:
    payload = "\x1f".join(
        [
            relationship.source,
            relationship.target,
            relationship.relationship_type,
            ",".join(sorted(relationship.source_evidence_ids)),
        ]
    )
    return "code-truth-edge:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def build_code_truth_impact(
    project_id: str,
    request: CodeArchitectureSnapshotRequest,
    symbol_id: str,
    *,
    direction: Literal["callers", "callees", "both"] = "callers",
    max_hops: int = 2,
    max_results: int = 40,
    architecture: Architecture | None = None,
) -> dict:
    index = request.code_truth
    if index is None:
        raise ValueError("latest Code Architecture snapshot has no verified Code Truth index")
    _validate_code_truth_architecture_bindings(request, architecture)
    if max_hops < 1 or max_hops > 5:
        raise ValueError("code truth impact max_hops must be between 1 and 5")
    if max_results < 1 or max_results > 100:
        raise ValueError("code truth impact max_results must be between 1 and 100")

    symbols = {symbol.id: symbol for symbol in index.symbols}
    origin = symbols.get(symbol_id)
    if origin is None:
        raise ValueError(f"code truth symbol not found: {symbol_id}")

    directions = ("callers", "callees") if direction == "both" else (direction,)
    reached: dict[str, dict[str, object]] = {}
    traversed_relationship_ids: set[str] = set()
    hit_hop_limit = False

    for current_direction in directions:
        adjacency: dict[str, list[CodeTruthRelationship]] = defaultdict(list)
        for relationship in index.relationships:
            key = relationship.target if current_direction == "callers" else relationship.source
            adjacency[key].append(relationship)
        for relationships in adjacency.values():
            relationships.sort(
                key=lambda relationship: (
                    relationship.relationship_type,
                    relationship.source,
                    relationship.target,
                )
            )
        visited = {symbol_id}
        queue: deque[tuple[str, int]] = deque([(symbol_id, 0)])
        while queue:
            current, hop = queue.popleft()
            candidates = adjacency.get(current, [])
            if hop >= max_hops:
                if any(
                    (relationship.source if current_direction == "callers" else relationship.target)
                    not in visited
                    for relationship in candidates
                ):
                    hit_hop_limit = True
                continue
            for relationship in candidates:
                traversed_relationship_ids.add(_code_truth_relationship_id(relationship))
                neighbor = relationship.source if current_direction == "callers" else relationship.target
                if neighbor in visited:
                    continue
                visited.add(neighbor)
                item = reached.setdefault(neighbor, {"hop": hop + 1, "directions": set()})
                item["hop"] = min(int(item["hop"]), hop + 1)
                matched = item["directions"]
                assert isinstance(matched, set)
                matched.add(current_direction)
                queue.append((neighbor, hop + 1))

    ordered_ids = sorted(reached, key=lambda item: (int(reached[item]["hop"]), item))
    hit_result_limit = len(ordered_ids) > max_results
    kept_ids = ordered_ids[:max_results]
    allowed_ids = {symbol_id, *kept_ids}

    def compact_symbol(symbol: CodeTruthSymbol) -> dict:
        return {
            "id": symbol.id,
            "qualified_name": symbol.qualified_name,
            "kind": symbol.kind,
            "path": symbol.path,
            "line_start": symbol.line_start,
            "line_end": symbol.line_end,
            "architecture_component_id": symbol.architecture_component_id,
            "architecture_node_id": (
                f"node:{symbol.architecture_component_id}"
                if symbol.architecture_component_id is not None
                else None
            ),
        }

    nodes = []
    for current_id in kept_ids:
        node = compact_symbol(symbols[current_id])
        node["hop"] = int(reached[current_id]["hop"])
        matched = reached[current_id]["directions"]
        assert isinstance(matched, set)
        node["matched_directions"] = sorted(matched)
        nodes.append(node)

    relationships = []
    for relationship in index.relationships:
        relationship_id = _code_truth_relationship_id(relationship)
        if relationship_id not in traversed_relationship_ids:
            continue
        if relationship.source not in allowed_ids or relationship.target not in allowed_ids:
            continue
        relationships.append(
            {
                "id": relationship_id,
                "source": relationship.source,
                "target": relationship.target,
                "relationship_type": relationship.relationship_type,
                "source_evidence_ids": list(relationship.source_evidence_ids),
            }
        )
    relationships.sort(key=lambda item: item["id"])

    affected_architecture_node_ids = sorted(
        {
            f"node:{symbol.architecture_component_id}"
            for symbol in [origin, *(symbols[item] for item in kept_ids)]
            if symbol.architecture_component_id is not None
        }
    )
    limit_reasons = []
    if hit_hop_limit:
        limit_reasons.append("MAX_HOPS")
    if hit_result_limit:
        limit_reasons.append("MAX_RESULTS")

    return {
        "schema": "archbro.code_truth_impact.v1",
        "project_id": project_id,
        "repository": request.repository,
        "revision": request.revision,
        "classification": "STRUCTURAL_BLAST_RADIUS",
        "runtime_breakage_claimed": False,
        "origin": compact_symbol(origin),
        "query": {
            "direction": direction,
            "max_hops": max_hops,
            "max_results": max_results,
        },
        "symbols": nodes,
        "relationships": relationships,
        "affected_architecture_node_ids": affected_architecture_node_ids,
        "truncated": hit_hop_limit or hit_result_limit,
        "limit_reason": (
            "MAX_RESULTS" if hit_result_limit else "MAX_HOPS" if hit_hop_limit else None
        ),
        "limit_reasons": limit_reasons,
    }


def build_code_architecture_snapshot(
    project_id: str,
    request: CodeArchitectureSnapshotRequest,
    *,
    architecture: Architecture | None = None,
) -> dict:
    code_truth_summary = _code_truth_summary(request, architecture)
    evidence_by_id = {item.id: item for item in request.source_evidence}
    nodes: list[dict] = []

    def visit(
        components: list[CodeArchitectureComponent],
        *,
        parent_node_id: str | None,
        depth: int,
    ) -> None:
        for component in sorted(components, key=lambda item: item.id):
            node_id = f"code-node:{component.id}"
            sources = [
                {
                    **evidence_by_id[evidence_id].model_dump(mode="json"),
                    "href": _source_href(
                        request.repository,
                        request.revision,
                        evidence_by_id[evidence_id],
                    ),
                }
                for evidence_id in component.source_evidence_ids
            ]
            nodes.append(
                {
                    "id": node_id,
                    "component_id": component.id,
                    "semantic_kind": component.kind.value,
                    "semantic_type": component.type,
                    "label": component.name,
                    "responsibility": component.responsibility,
                    "parent_id": parent_node_id,
                    "depth": depth,
                    "source_evidence_ids": list(component.source_evidence_ids),
                    "sources": sources,
                    "child_count": len(component.children),
                }
            )
            visit(component.children, parent_node_id=node_id, depth=depth + 1)

    visit(request.components, parent_node_id=None, depth=1)

    occurrences: dict[tuple[str, str, str, str], int] = {}
    edges: list[dict] = []
    for relationship in sorted(
        request.relationships,
        key=lambda item: (
            item.source,
            item.target,
            item.relationship_type,
            item.description,
        ),
    ):
        key = (
            relationship.source,
            relationship.target,
            relationship.relationship_type,
            relationship.description,
        )
        occurrence = occurrences.get(key, 0)
        occurrences[key] = occurrence + 1
        evidence = [
            {
                **evidence_by_id[evidence_id].model_dump(mode="json"),
                "href": _source_href(
                    request.repository,
                    request.revision,
                    evidence_by_id[evidence_id],
                ),
            }
            for evidence_id in relationship.source_evidence_ids
        ]
        edges.append(
            {
                "id": _edge_id(
                    repository=request.repository,
                    revision=request.revision,
                    relationship=relationship,
                    occurrence=occurrence,
                ),
                "source": f"code-node:{relationship.source}",
                "target": f"code-node:{relationship.target}",
                "semantic_type": relationship.relationship_type,
                "label": relationship.relationship_type,
                "supporting_text": relationship.description,
                "source_evidence_ids": list(relationship.source_evidence_ids),
                "sources": evidence,
            }
        )

    diagram = {
        "diagram_version": CODE_DIAGRAM_VERSION,
        "repository_revision": request.revision,
        "summary": request.summary,
        "nodes": nodes,
        "edges": edges,
    }
    positioned = layout_diagram(diagram)
    return {
        "schema": CODE_ARCHITECTURE_SCHEMA,
        "project_id": project_id,
        "classification": "IMPLEMENTATION_EVIDENCE",
        "canonical_state_mutated": False,
        "repository": {
            "provider": "github",
            "slug": request.repository,
            "url": f"https://github.com/{request.repository}",
            "revision": request.revision,
            "revision_pinned": True,
        },
        "evidence_verification": {
            "mode": "REVISION_PINNED_AGENT_SUPPLIED",
            "repository_checkout_verified": False,
            "note": (
                "Source excerpts are structurally validated and pinned to the supplied full commit SHA. "
                "ArchBro does not claim that the server independently fetched the repository; the agent "
                "must obtain these excerpts from the connected GitHub MCP at this exact revision."
            ),
        },
        "summary": request.summary,
        "code_truth": code_truth_summary,
        "source_evidence": [
            {
                **item.model_dump(mode="json"),
                "href": _source_href(request.repository, request.revision, item),
            }
            for item in request.source_evidence
        ],
        "diagram": diagram,
        "positioned_graph": {
            "layout_version": positioned.layout_version,
            "diagram_version": positioned.diagram_version,
            "architecture_version": positioned.architecture_version,
            "width": positioned.width,
            "height": positioned.height,
            "nodes": [
                {
                    "node_id": node.node_id,
                    "x": node.x,
                    "y": node.y,
                    "width": node.width,
                    "height": node.height,
                    "layer": node.layer,
                    "order": node.order,
                    "parent_id": node.parent_id,
                    "hierarchy_path": list(node.hierarchy_path),
                }
                for node in positioned.nodes
            ],
            "edges": [
                {
                    "edge_id": edge.edge_id,
                    "source": edge.source,
                    "target": edge.target,
                    "points": [{"x": point.x, "y": point.y} for point in edge.points],
                    "routing": edge.routing,
                    "order": edge.order,
                }
                for edge in positioned.edges
            ],
            "stable_order": list(positioned.stable_order),
        },
    }
