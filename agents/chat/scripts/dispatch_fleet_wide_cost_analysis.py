#!/usr/bin/env python3
"""Cron entry point for the weekly fleet waste audit — see platform_cron_dispatch.py."""

from platform_cron_dispatch import main

raise SystemExit(main("fleet-wide-cost-analysis"))
