#!/usr/bin/env python3
"""Cron entry point for the weekly fleet consistency drift audit — see platform_cron_dispatch.py."""

from platform_cron_dispatch import main

raise SystemExit(main("fleet-consistency-drift"))
