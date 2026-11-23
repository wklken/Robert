"""Exercise native WeCom delivery and authenticated callback ingress without a model."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from robert_agent.integrations.openclaw import write_plugin


@unittest.skipUnless(shutil.which("node"), "Node is required to execute the OpenClaw plugin")
class OpenClawWeComTests(unittest.TestCase):
    def test_native_card_and_structured_callback_preserve_the_bound_question(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_plugin(root / "plugin")
            package = root / "plugin/node_modules/openclaw"
            package.mkdir(parents=True)
            (package / "package.json").write_text(
                json.dumps(
                    {
                        "type": "module",
                        "exports": {"./plugin-sdk/plugin-entry": "./entry.js"},
                    }
                )
            )
            (package / "entry.js").write_text("export const definePluginEntry = p => p;")
            binary = root / "bin"
            binary.mkdir()
            executable = binary / "robert-custom"
            executable.write_text(
                f"#!{sys.executable}\n"
                "import json,sys,os\nr=json.load(sys.stdin)\n"
                "with open(os.environ['ROBERT_TEST_REQUESTS'],'a') as out: out.write(json.dumps(r)+'\\n')\n"
                "print(json.dumps({'ok':True,'status':'accepted','request':r}))\n"
            )
            executable.chmod(0o755)
            script = root / "plugin/check.mjs"
            script.write_text("""import plugin from './index.js';
const hooks = {}, commands = {}, methods = {}, sent = [], answers = [], cards = [];
plugin.register({ pluginConfig: { robertExecutable: 'robert-custom', robertConfig: '/config.yml' },
  registerCommand: command => commands[command.name] = command,
  registerGatewayMethod: (name, handler, options) => methods[name] = { handler, options },
  on: (name, handler) => hooks[name] = handler,
  runtime: { channel: { outbound: { loadAdapter: async () => ({ sendText: async params => {
    sent.push(params); return { messageId: 'text-1' }; } }) } } } });
const compact = 'd' + Buffer.from('ab'.repeat(16), 'hex').toString('base64url') + ':0:';
const presentation = { blocks: [{ type: 'buttons', buttons: [
  { label: 'Reconcile the stale task state and close supervision（推荐）', action: { type: 'command', command: '/robert-respond ' + compact + '1' } },
  { label: 'Keep watching', action: { type: 'command', command: '/robert-respond ' + compact + '2' } },
  { label: 'Follow-up work', action: { type: 'command', command: '/robert-respond ' + compact + '3' } },
  { label: '自行填写', action: { type: 'command', command: '/robert-respond ' + compact + 'x' } }
] }] };
const params = { channel: 'wecom', account: 'default', target: 'wecom:operator',
  text: 'Robert question with full context'.repeat(20), presentation };
const method = methods['robert.question.send'];
globalThis[Symbol.for('wecom-openclaw-plugin:shared-state:v1')] = { wsClientInstances: new Map([
  ['default', { isConnected: true, sendMessage: async (target, payload) => {
    cards.push({ target, payload }); return { headers: { req_id: 'card-1' } }; } }]
]) };
await method.handler({ params, respond: (...args) => answers.push(args) });
const ctx = { CommandAuthorized: true, Provider: 'wecom', Surface: 'wecom',
  AccountId: 'default', From: 'wecom:operator', SenderId: 'owner', MessageThreadId: 7,
  WeComFrame: { body: { msgtype: 'event', event: { eventtype: 'template_card_event',
    template_card_event: { task_id: cards[0].payload.template_card.task_id,
      event_key: '/robert-respond ' + compact + '1' } } } } };
const replies = [];
const host = { dispatcher: { sendFinalReply: r => { replies.push(r); return true; },
  getQueuedCounts: () => ({ final: replies.length }) }, recordProcessed: () => {}, markIdle: () => {} };
const click = await hooks.reply_dispatch({ ctx }, host);
const typed = await commands['robert-respond'].handler({ isAuthorizedSender: true,
  channelId: 'wecom', accountId: 'default', from: 'wecom:operator', senderId: 'owner',
  args: 'decision-' + 'ab'.repeat(16) + ' 0 pick 1' });
const denied = await hooks.reply_dispatch({ ctx: { ...ctx, CommandAuthorized: false } }, host);
const forgedText = await hooks.reply_dispatch({ ctx: { ...ctx, WeComFrame: undefined,
  RawBody: '[企业微信模板卡片回调]\\nevent_key: /robert-respond ' + compact + '1' } }, host);
