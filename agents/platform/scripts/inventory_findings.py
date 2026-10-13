#!/usr/bin/env python3
"""Deterministic bridge from `INVENTORY.raw.md` to the findings queue.

The onboarding hand-off (bootstrap_handoff.py) writes a ```findings block into
the raw file; this script
reads it and owns every deterministic step of the prioritization stage:
`extract` produces the authoritative item list, `register` refuses to send
anything until every one of those items carries a score, and `select` chooses
the items the first report lists.

The stage used to ask the worker to enumerate the findings from prose and call
`register_findings` itself, and it lost findings three ways at once. It decided
for itself what counted as a finding, so two runs over identical input produced
different sets; a batch rejected for one missing field was reported one field
at a time and abandoned; and a single accepted call read as done. Measured over
two instrumented runs on the same nine-finding file, one registered seven and
the other three. Enumeration is not a judgement, so it is not the model's to
make -- the model scores what this script extracted.
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.absolute()))

import findings_queue as fq

DEFAULT_RAW_PATH = "/opt/data/INVENTORY.raw.md"
DEFAULT_ITEMS_PATH = "/opt/data/INVENTORY.items.json"
DEFAULT_SCORES_PATH = "/opt/data/INVENTORY.scores.json"
# The pacing limits as the gateway's environment sets them, which this terminal
# cannot read: bootstrap_handoff.py writes them beside the raw file.
DEFAULT_LIMITS_PATH = "/opt/data/INVENTORY.limits.json"
# What `select` chose, for bootstrap_delivery.py to mark shown once the report
# is delivered: {"items": [{"class": <item class>, "ids": [<queue id>, ...]}]}.
DEFAULT_SHOWN_PATH = "/opt/data/INVENTORY.shown.json"
SHOWN_ITEMS = "items"
SHOWN_CLASS = "class"
SHOWN_IDS = "ids"
TMP_SUFFIX = ".tmp"
DEFAULT_ENDPOINT = "http://127.0.0.1:8699"
POST_TIMEOUT_SECONDS = 30

SOURCE = "inventory"

# ```findings ... ``` -- CommonMark's leading indent and long closing fence are
# both accepted: bootstrap_handoff.py writes a bare fence, but a raw file typed
# by a model, as earlier sweeps did, may indent it inside a list.
BLOCK_RE = re.compile(
    r"^ {0,3}(?P<fence>`{3,})[ \t]*findings[ \t]*$(?P<body>.*?)^ {0,3}(?P=fence)`*[ \t]*$",
    re.MULTILINE | re.DOTALL,
)

ITEM_REQUIRED = ("check", "project", "cluster", "object", "title")
ITEM_OPTIONAL = ("namespace", "detail", "severity_hint", "provider_managed")
ITEM_STRINGS = ITEM_REQUIRED + ("namespace", "detail", "severity_hint")

SCORE_REQUIRED = ("rubric", "recommendation", "remediation", "verification")
SCORE_OPTIONAL = ("actionable", "provider_managed", "root_cause")

# Distinct so the SOP can tell the worker what to do about each without parsing
# the message, and clear of 2, which argparse returns for a usage error.
EXIT_NO_BLOCK = 10
EXIT_BAD_BLOCK = 11
EXIT_INCOMPLETE = 12
EXIT_POST_FAILED = 13


class Failure(Exception):
    """A run that produced no output, carrying every reason at once."""

    def __init__(self, code: int, errors: list[str], hint: str = ""):
        super().__init__(f"{len(errors)} error(s)")
        self.code = code
        self.errors = errors
        self.hint = hint


# --------------------------------------------------------------------------
# extract
# --------------------------------------------------------------------------


def parse_block(text: str) -> list[dict]:
    """Every item in the raw file's findings block, in file order, with ids.

    Raises `Failure` listing every malformed line rather than the first, and
    writes nothing when any line is bad: a partial extract is the silent drop
    this script exists to make impossible.
    """
    blocks = list(BLOCK_RE.finditer(text))
    if not blocks:
        raise Failure(
            EXIT_NO_BLOCK,
            ["no ```findings block in the raw file"],
            "The sweep that wrote this file predates the block, or omitted it.",
        )

    errors: list[str] = []
    items: list[dict] = []
    for block in blocks:
        # Line numbers are the raw file's, so an error names something the
        # reader can go and look at.
        first_line = text.count("\n", 0, block.start("body")) + 1
        for offset, line in enumerate(block.group("body").splitlines()):
            lineno = first_line + offset
            stripped = line.strip()
            if not stripped or stripped.startswith("//") or stripped.startswith("#"):
                continue
            try:
                raw = json.loads(stripped)
            except json.JSONDecodeError as exc:
                errors.append(f"line {lineno}: not valid JSON ({exc.msg})")
                continue
            if not isinstance(raw, dict):
                errors.append(f"line {lineno}: expected a JSON object, got {type(raw).__name__}")
                continue
            item = _clean_item(raw, lineno, errors)
            if item is not None:
                items.append(item)

    if errors:
        raise Failure(EXIT_BAD_BLOCK, errors, "Fix the raw file's findings block, then re-run.")

    # An empty block is a clean fleet, which is a normal result. An absent one
    # is a sweep that did not write the block at all, which is not.
    for index, item in enumerate(items, 1):
        item["id"] = f"f{index:03d}"
    return items


def _clean_item(raw: dict, lineno: int, errors: list[str]) -> dict | None:
    unknown = sorted(set(raw) - set(ITEM_REQUIRED) - set(ITEM_OPTIONAL))
    if unknown:
        errors.append(
            f"line {lineno}: unknown field(s) {', '.join(unknown)}; "
            f"allowed: {', '.join(ITEM_REQUIRED + ITEM_OPTIONAL)}"
        )
        return None

    # Stringifying instead would put a list of clusters into the identity key as
    # one unlookupable cluster, and make the string "false" a suppression.
    bad = False
    mistyped = [key for key in ITEM_STRINGS if raw.get(key) is not None and not isinstance(raw[key], str)]
    if mistyped:
        errors.append(f"line {lineno}: {', '.join(mistyped)} must be a string")
        bad = True
    if not isinstance(raw.get("provider_managed", False), bool):
        errors.append(f"line {lineno}: provider_managed must be true or false, not a string")
        bad = True
    if bad:
        return None

    missing = [key for key in ITEM_REQUIRED if not (raw.get(key) or "").strip()]
    if missing:
        errors.append(f"line {lineno}: missing {', '.join(missing)}")
        return None

    item = {key: raw[key].strip() for key in ITEM_REQUIRED}
    for key in ("namespace", "detail", "severity_hint"):
        value = (raw.get(key) or "").strip()
        if value:
            item[key] = value
    if raw.get("provider_managed"):
        item["provider_managed"] = True
    item["line"] = lineno
    return item


def describe_items(items: list[dict]) -> str:
    lines = []
    for item in items:
        where = "/".join(x for x in (item["project"], item["cluster"], item.get("namespace"), item["object"]) if x)
        hint = f" [{item['severity_hint']}]" if item.get("severity_hint") else ""
        lines.append(f"  {item['id']}  {item['check']}  {where}{hint}\n        {item['title']}")
    return "\n".join(lines)


def cmd_extract(args: argparse.Namespace) -> int:
    raw_path = Path(args.raw)
    try:
        text = raw_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise Failure(EXIT_NO_BLOCK, [f"cannot read {raw_path}: {exc}"]) from None

    items = parse_block(text)
    payload = {"raw": str(raw_path), "total": len(items), "items": items}
    out = Path(args.out)
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    print(f"extracted {len(items)} findings from {raw_path}")
    if items:
        print(describe_items(items))
    print(f"\nitems written to {out}")
    if items:
        print(f"Score all {len(items)} ids. `register` rejects the batch if any is missing.")
    else:
        print("The block is empty: the sweep found nothing. There is nothing to register.")
    return 0


# --------------------------------------------------------------------------
# register
# --------------------------------------------------------------------------


def build_payloads(items: list[dict], scores: dict) -> list[dict]:
    """One validated registration payload per extracted item, or nothing.

    Every error across every item is collected before anything is sent, so the
    caller gets one list to fix rather than one field per round trip.
    """
    errors: list[str] = []
    by_id = {item["id"]: item for item in items}

    for unknown in sorted(set(scores) - set(by_id)):
        errors.append(f"{unknown}: scored but not an extracted id")
    # `is None` rather than key membership: the payload loop below skips a null
    # score, so keying on presence alone would drop the finding with no error.
    missing = [fid for fid in by_id if scores.get(fid) is None]
    if missing:
        errors.append(
            f"unscored: {', '.join(missing)} -- every extracted finding needs a score, "
            "including the ones nobody will be asked to fix"
        )

    payloads = []
    for fid, item in by_id.items():
        score = scores.get(fid)
        if score is None:
            continue
        if not isinstance(score, dict):
            errors.append(f"{fid}: score must be an object")
            continue
        unknown = sorted(set(score) - set(SCORE_REQUIRED) - set(SCORE_OPTIONAL))
        if unknown:
            errors.append(
                f"{fid}: unknown field(s) {', '.join(unknown)}; "
                f"allowed: {', '.join(SCORE_REQUIRED + SCORE_OPTIONAL)}"
            )
        absent = [key for key in SCORE_REQUIRED if score.get(key) is None]
        if absent:
            errors.append(f"{fid}: missing {', '.join(absent)}")
        # The queue coerces these with `bool()`, where the string "false" is
        # True and an explicit null is False. Both are silent: a wrongly
        # provider-managed row drops out of the report and can never carry a
        # pull request, and an unactionable one sorts behind everything.
        bad_flags = [
            key for key in ("actionable", "provider_managed")
            if key in score and not isinstance(score[key], bool)
        ]
        for key in bad_flags:
            errors.append(f"{fid}: {key} must be true or false, not a string")
        if unknown or absent or bad_flags:
            continue

        payload = {
            "source": SOURCE,
            "check": item["check"],
            "project": item["project"],
            "cluster": item["cluster"],
            "namespace": item.get("namespace", ""),
            "object": item["object"],
            "title": item["title"],
            "detail": item.get("detail", ""),
        }
        payload.update({key: score[key] for key in SCORE_REQUIRED})
        for key in SCORE_OPTIONAL:
            if key in score:
                payload[key] = score[key]
        if item.get("provider_managed"):
            payload["provider_managed"] = True

        # The same validation the queue runs, so a payload that reaches the
        # wire cannot come back 400 and cost the caller a round trip.
        try:
            fq.validate_finding(payload)
        except fq.FindingError as exc:
            errors.append(f"{fid}: {exc}")
            continue
        payloads.append(payload)

    if errors:
        raise Failure(EXIT_INCOMPLETE, errors, "Nothing was registered. Fix all of these, then re-run.")
    return payloads


def _request(endpoint: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    token = (os.environ.get("SESSION_KV_API_KEY") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        f"{endpoint}{path}", data=data, headers=headers, method="POST" if data else "GET"
    )
    with urllib.request.urlopen(req, timeout=POST_TIMEOUT_SECONDS) as resp:
        return json.loads(resp.read().decode("utf-8"))


def post_batch(endpoint: str, findings: list[dict], scope: dict | None) -> dict:
    body = {"findings": findings}
    if scope:
        body["scope"] = scope
    return _request(endpoint, "/v1/findings", body)


def _read_json(path: str, what: str) -> dict:
    """The scores file is hand-written each run, so a trailing comma is likely."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise Failure(EXIT_INCOMPLETE, [f"cannot read the {what} file: {exc}"]) from None
    except json.JSONDecodeError as exc:
        raise Failure(
            EXIT_INCOMPLETE,
            [f"{path} is not valid JSON: {exc.msg} at line {exc.lineno} column {exc.colno}"],
        ) from None


