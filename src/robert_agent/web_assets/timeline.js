"use strict";

const byId = (id) => document.getElementById(id);
const labels = {
  en: {
    title: "Completion timeline", theme: "Theme", system: "System", light: "Light", dark: "Dark",
    language: "Language", navigation: "Workbench", navWork: "Work", navBoard: "Board",
    navHistory: "History", navTimeline: "Timeline", navOperations: "Operations", navKnowledge: "Knowledge",
    filters: "Timeline filters", period: "Period", day: "Day", week: "Week", month: "Month",
    previous: "Previous period", next: "Next period", today: "Today", jump: "Jump to date",
    repository: "Repository", allRepositories: "All repositories", retry: "Retry",
    copy: "Copy work list", copied: "Copied", manualCopy: "Select and copy the Markdown below.",
    close: "Close", loading: "Loading completed work…", error: "Couldn't load completed work. Try again.",
    empty: "No completed work in this period.", emptyFilter: "This repository has no completed work in this period.",
    completed: (n) => n === 1 ? "1 completed item" : `${n} completed items`,
    repositories: (n) => n === 1 ? "1 repository" : `${n} repositories`,
    requirement: "Work item", board: "Board details", tapd: "TAPD",
    comment: "Published comment", open_pr: "Pull request", push_existing_pr: "Updated pull request",
    submit_pr_review: "PR review", delivery: "Delivery",
    receipt: "Agent receipt", receiptEvidence: "Request and deliveries", receiptCompleted: "Completed",
    receiptOutput: "Result", receiptAttempt: "Attempt", receiptStart: "Started", receiptElapsed: "Attempt duration",
    receiptWorker: "Worker", receiptModel: "Model", receiptSummary: "Work summary", receiptChecks: "Verification",
    receiptLoading: "Loading execution details…", receiptError: "Couldn't load execution details.",
    receiptMissing: "This result is no longer available. Close the receipt and refresh the timeline.",
    receiptEmptySummary: "No summary was recorded.", receiptHiddenSummary: "The summary contains sensitive information.",
    receiptNoChecks: "No checks were recorded.", passed: "Passed", failed: "Failed", skipped: "Skipped",
    copyReceiptImage: "Copy as image", receiptImagePreparing: "Preparing image…",
    receiptImageCopied: "Image copied. You can paste it into chat.",
    receiptImageBlocked: "Clipboard access is unavailable. Download the image to share it.",
    receiptImageFailed: "Couldn't create the image. Try again.", saveReceiptImage: "Download PNG",
    output: {
      local_result: "Local result", comment_analysis: "Analysis", new_pr: "Implementation",
      update_existing_pr: "PR update", review_comment: "Review", pr_review_comment: "PR review",
      multi_agent_review_report: "Review report", multi_agent_vote_report: "Vote report",
    },
  },
  zh: {
    title: "工作完成时间线", theme: "主题", system: "跟随系统", light: "浅色", dark: "深色",
    language: "语言", navigation: "工作台", navWork: "工作", navBoard: "任务看板",
    navHistory: "历史", navTimeline: "时间线", navOperations: "运行状态", navKnowledge: "知识",
    filters: "时间线筛选", period: "时间粒度", day: "日", week: "周", month: "月",
    previous: "上一段时间", next: "下一段时间", today: "今天", jump: "跳转日期",
    repository: "仓库", allRepositories: "全部仓库", retry: "重试",
    copy: "复制工作清单", copied: "已复制", manualCopy: "选中并复制下方的 Markdown 工作清单。",
    close: "关闭", loading: "正在加载已完成的工作…", error: "无法加载工作清单，请重试。",
    empty: "所选时间内没有已完成的工作。", emptyFilter: "该仓库在所选时间内没有已完成的工作。",
    completed: (n) => `${n} 项已完成`, repositories: (n) => `${n} 个仓库`,
    requirement: "需求", board: "看板详情", tapd: "TAPD",
    comment: "已发布评论", open_pr: "交付 PR", push_existing_pr: "已更新 PR",
    submit_pr_review: "PR 评审", delivery: "交付物",
    receipt: "Agent 收据", receiptEvidence: "原始需求与交付物", receiptCompleted: "完成于",
    receiptOutput: "结果", receiptAttempt: "执行尝试", receiptStart: "开始于", receiptElapsed: "本次尝试用时",
    receiptWorker: "执行 Agent", receiptModel: "模型", receiptSummary: "处理摘要", receiptChecks: "验证记录",
    receiptLoading: "正在加载执行详情…", receiptError: "无法加载执行详情。",
    receiptMissing: "这条结果已不可用。关闭小票后可刷新时间线。",
    receiptEmptySummary: "这次执行未记录摘要。", receiptHiddenSummary: "摘要含敏感信息，已隐藏。",
    receiptNoChecks: "这次执行未记录验证项。", passed: "通过", failed: "失败", skipped: "跳过",
    copyReceiptImage: "复制为图片", receiptImagePreparing: "正在生成图片…",
    receiptImageCopied: "图片已复制，可粘贴到群聊。",
    receiptImageBlocked: "浏览器未允许写入剪贴板，可下载图片后分享。",
    receiptImageFailed: "生成图片失败，请重试。", saveReceiptImage: "下载 PNG",
    output: {
      local_result: "本地结果", comment_analysis: "分析", new_pr: "实现",
      update_existing_pr: "PR 更新", review_comment: "评审", pr_review_comment: "PR 评审",
      multi_agent_review_report: "评审报告", multi_agent_vote_report: "投票报告",
    },
  },
};

