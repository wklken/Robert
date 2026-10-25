import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from robert_agent.dispatch import dispatch_worker, _BACKGROUND_PROCESSES
from robert_agent.worker_adapters import build_worker_launch


class ReviewDispatchTests(unittest.TestCase):
    def test_claude_streaming_print_mode_includes_required_verbose_flag(self):
        for agent in ('claude', 'tclaude'):
            launch = build_worker_launch(Path('/tmp/prompt.md'), worker_agent=agent,
                                         model='default', reasoning_effort='default')
            self.assertIn('--verbose', launch.command)

    def test_codex_and_claude_receive_identical_bytes_and_private_output_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tree = root / 'tree'
            tree.mkdir()
            prompt = root / 'reviewer-prompt.md'
            prompt.write_bytes('Review 统一上下文\nNo interpolation: $ROBERT_REVIEW_OUTPUT_DIR\n'.encode())
            binary = root / 'fake_model.py'
            binary.write_text('''import hashlib,json,os,sys
from pathlib import Path
payload={'prompt_sha256':hashlib.sha256(sys.stdin.buffer.read()).hexdigest(),'cwd':os.getcwd(),'output':os.environ['ROBERT_REVIEW_OUTPUT_DIR']}
Path(os.environ['ROBERT_REVIEW_OUTPUT_DIR'],'captured.json').write_text(json.dumps(payload))
''')
            observed = []
            with patch.dict(os.environ, {'ROBERT_REVIEW_OUTPUT_DIR': 'spoofed-parent-value'}):
                for agent in ('codex', 'claude'):
                    output = root / agent
                    output.mkdir()
                    result = dispatch_worker('task', agent, prompt, worktree_path=tree, model='default',
                        reasoning_effort='default', worker_agent_config={
                            'agent': agent, 'command_argv': [sys.executable, str(binary)],
                            'environment_allowlist': ['ROBERT_REVIEW_OUTPUT_DIR']}, dry_run=False,
                        trusted_environment={'ROBERT_REVIEW_OUTPUT_DIR': str(output)}, log_dir=output)
                    self.assertTrue(result['ok'], result)
                    if not result.get('process_exited'):
                        self.assertEqual(_BACKGROUND_PROCESSES.pop(result['pid']).wait(timeout=5), 0)
                    observed.append(json.loads((output / 'captured.json').read_text()))
                    self.assertEqual(os.environ['ROBERT_REVIEW_OUTPUT_DIR'], 'spoofed-parent-value')
                    self.assertEqual(Path(result['stdout_path']).parent, output)
            self.assertEqual(observed[0]['prompt_sha256'], observed[1]['prompt_sha256'])
            self.assertEqual(observed[0]['cwd'], observed[1]['cwd'])
            self.assertNotEqual(observed[0]['output'], observed[1]['output'])
