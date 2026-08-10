#!/usr/bin/env python3
"""Cron entry point for the daily security & RBAC posture audit — see platform_cron_dispatch.py."""

from platform_cron_dispatch import main

raise SystemExit(main("compliance-audit"))
