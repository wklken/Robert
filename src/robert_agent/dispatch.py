#!/usr/bin/env python3
"""Compatibility CLI entrypoint; implementation lives in robert_agent.adapters.processes.worker."""
from robert_agent.adapters.processes.worker import main


if __name__ == "__main__":
    raise SystemExit(main())
