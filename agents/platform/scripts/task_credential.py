#!/usr/bin/env python3
"""The credential proxy's verifier for task credentials (#3889).

The dispatcher signs one credential per task with the task-signing key, in the
format ``a2a/taskcred`` defines (``Mint``, ``Verify``). The auth callout verifies
the ``nats`` audience in Go; this module verifies the ``credential-proxy``
audience in Python, for the broker, which runs on the agent image's interpreter
and cannot import the Go package. So this is a second implementation of
``taskcred.Verify``, and the rule for it is parity or stricter, never laxer:

  * Every input ``taskcred.Verify`` refuses, this refuses. The golden fixtures
    in ``a2a/taskcred/testdata/credential_proxy_fixtures.json`` are minted and
    verified by the Go package (``go test ./taskcred -run
    TestCredentialProxyFixtures -update-proxy-fixtures`` regenerates them) and
    verified again here by ``test_task_credential.py``, so the two verifiers
    are held to one verdict per token.
  * Where this is stricter it says so: ``target`` is required (Go makes it
    optional for this audience, and a proxy credential with no target confines
    nothing), base64url must be canonical, the header must carry exactly
    ``typ`` and ``alg``, and a JSON ``null`` is refused where Go would read the
    zero value. Each of those is a fixture with ``go_ok`` and ``proxy_ok``
    apart.

The credential authenticates nothing by itself. The broker binds it to the pod
the API server attested by TokenReview (pod name and UID) and to that pod's
ServiceAccount's (profile, tier) entry in the operator-rendered identity map;
``bind`` is that check. Nothing here logs the token; callers log its ``jti``.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

# The Ed25519 primitive. The broker's interpreter is the Hermes venv, which
# carries `cryptography`; requirements-test.txt names it for the test sweep.
# Imported at module scope on purpose: credential_proxy imports this module
# only when the profile role is configured (build_authenticator,
# build_profile_gate and ProfileTaskGate), so a broker
# without that role never needs the package, and one with it fails at start
# rather than refusing every task credential with an ImportError.
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

# The audience this verifier is for. taskcred.AudienceCredentialProxy.
AUDIENCE = "credential-proxy"

# taskcred.TokenType: the nats.type claim that says the JWT was minted as a
# task credential rather than as any other jwt/v2 claim set.
TOKEN_TYPE = "kube-agents-task"

# jwt/v2's own nats.version, which GenericClaims.Encode writes into every
# credential (taskcred's jwtClaimsVersion).
JWT_CLAIMS_VERSION = 2

# The v2 header jwt/v2 writes. The v1 algorithm, "ed25519", signs the payload
# segment alone; taskcred refuses it and so does this.
HEADER_ALGORITHM = "ed25519-nkey"
HEADER_TYPE = "JWT"

# taskcred.MaxTrustedKeys: the current key and the one it replaces.
MAX_TRUSTED_KEYS = 2

# The clock skew allowed against the minter, the callout's
# taskCredentialSkew. One value for both verifiers, so a credential the
# callout accepts at one instant the proxy accepts too.
SKEW_SECONDS = 30

# The callout's defaultTaskCredentialMaxLifetime and its ceiling
# (maxTaskCredentialMaxLifetime). A2A_TASK_CREDENTIAL_MAX_LIFETIME is parsed
# exactly as ParseTaskCredentialMaxLifetime parses it.
DEFAULT_MAX_LIFETIME_SECONDS = 6 * 60 * 60
MAX_MAX_LIFETIME_SECONDS = 7 * 24 * 60 * 60

# A bound on what is decoded at all. A minted credential is well under 1 KiB;
# this is the header value's ceiling, checked before any base64.
MAX_TOKEN_BYTES = 8192

# taskcred's shape bounds: maxPodUID, maxTargetField, and lib.ValidSubjectToken's
# 63-byte DNS-1123 label.
MAX_POD_UID = 64
MAX_TARGET_FIELD = 128
MAX_LABEL = 63

# The nkey public-key encoding (github.com/nats-io/nkeys): base32 (standard
# alphabet, no padding) of a prefix byte, the 32-byte Ed25519 key, and a CRC-16
# (XMODEM) of those 33 bytes, little-endian. The task-signing key is a cluster
# key, prefix byte 2<<3, which encodes as a leading "C".
NKEY_PREFIX_CLUSTER = 2 << 3
NKEY_PUBLIC_LENGTH = 56
NKEY_RAW_LENGTH = 35
ED25519_PUBLIC_LENGTH = 32
ED25519_SIGNATURE_LENGTH = 64

# The claim keys a credential may carry, exactly as spelled, at each level:
# taskcred's topLevelClaims, natsClaims and targetClaims. A key outside the set
# is refused at every level (taskcred refuses an unknown nats or target key in
# decodePayload, and a case variant of an allowed one in checkStrictClaims).
TOP_LEVEL_CLAIMS = frozenset({"jti", "iat", "iss", "name", "sub", "aud", "exp", "nbf", "nats"})
NATS_CLAIMS = frozenset({"type", "version", "task", "profile", "tier", "pod", "pod_uid", "target"})
TARGET_CLAIMS = frozenset({"project", "location", "cluster"})
HEADER_KEYS = frozenset({"typ", "alg"})

# int64, the width of jwt/v2's time claims. A JSON integer outside it fails
# Go's decode; Python would read it as a bignum.
_INT64_MIN = -(1 << 63)
_INT64_MAX = (1 << 63) - 1

_BASE64URL = re.compile(r"\A[A-Za-z0-9_-]*\Z")
_BASE32 = re.compile(r"\A[A-Z2-7]*\Z")
_LABEL = re.compile(r"\A[a-z0-9]([a-z0-9-]*[a-z0-9])?\Z")
_POD_UID = re.compile(r"\A[a-z0-9-]+\Z")
# Go's time.ParseDuration grammar: an optional sign, then one or more decimal
# numbers each with a unit. "µs" is spelled both ways Go accepts.
_GO_DURATION_PART = re.compile(r"([0-9]*(?:\.[0-9]*)?)(ns|us|µs|μs|ms|s|m|h)")
_GO_DURATION_UNITS = {
    "ns": 1e-9, "us": 1e-6, "µs": 1e-6, "μs": 1e-6, "ms": 1e-3, "s": 1.0, "m": 60.0, "h": 3600.0,
}


class TaskCredentialError(ValueError):
    """A task credential, or the verifier's configuration, is refused.

    The message never quotes the token. It may name a claim key or a claim
    value the verifier already decoded, which is what makes a refusal
    diagnosable from the broker's log.
    """


@dataclass(frozen=True)
class TaskTarget:
    """The cluster a credential confines its task to (taskcred.Target)."""

    project: str
    location: str
    cluster: str


@dataclass(frozen=True)
class TaskClaims:
    """A verified credential's content (taskcred.Claims)."""

    task: str
    profile: str
    tier: str
    pod: str
    pod_uid: str
    expires: int
    issued_at: int
    jti: str
    issuer: str
    target: TaskTarget


