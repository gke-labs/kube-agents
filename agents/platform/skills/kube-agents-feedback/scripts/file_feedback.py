#!/usr/bin/env python3
"""File a confirmed kube-agents report through the credential broker, exactly once.

The shell sandbox reaches DNS and the credential broker and nothing else, so
the form's wire path lives on the broker (`feedback_broker.py`, route
`POST /v1/feedback/submit`). This script is the sandbox side: it asks once per
confirmed text, and remembers that it asked.

The memory is a marker file in the task workspace, written *before* the call
and finalised after it. A rerun that finds the marker with an issue URL
answers from it and asks nothing; one that finds it without a URL (the
broker's answer was lost mid-flight) asks the broker to *recheck only*, which
reads the tracker and never POSTs. A broker that could not be reached at all,
or that refused the payload, sent nothing, so the marker is removed again and
the next attempt is a first attempt. Everything here is the #1345/#1346
incident, encoded: one report filed twice because a retry did not know the
first attempt had landed.

Input is a JSON payload file:

    {"title": "...", "kind": "Bug", "happened": "...",
     "expected": "...", "environment": "...", "user_confirmed": true}

`title`, `kind`, `happened` and `user_confirmed` are required; `user_confirmed`
is the caller's assertion that the person saw this exact text and said yes.
The broker validates the rest (the kinds, the 120-character title, secret-shaped
text). Output is one JSON line on stdout, the broker's object as received:

    {"status": "SUBMITTED", "issue_url": "...", "fallback": {...}}
    {"status": "SUBMITTED", "issue_url": null, "detail": "...", "fallback": {...}}  (rerun rechecks)
    {"status": "ALREADY_SUBMITTED", "issue_url": "...", "fallback": {...}}
    {"status": "UNCONFIRMED", "issue_url": null, "detail": "...", "fallback": {...}}
    {"status": "BLOCKED", "reason": "...", "fallback": {...}}   (exit 1; nothing was sent)

`fallback` names the tracker and the form's short link on every answer, so a
report the install could not file can be handed back with where to file it.

Run:
    file_feedback.py submit --payload-file payload.json [--state-dir DIR]
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# The shared helpers: staged into /opt/defaults/scripts in the images, beside
# this tree in a source checkout.
sys.path.append("/opt/defaults/scripts")
sys.path.append("/opt/data/scripts")
sys.path.append(str(Path(__file__).resolve().parents[3] / "scripts"))

import credential_proxy_client  # noqa: E402
import feedback_broker  # noqa: E402

BROKER_ENDPOINT_VAR = "CREDENTIAL_PROXY_URL"
ROUTE = feedback_broker.ROUTE
RECHECK_FIELD = feedback_broker.RECHECK_FIELD
FORM_CONFIRMED_FIELD = feedback_broker.FORM_CONFIRMED_FIELD
FORM_CONFIRMED_AT_FIELD = feedback_broker.FORM_CONFIRMED_AT_FIELD
MARKER_TMP_SUFFIX = ".tmp"
CONTROL_FIELDS = (RECHECK_FIELD, FORM_CONFIRMED_FIELD, FORM_CONFIRMED_AT_FIELD)
STATUS_SUBMITTED = feedback_broker.STATUS_SUBMITTED
STATUS_UNCONFIRMED = feedback_broker.STATUS_UNCONFIRMED
FALLBACK = feedback_broker.FALLBACK
STATUS_BLOCKED = feedback_broker.STATUS_BLOCKED
STATUS_ALREADY_SUBMITTED = feedback_broker.STATUS_ALREADY_SUBMITTED

MARKER_PREFIX = ".feedback-submission-"
STATE_HASH_LENGTH = 16
BLOCKED_EXIT = 1
# A lost answer exits non-zero too, but on a different code than BLOCKED: the
# agent's next step differs (rerun to recheck, versus fix and resend).
LOST_EXIT = 2
CONTENT_TYPE_JSON = "application/json"

REASON_NO_BROKER = (
    f"{BROKER_ENDPOINT_VAR} is not set, so there is no broker to ask; this "
    "skill runs in the shell sandbox"
)
REASON_BROKER_UNREACHABLE = "the credential broker could not be reached ({error}); nothing was sent"
REASON_NO_TOKEN = "the broker token could not be read ({error}); nothing was sent"
REASON_BAD_ENDPOINT = "the broker endpoint {endpoint!r} is not a URL ({error}); nothing was sent"
REASON_BROKER_ANSWER = (
    "the credential broker answered HTTP {code} without a feedback status; nothing was sent"
)
DETAIL_MARKER_SUBMITTED = (
    "a submission for this exact text already went out from this workspace; not filing it again"
)
DETAIL_RECHECK_UNREACHABLE = (
    "the recheck could not reach the broker ({error}); an earlier request for this text "
    "went out, so nothing is resent -- rerun to recheck the tracker"
)
LOST_MESSAGE = (
    "the broker's answer was lost after the request went out ({error}); the marker "
    "is kept, and rerunning the same command only asks the broker to read the tracker"
)


class BrokerAnswerLost(Exception):
    """The request reached the broker and its answer did not come back whole."""


def _print(answer: dict[str, Any]) -> None:
    print(json.dumps(answer, separators=(",", ":")))


def _blocked(reason: str) -> int:
    _print(feedback_broker.blocked_answer(reason))
    return BLOCKED_EXIT


def _recheck_unreachable(error: str) -> int:
    _print({
        "status": STATUS_UNCONFIRMED,
        "issue_url": None,
        "detail": DETAIL_RECHECK_UNREACHABLE.format(error=error),
        "fallback": FALLBACK,
    })
    return 0


def _payload_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        {field: payload[field] for field in feedback_broker.QUESTION_LABELS}, sort_keys=True
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:STATE_HASH_LENGTH]


def _marker_path(state_dir: Path, payload: dict[str, Any]) -> Path:
    return state_dir / f"{MARKER_PREFIX}{_payload_hash(payload)}.json"


def _read_marker(marker: Path) -> dict[str, Any]:
    """The marker's state, or a bare attempted-state if it was corrupted.

    A marker that exists but cannot be parsed still means an attempt started;
    treating it as anything less would re-open the door to a second POST.
    """
    try:
        parsed = json.loads(marker.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {"started_at": 0.0}
    return parsed if isinstance(parsed, dict) else {"started_at": 0.0}


def _build_request(endpoint: str, payload: dict[str, Any]) -> urllib.request.Request:
    """The broker request, with the token read: everything that can fail before a byte is sent.

    Raises ``TokenUnavailable`` (the token file) and ``ValueError`` (an endpoint
    that is not a URL). Called before the marker is written, so a marker on
    disk always means a request was at least attempted on the wire.
    """
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = {"Content-Type": CONTENT_TYPE_JSON}
    headers.update(credential_proxy_client.authorization_headers())
    return urllib.request.Request(endpoint.rstrip("/") + ROUTE, data=body, headers=headers, method="POST")


def _send(request: urllib.request.Request) -> tuple[int, dict[str, Any]]:
    """Put a built request on the wire: the HTTP status and the JSON body (``{}`` if none)."""
    try:
        with credential_proxy_client.open_broker_request(request) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as exc:
        # The body read sits inside this arm on purpose: an exception raised
        # here is not offered to the sibling arms (credential_proxy_client
        # documents the same trap), so it is classified in place.
        try:
            with exc:
                status, raw = exc.code, exc.read()
        except (http.client.HTTPException, OSError) as broke:
            raise BrokerAnswerLost(f"{type(broke).__name__}: {broke}") from broke
    except urllib.error.URLError:
        raise
    except (http.client.HTTPException, OSError) as exc:
        raise BrokerAnswerLost(f"{type(exc).__name__}: {exc}") from exc
    return status, _json_or_empty(raw)


def _post_to_broker(request: urllib.request.Request) -> tuple[int, dict[str, Any]]:
    """One `POST /v1/feedback/submit` with a request `_build_request` made.

    Raises ``urllib.error.URLError`` when the broker could not be reached at all
    (nothing was sent) and ``BrokerAnswerLost`` when the connection broke after
    the request went out (the broker may have filed). The arm order is
    credential_proxy_client.vcs_call's: URLError first, then everything else
    off an open connection, and the refusal's body read in its own try. The
    request is built once, before the marker is written, so the token file is
    never read after the marker exists.
    """
    return _send(request)


def _write_marker(marker: Path, state: dict[str, Any]) -> None:
    """Write the marker whole: a temp name, then one rename, so a reader never sees a partial file."""
    tmp = marker.with_name(marker.name + MARKER_TMP_SUFFIX)
    tmp.write_text(json.dumps(state), encoding="utf-8")
    os.replace(tmp, marker)


def _json_or_empty(raw: bytes) -> dict[str, Any]:
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def submit(payload_path: Path, state_dir: Path) -> int:
    payload = feedback_broker.normalize(json.loads(payload_path.read_text(encoding="utf-8")))
    # The recheck fields are this script's to set, from its marker, never the
    # payload file's: a file that carried them would send a first attempt down
    # the broker's recheck path and report a request never made as filed.
    for control in CONTROL_FIELDS:
        payload.pop(control, None)
    marker = _marker_path(state_dir, payload)

    if marker.exists():
        state = _read_marker(marker)
        if state.get("issue_url"):
            _print({
                "status": STATUS_ALREADY_SUBMITTED,
                "issue_url": state["issue_url"],
                "detail": DETAIL_MARKER_SUBMITTED,
                "fallback": FALLBACK,
            })
            return 0
        payload = {**payload, RECHECK_FIELD: True}
        if state.get("status") == STATUS_SUBMITTED:
            payload[FORM_CONFIRMED_FIELD] = True
            payload[FORM_CONFIRMED_AT_FIELD] = float(state.get("started_at") or 0.0)

    endpoint = os.environ.get(BROKER_ENDPOINT_VAR, "").strip()
    if not endpoint:
        return _blocked(REASON_NO_BROKER)

    first_attempt = not marker.exists()
    # Everything that can fail before a byte is sent happens before the marker
    # exists: a marker that outlives a request never made would send every
    # rerun down the recheck path for a report nobody filed.
    try:
        request = _build_request(endpoint, payload)
    except credential_proxy_client.TokenUnavailable as exc:
        return _blocked(REASON_NO_TOKEN.format(error=exc))
    except ValueError as exc:
        return _blocked(REASON_BAD_ENDPOINT.format(endpoint=endpoint, error=exc))
    if first_attempt:
        _write_marker(marker, {"started_at": time.time(), "title": payload["title"]})
    try:
        code, answer = _post_to_broker(request)
    except urllib.error.URLError as exc:
        if not first_attempt:
            return _recheck_unreachable(str(exc.reason))
        marker.unlink(missing_ok=True)
        return _blocked(REASON_BROKER_UNREACHABLE.format(error=exc.reason))
    except BrokerAnswerLost as exc:
        print(LOST_MESSAGE.format(error=exc), file=sys.stderr)
        raise SystemExit(LOST_EXIT) from exc

    status = answer.get("status")
    if code == 200 and status and status != STATUS_BLOCKED:
        state = _read_marker(marker)
        state.update({"status": status, "issue_url": answer.get("issue_url")})
        _write_marker(marker, state)
        _print(answer)
        return 0
    if status == STATUS_BLOCKED:
        # Refused (400 BLOCKED): nothing was sent by this request, and a
        # refusal of the payload holds for a rerun too.
        if first_attempt:
            marker.unlink(missing_ok=True)
        _print(answer)
        return BLOCKED_EXIT
    # An answer that is not the broker's (a 503, a 502, a non-JSON body). On a
    # first attempt nothing was sent and the marker is forgotten; on a recheck
    # the earlier request went out, so the marker stays and the status says so.
    if not first_attempt:
        return _recheck_unreachable(f"HTTP {code}")
    marker.unlink(missing_ok=True)
    return _blocked(REASON_BROKER_ANSWER.format(code=code))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    submit_parser = subparsers.add_parser("submit", help="file the confirmed payload, exactly once")
    submit_parser.add_argument("--payload-file", type=Path, required=True)
    submit_parser.add_argument(
        "--state-dir", type=Path, default=Path.cwd(),
        help="where the submission marker lives; use the task workspace so a "
             "rerun sees its own earlier attempt",
    )
    args = parser.parse_args()
    return submit(args.payload_file, args.state_dir)


if __name__ == "__main__":
    sys.exit(main())
