"""Make the retry loop wait out Google's ``retryDelay`` on a 429.

One anchored edit in ``agent/conversation_loop.py``: the rate-limit branch of
the retry loop's error handler reads a ``Retry-After`` header into
``_retry_after`` and then computes ``wait_time`` from it. Between the two, when
the header said nothing, ask ``hermes_cli/rate_limit_retry_delay.py`` for the
``google.rpc.RetryInfo`` delay in the error body. Setting ``_retry_after`` is
what makes the rest of upstream's block behave as if the header had been there:
``wait_time`` takes it, the adaptive Z.AI backoff is skipped (it is gated on
``not _retry_after``) and the status line says how long the wait is.

The insert sits ahead of its anchor rather than consuming it, so the anchor
count cannot tell a fresh file from a patched one; the marker check can. See
the module docstring in rate_limit_retry_delay.py for the incident.
"""

from __future__ import annotations

import sys
from pathlib import Path

import patchlib

LOOP_RELATIVE = "agent/conversation_loop.py"

# The one line in the file that turns the header into a wait. Anchoring on it
# pins the insert to the point where ``_retry_after`` is fully decided and
# ``is_rate_limited`` is in scope, which is what the branch needs.
WAIT_ANCHOR = (
    "                wait_time = _retry_after if _retry_after else "
    "jittered_backoff(retry_count, base_delay=2.0, max_delay=60.0)\n"
)

WAIT_INSERT = '''                # kube-agents patch: Google's 429 carries its reset window in
                # the body (google.rpc.RetryInfo.retryDelay), not in a
                # Retry-After header, and LiteLLM passes that body through as
                # the error text. Without this the retries land 2-6s apart
                # against a ~54s window and exhaust before it opens. Read the
                # body only when the header said nothing; same 600s cap.
                # See hermes_cli/rate_limit_retry_delay.py.
                if is_rate_limited and not _retry_after:
                    try:
                        from hermes_cli.rate_limit_retry_delay import (
                            retry_delay_from_error as _kube_retry_delay_from_error,
                        )

                        _retry_after = _kube_retry_delay_from_error(api_error)
                    except Exception:
                        logger.debug("retryDelay parse failed", exc_info=True)
                        _retry_after = None
'''

MARKER = "_kube_retry_delay_from_error"


def apply(root: Path) -> None:
    patch = patchlib.Patch(root, LOOP_RELATIVE, prefix="rate-limit-retry-delay")
    patch.refuse_if_patched(MARKER)
    patch.substitute(
        WAIT_ANCHOR, WAIT_INSERT + WAIT_ANCHOR, label="rate-limit wait"
    )
    patch.commit("retryDelay fallback for the rate-limit wait")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