const datePattern = /^\d{4}-\d{2}-\d{2}$/;
const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
const darkMode = matchMedia("(prefers-color-scheme: dark)");
let language = localStorage.getItem("robertLanguage") === "zh" ? "zh" : "en";
let theme = localStorage.getItem("robertWorkbenchTheme") || "system";
let state;
let period;
let status;
let request;
let copyTimer;
let clock;
let receiptRequest;

function localToday() {
  const parts = new Intl.DateTimeFormat("en", {
    year: "numeric", month: "2-digit", day: "2-digit", timeZone: timezone,
  }).formatToParts(new Date());
  return ["year", "month", "day"].map((key) => parts.find((part) => part.type === key).value).join("-");
}

function readLocation() {
  const params = new URLSearchParams(location.search);
  const view = params.get("view");
  const date = params.get("date");
  return {
    view: ["day", "week", "month"].includes(view) ? view : "week",
    date: datePattern.test(date || "") ? date : localToday(),
    repo: params.get("repo") || "",
  };
}

function navigate(changes, reload = true) {
  state = { ...state, ...changes };
  resetCopyFeedback();
  const params = new URLSearchParams({ view: state.view, date: state.date });
  if (state.repo) params.set("repo", state.repo);
  if (params.toString() !== location.search.slice(1)) history.pushState(null, "", `?${params}`);
  if (reload) load();
  else render();
}

function setTheme() {
  document.documentElement.dataset.colorMode = theme === "system" ? (darkMode.matches ? "dark" : "light") : theme;
  byId("theme-select").value = theme;
}

