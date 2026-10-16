#!/usr/bin/env python3
"""The two statuses whose shared reading is wrong for GitLab.

401 on GitLab is a stored access token that expired or was revoked. Every
GitLab access token carries an expiry of at most a year, and nothing in this
broker refreshes one: an administrator replaces it in the Secret. The shared
401 guidance says nothing here will fix it, which is true; this says what will,
and where. 404 needs no override: GitLab answers 404 rather than 403 for a
project the token cannot see, and the shared 404 guidance already says a
private repository the credential cannot see looks exactly like that.

400 is where GitLab puts a request whose fields it validated and refused -- a
merge request from a branch that was never pushed, a title left blank -- the
answer GitHub gives as 422. The shared table has no 400, so without this the
caller is told one retry is reasonable for a call that cannot succeed until it
changes what it sends.
"""

from __future__ import annotations

from ..errors import GUIDANCE, Guidance

TOKEN_REFUSED = Guidance(
    401,
    "FORGE_UNAUTHENTICATED",
    "GitLab refused this install's access token: it has expired (GitLab tokens "
    "last at most a year) or been revoked. Nothing you can do from here will "
    "fix it. An administrator replaces the token in the Secret named by this "
    "forge's credentialsRef; the next call reads the new one, with no restart.",
)

ERROR_OVERRIDES = {400: GUIDANCE[422], 401: TOKEN_REFUSED}
