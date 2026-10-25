"use strict";

const translations = {
  en: {
    pageTitle: "Robert Task Board",
    productSubtitle: "Your Repo Teammate",
    filtersToggle: "Filters",
    boardTitle: "Repository task board",
    primaryNavigation: "Primary navigation",
    navWorkbench: "Workbench",
    requiredHelp: "Repository, title and requirement are required.", requiredField: "Required: {field}", requiredMark: "(required)", boardScope: "Task lifecycle includes local tasks and GitHub work. Needs attention counts tasks; the Work queue counts GitHub workflows.",
    navWork: "Work",
    navBoard: "Board",
    navHistory: "History",
    navOperations: "Operations",
    navKnowledge: "Knowledge",
    repositoryContext: "Repository context",
    taskBoardContext: "Task board",
    boardControls: "Board controls",
    settings: "Settings",
    theme: "Theme",
    themeSystem: "System",
    themeLight: "Light",
    themeDark: "Dark",
    language: "Language",
    languageEnglish: "English",
    languageChinese: "Chinese",
    newTask: "New task",
    boardFilters: "Board filters",
    find: "Find",
    searchPlaceholder: "Title, task, branch or PR",
    repository: "Repository",
    allRepositories: "All repositories",
    agent: "Agent",
    allAgents: "All agents",
    priority: "Priority",
    allPriorities: "All priorities",
    needsAttention: "Needs attention",
    refresh: "Refresh",
    refreshBoard: "Refresh board",
    taskStatus: "Task status",
    repositoryTasks: "Repository tasks",
    columnBacklog: "Backlog",
    columnTodo: "TODO",
    columnDoing: "Doing",
    columnWaiting: "Waiting for you",
    columnReview: "Review",
    columnDone: "Done",
    purposeBacklog: "Ideas that are still editable",
    purposeTodo: "Ready for an Agent slot",
    purposeDoing: "Agent work or publication in flight",
    purposeWaiting: "A decision or reply unblocks work",
    purposeReview: "PR or completion needs review",
    purposeDone: "Accepted and completed",
    taskDetail: "Task detail",
    close: "Close",
    closeTaskDetail: "Close task detail",
    localRequest: "Local request",
    newRepositoryTask: "New repository task",
    closeNewTask: "Close new task",
    title: "Title",
    requirement: "Requirement",
    routing: "Routing",
    auto: "Auto",
    specificAgent: "Specific agent",
    afterCreation: "After creation",
    keepInBacklog: "Keep in backlog",
    startNow: "Start now",
    cancel: "Cancel",
    cancelTask: "Cancel task",
    openGithub: "Open on GitHub ↗",
    createTask: "Create task",
    requestFailed: "Request failed ({status})",
    openTask: "Open {title}",
    attentionCount: "{count} needs attention",
    moveToTodo: "Move to TODO",
    noTasksStage: "No tasks in this stage.",
    agentsRunning: "{count} agent(s) running",
    agentsRunningEmpty: "0 agents running",
    status: "Status",
    version: "Version",
    task: "Task",
    branch: "Branch",
    pullRequest: "Pull request",
    availableActions: "Available actions",
    noDescription: "No requirement description.",
    noAction: "No action is required in this stage.",
    attention: "Attention",
    noUnresolved: "No unresolved operator request.",
    timeline: "Timeline",
    noEvents: "No events recorded.",
    open: "Open",
    editRequirement: "Edit requirement",
    approve: "Approve",
    reply: "Reply",
    requestChanges: "Request changes",
    retry: "Retry",
    reopen: "Reopen",
    yourReply: "Your reply",
    requestedChanges: "Requested changes",
    taskTitlePrompt: "Task title",
    requirementPrompt: "Requirement",
    commandCompleted: "{action} completed.",
    taskChanged:
      "The task changed elsewhere. Detail was refreshed; retry your action.",
    boardRefreshed: "Board refreshed.",
    boardRefreshFailed: "Board refresh failed: {message}",
    writesDisabled: "Writes are disabled.",
    taskCreated: "Task created.",
    boardStartupFailed: "Board startup failed: {message}",
  },
  zh: {
    pageTitle: "Robert 任务看板",
    productSubtitle: "Your Repo Teammate",
    boardTitle: "仓库任务看板",
    primaryNavigation: "一级导航",
    navWorkbench: "工作台",
    requiredHelp: "仓库、标题和需求描述为必填项。", requiredField: "请填写{field}", requiredMark: "（必填）", boardScope: "看板按任务生命周期统计，包含本地任务和 GitHub 工作；工作队列按 GitHub 工作流统计，两处需要处理的数量可能不同。",
    navWork: "工作",
    navBoard: "任务看板",
    navHistory: "历史",
    navOperations: "运行状态",
    navKnowledge: "知识",
    repositoryContext: "仓库上下文",
    taskBoardContext: "任务看板",
    boardControls: "看板控制",
    settings: "设置",
    theme: "主题",
    themeSystem: "跟随系统",
    themeLight: "浅色",
    themeDark: "深色",
    language: "语言",
    languageEnglish: "English",
    languageChinese: "中文",
    newTask: "新建任务",
    boardFilters: "看板筛选",
    find: "搜索",
    searchPlaceholder: "标题、任务、分支或 PR",
    repository: "仓库",
    allRepositories: "全部仓库",
    agent: "Agent",
    allAgents: "全部 Agent",
    priority: "优先级",
    allPriorities: "全部优先级",
    needsAttention: "需要处理",
    refresh: "刷新",
    refreshBoard: "刷新看板",
    taskStatus: "任务状态",
    repositoryTasks: "仓库任务",
    columnBacklog: "待整理",
    columnTodo: "待办",
    columnDoing: "进行中",
    columnWaiting: "等待你确认",
    columnReview: "待评审",
    columnDone: "已完成",
    purposeBacklog: "仍可编辑的需求想法",
    purposeTodo: "等待 Agent 执行槽位",
    purposeDoing: "Agent 正在工作或发布",
    purposeWaiting: "等待你的决定或回复后继续",
    purposeReview: "PR 或完成结果等待评审",
    purposeDone: "已验收并完成",
    taskDetail: "任务详情",
    close: "关闭",
    closeTaskDetail: "关闭任务详情",
    localRequest: "本地需求",
    newRepositoryTask: "新建仓库任务",
    closeNewTask: "关闭新建任务",
    title: "标题",
    requirement: "需求描述",
    routing: "路由",
    auto: "自动",
    specificAgent: "指定 Agent",
    afterCreation: "创建后",
    keepInBacklog: "保留在 Backlog",
    startNow: "立即开始",
    cancel: "取消",
    cancelTask: "取消任务",
    openGithub: "在 GitHub 打开 ↗",
    createTask: "创建任务",
    requestFailed: "请求失败（{status}）",
    openTask: "打开 {title}",
    attentionCount: "{count} 项需要处理",
    moveToTodo: "移到待办",
    noTasksStage: "当前阶段没有任务。",
    agentsRunning: "{count} 个 Agent 运行中",
    agentsRunningEmpty: "0 个 Agent 运行中",
    status: "状态",
    version: "版本",
    task: "任务",
    branch: "分支",
    pullRequest: "Pull Request",
    availableActions: "可执行操作",
    noDescription: "暂无需求描述。",
    noAction: "当前阶段无需操作。",
    attention: "需要处理",
    noUnresolved: "没有待处理的人工请求。",
    timeline: "时间线",
    noEvents: "暂无事件记录。",
    open: "打开",
    editRequirement: "编辑需求",
    approve: "批准",
    reply: "回复",
    requestChanges: "要求修改",
    retry: "重试",
    reopen: "重新打开",
    yourReply: "你的回复",
    requestedChanges: "修改要求",
    taskTitlePrompt: "任务标题",
    requirementPrompt: "需求描述",
    commandCompleted: "已完成：{action}。",
    taskChanged: "任务已在其他位置发生变化。详情已刷新，请重试操作。",
    boardRefreshed: "看板已刷新。",
    boardRefreshFailed: "看板刷新失败：{message}",
    writesDisabled: "当前禁止写入。",
    taskCreated: "任务已创建。",
    boardStartupFailed: "看板启动失败：{message}",
  },
};