# ---------------------------------------------------------------------------
# Configuration: the trusted keys and the lifetime ceiling.
# ---------------------------------------------------------------------------


def _crc16_xmodem(data: bytes) -> int:
    """nkeys' crc16: polynomial 0x1021, initial value 0, no reflection."""
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return crc


def decode_cluster_public_key(public: str) -> bytes:
    """The 32-byte Ed25519 key a task-signing public key (C...) encodes.

    taskcred.ValidPublicKey (nkeys.IsValidPublicClusterKey), plus a length
    check nkeys leaves to its caller: exactly 56 base32 characters, so exactly
    the 35 bytes of prefix, key and checksum.
    """
    if not isinstance(public, str) or len(public) != NKEY_PUBLIC_LENGTH or not _BASE32.match(public):
        raise TaskCredentialError("not a task-signing public key: wrong length or alphabet")
    try:
        raw = base64.b32decode(public)
    except (binascii.Error, ValueError) as exc:
        raise TaskCredentialError("not a task-signing public key: not base32") from exc
    if len(raw) != NKEY_RAW_LENGTH:
        raise TaskCredentialError("not a task-signing public key: wrong length")
    body, checksum = raw[:-2], raw[-2:]
    if int.from_bytes(checksum, "little") != _crc16_xmodem(body):
        raise TaskCredentialError("not a task-signing public key: checksum mismatch")
    if body[0] != NKEY_PREFIX_CLUSTER:
        raise TaskCredentialError("not a task-signing public key: not a cluster key (C...)")
    return bytes(body[1:])


