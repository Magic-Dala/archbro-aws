# Project GitHub repository selection

## Use

Open the project's **left sidebar ellipsis (...) > GitHub repository**. Search for a repository or enter `owner/repo` (a GitHub repository URL also works), optionally enter a default branch/ref such as `dev2`, then choose **Verify and save**. Only the project owner can change the selection. Errors stay inside the dialog and a failed verification preserves the previous selection.

**Connect GitHub** reuses the existing personal MCP connection dialog. Closing that dialog returns to the originating project's repository settings only while the navigation context is still current. **Remove binding** removes the project selection, not the user's GitHub authorization.

A project can have zero or one repository. Selection is not required to create a project. Initial architecture generation and ordinary task-status questions do not invoke GitHub just because a repository is selected.

## Identity and authorization

The existing `ProviderMcpRuntimeRegistry` still belongs to the authenticated caller. The project stores only non-secret repository metadata in its existing JSON document: `source_repository` and a monotonic `repository_revision`. It does not store a connection ID, token, new OAuth grant, or per-project MCP process. No new credential store or database table is introduced.

Every invocation uses the caller's own authorized GitHub connection, never the project owner's connection on someone else's behalf. A selected repository does not grant GitHub/org permissions. Reconnecting does not change the repository binding; discovery resolves the current connection. Multiple ready GitHub connections are refused rather than guessed between.

`PUT /projects/{id}/repository` verifies access via the current MCP connection (branch metadata, or root content at the selected ref). It does not require a README. The subsequent write rechecks project ownership and the expected revision under a PostgreSQL row lock. Ordinary project/architecture writes preserve the existing repository fields, so a stale whole-project model cannot undo a newer selection.

`repository_id` is reserved optional metadata; this implementation does not resolve or use it for connector scheduling. Repository rename/transfer continuity and automatic polling/webhook setup are not part of this change.

## Runtime boundary

Internal tools and the browser-facing project GitHub API use the same `AgentMcpToolSession`, JSON-schema adapter, and repository scope guard.

For a bound project, repo-targeted tools receive missing `owner`/`repo` from server-owned settings. Explicit conflicting targets fail before provider dispatch. The selected branch is a default, not an authorization boundary: an explicit different ref/SHA within the same repo remains valid. Code searches are narrowed to the repository and ambiguous Boolean or resource-selector syntax is refused. Unknown tools are not automatically enabled. Account-wide `search_repositories` is available to the human settings picker, not to the bound project's agent.

The public tool specification and the real Strands `AgentTool.stream()` validate the same flat schema. Legacy `{arguments: {...}}` is accepted only as an unambiguous single envelope. Invalid inputs are recorded as pre-dispatch validation failures, not as successful GitHub calls. `sdk_stream_attempts`, validation failures, and actual provider dispatch counts distinguish previously indistinguishable failures. This adapter uses the SDK's `ToolResultEvent` wrapper and is covered against the deployed Strands version.

Scope is checked before cached results are returned and after provider responses. In-flight scope changes discard the result. Code architecture snapshots record the binding revision and validate it again at the database write boundary. Historical snapshots remain stored, but a snapshot from an older binding is not presented as the current implementation. Browser `result_ref` recovery is project/generation/repository-version-bound, including a server revision check when available.

## API compatibility

- `GET /projects/{id}/repository`: selection, revision, ownership controls, and caller connection status.
- `GET /projects/{id}/repository-options?q=...&page=...`: bounded human repository search; MANAGE required.
- `PUT /projects/{id}/repository`: `full_name`, optional `branch`, and `expected_revision`.
- `DELETE /projects/{id}/repository?expected_revision=...`: remove only the selection.
- `GET /projects/{id}/github/tools`: approved tools with the current project schema.
- `POST /projects/{id}/github/tools/{tool_name}`: arguments, optional current connection ID, and optional expected repository revision.

Existing WebMCP public tool names and tool count remain unchanged. Their GitHub calls now use the project route. Raw account-wide GitHub tool execution through `/mcp/connections/{id}/tools/{name}` returns 409 with the new route; personal connection management and non-GitHub providers are unchanged. A bound project cannot use the known deployment-configured official GitHub MCP endpoint as an alternate credential/scope path. Arbitrary deployment-managed custom MCP servers remain a separate trusted integration boundary, not a claim of general-purpose per-resource sandboxing.

