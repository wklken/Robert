"""Durable file handoffs for native PR review rounds (SQLite, no agent framework)."""
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

SCHEMA = """
CREATE TABLE IF NOT EXISTS review_sessions (
  round_id TEXT PRIMARY KEY,
  repo TEXT NOT NULL,
  pr_number INTEGER NOT NULL,
  workstream_id TEXT NOT NULL,
  trigger_fingerprint TEXT NOT NULL,
  task_id TEXT UNIQUE REFERENCES tasks(task_id),
  status TEXT NOT NULL DEFAULT 'queued',
  request_json TEXT NOT NULL,
  manifest_json TEXT,
  report_path TEXT,
  report_sha256 TEXT,
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(repo, pr_number, trigger_fingerprint)
);
CREATE TABLE IF NOT EXISTS review_runs (
  run_id TEXT PRIMARY KEY,
  round_id TEXT NOT NULL REFERENCES review_sessions(round_id),
  stage TEXT NOT NULL CHECK(stage IN ('prepare','review','synthesis')),
  worker_id TEXT NOT NULL,
  model TEXT,
  attempt_no INTEGER NOT NULL,
  owner_attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
  status TEXT NOT NULL,
  output_dir TEXT NOT NULL UNIQUE,
  prompt_sha256 TEXT NOT NULL,
  dispatch_json TEXT,
  artifact_json TEXT,
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(round_id, stage, worker_id, attempt_no)
);
CREATE INDEX IF NOT EXISTS review_sessions_workstream ON review_sessions(workstream_id, created_at);
"""


def now():
    return datetime.now(timezone.utc).isoformat()


def initialize(conn):
    # execute separately: executescript would commit the caller's transaction.
    for statement in SCHEMA.split(';'):
        if statement.strip():
            conn.execute(statement)
    if 'model' not in {row[1] for row in conn.execute('PRAGMA table_info(review_runs)')}:
        conn.execute('ALTER TABLE review_runs ADD COLUMN model TEXT')


@contextmanager
def connect(db_path):
    with closing(sqlite3.connect(db_path, timeout=10)) as conn, conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        yield conn


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def decode(row):
    if row is None:
        return None
    result = dict(row)
    for field in ('request_json', 'manifest_json', 'dispatch_json', 'artifact_json'):
        if field in result:
            result[field[:-5]] = json.loads(result.pop(field) or '{}')
    return result


def session(conn, *, round_id=None, task_id=None):
    # Do not change a caller's row_factory.
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='review_sessions'").fetchone():
        return None
    cursor = conn.execute('SELECT * FROM review_sessions WHERE ' +
                          ('round_id=?' if round_id else 'task_id=?'),
                          (round_id or task_id,))
    row = cursor.fetchone()
    return decode(dict(zip([c[0] for c in cursor.description], row))) if row else None


def enqueue(conn, repo, event, route, workers):
    key = f"{repo['full_name']}:{event['number']}:{event['event_fingerprint']}"
    round_id = 'review-' + hashlib.sha256(key.encode()).hexdigest()[:24]
    request = {
        'repo': dict(repo), 'event': dict(event), 'route': dict(route),
        'workers': [dict(worker) for worker in workers],
    }
    timestamp = now()
    conn.execute('''INSERT OR IGNORE INTO review_sessions(
        round_id,repo,pr_number,workstream_id,trigger_fingerprint,request_json,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?)''',
        (round_id, repo['full_name'], event['number'], event['workstream_id'],
         event['event_fingerprint'], json.dumps(request, ensure_ascii=False), timestamp, timestamp))
    return session(conn, round_id=round_id)


def update(conn, round_id, **fields):
    allowed = {'task_id', 'status', 'manifest_json', 'report_path', 'report_sha256', 'error'}
    if not fields or set(fields) - allowed:
        raise ValueError('invalid review session update')
    fields['updated_at'] = now()
    conn.execute('UPDATE review_sessions SET ' + ','.join(f'{key}=?' for key in fields)
                 + ' WHERE round_id=?', [*fields.values(), round_id])


def runs(conn, round_id):
    cursor = conn.execute('SELECT * FROM review_runs WHERE round_id=? ORDER BY created_at,run_id', (round_id,))
    names = [col[0] for col in cursor.description]
    return [decode(dict(zip(names, row))) for row in cursor]


def new_run(conn, round_id, stage, worker_id, owner_attempt_id, root, prompt_sha256, model=None):
    attempt_no = conn.execute('''SELECT COALESCE(MAX(attempt_no),0)+1 FROM review_runs
        WHERE round_id=? AND stage=? AND worker_id=?''', (round_id, stage, worker_id)).fetchone()[0]
    output_dir = Path(root) / ('runs' if stage == 'review' else stage)
    if stage == 'review':
        # Worker names are arbitrary config keys, never path components.
        output_dir /= hashlib.sha256(worker_id.encode()).hexdigest()[:16]
    output_dir /= f'attempt-{attempt_no}'
    output_dir.mkdir(parents=True, exist_ok=False)
    run_id, timestamp = 'review-run-' + uuid4().hex, now()
    conn.execute('''INSERT INTO review_runs(run_id,round_id,stage,worker_id,model,attempt_no,
        owner_attempt_id,status,output_dir,prompt_sha256,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,'prepared',?,?,?,?)''',
        (run_id, round_id, stage, worker_id, model, attempt_no, owner_attempt_id,
         str(output_dir.resolve()), prompt_sha256, timestamp, timestamp))
    return next(run for run in runs(conn, round_id) if run['run_id'] == run_id)


def update_run(conn, run_id, **fields):
    allowed = {'status', 'dispatch_json', 'artifact_json', 'error'}
    if not fields or set(fields) - allowed:
        raise ValueError('invalid review run update')
    fields['updated_at'] = now()
    conn.execute('UPDATE review_runs SET ' + ','.join(f'{key}=?' for key in fields)
                 + ' WHERE run_id=?', [*fields.values(), run_id])


def task_status(conn, task_id):
    entry = session(conn, task_id=task_id)
    if not entry:
        return None
    return {key: value for key, value in entry.items() if key != 'request'} | {
        'runs': [{key: value for key, value in run.items() if key != 'dispatch'}
                 for run in runs(conn, entry['round_id'])]}


def board_status(conn, task_id):
    """Only bounded public status; no filesystem paths, environment or raw logs."""
    from robert_agent.board_execution import safe_text
    entry = session(conn, task_id=task_id)
    if not entry:
        return None
    manifest = entry['manifest']
    return {'round_id': entry['round_id'], 'status': entry['status'],
            'head_sha': manifest.get('head_sha'), 'base_ref': manifest.get('base_ref'),
            'error': safe_text(entry['error'] or ''),
            'runs': [{'stage': run['stage'], 'worker_id': safe_text(run['worker_id']),
                      'model': safe_text(run['model'] or ''),
                      'attempt_no': run['attempt_no'], 'status': run['status'],
                      'error': safe_text(run['error'] or '')}
                     for run in runs(conn, entry['round_id'])]}
