const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function display(snapshots, { missingReceipt = null } = {}) {
  class Element {
    constructor() {
      this.dataset = {};
      this.children = [];
      this.events = {};
      this.open = false;
    }
    addEventListener(name, listener) { this.events[name] = listener; }
    append(...children) { this.children.push(...children); }
    replaceChildren(...children) { this.children = children; }
    showModal() { assert.equal(this.open, false); this.open = true; }
    close() { this.open = false; this.events.close(); }
  }

  const nodes = new Map();
  const get = id => {
    if (!nodes.has(id)) nodes.set(id, new Element());
    return nodes.get(id);
  };
  const requests = [];
  const timers = new Map();
  let sequence = 0;
  let currentTime = 0;
  const context = vm.createContext({
    AbortSignal, Date, Intl, URLSearchParams,
    RobertShell: { translate() {} },
    document: {
      documentElement: { lang: 'zh-CN' },
      getElementById: get,
      createElement: () => new Element(),
    },
    setTimeout: (run, delay) => {
      timers.set(++sequence, { at: currentTime + delay, run });
      return sequence;
    },
    clearTimeout: id => timers.delete(id),
    fetch: async url => {
      if (!url.startsWith('/api/live-display/receipt?')) {
        assert.match(url, /^\/api\/live-display\?tz=/);
        const snapshot = snapshots.shift();
        return { ok: Boolean(snapshot), json: async () => snapshot };
      }
      const params = new URLSearchParams(url.split('?')[1]);
      const resultId = params.get('result_id');
      requests.push(resultId);
      return {
        ok: resultId !== missingReceipt,
        json: async () => ({
          entry: { title: resultId, completed_at: '2026-11-26T03:00:00Z' },
          verification: {},
        }),
      };
    },
  });
  function settle() { return new Promise(resolve => setImmediate(resolve)); }
  async function advance(ms) {
    const end = currentTime + ms;
    while (true) {
      const next = [...timers].sort((a, b) => a[1].at - b[1].at)[0];
      if (!next || next[1].at > end) break;
      timers.delete(next[0]);
      currentTime = next[1].at;
      next[1].run();
      await settle();
    }
    currentTime = end;
  }
  const script = fs.readFileSync(path.join(__dirname, '../../src/robert_agent/web_assets/live-display.js'), 'utf8');
  vm.runInContext(script, context);
  return { get, requests, advance, settle };
}

function snapshot(receipts, running = 0) {
  return {
    counts: { total: receipts.length, week: 0, today: 0, running },
    receipts: receipts.map(id => [`task-${id}`, id]),
  };
}

test('establishes a baseline, deduplicates repeated polls and queues receipts for 30 seconds each', async () => {
  const page = display([
    snapshot(['old'], 2),
    snapshot(['old', 'first', 'second'], 1),
    snapshot(['old', 'first', 'second']),
    snapshot(['third', 'old', 'first', 'second']),
  ]);
  const dialog = page.get('receipt-dialog');
  await page.settle();
  assert.equal(page.get('metric-running').textContent, '2');
  assert.equal(dialog.open, false);
  await page.advance(5_000);
  assert.equal(page.get('metric-total').textContent, '3');
  assert.equal(page.get('receipt-title').textContent, 'first');
  assert.deepEqual(page.requests, ['first']);
  await page.advance(29_999);
  assert.equal(page.get('receipt-title').textContent, 'first');
  await page.advance(1);
  assert.equal(page.get('receipt-title').textContent, 'second');
  await page.advance(30_000);
  assert.equal(page.get('receipt-title').textContent, 'third');
  await page.advance(30_000);
  assert.equal(dialog.open, false);
  assert.deepEqual(page.requests, ['first', 'second', 'third']);
});

test('preserves counts on a failed poll and advances past an unavailable receipt', async () => {
  const page = display([
    snapshot(['old'], 1), null, snapshot(['old', 'missing', 'later']),
  ], { missingReceipt: 'missing' });
  await page.settle();
  await page.advance(5_000);
  assert.equal(page.get('metric-running').textContent, '1');
  assert.equal(page.get('sync-status').dataset.state, 'retrying');
  await page.advance(5_000);
  assert.equal(page.get('sync-status').dataset.state, 'live');
  assert.equal(page.get('receipt-title').textContent, 'task-missing');
  page.get('receipt-dialog').close();
  await page.settle();
  assert.equal(page.get('receipt-title').textContent, 'later');
  assert.deepEqual(page.requests, ['missing', 'later']);
});
