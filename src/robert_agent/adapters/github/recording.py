"""adapters github recording responsibilities."""

from robert_agent.adapters.github import client as github_client
from robert_agent.security.redaction import redact_text
from robert_agent.security.text import strip_ansi
import json
import subprocess


class JobLogBlocked(subprocess.CalledProcessError):
    """The GitHub log was read but could not be retained after redaction."""


class RecordingRunner:
    def __init__(self, runner, *, max_job_log_chars=0):
        self.runner = runner
        self.max_job_log_chars = max_job_log_chars
        self.responses = {}

    def __call__(self, args, **kwargs):
        key = github_client.command_key(args)
        try:
            response = self.runner(args, **kwargs)
        except Exception:
            self.responses[key] = None
            raise
        if response.returncode == 0 and github_client.is_actions_job_log_read(args):
            output = strip_ansi(response.stdout or "").strip()
            safe = redact_text(output, strict_assignments=True)
            excerpt = safe["text"][-self.max_job_log_chars :] if self.max_job_log_chars and safe["ok"] else ""
            if not safe["ok"]:
                self.responses[key] = json.dumps({"evidence_status": "blocked_secret"})
            else:
                self.responses[key] = json.dumps({"log_excerpt": excerpt}) if excerpt else None
            return response
        try:
            json.loads(response.stdout)
        except (ValueError, TypeError):
            self.responses[key] = None
        else:
            self.responses[key] = response.stdout if response.returncode == 0 else None
        return response


class ReplayRunner:
    def __init__(self, responses):
        self.responses = dict(responses)

    def __call__(self, args, **kwargs):
        key = github_client.command_key(args)
        payload = self.responses.get(key)
        if payload is not None and github_client.is_actions_job_log_read(args):
            value = json.loads(payload)
            if isinstance(value, dict) and value.get("evidence_status") == "blocked_secret":
                if kwargs.get("check"):
                    raise JobLogBlocked(1, args, "", "GitHub job log blocked by evidence redaction")
                return subprocess.CompletedProcess(args, 1, "", "GitHub job log blocked by evidence redaction")
            payload = value.get("log_excerpt") if isinstance(value, dict) else None
        if payload is None:
            if kwargs.get("check"):
                raise subprocess.CalledProcessError(1, args, "", "remote observation unavailable")
            return subprocess.CompletedProcess(args, 1, "", "remote observation unavailable")
        return subprocess.CompletedProcess(args, 0, payload, "")
