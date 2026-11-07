"""Apply the structural rules and defaults of the packaged config schema."""

from copy import deepcopy
import json
import math


def apply_schema(value, spec, schema, path):
    """Apply the subset used by our packaged config schema, including defaults.

    This is not a general JSON Schema implementation or an external schema API.
    The packaged schema is the single source of structural rules and defaults.
    """
    if '$ref' in spec:
        spec = schema['$defs'][spec['$ref'].rsplit('/', 1)[1]]
    if 'oneOf' in spec:
        for alternative in spec['oneOf']:
            try:
                return apply_schema(value, alternative, schema, path)
            except ValueError:
                pass
        raise ValueError(f'{path} must be a command string or nonempty argv list')
    kind = spec.get('type')
    valid = {
        'object': isinstance(value, dict), 'array': isinstance(value, list),
        'string': isinstance(value, str), 'integer': type(value) is int,
        'number': type(value) is int or (type(value) is float and math.isfinite(value)),
        'boolean': type(value) is bool, 'null': value is None,
    }
    if isinstance(kind, list):
        matches = [candidate for candidate in kind if valid[candidate]]
        if not matches:
            raise ValueError(f'{path} must be one of {kind}')
        kind = matches[0]
    if kind == 'null':
        return None
    if kind and not valid[kind]:
        raise ValueError(f'{path} must be {kind}')
    if 'const' in spec and (type(value) is not type(spec['const']) or value != spec['const']):
        raise ValueError(f'{path} must be {spec["const"]}; see docs/reference.md#configuration')
    if 'enum' in spec and value not in spec['enum']:
        raise ValueError(f'{path} must be one of {spec["enum"]}')
    if kind == 'object':
        properties = spec.get('properties', {})
        for name in spec.get('required', []):
            if name not in value:
                raise ValueError(f'{path}.{name} is required')
        if len(value) < spec.get('minProperties', 0):
            raise ValueError(f'{path} must not be empty')
        result = {}
        for name in value:
            if not isinstance(name, str) or not name.strip():
                raise ValueError(f'{path} keys must be nonempty strings')
            child = properties.get(name, spec.get('additionalProperties', False))
            if child is False:
                raise ValueError(f'unsupported config field: {path}.{name}')
            result[name] = apply_schema(value[name], child, schema, f'{path}.{name}')
        for name, child in properties.items():
            resolved = schema['$defs'][child['$ref'].rsplit('/', 1)[1]] if '$ref' in child else child
            if name not in result and 'default' in resolved:
                result[name] = apply_schema(deepcopy(resolved['default']), child, schema, f'{path}.{name}')
        return result
    if kind == 'array':
        if len(value) < spec.get('minItems', 0):
            raise ValueError(f'{path} must contain at least {spec["minItems"]} items')
        if 'maxItems' in spec and len(value) > spec['maxItems']:
            raise ValueError(f'{path} must contain at most {spec["maxItems"]} items')
        if spec.get('uniqueItems') and len({json.dumps(v, sort_keys=True) for v in value}) != len(value):
            raise ValueError(f'{path} entries must be unique')
        return [apply_schema(item, spec['items'], schema, f'{path}[{index}]') for index, item in enumerate(value)]
    if kind == 'string' and len(value.strip()) < spec.get('minLength', 0):
        raise ValueError(f'{path} must not be empty')
    for bound, failed in (
        ('minimum', lambda n: value < n), ('maximum', lambda n: value > n),
        ('exclusiveMinimum', lambda n: value <= n),
    ):
        if bound in spec and failed(spec[bound]):
            raise ValueError(f'{path} violates {bound} {spec[bound]}')
    return value
