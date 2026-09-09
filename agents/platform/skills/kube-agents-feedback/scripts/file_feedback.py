#!/usr/bin/env python3
"""File a kube-agents feedback submission on a user's behalf, exactly once.

The submission path is the public feedback form behind the docs-site short
link; a submission becomes a public GitHub issue on gke-labs/kube-agents,
opened by the form's pipeline (see scripts/feedback_form/README.md in the
repository). The form needs no credential, which is why this works even when
the install's GitHub credential path is degraded.

This script owns the whole wire path — resolving the short link, reading the
form's field ids, the single POST, and finding the resulting issue — so the
agent never composes an HTTP request to the form by hand. A live test of the
hand-rolled alternative double-filed the same report (gke-labs/kube-agents
issues #1345/#1346): the first POST succeeded, the parser missed the
confirmation, and the retry filed a duplicate. Everything here that looks
defensive is that incident, encoded:

- The form URL is discovered from the short link at request time, never
  hardcoded, so a recreated form (same link, new form id) needs no release.
- Fields are mapped by their question label, not their entry id, because ids
  change when the form is recreated and labels are the contract with the
  form's own builder script.
- A state marker is written *before* the POST and finalized after it, so a
  rerun after any failure checks the tracker for the issue instead of posting
  again — and before a first POST, the tracker itself is probed for a recent
  issue with this exact title, so re-dispatching the same confirmed text in a
  fresh workspace does not file it twice either. `submit` never POSTs twice
  for one payload, and says so.

Input is a JSON payload file:

    {"title": "...", "kind": "Bug", "happened": "...",
     "expected": "...", "environment": "...", "user_confirmed": true}

`title`, `kind`, `happened` and `user_confirmed` are required; `user_confirmed`
is the caller's assertion that the user saw this exact text and said yes, and
nothing is sent without it. Values are stripped of surrounding whitespace; the
title is capped at the form pipeline's own 120-character issue-title limit so
the filed issue can be found by exact title. Output is one JSON line on stdout:

    {"status": "SUBMITTED", "issue_url": "...", ...}
    {"status": "ALREADY_SUBMITTED", "issue_url": "...", ...}   (never null)
    {"status": "UNCONFIRMED", "issue_url": null, ...}  (rerun only re-checks)
    {"status": "BLOCKED", "reason": "..."}   (exit 1; nothing was sent)

Run:
    file_feedback.py submit --payload-file payload.json [--state-dir DIR]
"""

from __future__ import annotations

import argparse
import calendar
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# The one link agents hand out; the site serves a meta-refresh page here that
# names the current form. tests/test_feedback_reference.py ties it to the
# redirect in docs/site/astro.config.mjs.
SHORT_LINK = "https://gke-labs.github.io/kube-agents/feedback"

# Where the pipeline files the issue, and the label the form's script applies.
# test_file_feedback.py ties both to scripts/feedback_form/Code.gs.
TRACKER_REPO = "gke-labs/kube-agents"
TRACKER_API = f"https://api.github.com/repos/{TRACKER_REPO}/issues"
FEEDBACK_LABEL = "external-feedback"

# The form questions this script fills, keyed by the label the form's builder
# (scripts/feedback_form/Code.gs) gives each one. Labels are the stable
# contract: entry ids change whenever the form is recreated.
QUESTION_LABELS = {
    "title": "One-line summary",
    "kind": "What kind of feedback is this?",
    "happened": "What happened?",
    "expected": "What did you expect instead?",
    "environment": "Version and environment",
}
REQUIRED_FIELDS = ("title", "kind", "happened")
KIND_CHOICES = ("Bug", "Feature request", "Question", "Other")

# The pipeline truncates the issue title at this many characters (Code.gs
# TITLE_MAX_CHARS). A longer summary would file fine but the filed issue's
# title would no longer equal the payload's, so find_issue could never return
# its URL; refuse it up front instead, where the user can shorten it.
TITLE_MAX_CHARS = 120

# A stable substring of the form's confirmation message
# (CONFIRMATION_MESSAGE in scripts/feedback_form/Code.gs); its presence in the
# POST response is what distinguishes "submitted" from "the form served an
# error page with status 200". test_file_feedback.py checks it against Code.gs.
CONFIRMATION_SUBSTRING = "filed as a public issue"

# Content that must never reach a public issue, whoever typed it. These block;
# the softer redaction judgment (cluster names, project ids) is the caller's,
# per the persona bullet, because no regex recognizes a cluster name.
SECRET_PATTERNS = (
    ("private key material", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("a GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}")),
    ("a Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}")),
    ("a bearer token", re.compile(r"\bBearer [A-Za-z0-9._~+/-]{20,}")),
    ("a signed JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ")),
)

# The viewform page embeds every question as JSON in this variable.
FB_DATA_PATTERN = re.compile(r"FB_PUBLIC_LOAD_DATA_\s*=\s*(\[.*?\])\s*;", re.DOTALL)
FORM_URL_PATTERN = re.compile(
    r"https://docs\.google\.com/forms/d/e/([A-Za-z0-9_-]+)/viewform"
)

