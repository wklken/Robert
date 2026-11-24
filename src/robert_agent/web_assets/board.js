"use strict";

const translations = {
  en: {
    agentSession: "Agent session", agentSessionWhy: "Messages and actions, newest first",
    sessionExecution: "Execution", sessionOpenHint: "Open to load the conversation.",
    sessionAgent: "Agent", sessionMain: "Task implementation / main session",
    sessionSucceeded: "Succeeded",
    sessionNoExecutions: "No executions yet.", sessionLoading: "Loading conversation…",
    sessionOlder: "Load earlier records", sessionNewestFirst: "Newest first",
    sessionLive: "Updates while running", sessionExpand: "Show full message",
    sessionOutput: "Show output / changes", sessionTruncated: "Large content shows a bounded excerpt.",
    sessionLimited: "Large records were omitted. Load earlier records to continue.",
    sessionUnavailable: "This machine has no readable conversation for this execution.",
    sessionEmpty: "Waiting for the Agent to record its first message.",
    session_assistant: "Agent", session_user: "Task input", session_reasoning: "Reasoning summary",
    session_command: "Command", session_file_change: "File changes", session_web_search: "Web search",
    session_tool: "Tool call", session_tool_output: "Tool result",
    supervision: "Task supervision", noIncidents: "No recorded supervision incidents.",
    supervisionWhy: "What the background supervisor noticed and how it recovered",
    incidentsCount: "{count} incidents",
    pageTitle: "Robert Task Board",
    productSubtitle: "Your Repo Teammate",
    primaryNavigation: "Primary navigation",
    navWorkbench: "Workbench",
    navWork: "Work",
    navBoard: "Board",
    navHistory: "History",
    navTimeline: "Timeline",
    navProposals: "Proposals",
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
    boardTitle: "Task board",
    boardHint: "Each card says where the task is, whether it needs you, and what to do next.",
    boardFilters: "Board filters",
    boardScope: "Counts follow the task lifecycle; the Work queue counts GitHub workflows.",
    find: "Find",
    searchPlaceholder: "Title, task, branch or PR",
    repository: "Repository",
    allRepositories: "All repositories",
    agent: "Agent",
    allAgents: "All agents",
    priority: "Priority",
    allPriorities: "All priorities",
    view: "View",
    activeTasks: "Active tasks",
    canceledTasks: "Canceled tasks",
    needsAttention: "Only what needs me",
    filtersToggle: "Filters",
    refresh: "Refresh",
    refreshBoard: "Refresh board",
    taskStatus: "Task status",
    repositoryTasks: "Repository tasks",
    columnBacklog: "Backlog",
    columnTodo: "Queued",
    columnDoing: "Executing",
    columnWaiting: "Needs attention",
    columnReview: "Review",
    columnDone: "Done",
    purposeBacklog: "Not started yet; still editable",
    purposeTodo: "Waiting for Robert to dispatch",
    purposeDoing: "An Agent is working or publishing",
    purposeWaiting: "Failures, questions, and decisions",
    purposeReview: "PR created, waiting for review",
    purposeDone: "Nothing left to do",
    historyHint: "Stopped tasks and their history remain available.",
    noTasksStage: "No tasks in this stage.",
    unloadedStage: "Tasks in this stage are on another page. Load more to view them.",
    loadMore: "Load more tasks",
    showing: "Showing {count} of {total} tasks",
    agentsRunning: "{count} agent(s) running",
    agentsRunningEmpty: "0 agents running",
    inboxCount: "{count} task(s) need you",
    inboxCalm: "Nothing needs you right now",
    showOnlyMine: "Show only these",
    noMatchingTasks: "No tasks match these filters",
    clearFilters: "Clear filters",
    activeConditions: "Active filters: {conditions}",
    filteredSummary: "Filtered: {conditions}",
    conditionSearch: "search “{value}”",
    conditionAttention: "only what needs me",
    conditionCanceled: "canceled tasks",
    taskDetail: "Task detail",
    close: "Close",
    closeTaskDetail: "Close task detail",
    openTask: "Open {title}",
    loading: "Loading…",
    syncing: "Updating…",
    synced: "Updated {time}",
    retryRefresh: "Refresh and retry",
    boardRefreshed: "Board refreshed.",
    boardRefreshFailed: "Board refresh failed: {message}",
    readOnly: "Read-only mode: {reason}",
    writesDisabled: "Writes are disabled.",
    requestFailed: "Request failed ({status})",

    layerNow: "What is happening",
    layerProgress: "How far it got",
    layerHistory: "Look here only when tracing back",
    factWhat: "What happened",
    factCause: "Why",
    factQuestion: "Question",
    factImpact: "Impact",
    factOwner: "Who acts",
    factProblem: "Problems",
    factLatest: "Latest progress",
    noProblem: "None",
    causeUnknown: "Cause not identified yet. Open “Execution history” below to read the error log.",
    causeFromAttempt: "From the error of attempt {number}",
    causeFromReview: "From the review record",
    causeFromAttention: "From the Agent's message",
    causeFromPublication: "From the publication record",
    causeFromWorkflow: "From execution control",
    causeFromCleanup: "From the worker cleanup record",
    rawText: "Original text",
    updatedAgo: "Updated {time}",

    headline_failed: "{stage} failed and needs you",
    headline_publish_failed: "The result is ready but publishing failed",
    headline_worker_unavailable: "The selected Agent is unavailable",
    headline_environment: "The runtime needs an administrator",
    headline_source_closed: "The source issue or PR is closed",
    headline_pr_closed: "The PR was closed without merging",
    headline_needs_reply: "The Agent is waiting for your answer",
    headline_needs_acceptance: "The result is ready for your acceptance",
    headline_needs_decision: "Approve to let it continue",
    headline_stuck: "Stuck: nothing can continue",
    headline_needs_attention: "This task needs you",
    headline_review_attention: "The PR review needs attention",
    headline_running: "The Agent is working on {phase}; nothing to do",
    headline_publishing: "Publishing the result",
    headline_queued: "Queued; it starts automatically when a slot is free",
    headline_in_review: "{pr} is open and waiting for review",
    headline_draft: "Saved, not started",
    headline_completed: "Done; nothing left to do",
    headline_canceled: "Canceled",

    what_failed: "{stage} did not finish: {failed} of {total} attempt(s) failed.",
    what_publish_failed: "The result passed audit, but publishing it to GitHub failed.",
    what_worker_unavailable: "The requested Agent is unavailable, so the task did not start.",
    what_environment: "A previous worker was not cleaned up, so new runs are blocked.",
    what_source_closed: "The source issue or PR was closed.",
    what_source_closed_running: "The source was closed, but a worker is still running.",
    what_pr_closed: "The PR was closed without being merged.",
    what_needs_reply: "The Agent paused and asked you a question.",
    what_needs_acceptance: "The Agent considers the task done and asks you to confirm.",
    what_needs_decision: "The execution route needs your approval before it continues.",
    what_stuck: "No task remains that can continue this item.",
    what_needs_attention: "The task needs a person to look at it.",
    what_review_attention: "The PR review raised something to handle.",
    what_running: "The Agent is working on {phase} (attempt {attempt}).",
    what_publishing: "The result is ready and being published to GitHub.",
    what_queued: "The task is in the queue, waiting for Robert to assign an Agent.",
    what_rerun_requested: "A new run of {stage} was requested; it starts when the daemon picks it up.",
    what_in_review: "{pr} was created and is waiting for review.",
    what_draft: "The requirement is saved; nothing has run yet.",
    what_completed: "The task is complete.",
    what_canceled: "The task was canceled {time}.",

    impact_failed_published: "{count} item(s) already published (comments, PRs) stay as they are; this step produced nothing new.",
    impact_failed_none: "This step produced nothing and published nothing.",
    impact_publish_failed: "The result is kept; publishing again does not rerun the task.",
    impact_worker_unavailable: "Nothing changed; the task stopped before starting.",
    impact_environment: "No new run can start in this repository until cleanup finishes.",
    impact_source_closed: "Review outstanding delivery or cleanup before finishing this item.",
    impact_pr_closed: "The changes in the PR will not reach the target branch.",
    impact_paused: "The task stays paused until you respond.",
    impact_stuck: "The task will not continue by itself.",

    owner_operator: "You (an operator with write access)",
    owner_initiator: "The requester ({identity})",
    owner_initiator_you: "You (the requester)",
    owner_admin: "The runtime administrator: check the worker process and worktree",
    owner_github: "Reviewers on GitHub. You only act if you want another round.",
    owner_robert: "Robert, automatically. Nothing to do.",
    owner_none: "Nobody; nothing left to do",
    ownerShort_operator: "Needs you",
    ownerShort_initiator: "Needs your answer",
    ownerShort_admin: "Needs an admin",
    ownerShort_github: "Waiting for reviewers",
    ownerShort_robert: "Robert is on it",
    ownerShort_none: "Nothing to do",
    ownerShort_draft: "Start when ready",

    chip_failed: "Failed · needs you",
    chip_publish_failed: "Publish failed",
    chip_worker_unavailable: "Agent unavailable",
    chip_environment: "Runtime problem",
    chip_source_closed: "Source closed",
    chip_pr_closed: "PR closed unmerged",
    chip_needs_reply: "Reply needed",
    chip_needs_acceptance: "Accept result",
    chip_needs_decision: "Approval needed",
    chip_stuck: "Stuck",
    chip_needs_attention: "Needs attention",
    chip_review_attention: "Review issue",
    chip_running: "Executing",
    chip_publishing: "Publishing",
    chip_queued: "Queued",
    chip_in_review: "In review",
    chip_draft: "Not started",
    chip_completed: "Done",
    chip_canceled: "Canceled",

    nextStep: "Next step",
    noAction_robert: "Nothing to do. Robert continues automatically; this page updates by itself.",
    noAction_github: "Nothing to do here. Reviewers continue on GitHub.",
    noAction_none: "Nothing to do.",
    noAction: "No action is available in this stage.",
    afterClick: "If you continue:",
    action_rerun: "Run {stage} again",
    action_reopen_run: "Reopen and run again",
    action_open_pr: "Open {pr} ↗",
    keepsPublished: "Published comments and PRs stay; nothing is withdrawn",
    will_retrigger_1: "Robert re-checks the GitHub source and your authorization",
    will_retrigger_2: "A new run of this step is queued",
    will_retry_1: "A new run of this step is queued",
    will_retry_2: "You can pick a different Agent",
    will_reopen_1: "The task returns to the execution flow",
    will_reopen_2: "A new round is created from its current route and queued",
    will_retry_publish: "The audited result is published again",
    keeps_retry_publish: "The task does not run again; successful publications are not repeated",
    will_reply: "The Agent continues the current task with your answer",
    will_approve: "The task continues with the current plan",
    will_start: "The task is queued and starts when an Agent is free",
    will_request_changes: "Robert makes another round of changes based on your notes",
    will_edit: "Only the requirement text changes; nothing runs",
    will_cancel: "Unfinished runs stop and unpublished results are skipped",
    keeps_cancel: "Content already published to GitHub stays",
    result_retrigger: "Submitted. A new run is queued; progress will show here.",
    result_retry: "Submitted. A new run is queued; progress will show here.",
    result_reopen: "Reopened. A new round is queued; progress will show here.",
    result_retry_publish: "Publication retry queued. The result will update here.",
    result_reply: "Reply sent. The Agent continues with it.",
    result_request_changes: "Changes requested. Robert starts another round.",
    result_approve: "Approved. The task continues.",
    result_start: "Started. The task is queued.",
    result_cancel: "Canceled.",
    result_edit: "Requirement saved.",
    openSourceGithub: "Open on GitHub ↗",
    openSourceTapd: "Open in TAPD ↗",
    openSource: "Open source ↗",
    tapdItem: "TAPD item",
    localTask: "Local task",
    sourceLink: "Source",

    lifecycle: "Progress",
    taskReceived: "Task received",
    attemptSummary: "{count} attempts · {failed} failed",
    currentStep: "Current",
    executionHistory: "Execution history",
    executionHistoryWhy: "Every attempt with its error, logs and reported steps",
    requirementWhy: "What the task was asked to do",
    timelineWhy: "Every recorded change in time order, for reconciliation",
    technicalWhy: "IDs to copy when reporting a problem",
    eventsCount: "{count} event(s)",
    moreAttempts: "Load earlier attempts ({count} remaining)",
    artifacts: "Logs and artifacts",
    missingArtifact: "File absent on this host; the database contains only its reference.",
    reviewModels: "Review models started: {in_use} / {limit}",
    reviewModelsUnknown: "Review models: not reported",
    reviewFinalCommit: "Final commit",
    requirement: "Requirement",
    noDescription: "No requirement description.",
    githubDescription: "This task comes from GitHub; the issue or PR body is the requirement.",
    timeline: "Timeline",
    noEvents: "No events recorded.",
    loadEvents: "Earlier activity",
    technical: "Technical details",
    task: "Task",
    branch: "Branch",
    routing: "Routing",
    version: "Version",
    copy: "Copy",
    copied: "Copied",
    attempt: "Attempt {number}",
    heartbeat: "Last heartbeat",
    started: "Started",
    finished: "Finished",
    noHeartbeat: "No heartbeat received yet",
    noProgress: "Waiting for the worker to report its first step.",

    localRequest: "Local request",
    newRepositoryTask: "New repository task",
    closeNewTask: "Close new task",
    title: "Title",
    titleExample: "e.g. Fix pagination when the cursor is empty",
    requirementExample: "What should change, how to verify it, and anything Robert must not touch.",
    requirementHint: "Describe the expected result and how to check it. Robert asks when something is unclear.",
    repoHint: "Robert works in a fresh worktree of this repository.",
    routingHint: "Auto lets Robert pick the Agent that fits the requirement.",
    workerHint: "Only Agents allowed for this board are listed.",
    auto: "Auto",
    specificAgent: "Specific agent",
    requiredHelp: "Repository, title and requirement are required.",
    requiredField: "Required: {field}",
    requiredMark: "(required)",
    cancel: "Cancel",
    saveToBacklog: "Save to Backlog",
    saveToBacklogHint: "Saved to Backlog; nothing runs until you start it.",
    createAndQueue: "Create and queue",
    createAndQueueHint: "Queued now; an Agent picks it up when a slot is free.",
    taskCreated: "Task created.",
    commandCompleted: "{action} completed.",
    taskChanged: "The task changed elsewhere. Detail was refreshed; retry your action.",

    editRequirement: "Edit requirement",
    approve: "Approve and continue",
    reply: "Answer the question",
    recommendedChoice: "Recommended",
    otherChoice: "None of these · write my own answer",
    requestChanges: "Request changes",
    retry: "Run again",
    retrigger: "Run again",
    retry_publish: "Publish again (no rerun)",
    reopen: "Reopen",
    moveToTodo: "Start",
    cancelTask: "Cancel task",
    stopTask: "Stop and cancel",
    yourReply: "Your answer",
    requestedChanges: "What should change",
    replyHelp: "Answer the question above. The Agent continues with your words.",
    changesHelp: "Describe what to change. Robert starts another round and updates the PR.",
    cancelHelp: "Stop this task and skip unpublished results? Existing GitHub changes are kept. A worker that cannot be stopped remains active.",
    confirmCancel: "Confirm cancellation",
    abandonReviewHelp: "I inspected and stopped all residual model processes for the unknown launches listed below. Abandon those launches and release their model slots.",
    save: "Save changes",
    confirmAction: "Confirm",
    reviewDraft: "I reviewed the latest state; keep editing",
    draftConflict: "This task changed. Your input is preserved. Review the latest content before continuing:",
    actionUnavailable: "This action is no longer available. Your input is preserved; close the editor to choose a current action.",
    draftKept: "Your draft is kept. Review the latest state before submitting again.",
    publicationPending: "The existing result is awaiting publication. No new worker will be started.",
    publicationRetry: "Publication retry queued. Waiting for the daemon.",

    classification_result: "Classify requirement",
    new_pr: "Implement and prepare PR",
    pr_update: "Update PR",
    pr_review_comment: "PR review",
    review_comment: "Review comment",
    multi_agent_review_report: "Multi-model review",
    multi_agent_vote_report: "Multi-Agent vote",
    review: "Review",
    vote: "Vote",
    voting: "Voting",
    synthesizing: "Synthesizing",
    publication_open_pr: "Create PR",
    publication_push_existing_pr: "Push code",
    publication_comment: "Publish comment",
    publication_review: "Publish review",
    publication_issue_comment: "Publish comment",
    publication_pr_comment: "Publish PR comment",
    published: "Published",
    not_published: "Awaiting publication",
    publish_failed: "Publication failed",
    skipped: "Skipped",
    auditAccepted: "Audit accepted",
    pr_opened: "PR created",
    pr_merged: "PR merge recorded",
    routing_selected: "Execution route selected",
    backfilled: "Imported historical task",
    zombie: "Worker exited but its process has not been reaped",
    prepare: "preparation",
    analyze: "analysis",
    plan: "planning",
    execute: "execution",
    verify: "verification",
    publish: "publishing",
    handoff: "handoff",
    preparing: "Preparing",
    pending: "Pending",
    running: "Running",
    completed: "Completed",
    failed: "Failed",
    canceled: "Canceled",
    ignored: "Ignored",
    queued: "Queued",
    detected: "Queued",
    authorized: "Queued",
    classified: "Queued",
    waiting_for_user: "Waiting for you",
    stale: "Interrupted",
    prepared: "Prepared",
    failed_timeout: "Worker timed out",
    failed_worker_process_exited: "Worker process exited unexpectedly",
    failed_worker_startup: "Worker failed to start",
    needs_resume: "Attempt interrupted; recovery was required",
    artifact_worker_stderr: "Error log",
    artifact_worker_stdout: "Run output",
    artifact_prompt: "Instructions to the Agent",
    artifact_github_context_json: "GitHub context (JSON)",
    artifact_github_context_md: "GitHub context",
    artifact_recovery_context: "Recovery context",
    artifact_result: "Result",
    event_created: "Task created",
    event_user_response: "You replied",
    event_changes_requested: "Changes requested",
    event_retry_requested: "Run requested again",
    event_publication_retry_requested: "Publication retry requested",
    event_reopened: "Reopened",
    event_canceled: "Canceled",
    event_completed: "Completed",
    event_operator_question: "The Agent asked a question",
    event_execution_failed: "Execution failed",
    event_publication_failed: "Publication failed",
    event_operator_decision_required: "Approval requested",
    event_completion_acceptance_requested: "Acceptance requested",
    event_pr_opened: "PR created",
    event_pr_merged: "PR merge recorded",
    reason_snapshot_changed: "The PR changed while Robert was reading it (probably a new push); it needs to be read again.",
    reason_zombie: "The worker process exited unexpectedly.",
    reason_timeout: "The run timed out.",
    reason_model_unavailable: "The model needed for this step is unavailable.",
    reason_worktree: "The run workspace is unavailable (an unmanaged worktree is in the way).",
    reason_rate_limit: "GitHub rate-limited the request.",
    reason_permission: "Robert lacks permission for this action.",
    reason_auto_stopped: "Automatic execution stopped.",
  },
  zh: {
    agentSession: "Agent 会话", agentSessionWhy: "消息与操作记录，从新到旧",
    sessionExecution: "选择执行", sessionOpenHint: "展开后加载会话记录。",
    sessionAgent: "选择 Agent", sessionMain: "任务实现 / 主会话",
    sessionSucceeded: "已完成",
    sessionNoExecutions: "还没有执行记录。", sessionLoading: "正在加载会话…",
    sessionOlder: "加载更早的记录", sessionNewestFirst: "从新到旧",
    sessionLive: "执行中自动更新", sessionExpand: "展开完整消息",
    sessionOutput: "展开输出 / 变更", sessionTruncated: "内容较大，仅展示有长度限制的片段。",
    sessionLimited: "部分超大记录已省略，可加载更早的记录。",
    sessionUnavailable: "当前机器没有本次执行的可读会话记录。",
    sessionEmpty: "等待 Agent 写入第一条会话记录。",
    session_assistant: "Agent", session_user: "任务输入", session_reasoning: "思考摘要",
    session_command: "执行命令", session_file_change: "文件变更", session_web_search: "网页搜索",
    session_tool: "工具调用", session_tool_output: "工具返回",
    supervision: "任务监督", noIncidents: "没有已记录的监督异常。",
    supervisionWhy: "后台监督发现的异常和自动恢复记录",
    incidentsCount: "{count} 条异常",
    pageTitle: "Robert 任务看板",
    productSubtitle: "Your Repo Teammate",
    primaryNavigation: "一级导航",
    navWorkbench: "工作台",
    navWork: "工作",
    navBoard: "任务看板",
    navHistory: "历史",
    navTimeline: "时间线",
    navProposals: "提案",
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
    boardTitle: "任务看板",
    boardHint: "每张卡片都写明：做到哪一步、要不要你处理、下一步做什么。",
    boardFilters: "看板筛选",
    boardScope: "看板按任务生命周期统计；工作队列按 GitHub 工作流统计。",
    find: "搜索",
    searchPlaceholder: "标题、任务、分支或 PR",
    repository: "仓库",
    allRepositories: "全部仓库",
    agent: "Agent",
    allAgents: "全部 Agent",
    priority: "优先级",
    allPriorities: "全部优先级",
    view: "视图",
    activeTasks: "进行中的任务",
    canceledTasks: "已取消的任务",
    needsAttention: "只看需要我处理",
    filtersToggle: "筛选",
    refresh: "刷新",
    refreshBoard: "刷新看板",
    taskStatus: "任务状态",
    repositoryTasks: "仓库任务",
    columnBacklog: "待整理",
    columnTodo: "已排队",
    columnDoing: "执行中",
    columnWaiting: "需要处理",
    columnReview: "待评审",
    columnDone: "已完成",
    purposeBacklog: "还没开始，可以编辑",
    purposeTodo: "等 Robert 调度",
    purposeDoing: "Agent 正在工作或发布",
    purposeWaiting: "失败、提问或要你决定",
    purposeReview: "PR 已创建，等评审",
    purposeDone: "无需再处理",
    historyHint: "保留已停止任务及其处理记录。",
    noTasksStage: "当前阶段没有任务。",
    unloadedStage: "此阶段的任务还未加载，点击加载更多后查看。",
    loadMore: "加载更多任务",
    showing: "已显示 {count} / {total} 个任务",
    agentsRunning: "{count} 个 Agent 运行中",
    agentsRunningEmpty: "0 个 Agent 运行中",
    inboxCount: "{count} 件事等你处理",
    inboxCalm: "现在没有需要你处理的任务",
    showOnlyMine: "只看这些",
    noMatchingTasks: "没有符合条件的任务",
    clearFilters: "清除筛选",
    activeConditions: "当前条件：{conditions}",
    filteredSummary: "已筛选：{conditions}",
    conditionSearch: "搜索「{value}」",
    conditionAttention: "只看需要我处理",
    conditionCanceled: "已取消的任务",
    taskDetail: "任务详情",
    close: "关闭",
    closeTaskDetail: "关闭任务详情",
    openTask: "打开 {title}",
    loading: "加载中…",
    syncing: "更新中…",
    synced: "更新于 {time}",
    retryRefresh: "刷新后重试",
    boardRefreshed: "看板已刷新。",
    boardRefreshFailed: "看板刷新失败：{message}",
    readOnly: "只读模式：{reason}",
    writesDisabled: "当前禁止写入。",
    requestFailed: "请求失败（{status}）",

    layerNow: "现在怎么样",
    layerProgress: "走到哪一步了",
    layerHistory: "需要追溯时再看",
    factWhat: "发生了什么",
    factCause: "原因",
    factQuestion: "问题",
    factImpact: "影响",
    factOwner: "谁处理",
    factProblem: "异常",
    factLatest: "最新进展",
    noProblem: "无",
    causeUnknown: "原因待定位。展开下方「执行历史」查看错误日志。",
    causeFromAttempt: "来自第 {number} 次执行的错误信息",
    causeFromReview: "来自评审记录",
    causeFromAttention: "来自 Agent 的消息",
    causeFromPublication: "来自发布记录",
    causeFromWorkflow: "来自执行控制",
    causeFromCleanup: "来自 Worker 清理记录",
    rawText: "原文",
    updatedAgo: "更新于 {time}",

    headline_failed: "{stage}失败，需要你处理",
    headline_publish_failed: "结果已生成，但发布失败",
    headline_worker_unavailable: "指定的 Agent 不可用",
    headline_environment: "运行环境异常，需要运行管理员处理",
    headline_source_closed: "源 Issue / PR 已关闭",
    headline_pr_closed: "PR 已关闭但没有合并",
    headline_needs_reply: "Agent 在等你回答一个问题",
    headline_needs_acceptance: "结果已完成，等你验收",
    headline_needs_decision: "需要你批准后才能继续",
    headline_stuck: "卡住了：没有可以继续的执行",
    headline_needs_attention: "这个任务需要你处理",
    headline_review_attention: "PR 评审需要处理",
    headline_running: "Agent 正在{phase}，你不用操作",
    headline_publishing: "正在发布结果",
    headline_queued: "已排队，有空闲 Agent 时自动开始",
    headline_in_review: "{pr} 已创建，等待评审",
    headline_draft: "已保存，还没开始",
    headline_completed: "已完成，无需处理",
    headline_canceled: "已取消",

    what_failed: "{stage}没有完成：共执行 {total} 次，{failed} 次失败。",
    what_publish_failed: "结果已通过审核，但发布到 GitHub 时失败了。",
    what_worker_unavailable: "指定的 Agent 当前不可用，任务没有开始。",
    what_environment: "上一次执行的 Worker 没有被正确清理，新的执行被挡住了。",
    what_source_closed: "源 Issue / PR 已经关闭。",
    what_source_closed_running: "源 Issue / PR 已关闭，但 Worker 还在运行。",
    what_pr_closed: "PR 被关闭了，没有合并。",
    what_needs_reply: "Agent 暂停了执行，向你提了一个问题。",
    what_needs_acceptance: "Agent 认为任务已经完成，请你确认结果。",
    what_needs_decision: "执行路线需要你批准后才能继续。",
    what_stuck: "已经没有可以让它继续的任务。",
    what_needs_attention: "任务需要人工处理。",
    what_review_attention: "PR 评审中出现了需要处理的问题。",
    what_running: "Agent 正在{phase}（第 {attempt} 次执行）。",
    what_publishing: "结果已生成，正在发布到 GitHub。",
    what_queued: "任务已进入队列，等 Robert 分配 Agent。",
    what_rerun_requested: "已请求重新运行{stage}，后台开始执行后这里会更新。",
    what_in_review: "{pr} 已创建，正在等评审。",
    what_draft: "需求已保存，还没有开始执行。",
    what_completed: "任务已完成。",
    what_canceled: "任务已于 {time}取消。",

    impact_failed_published: "已发布的 {count} 项内容（评论、PR 等）保持不变；这一步没有产生新结果。",
    impact_failed_none: "这一步没有产生结果，也没有发布任何内容。",
    impact_publish_failed: "结果已保存；重新发布不会重新执行任务。",
    impact_worker_unavailable: "任务停在开始之前，没有产生任何改动。",
    impact_environment: "清理完成前，这个仓库不能开始新的执行。",
    impact_source_closed: "请先检查剩余交付或清理事项，再决定是否取消。",
    impact_pr_closed: "PR 里的改动不会进入目标分支。",
    impact_paused: "你回复之前，任务保持暂停。",
    impact_stuck: "任务不会自己继续。",

    owner_operator: "你（有写权限的操作者）",
    owner_initiator: "任务发起人（{identity}）",
    owner_initiator_you: "你（任务发起人）",
    owner_admin: "运行管理员：检查 Worker 进程和工作区",
    owner_github: "GitHub 上的评审人。你不用操作，除非想让 Robert 再改一轮。",
    owner_robert: "Robert 自动处理，你不用操作。",
    owner_none: "无需处理",
    ownerShort_operator: "等你处理",
    ownerShort_initiator: "等你回复",
    ownerShort_admin: "等管理员",
    ownerShort_github: "等评审人",
    ownerShort_robert: "Robert 处理中",
    ownerShort_none: "无需处理",
    ownerShort_draft: "随时可以开始",

    chip_failed: "失败 · 待处理",
    chip_publish_failed: "发布失败",
    chip_worker_unavailable: "Agent 不可用",
    chip_environment: "运行环境异常",
    chip_source_closed: "来源已关闭",
    chip_pr_closed: "PR 未合并关闭",
    chip_needs_reply: "待你回复",
    chip_needs_acceptance: "待你验收",
    chip_needs_decision: "待你批准",
    chip_stuck: "已卡住",
    chip_needs_attention: "需要处理",
    chip_review_attention: "评审待处理",
    chip_running: "执行中",
    chip_publishing: "发布中",
    chip_queued: "排队中",
    chip_in_review: "等待评审",
    chip_draft: "未开始",
    chip_completed: "已完成",
    chip_canceled: "已取消",

    nextStep: "下一步",
    noAction_robert: "不需要操作。Robert 会自动继续，这里会自己更新。",
    noAction_github: "这里不需要操作，评审在 GitHub 上进行。",
    noAction_none: "不需要操作。",
    noAction: "当前阶段没有可执行的操作。",
    afterClick: "继续之后：",
    action_rerun: "重新运行{stage}",
    action_reopen_run: "重新打开并运行",
    action_open_pr: "查看 {pr} ↗",
    keepsPublished: "已发布的评论和 PR 保留，不会被撤回",
    will_retrigger_1: "重新校验 GitHub 来源和你的授权",
    will_retrigger_2: "为这一步新建一次执行并排队",
    will_retry_1: "为这一步新建一次执行并排队",
    will_retry_2: "可以换一个 Agent",
    will_reopen_1: "任务回到执行流程",
    will_reopen_2: "按当前路由新建一轮任务并排队",
    will_retry_publish: "复用已审核的结果重新发布",
    keeps_retry_publish: "不会重新执行任务，已发布成功的内容不会重复发布",
    will_reply: "Agent 带着你的回答继续当前任务",
    will_approve: "按当前计划继续执行",
    will_start: "进入队列，有空闲 Agent 时开始执行",
    will_request_changes: "Robert 按你的意见再改一轮",
    will_edit: "只修改需求内容，不会开始执行",
    will_cancel: "停止未完成的执行，跳过还没发布的结果",
    keeps_cancel: "已经发布到 GitHub 的内容保留",
    result_retrigger: "已提交：新的执行已排队，开始后这里会显示进度。",
    result_retry: "已提交：新的执行已排队，开始后这里会显示进度。",
    result_reopen: "已重新打开：新一轮任务已排队，这里会显示进度。",
    result_retry_publish: "已请求重新发布，结果会更新在这里。",
    result_reply: "回复已发送，Agent 会接着处理。",
    result_request_changes: "已提交修改意见，Robert 会再改一轮。",
    result_approve: "已批准，任务继续执行。",
    result_start: "已开始，任务进入队列。",
    result_cancel: "已取消。",
    result_edit: "需求已保存。",
    openSourceGithub: "在 GitHub 打开 ↗",
    openSourceTapd: "在 TAPD 打开 ↗",
    openSource: "打开来源 ↗",
    tapdItem: "TAPD 需求",
    localTask: "本地任务",
    sourceLink: "来源",

    lifecycle: "进度",
    taskReceived: "任务进入系统",
    attemptSummary: "{count} 次执行 · {failed} 次失败",
    currentStep: "当前",
    executionHistory: "执行历史",
    executionHistoryWhy: "每一次执行的错误、日志和上报步骤",
    requirementWhy: "任务最初要做什么",
    timelineWhy: "按时间排列的全部变化，用来对账",
    technicalWhy: "反馈问题时复制给开发者的编号",
    eventsCount: "{count} 条",
    moreAttempts: "加载更早的执行记录（还剩 {count} 次）",
    artifacts: "日志和产物",
    missingArtifact: "当前机器没有此文件，数据库仅保存了文件引用。",
    reviewModels: "评审模型启动：{in_use} / {limit}",
    reviewModelsUnknown: "评审模型：未上报",
    reviewFinalCommit: "最终提交",
    requirement: "需求描述",
    noDescription: "暂无需求描述。",
    githubDescription: "任务来自 GitHub，需求以 Issue / PR 正文为准。",
    timeline: "事件时间线",
    noEvents: "暂无事件记录。",
    loadEvents: "更早的记录",
    technical: "技术信息",
    task: "任务",
    branch: "分支",
    routing: "路由",
    version: "版本",
    copy: "复制",
    copied: "已复制",
    attempt: "第 {number} 次执行",
    heartbeat: "最近心跳",
    started: "开始时间",
    finished: "结束时间",
    noHeartbeat: "尚未收到心跳",
    noProgress: "等待 Worker 上报第一个执行阶段。",

    localRequest: "本地需求",
    newRepositoryTask: "新建仓库任务",
    closeNewTask: "关闭新建任务",
    title: "标题",
    titleExample: "例如：游标为空时分页报错",
    requirementExample: "要改成什么样、怎么验证，以及不能碰的地方。",
    requirementHint: "写清预期结果和验证方式。有不明确的地方，Robert 会来问你。",
    repoHint: "Robert 会在这个仓库的独立工作区里修改代码。",
    routingHint: "自动：由 Robert 按需求挑选合适的 Agent。",
    workerHint: "只列出这个看板允许使用的 Agent。",
    auto: "自动",
    specificAgent: "指定 Agent",
    requiredHelp: "仓库、标题和需求描述为必填项。",
    requiredField: "请填写{field}",
    requiredMark: "（必填）",
    cancel: "取消",
    saveToBacklog: "保存到待整理",
    saveToBacklogHint: "保存到「待整理」，你开始之前不会运行。",
    createAndQueue: "创建并排队",
    createAndQueueHint: "立即进入队列，有空闲 Agent 时开始执行。",
    taskCreated: "任务已创建。",
    commandCompleted: "已完成：{action}。",
    taskChanged: "任务已在其他位置发生变化。详情已刷新，请重试操作。",

    editRequirement: "编辑需求",
    approve: "批准并继续",
    recommendedChoice: "推荐",
    otherChoice: "以上都不选，自己填写",
    reply: "回答问题",
    requestChanges: "要求修改",
    retry: "重新运行",
    retrigger: "重新运行",
    retry_publish: "重新发布（不重跑）",
    reopen: "重新打开",
    moveToTodo: "开始",
    cancelTask: "取消任务",
    stopTask: "停止并取消",
    yourReply: "你的回答",
    requestedChanges: "希望怎么改",
    replyHelp: "回答上面的问题，Agent 会带着你的话继续。",
    changesHelp: "写下要改的地方。Robert 会再改一轮并更新 PR。",
    cancelHelp: "停止此任务并跳过尚未发布的结果？已产生的 GitHub 变更会保留。无法停止的 Worker 会继续显示为运行中。",
    confirmCancel: "确认取消任务",
    abandonReviewHelp: "我已检查并停止下列未知启动记录对应的所有残留模型进程。放弃这些记录并释放模型并发额度。",
    save: "保存修改",
    confirmAction: "确认",
    reviewDraft: "已核对最新内容，继续编辑",
    draftConflict: "任务已更新，输入已保留。请先核对最新内容：",
    actionUnavailable: "当前状态已不支持此操作，输入已保留；关闭编辑区后选择新的操作。",
    draftKept: "输入已保留，请核对最新状态后再次提交。",
    publicationPending: "已有结果等待发布，无需重新运行 Worker。",
    publicationRetry: "已请求重试发布，等待后台执行。",

    classification_result: "需求分析与分流",
    new_pr: "实现需求并准备 PR",
    pr_update: "更新 PR",
    pr_review_comment: "PR 评审",
    review_comment: "评审评论",
    multi_agent_review_report: "多模型评审",
    multi_agent_vote_report: "多 Agent 投票",
    review: "评审",
    vote: "投票",
    voting: "投票中",
    synthesizing: "汇总中",
    publication_open_pr: "创建 PR",
    publication_push_existing_pr: "推送代码",
    publication_comment: "发布评论",
    publication_review: "发布评审",
    publication_issue_comment: "发布评论",
    publication_pr_comment: "发布 PR 评论",
    published: "已发布",
    not_published: "等待发布",
    publish_failed: "发布失败",
    skipped: "已跳过",
    auditAccepted: "审核通过",
    pr_opened: "PR 已创建",
    pr_merged: "PR 合并已同步",
    routing_selected: "已选择执行路径",
    backfilled: "导入历史任务",
    zombie: "Worker 已退出但进程尚未回收（僵尸进程）",
    prepare: "准备",
    analyze: "分析",
    plan: "计划",
    execute: "执行",
    verify: "验证",
    publish: "发布",
    handoff: "交付",
    preparing: "准备中",
    pending: "待开始",
    running: "运行中",
    completed: "已完成",
    failed: "失败",
    canceled: "已取消",
    ignored: "已忽略",
    queued: "已排队",
    detected: "已排队",
    authorized: "已排队",
    classified: "已排队",
    waiting_for_user: "等你回复",
    stale: "已中断",
    prepared: "已准备",
    failed_timeout: "Worker 执行超时",
    failed_worker_process_exited: "Worker 进程意外退出",
    failed_worker_startup: "Worker 启动失败",
    needs_resume: "本次执行中断，进入恢复流程",
    artifact_worker_stderr: "错误日志",
    artifact_worker_stdout: "运行输出",
    artifact_prompt: "给 Agent 的指令",
    artifact_github_context_json: "GitHub 上下文（JSON）",
    artifact_github_context_md: "GitHub 上下文",
    artifact_recovery_context: "恢复上下文",
    artifact_result: "执行结果",
    event_created: "任务已创建",
    event_user_response: "你回复了",
    event_changes_requested: "要求修改",
    event_retry_requested: "请求重新运行",
    event_publication_retry_requested: "请求重新发布",
    event_reopened: "重新打开",
    event_canceled: "已取消",
    event_completed: "已完成",
    event_operator_question: "Agent 提了一个问题",
    event_execution_failed: "执行失败",
    event_publication_failed: "发布失败",
    event_operator_decision_required: "请求批准",
    event_completion_acceptance_requested: "请求验收",
    event_pr_opened: "PR 已创建",
    event_pr_merged: "PR 合并已同步",
    reason_snapshot_changed: "读取期间 PR 内容发生了变化（多半是有新的推送），需要重新读取 PR。",
    reason_zombie: "执行进程意外退出。",
    reason_timeout: "执行超时。",
    reason_model_unavailable: "这一步需要的模型不可用。",
    reason_worktree: "运行工作区不可用（有不受 Robert 管理的工作区挡住了）。",
    reason_rate_limit: "GitHub 接口限流。",
    reason_permission: "Robert 没有执行这个操作的权限。",
    reason_auto_stopped: "自动执行已停止。",
  },
};

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
const PROBLEM_STATES = new Set([
  "failed", "publish_failed", "worker_unavailable", "environment", "source_closed",
  "pr_closed", "stuck", "needs_attention", "review_attention",
]);
const QUESTION_STATES = new Set(["needs_reply", "needs_acceptance", "needs_decision"]);
const CONFIRMED_COMMANDS = ["reply", "request_changes", "edit", "cancel", "retry", "retrigger", "reopen", "retry_publish"];
// Known worker and runtime errors, phrased for people who do not read stack traces.
const REASON_PATTERNS = [
  [/snapshot changed while fetching/i, "reason_snapshot_changed"],
  [/process_missing_or_zombie|zombie/i, "reason_zombie"],
  [/timed? ?out|timeout/i, "reason_timeout"],
  [/model unavailable/i, "reason_model_unavailable"],
  [/unmanaged_worktree|worktree.*unavailable/i, "reason_worktree"],
  [/rate.?limit/i, "reason_rate_limit"],
  [/permission denied|forbidden|not authori[sz]ed/i, "reason_permission"],
  [/^automatic execution stopped/i, "reason_auto_stopped"],
];

