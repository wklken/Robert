// Chat presentation only. Status and permitted actions come from Robert's CLI.
const MAX_REPLY = 3500;
const PAGE_SIZE = 3;
const labels = {
  running: "运行中", completed: "已完成", failed: "失败", canceled: "已取消",
  queued: "排队中", waiting_for_user: "等待回复", waiting_user: "等待回复",
  worker_running: "执行中", waiting_worker: "等待执行", needs_attention: "需要处理",
  worker_stale: "心跳异常", worker_retrying: "重试中", result_pending_audit: "等待审核",
  waiting_publish: "等待发布", ignored: "已忽略", stopped: "已停止", unknown: "未知",
  draft: "草稿", accepted: "已接收，等待执行", creating: "正在创建 Issue",
  issue_created: "Issue 已创建", admitted: "任务已接收", delivered: "结果已交付",
  attention: "需要处理", ok: "成功", skipped: "已跳过",
  needs_attention_tasks: "任务需要处理", worker_stale_tasks: "任务心跳异常",
  worker_retrying_tasks: "任务重试中", result_pending_audit_tasks: "任务等待审核",
  authorized_unattached_events: "已授权事件等待处理", pending_actor_permission_events: "用户权限待确认",
  pending_authorization_events: "事件授权待确认", waiting_publish_tasks: "任务等待发布",
  execution_failed: "执行失败", publication_requires_attention: "发布需要处理",
  pending_authorization: "事件授权待确认", admission_disposition_missing: "已接收输入缺少处理记录",
};
const label = value => labels[value] ?? String(value ?? "未知");
const list = value => Array.isArray(value) ? value : [];
const short = (value, limit = 240) => {
  const text = String(value ?? "").replace(/\s+/g, " ").trim();
  return text.length > limit ? text.slice(0, limit) + "…（截断）" : text;
};
const errorText = value => typeof value === "string" ? value
  : value?.safe_error ?? value?.message ?? value?.reason_code ?? value?.status;

function bounded(text, footer = "", maximum = MAX_REPLY) {
  const tail = footer ? "\n\n" + footer : "";
  const limit = maximum - tail.length;
  if (text.length <= limit) return text + tail;
  const notice = "\n…（内容已截断）";
  let preview = text.slice(0, limit - notice.length);
  // Prefer complete lines so a copied operation never contains a partial ID.
  const newline = preview.lastIndexOf("\n");
  if (newline > preview.length / 2) preview = preview.slice(0, newline);
  if (/[\uD800-\uDBFF]$/.test(preview)) preview = preview.slice(0, -1);
  return preview + notice + tail;
}

function statusText(data) {
  const daemon = data.daemon ?? {};
  const summary = data.summary ?? {};
  const lines = ["Robert 状态", `服务记录：${label(daemon.latest_run?.status)}`];
  if (daemon.latest_lease?.heartbeat_at) lines.push(`最近心跳：${short(daemon.latest_lease.heartbeat_at)}`);
  const counts = Object.entries(data.operator_state_counts ?? {});
  lines.push("近期任务：" + (counts.map(([state, count]) => `${label(state)} ${count}`).join(" · ") || "暂无记录"));
  if (summary.repos !== undefined) lines.push(`仓库 ${summary.repos} · 历史任务 ${summary.tasks ?? "未知"}`);
  const alerts = list(data.operator_alerts);
  if (alerts.length) lines.push("\n需要处理：", ...alerts.slice(0, 5).map(a => `- ${label(a.alert_type)}：${a.count}`));
  const steps = list(data.operator_next_steps);
  for (const step of steps.slice(0, 3)) {
    lines.push(`- ${short(step.source_title || step.reason)}${step.safe_error ? "：" + short(step.safe_error) : ""}`);
    if (step.target_kind === "task") lines.push(`/robert-task ${step.target_id}`);
    else if (step.github_url) lines.push(short(step.github_url));
  }
  const incidents = list(data.supervision_incidents);
  if (incidents.length) {
    lines.push("\n监督告警（当前查询）：");
    for (const incident of incidents.slice(0, 3)) {
      lines.push(`- ${short(label(incident.reason_code))}`);
      if (incident.scope_type === "task") lines.push(`/robert-task ${incident.scope_id}`);
    }
    lines.push("监督详情：本地执行 robert supervisor status");
  }
  const tasks = list(data.recent_tasks);
  if (tasks.length) {
    lines.push("\n最近任务：");
    for (const task of tasks.slice(0, 3)) {
      lines.push(`- ${label(task.operator_state ?? task.lifecycle)} · ${short(task.source_key, 100)}`,
        `/robert-task ${task.task_id}`);
    }
  }
  if (data.latest_run?.run_id) lines.push(`\n最近运行：${label(data.latest_run.status)}`, `/robert-run ${data.latest_run.run_id}`);
  lines.push("\n待办和操作：/robert-inbox");
  return lines.join("\n");
}

