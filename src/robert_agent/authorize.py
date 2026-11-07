#!/usr/bin/env python3
"""Compatibility CLI entrypoint; implementation lives in robert_agent.security.authorization."""
from robert_agent.security.authorization import main


if __name__ == "__main__":
    raise SystemExit(main())
