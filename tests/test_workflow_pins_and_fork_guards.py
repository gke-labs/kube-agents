"""Every third-party action is SHA-pinned and every credentialed auto-triggered workflow is fork-guarded.

AGENTS.md ("Pull Request Hygiene") states two rules for `.github/workflows/` and
`.agents/rules/github_actions.md` gives each its form and its exemptions:

  - a third-party `uses:` names a 40-character commit SHA with the version in a
    trailing comment (`uses: actions/checkout@3d3c42e... # v7.0.1`), because a
    mutable tag lets a retagged release change what CI runs. Local reusable
    workflows (`./.github/workflows/...`) are exempt.
  - a workflow that starts on its own (`push`, `schedule`, `workflow_run`, ...)
    and needs this repository's credentials carries
    `if: github.repository == 'gke-labs/kube-agents'` on every job, because a
    fork inherits the triggers but none of the secrets, so an unguarded job
    fails there on every sync. A `workflow_call`- or `workflow_dispatch`-only
    workflow needs no guard, and `docs-deploy.yml` is unguarded on purpose so a
    fork can publish its own Pages site.

What already checks part of this: `tests/conformance/test_C_enforcement.py`
(C4) asserts every third-party `uses:` ends in a 40-hex SHA, and
`tests/conformance/test_B_write_path.py` (B4) asserts `github.repository ==` in
the `if:` of every `workflow_run` job. `actionlint.yml` checks syntax only. This
file adds what neither covers: the version comment beside the SHA, a digest on a
`docker://` ref, the fork guard on every job of every auto-triggered credentialed
workflow whatever its trigger, and a currency check on the allowlist of
workflows unguarded by design.

The `docker://` rule is this file's reading of "pin to an immutable reference",
not a clause of the rule file: an image has no commit SHA, and `@sha256:<digest>`
is its immutable form. No workflow uses one today.

The checker below is a set of pure functions over a directory; `FixtureTests`
exercises them against workflows written to a temporary directory, so the checker
is itself tested, and `RealTreeTests` points them at the checkout. The real-tree
test also asserts a floor on how many workflows and third-party `uses:` it
examined, so an empty or partial walk cannot pass.

"Credentialed" is read from the parsed document, never from comments, through
these signals:

  - a `${{ ... }}` expression naming `secrets.<NAME>` for a NAME other than
    `GITHUB_TOKEN` (compared case-insensitively, as Actions does), or naming the
    `vars.` context, which a fork's repository and environments do not carry;
  - `permissions:` at workflow or job level granting `id-token: write`
    (a cloud credential), `actions: write` (dispatching another workflow, which
    is how a scheduler starts a credentialed `workflow_dispatch`-only pipeline
    that the rule exempts from its own guard), or `write-all`;
  - a job bound to an `environment:`, which exists to hold secrets, variables
    and protection rules a fork does not have;
  - a job passing `secrets: inherit` to a reusable workflow.

A workflow that acts with `GITHUB_TOKEN` alone is not credentialed by this
definition even where it carries the guard by convention (the notifiers and
commenters do, so a fork does not open issues on itself); the rule's own
statement is "needs this repository's secrets", and widening scope past
credentials is a policy change this file does not make.

Run:
  python3 -m unittest discover -s tests -p 'test_workflow_pins_and_fork_guards.py' -v
"""

from __future__ import annotations

import re
import tempfile
import textwrap
import unittest
from dataclasses import dataclass, field
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
WORKFLOW_GLOBS = ("*.yml", "*.yaml")

_REPO = "gke-labs/kube-agents"
_GUARD = f"github.repository == '{_REPO}'"

# Triggers that fire without a deliberate dispatch and run with the
# repository's own secrets. `.agents/rules/github_actions.md` names push, a tag,
# schedule, status and workflow_run; `pull_request_target` and `release` share
# the property (the base repository's secrets, no operator starting them) and
# so fall under the rule's reasoning. `pull_request` is deliberately absent: a
# fork's pull requests to itself run with the fork's own, empty, secrets, so a
# guard there changes nothing, and the rule does not list it.
_AUTO_TRIGGERS = {"push", "schedule", "workflow_run", "pull_request_target", "release", "status"}
# Triggers that only fire when a caller or a person deliberately starts the
# workflow; a workflow reachable only through these needs no guard.
_MANUAL_ONLY = {"workflow_call", "workflow_dispatch"}

