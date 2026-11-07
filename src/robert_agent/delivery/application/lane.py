"""application delivery lane responsibilities."""
from contextlib import closing
import os
import sqlite3
import subprocess
from robert_agent.configuration import command as validate_config
from robert_agent.adapters.notifications import wakeup as wakeup_notifier
from robert_agent.foundation.commands import ControlDeadlineExceeded
from robert_agent.adapters.github import read as github_read

from robert_agent.adapters.github import budget as github_budget


def publish(config_path, *, dry_run=False):
    from robert_agent.delivery.application import notifications as notify
    from robert_agent.delivery.application import publish as publisher
    config = validate_config.validate_config(config_path, skip_external=True)
    if not config['ok']:
        return config
    from robert_agent.execution.application import cutover as result_cutover
    cutover = result_cutover.check(config['db_path']) if dry_run else result_cutover.prepare(config['db_path'])
    if not cutover['ok']:
        return cutover
    results, repo_ids = [], []
    for repo in config['repos']:
        with closing(sqlite3.connect(config['db_path'])) as conn:
            row = conn.execute('SELECT repo_id FROM repos WHERE full_name=?', (repo['full_name'],)).fetchone()
        if row:
            repo_ids.append(row[0])
    if not dry_run:
        notify.deliver_pending(config['db_path'], repo_ids=repo_ids)
        session = github_read.GitHubReadSession(config['db_path'], account=config['repos'][0]['github_account'], host=os.environ.get('GH_HOST', 'github.com'))
        try:
            if not github_budget.budget(session, config):
                return {'ok': False, 'status': 'core_rate_limit_floor', 'retry_after_seconds': session.poll_interval_seconds or 60}
        except ControlDeadlineExceeded:
            raise
        except (subprocess.SubprocessError, OSError, ValueError):
            return {'ok': False, 'status': 'rate_limit_check_failed'}
    for repo_id in repo_ids:
        from robert_agent.supervision.application import publication as supervision_publication
        kwargs = {'config': config} if supervision_publication.needs_config(config) else {}
        results.append(publisher.publish_ready_actions(config['db_path'], repo_id=repo_id, dry_run=dry_run, limit=5, **kwargs))
    wakeup_notifier.notify(config['db_path'])
    return {'ok': all(result.get('ok') for result in results), 'status': 'published', 'repo_count': len(results), 'failure_scope': 'task' if any(not result.get('ok') for result in results) else None}
