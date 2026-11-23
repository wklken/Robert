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
  let taskId;
  const buttonList = buttons.map((button, index) => {
    const binding = button.action?.type === "command" && questionCommand(button.action.command);
    if (!binding || binding.selected !== (index === buttons.length - 1 ? "x" : String(index + 1))
        || taskId && taskId !== binding.taskId || typeof button.label !== "string" || !button.label.trim())
      throw new Error("Invalid Robert question reference.");
    taskId = binding.taskId;
    return { text: index === buttons.length - 1 ? "自行填写" : `方案 ${index + 1}${index === 0 ? "（推荐）" : ""}`,
      style: index === 0 ? 1 : 4, key: button.action.command };
  });
  return { card_type: "button_interaction", task_id: `${taskId}_${randomUUID().replaceAll("-", "").slice(0, 12)}`,
    main_title: { title: "Robert：请选择方案", desc: "完整说明与选项见通知正文" },
    sub_title_text: buttons.slice(0, -1).map((button, index) => `${index + 1}. ${button.label}`).join("\n"),
    button_list: buttonList, card_action: { type: 0 } };
}

export function registerWeComQuestionTransport(api) {
  api.registerGatewayMethod?.("robert.question.send", async ({ params, respond }) => {
    let dispatched = false;
    try {
      if (!params || params.channel !== "wecom" || typeof params.account !== "string" || !params.account
          || typeof params.target !== "string" || !params.target || typeof params.text !== "string"
          || !params.text || Buffer.byteLength(JSON.stringify(params), "utf8") > 65536)
        throw new Error("Invalid Robert question delivery.");
      const card = questionCard(params);
      const adapter = await api.runtime.channel.outbound.loadAdapter("wecom");
      if (!adapter?.sendText) throw new Error("WeCom message transport is unavailable.");
      const connection = globalThis[sharedStateKey]?.wsClientInstances?.get(params.account);
      if (!connection?.isConnected || typeof connection.sendMessage !== "function") {
        dispatched = true;
        const result = await adapter.sendText({ cfg: api.config, accountId: params.account,
          to: params.target, threadId: params.thread || undefined, text: params.text });
        if (!result?.messageId) throw new Error("WeCom did not return a delivery receipt.");
        respond(true, { ok: true, mode: "text", messageId: result.messageId });
        return;
      }
      const target = params.target.replace(/^wecom:/i, "").replace(/^group:/i, "");
      dispatched = true;
      const notice = await adapter.sendText({ cfg: api.config, accountId: params.account,
        to: params.target, threadId: params.thread || undefined, text: params.text });
      if (!notice?.messageId) throw new Error("WeCom did not return a delivery receipt.");
      const result = await connection.sendMessage(target, { msgtype: "template_card",
        template_card: card });
      if (!result?.headers?.req_id) throw new Error("WeCom did not return a delivery receipt.");
      respond(true, { ok: true, mode: "card", messageId: result.headers.req_id });
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
  const binding = questionCommand(callback?.event_key);
  if (!binding || typeof callback.task_id !== "string"
      || !callback.task_id.startsWith(binding.taskId + "_")
      || !/^[a-f0-9]{12}$/.test(callback.task_id.slice(binding.taskId.length + 1))) return;
  const reply = await handler({ isAuthorizedSender: true, channelId: "wecom",
    accountId: ctx.AccountId ?? "", from: ctx.From ?? "", senderId: ctx.SenderId ?? "",
    messageThreadId: ctx.MessageThreadId, args: binding.args });
  const queuedFinal = host.dispatcher.sendFinalReply(reply);
  host.recordProcessed("completed", { reason: "robert_question_callback" });
  host.markIdle("message_completed");
  return { handled: true, queuedFinal, counts: host.dispatcher.getQueuedCounts() };
}
