"""The one way the health job talks to GitHub: `gh api`, best-effort.

gate_comment.py (the comment on a red pull request) and gate_issue.py (the
tracking issue an OUTAGE files) both write to GitHub with the workflow's own
GITHUB_TOKEN, through the `gh` binary the runner already has -- the same
call coverage-comment.yml makes for its sticky comment, so no new dependency
and no token handling here: `gh` reads GH_TOKEN from the environment and this
module never sees it.

Every call is best-effort. A failure (no binary, no token, a 4xx, a timeout)
is logged -- the status and gh's first line of stderr, never the body -- and
comes back as None; the callers treat None as "not this tick" and the job
carries on. Nothing here raises past a caller.

`--dry-run` turns writes into a printed description of the request; reads
still happen, so a dry run can say "would edit comment 123" rather than
"would post".
"""

from __future__ import annotations

import json
import subprocess
import sys

GH_BIN = "gh"
DEFAULT_REPO = "gke-labs/kube-agents"
# Where gh reads the token; the workflow sets it from `github.token`.
TOKEN_ENV = "GH_TOKEN"
# One call's ceiling; a hung `gh` must not eat the 30-minute job.
CALL_TIMEOUT_S = 60
# A paginated read comes back one object per line (`--jq '.[]'`), which is
# what stays valid JSON when gh concatenates pages.
PAGINATE_JQ = ".[]"
WRITE_METHODS = ("POST", "PATCH")
DRY_RUN_PREFIX = "--dry-run: would"


def log(message: str) -> None:
    print(message, file=sys.stderr)


class Gh:
    """`gh api` with an injectable runner (tests pass a recording fake)."""

    def __init__(self, repo: str = DEFAULT_REPO, runner=subprocess.run, dry_run: bool = False, binary: str = GH_BIN):
        self.repo = repo
        self.runner = runner
        self.dry_run = dry_run
        self.binary = binary

    def path(self, suffix: str) -> str:
        return f"repos/{self.repo}/{suffix}"

    def call(self, method: str, path: str, body: dict | None = None, paginate: bool = False):
        """The decoded JSON response, a list for a paginated read, or None."""
        if self.dry_run and method in WRITE_METHODS:
            log(f"{DRY_RUN_PREFIX} {method} {path}\n{json.dumps(body, indent=2, ensure_ascii=False) if body else ''}")
            return None
        argv = [self.binary, "api", "-X", method, path]
        if paginate:
            argv += ["--paginate", "--jq", PAGINATE_JQ]
        if body is not None:
            argv += ["--input", "-"]
        try:
            proc = self.runner(
                argv,
                input=json.dumps(body) if body is not None else None,
                capture_output=True,
                text=True,
                timeout=CALL_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            log(f"warning: gh api {method} {path}: {type(exc).__name__}; skipped")
            return None
        if proc.returncode != 0:
            first = (proc.stderr or "").strip().splitlines()
            log(f"warning: gh api {method} {path} failed (exit {proc.returncode}): {first[0] if first else 'no stderr'}")
            return None
        text = proc.stdout or ""
        try:
            if paginate:
                return [json.loads(line) for line in text.splitlines() if line.strip()]
            return json.loads(text) if text.strip() else {}
        except ValueError:
            log(f"warning: gh api {method} {path}: unparseable response; skipped")
            return None
