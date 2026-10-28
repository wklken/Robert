"""Remote observation producer and durable handoff to the local control plane."""
import argparse
from datetime import datetime, timezone
from contextlib import closing
import hashlib
import json
import os
import sqlite3
import subprocess
import time
from uuid import uuid4

from robert_agent import discover, pr_health, storage, validate_config, wakeup, wakeup_notifier
from robert_agent.common import ControlDeadlineExceeded, emit
from robert_agent.github_read import GitHubReadSession


def review_allowlist(config, repo):
    from robert_agent import route_config, run_once
    if config is None:
        return []
    settings = route_config.resolve_route_config(config, repo, run_once.ROUTE_POLICIES['review-pr']).get('multi_agent_review', {})
    return sorted(settings.get('author_allowlist') or []) if settings.get('enabled') and len(settings.get('workers', [])) > 1 else []


def context_digest(repo, known_workstreams, *, config=None):
    values = [repo['full_name'], repo['github_account'], sorted(repo.get('trusted_actors', [])),
              sorted(known_workstreams), review_allowlist(config, repo), os.environ.get('GH_HOST', 'github.com')]
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def event_identity(raw):
    return (raw.get('source_type', 'issue'), raw.get('number'),
            raw.get('event_fingerprint') or f"{raw.get('event_type')}:{raw.get('id') or raw.get('event_id')}")


def refresh_pending_events(events, pending, covered, repo):
    """Carry every unconsumed identity across context changes, after a fresh read.

    Authorization still runs locally. Never carry a derived trust boolean from
    the old configuration into the new context.
    """
    merged = {event_identity(raw): raw for raw in events}
    trusted = set(repo.get('trusted_actors', []))
    account = repo['github_account']
    for raw in pending:
        if (raw.get('source_type', 'issue'), raw['number']) not in covered:
            continue
        refreshed = dict(raw)
        refreshed['mentions_dd'] = f'@{account}' in (raw.get('body') or '')
        if raw.get('event_type') == 'notification':
            refreshed['trusted_trigger_found'] = bool(
                raw.get('authorization_lookup_complete') is not False and (
                    (raw.get('assigned_to') == account and raw.get('assignment_actor_login') in trusted)
                    or (raw.get('requested_reviewer') == account and raw.get('requester_login') in trusted)
                    or (refreshed['mentions_dd'] and raw.get('actor_login') in trusted)))
        merged.setdefault(event_identity(raw), refreshed)
    return list(merged.values())


def command_key(args):
    args = list(args)
    if '--hostname' in args:
        index = args.index('--hostname')
        del args[index:index + 2]
    return json.dumps(args)


class RecordingRunner:
    def __init__(self, runner):
        self.runner = runner
        self.responses = {}

    def __call__(self, args, **kwargs):
        key = command_key(args)
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
        key = command_key(args)
        if key not in self.responses or self.responses[key] is None:
            if kwargs.get('check'):
                raise subprocess.CalledProcessError(1, args, '', 'remote observation unavailable')
            return subprocess.CompletedProcess(args, 1, '', 'remote observation unavailable')
        return subprocess.CompletedProcess(args, 0, self.responses[key], '')


def _save_reads(conn, repo_id, responses, when):
    for key, payload in dict(responses).items():
        conn.execute('''INSERT INTO github_scan_reads(repo_id,command_key,response_json,observed_at)
            VALUES (?,?,?,?) ON CONFLICT(repo_id,command_key) DO UPDATE SET
            response_json=excluded.response_json,observed_at=excluded.observed_at''',
            (repo_id, key, payload, when))


def save_batch(db_path, repo_id, context, raw_events, responses, *, now=None, complete_sources=()):
    when = time.time() if now is None else now
    identifier = uuid4().hex
    with closing(sqlite3.connect(db_path, timeout=5)) as conn, conn:
        previous = conn.execute('SELECT batch_id,context_digest,raw_events_json FROM github_scan_batches WHERE repo_id=? AND completed_at IS NULL ORDER BY created_at DESC LIMIT 1', (repo_id,)).fetchone()
        if previous and previous[1] == context and previous[2] == json.dumps(raw_events):
            identifier = previous[0]
            conn.execute('UPDATE github_scan_batches SET complete_sources_json=? WHERE batch_id=?', (json.dumps(list(complete_sources)), identifier))
        conn.execute('''INSERT OR IGNORE INTO github_scan_batches
            (batch_id,repo_id,context_digest,raw_events_json,created_at,retry_at,complete_sources_json)
            VALUES (?,?,?,?,?,?,?)''',
            (identifier, repo_id, context, json.dumps(raw_events), when, when, json.dumps(list(complete_sources))))
        _save_reads(conn, repo_id, responses, when)
        if conn.execute('SELECT 1 FROM repos WHERE repo_id=?', (repo_id,)).fetchone():
            wakeup.request_wakeup(conn, repo_id=repo_id, reason='manual_operator_request',
                                  dedupe_key=identifier, metadata={'kind': 'github_observation_ready'})
    wakeup_notifier.notify(db_path)
    return identifier


