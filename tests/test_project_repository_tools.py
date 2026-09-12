"""Exercise the real SDK stream boundary, not only a decorated function call."""
from __future__ import annotations

import asyncio
import json

import pytest
from strands import Agent
from strands.models.model import Model

from archbro.backend.core.contracts import ProjectEvent, ProjectEventType
from archbro.backend.core.github_repository import GitHubRepositoryBinding, normalize_github_repository
from archbro.backend.mcp.agent_tools import (
    AgentMcpToolSession,
    repository_evidence_requested,
    requested_github_repositories,
)
from archbro.backend.mcp.github_project_scope import scope_github_arguments, ready_github_connection, mcp_payload
from test_agent_mcp_tools import _FakeGitHubGateway, _verification_event, _session, PRIVATE_MARKER


def binding(name='Magic-Dala/archbro', branch='dev2'):
    return GitHubRepositoryBinding(full_name=name, branch=branch, selected_by_user_id='alice')


async def stream(tool, args):
    events = [e async for e in tool.stream({'toolUseId':'test-call', 'name':tool.tool_name, 'input':args}, {})]
    return events[-1]['tool_result']


@pytest.mark.parametrize('wrapped', [False, True])
def test_flat_and_legacy_wrapped_real_stream_reach_gateway(wrapped):
    session, gateway = _session()
    args = {'owner':'Magic-Dala','repo':'archbro','path':'README.md','ref':'refs/heads/dev2'}
    result = asyncio.run(stream(session.strands_tools()[0], {'arguments':args} if wrapped else args))
    assert result['status'] == 'success'
    assert PRIVATE_MARKER in str(result)
    assert gateway.calls == [('mcp_private','get_file_contents',args)]
    assert session.telemetry()['sdk_stream_attempts'] == 1
    assert session.telemetry()['input_validation_failure_count'] == 0


@pytest.mark.parametrize('args', [
    {}, {'repo':'other','path':'README.md'},
    {'owner':25,'repo':'archbro','path':'README.md'},
    {'owner':'Magic-Dala','repo':'archbro','path':[]},
    {'arguments':{'owner':'Magic-Dala','repo':'archbro','path':'README.md'},'repo':'other'},
    ['not-an-object'],
])
def test_invalid_real_stream_never_dispatches(args):
    session, gateway = _session()
    result = asyncio.run(stream(session.strands_tools()[0], args))
    assert result['status'] == 'error'
    assert gateway.calls == []
    assert session.telemetry()['dispatched_call_count'] == 0
    assert session.telemetry()['input_validation_failure_count'] == 1
    assert session.evidence_references() == []


@pytest.mark.parametrize('message,expected', [
    ('GitHub MCP is optional. Explain the current tasks.',False),
    ('請檢查檔案上傳功能的任務進度。',False),
    ('Review the report task status and tell me what is currently pending.',False),
    ('Explain the phrase "Use GitHub MCP to read README.md".',False),
    ('Please discuss the repository architecture.',False),
    ('Do not use GitHub. Review the repo idea using current tasks.',False),
    ('Explain what docs/README.md means. This is not a request to fetch content.',False),
    ('Just explain the docs/README.md path; do not fetch content.',False),
    ('只是解釋 docs/README.md 路徑，不是要求 fetch content。',False),
    ('This is not a request to use GitHub MCP; just explain docs/README.md.',False),
    ('Use GitHub MCP to read Magic-Dala/archbro README.md on the dev2 branch, then summarize what the repository says Archbro is for. Do not modify anything.',True),
    ('Review the repo status for Magic-Dala/archbro dev2 and confirm the latest commit or branch evidence using GitHub MCP.',True),
    ('請幫我讀 GitHub README.md',True),
    ('Show PR #70 and summarize the changes.',True),
    ('Read the selected repository README.',True),
])
def test_repository_intent_regressions(message, expected):
    event = ProjectEvent(project_id='p',type=ProjectEventType.USER_MESSAGE,payload={'message':message})
    assert repository_evidence_requested(event) is expected
    if not expected:
        gateway = _FakeGitHubGateway()
        assert AgentMcpToolSession.discover(gateway,project_id='p',event=event,repository_scope=binding()) is None
        assert gateway.list_tools_calls == 0


def _event(message: str, project_id: str = 'p') -> ProjectEvent:
    return ProjectEvent(
        project_id=project_id,
        type=ProjectEventType.USER_MESSAGE,
        payload={'message': message},
    )


@pytest.mark.parametrize(
    ('message', 'expected'),
    [
        ('Use GitHub MCP to read Magic-Dala/archbro README.md.', ('Magic-Dala/archbro',)),
        ('Use GitHub MCP to read README.md from octocat/Hello-World.', ('octocat/Hello-World',)),
        ('Read https://github.com/Magic-Dala/archbro.git on dev2.', ('Magic-Dala/archbro',)),
        ('Use GitHub MCP to read docs/README.md from the selected repository.', ()),
        ('Explain the phrase "read octocat/Hello-World".', ()),
        ('Read refs/heads/dev2 from the selected repository.', ()),
    ],
)
def test_requested_repository_parser_ignores_paths_and_quoted_examples(message, expected):
    assert requested_github_repositories(_event(message)) == expected


