"use strict";

const filter = document.getElementById("proposal-filter");
const message = document.getElementById("proposal-message");
const list = document.getElementById("proposal-list");
const detail = document.getElementById("proposal-detail");

const statusLabel = {
  pending: "待决策", held: "暂缓", discarded: "已放弃",
  approved: "已批准，待受理", admitted: "已受理", blocked: "待复核",
};

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
}

async function request(path, options) {
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.safe_error || "读取请求失败。");
  return data;
}

function sourceLink(value) {
  try {
    const url = new URL(value);
    if (url.protocol !== "https:" || url.username || url.password) return null;
    const link = node("a", "查看原单据");
    link.href = url.href;
    return link;
  } catch {
    return null;
  }
}

function showDetail(proposal) {
  detail.replaceChildren(
    node("h2", proposal.title),
    node("p", [proposal.proposal_id, "v" + proposal.revision, statusLabel[proposal.status]].join(" · "), "proposal-meta"),
  );
  const source = sourceLink(proposal.source_url);
  if (source) detail.append(source);
  if (proposal.blocked_reason) detail.append(node("p", proposal.blocked_reason));
  const report = proposal.report;
  for (const [field, label] of Object.entries({
    summary: "建议", scope: "处理范围", exclusions: "排除项", verification: "验收", risks: "风险",
  })) {
    if (report[field]) detail.append(node("h3", label), node("p", report[field]));
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
    detail.append(node("h3", "代码证据"), evidence);
  }
  if (proposal.decisions?.length) {
    const history = node("ul");
    for (const decision of proposal.decisions) {
      history.append(node("li", [decision.actor, decision.action, decision.reason, decision.created_at].filter(Boolean).join(" · ")));
    }
    detail.append(node("h3", "决策记录"), history);
  }
  if (proposal.work) {
    detail.append(node("h3", "执行进度"), node("p", ({
      queued: "已入队", running: "运行中", publishing: "发布中", waiting_for_user: "待澄清",
      in_review: "等待 PR 评审", pr_merged: "PR 已合并", pr_closed: "PR 已关闭",
      completed: "已完成", canceled: "已取消", failed: "执行失败",
    }[proposal.work.state] || proposal.work.state || "")));
    if (proposal.work.pr?.url) {
      const pr = sourceLink(proposal.work.pr.url);
      if (pr) {
        pr.textContent = "查看关联 PR";
        detail.append(pr);
      }
    }
    const board = node("a", "查看工作看板");
    board.href = "/board?item=" + encodeURIComponent(proposal.work_item_id);
    detail.append(node("p"));
    detail.lastChild.append(board);
  }
  if (proposal.feedback?.length) {
    const receipts = node("ul");
    for (const receipt of proposal.feedback) {
      const label = {created: "新提案", admitted: "受理", operator_question: "澄清", execution_failed: "待检查",
        completed: "完成", pr_opened: "PR 待评审", pr_merged: "PR 合并", unmerged_pr_closed: "PR 关闭"}[receipt.event];
      const status = {pending: "待投递", sent: "已回评", failed: "投递失败", skipped: "已跳过"}[receipt.status];
      receipts.append(node("li", [label, status].filter(Boolean).join(" · ")));
    }
    detail.append(node("h3", "单据回评"), receipts);
  }
  detail.hidden = false;
}

function showActions(proposal, session) {
  if (!session?.proposal_enabled || !session.writes_enabled || !session.allowed_repo_ids.includes(proposal.repo_id)) return;
  const choices = proposal.status === "pending" ? {approve: "处理", hold: "暂缓", discard: "放弃"} :
    proposal.status === "held" ? {approve: "处理", discard: "放弃"} :
    proposal.status === "blocked" ? {discard: "放弃"} : {};
  if (!Object.keys(choices).length) return;
  const actions = node("div", undefined, "proposal-actions");
  const reason = node("input");
  reason.placeholder = "备注（可选）";
  reason.setAttribute("aria-label", "决策备注");
  const feedback = node("p");
  feedback.setAttribute("role", "status");
  actions.append(reason);
  const requestKeys = new Map();
  for (const [action, label] of Object.entries(choices)) {
    const button = node("button", label);
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
        feedback.textContent = error.message;
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
      const back = node("a", "返回提案列表");
      back.href = "/proposals";
      message.append(back);
      return;
    }
    const status = filter.value;
    const data = await request("/api/proposals?status=" + encodeURIComponent(status));
    if (filter.value !== status) return;
    const entries = data.proposals.map(proposal => {
      const entry = node("li");
      const link = node("a", proposal.title);
      link.href = "/proposals?id=" + encodeURIComponent(proposal.proposal_id);
      entry.append(link, node("p", [proposal.repository, statusLabel[proposal.status], proposal.created_at].join(" · "), "proposal-meta"));
      if (proposal.report.summary) entry.append(node("p", proposal.report.summary));
      return entry;
    });
    list.replaceChildren(...entries);
    message.textContent = entries.length ? "" : "该状态暂无提案。";
  } catch (error) {
    message.textContent = error.message;
  }
}

if (new URLSearchParams(window.location.search).has("id")) filter.closest("label").hidden = true;
filter.addEventListener("change", load);
load();
