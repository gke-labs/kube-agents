#!/usr/bin/env python3
"""Answer, in one line, whether a GitOps pull request can be opened at all.

    python3 "$HERMES_HOME"/scripts/gitops_preflight.py [--repo <owner/name or host/path>]

Prompt rules alone did not hold here: asked to install a chart on an install
with no registered repository, workers read the registry, found it empty, and
went on looking for another route until the delegation ceiling. This script
is the authoritative answer every PR-writing skill and SOUL.md run first. It
reads the registry the way the credential broker does
(gitops_workspace.get_managed_repo_entries: the mounted
/etc/gitops/managed_repos file, a mount without the key as known-empty) and
prints exactly one line:

    REGISTERED: <repo> [<repo> ...]                  exit 0
    NO_REGISTERED_REPO: stop now. ...                exit 3
    REPO_UNREACHABLE: <repo>: stop now. ...          exit 4  (with --repo)
    STATE_UNREADABLE: stop now. ...                  exit 5

With --repo, the named repository must be registered and answer the
broker's capabilities call; a repository not in the list is reported as
NO_REGISTERED_REPO for that name. Every non-zero line tells the reader to
stop and block, because that is the whole point of having one.
"""

import argparse
import os
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
VCS_SCRIPT_RELATIVE = Path("skills") / "version-control" / "scripts" / "vcs.py"
DEFAULT_HERMES_HOME = "/opt/data"

REGISTRATION_STEPS = (
    "Register the repository in the PlatformAgent's spec.integration.repositories "
    "(or re-run the installer with --gitops-repo), then ask again."
)
STOP = (
    'stop now. Call kanban_block(kind="needs_input") with this line, change nothing, '
    "and do not look for another route or check again another way."
)


def _vcs_script() -> Path:
    home = Path(os.environ.get("HERMES_HOME") or DEFAULT_HERMES_HOME)
    return home / VCS_SCRIPT_RELATIVE


def _reachable(repo: str) -> tuple[bool, str]:
    """Whether the broker answers its capabilities call for `repo`."""
    try:
        result = subprocess.run(
            [sys.executable, str(_vcs_script()), "capabilities", repo],
            capture_output=True,
            text=True,
            timeout=REACH_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, type(exc).__name__
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        return False, detail[-1] if detail else f"exit {result.returncode}"
    return True, ""


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
    if repo not in repos:
        return EXIT_NO_REGISTERED_REPO, (
            f"NO_REGISTERED_REPO: {repo} is not registered on this install "
            f"(registered: {' '.join(repos)}); {STOP} {REGISTRATION_STEPS}"
        )
    ok, why = _reachable(repo)
    if not ok:
        return EXIT_REPO_UNREACHABLE, (
            f"REPO_UNREACHABLE: {repo}: the broker cannot reach it ({why}); {STOP} "
            "Check the repository exists and the install's forge credential can read it."
        )
    return EXIT_REGISTERED, f"REGISTERED: {repo}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", help="also require this repository to be registered and reachable")
    args = parser.parse_args(argv)
    code, line = preflight(args.repo)
    print(line)
    return code


if __name__ == "__main__":
    sys.exit(main())
