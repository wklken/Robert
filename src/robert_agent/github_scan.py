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
from robert_agent.common import emit
from robert_agent.github_read import GitHubReadSession


MAX_BATCH_RETRIES = 5


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
        previous = conn.execute('SELECT batch_id,context_digest,raw_events_json,attention_reason FROM github_scan_batches WHERE repo_id=? AND context_digest=? AND raw_events_json=? AND completed_at IS NULL ORDER BY created_at DESC LIMIT 1', (repo_id, context, json.dumps(raw_events))).fetchone()
        if previous and previous[1] == context and previous[2] == json.dumps(raw_events):
            identifier = previous[0]
            conn.execute('UPDATE github_scan_batches SET complete_sources_json=? WHERE batch_id=?', (json.dumps(list(complete_sources)), identifier))
        conn.execute('''INSERT OR IGNORE INTO github_scan_batches
            (batch_id,repo_id,context_digest,raw_events_json,created_at,retry_at,complete_sources_json)
            VALUES (?,?,?,?,?,?,?)''',
            (identifier, repo_id, context, json.dumps(raw_events), when, when, json.dumps(list(complete_sources))))
        _save_reads(conn, repo_id, responses, when)
        if not (previous and previous[3]) and conn.execute('SELECT 1 FROM repos WHERE repo_id=?', (repo_id,)).fetchone():
            wakeup.request_wakeup(conn, repo_id=repo_id, reason='manual_operator_request',
                                  dedupe_key=identifier, metadata={'kind': 'github_observation_ready'})
    wakeup_notifier.notify(db_path)
    return identifier


def next_batch(db_path, repo_id, *, now=None, context=None):
    with closing(sqlite3.connect(db_path, timeout=5)) as conn:
        conn.row_factory = sqlite3.Row
        clause = ' AND context_digest=?' if context is not None else ''
        params = [repo_id, time.time() if now is None else now] + ([context] if context is not None else [])
        row = conn.execute('SELECT * FROM github_scan_batches WHERE repo_id=? AND completed_at IS NULL AND attention_reason IS NULL AND retry_at<=?' + clause + ' ORDER BY created_at,batch_id LIMIT 1', params).fetchone()
    if not row:
        return None
    # A newer failed read stores an explicit unknown, never an older success.
    with closing(sqlite3.connect(db_path, timeout=5)) as conn:
        reads = conn.execute('SELECT command_key,response_json FROM github_scan_reads WHERE repo_id=? AND observed_at>=?',
                             (repo_id, (time.time() if now is None else now) - 3600)).fetchall()
    return {**dict(row), 'raw_events': json.loads(row['raw_events_json']),
            'responses': dict(reads)}



def has_pending(conn, repo_ids, *, now=None, contexts=None):
    when = time.time() if now is None else now
    for repo_id in repo_ids:
        if contexts is not None and repo_id not in contexts:
            continue
        clause = ' AND context_digest=?' if contexts is not None else ''
        params = [repo_id, when] + ([contexts[repo_id]] if contexts is not None else [])
        if conn.execute('''SELECT 1 FROM github_scan_batches WHERE repo_id=?
                AND completed_at IS NULL AND attention_reason IS NULL AND retry_at<=?''' + clause + ' LIMIT 1', params).fetchone():
            return True
    return False


def _fail_batch(conn, batch_id, reason, when):
    row = conn.execute('SELECT repo_id,retry_count,attention_reason FROM github_scan_batches WHERE batch_id=? AND completed_at IS NULL', (batch_id,)).fetchone()
    if not row or row[2]:
        return
    count = row[1] + 1
    attention = reason if count >= MAX_BATCH_RETRIES else None
    retry_at = when + min(300, 30 * (2 ** (count - 1)))
    conn.execute('UPDATE github_scan_batches SET retry_count=?,attention_reason=?,retry_at=? WHERE batch_id=?', (count, attention, retry_at, batch_id))
    conn.execute("UPDATE wakeups SET status=?,not_before_at=?,updated_at=? WHERE json_extract(metadata_json,'$.kind')='github_observation_ready' AND dedupe_key=?",
                 ('canceled' if attention else 'pending', datetime.fromtimestamp(retry_at, timezone.utc).isoformat(), datetime.fromtimestamp(when, timezone.utc).isoformat(), batch_id))
    if attention:
        conn.execute('''INSERT INTO notifications(notification_id,notification_type,channel,status,created_at,metadata_json)
            VALUES (?,'github_scan_attention','local','recorded',?,?)
            ON CONFLICT(notification_id) DO UPDATE SET status=excluded.status,
                created_at=excluded.created_at,metadata_json=excluded.metadata_json''',
            ('github-scan:' + batch_id, datetime.fromtimestamp(when, timezone.utc).isoformat(),
             json.dumps({'repo_id': row[0], 'batch_id': batch_id, 'reason_code': reason, 'retry_count': count,
                         'required_action': f'Inspect GitHub batch {batch_id} (attention at {when}); restore access and explicitly retry.'})))


