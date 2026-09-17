#!/usr/bin/env python3
"""Read the eval evidence store into ``store.json`` for the Trend page.

Usage::

    python3 scripts/eval_dashboard/store.py --location gs://bucket/evidence \\
        [--prior store.json] [--window-days 90] [--lead-days 14] [--out store.json]
    python3 scripts/eval_dashboard/store.py --location gs://bucket/evidence \\
        --prior store.json --fail-with "<why this tick read nothing>" [--out store.json]

The evidence store is where the nightly appends one record per case per
night (``docs/designs/eval-scorer.md``, "What is stored"): one immutable
object per record, filed under the case and the version key it was measured
at, named by its ``recorded_at`` stamp and the Prow build that wrote it::

    <location>/<case>/<setup-id>/<judge-model>/<sv>-f<n>-v<n>/<stamp>-<build>.jsonl

This reader is the dashboard's, not the gate's: ``bench/kube_agents_bench/
baselines.py`` reads the same layout to decide admission and must stop on a
malformed line; this one feeds a page, skips the line with a warning and
carries on. It imports nothing from ``bench`` on purpose -- the dashboard is
stdlib plus gsutil (``collect.py``), and the record format is small enough to
re-parse here. What it keeps identical is the one thing that must match: the
per-case-per-key object cap (``EVAL_BASELINE_MAX_OBJECTS``, default 200)
and the rule that a record's own ``key`` is the truth and the path only an
index.

ONE LISTING PER REFRESH, THEN ONLY THE NEW OBJECTS. Objects are immutable
and the store is append-only, so a record once read is final. ``--prior``
is the ``store.json`` the last refresh published; every object it already
holds is kept without a fetch, and only the names the listing shows that it
does not hold are read (``gsutil cat``, in chunks). A tick therefore costs
one ``ls`` of the prefix plus last night's objects, whatever the store's
age. Without a prior, everything inside the window is read once.

THE WINDOW, THE LEAD-IN AND THE CAP. The Trend page draws ``--window-days``
(90, a quarter) of nights, and the read reaches ``--lead-days`` (14)
further back: the trailing window the page draws beside a night pools the
nights before it, and the gate pools without any date window, so the
first drawn nights need the nights before them or the page would call a
window "not full" that admission read whole (a 20-run window is seven
nights at three repetitions; two weeks covers a missed night or two).
Objects whose name stamps them older than window plus lead-in are not
fetched. Inside that span the newest ``--max-objects`` per case *per key*
are read, the gate's own cap, and ``truncated`` says per case how many
older objects that left out -- a cap that is silent reads as "I
considered everything" when it did not. One object per case per night
keeps the span (about 104 objects) well under the 200; the cap binds only
if the nightly records about twice a night.

THE OUTPUT is what ``render.py --store`` reads (SCHEMA.md,
"store.json")::

    {schema_version: 1, source, read_at, window_days, lead_days, max_objects,
     listed, fetched, truncated{case: n}, older{case: {key: n}}, partial,
     warnings[], error, records[]}

``older`` is what the listing showed and the read left behind, per case
and version key: objects older than the span plus those the cap trimmed,
keyed by the directories the writer filed them under (the listing sees
paths, not records). The Trend page maps a record's own key to that path
the way the writer does and reads it to say, of a short trailing window,
whether the store holds older records at that key that admission pools
(``cut``) rather than calling it "collecting" on a guess.

``records[]`` is every record read, each the JSON object as written plus
``object`` (its URL) and ``build`` (the Prow build id from the name; the
record itself does not carry it). ``recorded_at`` and ``key`` come from the
record, never from the path.

A READ THAT OUTRUNS ITS TIME finishes over the next ticks rather than
never. Objects are fetched in waves (``CAT_WORKERS`` chunks at a time);
with ``--deadline-s`` the reader stops between waves once the next one
would not fit, writes what it has with ``partial`` set (``{fetched,
remaining}``), and the next tick's ``--prior`` already holds those
records, so a cold read of months converges in a few ticks even when a
single tick cannot hold it. The workflow's ``timeout`` stays as the hard
stop behind the deadline. A read that was killed instead persists
nothing, which is why the deadline exists.

FAILURE POSTURE: a page, not a gate. A listing that fails writes the prior
document back with ``error`` set and ``read_at`` unchanged, so the Trend
page shows the last good read and says the store could not be read this
tick; with no prior either, nothing is written and the exit is non-zero.
``--fail-with REASON`` does the same without touching the store: the
workflow calls it when this script was killed (its ``timeout``) or
crashed, or when the job has no wall clock left to read at all, so every
failure class reaches the page the same way, as a stale read that says
why. The workflow runs this step best-effort and renders without
``--store`` when it produced nothing, so the other pages publish
regardless.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime
import json
import os
import pathlib
import re
import subprocess
import sys
import time

SCHEMA_VERSION = 1
#: How many nights the Trend page draws. A quarter, per #1493's question.
DEFAULT_WINDOW_DAYS = 90
#: How much further back the read reaches so the first drawn night's
#: trailing window pools the nights before it, as admission did (docstring).
DEFAULT_LEAD_DAYS = 14
#: Newest objects read per case per key: the gate's cap, the same default
#: and the same environment variable (bench/kube_agents_bench/evidence_store.py).
DEFAULT_MAX_OBJECTS = 200
MAX_OBJECTS_ENV = "EVAL_BASELINE_MAX_OBJECTS"
#: Objects per ``gsutil cat``: well under any argv limit, few enough calls
#: for a cold read of a quarter (about 3,500 objects at 38 cases a night).
CAT_CHUNK = 100
#: Concurrent ``gsutil cat`` calls. gsutil fetches the objects of one call
#: serially at close to a second each (38 objects took 30 s on 2026-09-17),
#: so a cold read of a quarter is three quarters of an hour single-file and
#: a few minutes at this width; the incremental tick reads one chunk.
CAT_WORKERS = 8
#: Seconds before one gsutil call is treated as failed.
GSUTIL_TIMEOUT_S = 300
#: What gsutil says about a prefix that holds nothing yet (collect.py reads
#: the same phrase): an empty store, not an unreachable one.
NO_OBJECTS = "matched no objects"
#: An object name: ``<recorded_at with ':' as '-'>-<build>.jsonl``, as
#: evidence_store.GcsBackend.append writes it. The build is Prow's numeric
#: id; ``local`` (a laptop run) or anything else reads as no build.
NAME_RE = re.compile(r"^(?P<stamp>\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z)-(?P<build>[^/]+)\.jsonl$")
UTC = datetime.timezone.utc


def utc_now() -> str:
    return datetime.datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def max_objects_from_env(env: dict | None = None) -> int:
    """``EVAL_BASELINE_MAX_OBJECTS`` or the default; junk falls back, as the
    gate's reader does -- this bounds a read, it is not a correctness knob."""
    raw = (env if env is not None else os.environ).get(MAX_OBJECTS_ENV, "")
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_OBJECTS
    return value if value > 0 else DEFAULT_MAX_OBJECTS