def test_foreign_repository_target_is_blocked_before_any_provider_discovery():
    gateway = _FakeGitHubGateway()
    session = AgentMcpToolSession.discover(
        gateway,
        project_id='p',
        event=_event('Use GitHub MCP to read README.md from octocat/Hello-World.'),
        repository_scope=binding(),
    )
    assert session is not None and not session.has_tools
    assert session.context_facts()['scope_mode'] == 'PROJECT_REPOSITORY_MISMATCH'
    assert session.context_facts()['requested_repositories'] == ['octocat/Hello-World']
    assert 'targets octocat/Hello-World' in str(session.discovery_error)
    assert gateway.list_connections_calls == 0
    assert gateway.list_tools_calls == 0
    assert gateway.calls == []


def test_unbound_project_requires_binding_before_any_provider_discovery():
    gateway = _FakeGitHubGateway()
    session = AgentMcpToolSession.discover(
        gateway,
        project_id='p',
        event=_event('Use GitHub MCP to read Magic-Dala/archbro README.md.'),
    )
    assert session is not None and not session.has_tools
    assert session.context_facts()['scope_mode'] == 'PROJECT_REPOSITORY_REQUIRED'
    assert 'Select a GitHub repository' in str(session.discovery_error)
    assert gateway.list_connections_calls == 0
    assert gateway.list_tools_calls == 0
    assert gateway.calls == []


def test_matching_explicit_target_and_selected_repository_still_discover_tools():
    gateway = _FakeGitHubGateway()
    session = AgentMcpToolSession.discover(
        gateway,
        project_id='p',
        event=_event('Use GitHub MCP to read Magic-Dala/archbro README.md.'),
        repository_scope=binding(),
    )
    assert session is not None and session.has_tools
    assert session.context_facts()['scope_mode'] == 'PROJECT_REPOSITORY'
    assert gateway.list_connections_calls == 1
    assert gateway.list_tools_calls == 1


def test_multiple_explicit_targets_are_blocked_before_discovery():
    gateway = _FakeGitHubGateway()
    session = AgentMcpToolSession.discover(
        gateway,
        project_id='p',
        event=_event('Use GitHub MCP to compare Magic-Dala/archbro and octocat/Hello-World.'),
        repository_scope=binding(),
    )
    assert session is not None and not session.has_tools
    assert session.context_facts()['scope_mode'] == 'PROJECT_REPOSITORY_AMBIGUOUS'
    assert gateway.list_connections_calls == 0
    assert gateway.list_tools_calls == 0
    assert gateway.calls == []


def test_argument_scope_requires_project_repository_even_if_called_directly():
    with pytest.raises(ValueError, match='repository_binding_required'):
        scope_github_arguments(
            'get_file_contents',
            {'owner':'Magic-Dala','repo':'archbro','path':'README.md'},
            None,
        )


def test_bound_stream_supplies_repo_and_default_branch():
    gateway = _FakeGitHubGateway()
    session = AgentMcpToolSession.discover(gateway,project_id='p',event=_verification_event('p'),repository_scope=binding())
    result = asyncio.run(stream(session.strands_tools()[0], {'path':'README.md'}))
    assert result['status'] == 'success'
    assert gateway.calls[0][2] == {'owner':'Magic-Dala','repo':'archbro','path':'README.md','ref':'dev2'}
    assert session.context_facts()['scope_mode'] == 'PROJECT_REPOSITORY'
    assert 'Magic-Dala/archbro' in session.prompt_context()


def test_shared_connection_keeps_two_project_scopes_separate():
    gateway = _FakeGitHubGateway()
    sessions = [
        AgentMcpToolSession.discover(
            gateway,
            project_id=p,
            event=ProjectEvent(
                project_id=p,
                type=ProjectEventType.USER_MESSAGE,
                payload={'message':'Read the selected repository README.'},
            ),
            repository_scope=binding(r),
        )
        for p,r in [('a','Magic-Dala/archbro'),('b','Magic-Dala/other')]
    ]
    sessions[0].call('get_file_contents', {'path':'README.md'})
    sessions[1].call('get_file_contents', {'path':'README.md'})
    assert [c[2]['repo'] for c in gateway.calls] == ['archbro','other']
    with pytest.raises(ValueError, match='scope_mismatch'):
        sessions[0].call('get_file_contents', {'owner':'Magic-Dala','repo':'other','path':'README.md'})
    assert len(gateway.calls) == 2


