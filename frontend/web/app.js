import {
  authenticationErrorMessage,
  createFirebaseEmailAccount,
  getFirebaseIdToken,
  restoreFirebaseIdentity,
  signInWithFirebaseEmail,
  signInWithGitHubAccount,
  signInWithGoogleAccount,
  signOutFromFirebase,
  usesFirebaseAuthentication,
} from './firebase-auth.js?v=854f576b09bf4d69';
import {
  bindProposalDecisionControls,
  formatOverviewAttentionLabel,
  formatTaskEnum,
  isProposalActionable,
  reconcileWorkspaceSelection,
  resolveTaskDependencies,
  taskInstructionContext,
} from './review-helpers.js?v=4430a4040cec2ad3';

import {createProjectRepositoryController} from './project-repository.js?v=4b7a639010850b3c';
let projectRepositoryController = null;

const prototype = window.ArchbroPrototype;
const RUNTIME_CONFIG = window.__ARCHBRO_RUNTIME_CONFIG__ || {};
const configuredArchitectureRequestTimeout = Number(RUNTIME_CONFIG.architecture_request_timeout_ms);
const ARCHITECTURE_REQUEST_TIMEOUT_MS = (
  Number.isFinite(configuredArchitectureRequestTimeout) && configuredArchitectureRequestTimeout > 0
)
  ? Math.max(60_000, configuredArchitectureRequestTimeout)
  : 930_000;
const URL_PARAMS = new URLSearchParams(window.location.search);
let ARCHITECTURE_CANVAS_MODE = URL_PARAMS.get('canvas') === 'architecture';
const REQUESTED_PROJECT_ID = String(URL_PARAMS.get('project') || '').trim() || null;
const INSPECTOR_TABS = new Set(['overview','dependencies','tasks','evidence','code','decisions']);
const persistedProjectId = localStorage.getItem('archbro-project-id');
const initialProjectId = REQUESTED_PROJECT_ID || persistedProjectId;
const WEBMCP_AGENT_MODE = new URLSearchParams(window.location.search).get('mode') === 'webmcp';
const AUTH_PROVIDER_SIGN_INS = new Map([
  ['google', signInWithGoogleAccount],
  ['github', signInWithGitHubAccount],
]);

function loadExpandedProjectIds(storage = localStorage) {
  try {
    const raw = storage.getItem('archbro-expanded-projects');
    const ids = raw ? JSON.parse(raw) : [];
    return new Set(Array.isArray(ids) ? ids.filter((id) => typeof id === 'string' && id.trim()) : []);
  } catch {
    return new Set();
  }
}

function persistExpandedProjectIds(storage = localStorage) {
  storage.setItem('archbro-expanded-projects', JSON.stringify([...state.expandedProjectIds]));
}

// WORKSPACE_ASYNC_STATE_START
function makeWorkspaceAsyncState(projectId = null) {
  const resource = () => ({requestGeneration:0,status:'idle',error:null,refreshing:false});
  return {
    generation:0,
    projectId:projectId || null,
    architectureVersion:null,
    phase:'idle',
    error:null,
    resources:{canvas:resource(),codeArchitecture:resource(),projectDiagram:resource()},
  };
}

function beginWorkspaceContext(asyncState, projectId) {
  const sameProject = asyncState.projectId === (projectId || null);
  asyncState.generation += 1;
  asyncState.projectId = projectId || null;
  asyncState.phase = 'loading';
  asyncState.error = null;
  if (!sameProject) asyncState.architectureVersion = null;
  for (const resource of Object.values(asyncState.resources)) {
    // Every new core-context request invalidates optional results launched by
    // an older context. Ready data may remain visible until the replacement
    // context has been committed, but an old request can never settle it.
    resource.requestGeneration += 1;
    resource.refreshing = false;
    if (!sameProject) {
      resource.status = 'idle';
      resource.error = null;
    }
  }
  return {generation:asyncState.generation,projectId:asyncState.projectId,architectureVersion:asyncState.architectureVersion};
}

function workspaceContextIsCurrent(asyncState, ticket) {
  return Boolean(ticket)
    && asyncState.generation === ticket.generation
    && asyncState.projectId === ticket.projectId;
}

function bindWorkspaceContextArchitecture(asyncState, ticket, architectureVersion) {
  if (!workspaceContextIsCurrent(asyncState, ticket)) return false;
  const version = Number(architectureVersion) || 0;
  asyncState.architectureVersion = version;
  asyncState.phase = 'ready';
  asyncState.error = null;
  ticket.architectureVersion = version;
  return true;
}

function failWorkspaceContext(asyncState, ticket, error) {
  if (!workspaceContextIsCurrent(asyncState, ticket)) return false;
  asyncState.phase = 'failed';
  asyncState.error = error?.message || String(error || 'Workspace could not be restored.');
  return true;
}

function currentWorkspaceContextTicket(asyncState) {
  return {
    generation:asyncState.generation,
    projectId:asyncState.projectId,
    architectureVersion:asyncState.architectureVersion,
  };
}

function beginWorkspaceResource(asyncState, name, contextTicket, {retainData = false} = {}) {
  const resource = asyncState.resources[name];
  if (!resource || !workspaceContextIsCurrent(asyncState, contextTicket)
      || Number(contextTicket.architectureVersion || 0) !== Number(asyncState.architectureVersion || 0)) return null;
  resource.requestGeneration += 1;
  const keepReady = Boolean(retainData && resource.status === 'ready');
  resource.refreshing = keepReady;
  resource.status = keepReady ? 'ready' : 'loading';
  resource.error = null;
  return {
    name,
    requestGeneration:resource.requestGeneration,
    contextGeneration:contextTicket.generation,
    projectId:contextTicket.projectId,
    architectureVersion:Number(contextTicket.architectureVersion || 0),
  };
}

function workspaceResourceIsCurrent(asyncState, request) {
  if (!request) return false;
  const resource = asyncState.resources[request.name];
  return Boolean(resource)
    && asyncState.generation === request.contextGeneration
    && asyncState.projectId === request.projectId
    && Number(asyncState.architectureVersion || 0) === Number(request.architectureVersion || 0)
    && resource.requestGeneration === request.requestGeneration;
}

function settleWorkspaceResource(asyncState, request, status, error = null) {
  if (!workspaceResourceIsCurrent(asyncState, request)) return false;
  const resource = asyncState.resources[request.name];
  resource.status = status;
  resource.error = error ? (error?.message || String(error)) : null;
  resource.refreshing = false;
  return true;
}
// WORKSPACE_ASYNC_STATE_END

const state = {
  projectId: initialProjectId,
  projects: [],
  project: null,
  tasks: [],
  architecture: null,
  diagram: null,
  diagramError: null,
  codeArchitecture: null,
  codeDiagram: null,
  architectureGraphKind: 'living',
  selectedCodeNodeId: null,
  scopeComponentId: null,
  readingMode: 'MAP',
  canvasInspectorOpen: false,
  selectedComponentId: null,
  selectedEdgeId: null,
  tracePathRequest: null,
  tracePathResult: null,
  tracePathLoading: false,
  tracePathError: null,
  tracePathRequestSerial: 0,
  inspectorTab: 'overview',
  canvasDeepLinkApplied: false,
  canvasDeepLinkFocusPending: false,
  graphFocusMode: 'all',
  collapsedNodeIds: new Set(),
  proposals: [],
  activity: [],
  lastRun: null,
  lastInstruction: '',
  instructionSubmission: null,
  plannerRecovery: null,
  agentContextManifest: null,
  agentContextKey: null,
  agentContextLoading: false,
  agentContextError: null,
  agentContextPromise: null,
  agentContextRequestSerial: 0,
  agentContextPolicy: 'ASK_ALL',
  agentContextTelemetryVisible: true,
  selectedTaskId: null,
  taskDetailId: null,
  taskDetailOrigin: null,
  taskActionNotice: null,
  selectedProposalId: null,
  proposalUpdating: new Set(),
  proposalDecisionNotice: null,
  notificationTransientMessage: null,
  proposalPreviews: new Map(),
  proposalPreviewSerial: 0,
  currentView: 'overview',
  projectContextRequestSerial: 0,
  workspaceTab: 'tasks',
  workspaceTabMemory: new Map(),
  workspaceTabScrollMemory: new Map(),
  taskUpdating: new Set(),
  workingRequests: new Map(),
  workingRequestSerial: 0,
  workingUiRequestId: null,
  architectureProgressRequestId: null,
  expandedProjectIds: loadExpandedProjectIds(),
  projectSnapshots: new Map(),
  workspaceAsync: makeWorkspaceAsyncState(initialProjectId),
  renamingProjectId: null,
  openProjectMenuId: null,
  projectMenuFocusId: null,
  onboarding: {
    active: !initialProjectId,
    stage: 'name',
    projectName: '',
    initialGoal: '',
    messages: [],
    draft: null,
    working: false,
    workingStartedAt: null,
    workingTimer: null,
    workingRequestId: null,
    lastError: null,
  },
  graphTransitionGeneration: 0,
  navigation: {
    generation: 0,
    initialized: false,
    committed: null,
    onboardingReturn: null,
  },
};

let workspaceContextGeneration = 0;

function beginWorkspaceContextRequest(projectId) {
  return {projectId, generation: ++workspaceContextGeneration};
}

function isWorkspaceContextRequestCurrent(request, {requireSelectedProject = false} = {}) {
  if (!request || request.generation !== workspaceContextGeneration) return false;
  return !requireSelectedProject || state.projectId === request.projectId;
}

function supersedeWorkspaceContextRequests() {
  workspaceContextGeneration += 1;
}

function commitWorkspaceContext(request, context, updates = {}, {requireSelectedProject = false} = {}) {
  if (!isWorkspaceContextRequestCurrent(request, {requireSelectedProject})) return false;
  Object.assign(state, context, updates);
  return true;
}

if (initialProjectId) state.expandedProjectIds.add(initialProjectId);

state.experience = {
  phase: 'restoring',
  authMode: 'signin',
  selectedLens: null,
  workspaceInitialized: false,
  workspaceError: null,
  dialogReturnFocus: new Map(),
  authClosingReturnFocus: true,
};

let activeMcpOAuthPopup = null;
let mcpOAuthStatusRequestId = 0;
const MCP_PROVIDER_STATUS_TTL_MS = 5000;
const mcpProviderStatusCache = new Map();
const handledMcpOAuthPopups = new WeakSet();
let mcpConnectionsSnapshot = [];
let mcpUiGeneration = 0;
let mcpConnectionsRequestSerial = 0;

const $ = (id) => document.getElementById(id);
const views = {
  overview: {title: 'Project Overview', subtitle: 'Keep project reality aligned with the accepted architecture.'},
  tasks: {title: 'Tasks', subtitle: 'Concrete, actionable work shared by humans and the agent.'},
  architecture: {title: 'Architecture', subtitle: 'Compare accepted design intent with revision-pinned implementation evidence.'},
};

const ROUTED_VIEWS = new Set(Object.keys(views));

function readNavigationRoute(locationLike = window.location, {useStorageFallback = false} = {}) {
  const params = new URLSearchParams(locationLike?.search || '');
  const explicitProject = params.has('project');
  let projectId = String(params.get('project') || '').trim() || null;
  if (!explicitProject && useStorageFallback) {
    projectId = String(localStorage.getItem('archbro-project-id') || '').trim() || null;
  }
  const canvas = params.get('canvas') === 'architecture';
  const requestedView = String(params.get('view') || '').trim().toLowerCase();
  const view = ROUTED_VIEWS.has(requestedView) ? requestedView : (canvas ? 'architecture' : 'overview');
  const nodeId = projectId && canvas ? (String(params.get('node') || '').trim() || null) : null;
  const requestedTab = String(params.get('tab') || '').trim().toLowerCase();
  const inspectorTab = nodeId && INSPECTOR_TABS.has(requestedTab) ? requestedTab : 'overview';
  let requestedWorkspaceTab = String(params.get('workspace') || '').trim().toLowerCase();
  if (!workspaceTabNames.includes(requestedWorkspaceTab) && useStorageFallback && projectId
      && !params.has('view') && !params.has('workspace')) {
    requestedWorkspaceTab = String(localStorage.getItem(`archbro-workspace-tab:${projectId}`) || '').trim().toLowerCase();
  }
  const workspaceTab = projectId && view === 'tasks' && workspaceTabNames.includes(requestedWorkspaceTab)
    ? requestedWorkspaceTab
    : 'tasks';
  return {projectId, explicitProject, view, canvas, nodeId, inspectorTab, workspaceTab};
}

function beginNavigationTransition(projectId = state.projectId, {invalidateGraph = true} = {}) {
  if (typeof projectRepositoryController !== 'undefined') projectRepositoryController?.close();
  state.navigation.generation += 1;
  if (invalidateGraph) state.graphTransitionGeneration += 1;
  // Advancing navigation immediately hides work owned by the previous project.
  // The request itself keeps its token and will retire only that token when it
  // eventually settles, so it cannot clear a newer project's indicator.
  if (typeof syncWorkingRequestUI === 'function') syncWorkingRequestUI();
  return {projectId: projectId || null, generation: state.navigation.generation};
}

function captureNavigationGuard(projectId = state.projectId) {
  return {projectId: projectId || null, generation: state.navigation.generation};
}

function navigationGenerationIsCurrent(guard) {
  return Boolean(guard) && guard.generation === state.navigation.generation;
}

function committedProjectGuardIsCurrent(guard) {
  return navigationGenerationIsCurrent(guard) && (guard.projectId || null) === (state.projectId || null);
}

function navigationSnapshotFromState(overrides = {}) {
  const projectId = Object.prototype.hasOwnProperty.call(overrides, 'projectId') ? overrides.projectId : state.projectId;
  const view = ROUTED_VIEWS.has(overrides.view) ? overrides.view : state.currentView;
  const canvas = Object.prototype.hasOwnProperty.call(overrides, 'canvas') ? Boolean(overrides.canvas) : ARCHITECTURE_CANVAS_MODE;
  const nodeId = Object.prototype.hasOwnProperty.call(overrides, 'nodeId') ? overrides.nodeId : state.selectedComponentId;
  const inspectorTab = Object.prototype.hasOwnProperty.call(overrides, 'inspectorTab') ? overrides.inspectorTab : state.inspectorTab;
  const requestedWorkspaceTab = Object.prototype.hasOwnProperty.call(overrides, 'workspaceTab') ? overrides.workspaceTab : state.workspaceTab;
  const workspaceTab = view === 'tasks' && workspaceTabNames.includes(requestedWorkspaceTab) ? requestedWorkspaceTab : 'tasks';
  return {
    projectId: projectId || null,
    view: ROUTED_VIEWS.has(view) ? view : 'overview',
    canvas,
    nodeId: projectId && canvas ? (nodeId || null) : null,
    inspectorTab: projectId && canvas && nodeId && INSPECTOR_TABS.has(inspectorTab) ? inspectorTab : 'overview',
    workspaceTab,
  };
}

function committedNavigationSnapshot() {
  const committed = state.navigation.committed;
  return {
    initialized: state.navigation.initialized,
    generation: state.navigation.generation,
    project_id: committed?.projectId || null,
    view: committed?.view || 'overview',
    canvas: Boolean(committed?.canvas),
    node_id: committed?.nodeId || null,
    inspector_tab: committed?.inspectorTab || 'overview',
    workspace_tab: committed?.workspaceTab || 'tasks',
  };
}

function writeNavigationUrl(snapshot, historyMode = 'replace') {
  if (typeof history === 'undefined' || historyMode === 'none') return;
  const url = new URL(window.location.href);
  if (snapshot.projectId) url.searchParams.set('project', snapshot.projectId);
  else url.searchParams.delete('project');
  if (snapshot.projectId && snapshot.view !== 'overview') url.searchParams.set('view', snapshot.view);
  else url.searchParams.delete('view');
  if (snapshot.canvas) url.searchParams.set('canvas', 'architecture');
  else url.searchParams.delete('canvas');
  if (snapshot.projectId && snapshot.canvas && snapshot.nodeId) url.searchParams.set('node', snapshot.nodeId);
  else url.searchParams.delete('node');
  if (snapshot.projectId && snapshot.canvas && snapshot.nodeId && snapshot.inspectorTab !== 'overview') url.searchParams.set('tab', snapshot.inspectorTab);
  else url.searchParams.delete('tab');
  if (snapshot.projectId && snapshot.view === 'tasks') url.searchParams.set('workspace', snapshot.workspaceTab);
  else url.searchParams.delete('workspace');
  const method = historyMode === 'push' ? 'pushState' : 'replaceState';
  history[method]({archbroNavigation: snapshot}, '', url.toString());
}

function commitNavigation(snapshot, {historyMode = 'replace', guard = null} = {}) {
  if (guard && !navigationGenerationIsCurrent(guard)) return false;
  const normalized = navigationSnapshotFromState(snapshot);
  state.navigation.committed = normalized;
  state.navigation.initialized = true;
  if (normalized.projectId) localStorage.setItem('archbro-project-id', normalized.projectId);
  else localStorage.removeItem('archbro-project-id');
  if (normalized.projectId && normalized.view === 'tasks') {
    localStorage.setItem(`archbro-workspace-tab:${normalized.projectId}`, normalized.workspaceTab);
    state.workspaceTabMemory.set(workspaceProjectKey(normalized.projectId), normalized.workspaceTab);
  }
  writeNavigationUrl(normalized, historyMode);
  return true;
}

function recommitCurrentNavigationAfterFailedTransition(guard, {historyMode = 'replace'} = {}) {
  if (!navigationGenerationIsCurrent(guard)) return false;
  const committed = state.navigation.committed || navigationSnapshotFromState();
  return commitNavigation(committed, {historyMode, guard});
}

function supersededNavigationError() {
  const error = new Error('Navigation changed before this request completed.');
  error.code = 'ARCHBRO_NAVIGATION_SUPERSEDED';
  return error;
}

function isSupersededNavigationError(error) {
  return error?.code === 'ARCHBRO_NAVIGATION_SUPERSEDED';
}

const architectureViewCache = new Map();

function normalizedArchitectureReadingMode(readingMode) {
  return ['MAP','READ','FULL'].includes(readingMode) ? readingMode : 'MAP';
}

function architectureViewCacheKey(surface, projectId, architectureVersion, scopeComponentId = null, readingMode = 'MAP') {
  const normalizedSurface = surface === 'canvas' ? 'canvas' : 'project';
  const scope = normalizedSurface === 'canvas' ? 'ROOT' : (scopeComponentId || 'ROOT');
  return `${projectId || ''}|${Number(architectureVersion) || 0}|${normalizedSurface}|${scope}|${normalizedArchitectureReadingMode(readingMode)}`;
}

const workspaceTabMeta = {
  tasks: {title: 'Tasks', subtitle: 'Concrete, actionable work shared by humans and the agent.'},
  review: {title: 'Architecture Review', subtitle: 'Review proposed architecture changes before they become the accepted design.'},
};
const workspaceTabNames = ['tasks', 'review'];

function workspaceProjectKey(projectId = state.projectId) {
  return projectId || '__no-project__';
}

function workspaceTabForProject(projectId = state.projectId) {
  const remembered = state.workspaceTabMemory.get(workspaceProjectKey(projectId));
  if (workspaceTabNames.includes(remembered)) return remembered;
  const persisted = projectId
    ? String(localStorage.getItem(`archbro-workspace-tab:${projectId}`) || '').trim().toLowerCase()
    : '';
  return workspaceTabNames.includes(persisted) ? persisted : 'tasks';
}

function workspaceTabScrollForProject(projectId = state.projectId) {
  const key = workspaceProjectKey(projectId);
  if (!state.workspaceTabScrollMemory.has(key)) state.workspaceTabScrollMemory.set(key, {tasks: 0, review: 0});
  return state.workspaceTabScrollMemory.get(key);
}

function workspaceScrollTop() {
  return Math.max(window.scrollY || 0, document.documentElement?.scrollTop || 0, $('workspaceMain')?.scrollTop || 0);
}

function rememberWorkspaceTabScroll(tabName = state.workspaceTab) {
  if (!workspaceTabNames.includes(tabName)) return;
  workspaceTabScrollForProject()[tabName] = workspaceScrollTop();
}

function restoreWorkspaceTabScroll(tabName = state.workspaceTab) {
  const scrollTop = workspaceTabScrollForProject()[tabName] || 0;
  requestAnimationFrame(() => {
    window.scrollTo(0, scrollTop);
    const workspaceMain = $('workspaceMain');
    if (workspaceMain) workspaceMain.scrollTop = scrollTop;
  });
}

function workspaceTabCount(tabName) {
  if (tabName === 'tasks') return state.tasks.length;
  return state.proposals.filter((proposal) => isProposalActionable(proposal, state.architecture)).length;
}

function applyWorkspaceTabInvariants(tabName, {focusedProposalId = null} = {}) {
  const next = reconcileWorkspaceSelection({
    tabName,
    tasks: state.tasks,
    proposals: state.proposals,
    architectureVersion: state.architecture?.version,
    selectedTaskId: state.selectedTaskId,
    taskDetailId: state.taskDetailId,
    taskDetailOrigin: state.taskDetailOrigin,
    selectedProposalId: state.selectedProposalId,
    focusedProposalId,
  });
  const clearedTaskContext = Boolean(
    (state.selectedTaskId || state.taskDetailId)
      && !next.selectedTaskId
      && !next.taskDetailId,
  );
  Object.assign(state, next);
  state.workspaceTab = tabName;
  if (clearedTaskContext || tabName === 'review') clearAgentContextPreview();
  return next;
}

function renderWorkspaceTabs() {
  const shell = $('workspaceTabs');
  if (!shell) return;
  const visible = Boolean(state.projectId && state.project) && state.currentView === 'tasks';
  const activeTab = workspaceTabNames.includes(state.workspaceTab) ? state.workspaceTab : 'tasks';
  shell.classList.toggle('hidden', !visible);
  workspaceTabNames.forEach((tabName) => {
    const button = $(`workspaceTab${tabName === 'tasks' ? 'Tasks' : 'Review'}`);
    const panel = $(`workspaceTab${tabName === 'tasks' ? 'Tasks' : 'Review'}Panel`);
    const count = $(`${tabName === 'tasks' ? 'task' : 'review'}TabCount`);
    if (!button || !panel) return;
    const selected = visible && activeTab === tabName;
    const total = workspaceTabCount(tabName);
    button.setAttribute('aria-selected', String(selected));
    button.setAttribute('aria-label', `${workspaceTabMeta[tabName].title} (${total})`);
    button.tabIndex = selected ? 0 : -1;
    panel.hidden = !selected;
    panel.inert = !selected;
    panel.classList.toggle('hidden', !selected);
    if (count) count.textContent = String(total);
  });
  if (visible) {
    $('pageTitle').textContent = workspaceTabMeta[activeTab].title;
    $('pageSubtitle').textContent = workspaceTabMeta[activeTab].subtitle;
  }
}

function switchWorkspaceTab(tabName, {
  restoreScroll = true,
  focus = false,
  focusedProposalId = null,
  historyMode = 'push',
  navigationGuard = null,
} = {}) {
  if (!workspaceTabNames.includes(tabName) || state.onboarding.active) return false;
  if (state.currentView !== 'tasks') {
    switchView('tasks', {workspaceTab: tabName, restoreScroll, historyMode, navigationGuard, focusedProposalId});
    if (focus) $(`workspaceTab${tabName === 'tasks' ? 'Tasks' : 'Review'}`)?.focus();
    return true;
  }
  const changed = state.workspaceTab !== tabName;
  const guard = navigationGuard || (changed
    ? beginNavigationTransition(state.projectId, {invalidateGraph:false})
    : captureNavigationGuard(state.projectId));
  if (!navigationGenerationIsCurrent(guard)) return false;
  rememberWorkspaceTabScroll(state.workspaceTab);
  applyWorkspaceTabInvariants(tabName, {focusedProposalId});
  if (!commitNavigation({
    projectId: state.projectId,
    view: 'tasks',
    canvas: ARCHITECTURE_CANVAS_MODE,
    nodeId: null,
    inspectorTab: 'overview',
    workspaceTab: tabName,
  }, {historyMode: changed ? historyMode : 'replace', guard})) return false;
  renderWorkspaceTabs();
  renderTaskDetails();
  renderProposals();
  updateInstructionContext();
  if (restoreScroll) restoreWorkspaceTabScroll(tabName);
  if (focus) $(`workspaceTab${tabName === 'tasks' ? 'Tasks' : 'Review'}`)?.focus();
  return true;
}

function handleWorkspaceTabKeydown(event) {
  if (!workspaceTabNames.includes(event.currentTarget.dataset.workspaceTab)) return;
  const currentIndex = workspaceTabNames.indexOf(event.currentTarget.dataset.workspaceTab);
  let nextIndex = null;
  if (event.key === 'ArrowRight') nextIndex = (currentIndex + 1) % workspaceTabNames.length;
  if (event.key === 'ArrowLeft') nextIndex = (currentIndex - 1 + workspaceTabNames.length) % workspaceTabNames.length;
  if (event.key === 'Home') nextIndex = 0;
  if (event.key === 'End') nextIndex = workspaceTabNames.length - 1;
  if (nextIndex !== null) {
    event.preventDefault();
    switchWorkspaceTab(workspaceTabNames[nextIndex], {focus: true});
    return;
  }
  if (event.key === 'Enter' || event.key === ' ') {
    event.preventDefault();
    switchWorkspaceTab(event.currentTarget.dataset.workspaceTab, {focus: true});
  }
}

function resetArchitectureViewCache(projectId, architectureVersion) {
  const version = Number(architectureVersion) || 0;
  for (const [key, entry] of architectureViewCache.entries()) {
    if (entry.projectId === projectId && entry.architectureVersion !== version) architectureViewCache.delete(key);
  }
  return architectureViewCache;
}

function invalidateArchitectureViewCache(projectId) {
  for (const [key, entry] of architectureViewCache.entries()) {
    if (entry.projectId === projectId) architectureViewCache.delete(key);
  }
  return architectureViewCache;
}

function cacheArchitectureView(surface, projectId, architectureVersion, scopeComponentId = null, readingMode = 'MAP', diagram = null) {
  // Compatibility with the pre-Task-4 four-argument form while callers migrate
  // to exact projection identity.
  if (diagram == null && scopeComponentId && typeof scopeComponentId === 'object') {
    diagram = scopeComponentId;
    scopeComponentId = null;
    readingMode = 'MAP';
  }
  if (!diagram || !['canvas','project'].includes(surface)) return diagram;
  const version = Number(architectureVersion) || 0;
  resetArchitectureViewCache(projectId, version);
  architectureViewCache.set(
    architectureViewCacheKey(surface, projectId, version, scopeComponentId, readingMode),
    {projectId, architectureVersion:version, surface, scopeComponentId:scopeComponentId || null, readingMode:normalizedArchitectureReadingMode(readingMode), diagram},
  );
  return diagram;
}

function cachedArchitectureView(surface, projectId, architectureVersion, scopeComponentId = null, readingMode = 'MAP') {
  return architectureViewCache.get(architectureViewCacheKey(surface, projectId, architectureVersion, scopeComponentId, readingMode))?.diagram || null;
}

function beginGraphTransition({projectId = state.projectId, architectureVersion = state.architecture?.version, surface = ARCHITECTURE_CANVAS_MODE ? 'canvas' : 'project', scopeComponentId = state.scopeComponentId, readingMode = state.readingMode} = {}) {
  return {
    generation: ++state.graphTransitionGeneration,
    projectId: projectId || null,
    architectureVersion: Number(architectureVersion) || 0,
    surface,
    scopeComponentId: scopeComponentId || null,
    readingMode: normalizedArchitectureReadingMode(readingMode),
  };
}

function graphTransitionIsCurrent(transition) {
  return Boolean(transition)
    && transition.generation === state.graphTransitionGeneration
    && (transition.projectId || null) === (state.projectId || null)
    && Number(transition.architectureVersion || 0) === Number(state.architecture?.version || 0);
}

function captureGraphProjectionGuard({projectId = state.projectId, architectureVersion = state.architecture?.version, surface = ARCHITECTURE_CANVAS_MODE ? 'canvas' : 'project', scopeComponentId = state.scopeComponentId, readingMode = state.readingMode} = {}) {
  return {
    generation: state.graphTransitionGeneration,
    projectId: projectId || null,
    architectureVersion: Number(architectureVersion) || 0,
    surface,
    scopeComponentId: surface === 'canvas' ? null : (scopeComponentId || null),
    readingMode: normalizedArchitectureReadingMode(readingMode),
  };
}

function graphProjectionMatchesCommittedState(guard) {
  if (!graphTransitionIsCurrent(guard)) return false;
  const surface = ARCHITECTURE_CANVAS_MODE ? 'canvas' : 'project';
  return guard.surface === surface
    && (guard.scopeComponentId || null) === (surface === 'canvas' ? null : (state.scopeComponentId || null))
    && guard.readingMode === normalizedArchitectureReadingMode(state.readingMode);
}

function captureGraphInteractionState() {
  return {
    selectedComponentId: state.selectedComponentId,
    selectedEdgeId: state.selectedEdgeId,
    inspectorTab: state.inspectorTab,
    graphFocusMode: state.graphFocusMode,
    tracePathRequest: state.tracePathRequest,
    tracePathResult: state.tracePathResult,
    tracePathLoading: state.tracePathLoading,
    tracePathError: state.tracePathError,
  };
}

function reconcileGraphInteractionState(diagram, previous = captureGraphInteractionState()) {
  const selectedComponentId = previous.selectedComponentId && (diagram?.nodes || []).some((node) => node.component_id === previous.selectedComponentId)
    ? previous.selectedComponentId
    : null;
  const selectedEdgeId = !selectedComponentId && previous.selectedEdgeId && (diagram?.edges || []).some((edge) => edge.id === previous.selectedEdgeId)
    ? previous.selectedEdgeId
    : null;
  state.selectedComponentId = selectedComponentId;
  state.selectedEdgeId = selectedEdgeId;
  state.inspectorTab = selectedComponentId || selectedEdgeId ? previous.inspectorTab : 'overview';
  state.graphFocusMode = selectedComponentId ? previous.graphFocusMode : 'all';
  const nodeIds = new Set((diagram?.nodes || []).map((node) => node.id));
  const trace = previous.tracePathRequest;
  const traceValid = Boolean(
    trace
    && Number(trace.expected_architecture_version) === Number(diagram?.architectureVersion || 0)
    && nodeIds.has(trace.source_id)
    && nodeIds.has(trace.target_id)
  );
  if (!traceValid) clearArchitectureTracePath({render:false});
  return {selectedComponentId, selectedEdgeId, traceValid};
}

function showExperience(phase) {
  state.experience.phase = phase;
  const bootstrapVisible = phase === 'restoring' || phase === 'failed';
  $('bootstrapExperience')?.classList.toggle('hidden', !bootstrapVisible);
  $('entryExperience').classList.toggle('hidden', phase === 'workspace' || bootstrapVisible);
  $('workspaceShell').classList.toggle('hidden', phase !== 'workspace');
  if (phase === 'restoring') {
    state.experience.workspaceError = null;
    if ($('bootstrapExperienceMessage')) $('bootstrapExperienceMessage').textContent = 'Restoring your workspace…';
    $('bootstrapRecoveryActions')?.classList.add('hidden');
  }
  for (const viewName of ['landing', 'auth', 'preference']) {
    if (viewName === 'auth') continue;
    const view = $(`${viewName}View`);
    if (!view) continue;
    const shouldHide = phase === 'auth' ? viewName !== 'landing' : phase !== viewName;
    view.classList.toggle('hidden', shouldHide);
  }
}

// WORKSPACE_RECOVERY_START
function showWorkspaceRecovery(error) {
  const message = error?.message || String(error || 'Workspace could not be restored.');
  state.experience.workspaceError = message;
  showExperience('failed');
  if ($('bootstrapExperienceMessage')) $('bootstrapExperienceMessage').textContent = `Workspace could not be restored. ${message}`;
  $('bootstrapRecoveryActions')?.classList.remove('hidden');
}
// WORKSPACE_RECOVERY_END

async function retryWorkspaceRestore() {
  const retryButton = $('bootstrapRetryBtn');
  if (retryButton?.disabled) return false;
  if (retryButton) retryButton.disabled = true;
  state.experience.workspaceInitialized = false;
  showExperience('restoring');
  try {
    const initialized = await initializeWorkspace();
    if (!initialized) {
      showWorkspaceRecovery(new Error(state.workspaceAsync.error || state.experience.workspaceError || 'Workspace could not be restored.'));
      return false;
    }
    state.experience.workspaceInitialized = true;
    showExperience('workspace');
    renderAccountIdentity();
    syncMobileSidebarLayers();
    return true;
  } finally {
    if (retryButton) retryButton.disabled = false;
  }
}

function authenticationBusy() {
  return $('authForm')?.getAttribute('aria-busy') === 'true';
}

function closeAuthentication({returnFocus = true, force = false} = {}) {
  if (authenticationBusy() && !force) return false;
  const authDialog = $('authView');
  const shouldReturnToLanding = returnFocus && state.experience.phase === 'auth';
  state.experience.authClosingReturnFocus = returnFocus;
  if (authDialog.open) authDialog.close();
  else if (returnFocus) $('landingAuthTeaser').focus();
  if (shouldReturnToLanding) showExperience('landing');
  return true;
}

function openAuthentication(trigger = document.activeElement) {
  setAuthMode('signin');
  showExperience('auth');
  showDialog('authView', trigger);
}

function setAuthMode(mode) {
  state.experience.authMode = mode;
  const signingUp = mode === 'signup';
  $('authForm').dataset.authMode = mode;
  $('authNameField').classList.toggle('hidden', !signingUp);
  $('authConfirmField').classList.toggle('hidden', !signingUp);
  $('authPassword').autocomplete = signingUp ? 'new-password' : 'current-password';
  $('authTitle').textContent = signingUp ? 'Create your account' : 'Welcome back';
  $('authSubtitle').textContent = signingUp
    ? (usesFirebaseAuthentication() ? 'Create your Archbro account with email and password.' : 'Create a local demo profile to continue building with Archbro.')
    : 'Sign in to continue building with Archbro.';
  $('authSubmitBtn').textContent = signingUp ? 'Create account' : 'Continue with email';
  $('authModeToggle').textContent = signingUp ? 'Already have an account? Log in' : 'New to Archbro? Create an account';
  writeAuthErrors({});
  writeAuthMessage('');
}

function togglePasswordVisibility(button) {
  const input = $(button.dataset.passwordTarget);
  if (!input) return;
  const showing = input.type === 'text';
  input.type = showing ? 'password' : 'text';
  button.textContent = showing ? 'Show' : 'Hide';
  button.setAttribute('aria-label', showing ? 'Show password' : 'Hide password');
}

function writeAuthErrors(errors) {
  for (const field of ['name', 'email', 'password', 'confirmPassword']) {
    const target = $(`auth${field[0].toUpperCase()}${field.slice(1)}Error`);
    if (!target) continue;
    const message = errors[field] || '';
    target.textContent = message;
    if (message) target.setAttribute('role', 'alert');
    else target.removeAttribute('role');
  }
}

function writeAuthMessage(message) {
  $('authFormMessage').textContent = message;
}

function setAuthenticationBusy(busy) {
  $('authForm').setAttribute('aria-busy', String(busy));
  $('authSubmitBtn').disabled = busy;
  $('authModeToggle').disabled = busy;
  $('authCloseBtn').disabled = busy;
  $('authBackBtn').disabled = busy;
  document.querySelectorAll('[data-auth-provider]').forEach((button) => { button.disabled = busy; });
  $('authSubmitBtn').textContent = busy
    ? (state.experience.authMode === 'signup' ? 'Creating account…' : 'Signing in…')
    : (state.experience.authMode === 'signup' ? 'Create account' : 'Continue with email');
}

async function routeAfterAuthentication() {
  const profile = prototype.currentProfile(localStorage);
  if (!profile) {
    state.experience.workspaceInitialized = false;
    closeAuthentication();
    showExperience('landing');
    toast('That session could not be restored. Sign in again to continue.', true);
    return false;
  }
  closeAuthentication({returnFocus: false, force: true});
  if (WEBMCP_AGENT_MODE) {
    await enterWorkspace();
    return true;
  }
  if (profile && !profile.onboardingComplete && $('preferenceView')) {
    showExperience('preference');
    return true;
  }
  await enterWorkspace();
  return true;
}

function selectProjectLens(lens) {
  state.experience.selectedLens = lens;
  document.querySelectorAll('[data-project-lens]').forEach((button) => {
    const selected = button.dataset.projectLens === lens;
    button.setAttribute('aria-checked', String(selected));
    button.tabIndex = selected ? 0 : -1;
    button.classList.toggle('selected', selected);
  });
  $('preferenceContinueBtn').disabled = !lens;
}

function wireLensRadioGroup(container, onSelect) {
  const options = [...container.querySelectorAll('[role="radio"]')];
  const checked = options.find((button) => button.getAttribute('aria-checked') === 'true');
  options.forEach((button, index) => { button.tabIndex = button === (checked || options[0]) ? 0 : -1; });
  const move = (current, offset) => options[(options.indexOf(current) + offset + options.length) % options.length];
  options.forEach((button) => {
    button.addEventListener('click', () => onSelect(button.dataset.projectLens || button.dataset.settingsLens));
    button.addEventListener('keydown', (event) => {
      let next = null;
      if (event.key === 'ArrowRight' || event.key === 'ArrowDown') next = move(button, 1);
      if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') next = move(button, -1);
      if (event.key === 'Home') next = options[0];
      if (event.key === 'End') next = options.at(-1);
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        onSelect(button.dataset.projectLens || button.dataset.settingsLens);
        return;
      }
      if (!next) return;
      event.preventDefault();
      onSelect(next.dataset.projectLens || next.dataset.settingsLens);
      next.focus();
    });
  });
}

async function completePreference() {
  if (!state.experience.selectedLens) return;
  prototype.updateCurrentProfile(localStorage, {
    onboardingComplete: true,
    defaultLens: state.experience.selectedLens,
  });
  await enterWorkspace();
}

async function submitAuthentication(event) {
  event.preventDefault();
  const signingUp = state.experience.authMode === 'signup';
  const values = {
    name: $('authName').value,
    email: $('authEmail').value,
    password: $('authPassword').value,
    confirmPassword: $('authConfirmPassword').value,
  };
  const errors = signingUp ? prototype.validateSignUp(values) : prototype.validateSignIn(values);
  writeAuthErrors(errors);
  writeAuthMessage('');
  if (Object.keys(errors).length) return;
  setAuthenticationBusy(true);
  try {
    if (usesFirebaseAuthentication()) {
      const identity = signingUp
        ? await createFirebaseEmailAccount(values)
        : await signInWithFirebaseEmail(values);
      prototype.startSession(localStorage, identity);
      if (identity.profileSynced === false) {
        toast('Your account was created. Your display name is saved in this browser.', false);
      }
    } else {
      prototype.startSession(localStorage, {provider: 'password', email: values.email, name: values.name});
    }
    $('authPassword').value = '';
    $('authConfirmPassword').value = '';
    await routeAfterAuthentication();
  } catch (error) {
    writeAuthMessage(authenticationErrorMessage(error));
  } finally {
    setAuthenticationBusy(false);
  }
}

async function enterWorkspace() {
  if (!state.experience.workspaceInitialized) {
    showExperience('restoring');
    const initialized = await initializeWorkspace();
    if (!initialized) {
      state.experience.workspaceInitialized = false;
      showWorkspaceRecovery(new Error(state.workspaceAsync.error || 'Workspace could not be restored.'));
      return false;
    }
    state.experience.workspaceInitialized = true;
  }
  showExperience('workspace');
  renderAccountIdentity();
  return true;
}

async function api(path, options = {}) {
  const {timeoutMs = 0, headers = {}, ...fetchOptions} = options;
  const controller = timeoutMs ? new AbortController() : null;
  const timeout = controller ? setTimeout(() => controller.abort(), timeoutMs) : null;
  try {
    const token = await getFirebaseIdToken();
    const res = await fetch(path, {
      headers: {
        'Content-Type': 'application/json',
        ...(token ? {Authorization: `Bearer ${token}`} : {}),
        ...headers,
      },
      ...fetchOptions,
      ...(controller ? {signal: controller.signal} : {}),
    });
    if (!res.ok) {
      let detail = 'Request failed';
      try {
        const body = await res.json();
        const rawDetail = body.detail ?? body;
        detail = typeof rawDetail === 'string' ? rawDetail : JSON.stringify(rawDetail);
      } catch {}
      throw new Error(`${res.status}: ${detail}`);
    }
    return res.status === 204 ? null : res.json();
  } catch (err) {
    if (err?.name === 'AbortError') {
      throw new Error(`Request timed out after ${Math.round(timeoutMs / 1000)}s. Your Goal and Ask are preserved; retry when ready.`);
    }
    throw err;
  } finally {
    if (timeout) clearTimeout(timeout);
  }
}

function toast(message, error = false) {
  const el = $('toast');
  el.textContent = message;
  el.classList.toggle('error', error);
  el.classList.remove('hidden');
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => el.classList.add('hidden'), 4500);
}

function setWorking(working, detail = '') {
  const el = $('agentStatus');
  if (!el) return;
  el.classList.toggle('working', working);
  el.innerHTML = `<span class="pulse"></span>${working ? (detail || 'Agent working…') : 'Agent ready'}`;
}

// WORKING_REQUEST_LOGIC_START
function workingRequestsForCurrentNavigation() {
  return [...state.workingRequests.values()]
    .filter((request) => (
      request.generation === state.navigation.generation
      && (request.projectId || null) === (state.projectId || null)
    ))
    .sort((left, right) => left.serial - right.serial);
}

function syncWorkingRequestUI() {
  const visible = workingRequestsForCurrentNavigation();
  const current = visible.at(-1) || null;
  state.workingUiRequestId = current?.id || null;
  setWorking(Boolean(current), current?.detail || '');

  const architectureRequest = [...visible].reverse().find((request) => request.architectureStartedAt) || null;
  const architectureRequestId = architectureRequest?.id || null;
  if (state.architectureProgressRequestId !== architectureRequestId) {
    state.architectureProgressRequestId = architectureRequestId;
    setArchitectureProgress(Boolean(architectureRequest), architectureRequest?.architectureStartedAt || 0);
  }
  return current;
}

function beginWorkingRequest(detail = '', {projectId = state.projectId, architectureStartedAt = null} = {}) {
  const serial = ++state.workingRequestSerial;
  const id = `working-request-${serial}`;
  state.workingRequests.set(id, {
    id,
    serial,
    detail,
    projectId: projectId || null,
    generation: state.navigation.generation,
    architectureStartedAt: Number(architectureStartedAt) || null,
  });
  syncWorkingRequestUI();
  return id;
}

function updateWorkingRequest(id, detail) {
  const request = state.workingRequests.get(id);
  if (!request) return false;
  state.workingRequests.set(id, {...request, detail});
  syncWorkingRequestUI();
  return true;
}

function finishWorkingRequest(id) {
  if (!id) return false;
  const removed = state.workingRequests.delete(id);
  syncWorkingRequestUI();
  return removed;
}
// WORKING_REQUEST_LOGIC_END

function escapeHtml(value = '') {
  return String(value).replace(/[&<>'"]/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
}

function statusClass(status) {
  return status === 'IN_PROGRESS' ? 'progress' : status.toLowerCase();
}

async function loadProjects({guard = null} = {}) {
  const projects = await api('/projects');
  if (guard && !navigationGenerationIsCurrent(guard)) return null;
  state.projects = projects;
  renderProjectTree();
  return state.projects;
}

async function loadProjectSnapshots({guard = null} = {}) {
  const snapshots = new Map();
  await Promise.all(state.projects.map(async (project) => {
    try {
      const architecture = await api(`/projects/${project.id}/architecture`);
      let rootDiagram = null;
      if (Number(architecture?.version || 0) > 0 && architecture?.components?.length) {
        try { rootDiagram = await loadArchitectureDiagram(project.id, architecture, null); } catch { rootDiagram = null; }
      }
      snapshots.set(project.id, {architecture, rootDiagram});
    } catch { snapshots.set(project.id, null); }
  }));
  if (guard && !navigationGenerationIsCurrent(guard)) return null;
  state.projectSnapshots = snapshots;
  return snapshots;
}

function graphPathData(points = [], radius = 8) {
  if (!points.length) return '';
  if (points.length < 3 || radius <= 0) {
    return points.map((point, index) => `${index === 0 ? 'M' : 'L'} ${point.x} ${point.y}`).join(' ');
  }
  const commands = [`M ${points[0].x} ${points[0].y}`];
  for (let index = 1; index < points.length - 1; index += 1) {
    const previous = points[index - 1];
    const current = points[index];
    const next = points[index + 1];
    const previousLength = Math.hypot(current.x - previous.x, current.y - previous.y);
    const nextLength = Math.hypot(next.x - current.x, next.y - current.y);
    const cornerRadius = Math.min(radius, previousLength / 2, nextLength / 2);
    if (cornerRadius < 1) {
      commands.push(`L ${current.x} ${current.y}`);
      continue;
    }
    const before = {
      x: current.x - ((current.x - previous.x) / previousLength) * cornerRadius,
      y: current.y - ((current.y - previous.y) / previousLength) * cornerRadius,
    };
    const after = {
      x: current.x + ((next.x - current.x) / nextLength) * cornerRadius,
      y: current.y + ((next.y - current.y) / nextLength) * cornerRadius,
    };
    commands.push(`L ${before.x} ${before.y}`);
    commands.push(`Q ${current.x} ${current.y} ${after.x} ${after.y}`);
  }
  const end = points[points.length - 1];
  commands.push(`L ${end.x} ${end.y}`);
  return commands.join(' ');
}

function renderArchitectureSnapshot(snapshot) {
  const architecture = snapshot?.architecture || null;
  const graph = snapshot?.rootDiagram || null;
  if (!architecture || Number(architecture.version || 0) < 1 || !graph?.nodes?.length) {
    return '<div class="project-snapshot project-snapshot-pending"><span class="snapshot-icon" aria-hidden="true">⌁</span><strong>Architecture snapshot pending</strong><small>Generate Living Architecture to see the system map here.</small></div>';
  }
  const edges = graph.edges.map((edge) => `<path data-snapshot-edge="${escapeHtml(edge.id)}" d="${graphPathData(edge.points)}"/>`).join('');
  const nodes = graph.nodes.map((node) => `<g class="snapshot-node" data-snapshot-node="${escapeHtml(node.id)}"><rect x="${node.x}" y="${node.y}" width="${node.width}" height="${node.height}" rx="12"/><text x="${node.x + node.width / 2}" y="${node.y + node.height / 2 + 3}" text-anchor="middle">${escapeHtml(node.label || node.component_id || 'Area')}</text></g>`).join('');
  return `<div class="project-snapshot project-snapshot-architecture" role="img" aria-label="Living Architecture root snapshot version ${escapeHtml(architecture.version)}"><span class="snapshot-label">LIVING ARCHITECTURE · v${escapeHtml(architecture.version)}</span><svg viewBox="0 0 ${graph.width} ${graph.height}" preserveAspectRatio="xMidYMid meet" aria-hidden="true"><g class="snapshot-edges">${edges}</g>${nodes}</svg></div>`;
}


function goalExcerpt(goal = '', maxLength = 180) {
  const normalized = String(goal || '').replace(/\s+/g, ' ').trim();
  if (normalized.length <= maxLength) return normalized;
  const clipped = normalized.slice(0, maxLength + 1).replace(/\s+\S*$/, '').trim();
  return `${clipped || normalized.slice(0, maxLength).trim()}…`;
}

function overviewArchitectureState({architecture = {}, pending = [], diagram = null, diagramError = '', resource = {}} = {}) {
  if (Number(architecture.version || 0) < 1 || !architecture.components?.length) {
    return {key: 'initial', label: 'AWAITING ARCHITECTURE', summaryLabel: 'Architecture pending'};
  }
  if (pending.length) return {key: 'review', label: 'NEEDS REVIEW', summaryLabel: 'Needs review'};
  if (resource.status === 'loading') return {key: 'loading', label: 'LOADING MAP', summaryLabel: 'Loading map'};
  if (resource.status === 'error' || diagramError) return {key: 'unavailable', label: 'MAP UNAVAILABLE', summaryLabel: 'Map unavailable'};
  if (!diagram?.nodes?.length) return {key: 'unavailable', label: 'MAP UNAVAILABLE', summaryLabel: 'Map unavailable'};
  return {key: 'aligned', label: 'ALIGNED', summaryLabel: 'Aligned'};
}

function renderOverviewArchitectureMap(diagram, architecture, {pending = [], diagramError = '', resource = {}} = {}) {
  const architectureState = overviewArchitectureState({architecture, pending, diagram, diagramError, resource});
  if (architectureState.key === 'loading') {
    return '<div class="overview-map-state" role="status"><strong>Loading the living architecture map…</strong><p>The accepted architecture is ready; its positioned overview is loading.</p></div>';
  }
  if (architectureState.key === 'unavailable') {
    return `<div class="overview-map-state" role="status"><strong>Living architecture map unavailable</strong><p>${escapeHtml(diagramError || 'Open Living Architecture to retry the positioned map.')}</p><button class="link-btn" data-go="architecture" type="button">Open Living Architecture ↗</button></div>`;
  }
  if (architectureState.key === 'initial') {
    return '<div class="overview-map-state"><strong>Living architecture is not available yet.</strong><p>Generate Architecture v1 to see its accepted map.</p></div>';
  }
  if (!diagram?.nodes?.length) {
    return '<div class="overview-map-state"><strong>No map projection is available.</strong><p>Open Living Architecture to inspect the complete system.</p><button class="link-btn" data-go="architecture" type="button">Open Living Architecture ↗</button></div>';
  }
  return `<div class="overview-map-render overview-snapshot">${renderArchitectureSnapshot({architecture, rootDiagram: diagram})}</div>`;
}

function projectCardStatus(project, architecture) {
  if (architecture?.version > 0) return {label: 'ACTIVE', className: 'active'};
  if (project.status === 'DONE') return {label: 'DONE', className: 'done'};
  return {label: 'DRAFT', className: 'draft'};
}

function renderProjectCards() {
  const cards = $('projectCards');
  if (!cards) return;
  cards.innerHTML = state.projects.map((project) => {
    const snapshot = state.projectSnapshots.get(project.id);
    const architecture = snapshot?.architecture || null;
    const status = projectCardStatus(project, architecture);
    const snapshotMeta = architecture?.version > 0
      ? `Architecture v${architecture.version} · click to open Living Graph`
      : 'Goal saved · architecture pending';
    return `<article class="project-card" data-project-card="${escapeHtml(project.id)}"><button class="project-card-open" type="button" data-project-card-open="${escapeHtml(project.id)}"><div class="project-card-preview">${renderArchitectureSnapshot(snapshot)}</div><div class="project-card-body"><div class="project-card-heading"><strong>${escapeHtml(project.name)}</strong><span class="project-card-status ${status.className}">${status.label}</span></div><p>${escapeHtml(snapshotMeta)}</p><span class="project-card-link">Open project <span aria-hidden="true">→</span></span></div></button></article>`;
  }).join('');
  cards.querySelectorAll('[data-project-card-open]').forEach((button) => button.addEventListener('click', async () => {
    if (await selectProject(button.dataset.projectCardOpen)) closeMobileSidebar();
  }));
}

function renderWorkspaceHome() {
  state.onboarding.active = false;
  syncDocumentTitle();
  $('emptyState').classList.add('hidden');
  $('workspace').classList.remove('hidden');
  $('workspaceHome').classList.remove('hidden');
  $('workspaceSwitcherBtn').setAttribute('aria-current', 'page');
  document.querySelectorAll('#workspace .view').forEach((view) => view.classList.remove('active'));
  renderWorkspaceTabs();
  renderTaskDetails();
  $('globalAgentDock').classList.add('hidden');
  $('pageTitle').textContent = 'Project workspace';
  $('pageSubtitle').textContent = 'Browse your projects and open one when you are ready.';
  $('workspaceHomeCount').textContent = `${state.projects.length} project${state.projects.length === 1 ? '' : 's'}`;
  $('workspaceHomeEmpty').classList.toggle('hidden', state.projects.length > 0);
  $('projectCards').classList.toggle('hidden', state.projects.length === 0);
  renderProjectTree();
  renderProjectCards();
  renderNotifications();
  renderAccountIdentity();
}

async function openPersonalWorkspace({historyMode = 'push', navigationGuard = null} = {}) {
  const guard = navigationGuard || beginNavigationTransition(null);
  if (!navigationGenerationIsCurrent(guard)) return false;
  supersedeWorkspaceContextRequests();
  beginWorkspaceContext(state.workspaceAsync, null);
  state.workspaceAsync.phase = 'ready';
  state.workspaceAsync.architectureVersion = 0;
  state.projectId = null;
  state.project = null;
  state.tasks = [];
  state.architecture = null;
  state.diagram = null;
  state.diagramError = null;
  state.codeArchitecture = null;
  state.codeDiagram = null;
  state.architectureGraphKind = 'living';
  state.selectedCodeNodeId = null;
  state.graphFocusMode = 'all';
  state.proposals = [];
  state.lastRun = null;
  state.lastInstruction = '';
  $('instruction').value = '';
  syncInstructionTextareaRows();
  state.plannerRecovery = null;
  clearAgentContextPreview();
  state.selectedComponentId = null;
  state.selectedEdgeId = null;
  state.inspectorTab = 'overview';
  state.canvasDeepLinkApplied = false;
  state.canvasDeepLinkFocusPending = false;
  state.collapsedNodeIds.clear();
  state.scopeComponentId = null;
  state.readingMode = 'MAP';
  state.selectedTaskId = null;
  state.taskDetailId = null;
  state.taskDetailOrigin = null;
  state.selectedProposalId = null;
  state.currentView = 'overview';
  state.openProjectMenuId = null;
  state.renamingProjectId = null;
  ARCHITECTURE_CANVAS_MODE = false;
  syncArchitectureCanvasDomMode();
  if (!commitNavigation({projectId:null, view:'overview', canvas:false, nodeId:null, inspectorTab:'overview'}, {historyMode, guard})) return false;
  await loadProjectSnapshots({guard});
  if (!navigationGenerationIsCurrent(guard)) return false;
  renderWorkspaceHome();
  closeMobileSidebar();
  return true;
}

function renderProjectTree() {
  const tree = $('projectTree');
  if (!tree) return;
  const nodes = state.projects.map((project) => {
    const expanded = state.expandedProjectIds.has(project.id);
    const current = project.id === state.projectId;
    const childrenId = `projectChildren-${project.id}`;
    const menuId = `projectMenu-${project.id}`;
    const menuOpen = state.openProjectMenuId === project.id;
    if (state.renamingProjectId === project.id) {
      return `<li class="project-node" data-project-id="${escapeHtml(project.id)}"><form class="project-rename-form" data-project-rename-form="${escapeHtml(project.id)}"><label class="sr-only" for="projectRenameInput">Project name</label><input id="projectRenameInput" data-project-rename-input value="${escapeHtml(project.name)}" /><button type="submit">Save</button><button type="button" data-project-rename-cancel>Cancel</button><span class="field-error" data-project-rename-error></span></form></li>`;
    }
    const children = `<ul id="${escapeHtml(childrenId)}" class="project-children"${expanded ? '' : ' hidden'}><li><button class="${current && state.currentView === 'overview' ? 'active' : ''}" data-project-view="overview"${current && state.currentView === 'overview' ? ' aria-current="page"' : ''}>Project Overview</button></li><li><button class="${current && state.currentView === 'architecture' ? 'active' : ''}" data-project-view="architecture"${current && state.currentView === 'architecture' ? ' aria-current="page"' : ''}>Living Graph</button></li><li><button class="${current && state.currentView === 'tasks' ? 'active' : ''}" data-project-view="tasks"${current && state.currentView === 'tasks' ? ' aria-current="page"' : ''}>Tasks</button></li></ul>`;
    const menu = menuOpen
      ? `<div id="${escapeHtml(menuId)}" class="project-row-menu" data-project-menu-panel role="menu"><button type="button" role="menuitem" data-project-action="edit">Edit project</button><button type="button" role="menuitem" data-project-action="repository">GitHub repository</button><button type="button" role="menuitem" data-project-action="rename">Rename project</button><button type="button" role="menuitem" data-project-action="delete">Delete project</button></div>`
      : '';
    return `<li class="project-node${current ? ' current' : ''}" data-project-id="${escapeHtml(project.id)}"><div class="project-row"><button class="project-toggle" type="button" data-project-toggle aria-label="${expanded ? 'Collapse' : 'Expand'} ${escapeHtml(project.name)}" aria-expanded="${expanded}" aria-controls="${escapeHtml(childrenId)}">${expanded ? '⌄' : '›'}</button><button class="project-name" type="button" data-project-open aria-pressed="${current}"${current ? ' aria-current="location"' : ''}>${escapeHtml(project.name)}</button><div class="project-menu-wrap"><button class="project-menu-trigger" type="button" data-project-menu aria-label="Project actions for ${escapeHtml(project.name)}" aria-haspopup="menu" aria-expanded="${menuOpen}" aria-controls="${escapeHtml(menuId)}">⋯</button>${menu}</div></div>${children}</li>`;
  }).join('');
  tree.innerHTML = nodes ? `<ul class="project-list">${nodes}</ul>` : '<p class="project-tree-empty">No projects yet.</p>';
  wireProjectTree();
  restoreProjectMenuFocus();
}

function queueProjectMenuFocus(projectId) {
  state.projectMenuFocusId = projectId;
}

function clearProjectMenuFocusQueue() {
  state.projectMenuFocusId = null;
}

function restoreProjectMenuFocus() {
  if (!state.projectMenuFocusId) return;
  const projectId = state.projectMenuFocusId;
  state.projectMenuFocusId = null;
  setTimeout(() => document.querySelector(`[data-project-id="${CSS.escape(projectId)}"] [data-project-menu]`)?.focus(), 0);
}

function projectMenuTrigger(projectId) {
  return document.querySelector(`[data-project-id="${CSS.escape(projectId)}"] [data-project-menu]`);
}

function closeProjectMenu({returnFocus = false} = {}) {
  if (!state.openProjectMenuId) return;
  const projectId = state.openProjectMenuId;
  state.openProjectMenuId = null;
  if (returnFocus) queueProjectMenuFocus(projectId);
  else clearProjectMenuFocusQueue();
  renderProjectTree();
}

function closeProjectMenuAfterPointerEvent({returnFocus = false} = {}) {
  if (!state.openProjectMenuId) return;
  const projectId = state.openProjectMenuId;
  if (returnFocus) queueProjectMenuFocus(projectId);
  else clearProjectMenuFocusQueue();
  setTimeout(() => {
    if (state.openProjectMenuId !== projectId) return;
    state.openProjectMenuId = null;
    renderProjectTree();
  }, 0);
}

function toggleProjectMenu(projectId) {
  if (state.renamingProjectId) return;
  const opening = state.openProjectMenuId !== projectId;
  state.openProjectMenuId = opening ? projectId : null;
  if (!opening) queueProjectMenuFocus(projectId);
  renderProjectTree();
  if (opening) setTimeout(() => document.querySelector(`[data-project-id="${CSS.escape(projectId)}"] [data-project-action]`)?.focus(), 0);
}

function toggleProjectExpanded(projectId) {
  const activeNode = document.activeElement?.closest?.('[data-project-id]');
  const restoreToggleFocus = Boolean(
    document.activeElement?.hasAttribute?.('data-project-toggle')
    && activeNode?.dataset.projectId === projectId
  );
  if (state.expandedProjectIds.has(projectId)) state.expandedProjectIds.delete(projectId);
  else state.expandedProjectIds.add(projectId);
  persistExpandedProjectIds();
  renderProjectTree();
  if (!restoreToggleFocus) return;
  [...document.querySelectorAll('[data-project-id]')]
    .find((node) => node.dataset.projectId === projectId)
    ?.querySelector('[data-project-toggle]')?.focus({preventScroll:true});
}

function mobileSidebarEnabled() {
  return window.matchMedia('(max-width: 760px)').matches;
}

function syncMobileSidebarLayers() {
  const mobile = mobileSidebarEnabled();
  const open = mobile && document.body.classList.contains('sidebar-open');
  $('workspaceSidebar').inert = mobile && !open;
  if (mobile && !open) $('workspaceSidebar').setAttribute('aria-hidden', 'true');
  else $('workspaceSidebar').removeAttribute('aria-hidden');
  $('workspaceMain').inert = open;
  if (!mobile) {
    document.body.classList.remove('sidebar-open');
    $('sidebarBackdrop').classList.add('hidden');
    $('mobileSidebarBtn').setAttribute('aria-expanded', 'false');
  }
}

function openMobileSidebar() {
  if (!mobileSidebarEnabled()) return;
  document.body.classList.add('sidebar-open');
  $('sidebarBackdrop').classList.remove('hidden');
  $('mobileSidebarBtn').setAttribute('aria-expanded', 'true');
  syncMobileSidebarLayers();
  setTimeout(() => {
    if (!mobileSidebarEnabled() || !document.body.classList.contains('sidebar-open')) return;
    // Do not override a keyboard/pointer focus already chosen inside the sidebar.
    if ($('workspaceSidebar').contains(document.activeElement)) return;
    ($('newProjectBtn') || document.querySelector('[data-project-open]'))?.focus();
  }, 0);
}

function closeMobileSidebar({returnFocus = true} = {}) {
  const wasOpen = mobileSidebarEnabled() && document.body.classList.contains('sidebar-open');
  document.body.classList.remove('sidebar-open');
  $('sidebarBackdrop').classList.add('hidden');
  $('mobileSidebarBtn').setAttribute('aria-expanded', 'false');
  syncMobileSidebarLayers();
  if (wasOpen && returnFocus) $('mobileSidebarBtn').focus();
}

function beginInlineRename(projectId) {
  state.openProjectMenuId = null;
  state.renamingProjectId = projectId;
  renderProjectTree();
  document.querySelector('[data-project-rename-input]')?.select();
}

async function commitInlineRename(projectId) {
  const input = document.querySelector('[data-project-rename-input]');
  const name = input?.value.trim() || '';
  const error = document.querySelector('[data-project-rename-error]');
  if (!name) {
    if (error) {
      error.textContent = 'Enter a project name.';
      error.setAttribute('role', 'alert');
    }
    return;
  }
  try {
    await api(`/projects/${projectId}`, {method: 'PATCH', body: JSON.stringify({name})});
    const project = state.projects.find((item) => item.id === projectId);
    if (project) project.name = name;
    if (projectId === state.projectId && state.project) {
      state.project = {...state.project, name};
      $('welcomeTitle').textContent = name;
    }
    state.renamingProjectId = null;
    queueProjectMenuFocus(projectId);
    await loadProjects();
    if (projectId === state.projectId) await refresh();
    toast('Project renamed.');
  } catch (err) {
    if (error) {
      error.textContent = err.message;
      error.setAttribute('role', 'alert');
    }
  }
}

async function activateProjectView(projectId, view, {historyMode = 'push'} = {}) {
  if (!projectId || !ROUTED_VIEWS.has(view)) return false;
  if (projectId !== state.projectId || !state.project) {
    return selectProject(projectId, {view, historyMode,
      ...(view === 'tasks' ? {route:{workspaceTab:'tasks'}} : {})});
  }
  return switchView(view, {historyMode, ...(view === 'tasks' ? {workspaceTab:'tasks'} : {})});
}

function wireProjectTree() {
  const cancelInlineRename = (projectId) => {
    state.renamingProjectId = null;
    queueProjectMenuFocus(projectId);
    renderProjectTree();
  };
  document.querySelectorAll('[data-project-id]').forEach((node) => {
    const projectId = node.dataset.projectId;
    node.querySelector('[data-project-toggle]')?.addEventListener('click', () => toggleProjectExpanded(projectId));
    node.querySelector('[data-project-open]')?.addEventListener('click', async () => {
      if (await activateProjectView(projectId, 'overview')) closeMobileSidebar();
    });
    node.querySelector('[data-project-menu]')?.addEventListener('click', (event) => {
      event.stopPropagation();
      toggleProjectMenu(projectId);
    });
    node.querySelector('[data-project-menu]')?.addEventListener('keydown', (event) => {
      if (!['ArrowDown', 'Enter', ' '].includes(event.key)) return;
      event.preventDefault();
      if (state.openProjectMenuId !== projectId) toggleProjectMenu(projectId);
    });
    node.querySelectorAll('[data-project-action]').forEach((button) => button.addEventListener('click', async () => {
      const action = button.dataset.projectAction;
      const menuTrigger = projectMenuTrigger(projectId);
      closeProjectMenu({returnFocus: false});
      if (action === 'rename') {
        beginInlineRename(projectId);
        return;
      }
      if (projectId !== state.projectId && !(await selectProject(projectId))) return;
      const dialogTrigger = projectMenuTrigger(projectId) || menuTrigger;
      if (action === 'edit') openEditProject(dialogTrigger);
      if (action === 'repository') void projectRepositoryController.open(state.project, dialogTrigger);
      if (action === 'delete') openDeleteProject(dialogTrigger);
    }));
    node.querySelector('[data-project-menu-panel]')?.addEventListener('keydown', (event) => {
      const items = [...node.querySelectorAll('[data-project-action]')];
      const current = items.indexOf(document.activeElement);
      let target = null;
      if (event.key === 'ArrowDown') target = items[(current + 1 + items.length) % items.length];
      if (event.key === 'ArrowUp') target = items[(current - 1 + items.length) % items.length];
      if (event.key === 'Home') target = items[0];
      if (event.key === 'End') target = items.at(-1);
      if (event.key === 'Escape') {
        event.preventDefault();
        event.stopPropagation();
        closeProjectMenu({returnFocus: true});
        return;
      }
      if (event.key === 'Tab') {
        closeProjectMenu({returnFocus: false});
        return;
      }
      if (!target) return;
      event.preventDefault();
      target.focus();
    });
    node.querySelector('[data-project-rename-cancel]')?.addEventListener('click', () => cancelInlineRename(projectId));
    node.querySelector('[data-project-rename-form]')?.addEventListener('submit', async (event) => {
      event.preventDefault();
      await commitInlineRename(projectId);
    });
    node.querySelector('[data-project-rename-input]')?.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        event.stopPropagation();
        cancelInlineRename(projectId);
      }
    });
    node.querySelectorAll('[data-project-view]').forEach((button) => button.addEventListener('click', async () => {
      if (!(await activateProjectView(projectId, button.dataset.projectView))) return;
      closeMobileSidebar();
    }));
  });
}

function normalizeDiagramGraph(payload) {
  const diagram = payload?.diagram || payload?.diagram_view || payload?.diagramView;
  const layout = payload?.positioned_graph || payload?.positionedGraph || payload?.layout;
  if (!diagram || !layout) throw new Error('Positioned diagram response must include DiagramView and PositionedGraph.');
  if (diagram.diagram_version !== 'archbro.diagram.v1') throw new Error(`Unsupported diagram contract: ${diagram.diagram_version || 'missing'}`);
  if (!['archbro.layout.v1','archbro.canvas-layout.v2','archbro.canvas-layout.v3','archbro.canvas-layout.v4','archbro.canvas-layout.v5','archbro.canvas-layout.v6','archbro.canvas-layout.v7','archbro.canvas-layout.v8','archbro.canvas-layout.v9','archbro.canvas-layout.v10'].includes(layout.layout_version)) throw new Error(`Unsupported layout contract: ${layout.layout_version || 'missing'}`);
  if (Number(diagram.architecture_version) !== Number(layout.architecture_version)) throw new Error('Diagram and layout architecture versions do not match.');
  const positionedById = new Map((layout.nodes || []).map((node) => [node.node_id, node]));
  const nodes = (diagram.nodes || []).map((node) => {
    const positioned = positionedById.get(node.id);
    if (!positioned) throw new Error(`PositionedGraph is missing node ${node.id}.`);
    const numbers = [positioned.x, positioned.y, positioned.width, positioned.height].map(Number);
    if (numbers.some((value) => !Number.isFinite(value)) || numbers[2] <= 0 || numbers[3] <= 0) throw new Error(`Invalid positioned node ${node.id}.`);
    const childCount = Number(node.child_count || 0);
    if (!Number.isInteger(childCount) || childCount < 0) throw new Error(`Invalid child_count for ${node.id}.`);
    const projectionRole = node.projection_role || 'PRIMARY';
    if (!['SCOPE','PRIMARY','CONTEXT'].includes(projectionRole)) throw new Error(`Invalid projection_role for ${node.id}.`);
    return {...node, projectionRole, childCount, x:numbers[0], y:numbers[1], width:numbers[2], height:numbers[3], layer:Number(positioned.layer || 0), order:Number(positioned.order || 0), hierarchyPath:positioned.hierarchy_path || []};
  });
  if (nodes.length !== positionedById.size) throw new Error('DiagramView and PositionedGraph node sets do not match.');
  const routesById = new Map((layout.edges || []).map((edge) => [edge.edge_id, edge]));
  const edges = (diagram.edges || []).map((edge) => {
    const route = routesById.get(edge.id);
    if (!route) throw new Error(`PositionedGraph is missing edge ${edge.id}.`);
    if (route.source !== edge.source || route.target !== edge.target) throw new Error(`Edge route endpoints do not match DiagramView for ${edge.id}.`);
    const points = (route.points || []).map((point) => ({x:Number(point.x), y:Number(point.y)}));
    if (points.length < 2 || points.some((point) => !Number.isFinite(point.x) || !Number.isFinite(point.y))) throw new Error(`Invalid route points for ${edge.id}.`);
    return {...edge, points, routing:route.routing || '', order:Number(route.order || 0)};
  });
  if (edges.length !== routesById.size) throw new Error('DiagramView and PositionedGraph edge sets do not match.');
  const width=Number(layout.width), height=Number(layout.height);
  if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) throw new Error('PositionedGraph requires positive graph dimensions.');
  return {diagramVersion:diagram.diagram_version, layoutVersion:layout.layout_version, architectureVersion:Number(diagram.architecture_version), summary:diagram.summary || '', width, height, nodes:nodes.sort((a,b)=>a.order-b.order || a.id.localeCompare(b.id)), edges:edges.sort((a,b)=>a.order-b.order || a.id.localeCompare(b.id))};
}

function normalizeScopedDiagramResponse(payload, requestedScopeComponentId = null) {
  if (payload?.schema && payload.schema !== 'archbro.scoped_diagram.v1') throw new Error(`Unsupported scoped diagram envelope: ${payload.schema}`);
  const graph = normalizeDiagramGraph(payload);
  const raw = payload?.scope || null;
  if (requestedScopeComponentId && !raw) throw new Error('Scoped diagram response is missing scope metadata.');
  const componentId = raw?.component_id ?? null;
  if (requestedScopeComponentId && componentId !== requestedScopeComponentId) throw new Error(`Scoped diagram response returned ${componentId || 'ROOT'} instead of ${requestedScopeComponentId}.`);
  const ancestorPath = Array.isArray(raw?.ancestor_path) ? raw.ancestor_path.map((item) => ({componentId:item.component_id ?? null,nodeId:item.node_id ?? null,label:String(item.label || item.component_id || 'Overview')})).filter((item) => item.componentId) : [];
  return {...graph, scope:{componentId,nodeId:raw?.node_id ?? (componentId ? `node:${componentId}` : null),label:String(raw?.label || (componentId ? componentId : 'Overview')),isLeaf:Boolean(raw?.is_leaf),ancestorPath,directRelationships:Array.isArray(raw?.direct_relationships) ? raw.direct_relationships : []}};
}

function normalizeFullCanvasResponse(payload) {
  if (payload?.schema !== 'archbro.full_canvas.v1') throw new Error(`Unsupported full canvas envelope: ${payload?.schema || 'missing'}`);
  const graph = normalizeDiagramGraph(payload);
  const nodeIds = new Set(graph.nodes.map((node) => node.id));
  const rawFrames = Array.isArray(payload.group_frames) ? payload.group_frames : [];
  const groupFrames = rawFrames.map((frame) => {
    const nodeId = String(frame.node_id || '');
    const parentGroupId = frame.parent_group_id ? String(frame.parent_group_id) : null;
    const numbers = [frame.x, frame.y, frame.width, frame.height, frame.depth, frame.order].map(Number);
    if (!nodeIds.has(nodeId)) throw new Error(`Canvas group frame references unknown node ${nodeId || 'missing'}.`);
    if (parentGroupId && !nodeIds.has(parentGroupId)) throw new Error(`Canvas group frame references unknown parent ${parentGroupId}.`);
    if (numbers.slice(0,4).some((value) => !Number.isFinite(value)) || numbers[2] <= 0 || numbers[3] <= 0) throw new Error(`Invalid canvas group frame ${nodeId}.`);
    return {nodeId,parentGroupId,x:numbers[0],y:numbers[1],width:numbers[2],height:numbers[3],depth:numbers[4],order:numbers[5]};
  }).sort((a,b)=>a.depth-b.depth || a.order-b.order || a.nodeId.localeCompare(b.nodeId));
  const edgeIds = new Set(graph.edges.map((edge) => edge.id));
  const readingViews = (payload.reading_views || []).map((view) => {
    if (Number(view.architecture_version)!==graph.architectureVersion || !view.id || !Array.isArray(view.edge_ids) || view.edge_ids.some((id) => !edgeIds.has(id))) throw new Error('Canvas reading view references an unknown relationship.');
    if (view.node_ids && (!Array.isArray(view.node_ids) || view.node_ids.some((id) => !nodeIds.has(id)))) throw new Error('Canvas reading view references an unknown node.');
    return {id:String(view.id), label:String(view.label || view.id), kind:view.kind || '', source:view.source || '', edgeIds:view.edge_ids, nodeIds:view.node_ids || []};
  });
  const summaryEnvelope=payload.connection_summaries;
  const summaryMembers=new Set(), summaryIds=new Set();
  if(summaryEnvelope && (summaryEnvelope.schema!=='archbro.connection-summaries.v1' || summaryEnvelope.architecture_version!==graph.architectureVersion || !Array.isArray(summaryEnvelope.groups))) throw new Error('Connection summary version does not match this architecture.');
  const connectionSummaries=(summaryEnvelope?.groups || []).map(raw=>{
    const members=raw.member_edge_ids;
    if(!raw.id || summaryIds.has(raw.id) || raw.architecture_version!==graph.architectureVersion || !['IN','OUT'].includes(raw.direction) || !nodeIds.has(raw.hub_node_id) || !nodeIds.has(raw.domain_node_id) || !Array.isArray(members) || members.length<2 || new Set(members).size!==members.length || members.some(id=>!edgeIds.has(id) || summaryMembers.has(id)) || !members.includes(raw.primary_edge_id)) throw new Error('Invalid connection summary membership.');
    const facts=members.map(id=>graph.edges.find(edge=>edge.id===id));
    const peers=facts.map(edge=>raw.direction==='IN'?edge.source:edge.target), peerCount=new Set(peers).size;
    if(peerCount<2 || facts.some(edge=>(raw.direction==='IN'?edge.target:edge.source)!==raw.hub_node_id || edge.relationship_category!==raw.relationship_category || graph.edges.some(other=>other.source===edge.target && other.target===edge.source)) || peers.some(id=>graph.nodes.find(node=>node.id===id)?.hierarchyPath[0]!==raw.domain_node_id)) throw new Error('Connection summary mixes boundaries, direction, or category.');
    const points=rawPoints=>{
      if(!Array.isArray(rawPoints) || rawPoints.length<2) throw new Error('Missing summary route.');
      const parsed=rawPoints.map(p=>({x:Number(p.x),y:Number(p.y)}));
      if(parsed.some((p,i)=>!Number.isFinite(p.x) || !Number.isFinite(p.y) || (i && p.x!==parsed[i-1].x && p.y!==parsed[i-1].y))) throw new Error('Invalid summary route.');
      return parsed;
    };
    if(!Array.isArray(raw.paths) || raw.paths.length!==peerCount || !raw.member_paths || Object.keys(raw.member_paths).length!==members.length) throw new Error('Incomplete summary routes.');
    const paths=raw.paths.map(points), memberPaths={};
    const primary=facts.find(edge=>edge.id===raw.primary_edge_id);
    for(const fact of facts) {
      const route=points(raw.member_paths[fact.id]);
      const startAllowed=raw.direction==='IN' ? [fact.points[0]] : [primary.points[0],fact.points[0]];
      const endAllowed=raw.direction==='IN' ? [primary.points.at(-1),fact.points.at(-1)] : [fact.points.at(-1)];
      if(!startAllowed.some(point=>JSON.stringify(route[0])===JSON.stringify(point)) || !endAllowed.some(point=>JSON.stringify(route.at(-1))===JSON.stringify(point))) throw new Error('Summary route changes a peer or shared endpoint.');
      memberPaths[fact.id]=route;
    }
    if(JSON.stringify(paths[0])!==JSON.stringify(primary.points)) throw new Error('Summary trunk must follow the canonical primary route.');
    if(raw.junctions!==undefined && !Array.isArray(raw.junctions)) throw new Error('Invalid summary junctions.');
    const junctionRayCount=(point)=>{
      const rays=new Set();
      const ray=(other)=>other.x<point.x?'L':other.x>point.x?'R':other.y<point.y?'U':other.y>point.y?'D':null;
      const contains=(a,b)=>a.x===b.x && a.x===point.x && Math.min(a.y,b.y)<=point.y && point.y<=Math.max(a.y,b.y)
        || a.y===b.y && a.y===point.y && Math.min(a.x,b.x)<=point.x && point.x<=Math.max(a.x,b.x);
      for(const route of Object.values(memberPaths)) for(let i=0;i<route.length-1;i+=1) {
        const a=route[i],b=route[i+1];
        if(!contains(a,b)) continue;
        if(a.x!==point.x || a.y!==point.y) {const direction=ray(a);if(direction)rays.add(direction);}
        if(b.x!==point.x || b.y!==point.y) {const direction=ray(b);if(direction)rays.add(direction);}
      }
      return rays.size;
    };
    const junctions=(raw.junctions || []).map(point=>{
      if(!point || !Number.isFinite(point.x) || !Number.isFinite(point.y) || junctionRayCount(point)<3) throw new Error('Summary junction must be a real branch in the final member-path union.');
      return {x:point.x,y:point.y};
    });
    summaryIds.add(raw.id);members.forEach(id=>summaryMembers.add(id));
    return {id:raw.id,direction:raw.direction,hubId:raw.hub_node_id,domainId:raw.domain_node_id,category:raw.relationship_category,memberIds:members,primaryId:raw.primary_edge_id,peerCount,paths,memberPaths,junctions,sharedPath:points(raw.shared_path)};
  });
  const presentation=payload.presentation;
  if (presentation) {
    if (presentation.locale!=='en' || presentation.architecture_version!==graph.architectureVersion
      || Object.keys(presentation.nodes || {}).some(id=>!nodeIds.has(id))
      || Object.keys(presentation.edges || {}).some(id=>!edgeIds.has(id))) throw new Error('Display translation does not match this architecture.');
    graph.nodes=graph.nodes.map(node=>({...node, canonicalLabel:node.label, canonicalResponsibility:node.responsibility, label:presentation.nodes[node.id]?.label || node.label, responsibility:presentation.nodes[node.id]?.responsibility || node.responsibility}));
    graph.edges=graph.edges.map(edge=>{
      const translated=presentation.edges[edge.id]?.label;
      return translated ? {...edge,canonicalLabel:edge.label,canonicalSemanticType:edge.semantic_type,label:translated,semantic_type:translated,supporting_text:translated} : edge;
    });
    readingViews.forEach(view=>{view.canonicalLabel=view.label;view.label=presentation.reading_labels?.[view.id] || view.label;});
  }
  return {...graph, fullCanvas:true, groupFrames, readingViews, connectionSummaries, scope:null, presentation};
}

function normalizeCodeArchitectureSnapshot(payload) {
  if (!payload || payload.schema !== 'archbro.code_architecture.v1') throw new Error(`Unsupported code architecture envelope: ${payload?.schema || 'missing'}`);
  const repository = payload.repository || {};
  const revision = String(repository.revision || '').toLowerCase();
  if (repository.provider !== 'github' || repository.revision_pinned !== true || !/^[0-9a-f]{40}$/.test(revision)) throw new Error('Code architecture requires an exact pinned GitHub commit SHA.');
  const diagram = payload.diagram;
  const layout = payload.positioned_graph;
  if (!diagram || !layout) throw new Error('Code architecture must include a diagram and positioned graph.');
  if (diagram.diagram_version !== 'archbro.code_diagram.v1') throw new Error(`Unsupported code diagram contract: ${diagram.diagram_version || 'missing'}`);
  if (layout.layout_version !== 'archbro.layout.v1') throw new Error(`Unsupported code layout contract: ${layout.layout_version || 'missing'}`);
  const positionedById = new Map((layout.nodes || []).map((node) => [node.node_id, node]));
  const nodes = (diagram.nodes || []).map((node) => {
    const positioned = positionedById.get(node.id);
    if (!positioned) throw new Error(`Code PositionedGraph is missing node ${node.id}.`);
    if (!String(node.id || '').startsWith('code-node:')) throw new Error(`Code graph node must use the code-node namespace: ${node.id}.`);
    const numbers = [positioned.x, positioned.y, positioned.width, positioned.height].map(Number);
    if (numbers.some((value) => !Number.isFinite(value)) || numbers[2] <= 0 || numbers[3] <= 0) throw new Error(`Invalid positioned code node ${node.id}.`);
    const sources = Array.isArray(node.sources) ? node.sources.map((source) => {
      const href = String(source.href || '');
      const expectedPrefix = `https://github.com/${repository.slug}/blob/${revision}/`;
      if (!href.startsWith(expectedPrefix)) throw new Error(`Code evidence for ${node.id} is not pinned to the snapshot revision.`);
      return {...source, href};
    }) : [];
    return {
      ...node,
      childCount:Number(node.child_count || 0),
      sources,
      x:numbers[0], y:numbers[1], width:numbers[2], height:numbers[3],
      layer:Number(positioned.layer || 0), order:Number(positioned.order || 0),
      hierarchyPath:positioned.hierarchy_path || [],
    };
  });
  if (nodes.length !== positionedById.size) throw new Error('Code Diagram and PositionedGraph node sets do not match.');
  const routesById = new Map((layout.edges || []).map((edge) => [edge.edge_id, edge]));
  const edges = (diagram.edges || []).map((edge) => {
    const route = routesById.get(edge.id);
    if (!route || route.source !== edge.source || route.target !== edge.target) throw new Error(`Code edge route does not match ${edge.id}.`);
    const points = (route.points || []).map((point) => ({x:Number(point.x), y:Number(point.y)}));
    if (points.length < 2 || points.some((point) => !Number.isFinite(point.x) || !Number.isFinite(point.y))) throw new Error(`Invalid code edge route ${edge.id}.`);
    return {...edge, points, routing:route.routing || '', order:Number(route.order || 0)};
  });
  if (edges.length !== routesById.size) throw new Error('Code Diagram and PositionedGraph edge sets do not match.');
  const width=Number(layout.width), height=Number(layout.height);
  if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) throw new Error('Code PositionedGraph requires positive dimensions.');
  return {
    schema:payload.schema,
    classification:payload.classification,
    canonicalStateMutated:Boolean(payload.canonical_state_mutated),
    repository:{...repository, revision},
    evidenceVerification:payload.evidence_verification || {},
    summary:payload.summary || diagram.summary || '',
    eventId:payload.event_id || null,
    publishedAt:payload.published_at || null,
    width, height,
    nodes:nodes.sort((a,b)=>a.order-b.order || a.id.localeCompare(b.id)),
    edges:edges.sort((a,b)=>a.order-b.order || a.id.localeCompare(b.id)),
  };
}

function architectureDiagramPath(projectId, architectureVersion, scopeComponentId = null, readingMode = 'MAP') {
  const params = new URLSearchParams();
  if (scopeComponentId) params.set('scope', scopeComponentId);
  if (Number(architectureVersion) > 0) params.set('expected_architecture_version', String(Number(architectureVersion)));
  if (['MAP','READ','FULL'].includes(readingMode)) params.set('reading_mode', readingMode);
  const query=params.toString();
  return `/projects/${projectId}/architecture/diagram${query ? `?${query}` : ''}`;
}

function architectureCanvasPath(projectId, architectureVersion, readingMode = 'MAP') {
  const params = new URLSearchParams();
  if (Number(architectureVersion) > 0) params.set('expected_architecture_version', String(Number(architectureVersion)));
  if (['MAP','READ','FULL'].includes(readingMode)) params.set('reading_mode', readingMode);
  const query=params.toString();
  return `/projects/${projectId}/architecture/canvas${query ? `?${query}` : ''}`;
}

async function loadArchitectureDiagram(projectId, architecture, scopeComponentId = null, readingMode = 'MAP') {
  if (!architecture?.components?.length) return null;
  const payload = await api(architectureDiagramPath(projectId, architecture.version, scopeComponentId, readingMode));
  return normalizeScopedDiagramResponse(payload, scopeComponentId);
}

async function loadArchitectureCanvasDiagram(projectId, architecture, readingMode = 'MAP') {
  if (!architecture?.components?.length) return null;
  const payload = await api(architectureCanvasPath(projectId, architecture.version, readingMode));
  return normalizeFullCanvasResponse(payload);
}

async function loadLatestCodeArchitecture(projectId) {
  return api(`/projects/${projectId}/code-architecture/latest`);
}

// WORKSPACE_CORE_LOADER_START
async function loadProjectCoreContext(projectId) {
  const bootstrap = await api(`/projects/${projectId}/workspace-bootstrap?reading_mode=FULL`);
  if (bootstrap?.schema !== 'archbro.workspace-bootstrap.v2') throw new Error('Unsupported workspace bootstrap contract.');
  return {
    project: bootstrap.project,
    tasks: bootstrap.tasks || [],
    architecture: bootstrap.architecture,
    proposals: bootstrap.proposals || [],
    activity: bootstrap.activity || [],
    lastRun: bootstrap.latest_agent_run || null,
    plannerRecovery: bootstrap.planner_recovery || null,
    deferredArchitectureResources: bootstrap.resources || {},
  };
}

function deferredBootstrapResourceHref(resource, expectedPrefix, architectureVersion) {
  const href = String(resource?.href || '');
  const versionToken = `expected_architecture_version=${Number(architectureVersion) || 0}`;
  if (resource?.status !== 'DEFERRED' || !href.startsWith(expectedPrefix) || !href.includes(versionToken)) {
    throw new Error('Workspace bootstrap returned an invalid deferred resource.');
  }
  return href;
}
// WORKSPACE_CORE_LOADER_END

async function refreshCanvasResource(contextTicket, architecture, {scopeComponentId = null, retainData = false, deferredResources = null, replacementAttempt = false} = {}) {
  const surface = ARCHITECTURE_CANVAS_MODE ? 'canvas' : 'project';
  const projectionScope = surface === 'canvas' ? null : (scopeComponentId || null);
  const readingMode = normalizedArchitectureReadingMode(state.readingMode);
  const cacheReadingMode = surface === 'canvas' ? 'FULL' : readingMode;
  const projectionGuard = captureGraphProjectionGuard({
    projectId:contextTicket.projectId,
    architectureVersion:architecture?.version,
    surface,
    scopeComponentId:projectionScope,
    readingMode,
  });
  const request = beginWorkspaceResource(state.workspaceAsync, 'canvas', contextTicket, {retainData:retainData && Boolean(state.diagram)});
  if (!request) return false;
  const replaceSupersededProjection = () => {
    if (!workspaceResourceIsCurrent(state.workspaceAsync, request)) return false;
    if (!replacementAttempt && state.projectId && state.architecture) {
      return refreshCanvasResource(currentWorkspaceContextTicket(state.workspaceAsync), state.architecture, {
        scopeComponentId:state.scopeComponentId, retainData:Boolean(state.diagram), replacementAttempt:true,
      });
    }
    const error = new Error('The graph view changed while loading. Retry the current view.');
    settleWorkspaceResource(state.workspaceAsync, request, 'error', error);
    render();
    return false;
  };
  try {
    let diagram;
    if (surface === 'canvas' && deferredResources?.canvas) {
      const href = deferredBootstrapResourceHref(deferredResources.canvas, `/projects/${contextTicket.projectId}/architecture/canvas?`, architecture.version);
      diagram = normalizeFullCanvasResponse(await api(href));
    } else if (surface === 'project' && !projectionScope && readingMode === 'MAP' && deferredResources?.project_diagram) {
      const href = deferredBootstrapResourceHref(deferredResources.project_diagram, `/projects/${contextTicket.projectId}/architecture/diagram?`, architecture.version);
      diagram = normalizeScopedDiagramResponse(await api(href), null);
    } else {
      diagram = surface === 'canvas'
        ? await loadArchitectureCanvasDiagram(contextTicket.projectId, architecture, 'FULL')
        : await loadArchitectureDiagram(contextTicket.projectId, architecture, projectionScope, readingMode);
    }
    if (!workspaceResourceIsCurrent(state.workspaceAsync, request)) return false;
    if (!graphProjectionMatchesCommittedState(projectionGuard)) return replaceSupersededProjection();
    const currentInteraction = captureGraphInteractionState();
    state.diagram = diagram;
    state.diagramError = null;
    settleWorkspaceResource(state.workspaceAsync, request, diagram ? 'ready' : 'empty');
    resetArchitectureViewCache(contextTicket.projectId, architecture.version);
    if (diagram) cacheArchitectureView(surface, contextTicket.projectId, architecture.version, projectionScope, cacheReadingMode, diagram);
    reconcileGraphInteractionState(diagram, currentInteraction);
    const committedNodeId = surface === 'canvas' ? (state.navigation?.committed?.nodeId || null) : null;
    if (committedNodeId && !diagramNodeByComponentId(committedNodeId, diagram)) {
      state.canvasInspectorOpen = false;
      state.canvasDeepLinkFocusPending = false;
      const navigationGuard = captureNavigationGuard(contextTicket.projectId);
      if (committedProjectGuardIsCurrent(navigationGuard)) {
        const committed = state.navigation.committed || navigationSnapshotFromState();
        if (!commitNavigation({...committed, nodeId:null, inspectorTab:'overview'}, {historyMode:'replace', guard:navigationGuard})) return false;
      }
    }
    render();
    return true;
  } catch (error) {
    if (!workspaceResourceIsCurrent(state.workspaceAsync, request)) return false;
    if (!graphProjectionMatchesCommittedState(projectionGuard)) return replaceSupersededProjection();
    architectureViewCache.delete(architectureViewCacheKey(surface, contextTicket.projectId, architecture.version, projectionScope, cacheReadingMode));
    state.diagramError = error?.message || String(error);
    if (!retainData) state.diagram = null;
    settleWorkspaceResource(state.workspaceAsync, request, 'error', error);
    render();
    return false;
  }
}

async function refreshCodeArchitectureResource(contextTicket, {retainData = false} = {}) {
  const request = beginWorkspaceResource(state.workspaceAsync, 'codeArchitecture', contextTicket, {retainData:retainData && Boolean(state.codeDiagram)});
  if (!request) return false;
  try {
    const codeArchitecture = await loadLatestCodeArchitecture(contextTicket.projectId);
    const codeDiagram = codeArchitecture ? normalizeCodeArchitectureSnapshot(codeArchitecture) : null;
    if (!workspaceResourceIsCurrent(state.workspaceAsync, request)) return false;
    state.codeArchitecture = codeArchitecture;
    state.codeDiagram = codeDiagram;
    settleWorkspaceResource(state.workspaceAsync, request, codeDiagram ? 'ready' : 'empty');
    if (state.currentView === 'architecture') renderGraph();
    return true;
  } catch (error) {
    if (!workspaceResourceIsCurrent(state.workspaceAsync, request)) return false;
    if (!retainData) {
      state.codeArchitecture = null;
      state.codeDiagram = null;
    }
    settleWorkspaceResource(state.workspaceAsync, request, 'error', error);
    if (state.currentView === 'architecture') renderGraph();
    return false;
  }
}

async function refreshProjectDiagramResource(contextTicket, architecture, {deferredResources = null} = {}) {
  if (!ARCHITECTURE_CANVAS_MODE || !deferredResources?.project_diagram) return false;
  const request = beginWorkspaceResource(
    state.workspaceAsync,
    'projectDiagram',
    contextTicket,
    {retainData:Boolean(cachedArchitectureView('project', contextTicket.projectId, architecture.version, null, 'MAP'))},
  );
  if (!request) return false;
  try {
    const href = deferredBootstrapResourceHref(deferredResources.project_diagram, `/projects/${contextTicket.projectId}/architecture/diagram?`, architecture.version);
    const diagram = normalizeScopedDiagramResponse(await api(href), null);
    if (!workspaceResourceIsCurrent(state.workspaceAsync, request)) return false;
    resetArchitectureViewCache(contextTicket.projectId, architecture.version);
    if (diagram) cacheArchitectureView('project', contextTicket.projectId, architecture.version, null, 'MAP', diagram);
    settleWorkspaceResource(state.workspaceAsync, request, diagram ? 'ready' : 'empty');
    return true;
  } catch (error) {
    if (!workspaceResourceIsCurrent(state.workspaceAsync, request)) return false;
    architectureViewCache.delete(architectureViewCacheKey('project', contextTicket.projectId, architecture.version, null, 'MAP'));
    settleWorkspaceResource(state.workspaceAsync, request, 'error', error);
    return false;
  }
}

function refreshWorkspaceOptionalResources(contextTicket, architecture, {scopeComponentId = null, retainData = false, deferredResources = null} = {}) {
  const requests = [
    refreshCanvasResource(contextTicket, architecture, {scopeComponentId, retainData, deferredResources}),
    refreshCodeArchitectureResource(contextTicket, {retainData}),
  ];
  if (ARCHITECTURE_CANVAS_MODE && deferredResources?.project_diagram) {
    requests.push(refreshProjectDiagramResource(contextTicket, architecture, {deferredResources}));
  }
  return Promise.allSettled(requests);
}

function retryWorkspaceResource(name) {
  if (state.workspaceAsync.phase !== 'ready' || !state.projectId || !state.architecture) return false;
  const ticket = currentWorkspaceContextTicket(state.workspaceAsync);
  if (name === 'canvas') {
    void refreshCanvasResource(ticket, state.architecture, {scopeComponentId:state.scopeComponentId,retainData:Boolean(state.diagram)});
    return true;
  }
  if (name === 'codeArchitecture') {
    void refreshCodeArchitectureResource(ticket, {retainData:Boolean(state.codeDiagram)});
    return true;
  }
  return false;
}

function wireWorkspaceResourceRetry(container) {
  container?.querySelectorAll?.('[data-retry-workspace-resource]').forEach((button) => button.addEventListener('click', () => {
    retryWorkspaceResource(button.dataset.retryWorkspaceResource);
  }));
}

function clearWorkspaceOptionalData() {
  state.diagram = null;
  state.diagramError = null;
  state.codeArchitecture = null;
  state.codeDiagram = null;
  for (const resource of Object.values(state.workspaceAsync.resources)) {
    resource.status = 'idle';
    resource.error = null;
    resource.refreshing = false;
  }
}

function restoreDisplayedWorkspaceAsync(error = null) {
  state.workspaceAsync.projectId = state.projectId || null;
  state.workspaceAsync.architectureVersion = Number(state.architecture?.version || 0);
  state.workspaceAsync.phase = state.project ? 'ready' : 'failed';
  state.workspaceAsync.error = error ? (error?.message || String(error)) : null;
}

async function selectProject(projectId, {
  view = 'overview',
  canvas = ARCHITECTURE_CANVAS_MODE,
  historyMode = 'push',
  navigationGuard = null,
  route = null,
} = {}) {
  if (!projectId) return false;
  const invalidateGraph = projectId !== state.projectId || !state.project || canvas !== ARCHITECTURE_CANVAS_MODE;
  const guard = navigationGuard || beginNavigationTransition(projectId, {invalidateGraph});
  if (!navigationGenerationIsCurrent(guard)) return false;
  state.openProjectMenuId = null;

  if (projectId === state.projectId && state.project && canvas === ARCHITECTURE_CANVAS_MODE) {
    state.onboarding.active = false;
    state.currentView = ROUTED_VIEWS.has(view) ? view : 'overview';
    if (state.currentView === 'tasks') {
      applyWorkspaceTabInvariants(route?.workspaceTab || 'tasks');
    }
    if (route?.canvas && route?.nodeId) {
      state.selectedComponentId = route.nodeId;
      state.selectedEdgeId = null;
      state.inspectorTab = route.inspectorTab || 'overview';
      state.canvasInspectorOpen = true;
      state.canvasDeepLinkApplied = false;
      state.canvasDeepLinkFocusPending = false;
    } else if (route?.canvas) {
      // A caller may restore the current project without going through the
      // popstate handler. The explicit empty Canvas route still owns selection.
      state.selectedComponentId = null;
      state.selectedEdgeId = null;
      state.inspectorTab = 'overview';
      state.canvasInspectorOpen = false;
      state.canvasDeepLinkApplied = false;
      state.canvasDeepLinkFocusPending = false;
      state.graphFocusMode = 'all';
      clearArchitectureTracePath({render:false});
    } else if (!canvas) {
      state.selectedComponentId = null;
      state.selectedEdgeId = null;
      state.inspectorTab = 'overview';
    }
    if (!commitNavigation({
      projectId,
      view:state.currentView,
      canvas,
      nodeId:route?.nodeId ?? state.selectedComponentId,
      inspectorTab:route?.inspectorTab ?? state.inspectorTab,
      workspaceTab:state.currentView === 'tasks' ? state.workspaceTab : 'tasks',
    }, {historyMode, guard})) return false;
    render();
    return true;
  }

  const previousProjectId = state.projectId;
  const previousArchitectureVersion = Number(state.architecture?.version || 0);
  const ticket = beginWorkspaceContext(state.workspaceAsync, projectId);
  $('projectTree')?.setAttribute('aria-busy', 'true');
  const contextRequest = beginWorkspaceContextRequest(projectId);
  try {
    const context = await loadProjectCoreContext(projectId);
    if (!navigationGenerationIsCurrent(guard)
      || !workspaceContextIsCurrent(state.workspaceAsync, ticket)
      || !isWorkspaceContextRequestCurrent(contextRequest)) return false;
    const retainOptional = previousProjectId === projectId
      && previousArchitectureVersion === Number(context.architecture?.version || 0)
      && canvas === ARCHITECTURE_CANVAS_MODE;
    if (!bindWorkspaceContextArchitecture(state.workspaceAsync, ticket, context.architecture?.version)) return false;
    if (!navigationGenerationIsCurrent(guard) || !isWorkspaceContextRequestCurrent(contextRequest)) return false;

    ARCHITECTURE_CANVAS_MODE = Boolean(canvas);
    syncArchitectureCanvasDomMode();
    const nextView = ROUTED_VIEWS.has(view) ? view : (canvas ? 'architecture' : 'overview');
    if (previousProjectId !== projectId) {
      state.lastInstruction = '';
      $('instruction').value = '';
      syncInstructionTextareaRows();
    }
    Object.assign(state, context, {
      projectId,
      agentContextManifest: null,
      agentContextKey: null,
      agentContextLoading: false,
      agentContextError: null,
      agentContextPromise: null,
      agentContextRequestSerial: state.agentContextRequestSerial + 1,
      agentContextPolicy: 'ASK_ALL',
      agentContextTelemetryVisible: true,
      scopeComponentId: null,
      readingMode: 'MAP',
      selectedComponentId: canvas && route?.nodeId ? route.nodeId : null,
      selectedCodeNodeId: null,
      architectureGraphKind: 'living',
      graphFocusMode: canvas && route?.nodeId ? 'connected' : 'all',
      collapsedNodeIds: new Set(),
      selectedTaskId: null,
      taskDetailId: null,
      taskDetailOrigin: null,
      taskActionNotice: null,
      selectedProposalId: null,
      proposalUpdating: new Set(),
      proposalDecisionNotice: null,
      notificationTransientMessage: null,
      proposalPreviews: new Map(),
      proposalPreviewSerial: state.proposalPreviewSerial + 1,
      workspaceTab: nextView === 'tasks'
        ? (route?.workspaceTab || 'tasks')
        : 'tasks',
      currentView: nextView,
      inspectorTab: canvas && route?.nodeId ? (route.inspectorTab || 'overview') : 'overview',
      canvasInspectorOpen: Boolean(canvas && route?.nodeId),
      canvasDeepLinkApplied: false,
      canvasDeepLinkFocusPending: false,
    });
    if (state.currentView === 'tasks') applyWorkspaceTabInvariants(state.workspaceTab);
    if (!retainOptional) clearWorkspaceOptionalData();
    state.onboarding.active = false;
    state.expandedProjectIds.add(projectId);
    persistExpandedProjectIds();
    if (!commitNavigation({
      projectId,
      view:state.currentView,
      canvas:ARCHITECTURE_CANVAS_MODE,
      nodeId:state.selectedComponentId,
      inspectorTab:state.inspectorTab,
      workspaceTab:state.currentView === 'tasks' ? state.workspaceTab : 'tasks',
    }, {historyMode, guard})) return false;
    render();
    void refreshWorkspaceOptionalResources(ticket, context.architecture, {
      retainData:retainOptional,
      deferredResources:context.deferredArchitectureResources,
    });
    return true;
  } catch (err) {
    if (!navigationGenerationIsCurrent(guard)
      || !workspaceContextIsCurrent(state.workspaceAsync, ticket)
      || !isWorkspaceContextRequestCurrent(contextRequest)) return false;
    if (state.project) restoreDisplayedWorkspaceAsync(err);
    else failWorkspaceContext(state.workspaceAsync, ticket, err);
    recommitCurrentNavigationAfterFailedTransition(guard, {historyMode:'replace'});
    toast(`Could not open that project. ${err.message}`, true);
    return false;
  } finally {
    $('projectTree')?.removeAttribute('aria-busy');
  }
}

async function refresh({projectId = state.projectId, guard = captureNavigationGuard(projectId)} = {}) {
  if (!navigationGenerationIsCurrent(guard)) return false;
  if (state.onboarding.active) {
    renderOnboarding();
    return true;
  }
  if (!state.projectId) {
    if (projectId) return false;
    await loadProjectSnapshots({guard});
    if (!navigationGenerationIsCurrent(guard)) return false;
    renderWorkspaceHome();
    return true;
  }
  if ((projectId || null) !== (state.projectId || null)) return false;
  const requestSerial = ++state.projectContextRequestSerial;
  const contextRequest = beginWorkspaceContextRequest(projectId);
  const rememberedProjectScope = state.scopeComponentId;
  const previousArchitectureVersion = Number(state.architecture?.version || 0);
  const ticket = beginWorkspaceContext(state.workspaceAsync, projectId);
  try {
    const context = await loadProjectCoreContext(projectId);
    if (!navigationGenerationIsCurrent(guard)
      || requestSerial !== state.projectContextRequestSerial
      || !workspaceContextIsCurrent(state.workspaceAsync, ticket)
      || !isWorkspaceContextRequestCurrent(contextRequest, {requireSelectedProject: true})) return false;
    const retainOptional = previousArchitectureVersion === Number(context.architecture?.version || 0);
    Object.assign(state, context);
    if (!bindWorkspaceContextArchitecture(state.workspaceAsync, ticket, context.architecture?.version)) return false;
    if (!navigationGenerationIsCurrent(guard)
      || requestSerial !== state.projectContextRequestSerial
      || !isWorkspaceContextRequestCurrent(contextRequest, {requireSelectedProject: true})) return false;
    // Projection facts contain task/proposal/health state that can change without
    // an architecture-version bump. Only the newest successful core refresh is
    // allowed to invalidate them; failed or superseded refreshes preserve cache.
    invalidateArchitectureViewCache(projectId);
    state.proposalPreviews.clear();
    state.proposalPreviewSerial += 1;
    if (state.currentView === 'tasks') applyWorkspaceTabInvariants(state.workspaceTab);
    if (!retainOptional) clearWorkspaceOptionalData();
    clearAgentContextPreview();
    render();
    void refreshWorkspaceOptionalResources(ticket, context.architecture, {
      scopeComponentId:rememberedProjectScope,
      retainData:retainOptional,
      deferredResources:context.deferredArchitectureResources,
    });
    return true;
  } catch (err) {
    if (!navigationGenerationIsCurrent(guard)
      || requestSerial !== state.projectContextRequestSerial
      || !workspaceContextIsCurrent(state.workspaceAsync, ticket)) return false;
    if (String(err.message).startsWith('404:')) {
      await openPersonalWorkspace({historyMode:'replace'});
      toast('This project is no longer available. Returned to your workspace without opening another project.', true);
      return true;
    }
    if (state.project) restoreDisplayedWorkspaceAsync(err);
    else failWorkspaceContext(state.workspaceAsync, ticket, err);
    toast(err.message, true);
    return false;
  }
}

function startOnboarding() {
  if (state.onboarding.workingTimer) clearInterval(state.onboarding.workingTimer);
  const returnNavigation = state.navigation.committed ? {...state.navigation.committed} : navigationSnapshotFromState();
  const guard = beginNavigationTransition(null);
  state.navigation.onboardingReturn = returnNavigation?.projectId ? returnNavigation : null;
  state.currentView = 'overview';
  state.onboarding = {
    active: true,
    stage: 'name',
    projectName: '',
    initialGoal: '',
    messages: [],
    draft: null,
    working: false,
    workingStartedAt: null,
    workingTimer: null,
    workingRequestId: null,
    lastError: null,
  };
  state.selectedTaskId = null;
  state.taskDetailId = null;
  state.taskDetailOrigin = null;
  state.taskActionNotice = null;
  state.workspaceTab = 'tasks';
  state.selectedProposalId = null;
  state.proposalUpdating.clear();
  state.proposalDecisionNotice = null;
  state.notificationTransientMessage = null;
  state.selectedComponentId = null;
  state.selectedCodeNodeId = null;
  state.architectureGraphKind = 'living';
  state.scopeComponentId = null;
  state.readingMode = 'MAP';
  commitNavigation({projectId:null, view:'overview', canvas:false, nodeId:null, inspectorTab:'overview'}, {historyMode:'push', guard});
  renderOnboarding();
  openNewProjectNameDialog();
}

function renderOnboarding() {
  syncDocumentTitle();
  $('emptyState').classList.remove('hidden');
  $('workspace').classList.add('hidden');
  renderProjectTree();
  const agentModePanel = $('webmcpAgentModePanel');
  if (WEBMCP_AGENT_MODE) {
    $('pageTitle').textContent = 'WebMCP Agent Mode';
    $('pageSubtitle').textContent = 'Project mutations in this session must use the registered Site Tools.';
    $('initialGoalStage').classList.add('hidden');
    $('refineGoalStage').classList.add('hidden');
    agentModePanel?.classList.remove('hidden');
    $('onboardingBackBtn').classList.add('hidden');
    renderNotifications();
    renderAccountIdentity();
    return;
  }
  agentModePanel?.classList.add('hidden');
  $('pageTitle').textContent = 'New Project';
  $('pageSubtitle').textContent = state.onboarding.stage === 'refine' ? 'Refine the Goal Draft with the Agent before generating architecture.' : 'Name the project, then write its first goal.';
  $('initialGoalStage').classList.toggle('hidden', state.onboarding.stage !== 'goal');
  $('refineGoalStage').classList.toggle('hidden', state.onboarding.stage !== 'refine');
  $('onboardingBackBtn').classList.toggle('hidden', !state.projectId);
  $('onboardingProjectName').textContent = state.onboarding.projectName;
  if (state.onboarding.stage === 'goal') $('initialGoal').value = state.onboarding.initialGoal;
  if (state.onboarding.stage === 'refine' && document.activeElement !== $('goalDraftText')) $('goalDraftText').value = state.onboarding.initialGoal;
  syncOnboardingAskRainbowState();
  renderOnboardingConversation();
  renderGoalDraft();
  renderNotifications();
  renderAccountIdentity();
}

function openNewProjectNameDialog() {
  $('newProjectName').value = state.onboarding.projectName;
  $('newProjectNameError').textContent = '';
  showDialog('newProjectNameDialog');
}

function submitNewProjectName(event) {
  event.preventDefault();
  const name = $('newProjectName').value.trim();
  if (!name) {
    $('newProjectNameError').textContent = 'Enter a project name.';
    $('newProjectName').setAttribute('aria-invalid', 'true');
    $('newProjectName').focus();
    return;
  }
  state.onboarding.projectName = name;
  $('newProjectName').removeAttribute('aria-invalid');
  state.onboarding.stage = state.onboarding.stage === 'refine' ? 'refine' : 'goal';
  $('newProjectNameDialog').close();
  renderOnboarding();
  setTimeout(() => (state.onboarding.stage === 'refine' ? $('goalDraftText') : $('initialGoal')).focus(), 0);
}

function continueToRefinement(event) {
  event.preventDefault();
  const goal = $('initialGoal').value.trim();
  if (!goal) {
    $('initialGoalError').textContent = 'Describe your project goal to continue.';
    $('initialGoal').setAttribute('aria-invalid', 'true');
    $('initialGoal').focus();
    return;
  }
  state.onboarding.initialGoal = goal;
  state.onboarding.stage = 'refine';
  $('initialGoal').removeAttribute('aria-invalid');
  $('initialGoalError').textContent = '';
  renderOnboarding();
  setTimeout(() => $('goalDraftText').focus(), 0);
}

function returnToProjectName() {
  openNewProjectNameDialog();
}

function hasCurrentProject() {
  return Boolean(state.projectId && state.project);
}

function syncDocumentTitle() {
  const nextTitle = ARCHITECTURE_CANVAS_MODE
    && state.currentView === 'architecture'
    && state.project
    ? `${state.diagram?.presentation?.title || 'System architecture'} · Archbro`
    : 'Archbro';
  if (document.title !== nextTitle) document.title = nextTitle;
}

function cancelNewProjectNameDialog() {
  $('newProjectNameDialog').close();
}

function handleNewProjectNameDialogClose() {
  if (state.onboarding.stage !== 'name') return;
  if (state.navigation.onboardingReturn?.projectId && hasCurrentProject()) {
    void backToCurrentProject();
    return;
  }
  renderWorkspaceHome();
}

function handleNewProjectNameDialogCancel(event) {
  event.preventDefault();
  cancelNewProjectNameDialog();
}

function onboardingProgressText() {
  const startedAt = state.onboarding.workingStartedAt || Date.now();
  const elapsed = Math.max(0, Math.floor((Date.now() - startedAt) / 1000));
  let stage = 'Reading your current Goal and Ask';
  if (elapsed >= 3) stage = 'Merging requirements without replacing your Goal';
  if (elapsed >= 7) stage = 'Building a self-contained Goal draft';
  if (elapsed >= 13) stage = 'Gemini is busy; trying a bounded fallback';
  return {elapsed, text: `${stage} · ${elapsed}s`};
}

function updateOnboardingProgressUI() {
  if (!state.onboarding.working) return;
  const progress = onboardingProgressText();
  const bubble = $('onboardingWorkingText');
  if (bubble) bubble.textContent = progress.text;
  if (state.onboarding.workingRequestId) {
    updateWorkingRequest(state.onboarding.workingRequestId, `Updating Goal · ${progress.elapsed}s`);
  }
}

function startOnboardingProgress() {
  if (state.onboarding.workingTimer) clearInterval(state.onboarding.workingTimer);
  state.onboarding.working = true;
  state.onboarding.workingStartedAt = Date.now();
  state.onboarding.lastError = null;
  const workingRequestId = beginWorkingRequest('Updating Goal · 0s', {projectId:state.projectId});
  state.onboarding.workingRequestId = workingRequestId;
  const sendButton = document.querySelector('#onboardingForm button[type="submit"]');
  if (sendButton) {
    sendButton.disabled = true;
    sendButton.textContent = 'Working…';
  }
  renderOnboardingConversation();
  updateOnboardingProgressUI();
  state.onboarding.workingTimer = setInterval(updateOnboardingProgressUI, 1000);
  return workingRequestId;
}

function stopOnboardingProgress(workingRequestId = state.onboarding.workingRequestId) {
  const ownsOnboarding = Boolean(workingRequestId) && state.onboarding.workingRequestId === workingRequestId;
  finishWorkingRequest(workingRequestId);
  if (!ownsOnboarding) return;
  if (state.onboarding.workingTimer) clearInterval(state.onboarding.workingTimer);
  state.onboarding.workingTimer = null;
  state.onboarding.working = false;
  state.onboarding.workingStartedAt = null;
  state.onboarding.workingRequestId = null;
  const sendButton = document.querySelector('#onboardingForm button[type="submit"]');
  if (sendButton) {
    sendButton.disabled = false;
    sendButton.textContent = 'Send';
  }
}

function renderOnboardingConversation() {
  const el = $('onboardingConversation');
  if (!state.onboarding.messages.length && !state.onboarding.working && !state.onboarding.lastError) {
    el.classList.add('hidden');
    el.innerHTML = '';
    return;
  }

  el.classList.remove('hidden');

  const messages = state.onboarding.messages.map((message) => {
    const label = message.role === 'user' ? 'You' : 'Agent';
    return `<div class="chat-message ${message.role}"><small>${label}</small><p>${escapeHtml(message.content)}</p></div>`;
  }).join('');

  const working = state.onboarding.working
    ? `<div class="chat-message assistant working-bubble"><small>Agent · working</small><p><span class="working-spinner" aria-hidden="true"></span><span id="onboardingWorkingText">${escapeHtml(onboardingProgressText().text)}</span></p><span class="working-hint">Your existing Goal is kept as the baseline while this runs.</span></div>`
    : '';

  const error = state.onboarding.lastError
    ? `<div class="chat-message assistant error-bubble"><small>Agent · stopped</small><p>I could not finish this Goal update. Nothing was cleared or persisted.</p><span class="working-hint">${escapeHtml(state.onboarding.lastError)}</span><button id="onboardingRetryBtn" class="retry-ask" type="button">Retry this Ask</button></div>`
    : '';

  el.innerHTML = messages + working + error;
  const retry = $('onboardingRetryBtn');
  if (retry) retry.onclick = retryOnboardingAsk;
  el.scrollTop = el.scrollHeight;
}

function renderGoalDraft() {
  const draft = state.onboarding.draft;
  const goalInput = $('goalDraftText');

  if (!draft) {
    const hasManualGoal = Boolean(goalInput.value.trim());
    $('goalDraftStatus').textContent = hasManualGoal ? 'Using your written Goal' : 'Write a Goal or start with Ask';
    $('goalReadyBadge').textContent = hasManualGoal ? 'MANUAL' : 'DRAFT';
    $('goalReadyBadge').className = `status-pill ${hasManualGoal ? 'DONE' : ''}`;
    $('missingInfoWrap').classList.add('hidden');
    updateGoalConfirmState();
    return;
  }

  $('goalDraftStatus').textContent = draft.ready ? 'Ready to become the project Goal' : 'Still shaping the project Goal';
  $('goalReadyBadge').textContent = draft.ready ? 'READY' : 'DRAFT';
  $('goalReadyBadge').className = `status-pill ${draft.ready ? 'DONE' : ''}`;
  if (document.activeElement !== goalInput) {
    goalInput.value = draft.goal || '';
    state.onboarding.initialGoal = goalInput.value;
  }
  const missing = draft.missing_information || [];
  $('missingInfoWrap').classList.toggle('hidden', !missing.length);
  $('missingInfo').innerHTML = missing.map((item) => `<li>${escapeHtml(item)}</li>`).join('');
  updateGoalConfirmState();
}

function syncOnboardingAskRainbowState({activate = false} = {}) {
  const onboardingAsk = $('onboardingAsk');
  const composer = onboardingAsk?.closest('.onboarding-ask');
  if (!onboardingAsk || !composer) return;
  const hasContent = Boolean(onboardingAsk.value.trim());
  const focused = document.activeElement === onboardingAsk || onboardingAsk.matches(':focus');
  const shouldGlow = activate && hasContent && focused;
  composer.classList.toggle('rainbow-active', shouldGlow);
}

function syncInstructionRainbowState({activate = false} = {}) {
  const instruction = $('instruction');
  const composer = instruction?.closest('.global-agent-composer');
  if (!instruction || !composer) return;
  const hasContent = Boolean(instruction.value.trim());
  const focused = document.activeElement === instruction || instruction.matches(':focus');
  const shouldGlow = activate && hasContent && focused;
  composer.classList.toggle('rainbow-active', shouldGlow);
}

function updateGoalConfirmState() {
  const hasGoal = Boolean($('goalDraftText').value.trim());
  $('useGoalBtn').disabled = !hasGoal || state.onboarding.working;
}

async function requestOnboardingGoalDraft() {
  if (state.onboarding.working) return null;
  const guard = captureNavigationGuard(state.projectId);
  const workingRequestId = startOnboardingProgress();
  updateGoalConfirmState();
  try {
    const draft = await api('/onboarding/goal', {
      method: 'POST',
      body: JSON.stringify({
        messages: state.onboarding.messages,
        current_goal: $('goalDraftText').value.trim(),
      }),
      timeoutMs: 30000,
    });
    if (!navigationGenerationIsCurrent(guard)) return null;
    state.onboarding.draft = draft;
    state.onboarding.lastError = null;
    state.onboarding.messages.push({role: 'assistant', content: draft.assistant_message});
    return draft;
  } catch (err) {
    if (!navigationGenerationIsCurrent(guard)) return null;
    state.onboarding.lastError = err.message || String(err);
    toast('Goal update stopped. Your Goal and Ask are preserved; retry when ready.', true);
    return null;
  } finally {
    stopOnboardingProgress(workingRequestId);
    if (navigationGenerationIsCurrent(guard)) {
      renderOnboardingConversation();
      renderGoalDraft();
    }
  }
}

async function submitOnboardingAsk() {
  if (WEBMCP_AGENT_MODE) {
    toast('Built-in Agent onboarding is disabled in WebMCP Agent Mode.', true);
    return;
  }
  if (state.onboarding.working) return;
  const input = $('onboardingAsk');
  const content = input.value.trim();
  if (!content) return;
  input.value = '';
  syncOnboardingAskRainbowState();
  state.onboarding.messages.push({role: 'user', content});
  state.onboarding.lastError = null;
  renderOnboardingConversation();
  await requestOnboardingGoalDraft();
}

async function retryOnboardingAsk() {
  if (state.onboarding.working) return;
  if (!state.onboarding.messages.length && !$('goalDraftText').value.trim()) return;
  state.onboarding.lastError = null;
  await requestOnboardingGoalDraft();
}

async function confirmGoalAndGenerate() {
  if (WEBMCP_AGENT_MODE) {
    toast('Built-in architecture generation is disabled in WebMCP Agent Mode.', true);
    return;
  }
  const name = state.onboarding.projectName.trim();
  const goal = $('goalDraftText').value.trim();
  if (!name || !goal || state.onboarding.working) return;
  const callerGuard = captureNavigationGuard(state.projectId);
  const workingRequestId = beginWorkingRequest('Creating project…', {projectId:state.projectId});
  try {
    const project = await api('/projects', {
      method: 'POST',
      body: JSON.stringify({name, goal, description: 'Goal drafted through Goal + Ask onboarding.'}),
    });
    if (!navigationGenerationIsCurrent(callerGuard)) return;
    $('goalDraftText').value = '';
    $('onboardingAsk').value = '';
    if (!(await selectProject(project.id, {historyMode:'push'}))) return;
    const projectGuard = captureNavigationGuard(project.id);
    await loadProjects({guard:projectGuard});
    if (!committedProjectGuardIsCurrent(projectGuard)) return;
    toast('Goal confirmed. Generating Architecture v1…');
    await generateInitialArchitecture();
  } catch (err) {
    if (navigationGenerationIsCurrent(callerGuard)) toast(err.message, true);
  } finally {
    finishWorkingRequest(workingRequestId);
  }
}

async function backToCurrentProject() {
  const target = state.navigation.onboardingReturn;
  if (!target?.projectId || target.projectId !== state.projectId || !state.project) return false;
  const guard = beginNavigationTransition(target.projectId);
  state.onboarding.active = false;
  state.navigation.onboardingReturn = null;
  state.currentView = target.view;
  ARCHITECTURE_CANVAS_MODE = Boolean(target.canvas);
  syncArchitectureCanvasDomMode();
  if (!commitNavigation(target, {historyMode:'replace', guard})) return false;
  render();
  return refresh({projectId:target.projectId, guard});
}

function openEditProject(trigger = document.activeElement) {
  if (!state.project) return;
  $('editProjectName').value = state.project.name;
  $('editProjectGoal').value = state.project.goal;
  $('editProjectDescription').value = state.project.description || '';
  const lockedGoal = (state.architecture?.version || 0) > 0;
  $('editProjectGoal').disabled = lockedGoal;
  $('editGoalHint').classList.toggle('hidden', !lockedGoal);
  showDialog('editProjectDialog', trigger);
}

async function saveProjectEdits() {
  if (!state.projectId) return;
  const projectId = state.projectId;
  const contextRequest = beginWorkspaceContextRequest(projectId);
  const body = {
    name: $('editProjectName').value.trim(),
    description: $('editProjectDescription').value.trim(),
  };
  if (!$('editProjectGoal').disabled) body.goal = $('editProjectGoal').value.trim();
  if (!body.name || (body.goal !== undefined && !body.goal)) return;
  try {
    const updated = await api(`/projects/${projectId}`, {method: 'PATCH', body: JSON.stringify(body)});
    if (!isWorkspaceContextRequestCurrent(contextRequest, {requireSelectedProject: true})) {
      await loadProjects();
      return;
    }
    state.project = updated;
    $('editProjectDialog').close();
    await loadProjects();
    await refresh();
    toast('Project updated.');
  } catch (err) {
    toast(err.message, true);
  }
}

function openDeleteProject(trigger = document.activeElement) {
  if (!state.project) return;
  $('deleteProjectName').textContent = state.project.name;
  showDialog('deleteProjectDialog', trigger);
}

async function deleteCurrentProject() {
  const deletedId = state.projectId;
  if (!deletedId) return;
  const guard = captureNavigationGuard(deletedId);
  const deletedName = state.project?.name || 'Project';
  try {
    await api(`/projects/${deletedId}`, {method: 'DELETE'});
    if (!committedProjectGuardIsCurrent(guard)) {
      const currentGuard = captureNavigationGuard(state.projectId);
      await loadProjects({guard:currentGuard});
      return;
    }
    $('deleteProjectDialog').close();
    state.projectSnapshots.delete(deletedId);
    state.expandedProjectIds.delete(deletedId);
    persistExpandedProjectIds();
    await loadProjects({guard});
    if (!committedProjectGuardIsCurrent(guard)) return;
    if (state.projects.length) {
      await selectProject(state.projects[0].id, {historyMode:'replace'});
    } else {
      await openPersonalWorkspace({historyMode:'replace'});
    }
    toast(`${deletedName} deleted.`);
  } catch (err) {
    if (committedProjectGuardIsCurrent(guard)) toast(err.message, true);
  }
}

function closeDialogOnBackdrop(event) {
  const dialog = event.currentTarget;
  if (event.target === dialog) dialog.close();
}

function closeAuthDialogOnBackdrop(event) {
  if (event.target === event.currentTarget) closeAuthentication();
}

function closeNewProjectNameDialogOnBackdrop(event) {
  if (event.target === event.currentTarget) cancelNewProjectNameDialog();
}

function showDialog(dialogId, trigger = document.activeElement) {
  const dialog = $(dialogId);
  state.experience.dialogReturnFocus.set(dialogId, trigger);
  dialog.showModal();
  setTimeout(() => dialog.querySelector('[autofocus], input:not([disabled]), textarea:not([disabled]), button:not([disabled])')?.focus(), 0);
}

function closeOverlay({returnFocus = false} = {}) {
  const openMenu = [['notificationBtn', 'notificationMenu'], ['accountBtn', 'accountMenu']]
    .find(([, menuId]) => !$(menuId).classList.contains('hidden'));
  const projectMenuWasOpen = state.openProjectMenuId;
  const mobileWasOpen = document.body.classList.contains('sidebar-open');
  closeProjectMenu({returnFocus});
  closeTopMenus();
  closeMobileSidebar();
  if (returnFocus && projectMenuWasOpen) return;
  if (returnFocus && openMenu) $(openMenu[0]).focus();
  else if (returnFocus && mobileWasOpen) $('mobileSidebarBtn').focus();
}

function renderNotifications() {
  const profile = prototype.currentProfile(localStorage);
  const items = state.onboarding.active ? [] : prototype.deriveNeedsYou(state.proposals, state.tasks, profile?.notifications, state.architecture?.version);
  $('notificationBadge').textContent = items.length;
  $('notificationBadge').classList.toggle('hidden', items.length === 0);
  if (state.notificationTransientMessage) {
    $('notificationCount').textContent = 'Updated';
    $('notificationList').innerHTML = `<p class="notification-empty" role="status" tabindex="-1">${escapeHtml(state.notificationTransientMessage)}</p>`;
    return;
  }
  $('notificationCount').textContent = `${items.length} request${items.length === 1 ? '' : 's'}`;
  $('notificationList').innerHTML = items.length
    ? items.map((item) => `<button class="notification-item" type="button" data-attention-kind="${item.kind}" data-attention-id="${escapeHtml(item.id)}"><strong>${escapeHtml(item.title)}</strong><span>${escapeHtml(item.description)}</span></button>`).join('')
    : `<p class="notification-empty" role="status" tabindex="-1">${state.onboarding.active ? 'Nothing needs you in a new project draft. Return to a project to review its items.' : 'Nothing needs your approval right now.'}</p>`;
  document.querySelectorAll('[data-attention-kind]').forEach((button) => button.addEventListener('click', async () => openAttentionItem(button.dataset.attentionKind, button.dataset.attentionId)));
}

function attentionItemExists(kind, id) {
  return kind === 'proposal'
    ? state.proposals.some((item) => item.id === id)
    : kind === 'task' && state.tasks.some((item) => item.id === id);
}

function showUnavailableAttentionItem() {
  closeTopMenus();
  state.notificationTransientMessage = 'Item no longer available. The project has been refreshed.';
  renderNotifications();
  $('notificationMenu').classList.remove('hidden');
  $('notificationBtn').setAttribute('aria-expanded', 'true');
  setTimeout(() => $('notificationList').querySelector('[role="status"]')?.focus(), 0);
}

async function openAttentionItem(kind, id) {
  closeTopMenus();
  if (!attentionItemExists(kind, id)) {
    if (!state.onboarding.active && state.projectId) await refresh();
    if (!attentionItemExists(kind, id)) {
      showUnavailableAttentionItem();
      return false;
    }
  }
  if (kind === 'proposal') {
    if (!isProposalActionable(state.proposals.find((item) => item.id === id), state.architecture)) return false;
    switchWorkspaceTab('review', {focusedProposalId:id});
    setTimeout(() => document.querySelector(`[data-proposal-select="${CSS.escape(id)}"]`)?.focus(), 0);
    return true;
  }
  switchView('tasks', {workspaceTab: 'tasks'});
  return openTaskDetails(id, 'tasks');
}

function closeTopMenus() {
  for (const [buttonId, menuId] of [['notificationBtn', 'notificationMenu'], ['accountBtn', 'accountMenu']]) {
    $(menuId).classList.add('hidden');
    $(buttonId).setAttribute('aria-expanded', 'false');
  }
}

function toggleTopMenu(buttonId, menuId) {
  const opening = $(menuId).classList.contains('hidden');
  closeProjectMenu({returnFocus: false});
  clearProjectMenuFocusQueue();
  closeTopMenus();
  if (!opening) return;
  if (menuId === 'notificationMenu' && state.notificationTransientMessage) {
    state.notificationTransientMessage = null;
    renderNotifications();
  }
  $(menuId).classList.remove('hidden');
  $(buttonId).setAttribute('aria-expanded', 'true');
  const target = menuId === 'notificationMenu'
    ? $(menuId).querySelector('[data-attention-kind], #notificationCloseBtn') || $(menuId)
    : $(menuId).querySelector('[role="menuitem"]') || $(menuId);
  target.focus();
}

function renderAccountIdentity() {
  const profile = prototype.currentProfile(localStorage);
  if (!profile) return;
  const initials = profile.name.split(/\s+/).filter(Boolean).slice(0, 2).map((part) => part[0].toUpperCase()).join('');
  const safeInitials = initials || 'HU';
  $('accountInitials').textContent = safeInitials;
  $('accountBtn').setAttribute('aria-label', `Account menu for ${profile.name}`);
  const lens = profile.defaultLens || '';
  const lensLabel = lens ? `${lens[0].toUpperCase()}${lens.slice(1)}` : '';
  $('workspaceLens').textContent = lensLabel ? `Default lens · ${lensLabel}` : '';
  $('workspaceLens').classList.toggle('hidden', !lensLabel);
}

function resetEphemeralSessionState() {
  resetMcpAccountUi();
  supersedeWorkspaceContextRequests();
  if (state.onboarding.workingTimer) clearInterval(state.onboarding.workingTimer);
  state.openProjectMenuId = null;
  state.projectMenuFocusId = null;
  closeTopMenus();
  closeMobileSidebar({returnFocus: false});
  document.querySelectorAll('dialog[open]').forEach((dialog) => dialog.close());
  setArchitectureProgress(false);
  setWorking(false);
  clearTimeout(toast.timer);
  $('toast').textContent = '';
  $('toast').classList.add('hidden');
  $('toast').classList.remove('error');
  $('authForm').reset();
  writeAuthErrors({});
  document.querySelectorAll('[data-password-target]').forEach((button) => {
    const input = $(button.dataset.passwordTarget);
    if (input) input.type = 'password';
    button.textContent = 'Show';
    button.setAttribute('aria-label', 'Show password');
  });
  setAuthMode('signin');
  state.experience.selectedLens = null;
  document.querySelectorAll('[data-project-lens]').forEach((button, index) => {
    button.setAttribute('aria-checked', 'false');
    button.tabIndex = index === 0 ? 0 : -1;
    button.classList.remove('selected');
  });
  $('preferenceContinueBtn').disabled = true;
  state.currentView = 'overview';
  state.renamingProjectId = null;
  state.selectedComponentId = null;
  state.selectedCodeNodeId = null;
  state.architectureGraphKind = 'living';
  state.scopeComponentId = null;
  state.readingMode = 'MAP';
  state.selectedTaskId = null;
  state.taskDetailId = null;
  state.taskDetailOrigin = null;
  state.taskActionNotice = null;
  state.selectedProposalId = null;
  state.proposalUpdating.clear();
  state.proposalDecisionNotice = null;
  state.notificationTransientMessage = null;
  state.lastRun = null;
  state.lastInstruction = '';
  state.plannerRecovery = null;
  clearAgentContextPreview();
  state.projects = [];
  state.project = null;
  state.tasks = [];
  state.architecture = null;
  state.diagram = null;
  state.diagramError = null;
  state.codeArchitecture = null;
  state.codeDiagram = null;
  state.graphFocusMode = 'all';
  state.collapsedNodeIds.clear();
  state.proposals = [];
  state.projectSnapshots = new Map();
  state.taskUpdating.clear();
  state.projectId = localStorage.getItem('archbro-project-id');
  state.workspaceAsync = makeWorkspaceAsyncState(state.projectId);
  state.experience.workspaceError = null;
  state.expandedProjectIds = loadExpandedProjectIds();
  if (state.projectId) state.expandedProjectIds.add(state.projectId);
  state.onboarding = {
    active: !state.projectId,
    stage: 'name',
    projectName: '',
    initialGoal: '',
    messages: [],
    draft: null,
    working: false,
    workingStartedAt: null,
    workingTimer: null,
    lastError: null,
  };
  state.experience.settingsSection = 'profile';
  $('onboardingAsk').value = '';
  syncOnboardingAskRainbowState();
  $('goalDraftText').value = '';
  $('initialGoal').value = '';
  $('newProjectName').value = '';
  $('instruction').value = '';
  syncInstructionTextareaRows();
  syncInstructionRainbowState();
  renderAgentConversationDialog();
  $('instruction').removeAttribute('aria-invalid');
  $('instructionError').textContent = '';
}

async function logout() {
  try {
    supersedeWorkspaceContextRequests();
    await signOutFromFirebase();
    prototype.endSession(localStorage);
    state.experience.workspaceInitialized = false;
    resetEphemeralSessionState();
    showExperience('landing');
    $('landingAuthTeaser').focus();
  } catch (error) {
    toast(authenticationErrorMessage(error), true);
  }
}

function openAccountSection(section) {
  const profile = prototype.currentProfile(localStorage);
  if (!profile) return;
  state.experience.settingsSection = section;
  $('accountSettingsTitle').textContent = section[0].toUpperCase() + section.slice(1);
  document.querySelectorAll('[data-settings-panel]').forEach((button) => button.classList.toggle('active', button.dataset.settingsPanel === section));
  if (section === 'profile') {
    $('settingsPanel').innerHTML = `<label>Display name<input id="settingsName" value="${escapeHtml(profile.name)}" /></label><label>Email<input value="${escapeHtml(profile.email)}" disabled /></label><p id="settingsNameError" class="field-error"></p>`;
  } else if (section === 'preferences') {
    $('settingsPanel').innerHTML = `<div class="settings-lens-group" role="radiogroup" aria-label="Default project lens">${['software', 'design', 'engineering'].map((lens) => `<button type="button" role="radio" aria-checked="${profile.defaultLens === lens}" tabindex="${profile.defaultLens === lens ? '0' : '-1'}" data-settings-lens="${lens}">${lens[0].toUpperCase() + lens.slice(1)}</button>`).join('')}</div>`;
    wireLensRadioGroup($('settingsPanel'), (lens) => selectSettingsLens(lens));
  } else {
    const notifications = profile.notifications || {};
    $('settingsPanel').innerHTML = `<fieldset><legend>In-app notifications</legend><label><input id="settingsArchitectureNotifications" type="checkbox"${notifications.architectureApprovals !== false ? ' checked' : ''} />Architecture approvals</label><label><input id="settingsBlockedNotifications" type="checkbox"${notifications.blockedTasks !== false ? ' checked' : ''} />Blocked tasks</label></fieldset><p class="muted">This prototype stores notification preferences only in this browser.</p>`;
  }
  closeTopMenus();
  showDialog('accountSettingsDialog', $('accountBtn'));
}

function selectSettingsLens(lens) {
  document.querySelectorAll('[data-settings-lens]').forEach((button) => {
    const selected = button.dataset.settingsLens === lens;
    button.setAttribute('aria-checked', String(selected));
    button.tabIndex = selected ? 0 : -1;
  });
}

function saveAccountSettings(event) {
  event.preventDefault();
  const section = state.experience.settingsSection;
  if (section === 'profile') {
    const name = $('settingsName').value.trim();
    if (!name) {
      $('settingsNameError').textContent = 'Enter your name.';
      $('settingsNameError').setAttribute('role', 'alert');
      return;
    }
    prototype.updateCurrentProfile(localStorage, {name});
  } else if (section === 'preferences') {
    const lens = document.querySelector('[data-settings-lens][aria-checked="true"]')?.dataset.settingsLens;
    if (lens) prototype.updateCurrentProfile(localStorage, {defaultLens: lens});
  } else {
    prototype.updateCurrentProfile(localStorage, {notifications: {
      architectureApprovals: $('settingsArchitectureNotifications').checked,
      blockedTasks: $('settingsBlockedNotifications').checked,
    }});
  }
  $('accountSettingsDialog').close();
  renderAccountIdentity();
  renderNotifications();
}

function render() {
  $('emptyState').classList.add('hidden');
  $('workspace').classList.remove('hidden');
  $('workspaceHome').classList.add('hidden');
  $('workspaceSwitcherBtn').removeAttribute('aria-current');
  renderProjectTree();

  const projectGoal = String(state.project.goal || '').trim();
  $('welcomeTitle').textContent = state.project.name;
  $('overviewGoalExcerpt').textContent = goalExcerpt(projectGoal) || 'No Goal has been supplied.';
  $('overviewGoalFull').textContent = projectGoal || 'No Goal has been supplied.';
  $('projectStatus').textContent = formatTaskEnum(state.project.status, 'Active');
  const activeView = views[state.currentView] ? state.currentView : 'overview';
  state.currentView = activeView;
  syncDocumentTitle();
  $('pageTitle').textContent = views[activeView].title;
  $('pageSubtitle').textContent = views[activeView].subtitle;

  const awaiting = Number(state.architecture?.version || 0) === 0;
  $('bootstrapPanel').classList.toggle('hidden', !awaiting);
  $('globalAgentDock').classList.toggle('hidden', awaiting);
  $('bootstrapGoal').textContent = projectGoal;
  syncInitialArchitectureAction();

  const ready = state.tasks.filter((task) => task.status === 'TODO' && (task.owner === 'HUMAN' || task.owner === 'UNASSIGNED'));
  const running = state.tasks.filter((task) => task.status === 'IN_PROGRESS');
  const pending = state.proposals.filter((proposal) => isProposalActionable(proposal, state.architecture));
  const needsYou = prototype.deriveNeedsYou(
    state.proposals,
    state.tasks,
    prototype.currentProfile(localStorage)?.notifications,
    state.architecture?.version,
  );

  $('readyCount').textContent = `${ready.length} ready task${ready.length === 1 ? '' : 's'} ↗`;
  $('runningCount').textContent = `${running.length} in progress ↗`;
  $('needsCount').textContent = formatOverviewAttentionLabel(needsYou.length);
  const needsSummary = $('needsSummary');
  needsSummary.classList.toggle('hidden', needsYou.length === 0);
  needsSummary.setAttribute('aria-label', formatOverviewAttentionLabel(needsYou.length).replace(' ↗', ''));
  needsSummary.onclick = needsYou.length
    ? () => void openAttentionItem(needsYou[0].kind, needsYou[0].id)
    : null;

  $('graphVersion').textContent = `v${state.diagram?.architectureVersion ?? state.architecture.version}`;
  $('graphReviewState').textContent = pending.length
    ? `${pending.length} item${pending.length === 1 ? '' : 's'} need review`
    : 'Aligned';
  $('architectureSummary').textContent = state.architecture.summary || 'No architecture generated yet.';
  $('overviewMessage').textContent = awaiting
    ? 'The Goal is saved. Architecture generation needs to complete before normal project updates begin.'
    : pending.length
      ? `${pending.length} architecture decision${pending.length === 1 ? '' : 's'} need your review.`
      : 'The current architecture has no pending approval boundary.';

  const canvasResource = state.workspaceAsync?.resources?.canvas || {};
  const architectureState = overviewArchitectureState({
    architecture: state.architecture,
    pending,
    diagram: state.diagram,
    diagramError: state.diagramError,
    resource: canvasResource,
  });
  $('archVersion').textContent = state.architecture.version
    ? `${architectureState.summaryLabel} · v${state.architecture.version} ↗`
    : `${architectureState.summaryLabel} ↗`;
  const architectureStatePill = $('architectureStatePill');
  architectureStatePill.textContent = architectureState.label;
  architectureStatePill.className = `status-pill architecture-state-pill ${architectureState.key}`;
  $('archVersionLabel').textContent = state.architecture.version
    ? `Accepted architecture · v${state.architecture.version}`
    : 'Accepted architecture · pending';
  $('overviewArchitectureMap').innerHTML = renderOverviewArchitectureMap(state.diagram, state.architecture, {
    pending,
    diagramError: state.diagramError,
    resource: canvasResource,
  });
  wireGoButtons();

  document.querySelectorAll('.view').forEach((view) => view.classList.remove('active'));
  $(`view-${activeView}`).classList.add('active');

  renderWorkspaceTabs();
  renderTasks();
  renderProposals();
  renderNotifications();
  renderAccountIdentity();
  renderGraph();
  renderRecentActivity();
  renderLastRun();
  renderGlobalAgentReply();
  updateInstructionContext();
}

function renderTasks() {
  const focusedTaskId = document.activeElement?.dataset?.taskOpen || null;
  const focusedTaskOrigin = document.activeElement?.dataset?.taskOrigin || null;
  const order = {IN_PROGRESS: 0, TODO: 1, BLOCKED: 2, DONE: 3};
  const sorted = [...state.tasks].sort((a, b) => order[a.status] - order[b.status]);
  $('taskTotal').textContent = `${sorted.length} task${sorted.length === 1 ? '' : 's'}`;
  $('taskList').innerHTML = sorted.length ? sorted.map((task) => taskRow(task, true, 'tasks')).join('') : '<p class="muted">No tasks yet.</p>';
  $('overviewTasks').innerHTML = sorted.filter((t) => t.status !== 'DONE').slice(0, 3).map((task) => taskRow(task, false, 'overview')).join('') || '<p class="muted">No active tasks.</p>';
  document.querySelectorAll('[data-task-open]').forEach((button) => {
    const open = () => openTaskDetails(button.dataset.taskOpen, button.dataset.taskOrigin || state.currentView);
    button.addEventListener('click', open);
    // Native button activation dispatches Space on keyup. A concurrent workspace
    // projection can replace the focused task row between keydown and keyup,
    // losing that click. Commit the Space activation on keydown so rerenders can
    // restore focus to the replacement without dropping the person's action.
    button.addEventListener('keydown', (event) => {
      if ((event.key !== ' ' && event.key !== 'Spacebar') || event.repeat) return;
      event.preventDefault();
      open();
    });
  });
  document.querySelectorAll('[data-task-action]').forEach((btn) => btn.addEventListener('click', (event) => {
    event.preventDefault();
    event.stopPropagation();
    updateTask(btn.dataset.taskId, btn.dataset.taskAction);
  }));
  renderTaskDetails();
  renderWorkspaceTabs();
  if (focusedTaskId) {
    const container = focusedTaskOrigin === 'overview' ? '#overviewTasks' : '#taskList';
    document.querySelector(`${container} [data-task-open="${CSS.escape(focusedTaskId)}"]`)?.focus({preventScroll:true});
  }
}

function openTaskDetails(taskId, originView = state.currentView, {preserveOrigin = false} = {}) {
  const task = state.tasks.find((item) => item.id === taskId);
  if (!task) return false;
  if (!preserveOrigin || !state.taskDetailOrigin) state.taskDetailOrigin = {view: originView, taskId};
  state.taskDetailId = task.id;
  state.selectedTaskId = task.id;
  state.selectedProposalId = null;
  state.selectedComponentId = null;
  updateInstructionContext();
  renderTasks();
  setTimeout(() => $('taskDetailClose')?.focus(), 0);
  return true;
}

function closeTaskDetails({restoreFocus = true} = {}) {
  const origin = state.taskDetailOrigin;
  state.taskDetailId = null;
  state.taskDetailOrigin = null;
  renderTasks();
  if (!restoreFocus) return;
  if (origin?.view && origin.view !== state.currentView) switchView(origin.view);
  const container = origin?.view === 'overview' ? '#overviewTasks' : '#taskList';
  setTimeout(() => document.querySelector(`${container} [data-task-open="${CSS.escape(origin?.taskId || '')}"]`)?.focus(), 0);
}

function taskActionForStatus(task) {
  if (WEBMCP_AGENT_MODE) return null;
  const updating = state.taskUpdating.has(task.id);
  if (task.status === 'TODO') return {action: 'start', label: updating ? 'Starting…' : 'Start task'};
  if (task.status === 'IN_PROGRESS') return {action: 'done', label: updating ? 'Saving…' : 'Mark done'};
  if (task.status === 'DONE') return {action: 'reopen', label: updating ? 'Reopening…' : 'Reopen'};
  return {action: 'blocked', label: 'Blocked', disabled: true};
}

function taskActionMarkup(task) {
  const action = taskActionForStatus(task);
  if (!action) return '';
  const data = action.action === 'blocked' ? '' : ` data-task-action="${action.action}"`;
  return `<button class="task-row-action${action.action === 'blocked' ? ' is-blocked' : ''}" type="button" data-task-id="${escapeHtml(task.id)}"${data} ${action.disabled || state.taskUpdating.has(task.id) ? 'disabled' : ''}>${action.label}</button>`;
}

async function navigateTaskToArchitecture(taskId) {
  const task = state.tasks.find((item) => item.id === taskId);
  if (!task?.related_component) return false;
  const node = findArchitectureNode(task.related_component);
  if (!node) {
    toast(`Architecture component not found for ${task.title}.`, true);
    return false;
  }
  switchView('architecture');
  const parentScopeComponentId = findArchitectureParentId(node.id);
  const opened = await navigateGraphScope(parentScopeComponentId ?? null, {focusComponentId:node.id});
  if (!opened || !diagramNodeByComponentId(node.id)) return false;
  state.selectedComponentId = node.id;
  state.graphFocusMode = 'connected';
  renderGraph();
  setTimeout(() => document.querySelector(`[data-component="${CSS.escape(node.id)}"]`)?.focus(), 0);
  return true;
}

function taskRow(t, selectable = false, originView = 'tasks') {
  const selected = selectable && state.selectedTaskId === t.id;
  const detailSelected = state.taskDetailId === t.id;
  const action = taskActionMarkup(t);
  return `<article class="task-row${selected ? ' context-selected' : ''}${detailSelected ? ' detail-selected' : ''}"><i class="status-dot ${statusClass(t.status)}" aria-hidden="true"></i><div class="task-row-main"><button class="task-open" type="button" data-task-open="${escapeHtml(t.id)}" data-task-origin="${escapeHtml(originView)}" aria-expanded="${detailSelected}" aria-controls="taskDetailPanel" aria-label="Open details for ${escapeHtml(t.title)}"><span class="task-row-copy"><strong>${escapeHtml(t.title)}</strong><span class="task-row-meta"><span>Owner: ${escapeHtml(formatTaskEnum(t.owner, 'Unassigned'))}</span><span>Source: ${escapeHtml(formatTaskEnum(t.source, 'Not provided'))}</span></span><span class="status-pill ${escapeHtml(t.status)}">${escapeHtml(formatTaskEnum(t.status))}</span></span><span class="task-row-chevron" aria-hidden="true">›</span></button><div class="task-row-actions">${action}</div></div></article>`;
}

async function updateTask(taskId, action) {
  const task = state.tasks.find((t) => t.id === taskId);
  if (!task || state.taskUpdating.has(taskId)) return;
  const status = {start: 'IN_PROGRESS', done: 'DONE', reopen: 'TODO'}[action];
  if (!status) return;
  state.taskUpdating.add(taskId);
  renderTasks();
  try {
    const result = await sendEvent(
      'TASK_UPDATED',
      {task_id: task.id, title: task.title, status, message: `Task "${task.title}" changed to ${status}. Treat this as observed human project state.`},
      status === 'DONE' ? 'Saving completed task…' : status === 'TODO' ? 'Reopening task…' : 'Starting task…',
    );
    state.taskActionNotice = result?.result === 'SUCCESS'
      ? {taskId, kind: 'success', text: `${task.title} is now ${formatTaskEnum(status)}.`}
      : {taskId, kind: 'error', text: 'The task update could not be saved. Try again.'};
  } finally {
    state.taskUpdating.delete(taskId);
    renderTasks();
  }
}

function taskDetailComponentMarkup(task) {
  if (!task.related_component) {
    return '<p class="task-detail-empty">No related component supplied.</p>';
  }
  const component = findArchitectureNode(task.related_component);
  if (!component) {
    return `<p class="task-detail-empty">Component unavailable · <code>${escapeHtml(task.related_component)}</code></p>`;
  }
  return `<div class="task-detail-component"><div><strong>${escapeHtml(component.name || component.id)}</strong><span>${escapeHtml(component.type || 'Component')}${component.responsibility ? ` · ${escapeHtml(component.responsibility)}` : ''}</span></div><button class="link-btn" type="button" data-task-component-open="${escapeHtml(task.id)}">Open component ↗</button></div>`;
}

function renderTaskDetails() {
  const panel = $('taskDetailPanel');
  const body = $('taskDetailBody');
  if (!panel || !body) return;
  const task = state.tasks.find((item) => item.id === state.taskDetailId);
  if (!task || state.currentView === 'architecture' || (state.currentView === 'tasks' && state.workspaceTab !== 'tasks')) {
    panel.classList.add('hidden');
    panel.hidden = true;
    body.innerHTML = '';
    return;
  }
  panel.classList.remove('hidden');
  panel.hidden = false;
  $('taskDetailTitle').textContent = task.title || 'Untitled task';
  const criteria = (Array.isArray(task.acceptance_criteria) ? task.acceptance_criteria : []).map((criterion) => String(criterion || '').trim()).filter(Boolean);
  body.innerHTML = `<div class="task-detail-status"><span class="status-pill ${escapeHtml(task.status)}">${escapeHtml(formatTaskEnum(task.status))}</span><code>${escapeHtml(task.id)}</code></div><p class="task-detail-description">${escapeHtml(task.description || 'No description supplied.')}</p><dl class="task-detail-facts"><div><dt>Owner</dt><dd>${escapeHtml(formatTaskEnum(task.owner, 'Unassigned'))}</dd></div><div><dt>Source</dt><dd>${escapeHtml(formatTaskEnum(task.source, 'Not provided'))}</dd></div><div><dt>Status</dt><dd>${escapeHtml(formatTaskEnum(task.status))}</dd></div><div><dt>Related component</dt><dd>${task.related_component ? `<code>${escapeHtml(task.related_component)}</code>` : 'Not supplied'}</dd></div></dl><section class="task-detail-section"><h3>Acceptance criteria</h3>${criteria.length ? `<ol class="task-criteria">${criteria.map((criterion) => `<li>${escapeHtml(criterion)}</li>`).join('')}</ol>` : '<p class="task-detail-empty">No acceptance criteria supplied.</p>'}</section><section class="task-detail-section"><h3>Dependencies</h3><div class="task-dependency-list">${(() => { const dependencies = resolveTaskDependencies(task, state.tasks); return dependencies.length ? dependencies.map((dependency) => dependency.available ? `<button class="task-dependency" type="button" data-task-dependency="${escapeHtml(dependency.id)}"><span>${escapeHtml(dependency.title)}</span><span class="status-pill ${escapeHtml(dependency.status)}">${escapeHtml(formatTaskEnum(dependency.status))}</span></button>` : `<p class="task-detail-empty">Dependency unavailable · <code>${escapeHtml(dependency.id || 'No dependency ID supplied')}</code></p>`).join('') : '<p class="task-detail-empty">No dependencies listed.</p>'; })()}</div></section><section class="task-detail-section"><h3>Related architecture component</h3>${taskDetailComponentMarkup(task)}</section>${state.taskActionNotice?.taskId === task.id ? `<p class="task-action-notice ${state.taskActionNotice.kind}" role="status">${escapeHtml(state.taskActionNotice.text)}</p>` : ''}`;
  const action = taskActionForStatus(task);
  const actionButton = $('taskDetailAction');
  actionButton.disabled = Boolean(action?.disabled || state.taskUpdating.has(task.id));
  actionButton.hidden = !action;
  actionButton.textContent = action?.label || '';
  actionButton.dataset.taskId = task.id;
  if (action && action.action !== 'blocked') actionButton.dataset.taskAction = action.action;
  else delete actionButton.dataset.taskAction;
  body.querySelectorAll('[data-task-dependency]').forEach((button) => button.addEventListener('click', () => openTaskDetails(button.dataset.taskDependency, state.taskDetailOrigin?.view || state.currentView, {preserveOrigin: true})));
  body.querySelector('[data-task-component-open]')?.addEventListener('click', async () => {
    const taskId = body.querySelector('[data-task-component-open]').dataset.taskComponentOpen;
    state.taskDetailId = null;
    state.taskDetailOrigin = null;
    renderTasks();
    await navigateTaskToArchitecture(taskId);
  });
}

function proposalStatusLabel(status) {
  return formatTaskEnum(status, 'Status not provided');
}

function proposalComponentLabel(componentId) {
  const id = String(componentId || '').trim();
  if (!id) return 'Component ID not supplied';
  const component = findArchitectureNode(id);
  return component ? `${component.name || id} · ${id}` : `Unavailable component · ${id}`;
}

function proposalEvidenceMarkup(proposal) {
  const evidence = (Array.isArray(proposal.evidence) ? proposal.evidence : []).map((item) => String(item || '').trim()).filter(Boolean);
  const eventIds = Array.isArray(proposal.evidence_event_ids) ? proposal.evidence_event_ids : [];
  const events = new Map((state.activity || []).map((event) => [String(event?.id || event?.event_id || ''), event]));
  const strings = evidence.length
    ? `<ul class="proposal-evidence-list">${evidence.map((item) => `<li>${escapeHtml(item)}</li>`).join('')}</ul>`
    : '<p class="proposal-empty">No evidence strings supplied.</p>';
  const references = eventIds.length
    ? `<div class="proposal-evidence-events">${eventIds.map((eventId) => {
      const id = String(eventId || '').trim();
      const event = events.get(id);
      return event
        ? `<details><summary>Evidence event · ${escapeHtml(id)}</summary><pre>${escapeHtml(JSON.stringify(event, null, 2))}</pre></details>`
        : `<p class="proposal-unresolved">Evidence reference unavailable · <code>${escapeHtml(id || 'No event ID supplied')}</code></p>`;
    }).join('')}</div>`
    : '';
  return `${strings}${references}`;
}

function proposalPreviewKey(proposal) {
  return `${state.projectId || ''}|${proposal?.id || ''}|${Number(proposal?.base_architecture_version)}|${Number(state.architecture?.version)}`;
}

function proposalPreviewEntry(proposal) {
  return state.proposalPreviews.get(proposalPreviewKey(proposal)) || null;
}

function previewArchitectureSnapshot(label, components, relationships) {
  return `<details class="proposal-preview-snapshot"><summary>${escapeHtml(label)}</summary><h5>Components</h5><pre>${escapeHtml(JSON.stringify(components || [], null, 2))}</pre><h5>Relationships</h5><pre>${escapeHtml(JSON.stringify(relationships || [], null, 2))}</pre></details>`;
}

function proposalPreviewMarkup(proposal) {
  if (!isProposalActionable(proposal, state.architecture)) {
    const stale = proposal.status === 'PENDING';
    return `<p class="proposal-limitation" role="note">${escapeHtml(stale
      ? `This proposal targets architecture v${proposal.base_architecture_version ?? 'unknown'}, while the accepted architecture is v${state.architecture?.version ?? 'unknown'}. It is stale and cannot be applied.`
      : (proposal.resolution_reason || 'This proposal is a read-only decision record.'))}</p>`;
  }
  const entry = proposalPreviewEntry(proposal);
  if (!entry || entry.status === 'loading') {
    return '<p class="proposal-preview-state" role="status">Loading the server-owned acceptance preview…</p>';
  }
  if (entry.status === 'error') {
    return `<div class="proposal-preview-state error" role="alert"><p>Acceptance preview unavailable: ${escapeHtml(entry.error || 'Unknown preview error')}</p><button class="link-btn" type="button" data-retry-proposal-preview="${escapeHtml(proposal.id)}">Retry preview</button></div>`;
  }
  const preview = entry.data;
  const taskUpdates = (preview.task_updates || []).map((change) => `<li><strong>${escapeHtml(change.after?.title || change.task_id)}</strong><span>${escapeHtml((change.changed_fields || []).map((field) => formatTaskEnum(field)).join(', ') || 'No tracked field changes')}</span>${change.blocked ? '<span class="status-pill BLOCKED">Blocked</span>' : ''}${change.remapped ? `<span>Component: ${escapeHtml(change.before?.related_component || 'none')} → ${escapeHtml(change.after?.related_component || 'none')}</span>` : ''}</li>`).join('');
  const created = (preview.created_tasks || []).map((task) => `<li><strong>${escapeHtml(task.title)}</strong><span>${escapeHtml(task.description || '')}</span><span>Component: ${escapeHtml(task.related_component || 'none')}</span></li>`).join('');
  const superseded = (preview.superseded_proposals || []).map((item) => `<li><code>${escapeHtml(item.proposal_id)}</code> · ${escapeHtml(item.reason)}</li>`).join('');
  const warnings = (preview.warnings || []).map((warning) => `<li>${escapeHtml(warning)}</li>`).join('');
  return `<div class="proposal-acceptance-preview" data-preview-version="${escapeHtml(preview.current_architecture_version)}"><p class="proposal-preview-version"><strong>Server acceptance plan</strong>Architecture v${escapeHtml(preview.current_architecture_version)} → v${escapeHtml(preview.resulting_architecture_version)}</p>${previewArchitectureSnapshot('Current accepted architecture', preview.components_before, preview.relationships_before)}${previewArchitectureSnapshot('Architecture after acceptance', preview.components_after, preview.relationships_after)}<h5>Task updates</h5>${taskUpdates ? `<ul class="proposal-preview-list">${taskUpdates}</ul>` : '<p class="proposal-empty">No existing tasks change.</p>'}<h5>Created tasks</h5>${created ? `<ul class="proposal-preview-list">${created}</ul>` : '<p class="proposal-empty">No tasks are created.</p>'}<h5>Proposals superseded by this acceptance</h5>${superseded ? `<ul class="proposal-preview-list">${superseded}</ul>` : '<p class="proposal-empty">No peer proposals are superseded.</p>'}${warnings ? `<h5>Warnings</h5><ul class="proposal-preview-list warnings">${warnings}</ul>` : ''}</div>`;
}

async function ensureProposalPreview(proposal, {force = false} = {}) {
  if (!isProposalActionable(proposal, state.architecture) || !state.projectId) return null;
  const key = proposalPreviewKey(proposal);
  const existing = state.proposalPreviews.get(key);
  if (!force && existing && (existing.status === 'loading' || existing.status === 'ready')) return existing.data || null;
  const serial = ++state.proposalPreviewSerial;
  const guard = captureNavigationGuard(state.projectId);
  state.proposalPreviews.set(key, {status:'loading', data:null, error:null, serial});
  renderProposals();
  try {
    const preview = await api(`/projects/${state.projectId}/architecture/proposals/${encodeURIComponent(proposal.id)}/acceptance-preview`);
    if (!committedProjectGuardIsCurrent(guard) || proposalPreviewKey(proposal) !== key) return null;
    if (!preview?.actionable
      || preview.proposal_id !== proposal.id
      || Number(preview.current_architecture_version) !== Number(state.architecture?.version)
      || Number(preview.resulting_architecture_version) !== Number(state.architecture?.version) + 1) {
      throw new Error('The preview response is stale or does not match the selected proposal.');
    }
    state.proposalPreviews.set(key, {status:'ready', data:preview, error:null, serial});
    renderProposals();
    return preview;
  } catch (error) {
    if (committedProjectGuardIsCurrent(guard) && state.proposalPreviews.get(key)?.serial === serial) {
      state.proposalPreviews.set(key, {status:'error', data:null, error:error?.message || String(error), serial});
      renderProposals();
    }
    return null;
  }
}

function proposalAffectedComponentsMarkup(proposal) {
  const affected = Array.isArray(proposal.affected_components) ? proposal.affected_components : [];
  if (!affected.length) return '<p class="proposal-empty">No affected components supplied.</p>';
  return `<ul class="proposal-affected-list">${affected.map((item) => {
    const id = typeof item === 'object' ? item?.id : item;
    const label = typeof item === 'object' && item?.name && !findArchitectureNode(id) ? `${item.name} · ${id || 'No component ID supplied'}` : proposalComponentLabel(id);
    return findArchitectureNode(id)
      ? `<li><button class="proposal-component-link" type="button" data-proposal-component="${escapeHtml(id)}">${escapeHtml(label)} ↗</button></li>`
      : `<li><span class="proposal-unresolved">${escapeHtml(label)}</span></li>`;
  }).join('')}</ul>`;
}

function proposalReviewMarkup(proposal) {
  const title = proposal.title || proposal.reason || 'Architecture proposal';
  const reason = proposal.reason || 'No reason supplied.';
  const actionable = isProposalActionable(proposal, state.architecture);
  const recommended = proposal.recommended_option ? `<p class="proposal-recommended"><strong>Recommended option</strong>${escapeHtml(proposal.recommended_option)}</p>` : '';
  return `<article class="proposal-card${state.selectedProposalId === proposal.id ? ' context-selected' : ''}${actionable ? '' : ' is-non-actionable'}" data-proposal-card="${escapeHtml(proposal.id)}"><header class="proposal-card-header"><button class="proposal-card-select" type="button" data-proposal-select="${escapeHtml(proposal.id)}" aria-pressed="${state.selectedProposalId === proposal.id}" ${actionable ? '' : 'disabled aria-disabled="true"'}><span class="proposal-kicker">Proposal</span><h3>${escapeHtml(title)}</h3></button><span class="status-pill ${escapeHtml(proposal.status)}">${escapeHtml(proposalStatusLabel(proposal.status))}</span></header><dl class="proposal-facts"><div><dt>Reason</dt><dd>${escapeHtml(reason)}</dd></div><div><dt>Status</dt><dd>${escapeHtml(proposalStatusLabel(proposal.status))}</dd></div><div><dt>Base architecture</dt><dd>Version ${escapeHtml(proposal.base_architecture_version ?? 'not supplied')}</dd></div></dl><p class="proposal-observed"><strong>Observed change</strong>${escapeHtml(proposal.observed_change || 'No observed change supplied.')}</p><section class="proposal-review-section"><h4>Acceptance preview</h4>${proposalPreviewMarkup(proposal)}</section><section class="proposal-review-section"><h4>Evidence</h4>${proposalEvidenceMarkup(proposal)}</section><section class="proposal-review-section"><h4>Impact</h4><p>${escapeHtml(proposal.impact || 'No impact supplied.')}</p>${recommended}</section><section class="proposal-review-section"><h4>Affected components</h4>${proposalAffectedComponentsMarkup(proposal)}</section></article>`;
}

function renderProposalDecisionBar(proposal) {
  const bar = $('proposalDecisionBar');
  if (!bar) return;
  const label = $('proposalDecisionLabel');
  const description = $('proposalDecisionDescription');
  const notice = $('proposalDecisionNotice');
  const actions = bar.querySelector('.proposal-decision-actions');
  const accept = bar.querySelector('[data-proposal-decision="accept"]');
  const reject = bar.querySelector('[data-proposal-decision="reject"]');
  bar.classList.toggle('hidden', !proposal);
  if (!proposal) return;
  const updating = state.proposalUpdating.has(proposal.id);
  const actionable = isProposalActionable(proposal, state.architecture);
  const resolved = !actionable;
  const preview = proposalPreviewEntry(proposal);
  label.textContent = proposal.status === 'ACCEPTED'
    ? 'Accepted'
    : proposal.status === 'REJECTED'
      ? 'Kept current'
      : proposal.status === 'SUPERSEDED'
        ? 'Superseded'
        : actionable ? 'Your decision' : 'Stale proposal';
  description.textContent = proposal.status === 'ACCEPTED'
    ? 'This proposal was accepted. Review remains available as a record of the decision.'
    : proposal.status === 'REJECTED'
      ? 'The current architecture was kept. Review remains available as a record of the decision.'
      : proposal.status === 'SUPERSEDED'
        ? (proposal.resolution_reason || 'A different accepted proposal advanced the architecture, so this proposal can no longer be applied.')
        : actionable
          ? 'Acceptance is enabled only after the current server reconciliation preview loads successfully.'
          : `This proposal targets v${proposal.base_architecture_version ?? 'unknown'} and cannot change accepted architecture v${state.architecture?.version ?? 'unknown'}.`;
  const decisionNotice = state.proposalDecisionNotice?.id === proposal.id ? state.proposalDecisionNotice : null;
  notice.textContent = decisionNotice?.text || '';
  notice.className = `proposal-decision-notice${decisionNotice?.kind ? ` ${decisionNotice.kind}` : ''}`;
  actions.classList.toggle('hidden', resolved);
  accept.disabled = updating || preview?.status !== 'ready';
  reject.disabled = updating;
  accept.textContent = updating ? 'Saving…' : preview?.status === 'loading' ? 'Preparing preview…' : 'Accept changes';
  reject.textContent = updating ? 'Saving…' : 'Keep Current';
}

function renderProposals() {
  const focusedProposalId = document.activeElement?.dataset?.proposalSelect || null;
  if (state.currentView === 'tasks' && state.workspaceTab === 'review') applyWorkspaceTabInvariants('review');
  const pending = state.proposals.filter((p) => isProposalActionable(p, state.architecture));
  $('overviewAttention').innerHTML = pending.length
    ? `<div class="attention-card"><strong>${escapeHtml(pending[0].reason || 'Architecture proposal')}</strong><p>${escapeHtml(pending[0].observed_change || 'Review the supplied architecture change.')}</p><div class="actions"><button class="btn secondary" data-open-proposal="${escapeHtml(pending[0].id)}">Review change</button></div></div>`
    : '<p>No pending architecture decision. The agent can maintain task/status state without asking you to approve normal aligned updates.</p>';
  const proposalList = $('proposalList');
  const selected = state.proposals.find((proposal) => proposal.id === state.selectedProposalId && isProposalActionable(proposal, state.architecture)) || null;
  if (proposalList) proposalList.innerHTML = state.proposals.length
    ? state.proposals.map((proposal) => proposalReviewMarkup(proposal)).join('')
    : '<article class="panel"><h3>No architecture review needed</h3><p class="muted">Normal aligned project updates stay ambient and do not interrupt the human.</p></article>';
  renderProposalDecisionBar(selected);
  document.querySelectorAll('[data-open-proposal]').forEach((button) => button.addEventListener('click', () => openAttentionItem('proposal', button.dataset.openProposal)));
  bindProposalDecisionControls(
    document.querySelectorAll('[data-proposal-decision]'),
    () => state.selectedProposalId,
    decideProposal,
  );
  document.querySelectorAll('[data-proposal-select]').forEach((button) => button.addEventListener('click', () => {
    switchWorkspaceTab('review', {focusedProposalId:button.dataset.proposalSelect, historyMode:'replace'});
    setTimeout(() => document.querySelector(`[data-proposal-select="${CSS.escape(state.selectedProposalId)}"]`)?.focus(), 0);
  }));
  document.querySelectorAll('[data-retry-proposal-preview]').forEach((button) => button.addEventListener('click', () => {
    const proposal = state.proposals.find((item) => item.id === button.dataset.retryProposalPreview);
    if (proposal) void ensureProposalPreview(proposal, {force:true});
  }));
  document.querySelectorAll('[data-proposal-component]').forEach((button) => button.addEventListener('click', async () => {
    const component = findArchitectureNode(button.dataset.proposalComponent);
    if (!component) {
      toast(`Architecture component not found: ${button.dataset.proposalComponent}.`, true);
      return;
    }
    switchView('architecture');
    await navigateGraphScope(findArchitectureParentId(component.id) ?? null, {focusComponentId: component.id});
  }));
  renderWorkspaceTabs();
  if (focusedProposalId) {
    const replacement = document.querySelector(`[data-proposal-select="${CSS.escape(focusedProposalId)}"]`);
    if (replacement && !replacement.disabled) replacement.focus({preventScroll:true});
  }
  if (selected && state.currentView === 'tasks' && state.workspaceTab === 'review') void ensureProposalPreview(selected);
}

async function readBackProposalDecision(projectId, proposalId, decision, guard) {
  const [proposalResult, architectureResult] = await Promise.allSettled([
    api(`/projects/${projectId}/architecture/proposals`),
    api(`/projects/${projectId}/architecture`),
  ]);
  if (!committedProjectGuardIsCurrent(guard)
    || proposalResult.status !== 'fulfilled'
    || architectureResult.status !== 'fulfilled') {
    return {outcome:'UNKNOWN', retryable:false, proposal:null, architecture:null};
  }
  const proposals = proposalResult.value;
  const architecture = architectureResult.value;
  const proposal = proposals.find((item) => item.id === proposalId) || null;
  state.proposals = proposals;
  state.architecture = architecture;
  if (state.currentView === 'tasks') applyWorkspaceTabInvariants(state.workspaceTab);
  const expectedStatus = decision === 'accept' ? 'ACCEPTED' : 'REJECTED';
  if (proposal?.status === expectedStatus) {
    return {outcome:'COMMITTED', retryable:false, proposal, architecture};
  }
  if (proposal?.status === 'PENDING' && isProposalActionable(proposal, architecture)) {
    return {outcome:'NOT_COMMITTED', retryable:true, proposal, architecture};
  }
  if (proposal) return {outcome:'NOT_COMMITTED', retryable:false, proposal, architecture};
  return {outcome:'UNKNOWN', retryable:false, proposal:null, architecture};
}

async function decideProposal(id, decision) {
  const proposal = state.proposals.find((item) => item.id === id);
  if (!['accept', 'reject'].includes(decision)) return {outcome:'NOT_COMMITTED', retryable:false, error:'Unsupported proposal decision.'};
  if (!proposal) return {outcome:'NOT_COMMITTED', retryable:false, error:'Proposal not found.'};
  if (!isProposalActionable(proposal, state.architecture)) return {outcome:'NOT_COMMITTED', retryable:false, proposal, error:'Proposal is not actionable against the current architecture.'};
  if (state.proposalUpdating.has(id)) return {outcome:'UNKNOWN', retryable:false, proposal, error:'A decision outcome is already being reconciled.'};
  if (decision === 'accept' && proposalPreviewEntry(proposal)?.status !== 'ready') {
    void ensureProposalPreview(proposal);
    return {outcome:'NOT_COMMITTED', retryable:true, proposal, error:'A current successful acceptance preview is required.'};
  }
  const projectId = state.projectId;
  const guard = captureNavigationGuard(projectId);
  if (!projectId || !committedProjectGuardIsCurrent(guard)) return {outcome:'UNKNOWN', retryable:false, proposal, error:'Project navigation is not stable.'};
  state.proposalUpdating.add(id);
  state.proposalDecisionNotice = {id, kind: 'pending', text: 'Saving decision…'};
  renderProposals();
  const workingRequestId = beginWorkingRequest('', {projectId});
  let mutationResponse = null;
  let mutationError = null;
  try {
    mutationResponse = await api(`/projects/${projectId}/architecture/proposals/${id}/${decision}`, {method: 'POST'});
  } catch (error) {
    mutationError = error;
  }
  try {
    let result = null;
    if (mutationResponse && committedProjectGuardIsCurrent(guard)) {
      const refreshed = await refresh({projectId, guard});
      if (refreshed) result = {outcome:'COMMITTED', retryable:false, proposal:mutationResponse, architecture:state.architecture};
    }
    if (!result) result = await readBackProposalDecision(projectId, id, decision, guard);
    if (!committedProjectGuardIsCurrent(guard)) return {outcome:'UNKNOWN', retryable:false, proposal:null, error:'Navigation changed while reconciling the decision.'};
    if (result.outcome === 'COMMITTED') {
      state.proposalUpdating.delete(id);
      state.proposalDecisionNotice = {id, kind:'success', text:decision === 'accept' ? 'Architecture proposal accepted.' : 'Current architecture kept.'};
      toast(decision === 'accept' ? 'Architecture change accepted.' : 'Current architecture kept.');
    } else if (result.outcome === 'NOT_COMMITTED') {
      state.proposalUpdating.delete(id);
      const detail = mutationError?.message || (result.retryable ? 'The decision was proven not to have committed.' : 'The proposal is no longer actionable.');
      state.proposalDecisionNotice = {id, kind:'error', text:detail};
      toast(detail, true);
    } else {
      state.proposalDecisionNotice = {id, kind:'unknown', text:'The server outcome could not be verified. Controls remain disabled to prevent a duplicate decision.'};
      toast('Decision outcome unknown; no retry was sent.', true);
    }
    renderProposals();
    updateInstructionContext();
    return {...result, decision, transport_error:mutationError?.message || null};
  } finally {
    finishWorkingRequest(workingRequestId);
  }
}

function flattenArchitectureNodes(nodes = state.architecture?.components || []) {
  const flat = [];
  const visit = (items) => items.forEach((node) => {
    flat.push(node);
    visit(node.children || []);
  });
  visit(nodes);
  return flat;
}

function findArchitectureNode(id) {
  if (!id) return null;
  return flattenArchitectureNodes().find((node) => node.id === id) || null;
}

function findArchitectureParentId(id, nodes = state.architecture?.components || [], parentId = null) {
  for (const node of nodes) {
    if (node.id === id) return parentId;
    const nestedParent = findArchitectureParentId(id, node.children || [], node.id);
    if (nestedParent !== undefined) return nestedParent;
  }
  return undefined;
}

function descendantArchitectureIds(node) {
  const ids = [];
  const visit = (item) => {
    ids.push(item.id);
    (item.children || []).forEach(visit);
  };
  if (node) visit(node);
  return ids;
}

function architectureHealth(node) {
  const ids = new Set(descendantArchitectureIds(node));
  const tasks = state.tasks.filter((task) => task.related_component && ids.has(task.related_component));
  const blockedTasks = tasks.filter((task) => task.status === 'BLOCKED');
  const activeTasks = tasks.filter((task) => task.status === 'IN_PROGRESS');
  const badNodes = flattenArchitectureNodes([node]).filter((item) => /BLOCKED|DRIFT|ERROR|DEGRADED|MISMATCH/i.test(item.status || ''));
  const pendingReviews = state.proposals.filter((proposal) => {
    if (!isProposalActionable(proposal, state.architecture)) return false;
    const affected = proposal.affected_components || [];
    const changed = (proposal.proposed_changes || []).map((change) => change.component_id).filter(Boolean);
    return [...affected, ...changed].some((id) => ids.has(id));
  });

  if (blockedTasks.length || badNodes.length) {
    const parts = [];
    if (blockedTasks.length) parts.push(`${blockedTasks.length} blocked task${blockedTasks.length === 1 ? '' : 's'}`);
    if (badNodes.length) parts.push(`${badNodes.length} unhealthy node${badNodes.length === 1 ? '' : 's'}`);
    if (pendingReviews.length) parts.push(`${pendingReviews.length} review${pendingReviews.length === 1 ? '' : 's'}`);
    return {key: 'blocked', label: 'Blocked', detail: parts.join(' · '), needsAttention: true, tasks, blockedTasks, activeTasks, pendingReviews};
  }
  if (pendingReviews.length) {
    return {key: 'review', label: 'Needs review', detail: `${pendingReviews.length} architecture decision${pendingReviews.length === 1 ? '' : 's'} waiting for you`, needsAttention: true, tasks, blockedTasks, activeTasks, pendingReviews};
  }
  if (activeTasks.length) {
    return {key: 'active', label: 'Active', detail: `${activeTasks.length} task${activeTasks.length === 1 ? '' : 's'} in progress · no action needed`, needsAttention: false, tasks, blockedTasks, activeTasks, pendingReviews};
  }
  return {key: 'healthy', label: 'Healthy', detail: 'Aligned · no action needed', needsAttention: false, tasks, blockedTasks, activeTasks, pendingReviews};
}

function diagramNodeByComponentId(componentId, diagram = state.diagram) {
  return (diagram?.nodes || []).find((node) => node.component_id === componentId) || null;
}

function diagramNodeById(nodeId) {
  return (state.diagram?.nodes || []).find((node) => node.id === nodeId) || null;
}

function diagramNodeHealth(node) {
  const value = node?.status?.health || 'UNKNOWN';
  const visual = {
    BLOCKED: {key:'blocked', label:'Blocked', needsAttention:true},
    CHANGE_PENDING: {key:'review', label:'Review', needsAttention:true},
    IN_PROGRESS: {key:'active', label:'Active', needsAttention:false},
    DONE: {key:'healthy', label:'Done', needsAttention:false},
    TODO: {key:'planned', label:'Todo', needsAttention:false},
    PLANNED: {key:'planned', label:'Planned', needsAttention:false},
    UNKNOWN: {key:'planned', label:'Unknown', needsAttention:false},
  }[value] || {key:'planned', label:String(value), needsAttention:false};
  return {...visual, detail:(node?.supporting_text || []).join(' · ') || node?.status?.canonical_status || 'No additional status evidence.'};
}

function wrapGraphText(text, maxChars, maxLines = 2) {
  const source = String(text || '').trim();
  if (!source || maxChars < 2 || maxLines < 1) return [];
  const words = source.split(/\s+/).filter(Boolean);
  const lines = [];
  let line = '';
  let consumed = 0;
  const fitWord = (word) => word.length <= maxChars ? word : `${word.slice(0, Math.max(1, maxChars - 1))}…`;
  for (const rawWord of words) {
    if (lines.length >= maxLines) break;
    const word = fitWord(rawWord);
    const next = line ? `${line} ${word}` : word;
    if (line && next.length > maxChars) {
      lines.push(line);
      if (lines.length >= maxLines) break;
      line = word;
    } else {
      line = next;
      consumed += 1;
    }
  }
  if (line && lines.length < maxLines) lines.push(line);
  const rendered = lines.join(' ').replace(/…/g, '');
  if ((consumed < words.length || rendered.length < source.length - 2) && lines.length) {
    const last = lines.length - 1;
    const base = lines[last].replace(/[.…]+$/, '');
    lines[last] = `${base.slice(0, Math.max(1, maxChars - 1)).trimEnd()}…`;
  }
  return lines;
}

function graphNodeKindMarkup(node) {
  const kind = String(node.semantic_kind || 'COMPONENT');
  const type = String(node.semantic_type || '');
  const text = kind.toUpperCase() === type.toUpperCase() ? kind : `${kind} · ${type}`;
  const line = graphWrapPixels(text, node.width - 58, 1, '750 8.8px')[0] || 'COMPONENT';
  return `<text class="node-kind" x="${node.x+18}" y="${node.y+25}">${escapeHtml(line)}</text>`;
}

function graphFocusState(
  selectedNode,
  projectedEdges = state.diagram?.edges || [],
  projectedNodes = state.diagram?.nodes || [],
) {
  const tracePath = architectureTraceForNode(selectedNode);
  if (tracePath?.status === 'FOUND') {
    const nodes = new Set(
      (tracePath.nodes || []).map((node) => (
        node?.node_id
        || (node?.component_id ? `node:${node.component_id}` : null)
        || node?.id
      )).filter(Boolean),
    );
    nodes.add(selectedNode.id);
    const relationshipIds = new Set(
      (tracePath.relationships || []).map((relationship) => (
        relationship?.relationship_id || relationship?.id
      )).filter(Boolean),
    );
    const edges = new Set();
    projectedEdges.forEach((edge) => {
      if (
        relationshipIds.has(edge.id)
        || (edge.provenance || []).some((item) => relationshipIds.has(item.relationship_id))
      ) edges.add(edge.id);
    });
    return {nodes, edges, trace:true};
  }
  if (!selectedNode || state.graphFocusMode === 'all') return null;
  const nodes = new Set([selectedNode.id]);
  const edges = new Set();
  if (state.graphFocusMode === 'hierarchy' || state.graphFocusMode === 'isolate') {
    const diagramNodes = projectedNodes;
    const nodeById = new Map(diagramNodes.map((node) => [node.id,node]));
    const prefix = selectedNode.hierarchyPath?.length ? selectedNode.hierarchyPath : [selectedNode.id];
    const subtreeNodes = new Set();
    prefix.forEach((nodeId) => nodes.add(nodeId));
    diagramNodes.forEach((node) => {
      const path = node.hierarchyPath?.length ? node.hierarchyPath : [node.id];
      if (prefix.every((part,index)=>path[index]===part)) {
        nodes.add(node.id);
        subtreeNodes.add(node.id);
      }
    });
    projectedEdges.forEach((edge) => {
      if (nodes.has(edge.source) && nodes.has(edge.target)) edges.add(edge.id);
    });
    if (state.graphFocusMode !== 'isolate') return {nodes, edges};

    // Isolate is a local reading projection over the returned canonical graph:
    // keep the selected subtree, its ancestors, and exactly one-hop crossing
    // endpoints with their authored ancestor boundaries. No topology is
    // manufactured and no traversal continues through those context nodes.
    const boundaryNodes = new Set();
    projectedEdges.forEach((edge) => {
      const sourceInSubtree = subtreeNodes.has(edge.source);
      const targetInSubtree = subtreeNodes.has(edge.target);
      if (sourceInSubtree === targetInSubtree) return;
      edges.add(edge.id);
      const peerId = sourceInSubtree ? edge.target : edge.source;
      const peer = nodeById.get(peerId);
      const peerPath = peer?.hierarchyPath?.length ? peer.hierarchyPath : [peerId];
      peerPath.forEach((nodeId) => {
        if (!nodes.has(nodeId)) boundaryNodes.add(nodeId);
        nodes.add(nodeId);
      });
    });
    return {nodes, edges, isolate:true, boundaryNodes, subtreeNodes};
  }
  if (state.graphFocusMode === 'connected') {
    projectedEdges.forEach((edge) => {
      if (edge.source === selectedNode.id || edge.target === selectedNode.id) { edges.add(edge.id); nodes.add(edge.source); nodes.add(edge.target); }
    });
    return {nodes, edges};
  }
  const upstream = state.graphFocusMode === 'upstream';
  const visited = new Set([selectedNode.id]);
  const queue = [selectedNode.id];
  while (queue.length) {
    const current = queue.shift();
    projectedEdges.forEach((edge) => {
      const matches = upstream ? edge.target === current : edge.source === current;
      if (!matches) return;
      const peer = upstream ? edge.source : edge.target;
      edges.add(edge.id); nodes.add(peer);
      if (!visited.has(peer)) { visited.add(peer); queue.push(peer); }
    });
  }
  return {nodes, edges};
}

function graphScopeTrail(diagram = state.diagram) {
  const scope = diagram?.scope;
  if (!scope) return [];
  const trail = [...(scope.ancestorPath || [])];
  if (scope.componentId && !trail.some((item) => item.componentId === scope.componentId)) trail.push({componentId:scope.componentId,nodeId:scope.nodeId,label:scope.label});
  return trail;
}

function parentGraphScopeComponentId(diagram = state.diagram) {
  const scope = diagram?.scope;
  if (!scope?.componentId) return null;
  const trail = graphScopeTrail(diagram);
  const index = trail.findIndex((item) => item.componentId === scope.componentId);
  return index > 0 ? trail[index - 1].componentId : null;
}

function nextReadingModeForScope(currentMode, nextScopeComponentId) {
  void currentMode;
  void nextScopeComponentId;
  return 'MAP';
}

function graphNodeAction(node) {
  return node?.projectionRole === 'PRIMARY' && node.childCount > 0 ? 'drill' : 'inspect';
}

function toggleGraphNodeCollapse(nodeId) {
  if (!ARCHITECTURE_CANVAS_MODE) return false;
  const node = diagramNodeById(nodeId);
  if (!node || node.childCount < 1) return false;
  let selectionChanged = false;
  if (state.collapsedNodeIds.has(nodeId)) state.collapsedNodeIds.delete(nodeId);
  else {
    state.collapsedNodeIds.add(nodeId);
    const selected=diagramNodeByComponentId(state.selectedComponentId);
    if (selected && (selected.hierarchyPath || []).slice(0,-1).includes(nodeId)) {
      state.selectedComponentId = node.component_id;
      selectionChanged = true;
    }
  }
  state.selectedEdgeId = null;
  if (selectionChanged) syncArchitectureCanvasSelectionUrl();
  renderGraph();
  return true;
}

function expandAllGraphNodes() {
  if (!state.collapsedNodeIds.size) return false;
  state.collapsedNodeIds.clear();
  renderGraph();
  return true;
}

async function navigateGraphScope(scopeComponentId, {focusComponentId = state.scopeComponentId, loader = loadArchitectureDiagram, render = renderGraph, notify = toast} = {}) {
  if (!state.projectId || !state.architecture) return false;
  if (ARCHITECTURE_CANVAS_MODE) {
    return focusComponentId ? revealArchitectureComponent(focusComponentId, {surface:'canvas', render, notify}) : false;
  }
  const projectId = state.projectId;
  const architecture = state.architecture;
  const targetScope = scopeComponentId || null;
  const nextMode = nextReadingModeForScope(state.readingMode, targetScope);
  const contextTicket = currentWorkspaceContextTicket(state.workspaceAsync);
  const request = beginWorkspaceResource(state.workspaceAsync, 'canvas', contextTicket, {retainData:Boolean(state.diagram)});
  if (!request) return false;
  const transition = beginGraphTransition({projectId, architectureVersion:architecture.version, surface:'project', scopeComponentId:targetScope, readingMode:nextMode});
  try {
    const nextDiagram = cachedArchitectureView('project', projectId, architecture.version, targetScope, nextMode)
      || await loader(projectId, architecture, targetScope, nextMode);
    if (!workspaceResourceIsCurrent(state.workspaceAsync, request) || !graphTransitionIsCurrent(transition)) return false;
    if (!nextDiagram) throw new Error('Scoped diagram is unavailable.');
    const currentInteraction = captureGraphInteractionState();
    cacheArchitectureView('project', projectId, architecture.version, targetScope, nextMode, nextDiagram);
    state.diagram = nextDiagram;
    state.scopeComponentId = targetScope;
    state.readingMode = nextMode;
    state.diagramError = null;
    const reconciled = reconcileGraphInteractionState(nextDiagram, currentInteraction);
    if (!reconciled.selectedComponentId && focusComponentId && diagramNodeByComponentId(focusComponentId, nextDiagram)) {
      state.selectedComponentId = focusComponentId;
      state.selectedEdgeId = null;
      state.graphFocusMode = 'connected';
      state.inspectorTab = currentInteraction.inspectorTab;
    }
    settleWorkspaceResource(state.workspaceAsync, request, 'ready');
    render();
    if (typeof document !== 'undefined') setTimeout(() => {
      if (!graphTransitionIsCurrent(transition) || state.currentView !== 'architecture') return;
      const componentTarget = focusComponentId
        ? document.querySelector(`[data-component="${CSS.escape(focusComponentId)}"]`)
        : null;
      // The scope bar represents a root omitted from the scoped graph.
      // Keep keyboard focus on a visible control after reconciliation.
      (componentTarget || (targetScope ? document.querySelector('[data-graph-back]') : null))?.focus();
    }, 0);
    return true;
  } catch (err) {
    if (!workspaceResourceIsCurrent(state.workspaceAsync, request) || !graphTransitionIsCurrent(transition)) return false;
    architectureViewCache.delete(architectureViewCacheKey('project', projectId, architecture.version, targetScope, nextMode));
    state.diagramError = err?.message || String(err);
    settleWorkspaceResource(state.workspaceAsync, request, 'error', err);
    notify(`Could not open that architecture scope. ${state.diagramError}`, true);
    return false;
  }
}

async function setGraphReadingMode(mode, {loader = loadArchitectureDiagram, render = renderGraph, notify = toast} = {}) {
  if (!['MAP','READ','FULL'].includes(mode)) return false;
  if (!state.projectId || !state.architecture) return false;
  const projectId = state.projectId;
  const architecture = state.architecture;
  const surface = ARCHITECTURE_CANVAS_MODE ? 'canvas' : 'project';
  const scopeComponentId = surface === 'canvas' ? null : state.scopeComponentId;
  const transition = beginGraphTransition({projectId, architectureVersion:architecture.version, surface, scopeComponentId, readingMode:mode});
  if (mode === state.readingMode) return true;
  const contextTicket = currentWorkspaceContextTicket(state.workspaceAsync);
  const request = beginWorkspaceResource(state.workspaceAsync, 'canvas', contextTicket, {retainData:Boolean(state.diagram)});
  if (!request) return false;
  try {
    const cached = cachedArchitectureView(surface, projectId, architecture.version, scopeComponentId, mode);
    const nextDiagram = cached || (surface === 'canvas'
      ? await loadArchitectureCanvasDiagram(projectId, architecture, mode)
      : await loader(projectId, architecture, scopeComponentId, mode));
    if (!workspaceResourceIsCurrent(state.workspaceAsync, request) || !graphTransitionIsCurrent(transition)) return false;
    if (!nextDiagram) throw new Error('Diagram is unavailable for this reading mode.');
    const currentInteraction = captureGraphInteractionState();
    cacheArchitectureView(surface, projectId, architecture.version, scopeComponentId, mode, nextDiagram);
    state.diagram = nextDiagram;
    state.readingMode = mode;
    state.diagramError = null;
    reconcileGraphInteractionState(nextDiagram, currentInteraction);
    settleWorkspaceResource(state.workspaceAsync, request, 'ready');
    render();
    if (ARCHITECTURE_CANVAS_MODE) syncArchitectureCanvasSelectionUrl();
    return true;
  } catch (err) {
    if (!workspaceResourceIsCurrent(state.workspaceAsync, request) || !graphTransitionIsCurrent(transition)) return false;
    architectureViewCache.delete(architectureViewCacheKey(surface, projectId, architecture.version, scopeComponentId, mode));
    state.diagramError = err?.message || String(err);
    settleWorkspaceResource(state.workspaceAsync, request, 'error', err);
    notify(state.diagramError, true);
    return false;
  }
}

async function activateGraphNode(node, {navigate = navigateGraphScope, render = renderGraph} = {}) {
  if (ARCHITECTURE_CANVAS_MODE) state.canvasInspectorOpen=true;
  if (!node) return false;
  if (ARCHITECTURE_CANVAS_MODE) {
    (node.hierarchyPath || []).slice(0,-1).forEach((nodeId) => state.collapsedNodeIds.delete(nodeId));
  }
  clearArchitectureTracePath({render:false});
  state.selectedComponentId = node.component_id;
  state.selectedEdgeId = null;
  state.inspectorTab = 'overview';
  state.graphFocusMode = 'connected';
  syncArchitectureCanvasSelectionUrl();
  render();
  return true;
}

async function drillGraphNode(node, {navigate = navigateGraphScope} = {}) {
  if (!node || graphNodeAction(node) !== 'drill') return false;
  if (ARCHITECTURE_CANVAS_MODE) {
    clearArchitectureTracePath({render:false});
    state.selectedComponentId = node.component_id;
    state.selectedEdgeId = null;
    state.graphFocusMode = 'hierarchy';
    syncArchitectureCanvasSelectionUrl();
    renderGraph();
    return true;
  }
  return navigate(node.component_id, {focusComponentId:node.component_id});
}

async function revealArchitectureComponent(componentId, {surface = ARCHITECTURE_CANVAS_MODE ? 'canvas' : 'project', inspectorTab = 'overview', render = renderGraph, notify = toast} = {}) {
  const target = findArchitectureNode(componentId);
  if (!target) {
    notify(`Architecture component not found: ${componentId}`, true);
    return false;
  }
  const desiredSurface = surface === 'canvas' ? 'canvas' : 'project';
  if (desiredSurface === 'canvas' && !ARCHITECTURE_CANVAS_MODE) {
    const opened = await setArchitectureCanvasMode(true);
    if (!opened) return false;
  } else if (desiredSurface === 'project' && ARCHITECTURE_CANVAS_MODE) {
    const opened = await setArchitectureCanvasMode(false);
    if (!opened) return false;
  }
  if (state.currentView !== 'architecture') switchView('architecture');

  if (ARCHITECTURE_CANVAS_MODE) {
    const projected = diagramNodeByComponentId(target.id);
    if (!projected) {
      notify(`Component ${target.name || target.id} is not present in the full-system Canvas.`, true);
      return false;
    }
    (projected.hierarchyPath || []).slice(0, -1).forEach((nodeId) => state.collapsedNodeIds.delete(nodeId));
    clearArchitectureTracePath({render:false});
    state.canvasInspectorOpen = true;
    state.selectedComponentId = target.id;
    state.selectedEdgeId = null;
    state.inspectorTab = inspectorTab;
    state.graphFocusMode = 'connected';
    syncArchitectureCanvasSelectionUrl();
    render();
    requestAnimationFrame(() => {
      const svg = document.querySelector('#graphCanvas .living-graph-svg');
      const projectedNode = diagramNodeByComponentId(target.id);
      if (svg && projectedNode) focusGraphNodeInViewport(svg, projectedNode);
      document.querySelector(`[data-component="${CSS.escape(target.id)}"]`)?.focus();
    });
    return true;
  }

  const parentScopeComponentId = findArchitectureParentId(target.id);
  const opened = await navigateGraphScope(parentScopeComponentId ?? null, {focusComponentId:target.id, render, notify});
  if (!opened || !diagramNodeByComponentId(target.id)) return false;
  clearArchitectureTracePath({render:false});
  state.selectedComponentId = target.id;
  state.selectedEdgeId = null;
  state.inspectorTab = inspectorTab;
  state.graphFocusMode = 'connected';
  render();
  setTimeout(() => document.querySelector(`[data-component="${CSS.escape(target.id)}"]`)?.focus(), 0);
  return true;
}

function graphBreadcrumbMarkup(diagram) {
  const scope = diagram?.scope || {componentId:null};
  const trail = graphScopeTrail(diagram);
  const overview = scope.componentId ? '<button type="button" data-scope-target="">Overview</button>' : '<span aria-current="page">Overview</span>';
  const rest = trail.map((item) => item.componentId === scope.componentId ? `<span aria-current="page">${escapeHtml(item.label)}</span>` : `<button type="button" data-scope-target="${escapeHtml(item.componentId)}">${escapeHtml(item.label)}</button>`).join('<span class="graph-crumb-sep">/</span>');
  return `<nav class="graph-breadcrumb" aria-label="Architecture scope">${overview}${rest ? `<span class="graph-crumb-sep">/</span>${rest}` : ''}</nav>`;
}

function graphScopeToolbar(diagram) {
  const scope = diagram.scope || {componentId:null,label:'Overview',directRelationships:[]};
  const back = scope.componentId ? '<button class="graph-back" type="button" data-graph-back>← Back</button>' : '';
  const modes = ['MAP','READ','FULL'].map((mode) => `<button type="button" data-reading-mode="${mode}" class="${state.readingMode === mode ? 'active' : ''}" aria-pressed="${state.readingMode === mode}">${mode}</button>`).join('');
  const directCount = scope.directRelationships?.length || 0;
  return `<div class="graph-scope-bar"><div class="graph-scope-copy">${graphBreadcrumbMarkup(diagram)}<div><strong>${escapeHtml(scope.label || 'Overview')}</strong><span>${scope.componentId ? 'Canonical subsystem scope' : 'Canonical root system map'}${directCount ? ` · ${directCount} direct boundary relationship${directCount === 1 ? '' : 's'}` : ''}</span></div></div><div class="graph-scope-actions">${back}<div class="graph-reading-modes" role="group" aria-label="Graph information level">${modes}</div></div></div>`;
}

const graphViewport = {
  key: null,
  fitViewBox: null,
  viewBox: null,
  panFrame: null,
  pendingPan: null,
};

function parseGraphViewBox(value) {
  const parts = String(value || '').trim().split(/\s+/).map(Number);
  if (parts.length !== 4 || parts.some((part) => !Number.isFinite(part))) return null;
  const [x, y, width, height] = parts;
  if (width <= 0 || height <= 0) return null;
  return {x, y, width, height};
}

function formatGraphViewBox(box) {
  return `${box.x} ${box.y} ${box.width} ${box.height}`;
}

function graphViewportKey(diagram, kind = state.architectureGraphKind) {
  if (kind === 'code') return `code:${diagram?.repository?.revision || 'none'}`;
  return `living:${state.projectId || ''}:${diagram?.layoutVersion || ''}:${diagram?.architectureVersion ?? state.architecture?.version ?? 0}:${diagram?.scope?.componentId || 'ROOT'}`;
}

function resolvedGraphViewport(fitViewBox, key) {
  const fit = parseGraphViewBox(fitViewBox);
  if (!fit) return fitViewBox;
  if (graphViewport.key !== key) {
    graphViewport.key = key;
    graphViewport.fitViewBox = fit;
    graphViewport.viewBox = {...fit};
  } else {
    graphViewport.fitViewBox = fit;
    if (!graphViewport.viewBox) graphViewport.viewBox = {...fit};
  }
  return formatGraphViewBox(graphViewport.viewBox);
}

function graphViewportControlsMarkup(diagram = state.diagram) {
  const resourceName = state.architectureGraphKind === 'code' ? 'codeArchitecture' : 'canvas';
  const resource = state.workspaceAsync.resources[resourceName];
  const retry = resource?.status === 'error'
    ? `<button type="button" data-retry-workspace-resource="${resourceName}">Retry ${resourceName === 'canvas' ? 'diagram' : 'Code Architecture'}</button>`
    : '';
  if (!ARCHITECTURE_CANVAS_MODE) return retry ? `<div class="graph-viewport-tools graph-resource-tools" role="toolbar" aria-label="Graph recovery">${retry}</div>` : '';
  const expandAll = state.architectureGraphKind === 'living' && state.collapsedNodeIds.size
    ? '<button type="button" data-expand-all>Expand all</button>'
    : '';
  const picker = state.architectureGraphKind === 'living' && diagram?.fullCanvas
    ? `<label class="graph-component-picker"><span>Find component</span><input type="search" data-component-picker list="architecture-component-options" placeholder="Component ID" autocomplete="off"/><datalist id="architecture-component-options">${(diagram.nodes || []).map((node)=>`<option value="${escapeHtml(node.component_id)}">${escapeHtml((node.hierarchyPath || []).map((id)=>diagramNodeById(id,diagram)?.label || String(id).replace(/^node:/,'')).join(' / ') || node.label)}</option>`).join('')}</datalist></label>`
    : '';
  return `<div class="graph-viewport-tools" role="toolbar" aria-label="Canvas navigation">${picker}<span class="graph-pan-hint" data-graph-pan-hint>Hold Space + drag to pan</span><button type="button" data-canvas-inspector aria-pressed="${state.canvasInspectorOpen}">Details & Agent</button><button type="button" data-graph-viewport="zoom-out" aria-label="Zoom out">−</button><span data-graph-zoom aria-live="polite">100%</span><button type="button" data-graph-viewport="zoom-in" aria-label="Zoom in">＋</button><button type="button" data-graph-viewport="fit">Fit</button><button type="button" data-graph-viewport="actual">100%</button>${retry}${expandAll}</div>`;
}

function graphViewportWithAspect(svg, box) {
  const rect = svg.getBoundingClientRect();
  if (!rect.width || !rect.height) return {...box};
  const width = Math.max(box.width, box.height * rect.width / rect.height);
  const height = width * rect.height / rect.width;
  return {x:box.x+(box.width-width)/2, y:box.y+(box.height-height)/2, width, height};
}

function setGraphViewportBox(svg, box, {updateDensity = true} = {}) {
  if (!svg || !box || box.width <= 0 || box.height <= 0) return;
  box = graphViewportWithAspect(svg, box);
  graphViewport.viewBox = {...box};
  svg.setAttribute('viewBox', formatGraphViewBox(box));
  if (updateDensity) updateGraphViewportDensity(svg);
}

// GRAPH_PAN_SCHEDULER_START
function scheduleGraphPan(svg, box) {
  graphViewport.pendingPan = {svg, box:{...box}};
  if (graphViewport.panFrame != null) return;
  graphViewport.panFrame = requestAnimationFrame(() => {
    graphViewport.panFrame = null;
    const pending = graphViewport.pendingPan;
    graphViewport.pendingPan = null;
    if (!pending) return;
    // A pan changes only the viewport origin. Scale-dependent arrows, junction
    // radii and zoom tiers remain valid until an actual zoom/resize occurs.
    setGraphViewportBox(pending.svg, pending.box, {updateDensity:false});
  });
}
// GRAPH_PAN_SCHEDULER_END

function updateGraphViewportDensity(svg) {
  if (!svg || !ARCHITECTURE_CANVAS_MODE) return;
  const canvas = svg.closest('#graphCanvas');
  const box = parseGraphViewBox(svg.getAttribute('viewBox'));
  const rect = svg.getBoundingClientRect();
  if (!canvas || !box || rect.width <= 0 || rect.height <= 0) return;
  const scale = Math.min(rect.width / box.width, rect.height / box.height);
  const zoom = Math.max(1, Math.round(scale * 100));
  // Endpoint glyphs are screen-sized; canonical ports and routes never move.
  svg.querySelectorAll('[data-arrow-transform]').forEach((arrow) => {
    arrow.setAttribute('transform', `${arrow.dataset.arrowTransform} scale(${1 / scale})`);
  });
  // Junctions stay 4 CSS pixels wide; the larger invisible hit area is local UI.
  svg.querySelectorAll('[data-junction-radius]').forEach((circle) => {
    circle.setAttribute('r', Number(circle.dataset.junctionRadius) / scale);
  });
  canvas.dataset.zoomTier = zoom < 60 ? 'overview' : zoom < 120 ? 'detail' : 'full';
  const label = canvas.querySelector('[data-graph-zoom]');
  if (label) label.textContent = `${zoom}%`;
}

function focusGraphNodeInViewport(svg, node) {
  const fit = graphViewport.fitViewBox;
  const rect = svg?.getBoundingClientRect();
  if (!ARCHITECTURE_CANVAS_MODE || !fit || !node || !rect || rect.width <= 0 || rect.height <= 0) return false;
  const aspect = rect.width / rect.height;
  let width = Math.max(node.width * 2.8, fit.width / 3);
  let height = Math.max(node.height * 2.8, width / aspect);
  width = Math.max(width, height * aspect);
  const scale = Math.min(1, fit.width / width, fit.height / height);
  width *= scale;
  height *= scale;
  const centerX = node.x + node.width / 2;
  const centerY = node.y + node.height / 2;
  // A selected node near an outer canvas boundary still needs to be centered.
  // Allow temporary whitespace outside the fit bounds rather than pinning the
  // viewport to the full-graph rectangle; Fit remains the explicit reset.
  setGraphViewportBox(svg, {x:centerX-width/2,y:centerY-height/2,width,height});
  return true;
}

function zoomGraphViewport(svg, factor, clientX = null, clientY = null) {
  const raw = parseGraphViewBox(svg?.getAttribute('viewBox'));
  const current = raw && svg ? graphViewportWithAspect(svg, raw) : null;
  const fit = graphViewport.fitViewBox;
  const rect = svg?.getBoundingClientRect();
  if (!current || !fit || !rect || rect.width <= 0 || rect.height <= 0) return;
  const nextWidth = Math.min(fit.width * 6, Math.max(fit.width / 10, current.width * factor));
  const nextHeight = current.height * (nextWidth / current.width);
  const px = clientX == null ? rect.width / 2 : Math.min(rect.width, Math.max(0, clientX - rect.left));
  const py = clientY == null ? rect.height / 2 : Math.min(rect.height, Math.max(0, clientY - rect.top));
  const anchorX = current.x + (px / rect.width) * current.width;
  const anchorY = current.y + (py / rect.height) * current.height;
  setGraphViewportBox(svg, {
    x: anchorX - (px / rect.width) * nextWidth,
    y: anchorY - (py / rect.height) * nextHeight,
    width: nextWidth,
    height: nextHeight,
  });
}

let graphViewportResizeObserver = null;
let graphViewportInteractionCleanup = null;

function wireGraphViewport(svg) {
  graphViewportInteractionCleanup?.();
  graphViewportInteractionCleanup = null;
  if (!ARCHITECTURE_CANVAS_MODE || !svg) return;
  const canvas = svg.closest('#graphCanvas');
  const fit = parseGraphViewBox(svg.dataset.fitViewBox);
  if (fit) graphViewport.fitViewBox = fit;
  updateGraphViewportDensity(svg);
  graphViewportResizeObserver?.disconnect();
  if (typeof ResizeObserver !== 'undefined') {
    graphViewportResizeObserver = new ResizeObserver(() => updateGraphViewportDensity(svg));
    graphViewportResizeObserver.observe(svg);
  }

  canvas.querySelectorAll('[data-graph-viewport]').forEach((button) => button.addEventListener('click', () => {
    const action = button.dataset.graphViewport;
    if (action === 'zoom-in') zoomGraphViewport(svg, .8);
    if (action === 'zoom-out') zoomGraphViewport(svg, 1.25);
    if (action === 'fit' && graphViewport.fitViewBox) setGraphViewportBox(svg, graphViewport.fitViewBox);
    if (action === 'actual') {
      const current = parseGraphViewBox(svg.getAttribute('viewBox'));
      const rect = svg.getBoundingClientRect();
      if (!current || rect.width <= 0 || rect.height <= 0) return;
      const centerX = current.x + current.width / 2;
      const centerY = current.y + current.height / 2;
      setGraphViewportBox(svg, {x:centerX-rect.width/2, y:centerY-rect.height/2, width:rect.width, height:rect.height});
    }
  }));
  canvas.querySelector('[data-expand-all]')?.addEventListener('click', expandAllGraphNodes);
  canvas.querySelector('[data-canvas-inspector]')?.addEventListener('click',()=>{state.canvasInspectorOpen=!state.canvasInspectorOpen;renderGraph();});
  const componentPicker = canvas.querySelector('[data-component-picker]');
  const revealPickedComponent = () => {
    const componentId = String(componentPicker?.value || '').trim();
    if (componentId && findArchitectureNode(componentId)) void revealArchitectureComponent(componentId, {surface:'canvas'});
  };
  componentPicker?.addEventListener('change', revealPickedComponent);
  componentPicker?.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      revealPickedComponent();
    }
  });

  svg.addEventListener('wheel', (event) => {
    event.preventDefault();
    zoomGraphViewport(svg, Math.exp(event.deltaY * .0014), event.clientX, event.clientY);
  }, {passive:false});

  let drag = null;
  let spacePressed = false;
  let suppressNextClick = false;
  const editableTarget = (target) => Boolean(target?.closest?.('input,textarea,select,button,[contenteditable="true"]'));
  const syncPanHint = () => {
    canvas.classList.toggle('is-space-pan-ready', spacePressed);
    const hint = canvas.querySelector('[data-graph-pan-hint]');
    if (hint) hint.textContent = spacePressed ? 'Drag anywhere to pan' : 'Hold Space + drag to pan';
  };
  const onKeyDown = (event) => {
    if (event.code !== 'Space' || editableTarget(event.target)) return;
    spacePressed = true;
    syncPanHint();
    event.preventDefault();
  };
  const onKeyUp = (event) => {
    if (event.code !== 'Space') return;
    spacePressed = false;
    syncPanHint();
  };
  const finishPan = (event = null, force = false) => {
    if (!drag || (!force && event?.pointerId !== drag.pointerId)) return;
    const completed = drag;
    drag = null;
    svg.classList.remove('is-panning');
    if (completed.moved) suppressNextClick = true;
    if (svg.hasPointerCapture?.(completed.pointerId)) svg.releasePointerCapture?.(completed.pointerId);
  };
  const onWindowBlur = () => {
    spacePressed = false;
    syncPanHint();
    finishPan(null, true);
  };
  window.addEventListener('keydown', onKeyDown);
  window.addEventListener('keyup', onKeyUp);
  window.addEventListener('blur', onWindowBlur);

  svg.addEventListener('click', (event) => {
    if (!suppressNextClick) return;
    suppressNextClick = false;
    event.preventDefault();
    event.stopImmediatePropagation();
  }, true);
  svg.addEventListener('pointerdown', (event) => {
    const interactive = Boolean(event.target.closest?.('[data-node],[data-code-node],[data-edge],[data-fold-group]'));
    if (event.button !== 0 || (interactive && !spacePressed)) return;
    const box = parseGraphViewBox(svg.getAttribute('viewBox'));
    if (!box) return;
    drag = {pointerId:event.pointerId, clientX:event.clientX, clientY:event.clientY, box:graphViewportWithAspect(svg, box), moved:false};
    svg.setPointerCapture?.(event.pointerId);
    event.preventDefault();
  });
  svg.addEventListener('pointermove', (event) => {
    if (!drag || event.pointerId !== drag.pointerId) return;
    const rect = svg.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return;
    const clientDx = event.clientX - drag.clientX;
    const clientDy = event.clientY - drag.clientY;
    if (!drag.moved && Math.hypot(clientDx, clientDy) < 4) return;
    drag.moved = true;
    svg.classList.add('is-panning');
    const dx = clientDx * drag.box.width / rect.width;
    const dy = clientDy * drag.box.height / rect.height;
    scheduleGraphPan(svg, {...drag.box, x:drag.box.x-dx, y:drag.box.y-dy});
    event.preventDefault();
  });
  svg.addEventListener('pointerup', finishPan);
  svg.addEventListener('pointercancel', finishPan);
  svg.addEventListener('lostpointercapture', finishPan);
  graphViewportInteractionCleanup = () => {
    window.removeEventListener('keydown', onKeyDown);
    window.removeEventListener('keyup', onKeyUp);
    window.removeEventListener('blur', onWindowBlur);
    canvas.classList.remove('is-space-pan-ready');
  };
}

function syncArchitectureCanvasDomMode() {
  document.body.dataset.architectureCanvasMode = ARCHITECTURE_CANVAS_MODE ? 'true' : 'false';
  document.body.classList.toggle('architecture-canvas-mode', ARCHITECTURE_CANVAS_MODE);
  const graphSide = document.querySelector('#view-architecture .graph-side');
  const agentDock = $('globalAgentDock');
  const workspace = $('workspace');
  if (ARCHITECTURE_CANVAS_MODE) {
    if (graphSide && agentDock && agentDock.parentElement !== graphSide) graphSide.appendChild(agentDock);
  } else if (workspace && agentDock && agentDock.parentElement !== workspace) {
    workspace.appendChild(agentDock);
  }
}

// ARCHITECTURE_INTERACTION_STATE_START
function snapshotArchitectureInteractionState(source = state) {
  return {
    currentView:source.currentView,
    scopeComponentId:source.scopeComponentId,
    selectedComponentId:source.selectedComponentId,
    selectedEdgeId:source.selectedEdgeId,
    inspectorTab:source.inspectorTab,
    graphFocusMode:source.graphFocusMode,
    collapsedNodeIds:new Set(source.collapsedNodeIds || []),
    canvasDeepLinkApplied:Boolean(source.canvasDeepLinkApplied),
    canvasDeepLinkFocusPending:Boolean(source.canvasDeepLinkFocusPending),
    canvasInspectorOpen:Boolean(source.canvasInspectorOpen),
    tracePathRequest:source.tracePathRequest,
    tracePathResult:source.tracePathResult,
    tracePathLoading:Boolean(source.tracePathLoading),
    tracePathError:source.tracePathError,
    diagram:source.diagram,
    diagramError:source.diagramError,
  };
}

function restoreArchitectureInteractionState(target, snapshot) {
  if (!snapshot) return;
  Object.assign(target, snapshot, {collapsedNodeIds:new Set(snapshot.collapsedNodeIds || [])});
}

function applyArchitectureNavigationToUrl(url, {enabled, projectId, selectedComponentId, inspectorTab}) {
  if (enabled) url.searchParams.set('canvas', 'architecture');
  else url.searchParams.delete('canvas');
  if (projectId) url.searchParams.set('project', projectId);
  else url.searchParams.delete('project');
  if (enabled && selectedComponentId) url.searchParams.set('node', selectedComponentId);
  else url.searchParams.delete('node');
  if (enabled && selectedComponentId && inspectorTab && inspectorTab !== 'overview') url.searchParams.set('tab', inspectorTab);
  else url.searchParams.delete('tab');
  url.searchParams.delete('mode');
  return url;
}
// ARCHITECTURE_INTERACTION_STATE_END

let architectureCanvasModeRequest = 0;

async function setArchitectureCanvasMode(enabled, {
  pushHistory = true,
  historyMode = null,
  navigationGuard = null,
  route = null,
} = {}) {
  const guard = navigationGuard || beginNavigationTransition(state.projectId);
  if (!navigationGenerationIsCurrent(guard)) return false;
  const requestId = ++architectureCanvasModeRequest;
  const projectId = state.projectId;
  const architecture = state.architecture;
  const architectureVersion = architecture?.version;
  const resolvedHistoryMode = historyMode || (pushHistory ? 'push' : 'none');
  const previousCommitted = state.navigation.committed ? {...state.navigation.committed} : navigationSnapshotFromState();
  const desiredReadingMode = normalizedArchitectureReadingMode(state.readingMode);
  const transitionInteraction = captureGraphInteractionState();
  const requestedNodeId = String(route?.nodeId || '').trim() || null;
  const requestedInspectorTab = route?.inspectorTab || null;
  let targetScopeComponentId = enabled ? null : state.scopeComponentId;
  if (!enabled && transitionInteraction.selectedComponentId) {
    const parentId = findArchitectureParentId(transitionInteraction.selectedComponentId);
    if (parentId !== undefined) targetScopeComponentId = parentId || null;
  }
  const targetSurface = enabled ? 'canvas' : 'project';
  const targetReadingMode = enabled ? 'FULL' : desiredReadingMode;
  const graphTransition = beginGraphTransition({projectId, architectureVersion, surface:targetSurface, scopeComponentId:targetScopeComponentId, readingMode:targetReadingMode});
  const beginModeResource = () => {
    if (!projectId || !architecture?.components?.length) return null;
    const contextTicket = currentWorkspaceContextTicket(state.workspaceAsync);
    return beginWorkspaceResource(state.workspaceAsync, 'canvas', contextTicket, {retainData:Boolean(state.diagram)});
  };
  const isCurrent = () => navigationGenerationIsCurrent(guard)
    && requestId === architectureCanvasModeRequest
    && state.projectId === projectId
    && state.architecture === architecture
    && state.architecture?.version === architectureVersion
    && graphTransitionIsCurrent(graphTransition);

  if (ARCHITECTURE_CANVAS_MODE === enabled && state.diagram) {
    const noOpResource = beginModeResource();
    if (noOpResource) settleWorkspaceResource(state.workspaceAsync, noOpResource, 'ready');
    state.currentView = ROUTED_VIEWS.has(route?.view) ? route.view : 'architecture';
    const requestedNode = enabled && requestedNodeId ? diagramNodeByComponentId(requestedNodeId) : null;
    const invalidRequestedNode = Boolean(enabled && requestedNodeId && !requestedNode);
    if (requestedNode) {
      (requestedNode?.hierarchyPath || []).slice(0, -1).forEach((id) => state.collapsedNodeIds.delete(id));
      clearArchitectureTracePath({render:false});
      state.selectedComponentId = requestedNodeId;
      state.selectedEdgeId = null;
      state.inspectorTab = requestedInspectorTab || 'overview';
      state.canvasInspectorOpen = true;
      state.graphFocusMode = 'connected';
    } else if (enabled && route) {
      // An explicit Canvas route with no node is itself authoritative. Do not
      // inherit the selection from the previously displayed history entry.
      state.selectedComponentId = null;
      state.selectedEdgeId = null;
      state.inspectorTab = 'overview';
      state.canvasInspectorOpen = false;
      state.graphFocusMode = 'all';
      state.canvasDeepLinkApplied = false;
      state.canvasDeepLinkFocusPending = false;
      clearArchitectureTracePath({render:false});
    }
    const routeHistoryMode = route
      ? (invalidRequestedNode && resolvedHistoryMode === 'none' ? 'replace' : resolvedHistoryMode)
      : 'none';
    if (enabled && route) {
      state.canvasDeepLinkApplied = true;
      state.canvasDeepLinkFocusPending = Boolean(requestedNode);
    }
    if (!commitNavigation({
      projectId,
      view:state.currentView,
      canvas:enabled,
      nodeId:enabled ? state.selectedComponentId : null,
      inspectorTab:enabled ? state.inspectorTab : 'overview',
    }, {historyMode:routeHistoryMode, guard})) return false;
    render();
    return true;
  }

  const previousMode = ARCHITECTURE_CANVAS_MODE;
  let previousState = null;
  let resourceRequest = null;
  try {
    let nextDiagram = state.diagram;
    if (projectId && architecture?.components?.length) {
      resourceRequest = beginModeResource();
      if (!resourceRequest) return false;
      const cached = cachedArchitectureView(targetSurface, projectId, architectureVersion, targetScopeComponentId, targetReadingMode);
      nextDiagram = cached || (enabled
        ? await loadArchitectureCanvasDiagram(projectId, architecture, targetReadingMode)
        : await loadArchitectureDiagram(projectId, architecture, targetScopeComponentId, targetReadingMode));
    }
    if (!isCurrent() || (resourceRequest && !workspaceResourceIsCurrent(state.workspaceAsync, resourceRequest))) return false;
    if (!nextDiagram && projectId && architecture?.components?.length) throw new Error('Architecture diagram is unavailable.');

    previousState = snapshotArchitectureInteractionState(state);
    const currentInteraction = captureGraphInteractionState();
    if (nextDiagram) cacheArchitectureView(targetSurface, projectId, architectureVersion, targetScopeComponentId, targetReadingMode, nextDiagram);
    ARCHITECTURE_CANVAS_MODE = enabled;
    state.diagram = nextDiagram;
    state.diagramError = null;
    if (!enabled) state.scopeComponentId = targetScopeComponentId;
    state.readingMode = desiredReadingMode;
    state.currentView = ROUTED_VIEWS.has(route?.view) ? route.view : 'architecture';
    state.canvasDeepLinkApplied = false;
    reconcileGraphInteractionState(nextDiagram, currentInteraction);

    const routeOwnsSelection = Boolean(enabled && route);
    const selectionId = routeOwnsSelection ? requestedNodeId : state.selectedComponentId;
    const invalidRequestedNode = Boolean(routeOwnsSelection && requestedNodeId && !diagramNodeByComponentId(requestedNodeId, nextDiagram));
    if (selectionId && diagramNodeByComponentId(selectionId, nextDiagram)) {
      state.selectedComponentId = selectionId;
      state.selectedEdgeId = null;
      state.inspectorTab = requestedInspectorTab || currentInteraction.inspectorTab || 'overview';
      state.graphFocusMode = currentInteraction.selectedComponentId === selectionId ? currentInteraction.graphFocusMode : 'connected';
      if (enabled) {
        const target = diagramNodeByComponentId(selectionId, nextDiagram);
        (target?.hierarchyPath || []).slice(0, -1).forEach((id) => state.collapsedNodeIds.delete(id));
      }
    } else if (routeOwnsSelection) {
      state.selectedComponentId = null;
      state.selectedEdgeId = null;
      state.inspectorTab = 'overview';
      state.graphFocusMode = 'all';
      clearArchitectureTracePath({render:false});
    }
    state.canvasInspectorOpen = Boolean(enabled && state.selectedComponentId);
    // Selection has already been reconciled with this projection. Rendering
    // must preserve it even when this mode switch has no explicit deep link.
    state.canvasDeepLinkApplied = Boolean(enabled);
    state.canvasDeepLinkFocusPending = Boolean(enabled && requestedNodeId && !invalidRequestedNode);
    syncArchitectureCanvasDomMode();
    if (resourceRequest) settleWorkspaceResource(state.workspaceAsync, resourceRequest, nextDiagram ? 'ready' : 'empty');
    const commitHistoryMode = invalidRequestedNode && resolvedHistoryMode === 'none' ? 'replace' : resolvedHistoryMode;
    if (!commitNavigation({
      projectId,
      view:state.currentView,
      canvas:enabled,
      nodeId:enabled ? state.selectedComponentId : null,
      inspectorTab:enabled ? state.inspectorTab : 'overview',
    }, {historyMode:commitHistoryMode, guard})) return false;
    render();
    if (enabled && state.selectedComponentId && typeof requestAnimationFrame === 'function') requestAnimationFrame(() => {
      const svg = document.querySelector('#graphCanvas .living-graph-svg');
      const projectedNode = diagramNodeByComponentId(state.selectedComponentId);
      if (svg && projectedNode) focusGraphNodeInViewport(svg, projectedNode);
    });
    return true;
  } catch (error) {
    if (!isCurrent() || (resourceRequest && !workspaceResourceIsCurrent(state.workspaceAsync, resourceRequest))) return false;
    architectureViewCache.delete(architectureViewCacheKey(targetSurface, projectId, architectureVersion, targetScopeComponentId, targetReadingMode));
    if (previousState) {
      restoreArchitectureInteractionState(state, previousState);
      ARCHITECTURE_CANVAS_MODE = previousMode;
    }
    if (resourceRequest) settleWorkspaceResource(state.workspaceAsync, resourceRequest, state.diagram ? 'ready' : 'empty');
    try {
      if (previousCommitted) commitNavigation(previousCommitted, {historyMode:'replace', guard});
      else recommitCurrentNavigationAfterFailedTransition(guard, {historyMode:'replace'});
      if (previousState) { syncArchitectureCanvasDomMode(); render(); }
    } catch (rollbackError) {
      console.error('Architecture view rollback failed.', rollbackError);
    }
    toast(`Could not switch architecture view. ${error?.message || String(error)}`, true);
    return false;
  }
}

async function openArchitectureCanvas() {
  return setArchitectureCanvasMode(true);
}

async function leaveArchitectureCanvas() {
  return setArchitectureCanvasMode(false);
}

async function toggleArchitectureCanvas() {
  return ARCHITECTURE_CANVAS_MODE ? leaveArchitectureCanvas() : openArchitectureCanvas();
}

function setArchitectureGraphKind(kind, {render = renderGraph} = {}) {
  if (!['living','code'].includes(kind)) return false;
  if (state.architectureGraphKind === kind) return true;
  state.architectureGraphKind = kind;
  // Living and Code graphs own independent selections. Switching presentation
  // must not erase the Living selection encoded by the committed Canvas route.
  render();
  updateInstructionContext();
  return true;
}

function renderArchitectureChrome() {
  const codeMode = state.architectureGraphKind === 'code';
  if ($('graphCanvas')) $('graphCanvas').dataset.graphKind=state.architectureGraphKind;
  const graphSide = document.querySelector('.graph-side');
  if (graphSide) {
    graphSide.dataset.graphKind = state.architectureGraphKind;
    graphSide.dataset.readingMode = ARCHITECTURE_CANVAS_MODE ? 'FULL' : state.readingMode;
  }
  document.querySelectorAll('[data-architecture-graph-kind]').forEach((button) => {
    const active = button.dataset.architectureGraphKind === state.architectureGraphKind;
    button.classList.toggle('active', active);
    button.setAttribute('aria-pressed', String(active));
  });
  if ($('architectureViewTitle')) $('architectureViewTitle').textContent = codeMode ? 'Code Graph' : (ARCHITECTURE_CANVAS_MODE ? (state.diagram?.presentation?.title || 'System architecture') : 'Living Graph');
  if ($('architectureViewSubtitle')) $('architectureViewSubtitle').textContent = codeMode
    ? 'Implementation evidence generated from source inspected at one exact GitHub commit.'
    : ARCHITECTURE_CANVAS_MODE
      ? (state.diagram?.presentation?.subtitle || 'The whole system. Every connection, in context.')
      : 'Start with the system health map, then open canonical subsystems one level at a time.';
  if ($('graphPanelTitle')) $('graphPanelTitle').textContent = codeMode ? 'Implementation map' : 'System architecture';
  if ($('graphPanelSubtitle')) $('graphPanelSubtitle').textContent = codeMode
    ? 'Derived evidence only. This graph never overwrites accepted Living Architecture.'
    : 'Follow the project backbone. Select a component to reveal its other connections.';
  if ($('graphHealthLegend')) $('graphHealthLegend').classList.toggle('hidden', codeMode);
  if ($('graphEvidenceTitle')) $('graphEvidenceTitle').textContent = codeMode ? 'Source evidence' : 'Component details';
  if ($('graphDecisionTitle')) $('graphDecisionTitle').textContent = codeMode ? 'Repository snapshot' : 'Architecture decisions';
  if ($('graphRiskTitle')) $('graphRiskTitle').textContent = codeMode ? 'Evidence boundary' : 'Risks & assumptions';
  if ($('architectureCanvasBtn')) $('architectureCanvasBtn').textContent = ARCHITECTURE_CANVAS_MODE ? 'Project View' : 'Open Canvas ↗';
  syncDocumentTitle();
  if (state.currentView === 'architecture') {
    $('pageTitle').textContent = codeMode ? 'Code Architecture' : 'Living Architecture';
    $('pageSubtitle').textContent = codeMode
      ? 'Revision-pinned implementation evidence from connected repository analysis.'
      : ARCHITECTURE_CANVAS_MODE
        ? 'Human-approved design intent as one backend-authored full-system architecture canvas.'
        : 'Human-approved design intent with backend-authored hierarchical drilldown.';
  }
}

function codeNodeById(nodeId) {
  return (state.codeDiagram?.nodes || []).find((node) => node.id === nodeId) || null;
}

function codeEvidenceLink(source) {
  const label = `${source.path || 'source'}:${source.line_start || '?'}${source.line_end && source.line_end !== source.line_start ? `-${source.line_end}` : ''}`;
  return `<a class="code-evidence-link" href="${escapeHtml(source.href)}" target="_blank" rel="noreferrer">${escapeHtml(label)} ↗</a>`;
}

function renderCodeSelectedNode() {
  const diagram = state.codeDiagram;
  const node = codeNodeById(state.selectedCodeNodeId);
  if (!diagram || !node) {
    $('selectedNode').innerHTML = diagram
      ? `<small>IMPLEMENTATION EVIDENCE</small><h3>${escapeHtml(diagram.repository.slug)}</h3><p>Select a code node to inspect the exact source excerpts that support it.</p>`
      : '<small>IMPLEMENTATION EVIDENCE</small><h3>No code snapshot yet</h3><p>Connect GitHub, let the agent inspect an exact commit, then publish a revision-pinned Code Architecture snapshot.</p>';
    $('nodeEvidence').innerHTML = diagram
      ? `<p><strong>Revision pinned</strong></p><p class="muted"><code>${escapeHtml(diagram.repository.revision)}</code></p><p><strong>Evidence mode</strong></p><p class="muted">${escapeHtml(diagram.evidenceVerification.mode || 'REVISION_PINNED_AGENT_SUPPLIED')}</p>`
      : '<p><strong>How this is created</strong></p><p class="muted">The agent must first inspect the connected GitHub repository, resolve a full commit SHA, and cite repository-relative source lines. File names alone are not accepted as architecture evidence.</p>';
    return;
  }
  const incoming = diagram.edges.filter((edge) => edge.target === node.id);
  const outgoing = diagram.edges.filter((edge) => edge.source === node.id);
  const connection = (edge, direction) => {
    const peer = codeNodeById(direction === 'in' ? edge.source : edge.target);
    return `<li><strong>${direction === 'in' ? 'From' : 'To'} ${escapeHtml(peer?.label || (direction === 'in' ? edge.source : edge.target))}</strong><span>${escapeHtml(edge.semantic_type || edge.label || 'relationship')}${edge.supporting_text ? ` · ${escapeHtml(edge.supporting_text)}` : ''}</span></li>`;
  };
  $('selectedNode').innerHTML = `<small>CODE COMPONENT · ${escapeHtml(String(node.semantic_kind || 'COMPONENT'))}</small><div class="selected-node-title"><h3>${escapeHtml(node.label)}</h3><span class="code-evidence-pill">PINNED</span></div><p>${escapeHtml(node.responsibility)}</p><div class="component-children-summary"><strong>Implementation facts</strong><span>Snapshot ID · ${escapeHtml(node.id)}</span><span>Children · ${node.childCount}</span><span>Commit · ${escapeHtml(diagram.repository.revision.slice(0, 12))}</span></div><div class="component-connections">${incoming.length || outgoing.length ? `<ul>${incoming.map((edge) => connection(edge, 'in')).join('')}${outgoing.map((edge) => connection(edge, 'out')).join('')}</ul>` : '<p class="muted">No evidence-backed runtime relationship is attached to this node.</p>'}</div>`;
  $('nodeEvidence').innerHTML = node.sources.length
    ? node.sources.map((source) => `<div class="code-evidence-item">${codeEvidenceLink(source)}${source.symbol ? `<span class="code-evidence-symbol">${escapeHtml(source.symbol)}</span>` : ''}<pre>${escapeHtml(source.excerpt || '')}</pre></div>`).join('')
    : '<p class="muted">No source excerpt is attached to this node.</p>';
}

function renderCodeGraph() {
  const canvas = $('graphCanvas');
  const diagram = state.codeDiagram;
  const resource = state.workspaceAsync.resources.codeArchitecture;
  if (!diagram) {
    const loading = resource.status === 'loading';
    const failed = resource.status === 'error';
    $('graphVersion').textContent = loading ? 'Loading' : failed ? 'Unavailable' : 'No snapshot';
    $('graphReviewState').textContent = loading ? 'Loading GitHub evidence' : failed ? 'Code evidence unavailable' : 'Awaiting GitHub evidence';
    const title = loading ? 'Loading Code Architecture…' : failed ? 'Code Architecture unavailable' : 'No Code Architecture snapshot yet';
    const detail = failed
      ? escapeHtml(resource.error || 'The latest Code Architecture could not be loaded.')
      : loading
        ? 'The last published implementation evidence is being loaded.'
        : 'Connect GitHub and ask the agent to inspect the repository at an exact commit, then publish evidence-backed implementation architecture.';
    const retry = failed ? '<button class="btn secondary" type="button" data-retry-workspace-resource="codeArchitecture">Retry Code Architecture</button>' : '';
    canvas.innerHTML = `<div class="graph-empty code-graph-empty"><div><strong>${title}</strong><p class="muted">${detail}</p>${retry}</div></div>`;
    wireWorkspaceResourceRetry(canvas);
    renderCodeSelectedNode();
    $('decisionList').innerHTML = failed ? '<p class="muted">The previous Code Architecture load failed. Retry without leaving this workspace.</p>' : '<p class="muted">No repository snapshot has been published for this project.</p>';
    $('riskList').innerHTML = '<p class="muted">Code Architecture is derived evidence. It never becomes accepted Living Architecture without a separate human-reviewed architecture proposal.</p>';
    return;
  }
  const selected = codeNodeById(state.selectedCodeNodeId);
  if (state.selectedCodeNodeId && !selected) state.selectedCodeNodeId = null;
  const byId = new Map(diagram.nodes.map((node) => [node.id, node]));
  const hierarchy = diagram.nodes.map((node) => {
    if (!node.parent_id) return '';
    const parent = byId.get(node.parent_id);
    if (!parent) return '';
    return `<line class="graph-hierarchy code-hierarchy" x1="${parent.x+parent.width/2}" y1="${parent.y+parent.height/2}" x2="${node.x+node.width/2}" y2="${node.y+node.height/2}"/>`;
  }).join('');
  const edges = diagram.edges.map((edge) => {
    const anchor = edge.points[Math.floor((edge.points.length - 1) / 2)];
    return `<g class="graph-edge code-edge" data-code-edge="${escapeHtml(edge.id)}"><path d="${graphPathData(edge.points)}" marker-end="url(#code-arrow)"/><text x="${anchor.x}" y="${anchor.y-7}" text-anchor="middle">${escapeHtml(edge.semantic_type || edge.label || '')}</text></g>`;
  }).join('');
  const nodes = diagram.nodes.map((node) => {
    const active = state.selectedCodeNodeId === node.id;
    const names = wrapGraphText(node.label, Math.max(13, Math.floor((node.width-40)/8.2)), 2).map((line,index) => `<text class="node-name" x="${node.x+18}" y="${node.y+55+index*17}">${escapeHtml(line)}</text>`).join('');
    const responsibility = wrapGraphText(node.responsibility, Math.max(18, Math.floor((node.width-40)/6.8)), 2).map((line,index) => `<text class="node-responsibility" x="${node.x+18}" y="${node.y+96+index*14}">${escapeHtml(line)}</text>`).join('');
    return `<g class="node-card code-node-card${active ? ' selected' : ''}" data-code-node="${escapeHtml(node.id)}" role="button" tabindex="0" aria-label="Inspect code evidence for ${escapeHtml(node.label)}"><rect class="node-surface" x="${node.x}" y="${node.y}" width="${node.width}" height="${node.height}" rx="16"/>${graphNodeKindMarkup(node)}${names}${responsibility}<text class="code-node-evidence-count" x="${node.x+18}" y="${node.y+node.height-13}">${node.sources.length} source${node.sources.length === 1 ? '' : 's'} · depth ${node.depth}</text></g>`;
  }).join('');
  $('graphVersion').textContent = `@${diagram.repository.revision.slice(0, 8)}`;
  $('graphReviewState').textContent = `${diagram.nodes.length} implementation node${diagram.nodes.length === 1 ? '' : 's'}${resource.refreshing ? ' · refreshing' : ''}`;
  const meta = `<div class="graph-meta code-graph-meta"><span>${escapeHtml(diagram.repository.slug)}</span><span>${diagram.nodes.length} nodes</span><span>${diagram.edges.length} evidence-backed relationship${diagram.edges.length === 1 ? '' : 's'}</span><span>Exact commit · ${escapeHtml(diagram.repository.revision.slice(0, 12))}</span><span class="graph-meta-ok">Living architecture unchanged</span></div>`;
  const fitViewBox = `0 0 ${diagram.width} ${diagram.height}`;
  const viewBox = ARCHITECTURE_CANVAS_MODE ? resolvedGraphViewport(fitViewBox, graphViewportKey(diagram, 'code')) : fitViewBox;
  canvas.innerHTML = `<div class="code-snapshot-bar"><div><strong>${escapeHtml(diagram.repository.slug)}</strong><span>Implementation evidence at exact Git revision</span></div><a href="${escapeHtml(`${diagram.repository.url}/tree/${diagram.repository.revision}`)}" target="_blank" rel="noreferrer"><code>${escapeHtml(diagram.repository.revision)}</code> ↗</a></div><div class="graph-stage">${meta}${graphViewportControlsMarkup()}<svg class="living-graph-svg code-graph-svg" data-fit-view-box="${escapeHtml(fitViewBox)}" viewBox="${escapeHtml(viewBox)}" role="img" aria-label="Revision-pinned code architecture graph"><defs><marker id="code-arrow" markerWidth="9" markerHeight="9" refX="8" refY="4.5" orient="auto"><path d="M1 1 L8 4.5 L1 8 Z"/></marker></defs>${hierarchy}${edges}${nodes}</svg></div>`;
  const activate = (element) => {
    state.selectedCodeNodeId = element.dataset.codeNode;
    renderCodeGraph();
    setTimeout(() => document.querySelector(`[data-code-node="${CSS.escape(state.selectedCodeNodeId)}"]`)?.focus(), 0);
  };
  canvas.querySelectorAll('[data-code-node]').forEach((element) => {
    element.addEventListener('click', () => activate(element));
    element.addEventListener('keydown', (event) => {
      if (event.key !== 'Enter' && event.key !== ' ') return;
      event.preventDefault();
      activate(element);
    });
  });
  wireWorkspaceResourceRetry(canvas);
  wireGraphViewport(canvas.querySelector('.living-graph-svg'));
  renderCodeSelectedNode();
  $('decisionList').innerHTML = `<ul><li><strong>${escapeHtml(diagram.repository.slug)}</strong></li><li><code>${escapeHtml(diagram.repository.revision)}</code></li><li>${escapeHtml(diagram.summary || 'No implementation summary.')}</li></ul>`;
  $('riskList').innerHTML = `<ul><li>Classification: ${escapeHtml(diagram.classification || 'IMPLEMENTATION_EVIDENCE')}</li><li>Canonical state mutated: ${diagram.canonicalStateMutated ? 'YES' : 'NO'}</li><li>${escapeHtml(diagram.evidenceVerification.note || 'Evidence is pinned to the supplied Git revision.')}</li></ul>`;
}

function graphDisplayModel(diagram) {
  const scoped = Boolean(diagram?.scope?.componentId);
  const canonicalNodes = scoped
    ? (diagram?.nodes || []).filter((node) => node.projectionRole !== 'SCOPE')
    : (diagram?.nodes || []);
  const nodeIds = new Set(canonicalNodes.map((node) => node.id));
  const canonicalEdges = (diagram?.edges || []).filter((edge) => nodeIds.has(edge.source) && nodeIds.has(edge.target));
  const canonicalGroupFrames = diagram?.fullCanvas ? (diagram.groupFrames || []) : [];
  if (!ARCHITECTURE_CANVAS_MODE || !diagram?.fullCanvas || !state.collapsedNodeIds.size) {
    return {
      scoped,
      nodes:canonicalNodes,
      edges:canonicalEdges,
      groupFrames:canonicalGroupFrames,
      collapsedNodeIds:new Set(),
      hiddenNodeCounts:new Map(),
    };
  }

  const nodeById = new Map(canonicalNodes.map((node) => [node.id,node]));
  const validCollapsed = new Set(
    [...state.collapsedNodeIds].filter((nodeId) => (nodeById.get(nodeId)?.childCount || 0) > 0)
  );
  if (validCollapsed.size !== state.collapsedNodeIds.size) state.collapsedNodeIds = validCollapsed;
  const hiddenOwnerByNode = new Map();
  canonicalNodes.forEach((node) => {
    const path = node.hierarchyPath?.length ? node.hierarchyPath : [node.id];
    const owner = path.slice(0,-1).find((nodeId) => validCollapsed.has(nodeId));
    if (owner) hiddenOwnerByNode.set(node.id, owner);
  });
  const hiddenNodeCounts = new Map();
  hiddenOwnerByNode.forEach((owner) => hiddenNodeCounts.set(owner, (hiddenNodeCounts.get(owner) || 0) + 1));
  const nodes = canonicalNodes.filter((node) => !hiddenOwnerByNode.has(node.id));
  const visibleNodeById = new Map(nodes.map((node) => [node.id,node]));
  const endpoint = (nodeId) => hiddenOwnerByNode.get(nodeId) || nodeId;
  const frameById=new Map(canonicalGroupFrames.map((frame)=>[frame.nodeId,frame]));
  const clipAtFrame=(route, ownerId)=>{
    const frame=frameById.get(ownerId);
    if (!frame) return route;
    const inside=(p)=>p.x>=frame.x && p.x<=frame.x+frame.width && p.y>=frame.y && p.y<=frame.y+frame.height;
    for (let i=1;i<route.length;i++) {
      const a=route[i-1],b=route[i];
      if (!inside(a) || inside(b)) continue;
      const exit=a.x===b.x
        ? {x:a.x,y:b.y>frame.y+frame.height?frame.y+frame.height:frame.y}
        : {x:b.x>frame.x+frame.width?frame.x+frame.width:frame.x,y:a.y};
      return [exit,...route.slice(i)];
    }
    return route;
  };
  const edges = canonicalEdges.flatMap((edge) => {
    const source = endpoint(edge.source), target = endpoint(edge.target);
    if (!visibleNodeById.has(source) || !visibleNodeById.has(target)) return [];
    if (source === target && (source !== edge.source || target !== edge.target)) return [];
    if (source === edge.source && target === edge.target) return [edge];
    let points = (edge.points || []).map((point) => ({...point}));
    if (source !== edge.source) points=clipAtFrame(points,source);
    if (target !== edge.target) points=clipAtFrame([...points].reverse(),target).reverse();
    return [{
      ...edge,
      source,
      target,
      points,
      projection_kind:'COLLAPSED',
      canonical_source:edge.source,
      canonical_target:edge.target,
    }];
  });
  const groupFrames = canonicalGroupFrames.filter((frame) => !hiddenOwnerByNode.has(frame.nodeId));
  return {scoped,nodes,edges,groupFrames,collapsedNodeIds:validCollapsed,hiddenNodeCounts};
}

function graphVisualConnections(edges = [], nodes = []) {
  const nodeById = new Map(nodes.map((node) => [node.id, node]));
  const grouped = new Map();
  edges.forEach((edge) => {
    const pair = [String(edge.source), String(edge.target)].sort();
    // JSON tuple encoding is collision-free for arbitrary component ids; a
    // delimiter such as "::" can merge unrelated pairs when ids contain it.
    const key = JSON.stringify(pair);
    if (!grouped.has(key)) grouped.set(key, []);
    grouped.get(key).push(edge);
  });
  return [...grouped.entries()].map(([key, members]) => {
    const ordered = [...members].sort((left, right) => {
      const leftBackbone = String(left.layout_role || 'BACKBONE') === 'BACKBONE' ? 0 : 1;
      const rightBackbone = String(right.layout_role || 'BACKBONE') === 'BACKBONE' ? 0 : 1;
      if (leftBackbone !== rightBackbone) return leftBackbone - rightBackbone;
      const leftSource = nodeById.get(left.source), leftTarget = nodeById.get(left.target);
      const rightSource = nodeById.get(right.source), rightTarget = nodeById.get(right.target);
      const leftForward = (leftSource?.x ?? 0) <= (leftTarget?.x ?? 0) ? 0 : 1;
      const rightForward = (rightSource?.x ?? 0) <= (rightTarget?.x ?? 0) ? 0 : 1;
      if (leftForward !== rightForward) return leftForward - rightForward;
      const leftProvenance = Array.isArray(left.provenance) ? left.provenance.length : 0;
      const rightProvenance = Array.isArray(right.provenance) ? right.provenance.length : 0;
      if (leftProvenance !== rightProvenance) return rightProvenance - leftProvenance;
      return String(left.id).localeCompare(String(right.id));
    });
    const primary = ordered[0];
    const directions = new Set(members.map((edge) => `${edge.source}->${edge.target}`));
    return {
      ...primary,
      id: `visual:${key}`,
      label: members.length > 1 ? `${members.length} relationships` : (primary.label || primary.semantic_type || ''),
      layout_role: 'BACKBONE',
      memberIds: members.map((edge) => edge.id),
      bidirectional: directions.size > 1,
    };
  }).sort((left, right) => String(left.id).localeCompare(String(right.id)));
}

function graphDisplayViewBox(diagram, nodes, edges) {
  if (diagram?.fullCanvas) {
    const xs = [], ys = [];
    nodes.forEach((node) => {
      xs.push(node.x, node.x + node.width);
      ys.push(node.y, node.y + node.height);
    });
    (diagram.groupFrames || []).forEach((frame) => {
      xs.push(frame.x, frame.x + frame.width);
      ys.push(frame.y, frame.y + frame.height);
    });
    edges.forEach((edge) => (edge.points || []).forEach((point) => { xs.push(point.x); ys.push(point.y); }));
    if (xs.length && ys.length) {
      const padding = 24;
      const minX=Math.min(...xs)-padding, maxX=Math.max(...xs)+padding;
      const minY=Math.min(...ys)-padding, maxY=Math.max(...ys)+padding;
      return `${minX} ${minY} ${maxX-minX} ${maxY-minY}`;
    }
  }
  if (!diagram?.scope?.componentId || !nodes.length) return `0 0 ${diagram.width} ${diagram.height}`;
  const xs = [];
  const ys = [];
  nodes.forEach((node) => {
    xs.push(node.x, node.x + node.width);
    ys.push(node.y, node.y + node.height);
  });
  edges.forEach((edge) => (edge.points || []).forEach((point) => {
    xs.push(point.x);
    ys.push(point.y);
  }));
  const padding = 34;
  let minX = Math.min(...xs) - padding;
  let maxX = Math.max(...xs) + padding;
  let minY = Math.min(...ys) - padding;
  let maxY = Math.max(...ys) + padding;
  const minWidth = Math.min(Number(diagram.width) || 0, 520);
  const minHeight = Math.min(Number(diagram.height) || 0, 320);
  if (maxX - minX < minWidth) {
    const delta = (minWidth - (maxX - minX)) / 2;
    minX -= delta;
    maxX += delta;
  }
  if (maxY - minY < minHeight) {
    const delta = (minHeight - (maxY - minY)) / 2;
    minY -= delta;
    maxY += delta;
  }
  return `${minX} ${minY} ${maxX - minX} ${maxY - minY}`;
}

function graphRectOverlaps(left, right, padding = 0) {
  return !(
    left.x + left.width + padding <= right.x - padding
    || right.x + right.width + padding <= left.x - padding
    || left.y + left.height + padding <= right.y - padding
    || right.y + right.height + padding <= left.y - padding
  );
}

function graphSegmentHitsRect(start, end, rect, padding = 0) {
  const left = rect.x - padding;
  const right = rect.x + rect.width + padding;
  const top = rect.y - padding;
  const bottom = rect.y + rect.height + padding;
  if (start.x === end.x) {
    const low = Math.min(start.y, end.y), high = Math.max(start.y, end.y);
    return start.x > left && start.x < right && Math.max(low, top) < Math.min(high, bottom);
  }
  if (start.y === end.y) {
    const low = Math.min(start.x, end.x), high = Math.max(start.x, end.x);
    return start.y > top && start.y < bottom && Math.max(low, left) < Math.min(high, right);
  }
  return false;
}

function syncArchitectureCanvasSelectionUrl() {
  if (!ARCHITECTURE_CANVAS_MODE || typeof history === 'undefined') return false;
  const guard = captureNavigationGuard(state.projectId);
  if (!committedProjectGuardIsCurrent(guard)) return false;
  return commitNavigation({
    projectId:state.projectId,
    view:'architecture',
    canvas:true,
    nodeId:state.selectedComponentId,
    inspectorTab:state.inspectorTab,
  }, {historyMode:'replace', guard});
}

function setArchitectureInspectorTab(tab) {
  const normalized = String(tab || '').toLowerCase();
  if (!INSPECTOR_TABS.has(normalized)) return false;
  state.inspectorTab = normalized;
  syncArchitectureCanvasSelectionUrl();
  renderSelectedNode();
  return true;
}

function architectureInspectorTabsMarkup() {
  const labels = {
    overview:'Overview', dependencies:'Dependencies', tasks:'Tasks', evidence:'Evidence', code:'Code', decisions:'Decisions',
  };
  return `<nav class="architecture-inspector-tabs" aria-label="Architecture inspector sections">${[...INSPECTOR_TABS].map((tab)=>`<button type="button" data-inspector-tab="${tab}" class="${state.inspectorTab===tab?'active':''}" aria-pressed="${state.inspectorTab===tab}">${labels[tab]}</button>`).join('')}</nav>`;
}

function wireArchitectureInspectorTabs() {
  $('nodeEvidence')?.querySelectorAll('[data-inspector-tab]').forEach((button)=>button.addEventListener('click',()=>setArchitectureInspectorTab(button.dataset.inspectorTab)));
}

function graphEdgeById(edgeId, diagram = state.diagram) {
  return (diagram?.edges || []).find((edge)=>edge.id===edgeId) || null;
}

function clearArchitectureTracePath({render = true} = {}) {
  state.tracePathRequestSerial += 1;
  state.tracePathRequest = null;
  state.tracePathResult = null;
  state.tracePathLoading = false;
  state.tracePathError = null;
  if (render) renderGraph();
}

function architectureTraceRequestForNode(node) {
  const request = state.tracePathRequest;
  const version = state.diagram?.architectureVersion ?? state.architecture?.version;
  if (
    !node
    || !request
    || request.source_id !== node.id
    || Number(request.expected_architecture_version) !== Number(version)
  ) return null;
  return request;
}

function architectureTraceForNode(node) {
  const request = architectureTraceRequestForNode(node);
  const result = request ? state.tracePathResult : null;
  if (!result || Number(result.architecture_version) !== Number(request.expected_architecture_version)) return null;
  return result;
}

async function requestArchitectureTracePath(sourceNodeId, targetNodeId) {
  const architectureVersion = Number(state.architecture?.version || 0);
  if (!state.projectId || !architectureVersion || !sourceNodeId || !targetNodeId) return false;
  const request = {
    source_id: sourceNodeId,
    target_id: targetNodeId,
    max_hops: 8,
    expected_architecture_version: architectureVersion,
  };
  const serial = ++state.tracePathRequestSerial;
  state.tracePathRequest = request;
  state.tracePathResult = null;
  state.tracePathError = null;
  state.tracePathLoading = true;
  renderGraph();
  const query = new URLSearchParams({
    source_id: request.source_id,
    target_id: request.target_id,
    max_hops: String(request.max_hops),
    expected_architecture_version: String(request.expected_architecture_version),
  });
  try {
    const result = await api(
      `/projects/${encodeURIComponent(state.projectId)}/architecture/path?${query.toString()}`,
    );
    if (serial !== state.tracePathRequestSerial) return false;
    if (result?.schema !== 'archbro.architecture_path.v1') {
      throw new Error('Trace Path returned an unsupported backend contract.');
    }
    if (Number(result.architecture_version) !== architectureVersion) {
      throw new Error(`Trace Path is stale: expected Architecture v${architectureVersion}.`);
    }
    state.tracePathResult = result;
    return result.status === 'FOUND';
  } catch (error) {
    if (serial === state.tracePathRequestSerial) {
      state.tracePathError = error?.message || String(error);
    }
    return false;
  } finally {
    if (serial === state.tracePathRequestSerial) {
      state.tracePathLoading = false;
      renderGraph();
    }
  }
}

function architectureTraceControlsMarkup(node, diagram) {
  if (!ARCHITECTURE_CANVAS_MODE) return '';
  const targets = (diagram?.nodes || [])
    .filter((candidate) => candidate.id !== node.id)
    .sort((left, right) => left.order - right.order || left.id.localeCompare(right.id));
  const request = architectureTraceRequestForNode(node);
  const result = architectureTraceForNode(node);
  const selectedTarget = request?.target_id || targets[0]?.id || '';
  let status = '';
  if (request && state.tracePathLoading) {
    status = 'Tracing canonical path…';
  } else if (request && state.tracePathError) {
    status = state.tracePathError;
  } else if (result?.status === 'FOUND') {
    const labels = (result.nodes || []).map((item) => item.name || item.label || item.component_id || item.node_id).filter(Boolean);
    status = `FOUND · ${result.hops} hop${result.hops === 1 ? '' : 's'}${labels.length ? ` · ${labels.join(' → ')}` : ''}`;
  } else if (result?.status === 'UNREACHABLE') {
    status = 'No authored directed path.';
  } else if (result?.status === 'LIMIT_REACHED') {
    status = 'Path exceeds the 8-hop trace limit.';
  }
  const options = targets.map((target) => (
    `<option value="${escapeHtml(target.id)}"${target.id === selectedTarget ? ' selected' : ''}>${escapeHtml(target.label)}</option>`
  )).join('');
  const traceControls = targets.length
    ? `<label>Trace Path <select data-trace-target aria-label="Trace Path target">${options}</select></label><button type="button" data-trace-path${state.tracePathLoading ? ' disabled' : ''}>Trace Path</button>`
    : '<span class="muted">No other canonical node is available to trace.</span>';
  return `<div class="graph-focus-controls graph-trace-controls">${traceControls}${request ? '<button type="button" data-clear-trace>Clear path</button>' : ''}<button type="button" data-agent-explore>Explore from here</button>${status ? `<span data-trace-status aria-live="polite">${escapeHtml(status)}</span>` : ''}</div>`;
}

function selectGraphEdge(edgeId) {
  if (ARCHITECTURE_CANVAS_MODE) state.canvasInspectorOpen=true;
  const edge = graphEdgeById(edgeId);
  if (!edge) return false;
  clearArchitectureTracePath({render:false});
  state.selectedEdgeId = edge.id;
  state.selectedComponentId = null;
  state.inspectorTab = 'overview';
  state.graphFocusMode = 'all';
  syncArchitectureCanvasSelectionUrl();
  renderGraph();
  return true;
}

function graphRelationshipVisualClass(edge) {
  const category = String(edge?.relationship_category || 'SUPPORT').toUpperCase();
  return {
    FLOW:'relationship-flow',
    DATA:'relationship-data',
    EVENT:'relationship-event',
    ACCESS:'relationship-access',
    OBSERVABILITY:'relationship-observability',
    VALIDATION:'relationship-validation',
    DELIVERY:'relationship-delivery',
    SUPPORT:'relationship-support',
  }[category] || 'relationship-support';
}

let graphLabelMeasureContext = null;

function graphMeasuredLabelWidth(label) {
  if (!graphLabelMeasureContext) graphLabelMeasureContext = document.createElement('canvas').getContext('2d');
  const family = getComputedStyle(document.body).fontFamily || 'Arial, sans-serif';
  graphLabelMeasureContext.font = `750 9.5px ${family}`;
  return Math.max(42, Math.ceil(graphLabelMeasureContext.measureText(label).width) + 12);
}

// Grapheme-aware measured wrapping handles CJK and long identifiers safely.
function graphWrapPixels(value, width, maxLines, font = '750 14px') {
  if (!graphLabelMeasureContext) graphLabelMeasureContext = document.createElement('canvas').getContext('2d');
  graphLabelMeasureContext.font = `${font} ${getComputedStyle(document.body).fontFamily || 'Arial, sans-serif'}`;
  const source = String(value || '').replace(/\s+/g, ' ').trim();
  const graphemes=text=>typeof Intl.Segmenter === 'function'
    ? [...new Intl.Segmenter(undefined,{granularity:'grapheme'}).segment(text)].map(item=>item.segment) : Array.from(text);
  // Keep English words intact; split long identifiers and CJK by grapheme.
  const tokens=(source.match(/[A-Za-z0-9][A-Za-z0-9'&/−-]*|\s+|./gu) || [])
    .flatMap(token=>graphLabelMeasureContext.measureText(token).width>width ? graphemes(token) : [token]);
  const lines=[]; let line='';
  for (const token of tokens) {
    const next=line+token;
    if (graphLabelMeasureContext.measureText(next).width<=width) {line=next;continue;}
    if (lines.length===maxLines-1) {
      let chars=graphemes(line.trimEnd());
      while(chars.length && graphLabelMeasureContext.measureText(chars.join('')+'…').width>width)chars.pop();
      lines.push(chars.join('')+'…');return lines;
    }
    if (line.trim()) lines.push(line.trim());
    line=token.trimStart();
  }
  if(line.trim())lines.push(line.trim());
  return lines;
}

let canvasReadingView = {key:null, id:'backbone'};
let canvasSummaryState = {key:null,expanded:new Set()};

function activeCanvasReadingView(diagram) {
  const key = graphViewportKey(diagram);
  if (canvasReadingView.key !== key) {canvasReadingView = {key, id:'backbone'};canvasSummaryState={key,expanded:new Set()};}
  return (diagram.readingViews || []).find((view) => view.id === canvasReadingView.id) || null;
}

function canvasReadingToolbar(diagram) {
  const view = activeCanvasReadingView(diagram);
  const journey=view?.kind==='AUTHORED_JOURNEY';
  const allRelationships=!view;
  const steps=journey ? `<div class="canvas-journey-strip" aria-label="Journey sequence">${view.nodeIds.map((id,index)=>`<button type="button" data-journey-node="${escapeHtml(id)}"><span>${String(index+1).padStart(2,'0')}</span>${escapeHtml(diagramNodeById(id)?.label || id)}</button>${index<view.nodeIds.length-1?'<i aria-hidden="true">→</i>':''}`).join('')}</div>` : '';
  const hint=journey ? 'Follow the journey. Select a step to explore.' : allRelationships ? 'Every accepted relationship is visible. Compatible connections remain grouped.' : 'Primary system structure. Select a component to reveal its other connections.';
  return `<div class="canvas-reading-toolbar"><div class="canvas-reading-choice"><span class="canvas-view-icon" aria-hidden="true">◈</span><label><span>DETAIL LEVEL</span><select data-canvas-reading-view aria-label="Architecture detail level">${(diagram.readingViews || []).map(item=>`<option value="${escapeHtml(item.id)}"${item.id===view?.id?' selected':''}>${escapeHtml(item.id==='backbone'?'Overview':item.label)}</option>`).join('')}<option value="all"${!view?' selected':''}>Complete detail</option></select></label></div><span class="canvas-reading-hint">${hint}</span><span class="canvas-readonly"><i></i>Accepted architecture</span>${canvasSummaryState.expanded.size?'<button type="button" class="canvas-summary-reset" data-regroup-connections>Regroup lines</button>':''}${graphViewportControlsMarkup()}</div>${steps}`;
}

function canvasDomainForNode(node, diagram) {
  if(!node || !diagram) return null;
  const roots=diagram.nodes.filter(item=>!item.parent_id);
  const rootId=node.hierarchyPath?.[0] || node.id;
  const index=roots.findIndex(item=>item.id===rootId);
  if(index<0) return null;
  const tones=[['#147d73','#f0f8f6'],['#5265ad','#f3f5fc'],['#97602e','#fcf7f0'],['#307dac','#f0f7fc'],['#875794','#f8f3fa'],['#71834a','#f6f8f1']];
  const [color,wash]=tones[index%tones.length];
  return {id:rootId,label:roots[index].label || rootId,color,wash,tone:index%tones.length};
}

function canvasDomainToneAttr(node, diagram) {
  const domain=canvasDomainForNode(node,diagram);
  return domain ? `data-domain-tone=\"${domain.tone}\"` : '';
}


function canvasConnectionDomain(edge, selected, diagram) {
  if(!selected || !diagram?.fullCanvas) return null;
  const peerId=edge.source===selected.id ? edge.target : edge.target===selected.id ? edge.source : null;
  return canvasDomainForNode(diagram.nodes.find(node=>node.id===peerId),diagram);
}

function canvasReciprocalPartner(edge, edges) {
  if (edge.routing!=='ORTHOGONAL_CANVAS_RECIPROCAL' || edge.source===edge.target || edge.projection_kind==='COLLAPSED') return null;
  const pair=edges.filter(other=>(other.source===edge.source && other.target===edge.target) || (other.source===edge.target && other.target===edge.source));
  if(pair.length!==2) return null;
  const other=pair.find(other=>other.id!==edge.id);
  return other?.routing===edge.routing && other.source===edge.target && other.target===edge.source
    && other.points.length===edge.points.length && edge.points.every((p,i)=>{
      const q=other.points[other.points.length-1-i];return p.x===q.x && p.y===q.y;
    }) ? other : null;
}

function canvasVisibleConnections(diagram, display, focus, selected) {
  const view = activeCanvasReadingView(diagram);
  const primary = view ? new Set(view.edgeIds) : null;
  const selectedRelationship=display.edges.find(edge=>edge.id===state.selectedEdgeId);
  const selectedPartner=selectedRelationship && view?.kind!=='AUTHORED_JOURNEY' && !state.tracePathResult ? canvasReciprocalPartner(selectedRelationship,display.edges) : null;
  const visible=display.edges.filter((edge) => !primary || primary.has(edge.id) || focus?.edges.has(edge.id)
    || (selected && (edge.source===selected.id || edge.target===selected.id)) || edge.id===state.selectedEdgeId || edge.id===selectedPartner?.id);
  // Overview reads a reciprocal pair as one complete connection. Include the
  // server-provided return relationship even if only its peer is backbone.
  // Directed journeys/trace results retain their exact member selection.
  if(view?.kind!=='AUTHORED_JOURNEY' && !state.tracePathResult) {
    const visibleIds=new Set(visible.map(edge=>edge.id));
    for(const edge of [...visible]) {
      const partner=canvasReciprocalPartner(edge,display.edges);
      if(partner && !visibleIds.has(partner.id)) {visible.push(partner);visibleIds.add(partner.id);}
    }
  }
  const seen=new Set(), result=[];
  for(const edge of visible) {
    if(seen.has(edge.id)) continue;
    const partner=view?.kind==='AUTHORED_JOURNEY' || state.tracePathResult ? null : canvasReciprocalPartner(edge,display.edges);
    const members=partner && visible.some(other=>other.id===partner.id) ? [edge,partner] : [edge];
    members.forEach(member=>seen.add(member.id));
    result.push({...edge, canvasLabel:members.length===2 ? '2 directed relationships' : graphWrapPixels(String(edge.label || edge.semantic_type || '').split(' · ')[0],120,1,'750 11px')[0] || '',
      memberIds:members.map(member=>member.id),bidirectional:members.length===2});
  }
  const parallelGroups=new Map(), ungrouped=[];
  for(const edge of result) {
    if(edge.memberIds.length!==1 || edge.bidirectional || edge.source===edge.target) {ungrouped.push(edge);continue;}
    const key=`${edge.source}\u0000${edge.target}\u0000${edge.relationship_category || ''}`;
    if(!parallelGroups.has(key)) parallelGroups.set(key,[]);
    parallelGroups.get(key).push(edge);
  }
  for(const group of parallelGroups.values()) {
    if(group.length<2) {ungrouped.push(...group);continue;}
    const ordered=[...group].sort((a,b)=>a.id.localeCompare(b.id));
    const primary=ordered[0];
    const labels=[...new Set(ordered.map(edge=>String(edge.label || edge.semantic_type || '').split(' · ')[0]).filter(Boolean))];
    ungrouped.push({...primary,memberIds:ordered.map(edge=>edge.id),parallelActions:true,bidirectional:false,canvasLabel:labels.join(' + ') || `${ordered.length} related actions`});
  }
  return canvasSummarizeConnections(ungrouped,diagram,display,view,selected,focus);
}

function canvasSummarizeConnections(connections,diagram,display,view,selected,focus) {
  if(!diagram.connectionSummaries?.length || view?.kind==='AUTHORED_JOURNEY' || state.tracePathResult) return connections;
  const key=graphViewportKey(diagram);
  if(canvasSummaryState.key!==key) canvasSummaryState={key,expanded:new Set()};
  let result=connections;
  for(const summary of diagram.connectionSummaries) {
    if(canvasSummaryState.expanded.has(summary.id) || summary.memberIds.includes(state.selectedEdgeId)) continue;
    if(selected && selected.id!==summary.hubId && summary.memberIds.some(id=>{const e=diagram.edges.find(e=>e.id===id);return e.source===selected.id || e.target===selected.id;})) continue;
    if(focus && summary.memberIds.some(id=>focus.edges.has(id)) && !summary.memberIds.every(id=>focus.edges.has(id))) continue;
    const originals=result.filter(edge=>edge.memberIds.some(id=>summary.memberIds.includes(id)));
    const covered=new Set(originals.flatMap(edge=>edge.memberIds));
    if(covered.size!==summary.memberIds.length || summary.memberIds.some(id=>!covered.has(id)) || originals.some(edge=>edge.bidirectional || edge.projection_kind==='COLLAPSED' || edge.memberIds.some(id=>!summary.memberIds.includes(id)))) continue;
    const primary=originals.find(edge=>edge.memberIds.includes(summary.primaryId));
    if(!primary) continue;
    const label=`${summary.peerCount || summary.memberIds.length} ${summary.direction==='IN'?'sources':'targets'}`;
    result=result.filter(edge=>!edge.memberIds.some(id=>summary.memberIds.includes(id)));
    result.push({...primary,memberIds:summary.memberIds,bidirectional:false,summary,canvasLabel:label});
  }
  return result;
}

function expandCanvasConnectionSummary(summaryId) {
  if(!state.diagram?.connectionSummaries?.some(group=>group.id===summaryId)) return;
  canvasSummaryState.expanded.add(summaryId);
  previewCanvasConnection(null);
  renderGraph();
}

// Local inspection only: highlight exact canonical members and their endpoints.
// The temporary paint order never changes route geometry or selection state.
let canvasConnectionRestore = null;
function previewCanvasConnection(edgeId, individual=false) {
  canvasConnectionRestore?.(); canvasConnectionRestore=null;
  const canvas=$('graphCanvas'), svg=canvas?.querySelector('.living-graph-svg');
  if(!svg || !state.diagram?.fullCanvas) return;
  svg.classList.remove('is-tracing-connection');
  canvas.querySelectorAll('.is-tracing-individual').forEach(el=>el.classList.remove('is-tracing-individual'));
  canvas.querySelectorAll('.is-tracing-connection').forEach(el=>el.classList.remove('is-tracing-connection'));
  document.querySelectorAll('[data-related-edge].is-tracing-connection').forEach(el=>el.classList.remove('is-tracing-connection'));
  const readout=canvas.querySelector('.canvas-connection-readout');
  if(readout) {readout.hidden=true;readout.replaceChildren();}
  if(!edgeId) return;
  const group=[...svg.querySelectorAll('.graph-edge[data-member-edges]')].find(el=>JSON.parse(el.dataset.memberEdges).includes(edgeId));
  if(!group || group.classList.contains('is-isolated-out')) return;
  const summary=state.diagram.connectionSummaries?.find(item=>item.id===group.dataset.summaryId);
  const members=individual && summary ? [edgeId] : JSON.parse(group.dataset.memberEdges);
  const facts=members.map(id=>state.diagram.edges.find(edge=>edge.id===id)).filter(Boolean);
  const endpointIds=new Set(facts.flatMap(edge=>[edge.source,edge.target]));
  svg.classList.add('is-tracing-connection'); group.classList.add('is-tracing-connection');
  svg.querySelectorAll('[data-node]').forEach(el=>el.classList.toggle('is-tracing-connection',endpointIds.has(el.dataset.node)));
  document.querySelectorAll('[data-related-edge]').forEach(el=>el.classList.toggle('is-tracing-connection',members.includes(el.dataset.relatedEdge)));
  const paint=document.createElementNS('http://www.w3.org/2000/svg','g');
  paint.classList.add('canvas-trace-paint');paint.setAttribute('aria-hidden','true');
  const domainTone=group.getAttribute('data-domain-tone');
  if(domainTone!==null) paint.setAttribute('data-domain-tone',domainTone);
  group.querySelectorAll('.graph-edge-casing,.graph-edge-line,.graph-edge-arrow,.canvas-summary-junction').forEach(path=>{
    const copy=path.cloneNode(true);
    copy.setAttribute('class',path.classList.contains('canvas-summary-junction')?'canvas-trace-junction':path.classList.contains('graph-edge-casing')?'canvas-trace-casing':'canvas-trace-stroke');
    paint.append(copy);
  });
  if(individual && summary) {
    group.classList.add('is-tracing-individual');paint.replaceChildren();
    const points=summary.memberPaths[edgeId],d=graphPathData(points,4),end=points.at(-1),previous=points.at(-2);
    for(const cls of ['canvas-trace-casing','canvas-trace-stroke']) {
      const path=document.createElementNS('http://www.w3.org/2000/svg','path');path.setAttribute('class',cls);path.setAttribute('d',d);paint.append(path);
    }
    const arrow=document.createElementNS('http://www.w3.org/2000/svg','path');
    arrow.setAttribute('class','canvas-trace-stroke');arrow.setAttribute('d','M -6 -3 L 0 0 L -6 3');
    arrow.setAttribute('data-arrow-transform',`translate(${end.x} ${end.y}) rotate(${Math.atan2(end.y-previous.y,end.x-previous.x)*180/Math.PI})`);paint.append(arrow);
  }
  svg.insertBefore(paint,svg.querySelector('[data-node]'));
  updateGraphViewportDensity(svg);
  canvasConnectionRestore=()=>paint.remove();
  if(readout) {
    if(domainTone!==null) readout.setAttribute('data-domain-tone',domainTone); else readout.removeAttribute('data-domain-tone');
    for(const fact of facts) {
      const row=document.createElement('div'), heading=document.createElement('strong'), detail=document.createElement('span');
      const name=id=>state.diagram.nodes.find(node=>node.id===id)?.label || id;
      heading.textContent=`${name(fact.source)} → ${name(fact.target)}`;
      detail.textContent=fact.label || fact.semantic_type || '';
      row.append(heading,detail);readout.append(row);
    }
    readout.hidden=false;
  }
}

// Short action labels stay on their route; full action text lives beside the
// selected component and in the relationship Inspector, never in a remote lane.
function canvasEdgeLabelPlacements(edges, nodes, labelledIds) {
  const placements = new Map(), occupied = [];
  for (const edge of edges) {
    if (!labelledIds.has(edge.id) || !edge.canvasLabel) continue;
    const labelWidth=Math.max(32,graphMeasuredLabelWidth(edge.canvasLabel)*1.18+8);
    if(edge.summary) {
      const shared=edge.summary.sharedPath;
      const candidates=shared.slice(1).map((end,i)=>({start:shared[i],end,length:Math.abs(end.x-shared[i].x)+Math.abs(end.y-shared[i].y)})).filter(seg=>seg.start.y===seg.end.y && seg.length>=labelWidth+24).sort((a,b)=>b.length-a.length);
      let slot=null;
      for(const seg of candidates) {
        for(const fraction of [.7,.5,.3]) {
          const x=seg.start.x+(seg.end.x-seg.start.x)*fraction;
          for(const offset of [-24,24,-36,36]) {
            const y=seg.start.y+offset,box={x:x-labelWidth/2,y:y-10,width:labelWidth,height:20};
            if(nodes.some(node=>graphRectOverlaps(box,node,6)) || occupied.some(other=>graphRectOverlaps(box,other,5)) || edges.some(other=>other.id!==edge.id && other.points.slice(1).some((end,i)=>graphSegmentHitsRect(other.points[i],end,box,3)))) continue;
            slot={x,y:y+3,box,attachment:{x,y:seg.start.y}};break;
          }
          if(slot) break;
        }
        if(slot) break;
      }
      if(!slot) {
        for(let i=1;i<shared.length;i++) {
          const start=shared[i-1],end=shared[i];
          if(start.x!==end.x || Math.abs(start.y-end.y)<24) continue;
          for(const dx of [16,-16,0,24,-24]) {
            const x=start.x+dx,y=(start.y+end.y)/2,box={x:x-labelWidth/2,y:y-10,width:labelWidth,height:20};
            if(nodes.some(node=>graphRectOverlaps(box,node,4)) || occupied.some(other=>graphRectOverlaps(box,other,5))) continue;
            slot={x,y:y+3,box,attachment:{x:start.x,y}};break;
          }
          if(slot) break;
        }
      }
      if(slot) {placements.set(edge.id,slot);occupied.push(slot.box);continue;}
    }
    const segments = edge.points.slice(1).map((end,index) => ({start:edge.points[index],end,index,length:Math.abs(end.x-edge.points[index].x)+Math.abs(end.y-edge.points[index].y)})).sort((a,b) => b.length-a.length || a.index-b.index);
    let slot = null;
    for (const segment of segments) {
      if (segment.length < (segment.start.y===segment.end.y ? labelWidth+20 : 40)) continue;
      for (const fraction of [.5,.3,.7]) {
        const x=segment.start.x+(segment.end.x-segment.start.x)*fraction, y=segment.start.y+(segment.end.y-segment.start.y)*fraction;
        const box={x:x-labelWidth/2,y:y-10,width:labelWidth,height:20};
        if (nodes.some((node) => graphRectOverlaps(box,node,6)) || occupied.some((other) => graphRectOverlaps(box,other,5))) continue;
        if (edges.some((other) => other.id!==edge.id && other.points.slice(1).some((end,index) => graphSegmentHitsRect(other.points[index],end,box,3)))) continue;
        slot={x,y:y+3,box}; break;
      }
      if (slot) break;
    }
    if (slot) { placements.set(edge.id,slot); occupied.push(slot.box); }
  }
  return placements;
}

function graphEdgeLabelPlacements(edges, nodes) {
  const occupied = [];
  const result = new Map();
  const nodeRects = nodes.map((node) => ({x:node.x, y:node.y, width:node.width, height:node.height}));
  const minNodeY = nodeRects.length ? Math.min(...nodeRects.map((rect) => rect.y)) : 48;
  const maxNodeBottom = nodeRects.length ? Math.max(...nodeRects.map((rect) => rect.y + rect.height)) : 320;
  const segmentsByEdge = new Map(edges.map((edge) => [edge.id, (edge.points || []).slice(0,-1).map((start,index) => {
    const end = edge.points[index+1];
    return {start,end,index,length:Math.abs(end.x-start.x)+Math.abs(end.y-start.y),horizontal:start.y===end.y};
  })]));
  const fractions = [0.5, 0.75, 0.25, 0.88, 0.12, 0.65, 0.35];

  edges.forEach((edge) => {
    const label = String(edge.label || edge.semantic_type || '').trim();
    if (!label) return;
    const width = graphMeasuredLabelWidth(label);
    const height = 17;
    const target = edge.points[edge.points.length - 1];
    const arrowBox = {x:target.x-11, y:target.y-11, width:22, height:22};
    const candidates = [];
    const segments = [...(segmentsByEdge.get(edge.id) || [])].sort((a,b) => b.length-a.length || a.index-b.index);
    segments.forEach((segment) => {
      fractions.forEach((fraction) => {
        const baseX = segment.start.x + (segment.end.x-segment.start.x)*fraction;
        const baseY = segment.start.y + (segment.end.y-segment.start.y)*fraction;
        if (segment.horizontal && segment.length >= 24) {
          [-14, 22, -30, 38, -46, 54, -62, 70].forEach((offset) => {
            const baselineY = baseY + offset;
            candidates.push({segmentIndex:segment.index, x:baseX, y:baselineY, box:{x:baseX-width/2, y:baselineY-height+4, width, height}});
          });
        } else if (!segment.horizontal && segment.length >= 24) {
          [8, 16, 28, 42, 58].forEach((extra) => {
            const distance = width/2 + extra;
            [1,-1].forEach((direction) => {
              const x = baseX + direction*distance;
              const baselineY = baseY + 4;
              candidates.push({segmentIndex:segment.index, x, y:baselineY, box:{x:x-width/2, y:baselineY-height+4, width, height}});
            });
          });
        }
      });
    });

    // Dense short routes can have no local label slot at all. Keep a
    // deterministic overflow lane in the graph padding instead of hiding the
    // relationship or placing text on a node/edge.
    const routeXs = (edge.points || []).map((point) => point.x);
    const routeCenterX = routeXs.length ? (Math.min(...routeXs) + Math.max(...routeXs)) / 2 : 48;
    const laneStep = width + 14;
    const laneXs = [routeCenterX, routeCenterX-laneStep, routeCenterX+laneStep, routeCenterX-2*laneStep, routeCenterX+2*laneStep];
    const topBaseline = Math.max(18, minNodeY - 16);
    const bottomBaseline = maxNodeBottom + 28;
    [topBaseline, bottomBaseline].forEach((baselineY) => laneXs.forEach((x) => {
      candidates.push({segmentIndex:-1, x, y:baselineY, box:{x:x-width/2, y:baselineY-height+4, width, height}});
    }));

    const safe = (candidate, avoidOtherEdges = true) => {
      if (graphRectOverlaps(candidate.box, arrowBox, 3)) return false;
      if (nodeRects.some((rect) => graphRectOverlaps(candidate.box, rect, 4))) return false;
      if (occupied.some((rect) => graphRectOverlaps(candidate.box, rect, 4))) return false;
      const ownSegments = segmentsByEdge.get(edge.id) || [];
      if (ownSegments.some((segment) => graphSegmentHitsRect(segment.start, segment.end, candidate.box, 3))) return false;
      if (!avoidOtherEdges) return true;
      return !edges.some((other) => {
        if (other.id === edge.id) return false;
        return (segmentsByEdge.get(other.id) || []).some((segment) => graphSegmentHitsRect(segment.start, segment.end, candidate.box, 3));
      });
    };

    let selected = candidates.find((candidate) => safe(candidate, true));
    // Preserve complete READ/FULL semantics if a very dense scope has no wholly
    // edge-free label lane. Even this fallback still forbids nodes, other labels,
    // this edge, its elbows, and its arrowhead; acceptance will surface any
    // unrelated-edge collision instead of silently hiding the relationship.
    if (!selected) selected = candidates.find((candidate) => safe(candidate, false));
    if (selected) {
      occupied.push(selected.box);
      result.set(edge.id, selected);
    }
  });
  return result;
}

function applyCanvasNavigationToDiagram(diagram) {
  if (!ARCHITECTURE_CANVAS_MODE || state.canvasDeepLinkApplied) return;
  const route = state.navigation.committed;
  if (!route?.canvas || route.projectId !== state.projectId) return;
  const requestedNode = route.nodeId
    ? diagram.nodes.find((node) => node.component_id === route.nodeId)
    : null;
  if (requestedNode) {
    (requestedNode.hierarchyPath || []).slice(0, -1).forEach((nodeId) => state.collapsedNodeIds.delete(nodeId));
    state.selectedComponentId = requestedNode.component_id;
    state.selectedEdgeId = null;
    state.inspectorTab = INSPECTOR_TABS.has(route.inspectorTab) ? route.inspectorTab : 'overview';
    state.graphFocusMode = 'connected';
    state.canvasDeepLinkFocusPending = true;
    state.canvasInspectorOpen = true;
  } else {
    state.selectedComponentId = null;
    state.selectedEdgeId = null;
    state.inspectorTab = 'overview';
    state.graphFocusMode = 'all';
    state.canvasDeepLinkFocusPending = false;
    state.canvasInspectorOpen = false;
    clearArchitectureTracePath({render:false});
    if (route.nodeId) {
      commitNavigation({...route, nodeId:null, inspectorTab:'overview'}, {
        historyMode:'replace', guard:captureNavigationGuard(state.projectId),
      });
    }
  }
  state.canvasDeepLinkApplied = true;
}

function renderGraph() {
  renderArchitectureChrome();
  document.querySelector('#view-architecture .graph-layout')?.classList.toggle('has-canvas-inspector', state.architectureGraphKind === 'code' || state.canvasInspectorOpen);
  if (state.architectureGraphKind === 'code') {
    renderCodeGraph();
    return;
  }
  const canvas = $('graphCanvas');
  if (!state.architecture?.components?.length) {
    canvas.innerHTML = '<div class="graph-empty"><div><strong>No architecture yet</strong><p class="muted">Architecture v1 has not completed.</p></div></div>';
    renderSelectedNode(); renderLists(); return;
  }
  if (!state.diagram) {
    const resource = state.workspaceAsync.resources.canvas;
    const loading = resource.status === 'loading';
    const failed = resource.status === 'error';
    const title = loading ? 'Loading positioned diagram…' : failed ? 'Positioned diagram unavailable' : 'Positioned diagram unavailable';
    const detail = failed
      ? escapeHtml(resource.error || state.diagramError || 'The positioned diagram could not be loaded.')
      : loading
        ? 'The workspace is ready. Canvas geometry is loading independently.'
        : escapeHtml(state.diagramError || 'The backend has not published the scoped positioned Diagram View for this architecture version.');
    const retry = failed ? '<button class="btn secondary" type="button" data-retry-workspace-resource="canvas">Retry diagram</button>' : '';
    canvas.innerHTML = `<div class="graph-empty"><div><strong>${title}</strong><p class="muted">${detail}</p>${retry}</div></div>`;
    wireWorkspaceResourceRetry(canvas);
    $('graphReviewState').textContent = loading ? 'Diagram loading' : 'Diagram unavailable'; renderSelectedNode(); renderLists(); return;
  }
  const diagram = state.diagram;
  // Apply committed navigation before deriving the visible projection. A route
  // to a descendant may need to expand collapsed ancestors first.
  applyCanvasNavigationToDiagram(diagram);
  const display = graphDisplayModel(diagram);
  document.querySelector('#view-architecture .graph-layout')?.classList.toggle('has-canvas-inspector',state.canvasInspectorOpen);
  const selected = diagramNodeByComponentId(state.selectedComponentId);
  if (state.selectedComponentId && !selected) state.selectedComponentId = null;
  if (state.selectedEdgeId && !graphEdgeById(state.selectedEdgeId, diagram)) state.selectedEdgeId = null;
  const selectedEdge = graphEdgeById(state.selectedEdgeId, diagram);
  const displayedSelectedEdge = selectedEdge
    ? display.edges.find((edge) => edge.id === selectedEdge.id)
    : null;
  const selectedEdgeFocus = displayedSelectedEdge
    ? {nodes:new Set([displayedSelectedEdge.source,displayedSelectedEdge.target]),edges:new Set([selectedEdge.id]),isolate:false}
    : null;
  const focus = selectedEdgeFocus || graphFocusState(selected, display.edges, display.nodes);
  const nodeById = new Map(diagram.nodes.map((node) => [node.id,node]));
  const attentionNodes = display.nodes.filter((node) => diagramNodeHealth(node).needsAttention);
  const activeTaskCount = state.tasks.filter((task) => task.status === 'IN_PROGRESS').length;
  const groupFrames = diagram.fullCanvas ? display.groupFrames : [];
  const groupFrameByNode = new Map(groupFrames.map((frame) => [frame.nodeId, frame]));
  const currentReadingView=diagram.fullCanvas ? activeCanvasReadingView(diagram) : null;
  const journeyNodes=currentReadingView?.kind==='AUTHORED_JOURNEY' ? new Set(currentReadingView.nodeIds) : null;
  const groups = groupFrames.map((frame) => {
    const owner = nodeById.get(frame.nodeId);
    if (!owner) return '';
    const highlighted=focus ? (focus.nodes.has(owner.id) || diagram.nodes.some(node=>focus.nodes.has(node.id) && (node.hierarchyPath || []).includes(owner.id))) : (!journeyNodes || journeyNodes.has(owner.id) || diagram.nodes.some(node=>journeyNodes.has(node.id) && (node.hierarchyPath || []).includes(owner.id)));
    const boundaryContext=Boolean(focus?.boundaryNodes?.has(owner.id));
    const collapsed=Boolean(display.collapsedNodeIds?.has(owner.id));
    const visibilityClass=focus?.isolate && !highlighted ? ' is-isolated-out' : highlighted ? ' is-focused' : ' is-dimmed';
    return `<g class="architecture-group-frame depth-${frame.depth}${collapsed?' is-collapsed':''}${boundaryContext?' is-boundary-context':''}${visibilityClass}" ${canvasDomainToneAttr(owner,diagram)} data-group-node="${escapeHtml(frame.nodeId)}"><rect x="${frame.x}" y="${frame.y}" width="${frame.width}" height="${frame.height}" rx="18"/></g>`;
  }).join('');
  const hierarchy = display.scoped || diagram.fullCanvas ? '' : diagram.nodes.map((node) => {
    if (!node.parent_id) return '';
    const parent=nodeById.get(node.parent_id); if (!parent) return '';
    return `<line class="graph-hierarchy" x1="${parent.x+parent.width/2}" y1="${parent.y+parent.height/2}" x2="${node.x+node.width/2}" y2="${node.y+node.height/2}"/>`;
  }).join('');
  const visibleEdges = diagram.fullCanvas ? canvasVisibleConnections(diagram, display, focus, selected) : graphVisualConnections(display.edges, display.nodes);
  const readingView = diagram.fullCanvas ? activeCanvasReadingView(diagram) : null;
  const numberedIds = new Set(diagram.fullCanvas ? visibleEdges.filter((edge) => edge.summary || edge.parallelActions || edge.memberIds.includes(state.selectedEdgeId) || (readingView?.kind==='AUTHORED_JOURNEY' && readingView.edgeIds.includes(edge.id))).map((edge) => edge.id) : []);
  const labelPlacements = diagram.fullCanvas ? canvasEdgeLabelPlacements(visibleEdges, display.nodes, numberedIds) : graphEdgeLabelPlacements(visibleEdges, display.nodes);
  const edges = [...visibleEdges].sort((a,b) => Number(a.memberIds.some(id=>focus?.edges.has(id))) - Number(b.memberIds.some(id=>focus?.edges.has(id)))).map((edge) => {
    const highlighted=Boolean(focus ? edge.memberIds.some(edgeId=>focus.edges.has(edgeId)) : (journeyNodes && currentReadingView.edgeIds.includes(edge.id))); const label=edge.label || edge.semantic_type || ''; const labelPlacement=labelPlacements.get(edge.id);
    const projectionKind=edge.summary ? 'summary' : edge.parallelActions ? 'parallel-actions' : edge.memberIds.length > 1 ? 'merged' : String(edge.projection_kind || 'AUTHORED').toLowerCase();
    const sourcePoint=edge.points[0];
    const sourcePort=diagram.fullCanvas || edge.bidirectional ? '' : `<circle class="graph-edge-source-port" cx="${sourcePoint.x}" cy="${sourcePoint.y}" r="2.25"/>`;
    const startMarker=!diagram.fullCanvas && edge.bidirectional ? ' marker-start="url(#arrow-backbone)"' : '';
    const tip=edge.points.at(-1), approach=edge.points.at(-2);
    const arrowTransform=tip && approach ? `translate(${tip.x} ${tip.y}) rotate(${Math.atan2(tip.y-approach.y,tip.x-approach.x)*180/Math.PI})` : '';
    const endArrow=diagram.fullCanvas && arrowTransform ? `<path class="graph-edge-arrow" d="M -6 -3 L 0 0 L -6 3" data-arrow-transform="${arrowTransform}" transform="${arrowTransform}"/>` : '';
    const departure=edge.points[1];
    const startTransform=sourcePoint && departure ? `translate(${sourcePoint.x} ${sourcePoint.y}) rotate(${Math.atan2(sourcePoint.y-departure.y,sourcePoint.x-departure.x)*180/Math.PI})` : '';
    const startArrow=diagram.fullCanvas && edge.bidirectional ? `<path class="graph-edge-arrow graph-edge-arrow-start" d="M -6 -3 L 0 0 L -6 3" data-arrow-transform="${startTransform}" transform="${startTransform}"/>` : '';
    const connectionTitle=edge.memberIds.map(id=>{const member=diagram.edges.find(item=>item.id===id);return `${nodeById.get(member.source)?.label || member.source} → ${nodeById.get(member.target)?.label || member.target}: ${member.label || member.semantic_type || ''}`;}).join('\n');
    const endMarker=diagram.fullCanvas ? '' : ' marker-end="url(#arrow-backbone)"';
    const routeData=edge.summary ? edge.summary.paths.map(points=>graphPathData(points,4)).join(' ') : graphPathData(edge.points,diagram.fullCanvas?4:8);
    const junctionDots=(edge.summary?.junctions || []).map(point=>`<circle class="canvas-summary-junction-hit" cx="${point.x}" cy="${point.y}" r="8" data-junction-radius="8" aria-hidden="true"/><circle class="canvas-summary-junction" cx="${point.x}" cy="${point.y}" r="2" data-junction-radius="2" aria-hidden="true"/>`).join('');
    const casing=diagram.fullCanvas ? `<path class="graph-edge-casing" d="${routeData}"/>` : '';
    const branchArrows=edge.summary?.direction==='OUT' ? edge.summary.paths.slice(1).map(points=>{
      const end=points.at(-1),previous=points.at(-2),transform=`translate(${end.x} ${end.y}) rotate(${Math.atan2(end.y-previous.y,end.x-previous.x)*180/Math.PI})`;
      return `<path class="graph-edge-arrow" d="M -6 -3 L 0 0 L -6 3" data-arrow-transform="${transform}" transform="${transform}"/>`;
    }).join('') : '';

    const selectedVisual=edge.memberIds.includes(state.selectedEdgeId);
    const visibilityClass=focus?.isolate && !highlighted ? ' is-isolated-out' : highlighted ? ' is-focused' : focus ? ' is-dimmed' : '';
    const selectableEdgeId=selectedVisual ? state.selectedEdgeId : edge.memberIds[0];
    const relationClass=graphRelationshipVisualClass(edge);
    const directIncoming=selected && edge.memberIds.some(id=>diagram.edges.find(item=>item.id===id)?.target===selected.id);
    const directOutgoing=selected && edge.memberIds.some(id=>diagram.edges.find(item=>item.id===id)?.source===selected.id);
    const flowDirection=directIncoming && directOutgoing ? 'both' : directIncoming ? 'in' : directOutgoing ? 'out' : '';
    const connectionDomain=canvasConnectionDomain(edge,selected,diagram);

    return `<g class="graph-edge projection-${escapeHtml(projectionKind)} layout-backbone ${relationClass}${visibilityClass}${selectedVisual?' selected':''}" data-edge="${escapeHtml(selectableEdgeId)}" data-member-edges="${escapeHtml(JSON.stringify(edge.memberIds))}" data-summary-id="${escapeHtml(edge.summary?.id || '')}" data-flow-direction="${flowDirection}" data-connection-domain="${escapeHtml(connectionDomain?.id || '')}" data-domain-tone="${connectionDomain?.tone ?? ''}" role="button" tabindex="0" aria-label="${edge.summary?'Expand '+edge.canvasLabel:'Inspect '+(edge.bidirectional?'two-way connection':'relationship')} ${escapeHtml(connectionTitle)}"><title>${escapeHtml(connectionTitle)}</title><path class="graph-edge-hit" d="${routeData}"/>${sourcePort}${casing}<path class="graph-edge-line" d="${routeData}"${startMarker}${endMarker}/>${endArrow}${startArrow}${branchArrows}${junctionDots}${label && labelPlacement ? `${labelPlacement.attachment?`<path class="canvas-summary-label-leader" d="M ${labelPlacement.attachment.x} ${labelPlacement.attachment.y} L ${labelPlacement.x} ${labelPlacement.y-3}"/>`: ''}${diagram.fullCanvas ? `<rect class="canvas-edge-key-bg" x="${labelPlacement.box.x}" y="${labelPlacement.box.y}" width="${labelPlacement.box.width}" height="20" rx="6"/>` : ''}<text class="graph-edge-label ${diagram.fullCanvas?'canvas-edge-key':'graph-detail-read'}" data-edge-label="${escapeHtml(edge.id)}" x="${labelPlacement.x}" y="${labelPlacement.y}" text-anchor="middle">${escapeHtml(diagram.fullCanvas?edge.canvasLabel:label)}</text>` : ''}</g>`;
  }).join('');
  const nodes = display.nodes.map((node) => {
    const health=diagramNodeHealth(node), selectedNode=state.selectedComponentId===node.component_id, highlighted=focus ? focus.nodes.has(node.id) : (!journeyNodes || journeyNodes.has(node.id) || diagram.nodes.some(child=>journeyNodes.has(child.id) && (child.hierarchyPath || []).includes(node.id)));
    const boundaryContext=Boolean(focus?.boundaryNodes?.has(node.id));
    const collapsed=Boolean(display.collapsedNodeIds?.has(node.id));
    const visibilityClass=focus?.isolate && !highlighted ? ' is-isolated-out' : highlighted ? ' is-focused' : ' is-dimmed';
    const groupFrame=groupFrameByNode.get(node.id);
    if (groupFrame) {
      const contained=diagram.nodes.filter(item=>(item.hierarchyPath || []).slice(0,-1).includes(node.id));
      const count=contained.length;
      const foldControl=`<g class="canvas-group-fold" data-fold-group="${escapeHtml(node.id)}" role="button" tabindex="0" aria-label="${collapsed?'Expand':'Group'} ${escapeHtml(node.label)}: ${count} components"><rect x="${node.x+node.width-52}" y="${node.y+5}" width="46" height="24" rx="6"/><text x="${node.x+node.width-29}" y="${node.y+22}" text-anchor="middle">${collapsed?'+':'−'} ${count}</text></g>`;
      const summaryCard=collapsed?`<g class="canvas-group-summary" data-fold-group="${escapeHtml(node.id)}" role="button" tabindex="0" aria-label="Expand ${escapeHtml(node.label)}: ${count} components"><rect x="${groupFrame.x+20}" y="${groupFrame.y+48}" width="${groupFrame.width-40}" height="64" rx="10"/><text x="${groupFrame.x+36}" y="${groupFrame.y+74}">${count} components grouped</text><text class="canvas-group-summary-hint" x="${groupFrame.x+36}" y="${groupFrame.y+96}">Expand to follow individual components</text></g>`:'';
      const role=node.projectionRole || 'PRIMARY', action='drill';
      const ariaLabel=`Inspect ${node.label}; ${collapsed?'collapsed':'expanded'} subsystem with ${node.childCount} child${node.childCount===1?'':'ren'}`;
      return `<g class="node-card architecture-group-owner projection-${role.toLowerCase()} health-${health.key} is-${action}${selectedNode ? ' selected' : ''}${collapsed?' is-collapsed':''}${boundaryContext?' is-boundary-context':''}${visibilityClass}" ${canvasDomainToneAttr(node,diagram)} data-node="${escapeHtml(node.id)}" data-component="${escapeHtml(node.component_id)}" data-child-count="${node.childCount}" data-collapsed="${collapsed}" data-projection-role="${role}" data-node-action="${action}" role="button" aria-label="${escapeHtml(ariaLabel)}" tabindex="0"><rect class="architecture-group-owner-hit" x="${node.x}" y="${node.y}" width="${node.width}" height="${node.height}" rx="12"/><text class="architecture-group-owner-name" x="${node.x+14}" y="${node.y+22}">${escapeHtml(graphWrapPixels(node.label,node.width-52,1,'700 17px')[0] || '')}</text></g>${foldControl}${summaryCard}`;
    }
    const nameLines=graphWrapPixels(node.label,node.width-44,2,diagram.fullCanvas?'650 17px':'750 13.5px');
    const names=nameLines.map((line,index)=>`<text class="node-name" x="${node.x+22}" y="${node.y+(diagram.fullCanvas?(nameLines.length>1?36:46):55)+index*21}">${escapeHtml(line)}</text>`).join('');
    const responsibility=graphWrapPixels(diagram.fullCanvas?'':node.responsibility,node.width-40,2,'500 10.2px').map((line,index)=>`<text class="node-responsibility graph-detail-read" x="${node.x+18}" y="${node.y+96+index*14}">${escapeHtml(line)}</text>`).join('');
    const drillable=graphNodeAction(node)==='drill', role=node.projectionRole || 'PRIMARY', action=drillable ? 'drill' : 'inspect';
    const drillLabel=ARCHITECTURE_CANVAS_MODE ? `FOCUS · ${node.childCount} CHILD${node.childCount===1?'':'REN'} ›` : `OPEN · ${node.childCount} CHILD${node.childCount===1?'':'REN'} ›`;
    const cue=drillable ? `<g class="node-drill-action" aria-hidden="true"><rect x="${node.x+node.width-126}" y="${node.y+node.height-31}" width="110" height="22" rx="11"/><text class="node-drill-cue" x="${node.x+node.width-25}" y="${node.y+node.height-16}" text-anchor="end">${escapeHtml(drillLabel)}</text></g>` : role==='SCOPE' ? `<text class="node-scope-cue" x="${node.x+node.width-16}" y="${node.y+node.height-13}" text-anchor="end">CURRENT SCOPE</text>` : role==='CONTEXT' ? `<text class="node-context-cue" x="${node.x+node.width-16}" y="${node.y+node.height-13}" text-anchor="end">CONTEXT</text>` : '';
    return `<g class="node-card projection-${role.toLowerCase()} health-${health.key} is-${action}${selectedNode ? ' selected' : ''}${collapsed?' is-collapsed':''}${boundaryContext?' is-boundary-context':''}${visibilityClass}" ${canvasDomainToneAttr(node,diagram)} data-node="${escapeHtml(node.id)}" data-component="${escapeHtml(node.component_id)}" data-child-count="${node.childCount}" data-collapsed="${collapsed}" data-projection-role="${role}" data-node-action="${action}" role="button" aria-label="${escapeHtml(drillable ? `Inspect ${node.label}; ${collapsed?'collapsed':'expanded'} subsystem with ${node.childCount} child${node.childCount===1?'':'ren'}` : `Inspect ${node.label}`)}" tabindex="0"><rect class="node-surface" x="${node.x}" y="${node.y}" width="${node.width}" height="${node.height}" rx="16"/>${graphNodeKindMarkup(node)}<path class="node-domain-tick" d="M${node.x+10} ${node.y+node.height/2-9}v18"/><circle class="node-health-dot" cx="${node.x+node.width-20}" cy="${node.y+21}" r="4.5"/>${names}${responsibility}<text class="node-status graph-detail-full${diagram.fullCanvas?' canvas-hidden-detail':''}" x="${node.x+18}" y="${node.y+node.height-13}">${escapeHtml(health.label)} · depth ${node.depth}</text>${cue}</g>`;
  }).join('');
  $('graphReviewState').textContent=attentionNodes.length ? `${attentionNodes.length} node${attentionNodes.length===1?'':'s'} need attention` : 'All projected nodes aligned';
  const boundaryRelationshipCount = diagram.scope?.directRelationships?.length || 0;
  const collapsedConnectionMeta = visibleEdges.length !== display.edges.length
    ? ` · ${visibleEdges.length} visual connection${visibleEdges.length===1?'':'s'}`
    : '';
  const relationshipMeta = diagram.fullCanvas
    ? `${visibleEdges.filter((edge)=>!focus?.isolate || edge.memberIds.some(id=>focus.edges.has(id))).reduce((sum,edge)=>sum+edge.memberIds.length,0)} shown / ${diagram.edges.length} total relationships${visibleEdges.some(edge=>edge.bidirectional)?` · ${visibleEdges.length} lines`:''}`
    : display.scoped
    ? `${display.edges.length} direct child relationship${display.edges.length===1?'':'s'}${collapsedConnectionMeta}`
    : `${display.edges.length} projected relationship${display.edges.length===1?'':'s'}${collapsedConnectionMeta}`;
  const scopeMeta = display.scoped && boundaryRelationshipCount
    ? `<span class="graph-meta-scope">${boundaryRelationshipCount} boundary relationship${boundaryRelationshipCount===1?'':'s'}${display.edges.length===0 ? ' · kept at scope boundary' : ''}</span>`
    : display.scoped && display.edges.length === 0
      ? '<span class="graph-meta-scope">No authored child-to-child links</span>'
      : '';
  const hierarchyMeta=diagram.fullCanvas ? `<span>${groupFrames.length} architecture boundar${groupFrames.length===1?'y':'ies'}</span>` : '';
  const collapsedMeta=display.collapsedNodeIds?.size ? `<span>${display.collapsedNodeIds.size} collapsed</span>` : '';
  const meta=`<div class="graph-meta"><span>${display.nodes.length} components</span>${hierarchyMeta}<span>${relationshipMeta}</span>${collapsedMeta}${scopeMeta}<span>${activeTaskCount} task${activeTaskCount===1?'':'s'} active</span><span>Accepted v${diagram.architectureVersion}</span>${attentionNodes.length ? `<span class="graph-meta-attention">${attentionNodes.length} need attention</span>` : ''}</div>`;
  const fitNodes = focus?.isolate ? display.nodes.filter((node)=>focus.nodes.has(node.id)) : display.nodes;
  const fitEdges = focus?.isolate ? visibleEdges.filter((edge)=>edge.memberIds.some((edgeId)=>focus.edges.has(edgeId))) : (diagram.fullCanvas ? display.edges : visibleEdges);
  const fitGroupFrames = focus?.isolate ? groupFrames.filter((frame)=>focus.nodes.has(frame.nodeId)) : groupFrames;
  const fitViewBox = graphDisplayViewBox(diagram.fullCanvas ? {...diagram,groupFrames:fitGroupFrames} : diagram, fitNodes, fitEdges);
  const viewBox = ARCHITECTURE_CANVAS_MODE ? resolvedGraphViewport(fitViewBox, graphViewportKey(diagram)) : fitViewBox;
  const toolbar=diagram.fullCanvas ? canvasReadingToolbar(diagram) : graphScopeToolbar(diagram);
  canvas.dataset.journey=Boolean(journeyNodes);
  const graphAria=diagram.fullCanvas ? 'Accepted full-system Living Architecture canvas' : 'Accepted scoped project architecture graph';
  canvas.innerHTML=`${toolbar}<div class="graph-stage" data-reading-mode="${state.readingMode}">${meta}${diagram.fullCanvas?'':graphViewportControlsMarkup()}<svg class="living-graph-svg" data-fit-view-box="${escapeHtml(fitViewBox)}" viewBox="${escapeHtml(viewBox)}" role="img" aria-label="${graphAria}"><defs><marker id="arrow-backbone" markerUnits="userSpaceOnUse" markerWidth="10" markerHeight="10" viewBox="0 0 10 10" refX="8.5" refY="5" orient="auto-start-reverse" overflow="visible"><path d="M1.5 1.5 L8.5 5 L1.5 8.5" fill="none" stroke="var(--brand-deep)" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></marker></defs>${groups}${hierarchy}${edges}${nodes}</svg>${diagram.fullCanvas?'<div class="canvas-connection-readout" hidden aria-live="polite"></div>':''}</div>`;
  canvas.querySelectorAll('button[data-reading-mode]').forEach((button)=>button.addEventListener('click',()=>setGraphReadingMode(button.dataset.readingMode)));
  canvas.querySelector('[data-graph-back]')?.addEventListener('click',()=>navigateGraphScope(parentGraphScopeComponentId(diagram),{focusComponentId:state.scopeComponentId}));
  canvas.querySelectorAll('[data-scope-target]').forEach((button)=>button.addEventListener('click',()=>navigateGraphScope(button.dataset.scopeTarget || null,{focusComponentId:state.scopeComponentId})));
  canvas.querySelector('[data-canvas-reading-view]')?.addEventListener('change', (event) => {
    canvasReadingView.id=event.target.value;
    state.selectedComponentId=null; state.selectedEdgeId=null; state.graphFocusMode='all'; state.canvasInspectorOpen=false; clearArchitectureTracePath({render:false}); syncArchitectureCanvasSelectionUrl();
    renderGraph();
  });
  canvas.querySelector('[data-regroup-connections]')?.addEventListener('click',()=>{canvasSummaryState.expanded.clear();renderGraph();});
  canvas.querySelectorAll('[data-journey-node]').forEach(button=>button.addEventListener('click',()=>activateGraphNode(diagramNodeById(button.dataset.journeyNode))));
  const focusNode=async(el)=>{ const node=diagramNodeById(el.dataset.node); await activateGraphNode(node); setTimeout(()=>{if(state.selectedComponentId===node.component_id)document.querySelector(`[data-component="${CSS.escape(node.component_id)}"]`)?.focus();},0); };
  const drillNode=async(el)=>{ const node=diagramNodeById(el.dataset.node); await drillGraphNode(node); };
  canvas.querySelectorAll('[data-fold-group]').forEach(el=>{const fold=event=>{event.stopPropagation();toggleGraphNodeCollapse(el.dataset.foldGroup);};el.addEventListener('click',fold);el.addEventListener('keydown',event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();fold(event);}});});
  canvas.querySelectorAll('[data-node]').forEach((el)=>{
    el.addEventListener('click',()=>{focusNode(el);});
    el.addEventListener('dblclick',(event)=>{ event.preventDefault(); drillNode(el); });
    el.addEventListener('keydown',(event)=>{
      if(event.key==='Enter' && event.shiftKey){event.preventDefault();drillNode(el);return;}
      if(event.key==='ArrowRight' && graphNodeAction(diagramNodeById(el.dataset.node))==='drill'){event.preventDefault();drillNode(el);return;}
      if(event.key==='Enter'||event.key===' '){event.preventDefault();focusNode(el);}
    });
  });
  canvas.querySelectorAll('.graph-edge[data-edge]').forEach((el)=>{
    for(const eventName of ['pointerenter','focus']) el.addEventListener(eventName,()=>previewCanvasConnection(el.dataset.edge));
    for(const eventName of ['pointerleave','blur']) el.addEventListener(eventName,()=>previewCanvasConnection(null));
    el.addEventListener('click',(event)=>{event.stopPropagation();if(el.dataset.summaryId) expandCanvasConnectionSummary(el.dataset.summaryId); else selectGraphEdge(el.dataset.edge);});
    el.addEventListener('keydown',(event)=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();if(el.dataset.summaryId) expandCanvasConnectionSummary(el.dataset.summaryId); else selectGraphEdge(el.dataset.edge);}});
  });
  const graphSvg=canvas.querySelector('.living-graph-svg');
  wireWorkspaceResourceRetry(canvas);
  wireGraphViewport(graphSvg);
  if (state.canvasDeepLinkFocusPending) {
    requestAnimationFrame(() => {
      if (!state.canvasDeepLinkFocusPending) return;
      const target=diagramNodeByComponentId(state.selectedComponentId);
      const currentSvg=canvas.querySelector('.living-graph-svg');
      if (target && focusGraphNodeInViewport(currentSvg, target)) {
        state.canvasDeepLinkFocusPending=false;
      }
    });
  }
  renderSelectedNode(); renderLists(); updateInstructionContext();
}

function canonicalBoundaryRelationshipsForComponent(componentId, diagram = state.diagram) {
  const canonical = findArchitectureNode(componentId);
  if (!canonical || !diagram || diagram.fullCanvas) return [];
  const subtree = new Set(descendantArchitectureIds(canonical));
  const projectedRelationshipIds = new Set(
    (diagram.edges || []).flatMap((edge) => (edge.provenance || []).map((item) => item.relationship_id).filter(Boolean)),
  );
  const seen = new Set();
  return (diagram.scope?.directRelationships || []).filter((relationship) => {
    const id = relationship?.relationship_id;
    if (!id || seen.has(id) || projectedRelationshipIds.has(id)) return false;
    const sourceInside = subtree.has(relationship.source_component_id);
    const targetInside = subtree.has(relationship.target_component_id);
    if (sourceInside === targetInside) return false;
    seen.add(id);
    return true;
  });
}

function canonicalBoundaryRelationshipMarkup(relationship, componentId) {
  const canonical = findArchitectureNode(componentId);
  const subtree = new Set(descendantArchitectureIds(canonical));
  const inbound = subtree.has(relationship.target_component_id);
  const peerId = inbound ? relationship.source_component_id : relationship.target_component_id;
  const peer = findArchitectureNode(peerId);
  return `<li class="canonical-boundary-relationship"><button type="button" data-reveal-component="${escapeHtml(peerId)}"><strong>${inbound ? '← From' : '→ To'} ${escapeHtml(peer?.name || peerId)}</strong><span>${escapeHtml(relationship.semantic_type || 'relationship')} · canonical cross-boundary</span></button></li>`;
}

function renderSelectedNode() {
  for (const panel of [$('selectedNode'),$('nodeEvidence')]) {
    if (panel) {
      const preview=(event)=>{const button=event.target.closest('[data-related-edge]');previewCanvasConnection(button?.dataset.relatedEdge || null,true);};
      panel.onpointerover=preview; panel.onfocusin=preview;
      panel.onpointerleave=()=>previewCanvasConnection(null);
      panel.onfocusout=()=>previewCanvasConnection(null);
    }
    if (panel) panel.onclick = (event) => {
      const button=event.target.closest('[data-related-edge]');
      if (button && panel.contains(button)) selectGraphEdge(button.dataset.relatedEdge);
    };
  }
  const c=diagramNodeByComponentId(state.selectedComponentId), diagram=state.diagram;
  const selectedEdge=graphEdgeById(state.selectedEdgeId, diagram);
  if (selectedEdge && diagram) {
    const source=diagramNodeById(selectedEdge.source), target=diagramNodeById(selectedEdge.target);
    const reciprocal=canvasReciprocalPartner(selectedEdge,diagram.edges);
    const directionPicker=reciprocal ? `<div class="connection-directions"><small>TWO-WAY CONNECTION · 2 DIRECTIONS</small>${[selectedEdge,reciprocal].sort((a,b)=>a.id.localeCompare(b.id)).map(edge=>`<button type="button" data-related-edge="${escapeHtml(edge.id)}" aria-pressed="${edge.id===selectedEdge.id}"><strong>${escapeHtml(diagramNodeById(edge.source)?.label || edge.source)} → ${escapeHtml(diagramNodeById(edge.target)?.label || edge.target)}</strong><span>${escapeHtml(edge.label || edge.semantic_type)}</span></button>`).join('')}</div>` : '';
    const provenance=selectedEdge.provenance || [];
    const tab=state.inspectorTab;
    const canonicalRelationshipIds=[...new Set(provenance.map((item)=>item.relationship_id).filter(Boolean))];
    const relationshipCategory=String(selectedEdge.relationship_category || 'SUPPORT');
    const projectDecisionItems=[...(state.architecture?.decisions || []).map((item)=>`Decision: ${item}`),...(state.architecture?.risks || []).map((item)=>`Risk: ${item}`),...(state.architecture?.assumptions || []).map((item)=>`Assumption: ${item}`)];
    const projectDecisionContent=projectDecisionItems.length
      ? `<section class="inspector-responsibility"><h4>Project-level context</h4><p>These accepted items are not linked specifically to this relationship by the current Architecture contract.</p></section><div class="bullet-list"><ul>${projectDecisionItems.map((item)=>`<li>${escapeHtml(item)}</li>`).join('')}</ul></div>`
      : '<p class="muted">No project-level accepted decisions, risks, or assumptions are recorded.</p>';
    const overview=`<section class="inspector-responsibility"><h4>Canonical relationship</h4><p><strong>${escapeHtml(source?.label || selectedEdge.source)}</strong> → <strong>${escapeHtml(target?.label || selectedEdge.target)}</strong></p><p>${escapeHtml(selectedEdge.supporting_text || selectedEdge.semantic_type || selectedEdge.label || 'Accepted dependency')}</p></section><dl class="inspector-facts"><div><dt>Type</dt><dd>${escapeHtml(selectedEdge.semantic_type || 'relationship')}</dd></div><div><dt>Category</dt><dd>${escapeHtml(relationshipCategory)}</dd></div><div><dt>Direction</dt><dd>${escapeHtml(source?.label || selectedEdge.source)} → ${escapeHtml(target?.label || selectedEdge.target)}</dd></div><div><dt>Diagram ID</dt><dd><code>${escapeHtml(selectedEdge.id)}</code></dd></div><div><dt>Canonical source IDs</dt><dd>${escapeHtml(canonicalRelationshipIds.join(', ') || 'None')}</dd></div><div><dt>Status</dt><dd>Accepted · v${diagram.architectureVersion}</dd></div></dl>`;
    const dependencies=`<div class="inspector-relationship-endpoints"><button type="button" data-inspect-component="${escapeHtml(source?.component_id || '')}">← ${escapeHtml(source?.label || selectedEdge.source)}</button><span>${escapeHtml(selectedEdge.semantic_type || 'relationship')}</span><button type="button" data-inspect-component="${escapeHtml(target?.component_id || '')}">${escapeHtml(target?.label || selectedEdge.target)} →</button></div>`;
    const evidence=provenance.length ? `<div class="inspector-provenance">${provenance.map((item)=>`<span>${escapeHtml(item.relationship_id || selectedEdge.id)} · ${escapeHtml(item.semantic_type || selectedEdge.semantic_type || '')} · accepted v${escapeHtml(String(item.architecture_version ?? diagram.architectureVersion))}</span>`).join('')}</div>` : '<p class="muted">No additional relationship provenance is attached.</p>';
    const content=tab==='dependencies' ? dependencies : tab==='evidence' ? evidence : tab==='tasks' ? '<p class="muted">Tasks are attached to architecture components, not directly to this relationship.</p>' : tab==='code' ? '<p class="muted">Use the endpoint components to inspect revision-pinned Code Truth for this relationship.</p>' : tab==='decisions' ? projectDecisionContent : overview;
    $('selectedNode').innerHTML=`<small>SELECTED RELATIONSHIP</small><div class="selected-node-title"><h3>${escapeHtml(source?.label || selectedEdge.source)} → ${escapeHtml(target?.label || selectedEdge.target)}</h3><span class="health-pill health-planned">ACCEPTED</span></div><p>${escapeHtml(selectedEdge.semantic_type || selectedEdge.label || 'Canonical relationship')}</p><div class="graph-focus-controls"><button type="button" data-clear-edge>Clear</button></div>`;
    $('nodeEvidence').innerHTML=`${directionPicker}${architectureInspectorTabsMarkup()}<div class="architecture-inspector-content">${content}</div>`;
    wireArchitectureInspectorTabs();
    $('nodeEvidence').querySelectorAll('[data-inspect-component]').forEach((button)=>button.addEventListener('click',()=>{
      const node=diagram.nodes.find((item)=>item.component_id===button.dataset.inspectComponent);
      if(node) activateGraphNode(node);
    }));
    $('selectedNode').querySelector('[data-clear-edge]')?.addEventListener('click',()=>{state.selectedEdgeId=null;state.inspectorTab='overview';renderGraph();});
    return;
  }
  if (!c || !diagram) {
    if (diagram?.fullCanvas) {
      const view=activeCanvasReadingView(diagram);
      const path=(view?.nodeIds || []).map((id)=>diagramNodeById(id)?.label || id);
      $('selectedNode').innerHTML=`<small>SYSTEM ARCHITECTURE · v${diagram.architectureVersion}</small><h3>${escapeHtml(view?.label || 'All relationships')}</h3><p>Select a component to see its incoming and outgoing relationships, or select a line to inspect its direction and purpose.</p>`;
      $('nodeEvidence').innerHTML=path.length
        ? `<h4>Journey order</h4><ol class="canvas-journey-steps">${path.map((name)=>`<li>${escapeHtml(name)}</li>`).join('')}</ol><p class="muted">Authored architecture journey; this is not a runtime trace.</p>`
        : '<p class="muted">The overview shows the project backbone. All relationships remain available through selection or the All relationships view.</p>';
      return;
    }
    const attention=(diagram?.nodes || []).filter((node)=>diagramNodeHealth(node).needsAttention), direct=diagram?.scope?.directRelationships || [];
    $('selectedNode').innerHTML=attention.length ? `<small>CURRENT SCOPE · ${escapeHtml(state.readingMode)}</small><h3>${attention.length} node${attention.length===1?'':'s'} need attention</h3><p>Select a node to inspect exact projected facts. Non-leaf primary nodes open their canonical backend scope.</p>` : `<small>CURRENT SCOPE · ${escapeHtml(state.readingMode)}</small><h3>${escapeHtml(diagram?.scope?.label || 'Overview')}</h3><p>Select a node to inspect it. Upstream/downstream focus follows only the currently returned projected edges.</p>`;
    $('nodeEvidence').innerHTML=state.diagramError ? `<p><strong>Diagram unavailable</strong></p><p class="muted">${escapeHtml(state.diagramError)}</p>` : direct.length ? `<p><strong>${direct.length} direct scope relationship${direct.length===1?'':'s'}</strong></p><p class="muted">These authored relationships touch this scope directly and are not converted into fake child edges.</p>` : '<p><strong>Canonical projection</strong></p><p class="muted">Backend owns scope, topology, geometry, and routes. MAP/READ/FULL only changes disclosure.</p>';
    return;
  }
  const health=diagramNodeHealth(c), incoming=diagram.edges.filter((edge)=>edge.target===c.id), outgoing=diagram.edges.filter((edge)=>edge.source===c.id), linkedTasks=state.tasks.filter((task)=>task.related_component===c.component_id);
  const boundaryRelationships=canonicalBoundaryRelationshipsForComponent(c.component_id,diagram);
  const connectionLine=(edge,direction)=>{
    const peerId=direction==='in'?edge.source:edge.target, peer=diagramNodeById(peerId);
    const domain=diagram.fullCanvas ? canvasDomainForNode(peer,diagram) : null;
    const domainBadge=domain ? `<span class="canvas-connection-domain" data-peer-domain="${escapeHtml(domain.id)}" data-domain-tone="${domain.tone}"><i aria-hidden="true"></i>${escapeHtml(domain.label)}</span>` : '';
    return `<li><button type="button" class="canvas-connection-button" data-domain-tone="${domain?.tone ?? ''}" data-related-edge="${escapeHtml(edge.id)}"><span><strong>${direction==='in'?'← From':'→ To'} ${escapeHtml(peer?.label || peerId)}</strong><span>${escapeHtml(edge.label || edge.semantic_type || 'relationship')}</span>${domainBadge}</span></button></li>`;
  };
  const openScope = graphNodeAction(c) === 'drill'
    ? `<button type="button" data-open-selected-scope class="graph-open-scope">${ARCHITECTURE_CANVAS_MODE ? 'Focus scope' : 'Open scope'} →</button>`
    : '';
  const collapsed=Boolean(state.collapsedNodeIds.has(c.id));
  const collapseControl=ARCHITECTURE_CANVAS_MODE && c.childCount>0
    ? `<button type="button" data-toggle-collapse="${escapeHtml(c.id)}" class="${collapsed?'active':''}" aria-pressed="${collapsed}">${collapsed?'Expand':'Collapse'}</button>`
    : '';
  const focusModes=[['connected','Connected'],['upstream','Upstream'],['downstream','Downstream'],['hierarchy','Focus'],['isolate','Isolate'],['all','All']];
  const controls=`<div class="graph-focus-controls" role="group" aria-label="Graph focus">${focusModes.map(([mode,label])=>`<button type="button" data-graph-focus="${mode}" class="${state.graphFocusMode===mode?'active':''}">${label}</button>`).join('')}<button type="button" data-graph-focus="clear">Clear</button>${collapseControl}${openScope}</div>`;
  const traceControls=architectureTraceControlsMarkup(c,diagram);
  const provenance=[...incoming,...outgoing].flatMap((edge)=>(edge.provenance || []).map((item)=>({edge,item})));
  const currentScopeLabel=diagram.fullCanvas ? 'Full system' : (diagram.scope?.label || 'Overview');
  $('selectedNode').innerHTML=`<small>SELECTED COMPONENT · ${escapeHtml(String(c.semantic_kind))}</small><div class="selected-node-title"><h3>${escapeHtml(c.label)}</h3><span class="health-pill health-${health.key}">${escapeHtml(health.label)}</span></div><p>${escapeHtml(c.responsibility)}</p>${controls}<div class="component-connections"><strong>${incoming.length} incoming · ${outgoing.length} outgoing</strong>${diagram.fullCanvas?'<span class="connection-domain-key">Colors identify connected boundaries. Arrows show direction.</span>':''}${incoming.length||outgoing.length ? `<ul>${incoming.map((edge)=>connectionLine(edge,'in')).join('')}${outgoing.map((edge)=>connectionLine(edge,'out')).join('')}</ul>` : '<p class="muted">No direct canonical relationships.</p>'}</div>${traceControls}`;
  const provenanceMarkup=provenance.length ? `<div class="inspector-technical-block"><h4>Relationship provenance</h4><div class="inspector-provenance">${provenance.map(({edge,item})=>`<span>${escapeHtml(edge.id)} ← ${escapeHtml(item.relationship_id || 'canonical relationship')} · ${escapeHtml(item.semantic_type || edge.semantic_type || '')}</span>`).join('')}</div></div>` : '';
  const inspectorTasks=linkedTasks.map((task)=>({status:task.status,title:task.title}));
  const supportingText=(c.supporting_text || []).map((text)=>String(text || '').trim()).filter(Boolean);
  const pendingChanges=supportingText.filter((text)=>/^Pending change:\s*/i.test(text));
  const nodeEvidence=supportingText.filter((text)=>!/^Task\s+[A-Z_]+:\s*/i.test(text) && !/^Pending change:\s*/i.test(text));
  const inspectorTaskMarkup=inspectorTasks.length
    ? `<div class="inspector-task-list">${inspectorTasks.map((task)=>`<div class="inspector-task-row"><i class="status-dot ${statusClass(task.status)}" aria-hidden="true"></i><span>${escapeHtml(task.title)}</span></div>`).join('')}</div>`
    : `<p class="inspector-status-detail">${escapeHtml(c.status?.canonical_status || 'No linked execution task.')}</p>`;
  const projectedDependencyMarkup=incoming.length||outgoing.length ? `<section class="component-connections"><h4>Shown in this projection</h4><ul>${incoming.map((edge)=>connectionLine(edge,'in')).join('')}${outgoing.map((edge)=>connectionLine(edge,'out')).join('')}</ul></section>` : '<section><h4>Shown in this projection</h4><p class="muted">No relationships are shown for this component at the current scope/detail.</p></section>';
  const canonicalBoundaryMarkup=boundaryRelationships.length ? `<section class="component-connections canonical-boundary-connections"><h4>Canonical cross-boundary</h4><ul>${boundaryRelationships.map((relationship)=>canonicalBoundaryRelationshipMarkup(relationship,c.component_id)).join('')}</ul></section>` : '<section><h4>Canonical cross-boundary</h4><p class="muted">No additional cross-boundary relationships are declared for this component subtree by the current scope contract.</p></section>';
  const dependencyContent=`${projectedDependencyMarkup}${canonicalBoundaryMarkup}`;
  const tasksContent=linkedTasks.length ? `<div class="inspector-task-list">${[...linkedTasks].sort((a,b)=>({BLOCKED:0,IN_PROGRESS:1,TODO:2,DONE:3}[a.status]??4)-({BLOCKED:0,IN_PROGRESS:1,TODO:2,DONE:3}[b.status]??4)).map((task)=>`<div class="inspector-task-row"><i class="status-dot ${statusClass(task.status)}" aria-hidden="true"></i><span>${escapeHtml(task.title)} · ${escapeHtml(task.status.replace('_',' '))}</span></div>`).join('')}</div>` : '<p class="muted">No execution task is linked to this component.</p>';
  const evidenceContent=`${nodeEvidence.length ? `<div class="inspector-evidence-list">${nodeEvidence.map((text)=>`<p>${escapeHtml(text)}</p>`).join('')}</div>` : '<p class="muted">No node-linked observation or external evidence is present in this Diagram projection.</p>'}${provenanceMarkup}`;
  const matchingCodeNodes=(state.codeDiagram?.nodes || []).filter((node)=>node.component_id===c.component_id);
  const codeContent=!state.codeDiagram
    ? '<p class="muted">No Code Truth snapshot yet.</p>'
    : matchingCodeNodes.length
      ? `<div class="inspector-code-links">${matchingCodeNodes.map((node)=>`<button type="button" data-open-code-node="${escapeHtml(node.id)}"><strong>${escapeHtml(node.label)}</strong><span>${escapeHtml(state.codeDiagram.repository.slug)}@${escapeHtml(state.codeDiagram.repository.revision.slice(0,12))} · ${node.sources?.length || 0} pinned source${node.sources?.length===1?'':'s'}</span></button>`).join('')}</div>`
      : `<p class="muted">The current revision-pinned Code Truth snapshot has no implementation component linked to <strong>${escapeHtml(c.label)}</strong>.</p>`;
  const projectDecisionItems=[...(state.architecture?.decisions || []).map((item)=>`Decision: ${item}`),...(state.architecture?.risks || []).map((item)=>`Risk: ${item}`),...(state.architecture?.assumptions || []).map((item)=>`Assumption: ${item}`)];
  const pendingDecisionContent=pendingChanges.length ? `<section class="inspector-responsibility"><h4>Pending proposals affecting this component</h4><div class="bullet-list"><ul>${pendingChanges.map((item)=>`<li>${escapeHtml(item.replace(/^Pending change:\s*/i,''))}</li>`).join('')}</ul></div></section>` : '';
  const projectDecisionContent=projectDecisionItems.length ? `<section class="inspector-responsibility"><h4>Project-level context</h4><p>These accepted items are not component-linked by the current Architecture contract.</p></section><div class="bullet-list"><ul>${projectDecisionItems.map((item)=>`<li>${escapeHtml(item)}</li>`).join('')}</ul></div>` : '';
  const decisionsContent=pendingDecisionContent || projectDecisionContent ? `${pendingDecisionContent}${projectDecisionContent}` : '<p class="muted">No component-affecting proposal or project-level accepted decision, risk, or assumption is recorded.</p>';
  const hierarchyLabels=(c.hierarchyPath?.length ? c.hierarchyPath : [c.id]).map((nodeId)=>diagramNodeById(nodeId)?.label || String(nodeId).replace(/^node:/,''));
  const parentNode=c.parent_id ? diagramNodeById(c.parent_id) : null;
  const childNodes=diagram.nodes.filter((node)=>node.parent_id===c.id).sort((a,b)=>a.order-b.order || a.id.localeCompare(b.id));
  const overviewContent=`<div class="inspector-summary"><span class="inspector-status-label">${escapeHtml(health.label)}</span>${inspectorTaskMarkup}</div><section class="inspector-responsibility"><h4>Accepted responsibility</h4><p>${escapeHtml(c.responsibility)}</p></section><dl class="inspector-facts inspector-map-facts"><div><dt>Role</dt><dd>${escapeHtml(c.projectionRole)}</dd></div><div><dt>Children</dt><dd>${c.childCount}</dd></div><div><dt>Architecture</dt><dd>v${diagram.architectureVersion}</dd></div></dl><div class="inspector-read"><dl class="inspector-facts"><div><dt>Canonical status</dt><dd>${escapeHtml(c.status?.canonical_status || 'UNKNOWN')}</dd></div><div><dt>Hierarchy path</dt><dd>${escapeHtml(hierarchyLabels.join(' / '))}</dd></div><div><dt>Parent</dt><dd>${escapeHtml(parentNode?.label || 'Top level')}</dd></div><div><dt>Direct children</dt><dd>${escapeHtml(childNodes.map((node)=>node.label).join(', ') || 'None')}</dd></div><div><dt>Current scope</dt><dd>${escapeHtml(currentScopeLabel)}</dd></div></dl></div><div class="inspector-full"><div class="inspector-divider"></div><dl class="inspector-facts inspector-technical-facts"><div><dt>Stable Diagram ID</dt><dd><code>${escapeHtml(c.id)}</code></dd></div><div><dt>Component ID</dt><dd><code>${escapeHtml(c.component_id)}</code></dd></div></dl></div>`;
  const tabContent={overview:overviewContent,dependencies:dependencyContent,tasks:tasksContent,evidence:evidenceContent,code:codeContent,decisions:decisionsContent}[state.inspectorTab] || overviewContent;
  $('nodeEvidence').innerHTML=`${architectureInspectorTabsMarkup()}<div class="architecture-inspector-content">${tabContent}</div>`;
  wireArchitectureInspectorTabs();
  $('nodeEvidence').querySelectorAll('[data-open-code-node]').forEach((button)=>button.addEventListener('click',()=>{state.architectureGraphKind='code';state.selectedCodeNodeId=button.dataset.openCodeNode;render();}));
  $('nodeEvidence').querySelectorAll('[data-reveal-component]').forEach((button)=>button.addEventListener('click',()=>{ void revealArchitectureComponent(button.dataset.revealComponent,{inspectorTab:'dependencies'}); }));
  $('selectedNode').querySelectorAll('[data-graph-focus]').forEach((button)=>button.addEventListener('click',()=>{ const mode=button.dataset.graphFocus; if(mode==='clear'){clearArchitectureTracePath({render:false});state.selectedComponentId=null;state.selectedEdgeId=null;state.graphFocusMode='all';state.inspectorTab='overview';syncArchitectureCanvasSelectionUrl();} else {clearArchitectureTracePath({render:false});state.graphFocusMode=mode;} renderGraph(); }));
  $('selectedNode').querySelector('[data-toggle-collapse]')?.addEventListener('click',()=>toggleGraphNodeCollapse(c.id));
  $('selectedNode').querySelector('[data-open-selected-scope]')?.addEventListener('click',()=>drillGraphNode(c));
  $('selectedNode').querySelector('[data-trace-path]')?.addEventListener('click',()=>{
    const target=$('selectedNode').querySelector('[data-trace-target]')?.value;
    if(target) requestArchitectureTracePath(c.id,target);
  });
  $('selectedNode').querySelector('[data-clear-trace]')?.addEventListener('click',()=>clearArchitectureTracePath());
  $('selectedNode').querySelector('[data-agent-explore]')?.addEventListener('click',()=>exploreSelectedAgentContext());
}

function renderLists() {
  const list = (items, empty) => items?.length ? `<ul>${items.map((x) => `<li>${escapeHtml(x)}</li>`).join('')}</ul>` : `<p class="muted graph-side-empty">${empty}</p>`;
  const architecture = state.architecture || {};
  $('decisionList').innerHTML = list(architecture.decisions, 'No recorded decisions.');
  $('riskList').innerHTML = list([...(architecture.risks || []), ...(architecture.assumptions || []).map((x) => `Assumption: ${x}`)], 'None recorded.');
}

function renderRecentActivity() {
  const el = $('recentActivity');
  if (!el) return;
  const events = [...(state.activity || [])].reverse().slice(0, 6);
  if (!events.length) {
    el.innerHTML = '<p class="muted">No observed project activity yet.</p>';
    return;
  }
  el.innerHTML = events.map((event) => {
    const source = event.payload?.external_source || event.source || 'SYSTEM';
    const summary = event.payload?.summary || event.payload?.message || event.payload?.note || event.type;
    const eventTitle = String(event.type || 'Project update')
      .replace(/[_-]+/g, ' ')
      .toLowerCase()
      .replace(/^\w/, (character) => character.toUpperCase());
    const detailPayload = event.payload && typeof event.payload === 'object'
      ? JSON.stringify(event.payload, null, 2)
      : String(event.payload || '');
    return `<article class="activity-row"><div class="activity-row-summary"><strong>${escapeHtml(eventTitle)}</strong><p>${escapeHtml(source)} · ${escapeHtml(goalExcerpt(summary, 96))}</p></div><details class="activity-details"><summary>View details <span aria-hidden="true">＋</span></summary><div class="activity-detail-body"><p>${escapeHtml(summary)}</p>${detailPayload ? `<pre>${escapeHtml(detailPayload)}</pre>` : ''}</div></details></article>`;
  }).join('');
}

function latestPlannerPhase(run = state.lastRun) {
  const phases = run?.provider_usage?.phases;
  if (!Array.isArray(phases)) return null;
  return [...phases].reverse().find((phase) => phase && phase.status !== 'COMPLETED') || null;
}

function friendlyAgentError(run = state.lastRun) {
  const admission = run?.provider_usage?.admission;
  if (admission?.status === 'TIMEOUT') {
    return 'Architecture generation is busy. No model request was sent; retry shortly.';
  }
  const phase = latestPlannerPhase(run);
  const provider = phase?.provider || {};
  const isInitialPlannerRun = run?.provider_usage?.schema === 'archbro.gemini_initial_planner_usage.v1';
  const recovery = run === state.lastRun && isInitialPlannerRun
    ? state.plannerRecovery
    : null;
  const retryable = provider.retryable_provider_error === true
    || recovery?.retryable_provider_error === true;
  const retryableGeneration = provider.retryable_generation === true
    || recovery?.retryable_generation === true;
  if (provider.known_validation_failure === true || recovery?.known_validation_failure === true) {
    return 'Gemini returned a complete response, but it did not satisfy the Architecture relationship contract. Archbro preserved all completed phases and will retry only reconciliation after explicit authorization when another paid call is required.';
  }
  const statusCode = provider.http_status_code ?? recovery?.http_status_code;
  const providerStatus = provider.provider_status ?? recovery?.provider_status;
  if (retryableGeneration) {
    const nextBudget = provider.next_max_output_tokens ?? recovery?.next_max_output_tokens;
    const budgetText = Number.isInteger(nextBudget)
      ? ` The next bounded budget is ${nextBudget.toLocaleString()} tokens.`
      : '';
    return `The unfinished architecture phase reached its current output budget. Archbro saved the phase and will resume it with a larger bounded budget; completed phases will not be regenerated.${budgetText}`;
  }
  if (retryable && (statusCode === 429 || providerStatus === 'RESOURCE_EXHAUSTED')) {
    return 'Gemini is temporarily at capacity. Archbro used bounded backoff and saved every completed architecture phase. Retry resumes only the unfinished phase.';
  }
  if (retryable) {
    return 'The model service temporarily rejected the unfinished architecture phase. Completed phases are saved, so retry resumes from the failure point.';
  }
  return run?.error || 'Agent run failed before state mutation.';
}

function renderLastRun() {
  const el = $('lastRun');
  if (!el) return;
  if (!state.lastRun) {
    el.innerHTML = '';
    return;
  }
  const ok = state.lastRun.result === 'SUCCESS';
  const actions = Array.isArray(state.lastRun.actions) ? state.lastRun.actions : [];
  const details = JSON.stringify(state.lastRun, null, 2);
  const displayError = ok ? null : friendlyAgentError(state.lastRun);
  const summary = ok
    ? (state.lastRun.summary || 'Latest agent result')
    : displayError;
  el.innerHTML = `<article class="activity-row activity-last-run"><div class="activity-row-summary"><strong>${escapeHtml(goalExcerpt(summary, 96))}</strong><p>${escapeHtml(state.lastRun.provider || 'Agent')} · Latest result</p></div><details class="activity-details"><summary>View result <span aria-hidden="true">＋</span></summary><div class="activity-detail-body"><p class="muted">${ok ? 'SUCCESS' : 'ERROR'} · ${actions.length} action${actions.length === 1 ? '' : 's'} · ${escapeHtml(state.lastRun.model || 'deterministic')}</p>${displayError ? `<p class="activity-error">${escapeHtml(displayError)}</p>` : ''}<pre>${escapeHtml(details)}</pre></div></details></article>`;
}

function renderAgentInline(value) {
  const source = String(value ?? '');
  const tokenPattern = /(`[^`\n]+`|\*\*[^*\n]+\*\*)/g;
  let cursor = 0;
  let html = '';
  for (const match of source.matchAll(tokenPattern)) {
    html += escapeHtml(source.slice(cursor, match.index));
    const token = match[0];
    if (token.startsWith('`')) html += `<code>${escapeHtml(token.slice(1, -1))}</code>`;
    else html += `<strong>${escapeHtml(token.slice(2, -2))}</strong>`;
    cursor = match.index + token.length;
  }
  return html + escapeHtml(source.slice(cursor));
}

function markdownTableCells(line) {
  const normalized = String(line).trim().replace(/^\|/, '').replace(/\|$/, '');
  return normalized.split('|').map((cell) => cell.trim());
}

function markdownTableDelimiter(line) {
  const cells = markdownTableCells(line);
  return cells.length > 1 && cells.every((cell) => /^:?-{3,}:?$/.test(cell));
}

function renderAgentRichText(value, depth = 0) {
  if (depth >= 16) return `<pre>${escapeHtml(String(value ?? ''))}</pre>`;
  const lines = String(value ?? '').replace(/\r\n?/g, '\n').split('\n');
  const output = [];
  let index = 0;
  const isSpecial = (line, next = '') => (
    /^\s*```/.test(line)
    || /^\s*#{1,4}\s+/.test(line)
    || /^\s*[-*+]\s+/.test(line)
    || /^\s*\d+[.)]\s+/.test(line)
    || (line.includes('|') && markdownTableDelimiter(next))
  );
  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) { index += 1; continue; }
    if (/^\s*```/.test(line)) {
      const language = line.trim().slice(3).trim();
      const code = [];
      index += 1;
      while (index < lines.length && !/^\s*```/.test(lines[index])) code.push(lines[index++]);
      if (index < lines.length) index += 1;
      output.push(`<pre class="agent-code"><code${language ? ` data-language="${escapeHtml(language)}"` : ''}>${escapeHtml(code.join('\n'))}</code></pre>`);
      continue;
    }
    if (line.includes('|') && index + 1 < lines.length && markdownTableDelimiter(lines[index + 1])) {
      const header = markdownTableCells(line);
      index += 2;
      const rows = [];
      while (index < lines.length && lines[index].includes('|') && lines[index].trim()) rows.push(markdownTableCells(lines[index++]));
      output.push(`<div class="agent-table-scroll"><table><thead><tr>${header.map((cell) => `<th>${renderAgentInline(cell)}</th>`).join('')}</tr></thead><tbody>${rows.map((row) => `<tr>${header.map((_, cellIndex) => `<td>${renderAgentInline(row[cellIndex] || '')}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`);
      continue;
    }
    const heading = line.match(/^\s*(#{1,4})\s+(.+)$/);
    if (heading) {
      const level = Math.min(4, heading[1].length + 1);
      output.push(`<h${level}>${renderAgentInline(heading[2])}</h${level}>`);
      index += 1;
      continue;
    }
    const listItem = line.match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/);
    if (listItem) {
      const indent = listItem[1].length;
      const ordered = /^\d/.test(listItem[2]);
      const tag = ordered ? 'ol' : 'ul';
      const items = [];
      while (index < lines.length) {
        const item = lines[index].match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/);
        if (!item || item[1].length !== indent || /^\d/.test(item[2]) !== ordered) break;
        const contentIndent = lines[index].indexOf(item[3], indent + item[2].length);
        index += 1;
        const continuation = [];
        while (index < lines.length) {
          const next = lines[index];
          if (!next.trim()) { continuation.push(''); index += 1; continue; }
          const nextIndent = next.match(/^\s*/)[0].length;
          if (nextIndent <= indent) break;
          continuation.push(next.slice(Math.min(nextIndent, contentIndent)));
          index += 1;
        }
        // Preserve the authored number even when paragraphs split the list.
        const value = ordered ? ` value="${Number.parseInt(item[2], 10)}"` : '';
        const nested = continuation.join('\n');
        items.push(`<li${value}>${renderAgentInline(item[3])}${nested.trim() ? renderAgentRichText(nested, depth + 1) : ''}</li>`);
      }
      output.push(`<${tag}>${items.join('')}</${tag}>`);
      continue;
    }
    const paragraph = [line.trim()];
    index += 1;
    while (
      index < lines.length
      && lines[index].trim()
      && !isSpecial(lines[index], lines[index + 1] || '')
    ) paragraph.push(lines[index++].trim());
    const evidence = /^(?:verified sources?|evidence|source references?)\s*:/i.test(paragraph[0]);
    output.push(`<p${evidence ? ' class="agent-evidence-reference"' : ''}>${paragraph.map(renderAgentInline).join('<br>')}</p>`);
  }
  return output.join('') || '<p>No response content.</p>';
}

function agentResponseText(run = state.lastRun) {
  if (!run) return '';
  if (run.result !== 'SUCCESS') return friendlyAgentError(run);
  const summary = run.summary || 'No response summary.';
  const evidence = Array.isArray(run.evaluation?.evidence)
    ? run.evaluation.evidence.map((item) => String(item || '').trim()).filter(Boolean)
    : [];
  if (!evidence.length) return summary;
  return `${summary}\n\nEvidence:\n${evidence.map((item) => `- ${item}`).join('\n')}`;
}

function syncInstructionTextareaRows() {
  const input = $('instruction');
  if (!input) return;
  // Measure actual wrapping: character counts are unreliable across fonts and widths.
  input.rows = 2;
  input.style.height = 'auto';
  if (!input.clientWidth) return;
  const style = getComputedStyle(input);
  const lineHeight = parseFloat(style.lineHeight) || 19.5;
  const padding = (parseFloat(style.paddingTop) || 0) + (parseFloat(style.paddingBottom) || 0);
  const visualLines = Math.ceil((input.scrollHeight - padding) / lineHeight);
  input.rows = Math.min(8, Math.max(2, visualLines));
  input.classList.toggle('instruction-scrollable', visualLines > 8);
}

function renderAgentConversationDialog() {
  const prompt = $('agentConversationPrompt');
  const response = $('agentConversationResponse');
  const meta = $('agentConversationResponseMeta');
  if (!prompt || !response || !meta) return;
  const draft = $('instruction')?.value || '';
  prompt.textContent = draft.trim() ? draft : (state.lastInstruction || 'No instruction yet.');
  if (!state.lastRun) {
    response.innerHTML = '<p>No Agent response yet.</p>';
    meta.textContent = '';
    return;
  }
  const ok = state.lastRun.result === 'SUCCESS';
  const responseText = agentResponseText(state.lastRun);
  response.innerHTML = renderAgentRichText(responseText);
  meta.textContent = `${ok ? 'SUCCESS' : 'ERROR'} · ${state.lastRun.provider || 'Agent'} · ${state.lastRun.model || 'unknown model'}`;
}

function openAgentConversation(trigger) {
  renderAgentConversationDialog();
  showDialog('agentConversationDialog', trigger || $('instruction'));
}

function renderGlobalAgentReply() {
  const reply = $('globalAgentReply');
  if (!reply) return;
  if (!state.lastRun) {
    reply.classList.add('hidden');
    reply.innerHTML = '';
    renderAgentConversationDialog();
    return;
  }
  const ok = state.lastRun.result === 'SUCCESS';
  reply.classList.remove('hidden');
  reply.classList.toggle('error', !ok);
  const responseText = agentResponseText(state.lastRun);
  reply.innerHTML = `<div class="global-agent-reply-head"><span>${ok ? 'AGENT RESPONSE' : 'AGENT ERROR'}</span><div><small>${escapeHtml(state.lastRun.provider)} · ${escapeHtml(state.lastRun.model)}</small><button class="link-btn" type="button" data-expand-agent-conversation>Expand</button></div></div><div class="agent-rich-text">${renderAgentRichText(responseText)}</div>`;
  renderAgentConversationDialog();
}

function selectedAgentContextNode() {
  if (
    state.currentView !== 'architecture'
    || state.architectureGraphKind !== 'living'
    || !state.selectedComponentId
    || !state.projectId
    || !state.architecture?.version
  ) return null;
  return findArchitectureNode(state.selectedComponentId);
}

function agentContextRequestForNode(node) {
  return {
    node_id: `node:${node.id}`,
    direction: 'both',
    expansion_policy: state.agentContextPolicy,
    expected_architecture_version: state.architecture.version,
  };
}

function agentContextExecutionRequest(manifest) {
  return {
    node_id: manifest.selection.node_id,
    direction: manifest.selection.direction,
    expansion_policy: manifest.selection.expansion_policy,
    expected_architecture_version: manifest.architecture_version,
    preview_manifest_hash: manifest.manifest_hash,
  };
}

async function exploreSelectedAgentContext() {
  const node = selectedAgentContextNode();
  if (!node) return false;
  const manifest = await ensureAgentContextManifest();
  if (!manifest) {
    toast('Explore from here needs the exact bounded context preview. Retry the Context Tray.', true);
    return false;
  }
  const context = currentInstructionContext();
  const result = await sendEvent('USER_MESSAGE', {
    message: `Explore from here: ${node.name}`,
    ui_context: context.payload,
    agent_context_request: agentContextExecutionRequest(manifest),
  });
  return result?.result === 'SUCCESS';
}

function agentContextPreviewKey(request) {
  return [
    state.projectId,
    request.expected_architecture_version,
    request.node_id,
    request.direction,
    request.expansion_policy,
  ].join('\u001f');
}

function clearAgentContextPreview({preservePolicy = true} = {}) {
  state.agentContextRequestSerial += 1;
  state.agentContextManifest = null;
  state.agentContextKey = null;
  state.agentContextLoading = false;
  state.agentContextError = null;
  state.agentContextPromise = null;
  if (!preservePolicy) state.agentContextPolicy = 'ASK_ALL';
  renderAgentContextTray();
}

function agentContextNames(items, limit = 6) {
  const values = (items || []).slice(0, limit).map((item) => (
    item?.name
    || item?.title
    || item?.summary
    || item?.symbol?.qualified_name
    || item?.component_id
    || item?.id
  )).filter(Boolean);
  const hidden = Math.max(0, (items || []).length - values.length);
  return `${values.join(', ') || 'None'}${hidden ? ` +${hidden} more` : ''}`;
}

function renderAgentContextTray() {
  const tray = $('agentContextTray');
  const body = $('agentContextTrayBody');
  const title = $('agentContextTrayTitle');
  const policy = $('agentContextPolicy');
  const telemetryToggle = $('agentContextTelemetryToggle');
  if (!tray || !body || !title || !policy || !telemetryToggle) return;

  const node = selectedAgentContextNode();
  tray.classList.toggle('hidden', !node);
  if (!node) {
    title.textContent = 'No architecture component selected';
    body.innerHTML = '';
    return;
  }

  title.textContent = node.name;
  policy.value = state.agentContextPolicy;
  telemetryToggle.setAttribute('aria-pressed', String(state.agentContextTelemetryVisible));
  telemetryToggle.textContent = state.agentContextTelemetryVisible ? 'Hide telemetry' : 'Show telemetry';

  if (state.agentContextLoading) {
    body.innerHTML = '<p class="agent-context-status">Building the exact server-owned preview…</p>';
    return;
  }
  if (state.agentContextError) {
    body.innerHTML = `<p class="agent-context-status error">${escapeHtml(state.agentContextError)}</p><button type="button" data-agent-context-retry>Retry preview</button>`;
    body.querySelector('[data-agent-context-retry]')?.addEventListener('click', () => ensureAgentContextManifest({force:true}));
    return;
  }

  const manifest = state.agentContextManifest;
  if (!manifest) {
    body.innerHTML = '<p class="agent-context-status">Preparing bounded context…</p>';
    return;
  }
  const sections = manifest.sections || {};
  const architecture = sections.architecture || {};
  const lineage = architecture.lineage || [];
  const children = architecture.children || [];
  const dependencies = architecture.dependency_context || {nodes:[], relationships:[], counts:{}};
  const tasks = sections.tasks || [];
  const evidence = sections.evidence || [];
  const codeTruth = sections.code_truth || {status:'NO_SNAPSHOT', chunks:[]};
  const mcpRefs = sections.mcp_refs || [];
  const usage = manifest.usage || {};
  const limitReasons = usage.limit_reasons || [];
  const latestContextTelemetry = state.lastRun?.context_telemetry || null;
  const providerUsage = state.lastRun?.provider_usage || null;
  const actualInputTokens = providerUsage?.input_tokens ?? providerUsage?.inputTokens;
  const providerActualInput = actualInputTokens == null
    ? (state.lastRun ? 'Unavailable' : 'Not run yet')
    : `${Number(actualInputTokens).toLocaleString()} tokens`;
  const parent = lineage.length > 1 ? lineage[lineage.length - 2]?.name : 'Top level';
  const telemetry = state.agentContextTelemetryVisible
    ? `<dl class="agent-context-telemetry"><div><dt>Preview size</dt><dd>${Number(usage.context_chars || 0).toLocaleString()} chars</dd></div><div><dt>Estimated input</dt><dd>${Number(usage.estimated_input_tokens || 0).toLocaleString()} tokens</dd></div><div><dt>Last provider actual</dt><dd>${providerActualInput}</dd></div><div><dt>Preview hash</dt><dd><code>${escapeHtml(String(manifest.manifest_hash || '').slice(0, 12))}</code></dd></div>${latestContextTelemetry ? `<div><dt>Last run hash</dt><dd><code>${escapeHtml(String(latestContextTelemetry.manifest_hash || '').slice(0, 12))}</code></dd></div>` : ''}</dl>`
    : '';
  body.innerHTML = `
    <div class="agent-context-boundary${usage.truncated ? ' bounded-warning' : ''}">
      <div><span>Selection</span><strong>${escapeHtml(architecture.origin?.name || node.name)}</strong></div>
      <div><span>Parent</span><strong>${escapeHtml(parent || 'Top level')}</strong></div>
      <div><span>Children</span><strong>${children.length}</strong><small>${escapeHtml(agentContextNames(children))}</small></div>
      <div><span>Dependency neighborhood</span><strong>${dependencies.counts?.nodes || 0} nodes · ${dependencies.counts?.relationships || 0} links</strong><small>${escapeHtml(agentContextNames(dependencies.nodes || []))}</small></div>
      <div><span>Linked tasks</span><strong>${tasks.length}</strong><small>${escapeHtml(agentContextNames(tasks))}</small></div>
      <div><span>Evidence</span><strong>${evidence.length}</strong><small>${escapeHtml(agentContextNames(evidence))}</small></div>
      <div><span>Code Truth</span><strong>${escapeHtml(codeTruth.status || 'NO_SNAPSHOT')}</strong><small>${escapeHtml(agentContextNames(codeTruth.chunks || []))}</small></div>
      <div><span>Connected MCP evidence</span><strong>${mcpRefs.length}</strong><small>${mcpRefs.length ? escapeHtml(agentContextNames(mcpRefs)) : 'Not included until explicitly gathered'}</small></div>
    </div>
    <div class="agent-context-policy-summary"><span>${escapeHtml(manifest.selection?.expansion_policy || state.agentContextPolicy)}</span><span>≤ ${manifest.selection?.effective_max_hops ?? 0} hops</span><span>Architecture v${manifest.architecture_version}</span>${usage.truncated ? `<span class="agent-context-limit">Limited · ${escapeHtml(limitReasons.join(', ') || 'budget')}</span>` : '<span>Within budget</span>'}</div>
    ${telemetry}`;
}

async function ensureAgentContextManifest({force = false} = {}) {
  const node = selectedAgentContextNode();
  if (!node) {
    clearAgentContextPreview();
    return null;
  }
  const request = agentContextRequestForNode(node);
  const key = agentContextPreviewKey(request);
  if (!force && state.agentContextKey === key && state.agentContextManifest) return state.agentContextManifest;
  if (!force && state.agentContextKey === key && state.agentContextPromise) return state.agentContextPromise;

  const serial = ++state.agentContextRequestSerial;
  state.agentContextKey = key;
  state.agentContextManifest = null;
  state.agentContextLoading = true;
  state.agentContextError = null;
  renderAgentContextTray();
  const pending = api(`/projects/${encodeURIComponent(state.projectId)}/agent-context/manifest`, {
    method: 'POST',
    body: JSON.stringify(request),
  });
  state.agentContextPromise = pending;
  try {
    const manifest = await pending;
    if (serial !== state.agentContextRequestSerial || key !== state.agentContextKey) return null;
    state.agentContextManifest = manifest;
    return manifest;
  } catch (err) {
    if (serial === state.agentContextRequestSerial && key === state.agentContextKey) {
      state.agentContextError = err?.message || String(err);
    }
    return null;
  } finally {
    if (serial === state.agentContextRequestSerial && key === state.agentContextKey) {
      state.agentContextLoading = false;
      state.agentContextPromise = null;
      renderAgentContextTray();
    }
  }
}

function syncAgentContextPreview() {
  const node = selectedAgentContextNode();
  if (!node) {
    if (state.agentContextKey || state.agentContextManifest || state.agentContextLoading || state.agentContextError) clearAgentContextPreview();
    else renderAgentContextTray();
    return;
  }
  const request = agentContextRequestForNode(node);
  const key = agentContextPreviewKey(request);
  if (state.agentContextKey !== key) {
    state.agentContextRequestSerial += 1;
    state.agentContextKey = key;
    state.agentContextManifest = null;
    state.agentContextLoading = false;
    state.agentContextError = null;
    state.agentContextPromise = null;
  }
  renderAgentContextTray();
  if (!state.agentContextManifest && !state.agentContextLoading && !state.agentContextError) {
    queueMicrotask(() => ensureAgentContextManifest());
  }
}

function currentInstructionContext() {
  const base = {
    view: state.currentView,
    project_id: state.projectId,
    project_name: state.project?.name || '',
  };

  const proposal = state.proposals.find((item) => (
    item.id === state.selectedProposalId && isProposalActionable(item, state.architecture)
  ));
  if (proposal && state.currentView === 'tasks' && state.workspaceTab === 'review') {
    return {
      label: `Proposal · ${proposal.reason}`,
      instruction: 'Ask about this architecture proposal or add review evidence',
      placeholder: 'Describe your decision, constraint, or evidence for this proposal.',
      payload: {...base, proposal_id: proposal.id, proposal_reason: proposal.reason, proposal_status: proposal.status},
    };
  }

  if (state.currentView === 'tasks' && state.workspaceTab === 'tasks') {
    const task = state.tasks.find((item) => item.id === state.selectedTaskId);
    return taskInstructionContext({view: state.currentView, projectId: state.projectId, projectName: state.project?.name || '', task});
  }

  if (state.taskDetailId) {
    const task = state.tasks.find((item) => item.id === state.taskDetailId);
    if (task) return taskInstructionContext({view: state.currentView, projectId: state.projectId, projectName: state.project?.name || '', task});
  }

  if (state.currentView === 'architecture') {
    if (state.architectureGraphKind === 'code') {
      const codeNode = codeNodeById(state.selectedCodeNodeId);
      const repository = state.codeDiagram?.repository;
      return {
        label: codeNode ? `Code · ${codeNode.label}` : repository ? `Code · ${repository.slug}@${repository.revision.slice(0, 8)}` : 'Code Architecture · no snapshot',
        instruction: codeNode ? 'Ask about this implementation component or its source evidence' : 'Ask the Agent to inspect GitHub and publish a revision-pinned Code Architecture snapshot',
        placeholder: codeNode ? `Example: What source evidence proves ${codeNode.label} belongs here?` : 'Inspect the connected GitHub repository at an exact commit and build the implementation architecture.',
        payload: {
          ...base,
          architecture_graph_kind: 'code',
          ...(repository ? {repository: repository.slug, revision: repository.revision} : {}),
          ...(codeNode ? {code_node_id: codeNode.id, code_component_id: codeNode.component_id, code_node_name: codeNode.label} : {}),
        },
      };
    }
    const node = findArchitectureNode(state.selectedComponentId || state.scopeComponentId);
    return {
      label: node ? `Architecture · ${node.name}` : `Architecture · v${state.architecture?.version || 0}`,
      instruction: node ? 'Ask about this architecture area or describe new evidence' : 'Ask about the accepted architecture or describe a mismatch',
      placeholder: node ? `Example: Can this be solved inside ${node.name} without changing the architecture?` : 'Describe an architecture concern, dependency change, or new requirement.',
      payload: {...base, ...(node ? {architecture_node_id: node.id, architecture_node_name: node.name, architecture_node_kind: node.kind || node.type} : {})},
    };
  }

  return {
    label: `Project · ${state.project?.name || 'Overview'}`,
    instruction: 'Ask the Agent, add a task, or describe a project change',
    placeholder: 'Describe what changed, what is blocked, or what you want the Agent to evaluate.',
    payload: base,
  };
}

function updateInstructionContext() {
  const context = currentInstructionContext();
  const chip = $('instructionContext');
  const label = $('instructionLabel');
  const input = $('instruction');
  if (!chip || !label || !input) return;
  chip.textContent = context.label;
  label.textContent = context.instruction;
  input.placeholder = context.placeholder;
  syncAgentContextPreview();
}

async function sendEvent(type, payload, workingDetail = '') {
  const projectId = state.projectId;
  if (!projectId) return null;
  const guard = captureNavigationGuard(projectId);
  if (!committedProjectGuardIsCurrent(guard)) return null;
  const boundedContextRequested = Boolean(payload?.agent_context_request);
  const workingRequestId = beginWorkingRequest(workingDetail, {projectId});
  try {
    const result = await api(`/projects/${projectId}/events`, {method: 'POST', body: JSON.stringify({type, source: 'FRONTEND', payload})});
    if (!committedProjectGuardIsCurrent(guard)) return result;
    state.lastRun = result;
    if (boundedContextRequested) clearAgentContextPreview();
    if (result.result === 'ERROR') toast(friendlyAgentError(result), true);
    else toast(result.architecture_review_required ? 'Agent created an architecture proposal for review.' : 'Project state updated.');
    await refresh({projectId, guard});
    return result;
  } catch (err) {
    if (!committedProjectGuardIsCurrent(guard)) return null;
    if (boundedContextRequested && /agent_context_preview_stale|stale_architecture_version/.test(String(err?.message || err))) {
      clearAgentContextPreview();
      syncAgentContextPreview();
    }
    toast(err.message, true);
    return null;
  } finally {
    finishWorkingRequest(workingRequestId);
  }
}

function setArchitectureProgress(working, startedAt = 0) {
  const wrap = $('architectureProgress');
  const button = $('generateArchitectureBtn');
  if (!wrap || !button) return;
  wrap.classList.toggle('hidden', !working);
  button.disabled = working;
  button.textContent = working ? 'Generating architecture...' : initialArchitectureActionState().label;
  clearInterval(setArchitectureProgress.timer);
  if (!working) {
    syncInitialArchitectureAction();
    return;
  }

  const update = () => {
    const elapsed = Math.max(0, Math.floor((Date.now() - startedAt) / 1000));
    $('architectureElapsed').textContent = `${elapsed}s`;
    if (elapsed < 8) {
      $('architectureProgressText').textContent = 'Reading Goal and shaping the V0 skeleton';
      $('architectureProgressHint').textContent = `${architectureModelDisplayName()} is reasoning over the confirmed Goal.`;
    } else if (elapsed < 30) {
      $('architectureProgressText').textContent = 'Reasoning within the configured model deadline';
      $('architectureProgressHint').textContent = 'Explicit transient provider rejections use bounded backoff. Ambiguous timeouts are never replayed automatically.';
    } else {
      $('architectureProgressText').textContent = 'Validating the architecture result';
      $('architectureProgressHint').textContent = 'Components, relationships, and tasks must satisfy the machine-readable Architecture contract.';
    }
  };
  update();
  setArchitectureProgress.timer = setInterval(update, 1000);
}

function architectureModelDisplayName(modelId = RUNTIME_CONFIG.architecture_model) {
  const normalized = String(modelId || '').trim();
  if (!normalized) return 'The architecture model';
  return normalized
    .split('-')
    .map((part, index) => (
      index === 0 && part.toLowerCase() === 'gemini'
        ? 'Gemini'
        : (/^\d+(?:\.\d+)?$/.test(part) ? part : `${part.slice(0, 1).toUpperCase()}${part.slice(1)}`)
    ))
    .join(' ');
}

function initialArchitectureActionState(recovery = state.plannerRecovery) {
  switch (recovery?.action) {
    case 'START_NEW_PLAN':
      return {
        label: 'Resume architecture generation',
        hint: 'Gemini explicitly rejected the unfinished phase before returning a result. The updated planner starts safely from the saved Goal without treating it as an unknown paid outcome.',
      };
    case 'AUTHORIZE_NEW_ATTEMPT':
      if (recovery?.known_validation_failure) {
        return {
          label: 'Authorize another reconciliation attempt',
          hint: 'The previous response completed but failed deterministic Architecture validation. This authorizes one new paid reconciliation attempt; completed topology phases will not be regenerated.',
        };
      }
      return {
        label: 'Authorize new architecture attempt',
        hint: 'The previous model request has an unknown outcome. This explicit action authorizes one new paid attempt; Archbro will never replay it automatically.',
      };
    case 'REPROCESS_RESPONSE':
      return {
        label: 'Recover saved architecture response',
        hint: 'A complete provider response was saved. Recovery reprocesses it locally without another model call.',
      };
    case 'RECLAIM_PREPARED':
      return {
        label: 'Resume architecture generation',
        hint: 'The previous attempt stopped before provider dispatch, so it can be reclaimed without duplicating a paid request.',
      };
    case 'RETRY_EVENT':
      if (recovery?.retryable_generation) {
        return {
          label: 'Resume architecture generation',
          hint: 'The unfinished phase reached its current output budget. Archbro will retry only that phase with the saved larger bounded budget.',
        };
      }
      if (recovery?.retryable_provider_error) {
        return {
          label: 'Resume architecture generation',
          hint: 'The provider temporarily rejected the unfinished phase. Completed phases are checkpointed, so retry resumes only from the failure point.',
        };
      }
      return {
        label: 'Retry initial architecture',
        hint: 'The durable planner state is ready for a fenced retry using the saved Goal.',
      };
    default:
      if (recovery) {
        return {
          label: 'Manual recovery required',
          hint: 'Archbro cannot safely infer the next planner action. No model request will be sent from this button.',
          disabled: true,
        };
      }
      return {
        label: 'Retry initial architecture',
        hint: 'Retry uses the already saved Goal as the source of truth.',
      };
  }
}

function syncInitialArchitectureAction() {
  const button = $('generateArchitectureBtn');
  const hint = $('architectureRecoveryHint');
  if (!button || !hint) return;
  const action = initialArchitectureActionState();
  const working = Boolean(state.architectureProgressRequestId);
  if (!working) button.textContent = action.label;
  button.disabled = working || action.disabled === true;
  hint.textContent = action.hint;
}

async function recoverInitialArchitectureCheckpoint(projectId, recovery, {signal} = {}) {
  if (!recovery || ['RETRY_EVENT', 'START_NEW_PLAN'].includes(recovery.action)) return null;
  if (!recovery.action) throw new Error('This planner checkpoint requires manual recovery.');
  const requestId = `architecture-recovery:${recovery.attempt_id}:${recovery.revision}`;
  const checkpoint = await api(
    `/projects/${encodeURIComponent(projectId)}/planner/checkpoints/${encodeURIComponent(recovery.plan_id)}/${encodeURIComponent(recovery.phase_key)}/recover`,
    {
      method: 'POST',
      signal,
      body: JSON.stringify({
        expected_attempt_id: recovery.attempt_id,
        expected_revision: recovery.revision,
        action: recovery.action,
        request_id: requestId,
      }),
    },
  );
  state.plannerRecovery = {
    ...recovery,
    revision: checkpoint.revision,
    status: checkpoint.status,
    action: 'RETRY_EVENT',
    requires_paid_call_confirmation: false,
  };
  return checkpoint;
}

async function generateInitialArchitecture() {
  if (WEBMCP_AGENT_MODE) {
    toast('Built-in architecture generation is disabled in WebMCP Agent Mode.', true);
    return null;
  }
  const projectId = state.projectId;
  if (!projectId || state.architecture?.version > 0) return null;
  const guard = captureNavigationGuard(projectId);
  if (!committedProjectGuardIsCurrent(guard)) return null;
  const startedAt = Date.now();
  const workingRequestId = beginWorkingRequest('', {projectId, architectureStartedAt:startedAt});
  const controller = new AbortController();
  const clientTimeout = setTimeout(() => controller.abort(), ARCHITECTURE_REQUEST_TIMEOUT_MS);
  try {
    await recoverInitialArchitectureCheckpoint(projectId, state.plannerRecovery, {signal: controller.signal});
    if (!committedProjectGuardIsCurrent(guard)) return null;
    const result = await api(`/projects/${projectId}/events`, {
      method: 'POST',
      signal: controller.signal,
      body: JSON.stringify({type: 'USER_MESSAGE', source: 'FRONTEND', payload: {intent: 'INITIAL_ARCHITECTURE'}}),
    });
    if (!committedProjectGuardIsCurrent(guard)) return result;
    state.lastRun = result;
    if (result.result === 'SUCCESS') {
      toast('Architecture v1 and initial tasks created from the confirmed Goal.');
    } else {
      toast(friendlyAgentError(result), true);
    }
    await refresh({projectId, guard});
    return result;
  } catch (err) {
    const message = err?.name === 'AbortError'
      ? 'Architecture generation reached the client deadline. The saved Goal is safe; retry once the backend is available.'
      : err.message;
    if (committedProjectGuardIsCurrent(guard)) {
      toast(message, true);
      await refresh({projectId, guard});
    }
    return null;
  } finally {
    clearTimeout(clientTimeout);
    finishWorkingRequest(workingRequestId);
  }
}

function switchView(name, {
  historyMode = 'push',
  navigationGuard = null,
  workspaceTab = null,
  restoreScroll = true,
  focusedProposalId = null,
} = {}) {
  if (state.onboarding.active) return false;
  if (!views[name]) return false;
  // Switching ordinary panels does not change the graph projection identity.
  const invalidateGraph = false;
  const guard = navigationGuard || beginNavigationTransition(state.projectId, {invalidateGraph});
  if (!navigationGenerationIsCurrent(guard)) return false;
  if (name === 'tasks') {
    const nextWorkspaceTab = workspaceTabNames.includes(workspaceTab) ? workspaceTab : 'tasks';
    applyWorkspaceTabInvariants(nextWorkspaceTab, {focusedProposalId});
    state.selectedComponentId = null;
  } else if (name === 'architecture') {
    state.taskDetailId = null;
    state.taskDetailOrigin = null;
    state.selectedProposalId = null;
    state.selectedTaskId = null;
  }
  state.currentView = name;
  if (!commitNavigation({
    projectId:state.projectId,
    view:name,
    canvas:ARCHITECTURE_CANVAS_MODE,
    nodeId:name === 'architecture' ? state.selectedComponentId : null,
    inspectorTab:name === 'architecture' ? state.inspectorTab : 'overview',
    workspaceTab:name === 'tasks' ? state.workspaceTab : 'tasks',
  }, {historyMode, guard})) return false;
  document.querySelectorAll('.view').forEach((v) => v.classList.remove('active'));
  $(`view-${name}`).classList.add('active');
  $('pageTitle').textContent = views[name].title;
  $('pageSubtitle').textContent = views[name].subtitle;
  renderProjectTree();
  if (name === 'architecture') renderTaskDetails();
  if (name === 'architecture') renderGraph();
  if (name === 'tasks') {
    renderTasks();
    renderProposals();
  }
  renderWorkspaceTabs();
  updateInstructionContext();
  const workspaceMain = $('workspaceMain');
  if (workspaceMain) workspaceMain.scrollTop = 0;
  window.scrollTo(0, 0);
  if (name === 'tasks' && restoreScroll) restoreWorkspaceTabScroll(state.workspaceTab);
  syncDocumentTitle();
  return true;
}

function goTargetOptions(element, viewName) {
  const workspaceTab = element?.dataset?.workspaceTabTarget;
  if (viewName === 'tasks' && workspaceTabNames.includes(workspaceTab)) return {workspaceTab};
  return {};
}

function wireGoButtons() {
  document.querySelectorAll('[data-go]').forEach((button) => {
    button.onclick = () => switchView(button.dataset.go, goTargetOptions(button, button.dataset.go));
  });
  document.querySelectorAll('[data-go-card]').forEach((card) => {
    const open = () => switchView(card.dataset.goCard, goTargetOptions(card, card.dataset.goCard));
    card.onclick = (event) => {
      if (event.target.closest('button,a,input,textarea,select')) return;
      open();
    };
    card.onkeydown = (event) => {
      if (event.key !== 'Enter' && event.key !== ' ') return;
      event.preventDefault();
      open();
    };
  });
}

function syncMcpTransportFields() {
  const isHttp = $('mcpTransport').value === 'streamable_http';
  $('mcpHttpFields').classList.toggle('hidden', !isHttp);
  $('mcpStdioFields').classList.toggle('hidden', isHttp);
}

function mcpOAuthProviderId(preset = $('mcpPreset').value) {
  if (preset === 'github-remote') return 'github';
  if (preset === 'slack') return 'slack';
  if (preset === 'google-drive') return 'google-drive';
  if (preset === 'microsoft-teams') return 'microsoft-teams';
  return null;
}

function mcpPresetForProvider(providerId) {
  if (providerId === 'github') return 'github-remote';
  if (providerId === 'slack') return 'slack';
  if (providerId === 'google-drive') return 'google-drive';
  if (providerId === 'microsoft-teams') return 'microsoft-teams';
  return null;
}

function captureMcpAccountUi() {
  return {generation:mcpUiGeneration, userId:prototype.currentProfile(localStorage)?.id || null};
}

function mcpAccountUiIsCurrent(ticket) {
  return ticket.generation === mcpUiGeneration
    && ticket.userId === (prototype.currentProfile(localStorage)?.id || null);
}

function resetMcpAccountUi() {
  mcpUiGeneration += 1;
  mcpConnectionsRequestSerial += 1;
  mcpConnectionsSnapshot = [];
  for (const entry of mcpProviderStatusCache.values()) entry.generation += 1;
  mcpProviderStatusCache.clear();
  if ($('mcpConnectionList')) $('mcpConnectionList').replaceChildren();
  if ($('mcpConnectedCount')) $('mcpConnectedCount').textContent = '0';
  const notice = $('mcpConnectionNotice');
  if (notice) { notice.textContent = ''; delete notice.dataset.provider; notice.classList.add('hidden'); }
  syncMcpProviderCards();
}

function mcpConnectionForProvider(providerId, connections = mcpConnectionsSnapshot) {
  return (connections || []).find((connection) => (
    connection.provider === providerId && !connection.authorization_pending
  )) || null;
}

function syncMcpProviderCards(connections = mcpConnectionsSnapshot) {
  document.querySelectorAll('[data-mcp-preset]').forEach((card) => {
    const providerId = mcpOAuthProviderId(card.dataset.mcpPreset);
    const connection = providerId ? mcpConnectionForProvider(providerId, connections) : null;
    const connected = Boolean(connection);
    card.classList.toggle('connected', connected);
    card.dataset.connected = String(connected);
    const chevron = card.querySelector('.mcp-provider-chevron');
    if (chevron) {
      chevron.textContent = connected ? '✓' : '›';
      chevron.classList.toggle('connected', connected);
      chevron.setAttribute('aria-label', connected ? 'Connected' : 'Open setup');
    }
  });
}

function announceMcpConnection(providerId, connections = mcpConnectionsSnapshot, message = '') {
  const connection = mcpConnectionForProvider(providerId, connections);
  const notice = $('mcpConnectionNotice');
  if (!notice || !connection) return connection;
  const persistence = connection.persistent ? 'Saved securely and available after deployment updates.' : 'Connected for this server session.';
  notice.dataset.provider = providerId;
  notice.textContent = message || `${connection.name} connected. ${persistence}`;
  notice.classList.remove('hidden');
  return connection;
}

async function reconcileMcpProviderConnection(
  providerId,
  {message = '', openConnected = true} = {},
) {
  const ticket = captureMcpAccountUi();
  const preset = mcpPresetForProvider(providerId);
  if (providerId) invalidateMcpProviderStatus(providerId);
  const statusRequest = providerId
    ? requestMcpProviderStatus(providerId, {force: true})
    : Promise.resolve(null);
  const [status, connections] = await Promise.all([
    statusRequest,
    loadMcpConnections(),
  ]);
  if (!mcpAccountUiIsCurrent(ticket)) {
    return {connected: false, status: null, connections: [], connection: null};
  }
  if (preset && status) renderMcpOAuthStatus(preset, status);
  syncMcpProviderCards(connections);
  const connection = providerId
    ? mcpConnectionForProvider(providerId, connections)
    : null;
  const connected = Boolean(connection || status?.connected === true);
  if (providerId && connection && message) {
    announceMcpConnection(providerId, connections, message);
  }
  if (connected && openConnected) setMcpPickerTab('connected');
  window.dispatchEvent(new CustomEvent('archbro:mcp-connections-changed', {
    detail: {provider: providerId, connected, connection, status},
  }));
  return {connected, status, connections, connection};
}

async function completeMcpConnectionUi(providerId, message = '') {
  const result = await reconcileMcpProviderConnection(providerId, {message});
  return result.connections;
}

function mcpProviderStatusEntry(providerId) {
  let entry = mcpProviderStatusCache.get(providerId);
  if (!entry) {
    entry = {value: null, fetchedAt: 0, inFlight: null, generation: 0};
    mcpProviderStatusCache.set(providerId, entry);
  }
  return entry;
}

function mcpProviderStatusEndpoint(providerId) {
  return `/mcp/oauth/${encodeURIComponent(providerId)}/status`;
}

function mcpLegacyProviderStatusEndpoint(providerId) {
  if (providerId === 'github') return '/mcp/auth/github/status';
  if (providerId === 'google-drive') return '/mcp/auth/google-drive/status';
  return null;
}

async function resolveMcpProviderStatus(providerId) {
  const generic = await api(mcpProviderStatusEndpoint(providerId));
  if (
    generic?.configured === true
    || generic?.connected === true
    || Boolean(generic?.restore_error)
    || generic?.legacy_fallback_allowed !== true
  ) {
    return {...generic, oauth_strategy: 'generic'};
  }

  const legacyEndpoint = mcpLegacyProviderStatusEndpoint(providerId);
  if (!legacyEndpoint) return {...generic, oauth_strategy: 'generic'};

  try {
    const legacy = await api(legacyEndpoint);
    if (legacy?.configured === true || legacy?.connected === true) {
      return {...legacy, oauth_strategy: 'legacy-runtime'};
    }
  } catch {
    // Keep the deployment OAuth result when the optional runtime fallback is unavailable.
  }
  return {...generic, oauth_strategy: 'generic'};
}

function invalidateMcpProviderStatus(providerId) {
  if (!providerId) return;
  const entry = mcpProviderStatusEntry(providerId);
  entry.value = null;
  entry.fetchedAt = 0;
  entry.generation += 1;
  entry.inFlight = null;
}

function requestMcpProviderStatus(providerId, {force = false} = {}) {
  const ticket = captureMcpAccountUi();
  const entry = mcpProviderStatusEntry(providerId);
  const fresh = entry.value && (Date.now() - entry.fetchedAt) < MCP_PROVIDER_STATUS_TTL_MS;
  if (!force && fresh) return Promise.resolve(entry.value);
  if (entry.inFlight) return entry.inFlight;

  const generation = entry.generation;
  const request = resolveMcpProviderStatus(providerId)
    .then((value) => {
      if (!mcpAccountUiIsCurrent(ticket) || entry.generation !== generation) return null;
      if (entry.generation === generation) {
        entry.value = value;
        entry.fetchedAt = Date.now();
      }
      return value;
    })
    .finally(() => {
      if (entry.inFlight === request) entry.inFlight = null;
    });
  entry.inFlight = request;
  return request;
}

function renderMcpOAuthStatusShell(preset) {
  const providerId = mcpOAuthProviderId(preset);
  const oauthMode = Boolean(providerId);
  $('mcpManualPanel').classList.toggle('hidden', oauthMode);
  $('mcpOAuthPanel').classList.toggle('hidden', !oauthMode);
  $('mcpManualConnectBtn').classList.toggle('hidden', oauthMode);
  $('mcpOAuthConnectBtn').classList.toggle('hidden', !oauthMode);
  if (!providerId) return null;

  const sourceIcon = $('mcpConfigIcon');
  $('mcpOAuthProviderIcon').innerHTML = sourceIcon?.innerHTML || '';
  $('mcpOAuthProviderIcon').className = `mcp-oauth-provider-icon ${sourceIcon?.className || ''}`;
  const titles = {github: 'Connect GitHub', slack: 'Connect Slack', 'google-drive': 'Connect Google Drive', 'microsoft-teams': 'Connect Microsoft Teams'};
  $('mcpOAuthTitle').textContent = titles[providerId] || 'Connect provider';
  const descriptions = {
    github: 'Sign in to your own GitHub account and approve ArchBro. Your GitHub token stays backend-only and is attached only to your ArchBro user while connecting to GitHub remote MCP.',
    slack: 'Sign in to your own Slack workspace account and approve ArchBro. Your Slack user token stays backend-only and is attached only to your ArchBro user while connecting to Slack remote MCP.',
    'google-drive': 'Sign in to your own Google account and approve ArchBro. Your Google token stays backend-only and is attached only to your ArchBro user while connecting to Google Drive remote MCP.',
    'microsoft-teams': 'A Microsoft authorization window will open. ArchBro uses delegated Microsoft Graph access for Teams and keeps the OAuth session backend-only and encrypted at rest.',
  };
  $('mcpOAuthDescription').textContent = descriptions[providerId] || 'A provider sign-in window will open.';
  $('mcpStoragePill').textContent = 'Checking storage…';
  return providerId;
}

function renderMcpOAuthStatusLoading(preset) {
  if ($('mcpPreset').value !== preset) return;
  $('mcpOAuthReady').classList.add('hidden');
  $('mcpProviderSetup').classList.add('hidden');
  $('mcpOAuthRedirectReady').textContent = 'Checking provider sign-in status…';
  $('mcpOAuthConnectBtn').classList.remove('hidden');
  $('mcpOAuthConnectBtn').disabled = true;
  $('mcpOAuthConnectBtn').textContent = 'Checking sign-in…';
  delete $('mcpOAuthConnectBtn').dataset.statusRetry;
}

function renderMcpOAuthStatus(preset, status) {
  if ($('mcpPreset').value !== preset || !status) return;
  const providerId = mcpOAuthProviderId(preset);
  $('mcpOAuthReady').classList.add('hidden');
  $('mcpProviderSetup').classList.add('hidden');
  delete $('mcpOAuthConnectBtn').dataset.statusRetry;

  const configured = status.configured === true;
  const connected = status.connected === true;
  const persistent = status.persistent === true;
  const restoreError = String(status.restore_error || '').trim();
  if (providerId && connected) {
    const entry = mcpProviderStatusEntry(providerId);
    entry.value = status;
    entry.fetchedAt = Date.now();
  }
  $('mcpStoragePill').textContent = persistent ? 'Encrypted at rest' : 'Session only';
  $('mcpOAuthPrivacyText').textContent = persistent
    ? 'Access and refresh tokens stay backend-only and are encrypted in PostgreSQL. The browser and WebMCP agent never receive them.'
    : 'This connector is available only for the current server session. Deployed ArchBro should use encrypted provider storage.';
  $('mcpOAuthStateTitle').textContent = connected
    ? 'Connected'
    : restoreError
      ? 'Reconnect required'
      : 'Ready to connect';
  $('mcpOAuthRedirectReady').textContent = connected
    ? `${status.name} is connected.${persistent ? ' This authorization follows your ArchBro account across signed-in browsers and deployment updates.' : ' This session is not persisted.'}`
    : restoreError
      ? `${status.name} authorization could not be restored. ${restoreError}`
      : configured
        ? `${status.name} sign-in is ready. Authorization opens in a separate window.`
        : `${status.name} is a built-in ArchBro connector. Sign-in requires the ArchBro deployment provider identity.`;
  const ready = configured || connected || Boolean(restoreError);
  $('mcpOAuthReady').classList.toggle('hidden', !ready);
  $('mcpProviderSetup').classList.toggle('hidden', ready);
  const missingConfiguration = Array.isArray(status.missing_configuration) && status.missing_configuration.length
    ? ` Missing: ${status.missing_configuration.join(', ')}.`
    : '';
  $('mcpProviderSetupText').textContent = configured || connected
    ? ''
    : `${status.name} sign-in is not provisioned for this ArchBro deployment. The deployment owner must configure the provider identity.${missingConfiguration}`;
  $('mcpOAuthConnectBtn').classList.remove('hidden');
  $('mcpOAuthConnectBtn').disabled = !configured;
  $('mcpOAuthConnectBtn').textContent = connected || restoreError
    ? `Reconnect ${status.name}`
    : `Continue with ${status.name}`;
  syncMcpProviderCards();
}

function renderMcpOAuthStatusError(preset, err) {
  if ($('mcpPreset').value !== preset) return;
  $('mcpOAuthRedirectReady').textContent = `Unable to refresh sign-in status: ${err.message}`;
  $('mcpOAuthConnectBtn').classList.remove('hidden');
  $('mcpOAuthConnectBtn').disabled = false;
  $('mcpOAuthConnectBtn').textContent = 'Retry status';
  $('mcpOAuthConnectBtn').dataset.statusRetry = 'true';
}

async function refreshMcpProviderStatusAfterMutation(providerId) {
  if (!providerId) return null;
  const ticket = captureMcpAccountUi();
  invalidateMcpProviderStatus(providerId);
  const preset = mcpPresetForProvider(providerId);
  try {
    const status = await requestMcpProviderStatus(providerId, {force: true});
    if (!mcpAccountUiIsCurrent(ticket)) return null;
    if (preset) renderMcpOAuthStatus(preset, status);
    syncMcpProviderCards();
    return status;
  } catch (err) {
    if (mcpAccountUiIsCurrent(ticket) && preset) renderMcpOAuthStatusError(preset, err);
    return null;
  }
}

function openMcpOAuthPopup(providerId) {
  const width = 620;
  const height = 760;
  const left = Math.max(0, window.screenX + Math.round((window.outerWidth - width) / 2));
  const top = Math.max(0, window.screenY + Math.round((window.outerHeight - height) / 2));
  const popup = window.open(
    'about:blank',
    `archbro_mcp_oauth_${providerId}`,
    `popup=yes,width=${width},height=${height},left=${left},top=${top}`,
  );
  if (!popup) {
    toast('Your browser blocked the OAuth window. Allow popups for this ArchBro site and retry.', true);
    return null;
  }
  activeMcpOAuthPopup = popup;
  popup.document.title = 'ArchBro authorization';
  popup.document.body.innerHTML = '<p style="font:16px system-ui;padding:24px">Opening authorization…</p>';
  popup.focus();
  return popup;
}

function watchMcpOAuthPopup(popup, preset, {wasConnected = false} = {}) {
  const providerId = mcpOAuthProviderId(preset);
  const timer = setInterval(async () => {
    if (!popup.closed) return;
    clearInterval(timer);
    if (activeMcpOAuthPopup === popup) activeMcpOAuthPopup = null;
    if (handledMcpOAuthPopups.has(popup)) {
      handledMcpOAuthPopups.delete(popup);
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
    if (!providerId) return;
    const openConnected = Boolean(
      $('mcpConnectionsDialog').open && $('mcpPreset').value === preset,
    );
    try {
      const result = await reconcileMcpProviderConnection(providerId, {openConnected});
      if (result.connected && !wasConnected) {
        const name = result.status?.name || providerId.replace('-', ' ');
        toast(`${name} connected to your ArchBro account.`);
        if (openConnected) setMcpPickerTab('connected');
      }
    } catch (err) {
      if (openConnected) renderMcpOAuthStatusError(preset, err);
    }
  }, 500);
}

function applyMcpPreset(preset = $('mcpPreset').value) {
  $('mcpStoragePill').textContent = mcpOAuthProviderId(preset) ? 'Checking storage…' : 'Memory only';
  $('mcpBearerToken').value = '';
  $('mcpCommand').value = '';
  $('mcpArgs').value = '';
  $('mcpEnv').value = '{}';
  $('mcpTransport').value = 'streamable_http';
  $('mcpBearerToken').placeholder = 'Paste access token if required';
  $('mcpAuthHint').textContent = 'Bearer token · kept in memory only';

  if (preset === 'github-remote') {
    $('mcpName').value = 'GitHub';
    $('mcpUrl').value = 'https://api.githubcopilot.com/mcp/';
    $('mcpBearerToken').placeholder = 'GitHub access token';
    $('mcpAuthHint').textContent = 'GitHub token · kept in memory only';
  } else if (preset === 'slack') {
    $('mcpName').value = 'Slack';
    $('mcpUrl').value = 'https://mcp.slack.com/mcp';
  } else if (preset === 'google-drive') {
    $('mcpName').value = 'Google Drive';
    $('mcpUrl').value = 'https://drivemcp.googleapis.com/mcp/v1';
    $('mcpAuthHint').textContent = 'Google Drive OAuth · remote MCP';
  } else if (preset === 'microsoft-teams') {
    $('mcpName').value = 'Microsoft Teams';
    $('mcpUrl').value = 'https://graph.microsoft.com/v1.0';
  } else {
    $('mcpName').value = 'Custom MCP';
    $('mcpUrl').value = '';
    $('mcpAuthHint').textContent = 'Optional bearer token · kept in memory only';
  }
  syncMcpTransportFields();
}

async function loadMcpOAuthStatus(preset = $('mcpPreset').value, {force = false, background = false} = {}) {
  const requestId = ++mcpOAuthStatusRequestId;
  const providerId = renderMcpOAuthStatusShell(preset);
  if (!providerId) return null;

  const entry = mcpProviderStatusEntry(providerId);
  const fresh = entry.value && (Date.now() - entry.fetchedAt) < MCP_PROVIDER_STATUS_TTL_MS;
  if (entry.value) renderMcpOAuthStatus(preset, entry.value);
  if (!force && fresh) return entry.value;
  if (!entry.value) renderMcpOAuthStatusLoading(preset);

  const request = requestMcpProviderStatus(providerId, {force});
  const applyResult = request
    .then((status) => {
      if (requestId === mcpOAuthStatusRequestId && $('mcpPreset').value === preset) {
        renderMcpOAuthStatus(preset, status);
      }
      return status;
    })
    .catch((err) => {
      if (requestId === mcpOAuthStatusRequestId && $('mcpPreset').value === preset) {
        renderMcpOAuthStatusError(preset, err);
      }
      return null;
    });

  if (background) {
    void applyResult;
    return entry.value;
  }
  return applyResult;
}

function selectMcpPreset(preset) {
  const selected = document.querySelector(`[data-mcp-preset="${preset}"]`);
  if (!selected) return;
  $('mcpPreset').value = preset;
  document.querySelectorAll('[data-mcp-preset]').forEach((card) => card.classList.toggle('selected', card === selected));
  const sourceIcon = selected.querySelector('.mcp-provider-icon');
  const configIcon = $('mcpConfigIcon');
  if (sourceIcon && configIcon) {
    configIcon.className = sourceIcon.className;
    configIcon.innerHTML = sourceIcon.innerHTML;
  }
  const title = selected.querySelector('.mcp-provider-copy strong')?.textContent || 'Custom MCP';
  const subtitle = selected.querySelector('.mcp-provider-copy small')?.textContent || 'Remote or local';
  $('mcpConfigTitle').textContent = title;
  $('mcpConfigSubtitle').textContent = subtitle;
  applyMcpPreset(preset);
  void loadMcpOAuthStatus(preset, {background: true});
}

async function startLegacyMcpOAuth(providerId, popup, preset, status) {
  let connectionId = null;
  try {
    const started = await api(`/mcp/auth/${encodeURIComponent(providerId)}/start`, {method: 'POST'});
    connectionId = started.connection?.id || null;
    if (started.connected) {
      popup.close();
      toast(`${status.name} connected: ${started.tool_count || started.connection?.tool_count || 0} tools discovered.`);
      await completeMcpConnectionUi(providerId, `${status.name} connected successfully.`);
      return;
    }
    if (!started.authorization_url || !connectionId) {
      throw new Error(`${status.name} did not return an authorization URL.`);
    }
    popup.location.replace(started.authorization_url);
    popup.focus();

    const deadline = Date.now() + 180000;
    while (Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, 1500));
      const result = await api(`/mcp/auth/${encodeURIComponent(providerId)}/${encodeURIComponent(connectionId)}/poll`, {method: 'POST'});
      if (!result.connected) {
        if (providerId === 'google-drive' && popup.closed) {
          throw new Error('Google Drive authorization was cancelled.');
        }
        continue;
      }
      if (!popup.closed) popup.close();
      toast(`${status.name} connected: ${result.tool_count || 0} tools discovered.`);
      await completeMcpConnectionUi(providerId, `${status.name} connected successfully.`);
      return;
    }
    throw new Error(`${status.name} authorization timed out. Retry Connect when ready.`);
  } catch (err) {
    if (!popup.closed) popup.close();
    if (connectionId) {
      try { await api(`/mcp/connections/${encodeURIComponent(connectionId)}`, {method: 'DELETE'}); } catch {}
    }
    toast(err.message, true);
    await refreshMcpProviderStatusAfterMutation(providerId);
  }
}

async function startMcpOAuth() {
  const preset = $('mcpPreset').value;
  const providerId = mcpOAuthProviderId(preset);
  if (!providerId) return;
  if ($('mcpOAuthConnectBtn').dataset.statusRetry === 'true') {
    await loadMcpOAuthStatus(preset, {force: true});
    return;
  }

  const popup = openMcpOAuthPopup(providerId);
  if (!popup) return;

  const status = await loadMcpOAuthStatus(preset);
  if ($('mcpPreset').value !== preset || !$('mcpConnectionsDialog').open) {
    popup.close();
    return;
  }

  $('mcpOAuthConnectBtn').disabled = true;
  $('mcpOAuthConnectBtn').textContent = 'Waiting for authorization…';



  if (!status?.configured) {
    popup.close();
    toast(`${providerId.replace('-', ' ')} sign-in is not available in this ArchBro deployment.`, true);
    return;
  }

  if (status.oauth_strategy === 'legacy-runtime') {
    await startLegacyMcpOAuth(providerId, popup, preset, status);
    return;
  }

  try {
    const started = await api(`/mcp/oauth/${encodeURIComponent(providerId)}/start`, {method: 'POST'});
    if (!started?.authorization_url) throw new Error(`${status.name} did not return an authorization URL.`);
    popup.location.replace(started.authorization_url);
    popup.focus();
    watchMcpOAuthPopup(popup, preset, {wasConnected: status.connected === true});
  } catch (err) {
    if (!popup.closed) popup.close();
    toast(err.message, true);
    await refreshMcpProviderStatusAfterMutation(providerId);
  }
}

function setMcpPickerTab(tab) {
  const connected = tab === 'connected';
  $('mcpBrowsePane').classList.toggle('hidden', connected);
  $('mcpConnectedPane').classList.toggle('hidden', !connected);
  document.querySelectorAll('[data-mcp-tab]').forEach((button) => button.classList.toggle('active', button.dataset.mcpTab === tab));
}

function filterMcpProviders(query = '') {
  const normalized = String(query).trim().toLowerCase();
  let visible = 0;
  document.querySelectorAll('[data-mcp-preset]').forEach((card) => {
    const haystack = `${card.dataset.mcpSearch || ''} ${card.textContent || ''}`.toLowerCase();
    const match = !normalized || haystack.includes(normalized);
    card.classList.toggle('hidden', !match);
    if (match) visible += 1;
  });
  $('mcpNoSearchResults').classList.toggle('hidden', visible !== 0);
}

function parseMcpJson(id, fallback) {
  const text = $(id).value.trim();
  if (!text) return fallback;
  try {
    return JSON.parse(text);
  } catch {
    throw new Error(`${id === 'mcpArgs' ? 'Args' : 'Env'} must be valid JSON.`);
  }
}

async function loadMcpConnections() {
  const ticket = captureMcpAccountUi();
  const serial = ++mcpConnectionsRequestSerial;
  const connections = await api('/mcp/connections');
  if (!mcpAccountUiIsCurrent(ticket)) return [];
  if (serial !== mcpConnectionsRequestSerial) return mcpConnectionsSnapshot;
  mcpConnectionsSnapshot = Array.isArray(connections) ? connections : [];
  syncMcpProviderCards(mcpConnectionsSnapshot);
  const connectionNotice = $('mcpConnectionNotice');
  if (connectionNotice?.dataset.provider
      && !mcpConnectionForProvider(connectionNotice.dataset.provider, mcpConnectionsSnapshot)) {
    connectionNotice.textContent = '';
    delete connectionNotice.dataset.provider;
    connectionNotice.classList.add('hidden');
  }
  const list = $('mcpConnectionList');
  $('mcpConnectedCount').textContent = mcpConnectionsSnapshot.length;
  if (!mcpConnectionsSnapshot.length) {
    list.innerHTML = '<div class="mcp-empty-state"><strong>No MCPs connected yet</strong><span>Choose Browse and connect one when you are ready.</span></div>';
    return connections;
  }
  list.innerHTML = mcpConnectionsSnapshot.map((connection) => {
    const probe = connection.last_probe_ok === true ? 'READY' : connection.last_probe_ok === false ? 'FAILED' : 'NOT TESTED';
    const probeClass = connection.last_probe_ok === true ? 'ready' : connection.last_probe_ok === false ? 'failed' : '';
    const toolCount = connection.tool_count == null ? '—' : connection.tool_count;
    const authBadge = connection.auth_type === 'oauth'
      ? '<span class="ready">OAuth</span>'
      : connection.auth_type === 'github_oauth'
        ? '<span class="ready">GitHub OAuth</span>'
      : ['google_gcloud', 'google_drive_oauth'].includes(connection.auth_type)
          ? '<span class="ready">Google OAuth</span>'
        : connection.auth_type === 'microsoft_teams_oauth'
          ? '<span class="ready">Teams OAuth</span>'
        : connection.has_credentials ? '<span>credential set</span>' : '';
    const storageBadge = connection.persistent
      ? '<span class="ready">Saved securely</span>'
      : connection.provider ? '<span>Session only</span>' : '';
    return `<div class="mcp-connection-row">
      <div class="mcp-connected-main"><span class="mcp-connected-dot ${probeClass}"></span><div><strong>${escapeHtml(connection.name)}</strong><p>${escapeHtml(connection.endpoint || '')}</p><div class="mcp-connection-meta"><span>${escapeHtml(connection.transport)}</span><span class="${probeClass}">${probe}</span><span>${toolCount} tools</span>${authBadge}${storageBadge}</div>${connection.last_error ? `<p class="mcp-connection-error">${escapeHtml(connection.last_error)}</p>` : ''}</div></div>
      <div class="mcp-connection-actions"><button type="button" data-mcp-probe="${escapeHtml(connection.id)}">Test</button><button type="button" class="danger" data-mcp-remove="${escapeHtml(connection.id)}" data-mcp-provider="${escapeHtml(connection.provider || '')}">Remove</button></div>
    </div>`;
  }).join('');
  list.querySelectorAll('[data-mcp-probe]').forEach((button) => button.addEventListener('click', async () => {
    button.disabled = true;
    button.textContent = 'Testing…';
    try {
      const result = await api(`/mcp/connections/${encodeURIComponent(button.dataset.mcpProbe)}/probe`, {method: 'POST'});
      toast(`MCP ready: ${result.tool_count} tool${result.tool_count === 1 ? '' : 's'} discovered.`);
    } catch (err) {
      toast(err.message, true);
    } finally {
      await loadMcpConnections();
    }
  }));
  list.querySelectorAll('[data-mcp-remove]').forEach((button) => button.addEventListener('click', async () => {
    button.disabled = true;
    const providerId = button.dataset.mcpProvider || null;
    try {
      await api(`/mcp/connections/${encodeURIComponent(button.dataset.mcpRemove)}`, {method: 'DELETE'});
      toast('MCP connection removed.');
      if (providerId) await refreshMcpProviderStatusAfterMutation(providerId);
    } catch (err) {
      toast(err.message, true);
    } finally {
      await loadMcpConnections();
    }
  }));
  return mcpConnectionsSnapshot;
}

function openMcpConnections() {
  $('mcpConnectionsDialog').showModal();
  $('mcpSearch').value = '';
  filterMcpProviders('');
  setMcpPickerTab('browse');
  selectMcpPreset($('mcpPreset').value || 'github-remote');
  setTimeout(() => $('mcpSearch').focus(), 20);
  void loadMcpConnections().catch((err) => toast(err.message, true));
}

async function addMcpConnection() {
  if (mcpOAuthProviderId()) throw new Error('Use the provider sign-in button for GitHub, Slack, Google Drive, or Microsoft Teams.');
  const transport = $('mcpTransport').value;
  const secret = $('mcpBearerToken').value.trim();
  const body = {
    name: $('mcpName').value.trim(),
    transport,
  };
  if (!body.name) throw new Error('Connection name is required.');
  if (transport === 'streamable_http') {
    body.url = $('mcpUrl').value.trim();
    body.headers = secret ? {Authorization: `Bearer ${secret}`} : {};
  } else {
    const args = parseMcpJson('mcpArgs', []);
    const env = parseMcpJson('mcpEnv', {});
    if (!Array.isArray(args)) throw new Error('Args must be a JSON array.');
    if (!env || Array.isArray(env) || typeof env !== 'object') throw new Error('Env must be a JSON object.');
    body.command = $('mcpCommand').value.trim();
    body.args = args.map((value) => String(value));
    body.env = Object.fromEntries(Object.entries(env).map(([key, value]) => [key, String(value)]));
  }
  await api('/mcp/connections', {method: 'POST', body: JSON.stringify(body)});
  $('mcpBearerToken').value = '';
  $('mcpEnv').value = transport === 'stdio' ? '{}' : '';
  toast('MCP connected in memory.');
  await loadMcpConnections();
  setMcpPickerTab('connected');
}

function committedWebMcpProjectId() {
  const committedProjectId = state.navigation.initialized
    ? (state.navigation.committed?.projectId || null)
    : null;
  if (
    state.onboarding.active
    || !committedProjectId
    || committedProjectId !== state.projectId
    || !state.project
    || !state.architecture
  ) return null;
  return committedProjectId;
}

function webMcpRequireProject() {
  const projectId = committedWebMcpProjectId();
  if (!projectId) throw new Error('No active ArchBro project is loaded.');
  return projectId;
}

function webMcpContext() {
  const projectId = committedWebMcpProjectId();
  if (!projectId) {
    return {
      project: null,
      view: state.onboarding.active ? 'onboarding' : 'workspace',
      project_count: state.projects.length,
      can_create_project: true,
    };
  }
  const selectedTask = state.tasks.find((item) => item.id === state.selectedTaskId) || null;
  const selectedNode = findArchitectureNode(state.selectedComponentId || state.scopeComponentId);
  const pending = state.proposals.filter((proposal) => isProposalActionable(proposal, state.architecture));
  const selectedProposal = state.proposals.find((proposal) => (
    proposal.id === state.selectedProposalId && isProposalActionable(proposal, state.architecture)
  )) || pending[0] || null;
  return {
    project: state.project,
    view: state.navigation.committed?.view || state.currentView,
    architecture_version: state.architecture.version,
    selected_task: selectedTask,
    selected_architecture_node: selectedNode,
    selected_proposal: selectedProposal,
    pending_proposal_count: pending.length,
  };
}

// Keep mutation receipts bound to the project that was actually mutated even
// when the person navigates elsewhere before the request finishes.
// WEBMCP_MUTATION_CONTEXT_HELPERS_START
function captureWebMcpProject() {
  webMcpRequireProject();
  const projectId = state.projectId;
  return {projectId, guard:captureNavigationGuard(projectId)};
}

function webMcpProjectStillCurrent(capture) {
  return Boolean(capture)
    && committedProjectGuardIsCurrent(capture.guard)
    && state.projectId === capture.projectId;
}

function webMcpMutationContext(capture) {
  return webMcpProjectStillCurrent(capture)
    ? webMcpContext()
    : {project_id:capture?.projectId || null, navigation_superseded:true};
}

async function refreshWebMcpProject(capture) {
  if (!webMcpProjectStillCurrent(capture)) return false;
  return refresh({projectId:capture.projectId, guard:capture.guard});
}
// WEBMCP_MUTATION_CONTEXT_HELPERS_END

const WEBMCP_ARCHITECTURE_KINDS = new Set([
  'SYSTEM', 'UI', 'SERVICE', 'AGENT', 'TOOL', 'DATA_STORE', 'STATE',
  'EXTERNAL_SERVICE', 'INFRASTRUCTURE',
]);

function normalizeWebMcpArchitectureComponents(rawComponents, {requireIds = false, maxDepth = 3} = {}) {
  if (!Array.isArray(rawComponents) || !rawComponents.length) {
    throw new Error('At least one architecture component is required.');
  }
  const usedIds = new Set();
  const aliases = new Map();
  const ambiguousNames = new Set();

  const registerName = (name, id) => {
    const key = name.toLowerCase();
    if (aliases.has(key) && aliases.get(key) !== id) {
      ambiguousNames.add(key);
      aliases.delete(key);
      return;
    }
    if (!ambiguousNames.has(key)) aliases.set(key, id);
  };

  const normalize = (component, depth, indexPath) => {
    if (!component || typeof component !== 'object' || Array.isArray(component)) {
      throw new Error('Every architecture component must be an object.');
    }
    if (depth > maxDepth) throw new Error(`Architecture depth is capped at ${maxDepth} levels.`);
    const componentName = String(component.name || '').trim();
    const componentType = String(component.type || '').trim();
    const responsibility = String(component.responsibility || '').trim();
    const explicitId = String(component.id || '').trim();
    if (!componentName || !componentType || !responsibility) {
      throw new Error('Every component requires name, type, and responsibility.');
    }
    if (requireIds && !explicitId) throw new Error('Every architecture component requires a stable id.');
    const baseId = explicitId || componentName.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '') || `component-${indexPath.join('-')}`;
    let id = baseId;
    if (explicitId && usedIds.has(id)) throw new Error(`Duplicate architecture component id: ${id}`);
    let suffix = 2;
    while (!explicitId && usedIds.has(id)) id = `${baseId}-${suffix++}`;
    usedIds.add(id);
    aliases.set(id.toLowerCase(), id);
    registerName(componentName, id);

    const kind = String(component.kind || 'SYSTEM').trim().toUpperCase();
    if (!WEBMCP_ARCHITECTURE_KINDS.has(kind)) throw new Error(`Unsupported architecture component kind: ${kind}`);
    const rawChildren = component.children ?? [];
    if (!Array.isArray(rawChildren)) throw new Error(`Component ${id} children must be an array.`);
    if (depth >= maxDepth && rawChildren.length) {
      throw new Error(`Architecture depth is capped at ${maxDepth} levels.`);
    }
    const children = rawChildren.map((child, index) => normalize(child, depth + 1, [...indexPath, index + 1]));
    return {
      id,
      name: componentName,
      type: componentType,
      responsibility,
      status: String(component.status || 'PLANNED').trim() || 'PLANNED',
      kind,
      children,
    };
  };

  const components = rawComponents.map((component, index) => normalize(component, 1, [index + 1]));
  const resolveComponent = (value) => {
    const raw = String(value || '').trim();
    const key = raw.toLowerCase();
    if (ambiguousNames.has(key)) throw new Error(`Architecture component name is ambiguous; use a stable id instead: ${raw}`);
    return aliases.get(key) || raw;
  };
  return {components, resolveComponent};
}

function normalizeInitialPlanningTrace(rawTrace, normalizedComponents) {
  if (!rawTrace || typeof rawTrace !== 'object' || Array.isArray(rawTrace)) {
    throw new Error('planning_trace is required for WebMCP initial architecture.');
  }
  if (rawTrace.reconciled !== true) {
    throw new Error('planning_trace.reconciled must be true after relationships and tasks are reconciled.');
  }
  const rootIds = normalizedComponents.map((component) => component.id);
  const leafRoots = normalizedComponents.filter((component) => !(component.children || []).length).map((component) => component.id);
  if (leafRoots.length) {
    throw new Error(`WebMCP SYSTEM_MAP roots must be expanded architecture boundaries; atomic components belong below a root: ${leafRoots.join(', ')}.`);
  }
  const systemMapRootIds = Array.isArray(rawTrace.system_map_root_ids)
    ? rawTrace.system_map_root_ids.map((value) => String(value || '').trim())
    : [];
  if (systemMapRootIds.length !== rootIds.length || systemMapRootIds.some((id, index) => id !== rootIds[index])) {
    throw new Error('planning_trace.system_map_root_ids must exactly match final architecture roots in order.');
  }
  const flattenPreorder = (components) => components.flatMap((component) => [component, ...flattenPreorder(component.children || [])]);
  const plannedComponents = flattenPreorder(normalizedComponents);
  const rawEvaluations = Array.isArray(rawTrace.scope_evaluations) ? rawTrace.scope_evaluations : [];
  if (rawEvaluations.length !== plannedComponents.length) {
    throw new Error('planning_trace.scope_evaluations must cover every canonical component exactly once in preorder.');
  }
  const scopeEvaluations = rawEvaluations.map((evaluation, index) => {
    const component = plannedComponents[index];
    const scopeComponentId = String(evaluation?.scope_component_id || '').trim();
    if (scopeComponentId !== component.id) {
      throw new Error('planning_trace.scope_evaluations must follow canonical component preorder.');
    }
    const decomposition = String(evaluation?.decomposition || '').trim();
    const childIds = Array.isArray(evaluation?.child_ids)
      ? evaluation.child_ids.map((value) => String(value || '').trim())
      : [];
    if (childIds.some((id) => !id) || new Set(childIds).size !== childIds.length) {
      throw new Error(`planning_trace child_ids must be non-empty and unique for scope ${scopeComponentId}.`);
    }
    const expectedChildIds = (component.children || []).map((child) => child.id);
    const leafReason = String(evaluation?.leaf_reason || '').trim();
    if (expectedChildIds.length) {
      if (decomposition !== 'EXPANDED') throw new Error(`Scope ${scopeComponentId} has children and must be EXPANDED.`);
      if (leafReason) throw new Error(`EXPANDED scope ${scopeComponentId} must not provide leaf_reason.`);
      if (childIds.length !== expectedChildIds.length || childIds.some((id, childIndex) => id !== expectedChildIds[childIndex])) {
        throw new Error(`planning_trace child_ids do not match immediate final children for scope ${scopeComponentId}.`);
      }
    } else {
      if (decomposition !== 'JUSTIFIED_LEAF') throw new Error(`Scope ${scopeComponentId} has no children and must be JUSTIFIED_LEAF.`);
      if (childIds.length) throw new Error(`JUSTIFIED_LEAF scope ${scopeComponentId} must not provide child_ids.`);
      if (leafReason.length < 24) throw new Error(`JUSTIFIED_LEAF scope ${scopeComponentId} requires a specific leaf_reason of at least 24 characters.`);
    }
    return {scope_component_id: scopeComponentId, decomposition, child_ids: childIds, ...(leafReason ? {leaf_reason: leafReason} : {})};
  });
  return {
    system_map_root_ids: systemMapRootIds,
    scope_evaluations: scopeEvaluations,
    reconciled: true,
  };
}

// A bootstrap mutation can succeed server-side even when its response is lost.
// Never invite an agent to retry until a canonical read-back proves the outcome.
// WEBMCP_BOOTSTRAP_RECONCILIATION_START
function bootstrapArchitectureVersion(result) {
  const version = result?.architecture?.version;
  return Number.isInteger(version) && version === 1 ? version : null;
}

function classifyBootstrapReadback(bootstrap) {
  if (!bootstrap || bootstrap.schema !== 'archbro.workspace-bootstrap.v2') {
    return {status:'UNKNOWN', reason:'invalid_workspace_bootstrap'};
  }
  const architectureVersion = bootstrap.architecture?.version;
  const projectVersion = bootstrap.project?.architecture_version;
  if (
    Number.isInteger(architectureVersion)
    && architectureVersion >= 1
    && projectVersion === architectureVersion
  ) {
    return {
      status:'INITIALIZED',
      project:bootstrap.project,
      architecture:bootstrap.architecture,
      tasks:Array.isArray(bootstrap.tasks) ? bootstrap.tasks : [],
    };
  }
  if (architectureVersion === 0 && projectVersion === 0) {
    return {
      status:'NOT_INITIALIZED',
      project:bootstrap.project,
      architecture:bootstrap.architecture,
      tasks:[],
    };
  }
  return {
    status:'UNKNOWN',
    reason:'architecture_version_mismatch',
    project:bootstrap.project || null,
    architecture:bootstrap.architecture || null,
  };
}

async function reconcileBootstrapInitialization(projectId) {
  try {
    const bootstrap = await api(`/projects/${encodeURIComponent(projectId)}/workspace-bootstrap?reading_mode=MAP`);
    return classifyBootstrapReadback(bootstrap);
  } catch (error) {
    const message = String(error?.message || error);
    if (/^404:\s*project not found\b/i.test(message)) return {status:'NOT_FOUND'};
    return {status:'UNKNOWN', reason:'readback_failed', error:message};
  }
}

async function resolveBootstrapInitialization(projectId, result) {
  const directVersion = bootstrapArchitectureVersion(result);
  if (directVersion !== null) {
    return {
      status:'INITIALIZED',
      result,
      project:null,
      architecture_version:directVersion,
      reconciled:false,
    };
  }
  const reconciliation = await reconcileBootstrapInitialization(projectId);
  if (reconciliation.status !== 'INITIALIZED') return {...reconciliation, result};
  const recoveredResult = {
    ...(result && typeof result === 'object' && !Array.isArray(result) ? result : {}),
    architecture:reconciliation.architecture,
    tasks:reconciliation.tasks,
  };
  return {
    status:'INITIALIZED',
    result:recoveredResult,
    project:reconciliation.project,
    architecture_version:reconciliation.architecture.version,
    reconciled:true,
  };
}

function bootstrapOutcomeUnknownError(projectId, reason = 'readback_failed') {
  const error = new Error(
    `Bootstrap initialization outcome is unknown for project ${projectId}; do not retry automatically. The project was retained for reconciliation.`,
  );
  error.code = 'ARCHBRO_BOOTSTRAP_OUTCOME_UNKNOWN';
  error.project_id = projectId;
  error.mutation_outcome = 'UNKNOWN';
  error.may_have_written = true;
  error.next_step = 'Read the project bootstrap state before retrying.';
  error.reason = reason;
  return error;
}
// WEBMCP_BOOTSTRAP_RECONCILIATION_END

window.ArchBroWebBridge = {
  getCommittedNavigation() {
    return committedNavigationSnapshot();
  },

  getActiveProjectId() {
    return committedWebMcpProjectId();
  },

  getActiveProjectBinding() {
    return {
      projectId: committedWebMcpProjectId(),
      generation: workspaceContextGeneration,
      repositoryRevision: state.project?.repository_revision || 0,
    };
  },

  async bootstrapProject({name, goal, architectureSummary, components = [], relationships = [], tasks = [], planningTrace, reasoning} = {}) {
    await ensureAppInitialized();
    const projectName = String(name || '').trim();
    const projectGoal = String(goal || '').trim();
    const summary = String(architectureSummary || '').trim();
    const bootstrapReasoning = String(reasoning || '').trim();
    if (!projectName) throw new Error('Project name is required.');
    if (!projectGoal) throw new Error('Project goal is required.');
    if (!summary) throw new Error('Architecture summary is required.');
    if (!Array.isArray(components) || !components.length) throw new Error('At least one architecture component is required.');
    if (!Array.isArray(tasks) || !tasks.length) throw new Error('At least one initial task is required.');
    if (!bootstrapReasoning) throw new Error('Architecture reasoning is required.');

    const {components: normalizedComponents, resolveComponent} = normalizeWebMcpArchitectureComponents(components, {requireIds: true});
    const normalizedPlanningTrace = normalizeInitialPlanningTrace(planningTrace, normalizedComponents);
    const architecture = {
      version: 1,
      summary,
      components: normalizedComponents,
      relationships: (relationships || []).map((relationship) => ({
        source: resolveComponent(relationship?.source),
        target: resolveComponent(relationship?.target),
        relationship_type: String(relationship?.type || 'DEPENDS_ON').trim(),
        description: String(relationship?.description || '').trim(),
      })),
      decisions: [],
      assumptions: [],
      risks: [],
    };
    const normalizedTasks = tasks.map((task) => ({
      title: String(task?.title || '').trim(),
      description: String(task?.description || '').trim(),
      related_component: task?.component ? resolveComponent(task.component) : null,
      source: 'AGENT',
      acceptance_criteria: [],
      dependencies: [],
    }));
    if (normalizedTasks.some((task) => !task.title)) throw new Error('Every initial task requires a title.');

    const callerGuard = captureNavigationGuard(state.projectId);
    const project = await api('/projects', {
      method: 'POST',
      body: JSON.stringify({name: projectName, goal: projectGoal, description: ''}),
    });
    let result = null;
    let initializationReconciled = false;
    let resolvedProject = null;
    let committedArchitectureVersion = null;
    try {
      result = await api(`/projects/${project.id}/interactive-initial-architecture`, {
        method: 'POST',
        body: JSON.stringify({
          architecture,
          tasks: normalizedTasks,
          reasoning: bootstrapReasoning,
          planning_trace: normalizedPlanningTrace,
        }),
      });
    } catch (initializationError) {
      const resolution = await resolveBootstrapInitialization(project.id, null);
      if (resolution.status === 'INITIALIZED') {
        initializationReconciled = true;
        resolvedProject = resolution.project;
        committedArchitectureVersion = resolution.architecture_version;
        result = {
          ...resolution.result,
          reconciled_after_initialization_error: true,
        };
      } else if (resolution.status === 'NOT_INITIALIZED' || resolution.status === 'NOT_FOUND') {
        if (resolution.status === 'NOT_INITIALIZED') {
          try {
            await api(`/projects/${project.id}`, {method:'DELETE'});
          } catch (_cleanupError) {
            // The initialization is confirmed absent; cleanup remains best-effort.
          }
        }
        throw initializationError;
      } else {
        const ambiguous = bootstrapOutcomeUnknownError(project.id, resolution.reason);
        ambiguous.cause = initializationError;
        throw ambiguous;
      }
    }

    if (committedArchitectureVersion === null) {
      const resolution = await resolveBootstrapInitialization(project.id, result);
      if (resolution.status === 'INITIALIZED') {
        result = resolution.result;
        resolvedProject = resolution.project;
        committedArchitectureVersion = resolution.architecture_version;
        if (resolution.reconciled) {
          initializationReconciled = true;
          result = {...result, reconciled_after_invalid_response:true};
        }
      } else if (resolution.status === 'NOT_INITIALIZED' || resolution.status === 'NOT_FOUND') {
        if (resolution.status === 'NOT_INITIALIZED') {
          try {
            await api(`/projects/${project.id}`, {method:'DELETE'});
          } catch (_cleanupError) {
            // Read-back proved initialization absent; cleanup remains best-effort.
          }
        }
        const invalid = new Error('Bootstrap returned an invalid canonical architecture result and read-back confirmed initialization was not committed.');
        invalid.code = 'ARCHBRO_BOOTSTRAP_NOT_COMMITTED';
        invalid.project_id = project.id;
        invalid.mutation_outcome = 'NOT_COMMITTED';
        throw invalid;
      } else {
        throw bootstrapOutcomeUnknownError(project.id, resolution.reason);
      }
    }

    let uiRefresh = {status:'SKIPPED_SUPERSEDED'};
    if (navigationGenerationIsCurrent(callerGuard)) {
      const activationGuard = beginNavigationTransition(project.id);
      const opened = await selectProject(project.id, {
        view:'overview',
        canvas:false,
        historyMode:'push',
        navigationGuard:activationGuard,
      });
      if (opened) {
        const committedGuard = captureNavigationGuard(project.id);
        try {
          await loadProjects({guard:committedGuard});
          uiRefresh = webMcpProjectStillCurrent({projectId:project.id, guard:committedGuard})
            ? {status:'PASS'}
            : {status:'SKIPPED_SUPERSEDED'};
        } catch (refreshError) {
          uiRefresh = {status:'PARTIAL', error:refreshError.message};
        }
      } else {
        uiRefresh = {status:'FAILED', error:'Project initialized, but the UI could not activate it.'};
      }
    }
    const capture = {projectId:project.id, guard:captureNavigationGuard(project.id)};
    const initializedProject = {...(resolvedProject || project), architecture_version:committedArchitectureVersion};
    const mutationContext = {
      ...webMcpMutationContext(capture),
      architecture_version: committedArchitectureVersion,
    };
    return {
      ...result,
      project: initializedProject,
      mutation_outcome: 'INITIALIZED',
      initialization_reconciled: initializationReconciled,
      ui_refresh: uiRefresh,
      built_in_model_called: false,
      context: mutationContext,
    };
  },

  async expandArchitectureScope({scopeComponentId, children = [], reasoning, evidence = [], impact = '', expectedArchitectureVersion} = {}) {
    await ensureAppInitialized();
    const capture = captureWebMcpProject();
    await refreshWebMcpProject(capture);
    if (!webMcpProjectStillCurrent(capture)) throw supersededNavigationError();
    const scopeId = String(scopeComponentId || '').trim();
    if (!scopeId) throw new Error('scope_component_id is required.');
    if (!findArchitectureNode(scopeId)) throw new Error(`Architecture component not found: ${scopeId}`);
    const expected = Number(expectedArchitectureVersion);
    if (!Number.isInteger(expected) || expected < 0) throw new Error('expected_architecture_version must be a non-negative integer.');
    if (Number(state.architecture.version) !== expected) {
      throw new Error(`Stale architecture version: expected ${expected}, current ${state.architecture.version}.`);
    }
    const normalizedEvidence = (Array.isArray(evidence) ? evidence : []).map((item) => String(item || '').trim()).filter(Boolean);
    if (!normalizedEvidence.length) throw new Error('At least one evidence item is required.');
    const expansionReasoning = String(reasoning || '').trim();
    if (!expansionReasoning) throw new Error('Expansion reasoning is required.');
    const {components: normalizedChildren} = normalizeWebMcpArchitectureComponents(children, {requireIds: true, maxDepth: 1});
    const existingIds = new Set();
    const collectExistingIds = (nodes) => {
      for (const node of nodes || []) {
        existingIds.add(node.id);
        collectExistingIds(node.children || []);
      }
    };
    collectExistingIds(state.architecture.components);
    const collisions = normalizedChildren.map((child) => child.id).filter((id) => existingIds.has(id));
    if (collisions.length) throw new Error(`Expanded child ids already exist in architecture: ${collisions.join(', ')}`);
    return window.ArchBroWebBridge.submitAgentRecommendation({
      recommendation: 'ACCEPT_PROPOSED_CHANGE',
      reasoning: expansionReasoning,
      evidence: normalizedEvidence,
      observedChange: `The accepted ${scopeId} boundary needs one more explicit decomposition level.`,
      affectedComponents: [scopeId],
      proposedChanges: [{operation: 'expand_scope', component_id: scopeId, children: normalizedChildren}],
      impact: String(impact || '').trim() || `Adds explicit child boundaries under ${scopeId} without replacing existing component identities.`,
      expectedArchitectureVersion: expected,
    });
  },

  async createProject({name, goal, description = ''} = {}) {
    await ensureAppInitialized();
    const projectName = String(name || '').trim();
    const projectGoal = String(goal || '').trim();
    const projectDescription = String(description || '').trim();
    if (!projectName) throw new Error('Project name is required.');
    if (!projectGoal) throw new Error('Project goal is required.');

    const callerGuard = captureNavigationGuard(state.projectId);
    const project = await api('/projects', {
      method: 'POST',
      body: JSON.stringify({name: projectName, goal: projectGoal, description: projectDescription}),
    });
    let uiRefresh = {status:'SKIPPED_SUPERSEDED'};
    if (navigationGenerationIsCurrent(callerGuard)) {
      const activationGuard = beginNavigationTransition(project.id);
      const opened = await selectProject(project.id, {historyMode:'push', navigationGuard:activationGuard});
      if (opened) {
        const committedGuard = captureNavigationGuard(project.id);
        try {
          await loadProjects({guard:committedGuard});
          uiRefresh = webMcpProjectStillCurrent({projectId:project.id, guard:committedGuard}) ? {status:'PASS'} : {status:'SKIPPED_SUPERSEDED'};
        } catch (error) {
          uiRefresh = {status:'PARTIAL', error:error.message};
        }
      } else {
        uiRefresh = {status:'FAILED', error:'Project created, but the UI could not activate it.'};
      }
    }
    return {
      project,
      mutation_outcome: 'CREATED',
      ui_refresh: uiRefresh,
      bootstrap_required: true,
      bootstrap_provider: 'webmcp-agent',
      built_in_model_called: false,
      bootstrap_context: {
        goal: projectGoal,
        description: projectDescription,
        architecture_version_required: 1,
        task_status_default: 'TODO',
        rules: [
          'This low-level bridge method is internal; public WebMCP project creation uses archbro_bootstrap_project atomically.',
          'Use stable component ids that tasks can reference.',
        ],
      },
      recommended_next_tool: null,
    };
  },

  async submitInitialArchitecture({architecture, tasks = [], planningTrace, reasoning} = {}) {
    await ensureAppInitialized();
    const capture = captureWebMcpProject();
    if (!architecture || typeof architecture !== 'object') throw new Error('Architecture v1 is required.');
    if (!Array.isArray(tasks) || !tasks.length) throw new Error('At least one initial task is required.');
    const {components: normalizedComponents} = normalizeWebMcpArchitectureComponents(architecture.components || [], {requireIds: true});
    const normalizedPlanningTrace = normalizeInitialPlanningTrace(planningTrace, normalizedComponents);
    const result = await api(`/projects/${capture.projectId}/interactive-initial-architecture`, {
      method: 'POST',
      body: JSON.stringify({architecture: {...architecture, components: normalizedComponents}, tasks, planning_trace: normalizedPlanningTrace, reasoning: String(reasoning || '').trim()}),
    });
    await refreshWebMcpProject(capture);
    return {
      ...result,
      built_in_model_called: false,
      context: webMcpMutationContext(capture),
    };
  },

  async getContext() {
    await ensureAppInitialized();
    return webMcpContext();
  },

  async publishCodeArchitectureSnapshot({repository, revision, summary, components, relationships = [], sourceEvidence = [], signal} = {}) {
    await ensureAppInitialized();
    const capture = captureWebMcpProject();
    const result = await api(`/projects/${capture.projectId}/code-architecture/snapshots`, {
      method:'POST',
      body:JSON.stringify({
        repository,
        revision,
        summary,
        components,
        relationships,
        source_evidence:sourceEvidence,
      }),
      signal,
    });
    if (webMcpProjectStillCurrent(capture)) {
      state.codeArchitectureRequestSerial += 1;
      state.codeArchitecture = result;
      state.codeDiagram = normalizeCodeArchitectureSnapshot(result);
      if (state.currentView === 'architecture' && state.architectureGraphKind === 'code') render();
    }
    return result;
  },

  async getProjectBrief() {
    await ensureAppInitialized();
    const capture = captureWebMcpProject();
    await refreshWebMcpProject(capture);
    if (!webMcpProjectStillCurrent(capture)) throw supersededNavigationError();
    const summarizeTask = (task) => ({
      id: task.id,
      title: task.title,
      status: task.status,
      owner: task.owner,
      related_component: task.related_component,
    });
    const done = state.tasks.filter((task) => task.status === 'DONE');
    const inProgress = state.tasks.filter((task) => task.status === 'IN_PROGRESS');
    const blocked = state.tasks.filter((task) => task.status === 'BLOCKED');
    const ready = state.tasks.filter((task) => task.status === 'TODO');
    const pending = state.proposals.filter((proposal) => isProposalActionable(proposal, state.architecture));
    const recentActivity = [...(state.activity || [])].reverse().slice(0, 6).map((event) => ({
      source: event.payload?.external_source || event.source || 'SYSTEM',
      type: event.type,
      summary: event.payload?.summary || event.payload?.message || event.payload?.note || event.type,
    }));
    const architectureStatus = pending.length
      ? 'REVIEW_REQUIRED'
      : blocked.length
        ? 'BLOCKED'
        : inProgress.length
          ? 'ACTIVE'
          : 'ALIGNED';
    const recommendedFocus = pending.length
      ? {kind: 'proposal', id: pending[0].id}
      : blocked.length
        ? {kind: 'task', id: blocked[0].id}
        : null;
    return {
      project: {
        id: state.project.id,
        name: state.project.name,
        status: state.project.status,
        goal: state.project.goal,
      },
      architecture: {
        version: state.architecture.version,
        summary: state.architecture.summary,
        status: architectureStatus,
      },
      execution: {
        counts: {done: done.length, in_progress: inProgress.length, blocked: blocked.length, ready: ready.length},
        done: done.map(summarizeTask),
        in_progress: inProgress.map(summarizeTask),
        blocked: blocked.map(summarizeTask),
        ready: ready.map(summarizeTask),
      },
      recent_activity: recentActivity,
      attention: {
        required: pending.length > 0 || blocked.length > 0,
        pending_reviews: pending.map((proposal) => ({
          id: proposal.id,
          reason: proposal.reason,
          observed_change: proposal.observed_change,
          affected_components: proposal.affected_components || [],
          impact: proposal.impact,
        })),
        blockers: blocked.map(summarizeTask),
        recommended_next_tool: null,
        recommended_human_action: pending.length
          ? 'REVIEW_ARCHITECTURE_PROPOSAL'
          : blocked.length
            ? 'UNBLOCK_TASK'
            : null,
        recommended_focus: recommendedFocus,
      },
      latest_agent_result: state.lastRun,
    };
  },

  async getDecisionContext() {
    await ensureAppInitialized();
    webMcpRequireProject();
    const brief = await window.ArchBroWebBridge.getProjectBrief();
    const componentIds = [];
    const collectIds = (nodes) => {
      for (const node of nodes || []) {
        componentIds.push(node.id);
        collectIds(node.children || []);
      }
    };
    collectIds(state.architecture.components);
    return {
      project_brief: brief,
      architecture: state.architecture,
      tasks: state.tasks,
      recent_activity: [...(state.activity || [])].reverse().slice(0, 10),
      pending_reviews: state.proposals.filter((proposal) => isProposalActionable(proposal, state.architecture)),
      decision_contract: {
        provider: 'webmcp-agent',
        mode: 'interactive',
        allowed_recommendations: ['KEEP_CURRENT', 'ACCEPT_PROPOSED_CHANGE'],
        existing_component_ids: componentIds,
        rules: [
          'Base the recommendation on the provided project evidence and accepted architecture.',
          'Use KEEP_CURRENT when the issue can be resolved without changing an accepted architecture boundary.',
          'Use ACCEPT_PROPOSED_CHANGE only when evidence justifies a reviewable architecture change.',
          'Submitting a recommendation never approves the architecture change; the human review boundary remains authoritative.',
        ],
      },
    };
  },

  async submitAgentRecommendation({
    recommendation,
    reasoning,
    evidence = [],
    observedChange,
    affectedComponents = [],
    proposedChanges = [],
    impact = '',
    expectedArchitectureVersion,
  } = {}) {
    await ensureAppInitialized();
    const capture = captureWebMcpProject();
    const expected = Number(expectedArchitectureVersion);
    if (!Number.isInteger(expected) || expected < 0) throw new Error('expected_architecture_version must be a non-negative integer.');
    const result = await api(`/projects/${capture.projectId}/agent-recommendations`, {
      method: 'POST',
      body: JSON.stringify({
        recommendation,
        reasoning,
        evidence,
        observed_change: observedChange,
        affected_components: affectedComponents,
        proposed_changes: proposedChanges,
        impact,
        expected_architecture_version: expected,
      }),
    });
    await refreshWebMcpProject(capture);
    if (webMcpProjectStillCurrent(capture) && result?.proposal?.id) {
      state.selectedProposalId = result.proposal.id;
    }
    return {
      ...result,
      context: webMcpMutationContext(capture),
    };
  },

  async inspectProjectStatus() {
    webMcpRequireProject();
    const blockers = state.tasks.filter((task) => task.status === 'BLOCKED');
    const inProgress = state.tasks.filter((task) => task.status === 'IN_PROGRESS');
    const ready = state.tasks.filter((task) => task.status === 'TODO');
    const pending = state.proposals.filter((proposal) => isProposalActionable(proposal, state.architecture));
    return {
      project: state.project,
      architecture: state.architecture,
      tasks: state.tasks,
      blockers,
      in_progress: inProgress,
      ready_tasks: ready,
      pending_reviews: pending,
      latest_agent_result: state.lastRun,
    };
  },

  async getRecentActivity({limit = 10} = {}) {
    await ensureAppInitialized();
    const capture = captureWebMcpProject();
    const boundedLimit = Math.min(50, Math.max(1, Number(limit) || 10));
    const events = await api(`/projects/${capture.projectId}/events?limit=${boundedLimit}`);
    if (webMcpProjectStillCurrent(capture)) {
      state.activity = events;
      renderRecentActivity();
    }
    return {project_id: capture.projectId, events, latest_agent_result: webMcpProjectStillCurrent(capture) ? state.lastRun : null};
  },

  async focusPendingReview() {
    await ensureAppInitialized();
    webMcpRequireProject();
    const proposal = state.proposals.find((item) => isProposalActionable(item, state.architecture)) || null;
    if (!proposal) {
      return {focused: false, reason: 'no-pending-review', context: webMcpContext()};
    }
    switchWorkspaceTab('review', {focusedProposalId:proposal.id});
    return {focused: true, proposal, context: webMcpContext()};
  },

  async inspectArchitecture({componentId = null} = {}) {
    webMcpRequireProject();
    const pending = state.proposals.filter((proposal) => isProposalActionable(proposal, state.architecture));
    if (!componentId) {
      return {
        project_id: state.projectId,
        architecture: state.architecture,
        tasks: state.tasks,
        pending_proposals: pending,
      };
    }

    const node = findArchitectureNode(componentId);
    if (!node) throw new Error(`Architecture component not found: ${componentId}`);
    const ids = new Set(descendantArchitectureIds(node));
    return {
      project_id: state.projectId,
      architecture_version: state.architecture.version,
      node,
      health: architectureHealth(node),
      tasks: state.tasks.filter((task) => task.related_component && ids.has(task.related_component)),
      pending_proposals: pending.filter((proposal) => {
        const affected = proposal.affected_components || [];
        const changed = (proposal.proposed_changes || []).map((change) => change.component_id).filter(Boolean);
        return [...affected, ...changed].some((id) => ids.has(id));
      }),
    };
  },

  async focusItem({kind, id = null} = {}) {
    webMcpRequireProject();
    if (kind === 'project') {
      if (!(await activateProjectView(id || state.projectId, 'overview'))) {
        throw new Error(`Project could not be focused: ${id || state.projectId}`);
      }
    } else if (kind === 'task') {
      const task = state.tasks.find((item) => item.id === id);
      if (!task) throw new Error(`Task not found: ${id}`);
      switchView('tasks', {workspaceTab: 'tasks'});
      openTaskDetails(task.id, 'tasks');
    } else if (kind === 'architecture') {
      const node = findArchitectureNode(id);
      if (!node) throw new Error(`Architecture component not found: ${id}`);
      switchView('architecture');
      const parentScopeComponentId = findArchitectureParentId(node.id);
      await navigateGraphScope(parentScopeComponentId ?? null, {focusComponentId: node.id});
      if (diagramNodeByComponentId(node.id)) {
        state.selectedComponentId = node.id;
        state.graphFocusMode = 'connected';
        renderGraph();
      }
    } else if (kind === 'proposal') {
      const proposal = state.proposals.find((item) => item.id === id);
      if (!proposal) throw new Error(`Architecture proposal not found: ${id}`);
      if (!isProposalActionable(proposal, state.architecture)) throw new Error(`Architecture proposal is not actionable: ${id}`);
      switchWorkspaceTab('review', {focusedProposalId:proposal.id});
    } else {
      throw new Error(`Unsupported ArchBro focus kind: ${kind}`);
    }
    updateInstructionContext();
    return webMcpContext();
  },

  async reportChange({summary, evidence = [], relatedComponent = null} = {}) {
    webMcpRequireProject();
    const message = String(summary || '').trim();
    if (!message) throw new Error('Project change summary is required.');
    if (state.architecture.version === 0) throw new Error('Architecture v1 must exist before reporting project changes.');
    const normalizedEvidence = evidence.map((item) => String(item).trim()).filter(Boolean);
    const uiContext = {
      ...currentInstructionContext().payload,
      ...(relatedComponent ? {related_component: relatedComponent} : {}),
    };
    return sendEvent(
      'USER_MESSAGE',
      {message, evidence: normalizedEvidence, ui_context: uiContext},
      'Evaluating WebMCP project change…',
      {authority:'webmcp'},
    );
  },

  async createTask({
    requestId,
    title,
    description = '',
    owner = 'UNASSIGNED',
    relatedComponent = null,
    dependencies = [],
    acceptanceCriteria = [],
  } = {}) {
    await ensureAppInitialized();
    const capture = captureWebMcpProject();
    const result = await api(`/projects/${capture.projectId}/tasks`, {
      method: 'POST',
      body: JSON.stringify({
        request_id: requestId,
        title,
        description,
        owner,
        related_component: relatedComponent,
        dependencies,
        acceptance_criteria: acceptanceCriteria,
      }),
    });
    await refreshWebMcpProject(capture);
    return {...result, context: webMcpMutationContext(capture)};
  },

  async recordProjectObservation({
    summary,
    evidence = [],
    relatedComponents = [],
    relatedTaskId = null,
  } = {}) {
    await ensureAppInitialized();
    const capture = captureWebMcpProject();
    const result = await api(`/projects/${capture.projectId}/observations`, {
      method: 'POST',
      body: JSON.stringify({
        summary,
        evidence,
        related_components: relatedComponents,
        related_task_id: relatedTaskId,
      }),
    });
    await refreshWebMcpProject(capture);
    return {...result, context: webMcpMutationContext(capture)};
  },

  async updateTaskStatus({taskId, status} = {}) {
    await ensureAppInitialized();
    const capture = captureWebMcpProject();
    const task = state.tasks.find((item) => item.id === taskId);
    if (!task) throw new Error(`Task not found: ${taskId}`);
    if (status === 'IN_PROGRESS' && task.status !== 'TODO') {
      throw new Error(`Task ${taskId} must be TODO before starting.`);
    }
    if (status === 'DONE' && task.status !== 'IN_PROGRESS') {
      throw new Error(`Task ${taskId} must be IN_PROGRESS before completion.`);
    }
    if (!['IN_PROGRESS', 'DONE'].includes(status)) throw new Error(`Unsupported task status: ${status}`);
    const result = await api(`/projects/${capture.projectId}/tasks/${encodeURIComponent(taskId)}/status`, {
      method: 'PATCH',
      body: JSON.stringify({status}),
    });
    await refreshWebMcpProject(capture);
    return {...result, context: webMcpMutationContext(capture)};
  },

  async decideProposal({proposalId, decision} = {}) {
    webMcpRequireProject();
    const proposal = state.proposals.find((item) => item.id === proposalId);
    if (!proposal) throw new Error(`Architecture proposal not found: ${proposalId}`);
    if (!isProposalActionable(proposal, state.architecture)) throw new Error(`Architecture proposal ${proposalId} is not actionable against the current architecture.`);
    if (!['accept', 'reject'].includes(decision)) throw new Error(`Unsupported proposal decision: ${decision}`);
    return decideProposal(proposalId, decision);
  },
};

$('nav')?.addEventListener('click', (e) => {
  const btn = e.target.closest('[data-view]');
  if (btn) switchView(btn.dataset.view);
});
$('instructionForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  if (WEBMCP_AGENT_MODE) {
    toast('Built-in Agent messaging is disabled in WebMCP Agent Mode.', true);
    return;
  }
  if (state.architecture?.version === 0) {
    toast('Architecture v1 must finish before normal project updates.', true);
    return;
  }
  if (committedProjectGuardIsCurrent(state.instructionSubmission)) return;
  const submissionGuard = captureNavigationGuard();
  const input = $('instruction');
  const message = input.value.trim();
  if (!message) return;
  state.instructionSubmission = submissionGuard;
  try {
    state.lastInstruction = message;
    renderAgentConversationDialog();
    input.removeAttribute('aria-invalid');
    $('instructionError').textContent = '';
    const context = currentInstructionContext();
    const payload = {message, ui_context: context.payload};
    if (selectedAgentContextNode()) {
      const manifest = await ensureAgentContextManifest();
      if (!committedProjectGuardIsCurrent(submissionGuard)) return;
      if (!manifest) {
        input.setAttribute('aria-invalid', 'true');
        $('instructionError').textContent = 'Instruction not sent. The exact bounded context preview is unavailable; retry it in the Context Tray.';
        input.focus();
        return;
      }
      payload.agent_context_request = agentContextExecutionRequest(manifest);
    }
    const result = await sendEvent('USER_MESSAGE', payload);
    if (!committedProjectGuardIsCurrent(submissionGuard)) return;
    if (result?.result === 'SUCCESS') {
      if (input.value.trim() === message) {
        input.value = '';
        syncInstructionRainbowState();
        syncInstructionTextareaRows();
        renderAgentConversationDialog();
      }
    } else {
      input.setAttribute('aria-invalid', 'true');
      $('instructionError').textContent = 'Instruction not sent. Your text and context are still here—review and press Send to retry.';
      input.focus();
    }
  } finally {
    if (state.instructionSubmission === submissionGuard) state.instructionSubmission = null;
  }
});

$('agentContextPolicy')?.addEventListener('change', (event) => {
  const policy = event.currentTarget.value;
  if (!['ASK_ALL', 'ALLOW_NEIGHBORHOOD', 'AUTO_BOUNDED'].includes(policy)) return;
  state.agentContextPolicy = policy;
  clearAgentContextPreview();
  syncAgentContextPreview();
});

$('agentContextTelemetryToggle')?.addEventListener('click', () => {
  state.agentContextTelemetryVisible = !state.agentContextTelemetryVisible;
  renderAgentContextTray();
});

$('onboardingForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  await submitOnboardingAsk();
});

$('onboardingAsk').addEventListener('input', () => syncOnboardingAskRainbowState({activate: true}));
$('onboardingAsk').addEventListener('focus', syncOnboardingAskRainbowState);
$('onboardingAsk').addEventListener('blur', syncOnboardingAskRainbowState);
$('instruction').addEventListener('input', () => {
  syncInstructionTextareaRows();
  syncInstructionRainbowState({activate: true});
  renderAgentConversationDialog();
});
let instructionWidth = 0;
const instructionResizeObserver = new ResizeObserver(([entry]) => {
  if (entry.contentRect.width === instructionWidth) return;
  instructionWidth = entry.contentRect.width;
  syncInstructionTextareaRows();
});
instructionResizeObserver.observe($('instruction'));
$('instruction').addEventListener('keydown', (event) => {
  if (event.isComposing) return;
  if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
    event.preventDefault();
    $('instructionForm').requestSubmit();
  }
});
document.addEventListener('click', (event) => {
  const trigger = event.target.closest('[data-expand-agent-conversation]');
  if (trigger) openAgentConversation(trigger);
});
$('instruction').addEventListener('focus', () => {
  if (state.taskDetailId && !$('taskDetailPanel')?.hidden) closeTaskDetails({restoreFocus:false});
  syncInstructionRainbowState();
});
$('instruction').addEventListener('blur', syncInstructionRainbowState);

$('goalDraftText').addEventListener('input', () => {
  state.onboarding.initialGoal = $('goalDraftText').value;
  updateGoalConfirmState();
  if (!state.onboarding.draft) renderGoalDraft();
});
$('useGoalBtn').addEventListener('click', confirmGoalAndGenerate);
$('onboardingBackBtn').addEventListener('click', backToCurrentProject);
$('newProjectNameForm').addEventListener('submit', submitNewProjectName);
$('initialGoalForm').addEventListener('submit', continueToRefinement);
$('initialGoal').addEventListener('input', () => {
  state.onboarding.initialGoal = $('initialGoal').value;
  $('initialGoal').removeAttribute('aria-invalid');
  $('initialGoalError').textContent = '';
});
$('initialGoalBackBtn').addEventListener('click', returnToProjectName);
$('editOnboardingProjectName').addEventListener('click', returnToProjectName);
$('generateArchitectureBtn').addEventListener('click', generateInitialArchitecture);
$('newProjectBtn').addEventListener('click', () => {
  closeMobileSidebar();
  startOnboarding();
});
$('accountMcpConnectionsBtn').addEventListener('click', () => { closeTopMenus(); openMcpConnections(); });
document.querySelectorAll('[data-mcp-preset]').forEach((card) => card.addEventListener('click', async () => selectMcpPreset(card.dataset.mcpPreset)));
document.querySelectorAll('[data-mcp-tab]').forEach((button) => button.addEventListener('click', async () => {
  setMcpPickerTab(button.dataset.mcpTab);
  if (button.dataset.mcpTab === 'connected') {
    try { await loadMcpConnections(); } catch (err) { toast(err.message, true); }
  }
}));
$('mcpSearch').addEventListener('input', (event) => filterMcpProviders(event.target.value));
$('mcpTransport').addEventListener('change', syncMcpTransportFields);
$('mcpOAuthConnectBtn').addEventListener('click', startMcpOAuth);
$('mcpConnectionForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  try {
    await addMcpConnection();
  } catch (err) {
    toast(err.message, true);
  }
});
window.addEventListener('message', async (event) => {
  const trustedOAuthOrigins = new Set([window.location.origin, 'https://archbro-dev.magicdala.com', 'https://archbro.magicdala.com', 'https://archbro-webmcp.magicdala.com']);
  if (!trustedOAuthOrigins.has(event.origin)) return;
  if (activeMcpOAuthPopup && event.source && event.source !== activeMcpOAuthPopup) return;
  const payload = event.data;
  if (!payload || payload.type !== 'archbro-mcp-oauth') return;
  if (event.source) handledMcpOAuthPopups.add(event.source);
  const providerId = payload.provider || mcpOAuthProviderId();
  if (payload.ok) {
    toast(payload.message || 'MCP OAuth connection completed.');
    try {
      await completeMcpConnectionUi(providerId, payload.message || 'MCP OAuth connection completed.');
    } catch (err) {
      toast(err.message, true);
    }
  } else {
    toast(payload.message || 'MCP OAuth connection failed.', true);
    if (providerId) await refreshMcpProviderStatusAfterMutation(providerId);
  }
});
$('workspaceSwitcherBtn').addEventListener('click', openPersonalWorkspace);
$('workspaceHomeNewProjectBtn').addEventListener('click', () => {
  closeMobileSidebar();
  startOnboarding();
});
$('mobileSidebarBtn').addEventListener('click', openMobileSidebar);
$('sidebarBackdrop').addEventListener('click', closeMobileSidebar);
projectRepositoryController = createProjectRepositoryController({
  api,
  getProject: () => state.project,
  showDialog,
  async onSaved(projectId, value) {
    if (state.projectId !== projectId || state.project?.id !== projectId) return;
    state.project = {...state.project, source_repository: value.source_repository,
      repository_revision: value.repository_revision};
    workspaceContextGeneration += 1;
    state.codeDiagram = null;
    await loadProjects();
    if (state.projectId === projectId) await refresh();
  },
  onConnect(projectId, trigger) {
    const guard = captureNavigationGuard(projectId);
    $('mcpConnectionsDialog').addEventListener('close', () => {
      if (committedProjectGuardIsCurrent(guard) && state.project?.id === projectId) {
        void projectRepositoryController.open(state.project, trigger);
      }
    }, {once: true});
    openMcpConnections();
    selectMcpPreset('github-remote');
  },
});
$('projectRepositoryDialog').addEventListener('click', closeDialogOnBackdrop);
$('agentConversationDialog').addEventListener('click', closeDialogOnBackdrop);
$('editProjectForm').addEventListener('submit', async (e) => { e.preventDefault(); await saveProjectEdits(); });
$('deleteProjectForm').addEventListener('submit', async (e) => { e.preventDefault(); await deleteCurrentProject(); });
document.querySelectorAll('[data-close-dialog]').forEach((button) => button.addEventListener('click', () => $(button.dataset.closeDialog).close()));
document.querySelectorAll('dialog').forEach((dialog) => dialog.addEventListener('close', () => {
  const trigger = state.experience.dialogReturnFocus.get(dialog.id);
  state.experience.dialogReturnFocus.delete(dialog.id);
  if (dialog.id === 'authView') {
    if (state.experience.authClosingReturnFocus) (trigger || $('landingAuthTeaser')).focus();
    state.experience.authClosingReturnFocus = true;
    return;
  }
  trigger?.focus?.();
}));
$('authView').addEventListener('click', closeAuthDialogOnBackdrop);
$('authView').addEventListener('cancel', (event) => {
  event.preventDefault();
  closeAuthentication();
});
$('editProjectDialog').addEventListener('click', closeDialogOnBackdrop);
$('newProjectNameDialog').addEventListener('click', closeNewProjectNameDialogOnBackdrop);
$('newProjectNameDialog').addEventListener('cancel', handleNewProjectNameDialogCancel);
$('newProjectNameDialog').addEventListener('close', handleNewProjectNameDialogClose);
document.querySelectorAll('[data-new-project-name-cancel]').forEach((button) => button.addEventListener('click', cancelNewProjectNameDialog));
$('deleteProjectDialog').addEventListener('click', closeDialogOnBackdrop);
$('taskDetailClose').addEventListener('click', () => closeTaskDetails());
$('taskDetailAction').addEventListener('click', () => {
  const button = $('taskDetailAction');
  if (button.dataset.taskId && button.dataset.taskAction) updateTask(button.dataset.taskId, button.dataset.taskAction);
});
$('accountSettingsDialog').addEventListener('click', closeDialogOnBackdrop);
$('mcpConnectionsDialog').addEventListener('click', closeDialogOnBackdrop);
$('notificationBtn').addEventListener('click', () => toggleTopMenu('notificationBtn', 'notificationMenu'));
$('notificationCloseBtn').addEventListener('click', () => {
  closeTopMenus();
  $('notificationBtn').focus();
});
$('accountBtn').addEventListener('click', () => toggleTopMenu('accountBtn', 'accountMenu'));
$('accountMenu').addEventListener('keydown', (event) => {
  const items = [...$('accountMenu').querySelectorAll('[role="menuitem"]')];
  const current = items.indexOf(document.activeElement);
  let target = null;
  if (event.key === 'ArrowDown') target = items[(current + 1 + items.length) % items.length];
  if (event.key === 'ArrowUp') target = items[(current - 1 + items.length) % items.length];
  if (event.key === 'Home') target = items[0];
  if (event.key === 'End') target = items.at(-1);
  if (event.key === 'Escape') {
    event.preventDefault();
    event.stopPropagation();
    closeTopMenus();
    $('accountBtn').focus();
    return;
  }
  if (event.key === 'Tab') closeTopMenus();
  if (!target) return;
  event.preventDefault();
  target.focus();
});
document.querySelectorAll('[data-account-section]').forEach((button) => button.addEventListener('click', () => openAccountSection(button.dataset.accountSection)));
$('logoutBtn').addEventListener('click', logout);
$('accountSettingsForm').addEventListener('submit', saveAccountSettings);
document.querySelectorAll('[data-settings-panel]').forEach((button) => button.addEventListener('click', () => openAccountSection(button.dataset.settingsPanel)));
document.addEventListener('pointerdown', (event) => {
  const focusableOutsideTarget = event.target.closest('button, [href], input, textarea, select, summary, [tabindex]:not([tabindex="-1"])');
  if (state.openProjectMenuId && !event.target.closest('[data-project-menu], [data-project-menu-panel]')) {
    const keepFocusOnTrigger = !focusableOutsideTarget;
    closeProjectMenuAfterPointerEvent({returnFocus: keepFocusOnTrigger});
  }
  if (!event.target.closest('.top-menu, #notificationBtn, #accountBtn')) closeTopMenus();
});
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && $('newProjectNameDialog').open) {
    event.preventDefault();
    event.stopPropagation();
    cancelNewProjectNameDialog();
    return;
  }
  if (event.key === 'Escape' && document.querySelector('dialog[open]')) return;
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'n' && !event.target.closest('input, textarea, select, [contenteditable="true"]')) {
    event.preventDefault();
    closeMobileSidebar();
    startOnboarding();
    return;
  }
  if (event.key === 'Escape' && state.openProjectMenuId) {
    event.preventDefault();
    event.stopPropagation();
    closeProjectMenu({returnFocus: true});
    return;
  }
  if (event.key === 'Escape') {
    const openMenu = [['notificationBtn', 'notificationMenu'], ['accountBtn', 'accountMenu']]
      .find(([, menuId]) => !$(menuId).classList.contains('hidden'));
    if (openMenu) {
      event.preventDefault();
      closeTopMenus();
      $(openMenu[0]).focus();
      return;
    }
    if (mobileSidebarEnabled() && document.body.classList.contains('sidebar-open')) {
      event.preventDefault();
      closeMobileSidebar({returnFocus:true});
      return;
    }
    const taskPanelVisible = state.taskDetailId
      && !$('taskDetailPanel')?.hidden
      && !$('taskDetailPanel')?.classList.contains('hidden');
    if (taskPanelVisible) {
      event.preventDefault();
      closeTaskDetails();
    }
  }
});

$('landingLoginBtn').addEventListener('click', (event) => openAuthentication(event.currentTarget));
$('landingAuthTeaser').addEventListener('click', (event) => openAuthentication(event.currentTarget));
$('landingAuthSend').addEventListener('click', (event) => openAuthentication(event.currentTarget));
$('authForm').addEventListener('submit', submitAuthentication);
$('authModeToggle').addEventListener('click', () => setAuthMode(state.experience.authMode === 'signup' ? 'signin' : 'signup'));
$('authCloseBtn').addEventListener('click', () => closeAuthentication());
document.querySelectorAll('[data-password-target]').forEach((button) => button.addEventListener('click', () => togglePasswordVisibility(button)));
document.querySelectorAll('[data-auth-provider]').forEach((button) => button.addEventListener('click', async () => {
  const provider = button.dataset.authProvider;
  const providerSignIn = AUTH_PROVIDER_SIGN_INS.get(provider);
  if (!providerSignIn) {
    writeAuthMessage('This sign-in method is not supported.');
    return;
  }
  if (usesFirebaseAuthentication()) {
    writeAuthMessage('');
    setAuthenticationBusy(true);
    try {
      const identity = await providerSignIn();
      prototype.startSession(localStorage, identity);
      await routeAfterAuthentication();
    } catch (error) {
      writeAuthMessage(authenticationErrorMessage(error, {provider}));
    } finally {
      setAuthenticationBusy(false);
    }
    return;
  }
  prototype.startSession(localStorage, {
    provider,
    email: `${provider}@demo.archbro.local`,
    name: `${provider} user`,
  });
  await routeAfterAuthentication();
}));
wireLensRadioGroup(document.querySelector('.preference-grid'), selectProjectLens);
$('preferenceContinueBtn').addEventListener('click', completePreference);
$('authBackBtn').addEventListener('click', () => closeAuthentication());
$('bootstrapRetryBtn')?.addEventListener('click', retryWorkspaceRestore);
$('bootstrapLogoutBtn')?.addEventListener('click', logout);

wireGoButtons();
document.querySelectorAll('[data-workspace-tab]').forEach((button) => {
  button.addEventListener('click', () => switchWorkspaceTab(button.dataset.workspaceTab));
  button.addEventListener('keydown', handleWorkspaceTabKeydown);
});
document.querySelectorAll('[data-architecture-graph-kind]').forEach((button) => button.addEventListener('click', () => setArchitectureGraphKind(button.dataset.architectureGraphKind)));
$('architectureCanvasBtn')?.addEventListener('click', toggleArchitectureCanvas);
async function restoreNavigationFromLocation() {
  const route = readNavigationRoute(window.location, {useStorageFallback:false});
  // History owns location, but an unchanged graph still owns its pending data.
  const invalidateGraph = route.projectId !== state.projectId || !state.project
    || route.canvas !== ARCHITECTURE_CANVAS_MODE;
  const guard = beginNavigationTransition(route.projectId, {invalidateGraph});
  if (!route.projectId) {
    return openPersonalWorkspace({historyMode:'none', navigationGuard:guard});
  }
  if (route.projectId !== state.projectId || !state.project) {
    return selectProject(route.projectId, {
      view:route.view,
      canvas:route.canvas,
      historyMode:'none',
      navigationGuard:guard,
      route,
    });
  }
  if (route.canvas !== ARCHITECTURE_CANVAS_MODE) {
    return setArchitectureCanvasMode(route.canvas, {
      pushHistory:false,
      historyMode:'none',
      navigationGuard:guard,
      route,
    });
  }
  state.currentView = route.view;
  if (route.view === 'tasks') applyWorkspaceTabInvariants(route.workspaceTab);
  state.canvasDeepLinkApplied = false;
  state.canvasDeepLinkFocusPending = false;
  state.inspectorTab = route.inspectorTab;
  if (route.canvas && route.nodeId) {
    state.selectedComponentId = route.nodeId;
    state.selectedEdgeId = null;
    state.graphFocusMode = 'connected';
    state.canvasInspectorOpen = true;
  } else {
    // Popstate is an explicit navigation intent. A Canvas URL without `node`
    // means no selected component, not "keep whatever was selected before".
    state.selectedComponentId = null;
    state.selectedEdgeId = null;
    state.inspectorTab = 'overview';
    state.graphFocusMode = 'all';
    state.canvasInspectorOpen = false;
    clearArchitectureTracePath({render:false});
  }
  if (!commitNavigation(route, {historyMode:'none', guard})) return false;
  render();
  return true;
}

window.addEventListener('popstate', () => { void restoreNavigationFromLocation(); });
window.matchMedia('(max-width: 760px)').addEventListener('change', syncMobileSidebarLayers);

async function initializeWorkspace() {
  const initialRoute = readNavigationRoute(window.location, {useStorageFallback:true});
  const initialProjectId = initialRoute.projectId;
  const guard = beginNavigationTransition(initialProjectId);
  state.projectId = initialProjectId;
  ARCHITECTURE_CANVAS_MODE = initialRoute.canvas;
  syncArchitectureCanvasDomMode();
  try {
    const projectsPromise = loadProjects({guard})
      .then(() => ({error: null}))
      .catch((error) => ({error}));
    const startupContextRequest = initialProjectId ? beginWorkspaceContextRequest(initialProjectId) : null;
    const startupTicket = initialProjectId ? beginWorkspaceContext(state.workspaceAsync, initialProjectId) : null;
    const directProjectContextPromise = initialProjectId
      ? loadProjectCoreContext(initialProjectId)
          .then((context) => ({context, error: null}))
          .catch((error) => ({context: null, error}))
      : Promise.resolve(null);

    if (initialProjectId) {
      const direct = await directProjectContextPromise;
      if (direct?.context
          && navigationGenerationIsCurrent(guard)
          && workspaceContextIsCurrent(state.workspaceAsync, startupTicket)
          && isWorkspaceContextRequestCurrent(startupContextRequest)) {
        if (!bindWorkspaceContextArchitecture(state.workspaceAsync, startupTicket, direct.context.architecture?.version)) return false;
        state.onboarding.active = false;
        Object.assign(state, direct.context);
        clearWorkspaceOptionalData();
        clearAgentContextPreview();
        state.currentView = initialRoute.view;
        state.workspaceTab = initialRoute.view === 'tasks' ? initialRoute.workspaceTab : 'tasks';
        state.inspectorTab = initialRoute.canvas && initialRoute.nodeId ? initialRoute.inspectorTab : 'overview';
        state.selectedComponentId = initialRoute.canvas ? initialRoute.nodeId : null;
        state.selectedEdgeId = null;
        state.graphFocusMode = initialRoute.canvas && initialRoute.nodeId ? 'connected' : 'all';
        state.canvasInspectorOpen = Boolean(initialRoute.canvas && initialRoute.nodeId);
        state.canvasDeepLinkApplied = false;
        state.canvasDeepLinkFocusPending = false;
        if (state.currentView === 'tasks') applyWorkspaceTabInvariants(state.workspaceTab);
        if (!commitNavigation(initialRoute, {historyMode:'replace', guard})) return false;
        render();
        void refreshWorkspaceOptionalResources(startupTicket, direct.context.architecture, {
          deferredResources:direct.context.deferredArchitectureResources,
        });
        const committedGuard = captureNavigationGuard(initialProjectId);
        void projectsPromise.then((result) => {
          if (result.error && committedProjectGuardIsCurrent(committedGuard)) {
            toast(`Could not refresh the project list. ${result.error.message}`, true);
          }
        });
        return true;
      }
      if (direct?.error && !String(direct.error.message).startsWith('404:')) throw direct.error;
      if (initialRoute.explicitProject && direct?.error && String(direct.error.message).startsWith('404:')) {
        const projectsResult = await projectsPromise;
        if (projectsResult.error) throw projectsResult.error;
        if (!navigationGenerationIsCurrent(guard)) return false;
        await openPersonalWorkspace({historyMode:'none', navigationGuard:guard});
        toast('The project in this link is unavailable. No other project was opened instead.', true);
        return true;
      }
    }

    const projectsResult = await projectsPromise;
    if (projectsResult.error) throw projectsResult.error;
    if (!navigationGenerationIsCurrent(guard)) return false;

    if (initialProjectId && !state.projects.some((project) => project.id === initialProjectId)) {
      state.expandedProjectIds.delete(initialProjectId);
      persistExpandedProjectIds();
      if (state.projects.length) {
        state.expandedProjectIds.add(state.projects[0].id);
        return selectProject(state.projects[0].id, {historyMode:'replace'});
      }
    }
    if (initialProjectId) {
      return selectProject(initialProjectId, {
        view:initialRoute.view,
        canvas:initialRoute.canvas,
        historyMode:'replace',
        route:initialRoute,
      });
    }

    supersedeWorkspaceContextRequests();
    beginWorkspaceContext(state.workspaceAsync, null);
    state.workspaceAsync.phase = 'ready';
    state.workspaceAsync.architectureVersion = 0;
    state.projectId = null;
    state.onboarding.active = WEBMCP_AGENT_MODE;
    if (!commitNavigation({projectId:null, view:'overview', canvas:false, nodeId:null, inspectorTab:'overview'}, {historyMode:'replace', guard})) return false;
    if (WEBMCP_AGENT_MODE) {
      renderOnboarding();
    } else {
      await loadProjectSnapshots({guard});
      if (!navigationGenerationIsCurrent(guard)) return false;
      renderWorkspaceHome();
    }
    return true;
  } catch (err) {
    if (isSupersededNavigationError(err) || !navigationGenerationIsCurrent(guard)) return false;
    state.workspaceAsync.phase = 'failed';
    state.workspaceAsync.error = err?.message || String(err);
    toast(err.message, true);
    return false;
  }
}

async function initializeApp() {
  if (WEBMCP_AGENT_MODE && !usesFirebaseAuthentication()) {
    showExperience('restoring');
    state.experience.workspaceInitialized = await initializeWorkspace();
    if (state.experience.workspaceInitialized) showExperience('workspace');
    else showWorkspaceRecovery(new Error(state.workspaceAsync.error || 'Workspace could not be restored.'));
    syncMobileSidebarLayers();
    return state.experience.workspaceInitialized;
  }
  const hadStoredSession = localStorage.getItem(prototype.KEYS.session) !== null;
  localStorage.removeItem('archbro-pending-goal');
  if (usesFirebaseAuthentication()) {
    showExperience('restoring');
    let identity = null;
    try {
      identity = await restoreFirebaseIdentity();
    } catch (error) {
      prototype.endSession(localStorage);
      showExperience('landing');
      toast(authenticationErrorMessage(error), true);
      syncMobileSidebarLayers();
      return false;
    }
    if (!identity) {
      prototype.endSession(localStorage);
      showExperience('landing');
      if (WEBMCP_AGENT_MODE) openAuthentication($('landingLoginBtn'));
      syncMobileSidebarLayers();
      return true;
    }
    prototype.startSession(localStorage, identity);
    await routeAfterAuthentication();
    syncMobileSidebarLayers();
    return true;
  }
  const session = prototype.loadSession(localStorage);
  if (!session) {
    showExperience('landing');
    if (hadStoredSession) toast('That local demo session was invalid and has been cleared. Your projects are still available.', true);
    syncMobileSidebarLayers();
    return;
  }
  await routeAfterAuthentication();
  syncMobileSidebarLayers();
  return true;
}

let appInitializationPromise = null;
async function initialize() {
  document.body.dataset.webmcpAgentMode = WEBMCP_AGENT_MODE ? 'true' : 'false';
  document.body.classList.toggle('webmcp-agent-mode', WEBMCP_AGENT_MODE);
  syncArchitectureCanvasDomMode();
  return initializeApp();
}

appInitializationPromise = initialize();

async function ensureAppInitialized() {
  if (appInitializationPromise) await appInitializationPromise;
}
