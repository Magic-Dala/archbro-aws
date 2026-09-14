"""Validate and publish the three frozen reference projects via an existing Workbench.

No credentials, provider calls, direct database writes, or replacement of existing
architecture. A durable pending journal stops duplicate creation after an uncertain
response. Use the same registry for every invocation against the same target.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.request
import urllib.error
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urlsplit

REPO = Path(__file__).resolve().parents[1]
CORPUS = REPO / 'examples' / 'reference-projects' / 'v1'
SAMPLES = ('01-commerce', '02-ai-workspace', '03-manufacturing')
sys.path.insert(0, str(REPO / 'src'))


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def flatten(components, root=None):
    for c in components:
        owner = root or c['id']
        yield c, owner
        yield from flatten(c.get('children', []), owner)


def architecture(payload):
    from archbro.backend.core.contracts import Architecture
    return Architecture.model_validate({
        'version': 1, 'summary': payload['architecture_summary'],
        'components': payload['components'],
        'relationships': [dict(source=e['source'], target=e['target'], relationship_type=e['type'])
                          for e in payload['relationships']],
    }).model_dump(mode='json')


def validate_sample(sample):
    from archbro.backend.api.routes import InteractiveInitialArchitectureRequest
    p = sample['payload']
    a = architecture(p)
    InteractiveInitialArchitectureRequest.model_validate({
        'architecture': a, 'reasoning': p['reasoning'], 'planning_trace': p['planning_trace'],
        'tasks': [dict(title=t['title'], related_component=t.get('component')) for t in p['tasks']],
    })
    nodes = dict((c['id'], c) for c, _ in flatten(p['components']))
    roots = dict((c['id'], owner) for c, owner in flatten(p['components']))
    leaves = {key for key, c in nodes.items() if not c.get('children')}
    require(len(nodes) == sample['expected_node_count'], 'Node count drift')
    require(len(p['relationships']) == sample['expected_relationship_count'], 'Relationship count drift')
    require(sample['source_requirement'] == p['goal'], 'Original requirement differs from payload goal')
    adjacency = {key: set() for key in leaves}
    directed = set()
    unique = set()
    cross = []
    for edge in p['relationships']:
        s, t, label = edge['source'], edge['target'], edge['type']
        require(s in leaves and t in leaves and s != t, f'Non-leaf or self relationship: {edge}')
        require(label != 'DEPENDS_ON' and ' · ' in label, f'Action/protocol label missing: {edge}')
        require((s, t, label) not in unique, f'Duplicate relationship: {edge}')
        unique.add((s, t, label))
        directed.add((s, t))
        adjacency[s].add(t)
        adjacency[t].add(s)
        if roots[s] != roots[t]:
            cross.append(edge)
    visited = set()
    queue = deque([min(leaves)])
    while queue:
        current = queue.popleft()
        if current not in visited:
            visited.add(current)
            queue.extend(adjacency[current] - visited)
    require(visited == leaves, f'Disconnected modules: {sorted(leaves - visited)}')
    require(len(sample['scenarios']) == 3 and len(p['tasks']) == 3, 'Exactly three fixed scenarios required')
    for scenario in sample['scenarios']:
        path = scenario['path']
        require(scenario['focus'] in leaves, 'Scenario focus must be a canonical leaf')
        require(2 <= len(path) <= 9, 'Scenario exceeds bounded live trace length')
        require(all((s, t) in directed for s, t in zip(path, path[1:])), f'Broken workflow: {path}')
    return dict(sample_id=sample['sample_id'], definition_sha256=digest(sample), payload_sha256=digest(p),
                architecture_sha256=digest(a), roots=len(p['components']), nodes=len(nodes), leaves=len(leaves),
                relationships=len(p['relationships']), cross_root_relationships=len(cross),
                weakly_connected=True, directed_scenarios=3)


class Workbench:
    def __init__(self, base, target, evidence):
        parts = urlsplit(base)
        require(parts.hostname in ('127.0.0.1', 'localhost') and parts.scheme == 'http',
                'Workbench API must be an existing local loopback service')
        self.base, self.target, self.evidence = base.rstrip('/'), target.rstrip('/'), Path(evidence)

    def request(self, path, body=None):
        request = urllib.request.Request(self.base + path, data=None if body is None else canonical(body),
                                         headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=50) as response:
            return json.load(response)

    def ready(self, project=None):
        deadline = time.monotonic() + 30
        while True:
            state = self.request('/v1/browser/state')
            query = urlsplit(state.get('url', '')).query
            correct_project = project is None or f'project={project}' in query
            if state.get('origin') == self.target and correct_project and state.get('webmcp', {}).get('status') == 'available':
                return state
            if time.monotonic() > deadline:
                raise RuntimeError('Existing Workbench tab is not ready at the requested target; no auth fallback used')
            time.sleep(0.5)

    def tool(self, name, arguments, stem):
        started = datetime.now(timezone.utc).isoformat()
        try:
            response = self.request(f'/v1/browser/tools/{name}/execute', {'arguments': arguments})
        except Exception as exc:
            denial = exc.read().decode('utf-8', errors='replace') if isinstance(exc, urllib.error.HTTPError) else None
            write(self.evidence / f'{stem}.error.json', dict(started_at=started, tool=name,
                  error_type=type(exc).__name__, error=str(exc), response_body=denial, status='BLOCKED_OR_ERROR'))
            raise
        result = response.get('result')
        if isinstance(result, str):
            result = json.loads(result)
        # Store only this business tool result; browser storage and credentials are never read.
        write(self.evidence / f'{stem}.json', dict(started_at=started, tool=name, arguments=arguments,
                                                 result=result, is_error=response.get('isError', False)))
        require(not response.get('isError') and isinstance(result, dict), f'Tool failed: {name}')
        return result

    def select(self, project):
        self.request('/v1/browser/navigate', {'url': self.target + '/?' + urlencode({'project': project, 'reference': 'v1'})})
        self.ready(project)


def verify(wb, sample, project, expected):
    key = sample['sample_id']
    wb.select(project)
    decision = wb.tool('archbro_get_architecture_decision_context', {}, f'{key}-decision')
    require(decision['project_brief']['project']['id'] == project, 'Wrong project selected')
    require(decision['project_brief']['project']['name'] == sample['payload']['name'], 'Project name drift')
    require(digest(decision['architecture']) == expected['architecture_sha256'], 'Saved architecture differs from frozen definition')
    require(not decision['pending_reviews'], 'Reference project has pending architecture changes')
    actual_tasks = sorted((t['title'], t['related_component'], t['status']) for t in decision['tasks'])
    fixed_tasks = sorted((t['title'], t['component'], 'TODO') for t in sample['payload']['tasks'])
    require(actual_tasks == fixed_tasks, 'Reference task baseline drift')
    diagram = wb.tool('archbro_get_architecture_diagram', {'expected_architecture_version': 1}, f'{key}-root-diagram')
    graph = diagram['diagram']
    require(len(graph['nodes']) == expected['roots'] and graph['edges'], 'Missing root projection or cross-system relationships')
    outcomes = []
    for i, scenario in enumerate(sample['scenarios'], 1):
        args = dict(source_id='node:' + scenario['path'][0], target_id='node:' + scenario['path'][-1],
                    max_hops=8, expected_architecture_version=1)
        trace = wb.tool('archbro_find_architecture_path', args, f'{key}-scenario-{i}-path')
        require(trace.get('status') == 'FOUND', f'Live scenario path not found: {scenario["name"]}')
        context = wb.tool('archbro_get_architecture_node_context', dict(node_id='node:' + scenario['focus'],
                         direction='both', max_hops=1, max_results=40, expected_architecture_version=1), f'{key}-scenario-{i}-context')
        require(context.get('architecture_version') == 1, 'Node context version mismatch')
        outcomes.append(dict(name=scenario['name'], focus=scenario['focus'], status='PASS', path_status=trace['status']))
    report = dict(status='PASS', project_id=project, architecture_version=1, **expected,
                  observed_architecture_sha256=digest(decision['architecture']), scenarios=outcomes,
                  root_projected_edges=len(graph['edges']), retained=True,
                  project_url=wb.target + '/?' + urlencode({'project': project}),
                  canvas_url=wb.target + '/?' + urlencode({'canvas': 'architecture', 'project': project, 'reference': 'v1'}))
    write(wb.evidence / f'{key}-verification.json', report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('validate', 'publish', 'verify'))
    parser.add_argument('--write-lock', action='store_true', help='Explicitly freeze this corpus before its first publication')
    parser.add_argument('--sample', choices=SAMPLES)
    parser.add_argument('--workbench', default='http://127.0.0.1:5180/zenu-app')
    parser.add_argument('--target', default='https://archbro-jim.magicdala.com')
    parser.add_argument('--registry', type=Path, default=REPO / '.demo/reference-projects/registry.json')
    parser.add_argument('--evidence', type=Path)
    args = parser.parse_args()
    definitions = {key: read(CORPUS / f'{key}.json') for key in SAMPLES}
    validated = {key: validate_sample(value) for key, value in definitions.items()}
    lock_path = CORPUS / 'corpus.lock.json'
    lock = dict(schema='archbro.reference-corpus-lock.v1', corpus_version=1, samples=validated)
    if args.write_lock:
        require(args.command == 'validate' and not lock_path.exists(), 'Lock creation is only allowed once before publication; use a new corpus version for changes')
        write(lock_path, lock)
    require(read(lock_path) == lock, 'Frozen reference corpus drift; create a new explicit corpus version')
    if args.command == 'validate':
        print(json.dumps(dict(status='PASS', samples=list(validated.values())), ensure_ascii=False, indent=2))
        return
    require(args.evidence is not None, '--evidence directory required')
    args.evidence.mkdir(parents=True, exist_ok=True)
    # A second process may read the registry, but cannot publish/verify concurrently.
    args.registry.parent.mkdir(parents=True, exist_ok=True)
    guard = args.registry.with_suffix('.lock')
    with guard.open('x', encoding='utf-8') as guard_file:
        guard_file.write(datetime.now(timezone.utc).isoformat())
    try:
        wb = Workbench(args.workbench, args.target, args.evidence)
        state = wb.ready()
        write(args.evidence / 'workbench-capability.json', dict(origin=state['origin'], url=state['url'],
              tab_id=state.get('tabId'), document_id=state.get('documentId'), tool_count=state['webmcp']['toolCount'],
              existing_profile=True, credential_export=False))
        tools = {t['name']: t for t in state['webmcp']['tools']}
        import jsonschema
        for definition in definitions.values():
            jsonschema.validate(definition['payload'], tools['archbro_bootstrap_project']['inputSchema'])
        registry = read(args.registry) if args.registry.exists() else dict(schema='archbro.reference-registry.v1', target_origin=wb.target, samples={})
        require(registry['target_origin'] == wb.target, 'Registry belongs to another target')
        reports = []
        for key in ([args.sample] if args.sample else SAMPLES):
            sample, expected = definitions[key], validated[key]
            entry = registry['samples'].get(key)
            if entry:
                require(entry['payload_sha256'] == expected['payload_sha256'], 'Registry payload identity drift')
                require(entry.get('project_id') and entry['status'] != 'PUBLISHING', 'Uncertain previous publication: reconcile its saved result before retrying; never create a duplicate')
                project = entry['project_id']
            else:
                require(args.command == 'publish', f'No owned reference project registered for {key}')
                entry = dict(payload_sha256=expected['payload_sha256'], status='PUBLISHING', project_id=None,
                             name=sample['payload']['name'], created_at=datetime.now(timezone.utc).isoformat())
                registry['samples'][key] = entry
                write(args.registry, registry)
                result = wb.tool('archbro_bootstrap_project', sample['payload'], f'{key}-bootstrap')
                project = result['project']['id']
                entry.update(project_id=project, status='CREATED', owner_user_id=result['project'].get('owner_user_id'))
                write(args.registry, registry)
                require(result.get('built_in_model_called') is False, 'Unexpected model dispatch during fixed publication')
            report = verify(wb, sample, project, expected)
            reports.append(report)
            entry.update(status='VERIFIED', architecture_sha256=report['observed_architecture_sha256'],
                         project_url=report['project_url'], canvas_url=report['canvas_url'])
            write(args.registry, registry)
            print(json.dumps(report, ensure_ascii=False), flush=True)
        completed = {report['sample_id']: report for report in reports}
        for key in SAMPLES:
            previous = args.evidence / f'{key}-verification.json'
            if key not in completed and previous.exists():
                report = read(previous)
                entry = registry['samples'].get(key, {})
                if (report.get('project_id') == entry.get('project_id')
                        and report.get('architecture_sha256') == validated[key]['architecture_sha256']
                        and entry.get('status') == 'VERIFIED'):
                    completed[key] = report
        write(args.evidence / 'current.json', dict(status='PASS' if len(completed) == 3 else 'PARTIAL',
              reports=[completed[key] for key in SAMPLES if key in completed], registry=str(args.registry)))
    finally:
        guard.unlink()


if __name__ == '__main__':
    main()
