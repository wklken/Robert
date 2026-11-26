"use strict";

const state = {
  filters: null,
  list: null,
  detail: null,
  selectedId: null,
  listController: null,
  detailController: null,
  refreshTimer: null,
  lastFocusedRow: null,
  hasLoaded: false,
  language: "en",
  theme: "system",
  session: null,
};

const translations = {
  en: {
    pageTitle: "Robert Workbench", workbenchControls: "Workbench controls", live: "Live",
    theme: "Theme", themeSystem: "System", themeLight: "Light", themeDark: "Dark",
    language: "Language", languageEnglish: "English", languageChinese: "Chinese",
    primaryNavigation: "Primary navigation", navWork: "Work", navBoard: "Board", navHistory: "History", navTimeline: "Timeline", navLiveDisplay: "Live display", navProposals: "Proposals",
    navOperations: "Operations", navKnowledge: "Knowledge", workTitle: "Work queue",
    workSubtitle: "Active GitHub work that still needs attention", historyTitle: "History",
    historySubtitle: "Completed, ignored and canceled GitHub work", workSummary: "Work summary",
    needsYou: "needs you", working: "working", waiting: "waiting", searchWork: "Search work",
    workSearchPlaceholder: "Search title, #number or task ID", repository: "Repository",
    allRepositories: "All repositories", actor: "Actor", anyActor: "Any actor", status: "Status",
    activeWork: "Active work", needsAttention: "Needs attention", agentWorking: "Agent working",
    sort: "Sort", priority: "Priority", newest: "Newest", oldest: "Oldest", retry: "Retry",
    githubWorkItems: "GitHub work items", loadMore: "Load more", selectWorkItem: "Select a PR or issue",
    selectWorkItemHelp: "Choose a work item to inspect its current Agent state.",
    workItemDetail: "Work item detail", closeDetail: "Close detail", unknown: "Unknown",
    refreshFailed: "Refresh failed", refreshError: "Could not refresh work: {message}",
    secondsAgo: "{count}s ago", minutesAgo: "{count}m ago", hoursAgo: "{count}h ago", daysAgo: "{count}d ago",
    bucketAttention: "Needs your attention", bucketWorking: "Agent working", bucketWaiting: "Waiting",
    bucketHistory: "History", publishFailed: "publish failed", publishSkipped: "publish skipped",
    taskFailed: "task failed", taskCanceled: "task canceled", workerStale: "worker stale",
    actionRejected: "action rejected", workstreamFailed: "workstream failed",
    waitingForInput: "waiting for input", waitingToPublish: "waiting to publish",
    pendingAudit: "pending audit", active: "active", complete: "Workflow ended", publish: "Publish",
    untitledWorkItem: "Untitled GitHub work item", openedBy: "opened by @{actor}", updated: "updated {time}",
    noMatches: "No work matches these filters", noActiveWork: "No active GitHub work needs attention",
    adjustFilters: "Adjust filters, check History for ended workflows, or open Board for local tasks and their lifecycle.",
    scopeNote: "Counts match the repository, actor and search filters. This queue tracks GitHub workflows; Board tracks task lifecycle, including local tasks.",
    outsideResults: "Shared selection is outside the loaded results. Clear filters or return to the list to change selection.",
    oldestUpdated: "Oldest update first", workflowState: "Workflow state", taskState: "Task lifecycle", noOutcome: "No delivery summary recorded. Inspect execution history for details.",
    backlog: "Backlog", todo: "Queued", doing: "In progress", review: "Awaiting review", done: "Completed", history: "Canceled",
    mostRecentlyUpdated: "Most recently updated", actionabilityOrder: "Actionability order",
    workItemsLoaded: "{count} work items loaded", whyAttention: "Why this needs attention",
    currentAgentState: "Current agent state", waitingReason: "What the agent is waiting for", outcome: "Outcome",
    workItem: "Work item", noOperatorAction: "No operator action is currently required.",
    discoveredAuthorized: "Discovered and authorized", workstreamRecorded: "Workstream recorded",
    agentExecution: "Agent execution", openGithub: "Open on GitHub ↗", copyTaskId: "Copy task ID",
    copyShareLink: "Copy share link", actions: "Actions", artifacts: "Artifacts", bytes: "{count} bytes",
    preview: "Preview", workItemUnavailable: "Work item unavailable", preparation: "Preparation",
    analysis: "Analysis", planning: "Planning", implementation: "Implementation", verification: "Verification",
    publication: "Publication", handoff: "Handoff", reviewComment: "Review comment",
    prReviewComment: "PR review comment", issueComment: "Issue comment", openPullRequest: "Open pull request",
    createPullRequest: "Create pull request", updatePullRequest: "Update pull request",
    retriggerTask: "Run again", retriggeringTask: "Retriggering...",
    retriggerFailed: "Could not retrigger task: {message}",
    failed_timeout: "Worker timed out", failed_worker_process_exited: "Worker process exited unexpectedly", failed_worker_startup: "Worker failed to start", needs_resume: "Attempt interrupted; recovery was required",
    published: "Published", failed: "Failed", succeeded: "Succeeded", completed: "Completed",
    closed: "Closed", open: "Open", ignored: "Ignored", pending: "Pending", skipped: "Skipped", running: "Running", canceled: "Canceled",
    waiting_for_user: "Waiting for a reply", queued: "Queued", stale: "Interrupted", not_published: "Awaiting publication", publish_failed: "Publication failed",
    nowLabel: "What is happening", progressLabel: "How far it got", traceLabel: "Look here only when tracing back",
    factWhat: "What happened", factCause: "Why", factOwner: "Who acts", nextStep: "Next step",
    headlineAttention: "This work needs you", headlineWorking: "The Agent is working; nothing to do",
    headlineWaiting: "Waiting for a reply on GitHub", headlineHistory: "This workflow has ended",
    owner_operator: "You (an operator with write access)", owner_initiator: "The requester", owner_admin: "The runtime administrator",
    owner_github: "Reviewers on GitHub", owner_robert: "Robert, automatically", owner_none: "Nobody; nothing left to do",
    ownerAttention: "You (an operator with write access)", ownerWorking: "Robert, automatically", ownerWaiting: "A trusted GitHub user", ownerHistory: "Nobody; nothing left to do",
    causeUnknown: "Cause not identified yet. Open “Execution history” below to read the error log.",
    rawText: "Original text", handleOnBoard: "Handle on the board", viewOnBoard: "View lifecycle on board",
    retriggerWill: "Robert re-checks the GitHub source and queues a new run; published comments and PRs stay.",
    nothingToDo: "Nothing to do here. This page refreshes by itself.",
    timingTitle: "Timing and waiting", timingWhy: "Where the time went, for slow runs",
    notificationsTitle: "Notification delivery", notificationsWhy: "Whether people were notified",
    historyWhy: "Every attempt with its error, logs and reported steps", publicationsWhy: "What Robert published to GitHub",
    artifactsWhy: "Files not tied to a single attempt", technicalTitle: "Technical details", technicalWhy: "IDs to copy when reporting a problem",
    attemptsBadge: "{count} attempt(s) · {failed} failed", attempt: "Attempt {number}", missingArtifact: "File absent on this host; only its database reference is available.",
    step_classification_result: "Classify requirement", step_new_pr: "Implement and prepare PR", step_pr_update: "Update PR", step_pr_review_comment: "PR review",
    step_review: "Review", step_multi_agent_review_report: "Multi-model review", step_multi_agent_vote_report: "Multi-Agent vote",
    artifact_worker_stderr: "Error log", artifact_worker_stdout: "Run output", artifact_prompt: "Instructions to the Agent",
    artifact_github_context_json: "GitHub context (JSON)", artifact_github_context_md: "GitHub context", artifact_result: "Result",
    reason_snapshot_changed: "The PR changed while Robert was reading it (probably a new push); it needs to be read again.",
    reason_zombie: "The worker process exited unexpectedly.", reason_timeout: "The run timed out.",
    reason_model_unavailable: "The model needed for this step is unavailable.", reason_worktree: "The run workspace is unavailable.",
    reason_rate_limit: "GitHub rate-limited the request.", reason_permission: "Robert lacks permission for this action.",
    copied: "Copied",
  },
  zh: {
    pageTitle: "Robert 工作台", workbenchControls: "工作台控制", live: "实时",
    theme: "主题", themeSystem: "跟随系统", themeLight: "浅色", themeDark: "深色",
    language: "语言", languageEnglish: "English", languageChinese: "中文",
    primaryNavigation: "一级导航", navWork: "工作", navBoard: "任务看板", navHistory: "历史", navTimeline: "时间线", navLiveDisplay: "大屏", navProposals: "提案",
    navOperations: "运行状态", navKnowledge: "知识", workTitle: "工作队列",
    workSubtitle: "仍需处理的活跃 GitHub 工作", historyTitle: "历史",
    historySubtitle: "已完成、已忽略和已取消的 GitHub 工作", workSummary: "工作摘要",
    needsYou: "需要你处理", working: "进行中", waiting: "等待中", searchWork: "搜索工作",
    workSearchPlaceholder: "搜索标题、编号或 task ID", repository: "仓库",
    allRepositories: "全部仓库", actor: "发起人", anyActor: "全部发起人", status: "状态",
    activeWork: "活跃工作", needsAttention: "需要处理", agentWorking: "Agent 工作中",
    sort: "排序", priority: "优先级", newest: "最新", oldest: "最早", retry: "重试",
    githubWorkItems: "GitHub 工作项", loadMore: "加载更多", selectWorkItem: "选择一个 PR 或 Issue",
    selectWorkItemHelp: "选择工作项以查看当前 Agent 状态。", workItemDetail: "工作项详情",
    closeDetail: "关闭详情", unknown: "未知", refreshFailed: "刷新失败",
    refreshError: "刷新工作列表失败：{message}", secondsAgo: "{count} 秒前", minutesAgo: "{count} 分钟前",
    hoursAgo: "{count} 小时前", daysAgo: "{count} 天前", bucketAttention: "需要你处理",
    bucketWorking: "Agent 工作中", bucketWaiting: "等待中", bucketHistory: "历史",
    publishFailed: "发布失败", publishSkipped: "已跳过发布", taskFailed: "任务失败",
    taskCanceled: "任务已取消", workerStale: "Worker 心跳过期", actionRejected: "动作被拒绝",
    workstreamFailed: "工作流失败", waitingForInput: "等待输入", waitingToPublish: "等待发布",
    pendingAudit: "等待审计", active: "活跃", complete: "工作流已结束", publish: "发布",
    untitledWorkItem: "未命名的 GitHub 工作项", openedBy: "由 @{actor} 发起", updated: "更新于 {time}",
    noMatches: "没有符合筛选条件的工作", noActiveWork: "暂无需要处理的活跃 GitHub 工作",
    scopeNote: "数量匹配当前仓库、发起人和搜索条件。此页统计 GitHub 工作流；看板按任务生命周期统计，包含本地任务，两处需要处理的数量可能不同。",
    adjustFilters: "调整筛选条件，在历史查看已结束的工作流，或进入任务看板查看本地任务与生命周期。", mostRecentlyUpdated: "按最近更新时间",
    outsideResults: "分享链接中的工作项不在当前已加载结果中，可清除筛选或返回列表重新选择。",
    oldestUpdated: "按更新时间升序（最早优先）", workflowState: "工作流状态", taskState: "任务生命周期", noOutcome: "暂无交付摘要，请查看执行记录。",
    backlog: "待整理", todo: "已排队", doing: "执行中", review: "待评审", done: "已完成", history: "已取消",
    actionabilityOrder: "按处理优先级", workItemsLoaded: "已加载 {count} 个工作项",
    whyAttention: "需要处理的原因", currentAgentState: "当前 Agent 状态",
    waitingReason: "Agent 正在等待什么", outcome: "结果", workItem: "工作项",
    noOperatorAction: "当前无需人工操作。", discoveredAuthorized: "已发现并授权",
    workstreamRecorded: "已记录工作流", agentExecution: "Agent 执行", openGithub: "在 GitHub 打开 ↗",
    copyTaskId: "复制 task ID", copyShareLink: "复制分享链接", actions: "操作", artifacts: "产物",
    bytes: "{count} 字节", preview: "预览", workItemUnavailable: "工作项不可用", preparation: "准备",
    analysis: "分析", planning: "规划", implementation: "实现", verification: "验证", publication: "发布",
    handoff: "交接", reviewComment: "评审评论", prReviewComment: "PR 行级评论",
    issueComment: "Issue 评论", openPullRequest: "创建 Pull Request", createPullRequest: "创建 Pull Request",
    updatePullRequest: "更新 Pull Request",
    retriggerTask: "重新触发执行", retriggeringTask: "正在重新触发...",
    retriggerFailed: "重新触发失败：{message}",
    failed_timeout: "Worker 执行超时", failed_worker_process_exited: "Worker 进程意外退出", failed_worker_startup: "Worker 启动失败", needs_resume: "本次执行中断，进入恢复流程",
    published: "已发布", failed: "失败", succeeded: "成功", completed: "已完成",
    closed: "已关闭", open: "进行中", ignored: "已忽略", pending: "待处理", skipped: "已跳过", running: "运行中", canceled: "已取消",
    waiting_for_user: "等待回复", queued: "已排队", stale: "已中断", not_published: "等待发布", publish_failed: "发布失败",
    nowLabel: "现在怎么样", progressLabel: "走到哪一步了", traceLabel: "需要追溯时再看",
    factWhat: "发生了什么", factCause: "原因", factOwner: "谁处理", nextStep: "下一步",
    headlineAttention: "这项工作需要你处理", headlineWorking: "Agent 正在处理，你不用操作",
    headlineWaiting: "在等 GitHub 上的回复", headlineHistory: "工作流已结束",
    owner_operator: "你（有写权限的操作者）", owner_initiator: "任务发起人", owner_admin: "运行管理员",
    owner_github: "GitHub 上的评审人", owner_robert: "Robert 自动处理", owner_none: "无需处理",
    ownerAttention: "你（有写权限的操作者）", ownerWorking: "Robert 自动处理", ownerWaiting: "可信的 GitHub 用户", ownerHistory: "无需处理",
    causeUnknown: "原因待定位。展开下方「执行历史」查看错误日志。",
    rawText: "原文", handleOnBoard: "去看板处理", viewOnBoard: "在看板查看完整生命周期",
    retriggerWill: "Robert 会重新校验 GitHub 来源并新建一次执行；已发布的评论和 PR 保留。",
    nothingToDo: "这里不需要操作，页面会自动刷新。",
    timingTitle: "耗时与等待", timingWhy: "执行慢时看时间花在哪里",
    notificationsTitle: "通知送达", notificationsWhy: "相关的人有没有收到通知",
    historyWhy: "每一次执行的错误、日志和上报步骤", publicationsWhy: "Robert 向 GitHub 发布了什么",
    artifactsWhy: "不属于某一次执行的文件", technicalTitle: "技术信息", technicalWhy: "反馈问题时复制给开发者的编号",
    attemptsBadge: "{count} 次执行 · {failed} 次失败", attempt: "第 {number} 次执行", missingArtifact: "当前机器没有此文件，数据库仅保存了文件引用。",
    step_classification_result: "需求分析与分流", step_new_pr: "实现需求并准备 PR", step_pr_update: "更新 PR", step_pr_review_comment: "PR 评审",
    step_review: "评审", step_multi_agent_review_report: "多模型评审", step_multi_agent_vote_report: "多 Agent 投票",
    artifact_worker_stderr: "错误日志", artifact_worker_stdout: "运行输出", artifact_prompt: "给 Agent 的指令",
    artifact_github_context_json: "GitHub 上下文（JSON）", artifact_github_context_md: "GitHub 上下文", artifact_result: "执行结果",
    reason_snapshot_changed: "读取期间 PR 内容发生了变化（多半是有新的推送），需要重新读取 PR。",
    reason_zombie: "执行进程意外退出。", reason_timeout: "执行超时。",
    reason_model_unavailable: "这一步需要的模型不可用。", reason_worktree: "运行工作区不可用。",
    reason_rate_limit: "GitHub 接口限流。", reason_permission: "Robert 没有执行这个操作的权限。",
    copied: "已复制",
  },
};

