#!/usr/bin/env python3
"""Compatibility CLI entrypoint; implementation lives in robert_agent.acceptance.controlled_e2e."""
from robert_agent.acceptance.controlled_e2e import main


if __name__ == "__main__":
    raise SystemExit(main())
