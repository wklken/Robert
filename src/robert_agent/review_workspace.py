"""Immutable, round-specific Git source and context bundles for PR review."""

import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess

from robert_agent import review_files


CONTEXT_FILES = ('pr-info.json', 'github-context.json', 'changed-files.txt', 'diff.patch',
                 'diff-stat.txt', 'diff-linecount.txt', 'runtime-info.json')
PRIMARY_FILES = ('context-summary.md', 'source-index.json')


def _git(repo, *args):
    return subprocess.run(['git', *args], cwd=repo, check=True, capture_output=True, text=True).stdout


def _digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _file_digest(path):
    if path.is_symlink():
        return _digest('symlink:' + str(path.readlink()))
    return review_files.sha256_file(path)


def _source_hashes(tree):
    names = _git(tree, 'ls-files', '-z').split('\0')
    result = {}
    for name in names:
        if not name:
            continue
        path = tree / name
        # Submodules are represented by their Git commit, never followed.
        if path.is_dir() and not path.is_symlink():
            result[name] = 'gitlink:' + _git(path, 'rev-parse', 'HEAD').strip()
        else:
            result[name] = _file_digest(path)
    return result


def _runtime_info(tree):
    declarations = {}
    for name in ('.python-version', 'pyproject.toml', 'setup.cfg', 'Pipfile'):
        path = tree / name
        if path.is_file() and not path.is_symlink():
            text = path.read_text(encoding='utf-8', errors='replace')
            if name == '.python-version':
                declarations[name] = text.strip()
            else:
                matches = re.findall(r'(?m)^\s*(?:requires-python|python_requires|python_version)\s*=\s*(.+)$', text)
                if matches:
                    declarations[name] = matches
    python = {'status': 'UNRESOLVED', 'interpreter': None, 'declarations': declarations,
              'reason': 'No exact project interpreter has been verified; do not use system Python for syntax claims.'}
    version = declarations.get('.python-version', '')
    if re.fullmatch(r'3\.\d+(?:\.\d+)?', version):
        major_minor = '.'.join(version.split('.')[:2])
        interpreter = shutil.which('python' + major_minor)
        if interpreter:
            reported = subprocess.run([interpreter, '--version'], capture_output=True, text=True, check=True).stdout.strip()
            if reported.removeprefix('Python ').startswith(version):
                python.update(status='RESOLVED', interpreter=str(Path(interpreter).resolve()),
                              version=reported, reason='Verified executable matches .python-version.')
    return {'python': python}


def _trigger(github_context):
    if isinstance(github_context, list):
        event = github_context[0] if github_context else {}
    else:
        event = github_context or {}
    if not isinstance(event, dict):
        return {}
    return {'trigger_event_fingerprint': event.get('event_fingerprint', ''),
            'trigger_url': event.get('url') or event.get('html_url') or event.get('source_url', '')}


