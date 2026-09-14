"""Readability is a projection: preserve accepted data and every directed edge."""
from dataclasses import asdict
import json
from pathlib import Path

import pytest

from archbro.backend.core.canvas_reading import canvas_reading_views
from archbro.backend.core.canvas_budget import CanvasWorkBudget
from archbro.backend.core.contracts import Architecture
from archbro.backend.core.diagram import project_diagram
from archbro.backend.core.diagram_layout import CANVAS_LAYOUT_VERSION, layout_canvas_diagram
from qa.reference_projects import architecture, digest

CORPUS = Path(__file__).resolve().parents[1] / "examples/reference-projects/v1"
DENSE_GEOMETRY = Path(__file__).resolve().parent / "fixtures" / "canvas_dense_geometry.json"


def assert_clear_routes(layout):
    nodes = {node.node_id: node for node in layout.graph.nodes}
    for edge in layout.graph.edges:
        for a, b, c in zip(edge.points, edge.points[1:], edge.points[2:]):
            assert (b.x-a.x)*(c.x-b.x)+(b.y-a.y)*(c.y-b.y) >= 0, (edge.edge_id, "route doubles back")
        for point, node_id in ((edge.points[0], edge.source), (edge.points[-1], edge.target)):
            node = nodes[node_id]
            assert (
                point.x in (node.x, node.x + node.width) and node.y <= point.y <= node.y + node.height
                or point.y in (node.y, node.y + node.height) and node.x <= point.x <= node.x + node.width
            ), (edge.edge_id, "endpoint not on its own node")
        for a, b in zip(edge.points, edge.points[1:]):
            assert a.x == b.x or a.y == b.y, (edge.edge_id, "diagonal")
            for node in nodes.values():
                if a.x == b.x:
                    hit = node.x < a.x < node.x + node.width and max(min(a.y, b.y), node.y) < min(max(a.y, b.y), node.y + node.height)
                else:
                    hit = node.y < a.y < node.y + node.height and max(min(a.x, b.x), node.x) < min(max(a.x, b.x), node.x + node.width)
                assert not hit, (edge.edge_id, node.node_id, "route through card/header")


@pytest.mark.parametrize("name", ["01-commerce", "02-ai-workspace", "03-manufacturing"])
def test_frozen_corpus_geometry_and_authored_journeys(name):
    sample = json.loads((CORPUS / f"{name}.json").read_text(encoding="utf-8"))
    accepted = Architecture.model_validate(architecture(sample["payload"]))
    before = digest(accepted.model_dump(mode="json"))
    diagram = project_diagram(accepted, tasks=[], proposals=[])
    full = layout_canvas_diagram(diagram)
    reordered = diagram.model_copy(update={"nodes": list(reversed(diagram.nodes)), "edges": list(reversed(diagram.edges))})
    assert asdict(full) == asdict(layout_canvas_diagram(reordered))
    assert {e.edge_id for e in full.graph.edges} == {e.id for e in diagram.edges}
    assert_clear_routes(full)
    views = canvas_reading_views(accepted, diagram)
    assert len(views) == 4
    for scenario, view in zip(sample["scenarios"], views[1:]):
        assert view["label"] == scenario["name"]
        assert view["node_ids"] == [f"node:{node}" for node in scenario["path"]]
        assert view["architecture_sha256"] == before
        for step in view["steps"]:
            edge = next(e for e in diagram.edges if e.id == step["edge_id"])
            assert (edge.source, edge.target) == (step["source"], step["target"])
    subset = layout_canvas_diagram(diagram, route_edge_ids=views[0]["edge_ids"])
    assert full.graph.nodes == subset.graph.nodes
    full_edges = {edge.edge_id: edge for edge in full.graph.edges}
    assert all(edge == full_edges[edge.edge_id] for edge in subset.graph.edges)
    assert digest(accepted.model_dump(mode="json")) == before
    changed = accepted.model_copy(update={"version": accepted.version + 1})
    assert len(canvas_reading_views(changed, project_diagram(changed, tasks=[], proposals=[]))) == 1
    first_id = views[1]["steps"][0]["edge_id"]
    missing = diagram.model_copy(update={"edges": [e for e in diagram.edges if e.id != first_id]})
    assert views[1]["id"] not in {v["id"] for v in canvas_reading_views(accepted, missing)}
    duplicate = next(e for e in diagram.edges if e.id == first_id).model_copy(update={"id": "ambiguous"})
    ambiguous = diagram.model_copy(update={"edges": [*diagram.edges, duplicate]})
    assert views[1]["id"] not in {v["id"] for v in canvas_reading_views(accepted, ambiguous)}


