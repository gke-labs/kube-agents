#!/usr/bin/env python3
"""Which command-line tools an install may run, given the forges it serves.

One module because there are two readers on opposite sides of the credential
boundary and they must not disagree. `credential_proxy.py` runs in the sidecar
and enforces the list; `credential_proxy_client.py` is the shim in the agent
sandbox and offers it. A shim narrower than the enforcer refuses a tool the
install is entitled to, and one wider produces a confusing refusal a layer
further in — so before this module the same tuple was written twice and
`docs/designs/multi-forge-support.md` §5 counted that as part of the cost of a
second forge.

The compiled-in table is the security property
--------------------------------------------
`FORGE_EXECUTABLES` is in this file, not in the environment.
`CREDENTIAL_PROXY_FORGE_PROVIDERS` states which of its entries an install may
run; it cannot introduce a name that is not here. Were the allowlist itself an
environment variable, anything that could set one variable on the sidecar could
run any binary on it — which is the escalation the operator's reserved-variable
list exists to prevent, and a control resting only on that list being complete
is one omission away from nothing.

So the environment selects, and this file defines.
"""

from __future__ import annotations

import os

#: The executables every install runs, whichever forge it is configured for.
#: Nothing here talks to a forge.
BASE_EXECUTABLES = ("gcloud", "kubectl", "git")

#: GitHub's provider name. Spelled once here because three modules compare
#: against it and one of them guards a credential path on the comparison.
GITHUB = "github"

#: Every forge command-line tool this harness will ever run, by provider name.
#: A forge with no such tool maps to no entry: Bitbucket Cloud has none, which
#: is why `forge.py` was built with its `_call` seam, and such a provider
#: reaches the sidecar over a `/v1/<forge>/…` route instead of shelling
#: anything. Keep the names in step with `GitProvider.CLI` in
#: `k8s-operator/api/v1alpha1/gitprovider.go` — the operator configures provider
#: names, never binaries, so the two sides agree on names rather than on paths.
FORGE_EXECUTABLES = {GITHUB: "gh"}

#: The environment variable the operator renders from the configured providers.
FORGE_PROVIDERS_ENV = "CREDENTIAL_PROXY_FORGE_PROVIDERS"

#: The provider assumed when nothing names one — an older operator alongside a
#: newer sidecar, or a dev install rendered from kustomize. GitHub, because
#: every install that predates this variable is a GitHub install and would
#: otherwise start up having lost `gh`.
DEFAULT_FORGE_PROVIDER = GITHUB


def configured_providers(configured: str | None = None) -> tuple[str, ...]:
    """The provider names this install serves, lowercased and deduplicated.

    `configured` defaults to the environment. Empty means
    `DEFAULT_FORGE_PROVIDER`, so a sidecar running beside an operator that does
    not set the variable behaves exactly as it did before the variable existed.
    """
    if configured is None:
        configured = os.getenv(FORGE_PROVIDERS_ENV, "")
    names = [part.strip().lower() for part in configured.split(",") if part.strip()]
    if not names:
        names = [DEFAULT_FORGE_PROVIDER]
    return tuple(dict.fromkeys(names))


def forge_executables(configured: str | None = None) -> tuple[str, ...]:
    """The forge tools the configured providers need, in provider order.

    A name with no entry in `FORGE_EXECUTABLES` contributes nothing, and that is
    not an error. It is either a forge that has no CLI or a provider newer than
    this image, and in both cases "no binary" is the honest answer — refusing to
    start would take down an install over a provider it may not even use, while
    an install that does try to run that tool is refused at the allowlist, which
    is where an operator can see it and where the refusal names the executable.
    """
    seen: dict[str, None] = {}
    for name in configured_providers(configured):
        executable = FORGE_EXECUTABLES.get(name)
        if executable:
            seen[executable] = None
    return tuple(seen)


def allowed_executables(configured: str | None = None) -> tuple[str, ...]:
    """Everything this install may run: the base tools plus its forge tools."""
    return BASE_EXECUTABLES + forge_executables(configured)