def prepare_snapshot(repo_root, worktree_root, artifact_root, round_id, pr_info, github_context):
    """Fetch a captured PR revision and create a new detached worktree.

    ``artifact_root`` is the configured review artifact base. Returned paths add
    round_id/snapshot_id. No existing worktree is reset, reused, or overwritten.
    GitHub metadata must be captured by the trusted caller before this function.
    """
    if not isinstance(round_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', round_id):
        raise ValueError('invalid review round_id')
    number = pr_info.get('number')
    if type(number) is not int or number < 1:
        raise ValueError('PR number must be a positive integer')
    head = pr_info.get('head') or {}
    base = pr_info.get('base') or {}
    head_sha, base_sha, base_ref = head.get('sha'), base.get('sha'), base.get('ref')
    for value in (head_sha, base_sha):
        if not isinstance(value, str) or not re.fullmatch(r'[a-fA-F0-9]{40}|[a-fA-F0-9]{64}', value):
            raise ValueError('PR head/base SHA must be complete Git object ids')
    if not isinstance(base_ref, str) or base_ref.startswith('-'):
        raise ValueError('invalid PR base branch')
    repo_root = Path(repo_root).resolve()
    worktree_root = Path(worktree_root).resolve()
    artifact_root = Path(artifact_root).resolve()
    _git(repo_root, 'check-ref-format', '--branch', base_ref)
    head_ref = f'refs/robert/review-rounds/{round_id}/head'
    base_local_ref = f'refs/robert/review-rounds/{round_id}/base'
    _git(repo_root, 'fetch', 'upstream', f'+pull/{number}/head:{head_ref}')
    _git(repo_root, 'fetch', 'upstream', f'+refs/heads/{base_ref}:{base_local_ref}')
    if _git(repo_root, 'rev-parse', head_ref).strip() != head_sha or _git(repo_root, 'rev-parse', base_local_ref).strip() != base_sha:
        raise ValueError('PR snapshot changed while fetching; recapture PR metadata')
    merge_base = _git(repo_root, 'merge-base', base_sha, head_sha).strip()
    snapshot_id = _digest(json.dumps([round_id, number, head_sha, base_sha, base_ref, merge_base]))[:24]
    tree = worktree_root / 'review' / f'pr-{number}' / round_id / snapshot_id
    artifacts = artifact_root / round_id / snapshot_id
    context = artifacts / 'context'
    if tree.exists() or artifacts.exists():
        saved_path = review_files.safe_path(context / 'manifest.json', artifact_root)
        saved = review_files._read_json(saved_path)
        expected = {'round_id': round_id, 'snapshot_id': snapshot_id, 'head_sha': head_sha,
                    'base_sha': base_sha, 'base_ref': base_ref, 'merge_base_sha': merge_base,
                    'pr_number': number, 'worktree_path': str(tree), 'context_dir': str(context),
                    'artifact_root': str(artifacts)}
        if any(saved.get(key) != value for key, value in expected.items()):
            raise ValueError('existing round snapshot identity does not match')
        if _trigger(github_context).get('trigger_event_fingerprint') != saved.get('trigger_event_fingerprint'):
            raise ValueError('existing round trigger identity does not match')
        verify_snapshot(saved)
        return saved
    tree.parent.mkdir(parents=True, exist_ok=True)
    context.mkdir(parents=True)
    _git(repo_root, 'worktree', 'add', '--detach', str(tree), head_sha)
    diff = _git(tree, 'diff', '--no-ext-diff', '--no-textconv', '--binary', merge_base, head_sha, '--')
    content = {
        'pr-info.json': json.dumps(pr_info, ensure_ascii=False, indent=2) + '\n',
        'github-context.json': json.dumps(github_context, ensure_ascii=False, indent=2) + '\n',
        'changed-files.txt': _git(tree, 'diff', '--no-ext-diff', '--name-only', merge_base, head_sha, '--'),
        'diff.patch': diff,
        'diff-stat.txt': _git(tree, 'diff', '--no-ext-diff', '--stat', merge_base, head_sha, '--'),
        'diff-linecount.txt': str(len(diff.splitlines())) + '\n',
        'runtime-info.json': json.dumps(_runtime_info(tree), ensure_ascii=False, indent=2) + '\n',
    }
    for name, text in content.items():
        (context / name).write_text(text, encoding='utf-8')
    manifest = {
        'schema_version': 1, 'context_version': 1, 'prompt_version': 1,
        'round_id': round_id, 'snapshot_id': snapshot_id, 'head_sha': head_sha,
        'base_sha': base_sha, 'base_ref': base_ref, 'merge_base_sha': merge_base,
        'pr_number': number, 'repo': (base.get('repo') or {}).get('full_name', ''),
        'worktree_path': str(tree), 'context_dir': str(context), 'artifact_root': str(artifacts),
        'source_hashes': _source_hashes(tree),
        'files': {str(context / name): review_files.sha256_file(context / name) for name in CONTEXT_FILES},
        'frozen': False, **_trigger(github_context),
    }
    review_files.atomic_json(context / 'manifest.json', manifest)
    return manifest


def verify_snapshot(manifest):
    tree = Path(manifest['worktree_path'])
    try:
        if _git(tree, 'rev-parse', 'HEAD').strip() != manifest['head_sha']:
            raise ValueError('review worktree HEAD changed')
        if _git(tree, 'status', '--porcelain=v1', '--untracked-files=all', '--ignore-submodules=none').strip():
            raise ValueError('review worktree source is dirty')
        if _git(tree, 'ls-files', '--others', '-z'):
            raise ValueError('review worktree contains unregistered files, including ignored files')
        if _source_hashes(tree) != manifest['source_hashes']:
            raise ValueError('review worktree source hash changed')
        for name, expected in manifest['files'].items():
            path = review_files.safe_path(name, manifest['artifact_root'])
            if review_files.sha256_file(path) != expected:
                raise ValueError('review context file hash changed')
        if manifest.get('frozen'):
            path = review_files.safe_path(manifest['prompt_path'], manifest['artifact_root'])
            if review_files.sha256_file(path) != manifest['prompt_sha256']:
                raise ValueError('review prompt hash changed')
            if path.read_text(encoding='utf-8') != review_files.build_reviewer_prompt(manifest):
                raise ValueError('review prompt no longer matches the built-in template')
            saved = json.loads(Path(manifest['context_dir'], 'manifest.json').read_text(encoding='utf-8'))
            if saved != manifest:
                raise ValueError('review manifest changed')
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        raise ValueError('review snapshot is missing or invalid') from exc
    return True


def freeze_context(manifest):
    """Validate primary-prepared files and freeze one shared reviewer prompt."""
    verify_snapshot(manifest)
    if manifest.get('frozen'):
        return manifest
    saved_path = review_files.safe_path(Path(manifest['context_dir']) / 'manifest.json', manifest['artifact_root'])
    saved = review_files._read_json(saved_path)
    if saved.get('frozen'):
        # The manifest may have committed before the controller registered it.
        excluded = {'files', 'frozen', 'prompt_path', 'prompt_sha256'}
        if any(saved.get(key) != value for key, value in manifest.items() if key not in excluded):
            raise ValueError('frozen manifest identity differs from prepared context')
        if any(saved.get('files', {}).get(key) != value for key, value in manifest['files'].items()):
            raise ValueError('frozen manifest changed prepared file hashes')
        expected_files = set(manifest['files']) | {str(Path(manifest['context_dir']) / name) for name in PRIMARY_FILES}
        if set(saved.get('files', {})) != expected_files:
            raise ValueError('frozen manifest has unexpected context files')
        verify_snapshot(saved)
        return saved
    context = Path(manifest['context_dir'])
    summary = review_files.safe_path(context / 'context-summary.md', manifest['artifact_root'])
    index_path = review_files.safe_path(context / 'source-index.json', manifest['artifact_root'])
    try:
        if not summary.read_text(encoding='utf-8').strip():
            raise ValueError('primary context summary is empty')
        index = json.loads(index_path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError('primary context files are missing or invalid') from exc
    if not isinstance(index, dict) or not isinstance(index.get('files'), list):
        raise ValueError('source-index requires files array')
    if not isinstance(index.get('reading_notes'), str) or not index['reading_notes'].strip():
        raise ValueError('source-index requires actual reading_notes')
    tree = Path(manifest['worktree_path'])
    for entry in index['files']:
        if not isinstance(entry, dict) or not isinstance(entry.get('reason'), str) or not entry['reason'].strip():
            raise ValueError('source-index entries require path and reason')
        name = review_files._source_path(entry.get('path'))
        path = review_files.safe_path(tree / name, tree)
        if not path.is_file():
            raise ValueError('source-index references a missing source file')
    frozen = {**manifest, 'files': dict(manifest['files']), 'frozen': True}
    for name in PRIMARY_FILES:
        frozen['files'][str(context / name)] = review_files.sha256_file(context / name)
    prompt_path = Path(manifest['artifact_root']) / 'reviewer-prompt.md'
    review_files.safe_path(prompt_path, manifest['artifact_root'])
    if prompt_path.exists():
        raise ValueError('unregistered reviewer prompt already exists')
    prompt_path.write_text(review_files.build_reviewer_prompt(frozen), encoding='utf-8')
    frozen.update(prompt_path=str(prompt_path), prompt_sha256=review_files.sha256_file(prompt_path))
    review_files.atomic_json(context / 'manifest.json', frozen)
    return frozen
