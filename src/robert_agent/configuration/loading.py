"""Load the strict v2 YAML document."""

from pathlib import Path

import yaml


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
