"""adapters github recording responsibilities."""
from robert_agent.adapters.github import client as github_client
import json
import subprocess



class RecordingRunner:
    def __init__(self, runner):
        self.runner = runner
        self.responses = {}

    def __call__(self, args, **kwargs):
        key = github_client.command_key(args)
        try:
            response = self.runner(args, **kwargs)
        except Exception:
            self.responses[key] = None
            raise
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
        if key not in self.responses or self.responses[key] is None:
            if kwargs.get('check'):
                raise subprocess.CalledProcessError(1, args, '', 'remote observation unavailable')
            return subprocess.CompletedProcess(args, 1, '', 'remote observation unavailable')
        return subprocess.CompletedProcess(args, 0, self.responses[key], '')
