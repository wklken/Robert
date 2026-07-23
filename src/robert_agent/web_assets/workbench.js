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
};

const translations = {
  en: {
    pageTitle: "Robert Workbench", workbenchControls: "Workbench controls", live: "Live",
    theme: "Theme", themeSystem: "System", themeLight: "Light", themeDark: "Dark",
    language: "Language", languageEnglish: "English", languageChinese: "Chinese",
    primaryNavigation: "Primary navigation", navWork: "Work", navBoard: "Board", navHistory: "History",
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
    pendingAudit: "pending audit", active: "active", complete: "complete", publish: "Publish",
    untitledWorkItem: "Untitled GitHub work item", openedBy: "opened by @{actor}", updated: "updated {time}",
    noMatches: "No work matches these filters", noActiveWork: "No active GitHub work needs attention",
    adjustFilters: "Adjust the filters or check History for completed work.",
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
    published: "Published", failed: "Failed", succeeded: "Succeeded", completed: "Completed",
    pending: "Pending", skipped: "Skipped", running: "Running", canceled: "Canceled",
  },
  zh: {
    pageTitle: "Robert 工作台", workbenchControls: "工作台控制", live: "实时",
    theme: "主题", themeSystem: "跟随系统", themeLight: "浅色", themeDark: "深色",
    language: "语言", languageEnglish: "English", languageChinese: "中文",
    primaryNavigation: "一级导航", navWork: "工作", navBoard: "任务看板", navHistory: "历史",
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
    pendingAudit: "等待审计", active: "活跃", complete: "已完成", publish: "发布",
    untitledWorkItem: "未命名的 GitHub 工作项", openedBy: "由 @{actor} 发起", updated: "更新于 {time}",
    noMatches: "没有符合筛选条件的工作", noActiveWork: "暂无需要处理的活跃 GitHub 工作",
    adjustFilters: "调整筛选条件，或在历史中查看已完成工作。", mostRecentlyUpdated: "按最近更新时间",
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
    published: "已发布", failed: "失败", succeeded: "成功", completed: "已完成",
    pending: "待处理", skipped: "已跳过", running: "运行中", canceled: "已取消",
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
  if (options.href) element.href = options.href;
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
  return {
    bucket: params.get("bucket") || "",
    repo: params.get("repo") || "",
    actor: params.get("actor") || "",
    q: params.get("q") || "",
    sort: params.get("sort") || "priority",
    cursor: params.get("cursor") || "",
    selectedId: hash ? decodeURIComponent(hash[1]) : null,
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
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString(state.language === "zh" ? "zh-CN" : "en");
}

function reasonLabel(item) { return t(reasonLabels[item.reason_code] || item.reason_code || item.bucket); }

function humanize(value) {
  if (!value) return t("unknown");
  const normalized = String(value).toLowerCase();
  if ((translations[state.language] || {})[normalized] || translations.en[normalized]) return t(normalized);
  const text = String(value).replaceAll("_", " ");
  return text.charAt(0).toUpperCase() + text.slice(1);
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

function stateIcon(bucket) {
  const symbol = bucket === "needs_attention" ? "!" : bucket === "waiting" ? "◷" : bucket === "history" ? "✓" : "●";
  return node("span", {
    className: `state-icon ${bucket === "needs_attention" ? "attention" : bucket}`,
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
  if (item.signals.failed_publish_actions) {
    signals.append(node("span", { className: "signal attention" }, [node("i"), node("span", { text: t("publish") })]));
  } else if (item.agent.attempt_status) {
    const live = item.agent.attempt_status === "running";
    signals.append(node("span", { className: `signal ${live ? "success" : ""}` }, [node("i"), node("span", { text: item.agent.attempt_status })]));
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
  [source.repo_full_name, source.number ? `#${source.number}` : null, source.author_login ? t("openedBy", { actor: source.author_login }) : null].filter(Boolean).forEach((value) => meta.append(node("span", { text: value })));
  if (item.agent.task_id) meta.append(node("code", { text: item.agent.task_id }));
  meta.append(node("span", { text: t("updated", { time: formatRelative(item.updated_at) }) }));
  const row = node("button", {
    className: `work-row ${item.id === state.selectedId ? "selected" : ""}`,
    type: "button",
    dataset: { workstreamId: item.id },
    attributes: { "aria-pressed": item.id === state.selectedId ? "true" : "false" },
  }, [stateIcon(item.bucket), node("span", { className: "work-copy" }, [titleLine, meta]), signalFor(item)]);
  row.addEventListener("click", () => selectWorkItem(item.id, row));
  return row;
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
        node("span", { text: bucket === "history" ? t("mostRecentlyUpdated") : t("actionabilityOrder") }),
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

function detailView(detail, titleId) {
  const source = detail.source || {};
  const stateData = detail.operator_state || {};
  const stateHeading = stateData.bucket === "needs_attention" ? t("whyAttention") : stateData.bucket === "working" ? t("currentAgentState") : stateData.bucket === "waiting" ? t("waitingReason") : t("outcome");
  const root = node("div");
  root.append(node("header", { className: "detail-head" }, [
    node("div", { className: `detail-kicker ${stateData.bucket || ""}`, text: t(bucketLabels[stateData.bucket] || stateData.bucket || "workItem") }),
    node("h2", { text: source.title || t("untitledWorkItem"), attributes: { id: titleId } }),
    node("p", { text: [source.repo_full_name, source.number ? `PR #${source.number}` : null].filter(Boolean).join(" · ") }),
  ]));
  root.append(node("section", { className: "detail-section" }, [
    node("h3", { text: stateHeading }),
    node("div", { className: `reason-flash ${stateData.bucket || ""}`, text: stateData.reason_summary || t("noOperatorAction") }),
  ]));
  const timeline = node("div", { className: "timeline" });
  const tasks = detail.tasks || [];
  if (tasks.length) timeline.append(timelineRow(t("discoveredAuthorized"), tasks[0].created_at, "done"));
  (detail.phases || []).forEach((phase) => timeline.append(timelineRow(`${t(phaseLabels[phase.phase] || phase.phase)}: ${phase.summary}`, phase.created_at, phase.status === "completed" || phase.status === "succeeded" ? "done" : phase.status === "failed" ? "failed" : "current")));
  (detail.actions || []).forEach((action) => timeline.append(timelineRow(`${t(actionLabels[action.action_type] || action.action_type)}: ${humanize(action.publish_status)}`, action.created_at, action.publish_status === "published" ? "done" : action.safe_error ? "failed" : "current")));
  if (!timeline.children.length) timeline.append(timelineRow(t("workstreamRecorded"), source.updated_at));
  root.append(node("section", { className: "detail-section" }, [node("h3", { text: t("agentExecution") }), timeline]));
  const actions = node("div", { className: "detail-actions" });
  if (source.url) actions.append(node("a", { className: "action-button primary", text: t("openGithub"), href: source.url, target: "_blank", rel: "noopener noreferrer" }));
  const taskId = tasks.length ? tasks[tasks.length - 1].task_id : null;
  if (taskId) {
    const copyTask = node("button", { className: "action-button", text: t("copyTaskId"), type: "button" });
    copyTask.addEventListener("click", () => navigator.clipboard.writeText(taskId));
    actions.append(copyTask);
  }
  const copyLink = node("button", { className: "action-button", text: t("copyShareLink"), type: "button" });
  copyLink.addEventListener("click", () => navigator.clipboard.writeText(window.location.href));
  actions.append(copyLink);
  root.append(node("section", { className: "detail-section" }, [node("h3", { text: t("actions") }), actions]));
  if ((detail.artifacts || []).length) {
    const artifacts = node("div", { className: "artifact-list" });
    detail.artifacts.forEach((artifact) => {
      const href = `/artifact.txt?task_id=${encodeURIComponent(artifact.task_id)}&artifact_type=${encodeURIComponent(artifact.artifact_type)}`;
      artifacts.append(node("a", { className: "artifact-link", href, target: "_blank", rel: "noopener noreferrer" }, [node("span", { text: artifact.artifact_type }), node("small", { text: artifact.bytes ? t("bytes", { count: artifact.bytes }) : t("preview") })]));
    });
    root.append(node("section", { className: "detail-section" }, [node("h3", { text: t("artifacts") }), artifacts]));
  }
  return root;
}

function renderWorkItemDetail(detail) {
  state.detail = detail;
  $("detail-panel").replaceChildren(detailView(detail, "detail-title"));
  $("drawer-content").replaceChildren(detailView(detail, "drawer-detail-title"));
  $("drawer-title").textContent = t("workItemDetail");
}

async function loadWorkItems({ append = false, reason = "manual" } = {}) {
  if (reason === "auto" && document.hidden) return;
  if (!state.hasLoaded) showSkeleton();
  if (state.listController) state.listController.abort();
  state.listController = new AbortController();
  try {
    const response = await fetch(listApiUrl(), { cache: "no-store", signal: state.listController.signal });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.safe_error || `HTTP ${response.status}`);
    if (append && state.list) payload.items = state.list.items.concat(payload.items || []);
    state.list = payload;
    state.hasLoaded = true;
    renderWorkItems(payload, { append: false });
    clearRefreshError();
    if (!state.selectedId && payload.items.length) await selectWorkItem(payload.items[0].id, null, { updateHistory: false, showDrawer: false });
    else if (state.selectedId && payload.items.some((item) => item.id === state.selectedId)) await loadWorkItemDetail(state.selectedId);
  } catch (error) {
    if (error.name !== "AbortError") showRefreshError(error.message);
  } finally {
    $("workbench-app").setAttribute("aria-busy", "false");
  }
}

async function loadWorkItemDetail(workstreamId) {
  if (!workstreamId) return;
  if (state.detailController) state.detailController.abort();
  state.detailController = new AbortController();
  try {
    const response = await fetch(`/api/work-items/${encodeURIComponent(workstreamId)}`, { cache: "no-store", signal: state.detailController.signal });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.safe_error || `HTTP ${response.status}`);
    renderWorkItemDetail(payload);
  } catch (error) {
    if (error.name === "AbortError") return;
    $("detail-panel").replaceChildren(node("div", { className: "detail-empty" }, [node("h2", { text: t("workItemUnavailable") }), node("p", { text: error.message })]));
  }
}

async function selectWorkItem(workstreamId, row, { updateHistory = true, showDrawer = true } = {}) {
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
  document.querySelector('[data-nav="work"]').classList.toggle("selected", state.filters.bucket !== "history");
  document.querySelector('[data-nav="history"]').classList.toggle("selected", state.filters.bucket === "history");
  $("work-title").textContent = state.filters.bucket === "history" ? t("historyTitle") : t("workTitle");
  $("work-subtitle").textContent = state.filters.bucket === "history" ? t("historySubtitle") : t("workSubtitle");
}

function updateFilters() {
  state.filters.q = $("work-search").value.trim();
  state.filters.repo = $("repo-filter").value;
  state.filters.actor = $("actor-filter").value;
  state.filters.bucket = $("bucket-filter").value;
  state.filters.sort = $("sort-filter").value;
  state.filters.cursor = "";
  state.selectedId = null;
  writeUrlState();
  syncControls();
  loadWorkItems();
}

function applyTheme(choice) {
  state.theme = ["system", "light", "dark"].includes(choice) ? choice : "system";
  const dark = matchMedia("(prefers-color-scheme: dark)").matches;
  document.documentElement.dataset.themeChoice = state.theme;
  document.documentElement.dataset.colorMode = state.theme === "system" ? (dark ? "dark" : "light") : state.theme;
  localStorage.setItem("robertWorkbenchTheme", state.theme);
  $("theme-select").value = state.theme;
}

function startAutoRefresh() {
  if (state.refreshTimer) clearInterval(state.refreshTimer);
  state.refreshTimer = setInterval(() => loadWorkItems({ reason: "auto" }), 30000);
}

document.addEventListener("DOMContentLoaded", () => {
  state.filters = readUrlState();
  state.selectedId = state.filters.selectedId;
  state.language = localStorage.getItem("robertLanguage") || "en";
  state.theme = localStorage.getItem("robertWorkbenchTheme") || "system";
  applyTranslations();
  syncControls();
  applyTheme(state.theme);
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
