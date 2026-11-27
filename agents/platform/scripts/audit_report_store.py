#!/usr/bin/env python3
"""The audit-report store's broker-only listener (#5619).

The fleet-audit report store (docs/designs/fleet-audit-report-store.md) is a
tree on the agent's PVC that `audit_report.py finish` writes in place. A
profile worker runs in a pod of its own with no PVC, so it
reaches the store through the credential broker, and the broker reaches it
here: an HTTP listener in the gateway pod over the same tree.

A process of its own on its own port (8700), not more routes on Session KV's
8699, so it serves the store routes and nothing else by construction. It runs
in the gateway pod's agent-api-auth container (start-services.sh), which
already mounts the PVC and the pod's API-audience ServiceAccount token; the
platform-agent container is held to audience-bound tokens only
(docs/security-requirements.md).

Every request carries the broker's projected ServiceAccount token, bound to
the audience AUDIENCE. It is verified offline against the cluster's JWKS,
which the API server publishes to every ServiceAccount through the default
`system:service-account-issuer-discovery` binding, so the listener needs no
RBAC of its own. The subject must be the broker's ServiceAccount
(ALLOWED_SUBJECT_ENV). A rule in the gateway NetworkPolicy
`<agent>-gateway-netpol` admits only the broker's pods on the port; this
check holds where that policy is off or not enforced.

Routes (the store key is ONE path segment: the file store's repository
directory with its `/` escaped as `%2F`; a non-GitHub `host/<path with %2F>`
escaped again, so `gitlab.com%2Fgroup%252Frepo`):

    GET    /v1/audit-reports                               the streams
    GET    /v1/audit-reports/<audit>/<key>/latest          latest.json
    GET    /v1/audit-reports/<audit>/<key>/runs            run names, newest first
    GET    /v1/audit-reports/<audit>/<key>/runs/<stamp>    one ring entry
    PUT    /v1/audit-reports/<audit>/<key>/runs/<stamp>    write a ring entry, prune
    GET    /v1/audit-reports/<audit>/<key>/runs/started-<run>  one run-start record
    PUT    /v1/audit-reports/<audit>/<key>/runs/started-<run>  write a run-start record, prune
    PUT    /v1/audit-reports/<audit>/<key>/latest          copy a ring entry to latest
    DELETE /v1/audit-reports/<audit>/<key>/latest          drop latest (idempotent)

Who wrote each run: the broker stamps every PUT of a ring entry with the
X-Report-Writer header (REPORT_WRITER_HEADER), `shell` for the platform
shell or `builtin-schedule:<audit>:<task>` for a profile task writing a
builtin schedule's stream, from the caller it verified; a caller's own header
never reaches here. The listener refuses a PUT of a ring entry whose header
is not well formed for the path's audit (400), and keeps it beside the
envelope as `runs/<stamp>.writer` (REPORT_WRITER_SUFFIX): a small JSON
object holding the writer and the SHA-256 of the exact envelope bytes it
describes. A PUT with no header at all (a broker from before this change:
the broker and this listener roll independently) is written with no
sidecar, any old one dropped, so it reads back unstamped. PUT latest copies the matched ring entry's sidecar to
`latest.writer` (the bytes are identical, so is the hash). GET latest and GET
runs/<stamp> answer the stored writer in the same header only when the
sidecar's hash is that of the bytes being returned, so a sidecar can never
vouch for another envelope; otherwise, or with no sidecar (an envelope written
before this, or by the file store directly), there is no header. The
envelope's bytes are never changed.

A run-start record (#6812) is the one name on the run routes that is not a
stamp: `started-<run>`, with <run> the run's id (a DNS-1123 label: a profile
task's id, or the id `start` made for a run in the shell). `start` writes it
and `finish` rewrites it with `done_at`, so a reader can tell a stream whose
newest run started and never finished. It is kept in a sibling directory,
`run-starts/<run>.json`, never in `runs/`: the file store's readers list
`runs/*.json` as the ring, and an older one would read a record there as the
newest run. So it is never a ring entry: not listed by GET runs, not pruned
with the ring, never matched by PUT latest, and GET or DELETE latest cannot
name it. A record is a small JSON object (RUN_START_MAX_BYTES) whose `run_id`
is the name's and whose `started_at` is a time with a zone, no more than
RUN_START_MAX_FUTURE_SECONDS ahead of this clock, so a done mark with no start
is refused. The writer's clock orders nothing: this listener sets the record's
`received_at` to its own clock when it first takes the run's record, replacing
any the body sends, and a rewrite of that run's record (the done mark) keeps
the first `received_at`, so marking a run done never moves it to the top.
"Newest" is by `received_at`, then run id; a record written before
`received_at` existed has none and sorts below every record that has one, by
its `started_at`. The stream keeps the newest RUN_START_HISTORY, a bound on
disk: the broker lets a profile task write only the record named for its own
task (credential-isolation.md), so the records a task can push out are older
than eight real runs. The writer header is checked as
on a ring entry's PUT and not kept: the record is no report, and the A2A relay
that trusts a stamp cannot read it. The stream list carries the newest record
of each stream as `last_run_start`, when there is one. A record never creates
the stream's store: a PUT for a store that does not exist yet is answered 404,
because a store that exists is one a run has published to, so a stream's first
run for a repository is not tracked.

The path is read from the raw request path, not the decoded one, because the
key's `%2F` is what keeps it one segment. The token is never logged.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import errno
import fcntl
import hashlib
import json
import logging
import os
import re
import secrets
import socket
import ssl
import stat
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
LOGGER = logging.getLogger("audit_report_store")

# The tree audit_report.py writes (its REPORTS_DIR default). Not read from
# the environment here: spec.deployment.env reaches this container, and a root
# a CR could move is a store a CR could point anywhere. The tests pass another
# root to create_app.
DEFAULT_REPORTS_DIR = "/opt/data/fleet-audit/reports"
# These mirror audit_report.py's constants of the same names. That module is a
# skill script and is not importable from here; test_audit_report_store.py
# holds the two copies equal.
REPORT_HISTORY = 14
# The clock run stamps are checked against; a test seam.
_now = time.time
# How far ahead of this clock a written run's stamp may be: finish stamps with
# its own clock as it ends, so this is clock skew, not a schedule.
RUN_STAMP_MAX_FUTURE_SECONDS = 300
REPORT_STAMP_FORMAT = "%Y%m%dT%H%M%S.%fZ"
REPORT_RUNS_DIR = "runs"
REPORT_LATEST_NAME = "latest.json"
# Who wrote a stored envelope (the module docstring): the header the broker
# sets, and the sidecar beside the envelope that keeps it. The broker holds a
# copy of the header name and the pattern (credential_proxy.REPORT_WRITER_*),
# which test_audit_report_routes.py holds equal.
REPORT_WRITER_HEADER = "X-Report-Writer"
REPORT_WRITER_SUFFIX = ".writer"
# A sidecar's `sha256`: hashlib's hexdigest form, so a planted value (a JSON
# \u escape decodes to a non-ASCII str) never reaches secrets.compare_digest.
REPORT_WRITER_DIGEST_RE = re.compile(r"^[0-9a-f]{64}\Z")
REPORT_WRITER_LATEST_NAME = "latest" + REPORT_WRITER_SUFFIX
REPORT_WRITER_SHELL = "shell"
REPORT_WRITER_BUILTIN = "builtin-schedule"
# `shell`, or `builtin-schedule:<audit id>:<task id>`, each id a DNS-1123
# label (a task id is task_credential's dot-free label).
_WRITER_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
REPORT_WRITER_RE = re.compile(
    rf"^(?:{REPORT_WRITER_SHELL}|{REPORT_WRITER_BUILTIN}:(?P<audit>{_WRITER_LABEL}):{_WRITER_LABEL})\Z"
)
REPORT_DIR_MODE = 0o755
REPORT_FILE_MODE = 0o644
REPORT_REPO_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.-]+\Z")
STORE_PATH_SEPARATOR = "%2F"
# The longest directory name the store creates, in bytes: NAME_MAX on Linux
# and macOS. A longer owner or name is a 400 before any directory is made.
STORE_DIR_NAME_MAX_BYTES = 255
# One envelope. audit_report.py keeps the ledger body near 1 MB across the
# ring (MAX_BODY_CHARS is 64 KiB an envelope) and the findings document is
# unbounded there; here a single envelope is capped at 1 MiB, and anything
# larger is answered 413 rather than written.
REPORT_MAX_BYTES = 1 << 20

# The run-start record (the module docstring). report_status.py holds copies of
# the names and the bounds, which test_audit_report_store.py holds equal.
RUN_START_PREFIX = "started-"
RUN_STARTS_DIR = "run-starts"
_RUN_ID = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
RUN_ID_RE = re.compile(rf"^{_RUN_ID}\Z")
RUN_START_NAME_RE = re.compile(rf"^{RUN_START_PREFIX}(?P<run>{_RUN_ID})\Z")
RUN_START_HISTORY = 8
RUN_START_MAX_BYTES = 4096
# How far ahead of this clock a record's `started_at` may be: `start` stamps it
# as the run begins, so an honest writer needs only clock skew.
RUN_START_MAX_FUTURE_SECONDS = 60
# Set by this listener alone, from its own clock: the order of the records.
RUN_START_RECEIVED_KEY = "received_at"
# The keys a stream listing passes on from the newest record, each only with
# its own type: `run_id`, `started_at`, `received_at`, `done_at` and `outcome`
# strings, `scheduled` a boolean.
RUN_START_LISTED_KEYS = ("run_id", "started_at", "received_at", "scheduled", "done_at", "outcome")
RUN_START_OUTCOME_MAX_CHARS = 64

AUDIT_REPORTS_PREFIX = "/v1/audit-reports"
AUDIT_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
STAMP_RE = re.compile(r"^[0-9]{8}T[0-9]{6}\.[0-9]{6}Z\Z")
_SEPARATOR_RE = re.compile(re.escape(STORE_PATH_SEPARATOR), re.IGNORECASE)

# The broker presents a token bound to this audience and to nothing else, so
# a token minted for any other listener does not pass here, and this one
# passes nowhere else. The operator renders the same string
# (auditReportStoreAudience in k8s-operator).
AUDIENCE = "kubeagents-audit-report-store"
# The two caller checks the operator renders (auditReportStoreSidecarEnv),
# both names reserved there so spec.deployment.env cannot set them: the
# broker's ServiceAccount as a subject, and the name prefix of the broker
# Deployment's pods. The broker's ServiceAccount is shared with other
# pods (the exec tier among them), so the subject alone is not the broker.
ALLOWED_SUBJECT_ENV = "AUDIT_REPORT_STORE_ALLOWED_SUBJECT"
ALLOWED_POD_PREFIX_ENV = "AUDIT_REPORT_STORE_ALLOWED_POD_PREFIX"
# How Kubernetes names a Deployment's pod, modelled exactly. The ReplicaSet is
# `<deployment>-<pod-template-hash>` and its pods' generateName is that plus
# "-"; the API server's SimpleNameGenerator cuts a generateName to
# POD_GENERATE_NAME_MAX_BASE characters and then adds
# POD_GENERATE_NAME_RANDOM_LENGTH random ones. So a broker pod is named
# (P + hash + "-")[:58] + random, with P the prefix the operator renders
# (`<agent>-credential-proxy-`). For an agent name of about 30 characters or
# more the "-" after the hash is cut, and from about 41 the prefix itself.
POD_GENERATE_NAME_MAX_BASE = 58  # 63 (a DNS label) minus the random suffix
POD_GENERATE_NAME_RANDOM_LENGTH = 5
POD_TEMPLATE_HASH_MIN_LENGTH = 1
POD_TEMPLATE_HASH_MAX_LENGTH = 10  # a uint32, safe-encoded
_POD_NAME_PART_RE = re.compile(r"[a-z0-9]*\Z")
# The exec tier's pods share the broker's ServiceAccount and are named
# `<agent>-credential-proxy-exec-tier-<hash>-<random>`, under the broker's
# prefix. Their remainder after it begins with this, which no pod-template
# hash does (its alphabet has no vowels), so a name whose remainder begins
# with it is refused at every length, including where the truncation would
# otherwise leave a bare "exec" that reads as a hash.
EXEC_TIER_REMAINDER_PREFIX = "exec"
# Where the JWKS comes from and the token it is read with: fixed, never from
# the environment, for the same reason as the root. A CR that could move the
# API server would be handed the pod's own token and could answer with a JWKS
# of its own, which would pass any token it signs.
DEFAULT_API_SERVER = "https://kubernetes.default.svc"
JWKS_ACCEPT = "application/jwk-set+json, application/json"
DEFAULT_SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
# The port. start-services.sh runs this module as a script, which binds it.
LISTEN_PORT = 8700
DISCOVERY_PATH = "/.well-known/openid-configuration"
JWKS_PATH = "/openid/v1/jwks"
JWKS_TTL_SECONDS = 300.0
# A token naming a kid the cache lacks refetches, at most this often, so a
# caller sending random kids cannot turn each request into an API call.
JWKS_FORCED_REFRESH_SECONDS = 10.0
JWKS_FETCH_TIMEOUT_SECONDS = 5.0
# A failed fetch is remembered this long: requests in the window are answered
# 503 at once instead of each waiting out the fetch timeout again.
JWKS_FAILURE_HOLD_SECONDS = 10.0
JWKS_MAX_BYTES = 1 << 20
CLOCK_SKEW_SECONDS = 30
MAX_TOKEN_BYTES = 16 * 1024


class AuthRefused(Exception):
    """A request the auth check refuses, with the status to answer it."""

    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


class JWKSUnavailable(Exception):
    """The discovery document or the JWKS could not be read."""


def _b64url_decode(text: str) -> bytes:
    if not re.fullmatch(r"[A-Za-z0-9_-]*", text):
        raise ValueError("not base64url")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _b64url_int(text: object) -> int:
    if not isinstance(text, str) or not text:
        raise ValueError("missing key parameter")
    return int.from_bytes(_b64url_decode(text), "big")


def _http_get_json(url: str, token: str | None, ca_file: str | None) -> dict:
    # The API server serves /openid/v1/jwks as application/jwk-set+json and
    # answers 406 to an Accept that names only application/json (seen live on
    # GKE); discovery is application/json. Name both.
    request = urllib.request.Request(url, headers={"Accept": JWKS_ACCEPT})
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    context = None
    if url.startswith("https://"):
        context = ssl.create_default_context(cafile=ca_file if ca_file and os.path.exists(ca_file) else None)
    try:
        with urllib.request.urlopen(request, timeout=JWKS_FETCH_TIMEOUT_SECONDS, context=context) as response:
            body = response.read(JWKS_MAX_BYTES + 1)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise JWKSUnavailable(f"GET {url}: {exc}") from exc
    if len(body) > JWKS_MAX_BYTES:
        raise JWKSUnavailable(f"GET {url}: answer over {JWKS_MAX_BYTES} bytes")
    try:
        document = json.loads(body)
    except (ValueError, RecursionError) as exc:
        raise JWKSUnavailable(f"GET {url}: not JSON") from exc
    if not isinstance(document, dict):
        raise JWKSUnavailable(f"GET {url}: not a JSON object")
    return document


def fetch_cluster_keys(api_server: str = DEFAULT_API_SERVER, sa_dir_path: str = DEFAULT_SA_DIR) -> tuple[str, list[dict]]:
    """The issuer and the signing keys, read from the API server.

    Authenticated with the pod's own token, read fresh each time because the
    kubelet rotates it. The two arguments are for the tests.
    """
    base = api_server.rstrip("/")
    sa_dir = Path(sa_dir_path)
    try:
        token = (sa_dir / "token").read_text(encoding="utf-8").strip()
    except OSError:
        token = None
    ca_file = str(sa_dir / "ca.crt")
    discovery = _http_get_json(base + DISCOVERY_PATH, token, ca_file)
    issuer = discovery.get("issuer")
    if not isinstance(issuer, str) or not issuer:
        raise JWKSUnavailable("the discovery document names no issuer")
    keys = _http_get_json(base + JWKS_PATH, token, ca_file).get("keys")
    if not isinstance(keys, list):
        raise JWKSUnavailable("the JWKS has no key list")
    return issuer, [key for key in keys if isinstance(key, dict)]


class TokenVerifier:
    """Offline verification of the broker's projected ServiceAccount token."""

    def __init__(
        self,
        fetch: Callable[[], tuple[str, list[dict]]] = fetch_cluster_keys,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetch = fetch
        self._clock = clock
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._issuer: str | None = None
        self._keys: dict[str, dict] = {}
        self._fetched_at: float | None = None
        self._forced_at: float | None = None
        self._failed_at: float | None = None

    def _keyset(self, kid: str) -> tuple[str, dict | None]:
        """The issuer and the key named `kid`, refetching as the cache needs.

        Raises JWKSUnavailable when a fetch it needs fails, and for
        JWKS_FAILURE_HOLD_SECONDS after one without trying again: an expired
        cache is not trusted past its TTL. The lock guards the cache only; the
        fetch runs outside it, so one slow API server call does not hold every
        other request, cached kid or not.
        """
        with self._lock:
            now = self._monotonic()
            expired = self._fetched_at is None or now - self._fetched_at >= JWKS_TTL_SECONDS
            # An unknown kid is the API server rotating its signing key:
            # refetch once, not once per request.
            forced = not expired and kid not in self._keys and (
                self._forced_at is None or now - self._forced_at >= JWKS_FORCED_REFRESH_SECONDS
            )
            # A kid the cache lacks, inside the hold after a failed fetch, is
            # one the cluster may well have: the JWKS is unavailable (503),
            # not the key unknown (401), whichever branch reaches it.
            held = self._failed_at is not None and now - self._failed_at < JWKS_FAILURE_HOLD_SECONDS
            if not expired and not forced:
                assert self._issuer is not None
                if held and kid not in self._keys:
                    raise JWKSUnavailable("the last JWKS fetch failed moments ago and the key id is not cached")
                return self._issuer, self._keys.get(kid)
            if held:
                if expired or kid not in self._keys:
                    raise JWKSUnavailable("the last JWKS fetch failed moments ago")
                assert self._issuer is not None
                return self._issuer, self._keys.get(kid)
            if forced:
                self._forced_at = now
        try:
            issuer, keys = self._fetch()
        except JWKSUnavailable:
            with self._lock:
                self._failed_at = self._monotonic()
            raise
        with self._lock:
            self._issuer = issuer
            self._keys = {key["kid"]: key for key in keys if isinstance(key.get("kid"), str)}
            self._fetched_at = self._monotonic()
            self._failed_at = None
            return self._issuer, self._keys.get(kid)

    def verify(self, authorization: str | None) -> str:
        """The token's subject, once every check passes.

        Raises AuthRefused: 401 for a missing or failing token, 403 for a valid
        one whose subject is not the broker, 503 when the JWKS cannot be read.
        """
        if not authorization:
            raise AuthRefused(401, "no bearer token")
        scheme, _, token = authorization.partition(" ")
        token = token.strip()
        if scheme.lower() != "bearer" or not token:
            raise AuthRefused(401, "not a bearer token")
        if len(token) > MAX_TOKEN_BYTES:
            raise AuthRefused(401, "token too long")
        parts = token.split(".")
        if len(parts) != 3:
            raise AuthRefused(401, "not a JWT")
        try:
            header = json.loads(_b64url_decode(parts[0]))
            claims = json.loads(_b64url_decode(parts[1]))
            signature = _b64url_decode(parts[2])
        except (ValueError, RecursionError, binascii.Error, UnicodeDecodeError):
            raise AuthRefused(401, "malformed JWT") from None
        if not isinstance(header, dict) or not isinstance(claims, dict):
            raise AuthRefused(401, "malformed JWT")
        alg = header.get("alg")
        kid = header.get("kid")
        if alg not in ("RS256", "ES256"):
            raise AuthRefused(401, f"algorithm {alg!r} is not accepted")
        if not isinstance(kid, str) or not kid:
            raise AuthRefused(401, "no key id")
        try:
            issuer, key = self._keyset(kid)
        except JWKSUnavailable as exc:
            LOGGER.warning("cannot read the cluster JWKS: %s", exc)
            raise AuthRefused(503, "the cluster JWKS is unavailable") from None
        if key is None:
            raise AuthRefused(401, "unknown key id")
        signing_input = f"{parts[0]}.{parts[1]}".encode("ascii")
        if not _signature_valid(alg, key, signing_input, signature):
            raise AuthRefused(401, "bad signature")
        if claims.get("iss") != issuer:
            raise AuthRefused(401, "wrong issuer")
        audience = claims.get("aud")
        audiences = [audience] if isinstance(audience, str) else audience if isinstance(audience, list) else []
        if AUDIENCE not in audiences:
            raise AuthRefused(401, "wrong audience")
        now = self._clock()
        exp = claims.get("exp")
        if not isinstance(exp, (int, float)) or isinstance(exp, bool) or now > exp + CLOCK_SKEW_SECONDS:
            raise AuthRefused(401, "expired")
        nbf = claims.get("nbf")
        if nbf is not None and (
            not isinstance(nbf, (int, float)) or isinstance(nbf, bool) or now < nbf - CLOCK_SKEW_SECONDS
        ):
            raise AuthRefused(401, "not yet valid")
        subject = claims.get("sub")
        allowed = os.environ.get(ALLOWED_SUBJECT_ENV, "").strip()
        if not isinstance(subject, str) or not allowed or subject != allowed:
            raise AuthRefused(403, "subject is not the broker")
        # The pod the token is bound to: the broker Deployment's, by name.
        prefix = os.environ.get(ALLOWED_POD_PREFIX_ENV, "").strip()
        kube = claims.get("kubernetes.io")
        pod = kube.get("pod") if isinstance(kube, dict) else None
        pod_name = pod.get("name") if isinstance(pod, dict) else None
        if not isinstance(pod_name, str) or not is_broker_pod_name(prefix, pod_name):
            raise AuthRefused(403, "the token is not bound to a broker pod")
        return subject


def _lower_alnum(text: str) -> bool:
    return bool(_POD_NAME_PART_RE.match(text))


def is_broker_pod_name(prefix: str, pod_name: str) -> bool:
    """Whether (prefix + hash + "-")[:58] + random can produce `pod_name`.

    The hash is POD_TEMPLATE_HASH_MIN_LENGTH to POD_TEMPLATE_HASH_MAX_LENGTH
    lowercase alphanumerics and the random part POD_GENERATE_NAME_RANDOM_LENGTH.
    A name whose remainder after the prefix begins with
    EXEC_TIER_REMAINDER_PREFIX is refused: that is the exec tier's.
    """
    if not prefix or len(pod_name) <= POD_GENERATE_NAME_RANDOM_LENGTH:
        return False
    if pod_name.startswith(prefix) and pod_name[len(prefix):].startswith(EXEC_TIER_REMAINDER_PREFIX):
        return False
    base, random_part = pod_name[:-POD_GENERATE_NAME_RANDOM_LENGTH], pod_name[-POD_GENERATE_NAME_RANDOM_LENGTH:]
    if not _lower_alnum(random_part) or len(base) > POD_GENERATE_NAME_MAX_BASE:
        return False
    if len(prefix) >= POD_GENERATE_NAME_MAX_BASE:
        # The prefix itself was cut: nothing of the hash is left to check.
        return base == prefix[:POD_GENERATE_NAME_MAX_BASE]
    if not base.startswith(prefix):
        return False
    rest = base[len(prefix):]
    if len(base) < POD_GENERATE_NAME_MAX_BASE or rest.endswith("-"):
        # Not cut, or cut exactly after the "-": rest is the whole hash and "-".
        pod_hash = rest[:-1]
        return (
            rest.endswith("-")
            and POD_TEMPLATE_HASH_MIN_LENGTH <= len(pod_hash) <= POD_TEMPLATE_HASH_MAX_LENGTH
            and _lower_alnum(pod_hash)
        )
    # Cut inside the hash or just before its "-": rest is the hash's first
    # characters.
    return len(rest) <= POD_TEMPLATE_HASH_MAX_LENGTH and _lower_alnum(rest)


def _signature_valid(alg: str, key: dict, signing_input: bytes, signature: bytes) -> bool:
    # Imported here so that importing the module costs nothing a test of the
    # path rules does not need. The image ships cryptography (the broker's
    # task_credential.py imports it).
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

    if key.get("alg") not in (None, alg):
        return False
    if key.get("use") not in (None, "sig"):
        return False
    try:
        if alg == "RS256":
            if key.get("kty") != "RSA":
                return False
            public = rsa.RSAPublicNumbers(_b64url_int(key.get("e")), _b64url_int(key.get("n"))).public_key()
            public.verify(signature, signing_input, padding.PKCS1v15(), hashes.SHA256())
            return True
        if key.get("kty") != "EC" or key.get("crv") != "P-256" or len(signature) != 64:
            return False
        public_ec = ec.EllipticCurvePublicNumbers(
            _b64url_int(key.get("x")), _b64url_int(key.get("y")), ec.SECP256R1()
        ).public_key()
        der = encode_dss_signature(int.from_bytes(signature[:32], "big"), int.from_bytes(signature[32:], "big"))
        public_ec.verify(der, signing_input, ec.ECDSA(hashes.SHA256()))
        return True
    except (InvalidSignature, ValueError, TypeError, binascii.Error):
        return False


# --------------------------------------------------------------------------- #
# The store.
# --------------------------------------------------------------------------- #


class Refused(Exception):
    """A request the store refuses, with its status."""

    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


def _plain_segment(part: str) -> bool:
    return bool(REPORT_REPO_SEGMENT_RE.match(part)) and part not in (os.curdir, os.pardir)


def store_dirs_for_key(key: str) -> tuple[str, str]:
    """The two directory names under the stream's directory for a store key.

    `key` is the raw path segment. Decoded once, it is the file store's
    repository directory relative to the stream: `owner/name`, or
    `host/<path with %2F>`. Lower-cased as audit_report.reports_dir_for does,
    and a GitHub name with its host keeps the bare slug's directory, as there.
    Raises Refused (400) for anything else.
    """
    try:
        decoded = urllib.parse.unquote(key, errors="strict")
    except UnicodeDecodeError:
        raise Refused(400, "the store key is not UTF-8") from None
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in decoded):
        raise Refused(400, "the store key has a control character")
    parts = decoded.split("/")
    if len(parts) != 2:
        raise Refused(400, "the store key is not owner/name or host/path")
    owner = parts[0].lower()
    below = [part.lower() for part in _SEPARATOR_RE.split(parts[1])]
    if not _plain_segment(owner) or not below or not all(_plain_segment(part) for part in below):
        raise Refused(400, "the store key has a part that is not a plain path segment")
    if len(below) == 1:
        return _bounded(owner, below[0])
    if owner == "github.com" and len(below) == 2:
        return _bounded(below[0], below[1])
    if "." not in owner:
        raise Refused(400, "a store key below a host names the host first")
    return _bounded(owner, STORE_PATH_SEPARATOR.join(below))


