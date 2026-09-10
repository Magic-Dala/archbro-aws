import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {runInNewContext} from 'node:vm';

const appUrl = new URL('../frontend/web/app.js', import.meta.url);
const appSource = await readFile(appUrl, 'utf8');

function sourceBetween(start, end) {
  const a = appSource.indexOf(start);
  const b = appSource.indexOf(end, a + start.length);
  assert.notEqual(a, -1, `Missing source boundary: ${start}`);
  assert.notEqual(b, -1, `Missing source boundary: ${end}`);
  return appSource.slice(a, b);
}

const asyncHelpers = sourceBetween('// WORKSPACE_ASYNC_STATE_START', '// WORKSPACE_ASYNC_STATE_END');
const requestHelpers = sourceBetween('let workspaceContextGeneration = 0;', 'if (initialProjectId) state.expandedProjectIds.add(initialProjectId);');
const graphHelpers = sourceBetween('const architectureViewCache = new Map();', 'function showExperience(');

function baseGraphState(overrides = {}) {
  return {
    projectId:'project-a',
    architecture:{version:7,components:[{id:'api'}]},
    scopeComponentId:null,
    readingMode:'MAP',
    selectedComponentId:null,
    selectedEdgeId:null,
    inspectorTab:'overview',
    graphFocusMode:'all',
    tracePathRequest:null,
    tracePathResult:null,
    tracePathLoading:false,
    tracePathError:null,
    graphTransitionGeneration:0,
    ...overrides,
  };
}

test('graph detail transitions reject stale completions and cache exact projection identity', () => {
  const state=baseGraphState();
  const context={state,ARCHITECTURE_CANVAS_MODE:false,clearArchitectureTracePath(){}};
  runInNewContext(`${graphHelpers};globalThis.cache=cacheArchitectureView;globalThis.cached=cachedArchitectureView;globalThis.begin=beginGraphTransition;globalThis.current=graphTransitionIsCurrent;globalThis.guard=captureGraphProjectionGuard;globalThis.matches=graphProjectionMatchesCommittedState;`,context);
  const a={id:'scope-a-map'}, b={id:'scope-b-map'}, c={id:'scope-a-full'}, canvas={id:'canvas-full'};
  context.cache('project','project-a',7,'scope-a','MAP',a);
  context.cache('project','project-a',7,'scope-b','MAP',b);
  context.cache('project','project-a',7,'scope-a','FULL',c);
  context.cache('canvas','project-a',7,null,'FULL',canvas);
  assert.equal(context.cached('project','project-a',7,'scope-a','MAP'),a);
  assert.equal(context.cached('project','project-a',7,'scope-b','MAP'),b);
  assert.equal(context.cached('project','project-a',7,'scope-a','FULL'),c);
  assert.equal(context.cached('canvas','project-a',7,null,'FULL'),canvas);
  assert.equal(context.cached('project','project-a',7,'scope-b','FULL'),null);
  const first=context.begin({surface:'project',scopeComponentId:'scope-a',readingMode:'MAP'});
  const second=context.begin({surface:'project',scopeComponentId:'scope-b',readingMode:'READ'});
  assert.equal(context.current(first),false);
  assert.equal(context.current(second),true);
  state.scopeComponentId='scope-b'; state.readingMode='READ';
  assert.equal(context.matches(second),true);
  state.readingMode='FULL';
  assert.equal(context.matches(second),false);
});

test('graph interaction reconciliation preserves valid selection and clears invalid trace state', () => {
  let clears=0;
  const state=baseGraphState({
    selectedComponentId:'api', inspectorTab:'tasks', graphFocusMode:'connected',
    tracePathRequest:{source_id:'node:api',target_id:'node:db',expected_architecture_version:7},
    tracePathResult:{status:'FOUND'},
  });
  const context={state,ARCHITECTURE_CANVAS_MODE:false,clearArchitectureTracePath(){clears+=1;state.tracePathRequest=null;state.tracePathResult=null;}};
  runInNewContext(`${graphHelpers};globalThis.capture=captureGraphInteractionState;globalThis.reconcile=reconcileGraphInteractionState;`,context);
  const prior=context.capture();
  const good={architectureVersion:7,nodes:[{id:'node:api',component_id:'api'},{id:'node:db',component_id:'db'}],edges:[]};
  assert.deepEqual(JSON.parse(JSON.stringify(context.reconcile(good,prior))),{selectedComponentId:'api',selectedEdgeId:null,traceValid:true});
  assert.equal(state.inspectorTab,'tasks'); assert.equal(clears,0);
  const stale={architectureVersion:8,nodes:[{id:'node:other',component_id:'other'}],edges:[]};
  const result=context.reconcile(stale,prior);
  assert.equal(result.selectedComponentId,null); assert.equal(result.traceValid,false);
  assert.equal(state.inspectorTab,'overview'); assert.equal(state.graphFocusMode,'all'); assert.equal(clears,1);
});

