from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Timer
import json
import sys
import unittest
from unittest.mock import patch

from robert_agent.adapters.processes.source_model import UnsupportedProfile, profile_for, run


class SourceModelInvocationTests(unittest.TestCase):
    def worker(self, command):
        return {
            "name": "reviewed",
            "agent": "claude",
            "command_argv": command,
            "timeout_seconds": 2,
            "environment_allowlist": ["GITHUB_TOKEN", "TAPD_ACCESS_TOKEN", "ANTHROPIC_API_KEY"],
            "default_model": "sonnet",
            "default_effort": "high",
        }

    def test_direct_no_tool_profile_rejects_wrappers_and_custom_flags(self):
        with TemporaryDirectory() as directory:
            fake = Path(directory) / "claude"
            fake.write_text("#!/bin/sh\nexit 0\n")
            fake.chmod(0o700)
            for worker in (
                self.worker(["claude"]),
                self.worker([sys.executable, "--tools", "Bash"]),
                self.worker([str(fake)]),
                {**self.worker([sys.executable]), "agent": "command"},
            ):
                with self.subTest(worker=worker), self.assertRaises(UnsupportedProfile):
                    profile_for(worker)

    def test_explicit_endpoint_is_never_discarded_or_silently_replaced(self):
        with TemporaryDirectory() as directory:
            executable = Path(directory) / "claude"
            executable.write_text("#!/bin/sh\n")
            executable.chmod(0o700)
            with (
                patch("robert_agent.adapters.processes.source_model._native_binary", return_value=True),
                patch.dict("os.environ", {}, clear=True),
            ):
                for variable in ("ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX"):
                    with (
                        self.subTest(variable=variable),
                        patch.dict(
                            "os.environ", {variable: "https://model.example" if variable.endswith("_URL") else "1"}
                        ),
                        self.assertRaises(UnsupportedProfile),
                    ):
                        profile_for(self.worker([str(executable)]))

    def test_endpoint_drift_before_spawn_is_explicit_no_child(self):
        with TemporaryDirectory() as directory:
            executable = Path(directory) / "claude"
            executable.write_text("#!/bin/sh\nexit 0\n")
            executable.chmod(0o700)
            with (
                patch("robert_agent.adapters.processes.source_model._native_binary", return_value=True),
                patch.dict("os.environ", {}, clear=True),
            ):
                profile = profile_for(self.worker([str(executable)]))
            with patch.dict("os.environ", {"ANTHROPIC_BASE_URL": "https://model.example"}, clear=True):
                result = run(profile, "prompt", Event(), lambda _: self.fail("unexpected process"))
        self.assertEqual(result.status, "source_model_custom_provider_unavailable")
        self.assertEqual(result.exit_kind, "spawn_failed")

    def test_controlled_executable_observes_tool_disable_and_minimal_environment(self):
        payload = """import json, os, sys, time
argv = sys.argv[1:]
checks = {'no_builtin': argv[argv.index('--tools')+1] == '',
          'strict_mcp': '--strict-mcp-config' in argv and
              json.load(open(argv[argv.index('--mcp-config')+1])) == {'mcpServers': {}},
          'bare': '--bare' in argv,
          'no_browser': '--no-chrome' in argv,
          'no_settings': argv[argv.index('--setting-sources')+1] == '',
          'no_session': '--no-session-persistence' in argv,
          'no_slash': '--disable-slash-commands' in argv,
          'credential_filtered': 'GITHUB_TOKEN' not in os.environ and 'TAPD_ACCESS_TOKEN' not in os.environ,
          'stdin': sys.stdin.read() == 'exact prompt'}
time.sleep(.15)
print(json.dumps(checks))"""
        with TemporaryDirectory() as directory:
            executable = Path(directory) / "claude"
            executable.write_text(f"#!{sys.executable}\n{payload}\n")
            executable.chmod(0o700)
            with (
                patch("robert_agent.adapters.processes.source_model._native_binary", return_value=True),
                patch.dict("os.environ", {}, clear=True),
            ):
                profile = profile_for(self.worker([str(executable)]))
            started = []
            with patch.dict("os.environ", {}, clear=True):
                result = run(profile, "exact prompt", Event(), lambda identity: started.append(identity) or True)
        self.assertTrue(all(json.loads(result.output).values()))
        self.assertEqual(result.exit_kind, "group_exited")
        self.assertEqual(result.identity, started[0])

    def test_child_without_recorded_identity_keeps_attention_even_after_exit(self):
        with TemporaryDirectory() as directory:
            executable = Path(directory) / "claude"
            executable.write_text(f"#!{sys.executable}\nprint('no proof')\n")
            executable.chmod(0o700)
            with (
                patch("robert_agent.adapters.processes.source_model._native_binary", return_value=True),
                patch.dict("os.environ", {}, clear=True),
            ):
                profile = profile_for(self.worker([str(executable)]))
                with patch("robert_agent.adapters.processes.model_command.stable_identity", return_value=None):
                    result = run(profile, "prompt", Event(), lambda _: self.fail("unexpected identity"))
        self.assertEqual(result.status, "process_attention")
        self.assertIsNone(result.exit_kind)
        self.assertEqual(result.output, b"")

    def test_owned_cancellation_reports_group_exit_for_recorded_identity(self):
        with TemporaryDirectory() as directory:
            executable = Path(directory) / "claude"
            executable.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(5)\n")
            executable.chmod(0o700)
            with (
                patch("robert_agent.adapters.processes.source_model._native_binary", return_value=True),
                patch.dict("os.environ", {}, clear=True),
            ):
                profile = profile_for(self.worker([str(executable)]))
            stop = Event()
            timer = Timer(0.2, stop.set)
            timer.start()
            try:
                with patch.dict("os.environ", {}, clear=True):
                    result = run(profile, "prompt", stop, lambda _identity: True)
            finally:
                timer.cancel()
                timer.join()
        self.assertEqual(result.status, "stopped")
        self.assertEqual(result.exit_kind, "group_exited")
