from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace as N

import pytest

from archbro.backend.core.canvas_connection_summaries import canvas_connection_summaries


@pytest.mark.parametrize('reverse', [False, True])
def test_real_obstructed_four_source_geometry_has_one_lossless_trunk(reverse):
    from archbro.backend.core.canvas_connection_summaries import _clear, _ink_length, _length, _simple
    raw = json.loads((Path(__file__).parent / 'fixtures/canvas_four_source_geometry.json').read_text(encoding='utf-8'))
    diagram = N(**{key: [N(**item) for item in raw['diagram'][key]] for key in ('nodes', 'edges')})
    graph = N(architecture_version=raw['graph']['architecture_version'],
              nodes=[N(**node) for node in raw['graph']['nodes']],
              edges=[N(**{**edge, 'points': [N(**point) for point in edge['points']]}) for edge in raw['graph']['edges']])
    if reverse:
        for edge in [*diagram.edges, *graph.edges]:
            edge.source, edge.target = edge.target, edge.source
        for edge in graph.edges:
            edge.points.reverse()
    before = deepcopy((diagram, graph))
    hub = 'node:persistence_postgresql_store'
    expected = {edge.id for edge in diagram.edges
                if (edge.source if reverse else edge.target) == hub and edge.relationship_category == 'DATA'}
    result = canvas_connection_summaries(diagram, graph)
    groups = [group for group in result['groups'] if group['hub_node_id'] == hub]
    assert len(groups) == 1
    group = groups[0]
    assert set(group['member_edge_ids']) == expected
    assert len(expected) == 5 and len(group['paths']) == 4
    assert group['direction'] == ('OUT' if reverse else 'IN')
    shared = [(p['x'], p['y']) for p in group['shared_path']]
    assert _length(shared) >= 400, 'The four sources must converge before the long descent, not just at the arrowhead'
    boxes = [(n.x, n.y, n.x+n.width, n.y+n.height) for n in graph.nodes]
    for path in group['paths']:
        points = [(p['x'], p['y']) for p in path]
        assert _simple(points)
        assert _clear(points, boxes, []), 'Summary must not cut through a component or header'
    original_routes = {edge.edge_id: [(p.x, p.y) for p in edge.points] for edge in graph.edges}
    for edge_id, path in group['member_paths'].items():
        member = [(p['x'], p['y']) for p in path]
        original = original_routes[edge_id]
        assert _length(member) <= _length(original)*1.25+24+1e-6
        assert member[-1 if reverse else 0] == original[-1 if reverse else 0]
    representatives = {}
    for edge in diagram.edges:
        if edge.id in expected:
            peer = edge.target if reverse else edge.source
            representatives[peer] = max(representatives.get(peer, 0), _length(original_routes[edge.id]))
    assert _ink_length([[(p['x'],p['y']) for p in path] for path in group['paths']]) <= sum(representatives.values())*0.8
    assert (diagram, graph) == before
    diagram.nodes.reverse(); diagram.edges.reverse(); graph.nodes.reverse(); graph.edges.reverse()
    assert canvas_connection_summaries(diagram, graph) == result


def fixture(reverse=False):
    nodes=[N(id='orders',parent_id=None),N(id='events',parent_id=None),N(id='a',parent_id='orders'),N(id='b',parent_id='orders'),N(id='hub',parent_id='events')]
    edges=[N(id='a-hub',source='a',target='hub',relationship_category='EVENT'),N(id='b-hub',source='b',target='hub',relationship_category='EVENT')]
    points=[[(0,0),(100,0),(100,100)],[(0,50),(50,50),(50,0),(80,0),(80,100),(100,100)]]
    if reverse:
        for edge in edges:
            edge.source,edge.target=edge.target,edge.source
        points=[list(reversed(path)) for path in points]
    routes=[N(edge_id=edge.id,source=edge.source,target=edge.target,points=[N(x=x,y=y) for x,y in path]) for edge,path in zip(edges,points)]
    return N(nodes=nodes,edges=edges),N(architecture_version=3,edges=routes)


