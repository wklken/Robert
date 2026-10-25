"""OS-enforced file isolation for model processes; never silently bypass it."""
import json
import os
from pathlib import Path
import re
import shutil
import sys


def launch_policy(manifest, output_dir, data_dir, prompt_path, readable_files=()):
    output = Path(output_dir).resolve()
    scratch = output / '.tmp'
    scratch.mkdir(exist_ok=True)
    data = Path(data_dir).resolve()
    context = Path(manifest['context_dir']).resolve()
    tree = Path(manifest['worktree_path']).resolve()
    readable = [context, tree, Path(prompt_path).resolve(), *map(lambda p: Path(p).resolve(), readable_files)]
    environment = {'ROBERT_REVIEW_OUTPUT_DIR': str(output), 'TMPDIR': str(scratch),
                   'TMP': str(scratch), 'TEMP': str(scratch), 'PYTHONDONTWRITEBYTECODE': '1'}
    # CLIs may maintain their own auth/session caches. Shared source and Robert's
    # database/artifacts remain forbidden regardless of CLI permission flags.
    home = Path.home().resolve()
    writable = [output, scratch, home / '.codex', home / '.claude', home / '.claude.json']
    if os.environ.get('CODEX_HOME'):
        writable.append(Path(os.environ['CODEX_HOME']).resolve())
        environment['CODEX_HOME'] = str(writable[-1])
    if sys.platform == 'darwin' and shutil.which('sandbox-exec'):
        def rule(operation, paths):
            return '(' + operation + ' ' + ' '.join('(subpath ' + json.dumps(str(p)) + ')' for p in paths) + ')'
        profile = '\n'.join([
            '(version 1)', '(allow default)', '(deny file-write*)',
            rule('allow file-write*', writable), '(allow file-write* (literal "/dev/null"))',
            # Claude locks and atomically replaces its per-user config beside
            # the file, rather than inside ~/.claude. Permit only those names.
            '(allow file-write* (regex ' + json.dumps(
                '^' + re.escape(str(home / '.claude.json')) + r'\.(lock|tmp\.[0-9]+\.[0-9]+)$'
            ) + '))',
            rule('deny file-read* file-write*', [data]),
            rule('allow file-read*', readable), rule('allow file-read* file-write*', [output]),
            rule('deny file-write*', [tree, context]),
        ])
        profile_path = output / 'sandbox.sb'
        profile_path.write_text(profile, encoding='utf-8')
        return ['/usr/bin/sandbox-exec', '-f', str(profile_path)], environment
    if sys.platform.startswith('linux') and shutil.which('bwrap'):
        prefix = ['bwrap', '--die-with-parent', '--new-session', '--ro-bind', '/', '/',
                  '--dev-bind', '/dev', '/dev', '--proc', '/proc', '--tmpfs', '/tmp',
                  '--tmpfs', str(data)]
        # Re-expose only this stage's inputs after hiding Robert's data directory.
        for path in readable:
            prefix.extend(['--ro-bind', str(path), str(path)])
        for path in writable:
            if path.exists():
                prefix.extend(['--bind', str(path), str(path)])
        prefix.extend(['--chdir', str(tree), '--'])
        return prefix, environment
    raise RuntimeError('native review needs sandbox-exec on macOS or bubblewrap (bwrap) on Linux')
