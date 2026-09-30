#!/usr/bin/env python3
"""Walk the pool's free projects through Boskos, holding each for one visit.

Shared by the periodics that touch every pool project from outside any run:
the pull-request sweep (ci_sweep_agent_pulls.py) and the fleet reconcile
(fleet_reconcile.py). Each acquires a project out of `free` into its own hold
state, visits it, and releases it back, so a project a run holds is never
touched and a run arriving mid-visit waits at its own acquire. No listing
endpoint is needed and no run's state is read.
"""

import http.client
import json
import signal
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

# The Prow wrapper's Boskos conventions (oss-test-infra
# prow/prowjobs/gke-labs/kube-agents/kube-agents-presubmits.yaml): the server
# inside the build cluster, the resource type, and the free state.
DEFAULT_SERVER = "http://boskos.boskos.svc.cluster.local"
RESOURCE_TYPE = "kube-agents-evals-project"
FREE_STATE = "free"
TIMEOUT_SECONDS = 30
# Boskos answers /acquire with 404 when no resource is in the requested state.
NO_RESOURCE_CODE = 404
# Boskos hands out the free project untouched the longest (its list is sorted
# by LastUpdate), and a release stamps the project, so a walk gets every free
# project before it sees one again: a repeat means the pool has been walked and
# anything unvisited is busy. Three in a row is margin; each is a hold of
# MIN_HOLD_SECONDS and nothing more.
MAX_CONSECUTIVE_REPEATS = 3
# Boskos answers a release by reading the project back through a cache that
# its own lease write reaches a moment later. A release within milliseconds of
# the lease can read the pre-lease copy -- no owner -- and be refused "owner
# mismatch ... currently owned by" nobody, while the lease stands until the
# reaper's expiry. So every hold lasts at least this long before its release,
# and a release refused that way is tried once more after the same pause.
MIN_HOLD_SECONDS = 1.0
OWNER_MISMATCH_CODE = 401
# How much of an error body a message keeps: Boskos's names the current owner,
# GitHub's the limit or permission that refused.
ERROR_BODY_CHARS = 300
# The clock and the pause, as module attributes so a test can stand in for both.
clock = time.monotonic
pause = time.sleep
# A hold longer than seconds keeps its LastUpdate fresh with /update, as the
# presubmit does (hack/boskos_heartbeat.sh, same cadence): the pool's reaper
# reclaims a lease whose LastUpdate is stale for about five minutes.
HEARTBEAT_SECONDS = 30
# Prow ends a job with a signal and a grace period before SIGKILL. Python's
# default action skips `finally`, which is where a held project is released;
# converting the signal to an exception is what lets the release run. The
# signals are deferred from the acquire until the hold's `try` is entered and
# again across the release, so one landing in those windows is acted on after
# the project is safe, not instead of it.
TERMINATED_EXIT_CODE = 143
TERMINATION_SIGNALS = (signal.SIGTERM, signal.SIGINT)
# What acquire_and_hold returns when the acquire handed out nothing.
NOT_ACQUIRED = object()
# Before the deferring handler is taken down: a signal the kernel handed to
# another thread a moment ago reaches the main thread's handler at its next
# check, and this is what makes that check happen here, in the hold's frame.
SIGNAL_SETTLE_SECONDS = 0.01

REACH_ERRORS = (urllib.error.HTTPError, OSError, http.client.HTTPException)


class BoskosError(Exception):
    """Boskos answered, but not with what the protocol says."""


class Terminated(Exception):
    """A termination signal arrived; the run unwinds, releasing what it holds."""


def terminate(signum, frame):
    # Later terminations are held back until the next unblock at depth 0
    # (_hold_signals), which raises the first of them: the code unwinding from
    # this raise reaches its next deferred region without a second raise
    # landing between the catch and the region's start.
    _defer_terminations()
    raise Terminated("signal %d" % signum)


def error_body(exc):
    """The first ERROR_BODY_CHARS of an HTTPError's body, read once and kept on it."""
    kept = getattr(exc, "body", None)
    if kept is None:
        try:
            kept = exc.read().decode("utf-8", "replace")[:ERROR_BODY_CHARS].strip()
        except (OSError, ValueError, AttributeError):
            kept = ""
        exc.body = kept
    return kept


