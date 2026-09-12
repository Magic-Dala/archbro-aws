import assert from 'node:assert/strict';
import test from 'node:test';
import {readFile} from 'node:fs/promises';
import {runInNewContext} from 'node:vm';

const app = (await readFile(new URL('../frontend/web/app.js', import.meta.url), 'utf8')).replace(/\r\n/g, '\n');

function section(startSignature, endSignature) {
  const start = app.indexOf(startSignature);
  const end = app.indexOf(endSignature, start);
  assert.ok(start >= 0, `missing ${startSignature}`);
  assert.ok(end > start, `missing ${endSignature} after ${startSignature}`);
  return app.slice(start, end);
}

function activateHarness({projectId = 'A', hasProject = true} = {}) {
  const state = {projectId, project: hasProject ? {id:projectId} : null};
  const selected = [];
  const switched = [];
  const context = {
    state,
    ROUTED_VIEWS:new Set(['overview','architecture','tasks']),
    selectProject:async (nextProjectId, options) => {
      selected.push({projectId:nextProjectId, options});
      state.projectId = nextProjectId;
      state.project = {id:nextProjectId};
      return true;
    },
    switchView:(view, options) => {
      switched.push({view, options});
      return true;
    },
  };
  const activate = runInNewContext(
    `${section('async function activateProjectView(', 'function wireProjectTree(')};activateProjectView`,
    context,
  );
  return {state, selected, switched, activate};
}

test('ordinary view navigation advances URL ownership without invalidating a current graph request', () => {
  const state = {
    projectId:'A',
    navigation:{generation:7},
    graphTransitionGeneration:11,
  };
  let syncs = 0;
  const begin = runInNewContext(
    `${section('function beginNavigationTransition(', 'function captureNavigationGuard(')};beginNavigationTransition`,
    {state, syncWorkingRequestUI:()=>{syncs+=1;}},
  );

  const capturedGraphGeneration = state.graphTransitionGeneration;
  const viewGuard = begin('A', {invalidateGraph:false});
  assert.equal(viewGuard.generation, 8);
  assert.equal(state.graphTransitionGeneration, capturedGraphGeneration);

  const projectGuard = begin('B');
  assert.equal(projectGuard.generation, 9);
  assert.equal(state.graphTransitionGeneration, capturedGraphGeneration + 1);
  assert.equal(syncs, 2);
});

test('cross-project child-view activation selects the project directly into the requested view', async () => {
  const harness = activateHarness();
  assert.equal(await harness.activate('B', 'tasks'), true);
  assert.deepEqual(JSON.parse(JSON.stringify(harness.selected)), [
    {projectId:'B', options:{view:'tasks', historyMode:'push'}},
  ]);
  assert.deepEqual(harness.switched, []);
});

test('same-project child-view activation performs one view transition and no project reload', async () => {
  const harness = activateHarness({projectId:'B'});
  assert.equal(await harness.activate('B', 'architecture', {historyMode:'replace'}), true);
  assert.deepEqual(harness.selected, []);
  assert.deepEqual(JSON.parse(JSON.stringify(harness.switched)), [
    {view:'architecture', options:{historyMode:'replace'}},
  ]);
});

test('repeated A/B project-view switches cannot supersede each newly launched map projection', async () => {
  const state = {projectId:'A', project:{id:'A'}};
  let graphGeneration = 0;
  const mapResults = [];
  const activate = runInNewContext(
    `${section('async function activateProjectView(', 'function wireProjectTree(')};activateProjectView`,
    {
      state,
      ROUTED_VIEWS:new Set(['overview','architecture','tasks']),
      selectProject:async (projectId) => {
        const requestGeneration = ++graphGeneration;
        state.projectId = projectId;
        state.project = {id:projectId};
        mapResults.push(() => requestGeneration === graphGeneration);
        return true;
      },
      // This models the old second transition: any call here would supersede
      // the map projection just launched by selectProject.
      switchView:() => {
        graphGeneration += 1;
        return true;
      },
    },
  );

  for (let index = 0; index < 10; index += 1) {
    const target = state.projectId === 'A' ? 'B' : 'A';
    assert.equal(await activate(target, 'overview'), true);
    assert.equal(mapResults.at(-1)(), true);
  }
  assert.equal(graphGeneration, 10);
});

test('project tree, workspace tabs, normal views, and WebMCP focus use one navigation authority', () => {
  const tree = section('function wireProjectTree(', 'function normalizeDiagramGraph(');
  assert.match(tree, /activateProjectView\(projectId, 'overview'\)/);
  assert.match(tree, /activateProjectView\(projectId, button\.dataset\.projectView\)/);
  assert.doesNotMatch(tree, /await selectProject\(projectId\)[\s\S]{0,120}switchView\(/);

  const tab = section('function switchWorkspaceTab(', 'function handleWorkspaceTabKeydown(');
  assert.match(tab, /beginNavigationTransition\(state\.projectId, \{invalidateGraph:false\}\)/);

  const view = section('function switchView(', 'function goTargetOptions(');
  assert.match(view, /const invalidateGraph = state\.currentView === 'architecture' && name !== 'architecture'/);
  assert.match(view, /beginNavigationTransition\(state\.projectId, \{invalidateGraph\}\)/);

  const focus = section('async focusItem(', 'async reportChange(');
  assert.match(focus, /await activateProjectView\(id \|\| state\.projectId, 'overview'\)/);
  assert.doesNotMatch(focus, /await selectProject\(id\)[\s\S]{0,100}switchView\('overview'\)/);
});
