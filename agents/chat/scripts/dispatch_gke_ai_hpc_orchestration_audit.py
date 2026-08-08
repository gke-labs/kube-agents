#!/usr/bin/env python3
"""Cron entry point for the daily GKE AI/ML & HPC orchestration audit — see platform_cron_dispatch.py."""

from platform_cron_dispatch import main

raise SystemExit(main("gke-ai-hpc-orchestration-audit"))