def next_batch(db_path, repo_id, *, now=None, context=None):
    with closing(sqlite3.connect(db_path, timeout=5)) as conn:
        conn.row_factory = sqlite3.Row
        clause = ' AND context_digest=?' if context is not None else ''
        params = [repo_id, time.time() if now is None else now] + ([context] if context is not None else [])
        row = conn.execute('SELECT * FROM github_scan_batches WHERE repo_id=? AND completed_at IS NULL AND retry_at<=?' + clause + ' ORDER BY created_at,batch_id LIMIT 1', params).fetchone()
    if not row:
        return None
    # A newer failed read stores an explicit unknown, never an older success.
    with closing(sqlite3.connect(db_path, timeout=5)) as conn:
        reads = conn.execute('SELECT command_key,response_json FROM github_scan_reads WHERE repo_id=? AND observed_at>=?',
                             (repo_id, (time.time() if now is None else now) - 3600)).fetchall()
    return {**dict(row), 'raw_events': json.loads(row['raw_events_json']),
            'responses': dict(reads)}



def has_pending(conn, repo_ids, *, now=None):
    if not repo_ids:
        return False
    placeholders = ','.join('?' for _ in repo_ids)
    return bool(conn.execute(f'''SELECT 1 FROM github_scan_batches
        WHERE repo_id IN ({placeholders}) AND completed_at IS NULL AND retry_at<=? LIMIT 1''',
        [*repo_ids, time.time() if now is None else now]).fetchone())


def finish_batch(db_path, batch_id, *, success, now=None):
    when = time.time() if now is None else now
    with closing(sqlite3.connect(db_path, timeout=5)) as conn, conn:
        if success:
            current = conn.execute('SELECT repo_id,context_digest,created_at,complete_sources_json,raw_events_json FROM github_scan_batches WHERE batch_id=?', (batch_id,)).fetchone()
            if current:
                covered = {tuple(source) for source in json.loads(current[3])}
                replayed = {event_identity(raw) for raw in json.loads(current[4])}
                older = conn.execute('SELECT batch_id,raw_events_json FROM github_scan_batches WHERE repo_id=? AND context_digest!=? AND created_at<=? AND completed_at IS NULL', (current[0], current[1], current[2])).fetchall()
                for old_id, encoded in older:
                    sources = {(event.get('source_type', 'issue'), event['number']) for event in json.loads(encoded)}
                    if sources <= covered and {event_identity(raw) for raw in json.loads(encoded)} <= replayed:
                        conn.execute('UPDATE github_scan_batches SET completed_at=? WHERE batch_id=?', (when, old_id))
                        conn.execute("UPDATE wakeups SET status='consumed' WHERE json_extract(metadata_json,'$.kind')='github_observation_ready' AND dedupe_key=?", (old_id,))
            conn.execute('UPDATE github_scan_batches SET completed_at=? WHERE batch_id=?', (when, batch_id))
            conn.execute("UPDATE wakeups SET status='consumed' WHERE json_extract(metadata_json,'$.kind')='github_observation_ready' AND dedupe_key=?", (batch_id,))
        else:
            conn.execute('UPDATE github_scan_batches SET retry_at=? WHERE batch_id=?', (when + 30, batch_id))
            conn.execute("UPDATE wakeups SET not_before_at=? WHERE json_extract(metadata_json,'$.kind')='github_observation_ready' AND dedupe_key=?", (datetime.fromtimestamp(when + 30, timezone.utc).isoformat(), batch_id))
        conn.execute("DELETE FROM wakeups WHERE status='consumed' AND json_extract(metadata_json,'$.kind') IN ('github_observation_ready','github_context_refresh')")
        conn.execute('DELETE FROM github_scan_batches WHERE completed_at IS NOT NULL')
        conn.execute('DELETE FROM github_scan_reads WHERE observed_at < ?', (when - 3600,))