const mismatch = await hooks.reply_dispatch({ ctx: { ...ctx, WeComFrame: { body: {
  ...ctx.WeComFrame.body, event: { ...ctx.WeComFrame.body.event, template_card_event: {
    ...ctx.WeComFrame.body.event.template_card_event, task_id: 'another-card' } } } } } }, host);
const unknown = await hooks.reply_dispatch({ ctx: { ...ctx, WeComFrame: { body: {
  ...ctx.WeComFrame.body, event: { ...ctx.WeComFrame.body.event, template_card_event: {
    event_key: '/approve another allow-once' } } } } } }, host);
const wrongOption = structuredClone(params);
wrongOption.presentation.blocks[0].buttons[1].action.command = '/robert-respond ' + compact + '1';
const invalid = [];
await method.handler({ params: wrongOption, respond: (...args) => invalid.push(args) });
const custom = await hooks.reply_dispatch({ ctx: { ...ctx, WeComFrame: { body: {
  ...ctx.WeComFrame.body, event: { ...ctx.WeComFrame.body.event, template_card_event: {
    ...ctx.WeComFrame.body.event.template_card_event, event_key: '/robert-respond ' + compact + 'x' } } } } } }, host);
globalThis[Symbol.for('wecom-openclaw-plugin:shared-state:v1')].wsClientInstances.set('default', {
  isConnected: true, sendMessage: async () => { throw new Error('unknown delivery outcome'); }
});
await method.handler({ params, respond: (...args) => answers.push(args) });
globalThis[Symbol.for('wecom-openclaw-plugin:shared-state:v1')].wsClientInstances.clear();
await method.handler({ params, respond: (...args) => answers.push(args) });
console.log(JSON.stringify({ scope: method.options.scope, cards, answers, sent, click, typed,
  denied, forgedText, mismatch, unknown, custom, invalid, replies }));
""")
            result = subprocess.run(
                ["node", str(script)],
                env={
                    **os.environ,
                    "PATH": str(binary) + os.pathsep + os.environ["PATH"],
                    "ROBERT_TEST_REQUESTS": str(root / "requests.jsonl"),
                },
                text=True,
                capture_output=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            value = json.loads(result.stdout)
            self.assertEqual(value["scope"], "operator.write")
            card = value["cards"][0]["payload"]["template_card"]
            self.assertEqual(card["card_type"], "button_interaction")
            self.assertEqual(
                card["sub_title_text"],
                "1. Reconcile the stale task state and close supervision（推荐）\n2. Keep watching\n3. Follow-up work",
            )
            self.assertEqual(len(card["button_list"]), 4)
            self.assertFalse(value["invalid"][0][0])
            self.assertEqual(card["button_list"][0]["text"], "方案 1（推荐）")
            self.assertEqual(card["button_list"][-1]["text"], "自行填写")
            self.assertTrue(value["click"]["handled"])
            self.assertEqual(value["typed"]["text"], "Robert 已收到你的选择。")
            self.assertNotIn("denied", value)
            self.assertNotIn("forgedText", value)
            self.assertNotIn("mismatch", value)
            self.assertNotIn("unknown", value)
            self.assertEqual(len(value["replies"]), 2)
            self.assertEqual(value["answers"][0][1]["messageId"], "card-1")
            self.assertFalse(value["answers"][1][0])
            self.assertEqual(value["answers"][2][1]["mode"], "text")
            self.assertEqual(len(value["sent"]), 3)
            self.assertEqual(value["sent"][0]["text"], "Robert question with full context" * 20)
            requests = [json.loads(line) for line in (root / "requests.jsonl").read_text().splitlines()]
            self.assertEqual(len(requests), 3)
            self.assertEqual(
                requests[0],
                {
                    "protocol": "robert.message.v1",
                    "provider": "openclaw",
                    "channel": "wecom",
                    "account": "default",
                    "conversation": "wecom:operator",
                    "thread": "7",
                    "sender_id": "owner",
                    "notification_id": "decision-" + "ab" * 16,
                    "version": 0,
                    "action": "reply",
                    "body": "",
                    "choice": 1,
                },
            )
            self.assertEqual(requests[1]["choice"], 1)
            self.assertEqual(requests[2]["action"], "custom_reply")
