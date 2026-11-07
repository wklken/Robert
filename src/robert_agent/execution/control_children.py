"""Persist the startup handshake and ownership of managed control children."""
import json


def owner_lease_active(conn, owner):
    return bool(conn.execute("""SELECT 1 FROM leases WHERE owner_id=?
        AND resource_type='daemon' AND status='active'
        AND (julianday(expires_at) IS NULL OR julianday(expires_at)>julianday('now')) LIMIT 1""",
        (owner,)).fetchone())


def list_children(conn):
    return conn.execute(
        'SELECT child_id,owner_id,lane,identity_json,status FROM github_control_children ORDER BY owner_id,lane,child_id'
    ).fetchall()


def child(conn, identifier):
    return conn.execute(
        'SELECT child_id,owner_id,lane,identity_json,status FROM github_control_children WHERE child_id=?',
        (identifier,),
    ).fetchone()


def state(conn, identifier):
    return conn.execute('SELECT status,identity_json FROM github_control_children WHERE child_id=?',
                        (identifier,)).fetchone()


def record_starting(conn, identifier, owner, lane):
    conn.execute('''INSERT INTO github_control_children
        (child_id,owner_id,lane,identity_json,status) VALUES (?,?,?,'{}','starting')''',
        (identifier, owner, lane))


def claim_starting(conn, identifier, identity):
    return conn.execute(
        "UPDATE github_control_children SET status='running',identity_json=? WHERE child_id=? AND status='starting'",
        (json.dumps(identity), identifier),
    ).rowcount


def cancel_starting(conn, identifier):
    return conn.execute("UPDATE github_control_children SET status='stopped' WHERE child_id=? AND status='starting'",
                        (identifier,)).rowcount


def mark_retired(conn, identifier):
    return conn.execute(
        "UPDATE github_control_children SET status='stopped' WHERE child_id=? AND status IN ('starting','running')",
        (identifier,),
    ).rowcount


def mark_stopped(conn, identifier):
    conn.execute("UPDATE github_control_children SET status='stopped' WHERE child_id=?", (identifier,))


def prune_stopped(conn, owner, *, keep):
    conn.execute("DELETE FROM github_control_children WHERE owner_id=? AND status='stopped' AND child_id!=?",
                 (owner, keep))


def previous_owner_children(conn, *, scope_key, current_owner):
    return conn.execute('''SELECT DISTINCT c.child_id,c.identity_json,c.status,c.owner_id,c.lane
        FROM github_control_children c JOIN leases l ON l.owner_id=c.owner_id
        WHERE c.status IN ('starting','running') AND c.owner_id!=?
          AND l.resource_type='daemon' AND l.resource_key=?
          AND (l.status!='active' OR julianday(l.expires_at)<=julianday('now'))''',
        (current_owner, scope_key)).fetchall()
