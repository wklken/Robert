#!/usr/bin/env python3
"""Compatibility CLI entrypoint; implementation lives in robert_agent.adapters.git.workspace."""
from robert_agent.adapters.git.workspace import main


if __name__ == "__main__":
    raise SystemExit(main())
