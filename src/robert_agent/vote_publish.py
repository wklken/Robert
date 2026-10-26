"""Snapshot-bound, audited voting comments with stable round idempotency."""
import hashlib
import json
from pathlib import Path
from subprocess import SubprocessError

from robert_agent import review_publish, vote_files, vote_workspace


def is_vote(action):
    try:
        return bool(review_publish.payload(action).get('vote_round_id'))
    except ValueError:
        return False


def validate_action(action, scope):
    data = review_publish.payload(action)
    if not isinstance(scope, dict) or not scope.get('vote_publisher'):
        raise ValueError('vote report requires trusted scope and publishing account')
    if data.get('type') != 'comment' or data.get('target_url') != scope.get('vote_target_url'):
        raise ValueError('vote report target differs from trigger source')
    for field in ('vote_round_id', 'vote_snapshot_id', 'vote_report_path', 'vote_report_sha256'):
        if not data.get(field) or data[field] != scope.get(field):
            raise ValueError('vote report differs from trusted scope')
    for path_field, hash_field in [('vote_report_path', 'vote_report_sha256'), ('vote_manifest_path', 'vote_manifest_sha256')]:
        path = Path(scope.get(path_field, ''))
        if not path.is_absolute() or path.resolve() != path or path.is_symlink() or vote_files.digest(path) != scope.get(hash_field):
            raise ValueError('registered vote artifact changed')
    manifest = vote_files._read_json(scope['vote_manifest_path'])
    if (manifest.get('round_id') != data['vote_round_id'] or manifest.get('snapshot_id') != data['vote_snapshot_id']
            or not manifest.get('frozen')):
        raise ValueError('vote manifest identity mismatch')
    expected_target = f"https://github.com/{manifest['repo']}/{'pull' if manifest['source_type'] == 'pull_request' else 'issues'}/{manifest['number']}"
    if (expected_target != data['target_url'] or scope.get('repo', '').casefold() != manifest['repo'].casefold()
            or (manifest['source_type'], manifest['number']) not in {
                (s.get('source_type'), s.get('number')) for s in scope.get('sources', [])}):
        raise ValueError('vote target is outside trusted source scope')
    vote_workspace.verify_snapshot(manifest)
    body = Path(data['vote_report_path']).read_text()
    marker = f"<!-- robert-vote round={data['vote_round_id']} -->"
    if body != data.get('body') or body.count(marker) != 1 or '# 多 Agent 投票报告' not in body:
        raise ValueError('vote body or marker differs from registered report')
    return data, manifest, marker


def publication_keys(action, scope):
    data, manifest, _ = validate_action(action, scope)
    identity = {'type': 'vote_comment', 'repo': manifest['repo'].casefold(), 'source_type': manifest['source_type'],
                'number': manifest['number'], 'round': data['vote_round_id']}
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    request = hashlib.sha256(json.dumps([identity, data['vote_snapshot_id'], data['vote_report_sha256']], sort_keys=True).encode()).hexdigest()
    return 'publication:' + key, 'request:' + request


def reconcile(action, scope, run_command):
    try:
        data, manifest, marker = validate_action(action, scope)
        query = ['gh', 'api', f"repos/{manifest['repo']}/issues/{manifest['number']}/comments?per_page=100", '--paginate', '--slurp']
        completed = run_command(query, capture_output=True, text=True)
        if completed.returncode:
            raise ValueError('query failed')
        comments = list(review_publish._objects(json.loads(completed.stdout)))
        if any(not c.get('id') or not isinstance(c.get('body'), (str, type(None)))
               or not isinstance(c.get('user'), dict) or not c['user'].get('login') for c in comments):
            raise ValueError('malformed comment query response')
        matches = [c for c in comments if isinstance(c.get('body'), str) and marker in c['body']]
        if not matches:
            return {'status': 'confirmed_absent', 'reason_code': 'vote_lookup_complete_absent', 'evidence': {}}
        if len(matches) != 1:
            raise ValueError('vote marker conflict')
        comment = matches[0]
        if (not comment.get('id') or comment.get('body') != data['body']
                or comment.get('user', {}).get('login', '').casefold() != scope['vote_publisher'].casefold()):
            raise ValueError('vote account or body conflict')
        return {'status': 'confirmed_effect', 'reason_code': 'vote_round_marker_match',
                'evidence': {'external_id': str(comment['id']), 'target_url': comment.get('html_url') or data['target_url']}}
    except (ValueError, OSError, TypeError, KeyError):
        return {'status': 'unknown', 'reason_code': 'vote_reconciliation_unknown', 'evidence': {}}