def _budget(session, config):
    payload = discover._run_json(['gh', 'api', 'rate_limit'], runner=session)
    resources = payload.get('resources') or {}
    core = resources.get('core') or {}
    search = resources.get('search') or {}
    session.search_allowed = search.get('remaining', 0) >= config['daemon']['min_search_remaining']
    allowed = core.get('remaining', 0) >= config['daemon']['min_core_remaining']
    if not allowed:
        session.poll_interval_seconds = max(60, int(core.get('reset', 0) - time.time()))
    return allowed


def _scan_repo(config, repo, session, hints):
    from robert_agent import run_once
    db_path = config['db_path']
    recorded = RecordingRunner(session)
    errors_before = session.metrics()['errors']
    with closing(sqlite3.connect(db_path, timeout=5)) as conn, conn:
        repo_id = run_once._upsert_repo(conn, repo)
        known = run_once._load_known_workstreams(conn, repo_id)
        sources = conn.execute('''SELECT DISTINCT gs.source_type,gs.number,
            CASE WHEN w.origin_workstream_id IS NOT NULL AND gs.source_type='pull_request' AND gs.state='open' THEN 1 ELSE 0 END
            FROM github_sources gs LEFT JOIN workstreams w ON w.primary_source_id=gs.source_id
            WHERE gs.repo_id=? AND (gs.state='open' OR w.active_task_id IS NOT NULL
              OR (gs.source_type='issue' AND EXISTS (SELECT 1 FROM work_items wi WHERE wi.origin_source_id=gs.source_id AND wi.completed_at IS NULL AND wi.canceled_at IS NULL)))''', (repo_id,)).fetchall()
        pending = conn.execute('SELECT raw_events_json FROM github_scan_batches WHERE repo_id=? AND completed_at IS NULL', (repo_id,)).fetchall()
    hints = list(hints)
    for (encoded,) in pending:
        for raw in json.loads(encoded):
            source = (raw.get('source_type', 'issue'), raw['number'])
            if not any((kind, number) == source for kind, number, _ in sources):
                sources.append((*source, False))
            if not any(h.get('number') == raw['number'] and h.get('source_type') == raw.get('source_type', 'issue') for h in hints):
                hints.append({'id': str(raw.get('id') or raw.get('event_fingerprint')), 'repo_full_name': repo['full_name'],
                    'number': raw['number'], 'source_type': raw.get('source_type', 'issue'),
                    'event_type': 'notification', 'event_at': raw.get('event_at')})
    try:
        for source_type, number, health_candidate in sources:
            run_once._remote_source_state(repo['full_name'], source_type, number, recorded)
            automation = repo.get('pr_automation') or {}
            if health_candidate and any((automation.get(k) or {}).get('enabled') for k in ('ci', 'conflict')):
                snapshot = pr_health.fetch_pr_snapshot(repo['full_name'], number, runner=recorded)
                ci = automation.get('ci') or {}
                if snapshot and snapshot.get('author_login', '').casefold() == repo['github_account'].casefold() and ci.get('enabled') and snapshot.get('head_sha'):
                    pr_health.collect_ci_observations(repo['full_name'], snapshot['head_sha'],
                        check_allowlist=ci['check_allowlist'], max_summary_chars=ci['max_failure_summary_chars'], runner=recorded)
        events = session.collect_events(repo, known_workstreams=known, notification_hints=hints,
            include_notifications=False, review_author_allowlist=review_allowlist(config, repo))
        for raw in [*events, *(raw for (encoded,) in pending for raw in json.loads(encoded))]:
            run_once._remote_source_state(repo['full_name'], raw.get('source_type', 'issue'), raw['number'], recorded)
        covered = {(source_type, number) for source_type, number, _ in sources}
        covered.update((raw.get('source_type', 'issue'), raw['number']) for raw in [*hints, *events])
        if session.metrics()['errors'] != errors_before or any(event.get('authorization_lookup_complete') is False for event in events):
            covered = set()
        events = refresh_pending_events(events, [raw for (encoded,) in pending for raw in json.loads(encoded)], covered, repo)
        identifier = save_batch(db_path, repo_id, context_digest(repo, known, config=config), events, recorded.responses, complete_sources=covered)
    except Exception:
        # A later discovery/search failure must not resurrect an older successful
        # safety read. Publish both fresh state and explicit failed-read markers.
        with closing(sqlite3.connect(db_path, timeout=5)) as conn, conn:
            _save_reads(conn, repo_id, recorded.responses, time.time())
        raise
    with closing(sqlite3.connect(db_path, timeout=5)) as conn, conn:
        conn.execute("UPDATE wakeups SET status='consumed' WHERE repo_id=? AND json_extract(metadata_json,'$.kind')='github_context_refresh' AND status='pending'", (repo_id,))
    return identifier


