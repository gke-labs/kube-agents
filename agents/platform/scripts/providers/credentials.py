#!/usr/bin/env python3
"""How a forge's token is acquired, and how it is presented -- one object.

Four things about a credential differ per forge, and the natural place to put
each of them is a different one: whether it expires (nowhere -- it is assumed),
how it is acquired (the process holding the privilege), how the API presents it
(inside whichever client makes the call), and how `git` presents it (a side
effect of acquisition). Every one of those is defensible alone and the set is
wrong, because they are four views of one question -- how is *this* forge's
token presented -- and scattering them is what allows the fourth to become
invisible. It has been invisible before: `gh auth setup-git` writes a global
git credential helper as an undeclared side effect of authenticating the API,
which is why `git clone` works in a design where nothing says it should.

So: one object, which the forge constructs and owns, holding all three.
Acquisition is a strategy the forge *selects*, not a pipeline every forge is
fitted into -- a forge whose token does not expire says so by choosing a
strategy that has nothing to do, rather than by implementing a method that
returns immediately.

A credential may not run a subprocess, for the same reason a forge may not: the
broker owns process execution. A strategy that needs a privileged act names it
and the executor performs it. Three roles, cleanly separated -- the forge
chooses the strategy, the strategy names the privileged operation, the executor
performs it.
"""

from __future__ import annotations

import logging
from typing import Callable, Protocol

LOGGER = logging.getLogger("credential-proxy.vcs")

# The privileged operation a BrokeredCredential names: (provider, repository).
# In the broker process this is invoked directly; the same executor is what
# `POST /v1/forge/refresh` reaches, which is how an out-of-process caller asks
# for it. Carrying the provider as an argument rather than in a route path is
# what lets an agent image and a broker image differ by a release.
RefreshOperation = Callable[[str, str], None]


class Credential(Protocol):
    """One forge's token, in the three forms anything here needs it."""

    def ensure(self, repo: str) -> None:
        """Make this credential current, if that means anything to you."""

    def headers(self, repo: str) -> dict[str, str]:
        """Headers the API transport should send. May be empty."""

    def git_config(self, repo: str) -> tuple[tuple[str, str], ...]:
        """Config keys for the git invocations the broker makes on this forge's
        behalf. Applied to those invocations only. May be empty."""


class BrokeredCredential:
    """A short-lived token the broker re-acquires before it is spent.

    Refreshing happens before every credentialed verb rather than in response
    to a failure. An expired token surfaces from inside the broker's own clone
    as `Authentication failed`, which reaches the caller as a clone failure and
    reads like the repository is gone; the alternative to refreshing eagerly is
    that the first verb after an idle hour fails once, for a reason the caller
    cannot act on. Acquisition is idempotent and costs one local process.

    A failure here is logged and not raised. The broker may already hold a
    valid token, in which case the verb about to run succeeds and a refusal
    would have been the only thing that failed.

    `PermissionError` is the exception, and it is not a failure to refresh. The
    operation this strategy names answers two questions at once -- is the token
    current, and is this a repository the install acts on -- and the second is
    an authorization decision. Swallowing it would let a verb proceed against a
    repository that was just refused, on a token that is valid, which is the
    only shape of "refresh failed" that must stop the verb.

    `headers` and `git_config` are both empty, and that is a statement rather
    than an omission: this strategy is for a forge whose CLI carries the token
    on the API side and installs a git credential helper on the git side, so
    there is nothing for the broker to add to either.
    """

    def __init__(self, provider: str, refresh: RefreshOperation | None) -> None:
        self.provider = provider
        self._refresh = refresh

    def ensure(self, repo: str) -> None:
        if self._refresh is None:
            return
        try:
            self._refresh(self.provider, repo)
        except PermissionError:
            raise
        except Exception as exc:  # noqa: BLE001 - the verb's own error is better
            LOGGER.warning(
                "%s: credential refresh for %s failed: %s",
                self.provider,
                repo,
                type(exc).__name__,
            )

    def headers(self, repo: str) -> dict[str, str]:
        return {}

    def git_config(self, repo: str) -> tuple[tuple[str, str], ...]:
        return ()


class NoCredential:
    """Nothing to acquire and nothing to present.

    What a forge this install has not been configured for holds, so that the
    stub still satisfies the interface and a reader does not have to check
    whether `credential` can be `None`.
    """

    def ensure(self, repo: str) -> None:
        return None

    def headers(self, repo: str) -> dict[str, str]:
        return {}

    def git_config(self, repo: str) -> tuple[tuple[str, str], ...]:
        return ()
