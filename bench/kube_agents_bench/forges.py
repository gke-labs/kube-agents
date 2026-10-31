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
``ledger_issue_contains``, ``forge_issue_state``, ``pull_request_opened`` and
``github_writes`` -- read the forge from ``BENCH_FORGE``: ``github`` (the default, and every pool
project today) or ``gitlab``. The forge decides four things and nothing
else lives here: the grading credential's environment variable, which web
URLs in a reply point at an issue or a proposal of the repository, the API
root those resolve against, and how the agent's own writes are told from a
person's on GitLab, where no ``[bot]`` suffix marks them. One more setting
applies to every forge: ``BENCH_FORGE_CA_FILE``, a CA certificate the checks
trust in addition to the system CAs, for an API host that a private CA
signed.

A GitHub Enterprise Server install is ``BENCH_FORGE=github`` with
``BENCH_GITHUB_HOST`` set to its host. The reply's URLs must then be on that
host, and a github.com URL names nothing. The API the checks read stays
api.github.com unless ``BENCH_GITHUB_API_ROOT`` names another: the eval's
GHES host is a forwarding proxy to github.com (``hack/ghes-proxy``), so the
data is there.

A GitLab repository is its project's full path, ``group/subgroup/project``,
as deep as the groups nest; GitLab's issue and merge-request web URLs put
``/-/`` between that path and the object, and an issue's URL may read
``/-/work_items/<iid>`` (what gitlab.com returns on create since 2025) as
well as ``/-/issues/<iid>``.
"""

from __future__ import annotations

import functools
import os
import re
import ssl
import urllib.parse
from collections.abc import Mapping
from pathlib import Path

__all__ = [
    "FORGES",
    "FORGE_CA_FILE_ENV_VAR",
    "FORGE_ENV_VAR",
    "FORGE_GITHUB",
    "FORGE_GITLAB",
    "GITHUB_API_ROOT",
    "GITHUB_API_ROOT_ENV_VAR",
    "GITHUB_DEFAULT_HOST",
    "GITHUB_HOST_ENV_VAR",
    "GITHUB_TOKEN_ENV_VARS",
    "GITHUB_TOKEN_FILE_ENV_VAR",
    "GITLAB_AGENT_LOGIN_ENV_VAR",
    "GITLAB_DEFAULT_HOST",
    "GITLAB_HOST_ENV_VAR",
    "GITLAB_TOKEN_ENV_VARS",
    "UnknownForge",
    "forge_name",
    "forge_ssl_context",
    "github_api_root",
    "github_host",
    "gitlab_agent_login",
    "gitlab_api_root",
    "gitlab_host",
    "gitlab_project_path",
    "is_gitlab_token_bot",
    "issue_refs",
    "proposal_refs",
    "missing_token_hint",
    "read_token",
    "token_env_vars",
    "token_source",
    "web_host",
]

FORGE_ENV_VAR = "BENCH_FORGE"
FORGE_GITHUB = "github"
FORGE_GITLAB = "gitlab"
FORGES = (FORGE_GITHUB, FORGE_GITLAB)

GITHUB_API_ROOT = "https://api.github.com"
#: The GitHub host the reply's URLs are on: github.com, or a GitHub
#: Enterprise Server's host. It names the web host only; the API stays
#: GITHUB_API_ROOT unless GITHUB_API_ROOT_ENV_VAR is set.
GITHUB_HOST_ENV_VAR = "BENCH_GITHUB_HOST"
GITHUB_DEFAULT_HOST = "github.com"
#: The GitHub REST API root the checks read, ``https://<host>[:port][/path]``,
#: when it is not api.github.com: for example the GHES proxy's
#: ``https://<host>/api/v3``.
GITHUB_API_ROOT_ENV_VAR = "BENCH_GITHUB_API_ROOT"
#: A PEM CA certificate file the checks trust in addition to the system CAs,
#: for a forge API that a private CA signed: a self-managed GitLab, or the
#: GHES proxy through BENCH_GITHUB_API_ROOT.
FORGE_CA_FILE_ENV_VAR = "BENCH_FORGE_CA_FILE"
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
#: A file holding the GitHub grading credential, read on every call and
#: preferred over GITHUB_TOKEN_ENV_VARS while it holds one. The minted
#: installation token lasts an hour from just before devops-bench starts, and
#: a unit whose stack runs longer than that (oobe-first-run-audits' first
#: repetition waits out a fresh install's own first-run stage) would grade
#: with a dead one; hack/ci-eval-pr.sh writes the token here and renews it
#: while the unit runs, which an environment variable read at startup cannot.
GITHUB_TOKEN_FILE_ENV_VAR = "BENCH_GITHUB_TOKEN_FILE"
GITLAB_TOKEN_ENV_VARS = ("BENCH_GITLAB_TOKEN",)

# One path segment of a GitLab project path: a group, a subgroup or the
# project, which may start with `_` or `.`.
_SEGMENT = r"[A-Za-z0-9_.][A-Za-z0-9_.-]*"
# GitHub's two, kept to the segment shape they have always matched: an owner
# or a name starts with a letter or a digit.
_GITHUB_SEGMENT = r"[A-Za-z0-9][A-Za-z0-9_.-]*"


def _github_url_re(host: str, kind: str) -> re.Pattern[str]:
    return re.compile(
        rf"https://{re.escape(host)}/({_GITHUB_SEGMENT}/{_GITHUB_SEGMENT})/{kind}/(\d+)",
        re.IGNORECASE,
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
    # Refused here, where every check already turns UnknownForge into an
    # error, rather than at whichever later read first builds a URL.
    if name == FORGE_GITLAB:
        gitlab_host(environ)
    else:
        github_host(environ)
        github_api_root(environ)
    forge_ssl_context(environ)
    return name


def token_env_vars(forge: str) -> tuple[str, ...]:
    """The environment variables the grading credential is read from, first set wins."""
    return GITLAB_TOKEN_ENV_VARS if forge == FORGE_GITLAB else GITHUB_TOKEN_ENV_VARS


def read_token(forge: str, environ: Mapping[str, str] | None = None) -> str | None:
    """The grading credential: GITHUB_TOKEN_FILE_ENV_VAR's file on GitHub when
    it holds one, else the first of :func:`token_env_vars` that is set. A
    file that is unreadable, not UTF-8, empty, or anything but one line of
    printable ASCII with no whitespace inside falls back to the variables,
    which hold the token the unit started with: such a file cannot be a
    token, and an embedded newline or a non-Latin-1 character would otherwise
    raise out of the HTTP request's header instead of grading."""
    env = _env(environ)
    token = _file_token(forge, env)
    if token:
        return token
    return next((v for v in (env.get(n) for n in token_env_vars(forge)) if v), None)


