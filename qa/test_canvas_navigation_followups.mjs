import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import test from 'node:test';
import {runInNewContext} from 'node:vm';

const app = (await readFile(new URL('../frontend/web/app.js', import.meta.url), 'utf8')).replace(/\r\n/g, '\n');
function source(signature) {
  const start = app.indexOf(signature);
  assert.ok(start >= 0, signature);
  return app.slice(start, app.indexOf('\n}', start) + 2);
}
function canvasHarness(route = {}) {
  const commits = [];
  const state = {
    projectId:'current', canvasDeepLinkApplied:false, canvasDeepLinkFocusPending:false,
    selectedComponentId:'current-node', selectedEdgeId:null, inspectorTab:'tasks',
    graphFocusMode:'isolate', canvasInspectorOpen:true, tracePathResult:{path:'retained'},
    collapsedNodeIds:new Set(),
    navigation:{committed:{canvas:true, projectId:'current', nodeId:'current-node', inspectorTab:'tasks', ...route}},
  };
  const context = {
    state, ARCHITECTURE_CANVAS_MODE:true,
    REQUESTED_ARCHITECTURE_NODE_ID:'startup-node', REQUESTED_INSPECTOR_TAB:'dependencies',
    INSPECTOR_TABS:new Set(['overview','dependencies','tasks']),
    clearArchitectureTracePath:()=>{state.tracePathResult=null;},
    captureNavigationGuard:()=>({projectId:state.projectId}),
    commitNavigation:(route, options)=>{commits.push({route,options}); state.navigation.committed=route; return true;},
  };
  const apply = runInNewContext(source('function applyCanvasNavigationToDiagram(') + ';applyCanvasNavigationToDiagram', context);
  const defaultDiagram={nodes:[{id:'node:startup-node',component_id:'startup-node',hierarchyPath:['node:startup-node']}, {id:'node:current-node',component_id:'current-node',hierarchyPath:['node:current-node']}]};
  return {state, commits, apply:(diagram=defaultDiagram)=>apply(diagram)};
}

test('Canvas applies the current committed node and inspector tab after later navigation', () => {
  const h=canvasHarness(); h.state.selectedComponentId=null; h.apply();
  assert.equal(h.state.selectedComponentId, 'current-node');
  assert.equal(h.state.inspectorTab, 'tasks');
  assert.equal(h.state.canvasDeepLinkFocusPending, true);
});
test('Explicit Canvas route without a node clears stale local selection', () => {
  const h=canvasHarness({nodeId:null,inspectorTab:'overview'}); h.state.selectedEdgeId='edge-current'; h.apply();
  assert.equal(h.state.selectedEdgeId, null);
  assert.equal(h.state.selectedComponentId, null);
  assert.equal(h.state.inspectorTab, 'overview');
  assert.equal(h.state.graphFocusMode, 'all');
  assert.equal(h.state.canvasInspectorOpen, false);
  assert.equal(h.state.tracePathResult, null);
  assert.equal(h.commits.length, 0);
});
test('Committed Canvas route expands collapsed ancestors before projection', () => {
  const h=canvasHarness({nodeId:'current-node',inspectorTab:'tasks'});
  h.state.collapsedNodeIds.add('node:group');
  h.apply({nodes:[
    {id:'node:group',component_id:'group',hierarchyPath:['node:group']},
    {id:'node:current-node',component_id:'current-node',hierarchyPath:['node:group','node:current-node']},
  ]});
  assert.equal(h.state.collapsedNodeIds.has('node:group'), false);
  assert.equal(h.state.selectedComponentId, 'current-node');
});
test('Canvas ignores navigation belonging to another project', () => {
  const h=canvasHarness({projectId:'stale-project'}); const before=JSON.stringify(h.state); h.apply();
  assert.equal(JSON.stringify(h.state), before);
});
test('Canvas normalizes a missing committed node once and clears stale disclosure', () => {
  const h=canvasHarness({nodeId:'missing-node'}); h.apply(); h.apply();
  assert.equal(h.state.selectedComponentId, null);
  assert.equal(h.state.canvasInspectorOpen, false);
  assert.equal(h.state.tracePathResult, null);
  assert.equal(h.state.inspectorTab, 'overview');
  assert.equal(h.commits.length, 1);
  assert.equal(h.commits[0].route.nodeId, null);
  assert.equal(h.commits[0].options.historyMode, 'replace');
});
test('Canvas rendering preserves selection already reconciled by a mode transition', () => {
  const h=canvasHarness(); h.state.canvasDeepLinkApplied=true;
  const before=JSON.stringify(h.state); h.apply();
  assert.equal(JSON.stringify(h.state), before);
});
test('Same-project explicit empty Canvas route clears local selection before commit', async () => {
  const start=app.indexOf('async function selectProject(');
  const end=app.indexOf('\nasync function refresh(', start);
  assert.ok(start >= 0 && end > start);
  const commits=[];
  const state={
    projectId:'current', project:{id:'current'}, openProjectMenuId:'current',
    onboarding:{active:false}, currentView:'architecture', selectedComponentId:'stale-node',
    selectedEdgeId:'stale-edge', inspectorTab:'tasks', canvasInspectorOpen:true,
    canvasDeepLinkApplied:true, canvasDeepLinkFocusPending:true, graphFocusMode:'isolate',
    tracePathResult:{path:'stale'},
  };
  const context={
    state, ARCHITECTURE_CANVAS_MODE:true, ROUTED_VIEWS:new Set(['overview','tasks','architecture']),
    beginNavigationTransition:projectId=>({projectId,generation:1}),
    navigationGenerationIsCurrent:()=>true,
    clearArchitectureTracePath:()=>{state.tracePathResult=null;},
    commitNavigation:(route,options)=>{commits.push({route,options});return true;},
    render:()=>{},
  };
  const selectProject=runInNewContext(app.slice(start,end)+';selectProject',context);
  assert.equal(await selectProject('current',{
    view:'architecture',canvas:true,historyMode:'none',
    route:{projectId:'current',view:'architecture',canvas:true,nodeId:null,inspectorTab:'overview'},
  }),true);
  assert.equal(state.selectedComponentId,null);
  assert.equal(state.selectedEdgeId,null);
  assert.equal(state.inspectorTab,'overview');
  assert.equal(state.canvasInspectorOpen,false);
  assert.equal(state.graphFocusMode,'all');
  assert.equal(state.tracePathResult,null);
  assert.equal(commits.length,1);
  assert.equal(commits[0].route.nodeId,null);
});
test('Collapsing a selected descendant promotes and commits the visible parent', () => {
  const sync=[];
  const state={collapsedNodeIds:new Set(),selectedComponentId:'child',selectedEdgeId:null};
  const parent={id:'node:group',component_id:'group',childCount:1};
  const child={component_id:'child',hierarchyPath:['node:group','node:child']};
  const context={
    state,ARCHITECTURE_CANVAS_MODE:true,
    diagramNodeById:id=>id===parent.id?parent:null,
    diagramNodeByComponentId:id=>id==='child'?child:null,
    syncArchitectureCanvasSelectionUrl:()=>sync.push(state.selectedComponentId),
    renderGraph:()=>{},
  };
  const toggle=runInNewContext(source('function toggleGraphNodeCollapse(')+';toggleGraphNodeCollapse',context);
  assert.equal(toggle('node:group'),true);
  assert.equal(state.selectedComponentId,'group');
  assert.equal(state.collapsedNodeIds.has('node:group'),true);
  assert.deepEqual(sync,['group']);
});
test('Living and Code graph switches preserve independent selections', () => {
  let renders=0,contexts=0;
  const state={architectureGraphKind:'living',selectedComponentId:'api',selectedCodeNodeId:'code-api',graphFocusMode:'connected'};
  const context={state,renderGraph:()=>{},updateInstructionContext:()=>{contexts+=1;}};
  const setKind=runInNewContext(source('function setArchitectureGraphKind(')+';setArchitectureGraphKind',context);
  assert.equal(setKind('code',{render:()=>{renders+=1;}}),true);
  assert.equal(state.selectedComponentId,'api');
  assert.equal(state.selectedCodeNodeId,'code-api');
  assert.equal(state.graphFocusMode,'connected');
  assert.equal(setKind('living',{render:()=>{renders+=1;}}),true);
  assert.equal(state.selectedComponentId,'api');
  assert.equal(state.selectedCodeNodeId,'code-api');
  assert.equal(renders,2); assert.equal(contexts,2);
});

