#!/usr/bin/env python3
"""Compatibility CLI entrypoint; implementation lives in robert_agent.work.streams."""
from robert_agent.work.streams import main


if __name__ == "__main__":
    raise SystemExit(main())
