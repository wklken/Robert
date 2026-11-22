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
    started_at: "2026-11-22T14:12:07+08:00", duration_seconds: 473,
    verification: { passed: 12, failed: 1, skipped: 1 },
  }}));

  await page.goto(`${origin}/timeline?view=day&date=2026-11-22`);
  await page.getByRole("button", { name: "Agent 收据 · 统一差异检查" }).click();
  const receipt = page.getByRole("dialog", { name: "统一差异检查" });
  await receipt.getByText("合并重复的版本差异检查。", { exact: false }).waitFor();
  const content = await receipt.innerText();
  for (const text of ["private-worker", "private-model", "task-debug-id", "本次尝试用时"]) {
    if (content.includes(text)) throw new Error(`Receipt exposes execution detail: ${text}`);
  }
  await receipt.getByText("12 项通过 · 1 项失败 · 1 项跳过", { exact: false }).waitFor();
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
  if (png.type !== "image/png" || png.height > 850) throw new Error(`Invalid or oversized receipt PNG: ${JSON.stringify(png)}`);
}
