"""Real PostgreSQL settings and project-authorized MCP routes with a recording provider."""
from __future__ import annotations

import copy
from contextlib import contextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from archbro.backend.api.routes import build_router
from archbro.backend.api.provider_connections import ProviderMcpRuntimeRegistry
from archbro.backend.core.authorization import TrustedPrincipal
from archbro.backend.core.contracts import Project, Architecture, ProjectEvent, ProjectEventSource, ProjectEventType
from archbro.backend.core.github_repository import GitHubRepositoryBinding
from archbro.backend.core.repository import ConcurrentStateError
from archbro.backend.llm.fake import FakeModelProvider
from conftest import requires_database
from test_agent_mcp_tools import _FakeGitHubGateway, PRIVATE_MARKER
from test_code_architecture import code_truth_payload, canonical_architecture

pytestmark = requires_database


class RepositoryGateway(_FakeGitHubGateway):
    def __init__(self):
        super().__init__()
        self.failure = False
        self.before_return = None
    def list_connections(self):
        return [{**c, 'last_probe_ok': True, 'tool_count': 4} for c in super().list_connections()]

    def call_tool(self, connection_id, tool_name, arguments):
        if self.before_return:
            callback, self.before_return = self.before_return, None
            callback()
        if self.failure:
            self.calls.append((connection_id,tool_name,arguments))
            return {'isError':True,'content':[{'type':'text','text':'denied-secret-must-not-leak'}]}
        if tool_name == 'search_repositories':
            self.calls.append((connection_id,tool_name,arguments))
            return {'items':[{'id':1,'full_name':'Magic-Dala/archbro','private':True,'access_token':'must-not-leak'}]}
        if tool_name == 'list_branches':
            self.calls.append((connection_id,tool_name,arguments))
            return {'external_evidence':[{'name':'dev2','commit':{'sha':'a'*40}}]}
        return super().call_tool(connection_id,tool_name,arguments)


@contextmanager
def client_for(repo, *, user='alice', registry=None):
    async def principal(_token):
        return TrustedPrincipal(user_id=user)
    app=FastAPI()
    app.include_router(build_router(repo,FakeModelProvider(),principal_provider=principal,provider_mcp_runtime=registry))
    with TestClient(app,base_url='http://127.0.0.1',headers={'Authorization':'Bearer test'}) as client:
        yield client


@pytest.fixture
def setup(repo):
    project=Project(name='Repository test',goal='Keep architecture aligned',owner_user_id='alice')
    repo.save_project(project)
    repo.save_architecture(project.id,Architecture())
    registry=ProviderMcpRuntimeRegistry()
    gateway=RepositoryGateway()
    registry.gateways['alice']=gateway
    # runtime_for must preserve the existing already-authorized provider identity.
    registry.oauth_managers['alice']=object()
    return repo,project,registry,gateway


def save(client, project_id, name='Magic-Dala/archbro', revision=0, branch=None):
    return client.put(f'/projects/{project_id}/repository',json={'full_name':name,'branch':branch,'expected_revision':revision})


def test_select_read_reload_update_remove_without_new_credentials(setup):
    repo,p,reg,gw=setup
    with client_for(repo,registry=reg) as client:
        before=client.get(f'/projects/{p.id}/repository').json()
        assert before['source_repository'] is None and before['can_manage']
        listed=client.get(f'/projects/{p.id}/repository-options?q=archbro').json()
        assert listed['items'][0]['full_name']=='Magic-Dala/archbro'
        assert 'access_token' not in str(listed)
        selected=save(client,p.id)
        assert selected.status_code==200,selected.text
        assert selected.json()['repository_revision']==1
        assert gw.calls[-1][1]=='list_branches'
        assert 'connection_id' not in str(selected.json()['source_repository'])
        assert 'token' not in str(selected.json()['source_repository'])
        assert repo.get_project(p.id).source_repository.full_name=='Magic-Dala/archbro'
        changed=save(client,p.id,'Magic-Dala/another',1,'dev2')
        assert changed.status_code==200,changed.text
        assert gw.calls[-1][2]['ref']=='dev2'
        removed=client.delete(f'/projects/{p.id}/repository?expected_revision=2')
        assert removed.status_code==200 and removed.json()['source_repository'] is None
        assert repo.get_project(p.id).repository_revision==3


def test_selection_is_owner_only_and_denial_precedes_network(setup):
    repo,p,reg,gw=setup
    p.member_user_ids=['bob']; repo.save_project(p)
    with client_for(repo,registry=reg,user='bob') as member:
        assert member.get(f'/projects/{p.id}/repository').json()['can_manage'] is False
        assert save(member,p.id).status_code==403
        assert member.get(f'/projects/{p.id}/repository-options?q=archbro').status_code==403
        call=member.post(f'/projects/{p.id}/github/tools/get_file_contents',json={'arguments':{'owner':'Magic-Dala','repo':'archbro','path':'README.md'}})
        assert call.status_code==409,call.text
    with client_for(repo,registry=reg,user='mallory') as other:
        assert other.get(f'/projects/{p.id}/repository').status_code==404
    assert gw.calls==[], 'Neither a non-owner nor another member may borrow Alice credentials'


