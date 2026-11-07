#!/usr/bin/env python3
"""Compatibility CLI entrypoint; implementation lives in robert_agent.worker.result."""
from robert_agent.worker.result import main


if __name__ == "__main__":
    raise SystemExit(main())