async function scopeHarness({visibleComponent=false}={}) {
  const callbacks=[]; const focused=[];
  const state={projectId:'current', currentView:'architecture', architecture:{version:1}, readingMode:'MAP', scopeComponentId:null, workspaceAsync:{}, diagram:{nodes:[]}};
  let current=true;
  const context={
    state, ARCHITECTURE_CANVAS_MODE:false, CSS:{escape:value=>value},
    nextReadingModeForScope:()=> 'MAP', currentWorkspaceContextTicket:()=>({}),
    beginWorkspaceResource:()=>({}), beginGraphTransition:()=>({}),
    workspaceResourceIsCurrent:()=>true, graphTransitionIsCurrent:()=>current,
    cachedArchitectureView:()=>({nodes:[]}), cacheArchitectureView:()=>{},
    captureGraphInteractionState:()=>({inspectorTab:'tasks'}),
    reconcileGraphInteractionState:()=>({selectedComponentId:null}),
    diagramNodeByComponentId:()=>null, settleWorkspaceResource:()=>{},
    setTimeout:callback=>callbacks.push(callback),
    document:{querySelector:selector=>selector==='[data-graph-back]'
      ? {focus:()=>focused.push('back')}
      : visibleComponent ? {focus:()=>focused.push('component')} : null},
  };
  const navigate=runInNewContext(source('async function navigateGraphScope(')+';navigateGraphScope',context);
  assert.equal(await navigate('scope-root', {focusComponentId:'scope-root', loader:async()=>({}), render:()=>{}, notify:()=>{}}),true);
  return {focused, flush:()=>callbacks.forEach(callback=>callback()), supersede:()=>{current=false;}, leave:()=>{state.currentView='tasks';}};
}
test('Scoped navigation focuses Back when the projected root is omitted', async () => {
  const h=await scopeHarness(); h.flush(); assert.deepEqual(h.focused,['back']);
});
test('Scoped navigation focuses a component when it remains visible', async () => {
  const h=await scopeHarness({visibleComponent:true}); h.flush(); assert.deepEqual(h.focused,['component']);
});
test('A superseded scope transition cannot steal keyboard focus', async () => {
  const h=await scopeHarness(); h.supersede(); h.flush(); assert.deepEqual(h.focused,[]);
});

test('A completed scope cannot steal focus after leaving Architecture', async () => {
  const h=await scopeHarness(); h.leave(); h.flush(); assert.deepEqual(h.focused,[]);
});
