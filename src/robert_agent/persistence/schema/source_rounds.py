"""Amend prerelease source round membership while preserving frozen history."""


def has_unique_request(conn):
    indexes = conn.execute("PRAGMA index_list(source_round_requests)").fetchall()
    return any(
        row[2]
        and [column[0] for column in conn.execute("SELECT name FROM pragma_index_info(?)", (row[1],))] == ["request_id"]
        for row in indexes
    )


def allow_historical_membership(conn, schema):
    if conn.execute("PRAGMA foreign_keys").fetchone()[0]:
        raise RuntimeError("Source round membership upgrade requires a dedicated rebuild connection")
    prefix = "CREATE TABLE IF NOT EXISTS source_round_requests ("
    start = schema.index(prefix) + len(prefix)
    end = schema.index("\n);", start)
    conn.execute("CREATE TABLE new_source_round_requests (" + schema[start:end] + "\n)")
    conn.execute("INSERT INTO new_source_round_requests SELECT * FROM source_round_requests")
    count = conn.execute("SELECT count(*) FROM source_round_requests").fetchone()
    if conn.execute("SELECT count(*) FROM new_source_round_requests").fetchone() != count:
        raise RuntimeError("Source round membership row count changed")
    conn.execute("DROP TABLE source_round_requests")
    conn.execute("ALTER TABLE new_source_round_requests RENAME TO source_round_requests")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_source_round_requests_request ON source_round_requests(request_id)")