def test_empty_canvas_keeps_canvas_layout_identity():
    assert layout_canvas_diagram({"nodes": [], "edges": []}).graph.layout_version == CANVAS_LAYOUT_VERSION


def test_nested_group_ports_reverse_edges_parallel_actions_and_self_loop():
    nodes = [
        {"id": "root", "label": "Root", "parent_id": None},
        {"id": "group", "label": "Group", "parent_id": "root"},
        {"id": "a", "label": "A", "parent_id": "group"},
        {"id": "b", "label": "B", "parent_id": "group"},
        {"id": "outside", "label": "Outside", "parent_id": None},
    ]
    pairs = [("a", "b"), ("b", "a"), ("a", "b"), ("a", "a"), ("group", "a"), ("a", "root"), ("outside", "b")]
    edges = [{"id": f"edge:{i}", "source": a, "target": b} for i, (a, b) in enumerate(pairs)]
    layout = layout_canvas_diagram({"nodes": nodes, "edges": edges})
    assert len(layout.graph.edges) == len(edges)
    assert_clear_routes(layout)
    assert len({tuple((p.x, p.y) for p in e.points) for e in layout.graph.edges}) == len(edges)


@pytest.mark.parametrize("vertical", [False, True])
def test_close_cards_have_direct_terminal_runs_without_arrow_hairpins(vertical):
    from types import SimpleNamespace
    from archbro.backend.core.canvas_routing import route_canvas_connections

    nodes = [SimpleNamespace(node_id="a", x=0, y=0, width=224, height=80),
             SimpleNamespace(node_id="b", x=0 if vertical else 256,
                             y=112 if vertical else 0, width=224, height=80)]
    edges = [SimpleNamespace(edge_id="forward", source="a", target="b"),
             SimpleNamespace(edge_id="reverse", source="b", target="a")]
    routes = route_canvas_connections(nodes, edges)
    for route in routes.values():
        # Opposing 18-unit stubs used to overshoot in this 32-unit gap.
        assert len(route) == 2
        assert sum(abs(b-a) for a, b in zip(route[0], route[-1])) == 32
    assert routes["forward"] == tuple(reversed(routes["reverse"]))


def test_reciprocal_routes_keep_both_ids_and_do_not_bundle_parallel_actions():
    nodes = [{"id": "a"}, {"id": "b"}]
    edges = [{"id": "out", "source": "a", "target": "b"},
             {"id": "back", "source": "b", "target": "a"}]
    layout = layout_canvas_diagram({"nodes": nodes, "edges": edges})
    a, b = layout.graph.edges
    assert {a.edge_id, b.edge_id} == {"out", "back"}
    assert a.points == tuple(reversed(b.points))
    assert a.routing == b.routing == "ORTHOGONAL_CANVAS_RECIPROCAL"
    multiple = layout_canvas_diagram({"nodes": nodes, "edges": [*edges, {"id": "another", "source": "a", "target": "b"}]})
    assert all(e.routing == "ORTHOGONAL_CANVAS" for e in multiple.graph.edges)


def test_checkout_payment_can_use_the_open_channel_through_group_boundaries():
    sample = json.loads((CORPUS / "01-commerce.json").read_text(encoding="utf-8"))
    accepted = Architecture.model_validate(architecture(sample["payload"]))
    diagram = project_diagram(accepted, tasks=[], proposals=[])
    reading_ids = {edge for view in canvas_reading_views(accepted, diagram) if view['kind'] == 'AUTHORED_JOURNEY' for edge in view['edge_ids']}
    layout = layout_canvas_diagram(diagram, reading_edge_ids=reading_ids)
    edge = next(e for e in layout.graph.edges if e.source == 'node:checkout_api' and e.target == 'node:payment_router')
    length = sum(abs(a.x-b.x)+abs(a.y-b.y) for a,b in zip(edge.points,edge.points[1:]))
    first,last = edge.points[0],edge.points[-1]
    # A monotone route is feasible here; the old invisible header wall forced
    # an 1108-unit detour. Assert the geometric lower bound, not saved waypoints.
    assert length == abs(first.x-last.x)+abs(first.y-last.y)
    assert_clear_routes(layout)


