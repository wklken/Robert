"""Pure proof checks for durable publication receipts."""
import json
from robert_agent.supervision import rules as supervision_rules


def _object(value):
    decoded = json.loads(value or '{}') if isinstance(value, (str, bytes)) else None
    return decoded if isinstance(decoded, dict) else {}


def matches(facts, action, scope, receipt):
    if (action['intent_status'] != 'published' or _object(action['intent_receipt']) != receipt
            or receipt.get('scope') != scope or receipt.get('result_id') != facts['result_id']
            or not supervision_rules.timestamp(receipt.get('confirmed_at'))
            or receipt.get('confirmation_method') not in {
                'remote_reconciliation', 'remote_deduplication', 'publisher_acknowledgement'}
            or any(receipt.get(key) != action[key] for key in ('action_id', 'task_id', 'action_type'))):
        return False
    if action['action_type'] == 'push_existing_pr':
        return bool(scope.get('result_head_sha') and receipt.get('result_head_sha') == scope['result_head_sha'])
    return bool(receipt.get('external_id') and str(receipt['external_id']) == action['external_id']
                and receipt.get('target_url') and receipt['target_url'] == action['target_url'])