def test_upstream_failure_retains_binding_and_does_not_leak_detail(setup):
    repo,p,reg,gw=setup
    with client_for(repo,registry=reg) as client:
        assert save(client,p.id).status_code==200
        gw.failure=True
        failed=save(client,p.id,'Magic-Dala/other',1)
        assert failed.status_code==409
        assert 'secret-must-not-leak' not in failed.text
        assert repo.get_project(p.id).source_repository.full_name=='Magic-Dala/archbro'
        assert repo.get_project(p.id).repository_revision==1


def test_stale_revision_and_stale_whole_project_writer_cannot_clobber_binding(setup):
    repo,p,reg,gw=setup
    stale=repo.get_project(p.id)
    with client_for(repo,registry=reg) as client:
        assert save(client,p.id).status_code==200
        count=len(gw.calls)
        assert save(client,p.id,'Magic-Dala/other',0).status_code==409
        assert len(gw.calls)==count
    stale.name='New title';repo.save_project(stale)
    stored=repo.get_project(p.id)
    assert stored.name=='New title'
    assert stored.source_repository.full_name=='Magic-Dala/archbro' and stored.repository_revision==1


def test_permission_and_revision_are_rechecked_after_network(setup):
    repo,p,reg,gw=setup
    def revoke():
        current=repo.get_project(p.id)
        current.owner_user_id='bob';repo.save_project(current)
    gw.before_return=revoke
    with client_for(repo,registry=reg) as client:
        assert save(client,p.id).status_code==403
    assert repo.get_project(p.id).source_repository is None


def test_real_api_stream_adapter_scope_and_old_route_cannot_bypass(setup):
    repo,p,reg,gw=setup
    with client_for(repo,registry=reg) as client:
        assert save(client,p.id,branch='dev2').status_code==200
        tools=client.get(f'/projects/{p.id}/github/tools')
        assert tools.status_code==200,tools.text
        assert 'owner' not in tools.json()['tools'][0]['inputSchema']['required']
        result=client.post(f'/projects/{p.id}/github/tools/get_file_contents',json={'arguments':{'path':'README.md'},'expected_repository_revision':1})
        assert result.status_code==200,result.text
        assert PRIVATE_MARKER in result.text
        assert gw.calls[-1][2]=={'owner':'Magic-Dala','repo':'archbro','path':'README.md','ref':'dev2'}
        count=len(gw.calls)
        foreign=client.post(f'/projects/{p.id}/github/tools/get_file_contents',json={'arguments':{'owner':'evil','repo':'other','path':'README.md'}})
        assert foreign.status_code==422
        wrong_connection=client.post(f'/projects/{p.id}/github/tools/get_file_contents',json={'arguments':{'path':'README.md'},'connection_id':'someone-else'})
        assert wrong_connection.status_code==409
        raw=client.post('/mcp/connections/mcp_private/tools/get_file_contents',json={'arguments':{'owner':'evil','repo':'other','path':'README.md'}})
        assert raw.status_code==409
        assert len(gw.calls)==count


def test_snapshot_scope_and_rebinding_hide_old_implementation_evidence(setup):
    repo,p,reg,gw=setup
    repo.save_architecture(p.id,canonical_architecture())
    payload=code_truth_payload()
    with client_for(repo,registry=reg) as client:
        # The existing fixture's repo is used to exercise actual code evidence contracts.
        name=payload['repository']
        assert save(client,p.id,name).status_code==200
        posted=client.post(f'/projects/{p.id}/code-architecture/snapshots',json=payload)
        assert posted.status_code==200,posted.text
        assert client.get(f'/projects/{p.id}/code-architecture/latest').status_code==200
        foreign=copy.deepcopy(payload);foreign['repository']='evil/other'
        assert client.post(f'/projects/{p.id}/code-architecture/snapshots',json=foreign).status_code==422
        assert save(client,p.id,'Magic-Dala/other',1).status_code==200
        assert client.get(f'/projects/{p.id}/code-architecture/latest').status_code==204
        assert len([e for e in repo.list_events(p.id) if e.type==ProjectEventType.CODE_ARCHITECTURE_SNAPSHOT])==1
        stale=ProjectEvent(project_id=p.id,type=ProjectEventType.CODE_ARCHITECTURE_SNAPSHOT,source=ProjectEventSource.SYSTEM,
                           payload={'repository_binding_revision':1,'request':payload})
        with pytest.raises(ConcurrentStateError):
            repo.save_event(stale)


