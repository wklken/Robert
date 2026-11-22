async page => {
  const origin = new URL(page.url()).origin;
  const nav = ["/work", "/board", "/work?bucket=history", "/timeline", "/proposals", "/operations", "/operations#knowledge"];
  const proposal = {
    proposal_id: "p-test", title: "原始单据标题", status: "pending", version: 1, revision: 1,
    source_url: "https://github.com/example/repo/issues/1", repo_id: "repo:test",
    report: { summary: "Agent's original report" },
    decisions: [{ actor: "operator", action: "hold", created_at: "2026-11-22T01:00:00Z" }],
    feedback: [{ event: "created", status: "sent" }],
  };

  await page.route("**/api/proposals**", route => {
    const url = new URL(route.request().url());
    return route.fulfill({ json: url.pathname.endsWith("/p-test") ? proposal : { proposals: [] } });
  });
  await page.route("**/api/session", route => route.fulfill({
    json: { writes_enabled: true, proposal_enabled: true, allowed_repo_ids: ["repo:test"], allowed_workers: [] },
  }));

  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  const verify = (condition, message) => { if (!condition) throw new Error(message); };
  const checkView = async (route, language, theme) => {
    await page.waitForURL(`${origin}${route}`);
    await page.locator(".global-bar .brand").waitFor();
    await page.waitForFunction(
      ([label, mode]) => document.querySelector('.underline-nav a[href="/proposals"] span')?.textContent === label
        && document.documentElement.dataset.colorMode === mode,
      [language === "zh" ? "提案" : "Proposals", theme],
    );
    const shell = await page.evaluate(() => ({
      icon: document.querySelector('link[rel="icon"]').getAttribute("href"),
      brand: document.querySelector(".global-bar .brand").innerHTML,
      nav: Array.from(document.querySelectorAll(".underline-nav a"), link => ({
        href: link.getAttribute("href"),
        glyph: link.querySelector("svg")?.innerHTML,
      })),
      active: document.querySelector('.underline-nav [aria-current="page"]')?.getAttribute("href"),
      title: document.querySelector('.underline-nav a[href="/proposals"] span').textContent,
      language: document.documentElement.lang,
      theme: document.documentElement.dataset.colorMode,
      scheme: getComputedStyle(document.documentElement).colorScheme,
    }));
    verify(shell.icon === "/assets/robert-favicon.svg", `${route}: ${shell.icon}`);
    verify(shell.nav.map(link => link.href).join() === nav.join() && shell.nav.every(link => link.glyph), `${route}: navigation differs`);
    verify(shell.active === route, `${route}: page marker is ${shell.active}`);
    verify(shell.language === (language === "zh" ? "zh-CN" : "en"), `${route}: wrong language`);
    verify(shell.title === (language === "zh" ? "提案" : "Proposals"), `${route}: proposal navigation is not translated`);
    verify(shell.theme === theme && shell.scheme === theme, `${route}: wrong theme`);
    return shell.brand;
  };

  await page.setViewportSize({ width: 1350, height: 850 });
  await page.emulateMedia({ colorScheme: "dark" });
  await page.goto(`${origin}/operations`);
  const browserLanguage = await page.evaluate(() => /^zh/i.test(navigator.language) ? "zh" : "en");
  await checkView("/operations", browserLanguage, "dark");
  await page.locator('.underline-nav a[href="/timeline"]').click();
  await checkView("/timeline", browserLanguage, "dark");
  await page.locator('.underline-nav a[href="/board"]').click();
  await checkView("/board", browserLanguage, "dark");
  await page.selectOption("#language-select", "zh");
  const brand = await checkView("/board", "zh", "dark");

  for (const route of ["/work", "/work?bucket=history", "/timeline", "/proposals", "/operations", "/operations#knowledge"]) {
    await page.locator(`.underline-nav a[href="${route}"]`).click();
    verify(await checkView(route, "zh", "dark") === brand, `${route}: brand differs`);
  }
  await page.locator('.underline-nav a[href="/proposals"]').click();
  await page.getByText("该状态暂无提案。").waitFor();
  await page.selectOption("#language-select", "en");
  await page.getByText("No proposals with this status.").waitFor();
  await page.selectOption("#theme-select", "light");

  await page.goto(`${origin}/proposals?id=p-test`);
  await page.getByRole("heading", { name: "Recommendation" }).waitFor();
  await page.getByRole("button", { name: "Approve" }).waitFor();
  await page.getByRole("textbox", { name: "Decision note" }).fill("Draft decision note");
  await page.selectOption("#language-select", "zh");
  await page.getByRole("heading", { name: "建议", exact: true }).waitFor();
  await page.getByRole("button", { name: "处理", exact: true }).waitFor();
  await page.getByText("已回评").waitFor();
  await page.getByRole("heading", { name: proposal.title }).waitFor();
  verify(await page.getByRole("textbox", { name: "决策备注" }).inputValue() === "Draft decision note", "Language switch cleared the decision draft");

  await page.selectOption("#language-select", "en");
  await page.locator('.underline-nav a[href="/timeline"]').click();
  await checkView("/timeline", "en", "light");
  await page.getByText("No completed work in this period.").waitFor();
  await page.locator('.underline-nav a[href="/work"]').click();
  await checkView("/work", "en", "light");
  await page.selectOption("#theme-select", "dark");
  await checkView("/work", "en", "dark");
  await page.selectOption("#theme-select", "light");
  await checkView("/work", "en", "light");

  await page.setViewportSize({ width: 390, height: 844 });
  for (const route of ["/timeline", "/proposals"]) {
    await page.locator(`.underline-nav a[href="${route}"]`).click();
    await checkView(route, "en", "light");
    verify(await page.locator("#language-select").isVisible(), `${route}: language control hidden on mobile`);
    verify(await page.evaluate(() => document.documentElement.scrollWidth === innerWidth), `${route}: page overflows on mobile`);
  }
  verify(errors.length === 0, errors.join("\n"));
}