def retry_batch(db_path, batch_id, repo_ids, *, now=None):
    when = time.time() if now is None else now
    with closing(sqlite3.connect(db_path, timeout=5)) as conn, conn:
        row = conn.execute('SELECT repo_id,attention_reason FROM github_scan_batches WHERE batch_id=? AND completed_at IS NULL', (batch_id,)).fetchone()
        if not row or row[0] not in repo_ids or not row[1]:
            return {'ok': False, 'status': 'not_in_scope_or_not_attention'}
        conn.execute('UPDATE github_scan_batches SET retry_count=0,attention_reason=NULL,retry_at=? WHERE batch_id=?', (when, batch_id))
        conn.execute("UPDATE wakeups SET status='pending',not_before_at=? WHERE json_extract(metadata_json,'$.kind')='github_observation_ready' AND dedupe_key=?", (datetime.fromtimestamp(when, timezone.utc).isoformat(), batch_id))
        conn.execute("UPDATE notifications SET status='resolved' WHERE notification_id=?", ('github-scan:' + batch_id,))
    wakeup_notifier.notify(db_path)
    return {'ok': True, 'status': 'retry_requested', 'batch_id': batch_id}


def finish_batch(db_path, batch_id, *, success, now=None, failure_reason="local_processing_failed"):
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
            _fail_batch(conn, batch_id, failure_reason, when)
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
    with closing(sqlite3.connect(db_path, timeout=5)) as conn, conn:
        repo_id = run_once._upsert_repo(conn, repo)
        known = run_once._load_known_workstreams(conn, repo_id)
        health_sources = set(conn.execute('''SELECT gs.source_type,gs.number FROM workstreams w
            JOIN github_sources gs ON gs.source_id=w.primary_source_id
            WHERE w.repo_id=? AND w.origin_workstream_id IS NOT NULL
              AND gs.source_type='pull_request' AND gs.state='open' ''', (repo_id,)).fetchall())
        sources = list(dict.fromkeys((row[4], row[5], (row[4], row[5]) in health_sources)
                                    for row in run_once._remote_source_rows(conn, repo_id)))
        pending_rows = conn.execute('SELECT batch_id,context_digest,raw_events_json,retry_at FROM github_scan_batches WHERE repo_id=? AND completed_at IS NULL AND attention_reason IS NULL', (repo_id,)).fetchall()
        pending = [(row[2],) for row in pending_rows]
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
    remote_states = {}
    context = context_digest(repo, known, config=config)
    try:
        for source_type, number, health_candidate in sources:
            remote_states[(source_type, number)] = run_once._remote_source_state(repo['full_name'], source_type, number, recorded)
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
            source = (raw.get('source_type', 'issue'), raw['number'])
            if source not in remote_states:
                remote_states[source] = run_once._remote_source_state(repo['full_name'], *source, recorded)
        incomplete = {(raw.get('source_type', 'issue'), raw['number']) for raw in events
                      if raw.get('authorization_lookup_complete') is False}
        covered = {source for source, state in remote_states.items()
                   if state is not None and source not in incomplete
                   and discover.source_key(repo['full_name'], *source) not in session.failed_sources}
        with closing(sqlite3.connect(db_path, timeout=5)) as conn, conn:
            for old_id, old_context, encoded, retry_at in pending_rows:
                old_sources = {(raw.get('source_type', 'issue'), raw['number']) for raw in json.loads(encoded)}
                if old_context != context and retry_at <= time.time() and not old_sources <= covered:
                    _fail_batch(conn, old_id, 'source_refresh_unavailable', time.time())
        events = refresh_pending_events(events, [raw for (encoded,) in pending for raw in json.loads(encoded)], covered, repo)
        identifier = save_batch(db_path, repo_id, context, events, recorded.responses, complete_sources=covered)
    except Exception:
        # A later discovery/search failure must not resurrect an older successful
        # safety read. Publish both fresh state and explicit failed-read markers.
        with closing(sqlite3.connect(db_path, timeout=5)) as conn, conn:
            _save_reads(conn, repo_id, recorded.responses, time.time())
            for old_id, old_context, _encoded, retry_at in pending_rows:
                if old_context != context and retry_at <= time.time():
                    _fail_batch(conn, old_id, 'discovery_refresh_failed', time.time())
        raise
    with closing(sqlite3.connect(db_path, timeout=5)) as conn, conn:
        conn.execute("UPDATE wakeups SET status='consumed' WHERE repo_id=? AND json_extract(metadata_json,'$.kind')='github_context_refresh' AND status='pending'", (repo_id,))
    return identifier