# --------------------------------------------------------------------------
# gsutil


def gsutil_call(args: list[str], gsutil: str = "gsutil", runner=None) -> tuple[str | None, str]:
    """(stdout, stderr) of one gsutil call; stdout None when it failed.
    ``runner`` defaults to ``subprocess.run`` at call time, so a test that
    patches it sees every call."""
    run = runner or subprocess.run
    try:
        proc = run([gsutil, *args], capture_output=True, text=True, timeout=GSUTIL_TIMEOUT_S, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, str(exc)
    if proc.returncode != 0:
        return None, (proc.stderr or "").strip()
    return proc.stdout, (proc.stderr or "")


def list_objects(location: str, gsutil: str = "gsutil", runner=None) -> list[str]:
    """Every ``.jsonl`` object URL under ``location``, sorted. An empty prefix
    is ``[]``; a listing that failed raises ``RuntimeError`` with gsutil's
    words, so the caller can tell the two apart (module docstring)."""
    out, err = gsutil_call(["ls", f"{location.rstrip('/')}/**"], gsutil, runner)
    if out is None:
        if NO_OBJECTS in err.lower():
            return []
        raise RuntimeError(f"gsutil ls {location}: {err[:400] or 'failed'}")
    return sorted(
        line.strip() for line in out.splitlines()
        if line.strip().startswith("gs://") and line.strip().endswith(".jsonl")
    )


def cat_objects(urls: list[str], gsutil: str = "gsutil", runner=None, chunk: int = CAT_CHUNK, workers: int = CAT_WORKERS):
    """``(URLs, text or None, error)`` per ``gsutil cat`` call, in URL order;
    the calls run ``workers`` at a time. gsutil prints the objects in
    argument order, each ending in the newline the writer put there, so a
    chunk's text is its lines in order. A chunk whose call failed is read
    again one object at a time, so one bad object (a 5xx, a 403) costs that
    object and not its chunk-mates; each object that still fails is its own
    ``(url, None, error)`` entry, so the caller can name it."""
    parts = [urls[start:start + chunk] for start in range(0, len(urls), chunk)]
    if not parts:
        return []
    pool_size = max(1, min(workers, len(parts)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=pool_size) as pool:
        results = list(pool.map(lambda part: gsutil_call(["cat", *part], gsutil, runner), parts))
    out = []
    for part, (text, err) in zip(parts, results, strict=True):
        if text is not None or len(part) == 1:
            out.append((part, text, err))
            continue
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(workers, len(part)))) as pool:
            singles = list(pool.map(lambda url: gsutil_call(["cat", url], gsutil, runner), part))
        out.extend(([url], text, err) for url, (text, err) in zip(part, singles, strict=True))
    return out


