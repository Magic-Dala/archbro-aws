"""Repository narrowing, shared by internal and browser-facing GitHub tools."""
from __future__ import annotations

import copy
import json
import re
from typing import Any

from archbro.backend.core.github_repository import GitHubRepositoryBinding, normalize_github_repository

REPOSITORY_TOOLS = frozenset({
    'get_file_contents', 'list_branches', 'get_commit', 'list_commits',
    'pull_request_read', 'list_pull_requests', 'issue_read', 'list_issues',
    'list_tags', 'get_tag', 'search_code',
})
_READ_METHODS = {
    'pull_request_read': {'get', 'get_diff', 'get_files', 'get_comments', 'get_reviews', 'get_review_comments', 'get_status', 'is_merged', 'get_check_runs'},
    'issue_read': {'get', 'get_comments', 'get_sub_issues', 'get_labels'},
}
_TARGET_KEYS = {'owner', 'repo', 'repository', 'repositories', 'full_name', 'repo_full_name', 'repository_id', 'url'}


def ready_github_connection(gateway: Any) -> dict[str, Any]:
    choices = [c for c in gateway.list_connections()
               if c.get('provider') == 'github' and not c.get('authorization_pending')
               and c.get('last_probe_ok') is not False and c.get('id')]
    if not choices:
        raise RuntimeError('Connect or reconnect your GitHub account first.')
    if len(choices) != 1:
        raise RuntimeError('More than one GitHub connection is ready. Keep the intended account connected before selecting a repository.')
    return choices[0]


def scoped_tool_schema(tool: dict[str, Any], binding: GitHubRepositoryBinding | None) -> dict[str, Any]:
    result = copy.deepcopy(tool)
    if not binding:
        return result
    schema = result.get('inputSchema', {})
    # Defaults are supplied by the server, not by a schema validator or model.
    schema['required'] = [k for k in schema.get('required', []) if k not in {'owner', 'repo'}]
    for key, value in [('owner', binding.owner), ('repo', binding.repo)]:
        if key in schema.get('properties', {}):
            schema['properties'][key]['description'] = 'Optional; must match the project repository. The server fills this value.'
            schema['properties'][key]['default'] = value
    result['description'] = str(result.get('description', ''))[:900] + ' Project repository: ' + binding.full_name
    return result


def scope_github_arguments(tool_name: str, arguments: dict[str, Any], binding: GitHubRepositoryBinding | None) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise ValueError('GitHub MCP arguments must be an object')
    args = copy.deepcopy(arguments)
    if binding is None:
        return args
    if tool_name not in REPOSITORY_TOOLS:
        raise ValueError('This tool is not available inside a repository-bound project. Use the project repository picker to change repositories.')
    def reject_nested_targets(value: Any, depth: int = 0) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if str(key).casefold() in _TARGET_KEYS and (depth > 0 or key not in {'owner', 'repo'}):
                    raise ValueError('Ambiguous or nested repository selector is not allowed')
                reject_nested_targets(child, depth + 1)
        elif isinstance(value, list):
            for child in value:
                reject_nested_targets(child, depth + 1)
    reject_nested_targets(args)
    for key, expected in [('owner', binding.owner), ('repo', binding.repo)]:
        if key in args and (not isinstance(args[key], str) or args[key].strip().casefold() != expected.casefold()):
            raise ValueError('repository_scope_mismatch: tool arguments name a different repository')
    if tool_name == 'search_code':
        if 'q' in args:
            raise ValueError('Use query for repository-scoped code search')
        query = args.get('query')
        if not isinstance(query, str) or not query.strip() or len(query) > 1500:
            raise ValueError('Code search requires a bounded non-empty query')
        if re.search(r'[\x00-\x1f\x7f()|]|\b(?:OR|NOT|AND)\b', query, re.IGNORECASE):
            raise ValueError('Use a simple conjunctive code search without Boolean operators or grouping')
        qualifier = re.compile(r'(?<!\S)(-?)(repo|org|user|owner|enterprise):([^\s]+)', re.IGNORECASE)
        def narrow(match: re.Match[str]) -> str:
            if match.group(1) or match.group(2).casefold() != 'repo' or match.group(3).casefold() != binding.full_name.casefold():
                raise ValueError('repository_scope_mismatch: search qualifier reaches outside this project')
            return ''
        query = qualifier.sub(narrow, query)
        # Reject non-token or quoted resource qualifiers rather than guessing their grammar.
        if re.search(r'(?:repo|org|user|owner|enterprise)\s*:', query, re.IGNORECASE):
            raise ValueError('Ambiguous repository search qualifier')
        args.pop('owner', None)
        args.pop('repo', None)
        args['query'] = f'{query.strip()} repo:{binding.full_name}'.strip()
        return args
    args['owner'], args['repo'] = binding.owner, binding.repo
    if tool_name in _READ_METHODS and args.get('method', 'get') not in _READ_METHODS[tool_name]:
        raise ValueError('Unsupported read method for a repository-scoped tool')
    if binding.branch:
        if tool_name == 'get_file_contents' and not args.get('ref') and not args.get('sha'):
            args['ref'] = binding.branch
        if tool_name == 'list_commits' and not args.get('sha'):
            args['sha'] = binding.branch
    return args


