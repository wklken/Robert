#!/usr/bin/env python3
"""Compatibility CLI entrypoint; implementation lives in robert_agent.runtime.cycle."""
from robert_agent.runtime.cycle import main


if __name__ == "__main__":
    raise SystemExit(main())
