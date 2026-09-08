#!/usr/bin/env python3
"""The workspace protocol's path validator, for both ends of it.

`content_workspace` states the rule this enforces: reject any path under
`.git`, in both directions, with one validator rather than two that could
disagree about what `manifests/../.git/config` means. That argument does not
stop at the broker. A name the broker validated travels over the protocol and
becomes a write on the reader's filesystem, and the check that matters is the
one next to the effect -- so the reader validates it again, and it has to be
the same check. Two implementations of "is this under `.git`" is the parser
differential the rule was written to avoid, just spread over two containers
instead of two functions.

It lives in its own module because the two ends do not ship the same code. The
broker's `content_workspace` drives git in a tree on its own volume and has no
business in the sandbox image; this file is stdlib-only, has no idea a git
exists, and is on the sandbox's shared-scripts allowlist. `content_workspace`
delegates to it, translating `WorkspaceError` into the `PathRefused` its own
callers are written against, so `content_workspace.repo_relative` keeps working
for everything that already called it.
"""

from __future__ import annotations

import unicodedata
from pathlib import PurePosixPath
from typing import Any

# Codepoints HFS+ drops when it compares names, so `.gi<U+200C>t` opens `.git`
# on a Mac. Git carries its own copy of this list in `is_hfs_dotgit`; this one
# is deliberately not a port of it. See `looks_like_dotgit`.
_HFS_IGNORABLE = {
    0x200C, 0x200D, 0x200E, 0x200F,
    0x202A, 0x202B, 0x202C, 0x202D, 0x202E,
    0x206A, 0x206B, 0x206C, 0x206D, 0x206E, 0x206F,
    0xFEFF,
}

# Unicode's class for a control character: C0, DEL, and C1. `str.isspace()`
# covers only some of them, and the ones it misses -- ESC, and the C1 block --
# are the ones that rewrite a terminal that prints the name back.
_CONTROL_CATEGORY = "Cc"


def looks_like_dotgit(segment: str) -> bool:
    """True for anything a filesystem might open as `.git`.

    This refuses more spellings than git itself accepts, on purpose. Git has two
    functions for the same question -- `is_ntfs_dotgit` and `is_hfs_dotgit` --
    and matching them exactly would mean porting both and then betting the port
    agrees with whichever git version is in the image. That is the bet this
    project keeps losing: a check written against a different parser than the
    enforcer fails silently, and it fails permissive.

    Over-refusing is free here. The repositories this carries hold Kubernetes
    manifests, and none of them contains a file called `.git.` or `git~1`.

    Covered: case (`.GIT`, case-insensitive filesystems fold it); the NTFS 8.3
    short name (`git~1`, and any `git~<n>`); trailing dots and spaces, which
    Windows strips before it opens the name; the NTFS alternate-data-stream
    suffix (`.git::$DATA`); and HFS+ ignorable codepoints anywhere inside.
    """
    text = "".join(ch for ch in segment if ord(ch) not in _HFS_IGNORABLE)
    text = text.split(":", 1)[0]
    text = text.rstrip(". ").lower()
    if text == ".git":
        return True
    # `git~1`, the short name Windows generates for `.git`, and every numbered
    # sibling of it.
    return text.startswith("git~") and text[4:].isdigit()


class WorkspaceError(Exception):
    """A request the broker refuses. Carries the HTTP status to answer with."""

    def __init__(self, message: str, status: int = 400, **fields: Any) -> None:
        super().__init__(message)
        self.status = status
        self.fields = fields


def validate_path(raw: Any) -> str:
    """A repository-relative name, or a refusal. One validator, both directions.

    Refuses, in order: a non-string; an empty name; any control character;
    surrounding whitespace; a backslash separator; an absolute path; any `.` or
    `..` segment; and any segment that spells `.git`. Every rejection is
    outright rather than normalising, because normalising means reimplementing
    another library's edge cases and betting the two agree -- refusing the
    ambiguous form is the rule that does not depend on that bet. Surrounding
    whitespace is in that list for the same reason it would have been stripped:
    ` a.yaml` and `a.yaml` are two names, and deciding they are one is a
    normalisation the enforcer downstream does not make.

    Names travel both ways through this, which is what bounds the strictness.
    `list` and `grep` answer with names read out of a repository nobody here
    chose, and `read` then takes one of those names back -- so a spelling this
    refuses is a file the protocol cannot see, not merely one the agent cannot
    invent. `foo:bar` is such a name: git accepts it, `grep` already passes
    `-z` so that a colon in a name cannot be misread, and a validator that
    called it a drive letter would hide a file from the repository that holds
    it.
    """
    if not isinstance(raw, str):
        raise WorkspaceError("path must be a string")
    text = raw
    if not text:
        raise WorkspaceError("path must not be empty")
    # Ahead of the whitespace check, which `strip` would otherwise answer for
    # a name ending in a newline -- "write it without the whitespace" reads as
    # a formatting nit for a name carrying a control character. Every control
    # character rather than NUL, CR and LF alone: ESC and the C1 block reach a
    # log line and a terminal, and git quotes all of them in a name because
    # none of them is one.
    control = next(
        (ch for ch in text if unicodedata.category(ch) == _CONTROL_CATEGORY), ""
    )
    if control:
        raise WorkspaceError(
            f"path must not contain control characters (found {control!r})"
        )
    if text != text.strip():
        raise WorkspaceError(
            f"path {raw!r} has leading or trailing whitespace; write it without"
        )
    if "\\" in text:
        raise WorkspaceError(f"path {raw!r} must use / as its separator")
    if text.startswith("/"):
        raise WorkspaceError(f"path {raw!r} must be repository-relative, not absolute")
    # Split by hand rather than through PurePosixPath. pathlib *normalises*:
    # it drops `.` segments and collapses `//`, so `./manifests/app.yaml` and
    # `manifests//app.yaml` arrive here looking clean. Normalising is the D15
    # defect -- this validator would be answering a different question from the
    # one the filesystem later answers. Refuse the ambiguous spelling instead.
    parts = text.split("/")
    if not parts:
        raise WorkspaceError("path must not be empty")
    for part in parts:
        if not part:
            raise WorkspaceError(
                f"path {raw!r} has an empty segment; write it without the extra /"
            )
        if part in (".", ".."):
            raise WorkspaceError(f"path {raw!r} must not contain . or .. segments")
        # Every segment, not just the first. A nested `.git` is inert in the
        # outer repository but is a live config directory for anything that
        # later treats that subdirectory as a repository of its own, and the
        # cost of refusing it is zero.
        if looks_like_dotgit(part):
            raise WorkspaceError(
                f"path {raw!r} names a git directory. Nothing the agent authors "
                "belongs there: `.git/config` is where a filter driver, an alias "
                "or a hook path would be defined, and content-passing exists so "
                "that the agent cannot define one."
            )
    return str(PurePosixPath(*parts))
