#!/usr/bin/env python3
"""Command entry point for v2 configuration validation."""

import argparse

from robert_agent.foundation.commands import emit
from robert_agent.configuration.validation import validate_config as validate_config


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    parser.add_argument('--skip-external', action='store_true')
    args = parser.parse_args(argv)
    result = validate_config(args.config, skip_external=args.skip_external)
    return emit(result, 0 if result['ok'] else 3)


if __name__ == '__main__':
    raise SystemExit(main())
