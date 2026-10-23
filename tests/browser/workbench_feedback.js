async page => {
  const origin = new URL(page.url()).origin;
  const source = {type:'issue',number:3404,title:'Feedback fixture',repo_full_name:'org/repo',url:'https://github.com/org/repo/issues/3404'};
  const item = {id:'ws-feedback',source,bucket:'history',reason_code:'inactive',signals:{},agent:{},workstream_lifecycle:'completed',work_item_id:'wi-feedback',
    lifecycle:{column:'review',reason_code:'open_pr',pr:{number:3405,url:'https://github.com/org/repo/pull/3405',state:'open'}},operator_state:{bucket:'history',reason_code:'inactive'},tasks:[]};
  const route = async request => {
    const url = new URL(request.request().url());
    let payload;
    if (url.pathname === '/api/session') payload = {writes_enabled:true,allowed_repo_ids:['repo'],allowed_workers:[]};
    else if (url.pathname === '/api/work-items') {
      if (request.request().method() === 'POST') throw new Error('Invalid forms must not create tasks');
      payload = {items:url.searchParams.get('q') ? [] : [item],counts:{history:1},repositories:[{full_name:'org/repo'}],actors:[]};
    } else if (url.pathname === '/api/board') payload = {items:[],counts:{},capacity:{running:0},filters:{repos:[{repo_id:'repo',full_name:'org/repo'}],agents:[]}};
    else payload = item;
    await request.fulfill({contentType:'application/json',body:JSON.stringify(payload)});
  };
  await page.route('**/api/**',route);
  try {
    await page.addInitScript(() => localStorage.setItem('robertLanguage','zh'));
    await page.goto(`${origin}/work?bucket=history`);
    await page.locator('.work-row').waitFor();
    await page.locator('#detail-panel').getByRole('link',{name:'PR #3405 · 进行中'}).waitFor();
    if (!(await page.locator('#detail-panel').innerText()).includes('Issue #3404')) throw new Error('Source identity incorrect');
    for (const width of [1440,1040,960,768,375]) {
      await page.setViewportSize({width,height:900});
      for (const id of ['work-search','repo-filter','actor-filter','bucket-filter','sort-filter']) {
        const control = page.locator(`#${id}`);
        if (!await control.isVisible()) throw new Error(`${id} hidden at ${width}`);
        const bounds=await control.boundingBox();
        const column=await page.locator('.work-column').boundingBox();
        if (bounds.x<0 || bounds.x+bounds.width>column.x+column.width+1) throw new Error(`${id} overflows at ${width}`);
        await control.focus();
        if (!await control.evaluate(el=>el===document.activeElement)) throw new Error(`${id} cannot receive focus`);
      }
    }
    await page.setViewportSize({width:1440,height:1000});
    await page.locator('#work-search').fill('no-match');
    await page.getByText('没有符合筛选条件的工作',{exact:true}).waitFor();
    for (const id of ['detail-panel','drawer-content']) if ((await page.locator(`#${id}`).innerText()).includes('Feedback fixture')) throw new Error('Stale detail survived filtering');
    await page.locator('#work-search').fill('');
    await page.locator('.work-row').waitFor();
    await page.locator('.work-row').click();
    const shared=page.url();
    await page.goto(shared);
    await page.locator('#detail-panel').getByRole('heading',{name:'Feedback fixture'}).waitFor();
    await page.goto(`${origin}/board`);
    await page.locator('#new-task-button').click();
    await page.locator('#new-task-form button[type=submit]').click();
    const title=page.locator('#new-task-form [name=title]');
    if (!await title.evaluate(el=>el.validationMessage.includes('标题'))) throw new Error('Title validation is not localized');
    await title.fill('Test title');
    await page.locator('#new-task-form button[type=submit]').click();
    if (!await page.locator('#new-task-form [name=description]').evaluate(el=>el.validationMessage.includes('需求'))) throw new Error('Description validation missing');
    return 'Source and PR identity, review state, empty filters, share revisit, five responsive widths, focus visibility, and localized required title/description passed.';
  } finally { await page.unroute('**/api/**',route); }
}
