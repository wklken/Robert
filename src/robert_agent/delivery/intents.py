"""delivery intents responsibilities."""



def load_intent(conn, action_id):
    row = conn.execute(
        "SELECT * FROM publication_intents WHERE action_id = ?", (action_id,)
    ).fetchone()
    return dict(row) if row else None
