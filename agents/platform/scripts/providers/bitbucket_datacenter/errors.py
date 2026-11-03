#!/usr/bin/env python3
"""The statuses whose shared reading is wrong for Bitbucket Data Center.

401 from the REST API is a stored HTTP access token that expired or was
revoked. An administrator replaces it in the Secret; nothing here refreshes
it. 404 needs no override: the shared 404 text already says that a private
repository the token cannot see looks like a missing one. 400 is a request
whose fields the instance validated and refused, as on GitLab. 409 is a
duplicate pull request or a stale `version`; the shared 409 text says to read
again and retry, which is right, and the package retries a stale `version`
once itself.
"""

from __future__ import annotations

from ..errors import GUIDANCE, Guidance

TOKEN_REFUSED = Guidance(
    401,
    "FORGE_UNAUTHENTICATED",
    "Bitbucket Data Center refused this install's HTTP access token: it has "
    "expired or been revoked. Nothing you can do from here will fix it. An "
    "administrator replaces the token in the Secret named by this forge's "
    "credentialsRef; the next call reads the new one, with no restart.",
)

#: What a clone or a push says when git over HTTPS answers 401: an expired or
#: revoked token, or a repository outside what the token may read.
GIT_REFUSED = (
    "Bitbucket Data Center refused the git request with this install's token. "
    "The token has expired or was revoked, or it cannot see this repository: "
    "a project token reaches its own project only. Check the repository name. "
    "If it is correct, an administrator gives the token access to the "
    "repository, or replaces the token in the Secret named by this forge's "
    "credentialsRef."
)

ERROR_OVERRIDES = {400: GUIDANCE[422], 401: TOKEN_REFUSED}