# Auto-triggered credentialed workflows allowed to run unguarded, each with the
# reason `.agents/rules/github_actions.md` gives. `test_allowlist_is_current`
# fails when an entry no longer exists, is no longer in scope, or has since been
# guarded, so the exemption cannot outlive its reason.
_UNGUARDED_BY_DESIGN = {"docs-deploy.yml": "a fork publishes its own Pages site"}

_SHA_RE = re.compile(r"@[0-9a-f]{40}$")
_VERSION_COMMENT_RE = re.compile(r"#\s*v?\d")
_LOCAL_REF_PREFIX = "./"
_DOCKER_REF_PREFIX = "docker://"
_DOCKER_DIGEST_MARKER = "@sha256:"

# A `uses:` line as written: optional list dash, the ref (optionally quoted),
# then anything after it, which is where the version comment lives. The raw
# line is what carries the comment; PyYAML drops comments, so the parsed
# document supplies the structure and this regex supplies the annotation.
_USES_LINE_RE = re.compile(r"""^\s*-?\s*uses:\s*(?P<quote>["']?)(?P<ref>[^\s"']+)(?P=quote)(?P<rest>.*)$""")

# A `${{ ... }}` expression, and the secrets and vars references inside one.
# Both are matched against string values of the parsed document, so a comment
# or a filename that merely contains the word does not count.
_EXPRESSION_RE = re.compile(r"\$\{\{(?P<body>.*?)\}\}", re.DOTALL)
_SECRET_REF_RE = re.compile(r"\bsecrets\.(?P<name>[A-Za-z_][A-Za-z0-9_]*)")
_VARS_REF_RE = re.compile(r"\bvars\.[A-Za-z_]")
# The token every workflow gets, fork or not; referencing it needs no guard.
# Actions secret names are case-insensitive, so the comparison is upper-cased.
_BUILTIN_SECRET = "GITHUB_TOKEN"

_PERMISSIONS_KEY = "permissions"
# Permission scopes whose `write` marks a job as credentialed: an OIDC token is
# a cloud credential, and `actions: write` lets the job dispatch a workflow that
# holds one.
_CREDENTIAL_SCOPES = ("id-token", "actions")
_WRITE = "write"
_WRITE_ALL = "write-all"
_ENVIRONMENT_KEY = "environment"
_SECRETS_KEY = "secrets"
_SECRETS_INHERIT = "inherit"
_JOBS_KEY = "jobs"
_STEPS_KEY = "steps"
_USES_KEY = "uses"
_IF_KEY = "if"
_ON_KEY = "on"

# Floors for the real-tree test, so an empty or partial walk fails. Each sits
# at roughly two thirds of what the tree carries as this file lands, so a
# workflow removed in ordinary cleanup does not trip it while a walk that lost
# a third of the directory does. Lower them, with the count in the commit, if
# a deliberate removal ever reaches one; raise them as the tree grows.
_MIN_WORKFLOWS = 30
_MIN_THIRD_PARTY_USES = 100


@dataclass
class PinReport:
    violations: list[str] = field(default_factory=list)
    workflows: int = 0
    third_party_uses: int = 0


@dataclass
class GuardReport:
    violations: list[str] = field(default_factory=list)
    workflows: int = 0
    # Auto-triggered credentialed workflows by file name, each mapped to the
    # names of its jobs that lack the guard (empty when every job carries it).
    in_scope: dict[str, list[str]] = field(default_factory=dict)


def _workflow_files(directory: Path) -> list[Path]:
    files: list[Path] = []
    for pattern in WORKFLOW_GLOBS:
        files.extend(directory.glob(pattern))
    return sorted(files)


def _load(path: Path) -> tuple[str, dict | None]:
    """Return the raw text and the parsed mapping, or None when it is not one."""
    text = path.read_text(encoding="utf-8")
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        return text, None
    return text, doc if isinstance(doc, dict) else None


