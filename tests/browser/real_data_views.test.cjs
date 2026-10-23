const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.dataset = {}; this.events = {}; this.attrs = {}; this.classList = { toggle() {}, remove() {}, add() {} }; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  setAttribute(key, value) { this.attrs[key] = value; }
  addEventListener(name, fn) { this.events[name] = fn; }
  focus() {}
  scrollIntoView() { this.scrolled = (this.scrolled || 0) + 1; }
  querySelectorAll(tag) { return this.children.flatMap(descendants).filter(n => n.tag === tag); }
}
function load(name, search = '') {
  const nodes = new Map();
  const get = key => { if (!nodes.has(key)) nodes.set(key, new Element(key)); return nodes.get(key); };
  const context = vm.createContext({ URLSearchParams, location: { search }, localStorage: { getItem: () => null },
    document: { body:new Element("body"), getElementById: key => get(`#${key}`), createElement: tag => new Element(tag), addEventListener() {}, querySelectorAll: () => [],
      querySelector: key => { if (!nodes.has(key)) nodes.set(key, new Element(key)); return nodes.get(key); } },
  });
  const source = fs.readFileSync(path.join(__dirname, '../../src/robert_agent/web_assets', name), 'utf8').replace(/start\(\);\s*$/, '');
  vm.runInContext(source, context);
  return context;
}
function descendants(node) { return [node, ...node.children.flatMap(descendants)]; }

test('initial board shows a populated actionable stage and honors an explicit empty stage', () => {
  for (const [search, expected] of [['', 'waiting'], ['?column=doing', 'doing']]) {
    const context = load('board.js', search);
    vm.runInContext('renderMobileColumn(); renderBoard({items:[],counts:{waiting:1,done:40},capacity:{running:0}})', context);
    assert.equal(vm.runInContext('state.mobileColumn', context), expected);
  }
});

test('SQLite UTC timestamps and explicit UTC timestamps display the same time', () => {
  for (const file of ['board.js', 'workbench.js']) {
    const context = load(file);
    assert.equal(vm.runInContext('formatTime("2026-10-22 12:00:00")', context), vm.runInContext('formatTime("2026-10-22T12:00:00Z")', context));
  }
});

