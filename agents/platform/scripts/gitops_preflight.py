#!/usr/bin/env python3
"""Answer, in one line, whether a GitOps pull request can be opened at all.

    python3 "$HERMES_HOME"/scripts/gitops_preflight.py [--repo <owner/name or host/path>]

Prompt rules alone did not hold here: asked to install a chart on an install
with no registered repository, workers read the registry, found it empty, and
went on looking for another route until the delegation ceiling. This script
is the authoritative answer a PR-writing skill runs first. It reads the
registry through gitops_workspace.get_managed_repos, the list of usable
entries the verbs accept (the mounted /etc/gitops/managed_repos file, a
mount without the key as known-empty; a list with no usable entry reads as
empty), and prints exactly one line:

    REGISTERED: <repo> [<repo> ...]                  exit 0
    NO_REGISTERED_REPO: stop now. ...                exit 3
    REPO_UNREACHABLE: <repo>: stop now. ...          exit 4  (with --repo)
    STATE_UNREADABLE: stop now. ...                  exit 5

With --list it prints both registries instead, for reading rather than
gating: `managed_repos` (swept, audited, sent pull requests) and
`context_repos` (read for declared intent only, never written):

    MANAGED: <repo> ... | MANAGED: none
    CONTEXT: <repo>[@<ref>] ... | CONTEXT: none      exit 0, or 5 if unreadable

With --repo, the named repository (any spelling gitops_workspace.qualify
accepts) must be registered and must answer a read the broker serves with
the install's forge credential (`vcs.py proposal list --limit 1`); a
repository not in the list is reported as NO_REGISTERED_REPO for that name. Every non-zero line tells the reader to
stop and block, because that is the whole point of having one.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gitops_workspace  # noqa: E402

EXIT_REGISTERED = 0
EXIT_NO_REGISTERED_REPO = 3
EXIT_REPO_UNREACHABLE = 4
EXIT_STATE_UNREADABLE = 5

REACH_TIMEOUT_SECONDS = 60
# This script ships in <profile>/scripts/; the version-control client in
# <profile>/skills/version-control/scripts/.
VCS_SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "version-control" / "scripts" / "vcs.py"
REACH_PROBE_LIMIT = "1"
URL_SCHEME_SEPARATOR = "://"
NONE = "none"

REGISTRATION_STEPS = (
    "Register the repository in the PlatformAgent's spec.integration.repositories "
    "(or re-run the installer with --gitops-repo), then ask again."
)
STOP = (
    'stop now. Call kanban_block(kind="needs_input") with this line, change nothing, '
    "and do not look for another route or check again another way."
)


def _reachable(repo: str) -> tuple[bool, str]:
    """Whether the broker can read `repo` with the install's forge credential.

    `capabilities` answers without the network, so the probe is a one-item
    proposal list, which the broker serves only by spending the credential.
    """
    if not VCS_SCRIPT.is_file():
        return False, f"the version-control client is missing at {VCS_SCRIPT}"
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
        return False, type(exc).__name__
    if result.returncode != 0:
        return False, _error_of(result)
    return True, ""


def _error_of(result: subprocess.CompletedProcess) -> str:
    """The client's `error` field (it prints one JSON object), else its stderr."""
    try:
        answer = json.loads(result.stdout)
        if isinstance(answer, dict) and answer.get("error"):
            return str(answer["error"])
    except (json.JSONDecodeError, TypeError):
        pass
    detail = (result.stderr or "").strip().splitlines()
    return detail[-1] if detail else f"exit {result.returncode}"


def _context_names(entries: list[dict[str, str]]) -> list[str]:
    """`host/path`, with `@ref` when the entry pins one, for each context entry."""
    names = []
    for entry in entries:
        name = entry["url"].split("://", 1)[-1].rstrip("/")
        ref = entry.get(gitops_workspace.CONTEXT_REF_KEY)
        names.append(f"{name}@{ref}" if ref else name)
    return names


def listing() -> tuple[int, str]:
    """Both registries, two lines."""
    try:
        managed = gitops_workspace.get_managed_repos()
        context = _context_names(gitops_workspace.get_context_repo_entries())
    except Exception as exc:  # noqa: BLE001 - any failure to read is the same answer
        return EXIT_STATE_UNREADABLE, (
            f"STATE_UNREADABLE: the registry could not be read ({exc}); {STOP}"
        )
    return EXIT_REGISTERED, (
        f"MANAGED: {' '.join(managed) or NONE}\nCONTEXT: {' '.join(context) or NONE}"
    )


def preflight(repo: str | None = None) -> tuple[int, str]:
    """The exit code and the one line to print."""
    try:
        repos = gitops_workspace.get_managed_repos()
    except Exception as exc:  # noqa: BLE001 - any failure to read is the same answer
        return EXIT_STATE_UNREADABLE, (
            f"STATE_UNREADABLE: the registry could not be read ({exc}); {STOP}"
        )
    if not repos:
        return EXIT_NO_REGISTERED_REPO, (
            f"NO_REGISTERED_REPO: this install has no registered GitOps repository; "
            f"{STOP} {REGISTRATION_STEPS}"
        )
    if repo is None:
        return EXIT_REGISTERED, "REGISTERED: " + " ".join(repos)
    if URL_SCHEME_SEPARATOR in repo:
        repo = repo.split(URL_SCHEME_SEPARATOR, 1)[1].rstrip("/")
    repo = gitops_workspace.qualify(repo, repos)
    if repo.lower() not in {name.lower() for name in repos}:
        return EXIT_NO_REGISTERED_REPO, (
            f"NO_REGISTERED_REPO: {repo} is not registered on this install "
            f"(registered: {' '.join(repos)}); {STOP} {REGISTRATION_STEPS}"
        )
    ok, why = _reachable(repo)
    if not ok:
        return EXIT_REPO_UNREACHABLE, (
            f"REPO_UNREACHABLE: {repo}: the broker could not read it ({why}); {STOP} "
            "Check the repository exists and the install's forge credential can read it."
        )
    return EXIT_REGISTERED, f"REGISTERED: {repo}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--repo", help="also require this repository to be registered and reachable")
    mode.add_argument("--list", action="store_true", help="print both registries instead of gating")
    args = parser.parse_args(argv)
    code, line = listing() if args.list else preflight(args.repo)
    print(line)
    return code


if __name__ == "__main__":
    sys.exit(main())