def scan(config_path, *, runner=subprocess.run):
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
                buckets[account] = session.collect_notifications(account_repos)
            except (subprocess.SubprocessError, OSError, ValueError):
                # Search remains a fallback when notification lookup fails.
                buckets[account] = {}
                failures.append({'account': account, 'reason': 'notification_read_failed'})
        except (subprocess.SubprocessError, OSError, ValueError):
            failures.append({'account': account, 'reason': 'rate_limit_check_failed'})
    for repo in config['repos']:
        if repo['github_account'] not in buckets:
            continue
        try:
            batches.append(_scan_repo(config, repo, sessions[repo['github_account']],
                                      buckets[repo['github_account']].get(repo['full_name'], [])))
        except (subprocess.SubprocessError, OSError, ValueError, sqlite3.Error):
            failures.append({'repo': repo['full_name'], 'reason': 'repo_scan_failed'})
    intervals = [session.poll_interval_seconds for session in sessions.values() if session.poll_interval_seconds]
    return {'ok': not failures, 'status': 'scanned', 'batch_count': len(batches), 'failures': failures,
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
        except (subprocess.SubprocessError, OSError, ValueError):
            return {'ok': False, 'status': 'rate_limit_check_failed'}
    for repo_id in repo_ids:
        results.append(publisher.publish_ready_actions(config['db_path'], repo_id=repo_id, dry_run=dry_run, limit=5))
    wakeup_notifier.notify(config['db_path'])
    return {'ok': all(result.get('ok') for result in results), 'status': 'published', 'repo_count': len(results)}


def manage_batches(config_path, *, retry=None, dry_run=False):
    config = validate_config.validate_config(config_path, skip_external=True)
    if not config['ok']:
        return config
    storage.init_database(config['db_path'])
    with closing(sqlite3.connect(config['db_path'])) as conn:
        repo_ids = [row[0] for repo in config['repos']
                    for row in conn.execute('SELECT repo_id FROM repos WHERE full_name=?', (repo['full_name'],))]
        if retry and not dry_run:
            return retry_batch(config['db_path'], retry, repo_ids)
        batches = []
        for repo_id in repo_ids:
            for row in conn.execute('''SELECT batch_id,created_at,retry_at,retry_count,attention_reason
                    FROM github_scan_batches WHERE repo_id=? AND completed_at IS NULL
                    ORDER BY created_at,batch_id''', (repo_id,)):
                batches.append(dict(zip(('batch_id','created_at','retry_at','retry_count','attention_reason'), row), repo_id=repo_id))
    return {'ok': True, 'status': 'dry_run' if retry else 'listed', 'batches': batches}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--publish', action='store_true')
    action.add_argument('--list-batches', action='store_true')
    action.add_argument('--retry-batch')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    if args.list_batches or args.retry_batch:
        result = manage_batches(args.config, retry=args.retry_batch, dry_run=args.dry_run)
    else:
        result = publish(args.config, dry_run=args.dry_run) if args.publish else scan(args.config)
    return emit(result, 0 if result.get('ok') else 3)


if __name__ == '__main__':
    raise SystemExit(main())