def describe(exc):
    """An exception for a log line: an HTTPError with its status and body, else str()."""
    if isinstance(exc, urllib.error.HTTPError):
        body = error_body(exc)
        return "HTTP %d %s%s" % (exc.code, exc.reason, ": " + body if body else "")
    return str(exc)


def _call(server, action, params):
    query = urllib.parse.urlencode(params)
    request = urllib.request.Request("%s/%s?%s" % (server.rstrip("/"), action, query), method="POST")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        error_body(exc)
        raise
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise BoskosError("Boskos answered %s with a body that is not JSON: %s" % (action, exc))


def acquire(server, owner, hold_state, name=None):
    """One free project moved to `hold_state`, or None when there is none.

    With `name`, that project and only that one (Boskos's /acquirebystate);
    None then means it is not free.
    """
    try:
        if name is None:
            resource = _call(
                server,
                "acquire",
                {"type": RESOURCE_TYPE, "state": FREE_STATE, "dest": hold_state, "owner": owner},
            )
        else:
            resources = _call(
                server,
                "acquirebystate",
                {"state": FREE_STATE, "dest": hold_state, "owner": owner, "names": name},
            )
            resource = (resources or [None])[0]
    except urllib.error.HTTPError as exc:
        if exc.code == NO_RESOURCE_CODE:
            return None
        raise
    return (resource or {}).get("name") or None


def release(server, owner, name):
    _call(server, "release", {"name": name, "dest": FREE_STATE, "owner": owner})


def release_settled(server, owner, name, held_since):
    """Release after the hold has lasted MIN_HOLD_SECONDS; on an owner-mismatch
    refusal, the cache lag above, pause once more and release again."""
    remaining = MIN_HOLD_SECONDS - (clock() - held_since)
    if remaining > 0:
        pause(remaining)
    try:
        release(server, owner, name)
    except urllib.error.HTTPError as exc:
        if exc.code != OWNER_MISMATCH_CODE:
            raise
        print("  %s: release refused (%s); releasing again after %.0fs" % (name, describe(exc), MIN_HOLD_SECONDS), file=sys.stderr)
        pause(MIN_HOLD_SECONDS)
        release(server, owner, name)


def update(server, owner, hold_state, name):
    """Refresh the hold's LastUpdate; 401 on an owner mismatch, 409 on a state mismatch."""
    _call(server, "update", {"name": name, "owner": owner, "state": hold_state})


def reset_stranded(server, hold_state, expire, what):
    """Projects an earlier run left in `hold_state` past `expire`, returned to free.

    Returns their names. A failure here is reported and does not stop the walk:
    the projects it would have freed stay where they are until the next run,
    which is no worse than not having asked.
    """
    try:
        stranded = _call(
            server,
            "reset",
            {"type": RESOURCE_TYPE, "state": hold_state, "dest": FREE_STATE, "expire": expire},
        )
    except (BoskosError,) + REACH_ERRORS as exc:
        print("could not reset stranded projects: %s" % describe(exc), file=sys.stderr)
        return []
    names = sorted(stranded or {})
    for name in names:
        print("returned %s to free: left in %s by an earlier %s" % (name, hold_state, what))
    return names


def _heartbeat(server, owner, hold_state, name, stop):
    while not stop.wait(HEARTBEAT_SECONDS):
        try:
            update(server, owner, hold_state, name)
        except (BoskosError,) + REACH_ERRORS as exc:
            print("  %s: heartbeat failed (%s)" % (name, describe(exc)), file=sys.stderr)


# Termination signals deferred while a hold is between its acquire and its
# armed `finally`, or inside its release: the handler below records them and
# the unblock raises the first as Terminated in the hold's own frame. Done
# with Python-level handlers rather than a signal mask because the mask does
# not stop a process-directed signal reaching another thread.
_DEFERRED = []
_HOLD_DEPTH = 0


def _defer(signum, frame):
    _DEFERRED.append(signum)


def _defer_terminations():
    for sig in TERMINATION_SIGNALS:
        if signal.getsignal(sig) is terminate:
            signal.signal(sig, _defer)