@pytest.mark.parametrize('reverse',[False,True])
def test_summary_retains_both_directed_members_and_uses_existing_segments(reverse):
    diagram,graph=fixture(reverse)
    before=deepcopy((diagram,graph))
    result=canvas_connection_summaries(diagram,graph)
    assert result['schema']=='archbro.connection-summaries.v1'
    assert len(result['groups'])==1
    group=result['groups'][0]
    assert group['direction']==('OUT' if reverse else 'IN')
    assert group['member_edge_ids']==['a-hub','b-hub']
    assert group['domain_node_id']=='orders' and group['hub_node_id']=='hub'
    assert group['relationship_category']=='EVENT'
    assert set(group['member_paths'])=={'a-hub','b-hub'}
    assert len(group['paths'])==2
    for member_id,points in group['member_paths'].items():
        route=next(edge for edge in graph.edges if edge.edge_id==member_id)
        peer=route.points[-1] if reverse else route.points[0]
        actual_peer=points[-1] if reverse else points[0]
        assert actual_peer==dict(x=peer.x,y=peer.y)
        assert all(a['x']==b['x'] or a['y']==b['y'] for a,b in zip(points,points[1:]))
        # Every summary segment lies on an existing member segment.
        for a,b in zip(points,points[1:]):
            assert any(p.x==q.x==a['x']==b['x'] and min(p.y,q.y)<=min(a['y'],b['y'])<=max(a['y'],b['y'])<=max(p.y,q.y) or p.y==q.y==a['y']==b['y'] and min(p.x,q.x)<=min(a['x'],b['x'])<=max(a['x'],b['x'])<=max(p.x,q.x) for edge in graph.edges for p,q in zip(edge.points,edge.points[1:])),(a,b)
    assert (diagram,graph)==before
    diagram.nodes.reverse();diagram.edges.reverse();graph.edges.reverse()
    assert canvas_connection_summaries(diagram,graph)==result


def test_junctions_follow_real_divergence_after_an_overlapping_branch():
    from archbro.backend.core.canvas_connection_summaries import _branch_points
    trunk=[(0,0),(0,100)]
    branch=[(0,20),(0,70),(40,70)]
    assert _branch_points([trunk,branch])==[(0,70)]


def test_same_boundary_sibling_connections_can_share_a_trunk():
    nodes=[N(id='backend',parent_id=None),N(id='api',parent_id='backend'),N(id='context',parent_id='backend'),N(id='governance',parent_id='backend')]
    edges=[N(id='api-context',source='api',target='context',relationship_category='FLOW'),N(id='api-governance',source='api',target='governance',relationship_category='FLOW')]
    routes=[
        N(edge_id='api-context',source='api',target='context',points=[N(x=100,y=100),N(x=100,y=0),N(x=0,y=0)]),
        N(edge_id='api-governance',source='api',target='governance',points=[N(x=100,y=100),N(x=80,y=100),N(x=80,y=0),N(x=50,y=0),N(x=50,y=50),N(x=0,y=50)]),
    ]
    result=canvas_connection_summaries(N(nodes=nodes,edges=edges),N(architecture_version=1,edges=routes))
    assert len(result['groups'])==1
    assert result['groups'][0]['hub_node_id']=='api'
    assert result['groups'][0]['domain_node_id']=='backend'


@pytest.mark.parametrize('change',['boundary','category','reciprocal','no_intersection','only_terminal'])
def test_unrelated_or_ambiguous_connections_do_not_become_a_summary(change):
    diagram,graph=fixture()
    if change=='boundary':
        diagram.nodes[3].parent_id='events'
    elif change=='category':
        diagram.edges[1].relationship_category='DATA'
    elif change=='reciprocal':
        edge=deepcopy(diagram.edges[0]);edge.id='extra'
        route=deepcopy(graph.edges[0]);route.edge_id=edge.id
        edge.source,edge.target=edge.target,edge.source
        route.source,route.target=route.target,route.source
        route.points.reverse()
        diagram.edges.append(edge);graph.edges.append(route)
    elif change=='no_intersection':
        graph.edges[1].points=[N(x=0,y=50),N(x=90,y=50),N(x=90,y=100)]
    else:
        graph.edges[1].points=[N(x=0,y=50),N(x=0,y=100),N(x=100,y=100)]
    assert canvas_connection_summaries(diagram,graph)['groups']==[]


def test_summary_identity_changes_with_the_accepted_version():
    diagram,graph=fixture()
    first=canvas_connection_summaries(diagram,graph)
    graph.architecture_version+=1
    second=canvas_connection_summaries(diagram,graph)
    assert first['groups'][0]['id']!=second['groups'][0]['id']