const bucketLabels = {
  needs_attention: "bucketAttention",
  working: "bucketWorking",
  waiting: "bucketWaiting",
  history: "bucketHistory",
};

const reasonLabels = {
  publish_failed: "publishFailed", publish_skipped: "publishSkipped", task_failed: "taskFailed",
  task_canceled: "taskCanceled", worker_stale: "workerStale", github_action_rejected: "actionRejected",
  workstream_failed: "workstreamFailed", waiting_for_user: "waitingForInput",
  waiting_publish: "waitingToPublish", result_pending_audit: "pendingAudit",
  agent_working: "agentWorking", active_workstream: "active", inactive: "complete",
};

const phaseLabels = {
  prepare: "preparation", analyze: "analysis", plan: "planning", execute: "implementation",
  verify: "verification", publish: "publication", handoff: "handoff",
};

const actionLabels = {
  review_comment: "reviewComment", pr_review_comment: "prReviewComment", issue_comment: "issueComment",
  open_pr: "openPullRequest", create_pr: "createPullRequest", push_existing_pr: "updatePullRequest",
};

// Known worker and runtime errors, phrased for people who do not read stack traces.
const REASON_PATTERNS = [
  [/snapshot changed while fetching/i, "reason_snapshot_changed"],
  [/process_missing_or_zombie|zombie/i, "reason_zombie"],
  [/timed? ?out|timeout/i, "reason_timeout"],
  [/model unavailable/i, "reason_model_unavailable"],
  [/unmanaged_worktree|worktree.*unavailable/i, "reason_worktree"],
  [/rate.?limit/i, "reason_rate_limit"],
  [/permission denied|forbidden|not authori[sz]ed/i, "reason_permission"],
];

