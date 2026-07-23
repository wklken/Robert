"use strict";

const translations = {
  en: {
    pageTitle: "Robert Task Board", productSubtitle: "Your Repo Teammate", boardTitle: "Repository task board",
    primaryNavigation: "Primary navigation", navWorkbench: "Workbench", navWork: "Work", navBoard: "Board",
    navHistory: "History", navOperations: "Operations", navKnowledge: "Knowledge",
    repositoryContext: "Repository context", taskBoardContext: "Task board", boardControls: "Board controls",
    settings: "Settings", theme: "Theme", themeSystem: "System", themeLight: "Light", themeDark: "Dark",
    language: "Language", languageEnglish: "English", languageChinese: "Chinese",
    newTask: "New task", boardFilters: "Board filters", find: "Find", searchPlaceholder: "Title, task, branch or PR",
    repository: "Repository", allRepositories: "All repositories", agent: "Agent", allAgents: "All agents",
    priority: "Priority", allPriorities: "All priorities", needsAttention: "Needs attention", refresh: "Refresh",
    refreshBoard: "Refresh board", taskStatus: "Task status", repositoryTasks: "Repository tasks",
    columnBacklog: "Backlog", columnTodo: "TODO", columnDoing: "Doing", columnWaiting: "Waiting for you",
    columnReview: "Review", columnDone: "Done", purposeBacklog: "Ideas that are still editable",
    purposeTodo: "Ready for an Agent slot", purposeDoing: "Agent work or publication in flight",
    purposeWaiting: "A decision or reply unblocks work", purposeReview: "PR or completion needs review",
    purposeDone: "Accepted and completed", taskDetail: "Task detail", close: "Close",
    closeTaskDetail: "Close task detail", localRequest: "Local request", newRepositoryTask: "New repository task",
    closeNewTask: "Close new task", title: "Title", requirement: "Requirement", routing: "Routing", auto: "Auto",
    specificAgent: "Specific agent", afterCreation: "After creation", keepInBacklog: "Keep in backlog",
    startNow: "Start now", cancel: "Cancel", createTask: "Create task", requestFailed: "Request failed ({status})",
    openTask: "Open {title}", attentionCount: "{count} needs attention", moveToTodo: "Move to TODO",
    noTasksStage: "No tasks in this stage.", agentsRunning: "{count} agent(s) running", agentsRunningEmpty: "0 agents running", status: "Status",
    version: "Version", task: "Task", branch: "Branch", pullRequest: "Pull request", availableActions: "Available actions",
    noDescription: "No requirement description.", noAction: "No action is required in this stage.", attention: "Attention",
    noUnresolved: "No unresolved operator request.", timeline: "Timeline", noEvents: "No events recorded.", open: "Open",
    editRequirement: "Edit requirement", approve: "Approve", reply: "Reply", requestChanges: "Request changes",
    retry: "Retry", reopen: "Reopen", yourReply: "Your reply", requestedChanges: "Requested changes",
    taskTitlePrompt: "Task title", requirementPrompt: "Requirement", commandCompleted: "{action} completed.",
    taskChanged: "The task changed elsewhere. Detail was refreshed; retry your action.", boardRefreshed: "Board refreshed.",
    boardRefreshFailed: "Board refresh failed: {message}", writesDisabled: "Writes are disabled.", taskCreated: "Task created.",
    boardStartupFailed: "Board startup failed: {message}",
  },
  zh: {
    pageTitle: "Robert 任务看板", productSubtitle: "Your Repo Teammate", boardTitle: "仓库任务看板",
    primaryNavigation: "一级导航", navWorkbench: "工作台", navWork: "工作", navBoard: "任务看板",
    navHistory: "历史", navOperations: "运行状态", navKnowledge: "知识",
    repositoryContext: "仓库上下文", taskBoardContext: "任务看板", boardControls: "看板控制",
    settings: "设置", theme: "主题", themeSystem: "跟随系统", themeLight: "浅色", themeDark: "深色",
    language: "语言", languageEnglish: "English", languageChinese: "中文",
    newTask: "新建任务", boardFilters: "看板筛选", find: "搜索", searchPlaceholder: "标题、任务、分支或 PR",
    repository: "仓库", allRepositories: "全部仓库", agent: "Agent", allAgents: "全部 Agent",
    priority: "优先级", allPriorities: "全部优先级", needsAttention: "需要处理", refresh: "刷新",
    refreshBoard: "刷新看板", taskStatus: "任务状态", repositoryTasks: "仓库任务",
    columnBacklog: "待整理", columnTodo: "待办", columnDoing: "进行中", columnWaiting: "等待你确认",
    columnReview: "待评审", columnDone: "已完成", purposeBacklog: "仍可编辑的需求想法",
    purposeTodo: "等待 Agent 执行槽位", purposeDoing: "Agent 正在工作或发布",
    purposeWaiting: "等待你的决定或回复后继续", purposeReview: "PR 或完成结果等待评审",
    purposeDone: "已验收并完成", taskDetail: "任务详情", close: "关闭",
    closeTaskDetail: "关闭任务详情", localRequest: "本地需求", newRepositoryTask: "新建仓库任务",
    closeNewTask: "关闭新建任务", title: "标题", requirement: "需求描述", routing: "路由", auto: "自动",
    specificAgent: "指定 Agent", afterCreation: "创建后", keepInBacklog: "保留在 Backlog",
    startNow: "立即开始", cancel: "取消", createTask: "创建任务", requestFailed: "请求失败（{status}）",
    openTask: "打开 {title}", attentionCount: "{count} 项需要处理", moveToTodo: "移到待办",
    noTasksStage: "当前阶段没有任务。", agentsRunning: "{count} 个 Agent 运行中", agentsRunningEmpty: "0 个 Agent 运行中", status: "状态",
    version: "版本", task: "任务", branch: "分支", pullRequest: "Pull Request", availableActions: "可执行操作",
    noDescription: "暂无需求描述。", noAction: "当前阶段无需操作。", attention: "需要处理",
    noUnresolved: "没有待处理的人工请求。", timeline: "时间线", noEvents: "暂无事件记录。", open: "打开",
    editRequirement: "编辑需求", approve: "批准", reply: "回复", requestChanges: "要求修改",
    retry: "重试", reopen: "重新打开", yourReply: "你的回复", requestedChanges: "修改要求",
    taskTitlePrompt: "任务标题", requirementPrompt: "需求描述", commandCompleted: "已完成：{action}。",
    taskChanged: "任务已在其他位置发生变化。详情已刷新，请重试操作。", boardRefreshed: "看板已刷新。",
    boardRefreshFailed: "看板刷新失败：{message}", writesDisabled: "当前禁止写入。", taskCreated: "任务已创建。",
    boardStartupFailed: "看板启动失败：{message}",
  },
};

