"use strict";

const filter = document.getElementById("proposal-filter");
const message = document.getElementById("proposal-message");
const list = document.getElementById("proposal-list");
const detail = document.getElementById("proposal-detail");

const labels = {
  en: {
    status: "Status", pending: "Needs decision", held: "On hold", discarded: "Discarded",
    approved: "Approved, awaiting admission", admitted: "Admitted", blocked: "Needs review", all: "All",
    today: "Today’s recommendations", investigating: "Investigating",
    priority: "Priority", recommendation: "Recommendation", feasibility: "Feasibility", analyzed_at: "Last evaluated",
    high: "High", medium: "Medium", low: "Low", ready: "Ready", investigate: "Investigation recommended",
    needs_clarification: "Needs clarification", unassessed: "Awaiting analysis",
    action_investigate: "Investigate", action_restore: "Restore", action_assign: "Evaluate selected repository",
    review_date: "Review again on", days: "Days to defer", repository_choice: "Repository", choose_repository: "Choose a repository",
    event_started: "Started", event_tag_sync: "Repository label", event_status_sync: "Ticket status",
    loading: "Loading proposals…", empty: "No proposals with this status.", back: "Back to proposals",
    loadFailed: "Couldn't load proposals.", decisionFailed: "Couldn't save the decision.", notFound: "Proposal not found.",
    source: "View source ticket", summary: "Recommendation", scope: "Scope", exclusions: "Exclusions",
    verification: "Acceptance checks", risks: "Risks", evidence: "Code evidence", decisions: "Decision history",
    execution: "Execution progress", relatedPR: "View linked PR", board: "View on board", feedback: "Ticket updates",
    reason: "Decision note", optionalReason: "Optional note",
    action_approve: "Approve", action_hold: "Hold", action_discard: "Discard",
    work_queued: "Queued", work_running: "Running", work_publishing: "Publishing", work_waiting_for_user: "Needs clarification",
    work_in_review: "Awaiting PR review", work_pr_merged: "PR merged", work_pr_closed: "PR closed",
    work_completed: "Completed", work_canceled: "Canceled", work_failed: "Failed",
    event_created: "Proposal created", event_admitted: "Admitted", event_operator_question: "Clarification",
    event_execution_failed: "Needs attention", event_completed: "Completed", event_pr_opened: "PR awaiting review",
    event_pr_merged: "PR merged", event_unmerged_pr_closed: "PR closed",
    receipt_pending: "Pending delivery", receipt_sent: "Posted", receipt_failed: "Delivery failed", receipt_skipped: "Skipped",
  },
  zh: {
    status: "状态", pending: "待决策", held: "暂缓", discarded: "已放弃",
    approved: "已批准，待受理", admitted: "已受理", blocked: "待复核", all: "全部",
    today: "今日推荐", investigating: "调查中",
    priority: "优先级", recommendation: "推荐程度", feasibility: "可行性", analyzed_at: "最后分析",
    high: "高", medium: "中", low: "低", ready: "可处理", investigate: "建议先调查",
    needs_clarification: "待澄清", unassessed: "待分析",
    action_investigate: "先调查", action_restore: "恢复", action_assign: "分析所选仓库",
    review_date: "重新评审日期", days: "延期天数", repository_choice: "仓库", choose_repository: "选择仓库",
    event_started: "已开工", event_tag_sync: "仓库标签", event_status_sync: "单据状态",
    loading: "正在读取提案…", empty: "该状态暂无提案。", back: "返回提案列表",
    loadFailed: "无法读取提案。", decisionFailed: "无法保存决策。", notFound: "找不到该提案。",
    source: "查看原单据", summary: "建议", scope: "处理范围", exclusions: "排除项",
    verification: "验收", risks: "风险", evidence: "代码证据", decisions: "决策记录",
    execution: "执行进度", relatedPR: "查看关联 PR", board: "查看工作看板", feedback: "单据回评",
    reason: "决策备注", optionalReason: "备注（可选）",
    action_approve: "处理", action_hold: "暂缓", action_discard: "放弃",
    work_queued: "已入队", work_running: "运行中", work_publishing: "发布中", work_waiting_for_user: "待澄清",
    work_in_review: "等待 PR 评审", work_pr_merged: "PR 已合并", work_pr_closed: "PR 已关闭",
    work_completed: "已完成", work_canceled: "已取消", work_failed: "执行失败",
    event_created: "新提案", event_admitted: "受理", event_operator_question: "澄清",
    event_execution_failed: "待检查", event_completed: "完成", event_pr_opened: "PR 待评审",
    event_pr_merged: "PR 合并", event_unmerged_pr_closed: "PR 关闭",
    receipt_pending: "待投递", receipt_sent: "已回评", receipt_failed: "投递失败", receipt_skipped: "已跳过",
  },
};

let language;

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
}

function translatedNode(tag, key, fallback = "") {
  const element = node(tag, fallback);
  if (Object.hasOwn(labels.en, key)) element.dataset.i18n = key;
  return element;
}