HTTP_TIMEOUT_SECONDS = 30
USER_AGENT = "kube-agents-feedback-skill"
# The pipeline says the issue appears "within a minute"; poll a little longer.
TRACKER_POLL_SECONDS = 90
TRACKER_POLL_INTERVAL_SECONDS = 10
TRACKER_PAGE_SIZE = 20
# Before a first POST, one tracker read looks for this payload's title among
# recent feedback issues: a match inside this window means some earlier card
# already filed this text, and filing it again is the #1345/#1346 duplicate.
DUPLICATE_LOOKBACK_SECONDS = 24 * 60 * 60
MARKER_PREFIX = ".feedback-submission-"
STATE_HASH_LENGTH = 16


def _fetch(url: str, data: bytes | None = None) -> str:
    request = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
        return response.read().decode("utf-8", errors="replace")


def resolve_form_url(short_link_html: str) -> str:
    """The current form's viewform URL, from the short link's redirect page."""
    match = FORM_URL_PATTERN.search(short_link_html)
    if not match:
        raise SystemExit(
            _blocked(f"{SHORT_LINK} no longer names a Google form; the form may "
                     "have been retired (scripts/feedback_form/README.md)")
        )
    return match.group(0)


def entry_ids_by_field(viewform_html: str) -> dict[str, str]:
    """Map payload field -> form entry id, matching questions by label."""
    match = FB_DATA_PATTERN.search(viewform_html)
    if not match:
        raise SystemExit(_blocked("the form page carried no FB_PUBLIC_LOAD_DATA_"))
    data = json.loads(match.group(1))
    label_to_entry: dict[str, str] = {}
    for question in data[1][1]:
        try:
            label, entry = question[1], question[4][0][0]
        except (TypeError, IndexError):
            continue
        label_to_entry[label] = str(entry)
    mapped, missing = {}, []
    for field, label in QUESTION_LABELS.items():
        if label in label_to_entry:
            mapped[field] = f"entry.{label_to_entry[label]}"
        else:
            missing.append(label)
    if missing:
        raise SystemExit(
            _blocked("the form no longer asks: " + ", ".join(missing) + " — its "
                     "questions and this script's QUESTION_LABELS have diverged")
        )
    return mapped


def normalize(payload: dict) -> dict:
    """The payload with every form field stripped, exactly as it is POSTed.

    The stripped title is also what find_issue matches, so stripping in one
    place keeps the submission and the lookup identical.
    """
    normalized = dict(payload)
    for field in QUESTION_LABELS:
        normalized[field] = str(payload.get(field, "")).strip()
    return normalized


def validate(payload: dict) -> None:
    if payload.get("user_confirmed") is not True:
        raise SystemExit(
            _blocked("payload lacks user_confirmed: true — nothing is filed "
                     "until the user has seen this exact text and said yes")
        )
    for field in REQUIRED_FIELDS:
        if not payload[field]:
            raise SystemExit(_blocked(f"required field '{field}' is empty"))
    if payload["kind"] not in KIND_CHOICES:
        raise SystemExit(
            _blocked(f"kind {payload['kind']!r} is not one of {', '.join(KIND_CHOICES)}")
        )
    if len(payload["title"]) > TITLE_MAX_CHARS:
        raise SystemExit(
            _blocked(f"the one-line summary is {len(payload['title'])} characters; "
                     f"the pipeline truncates issue titles at {TITLE_MAX_CHARS}, "
                     "which would leave the filed issue unfindable by title — "
                     "shorten it")
        )
    text = "\n".join(payload[field] for field in QUESTION_LABELS)
    for name, pattern in SECRET_PATTERNS:
        if pattern.search(text):
            raise SystemExit(
                _blocked(f"the text contains {name}; that must never reach a "
                         "public issue, so nothing was sent")
            )


def _blocked(reason: str) -> int:
    print(json.dumps({"status": "BLOCKED", "reason": reason}))
    return 1


def _unconfirmed(detail: str) -> int:
    print(json.dumps({"status": "UNCONFIRMED", "issue_url": None, "detail": detail}))
    return 0


def _payload_hash(payload: dict) -> str:
    canonical = json.dumps(
        {field: payload[field] for field in QUESTION_LABELS}, sort_keys=True
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:STATE_HASH_LENGTH]