const COLUMNS = ["backlog", "todo", "doing", "waiting", "review", "done"];
const COLUMN_KEYS = {
  backlog: "columnBacklog", todo: "columnTodo", doing: "columnDoing",
  waiting: "columnWaiting", review: "columnReview", done: "columnDone",
};
const state = {
  session: null,
  board: null,
  detail: null,
  mobileColumn: new URLSearchParams(location.search).get("column") || "backlog",
  selectedItem: new URLSearchParams(location.search).get("item"),
  refreshTimer: null,
  language: "en",
  theme: "system",
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

function t(key, variables = {}) {
  const dictionary = translations[state.language] || translations.en;
  let value = dictionary[key] || translations.en[key] || key;
  Object.entries(variables).forEach(([name, replacement]) => {
    value = value.replaceAll(`{${name}}`, String(replacement));
  });
  return value;
}

function applyTranslations() {
  document.documentElement.lang = state.language === "zh" ? "zh-CN" : "en";
  document.title = t("pageTitle");
  $$('[data-i18n]').forEach((element) => { element.textContent = t(element.dataset.i18n); });
  $$('[data-i18n-placeholder]').forEach((element) => { element.placeholder = t(element.dataset.i18nPlaceholder); });
  $$('[data-i18n-aria]').forEach((element) => { element.setAttribute("aria-label", t(element.dataset.i18nAria)); });
  $("#language-select").value = state.language;
  $("#theme-select").value = state.theme;
}

function setTheme(value) {
  const next = ["system", "light", "dark"].includes(value) ? value : "system";
  state.theme = next;
  localStorage.setItem("robertWorkbenchTheme", next);
  document.documentElement.dataset.themeChoice = next;
  const prefersDark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  document.documentElement.dataset.theme = next === "system" ? (prefersDark ? "dark" : "light") : next;
  document.documentElement.dataset.colorMode = document.documentElement.dataset.theme;
  $("#theme-select").value = next;
}

function setLanguage(value) {
  state.language = translations[value] ? value : "en";
  localStorage.setItem("robertLanguage", state.language);
  applyTranslations();
  if (state.board) {
    renderFilters(state.board);
    renderBoard(state.board);
  }
  if (state.detail) renderDetail(state.detail);
}

function applyPreferences() {
  state.theme = localStorage.getItem("robertWorkbenchTheme") || "system";
  state.language = localStorage.getItem("robertLanguage") || "en";
  setTheme(state.theme);
  applyTranslations();
  if (window.matchMedia) {
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
      if (state.theme === "system") setTheme("system");
    });
  }
}