def publish(action, scope, run_command, scope_guard=None):
    try:
        data, manifest, _ = validate_action(action, scope)
    except (ValueError, OSError, TypeError, KeyError):
        return {'ok': False, 'status': 'publish_failed', 'retryable': False, 'reason_code': 'vote_scope_invalid',
                'safe_error': 'Vote scope or registered artifacts changed.'}
    existing = reconcile(action, scope, run_command)
    if existing['status'] == 'confirmed_effect':
        return {'ok': True, 'status': 'published', 'deduplicated': True, **existing['evidence']}
    if existing['status'] == 'unknown':
        return {'ok': False, 'status': 'publish_failed', 'reason_code': 'vote_lookup_unknown',
                'safe_error': 'Vote lookup is unknown; reconcile before posting again.'}
    try:
        # Complete paginated snapshots distinguish missing adopted comments from
        # failed lookups and avoid one request per historical comment.
        current = vote_workspace.read_source(manifest['repo'], manifest['number'], manifest['source_type'],
                                             run_command=run_command)
        same = vote_workspace.same_sources(manifest, current)
        if manifest.get('source_worktree') and manifest['source_type'] == 'issue':
            same = same and vote_workspace.api(manifest['repo'], 'commits/HEAD', run_command=run_command).get('sha') == manifest['head_sha']
    except (ValueError, OSError, TypeError, KeyError, SubprocessError):
        return {'ok': False, 'status': 'publish_failed', 'reason_code': 'vote_source_query_failed',
                'safe_error': 'Cannot verify current vote source.'}
    if not same:
        return {'ok': True, 'status': 'superseded', 'reason_code': 'vote_snapshot_changed',
                'safe_error': 'Vote inputs changed; request a new round.'}
    if scope_guard:
        guarded = scope_guard()
        if guarded:
            return guarded
    try:
        validate_action(action, scope)
    except (ValueError, OSError, TypeError):
        return {'ok': False, 'status': 'publish_failed', 'retryable': False, 'safe_error': 'Vote artifacts changed.'}
    result = run_command(['gh', 'api', f"repos/{manifest['repo']}/issues/{manifest['number']}/comments", '--method', 'POST',
                          '-f', 'body=' + data['body']], capture_output=True, text=True)
    try:
        response = json.loads(result.stdout) if result.returncode == 0 else {}
        if (not isinstance(response, dict) or not response.get('id') or response.get('body') != data['body']
                or response.get('user', {}).get('login', '').casefold() != scope['vote_publisher'].casefold()):
            raise ValueError('ambiguous vote POST')
    except (ValueError, TypeError):
        return {'ok': False, 'status': 'publish_failed', 'reason_code': 'vote_post_ambiguous',
                'safe_error': 'Vote POST outcome is unknown; reconcile before retry.'}
    return {'ok': True, 'status': 'published', 'external_id': str(response['id']),
            'target_url': response.get('html_url') or data['target_url'], 'response': {'comment_id': response['id']}}


def record_outcome(conn, action, status, now):
    if is_vote(action) and status in {'published', 'superseded'}:
        conn.execute('UPDATE vote_sessions SET status=?,updated_at=? WHERE round_id=?',
                     (status, now, review_publish.payload(action)['vote_round_id']))