test('shared component reveal keeps Canvas local and Project scoped', async () => {
  const fn=sourceBetween('async function revealArchitectureComponent(', 'function graphBreadcrumbMarkup(');
  const state={currentView:'architecture',scopeComponentId:'scope-a',collapsedNodeIds:new Set(['group']),selectedComponentId:null,selectedEdgeId:null,inspectorTab:'overview',graphFocusMode:'all',canvasInspectorOpen:false};
  let canvasMode=true, navigateCalls=[];
  const target={id:'child',name:'Child'};
  const projected={component_id:'child',hierarchyPath:['group','node:child']};
  const context={
    state,
    get ARCHITECTURE_CANVAS_MODE(){return canvasMode;}, set ARCHITECTURE_CANVAS_MODE(v){canvasMode=v;},
    findArchitectureNode:id=>id==='child'?target:{id,name:id}, diagramNodeByComponentId:id=>id==='child'?projected:null,
    findArchitectureParentId:()=> 'parent', clearArchitectureTracePath(){}, syncArchitectureCanvasSelectionUrl(){}, renderGraph(){},
    switchView(){}, toast(){}, setArchitectureCanvasMode:async enabled=>{canvasMode=enabled;return true;},
    navigateGraphScope:async(scope)=>{navigateCalls.push(scope);return true;},
    requestAnimationFrame:fn=>fn(), setTimeout:fn=>fn(), focusGraphNodeInViewport(){}, CSS:{escape:s=>s},
    document:{querySelector(){return null;}}, console,
  };
  runInNewContext(`${fn};globalThis.reveal=revealArchitectureComponent;`,context);
  assert.equal(await context.reveal('child',{surface:'canvas'}),true);
  assert.equal(state.scopeComponentId,'scope-a'); assert.equal(navigateCalls.length,0);
  assert.equal(state.collapsedNodeIds.has('group'),false); assert.equal(state.selectedComponentId,'child');
  canvasMode=false; state.selectedComponentId=null;
  assert.equal(await context.reveal('child',{surface:'project'}),true);
  assert.deepEqual(navigateCalls,['parent']); assert.equal(state.selectedComponentId,'child');
  assert.match(appSource,/data-component-picker/); assert.match(appSource,/event\.code !== 'Space'/); assert.match(appSource,/scheduleGraphPan\(svg/);
});

test('component dependency disclosure separates projected facts from canonical cross-boundary facts', () => {
  const fn=sourceBetween('function canonicalBoundaryRelationshipsForComponent(', 'function renderSelectedNode()');
  const canonical={id:'api',name:'API'};
  const nodes={api:canonical,db:{id:'db',name:'DB'},queue:{id:'queue',name:'Queue'}};
  const context={
    state:{diagram:null},
    findArchitectureNode:id=>nodes[id]||{id,name:id},
    descendantArchitectureIds:()=>['api','child'],
    escapeHtml:value=>String(value),
  };
  runInNewContext(`${fn};globalThis.boundaries=canonicalBoundaryRelationshipsForComponent;globalThis.markup=canonicalBoundaryRelationshipMarkup;`,context);
  const diagram={fullCanvas:false,edges:[{provenance:[{relationship_id:'shown'}]}],scope:{directRelationships:[
    {relationship_id:'shown',source_component_id:'api',target_component_id:'db',semantic_type:'READS'},
    {relationship_id:'boundary-out',source_component_id:'api',target_component_id:'queue',semantic_type:'PUBLISHES'},
    {relationship_id:'boundary-in',source_component_id:'db',target_component_id:'child',semantic_type:'CALLS'},
    {relationship_id:'internal',source_component_id:'api',target_component_id:'child',semantic_type:'USES'},
  ]}};
  const result=context.boundaries('api',diagram);
  assert.deepEqual(result.map(x=>x.relationship_id),['boundary-out','boundary-in']);
  const markup=context.markup(result[0],'api');
  assert.match(markup,/data-reveal-component="queue"/); assert.match(markup,/canonical cross-boundary/);
});

async function makeRefreshHarness() {
  const refreshSource=sourceBetween('async function refresh({projectId = state.projectId', 'function startOnboarding()');
  const state={projectId:'project-a',project:{id:'project-a'},projects:[{id:'project-a'}],onboarding:{active:false},architecture:{version:7},projectContextRequestSerial:0,scopeComponentId:'scope-a'};
  const resolvers=[]; let invalidations=0, optionalCalls=0;
  const context={
    state,
    navigationGenerationIsCurrent:()=>true,captureNavigationGuard:()=>({}),loadProjectCoreContext:()=>new Promise((resolve,reject)=>resolvers.push({resolve,reject})),
    loadProjectSnapshots:async()=>{},renderWorkspaceHome(){},clearWorkspaceOptionalData(){},clearAgentContextPreview(){},render(){},
    invalidateArchitectureViewCache(){invalidations+=1;},refreshWorkspaceOptionalResources(){optionalCalls+=1;return Promise.resolve([]);},
    openPersonalWorkspace:async()=>true,toast(){},restoreDisplayedWorkspaceAsync(){},
  };
  runInNewContext(`${asyncHelpers}\n${requestHelpers}\n${refreshSource};state.workspaceAsync=makeWorkspaceAsyncState('project-a');const initial=beginWorkspaceContext(state.workspaceAsync,'project-a');bindWorkspaceContextArchitecture(state.workspaceAsync,initial,7);globalThis.refresh=refresh;`,context);
  return {context,state,resolvers,get invalidations(){return invalidations;},get optionalCalls(){return optionalCalls;}};
}

test('newer same-project refresh owns project context and projection cache when responses finish out of order', async () => {
  const h=await makeRefreshHarness();
  const first=h.context.refresh(); const second=h.context.refresh();
  assert.equal(h.resolvers.length,2);
  h.resolvers[1].resolve({project:{id:'project-a',stamp:'new'},architecture:{version:7},tasks:[]});
  assert.equal(await second,true);
  h.resolvers[0].resolve({project:{id:'project-a',stamp:'old'},architecture:{version:7},tasks:[]});
  assert.equal(await first,false);
  assert.equal(h.state.project.stamp,'new'); assert.equal(h.invalidations,1); assert.equal(h.optionalCalls,1);
});

function makeOptionalHarness({canvas=true,readingMode='READ',scope='remembered'}={}) {
  const refreshCanvas=sourceBetween('async function refreshCanvasResource(', 'async function refreshCodeArchitectureResource(');
  const refreshProject=sourceBetween('async function refreshProjectDiagramResource(', 'function refreshWorkspaceOptionalResources(');
  const state=baseGraphState({scopeComponentId:scope,readingMode,diagram:{kind:'old'},currentView:'architecture'});
  state.workspaceAsync=null;
  const calls={canvas:[],cache:[],api:[]};
  const context={
    state,ARCHITECTURE_CANVAS_MODE:canvas,
    loadArchitectureCanvasDiagram:async(...args)=>{calls.canvas.push(args);return {architectureVersion:7,nodes:[],edges:[]};},
    loadArchitectureDiagram:async()=>({architectureVersion:7,nodes:[],edges:[]}),
    deferredBootstrapResourceHref:r=>r.href,api:async href=>{calls.api.push(href);return {architectureVersion:7,nodes:[],edges:[]};},
    normalizeFullCanvasResponse:x=>x,normalizeScopedDiagramResponse:x=>x,
    render(){},clearArchitectureTracePath(){state.tracePathRequest=null;},
  };
  runInNewContext(`${asyncHelpers}\n${graphHelpers}\n${refreshCanvas}\n${refreshProject};state.workspaceAsync=makeWorkspaceAsyncState('project-a');const ticket=beginWorkspaceContext(state.workspaceAsync,'project-a');bindWorkspaceContextArchitecture(state.workspaceAsync,ticket,7);globalThis.ticket=ticket;globalThis.refreshCanvas=refreshCanvasResource;globalThis.refreshProject=refreshProjectDiagramResource;globalThis.cached=cachedArchitectureView;`,context);
  return {context,state,calls};
}

test('canvas refresh isolates remembered Project scope from the Canvas loader', async () => {
  const h=makeOptionalHarness({canvas:true,readingMode:'READ',scope:'remembered'});
  assert.equal(await h.context.refreshCanvas(h.context.ticket,h.state.architecture,{scopeComponentId:'remembered'}),true);
  assert.equal(h.calls.canvas.length,1); assert.equal(h.calls.canvas[0][2],'FULL');
  assert.equal(h.state.scopeComponentId,'remembered');
});

test('workspace bootstrap caches its Project diagram as MAP rather than FULL', async () => {
  const h=makeOptionalHarness({canvas:true});
  const deferred={project_diagram:{href:'/projects/project-a/architecture/diagram?reading_mode=MAP'}};
  assert.equal(await h.context.refreshProject(h.context.ticket,h.state.architecture,{deferredResources:deferred}),true);
  const cached=h.context.cached('project','project-a',7,null,'MAP');
  assert.ok(cached); assert.equal(h.context.cached('project','project-a',7,null,'FULL'),null);
});

test('detail transition reconciles the latest interaction instead of its request-start snapshot', async () => {
  const navigateSource=sourceBetween('async function navigateGraphScope(', 'async function setGraphReadingMode(');
  const state=baseGraphState({scopeComponentId:null,selectedComponentId:'old',inspectorTab:'overview',graphFocusMode:'connected',diagram:{architectureVersion:7,nodes:[],edges:[]}});
  state.workspaceAsync=null; let resolveLoader;
  const context={state,ARCHITECTURE_CANVAS_MODE:false,
    revealArchitectureComponent:async()=>false, nextReadingModeForScope:()=> 'MAP',
    loadArchitectureDiagram:()=>new Promise(resolve=>{resolveLoader=resolve;}),renderGraph(){},toast(){},
    document:undefined,CSS:{escape:s=>s},clearArchitectureTracePath(){state.tracePathRequest=null;},
  };
  runInNewContext(`${asyncHelpers}\n${graphHelpers}\n${navigateSource};state.workspaceAsync=makeWorkspaceAsyncState('project-a');const ticket=beginWorkspaceContext(state.workspaceAsync,'project-a');bindWorkspaceContextArchitecture(state.workspaceAsync,ticket,7);globalThis.navigate=navigateGraphScope;`,context);
  const pending=context.navigate('scope-b',{loader:context.loadArchitectureDiagram});
  state.selectedComponentId='latest'; state.inspectorTab='tasks';
  resolveLoader({architectureVersion:7,nodes:[{id:'node:latest',component_id:'latest'}],edges:[]});
  assert.equal(await pending,true); assert.equal(state.selectedComponentId,'latest'); assert.equal(state.inspectorTab,'tasks');
});

test('ordinary node activation invalidates completed and pending Trace Path state', async () => {
  const activate=sourceBetween('async function activateGraphNode(', 'async function drillGraphNode(');
  const drill=sourceBetween('async function drillGraphNode(', 'async function revealArchitectureComponent(');
  let clears=0; const state={collapsedNodeIds:new Set(['group']),selectedComponentId:null,selectedEdgeId:null,inspectorTab:'overview',graphFocusMode:'all'};
  const context={state,ARCHITECTURE_CANVAS_MODE:true,clearArchitectureTracePath(){clears+=1;},syncArchitectureCanvasSelectionUrl(){},renderGraph(){},graphNodeAction:()=> 'drill',navigateGraphScope:async()=>true};
  runInNewContext(`${activate}\n${drill};globalThis.activate=activateGraphNode;globalThis.drill=drillGraphNode;`,context);
  const node={id:'node:api',component_id:'api',hierarchyPath:['group','node:api']};
  assert.equal(await context.activate(node,{render:()=>{}}),true); assert.equal(clears,1); assert.equal(state.collapsedNodeIds.has('group'),false);
  assert.equal(await context.drill(node),true); assert.equal(clears,2);
});

test('failed mutation and superseded read preserve the original project cache', async () => {
  const h=await makeRefreshHarness();
  const failed=h.context.refresh(); h.resolvers[0].reject(new Error('temporary'));
  assert.equal(await failed,false); assert.equal(h.invalidations,0);
  const first=h.context.refresh(); const second=h.context.refresh();
  h.resolvers[2].resolve({project:{id:'project-a',stamp:'winner'},architecture:{version:7},tasks:[]});
  assert.equal(await second,true);
  h.resolvers[1].resolve({project:{id:'project-a',stamp:'stale'},architecture:{version:7},tasks:[]});
  assert.equal(await first,false); assert.equal(h.invalidations,1);
});

test('failed fresh projection does not leave old detail or Canvas projections reusable', async () => {
  const h=makeOptionalHarness({canvas:true,readingMode:'READ',scope:'remembered'});
  // Seed an exact Canvas/FULL cache entry, then make the fresh load fail.
  runInNewContext(`cacheArchitectureView('canvas','project-a',7,null,'FULL',{stale:true});`,h.context);
  h.context.loadArchitectureCanvasDiagram=async()=>{throw new Error('projection failed');};
  assert.equal(await h.context.refreshCanvas(h.context.ticket,h.state.architecture,{retainData:true}),false);
  assert.equal(h.context.cached('canvas','project-a',7,null,'FULL'),null);
  assert.deepEqual(h.state.diagram,{kind:'old'});
});