def _read_items(items_path: str) -> list:
    """`extract`'s items, checked for their shape."""
    items = _read_json(items_path, "items").get("items")
    if not isinstance(items, list):
        raise Failure(
            EXIT_INCOMPLETE,
            [f"{items_path} has no `items` list -- run `extract` first, or point --items at its output"],
        )
    return items


def _read_scores(scores_path: str) -> dict:
    """The scores file, checked for its shape."""
    raw_scores = _read_json(scores_path, "scores")
    if not isinstance(raw_scores, dict) or not isinstance(raw_scores.get("scores"), dict):
        raise Failure(
            EXIT_INCOMPLETE,
            ["the scores file must be an object with a `scores` map keyed by finding id"],
        )
    return raw_scores


def _read_batch(items_path: str, scores_path: str) -> tuple[list, dict]:
    """`extract`'s items and the scores file, each checked for its shape."""
    return _read_items(items_path), _read_scores(scores_path)


def complete_clusters(raw_scores: dict) -> set[str]:
    """The scores file's `complete_clusters`, as '<project>/<cluster>' strings."""
    return {str(name) for name in raw_scores.get("complete_clusters") or []}


def cluster_batches(payloads: list[dict], complete: set[str]) -> list[tuple[str, list[dict], dict | None]]:
    """The payloads as one `(where, batch, scope)` per cluster, in sorted order.

    `where` is '<project>/<cluster>'; `scope` marks that cluster's sweep
    complete when `complete` names it, and is None otherwise.
    """
    by_cluster: dict[tuple[str, str], list[dict]] = {}
    for payload in payloads:
        by_cluster.setdefault((payload["project"], payload["cluster"]), []).append(payload)
    batches = []
    for (project, cluster), batch in sorted(by_cluster.items()):
        where = f"{project}/{cluster}"
        scope = {"project": project, "cluster": cluster, "complete": True} if where in complete else None
        batches.append((where, batch, scope))
    return batches


