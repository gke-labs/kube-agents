# Copyright 2026 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Which forge a case's GitOps repository is on, and how its URLs read.

The checks that grade what a run wrote to its repository --
``ledger_issue_contains``, ``pull_request_opened`` and ``github_writes`` --
read the forge from ``BENCH_FORGE``: ``github`` (the default, and every pool
project today) or ``gitlab``. The forge decides four things and nothing
else lives here: the grading credential's environment variable, which web
URLs in a reply point at an issue or a proposal of the repository, the API
root those resolve against, and how the agent's own writes are told from a
person's on GitLab, where no ``[bot]`` suffix marks them.

A GitLab repository is its project's full path, ``group/subgroup/project``,
as deep as the groups nest; GitLab's issue and merge-request web URLs put
``/-/`` between that path and the object, and an issue's URL may read
``/-/work_items/<iid>`` (what gitlab.com returns on create since 2025) as
well as ``/-/issues/<iid>``.
"""

from __future__ import annotations

import os
import re
import urllib.parse
from collections.abc import Mapping

__all__ = [
    "FORGES",
    "FORGE_ENV_VAR",
    "FORGE_GITHUB",
    "FORGE_GITLAB",
    "GITHUB_API_ROOT",
    "GITHUB_TOKEN_ENV_VARS",
    "GITLAB_AGENT_LOGIN_ENV_VAR",
    "GITLAB_DEFAULT_HOST",
    "GITLAB_HOST_ENV_VAR",
    "GITLAB_TOKEN_ENV_VARS",
    "UnknownForge",
    "forge_name",
    "gitlab_agent_login",
    "gitlab_api_root",
    "gitlab_host",
    "gitlab_project_path",
    "is_gitlab_token_bot",
    "issue_refs",
    "proposal_refs",
    "read_token",
    "token_env_vars",
    "web_host",
]

FORGE_ENV_VAR = "BENCH_FORGE"
FORGE_GITHUB = "github"
FORGE_GITLAB = "gitlab"
FORGES = (FORGE_GITHUB, FORGE_GITLAB)

GITHUB_API_ROOT = "https://api.github.com"
#: The GitLab instance, ``gitlab.com`` unless a self-managed one is named.
GITLAB_HOST_ENV_VAR = "BENCH_GITLAB_HOST"
GITLAB_DEFAULT_HOST = "gitlab.com"
#: The agent's GitLab username, for an install whose credential is not a
#: token bot. A GitLab project or group access token writes as a bot user
#: (``project_<id>_bot_<hex>``, ``group_<id>_bot_<hex>``), which the
#: ownership test recognises on its own, as it does a GitHub ``[bot]``. On
#: gitlab.com Free there are no such tokens, and the agent writes as an
#: ordinary account holding a personal access token -- nothing marks it, so
#: the run names it here. It adds to the token-bot rule rather than
#: replacing it. Unset, an ordinary account's merge request in the window
#: makes ``github_writes`` an error, since nothing says whose it is.
GITLAB_AGENT_LOGIN_ENV_VAR = "BENCH_GITLAB_AGENT_LOGIN"
#: A GitLab project or group access token's bot username: ``project_<id>_bot``
#: with a hex suffix (``_<hex>``, current instances) or a number (``<N>``,
#: older ones), and the same for ``group_``. The same rule the broker's own
#: GitLab forge applies. Not matched: a GitLab service account
#: (``service_account_*``), which is an ordinary-looking user an install
#: names through ``BENCH_GITLAB_AGENT_LOGIN`` like any personal account. The
#: user object's ``bot`` field would say more, but GitLab leaves it out of a
#: merge request's ``author``, so the username is what there is.
_GITLAB_TOKEN_BOT_RE = re.compile(r"^(project|group)_\d+_bot(_[0-9a-f]+|\d+)?$")

#: The grading credential per forge, first set wins. GitHub's is the
#: read-scoped installation token ``hack/ci-eval-pr.sh`` mints; GitLab's is a
#: group or project access token with ``read_api`` and the Reporter role.
GITHUB_TOKEN_ENV_VARS = ("BENCH_GITHUB_TOKEN", "GITHUB_TOKEN")
GITLAB_TOKEN_ENV_VARS = ("BENCH_GITLAB_TOKEN",)

# One path segment of a GitLab project path: a group, a subgroup or the
# project, which may start with `_` or `.`.
_SEGMENT = r"[A-Za-z0-9_.][A-Za-z0-9_.-]*"
# GitHub's two, kept to the segment shape they have always matched: an owner
# or a name starts with a letter or a digit.
_GITHUB_SEGMENT = r"[A-Za-z0-9][A-Za-z0-9_.-]*"
_GITHUB_ISSUE_URL_RE = re.compile(
    rf"https://github\.com/({_GITHUB_SEGMENT}/{_GITHUB_SEGMENT})/issues/(\d+)", re.IGNORECASE
)
_GITHUB_PULL_URL_RE = re.compile(
    rf"https://github\.com/({_GITHUB_SEGMENT}/{_GITHUB_SEGMENT})/pull/(\d+)", re.IGNORECASE
)


class UnknownForge(ValueError):
    """``BENCH_FORGE`` names a forge no check reads."""


def _env(environ: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if environ is None else environ


def forge_name(environ: Mapping[str, str] | None = None) -> str:
    """The case's forge from ``BENCH_FORGE``, ``github`` when unset.

    Raises :class:`UnknownForge` for a value it does not know: grading a
    GitLab project against GitHub would read an unrelated repository, or none.
    """
    name = (_env(environ).get(FORGE_ENV_VAR) or FORGE_GITHUB).strip().lower()
    if name not in FORGES:
        raise UnknownForge(
            f"{FORGE_ENV_VAR}={name!r} is not a forge this check reads "
            f"({', '.join(FORGES)}); this check could not be evaluated"
        )
    if name == FORGE_GITLAB:
        # Refused here, where every check already turns UnknownForge into an
        # error, rather than at whichever later read first builds a URL.
        gitlab_host(environ)
    return name


def token_env_vars(forge: str) -> tuple[str, ...]:
    """The environment variables the grading credential is read from, first set wins."""
    return GITLAB_TOKEN_ENV_VARS if forge == FORGE_GITLAB else GITHUB_TOKEN_ENV_VARS


def read_token(forge: str, environ: Mapping[str, str] | None = None) -> str | None:
    env = _env(environ)
    return next((v for v in (env.get(n) for n in token_env_vars(forge)) if v), None)


_HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)*\Z")


def gitlab_host(environ: Mapping[str, str] | None = None) -> str:
    """The GitLab instance's hostname, refused as :class:`UnknownForge` when
    ``BENCH_GITLAB_HOST`` carries a scheme, a path or a port: each would be
    spliced into every API URL and read from somewhere other than the
    instance it names."""
    host = (_env(environ).get(GITLAB_HOST_ENV_VAR) or GITLAB_DEFAULT_HOST).strip()
    if not _HOST_RE.match(host):
        raise UnknownForge(
            f"{GITLAB_HOST_ENV_VAR}={host!r} is not a bare hostname (no scheme, path "
            "or port, e.g. gitlab.example.com); this check could not be evaluated"
        )
    return host


def gitlab_agent_login(environ: Mapping[str, str] | None = None) -> str:
    return (_env(environ).get(GITLAB_AGENT_LOGIN_ENV_VAR) or "").strip()


def is_gitlab_token_bot(user: Mapping[str, object]) -> bool:
    """Whether a GitLab user object is an automation: ``bot: true`` where the
    API includes it, else a token bot's username."""
    if user.get("bot") is True:
        return True
    return bool(_GITLAB_TOKEN_BOT_RE.match(str(user.get("username") or "")))


