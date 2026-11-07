#!/usr/bin/env python3
"""Compatibility CLI entrypoint; implementation lives in robert_agent.interfaces.web.server."""
from robert_agent.interfaces.web.server import main


if __name__ == "__main__":
    raise SystemExit(main())
