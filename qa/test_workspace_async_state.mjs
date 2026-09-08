import assert from 'node:assert/strict';
import test from 'node:test';
import { readFile } from 'node:fs/promises';
import { runInNewContext } from 'node:vm';

const webRoot = new URL('../frontend/web/', import.meta.url);
const app = await readFile(new URL('app.js', webRoot), 'utf8');

function markedBlock(start, end) {
  const first = app.indexOf(start);
  const last = app.indexOf(end);
  assert.notEqual(first, -1, `missing ${start}`);
  assert.notEqual(last, -1, `missing ${end}`);
  assert.ok(last > first, `${end} must follow ${start}`);
  return app.slice(first + start.length, last);
}

test('workspace and optional resources use independent generations', () => {
  const block = markedBlock('// WORKSPACE_ASYNC_STATE_START', '// WORKSPACE_ASYNC_STATE_END');
  const result = runInNewContext(`${block}
    const asyncState = makeWorkspaceAsyncState('A');
    const a = beginWorkspaceContext(asyncState, 'A');
    bindWorkspaceContextArchitecture(asyncState, a, 3);
    const aCanvas = beginWorkspaceResource(asyncState, 'canvas', a, {retainData:false});
    const b = beginWorkspaceContext(asyncState, 'B');
    bindWorkspaceContextArchitecture(asyncState, b, 8);
    const bCanvas = beginWorkspaceResource(asyncState, 'canvas', b, {retainData:false});
    ({
      staleA: workspaceResourceIsCurrent(asyncState, aCanvas),
      currentB: workspaceResourceIsCurrent(asyncState, bCanvas),
      settleA: settleWorkspaceResource(asyncState, aCanvas, 'ready'),
      settleB: settleWorkspaceResource(asyncState, bCanvas, 'ready'),
      phase: asyncState.phase,
      projectId: asyncState.projectId,
      canvasStatus: asyncState.resources.canvas.status,
    });
  `);
  assert.deepEqual(JSON.parse(JSON.stringify(result)), {
    staleA: false,
    currentB: true,
    settleA: false,
    settleB: true,
    phase: 'ready',
    projectId: 'B',
    canvasStatus: 'ready',
  });
});

test('same-context refresh keeps last good optional data while marking refreshing', () => {
  const block = markedBlock('// WORKSPACE_ASYNC_STATE_START', '// WORKSPACE_ASYNC_STATE_END');
  const result = runInNewContext(`${block}
    const asyncState = makeWorkspaceAsyncState('A');
    const first = beginWorkspaceContext(asyncState, 'A');
    bindWorkspaceContextArchitecture(asyncState, first, 3);
    const initial = beginWorkspaceResource(asyncState, 'codeArchitecture', first, {retainData:false});
    settleWorkspaceResource(asyncState, initial, 'ready');
    const refresh = beginWorkspaceContext(asyncState, 'A');
    bindWorkspaceContextArchitecture(asyncState, refresh, 3);
    const next = beginWorkspaceResource(asyncState, 'codeArchitecture', refresh, {retainData:true});
    ({status:asyncState.resources.codeArchitecture.status, refreshing:asyncState.resources.codeArchitecture.refreshing,
      current:workspaceResourceIsCurrent(asyncState,next)});
  `);
  assert.deepEqual(JSON.parse(JSON.stringify(result)), {status:'ready', refreshing:true, current:true});
});

test('core project load does not depend on Canvas or Code Truth', async () => {
  const block = markedBlock('// WORKSPACE_CORE_LOADER_START', '// WORKSPACE_CORE_LOADER_END');
  const calls = [];
  const result = await runInNewContext(`${block}; loadProjectCoreContext('project-A')`, {
    api: async (path) => {
      calls.push(path);
      if (path.includes('code-architecture') || path.includes('architecture/canvas')) throw new Error('optional resource should not be in core load');
      if (path.endsWith('/architecture')) return {version:3, components:[]};
      if (path.endsWith('/tasks') || path.endsWith('/architecture/proposals') || path.includes('/events?')) return [];
      return {id:'project-A'};
    },
  });
  assert.equal(result.project.id, 'project-A');
  assert.equal(result.architecture.version, 3);
  assert.equal(calls.some((path) => path.includes('code-architecture')), false);
  assert.equal(calls.some((path) => path.includes('architecture/canvas')), false);
});

test('failed workspace restoration exposes explicit recovery instead of leaving the restoring screen', () => {
  const block = markedBlock('// WORKSPACE_RECOVERY_START', '// WORKSPACE_RECOVERY_END');
  const elements = {
    bootstrapRecoveryActions: {classList:{removed:false, add(){this.removed=false;}, remove(){this.removed=true;}}},
    bootstrapExperienceMessage: {textContent:''},
  };
  const state = {experience:{workspaceError:null}};
  const phases = [];
  runInNewContext(`${block}; showWorkspaceRecovery(new Error('backend down'));`, {
    state,
    $: (id) => elements[id] || null,
    showExperience: (phase) => phases.push(phase),
  });
  assert.deepEqual(phases, ['failed']);
  assert.equal(state.experience.workspaceError, 'backend down');
  assert.equal(elements.bootstrapRecoveryActions.classList.removed, true);
  assert.match(elements.bootstrapExperienceMessage.textContent, /backend down/);
});