def parse_trusted_keys(raw: str) -> tuple[str, ...]:
    """A2A_TASK_SIGNING_PUBLIC_KEYS, read as authcallout.ParseTaskSigningKeys reads it.

    Comma separated, whitespace trimmed. Empty means none, and then no task
    credential verifies. An empty element, a key that is not a task-signing
    public key, a repeat, or more than MAX_TRUSTED_KEYS is refused.
    """
    if not raw or not raw.strip():
        return ()
    keys: list[str] = []
    for field in raw.split(","):
        key = field.strip()
        if not key:
            raise TaskCredentialError("the task-signing key list has an empty element")
        decode_cluster_public_key(key)
        if key in keys:
            raise TaskCredentialError("the task-signing key list names one key twice")
        keys.append(key)
    if len(keys) > MAX_TRUSTED_KEYS:
        raise TaskCredentialError(
            f"{len(keys)} task-signing keys; at most {MAX_TRUSTED_KEYS} (the current one and the one it replaces)"
        )
    return tuple(keys)


def parse_go_duration(raw: str) -> float:
    """Seconds in a Go duration string (time.ParseDuration), e.g. "6h0m0s"."""
    text = raw.strip()
    if not text:
        raise TaskCredentialError("an empty duration")
    sign = 1.0
    if text[0] in "+-":
        sign = -1.0 if text[0] == "-" else 1.0
        text = text[1:]
    if text == "0":
        return 0.0
    total = 0.0
    position = 0
    while position < len(text):
        match = _GO_DURATION_PART.match(text, position)
        if match is None or match.group(1) in ("", "."):
            raise TaskCredentialError(f"{raw!r} is not a Go duration")
        total += float(match.group(1)) * _GO_DURATION_UNITS[match.group(2)]
        position = match.end()
    return sign * total


def max_lifetime_from_env(raw: str | None) -> float:
    """A2A_TASK_CREDENTIAL_MAX_LIFETIME, as authcallout.ParseTaskCredentialMaxLifetime.

    Unset or blank is the default. Anything else must be a positive Go duration
    no greater than the ceiling, or it is an error: the caller refuses every
    task credential rather than falling back, because a cap someone set and got
    wrong is not the default cap.
    """
    if raw is None or not raw.strip():
        return float(DEFAULT_MAX_LIFETIME_SECONDS)
    try:
        seconds = parse_go_duration(raw)
    except TaskCredentialError as exc:
        raise TaskCredentialError(f"A2A_TASK_CREDENTIAL_MAX_LIFETIME: {exc}") from exc
    if seconds <= 0:
        raise TaskCredentialError(f"A2A_TASK_CREDENTIAL_MAX_LIFETIME={raw!r} is not positive")
    if seconds > MAX_MAX_LIFETIME_SECONDS:
        raise TaskCredentialError(f"A2A_TASK_CREDENTIAL_MAX_LIFETIME={raw!r} is above the 7-day ceiling")
    return seconds


# ---------------------------------------------------------------------------
# Strict decoding.
# ---------------------------------------------------------------------------


