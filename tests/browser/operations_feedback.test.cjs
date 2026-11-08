const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
function dashboard() {
  const nodes = new Map();
  const context = vm.createContext({window:{location:{hash:""},localStorage:{getItem(){return ""}}},document:{getElementById(id){
    if (!nodes.has(id)) nodes.set(id,{innerHTML:'',textContent:'',addEventListener(){},insertAdjacentHTML(where,html){this.innerHTML+=html;},querySelector(){return {}}});
    return nodes.get(id);
  },querySelectorAll(){return [];}}});
  const source = fs.readFileSync(path.join(__dirname,'../../src/robert_agent/interfaces/web/templates.py'),'utf8');
  vm.runInContext(source.slice(source.indexOf('    const translations ='),source.indexOf("    $('theme-select').addEventListener")),context);
  vm.runInContext('state.language="zh"',context);
  return {context,nodes};
}
test('operations describes recent workflows and total runs accurately', () => {
  const {context,nodes} = dashboard();
  assert.equal(vm.runInContext('t("currentWork")',context),'最近工作流');
  vm.runInContext('renderMetrics({summary:{agent_runs:59667},recent_runs:[{status:"completed"}]})',context);
  assert.match(nodes.get('summary-metrics').innerHTML,/累计运行次数/);
  assert.match(nodes.get('summary-metrics').innerHTML,/59667/);
  assert.equal(vm.runInContext('stateLabel("completed")',context),'已完成');
});
test('notification severity filters preserve order and distinguish empty matches', () => {
  const {context,nodes} = dashboard();
  context.data={notification_center:[{id:'one',severity:'info',title:'Routine'},{id:'two',severity:'critical',title:'Failure'},{id:'three',severity:'warning',title:'Warning'}]};
  vm.runInContext('state.notificationSeverity="attention";renderNotifications(data)',context);
  assert.doesNotMatch(nodes.get('notification-center').innerHTML,/Routine/);
  assert.match(nodes.get('notification-center').innerHTML,/Failure[\s\S]*Warning/);
  vm.runInContext('state.notificationSeverity="critical";renderNotifications(data)',context);
  assert.doesNotMatch(nodes.get('notification-center').innerHTML,/Warning/);
  vm.runInContext('state.notificationSeverity="critical";renderNotifications({notification_center:[]})',context);
  assert.match(nodes.get('notification-center').innerHTML,/没有符合筛选条件的通知/);
});
test('known system events have Chinese labels with raw identifiers preserved', () => {
  const {context} = dashboard();
  assert.equal(vm.runInContext('dl("diagnostics_pruned")',context),'诊断记录清理完成');
  assert.equal(vm.runInContext('dl("pending_authorization")',context),'等待授权');
  const html=vm.runInContext('timelineCard({title:"diagnostics_pruned",kind:"daemon_event",summary:"diagnostics_pruned"})',context);
  assert.match(html,/诊断记录清理完成/);
  assert.match(html,/diagnostics_pruned/);
  assert.equal(vm.runInContext('dl("An original user title")',context),'An original user title');
});


test('English operations states remain human-readable', () => {
  const {context}=dashboard();
  vm.runInContext('state.language="en"',context);
  for (const [value,label] of [['completed','Completed'],['needs_attention','Needs attention'],['worker_running','Worker running'],['waiting_publish','Waiting publish']]) {
    assert.equal(vm.runInContext(`stateLabel(${JSON.stringify(value)})`,context),label);
  }
});

test('failed control-session fetch can be retried',async()=>{
  const {context}=dashboard(); let calls=0;
  context.fetch=async()=>{if(++calls===1)throw new Error('offline');return {ok:true,json:async()=>({csrf_token:'token'})}};
  await assert.rejects(vm.runInContext('controlSession()',context));
  assert.equal((await vm.runInContext('controlSession()',context)).csrf_token,'token');
  assert.equal(calls,2);
});
test('knowledge candidate identifiers are data rather than inline JavaScript',()=>{
  const {context}=dashboard();
  context.candidate={candidate_id:"x');alert(1);//",title:'Test'};
  const html=vm.runInContext('candidateCard(candidate)',context);
  assert.doesNotMatch(html,/on(?:click|submit)=/);
  assert.match(html,/data-candidate-id=/);
});
test('malformed operations hash leaves navigation usable',()=>{
  const {context}=dashboard();context.window.location.hash='#work/%ZZ';
  vm.runInContext('setView=()=>{}; applyHash()',context);
  assert.equal(vm.runInContext('state.selectedTaskId',context),null);
});
test('older dashboard and artifact responses cannot replace a newer selection',async()=>{
  const {context}=dashboard(); const requests=[];
  context.fetch=()=>new Promise(resolve=>requests.push(resolve));
  vm.runInContext('chooseDefaultTask=()=>{}; renderAll=()=>{}; openArtifactDrawer=()=>{}; resetArtifactPreview=()=>{}; renderArtifactPreview=(text)=>{globalThis.preview=text;}',context);
  const first=vm.runInContext('loadDashboard()',context), second=vm.runInContext('loadDashboard()',context);
  requests[1]({ok:true,json:async()=>({id:'new'})}); await second;
  requests[0]({ok:true,json:async()=>({id:'old'})}); await first;
  assert.equal(vm.runInContext('state.data.id',context),'new');
  const one=vm.runInContext('previewArtifact("one","stdout")',context),two=vm.runInContext('previewArtifact("two","stdout")',context);
  requests[3]({ok:true,text:async()=> 'two'});await two;
  requests[2]({ok:true,text:async()=> 'one'});await one;
  assert.equal(context.preview,'two');
});
test('repeated dashboard errors share one visible status',async()=>{
  const {context,nodes}=dashboard();context.fetch=async()=>{throw new Error('offline')};
  await vm.runInContext('loadDashboard()',context);await vm.runInContext('loadDashboard()',context);
  assert.equal((nodes.get('dashboard-app')?.innerHTML.match(/class="error"/g)||[]).length,0);
  assert.match(nodes.get('dashboard-load-error').textContent,/offline/);
});