def scan(config_path, *, runner=subprocess.run, refresh_context=False):
    config = validate_config.validate_config(config_path, skip_external=True)
    if not config['ok']:
        return config
    db_path = config['db_path']
    storage.init_database(db_path)
    sessions, buckets, failures, batches = {}, {}, [], []
    for repo in config['repos']:
        account = repo['github_account']
        if account in sessions:
            continue
        session = GitHubReadSession(db_path, account=account, host=os.environ.get('GH_HOST', 'github.com'), runner=runner)
        sessions[account] = session
        try:
            if not _budget(session, config):
                failures.append({'account': account, 'reason': 'core_rate_limit_floor'})
                continue
            account_repos = [item for item in config['repos'] if item['github_account'] == account]
            try:
                buckets[account] = (session.cached_notifications(account_repos) if refresh_context
                                    else session.collect_notifications(account_repos))
            except ControlDeadlineExceeded:
                raise
            except (subprocess.SubprocessError, OSError, ValueError):
                # Search remains a fallback when notification lookup fails.
                buckets[account] = {}
                failures.append({'account': account, 'reason': 'notification_read_failed'})
        except ControlDeadlineExceeded:
            raise
        except (subprocess.SubprocessError, OSError, ValueError):
            failures.append({'account': account, 'reason': 'rate_limit_check_failed'})
    for repo in config['repos']:
        if repo['github_account'] not in buckets:
            continue
        try:
            batches.append(_scan_repo(config, repo, sessions[repo['github_account']],
                                      buckets[repo['github_account']].get(repo['full_name'], [])))
        except ControlDeadlineExceeded:
            raise
        except (subprocess.SubprocessError, OSError, ValueError, sqlite3.Error):
            failures.append({'repo': repo['full_name'], 'reason': 'repo_scan_failed'})
    intervals = [session.poll_interval_seconds for session in sessions.values() if session.poll_interval_seconds]
    return {'ok': not failures, 'status': 'scanned', 'context_refresh': refresh_context, 'batch_count': len(batches), 'failures': failures,
            'poll_interval_seconds': max(intervals, default=60),
            'github_reads': {account: session.metrics() for account, session in sessions.items()}}


def publish(config_path, *, dry_run=False):
    from robert_agent import notify, publish as publisher
    config = validate_config.validate_config(config_path, skip_external=True)
    if not config['ok']:
        return config
    results, repo_ids = [], []
    for repo in config['repos']:
        with closing(sqlite3.connect(config['db_path'])) as conn:
            row = conn.execute('SELECT repo_id FROM repos WHERE full_name=?', (repo['full_name'],)).fetchone()
        if row:
            repo_ids.append(row[0])
    if not dry_run:
        notify.deliver_pending(config['db_path'], repo_ids=repo_ids)
        session = GitHubReadSession(config['db_path'], account=config['repos'][0]['github_account'], host=os.environ.get('GH_HOST', 'github.com'))
        try:
            if not _budget(session, config):
                return {'ok': False, 'status': 'core_rate_limit_floor'}
        except ControlDeadlineExceeded:
            raise
        except (subprocess.SubprocessError, OSError, ValueError):
            return {'ok': False, 'status': 'rate_limit_check_failed'}
    for repo_id in repo_ids:
        results.append(publisher.publish_ready_actions(config['db_path'], repo_id=repo_id, dry_run=dry_run, limit=5))
    wakeup_notifier.notify(config['db_path'])
    return {'ok': all(result.get('ok') for result in results), 'status': 'published', 'repo_count': len(results)}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    parser.add_argument('--publish', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--refresh-context', action='store_true')
    args = parser.parse_args(argv)
    result = publish(args.config, dry_run=args.dry_run) if args.publish else scan(args.config, refresh_context=args.refresh_context)
    return emit(result, 0 if result.get('ok') else 3)


if __name__ == '__main__':
    raise SystemExit(main())
