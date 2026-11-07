#!/usr/bin/env python3
"""Compatibility CLI entrypoint; implementation lives in robert_agent.acceptance.publish_dedupe."""
from robert_agent.acceptance.publish_dedupe import main


if __name__ == "__main__":
    raise SystemExit(main())
