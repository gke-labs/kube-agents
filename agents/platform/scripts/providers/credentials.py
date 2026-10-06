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

import base64
import logging
import re
from typing import Callable, Protocol

from workspace_paths import WorkspaceError

LOGGER = logging.getLogger("credential-proxy.vcs")

# The privileged operation a BrokeredCredential names: (provider, repository).
# In the broker process this is invoked directly; the same executor is what
# `POST /v1/forge/refresh` reaches, which is how an out-of-process caller asks
# for it. Carrying the provider as an argument rather than in a route path is
# what lets an agent image and a broker image differ by a release.
RefreshOperation = Callable[[str, str], None]

# The privileged operation a MintedReadCredential names: (provider, repository)
# -> a read-only token for that one repository. Nothing out of process reaches
# it: the broker asks for it inside its own clone, and the executor that
# performs it decides from the repository's registered role whether to.
MintOperation = Callable[[str, str], str]

# How an installation token is presented to git over HTTPS: basic auth with
# this fixed username and the token as the password, on the
# `http.<url>.extraheader` key so the token never becomes part of a URL git
# might print. The key is per host, composed below from the forge's own; the
# username is the convention the first forge's installation tokens use, and
# a forge with another convention picks a different strategy.
HTTP_EXTRAHEADER_KEY = "http.https://{host}/.extraheader"
EXTRAHEADER_USERNAME = "x-access-token"
# `credential.helper` set to the empty string clears every helper configured
# below it in git's precedence, which in the broker means the one the CLI
# installed for the write token. Without this, a 401 from a bad read token --
# or the challenge a clone with no token at all gets -- would fall back to the
# write credential, the fallback this credential exists to make impossible.
# So a MintedReadCredential presents it whether or not a token was minted.
CREDENTIAL_HELPER_KEY = "credential.helper"


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


class MintedReadCredential:
    """A read-only token minted for one clone of one repository, shown to git only.

    What the broker presents when it clones a *context* repository -- one
    registered to be read for declared intent and never written. It is the
    counterpart of `BrokeredCredential` on the other side of a line that
    strategy cannot cross: the brokered token is installed once, ambiently, in
    a helper every git in the sidecar consults, and it is a write token. A
    context repository must never be reached on that token, so this one is
    never installed anywhere. `ensure` asks the executor to mint it, `git_config`
    hands it to the one git invocation the caller is about to run as an
    `extraheader`, and the process it was handed to is the only place it ever
    lives. The same layer clears `credential.helper`, with or without a
    token: the ambient helper is never consulted for a context repository,
    so the line holds when the mint fails as well as when it succeeds.

    Two things are asymmetric with `BrokeredCredential`, on purpose:

    * A failed mint is swallowed, `PermissionError` included, and the caller
      proceeds with no credential. The refusal there stops a verb from running
      on a *valid* token against a repository just refused; here there is no
      token when the mint is refused, so proceeding means a credential-less
      clone, with the helper cleared so it is one: a public repository reads
      as it always did, and a private one fails on the missing token rather
      than being tried on the write one.
    * `headers` is empty. The API side is not part of the read path: a context
      repository is cloned and read, and the collaboration verbs on it are the
      write gate's business.
    """

    def __init__(self, provider: str, mint: MintOperation | None, host: str) -> None:
        self.provider = provider
        self._mint = mint
        self._host = host
        self._token: str | None = None

    def ensure(self, repo: str) -> None:
        self._token = None
        if self._mint is None:
            return
        try:
            token = self._mint(self.provider, repo)
        except Exception as exc:  # noqa: BLE001 - the clone proceeds without it
            LOGGER.warning(
                "%s: read-only credential for %s was not minted: %s",
                self.provider,
                repo,
                type(exc).__name__,
            )
            return
        self._token = token.strip() or None

    def headers(self, repo: str) -> dict[str, str]:
        return {}

    def git_config(self, repo: str) -> tuple[tuple[str, str], ...]:
        helper_cleared = (CREDENTIAL_HELPER_KEY, "")
        if not self._token:
            return (helper_cleared,)
        basic = base64.b64encode(
            f"{EXTRAHEADER_USERNAME}:{self._token}".encode("utf-8")
        ).decode("ascii")
        return (
            (HTTP_EXTRAHEADER_KEY.format(host=self._host), f"AUTHORIZATION: basic {basic}"),
            helper_cleared,
        )


