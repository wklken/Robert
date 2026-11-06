"""Validated, bounded settings for the independent observation service."""

DEFAULTS = {'mode': 'observe', 'poll_seconds': 60, 'batch_size': 100, 'stall_seconds': 300}


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
    return result
