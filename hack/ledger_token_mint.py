#!/usr/bin/env python3
"""Mint a one-hour GitHub installation token from the ledger-reader App's key.

Shared by hack/ci-eval-pr.sh (grading's reads and the two resets) and by step 0,
hack/ci-revalidate.sh (its status reads: the /override check and the attestation). Everything secret or
caller-specific arrives through the environment, never argv:

  EVAL_LEDGER_APP_KEY_FILE   the App's private key (PEM); required
  LEDGER_MINT_BODY           the JSON body naming what the token may reach;
                             required and non-empty, see below
  EVAL_LEDGER_APP_ID         the App; defaults to the ledger-reader App
  EVAL_LEDGER_INSTALLATION_ID  its installation; defaults likewise

The one argument is the exit code to use for a failure another attempt could
survive (a 5xx, a 429, a 403 GitHub marks as its rate limit, an unreachable
api.github.com); any other failure exits
1. Passed in rather than duplicated so the two halves of the contract cannot
drift: the shell decides what it retries, this decides what is retryable.

Emits "<token> <expires_at>" on stdout and diagnostics on stderr.
"""

import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

# The ledger-reader App and its installation on the infrastructure
# organisation (docs/ci-pool-projects.md 5.4). hack/ci-eval-pr.sh exports the
# same two values; step 0 relies on these.
DEFAULT_LEDGER_APP_ID = "4739812"
DEFAULT_LEDGER_INSTALLATION_ID = "157029058"
MINT_URL = "https://api.github.com/app/installations/%s/access_tokens"
USER_AGENT = "kube-agents-ci-eval-pr"
HTTP_TIMEOUT_SECONDS = 30
# GitHub rejects an App JWT whose exp is more than ten minutes out; nine leaves
# room for clock skew, and the backdated iat covers a runner that is slow.
JWT_BACKDATE_SECONDS = 60
JWT_LIFETIME_SECONDS = 540
# How much of openssl's stderr a signing failure quotes.
OPENSSL_STDERR_LIMIT = 300
# A 403 is GitHub's secondary rate limit, not a refusal, when it carries one of
# these marks. The same discriminator as hack/ci_sweep_agent_pulls.py's
# is_rate_limited, copied rather than imported so the mint loads nothing else
# from hack/; tests/test_ci_eval_ledger_mint.py holds the two copies together.
FORBIDDEN_CODE = 403
RETRY_AFTER_HEADER = "Retry-After"
RATELIMIT_REMAINING_HEADER = "X-RateLimit-Remaining"
RATE_LIMIT_BODY_MARKERS = ("rate limit", "abuse detection")
# How much of a refusal's body is read for the marks.
ERROR_BODY_CHARS = 300

retryable = int(sys.argv[1])


def temporary(message):
    sys.stderr.write(message + "\n")
    sys.exit(retryable)


key_file = os.environ["EVAL_LEDGER_APP_KEY_FILE"]
app_id = os.environ.get("EVAL_LEDGER_APP_ID") or DEFAULT_LEDGER_APP_ID
installation_id = os.environ.get("EVAL_LEDGER_INSTALLATION_ID") or DEFAULT_LEDGER_INSTALLATION_ID
# What the token may reach. The grading mint asks for its three reads
# (LEDGER_GRADING_MINT_BODY) and the ledger reset asks for one repository and
# `issues: write` (ledger_reset_token). An empty body would mean the
# installation's whole grant -- issues: write on every pool repository -- so
# it is refused here rather than sent: a caller that forgets the body fails
# to mint instead of silently holding the widest token there is. A token
# narrowed at mint cannot be widened by whoever holds it afterwards.
mint_body = os.environ.get("LEDGER_MINT_BODY", "").strip()
if not mint_body:
    sys.exit(
        "LEDGER_MINT_BODY is empty; refusing to mint for App %s: a mint without a body "
        "receives the installation's whole grant, and every caller names what it asks for"
        % app_id
    )


def rate_limited_403(exc):
    """A 403 GitHub marks as its rate limit: a Retry-After, no requests
    remaining, or a body naming a limit. An unmarked 403 is a refusal."""
    if exc.code != FORBIDDEN_CODE:
        return False
    headers = exc.headers or {}
    if headers.get(RETRY_AFTER_HEADER) is not None:
        return True
    if str(headers.get(RATELIMIT_REMAINING_HEADER, "")).strip() == "0":
        return True
    try:
        text = exc.read(ERROR_BODY_CHARS).decode("utf-8", "replace").lower()
    except Exception:
        # A body cut short reads as no marks: the status stays a refusal.
        text = ""
    return any(marker in text for marker in RATE_LIMIT_BODY_MARKERS)


def b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=")


now = int(time.time())
header = b64(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
payload = b64(
    json.dumps(
        {"iat": now - JWT_BACKDATE_SECONDS, "exp": now + JWT_LIFETIME_SECONDS, "iss": app_id},
        separators=(",", ":"),
    ).encode()
)
signing_input = header + b"." + payload

signed = subprocess.run(
    ["openssl", "dgst", "-sha256", "-sign", key_file],
    input=signing_input,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
)
if signed.returncode != 0:
    sys.exit(
        "openssl could not sign with %s: %s" % (key_file, signed.stderr.decode()[:OPENSSL_STDERR_LIMIT])
    )
jwt = (signing_input + b"." + b64(signed.stdout)).decode("ascii")

mint_headers = {
    "Authorization": "Bearer " + jwt,
    "Accept": "application/vnd.github+json",
    "Content-Type": "application/json",
    "User-Agent": USER_AGENT,
}
request = urllib.request.Request(
    MINT_URL % installation_id,
    method="POST",
    headers=mint_headers,
    data=mint_body.encode(),
)
try:
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
        body = json.load(response)
except urllib.error.HTTPError as exc:
    # 401: the PEM is not App app_id's. 404: the installation id is wrong, or
    # the App was uninstalled from the org. Neither survives another attempt,
    # and a caller holding two locks should hear about them on the first.
    # A 403 is either: a suspended installation is terminal with them, but
    # GitHub's secondary rate limit -- one installation minting for every unit
    # of concurrent runs -- is a 403 too, and it says so (rate_limited_403),
    # so that one is retried like a 429. 422 is terminal too: with a body it means the installation
    # does not hold a permission or repository the body asked for, which is
    # an organisation-settings change, not something a retry reaches.
    message = "GitHub answered HTTP %d (%s) minting for App %s installation %s" % (
        exc.code,
        exc.reason,
        app_id,
        installation_id,
    )
    if exc.code >= 500 or exc.code == 429 or rate_limited_403(exc):
        temporary(message)
    sys.exit(message)
except Exception as exc:
    # A timeout, a reset connection, DNS: api.github.com was not reached, which
    # says nothing about the credential.
    temporary(
        "could not reach api.github.com to mint for App %s (%s: %s)"
        % (app_id, type(exc).__name__, exc)
    )

print(body["token"] + " " + body["expires_at"])
