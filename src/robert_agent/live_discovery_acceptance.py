#!/usr/bin/env python3
"""Compatibility CLI entrypoint; implementation lives in robert_agent.acceptance.discovery."""
from robert_agent.acceptance.discovery import main


if __name__ == "__main__":
    raise SystemExit(main())
