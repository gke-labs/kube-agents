#!/usr/bin/env python3
"""Deterministic (no-LLM) delivery for first-time onboarding.

This script backs the ``bootstrap-inventory-delivery`` cron job, which runs
with ``no_agent: true``. Its stdout is delivered verbatim by the cron
scheduler to the job's configured target (``deliver: origin`` — the chat the
user first spoke in, bound by the ``bootstrap_onboarding`` plugin).

Delivery is claimed exactly once, and only when discovery has finished AND a
human has connected:

- ``.user_aligned`` present -> a human has opened the chat (set by the plugin;
  never by a background task — see the plugin README).
- ``.bootstrap_completed`` absent -> the report has not been delivered yet.
- ``INVENTORY.md`` present  -> the background scan has produced the report.

The two markers are this pod's, and are checked first. The report is not: the
prioritization worker writes it through its terminal, and with the shell
sandbox on that terminal is the sandbox pod, whose data volume this pod does
not mount. So the report is read over ``sandbox_exec.read_bytes`` when the
sandbox is on, and from ``HERMES_HOME`` when it is off, and only once both
markers say a delivery is due — the ssh read is the one check with a cost.

When all three hold, the script claims delivery, prints ``INVENTORY.md``
(verbatim, or reshaped by ``inventory_presenter`` when ``KAGE_SLACK_UX`` is on
and the job is bound to Slack) and sets the report aside where it was read. Otherwise it
prints nothing, which the ``no_agent`` cron path treats as a silent run (no
message). A report it cannot read, or one over ``REPORT_MAX_BYTES``, fails the
run instead (exit 1), which the scheduler posts as an alert; an unreachable
sandbox stays silent and is retried on the next tick. The first run
``RETIRE_AFTER_SECONDS`` or more after a delivery removes the two onboarding
cron jobs; ``_retire_jobs`` says why the delivering run cannot.

With the flag on, a Slack origin and the credential proxy's Slack relay in the
environment, the report goes out as Block Kit instead (a headline with the
total, the top rows, and "Fix the first one" and "See all N" buttons), posted here
through ``slack_blocks_post`` because the scheduler's delivery takes text only.
Then nothing is printed; any failure prints the text. A failure after the request was
sent may have posted, so that path can send the report twice; see
``_posted_as_blocks``.

Just before the claim, the run registers the sweep's findings in the
findings queue from this pod, from the items and scores the prioritization
worker left beside the report (``_register_findings`` says why the worker's
own registration is not enough). Between the claim and the first byte of
stdout, it marks the report's findings shown in the findings queue, as the paced publisher ``first_report``,
from ``INVENTORY.shown.json`` (what ``inventory_findings.py select`` chose).
That counts them against the day's limit and keeps the hourly findings nudge,
whose hold ends at the claim, from announcing them again as new. The marks are
best-effort: a missing file or an unreachable queue is logged to stderr and the
report is delivered regardless, and the registration is best-effort the
same way.

The claim is what makes "one delivery run per report" true rather than merely likely.
``.bootstrap_completed`` is created with ``O_CREAT | O_EXCL`` *before* anything
reaches stdout, so of two runs racing on the same report — a scheduled tick and
the plugin's ``trigger_job``, say — exactly one can win the create and emit;
the loser exits silently. Checking the marker and then writing it after
delivery would leave both runs inside the same window, and the user would be
sent the entire onboarding report twice.

Because the prioritization stage writes a finished, presentation-ready
``INVENTORY.md``, no LLM is involved in delivery: what that stage produced is
what the user sees, verbatim or, on Slack with the flag on, laid out again by
``inventory_presenter`` without a model call. The sweep's complete findings are a different file
(``INVENTORY.raw.md``) and are never delivered from here.
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import sandbox_exec

SCAN_JOB_ID = "bootstrap-inventory-scan"
DELIVERY_JOB_ID = "bootstrap-inventory-delivery"

# The delivered report is renamed here rather than deleted. It is the only copy
# of a sweep that can take many minutes over a whole fleet, and a chat message
# is easy to lose; keeping it means a re-send is a `cat`, not a re-scan.
DELIVERED_REPORT_NAME = "INVENTORY.delivered.md"
REPORT_NAME = "INVENTORY.md"

# The items the report lists, written by ``inventory_findings.py select``
# (its DEFAULT_SHOWN_PATH and the keys below; test_bootstrap_onboarding_scripts.py
# holds the copies equal), and set aside beside the delivered report once read.
SHOWN_NAME = "INVENTORY.shown.json"
DELIVERED_SHOWN_NAME = "INVENTORY.shown.delivered.json"
SHOWN_ITEMS = "items"
SHOWN_CLASS = "class"
SHOWN_IDS = "ids"
# A few ids per item; far above any real file.
SHOWN_MAX_BYTES = 64 * 1024
# The sweep's extracted findings and the worker's scores, which this run
# registers in the queue: inventory_findings.py's DEFAULT_ITEMS_PATH and
# DEFAULT_SCORES_PATH (test_bootstrap_onboarding_scripts.py holds them equal).
ITEMS_NAME = "INVENTORY.items.json"
SCORES_NAME = "INVENTORY.scores.json"
ITEMS_KEY = "items"
SCORES_KEY = "scores"
# A few KB per finding; far above any fleet's sweep.
BATCH_MAX_BYTES = 4 * 1024 * 1024
# The outcome the queue gives a row the user dismissed (findings_queue._register_one).
SUPPRESSED = "suppressed"

# The findings queue on this pod's loopback, as findings_nudge.py reaches it.
# The cron child inherits SESSION_KV_API_KEY (deploy/docker/plugins/verify_chat_relay.py).
FINDINGS_ENDPOINT_ENV = "SESSION_KV_ENDPOINT"
SESSION_KV_AUTH_ENV = "SESSION_KV_API_KEY"
DEFAULT_FINDINGS_ENDPOINT = "http://127.0.0.1:8699"
SURFACED_PATH = "/v1/findings/{id}/surfaced"
JSON_HEADERS = {"Content-Type": "application/json"}
AUTH_HEADER = "Authorization"
BEARER_PREFIX = "Bearer "
# The paced publisher this report marks as (findings_queue.PACED_PUBLISHERS).
PUBLISHER = "first_report"
# One run id per delivery, so the rows of one item are one addition.
RUN_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
# Short: the report waits on these. A queue that does not answer one mark is
# not asked for the rest.
MARK_TIMEOUT_SECONDS = 5

# The sandbox's data volume, whatever HERMES_HOME says on this side: the same
# path by construction (deploy/sandbox/Dockerfile), and a separate constant
# because it names a directory on the far side of the connection.
SANDBOX_HOME = "/opt/data"

# Far above the report the prioritization stage writes, which is sized for one
# chat message. It bounds what one tick moves over ssh; a report past it is
# refused rather than cut, since a truncated report would be delivered as whole.
REPORT_MAX_BYTES = 256 * 1024
SANDBOX_TIMEOUT_SECONDS = 30

# How old ``.bootstrap_completed`` must be before a run removes the jobs. A
# younger marker may belong to a racing run that is still delivering (see the
# claim in the module docstring), and removing the delivery job under it would discard its
# report. Far above that run's post-claim work: one read and two archives over
# ssh, each bounded by SANDBOX_TIMEOUT_SECONDS, marks that stop at the first
# MARK_TIMEOUT_SECONDS timeout, and a stdout write.
RETIRE_AFTER_SECONDS = 300

# By absolute path, so no PATH entry picks the binary. A function defined under
# this name in a ~/.bashrc the model wrote, which older sandbox images allow,
# still shadows it, since bash allows a slash in a function name; that loses only
# the rename, as the claim comes first.
REMOTE_MV = "/bin/mv"

# The only surface the reshaped report is written for; every other one gets it verbatim.
PRESENTED_PLATFORM = "slack"

# The delivery job's ``origin`` and the keys the plugin writes into it.
ORIGIN_KEY = "origin"
PLATFORM_KEY = "platform"
CHAT_ID_KEY = "chat_id"
THREAD_ID_KEY = "thread_id"


def _data_dir() -> Path:
    return Path(os.environ.get("HERMES_HOME", "/opt/data"))


def _awaiting_delivery(data_dir: Path) -> bool:
    """True when a human is present; main() has already returned on a delivered report.

    The marker lives on this pod, so this costs one stat; it runs before the
    report read, which may cross into the sandbox.
    """
    return (data_dir / ".user_aligned").exists()


def _completed_at(data_dir: Path) -> float | None:
    """When the delivery claim was taken, or None if it has not been."""
    try:
        return (data_dir / ".bootstrap_completed").stat().st_mtime
    except FileNotFoundError:
        return None


def _read_report(data_dir: Path, in_sandbox: bool) -> bytes | None:
    """Up to ``REPORT_MAX_BYTES + 1`` bytes of the report, or None if there is none.

    Raises ``sandbox_exec.SandboxUnavailable`` or ``subprocess.TimeoutExpired``
    when the sandbox did not answer, ``sandbox_exec.SandboxReadFailed`` when the
    read ran and did not return the report, and ``OSError`` when the local file
    could not be read.
    """
    if in_sandbox:
        return sandbox_exec.read_bytes(
            f"{SANDBOX_HOME}/{REPORT_NAME}",
            max_bytes=REPORT_MAX_BYTES + 1,
            timeout=SANDBOX_TIMEOUT_SECONDS,
        )
    try:
        with open(data_dir / REPORT_NAME, "rb") as handle:
            return handle.read(REPORT_MAX_BYTES + 1)
    except FileNotFoundError:
        return None


def _claim_delivery(data_dir: Path) -> bool:
    """Atomically claim the right to deliver the report. True if we won.

    ``O_CREAT | O_EXCL`` is a single filesystem operation, so this is the point
    at which "may I deliver?" and "I am delivering" become indivisible. Called
    before the first byte of the report is written to stdout.

    A False return means another run already claimed it: the caller must emit
    nothing at all.
    """
    completed = data_dir / ".bootstrap_completed"
    try:
        fd = os.open(str(completed), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False  # another run got there first
    except OSError as e:
        # Cannot claim -> cannot safely deliver. Staying silent costs a retry
        # next tick; delivering unclaimed risks sending the report twice.
        sys.stderr.write(f"bootstrap_delivery: could not claim delivery: {e}\n")
        return False
    os.close(fd)
    return True


def _archive(data_dir: Path, in_sandbox: bool, name: str = REPORT_NAME, archived: str = DELIVERED_REPORT_NAME) -> None:
    """Rename ``name`` to ``archived`` where it was read."""
    if not in_sandbox:
        try:
            source = data_dir / name
            if source.exists():
                source.replace(data_dir / archived)
        except OSError as e:
            sys.stderr.write(f"bootstrap_delivery: could not archive {name}: {e}\n")
        return
    # As the terminal's login: the sandbox's /opt/data is that account's and
    # mode 755, so the default login cannot rename inside it.
    try:
        moved = sandbox_exec.run(
            [REMOTE_MV, "-f", "--", f"{SANDBOX_HOME}/{name}", f"{SANDBOX_HOME}/{archived}"],
            principal=sandbox_exec.TERMINAL_PRINCIPAL,
            timeout=SANDBOX_TIMEOUT_SECONDS,
        )
    except Exception as e:
        sys.stderr.write(f"bootstrap_delivery: could not archive {name} in the sandbox: {e}\n")
        return
    if moved.returncode != 0:
        sys.stderr.write(
            f"bootstrap_delivery: could not archive {name} in the sandbox: "
            f"{(moved.stderr or '').strip()}\n"
        )


def _read_beside(data_dir: Path, in_sandbox: bool, name: str, max_bytes: int) -> bytes | None:
    """Up to ``max_bytes + 1`` bytes of ``name`` where the report was read, or None if there is none."""
    if in_sandbox:
        return sandbox_exec.read_bytes(
            f"{SANDBOX_HOME}/{name}", max_bytes=max_bytes + 1, timeout=SANDBOX_TIMEOUT_SECONDS
        )
    try:
        with open(data_dir / name, "rb") as handle:
            return handle.read(max_bytes + 1)
    except FileNotFoundError:
        return None


def _read_shown(data_dir: Path, in_sandbox: bool) -> bytes | None:
    """``SHOWN_NAME``'s bytes where the report was read, or None if there is none."""
    return _read_beside(data_dir, in_sandbox, SHOWN_NAME, SHOWN_MAX_BYTES)


