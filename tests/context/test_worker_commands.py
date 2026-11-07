from robert_agent.context import attempt as attempt_context
from tests.support.temp_workspace import TemporaryWorkspace


class WorkerCommandPromptTests(TemporaryWorkspace):
    def test_prompt_uses_current_python_executable_for_worker_commands(self):
        from robert_agent.context import prompts as render_prompt
        prompt = render_prompt.render_prompt(
            task={
                "task_id": "task-1",
                "attempt_id": "attempt-1",
                "workstream_id": "github:example/backend!456",
            },
            route_result={
                "expected_output": "update_existing_pr",
                "allowed_github_actions": ["push_existing_pr", "comment"],
                "required_skills": [],
            },
            events=[{"event_fingerprint": "comment:1"}],
            runtime_context={
                "python_bin": "/usr/bin/python3.11",
                "db_path": "/tmp/dd.sqlite3",
                "result_script": "/agent/result.py",
                "snapshot_script": "/worker/snapshot.py",
                "heartbeat_script": "/worker/heartbeat.py",
                "status_script": "/agent/status.py",
            },
        )

        import sys
        self.assertIn(f"{sys.executable} /agent/status.py", prompt)
        self.assertIn("For control-plane state", prompt)
        self.assertIn("task task-1", prompt)
        self.assertIn(f"{sys.executable} /agent/status.py --help", prompt)
        self.assertNotIn("run latest", prompt)
        self.assertIn(f"{sys.executable} /worker/snapshot.py", prompt)
        self.assertIn(f"{sys.executable} /worker/heartbeat.py", prompt)
        self.assertIn(f"{sys.executable} /agent/result.py", prompt)

    def test_runtime_context_uses_running_python_bin(self):
        context = attempt_context.runtime_context(
            self.root / "dd.sqlite3",
            {"default_base_branch": "master", "python_bin": "/opt/python/bin/python3"},
        )

        import sys
        self.assertEqual(context["python_bin"], sys.executable)
        self.assertTrue(context["status_script"].endswith("readmodels/status.py"))
