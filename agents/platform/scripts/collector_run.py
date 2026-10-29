#!/usr/bin/env python3
"""Subprocess plumbing shared by the fleet-audit collectors.

One owner for the names every collector needs: `Run`, `default_run`,
`run_and_gate`, `Discovery`, `_text` and `TIMEOUT_RC`. A collector binds them
at import time (`Run = collector_run.Run`,
`default_run = functools.partial(collector_run.default_run, timeout=...)`), so
its tests and fakes keep addressing them through the collector's own name.
`default_run` has no default timeout here on purpose: the collectors do not
agree on one, and each binds its own.
"""

from __future__ import annotations

import json
import subprocess
import time
from typing import Callable, NamedTuple

#: The exit code a timed-out child is reported with, the one `timeout(1)` uses.
TIMEOUT_RC = 124


class Run(NamedTuple):
    """One subprocess's outcome, in the shape the manifest records it."""

    argv: list[str]
    rc: int
    stdout: str
    stderr: str
    duration_s: float


RunFn = Callable[..., Run]


class Discovery(NamedTuple):
    """The projects a collector sweeps, and how complete that list is."""

    projects: list[str]
    # Set when no project could be resolved at all: an empty fleet is then a
    # failure to look, and the manifest says so with a top-level `error`.
    error: str | None
    # Set when discovery resolved part of the fleet, or `--project` narrowed
    # it; the collector turns it into a row the document must account for.
    partial: str | None = None


def _text(output: str | bytes | None) -> str:
    # `TimeoutExpired` carries the child's output as bytes even under `text=True`,
    # and every reader of `Run` matches markers against a str.
    if isinstance(output, bytes):
        return output.decode(errors="replace")
    return output or ""


def default_run(argv: list[str], *, env: dict | None = None, timeout: int) -> Run:
    """The real subprocess call. Every driver takes `run` as a parameter and the
    tests inject a fake in its place, so nothing above this needs a live cluster."""
    t0 = time.monotonic()
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=timeout)
        return Run(argv, proc.returncode, proc.stdout, proc.stderr, time.monotonic() - t0)
    except subprocess.TimeoutExpired as exc:
        return Run(argv, TIMEOUT_RC, _text(exc.stdout), _text(exc.stderr), time.monotonic() - t0)
    except Exception as exc:  # a missing binary, a bad env
        return Run(argv, -1, "", str(exc), time.monotonic() - t0)


def run_and_gate(argv: list[str], *, run: RunFn, env: dict | None = None) -> tuple[object | None, Run]:
    """One JSON-producing command behind a fail-closed gate: `(None, run)` when
    the command failed, printed nothing, or printed something that is not JSON,
    so a caller never reads a truncated or empty answer as "nothing here"."""
    result = run(argv) if env is None else run(argv, env=env)
    if result.rc != 0 or not result.stdout.strip():
        return None, result
    try:
        return json.loads(result.stdout), result
    except json.JSONDecodeError:
        return None, result
