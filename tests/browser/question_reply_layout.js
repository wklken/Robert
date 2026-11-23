async page => {
  const origin = new URL(page.url()).origin;
  const reference = "https://github.com/example/repo/pull/60?context=" + "a".repeat(180);
  const item = {
    work_item_id: "wi-browser-question", repo_id: "repo-browser", repo_full_name: "example/repo",
    title: "Review pending", column: "waiting", version: 2,
    valid_commands: ["reply", "cancel"],
    status_summary: {state: "needs_reply", tone: "attention", owner: "operator", primary: "reply"},
    attention_signals: [{
      event_id: "question", type: "operator_question",
      summary: `The pull request is ready for review. How should it proceed?\n${reference}`,
      choices: [
        {label: "I will review the pull request soon", tradeoff: "The pull request stays open and ready for review. Robert will check again after the review is complete."},
        {label: "Ask another reviewer with merge permissions to review the pull request", tradeoff: `Give the new reviewer the link to the pull request: ${reference}`},
      ],
    }],
  };
  const responses = {
    "/api/session": {writes_enabled: true, allowed_repo_ids: [item.repo_id], allowed_workers: []},
    "/api/board": {items: [item], counts: {waiting: 1}, capacity: {running: 0}, filters: {repos: [], agents: []}},
    [`/api/work-items/${item.work_item_id}`]: item,
  };
  await page.route("**/api/**", route => {
    const path = new URL(route.request().url()).pathname;
    if (!responses[path]) throw new Error(`Unexpected browser fixture request: ${path}`);
    return route.fulfill({json: responses[path]});
  });
  try {
    await page.goto(`${origin}/board?item=${item.work_item_id}`);
    const drawer = page.locator("#work-item-drawer");
    await drawer.locator(".question-choice").last().waitFor();
    for (const width of [1440, 760, 320]) {
      await page.setViewportSize({width, height: 900});
      const failures = await drawer.evaluate(drawer => {
        const fits = (text, container) => {
          const range = document.createRange();
          range.selectNodeContents(text);
          const {left, right} = container.getBoundingClientRect();
          return [...range.getClientRects()].every(rect => rect.left >= left - 1 && rect.right <= right + 1);
        };
        const choices = [...drawer.querySelectorAll(".question-choice")];
        const question = drawer.querySelector(".question-text");
        return [
          ...choices.flatMap((button, index) =>
            [...button.children].filter(text => !fits(text, button))
              .map(text => `choice ${index + 1} (${text.className || "title"})`)),
          ...(!fits(question, question) ? ["question"] : []),
        ];
      });
      if (failures.length) throw new Error(`Text escapes its container at ${width}px: ${failures.join(", ")}`);
    }
  } finally {
    await page.unroute("**/api/**");
  }
}