def _bounded(owner: str, name: str) -> tuple[str, str]:
    for part in (owner, name):
        if len(part.encode("utf-8")) > STORE_DIR_NAME_MAX_BYTES:
            raise Refused(400, f"a store key part is over {STORE_DIR_NAME_MAX_BYTES} bytes")
    return owner, name


def store_key_for_dirs(owner: str, name: str) -> str:
    """The store key a listing hands back for a store directory: the inverse."""
    return urllib.parse.quote(f"{owner}/{name}", safe="")


def _validate_audit_id(audit_id: str) -> str:
    if not AUDIT_ID_RE.match(audit_id):
        raise Refused(400, "the audit id is not a DNS-1123 label")
    return audit_id


def _validate_stamp(stamp: str) -> str:
    if not STAMP_RE.match(stamp):
        raise Refused(400, "the run is not a report stamp")
    try:
        datetime.strptime(stamp, REPORT_STAMP_FORMAT)
    except ValueError:
        raise Refused(400, "the run is not a report stamp") from None
    return stamp


def _refuse_a_future_stamp(stamp: str) -> None:
    """Refuse a run stamped further ahead of this clock than RUN_STAMP_MAX_FUTURE_SECONDS.

    The ring prunes by name, oldest first, so a far-future stamp would never be
    pruned and would hold one of the stream's REPORT_HISTORY places for good.
    `finish` stamps a run with its own clock as it ends, so a small skew is all
    an honest writer needs.
    """
    when = datetime.strptime(stamp, REPORT_STAMP_FORMAT).replace(tzinfo=timezone.utc).timestamp()
    if when > _now() + RUN_STAMP_MAX_FUTURE_SECONDS:
        raise Refused(400, "the run is stamped in the future")