function appendJoined(element, ...parts) {
  parts.filter(Boolean).forEach((part, index) => {
    if (index) element.append(" · ");
    element.append(part);
  });
  return element;
}

function translate() {
  RobertShell.translate(language, labels[language]);
}

function showMessage(target, key, diagnostic) {
  target.replaceChildren(translatedNode("span", key));
  if (diagnostic) target.append(" ", diagnostic);
  translate();
}

async function request(path, options) {
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) {
    const error = new Error(data.safe_error || "");
    error.status = response.status;
    throw error;
  }
  return data;
}

function sourceLink(value, label) {
  try {
    const url = new URL(value);
    if (url.protocol !== "https:" || url.username || url.password) return null;
    const link = translatedNode("a", label);
    link.href = url.href;
    return link;
  } catch {
    return null;
  }
}

function showAssessment(parent, report) {
  const facts = [];
  for (const [key, value] of [
    ["feasibility", report.feasibility],
    ["priority", report.source_priority || report.priority],
    ["recommendation", report.recommendation],
    ["analyzed_at", report.analyzed_at ? new Date(report.analyzed_at).toLocaleString(language) : null],
  ]) {
    if (!value) continue;
    const fact = node("span");
    fact.append(translatedNode("span", key), " ", translatedNode("strong", value, value));
    facts.push(fact);
  }
  if (facts.length) parent.append(appendJoined(node("p", undefined, "proposal-meta"), ...facts));
  if (report.recommendation_reason && report.recommendation_reason !== report.summary) {
    parent.append(node("p", report.recommendation_reason));
  }
}

function showDetail(proposal) {
  detail.replaceChildren(
    node("h2", proposal.title),
    appendJoined(node("p", undefined, "proposal-meta"), proposal.proposal_id, `v${proposal.revision}`,
      translatedNode("span", proposal.status, proposal.status)),
  );
  const source = sourceLink(proposal.source_url, "source");
  if (source) detail.append(source);
  if (proposal.blocked_reason) detail.append(node("p", proposal.blocked_reason));
  const report = proposal.report;
  showAssessment(detail, report);
  if (report.routing_reason) detail.append(node("p", report.routing_reason));
  if (proposal.defer_until) {
    const review = node("p");
    review.append(translatedNode("span", "review_date"), " ", new Date(proposal.defer_until).toLocaleDateString(language));
    detail.append(review);
  }
  for (const field of ["summary", "scope", "exclusions", "verification", "risks"]) {
    if (report[field]) detail.append(translatedNode("h3", field), node("p", report[field]));
  }
  if (report.commit) {
    const commit = node("p");
    commit.append(node("code", report.commit));
    detail.append(commit);
  }
  if (report.evidence?.length) {
    const evidence = node("ul");
    for (const item of report.evidence) {
      const entry = node("li");
      entry.append(node("code", item.path + ":" + item.line), document.createTextNode(" " + item.reason));
      evidence.append(entry);
    }
    detail.append(translatedNode("h3", "evidence"), evidence);
  }
  if (proposal.decisions?.length) {
    const history = node("ul");
    for (const decision of proposal.decisions) {
      history.append(appendJoined(node("li"), decision.actor,
        translatedNode("span", `action_${decision.action}`, decision.action), decision.reason, decision.created_at));
    }
    detail.append(translatedNode("h3", "decisions"), history);
  }
  if (proposal.work) {
    detail.append(translatedNode("h3", "execution"),
      translatedNode("p", `work_${proposal.work.state}`, proposal.work.state));
    const pr = sourceLink(proposal.work.pr?.url, "relatedPR");
    if (pr) detail.append(pr);
    const board = translatedNode("a", "board");
    board.href = "/board?item=" + encodeURIComponent(proposal.work_item_id);
    detail.append(node("p"));
    detail.lastChild.append(board);
  }
  if (proposal.feedback?.length) {
    const receipts = node("ul");
    for (const receipt of proposal.feedback) {
      receipts.append(appendJoined(node("li"),
        translatedNode("span", `event_${receipt.event}`, receipt.event),
        translatedNode("span", `receipt_${receipt.status}`, receipt.status)));
    }
    detail.append(translatedNode("h3", "feedback"), receipts);
  }
  detail.hidden = false;
}

