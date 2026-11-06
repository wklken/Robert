#!/usr/bin/env python3
"""Strict v2 configuration boundary; internal normalized values are not inputs."""
import argparse
from copy import deepcopy
import json
import math
from pathlib import Path
import sys

import yaml

from robert_agent import supervision_config, worker_adapters
from robert_agent.common import emit
from robert_agent.resource_files import resource
from robert_agent.route_config import load_route_policies, resolve_route_config


class _ConfigLoader(yaml.SafeLoader):
    pass


def _mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        if not isinstance(key, str):
            raise ValueError('configuration keys must be strings')
        if key in result:
            raise ValueError(f'duplicate configuration key: {key}')
        result[key] = loader.construct_object(value_node, deep=True)
    return result


_ConfigLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def load_config(path):
    data = yaml.load(Path(path).expanduser().read_text(encoding='utf-8'), Loader=_ConfigLoader)
    if not isinstance(data, dict):
        raise ValueError('config root must be a mapping')
    return data


def _schema_value(value, spec, schema, path):
    """Apply the subset used by our packaged config schema, including defaults.

    This is not a general JSON Schema implementation or an external schema API.
    The packaged schema is the single source of structural rules and defaults.
    """
    if '$ref' in spec:
        spec = schema['$defs'][spec['$ref'].rsplit('/', 1)[1]]
    if 'oneOf' in spec:
        for alternative in spec['oneOf']:
            try:
                return _schema_value(value, alternative, schema, path)
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
        raise ValueError(f'{path} must be {spec["const"]}; see docs/config-v2-upgrade.md')
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
            result[name] = _schema_value(value[name], child, schema, f'{path}.{name}')
        for name, child in properties.items():
            resolved = schema['$defs'][child['$ref'].rsplit('/', 1)[1]] if '$ref' in child else child
            if name not in result and 'default' in resolved:
                result[name] = _schema_value(deepcopy(resolved['default']), child, schema, f'{path}.{name}')
        return result
    if kind == 'array':
        if len(value) < spec.get('minItems', 0):
            raise ValueError(f'{path} must contain at least {spec["minItems"]} items')
        if 'maxItems' in spec and len(value) > spec['maxItems']:
            raise ValueError(f'{path} must contain at most {spec["maxItems"]} items')
        if spec.get('uniqueItems') and len({json.dumps(v, sort_keys=True) for v in value}) != len(value):
            raise ValueError(f'{path} entries must be unique')
        return [_schema_value(item, spec['items'], schema, f'{path}[{index}]') for index, item in enumerate(value)]
    if kind == 'string' and len(value.strip()) < spec.get('minLength', 0):
        raise ValueError(f'{path} must not be empty')
    for bound, failed in (
        ('minimum', lambda n: value < n), ('maximum', lambda n: value > n),
        ('exclusiveMinimum', lambda n: value <= n),
    ):
        if bound in spec and failed(spec[bound]):
            raise ValueError(f'{path} violates {bound} {spec[bound]}')
    return value


def _workers(config):
    result = []
    credentials = {'GH_TOKEN', 'GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN', 'GITHUB_ENTERPRISE_TOKEN'}
    for name, raw in config['workers'].items():
        if name != name.strip():
            raise ValueError(f'workers.{name} name must not contain surrounding whitespace')
        if credentials.intersection(name.strip() for name in raw['environment_allowlist']):
            raise ValueError(f'workers.{name}.environment_allowlist must not include GitHub credentials')
        normalized = worker_adapters.normalize_worker_agent_config({
            'worker_agent': raw['adapter'], 'worker_command': raw['command'],
            'timeout_seconds': raw['timeout_seconds'], 'environment_allowlist': raw['environment_allowlist'],
        })
        result.append({'name': name, **normalized, 'default_model': raw['model'].strip(),
                       'default_effort': raw['effort'].strip(),
                       'fallback_model': raw['fallback_model'].strip() if 'fallback_model' in raw else None})
    return result


