#!/usr/bin/env python3
"""Compatibility CLI entrypoint; implementation lives in robert_agent.runtime.control."""
from robert_agent.runtime.control import main


if __name__ == "__main__":
    raise SystemExit(main())
