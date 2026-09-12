"""Regressions from ARCHBRO-GPT-FIX-HANDOFF (isolated, no live OAuth/GitHub)."""
import asyncio
import pytest

from archbro.backend.mcp.agent_tools import AgentMcpToolSession, requested_github_repositories, repository_evidence_requested
from test_agent_mcp_tools import _FakeGitHubGateway, _session
from test_project_repository_tools import _event, binding, stream


@pytest.mark.parametrize('target', ['"octocat/Hello-World"', '“octocat/Hello-World”', '「octocat/Hello-World」', '`octocat/Hello-World`', "'octocat/Hello-World'"])
def test_quoted_execution_target_denied_before_discovery(target):
    gateway = _FakeGitHubGateway()
    event = _event(f'Use GitHub MCP to read README.md from {target}.')
    assert requested_github_repositories(event) == ('octocat/Hello-World',)
    session = AgentMcpToolSession.discover(gateway, project_id='p', event=event, repository_scope=binding())
    assert session is not None and not session.has_tools
    assert session.scope_mode == 'PROJECT_REPOSITORY_MISMATCH'
    assert gateway.list_connections_calls == gateway.list_tools_calls == 0
    assert gateway.calls == []


@pytest.mark.parametrize('message', [
    'Explain the phrase "Use GitHub MCP to read README.md".',
    'Explain the phrase “Use GitHub MCP to read README.md”.',
    '請解釋「Use GitHub MCP to read README.md」這句話。',
    'Do not use GitHub. Review the repo idea using the current tasks.',
    'GitHub MCP is optional. Explain the current tasks.',
])
def test_explanations_and_opt_out_never_discover(message):
    gateway = _FakeGitHubGateway()
    event = _event(message)
    assert not repository_evidence_requested(event)
    assert AgentMcpToolSession.discover(gateway, project_id='p', event=event, repository_scope=binding()) is None
    assert gateway.list_connections_calls == gateway.list_tools_calls == 0


@pytest.mark.parametrize('message', [
    'Use GitHub MCP to inspect branch feature/login in Magic-Dala/archbro.',
    'Use GitHub MCP to read report/status in Magic-Dala/archbro.',
    'Use GitHub MCP to inspect branch "feature/login" in Magic-Dala/archbro.',
    'Use GitHub MCP to read path "report/status" from Magic-Dala/archbro.',
    'Use GitHub MCP to read docs/README.md in Magic-Dala/archbro.',
    'Use GitHub MCP to read frontend/web/app.js in Magic-Dala/archbro.',
])
def test_branch_and_path_are_not_repository_targets(message):
    assert requested_github_repositories(_event(message)) == ('Magic-Dala/archbro',)


@pytest.mark.parametrize('target', [
    'Magic-Dala/archbro', 'https://github.com/Magic-Dala/archbro',
    'https://github.com/Magic-Dala/archbro.git', 'repository Magic-Dala/archbro',
    'repo Magic-Dala/archbro', '"Magic-Dala/archbro"',
    'https://github.com/Magic-Dala/archbro/blob/dev2/README.md',
])
def test_explicit_repository_formats_remain_supported(target):
    assert requested_github_repositories(_event(f'Use GitHub MCP to inspect {target}.')) == ('Magic-Dala/archbro',)


def test_scope_mismatch_is_terminal_not_an_invitation_to_fallback():
    session, gateway = _session()
    tool = session.strands_tools()[0]
    bad = asyncio.run(stream(tool, {'owner':'octocat', 'repo':'Hello-World', 'path':'README.md'}))
    assert bad['status'] == 'error'
    fallback = asyncio.run(stream(tool, {'path':'README.md'}))
    assert fallback['status'] == 'error'
    assert gateway.calls == []
    assert session.evidence_references() == []


def test_initial_architecture_remains_github_optional():
    gateway = _FakeGitHubGateway()
    event = _event('Use GitHub MCP to read Magic-Dala/archbro.')
    event.payload['intent'] = 'INITIAL_ARCHITECTURE'
    assert AgentMcpToolSession.discover(gateway, project_id='p', event=event) is None
    assert gateway.list_connections_calls == gateway.list_tools_calls == 0
