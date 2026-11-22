"use strict";

const filter = document.getElementById("proposal-filter");
const message = document.getElementById("proposal-message");
const list = document.getElementById("proposal-list");
const detail = document.getElementById("proposal-detail");

const labels = {
  en: {
    status: "Status", pending: "Needs decision", held: "On hold", discarded: "Discarded",
    approved: "Approved, awaiting admission", admitted: "Admitted", blocked: "Needs review", all: "All",
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
  if (!session?.proposal_enabled || !session.writes_enabled || !session.allowed_repo_ids.includes(proposal.repo_id)) return;
  const choices = {
    pending: ["approve", "hold", "discard"],
    held: ["approve", "discard"],
    blocked: ["discard"],
  }[proposal.status];
  if (!choices) return;
  const actions = node("div", undefined, "proposal-actions");
  const reason = node("input");
  reason.dataset.i18nPlaceholder = "optionalReason";
  reason.dataset.i18nAria = "reason";
  const feedback = node("p");
  feedback.setAttribute("role", "status");
  actions.append(reason);
  const requestKeys = new Map();
  for (const action of choices) {
    const button = translatedNode("button", `action_${action}`);
    button.className = action === "approve" ? "shell-button primary" : "shell-button";
    button.type = "button";
    button.addEventListener("click", async () => {
      for (const item of actions.querySelectorAll("button")) item.disabled = true;
      const body = JSON.stringify({action, expected_version: proposal.version, reason: reason.value});
      try {
        if (!requestKeys.has(body)) requestKeys.set(body, crypto.randomUUID());
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
  if (proposal.status === "approved" || (proposal.status === "admitted" && (
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