@pytest.mark.parametrize("vertical", [True, False])
def test_one_way_connection_with_unequal_port_load_stays_straight(vertical):
    from types import SimpleNamespace
    from archbro.backend.core.canvas_routing import route_canvas_connections

    def node(name, y):
        box = (0,y,224,80) if vertical else (y,0,80,224)
        return SimpleNamespace(node_id=name, x=box[0], y=box[1], width=box[2], height=box[3])
    nodes = [node('a',0),node('b',112),node('c',224)]
    edges = [SimpleNamespace(edge_id='direct',source='a',target='b'),
             SimpleNamespace(edge_id='beyond',source='a',target='c')]
    routes = route_canvas_connections(nodes,edges)
    assert len(routes['direct']) == 2
    assert sum(abs(a-b) for a,b in zip(*routes['direct'])) == 32
    assert len(routes['beyond']) > 2, 'The intervening card must still be avoided'
    assert routes == route_canvas_connections(list(reversed(nodes)),list(reversed(edges)))


def test_crossing_a_relationship_does_not_bend_a_clear_straight_line():
    from types import SimpleNamespace
    from archbro.backend.core.canvas_routing import route_canvas_connections

    def node(name,x,y):
        return SimpleNamespace(node_id=name,x=x,y=y,width=80,height=80)
    nodes=[node('left',0,200),node('right',400,200),node('top',200,0),node('bottom',200,400)]
    edges=[SimpleNamespace(edge_id='horizontal',source='left',target='right'),
           SimpleNamespace(edge_id='vertical',source='top',target='bottom')]
    routes=route_canvas_connections(nodes,edges)
    assert all(len(points)==2 for points in routes.values())


def test_event_bus_connections_have_distinct_channels_and_spaced_ports():
    sample=json.loads((CORPUS/'01-commerce.json').read_text(encoding='utf-8'))
    accepted=Architecture.model_validate(architecture(sample['payload']))
    diagram=project_diagram(accepted,tasks=[],proposals=[])
    reading_ids={edge for view in canvas_reading_views(accepted,diagram) if view['kind']=='AUTHORED_JOURNEY' for edge in view['edge_ids']}
    layout=layout_canvas_diagram(diagram,reading_edge_ids=reading_ids)
    incident=[e for e in layout.graph.edges if 'node:event_bus' in (e.source,e.target)]
    assert len(incident)==9
    # Count the actual two-way connector once, retaining both original IDs.
    unique={min(tuple((p.x,p.y) for p in e.points),tuple((p.x,p.y) for p in reversed(e.points))):e for e in incident}
    assert len(unique)==8
    hub=next(n for n in layout.graph.nodes if n.node_id=='node:event_bus')
    ports=[]
    for edge in unique.values():
        p=edge.points[0] if edge.source==hub.node_id else edge.points[-1]
        side='left' if p.x==hub.x else 'right' if p.x==hub.x+hub.width else 'top' if p.y==hub.y else 'bottom'
        ports.append((side,p))
    for i,(side,p) in enumerate(ports):
        for other,q in ports[i+1:]:
            if side==other:
                assert abs(p.x-q.x)+abs(p.y-q.y)>=16
    paths=list(unique)
    # Regression: exact overlap was zero while the old hub still had five
    # crossings and 3488 units of sub-12-unit parallel crowding.
    from itertools import combinations
    from archbro.backend.core.canvas_route_separation import route_conflicts
    conflicts=[route_conflicts(a,b) for a,b in combinations(paths,2)]
    assert sum(len(points) for points,_ in conflicts) <= 1
    assert sum(crowding for _,crowding in conflicts) == 0
    for i,points in enumerate(paths):
        for other in paths[i+1:]:
            for a,b in zip(points,points[1:]):
                for p,q in zip(other,other[1:]):
                    for axis in (0,1):
                        fixed=1-axis
                        if a[fixed]==b[fixed]==p[fixed]==q[fixed]:
                            assert min(max(a[axis],b[axis]),max(p[axis],q[axis]))<=max(min(a[axis],b[axis]),min(p[axis],q[axis]))
    assert_clear_routes(layout)