const params = new URLSearchParams(location.search);
const state = {
  session: null,
  board: null,
  headItemIds: new Set(),
  detail: null,
  historyLimits: {},
  selectedItem: params.get("item"),
  mobileColumn: COLUMNS.includes(params.get("column")) ? params.get("column") : null,
  view: params.get("view") || "active",
  language: localStorage.getItem("robertLanguage") ||
    (typeof navigator !== "undefined" && /^zh/i.test(navigator.language || "") ? "zh" : "en"),
  theme: localStorage.getItem("robertWorkbenchTheme") || "system",
  boardRequest: 0,
  detailRequest: 0,
  detailCheckedAt: 0,
  busy: false,
  composer: null,
  lastResult: null,
  returnFocus: null,
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
function known(key) {
  return Boolean((translations[state.language] || translations.en)[key] || translations.en[key]);
}
function label(code) {
  if (!code) return "";
  if (known(code)) return t(code);
  return String(code).replace(/[_-]+/g, " ").replace(/^\w/, (c) => c.toUpperCase());
}
function node(tag, className, text) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text !== undefined && text !== null) el.textContent = String(text);
  return el;
}
function button(text, callback, className = "quiet-button") {
  const el = node("button", className, text);
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
function relativeTime(value) {
  if (!value) return "—";
  const date = parseTime(value);
  if (Number.isNaN(date.getTime())) return value;
  const seconds = Math.round((date.getTime() - Date.now()) / 1000);
  const units = [["year", 31536000], ["month", 2592000], ["week", 604800], ["day", 86400], ["hour", 3600], ["minute", 60]];
  const locale = state.language === "zh" ? "zh-CN" : "en";
  try {
    const format = new Intl.RelativeTimeFormat(locale, { numeric: "auto" });
    for (const [unit, size] of units)
      if (Math.abs(seconds) >= size) return format.format(Math.round(seconds / size), unit);
    return format.format(seconds, "second");
  } catch (_) {
    return formatTime(value);
  }
}
function timeNode(value, className = "") {
  const el = node("time", className, relativeTime(value));
  if (value) {
    el.dateTime = value;
    el.title = formatTime(value);
  }
  return el;
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
function externalLink(value, text, className = "") {
  const url = safeExternalUrl(value);
  if (!url) return null;
  const a = node("a", className, text);
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
  clearTimeout(showStatus.timer);
  if (!isError)
    showStatus.timer = setTimeout(() => { el.textContent = ""; }, 4500);
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
  $$("[data-i18n-title]").forEach((el) => {
    el.title = t(el.dataset.i18nTitle);
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
function setOptions(select, values, text, first) {
  const previous = select.value;
  const options = first
    ? [Object.assign(node("option", "", first), { value: "" })]
    : [];
  values.forEach((value) =>
    options.push(
      Object.assign(node("option", "", text(value)), {
        value: typeof value === "string" ? value : value.repo_id,
      }),
    ),
  );
  if (previous && !options.some((el) => el.value === previous)) {
    options.push(Object.assign(node("option", "", previous), { value: previous }));
  }
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
  setOptions($("#agent-filter"), data.filters.agents, (a) => (a === "Auto" ? t("auto") : a), t("allAgents"));
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
function inputValue(selector) {
  return String($(selector).value || "").trim();
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
    if (inputValue(selector)) query.set(key, inputValue(selector));
  });
  if ($("#attention-filter").checked) query.set("attention", "true");
  if (state.view === "history") query.set("column", "history");
  if (cursor) query.set("cursor", cursor);
  return `/api/board?${query}`;
}
function activeConditions() {
  const conditions = [];
  const query = inputValue("#search-filter");
  if (query) conditions.push(t("conditionSearch", { value: query }));
  const selected = (selector) => {
    const select = $(selector);
    if (!select.value) return;
    const option = Array.from(select.options || []).find((el) => el.value === select.value);
    conditions.push(option ? option.textContent : select.value);
  };
  selected("#repo-filter");
  selected("#agent-filter");
  selected("#priority-filter");
  if ($("#attention-filter").checked) conditions.push(t("conditionAttention"));
  if (state.view === "history") conditions.push(t("conditionCanceled"));
  return conditions;
}

/* Status summary: the one place that turns backend codes into words. */
function summaryOf(item) {
  if (item.status_summary) return item.status_summary;
  const column = item.column;
  const fallback = {
    history: ["canceled", "neutral", "none"],
    done: ["completed", "success", "none"],
    backlog: ["draft", "neutral", "operator"],
    todo: ["queued", "neutral", "robert"],
    doing: ["running", "running", "robert"],
    review: ["in_review", "info", "github"],
    waiting: ["needs_attention", "attention", "operator"],
  }[column] || ["needs_attention", "neutral", "operator"];
  return {
    state: fallback[0], tone: fallback[1], owner: fallback[2], owner_identity: null,
    needs_action: column === "waiting", stage: { task: null, phase: item.execution?.phase || null },
    attempts: { total: 0, failed: 0 }, cause: null, primary: null, since: item.updated_at,
  };
}
function prLabel(item) {
  return item.pr?.number ? `PR #${item.pr.number}` : "PR";
}
function stageName(item) {
  const summary = summaryOf(item);
  return summary.stage?.task ? label(summary.stage.task) : columnLabel(item.column);
}
function stageText(item) {
  const summary = summaryOf(item);
  const phase = summary.stage?.phase;
  if (!summary.stage?.task) return columnLabel(item.column);
  return phase && !["completed", "canceled", "queued", "draft"].includes(summary.state)
    ? `${label(summary.stage.task)} · ${label(phase)}`
    : label(summary.stage.task);
}
function phaseText(item) {
  const phase = summaryOf(item).stage?.phase || item.execution?.phase;
  return phase ? label(phase) : t("execute");
}
function humanizeReason(text) {
  const raw = String(text || "").trim();
  if (!raw) return null;
  if (known(raw) && t(raw) !== raw) return { text: t(raw), raw };
  const match = REASON_PATTERNS.find(([pattern]) => pattern.test(raw));
  return { text: match ? t(match[1]) : raw, raw };
}
function chipLabel(item) {
  return t(`chip_${summaryOf(item).state}`);
}
function statusChip(item) {
  const summary = summaryOf(item);
  return node("span", `status-chip tone-${summary.tone}`, chipLabel(item));
}
function ownerShort(item) {
  const summary = summaryOf(item);
  if (summary.state === "draft") return t("ownerShort_draft");
  return t(`ownerShort_${summary.owner}`);
}
function ownerLabel(item) {
  const summary = summaryOf(item);
  const classes = ["owner-label"];
  if (summary.needs_action && ["operator", "initiator"].includes(summary.owner)) classes.push("owner-you");
  if (summary.owner === "admin") classes.push("owner-admin");
  return node("span", classes.join(" "), ownerShort(item));
}
function sourceInfo(item) {
  const source = item.source || {};
  const url = safeExternalUrl(source.url) || safeExternalUrl(item.pr?.url);
  let host = "";
  try { host = url ? new URL(url).hostname : ""; } catch (_) { host = ""; }
  let text;
  if (source.number !== undefined && source.number !== null)
    text = `${source.type === "pull_request" ? "PR" : "Issue"} #${source.number}`;
  else if (/tapd/i.test(host)) text = t("tapdItem");
  else if (item.origin_type === "web" || !url) text = t("localTask");
  else text = t("sourceLink");
  const open = /github/i.test(host) ? t("openSourceGithub") : /tapd/i.test(host) ? t("openSourceTapd") : t("openSource");
  return { text, url, open };
}
function cardLine(item) {
  const summary = summaryOf(item);
  if (summary.state === "running" && item.execution?.phase)
    return t("what_running", { phase: phaseText(item), attempt: item.execution.attempt_no || 1 });
  const cause = humanizeReason(summary.cause?.text);
  if (cause && (PROBLEM_STATES.has(summary.state) || QUESTION_STATES.has(summary.state)))
    return summary.stage?.task ? `${stageName(item)} · ${cause.text}` : cause.text;
  if (summary.state === "in_review") return t("what_in_review", { pr: prLabel(item) });
  if (item.reason_code === "rerun_requested") return t("what_rerun_requested", { stage: stageName(item) });
  return t(`what_${summary.state}`, { stage: stageName(item), phase: phaseText(item), pr: prLabel(item), total: summary.attempts?.total || 0, failed: summary.attempts?.failed || 0, attempt: item.execution?.attempt_no || 1, time: relativeTime(summary.since) });
}
function describe(item) {
  return cardLine(item);
}
function publishedCount(detail) {
  return (detail.tasks || []).flatMap((task) => task.publication_actions || []).filter((action) => action.status === "published").length;
}

function createCard(item) {
  const summary = summaryOf(item);
  const card = node("article", "task-card");
  Object.assign(card.dataset, {
    column: item.column,
    priority: item.priority,
    workItemId: item.work_item_id,
    tone: summary.tone,
  });
  card.draggable =
    !state.busy && item.valid_commands.includes("start") && canWrite(item);
  card.addEventListener("dragstart", (event) => {
    event.dataTransfer.setData("text/plain", item.work_item_id);
    event.dataTransfer.setData("application/x-robert-column", item.column);
    event.dataTransfer.effectAllowed = "move";
  });
  const top = node("div", "task-topline");
  const repo = node("span", "repo-label", (item.repo_full_name || "").split("/").pop());
  repo.title = item.repo_full_name || "";
  const priority = node("span", "priority", item.priority);
  priority.dataset.priority = item.priority;
  top.append(repo, priority);
  const title = node("h3", "task-title");
  const open = button(
    item.title,
    (event) => openDetail(item.work_item_id, undefined, event.currentTarget),
    "card-open",
  );
  open.setAttribute("aria-label", t("openTask", { title: item.title }));
  title.append(open);
  card.append(top, title, statusChip(item));
  if (item.column !== "history") card.append(node("p", "task-reason", cardLine(item)));
  const footer = node("div", "task-footer");
  footer.append(ownerLabel(item));
  const meta = node("div", "task-meta");
  const source = sourceInfo(item);
  const sourceLink = externalLink(source.url, source.text);
  meta.append(sourceLink || node("span", "", source.text));
  if (item.pr?.url && item.pr.url !== item.source?.url) {
    const pr = externalLink(item.pr.url, prLabel(item));
    if (pr) meta.append(pr);
  }
  meta.append(timeNode(item.execution?.heartbeat_at || summary.since || item.updated_at));
  footer.append(meta);
  card.append(footer);
  const quick = ["start", "reply", "approve"].find((command) => item.valid_commands.includes(command));
  if (quick) {
    const control = button(
      quick === "start" ? commandLabel("start") : commandLabel(quick),
      (event) =>
        quick === "reply"
          ? openDetail(item.work_item_id, quick, event.currentTarget)
          : executeCommand(item, quick),
      "start-card-button",
    );
    control.dataset.command = quick;
    control.disabled = state.busy || !canWrite(item);
    footer.append(control);
  }
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
    $(`.board-column[data-column="${state.mobileColumn}"]`).scrollIntoView({block: "nearest", inline: "nearest"});
}
function renderInbox(data) {
  const inbox = $("#attention-inbox");
  const count = data.counts?.needs_action;
  if (state.view === "history" || count === undefined || $("#attention-filter").checked) {
    inbox.hidden = true;
    return;
  }
  inbox.hidden = false;
  inbox.classList.toggle("calm", count === 0);
  $("#attention-inbox-title").textContent = count ? t("inboxCount", { count }) : t("inboxCalm");
  $("#attention-inbox-filter").hidden = !count;
  $("#attention-inbox-items").replaceChildren(
    ...(data.attention_items || []).map((item) => {
      const entry = button("", (event) => openDetail(item.work_item_id, undefined, event.currentTarget), "inbox-item");
      entry.append(statusChip(item), node("span", "inbox-title", item.title));
      entry.title = `${item.repo_full_name} · ${item.title}`;
      return entry;
    }),
  );
  const navCount = $("#nav-attention-count");
  navCount.textContent = count;
  navCount.hidden = !count;
}
function renderBoard(data) {
  if (!state.mobileColumn)
    state.mobileColumn = ["waiting", "doing", "review", "todo", "backlog", "done"].find((column) => data.counts[column] > 0) || "doing";
  [...COLUMNS, "history"].forEach((column) => {
    $$(`[data-count="${column}"]`).forEach((el) => {
      el.textContent = data.counts[column] || 0;
      el.dataset.zero = String(!data.counts[column]);
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
  const conditions = activeConditions().filter((text) => text !== t("conditionCanceled") || total === 0);
  const empty = total === 0 && conditions.length > 0;
  $("#filter-empty").hidden = !empty;
  $("#filter-empty-conditions").textContent = empty ? t("activeConditions", { conditions: conditions.join(state.language === "zh" ? "，" : ", ") }) : "";
  $("#board-app").hidden = empty;
  $("#filter-summary").textContent = !empty && conditions.length ? t("filteredSummary", { conditions: conditions.join(state.language === "zh" ? "，" : ", ") }) : "";
  renderInbox(data);
  renderMobileColumn({ scroll: !state.columnPositioned });
  state.columnPositioned = true;
}
function detailField(text, value) {
  const field = node("div", "detail-field");
  field.append(node("span", "", text), node("strong", "", value ?? "—"));
  return field;
}

/* Detail layer 1: conclusion first. */
function renderHeader(detail) {
  const summary = summaryOf(detail);
  $("#drawer-title").textContent = detail.title;
  const repo = $("#drawer-repo");
  const source = sourceInfo(detail);
  const link = externalLink(source.url, source.text);
  repo.replaceChildren(node("span", "", detail.repo_full_name), link || node("span", "", source.text));
  const status = $("#drawer-status");
  status.replaceChildren(
    node("span", "stage-label", stageText(detail)),
    statusChip(detail),
    ownerLabel(detail),
    node("span", "", t("updatedAgo", { time: relativeTime(summary.since || detail.updated_at) })),
  );
}
function causeFact(detail, summary) {
  const cause = humanizeReason(summary.cause?.text);
  const value = node("dd");
  if (!cause) {
    value.append(node("span", "fact-unknown", t("causeUnknown")));
    return value;
  }
  value.append(node("span", "", cause.text));
  const source = summary.cause.source;
  const origin = {
    attempt: t("causeFromAttempt", { number: summary.cause.attempt_no || summary.attempts?.total || 1 }),
    review: t("causeFromReview"),
    attention: t("causeFromAttention"),
    publication: t("causeFromPublication"),
    workflow: t("causeFromWorkflow"),
    cleanup: t("causeFromCleanup"),
  }[source];
  if (origin) value.append(node("span", "fact-source", `${origin}${summary.cause.at ? ` · ${formatTime(summary.cause.at)}` : ""}`));
  if (cause.raw !== cause.text) {
    const raw = node("span", "fact-source raw-detail");
    raw.append(node("span", "", `${t("rawText")}${state.language === "zh" ? "：" : ": "}`), node("code", "", cause.raw));
    value.append(raw);
  }
  return value;
}
function ownerFact(detail, summary) {
  if (summary.owner === "initiator") {
    const identity = summary.owner_identity || detail.created_by;
    return identity && identity === state.session?.operator_identity
      ? t("owner_initiator_you")
      : t("owner_initiator", { identity: identity || "—" });
  }
  return t(`owner_${summary.owner}`);
}
function impactText(detail, summary) {
  if (summary.state === "failed") {
    const count = publishedCount(detail);
    return count ? t("impact_failed_published", { count }) : t("impact_failed_none");
  }
  if (QUESTION_STATES.has(summary.state)) return t("impact_paused");
  return known(`impact_${summary.state}`) ? t(`impact_${summary.state}`) : null;
}
function renderLive(detail) {
  const summary = summaryOf(detail);
  const section = node("section", "detail-block live-detail");
  section.id = "detail-live";
  const values = {
    stage: stageName(detail),
    phase: phaseText(detail),
    pr: prLabel(detail),
    total: summary.attempts?.total || 0,
    failed: summary.attempts?.failed || 0,
    attempt: detail.execution?.attempt_no || 1,
    time: formatTime(detail.canceled_at || summary.since),
  };
  section.append(node("h3", "now-head", t(`headline_${summary.state}`, values)));
  const facts = node("dl", "facts");
  const add = (term, value) => {
    facts.append(node("dt", "", term));
    facts.append(typeof value === "string" ? node("dd", "", value) : value);
  };
  const what = summary.state === "source_closed" && detail.reason_code === "closed_source_running"
    ? t("what_source_closed_running")
    : detail.reason_code === "rerun_requested"
      ? t("what_rerun_requested", values)
      : t(`what_${summary.state}`, values);
  add(t("factWhat"), what);
  if (summary.state === "running" && detail.execution?.summary) add(t("factLatest"), detail.execution.summary);
  if (PROBLEM_STATES.has(summary.state)) {
    add(t("factCause"), causeFact(detail, summary));
    const impact = impactText(detail, summary);
    if (impact) add(t("factImpact"), impact);
  } else if (QUESTION_STATES.has(summary.state)) {
    const impact = impactText(detail, summary);
    if (impact) add(t("factImpact"), impact);
  } else if (!["completed", "canceled", "draft"].includes(summary.state)) {
    add(t("factProblem"), t("noProblem"));
  }
  add(t("factOwner"), ownerFact(detail, summary));
  section.append(facts);
  if (detail.publication?.blocked_reason)
    section.append(node("p", "notice", detail.publication.blocked_reason));
  if (detail.publication?.retry_pending)
    section.append(node("p", "notice", t("publicationRetry")));
  else if (detail.action_blocked_reason)
    section.append(node("p", "notice", t("publicationPending")));
  return section;
}

/* Next step: one primary action, the result it has, and quieter alternatives. */
function actionLabel(command, detail) {
  const summary = summaryOf(detail);
  if (["retrigger", "retry"].includes(command) && summary.stage?.task)
    return t("action_rerun", { stage: stageName(detail) });
  if (command === "reopen" && summary.state === "failed") return t("action_reopen_run");
  if (command === "cancel" && detail.execution?.status === "running") return t("stopTask");
  return commandLabel(command);
}
function consequence(command) {
  const will = {
    retrigger: [t("will_retrigger_1"), t("will_retrigger_2")],
    retry: [t("will_retry_1"), t("will_retry_2")],
    reopen: [t("will_reopen_1"), t("will_reopen_2")],
    retry_publish: [t("will_retry_publish")],
    reply: [t("will_reply")],
    approve: [t("will_approve")],
    start: [t("will_start")],
    request_changes: [t("will_request_changes")],
    edit: [t("will_edit")],
    cancel: [t("will_cancel")],
  }[command];
  if (!will) return null;
  const keeps = {
    retrigger: [t("keepsPublished")],
    retry: [t("keepsPublished")],
    reopen: [t("keepsPublished")],
    retry_publish: [t("keeps_retry_publish")],
    cancel: [t("keeps_cancel")],
  }[command] || [];
  const box = node("div", "consequence");
  box.append(node("strong", "", t("afterClick")));
  const list = node("ul");
  will.forEach((text) => list.append(node("li", "", text)));
  keeps.forEach((text) => list.append(node("li", "keeps", text)));
  box.append(list);
  return box;
}
function primaryCommand(detail) {
  const commands = detail.valid_commands || [];
  const summary = summaryOf(detail);
  if (summary.primary && commands.includes(summary.primary)) return summary.primary;
  return commands.length && !["cancel", "request_changes"].includes(commands[0]) ? commands[0] : null;
}
function renderActions(detail) {
  const summary = summaryOf(detail);
  const section = node("section", "detail-block");
  section.id = "detail-actions";
  section.append(node("h3", "", t("nextStep")));
  const commands = detail.valid_commands || [];
  const primary = primaryCommand(detail);
  const question = primary === "reply"
    ? detail.attention_signals?.find(signal => signal.event_id === attentionForCommand(detail, "reply"))
    : null;
  const questionText = question?.summary || summary.cause?.text;
  if (QUESTION_STATES.has(summary.state) && questionText)
    section.append(node("p", "question-text", questionText));
  const row = node("div", "command-row");
  const commandButton = (command, className) => {
    const b = button(actionLabel(command, detail), () => composeOrExecute(command), className);
    b.dataset.command = command;
    b.disabled = state.busy || !canWrite(detail);
    return b;
  };
  if (summary.primary === "open_pr") {
    const link = externalLink(detail.pr?.url, t("action_open_pr", { pr: prLabel(detail) }), "primary-button");
    if (link) row.append(link);
  }
  const composing = state.composer?.command;
  const hasChoices = Boolean(question?.choices?.length);
  if (hasChoices && !composing) {
    const choices = node("div", "question-choices");
    for (const [index, choice] of question.choices.entries()) {
      const option = button(null, () => executeCommand(detail, "reply", { choice: index + 1 }),
        "command-button question-choice");
      option.disabled = state.busy || !canWrite(detail);
      option.append(node("strong", "", choice.label));
      if (index === 0) option.append(node("span", "choice-recommendation", t("recommendedChoice")));
      option.append(node("span", "choice-tradeoff", choice.tradeoff));
      choices.append(option);
    }
    const other = button(t("otherChoice"), () => composeOrExecute("reply"), "command-button choice-other");
    other.disabled = state.busy || !canWrite(detail);
    choices.append(other);
    section.append(choices);
  }
  if (primary && primary !== composing && !hasChoices) row.append(commandButton(primary, "primary-button command-button"));
  if (row.children.length) section.append(row);
  if (primary && !state.composer) {
    const effects = consequence(primary);
    if (effects) section.append(effects);
  }
  const secondary = node("div", "secondary-row");
  commands.filter((command) => command !== primary && command !== composing).forEach((command) =>
    secondary.append(commandButton(command, command === "cancel" ? "command-button danger" : "command-button")));
  const source = sourceInfo(detail);
  const sourceUrl = summary.primary === "open_pr" && detail.pr?.url === source.url ? null : source.url;
  const github = externalLink(sourceUrl, source.open, "link-button");
  if (github) secondary.append(github);
  if (secondary.children.length) section.append(secondary);
  if (!commands.length && summary.primary !== "open_pr")
    section.append(node("p", "no-action", known(`noAction_${summary.owner}`) ? t(`noAction_${summary.owner}`) : t("noAction")));
  if (state.lastResult && state.lastResult.id === detail.work_item_id)
    section.append(node("p", "result-banner", state.lastResult.text));
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

/* Detail layer 2: the stages this item actually went through. */
function taskStep(lifecycle) {
  return { completed: "done", failed: "failed", running: "active", waiting_for_user: "waiting", canceled: "neutral", ignored: "neutral" }[lifecycle] || "pending";
}
function publicationStep(status) {
  return { published: "done", publish_failed: "failed", skipped: "neutral" }[status] || "pending";
}
function renderJourney(detail) {
  const summary = summaryOf(detail);
  const section = node("section", "detail-block lifecycle-section");
  section.id = "detail-journey";
  const tasks = detail.tasks || [];
  const attempts = tasks.flatMap(task => task.attempts || []);
  if (attempts.length) section.append(node("p", "lifecycle-counts", attemptCounts(attempts)));
  const milestones = [];
  if (detail.created_at) milestones.push({title: t("taskReceived"), at: detail.created_at, step: "done", first: true});
  tasks.forEach(task => {
    const runs = task.attempts || [];
    const latest = runs.at(-1);
    milestones.push({
      title: `${label(task.expected_output || task.route_id || "task")} · ${t(task.lifecycle)}`,
      at: runs[0]?.started_at || task.created_at,
      body: runs.length ? `${attemptCounts(runs)}${latest?.finished_at ? ` · ${t("finished")} ${formatTime(latest.finished_at)}` : ""}` : t("pending"),
      step: taskStep(task.lifecycle),
    });
  });
  const actions = tasks.flatMap(task => task.publication_actions || []);
  actions.forEach(action => {
    const key = `publication_${action.action_type}`;
    milestones.push({
      title: `${known(key) ? t(key) : label(action.action_type)} · ${t(action.status)}`,
      at: action.published_at || action.created_at,
      body: [humanizeReason(action.safe_error)?.text || (action.audit_status === "accepted" ? t("auditAccepted") : action.audit_status), action.attempt_count ? `${action.attempt_count}/${action.max_attempts || "?"}` : "", action.next_retry_at ? formatTime(action.next_retry_at) : "", action.required_action].filter(Boolean).join(" · "),
      step: publicationStep(action.status),
    });
  });
  // Event timestamps record when Robert observed the change, not necessarily
  // when it happened on GitHub. A closed PR alone is not evidence of a merge.
  (detail.events || []).filter(event => ["pr_opened", "pr_merged", "canceled", "completed"].includes(event.event_type))
    .filter(event => event.event_type !== "pr_opened" || !actions.some(action => action.action_type === "open_pr" && action.status === "published"))
    .forEach(event => {
    milestones.push({title: t(event.event_type), at: event.created_at, step: event.event_type === "canceled" ? "neutral" : "done"});
  });
  // A work item can be recorded after its task started, so intake stays first.
  milestones.sort((a, b) => (b.first === true) - (a.first === true)
    || (parseTime(a.at).getTime() || 0) - (parseTime(b.at).getTime() || 0));
  const settled = ["completed", "canceled"].includes(summary.state);
  const current = settled ? -1 : milestones.map((item) => item.step).findLastIndex((step) => step !== "done" && step !== "neutral");
  const marks = { done: "✓", failed: "✕", waiting: "…", active: "●", pending: "", neutral: "–" };
  const timeline = node("ol", "lifecycle-timeline");
  milestones.forEach((item, index) => {
    const row = node("li", "lifecycle-event");
    row.dataset.step = item.step;
    const mark = node("span", "step-mark", marks[item.step]);
    mark.setAttribute("aria-hidden", "true");
    const title = node("strong", "", item.title);
    if (index === current) {
      row.setAttribute("aria-current", "step");
      title.append(node("span", "current-step", t("currentStep")));
    }
    row.append(mark, title, node("time", "", formatTime(item.at)));
    if (item.body) row.append(node("p", "", item.body));
    timeline.append(row);
  });
  section.append(timeline);
  return section;
}

/* Detail layer 3: history, raw errors and logs, opened on demand. */
function disclosure(id, title, why, badge, { warn = false } = {}) {
  const box = node("details", "detail-block disclosure disclosure-card");
  if (id) box.id = id;
  const summary = node("summary");
  const text = node("span");
  text.append(node("span", "disclosure-title", title), node("span", "disclosure-why", why));
  summary.append(text, node("span", `disclosure-badge${warn ? " warn" : ""}`, badge || ""));
  const body = node("div", "disclosure-body");
  box.append(summary, body);
  box.body = body;
  return box;
}
function eventList(events) {
  const timeline = node("div", "timeline");
  events.forEach((item) => {
    const event = node("article", "timeline-event");
    const key = `event_${item.event_type}`;
    event.append(node("strong", "", known(key) ? t(key) : label(item.event_type)));
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
function unknownReviewRunIds(detail) {
  return (detail.tasks || []).flatMap(task => ((task.vote || task.review || task.pre_pr_review)?.runs || [])
    .filter(run => run.can_abandon).map(run => run.run_id)).sort();
}
function updateComposerState(detail) {
  const draft = state.composer;
  if (!draft?.send) return;
  const changed =
    draft.item.version !== detail.version ||
    attentionForCommand(draft.item, draft.command) !==
      attentionForCommand(detail, draft.command) ||
    (draft.command === "cancel" && JSON.stringify(draft.abandonRunIds) !== JSON.stringify(unknownReviewRunIds(detail)));
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
function artifactLabel(type) {
  const key = `artifact_${type}`;
  return known(key) ? t(key) : label(type);
}
function renderExecutionHistory(detail) {
  const tasks = detail.tasks || [];
  const all = tasks.flatMap((task) => task.attempts || []);
  const history = disclosure(
    "execution-history",
    t("executionHistory"),
    t("executionHistoryWhy"),
    all.length ? attemptCounts(all) : "",
    { warn: summaryOf(detail).tone === "danger" },
  );
  [...tasks].reverse().forEach((task) => {
    const entry = node("section", "execution-history-task");
    entry.append(node("h3", "", `${label(task.expected_output || task.route_id || "task")} · ${t(task.lifecycle)}`));
    const counts = node("p", "attempt-counts");
    counts.append(node("span", "", attemptCounts(task.attempts)), node("code", "raw-detail", task.task_id));
    entry.append(counts);
    if (task.vote || task.review || task.pre_pr_review) {
      const review = task.vote || task.review || task.pre_pr_review;
      entry.append(node("p", "", `${label(review.status)} · ${review.head_sha ? review.head_sha.slice(0, 12) : t("pending")}`));
      if (review.final_sha) entry.append(node("p", "raw-detail", `${t("reviewFinalCommit")} · ${review.final_sha}`));
      entry.append(node("p", "", review.process_pool
        ? t("reviewModels", { in_use: review.process_pool.in_use, limit: review.process_pool.limit })
        : t("reviewModelsUnknown")));
      const reason = humanizeReason(review.error);
      if (reason) {
        entry.append(node("p", "notice error", reason.text));
        if (reason.raw !== reason.text) entry.append(node("p", "raw-detail", reason.raw));
      }
      (review.runs || []).forEach((run) => {
        entry.append(node("p", "raw-detail", `${run.stage} · ${run.worker_id}${run.model ? " (" + run.model + ")" : ""} · #${run.attempt_no} · ${label(run.status)}${run.error ? " · " + run.error : ""}`));
      });
      entry.append(node("p", "raw-detail", review.round_id || ""));
    }
    if (!task.attempts.length) entry.append(node("p", "", t("noProgress")));
    const attempts = [...task.attempts].reverse();
    let shown = Math.min(state.historyLimits[task.task_id] || 10, attempts.length);
    const list = node("div", "attempt-list");
    const appendRuns = (runs) => runs.forEach((attempt) => {
      const run = node("details", "execution-history-attempt");
      run.dataset.attemptId = attempt.attempt_id;
      run.dataset.status = attempt.status;
      run.append(node("summary", "", `${t("attempt", { number: attempt.attempt_no })} · ${t(attempt.status)} · ${formatTime(attempt.started_at)}`));
      const times = node("div", "execution-times");
      times.append(detailField(t("heartbeat"), formatTime(attempt.heartbeat_at)), detailField(t("finished"), formatTime(attempt.finished_at)));
      run.append(times);
      if (attempt.failure || attempt.cleanup_reason) {
        const reasons = [attempt.failure, attempt.cleanup_reason].filter(Boolean).map(humanizeReason);
        run.append(node("p", "notice error", reasons.map((reason) => reason.text).join(" · ")));
        const raw = reasons.filter((reason) => reason.raw !== reason.text).map((reason) => reason.raw);
        if (raw.length) run.append(node("p", "raw-detail", raw.join(" · ")));
      }
      if (attempt.artifacts?.length) {
        const logs = node("div", "attempt-artifacts");
        logs.append(node("h4", "", t("artifacts")));
        const missing = attempt.artifacts.filter(artifact => !artifact.available);
        if (missing.length) logs.append(node("p", "notice", `${t("missingArtifact")} (${missing.map(artifact => artifact.artifact_type).join(", ")})`));
        attempt.artifacts.filter(artifact => artifact.available).forEach((artifact) => {
          const link = node("a", "quiet-button", artifactLabel(artifact.artifact_type));
          link.title = artifact.artifact_type;
          link.href = `/artifact.txt?${new URLSearchParams({task_id: task.task_id, artifact_type: artifact.artifact_type, artifact_id: artifact.artifact_id})}`;
          link.target = "_blank";
          link.rel = "noopener noreferrer";
          logs.append(link);
        });
        run.append(logs);
      }
      (attempt.phases || []).forEach((phase) => {
        const row = node("article", "timeline-event");
        row.append(node("strong", "", `${label(phase.phase)} · ${label(phase.status)}`), node("time", "", formatTime(phase.created_at)), node("p", "", phase.summary));
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
    history.body.append(entry);
  });
  if (!tasks.length) history.body.append(node("p", "", t("noProgress")));
  return history;
}
function copyButton(value) {
  return button(t("copy"), async (event) => {
    const control = event.currentTarget;
    try {
      await navigator.clipboard.writeText(String(value));
      control.textContent = t("copied");
      setTimeout(() => { control.textContent = t("copy"); }, 1500);
    } catch (_) {
      control.textContent = String(value);
    }
  }, "link-button");
}
function layerLabel(key) {
  return node("h3", "layer-label", t(key));
}
function renderSupervision(detail) {
  const incidents = detail.supervision_incidents || [];
  const box = disclosure("detail-supervision", t("supervision"), t("supervisionWhy"),
    incidents.length ? t("incidentsCount", { count: incidents.length }) : "", { warn: incidents.length > 0 });
  const panel = box.body;
  const supervision = detail.supervision_decision;
  if (supervision) {
    const entry = node("section", "execution-history-task");
    const decision = supervision.decision || {};
    entry.append(node("p", "", `${supervision.status} · ${decision.action || ""}`));
    [decision.reason, decision.message, decision.expected_progress, supervision.next_check_at]
      .filter(Boolean).forEach(text => entry.append(node("p", "", text)));
    const failure = supervision.last_failure;
    if (failure) {
      [failure.decision_id, failure.failed_at, failure.safe_error]
        .filter(Boolean).forEach(text => entry.append(node("p", "", text)));
      if (failure.exit_code !== null && failure.exit_code !== undefined)
        entry.append(node("p", "", `Exit: ${failure.exit_code}`));
    }
    panel.append(entry);
  }
  if (!incidents.length && !supervision) panel.append(node("p", "", t("noIncidents")));
  incidents.forEach(incident => {
    const entry = node("section", "execution-history-task");
    entry.append(node("p", "", `${incident.status} · ${incident.reason_code}`));
    entry.append(node("p", "", `${incident.scope_id} · ${incident.last_observed_at}`));
    if (incident.diagnosis?.expected_progress)
      entry.append(node("p", "", incident.diagnosis.expected_progress));
    (incident.recovery_actions || []).forEach(action =>
      entry.append(node("p", "", `${action.action_kind}: ${action.status} (${action.budget_cost})`)));
    if (incident.repair_work_item_id) {
      entry.append(node("p", "", incident.repair_work_item_id));
      Object.entries(incident.repair_verification || {}).forEach(([stage, status]) =>
        entry.append(node("p", "", `${stage}: ${status}`)));
    }
    panel.append(entry);
  });
  return box;
}
function renderDetail(detail, { preserve = false } = {}) {
  state.detail = detail;
  state.detailCheckedAt = Date.now();
  renderHeader(detail);
  if (preserve && $("#detail-live")) {
    const nextLive = renderLive(detail);
    $("#detail-now").dataset.tone = summaryOf(detail).tone;
    $("#detail-live").replaceWith(nextLive);
    $("#detail-journey")?.replaceWith(renderJourney(detail));
    if (!state.busy && !state.composer) $("#detail-actions").replaceWith(renderActions(detail));
    const previousHistory = $("#execution-history");
    const nextHistory = renderExecutionHistory(detail);
    nextHistory.open = previousHistory.open;
    const openAttempts = new Set([...previousHistory.querySelectorAll("details")].filter(el => el.open).map(el => el.dataset.attemptId));
    nextHistory.querySelectorAll("details").forEach(el => { el.open = openAttempts.has(el.dataset.attemptId); });
    previousHistory.replaceWith(nextHistory);
    const previousSupervision = $("#detail-supervision");
    const nextSupervision = renderSupervision(detail);
    nextSupervision.open = previousSupervision?.open || false;
    previousSupervision?.replaceWith(nextSupervision);
    $("#detail-agent-session")?.update?.(detail, { poll: true });
    updateComposerState(detail);
    return;
  }
  state.composer = null;
  const content = $("#drawer-content");
  const now = node("div", "now-panel");
  now.id = "detail-now";
  now.dataset.tone = summaryOf(detail).tone;
  const composer = node("div");
  composer.id = "composer-host";
  now.append(renderLive(detail), renderActions(detail), composer);

  const description = disclosure(null, t("requirement"), t("requirementWhy"), "");
  description.body.append(node("p", "detail-description",
    detail.description || (detail.origin_type === "github" ? t("githubDescription") : t("noDescription"))));

  const technical = disclosure(null, t("technical"), t("technicalWhy"), "");
  const grid = node("div", "detail-grid");
  [
    [t("task"), detail.task_id],
    [t("branch"), detail.branch],
    [t("routing"), detail.agent === "Auto" ? t("auto") : detail.agent],
    [t("priority"), detail.priority],
    [t("version"), detail.version],
  ].forEach(([text, value]) => {
    const field = detailField(text, value);
    if (value && [t("task"), t("branch")].includes(text)) field.append(copyButton(value));
    grid.append(field);
  });
  technical.body.append(grid);

  const activity = disclosure(null, t("timeline"), t("timelineWhy"), (detail.events || []).length ? t("eventsCount", { count: (detail.events || []).length }) : "");
  const events = eventList(detail.events || []);
  events.id = "detail-events";
  activity.body.append((detail.events || []).length ? events : node("p", "", t("noEvents")));
  if (detail.events_next_cursor)
    activity.body.append(
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
    layerLabel("layerNow"),
    now,
    layerLabel("layerProgress"),
    renderJourney(detail),
    AgentSessionView.create(detail, { node, button, disclosure, t, formatTime, get: url => BoardClient.get(url) }),
    layerLabel("layerHistory"),
    renderExecutionHistory(detail),
    renderSupervision(detail),
    description,
    activity,
    technical,
  );
}
function composeOrExecute(command) {
  if (state.busy || !state.detail) return;
  if (!CONFIRMED_COMMANDS.includes(command)) {
    executeCommand(state.detail, command);
    return;
  }
  const detail = state.detail;
  const draft = { command, item: detail };
  state.composer = draft;
  const host = $("#composer-host");
  const form = node("form", "command-composer");
  const inputs = {};
  function input(text, name, value, multiline = true) {
    const field = node("label", "", text);
    const el = node(multiline ? "textarea" : "input");
    el.value = value || "";
    el.required = true;
    el.maxLength = name === "title" ? 200 : command === "edit" ? 20000 : 8000;
    if (multiline) el.rows = 4;
    field.append(el);
    form.append(field);
    inputs[name] = el;
  }
  form.append(node("h4", "", actionLabel(command, detail)));
  if (command === "cancel") {
    form.append(node("p", "", t("cancelHelp")));
    draft.abandonRunIds = unknownReviewRunIds(detail);
    if (draft.abandonRunIds.length) {
      const check = node("label", "check");
      draft.abandonment = node("input");
      draft.abandonment.type = "checkbox";
      draft.abandonment.checked = false;
      check.append(draft.abandonment, node("span", "", t("abandonReviewHelp")));
      form.append(check, node("p", "raw-detail", draft.abandonRunIds.join(", ")));
    }
  }
  else if (command === "edit") {
    input(t("title"), "title", detail.title, false);
    input(t("requirement"), "description", detail.description);
  } else if (["reply", "request_changes"].includes(command)) {
    form.append(node("p", "", t(command === "reply" ? "replyHelp" : "changesHelp")));
    input(
      command === "reply" ? t("yourReply") : t("requestedChanges"),
      "body",
      "",
    );
  } else if (command === "retry") {
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
    form.append(field);
    inputs.routing = select;
  }
  const effects = consequence(command);
  if (effects) form.append(effects);
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
    command === "cancel" ? "danger-button" : "primary-button",
    command === "cancel"
      ? t("confirmCancel")
      : command === "edit"
        ? t("save")
        : ["reply", "request_changes"].includes(command)
          ? commandLabel(command)
          : t("confirmAction"),
  );
  send.type = "submit";
  row.append(send);
  draft.send = send;
  draft.notice = node("p", "notice");
  draft.confirm = button(t("reviewDraft"), () => {
    if (command === "cancel") return composeOrExecute(command);
    draft.item = state.detail;
    updateComposerState(state.detail);
  });
  form.append(draft.notice, draft.confirm, row);
  updateComposerState(detail);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const extra = {};
    if (draft.abandonment?.checked) extra.abandon_review_run_ids = draft.abandonRunIds;
    Object.entries(inputs).forEach(([name, el]) => {
      if (name !== "routing") extra[name] = el.value;
    });
    if (inputs.routing) {
      extra.routing_mode = inputs.routing.value === "auto" ? "auto" : "manual";
      extra.requested_worker =
        extra.routing_mode === "auto" ? null : inputs.routing.value;
    }
    executeCommand(draft.item, command, extra);
  });
  host.replaceChildren(form);
  $("#detail-actions").replaceWith(renderActions(detail));
  (inputs.body || inputs.title || send).focus();
}
async function openDetail(id, command, origin) {
  const request = ++state.detailRequest;
  if (state.selectedItem !== id) state.lastResult = null;
  state.selectedItem = id;
  state.composer = null;
  if (origin) state.returnFocus = id;
  updateUrl({ item: id });
  feedback("#drawer-feedback", "");
  $("#drawer-title").textContent = t("loading");
  $("#drawer-status").replaceChildren();
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
  state.lastResult = null;
  $("#work-item-drawer").close();
  updateUrl({ item: null });
  const id = state.returnFocus;
  state.returnFocus = null;
  if (id) {
    const card = $$(".task-card").find((el) => el.dataset.workItemId === id);
    card?.querySelector(".card-open")?.focus();
  }
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
    const resultKey = `result_${command}`;
    state.lastResult = { id: item.work_item_id, text: known(resultKey) ? t(resultKey) : t("commandCompleted", { action: commandLabel(command) }) };
    state.composer = null;
    if (state.selectedItem === item.work_item_id) {
      const detail =
        command === "retrigger"
          ? await BoardClient.get(
              `/api/work-items/${encodeURIComponent(item.work_item_id)}`,
            )
          : response;
      if (state.selectedItem === item.work_item_id) renderDetail(detail);
    }
    showStatus(state.lastResult.text);
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
let boardRefresh = null;
let pendingBoardRefresh = null;
let boardPollTimer = null;

function boardPollDelay() {
  const active = state.board?.capacity?.running > 0 ||
    ["todo", "doing"].some(column => state.board?.counts?.[column] > 0) ||
    state.detail?.execution?.status === "running" ||
    $("#detail-agent-session")?.pollingActive;
  return active ? 15000 : 45000;
}
function scheduleBoardPoll() {
  clearTimeout(boardPollTimer);
  boardPollTimer = null;
  if (!state.polling || document.hidden) return;
  let delay = boardPollDelay();
  // Do not let a 45s idle tick extend a pending 60s freshness deadline.
  if ((state.pageCount || 1) > 1 && state.pagesCheckedAt)
    delay = Math.min(delay, Math.max(0, 60000 - (Date.now() - state.pagesCheckedAt)));
  if (state.selectedItem && state.detailCheckedAt)
    delay = Math.min(delay, Math.max(0, 60000 - (Date.now() - state.detailCheckedAt)));
  const sessionDelay = $("#detail-agent-session")?.pollDelay;
  if (Number.isFinite(sessionDelay)) delay = Math.min(delay, sessionDelay);
  // Errors or paused controls should retry at the normal cadence, not spin.
  if (delay <= 0) delay = boardPollDelay();
  boardPollTimer = setTimeout(() => refreshBoard({ quiet: true, automatic: true }), delay);
}
function refreshBoard({ quiet = false, more = false, automatic = false } = {}) {
  if (automatic && (document.hidden || state.busy || $("#new-task-dialog").open)) {
    scheduleBoardPoll();
    return Promise.resolve();
  }
  if (boardRefresh) {
    // Periodic ticks can share the current read. User intent needs one later
    // read, after the action/filter change, with the latest controls.
    if (!automatic || state.boardUrl !== boardUrl()) {
      state.boardRequest++;
      pendingBoardRefresh = {
        quiet: quiet && (pendingBoardRefresh?.quiet ?? true),
        more: more && (pendingBoardRefresh?.more ?? true),
        automatic: automatic && (pendingBoardRefresh?.automatic ?? true),
      };
    }
    return boardRefresh;
  }
  clearTimeout(boardPollTimer);
  boardRefresh = (async () => {
    let options = { quiet, more, automatic };
    do {
      pendingBoardRefresh = null;
      await performBoardRefresh(options);
      options = pendingBoardRefresh;
    } while (options);
  })().finally(() => {
    boardRefresh = null;
    scheduleBoardPoll();
  });
  return boardRefresh;
}
function cardFingerprint(item) {
  if (!item) return null;
  // Include execution heartbeat/phase, workflow runs and attention identities;
  // version/status alone cannot establish detail freshness.
  return JSON.stringify([
    item.work_item_id, item.version, item.updated_at, item.title, item.priority,
    item.column, item.reason_code, item.reason_summary, item.routing_mode,
    item.agent, item.task_id, item.task_lifecycle, item.execution, item.review,
    item.pre_pr_review, item.vote, item.publication, item.action_blocked_reason,
    item.source, item.pr, item.attention_signals, item.valid_commands, item.status_summary,
    item.repo_id, item.repo_full_name, item.origin_type, item.branch, item.worktree_label,
  ]);
}
async function performBoardRefresh({ quiet, more, automatic }) {
  const request = ++state.boardRequest;
  const url = boardUrl();
  const sameQuery = state.boardUrl === url;
  if (!sameQuery) {
    state.pageCount = 1;
    state.pagesCheckedAt = 0;
    more = false;
  }
  state.boardUrl = url;
  const cursor = more ? state.board?.next_cursor : null;
  if (more && !cursor) return;
  // Rebuild all loaded pages at least once a minute to bound stale retained
  // cards and repair pagination after items move, disappear or change state.
  const headOnly = automatic && sameQuery && state.board &&
    (state.pageCount || 1) > 1 &&
    Date.now() - state.pagesCheckedAt < 60000;
  $("#sync-status").textContent = t("syncing");
  $("#load-more").disabled = true;
  try {
    if (!state.session) await loadSession();
    let data = more
      ? await BoardClient.get(boardUrl(cursor))
      : await BoardClient.pages(url, headOnly ? 1 : state.pageCount || 1);
    if (request !== state.boardRequest || url !== boardUrl()) return;
    const freshItems = data.items;
    if (more && state.board) {
      state.pageCount = (state.pageCount || 1) + 1;
      const merged = new Map(
        state.board.items.map((item) => [item.work_item_id, item]),
      );
      data.items.forEach((item) => merged.set(item.work_item_id, item));
      data = { ...state.board, items: Array.from(merged.values()), next_cursor: data.next_cursor };
    } else if (headOnly) {
      const freshIds = new Set(data.items.map(item => item.work_item_id));
      // The head is authoritative. Only cards from loaded, unrefreshed history
      // survive; cards promoted into the head leave that retained history.
      data.items = [...data.items, ...state.board.items.filter(item =>
        !state.headItemIds.has(item.work_item_id) && !freshIds.has(item.work_item_id))];
      data.next_cursor = state.board.next_cursor;
    }
    if (!more) {
      state.headItemIds = new Set(data.headItemIds);
      if (!headOnly) {
        state.pageCount = data.pageCount;
        state.pagesCheckedAt = Date.now();
      }
    }
    state.board = data;
    renderFilters(data);
    renderBoard(data);
    feedback("#board-feedback", "");
    $("#sync-status").textContent = t("synced", {
      time: new Date().toLocaleTimeString(state.language === "zh" ? "zh-CN" : "en"),
    });
    const selected = state.selectedItem;
    const selectionRequest = state.detailRequest;
    const freshSelected = freshItems.find(item => item.work_item_id === selected);
    const detailUnchanged = automatic && freshSelected &&
      state.detail?.work_item_id === selected &&
      cardFingerprint(freshSelected) === cardFingerprint(state.detail) &&
      Date.now() - state.detailCheckedAt < 60000;
    if (selected && !state.busy && detailUnchanged) {
      // Transcript writes can precede heartbeat changes. Keep its active poll
      // independent of whether the much larger detail read was needed.
      $("#detail-agent-session")?.update?.(state.detail, { poll: true });
    } else if (selected && !state.busy) {
      try {
        const detail = await BoardClient.get(
          `/api/work-items/${encodeURIComponent(selected)}`,
        );
        if (
          request === state.boardRequest &&
          selected === state.selectedItem &&
          selectionRequest === state.detailRequest &&
          !state.busy && url === boardUrl()
        )
          renderDetail(detail, { preserve: true });
      } catch (error) {
        if (request === state.boardRequest && selected === state.selectedItem &&
            selectionRequest === state.detailRequest && !state.busy && url === boardUrl())
          feedback("#drawer-feedback", error.message);
      }
    }
    if (!quiet) showStatus(t("boardRefreshed"));
  } catch (error) {
    if (request === state.boardRequest && url === boardUrl()) {
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
  if (event.submitter?.dataset.mode) $("#new-task-mode").value = event.submitter.dataset.mode;
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
function clearFilters() {
  $("#search-filter").value = "";
  ["#repo-filter", "#agent-filter", "#priority-filter"].forEach((selector) => { $(selector).value = ""; });
  $("#attention-filter").checked = false;
  state.view = "active";
  $("#view-filter").value = "active";
  updateUrl({ view: null });
  refreshBoard({ quiet: true });
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
  $("#work-item-drawer").addEventListener("click", (event) => {
    if (event.target === event.currentTarget) closeDetail();
  });
  $("#new-task-form").addEventListener("submit", createTask);
  $("#new-task-form").addEventListener("invalid", (event) => {
    const field = event.target;
    const text = field.name === "title" ? t("title") : field.name === "description" ? t("requirement") : t("repository");
    field.setCustomValidity(t("requiredField", { field: text }));
    field.setAttribute("aria-invalid", "true");
    const ids = (field.getAttribute("aria-describedby") || "").split(" ");
    const error = document.getElementById(ids.find((id) => id.endsWith("-error")));
    if (error) error.textContent = field.validationMessage;
  }, true);
  $("#new-task-form").addEventListener("input", (event) => {
    event.target.setCustomValidity("");
    event.target.removeAttribute("aria-invalid");
    const ids = (event.target.getAttribute("aria-describedby") || "").split(" ");
    const error = document.getElementById(ids.find((id) => id.endsWith("-error")) || "");
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
  $("#attention-inbox-filter").addEventListener("click", () => {
    $("#attention-filter").checked = true;
    refreshBoard({ quiet: true });
  });
  $("#clear-filters").addEventListener("click", clearFilters);
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
    scheduleBoardPoll();
    if (!document.hidden && !state.busy) return refreshBoard({ quiet: true, automatic: true });
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
  // The initial detail belongs to openDetail, which also opens the drawer.
  state.selectedItem = null;
  await refreshBoard({ quiet: true });
  if (selected) await openDetail(selected);
  state.polling = true;
  scheduleBoardPoll();
}
start();
