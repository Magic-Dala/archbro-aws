import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {spawnSync} from 'node:child_process';
import {readFile} from 'node:fs/promises';
import test from 'node:test';
import {fileURLToPath} from 'node:url';
import {runInNewContext} from 'node:vm';

const webRoot = new URL('../frontend/web/', import.meta.url);

test('captured obstructed geometry renders four sources as one trunk with PostgreSQL selected', async () => {
  const root = new URL('../', import.meta.url);
  const fixtureUrl = new URL('tests/fixtures/canvas_four_source_geometry.json', root);
  const fixture = JSON.parse(await readFile(fixtureUrl, 'utf8'));
  const probe = spawnSync('python', [
    fileURLToPath(new URL('qa/canvas_geometry_summary.py', root)),
    fileURLToPath(fixtureUrl),
    fileURLToPath(new URL('src/archbro/backend/core/canvas_connection_summaries.py', root)),
  ], {encoding:'utf8', timeout:10000});
  assert.equal(probe.status, 0, probe.stderr || probe.stdout);
  const payload = {
    schema:'archbro.full_canvas.v1',
    diagram:{...fixture.diagram, diagram_version:'archbro.diagram.v1', architecture_version:fixture.graph.architecture_version},
    positioned_graph:{...fixture.graph, layout_version:'archbro.canvas-layout.v10',
      width:Math.max(...fixture.graph.nodes.map(node=>node.x+node.width))+48,
      height:Math.max(...fixture.graph.nodes.map(node=>node.y+node.height))+48},
    connection_summaries:JSON.parse(probe.stdout),
  };
  const app = await readFile(new URL('app.js', webRoot), 'utf8');
  const normalizers = app.slice(app.indexOf('function normalizeDiagramGraph('), app.indexOf('function normalizeCodeArchitectureSnapshot('));
  const renderers = app.slice(app.indexOf('function canvasReciprocalPartner('), app.indexOf('// Local inspection only:'));
  const diagram = runInNewContext(normalizers+';normalizeFullCanvasResponse(payload)', {payload});
  const hub = 'node:persistence_postgresql_store';
  const selected = diagram.nodes.find(node=>node.id===hub);
  const ctx = {diagram, display:diagram, selected,
    state:{selectedEdgeId:null, tracePathResult:null}, view:{kind:'BACKBONE', edgeIds:[]},
    graphWrapPixels:text=>[text], graphViewportKey:()=> 'real-capture',
    canvasSummaryState:{key:'real-capture', expanded:new Set()}};
  const render = ()=>runInNewContext('function activeCanvasReadingView(){return view;}\n'+renderers+';canvasVisibleConnections(diagram,display,null,selected)',ctx).filter(edge=>edge.target===hub);
  for (const view of [{kind:'BACKBONE', edgeIds:[]}, null]) {
    ctx.view=view;
    const connections=render();
    assert.equal(connections.length,2, 'One blue data trunk plus one independent health-probe line');
    const trunk=connections.find(edge=>edge.summary);
    assert.equal(trunk.canvasLabel,'4 sources');
    assert.equal(trunk.memberIds.length,5, 'Governance READS and WRITES remain distinct canonical facts');
    assert.equal(trunk.summary.paths.length,4);
    const shared=trunk.summary.sharedPath;
    assert.ok(shared.slice(1).reduce((length,p,i)=>length+Math.abs(p.x-shared[i].x)+Math.abs(p.y-shared[i].y),0)>=400);
    assert.equal(connections.find(edge=>!edge.summary).relationship_category,'FLOW');
  }
  const summary=render().find(edge=>edge.summary).summary;
  ctx.canvasSummaryState.expanded.add(summary.id);
  assert.equal(render().length,5, 'Explicit expansion restores four logical data connections plus the health probe');
  assert.equal(diagram.edges.filter(edge=>edge.target===hub).length,6);
});

test('connection hue follows the peer top-level boundary regardless of direction', async () => {
  const app=await readFile(new URL('app.js',webRoot),'utf8');
  const functions=app.slice(app.indexOf('function canvasDomainForNode('),app.indexOf('function canvasReciprocalPartner('));
  const a={id:'a',parent_id:'orders',hierarchyPath:['orders','a']};
  const b={id:'b',parent_id:'subsystem',hierarchyPath:['inventory','subsystem','b']};
  const c={id:'c',parent_id:'orders',hierarchyPath:['orders','c']};
  const diagram={fullCanvas:true,nodes:[{id:'orders',label:'Orders'}, {id:'inventory',label:'Inventory'},a,b,c,{id:'subsystem',parent_id:'inventory',hierarchyPath:['inventory','subsystem']}]};
  const ctx={diagram,a,b,c};
  const call=expression=>JSON.parse(JSON.stringify(runInNewContext(functions+';'+expression,ctx)));
  const incoming=call("canvasConnectionDomain({source:'b',target:'a'},a,diagram)");
  assert.equal(incoming.id,'inventory');assert.equal(incoming.label,'Inventory');
  assert.deepEqual(incoming,call("canvasConnectionDomain({source:'a',target:'b'},a,diagram)"));
  assert.equal(call("canvasConnectionDomain({source:'a',target:'b'},b,diagram)").id,'orders');
  assert.equal(call("canvasConnectionDomain({source:'c',target:'a'},a,diagram)").id,'orders');
  assert.equal(call("canvasConnectionDomain({source:'a',target:'a'},a,diagram)").id,'orders');
  assert.equal(call("canvasConnectionDomain({source:'b',target:'c'},a,diagram)"),null);
  assert.equal(call("canvasConnectionDomain({source:'a',target:'b'},null,diagram)"),null);
  assert.equal(incoming.tone,1);
  assert.equal(call('canvasDomainToneAttr(b,diagram)'), 'data-domain-tone=\"1\"');
});


