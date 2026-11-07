"""delivery intents responsibilities."""

from robert_agent.persistence import records as db_records
from robert_agent.persistence.queries import publication_intents as db_read_publication_intents


def load_intent(conn, action_id):
    row = db_records.first(db_read_publication_intents.find_record_by_action_id(conn, action_id))
    return dict(row) if row else None