def _validated_writer(writer: str | None, audit_id: str) -> str | None:
    """The writer header of a PUT of a ring entry, None when absent. 400 unless well formed.

    `shell`, or `builtin-schedule:<audit>:<task>` with <audit> the path's own:
    a builtin schedule's task writes its own audit's stream and no other. No
    header is a broker from before the stamp (the two roll independently):
    accepted, and the run is stored unstamped.
    """
    if writer is None:
        return None
    match = REPORT_WRITER_RE.match(writer)
    if match is None:
        raise Refused(400, f"the {REPORT_WRITER_HEADER} header is not a writer")
    if match.group("audit") is not None and match.group("audit") != audit_id:
        raise Refused(400, f"the {REPORT_WRITER_HEADER} header names another audit")
    return writer


def _writer_sidecar(writer: str, data: bytes) -> bytes:
    """The sidecar for `writer` over the envelope `data`: the writer and the envelope's SHA-256."""
    return json.dumps({"writer": writer, "sha256": hashlib.sha256(data).hexdigest()}, sort_keys=True).encode("ascii")


def _bound_writer(dir_fd: int, name: str, data: bytes) -> str | None:
    """The writer the sidecar `name` records for exactly the envelope `data`, or None.

    None when there is no sidecar, when it is not a sidecar (not the JSON
    object _writer_sidecar writes, or a writer that is not one), and when its
    hash is not that of `data`: a sidecar describing another envelope (a
    concurrent write, or a listener without sidecars rewriting the envelope
    after a rollback) is never passed on. A symlink or a non-regular file at
    the name is never followed or read, and is no writer: the envelope's read
    goes on, unstamped.
    """
    try:
        raw = _read_at(dir_fd, name)
    except Refused as exc:
        if exc.status != 404:
            LOGGER.warning("ignored a report writer sidecar that could not be read (%d): %s", exc.status, _sanitize(name))
        return None
    try:
        sidecar = json.loads(raw.decode("ascii"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        sidecar = None
    writer = sidecar.get("writer") if isinstance(sidecar, dict) else None
    digest = sidecar.get("sha256") if isinstance(sidecar, dict) else None
    if (
        not isinstance(writer, str)
        or not REPORT_WRITER_RE.match(writer)
        or not isinstance(digest, str)
        or not REPORT_WRITER_DIGEST_RE.match(digest)
    ):
        LOGGER.warning("ignored a report writer sidecar that is not one: %s", _sanitize(name))
        return None
    if not secrets.compare_digest(digest, hashlib.sha256(data).hexdigest()):
        return None
    return writer


def _drop_at(dir_fd: int, name: str) -> None:
    """Unlink `name` (a symlink is unlinked, never followed); absent is fine, and costs no unlink."""
    try:
        os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        os.unlink(name, dir_fd=dir_fd)
    except FileNotFoundError:
        pass


# Every filesystem step below is fd-relative and refuses a symlink. The tree
# is on the agent's data volume, which the platform-agent container beside
# this one writes as the same uid, so a path checked and then used could be a
# symlink by the time it is used. Each directory is opened from its parent's
# fd with O_NOFOLLOW, each file is opened, created, renamed and unlinked by
# name relative to its directory's fd, and modes are set with fchmod on the
# open fd. A symlink anywhere on the path, the root's own path included, is
# answered 409 and never followed.
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
# O_NONBLOCK: a FIFO planted under a name this opens would otherwise block the
# open until a writer came, holding a worker thread for good. The fstat check
# after it refuses anything that is not a regular file.
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
# A directory with more entries than this is not one this store wrote (a
# ring holds REPORT_HISTORY); refused rather than listed and sorted whole.
MAX_DIR_ENTRIES = 4096
_CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
_SYMLINK_ERRNOS = (errno.ELOOP, errno.ENOTDIR, errno.EMLINK)


class _Missing(Exception):
    """A directory on the path does not exist (and was not to be created)."""


def _symlink_refusal(where: str) -> Refused:
    LOGGER.warning("refused a symlink or non-directory on the report store path at %s", _sanitize(where))
    return Refused(409, "a symlink or a non-directory is on the store path")


def _open_child_dir(parent_fd: int, name: str, create: bool) -> int:
    try:
        return os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        if not create:
            raise _Missing(name) from None
    except OSError as exc:
        if exc.errno in _SYMLINK_ERRNOS:
            raise _symlink_refusal(name) from None
        if exc.errno == errno.ENAMETOOLONG:
            # A backstop: store_dirs_for_key bounds what a request can name.
            raise Refused(400, "a store path component is too long") from None
        raise
    try:
        os.mkdir(name, REPORT_DIR_MODE, dir_fd=parent_fd)
        created = True
    except FileExistsError:
        created = False
    except OSError as exc:
        if exc.errno == errno.ENAMETOOLONG:
            raise Refused(400, "a store path component is too long") from None
        raise
    try:
        fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        if exc.errno in _SYMLINK_ERRNOS:
            raise _symlink_refusal(name) from None
        raise
    if created:
        os.fchmod(fd, REPORT_DIR_MODE)
    return fd


class _DirChain:
    """The directories root/<parts...>, opened fd by fd; `fd` is the last."""

    def __init__(self, root: str, parts: list[str], create: bool) -> None:
        self._fds: list[int] = []
        components = [part for part in os.path.abspath(root).split(os.sep) if part] + parts
        try:
            self._fds.append(os.open(os.sep, _DIR_FLAGS & ~os.O_NOFOLLOW))
            for name in components:
                self._fds.append(_open_child_dir(self._fds[-1], name, create))
        except BaseException:
            self.close()
            raise

    @property
    def fd(self) -> int:
        return self._fds[-1]

    def close(self) -> None:
        for fd in reversed(self._fds):
            os.close(fd)
        self._fds = []

    def __enter__(self) -> "_DirChain":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _read_at(dir_fd: int, name: str) -> bytes:
    """A regular file's bytes, opened without following a symlink. 404 if absent."""
    try:
        fd = os.open(name, _READ_FLAGS, dir_fd=dir_fd)
    except FileNotFoundError:
        raise Refused(404, "not found") from None
    except OSError as exc:
        if exc.errno in _SYMLINK_ERRNOS:
            raise _symlink_refusal(name) from None
        raise
    # Checked on the raw fd, before fdopen: fdopen refuses a directory itself
    # (IsADirectoryError, a 500), and the fd must close on every path.
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise _symlink_refusal(name)
        chunks = []
        remaining = REPORT_MAX_BYTES + 1
        while remaining > 0:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
    finally:
        os.close(fd)
    if len(data) > REPORT_MAX_BYTES:
        raise Refused(409, f"a stored entry is over {REPORT_MAX_BYTES} bytes")
    return data


def _write_at(dir_fd: int, name: str, data: bytes) -> None:
    """Replace `name` in one step from a temp file created in the same directory."""
    tmp = f".{name}.{secrets.token_hex(8)}.tmp"
    fd = os.open(tmp, _CREATE_FLAGS, REPORT_FILE_MODE, dir_fd=dir_fd)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            os.fchmod(handle.fileno(), REPORT_FILE_MODE)
        # rename(2) replaces a symlink at `name` itself; it never follows one.
        os.replace(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except BaseException:
        try:
            os.unlink(tmp, dir_fd=dir_fd)
        except FileNotFoundError:
            pass
        raise


def _listdir(dir_fd: int) -> list[str]:
    names = []
    with os.scandir(dir_fd) as entries:
        for entry in entries:
            names.append(entry.name)
            if len(names) > MAX_DIR_ENTRIES:
                raise Refused(409, "a store directory holds more entries than the store writes")
    return names


def _ring_names(runs_fd: int) -> list[str]:
    """The ring's newest REPORT_HISTORY entries, oldest first: regular files named by a valid stamp."""
    return _all_ring_names(runs_fd)[-REPORT_HISTORY:]


def _all_ring_names(runs_fd: int) -> list[str]:
    """Every ring entry, oldest first, for the prune."""
    names = []
    for name in _listdir(runs_fd):
        if not (name.endswith(".json") and STAMP_RE.match(name[: -len(".json")])):
            continue
        try:
            if stat.S_ISREG(os.stat(name, dir_fd=runs_fd, follow_symlinks=False).st_mode):
                names.append(name)
        except FileNotFoundError:
            continue
    return sorted(names)


def _ring_match(runs_fd: int, data: bytes) -> str | None:
    """The newest ring entry byte-identical to `data`, or None, stopping at the first match.

    An entry that cannot be read (over REPORT_MAX_BYTES, which the file
    store's writer does not cap, gone, or no longer a regular file) is a
    non-match, never an error: one bad old entry must not refuse every PUT
    latest until it ages out. `data` is at most REPORT_MAX_BYTES, so it can
    equal no oversized entry anyway.
    """
    for entry in reversed(_ring_names(runs_fd)):
        try:
            if _read_at(runs_fd, entry) == data:
                return entry
        except (Refused, OSError):
            continue
    return None


def _ring_of(root: str, parts: list[str]) -> list[str]:
    try:
        with _DirChain(root, parts + [REPORT_RUNS_DIR], create=False) as runs:
            return _ring_names(runs.fd)
    except _Missing:
        return []


def _validated_body(body: bytes) -> bytes:
    if len(body) > REPORT_MAX_BYTES:
        raise Refused(413, f"the body is over {REPORT_MAX_BYTES} bytes")
    try:
        document = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise Refused(400, "the body is not JSON") from None
    if not isinstance(document, dict):
        raise Refused(400, "the body is not a JSON object")
    return body


def _child_dirs(dir_fd: int) -> list[str]:
    """The real (not symlinked) subdirectories of a directory, sorted."""
    names = []
    for name in _listdir(dir_fd):
        try:
            if stat.S_ISDIR(os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode):
                names.append(name)
        except FileNotFoundError:
            continue
    return sorted(names)


def list_streams(root: str) -> list[dict]:
    streams = []
    try:
        top = _DirChain(root, [], create=False)
    except _Missing:
        return []
    with top:
        audits = [name for name in _child_dirs(top.fd) if AUDIT_ID_RE.match(name)]
    for audit in audits:
        try:
            with _DirChain(root, [audit], create=False) as chain:
                owners = _child_dirs(chain.fd)
        except (_Missing, Refused):
            continue
        for owner in owners:
            try:
                with _DirChain(root, [audit, owner], create=False) as chain:
                    stores = _child_dirs(chain.fd)
            except (_Missing, Refused):
                continue
            for name in stores:
                key = store_key_for_dirs(owner, name)
                try:
                    if store_dirs_for_key(key) != (owner, name):
                        continue
                    ring = _ring_of(root, [audit, owner, name])
                except Refused:
                    continue
                finished = None
                try:
                    with _DirChain(root, [audit, owner, name], create=False) as chain:
                        latest = json.loads(_read_at(chain.fd, REPORT_LATEST_NAME))
                    if isinstance(latest, dict) and isinstance(latest.get("finished_at"), str):
                        finished = latest["finished_at"]
                except (_Missing, Refused, OSError, ValueError, RecursionError):
                    pass
                row = {
                    "audit_id": audit,
                    "key": key,
                    "newest_run": ring[-1][: -len(".json")] if ring else None,
                    "latest_finished_at": finished,
                }
                newest = _newest_run_start_of(root, [audit, owner, name])
                if newest is not None:
                    row["last_run_start"] = newest
                streams.append(row)
    return streams


def _newest_run_start_of(root: str, parts: list[str]) -> dict | None:
    """The stream's newest run-start record as a listing carries it, or None.

    None too when the records cannot be read: the listing is of reports, and a
    record it could not read costs only the note a reader would add.
    """
    try:
        with _DirChain(root, parts + [RUN_STARTS_DIR], create=False) as starts:
            records, _ = _run_start_records(starts.fd)
    except (_Missing, Refused, OSError):
        return None
    if not records:
        return None
    record = records[-1][3]
    listed: dict = {}
    for name in RUN_START_LISTED_KEYS:
        value = record.get(name)
        if name == "scheduled":
            if isinstance(value, bool):
                listed[name] = value
        elif isinstance(value, str) and len(value) <= RUN_START_OUTCOME_MAX_CHARS:
            listed[name] = value
    return listed


def _record_time(value: object) -> float | None:
    """An ISO 8601 time with a zone as an epoch, or None for anything else."""
    if not isinstance(value, str) or len(value) > RUN_START_OUTCOME_MAX_CHARS:
        return None
    try:
        when = datetime.fromisoformat(value)
    except ValueError:
        return None
    if when.tzinfo is None:
        return None
    try:
        return when.timestamp()
    except (OverflowError, OSError, ValueError):
        return None


def _validated_run_start(body: bytes, run_id: str) -> dict:
    """A run-start record's body as an object, once it is one (the module docstring).

    Any `received_at` it sends is dropped: only this listener sets it.
    """
    if len(body) > RUN_START_MAX_BYTES:
        raise Refused(413, f"a run-start record is over {RUN_START_MAX_BYTES} bytes")
    try:
        record = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise Refused(400, "the body is not JSON") from None
    if not isinstance(record, dict):
        raise Refused(400, "the body is not a JSON object")
    if record.get("run_id") != run_id:
        raise Refused(400, "the run-start record names another run")
    started = _record_time(record.get("started_at"))
    if started is None:
        # A done mark with no start of its own is no record of a run.
        raise Refused(400, "the run-start record has no started_at")
    if started > _now() + RUN_START_MAX_FUTURE_SECONDS:
        raise Refused(400, "the run-start record is stamped in the future")
    if "scheduled" in record and not isinstance(record["scheduled"], bool):
        raise Refused(400, "the run-start record's scheduled is not a boolean")
    if record.get("done_at") is not None and _record_time(record.get("done_at")) is None:
        raise Refused(400, "the run-start record's done_at is not a time")
    outcome = record.get("outcome")
    if outcome is not None and (not isinstance(outcome, str) or len(outcome) > RUN_START_OUTCOME_MAX_CHARS):
        raise Refused(400, "the run-start record's outcome is not a short string")
    record.pop(RUN_START_RECEIVED_KEY, None)
    return record


def _received_order(record: dict, started: float) -> tuple[int, float]:
    """A record's place in the order: by this listener's `received_at`; a record
    with none (written before it existed) below all that have one, by `started_at`."""
    received = _record_time(record.get(RUN_START_RECEIVED_KEY))
    return (1, received) if received is not None else (0, started)


def _stamped_run_start(starts_fd: int, name: str, record: dict) -> bytes:
    """`record` with its `received_at`: the run's first record's, when one is
    there, else this clock's now. A done mark never moves a record to the top."""
    received = None
    try:
        existing = json.loads(_read_at(starts_fd, name).decode("utf-8"))
    except (Refused, UnicodeDecodeError, ValueError, RecursionError):
        existing = None
    if isinstance(existing, dict) and existing.get("run_id") == record.get("run_id"):
        kept = existing.get(RUN_START_RECEIVED_KEY)
        if _record_time(kept) is not None:
            received = kept
    if received is None:
        received = datetime.fromtimestamp(_now(), timezone.utc).isoformat(timespec="microseconds")
    data = json.dumps({**record, RUN_START_RECEIVED_KEY: received}, sort_keys=True).encode("utf-8")
    if len(data) > RUN_START_MAX_BYTES:
        raise Refused(413, f"a run-start record is over {RUN_START_MAX_BYTES} bytes")
    return data


def _run_start_records(starts_fd: int) -> tuple[list[tuple[tuple[int, float], str, str, dict]], list[str]]:
    """The readable records, oldest first by `received_at` then run id, and the names that are not.

    Each record as (order, run id, file name, record), the order _received_order's. A file named for a run
    that does not read back as that run's record (another run's id inside, no
    `started_at`, over RUN_START_MAX_BYTES, not a regular file) is in the
    second list, for the prune; anything not named `<run id>.json` (a temp
    file mid-write) is in neither.
    """
    records = []
    invalid = []
    for name in _listdir(starts_fd):
        run_id = name[: -len(".json")] if name.endswith(".json") else ""
        if not RUN_ID_RE.match(run_id):
            continue
        try:
            data = _read_at(starts_fd, name)
            record = json.loads(data.decode("utf-8")) if len(data) <= RUN_START_MAX_BYTES else None
        except Refused as exc:
            if exc.status == 404:
                continue
            record = None
        except (UnicodeDecodeError, ValueError, RecursionError):
            record = None
        started = (
            _record_time(record.get("started_at"))
            if isinstance(record, dict) and record.get("run_id") == run_id
            else None
        )
        if started is None:
            invalid.append(name)
            continue
        records.append((_received_order(record, started), run_id, name, record))
    records.sort(key=lambda entry: (entry[0], entry[1]))
    return records, invalid


def _prune_run_starts(starts_fd: int) -> None:
    """Keep the newest RUN_START_HISTORY records; drop every file named for a run that is not one."""
    records, invalid = _run_start_records(starts_fd)
    for name in invalid + [entry[2] for entry in records[:-RUN_START_HISTORY]]:
        _drop_at(starts_fd, name)


@contextlib.contextmanager
def _locked(dir_fd: int):
    """An exclusive flock on a store directory, so two writers to one store
    cannot interleave a check with a write (PUT latest) or a write with a prune
    (PUT run)."""
    fcntl.flock(dir_fd, fcntl.LOCK_EX)
    try:
        yield
    finally:
        fcntl.flock(dir_fd, fcntl.LOCK_UN)


def handle(
    root: str, method: str, segments: list[str], body: bytes, writer: str | None = None
) -> tuple[int, bytes | list | None, str | None]:
    """One authenticated request, after the raw path was split.

    `segments` is the raw path below AUDIT_REPORTS_PREFIX; `writer` is the
    request's REPORT_WRITER_HEADER, None when absent. Returns the status,
    either raw JSON bytes, a value to encode, or None for no body, and the
    stored writer to answer in REPORT_WRITER_HEADER (None for none).
    """
    if not segments:
        if method != "GET":
            raise Refused(405, "method not allowed")
        return 200, list_streams(root), None
    if len(segments) < 3:
        raise Refused(404, "no such route")
    audit_id, key, *rest = segments
    if rest not in (["latest"], ["runs"]) and not (len(rest) == 2 and rest[0] == "runs"):
        raise Refused(404, "no such route")
    owner, name = store_dirs_for_key(key)
    store = [_validate_audit_id(audit_id), owner, name]
    try:
        return _handle_store(root, method, audit_id, store, rest, body, writer)
    except _Missing:
        raise Refused(404, "not found") from None


def _handle_store(
    root: str, method: str, audit_id: str, store: list[str], rest: list[str], body: bytes, writer: str | None
) -> tuple[int, bytes | list | None, str | None]:
    if rest == ["latest"]:
        if method == "GET":
            # Under the store's lock, so a write does not land between the
            # two reads; the hash binding is what makes a pair never mismatched.
            with _DirChain(root, store, create=False) as chain, _locked(chain.fd):
                data = _read_at(chain.fd, REPORT_LATEST_NAME)
                return 200, data, _bound_writer(chain.fd, REPORT_WRITER_LATEST_NAME, data)
        if method == "PUT":
            data = _validated_body(body)
            try:
                chain = _DirChain(root, store, create=False)
            except _Missing:
                raise Refused(409, "latest must be a copy of a run in the ring") from None
            with chain, _locked(chain.fd):
                try:
                    runs_fd = _open_child_dir(chain.fd, REPORT_RUNS_DIR, create=False)
                except _Missing:
                    raise Refused(409, "latest must be a copy of a run in the ring") from None
                try:
                    entry = _ring_match(runs_fd, data)
                    if entry is None:
                        raise Refused(409, "latest must be a copy of a run in the ring")
                    # Byte-identical to the entry, so the entry's sidecar binds it too.
                    copied = _bound_writer(runs_fd, entry[: -len(".json")] + REPORT_WRITER_SUFFIX, data)
                finally:
                    os.close(runs_fd)
                # The old writer goes first, so a failure below leaves latest
                # with no writer, never with another envelope's.
                _drop_at(chain.fd, REPORT_WRITER_LATEST_NAME)
                _write_at(chain.fd, REPORT_LATEST_NAME, data)
                if copied is not None:
                    _write_at(chain.fd, REPORT_WRITER_LATEST_NAME, _writer_sidecar(copied, data))
            return 204, None, None
        if method == "DELETE":
            try:
                with _DirChain(root, store, create=False) as chain, _locked(chain.fd):
                    _drop_at(chain.fd, REPORT_WRITER_LATEST_NAME)
                    os.unlink(REPORT_LATEST_NAME, dir_fd=chain.fd)
            except (_Missing, FileNotFoundError):
                pass
            return 204, None, None
        raise Refused(405, "method not allowed")
    if rest == ["runs"]:
        if method != "GET":
            raise Refused(405, "method not allowed")
        return 200, [entry[: -len(".json")] for entry in reversed(_ring_of(root, store))], None
    run_id = _run_start_id(rest[1])
    if run_id is not None:
        return _handle_run_start(root, method, audit_id, store, run_id, body, writer)
    stamp = _validate_stamp(rest[1])
    if method == "GET":
        with _DirChain(root, store, create=False) as chain, _locked(chain.fd):
            runs_fd = _open_child_dir(chain.fd, REPORT_RUNS_DIR, create=False)
            try:
                data = _read_at(runs_fd, f"{stamp}.json")
                return 200, data, _bound_writer(runs_fd, stamp + REPORT_WRITER_SUFFIX, data)
            finally:
                os.close(runs_fd)
    if method == "PUT":
        _refuse_a_future_stamp(stamp)
        stamp_writer = _validated_writer(writer, audit_id)
        data = _validated_body(body)
        sidecar = _writer_sidecar(stamp_writer, data) if stamp_writer is not None else None
        with _DirChain(root, store, create=True) as chain, _locked(chain.fd):
            runs_fd = _open_child_dir(chain.fd, REPORT_RUNS_DIR, create=True)
            try:
                # The old writer goes first, so a failure below leaves the
                # entry with no writer, never with the one it replaced.
                _drop_at(runs_fd, stamp + REPORT_WRITER_SUFFIX)
                _write_at(runs_fd, f"{stamp}.json", data)
                if sidecar is not None:
                    _write_at(runs_fd, stamp + REPORT_WRITER_SUFFIX, sidecar)
                try:
                    _prune(runs_fd)
                except Exception as exc:  # noqa: BLE001 - the entry is written; a long ring costs disk only
                    LOGGER.warning("report store prune failed for %s: %s", audit_id, exc)
            finally:
                os.close(runs_fd)
        return 204, None, None
    raise Refused(405, "method not allowed")


def _run_start_id(name: str) -> str | None:
    """The run id a `started-<run>` name carries, or None for any other name."""
    match = RUN_START_NAME_RE.match(name)
    return match.group("run") if match else None


def _handle_run_start(
    root: str, method: str, audit_id: str, store: list[str], run_id: str, body: bytes, writer: str | None
) -> tuple[int, bytes | list | None, str | None]:
    """GET or PUT one run-start record, `run-starts/<run>.json` (the module docstring)."""
    name = f"{run_id}.json"
    if method == "GET":
        with _DirChain(root, store, create=False) as chain, _locked(chain.fd):
            starts_fd = _open_child_dir(chain.fd, RUN_STARTS_DIR, create=False)
            try:
                return 200, _read_at(starts_fd, name), None
            finally:
                os.close(starts_fd)
    if method == "PUT":
        # Checked as on a ring entry's PUT, so a malformed stamp is refused
        # alike; not kept, since the record is no report.
        _validated_writer(writer, audit_id)
        data = _validated_run_start(body, run_id)
        # The stream's store must exist already: a record never makes one. A
        # store that exists is one a run published to, and a first run's
        # record would make the next run read a lost memory. So a stream's
        # first run is answered 404, as GET is for a missing store, untracked.
        with _DirChain(root, store, create=False) as chain, _locked(chain.fd):
            starts_fd = _open_child_dir(chain.fd, RUN_STARTS_DIR, create=True)
            try:
                # Under the store's lock, so two writes of one run's record
                # cannot both find none and both stamp now.
                _write_at(starts_fd, name, _stamped_run_start(starts_fd, name, data))
                try:
                    _prune_run_starts(starts_fd)
                except Exception as exc:  # noqa: BLE001 - the record is written; extra records cost disk only
                    LOGGER.warning("run-start prune failed for %s: %s", audit_id, exc)
            finally:
                os.close(starts_fd)
        return 204, None, None
    raise Refused(405, "method not allowed")


def _prune(runs_fd: int) -> None:
    """Drop the ring's entries past REPORT_HISTORY, each with its writer sidecar.

    A sidecar whose envelope is no longer kept (one a listener that keeps no
    sidecars pruned, after a rollback) goes too.
    """
    names = _all_ring_names(runs_fd)
    for stale in names[:-REPORT_HISTORY]:
        os.unlink(stale, dir_fd=runs_fd)
        _drop_at(runs_fd, stale[: -len(".json")] + REPORT_WRITER_SUFFIX)
    kept = set(names[-REPORT_HISTORY:])
    for name in _listdir(runs_fd):
        stem = name[: -len(REPORT_WRITER_SUFFIX)]
        if name.endswith(REPORT_WRITER_SUFFIX) and STAMP_RE.match(stem) and f"{stem}.json" not in kept:
            _drop_at(runs_fd, name)


def _raw_segments(raw_path: bytes) -> list[str] | None:
    """The raw path below AUDIT_REPORTS_PREFIX as segments, or None off it."""
    try:
        path = raw_path.decode("ascii")
    except UnicodeDecodeError:
        return None
    path = path.split("?", 1)[0]
    if path == AUDIT_REPORTS_PREFIX:
        return []
    if not path.startswith(AUDIT_REPORTS_PREFIX + "/"):
        return None
    segments = path[len(AUDIT_REPORTS_PREFIX) + 1 :].split("/")
    if any(segment == "" for segment in segments):
        return None
    return segments


def _sanitize(text: str, limit: int = 200) -> str:
    return "".join(ch if 0x20 <= ord(ch) < 0x7F else "?" for ch in text)[:limit]


async def _read_body(request: Request) -> bytes:
    length = request.headers.get("content-length")
    if length is not None:
        try:
            if int(length) > REPORT_MAX_BYTES:
                raise Refused(413, f"the body is over {REPORT_MAX_BYTES} bytes")
        except ValueError:
            raise Refused(400, "bad Content-Length") from None
    chunks = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > REPORT_MAX_BYTES:
            raise Refused(413, f"the body is over {REPORT_MAX_BYTES} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


def create_app(verifier: TokenVerifier | None = None, root: str = DEFAULT_REPORTS_DIR) -> FastAPI:
    """The listener.

    No routes: one middleware answers every request, so nothing else is
    served and nothing (a path the router's patterns cannot match, a newline
    in it) reaches a default handler ahead of the auth check. `root` and the
    verifier's fetch are parameters for the tests; production takes the
    defaults, which no environment variable moves.
    """
    application = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    application.state.verifier = verifier or TokenVerifier()
    application.state.root = root

    @application.middleware("http")
    async def store(request: Request, _call_next) -> Response:
        raw_path = request.scope.get("raw_path") or request.url.path.encode("ascii", "replace")
        route = _sanitize(raw_path.decode("ascii", "replace"))
        subject = "-"
        try:
            subject = await run_in_threadpool(
                request.app.state.verifier.verify, request.headers.get("authorization")
            )
            segments = _raw_segments(raw_path)
            if segments is None:
                raise Refused(404, "no such route")
            if request.method not in ("GET", "PUT", "DELETE"):
                raise Refused(405, "method not allowed")
            body = await _read_body(request) if request.method == "PUT" else b""
            writers = request.headers.getlist(REPORT_WRITER_HEADER)
            if len(writers) > 1:
                raise Refused(400, f"more than one {REPORT_WRITER_HEADER} header")
            status, payload, stored = await run_in_threadpool(
                handle, request.app.state.root, request.method, segments, body, writers[0] if writers else None
            )
        except AuthRefused as exc:
            LOGGER.info("%s %s -> %d (%s) subject=%s", request.method, route, exc.status, exc.reason, subject)
            return JSONResponse({"error": exc.reason}, status_code=exc.status)
        except Refused as exc:
            LOGGER.info("%s %s -> %d (%s) subject=%s", request.method, route, exc.status, exc.reason, subject)
            return JSONResponse({"error": exc.reason}, status_code=exc.status)
        except OSError as exc:
            LOGGER.warning("%s %s -> 500 (%s) subject=%s", request.method, route, exc, subject)
            return JSONResponse({"error": "the store could not be read or written"}, status_code=500)
        LOGGER.info("%s %s -> %d subject=%s", request.method, route, status, subject)
        if payload is None:
            return Response(status_code=status)
        if isinstance(payload, bytes):
            headers = {REPORT_WRITER_HEADER: stored} if stored is not None else None
            return Response(content=payload, status_code=status, media_type="application/json", headers=headers)
        return JSONResponse(payload, status_code=status)

    return application


app = create_app()


def bind_listener(port: int = LISTEN_PORT) -> socket.socket:
    """The listening socket, or a loud ERROR and SystemExit when the port is taken.

    The platform-agent container shares this pod's network namespace, so a
    process there could bind the port first. uvicorn's own message for that is
    one errno line among its startup logs; this one names the port and says
    what it means, because a squatter on it would be answering the broker.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("0.0.0.0", port))
    except OSError as exc:
        sock.close()
        if exc.errno == errno.EADDRINUSE:
            LOGGER.error(
                "ALERT the audit-report store cannot bind port %d: another process in this pod already holds it, "
                "so whatever answers the broker there is not this listener",
                port,
            )
            raise SystemExit(1) from None
        raise
    sock.listen(128)
    return sock


def main() -> None:
    import uvicorn

    sock = bind_listener()
    server = uvicorn.Server(uvicorn.Config(app, access_log=False, log_level="info"))
    server.run(sockets=[sock])


if __name__ == "__main__":
    main()
