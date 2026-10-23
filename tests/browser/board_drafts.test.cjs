const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.events = {}; this.dataset = {}; }
  append(...els) { this.children.push(...els); }
  replaceChildren(...els) { this.children = els; }
  replaceWith(el) { this.replaced = el; }
  querySelectorAll() { return []; }
  setAttribute() {}
  addEventListener(name, fn) { this.events[name] = fn; }
  focus() {}
}
function scenario(command, detail, updated) {
  const nodes = new Map();
  const get = selector => {
    if (!nodes.has(selector)) nodes.set(selector, new Element(selector));
    return nodes.get(selector);
  };
  const context = vm.createContext({
    URLSearchParams, location: { search: '' }, localStorage: { getItem: () => null },
    document: { createElement: tag => new Element(tag), querySelector: get, querySelectorAll: () => [] },
    initial: { work_item_id: 'item', version: 1, title: 'Original', description: 'Original requirement',
      repo_full_name: 'x/y', repo_id: 'repo', attention_signals: [], column: 'backlog',
      valid_commands: [command, 'cancel'], ...detail }, updated, command,
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../../src/robert_agent/web_assets/board.js'), 'utf8').replace(/start\(\);\s*$/, ''), context);
  vm.runInContext(`
    state.session = {writes_enabled:true,allowed_repo_ids:['repo']};
    state.detail = initial;
    composeOrExecute(command);
    renderDetail({...state.detail, ...updated}, {preserve:true});
    executeCommand = (item, command, extra) => { captured = { version:item.version, command, ...extra }; };
  `, context);
  const submit = () => get('#composer-host').children[0].events.submit({ preventDefault() {} });
  return { context, submit };
}

test('editing keeps the version of the original draft after polling', () => {
  const { context, submit } = scenario('edit', {}, { version: 2, title: 'Other operator title' });
  submit();
  assert.equal(vm.runInContext('captured.version', context), 1);
  assert.equal(vm.runInContext('captured.title', context), 'Original');
});

test('reply remains bound to its original question version', () => {
  const { context, submit } = scenario('reply', { column:'waiting' }, { version:2, attention_signals:[{type:'operator_question', summary:'A different question'}] });
  submit();
  assert.equal(vm.runInContext('captured.version', context), 1);
});


test('updated draft needs explicit review before adopting the newer version', () => {
  const { context, submit } = scenario('edit', {}, { version:2, title:'Reviewed title' });
  assert.equal(vm.runInContext('state.composer.send.disabled', context), true);
  vm.runInContext('state.composer.confirm.events.click()', context);
  assert.equal(vm.runInContext('state.composer.send.disabled', context), false);
  submit();
  assert.equal(vm.runInContext('captured.version', context), 2);
});

test('a new question without a version bump still invalidates the reply draft', () => {
  const { context } = scenario('reply', { column:'waiting', attention_signals:[{event_id:'old',type:'operator_question',summary:'Old question'}] },
    { attention_signals:[{event_id:'new',type:'operator_question',summary:'New question'}] });
  assert.equal(vm.runInContext('state.composer.send.disabled', context), true);
});
