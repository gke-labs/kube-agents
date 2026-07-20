#!/usr/bin/env python3
"""Wake-gate for the ``bootstrap-inventory-scan`` cron job.

The scan is an LLM job on a 1-minute interval so a transient failure retries
on the next tick. Once discovery has produced ``INVENTORY.md`` there is nothing
left to do, but an interval job keeps firing forever while enabled — each fire
otherwise paying for a full model turn just to notice the file already exists.

This script runs as the scan job's pre-check. The cron scheduler parses its
last stdout line as a wake gate: ``{"wakeAgent": false}`` skips the LLM run
entirely (no tokens), any other output wakes the agent normally. So once the
report exists (or onboarding is complete), the scan becomes a near-free no-op
until the plugin/delivery cleanup removes it.

It also gates on ``.bootstrap_completed`` so that removing ``INVENTORY.md`` at
cleanup can never re-trigger a fresh scan.

Note: a pre-run script that exits successfully with EMPTY stdout makes the
scheduler treat the run as "nothing to report" and skip the agent entirely
(``_build_job_prompt`` returns ``None``). So the wake path must print a
non-empty gate line (``{"wakeAgent": true}``), not nothing.
"""

import os
from pathlib import Path


def _data_dir() -> Path:
    return Path(os.environ.get("HERMES_HOME", "/opt/data"))


def should_skip(data_dir: Path) -> bool:
    """True when discovery is already done and the scan LLM run can be skipped."""
    return (data_dir / "INVENTORY.md").exists() or (data_dir / ".bootstrap_completed").exists()


def main(data_dir: Path | None = None) -> int:
    if data_dir is None:
        data_dir = _data_dir()
    # The scheduler parses the last stdout line as the wake gate. Always emit a
    # non-empty line: false -> skip the LLM run; true -> wake and run discovery
    # (empty stdout would make the scheduler skip the agent as "nothing to do").
    if should_skip(data_dir):
        print('{"wakeAgent": false}')
    else:
        print('{"wakeAgent": true}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
