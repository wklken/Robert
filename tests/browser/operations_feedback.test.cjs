const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
function dashboard() {
  const nodes = new Map();
  const context = vm.createContext({document:{getElementById(id){
    if (!nodes.has(id)) nodes.set(id,{innerHTML:'',textContent:'',addEventListener(){}});
    return nodes.get(id);
  },querySelectorAll(){return [];}}});
  const source = fs.readFileSync(path.join(__dirname,'../../src/robert_agent/web.py'),'utf8');
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
