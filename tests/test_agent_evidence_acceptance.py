"""Repository evidence through real API, orchestration, proposal acceptance and PostgreSQL.

Only the external model and GitHub transport are deterministic fixtures. No live
credentials or paid provider calls are used. The browser lane reuses this stack.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from types import MethodType

from fastapi import FastAPI
from fastapi.testclient import TestClient

from archbro.backend.api.routes import build_router
from archbro.backend.api.provider_connections import ProviderMcpRuntimeRegistry
from archbro.backend.core.authorization import TrustedPrincipal
from archbro.backend.core.contracts import Architecture, Component, Project, Relationship
from archbro.backend.llm.gemini import GeminiDecisionWire
from conftest import requires_database
from test_agent_mcp_tools import _FakeGitHubGateway, _binding, _gemini_provider, _aligned_wire

ARCHITECTURE_PROMPT = """Use GitHub MCP to inspect the bound Magic-Dala/archbro repository on the dev2 branch.

Verify whether the current Archbro Architecture matches the actual implementation.

Focus only on these major runtime boundaries:
- Web frontend and workspace UI
- Backend project / architecture / task domain
- Built-in Strands agent runtime
- Project-scoped MCP and provider integrations
- GitHub repository binding and evidence flow
- Persistence
- Deployment/runtime

For anything that does not match the current Architecture, propose the smallest architecture changes needed.

