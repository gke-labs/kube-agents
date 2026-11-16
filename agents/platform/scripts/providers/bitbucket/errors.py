#!/usr/bin/env python3
"""The statuses whose shared reading is wrong for Bitbucket Cloud.

401 from the REST API is a stored token that expired or was revoked. Nothing
in this broker refreshes a Bitbucket token: an administrator replaces it in
the Secret. 404 needs no override: the REST API answers 404 for a private
repository that the token cannot see, and the shared 404 text already says
so. Git over HTTPS answers 401 for the same repository, so the clone refusal
(`GIT_REFUSED`) names both causes.

400 is where Bitbucket Cloud puts a request whose fields it validated and
refused, such as a pull request from a branch that does not exist. The
shared table has no 400, so without this the caller is told one retry is
reasonable for a call that cannot succeed until it changes what it sends.
"""

from __future__ import annotations

from ..errors import GUIDANCE, Guidance

TOKEN_REFUSED = Guidance(
    401,
    "FORGE_UNAUTHENTICATED",
    "Bitbucket Cloud refused this install's access token: it has expired or "
    "been revoked. Nothing you can do from here will fix it. An administrator "
    "replaces the token in the Secret named by this forge's credentialsRef; "
    "the next call reads the new one, with no restart.",
)

#: What a clone or a push says when git over HTTPS answers 401. Bitbucket
#: answers 401 there for a repository that the token cannot see, as well as
#: for a token that is no longer valid, so the text names both.
GIT_REFUSED = (
    "Bitbucket Cloud refused the git request with this install's token. The "
    "token has expired or was revoked, or it cannot see this repository: "
    "Bitbucket answers a private repository that a token cannot reach with "
    "the same refusal. Check the repository name. If it is correct, an "
    "administrator gives the token access to the repository, or replaces the "
    "token in the Secret named by this forge's credentialsRef."
)

ERROR_OVERRIDES = {400: GUIDANCE[422], 401: TOKEN_REFUSED}
