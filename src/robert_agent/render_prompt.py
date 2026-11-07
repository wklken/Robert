#!/usr/bin/env python3
"""Compatibility CLI entrypoint; implementation lives in robert_agent.context.prompts."""
from robert_agent.context.prompts import main


if __name__ == "__main__":
    raise SystemExit(main())
