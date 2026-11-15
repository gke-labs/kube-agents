#!/usr/bin/env python3
"""The audit-report store's broker-only listener (#5619), next mode only.

The fleet-audit report store (docs/designs/fleet-audit-report-store.md) is a
tree on the agent's PVC that `audit_report.py finish` writes in place. Under
`mode: next` a profile worker runs in a pod of its own with no PVC, so it
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
(ALLOWED_SUBJECT_ENV). The NetworkPolicy `<agent>-report-store` admits only
the broker's pods on the port; this check holds where that policy is not
enforced.

Routes (the store key is ONE path segment: the file store's repository
directory with its `/` escaped as `%2F`; a non-GitHub `host/<path with %2F>`
escaped again, so `gitlab.com%2Fgroup%252Frepo`):

    GET    /v1/audit-reports                               the streams
    GET    /v1/audit-reports/<audit>/<key>/latest          latest.json
    GET    /v1/audit-reports/<audit>/<key>/runs            run names, newest first
    GET    /v1/audit-reports/<audit>/<key>/runs/<stamp>    one ring entry
    PUT    /v1/audit-reports/<audit>/<key>/runs/<stamp>    write a ring entry, prune
    PUT    /v1/audit-reports/<audit>/<key>/latest          copy a ring entry to latest
    DELETE /v1/audit-reports/<audit>/<key>/latest          drop latest (idempotent)

The path is read from the raw request path, not the decoded one, because the
key's `%2F` is what keeps it one segment. The token is never logged.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import re
import ssl
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
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

# The tree audit_report.py writes (its REPORTS_DIR), re-read per request as
# that module's writer and readers do.
REPORTS_DIR_ENV = "FLEET_AUDIT_REPORTS_DIR"
DEFAULT_REPORTS_DIR = "/opt/data/fleet-audit/reports"
# These mirror audit_report.py's constants of the same names. That module is a
# skill script and is not importable from here; test_audit_report_store.py
# holds the two copies equal.
REPORT_HISTORY = 14
REPORT_STAMP_FORMAT = "%Y%m%dT%H%M%S.%fZ"
REPORT_RUNS_DIR = "runs"
REPORT_LATEST_NAME = "latest.json"
REPORT_DIR_MODE = 0o755
REPORT_FILE_MODE = 0o644
REPORT_REPO_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.-]+\Z")
STORE_PATH_SEPARATOR = "%2F"
# One envelope. audit_report.py keeps the ledger body near 1 MB across the
# ring (MAX_BODY_CHARS is 64 KiB an envelope) and the findings document is
# unbounded there; here a single envelope is capped at 1 MiB, and anything
# larger is answered 413 rather than written.
REPORT_MAX_BYTES = 1 << 20

AUDIT_REPORTS_PREFIX = "/v1/audit-reports"
AUDIT_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
STAMP_RE = re.compile(r"^[0-9]{8}T[0-9]{6}\.[0-9]{6}Z\Z")
_SEPARATOR_RE = re.compile(re.escape(STORE_PATH_SEPARATOR), re.IGNORECASE)

# The broker presents a token bound to this audience and to nothing else, so
# a token minted for any other listener does not pass here, and this one
# passes nowhere else. The operator renders the same string
# (auditReportStoreAudience in k8s-operator).
AUDIENCE = "kubeagents-audit-report-store"
ALLOWED_SUBJECT_ENV = "AUDIT_REPORT_STORE_ALLOWED_SUBJECT"
API_SERVER_ENV = "AUDIT_REPORT_STORE_API_SERVER"
DEFAULT_API_SERVER = "https://kubernetes.default.svc"
# The pod's own API-audience token and the cluster CA: what lets it read the
# discovery document and the JWKS.
SA_DIR_ENV = "AUDIT_REPORT_STORE_SA_DIR"
DEFAULT_SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
DISCOVERY_PATH = "/.well-known/openid-configuration"
JWKS_PATH = "/openid/v1/jwks"
JWKS_TTL_SECONDS = 300.0
# A token naming a kid the cache lacks refetches, at most this often, so a
# caller sending random kids cannot turn each request into an API call.
JWKS_FORCED_REFRESH_SECONDS = 10.0
JWKS_FETCH_TIMEOUT_SECONDS = 5.0
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
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
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
    except ValueError as exc:
        raise JWKSUnavailable(f"GET {url}: not JSON") from exc
    if not isinstance(document, dict):
        raise JWKSUnavailable(f"GET {url}: not a JSON object")
    return document


def fetch_cluster_keys() -> tuple[str, list[dict]]:
    """The issuer and the signing keys, read from the API server.

    Authenticated with the pod's own token, read fresh each time because the
    kubelet rotates it.
    """
    base = (os.environ.get(API_SERVER_ENV) or DEFAULT_API_SERVER).rstrip("/")
    sa_dir = Path(os.environ.get(SA_DIR_ENV) or DEFAULT_SA_DIR)
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

    def _refresh_locked(self) -> None:
        issuer, keys = self._fetch()
        self._issuer = issuer
        self._keys = {key["kid"]: key for key in keys if isinstance(key.get("kid"), str)}
        self._fetched_at = self._monotonic()

    def _keyset(self, kid: str) -> tuple[str, dict | None]:
        """The issuer and the key named `kid`, refetching as the cache needs.

        Raises JWKSUnavailable when a fetch it needs fails: an expired cache
        is not trusted past its TTL.
        """
        with self._lock:
            now = self._monotonic()
            if self._fetched_at is None or now - self._fetched_at >= JWKS_TTL_SECONDS:
                self._refresh_locked()
            elif kid not in self._keys and (
                self._forced_at is None or now - self._forced_at >= JWKS_FORCED_REFRESH_SECONDS
            ):
                # An unknown kid is the API server rotating its signing key:
                # refetch once, not once per request.
                self._forced_at = now
                self._refresh_locked()
            assert self._issuer is not None
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
        except (ValueError, binascii.Error, UnicodeDecodeError):
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
        return subject


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


def reports_root() -> Path:
    return Path(os.environ.get(REPORTS_DIR_ENV) or DEFAULT_REPORTS_DIR)


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
        return owner, below[0]
    if owner == "github.com" and len(below) == 2:
        return below[0], below[1]
    if "." not in owner:
        raise Refused(400, "a store key below a host names the host first")
    return owner, STORE_PATH_SEPARATOR.join(below)


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


def _inside(root: Path, path: Path) -> bool:
    real_root = os.path.realpath(root)
    real = os.path.realpath(path)
    return real != real_root and os.path.commonpath([real_root, real]) == real_root


def _store_dir(audit_id: str, key: str) -> Path:
    root = reports_root()
    owner, name = store_dirs_for_key(key)
    directory = root / _validate_audit_id(audit_id) / owner / name
    if not _inside(root, directory):
        raise Refused(400, "the store key resolves outside the store")
    return directory


def _checked_file(directory: Path, name: str) -> Path:
    path = directory / name
    if not _inside(reports_root(), path):
        raise Refused(400, "the path resolves outside the store")
    return path


def _make_dirs(directory: Path) -> None:
    root = reports_root()
    if not root.exists():
        root.mkdir(mode=REPORT_DIR_MODE, parents=True, exist_ok=True)
    missing = []
    current = directory
    while not current.exists() and current != root and current != current.parent:
        missing.append(current)
        current = current.parent
    for path in reversed(missing):
        try:
            path.mkdir(mode=REPORT_DIR_MODE)
        except FileExistsError:
            pass
        os.chmod(path, REPORT_DIR_MODE)
    if not _inside(root, directory):
        raise Refused(400, "the store key resolves outside the store")


def _atomic_write(path: Path, data: bytes) -> None:
    handle = tempfile.NamedTemporaryFile("wb", dir=str(path.parent), suffix=".tmp", delete=False)
    try:
        with handle:
            handle.write(data)
        os.chmod(handle.name, REPORT_FILE_MODE)
        os.replace(handle.name, path)
    except BaseException:
        Path(handle.name).unlink(missing_ok=True)
        raise


def _ring(runs: Path) -> list[Path]:
    """The ring's entries, oldest first: files named by a valid stamp."""
    try:
        entries = [p for p in runs.iterdir() if p.suffix == ".json" and STAMP_RE.match(p.stem)]
    except (FileNotFoundError, NotADirectoryError):
        return []
    return sorted(entries, key=lambda p: p.name)


