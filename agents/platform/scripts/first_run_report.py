"""The first-run (onboarding) inventory report: its file names, its markers, and a read of it on demand.

One home for the names three scripts share. The ranking worker writes the ranked report
(``INVENTORY.md``) on the shell sandbox's data volume, beside the sweep's complete findings
(``INVENTORY.raw.md``); ``bootstrap_delivery.py`` (beside this module in the image: one ``COPY`` of
``agents/chat/scripts/`` and ``agents/platform/scripts/`` into the same directory) posts or files
it, renames it ``INVENTORY.delivered.md`` where it was read, and records on the agent pod's own
home which path took it: the tag in ``.bootstrap_completed``, the ledger issue in
``.bootstrap_report_filed``, the retire in ``.bootstrap_report_retired``. With the sandbox off the
report files sit in the same home as the markers.

``read_first_run_report`` reads the markers under ``home`` and the report through ``sandbox_read``
when one is given (the MCP server passes ``sandbox_exec.read_bytes`` when the sandbox is on), else
under ``home``, and says what became of the report: ``delivered`` (posted into the chat the first
person wrote in), ``filed`` (a ledger issue, with its record), ``retired`` (a week passed with
nobody arriving), ``pending`` (written, not yet delivered), or ``absent`` (no report file yet). No
environment is read here: the caller names the home, so this module can run as the
``platform_control`` MCP child, whose environment is the config's ``env:`` block alone.
"""

import json
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

# The ranked report the user receives, and the name delivery renames it to where it was read: kept
# rather than deleted, since it is the only copy of a sweep that can take many minutes over a fleet.
REPORT_NAME = "INVENTORY.md"
DELIVERED_REPORT_NAME = "INVENTORY.delivered.md"
# The sweep's complete findings (bootstrap_handoff.py writes it; inventory_findings.py reads it),
# never renamed: the ranked report is a selection from it.
RAW_REPORT_NAME = "INVENTORY.raw.md"
# The sandbox's data volume, whatever HERMES_HOME says on the agent pod: the same path by
# construction (deploy/sandbox/Dockerfile), and a separate constant because it names a directory
# on the far side of the connection.
SANDBOX_HOME = "/opt/data"

# Markers on the agent pod, under the chat profile's home. The delivery claim holds the path that
# took it (one of the CLAIM_PATH_* tags, written with a trailing newline); an empty file is a
# claim from a build before the tag.
COMPLETED_FILE = ".bootstrap_completed"
CLAIM_PATH_ORIGIN = "origin"
CLAIM_PATH_CONVERSATION = "conversation"
CLAIM_PATH_NO_HUMAN = "no-human"
# A claim that is there but could not be read: nothing settles or releases on it.
CLAIM_PATH_UNREADABLE = "unreadable"
# The conversation path claims first and posts after; this file, written with the post's
# outcome, is what says the post landed. The origin path posts in the claiming run.
CONVERSATION_DONE_FILE = ".bootstrap_conversation_done"
# The no-human path's two records: the ledger issue it filed (a JSON object with the keys below;
# FILED_AT is a Unix time), or the retire (one Unix-time line) when nothing could take the report
# in a week.
REPORT_FILED_FILE = ".bootstrap_report_filed"
FILED_REPO = "repo"
FILED_NUMBER = "number"
FILED_URL = "url"
FILED_AT = "filed_at"
REPORT_RETIRED_FILE = ".bootstrap_report_retired"
# Touched once the sweep's findings are in the findings queue, before the claim.
FINDINGS_REGISTERED_MARKER = ".bootstrap_findings_registered"

# What a reader may ask for: the ranked report, or the sweep's complete findings.
WHICH_RANKED = "ranked"
WHICH_FULL = "full"
REPORT_KINDS = (WHICH_RANKED, WHICH_FULL)

# What became of the ranked report, most settled first. ``filed`` and ``retired`` are the no-human
# path's own records and outrank the file names: that path renames the report too.
STATE_DELIVERED = "delivered"
STATE_FILED = "filed"
STATE_RETIRED = "retired"
STATE_PENDING = "pending"
STATE_ABSENT = "absent"

TEXT_ENCODING = "utf-8"
# The form a recorded time is given back in: UTC, to the second, the way the issue's own date is.
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
# A probe for a file's presence moves one byte.
PROBE_BYTES = 1

SandboxRead = Callable[[str, int], bytes | None]


def _read_local(home: Path, name: str, max_bytes: int) -> bytes | None:
    """Up to ``max_bytes`` bytes of ``name`` under ``home``, or None when there is no such file."""
    try:
        with open(home / name, "rb") as handle:
            return handle.read(max_bytes)
    except FileNotFoundError:
        return None


def _read_report_file(home: Path, name: str, max_bytes: int, sandbox_read: SandboxRead | None) -> bytes | None:
    """Up to ``max_bytes`` bytes of a report file where the sandbox keeps it, or under ``home``.

    Errors from ``sandbox_read`` and local errors other than a missing file propagate: the caller
    decides what a failed read is worth saying.
    """
    if sandbox_read is not None:
        return sandbox_read(f"{SANDBOX_HOME}/{name}", max_bytes)
    return _read_local(home, name, max_bytes)