Unbound projects retain legacy explicit-target GitHub behavior through the project route. They are labelled `EXPLICIT_TARGET_LEGACY`; this is compatibility behavior, not a single-repository restriction. Selecting a repository changes their calls to `PROJECT_REPOSITORY` mode without changing credentials.

## Local acceptance performed, 2026-09-12

Base: dev2 `6d48dd60fef6f178a033b6548c0e27874ce0f335`.

The reported flat-input failure and the two optional-intent false positives were independently reproduced before the change. Candidate tests used Strands **1.55.1**, Pydantic **2.13.5**, and google-genai **2.23.0**, matching the supplied deployment report. Tests used a disposable PostgreSQL 17 instance, not the deployed database.

| Group | Result |
|---|---:|
| Repository API, schema/real stream, existing MCP, code snapshots | 75 passed |
| Hierarchical planner, event API, provider surface, Gemini fallback | 120 passed |
| WebMCP integration/schema budget, context gateway/text | 46 passed |
| Repository dialog and result-ref isolation Node tests | 12 passed |
| Existing navigation, UI prototype and workspace Node regressions | 58 passed |
| Actual local browser -> product API -> PostgreSQL with fake GitHub | PASS; zero page errors |

The real Strands Agent loop uses a deterministic injected model emitting a flat tool call, then asserts that the returned private-fixture sentinel reaches its next turn. It does not bypass `.stream()` or merely call a decorated function directly. The local browser smoke clicks the real sidebar menu, searches, saves, reloads, switches projects, and invokes the existing WebMCP tool against the real project API. Its GitHub gateway is explicitly a recording fake.

Commands (set `DATABASE_URL` to a disposable local test database first):

```text
python -B -m pytest tests/test_project_repository_tools.py tests/test_project_repository_api.py tests/test_agent_mcp_tools.py tests/test_code_architecture.py -q -p no:cacheprovider
python -B -m pytest tests/test_hierarchical_planner.py tests/test_api_contract.py tests/test_provider_mcp_surface.py tests/test_gemini_fallback.py -q -p no:cacheprovider
python -B -m pytest tests/test_webmcp_integration.py tests/test_webmcp_schema_budget.py tests/test_agent_context_mcp_gateway.py tests/test_context_projection_text.py -q -p no:cacheprovider
node --test qa/test_project_repository.mjs qa/test_repository_webmcp.mjs
node --test qa/test_ui_prototype.mjs qa/test_workspace_async_state.mjs qa/test_workspace_review_transition.mjs qa/test_canvas_navigation_followups.mjs
python -B qa/project_repository_browser_smoke.py
```

The browser smoke intentionally refuses anything except the disposable localhost port 55479, creates/drops its own schema, and shuts down its local server. Its screenshot/report are written under `.local/browser-repository/`, not into source history.

## Release acceptance still required

This is **local targeted acceptance**, not a full Python/Node/CI, production-capacity, or real-provider acceptance claim. No production deployment, OAuth change, or paid model call was performed in this implementation pass. No planner timeouts, retry policy, or UNKNOWN-paid-call safeguards were relaxed.

After an authorized deployment, select `Magic-Dala/archbro` and `dev2` in a disposable project, then submit the exact original prompts:

1. `Use GitHub MCP to read Magic-Dala/archbro README.md on the dev2 branch, then summarize what the repository says Archbro is for. Do not modify anything.`
2. `Review the repo status for Magic-Dala/archbro dev2 and confirm the latest commit or branch evidence using GitHub MCP.`

Require actual built-in-agent dispatch, successful evidence, and the correct repository/ref. A separate direct connector call, HTTP 200, discovery READY, or zero missing-owner errors is not proof of this acceptance. The original roughly 36-second timeouts were not conclusively attributed to the schema mismatch alone; the new pre-dispatch telemetry makes a remaining tool-selection/model-timeout failure distinguishable without blindly increasing limits.