Object.assign(translations.en, {
  lifecycle: "Lifecycle",
  taskReceived: "Task received",
  attemptSummary: "{count} attempts · {failed} failed",
  lastExecution: "Latest execution report",
  publication_open_pr: "Create PR",
  publication_push_existing_pr: "Push code",
  publication_comment: "Publish comment",
  publication_review: "Publish review",
  published: "Published",
  not_published: "Awaiting publication",
  publish_failed: "Publication failed",
  skipped: "Skipped",
  auditAccepted: "Audit accepted",
  classification_result: "Classify requirement",
  new_pr: "Implement and prepare PR",
  pr_update: "Update PR",
  pr_opened: "PR created",
  pr_merged: "PR merge recorded",
  routing_selected: "Execution route selected",
  backfilled: "Imported historical task",
  zombie: "Worker exited but its process has not been reaped",
  boardTitle: "Task board",
  boardHint: "Track execution, resolve blockers, and finish work.",
  view: "View",
  canceledTasks: "Canceled",
  historyHint: "Stopped tasks and their history remain available.",
  loadMore: "Load more tasks",
  loadEvents: "Earlier activity",
  loading: "Loading…",
  syncing: "Updating…",
  synced: "Updated {time}",
  showing: "Showing {count} of {total} tasks",
  readOnly: "Read-only mode: {reason}",
  retryRefresh: "Refresh and retry",
  execution: "Execution",
  executionHistory: "Execution history",
  moreAttempts: "Load earlier attempts ({count} remaining)",
  artifacts: "Logs and artifacts",
  missingArtifact: "File absent on this host; the database contains only its reference.",
  failed: "Failed",
  failed_timeout: "Worker timed out",
  failed_worker_process_exited: "Worker process exited unexpectedly",
  failed_worker_startup: "Worker failed to start",
  needs_resume: "Attempt interrupted; recovery was required",
  unloadedStage: "Tasks in this stage are on another page. Load more to view them.",
  decisionHelp: "Review the requirement and execution history, then approve to continue or cancel the task.",
  nextStep: "Next step",
  technical: "Task metadata",
  attempt: "Attempt {number}",
  heartbeat: "Last heartbeat",
  started: "Started",
  finished: "Finished",
  noHeartbeat: "No heartbeat received yet",
  noProgress: "Waiting for the worker to report its first step.",
  failure: "What went wrong",
  pending: "Waiting for dispatch",
  retry_publish: "Retry publication",
  retrigger: "Retry execution",
  cancelTask: "Cancel task",
  stopTask: "Stop and cancel",
  cancelHelp:
    "Stop this task and skip unpublished results? Existing GitHub changes are kept. A worker that cannot be stopped remains active.",
  confirmCancel: "Confirm cancellation",
  submitting: "Submitting…",
  save: "Save changes",
  reviewDraft: "I reviewed the latest state; keep editing",
  draftConflict:
    "This task changed. Your input is preserved. Review the latest content before continuing:",
  actionUnavailable:
    "This action is no longer available. Your input is preserved; close the editor to choose a current action.",
  draftKept:
    "Your draft is kept. Review the latest state before submitting again.",
  publicationPending:
    "The existing result is awaiting publication. No new worker will be started.",
  publicationRetry: "Publication retry queued. Waiting for the daemon.",
  closed_source_running: "Source closed; stop the worker to finish this task.",
  source_closed: "Source closed. Cancel to finish this task.",
  worker_cleanup_pending:
    "Worker cleanup is incomplete. Retry stopping it; see the reason below.",
  publishing: "Publishing result",
  running: "Worker running",
  execution_failed: "Execution failed",
  publication_failed: "Publication failed",
  operator_question: "Reply needed",
  completion_acceptance_requested: "Ready for acceptance",
  operator_decision_required: "Decision needed",
  manual_worker_unavailable: "Selected worker unavailable",
  unmerged_pr_closed: "PR closed without merge",
  review_attention: "Review needs attention",
  draft: "Ready to define",
  activated: "Queued",
  detected: "Queued",
  authorized: "Queued",
  classified: "Queued",
  queued: "Queued",
  open_pr: "Review on GitHub",
  completed: "Completed",
  canceled: "Canceled",
  retry: "Retry execution",
  approve: "Approve and continue",
  moveToTodo: "Start task",
  columnWaiting: "Needs attention",
  columnDoing: "Executing",
  columnTodo: "Queued",
  purposeTodo: "Waiting for dispatch",
  purposeDoing: "Live execution and publication",
  purposeWaiting: "Failures, questions, and decisions",
  prepare: "Prepare",
  analyze: "Analyze",
  plan: "Plan",
  execute: "Execute",
  verify: "Verify",
  publish: "Publish",
  handoff: "Handoff",
});
Object.assign(translations.zh, {
  lifecycle: "生命周期",
  taskReceived: "任务进入系统",
  attemptSummary: "{count} 次执行 · {failed} 次失败",
  lastExecution: "最后一次执行报告",
  publication_open_pr: "创建 PR",
  publication_push_existing_pr: "推送代码",
  publication_comment: "发布评论",
  publication_review: "发布评审",
  published: "已发布",
  not_published: "等待发布",
  publish_failed: "发布失败",
  skipped: "已跳过",
  auditAccepted: "审核通过",
  classification_result: "需求分析与分流",
  new_pr: "实现需求并准备 PR",
  pr_update: "更新 PR",
  pr_opened: "PR 已创建",
  pr_merged: "PR 合并已同步",
  routing_selected: "已选择执行路径",
  backfilled: "导入历史任务",
  zombie: "Worker 已退出但进程尚未回收（僵尸进程）",
  filtersToggle: "筛选",
  boardTitle: "任务看板",
  boardHint: "跟踪执行，处理阻塞，让每个任务走到完成。",
  view: "视图",
  canceledTasks: "已取消",
  historyHint: "保留已停止任务及其处理记录。",
  loadMore: "加载更多任务",
  loadEvents: "更早的记录",
  loading: "加载中…",
  syncing: "更新中…",
  synced: "更新于 {time}",
  showing: "已显示 {count} / {total} 个任务",
  readOnly: "只读模式：{reason}",
  retryRefresh: "刷新后重试",
  execution: "执行进度",
  executionHistory: "执行历史",
  moreAttempts: "加载更早的执行记录（还剩 {count} 次）",
  artifacts: "日志和产物",
  missingArtifact: "当前机器没有此文件，数据库仅保存了文件引用。",
  failed: "失败",
  failed_timeout: "Worker 执行超时",
  failed_worker_process_exited: "Worker 进程意外退出",
  failed_worker_startup: "Worker 启动失败",
  needs_resume: "本次执行中断，进入恢复流程",
  unloadedStage: "此阶段的任务还未加载，点击加载更多后查看。",
  decisionHelp: "请先查看需求与执行历史，再批准继续或取消任务。",
  nextStep: "下一步",
  technical: "任务技术信息",
  attempt: "第 {number} 次执行",
  heartbeat: "最近心跳",
  started: "开始时间",
  finished: "结束时间",
  noHeartbeat: "尚未收到心跳",
  noProgress: "等待 Worker 上报第一个执行阶段。",
  failure: "出错原因",
  pending: "等待调度",
  retry_publish: "重试发布",
  retrigger: "重试执行",
  cancelTask: "取消任务",
  stopTask: "停止并取消",
  cancelHelp:
    "停止此任务并跳过尚未发布的结果？已产生的 GitHub 变更会保留。无法停止的 Worker 会继续显示为运行中。",
  confirmCancel: "确认取消任务",
  submitting: "提交中…",
  save: "保存修改",
  reviewDraft: "已核对最新内容，继续编辑",
  draftConflict: "任务已更新，输入已保留。请先核对最新内容：",
  actionUnavailable:
    "当前状态已不支持此操作，输入已保留；关闭编辑区后选择新的操作。",
  draftKept: "输入已保留，请核对最新状态后再次提交。",
  publicationPending: "已有结果等待发布，无需重新运行 Worker。",
  publicationRetry: "已请求重试发布，等待后台执行。",
  closed_source_running: "源 Issue / PR 已关闭，需要停止仍在运行的 Worker。",
  source_closed: "源 Issue / PR 已关闭，取消此任务以结束流程。",
  worker_cleanup_pending: "Worker 尚未停止，可再次尝试取消；具体原因如下。",
  publishing: "正在发布结果",
  running: "Worker 运行中",
  execution_failed: "执行失败",
  publication_failed: "发布失败",
  operator_question: "需要回复",
  completion_acceptance_requested: "等待验收",
  operator_decision_required: "需要决策",
  manual_worker_unavailable: "指定 Worker 不可用",
  unmerged_pr_closed: "PR 已关闭但未合并",
  review_attention: "评审需要处理",
  draft: "可编辑需求",
  activated: "已排队",
  detected: "已排队",
  authorized: "已排队",
  classified: "已排队",
  queued: "已排队",
  open_pr: "等待 GitHub 评审",
  completed: "已完成",
  canceled: "已取消",
  retry: "重试执行",
  approve: "批准并继续",
  moveToTodo: "开始任务",
  columnWaiting: "需要处理",
  columnDoing: "执行中",
  columnTodo: "已排队",
  purposeTodo: "等待调度执行",
  purposeDoing: "执行过程与结果发布",
  purposeWaiting: "失败、提问或待确认事项",
  prepare: "准备",
  analyze: "分析",
  plan: "计划",
  execute: "执行",
  verify: "验证",
  publish: "发布",
  handoff: "交付",
});

