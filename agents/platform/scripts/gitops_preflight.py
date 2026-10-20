#!/usr/bin/env python3
"""Answer, in one line, whether a GitOps pull request can be opened at all.

    python3 "$HERMES_HOME"/scripts/gitops_preflight.py [--repo <name or URL> | --list]

Prompt rules alone did not hold here: asked to install a chart on an install
with no registered repository, workers read the registry, found it empty, and
went on looking for another route until the delegation ceiling. This script
is the authoritative answer a PR-writing skill or SOUL.md is to run first. It
reads the registry through gitops_workspace.get_managed_repos, the list of
usable entries the verbs accept: the mounted /etc/gitops/managed_repos file,
a mount without the key as known-empty, and the ConfigMap through kubectl
when there is no mount. A list with no usable entry reads as empty. It
prints exactly one line:

    REGISTERED: <repo> [<repo> ...]                  exit 0
    NO_REGISTERED_REPO: <fact>. <remedy>. Stop now: ...           exit 3
    REPO_UNREACHABLE: <repo>: <fact>. <remedy>. Stop now: ...     exit 4  (with --repo)
    STATE_UNREADABLE: <fact>. Stop now: ...                       exit 5
    BROKER_UNAVAILABLE: <fact>. <remedy>. Stop now: ...           exit 6  (with --repo)

Only REGISTERED (and --list with a usable managed repository) is exit 0.
Every other line tells the reader to stop and block, because that is the whole
point of having one. Each line puts the fact and the remedy first and the
worker's stop last: an operator reads a blocked card's reason, and a clipped
notice keeps its start. A malformed command line
(an empty --repo, --repo with --list) is argparse's exit 2, with no status line.

With --repo, the named repository (a bare path, a clone URL, an scp remote:
anything repo_ref parses, then spelt as the managed list spells it; a GitHub
browser URL counts as its first two path segments) must be registered and
must answer a read the broker serves with the install's forge credential
(`vcs.py proposal list --limit 1`). A name not in the list, or one the broker
says it does not manage (REPOSITORY_NOT_MANAGED), is NO_REGISTERED_REPO. The
forge saying the repository is missing or the credential cannot see it
(FORGE_NOT_FOUND, FORGE_FORBIDDEN, FORGE_UNAUTHENTICATED) is REPO_UNREACHABLE.
Every other failure -- a forge or broker outage, a rate limit, an untrusted
TLS certificate, a broker that does not route the verb, a sandbox missing the
client -- is BROKER_UNAVAILABLE, with the client's reason in parentheses. A
registry whose entries were all dropped as unusable says so and names them,
under NO_REGISTERED_REPO.

With --list it prints both registries instead, for reading rather than
gating: `managed_repos` (swept, audited, sent pull requests) and
`context_repos` (read for declared intent only, never written):

    MANAGED: <repo> ... | none | none usable (<n> registered, unusable: ...)
    CONTEXT: <host/path>[@<ref>] ... | none           exit 0, or 5 if unreadable

With no usable managed repository, a third line follows: the same
NO_REGISTERED_REPO stop the bare form prints, and the exit is 3. Nothing can
be proposed then, so reading the list must not look like an answer.

MANAGED uses the names the verbs accept (bare owner/name on a GitHub-only
install); CONTEXT uses host/path, because context repositories are only ever
read, never passed to a verb. A context entry whose ref was refused prints
as <host/path>(ref-refused:'<ref>',not-searched). Userinfo in a URL is never
printed.
"""

import argparse
import json
import subprocess
import sys
from urllib.parse import urlsplit
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gitops_workspace  # noqa: E402
import repo_ref  # noqa: E402

EXIT_REGISTERED = 0
EXIT_NO_REGISTERED_REPO = 3
EXIT_REPO_UNREACHABLE = 4
EXIT_STATE_UNREADABLE = 5
EXIT_BROKER_UNAVAILABLE = 6

# The forge's answers that are about this repository and this credential
# (providers/errors.py): the repository is missing, or the credential cannot
# see it. Every other failure -- a forge or broker outage, a rate limit, a
# broker that does not route the verb, a sandbox missing the client -- is not
# the repository's, and says so as BROKER_UNAVAILABLE.
REPOSITORY_CODES = frozenset({"FORGE_UNAUTHENTICATED", "FORGE_FORBIDDEN", "FORGE_NOT_FOUND"})
# The broker's own refusal of a repository missing from its registry
# (credential_proxy.py): the sandbox's mounted list and the broker's can
# disagree for a moment after a ConfigMap edit.
NOT_MANAGED_CODE = "REPOSITORY_NOT_MANAGED"
GITHUB_REPOSITORY_DEPTH = 2

