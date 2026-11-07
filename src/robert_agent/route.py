#!/usr/bin/env python3
"""Compatibility CLI entrypoint; implementation lives in robert_agent.workflows.routes."""
from robert_agent.workflows.routes import main


if __name__ == "__main__":
    raise SystemExit(main())
