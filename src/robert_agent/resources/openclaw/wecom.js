import { randomUUID } from "node:crypto";

// The bundled WeCom channel exposes the connected bot through this shared state
// in OpenClaw 2026.10.3-t.8. Read it only; never create or replace channel state.
const sharedStateKey = Symbol.for("wecom-openclaw-plugin:shared-state:v1");
const commandPattern = /^\/robert-respond ([ad][A-Za-z0-9_-]{22}):(\d+):([1-5x])$/;

function questionCommand(command) {
  if (typeof command !== "string") return;
  const match = command.match(commandPattern);
  if (!match || !Number.isSafeInteger(Number(match[2]))) return;
  const encoded = match[1].slice(1);
  const identifier = Buffer.from(encoded, "base64url");
  if (identifier.length !== 16 || identifier.toString("base64url") !== encoded) return;
  return { args: command.slice("/robert-respond ".length),
    selected: match[3], taskId: `robert_${match[1]}_${match[2]}` };
}

function questionCard(params) {
  const blocks = params.presentation?.blocks;
  if (!Array.isArray(blocks) || blocks.length !== 1 || blocks[0].type !== "buttons")
    throw new Error("Invalid Robert question buttons.");
  const buttons = blocks[0].buttons;
  if (!Array.isArray(buttons) || buttons.length < 2 || buttons.length > 6)
    throw new Error("Invalid Robert question options.");
  const previewId = params.preview === true ? randomUUID().replaceAll("-", "") : undefined;
  let taskId;
  const buttonList = buttons.map((button, index) => {
    if (typeof button.label !== "string" || !button.label.trim())
      throw new Error("Invalid Robert question label.");
    const binding = button.action?.type === "command" && questionCommand(button.action.command);
    if (!previewId && (!binding || binding.selected !== (index === buttons.length - 1 ? "x" : String(index + 1))
        || taskId && taskId !== binding.taskId))
      throw new Error("Invalid Robert question reference.");
    taskId = previewId ? `robert_test_${previewId}` : binding.taskId;
    return { text: index === buttons.length - 1 ? "自行填写" : `方案 ${index + 1}${index === 0 ? "（推荐）" : ""}`,
      style: index === 0 ? 1 : 4, key: previewId
        ? `robert-test:${previewId}:${index === buttons.length - 1 ? "x" : index + 1}` : button.action.command };
  });
  return { card_type: "button_interaction", task_id: previewId ? taskId
      : `${taskId}_${randomUUID().replaceAll("-", "").slice(0, 12)}`,
    main_title: { title: previewId ? "Robert：交互测试" : "Robert：请选择方案",
      desc: previewId ? "点击仅回显测试提示，不提交业务答复" : "完整说明与选项见通知正文" },
    sub_title_text: buttons.slice(0, -1).map((button, index) => `${index + 1}. ${button.label}`).join("\n"),
    button_list: buttonList, card_action: { type: 0 } };
}

async function sendQuestionCard(connection, target, card) {
  if (!connection?.isConnected || typeof connection.sendMessage !== "function")
    return { status: "failed" }; // No send was attempted; retry is safe.
  try {
    const result = await connection.sendMessage(target, { msgtype: "template_card", template_card: card });
    if (Number.isInteger(result?.errcode) && result.errcode !== 0) return { status: "failed" };
    const messageId = result?.headers?.req_id;
    return typeof messageId === "string" && messageId.length > 0 && messageId.length <= 256
      ? { status: "sent", messageId } : { status: "unknown" };
  } catch (error) {
    // A protocol rejection proves failure. A timeout/disconnect does not prove
    // that the remote card was absent, so it cannot authorize a duplicate.
    return { status: Number.isInteger(error?.errcode) && error.errcode !== 0 ? "failed" : "unknown" };
  }
}

