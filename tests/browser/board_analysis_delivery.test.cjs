const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function view() {
  class Element {
    constructor(tag) { this.tag = tag; this.children = []; this.dataset = {}; }
    append(...children) { this.children.push(...children); }
    setAttribute(key, value) { this[key] = value; }
    addEventListener() {}
  }
  const context = vm.createContext({ URL, URLSearchParams, location: { search: '' },
    localStorage: { getItem: () => null }, document: { createElement: tag => new Element(tag) } });
  const source = fs.readFileSync(path.join(__dirname, '../../src/robert_agent/web_assets/board.js'), 'utf8');
  vm.runInContext(source.replace(/start\(\);\s*$/, ''), context);
  vm.runInContext('state.session={writes_enabled:true,allowed_repo_ids:["repo"]};', context);
  return context;
}
const descendants = node => [node, ...node.children.flatMap(descendants)];

test('delivered analysis names the idle outcome and offers source continuation plus safe cancellation', () => {
  for (const language of ['en', 'zh']) {
    const context = view();
    context.language = language;
    context.detail = { work_item_id: 'item', repo_id: 'repo', origin_type: 'tapd', column: 'review',
      reason_code: 'analysis_delivered', valid_commands: ['cancel'],
      source: {type:'tapd_story',url:'https://tapd.woa.com/69995498/story/16'},
      publication: {delivery: {receipts:[{action_id:'action',status:'published',url:'https://tapd.woa.com/69995498/story/16',published_at:'2026-11-24T01:00:00Z'}]}},
      status_summary:{state:'analysis_delivered',tone:'success',owner:'none',needs_action:false,primary:'open_source',stage:{task:'comment_analysis'},attempts:{total:1,failed:0}} };
    vm.runInContext('state.language=language;', context);
    const live = descendants(vm.runInContext('renderLive(detail)', context));
    const text = live.map(node => node.textContent || '').join('\n');
    assert.match(text, language === 'en' ? /Analysis delivered/ : /方案已交付/);
    assert.match(text, language === 'en' ? /next instruction/ : /下一步指令/);
    assert.ok(live.some(node => node.href === context.detail.source.url));
    const actions = descendants(vm.runInContext('renderActions(detail)', context));
    assert.ok(actions.some(node => node.tag === 'a' && node.href === context.detail.source.url));
    assert.deepEqual(actions.filter(node => node.dataset.command).map(node => node.dataset.command), ['cancel']);
    assert.doesNotMatch(text, /headline_analysis_delivered|what_analysis_delivered|Stuck|卡住/);
  }
});

test('round error remains primary while process observation is visibly separate', () => {
  const context = view();
  context.detail = {status_summary:{cause:{text:'Invalid handoff: source commit differs.',source:'pre_pr_review',round_id:'round',process_observation:'process_missing_or_zombie'}}};
  const nodes = descendants(vm.runInContext('causeFact(detail, detail.status_summary)', context));
  const text = nodes.map(node => node.textContent || '').join('\n');
  assert.match(text, /Invalid handoff: source commit differs/);
  assert.match(text, /pre-publication review/i);
  assert.match(text, /Process observation/);
  assert.match(text, /process_missing_or_zombie/);
});