def _read_json_beside(data_dir: Path, in_sandbox: bool, name: str) -> dict | None:
    """``name`` parsed where the report was read, or None if there is none. Raises on anything unusable."""
    raw = _read_beside(data_dir, in_sandbox, name, BATCH_MAX_BYTES)
    if raw is None:
        return None
    if len(raw) > BATCH_MAX_BYTES:
        raise ValueError(f"{name} is larger than {BATCH_MAX_BYTES} bytes")
    parsed = json.loads(raw.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError(f"{name} is not a JSON object")
    return parsed


def _findings_endpoint() -> str:
    return (os.environ.get(FINDINGS_ENDPOINT_ENV) or DEFAULT_FINDINGS_ENDPOINT).rstrip("/")


def _register_findings(data_dir: Path, in_sandbox: bool) -> set[str]:
    """Register every finding the sweep extracted in the findings queue, as
    ``inventory_findings.py register`` does, from this pod.

    The worker runs ``register`` through its terminal. With the shell sandbox
    on, that terminal cannot reach the queue on this pod's loopback, so
    ``register`` exits 13 and nothing reaches the queue; the findings the report
    leaves out would then never reach the findings nudge, which reads only the
    queue, and the report's marks would find no rows. This run can reach it.
    Registering is an upsert, so a batch the worker did register is written
    again unchanged.

    Nothing is registered when the hand-off filed no prioritization card
    (``bootstrap_handoff.NO_RANKING``): no cluster was audited, nothing
    rewrote the items and scores, and any left beside the report are an
    earlier sweep's, whose complete scopes would re-open findings and mark
    current ones absent.

    Returns the ids the queue answered ``suppressed``: rows the user
    dismissed. With the shell sandbox on, ``select`` never learned them, so
    the report may list one; ``_mark_shown`` leaves them unmarked.

    Never raises. Runs before the claim, so it adds nothing to the claimed
    run's work (``RETIRE_AFTER_SECONDS``), and the nudge holds until the claim.
    """
    suppressed: set[str] = set()
    try:
        import bootstrap_handoff  # beside this script in the pod
        import inventory_findings  # beside this script in the pod

        handed_off = bootstrap_handoff._read_marker(data_dir / bootstrap_handoff.HANDOFF_MARKER)
        if handed_off.get("task_id") == bootstrap_handoff.NO_RANKING:
            sys.stderr.write(
                "bootstrap_delivery: the hand-off filed no prioritization card, so any "
                f"{ITEMS_NAME} is an earlier sweep's; registering nothing\n"
            )
            return suppressed
        extracted = _read_json_beside(data_dir, in_sandbox, ITEMS_NAME)
        if extracted is None:
            sys.stderr.write(f"bootstrap_delivery: no {ITEMS_NAME}; registering nothing\n")
            return suppressed
        items = extracted[ITEMS_KEY]
        if not items:
            return suppressed  # a clean fleet: nothing to register, and no scores file
        raw_scores = _read_json_beside(data_dir, in_sandbox, SCORES_NAME)
        if raw_scores is None:
            sys.stderr.write(f"bootstrap_delivery: no {SCORES_NAME}; registering nothing\n")
            return suppressed
        payloads = inventory_findings.build_payloads(items, raw_scores[SCORES_KEY])
        batches = inventory_findings.cluster_batches(payloads, inventory_findings.complete_clusters(raw_scores))
    except Exception as e:
        detail = "; ".join(getattr(e, "errors", None) or [str(e)])
        sys.stderr.write(f"bootstrap_delivery: could not read the sweep's findings; registering nothing: {detail}\n")
        return suppressed
    endpoint = _findings_endpoint()
    for where, batch, scope in batches:
        try:
            result = inventory_findings.post_batch(endpoint, batch, scope)
        except urllib.error.HTTPError as e:
            # This cluster only.
            sys.stderr.write(f"bootstrap_delivery: the findings queue refused {where}'s findings: {e.code}\n")
            continue
        except Exception as e:
            sys.stderr.write(
                f"bootstrap_delivery: the findings queue at {endpoint} did not answer ({e}); "
                "not registering the rest\n"
            )
            return suppressed
        outcomes = result.get("results") if isinstance(result, dict) else None
        for entry in outcomes if isinstance(outcomes, list) else []:
            if isinstance(entry, dict) and entry.get("outcome") == SUPPRESSED:
                suppressed.add(str(entry.get("id")))
    return suppressed


def _post_surfaced(endpoint: str, finding_id: str, body: dict) -> None:
    headers = dict(JSON_HEADERS)
    token = (os.environ.get(SESSION_KV_AUTH_ENV) or "").strip()
    if token:
        headers[AUTH_HEADER] = f"{BEARER_PREFIX}{token}"
    path = SURFACED_PATH.format(id=urllib.parse.quote(finding_id, safe=""))
    request = urllib.request.Request(
        f"{endpoint}{path}", data=json.dumps(body).encode("utf-8"), headers=headers, method="POST"
    )
    with urllib.request.urlopen(request, timeout=MARK_TIMEOUT_SECONDS) as response:
        response.read()


def _mark_shown(data_dir: Path, in_sandbox: bool, suppressed: set[str] = frozenset()) -> bool:
    """Mark every row of every item the report lists shown, as ``PUBLISHER``,
    except the ``suppressed`` ids registration reported: the user dismissed
    them, so they are never pending and spend no slot of the day's limit.
    The queue refuses such a mark anyway; skipping it saves the request.

    Returns whether to set the shown file aside: True unless there is none,
    so a file this run could not read is not marked by a later report.
    Never raises: the claim is taken, so nothing here may stop the report
    going out.
    """
    try:
        raw = _read_shown(data_dir, in_sandbox)
    except Exception as e:
        sys.stderr.write(f"bootstrap_delivery: could not read {SHOWN_NAME}; marking nothing: {e}\n")
        return True
    if raw is None:
        sys.stderr.write(f"bootstrap_delivery: no {SHOWN_NAME}; marking nothing\n")
        return False
    try:
        if len(raw) > SHOWN_MAX_BYTES:
            raise ValueError(f"larger than {SHOWN_MAX_BYTES} bytes")
        items = json.loads(raw.decode("utf-8"))[SHOWN_ITEMS]
        marks = [(str(item[SHOWN_CLASS]), [str(fid) for fid in item[SHOWN_IDS]]) for item in items]
    except Exception as e:
        sys.stderr.write(f"bootstrap_delivery: {SHOWN_NAME} is not readable; marking nothing: {e}\n")
        return True
    endpoint = _findings_endpoint()
    run = datetime.now(timezone.utc).strftime(RUN_FORMAT)
    for added_class, ids in marks:
        body = {"publisher": PUBLISHER, "added_class": added_class, "run": run}
        for finding_id in ids:
            if finding_id in suppressed:
                sys.stderr.write(f"bootstrap_delivery: not marking {finding_id} shown: the user dismissed it\n")
                continue
            try:
                _post_surfaced(endpoint, finding_id, body)
            except urllib.error.HTTPError as e:
                # This row only: an unregistered one is a 404, one the user
                # decided since registration a 400. Reading the
                # body can itself time out, which must not escape.
                try:
                    detail = e.read().decode("utf-8", "replace").strip()
                except Exception:
                    detail = ""
                sys.stderr.write(f"bootstrap_delivery: could not mark {finding_id} shown: {e.code} {detail}\n")
            except Exception as e:
                sys.stderr.write(
                    f"bootstrap_delivery: the findings queue at {endpoint} did not answer ({e}); "
                    "not marking the rest\n"
                )
                return True
    return True


def _cleanup(data_dir: Path, in_sandbox: bool, shown: bool = False) -> None:
    """Tidy up after the report has been posted as blocks or emitted to stdout.

    Onboarding is already marked complete by the delivery claim, so everything
    here is best-effort: a cleanup hiccup must never turn a delivered report
    into a reported failure.

    The report is renamed, not deleted — see ``DELIVERED_REPORT_NAME``. Moving
    it out of the way still matters: the sweep and prioritization SOPs, which
    run where the report is, treat a present ``INVENTORY.md`` as "already done",
    so leaving it in place would make a later, deliberate re-run of onboarding a
    no-op. ``shown`` sets the shown file aside beside it, so a later report
    whose worker never ran ``select`` is not marked with this one's items.
    """
    _archive(data_dir, in_sandbox)
    if shown:
        _archive(data_dir, in_sandbox, SHOWN_NAME, DELIVERED_SHOWN_NAME)


def _retire_jobs() -> None:
    """Remove both onboarding cron jobs, in-process.

    Only a run with nothing to deliver may call this. Removing a job while it
    runs drops the run's fire claim, and the scheduler then discards that run's
    output instead of posting it. So the run that delivers the report leaves
    both jobs in place, and a later run, which finds ``.bootstrap_completed``
    and has nothing to post, removes them and loses nothing. The delivery job
    goes last because removing it ends this run.
    """
    try:
        from cron.jobs import remove_job  # type: ignore import-not-found
    except Exception:
        return
    for job_id in (SCAN_JOB_ID, DELIVERY_JOB_ID):
        try:
            remove_job(job_id)
        except Exception as e:
            sys.stderr.write(f"bootstrap_delivery: could not remove {job_id}: {e}\n")


def _origin() -> dict:
    """The origin the plugin bound this job's delivery to: ``platform``,
    ``chat_id`` and ``thread_id``; empty if unknown.

    The plugin writes the origin before ``.user_aligned``, so it is set by the
    time a delivery can fire.
    """
    try:
        from cron.jobs import get_job  # type: ignore import-not-found

        job = get_job(DELIVERY_JOB_ID) or {}
        return job.get(ORIGIN_KEY) or {}
    except Exception as e:
        sys.stderr.write(f"bootstrap_delivery: could not read the delivery origin: {e}\n")
        return {}


def _origin_platform() -> str | None:
    """The platform the plugin bound this job's delivery to, or None if unknown."""
    return _origin().get(PLATFORM_KEY)


def _presented(content: str) -> str:
    """The report as delivered: reshaped when ``KAGE_SLACK_UX`` is on and the
    job is bound to Slack, verbatim otherwise.

    Both helpers ship beside this script in ``/opt/defaults/scripts``. Any
    failure to load or reshape delivers the report verbatim, since this runs
    after the claim and a lost report is not retried.
    """
    try:
        import slack_presenter

        if not slack_presenter.enabled() or _origin_platform() != PRESENTED_PLATFORM:
            return content
        import inventory_presenter

        return inventory_presenter.present(content)
    except Exception as e:
        sys.stderr.write(f"bootstrap_delivery: delivering verbatim: {e}\n")
        return content


def _posted_as_blocks(content: str) -> bool:
    """Whether the report was posted to Slack as Block Kit; False posts nothing.

    False, and the caller prints the text, unless the flag is on, the origin is
    Slack with a chat id, the relay is configured and the report parses. Any
    failure is False too, including one that may have posted (a timeout once
    the request was sent): this report is sent once per install, and a second
    copy of it is a smaller loss than none.
    """
    try:
        import slack_presenter

        if not slack_presenter.enabled():
            return False
        origin = _origin()
        channel = str(origin.get(CHAT_ID_KEY) or "")
        if origin.get(PLATFORM_KEY) != PRESENTED_PLATFORM or not channel:
            return False
        import inventory_presenter
        import slack_blocks_post

        if not slack_blocks_post.configured():
            return False
        built = inventory_presenter.blocks(content)
        if built is None:
            return False
        blocks, text = built
        try:
            slack_blocks_post.post(channel, text, blocks, str(origin.get(THREAD_ID_KEY) or ""))
        except slack_blocks_post.Refused as e:
            sys.stderr.write(f"bootstrap_delivery: Slack refused the report blocks ({e})\n")
            return False
        return True
    except Exception as e:
        sys.stderr.write(f"bootstrap_delivery: posting the report as text: {e}\n")
    return False


def main(data_dir: Path | None = None) -> int:
    if data_dir is None:
        data_dir = _data_dir()

    completed = _completed_at(data_dir)
    if completed is not None:
        if time.time() - completed >= RETIRE_AFTER_SECONDS:
            _retire_jobs()
        return 0

    if not _awaiting_delivery(data_dir):
        return 0  # silent run — nobody to deliver to yet

    try:
        in_sandbox = sandbox_exec.sandbox_enabled()
    except sandbox_exec.SandboxMisconfigured as e:
        sys.stderr.write(f"bootstrap_delivery: cannot tell where INVENTORY.md is: {e}\n")
        return 1

    # Read before claiming, so a read failure leaves no claim behind to undo
    # and the next tick retries cleanly.
    try:
        raw = _read_report(data_dir, in_sandbox)
    except (sandbox_exec.SandboxUnavailable, subprocess.TimeoutExpired) as e:
        # Silent, and retried next tick: a non-zero exit is posted to the
        # user's chat as a failure alert on every tick. A lasting fault (a
        # rejected key, a changed host key, no ``ssh_host``) lands here too; the
        # agent's terminal shares that key and host, so it fails there too.
        sys.stderr.write(f"bootstrap_delivery: the shell sandbox did not answer: {e}\n")
        return 0
    except (OSError, sandbox_exec.SandboxMisconfigured, sandbox_exec.SandboxReadFailed) as e:
        sys.stderr.write(f"bootstrap_delivery: could not read INVENTORY.md: {e}\n")
        return 1
    if raw is None:
        return 0  # silent run — the report is not written yet
    if len(raw) > REPORT_MAX_BYTES:
        sys.stderr.write(
            f"bootstrap_delivery: INVENTORY.md is larger than {REPORT_MAX_BYTES} bytes; not delivering it\n"
        )
        return 1
    content = raw.decode("utf-8", errors="replace")

    # Before the claim, which ends the nudge's hold, so the rows exist when
    # the marks below are sent.
    suppressed = _register_findings(data_dir, in_sandbox)

    # The cheap check above is advisory; this is the decision. Nothing may be
    # written to stdout before it succeeds.
    if not _claim_delivery(data_dir):
        return 0  # another run is delivering this report — stay silent

    # Right after the claim, which ends the nudge's hold, so a nudge run has
    # the shortest window in which to announce these findings as new.
    shown = _mark_shown(data_dir, in_sandbox, suppressed)

    if not _posted_as_blocks(content):
        sys.stdout.write(_presented(content))
        sys.stdout.flush()

    # Cleanup runs only after the report is posted or safely on stdout (already
    # captured by the scheduler), so removing INVENTORY.md here cannot truncate delivery.
    _cleanup(data_dir, in_sandbox, shown)
    return 0


if __name__ == "__main__":
    sys.exit(main())