def _read_file(path: Path) -> bytes:
    if not path.is_file():
        raise Refused(404, "not found")
    try:
        return path.read_bytes()
    except FileNotFoundError:
        raise Refused(404, "not found") from None


def _validated_body(body: bytes) -> bytes:
    if len(body) > REPORT_MAX_BYTES:
        raise Refused(413, f"the body is over {REPORT_MAX_BYTES} bytes")
    try:
        document = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise Refused(400, "the body is not JSON") from None
    if not isinstance(document, dict):
        raise Refused(400, "the body is not a JSON object")
    return body


def list_streams() -> list[dict]:
    root = reports_root()
    streams = []
    try:
        audits = sorted(p for p in root.iterdir() if p.is_dir() and not p.is_symlink() and AUDIT_ID_RE.match(p.name))
    except (FileNotFoundError, NotADirectoryError):
        return []
    for audit in audits:
        for owner in sorted(p for p in audit.iterdir() if p.is_dir() and not p.is_symlink()):
            for store in sorted(p for p in owner.iterdir() if p.is_dir() and not p.is_symlink()):
                key = store_key_for_dirs(owner.name, store.name)
                try:
                    if store_dirs_for_key(key) != (owner.name, store.name):
                        continue
                except Refused:
                    continue
                ring = _ring(store / REPORT_RUNS_DIR)
                finished = None
                try:
                    latest = json.loads((store / REPORT_LATEST_NAME).read_text(encoding="utf-8"))
                    if isinstance(latest, dict) and isinstance(latest.get("finished_at"), str):
                        finished = latest["finished_at"]
                except (OSError, ValueError):
                    pass
                streams.append(
                    {
                        "audit_id": audit.name,
                        "key": key,
                        "newest_run": ring[-1].stem if ring else None,
                        "latest_finished_at": finished,
                    }
                )
    return streams


