"""Audited, revision-bound GitHub PR Review COMMENT publication."""

import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlparse


SCOPE_FIELDS = {
    'round_id': 'review_round_id', 'commit_id': 'review_head_sha',
    'base_sha': 'review_base_sha', 'base_ref': 'review_base_ref',
    'report_path': 'review_report_path', 'report_sha256': 'review_report_sha256',
}


def payload(action):
    if 'metadata_json' in action:
        try:
            result = json.loads(action['metadata_json'] or '{}')
        except (ValueError, TypeError) as exc:
            raise ValueError('invalid review action metadata') from exc
        if not isinstance(result, dict):
            raise ValueError('review action metadata must be an object')
        result = {**result, 'type': action.get('action_type')}
        if action.get('target_url'):
            result['target_url'] = action['target_url']
        return result
    return dict(action)


def _target(url):
    parsed = urlparse(url or '')
    parts = parsed.path.strip('/').split('/')
    if (parsed.scheme != 'https' or parsed.netloc != 'github.com' or parsed.query or parsed.fragment
            or len(parts) != 4 or parts[2] != 'pull' or not parts[3].isdigit() or int(parts[3]) < 1):
        raise ValueError('review target must be a GitHub PR URL')
    return '/'.join(parts[:2]), int(parts[3])


def validate_action(action, scope):
    """Read only the audit-registered report, never a model-selected alternate."""
    action = payload(action)
    if action.get('type') != 'submit_pr_review' or not isinstance(scope, dict):
        raise ValueError('PR review requires its trusted action scope')
    repo, number = _target(action.get('target_url'))
    if not scope.get('repo') or repo.casefold() != scope['repo'].casefold():
        raise ValueError('review repository is outside its trusted scope')
    if ('pull_request', number) not in {
        (item.get('source_type'), item.get('number')) for item in scope.get('sources', []) if isinstance(item, dict)
    }:
        raise ValueError('review PR is outside its trusted source scope')
    for field, scoped_field in SCOPE_FIELDS.items():
        value = action.get(field)
        if not isinstance(value, str) or not value or value != scope.get(scoped_field):
            raise ValueError(f'review {field} differs from its trusted scope')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', action['round_id']):
        raise ValueError('invalid review round id')
    for field in ('commit_id', 'base_sha'):
        if not re.fullmatch(r'[a-fA-F0-9]{40}|[a-fA-F0-9]{64}', action[field]):
            raise ValueError('review revision must be a complete Git object id')
    if action.get('event', 'COMMENT') != 'COMMENT':
        raise ValueError('review publication only permits COMMENT')
    path = Path(scope['review_report_path'])
    if not path.is_absolute() or path.resolve() != path or path.is_symlink():
        raise ValueError('registered review report path contains symlinks or is not canonical')
    try:
        raw = path.read_bytes()
        body = raw.decode('utf-8')
    except (OSError, UnicodeError) as exc:
        raise ValueError('registered review report is missing or invalid') from exc
    digest = hashlib.sha256(raw).hexdigest()
    if digest != action['report_sha256'] or body != action.get('body'):
        raise ValueError('review report hash or body changed after registration')
    marker = f"<!-- robert-review round={action['round_id']} -->"
    if body.count(marker) != 1:
        raise ValueError('review report requires its unique round marker')
    headings = [f'# PR #{number} Code Review 汇总报告', '## Report', '### Critical Issues',
                '### High Issues', '### Medium Issues', '### Low Issues',
                '### Dismissed Findings', '### Agent Agreement Summary']
    if any(heading not in body.splitlines() for heading in headings) or '[from Robert]' not in body:
        raise ValueError('review report is missing the required report structure')
    return action, repo, number, marker


def publication_keys(action, scope):
    data, repo, number, _ = validate_action(action, scope)
    identity = {'type': 'submit_pr_review', 'repo': repo.casefold(), 'number': number, 'round_id': data['round_id']}
    encoded = json.dumps(identity, sort_keys=True, separators=(',', ':')).encode()
    key = 'publication:' + hashlib.sha256(encoded).hexdigest()
    request = {**identity, 'commit_id': data['commit_id'], 'base_sha': data['base_sha'],
               'base_ref': data['base_ref'], 'report_sha256': data['report_sha256']}
    encoded = json.dumps(request, sort_keys=True, separators=(',', ':')).encode()
    return key, 'request:' + hashlib.sha256(encoded).hexdigest()


def _result(status, reason, **evidence):
    return {'status': status, 'reason_code': reason, 'evidence': evidence}


def _objects(value):
    if not isinstance(value, list):
        raise ValueError('review response is not a paginated list')
    for item in value:
        if isinstance(item, list):
            yield from _objects(item)
        elif isinstance(item, dict):
            yield item
        else:
            raise ValueError('review response contains an invalid entry')


