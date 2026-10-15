async page => {
  const origin = new URL(page.url()).origin;
  const github = 'https://github.com/example/repo/issues/11';
  const pr = 'https://github.com/example/repo/pull/12';
  const task = {task_id:'task-links', workstream_id:'ws-links', source_title:'Link fixture',
    source_type:'issue', source_number:11, github_url:github, operator_state:'completed',
    priority:'P1', route_id:'review', updated_at:'2026-10-15T00:00:00Z', artifacts:{}};
  const workItem = {id:'ws-links', source:{title:'Link fixture',repo_full_name:'example/repo',
    type:'issue', number:11, url:github}, bucket:'history', priority:'P1',
    reason_code:'inactive', signals:{}, agent:{task_id:'task-links'}, tasks:[task],
    operator_state:{bucket:'history'}, phases:[], actions:[], artifacts:[]};
  const boardItem = {work_item_id:'wi-links', title:'Link fixture', repo_id:'repo-links',
    repo_full_name:'example/repo', column:'review', priority:'P1', agent:'Auto', routing_mode:'auto',
    version:1, task_id:'task-links', attention_signals:[], valid_commands:['request_changes'],
    source:{type:'issue',number:11,url:github}, pr:{number:12,state:'open',url:pr}, events:[]};
  const dashboard = {summary:{}, repos:[], recent_tasks:[task], work_items:[{
    id:'ws-links', source_title:'Link fixture', github_url:github, task_count:1,
    top_operator_state:'completed', tasks:[task]}], notification_center:[],
    daemon:{}, recent_runs:[], used_skill_counts:{}, knowledge_review:{counts:{}}};
  const route = async request => {
    const path = new URL(request.request().url()).pathname;
    let payload;
    if (path === '/data.json') payload = dashboard;
    else if (path === '/api/session') payload = {writes_enabled:true,allowed_repo_ids:['repo-links'],allowed_workers:[]};
    else if (path === '/api/board') payload = {items:[boardItem],counts:{review:1},capacity:{running:0},filters:{repos:[],agents:[]}};
    else if (path === '/api/work-items') payload = {items:[workItem], counts:{history:1}, actors:[], repositories:[]};
    else if (path.endsWith('/wi-links')) payload = boardItem;
    else if (path.endsWith('/ws-links')) payload = workItem;
    else throw new Error(`Unexpected request ${path}`);
    await request.fulfill({contentType:'application/json',body:JSON.stringify(payload)});
  };
  await page.route('**/api/**',route);
  await page.route('**/data.json',route);
  await page.context().route('https://github.com/**',async route => route.fulfill({body:'GitHub navigation fixture'}));
  async function newTab(link, expected, keyboard = false) {
    await link.waitFor();
    if (await link.getAttribute('target') !== '_blank') throw new Error('GitHub links must open a new tab.');
    if (!(await link.getAttribute('rel')).includes('noopener')) throw new Error('GitHub tabs require noopener.');
    const before = page.url();
    const popupPromise = page.waitForEvent('popup');
    if (keyboard) { await link.focus(); await link.press("Enter"); }
    else await link.click();
    const popup = await popupPromise;
    await popup.waitForURL(expected);
    if (page.url() !== before) throw new Error('Opening GitHub must not navigate or select a task in the original tab.');
    await popup.close();
  }
  try {
    await page.goto(`${origin}/board`);
    await newTab(page.locator('.task-card a').filter({hasText:'Issue #11'}),github);
    await newTab(page.locator('.task-card a').filter({hasText:'PR #12'}),pr);
    await page.locator('.task-card .card-open').click();
    await newTab(page.locator('#work-item-drawer .command-row a'),pr);
    await page.goto(`${origin}/?bucket=history`);
    await newTab(page.locator('.work-entry > a'),github);
    await newTab(page.locator('.work-entry > a'),github,true);
    await page.locator('[data-workstream-id="ws-links"]').click();
    await newTab(page.locator('#detail-panel .detail-actions a'),github);
    await page.goto(`${origin}/operations#work`);
    await newTab(page.locator('#task-list .task-card > a'),github);
    await page.locator('#task-list [data-work-item-id="ws-links"]').click();
    await newTab(page.locator('#work-task-list .task-card > a'),github);
    await newTab(page.locator('#task-detail .detail-actions a'),github);
    return 'Board issue/PR, workbench list/detail, and operations work/task links open GitHub in separate tabs.';
  } finally {
    await page.unroute('**/api/**',route);
    await page.unroute('**/data.json',route);
    await page.context().unroute('https://github.com/**');
  }
}