def test_event_repository_scope_blocks_foreign_and_unbound_before_provider_discovery(setup):
    repo,p,reg,gw=setup
    repo.save_architecture(p.id, Architecture(version=1, summary='Accepted architecture'))
    with client_for(repo,registry=reg) as client:
        assert save(client,p.id,branch='dev2').status_code==200
        gw.calls.clear()
        gw.list_connections_calls = 0
        gw.list_tools_calls = 0

        foreign=client.post(
            f'/projects/{p.id}/events',
            json={
                'type':'USER_MESSAGE',
                'payload':{'message':'Use GitHub MCP to read README.md from octocat/Hello-World.'},
            },
        )
        assert foreign.status_code==200,foreign.text
        foreign_body=foreign.json()
        assert foreign_body['result']=='SUCCESS'
        assert foreign_body['provider']=='deterministic'
        assert 'targets octocat/Hello-World' in foreign_body['summary']
        foreign_mcp=foreign_body['context_telemetry']['external_mcp']
        assert foreign_mcp['scope_mode']=='PROJECT_REPOSITORY_MISMATCH'
        assert foreign_mcp['dispatched_call_count']==0
        assert gw.list_connections_calls==0
        assert gw.list_tools_calls==0
        assert gw.calls==[]

        removed=client.delete(f'/projects/{p.id}/repository?expected_revision=1')
        assert removed.status_code==200,removed.text
        gw.calls.clear()
        gw.list_connections_calls = 0
        gw.list_tools_calls = 0

        unbound=client.post(
            f'/projects/{p.id}/events',
            json={
                'type':'USER_MESSAGE',
                'payload':{'message':'Use GitHub MCP to read Magic-Dala/archbro README.md.'},
            },
        )
        assert unbound.status_code==200,unbound.text
        unbound_body=unbound.json()
        assert unbound_body['result']=='SUCCESS'
        assert unbound_body['provider']=='deterministic'
        assert 'Select a GitHub repository' in unbound_body['summary']
        unbound_mcp=unbound_body['context_telemetry']['external_mcp']
        assert unbound_mcp['scope_mode']=='PROJECT_REPOSITORY_REQUIRED'
        assert unbound_mcp['dispatched_call_count']==0
        assert gw.list_connections_calls==0
        assert gw.list_tools_calls==0
        assert gw.calls==[]


def test_settings_do_not_enable_github_for_initial_or_ordinary_tasks(setup):
    repo,p,reg,gw=setup
    with client_for(repo,registry=reg) as client:
        assert save(client,p.id).status_code==200
        gw.calls.clear()
        initial=client.post(f'/projects/{p.id}/events',json={'type':'USER_MESSAGE','payload':{'intent':'INITIAL_ARCHITECTURE','message':p.goal}})
        assert initial.status_code==200,initial.text
        assert gw.calls==[]
        for message in ['GitHub MCP is optional. Explain the current tasks.','請檢查檔案上傳功能的任務進度。']:
            result=client.post(f'/projects/{p.id}/events',json={'type':'USER_MESSAGE','payload':{'message':message}})
            assert result.status_code==200
        assert gw.calls==[]


def test_unbound_webmcp_route_reports_binding_action_without_discovery(setup):
    repo,p,reg,gw=setup
    with client_for(repo,registry=reg) as client:
        for response in (
            client.get(f'/projects/{p.id}/github/tools'),
            client.post(f'/projects/{p.id}/github/tools/get_file_contents',json={'arguments':{'path':'README.md'}}),
        ):
            assert response.status_code==409
            detail=response.json()['detail']
            assert detail['code']=='PROJECT_REPOSITORY_REQUIRED'
            assert detail['action']=='OPEN_PROJECT_REPOSITORY_SETTINGS'
            assert 'reconnect' not in detail['message'].lower()
    assert gw.list_connections_calls==gw.list_tools_calls==0
    assert gw.calls==[]


@pytest.mark.parametrize('target',['"octocat/Hello-World"','“octocat/Hello-World”','「octocat/Hello-World」'])
def test_quoted_scope_denial_is_durable_and_has_no_bound_fallback(setup,target):
    repo,p,reg,gw=setup
    repo.save_architecture(p.id,Architecture(version=1,summary='Accepted architecture'))
    with client_for(repo,registry=reg) as client:
        assert save(client,p.id,branch='dev2').status_code==200
        gw.calls.clear();gw.list_connections_calls=gw.list_tools_calls=0
        response=client.post(f'/projects/{p.id}/events',json={'type':'USER_MESSAGE','payload':{'message':f'Use GitHub MCP to read README.md from {target}.'}})
        assert response.status_code==200,response.text
        body=response.json()
        assert body['result']=='SUCCESS' and body['provider']=='deterministic'
        assert body['context_telemetry']['external_mcp']['scope_mode']=='PROJECT_REPOSITORY_MISMATCH'
        assert body['context_telemetry']['external_mcp']['dispatched_call_count']==0
        assert 'targets octocat/Hello-World' in body['summary']
        assert gw.list_connections_calls==gw.list_tools_calls==0 and gw.calls==[]
