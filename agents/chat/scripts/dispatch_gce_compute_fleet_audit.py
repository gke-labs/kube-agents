#!/usr/bin/env python3
"""Cron entry point for the daily GCE Compute Engine & MIG fleet audit — see platform_cron_dispatch.py."""

from platform_cron_dispatch import main

raise SystemExit(main("gce-compute-fleet-audit"))
