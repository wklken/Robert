async page => {
  const origin = new URL(page.url()).origin;
  await page.addInitScript(() => {
    localStorage.setItem("robertLanguage", "zh");
    Object.defineProperty(navigator, "clipboard", { value: { write: async () => { throw new Error("denied"); } } });
  });
  await page.route("**/api/timeline?**", route => route.fulfill({ json: {
    view: "day", timezone: "Asia/Shanghai", start: "2026-11-22", end: "2026-11-22",
    previous: "2026-11-21", next: "2026-11-23", entries: [{
      task_id: "task-debug-id", result_id: "result-debug-id", work_item_id: "wi-test",
      title: "统一差异检查", repository: "example/robert", output_type: "new_pr",
      source_url: "https://github.com/example/robert/issues/1", completed_at: "2026-11-22T14:20:00+08:00",
      deliveries: [{ type: "open_pr", url: "https://github.com/example/robert/pull/2" }],
    }],
  }}));
  await page.route("**/api/timeline/receipt?**", route => route.fulfill({ json: {
    summary: "合并重复的版本差异检查。\n覆盖空值和文档缺失两种情况。",
    summary_truncated: false, summary_redacted: false,
    worker: "private-worker", model: "private-model", attempt: 2,
    started_at: "2026-11-22T14:12:07+08:00", finished_at: "2026-11-22T14:20:00+08:00", duration_seconds: 473,
    verification: { passed: 12, failed: 1, skipped: 1 },
    usage: {
      scope: "request", run_count: 3, token_run_count: 2, billing_token_run_count: 2,
      input_tokens: 4470, uncached_input_tokens: 4288, output_tokens: 1300,
      cached_input_tokens: 142, cache_creation_input_tokens: 40, cost_run_count: 1, reported_cost_usd: 0.012, reported_cost_cny: "0.0805572",
      usd_to_cny: "6.7131", fx_date: "2026-11-20", fx_source: "cfets_reference",
    },
  }}));

  await page.goto(`${origin}/timeline?view=day&date=2026-11-22`);
  await page.getByRole("button", { name: "Agent 收据 · 统一差异检查" }).click();
  const receipt = page.getByRole("dialog", { name: "统一差异检查" });
  await receipt.getByText("合并重复的版本差异检查。", { exact: false }).waitFor();
  const content = await receipt.innerText();
  for (const text of ["启动于", "结束于", "14:12:07", "14:20:00", "本次尝试用时", "private-worker", "private-model", "task-debug-id"]) {
    if (!content.includes(text)) throw new Error(`Receipt lost an execution detail: ${text}`);
  }
  if (content.includes("…")) throw new Error("Receipt appended a truncation marker to the generated summary");
  await receipt.getByRole("heading", { name: "验证记录" }).waitFor();
  await receipt.getByRole("heading", { name: "请求链路用量" }).waitFor();
  await receipt.getByText("普通输入", {exact: true}).waitFor();
  await receipt.getByText("4,288", {exact: true}).waitFor();
  await receipt.getByText("缓存写入", {exact: true}).waitFor();
  await receipt.getByText("40", {exact: true}).waitFor();
  await receipt.getByText("输入/输出 2/3 · 四类齐全 2/3 · 费用 1/3 次运行").waitFor();
  await receipt.getByText(/0[.]012/).waitFor();
  await receipt.getByText(/0[.]080557/).waitFor();
  await receipt.getByText("1 美元 = 6.7131 元 · 官方中间价 · 2026-11-20").waitFor();
  await receipt.getByRole("link", { name: "交付 PR" }).waitFor();
  await receipt.getByRole("link", { name: "看板详情" }).waitFor();
  await receipt.getByRole("button", { name: "复制为图片" }).click();
  const download = receipt.getByRole("link", { name: "下载 PNG" });
  await download.waitFor();
  const png = await download.evaluate(async link => {
    const blob = await (await fetch(link.href)).blob();
    const image = await createImageBitmap(blob);
    return { type: blob.type, height: image.height };
  });
  if (png.type !== "image/png" || !png.height) throw new Error(`Invalid receipt PNG: ${JSON.stringify(png)}`);
}
