"""Read-only gh sessions with rebuildable discovery caches and bounded history.

Cached events are observations, never proof that local routing succeeded. Every
cache hit is returned to the caller for current authorization and routing.
"""

from contextlib import closing
import copy
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import time
from urllib.parse import urlsplit


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


class GitHubReadSession:
    def __init__(self, db_path, *, account, host='github.com', runner=subprocess.run,
                 now=None, search_ttl_seconds=300, refresh_seconds=1800):
        self.db_path = db_path
        self.scope = _digest([host, account])
        self.host = host
        self.runner = runner
        self.now = now or time.time
        self.search_ttl_seconds = search_ttl_seconds
        self.refresh_seconds = refresh_seconds
        self.poll_interval_seconds = None
        self.search_allowed = True
        self._memo = {}
        self._stats = {'calls': 0, 'cache_hits': 0, 'errors': 0, 'duration_seconds': 0.0,
                       'endpoints': {}}

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=5)
        conn.execute('PRAGMA busy_timeout=5000')
        return conn

    def _get(self, key, version=None, *, allow_expired=False):
        with closing(self._connect()) as conn:
            row = conn.execute('''SELECT version,payload_json,expires_at FROM github_read_cache
                WHERE scope=? AND cache_key=?''', (self.scope, key)).fetchone()
        if row and (allow_expired or row[2] > self.now()) and (version is None or row[0] == version):
            return json.loads(row[1])
        return None

    def _put(self, key, version, payload, ttl):
        now = self.now()
        with closing(self._connect()) as conn, conn:
            conn.execute('''INSERT INTO github_read_cache(scope,cache_key,version,payload_json,expires_at)
                VALUES (?,?,?,?,?) ON CONFLICT(scope,cache_key) DO UPDATE SET
                version=excluded.version,payload_json=excluded.payload_json,expires_at=excluded.expires_at''',
                (self.scope, key, version, json.dumps(payload), now + ttl))
            # Cache history is expendable. Keep no superseded versions and remove
            # stale entries without touching durable task/event observations.
            conn.execute('DELETE FROM github_read_cache WHERE expires_at < ?', (now,))

    def __call__(self, args, **kwargs):
        args = list(args)
        if args[:2] == ['gh', 'api'] and '--hostname' not in args:
            args.extend(['--hostname', self.host])
        if args[:3] == ['gh', 'search', 'issues']:
            kwargs['env'] = {**os.environ, **(kwargs.get('env') or {}), 'GH_HOST': self.host}
        method = None
        for flag in ('-X', '--method'):
            if flag in args:
                method = args[args.index(flag) + 1].upper()
        read = (args[:2] == ['gh', 'api'] and method in (None, 'GET')
                and not any(flag in args for flag in ('-f', '-F', '--field', '--raw-field')))
        read = read or args[:3] == ['gh', 'search', 'issues']
        key = _digest([self.scope, args, kwargs.get('cwd'), kwargs.get('env')])
        if read and key in self._memo:
            self._stats['cache_hits'] += 1
            return copy.deepcopy(self._memo[key])
        endpoint = args[2].split('?')[0] if args[:2] == ['gh', 'api'] else 'search/issues'
        endpoint = re.sub(r'/\d+(?=/|$)', '/:number', endpoint)
        metric = self._stats['endpoints'].setdefault(endpoint, {'calls': 0, 'duration_seconds': 0.0})
        started = time.monotonic()
        self._stats['calls'] += 1
        metric['calls'] += 1
        try:
            result = self.runner(args, **kwargs)
        except Exception:
            self._stats['errors'] += 1
            raise
        else:
            if result.returncode:
                self._stats['errors'] += 1
            elif read:
                # Invalid JSON is not a complete successful remote observation.
                try:
                    json.loads(result.stdout)
                except (ValueError, TypeError):
                    if '--include' not in args and '-i' not in args:
                        self._stats['errors'] += 1
                else:
                    self._memo[key] = copy.deepcopy(result)
            return result
        finally:
            elapsed = time.monotonic() - started
            metric['duration_seconds'] += elapsed
            self._stats['duration_seconds'] += elapsed

    def metrics(self):
        return copy.deepcopy(self._stats)

    def search(self, args, *, notification_hints=()):
        from robert_agent import discover
        key = 'search:' + _digest(args)
        if not self.search_allowed:
            return self._get(key, allow_expired=True) or []
        version = _digest(notification_hints)
        cached = self._get(key, version)
        if cached is not None:
            self._stats['cache_hits'] += 1
            return cached
        payload = discover._run_json(args, runner=self)
        if not isinstance(payload, list):
            raise ValueError('GitHub search did not return a list')
        self._put(key, version, payload, self.search_ttl_seconds)
        return copy.deepcopy(payload)

    def source_events(self, repo, raw, load, *, known_workstreams=None,
                      review_author_allowlist=None, notification_hints=()):
        from robert_agent import discover
        source = discover.source_key(repo['full_name'], raw.get('source_type', 'issue'), raw['number'])
        key = 'source:' + source + ':' + raw['event_type']
        context = [repo['github_account'], sorted(repo.get('trusted_actors', [])),
                   sorted(review_author_allowlist or []), source in set(known_workstreams or [])]
        version = _digest([raw, context, notification_hints])
        cached = self._get(key, version)
        if cached is not None:
            self._stats['cache_hits'] += 1
            return cached
        before = self._stats['errors']
        events = load()
        if (self._stats['errors'] == before
                and all(event.get('authorization_lookup_complete') is not False for event in events)):
            self._put(key, version, events, self.refresh_seconds)
        return copy.deepcopy(events)

    def collect_events(self, repo_config, **kwargs):
        from robert_agent import discover
        return discover.collect_live_events(repo_config, runner=self, read_session=self, **kwargs)

    def _page(self, endpoint, marker=None):
        args = ['gh', 'api', endpoint, '--hostname', self.host, '--include', '--method', 'GET']
        if marker:
            args.extend(['-H', 'If-Modified-Since: ' + marker])
        # gh exits 1 on HTTP304. Parse the HTTP status before interpreting the
        # process exit, while preserving real errors for the scanner's retry.
        response = self(args, text=True, capture_output=True, check=False)
        parts = re.split(r'\r?\n\r?\n', response.stdout, maxsplit=1)
        lines = parts[0].splitlines()
        match = re.match(r'HTTP/\S+\s+(\d+)', lines[0] if lines else '')
        status = int(match[1]) if match else None
        headers = {}
        for line in lines[1:]:
            if ':' in line:
                name, value = line.split(':', 1)
                headers[name.lower()] = value.strip()
        interval = headers.get('x-poll-interval')
        if interval and interval.isdigit() and int(interval) > 0:
            self.poll_interval_seconds = int(interval)
        retry_after = headers.get('retry-after', '')
        if retry_after.isdigit():
            self.poll_interval_seconds = max(self.poll_interval_seconds or 0, int(retry_after))
        if status == 304:
            if response.returncode:
                self._stats['errors'] -= 1
            return status, headers, []
        if response.returncode or status != 200:
            raise subprocess.CalledProcessError(response.returncode or 1, args,
                                                response.stdout, response.stderr)
        payload = json.loads(parts[1] if len(parts) == 2 else '')
        if not isinstance(payload, list):
            raise ValueError('GitHub notification page did not return a list')
        return status, headers, payload

    def cached_notifications(self, repo_configs):
        """Reuse hints for context refresh without advancing the notification poll."""
        cached = self._get('notifications', allow_expired=True) or {}
        configured = {repo['full_name'] for repo in repo_configs}
        buckets = {}
        for hint in cached.get('hints', []):
            if hint['repo_full_name'] in configured:
                buckets.setdefault(hint['repo_full_name'], []).append(hint)
        return copy.deepcopy(buckets)

    def collect_notifications(self, repo_configs):
        from robert_agent import discover
        key = 'notifications'
        cached = self._get(key)
        status, headers, payload = self._page('notifications?per_page=50',
                                              (cached or {}).get('last_modified'))
        if status == 304:
            if cached is None:
                raise ValueError('GitHub returned304 without a complete local notification snapshot')
            hints = cached['hints']
            self._stats['cache_hits'] += 1
        else:
            notifications = list(payload)
            link = headers.get('link', '')
            visited = set()
            while True:
                match = re.search(r'<([^>]+)>;\s*rel="next"', link)
                if not match:
                    break
                next_url = match[1]
                parsed = urlsplit(next_url)
                expected_host = 'api.github.com' if self.host == 'github.com' else self.host
                if parsed.scheme != 'https' or parsed.hostname != expected_host or next_url in visited:
                    raise ValueError('Invalid GitHub notification pagination link')
                visited.add(next_url)
                next_status, next_headers, page = self._page(next_url)
                if next_status != 200:
                    raise ValueError('Incomplete GitHub notification pagination')
                notifications.extend(page)
                link = next_headers.get('link', '')
            hints = [raw for item in notifications if (raw := discover._notification_to_raw(item))]
            self._put(key, '', {'hints': hints, 'last_modified': headers.get('last-modified')},
                      self.refresh_seconds)
        configured = {repo['full_name'] for repo in repo_configs}
        buckets = {}
        for hint in hints:
            if hint['repo_full_name'] in configured:
                buckets.setdefault(hint['repo_full_name'], []).append(hint)
        return copy.deepcopy(buckets)
