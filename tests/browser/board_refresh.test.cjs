const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const flush = () => new Promise(resolve => setImmediate(resolve));
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return {promise, resolve}; };
const card = (id, extra = {}) => ({work_item_id:id, version:1, updated_at:'2026-11-24', column:'done', ...extra});
const page = (items, next_cursor = null, running = 0) => ({items, next_cursor, counts:{done:100, doing:running},
  capacity:{running}, filters:{repos:[],agents:[],priorities:[]}, attention_items:[], columns:[]});

function board(get) {
  const nodes = new Map();
  const events = {};
  const timers = new Map();
  let time = 100000;
  let timerId = 0;
  const element = () => ({value:'', children:[], events:{}, open:false, classList:{add(){},remove(){},toggle(){}},
    append(...items){this.children.push(...items);}, replaceChildren(...items){this.children=items;},
    addEventListener(event, callback){this.events[event]=callback;}, setAttribute(){}, focus(){}});
  const node = selector => { if (!nodes.has(selector)) nodes.set(selector, element()); return nodes.get(selector); };
  const document = {hidden:false, body:element(), querySelector:node, querySelectorAll:()=>[], createElement:element,
    addEventListener(event, callback){events[event]=callback;}};
  const context = vm.createContext({URLSearchParams, location:{search:''}, localStorage:{getItem:()=>null},
    document, matchMedia:()=>({addEventListener(){}}), AbortController, fetch:async url => {
      const payload=await get(url);return {ok:true,status:200,json:async()=>payload};
    },
    setTimeout(callback, delay){const id=++timerId; timers.set(id,{callback,delay}); return id;}, clearTimeout:id=>timers.delete(id),
    setInterval(callback, delay){const id=++timerId; timers.set(id,{callback,delay}); return id;},
    Date:class extends Date {static now(){return time;}}, rendered:[], details:[], notices:[], sessionPolls:[]});
  for (const name of ['board-client.js','board.js']) {
    const source = fs.readFileSync(path.join(__dirname, '../../src/robert_agent/web_assets', name),'utf8');
    vm.runInContext(source.replace(/start\(\);\s*$/, ''),context);
  }
  vm.runInContext(`state.session={writes_enabled:true}; renderFilters=()=>{};
    renderBoard=data=>rendered.push(data); feedback=(target,text)=>notices.push({target,text}); showStatus=()=>{};
    renderDetail=(detail)=>{state.detail=detail; state.detailCheckedAt=Date.now(); details.push(detail);};
    applyTranslations=()=>{}; setTheme=()=>{}; renderMobileColumn=()=>{}; updateUrl=()=>{};`, context);
  node('#detail-agent-session').update = (detail, options) => context.sessionPolls.push({detail,options});
  return {context, node, document, events, timers, advance:ms=>{time+=ms;},
    run:code=>vm.runInContext(code,context), refresh:options=>vm.runInContext(`refreshBoard(${JSON.stringify(options || {})})`,context)};
}

test('overlapping automatic polls share one request; manual requests coalesce into one follow-up', async () => {
  const wait = deferred();
  let calls = 0, active = 0, maximum = 0;
  const b = board(async () => {calls++; maximum=Math.max(maximum,++active); if(calls===1) await wait.promise; active--; return page([card('a')]);});
  const first = b.refresh({automatic:true,quiet:true});
  const poll = b.refresh({automatic:true,quiet:true});
  const manual = b.refresh();
  const repeated = b.refresh();
  await flush();
  assert.equal(calls,1);
  wait.resolve();
  await Promise.all([first,poll,manual,repeated]);
  assert.equal(calls,2);
  assert.equal(maximum,1);
  assert.equal(b.context.rendered.length,1);
});

test('a filter change during refresh discards the old response and loads the latest query', async () => {
  const wait = deferred();
  const urls = [];
  const b = board(async url => {urls.push(url); if(urls.length===1) await wait.promise; return page([card(url.includes('repo=')?'selected':'STALE')]);});
  const first = b.refresh({automatic:true,quiet:true});
  b.node('#repo-filter').value='x/y';
  const changed = b.refresh({quiet:true});
  await flush();
  assert.equal(urls.length,1);
  wait.resolve();
  await Promise.all([first,changed]);
  assert.equal(urls.length,2);
  assert.equal(new URLSearchParams(urls[1].split('?')[1]).get('repo'),'x/y');
  assert.deepEqual(b.context.rendered.map(data=>data.items[0].work_item_id),['selected']);
});