def mcp_payload(result: Any) -> Any:
    value = result
    for _ in range(4):
        if not isinstance(value, dict):
            break
        if value.get('isError') is True:
            raise RuntimeError('GitHub could not read the requested repository or branch with your current account.')
        if 'external_evidence' in value:
            value = value['external_evidence']
            continue
        if 'structuredContent' in value:
            value = value['structuredContent']
            continue
        content = value.get('content')
        if isinstance(content, list):
            texts = [item.get('text', '') for item in content if isinstance(item, dict) and item.get('type') == 'text']
            for text in texts:
                if isinstance(text, str) and len(text) <= 1000000:
                    try:
                        value = json.loads(text)
                        break
                    except ValueError:
                        continue
            else:
                return value
            continue
        break
    if isinstance(value, dict) and (value.get('isError') is True or value.get('error')):
        raise RuntimeError('GitHub repository request failed.')
    return value


def repository_options(gateway: Any, query: str, page: int) -> dict[str, Any]:
    connection = ready_github_connection(gateway)
    result = mcp_payload(gateway.call_tool(connection['id'], 'search_repositories', {'query': query, 'page': page, 'perPage': 20}))
    items = result.get('items', result.get('repositories', [])) if isinstance(result, dict) else result
    if not isinstance(items, list):
        raise RuntimeError('GitHub returned an unreadable repository list. Enter owner/repo directly instead.')
    choices = []
    for item in items[:20]:
        if not isinstance(item, dict):
            continue
        try:
            name = normalize_github_repository(str(item.get('full_name') or item.get('fullName') or ''))
        except ValueError:
            continue
        repo_id = item.get('id')
        choices.append({'full_name': name, 'private': item.get('private') is True,
                        'repository_id': repo_id if isinstance(repo_id, int) and not isinstance(repo_id, bool) and repo_id > 0 else None})
    return {'items': choices, 'page': page, 'has_more': len(items) >= 20}


def verify_repository(gateway: Any, full_name: str, branch: str | None) -> None:
    connection = ready_github_connection(gateway)
    owner, repo = full_name.split('/', 1)
    # No README dependency. A branchless binding also supports empty repositories.
    name, args = ('get_file_contents', {'owner': owner, 'repo': repo, 'path': '/', 'ref': branch}) if branch else (
        'list_branches', {'owner': owner, 'repo': repo, 'perPage': 1, 'page': 1})
    value = mcp_payload(gateway.call_tool(connection['id'], name, args))
    if not branch:
        rows = value.get('branches') if isinstance(value, dict) else value
        if not isinstance(rows, list) or any(not isinstance(row, dict) or not isinstance(row.get('name'), str) for row in rows):
            raise RuntimeError('GitHub did not return repository branch metadata')
    elif not isinstance(value, (dict, list)) or (isinstance(value, dict) and not any(k in value for k in ('sha', 'content', 'entries', 'type'))):
        raise RuntimeError('GitHub did not return repository content metadata')
