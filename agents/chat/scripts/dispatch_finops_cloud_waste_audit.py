#!/usr/bin/env python3
"""Cron entry point for the daily FinOps & Cloud Resource Waste audit — see platform_cron_dispatch.py."""

from platform_cron_dispatch import main

raise SystemExit(main("finops-cloud-waste-audit"))
