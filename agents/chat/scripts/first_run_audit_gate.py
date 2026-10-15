#!/usr/bin/env python3
"""No-op script for the retired ``first-run-quick-value-audit`` cron job.

``oobe.py`` runs the first-run audits, and the image no longer ships this job.
``cron_jobs_sync.py`` never prunes a volume entry, so an install keeps the copy
it already holds: disabled on one that booted an image shipping ``enabled:
false``, still enabled on one that never did. Either copy names this script,
so it stays, and does nothing, until something removes the id from volumes.
"""

import sys

if __name__ == "__main__":
    sys.exit(0)