function columnLabel(column) {
  return t(COLUMN_KEYS[column] || column);
}

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined && text !== null) element.textContent = String(text);
  return element;
}

function idempotencyKey() {
  return globalThis.crypto?.randomUUID?.() || `web-${Date.now()}-${Math.random()}`;
}

function safeExternalUrl(value) {
  if (!value) return null;
  try {
    const parsed = new URL(value, location.origin);
    return ["http:", "https:"].includes(parsed.protocol) ? parsed.href : null;
  } catch (_error) {
    return null;
  }
}

function showStatus(message, isError = false) {
  const target = $("#status-message");
  target.textContent = message;
  target.classList.toggle("error", isError);
  target.classList.add("visible");
  window.clearTimeout(showStatus.timer);
  showStatus.timer = window.setTimeout(() => target.classList.remove("visible"), 3500);
}

async function getJson(url) {
  const response = await fetch(url, { cache: "no-store" });
  const payload = await response.json();
  if (!response.ok) {
    const error = new Error(payload.safe_error || t("requestFailed", { status: response.status }));
    error.payload = payload;
    error.status = response.status;
    throw error;
  }
  return payload;
}

async function postJson(url, payload) {
  const response = await fetch(url, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "x-robert-csrf-token": state.session?.csrf_token || "",
      "x-idempotency-key": idempotencyKey(),
    },
    body: JSON.stringify(payload),
  });
  const result = await response.json();
  if (!response.ok) {
    const error = new Error(result.safe_error || t("requestFailed", { status: response.status }));
    error.payload = result;
    error.status = response.status;
    throw error;
  }
  return result;
}

function updateUrl(changes) {
  const params = new URLSearchParams(location.search);
  Object.entries(changes).forEach(([key, value]) => {
    if (value) params.set(key, value);
    else params.delete(key);
  });
  history.replaceState(null, "", `${location.pathname}${params.size ? `?${params}` : ""}`);
}

function boardUrl() {
  const params = new URLSearchParams({ limit: "200" });
  const mappings = [
    ["repo", $("#repo-filter").value],
    ["agent", $("#agent-filter").value],
    ["priority", $("#priority-filter").value],
    ["q", $("#search-filter").value.trim()],
  ];
  mappings.forEach(([key, value]) => { if (value) params.set(key, value); });
  if ($("#attention-filter").checked) params.set("attention", "true");
  return `/api/board?${params}`;
}

function setOptions(select, items, valueFor, labelFor, firstLabel) {
  const previous = select.value;
  const options = [];
  if (firstLabel !== null) {
    const first = node("option", "", firstLabel);
    first.value = "";
    options.push(first);
  }
  items.forEach((item) => {
    const option = node("option", "", labelFor(item));
    option.value = valueFor(item);
    options.push(option);
  });
  select.replaceChildren(...options);
  if (options.some((option) => option.value === previous)) select.value = previous;
}

function renderFilters(data) {
  setOptions(
    $("#repo-filter"),
    data.filters.repos,
    (repo) => repo.repo_id,
    (repo) => repo.full_name,
    t("allRepositories"),
  );
  setOptions(
    $("#agent-filter"),
    data.filters.agents,
    (agent) => agent,
    (agent) => agent,
    t("allAgents"),
  );
  setOptions(
    $("#new-task-repo"),
    data.filters.repos.filter((repo) => state.session.allowed_repo_ids.includes(repo.repo_id)),
    (repo) => repo.repo_id,
    (repo) => repo.full_name,
    null,
  );
  renderContext(data);
}

function renderContext(data) {
  const repos = data.filters?.repos || [];
  const selected = repos.find((repo) => repo.repo_id === $("#repo-filter").value);
  $("#repo-context").textContent = selected?.full_name || (repos.length === 1 ? repos[0].full_name : t("taskBoardContext"));
}

function addMeta(container, text) {
  if (text === null || text === undefined || text === "") return;
  container.append(node("span", "", text));
}