def find_issue(title: str, since_epoch: float, deadline_seconds: int) -> str | None:
    """The tracker URL of the filed issue, matched by exact title, or None.

    Unauthenticated read of a public repo: this works even when the install
    has no usable GitHub credential, and never needs one. A deadline of 0
    means one read, no waiting.
    """
    query = urllib.parse.urlencode(
        {"labels": FEEDBACK_LABEL, "state": "all", "per_page": TRACKER_PAGE_SIZE}
    )
    deadline = time.time() + deadline_seconds
    while True:
        try:
            issues = json.loads(_fetch(f"{TRACKER_API}?{query}"))
            for issue in issues:
                created = calendar.timegm(
                    time.strptime(issue["created_at"], "%Y-%m-%dT%H:%M:%SZ")
                )
                if issue["title"] == title and created >= since_epoch:
                    return issue["html_url"]
        except (urllib.error.URLError, ValueError, KeyError):
            pass  # the tracker being briefly unreachable is not a submit failure
        if time.time() >= deadline:
            return None
        time.sleep(TRACKER_POLL_INTERVAL_SECONDS)


def _read_marker(marker: Path) -> dict:
    """The marker's state, or a bare attempted-state if it was corrupted.

    A marker that exists but cannot be parsed still means an attempt started;
    treating it as anything less would re-open the door to a second POST.
    """
    try:
        return json.loads(marker.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {"started_at": 0.0}


def submit(payload_path: Path, state_dir: Path) -> int:
    payload = normalize(json.loads(payload_path.read_text(encoding="utf-8")))
    validate(payload)
    title = payload["title"]

    marker = state_dir / f"{MARKER_PREFIX}{_payload_hash(payload)}.json"
    if marker.exists():
        state = _read_marker(marker)
        # Attempted or submitted, the answer is the same: never POST again.
        # If the first attempt died before its outcome was known, the tracker
        # is the arbiter of whether it landed.
        issue_url = state.get("issue_url") or find_issue(
            title, float(state.get("started_at", 0.0)) - 1, TRACKER_POLL_SECONDS
        )
        if issue_url:
            if not state.get("issue_url"):
                state["issue_url"] = issue_url
                marker.write_text(json.dumps(state), encoding="utf-8")
            print(json.dumps({
                "status": "ALREADY_SUBMITTED",
                "issue_url": issue_url,
                "detail": "a submission for this exact text already went out; "
                          "not filing it again",
            }))
            return 0
        return _unconfirmed(
            "an earlier attempt for this exact text exists but no issue has "
            "appeared on the tracker; nothing was re-sent — if this persists, "
            "hand it to a human rather than filing again"
        )

    # No marker in this workspace — but the same confirmed text may already
    # have been filed from another card. One tracker read settles it before
    # anything is sent.
    prior_url = find_issue(title, time.time() - DUPLICATE_LOOKBACK_SECONDS, 0)
    if prior_url:
        marker.write_text(
            json.dumps({"started_at": time.time(), "issue_url": prior_url}),
            encoding="utf-8",
        )
        print(json.dumps({
            "status": "ALREADY_SUBMITTED",
            "issue_url": prior_url,
            "detail": "an issue with this exact title was filed recently; "
                      "not filing it again",
        }))
        return 0

    try:
        form_url = resolve_form_url(_fetch(SHORT_LINK))
        entries = entry_ids_by_field(_fetch(form_url))
    except (urllib.error.URLError, OSError) as exc:
        raise SystemExit(
            _blocked(f"could not reach the feedback form ({exc}); nothing was sent")
        )
    form_data = {
        entries[field]: payload[field]
        for field in QUESTION_LABELS
        if payload[field]
    }
    response_url = form_url.replace("/viewform", "/formResponse")

    started_at = time.time()
    state = {"started_at": started_at, "title": title}
    marker.write_text(json.dumps(state), encoding="utf-8")

    try:
        body = _fetch(response_url, urllib.parse.urlencode(form_data).encode("utf-8"))
    except (urllib.error.URLError, OSError) as exc:
        # The POST's fate is unknown and the marker is in place: report that
        # honestly instead of a traceback, and let a rerun consult the tracker.
        return _unconfirmed(
            f"the POST to the form did not complete cleanly ({exc}); rerunning "
            "is safe and will only check the tracker for the issue"
        )
    confirmed = CONFIRMATION_SUBSTRING in body

    issue_url = find_issue(title, started_at - 1, TRACKER_POLL_SECONDS)
    state.update({"confirmed": confirmed, "issue_url": issue_url})
    marker.write_text(json.dumps(state), encoding="utf-8")

    if not confirmed and not issue_url:
        # One POST went out and neither signal came back. Do not retry: the
        # marker above makes a rerun of this payload check the tracker instead.
        return _unconfirmed(
            "the form did not confirm and no issue has appeared yet; rerunning "
            "is safe and will only re-check the tracker"
        )

    print(json.dumps({"status": "SUBMITTED", "issue_url": issue_url}))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    submit_parser = subparsers.add_parser(
        "submit", help="validate the payload and file it, exactly once"
    )
    submit_parser.add_argument("--payload-file", type=Path, required=True)
    submit_parser.add_argument(
        "--state-dir", type=Path, default=Path.cwd(),
        help="where the submission marker lives; use the kanban card workspace "
             "so a retried card sees its own earlier attempt",
    )
    args = parser.parse_args()
    return submit(args.payload_file, args.state_dir)


if __name__ == "__main__":
    sys.exit(main())