def _jobs(doc: dict) -> dict[str, dict]:
    jobs = doc.get(_JOBS_KEY)
    if not isinstance(jobs, dict):
        return {}
    return {str(name): job for name, job in jobs.items() if isinstance(job, dict)}


def _triggers(doc: dict) -> set[str]:
    # PyYAML reads an unquoted `on:` key as the boolean True (YAML 1.1).
    on = doc.get(_ON_KEY, doc.get(True))
    if isinstance(on, str):
        return {on}
    if isinstance(on, list):
        return {str(item) for item in on}
    if isinstance(on, dict):
        return {str(key) for key in on}
    return set()


def _uses_refs(doc: dict) -> list[tuple[str, str]]:
    """Every `uses:` in the document as (job name, ref), job-level and step-level."""
    refs: list[tuple[str, str]] = []
    for job_name, job in _jobs(doc).items():
        if isinstance(job.get(_USES_KEY), str):
            refs.append((job_name, job[_USES_KEY]))
        steps = job.get(_STEPS_KEY)
        if not isinstance(steps, list):
            continue
        for step in steps:
            if isinstance(step, dict) and isinstance(step.get(_USES_KEY), str):
                refs.append((job_name, step[_USES_KEY]))
    return refs


def _raw_uses_lines(text: str) -> dict[str, list[str]]:
    """Raw `uses:` lines keyed by the ref they name (the comment travels with the line)."""
    lines: dict[str, list[str]] = {}
    for line in text.splitlines():
        match = _USES_LINE_RE.match(line)
        if match:
            lines.setdefault(match.group("ref"), []).append(line.rstrip())
    return lines


def _pin_problem(ref: str, raw_line: str) -> str | None:
    """Why this ref, as written on this line, is not a pin; None when it is one."""
    if ref.startswith(_DOCKER_REF_PREFIX):
        if _DOCKER_DIGEST_MARKER not in ref:
            return f"docker image is not pinned by {_DOCKER_DIGEST_MARKER[1:-1]} digest"
        return None
    if not _SHA_RE.search(ref):
        return "not pinned to a 40-character commit SHA"
    if not _VERSION_COMMENT_RE.search(raw_line):
        return "SHA pin has no version comment on its line"
    return None


def check_pins(directory: Path) -> PinReport:
    """Every third-party `uses:` under `directory` is a SHA pin with a version comment."""
    report = PinReport()
    for path in _workflow_files(directory):
        report.workflows += 1
        text, doc = _load(path)
        if doc is None:
            report.violations.append(f"{path.name}: not a YAML mapping, cannot check uses:")
            continue
        raw_lines = _raw_uses_lines(text)
        for job_name, ref in _uses_refs(doc):
            if ref.startswith(_LOCAL_REF_PREFIX):
                continue
            report.third_party_uses += 1
            lines = raw_lines.get(ref)
            if not lines:
                # Quoted across lines, folded, or otherwise unfindable: a
                # comment cannot be read for it, so it fails rather than skips.
                report.violations.append(
                    f"{path.name}: job {job_name!r} uses {ref!r} on a line this check cannot find"
                )
                continue
            for line in lines:
                problem = _pin_problem(ref, line)
                if problem:
                    report.violations.append(f"{path.name}: job {job_name!r} uses {ref!r}: {problem}")
    return report


def _grants_credential(permissions: object) -> bool:
    if isinstance(permissions, str):
        return permissions == _WRITE_ALL
    if isinstance(permissions, dict):
        return any(permissions.get(scope) == _WRITE for scope in _CREDENTIAL_SCOPES)
    return False


