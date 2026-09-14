"""Version-pinned English display copy; never rewrite accepted architecture."""
import hashlib
import json
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def _catalog():
    return json.loads(Path(__file__).with_name("canvas_reference_en.json").read_text(encoding="utf-8"))


def canvas_presentation(architecture, diagram):
    encoded = json.dumps(architecture.model_dump(mode="json"), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
    entry = _catalog().get(fingerprint)
    if entry is None:
        return None
    nodes = {node.id: entry["nodes"][node.id] for node in diagram.nodes if node.id in entry["nodes"]}
    if len(nodes) != len(diagram.nodes):
        return None
    edges = {}
    for edge in diagram.edges:
        label = entry["relationship_types"].get(edge.semantic_type)
        if label is None:
            return None
        edges[edge.id] = {"label": label}
    return {
        "locale": "en",
        "source": f"reference-projects/{entry['sample_id']}/english-display-v1",
        "architecture_sha256": fingerprint,
        "architecture_version": architecture.version,
        "title": entry["title"], "subtitle": entry["subtitle"],
        "nodes": nodes, "edges": edges,
        "reading_labels": {f"{entry['sample_id']}:{i}": label for i, label in enumerate(entry["journeys"], 1)},
    }
