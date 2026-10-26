"""Versioned discussion inputs with optional immutable source worktrees."""
import hashlib
import json
from pathlib import Path
import subprocess

from robert_agent import review_workspace, vote_files


def api(repo, endpoint, *, run_command=None):
    completed = (run_command or subprocess.run)(['gh', 'api', f'repos/{repo}/{endpoint}'],
                               capture_output=True, text=True, timeout=60)
    if completed.returncode:
        raise ValueError('vote source query failed')
    return json.loads(completed.stdout)


def read_source(repo, number, source_type, *, run_command=None):
    endpoint = f'{"pulls" if source_type == "pull_request" else "issues"}/{number}'
    value = api(repo, endpoint, run_command=run_command) if run_command else api(repo, endpoint)
    if not isinstance(value, dict) or value.get('number') != number:
        raise ValueError('invalid GitHub vote source')
    discussion = []
    endpoints = [('comment', f'issues/{number}/comments')]
    if source_type == 'pull_request':
        endpoints.extend([('review_comment', f'pulls/{number}/comments'), ('review', f'pulls/{number}/reviews')])
    for kind, endpoint in endpoints:
        result = (run_command or subprocess.run)(['gh', 'api', f'repos/{repo}/{endpoint}?per_page=100', '--paginate', '--slurp'],
                                capture_output=True, text=True, timeout=60)
        if result.returncode:
            raise ValueError('vote discussion query failed')
        pages = json.loads(result.stdout)
        if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
            raise ValueError('invalid paginated vote discussion')
        for page in pages:
            for comment in page:
                if not isinstance(comment, dict) or not comment.get('id'):
                    raise ValueError('invalid discussion item')
                discussion.append({**comment, 'kind': kind})
    value['discussion'] = discussion
    return value


def source_records(source, event):
    records = {'source-1': {'kind': 'target', 'title': source.get('title') or '',
                            'body': source.get('body') or '', 'url': source.get('html_url') or event.get('html_url')}}
    cutoff = max([e.get('event_at') or '' for e in [event, *event.get('clarifications', [])]])
    for comment in source.get('discussion', []):
        if cutoff and (comment.get('created_at') or comment.get('submitted_at')) and (comment.get('created_at') or comment.get('submitted_at')) > cutoff:
            continue
        key = f"{comment.get('kind', 'comment')}:{comment['id']}"
        records[key] = {'kind': comment.get('kind', 'comment'), 'id': comment['id'],
                        'body': comment.get('body') or '', 'url': comment.get('html_url')}
    fingerprint = event['event_fingerprint']
    if fingerprint.split(':')[-1].isdigit() and event.get('event_type') in {'comment', 'review_comment', 'review'}:
        if fingerprint not in records or records[fingerprint]['body'] != event['body']:
            raise ValueError('vote request was edited or deleted before preparation')
    return records


def same_sources(manifest, current):
    if current.get('state') != 'open' or current.get('merged'):
        return False
    if manifest['source_type'] == 'pull_request' and not (
            current.get('head', {}).get('sha') == manifest.get('head_sha')
            and current.get('base', {}).get('sha') == manifest.get('base_sha')
            and current.get('base', {}).get('ref') == manifest.get('base_ref')):
        return False
    available = {'source-1': {'kind': 'target', 'title': current.get('title') or '', 'body': current.get('body') or ''}}
    for comment in current.get('discussion', []):
        key = f"{comment.get('kind', 'comment')}:{comment['id']}"
        available[key] = {'kind': comment.get('kind', 'comment'), 'id': comment['id'], 'body': comment.get('body') or ''}
    for key, value in manifest['sources'].items():
        if key not in available or any(available[key].get(field) != entry for field, entry in value.items() if field != 'url'):
            return False
    return True


