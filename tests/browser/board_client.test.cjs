const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../../src/robert_agent/web_assets/board-client.js'), 'utf8');
function client(fetch) {
  const context = vm.createContext({ fetch, AbortController, setTimeout, clearTimeout, crypto: require('node:crypto').webcrypto });
  return vm.runInContext(source + '\nBoardClient;', context);
}
const response = (body, status = 200) => ({ ok: status < 400, status, json: async () => body });

test('lost POST response retains its idempotency key until recovery succeeds', async () => {
  const keys = [];
  const api = client(async (_, options) => {
    keys.push(options.headers['x-idempotency-key']);
    assert.equal(options.headers['x-robert-csrf-token'], 'session-token');
    if (keys.length === 1) throw new TypeError('Network disconnected');
    return response({ ok: true });
  });
  await assert.rejects(api.post('/commands', { command: 'retry', expected_version: 2 }, 'session-token'));
  await api.post('/commands', { command: 'retry', expected_version: 2 }, 'session-token');
  await api.post('/commands', { command: 'retry', expected_version: 2 }, 'session-token');
  assert.equal(keys[0], keys[1]);
  assert.notEqual(keys[1], keys[2]);
});

test('conflict exposes refreshed state and ends the rejected command identity', async () => {
  const keys = [];
  const api = client(async (_, options) => {
    keys.push(options.headers['x-idempotency-key']);
    return keys.length === 1 ? response({ safe_error: 'State changed', current: { version: 3 } }, 409) : response({ ok: true });
  });
  await assert.rejects(api.post('/commands', {}, ''), e => e.status === 409 && e.payload.current.version === 3);
  await api.post('/commands', {}, '');
  assert.notEqual(keys[0], keys[1]);
});

test('timeout rejects promptly so the interface can release its busy state', async () => {
  const api = client((_, { signal }) => new Promise((resolve, reject) => {
    signal.addEventListener('abort', () => reject(Object.assign(new Error(), { name: 'AbortError' })));
  }));
  await assert.rejects(api.get('/board', {}, 5), /timed out/);
});

test('invalid JSON and server errors remain visible', async () => {
  const api = client(async () => ({ status: 502, ok: false, json: async () => { throw new Error('HTML gateway page'); } }));
  await assert.rejects(api.get('/board'), /Invalid server response.*502/);
});

test('refresh retains loaded pages and deduplicates moving cards', async () => {
  const urls = [];
  const api = client(async url => {
    urls.push(url);
    return response(urls.length === 1
      ? { items: [{ work_item_id: 'a', title: 'new' }], next_cursor: 'cursor+/=' }
      : { items: [{ work_item_id: 'a', title: 'old' }, { work_item_id: 'b' }], next_cursor: null });
  });
  const page = await api.pages('/board?limit=50', 3);
  assert.equal(page.items.length, 2);
  assert.equal(page.items[0].title, 'new');
  assert.equal(page.next_cursor, null);
  assert.equal(urls[1], '/board?limit=50&cursor=cursor%2B%2F%3D');
  assert.equal(urls.length, 2);
});

test('multi-page results use head metadata and the final pagination cursor', async () => {
  let calls = 0;
  const api = client(async () => response(++calls === 1
    ? {items:[{work_item_id:'a'}],next_cursor:'next',counts:{done:12},capacity:{running:2},attention_items:['fresh'],filters:{agents:['new']}}
    : {items:[{work_item_id:'b'}],next_cursor:'last',counts:{done:11},capacity:{running:1},attention_items:['old'],filters:{agents:['old']}}));
  const result = await api.pages('/board?limit=50',2);
  assert.equal(result.counts.done,12);
  assert.equal(result.capacity.running,2);
  assert.equal(result.attention_items[0],'fresh');
  assert.equal(result.filters.agents[0],'new');
  assert.equal(result.next_cursor,'last');
  assert.equal(result.items.length,2);
});
