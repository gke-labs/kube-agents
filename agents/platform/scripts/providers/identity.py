#!/usr/bin/env python3
"""Which host a repository spec names, and what a legal name looks like.

This is the one question that has to be answered before a forge is chosen, so
it cannot live inside a forge: asking each forge in turn "is this yours?" makes
the answer depend on the order they were registered, and a forge that answers
generously takes URLs belonging to a forge registered later. Instead the host
is extracted here, once, by a function no forge can influence, and the host is
what selects the forge.

The regexes are here for the same reason. A forge validates the *shape* of its
own repository names -- how many segments, in what order -- but every forge in
this design is reached over https and pushes through git, so the characters a
segment may contain and what git will accept as a branch are shared facts. A
forge that shipped its own copy would be a second answer to a question with one
right answer, and the looser of the two is the one an attacker would use.
"""

from __future__ import annotations

import re

# A branch name git will accept and that cannot be read as an option or as
# revision syntax. Deliberately narrower than `git check-ref-format`: every name
# this has to carry is one a person typed.
BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
# A leading dot is legal and in use: the convention of a dot-prefixed
# repository holding an organisation's own metadata is one more than one forge
# has, and refusing it here would make those repositories unreachable. So the
# first character admits a dot, and the lookahead is what keeps that from also
# admitting `.` and `..`. A leading hyphen stays refused -- that is the one
# that reads as an option.
SEGMENT_RE = re.compile(r"^(?!\.\.?$)[A-Za-z0-9.][A-Za-z0-9._-]{0,99}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")

_SCHEMES = ("https://", "http://", "ssh://", "git+ssh://", "git@")


def strip_scheme(url: str) -> str:
    """The URL without whichever transport prefix it carries."""
    text = url.strip()
    for prefix in _SCHEMES:
        text = text.removeprefix(prefix)
    return text


def _without_userinfo(text: str) -> str:
    """`user:secret@host/path` without the credential someone pasted in.

    Applied to the authority only, never the path: a `@` after the first slash
    belongs to a repository name.
    """
    head, slash, rest = text.partition("/")
    if "@" in head:
        head = head.rsplit("@", 1)[1]
    return head + slash + rest


def repository_host(url: str) -> str:
    """The host a repository spec names, lowercased, or "" if it names none.

    The scheme comes off before the split rather than after. Splitting first and
    stripping the pieces reads plausibly and matches nothing: the first segment
    of `https://gitlab.com/acme/infra` is `https:`.

    The order of the two splits that follow is the whole of the function. Taking
    the `:` field first reads `oauth2:token@evil.example` as the host `oauth2`,
    which is not a key in the allowlist and so falls through to the bare-name
    default -- a URL naming one host resolved as some other forge's repository.
    Userinfo comes off first, then the port.
    """
    text = url.strip()
    remainder = strip_scheme(text)
    first = remainder.split("/", 1)[0]
    head = first.rsplit("@", 1)[1] if "@" in first else first
    head = head.split(":", 1)[0].lower()
    # Whether that first segment is a host at all. An explicit scheme settles
    # it; otherwise a dot, a colon or a userinfo marker distinguishes
    # `example.com/acme/infra` from the bare `acme/infra` every skill in this
    # repository writes.
    if remainder != text or "." in head or ":" in first or "@" in first:
        return head
    return ""


def path_segments(url: str, hosts: tuple[str, ...]) -> list[str]:
    """The path of a repository URL, with the host and `.git` suffix removed.

    Every forge's `parse` starts here and differs only in how many segments it
    expects and what it joins them back into. The scp-style `host:owner/name`
    spelling is rewritten to a slash first, so one split serves both forms.

    Userinfo comes off here as well as in `repository_host`, and for a second
    reason: a URL someone pasted a token into must not turn that token into a
    segment, because a segment ends up in a clone URL, in an API path, and in
    whatever the caller is shown when one of those is refused.
    """
    text = _without_userinfo(strip_scheme(url)).removesuffix(".git")
    for host in hosts:
        text = text.replace(f"{host}:", f"{host}/")
    parts = [part for part in text.split("/") if part]
    if parts and parts[0].lower() in hosts:
        parts = parts[1:]
    return parts