# --------------------------------------------------------------------------
# names


def parse_url(url: str, location: str) -> dict | None:
    """``{case, key_dir, name, stamp, build}`` for an object URL under
    ``location``, or None for one that is not in the store's layout (a stray
    object directly under the prefix, a name without a stamp)."""
    root = location.rstrip("/") + "/"
    if not url.startswith(root):
        return None
    relative = url[len(root):]
    parts = relative.split("/")
    if len(parts) < 2:
        return None
    match = NAME_RE.match(parts[-1])
    if not match:
        return None
    build = match.group("build")
    return {
        "case": parts[0],
        "key_dir": "/".join(parts[:-1]),
        "name": parts[-1],
        "stamp": match.group("stamp"),
        "build": build if build.isdigit() else None,
    }


def stamp_ms(stamp: str) -> float | None:
    """Epoch milliseconds of a name's stamp (``2026-09-17T05-54-31Z``)."""
    try:
        parsed = datetime.datetime.strptime(stamp, "%Y-%m-%dT%H-%M-%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None
    return parsed.timestamp() * 1000


def select_objects(urls: list[str], location: str, *, now_ms: float, window_days: int, max_objects: int, lead_days: int = 0) -> tuple[list[str], dict[str, int], dict[str, dict[str, int]], list[str]]:
    """The URLs worth reading: inside the window plus the lead-in by their
    name's stamp, then the newest ``max_objects`` per case per key
    directory. Returns them sorted, with ``{case: objects the cap left
    out}`` and ``{case: {key: objects left behind at that key}}`` (older
    than the span, or trimmed by the cap; ``case`` and ``key`` as the
    directories the writer filed them under, so a component the writer
    sanitised appears sanitised, ``unkeyed`` is a record without a key,
    and ``""`` is an object filed directly under its case, the flat layout
    the gate's reader also groups by directory), and the URLs this reader
    cannot place: a name without a stamp (``bench-gate record
    --recorded-at`` with a non-``Z`` stamp writes one) or an object
    directly under the prefix. The gate's reader pools such an object
    without reading its name, so the caller names each one in a warning
    rather than dropping it in silence."""
    since_ms = now_ms - (window_days + lead_days) * 24 * 3600 * 1000
    by_dir: dict[str, list[tuple[str, str]]] = {}
    older: dict[str, dict[str, int]] = {}
    skipped: list[str] = []

    def left_behind(key_dir: str, count: int) -> None:
        case, _, key = key_dir.partition("/")  # key is "" for the flat layout
        older.setdefault(case, {})[key] = older.get(case, {}).get(key, 0) + count

    for url in urls:
        parsed = parse_url(url, location)
        at = stamp_ms(parsed["stamp"]) if parsed else None
        if parsed is None or at is None:
            skipped.append(url)
            continue
        if at < since_ms:
            left_behind(parsed["key_dir"], 1)
            continue
        by_dir.setdefault(parsed["key_dir"], []).append((parsed["name"], url))
    chosen: list[str] = []
    truncated: dict[str, int] = {}
    for key_dir, group in by_dir.items():
        group.sort()  # names start with the stamp: chronological
        if len(group) > max_objects:
            case = key_dir.split("/", 1)[0]
            truncated[case] = truncated.get(case, 0) + len(group) - max_objects
            left_behind(key_dir, len(group) - max_objects)
            group = group[-max_objects:]
        chosen.extend(url for _, url in group)
    return sorted(chosen), truncated, older, sorted(skipped)


# --------------------------------------------------------------------------
# records


def parse_records(text: str, urls: list[str], location: str, warnings: list[str]) -> list[dict]:
    """The records in one ``gsutil cat`` output, each with its ``object`` and
    ``build``. Every line of a single object is that object's; across a
    chunk, lines are matched to objects by order when the counts agree
    (one record per object, the writer's rule), otherwise by the case and
    stamp the name carries (``read_store`` re-reads such a chunk one
    object at a time, so this is a fallback for a caller that does not),
    and an unmatched record keeps ``object`` and ``build`` null rather
    than a guess. A line that is not a JSON object is a warning and is
    skipped; the record's own ``case``, ``recorded_at`` and ``key`` are
    what the page reads, so a record without them is skipped too."""
    lines = [line for line in text.splitlines() if line.strip()]
    parsed_urls = [parse_url(u, location) for u in urls]
    by_case_stamp = {
        (p["case"], p["stamp"]): (u, p) for u, p in zip(urls, parsed_urls) if p is not None
    }
    records = []
    for index, line in enumerate(lines):
        try:
            doc = json.loads(line)
        except ValueError as exc:
            warnings.append(f"{_where(urls, index, len(lines))}: not valid JSON: {exc}")
            continue
        if not isinstance(doc, dict):
            warnings.append(f"{_where(urls, index, len(lines))}: not a JSON object")
            continue
        case = doc.get("case")
        recorded_at = doc.get("recorded_at")
        key = doc.get("key")
        if not isinstance(case, str) or not isinstance(recorded_at, str) or not isinstance(key, dict):
            warnings.append(f"{_where(urls, index, len(lines))}: record without case, recorded_at or key")
            continue
        url, name = None, None
        if len(urls) == 1:
            url, name = urls[0], parsed_urls[0]
        elif len(lines) == len(urls):
            url, name = urls[index], parsed_urls[index]
        else:
            hit = by_case_stamp.get((case, recorded_at.replace(":", "-")))
            if hit:
                url, name = hit
        record = dict(doc)
        record["object"] = url
        record["build"] = name["build"] if name else None
        records.append(record)
    return records


def _where(urls: list[str], index: int, count: int) -> str:
    return urls[index] if count == len(urls) else f"{urls[0]} .. {urls[-1]} line {index + 1}"


# --------------------------------------------------------------------------
# the document


def load_prior(path: pathlib.Path | None) -> dict | None:
    if path is None or not path.exists():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or not isinstance(doc.get("records"), list):
        return None
    return doc


def now_utc() -> datetime.datetime:
    """The wall clock; a test seam (``read_store``'s ``now`` is the other)."""
    return datetime.datetime.now(UTC)


def read_store(location: str, *, prior: dict | None = None, window_days: int = DEFAULT_WINDOW_DAYS,
               lead_days: int = DEFAULT_LEAD_DAYS, max_objects: int | None = None,
               now: datetime.datetime | None = None, gsutil: str = "gsutil", runner=None,
               deadline_s: float | None = None, chunk: int = CAT_CHUNK, workers: int = CAT_WORKERS,
               clock=time.monotonic) -> dict:
    """The store.json document for ``location``: one listing, the prior's
    records kept where the listing still names them, the rest fetched in
    waves of ``workers`` chunks. With ``deadline_s`` the read stops between
    waves once the next wave (sized by the last one) would end past the
    deadline, and the document says so in ``partial`` (module docstring).
    Raises ``RuntimeError`` when the listing fails; the CLI decides what
    that means with or without a prior."""
    started = clock()
    now = now or now_utc()
    cap = max_objects if max_objects is not None else max_objects_from_env()
    urls = list_objects(location, gsutil, runner)
    wanted, truncated, older, skipped = select_objects(urls, location, now_ms=now.timestamp() * 1000, window_days=window_days, lead_days=lead_days, max_objects=cap)
    known: dict[str, list[dict]] = {}  # an object's records: one by the writer's rule, every line of it either way
    if prior and prior.get("source") == location.rstrip("/"):
        for record in prior.get("records") or []:
            if isinstance(record, dict) and isinstance(record.get("object"), str):
                known.setdefault(record["object"], []).append(record)
    kept = [record for url in wanted if url in known for record in known[url]]
    to_read = [url for url in wanted if url not in known]
    warnings: list[str] = [f"{url}: not in the store's layout (no stamp in the name); not read" for url in skipped]
    fetched: list[dict] = []
    parts = [to_read[start:start + chunk] for start in range(0, len(to_read), chunk)]
    waves = [parts[start:start + workers] for start in range(0, len(parts), max(1, workers))]
    done = 0
    wave_s = 0.0
    stopped = False
    for index, wave in enumerate(waves):
        if deadline_s is not None and index and clock() - started + wave_s > deadline_s:
            stopped = True
            break
        wave_started = clock()
        for part, text, err in cat_objects([url for piece in wave for url in piece], gsutil, runner, chunk=chunk, workers=workers):
            if text is None:
                warnings.append(f"{part[0]}: gsutil cat failed: {err[:200] or 'failed'}")
                continue
            if len(part) > 1 and sum(1 for line in text.splitlines() if line.strip()) != len(part):
                # An object with more than one line among them (not the
                # writer's rule, but possible): read them one at a time,
                # so every record carries its object and the incremental
                # read keeps all of them next tick.
                for url, single, single_err in cat_objects(part, gsutil, runner, chunk=1):
                    if single is None:
                        warnings.append(f"{url[0]}: gsutil cat failed: {single_err[:200] or 'failed'}")
                        continue
                    fetched.extend(parse_records(single, url, location, warnings))
                continue
            fetched.extend(parse_records(text, part, location, warnings))
        done += sum(len(part) for part in wave)
        wave_s = clock() - wave_started
    records = kept + fetched
    records.sort(key=lambda r: (str(r.get("recorded_at") or ""), str(r.get("case") or ""), str(r.get("object") or "")))
    return {
        "schema_version": SCHEMA_VERSION,
        "source": location.rstrip("/"),
        "read_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window_days": window_days,
        "lead_days": lead_days,
        "max_objects": cap,
        "listed": len(urls),
        "fetched": done,
        "truncated": truncated,
        "older": older,
        "partial": {"fetched": done, "remaining": len(to_read) - done} if stopped else None,
        "warnings": warnings,
        "error": None,
        "records": records,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--location", required=True, metavar="GS_PREFIX", help="the store, e.g. gs://kube-agents-evals-bench/evidence")
    parser.add_argument("--prior", type=pathlib.Path, default=None, help="the store.json the last refresh published; its records are kept without a fetch (missing or unreadable: read everything in the window)")
    parser.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS, metavar="N", help=f"the page draws objects stamped inside the last N days (default {DEFAULT_WINDOW_DAYS})")
    parser.add_argument("--lead-days", type=int, default=DEFAULT_LEAD_DAYS, metavar="N", help=f"read N days further back than the window so the first drawn nights' trailing windows are whole (default {DEFAULT_LEAD_DAYS})")
    parser.add_argument("--max-objects", type=int, default=None, metavar="N", help=f"newest objects per case per key (default {MAX_OBJECTS_ENV} or {DEFAULT_MAX_OBJECTS}, the gate's cap)")
    parser.add_argument("--out", type=pathlib.Path, default=pathlib.Path("store.json"))
    parser.add_argument("--gsutil", default="gsutil", help="gsutil binary to invoke")
    parser.add_argument("--deadline-s", type=float, default=None, metavar="S", help="stop fetching between waves once the next wave would end past S seconds from the start, and write what was read with `partial` set; the rest is read next tick (default: no deadline)")
    parser.add_argument("--fail-with", metavar="REASON", default=None, help="read nothing: write --prior back with `error` set to REASON (the workflow's path when this script was killed or had no time to run); exit 1 without a prior")
    args = parser.parse_args(argv)
    if args.window_days < 1:
        parser.error("--window-days must be at least 1")
    if args.lead_days < 0:
        parser.error("--lead-days must be at least 0")
    if args.max_objects is not None and args.max_objects < 1:
        parser.error("--max-objects must be at least 1")
    if args.deadline_s is not None and args.deadline_s < 0:
        parser.error("--deadline-s must be at least 0")

    prior = load_prior(args.prior)

    def keep_prior(why: str) -> int:
        if prior is None:
            print(f"error: {why}; no prior store.json to fall back on", file=sys.stderr)
            return 1
        stale = dict(prior)
        stale["error"] = f"{utc_now()}: {why}"
        args.out.write_text(json.dumps(stale, separators=(",", ":")) + "\n", encoding="utf-8")
        print(f"warning: {why}; wrote the prior read of {stale.get('read_at')} to {args.out}", file=sys.stderr)
        return 0

    if args.fail_with is not None:
        return keep_prior(args.fail_with.strip() or "the store was not read this tick")
    try:
        doc = read_store(args.location, prior=prior, window_days=args.window_days, lead_days=args.lead_days, max_objects=args.max_objects, gsutil=args.gsutil, deadline_s=args.deadline_s)
    except RuntimeError as exc:
        return keep_prior(str(exc))
    for warning in doc["warnings"]:
        print(f"warning: {warning}", file=sys.stderr)
    args.out.write_text(json.dumps(doc, separators=(",", ":")) + "\n", encoding="utf-8")
    partial = doc["partial"]
    print(
        f"wrote {args.out}: {len(doc['records'])} records ({doc['fetched']} objects fetched,"
        f" {doc['listed']} listed, {len(doc['warnings'])} warnings"
        + (f"; stopped at the deadline with {partial['remaining']} objects left for the next tick" if partial else "")
        + ")",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