def _file_token(forge: str, env: Mapping[str, str]) -> str | None:
    """GITHUB_TOKEN_FILE_ENV_VAR's token when it names a file holding one."""
    token_file = env.get(GITHUB_TOKEN_FILE_ENV_VAR)
    if forge == FORGE_GITLAB or not token_file:
        return None
    try:
        token = Path(token_file).read_text(encoding="utf-8").strip()
    except (OSError, ValueError):  # UnicodeDecodeError is a ValueError
        return None
    if token and token.isascii() and token.isprintable() and not any(c.isspace() for c in token):
        return token
    return None


def token_source(forge: str, environ: Mapping[str, str] | None = None) -> str:
    """Names the credential :func:`read_token` returns, for a diagnostic:
    the file when it supplied the token, else the first variable that is set
    (the forge's first variable when none is)."""
    env = _env(environ)
    if _file_token(forge, env):
        return f"the token in the file {GITHUB_TOKEN_FILE_ENV_VAR} names ({env[GITHUB_TOKEN_FILE_ENV_VAR]})"
    names = token_env_vars(forge)
    name = next((n for n in names if env.get(n)), names[0])
    return f"the token in {name}"


def missing_token_hint(forge: str, environ: Mapping[str, str] | None = None) -> str:
    """What to set when :func:`read_token` found nothing, saying so when
    GITHUB_TOKEN_FILE_ENV_VAR is set but its file holds no usable token."""
    env = _env(environ)
    hint = f"set one of {', '.join(token_env_vars(forge))}"
    token_file = env.get(GITHUB_TOKEN_FILE_ENV_VAR)
    if forge != FORGE_GITLAB and token_file:
        hint += (
            f" ({GITHUB_TOKEN_FILE_ENV_VAR} names {token_file}, which was refused: "
            "unreadable, empty, or not one line of printable ASCII)"
        )
    return hint


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


