#!/usr/bin/env python3
"""What the forge said, and what to do about it.

The reader of these messages is a model deciding its next tool call, not an
operator reading a log, so each one names the cause and then the action that
follows from it. The distinction matters most where the right action differs
while the symptom does not: a rate limit and a missing scope are both HTTP 403,
and an agent told only "the forge refused it" retries the one that will never
succeed and gives up on the one that would have succeeded in ten seconds.
Collapsing every failure into one status is the same bug wearing a number.

The forge's own first line is kept as `detail` underneath. It says which field
was rejected or that the branch has no commits, which no fixed message can, and
the two are answering different questions.

Two things are deliberately not in this module, and both are places a
single-forge design would have put them.

*Recovering the status.* An HTTP transport has it as an integer; a CLI prints
`(HTTP 404)` into its stderr and something has to dig it out with a regex. That
regex is a property of how the call was made rather than of what the forge
answered, so it belongs to the transport.

*Splitting one status by message text.* At least one forge spends 403 on both
a missing scope and a throttle, and telling those apart means matching markers
in prose the forge wrote. A forge that needs it supplies it through
`error_overrides`; a forge that returns a distinct status for throttling --
most of them do -- inherits nothing it has to opt out of.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Callable, Mapping, Union

from workspace_paths import WorkspaceError


@dataclass(frozen=True)
class Guidance:
    """A refusal's status, its stable symbol, and what to do next."""

    status: int
    code: str
    text: str


# An override is either a fixed reading of a status or a function of the forge's
# own message, for the case where one status carries two meanings. Returning
# `None` from the function falls through to the shared table below, so a forge
# only has to describe the case it disagrees about.
Override = Union[Guidance, Callable[[str], "Guidance | None"]]

__all__ = [
    "GUIDANCE",
    "Guidance",
    "Override",
    "TLS_GUIDANCE",
    "TLS_UNTRUSTED_CODE",
    "UNAVAILABLE",
    "UNRECOGNISED",
    "ca_missing_reason",
    "ca_unloadable_reason",
    "classify_tls",
    "forge_error",
    "tls_refusal",
    "tls_untrusted",
]


GUIDANCE: dict[int, Guidance] = {
    401: Guidance(
        401,
        "FORGE_UNAUTHENTICATED",
        "The forge rejected this install's credential. It has expired or been "
        "revoked. Nothing you can do from here will fix it -- report it and "
        "stop rather than retrying.",
    ),
    403: Guidance(
        403,
        "FORGE_FORBIDDEN",
        "The forge accepted the credential and refused the operation. The "
        "credential is missing the permission this call needs, or the "
        "repository denies it. Retrying will not change the answer; try a "
        "read-only route, or report what you were denied.",
    ),
    404: Guidance(
        404,
        "FORGE_NOT_FOUND",
        "No such repository, revision, or path. A private repository this "
        "install's credential cannot see also answers 404, so this does not "
        "prove the thing does not exist. Check the spelling and the branch "
        "with `files` or `log` before concluding it is missing.",
    ),
    409: Guidance(
        409,
        "FORGE_CONFLICT",
        "The forge says the state changed underneath this call -- something "
        "else moved the branch or the proposal. Re-read it and try again "
        "against what is there now.",
    ),
    422: Guidance(
        422,
        "FORGE_REJECTED",
        "The forge understood the request and rejected its contents. This is "
        "a bad argument, not a transient failure: fix the field named in the "
        "detail below rather than retrying the same call.",
    ),
    429: Guidance(
        429,
        "FORGE_RATE_LIMITED",
        "The forge is rate-limiting this install. Wait before the next call "
        "and prefer one wide request over many narrow ones -- `files` over a "
        "`show` per path. This will succeed later.",
    ),
}

UNAVAILABLE = Guidance(
    503,
    "FORGE_UNAVAILABLE",
    "The forge is having its own problems -- this call failed on its side, not "
    "on anything you sent. Wait a few minutes and retry the same call "
    "unchanged.",
)

UNRECOGNISED = Guidance(
    502,
    "FORGE_CALL_FAILED",
    "The forge did not answer this call and did not say why in a form this "
    "broker recognises. One retry is reasonable; two is not.",
)


# Every TLS failure answers one code, FORGE_TLS_UNTRUSTED: whatever the
# cause, no retry fixes it, and the caller stops and reports. The text names
# the cause, because each one sends the administrator somewhere else.
TLS_UNTRUSTED_CODE = "FORGE_TLS_UNTRUSTED"
TLS_GUIDANCE: dict[str, str] = {
    "untrusted": (
        "The broker could not verify the forge's TLS certificate: it does not "
        "chain to a CA the broker trusts for this host. No retry fixes this. "
        "Report it and stop. For a self-managed forge behind a private CA, an "
        "administrator names that CA in the forge's caBundleRef (install.sh "
        "--gitops-ca-file). For a forge's public host, which does not accept "
        "caBundleRef, something between the broker and the forge, such as a "
        "TLS-inspecting proxy, presents the certificate."
    ),
    "expired": (
        "The forge's TLS certificate has expired, or is not valid yet. No retry "
        "fixes this. Report it and stop: the forge's administrator renews the "
        "certificate, or the broker's clock is wrong."
    ),
    "hostname": (
        "The forge's TLS certificate does not name the host that the broker "
        "called. No retry fixes this. Report it and stop: the certificate, or "
        "the forge's configured host, is wrong."
    ),
    "ca_missing": (
        "The CA bundle that the forge's caBundleRef names is not mounted. No "
        "retry fixes this until it is. Report it and stop: an administrator "
        "creates the Secret, with the key, that caBundleRef names."
    ),
    "ca_unloadable": (
        "The CA bundle that the forge's caBundleRef names could not be loaded: "
        "it is not PEM, or it holds no certificate. No retry fixes this. Report "
        "it and stop: an administrator puts the PEM CA certificate in the "
        "Secret key that caBundleRef names."
    ),
}