def _claim_tag(home: Path) -> str | None:
    """The tag inside ``.bootstrap_completed``: a CLAIM_PATH_*, "" for an older build's claim, None
    when there is no claim, CLAIM_PATH_UNREADABLE when the file is there and cannot be read."""
    try:
        return (home / COMPLETED_FILE).read_text(encoding=TEXT_ENCODING, errors="replace").strip()
    except FileNotFoundError:
        return None
    except OSError:
        return CLAIM_PATH_UNREADABLE


def _present(home: Path, name: str) -> bool:
    try:
        return (home / name).exists()
    except OSError:
        return False


def _time_text(value: object) -> str | None:
    """A Unix time as TIME_FORMAT, or None for anything that is not one."""
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).strftime(TIME_FORMAT)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _filed_record(home: Path) -> dict | None:
    """The filed marker's record, with its time rendered, or None when it does not parse."""
    try:
        record = json.loads((home / REPORT_FILED_FILE).read_text(encoding=TEXT_ENCODING))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    return {
        FILED_REPO: record.get(FILED_REPO),
        FILED_NUMBER: record.get(FILED_NUMBER),
        FILED_URL: record.get(FILED_URL),
        FILED_AT: _time_text(record.get(FILED_AT)),
    }


def _retired_at(home: Path) -> str | None:
    """The retired marker's time, or None when the file is missing or does not hold one. Bytes
    that are not UTF-8 decode to replacement characters, which _time_text refuses: no raise."""
    try:
        return _time_text((home / REPORT_RETIRED_FILE).read_text(encoding=TEXT_ENCODING, errors="replace").strip())
    except OSError:
        return None


def read_first_run_report(
    home: str | Path,
    *,
    which: str = WHICH_RANKED,
    max_bytes: int,
    sandbox_read: SandboxRead | None = None,
) -> dict:
    """What became of the first-run report, and its text.

    ``home`` is the agent pod's chat-profile home, where the markers are. ``which`` is
    ``WHICH_RANKED`` (the delivered report when there is one, else the pending one) or
    ``WHICH_FULL`` (the sweep's complete findings). ``max_bytes`` caps the text; a longer file comes
    back cut to it with ``truncated`` true. ``sandbox_read(path, max_bytes)`` reads a report file
    off the shell sandbox when the report lives there; without it the files are read under
    ``home``. A read that fails for any reason but a missing file raises.

    ``STATE_DELIVERED`` is the delivered file where the report was read, an origin claim (that path
    posts in the claiming run), or a conversation claim whose post recorded its outcome
    (``CONVERSATION_DONE_FILE``); a conversation claim with no outcome yet is still pending.

    Returns ``state`` (a STATE_*), ``path_taken`` (the claim tag, or None), ``report`` (``kind``,
    ``source`` file name, ``text``, ``bytes``, ``truncated``; None when the asked-for file is not
    there), ``filed`` (the ledger record under ``STATE_FILED``), ``retired_at`` (under
    ``STATE_RETIRED``) and ``findings_registered``.
    """
    if which not in REPORT_KINDS:
        raise ValueError(f"which must be one of {', '.join(REPORT_KINDS)}, not {which!r}")
    home = Path(home)
    # One byte past the cap, to tell "at the cap" from "over it".
    want = max_bytes + 1

    # The markers on the agent pod first: when they settle the state, the full read probes no
    # report file beyond the one asked for, so a sandbox round-trip is never spent on a probe
    # whose answer would not be read.
    tag = _claim_tag(home)
    filed = None
    retired_at = None
    state = None
    if _present(home, REPORT_FILED_FILE):
        state = STATE_FILED
        filed = _filed_record(home)
    elif _present(home, REPORT_RETIRED_FILE):
        state = STATE_RETIRED
        retired_at = _retired_at(home)
    elif tag == CLAIM_PATH_ORIGIN or (tag == CLAIM_PATH_CONVERSATION and _present(home, CONVERSATION_DONE_FILE)):
        state = STATE_DELIVERED

    report = None
    delivered_file = False
    pending_file = False
    if which == WHICH_RANKED:
        source = DELIVERED_REPORT_NAME
        raw = _read_report_file(home, source, want, sandbox_read)
        if raw is None:
            source = REPORT_NAME
            raw = _read_report_file(home, source, want, sandbox_read)
        delivered_file = raw is not None and source == DELIVERED_REPORT_NAME
        pending_file = raw is not None and source == REPORT_NAME
    else:
        source = RAW_REPORT_NAME
        raw = _read_report_file(home, source, want, sandbox_read)
        if state is None:
            delivered_file = _read_report_file(home, DELIVERED_REPORT_NAME, PROBE_BYTES, sandbox_read) is not None
            pending_file = (not delivered_file) and _read_report_file(home, REPORT_NAME, PROBE_BYTES, sandbox_read) is not None
    if raw is not None:
        cut = raw[:max_bytes]
        report = {
            "kind": which,
            "source": source,
            "text": cut.decode(TEXT_ENCODING, errors="replace"),
            "bytes": len(cut),
            "truncated": len(raw) > max_bytes,
        }

    if state is None:
        state = STATE_DELIVERED if delivered_file else STATE_PENDING if pending_file else STATE_ABSENT

    return {
        "state": state,
        "path_taken": tag or None,
        "report": report,
        "filed": filed,
        "retired_at": retired_at,
        "findings_registered": _present(home, FINDINGS_REGISTERED_MARKER),
    }
