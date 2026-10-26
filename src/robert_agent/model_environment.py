"""Per-stage output and CLI configuration for directly launched model processes."""
import os
from pathlib import Path


def stage_environment(manifest, output_dir, data_dir, *, workflow_name):
    output = Path(output_dir).resolve()
    scratch = output / '.tmp'
    scratch.mkdir(exist_ok=True)
    environment = {
        f'ROBERT_{workflow_name.upper()}_OUTPUT_DIR': str(output),
        'TMPDIR': str(scratch), 'TMP': str(scratch), 'TEMP': str(scratch),
        'PYTHONDONTWRITEBYTECODE': '1',
    }
    if os.environ.get('CODEX_HOME'):
        environment['CODEX_HOME'] = str(Path(os.environ['CODEX_HOME']).resolve())
    if os.environ.get('CLAUDE_CONFIG_DIR'):
        config = Path(os.environ['CLAUDE_CONFIG_DIR']).expanduser().resolve()
        protected = [Path(data_dir).resolve(), Path(manifest['worktree_path']).resolve(),
                     Path(manifest['context_dir']).resolve()]
        if any(config == path or config in path.parents or path in config.parents for path in protected):
            raise RuntimeError('CLAUDE_CONFIG_DIR must not overlap review inputs or Robert data')
        if not config.is_dir():
            raise RuntimeError('CLAUDE_CONFIG_DIR must be an existing, authenticated Claude configuration directory')
        environment['CLAUDE_CONFIG_DIR'] = str(config)
    return environment