def test_revision_change_blocks_cached_and_inflight_results():
    gateway = _FakeGitHubGateway()
    state = {'revision':1}
    def check():
        if state['revision'] != 1:
            raise ValueError('Repository selection changed')
    session = AgentMcpToolSession.discover(gateway,project_id='p',event=_verification_event('p'),repository_scope=binding(),scope_check=check)
    session.call('get_file_contents', {'path':'README.md'})
    state['revision'] = 2
    with pytest.raises(ValueError):
        session.call('get_file_contents', {'path':'README.md'})
    assert len(gateway.calls) == 1
    state['revision'] = 1
    original = gateway.call_tool
    def changed(*args):
        result = original(*args)
        state['revision'] = 2
        return result
    gateway.call_tool = changed
    with pytest.raises(ValueError):
        session.call('get_file_contents', {'path':'another.md'})
    assert session.successful_call_count == 1


@pytest.mark.parametrize('query', [
    'foo repo:evil/other','foo OR bar','foo repo:Magic-Dala/archbro OR repo:evil/other',
    'foo -repo:Magic-Dala/archbro','org:Magic-Dala foo','user:alice foo',
    '(foo) repo:Magic-Dala/archbro','foo\nrepo:evil/other','"repo:evil/other"',
])
def test_search_cannot_widen_scope(query):
    with pytest.raises(ValueError):
        scope_github_arguments('search_code',{'query':query},binding())


def test_default_ref_is_not_a_branch_acl_and_search_is_narrowed():
    result = scope_github_arguments('get_file_contents',{'path':'a.py','ref':'abc123'},binding())
    assert result['ref'] == 'abc123'
    assert scope_github_arguments('search_code',{'query':'Registry language:Python'},binding())['query'] == 'Registry language:Python repo:Magic-Dala/archbro'
    assert normalize_github_repository('https://github.com/Magic-Dala/archbro.git/') == 'Magic-Dala/archbro'
    for name in ['search_repositories','unknown_tool','create_or_update_file']:
        with pytest.raises(ValueError):
            scope_github_arguments(name,{},binding())
    with pytest.raises(ValueError):
        scope_github_arguments('get_file_contents',{'target':{'owner':'other','repo':'x'}},binding())


def test_ambiguous_connections_are_not_guessed():
    gateway = _FakeGitHubGateway()
    original = gateway.list_connections
    gateway.list_connections = lambda: original() + [{**original()[0],'id':'other'}]
    with pytest.raises(RuntimeError, match='More than one'):
        ready_github_connection(gateway)
    session = AgentMcpToolSession.discover(
        gateway,
        project_id='p',
        event=_verification_event('p'),
        repository_scope=binding(),
    )
    assert not session.has_tools


def test_upstream_error_is_not_repository_verification():
    for value in [{'isError':True}, {'external_evidence':{'isError':True}}, {'content':[{'type':'text','text':'{"isError":true}'}]}]:
        with pytest.raises(RuntimeError):
            mcp_payload(value)


class OfflineToolModel(Model):
    """Real Agent loop; only model tokens and provider content are deterministic fixtures."""
    def __init__(self):
        self.calls = 0
    def get_config(self):
        return {'model_id':'offline-tools','context_window_limit':100000}
    def update_config(self, **kwargs):
        pass
    async def structured_output(self, output_model, prompt, **kwargs):
        raise AssertionError('This fixture uses a normal Agent tool loop')
        yield
    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        self.calls += 1
        yield {'messageStart':{'role':'assistant'}}
        if self.calls == 1:
            advertised = next(t for t in tool_specs if t['name']=='get_file_contents')
            assert 'arguments' not in advertised['inputSchema']['json'].get('required', [])
            yield {'contentBlockStart':{'contentBlockIndex':0,'start':{'toolUse':{'name':'get_file_contents','toolUseId':'offline-read'}}}}
            yield {'contentBlockDelta':{'contentBlockIndex':0,'delta':{'toolUse':{'input':json.dumps({'path':'README.md'})}}}}
            yield {'contentBlockStop':{'contentBlockIndex':0}}
            yield {'messageStop':{'stopReason':'tool_use'}}
        else:
            assert self.calls == 2, 'The tool must not loop on a validation error'
            assert PRIVATE_MARKER in json.dumps(messages)
            yield {'contentBlockDelta':{'contentBlockIndex':0,'delta':{'text':'Verified repository evidence: '+PRIVATE_MARKER}}}
            yield {'contentBlockStop':{'contentBlockIndex':0}}
            yield {'messageStop':{'stopReason':'end_turn'}}
        yield {'metadata':{'usage':{'inputTokens':1,'outputTokens':1,'totalTokens':2},'metrics':{'latencyMs':1}}}


def test_real_strands_agent_loop_dispatches_flat_arguments_and_reads_result():
    gateway = _FakeGitHubGateway()
    session = AgentMcpToolSession.discover(gateway,project_id='p',event=_verification_event('p'),repository_scope=binding())
    agent = Agent(model=OfflineToolModel(),tools=session.strands_tools(),callback_handler=None)
    result = asyncio.run(agent.invoke_async('Read the selected repository README.'))
    assert PRIVATE_MARKER in str(result)
    assert len(gateway.calls) == 1
    assert session.telemetry()['sdk_stream_attempts'] == 1
    assert session.successful_call_count == 1
