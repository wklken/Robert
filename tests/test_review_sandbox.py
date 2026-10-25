import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from robert_agent.review_sandbox import launch_policy


class ReviewSandboxTests(unittest.TestCase):
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

    @unittest.skipUnless(sys.platform == 'darwin' and shutil.which('sandbox-exec'), 'requires macOS sandbox-exec')
    def test_source_and_other_results_are_protected_but_own_output_is_writable(self):
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
    except PermissionError:
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
