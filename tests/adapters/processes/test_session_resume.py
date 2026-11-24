from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

from robert_agent.adapters.processes import session_history
from robert_agent import worker_adapters


class SessionResumeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.tree = self.root / "tree"
        self.tree.mkdir()
        self.prompt = self.root / "data" / "prompt.md"
        self.prompt.parent.mkdir()
        self.prompt.write_bytes("task_id: task\nrun_id: original\nReview 原始输入\n".encode())
        self.sha = hashlib.sha256(self.prompt.read_bytes()).hexdigest()
        self.sid = "01a1f6d9-45c5-7670-8957-9abde29f8db4"
        self.started = datetime(2026, 11, 24, tzinfo=timezone.utc)
        self.addCleanup(patch.stopall)
        patch("pathlib.Path.home", return_value=self.root).start()

    def history(self, agent="tcodex", *, prompt=None, cwd=None):
        cwd = str(cwd or self.tree)
        text = self.prompt.read_text() if prompt is None else prompt
        if agent in {"codex", "tcodex"}:
            directory = self.root / (".tcodex" if agent == "tcodex" else ".codex") / "sessions/2026/11/24"
            path = directory / f"rollout-2026-11-24T00-00-00-{self.sid}.jsonl"
            rows = [
                {"type": "session_meta", "payload": {"id": self.sid, "source": "exec", "cwd": cwd}},
                {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"text": text}]}},
            ]
            event = {"type": "thread.started", "thread_id": self.sid}
        else:
            directory = (
                self.root
                / (".tclaude" if agent == "tclaude" else ".claude")
                / "projects"
                / re.sub(r"[^a-zA-Z0-9]", "-", str(self.tree))
            )
            path = directory / f"{self.sid}.jsonl"
            rows = [{"type": "user", "sessionId": self.sid, "cwd": cwd, "message": {"role": "user", "content": text}}]
            event = {"type": "system", "subtype": "init", "session_id": self.sid, "cwd": cwd}
        directory.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        stdout = self.prompt.parent / f"{agent}.stdout.log"
        stdout.write_text(json.dumps(event) + "\n")
        return path, stdout

    def resolve(self, agent="tcodex", stdout=None):
        from robert_agent.adapters.processes.session_resume import resolve_native_session

        return resolve_native_session(
            data_root=self.prompt.parent,
            agent=agent,
            task_id="task",
            run_id="original",
            prompt_path=self.prompt,
            prompt_sha256=self.sha,
            cwd=self.tree,
            stdout_path=stdout,
            started_at=self.started,
        )

    def descriptor(self, binding, **changes):
        from robert_agent.adapters.processes.session_resume import resume_descriptor

        args = dict(
            task_id="task", run_id="resumed", prompt_path=self.prompt, prompt_sha256=self.sha, cwd=self.tree, segment=1
        )
        args.update(changes)
        return resume_descriptor(binding, **args)

    def test_claude_history_dispatch_does_not_search_codex(self):
        self.history()
        self.assertIsNone(
            session_history.find_native_history(
                "tclaude", self.started, "task", "original", self.sid, prompt_sha256=self.sha
            )
        )

    def test_each_provider_binds_initial_prompt_and_preserves_resume_identity(self):
        for agent in ("codex", "tcodex", "claude", "tclaude"):
            with self.subTest(agent=agent):
                path, stdout = self.history(agent)
                binding = self.resolve(agent, stdout)
                self.assertEqual(binding["history_path"], str(path))
                self.assertEqual(binding["run_id"], "original")
                descriptor = self.descriptor(binding)
                self.assertEqual(descriptor["parent_run_id"], "original")
                self.assertEqual(descriptor["run_id"], "resumed")
                self.assertEqual(descriptor["segment"], 1)
                self.assertEqual(descriptor["session_id"], self.sid)
                self.assertEqual(descriptor["prompt_sha256"], self.sha)

    def test_explicit_provider_resume_argv_preserves_cwd_and_prompt(self):
        for agent in ("codex", "tcodex", "claude", "tclaude"):
            with self.subTest(agent=agent):
                _, stdout = self.history(agent)
                descriptor = self.descriptor(self.resolve(agent, stdout))
                launch = worker_adapters.build_worker_launch(
                    self.prompt, self.tree, worker_agent=agent, resume=descriptor
                )
                self.assertEqual(launch.cwd, str(self.tree))
                self.assertEqual(launch.stdin_path, str(self.prompt))
                self.assertEqual(launch.metadata()["resume"], descriptor)
                if agent in {"codex", "tcodex"}:
                    self.assertEqual(launch.command[-2:], [self.sid, "-"])
                    self.assertEqual(launch.command[launch.command.index("exec") + 1], "resume")
                    for flag in ("--cd", "--sandbox", "--add-dir", "--last"):
                        self.assertNotIn(flag, launch.command)
                else:
                    self.assertEqual(launch.command[launch.command.index("--resume") + 1], self.sid)
                    dirs = [launch.command[i + 1] for i, value in enumerate(launch.command) if value == "--add-dir"]
                    self.assertEqual(dirs, descriptor["add_dirs"])
                    self.assertNotIn("--continue", launch.command)
                    self.assertNotIn("--fork-session", launch.command)
                    if agent == "tclaude":
                        self.assertIn(("TCLAUDE_SKIP_UPDATE", "1"), launch.environment)

    def test_rejects_history_wrong_initial_prompt_cwd_and_later_matching_prompt(self):
        for agent in ("tcodex", "tclaude"):
            for wrong in ("prompt", "cwd", "later"):
                with self.subTest(agent=agent, wrong=wrong):
                    path, stdout = self.history(
                        agent, prompt="different" if wrong != "cwd" else None, cwd=self.root if wrong == "cwd" else None
                    )
                    if wrong == "later":
                        original = path.read_text().splitlines()[0 if agent == "tclaude" else 1]
                        row = json.loads(original)
                        if agent == "tclaude":
                            row["message"]["content"] = self.prompt.read_text()
                        else:
                            row["payload"]["content"] = [{"text": self.prompt.read_text()}]
                        with path.open("a") as stream:
                            stream.write(json.dumps(row) + "\n")
                    binding = self.resolve(agent, stdout)
                    with self.assertRaises(ValueError):
                        self.descriptor(binding)

    def test_early_stdout_binding_requires_history_before_resume(self):
        path, stdout = self.history("tclaude")
        original = path.read_text()
        path.unlink()
        binding = self.resolve("tclaude", stdout)
        self.assertEqual(binding["session_id"], self.sid)
        self.assertIsNone(binding["history_path"])
        with self.assertRaises(ValueError):
            self.descriptor(binding)
        path.write_text(original)
        complete = self.resolve("tclaude", stdout)
        self.assertEqual(complete["session_id"], binding["session_id"])
        self.assertEqual(self.descriptor(complete)["session_id"], self.sid)

    def test_codex_rollout_filename_uses_uuid_suffix_without_timestamp_parsing(self):
        path, stdout = self.history()
        renamed = path.with_name(f"rollout-synthetic-prefix-{self.sid}.jsonl")
        path.rename(renamed)
        descriptor = self.descriptor(self.resolve(stdout=stdout))
        self.assertEqual(descriptor["history_path"], str(renamed))
        self.assertEqual(
            worker_adapters.build_worker_launch(
                self.prompt, self.tree, worker_agent="tcodex", resume=descriptor
            ).command[-2],
            self.sid,
        )

    def test_refuses_symlinked_history_stdout_or_provider_root(self):
        for location in ("history", "stdout", "root"):
            with self.subTest(location=location):
                path, stdout = self.history()
                target = path if location == "history" else stdout if location == "stdout" else self.root / ".tcodex"
                moved = target.with_name(target.name + ".saved")
                target.rename(moved)
                target.symlink_to(moved, target_is_directory=location == "root")
                binding = self.resolve(stdout=stdout)
                if binding is not None:
                    with self.assertRaises(ValueError):
                        self.descriptor(binding)
                target.unlink()
                moved.rename(target)

    def test_descriptor_refuses_prompt_task_cwd_history_identity_changes(self):
        path, stdout = self.history()
        binding = self.resolve(stdout=stdout)
        for changes in (
            {"task_id": "other"},
            {"cwd": self.root},
            {"prompt_sha256": "0" * 64},
            {"segment": 0},
            {"run_id": "original"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.descriptor(binding, **changes)
        path.rename(path.with_suffix(".old"))
        path.write_text(path.with_suffix(".old").read_text())
        with self.assertRaises(ValueError):
            self.descriptor(binding)

    def test_adapter_rejects_cross_provider_changed_prompt_and_raw_command(self):
        _, stdout = self.history()
        descriptor = self.descriptor(self.resolve(stdout=stdout))
        for changes in (
            {"agent": "tclaude"},
            {"command": ["arbitrary"]},
            {"session_id": "--last"},
            {"cwd": str(self.root)},
            {"add_dirs": [str(self.root)]},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                worker_adapters.build_worker_launch(
                    self.prompt, self.tree, worker_agent="tcodex", resume={**descriptor, **changes}
                )
        self.prompt.write_text("modified")
        with self.assertRaises(ValueError):
            worker_adapters.build_worker_launch(self.prompt, self.tree, worker_agent="tcodex", resume=descriptor)

    def test_unsupported_provider_and_conflicting_stdout_do_not_fallback(self):
        _, stdout = self.history()
        self.assertIsNone(self.resolve("cbc", stdout))
        with stdout.open("a") as stream:
            stream.write(
                json.dumps({"type": "thread.started", "thread_id": "01a1f6d9-45c5-7670-8957-9abde29f8db5"}) + "\n"
            )
        self.assertIsNone(self.resolve(stdout=stdout))

    def test_native_only_discovery_and_aware_timestamp_validation(self):
        _, stdout = self.history()
        self.assertEqual(self.resolve()["session_id"], self.sid)
        self.started = "2026-11-24T00:00:00Z"
        self.assertEqual(self.resolve(stdout=stdout)["session_id"], self.sid)
        for started in ("2026-11-24T00:00:00", datetime(2026, 11, 24), "invalid"):
            self.started = started
            self.assertIsNone(self.resolve(stdout=stdout))

    def test_same_inode_native_prompt_tamper_is_refused_by_adapter(self):
        path, stdout = self.history()
        descriptor = self.descriptor(self.resolve(stdout=stdout))
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        rows[1]["payload"]["content"] = [{"text": "different task"}]
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        with self.assertRaises(ValueError):
            worker_adapters.build_worker_launch(self.prompt, self.tree, worker_agent="tcodex", resume=descriptor)

    def test_codex_runtime_context_before_original_task_prompt_is_supported(self):
        path, stdout = self.history()
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        rows.insert(
            1,
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "# AGENTS.md instructions for /tree\n\n<INSTRUCTIONS>rules</INSTRUCTIONS><environment_context>cwd</environment_context>",
                        }
                    ],
                },
            },
        )
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        self.assertEqual(self.descriptor(self.resolve(stdout=stdout))["session_id"], self.sid)

    def test_adapter_declares_resume_capability_and_preserves_configured_prefix(self):
        for agent in worker_adapters.available_worker_agents():
            self.assertEqual(
                worker_adapters.supports_native_resume(agent), agent in {"codex", "tcodex", "claude", "tclaude"}
            )
        _, stdout = self.history()
        descriptor = self.descriptor(self.resolve(stdout=stdout))
        prefix = ["env", "SAMPLE=1", "tcodex", "--profile", "review"]
        launch = worker_adapters.build_worker_launch(
            self.prompt, self.tree, worker_agent_config={"agent": "tcodex", "command_argv": prefix}, resume=descriptor
        )
        self.assertEqual(launch.command[: len(prefix)], prefix)
        with self.assertRaises(ValueError):
            worker_adapters.build_worker_launch(self.prompt, self.tree, worker_agent="cbc", resume=descriptor)

    def test_controlled_provider_subprocess_resumes_exact_session_with_frozen_bytes(self):
        from robert_agent.adapters.processes.worker import dispatch_worker, tracked_process, reap_finished_process

        binary = self.root / "provider.py"
        binary.write_text(
            """import hashlib,json,os,sys\nfrom pathlib import Path\nargs=sys.argv[1:]\nsid=args[args.index('--resume')+1] if '--resume' in args else args[-2]\nassert sid==os.environ['EXPECTED_SESSION']\nassert '--continue' not in args and '--fork-session' not in args\nassert '--cd' not in args and '--sandbox' not in args\nprint(json.dumps({'session':sid,'prompt':hashlib.sha256(sys.stdin.buffer.read()).hexdigest(),'cwd':os.getcwd(),'argv':args}))\n"""
        )
        for agent in ("tcodex", "tclaude"):
            with self.subTest(agent=agent):
                _, stdout = self.history(agent)
                descriptor = self.descriptor(self.resolve(agent, stdout))
                output = self.root / agent
                result = dispatch_worker(
                    "task",
                    "resumed",
                    self.prompt,
                    worktree_path=self.tree,
                    worker_agent_config={
                        "agent": agent,
                        "command_argv": ["env", "IS_SANDBOX=1", sys.executable, str(binary)],
                    },
                    resume=descriptor,
                    trusted_environment={"EXPECTED_SESSION": self.sid},
                    log_dir=output,
                    dry_run=False,
                )
                self.assertTrue(result["ok"], result)
                proc = tracked_process(result["pid"])
                if proc is not None:
                    self.assertEqual(proc.wait(timeout=5), 0)
                    reap_finished_process(proc.pid)
                else:
                    self.assertTrue(result["process_exited"])
                    self.assertEqual(result["returncode"], 0)
                self.assertEqual(result["worker_launch"]["resume"], descriptor)
                captured = json.loads(Path(result["stdout_path"]).read_text())
                self.assertEqual(captured["session"], self.sid)
                self.assertEqual(captured["prompt"], self.sha)
                self.assertEqual(captured["cwd"], str(self.tree))

    def test_codex_stage_environment_selects_tcodex_home_for_history_and_fake_dispatch(self):
        from robert_agent.adapters.processes.environment import stage_environment
        from robert_agent.adapters.processes.session_resume import resolve_native_session
        from robert_agent.adapters.processes.worker import dispatch_worker, tracked_process, reap_finished_process

        path, stdout = self.history("tcodex")
        home = self.root / ".tcodex"
        with patch.dict(os.environ, {"CODEX_HOME": str(home)}):
            environment = stage_environment(
                {"worktree_path": str(self.tree), "context_dir": str(self.prompt.parent)},
                self.prompt.parent,
                self.prompt.parent,
                workflow_name="review",
            )
        binding = resolve_native_session(
            data_root=self.prompt.parent,
            agent="codex",
            task_id="task",
            run_id="original",
            prompt_path=self.prompt,
            prompt_sha256=self.sha,
            cwd=self.tree,
            stdout_path=stdout,
            started_at=self.started,
            native_home=environment["CODEX_HOME"],
        )
        self.assertEqual(binding["history_path"], str(path))
        self.assertEqual(binding["native_home"], str(home))
        descriptor = self.descriptor(binding, native_home=home)
        binary = self.root / "home_provider.py"
        binary.write_text(
            "import os,sys\nfrom pathlib import Path\nassert sys.argv[-2]==os.environ['EXPECTED_SESSION']\nassert Path(os.environ['CODEX_HOME'], 'sessions').is_dir()\nprint(os.environ['CODEX_HOME'])\n"
        )
        result = dispatch_worker(
            "task",
            "resumed",
            self.prompt,
            worktree_path=self.tree,
            worker_agent_config={"agent": "codex", "command_argv": [sys.executable, str(binary)]},
            resume=descriptor,
            trusted_environment={**environment, "CODEX_HOME": str(self.root / "wrong"), "EXPECTED_SESSION": self.sid},
            log_dir=self.root / "custom-home-resumed",
            dry_run=False,
        )
        self.assertTrue(result["ok"], result)
        proc = tracked_process(result["pid"])
        if proc is not None:
            self.assertEqual(proc.wait(timeout=5), 0)
            reap_finished_process(proc.pid)
        else:
            self.assertEqual(result["returncode"], 0)
        self.assertEqual(Path(result["stdout_path"]).read_text().strip(), str(home))
        self.assertEqual(result["worker_launch"]["environment"]["CODEX_HOME"], str(home))

    def test_explicit_claude_config_home_binds_only_registered_runtime(self):
        from robert_agent.adapters.processes.session_resume import native_home_from_environment, resolve_native_session
        from robert_agent.adapters.processes.worker import dispatch_worker, tracked_process, reap_finished_process

        path, stdout = self.history("claude")
        home = self.root / "registered-claude-config"
        (self.root / ".claude").rename(home)
        path = home / path.relative_to(self.root / ".claude")
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.root / "ambient-wrong")}):
            self.assertEqual(native_home_from_environment("claude", {"CLAUDE_CONFIG_DIR": str(home)}), str(home))
            binding = resolve_native_session(
                data_root=self.prompt.parent,
                agent="claude",
                task_id="task",
                run_id="original",
                prompt_path=self.prompt,
                prompt_sha256=self.sha,
                cwd=self.tree,
                stdout_path=stdout,
                started_at=self.started,
                native_home=home,
            )
        with self.assertRaises(ValueError):
            self.descriptor(binding)
        descriptor = self.descriptor(binding, native_home=home)
        self.assertEqual(descriptor["history_path"], str(path))
        launch = worker_adapters.build_worker_launch(self.prompt, self.tree, worker_agent="claude", resume=descriptor)
        self.assertEqual(dict(launch.environment)["CLAUDE_CONFIG_DIR"], str(home))
        self.assertEqual(launch.cwd, str(self.tree))
        binary = self.root / "claude_home_provider.py"
        binary.write_text("import os,sys\nassert '--resume' in sys.argv\nprint(os.environ['CLAUDE_CONFIG_DIR'])\n")
        result = dispatch_worker(
            "task",
            "resumed",
            self.prompt,
            worktree_path=self.tree,
            worker_agent_config={"agent": "claude", "command_argv": [sys.executable, str(binary)]},
            resume=descriptor,
            trusted_environment={"CLAUDE_CONFIG_DIR": str(self.root / "ambient-wrong")},
            log_dir=self.root / "claude-home-resumed",
            dry_run=False,
        )
        self.assertTrue(result["ok"], result)
        proc = tracked_process(result["pid"])
        if proc is not None:
            self.assertEqual(proc.wait(timeout=5), 0)
            reap_finished_process(proc.pid)
        else:
            self.assertEqual(result["returncode"], 0)
        self.assertEqual(Path(result["stdout_path"]).read_text().strip(), str(home))
        other = self.root / "unregistered-config"
        other.mkdir()
        with self.assertRaises(ValueError):
            self.descriptor(binding, native_home=other)
        alias = self.root / "config-alias"
        alias.symlink_to(home, target_is_directory=True)
        self.assertIsNone(
            resolve_native_session(
                data_root=self.prompt.parent,
                agent="claude",
                task_id="task",
                run_id="original",
                prompt_path=self.prompt,
                prompt_sha256=self.sha,
                cwd=self.tree,
                stdout_path=stdout,
                started_at=self.started,
                native_home=alias,
            )
        )

    def test_registered_native_home_inode_and_history_root_cannot_be_rebound(self):
        _, stdout = self.history()
        binding = self.resolve(stdout=stdout)
        home = self.root / ".tcodex"
        wrong = self.root / "wrong-home"
        wrong.mkdir()
        with self.assertRaises(ValueError):
            self.descriptor(binding, native_home=wrong)
        descriptor = self.descriptor(binding, native_home=home)
        with self.assertRaises(ValueError):
            worker_adapters.build_worker_launch(
                self.prompt, self.tree, worker_agent="tcodex", resume={**descriptor, "history_root": str(wrong)}
            )
        saved = home.with_name("old-home")
        home.rename(saved)
        home.mkdir()
        (saved / "sessions").rename(home / "sessions")
        with self.assertRaises(ValueError):
            self.descriptor(binding, native_home=home)
