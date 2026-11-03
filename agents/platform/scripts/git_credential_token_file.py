#!/usr/bin/env python3
"""A git credential helper that answers with a token read from a file.

    git -c credential.https://<host>.helper='<this> <token-file> <username>' ...

What `providers.StaticFileCredential.git_config` points git at, for the git
invocations the broker makes on behalf of a forge whose credential is a
long-lived token in a mounted Secret. Git runs it with `get`, `store` or
`erase` appended and the request on stdin; only `get` answers. The file is read
on each call, so a rotated Secret is the next clone's credential.

The token is written to stdout for git and nowhere else -- not to stderr, not
to a log -- and it is never in argv: the arguments are the file's path and the
username, both checked by the credential before git is handed them.

Exits 0 with no output when the file is missing or empty, which git reads as
"this helper has nothing", and the clone fails on its own authentication error
rather than on a traceback from here.
"""

from __future__ import annotations

import sys

#: The username that asks for the token as a bearer, not a password. Kept in
#: step with `providers.credentials.BEARER_GIT_USERNAME` by hand: this script
#: imports nothing of the broker's.
BEARER_USERNAME = "authtype-bearer"


def main(argv: list[str]) -> int:
    if len(argv) != 4 or argv[3] != "get":
        # `store` and `erase` are git offering to remember or forget a
        # credential; this helper's only store is the Secret.
        return 0
    token_path, username = argv[1], argv[2]
    # The request git sends. Only its capabilities matter: git asks this
    # helper only for the host its config key is scoped to.
    request = sys.stdin.read()
    try:
        with open(token_path, encoding="utf-8") as handle:
            token = handle.read().strip()
    except (OSError, UnicodeDecodeError):
        # Unreadable, or not text: nothing to give git, as for an empty file.
        return 0
    # The rule `providers.credentials.is_token` applies, kept in step by hand:
    # this script imports nothing of the broker's.
    if not (token and token.isascii() and token.isprintable() and " " not in token):
        return 0
    if username == BEARER_USERNAME:
        # A forge that takes the token only as a bearer (Bitbucket Data
        # Center's HTTP access tokens refuse Basic with any shared username).
        # Git 2.46 and later offer the `authtype` capability and then send
        # `Authorization: Bearer <credential>`; an older git gets nothing,
        # and its clone fails on its own authentication error.
        if "capability[]=authtype" in request.splitlines():
            sys.stdout.write(f"capability[]=authtype\nauthtype=Bearer\ncredential={token}\n")
        return 0
    sys.stdout.write(f"username={username}\npassword={token}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
