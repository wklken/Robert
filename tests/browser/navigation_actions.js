async page => {
  const origin = new URL(page.url()).origin;
  const item = {
    work_item_id:'wi-review-style', repo_id:'repo-browser', repo_full_name:'example/repo',
    title:'Review action styling', priority:'P1', column:'review', agent:'Auto',
    routing_mode:'auto', version:1, description:'', events:[], attention_signals:[],
    valid_commands:['request_changes','cancel'],
    pr:{number:12,state:'open',url:'https://github.com/example/repo/pull/12'},
  };
  const route = async route => {
    const path = new URL(route.request().url()).pathname;
    const data = path === '/api/session'
      ? {writes_enabled:true,allowed_repo_ids:['repo-browser'],allowed_workers:[]}
      : path === '/api/board'
        ? {items:[item],counts:{review:1},capacity:{running:0},filters:{repos:[],agents:[]}}
        : path === '/api/work-items/wi-review-style'
          ? item
          : {items:[],counts:{},actors:[],repositories:[]};
    await route.fulfill({contentType:'application/json',body:JSON.stringify(data)});
  };
  await page.route('**/api/**', route);
  try {
    await page.setViewportSize({width:1200,height:850});
    const tabs = [];
    for (const language of ['en','zh']) {
      await page.goto(`${origin}/board`);
      await page.selectOption('#language-select',language);
      for (const path of ['/board','/work','/work?bucket=history','/operations','/operations#knowledge']) {
        await page.goto(`${origin}${path}`);
        const tab = page.locator('.underline-nav a[href="/work"]');
        const label = await tab.locator('[data-i18n]').textContent();
        const expected = language === 'zh' ? '工作' : 'Work';
        if (label !== expected) throw new Error(`${path}: expected ${expected}, got ${label}`);
        if (await tab.locator('svg').count() !== 1) throw new Error(`${path}: missing work icon`);
        tabs.push({language,path,label});
      }
    }
    await page.goto(`${origin}/board`);
    await page.locator('.task-card .card-open').click();
    const action = page.locator('#detail-actions button[data-command="request_changes"]');
    const cancel = page.locator('#detail-actions button[data-command="cancel"]');
    for (const theme of ['light','dark']) {
      await page.selectOption('#theme-select',theme);
      await page.mouse.move(0,0);
      const styles = await action.evaluate(e => {
        const style=getComputedStyle(e);
        const probe=document.createElement('span');
        probe.style.color='var(--fgColor-default)';
        e.append(probe);
        const expected=getComputedStyle(probe).color;
        probe.remove();
        return {primary:e.classList.contains('primary-button'),color:style.color,expected};
      });
      if (styles.primary || styles.color !== styles.expected) throw new Error(`${theme}: request changes must be a neutral secondary action: ${JSON.stringify(styles)}`);
      const danger = await cancel.evaluate(e => {
        const probe=document.createElement('span');probe.style.color='var(--fgColor-danger)';e.append(probe);
        const result=getComputedStyle(e).color===getComputedStyle(probe).color;probe.remove();return result;
      });
      if (!danger) throw new Error(`${theme}: cancel must retain danger text`);
      await action.hover();
      if (await action.isDisabled()) throw new Error('Action must stay usable');
    }
    return {tabs,buttons:'Neutral request changes and danger cancellation verified in light and dark themes'};
  } finally {
    await page.unroute('**/api/**',route);
  }
}