function createCard(item) {
  const card = node("article", "task-card");
  card.dataset.column = item.column;
  card.dataset.priority = item.priority;
  card.dataset.workItemId = item.work_item_id;
  card.draggable = item.column === "backlog" && Boolean(state.session?.writes_enabled);

  const topline = node("div", "task-topline");
  topline.append(node("span", "repo-label", item.repo_full_name));
  topline.append(node("span", "priority", item.priority));
  const title = node("h3", "task-title");
  const openButton = node("button", "card-open", item.title);
  openButton.type = "button";
  openButton.setAttribute("aria-label", t("openTask", { title: item.title }));
  openButton.addEventListener("click", () => openDetail(item.work_item_id));
  title.append(openButton);
  card.append(topline, title);
  card.append(node("p", "task-reason", item.reason_summary));

  const meta = node("div", "task-meta");
  addMeta(meta, item.agent);
  addMeta(meta, item.branch);
  const prUrl = safeExternalUrl(item.pr?.url);
  if (prUrl) {
    const link = node("a", "", `PR #${item.pr.number}`);
    link.href = prUrl;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    meta.append(link);
  }
  card.append(meta);

  const footer = node("div", "task-footer");
  if (item.attention_signals.length) {
    footer.append(node("span", "attention-badge", t("attentionCount", { count: item.attention_signals.length })));
  }
  if (item.column === "backlog" && item.valid_commands.includes("start") && state.session?.writes_enabled) {
    const start = node("button", "start-card-button", t("moveToTodo"));
    start.type = "button";
    start.addEventListener("click", async (event) => {
      await executeCommand(item.work_item_id, "start", item.version);
    });
    footer.append(start);
  }
  card.append(footer);

  card.addEventListener("dragstart", (event) => {
    event.dataTransfer.setData("text/plain", item.work_item_id);
    event.dataTransfer.setData("application/x-robert-column", item.column);
    event.dataTransfer.effectAllowed = "move";
  });
  return card;
}

function renderBoard(data) {
  COLUMNS.forEach((column) => {
    $$(`[data-count="${column}"]`).forEach((target) => {
      target.textContent = String(data.counts[column] || 0);
    });
    const target = $(`[data-cards="${column}"]`);
    const cards = data.items.filter((item) => item.column === column).map(createCard);
    if (!cards.length) cards.push(node("p", "empty-column", t("noTasksStage")));
    target.replaceChildren(...cards);
  });
  $("#capacity").textContent = t("agentsRunning", { count: data.capacity.running });
  renderMobileColumn();
}

function renderMobileColumn() {
  if (!COLUMNS.includes(state.mobileColumn)) state.mobileColumn = "backlog";
  $$("[data-mobile-column]").forEach((button) => {
    button.classList.toggle("active", button.dataset.mobileColumn === state.mobileColumn);
  });
  $$(".board-column").forEach((column) => {
    column.classList.toggle("mobile-active", column.dataset.column === state.mobileColumn);
  });
}

function detailField(label, value, href) {
  const field = node("div", "detail-field");
  field.append(node("span", "", label));
  if (href) {
    const link = node("a", "", value || t("open"));
    link.href = href;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    field.append(link);
  } else {
    field.append(node("strong", "", value || "—"));
  }
  return field;
}

const COMMAND_KEYS = {
  edit: "editRequirement",
  start: "moveToTodo",
  approve: "approve",
  reply: "reply",
  request_changes: "requestChanges",
  retry: "retry",
  cancel: "cancel",
  reopen: "reopen",
};

function commandLabel(command) {
  return t(COMMAND_KEYS[command] || command);
}

function renderCommandComposer(container, detail, command) {
  const composer = node("form", "command-composer");
  const label = node("label", "", command === "reply" ? t("yourReply") : t("requestedChanges"));
  const textarea = node("textarea");
  textarea.rows = 4;
  textarea.required = true;
  label.append(textarea);
  const row = node("div", "command-row");
  const cancel = node("button", "quiet-button", t("cancel"));
  cancel.type = "button";
  cancel.addEventListener("click", () => composer.remove());
  const send = node("button", "primary-button", commandLabel(command));
  send.type = "submit";
  row.append(cancel, send);
  composer.append(label, row);
  composer.addEventListener("submit", async (event) => {
    event.preventDefault();
    await executeCommand(detail.work_item_id, command, detail.version, { body: textarea.value });
  });
  container.append(composer);
  textarea.focus();
}

