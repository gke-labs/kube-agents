#!/usr/bin/env python3
"""Deterministic bridge from `INVENTORY.raw.md` to the findings queue.

The onboarding sweep writes a ```findings block into the raw file; this script
reads it and owns every deterministic step of the prioritization stage:
`extract` produces the authoritative item list, `register` refuses to send
anything until every one of those items carries a score, and `ranked` reads
back the queue's order and the total the report's roll-up line counts from.

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
DEFAULT_ENDPOINT = "http://127.0.0.1:8699"
POST_TIMEOUT_SECONDS = 30

SOURCE = "inventory"

# ```findings ... ``` -- CommonMark's leading indent and long closing fence are
# both accepted because the only example the sweep is shown, in `inventory.md`
# Step 4 item 5, sits indented inside a numbered list.
BLOCK_RE = re.compile(
    r"^ {0,3}(?P<fence>`{3,})[ \t]*findings[ \t]*$(?P<body>.*?)^ {0,3}(?P=fence)`*[ \t]*$",
    re.MULTILINE | re.DOTALL,
)

ITEM_REQUIRED = ("check", "cluster", "object", "title")
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
        where = "/".join(x for x in (item["cluster"], item.get("namespace"), item["object"]) if x)
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
    missing = [fid for fid in by_id if fid not in scores]
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


def fetch_ranked(endpoint: str) -> list[dict]:
    return _request(endpoint, "/v1/findings/ranked").get("findings") or []


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


def cmd_register(args: argparse.Namespace) -> int:
    items = _read_json(args.items, "items").get("items")
    if not isinstance(items, list):
        raise Failure(
            EXIT_INCOMPLETE,
            [f"{args.items} has no `items` list -- run `extract` first, or point --items at its output"],
        )
    raw_scores = _read_json(args.scores, "scores")
    if not isinstance(raw_scores, dict) or not isinstance(raw_scores.get("scores"), dict):
        raise Failure(
            EXIT_INCOMPLETE,
            ["the scores file must be an object with a `scores` map keyed by finding id"],
        )
    complete = {str(name) for name in raw_scores.get("complete_clusters") or []}
    payloads = build_payloads(items, raw_scores["scores"])
    if not payloads:
        print("nothing to register: the sweep extracted no findings")
        return 0

    by_cluster: dict[str, list[dict]] = {}
    for payload in payloads:
        by_cluster.setdefault(payload["cluster"], []).append(payload)

    sent = 0
    failures: list[str] = []
    for cluster, batch in sorted(by_cluster.items()):
        scope = {"cluster": cluster, "complete": True} if cluster in complete else None
        if args.dry_run:
            print(f"{cluster}: {len(batch)} finding(s), scope={'complete' if scope else 'omitted'} (dry run)")
            sent += len(batch)
            continue
        try:
            result = post_batch(args.endpoint, batch, scope)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            detail = exc.read().decode("utf-8", "replace") if isinstance(exc, urllib.error.HTTPError) else str(exc)
            failures.append(f"{cluster}: {detail}")
            continue
        outcomes = result.get("results") or []
        sent += len(outcomes)
        tally: dict[str, int] = {}
        for entry in outcomes:
            tally[entry.get("outcome", "?")] = tally.get(entry.get("outcome", "?"), 0) + 1
        summary = ", ".join(f"{count} {name}" for name, count in sorted(tally.items()))
        print(f"{cluster}: {summary}, scope={'complete' if scope else 'omitted'}")
        for entry in outcomes:
            if entry.get("outcome") == "suppressed":
                print(f"  suppressed (do not report or count): {entry.get('id')}")

    print(f"registered {sent} of {len(items)} extracted findings")
    if failures:
        raise Failure(
            EXIT_POST_FAILED,
            failures,
            f"The other {sent} did register and are in the queue. Write the report from the "
            "scores you computed, and name the clusters above in the card summary as missing "
            "from the queue.",
        )
    return 0


# --------------------------------------------------------------------------
# ranked
# --------------------------------------------------------------------------


def cmd_ranked(args: argparse.Namespace) -> int:
    try:
        ranked = fetch_ranked(args.endpoint)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise Failure(
            EXIT_POST_FAILED,
            [str(exc)],
            "Rank by the scores you computed instead, and say so in the card summary.",
        ) from None

    for index, finding in enumerate(ranked, 1):
        where = "/".join(
            x for x in (finding.get("cluster"), finding.get("namespace"), finding.get("object")) if x
        )
        flags = []
        if finding.get("provider_managed"):
            flags.append("provider_managed")
        if not finding.get("actionable", True):
            flags.append("not_actionable")
        suffix = f"  [{','.join(flags)}]" if flags else ""
        print(
            f"{index:>3}. {finding.get('rank_score'):>4} {finding.get('severity'):<8} "
            f"{finding.get('check')}  {where}{suffix}\n     {finding.get('title')}"
        )
    print(f"\ntotal: {len(ranked)}")
    print("The roll-up count is this total minus the rows you show or gather into a shown line.")
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

    ranked = sub.add_parser("ranked", help="the queue's order, and the authoritative total")
    ranked.add_argument("--endpoint", default=os.environ.get("FINDINGS_ENDPOINT", DEFAULT_ENDPOINT))
    ranked.set_defaults(func=cmd_ranked)

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
