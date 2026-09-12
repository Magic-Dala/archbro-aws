"""Project repository settings reuse the caller's personal GitHub connection."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.concurrency import run_in_threadpool

from archbro.backend.core.authorization import ProjectAuthorizer, ProjectPermission
from archbro.backend.core.contracts import ProjectEvent, ProjectEventType
from archbro.backend.core.github_repository import (
    GitHubRepositoryBinding, normalize_github_repository, normalize_repository_branch,
)
from archbro.backend.core.repository import ConcurrentStateError
from archbro.backend.mcp.agent_tools import AgentMcpToolSession
from archbro.backend.mcp.github_project_scope import repository_options, verify_repository


class RepositorySelection(BaseModel):
    model_config = ConfigDict(extra='forbid')
    full_name: str = Field(min_length=3, max_length=300)
    branch: str | None = Field(default=None, max_length=200)
    expected_revision: int = Field(ge=0)
    _name = field_validator('full_name')(normalize_github_repository)
    _branch = field_validator('branch')(normalize_repository_branch)


class ProjectGitHubCall(BaseModel):
    model_config = ConfigDict(extra='forbid')
    arguments: dict[str, Any] = Field(default_factory=dict)
    connection_id: str | None = Field(default=None, max_length=200)
    expected_repository_revision: int | None = Field(default=None, ge=0)


def build_project_repository_router(repository, registry, authorized_project, principal_for):
    router = APIRouter()
    authorizer = ProjectAuthorizer()

    def describe(project, principal, gateway):
        try:
            authorizer.require(principal, project, ProjectPermission.MANAGE)
            can_manage = True
        except PermissionError:
            can_manage = False
        connected = any(c.get('provider') == 'github' and not c.get('authorization_pending')
                        and c.get('last_probe_ok') is not False for c in gateway.list_connections())
        return {'project_id': project.id, 'source_repository': project.source_repository,
                'repository_revision': project.repository_revision,
                'can_manage': can_manage, 'connected': connected}

    async def resolved(http_request, project_id, permission):
        principal = await principal_for(http_request)
        project = await authorized_project(http_request, project_id, permission, principal=principal)
        # Match the existing personal provider boundary on public/tunneled demo origins.
        from archbro.backend.api.provider_connections import _request_uses_loopback_origin
        if principal.local_development and not _request_uses_loopback_origin(http_request):
            raise HTTPException(403, 'Public provider connections require verified per-user authentication')
        gateway, _ = registry.runtime_for(principal)
        return principal, project, gateway

    @router.get('/projects/{project_id}/repository')
    async def get_repository(project_id: str, http_request: Request):
        principal, project, gateway = await resolved(http_request, project_id, ProjectPermission.READ)
        return describe(project, principal, gateway)

    @router.get('/projects/{project_id}/repository-options')
    async def get_repository_options(project_id: str, http_request: Request,
                                     q: str = Query(min_length=2, max_length=200),
                                     page: int = Query(default=1, ge=1, le=50)):
        _, _, gateway = await resolved(http_request, project_id, ProjectPermission.MANAGE)
        try:
            return await run_in_threadpool(repository_options, gateway, q, page)
        except (ValueError, RuntimeError, KeyError):
            raise HTTPException(409, 'Repository search is unavailable. Connect GitHub, or enter owner/repo directly and verify it.') from None

    @router.put('/projects/{project_id}/repository')
    async def put_repository(project_id: str, request: RepositorySelection, http_request: Request):
        principal, project, gateway = await resolved(http_request, project_id, ProjectPermission.MANAGE)
        if project.repository_revision != request.expected_revision:
            raise HTTPException(409, 'Repository selection changed. Reload before saving.')
        try:
            await run_in_threadpool(verify_repository, gateway, request.full_name, request.branch)
        except (ValueError, RuntimeError, KeyError):
            raise HTTPException(409, 'Could not verify repository/branch access using your GitHub account. Check the name, branch and connection; the saved selection was not changed.') from None
        binding = GitHubRepositoryBinding(full_name=request.full_name, branch=request.branch,
                                           selected_by_user_id=principal.user_id)
        try:
            updated = await run_in_threadpool(
                repository.set_source_repository, project_id, binding,
                expected_revision=request.expected_revision, actor_user_id=principal.user_id,
                local_development=principal.local_development,
            )
        except ConcurrentStateError:
            raise HTTPException(409, 'Repository selection changed during verification. Reload before saving.') from None
        except PermissionError:
            raise HTTPException(403, 'Only the project owner may change its repository.') from None
        except KeyError:
            raise HTTPException(404, 'project not found') from None
        return describe(updated, principal, gateway)

    @router.delete('/projects/{project_id}/repository')
    async def delete_repository(project_id: str, http_request: Request, expected_revision: int = Query(ge=0)):
        principal, _, gateway = await resolved(http_request, project_id, ProjectPermission.MANAGE)
        try:
            updated = await run_in_threadpool(
                repository.set_source_repository, project_id, None,
                expected_revision=expected_revision, actor_user_id=principal.user_id,
                local_development=principal.local_development,
            )
        except ConcurrentStateError:
            raise HTTPException(409, 'Repository selection changed. Reload before removing.') from None
        except PermissionError:
            raise HTTPException(403, 'Only the project owner may change its repository.') from None
        except KeyError:
            raise HTTPException(404, 'project not found') from None
        return describe(updated, principal, gateway)

    async def session_for(http_request, project_id):
        principal, project, gateway = await resolved(http_request, project_id, ProjectPermission.WRITE)
        def check_scope():
            current = repository.get_project(project_id)
            authorizer.require(principal, current, ProjectPermission.WRITE)
            if current.repository_revision != project.repository_revision:
                raise ValueError('Repository selection changed during this request; retry')
        event = ProjectEvent(project_id=project_id, type=ProjectEventType.USER_MESSAGE,
                             payload={'message': 'Use GitHub MCP to read repository evidence.'})
        session = await run_in_threadpool(
            AgentMcpToolSession.discover, gateway, project_id=project_id, event=event,
            repository_scope=project.source_repository, scope_check=check_scope,
        )
        if session is None or not session.has_tools:
            raise HTTPException(409, 'No usable GitHub tools are available through your account. Connect or reconnect GitHub.')
        return project, session

    @router.get('/projects/{project_id}/github/tools')
    async def get_github_tools(project_id: str, http_request: Request):
        project, session = await session_for(http_request, project_id)
        return {'project_id': project_id, 'repository_revision': project.repository_revision,
                'source_repository': project.source_repository, 'tool_count': len(session.descriptors),
                'tools': [{'name': d.name, 'description': d.description, 'inputSchema': d.input_schema,
                           'annotations': {'readOnlyHint': True}} for d in session.descriptors]}

    @router.post('/projects/{project_id}/github/tools/{tool_name}')
    async def call_github_tool(project_id: str, tool_name: str, request: ProjectGitHubCall, http_request: Request):
        project, session = await session_for(http_request, project_id)
        if request.connection_id and request.connection_id != session.connection_id:
            raise HTTPException(409, 'Your GitHub connection changed. Refresh the available tools and retry.')
        if request.expected_repository_revision is not None and request.expected_repository_revision != project.repository_revision:
            raise HTTPException(409, 'Repository selection changed. Refresh the project context and retry.')
        tool = next((t for t in session.strands_tools() if t.tool_name == tool_name), None)
        if tool is None:
            raise HTTPException(403, 'This GitHub tool is not available in the project scope.')
        try:
            result = await run_in_threadpool(tool, **request.arguments)
        except (ValueError, PermissionError):
            raise HTTPException(422, 'GitHub tool arguments conflict with the selected repository, schema or current project context.') from None
        except (RuntimeError, KeyError):
            raise HTTPException(502, 'The GitHub read failed. Check repository access and the connection; no verified evidence was produced.') from None
        return {'project_id': project_id, 'repository_revision': project.repository_revision,
                'result': result, 'telemetry': session.telemetry(), 'classification': 'EXTERNAL_EVIDENCE',
                'canonical_state_mutated': False}

    return router