def cmd_register(args: argparse.Namespace) -> int:
    items, raw_scores = _read_batch(args.items, args.scores)
    complete = complete_clusters(raw_scores)
    payloads = build_payloads(items, raw_scores["scores"])
    if not payloads:
        print("nothing to register: the sweep extracted no findings")
        return 0

    batches = cluster_batches(payloads, complete)
    sent = 0
    failures: list[str] = []
    for where, batch, scope in batches:
        if args.dry_run:
            print(f"{where}: {len(batch)} finding(s), scope={'complete' if scope else 'omitted'} (dry run)")
            sent += len(batch)
            continue
        try:
            result = post_batch(args.endpoint, batch, scope)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            detail = exc.read().decode("utf-8", "replace") if isinstance(exc, urllib.error.HTTPError) else str(exc)
            failures.append(f"{where}: {detail}")
            continue
        outcomes = result.get("results") or []
        sent += len(outcomes)
        tally: dict[str, int] = {}
        for entry in outcomes:
            tally[entry.get("outcome", "?")] = tally.get(entry.get("outcome", "?"), 0) + 1
        summary = ", ".join(f"{count} {name}" for name, count in sorted(tally.items()))
        print(f"{where}: {summary}, scope={'complete' if scope else 'omitted'}")
        for entry in outcomes:
            if entry.get("outcome") == "suppressed":
                print(f"  suppressed (do not report or count): {entry.get('id')}")

    # An unmatched entry is a silent no-op with a real cost: the absence rule
    # never runs, so a fixed critical keeps its floor severity and the nudge
    # nags about it every morning with no exit.
    for entry in sorted(complete - {where for where, _, _ in batches}):
        print(
            f"warning: complete_clusters entry {entry!r} matched no registered batch; "
            "entries are '<project>/<cluster>' and the absence rule did not run for it"
        )

    print(f"registered {sent} of {len(items)} extracted findings")
    if failures:
        raise Failure(
            EXIT_POST_FAILED,
            failures,
            f"The other {sent} did register and are in the queue. The delivery job registers "
            "this whole batch again from the agent pod when it delivers the report. Go on to "
            "`select`, and name the clusters above in the card summary as not yet in the queue.",
        )
    return 0


