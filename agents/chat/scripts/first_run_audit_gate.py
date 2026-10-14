#!/usr/bin/env python3
"""No-op script for the retired ``first-run-quick-value-audit`` cron job.

``oobe.py`` runs the first-run audits. The entry still ships, disabled, because
``cron_jobs_sync.py`` never prunes a volume entry and the image's ``enabled``
is what switches off a copy an install already holds. A disabled ``no_agent``
entry still needs its script to exist, so this file stays until the entry is
dropped.
"""

import sys

if __name__ == "__main__":
    sys.exit(0)
