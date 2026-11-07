"""One SQLite admission limit shared by native review and voting processes."""

RUN_TABLES = ('review_runs', 'vote_runs')


def status(conn):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='review_process_pool'").fetchone():
        return None
    limit = conn.execute('SELECT max_concurrency FROM review_process_pool WHERE singleton=1').fetchone()
    if not limit:
        return None
    count = 0
    for table in RUN_TABLES:
        columns = {row[1] for row in conn.execute(f'PRAGMA table_info({table})')}
        if not columns:
            continue
        if 'slot_reserved' not in columns:
            return None
        count += conn.execute(f'SELECT COUNT(*) FROM {table} WHERE slot_reserved=1').fetchone()[0]
    return {'limit': limit[0], 'in_use': count}


def reserve(conn, table, run_id, now):
    if table not in RUN_TABLES or not conn.in_transaction:
        raise ValueError('model slot reservation requires a known run table and write transaction')
    pool = status(conn)
    if pool is None:
        raise ValueError('model process pool is not initialized')
    if pool['in_use'] >= pool['limit']:
        return False
    return bool(conn.execute(f'''UPDATE {table} SET slot_reserved=1,status='starting',started_at=?,updated_at=?
        WHERE run_id=? AND status='waiting' AND slot_reserved=0''', (now, now, run_id)).rowcount)