def reconcile(action, scope, run_command):
    try:
        data, repo, number, marker = validate_action(action, scope)
    except (ValueError, TypeError, OSError) as exc:
        return _result('unknown', 'review_artifact_or_scope_invalid', error_type=type(exc).__name__)
    command = ['gh', 'api', f'repos/{repo}/pulls/{number}/reviews?per_page=100', '--paginate', '--slurp']
    try:
        completed = run_command(command, capture_output=True, text=True)
        if getattr(completed, 'returncode', 0) != 0:
            return _result('unknown', 'review_query_failed')
        response = json.loads(getattr(completed, 'stdout', ''))
        reviews = list(_objects(response))
        for review in reviews:
            if (not review.get('id') or not isinstance(review.get('commit_id'), str)
                    or review.get('state') not in {'COMMENTED', 'APPROVED', 'CHANGES_REQUESTED', 'DISMISSED', 'PENDING'}
                    or review.get('body') is not None and not isinstance(review.get('body'), str)):
                raise ValueError('malformed PR review record')
        matches = [review for review in reviews
                   if isinstance(review.get('body'), str) and marker in review['body']]
    except Exception as exc:
        return _result('unknown', 'review_query_failed', error_type=type(exc).__name__)
    if not matches:
        return _result('confirmed_absent', 'review_lookup_complete_absent')
    if len(matches) != 1:
        return _result('unknown', 'review_round_marker_conflict', match_count=len(matches))
    review = matches[0]
    digest = hashlib.sha256(review['body'].encode('utf-8')).hexdigest()
    if (review.get('commit_id') != data['commit_id'] or digest != data['report_sha256']
            or not review.get('id') or review.get('state') != 'COMMENTED'):
        return _result('unknown', 'review_revision_or_body_conflict')
    return _result('confirmed_effect', 'review_round_marker_match', external_id=str(review['id']),
                   target_url=review.get('html_url') or review.get('url') or data['target_url'])


def publish(action, scope, run_command, scope_guard=None):
    try:
        data, repo, number, _ = validate_action(action, scope)
    except (ValueError, TypeError, OSError):
        return {'ok': False, 'status': 'publish_failed', 'retryable': False,
                'reason_code': 'review_artifact_or_scope_invalid', 'safe_error': 'Review report or trusted scope changed.'}
    existing = reconcile(action, scope, run_command)
    if existing['status'] == 'confirmed_effect':
        return {'ok': True, 'status': 'published', 'deduplicated': True,
                **existing['evidence'], 'response': {'reconciliation': existing['evidence']}}
    if existing['status'] == 'unknown':
        return {'ok': False, 'status': 'publish_failed', 'reason_code': existing['reason_code'],
                'safe_error': 'PR Review lookup is unknown; reconciliation is required.'}
    # Reconcile first: a previously successful review still belongs to its old
    # revision even if the PR has advanced since the ambiguous response.
    command = ['gh', 'api', f'repos/{repo}/pulls/{number}']
    try:
        completed = run_command(command, capture_output=True, text=True)
        if getattr(completed, 'returncode', 0) != 0:
            raise ValueError('PR snapshot query failed')
        current = json.loads(getattr(completed, 'stdout', ''))
        if not isinstance(current, dict) or not isinstance(current.get('head'), dict) or not isinstance(current.get('base'), dict):
            raise ValueError('invalid PR snapshot')
    except Exception:
        return {'ok': False, 'status': 'publish_failed', 'reason_code': 'review_snapshot_query_failed',
                'safe_error': 'PR snapshot could not be verified before publication.'}
    if (current.get('state') != 'open' or current.get('merged')
            or current['head'].get('sha') != data['commit_id']
            or current['base'].get('sha') != data['base_sha']
            or current['base'].get('ref') != data['base_ref']):
        return {'ok': True, 'status': 'superseded', 'reason_code': 'review_snapshot_changed',
                'safe_error': 'PR closed or head/base/target branch changed; request a new review round.'}
    if scope_guard:
        guarded = scope_guard()
        if guarded:
            return guarded
    # Re-read the immutable report immediately before POST after remote I/O.
    try:
        validate_action(action, scope)
    except ValueError:
        return {'ok': False, 'status': 'publish_failed', 'retryable': False,
                'reason_code': 'review_artifact_or_scope_invalid', 'safe_error': 'Registered review report changed.'}
    command = ['gh', 'api', f'repos/{repo}/pulls/{number}/reviews', '--method', 'POST',
               '-f', 'event=COMMENT', '-f', f"commit_id={data['commit_id']}", '-f', f"body={data['body']}"]
    completed = run_command(command, capture_output=True, text=True)
    if getattr(completed, 'returncode', 0) != 0:
        return {'ok': False, 'status': 'publish_failed', 'reason_code': 'review_post_ambiguous',
                'safe_error': 'PR Review POST outcome is unknown; reconcile before retrying.'}
    try:
        response = json.loads(getattr(completed, 'stdout', ''))
    except (ValueError, TypeError):
        response = {}
    if (not isinstance(response, dict) or not response.get('id') or response.get('commit_id') != data['commit_id']
            or response.get('state') != 'COMMENTED' or response.get('body') != data['body']):
        return {'ok': False, 'status': 'publish_failed', 'reason_code': 'review_post_ambiguous',
                'safe_error': 'PR Review response is incomplete; reconcile before retrying.'}
    return {'ok': True, 'status': 'published', 'external_id': str(response['id']),
            'target_url': response.get('html_url') or data['target_url'], 'response': {'review_id': response['id']}}


def record_outcome(conn, action, status, now):
    if action.get('action_type') != 'submit_pr_review' or status not in {'published', 'superseded'}:
        return
    try:
        round_id = payload(action).get('round_id')
    except ValueError:
        return
    conn.execute('UPDATE review_sessions SET status=?,updated_at=? WHERE round_id=?', (status, now, round_id))
