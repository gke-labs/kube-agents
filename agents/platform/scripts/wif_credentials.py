#!/usr/bin/env python3
"""Write the external_account credential file the co-located proxy authenticates with.

The credential proxy normally gets its GCP identity from the GKE metadata server.
That stops being acceptable the moment it shares a pod with the shell sandbox:
Workload Identity resolves by pod IP, so the shell container would curl
169.254.169.254 and mint the proxy's own service-account token, which is every
credential the proxy holds with the policy layer stepped around.

Workload Identity Federation is the way out, because it authenticates a *file*.
The pod's ServiceAccount is left unannotated, so the metadata server answers both
containers with an unbound <project>.svc.id.goog principal that IAM grants
nothing. A projected ServiceAccount token, audience-scoped to the federation
provider, is mounted into the proxy container alone -- and a volumeMount is the
one boundary in a pod that is per-container rather than pod-wide.

This script turns the CREDENTIAL_PROXY_WIF_* variables the operator sets into the
document google-auth and gcloud read. It runs from start-services.sh before any
of them start, and no-ops when the variables are absent, which is the standalone
placement where the metadata server is still the right answer.

It is also imported, for `fetch_identity_token` -- the one thing federation does
not give the proxy for free. See that function.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

# The Google endpoints the exchange goes through. None varies per install: STS is
# global, and the IAM Credentials URLs take `-` for the project because the
# service account's email already identifies it.
STS_TOKEN_URL = "https://sts.googleapis.com/v1/token"
IMPERSONATION_URL = (
    "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/{email}:generateAccessToken"
)
ID_TOKEN_URL = (
    "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/{email}:generateIdToken"
)
CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
# The segment of an impersonation URL that the service account's email follows.
# fetch_identity_token splits on it to recover that email.
IMPERSONATION_PATH = "/serviceAccounts/"

# How long an STS or IAM call may take. Both are Google endpoints reached over
# the pod's own network, so a call that has not answered by now is a broken
# route rather than a slow one -- and the caller is a proxy holding a request
# open while it waits.
TOKEN_REQUEST_TIMEOUT_SECONDS = 10


def build_document(audience: str, service_account: str, token_file: str) -> dict:
    """Return the external_account document.

    Impersonation rather than direct grants on the federated principal. The
    agent's roles are already attached to a service account, and every install
    surface grants them there; pointing the federated identity at that same
    account through iam.workloadIdentityUser means the role set does not have to
    be reproduced against a principal:// member, and revoking access is one
    binding rather than a sweep.
    """
    return {
        "type": "external_account",
        "audience": audience,
        # The projected token is a JWT, and STS has to be told so -- the default
        # for a file source is an opaque token, which the provider rejects with
        # an error that names neither the token nor the format.
        "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
        "token_url": STS_TOKEN_URL,
        "service_account_impersonation_url": IMPERSONATION_URL.format(email=service_account),
        "credential_source": {
            "file": token_file,
            "format": {"type": "text"},
        },
    }


def write_document(path: str, document: dict) -> None:
    """Write it atomically, readable only by the proxy's own uid.

    Atomic because the auth libraries re-read this file on every token refresh,
    not once at startup: a truncated file caught mid-write fails a command that
    had nothing to do with the restart that caused it.
    """
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".wif-credentials-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2)
            handle.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        # A leftover dotfile in the runtime directory would be picked up by
        # nothing, but it would also never be cleaned up: the emptyDir survives
        # every restart short of the pod's.
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _post(url: str, body: bytes, headers: dict) -> dict:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(
            request, timeout=TOKEN_REQUEST_TIMEOUT_SECONDS
        ) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace").strip()
        raise RuntimeError(f"{url} returned HTTP {exc.code}: {detail}") from exc


def load_document(path: str = "") -> dict | None:
    """Return the external_account document in use, or None under any other identity.

    The path is taken from the same three variables gcloud and google-auth
    consult, in their order, so a caller sees the credential the rest of the
    container is actually using rather than one this module happened to write.
    Anything that is not an external_account document -- a service-account key, a
    user credential, nothing at all -- returns None, which is the caller's signal
    that the metadata server is the identity here.
    """
    for candidate in (
        path,
        os.environ.get("CREDENTIAL_PROXY_WIF_CREDENTIAL_FILE", ""),
        os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", ""),
        os.environ.get("CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE", ""),
    ):
        if not candidate.strip():
            continue
        try:
            with open(candidate.strip(), encoding="utf-8") as handle:
                document = json.load(handle)
        except (OSError, ValueError):
            continue
        if isinstance(document, dict) and document.get("type") == "external_account":
            return document
    return None


def fetch_identity_token(audience: str, path: str = "") -> str | None:
    """Mint a Google ID token for the impersonated service account, or None.

    Federation covers access tokens and nothing else. `gcloud auth
    print-identity-token` refuses an external_account credential outright --
    "Invalid account type for `--audiences`. Requires valid service account." --
    so the one caller that needs an ID token rather than an access token, the
    GitHub token refresh, has no route through the CLI. Under the standalone
    placement that call worked because the metadata server was minting it.

    The two legs here are what gcloud will not do: exchange the projected token
    at STS for a federated access token, then call generateIdToken on the service
    account. Deliberately the federated token and not the impersonated one that
    the credential file yields -- iam.workloadIdentityUser, which is the binding
    every install surface already creates, carries getOpenIdToken, whereas
    reaching generateIdToken with the service account's own access token is
    self-impersonation and needs a tokenCreator binding on itself that nothing
    grants.

    The token this returns is issued by accounts.google.com and carries the
    service account's email, so the identity the token broker sees is the same
    one the metadata server presented. No broker-side configuration changes when
    an install moves the proxy into the sandbox pod.

    Returns None when the credential is not an external_account document, or is
    one of the external_account shapes these two legs cannot drive, which leaves
    the caller on its existing path.
    """
    document = load_document(path)
    if document is None:
        return None

    # Not every external_account carries these. Direct-access federation omits
    # the impersonation URL, and credential_source may name a `url` or an
    # `executable` instead of a `file`. Both are valid credentials, and
    # load_document reads GOOGLE_APPLICATION_CREDENTIALS precisely so a
    # hand-placed one is honoured -- so reaching either is a configuration this
    # path cannot drive, not a bug. Decline the way an unfederated identity is
    # declined and let the caller fall through to gcloud, rather than raising
    # KeyError out of a function whose contract is to return None. `audience`,
    # `token_url` and `subject_token_type` below are indexed directly because
    # the schema requires them of every external_account.
    impersonation = document.get("service_account_impersonation_url")
    source = document.get("credential_source")
    source_file = source.get("file") if isinstance(source, dict) else None
    if not isinstance(impersonation, str) or IMPERSONATION_PATH not in impersonation:
        return None
    if not isinstance(source_file, str) or not source_file:
        return None

    email = impersonation.split(IMPERSONATION_PATH, 1)[1].split(":", 1)[0]
    with open(source_file, encoding="utf-8") as handle:
        subject_token = handle.read().strip()

    federated = _post(
        document["token_url"],
        urllib.parse.urlencode(
            {
                "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
                "audience": document["audience"],
                "scope": CLOUD_PLATFORM_SCOPE,
                "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
                "subject_token": subject_token,
                "subject_token_type": document["subject_token_type"],
            }
        ).encode("utf-8"),
        {"Content-Type": "application/x-www-form-urlencoded"},
    )

    minted = _post(
        ID_TOKEN_URL.format(email=email),
        json.dumps({"audience": audience, "includeEmail": True}).encode("utf-8"),
        {
            "Authorization": "Bearer " + federated["access_token"],
            "Content-Type": "application/json",
        },
    )
    return minted["token"]


def main(argv: list[str]) -> int:
    audience = os.environ.get("CREDENTIAL_PROXY_WIF_AUDIENCE", "").strip()
    service_account = os.environ.get("CREDENTIAL_PROXY_WIF_SERVICE_ACCOUNT", "").strip()
    token_file = os.environ.get("CREDENTIAL_PROXY_WIF_TOKEN_FILE", "").strip()
    credential_file = os.environ.get("CREDENTIAL_PROXY_WIF_CREDENTIAL_FILE", "").strip()

    if not audience:
        # The standalone placement. Silent rather than logged: this is the
        # default for every install that has not opted into the sandbox, and a
        # line about federation in their logs invites a search for a
        # misconfiguration that is not there.
        return 0

    missing = [
        name
        for name, value in (
            ("CREDENTIAL_PROXY_WIF_SERVICE_ACCOUNT", service_account),
            ("CREDENTIAL_PROXY_WIF_TOKEN_FILE", token_file),
            ("CREDENTIAL_PROXY_WIF_CREDENTIAL_FILE", credential_file),
        )
        if not value
    ]
    if missing:
        # Fail the container rather than starting one whose every credentialed
        # command reports itself unconfigured. The operator sets all four
        # together, so reaching this means something rewrote the pod spec by
        # hand, and a CrashLoopBackOff is the fastest way for that to be seen.
        print(
            "wif_credentials: CREDENTIAL_PROXY_WIF_AUDIENCE is set but "
            + ", ".join(missing)
            + " is not; the credential proxy has no way to authenticate to GCP",
            file=sys.stderr,
        )
        return 1

    if not os.path.exists(token_file):
        # Kubelet writes the projection before the container starts, so an
        # absent file means the volume is not mounted -- which is the mistake
        # this design is most exposed to, because the pod comes up either way
        # and only the credentialed commands fail.
        print(
            f"wif_credentials: the projected token {token_file} does not exist; "
            "the credential proxy's WIF volume is not mounted",
            file=sys.stderr,
        )
        return 1

    write_document(credential_file, build_document(audience, service_account, token_file))
    # No secret in this line: the audience is a resource name and the service
    # account is an email, both of which are already in the pod spec. It is here
    # because "which identity is this container using" is the first question
    # asked when a credentialed command fails.
    print(
        f"wif_credentials: wrote {credential_file} — federated identity "
        f"{service_account} via {audience}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