def test_dense_hub_rebalances_overloaded_ports_across_card_sides():
    from types import SimpleNamespace
    from archbro.backend.core.canvas_routing import route_canvas_connections

    hub=SimpleNamespace(node_id='hub',x=0,y=200,width=224,height=80)
    peers=[SimpleNamespace(node_id=f'p{i}',x=420,y=i*140,width=80,height=80) for i in range(9)]
    edges=[SimpleNamespace(edge_id=f'e{i}',source='hub',target=peer.node_id) for i,peer in enumerate(peers)]
    routes=route_canvas_connections([hub,*peers],edges)
    ports=[]
    for edge in edges:
        p=routes[edge.edge_id][0]
        side='left' if p[0]==hub.x else 'right' if p[0]==hub.x+hub.width else 'top' if p[1]==hub.y else 'bottom'
        ports.append((side,p))
    assert len({side for side,_ in ports}) >= 2
    for index,(side,p) in enumerate(ports):
        for other,q in ports[index+1:]:
            if side==other:
                assert abs(p[0]-q[0])+abs(p[1]-q[1]) >= 16


def test_extreme_hub_expands_canvas_geometry_instead_of_overlapping_ports():
    hub={"id":"hub","semantic_kind":"SERVICE","semantic_type":"service"}
    peers=[{"id":f"peer-{index}","semantic_kind":"SERVICE","semantic_type":"service"} for index in range(39)]
    edges=[{"id":f"edge-{index}","source":"hub","target":peer["id"],"relationship_category":"FLOW"} for index,peer in enumerate(peers)]

    layout=layout_canvas_diagram({"nodes":[hub,*peers],"edges":edges})
    positioned_hub=next(node for node in layout.graph.nodes if node.node_id=="hub")
    assert positioned_hub.width > 224
    ports=[]
    for edge in layout.graph.edges:
        point=edge.points[0]
        side=(
            "left" if point.x==positioned_hub.x else
            "right" if point.x==positioned_hub.x+positioned_hub.width else
            "top" if point.y==positioned_hub.y else "bottom"
        )
        ports.append((side,point))
    for index,(side,point) in enumerate(ports):
        for other,peer_point in ports[index+1:]:
            if side==other:
                assert abs(point.x-peer_point.x)+abs(point.y-peer_point.y) >= 16


def test_fixed_geometry_fails_closed_when_endpoint_capacity_is_impossible():
    from types import SimpleNamespace
    from archbro.backend.core.canvas_routing import route_canvas_connections

    hub=SimpleNamespace(node_id="hub",x=0,y=200,width=224,height=80)
    peers=[SimpleNamespace(node_id=f"p{i}",x=420,y=i*140,width=80,height=80) for i in range(39)]
    edges=[SimpleNamespace(edge_id=f"e{i}",source="hub",target=peer.node_id) for i,peer in enumerate(peers)]
    with pytest.raises(ValueError, match="port capacity exceeded"):
        route_canvas_connections([hub,*peers],edges)


def planner_limit_architecture(shape: str) -> Architecture:
    from archbro.backend.core.contracts import Component, Relationship

    leaves = [f"service-{index:02d}" for index in range(34)]
    boundaries = (0, 6, 12, 18, 24, 29, 34)
    accepted = Architecture(
        version=1,
        components=[
            Component(
                id=f"system-{root}", name=f"System {root}", type="system",
                responsibility="Own this service boundary.",
                children=[
                    Component(id=node, name=node, type="service", responsibility="Handle requests.")
                    for node in leaves[boundaries[root]:boundaries[root + 1]]
                ],
            )
            for root in range(6)
        ],
        relationships=[
            Relationship(source=node, target=leaves[(index + offset) % 34], relationship_type="CALLS")
            for index, node in enumerate(leaves) for offset in (1, 5)
        ] + [
            Relationship(source=f"system-{root}", target=leaves[boundaries[root] + offset], relationship_type="CALLS")
            for root in range(6) for offset in (0, 4)
        ],
    )
    if shape == "hub":
        peers = [f"system-{root}" for root in range(6)] + leaves[1:]
        pairs = [(leaves[0], peer) for peer in peers]
        pairs += [(peer, peers[(index + offset) % len(peers)])
                  for index, peer in enumerate(peers) for offset in (1, 5)][:41]
        accepted.relationships = [
            Relationship(source=source, target=target, relationship_type="CALLS")
            for source, target in pairs
        ]
        accepted = Architecture.model_validate(accepted.model_dump(mode="json"))
    return accepted


def json_geometry(layout) -> dict:
    return json.loads(json.dumps(asdict(layout), ensure_ascii=False, sort_keys=True))