def _normalize(config, path, skip_external):
    schema = json.loads(resource('schemas/config.schema.json').read_text(encoding='utf-8'))
    config = _schema_value(config, schema, schema, 'config')
    data_dir = Path(config['data_dir']).expanduser()
    database = config['database'].strip()
    if Path(database).name != database or database in {'.', '..'}:
        raise ValueError(f'database must be a filename, not a path: {database}')
    if data_dir.exists() and not data_dir.is_dir():
        raise ValueError(f'data_dir is not a directory: {data_dir}')
    config['github']['account'] = config['github']['account'].strip()
    workers = _workers(config)
    by_name = {w['name']: w for w in workers}
    if config['default_worker'] not in by_name:
        raise ValueError('default_worker must reference a configured worker')
    default_worker = by_name[config['default_worker']]
    scheduler = config['scheduler']
    supervision = config['supervision']
    daemon = config['daemon']
    polling = config['github']['polling']
    result = {
        'ok': True, 'status': 'valid', 'version': 2, 'config_path': str(path),
        'data_dir': str(data_dir), 'db_path': str(data_dir / database),
        'github': config['github'], 'github_account': config['github']['account'],
        'workers': workers, 'default_worker': default_worker,
        'worker_agent': default_worker['agent'], 'worker_command': default_worker['command'],
        'worker_agent_config': default_worker,
        'python_bin': config.get('python_bin', sys.executable or 'python3').strip(),
        'max_concurrency': scheduler['max_concurrency'],
        'model_max_concurrency': scheduler['model_max_concurrency'],
        'lease_ttl_seconds': scheduler['lease_ttl_seconds'],
        'heartbeat_stale_seconds': supervision['heartbeat_stale_seconds'],
        'hard_timeout_seconds': supervision['hard_timeout_seconds'],
        'worker_startup_grace_seconds': supervision['startup_grace_seconds'],
        'routes': config['routes'], 'repos': [],
        'skills': {'search_paths': [str(Path(p).expanduser()) for p in config['skills']['search_paths']]},
        'maintenance': config['maintenance'],
        'supervision': supervision_config.normalize({
            key: value for key, value in supervision.items() if key in supervision_config.DEFAULTS
        }),
        # Runtime control-loop names; only the schema above defines accepted YAML.
        'daemon': {
            'enabled': daemon['enabled'], 'run_on_start': daemon['scan_on_start'],
            'local_poll_seconds': scheduler['local_poll_seconds'],
            'github_poll_seconds': polling['fallback_seconds'],
            'github_poll_when_full_seconds': polling['synchronous']['full_capacity_seconds'],
            'rate_limit_cache_seconds': polling['synchronous']['rate_limit_cache_seconds'],
            **config['github']['rate_limit'],
            'live_run_timeout_seconds': daemon['control_timeouts']['github_scan_seconds'],
            'local_drain_timeout_seconds': daemon['control_timeouts']['local_cycle_seconds'],
        },
        'skip_external': bool(skip_external),
    }
    known_routes = {p['id']: p for p in load_route_policies()}
    seen = set()
    for index, raw in enumerate(config['repos']):
        name = raw['full_name'].strip()
        if name in seen:
            raise ValueError(f'duplicate repo full_name: {name}')
        seen.add(name)
        if name == 'OWNER/REPO' or 'your-github-login' in raw['trusted_actors']:
            raise ValueError(f'repos[{index}] contains placeholder values')
        checkout = Path(raw['checkout']).expanduser()
        if not checkout.exists() or not (checkout / '.git').exists():
            raise ValueError(f'repos[{index}].checkout is not a git checkout: {checkout}')
        limit = raw.get('max_concurrency', scheduler['max_concurrency'])
        if limit > scheduler['max_concurrency']:
            raise ValueError(f'repos[{index}].max_concurrency exceeds global max_concurrency')
        if raw['pr_automation']['ci']['enabled'] and not raw['pr_automation']['ci']['check_allowlist']:
            raise ValueError(f'repos[{index}].pr_automation.ci.check_allowlist must not be empty when enabled')
        ci = raw['pr_automation']['ci']
        ci['check_allowlist'] = [check.strip() for check in ci['check_allowlist']]
        if len(ci['check_allowlist']) != len(set(ci['check_allowlist'])):
            raise ValueError(f'repos[{index}].pr_automation.ci.check_allowlist entries must be unique')
        result['repos'].append({**raw, 'full_name': name, 'repo_root': str(checkout),
            'worktree_root': str(Path(raw['worktrees']).expanduser()),
            'default_base_branch': raw['default_branch'], 'max_concurrency': limit,
            'github_account': raw.get('github_account', config['github']['account']).strip()})
    for label, routes in [('routes', config['routes']), *[(f'repos[{i}].routes', r.get('routes', {})) for i, r in enumerate(result['repos'])]]:
        unknown = set(routes) - set(known_routes)
        if unknown:
            raise ValueError(f'{label} contains unknown route: {sorted(unknown)[0]}')
        # Validate raw references even when repository overrides shadow them.
        # Native first-worker rules apply to the actual merged repository route.
        for route_id, override in routes.items():
            names = [override['worker']] if 'worker' in override else []
            for field, allowed_route in (('multi_agent_review', 'review-pr'), ('multi_agent_vote', 'vote')):
                if field in override:
                    if route_id != allowed_route:
                        raise ValueError(f'{field} is only supported by {allowed_route}')
                    names.extend(override[field].get('workers', []))
            for name in names:
                if name not in by_name:
                    raise ValueError(f'{label}.{route_id} references unknown worker: {name}')
    for repo in result['repos']:
        for policy in known_routes.values():
            resolve_route_config(result, repo, policy)
    return result


def validate_config(config_path, skip_external=False):
    try:
        return _normalize(load_config(config_path), config_path, skip_external)
    except (KeyError, OSError, TypeError, ValueError, yaml.YAMLError) as exc:
        return {'ok': False, 'status': 'failed_config', 'safe_error': f'invalid config: {exc}'}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    parser.add_argument('--skip-external', action='store_true')
    args = parser.parse_args(argv)
    result = validate_config(args.config, skip_external=args.skip_external)
    return emit(result, 0 if result['ok'] else 3)


if __name__ == '__main__':
    raise SystemExit(main())