const COLUMNS = ["backlog", "todo", "doing", "waiting", "review", "done"];
const COLUMN_KEYS = {
  backlog: "columnBacklog",
  todo: "columnTodo",
  doing: "columnDoing",
  waiting: "columnWaiting",
  review: "columnReview",
  done: "columnDone",
  history: "canceledTasks",
};
const COMMAND_KEYS = {
  edit: "editRequirement",
  start: "moveToTodo",
  approve: "approve",
  reply: "reply",
  request_changes: "requestChanges",
  retry: "retry",
  cancel: "cancelTask",
  reopen: "reopen",
};
const PHASES = [
  "prepare",
  "analyze",
  "plan",
  "execute",
  "verify",
  "publish",
  "handoff",
];
const params = new URLSearchParams(location.search);
const state = {
  session: null,
  board: null,
  detail: null,
  historyLimits: {},
  selectedItem: params.get("item"),
  mobileColumn: COLUMNS.includes(params.get("column")) ? params.get("column") : null,
  view: params.get("view") || "active",
  language: localStorage.getItem("robertLanguage") || "en",
  theme: localStorage.getItem("robertWorkbenchTheme") || "system",
  boardRequest: 0,
  detailRequest: 0,
  busy: false,
  composer: null,
};
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) =>
  Array.from(root.querySelectorAll(selector));
