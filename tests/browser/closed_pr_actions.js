async page => {
  const baseURL = new URL(page.url()).origin;
  const item = {
    work_item_id: 'wi-browser-closed-pr', repo_id: 'repo-browser', repo_full_name: 'example/repo',
    title: 'Closed PR awaiting cancellation', priority: 'P1', column: 'review', agent: 'Auto',
    routing_mode: 'auto', branch: null, task_id: 'task-browser', version: 2,
    reason_summary: 'Pull request closed without merge.', reason_code: 'unmerged_pr_closed',
    pr: {number: 3353, state: 'closed', url: 'https://github.com/example/repo/pull/3353'},
    valid_commands: ['request_changes', 'cancel'],
    attention_signals: [{type: 'unmerged_pr_closed', summary: 'Pull request closed without merge.'}],
    description: '', events: [],
  };
  const openItem = {...item, work_item_id: 'wi-browser-open-pr', title: 'Open PR',
    pr: {...item.pr, number: 3354, state: 'open'}, valid_commands: ['request_changes'], attention_signals: []};
  let writesEnabled = true;
  let canceled = false;
  let rejectCommand = true;
  let command;
  const card = '.task-card[data-work-item-id="wi-browser-closed-pr"]';
  const dialogCancel = '#work-item-drawer button[data-command="cancel"]';
  const board = () => ({items: canceled ? [openItem] : [item, openItem],
    counts: {review: canceled ? 1 : 2}, capacity: {running: 0},
    filters: {repos: [{repo_id:'repo-browser',full_name:'example/repo'}], agents:['Auto']}});
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    let payload;
    let status = 200;
    if (path === '/api/session') {
      payload = {writes_enabled:writesEnabled, allowed_repo_ids:['repo-browser'], allowed_workers:['default'], csrf_token:'browser-fixture'};
    } else if (path === '/api/board') {
      payload = board();
    } else if (path.endsWith('/commands')) {
      command = route.request().postDataJSON();
      if (command.command !== 'cancel' || command.expected_version !== 2) throw new Error('Cancel must send the current item version.');
      if (rejectCommand) { status = 403; payload = {safe_error:'invalid Origin'}; }
      else { canceled = true; payload = {...item, column:'history', valid_commands:[], version:3}; }
    } else if (path === '/api/work-items/wi-browser-closed-pr') {
      payload = item;
    } else {
      throw new Error(`Unexpected browser fixture request: ${path}`);
    }
    await route.fulfill({status, contentType:'application/json', body:JSON.stringify(payload)});
  });
  try {
    await page.goto(`${baseURL}/board`);
    await page.locator(`${card} .card-open`).waitFor();
    const cancel = page.locator(`${card} button[data-command="cancel"]`);
    if (await cancel.count() !== 1) throw new Error('A closed PR card must expose Cancel without opening its detail.');
    if (await page.locator('.task-card[data-work-item-id="wi-browser-open-pr"] button[data-command="cancel"]').count()) throw new Error('Open PR cards must retain their existing actions.');
    await cancel.click();
    await page.waitForFunction(() => document.querySelector('#status-message').textContent === 'invalid Origin');
    await page.locator(`${card} .card-open`).click();
    await page.locator(dialogCancel).click();
    await page.waitForFunction(() => document.querySelector('#status-message').textContent === 'invalid Origin');
    await page.locator('#work-item-drawer #status-message.visible').waitFor();
    await page.locator('[data-close-drawer]').click();
    await page.waitForFunction(() => !!document.querySelector('body > #status-message'));
    writesEnabled = false;
    await page.reload();
    await cancel.waitFor();
    if (!await cancel.isDisabled()) throw new Error('Read-only sessions must disable cancellation.');
    writesEnabled = true;
    rejectCommand = false;
    await page.reload();
    await cancel.click();
    await page.locator(card).waitFor({state:'detached'});
    return 'Closed PR card cancellation, modal errors, read-only controls, and successful board removal passed.';
  } finally {
    await page.unroute('**/api/**');
  }
}