Do not modify code."""

PROGRESS_PROMPT = """Use GitHub MCP to verify the implementation progress of the bound
Magic-Dala/archbro repository on dev2 against the current accepted Architecture.
Classify Web frontend, Backend domain, Strands agent runtime, project-scoped MCP,
GitHub evidence flow, Persistence, and Deployment/runtime as VERIFIED / PARTIAL /
UNVERIFIED, with repository file evidence for each positive claim. Keep unsearched
areas UNVERIFIED. Do not modify code or create a proposal without demonstrated drift."""

EVIDENCE_TEXT = """# Archbro (private repository evidence fixture)
Archbro turns a codebase into an architecture and task workspace.
The web frontend calls a Python API. The production persistence layer is PostgreSQL;
SQLite is not used. Human approval is required for architecture changes.
PRIVATE_ACCEPTANCE_README
"""


def drift_wire():
    return GeminiDecisionWire.model_validate({
        "summary": "Persistence is PARTIAL: README.md and persistence configuration prove PostgreSQL, but the accepted map still names SQLite. Other runtime areas remain UNVERIFIED.",
        "evaluation": {
            "classification": "ARCHITECTURE_DRIFT",
            "summary": "Represent the implemented PostgreSQL persistence boundary.",
            "evidence": ["Magic-Dala/archbro@dev2 README.md and persistence configuration identify PostgreSQL."],
            "affected_components": ["database"],
            "affected_tasks": [],
            "architecture_change_required": True,
            "recommended_action": "PROPOSE_ARCHITECTURE_CHANGE",
        },
        "architecture_review_required": True,
        "architecture_proposal": {
            "reason": "The implemented persistence engine differs from the accepted architecture.",
            "evidence": ["Magic-Dala/archbro@dev2 README.md and persistence configuration identify PostgreSQL."],
            "observed_change": "The repository uses PostgreSQL rather than the currently recorded SQLite.",
            "affected_components": ["database"],
            "proposed_changes": [{
                "operation": "replace_component", "component_id": "database",
                "new_name": "PostgreSQL", "new_type": "database",
                "new_responsibility": "Persist canonical projects, architecture, tasks and agent runs.",
            }],
            "impact": "Update one persistence component after human approval.",
            "recommended_option": "ACCEPT_PROPOSED_CHANGE",
        },
        "actions": [],
    })


class EvidenceGateway(_FakeGitHubGateway):
    def call_tool(self, connection_id, tool_name, arguments):
        self.calls.append((connection_id, tool_name, dict(arguments)))
        assert arguments['owner'] == 'Magic-Dala' and arguments['repo'] == 'archbro'
        assert arguments['ref'] in ('dev2', 'refs/heads/dev2')
        assert arguments['path'] in ('README.md', 'src/persistence/config.py')
        return {"external_evidence": {"content": EVIDENCE_TEXT, "sha": "a" * 40}}


class RecordingRegistry(ProviderMcpRuntimeRegistry):
    def __init__(self, gateway):
        super().__init__()
        self.gateways['acceptance-owner'] = gateway
        self.oauth_managers['acceptance-owner'] = object()
        self.sessions = []

    def agent_tool_session(self, *args, **kwargs):
        session = super().agent_tool_session(*args, **kwargs)
        if session is not None:
            self.sessions.append(session)
        return session


@contextmanager
def evidence_acceptance_client(repo):
    project = Project(name='Evidence acceptance', goal='Keep architecture aligned with code evidence.', owner_user_id='acceptance-owner', architecture_version=1, source_repository=_binding())
    repo.save_project(project)
    repo.save_architecture(project.id, Architecture(version=1, summary='Accepted application architecture', components=[
        Component(id='web', name='Web Workspace', type='frontend', responsibility='Present the project workspace.'),
        Component(id='database', name='SQLite', type='database', responsibility='Persist project state.'),
    ], relationships=[Relationship(source='web', target='database', relationship_type='uses', description='Persists project state')]))
    gateway = EvidenceGateway()
    registry = RecordingRegistry(gateway)
    provider = _gemini_provider()
    provider.tool_interaction_model_timeout_seconds = 0.06
    provider.tool_interaction_total_timeout_seconds = 1.0
    invocations = []

    async def invoke(self, model_id, prompt, *, tools=None, evidence_completion=False):
        invocations.append((bool(tools), evidence_completion))
        if tools:
            session = registry.sessions[-1]
            for path in ('README.md', 'README.md', 'src/persistence/config.py'):
                session.call('get_file_contents', {'owner': 'Magic-Dala', 'repo': 'archbro', 'ref': 'dev2', 'path': path})
            await asyncio.sleep(2)
            raise AssertionError('tool turn must be canceled')
        assert evidence_completion and 'PRIVATE_ACCEPTANCE_README' in prompt
        assert 'A successful supplied GitHub tool call is mandatory' not in prompt
        assert len(registry.sessions[-1].completion_evidence_context()['sources']) == 2
        if 'Classify Web frontend' in prompt:
            assert repo.load_context(project.id).architecture.version == 2
            return _aligned_wire('Persistence: VERIFIED — PostgreSQL in README.md and src/persistence/config.py matches the accepted map. Other runtime areas: UNVERIFIED; bounded evidence does not prove absence.')
        return drift_wire()

    provider._invoke = MethodType(invoke, provider)

    async def principal(_token):
        return TrustedPrincipal(user_id='acceptance-owner')

    app = FastAPI()
    app.include_router(build_router(repo, provider, principal_provider=principal, provider_mcp_runtime=registry))
    with TestClient(app, base_url='http://127.0.0.1', headers={'Authorization': 'Bearer isolated-test'}) as client:
        yield client, project.id, gateway, registry, invocations


@requires_database
def test_repository_verification_acceptance_and_followup_progress(repo):
    with evidence_acceptance_client(repo) as (client, pid, gateway, registry, invocations):
        result = client.post(f'/projects/{pid}/events', json={'type': 'USER_MESSAGE', 'payload': {'message': ARCHITECTURE_PROMPT}})
        assert result.status_code == 200, result.text
        run = result.json()
        assert run['result'] == 'SUCCESS', run
        assert run['architecture_review_required'] is True
        assert len(gateway.calls) == 2
        assert registry.sessions[-1].verification_kind == 'ARCHITECTURE_ALIGNMENT'
        assert invocations == [(True, False), (False, True)]
        proposal_id, = run['proposal_ids']
        proposals = client.get(f'/projects/{pid}/architecture/proposals').json()
        assert proposals[0]['id'] == proposal_id and proposals[0]['status'] == 'PENDING'
        assert repo.load_context(pid).architecture.version == 1
        preview = client.get(f'/projects/{pid}/architecture/proposals/{proposal_id}/acceptance-preview')
        assert preview.status_code == 200 and preview.json()['actionable'] is True
        accepted = client.post(f'/projects/{pid}/architecture/proposals/{proposal_id}/accept')
        assert accepted.status_code == 200, accepted.text
        architecture = repo.load_context(pid).architecture
        assert architecture.version == 2
        assert next(c for c in architecture.components if c.id == 'database').name == 'PostgreSQL'
        diagram = client.get(f'/projects/{pid}/architecture/diagram?expected_architecture_version=2&reading_mode=MAP')
        assert diagram.status_code == 200 and 'PostgreSQL' in diagram.text
        progress = client.post(f'/projects/{pid}/events', json={'type': 'USER_MESSAGE', 'payload': {'message': PROGRESS_PROMPT}})
        assert progress.status_code == 200 and progress.json()['result'] == 'SUCCESS', progress.text
        assert 'VERIFIED' in progress.json()['summary'] and 'UNVERIFIED' in progress.json()['summary']
        assert progress.json()['proposal_ids'] == []
        assert registry.sessions[-1].verification_kind == 'IMPLEMENTATION_PROGRESS'
        assert invocations == [(True, False), (False, True)] * 2
        assert len(gateway.calls) == 4
        assert repo.load_context(pid).architecture.version == 2