function t(key, variables = {}) {
  let value =
    (translations[state.language] || translations.en)[key] ||
    translations.en[key] ||
    key;
  Object.entries(variables).forEach(([k, v]) => {
    value = value.replaceAll(`{${k}}`, String(v));
  });
  return value;
}
function node(tag, className, text) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text !== undefined && text !== null) el.textContent = String(text);
  return el;
}
function button(label, callback, className = "quiet-button") {
  const el = node("button", className, label);
  el.type = "button";
  el.addEventListener("click", callback);
  return el;
}
function parseTime(value) {
  return new Date(/^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$/.test(value) ? value.replace(" ", "T") + "Z" : value);
}
function formatTime(value) {
  if (!value) return "—";
  const date = parseTime(value);
  return Number.isNaN(date.getTime())
    ? value
    : date.toLocaleString(state.language === "zh" ? "zh-CN" : "en", {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      });
}
function columnLabel(value) {
  return t(COLUMN_KEYS[value] || value);
}
function commandLabel(value) {
  return t(COMMAND_KEYS[value] || value);
}
function safeExternalUrl(value) {
  try {
    const url = new URL(value);
    return ["http:", "https:"].includes(url.protocol) ? url.href : null;
  } catch (_) {
    return null;
  }
}
function externalLink(value, label, className = "") {
  const url = safeExternalUrl(value);
  if (!url) return null;
  const a = node("a", className, label);
  a.href = url;
  a.target = "_blank";
  a.rel = "noopener noreferrer";
  return a;
}
function updateUrl(changes) {
  const query = new URLSearchParams(location.search);
  Object.entries(changes).forEach(([k, v]) => {
    if (v) query.set(k, v);
    else query.delete(k);
  });
  history.replaceState(
    null,
    "",
    `${location.pathname}${query.size ? `?${query}` : ""}`,
  );
}
function feedback(selector, text) {
  const el = $(selector);
  el.textContent = text || "";
  el.hidden = !text;
}
function showStatus(message, isError = false) {
  const el = $("#status-message");
  ($("dialog[open]") || document.body).append(el);
  el.textContent = message;
  el.classList.toggle("error", isError);
  el.classList.add("visible");
  clearTimeout(showStatus.timer);
  if (!isError)
    showStatus.timer = setTimeout(() => el.classList.remove("visible"), 4500);
  if (isError)
    feedback(
      $("#new-task-dialog").open
        ? "#form-feedback"
        : $("#work-item-drawer").open
          ? "#drawer-feedback"
          : "#board-feedback",
      message,
    );
}
function canWrite(item) {
  return Boolean(
    state.session?.writes_enabled &&
      state.session.allowed_repo_ids.includes(item.repo_id),
  );
}
function applyTranslations() {
  document.documentElement.lang = state.language === "zh" ? "zh-CN" : "en";
  document.title = t("pageTitle");
  $$("[data-i18n]").forEach((el) => {
    el.textContent = t(el.dataset.i18n);
  });
  $$("[data-i18n-placeholder]").forEach((el) => {
    el.placeholder = t(el.dataset.i18nPlaceholder);
  });
  $$("[data-i18n-aria]").forEach((el) => {
    el.setAttribute("aria-label", t(el.dataset.i18nAria));
  });
  $("#language-select").value = state.language;
}
function setTheme(value) {
  state.theme = ["system", "light", "dark"].includes(value) ? value : "system";
  localStorage.setItem("robertWorkbenchTheme", state.theme);
  document.documentElement.dataset.theme =
    document.documentElement.dataset.colorMode =
      state.theme === "system"
        ? matchMedia("(prefers-color-scheme: dark)").matches
          ? "dark"
          : "light"
        : state.theme;
  $("#theme-select").value = state.theme;
}
function setOptions(select, values, label, first) {
  const previous = select.value;
  const options = first
    ? [Object.assign(node("option", "", first), { value: "" })]
    : [];
  values.forEach((value) =>
    options.push(
      Object.assign(node("option", "", label(value)), {
        value: typeof value === "string" ? value : value.repo_id,
      }),
    ),
  );
  select.replaceChildren(...options);
  if (options.some((el) => el.value === previous)) select.value = previous;
}
function renderFilters(data) {
  setOptions(
    $("#repo-filter"),
    data.filters.repos,
    (r) => r.full_name,
    t("allRepositories"),
  );
  setOptions($("#agent-filter"), data.filters.agents, (a) => a, t("allAgents"));
  if (!$("#new-task-dialog").open)
    setOptions(
      $("#new-task-repo"),
      data.filters.repos.filter((r) =>
        state.session?.allowed_repo_ids.includes(r.repo_id),
      ),
      (r) => r.full_name,
      null,
    );
}
function boardUrl(cursor) {
  const query = new URLSearchParams({
    limit: "50",
    include_history: String(state.view === "history"),
  });
  [
    ["repo", "#repo-filter"],
    ["agent", "#agent-filter"],
    ["priority", "#priority-filter"],
    ["q", "#search-filter"],
  ].forEach(([key, selector]) => {
    if ($(selector).value.trim()) query.set(key, $(selector).value.trim());
  });
  if ($("#attention-filter").checked) query.set("attention", "true");
  if (state.view === "history") query.set("column", "history");
  if (cursor) query.set("cursor", cursor);
  return `/api/board?${query}`;
}
function describe(item) {
  const execution = item.execution;
  if (item.column === "doing" && execution?.summary) return execution.summary;
  return t(item.reason_code) !== item.reason_code
    ? t(item.reason_code)
    : item.reason_summary || columnLabel(item.column);
}
function createCard(item) {
  const card = node("article", "task-card");
  Object.assign(card.dataset, {
    column: item.column,
    priority: item.priority,
    workItemId: item.work_item_id,
  });
  card.draggable =
    !state.busy && item.valid_commands.includes("start") && canWrite(item);
  card.addEventListener("dragstart", (event) => {
    event.dataTransfer.setData("text/plain", item.work_item_id);
    event.dataTransfer.setData("application/x-robert-column", item.column);
    event.dataTransfer.effectAllowed = "move";
  });
  const top = node("div", "task-topline");
  top.append(
    node("span", "repo-label", item.repo_full_name),
    node("span", "priority", item.priority),
  );
  const title = node("h3", "task-title");
  const open = button(
    item.title,
    () => openDetail(item.work_item_id),
    "card-open",
  );
  open.setAttribute("aria-label", t("openTask", { title: item.title }));
  title.append(open);
  card.append(top, title);
  if (!["done", "history"].includes(item.column))
    card.append(node("p", "task-reason", describe(item)));
  if (item.column === "doing" && item.execution?.phase)
    card.append(
      node(
        "div",
        "phase-label",
        `${t(item.execution.phase)} · ${t("attempt", { number: item.execution.attempt_no })}`,
      ),
    );
  const meta = node("div", "task-meta");
  if (item.agent && item.agent !== "Auto") meta.append(node("span", "", item.agent));
  if (item.source?.url !== item.pr?.url) {
    const a = externalLink(
      item.source?.url,
      `${item.source?.type === "pull_request" ? "PR" : "Issue"} #${item.source?.number}`,
    );
    if (a) meta.append(a);
  }
  const pr = externalLink(item.pr?.url, `PR #${item.pr?.number}`);
  if (pr) meta.append(pr);
  card.append(meta);
  const footer = node("div", "task-footer");
  footer.append(
    node(
      "time",
      "",
      formatTime(item.execution?.heartbeat_at || item.updated_at),
    ),
  );
  const primary = item.valid_commands.find((command) =>
    [
      "start",
      "retry",
      "retry_publish",
      "retrigger",
      "reply",
      "approve",
    ].includes(command),
  );
  const action =
    primary ||
    (item.pr?.state === "closed" && item.valid_commands.includes("cancel")
      ? "cancel"
      : null);
  if (action) {
    const control = button(
      commandLabel(action),
      () =>
        ["cancel", "reply", "retry", "retrigger"].includes(action)
          ? openDetail(item.work_item_id, action)
          : executeCommand(item, action),
      "start-card-button",
    );
    control.dataset.command = action;
    control.disabled = state.busy || !canWrite(item);
    footer.append(control);
  }
  card.append(footer);
  return card;
}
function renderMobileColumn({ scroll = false } = {}) {
  $$("[data-mobile-column]").forEach((el) => {
    const selected = el.dataset.mobileColumn === state.mobileColumn;
    el.classList.toggle("active", selected);
    el.setAttribute("aria-pressed", String(selected));
  });
  $$(".board-column").forEach((el) => {
    el.hidden =
      state.view === "history"
        ? el.dataset.column !== "history"
        : el.dataset.column === "history";
    el.classList.toggle(
      "mobile-active",
      state.view === "history" || el.dataset.column === state.mobileColumn,
    );
  });
  $("#mobile-column-tabs").hidden = state.view === "history";
  $("#board-app").classList.toggle("history-view", state.view === "history");
  if (scroll && state.mobileColumn && state.view !== "history")
    $(`.board-column[data-column="${state.mobileColumn}"]`).scrollIntoView({block: "nearest", inline: "start"});
}
function renderBoard(data) {
  if (!state.mobileColumn)
    state.mobileColumn = ["waiting", "doing", "review", "todo", "backlog", "done"].find((column) => data.counts[column] > 0) || "doing";
  [...COLUMNS, "history"].forEach((column) => {
    $$(`[data-count="${column}"]`).forEach((el) => {
      el.textContent = data.counts[column] || 0;
    });
    const target = $(`[data-cards="${column}"]`);
    const cards = data.items
      .filter((item) => item.column === column)
      .map(createCard);
    target.replaceChildren(
      ...(cards.length
        ? cards
        : [node("p", "empty-column", t(data.counts[column] ? "unloadedStage" : "noTasksStage"))]),
    );
  });
  $("#capacity").textContent = t("agentsRunning", {
    count: data.capacity.running,
  });
  const visible = data.items.filter((item) =>
    state.view === "history"
      ? item.column === "history"
      : item.column !== "history",
  ).length;
  const total =
    state.view === "history"
      ? data.counts.history || 0
      : COLUMNS.reduce((sum, col) => sum + (data.counts[col] || 0), 0);
  $("#loaded-count").textContent = t("showing", { count: visible, total });
  $("#load-more").hidden = !data.next_cursor;
  renderMobileColumn({ scroll: !state.columnPositioned });
  state.columnPositioned = true;
}
function detailField(label, value) {
  const field = node("div", "detail-field");
  field.append(node("span", "", label), node("strong", "", value ?? "—"));
  return field;
}
function renderLive(detail) {
  const section = node("section", "detail-block live-detail");
  section.id = "detail-live";
  section.append(
    node(
      "span",
      `state-label state-${detail.column}`,
      columnLabel(detail.column),
    ),
  );
  if (describe(detail) !== columnLabel(detail.column))
    section.append(node("p", "state-summary", describe(detail)));
  const links = node("div", "source-links");
  const source = externalLink(detail.source?.url, `${detail.source?.type === "pull_request" ? "PR" : "Issue"} #${detail.source?.number}`);
  const pr = externalLink(detail.pr?.url, `PR #${detail.pr?.number}`);
  if (source) links.append(source);
  if (pr && detail.pr.url !== detail.source?.url) links.append(pr);
  section.append(links);
  const failures = [...(detail.publication?.failures || [])];
  if (detail.execution?.cleanup_reason)
    failures.push({ reason: detail.execution.cleanup_reason });
  if (detail.execution?.failure && detail.column !== "done")
    failures.push({ reason: detail.execution.failure });
  if (detail.attention_signals?.length || failures.length) {
    const alert = node("div", "attention-panel");
    alert.append(
      node(
        "h3",
        "",
        t(
          failures.length ||
            [
              "execution_failed",
              "publication_failed",
              "manual_worker_unavailable",
              "worker_cleanup_pending",
            ].includes(detail.reason_code)
            ? "failure"
            : "attention",
        ),
      ),
    );
    [
      ...new Set(
        [
          ...failures.map((f) => f.reason),
          ...detail.attention_signals.map((signal) => signal.summary || (signal.type === "operator_decision_required" ? t("decisionHelp") : t(signal.type))),
        ].filter(Boolean),
      ),
    ].forEach((text) => alert.append(node("p", "", text)));
    section.append(alert);
  }
  if (detail.publication?.blocked_reason)
    section.append(node("p", "notice", detail.publication.blocked_reason));
  if (detail.publication?.retry_pending)
    section.append(node("p", "notice", t("publicationRetry")));
  else if (detail.action_blocked_reason)
    section.append(node("p", "notice", t("publicationPending")));
  const execution = detail.execution;
  if (execution) {
    const terminal = ["done", "history"].includes(detail.column);
    const progress = node(terminal ? "details" : "div", "execution-panel");
    progress.append(node(terminal ? "summary" : "h3", "", t(terminal ? "lastExecution" : "execution")));
    const phases = node("ol", "phase-track");
    PHASES.forEach((phase) => {
      const step = node(
        "li",
        phase === execution.phase ? "current" : "",
        t(phase),
      );
      if (phase === execution.phase) step.setAttribute("aria-current", "step");
      phases.append(step);
    });
    progress.append(phases);
    progress.append(node("p", "", execution.summary || t("noProgress")));
    if (execution.next_step)
      progress.append(
        node("p", "next-step", `${t("nextStep")}：${execution.next_step}`),
      );
    const times = node("div", "execution-times");
    times.append(
      detailField(t("started"), formatTime(execution.started_at)),
      detailField(
        t("heartbeat"),
        execution.heartbeat_at
          ? formatTime(execution.heartbeat_at)
          : t("noHeartbeat"),
      ),
    );
    if (execution.finished_at)
      times.append(
        detailField(t("finished"), formatTime(execution.finished_at)),
      );
    progress.append(times);
    section.append(progress);
  } else if (detail.column === "todo")
    section.append(node("p", "notice", t("pending")));
  return section;
}
function renderActions(detail) {
  const section = node("section", "detail-block");
  section.id = "detail-actions";
  section.append(node("h3", "", t("availableActions")));
  const row = node("div", "command-row");
  detail.valid_commands.forEach((command, index) => {
    const b = button(
      command === "cancel" && detail.execution?.status === "running"
        ? t("stopTask")
        : commandLabel(command),
      () => composeOrExecute(command),
      index === 0 && !["cancel", "request_changes"].includes(command)
        ? "primary-button command-button"
        : "command-button",
    );
    b.dataset.command = command;
    b.disabled = state.busy || !canWrite(detail);
    row.append(b);
  });
  const github = externalLink(
    detail.pr?.url || detail.source?.url,
    t("openGithub"),
    "quiet-button",
  );
  if (github) row.append(github);
  if (!detail.valid_commands.length)
    row.append(node("p", "task-reason", t("noAction")));
  section.append(row);
  if (!canWrite(detail) && state.session?.write_error)
    section.append(node("p", "notice", state.session.write_error));
  return section;
}