function translate() {
  const text = labels[language];
  clock = new Intl.DateTimeFormat(language, { timeZone: timezone, hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
  document.documentElement.lang = language;
  document.title = `${text.title} · Robert`;
  document.querySelectorAll("[data-i18n]").forEach((node) => { node.textContent = text[node.dataset.i18n]; });
  document.querySelectorAll("[data-i18n-aria]").forEach((node) => {
    node.setAttribute("aria-label", text[node.dataset.i18nAria]);
  });
}

function dateLabel(date, options) {
  return new Intl.DateTimeFormat(language, { timeZone: "UTC", ...options }).format(new Date(`${date}T00:00:00Z`));
}

function rangeLabel() {
  if (!period) return state.date;
  const format = { year: "numeric", month: "short", day: "numeric" };
  if (period.view === "month") return dateLabel(period.start, { year: "numeric", month: "long" });
  const first = dateLabel(period.start, format);
  return period.start === period.end ? first : `${first} – ${dateLabel(period.end, format)}`;
}

function entryTime(entry) {
  return clock.format(new Date(entry.completed_at));
}

function groupByDate(entries) {
  const groups = new Map();
  for (const entry of entries) {
    const date = entry.completed_at.slice(0, 10);
    if (!groups.has(date)) groups.set(date, []);
    groups.get(date).push(entry);
  }
  return groups;
}

function visibleEntries() {
  return (period?.entries || []).filter((entry) => !state.repo || entry.repository === state.repo);
}

function element(tag, className, content) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (content) node.textContent = content;
  return node;
}

function renderCalendar(groups) {
  const calendar = byId("activity-calendar");
  calendar.replaceChildren();
  calendar.hidden = !period || state.view === "day";
  if (calendar.hidden) return;

  const first = new Date(`${period.start}T00:00:00Z`);
  const last = new Date(`${period.end}T00:00:00Z`);
  const month = state.view === "month";
  const firstWeekday = (first.getUTCDay() + 6) % 7;
  calendar.classList.toggle("month", month);
  calendar.style.setProperty("--period-days", (last - first) / 86400000 + 1);
  calendar.style.setProperty("--first-weekday", firstWeekday + 1);
  if (month) {
    const headings = element("div", "calendar-weekdays");
    headings.setAttribute("aria-hidden", "true");
    const weekday = new Date(first);
    weekday.setUTCDate(weekday.getUTCDate() - firstWeekday);
    for (let day = 0; day < 7; day++) {
      headings.append(element("span", "", dateLabel(weekday.toISOString().slice(0, 10), { weekday: "short" })));
      weekday.setUTCDate(weekday.getUTCDate() + 1);
    }
    calendar.append(headings);
  }
  for (const date = new Date(first); date <= last; date.setUTCDate(date.getUTCDate() + 1)) {
    const key = date.toISOString().slice(0, 10);
    const count = groups.get(key)?.length || 0;
    const button = element("button", "activity-day");
    button.type = "button";
    button.classList.toggle("has-work", count > 0);
    button.classList.toggle("is-anchor", key === state.date);
    const label = dateLabel(key, { weekday: "long", month: "long", day: "numeric" });
    button.setAttribute("aria-label", `${label}, ${labels[language].completed(count)}`);
    const shortDate = month ? date.getUTCDate() : dateLabel(key, { weekday: "short", day: "numeric" });
    button.append(
      element("span", "activity-date", shortDate),
      element("span", "activity-count", count ? String(count) : "·"),
    );
    button.addEventListener("click", () => {
      navigate({ view: "day", date: key });
      byId("period-title").focus();
    });
    calendar.append(button);
  }
}

function titleFor(entry) {
  return entry.title || outputLabel(entry) || labels[language].requirement;
}

function outputLabel(entry) {
  const output = labels[language].output;
  return Object.hasOwn(output, entry.output_type) ? output[entry.output_type] : "";
}

function projectFor(entry) {
  return entry.project ? `${entry.repository} (${labels[language].tapd} ${entry.project})` : entry.repository;
}

function deliveryLabel(delivery) {
  const text = labels[language];
  return ["comment", "open_pr", "push_existing_pr", "submit_pr_review"].includes(delivery.type)
    ? text[delivery.type] : text.delivery;
}

function makeLink(label, href, className) {
  const link = element("a", className, label);
  link.href = href;
  if (href.startsWith("http")) {
    link.target = "_blank";
    link.rel = "noopener noreferrer";
  }
  return link;
}

function boardPath(entry) {
  return `/board?item=${encodeURIComponent(entry.work_item_id)}`;
}

function receiptLine(list, label, value) {
  if (value === null || value === "") return;
  const line = element("div", "receipt-line");
  line.append(element("dt", "", label), element("dd", "", String(value)));
  list.append(line);
}

function receiptTime(instant) {
  return new Intl.DateTimeFormat(language, {
    timeZone: timezone, year: "numeric", month: "short", day: "numeric",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23", timeZoneName: "short",
  }).format(new Date(instant));
}

function receiptDuration(seconds) {
  const elapsed = Math.round(seconds);
  const units = [
    [Math.floor(elapsed / 3600), "hour"],
    [Math.floor(elapsed / 60) % 60, "minute"],
    [elapsed % 60, "second"],
  ];
  return units.filter(([value, unit]) => value > 0 || (unit === "second" && elapsed === 0))
    .map(([value, unit]) => new Intl.NumberFormat(language, { style: "unit", unit }).format(value)).join(" ");
}

function renderReceiptDetails(receipt) {
  const text = labels[language];
  const details = byId("receipt-details");
  const execution = element("dl", "receipt-lines");
  receiptLine(execution, text.receiptAttempt, `#${receipt.attempt}`);
  receiptLine(execution, text.receiptWorker, receipt.worker);
  receiptLine(execution, text.receiptModel, receipt.model);
  if (receipt.started_at) receiptLine(execution, text.receiptStart, receiptTime(receipt.started_at));
  if (receipt.duration_seconds !== null) {
    receiptLine(execution, text.receiptElapsed, receiptDuration(receipt.duration_seconds));
  }

  const summary = element("section", "receipt-section");
  const summaryText = receipt.summary
    ? receipt.summary + (receipt.summary_truncated ? "…" : "")
    : receipt.summary_redacted ? text.receiptHiddenSummary : text.receiptEmptySummary;
  summary.append(element("h3", "", text.receiptSummary), element("p", "receipt-summary", summaryText));

  const verification = element("section", "receipt-section");
  const checks = element("dl", "receipt-lines receipt-checks");
  for (const status of ["passed", "failed", "skipped"]) {
    if (receipt.verification[status]) receiptLine(checks, text[status], receipt.verification[status]);
  }
  verification.append(
    element("h3", "", text.receiptChecks),
    checks.childElementCount ? checks : element("p", "", text.receiptNoChecks),
  );
  details.replaceChildren(execution, summary, verification);
  details.hidden = false;
}

function resetReceiptImageFeedback() {
  byId("receipt-image-status").textContent = "";
  const download = byId("receipt-image-download");
  if (download.hasAttribute("href")) URL.revokeObjectURL(download.href);
  download.removeAttribute("href");
  download.hidden = true;
}

async function writeReceiptPng(png) {
  try {
    // Pass the promise to ClipboardItem while the button's activation is still active.
    await navigator.clipboard.write([new ClipboardItem({ "image/png": png })]);
    return true;
  } catch {
    return false;
  }
}

async function copyReceiptImage() {
  const dialog = byId("receipt-dialog");
  const button = byId("copy-receipt-image");
  const status = byId("receipt-image-status");
  const activeRequest = receiptRequest;
  const text = labels[language];
  const stillOpen = () => dialog.open && receiptRequest === activeRequest;
  button.disabled = true;
  resetReceiptImageFeedback();
  status.textContent = text.receiptImagePreparing;
  try {
    const png = renderReceiptImage(dialog);
    const [image, copied] = await Promise.all([png, writeReceiptPng(png)]);
    if (!stillOpen()) return;
    status.textContent = copied ? text.receiptImageCopied : text.receiptImageBlocked;
    if (!copied) {
      const download = byId("receipt-image-download");
      download.href = URL.createObjectURL(image);
      download.hidden = false;
    }
  } catch {
    if (stillOpen()) status.textContent = text.receiptImageFailed;
  } finally {
    if (stillOpen()) button.disabled = false;
  }
}

async function loadReceipt(entry) {
  receiptRequest?.abort();
  const current = new AbortController();
  receiptRequest = current;
  const status = byId("receipt-status");
  const retry = byId("receipt-retry");
  const text = labels[language];
  status.hidden = false;
  status.textContent = text.receiptLoading;
  retry.hidden = true;
  byId("copy-receipt-image").disabled = true;
  resetReceiptImageFeedback();
  const params = new URLSearchParams({ task_id: entry.task_id, result_id: entry.result_id });
  try {
    const response = await fetch(`/api/timeline/receipt?${params}`, { signal: current.signal, cache: "no-store" });
    if (current.signal.aborted || !byId("receipt-dialog").open) return;
    if (response.status === 404) {
      status.textContent = text.receiptMissing;
      return;
    }
    if (!response.ok) throw new Error("receipt unavailable");
    const receipt = await response.json();
    if (current.signal.aborted || !byId("receipt-dialog").open) return;
    renderReceiptDetails(receipt);
    status.hidden = true;
    byId("copy-receipt-image").disabled = false;
  } catch {
    if (current.signal.aborted || !byId("receipt-dialog").open) return;
    status.textContent = text.receiptError;
    retry.hidden = false;
  }
}

function openReceipt(entry, opener) {
  const text = labels[language];
  const dialog = byId("receipt-dialog");
  byId("receipt-title").textContent = titleFor(entry);
  byId("receipt-details").hidden = true;
  const facts = byId("receipt-facts");
  facts.replaceChildren();
  receiptLine(facts, text.repository, projectFor(entry));
  receiptLine(facts, text.receiptCompleted, receiptTime(entry.completed_at));
  receiptLine(facts, text.receiptOutput, outputLabel(entry));

  const links = byId("receipt-links");
  links.replaceChildren();
  const append = (label, url) => {
    const item = element("li");
    item.append(makeLink(label, url));
    links.append(item);
  };
  if (entry.source_url) append(text.requirement, entry.source_url);
  for (const delivery of entry.deliveries) append(deliveryLabel(delivery), delivery.url);
  if (entry.work_item_id) append(text.board, boardPath(entry));
  byId("receipt-deliveries").hidden = !links.childElementCount;
  byId("receipt-reference").textContent = `Task ${entry.task_id}`;
  byId("receipt-retry").onclick = () => loadReceipt(entry);
  opener.focus();
  dialog.showModal();
  loadReceipt(entry);
}

function renderEntry(entry) {
  const text = labels[language];
  const row = element("li", "timeline-entry");
  const time = element("time", "", entryTime(entry));
  time.dateTime = entry.completed_at;
  const detail = element("div");
  const project = element("p", "entry-project", projectFor(entry));
  const title = element("p", "entry-title");
  const trigger = element("button", "receipt-trigger");
  trigger.type = "button";
  trigger.setAttribute("aria-haspopup", "dialog");
  trigger.setAttribute("aria-label", `${text.receipt} · ${titleFor(entry)}`);
  trigger.append(element("span", "", titleFor(entry)), element("span", "receipt-trigger-label", text.receipt));
  trigger.addEventListener("click", () => openReceipt(entry, trigger));
  title.append(trigger);
  const links = element("div", "entry-links");
  const type = outputLabel(entry);
  if (type && !entry.deliveries.some((delivery) => deliveryLabel(delivery) === type)) {
    links.append(element("span", "entry-type", type));
  }
  if (entry.source_url) links.append(makeLink(text.requirement, entry.source_url));
  for (const delivery of entry.deliveries) {
    links.append(makeLink(deliveryLabel(delivery), delivery.url, "entry-delivery"));
  }
  if (entry.work_item_id) {
    links.append(makeLink(text.board, boardPath(entry)));
  }
  detail.append(project, title, links);
  row.append(time, detail);
  row.addEventListener("click", (event) => {
    if (!event.target.closest("a, button") && !window.getSelection()?.toString()) openReceipt(entry, trigger);
  });
  return row;
}

function render() {
  const text = labels[language];
  const entries = visibleEntries();
  const groups = groupByDate(entries);
  byId("period-title").textContent = rangeLabel();
  byId("jump-date").value = state.date;
  byId("next-period").disabled = !period || period.next > "9998-12-31";
  byId("previous-period").disabled = !period || period.previous < "0002-01-01";
  byId("copy-list").disabled = entries.length === 0;
  byId("retry").hidden = status !== "error";
  document.querySelectorAll("[data-view]").forEach((button) => {
    button.setAttribute("aria-pressed", String(button.dataset.view === state.view));
  });

  const repos = new Set((period?.entries || []).map((entry) => entry.repository));
  if (state.repo) repos.add(state.repo);
  const filter = byId("repository-filter");
  const all = element("option", "", text.allRepositories);
  all.value = "";
  filter.replaceChildren(all, ...[...repos].sort().map((repo) => {
    const option = element("option", "", repo);
    option.value = repo;
    return option;
  }));
  filter.value = state.repo;

  const uniqueRepos = new Set(entries.map((entry) => entry.repository));
  byId("timeline-summary").textContent = period
    ? [text.completed(entries.length), text.repositories(uniqueRepos.size), timezone].join(" · ")
    : timezone;
  const message = status ? text[status] : entries.length ? "" : state.repo ? text.emptyFilter : text.empty;
  byId("load-status").textContent = message;
  renderCalendar(groups);
  byId("completed-work").replaceChildren(...[...groups.keys()].sort().reverse().map((date) => {
    const items = groups.get(date);
    const section = element("section", "timeline-day");
    const heading = element("div", "timeline-day-heading");
    heading.append(
      element("h3", "", dateLabel(date, { month: "short", day: "numeric", weekday: "short" })),
      element("span", "day-count", text.completed(items.length)),
    );
    const list = element("ol", "timeline-entries");
    list.append(...items.map(renderEntry));
    section.append(heading, list);
    return section;
  }));
}

async function load() {
  if (request) request.abort();
  const current = new AbortController();
  request = current;
  const params = new URLSearchParams({ view: state.view, date: state.date, tz: timezone });
  period = null;
  status = "loading";
  render();
  try {
    const response = await fetch(`/api/timeline?${params}`, { signal: current.signal, cache: "no-store" });
    if (!response.ok) throw new Error("timeline unavailable");
    const data = await response.json();
    if (current.signal.aborted) return;
    period = data;
    status = "";
  } catch {
    if (current.signal.aborted) return;
    status = "error";
  }
  render();
}

function markdownText(value) {
  return value.replace(/\s+/g, " ").replace(/[\\`*_{}\[\]()<>~|#]/g, "\\$&").trim();
}

function markdownLink(label, url) {
  return `[${markdownText(label)}](<${url}>)`;
}

function workListMarkdown(entries) {
  const text = labels[language];
  const lines = [`# ${text.title} (${rangeLabel()})`, "", `${text.completed(entries.length)} · ${timezone}`];
  const groups = groupByDate(entries);
  for (const date of [...groups.keys()].sort().reverse()) {
    lines.push("", `## ${date}`);
    for (const entry of groups.get(date)) {
      const title = entry.source_url ? markdownLink(titleFor(entry), entry.source_url) : markdownText(titleFor(entry));
      const links = entry.deliveries.map((delivery) => markdownLink(deliveryLabel(delivery), delivery.url));
      if (!links.length && entry.work_item_id) {
        links.push(markdownLink(text.board, new URL(boardPath(entry), location.origin).href));
      }
      lines.push(`- ${entryTime(entry)} · **${markdownText(projectFor(entry))}** · ${title}`);
      if (links.length) lines.push(`  - ${links.join(" · ")}`);
    }
  }
  return lines.join("\n");
}

function selectCopyText(textarea, value) {
  textarea.value = value;
  textarea.focus();
  textarea.select();
}

function resetCopyFeedback() {
  clearTimeout(copyTimer);
  byId("copy-list").querySelector("span").textContent = labels[language].copy;
  byId("copy-feedback").textContent = "";
}

async function copyList() {
  const entries = visibleEntries();
  if (!entries.length) return;
  const value = workListMarkdown(entries);
  try {
    await navigator.clipboard.writeText(value);
  } catch {
    const fallback = element("textarea", "copy-fallback");
    fallback.readOnly = true;
    document.body.append(fallback);
    selectCopyText(fallback, value);
    let copied = false;
    try {
      copied = document.execCommand("copy");
    } catch {
      // Clipboard access can be blocked by the browser or the host.
    } finally {
      fallback.remove();
    }
    byId("copy-list").focus();
    if (!copied) {
      byId("copy-dialog").showModal();
      selectCopyText(byId("copy-text"), value);
      return;
    }
  }
  const label = byId("copy-list").querySelector("span");
  label.textContent = labels[language].copied;
  byId("copy-feedback").textContent = labels[language].copied;
  clearTimeout(copyTimer);
  copyTimer = setTimeout(resetCopyFeedback, 1600);
}

function start() {
  state = readLocation();
  document.querySelectorAll("[data-view]").forEach((button) => {
    button.addEventListener("click", () => {
      if (button.dataset.view !== state.view) navigate({ view: button.dataset.view });
    });
  });
  byId("previous-period").addEventListener("click", () => navigate({ date: period.previous }));
  byId("next-period").addEventListener("click", () => navigate({ date: period.next }));
  byId("today").addEventListener("click", () => navigate({ date: localToday() }));
  byId("jump-date").addEventListener("change", (event) => {
    if (event.target.validity.valid) navigate({ date: event.target.value });
  });
  byId("repository-filter").addEventListener("change", (event) => navigate({ repo: event.target.value }, false));
  byId("retry").addEventListener("click", load);
  byId("copy-list").addEventListener("click", copyList);
  byId("copy-receipt-image").addEventListener("click", copyReceiptImage);
  const receiptDialog = byId("receipt-dialog");
  receiptDialog.addEventListener("click", (event) => {
    if (event.target === receiptDialog) receiptDialog.close();
  });
  receiptDialog.addEventListener("close", () => {
    if (receiptDialog.open) return;
    receiptRequest?.abort();
    resetReceiptImageFeedback();
  });
  byId("language-select").value = language;
  byId("language-select").addEventListener("change", (event) => {
    language = event.target.value;
    localStorage.setItem("robertLanguage", language);
    translate();
    render();
  });
  byId("theme-select").addEventListener("change", (event) => {
    theme = event.target.value;
    localStorage.setItem("robertWorkbenchTheme", theme);
    setTheme();
  });
  darkMode.addEventListener("change", setTheme);
  window.addEventListener("popstate", () => {
    const previous = state;
    state = readLocation();
    resetCopyFeedback();
    if (state.view !== previous.view || state.date !== previous.date) load();
    else render();
  });
  setTheme();
  translate();
  load();
}

start();
