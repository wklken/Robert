"""Execute generated JavaScript against the documented OpenClaw command context."""

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
class OpenClawMessagingTests(unittest.TestCase):
    def test_generated_plugin_translates_authenticated_reply_without_shell(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_plugin(root / "plugin")
            package = root / "plugin/node_modules/openclaw"
            package.mkdir(parents=True)
            (package / "package.json").write_text(
                json.dumps({"type": "module", "exports": {"./plugin-sdk/plugin-entry": "./entry.js"}})
            )
            (package / "entry.js").write_text("export const definePluginEntry = (plugin) => plugin;")
            binary = root / "bin"
            binary.mkdir()
            executable = binary / "robert"
            executable.write_text(
                f"#!{sys.executable}\n"
                + 'import json, sys\nr=json.loads(sys.stdin.read() or "{}")\nprint(json.dumps({"ok":True,"request":r,"args":sys.argv[1:]}))\n'
            )
            executable.chmod(0o755)
            script = root / "plugin/check.mjs"
            script.write_text("""import plugin from './index.js';
const commands = {};
plugin.register({ pluginConfig: { robertConfig: '/configuration with spaces.yml' },
  registerCommand: (cmd) => { commands[cmd.name] = cmd; } });
const context = { isAuthorizedSender: true, channel: 'chat', channelId: 'telegram',
  accountId: 'personal', from: 'telegram:42', senderId: '42', messageThreadId: 7,
  args: 'notice-1 3 reply Use SQLite.\\nKeep $(touch never) literal.' };
const response = await commands['robert-respond'].handler(context);
const status = await commands['robert-status'].handler(context);
const denied = await commands['robert-respond'].handler({ ...context, isAuthorizedSender: false });
const malformed = await commands['robert-respond'].handler({ ...context, args: 'notice reply yes' });
console.log(JSON.stringify({ response, status, denied, malformed, auth: commands['robert-respond'].requireAuth }));
""")
            result = subprocess.run(
                ["node", str(script)],
                env={**os.environ, "PATH": str(binary) + os.pathsep + os.environ["PATH"]},
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )
            value = json.loads(result.stdout)
            data = json.loads(value["response"]["text"])
            self.assertEqual(
                data["args"], ["messages", "respond", "--output", "json", "--config", "/configuration with spaces.yml"]
            )
            self.assertEqual(
                data["request"],
                {
                    "protocol": "robert.message.v1",
                    "provider": "openclaw",
                    "channel": "telegram",
                    "account": "personal",
                    "conversation": "telegram:42",
                    "thread": "7",
                    "sender_id": "42",
                    "notification_id": "notice-1",
                    "version": 3,
                    "action": "reply",
                    "body": "Use SQLite.\nKeep $(touch never) literal.",
                },
            )
            self.assertEqual(
                json.loads(value["status"]["text"])["args"],
                ["status", "--output", "json", "--config", "/configuration with spaces.yml"],
            )
            self.assertTrue(value["auth"])
            self.assertTrue(value["denied"]["isError"])
            self.assertTrue(value["malformed"]["isError"])
