const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const assets = path.join(__dirname, '../../src/robert_agent/web_assets');
const html = fs.readFileSync(path.join(assets, 'timeline.html'), 'utf8');
const script = fs.readFileSync(path.join(assets, 'timeline.js'), 'utf8');

class Element {
  constructor() {
    this.children = [];
    this.events = {};
    this.attributes = {};
    this.dataset = {};
    this.classList = { toggle() {} };
    this.style = { setProperty() {} };
  }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(name, callback) { this.events[name] = callback; }
  querySelector() { return this.children[0] ||= new Element(); }
}

function timeline(search = '') {
  const nodes = new Map();
  const get = id => {
    if (!nodes.has(id)) nodes.set(id, new Element());
    return nodes.get(id);
  };
  const buttons = [...html.matchAll(/data-view="(day|week|month)" aria-pressed="(true|false)"/g)]
    .map(([, view, pressed]) => {
      const button = new Element();
      button.dataset.view = view;
      button.setAttribute('aria-pressed', pressed);
      return button;
    });
  const requests = [];
  const events = {};
  const location = { search };
  const context = vm.createContext({
    URLSearchParams, AbortController, Intl, Date, clearTimeout,
    location,
    history: { pushState: (_, __, search) => { location.search = search; } },
    window: { addEventListener: (name, callback) => { events[name] = callback; } },
    document: {
      getElementById: get,
      createElement: () => new Element(),
      querySelectorAll: () => buttons,
    },
    RobertShell: { translate() {}, bindPreferences: callback => callback('zh') },
    fetch: async url => {
      const params = new URLSearchParams(url.split('?')[1]);
      requests.push(params);
      return { ok: true, json: async () => ({
        view: params.get('view'), start: params.get('date'), end: params.get('date'),
        previous: '2026-10-01', next: '2026-12-01', entries: [],
      }) };
    },
  });
  vm.runInContext(script, context);
  const selected = () => buttons.filter(button => button.attributes['aria-pressed'] === 'true')
    .map(button => button.dataset.view);
  return { buttons, events, get, location, requests, selected };
}

test('initial HTML selects only month before JavaScript starts', () => {
  const selected = [...html.matchAll(/data-view="(day|week|month)" aria-pressed="true"/g)]
    .map(([, view]) => view);
  assert.deepEqual(selected, ['month']);
});

test('opening the timeline without a view selects and requests month', () => {
  const page = timeline();
  assert.deepEqual(page.selected(), ['month']);
  assert.equal(page.requests.length, 1);
  assert.equal(page.requests[0].get('view'), 'month');
  assert.match(page.requests[0].get('date'), /^\d{4}-\d{2}-\d{2}$/);
  assert.equal(page.requests[0].get('date'), page.get('jump-date').value);
});

test('missing, empty and unsupported views use month and preserve date and repository', () => {
  for (const view of ['', '&view=', '&view=year']) {
    const page = timeline(`?date=2026-11-22&repo=example%2Frepo${view}`);
    assert.deepEqual(page.selected(), ['month']);
    assert.equal(page.requests[0].get('view'), 'month');
    assert.equal(page.requests[0].get('date'), '2026-11-22');
    assert.equal(page.get('repository-filter').value, 'example/repo');
  }
});

test('explicit day, week and month links retain their selected view', () => {
  for (const view of ['day', 'week', 'month']) {
    const page = timeline(`?view=${view}&date=2026-11-22`);
    assert.deepEqual(page.selected(), [view]);
    assert.equal(page.requests[0].get('view'), view);
    assert.equal(page.requests[0].get('date'), '2026-11-22');
  }
});

test('view switches update selection, requests and URL; returning to the bare URL restores month', () => {
  const page = timeline('?date=2026-11-22&repo=example%2Frepo');
  for (const view of ['day', 'week', 'month']) {
    page.buttons.find(button => button.dataset.view === view).events.click();
    assert.deepEqual(page.selected(), [view]);
    assert.equal(page.requests.at(-1).get('view'), view);
    const params = new URLSearchParams(page.location.search);
    assert.equal(params.get('view'), view);
    assert.equal(params.get('date'), '2026-11-22');
    assert.equal(params.get('repo'), 'example/repo');
  }
  page.buttons.find(button => button.dataset.view === 'week').events.click();
  page.location.search = '';
  page.events.popstate();
  assert.deepEqual(page.selected(), ['month']);
  assert.equal(page.requests.at(-1).get('view'), 'month');
});