function attemptCounts(attempts) {
  return t("attemptSummary", {
    count: attempts.length,
    failed: attempts.filter(run => run.status === "failed" || run.status?.startsWith("failed_") || run.status === "needs_resume").length,
  });
}

function renderJourney(detail) {
  const section = node("section", "detail-block lifecycle-section");
  section.id = "detail-journey";
  section.append(node("h3", "", t("lifecycle")));
  const tasks = detail.tasks || [];
  const attempts = tasks.flatMap(task => task.attempts || []);
  if (attempts.length) section.append(node("p", "lifecycle-counts", attemptCounts(attempts)));
  const milestones = [];
  if (detail.created_at) milestones.push({title: t("taskReceived"), at: detail.created_at});
  tasks.forEach(task => {
    const runs = task.attempts || [];
    const latest = runs.at(-1);
    milestones.push({
      title: `${t(task.expected_output || task.route_id || "task")} · ${t(task.lifecycle)}`,
      at: runs[0]?.started_at || task.created_at,
      body: runs.length ? `${attemptCounts(runs)}${latest?.finished_at ? ` · ${t("finished")} ${formatTime(latest.finished_at)}` : ""}` : t("pending"),
    });
  });
  const actions = tasks.flatMap(task => task.publication_actions || []);
  actions.forEach(action => {
    const key = `publication_${action.action_type}`;
    milestones.push({
      title: `${t(key) === key ? action.action_type : t(key)} · ${t(action.status)}`,
      at: action.published_at || action.created_at,
      body: [action.safe_error || (action.audit_status === "accepted" ? t("auditAccepted") : action.audit_status), action.attempt_count ? `${action.attempt_count}/${action.max_attempts || "?"}` : "", action.next_retry_at ? formatTime(action.next_retry_at) : "", action.required_action].filter(Boolean).join(" · "),
    });
  });
  // Event timestamps record when Robert observed the change, not necessarily
  // when it happened on GitHub. A closed PR alone is not evidence of a merge.
  (detail.events || []).filter(event => ["pr_opened", "pr_merged", "canceled", "completed"].includes(event.event_type))
    .filter(event => event.event_type !== "pr_opened" || !actions.some(action => action.action_type === "open_pr" && action.status === "published"))
    .forEach(event => {
    milestones.push({title: t(event.event_type), at: event.created_at});
  });
  const timeline = node("ol", "lifecycle-timeline");
  milestones.sort((a, b) => (parseTime(a.at).getTime() || 0) - (parseTime(b.at).getTime() || 0)).forEach(item => {
    const row = node("li", "lifecycle-event");
    row.append(node("strong", "", item.title), node("time", "", formatTime(item.at)));
    if (item.body) row.append(node("p", "", item.body));
    timeline.append(row);
  });
  section.append(timeline);
  return section;
}
function eventList(events) {
  const timeline = node("div", "timeline");
  events.forEach((item) => {
    const event = node("article", "timeline-event");
    event.append(node("strong", "", t(item.event_type).replaceAll("_", " ")));
    if (item.body) event.append(node("p", "", item.body));
    event.append(
      node(
        "time",
        "",
        `${item.actor_identity || item.actor_kind} · ${formatTime(item.created_at)}`,
      ),
    );
    timeline.append(event);
  });
  return timeline;
}
function attentionForCommand(detail, command) {
  const questions = ["operator_question", "completion_acceptance_requested"];
  const failures = [
    "execution_failed",
    "publication_failed",
    "manual_worker_unavailable",
    "review_attention",
    "unmerged_pr_closed",
  ];
  const kinds =
    command === "reply"
      ? questions
      : command === "retry"
        ? failures
        : command === "approve"
          ? ["operator_decision_required", "completion_acceptance_requested"]
          : command === "request_changes"
            ? [...questions, ...failures]
            : [];
  return (detail.attention_signals || []).find((signal) =>
    kinds.includes(signal.type),
  )?.event_id;
}
function updateComposerState(detail) {
  const draft = state.composer;
  if (!draft?.send) return;
  const changed =
    draft.item.version !== detail.version ||
    attentionForCommand(draft.item, draft.command) !==
      attentionForCommand(detail, draft.command);
  const available = detail.valid_commands.includes(draft.command);
  draft.send.disabled =
    state.busy || !canWrite(detail) || changed || !available;
  draft.confirm.hidden = !changed || !available;
  draft.confirm.disabled = state.busy;
  draft.notice.hidden = !changed && available;
  const context =
    draft.command === "edit"
      ? `${detail.title} — ${detail.description || ""}`
      : (detail.attention_signals || [])
          .map((signal) => signal.summary)
          .join("; ");
  draft.notice.textContent = !available
    ? t("actionUnavailable")
    : `${t("draftConflict")} ${context.slice(0, 1000)}`;
}
function renderExecutionHistory(detail) {
  const history = node("details", "detail-block disclosure");
  history.id = "execution-history";
  history.append(node("summary", "", t("executionHistory")));
  [...(detail.tasks || [])].reverse().forEach((task) => {
    const entry = node("section", "execution-history-task");
    entry.append(node("h3", "", `${t(task.expected_output || task.route_id || "task")} · ${t(task.lifecycle)}`));
    entry.append(node("p", "attempt-counts", `${attemptCounts(task.attempts)} · ${task.task_id}`));
    if (task.review) {
      const review = task.review;
      entry.append(node("p", "", `${review.round_id} · ${review.head_sha || "pending"} · ${t(review.status)}`));
      if (review.process_pool) entry.append(node("p", "", `Review processes: ${review.process_pool.in_use} / ${review.process_pool.limit}`));
      else entry.append(node("p", "", "Review processes: unavailable"));
      if (review.error) entry.append(node("p", "", review.error));
      (review.runs || []).forEach((run) => {
        entry.append(node("p", "", `${run.stage} · ${run.worker_id}${run.model ? " (" + run.model + ")" : ""} · #${run.attempt_no} · ${t(run.status)}${run.error ? " · " + run.error : ""}`));
      });
    }
    if (!task.attempts.length) entry.append(node("p", "", t("noProgress")));
    const attempts = [...task.attempts].reverse();
    let shown = Math.min(state.historyLimits[task.task_id] || 10, attempts.length);
    const list = node("div", "attempt-list");
    const appendRuns = (runs) => runs.forEach((attempt) => {
      const run = node("details", "execution-history-attempt");
      run.dataset.attemptId = attempt.attempt_id;
      run.append(node("summary", "", `${t("attempt", { number: attempt.attempt_no })} · ${t(attempt.status)} · ${formatTime(attempt.started_at)}`));
      const times = node("div", "execution-times");
      times.append(detailField(t("heartbeat"), formatTime(attempt.heartbeat_at)), detailField(t("finished"), formatTime(attempt.finished_at)));
      run.append(times);
      if (attempt.artifacts?.length) {
        const logs = node("div", "attempt-artifacts");
        logs.append(node("h4", "", t("artifacts")));
        const missing = attempt.artifacts.filter(artifact => !artifact.available);
        if (missing.length) logs.append(node("p", "notice", `${t("missingArtifact")} (${missing.map(artifact => artifact.artifact_type).join(", ")})`));
        attempt.artifacts.filter(artifact => artifact.available).forEach((artifact) => {
          if (artifact.available) {
            const link = node("a", "quiet-button", artifact.artifact_type);
            link.href = `/artifact.txt?${new URLSearchParams({task_id: task.task_id, artifact_type: artifact.artifact_type, artifact_id: artifact.artifact_id})}`;
            link.target = "_blank";
            link.rel = "noopener noreferrer";
            logs.append(link);
          }
        });
        run.append(logs);
      }
      if (attempt.failure || attempt.cleanup_reason)
        run.append(node("p", "notice error", [attempt.failure, attempt.cleanup_reason].filter(Boolean).map((reason) => t(reason) === reason ? reason : `${t(reason)} (${reason})`).join(" · ")));
      (attempt.phases || []).forEach((phase) => {
        const row = node("article", "timeline-event");
        row.append(node("strong", "", `${t(phase.phase)} · ${t(phase.status)}`), node("time", "", formatTime(phase.created_at)), node("p", "", phase.summary));
        if (phase.next_step) row.append(node("p", "", `${t("nextStep")}：${phase.next_step}`));
        run.append(row);
      });
      list.append(run);
    });
    appendRuns(attempts.slice(0, shown));
    const more = button(t("moreAttempts", { count: attempts.length - shown }), () => {
      appendRuns(attempts.slice(shown, shown + 10));
      shown = Math.min(shown + 10, attempts.length);
      state.historyLimits[task.task_id] = shown;
      more.hidden = shown >= attempts.length;
      more.textContent = t("moreAttempts", { count: attempts.length - shown });
    });
    more.hidden = shown >= attempts.length;
    entry.append(list, more);
    history.append(entry);
  });
  return history;
}
function renderDetail(detail, { preserve = false } = {}) {
  state.detail = detail;
  $("#drawer-title").textContent = detail.title;
  $("#drawer-repo").textContent = detail.repo_full_name;
  if (preserve && $("#detail-live")) {
    const reportOpen = $("#detail-live details")?.open;
    const nextLive = renderLive(detail);
    nextLive.querySelectorAll("details").forEach(el => { el.open = reportOpen; });
    $("#detail-live").replaceWith(nextLive);
    $("#detail-journey")?.replaceWith(renderJourney(detail));
    if (!state.busy) $("#detail-actions").replaceWith(renderActions(detail));
    const previousHistory = $("#execution-history");
    const nextHistory = renderExecutionHistory(detail);
    nextHistory.open = previousHistory.open;
    const openAttempts = new Set([...previousHistory.querySelectorAll("details")].filter(el => el.open).map(el => el.dataset.attemptId));
    nextHistory.querySelectorAll("details").forEach(el => { el.open = openAttempts.has(el.dataset.attemptId); });
    previousHistory.replaceWith(nextHistory);
    updateComposerState(detail);
    return;
  }
  state.composer = null;
  const content = $("#drawer-content");
  const composer = node("div");
  composer.id = "composer-host";
  const description = node("details", "detail-block disclosure");
  description.append(
    node("summary", "", t("requirement")),
    node("p", "detail-description", detail.description || t("noDescription")),
  );
  const technical = node("details", "detail-block disclosure");
  technical.append(node("summary", "", t("technical")));
  const grid = node("div", "detail-grid");
  [
    [t("task"), detail.task_id],
    [t("branch"), detail.branch],
    [t("routing"), detail.agent],
    [t("priority"), detail.priority],
    [t("version"), detail.version],
  ].forEach(([label, value]) => grid.append(detailField(label, value)));
  technical.append(grid);
  const activity = node("details", "detail-block disclosure");
  activity.append(node("summary", "", t("timeline")));
  const events = eventList(detail.events || []);
  events.id = "detail-events";
  activity.append(events);
  if (detail.events_next_cursor)
    activity.append(
      button(t("loadEvents"), async (event) => {
        const control = event.currentTarget;
        control.disabled = true;
        try {
          const data = await BoardClient.get(
            `/api/work-items/${encodeURIComponent(detail.work_item_id)}/events?cursor=${encodeURIComponent(detail.events_next_cursor)}`,
          );
          if (state.selectedItem !== detail.work_item_id) return;
          events.append(...eventList(data.events).children);
          detail.events_next_cursor = data.next_cursor;
          control.hidden = !data.next_cursor;
        } catch (error) {
          showStatus(error.message, true);
        } finally {
          control.disabled = false;
        }
      }),
    );
  content.replaceChildren(
    renderLive(detail),
    renderActions(detail),
    composer,
    renderJourney(detail),
    description,
    renderExecutionHistory(detail),
    activity,
    technical,
  );
}
function composeOrExecute(command) {
  if (state.busy || !state.detail) return;
  if (
    ![
      "reply",
      "request_changes",
      "edit",
      "cancel",
      "retry",
      "retrigger",
    ].includes(command)
  ) {
    executeCommand(state.detail, command);
    return;
  }
  const detail = state.detail;
  const draft = { command, item: detail };
  state.composer = draft;
  const host = $("#composer-host");
  const form = node("form", "command-composer");
  const inputs = {};
  function input(label, name, value, multiline = true) {
    const field = node("label", "", label);
    const el = node(multiline ? "textarea" : "input");
    el.value = value || "";
    el.required = true;
    el.maxLength = name === "title" ? 200 : command === "edit" ? 20000 : 8000;
    if (multiline) el.rows = 4;
    field.append(el);
    form.append(field);
    inputs[name] = el;
  }
  if (command === "cancel") form.append(node("p", "", t("cancelHelp")));
  else if (command === "edit") {
    input(t("title"), "title", detail.title, false);
    input(t("requirement"), "description", detail.description);
  } else if (["reply", "request_changes"].includes(command))
    input(
      command === "reply" ? t("yourReply") : t("requestedChanges"),
      "body",
      "",
    );
  else {
    form.append(node("p", "", t("retry")));
    const field = node("label", "", t("routing"));
    const select = node("select");
    select.name = "routing_mode";
    select.append(
      Object.assign(node("option", "", t("auto")), { value: "auto" }),
    );
    (state.session.allowed_workers || []).forEach((worker) =>
      select.append(
        Object.assign(node("option", "", worker), { value: worker }),
      ),
    );
    select.value = detail.routing_mode === "manual" ? detail.agent : "auto";
    if (!select.value) select.value = "auto";
    field.append(select);
    if (command !== "retrigger") form.append(field);
    inputs.routing = select;
  }
  const row = node("div", "command-row");
  row.append(
    button(t("cancel"), () => {
      state.composer = null;
      host.replaceChildren();
      $("#detail-actions").replaceWith(renderActions(state.detail));
    }),
  );
  const send = node(
    "button",
    "primary-button",
    command === "cancel"
      ? t("confirmCancel")
      : command === "edit"
        ? t("save")
        : commandLabel(command),
  );
  send.type = "submit";
  row.append(send);
  draft.send = send;
  draft.notice = node("p", "notice");
  draft.confirm = button(t("reviewDraft"), () => {
    draft.item = state.detail;
    updateComposerState(state.detail);
  });
  form.append(draft.notice, draft.confirm, row);
  updateComposerState(detail);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const extra = {};
    Object.entries(inputs).forEach(([name, el]) => {
      if (name !== "routing") extra[name] = el.value;
    });
    if (inputs.routing && command !== "retrigger") {
      extra.routing_mode = inputs.routing.value === "auto" ? "auto" : "manual";
      extra.requested_worker =
        extra.routing_mode === "auto" ? null : inputs.routing.value;
    }
    executeCommand(draft.item, command, extra);
  });
  host.replaceChildren(form);
  (inputs.body || inputs.title || send).focus();
}
async function openDetail(id, command) {
  const request = ++state.detailRequest;
  state.selectedItem = id;
  state.composer = null;
  updateUrl({ item: id });
  feedback("#drawer-feedback", "");
  $("#drawer-title").textContent = t("loading");
  $("#drawer-content").replaceChildren(node("p", "notice", t("loading")));
  if (!$("#work-item-drawer").open) $("#work-item-drawer").showModal();
  try {
    const detail = await BoardClient.get(
      `/api/work-items/${encodeURIComponent(id)}`,
    );
    if (request !== state.detailRequest || state.selectedItem !== id) return;
    renderDetail(detail);
    if (command) composeOrExecute(command);
  } catch (error) {
    if (request === state.detailRequest) showStatus(error.message, true);
  }
}
function closeDetail() {
  state.detailRequest++;
  state.selectedItem = null;
  state.detail = null;
  state.composer = null;
  $("#work-item-drawer").close();
  updateUrl({ item: null });
}
function setBusy(value) {
  state.busy = value;
  $$(
    '.command-button, .command-composer button, #new-task-form button[type="submit"], .start-card-button',
  ).forEach((el) => {
    el.disabled =
      value ||
      (el.classList.contains("command-button") &&
        state.detail &&
        !canWrite(state.detail));
  });
  $$("dialog").forEach((el) => el.setAttribute("aria-busy", String(value)));
  if (state.detail) updateComposerState(state.detail);
}
async function executeCommand(item, command, extra = {}) {
  if (state.busy || !canWrite(item)) return;
  setBusy(true);
  feedback("#drawer-feedback", "");
  try {
    const url =
      command === "retrigger"
        ? `/api/tasks/${encodeURIComponent(item.task_id)}/retrigger`
        : `/api/work-items/${encodeURIComponent(item.work_item_id)}/commands`;
    const payload =
      command === "retrigger"
        ? {}
        : {
            command,
            expected_version: item.version,
            expected_event_id: attentionForCommand(item, command),
            ...extra,
          };
    const response = await BoardClient.post(
      url,
      payload,
      state.session.csrf_token,
    );
    if (state.selectedItem === item.work_item_id) {
      const detail =
        command === "retrigger"
          ? await BoardClient.get(
              `/api/work-items/${encodeURIComponent(item.work_item_id)}`,
            )
          : response;
      if (state.selectedItem === item.work_item_id) renderDetail(detail);
    }
    showStatus(t("commandCompleted", { action: commandLabel(command) }));
    await refreshBoard({ quiet: true });
    const current = state.board?.items.find(
      (card) => card.work_item_id === item.work_item_id,
    );
    if (current && COLUMNS.includes(current.column)) {
      state.mobileColumn = current.column;
      updateUrl({ column: current.column });
      renderMobileColumn();
    }
  } catch (error) {
    if (
      error.status === 409 &&
      error.payload?.current &&
      state.selectedItem === item.work_item_id
    )
      renderDetail(error.payload.current, { preserve: true });
    showStatus(
      `${error.message}${state.composer ? ` ${t("draftKept")}` : ""}`,
      true,
    );
  } finally {
    setBusy(false);
    if (state.detail && !state.composer)
      $("#detail-actions")?.replaceWith(renderActions(state.detail));
    if (state.board) renderBoard(state.board);
  }
}
async function loadSession() {
  state.session = await BoardClient.get("/api/session");
  $("#new-task-button").disabled = !state.session.writes_enabled;
  feedback(
    "#read-only-notice",
    state.session.writes_enabled
      ? ""
      : t("readOnly", {
          reason: state.session.write_error || t("writesDisabled"),
        }),
  );
  setOptions(
    $("#worker-select"),
    state.session.allowed_workers || [],
    (v) => v,
    null,
  );
}
async function refreshBoard({ quiet = false, more = false } = {}) {
  const request = ++state.boardRequest;
  const url = boardUrl();
  if (state.boardUrl !== url) state.pageCount = 1;
  state.boardUrl = url;
  const cursor = more ? state.board?.next_cursor : null;
  $("#sync-status").textContent = t("syncing");
  $("#load-more").disabled = true;
  try {
    if (!state.session) await loadSession();
    const data = more
      ? await BoardClient.get(boardUrl(cursor))
      : await BoardClient.pages(url, state.pageCount || 1);
    if (request !== state.boardRequest) return;
    if (more && state.board) {
      state.pageCount = (state.pageCount || 1) + 1;
      const merged = new Map(
        state.board.items.map((item) => [item.work_item_id, item]),
      );
      data.items.forEach((item) => merged.set(item.work_item_id, item));
      data.items = Array.from(merged.values());
    }
    state.board = data;
    renderFilters(data);
    renderBoard(data);
    feedback("#board-feedback", "");
    $("#sync-status").textContent = t("synced", {
      time: new Date().toLocaleTimeString(),
    });
    const selected = state.selectedItem;
    const selectionRequest = state.detailRequest;
    if (selected && !state.busy) {
      try {
        const detail = await BoardClient.get(
          `/api/work-items/${encodeURIComponent(selected)}`,
        );
        if (
          request === state.boardRequest &&
          selected === state.selectedItem &&
          selectionRequest === state.detailRequest &&
          !state.busy
        )
          renderDetail(detail, { preserve: true });
      } catch (error) {
        if (selected === state.selectedItem)
          feedback("#drawer-feedback", error.message);
      }
    }
    if (!quiet) showStatus(t("boardRefreshed"));
  } catch (error) {
    if (request === state.boardRequest) {
      feedback(
        "#board-feedback",
        t("boardRefreshFailed", { message: error.message }),
      );
      $("#sync-status").textContent = t("retryRefresh");
    }
  } finally {
    if (request === state.boardRequest) $("#load-more").disabled = false;
  }
}
async function createTask(event) {
  event.preventDefault();
  if (state.busy) return;
  const form = event.currentTarget;
  const payload = Object.fromEntries(new FormData(form).entries());
  if (payload.routing_mode !== "manual") payload.requested_worker = null;
  setBusy(true);
  feedback("#form-feedback", "");
  try {
    const detail = await BoardClient.post(
      "/api/work-items",
      payload,
      state.session.csrf_token,
    );
    form.reset();
    $("#worker-field").hidden = true;
    $("#new-task-dialog").close();
    await refreshBoard({ quiet: true });
    await openDetail(detail.work_item_id);
    showStatus(t("taskCreated"));
  } catch (error) {
    showStatus(error.message, true);
  } finally {
    setBusy(false);
  }
}
function bindControls() {
  const target = $("[data-cards=todo]");
  target.addEventListener("dragover", (event) => {
    if (
      !state.busy &&
      event.dataTransfer.types.includes("application/x-robert-column")
    ) {
      event.preventDefault();
      target.classList.add("drag-target");
    }
  });
  target.addEventListener("dragleave", () =>
    target.classList.remove("drag-target"),
  );
  target.addEventListener("drop", (event) => {
    event.preventDefault();
    target.classList.remove("drag-target");
    const item = state.board?.items.find(
      (card) => card.work_item_id === event.dataTransfer.getData("text/plain"),
    );
    if (item?.valid_commands.includes("start") && canWrite(item))
      executeCommand(item, "start");
  });
  $("#mobile-filter-toggle").addEventListener("click", (event) => {
    const open = $(".control-rail").classList.toggle("filters-open");
    event.currentTarget.setAttribute("aria-expanded", String(open));
  });
  $("#language-select").addEventListener("change", (event) => {
    state.language = event.target.value;
    localStorage.setItem("robertLanguage", state.language);
    applyTranslations();
    if (state.board) {
      renderFilters(state.board);
      renderBoard(state.board);
    }
    if (state.detail && !state.composer) renderDetail(state.detail);
  });
  $("#theme-select").addEventListener("change", (event) =>
    setTheme(event.target.value),
  );
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    if (state.theme === "system") setTheme("system");
  });
  $("#new-task-button").addEventListener("click", () => {
    if (!state.session?.writes_enabled) return;
    feedback("#form-feedback", "");
    $("#new-task-dialog").showModal();
  });
  $$("[data-close-new-task]").forEach((el) =>
    el.addEventListener("click", () => {
      if (!state.busy) $("#new-task-dialog").close();
    }),
  );
  $$("[data-close-drawer]").forEach((el) =>
    el.addEventListener("click", closeDetail),
  );
  $$("dialog").forEach((dialog) => {
    dialog.addEventListener("close", () =>
      document.body.append($("#status-message")),
    );
    dialog.addEventListener("cancel", (event) => {
      event.preventDefault();
      if (dialog.id === "work-item-drawer") closeDetail();
      else if (!state.busy) dialog.close();
    });
  });
  $("#new-task-form").addEventListener("submit", createTask);
  $("#new-task-form").addEventListener("invalid", (event) => {
    const field = event.target;
    const label = field.name === "title" ? t("title") : field.name === "description" ? t("requirement") : t("repository");
    field.setCustomValidity(t("requiredField", { field: label }));
    field.setAttribute("aria-invalid", "true");
    document.getElementById(field.getAttribute("aria-describedby")).textContent = field.validationMessage;
  }, true);
  $("#new-task-form").addEventListener("input", (event) => {
    event.target.setCustomValidity("");
    event.target.removeAttribute("aria-invalid");
    const error = document.getElementById(event.target.getAttribute("aria-describedby"));
    if (error) error.textContent = "";
  });
  $("#routing-mode").addEventListener("change", (event) => {
    $("#worker-field").hidden = event.target.value !== "manual";
  });
  $("#refresh-button").addEventListener("click", () => refreshBoard());
  $("#load-more").addEventListener("click", () =>
    refreshBoard({ quiet: true, more: true }),
  );
  [
    "#repo-filter",
    "#agent-filter",
    "#priority-filter",
    "#attention-filter",
  ].forEach((selector) =>
    $(selector).addEventListener("change", () => refreshBoard({ quiet: true })),
  );
  $("#view-filter").value = state.view;
  $("#view-filter").addEventListener("change", (event) => {
    state.view = event.target.value;
    updateUrl({ view: state.view === "active" ? null : state.view });
    refreshBoard({ quiet: true });
  });
  let searchTimer;
  $("#search-filter").addEventListener("input", () => {
    clearTimeout(searchTimer);
    state.boardRequest++;
    searchTimer = setTimeout(() => refreshBoard({ quiet: true }), 250);
  });
  $$("[data-mobile-column]").forEach((el) =>
    el.addEventListener("click", () => {
      state.mobileColumn = el.dataset.mobileColumn;
      updateUrl({ column: state.mobileColumn });
      renderMobileColumn({ scroll: true });
    }),
  );
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && !state.busy) refreshBoard({ quiet: true });
  });
  document.addEventListener("keydown", (event) => {
    if (
      event.key === "/" &&
      !event.metaKey &&
      !event.ctrlKey &&
      !$("dialog[open]") &&
      !["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName)
    ) {
      event.preventDefault();
      $("#search-filter").focus();
    }
  });
}
async function start() {
  applyTranslations();
  setTheme(state.theme);
  bindControls();
  renderMobileColumn();
  const selected = state.selectedItem;
  await refreshBoard({ quiet: true });
  if (selected) await openDetail(selected);
  setInterval(() => {
    if (!document.hidden && !state.busy && !$("#new-task-dialog").open)
      refreshBoard({ quiet: true });
  }, 15000);
}
start();
