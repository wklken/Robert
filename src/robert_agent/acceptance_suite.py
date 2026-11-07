#!/usr/bin/env python3
"""Compatibility CLI entrypoint; implementation lives in robert_agent.acceptance.suite."""
from robert_agent.acceptance.suite import main


if __name__ == "__main__":
    raise SystemExit(main())
