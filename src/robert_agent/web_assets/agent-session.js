"use strict";

const AgentSessionView = (() => {
  function markdown(text, node) {
    const body = node("div", "session-markdown");
    function inline(host, value) {
      const pattern = /(`[^`\n]+`|\*\*[^*\n]+\*\*)/g;
      let start = 0;
      for (const match of value.matchAll(pattern)) {
        host.append(document.createTextNode(value.slice(start, match.index)));
        const code = match[0].startsWith("`");
        host.append(node(code ? "code" : "strong", "", match[0].slice(code ? 1 : 2, code ? -1 : -2)));
        start = match.index + match[0].length;
      }
      host.append(document.createTextNode(value.slice(start)));
    }
    const lines = String(text).split("\n");
    let code = null;
    let list = null;
    for (const line of lines) {
      if (/^\s*```/.test(line)) {
        if (code) code = null;
        else {
          code = node("code");
          const pre = node("pre", "session-code");
          pre.append(code);
          body.append(pre);
        }
        list = null;
        continue;
      }
      if (code) { code.textContent = (code.textContent || "") + line + "\n"; continue; }
      if (!line.trim()) { list = null; continue; }
      const bullet = line.match(/^\s*(?:[-*]|\d+\.)\s+(.*)$/);
      if (bullet) {
        if (!list) { list = node("ul"); body.append(list); }
        const entry = node("li");
        inline(entry, bullet[1]);
        list.append(entry);
      } else {
        list = null;
        const heading = line.match(/^(#{1,6})\s+(.*)$/);
        const paragraph = node(heading ? "h4" : "p");
        inline(paragraph, heading ? heading[2] : line);
        body.append(paragraph);
      }
    }
    return body;
  }

  function recordView(record, helpers, expanded) {
    const { node, t, formatTime } = helpers;
    const article = node("article", `session-record session-${record.kind}`);
    article.dataset.recordId = record.id;
    const header = node("div", "session-record-head");
    header.append(node("strong", "", t(`session_${record.kind}`)));
    if (record.timestamp) header.append(node("time", "", formatTime(record.timestamp)));
    if (record.status) header.append(node("span", `session-status ${record.status}`, t(record.status)));
    if (record.exit_code !== null && record.exit_code !== undefined)
      header.append(node("span", "session-status", `Exit ${record.exit_code}`));
    article.append(header);
    const rich = ["assistant", "user", "reasoning"].includes(record.kind);
    const viewText = text => rich ? markdown(text, node) : node("pre", "session-code", text);
    if (record.text.length > 800) {
      article.append(viewText(record.text.slice(0, 800) + "…"));
      const details = node("details", "session-expanded");
      details.append(node("summary", "", t("sessionExpand")));
      const key = `${record.id}:text`;
      const fill = () => {
        if (details.open) {
          expanded.add(key);
          if (details.children.length === 1) details.append(viewText(record.text));
        } else expanded.delete(key);
      };
      details.addEventListener("toggle", fill);
      details.open = expanded.has(key);
      fill();
      article.append(details);
    } else if (record.text) article.append(viewText(record.text));
    if (record.output) {
      const details = node("details", "session-expanded session-output");
      const key = `${record.id}:output`;
      details.append(node("summary", "", t("sessionOutput")));
      const fill = () => {
        if (details.open) {
          expanded.add(key);
          if (details.children.length === 1) details.append(node("pre", "session-code", record.output));
        } else expanded.delete(key);
      };
      details.addEventListener("toggle", fill);
      details.open = expanded.has(key);
      fill();
      article.append(details);
    }
    if (record.truncated) article.append(node("p", "notice", t("sessionTruncated")));
    return article;
  }

  function create(detail, helpers) {
    const { node, button, disclosure, t, formatTime, get } = helpers;
    const panel = disclosure("detail-agent-session", t("agentSession"), t("agentSessionWhy"), "");
    const controls = node("div", "session-controls");
    const field = node("label", "", t("sessionExecution"));
    const select = node("select");
    select.setAttribute("aria-label", t("sessionExecution"));
    field.append(select);
    const runField = node("label", "", t("sessionAgent"));
    const runSelect = node("select");
    runSelect.setAttribute("aria-label", t("sessionAgent"));
    runField.append(runSelect);
    runField.hidden = true;
    const refresh = button(t("refresh"), () => load({ reset: true }));
    controls.append(field, runField, refresh);
    const info = node("p", "session-info");
    const message = node("p", "notice");
    message.setAttribute("role", "status");
    const list = node("div", "session-records");
    const more = button(t("sessionOlder"), () => load({ older: true }));
    more.hidden = true;
    panel.body.append(controls, info, message, list, more);
    let executions = [];
    let selected = null;
    let items = [];
    let nextCursor = null;
    let request = 0;
    let inFlight = null;
    let pendingLoad = null;
    let loaded = false;
    let active = false;
    let activeRuns = false;
    let checkedAt = 0;
    let loadedFingerprint = null;
    let requestedFingerprint = null;
    let runId = "";
    let sourceIdentity = "";
    const expanded = new Set();

    function reset() {
      request++;
      pendingLoad = null;
      loaded = false;
      active = false;
      activeRuns = false;
      loadedFingerprint = null;
      items = [];
      nextCursor = null;
      expanded.clear();
      info.textContent = "";
      list.replaceChildren();
      more.hidden = true;
      refresh.disabled = false;
      message.textContent = t(executions.length ? "sessionOpenHint" : "sessionNoExecutions");
      message.hidden = false;
    }
    function render() {
      list.replaceChildren(...items.map(item => recordView(item, helpers, expanded)));
      more.hidden = !nextCursor;
    }
    function fingerprint() {
      return JSON.stringify([selected, runId]);
    }
    function pollDue() {
      // An active transcript may grow with no attempt/heartbeat change. An
      // inactive one still gets a bounded recheck for late output and runs.
      const interval = active || activeRuns || selected?.status === "running" ? 15000 : 60000;
      return !loaded || fingerprint() !== loadedFingerprint || Date.now() - checkedAt >= interval;
    }
    function load({ older = false, reset: refreshAll = false, poll = false } = {}) {
      if (!selected || (older && !nextCursor)) return Promise.resolve();
      if (inFlight) {
        if (!poll || fingerprint() !== requestedFingerprint) {
          pendingLoad = {
            older: older && !refreshAll && !(pendingLoad?.reset),
            reset: refreshAll || (pendingLoad?.reset ?? false),
          };
        }
        return inFlight;
      }
      inFlight = (async () => {
        let options = { older, reset: refreshAll };
        do {
          pendingLoad = null;
          await readPage(options);
          options = pendingLoad;
        } while (options && selected);
      })().finally(() => { inFlight = null; });
      return inFlight;
    }
    async function readPage({ older = false, reset: refreshAll = false } = {}) {
      const token = ++request;
      const execution = selected;
      const evidence = fingerprint();
      requestedFingerprint = evidence;
      refresh.disabled = true;
      more.disabled = true;
      message.textContent = t("sessionLoading");
      message.hidden = false;
      const query = new URLSearchParams({ task_id: execution.task_id, attempt_id: execution.attempt_id, limit: "30" });
      if (runId) query.set("run_id", runId);
      if (older) query.set("cursor", nextCursor);
      try {
        const page = await get(`/api/agent-session?${query}`);
        if (token !== request) return;
        const identity = `${page.source}:${page.session_id || ""}`;
        if (sourceIdentity && sourceIdentity !== identity) {
          items = [];
          expanded.clear();
        }
        sourceIdentity = identity;
        runSelect.replaceChildren(
          Object.assign(node("option", "", t("sessionMain")), { value: "" }),
          ...(page.runs || []).map(run => Object.assign(node("option", "", `${run.stage} · ${run.worker} · ${t(run.status === "succeeded" ? "sessionSucceeded" : run.status)}`), { value: run.run_id })),
        );
        runSelect.value = runId;
        runField.hidden = !(page.runs || []).length;
        const fresh = page.items || [];
        if (older) {
          const known = new Set(items.map(item => item.id));
          items.push(...fresh.filter(item => !known.has(item.id)));
          nextCursor = page.next_cursor;
        } else if (refreshAll || !items.length) {
          items = fresh;
          nextCursor = page.next_cursor;
        } else {
          const overlap = fresh.some(item => items.some(previous => previous.id === item.id));
          if (overlap) {
            const known = new Set(fresh.map(item => item.id));
            items = [...fresh, ...items.filter(item => !known.has(item.id))];
          } else {
            items = fresh;
            nextCursor = page.next_cursor;
          }
        }
        active = page.active;
        activeRuns = (page.runs || []).some(run => ["running", "starting"].includes(run.status));
        loaded = true;
        checkedAt = Date.now();
        loadedFingerprint = evidence;
        info.textContent = [page.session_id ? `Session ${page.session_id}` : "", t("sessionNewestFirst"),
          active ? t("sessionLive") : ""].filter(Boolean).join(" · ");
        message.textContent = page.limited ? t("sessionLimited") : items.length ? "" : t(page.status === "unavailable" ? "sessionUnavailable" : "sessionEmpty");
        message.hidden = !message.textContent;
        render();
      } catch (error) {
        if (token === request) {
          message.textContent = error.message;
          message.hidden = false;
        }
      } finally {
        if (token === request) {
          refresh.disabled = false;
          more.disabled = false;
        }
      }
    }
    panel.update = (updated, { poll = false } = {}) => {
      executions = (updated.tasks || []).flatMap(task => (task.attempts || []).map(attempt => ({
        ...attempt, task_id: task.task_id, stage: task.expected_output || task.route_id,
        runEvidence: [task.review, task.pre_pr_review, task.vote],
      }))).sort((a, b) => String(b.started_at || "").localeCompare(String(a.started_at || "")) || b.attempt_no - a.attempt_no);
      const previousId = selected?.attempt_id;
      selected = executions.find(execution => execution.attempt_id === previousId) || executions[0] || null;
      select.replaceChildren(...executions.map(execution => {
        const option = node("option", "", `${t(execution.stage)} · ${t("attempt", { number: execution.attempt_no })} · ${t(execution.status)} · ${formatTime(execution.started_at)}`);
        option.value = execution.attempt_id;
        return option;
      }));
      select.value = selected?.attempt_id || "";
      select.disabled = !selected;
      if (selected?.attempt_id !== previousId) {
        runId = "";
        sourceIdentity = "";
        runField.hidden = true;
        reset();
      }
      if (panel.open && selected && (!loaded || (poll && pollDue()))) load({ poll: true });
    };
    Object.defineProperties(panel, {
      pollingActive: { get: () => panel.open && (active || activeRuns || selected?.status === "running") },
      pollDelay: { get: () => panel.open && selected
        ? Math.max(0, (active || activeRuns || selected.status === "running" ? 15000 : 60000) - (Date.now() - checkedAt))
        : Infinity },
    });
    select.addEventListener("change", () => {
      selected = executions.find(execution => execution.attempt_id === select.value);
      runId = "";
      sourceIdentity = "";
      runField.hidden = true;
      reset();
      if (panel.open) load();
    });
    runSelect.addEventListener("change", () => {
      runId = runSelect.value;
      sourceIdentity = "";
      reset();
      if (panel.open) load();
    });
    panel.addEventListener("toggle", () => { if (panel.open && selected && pollDue()) load({ poll: true }); });
    panel.update(detail);
    return panel;
  }
  return { create, markdown, recordView };
})();
