#!/usr/bin/env python3
"""Compatibility CLI entrypoint; implementation lives in robert_agent.delivery.application.publish."""
from robert_agent.delivery.application.publish import main


if __name__ == "__main__":
    raise SystemExit(main())
