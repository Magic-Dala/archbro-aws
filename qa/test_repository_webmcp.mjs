import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

function setup() {
  let projectId='a', generation=1, repositoryRevision=1, serverRevision=1;
  let providerCalls=0;
  const noop=async()=>({});
  const bridge={
    getCommittedNavigation:()=>({initialized:true,project_id:projectId}),
    getActiveProjectBinding:()=>({projectId,generation,repositoryRevision}),
    bootstrapProject:noop,expandArchitectureScope:noop,getDecisionContext:noop,
    submitAgentRecommendation:noop,createTask:noop,updateTaskStatus:noop,
    recordProjectObservation:noop,publishCodeArchitectureSnapshot:noop,
    callConnectedMcpTool:async()=>{providerCalls+=1;return {items:Array.from({length:40},(_,id)=>({id,text:'large-evidence-'.repeat(100)}))};},
  };
  const requests=[];
  const scope={window:{ArchBroWebBridge:bridge},console,URLSearchParams,AbortController,
    getFirebaseIdToken:async()=>null,
    fetch:async(path)=>{requests.push(path);return {ok:true,status:200,json:async()=>({repository_revision:serverRevision})};},
  };
  vm.createContext(scope);
  let source=fs.readFileSync(new URL('../frontend/web/archbro-webmcp.js',import.meta.url),'utf8')
    .replace(/^import .*?;\r?\n/,'').replace(/\bexport\s+/g,'');
  vm.runInContext(source+'\nglobalThis.createToolsForTest=createArchBroTools;',scope);
  const tool=scope.createToolsForTest(bridge).find(t=>t.name==='archbro_call_connected_mcp_tool');
  const call=async(input)=>JSON.parse(await tool.execute({server_id:'github',tool_name:'get_file_contents',...input}));
  return {call,requests,get providerCalls(){return providerCalls;},
    navigate(id){projectId=id;generation+=1;},
    localRebind(){repositoryRevision+=1;serverRevision+=1;},
    remoteRebind(){serverRevision+=1;},
  };
}

test('same-project result recovery does not repeat provider calls',async()=>{
  const f=setup();const first=await f.call({arguments:{path:'README.md'}});
  assert.ok(first.full_result_ref);
  const part=await f.call({result_ref:first.full_result_ref,offset:0,max_chars:200});
  assert.equal(part.content.length,200);assert.equal(f.providerCalls,1);
  assert.deepEqual(f.requests,['/projects/a/repository']);
});

test('opaque result reference cannot be recovered after switching projects',async()=>{
  const f=setup();const first=await f.call({arguments:{path:'README.md'}});
  f.navigate('b');
  await assert.rejects(()=>f.call({result_ref:first.full_result_ref}),/project changed|different project|navigation/i);
  assert.equal(f.providerCalls,1);
});

test('local rebind invalidates an opaque result reference',async()=>{
  const f=setup();const first=await f.call({arguments:{path:'README.md'}});
  f.localRebind();
  await assert.rejects(()=>f.call({result_ref:first.full_result_ref}),/project changed|selection changed|navigation/i);
  assert.equal(f.providerCalls,1);
});

test('server-side rebind invalidates cached evidence even before the browser refreshes',async()=>{
  const f=setup();const first=await f.call({arguments:{path:'README.md'}});
  f.remoteRebind();
  await assert.rejects(()=>f.call({result_ref:first.full_result_ref}),/selection changed/i);
  assert.equal(f.providerCalls,1);
});