const BUCKET_TONES = { needs_attention: "danger", working: "running", waiting: "attention", history: "neutral" };

function $(id) { return document.getElementById(id); }

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
  document.querySelectorAll("[data-i18n]").forEach((element) => {
    element.textContent = t(element.dataset.i18n);
  });
  document.querySelectorAll("[data-i18n-placeholder]").forEach((element) => {
    element.placeholder = t(element.dataset.i18nPlaceholder);
  });
  document.querySelectorAll("[data-i18n-aria]").forEach((element) => {
    element.setAttribute("aria-label", t(element.dataset.i18nAria));
  });
  $("language-select").value = state.language;
  $("theme-select").value = state.theme;
}

function setLanguage(value) {
  state.language = translations[value] ? value : "en";
  localStorage.setItem("robertLanguage", state.language);
  applyTranslations();
  syncControls();
  if (state.list) renderWorkItems(state.list);
  if (state.detail) renderWorkItemDetail(state.detail);
}

function node(tag, options = {}, children = []) {
  const element = document.createElement(tag);
  if (options.className) element.className = options.className;
  if (options.text !== undefined) element.textContent = options.text;
  if (options.type) element.type = options.type;
  if (options.href && (/^https?:\/\//i.test(options.href) || (/^\/(?!\/)/.test(options.href) && !options.href.includes("\\")))) {
    try {
      const target = new URL(options.href, window.location.href);
      if (["http:", "https:"].includes(target.protocol)) element.href = options.href;
    } catch (_) { /* Invalid stored links remain plain text. */ }
  }
  if (options.target) element.target = options.target;
  if (options.rel) element.rel = options.rel;
  if (options.title) element.title = options.title;
  if (options.dataset) Object.assign(element.dataset, options.dataset);
  if (options.attributes) {
    Object.entries(options.attributes).forEach(([name, value]) => element.setAttribute(name, value));
  }
  children.filter(Boolean).forEach((child) => element.append(child));
  return element;
}

function readUrlState() {
  const params = new URLSearchParams(window.location.search);
  const hash = window.location.hash.match(/^#work\/(.+)$/);
  let selectedId = null;
  try { selectedId = hash ? decodeURIComponent(hash[1]) : null; } catch (_) { /* Ignore invalid fragments. */ }
  return {
    bucket: params.get("bucket") || "",
    repo: params.get("repo") || "",
    actor: params.get("actor") || "",
    q: params.get("q") || "",
    sort: params.get("sort") || "priority",
    cursor: params.get("cursor") || "",
    selectedId,
  };
}

function writeUrlState({ replace = true } = {}) {
  const params = new URLSearchParams();
  ["bucket", "repo", "actor", "q", "sort", "cursor"].forEach((name) => {
    const value = state.filters[name];
    if (value && !(name === "sort" && value === "priority")) params.set(name, value);
  });
  const query = params.toString();
  const hash = state.selectedId ? `#work/${encodeURIComponent(state.selectedId)}` : "";
  const url = `${window.location.pathname}${query ? `?${query}` : ""}${hash}`;
  window.history[replace ? "replaceState" : "pushState"](null, "", url);
}

function listApiUrl() {
  const params = new URLSearchParams();
  ["bucket", "repo", "actor", "q", "sort", "cursor"].forEach((name) => {
    const value = state.filters[name];
    if (value && !(name === "sort" && value === "priority")) params.set(name, value);
  });
  params.set("limit", "30");
  return `/api/work-items?${params.toString()}`;
}

function formatRelative(value) {
  if (!value) return t("unknown");
  const time = new Date(value).getTime();
  if (Number.isNaN(time)) return value;
  const seconds = Math.max(0, Math.floor((Date.now() - time) / 1000));
  if (seconds < 60) return t("secondsAgo", { count: seconds });
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return t("minutesAgo", { count: minutes });
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return t("hoursAgo", { count: hours });
  const days = Math.floor(hours / 24);
  return days < 30 ? t("daysAgo", { count: days }) : new Date(value).toLocaleDateString(state.language === "zh" ? "zh-CN" : "en");
}

function formatTime(value) {
  if (!value) return "—";
  const date = new Date(/^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$/.test(value) ? value.replace(" ", "T") + "Z" : value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString(state.language === "zh" ? "zh-CN" : "en");
}

function columnLabel(column) {
  return t({ history: "canceled", waiting: "needsAttention" }[column] || column);
}

function reasonLabel(item) {
  if (item.bucket === "history" && item.lifecycle) return columnLabel(item.lifecycle.column);
  return t(reasonLabels[item.reason_code] || item.reason_code || item.bucket);
}

function sortDescription() {
  return t(state.filters.sort === "updated-asc" ? "oldestUpdated" : state.filters.sort === "updated-desc" ? "mostRecentlyUpdated" : "actionabilityOrder");
}

function sourceLabel(source) {
  const type = source.type === "issue" ? "Issue" : source.type === "pull_request" ? "PR" : t("workItem");
  return source.number ? `${type} #${source.number}` : type;
}

function humanize(value) {
  if (!value) return t("unknown");
  const normalized = String(value).toLowerCase();
  if ((translations[state.language] || {})[normalized] || translations.en[normalized]) return t(normalized);
  const text = String(value).replaceAll("_", " ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function humanizeReason(value) {
  const raw = String(value || "").trim();
  if (!raw) return null;
  if (t(raw) !== raw) return { text: t(raw), raw };
  const match = REASON_PATTERNS.find(([pattern]) => pattern.test(raw));
  return { text: match ? t(match[1]) : raw, raw };
}

function toneOf(item) {
  return item.lifecycle?.status_summary?.tone || BUCKET_TONES[item.bucket] || "neutral";
}

function stepName(task) {
  const code = task.expected_output || task.route_id || "task";
  return t(`step_${code}`) === `step_${code}` ? humanize(code) : t(`step_${code}`);
}

function latestFailure(detail) {
  const attempts = [...(detail.attempts || [])].filter((attempt) => attempt.failure_summary)
    .sort((a, b) => (b.started_at || "").localeCompare(a.started_at || "") || b.attempt_no - a.attempt_no);
  if (attempts.length) return { raw: attempts[0].failure_summary, attempt: attempts[0] };
  const action = (detail.actions || []).find((candidate) => candidate.safe_error);
  return action ? { raw: action.safe_error } : null;
}

function disclosure(key, title, why, badge, { className = "detail-section disclosure-card", warn = false } = {}) {
  const box = node("details", { className, dataset: { disclosure: key } });
  box.append(node("summary", {}, [
    node("span", {}, [node("span", { className: "disclosure-title", text: title }), node("span", { className: "disclosure-why", text: why })]),
    badge ? node("span", { className: `disclosure-badge${warn ? " warn" : ""}`, text: badge }) : null,
  ]));
  return box;
}

function showSkeleton() {
  const list = $("work-list");
  list.replaceChildren($("skeleton-template").content.cloneNode(true));
}

function showRefreshError(message) {
  $("refresh-error").textContent = t("refreshError", { message });
  $("refresh-notice").hidden = false;
  $("refresh-label").textContent = t("refreshFailed");
}

function clearRefreshError() {
  $("refresh-notice").hidden = true;
  $("refresh-label").textContent = t("live");
}

function stateIcon(item) {
  const bucket = item.bucket;
  const symbol = bucket === "history" ? (item.lifecycle?.column === "done" ? "✓" : item.lifecycle?.column === "history" ? "×" : item.lifecycle ? "◷" : "—") : bucket === "needs_attention" ? "!" : bucket === "waiting" ? "◷" : "●";
  return node("span", {
    className: `state-icon ${bucket === "needs_attention" ? "attention" : bucket === "history" && item.lifecycle?.column !== "done" ? "" : bucket}`,
    text: symbol,
    attributes: { "aria-hidden": "true" },
  });
}

function labelFor(item) {
  return node("span", {
    className: `label ${item.bucket === "needs_attention" ? "attention" : item.bucket}`,
    text: reasonLabel(item),
  });
}

function signalFor(item) {
  const signals = node("div", { className: "work-signals" });
  if (item.bucket === "history") return signals;
  if (item.signals.failed_publish_actions) {
    signals.append(node("span", { className: "signal attention" }, [node("i"), node("span", { text: t("publish") })]));
  } else if (item.agent.attempt_status) {
    const live = item.agent.attempt_status === "running";
    signals.append(node("span", { className: `signal ${live ? "success" : ""}` }, [node("i"), node("span", { text: humanize(item.agent.attempt_status) })]));
  }
  return signals;
}

function workRow(item) {
  const source = item.source || {};
  const titleLine = node("div", { className: "work-title-line" }, [
    node("strong", { text: source.title || t("untitledWorkItem") }),
    labelFor(item),
  ]);
  const meta = node("div", { className: "work-meta" });
  [source.repo_full_name, source.number ? sourceLabel(source) : null, source.author_login ? t("openedBy", { actor: source.author_login }) : null].filter(Boolean).forEach((value) => meta.append(node("span", { text: value })));
  meta.append(node("span", { text: t("updated", { time: formatRelative(item.updated_at) }) }));
  const why = item.bucket === "needs_attention" ? node("p", { className: "work-why", text: reasonSummary(item) }) : null;
  const row = node("button", {
    className: `work-row ${item.id === state.selectedId ? "selected" : ""}`,
    type: "button",
    dataset: { workstreamId: item.id, tone: toneOf(item) },
    attributes: { "aria-pressed": item.id === state.selectedId ? "true" : "false" },
  }, [stateIcon(item), node("span", { className: "work-copy" }, [titleLine, why, meta]), signalFor(item)]);
  row.addEventListener("click", () => selectWorkItem(item.id, row));
  const entry = node("div", { className: "work-entry" }, [row]);
  if (/^https?:\/\//i.test(source.url || "")) {
    entry.append(node("a", { className: "work-github-link", text: t("openGithub"), href: source.url, target: "_blank", rel: "noopener noreferrer" }));
  }
  return entry;
}

function renderFilterOptions(payload) {
  const repoSelect = $("repo-filter");
  const repoOptions = [node("option", { text: t("allRepositories"), attributes: { value: "" } })];
  (payload.repositories || []).forEach((repo) => repoOptions.push(node("option", { text: repo.full_name, attributes: { value: repo.full_name } })));
  repoSelect.replaceChildren(...repoOptions);
  repoSelect.value = state.filters.repo;
  const actorSelect = $("actor-filter");
  const actorOptions = [node("option", { text: t("anyActor"), attributes: { value: "" } })];
  (payload.actors || []).forEach((actor) => actorOptions.push(node("option", { text: `@${actor}`, attributes: { value: actor } })));
  actorSelect.replaceChildren(...actorOptions);
  actorSelect.value = state.filters.actor;
}

function renderWorkItems(payload, { append = false } = {}) {
  const list = $("work-list");
  const scrollTop = window.scrollY;
  const items = payload.items || [];
  renderFilterOptions(payload);
  if (!append) list.replaceChildren();
  if (!items.length && !append) {
    list.append(node("div", { className: "empty-state" }, [
      node("h2", { text: state.filters.q || state.filters.repo || state.filters.bucket ? t("noMatches") : t("noActiveWork") }),
      node("p", { text: t("adjustFilters") }),
      node("a", { text: t("navBoard"), href: "/board" }),
    ]));
  } else {
    const grouped = new Map();
    items.forEach((item) => {
      if (!grouped.has(item.bucket)) grouped.set(item.bucket, []);
      grouped.get(item.bucket).push(item);
    });
    grouped.forEach((group, bucket) => {
      const box = node("section", { className: "box", dataset: { bucket } });
      box.append(node("header", { className: "box-header" }, [
        node("strong", { text: `${t(bucketLabels[bucket] || bucket)} · ${group.length}` }),
        node("span", { text: sortDescription() }),
      ]));
      group.forEach((item) => box.append(workRow(item)));
      list.append(box);
    });
  }
  const counts = payload.counts || {};
  $("count-attention").textContent = counts.needs_attention || 0;
  $("count-working").textContent = counts.working || 0;
  $("count-waiting").textContent = counts.waiting || 0;
  $("nav-work-count").textContent = (counts.needs_attention || 0) + (counts.working || 0) + (counts.waiting || 0);
  $("nav-history-count").textContent = counts.history || 0;
  $("pagination").hidden = !payload.next_cursor;
  $("list-status").textContent = t("workItemsLoaded", { count: items.length });
  window.scrollTo({ top: scrollTop });
}

function timelineRow(label, time, tone = "") {
  return node("div", { className: `timeline-row ${tone}` }, [
    node("b", { className: "timeline-icon", text: tone === "done" ? "✓" : tone === "failed" ? "!" : "●" }),
    node("span", { text: label }),
    node("time", { text: formatTime(time) }),
  ]);
}

async function loadControlSession() {
  try {
    const response = await fetch("/api/session", { cache: "no-store" });
    state.session = response.ok ? await response.json() : null;
  } catch (_error) {
    state.session = null;
  }
}

async function retriggerTask(taskId, button) {
  if (!taskId || !state.session?.writes_enabled) return;
  const previousText = button.textContent;
  button.disabled = true;
  button.textContent = t("retriggeringTask");
  try {
    await BoardClient.post(`/api/tasks/${encodeURIComponent(taskId)}/retrigger`, {}, state.session.csrf_token);
    await loadWorkItems();
  } catch (error) {
    button.disabled = false;
    button.textContent = previousText;
    window.alert(t("retriggerFailed", { message: error.message }));
  }
}

function artifactList(items) {
  const artifacts = node("div", { className: "artifact-list" });
  items.forEach((artifact) => {
    const name = t(`artifact_${artifact.artifact_type}`) === `artifact_${artifact.artifact_type}` ? artifact.artifact_type : t(`artifact_${artifact.artifact_type}`);
    if (!artifact.preview_available) {
      artifacts.append(node("p", { className: "raw-detail", text: `${name} — ${t("missingArtifact")}` }));
      return;
    }
    const href = `/artifact.txt?${new URLSearchParams({ task_id: artifact.task_id, artifact_type: artifact.artifact_type, artifact_id: artifact.artifact_id })}`;
    artifacts.append(node("a", { className: "artifact-link", href, target: "_blank", rel: "noopener noreferrer", title: artifact.artifact_type }, [node("span", { text: name }), node("small", { text: artifact.bytes ? t("bytes", { count: artifact.bytes }) : t("preview") })]));
  });
  return artifacts;
}

function reasonSummary(data) {
  const labels = {
    inactive: ["Workflow ended; see the progress below for the delivery outcome.", "工作流已结束，交付结果见下方进度。"],
    task_failed: ["The current task failed.", "当前任务执行失败。"], task_canceled: ["The current task was canceled.", "当前任务已取消。"],
    agent_working: ["The agent is processing this work.", "Agent 正在处理该工作。"],
    active_workstream: ["The workflow is active.", "工作流仍在推进。"],
    waiting_for_user: ["Waiting for a trusted GitHub user response.", "等待可信 GitHub 用户回复。"],
    publish_failed: ["Publishing to GitHub failed.", "发布到 GitHub 失败。"],
    publish_skipped: ["An accepted GitHub action was skipped during publication.", "一个已通过审核的 GitHub 动作在发布时被跳过。"],
    worker_stale: ["The worker stopped sending heartbeats.", "Worker 停止上报心跳，可能已中断。"],
    github_action_rejected: ["A planned GitHub action failed policy or audit checks.", "计划中的 GitHub 动作没有通过策略或审核检查。"],
    workstream_failed: ["The GitHub workflow is in a failed state.", "GitHub 工作流处于失败状态。"],
    waiting_publish: ["An accepted action is waiting for publication.", "已接受的动作正在等待发布。"],
    result_pending_audit: ["The worker result is waiting for audit.", "执行结果正在等待审计。"],
  };
  return labels[data.reason_code]?.[state.language === "zh" ? 1 : 0] || data.reason_summary || t("noOperatorAction");
}

function headlineFor(bucket) {
  return t({ needs_attention: "headlineAttention", working: "headlineWorking", waiting: "headlineWaiting" }[bucket] || "headlineHistory");
}

function ownerFor(detail, bucket) {
  const owner = detail.lifecycle?.status_summary?.owner;
  if (owner && bucket !== "history") return t(`owner_${owner}`);
  return t({ needs_attention: "ownerAttention", working: "ownerWorking", waiting: "ownerWaiting" }[bucket] || "ownerHistory");
}

function nowSection(detail) {
  const stateData = detail.operator_state || {};
  const bucket = stateData.bucket;
  const section = node("section", { className: "detail-section now-panel", dataset: { tone: toneOf({ ...detail, bucket }) } });
  section.append(node("h3", { className: "now-head", text: headlineFor(bucket) }));
  const facts = node("dl", { className: "facts" });
  const add = (term, value) => facts.append(node("dt", { text: term }), typeof value === "string" ? node("dd", { text: value }) : value);
  add(t("factWhat"), reasonSummary(stateData));
  if (bucket === "needs_attention") {
    const failure = latestFailure(detail);
    const reason = humanizeReason(failure?.raw);
    const value = node("dd");
    if (!reason) value.append(node("span", { className: "fact-unknown", text: t("causeUnknown") }));
    else {
      value.append(node("span", { text: reason.text }));
      if (failure.attempt) value.append(node("span", { className: "fact-source", text: `${t("attempt", { number: failure.attempt.attempt_no })} · ${formatTime(failure.attempt.finished_at || failure.attempt.started_at)}` }));
      if (reason.raw !== reason.text) value.append(node("span", { className: "fact-source raw-detail" }, [node("span", { text: `${t("rawText")}: ` }), node("code", { text: reason.raw })]));
    }
    add(t("factCause"), value);
  }
  add(t("factOwner"), ownerFor(detail, bucket));
  section.append(facts);

  const actions = node("div", { className: "detail-actions" });
  const canRetrigger = detail.controls?.can_retrigger && state.session?.writes_enabled && detail.controls.task_id;
  if (canRetrigger) {
    const retrigger = node("button", {
      className: "action-button primary",
      text: t("retriggerTask"),
      type: "button",
      dataset: { retriggerTask: detail.controls.task_id },
    });
    retrigger.addEventListener("click", () => retriggerTask(detail.controls.task_id, retrigger));
    actions.append(retrigger);
  }
  const needsBoard = detail.lifecycle?.status_summary?.needs_action;
  if (detail.work_item_id) actions.append(node("a", {
    className: `action-button${needsBoard && !canRetrigger ? " primary" : ""}`,
    text: needsBoard ? t("handleOnBoard") : t("viewOnBoard"),
    href: `/board?item=${encodeURIComponent(detail.work_item_id)}`,
  }));
  const source = detail.source || {};
  if (/^https?:\/\//i.test(source.url || "")) actions.append(node("a", { className: "action-button", text: t("openGithub"), href: source.url, target: "_blank", rel: "noopener noreferrer" }));
  section.append(node("div", { className: "next-step" }, [node("h4", { text: t("nextStep") }), actions]));
  if (canRetrigger) section.append(node("p", { className: "consequence", text: t("retriggerWill") }));
  else if (bucket !== "needs_attention" && !needsBoard) section.append(node("p", { className: "no-action", text: t("nothingToDo") }));
  return section;
}

function progressSection(detail) {
  const section = node("section", { className: "detail-section progress-section" });
  const lifecycle = detail.lifecycle;
  if (lifecycle) {
    section.append(node("p", { className: "progress-column", text: `${t("taskState")}: ${columnLabel(lifecycle.column)}` }));
    const pr = lifecycle.pr;
    if (pr?.url) section.append(node("a", { className: "pr-link", text: `PR #${pr.number} · ${humanize(pr.state)}`, href: pr.url, target: "_blank", rel: "noopener noreferrer" }));
  } else section.append(node("p", { text: t("noOutcome") }));
  const tasks = detail.tasks || [];
  if (tasks.length) {
    const timeline = node("div", { className: "timeline" });
    tasks.forEach((task) => {
      const attempts = (detail.attempts || []).filter((attempt) => attempt.task_id === task.task_id);
      const failed = attempts.filter((attempt) => attempt.status === "failed" || String(attempt.status || "").startsWith("failed_")).length;
      const tone = task.lifecycle === "completed" ? "done" : task.lifecycle === "failed" ? "failed" : "current";
      timeline.append(timelineRow(`${stepName(task)} · ${humanize(task.lifecycle)}${attempts.length ? ` · ${t("attemptsBadge", { count: attempts.length, failed })}` : ""}`, task.created_at, tone));
    });
    section.append(timeline);
  }
  if (detail.workstream_lifecycle) section.append(node("p", { className: "raw-detail", text: `${t("workflowState")}: ${humanize(detail.workstream_lifecycle)}` }));
  return section;
}

function detailView(detail, titleId) {
  const source = detail.source || {};
  const stateData = detail.operator_state || {};
  const root = node("div");
  if (state.outsideResults) root.append(node("p", {className: "reason-flash", text: t("outsideResults"), attributes: {role: "status"}}));
  root.append(node("header", { className: "detail-head" }, [
    node("div", { className: `detail-kicker ${stateData.bucket || ""}`, text: t(bucketLabels[stateData.bucket] || stateData.bucket || "workItem") }),
    node("h2", { text: source.title || t("untitledWorkItem"), attributes: { id: titleId } }),
    node("p", { text: [source.repo_full_name, source.number ? sourceLabel(source) : null, detail.updated_at ? t("updated", { time: formatRelative(detail.updated_at) }) : null].filter(Boolean).join(" · ") }),
  ]));
  root.append(node("h3", { className: "layer-label", text: t("nowLabel") }), nowSection(detail));
  root.append(node("h3", { className: "layer-label", text: t("progressLabel") }), progressSection(detail));
  root.append(node("h3", { className: "layer-label", text: t("traceLabel") }));

  const attempts = detail.attempts || [];
  const failedCount = attempts.filter((attempt) => attempt.failure_summary || attempt.status === "failed").length;
  const tasksById = new Map((detail.tasks || []).map((task) => [task.task_id, task]));
  const history = disclosure("attempts", t("agentExecution"), t("historyWhy"), attempts.length ? t("attemptsBadge", { count: attempts.length, failed: failedCount }) : "", { className: "detail-section execution-history", warn: failedCount > 0 });
  [...attempts].sort((a, b) => (b.started_at || "").localeCompare(a.started_at || "") || b.attempt_no - a.attempt_no).forEach((attempt) => {
    const run = node("details", { className: "detail-section", dataset: { disclosure: attempt.attempt_id, status: attempt.status || "" } });
    const task = tasksById.get(attempt.task_id);
    run.append(node("summary", { text: `${task ? stepName(task) : attempt.task_id} · #${attempt.attempt_no} · ${humanize(attempt.status)} · ${formatTime(attempt.started_at)}` }));
    if (attempt.failure_summary) {
      const reason = humanizeReason(attempt.failure_summary);
      run.append(node("p", { className: "reason-flash needs_attention", text: reason.text }));
      if (reason.raw !== reason.text) run.append(node("p", { className: "raw-detail", text: reason.raw }));
    }
    const timeline = node("div", { className: "timeline" });
    (detail.phases || []).filter((phase) => phase.attempt_id === attempt.attempt_id)
      .sort((a, b) => (a.created_at || "").localeCompare(b.created_at || ""))
      .forEach((phase) => timeline.append(timelineRow(`${t(phaseLabels[phase.phase] || phase.phase)}: ${phase.summary}`, phase.created_at, phase.status === "completed" || phase.status === "succeeded" ? "done" : phase.status === "failed" ? "failed" : "current")));
    run.append(timeline, artifactList((detail.artifacts || []).filter((artifact) => artifact.attempt_id === attempt.attempt_id)));
    history.append(run);
  });
  root.append(history);
  if (detail.actions?.length) {
    const failedActions = detail.actions.filter((action) => action.safe_error).length;
    const publications = disclosure("publications", t("actions"), t("publicationsWhy"), String(detail.actions.length), { warn: failedActions > 0 });
    const list = node("div", { className: "timeline" });
    detail.actions.forEach((action) => list.append(timelineRow(`${t(actionLabels[action.action_type] || action.action_type)}: ${humanize(action.publish_status)}${action.safe_error ? ` · ${humanizeReason(action.safe_error).text}` : ""}`, action.created_at, action.publish_status === "published" ? "done" : action.safe_error ? "failed" : "current")));
    publications.append(list);
    root.append(publications);
  }
  if (detail.execution_timings?.length) {
    const timingSection = disclosure("timings", t("timingTitle"), t("timingWhy"), "");
    const labels = state.language === "zh"
      ? { queue_seconds: "首次排队", worker_seconds: "Worker 执行", result_ingestion_seconds: "结果入库等待", result_to_audit_seconds: "结果至审核", result_to_publication_seconds: "结果至发布" }
      : { queue_seconds: "Initial queue", worker_seconds: "Worker execution", result_ingestion_seconds: "Result ingestion wait", result_to_audit_seconds: "Result to audit", result_to_publication_seconds: "Result to publication" };
    detail.execution_timings.forEach((timing) => {
      timingSection.append(node("p", { className: "raw-detail", text: timing.task_id }));
      Object.entries(labels).forEach(([key, label]) => timingSection.append(node("p", { text: `${label}: ${timing[key] == null ? (state.language === "zh" ? "未记录" : "Not recorded") : `${Math.round(timing[key])} s`}` })));
      const waits = state.language === "zh" ? { repository_busy: "等待仓库执行租约", human_input: "等待人工回复", publication_attention: "发布已停止，需处理", publication_backoff: "发布退避", publication_pending: "等待发布", scheduler_pending: "等待调度", worker_or_result_pending: "等待 Worker 或结果", terminal: "已结束", unknown: "等待原因未记录" } : { repository_busy: "Waiting for repository lease", human_input: "Waiting for a reply", publication_attention: "Publication needs attention", publication_backoff: "Publication backoff", publication_pending: "Waiting for publication", scheduler_pending: "Waiting for scheduling", worker_or_result_pending: "Waiting for worker or result", terminal: "Finished", unknown: "Waiting reason not recorded" };
      timingSection.append(node("p", { text: waits[timing.waiting_reason] || timing.waiting_reason }));
      if (timing.lease_expires_at) timingSection.append(node("p", { text: `${state.language === "zh" ? "租约到期" : "Lease expires"}: ${formatTime(timing.lease_expires_at)}` }));
      if (timing.next_retry_at) timingSection.append(node("p", { text: `${state.language === "zh" ? "下次发布重试" : "Next publication retry"}: ${formatTime(timing.next_retry_at)}` }));
    });
    timingSection.append(node("p", { className: "raw-detail", text: state.language === "zh" ? "各区间可能重叠；缺少记录不代表耗时为零。" : "Intervals may overlap; missing evidence does not mean zero elapsed time." }));
    root.append(timingSection);
  }
  if (detail.notification_delivery?.length) {
    const notices = disclosure("notifications", t("notificationsTitle"), t("notificationsWhy"), String(detail.notification_delivery.length));
    const labels = state.language === "zh" ? { pending: "等待发送（需配置通知渠道）", sending: "正在发送或等待确认", sent: "已送达", failed: "发送失败，等待重试", exhausted: "发送已停止，请检查通知渠道" } : { pending: "Pending (requires a configured channel)", sending: "Sending or awaiting confirmation", sent: "Delivered", failed: "Failed; waiting to retry", exhausted: "Stopped; check notification channel" };
    detail.notification_delivery.forEach((notice) => {
      const delivery = notice.delivery || {};
      notices.append(node("p", { text: `${notice.type}: ${labels[delivery.status] || delivery.status || labels.pending}${delivery.attempt_count ? ` (${delivery.attempt_count}/${delivery.max_attempts})` : ""}${delivery.sent_at ? ` · ${formatTime(delivery.sent_at)}` : ""}${delivery.next_retry_at ? ` · ${formatTime(delivery.next_retry_at)}` : ""}` }));
    });
    root.append(notices);
  }
  const otherArtifacts = (detail.artifacts || []).filter((artifact) => !artifact.attempt_id);
  if (otherArtifacts.length) {
    const box = disclosure("artifacts", t("artifacts"), t("artifactsWhy"), String(otherArtifacts.length));
    box.append(artifactList(otherArtifacts));
    root.append(box);
  }
  const tasks = detail.tasks || [];
  const technical = disclosure("technical", t("technicalTitle"), t("technicalWhy"), "");
  const copyActions = node("div", { className: "detail-actions" });
  const taskId = tasks.length ? tasks[tasks.length - 1].task_id : null;
  const copy = (label, value) => {
    const control = node("button", { className: "action-button", text: label, type: "button" });
    control.addEventListener("click", async () => {
      const text = typeof value === "function" ? value() : value;
      try { await navigator.clipboard.writeText(text); control.textContent = t("copied"); setTimeout(() => { control.textContent = label; }, 1500); } catch (_error) { control.textContent = text; }
    });
    return control;
  };
  if (taskId) {
    technical.append(node("p", { className: "raw-detail", text: taskId }));
    copyActions.append(copy(t("copyTaskId"), taskId));
  }
  copyActions.append(copy(t("copyShareLink"), () => window.location.href));
  technical.append(copyActions);
  root.append(technical);
  return root;
}

function renderWorkItemDetail(detail) {
  const sameItem = state.detail?.id === detail.id;
  for (const [panelId, titleId] of [["detail-panel", "detail-title"], ["drawer-content", "drawer-detail-title"]]) {
    const panel = $(panelId);
    const expanded = new Set(sameItem ? [...panel.querySelectorAll("details")].filter((el) => el.open).map((el) => el.dataset.disclosure) : []);
    const view = detailView(detail, titleId);
    view.querySelectorAll("details").forEach((el) => { el.open = expanded.has(el.dataset.disclosure); });
    panel.replaceChildren(view);
  }
  state.detail = detail;
  $("drawer-title").textContent = t("workItemDetail");
}

async function loadWorkItems({ append = false, reason = "manual" } = {}) {
  if (reason === "auto" && document.hidden) return;
  if (!append && state.hasLoaded && state.filters.cursor) {
    state.filters.cursor = "";
    writeUrlState();
  }
  if (!state.hasLoaded) showSkeleton();
  if (state.listController) state.listController.abort();
  const controller = new AbortController();
  state.listController = controller;
  try {
    const response = await fetch(listApiUrl(), { cache: "no-store", signal: controller.signal });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.safe_error || `HTTP ${response.status}`);
    if (controller.signal.aborted) return;
    if (append && state.list) payload.items = state.list.items.concat(payload.items || []);
    state.list = payload;
    state.hasLoaded = true;
    renderWorkItems(payload, { append: false });
    clearRefreshError();
    if (!state.selectedId && payload.items.length) await selectWorkItem(payload.items[0].id, null, { updateHistory: false, showDrawer: false });
    else if (state.selectedId && payload.items.some((item) => item.id === state.selectedId)) {
      state.outsideResults = false;
      await loadWorkItemDetail(state.selectedId);
    }
    else if (state.selectedId && state.filters.selectedId === state.selectedId) {
      state.outsideResults = true;
      await loadWorkItemDetail(state.selectedId);
    } else clearWorkItemDetail();
  } catch (error) {
    if (error.name !== "AbortError") showRefreshError(error.message);
  } finally {
    $("workbench-app").setAttribute("aria-busy", "false");
  }
}

function clearWorkItemDetail() {
  if (state.detailController) state.detailController.abort();
  state.selectedId = null;
  state.detail = null;
  state.lastFocusedRow = null;
  state.outsideResults = false;
  closeDetailDrawer();
  for (const id of ["detail-panel", "drawer-content"]) {
    $(id).replaceChildren(node("div", { className: "detail-empty" }, [node("h2", { text: t("selectWorkItem") }), node("p", { text: t("selectWorkItemHelp") })]));
  }
  writeUrlState();
}

async function loadWorkItemDetail(workstreamId) {
  if (!workstreamId) return;
  if (state.detailController) state.detailController.abort();
  const controller = new AbortController();
  state.detailController = controller;
  try {
    const response = await fetch(`/api/work-items/${encodeURIComponent(workstreamId)}`, { cache: "no-store", signal: controller.signal });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.safe_error || `HTTP ${response.status}`);
    if (state.selectedId === workstreamId && !controller.signal.aborted) renderWorkItemDetail(payload);
  } catch (error) {
    if (error.name === "AbortError") return;
    $("detail-panel").replaceChildren(node("div", { className: "detail-empty" }, [node("h2", { text: t("workItemUnavailable") }), node("p", { text: error.message })]));
  }
}

async function selectWorkItem(workstreamId, row, { updateHistory = true, showDrawer = true } = {}) {
  state.outsideResults = false;
  state.selectedId = workstreamId;
  state.lastFocusedRow = row || document.querySelector(`[data-workstream-id="${CSS.escape(workstreamId)}"]`);
  document.querySelectorAll("[data-workstream-id]").forEach((candidate) => {
    const selected = candidate.dataset.workstreamId === workstreamId;
    candidate.classList.toggle("selected", selected);
    candidate.setAttribute("aria-pressed", selected ? "true" : "false");
  });
  writeUrlState({ replace: !updateHistory });
  await loadWorkItemDetail(workstreamId);
  if (showDrawer && window.matchMedia("(max-width: 900px)").matches) openDetailDrawer();
}

function openDetailDrawer() {
  const drawer = $("detail-drawer");
  $("drawer-scrim").hidden = false;
  drawer.classList.add("open");
  drawer.setAttribute("aria-hidden", "false");
  document.body.classList.add("drawer-open");
  $("drawer-close").focus();
}

function closeDetailDrawer() {
  const drawer = $("detail-drawer");
  drawer.classList.remove("open");
  drawer.setAttribute("aria-hidden", "true");
  $("drawer-scrim").hidden = true;
  document.body.classList.remove("drawer-open");
  if (state.lastFocusedRow) state.lastFocusedRow.focus();
}

function visibleRows() { return Array.from(document.querySelectorAll("[data-workstream-id]")); }

function editableTarget(target) {
  return target && (target.matches("input, textarea, select") || target.isContentEditable);
}

function handleKeyboardShortcut(event) {
  if (event.target.closest("a")) return;
  if (editableTarget(event.target)) {
    if (event.key === "Escape") event.target.blur();
    return;
  }
  if (event.key === "/") { event.preventDefault(); $("work-search").focus(); return; }
  if (event.key === "Escape") { closeDetailDrawer(); return; }
  const rows = visibleRows();
  const index = rows.findIndex((row) => row.dataset.workstreamId === state.selectedId);
  if ((event.key === "j" || event.key === "k") && rows.length) {
    event.preventDefault();
    const delta = event.key === "j" ? 1 : -1;
    const next = rows[Math.max(0, Math.min(rows.length - 1, index + delta))] || rows[0];
    next.focus();
    selectWorkItem(next.dataset.workstreamId, next);
  } else if (event.key === "Enter" && index >= 0) {
    event.preventDefault();
    selectWorkItem(rows[index].dataset.workstreamId, rows[index]);
  } else if (event.key === "g" && index >= 0) {
    const item = state.list && state.list.items.find((candidate) => candidate.id === state.selectedId);
    if (item && item.source.url) window.open(item.source.url, "_blank", "noopener");
  }
}

function syncControls() {
  $("work-search").value = state.filters.q;
  $("bucket-filter").value = state.filters.bucket;
  $("sort-filter").value = state.filters.sort;
  for (const [tab, active] of [["work", state.filters.bucket !== "history"], ["history", state.filters.bucket === "history"]]) {
    const link = document.querySelector(`[data-nav="${tab}"]`);
    link.classList.toggle("selected", active);
    if (active) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  $("work-title").textContent = state.filters.bucket === "history" ? t("historyTitle") : t("workTitle");
  document.querySelector(".summary-counts").hidden = state.filters.bucket === "history";
  $("work-subtitle").textContent = state.filters.bucket === "history" ? t("historySubtitle") : t("workSubtitle");
}

function updateFilters() {
  state.filters.q = $("work-search").value.trim();
  state.filters.repo = $("repo-filter").value;
  state.filters.actor = $("actor-filter").value;
  state.filters.bucket = $("bucket-filter").value;
  state.filters.sort = $("sort-filter").value;
  state.filters.cursor = "";
  state.filters.selectedId = null;
  clearWorkItemDetail();
  syncControls();
  loadWorkItems();
}

function applyTheme(choice) {
  state.theme = ["system", "light", "dark"].includes(choice) ? choice : "system";
  const dark = matchMedia("(prefers-color-scheme: dark)").matches;
  document.documentElement.dataset.themeChoice = state.theme;
  document.documentElement.dataset.theme = document.documentElement.dataset.colorMode =
    state.theme === "system" ? (dark ? "dark" : "light") : state.theme;
  localStorage.setItem("robertWorkbenchTheme", state.theme);
  $("theme-select").value = state.theme;
}

function startAutoRefresh() {
  if (state.refreshTimer) clearInterval(state.refreshTimer);
  state.refreshTimer = setInterval(() => loadWorkItems({ reason: "auto" }), 30000);
}

document.addEventListener("DOMContentLoaded", async () => {
  state.filters = readUrlState();
  state.selectedId = state.filters.selectedId;
  state.language = localStorage.getItem("robertLanguage") || (/^zh/i.test(navigator.language || "") ? "zh" : "en");
  state.theme = localStorage.getItem("robertWorkbenchTheme") || "system";
  applyTranslations();
  syncControls();
  applyTheme(state.theme);
  await loadControlSession();
  if (state.session && !state.session.writes_enabled) {
    $("workbench-app").prepend(node("p", { className: "snapshot-notice", text: state.session.write_error || t("retriggerFailed"), attributes: { role: "status" } }));
  }
  let searchTimer = null;
  $("filter-form").addEventListener("submit", (event) => { event.preventDefault(); updateFilters(); });
  $("work-search").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(updateFilters, 220); });
  $("repo-filter").addEventListener("change", updateFilters);
  $("actor-filter").addEventListener("change", updateFilters);
  $("bucket-filter").addEventListener("change", updateFilters);
  $("sort-filter").addEventListener("change", updateFilters);
  $("retry-button").addEventListener("click", () => loadWorkItems());
  $("load-more").addEventListener("click", () => { state.filters.cursor = state.list.next_cursor || ""; writeUrlState(); loadWorkItems({ append: true }); });
  $("drawer-close").addEventListener("click", closeDetailDrawer);
  $("drawer-scrim").addEventListener("click", closeDetailDrawer);
  $("theme-select").addEventListener("change", (event) => applyTheme(event.target.value));
  $("language-select").addEventListener("change", (event) => setLanguage(event.target.value));
  window.addEventListener("keydown", handleKeyboardShortcut);
  window.addEventListener("popstate", () => { state.filters = readUrlState(); state.selectedId = state.filters.selectedId; syncControls(); loadWorkItems(); });
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { if (document.documentElement.dataset.themeChoice === "system") applyTheme("system"); });
  loadWorkItems();
  startAutoRefresh();
});