REACH_TIMEOUT_SECONDS = 60
# This script ships in <profile>/scripts/; the version-control client in
# <profile>/skills/version-control/scripts/.
VCS_SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "version-control" / "scripts" / "vcs.py"
REACH_PROBE_LIMIT = "1"
OK, REPOSITORY, BROKER, NOT_MANAGED = "ok", "repository", "broker", "not-managed"
URL_SCHEME_SEPARATOR = "://"
USERINFO_SEPARATOR = "@"
NONE = "none"

REGISTRATION_STEPS = (
    "Register the repository in the PlatformAgent's spec.integration.repositories "
    "(or re-run the installer with --gitops-repo), then ask again."
)
# Last on every line: the operator reads a blocked card's reason first, and a
# clipped notice keeps its start, so the fact and the remedy come before the
# worker's instruction.
STOP = (
    "Stop now: run no further commands. Your next call is "
    'kanban_block(kind="needs_input") with this line; change nothing, and do not '
    "look for another route or check again another way."
)


def _reachable(repo: str) -> tuple[str, str]:
    """Whether the broker can read `repo` with the install's forge credential.

    `capabilities` answers without the network, so the probe is a one-item
    proposal list, which the broker serves only by spending the credential.
    """
    if not VCS_SCRIPT.is_file():
        return BROKER, f"the version-control client is missing at {VCS_SCRIPT}"
    try:
        result = subprocess.run(
            [sys.executable, str(VCS_SCRIPT), "proposal", "list",
             "--limit", REACH_PROBE_LIMIT, "--repo", repo],
            capture_output=True,
            text=True,
            timeout=REACH_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return BROKER, f"{type(exc).__name__} running the version-control client: {_one_line(str(exc))}"
    if result.returncode == 0:
        return OK, ""
    code, message = _error_of(result)
    reason = f"{code}: {message}" if code else message
    if code == NOT_MANAGED_CODE:
        return NOT_MANAGED, reason
    return (REPOSITORY if code in REPOSITORY_CODES else BROKER), reason


def _one_line(text: str) -> str:
    """`text` folded onto one line, so the answer stays exactly one line."""
    return " ".join(text.split())


def _error_of(result: subprocess.CompletedProcess) -> tuple[str, str]:
    """The client's `code` and `error` fields (it prints one JSON object), else stderr."""
    try:
        answer = json.loads(result.stdout)
        if isinstance(answer, dict) and answer.get("error"):
            return str(answer.get("code") or ""), str(answer["error"])
    except (json.JSONDecodeError, TypeError):
        pass
    detail = (result.stderr or "").strip().splitlines()
    return "", detail[-1] if detail else f"exit {result.returncode}"


def _normalised(repo: str, repos: list[str]) -> str:
    """`repo` in the spelling the managed list uses, from any form repo_ref parses.

    A clone URL (`https://github.com/o/r.git`), an scp remote
    (`git@github.com:o/r.git`) or a capitalised host all reduce to host and
    path, and `qualify` then spells a GitHub one the way the list does.
    """
    ref = repo_ref.try_parse(repo)
    if ref is not None:
        if ref.is_github:
            # repo_ref.GITHUB_HOSTS already names GitHub's aliases (www., ssh.);
            # spell them as the canonical host. A browser URL (/tree/main,
            # /pull/12) names its first two segments: on GitHub that is the
            # repository.
            ref = repo_ref.RepoRef(
                repo_ref.GITHUB_CANONICAL_HOST,
                "/".join(ref.segments[:GITHUB_REPOSITORY_DEPTH]),
            )
        repo = str(ref)
    return gitops_workspace.qualify(repo, repos)


def _display_name(url: str) -> str:
    """`host/path` for a registered URL, through repo_ref, never with userinfo."""
    ref = repo_ref.try_parse(url)
    if ref is not None:
        try:
            port = urlsplit(url).port if URL_SCHEME_SEPARATOR in url else None
        except ValueError:
            port = None
        # repo_ref drops a port, and a port is often why an entry is unusable.
        return f"{ref.host}:{port}/{ref.path}" if port else str(ref)
    # Unparseable: still never echo userinfo, and keep the answer one line.
    return _one_line(url.split(URL_SCHEME_SEPARATOR, 1)[-1].rsplit(USERINFO_SEPARATOR, 1)[-1])


def _context_names(entries: list[dict[str, str]]) -> list[str]:
    """`host/path`, with `@ref` when the entry pins one, for each context entry."""
    names = []
    for entry in entries:
        name = _display_name(entry["url"])
        ref = entry.get(gitops_workspace.CONTEXT_REF_KEY)
        refused = entry.get(gitops_workspace.CONTEXT_REF_REFUSED_KEY)
        if refused is not None:
            # The declared-intent search skips such an entry; say so rather
            # than print it as if it were read at its default branch.
            names.append(f"{name}(ref-refused:{refused!r},not-searched)")
        else:
            names.append(f"{name}@{ref}" if ref else name)
    return names


def listing() -> tuple[int, str]:
    """Both registries, two lines."""
    try:
        managed = gitops_workspace.get_managed_repos()
        context = _context_names(gitops_workspace.get_context_repo_entries())
    except Exception as exc:  # noqa: BLE001 - any failure to read is the same answer
        return EXIT_STATE_UNREADABLE, (
            f"STATE_UNREADABLE: the registry could not be read ({_one_line(str(exc))}). {STOP}"
        )
    managed_text = " ".join(managed)
    if not managed:
        skipped = _unusable_entries()
        managed_text = f"{NONE} usable ({len(skipped)} registered, unusable: {' '.join(skipped)})" if skipped else NONE
    lines = f"MANAGED: {managed_text}\nCONTEXT: {' '.join(context) or NONE}"
    if not managed:
        # An empty registry is the stop whichever form a worker runs: a
        # "MANAGED: none" read as an answer was followed by a direct
        # `kubectl delete` on #3086's eval (#3077). Built from the read
        # above, so the stop cannot disagree with the MANAGED line.
        return EXIT_NO_REGISTERED_REPO, f"{lines}\n{_empty_registry_line(skipped)}"
    return EXIT_REGISTERED, lines


def _empty_registry_line(skipped: list[str]) -> str:
    """The NO_REGISTERED_REPO line for a registry with no usable entry."""
    if skipped:
        return (
            f"NO_REGISTERED_REPO: {len(skipped)} registered entr"
            f"{'y is' if len(skipped) == 1 else 'ies are'} not usable "
            f"({' '.join(skipped)}): an unsupported type, a port, or a host the "
            "forges do not serve. Fix the entry in the PlatformAgent's "
            f"spec.integration.repositories, then ask again. {STOP}"
        )
    return (
        f"NO_REGISTERED_REPO: this install has no registered GitOps repository. "
        f"{REGISTRATION_STEPS} {STOP}"
    )


def _unusable_entries() -> list[str]:
    """The URLs of registered entries the name builder dropped, if any."""
    try:
        entries = gitops_workspace.get_managed_repo_entries()
    except Exception:  # noqa: BLE001 - the caller already read the list once
        return []
    return [_display_name(entry["url"]) for entry in entries]


def preflight(repo: str | None = None) -> tuple[int, str]:
    """The exit code and the one line to print."""
    try:
        repos = gitops_workspace.get_managed_repos()
    except Exception as exc:  # noqa: BLE001 - any failure to read is the same answer
        return EXIT_STATE_UNREADABLE, (
            f"STATE_UNREADABLE: the registry could not be read ({_one_line(str(exc))}). {STOP}"
        )
    if not repos:
        return EXIT_NO_REGISTERED_REPO, _empty_registry_line(_unusable_entries())
    if repo is None:
        return EXIT_REGISTERED, "REGISTERED: " + " ".join(repos)
    asked = _display_name(repo)
    by_lower = {name.lower(): name for name in repos}
    registered = by_lower.get(_normalised(repo, repos).lower())
    if registered is None:
        return EXIT_NO_REGISTERED_REPO, (
            f"NO_REGISTERED_REPO: {asked} is not registered on this install "
            f"(registered: {' '.join(repos)}). {REGISTRATION_STEPS} {STOP}"
        )
    # From here on the name is the list's own spelling, which is what every
    # later gate (the broker, submit-suggestion) compares against.
    repo = registered
    outcome, why = _reachable(repo)
    if outcome == NOT_MANAGED:
        return EXIT_NO_REGISTERED_REPO, (
            f"NO_REGISTERED_REPO: the broker does not manage {repo} ({why}). "
            f"{REGISTRATION_STEPS} {STOP}"
        )
    if outcome == BROKER:
        return EXIT_BROKER_UNAVAILABLE, (
            f"BROKER_UNAVAILABLE: the broker, the forge or this sandbox could not "
            f"answer for {repo} ({why}). This is not a registration problem; "
            f"the reason in parentheses says what to fix. {STOP}"
        )
    if outcome == REPOSITORY:
        return EXIT_REPO_UNREACHABLE, (
            f"REPO_UNREACHABLE: {repo}: the broker could not read it ({why}). "
            f"Check the repository exists and the install's forge credential can read it. {STOP}"
        )
    return EXIT_REGISTERED, f"REGISTERED: {repo}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--repo", help="also require this repository to be registered and reachable")
    mode.add_argument("--list", action="store_true", help="print both registries instead of gating")
    args = parser.parse_args(argv)
    if args.repo is not None and not args.repo.strip():
        parser.error("--repo needs a repository name or URL")
    code, line = listing() if args.list else preflight(args.repo)
    print(line)
    return code


if __name__ == "__main__":
    sys.exit(main())
