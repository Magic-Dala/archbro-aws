from pathlib import Path
import sys, json, time, argparse
from dataclasses import asdict

parser = argparse.ArgumentParser(description="Render the frozen corpus with the local backend; no DB, auth, or provider calls.")
parser.add_argument('--output', type=Path, default=Path(__file__).resolve().parent/'playwright_artifacts/canvas-readability')
args = parser.parse_args()
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'src'), str(ROOT/'qa')]
from archbro.backend.core.diagram import project_diagram
from archbro.backend.core.diagram_layout import layout_canvas_diagram
from archbro.backend.core.canvas_reading import canvas_reading_views
from archbro.backend.core.canvas_connection_summaries import canvas_connection_summaries
from archbro.backend.core.canvas_presentation import canvas_presentation
from archbro.backend.core.contracts import Architecture
from reference_projects import architecture, digest

OUT = args.output
OUT.mkdir(parents=True, exist_ok=True)
results=[]
for p in sorted((ROOT/'examples/reference-projects/v1').glob('0*.json')):
    sample = json.loads(p.read_text(encoding='utf-8'))
    accepted = Architecture.model_validate(architecture(sample['payload']))
    before = digest(accepted.model_dump(mode='json'))
    diagram = project_diagram(accepted, tasks=[], proposals=[])
    reading_ids={edge_id for view in canvas_reading_views(accepted, diagram) if view["kind"]=="AUTHORED_JOURNEY" for edge_id in view["edge_ids"]}
    start = time.perf_counter()
    layout = layout_canvas_diagram(diagram, reading_edge_ids=reading_ids)
    elapsed = time.perf_counter()-start
    repeat = layout_canvas_diagram(diagram.model_copy(update={'nodes':list(reversed(diagram.nodes)), 'edges':list(reversed(diagram.edges))}), reading_edge_ids=reading_ids)
    assert asdict(layout) == asdict(repeat), 'Input order changed geometry'
    nodes = {n.node_id:n for n in layout.graph.nodes}
    assert {e.edge_id for e in layout.graph.edges} == {e.id for e in diagram.edges}
    hits=[]
    for edge in layout.graph.edges:
        for a,b in zip(edge.points, edge.points[1:]):
            assert a.x==b.x or a.y==b.y, ('diagonal', edge.edge_id)
            for node in nodes.values():
                hit = (node.x < a.x < node.x+node.width and max(min(a.y,b.y),node.y)<min(max(a.y,b.y),node.y+node.height)) if a.x==b.x else (node.y<a.y<node.y+node.height and max(min(a.x,b.x),node.x)<min(max(a.x,b.x),node.x+node.width))
                if hit: hits.append((edge.edge_id, node.node_id))
        for point, node_id in [(edge.points[0],edge.source),(edge.points[-1],edge.target)]:
            n=nodes[node_id]
            assert ((point.x in (n.x,n.x+n.width) and n.y<=point.y<=n.y+n.height) or (point.y in (n.y,n.y+n.height) and n.x<=point.x<=n.x+n.width)), ('bad port',edge.edge_id,node_id)
    assert not hits, hits[:10]
    views = canvas_reading_views(accepted, diagram)
    assert len(views)==4
    assert before == digest(accepted.model_dump(mode='json'))
    body = dict(schema='archbro.full_canvas.v1', project_id=sample['sample_id'], architecture_version=1, diagram=diagram.model_dump(mode='json'), positioned_graph=asdict(layout.graph), group_frames=[asdict(f) for f in layout.group_frames], reading_views=views, connection_summaries=canvas_connection_summaries(diagram,layout.graph), presentation=canvas_presentation(accepted, diagram))
    (OUT/f'{sample["sample_id"]}-canvas.json').write_text(json.dumps(dict(sample=sample, architecture=accepted.model_dump(mode='json'), canvas=body), ensure_ascii=False, indent=2), encoding='utf-8')
    result=dict(sample=sample['sample_id'], nodes=len(nodes), edges=len(layout.graph.edges), overview_edges=len(views[0]['edge_ids']), named_journeys=len(views)-1, seconds=round(elapsed,3), width=layout.graph.width,height=layout.graph.height, node_collisions=len(hits),deterministic=True, architecture_sha256=before)
    print(json.dumps(result),flush=True)
    results.append(result)
(OUT/'geometry.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
