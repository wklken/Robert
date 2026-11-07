#!/usr/bin/env python3
"""Compatibility CLI entrypoint; implementation lives in robert_agent.execution.inspection."""
from robert_agent.execution.inspection import main


if __name__ == "__main__":
    raise SystemExit(main())