test('single-page automatic polling removes vanished cards even with unloaded history available', async () => {
  for (const cursor of [null, 'unloaded']) {
    const urls = [];
    const b = board(async url => {
      urls.push(url);
      return urls.length === 1 ? page([card('gone')], cursor)
        : {...page([]), counts:{done:0, doing:0}};
    });
    await b.run('start()');
    assert.deepEqual(Array.from(b.context.rendered.at(-1).items, item=>item.work_item_id), ['gone']);
    const timer = Array.from(b.timers.values()).at(-1);
    assert.equal(timer.delay, 45000);
    b.advance(timer.delay);
    await timer.callback();
    const data = b.context.rendered.at(-1);
    assert.equal(urls.length, 2);
    assert.equal(urls.some(url=>url.includes('cursor=')), false);
    assert.deepEqual(Array.from(data.items, item=>item.work_item_id), []);
    assert.equal(data.counts.done, 0);
    assert.equal(data.next_cursor, null);
  }
});

test('head polls replace old head cards and retain only loaded history until its deadline', async () => {
  const urls = [];
  let head = [card('gone')], history = [card('promoted'), card('old')];
  let running = 1;
  const b = board(async url => {
    urls.push(url);
    return url.includes('cursor=') ? page(history)
      : {...page(head, head.length ? 'older' : null, running), counts:{done:head.length + history.length, doing:running}};
  });
  await b.run('start()');
  await b.refresh({more:true, quiet:true});
  await b.refresh();
  const historyReads = urls.filter(url=>url.includes('cursor=')).length;
  head = [card('new'), card('promoted', {version:2})];
  let timer = Array.from(b.timers.values()).at(-1);
  assert.equal(timer.delay, 15000);
  b.advance(timer.delay);
  await timer.callback();
  let data = b.context.rendered.at(-1);
  assert.deepEqual(Array.from(data.items, item=>item.work_item_id), ['new', 'promoted', 'old']);
  assert.equal(data.items.find(item=>item.work_item_id==='promoted').version, 2);
  assert.equal(data.next_cursor, null);
  head = [];
  running = 0;
  timer = Array.from(b.timers.values()).at(-1);
  b.advance(timer.delay);
  await timer.callback();
  data = b.context.rendered.at(-1);
  assert.deepEqual(Array.from(data.items, item=>item.work_item_id), ['old']);
  assert.equal(urls.filter(url=>url.includes('cursor=')).length, historyReads);
  const deadline = Array.from(b.timers.values()).at(-1);
  assert.equal(deadline.delay, 30000);
  history = [];
  b.advance(deadline.delay);
  await deadline.callback();
  assert.deepEqual(Array.from(b.context.rendered.at(-1).items, item=>item.work_item_id), []);
  assert.equal(b.run('state.pageCount'), 1);
  assert.equal(Array.from(b.timers.values()).at(-1).delay, 45000);
});

test('automatic refresh retains loaded history with fresh metadata and rebuilds loaded pages after 60 seconds', async () => {
  const urls = [];
  const b = board(async url => {urls.push(url); return url.includes('cursor=') ? page([card('old')])
    : {...page([card('head')],'older'), counts:{done:100+urls.length},capacity:{running:urls.length},attention_items:[{work_item_id:'notice'}]};});
  await b.refresh();
  await b.refresh({more:true,quiet:true});
  assert.equal(b.context.rendered.at(-1).counts.done,101);
  assert.equal(b.context.rendered.at(-1).capacity.running,1);
  assert.equal(b.context.rendered.at(-1).attention_items[0].work_item_id,'notice');
  await b.refresh({automatic:true,quiet:true});
  assert.equal(urls.filter(url=>url.includes('cursor=')).length,1);
  let data = b.context.rendered.at(-1);
  assert.deepEqual(Array.from(data.items,item=>item.work_item_id),['head','old']);
  assert.equal(data.counts.done,103);
  assert.equal(data.capacity.running,3);
  assert.equal(data.attention_items[0].work_item_id,'notice');
  assert.equal(data.next_cursor,null);
  b.advance(60000);
  await b.refresh({automatic:true,quiet:true});
  data = b.context.rendered.at(-1);
  assert.deepEqual(Array.from(data.items,item=>item.work_item_id),['head','old']);
  assert.equal(data.next_cursor,null);
  assert.equal(urls.filter(url=>url.includes('cursor=')).length,2);
  assert.equal(b.run('state.pageCount'),2);
  await b.refresh();
  assert.equal(urls.filter(url=>url.includes('cursor=')).length,3);
});

test('unchanged selected head skips detail reads but still polls the session; markers and time trigger detail reads', async () => {
  let heartbeat = 'one', detailReads = 0;
  const b = board(async url => {
    const item = card('a',{execution:{attempt_id:'attempt',status:'running',heartbeat_at:heartbeat}});
    if(url.startsWith('/api/work-items/')){detailReads++; return {...item,tasks:[]};}
    return page([item],null,1);
  });
  b.run("state.selectedItem='a'; state.detail={work_item_id:'a'};");
  await b.refresh();
  await b.refresh({automatic:true,quiet:true});
  assert.equal(detailReads,1);
  assert.equal(b.context.sessionPolls.length,1);
  heartbeat='two';
  await b.refresh({automatic:true,quiet:true});
  assert.equal(detailReads,2);
  b.advance(60000);
  await b.refresh({automatic:true,quiet:true});
  assert.equal(detailReads,3);
  await b.refresh();
  assert.equal(detailReads,4);
});