@pytest.mark.parametrize("shape", ["ring", "hub"])
def test_planner_limit_canvas_keeps_all_routes_within_uncached_budget(monkeypatch, shape):
    """Exercise the engine, not the API cache, at 40 components / 80 links."""
    from time import perf_counter
    from archbro.backend.core import canvas_routing

    accepted = planner_limit_architecture(shape)
    before = digest(accepted.model_dump(mode="json"))
    diagram = project_diagram(accepted, tasks=[], proposals=[])
    assert len(diagram.nodes) == 40
    assert len(diagram.edges) == 80
    separation_seconds = []
    separate = canvas_routing.separate_canvas_routes

    def measured_separation(*args, **kwargs):
        started = perf_counter()
        try:
            return separate(*args, **kwargs)
        finally:
            separation_seconds.append(perf_counter() - started)

    monkeypatch.setattr(canvas_routing, "separate_canvas_routes", measured_separation)
    work_budget = CanvasWorkBudget()
    started = perf_counter()
    layout = layout_canvas_diagram(diagram, work_budget=work_budget)
    elapsed = perf_counter() - started
    print(json.dumps({"shape": shape, "dense_nodes": 40, "dense_edges": 80, "layout_seconds": elapsed,
                      "separation_seconds": sum(separation_seconds),
                      "geometry_sha256": digest(asdict(layout)),
                      "work_budget": work_budget.snapshot()}))
    assert {edge.edge_id for edge in layout.graph.edges} == {edge.id for edge in diagram.edges}
    assert_clear_routes(layout)
    assert digest(accepted.model_dump(mode="json")) == before
    # The readable fixture is the contract; the hash is only a compact log
    # fingerprint. Python 3.11 and 3.13 both compare this exact structure.
    expected_geometry = json.loads(DENSE_GEOMETRY.read_text(encoding="utf-8"))[shape]
    assert json_geometry(layout) == expected_geometry
    counters = work_budget.snapshot()
    assert 0 < counters["route_expansions"] <= work_budget.max_route_expansions
    assert 0 < counters["route_candidates"] <= work_budget.max_route_candidates
    assert 0 < counters["pair_evaluations"] <= work_budget.max_pair_evaluations
    # Wall clock remains a smoke signal only; deterministic operation limits
    # above are the actual cold-request safety contract.
    assert elapsed < 15.0, "uncached Canvas routing exceeded the smoke benchmark envelope"


def test_reference_corpus_lock_is_recomputed_in_ci():
    from qa.reference_projects import SAMPLES, read, validate_sample

    lock = read(CORPUS / "corpus.lock.json")
    expected = {
        "schema": "archbro.reference-corpus-lock.v1",
        "corpus_version": 1,
        "samples": {
            key: validate_sample(read(CORPUS / f"{key}.json"))
            for key in SAMPLES
        },
    }
    assert lock == expected


def test_cached_segment_conflicts_preserve_reference_costs_and_crossings():
    from random import Random
    from archbro.backend.core.canvas_route_separation import route_conflicts

    def reference(a, b, spacing):
        crossings, crowding = set(), 0.0
        for p, q in zip(a, a[1:]):
            axis = 0 if p[1] == q[1] else 1
            fixed = 1-axis
            low, high = sorted((p[axis], q[axis]))
            for r, s in zip(b, b[1:]):
                if r[fixed] == s[fixed]:
                    distance = abs(p[fixed]-r[fixed])
                    overlap = min(high, max(r[axis], s[axis]))-max(low, min(r[axis], s[axis]))
                    if distance < spacing and overlap > 0:
                        crowding += (spacing-distance)*overlap
                elif low <= r[axis] <= high and min(r[fixed], s[fixed]) <= p[fixed] <= max(r[fixed], s[fixed]):
                    crossings.add((r[0], p[1]) if axis == 0 else (p[0], r[1]))
        return crossings, crowding

    rng = Random(260907)
    for _ in range(200):
        routes = []
        for _ in range(2):
            route = [(rng.randrange(-30, 31), rng.randrange(-30, 31))]
            for _ in range(rng.randrange(1, 8)):
                point = list(route[-1])
                point[rng.randrange(2)] += rng.choice([-48, -12, -6, 0, 6, 12, 48])
                route.append(tuple(point))
            routes.append(route)
        for spacing in (0.0, 6.0, 12.0, 24.0):
            assert route_conflicts(*routes, spacing) == reference(*routes, spacing)
