"""Put a 30-60 s floor under the retry wait for a 429 that names no window.

One anchored edit in ``agent/turn_recovery.py``, beside the one
``apply_rate_limit_retry_delay.py`` makes. ``compute_error_backoff`` decides
``_retry_after`` from a ``Retry-After`` header, then a ``retry_after`` body
field, then (the retryDelay patch) a Google ``RetryInfo.retryDelay`` in the
body, and then computes ``wait_time`` from it. Between the retryDelay branch
and that wait, when a rate-limited error still has no answer, ask
``hermes_cli/rate_limit_backoff_floor.py`` for a jittered 30-60 s and set
``_retry_after`` to it. The rest of upstream's function then behaves as if
the provider had named the wait: ``wait_time`` takes it, the adaptive Z.AI
backoff is skipped (gated on ``_retry_after is None``) and the status line
says how long the wait is.

The order of the two patches matters and is enforced: this one refuses a file
without the retryDelay marker, because a body that does name a delay has to
win over the floor, and both inserts sit ahead of the same anchor, so the
later one runs later. The insert does not consume the anchor, so the anchor
count cannot tell a fresh file from a patched one; the marker check can. See
the module docstring in rate_limit_backoff_floor.py for the incident.
"""

from __future__ import annotations

import sys
from pathlib import Path

import patchlib

LOOP_RELATIVE = "agent/turn_recovery.py"

# The one line in the file that turns ``_retry_after`` into a wait; the same
# anchor the retryDelay patch uses. Inserting ahead of it, after that patch
# has run, puts the floor last among the sources of ``_retry_after``.
WAIT_ANCHOR = (
    "    wait_time = _retry_after if _retry_after is not None else "
    "jittered_backoff(retry_count, base_delay=2.0, max_delay=60.0)\n"
)

#: Text only the retryDelay patch leaves behind; it has to be in the file first.
RETRY_DELAY_MARKER = "_kube_retry_delay_from_error"

FLOOR_INSERT = '''    # kube-agents patch: a 429 that names no reset window, as gemini-4-argon's
    # dynamic-shared-quota 429 does not (no Retry-After, no retry_after field,
    # no RetryInfo), would get the stock 2-6s jittered backoff and spend all
    # three retries inside a quota minute that is still closed. Wait a
    # jittered 30-60s instead, only when nothing above named a wait. See
    # hermes_cli/rate_limit_backoff_floor.py.
    if is_rate_limited and _retry_after is None:
        try:
            from hermes_cli.rate_limit_backoff_floor import (
                rate_limit_floor_seconds as _kube_rate_limit_floor_seconds,
            )

            _retry_after = _kube_rate_limit_floor_seconds()
        except Exception:
            logger.debug("rate-limit floor failed", exc_info=True)
            _retry_after = None
'''

MARKER = "_kube_rate_limit_floor_seconds"


def apply(root: Path) -> None:
    patch = patchlib.Patch(root, LOOP_RELATIVE, prefix="rate-limit-backoff-floor")
    patch.refuse_if_patched(MARKER)
    if RETRY_DELAY_MARKER not in patch.source:
        raise SystemExit(
            f"{patch.prefix} patch: {patch.relative} lacks the retryDelay patch "
            f"({RETRY_DELAY_MARKER!r} is absent); run apply_rate_limit_retry_delay.py "
            "first so a delay the body names wins over the floor"
        )
    patch.substitute(WAIT_ANCHOR, FLOOR_INSERT + WAIT_ANCHOR, label="rate-limit wait")
    patch.commit("floor under the rate-limit wait")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