# --------------------------------------------------------------------------
# select
# --------------------------------------------------------------------------


def read_limits(path: str) -> fq.PacingLimits:
    """The limits the hand-off wrote, or the defaults when the file is missing or unusable."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        sys.stderr.write(f"select: no {path}; using the default limits\n")
        return fq.PacingLimits()
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"select: cannot read {path} ({exc}); using the default limits\n")
        return fq.PacingLimits()
    if not isinstance(raw, dict):
        sys.stderr.write(f"select: {path} is not a JSON object; using the default limits\n")
        return fq.PacingLimits()
    # Through the parser the environment goes through, so a bad value here
    # falls back to its default the same way.
    return fq.pacing_limits({fq.PACING_ENV[name]: str(value) for name, value in raw.items() if name in fq.PACING_ENV})


def select_items(
    items: list[dict], scores: dict, limit: int, exclude: frozenset[str] = frozenset()
) -> tuple[list[list[dict]], int, int]:
    """The first report's items, the critical items it leaves out, and every other item.

    Scored here from the vectors, as the queue would score them, so the
    choice does not depend on the queue being reachable and sees only this
    batch. Rows are gathered into items by `fq.item_key` and ordered by their
    best row in the queue's order; an item is critical when any of its rows
    is. The report lists the top `limit` critical items and nothing else.
    Provider-managed observations (`fq.rolled_up`) are never items and count
    once per line (`fq.item_key`) among the others; a line that also has an
    ordinary row is already counted as that item. The nudge's open count
    still counts such a line twice, once as the item and once as managed.
    Ids in `exclude` are rows the user dismissed, which are neither listed nor
    counted.
    """
    payloads = build_payloads(items, scores)
    by_ref = {item["id"]: item for item in items}
    rows: dict[str, dict] = {}
    # `build_payloads` returns one payload per extracted id, in this order, or raises.
    for item, payload in zip(by_ref.values(), payloads):
        row = fq.validate_finding(payload)
        row["ref"] = item["id"]
        # Two lines naming one object are one row in the queue, the later
        # line's, as registering them would leave it.
        rows[row["id"]] = row

    managed: set[tuple] = set()
    lines: dict[tuple, list[dict]] = {}
    for row in sorted(rows.values(), key=fq.ranked_sort_key):
        if row["id"] in exclude:
            continue
        if fq.rolled_up(row):
            managed.add(fq.item_key(row))
            continue
        lines.setdefault(fq.item_key(row), []).append(row)
    critical = [members for members in lines.values() if any(r["severity"] == fq.SEVERITIES[0] for r in members)]
    shown = critical[:limit]
    return shown, len(critical) - len(shown), len(lines) - len(critical) + len(managed - lines.keys())


def _plural(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


def describe_selection(
    shown: list[list[dict]], deferred: int, others: int, limits: fq.PacingLimits
) -> str:
    if shown:
        out = [f"first report: list exactly {_plural(len(shown), 'critical item', 'critical items')}, in this order"]
    elif deferred:
        out = [f"first report: list no item; the limit is {limits.first_report_criticals}"]
    else:
        out = ["first report: list no item; there are no critical findings"]
    for index, members in enumerate(shown, 1):
        best = members[0]
        objects = _plural(len(members), "object", "objects")
        out.append(f"{index:>3}. critical  {best['check_slug']}  {best['project']}/{best['cluster']}  ({objects})")
        for row in members:
            where = "/".join(x for x in (row["namespace"], row["object"]) if x)
            out.append(f"       {row['ref']}  {row['severity']:<8} {where}  {row['title']}")

    remaining = deferred + others
    if remaining:
        critical_part = f", {deferred} of them critical" if deferred else ", none of them critical"
        out.append(f"\nroll-up: {_plural(remaining, 'more item', 'more items')}{critical_part}")
    else:
        out.append("\nroll-up: none; the report has no roll-up line")
    # The listed items count against the delivery day's critical allowance
    # (bootstrap_delivery.py marks them), so when they fill it the rest start
    # the next day. A limit of 0 adds none of that kind at all.
    if deferred:
        if not limits.daily_criticals:
            out.append("pace: the critical items not listed are not added in chat; the daily limit is 0")
        else:
            start = " the day after the report arrives" if len(shown) >= limits.daily_criticals else ""
            out.append(
                f"pace: the critical items not listed are added in chat from {fq.REMIND_HOUR}:00 UTC{start}, "
                f"at most {limits.daily_criticals} a day"
            )
    elif not shown and others:
        if not limits.noncritical_max:
            out.append("pace: non-critical items are not added in chat; the daily limit is 0")
        else:
            out.append(
                f"pace: non-critical items are added in chat from {limits.noncritical_after_hour}:00 UTC, "
                f"at most {limits.noncritical_max} a day"
            )
    return "\n".join(out)


def cmd_select(args: argparse.Namespace) -> int:
    items = _read_items(args.items)
    # A clean fleet has nothing to score, so the SOP writes no scores file for it.
    scores = _read_scores(args.scores)["scores"] if items else {}
    limits = read_limits(args.limits)
    try:
        shown, deferred, others = select_items(
            items, scores, limits.first_report_criticals, frozenset(args.exclude or ())
        )
    except Failure as failure:
        failure.hint = "Nothing was selected. Fix all of these, then re-run."
        raise

    record = {
        SHOWN_ITEMS: [
            {SHOWN_CLASS: fq.ITEM_CLASSES[0], SHOWN_IDS: [row["id"] for row in members]} for members in shown
        ]
    }
    out = Path(args.out)
    tmp = out.with_name(out.name + TMP_SUFFIX)
    tmp.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    tmp.replace(out)

    print(describe_selection(shown, deferred, others, limits))
    print(f"\nthe listed items are written to {out}")
    return 0


# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    extract = sub.add_parser("extract", help="parse the raw file's findings block")
    extract.add_argument("--raw", default=DEFAULT_RAW_PATH)
    extract.add_argument("--out", default=DEFAULT_ITEMS_PATH)
    extract.set_defaults(func=cmd_extract)

    register = sub.add_parser("register", help="score-check and register every extracted finding")
    register.add_argument("--items", default=DEFAULT_ITEMS_PATH)
    register.add_argument("--scores", required=True)
    register.add_argument("--endpoint", default=os.environ.get("FINDINGS_ENDPOINT", DEFAULT_ENDPOINT))
    register.add_argument("--dry-run", action="store_true")
    register.set_defaults(func=cmd_register)

    select = sub.add_parser("select", help="choose the items the first report lists")
    select.add_argument("--items", default=DEFAULT_ITEMS_PATH)
    select.add_argument("--scores", default=DEFAULT_SCORES_PATH)
    select.add_argument("--limits", default=DEFAULT_LIMITS_PATH)
    select.add_argument("--out", default=DEFAULT_SHOWN_PATH)
    select.add_argument("--exclude", action="append", metavar="ID", help="a queue id `register` named as suppressed")
    select.set_defaults(func=cmd_select)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except Failure as failure:
        print(f"{args.command} failed:", file=sys.stderr)
        for error in failure.errors:
            print(f"  - {error}", file=sys.stderr)
        if failure.hint:
            print(failure.hint, file=sys.stderr)
        return failure.code


if __name__ == "__main__":
    sys.exit(main())