export function registerWeComQuestionTransport(api) {
  api.registerGatewayMethod?.("robert.question.send", async ({ params, respond }) => {
    let dispatched = false;
    try {
      if (!params || params.channel !== "wecom" || typeof params.account !== "string" || !params.account
          || typeof params.target !== "string" || !params.target || typeof params.text !== "string"
          || !params.text || params.preview !== undefined && typeof params.preview !== "boolean"
          || params.phase !== undefined && !["text", "card"].includes(params.phase)
          || params.phase === "card" && (typeof params.messageId !== "string"
            || !params.messageId || params.messageId.length > 256)
          || Buffer.byteLength(JSON.stringify(params), "utf8") > 65536)
        throw new Error("Invalid Robert question delivery.");
      const card = questionCard(params);
      const text = params.preview ? `【交互测试】点击仅回显测试提示，不提交业务答复。\n\n${params.text}` : params.text;
      const connection = globalThis[sharedStateKey]?.wsClientInstances?.get(params.account);
      const target = params.target.replace(/^wecom:/i, "").replace(/^group:/i, "");
      if (params.phase === "card") {
        const result = await sendQuestionCard(connection, target, card);
        respond(true, { ok: true, mode: "card", messageId: params.messageId, card: result });
        return;
      }
      const adapter = await api.runtime.channel.outbound.loadAdapter("wecom");
      if (!adapter?.sendText) throw new Error("WeCom message transport is unavailable.");
      if (!connection?.isConnected || typeof connection.sendMessage !== "function") {
        dispatched = true;
        const result = await adapter.sendText({ cfg: api.config, accountId: params.account,
          to: params.target, threadId: params.thread || undefined, text });
        if (!result?.messageId) throw new Error("WeCom did not return a delivery receipt.");
        respond(true, { ok: true, mode: "text", messageId: result.messageId, card: { status: "skipped" } });
        return;
      }
      dispatched = true;
      const notice = await adapter.sendText({ cfg: api.config, accountId: params.account,
        to: params.target, threadId: params.thread || undefined, text });
      if (!notice?.messageId) throw new Error("WeCom did not return a delivery receipt.");
      const result = params.phase === "text" ? { status: "pending" }
        : await sendQuestionCard(connection, target, card);
      respond(true, { ok: true, mode: params.phase === "text" ? "text" : "card",
        messageId: notice.messageId, card: result });
    } catch {
      respond(false, undefined, { code: "UNAVAILABLE",
        message: dispatched ? "Robert question delivery failed; inspect local delivery diagnostics."
          : "Robert question transport is unavailable or the request is invalid." });
    }
  }, { scope: "operator.write" });
}

export async function handleWeComQuestionCallback(event, host, handler) {
  const ctx = event.ctx;
  if (!ctx || ctx.CommandAuthorized !== true || event.isTailDispatch || event.sendPolicy === "deny"
      || event.suppressUserDelivery || (ctx.Surface ?? ctx.Provider) !== "wecom"
      || ctx.MediaPath || ctx.MediaPaths?.length) return;
  const frame = ctx.WeComFrame?.body;
  if (frame?.msgtype !== "event" || frame.event?.eventtype !== "template_card_event") return;
  const callback = frame.event.template_card_event;
  const preview = typeof callback?.event_key === "string"
    && callback.event_key.match(/^robert-test:([a-f0-9]{32}):([1-5x])$/);
  let reply;
  if (preview) {
    if (callback.task_id !== `robert_test_${preview[1]}`) return;
    reply = { text: `测试点击已收到：${preview[2] === "x" ? "自行填写" : `方案 ${preview[2]}`}。未提交业务答复。` };
  } else {
    const binding = questionCommand(callback?.event_key);
    if (!binding || typeof callback.task_id !== "string"
        || !callback.task_id.startsWith(binding.taskId + "_")
        || !/^[a-f0-9]{12}$/.test(callback.task_id.slice(binding.taskId.length + 1))) return;
    reply = await handler({ isAuthorizedSender: true, channelId: "wecom",
      accountId: ctx.AccountId ?? "", from: ctx.From ?? "", senderId: ctx.SenderId ?? "",
      messageThreadId: ctx.MessageThreadId, args: binding.args });
  }
  const queuedFinal = host.dispatcher.sendFinalReply(reply);
  host.recordProcessed("completed", { reason: "robert_question_callback" });
  host.markIdle("message_completed");
  return { handled: true, queuedFinal, counts: host.dispatcher.getQueuedCounts() };
}
