#!/usr/bin/env python3
"""The pool of per-cluster service accounts, and the choice between them.

GCP has no delegation primitive — no Credential Access Boundary outside Cloud
Storage, no `actor_token`, no `act` claim — so a broker cannot attenuate the
credential it holds. Google's own documented workaround is several service
accounts with different role sets, and that is what this module selects from.

**Members currently hold no IAM grant, and the pool is off by default.**

The original design gave each member `roles/container.viewer` under an IAM
Condition on one cluster's `resource.name`. Measured 2026-08-12: that grants
nothing for Kubernetes object operations. Four spellings were tried, including
`resource.service == "container.googleapis.com"` — which asserts nothing beyond
"this is a GKE call" — and all were refused. Resource attributes are not
populated on the path GKE uses to authorize object operations, the same seam
where the `container.read-only` OAuth scope was already found not to constrain
object writes.

Deleting the condition is not the repair. Un-conditioned, that binding is
project-wide `container.viewer`, which is the exact ceiling the pool exists to
remove. So the grant is gone entirely, and a member is presently a principal
with no authority at all.

The replacement is per-cluster Kubernetes RBAC. GKE authorizes on IAM *or*
RBAC, and RBAC is per-cluster natively, so a binding in one cluster says
nothing about any other. That is a separate change and it is not here yet.

Everything else in this module was measured working: the selection, the refusal,
the mint, and the kubeconfig rewrite. What is missing is any authority for the
identity it selects, which is why `pool_enabled` defaults off.

Three properties are the point of the module, and each one is here rather than
at the call site so that removing it is a visible diff:

* **No field of the request names an account.** `select` takes a triple, and
  the only way to reach it is through a kubeconfig's `current-context`, which
  the broker parses into a cluster itself. A payload naming a service account,
  a scope or an email is data the broker never reads.

  What that does *not* say is that the input is out of the agent's hands. The
  agent writes the kubeconfig in the shared workspace, and `current-context` is
  the selector; it can also re-point the sidecar's own default with an allowed
  `get-credentials`. So the agent chooses **which cluster** it is asking about.
  What it cannot do is choose the account, because the mapping is a file it
  cannot write, and asking about a cluster with no entry is a refusal rather
  than a wider credential. The token and the API server address both come from
  the same resolved cluster, so naming a different one buys a credential scoped
  to that different one.
* **A scope with no member is refused.** There is no widest-member fallback and
  no ambient fallback, because a fallback to the credential this pool exists to
  stop using is invisible in every log line that matters.
* **The mapping key is the GKE resource name, spelled once.** `scope_key`
  builds `projects/P/locations/L/clusters/C` and Terraform keys the pool on the
  identical string, so the broker and the provisioner compare one rendering
  rather than two of the same idea. Every Critical this project has found came
  from a checker and an enforcer parsing the same input differently. This
  survived the condition's removal; it is the pool's index, and it was only ever
  incidentally the condition's operand.

Two things this module does *not* do, both worth stating because the obvious
reading of it overclaims.

It does not bound which pool member a given agent session may draw. It cannot:
the broker's finest identity is one ServiceAccount shared by both pods, and
`Principal.caller` is still unpopulated. What the pool bounds is the authority
of any single credential the broker will hold — a ceiling, not an assignment.
Per-session assignment arrives with per-caller identity, which is separate work.

And it does not make the broker the only route to a Kubernetes object. The
`gke` remote MCP server, configured in every profile, proxies to
container.googleapis.com/mcp from the agent container on the ambient Workload
Identity credential; it never touches this process. So the pool narrows what
goes *through* the broker, and the agent's own IAM grant is what narrows what
goes around it. Neither is sufficient alone, and the second is the one that
covers the paths nobody has enumerated yet.
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

POOL_FLAG_ENV = "CREDENTIAL_PROXY_SCOPED_SA_POOL"
POOL_FILE_ENV = "CREDENTIAL_PROXY_SCOPED_SA_POOL_FILE"
DEFAULT_POOL_FILE = "/etc/credential-proxy/scoped-sa-pool.json"

# The pool file is rendered by the operator into a ConfigMap. It is small by
# construction — one line per managed cluster — so a bound this far above any
# real fleet only exists to keep a malformed mount from being read into memory.
MAX_POOL_BYTES = 1 << 20

# Matches `credential_proxy._GKE_CONTEXT_COMPONENT`. The duplication is
# deliberate: this module must not import the broker (the broker imports it),
# and a test asserts the two agree on every component it can construct, so a
# drift between them fails rather than silently admitting a key the other half
# would reject.
#
# `\Z` rather than `$`, for the reason spelled out beside the broker's copy:
# `$` matches before a trailing newline, so `$` plus `re.match` admits
# "cluster\n" -- which then goes into the scope key, and the scope key goes
# into a log line.
_COMPONENT = re.compile(r"^[a-z0-9][a-z0-9-]*\Z")

# `<id>@<project>.iam.gserviceaccount.com`.
#
# This checks shape and nothing else, and the distinction matters. It rejects a
# human (`someone@corp.com`) and the legacy default compute account
# (`...@developer.gserviceaccount.com`), which are the mistakes an operator can
# actually make by hand. It cannot reject a Google-managed service agent such as
# `service-1@container-engine-robot.iam.gserviceaccount.com`, because that
# domain is shaped exactly like a project id and no pattern can tell the two
# apart.
#
# So this is a typo check, not a trust boundary. What makes an entry
# trustworthy is where the file comes from: a ConfigMap the operator renders
# from the PlatformAgent CR, mounted read-only, on a volume the agent does not
# write. Reading it as validation of provenance would be a declaration standing
# in for the property it describes.
# \Z and fullmatch, not $ and match — the same trailing-newline trap the
# _COMPONENT comment records. The file is operator-rendered and CRD-validated,
# so a newline only arrives via a hand-written pool file, but the invariant
# this module keeps is that no pattern in it admits one.
_SERVICE_ACCOUNT = re.compile(
    r"[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z0-9-]{6,30}\.iam\.gserviceaccount\.com\Z"
)

# One hour is the ceiling `generateAccessToken` enforces without
# `constraints/iam.allowServiceAccountCredentialLifetimeExtension`, which lifts
# it to twelve. This deployment deliberately does not enable that org policy,
# and a constant that merely happens to sit under the limit would not record
# the refusal — so the cap is asserted here and a request for more raises
# rather than being clamped.
MAX_LIFETIME_SECONDS = 3600
DEFAULT_LIFETIME_SECONDS = 900

CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"


class PoolRefusal(Exception):
    """No pool member covers the scope this request resolved to.

    Distinct from `ValueError` so the handler can answer with a policy refusal
    rather than a malformed-request error: the caller did nothing wrong, the
    deployment has no credential narrow enough to serve it.
    """


class PoolConfigurationError(Exception):
    """The pool is switched on and its mapping is unusable.

    Raised at construction, never at request time, and never downgraded to
    "off". An operator who asked for scoped accounts and silently got the
    ambient credential back would believe they had a property they do not have —
    the same shape as a ValidatingAdmissionPolicy that installs without binding.
    """


def pool_enabled(environ: dict[str, str] | None = None) -> bool:
    """Is the pool armed?

    **Off by default, changed 2026-08-12.** It was on by default, on the
    reasoning that the ambient credential is a rollback rather than a migration
    path and a rollback reachable by a typo is not one. That reasoning still
    holds and will apply again once the pool grants anything.

    It does not today. The IAM Condition each member was scoped by grants
    nothing for Kubernetes object operations, so the grant was removed and
    members hold no authority. Armed by default, this module would select a
    powerless identity for every request and turn every cluster read into a
    Forbidden -- fail-closed, and a full outage.

    Flip the default back in the same change that lands per-cluster RBAC, and
    not before. A test asserting a real read succeeds through the pool is the
    thing that earns it.
    """
    values = os.environ if environ is None else environ
    return values.get(POOL_FLAG_ENV, "0").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def scope_key(project: str, location: str, cluster: str) -> str:
    """The GKE resource name, which is also this pool's mapping key.

    Callers pass components that have already been validated as a GKE context;
    this re-checks them anyway, because the function's whole value is that its
    output is byte-identical to the operand of an IAM Condition, and a component
    carrying a slash or a quote would produce a key that silently matches
    nothing (or, in the Terraform half, an expression that means something else).
    """
    for name, value in (("project", project), ("location", location), ("cluster", cluster)):
        if not isinstance(value, str) or not _COMPONENT.fullmatch(value):
            raise ValueError(f"{name} is not a GKE name component: {value!r}")
    return f"projects/{project}/locations/{location}/clusters/{cluster}"


# `iam_condition_expression` was removed on 2026-08-12.
#
# It rendered `resource.name == "<key>"` so a test could assert that the broker
# and Terraform spelled the condition identically. They did. The condition still
# granted nothing -- the two halves agreed perfectly about a string that GKE
# never evaluates for object operations.
#
# Worth leaving the note rather than a clean deletion, because the test that
# guarded it passed throughout and would have again. It compared two renderings
# of an expression instead of asking whether a principal carrying it could read
# a pod. Assert the behaviour, never the artifact.


@dataclass(frozen=True)
class PoolMember:
    """One cluster and the service account that may read it."""

    key: str
    service_account: str


def parse_pool(document: object) -> dict[str, PoolMember]:
    """Validate the mapping document and index it by scope key.

    Strict on every field, and specifically strict about duplicates: two entries
    for one cluster is an ambiguity, and resolving it by last-wins would mean the
    account a request gets depends on the order the operator happened to render.
    """
    if not isinstance(document, dict):
        raise PoolConfigurationError("pool file must contain a JSON object")
    if document.get("version") != 1:
        raise PoolConfigurationError(
            f"unsupported pool file version: {document.get('version')!r} (expected 1)"
        )
    entries = document.get("serviceAccounts")
    if not isinstance(entries, list):
        raise PoolConfigurationError("pool file must carry a `serviceAccounts` list")

    members: dict[str, PoolMember] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise PoolConfigurationError(f"serviceAccounts[{index}] is not an object")
        try:
            key = scope_key(
                entry.get("projectId"),
                entry.get("location"),
                entry.get("clusterName"),
            )
        except ValueError as error:
            raise PoolConfigurationError(f"serviceAccounts[{index}]: {error}") from error
        email = entry.get("serviceAccountEmail")
        if not isinstance(email, str) or not _SERVICE_ACCOUNT.fullmatch(email):
            raise PoolConfigurationError(
                f"serviceAccounts[{index}] has no well-formed serviceAccountEmail: {email!r}"
            )
        if key in members:
            raise PoolConfigurationError(
                f"serviceAccounts[{index}] repeats {key}; a scope maps to one account"
            )
        members[key] = PoolMember(key=key, service_account=email)

    if not members:
        raise PoolConfigurationError(
            "pool file lists no service accounts; every scoped request would be refused."
            f" Set {POOL_FLAG_ENV}=0 to run on the ambient credential instead."
        )
    return members


def load_pool_file(path: Path) -> dict[str, PoolMember]:
    """Read and validate the mapping from disk."""
    try:
        if path.stat().st_size > MAX_POOL_BYTES:
            raise PoolConfigurationError(f"pool file is implausibly large: {path}")
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise PoolConfigurationError(
            f"pool file is unreadable: {path} ({error})."
            f" Set {POOL_FLAG_ENV}=0 to run on the ambient credential instead."
        ) from error
    try:
        document = json.loads(text)
    except json.JSONDecodeError as error:
        raise PoolConfigurationError(f"pool file is not valid JSON: {path} ({error})") from error
    return parse_pool(document)


def mint_impersonated_token(service_account: str, lifetime_seconds: int) -> tuple[str, float]:
    """Exchange the ambient credential for one bounded to `service_account`.

    The broker's own identity is the caller here — it holds
    `roles/iam.serviceAccountTokenCreator` **on each pool member as a resource**,
    never at project level, so the set of accounts it can become is exactly the
    pool and every one of them is narrower than it is.

    Returns the token and its expiry as a POSIX timestamp. `google.auth` is
    imported lazily, matching the rest of this directory, so the module stays
    importable in a test environment that has no cloud libraries.
    """
    if lifetime_seconds > MAX_LIFETIME_SECONDS:
        raise ValueError(
            f"token lifetime {lifetime_seconds}s exceeds the {MAX_LIFETIME_SECONDS}s"
            " ceiling; raising it needs the credential-lifetime-extension org policy,"
            " which this deployment deliberately does not enable"
        )

    import google.auth
    from google.auth import impersonated_credentials
    from google.auth.transport.requests import Request

    source, _ = google.auth.default(scopes=[CLOUD_PLATFORM_SCOPE])
    credentials = impersonated_credentials.Credentials(
        source_credentials=source,
        target_principal=service_account,
        target_scopes=[CLOUD_PLATFORM_SCOPE],
        lifetime=lifetime_seconds,
    )
    credentials.refresh(Request())
    if not credentials.token:
        raise RuntimeError(f"no access token was returned for {service_account}")
    # google-auth parses generateAccessToken's expireTime into a naive UTC
    # datetime, and .timestamp() on a naive datetime reads it in the host's
    # local timezone — east of UTC every cached token would look already
    # expired, west of UTC it would be reused past its real life. The shipped
    # container runs UTC, so pin the zone rather than the deployment.
    expiry = credentials.expiry
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    return credentials.token, expiry.timestamp()


class ScopedServiceAccountPool:
    """Selection, and the short-lived tokens selection produces.

    `minter` and `clock` are injected so the selection and refusal behaviour can
    be tested without reaching Google. The default minter is the real one; a test
    that wants the real one and does not have credentials will fail loudly at the
    call rather than pass against a stub.
    """

    # A token is replaced this long before it expires, so a command that starts
    # just under the wire does not run out mid-request.
    REFRESH_MARGIN_SECONDS = 120

    def __init__(
        self,
        members: dict[str, PoolMember],
        minter: Callable[[str, int], tuple[str, float]] = mint_impersonated_token,
        lifetime_seconds: int = DEFAULT_LIFETIME_SECONDS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if lifetime_seconds > MAX_LIFETIME_SECONDS:
            raise PoolConfigurationError(
                f"token lifetime {lifetime_seconds}s exceeds the"
                f" {MAX_LIFETIME_SECONDS}s ceiling"
            )
        import time

        self._members = dict(members)
        self._minter = minter
        self._lifetime_seconds = lifetime_seconds
        self._clock = clock or time.time
        self._lock = threading.Lock()
        self._tokens: dict[str, tuple[str, float]] = {}

    @property
    def scopes(self) -> list[str]:
        """Every scope the pool covers, for logging and for the startup line."""
        return sorted(self._members)

    def select(self, project: str, location: str, cluster: str) -> PoolMember:
        """The account for one cluster, or a refusal.

        The arguments are three validated strings, not a request and not a
        mapping, and that is the property the signature carries: no field of a
        request reaches this method, because the broker resolves a cluster of
        its own before it calls.

        The cluster itself is agent-influenced -- it comes from the
        `current-context` of a kubeconfig in the shared workspace -- and the
        module docstring says why that is not the same as choosing an account.
        Read this as "the account is not selectable", not as "the input is
        trusted".
        """
        key = scope_key(project, location, cluster)
        member = self._members.get(key)
        if member is None:
            raise PoolRefusal(
                f"no scoped service account is provisioned for {key}."
                " The broker will not fall back to the ambient credential;"
                " add the cluster to the pool or exclude it from the fleet."
            )
        return member

    def token_for(self, project: str, location: str, cluster: str) -> str:
        """A short-lived access token for the account this cluster maps to."""
        member = self.select(project, location, cluster)
        now = self._clock()
        with self._lock:
            cached = self._tokens.get(member.key)
            if cached is not None and cached[1] - self.REFRESH_MARGIN_SECONDS > now:
                return cached[0]
            token, expiry = self._minter(member.service_account, self._lifetime_seconds)
            self._tokens[member.key] = (token, expiry)
            return token


def build_pool(
    environ: dict[str, str] | None = None,
    minter: Callable[[str, int], tuple[str, float]] = mint_impersonated_token,
) -> ScopedServiceAccountPool | None:
    """The pool, or None when the deployment has opted out.

    None means the ambient credential, which is the supported rollback. It is
    reachable only by setting the flag, never by a missing file or an empty
    list — those raise, because "I configured a pool and got the wide
    credential" is precisely the invisible failure the pool exists to avoid.
    """
    values = os.environ if environ is None else environ
    if not pool_enabled(values):
        return None
    path = Path(values.get(POOL_FILE_ENV, DEFAULT_POOL_FILE))
    members = load_pool_file(path)
    return ScopedServiceAccountPool(members, minter=minter)


def kubeconfig_with_token(text: str, token: str) -> str:
    """Rewrite a gcloud-authored kubeconfig to authenticate with `token`.

    `get-credentials` writes an `exec` stanza naming gke-gcloud-auth-plugin, and
    that plugin resolves Application Default Credentials on its own — it does not
    read `CLOUDSDK_AUTH_ACCESS_TOKEN`, so setting that variable would scope
    gcloud and leave kubectl running as the ambient identity. Half a control is
    worse than none here, because the half that works is the one nobody tests.

    So the credential is put where kubectl will certainly look: every user entry
    is replaced outright by a bearer token. Replaced rather than merged — an
    `exec`, `auth-provider` or `tokenFile` left beside a token is a second
    credential path, and which one kubectl prefers is a question about somebody
    else's parser.
    """
    import yaml

    document = yaml.safe_load(text)
    if not isinstance(document, dict):
        raise ValueError("kubeconfig is not a mapping")
    users = document.get("users")
    if not isinstance(users, list) or not users:
        raise ValueError("kubeconfig names no users")
    rewritten = []
    for user in users:
        if not isinstance(user, dict) or "name" not in user:
            raise ValueError("kubeconfig user entry has no name")
        rewritten.append({"name": user["name"], "user": {"token": token}})
    document["users"] = rewritten
    return yaml.safe_dump(document, default_flow_style=False, sort_keys=False)
