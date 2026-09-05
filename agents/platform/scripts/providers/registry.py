#!/usr/bin/env python3
"""Which forges exist, how many of each this install has, and which one a URL is.

The one shared file a new forge edits, and it edits it twice: an import line
and an entry in `AVAILABLE`. Nothing else here changes, and nothing downstream
of `build_forges` changes at all.

The tension this resolves is that a registry should know nothing about a forge,
while a self-managed host is not knowable at import time -- there may be zero of
them or four, and their hostnames come from configuration. Asking the *class*
how many of itself this install has is the only version of that question that
does not put a hostname in a shared file.
"""

from __future__ import annotations

from typing import Any, Mapping

from workspace_paths import WorkspaceError

from .base import Forge, ForgeUnsupported, StubForge
from .github import GitHubForge
from .identity import repository_host

AVAILABLE: tuple[type[Forge], ...] = (GitHubForge,)


# Hosts this design has a name and a shape for but no implementation of yet.
# Present rather than absent so a caller naming one is told what is missing
# instead of being told its URL is not a repository of some forge it did not
# ask about. Each entry is dropped the moment its package joins `AVAILABLE`.
_UNIMPLEMENTED: tuple[tuple[str, tuple[str, ...], str, tuple[str, ...]], ...] = (
    (
        "gitlab",
        ("gitlab.com",),
        "merge request",
        (
            "no credential is configured for gitlab.com",
            "merge requests and issues need a GitLab client in the broker",
        ),
    ),
    (
        "bitbucket",
        ("bitbucket.org",),
        "pull request",
        (
            "no credential is configured for bitbucket.org",
            "pull requests and issues need a Bitbucket client in the broker",
        ),
    ),
)


def build_forges(config: Mapping[str, Any] | None = None) -> tuple[Forge, ...]:
    """Every forge instance this install has, in registration order."""
    settings = config or {}
    return tuple(forge for cls in AVAILABLE for forge in cls.for_config(settings))


def build_stubs(forges: tuple[Forge, ...]) -> tuple[Forge, ...]:
    """The named gaps, minus anything an actual forge already answers for."""
    taken = {host for forge in forges for host in forge.hosts}
    return tuple(
        StubForge(name, hosts, noun, missing)
        for name, hosts, noun, missing in _UNIMPLEMENTED
        if not taken.intersection(hosts)
    )


class Registry:
    """The built forges, and the host table resolution walks.

    The table is the security boundary as much as it is a lookup. A
    caller-chosen URL decides where a credential gets presented, and an
    allowlist is what stops "clone this repository" from meaning "post my
    credential there". A host with no entry is refused by name before anything
    is spent, rather than attempted with whichever credential happens to be
    loaded.
    """

    def __init__(self, config: Mapping[str, Any] | None = None) -> None:
        self.forges = build_forges(config)
        self.stubs = build_stubs(self.forges)
        self.hosts: dict[str, Forge] = {
            host: forge
            for forge in (*self.forges, *self.stubs)
            for host in forge.hosts
        }
        # What a bare `owner/name` means. The first configured forge, which is
        # registration order rather than a name in this file: every skill in
        # this repository has always written bare slugs and meant the forge
        # this install was built around.
        self.default = self.forges[0] if self.forges else None

    @property
    def executables(self) -> tuple[str, ...]:
        """The forge CLIs this install actually needs, derived not listed.

        What the credentialed process may run is the executor's decision, and
        the union of every forge's binaries granted to every install is the
        version of that decision nobody makes on purpose. An install with no
        CLI-backed forge gets none of them.
        """
        return tuple(
            sorted(
                {
                    forge.cli
                    for forge in self.forges
                    if forge.transport == "cli" and forge.cli
                }
            )
        )

    def resolve(self, url: Any) -> tuple[Forge, str]:
        """The forge for this URL and the repository it names, or a refusal.

        A URL that names a host must have that host in the table. There is no
        default for one, because defaulting is how a token reaches a host
        nobody configured.
        """
        if not isinstance(url, str) or not url.strip():
            raise WorkspaceError("repository must be a URL or owner/name")
        host = repository_host(url)
        forge = self.hosts.get(host) if host else self.default
        if forge is None:
            known = ", ".join(sorted(self.hosts)) or "none"
            raise ForgeUnsupported(
                f"{host or 'a bare owner/name'} is not a forge this install "
                f"serves. Configured: {known}."
            )
        return forge, forge.parse(url)