def web_host(forge: str, environ: Mapping[str, str] | None = None) -> str:
    """The host a reply's URLs must be on: how a reason names the forge."""
    return gitlab_host(environ) if forge == FORGE_GITLAB else "github.com"


def gitlab_api_root(environ: Mapping[str, str] | None = None) -> str:
    return f"https://{gitlab_host(environ)}/api/v4"


def gitlab_project_path(repo: str) -> str:
    """``/projects/<full path, encoded whole>``: GitLab answers an unencoded
    nested path with a bare 404, so the slashes must not survive."""
    return "/projects/" + urllib.parse.quote(repo, safe="")


def _gitlab_url_re(host: str, kinds: str) -> re.Pattern[str]:
    return re.compile(
        rf"https://{re.escape(host)}/({_SEGMENT}(?:/{_SEGMENT})+)/-/(?:{kinds})/(\d+)",
        re.IGNORECASE,
    )


def _refs(pattern: re.Pattern[str], text: str) -> list[tuple[str, int]]:
    seen: list[tuple[str, int]] = []
    for repo, number in pattern.findall(text):
        key = (repo, int(number))
        if key not in seen:
            seen.append(key)
    return seen


def issue_refs(
    text: str, forge: str, environ: Mapping[str, str] | None = None
) -> list[tuple[str, int]]:
    """``(repository, number)`` for every issue web URL ``text`` carries in
    full on the forge, first appearance first, each once."""
    if forge == FORGE_GITLAB:
        return _refs(_gitlab_url_re(gitlab_host(environ), "issues|work_items"), text)
    return _refs(_GITHUB_ISSUE_URL_RE, text)


def proposal_refs(
    text: str, forge: str, environ: Mapping[str, str] | None = None
) -> list[tuple[str, int]]:
    """``(repository, number)`` for every pull request (GitLab: merge
    request) web URL ``text`` carries in full on the forge."""
    if forge == FORGE_GITLAB:
        return _refs(_gitlab_url_re(gitlab_host(environ), "merge_requests"), text)
    return _refs(_GITHUB_PULL_URL_RE, text)
