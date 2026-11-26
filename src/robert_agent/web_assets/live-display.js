"use strict";

(() => {
  const labels = {
    en: {
      title: "Robert live display", timeline: "Timeline", fullscreen: "Full screen", exitFullscreen: "Exit full screen",
      today: "Completed today", running: "In progress", week: "Completed this week", total: "All-time completions",
      loading: "Connecting…", live: "Live", retrying: "Refresh failed. Reconnecting…",
      lastSync: (time) => `Updated ${time}`,
      close: "Close", receipt: "Agent receipt", repository: "Repository", completed: "Completed",
      worker: "Worker", model: "Model", started: "Started", finished: "Finished", duration: "Attempt duration",
      verification: "Verification", checks: (passed, failed, skipped) => `${passed} passed / ${failed} failed / ${skipped} skipped`,
      cost: "Reported cost in CNY", usage: "Input / output tokens",
      hidden: "The work summary contains sensitive information.",
      unavailable: "Receipt details are temporarily unavailable.",
      queue: (count) => `${count} ${count === 1 ? "receipt" : "receipts"} waiting`,
    },
    zh: {
      title: "Robert 任务大屏", timeline: "时间线", fullscreen: "全屏", exitFullscreen: "退出全屏",
      today: "今日完成", running: "正在处理", week: "本周完成", total: "累计完成",
      loading: "正在连接…", live: "实时更新", retrying: "刷新失败，正在重连…",
      lastSync: (time) => `更新于 ${time}`,
      close: "关闭", receipt: "Agent 收据", repository: "仓库", completed: "完成时间",
      worker: "执行 Agent", model: "模型", started: "启动时间", finished: "结束时间", duration: "本次尝试用时",
      verification: "验证结果", checks: (passed, failed, skipped) => `通过 ${passed} / 失败 ${failed} / 跳过 ${skipped}`,
      cost: "已上报费用（人民币折算）", usage: "输入 / 输出 Token",
      hidden: "处理摘要含敏感信息，已隐藏。",
      unavailable: "暂时无法加载收据详情。",
      queue: (count) => `还有 ${count} 张收据待展示`,
    },
  };

  const byId = (id) => document.getElementById(id);
  const language = document.documentElement.lang.startsWith("zh") ? "zh" : "en";
  const text = labels[language];
  const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  const clock = new Intl.DateTimeFormat(language, {
    timeZone: timezone, hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
  });
  const receiptClock = new Intl.DateTimeFormat(language, {
    timeZone: timezone, dateStyle: "medium", timeStyle: "medium", hourCycle: "h23",
  });
  const number = new Intl.NumberFormat(language);
  const duration = new Intl.NumberFormat(language, { style: "unit", unit: "second", maximumFractionDigits: 0 });
  const cost = new Intl.NumberFormat(language, { style: "currency", currency: "CNY", maximumFractionDigits: 6 });
  const dialog = byId("receipt-dialog");
  const queue = [];
  const seen = new Set();
  let initialized = false;
  let presenting = false;
  let dismissTimer;

  async function read(path, params) {
    const response = await fetch(`${path}?${new URLSearchParams(params)}`, {
      cache: "no-store", signal: AbortSignal.timeout(10_000),
    });
    if (!response.ok) throw new Error("Display data unavailable");
    return response.json();
  }

  function syncStatus(state) {
    const status = byId("sync-status");
    if (status.dataset.state !== state) {
      status.dataset.state = state;
      byId("sync-message").textContent = text[state];
    }
  }

  function updateQueueCount() {
    const pending = byId("receipt-queue");
    pending.hidden = queue.length === 0;
    pending.textContent = queue.length ? text.queue(queue.length) : "";
  }

  function receiptLine(list, label, value) {
    if (value == null || value === "") return;
    const line = document.createElement("div");
    line.className = "receipt-line";
    const term = document.createElement("dt");
    term.textContent = label;
    const detail = document.createElement("dd");
    detail.textContent = value;
    line.append(term, detail);
    list.append(line);
  }

  function renderReceipt(taskId, receipt) {
    const facts = byId("display-receipt-facts");
    facts.replaceChildren();
    byId("receipt-title").textContent = receipt?.entry.title || taskId;
    const summary = byId("display-receipt-summary");
    summary.textContent = receipt
      ? receipt.summary || (receipt.summary_redacted ? text.hidden : "")
      : text.unavailable;
    summary.hidden = !summary.textContent;
    facts.hidden = !receipt;
    if (!receipt) return;

    const time = (instant) => instant ? receiptClock.format(new Date(instant)) : null;
    receiptLine(facts, text.repository, receipt.entry.repository);
    receiptLine(facts, text.completed, time(receipt.entry.completed_at));
    receiptLine(facts, text.worker, receipt.worker);
    receiptLine(facts, text.model, receipt.model);
    receiptLine(facts, text.started, time(receipt.started_at));
    receiptLine(facts, text.finished, time(receipt.finished_at));
    if (receipt.duration_seconds != null) {
      receiptLine(facts, text.duration, duration.format(receipt.duration_seconds));
    }
    const { passed = 0, failed = 0, skipped = 0 } = receipt.verification;
    if (passed || failed || skipped) {
      receiptLine(facts, text.verification, text.checks(passed, failed, skipped));
    }
    const { input_tokens: input, output_tokens: output, reported_cost_cny: cny } = receipt.usage || {};
    if (input != null || output != null) {
      const tokens = (value) => value == null ? "—" : number.format(value);
      receiptLine(facts, text.usage, `${tokens(input)} / ${tokens(output)}`);
    }
    if (cny != null) receiptLine(facts, text.cost, cost.format(cny));
  }

  async function presentNext() {
    if (presenting || !queue.length) return;
    presenting = true;
    const [taskId, resultId] = queue.shift();
    updateQueueCount();
    const receipt = await read("/api/live-display/receipt", { task_id: taskId, result_id: resultId }).catch(() => null);
    renderReceipt(taskId, receipt);
    dialog.showModal();
    dismissTimer = setTimeout(() => dialog.close(), 30_000);
  }

  dialog.addEventListener("close", () => {
    clearTimeout(dismissTimer);
    presenting = false;
    void presentNext();
  });

  async function poll() {
    try {
      const snapshot = await read("/api/live-display", { tz: timezone });
      for (const [metric, value] of Object.entries(snapshot.counts)) {
        byId(`metric-${metric}`).textContent = number.format(value);
      }
      for (const pair of snapshot.receipts) {
        const resultId = pair[1];
        if (seen.has(resultId)) continue;
        seen.add(resultId);
        if (initialized) queue.push(pair);
      }
      initialized = true;
      const now = new Date();
      const lastSync = byId("sync-time");
      lastSync.dateTime = now.toISOString();
      lastSync.textContent = text.lastSync(clock.format(now));
      syncStatus("live");
      updateQueueCount();
      void presentNext();
    } catch {
      syncStatus("retrying");
    } finally {
      setTimeout(poll, 5_000);
    }
  }

  document.title = text.title;
  RobertShell.translate(language, text);
  const fullscreen = byId("fullscreen");
  if (document.fullscreenEnabled) {
    fullscreen.hidden = false;
    fullscreen.addEventListener("click", async () => {
      try {
        if (document.fullscreenElement) await document.exitFullscreen();
        else await document.documentElement.requestFullscreen();
      } catch {
        // The browser may decline full screen while leaving the display usable.
      }
    });
    document.addEventListener("fullscreenchange", () => {
      fullscreen.textContent = document.fullscreenElement ? text.exitFullscreen : text.fullscreen;
    });
  }
  void poll();
})();