def github_host(environ: Mapping[str, str] | None = None) -> str:
    """The GitHub host from ``BENCH_GITHUB_HOST``, ``github.com`` when unset,
    lower-cased; refused as :class:`UnknownForge` when it is not a bare
    hostname, as ``gitlab_host`` refuses one."""
    host = (_env(environ).get(GITHUB_HOST_ENV_VAR) or GITHUB_DEFAULT_HOST).strip().lower()
    if not _HOST_RE.match(host):
        raise UnknownForge(
            f"{GITHUB_HOST_ENV_VAR}={host!r} is not a bare hostname (no scheme, path "
            "or port, e.g. ghes.example.com); this check could not be evaluated"
        )
    return host


_API_ROOT_RE = re.compile(
    r"^https://[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?(?::\d{1,5})?(?:/[A-Za-z0-9._~%-]+)*\Z"
)


def github_api_root(environ: Mapping[str, str] | None = None) -> str:
    """The GitHub REST API root the checks read: ``BENCH_GITHUB_API_ROOT``
    without a trailing slash, else api.github.com, whatever
    ``BENCH_GITHUB_HOST`` says. Refused as :class:`UnknownForge` when it is
    not an https URL with no query or fragment: the read credential goes to
    whatever it names."""
    raw = (_env(environ).get(GITHUB_API_ROOT_ENV_VAR) or "").strip()
    if not raw:
        return GITHUB_API_ROOT
    root = raw.rstrip("/")
    valid = bool(_API_ROOT_RE.match(root))
    if valid:
        # The regex takes up to five digits; urllib refuses a port over 65535,
        # and the read would then fail with an error that names no setting.
        try:
            urllib.parse.urlsplit(root).port
        except ValueError:
            valid = False
    if not valid:
        raise UnknownForge(
            f"{GITHUB_API_ROOT_ENV_VAR}={raw!r} is not an https URL with a host and an "
            "optional port and path (e.g. https://ghes.example.com/api/v3); this check "
            "could not be evaluated"
        )
    return root


@functools.lru_cache(maxsize=8)
def _ssl_context_for(path: str) -> ssl.SSLContext:
    # A server certificate loads as well as a CA does, and then every read
    # fails on a chain it cannot build, with an error that names no setting.
    # get_ca_certs() lists only CA:TRUE certificates, so an empty context that
    # loaded the file and lists none got no CA from it.
    probe = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    probe.load_verify_locations(cafile=path)
    if not probe.get_ca_certs():
        raise ValueError("it holds no CA certificate (CA:TRUE); give the CA, for example ca.crt, not the server's tls.crt")
    context = ssl.create_default_context()
    context.load_verify_locations(cafile=path)
    return context


def forge_ssl_context(environ: Mapping[str, str] | None = None) -> ssl.SSLContext | None:
    """The TLS context for forge API reads: the system CAs plus the one in
    ``BENCH_FORGE_CA_FILE``, or None (urllib's default) when it is unset.
    Refused as :class:`UnknownForge` when the file cannot be read as a CA
    certificate: a check that silently dropped it would fail every read with
    a certificate error that names no setting."""
    path = (_env(environ).get(FORGE_CA_FILE_ENV_VAR) or "").strip()
    if not path:
        return None
    try:
        return _ssl_context_for(os.path.abspath(os.path.expanduser(path)))
    except (OSError, ssl.SSLError, ValueError) as exc:
        raise UnknownForge(
            f"{FORGE_CA_FILE_ENV_VAR}={path!r} cannot be read as a PEM CA certificate "
            f"({exc}); this check could not be evaluated"
        ) from exc


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
    return gitlab_host(environ) if forge == FORGE_GITLAB else github_host(environ)


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
    full on the forge, first appearance first, each once. On GitHub the URL
    must be on ``BENCH_GITHUB_HOST``: on a GitHub Enterprise Server run a
    github.com URL names nothing, so an agent that wrote to github.com, and
    not to the instance, fails the check."""
    if forge == FORGE_GITLAB:
        return _refs(_gitlab_url_re(gitlab_host(environ), "issues|work_items"), text)
    return _refs(_github_url_re(github_host(environ), "issues"), text)


def proposal_refs(
    text: str, forge: str, environ: Mapping[str, str] | None = None
) -> list[tuple[str, int]]:
    """``(repository, number)`` for every pull request (GitLab: merge
    request) web URL ``text`` carries in full on the forge, on
    ``BENCH_GITHUB_HOST`` for GitHub as :func:`issue_refs` reads it."""
    if forge == FORGE_GITLAB:
        return _refs(_gitlab_url_re(gitlab_host(environ), "merge_requests"), text)
    return _refs(_github_url_re(github_host(environ), "pull"), text)
