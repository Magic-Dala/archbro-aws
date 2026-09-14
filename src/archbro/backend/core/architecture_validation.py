from __future__ import annotations

from archbro.backend.core.contracts import Architecture


def validate_architecture_relationship_connectivity(
    architecture: Architecture,
    *,
    label: str = "architecture",
) -> None:
    """Validate the shared relationship invariants for an initial architecture."""

    components = architecture.all_components()
    leaves = {component.id for component in components if not component.children}
    adjacency: dict[str, set[str]] = {component.id: set() for component in components}
    incident: set[str] = set()
    directed_keys: set[tuple[str, str, str]] = set()

    for relationship in architecture.relationships:
        if relationship.source == relationship.target:
            raise ValueError(f"{label} relationships cannot be self-referential")
        key = (
            relationship.source,
            relationship.target,
            relationship.relationship_type,
        )
        if key in directed_keys:
            raise ValueError(f"{label} relationships cannot duplicate source/target/type")
        directed_keys.add(key)
        adjacency[relationship.source].add(relationship.target)
        adjacency[relationship.target].add(relationship.source)
        incident.add(relationship.source)
        incident.add(relationship.target)

    if len(leaves) <= 1:
        return

    isolated = sorted(leaves - incident)
    if isolated:
        raise ValueError(f"{label} left architecture leaves isolated: " + ", ".join(isolated))

    seed = min(leaves)
    visited: set[str] = set()
    pending = [seed]
    while pending:
        current = pending.pop()
        if current in visited:
            continue
        visited.add(current)
        pending.extend(adjacency[current] - visited)
    disconnected = sorted(leaves - visited)
    if disconnected:
        raise ValueError(
            f"{label} relationships do not weakly connect all architecture leaves: "
            + ", ".join(disconnected)
        )
