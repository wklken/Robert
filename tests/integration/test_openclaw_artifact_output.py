"""Verify complete-file guidance against the registered public CLI preview."""

from contextlib import closing, redirect_stdout
import io
import json
from pathlib import Path
import shlex
import shutil
import sqlite3
import subprocess
import unittest

import yaml

from robert_agent.cli.main import main
from robert_agent.integrations.openclaw import write_plugin
from robert_agent.operations.initialize import init_config
from tests.support.summary_database import SummaryFixture


@unittest.skipUnless(shutil.which("node"), "Node is required to execute the OpenClaw plugin")
class OpenClawArtifactOutputTests(SummaryFixture):
    def test_complete_file_hint_reaches_content_missing_from_public_cli_preview(self):
        db_path = self.root / "robert.sqlite3"
        self._init_summary_db(db_path)
        path = self.root / "task-artifacts/worker.stdout.log"
        content = "FIRST LINE\n" + "x" * 70000
        path.write_text(content, encoding="utf-8")
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute("UPDATE artifacts SET bytes = ? WHERE artifact_id = 'artifact-stdout'", (path.stat().st_size,))
        repo = self.root / "repo"
        (repo / ".git").mkdir(parents=True)
        config_path = self.root / "config.yml"
        init_config(
            config_path,
            {
                "repo": "example/backend",
                "repo_path": str(repo),
                "worker": "codex",
                "github_account": "robert-bot",
                "trusted_actor": "maintainer",
            },
            non_interactive=True,
        )
        config = yaml.safe_load(config_path.read_text())
        config["data_dir"] = str(self.root)
        config_path.write_text(json.dumps(config))
        args = ["artifact", "show", "task-1", "worker_stdout", "--config", str(config_path), "--output", "json"]
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(args)
        payload = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertTrue(payload["data"]["truncated"])
        self.assertNotIn("FIRST LINE", payload["data"]["content"])
        plugin_dir = self.root / "plugin"
        write_plugin(plugin_dir)
        script = plugin_dir / "check.mjs"
        script.write_text("""import { formatReply } from './replies.js';
const args = JSON.parse(process.argv[2]);
const result = JSON.parse(process.argv[3]);
console.log(JSON.stringify([false, true].map(jsonOutput => formatReply(args, result, undefined, { jsonOutput }))));
""")
        result = subprocess.run(
            ["node", str(script), json.dumps(args), json.dumps(payload)],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        for reply in json.loads(result.stdout):
            text = reply["text"]
            self.assertLessEqual(len(text.encode("utf-16-le")) // 2, 3500)
            file_hint = next(line for line in text.splitlines() if line.startswith("完整产物："))
            [registered_path] = shlex.split(file_hint.removeprefix("完整产物：在 Robert 主机打开已登记文件 "))
            self.assertEqual(Path(registered_path).read_text(encoding="utf-8"), content)
