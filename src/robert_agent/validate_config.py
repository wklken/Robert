#!/usr/bin/env python3
"""Compatibility CLI entrypoint; implementation lives in robert_agent.configuration.command."""
from robert_agent.configuration.command import main


if __name__ == "__main__":
    raise SystemExit(main())
