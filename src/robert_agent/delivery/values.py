"""delivery values responsibilities."""
import json
from datetime import datetime, timezone
from uuid import uuid4



def now():
    return datetime.now(timezone.utc).isoformat()



def decode_json(value):
    if not value:
        return {}
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return {}



def encode_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)



def id(prefix):
    return f"{prefix}-{uuid4().hex[:12]}"
