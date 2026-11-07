#!/usr/bin/env python3
"""Compatibility CLI entrypoint; implementation lives in robert_agent.runtime.scan."""
from robert_agent.runtime.scan import main


if __name__ == "__main__":
    raise SystemExit(main())
