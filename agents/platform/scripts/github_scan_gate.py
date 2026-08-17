#!/usr/bin/env python3
"""Dispatcher for the ``github-repo-watcher`` cron job.

Polling GitHub is deterministic. Deciding what to do about what the poll found
is not. This script is the first half, and it exists so the second half stops
running when there is nothing to decide.

``github-issue-resolver`` used to be a *prompt* job on this roster at ``*/30``:
every half hour the scheduler started a real agent turn, which loaded the
persona and the skill's ``SKILL.md``, ran ``resolver.py poll``, read
``NO_ISSUES``, and answered ``[SILENT]``. Forty-eight turns a day, and on a
quiet repository every one of them paid for the context before the first API
call. The work the model did was to notice there was no work.

So the poll runs here instead — a ``no_agent`` script, a plain subprocess with
no model attached — and the model is woken only for a repository that actually
has something waiting, by filing a kanban card assigned to ``platform``. An
idle tick costs one ``gh`` call and writes nothing at all.

Why a card rather than the cron job the watchdogs use
-----------------------------------------------------
``../cron/README.md`` argues, correctly, that a card is not a cron run: the
indirection strips ``skills``, ``model`` and ``deliver`` from the thing that
ends up running, which is why the seven governance audits were moved back onto
this roster. That argument is about jobs that fire unconditionally and whose
entire product *is* the cron delivery. A poller is the inverse. It has nothing
to deliver on almost every tick; its product goes to GitHub, not to chat; and a
card appears only in the rare case where there is genuine work. What it gives
up it can afford — the card body names the skill, and ``model``/``max_turns``
take their defaults — and the one thing it must not give up, an audible
failure, stays here: this job keeps ``deliver: "all"``, and anything printed to
stdout below is a fault report that reaches the room.

One watcher, several sweeps
---------------------------
Two cron jobs polling one repository through one credential is one cron job.
Sweeps are registered in ``SWEEPS`` and each runs inside its own ``try``, so a
sweep that raises cannot take its siblings down with it — the isolation that
separate jobs would have given for free, and the reason the ``except`` below is
deliberately broad.

Each sweep owns its own repo resolution and ``gh`` preflight rather than
inheriting one from here. That is not an oversight: ``resolver.py poll``
already does both and already reports precise reason codes
(``GH_CLI_NOT_FOUND`` vs ``GITHUB_AUTH_NOT_CONFIGURED`` vs ``REPO_UNREACHABLE``)
that a hoisted preflight here could only flatten or duplicate.

Consolidating did take something away: an operator could previously stop one
poller by disabling its roster entry. ``GITHUB_WATCHER_SWEEPS`` gives that back.
"""

import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# Which sweeps run, and in what order, is `SWEEP_ORDER` — derived from the
# `SWEEPS` registry near the bottom of this file rather than written out twice.
# Two hand-maintained lists is a sweep that is registered but never runs, and
# an operator who names it in the env var below being told it does not exist.

# Comma-separated sweep names. Unset means all of them.
SWEEPS_ENV = "GITHUB_WATCHER_SWEEPS"

# Every card this job files goes to the privileged specialist: the Chat Agent's
# toolsets are stripped to `mcp-router` + `kanban`, so it could not act on one.
ASSIGNEE = "platform"

RESOLVER_REL = "skills/github-issue-resolver/scripts/resolver.py"

# `resolver.py poll` sweeps stale issues before it queries, so it is not a
# read-only call and its runtime is not bounded by a single request.
RESOLVER_TIMEOUT_S = 300


@dataclass
class Card:
    """A kanban card a sweep wants filed."""

    title: str
    body: str
    idempotency_key: str


@dataclass
class SweepResult:
    """What a sweep found.

    ``warnings`` reach the chat room; an empty result is silence. A sweep with
    nothing to report returns the default of both, which is how a quiet tick
    stays quiet.
    """

    cards: list[Card] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", "/opt/data"))


