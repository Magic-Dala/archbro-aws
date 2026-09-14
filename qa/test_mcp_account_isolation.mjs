import assert from 'node:assert/strict';
import test from 'node:test';
import {readFile} from 'node:fs/promises';
import {runInNewContext} from 'node:vm';
const app=(await readFile(new URL('../frontend/web/app.js',import.meta.url),'utf8')).replace(/\r\n/g,'\n');
function source(start,end){const a=app.indexOf(start),b=app.indexOf(end,a);assert.ok(a>=0&&b>a);return app.slice(a,b);}
const helpers=source('function captureMcpAccountUi(', 'function mcpConnectionForProvider(');
function setup() {
  let user='alice', resolve;
  const elements=new Map();
  const element=id=>{
    if(!elements.has(id))elements.set(id,{dataset:{provider:'github'},textContent:'old',classList:{add:()=>{}},replaceChildren(){this.textContent='';}});
    return elements.get(id);
  };
  const ctx={mcpUiGeneration:0,mcpConnectionsRequestSerial:0,mcpConnectionsSnapshot:[{id:'old'}],mcpProviderStatusCache:new Map([['github',{generation:1,value:{connected:true}}]]),
    prototype:{currentProfile:()=>user?{id:user}:null},localStorage:{},$:element,syncMcpProviderCards:()=>{},
    api:()=>new Promise(r=>{resolve=r;})};
  const f=runInNewContext(helpers+source('async function loadMcpConnections(', 'function openMcpConnections(')+'\n({load:loadMcpConnections,reset:resetMcpAccountUi})',ctx);
  return {ctx,f,element,setUser:u=>{user=u;},resolve:v=>resolve(v)};
}
test('late connection response cannot paint after an account switch',async()=>{
  const h=setup(); const pending=h.f.load(); h.setUser('bob');
  h.resolve([{id:'alice-private-mcp'}]);
  assert.equal((await pending).length,0);
  assert.equal(h.ctx.mcpConnectionsSnapshot[0].id,'old');
  assert.equal(h.element('mcpConnectedCount').textContent,'old');
});
test('logout clears connection UI and invalidates already pending responses',async()=>{
  const h=setup(); const pending=h.f.load(); h.f.reset(); h.setUser(null);
  h.resolve([{id:'alice-private-mcp'}]);
  assert.equal((await pending).length,0);
  assert.equal(h.ctx.mcpConnectionsSnapshot.length,0);
  assert.equal(h.ctx.mcpProviderStatusCache.size,0);
  assert.equal(h.element('mcpConnectedCount').textContent,'0');
  assert.equal(h.element('mcpConnectionNotice').textContent,'');
});
test('provider status begun under another account cannot refill its status cache',async()=>{
  const h=setup(); let finish;
  h.ctx.MCP_PROVIDER_STATUS_TTL_MS=5000;
  h.ctx.resolveMcpProviderStatus=()=>new Promise(r=>{finish=r;});
  const request=runInNewContext(helpers+source('function mcpProviderStatusEntry(', 'function mcpProviderStatusEndpoint(')+source('function requestMcpProviderStatus(', 'function renderMcpOAuthStatusShell(')+'\nrequestMcpProviderStatus',h.ctx);
  const pending=request('github',{force:true});h.setUser('bob');
  finish({connected:true,connection:{id:'alice-private-mcp'}});
  assert.equal(await pending,null);
  assert.equal(h.ctx.mcpProviderStatusCache.get('github').value.connection,undefined);
});
test('invalidated provider status cannot overwrite a newer connect result',async()=>{
  const h=setup();let finish;
  h.ctx.MCP_PROVIDER_STATUS_TTL_MS=5000;
  h.ctx.resolveMcpProviderStatus=()=>new Promise(r=>{finish=r;});
  const request=runInNewContext(helpers+source('function mcpProviderStatusEntry(', 'function mcpProviderStatusEndpoint(')+source('function requestMcpProviderStatus(', 'function renderMcpOAuthStatusShell(')+'\nrequestMcpProviderStatus',h.ctx);
  const pending=request('github',{force:true});
  const entry=h.ctx.mcpProviderStatusCache.get('github');entry.generation+=1;
  entry.value={connected:true,connection:{id:'new-connection'}};
  finish({connected:false});
  assert.equal(await pending,null);
  assert.equal(entry.value.connection.id,'new-connection');
});
