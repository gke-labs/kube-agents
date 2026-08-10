#!/usr/bin/env python3
"""Cron entry point for the half-hourly GitHub issue resolver — see platform_cron_dispatch.py."""

from platform_cron_dispatch import main

raise SystemExit(main("github-issue-resolver"))
