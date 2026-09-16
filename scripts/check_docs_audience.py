#!/usr/bin/env python3
"""Fail when a published site page carries a maintainer identifier.

``docs/site/src/content/docs/`` is for people installing and operating
kube-agents on their own clusters (``.agents/rules/documentation.md``, "Who
the site is for"). Maintainer runbooks drift onto it one paragraph at a time,
and each one brings the identifiers a runbook needs: a workflow secret name, a
GitHub App ID, the Prow project, a service account in a project no reader owns.
This check makes that a CI failure instead of a review-time hope. Three
sources feed it:

* **The denylist** -- ``scripts/docs_audience_denylist.txt``, one regular
  expression per line. Shapes (``secrets.X``) and the few literals a shape
  cannot express live there, so the next identifier is a one-line addition.
* **Project IDs from ``hack/ci-env.sh``** -- every ``export ..PROJECT_ID=``
  line's literal default, read at run time so the denylist never repeats a
  value that script already owns.
* **Service-account emails** -- any ``@<project>.iam.gserviceaccount.com``
  whose project segment is a real-looking ID rather than a placeholder
  (``<project>``, ``${PROJECT_ID}``, ``your-project``) or a Google service
  agent domain (``gcp-sa-*``, ``*-robot``, ``*-system``, ``cloudservices``).

The check also fails when it finds no site page at all, or derives no project
ID from ``hack/ci-env.sh``: either would otherwise turn the gate green with
nothing checked, the moment the site root moves or the export is reworded.

The check prints the file, line, and the shape that matched. It prints the
matched text too: the text is already in a tracked site page, so the log
reveals nothing the tree does not.

Standard library only, so it runs in CI and in a bare clone.

Usage::

    python3 scripts/check_docs_audience.py
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterable
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# The published site: the only tree the audience rule binds.
SITE_ROOT = REPO / "docs" / "site" / "src" / "content" / "docs"
SITE_SUFFIXES = (".md", ".mdx")

# One regular expression per line; `#` lines and blank lines are ignored.
DENYLIST = REPO / "scripts" / "docs_audience_denylist.txt"
DENYLIST_COMMENT_PREFIX = "#"

# The script CI sources before every eval run; its `export` lines carry the
# maintainers' project IDs as literal defaults. Only variables named *PROJECT_ID
# are read: the same file exports the default host-cluster name, which the
# quickstart legitimately shows. Either quote style, a `${VAR:-default}` or a
# bare literal, and a trailing comment are accepted, so a reformat of the line
# does not empty the derived list.
CI_ENV = REPO / "hack" / "ci-env.sh"
CI_ENV_PROJECT_EXPORT_RE = re.compile(
    r"^export\s+[A-Z_]*PROJECT_ID="
    r"(?:[\"']?\$\{[A-Z_]+:-[\"']?(?P<default>[a-z][a-z0-9-]+)[\"']?\}[\"']?"
    r"|[\"']?(?P<literal>[a-z][a-z0-9-]+)[\"']?)"
    r"\s*(?:#.*)?$"
)
# A derived project ID is matched as a whole word, so `evals` inside another
# identifier is not a hit while `<id>-3`, a pool member, still is.
WORD_BOUNDARY = r"\b"

# A service-account email in a real project. The project segment is matched
# case-sensitively in lowercase, which is what keeps `PROJECT_ID`, `<project>`
# and `${PROJECT_ID}` from matching at all; the placeholder words a doc writes
# in lowercase are excused below, as are Google's own service-agent domains
# (the `gcp-sa-*` family and the older per-product ones a GKE prerequisite page
# has to show).
SERVICE_ACCOUNT_RE = re.compile(r"@(?P<project>[a-z][a-z0-9-]+)\.iam\.gserviceaccount\.com")
PLACEHOLDER_PROJECT_RE = re.compile(r"^(?:your-|my-|example|project)")
GOOGLE_AGENT_PROJECT_RE = re.compile(
    r"^(?:gcp-sa-|cloudservices$|cloudbuild$|containerregistry$|[a-z-]+-robot$|[a-z-]+-system$)"
)
SERVICE_ACCOUNT_SHAPE = "service-account email in a non-placeholder project"

# How a denylist line or a derived project ID is named in the report.
DENYLIST_SHAPE = "denylist: {pattern}"
CI_ENV_SHAPE = "project ID exported by hack/ci-env.sh"

# Why the check refuses to report a clean run.
NO_PAGES_ERROR = "no .md/.mdx page found under {root}; the site root moved and this check scans nothing"
NO_PROJECT_ERROR = "no project ID derived from {ci_env}; its PROJECT_ID export changed shape"


class Finding:
    """One match: where it is and which shape caught it."""

    __slots__ = ("path", "line", "shape", "text")

    def __init__(self, path: Path, line: int, shape: str, text: str) -> None:
        self.path = path
        self.line = line
        self.shape = shape
        self.text = text

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Finding({self.path!s}:{self.line}, {self.shape!r}, {self.text!r})"


def load_denylist(path: Path = DENYLIST) -> list[re.Pattern[str]]:
    """Compile every non-comment, non-blank line of the denylist."""
    patterns = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith(DENYLIST_COMMENT_PREFIX):
            continue
        patterns.append(re.compile(line))
    return patterns


def ci_env_project_ids(path: Path = CI_ENV) -> list[str]:
    """Literal project IDs that hack/ci-env.sh exports, in file order."""
    ids: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        match = CI_ENV_PROJECT_EXPORT_RE.match(raw.strip())
        if not match:
            continue
        value = match.group("default") or match.group("literal")
        if value and value not in ids:
            ids.append(value)
    return ids


def service_account_findings(path: Path, text: str) -> list[Finding]:
    """Service-account emails whose project segment is not a placeholder."""
    findings = []
    for number, line in enumerate(text.splitlines(), start=1):
        for match in SERVICE_ACCOUNT_RE.finditer(line):
            project = match.group("project")
            if PLACEHOLDER_PROJECT_RE.match(project) or GOOGLE_AGENT_PROJECT_RE.match(project):
                continue
            findings.append(Finding(path, number, SERVICE_ACCOUNT_SHAPE, match.group(0)))
    return findings


def pattern_findings(path: Path, text: str, shapes: Iterable[tuple[str, re.Pattern[str]]]) -> list[Finding]:
    """Every (shape name, pattern) hit in the text, one Finding per line per shape."""
    findings = []
    for number, line in enumerate(text.splitlines(), start=1):
        for shape, pattern in shapes:
            match = pattern.search(line)
            if match:
                findings.append(Finding(path, number, shape, match.group(0)))
    return findings


def site_pages(root: Path = SITE_ROOT) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.suffix in SITE_SUFFIXES and p.is_file())


def scan(
    root: Path = SITE_ROOT,
    denylist: Path = DENYLIST,
    ci_env: Path = CI_ENV,
) -> list[Finding]:
    """Every finding across the site, for the three sources described above."""
    shapes: list[tuple[str, re.Pattern[str]]] = [
        (DENYLIST_SHAPE.format(pattern=p.pattern), p) for p in load_denylist(denylist)
    ]
    shapes.extend(
        (CI_ENV_SHAPE, re.compile(WORD_BOUNDARY + re.escape(project) + WORD_BOUNDARY))
        for project in ci_env_project_ids(ci_env)
    )

    findings: list[Finding] = []
    for page in site_pages(root):
        text = page.read_text(encoding="utf-8")
        findings.extend(pattern_findings(page, text, shapes))
        findings.extend(service_account_findings(page, text))
    findings.sort(key=lambda f: (str(f.path), f.line, f.shape))
    return findings


def preconditions(root: Path = SITE_ROOT, ci_env: Path = CI_ENV) -> list[str]:
    """Reasons a run cannot be trusted to have checked anything."""
    errors = []
    if not site_pages(root):
        errors.append(NO_PAGES_ERROR.format(root=root))
    if not ci_env_project_ids(ci_env):
        errors.append(NO_PROJECT_ERROR.format(ci_env=ci_env))
    return errors


def display_path(path: Path) -> Path:
    """Repository-relative when the path is inside the repository, otherwise as given."""
    try:
        return path.relative_to(REPO)
    except ValueError:
        return path


def main(
    root: Path = SITE_ROOT,
    denylist: Path = DENYLIST,
    ci_env: Path = CI_ENV,
) -> int:
    errors = preconditions(root, ci_env)
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    findings = scan(root, denylist, ci_env)
    pages = len(site_pages(root))
    if findings:
        print(
            f"{len(findings)} maintainer identifier(s) on the published site "
            f"(rule: .agents/rules/documentation.md; shapes: {display_path(denylist)}). "
            "Move the page to the home the AGENTS.md canonical-home table names, or replace "
            "the value with a placeholder the reader fills in:"
        )
        for finding in findings:
            print(f"  {display_path(finding.path)}:{finding.line}: {finding.shape} -- {finding.text!r}")
        return 1
    print(f"No maintainer identifiers on the {pages} published site pages.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