function taskText(task) {
  const lines = [`任务 ${task.task_id}`, `状态：${label(task.lifecycle)} · 路由：${short(task.route_id)}`];
  if (task.source_key) lines.push(`来源：${short(task.source_key)}`);
  const attempt = task.latest_attempt;
  if (attempt) {
    lines.push(`最近执行：${label(attempt.status)}`);
    const failure = errorText(attempt.failure);
    if (failure) lines.push(`原因：${short(failure, 500)}`);
  }
  if (task.latest_phase) lines.push(`阶段：${short(task.latest_phase.phase ?? task.latest_phase.phase_key ?? task.latest_phase.status)}`);
  if (task.latest_result?.handoff) lines.push(`结果：${short(task.latest_result.handoff, 500)}`);
  if (task.action_counts?.total) {
    lines.push("发布：" + Object.entries(task.action_counts.publish_status ?? {}).map(([state, count]) => `${label(state)} ${count}`).join(" · "));
  }
  const artifacts = list(task.artifacts);
  if (artifacts.length) lines.push("\n产物：", ...artifacts.slice(0, 3).map(a => `/robert-artifact ${task.task_id} ${a.artifact_type}`));
  lines.push("\n回复、批准或重试：/robert-inbox（查看当前允许的操作）");
  return lines.join("\n");
}

function runText(run) {
  const lines = [`运行 ${run.run_id}`, `状态：${label(run.status)}`];
  if (run.started_at) lines.push(`开始：${short(run.started_at)}`);
  if (run.finished_at) lines.push(`结束：${short(run.finished_at)}`);
  if (errorText(run.error)) lines.push(`原因：${short(errorText(run.error), 500)}`);
  const steps = [...list(run.steps), ...list(run.repo_steps)];
  const failures = steps.filter(step => step.status === "failed" || errorText(step.error));
  if (failures.length) {
    lines.push("\n异常步骤：", ...failures.slice(0, 5).map(step =>
      `- ${short(step.repo_full_name || "")} ${short(step.step_key)}：${short(errorText(step.error) || label(step.status), 400)}`));
  } else if (steps.length) {
    lines.push(`步骤：${steps.length} 项`, ...steps.slice(0, 5).map(step => `- ${short(step.step_key)}：${label(step.status)}`));
  }
  if (run.diagnostics?.summary) lines.push(short(run.diagnostics.summary, 500));
  return lines.join("\n");
}

function inboxText(result, page) {
  const messages = list(result.messages);
  const blocks = messages.map(message => {
    const lines = [short(message.title || message.summary || message.type, 200)];
    if (message.title && message.summary) lines.push(short(message.summary, 200));
    if (message.safe_error) lines.push(`原因：${short(message.safe_error, 200)}`);
    if (message.issue_url || message.pr_url) lines.push(short(message.issue_url || message.pr_url, 180));
    if (message.required_action) lines.push(short(message.required_action, 200));
    const commands = [];
    if (message.task_id) commands.push(`/robert-task ${message.task_id}`);
    const actions = list(message.actions);
    const prefix = `/robert-respond ${message.notification_id} ${message.version}`;
    const choices = list(message.choices);
    if (actions.includes("reply")) {
      for (const [index, choice] of choices.slice(0, 5).entries()) {
        lines.push(`${index + 1}. ${short(choice.label, 80)}${choice.tradeoff ? " — " + short(choice.tradeoff, 80) : ""}`);
        if (message.selectable_choices === true) commands.push(`${prefix} pick ${index + 1}`);
      }
      commands.push(`${prefix} reply <你的答案>`);
    }
    for (const action of actions.filter(action => ["approve", "retry", "retry_publish", "cancel", "ack"].includes(action))) {
      commands.push(`${prefix} ${action}`);
    }
    if (message.request_id) commands.push(`/robert-inbox status ${message.request_id}`);
    return bounded(lines.join("\n"), commands.join("\n"), 2600);
  });
  const pages = [[]];
  for (const block of blocks) {
    let current = pages[pages.length - 1];
    if (current.length && (current.length >= PAGE_SIZE || [...current, block].join("\n\n").length > 2800)) {
      current = [];
      pages.push(current);
    }
    current.push(block);
  }
  if (page > pages.length) return { text: `待办没有第 ${page} 页，请使用 /robert-inbox 查看。`, footer: "查看待办：/robert-inbox" };
  const lines = [messages.length ? `Robert 待办：${messages.length} 条（第 ${page}/${pages.length} 页）` : "Robert 当前没有待处理消息。",
    ...pages[page - 1]];
  if (result.dispatch_help) {
    lines.push("\n发起任务：", result.dispatch_help);
    if (result.dispatch_repos?.length) {
      lines.push("可用仓库：" + result.dispatch_repos.slice(0, 3).map(repo => short(repo, 80)).join("、")
        + (result.dispatch_repos.length > 3 ? "（更多仓库见 --json）" : ""));
    }
  }
  const remaining = pages.slice(page).reduce((count, entries) => count + entries.length, 0);
  return { text: lines.join("\n\n"), footer: (remaining ? `还有 ${remaining} 条未展示：/robert-inbox page ${page + 1}\n` : "")
    + "诊断 JSON：/robert-inbox --json" };
}