def _strings(node: object):
    """Every string value in the parsed document, depth first; comments are already gone."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _strings(value)
    elif isinstance(node, list):
        for item in node:
            yield from _strings(item)


def _references_credential_context(doc: dict) -> bool:
    """A `${{ }}` expression names a non-builtin secret or the vars context."""
    for value in _strings(doc):
        for expression in _EXPRESSION_RE.finditer(value):
            body = expression.group("body")
            if _VARS_REF_RE.search(body):
                return True
            for secret in _SECRET_REF_RE.finditer(body):
                if secret.group("name").upper() != _BUILTIN_SECRET:
                    return True
    return False


def _is_credentialed(doc: dict) -> bool:
    if _references_credential_context(doc):
        return True
    if _grants_credential(doc.get(_PERMISSIONS_KEY)):
        return True
    for job in _jobs(doc).values():
        if _grants_credential(job.get(_PERMISSIONS_KEY)):
            return True
        if job.get(_ENVIRONMENT_KEY) is not None:
            return True
        if job.get(_SECRETS_KEY) == _SECRETS_INHERIT:
            return True
    return False


def _is_auto_triggered(doc: dict) -> bool:
    # _AUTO_TRIGGERS and _MANUAL_ONLY are disjoint, so a workflow whose triggers
    # are all manual has an empty intersection; no second clause is needed.
    return bool(_triggers(doc) & _AUTO_TRIGGERS)


def _unguarded_jobs(doc: dict) -> list[str]:
    return [name for name, job in _jobs(doc).items() if _GUARD not in str(job.get(_IF_KEY, ""))]


def check_guards(directory: Path) -> GuardReport:
    """Every job of an auto-triggered credentialed workflow carries the fork guard."""
    report = GuardReport()
    for path in _workflow_files(directory):
        report.workflows += 1
        _, doc = _load(path)
        if doc is None:
            report.violations.append(f"{path.name}: not a YAML mapping, cannot check guards")
            continue
        if not (_is_auto_triggered(doc) and _is_credentialed(doc)):
            continue
        unguarded = _unguarded_jobs(doc)
        report.in_scope[path.name] = unguarded
        if path.name in _UNGUARDED_BY_DESIGN:
            continue
        for job_name in unguarded:
            report.violations.append(
                f"{path.name}: job {job_name!r} is auto-triggered and credentialed "
                f"but has no `if: {_GUARD}`"
            )
    return report


def check_allowlist(directory: Path) -> list[str]:
    """Every `_UNGUARDED_BY_DESIGN` entry still exists, is in scope, and is unguarded."""
    in_scope = check_guards(directory).in_scope
    present = {path.name for path in _workflow_files(directory)}
    problems: list[str] = []
    for name, reason in _UNGUARDED_BY_DESIGN.items():
        if name not in present:
            problems.append(f"{name}: allowlisted ({reason}) but no such workflow")
        elif name not in in_scope:
            problems.append(f"{name}: allowlisted ({reason}) but not auto-triggered and credentialed")
        elif not in_scope[name]:
            problems.append(f"{name}: allowlisted ({reason}) but every job is guarded")
    return problems


# --- fixtures ---------------------------------------------------------------

_PINNED_CHECKOUT = "actions/checkout@" + "a" * 40 + " # v4.2.2"

PASSING_WORKFLOW = f"""
name: passing
on:
  push:
    branches: [main]
jobs:
  build:
    if: github.repository == '{_REPO}'
    runs-on: ubuntu-latest
    steps:
      - uses: {_PINNED_CHECKOUT}
      - run: echo "${{{{ secrets.DEPLOY_KEY }}}}"
  call:
    if: github.repository == '{_REPO}'
    uses: ./.github/workflows/reusable.yml
    secrets: inherit
"""

TAG_PINNED_WORKFLOW = """
name: tag
on: pull_request
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
"""

SHA_WITHOUT_COMMENT_WORKFLOW = """
name: no-comment
on: pull_request
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@{sha}
""".format(sha="b" * 40)

DOCKER_TAG_WORKFLOW = """
name: docker-tag
on: pull_request
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: docker://alpine:3.20
"""

DOCKER_DIGEST_WORKFLOW = """
name: docker-digest
on: pull_request
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: docker://alpine@sha256:{digest}
""".format(digest="c" * 64)

JOB_LEVEL_TAG_WORKFLOW = """
name: job-level-tag
on: pull_request
jobs:
  call:
    uses: octo-org/shared/.github/workflows/build.yml@main
"""

SECOND_JOB_UNGUARDED_WORKFLOW = f"""
name: second-job-unguarded
on:
  schedule:
    - cron: "0 3 * * *"
jobs:
  first:
    if: github.repository == '{_REPO}'
    runs-on: ubuntu-latest
    steps:
      - run: echo "${{{{ secrets.DEPLOY_KEY }}}}"
  second:
    needs: first
    runs-on: ubuntu-latest
    steps:
      - run: echo done
