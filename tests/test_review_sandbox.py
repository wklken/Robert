import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from uuid import uuid4
from unittest.mock import patch

from robert_agent.review_sandbox import launch_policy


class ReviewSandboxTests(unittest.TestCase):
    def test_linux_uses_pid_namespace_for_descendant_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / 'output'
            output.mkdir()
            with patch('robert_agent.review_sandbox.sys.platform', 'linux'), patch(
                    'robert_agent.review_sandbox.shutil.which', return_value='/usr/bin/bwrap'):
                prefix, _ = launch_policy({'context_dir': str(root / 'context'),
                                           'worktree_path': str(root / 'tree')},
                                          output, root / 'data', root / 'prompt')
            self.assertIn('--unshare-pid', prefix)
            self.assertIn('--die-with-parent', prefix)

    def test_custom_claude_config_directory_is_writable_on_both_platforms(self):
        for platform in ('linux', 'darwin'):
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp).resolve()
                output, custom = root / 'output', root / 'custom-claude'
                output.mkdir()
                custom.mkdir()
                with patch('robert_agent.review_sandbox.sys.platform', platform), patch(
                        'robert_agent.review_sandbox.shutil.which', return_value='sandbox'), patch.dict(
                        os.environ, {'CLAUDE_CONFIG_DIR': str(custom)}):
                    prefix, environment = launch_policy({'context_dir': str(root / 'context'),
                                                          'worktree_path': str(root / 'tree')},
                                                         output, root / 'data', root / 'prompt')
                self.assertEqual(environment.get('CLAUDE_CONFIG_DIR'), str(custom))
                if platform == 'linux':
                    self.assertIn(['--bind', str(custom), str(custom)],
                                  [prefix[index:index + 3] for index in range(len(prefix))])
                else:
                    self.assertIn(str(custom), (output / 'sandbox.sb').read_text())

    def test_linux_claude_requires_explicit_config_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / 'output'
            output.mkdir()
            with patch('robert_agent.review_sandbox.sys.platform', 'linux'), patch(
                    'robert_agent.review_sandbox.shutil.which', return_value='/usr/bin/bwrap'), patch.dict(
                    os.environ, {'CLAUDE_CONFIG_DIR': ''}):
                for adapter in ('claude', 'tclaude'):
                    with self.subTest(adapter=adapter), self.assertRaisesRegex(RuntimeError, 'CLAUDE_CONFIG_DIR'):
                        launch_policy({'context_dir': str(root / 'context'), 'worktree_path': str(root / 'tree')},
                                      output, root / 'data', root / 'prompt', worker_agent=adapter)

    def test_custom_claude_config_cannot_expose_protected_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            output = root / 'output'
            output.mkdir()
            for config in (root, root / 'tree', root / 'data' / 'private'):
                config.mkdir(parents=True, exist_ok=True)
                with self.subTest(config=config), patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(config)}):
                    with self.assertRaisesRegex(RuntimeError, 'must not overlap'):
                        launch_policy({'context_dir': str(root / 'context'), 'worktree_path': str(root / 'tree')},
                                      output, root / 'data', root / 'prompt', worker_agent='claude')

    @unittest.skipUnless(sys.platform.startswith('linux'), 'requires Linux bubblewrap')
    def test_linux_termination_stops_detached_tool_descendants(self):
        self.require_bwrap()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            output, context, tree = [root / name for name in ('output', 'context', 'tree')]
            for path in (output, context, tree):
                path.mkdir()
            prompt = context / 'prompt'
            prompt.write_text('test')
            prefix, environment = launch_policy({'context_dir': str(context), 'worktree_path': str(tree)},
                                                 output, root / 'data', prompt)
            token = 'robert-sandbox-tool-' + uuid4().hex
            code = '''
import os,subprocess,sys,time
tool="""import os,signal,time
from pathlib import Path
signal.signal(signal.SIGTERM,signal.SIG_IGN)
while True:
    Path(os.environ['ROBERT_REVIEW_OUTPUT_DIR'],'heartbeat').write_text(str(time.monotonic()))
    time.sleep(.02)
"""
subprocess.Popen([sys.executable,'-c',tool,os.environ['TOOL_TOKEN']])
while True:
    time.sleep(.1)
'''
            process = subprocess.Popen([*prefix, sys.executable, '-c', code], start_new_session=True,
                                       env={**os.environ, **environment, 'TOOL_TOKEN': token},
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            tool_pids = []
            try:
                deadline = time.monotonic() + 5
                while not (output / 'heartbeat').exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue((output / 'heartbeat').exists(), process.communicate(timeout=1) if process.poll() is not None else 'tool did not start')
                found = subprocess.run(['pgrep', '-f', token], capture_output=True, text=True, check=True)
                tool_pids = [int(pid) for pid in found.stdout.split()]
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=5)
                deadline = time.monotonic() + 3
                while any(self.process_is_live(pid) for pid in tool_pids) and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(all(not self.process_is_live(pid) for pid in tool_pids), 'sandbox tool survived bwrap termination')
            finally:
                for pid in tool_pids:
                    if self.process_is_live(pid):
                        os.kill(pid, signal.SIGKILL)
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                process.communicate(timeout=5)

    def require_bwrap(self):
        if not shutil.which('bwrap'):
            if os.environ.get('ROBERT_REQUIRE_LINUX_SANDBOX_TESTS'):
                self.fail('CI requires bubblewrap sandbox tests')
            self.skipTest('bubblewrap is not installed')

    @staticmethod
    def process_is_live(pid):
        result = subprocess.run(['ps', '-p', str(pid), '-o', 'stat='], capture_output=True, text=True)
        return bool(result.stdout.strip() and result.stdout.strip()[0] not in 'ZX')

    @unittest.skipUnless((sys.platform == 'darwin' and shutil.which('sandbox-exec')) or
                         sys.platform.startswith('linux'), 'requires native sandbox')
    def test_custom_claude_config_supports_lock_and_atomic_update(self):
        if sys.platform.startswith('linux'):
            self.require_bwrap()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            home, custom, output, context, tree = [root / name for name in ('home', 'custom', 'output', 'context', 'tree')]
            for path in (home, custom, output, context, tree):
                path.mkdir()
            prompt = context / 'prompt'
            prompt.write_text('test')
            (custom / '.claude.json').write_text('{"existing": true}')
            with patch('robert_agent.review_sandbox.Path.home', return_value=home), patch.dict(
                    os.environ, {'CLAUDE_CONFIG_DIR': str(custom)}):
                prefix, env = launch_policy({'context_dir': str(context), 'worktree_path': str(tree)},
                                            output, root / 'data', prompt, worker_agent='claude')
            code = '''
import os
from pathlib import Path
config=Path(os.environ['CLAUDE_CONFIG_DIR'])
assert (config/'.claude.json').read_text() == '{"existing": true}'
(config/'.claude.json.lock').write_text('lock')
(config/'.claude.json.tmp.123.456').write_text('{}')
(config/'.claude.json.tmp.123.456').replace(config/'.claude.json')
try:
    Path(os.environ['TEST_HOME'],'unrelated').write_text('forbidden')
except OSError:
    pass
else:
    raise AssertionError('unrelated home writes must stay forbidden')
'''
            result = subprocess.run([*prefix, sys.executable, '-c', code], env={**os.environ, **env,
                                    'TEST_HOME': str(home)}, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((custom / '.claude.json').read_text(), '{}')

    @unittest.skipUnless(sys.platform == 'darwin' and shutil.which('sandbox-exec'), 'requires macOS sandbox-exec')
    def test_claude_can_lock_and_atomically_update_only_its_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            home, output, context, tree = [root / name for name in ('home', 'output', 'context', 'tree')]
            for path in (home, output, context, tree):
                path.mkdir()
            prompt = context / 'prompt'
            prompt.write_text('test')
            with patch('robert_agent.review_sandbox.Path.home', return_value=home):
                prefix, env = launch_policy({'context_dir': str(context), 'worktree_path': str(tree)},
                                            output, root / 'data', prompt)
            code = '''
import os
from pathlib import Path
home=Path(os.environ['TEST_HOME'])
(home/'.claude.json.lock').write_text('lock')
(home/'.claude.json.tmp.123.456').write_text('{}')
(home/'.claude.json.tmp.123.456').replace(home/'.claude.json')
try:
    (home/'unrelated').write_text('forbidden')
except PermissionError:
    pass
else:
    raise AssertionError('unrelated home writes must stay forbidden')
'''
            result = subprocess.run([*prefix, sys.executable, '-c', code], env={**os.environ, **env, 'TEST_HOME': str(home)}, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless((sys.platform == 'darwin' and shutil.which('sandbox-exec')) or
                         sys.platform.startswith('linux'), 'requires native sandbox')
    def test_source_and_other_results_are_protected_but_own_output_is_writable(self):
        if sys.platform.startswith('linux'):
            self.require_bwrap()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            data, tree = root / 'data', root / 'worktree'
            context, output, other = data / 'context', data / 'runs' / 'own', data / 'runs' / 'other'
            for path in (context, output, other, tree):
                path.mkdir(parents=True)
            source = tree / 'source.txt'
            source.write_text('source')
            (context / 'diff').write_text('diff')
            (other / 'result').write_text('other private finding')
            prompt = data / 'reviewer-prompt.md'
            prompt.write_text('identical prompt')
            prefix, environment = launch_policy({'worktree_path': str(tree), 'context_dir': str(context)},
                                                  output, data, prompt)
            code = '''
import os
from pathlib import Path
assert Path(os.environ['SOURCE']).read_text() == 'source'
assert Path(os.environ['CONTEXT']).read_text() == 'diff'
for path, operation in [(os.environ['SOURCE'], 'w'), (os.environ['CONTEXT'], 'w'), (os.environ['OTHER'], 'r')]:
    try:
        with open(path, operation) as stream:
            stream.write('corrupt') if operation == 'w' else stream.read()
    except OSError:
        pass
    else:
        raise AssertionError('sandbox allowed forbidden access: ' + path)
Path(os.environ['ROBERT_REVIEW_OUTPUT_DIR'], 'result').write_text('own result')
'''
            result = subprocess.run([*prefix, sys.executable, '-c', code], cwd=tree,
                env={**os.environ, **environment, 'SOURCE': str(source), 'CONTEXT': str(context / 'diff'),
                     'OTHER': str(other / 'result')}, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((output / 'result').read_text(), 'own result')
            self.assertEqual(source.read_text(), 'source')

    def test_unavailable_sandbox_fails_closed(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp, patch('robert_agent.review_sandbox.shutil.which', return_value=None):
            root = Path(tmp)
            output = root / 'output'
            output.mkdir()
            with self.assertRaisesRegex(RuntimeError, 'sandbox-exec'):
                launch_policy({'worktree_path': str(root / 'tree'), 'context_dir': str(root / 'context')},
                              output, root, root / 'prompt')