function renderDetail(detail) {
  state.detail = detail;
  $("#drawer-title").textContent = detail.title;
  $("#drawer-repo").textContent = detail.repo_full_name;
  const content = $("#drawer-content");

  const summary = node("section", "detail-block");
  summary.append(node("p", "detail-description", detail.description || t("noDescription")));
  const grid = node("div", "detail-grid");
  grid.append(
    detailField(t("status"), columnLabel(detail.column)),
    detailField(t("priority"), detail.priority),
    detailField(t("routing"), detail.routing_mode === "manual" ? detail.agent : t("auto")),
    detailField(t("version"), detail.version),
    detailField(t("task"), detail.task_id),
    detailField(t("branch"), detail.branch),
  );
  if (detail.pr) grid.append(detailField(t("pullRequest"), `#${detail.pr.number}`, safeExternalUrl(detail.pr.url)));
  summary.append(grid);

  const actions = node("section", "detail-block");
  actions.append(node("h3", "", t("availableActions")));
  const actionRow = node("div", "command-row");
  const composerHost = node("div");
  detail.valid_commands.forEach((command) => {
    const button = node("button", "command-button", commandLabel(command));
    button.type = "button";
    button.dataset.command = command;
    button.disabled = !state.session?.writes_enabled;
    button.addEventListener("click", async () => {
      composerHost.replaceChildren();
      if (command === "reply" || command === "request_changes") {
        renderCommandComposer(composerHost, detail, command);
      } else if (command === "edit") {
        const title = window.prompt(t("taskTitlePrompt"), detail.title);
        if (title === null) return;
        const description = window.prompt(t("requirementPrompt"), detail.description || "");
        if (description === null) return;
        await executeCommand(detail.work_item_id, command, detail.version, { title, description });
      } else {
        await executeCommand(detail.work_item_id, command, detail.version);
      }
    });
    actionRow.append(button);
  });
  if (!detail.valid_commands.length) actionRow.append(node("span", "task-reason", t("noAction")));
  actions.append(actionRow, composerHost);

  const attention = node("section", "detail-block");
  attention.append(node("h3", "", t("attention")));
  if (detail.attention_signals.length) {
    detail.attention_signals.forEach((signal) => {
      const event = node("div", "timeline-event");
      event.append(node("strong", "", signal.type.replaceAll("_", " ")));
      event.append(node("p", "", signal.summary));
      attention.append(event);
    });
  } else {
    attention.append(node("p", "task-reason", t("noUnresolved")));
  }

  const history = node("section", "detail-block");
  history.append(node("h3", "", t("timeline")));
  const timeline = node("div", "timeline");
  detail.events.forEach((item) => {
    const event = node("article", "timeline-event");
    event.append(node("strong", "", item.event_type.replaceAll("_", " ")));
    if (item.body) event.append(node("p", "", item.body));
    event.append(node("time", "", `${item.actor_identity || item.actor_kind} · ${item.created_at}`));
    timeline.append(event);
  });
  if (!detail.events.length) timeline.append(node("p", "empty-column", t("noEvents")));
  history.append(timeline);

  content.replaceChildren(summary, actions, attention, history);
}

async function openDetail(workItemId) {
  try {
    const detail = await getJson(`/api/work-items/${encodeURIComponent(workItemId)}`);
    renderDetail(detail);
    state.selectedItem = workItemId;
    updateUrl({ item: workItemId });
    const drawer = $("#work-item-drawer");
    if (!drawer.open) drawer.showModal();
  } catch (error) {
    showStatus(error.message, true);
  }
}

function closeDetail() {
  const drawer = $("#work-item-drawer");
  if (drawer.open) drawer.close();
  state.selectedItem = null;
  state.detail = null;
  updateUrl({ item: null });
}

async function executeCommand(workItemId, command, version, extra = {}) {
  try {
    const detail = await postJson(`/api/work-items/${encodeURIComponent(workItemId)}/commands`, {
      command,
      expected_version: version,
      ...extra,
    });
    showStatus(t("commandCompleted", { action: commandLabel(command) }));
    renderDetail(detail);
    await refreshBoard();
  } catch (error) {
    if (error.status === 409 && error.payload?.current) {
      renderDetail(error.payload.current);
      showStatus(t("taskChanged"), true);
      return;
    }
    showStatus(error.message, true);
  }
}