def _slug(text: str) -> str:
    """Reduce a repo slug to something safe inside an idempotency key."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-").lower()


def selected_sweeps() -> tuple[tuple[str, ...], list[str]]:
    """Resolve ``GITHUB_WATCHER_SWEEPS`` into an ordered sweep list.

    Returns the sweeps to run and any warnings about the value itself. A
    misspelled name is reported rather than ignored: ``GITHUB_WATCHER_SWEEPS=issue``
    would otherwise read as "disable everything", and a watcher that silently
    stopped watching is the failure this whole job exists to avoid.
    """
    raw = os.environ.get(SWEEPS_ENV, "").strip()
    if not raw:
        return SWEEP_ORDER, []

    names = [n.strip() for n in raw.split(",") if n.strip()]
    unknown = [n for n in names if n not in SWEEP_ORDER]
    warnings = []
    if unknown:
        warnings.append(
            f"⚠️ **GitHub repo watcher:** `{SWEEPS_ENV}` names unknown "
            f"{'sweeps' if len(unknown) > 1 else 'sweep'} "
            f"{', '.join('`' + n + '`' for n in unknown)}. Known sweeps: "
            f"{', '.join('`' + n + '`' for n in SWEEP_ORDER)}."
        )
    selected = tuple(n for n in SWEEP_ORDER if n in names)
    if not selected:
        warnings.append(
            f"⚠️ **GitHub repo watcher is doing nothing:** `{SWEEPS_ENV}` "
            f"selected no known sweep."
        )
    return selected, warnings


# --------------------------------------------------------------------------
# Sweep: unaddressed issues
# --------------------------------------------------------------------------


def _resolver_path() -> Path:
    return hermes_home() / RESOLVER_REL


def run_resolver_poll() -> dict:
    """Run ``resolver.py poll`` and return its JSON.

    Launched with ``sys.executable`` rather than the shebang: the scheduler
    hands this script the gateway's own venv interpreter, and the resolver must
    run under the same one that owns its dependencies, whatever the file mode
    on the PVC happens to be.

    Raises on anything that leaves us without a status to branch on, so the
    caller's ``except`` turns it into one visible warning rather than a sweep
    that quietly decides there are no issues.
    """
    script = _resolver_path()
    if not script.is_file():
        raise FileNotFoundError(f"resolver not found at {script}")

    proc = subprocess.run(
        [sys.executable, str(script), "poll"],
        capture_output=True,
        text=True,
        timeout=RESOLVER_TIMEOUT_S,
    )
    # Deliberately not `check=True`: the resolver reports its faults as JSON on
    # stdout, and a non-zero exit with parseable output is still an answer. Only
    # unparseable output is a real dead end.
    stdout = proc.stdout.strip()
    if not stdout:
        raise RuntimeError(
            f"resolver poll exited {proc.returncode} with no output"
            + (f": {proc.stderr.strip()[:300]}" if proc.stderr.strip() else "")
        )
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"resolver poll did not return JSON: {stdout[:300]}") from e
    if not isinstance(payload, dict):
        raise RuntimeError(f"resolver poll returned {type(payload).__name__}, not an object")
    return payload


#: Idempotency-key granularity. Long enough that a worker still working an
#: issue is never handed a second card — `claim` happens in the skill's Step 2,
#: within a minute or two of dispatch — and short enough that a wedged issue
#: costs an hour of blindness rather than forever.
CARD_BUCKET_FORMAT = "%Y%m%dT%H"


def _issue_card(payload: dict, now: datetime | None = None) -> Card:
    """The card that hands one issue to the Platform Agent.

    The body carries the issue number and nothing else the worker could act on
    directly. It re-runs ``poll`` itself in Step 1 — re-reading GitHub is
    cheaper than trusting a card that may have been sitting on the board while
    somebody closed the issue.

    ``now`` is injected so the bucketing below is testable.
    """
    number = payload["issue_number"]
    repo = payload.get("repository", "")
    title = payload.get("title", "") or f"issue #{number}"
    bucket = (now or datetime.now(timezone.utc)).strftime(CARD_BUCKET_FORMAT)
    return Card(
        title=f"Triage and resolve {repo}#{number}: {title}"[:200],
        body=(
            f"An unaddressed open issue is waiting on `{repo}`.\n\n"
            "Run the **github-issue-resolver** skill and follow its procedure from "
            f"Step 1 for issue **#{number}** — poll, claim, investigate, then call "
            "`resolver.py transition` with your report. Its safety red lines, scope "
            "constraints, and turn completion checklist apply in full.\n\n"
            "Re-run the poll rather than trusting this card: it was filed by the "
            "`github-repo-watcher` cron job and the issue may have moved since."
        ),
        # Scoped to the repository as well as the number, because a deployment
        # can be repointed at a different repo and #12 is not #12 everywhere.
        #
        # And scoped to an hour, because the board's dedupe cannot be the
        # durable guarantee. It matches non-archived rows regardless of their
        # state, so a *finished* card answers its key forever, and nothing here
        # archives cards. A worker that ends its turn before the skill's Step 2
        # therefore latches the key permanently — and the skill has an ordinary
        # path that does exactly that: Step 1 says to alert the room and
        # terminate on an `ERROR` status. The issue keeps no `status:` label, so
        # `handle_poll` keeps returning it; because it returns only the
        # lowest-numbered unaddressed issue, every higher-numbered one goes
        # unseen too, and `file_card` cannot tell a create from a dedupe hit, so
        # every subsequent tick looks like a clean run. The old `*/30` prompt
        # job had no cross-tick state to wedge; the key is what introduced it.
        #
        # The durable claim is the `status:in-progress` label on GitHub, which
        # survives a reset volume and is what drops the issue out of the poll.
        # The key only has to cover the window between filing and `claim`, so an
        # hour of it is enough. Buckets are wall-clock aligned, so a card filed
        # at 10:59 is retried at 11:00 rather than an hour later; that costs one
        # redundant card, and the body above already tells the worker to re-poll
        # rather than trust the card, so the second worker finds the issue
        # claimed or gone and stops.
        idempotency_key=f"issue-resolve-{_slug(repo)}-{number}-{bucket}",
    )


def sweep_issues(dry_run: bool = False) -> SweepResult:
    if dry_run:
        # `resolver.py poll` performs its own stale-label sweep as a side
        # effect, and it has no dry-run of its own, so this one cannot promise
        # a read-only pass over the issues. Said out loud rather than quietly
        # relabelling issues under a flag whose name promises it will not.
        sys.stderr.write(
            "github_scan_gate: --dry-run note — the issues sweep runs "
            "`resolver.py poll`, whose stale-label sweep still writes to GitHub\n"
        )
    payload = run_resolver_poll()
    status = payload.get("status")

    if status in ("NO_ISSUES", "NOT_CONFIGURED"):
        # NOT_CONFIGURED is a supported deployment, not a fault: a install with
        # no target repository has nothing to watch and should stay silent.
        return SweepResult()

    if status == "FOUND":
        return SweepResult(cards=[_issue_card(payload)])

    if status == "ERROR":
        reason = payload.get("reason", "unknown")
        value = payload.get("value")
        detail = f"{reason}" + (f" ({value})" if value else "")
        return SweepResult(
            warnings=[f"⚠️ **GitHub issue resolver is not running:** {detail}"]
        )

    return SweepResult(
        warnings=[
            f"⚠️ **GitHub repo watcher:** resolver poll returned an unrecognised "
            f"status `{status}`."
        ]
    )


SWEEPS = {
    "issues": sweep_issues,
}

# Insertion order is run order, so this is the registry read one way rather
# than a second list to keep in step with it. Adding a sweep is one line above.
SWEEP_ORDER: tuple[str, ...] = tuple(SWEEPS)


# --------------------------------------------------------------------------
# Card filing
# --------------------------------------------------------------------------


def _parse_task_id(out: str) -> str | None:
    """Pull the card id out of a ``create`` response.

    ``--json`` is asked for, but the board's own stderr can share the buffer,
    so locate the JSON object rather than assuming the whole string is one.
    Falls back to the human line (``Created <id>  (...)``) in case the board is
    older than ``--json`` on this subcommand.
    """
    start = out.find("{")
    end = out.rfind("}")
    if start != -1 and end > start:
        try:
            task_id = json.loads(out[start : end + 1]).get("id")
            if task_id:
                return str(task_id)
        except Exception:  # noqa: BLE001 - fall through to the text form
            pass
    match = re.search(r"Created\s+(\S+)", out)
    return match.group(1) if match else None


def file_card(card: Card) -> str | None:
    """File one card, returning its id.

    Failures go to stderr, not stdout. A board that is briefly unavailable is
    not something to page the room about — the next tick re-files, because the
    issue is still unclaimed and the poll will find it again. Only a *sweep*
    failing is loud, because that is what makes the watcher blind.
    """
    try:
        from hermes_cli.kanban import run_slash
    except Exception as e:  # noqa: BLE001 - kanban unavailable; retry next tick
        sys.stderr.write(f"github_scan_gate: kanban API unavailable: {e}\n")
        return None

    cmd = (
        f"create --json --assignee {shlex.quote(ASSIGNEE)} "
        f"--idempotency-key {shlex.quote(card.idempotency_key)} "
        f"--body {shlex.quote(card.body)} "
        f"{shlex.quote(card.title)}"
    )
    try:
        out = str(run_slash(cmd)).strip()
    except Exception as e:  # noqa: BLE001 - never fail the cron run
        sys.stderr.write(f"github_scan_gate: could not file card: {e}\n")
        return None

    task_id = _parse_task_id(out)
    if not task_id:
        sys.stderr.write(
            f"github_scan_gate: could not read a task id from the board response: {out}\n"
        )
        return None
    sys.stderr.write(f"github_scan_gate: filed card {task_id} ({card.idempotency_key})\n")
    return task_id


# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    dry_run = "--dry-run" in (argv if argv is not None else sys.argv[1:])

    sweeps, warnings = selected_sweeps()

    for name in sweeps:
        try:
            result = SWEEPS[name](dry_run)
        except Exception as e:  # noqa: BLE001 - one blind sweep must not blind the rest
            warnings.append(
                f"⚠️ **GitHub repo watcher — `{name}` sweep failed:** "
                f"{type(e).__name__}: {e}"
            )
            continue

        warnings.extend(result.warnings)
        for card in result.cards:
            if dry_run:
                sys.stderr.write(
                    f"github_scan_gate: would file {card.idempotency_key}: {card.title}\n"
                )
            else:
                file_card(card)

    # Stdout is the delivery channel. Empty means a clean, quiet tick and the
    # scheduler posts nothing; anything here is a fault the room needs to see.
    if warnings:
        print("\n".join(warnings))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
