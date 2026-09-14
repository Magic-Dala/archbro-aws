import test from 'node:test';
import assert from 'node:assert/strict';
import {createProjectRepositoryController} from '../frontend/web/project-repository.js';

class Element {
  constructor() { this.value=''; this.open=false; this.dataset={}; this.children=[]; this.handlers={}; this.disabled=false; this.hidden=false; this.textContent=''; }
  addEventListener(name, fn) { (this.handlers[name] ||= []).push(fn); }
  fire(name) { for (const fn of this.handlers[name] || []) fn({preventDefault(){}}); }
  close() { this.open=false; queueMicrotask(() => this.fire('close')); }
  focus() { this.focused=true; }
  replaceChildren(...children) { this.children=children; this.textContent=''; }
  append(child) { this.children.push(child); }
}
const deferred=()=> { let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject}; };
const flush=()=>new Promise(resolve=>setImmediate(resolve));
function fixture(api) {
  const elements=new Map();
  const doc={getElementById(id){if(!elements.has(id)) elements.set(id,new Element());return elements.get(id);},createElement(){return new Element();}};
  const context={project:{id:'a',name:'Project A'},saved:[],connected:[]};
  const controller=createProjectRepositoryController({api,getProject:()=>context.project,document:doc,
    showDialog:(id)=>{doc.getElementById(id).open=true;},
    onSaved:async(id,value)=>{context.saved.push({id,value});},
    onConnect:(id)=>context.connected.push(id),
  });
  const el=(name)=>doc.getElementById('projectRepository'+name);
  return {controller,context,el};
}
const state=(name=null,rev=0)=>({source_repository:name?{full_name:name,branch:'dev2'}:null,repository_revision:rev,can_manage:true,connected:true});

test('loads server selection and saves repo/default branch with optimistic version',async()=>{
  const calls=[];
  const f=fixture(async(path,options)=>{calls.push({path,options});return state('Magic-Dala/archbro',1);});
  await f.controller.open(f.context.project);
  assert.equal(f.el('Name').value,'Magic-Dala/archbro');
  assert.equal(f.el('Branch').value,'dev2');
  f.el('Name').value='Magic-Dala/other';
  await f.controller.save();
  assert.deepEqual(JSON.parse(calls[1].options.body),{full_name:'Magic-Dala/other',branch:'dev2',expected_revision:1});
  assert.equal(f.context.saved[0].id,'a');
  assert.equal(f.el('Dialog').open,false);
});

test('older search cannot overwrite a newer query or a different project',async()=>{
  const first=deferred(),second=deferred();let n=0;
  const f=fixture(path=>path.includes('repository-options')?(++n===1?first.promise:second.promise):Promise.resolve(state()));
  await f.controller.open(f.context.project);
  f.el('Name').value='one';const a=f.controller.search();
  f.el('Name').value='two';const b=f.controller.search();
  second.resolve({items:[{full_name:'b/two'}]});await b;
  first.resolve({items:[{full_name:'a/one'}]});await a;
  assert.equal(f.el('Results').children[0].textContent,'b/two');
  f.context.project={id:'b',name:'Project B'};
  await f.controller.open(f.context.project);
  await flush(); // delayed native close event from Project A must not retire B
  assert.equal(f.el('Name').disabled,false);
  f.el('Name').value='b/three';await f.controller.save();
  assert.equal(f.context.saved[0].id,'b');
});

test('late save for project A never paints onto project B',async()=>{
  const pending=deferred();
  const f=fixture((path,options)=>options?pending.promise:Promise.resolve(state()));
  await f.controller.open(f.context.project);
  f.el('Name').value='a/one';const saving=f.controller.save();
  f.context.project={id:'b',name:'Project B'};
  await f.controller.open(f.context.project);
  pending.resolve(state('a/one',1));await saving;
  assert.deepEqual(f.context.saved,[]);
  assert.equal(f.el('Name').value,'');
  assert.equal(f.el('Dialog').open,true);
});

test('verification errors remain inline and preserve selection',async()=>{
  const f=fixture(async(_path,options)=>{if(options)throw new Error('Repository access denied');return state('a/one',4);});
  await f.controller.open(f.context.project);
  f.el('Name').value='a/two';await f.controller.save();
  assert.match(f.el('Error').textContent,/access denied/);
  assert.match(f.el('Current').textContent,/a\/one/);
  assert.equal(f.el('Name').value,'a/two');
  assert.equal(f.el('Dialog').open,true);
  assert.equal(f.el('Save').disabled,false);
});

test('non-owner cannot save/search even through controller calls',async()=>{
  const calls=[];const f=fixture(async(path)=>{calls.push(path);return {...state(),can_manage:false};});
  await f.controller.open(f.context.project);
  f.el('Name').value='a/one';await f.controller.save();await f.controller.search();
  assert.equal(calls.length,1);assert.equal(f.el('Save').disabled,true);
});

test('removing binding keeps credential connection untouched',async()=>{
  const calls=[];const f=fixture(async(path,options)=>{calls.push({path,options});return state('a/one',3);});
  await f.controller.open(f.context.project);await f.controller.save(true);
  assert.equal(calls[1].path,'/projects/a/repository?expected_revision=3');
  assert.equal(calls[1].options.method,'DELETE');
  assert.ok(calls.every(call=>!call.path.includes('/mcp/connections')));
});

test('connect action carries original project rather than an OAuth credential',async()=>{
  const f=fixture(async()=>({...state(),connected:false}));
  await f.controller.open(f.context.project);f.el('Connect').fire('click');
  assert.deepEqual(f.context.connected,['a']);assert.equal(f.el('Dialog').open,false);
});

test('same project dialog closed/reopened does not accept old search result',async()=>{
  const pending=deferred();
  const f=fixture(path=>path.includes('repository-options')?pending.promise:Promise.resolve(state()));
  await f.controller.open(f.context.project);f.el('Name').value='a/one';const first=f.controller.search();
  f.controller.close();await f.controller.open(f.context.project);
  pending.resolve({items:[{full_name:'a/old'}]});await first;
  assert.equal(f.el('Results').children.length,0);
});
