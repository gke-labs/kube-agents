#!/usr/bin/env python3
"""Cron entry point for the daily GKE Runtime Telemetry & Linux Kernel audit — see platform_cron_dispatch.py."""

from platform_cron_dispatch import main

raise SystemExit(main("gke-runtime-telemetry-audit"))
