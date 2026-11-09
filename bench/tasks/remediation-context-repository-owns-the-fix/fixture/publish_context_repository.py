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

"""Publish `context-repository/` as the tree of a branch on a GitHub repository.

    python3 publish_context_repository.py <owner>/<name> [<branch>]

The public platform charts repository that
bench/tf/prebuilt/context-repository-owns-the-fix registers for each run
(its `context_repository_*` defaults) holds this directory's files and
nothing else, on the branch the stack's `context_repository_ref` names.
This script makes that tree reproducible: it reads every file under
`context-repository/`, writes them as one commit with no parent through the
Git Data API, and points the branch at it, creating the branch or moving
it; a repository with no commit yet gets one through the contents API first,
which the publish then replaces. Run it with a `gh` login that may write the repository, once when the
repository is created and again after any change to the fixture tree. The
repository itself is created by hand (`gh repo create <owner>/<name>
--public`), as the other public eval fixture repositories were.

The commit replaces the branch's history rather than extending it: the
tree is the fixture, and a diff against an earlier plant is the case's
repository, not this one.
"""

from __future__ import annotations

import base64
import json
import subprocess
import sys
from pathlib import Path

CONTEXT_REPOSITORY_DIR = Path(__file__).resolve().parent / "context-repository"
DEFAULT_BRANCH = "main"
BLOB_MODE = "100644"
COMMIT_MESSAGE = "fixture: the platform charts context repository for remediation-context-repository-owns-the-fix"
USAGE = "usage: publish_context_repository.py <owner>/<name> [<branch>]"
# What gh prints when the Git Data API refuses a repository with no commit (HTTP 409).
EMPTY_REPOSITORY_TEXT = "Git Repository is empty"


def gh_api(method: str, path: str, body: dict | None = None) -> dict:
    """One `gh api` call; the login's token never leaves gh."""
    argv = ["gh", "api", "-X", method, path]
    if body is not None:
        argv += ["--input", "-"]
    done = subprocess.run(argv, input=json.dumps(body) if body is not None else None, capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise SystemExit(f"{method} {path}: {done.stderr.strip()}")
    return json.loads(done.stdout) if done.stdout.strip() else {}


def fixture_files(root: Path = CONTEXT_REPOSITORY_DIR) -> list[tuple[str, bytes]]:
    """`(path relative to the tree, content)` for every file, in path order."""
    return [
        (str(path.relative_to(root)), path.read_bytes())
        for path in sorted(root.rglob("*"))
        if path.is_file()
    ]


def repository_is_empty(repo: str) -> bool:
    """GitHub answers 409 to every Git Data call on a repository with no commit."""
    probe = subprocess.run(["gh", "api", f"repos/{repo}/commits?per_page=1"], capture_output=True, text=True, check=False)
    return probe.returncode != 0 and EMPTY_REPOSITORY_TEXT in probe.stderr


def seed_empty_repository(repo: str, branch: str) -> None:
    """The first commit, through the contents API, which an empty repository accepts; the publish replaces it."""
    relative, content = fixture_files()[0]
    gh_api("PUT", f"repos/{repo}/contents/{relative}", {
        "message": COMMIT_MESSAGE,
        "content": base64.b64encode(content).decode(),
        "branch": branch,
    })


def publish(repo: str, branch: str) -> dict:
    if repository_is_empty(repo):
        seed_empty_repository(repo, branch)
    tree = []
    for relative, content in fixture_files():
        blob = gh_api("POST", f"repos/{repo}/git/blobs", {"content": base64.b64encode(content).decode(), "encoding": "base64"})
        tree.append({"path": relative, "mode": BLOB_MODE, "type": "blob", "sha": blob["sha"]})
    tree_sha = gh_api("POST", f"repos/{repo}/git/trees", {"tree": tree})["sha"]
    commit_sha = gh_api("POST", f"repos/{repo}/git/commits", {"message": COMMIT_MESSAGE, "tree": tree_sha, "parents": []})["sha"]
    ref = f"heads/{branch}"
    probe = subprocess.run(["gh", "api", f"repos/{repo}/git/ref/{ref}"], capture_output=True, text=True, check=False)
    if probe.returncode == 0:
        gh_api("PATCH", f"repos/{repo}/git/refs/{ref}", {"sha": commit_sha, "force": True})
        action = "moved"
    else:
        gh_api("POST", f"repos/{repo}/git/refs", {"ref": f"refs/{ref}", "sha": commit_sha})
        action = "created"
    return {"repository": repo, "branch": branch, "action": action, "commit": commit_sha, "tree": tree_sha, "files": len(tree)}


def main(argv: list[str]) -> int:
    if len(argv) not in (2, 3) or "/" not in argv[1]:
        print(USAGE, file=sys.stderr)
        return 2
    result = publish(argv[1], argv[2] if len(argv) == 3 else DEFAULT_BRANCH)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