test('selected items on retained pages are read directly on every automatic poll', async () => {
  let detailReads=0;
  const b = board(async url => {
    if(url.startsWith('/api/work-items/'))return card('old',{version:++detailReads});
    return url.includes('cursor=')?page([card('old')]):page([card('head')],'older');
  });
  await b.refresh(); await b.refresh({more:true,quiet:true});
  b.run("state.selectedItem='old'; state.detail={work_item_id:'old',version:0};");
  await b.refresh({automatic:true,quiet:true}); await b.refresh({automatic:true,quiet:true});
  assert.equal(detailReads,2);
  assert.equal(b.run('state.detail.version'),2);
});

test('selection changes discard late detail successes and errors', async () => {
  for(const fail of [false,true]) {
    const wait = deferred();
    const b = board(async url => {if(url.startsWith('/api/work-items/')){await wait.promise;if(fail)throw new Error('STALE');return card('old');}return page([card('old')]);});
    b.run("state.selectedItem='old'; state.detail={work_item_id:'old'};");
    const refresh=b.refresh(); await flush();
    b.run("state.selectedItem='new'; state.detailRequest++; state.detail={work_item_id:'new'};");
    wait.resolve(); await refresh;
    assert.equal(b.run('state.detail.work_item_id'),'new');
    assert.equal(b.context.notices.some(notice=>notice.text==='STALE'),false);
  }
});

test('poll scheduling follows active capacity and pauses while hidden or busy', async () => {
  let calls=0, running=0;
  const b=board(async()=>{calls++;return page([card('head')],null,running);});
  await b.run('start()');
  let timer=Array.from(b.timers.values()).at(-1);
  assert.ok(timer.delay>=30000&&timer.delay<=60000);
  running=1;
  await b.refresh();
  timer=Array.from(b.timers.values()).at(-1);
  assert.equal(timer.delay,15000);
  b.document.hidden=true; b.events.visibilitychange();
  await b.refresh({automatic:true,quiet:true});
  assert.equal(calls,2);
  assert.equal(b.timers.size,0);
  b.document.hidden=false; await b.events.visibilitychange(); await flush();
  assert.equal(calls,3);
  b.run('state.busy=true');
  await b.refresh({automatic:true,quiet:true});
  assert.equal(calls,3);
});

test('idle scheduling honors the loaded-page deadline and active session cadence', async () => {
  const b=board(async url=>url.includes('cursor=')?page([card('old')]):page([card('head')],'older'));
  await b.run('start()');
  await b.refresh({more:true,quiet:true});
  b.advance(45000);
  await b.refresh({automatic:true,quiet:true});
  assert.equal(Array.from(b.timers.values()).at(-1).delay,15000);
  b.node('#detail-agent-session').pollingActive=true;
  assert.equal(b.run('boardPollDelay()'),15000);
});

test('load more queued behind a poll survives coalescing and errors allow a later refresh', async () => {
  let calls=0, wait;
  const b=board(async url=>{calls++; if(wait){const pending=wait;wait=null;await pending.promise;}
    return url.includes('cursor=')?page([card('old')]):page([card('head')],'older');});
  await b.refresh();
  wait=deferred();const pending=wait;
  const poll=b.refresh({automatic:true,quiet:true});
  const more=b.refresh({more:true,quiet:true});await flush();
  assert.equal(calls,2);pending.resolve();await Promise.all([poll,more]);
  assert.equal(calls,3);assert.equal(b.run('state.board.items.length'),2);
  const broken=board(async()=>{throw new Error('offline');});
  await broken.refresh();await broken.refresh();
  assert.equal(broken.context.notices.filter(notice=>notice.text.includes('offline')).length,2);
  assert.equal(broken.node('#load-more').disabled,false);
});

test('manual full refresh dominates queued load-more in either arrival order', async () => {
  for (const moreFirst of [true,false]) {
    const wait=deferred();const urls=[];
    const b=board(async url=>{urls.push(url);if(urls.length===2)await wait.promise;
      return url.includes('cursor=')?page([card('old')]):page([card('head',{title:String(urls.length)})],'older');});
    await b.refresh();
    const poll=b.refresh({automatic:true,quiet:true});
    const requests=moreFirst
      ? [b.refresh({more:true,quiet:true}),b.refresh()]
      : [b.refresh(),b.refresh({more:true,quiet:true})];
    await flush();assert.equal(urls.length,2);
    wait.resolve();await Promise.all([poll,...requests]);
    assert.equal(urls.length,3);
    assert.equal(urls.some(url=>url.includes('cursor=')),false);
    assert.equal(b.run('state.board.items[0].title'),'3');
    assert.equal(b.run('state.pageCount'),1);
  }
});