def _b64url(segment: str, what: str) -> bytes:
    """Raw base64url, canonical: no padding, no whitespace, no stray bits.

    Go's RawURLEncoding refuses padding and foreign characters but skips CR and
    LF and accepts non-zero trailing bits; this refuses both, so one credential
    has one spelling.
    """
    if not _BASE64URL.match(segment) or len(segment) % 4 == 1:
        raise TaskCredentialError(f"task credential {what} is not base64url")
    try:
        decoded = base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
    except (binascii.Error, ValueError) as exc:
        raise TaskCredentialError(f"task credential {what} is not base64url") from exc
    if base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii") != segment:
        raise TaskCredentialError(f"task credential {what} is not canonical base64url")
    return decoded


def _refuse_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """object_pairs_hook: a key twice at any level is refused, not last-wins."""
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise TaskCredentialError(f"task credential key {key!r} appears twice")
        out[key] = value
    return out


def _refuse_constant(name: str) -> Any:
    """parse_constant: NaN and Infinity are not JSON, and Go refuses them."""
    raise TaskCredentialError(f"task credential carries the non-JSON constant {name}")


def _strict_json(raw: bytes, what: str) -> dict[str, Any]:
    try:
        text = raw.decode("utf-8")
        value = json.loads(text, object_pairs_hook=_refuse_duplicates, parse_constant=_refuse_constant)
    except TaskCredentialError:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise TaskCredentialError(f"task credential {what} is not JSON") from exc
    if not isinstance(value, dict):
        raise TaskCredentialError(f"task credential {what} is not a JSON object")
    return value


def _exact_keys(obj: Mapping[str, Any], allowed: frozenset[str], required: Iterable[str], where: str) -> None:
    unknown = sorted(set(obj) - allowed)
    if unknown:
        raise TaskCredentialError(f"task credential {where} carries an unknown key {unknown[0]!r}")
    missing = sorted(set(required) - set(obj))
    if missing:
        raise TaskCredentialError(f"task credential {where} has no {missing[0]!r}")


def _string(obj: Mapping[str, Any], key: str, where: str) -> str:
    value = obj[key]
    if type(value) is not str:
        raise TaskCredentialError(f"task credential {where}.{key} is not a string")
    return value


def _integer(obj: Mapping[str, Any], key: str, where: str) -> int:
    # `type(...) is int`, not isinstance: a JSON true is a Python bool, which
    # is an int subclass, and Go refuses a bool where an integer goes.
    value = obj[key]
    if type(value) is not int or not _INT64_MIN <= value <= _INT64_MAX:
        raise TaskCredentialError(f"task credential {where}.{key} is not an integer")
    return value


def _label(value: str, name: str) -> str:
    if len(value) > MAX_LABEL or not _LABEL.match(value):
        raise TaskCredentialError(
            f"task credential {name} {value[:MAX_LABEL]!r} is not a single subject token (a dot-free DNS-1123 label)"
        )
    return value


def _target_field(value: str, name: str) -> str:
    if not value or len(value) > MAX_TARGET_FIELD or any(not ("!" <= c <= "~") for c in value):
        raise TaskCredentialError(f"task credential target {name} is empty, too long or not printable")
    return value


# ---------------------------------------------------------------------------
# Verify.
# ---------------------------------------------------------------------------


