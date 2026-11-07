#!/usr/bin/env python3
"""Compatibility CLI entrypoint; implementation lives in robert_agent.worker.heartbeat."""
from robert_agent.worker.heartbeat import main


if __name__ == "__main__":
    raise SystemExit(main())
