import assert from 'node:assert/strict';
import test from 'node:test';
import {readFile} from 'node:fs/promises';
import {runInNewContext} from 'node:vm';
const app = (await readFile(new URL('../frontend/web/app.js',import.meta.url),'utf8')).replace(/\r\n/g,'\n');
function section(start,end) {
  const a=app.indexOf(start), b=app.indexOf(end,a);
  assert.ok(a>=0 && b>a); return app.slice(a,b);
}
function harness(projectId='A') {
  const state={projectId, project:{id:projectId},architecture:{version:1},currentView:'tasks',
    readingMode:'MAP',scopeComponentId:null,navigation:{generation:1},graphTransitionGeneration:10};
  let route={projectId,canvas:false,view:'architecture',workspaceTab:'tasks',nodeId:null,inspectorTab:'overview'};
  const context={state, window:{location:{}},ARCHITECTURE_CANVAS_MODE:false,
    readNavigationRoute:()=>route, syncWorkingRequestUI:()=>{},
    navigationGenerationIsCurrent:g=>g.generation===state.navigation.generation,
    applyWorkspaceTabInvariants:()=>{},clearArchitectureTracePath:()=>{},render:()=>{},
    commitNavigation:()=>true, selectProject:async()=>true, setArchitectureCanvasMode:async()=>true,
    openPersonalWorkspace:async()=>true,
  };
  const restore=runInNewContext(`${section('function beginNavigationTransition(', 'function captureNavigationGuard(')}\n${section('async function restoreNavigationFromLocation(', "window.addEventListener('popstate'")}\nrestoreNavigationFromLocation`,context);
  return {state,restore,setRoute:r=>{route={...route,...r};}};
}
for (const project of ['A','B']) {
  test(`in-flight ${project} map remains current through same-project Back/Forward`, async()=>{
    const h=harness(project), captured=h.state.graphTransitionGeneration;
    // A diagram started for this exact projection before these popstate events.
    for (const view of ['tasks','architecture','tasks','architecture']) {
      h.setRoute({view}); await h.restore();
      assert.equal(h.state.graphTransitionGeneration,captured,'same projection must not expire');
    }
  });
}
test('cross-project and surface-changing History still invalidate the old projection',async()=>{
  const h=harness();
  h.setRoute({projectId:'B'}); await h.restore();
  assert.equal(h.state.graphTransitionGeneration,11);
  h.setRoute({projectId:'A',canvas:true}); await h.restore();
  assert.equal(h.state.graphTransitionGeneration,12);
});
test('explicit Tasks deep link wins over remembered Review, explicit Review remains valid',()=>{
  const read=runInNewContext(`${section('function readNavigationRoute(', 'function beginNavigationTransition(')}\nreadNavigationRoute`,{
    window:{location:{}},URLSearchParams,ROUTED_VIEWS:new Set(['overview','architecture','tasks']),
    INSPECTOR_TABS:new Set(['overview']),workspaceTabNames:['tasks','review'],
    localStorage:{getItem:key=>key==='archbro-project-id'?'A':'review'},
  });
  assert.equal(read({search:'?project=A&view=tasks'},{useStorageFallback:true}).workspaceTab,'tasks');
  assert.equal(read({search:'?project=A&view=tasks&workspace=review'},{useStorageFallback:true}).workspaceTab,'review');
});
