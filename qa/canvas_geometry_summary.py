"""Recompute summaries from a captured, credential-free geometry fixture."""
import json
import importlib
from pathlib import Path
import sys
from types import SimpleNamespace


def main():
    if len(sys.argv) != 3:
        raise SystemExit('usage: canvas_geometry_summary.py FIXTURE SUMMARY_MODULE')
    fixture = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    diagram = json.loads(json.dumps(fixture['diagram']), object_hook=lambda row: SimpleNamespace(**row))
    graph = json.loads(json.dumps(fixture.get('graph', fixture.get('positioned_graph'))), object_hook=lambda row: SimpleNamespace(**row))
    module_path = Path(sys.argv[2]).resolve()
    src_root = next((parent for parent in module_path.parents if parent.name == 'src'), None)
    if src_root is None:
        raise SystemExit('summary module must live under the repository src/ tree')
    module_name = '.'.join(module_path.relative_to(src_root).with_suffix('').parts)
    sys.path.insert(0, str(src_root))
    module = importlib.import_module(module_name)
    print(json.dumps(module.canvas_connection_summaries(diagram, graph)))


if __name__ == '__main__':
    main()