# The git credential helper a StaticFileCredential points git at, at the fixed
# path the broker image installs every script under. Git runs a helper value
# that starts with an absolute path through the shell, with `get` appended, so
# the value is composed from this literal and two arguments that are checked
# against _HELPER_ARGUMENT_RE first; nothing a caller sent reaches it.
TOKEN_FILE_HELPER = "/opt/defaults/scripts/git_credential_token_file.py"
_HELPER_ARGUMENT_RE = re.compile(r"[A-Za-z0-9._/-]+")
_HOST_RE = re.compile(r"[a-z0-9]([a-z0-9.-]*[a-z0-9])?(:[0-9]+)?")



def is_token(token: str) -> bool:
    """Whether a token file's stripped contents can be a token at all.

    One line of printable ASCII with no spaces: what every forge issues, and
    the only thing an HTTP header carries unchanged. Kept in step by hand with
    `git_credential_token_file.py`, which cannot import it.
    """
    return bool(token) and token.isascii() and token.isprintable() and " " not in token

class StaticFileCredential:
    """A long-lived token an administrator put in a Secret, read from its file.

    Nothing to acquire and nothing to refresh, so `ensure` does nothing -- the
    strategy says the token does not need it rather than implementing a step
    that returns at once. The file is read on every call instead of once at
    construction: a rotated Secret updates the projected file, and the next
    call presents the new token with no restart.

    The API sees it as one header, named and formatted by the forge. Git sees
    it through a credential helper scoped to the forge's host, which reads the
    same file, so the token never appears in git's argv, environment or
    config. The layer first clears `credential.helper`, so for these
    invocations the ambient helper another forge's CLI installed is never
    asked -- this host's credential is this file or nothing.

    A missing or empty file is refused, not sent as an unauthenticated call: a
    request without the token reaches the forge as an anonymous read, which
    answers a private repository with 404 and reads like the repository is
    gone.
    """

    def __init__(
        self,
        token_path: str,
        host: str,
        *,
        header: str,
        header_format: str = "{token}",
        username: str = "oauth2",
        helper: str = TOKEN_FILE_HELPER,
    ) -> None:
        for name, value in (("token path", token_path), ("username", username)):
            if not _HELPER_ARGUMENT_RE.fullmatch(value or "") or ".." in value:
                raise ValueError(f"the {name} {value!r} is not one git may be handed")
        if not token_path.startswith("/"):
            raise ValueError(f"the token path {token_path!r} is not absolute")
        if not _HOST_RE.fullmatch(host or ""):
            raise ValueError(f"the host {host!r} is not a hostname")
        self._token_path = token_path
        self._host = host
        self._header = header
        self._format = header_format
        self._username = username
        self._helper = helper

    def ensure(self, repo: str) -> None:
        return None

    def _token(self) -> str:
        try:
            with open(self._token_path, encoding="utf-8") as handle:
                token = handle.read().strip()
        except OSError as exc:
            raise WorkspaceError(
                f"the forge credential for {self._host} could not be read: {type(exc).__name__}",
                status=503,
                code="FORGE_CREDENTIAL_UNAVAILABLE",
            ) from exc
        if not is_token(token):
            # Empty, more than one line, or carrying a character no forge
            # token has -- a byte-order mark, a smart quote from a copy-paste.
            # None is a token, and each splits the credential's two faces: an
            # invalid header on the API side, a 401 or nothing from the git
            # helper on the other. Refused here, by the same rule the helper
            # applies, so both faces say the same thing.
            raise WorkspaceError(
                f"the forge credential for {self._host} is empty or is not one line of printable ASCII",
                status=503,
                code="FORGE_CREDENTIAL_UNAVAILABLE",
            )
        return token

    def headers(self, repo: str) -> dict[str, str]:
        return {self._header: self._format.format(token=self._token())}

    def git_config(self, repo: str) -> tuple[tuple[str, str], ...]:
        return (
            (CREDENTIAL_HELPER_KEY, ""),
            (
                f"credential.https://{self._host}.helper",
                f"{self._helper} {self._token_path} {self._username}",
            ),
        )


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