function dispatchText(result) {
  const lines = [`Robert 请求：${label(result.status)}`, `标题：${short(result.title)}`, `请求：${result.request_id}`];
  if (result.safe_error) lines.push(`原因：${short(result.safe_error, 500)}`);
  if (result.issue_url) lines.push(result.issue_url);
  const taskId = result.task?.task_id ?? result.task_id;
  if (taskId) lines.push(`/robert-task ${taskId}`);
  if (result.submit_command) lines.push("\n确认后提交：", result.submit_command);
  if (["failed", "attention"].includes(result.status)) lines.push("\n请求重试：", `/robert-inbox retry ${result.request_id}`);
  if (result.result?.summary) lines.push(`结果：${short(result.result.summary, 500)}`);
  lines.push("\n查看进展：", `/robert-inbox status ${result.request_id}`);
  return lines.join("\n");
}

function responseText(result, input) {
  if (result.status === "awaiting_custom_reply") return result.message;
  const lines = [result.status === "acknowledged"
    ? "Robert 已记录收悉。ack 仅记录收悉，不会解决故障、重试任务或批准修复。"
    : `Robert 已收到${({ reply: "答复", approve: "批准请求", retry: "重试请求", retry_publish: "发布重试请求", cancel: "取消请求" })[input?.action] ?? "请求"}。`];
  if (result.notification_id) lines.push(`关联消息：${result.notification_id}`);
  lines.push(result.task_id ? `/robert-task ${result.task_id}` : "查看当前待办：/robert-inbox");
  return lines.join("\n");
}

export function formatReply(args, result, input, { jsonOutput = false, page = 1 } = {}) {
  if (result?.ok !== true) return { text: bounded(String(result?.safe_error ?? "Robert 命令失败，请查看本地诊断。")), isError: true };
  const quote = value => /^[A-Za-z0-9_./:@=-]+$/.test(value) ? value : "'" + value.replace(/'/g, "'\\''") + "'";
  const local = (input === undefined ? "" : `printf '%s' ${quote(JSON.stringify(input))} | `)
    + "robert " + args.map(quote).join(" ");
  if (jsonOutput) {
    const json = JSON.stringify(result, null, 2);
    return { text: json.length <= MAX_REPLY ? json : bounded(json, `完整 JSON：本地执行 ${local}`) };
  }
  let text, footer;
  if (args[0] === "status") {
    text = statusText(result.data ?? {});
    footer = "诊断 JSON：/robert-status --json";
  } else if (args[0] === "task") {
    text = taskText(result.data ?? {});
    footer = `/robert-task ${args[2]} --json`;
  } else if (args[0] === "run") {
    text = runText(result.data ?? {});
    footer = `/robert-run ${args[2]} --json`;
  } else if (args[0] === "artifact") {
    const artifact = result.data ?? {};
    text = `产物 ${artifact.artifact_type} · 任务 ${artifact.task_id}\n\n`
      + (artifact.missing ? "产物文件不可用，请检查本地文件。" : String(artifact.content ?? ""))
      + (artifact.truncated ? "\n…（产物预览已截断）" : "");
    footer = `完整产物：本地执行 ${local}`;
  } else if (args[1] === "inbox") {
    ({ text, footer } = inboxText(result, page));
  } else if (args[1] === "dispatch") text = dispatchText(result);
  else if (args[1] === "respond") text = responseText(result, input);
  else text = "Robert 已收到请求。";
  return { text: bounded(text, footer) };
}