def handle(method: str, segments: list[str], body: bytes) -> tuple[int, bytes | list | None]:
    """One authenticated request, after the raw path was split.

    `segments` is the raw path below AUDIT_REPORTS_PREFIX. Returns the status
    and either raw JSON bytes, a value to encode, or None for no body.
    """
    if not segments:
        if method != "GET":
            raise Refused(405, "method not allowed")
        return 200, list_streams()
    if len(segments) < 3:
        raise Refused(404, "no such route")
    audit_id, key, *rest = segments
    if rest not in (["latest"], ["runs"]) and not (len(rest) == 2 and rest[0] == "runs"):
        raise Refused(404, "no such route")
    directory = _store_dir(audit_id, key)
    runs = directory / REPORT_RUNS_DIR
    if rest == ["latest"]:
        latest = _checked_file(directory, REPORT_LATEST_NAME)
        if method == "GET":
            return 200, _read_file(latest)
        if method == "PUT":
            data = _validated_body(body)
            if not any(_checked_file(runs, entry.name).read_bytes() == data for entry in _ring(runs)):
                raise Refused(409, "latest must be a copy of a run in the ring")
            _atomic_write(latest, data)
            return 204, None
        if method == "DELETE":
            try:
                latest.unlink()
            except FileNotFoundError:
                pass
            return 204, None
        raise Refused(405, "method not allowed")
    if rest == ["runs"]:
        if method != "GET":
            raise Refused(405, "method not allowed")
        return 200, [entry.stem for entry in reversed(_ring(runs))]
    if len(rest) == 2 and rest[0] == "runs":
        stamp = _validate_stamp(rest[1])
        if method == "GET":
            return 200, _read_file(_checked_file(runs, f"{stamp}.json"))
        if method == "PUT":
            data = _validated_body(body)
            _make_dirs(runs)
            _atomic_write(_checked_file(runs, f"{stamp}.json"), data)
            try:
                for stale in _ring(runs)[:-REPORT_HISTORY]:
                    stale.unlink()
            except Exception as exc:  # noqa: BLE001 - the entry is written; a long ring costs disk only
                LOGGER.warning("report store prune failed for %s: %s", audit_id, exc)
            return 204, None
        raise Refused(405, "method not allowed")
    raise Refused(404, "no such route")


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


def create_app(verifier: TokenVerifier | None = None) -> FastAPI:
    """The listener.

    No routes: one middleware answers every request, so nothing else is
    served and nothing (a path the router's patterns cannot match, a newline
    in it) reaches a default handler ahead of the auth check.
    """
    application = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    application.state.verifier = verifier or TokenVerifier()

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
            status, payload = await run_in_threadpool(handle, request.method, segments, body)
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
            return Response(content=payload, status_code=status, media_type="application/json")
        return JSONResponse(payload, status_code=status)

    return application


app = create_app()