test('workbench groups phases by attempt and links the exact available artifact', () => {
  const context = load('workbench.js');
  context.detail = { source:{}, operator_state:{}, tasks:[], attempts:[
    {attempt_id:'old', task_id:'task', attempt_no:1, status:'failed', started_at:'2026-10-20', failure_summary:'Timed out'},
    {attempt_id:'new', task_id:'task', attempt_no:2, status:'completed', started_at:'2026-10-21'},
  ], phases:[{attempt_id:'old',phase:'execute',summary:'Older attempt',created_at:'2026-10-20'}],
  artifacts:[
    {artifact_id:'a1',task_id:'task',attempt_id:'old',artifact_type:'worker_stdout',preview_available:true},
    {artifact_id:'a2',task_id:'task',attempt_id:'new',artifact_type:'worker_stdout',preview_available:false},
  ] };
  const tree = vm.runInContext('detailView(detail, "title")', context);
  const nodes = descendants(tree);
  const history = nodes.find(n => n.className === 'detail-section execution-history');
  assert.equal(history.tag, 'details');
  assert.equal(history.attrs.open, undefined);
  const runs = history.children.filter(n => n.tag === 'details');
  assert.match(runs[0].children[0].textContent, /#2/);
  assert.match(runs[1].children[0].textContent, /#1/);
  const links = nodes.filter(n => n.href?.startsWith('/artifact.txt'));
  assert.equal(links.length, 1);
  assert.equal(new URLSearchParams(links[0].href.split('?')[1]).get('artifact_id'), 'a1');
  assert.ok(descendants(runs[1]).some(n => n.textContent === 'Timed out'));
  assert.ok(!descendants(runs[0]).some(n => n.textContent === 'Timed out'));
});


test('hundreds of attempts are loaded in batches and retained after refreshing', () => {
  const context = load('board.js');
  context.detail = { tasks:[{task_id:'heavy-task',lifecycle:'completed',attempts:Array.from({length:394}, (_, i) => ({attempt_id:`a-${i}`,attempt_no:i+1,status:'failed',phases:[]}))}] };
  let history = vm.runInContext('renderExecutionHistory(detail)', context);
  let runs = descendants(history).filter(n => n.dataset.attemptId);
  assert.equal(runs.length, 10);
  assert.equal(runs[0].dataset.attemptId, 'a-393');
  const more = descendants(history).find(n => n.tag === 'button');
  more.events.click();
  history = vm.runInContext('renderExecutionHistory(detail)', context);
  assert.equal(descendants(history).filter(n => n.dataset.attemptId).length, 20);
  const loadAll = descendants(history).find(n => n.tag === 'button');
  for (let i = 0; i < 38; i++) loadAll.events.click();
  runs = descendants(history).filter(n => n.dataset.attemptId);
  assert.equal(runs.length, 394);
  assert.equal(new Set(runs.map(n => n.dataset.attemptId)).size, 394);
  assert.equal(loadAll.hidden, true);
});

test('workbench refresh retains open history for the same item only', () => {
  const context = load('workbench.js');
  context.detail = {id:'one',source:{},tasks:[],attempts:[{attempt_id:'a',task_id:'t',attempt_no:1}]};
  vm.runInContext('renderWorkItemDetail(detail)', context);
  let tree = vm.runInContext('$("drawer-content")', context);
  descendants(tree).filter(n => n.tag === 'details').forEach(n => n.open = true);
  vm.runInContext('renderWorkItemDetail(detail)', context);
  tree = vm.runInContext('$("drawer-content")', context);
  assert.ok(descendants(tree).filter(n => n.tag === 'details').every(n => n.open));
  vm.runInContext('renderWorkItemDetail({...detail, id:"two"})', context);
  assert.ok(descendants(tree).filter(n => n.tag === 'details').every(n => !n.open));
});

test('lifecycle joins intake, execution recovery and recorded PR events in time order', () => {
  const context = load('board.js');
  context.detail = {created_at:'2026-10-20T00:00:00Z', tasks:[{
    task_id:'classify', expected_output:'classification_result', lifecycle:'completed', attempts:[
      {status:'failed', started_at:'2026-10-20T00:01:00Z', finished_at:'2026-10-20T00:02:00Z'},
      {status:'completed', started_at:'2026-10-20T00:03:00Z', finished_at:'2026-10-20T00:04:00Z'},
    ],
  }], events:[
    {event_type:'pr_merged', created_at:'2026-10-22T00:00:00Z'},
    {event_type:'pr_opened', created_at:'2026-10-21T00:00:00Z'},
  ], pr:{state:'closed'}};
  const tree = vm.runInContext('renderJourney(detail)', context);
  assert.equal(tree.tag, 'section');
  const text = descendants(tree).map(n => n.textContent || '').join('\n');
  assert.match(text, /2 attempts · 1 failed/);
  assert.match(text, /Classify requirement/);
  assert.ok(text.indexOf('Task received') < text.indexOf('Classify requirement'));
  assert.ok(text.indexOf('PR created') < text.indexOf('PR merge recorded'));
  const withoutEvidence = vm.runInContext('renderJourney({...detail, events:[]})', context);
  assert.ok(!descendants(withoutEvidence).some(n => n.textContent?.includes('PR merge recorded')));
  context.detail.tasks[0].publication_actions = [{action_type:'open_pr',status:'published',audit_status:'accepted',created_at:'2026-10-20T00:04:00Z',published_at:'2026-10-20T00:05:00Z'}];
  const published = vm.runInContext('renderJourney({...detail, events:[]})', context);
  assert.ok(descendants(published).some(n => n.textContent === 'Create PR · Published'));
});

test('board positions the initial stage but polling does not reset horizontal browsing', () => {
  const context = load('board.js');
  vm.runInContext('renderBoard({items:[],counts:{waiting:1},capacity:{running:0}})', context);
  assert.equal(vm.runInContext('$(\'.board-column[data-column="waiting"]\').scrolled', context), 1);
  vm.runInContext('renderBoard({items:[],counts:{waiting:1},capacity:{running:0}})', context);
  assert.equal(vm.runInContext('$(\'.board-column[data-column="waiting"]\').scrolled', context), 1);
});


test('source identity and linked delivery stay distinct in workbench detail', () => {
  const context = load('workbench.js');
  context.detail = {source:{type:'issue',number:3404,url:'https://github.com/org/repo/issues/3404'},
    operator_state:{bucket:'history'}, lifecycle:{column:'done',reason_code:'completed',pr:{number:3405,url:'https://github.com/org/repo/pull/3405',state:'closed'}}, tasks:[]};
  const tree = vm.runInContext('detailView(detail,"title")',context);
  const text = descendants(tree).map(n => n.textContent || '').join('\n');
  assert.match(text,/Issue #3404/);
  assert.doesNotMatch(text,/PR #3404/);
  assert.ok(descendants(tree).some(n => n.href === 'https://github.com/org/repo/pull/3405' && n.textContent.includes('PR #3405')));
});

test('archived workflow is not presented as successful task completion', () => {
  const context = load('workbench.js');
  for (const [lifecycle,label,icon] of [[{column:'history',reason_code:'canceled'},'Canceled','×'],[{column:'review',reason_code:'open_pr'},'Awaiting review','◷'],[null,'Workflow ended','—']]) {
    context.item = {bucket:'history',reason_code:'inactive',lifecycle};
    assert.equal(vm.runInContext('reasonLabel(item)',context),label);
    assert.equal(vm.runInContext('stateIcon(item).textContent',context),icon);
  }
});

test('sort description follows all three sort choices', () => {
  const context = load('workbench.js');
  for (const [sort,label] of [['priority','Actionability order'],['updated-desc','Most recently updated'],['updated-asc','Oldest update first']]) {
    vm.runInContext(`state.filters = {sort:${JSON.stringify(sort)}}`,context);
    assert.equal(vm.runInContext('sortDescription()',context),label);
  }
});

test('empty results clear both detail surfaces and invalidate pending detail requests', async () => {
  const context = load('workbench.js');
  const window = {scrollY:0,scrollTo(){},location:{pathname:'/work'},history:{replaceState(){}}};
  Object.assign(context,{window,AbortController,fetch:async()=>({ok:true,json:async()=>({items:[],counts:{}})})});
  vm.runInContext('state.hasLoaded=true;state.filters={bucket:"history",q:"missing"};state.selectedId="old";state.detail={id:"old"};state.detailController=new AbortController()',context);
  await vm.runInContext('loadWorkItems()',context);
  assert.equal(vm.runInContext('state.detail',context),null);
  assert.equal(vm.runInContext('state.selectedId',context),null);
  assert.equal(vm.runInContext('state.detailController.signal.aborted',context),true);
  for (const id of ['detail-panel','drawer-content']) {
    assert.ok(descendants(vm.runInContext(`$("${id}")`,context)).some(n=>n.textContent==='Select a PR or issue'));
  }
});

test('loading a shared item into the result page removes its outside-results notice', async () => {
  const context=load('workbench.js');
  Object.assign(context,{AbortController,window:{scrollY:0,scrollTo(){}},fetch:async()=>({ok:true,json:async()=>({items:[{id:'shared',bucket:'history',source:{},agent:{},signals:{}}],counts:{}})})});
  vm.runInContext('state.hasLoaded=true;state.filters={bucket:"history",selectedId:"shared"};state.selectedId="shared";state.outsideResults=true;loadWorkItemDetail=async()=>{}',context);
  await vm.runInContext('loadWorkItems()',context);
  assert.equal(vm.runInContext('state.outsideResults',context),false);
});