def prepare(repo, data_dir, round_id, event, workers, source):
    if source.get('state') != 'open' or source.get('merged'):
        raise ValueError('vote source is closed')
    root = Path(data_dir) / 'artifacts' / 'votes' / hashlib.sha256(repo['full_name'].encode()).hexdigest()[:16] / round_id
    context = root / 'context'
    context.mkdir(parents=True, exist_ok=True)
    saved = context / 'manifest.json'
    if saved.exists():
        manifest = vote_files._read_json(saved)
        if manifest.get('round_id') != round_id or manifest.get('event', {}).get('event_fingerprint') != event['event_fingerprint']:
            raise ValueError('vote manifest identity changed')
        verify_snapshot(manifest)
        if manifest['event'] == event:
            return manifest
        if manifest.get('frozen'):
            raise ValueError('cannot change a frozen vote request')
    sources = source_records(source, event)
    snapshot = hashlib.sha256(json.dumps([round_id, event, sources, source.get('head'), source.get('base')], sort_keys=True).encode()).hexdigest()[:24]
    manifest = {'schema_version': 1, 'round_id': round_id, 'snapshot_id': snapshot,
                'repo': repo['full_name'], 'number': event['number'], 'source_type': event['source_type'],
                'event': event, 'workers': workers, 'sources': sources, 'artifact_root': str(root),
                'context_dir': str(context), 'worktree_path': str(context), 'source_worktree': None,
                'files': {}, 'frozen': False}
    if event['source_type'] == 'pull_request':
        code = review_workspace.prepare_snapshot(repo['repo_root'], repo['worktree_root'], root / 'code',
                                                round_id, source, [event])
        manifest['code_manifest'] = code
        manifest.update({key: code[key] for key in ('head_sha', 'base_sha', 'base_ref')})
        manifest['source_worktree'] = code['worktree_path']
        manifest['worktree_path'] = code['worktree_path']
    for name, value in [('request.json', event), ('sources.json', sources)]:
        path = context / name
        vote_files.atomic_json(path, value)
        manifest['files'][str(path)] = vote_files.digest(path)
    vote_files.atomic_json(saved, manifest)
    return manifest


def add_issue_code(manifest, repo):
    git = review_workspace._git
    ref = f"refs/robert/vote-rounds/{manifest['round_id']}/head"
    git(repo['repo_root'], 'fetch', 'upstream', f'+HEAD:{ref}')
    sha = git(repo['repo_root'], 'rev-parse', ref).strip()
    # Siblings preserve both prior revisions and the legacy vote/<round> layout.
    tree = Path(repo['worktree_root']) / 'vote' / f"{manifest['round_id']}-{sha}"
    if not tree.exists():
        tree.parent.mkdir(parents=True, exist_ok=True)
        git(repo['repo_root'], 'worktree', 'add', '--detach', str(tree), sha)
    manifest.update(source_worktree=str(tree), worktree_path=str(tree), head_sha=sha,
                    source_hashes=review_workspace._source_hashes(tree))
    # Still before ballot freeze: a second prepare gets the source it requested.
    manifest['snapshot_id'] = hashlib.sha256((manifest['snapshot_id'] + sha).encode()).hexdigest()[:24]
    vote_files.atomic_json(Path(manifest['context_dir']) / 'manifest.json', manifest)


def verify_snapshot(manifest):
    for name, expected in manifest['files'].items():
        path = vote_files.safe_path(name, manifest['artifact_root'])
        if vote_files.digest(path) != expected:
            raise ValueError('vote context file changed')
    if manifest.get('code_manifest'):
        review_workspace.verify_snapshot(manifest['code_manifest'])
    elif manifest.get('source_worktree'):
        tree = Path(manifest['source_worktree'])
        if (review_workspace._git(tree, 'rev-parse', 'HEAD').strip() != manifest['head_sha']
                or review_workspace._git(tree, 'status', '--porcelain=v1', '--untracked-files=all').strip()
                or review_workspace._git(tree, 'ls-files', '--others', '-z')
                or review_workspace._source_hashes(tree) != manifest['source_hashes']):
            raise ValueError('vote source worktree changed')
    if manifest.get('frozen'):
        if (vote_files.digest(manifest['prompt_path']) != manifest['prompt_sha256']
                or Path(manifest['prompt_path']).read_text() != vote_files.voter_prompt(manifest)
                or vote_files._read_json(Path(manifest['context_dir']) / 'manifest.json') != manifest):
            raise ValueError('frozen vote manifest or prompt changed')
    return True


def freeze(manifest, prepared):
    verify_snapshot(manifest)
    spec = {**prepared, 'participants': [w['name'] for w in manifest['workers']], 'method': 'single_choice_equal'}
    path = Path(manifest['context_dir']) / 'ballot-spec.json'
    vote_files.atomic_json(path, spec)
    frozen = {**manifest, 'files': {**manifest['files'], str(path): vote_files.digest(path)},
              'spec': spec, 'ballot_spec_sha256': vote_files.digest(path), 'frozen': True}
    prompt = Path(manifest['artifact_root']) / 'voter-prompt.md'
    content = vote_files.voter_prompt(frozen)
    if prompt.exists() and prompt.read_text() != content:
        raise ValueError('existing voter prompt differs')
    prompt.write_text(content)
    frozen.update(prompt_path=str(prompt), prompt_sha256=vote_files.digest(prompt))
    vote_files.atomic_json(Path(manifest['context_dir']) / 'manifest.json', frozen)
    return frozen