test('architecture interaction rollback clones Sets and leaving Canvas removes stale deep-link state', () => {
  const block = markedBlock('// ARCHITECTURE_INTERACTION_STATE_START', '// ARCHITECTURE_INTERACTION_STATE_END');
  const result = runInNewContext(`${block}
    const source={currentView:'architecture',scopeComponentId:'api',selectedComponentId:'api',selectedEdgeId:'edge-1',
      inspectorTab:'tasks',graphFocusMode:'connected',collapsedNodeIds:new Set(['group']),canvasDeepLinkApplied:true,
      canvasDeepLinkFocusPending:true,diagram:{id:'diagram'},diagramError:null};
    const snapshot=snapshotArchitectureInteractionState(source);
    source.collapsedNodeIds.add('late');
    const url=new URL('https://example.test/?project=p&canvas=architecture&node=api&tab=tasks');
    applyArchitectureNavigationToUrl(url,{enabled:false,projectId:'p',selectedComponentId:null,inspectorTab:'overview'});
    ({collapsed:[...snapshot.collapsedNodeIds], url:url.toString()});
  `, {URL});
  assert.deepEqual(JSON.parse(JSON.stringify(result.collapsed)), ['group']);
  assert.equal(new URL(result.url).searchParams.has('canvas'), false);
  assert.equal(new URL(result.url).searchParams.has('node'), false);
  assert.equal(new URL(result.url).searchParams.has('tab'), false);
});

test('pan updates are frame-coalesced and skip scale-dependent density rewrites', () => {
  const block = markedBlock('// GRAPH_PAN_SCHEDULER_START', '// GRAPH_PAN_SCHEDULER_END');
  const callbacks = [];
  const updates = [];
  const graphViewport = {panFrame:null, pendingPan:null};
  runInNewContext(`${block}
    scheduleGraphPan({}, {x:1,y:2,width:100,height:80});
    scheduleGraphPan({}, {x:3,y:4,width:100,height:80});
  `, {
    graphViewport,
    requestAnimationFrame: (callback) => { callbacks.push(callback); return callbacks.length; },
    setGraphViewportBox: (...args) => updates.push(args),
  });
  assert.equal(callbacks.length, 1);
  callbacks[0]();
  assert.equal(updates.length, 1);
  assert.deepEqual(JSON.parse(JSON.stringify(updates[0][1])), {x:3,y:4,width:100,height:80});
  assert.deepEqual(JSON.parse(JSON.stringify(updates[0][2])), {updateDensity:false});
});

test('graph navigation uses the canvas resource generation gate', () => {
  const scopeStart = app.indexOf('async function navigateGraphScope(');
  const modeStart = app.indexOf('async function setGraphReadingMode(');
  const modeEnd = app.indexOf('async function activateGraphNode(');
  assert.notEqual(scopeStart, -1);
  assert.notEqual(modeStart, -1);
  assert.notEqual(modeEnd, -1);
  const scope = app.slice(scopeStart, modeStart);
  const mode = app.slice(modeStart, modeEnd);
  for (const section of [scope, mode]) {
    assert.match(section, /beginWorkspaceResource\(state\.workspaceAsync, 'canvas'/);
    assert.match(section, /workspaceResourceIsCurrent\(state\.workspaceAsync, request\)/);
    assert.match(section, /settleWorkspaceResource\(state\.workspaceAsync, request, 'ready'\)/);
    assert.match(section, /settleWorkspaceResource\(state\.workspaceAsync, request, 'error'/);
  }
});

test('workspace recovery and optional-resource retry controls are wired into the real UI', async () => {
  const page = await readFile(new URL('index.html', webRoot), 'utf8');
  for (const id of ['bootstrapExperienceMessage','bootstrapRecoveryActions','bootstrapRetryBtn','bootstrapLogoutBtn']) {
    assert.match(page, new RegExp(`id="${id}"`));
  }
  assert.match(app, /bootstrapRetryBtn'\)\?\.addEventListener\('click', retryWorkspaceRestore\)/);
  assert.match(app, /bootstrapLogoutBtn'\)\?\.addEventListener\('click', logout\)/);
  assert.match(app, /data-retry-workspace-resource="canvas"/);
  assert.match(app, /data-retry-workspace-resource="codeArchitecture"/);
  assert.match(app, /resource\.refreshing \? ' · refreshing' : ''/);
  const controls = app.slice(app.indexOf('function graphViewportControlsMarkup()'), app.indexOf('function graphViewportWithAspect('));
  assert.match(controls, /resource\?\.status === 'error'/);
  assert.match(controls, /graph-resource-tools/);
  assert.match(controls, /data-retry-workspace-resource/);
});
