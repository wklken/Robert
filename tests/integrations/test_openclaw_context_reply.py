"""Run the plugin's terminal reply hook with the installed host's context shape."""

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
class OpenClawContextReplyTests(unittest.TestCase):
    def test_plain_and_quoted_replies_bypass_model_and_preserve_host_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_plugin(root / "plugin")
            package = root / "plugin/node_modules/openclaw"
            package.mkdir(parents=True)
            (package / "package.json").write_text(
                json.dumps({"type": "module", "exports": {"./plugin-sdk/plugin-entry": "./entry.js"}})
            )
            (package / "entry.js").write_text("export const definePluginEntry = p => p;")
            binary = root / "bin"
            binary.mkdir()
            executable = binary / "robert"
            executable.write_text(
                f"#!{sys.executable}\n"
                + "import json,sys\nr=json.load(sys.stdin)\nstatus={'ignored':'ignored','empty':'no_question','rejected':'rejected'}.get(r['body'],'accepted')\nprint(json.dumps({'ok':status!='rejected','status':status,'safe_error':'Question is stale.','notification_id':'attention-one','task_id':'task-child','request':r}))\n"
            )
            executable.chmod(0o755)
            script = root / "plugin/check.mjs"
            script.write_text("""import plugin from './index.js';
const hooks = {};
const register = config => plugin.register({ pluginConfig: config,
  registerCommand: () => {}, on: (name, fn) => hooks[name] = fn });
register({});
const defaultHooks = Object.keys(hooks);
register({ contextualReplies: true, robertConfig: '/config with spaces.yml' });
const context = { RawBody: 'Use release/1.25.\\nKeep $(touch never) literal.',
  Body: '[envelope] irrelevant', CommandBody: 'same', CommandAuthorized: true,
  Provider: 'wecom', Surface: 'wecom', From: 'wecom:operator', To: 'wecom:bot',
  AccountId: 'default', SenderId: 'owner', MessageSid: 'incoming-1' };
const calls = [];
const host = { dispatcher: { sendFinalReply: reply => { calls.push(reply); return true; },
  getQueuedCounts: () => ({ tool: 0, block: 0, final: calls.length }) },
  recordProcessed: (...args) => {}, markIdle: () => {} };
const plain = await hooks.reply_dispatch({ ctx: context }, host);
const quoted = await hooks.reply_dispatch({ ctx: { ...context, MessageSid: 'incoming-2',
  ReplyToId: 'outgoing-1', ReplyToBody: 'Delivery: attention-one:hash', ReplyToIsQuote: true,
  MessageThreadId: 7 } }, host);
const slash = await hooks.reply_dispatch({ ctx: { ...context, RawBody: '/robert-status' } }, host);
const unauthorized = await hooks.reply_dispatch({ ctx: { ...context, CommandAuthorized: false } }, host);
const tail = await hooks.reply_dispatch({ ctx: context, isTailDispatch: true }, host);
const ignored = await hooks.reply_dispatch({ ctx: { ...context, RawBody: 'ignored' } }, host);
const empty = await hooks.reply_dispatch({ ctx: { ...context, RawBody: 'empty' } }, host);
const rejected = await hooks.reply_dispatch({ ctx: { ...context, RawBody: 'rejected' } }, host);
console.log(JSON.stringify({ defaultHooks, plain, quoted, slash, unauthorized, tail, ignored, empty, rejected, calls }));
""")
            result = subprocess.run(
                ["node", str(script)],
                env={**os.environ, "PATH": str(binary) + os.pathsep + os.environ["PATH"]},
                text=True,
                capture_output=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            value = json.loads(result.stdout)
            self.assertEqual(value["defaultHooks"], [])
            self.assertTrue(value["plain"]["handled"])
            self.assertTrue(value["quoted"]["handled"])
            self.assertNotIn("slash", value)
            self.assertNotIn("unauthorized", value)
            self.assertNotIn("tail", value)
            self.assertNotIn("ignored", value)
            self.assertTrue(value["empty"]["handled"])
            self.assertTrue(value["rejected"]["handled"])
            self.assertEqual(len(value["calls"]), 4)
            self.assertIn("attention-one", value["calls"][0]["text"])
            self.assertIn("没有", value["calls"][2]["text"])
            self.assertIn("Question is stale", value["calls"][3]["text"])
            # The fake CLI puts its input in the result so quote/body separation
            # is verified by a second script without replacing production parsing.
            executable.write_text(
                f"#!{sys.executable}\n"
                + "import json,sys\nr=json.load(sys.stdin)\nprint(json.dumps({'ok':True,'status':'accepted','notification_id':json.dumps(r),'task_id':'task-child'}))\n"
            )
            result = subprocess.run(
                ["node", str(script)],
                env={**os.environ, "PATH": str(binary) + os.pathsep + os.environ["PATH"]},
                text=True,
                capture_output=True,
                timeout=10,
                check=True,
            )
            value = json.loads(result.stdout)
            self.assertIn('"conversation": "wecom:operator"', value["calls"][0]["text"])
            self.assertIn('"quote_message_id": "outgoing-1"', value["calls"][1]["text"])
            self.assertIn('"thread": "7"', value["calls"][1]["text"])
            self.assertIn("$(touch never)", value["calls"][0]["text"])