def _hold_signals(block):
    global _HOLD_DEPTH
    if block:
        if _HOLD_DEPTH == 0:
            # Swapped before counted: a termination raised out of the swap
            # leaves the depth at 0, and the next unblock restores the
            # handlers it did swap.
            _defer_terminations()
        _HOLD_DEPTH += 1
        return
    _HOLD_DEPTH = max(0, _HOLD_DEPTH - 1)
    if _HOLD_DEPTH:
        return
    time.sleep(SIGNAL_SETTLE_SECONDS)
    for sig in TERMINATION_SIGNALS:
        if signal.getsignal(sig) is _defer:
            signal.signal(sig, terminate)
    if _DEFERRED:
        signum = _DEFERRED[0]
        _DEFERRED.clear()
        raise Terminated("signal %d" % signum)


def acquire_and_hold(server, owner, hold_state, acquire_fn, visit, release_failures, heartbeat=False):
    """Acquire a project with acquire_fn(), run visit(name) with it held, and
    release it whatever happens; NOT_ACQUIRED when acquire_fn() returned None.

    With `heartbeat`, a thread refreshes the hold every HEARTBEAT_SECONDS for
    as long as the visit runs. Termination signals are deferred from the
    acquire until the visit starts and across the release, so none is acted
    on between a project being acquired and its `finally` being armed, or
    while it is being given back. A release that fails is recorded in
    `release_failures` and not raised: the project stays in `hold_state` until
    the next run's reset returns it, and an exception already unwinding (a
    termination) is not replaced by this one.
    """
    _hold_signals(True)
    try:
        name = acquire_fn()
        if name is None:
            return NOT_ACQUIRED
        held_since = clock()
        stop = threading.Event()
        beater = None
        unblocked = False
        try:
            if heartbeat:
                # Bound to `beater` only once started: a start that fails must
                # not leave a thread the finally would try to join before the
                # release.
                thread = threading.Thread(target=_heartbeat, args=(server, owner, hold_state, name, stop), daemon=True)
                thread.start()
                beater = thread
            # Marked before the unblock: the unblock gives the hold up (it
            # decrements) before it can raise a deferred termination, and the
            # release after that raise must still run under held signals.
            unblocked = True
            _hold_signals(False)
            return visit(name)
        finally:
            # A termination raised out of the swap below (a signal in the
            # moment before the handlers are held) must not skip the release:
            # the raise itself leaves later signals held, so the release runs.
            # The hold is re-taken only if it was given up: a heartbeat that
            # failed to start never reached the unblock, and a second block
            # would leave the depth above zero for the rest of the process.
            try:
                if unblocked:
                    _hold_signals(True)
            finally:
                stop.set()
                if beater is not None:
                    beater.join()
                try:
                    release_settled(server, owner, name, held_since)
                except (BoskosError,) + REACH_ERRORS as exc:
                    print("  %s: release failed (%s); the next run's reset returns it" % (name, describe(exc)), file=sys.stderr)
                    release_failures[name] = "release failed: %s" % describe(exc)
    finally:
        _hold_signals(False)


def hold(server, owner, hold_state, name, visit, release_failures, heartbeat=False):
    """Run visit(name) with an already acquired project held; see acquire_and_hold."""
    return acquire_and_hold(server, owner, hold_state, lambda: name, visit, release_failures, heartbeat=heartbeat)


def walk(server, owner, hold_state, pool_size, visit, heartbeat=False, release_failures=None):
    """Offer every project Boskos will hand out as free to visit(name), once each.

    Returns (visited names in order, release failures by name). Bounded twice:
    by consecutive repeats, and by an absolute count no pool can reach, so a
    Boskos that keeps answering cannot hold the job open.
    """
    visited = []
    seen = set()
    # The caller's dict when given, so a release that fails before a
    # termination unwinds the walk is still on the caller's record.
    release_failures = release_failures if release_failures is not None else {}
    repeats = 0
    for _ in range(2 * pool_size + MAX_CONSECUTIVE_REPEATS):

        def once(project):
            nonlocal repeats
            if project in seen:
                repeats += 1
                return False
            repeats = 0
            seen.add(project)
            visited.append(project)
            visit(project)
            return True

        outcome = acquire_and_hold(
            server, owner, hold_state, lambda: acquire(server, owner, hold_state), once, release_failures, heartbeat=heartbeat
        )
        if outcome is NOT_ACQUIRED or repeats >= MAX_CONSECUTIVE_REPEATS:
            break
    return visited, release_failures
