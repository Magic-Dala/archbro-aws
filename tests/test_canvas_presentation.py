import json
import re
from pathlib import Path

import pytest

from archbro.backend.core.canvas_presentation import canvas_presentation
from archbro.backend.core.canvas_reading import canvas_reading_views
from archbro.backend.core.contracts import Architecture
from archbro.backend.core.diagram import project_diagram
from archbro.backend.core.diagram_layout import layout_canvas_diagram
from qa.reference_projects import architecture

CORPUS=Path(__file__).resolve().parents[1]/'examples/reference-projects/v1'


@pytest.mark.parametrize('name',['01-commerce','02-ai-workspace','03-manufacturing'])
def test_english_is_complete_display_copy_without_rewriting_canonical_facts(name):
    sample=json.loads((CORPUS/f'{name}.json').read_text(encoding='utf-8'))
    accepted=Architecture.model_validate(architecture(sample['payload']))
    diagram=project_diagram(accepted,tasks=[],proposals=[])
    before=(accepted.model_dump_json(),diagram.model_dump_json())
    display=canvas_presentation(accepted,diagram)
    assert display['locale']=='en'
    assert set(display['nodes'])=={node.id for node in diagram.nodes}
    assert set(display['edges'])=={edge.id for edge in diagram.edges}
    assert len(display['reading_labels'])==3
    assert not re.search(r'[\u3400-\u9fff]', json.dumps(display,ensure_ascii=False))
    assert all(value['label'] and value['responsibility'] for value in display['nodes'].values())
    assert (accepted.model_dump_json(),diagram.model_dump_json())==before
    assert canvas_presentation(accepted.model_copy(update={'version':2}),diagram) is None
    renamed=accepted.model_copy(update={'summary':accepted.summary+' revised'})
    assert canvas_presentation(renamed,diagram) is None
    # All views share one geometry, including MAP subset responses.
    views=canvas_reading_views(accepted,diagram)
    reading_ids={edge for view in views if view['kind']=='AUTHORED_JOURNEY' for edge in view['edge_ids']}
    full=layout_canvas_diagram(diagram,reading_edge_ids=reading_ids)
    subset=layout_canvas_diagram(diagram,reading_edge_ids=reading_ids,route_edge_ids=views[0]['edge_ids'])
    assert full.graph.nodes==subset.graph.nodes
    routes={edge.edge_id:edge for edge in full.graph.edges}
    assert all(edge==routes[edge.edge_id] for edge in subset.graph.edges)
    reordered=diagram.model_copy(update={'nodes':list(reversed(diagram.nodes)),'edges':list(reversed(diagram.edges))})
    assert layout_canvas_diagram(reordered,reading_edge_ids=reading_ids)==full


def test_layout_rejects_invented_reading_relationship():
    with pytest.raises(ValueError,match='unknown canonical edge'):
        layout_canvas_diagram({'nodes':[{'id':'a'}],'edges':[]},reading_edge_ids=['invented'])
