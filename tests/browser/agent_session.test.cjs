const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.events = {}; this.dataset = {}; this.textContent = ''; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  addEventListener(name, callback) { this.events[name] = callback; }
  setAttribute() {}
}
const tree = node => [node, ...node.children.flatMap(tree)];
const record = (id, text = id) => ({id, kind:'assistant', text, output:'', status:'', exit_code:null});
const detail = (attempts = [{attempt_id:'a1', attempt_no:1, status:'running', started_at:'2026-11-20T01:00:00Z'}]) => ({tasks:[{task_id:'task', expected_output:'new_pr', attempts}]});
function view(get, initial = detail()) {
  const context = vm.createContext({ URLSearchParams, document:{
    createTextNode: text => Object.assign(new Element('#text'), {textContent:text}),
  }});
  const api = vm.runInContext(fs.readFileSync(path.join(__dirname, '../../src/robert_agent/web_assets/agent-session.js'), 'utf8') + '\nAgentSessionView;', context);
  const node = (tag, className, text) => Object.assign(new Element(tag), {className, textContent:text || ''});
  const t = key => key;
  const button = (text, callback) => { const el = node('button', '', text); el.events.click = callback; return el; };
  const disclosure = () => { const el = node('details'); el.body = node('div'); el.append(el.body); return el; };
  const panel = api.create(initial, {node, t, button, disclosure, formatTime: value => value, get});
  const open = async () => { panel.open = true; panel.events.toggle(); await new Promise(resolve => setImmediate(resolve)); };
  return {panel, open, api, node};
}

test('opening fetches only the selected execution and earlier pages append newest-first', async () => {
  const urls = [];
  const {panel, open} = view(async url => {
    urls.push(url);
    return urls.length === 1
      ? {items:[record('new'),record('middle')],next_cursor:'older+/=',active:false}
      : {items:[record('old')],next_cursor:null,active:false};
  });
  assert.equal(urls.length, 0);
  await open();
  assert.equal(urls.length, 1);
  const query = new URLSearchParams(urls[0].split('?')[1]);
  assert.equal(query.get('attempt_id'), 'a1');
  assert.equal(query.get('task_id'), 'task');
  assert.equal(query.get('limit'), '30');
  await tree(panel).find(node => node.textContent === 'sessionOlder').events.click();
  assert.equal(new URLSearchParams(urls[1].split('?')[1]).get('cursor'),'older+/=');
  assert.deepEqual(tree(panel).filter(node => node.dataset.recordId).map(node => node.dataset.recordId), ['new','middle','old']);
});

test('polling preserves earlier pages and expanded output while updating the latest page', async () => {
  let calls = 0;
  const command = {id:'cmd',kind:'command',text:'git status',output:'clean',status:'completed',exit_code:0};
  const {panel, open} = view(async () => {
    calls++;
    return calls === 1 ? {items:[command,record('middle')],next_cursor:'cursor',active:true}
      : calls === 2 ? {items:[record('old')],next_cursor:null,active:true}
      : {items:[record('new'),command,record('middle')],next_cursor:'cursor',active:true};
  });
  await open();
  await tree(panel).find(node => node.textContent === 'sessionOlder').events.click();
  const output = tree(panel).find(node => node.className?.includes('session-output'));
  output.open = true; output.events.toggle();
  panel.update(detail(), {poll:true});
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(tree(panel).filter(node => node.dataset.recordId).map(node => node.dataset.recordId), ['new','cmd','middle','old']);
  assert.equal(tree(panel).find(node => node.className?.includes('session-output')).open,true);
  assert.equal(tree(panel).find(node => node.textContent === 'sessionOlder').hidden,true);
});

test('switching executions discards late responses for the previous selection', async () => {
  let resolveOld;
  let calls = 0;
  const initial = detail([
    {attempt_id:'a1',attempt_no:1,status:'completed',started_at:'2026-11-19'},
    {attempt_id:'a2',attempt_no:2,status:'running',started_at:'2026-11-20'},
  ]);
  const {panel, open} = view(() => ++calls === 1 ? new Promise(resolve => {resolveOld = resolve;})
    : Promise.resolve({items:[record('selected')],active:false}),initial);
  await open();
  const select = tree(panel).find(node => node.tag === 'select');
  assert.equal(select.children[0].value,'a2');
  select.value = 'a1'; select.events.change();
  await new Promise(resolve => setImmediate(resolve));
  resolveOld({items:[record('STALE')],active:true});
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(tree(panel).filter(node => node.dataset.recordId).map(node => node.dataset.recordId),['selected']);
});

test('Markdown and long messages render as safe nodes with lazy expansion', () => {
  const {api,node} = view(async () => ({}));
  const text = '# Result\n**Done** with `git status`\n- one\n```js\n<img src=x onerror=alert(1)>\n```';
  const rendered = api.markdown(text,node);
  assert.ok(tree(rendered).some(node => node.tag === 'strong' && node.textContent === 'Done'));
  assert.ok(tree(rendered).some(node => node.tag === 'code' && node.textContent === 'git status'));
  assert.ok(!tree(rendered).some(node => node.tag === 'img'));
  assert.ok(tree(rendered).some(node => node.tag === 'code' && node.textContent.includes('<img')));
  const article = api.recordView(record('long','x'.repeat(10000)),{node,t:key=>key,formatTime:v=>v},new Set());
  const expansion = tree(article).find(node => node.tag === 'details');
  assert.equal(expansion.children.length,1);
  expansion.open = true; expansion.events.toggle();
  assert.equal(expansion.children.length,2);
});

test('missing history and request errors are shown in the session panel', async () => {
  let fail = false;
  const {panel,open} = view(async () => {if (fail) throw new Error('Lost connection'); return {items:[],status:'unavailable'};});
  await open();
  assert.ok(tree(panel).some(node => node.textContent === 'sessionUnavailable'));
  fail = true;
  await tree(panel).find(node => node.textContent === 'refresh').events.click();
  assert.ok(tree(panel).some(node => node.textContent === 'Lost connection'));
});