test('architecture canvas dynamic presentation does not require inline styles under strict CSP', async () => {
  const [app, css] = await Promise.all([
    readFile(new URL('app.js', webRoot), 'utf8'),
    readFile(new URL('styles.css', webRoot), 'utf8'),
  ]);
  assert.doesNotMatch(app, /alignmentFill'\)\.style\.width/);
  assert.doesNotMatch(app, /paint\.style\.setProperty\('--connection-color'/);
  assert.doesNotMatch(app, /readout\.style\.setProperty\('--connection-color'/);
  assert.doesNotMatch(app, /style=\"\$\{canvasDomainStyle/);
  assert.doesNotMatch(app, /class=\"canvas-connection-domain\"[^>]*style=/);
  assert.doesNotMatch(app, /class=\"canvas-connection-button\"[^>]*style=/);
  for (let tone = 0; tone < 6; tone += 1) {
    assert.ok(css.includes(`[data-domain-tone=\"${tone}\"]`));
  }
});

test('reciprocal display retains both directed facts and never invents a reverse journey', async () => {
  const app=await readFile(new URL('app.js',webRoot),'utf8');
  const fn=app.slice(app.indexOf('function canvasReciprocalPartner('),app.indexOf('// Local inspection only:'));
  const forward={id:'out',source:'a',target:'b',routing:'ORTHOGONAL_CANVAS_RECIPROCAL',label:'Send order · HTTPS',points:[{x:0,y:0},{x:20,y:0}]};
  const reverse={id:'back',source:'b',target:'a',routing:forward.routing,label:'Report result · Webhook',points:[...forward.points].reverse()};
  const diagram={edges:[forward,reverse]},display=diagram;
  const context={diagram,display,state:{selectedEdgeId:null,tracePathResult:null},view:null,graphWrapPixels:t=>[t]};
  const run=()=>JSON.parse(JSON.stringify(runInNewContext('function activeCanvasReadingView(){return view;}\n'+fn+';canvasVisibleConnections(diagram,display,null,null)',context)));
  assert.deepEqual(run()[0].memberIds,['out','back']);
  assert.equal(run()[0].bidirectional,true);
  assert.deepEqual(run()[0].points,forward.points);
  context.view={edgeIds:['out'],kind:'BACKBONE'};
  assert.equal(run()[0].bidirectional,true);
  context.display={edges:[forward]};
  assert.equal(run()[0].bidirectional,false, 'A missing server relationship must not be invented');
  context.display=display;
  context.state.selectedEdgeId='out';
  assert.equal(run()[0].bidirectional,true);
  context.view={edgeIds:['out'],kind:'AUTHORED_JOURNEY'};
  assert.deepEqual(run()[0].memberIds,['out']);
  context.view=null;context.state.tracePathResult={status:'FOUND'};
  assert.equal(run().length,2);
  context.state.tracePathResult=null;
  diagram.edges.push({...forward,id:'additional'});
  assert.equal(run().length,2, 'Same-direction parallel actions stay one visual connection while the reverse fact remains explicit');
});

test('context tray distinguishes missing observed usage from no run and real zero', async () => {
  const app = await readFile(new URL('app.js', webRoot), 'utf8');
  const renderer = app.slice(app.indexOf('function renderAgentContextTray() {'), app.indexOf('async function ensureAgentContextManifest('));
  for (const [lastRun, expected] of [
    [null, 'Not run yet'],
    [{context_telemetry: {manifest_hash: 'run'}}, 'Unavailable'],
    [{provider_usage: null}, 'Unavailable'],
    [{provider_usage: {input_tokens: 0}}, '0 tokens'],
    [{provider_usage: {inputTokens: 25}}, '25 tokens'],
  ]) {
    const elements = Object.fromEntries(['agentContextTray', 'agentContextTrayBody', 'agentContextTrayTitle', 'agentContextPolicy', 'agentContextTelemetryToggle'].map(id => [id, {classList: {toggle() {}}, setAttribute() {}}]));
    runInNewContext(`${renderer}\nrenderAgentContextTray();`, {
      $: id => elements[id],
      state: {lastRun, agentContextTelemetryVisible: true, agentContextManifest: {sections: {}, usage: {}, selection: {}}},
      selectedAgentContextNode: () => ({name: 'API'}),
      escapeHtml: String,
      agentContextNames: () => 'None',
    });
    assert.ok(elements.agentContextTrayBody.innerHTML.includes(`<dt>Last provider actual</dt><dd>${expected}</dd>`));
  }
});

test('architecture canvas app passes Node syntax check', () => {
  const appPath = fileURLToPath(new URL('app.js', webRoot));
  const syntax = spawnSync(process.execPath, ['--check', appPath], {encoding: 'utf8'});
  assert.equal(syntax.status, 0, syntax.stderr || syntax.stdout);
});

test('architecture canvas reuses the canonical diagram surface with a dedicated viewport mode', async () => {
  const [page, app, css, webmcp] = await Promise.all([
    readFile(new URL('index.html', webRoot), 'utf8'),
    readFile(new URL('app.js', webRoot), 'utf8'),
    readFile(new URL('styles.css', webRoot), 'utf8'),
    readFile(new URL('archbro-webmcp.js', webRoot), 'utf8'),
  ]);

  assert.match(page, /id="architectureCanvasBtn"/);
  assert.match(page, /Open Canvas ↗/);
  for (const asset of ['app.js', 'styles.css']) {
    const body = await readFile(new URL(asset, webRoot));
    const normalized = body.toString('utf8').replace(/\r\n/g, '\n');
    const hash = createHash('sha256').update(normalized).digest('hex').slice(0, 16);
    assert.ok(page.includes(`/static/${asset}?v=${hash}`), `${asset} must use its current content hash`);
  }
  assert.match(app, /ARCHITECTURE_CANVAS_MODE/);
  assert.match(app, /URL_PARAMS\.get\('canvas'\) === 'architecture'/);
  assert.match(app, /function commitNavigation\(/);
  assert.match(app, /localStorage\.setItem\('archbro-project-id', normalized\.projectId\)/);
  const bindingAuthority=webmcp.slice(webmcp.indexOf('function activeProjectBinding()'),webmcp.indexOf('function activeProjectId()'));
  assert.match(bindingAuthority, /getCommittedNavigation/);
  assert.ok(bindingAuthority.indexOf("typeof bridge?.getCommittedNavigation === 'function'") < bindingAuthority.indexOf('Legacy embedding surfaces'));
  assert.match(app, /\/architecture\/canvas/);
  assert.match(app, /normalizeFullCanvasResponse/);
  assert.match(app, /groupFrames/);
  assert.match(app, /architecture-group-frame/);
  assert.match(app, /architecture-group-owner/);
  assert.match(app, /architectureInspectorTabsMarkup/);
  assert.match(app, /selectedEdgeId/);
  assert.match(app, /selectGraphEdge/);
  assert.match(app, /syncArchitectureCanvasSelectionUrl/);
  assert.match(app, /\/architecture\/path/);
  assert.match(app, /archbro\.architecture_path\.v1/);
  assert.match(app, /data-trace-path/);
  assert.match(app, /LIMIT_REACHED/);
  assert.match(app, /data-agent-explore/);
  assert.match(app, /agentContextExecutionRequest/);
  assert.match(app, /data-inspector-tab/);
  assert.match(app, /graphFocusMode === 'isolate'/);
  assert.match(app, /boundaryNodes/);
  assert.match(app, /focusGraphNodeInViewport/);
  assert.match(app, /collapsedNodeIds/);
  assert.match(app, /projection_kind:'COLLAPSED'/);
  assert.match(app, /data-toggle-collapse/);
  assert.match(app, /data-expand-all/);
  assert.match(app, /relationship-event/);
  assert.match(app, /edge\?\.relationship_category/);
  assert.match(app, /Project-level context/);
  assert.match(app, /Pending proposals affecting this component/);
  assert.match(app, /Hierarchy path/);
  assert.match(app, /No Code Truth snapshot yet/);
  assert.match(app, /fullCanvas:true/);
  assert.match(app, /wireGraphViewport\(canvas\.querySelector\('\.living-graph-svg'\)\)/);
  assert.match(app, /addEventListener\('wheel'/);
  assert.match(app, /addEventListener\('pointerdown'/);
  assert.match(app, /data-open-selected-scope/);
  assert.match(css, /\.architecture-canvas-mode #graphCanvas \.living-graph-svg/);
  assert.match(css, /\.architecture-canvas-mode #graphCanvas \.architecture-group-frame/);
  assert.match(css, /\.architecture-canvas-mode #graphCanvas \.architecture-group-owner/);
  assert.match(css, /\.architecture-inspector-tabs/);
  assert.match(css, /\.graph-edge\.selected/);
  assert.match(css, /\.graph-edge\.is-isolated-out/);
  assert.match(css, /data-zoom-tier="overview"/);
  assert.doesNotMatch(app, /ReactFlow|Cytoscape|dagre|elkjs/i);
});

test('nested collapse clips canonical routes and preserves unrelated self loops', async () => {
  const app=await readFile(new URL('app.js',webRoot),'utf8');
  const fn=app.slice(app.indexOf('function graphDisplayModel(diagram) {'), app.indexOf('function graphVisualConnections('));
  const diagram={fullCanvas:true,nodes:[
    {id:'root',childCount:1,hierarchyPath:['root']},
    {id:'inner',childCount:1,hierarchyPath:['root','inner']},
    {id:'child',childCount:0,hierarchyPath:['root','inner','child']},
    {id:'peer',childCount:0,hierarchyPath:['peer']},
  ],groupFrames:[{nodeId:'root',x:0,y:0,width:100,height:100},{nodeId:'inner',x:10,y:10,width:80,height:80}],edges:[
    {id:'out',source:'child',target:'peer',points:[{x:50,y:50},{x:150,y:50}]},
    {id:'in',source:'peer',target:'child',points:[{x:150,y:70},{x:50,y:70}]},
    {id:'self',source:'peer',target:'peer',points:[{x:150,y:60},{x:170,y:60},{x:170,y:40},{x:150,y:40}]},
  ]};
  const original=JSON.stringify(diagram);
  const state={collapsedNodeIds:new Set(['inner','root'])};
  const context={diagram,state,ARCHITECTURE_CANVAS_MODE:true};
  const result=JSON.parse(JSON.stringify(runInNewContext(fn+';graphDisplayModel(diagram)',context)));
  assert.deepEqual(result.nodes.map(n=>n.id),['root','peer']);
  assert.deepEqual(result.edges.map(e=>e.id),['out','in','self']);
  assert.deepEqual(result.edges[0].points,[{x:100,y:50},{x:150,y:50}]);
  assert.deepEqual(result.edges[1].points,[{x:150,y:70},{x:100,y:70}]);
  assert.equal(result.edges[0].canonical_source,'child');
  assert.equal(result.edges[1].canonical_target,'child');
  assert.equal(JSON.stringify(diagram),original);
  state.collapsedNodeIds.clear();
  const expanded=JSON.parse(JSON.stringify(runInNewContext(fn+';graphDisplayModel(diagram)',context)));
  assert.deepEqual(expanded.edges,diagram.edges);
});

test('server connection summaries preserve explicit drilldown and exact directed views', async () => {
  const app=await readFile(new URL('app.js',webRoot),'utf8');
  const fn=app.slice(app.indexOf('function canvasSummarizeConnections('),app.indexOf('function expandCanvasConnectionSummary('));
  const a={id:'a',source:'one',target:'hub',memberIds:['a'],points:[{x:0,y:0},{x:40,y:0}]};
  const b={id:'b',source:'two',target:'hub',memberIds:['b'],points:[{x:0,y:10},{x:40,y:10}]};
  const group={id:'summary',hubId:'hub',direction:'IN',category:'EVENT',memberIds:['a','b'],primaryId:'a',paths:[a.points,b.points]};
  const ctx={connections:[a,b],diagram:{edges:[a,b],connectionSummaries:[group]},display:{},view:{kind:'BACKBONE'},selected:null,focus:null,state:{selectedEdgeId:null,tracePathResult:null},canvasSummaryState:{key:'key',expanded:new Set()},graphViewportKey:()=> 'key'};
  const run=()=>JSON.parse(JSON.stringify(runInNewContext(fn+';canvasSummarizeConnections(connections,diagram,display,view,selected,focus)',ctx)));
  assert.equal(run().length,1);assert.deepEqual(run()[0].memberIds,['a','b']);assert.equal(run()[0].bidirectional,false);
  ctx.canvasSummaryState.expanded.add('summary');assert.equal(run().length,2);ctx.canvasSummaryState.expanded.clear();
  ctx.state.selectedEdgeId='b';assert.equal(run().length,2);ctx.state.selectedEdgeId=null;
  ctx.selected={id:'one'};assert.equal(run().length,2);ctx.selected={id:'hub'};assert.equal(run().length,1);ctx.selected=null;
  ctx.view={kind:'AUTHORED_JOURNEY'};assert.equal(run().length,2);ctx.view=null;assert.equal(run().length,1);ctx.view={kind:'BACKBONE'};
  ctx.state.tracePathResult={status:'FOUND'};assert.equal(run().length,2);ctx.state.tracePathResult=null;
  ctx.connections=[a];assert.equal(run()[0].summary,undefined);ctx.connections=[a,b];
  ctx.focus={edges:new Set(['a'])};assert.equal(run().length,2);ctx.focus=null;
  b.projection_kind='COLLAPSED';assert.equal(run().length,2);delete b.projection_kind;
  b.memberIds=['b','return'];assert.equal(run().length,2);
});

test('complete detail keeps compatible parallel actions grouped', async () => {
  const app=await readFile(new URL('app.js',webRoot),'utf8');
  const fn=app.slice(app.indexOf('function canvasReciprocalPartner('),app.indexOf('// Local inspection only:'));
  const read={id:'read',source:'governance',target:'store',relationship_category:'DATA',semantic_type:'READS',label:'READS',points:[{x:0,y:0},{x:0,y:100}]};
  const write={id:'write',source:'governance',target:'store',relationship_category:'DATA',semantic_type:'WRITES',label:'WRITES',points:[{x:20,y:0},{x:20,y:100}]};
  const diagram={fullCanvas:true,edges:[read,write],connectionSummaries:[]},display={edges:[read,write]};
  const ctx={diagram,display,state:{selectedEdgeId:null,tracePathResult:null},view:null,graphWrapPixels:t=>[t],canvasSummaryState:{key:'key',expanded:new Set()},graphViewportKey:()=> 'key'};
  const result=JSON.parse(JSON.stringify(runInNewContext('function activeCanvasReadingView(){return view;}\n'+fn+';canvasVisibleConnections(diagram,display,null,null)',ctx)));
  assert.equal(result.length,1);
  assert.deepEqual(result[0].memberIds,['read','write']);
  assert.equal(result[0].canvasLabel,'READS + WRITES');
  assert.equal(result[0].parallelActions,true);
});

test('parallel action bundles stay labelled even at overview density', async () => {
  const [app,css]=await Promise.all([readFile(new URL('app.js',webRoot),'utf8'),readFile(new URL('styles.css',webRoot),'utf8')]);
  assert.match(app,/edge\.summary \|\| edge\.parallelActions \|\| edge\.memberIds\.includes/);
  assert.match(app,/edge\.parallelActions \? 'parallel-actions'/);
  assert.match(css,/data-zoom-tier="overview"\] \.projection-parallel-actions \.canvas-edge-key/);
});

test('server shared-trunk summaries compose over a parallel action representative', async () => {
  const app=await readFile(new URL('app.js',webRoot),'utf8');
  const fn=app.slice(app.indexOf('function canvasSummarizeConnections('),app.indexOf('function expandCanvasConnectionSummary('));
  const task={id:'task',source:'task',target:'store',memberIds:['task'],points:[{x:0,y:0},{x:100,y:0}]};
  const context={id:'context',source:'context',target:'store',memberIds:['context'],points:[{x:0,y:20},{x:100,y:20}]};
  const layout={id:'layout',source:'layout',target:'store',memberIds:['layout'],points:[{x:0,y:40},{x:100,y:40}]};
  const parallel={id:'read',source:'governance',target:'store',memberIds:['read','write'],parallelActions:true,points:[{x:0,y:60},{x:100,y:60}]};
  const canonical=[task,context,layout,{id:'read',source:'governance',target:'store'},{id:'write',source:'governance',target:'store'}];
  const memberIds=['context','layout','read','task','write'];
  const group={id:'summary',hubId:'store',direction:'IN',category:'DATA',memberIds,primaryId:'task',peerCount:4,paths:[task.points,context.points,layout.points,parallel.points]};
  const ctx={connections:[task,context,layout,parallel],diagram:{edges:canonical,connectionSummaries:[group]},display:{},view:{kind:'BACKBONE'},selected:null,focus:null,state:{selectedEdgeId:null,tracePathResult:null},canvasSummaryState:{key:'key',expanded:new Set()},graphViewportKey:()=> 'key'};
  const result=JSON.parse(JSON.stringify(runInNewContext(fn+';canvasSummarizeConnections(connections,diagram,display,view,selected,focus)',ctx)));
  assert.equal(result.length,1);
  assert.deepEqual(result[0].memberIds,memberIds);
  assert.equal(result[0].canvasLabel,'4 sources');
});

test('canvas mode switches in-page and never uses popup or location navigation', async () => {
  const app=await readFile(new URL('app.js',webRoot),'utf8');
  const section=app.slice(app.indexOf('function syncArchitectureCanvasDomMode('),app.indexOf('function setArchitectureGraphKind('));
  assert.match(section,/commitNavigation/);
  const navigationWriter=app.slice(app.indexOf('function writeNavigationUrl('),app.indexOf('function supersededNavigationError('));
  assert.match(navigationWriter,/history\[method\]/);
  assert.doesNotMatch(section,/window\.open\(/);
  assert.doesNotMatch(section,/window\.location\.assign/);
  assert.match(app,/addEventListener\('popstate'/);
  assert.match(app,/const architectureViewCache/);
  assert.match(app,/cachedArchitectureView\(targetSurface, projectId, architectureVersion, targetScopeComponentId, targetReadingMode\)/);
  const core=app.slice(app.indexOf('// WORKSPACE_CORE_LOADER_START'),app.indexOf('// WORKSPACE_CORE_LOADER_END'));
  assert.match(core,/loadProjectCoreContext/);
  assert.match(core,/workspace-bootstrap\?reading_mode=FULL/);
  assert.match(core,/archbro\.workspace-bootstrap\.v2/);
  assert.doesNotMatch(core,/code-architecture|architecture\/canvas/);
  const optional=app.slice(app.indexOf('async function refreshCanvasResource('),app.indexOf('function clearWorkspaceOptionalData('));
  assert.match(optional,/deferredBootstrapResourceHref/);
  assert.match(optional,/refreshCodeArchitectureResource/);
  assert.match(optional,/refreshProjectDiagramResource/);
  assert.match(optional,/refreshWorkspaceOptionalResources/);
  assert.ok(optional.includes("beginWorkspaceResource(")); assert.ok(optional.includes("'projectDiagram'"));
  assert.doesNotMatch(app,/bootstrapArchitectureViewsRequest|diagramLoading/);
});

async function canvasSwitchHarness(options = {}) {
  const app = await readFile(new URL('app.js', webRoot), 'utf8');
  const navigation = app.slice(app.indexOf('const ROUTED_VIEWS'), app.indexOf('const architectureViewCache'));
  const normalizedMode = app.slice(app.indexOf('function normalizedArchitectureReadingMode('), app.indexOf('function architectureViewCacheKey('));
  const graphHelpers = app.slice(app.indexOf('function beginGraphTransition('), app.indexOf('function showExperience('));
  const asyncState = app.slice(
    app.indexOf('// WORKSPACE_ASYNC_STATE_START'),
    app.indexOf('// WORKSPACE_ASYNC_STATE_END') + '// WORKSPACE_ASYNC_STATE_END'.length,
  );
  const helpers = app.slice(app.indexOf('// ARCHITECTURE_INTERACTION_STATE_START'), app.indexOf('// ARCHITECTURE_INTERACTION_STATE_END') + '// ARCHITECTURE_INTERACTION_STATE_END'.length);
  const section = app.slice(app.indexOf('async function setArchitectureCanvasMode('), app.indexOf('async function openArchitectureCanvas('));
  const state = {
    projectId:'project-1', architecture:{version:7,components:[{id:'api'}]}, diagram:{kind:options.initialMode ? 'canvas' : 'project'},
    readingMode:'MAP', currentView:'architecture', scopeComponentId:'backend', selectedComponentId:'api',
    selectedEdgeId:'edge-1', inspectorTab:'tasks', graphFocusMode:'connected', collapsedNodeIds:new Set(['group']),
    canvasDeepLinkApplied:true, canvasDeepLinkFocusPending:false, canvasInspectorOpen:true,
    tracePathRequest:{source_id:'node:api'}, tracePathResult:{status:'FOUND'}, tracePathLoading:false, tracePathError:null, diagramError:null,
    graphTransitionGeneration:0,
    navigation:{generation:0,initialized:true,committed:{projectId:'project-1',view:'architecture',canvas:Boolean(options.initialMode),nodeId:options.initialMode?'api':null,inspectorTab:options.initialMode?'tasks':'overview',workspaceTab:'tasks'}},
  };
  const calls = {push:0,replace:0,clear:0,render:0,load:0,cache:[],toasts:[]};
  const context = {
    state, URL, URLSearchParams, views:{overview:{},tasks:{},architecture:{}}, INSPECTOR_TABS:new Set(['overview','dependencies','tasks','evidence','code','decisions']),
    ARCHITECTURE_CANVAS_MODE:Boolean(options.initialMode), architectureCanvasModeRequest:0,
    localStorage:{getItem(){return null;},setItem(){},removeItem(){}},
    window:{location:{href:`https://archbro.invalid/?project=project-1&view=architecture${options.initialMode ? '&canvas=architecture&node=api&tab=tasks' : ''}`}},
    history:{
      pushState(_state,_title,url){calls.push += 1; context.window.location.href = url;},
      replaceState(_state,_title,url){calls.replace += 1; context.window.location.href = url;},
    },
    cachedArchitectureView:kind => options.cached?.[kind] || null,
    loadArchitectureCanvasDiagram:async(...args) => {calls.load += 1; return options.load ? options.load(...args) : {kind:'canvas',architectureVersion:7,nodes:[{id:'node:api',component_id:'api',hierarchyPath:[]}]};},
    loadArchitectureDiagram:async(...args) => {calls.load += 1; return options.load ? options.load(...args) : {kind:'project',architectureVersion:7,nodes:[{id:'node:api',component_id:'api',hierarchyPath:[]}]};},
    cacheArchitectureView:(...args) => calls.cache.push(args),
    architectureViewCache:{delete(){}}, architectureViewCacheKey:()=>'',
    diagramNodeByComponentId:(id,diagram=state.diagram)=>(diagram?.nodes||[]).find(node=>node.component_id===id)||null,
    findArchitectureParentId:()=>null, syncArchitectureCanvasDomMode:() => {},
    clearArchitectureTracePath:() => {calls.clear += 1; state.tracePathRequest = state.tracePathResult = null; state.tracePathLoading=false; state.tracePathError=null;},
    render:() => {calls.render += 1; if(options.renderFailure && calls.render === 1) throw new Error('render failed');},
    toast:message => calls.toasts.push(message), console,
  };
  runInNewContext(`${navigation}\n${normalizedMode}\n${graphHelpers}\n${asyncState}\n${helpers}\n${section}\n
    state.workspaceAsync=makeWorkspaceAsyncState(state.projectId);
    const contextTicket=beginWorkspaceContext(state.workspaceAsync,state.projectId);
    bindWorkspaceContextArchitecture(state.workspaceAsync,contextTicket,state.architecture?.version);
    state.workspaceAsync.resources.canvas.status=state.diagram ? 'ready' : 'empty';
    globalThis.switchMode=setArchitectureCanvasMode;
  `, context);
  return {context,state,calls,switchMode:context.switchMode};
}

function comparableCanvasSwitchState(value) {
  const copy = {...value, collapsedNodeIds:[...(value.collapsedNodeIds || [])]};
  delete copy.workspaceAsync;
  if (copy.navigation) copy.navigation = {...copy.navigation, generation:0};
  if ('graphTransitionGeneration' in copy) copy.graphTransitionGeneration = 0;
  return JSON.parse(JSON.stringify(copy));
}

test('canvas switch failure preserves all reading state in both directions', async () => {
  for (const initialMode of [false,true]) {
    const h = await canvasSwitchHarness({initialMode,load:async() => {throw new Error('network unavailable');}});
    const before = structuredClone(h.state), url = h.context.window.location.href;
    assert.equal(await h.switchMode(!initialMode), false);
    assert.deepEqual(comparableCanvasSwitchState(h.state), comparableCanvasSwitchState(before));
    assert.equal(h.context.ARCHITECTURE_CANVAS_MODE, initialMode);
    const restoredUrl = new URL(h.context.window.location.href);
    assert.equal(restoredUrl.searchParams.get('project'), 'project-1');
    assert.equal(restoredUrl.searchParams.get('view'), 'architecture');
    assert.equal(restoredUrl.searchParams.get('canvas') === 'architecture', initialMode);
    assert.equal(h.calls.push, 0); assert.equal(h.calls.clear, 0); assert.equal(h.calls.render, 0);
    assert.equal(h.state.workspaceAsync.resources.canvas.status, 'ready');
    assert.equal(h.state.workspaceAsync.resources.canvas.refreshing, false);
  }
});

test('canvas switch rejects delayed results after project or architecture changes', async () => {
  for (const change of ['project','version']) {
    let resolve;
    const h = await canvasSwitchHarness({load:() => new Promise(done => {resolve = done;})});
    const pending = h.switchMode(true);
    if (change === 'project') h.state.projectId = 'project-2';
    else h.state.architecture.version += 1;
    const before = structuredClone(h.state);
    resolve({kind:'stale'});
    assert.equal(await pending, false);
    assert.deepEqual(comparableCanvasSwitchState(h.state), comparableCanvasSwitchState(before));
    assert.equal(h.calls.cache.length, 0); assert.equal(h.calls.push, 0); assert.equal(h.calls.render, 0);
  }
});

test('canvas switch back to the current mode cancels an in-flight opposite switch', async () => {
  let resolve;
  const h = await canvasSwitchHarness({load:() => new Promise(done => {resolve = done;})});
  const before = structuredClone(h.state);
  const pending = h.switchMode(true);
  assert.equal(await h.switchMode(false), true);
  resolve({kind:'obsolete-canvas'});
  assert.equal(await pending, false);
  assert.equal(h.context.ARCHITECTURE_CANVAS_MODE, false);
  assert.deepEqual(comparableCanvasSwitchState(h.state), comparableCanvasSwitchState(before));
  assert.equal(h.calls.push, 0); assert.equal(h.calls.clear, 0);
  assert.equal(new URL(h.context.window.location.href).searchParams.get('project'), 'project-1');
  assert.equal(new URL(h.context.window.location.href).searchParams.get('view'), 'architecture');
  assert.equal(h.state.workspaceAsync.resources.canvas.status, 'ready');
  assert.equal(h.state.workspaceAsync.resources.canvas.refreshing, false);
});

test('canvas switch latest request wins and stale failures do not alter it', async () => {
  const requests = [];
  const h = await canvasSwitchHarness({load:() => new Promise((resolve,reject) => {requests.push({resolve,reject});})});
  const first = h.switchMode(true), second = h.switchMode(true);
  requests[1].resolve({kind:'latest-canvas'});
  assert.equal(await second, true);
  const accepted = structuredClone(h.state);
  requests[0].reject(new Error('obsolete failure'));
  assert.equal(await first, false);
  assert.deepEqual(comparableCanvasSwitchState(h.state), comparableCanvasSwitchState(accepted));
  assert.equal(h.context.ARCHITECTURE_CANVAS_MODE, true);
  assert.equal(h.calls.push, 1); assert.equal(h.calls.render, 1); assert.equal(h.calls.toasts.length, 0);
});

test('canvas switch failure during popstate restores the displayed mode URL', async () => {
  const h = await canvasSwitchHarness({load:async() => {throw new Error('network unavailable');}});
  const before = structuredClone(h.state);
  h.context.window.location.href += '&canvas=architecture';
  assert.equal(await h.switchMode(true, {pushHistory:false}), false);
  assert.deepEqual(comparableCanvasSwitchState(h.state), comparableCanvasSwitchState(before));
  assert.equal(new URL(h.context.window.location.href).searchParams.has('canvas'), false);
  assert.equal(h.calls.push, 0); assert.equal(h.calls.replace, 1);
});

test('canvas switch rolls back reading state after a synchronous commit failure', async () => {
  const h = await canvasSwitchHarness({renderFailure:true});
  const before = structuredClone(h.state), url = h.context.window.location.href;
  assert.equal(await h.switchMode(true), false);
  assert.deepEqual(comparableCanvasSwitchState(h.state), comparableCanvasSwitchState(before));
  assert.equal(h.context.ARCHITECTURE_CANVAS_MODE, false);
  const restoredUrl = new URL(h.context.window.location.href);
  assert.equal(restoredUrl.searchParams.get('project'), 'project-1');
  assert.equal(restoredUrl.searchParams.get('view'), 'architecture');
  assert.equal(restoredUrl.searchParams.has('canvas'), false);
  assert.equal(h.state.workspaceAsync.resources.canvas.status, 'ready');
  assert.equal(h.state.workspaceAsync.resources.canvas.refreshing, false);
});

test('canvas switch uses the current cached diagram without a reload', async () => {
  const cached = {kind:'cached-canvas'};
  const h = await canvasSwitchHarness({cached:{canvas:cached}});
  const architecture = structuredClone(h.state.architecture);
  assert.equal(await h.switchMode(true), true);
  assert.equal(h.state.diagram, cached);
  assert.equal(h.calls.load, 0); assert.equal(h.calls.push, 1); assert.equal(h.calls.render, 1);
  assert.deepEqual(h.state.architecture, architecture);
});

test('canvas switch fences an older pending Project Diagram resource load', async () => {
  const app = await readFile(new URL('app.js', webRoot), 'utf8');
  const navigation = app.slice(app.indexOf('const ROUTED_VIEWS'), app.indexOf('const architectureViewCache'));
  const normalizedMode = app.slice(app.indexOf('function normalizedArchitectureReadingMode('), app.indexOf('function architectureViewCacheKey('));
  const graphHelpers = app.slice(app.indexOf('function beginGraphTransition('), app.indexOf('function showExperience('));
  const asyncState = app.slice(
    app.indexOf('// WORKSPACE_ASYNC_STATE_START'),
    app.indexOf('// WORKSPACE_ASYNC_STATE_END') + '// WORKSPACE_ASYNC_STATE_END'.length,
  );
  const refresh = app.slice(
    app.indexOf('async function refreshCanvasResource('),
    app.indexOf('async function refreshCodeArchitectureResource('),
  );
  const helpers = app.slice(
    app.indexOf('// ARCHITECTURE_INTERACTION_STATE_START'),
    app.indexOf('// ARCHITECTURE_INTERACTION_STATE_END') + '// ARCHITECTURE_INTERACTION_STATE_END'.length,
  );
  const mode = app.slice(
    app.indexOf('async function setArchitectureCanvasMode('),
    app.indexOf('async function openArchitectureCanvas('),
  );

  let resolveProjectDiagram;
  const state = {
    projectId:'project-1', architecture:{version:7,components:[{id:'api'}]}, diagram:{kind:'project'}, diagramError:null,
    readingMode:'MAP', currentView:'architecture', scopeComponentId:null, selectedComponentId:null,
    selectedEdgeId:null, inspectorTab:'overview', graphFocusMode:'all', collapsedNodeIds:new Set(),
    canvasDeepLinkApplied:false, canvasDeepLinkFocusPending:false, canvasInspectorOpen:false,
    tracePathRequest:null, tracePathResult:null, tracePathLoading:false, tracePathError:null,
    graphTransitionGeneration:0,
    navigation:{generation:0,initialized:true,committed:{projectId:'project-1',view:'architecture',canvas:false,nodeId:null,inspectorTab:'overview',workspaceTab:'tasks'}},
  };
  const context = {
    state, URL, URLSearchParams, views:{overview:{},tasks:{},architecture:{}}, INSPECTOR_TABS:new Set(['overview']),
    ARCHITECTURE_CANVAS_MODE:false, architectureCanvasModeRequest:0,
    localStorage:{getItem(){return null;},setItem(){},removeItem(){}},
    window:{location:{href:'https://archbro.invalid/?project=project-1'}},
    history:{pushState(_state,_title,url){context.window.location.href=url;},replaceState(_state,_title,url){context.window.location.href=url;}},
    loadArchitectureDiagram:async() => new Promise(resolve => {resolveProjectDiagram=resolve;}),
    loadArchitectureCanvasDiagram:async() => ({kind:'canvas'}),
    cachedArchitectureView:() => null,
    cacheArchitectureView:() => {}, resetArchitectureViewCache:() => {},
    architectureViewCache:{delete(){}}, architectureViewCacheKey:()=>'',
    diagramNodeByComponentId:(id,diagram=state.diagram)=>(diagram?.nodes||[]).find(node=>node.component_id===id)||null,
    findArchitectureParentId:()=>null, syncArchitectureCanvasDomMode:() => {},
    clearArchitectureTracePath:() => {state.tracePathRequest=state.tracePathResult=null;state.tracePathLoading=false;state.tracePathError=null;}, render:() => {}, toast:() => {}, console,
  };
  runInNewContext(`${navigation}\n${normalizedMode}\n${graphHelpers}\n${asyncState}\n${refresh}\n${helpers}\n${mode}\n
    state.workspaceAsync=makeWorkspaceAsyncState('project-1');
    const ticket=beginWorkspaceContext(state.workspaceAsync,'project-1');
    bindWorkspaceContextArchitecture(state.workspaceAsync,ticket,7);
    globalThis.ticket=ticket;
    globalThis.refreshCanvasResourceForTest=refreshCanvasResource;
    globalThis.switchCanvasModeForTest=setArchitectureCanvasMode;
  `, context);

  const staleProjectRefresh = context.refreshCanvasResourceForTest(context.ticket, state.architecture, {retainData:true});
  assert.equal(state.workspaceAsync.resources.canvas.requestGeneration, 2);
  assert.equal(await context.switchCanvasModeForTest(true), true);
  assert.equal(context.ARCHITECTURE_CANVAS_MODE, true);
  assert.deepEqual(state.diagram, {kind:'canvas'});
  assert.equal(state.workspaceAsync.resources.canvas.requestGeneration, 3);
  assert.equal(state.workspaceAsync.resources.canvas.status, 'ready');

  resolveProjectDiagram({kind:'stale-project'});
  assert.equal(await staleProjectRefresh, false);
  assert.equal(context.ARCHITECTURE_CANVAS_MODE, true);
  assert.deepEqual(state.diagram, {kind:'canvas'});
  assert.equal(state.workspaceAsync.resources.canvas.status, 'ready');
});

test('canvas fallback title is model-neutral', async () => {
  const app=await readFile(new URL('app.js',webRoot),'utf8');
  const section=app.slice(app.indexOf('function renderArchitectureChrome('),app.indexOf('function renderCodeGraph('));
  assert.match(section,/presentation\?\.title \|\| 'System architecture'/);
  assert.doesNotMatch(section,/state\.project\?\.name \|\| 'System architecture'/);
});

test('workspace restore never presents the signed-out landing screen as loading state', async () => {
  const [page,app]=await Promise.all([readFile(new URL('index.html',webRoot),'utf8'),readFile(new URL('app.js',webRoot),'utf8')]);
  assert.match(page,/id="bootstrapExperience"/);
  assert.match(page,/id="entryExperience" class="entry-experience hidden"/);
  const enter=app.slice(app.indexOf('async function enterWorkspace()'),app.indexOf('async function api('));
  assert.ok(enter.indexOf("showExperience('restoring')") < enter.indexOf('initializeWorkspace()'));
  assert.ok(enter.indexOf('initializeWorkspace()') < enter.indexOf("showExperience('workspace')"));
  assert.match(page,/id="bootstrapRetryBtn"/);
  assert.match(page,/id="bootstrapLogoutBtn"/);
  assert.match(app,/showWorkspaceRecovery\(new Error/);
  const core=app.slice(app.indexOf('// WORKSPACE_CORE_LOADER_START'),app.indexOf('// WORKSPACE_CORE_LOADER_END'));
  assert.match(core,/workspace-bootstrap\?reading_mode=FULL/);
  assert.match(core,/archbro\.workspace-bootstrap\.v2/);
  assert.doesNotMatch(core,/architecture\/canvas|code-architecture/);
  assert.match(app,/void refreshWorkspaceOptionalResources\(/);
  const workspaceInit=app.slice(app.indexOf('async function initializeWorkspace()'),app.indexOf('async function initializeApp()'));
  assert.match(workspaceInit,/const projectsPromise = loadProjects\(\{guard\}\)/);
  assert.match(workspaceInit,/const directProjectContextPromise = initialProjectId/);
  assert.match(workspaceInit,/loadProjectCoreContext\(initialProjectId/);
  assert.ok(workspaceInit.indexOf('await directProjectContextPromise') < workspaceInit.indexOf('await projectsPromise'));
  assert.match(workspaceInit,/refreshWorkspaceOptionalResources\(startupTicket/);
  assert.doesNotMatch(app,/bootstrapArchitectureViewsRequest|diagramLoading/);
});

test('deferred bootstrap projections use canonical resource generations and isolate failures', async () => {
  const app=await readFile(new URL('app.js',webRoot),'utf8');
  const asyncBlock=app.slice(app.indexOf('// WORKSPACE_ASYNC_STATE_START')+'// WORKSPACE_ASYNC_STATE_START'.length,app.indexOf('// WORKSPACE_ASYNC_STATE_END'));
  const normalizedMode=app.slice(app.indexOf('function normalizedArchitectureReadingMode('),app.indexOf('function architectureViewCacheKey('));
  const graphHelpers=app.slice(app.indexOf('function beginGraphTransition('),app.indexOf('function showExperience('));
  const optionalBlock=app.slice(app.indexOf('function deferredBootstrapResourceHref('),app.indexOf('function clearWorkspaceOptionalData('));
  let resolveCanvas, rejectProject;
  const canvasPromise=new Promise(resolve=>{resolveCanvas=resolve;});
  const projectPromise=new Promise((_resolve,reject)=>{rejectProject=reject;});
  const cached=[], renders=[];
  const state={projectId:'project-1',architecture:{version:4},diagram:null,diagramError:null,readingMode:'MAP',currentView:'architecture',scopeComponentId:null,selectedComponentId:null,selectedEdgeId:null,inspectorTab:'overview',graphFocusMode:'all',tracePathRequest:null,tracePathResult:null,tracePathLoading:false,tracePathError:null,graphTransitionGeneration:0,codeDiagram:null};
  const context={
    state,
    ARCHITECTURE_CANVAS_MODE:true,
    api:href=>href.includes('/architecture/canvas?')?canvasPromise:projectPromise,
    normalizeFullCanvasResponse:payload=>payload,
    normalizeScopedDiagramResponse:payload=>payload,
    loadArchitectureCanvasDiagram:()=>Promise.reject(new Error('deferred canvas href should be used')),
    loadArchitectureDiagram:()=>Promise.reject(new Error('deferred project href should be used')),
    loadLatestCodeArchitecture:()=>Promise.resolve(null),
    normalizeCodeArchitectureSnapshot:payload=>payload,
    cachedArchitectureView:()=>null,
    resetArchitectureViewCache:()=>{},
    cacheArchitectureView:(...args)=>cached.push(args),
    architectureViewCache:{delete(){}}, architectureViewCacheKey:()=>'',
    clearArchitectureTracePath:()=>{state.tracePathRequest=state.tracePathResult=null;state.tracePathLoading=false;state.tracePathError=null;},
    render:()=>renders.push('render'),
    renderGraph:()=>{},
  };
  runInNewContext(asyncBlock+normalizedMode+graphHelpers+optionalBlock+';globalThis.makeAsync=makeWorkspaceAsyncState;globalThis.beginContext=beginWorkspaceContext;globalThis.bindContext=bindWorkspaceContextArchitecture;globalThis.refreshCanvas=refreshCanvasResource;globalThis.refreshProject=refreshProjectDiagramResource;',context);
  state.workspaceAsync=context.makeAsync('project-1');
  const ticket=context.beginContext(state.workspaceAsync,'project-1');
  context.bindContext(state.workspaceAsync,ticket,4);
  const resources={
    canvas:{status:'DEFERRED',href:'/projects/project-1/architecture/canvas?expected_architecture_version=4&reading_mode=FULL'},
    project_diagram:{status:'DEFERRED',href:'/projects/project-1/architecture/diagram?expected_architecture_version=4&reading_mode=MAP'},
  };
  const canvas=context.refreshCanvas(ticket,{version:4},{deferredResources:resources});
  const project=context.refreshProject(ticket,{version:4},{deferredResources:resources});
  resolveCanvas({fullCanvas:true,id:'canvas-ready'});
  assert.equal(await canvas,true);
  assert.equal(state.diagram.id,'canvas-ready');
  assert.equal(state.workspaceAsync.resources.canvas.status,'ready');
  assert.equal(state.workspaceAsync.resources.projectDiagram.status,'loading');
  rejectProject(new Error('project view unavailable'));
  assert.equal(await project,false);
  assert.equal(state.diagram.id,'canvas-ready');
  assert.equal(state.workspaceAsync.resources.projectDiagram.status,'error');
  assert.equal(cached.some(args=>args[0]==='canvas'),true);
  assert.equal(cached.some(args=>args[0]==='project'),false);

  let resolveStale;
  const stalePromise=new Promise(resolve=>{resolveStale=resolve;});
  context.api=()=>stalePromise;
  const staleTicket=context.beginContext(state.workspaceAsync,'project-1');
  context.bindContext(state.workspaceAsync,staleTicket,4);
  const pending=context.refreshCanvas(staleTicket,{version:4},{deferredResources:resources});
  const b=context.beginContext(state.workspaceAsync,'project-2');
  context.bindContext(state.workspaceAsync,b,9);
  resolveStale({fullCanvas:true,id:'stale'});
  assert.equal(await pending,false);
  assert.equal(state.diagram.id,'canvas-ready');
});

test('summary response validates membership, direction, boundaries and route identities', async () => {
  const app=await readFile(new URL('app.js',webRoot),'utf8');
  const normalizers=app.slice(app.indexOf('function normalizeDiagramGraph('),app.indexOf('function normalizeCodeArchitectureSnapshot('));
  const nodes=[{id:'domain'}, {id:'other'}, {id:'a',parent_id:'domain'}, {id:'b',parent_id:'domain'}, {id:'hub',parent_id:'other'}];
  const points=[{x:0,y:0},{x:100,y:0},{x:100,y:100}];
  const edges=[{id:'one',source:'a',target:'hub',relationship_category:'EVENT'},{id:'two',source:'b',target:'hub',relationship_category:'EVENT'}];
  const group={id:'summary',architecture_version:1,direction:'IN',hub_node_id:'hub',domain_node_id:'domain',relationship_category:'EVENT',member_edge_ids:['one','two'],primary_edge_id:'one',paths:[points,points],shared_path:points,member_paths:{one:points,two:points}};
  const base={schema:'archbro.full_canvas.v1',diagram:{diagram_version:'archbro.diagram.v1',architecture_version:1,nodes,edges},positioned_graph:{layout_version:'archbro.canvas-layout.v8',architecture_version:1,width:200,height:200,nodes:nodes.map((n,i)=>({node_id:n.id,x:i*10,y:0,width:20,height:20,hierarchy_path:n.parent_id?[n.parent_id,n.id]:[n.id]})),edges:edges.map(e=>({edge_id:e.id,source:e.source,target:e.target,points}))},connection_summaries:{schema:'archbro.connection-summaries.v1',architecture_version:1,groups:[group]}};
  const run=payload=>runInNewContext(normalizers+';normalizeFullCanvasResponse(payload)',{payload});
  assert.equal(run(structuredClone(base)).connectionSummaries.length,1);
  for(const modify of [p=>p.connection_summaries.architecture_version++,p=>p.connection_summaries.groups[0].member_edge_ids=['one','missing'],p=>p.connection_summaries.groups[0].member_edge_ids=['one','one'],p=>p.connection_summaries.groups[0].direction='OUT',p=>p.connection_summaries.groups[0].domain_node_id='other',p=>p.connection_summaries.groups[0].relationship_category='DATA',p=>p.connection_summaries.groups[0].paths[0][0].x=999]) {
    const payload=JSON.parse(JSON.stringify(base));modify(payload);assert.throws(()=>run(payload));
  }
  const legacy=structuredClone(base);delete legacy.connection_summaries;assert.equal(run(legacy).connectionSummaries.length,0);
});

test('summary validator accepts the real divergence after an overlapping branch', async () => {
  const app=await readFile(new URL('app.js',webRoot),'utf8');
  const normalizers=app.slice(app.indexOf('function normalizeDiagramGraph('),app.indexOf('function normalizeCodeArchitectureSnapshot('));
  const nodes=[{id:'domain'},{id:'hub',parent_id:'domain'},{id:'a',parent_id:'domain'},{id:'b',parent_id:'domain'}];
  const primary=[{x:100,y:100},{x:100,y:0},{x:0,y:0}];
  const second=[{x:100,y:100},{x:100,y:0},{x:80,y:0},{x:80,y:50},{x:0,y:50}];
  const edges=[{id:'one',source:'hub',target:'a',relationship_category:'FLOW'},{id:'two',source:'hub',target:'b',relationship_category:'FLOW'}];
  const group={id:'summary',architecture_version:1,direction:'OUT',hub_node_id:'hub',domain_node_id:'domain',relationship_category:'FLOW',member_edge_ids:['one','two'],primary_edge_id:'one',shared_path:[{x:100,y:100},{x:100,y:0},{x:80,y:0}],paths:[primary,[{x:50,y:0},{x:80,y:0},{x:80,y:50},{x:0,y:50}]],junctions:[{x:80,y:0}],member_paths:{one:primary,two:second}};
  const positionedNodes=nodes.map((n,i)=>({node_id:n.id,x:i*200,y:200,width:80,height:80,hierarchy_path:n.parent_id?['domain',n.id]:[n.id]}));
  const payload={schema:'archbro.full_canvas.v1',diagram:{diagram_version:'archbro.diagram.v1',architecture_version:1,nodes,edges},positioned_graph:{layout_version:'archbro.canvas-layout.v9',architecture_version:1,width:800,height:500,nodes:positionedNodes,edges:[{edge_id:'one',source:'hub',target:'a',points:primary},{edge_id:'two',source:'hub',target:'b',points:second}]},connection_summaries:{schema:'archbro.connection-summaries.v1',architecture_version:1,groups:[group]}};
  const result=runInNewContext(normalizers+';normalizeFullCanvasResponse(payload)',{payload});
  assert.deepEqual(JSON.parse(JSON.stringify(result.connectionSummaries[0].junctions)),[{x:80,y:0}]);
  const invalid=structuredClone(payload);invalid.connection_summaries.groups[0].junctions=[{x:50,y:0}];
  assert.throws(()=>runInNewContext(normalizers+';normalizeFullCanvasResponse(payload)',{payload:invalid}),/real branch/);
});

test('summary validator accepts parallel canonical facts inside one logical peer', async () => {
  const app=await readFile(new URL('app.js',webRoot),'utf8');
  const normalizers=app.slice(app.indexOf('function normalizeDiagramGraph('),app.indexOf('function normalizeCodeArchitectureSnapshot('));
  const nodes=[
    {id:'backend'}, {id:'persistence'},
    {id:'task',parent_id:'backend'}, {id:'context',parent_id:'backend'},
    {id:'governance',parent_id:'backend'}, {id:'store',parent_id:'persistence'},
  ];
  const taskPath=[{x:0,y:0},{x:100,y:0},{x:100,y:100}];
  const contextPath=[{x:0,y:30},{x:60,y:30},{x:60,y:0},{x:100,y:0},{x:100,y:100}];
  const readPath=[{x:0,y:60},{x:80,y:60},{x:80,y:0},{x:100,y:0},{x:100,y:100}];
  const writePath=[{x:0,y:60},{x:90,y:60},{x:90,y:110},{x:110,y:110}];
  const edges=[
    {id:'task-store',source:'task',target:'store',relationship_category:'DATA'},
    {id:'context-store',source:'context',target:'store',relationship_category:'DATA'},
    {id:'governance-read',source:'governance',target:'store',relationship_category:'DATA'},
    {id:'governance-write',source:'governance',target:'store',relationship_category:'DATA'},
  ];
  const routes=[taskPath,contextPath,readPath,writePath];
  const memberIds=edges.map(edge=>edge.id).sort();
  const group={
    id:'summary-parallel-peer',architecture_version:2,direction:'IN',hub_node_id:'store',domain_node_id:'backend',relationship_category:'DATA',
    member_edge_ids:memberIds,primary_edge_id:'task-store',shared_path:[{x:100,y:0},{x:100,y:100}],
    paths:[taskPath,contextPath,readPath],member_paths:{
      'task-store':taskPath,'context-store':contextPath,'governance-read':readPath,'governance-write':writePath,
    },
  };
  const positionedNodes=nodes.map((node,index)=>({node_id:node.id,x:index*150,y:200,width:90,height:60,hierarchy_path:node.parent_id?[node.parent_id,node.id]:[node.id]}));
  const payload={
    schema:'archbro.full_canvas.v1',
    diagram:{diagram_version:'archbro.diagram.v1',architecture_version:2,nodes,edges},
    positioned_graph:{layout_version:'archbro.canvas-layout.v9',architecture_version:2,width:1000,height:600,nodes:positionedNodes,edges:edges.map((edge,index)=>({edge_id:edge.id,source:edge.source,target:edge.target,points:routes[index]}))},
    connection_summaries:{schema:'archbro.connection-summaries.v1',architecture_version:2,groups:[group]},
  };
  const result=runInNewContext(normalizers+';normalizeFullCanvasResponse(payload)',{payload});
  assert.equal(result.connectionSummaries.length,1);
  assert.equal(result.connectionSummaries[0].peerCount,3);
  assert.equal(result.connectionSummaries[0].paths.length,3);
  assert.deepEqual([...result.connectionSummaries[0].memberIds].sort(),memberIds);
});