async function refreshBoard({ quiet = false } = {}) {
  try {
    const data = await getJson(boardUrl());
    state.board = data;
    renderFilters(data);
    renderBoard(data);
    if (!quiet) showStatus(t("boardRefreshed"));
  } catch (error) {
    showStatus(t("boardRefreshFailed", { message: error.message }), true);
  }
}

function bindDropTargets() {
  const target = $("[data-cards=\"todo\"]");
  target.addEventListener("dragover", (event) => {
    if (event.dataTransfer.types.includes("application/x-robert-column")) {
      event.preventDefault();
      target.classList.add("drag-target");
    }
  });
  target.addEventListener("dragleave", () => target.classList.remove("drag-target"));
  target.addEventListener("drop", async (event) => {
    event.preventDefault();
    target.classList.remove("drag-target");
    if (event.dataTransfer.getData("application/x-robert-column") !== "backlog") return;
    const workItemId = event.dataTransfer.getData("text/plain");
    const item = state.board?.items.find((candidate) => candidate.work_item_id === workItemId);
    if (item) await executeCommand(item.work_item_id, "start", item.version);
  });
}

function openNewTask() {
  if (!state.session?.writes_enabled) {
    showStatus(state.session?.write_error || t("writesDisabled"), true);
    return;
  }
  $("#new-task-dialog").showModal();
}

function closeNewTask() {
  const dialog = $("#new-task-dialog");
  if (dialog.open) dialog.close();
}

async function createTask(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const data = new FormData(form);
  const payload = Object.fromEntries(data.entries());
  if (payload.routing_mode !== "manual") payload.requested_worker = null;
  try {
    const detail = await postJson("/api/work-items", payload);
    form.reset();
    $("#worker-field").hidden = true;
    closeNewTask();
    await refreshBoard({ quiet: true });
    await openDetail(detail.work_item_id);
    showStatus(t("taskCreated"));
  } catch (error) {
    showStatus(error.message, true);
  }
}

function bindControls() {
  $("#theme-select").addEventListener("change", (event) => setTheme(event.target.value));
  $("#language-select").addEventListener("change", (event) => setLanguage(event.target.value));
  $("#new-task-button").addEventListener("click", openNewTask);
  $$('[data-close-new-task]').forEach((button) => button.addEventListener("click", closeNewTask));
  $$('[data-close-drawer]').forEach((button) => button.addEventListener("click", closeDetail));
  $("#new-task-form").addEventListener("submit", createTask);
  $("#routing-mode").addEventListener("change", (event) => {
    $("#worker-field").hidden = event.target.value !== "manual";
  });
  $("#refresh-button").addEventListener("click", () => refreshBoard());
  ["#repo-filter", "#agent-filter", "#priority-filter", "#attention-filter"].forEach((selector) => {
    $(selector).addEventListener("change", () => refreshBoard({ quiet: true }));
  });
  let searchTimer;
  $("#search-filter").addEventListener("input", () => {
    window.clearTimeout(searchTimer);
    searchTimer = window.setTimeout(() => refreshBoard({ quiet: true }), 250);
  });
  $$("[data-mobile-column]").forEach((button) => {
    button.addEventListener("click", () => {
      state.mobileColumn = button.dataset.mobileColumn;
      updateUrl({ column: state.mobileColumn });
      renderMobileColumn();
    });
  });
  $("#work-item-drawer").addEventListener("cancel", (event) => {
    event.preventDefault();
    closeDetail();
  });
  $("#new-task-dialog").addEventListener("cancel", (event) => {
    event.preventDefault();
    closeNewTask();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "/" || event.metaKey || event.ctrlKey || event.altKey) return;
    if (["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName)) return;
    event.preventDefault();
    $("#search-filter").focus();
  });
  bindDropTargets();
}

async function start() {
  applyPreferences();
  bindControls();
  try {
    state.session = await getJson("/api/session");
    $("#new-task-button").disabled = !state.session.writes_enabled;
    $("#new-task-button").title = state.session.writes_enabled ? "" : state.session.write_error;
    setOptions(
      $("#worker-select"),
      state.session.allowed_workers,
      (worker) => worker,
      (worker) => worker,
      null,
    );
    await refreshBoard({ quiet: true });
    if (state.selectedItem) await openDetail(state.selectedItem);
  } catch (error) {
    showStatus(t("boardStartupFailed", { message: error.message }), true);
  }
  state.refreshTimer = window.setInterval(() => refreshBoard({ quiet: true }), 15000);
}

start();