def verify(
    token: str,
    trusted: tuple[str, ...] | list[str],
    now: float,
    max_lifetime: float,
    skew: float = SKEW_SECONDS,
) -> TaskClaims:
    """Check a credential-proxy task credential and return its claims.

    taskcred.Verify with aud=AudienceCredentialProxy, check for check; see that
    function for why each one exists. Every failure raises TaskCredentialError
    and returns nothing, and nothing is clamped: a credential asking for more
    than the ceiling is refused, not shortened.
    """
    if not trusted:
        raise TaskCredentialError("no task-signing key is trusted")
    if len(trusted) > MAX_TRUSTED_KEYS:
        raise TaskCredentialError(f"{len(trusted)} task-signing keys are trusted; at most {MAX_TRUSTED_KEYS}")
    keys = {key: decode_cluster_public_key(key) for key in trusted}
    if not max_lifetime > 0 or skew < 0:
        raise TaskCredentialError("a verifier needs a positive maximum lifetime and a non-negative skew")

    if not isinstance(token, str) or not token or len(token) > MAX_TOKEN_BYTES or not token.isascii():
        raise TaskCredentialError("task credential is empty, too long or not ASCII")
    parts = token.split(".")
    if len(parts) != 3:
        raise TaskCredentialError("task credential is not a JWT")
    head_segment, payload_segment, signature_segment = parts

    header = _strict_json(_b64url(head_segment, "header"), "header")
    _exact_keys(header, HEADER_KEYS, HEADER_KEYS, "header")
    alg, typ = header["alg"], header["typ"]
    if type(alg) is not str or type(typ) is not str or alg != HEADER_ALGORITHM or typ.upper() != HEADER_TYPE:
        raise TaskCredentialError(f"task credential header is not {HEADER_TYPE}/{HEADER_ALGORITHM}")

    claims = _strict_json(_b64url(payload_segment, "payload"), "payload")
    _exact_keys(claims, TOP_LEVEL_CLAIMS, ("jti", "iat", "iss", "sub", "aud", "exp", "nats"), "payload")

    # The issuer before the signature: the signature is checked against the
    # key the issuer names, so only a trusted issuer makes it mean anything.
    issuer = _string(claims, "iss", "payload")
    public = keys.get(issuer)
    if public is None:
        raise TaskCredentialError("task credential is signed by a key that is not a trusted task-signing key")
    signature = _b64url(signature_segment, "signature")
    if len(signature) != ED25519_SIGNATURE_LENGTH:
        raise TaskCredentialError("task credential signature has the wrong length")
    try:
        Ed25519PublicKey.from_public_bytes(public).verify(
            signature, f"{head_segment}.{payload_segment}".encode("ascii")
        )
    except InvalidSignature as exc:
        raise TaskCredentialError("task credential signature does not verify") from exc

    nats = claims["nats"]
    if not isinstance(nats, dict):
        raise TaskCredentialError("task credential nats claim is not an object")
    _exact_keys(nats, NATS_CLAIMS, NATS_CLAIMS, "nats")
    if _string(nats, "type", "nats") != TOKEN_TYPE:
        raise TaskCredentialError("task credential type is not a task credential")
    if _integer(nats, "version", "nats") != JWT_CLAIMS_VERSION:
        raise TaskCredentialError(f"task credential claims version is not {JWT_CLAIMS_VERSION}")
    target_claim = nats["target"]
    if not isinstance(target_claim, dict):
        raise TaskCredentialError("task credential target is not an object")
    _exact_keys(target_claim, TARGET_CLAIMS, TARGET_CLAIMS, "nats.target")

    audience = claims["aud"]
    if type(audience) is not str or audience != AUDIENCE:
        raise TaskCredentialError(f"task credential is not for {AUDIENCE!r}")

    if "name" in claims:
        _string(claims, "name", "payload")
    jti = _string(claims, "jti", "payload")
    subject = _string(claims, "sub", "payload")
    exp = _integer(claims, "exp", "payload")
    iat = _integer(claims, "iat", "payload")
    nbf = _integer(claims, "nbf", "payload") if "nbf" in claims else 0

    # Go reads an absent time claim as 0, so 0 is "absent" for both.
    if exp == 0:
        raise TaskCredentialError("task credential has no expiry")
    if not exp > now - skew:
        raise TaskCredentialError("task credential has expired")
    if exp - now > max_lifetime + skew:
        raise TaskCredentialError("task credential expires past the ceiling; refused rather than shortened")
    if iat == 0:
        raise TaskCredentialError("task credential has no issue time")
    if iat > now + skew or iat > exp:
        raise TaskCredentialError("task credential was issued after now or after its expiry")
    if exp - iat > max_lifetime + skew:
        raise TaskCredentialError("task credential spans past the ceiling; refused rather than shortened")
    if nbf != 0 and nbf > now + skew:
        raise TaskCredentialError("task credential is not valid yet")
    if not jti or not subject:
        raise TaskCredentialError("task credential has no jti or no subject")

    pod_uid = _string(nats, "pod_uid", "nats")
    if len(pod_uid) > MAX_POD_UID or not _POD_UID.match(pod_uid):
        raise TaskCredentialError("task credential pod_uid is not a UID")
    return TaskClaims(
        task=_label(_string(nats, "task", "nats"), "task"),
        profile=_label(_string(nats, "profile", "nats"), "profile"),
        tier=_label(_string(nats, "tier", "nats"), "tier"),
        pod=_label(_string(nats, "pod", "nats"), "pod"),
        pod_uid=pod_uid,
        expires=exp,
        issued_at=iat,
        jti=jti,
        issuer=issuer,
        target=TaskTarget(
            project=_target_field(_string(target_claim, "project", "nats.target"), "project"),
            location=_target_field(_string(target_claim, "location", "nats.target"), "location"),
            cluster=_target_field(_string(target_claim, "cluster", "nats.target"), "cluster"),
        ),
    )


