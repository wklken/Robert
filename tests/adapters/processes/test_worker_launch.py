from pathlib import Path
from tests.support.temp_workspace import TemporaryWorkspace


class WorkerLaunchTests(TemporaryWorkspace):
    def test_dispatch_builds_worker_command_with_prompt_and_worktree(self):
        from robert_agent import worker_adapters
        prompt_path = self.root / "task" / "prompt.md"
        prompt_path.parent.mkdir()
        prompt = "- prompt that starts like an option\nquotes: ' \" ` $() ; | --"
        prompt_path.write_text(prompt, encoding="utf-8")
        command = worker_adapters.build_worker_launch(
            prompt_path=prompt_path,
            worktree_path=Path("/tmp/repo/.worktrees/dd-123"),
            model="gpt-5.4",
            reasoning_effort="high",
        ).command

        self.assertEqual(command[:2], ["cbc", "-p"])
        self.assertNotIn("/bin/zsh", command)
        self.assertIn("--model", command)
        self.assertIn("gpt-5.4", command)
        self.assertIn("--effort", command)
        self.assertNotIn("--reasoning-effort", command)
        self.assertIn("--input-format", command)
        self.assertEqual(command[command.index("--input-format") + 1], "text")
        self.assertIn("--output-format", command)
        self.assertEqual(command[command.index("--output-format") + 1], "stream-json")
        self.assertIn("--add-dir", command)
        self.assertIn(str(prompt_path.parent), command)
        self.assertIn("/tmp/repo/.worktrees/dd-123", command)
        self.assertIn("--disallowedTools", command)
        disallowed = command[command.index("--disallowedTools") + 1]
        self.assertIn("TaskCreate", disallowed)
        self.assertIn("TeamCreate", disallowed)
        self.assertIn("SkillManage", disallowed)
        self.assertNotIn("Skill,", f"{disallowed},")
        self.assertNotIn("--", command)
        self.assertNotIn(prompt, command)

    def test_worker_agents_are_discovered_from_adapter_modules(self):
        from robert_agent import worker_adapters

        for agent in [
            "cbc",
            "claude",
            "codex",
            "opencode",
            "tclaude",
            "tcodex",
        ]:
            with self.subTest(agent=agent):
                self.assertIn(agent, worker_adapters.available_worker_agents())
                self.assertEqual(
                    worker_adapters._load_adapter(agent).AGENT_NAME,
                    agent,
                )

    def test_dispatch_builds_worker_command_with_configured_launcher(self):
        from robert_agent import worker_adapters
        prompt_path = self.root / "task" / "prompt.md"
        prompt_path.parent.mkdir()
        prompt_path.write_text("hello", encoding="utf-8")

        command = worker_adapters.build_worker_launch(
            prompt_path=prompt_path,
            worker_command="custom-cbc",
        ).command

        self.assertEqual(command[0], "custom-cbc")
        self.assertIn("--input-format", command)
        self.assertNotIn("--", command)
        self.assertNotIn("hello", command)

    def test_dispatch_builds_codex_worker_command_with_stdin_prompt(self):
        from robert_agent import worker_adapters
        prompt_path = self.root / "task" / "prompt.md"
        prompt_path.parent.mkdir()
        prompt = "codex prompt"
        prompt_path.write_text(prompt, encoding="utf-8")

        command = worker_adapters.build_worker_launch(
            prompt_path=prompt_path,
            worktree_path=Path("/tmp/repo/.worktrees/dd-123"),
            model="gpt-5.4",
            reasoning_effort="high",
            worker_command="/opt/homebrew/bin/codex",
            worker_agent="codex",
        ).command

        self.assertEqual(command[0], "/opt/homebrew/bin/codex")
        self.assertIn("--model", command)
        self.assertEqual(command[command.index("--model") + 1], "gpt-5.4")
        self.assertIn("--config", command)
        self.assertEqual(
            command[command.index("--config") + 1],
            'model_reasoning_effort="high"',
        )
        self.assertLess(command.index("--config"), command.index("exec"))
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", command)
        self.assertIn("--sandbox", command)
        self.assertEqual(command[command.index("--sandbox") + 1], "danger-full-access")
        self.assertNotIn("--ask-for-approval", command)
        self.assertIn("--json", command)
        self.assertIn("--cd", command)
        self.assertEqual(command[command.index("--cd") + 1], "/tmp/repo/.worktrees/dd-123")
        self.assertIn("--add-dir", command)
        self.assertIn(str(prompt_path.parent), command)
        self.assertEqual(command[-1], "-")
        self.assertNotIn("--input-format", command)
        self.assertNotIn("--output-format", command)
        self.assertNotIn(prompt, command)

    def test_dispatch_builds_tcodex_worker_command_with_reasoning_effort(self):
        from robert_agent import worker_adapters
        prompt_path = self.root / "task" / "prompt.md"
        prompt_path.parent.mkdir()
        prompt_path.write_text("tcodex prompt", encoding="utf-8")

        command = worker_adapters.build_worker_launch(
            prompt_path=prompt_path,
            model="gpt-5.6-sol",
            reasoning_effort="high",
            worker_command="tcodex",
            worker_agent="tcodex",
        ).command

        self.assertEqual(command[0], "tcodex")
        self.assertEqual(command[command.index("--model") + 1], "gpt-5.6-sol")
        self.assertEqual(
            command[command.index("--config") + 1],
            'model_reasoning_effort="high"',
        )
        self.assertLess(command.index("--config"), command.index("exec"))

    def test_dispatch_builds_claude_family_worker_commands(self):
        from robert_agent import worker_adapters
        from robert_agent.worker_adapters.cbc import (
            DEFAULT_DISALLOWED_WORKER_TOOLS,
        )
        prompt_path = self.root / "task" / "prompt.md"
        prompt_path.parent.mkdir()
        prompt = "claude prompt that must stay on stdin"
        prompt_path.write_text(prompt, encoding="utf-8")
        worktree = Path("/tmp/repo/.worktrees/dd-123")

        for agent in ["claude", "tclaude"]:
            with self.subTest(agent=agent):
                command = worker_adapters.build_worker_launch(
                    prompt_path=prompt_path,
                    worktree_path=worktree,
                    model="claude-sonnet-4-6",
                    reasoning_effort="high",
                    worker_command=agent,
                    worker_agent=agent,
                ).command

                self.assertEqual(command[:2], [agent, "-p"])
                self.assertEqual(
                    command[command.index("--model") + 1],
                    "claude-sonnet-4-6",
                )
                self.assertEqual(
                    command[command.index("--effort") + 1],
                    "high",
                )
                self.assertEqual(
                    command[command.index("--permission-mode") + 1],
                    "bypassPermissions",
                )
                self.assertEqual(
                    command[command.index("--disallowedTools") + 1],
                    DEFAULT_DISALLOWED_WORKER_TOOLS,
                )
                self.assertEqual(
                    command[command.index("--input-format") + 1],
                    "text",
                )
                self.assertEqual(
                    command[command.index("--output-format") + 1],
                    "stream-json",
                )
                self.assertIn(str(prompt_path.parent), command)
                self.assertIn(str(worktree), command)
                self.assertNotIn(prompt, command)

    def test_dispatch_builds_opencode_worker_command_with_stdin_prompt(self):
        from robert_agent import worker_adapters
        prompt_path = self.root / "task" / "prompt.md"
        prompt_path.parent.mkdir()
        prompt = "opencode prompt that must stay on stdin"
        prompt_path.write_text(prompt, encoding="utf-8")
        worktree = Path("/tmp/repo/.worktrees/dd-123")

        command = worker_adapters.build_worker_launch(
            prompt_path=prompt_path,
            worktree_path=worktree,
            model="openai/gpt-5.4",
            reasoning_effort="high",
            worker_command="opencode",
            worker_agent="opencode",
        ).command

        self.assertEqual(
            command,
            [
                "opencode",
                "run",
                "--model",
                "openai/gpt-5.4",
                "--variant",
                "high",
                "--format",
                "json",
                "--auto",
                "--dir",
                str(worktree),
            ],
        )
        self.assertNotIn(prompt, command)

    def test_dispatch_splits_configured_launcher_command(self):
        from robert_agent import worker_adapters
        prompt_path = self.root / "task" / "prompt.md"
        prompt_path.parent.mkdir()
        prompt_path.write_text("hello", encoding="utf-8")

        command = worker_adapters.build_worker_launch(
            prompt_path=prompt_path,
            worker_command="/opt/bin/codex --profile dd-worker",
            worker_agent="codex",
        ).command

        self.assertEqual(command[:3], ["/opt/bin/codex", "--profile", "dd-worker"])
        self.assertLess(command.index("--config"), command.index("exec"))

    def test_dispatch_expands_user_in_configured_launcher_path(self):
        from robert_agent import worker_adapters
        prompt_path = self.root / "task" / "prompt.md"
        prompt_path.parent.mkdir()
        prompt_path.write_text("hello", encoding="utf-8")

        command = worker_adapters.build_worker_launch(
            prompt_path=prompt_path,
            worker_command="~/bin/codex --profile dd-worker",
            worker_agent="codex",
        ).command

        self.assertEqual(
            command[:3],
            [str(Path("~/bin/codex").expanduser()), "--profile", "dd-worker"],
        )
        self.assertLess(command.index("--config"), command.index("exec"))

    def test_dispatch_accepts_normalized_worker_agent_config(self):
        from robert_agent import worker_adapters
        prompt_path = self.root / "task" / "prompt.md"
        prompt_path.parent.mkdir()
        prompt_path.write_text("hello", encoding="utf-8")

        command = worker_adapters.build_worker_launch(
            prompt_path=prompt_path,
            worker_agent_config={
                "agent": "codex",
                "command": "/opt/homebrew/bin/codex",
                "command_argv": ["/opt/homebrew/bin/codex"],
            },
        ).command

        self.assertEqual(command[0], "/opt/homebrew/bin/codex")
        self.assertLess(command.index("--config"), command.index("exec"))

    def test_dispatch_worker_environment_defaults_bash_timeout(self):
        from robert_agent.adapters.processes import worker as dispatch
        env = dispatch._worker_environment(
            environ={"KEEP": "1"},
        )

        self.assertEqual(env["BASH_DEFAULT_TIMEOUT_MS"], "300000")
        self.assertNotIn("KEEP", env)
        self.assertNotIn("BASH_MAX_TIMEOUT_MS", env)
        overridden = dispatch._worker_environment(
            allowed_names=["BASH_DEFAULT_TIMEOUT_MS"],
            environ={"BASH_DEFAULT_TIMEOUT_MS": "450000"},
        )
        self.assertEqual(overridden["BASH_DEFAULT_TIMEOUT_MS"], "450000")