function showActions(proposal, session) {
  if (!session?.proposal_enabled || !session.writes_enabled) return;
  if (proposal.repo_id && !session.allowed_repo_ids.includes(proposal.repo_id)) return;
  const repositoryCandidates = proposal.repository_candidates || [];
  const choices = {
    pending: ["approve", "investigate", "hold", "discard"],
    held: ["approve", "investigate", "hold", "restore", "discard"],
    blocked: ["assign", "hold", "discard"],
    discarded: ["restore"],
  }[proposal.status];
  if (!choices) return;
  const actions = node("div", undefined, "proposal-actions");
  const days = node("input");
  days.type = "number";
  days.min = "1";
  days.max = "365";
  days.value = "3";
  const daysLabel = node("label");
  daysLabel.append(translatedNode("span", "days"), days);
  if (choices.includes("hold")) actions.append(daysLabel);
  const repository = node("select");
  if (repositoryCandidates.length) {
    const empty = translatedNode("option", "choose_repository");
    empty.value = "";
    repository.append(empty);
    for (const entry of repositoryCandidates) {
      const option = node("option", `${entry.repository} (${entry.label})`);
      option.value = entry.label;
      repository.append(option);
    }
    repository.value = proposal.report.suggested_label || "";
    const repositoryLabel = node("label");
    repositoryLabel.append(translatedNode("span", "repository_choice"), repository);
    actions.append(repositoryLabel);
    if (!choices.includes("assign") && proposal.status !== "discarded") choices.unshift("assign");
  }
  const reason = node("input");
  reason.dataset.i18nPlaceholder = "optionalReason";
  reason.dataset.i18nAria = "reason";
  const feedback = node("p");
  feedback.setAttribute("role", "status");
  actions.append(reason);
  const requestKeys = new Map();
  for (const action of choices) {
    if (action === "assign" && !repositoryCandidates.length) continue;
    if (action === "approve" && ["investigate", "unassessed", "needs_clarification"].includes(proposal.report.feasibility)) continue;
    if (action === "investigate" && ["unassessed", "needs_clarification"].includes(proposal.report.feasibility)) continue;
    const button = translatedNode("button", `action_${action}`);
    button.className = action === "approve" ? "shell-button primary" : "shell-button";
    button.type = "button";
    button.addEventListener("click", async () => {
      if (action === "hold" && !days.reportValidity()) return;
      if (["approve", "investigate", "assign"].includes(action) && repositoryCandidates.length && !repository.value) {
        repository.focus();
        return;
      }
      for (const item of actions.querySelectorAll("button")) item.disabled = true;
      const requestBody = {action, expected_version: proposal.version, reason: reason.value};
      if (action === "hold") requestBody.defer_days = Number(days.value);
      if (["approve", "investigate", "assign"].includes(action) && repository.value) {
        requestBody.repository_label = repository.value;
      }
      const body = JSON.stringify(requestBody);
      try {
        if (!requestKeys.has(body)) requestKeys.set(body, Array.from(crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2, "0")).join(""));
        const updated = await request("/api/proposals/" + encodeURIComponent(proposal.proposal_id) + "/decisions", {
          method: "POST",
          headers: {"Content-Type": "application/json", "X-Robert-CSRF-Token": session.csrf_token, "X-Idempotency-Key": requestKeys.get(body)},
          body,
        });
        render(updated, session);
      } catch (error) {
        showMessage(feedback, "decisionFailed", error.status && error.message);
        for (const item of actions.querySelectorAll("button")) item.disabled = false;
      }
    });
    actions.append(button);
  }
  detail.append(actions, feedback);
}

let refresh;
function render(proposal, session) {
  clearTimeout(refresh);
  showDetail(proposal);
  showActions(proposal, session);
  translate();
  if (["approved", "investigating"].includes(proposal.status) || (proposal.status === "admitted" && (
    !proposal.work?.terminal ||
    proposal.feedback?.some(receipt => receipt.status === "pending")
  ))) {
    refresh = setTimeout(load, 8000);
  }
}

async function load() {
  const id = new URLSearchParams(window.location.search).get("id");
  try {
    if (id) {
      const [proposal, session] = await Promise.all([
        request("/api/proposals/" + encodeURIComponent(id)),
        request("/api/session").catch(() => null),
      ]);
      render(proposal, session);
      message.replaceChildren();
      const back = translatedNode("a", "back");
      back.href = "/proposals";
      message.removeAttribute("data-i18n");
      message.append(back);
      translate();
      return;
    }
    const status = filter.value;
    const data = await request("/api/proposals?status=" + encodeURIComponent(status));
    if (filter.value !== status) return;
    const entries = data.proposals.map(proposal => {
      const entry = node("li");
      const link = node("a", proposal.title);
      link.href = "/proposals?id=" + encodeURIComponent(proposal.proposal_id);
      entry.append(link, appendJoined(node("p", undefined, "proposal-meta"), proposal.repository,
        translatedNode("span", proposal.status, proposal.status), proposal.created_at));
      if (proposal.report.summary) entry.append(node("p", proposal.report.summary));
      showAssessment(entry, proposal.report);
      return entry;
    });
    list.replaceChildren(...entries);
    message.removeAttribute("data-i18n");
    message.replaceChildren();
    if (!entries.length) message.append(translatedNode("span", "empty"));
    translate();
  } catch (error) {
    message.removeAttribute("data-i18n");
    showMessage(message, id && error.status === 404 ? "notFound" : "loadFailed", error.status && error.message);
  }
}

if (new URLSearchParams(window.location.search).has("id")) filter.closest("label").hidden = true;
filter.addEventListener("change", load);
RobertShell.bindPreferences((choice) => {
  language = choice;
  document.title = choice === "zh" ? "Robert · 提案" : "Robert · Proposals";
  translate();
});
load();
