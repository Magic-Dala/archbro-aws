from __future__ import annotations

import ast
import json
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "frontend" / "web" / "archbro-webmcp.js"
APP_MODULE = ROOT / "frontend" / "web" / "app.js"
MAX_MODEL_INVENTORY_CHARS = 14_000


def _model_inventory() -> list[dict[str, object]]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the WebMCP schema budget check")
    script = r"""
const fs = require('fs');
let source = fs.readFileSync('frontend/web/archbro-webmcp.js', 'utf8')
  .replace(/^import .*?;\r?\n/, '')
  .replace(/\bexport\s+/g, '');
source += '\nglobalThis.__createArchBroTools = createArchBroTools;';
eval(source);
const noop = async () => ({});
const bridge = {
  bootstrapProject: noop,
  expandArchitectureScope: noop,
  getDecisionContext: noop,
  submitAgentRecommendation: noop,
  createTask: noop,
  updateTaskStatus: noop,
  recordProjectObservation: noop,
  publishCodeArchitectureSnapshot: noop,
};
const inventory = globalThis.__createArchBroTools(bridge).map((tool) => ({
  name: tool.name,
  title: tool.title,
  description: tool.description,
  inputSchema: tool.inputSchema,
  readOnly: tool.annotations?.readOnlyHint === true,
}));
process.stdout.write(JSON.stringify(inventory));
"""
    completed = subprocess.run(
        [node, "-e", script],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def test_webmcp_model_inventory_stays_within_fixed_context_budget():
    inventory = _model_inventory()
    assert [tool["name"] for tool in inventory] == [
        "archbro_ping",
        "archbro_get_agent_context",
        "archbro_get_architecture_diagram",
        "archbro_publish_code_architecture",
        "archbro_get_code_architecture",
        "archbro_get_architecture_node_context",
        "archbro_find_architecture_path",
        "archbro_bootstrap_project",
        "archbro_expand_architecture_scope",
        "archbro_get_architecture_decision_context",
        "archbro_submit_architecture_recommendation",
        "archbro_create_task",
        "archbro_update_task_status",
        "archbro_record_project_observation",
        "archbro_list_connected_mcp_servers",
        "archbro_list_connected_mcp_tools",
        "archbro_call_connected_mcp_tool",
    ]
    encoded = json.dumps(inventory, separators=(",", ":"), ensure_ascii=False)
    assert len(encoded) <= MAX_MODEL_INVENTORY_CHARS


def test_webmcp_model_schemas_keep_validation_and_bootstrap_contract_prose():
    inventory = _model_inventory()
    by_name = {tool["name"]: tool for tool in inventory}
    agent_context = by_name["archbro_get_agent_context"]
    bootstrap = by_name["archbro_bootstrap_project"]
    code_snapshot = by_name["archbro_publish_code_architecture"]
    create_task = by_name["archbro_create_task"]
    connected_call = by_name["archbro_call_connected_mcp_tool"]

    assert agent_context["inputSchema"]["properties"]["node_id"]["pattern"] == "^node:.+"
    assert "direction" not in agent_context["inputSchema"]["properties"]
    assert "expansion_policy" not in agent_context["inputSchema"]["properties"]
    assert agent_context["inputSchema"]["properties"]["expected_architecture_version"]["minimum"] == 1
    assert "planning_trace" in bootstrap["inputSchema"]["required"]
    bootstrap_properties = bootstrap["inputSchema"]["properties"]
    assert bootstrap_properties["components"]["maxItems"] == 6
    assert bootstrap_properties["components"]["description"] == (
        "SYSTEM_MAP roots; each must have children."
    )
    planning = bootstrap_properties["planning_trace"]["properties"]
    assert planning["system_map_root_ids"]["description"] == (
        "Root ids in component order; all EXPANDED."
    )
    assert planning["scope_evaluations"]["description"] == (
        "One per component in depth-first preorder."
    )
    evaluation = planning["scope_evaluations"]["items"]["properties"]
    assert evaluation["child_ids"]["description"] == (
        "EXPANDED: ordered child ids; JUSTIFIED_LEAF: empty."
    )
    assert "SYSTEM_MAP root" in bootstrap["description"]
    assert "depth-first preorder" in bootstrap["description"]
    assert code_snapshot["inputSchema"]["properties"]["revision"]["pattern"] == "^[0-9a-fA-F]{40}$"
    assert code_snapshot["inputSchema"]["properties"]["source_evidence"]["maxItems"] == 160
    assert connected_call["inputSchema"]["properties"]["result_ref"]["minLength"] == 1
    assert connected_call["inputSchema"]["properties"]["max_chars"]["maximum"] == 12000
    assert connected_call["inputSchema"]["required"] == ["server_id", "tool_name"]
    assert "description" in create_task["inputSchema"]["properties"]
    assert create_task["inputSchema"]["properties"]["description"] == {"type": "string"}
    bootstrap_task = bootstrap["inputSchema"]["properties"]["tasks"]["items"]
    assert "description" in bootstrap_task["properties"]
    assert bootstrap_task["properties"]["description"] == {"type": "string"}
    assert code_snapshot["inputSchema"]["properties"]["repository"] == {
        "type": "string",
        "minLength": 3,
    }


def test_webmcp_regressions_have_unique_names():
    module = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    names = [
        item.name for item in module.body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        and item.name.startswith("test_")
    ]
    assert len(names) == len(set(names)), "duplicate test names shadow regression coverage"


@pytest.mark.parametrize("query,stored", [("", "project-1"), ("?project=project-1", "project-stale")])
def test_webmcp_path_and_bounded_context_execute_only_through_canonical_server_endpoints(query, stored):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the WebMCP bridge execution check")
    script = r"""
const fs = require('fs');
const getFirebaseIdToken = async () => null;
let source = fs.readFileSync('frontend/web/archbro-webmcp.js', 'utf8')
  .replace(/^import .*?;\r?\n/, '')
  .replace(/\bexport\s+/g, '');
source += '\nglobalThis.__createArchBroTools = createArchBroTools;';
globalThis.location = {search: process.argv[1]};
globalThis.localStorage = {getItem: (key) => key === 'archbro-project-id' ? process.argv[2] : null};
const requests = [];
let pathStatus = 'FOUND';
globalThis.fetch = async (url, options = {}) => {
  const value = String(url);
  const body = options.body ? JSON.parse(options.body) : null;
  requests.push({url: value, method: options.method || 'GET', body});
  if (value === '/mcp/connections') return {ok: true, status: 200, json: async () => []};
  if (value === '/projects/project-1/agent-context') {
    return {ok: true, status: 200, json: async () => ({format: 'markdown', content: '# compact', connected_source_count: 0})};
  }
  if (value === '/projects/project-1/agent-context/manifest') {
    return {
      ok: true,
      status: 200,
      json: async () => ({
        schema: 'archbro.agent_context_manifest.v1',
        project_id: 'project-1',
        architecture_version: 7,
        selection: {
          node_id: body.node_id,
          direction: body.direction,
          expansion_policy: body.expansion_policy,
          effective_max_hops: 1,
          effective_max_results: 8,
        },
        sections: {architecture: {}, tasks: [], evidence: [], code_truth: {status: 'NO_SNAPSHOT', chunks: []}, mcp_refs: []},
        usage: {context_chars: 100, estimated_input_tokens: 25, truncated: false, limit_reasons: []},
        manifest_hash: 'a'.repeat(64),
      }),
    };
  }
  if (value.startsWith('/projects/project-1/architecture/path?')) {
    if (pathStatus === 'STALE') {
      return {
        ok: false,
        status: 409,
        json: async () => ({detail: {code: 'stale_architecture_version', expected_architecture_version: 6, current_architecture_version: 7}}),
      };
    }
    return {
      ok: true,
      status: 200,
      json: async () => ({schema: 'archbro.architecture_path.v1', architecture_version: 7, status: pathStatus, nodes: [], relationships: []}),
    };
  }
  throw new Error(`unexpected fetch ${value}`);
};
eval(source);
const noop = async () => ({});
const bridge = {
  bootstrapProject: noop,
  expandArchitectureScope: noop,
  getDecisionContext: noop,
  submitAgentRecommendation: noop,
  createTask: noop,
  updateTaskStatus: noop,
  recordProjectObservation: noop,
  publishCodeArchitectureSnapshot: noop,
};
(async () => {
  const tools = globalThis.__createArchBroTools(bridge);
  const contextTool = tools.find((item) => item.name === 'archbro_get_agent_context');
  const pathTool = tools.find((item) => item.name === 'archbro_find_architecture_path');
  const legacy = JSON.parse(await contextTool.execute({}));
  const beforeBounded = requests.length;
  const bounded = JSON.parse(await contextTool.execute({
    node_id: 'node:api',
    expected_architecture_version: 7,
  }));
  const boundedRequests = requests.slice(beforeBounded);
  let unboundedVersionError = '';
  const beforeInvalidContext = requests.length;
  try {
    await contextTool.execute({expected_architecture_version: 999});
  } catch (error) {
    unboundedVersionError = String(error.message || error);
  }
  const invalidContextRequestCount = requests.length - beforeInvalidContext;
  const statuses = [];
  for (const status of ['FOUND', 'UNREACHABLE', 'LIMIT_REACHED']) {
    pathStatus = status;
    const result = JSON.parse(await pathTool.execute({source_id: 'node:web', target_id: 'node:api', max_hops: 3, expected_architecture_version: 7}));
    statuses.push(result.status);
  }
  pathStatus = 'STALE';
  let staleError = '';
  try {
    await pathTool.execute({source_id: 'node:web', target_id: 'node:api', max_hops: 3, expected_architecture_version: 6});
  } catch (error) {
    staleError = String(error.message || error);
  }
  process.stdout.write(JSON.stringify({legacy, bounded, boundedRequests, unboundedVersionError, invalidContextRequestCount, statuses, staleError, requests}));
})().catch((error) => { console.error(error); process.exit(1); });
"""
    completed = subprocess.run(
        [node, "-e", script, query, stored],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    result = json.loads(completed.stdout)
    assert result["legacy"]["content"] == "# compact"
    assert result["bounded"]["mode"] == "BOUNDED_NODE"
    assert result["bounded"]["manifest"]["schema"] == "archbro.agent_context_manifest.v1"
    assert result["bounded"]["agent_context_request"] == {
        "node_id": "node:api",
        "direction": "both",
        "expansion_policy": "ASK_ALL",
        "expected_architecture_version": 7,
        "preview_manifest_hash": "a" * 64,
    }
    assert result["boundedRequests"] == [{
        "url": "/projects/project-1/agent-context/manifest",
        "method": "POST",
        "body": {
            "node_id": "node:api",
            "direction": "both",
            "expansion_policy": "ASK_ALL",
            "expected_architecture_version": 7,
        },
    }]
    assert "only valid when node_id is provided" in result["unboundedVersionError"]
    assert result["invalidContextRequestCount"] == 0
    assert result["statuses"] == ["FOUND", "UNREACHABLE", "LIMIT_REACHED"]
    assert "409:" in result["staleError"]
    assert "stale_architecture_version" in result["staleError"]
    assert '"expected_architecture_version":6' in result["staleError"]
    assert '"current_architecture_version":7' in result["staleError"]
    path_requests = [item for item in result["requests"] if "/architecture/path?" in item["url"]]
    assert len(path_requests) == 4
    assert all(item["method"] == "GET" for item in path_requests)
    assert all("source_id=node%3Aweb" in item["url"] for item in path_requests)
    assert all("target_id=node%3Aapi" in item["url"] for item in path_requests)


def test_webmcp_url_project_overrides_stale_local_storage_project():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the WebMCP deep-link check")
    script = r"""
const fs = require('fs');
let source = fs.readFileSync('frontend/web/archbro-webmcp.js', 'utf8')
  .replace(/^import .*?;\r?\n/, '')
  .replace(/\bexport\s+/g, '');
source += '\nglobalThis.__activeProjectId = activeProjectId;';
globalThis.localStorage = {getItem: () => 'project-stale'};
globalThis.location = {search: '?project=project-deep-link'};
eval(source);
process.stdout.write(globalThis.__activeProjectId());
"""
    completed = subprocess.run(
        [node, "-e", script],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert completed.stdout == "project-deep-link"


def test_webmcp_loaded_project_bridge_overrides_stale_url_and_storage():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the WebMCP active-project binding check")
    script = r"""
const fs = require('fs');
let source = fs.readFileSync('frontend/web/archbro-webmcp.js', 'utf8')
  .replace(/^import .*?;\r?\n/, '')
  .replace(/\bexport\s+/g, '');
source += '\nglobalThis.__activeProjectId = activeProjectId;';
globalThis.location = {search: '?project=project-alpha'};
globalThis.localStorage = {getItem: () => 'project-alpha'};
globalThis.window = {
  ArchBroWebBridge: {
    getActiveProjectId: () => 'project-bravo',
  },
};
eval(source);
process.stdout.write(globalThis.__activeProjectId());
"""
    completed = subprocess.run(
        [node, "-e", script],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert completed.stdout == "project-bravo"


def test_app_has_one_active_project_persistence_boundary_for_url_and_storage():
    source = APP_MODULE.read_text(encoding="utf-8")

    assert "const REQUESTED_PROJECT_ID = String(URL_PARAMS.get('project') || '').trim() || null;" in source
    assert "const persistedProjectId = localStorage.getItem('archbro-project-id');" in source
    assert "const initialProjectId = REQUESTED_PROJECT_ID || persistedProjectId;" in source
    assert source.count("localStorage.setItem('archbro-project-id'") == 1
    assert source.count("localStorage.removeItem('archbro-project-id'") == 1
    assert "function commitNavigation(" in source
    assert "function writeNavigationUrl(" in source
    assert "function persistActiveProjectSelection(" not in source
    assert "function clearActiveProjectSelection(" not in source
    writer = source[
        source.index("function commitNavigation("):
        source.index("function recommitCurrentNavigationAfterFailedTransition(")
    ]
    assert "localStorage.setItem('archbro-project-id', normalized.projectId)" in writer
    assert "localStorage.removeItem('archbro-project-id')" in writer

def test_workspace_context_generation_rejects_late_project_and_refresh_commits():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the workspace commit-generation check")
    source = APP_MODULE.read_text(encoding="utf-8")
    start = source.index("let workspaceContextGeneration = 0;")
    end = source.index("if (initialProjectId) state.expandedProjectIds.add(initialProjectId);")
    helpers = source[start:end]
    script = r"""
const state = {projectId: 'project-a'};
eval(process.argv[1]);
const slowA = beginWorkspaceContextRequest('project-a');
const fastB = beginWorkspaceContextRequest('project-b');
const committedB = commitWorkspaceContext(fastB, {project: {id: 'project-b'}}, {projectId: 'project-b'});
const committedLateA = commitWorkspaceContext(slowA, {project: {id: 'project-a'}}, {projectId: 'project-a'});
const oldRefresh = beginWorkspaceContextRequest('project-b');
const newRefresh = beginWorkspaceContextRequest('project-b');
const committedOldRefresh = commitWorkspaceContext(oldRefresh, {project: {id: 'project-b-old'}}, {}, {requireSelectedProject: true});
const committedNewRefresh = commitWorkspaceContext(newRefresh, {project: {id: 'project-b-new'}}, {}, {requireSelectedProject: true});
process.stdout.write(JSON.stringify({committedB, committedLateA, committedOldRefresh, committedNewRefresh, state}));
"""
    completed = subprocess.run(
        [node, "-e", script, helpers],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    result = json.loads(completed.stdout)
    assert result["committedB"] is True
    assert result["committedLateA"] is False
    assert result["committedOldRefresh"] is False
    assert result["committedNewRefresh"] is True
    assert result["state"]["projectId"] == "project-b"
    assert result["state"]["project"]["id"] == "project-b-new"


def test_direct_webmcp_binding_fails_closed_if_project_changes_during_request_setup():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the WebMCP project-binding race check")
    source = MODULE.read_text(encoding="utf-8")
    start = source.index("function activeProjectBinding()")
    end = source.index("async function agentSurfaceApi(")
    helpers = source[start:end]
    script = r"""
let binding = {projectId: 'project-a', generation: 7};
globalThis.window = {ArchBroWebBridge: {getActiveProjectBinding: () => binding}};
globalThis.location = {search: ''};
globalThis.localStorage = {getItem: () => null};
eval(process.argv[1]);
const captured = activeProjectBinding();
binding = {projectId: 'project-b', generation: 8};
let error = '';
try { assertActiveProjectBinding(captured); } catch (exc) { error = String(exc.message || exc); }
process.stdout.write(error);
"""
    completed = subprocess.run(
        [node, "-e", script, helpers],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert "Active ArchBro project changed" in completed.stdout


def test_workspace_exit_supersedes_pending_context_commit():
    source = APP_MODULE.read_text(encoding="utf-8")
    fn = source[
        source.index("async function openPersonalWorkspace("):
        source.index("function renderProjectTree()")
    ]
    assert "const guard = navigationGuard || beginNavigationTransition(null);" in fn
    assert "supersedeWorkspaceContextRequests();" in fn
    assert "beginWorkspaceContext(state.workspaceAsync, null);" in fn
    assert "commitNavigation({projectId:null, view:'overview', canvas:false, nodeId:null, inspectorTab:'overview'}" in fn
    assert "if (!navigationGenerationIsCurrent(guard)) return false;" in fn

def test_delete_completion_cannot_clear_a_newly_selected_project():
    source = APP_MODULE.read_text(encoding="utf-8")
    fn = source[
        source.index("async function deleteCurrentProject()"):
        source.index("function closeDialogOnBackdrop")
    ]
    assert "const guard = captureNavigationGuard(deletedId);" in fn
    assert "if (!committedProjectGuardIsCurrent(guard))" in fn
    assert "const currentGuard = captureNavigationGuard(state.projectId);" in fn
    assert "await loadProjects({guard:currentGuard});" in fn
    assert "if (!committedProjectGuardIsCurrent(guard)) return;" in fn

def test_logout_invalidates_pending_project_context_before_firebase_signout_returns():
    source = APP_MODULE.read_text(encoding="utf-8")
    logout_start = source.index("async function logout()")
    logout_end = source.index("function openAccountSection")
    logout_source = source[logout_start:logout_end]
    assert "supersedeWorkspaceContextRequests();\n    await signOutFromFirebase();" in logout_source


def test_bootstrap_failure_does_not_restore_old_project_over_a_new_selection():
    source = APP_MODULE.read_text(encoding="utf-8")
    bootstrap_start = source.index("  async bootstrapProject(")
    bootstrap_end = source.index("\n  async expandArchitectureScope(", bootstrap_start)
    bootstrap_source = source[bootstrap_start:bootstrap_end]
    assert "const callerGuard = captureNavigationGuard(state.projectId);" in bootstrap_source
    assert "if (navigationGenerationIsCurrent(callerGuard))" in bootstrap_source
    assert "const activationGuard = beginNavigationTransition(project.id);" in bootstrap_source
    assert "navigationGuard:activationGuard" in bootstrap_source
    assert "state.projectId = project.id" not in bootstrap_source
    assert "persistActiveProjectSelection" not in bootstrap_source


def test_webmcp_project_creation_has_no_undefined_dispatch_gate():
    source = APP_MODULE.read_text(encoding="utf-8")
    assert "allowActionDispatch" not in source


def test_webmcp_code_publish_routes_through_ui_bridge_and_compacts_excerpts():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the WebMCP publish bridge check")
    script = r"""
const fs = require('fs');
let source = fs.readFileSync('frontend/web/archbro-webmcp.js', 'utf8')
  .replace(/^import .*?;\r?\n/, '')
  .replace(/\bexport\s+/g, '');
source += '\nglobalThis.__createArchBroTools = createArchBroTools;';
eval(source);
const noop = async () => ({});
const calls = [];
const bridge = {
  bootstrapProject: noop,
  expandArchitectureScope: noop,
  getDecisionContext: noop,
  submitAgentRecommendation: noop,
  createTask: noop,
  updateTaskStatus: noop,
  recordProjectObservation: noop,
  publishCodeArchitectureSnapshot: async (args) => {
    calls.push(args);
    return {
      id: 'snapshot-1',
      diagram: {
        nodes: [{id: 'code:ui', sources: [{path: 'src/ui.js', line_start: 1, excerpt: 'private excerpt'}]}],
        edges: [],
      },
    };
  },
};
(async () => {
  const signal = {tag: 'client-signal'};
  const tool = globalThis.__createArchBroTools(bridge).find((item) => item.name === 'archbro_publish_code_architecture');
  const result = JSON.parse(await tool.execute({
    repository: 'Magic-Dala/archbro',
    revision: 'a'.repeat(40),
    summary: 'Synthetic bridge fixture',
    components: [{id: 'code:ui', name: 'UI', type: 'module', responsibility: 'Render UI', sources: [{path: 'src/ui.js', line_start: 1, excerpt: 'fixture'}]}],
    relationships: [],
    source_evidence: [{path: 'src/ui.js', line_start: 1, excerpt: 'fixture'}],
  }, {signal}));
  process.stdout.write(JSON.stringify({calls, result}));
})().catch((error) => { console.error(error); process.exit(1); });
"""
    completed = subprocess.run(
        [node, "-e", script],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    result = json.loads(completed.stdout)
    assert len(result["calls"]) == 1
    assert result["calls"][0]["repository"] == "Magic-Dala/archbro"
    assert result["calls"][0]["sourceEvidence"][0]["excerpt"] == "fixture"
    assert result["calls"][0]["signal"] == {"tag": "client-signal"}
    assert result["result"]["id"] == "snapshot-1"
    assert result["result"]["diagram"]["nodes"][0]["sources"] == [
        {"path": "src/ui.js", "line_start": 1}
    ]


def test_bootstrap_result_resolves_committed_architecture_version_before_success():
    source = APP_MODULE.read_text(encoding="utf-8")
    bootstrap_start = source.index("  async bootstrapProject(")
    bootstrap_end = source.index("\n  async expandArchitectureScope(", bootstrap_start)
    bootstrap_source = source[bootstrap_start:bootstrap_end]
    assert "let committedArchitectureVersion = null;" in bootstrap_source
    assert "await resolveBootstrapInitialization(project.id, null)" in bootstrap_source
    assert "await resolveBootstrapInitialization(project.id, result)" in bootstrap_source
    assert "bootstrapOutcomeUnknownError(project.id" in bootstrap_source
    assert "project: initializedProject" in bootstrap_source
    assert "architecture_version:committedArchitectureVersion" in bootstrap_source
    assert "architecture_version: committedArchitectureVersion" in bootstrap_source
    assert "context: mutationContext" in bootstrap_source


def test_instruction_context_does_not_duplicate_raw_goal_into_event_payload():
    source = APP_MODULE.read_text(encoding="utf-8")
    start = source.index("function currentInstructionContext()")
    end = source.index("function updateInstructionContext()", start)
    context_source = source[start:end]
    assert "project_goal" not in context_source

def test_connected_mcp_large_results_are_bounded_and_recoverable_without_second_provider_call():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the WebMCP result-boundary check")
    script = r"""
const fs = require('fs');
let source = fs.readFileSync('frontend/web/archbro-webmcp.js', 'utf8')
  .replace(/^import .*?;\r?\n/, '')
  .replace(/\bexport\s+/g, '');
source += '\nglobalThis.__createArchBroTools = createArchBroTools;';
eval(source);
let providerCalls = 0;
const noop = async () => ({});
const bridge = {
  bootstrapProject: noop,
  expandArchitectureScope: noop,
  getDecisionContext: noop,
  submitAgentRecommendation: noop,
  createTask: noop,
  updateTaskStatus: noop,
  recordProjectObservation: noop,
  publishCodeArchitectureSnapshot: noop,
  callConnectedMcpTool: async ({arguments: args}) => {
    providerCalls += 1;
    if (args?.q === 'small') return {ok: true};
    return {items: Array.from({length: 40}, (_, i) => ({id: i, text: 'x'.repeat(250)}))};
  },
};
(async () => {
  const tool = globalThis.__createArchBroTools(bridge).find((item) => item.name === 'archbro_call_connected_mcp_tool');
  const small = JSON.parse(await tool.execute({server_id: 'github', tool_name: 'search', arguments: {q: 'small'}}));
  const first = JSON.parse(await tool.execute({server_id: 'github', tool_name: 'search', arguments: {q: 'archbro'}}));
  const second = JSON.parse(await tool.execute({
    server_id: 'github',
    tool_name: 'search',
    result_ref: first.full_result_ref,
    offset: 0,
    max_chars: 1000,
  }));
  process.stdout.write(JSON.stringify({small, first, second, providerCalls}));
})().catch((error) => { console.error(error); process.exit(1); });
"""
    completed = subprocess.run(
        [node, "-e", script],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    result = json.loads(completed.stdout)
    assert result["providerCalls"] == 2
    assert result["small"] == {"ok": True}
    assert result["first"]["schema"] == "archbro.bounded_result.v1"
    assert result["first"]["truncated"] is True
    assert result["first"]["full_result_ref"].startswith("webmcp-result:")
    assert result["first"]["result"]["items"]["shown"] == 20
    assert result["first"]["result"]["items"]["overflow_count"] == 20
    assert result["second"]["schema"] == "archbro.bounded_result_slice.v1"
    assert result["second"]["offset"] == 0
    assert result["second"]["next_offset"] == 1000
    assert len(result["second"]["content"]) == 1000
    assert result["second"]["complete"] is False
