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

import json
import os
import re
from typing import Any, Mapping

import repo_ref
from workspace_paths import WorkspaceError

from .base import Forge, ForgeUnsupported, StubForge
from .github import GitHubForge

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


# Where the operator mounts the forges this install was configured with. Unset
# means the install predates per-forge configuration, and each forge class
# decides what that means for it (see `Forge.for_config`). Set, the file is the
# whole answer: a forge it does not list is not built.
FORGES_CONFIG_ENV = "VCS_FORGES_CONFIG"
# A hostname and nothing else. A port is refused rather than accepted: the
# repository parser reads a URL's host without its port, so a forge declared at
# `host:8443` would load and then match no request; until ports are carried
# through resolution end to end, the misconfiguration stops the build.
_HOST_RE = re.compile(r"[a-z0-9]([a-z0-9.-]*[a-z0-9])?")


def load_forge_entries(path: str | None = None) -> list[dict[str, Any]] | None:
    """The configured forges, normalised, or None when nothing configures them.

    The file is `{"forges": [{"provider", "host", "tokenPath"?, "allowedPaths"?}]}`.
    Read at registry construction, never cached across it, so a test or a
    remount sees the file it names. A file that is named and cannot be read
    raises: a broker that does not know which forges it serves must not start
    on a guess.
    """
    path = path if path is not None else os.environ.get(FORGES_CONFIG_ENV, "").strip()
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"the forge configuration {path} could not be read: {exc}") from exc
    raw = document.get("forges") if isinstance(document, dict) else None
    if not isinstance(raw, list):
        raise ValueError(f"the forge configuration {path} has no `forges` list")
    entries = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"forges[{index}] in {path} is not an object")
        provider = str(item.get("provider") or "").strip().lower()
        host = str(item.get("host") or "").strip().lower()
        if not provider or not _HOST_RE.fullmatch(host):
            raise ValueError(
                f"forges[{index}] in {path} needs a provider and a hostname "
                "(no scheme, path or port)"
            )
        allowed = item.get("allowedPaths") or []
        if not isinstance(allowed, list) or not all(isinstance(p, str) for p in allowed):
            raise ValueError(f"forges[{index}].allowedPaths in {path} must be a list of paths")
        entries.append(
            {
                "provider": provider,
                "host": host,
                "token_path": str(item.get("tokenPath") or "").strip(),
                "allowed_paths": tuple(p.strip("/") for p in allowed if p.strip("/")),
            }
        )
    return entries


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
        settings = dict(config or {})
        if "forges" not in settings:
            settings["forges"] = load_forge_entries()
        self.forges = build_forges(settings)
        # An entry no forge class claims is a misspelt provider, or one this
        # image predates. Building without it would start a broker that refuses
        # every request at runtime; like the other misconfigurations, it stops
        # the build instead.
        known = {cls.name for cls in AVAILABLE}
        unclaimed = sorted(
            {entry.get("provider", "") for entry in settings.get("forges") or ()} - known
        )
        if unclaimed:
            raise ValueError(
                f"no forge in this image serves provider {', '.join(unclaimed)}; "
                f"this image serves {', '.join(sorted(known))}"
            )
        # One forge per host. A host two forges claim would be answered by
        # whichever registered first, which is a credential presented by the
        # order of a list; refusing at construction keeps resolution a
        # function of the host the repository names and nothing else.
        claimed: dict[str, Forge] = {}
        for forge in self.forges:
            for host in forge.hosts:
                if host in claimed:
                    raise ValueError(
                        f"{host} is configured for both {claimed[host].name} and {forge.name}"
                    )
                claimed[host] = forge
        self.stubs = build_stubs(self.forges)
        self.hosts: dict[str, Forge] = {
            host: forge
            for forge in (*self.forges, *self.stubs)
            for host in forge.hosts
        }
        # What a bare `owner/name` means: the forge, when there is exactly one.
        # Every skill in this repository has always written bare slugs and
        # meant the forge the install was built around, and with one forge
        # that is still unambiguous. With two it is not, and guessing is how a
        # token for one forge is spent on a name that belongs to the other, so
        # a bare name is refused and the caller names the host.
        self.default = self.forges[0] if len(self.forges) == 1 else None

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
        ref = repo_ref.try_parse(url)
        if ref is None:
            raise WorkspaceError(f"{url!r} is not a repository URL or owner/name")
        host = ref.host
        if not host and len(ref.segments) > 1:
            # The registration shorthand, for the hosts this install actually
            # serves: the same lift `repo_ref` applies for the hosts it knows
            # on its own, done here for a *configured* one -- answered from
            # the table, never inferred from the string's shape.
            first = ref.segments[0].lower()
            if first in self.hosts:
                host = first
        if not host and len(self.forges) > 1:
            served = ", ".join(sorted(h for f in self.forges for h in f.hosts))
            raise ForgeUnsupported(
                f"{url!r} names no host, and this install serves more than one forge "
                f"({served}); name the repository by its URL or as <host>/<path>."
            )
        forge = self.hosts.get(host) if host else self.default
        if forge is None:
            # The forges built, not every host in the table: a placeholder for
            # an unconfigured forge is in the table so it can name its gap,
            # and listing it here would call it configured.
            known = ", ".join(sorted(h for f in self.forges for h in f.hosts)) or "none"
            raise ForgeUnsupported(
                f"{host or 'a bare owner/name'} is not a forge this install "
                f"serves. Configured: {known}."
            )
        return forge, forge.parse(url)