# What git (libcurl with GnuTLS or OpenSSL) and Python's ssl module write for
# each cause, lowercased, matched as substrings of one line. The kinds are
# tried in this order: GnuTLS writes "server verification failed: certificate
# has expired", which also holds an "untrusted" marker.
_TLS_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    # A CA file the client could not load. git words a missing file and a
    # malformed one alike (GnuTLS: "Problem with the SSL CA cert"), so
    # `tls_refusal` tells the two apart by whether the forge's file exists.
    ("ca_load", (
        "problem with the ssl ca cert",
        "error setting certificate",
        "could not load ca file",
        "error reading ca cert",
        "error adding trust anchors from file",
    )),
    ("expired", (
        "certificate has expired",
        "certificate is not yet valid",
        "certificate expired",
    )),
    ("hostname", (
        "does not match target hostname",
        "no alternative certificate subject name matches",
        "hostname mismatch",
        "ip address mismatch",
    )),
    ("untrusted", (
        "certificate signer not trusted",
        "unable to get local issuer certificate",
        "self-signed certificate",
        "self signed certificate",
        "certificate verify failed",
        "ssl certificate problem",
        "server verification failed",
        # Older GnuTLS builds (Ubuntu's git) give no reason with it.
        "server certificate verification failed",
        "certificate is not trusted",
    )),
)

_HOST_IN_LINE_RE = re.compile(r"https://([^/'\"\s]+)")


def classify_tls(text: str) -> tuple[str, str]:
    """`(kind, line)` for the first line of `text` that names a TLS failure.

    `kind` is a key of TLS_GUIDANCE, or "" when no line does. Lines that git
    prints from the server (`remote: ...`) are skipped: the forge's own words
    are not the client's verdict on the forge's certificate. Only the matching
    line is answered, cut short, so a caller can show it without the rest of
    an output that may carry a credential.
    """
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("remote:"):
            continue
        lowered = stripped.lower()
        for kind, markers in _TLS_MARKERS:
            if any(marker in lowered for marker in markers):
                return kind, stripped[:200]
    return "", ""


def tls_untrusted(host: str, why: str, kind: str = "untrusted") -> WorkspaceError:
    """The refusal for a TLS failure of `kind`, naming `host`."""
    named = f"{host}: {why}" if host else why
    return WorkspaceError(
        TLS_GUIDANCE.get(kind, TLS_GUIDANCE["untrusted"]),
        status=502,
        code=TLS_UNTRUSTED_CODE,
        detail=named.strip()[:400],
    )


def ca_missing_reason(ca_source: str, fallback: str = "") -> str:
    """Why a forge's CA file is not there, naming its Secret and key if known."""
    if ca_source:
        return f"{ca_source} is missing"
    return fallback or "the CA file that the forge's caBundleRef names is missing"


def ca_unloadable_reason(ca_source: str, error: str = "") -> str:
    """Why a forge's CA file could not be loaded, naming its Secret and key if known."""
    where = f"the CA file from {ca_source.removeprefix('the ')}" if ca_source else "the CA file"
    reason = f"{where} could not be loaded (not PEM, or no certificate in it)"
    return f"{reason}: {error}" if error else reason


def tls_refusal(
    text: str,
    ca_sources: Mapping[str, str] | None = None,
    ca_files: Mapping[str, str] | None = None,
) -> WorkspaceError | None:
    """The FORGE_TLS_UNTRUSTED refusal a git error output stands for, or None.

    `ca_sources` maps a forge host to where its CA comes from ("the Secret
    <name> or its key <key>"), and `ca_files` to the file it is mounted at. A
    CA file git could not load is answered as missing when the file is not
    there and as unloadable when it is, with what to fix, as the API client
    answers it. Without the file, git's line alone says nothing more, and the
    answer is the missing case's.
    """
    kind, line = classify_tls(text)
    if not kind:
        return None
    found = _HOST_IN_LINE_RE.search(line)
    host = found.group(1) if found else ""
    why = line
    if kind == "ca_load":
        source = (ca_sources or {}).get(host, "")
        ca_file = (ca_files or {}).get(host, "")
        if ca_file and os.path.exists(ca_file):
            kind, why = "ca_unloadable", ca_unloadable_reason(source)
        else:
            kind, why = "ca_missing", ca_missing_reason(source, line)
    return tls_untrusted(host, why, kind)


def forge_error(
    status: int,
    detail: str = "",
    overrides: Mapping[int, Override] | None = None,
    message: str | None = None,
) -> WorkspaceError:
    """Turn a forge's refusal into an answer the caller can act on.

    `detail` is the one line the caller is shown underneath the guidance.
    `message` is everything the forge said, which is what an override reads
    when it has to split a status on wording -- the marker that distinguishes
    the two meanings is often not on the first line. It defaults to `detail`.
    """
    full = detail if message is None else message
    detail = detail.strip()[:400]
    chosen = None
    override = (overrides or {}).get(status)
    if isinstance(override, Guidance):
        chosen = override
    elif callable(override):
        chosen = override(full)
    if chosen is None:
        if status >= 500:
            chosen = UNAVAILABLE
        else:
            chosen = GUIDANCE.get(status, UNRECOGNISED)
    return WorkspaceError(
        chosen.text, status=chosen.status, code=chosen.code, detail=detail
    )