"""

DISPATCH_ONLY_WITH_SECRETS_WORKFLOW = """
name: dispatch-only
on:
  workflow_dispatch:
    inputs:
      target:
        type: string
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - run: echo "${{ secrets.DEPLOY_KEY }}"
"""

CALL_ONLY_WITH_ID_TOKEN_WORKFLOW = """
name: call-only
on:
  workflow_call:
jobs:
  deploy:
    runs-on: ubuntu-latest
    permissions:
      id-token: write
    steps:
      - run: echo deploying
"""

PUSH_WITH_INHERIT_UNGUARDED_WORKFLOW = """
name: push-inherit
on: [push]
jobs:
  call:
    uses: ./.github/workflows/reusable.yml
    secrets: inherit
"""

PUSH_WITH_WORKFLOW_ID_TOKEN_UNGUARDED = """
name: push-id-token
on:
  push:
    tags: ["v*"]
permissions:
  id-token: write
jobs:
  publish:
    runs-on: ubuntu-latest
    steps:
      - run: echo publishing
"""

PUSH_WITH_ONLY_GITHUB_TOKEN_WORKFLOW = """
name: push-builtin-token
on:
  push:
    branches: [main]
jobs:
  label:
    runs-on: ubuntu-latest
    steps:
      - run: gh pr edit --add-label ready
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
"""

PUSH_WITH_SECRETS_FILENAME_IN_RUN_WORKFLOW = """
name: push-filename
on:
  push:
    branches: [main]
jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - run: kubectl apply -f tests/fixtures/bad-pull-secrets.yaml
"""

WORKFLOW_RUN_WITH_SECRET_GUARDED = f"""
name: workflow-run
on:
  workflow_run:
    workflows: [ci]
    types: [completed]
jobs:
  notify:
    if: github.repository == '{_REPO}' && github.event.workflow_run.conclusion == 'failure'
    runs-on: ubuntu-latest
    steps:
      - run: |
          curl -H "Authorization: ${{{{ secrets.WEBHOOK }}}}" https://example.invalid
"""


PUSH_WITH_SECRET_ONLY_IN_COMMENT_WORKFLOW = """
name: push-comment
on:
  push:
    branches: [main]
jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      # do not use ${{ secrets.FOO }} here; this job needs no credential
      - run: echo checking
"""

PUSH_WITH_LOWERCASE_GITHUB_TOKEN_WORKFLOW = """
name: push-lowercase-token
on:
  push:
    branches: [main]
jobs:
  label:
    runs-on: ubuntu-latest
    steps:
      - run: gh pr edit --add-label ready
        env:
          GH_TOKEN: ${{ secrets.github_token }}
"""

SCHEDULE_WITH_ENVIRONMENT_UNGUARDED_WORKFLOW = """
name: schedule-environment
on:
  schedule:
    - cron: "0 4 * * *"
jobs:
  resolve:
    runs-on: ubuntu-latest
    environment: nightly
    steps:
      - run: echo resolving
"""

SCHEDULE_WITH_VARS_UNGUARDED_WORKFLOW = """
name: schedule-vars
on:
  schedule:
    - cron: "0 4 * * *"
jobs:
  resolve:
    runs-on: ubuntu-latest
    steps:
      - run: echo "$GH_ORG"
        env:
          GH_ORG: ${{ vars.GH_ORG }}
"""

SCHEDULE_WITH_ACTIONS_WRITE_UNGUARDED_WORKFLOW = """
name: schedule-dispatch
on:
  schedule:
    - cron: "0 4 * * *"
permissions:
  contents: read
jobs:
  dispatch:
    runs-on: ubuntu-latest
    permissions:
      actions: write
    steps:
      - run: gh workflow run pipeline.yml
        env:
          GH_TOKEN: ${{ github.token }}
"""

# One credentialed, unguarded job under a trigger filled in per test, so every
# member of _AUTO_TRIGGERS is shown to put a workflow in scope and every member
# of _MANUAL_ONLY to leave it out.
TRIGGER_TEMPLATE_WORKFLOW = """
name: trigger-{trigger}
on:
  {trigger}:
jobs:
  job:
    runs-on: ubuntu-latest
    steps:
      - run: echo "${{{{ secrets.DEPLOY_KEY }}}}"
"""


def _write_workflows(directory: Path, **workflows: str) -> None:
    for stem, body in workflows.items():
        (directory / f"{stem}.yml").write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")


class FixtureTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def test_passing_workflow_has_no_violations(self) -> None:
        _write_workflows(self.dir, passing=PASSING_WORKFLOW)
        pins = check_pins(self.dir)
        guards = check_guards(self.dir)
        self.assertEqual(pins.violations, [])
        self.assertEqual(pins.workflows, 1)
        self.assertEqual(pins.third_party_uses, 1, "the local reusable call is exempt, the checkout counts")
        self.assertEqual(guards.violations, [])
        self.assertEqual(guards.in_scope, {"passing.yml": []})

    def test_tag_pin_is_a_violation(self) -> None:
        _write_workflows(self.dir, tag=TAG_PINNED_WORKFLOW)
        violations = check_pins(self.dir).violations
        self.assertEqual(len(violations), 1, violations)
        self.assertIn("tag.yml", violations[0])
        self.assertIn("actions/checkout@v4", violations[0])
        self.assertIn("40-character commit SHA", violations[0])

    def test_sha_without_version_comment_is_a_violation(self) -> None:
        _write_workflows(self.dir, nocomment=SHA_WITHOUT_COMMENT_WORKFLOW)
        violations = check_pins(self.dir).violations
        self.assertEqual(len(violations), 1, violations)
        self.assertIn("no version comment", violations[0])

    def test_docker_ref_needs_a_digest(self) -> None:
        _write_workflows(self.dir, tagged=DOCKER_TAG_WORKFLOW, digested=DOCKER_DIGEST_WORKFLOW)
        report = check_pins(self.dir)
        self.assertEqual(len(report.violations), 1, report.violations)
        self.assertIn("tagged.yml", report.violations[0])
        self.assertIn("sha256", report.violations[0])
        self.assertEqual(report.third_party_uses, 2)

    def test_job_level_uses_is_checked(self) -> None:
        _write_workflows(self.dir, joblevel=JOB_LEVEL_TAG_WORKFLOW)
        violations = check_pins(self.dir).violations
        self.assertEqual(len(violations), 1, violations)
        self.assertIn("build.yml@main", violations[0])

    def test_ref_whose_raw_line_is_missing_is_a_violation(self) -> None:
        # A folded scalar parses to the same ref but leaves no single line for
        # the comment to sit on; the check must fail rather than skip it.
        folded = """
        name: folded
        on: pull_request
        jobs:
          build:
            runs-on: ubuntu-latest
            steps:
              - uses: >-
                  actions/checkout@{sha}
        """.format(sha="d" * 40)
        _write_workflows(self.dir, folded=folded)
        violations = check_pins(self.dir).violations
        self.assertEqual(len(violations), 1, violations)
        self.assertIn("cannot find", violations[0])

    def test_unparseable_workflow_is_a_violation(self) -> None:
        (self.dir / "broken.yml").write_text("jobs: [\n", encoding="utf-8")
        self.assertEqual(len(check_pins(self.dir).violations), 1)
        self.assertEqual(len(check_guards(self.dir).violations), 1)

    def test_second_job_without_guard_is_a_violation(self) -> None:
        _write_workflows(self.dir, sched=SECOND_JOB_UNGUARDED_WORKFLOW)
        report = check_guards(self.dir)
        self.assertEqual(len(report.violations), 1, report.violations)
        self.assertIn("job 'second'", report.violations[0])
        self.assertIn(_GUARD, report.violations[0])
        self.assertEqual(report.in_scope, {"sched.yml": ["second"]})

    def test_dispatch_only_workflow_with_secrets_passes(self) -> None:
        _write_workflows(self.dir, dispatch=DISPATCH_ONLY_WITH_SECRETS_WORKFLOW)
        report = check_guards(self.dir)
        self.assertEqual(report.violations, [])
        self.assertEqual(report.in_scope, {})

    def test_call_only_workflow_with_id_token_passes(self) -> None:
        _write_workflows(self.dir, call=CALL_ONLY_WITH_ID_TOKEN_WORKFLOW)
        report = check_guards(self.dir)
        self.assertEqual(report.violations, [])
        self.assertEqual(report.in_scope, {})

    def test_push_workflow_with_secrets_inherit_needs_a_guard(self) -> None:
        _write_workflows(self.dir, inherit=PUSH_WITH_INHERIT_UNGUARDED_WORKFLOW)
        report = check_guards(self.dir)
        self.assertEqual(len(report.violations), 1, report.violations)
        self.assertIn("job 'call'", report.violations[0])

    def test_workflow_level_id_token_counts_as_credentialed(self) -> None:
        _write_workflows(self.dir, idtoken=PUSH_WITH_WORKFLOW_ID_TOKEN_UNGUARDED)
        report = check_guards(self.dir)
        self.assertEqual(len(report.violations), 1, report.violations)
        self.assertIn("job 'publish'", report.violations[0])

    def test_write_all_permissions_count_as_credentialed(self) -> None:
        body = PUSH_WITH_WORKFLOW_ID_TOKEN_UNGUARDED.replace("permissions:\n  id-token: write", "permissions: write-all")
        self.assertIn("write-all", body)
        _write_workflows(self.dir, writeall=body)
        self.assertEqual(len(check_guards(self.dir).violations), 1)

    def test_github_token_alone_is_not_a_credential(self) -> None:
        _write_workflows(self.dir, builtin=PUSH_WITH_ONLY_GITHUB_TOKEN_WORKFLOW)
        report = check_guards(self.dir)
        self.assertEqual(report.violations, [])
        self.assertEqual(report.in_scope, {})

    def test_secrets_in_a_filename_is_not_a_credential(self) -> None:
        _write_workflows(self.dir, filename=PUSH_WITH_SECRETS_FILENAME_IN_RUN_WORKFLOW)
        report = check_guards(self.dir)
        self.assertEqual(report.violations, [])
        self.assertEqual(report.in_scope, {})

    def test_secret_named_only_in_a_comment_is_not_a_credential(self) -> None:
        _write_workflows(self.dir, comment=PUSH_WITH_SECRET_ONLY_IN_COMMENT_WORKFLOW)
        report = check_guards(self.dir)
        self.assertEqual(report.violations, [])
        self.assertEqual(report.in_scope, {})

    def test_builtin_token_is_matched_case_insensitively(self) -> None:
        _write_workflows(self.dir, lower=PUSH_WITH_LOWERCASE_GITHUB_TOKEN_WORKFLOW)
        report = check_guards(self.dir)
        self.assertEqual(report.violations, [])
        self.assertEqual(report.in_scope, {})

    def test_environment_vars_and_actions_write_count_as_credentialed(self) -> None:
        _write_workflows(
            self.dir,
            env=SCHEDULE_WITH_ENVIRONMENT_UNGUARDED_WORKFLOW,
            vars=SCHEDULE_WITH_VARS_UNGUARDED_WORKFLOW,
            dispatch=SCHEDULE_WITH_ACTIONS_WRITE_UNGUARDED_WORKFLOW,
        )
        report = check_guards(self.dir)
        self.assertEqual(sorted(report.in_scope), ["dispatch.yml", "env.yml", "vars.yml"])
        self.assertEqual(len(report.violations), 3, report.violations)

    def test_every_auto_trigger_puts_a_workflow_in_scope(self) -> None:
        for trigger in sorted(_AUTO_TRIGGERS):
            with self.subTest(trigger=trigger):
                _write_workflows(self.dir, wf=TRIGGER_TEMPLATE_WORKFLOW.format(trigger=trigger))
                report = check_guards(self.dir)
                self.assertEqual(report.in_scope, {"wf.yml": ["job"]})
                self.assertEqual(len(report.violations), 1, report.violations)

    def test_every_manual_only_trigger_leaves_a_workflow_out_of_scope(self) -> None:
        for trigger in sorted(_MANUAL_ONLY):
            with self.subTest(trigger=trigger):
                _write_workflows(self.dir, wf=TRIGGER_TEMPLATE_WORKFLOW.format(trigger=trigger))
                report = check_guards(self.dir)
                self.assertEqual(report.in_scope, {})
                self.assertEqual(report.violations, [])

    def test_guard_inside_a_compound_condition_counts(self) -> None:
        _write_workflows(self.dir, wr=WORKFLOW_RUN_WITH_SECRET_GUARDED)
        report = check_guards(self.dir)
        self.assertEqual(report.violations, [])
        self.assertEqual(report.in_scope, {"wr.yml": []})

    def test_allowlisted_file_is_exempt_from_guard_violations(self) -> None:
        # The fixture directory names the file after the real allowlist entry.
        stem = next(iter(_UNGUARDED_BY_DESIGN)).removesuffix(".yml")
        _write_workflows(self.dir, **{stem: PUSH_WITH_WORKFLOW_ID_TOKEN_UNGUARDED})
        report = check_guards(self.dir)
        self.assertEqual(report.violations, [])
        self.assertEqual(report.in_scope, {f"{stem}.yml": ["publish"]})
        self.assertEqual(check_allowlist(self.dir), [])

    def test_stale_allowlist_entry_fails(self) -> None:
        stem = next(iter(_UNGUARDED_BY_DESIGN)).removesuffix(".yml")
        # Missing entirely.
        _write_workflows(self.dir, other=PASSING_WORKFLOW)
        problems = check_allowlist(self.dir)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("no such workflow", problems[0])
        # Present but every job now guarded.
        _write_workflows(self.dir, **{stem: PASSING_WORKFLOW})
        problems = check_allowlist(self.dir)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("every job is guarded", problems[0])
        # Present but no longer auto-triggered and credentialed.
        _write_workflows(self.dir, **{stem: DISPATCH_ONLY_WITH_SECRETS_WORKFLOW})
        problems = check_allowlist(self.dir)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("not auto-triggered and credentialed", problems[0])

    def test_on_forms_string_list_and_mapping(self) -> None:
        self.assertEqual(_triggers(yaml.safe_load("on: push\njobs: {}")), {"push"})
        self.assertEqual(_triggers(yaml.safe_load("on: [push, schedule]\njobs: {}")), {"push", "schedule"})
        self.assertEqual(
            _triggers(yaml.safe_load("on:\n  push:\n    branches: [main]\n  workflow_dispatch:\njobs: {}")),
            {"push", "workflow_dispatch"},
        )
        self.assertEqual(_triggers(yaml.safe_load('"on": push\njobs: {}')), {"push"})
        self.assertEqual(_triggers(yaml.safe_load("jobs: {}")), set())


class RealTreeTests(unittest.TestCase):
    def test_workflows_directory_exists(self) -> None:
        self.assertTrue(WORKFLOWS_DIR.is_dir(), WORKFLOWS_DIR)

    def test_every_third_party_action_is_sha_pinned_with_a_version_comment(self) -> None:
        report = check_pins(WORKFLOWS_DIR)
        self.assertEqual(
            report.violations,
            [],
            "Pin every third-party action to a full commit SHA with the version in a trailing "
            "comment (.agents/rules/github_actions.md):\n  " + "\n  ".join(report.violations),
        )
        self.assertGreaterEqual(report.workflows, _MIN_WORKFLOWS, "the walk examined too few workflows")
        self.assertGreaterEqual(
            report.third_party_uses, _MIN_THIRD_PARTY_USES, "the walk examined too few third-party uses:"
        )

    def test_every_credentialed_auto_triggered_workflow_is_fork_guarded(self) -> None:
        report = check_guards(WORKFLOWS_DIR)
        self.assertEqual(
            report.violations,
            [],
            "Guard every job of an automatically-triggered credentialed workflow with "
            f"`if: {_GUARD}` (.agents/rules/github_actions.md):\n  " + "\n  ".join(report.violations),
        )
        self.assertGreaterEqual(report.workflows, _MIN_WORKFLOWS, "the walk examined too few workflows")
        self.assertTrue(report.in_scope, "no workflow was auto-triggered and credentialed; the scope rule is broken")

    def test_allowlist_is_current(self) -> None:
        self.assertEqual(check_allowlist(WORKFLOWS_DIR), [])


if __name__ == "__main__":
    unittest.main()