def test_sources_join_near_their_boundary_instead_of_waiting_for_a_late_crossing():
    from archbro.backend.core.canvas_connection_summaries import _join, _length
    trunk=[(20,0),(40,0),(40,-40),(300,-40),(300,40),(400,40)]
    branch=[(120,80),(280,80),(280,-28),(380,-28),(380,40),(400,40)]
    late=_join(branch,trunk)
    boxes=[(90,60,132,112),(0,-12,32,12)]
    early=_join(branch,trunk,boxes)
    assert early[0]==(132,-40)
    assert early[1]==[(120,80),(132,80),(132,-40)]
    assert _length(early[1])<_length(late[1])
    assert early[2][0]==branch[0] and early[2][-1]==trunk[-1]
    assert _length(early[2])<=_length(branch)+24


def test_early_merge_never_cuts_through_a_card_or_shares_an_unrelated_line():
    from archbro.backend.core.canvas_connection_summaries import _join, _clear
    trunk=[(20,0),(40,0),(40,-40),(300,-40),(300,40),(400,40)]
    branch=[(120,80),(280,80),(280,-28),(380,-28),(380,40),(400,40)]
    boxes=[(90,60,132,112),(0,-12,32,12),(124,-20,244,20)]
    early=_join(branch,trunk,boxes)
    assert early[0]!=(132,-40)
    assert _clear(early[1][1:],boxes,[])
    unrelated=[[(132,-40),(132,80)]]
    assert _join(branch,trunk,boxes[:2],unrelated)[0]!=(132,-40)


def test_summary_paths_reject_hairpins_and_loops():
    from archbro.backend.core.canvas_connection_summaries import _simple
    assert not _simple([(0,0),(20,0),(10,0)])
    assert not _simple([(0,0),(20,0),(20,20),(0,20),(0,0)])
    assert _simple([(0,0),(20,0),(20,20)])


def test_early_merge_accounts_for_the_shared_hub_port_without_a_large_detour():
    from archbro.backend.core.canvas_connection_summaries import _join, _length
    trunk=[(20,0),(40,0),(40,-40),(300,-40),(300,58),(400,58)]
    branch=[(120,80),(280,80),(280,-28),(380,-28),(380,40),(400,40)]
    early=_join(branch,trunk,[(90,60,132,112),(0,-12,32,12)])
    assert early[0]==(132,-40)
    assert _length(early[2])<=_length(branch)+42


def test_representative_target_trunk_composes_parallel_actions_and_independent_hub_ports():
    nodes=[
        N(id='backend',parent_id=None),N(id='persistence',parent_id=None),
        N(id='task',parent_id='backend'),N(id='context',parent_id='backend'),
        N(id='layout',parent_id='backend'),N(id='governance',parent_id='backend'),
        N(id='store',parent_id='persistence'),
    ]
    edges=[
        N(id='task-store',source='task',target='store',relationship_category='DATA'),
        N(id='context-store',source='context',target='store',relationship_category='DATA'),
        N(id='layout-store',source='layout',target='store',relationship_category='DATA'),
        N(id='governance-read',source='governance',target='store',relationship_category='DATA'),
        N(id='governance-write',source='governance',target='store',relationship_category='DATA'),
    ]
    raw=[
        [(0,0),(40,0),(40,180),(100,180),(100,200)],
        [(0,50),(55,50),(55,165),(106,165),(106,200)],
        [(0,100),(70,100),(70,150),(112,150),(112,200)],
        [(0,150),(85,150),(85,135),(118,135),(118,200)],
        [(0,160),(90,160),(90,125),(120,125),(120,200)],
    ]
    routes=[
        N(edge_id=edge.id,source=edge.source,target=edge.target,points=[N(x=x,y=y) for x,y in points])
        for edge,points in zip(edges,raw)
    ]
    # A non-empty, remote obstacle set enables the deterministic bridge search
    # without constraining this synthetic geometry.
    graph=N(architecture_version=7,edges=routes,nodes=[N(x=1000,y=1000,width=20,height=20)])
    result=canvas_connection_summaries(N(nodes=nodes,edges=edges),graph)

    assert len(result['groups'])==1
    group=result['groups'][0]
    assert group['direction']=='IN'
    assert group['hub_node_id']=='store'
    assert group['domain_node_id']=='backend'
    assert group['member_edge_ids']==sorted(edge.id for edge in edges)
    # Four logical source representatives produce four drawable route pieces;
    # READS/WRITES remain two canonical facts inside governance's one branch.
    assert len(group['paths'])==4
    assert set(group['member_paths'])==set(group['member_edge_ids'])
