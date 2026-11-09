const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
function workbench(fetch) {
  const context = vm.createContext({fetch, AbortController, setTimeout, clearTimeout,
    crypto: require('node:crypto').webcrypto, URLSearchParams, URL,
    document: {addEventListener() {}, createElement(){return {append(){},setAttribute(){},dataset:{}}}, getElementById(){return {setAttribute(){}}}}, window: {alert() {}, location:{search:"",hash:"",pathname:"/work",href:"http://localhost/work"}, history:{replaceState(){},pushState(){}}}});
  const assets = path.join(__dirname, '../../src/robert_agent/web_assets');
  vm.runInContext(fs.readFileSync(path.join(assets,'board-client.js'),'utf8'),context);
  vm.runInContext(fs.readFileSync(path.join(assets,'workbench.js'),'utf8'),context);
  vm.runInContext('state.session={writes_enabled:true,csrf_token:"token"}; const originalLoadWorkItems=loadWorkItems; loadWorkItems=async()=>{};',context);
  return context;
}
test('each successful retrigger has its own key while a lost response is retried with the same key',async()=>{
  const keys=[];
  const context=workbench(async(url,options)=>{
    keys.push(options.headers['x-idempotency-key']);
    if(keys.length===1) throw new Error('lost response');
    return {ok:true,json:async()=>({ok:true})};
  });
  for(let n=0;n<3;n++) await vm.runInContext('retriggerTask("task",{textContent:"Retry"})',context);
  assert.equal(keys[0],keys[1]);
  assert.notEqual(keys[1],keys[2]);
});

test('unsafe links and malformed hash cannot execute code or break initialization',()=>{
  const context=workbench(async()=>{});
  context.window.location.hash='#work/%ZZ';
  assert.equal(vm.runInContext('readUrlState().selectedId',context),null);
  for(const href of ['javascript:alert(1)','data:text/html,test','//evil.example/']) {
    context.link=href;
    assert.equal(vm.runInContext('node("a",{href:link}).href',context),undefined);
  }
  assert.equal(vm.runInContext('node("a",{href:"https://github.com/x/y"}).href',context),'https://github.com/x/y');
});
test('refresh after loading another page starts from the beginning',async()=>{
  const urls=[];
  const context=workbench(async(url)=>{urls.push(url);return {ok:true,json:async()=>({items:[],next_cursor:null})}});
  vm.runInContext('state.filters={cursor:"later",sort:"priority"}; state.hasLoaded=true; renderWorkItems=()=>{}; clearRefreshError=()=>{}; clearWorkItemDetail=()=>{}; loadWorkItems=originalLoadWorkItems;',context);
  await vm.runInContext('loadWorkItems()',context);
  assert.equal(new URL(urls[0],'http://localhost').searchParams.has('cursor'),false);
});