# ---------------------------------------------------------------------------
# The identity map and the binding.
# ---------------------------------------------------------------------------

# The TokenReview username of a ServiceAccount, the identity map's key.
_SERVICE_ACCOUNT_USER = re.compile(r"\Asystem:serviceaccount:[a-z0-9]([a-z0-9-]*[a-z0-9])?:[a-z0-9]([a-z0-9.-]*[a-z0-9])?\Z")


@dataclass(frozen=True)
class ProfileIdentity:
    """A profile worker ServiceAccount's (profile, tier), as the operator rendered it."""

    profile: str
    tier: str


def parse_identity_map(raw: str | None) -> dict[str, ProfileIdentity]:
    """CREDENTIAL_PROXY_PROFILE_IDENTITIES: ServiceAccount user to (profile, tier).

    The operator renders it from the AgentProfiles' per-tier ServiceAccounts
    (agentProfileMapEntries), because a ServiceAccount name can be user-set and
    is not something to parse a profile out of. Read strictly, like a
    credential: an object of objects, each with exactly "profile" and "tier",
    both DNS-1123 labels, keyed by a ServiceAccount user. Unset or blank is an
    empty map, which confers the profile role on nobody.
    """
    if raw is None or not raw.strip():
        return {}
    document = _strict_json(raw.encode("utf-8"), "identity map")
    out: dict[str, ProfileIdentity] = {}
    for user, entry in document.items():
        if not _SERVICE_ACCOUNT_USER.match(user):
            raise TaskCredentialError(f"identity map key {user[:128]!r} is not a ServiceAccount user")
        if not isinstance(entry, dict):
            raise TaskCredentialError(f"identity map entry for {user!r} is not an object")
        _exact_keys(entry, frozenset({"profile", "tier"}), ("profile", "tier"), f"identity map entry {user!r}")
        out[user] = ProfileIdentity(
            profile=_label(_string(entry, "profile", user), "identity map profile"),
            tier=_label(_string(entry, "tier", user), "identity map tier"),
        )
    return out


def bind(claims: TaskClaims, *, workload: str, pod: str, pod_uid: str, identities: Mapping[str, ProfileIdentity]) -> None:
    """Refuse a verified credential that is not this caller's.

    ``workload``, ``pod`` and ``pod_uid`` come from the TokenReview, never from
    the request: the ServiceAccount the projected token authenticated, and the
    pod name and UID the API server says it is bound to. A copied credential
    is useless without the pod's own ServiceAccount token, and one minted for
    another profile or tier does not match this ServiceAccount's entry.
    """
    identity = identities.get(workload)
    if identity is None:
        raise TaskCredentialError("the caller's ServiceAccount has no profile identity")
    if not pod or claims.pod != pod:
        raise TaskCredentialError("task credential is for another pod")
    if not pod_uid or claims.pod_uid != pod_uid:
        raise TaskCredentialError("task credential is for another pod UID")
    if claims.profile != identity.profile:
        raise TaskCredentialError("task credential is for another profile")
    if claims.tier != identity.tier:
        raise TaskCredentialError("task credential is for another tier")
