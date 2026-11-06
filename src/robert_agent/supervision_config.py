"""Validated, bounded settings for the independent observation service."""

DEFAULTS = {'mode': 'observe', 'poll_seconds': 60, 'batch_size': 100, 'stall_seconds': 300, 'diagnosis': None, 'recovery': None, 'repair': None, 'build_manifest': None}


def normalize(raw):
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError('supervision must be a mapping')
    unknown = set(raw) - set(DEFAULTS)
    if unknown:
        raise ValueError('unsupported supervision fields: ' + ', '.join(sorted(unknown)))
    result = {**DEFAULTS, **raw}
    if result['mode'] not in ('off', 'observe', 'recover_known'):
        raise ValueError('supervision.mode must be off, observe or recover_known')
    for key, maximum in (('poll_seconds', 3600), ('batch_size', 500), ('stall_seconds', 86400)):
        if type(result[key]) is not int or not 1 <= result[key] <= maximum:
            raise ValueError(f'supervision.{key} must be an integer between 1 and {maximum}')
    from robert_agent.supervision_diagnose import normalize_diagnosis
    result['diagnosis'] = normalize_diagnosis(result['diagnosis'])
    recovery = result['recovery']
    if recovery is not None:
        from robert_agent.supervision_actions import STAGES
        if not isinstance(recovery, dict) or set(recovery) != {'allowed_actions', 'repositories'}:
            raise ValueError('supervision.recovery requires allowed_actions and repositories')
        for field in ('allowed_actions', 'repositories'):
            values = recovery[field]
            if not isinstance(values, list) or not values or len(values) > 100 or any(not isinstance(v, str) or not v for v in values):
                raise ValueError('supervision.recovery lists must contain bounded nonempty strings')
        if set(recovery['allowed_actions']) - set(STAGES):
            raise ValueError('supervision.recovery contains an unsupported action')
        result['recovery'] = {key: sorted(set(value)) for key, value in recovery.items()}
    from robert_agent.supervision_repair import normalize_repair
    result['repair'] = normalize_repair(result['repair'])
    if result['build_manifest'] is not None and (not isinstance(result['build_manifest'], str) or not result['build_manifest'].strip()):
        raise ValueError('supervision.build_manifest must be a nonempty path')
    return result
