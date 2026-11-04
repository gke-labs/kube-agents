"""A floor under the retry wait for a 429 that names no reset window.

The gap
-------
``compute_error_backoff`` in ``agent/turn_recovery.py`` (Hermes v2026.9.14)
waits out a ``Retry-After`` header or a ``retry_after`` body field, and, with
the retryDelay patch beside this one (``rate_limit_retry_delay.py``), a Google
``RetryInfo.retryDelay`` in the error body. A 429 that carries none of the
three falls to ``jittered_backoff(base_delay=2.0, max_delay=60.0)``: the
default three retries land roughly 2-3 s, 4-6 s and 8-12 s after each failure,
about 20 s end to end.

gemini-4-argon on Vertex's dynamic shared quota sends exactly that 429. Its
body reads "TPM quota exhausted. No spare capacity is currently available to
serve over-limit requests" with no header, no ``retry_after`` field and no
``RetryInfo`` (0 of 16 such errors in one gateway log carried a delay). The
quota is a per-minute window, so a worker that spends its three retries
inside 20 s finds it still closed, exits 75 (``quiet_rate_limit_exit``), and
the bridge reports the rep as ``hermes-rate-limited``: an infrastructure
loss the harness never grades. On the first Argon day (2026-10-29) that was
36 reps, three in a row on one case, and one presubmit verdict lost to it
(gke-labs/kube-agents#4238). No Claude day had any.

What it does
------------
:func:`rate_limit_floor_seconds` returns a uniform draw from
[:data:`FLOOR_MIN_SECONDS`, :data:`FLOOR_MAX_SECONDS`], 30 to 60 s. Three
such waits span at least ninety seconds, past the minute the quota resets
on, and the jitter keeps a fan-out of workers that hit the same wall from
coming back in step. The loop takes the draw as ``_retry_after`` only when
the header, the field and the body all said nothing, so a provider that
names its window keeps that number, and the cap on the header path (600 s)
still bounds everything. Nothing here changes the retry count: the worst
case is three minutes more on a turn that would otherwise have ended the rep.
"""

from __future__ import annotations

import random

#: The shortest and longest wait, in seconds, for a 429 that named none.
#: 30 s is half a quota minute: with three retries the last one lands after
#: at least 90 s. 60 s matches ``jittered_backoff``'s own ceiling in
#: ``agent/turn_recovery.py``, and stays well under the 600 s header cap.
FLOOR_MIN_SECONDS = 30.0
FLOOR_MAX_SECONDS = 60.0


def rate_limit_floor_seconds(*, low=FLOOR_MIN_SECONDS, high=FLOOR_MAX_SECONDS, rng=random):
    """A jittered wait in ``[low, high]`` seconds for a 429 that named no window.

    ``rng`` is anything with ``uniform(a, b)``; the loop passes nothing and
    gets the module's ``random``. A bound that is not a positive, ordered pair
    raises ``ValueError``: the caller wraps this in a ``try`` that falls back
    to the stock backoff, so a bad constant costs the floor, not the worker.
    """
    low = float(low)
    high = float(high)
    if not 0 < low <= high:
        raise ValueError(f"rate-limit floor bounds must satisfy 0 < low <= high, got {low!r}..{high!r}")
    return rng.uniform(low, high)
