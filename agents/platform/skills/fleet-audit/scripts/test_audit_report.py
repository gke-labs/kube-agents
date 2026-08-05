"""Unit tests for audit_report — the fleet-audit PR harness.

Run:
  python3 -m unittest discover -s agents/platform/skills/fleet-audit/scripts \
      -p 'test_audit_report.py' -v

Stdlib only, matching the other agent-script tests. No gh, gcloud, or GitHub
credentials are required: the validate/render/delta layer is pure, and the two
commands that do touch the network are driven through a single recorded seam
(audit_report.run_cmd) plus stubs for credential minting.
"""

import contextlib
import copy
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from subprocess import CalledProcessError, CompletedProcess
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
# `gitops_workspace` is a shared module now — `submit-suggestion` leases a
# workspace from the same code — so it lives in the Platform Agent scripts
# directory the image stages into /opt. Its own tests live beside it, in
# agents/platform/scripts/test_gitops_workspace.py.
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))

import audit_report  # noqa: E402
import gitops_workspace  # noqa: E402

AUDIT = "compliance-audit"
NOW = datetime(2026, 8, 1, 9, 30, tzinfo=timezone.utc)

# Which SOP owns each stream's check roster. Spelled out rather than derived
# from the audit id so that renaming a file breaks this mapping loudly instead
# of silently skipping the roster-drift check for that stream.
SOP_FILENAMES = {
    "compliance-audit": "compliance_audit_sop.md",
    "security-patch-orchestrator": "security_patch_orchestrator_sop.md",
    "obtainability-audit": "obtainability_audit_sop.md",
    "fleet-wide-cost-analysis": "fleet_wide_cost_analysis_sop.md",
    "fleet-consistency-drift": "fleet_consistency_drift_sop.md",
}

# What GitHub enforces on an issue body, a comment and a pull request body,
# written out here rather than imported. The harness's `MAX_BODY_CHARS` is only
# the harness's *belief* about that number, and a size test asserting a body
# fits under the harness's own belief passes just as happily when the belief is
# wrong: raise the constant to 200,000 and every budget test below stays green
# while every publish 422s — which is the whole failure the budget exists to
# prevent. The real number is not ours to change, so it is a literal, and the
# constant is checked against it once (`test_the_budget_matches_github`).
GITHUB_BODY_LIMIT = 65_536


def render_body(doc, **kwargs):
    """The rendered issue text.

    `render_issue_body` returns a `RenderedIssue` — the text plus the ids it
    actually managed to render — because the delta has to describe what a
    reader can see, not what the audit found. Most assertions here are about
    the prose, so they go through this; the ones that care about omission ask
    for the tuple directly.
    """
    return audit_report.render_issue_body(doc, **kwargs).body


def published_body(doc, **kwargs):
    """The ledger text a *previous* run would have left behind.

    A real ledger is only ever written after `validate_findings` has stamped
    the derived ids into the document, so its hidden `audit-findings` block
    carries four-segment ids. A fixture that renders an unvalidated document
    publishes the bare handles instead — `a`, `b` — which the migration guard
    reads as the old scheme and suppresses the delta over. A delta test
    written against such a body would then pass without a delta ever having
    been computed, which is the opposite of what it claims to check.
    """
    doc = copy.deepcopy(doc)
    audit_report.validate_findings(doc, doc.get("audit", AUDIT))
    return render_body(doc, **kwargs)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def make_finding(
    fid="no-network-policy",
    severity="critical",
    title="Namespace has no NetworkPolicy",
    cluster="prod-us-east",
    namespace="payments",
    obj=None,
    command="kubectl get networkpolicy -n payments",
    excerpt="No resources found in payments namespace.",
    impact="All pod-to-pod traffic in payments is unrestricted.",
    remediation=None,
    recommendation=None,
    check="netpol-missing",
):
    """One finding. `fid` is the handle most tests know it by.

    The `id` here is what the *renderers* read, so `fid` still names the finding
    for every test that builds a document and renders it. `validate_findings`
    is the one caller that overwrites it, because identity is derived from
    `(check, cluster, namespace, object)` — so `obj` defaults to something
    carrying `fid`, keeping two findings that differ only by handle distinct
    under derivation too. A test that cares about the derived spelling asks
    `derived_id(...)` for it rather than hard-coding four segments.
    """
    return {
        "id": fid,
        "check": check,
        "severity": severity,
        "title": title,
        "cluster": cluster,
        "namespace": namespace,
        "object": obj if obj is not None else f"Namespace/{fid}",
        "evidence": {"command": command, "excerpt": excerpt},
        "impact": impact,
        "recommendation": recommendation
        or {
            "action": "Apply a default-deny NetworkPolicy to the payments namespace.",
            "rationale": (
                "Namespace-scoped default-deny is the smallest change that closes "
                "east-west exposure; a mesh AuthorizationPolicy would only cover "
                "injected pods."
            ),
            "risk": (
                "Unlabelled cross-namespace traffic into payments breaks on apply. "
                "Check current flows first."
            ),
        },
        "remediation": remediation
        or {
            "kind": "manifest",
            "path": "clusters/prod-us-east/payments-netpol.yaml",
            "note": "Apply a default-deny NetworkPolicy.",
        },
    }


def derived_id(
    check="netpol-missing", cluster="prod-us-east", namespace="payments", obj=None,
    fid="no-network-policy",
):
    """The id `make_finding` with these arguments will validate to."""
    return audit_report.derive_finding_id(
        {
            "check": check,
            "cluster": cluster,
            "namespace": namespace,
            "object": obj if obj is not None else f"Namespace/{fid}",
        }
    )


def ran(check, cluster="prod-us-east"):
    """One `checks_run` entry: a slug and the command that backs it.

    The command is synthetic, but it has to satisfy `validate_check_command`
    for real — an inspection binary, a target, long enough to be a command —
    because a fixture that produced something the validator rejects would fail
    every test in this file for the same uninformative reason. It varies by
    check and by cluster so a renderer assertion can tell two evidence rows
    apart.
    """
    return {
        "check": check,
        "command": f"kubectl --context {cluster} get {check} --all-namespaces -o json",
    }


def make_doc(findings=None, audit=AUDIT, clusters=None, skipped=None):
    if clusters is None:
        clusters = [
            {
                "name": "prod-us-east",
                "location": "us-east1",
                "project": "acme-prod",
            },
            {
                "name": "stage-eu",
                "location": "europe-west1",
                "project": "acme-stage",
            },
        ]
    # Every cluster ran the full roster unless the test says otherwise. The
    # fixture fills `checks_run` in rather than each call site doing it, because
    # a default of "no checks ran" would turn every unrelated test in this file
    # into a coverage test — and would make a *partial* run the baseline the
    # renderer, the delta and the ledger-closing tests are all written against.
    #
    # A call site that *does* say otherwise may write its `checks_run` as bare
    # slugs — `["netpol-missing"]` — and have them expanded here. The wire
    # format is `{check, command}`, but a coverage test is about which checks
    # ran, and making thirty of those tests spell out a command each would bury
    # the thing they assert. A test that is genuinely about the entry shape
    # assigns `checks_run` after this returns, so nothing expands it.
    full = list(audit_report.audit_checks(audit))

    def with_checks(cluster):
        if not isinstance(cluster, dict):
            return cluster
        name = str(cluster.get("name", "prod-us-east"))
        if "checks_run" not in cluster:
            return {**cluster, "checks_run": [ran(c, name) for c in full]}
        entries = cluster["checks_run"]
        if not isinstance(entries, list):
            return cluster
        return {
            **cluster,
            "checks_run": [
                ran(entry, name) if isinstance(entry, str) else entry
                for entry in entries
            ],
        }

    clusters = [with_checks(cluster) for cluster in clusters]
    return {
        "audit": audit,
        "scope": {
            "clusters": clusters,
            "skipped": skipped if skipped is not None else [],
        },
        "findings": findings if findings is not None else [make_finding()],
    }


THREE_SEVERITIES = [
    make_finding(fid="minor-one", severity="minor", title="Minor one"),
    make_finding(fid="crit-one", severity="critical", title="Crit one"),
    make_finding(fid="major-one", severity="major", title="Major one"),
    make_finding(fid="crit-two", severity="critical", title="Crit two"),
]


class Recorder:
    """Stands in for audit_report.run_cmd, recording every command and replying by rule.

    `failures` maps a command fragment to the return code that command should
    produce: with `check=True` it raises CalledProcessError exactly as
    subprocess would, and with `check=False` it returns the non-zero result.
    Without it every failure path in the harness is untestable, because a
    recorder that always succeeds can only ever exercise the happy path.
    """

    def __init__(self, replies=None, failures=None):
        self.calls: list[list[str]] = []
        self.cwds: list[str | None] = []
        # Body-file contents, one entry per call, None when the call had no
        # `--body-file`. Captured here because the harness unlinks each temp
        # file the moment `gh` returns — read it later and there is nothing to
        # read. See `bodies_for` for why the seam has to exist at all.
        self.bodies: list[str | None] = []
        self.replies = replies or {}
        self.failures = failures or {}
        # `git diff --cached --quiet` is the harness's commit classifier: rc 0
        # is "nothing staged, the fix is already on main", rc 1 is "there is a
        # commit to make". Defaulting to rc 0 like everything else would make
        # every remediation test silently take the no-op path.
        self.staged = True
        # What `git symbolic-ref refs/remotes/origin/HEAD` reports — the base
        # branch every remediation branch is cut from. Set it to a different
        # branch to describe a repository that is not on `main`, or to None to
        # describe a clone with no origin/HEAD recorded (rc 1).
        self.origin_head = "origin/main"

    def __call__(self, cmd, *, check=True, capture=True, cwd=None):
        self.calls.append(list(cmd))
        self.cwds.append(None if cwd is None else str(cwd))
        self.bodies.append(self._read_body_file(cmd))
        joined = " ".join(cmd)
        for key, code in self.failures.items():
            if key in joined:
                if check:
                    raise CalledProcessError(code, cmd, "", "simulated failure")
                return CompletedProcess(cmd, code, "", "simulated failure")
        if "diff --cached --quiet" in joined:
            return CompletedProcess(cmd, 1 if self.staged else 0, "", "")
        if cmd[:2] == ["git", "symbolic-ref"]:
            if not self.origin_head:
                return CompletedProcess(cmd, 1, "", "")
            return CompletedProcess(cmd, 0, self.origin_head + "\n", "")
        self._simulate_clone(cmd)
        for key, payload in self.replies.items():
            if key in joined:
                return CompletedProcess(cmd, 0, payload, "")
        return CompletedProcess(cmd, 0, "", "")

    @staticmethod
    def _simulate_clone(cmd):
        """Make a recorded `git clone` leave a working tree behind.

        `gitops_workspace.ensure_workspace` verifies the clone produced a `.git`
        rather than trusting the exit code, so a recorder that only says "rc 0"
        would trip that guard on every run. Reproducing the one filesystem
        effect the real command has keeps the guard live — a clone the recorder
        is told to fail still leaves nothing, and still raises.
        """
        if cmd[:2] != ["git", "clone"] or len(cmd) < 3:
            return
        destination = Path(cmd[-1])
        (destination / ".git").mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _read_body_file(cmd):
        """The contents of this call's body file, or None if it has none.

        Both spellings: `gh issue/pr create|edit` takes `--body-file`, while
        `gh issue/pr comment` takes `-F`. Recognising only one silently returns
        None for the other, which reads as "nothing was published" — the exact
        blind spot this seam exists to close.
        """
        cmd = list(cmd)
        flag = next((f for f in ("--body-file", "-F") if f in cmd), None)
        if flag is None:
            return None
        index = cmd.index(flag) + 1
        if index >= len(cmd):
            return None
        try:
            return Path(cmd[index]).read_text(encoding="utf-8")
        except OSError:
            return None

    def bodies_for(self, *path):
        """What `gh <path...>` actually published, in order.

        The one seam the suite was missing. Every other assertion checks either
        the *arguments* handed to `gh` or the *return value* of a renderer, and
        nothing checked the wire between them: `_write_temp` could write an
        empty string — blanking every issue, comment and pull request the
        feature exists to produce — and the whole suite stayed green. Anything
        asserting that something was *published* has to come through here.

        Calls with no `--body-file` contribute nothing, so `gh issue edit` for a
        label and `gh issue edit` for a report do not have to be told apart by
        the caller; the length of this list is the number of bodies that
        reached GitHub.
        """
        wanted = ["gh", *path]
        return [
            body
            for call, body in zip(self.calls, self.bodies)
            if call[: len(wanted)] == wanted and body is not None
        ]

    def matching(self, *fragments):
        return [
            call
            for call in self.calls
            if all(fragment in " ".join(call) for fragment in fragments)
        ]

    def gh_calls(self, *path):
        """Every `gh <path...>` call, matched on argv position, not substring.

        `matching` is unsafe for a short fragment like "pr": a temp body file
        named /tmp/tmpri8dla1x.md makes `gh issue create` look like `gh pr
        create`. Anything asserting that a *pull request* was never touched
        has to go through here.
        """
        return [c for c in self.calls if c[: len(path) + 1] == ["gh", *path]]


class BaseTestCase(unittest.TestCase):
    """A temp working tree, patch bookkeeping, and captured stdout/stderr."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp_path = Path(tmp.name)
        self.out = ""
        self.err = ""
        # Both of these outlive a single test. `_WORKSPACE` is a module global
        # that `ensure_workspace` sets and nothing clears, and the base-branch
        # answer is memoised per workspace inside `gitops_workspace`; a
        # developer with GITOPS_BASE_BRANCH exported would move it again.
        # Leaving any of the three alone makes a test's result depend on which
        # tests ran before it.
        audit_report.set_workspace(None)
        self.addCleanup(audit_report.set_workspace, None)
        gitops_workspace.forget_base_branch()
        self.addCleanup(gitops_workspace.forget_base_branch)
        env = patch.dict(os.environ, {"GITOPS_BASE_BRANCH": ""})
        env.start()
        self.addCleanup(env.stop)

    def issue_list(self, number=42, url="https://github.com/acme/fleet/issues/42"):
        return json.dumps([{"number": number, "url": url}])

    def patch_attr(self, name, value):
        """monkeypatch.setattr(audit_report, name, value), undone at teardown."""
        patcher = patch.object(audit_report, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_main(self, argv):
        """Invoke the CLI, capturing stdout/stderr into self.out / self.err."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = audit_report.main(argv)
        self.out = out.getvalue()
        self.err = err.getvalue()
        return code

    def stdout_json(self):
        return json.loads(self.out.strip())

    def write_findings(self, doc):
        path = self.tmp_path / "findings.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        return str(path)

    def touch(self, relative):
        target = self.tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# remediation\n", encoding="utf-8")
        return target

    def run_finish(self, doc, argv_extra=(), audit=AUDIT):
        findings_file = self.write_findings(doc)
        return self.run_main(
            ["finish", "--audit", audit, "--findings-file", findings_file, *argv_extra]
        )

    def git_add_calls(self, recorder):
        # "add" is not at a fixed index: the harness passes git-level flags
        # (--literal-pathspecs) ahead of the subcommand.
        return [c for c in recorder.calls if c[0] == "git" and "add" in c[:3]]


class HarnessTestCase(BaseTestCase):
    """Wires audit_report's I/O seam to a recorder and its two PVC paths to temp dirs.

    The workspace is *not* stubbed out. `ensure_workspace` is the code that was
    missing entirely — nothing in the pod ever cloned the GitOps repository, so
    every git call the harness made ran outside a working tree — and a seam
    that skips it would leave the replacement just as unexercised. It runs for
    real here, against a recorder whose `git clone` materialises a tree, so the
    clone-or-fetch decision and the identity configuration are covered.

    `self.workspace` is where the audit's own files land: a remediation path
    written with `self.touch(...)` has to be under the clone, because that is
    the tree `git add` stages from.
    """

    def setUp(self):
        super().setUp()
        self.harness = Recorder()
        self.gitops_root = self.tmp_path / "gitops"
        # The lease segment is the audit id: each stream gets a private clone so
        # two whose schedules collide cannot branch over each other.
        self.workspace = self.gitops_root / AUDIT / "acme__fleet"
        self.patch_attr("GITOPS_WORKSPACE", str(self.gitops_root))
        self.patch_attr("SCRATCH_DIR", str(self.tmp_path / "scratch"))
        self.patch_attr("run_cmd", self.harness)
        self.patch_attr("refresh_credentials", lambda repo=None: None)
        self.patch_attr("resolve_repo", lambda: "acme/fleet")
        self.patch_attr("repo_root", lambda: self.workspace)
        # Most tests describe a pod that has audited before, so the clone
        # already exists and `ensure_workspace` takes the fetch path. Tests
        # about the first run call `self.unclone()` to remove it.
        (self.workspace / ".git").mkdir(parents=True)
        # Every harness test describes code that runs *after* the workspace was
        # established, so say so. A test that calls `open_remediation_pr`
        # directly and leaves this at None resolves the base branch without ever
        # asking git — it would assert `origin/main` on a repository whose
        # default branch the harness was never consulted about.
        audit_report.set_workspace(self.workspace)

    def unclone(self):
        """Put the workspace back to how a freshly started pod finds it."""
        shutil.rmtree(self.workspace)

    def touch(self, relative):
        """Write a remediation file where the harness will look for it."""
        target = self.workspace / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# remediation\n", encoding="utf-8")
        return target


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


class TestRenderBody(unittest.TestCase):
    def test_renders_scope_findings_and_footer(self):
        doc = make_doc()
        body = render_body(doc, generated_at=NOW)
        self.assertIn("This issue is the ledger for the `compliance-audit` audit", body)
        self.assertIn("## Scope", body)
        self.assertIn("| `prod-us-east` | us-east1 | `acme-prod` |", body)
        self.assertIn("## Findings", body)
        self.assertIn("### Critical (1)", body)
        self.assertIn("kubectl get networkpolicy -n payments", body)
        self.assertIn("No resources found in payments namespace.", body)
        self.assertIn("All pod-to-pod traffic in payments is unrestricted.", body)
        self.assertIn(
            "Generated by the Platform Agent `compliance-audit` watchdog", body
        )
        # The stamp is part of the footer, not a separate emission: a body that
        # ended at the ids would be unjoinable a run later.
        self.assertTrue(
            body.rstrip().endswith(audit_report.delta_block(["no-network-policy"])),
            body[-300:],
        )

    def test_body_explains_how_to_ask_for_a_remediation_pr(self):
        body = render_body(make_doc(), generated_at=NOW)
        self.assertIn("`/remediate <finding-id>`", body)
        self.assertIn("collaborator on this repository", body)

    def test_body_tells_an_agent_reader_not_to_post_the_command(self):
        # The audit agent reads this body, and on issue #29 it took the
        # "comment `/remediate all`" line as an instruction to itself and
        # followed it under its own App credentials. The affordance has to stay
        # for human reviewers, so the body says who it is talking to.
        body = render_body(make_doc(), generated_at=NOW)
        self.assertIn("addressed to human reviewers", body)
        self.assertIn("must never post that command itself", body)

    def test_body_names_no_staged_files(self):
        # The ledger is an issue: it has no diff, so it must never claim one.
        body = render_body(make_doc(), generated_at=NOW)
        self.assertNotIn("Remediation files in this PR", body)

    def test_evidence_command_is_fenced(self):
        body = render_body(make_doc(), generated_at=NOW)
        self.assertIn("```bash\nkubectl get networkpolicy -n payments\n```", body)

    def test_severity_groups_ordered_critical_major_minor(self):
        body = render_body(
            make_doc(findings=THREE_SEVERITIES), generated_at=NOW
        )
        order = [
            body.index("### Critical (2)"),
            body.index("### Major (1)"),
            body.index("### Minor (1)"),
        ]
        self.assertEqual(order, sorted(order))
        self.assertIn("4 findings: 2 critical, 1 major, 1 minor.", body)

    def test_empty_severity_group_is_omitted(self):
        body = render_body(
            make_doc(findings=[make_finding(severity="minor")]),
            generated_at=NOW,
        )
        self.assertIn("### Minor (1)", body)
        self.assertNotIn("### Critical", body)
        self.assertNotIn("### Major", body)

    def test_skipped_clusters_declare_partial_coverage(self):
        doc = make_doc(
            skipped=[{"cluster": "dr-west", "reason": "control plane unreachable"}]
        )
        body = render_body(doc, generated_at=NOW)
        self.assertIn("### Skipped", body)
        self.assertIn("**Coverage is partial.**", body)
        self.assertIn("| `dr-west` | control plane unreachable |", body)

    def test_no_skipped_section_when_none_skipped(self):
        body = render_body(make_doc(), generated_at=NOW)
        self.assertNotIn("### Skipped", body)
        self.assertNotIn("Coverage is partial", body)

    def test_gcloud_remediation_shows_command_and_stages_nothing(self):
        finding = make_finding(
            remediation={
                "kind": "gcloud",
                "note": "gcloud container clusters update prod-us-east --enable-shielded-nodes",
            }
        )
        body = render_body(
            make_doc(findings=[finding]), generated_at=NOW
        )
        self.assertIn("- **Remediation (gcloud):**", body)
        self.assertIn("gcloud container clusters update prod-us-east", body)
        self.assertEqual(audit_report.manifest_paths([finding]), [])

    def test_manifest_remediation_links_the_path(self):
        body = render_body(make_doc(), generated_at=NOW)
        self.assertIn(
            "[`clusters/prod-us-east/payments-netpol.yaml`]"
            "(clusters/prod-us-east/payments-netpol.yaml)",
            body,
        )

    def test_cluster_scoped_finding_renders_without_namespace(self):
        body = render_body(
            make_doc(findings=[make_finding(namespace="", obj="ClusterRole/admin")]),
            generated_at=NOW,
        )
        self.assertIn("_cluster-scoped_", body)

    def test_body_is_deterministic_regardless_of_input_order(self):
        first = render_body(
            make_doc(findings=THREE_SEVERITIES), generated_at=NOW
        )
        second = render_body(
            make_doc(findings=list(reversed(THREE_SEVERITIES))),
            generated_at=NOW,
        )
        self.assertEqual(first, second)

    def test_title_and_commit_subject(self):
        self.assertEqual(
            audit_report.issue_title(AUDIT, THREE_SEVERITIES),
            "[audit] Security & RBAC Posture Audit — 4 findings (2 critical)",
        )
        self.assertEqual(
            audit_report.commit_subject(AUDIT, THREE_SEVERITIES),
            "chore(audit): compliance-audit — 4 findings (2 critical, 1 major, 1 minor)",
        )

    def test_single_finding_is_not_pluralised(self):
        one = [make_finding(fid="only-one")]
        self.assertEqual(
            audit_report.issue_title(AUDIT, one),
            "[audit] Security & RBAC Posture Audit — 1 finding (1 critical)",
        )
        self.assertEqual(
            audit_report.commit_subject(AUDIT, one),
            "chore(audit): compliance-audit — 1 finding (1 critical, 0 major, 0 minor)",
        )
        body = render_body(
            make_doc(findings=one), generated_at=NOW
        )
        self.assertIn("1 finding: 1 critical, 0 major, 0 minor.", body)
        self.assertNotIn("1 findings", body)

    def test_zero_findings_title_is_pluralised(self):
        self.assertEqual(
            audit_report.issue_title(AUDIT, []),
            "[audit] Security & RBAC Posture Audit — 0 findings (0 critical)",
        )

    def test_excerpt_is_trimmed(self):
        long_excerpt = "\n".join(f"line {i}" for i in range(200))
        trimmed = audit_report.trim_excerpt(long_excerpt)
        self.assertLessEqual(trimmed.count("\n"), audit_report.MAX_EXCERPT_LINES)
        self.assertIn("excerpt truncated", trimmed)

    def test_excerpt_containing_a_fence_does_not_break_out(self):
        finding = make_finding(excerpt="```\nnested fence\n```")
        body = render_body(
            make_doc(findings=[finding]), generated_at=NOW
        )
        self.assertIn("````text", body)


# --------------------------------------------------------------------------- #
# Hidden delta block
# --------------------------------------------------------------------------- #


class TestDeltaBlock(unittest.TestCase):
    def test_block_is_sorted_and_exact(self):
        self.assertEqual(
            audit_report.delta_block(["b", "a"]),
            '<!-- audit-findings: ["a","b"] -->\n'
            f"<!-- audit-id-scheme: {audit_report.ID_SCHEME} -->",
        )

    def test_round_trip(self):
        ids = ["zeta", "alpha", "mid"]
        body = render_body(
            make_doc(findings=[make_finding(fid=i) for i in ids]),
            generated_at=NOW,
        )
        self.assertEqual(audit_report.parse_delta_block(body), sorted(ids))

    def test_missing_or_broken_block_parses_as_empty(self):
        self.assertEqual(audit_report.parse_delta_block(""), [])
        self.assertEqual(audit_report.parse_delta_block(None), [])
        self.assertEqual(audit_report.parse_delta_block("no marker here"), [])
        self.assertEqual(
            audit_report.parse_delta_block("<!-- audit-findings: [oops] -->"), []
        )

    def test_compute_delta(self):
        new, resolved = audit_report.compute_delta(["a", "b"], ["b", "c"])
        self.assertEqual(new, ["c"])
        self.assertEqual(resolved, ["a"])

    def test_a_truncated_finding_is_not_announced_as_new_every_run(self):
        # The block records what the body *rendered*, so `new` has to be
        # measured against the same set. Against the full finding list, every
        # finding the budget dropped reads as new every morning, forever.
        new, _ = audit_report.compute_delta(
            previous_ids=["a", "b"],
            rendered_ids=["a", "b"],
            all_current_ids=["a", "b", "truncated"],
        )
        self.assertEqual(new, [])

    def test_a_truncated_finding_is_not_announced_as_resolved(self):
        # It still reproduces; it just did not fit. Calling it resolved claims
        # a fix that never happened, on a finding nobody can see.
        _, resolved = audit_report.compute_delta(
            previous_ids=["a", "b"],
            rendered_ids=["a"],
            all_current_ids=["a", "b"],
        )
        self.assertEqual(resolved, [])

    def test_a_genuinely_absent_finding_is_still_resolved_when_truncation_happens(self):
        _, resolved = audit_report.compute_delta(
            previous_ids=["a", "b", "gone"],
            rendered_ids=["a"],
            all_current_ids=["a", "b"],
        )
        self.assertEqual(resolved, ["gone"])

    def test_a_finding_that_becomes_renderable_is_announced_then(self):
        new, resolved = audit_report.compute_delta(
            previous_ids=["a"],
            rendered_ids=["a", "b"],
            all_current_ids=["a", "b"],
        )
        self.assertEqual(new, ["b"])
        self.assertEqual(resolved, [])


class TestDeltaCommentOrdering(BaseTestCase):
    """The delta comment is the notification that says "look now"."""

    def findings_at(self, spec):
        return [
            make_finding(fid=fid, severity=severity, title=f"{fid} title")
            for fid, severity in spec
        ]

    def test_a_new_critical_survives_the_row_cap(self):
        # Alphabetical order decides what a reader sees by the first letter of
        # an id; on a bad night that keeps fifty minors and drops the criticals.
        cap = audit_report.MAX_DELTA_ROWS
        spec = [(f"a-minor-{i:03d}", "minor") for i in range(cap + 10)]
        spec.append(("z-critical", "critical"))
        findings = self.findings_at(spec)
        comment = audit_report.render_delta_comment(
            AUDIT, sorted(f["id"] for f in findings), [], findings, {}, NOW
        )
        self.assertIn("z-critical", comment)
        self.assertIn(f"**{len(spec)} new**", comment)
        self.assertIn("lower severity first to be cut", comment)

    def test_rows_are_severity_ordered(self):
        findings = self.findings_at(
            [("a", "minor"), ("b", "critical"), ("c", "major")]
        )
        comment = audit_report.render_delta_comment(
            AUDIT, ["a", "b", "c"], [], findings, {}, NOW
        )
        self.assertLess(comment.index("`b`"), comment.index("`c`"))
        self.assertLess(comment.index("`c`"), comment.index("`a`"))

    def test_an_id_with_no_finding_behind_it_does_not_stop_the_notification(self):
        findings = self.findings_at([("b", "critical")])
        comment = audit_report.render_delta_comment(
            AUDIT, ["ghost", "b"], [], findings, {}, NOW
        )
        self.assertIn("`b`", comment)
        self.assertIn("`ghost`", comment)
        self.assertLess(comment.index("`b`"), comment.index("`ghost`"))

    def test_delta_across_two_rendered_runs(self):
        run_one = render_body(
            make_doc(
                findings=[
                    make_finding(fid="a", title="Alpha finding"),
                    make_finding(fid="b", title="Bravo finding"),
                ]
            ),
            generated_at=NOW,
        )
        run_two_doc = make_doc(
            findings=[
                make_finding(fid="b", title="Bravo finding"),
                make_finding(fid="c", title="Charlie finding"),
            ]
        )
        run_two = render_body(run_two_doc, generated_at=NOW)

        previous_ids = audit_report.parse_delta_block(run_one)
        current_ids = audit_report.parse_delta_block(run_two)
        new, resolved = audit_report.compute_delta(previous_ids, current_ids)
        self.assertEqual(new, ["c"])
        self.assertEqual(resolved, ["a"])

        titles = audit_report.parse_finding_titles(run_one)
        self.assertEqual(titles["a"], "Alpha finding")

        comment = audit_report.render_delta_comment(
            AUDIT, new, resolved, run_two_doc["findings"], titles, NOW
        )
        self.assertIn("**1 new**", comment)
        self.assertIn("Charlie finding", comment)
        self.assertIn("**1 resolved**", comment)
        # Resolved findings are named by the title recovered from the old body.
        self.assertIn("Alpha finding", comment)

    def test_no_comment_when_nothing_changed(self):
        self.assertIsNone(audit_report.render_delta_comment(AUDIT, [], [], [], {}, NOW))


# --------------------------------------------------------------------------- #
# Schema validation
# --------------------------------------------------------------------------- #


class TestValidation(unittest.TestCase):
    def test_valid_document_passes(self):
        self.assertEqual(audit_report.validate_findings(make_doc(), AUDIT)["audit"], AUDIT)

    def test_zero_findings_is_valid(self):
        audit_report.validate_findings(make_doc(findings=[]), AUDIT)

    def test_unknown_audit_id_rejected(self):
        with self.assertRaisesRegex(audit_report.ValidationError, "unknown audit id"):
            audit_report.validate_audit_id("not-an-audit")

    def test_audit_id_mismatch_rejected(self):
        with self.assertRaises(audit_report.ValidationError) as exc:
            audit_report.validate_findings(make_doc(audit="obtainability-audit"), AUDIT)
        self.assertIn("audit:", str(exc.exception))
        self.assertIn("obtainability-audit", str(exc.exception))

    def test_empty_scope_clusters_rejected(self):
        with self.assertRaises(audit_report.ValidationError) as exc:
            audit_report.validate_findings(make_doc(clusters=[]), AUDIT)
        self.assertIn("scope.clusters", str(exc.exception))
        self.assertIn("not a clean run", str(exc.exception))

    def test_missing_evidence_command_rejected(self):
        doc = make_doc(findings=[make_finding(), make_finding(fid="second")])
        del doc["findings"][1]["evidence"]["command"]
        with self.assertRaises(audit_report.ValidationError) as exc:
            audit_report.validate_findings(doc, AUDIT)
        self.assertIn("findings[1].evidence.command", str(exc.exception))

    def test_empty_evidence_command_rejected(self):
        doc = make_doc(findings=[make_finding(command="   ")])
        with self.assertRaises(audit_report.ValidationError) as exc:
            audit_report.validate_findings(doc, AUDIT)
        self.assertIn("findings[0].evidence.command", str(exc.exception))
        self.assertIn("dropped, not softened", str(exc.exception))

    def test_two_findings_with_the_same_identity_are_rejected(self):
        """Same (check, cluster, namespace, object) is one finding, said twice.

        Ids are derived, so a collision is no longer a typo in a field the
        worker filled in — it is a claim that one object failed one check in
        two different ways. The ledger cannot carry both under one id, and the
        delta cannot tell them apart, so the run stops and says which pair.
        """
        doc = make_doc(
            findings=[
                make_finding(fid="dupe"),
                make_finding(fid="other"),
                make_finding(fid="dupe"),
            ]
        )
        with self.assertRaises(audit_report.ValidationError) as exc:
            audit_report.validate_findings(doc, AUDIT)
        self.assertIn("findings[2]", str(exc.exception))
        self.assertIn("findings[0]", str(exc.exception))
        self.assertIn(derived_id(fid="dupe"), str(exc.exception))

    def test_manifest_without_path_rejected(self):
        doc = make_doc(
            findings=[make_finding(remediation={"kind": "manifest", "note": "fix it"})]
        )
        with self.assertRaises(audit_report.ValidationError) as exc:
            audit_report.validate_findings(doc, AUDIT)
        self.assertIn("findings[0].remediation.path", str(exc.exception))

    def test_gcloud_with_path_rejected(self):
        doc = make_doc(
            findings=[
                make_finding(
                    remediation={"kind": "gcloud", "path": "a.yaml", "note": "n"}
                )
            ]
        )
        with self.assertRaises(audit_report.ValidationError) as exc:
            audit_report.validate_findings(doc, AUDIT)
        self.assertIn("findings[0].remediation.path", str(exc.exception))

    def test_bad_severity_rejected(self):
        doc = make_doc(findings=[make_finding(severity="catastrophic")])
        with self.assertRaises(audit_report.ValidationError) as exc:
            audit_report.validate_findings(doc, AUDIT)
        self.assertIn("findings[0].severity", str(exc.exception))

    def test_bad_remediation_kind_rejected(self):
        doc = make_doc(
            findings=[make_finding(remediation={"kind": "ansible", "note": "n"})]
        )
        with self.assertRaises(audit_report.ValidationError) as exc:
            audit_report.validate_findings(doc, AUDIT)
        self.assertIn("findings[0].remediation.kind", str(exc.exception))

    def test_empty_namespace_allowed(self):
        audit_report.validate_findings(
            make_doc(findings=[make_finding(namespace="")]), AUDIT
        )

    def test_path_escaping_repo_root_rejected(self):
        for bad in ("../../etc/passwd", "/etc/passwd"):
            with self.subTest(path=bad):
                doc = make_doc(
                    findings=[
                        make_finding(
                            remediation={"kind": "manifest", "path": bad, "note": "n"}
                        )
                    ]
                )
                with self.assertRaises(audit_report.ValidationError) as exc:
                    audit_report.validate_findings(doc, AUDIT)
                self.assertIn("findings[0].remediation.path", str(exc.exception))

    def test_findings_must_be_a_list(self):
        doc = make_doc()
        doc["findings"] = {"nope": True}
        with self.assertRaisesRegex(audit_report.ValidationError, "findings:"):
            audit_report.validate_findings(doc, AUDIT)

    def test_skipped_entry_needs_a_reason(self):
        doc = make_doc(skipped=[{"cluster": "dr-west"}])
        with self.assertRaises(audit_report.ValidationError) as exc:
            audit_report.validate_findings(doc, AUDIT)
        self.assertIn("scope.skipped[0].reason", str(exc.exception))


# --------------------------------------------------------------------------- #
# Derived identity
# --------------------------------------------------------------------------- #


class TestDerivedFindingId(unittest.TestCase):
    """The join key is computed, not written.

    Every test here exists because of one morning: on 2026-08-03 the
    compliance stream found the same nine problems in three consecutive runs
    and spelled them three different ways, so `compute_delta` read the renames
    as fixes and the 16:34 ledger announced four unfixed criticals — three
    internet-reachable control planes among them — as resolved. Nothing in the
    old id grammar was violated; the grammar was just a paragraph of prose
    being re-read by inference each run. These tests hold the properties that
    paragraph was asking for and could not enforce.
    """

    def ids_for(self, findings, audit=AUDIT):
        doc = make_doc(findings=findings, audit=audit)
        return [f["id"] for f in audit_report.validate_findings(doc, audit)["findings"]]

    def test_the_model_supplied_id_is_discarded(self):
        # The worker may write whatever it likes in `id`; nothing reads it.
        # Two findings against different objects both claiming the id `same`
        # come out distinct, which is the property that makes the delta a join
        # on facts rather than on the model's memory of last run's prose.
        got = self.ids_for(
            [
                make_finding(fid="same", obj="Namespace/payments"),
                make_finding(fid="same", obj="Namespace/checkout"),
            ]
        )
        self.assertEqual(
            got,
            [
                "netpol-missing.prod-us-east.payments.namespace-payments",
                "netpol-missing.prod-us-east.payments.namespace-checkout",
            ],
        )

    def test_identity_is_the_four_fields_and_nothing_else(self):
        # Severity is re-judged run to run and the title is prose, so neither
        # may enter the key: the same problem re-rated `major` on Tuesday must
        # not read as Monday's finding resolved and a new one opened.
        monday = make_finding(severity="critical", title="Namespace has no NetworkPolicy")
        tuesday = make_finding(severity="major", title="No NetworkPolicy in payments")
        self.assertEqual(
            audit_report.derive_finding_id(monday),
            audit_report.derive_finding_id(tuesday),
        )

    def test_the_same_finding_derives_the_same_id_whatever_its_position(self):
        first = self.ids_for([make_finding(fid="a"), make_finding(fid="b")])
        second = self.ids_for([make_finding(fid="b"), make_finding(fid="a")])
        self.assertEqual(sorted(first), sorted(second))

    def test_an_absent_namespace_gets_the_sentinel_not_an_empty_segment(self):
        # Cluster-scoped objects are the majority of the compliance roster. The
        # retired SOP offered `_` as the sentinel and, one line later, a
        # sanitiser that mapped `_` to `-`, so both spellings shipped.
        for namespace in ("", "   ", None):
            with self.subTest(namespace=namespace):
                fid = audit_report.derive_finding_id(
                    {
                        "check": "wildcard-rbac",
                        "cluster": "prod-us-east",
                        "namespace": namespace,
                        "object": "ClusterRole/cluster-admin",
                    }
                )
                self.assertEqual(
                    fid, "wildcard-rbac.prod-us-east._.clusterrole-cluster-admin"
                )

    def test_a_dotted_value_cannot_manufacture_a_segment(self):
        # `widgets.example.com` used to split a four-segment id into six, and
        # six segments is exactly the tell `is_legacy_finding_id` reads, so a
        # CRD finding would have looked like the retired scheme forever.
        fid = audit_report.derive_finding_id(
            {
                "check": "crd-drift",
                "cluster": "prod.us.east",
                "namespace": "",
                "object": "CustomResourceDefinition/widgets.example.com",
            }
        )
        self.assertEqual(len(fid.split(".")), audit_report.ID_SEGMENTS)
        self.assertEqual(
            fid, "crd-drift.prod-us-east._.customresourcedefinition-widgets-example-com"
        )

    def test_punctuation_case_and_repeated_separators_normalise(self):
        # `Cluster//foo` and `Cluster/foo` are one object, so they must be one
        # finding; a run of separators that survived would make them two.
        self.assertEqual(
            audit_report.derive_finding_id(
                {
                    "check": "Netpol Missing",
                    "cluster": "PROD-US-East",
                    "namespace": "Payments",
                    "object": "Deployment//api",
                }
            ),
            "netpol-missing.prod-us-east.payments.deployment-api",
        )

    def test_a_rejection_never_sends_the_operator_after_a_derived_string(self):
        # The worker wrote `object`; it has never seen the id. A message about
        # a string it did not produce is a message it cannot act on, so every
        # rejection on this path has to name a field — whether it comes from
        # the field checks or from `validate_finding_id` on the derived id.
        for value in ("-", "...", "///", "   ", "..", "lock", "——"):
            with self.subTest(value=value):
                doc = make_doc(findings=[make_finding(obj=value)])
                try:
                    got = audit_report.validate_findings(doc, AUDIT)
                except audit_report.ValidationError as exc:
                    self.assertIn("object", str(exc))
                else:
                    audit_report.validate_finding_id(
                        got["findings"][0]["id"], "derived"
                    )

    def test_a_name_with_no_letter_or_digit_is_refused_by_field(self):
        for field, kwargs in (("object", {"obj": "///"}), ("cluster", {})):
            with self.subTest(field=field):
                doc = make_doc(findings=[make_finding(**kwargs)])
                if field == "cluster":
                    doc["findings"][0]["cluster"] = "///"
                    doc["scope"]["clusters"][0]["name"] = "///"
                with self.assertRaises(audit_report.ValidationError) as exc:
                    audit_report.validate_findings(doc, AUDIT)
                self.assertIn(field, str(exc.exception))

    def test_a_long_namespace_does_not_collapse_two_objects_into_one(self):
        # RFC 1123 allows a 63-character namespace. Trimming right-to-left —
        # what the retired SOPs asked for — spends the entire allowance on the
        # object, the most distinguishing segment, and lands both of these on
        # the same string: one row in the ledger for two findings.
        namespace = "a" * 63
        ids = [
            audit_report._shorten_id(
                audit_report.derive_finding_id(
                    {
                        "check": "missing-resource-limits",
                        "cluster": "prod-us-east-1-primary",
                        "namespace": namespace,
                        "object": obj,
                    }
                )
            )
            for obj in ("Deployment/api", "Deployment/web")
        ]
        self.assertEqual(len(set(ids)), 2, f"{ids[0]} collided with {ids[1]}")
        for fid in ids:
            with self.subTest(fid=fid):
                self.assertLessEqual(len(fid), audit_report.MAX_FINDING_ID)
                audit_report.validate_finding_id(fid, "derived")
                # The check slug is never trimmed: it is what tells a reader
                # which of the eleven checks fired.
                self.assertTrue(fid.startswith("missing-resource-limits."))

    def test_validation_shortens_before_it_checks(self):
        # The two tests above call `_shorten_id` directly, which leaves the
        # question of whether `validate_findings` actually calls it. Dropping
        # it from that pipeline is not cosmetic: an RFC-1123 namespace overruns
        # the 100-character ceiling on its own, so the derived id fails its own
        # charset rule, `finish` exits 2, and a fleet with real findings
        # publishes nothing at all.
        namespace = "n" * 63
        (fid,) = self.ids_for(
            [make_finding(namespace=namespace, obj="Deployment/api")]
        )
        self.assertGreater(
            len(
                audit_report.derive_finding_id(
                    {
                        "check": "netpol-missing",
                        "cluster": "prod-us-east",
                        "namespace": namespace,
                        "object": "Deployment/api",
                    }
                )
            ),
            audit_report.MAX_FINDING_ID,
            "fixture no longer overruns the cap, so it proves nothing",
        )
        self.assertLessEqual(len(fid), audit_report.MAX_FINDING_ID)
        audit_report.validate_finding_id(fid, "derived")

    def test_shortening_is_stable(self):
        long = {
            "check": "control-plane-authorized-networks",
            "cluster": "prod-us-east-1-primary-failover",
            "namespace": "b" * 63,
            "object": "Deployment/checkout-api-gateway",
        }
        once = audit_report._shorten_id(audit_report.derive_finding_id(long))
        again = audit_report._shorten_id(audit_report.derive_finding_id(dict(long)))
        self.assertEqual(once, again)

    def test_check_must_be_on_the_audits_roster(self):
        # The check slug is the first segment of every id in the stream, so a
        # freelanced one is a whole row of the ledger nothing else can join to.
        doc = make_doc(findings=[make_finding(check="something-i-made-up")])
        with self.assertRaises(audit_report.ValidationError) as exc:
            audit_report.validate_findings(doc, AUDIT)
        self.assertIn("findings[0].check", str(exc.exception))

    def test_check_is_required(self):
        doc = make_doc(findings=[make_finding()])
        del doc["findings"][0]["check"]
        with self.assertRaises(audit_report.ValidationError) as exc:
            audit_report.validate_findings(doc, AUDIT)
        self.assertIn("findings[0].check", str(exc.exception))

    def test_a_derived_check_is_accepted_without_being_on_the_roster(self):
        # `uncohorted` is the drift stream's own verdict about a cluster that
        # matched no cohort. It is a real check as far as identity goes, but it
        # is not something a facet comparison "ran", so it is not on the
        # roster `checks_run` is validated against.
        audit = "fleet-consistency-drift"
        self.assertNotIn("uncohorted", audit_report.audit_checks(audit))
        self.assertIn("uncohorted", audit_report.audit_finding_checks(audit))

    def test_a_second_finding_with_the_same_identity_names_both_indices(self):
        doc = make_doc(
            findings=[make_finding(obj="Namespace/payments")] * 2,
        )
        with self.assertRaises(audit_report.ValidationError) as exc:
            audit_report.validate_findings(doc, AUDIT)
        message = str(exc.exception)
        self.assertIn("findings[1]", message)
        self.assertIn("findings[0]", message)
        # It has to say what to change, and the answer is never "pick another
        # id" any more — there is no id to pick.
        self.assertIn("object", message)


class TestIdSchemeStamp(unittest.TestCase):
    def test_every_delta_block_is_stamped(self):
        block = audit_report.delta_block(["a.b.c.d"])
        self.assertIn(f"<!-- audit-id-scheme: {audit_report.ID_SCHEME} -->", block)
        # The ids and the stamp are one unit: an artifact that carried the
        # first without the second would be unjoinable a run later, and both
        # the ledger and every remediation pull request emit this string.
        self.assertEqual(audit_report.parse_delta_block(block), ["a.b.c.d"])
        self.assertEqual(audit_report.parse_id_scheme(block), audit_report.ID_SCHEME)

    def test_an_unstamped_body_reads_as_scheme_zero(self):
        # Every ledger written before the stamp existed, and anything an
        # operator has edited the stamp out of. Zero is never `ID_SCHEME`, so
        # both land on "cannot be joined", which is the safe answer.
        for body in (
            None,
            "",
            "## Findings\n\n<!-- audit-findings: [\"a.b.c.d\"] -->\n",
        ):
            with self.subTest(body=body):
                self.assertEqual(audit_report.parse_id_scheme(body), 0)
                self.assertNotEqual(audit_report.parse_id_scheme(body), audit_report.ID_SCHEME)

    def test_the_last_stamp_wins(self):
        # Same rule the delta block itself follows: an excerpt pasted from a
        # cluster may contain anything, and the harness's own footer is last.
        pasted = "<!-- audit-id-scheme: 99 -->\n"
        body = pasted + audit_report.delta_block(["a.b.c.d"])
        self.assertEqual(audit_report.parse_id_scheme(body), audit_report.ID_SCHEME)


class TestSchemeMigration(HarnessTestCase):
    """One run of withheld `resolved`, then the guard lifts by itself."""

    def previous(self, ids, scheme=None):
        block = json.dumps(list(ids))
        stamp = "" if scheme is None else f"<!-- audit-id-scheme: {scheme} -->\n"
        return f"## Findings\n\n<!-- audit-findings: {block} -->\n{stamp}"

    def test_an_unstamped_ledger_reports_no_resolutions(self):
        # This is the 16:34 run, replayed: a previous block the current scheme
        # cannot join against, whose ids nevertheless look entirely ordinary. A
        # naive join calls every one of them fixed.
        self.harness.replies = {
            "issue list": self.issue_list(),
            "--json body": json.dumps(
                {
                    "body": self.previous(
                        [
                            "wildcard-rbac.prod-us-east._."
                            "clusterrolebinding-argocd-application-controller"
                        ]
                    )
                }
            ),
        }
        self.touch("clusters/prod-us-east/payments-netpol.yaml")

        self.assertEqual(self.run_finish(make_doc()), 0)

        out = self.stdout_json()
        self.assertEqual(out["resolved"], 0)
        self.assertIn("identity scheme 0", self.err)

    def test_the_comment_a_human_reads_withholds_it_too(self):
        # The stdout counter and the posted comment are two renderings of one
        # claim, and the incident was the *comment*: "4 resolved" in prose, on
        # a security ledger, under a body still listing the four as open.
        # Guarding only the counter leaves the half a human actually reads
        # free to say the opposite.
        stale = (
            "wildcard-rbac.prod-us-east._."
            "clusterrolebinding-argocd-application-controller"
        )
        self.harness.replies = {
            "issue list": self.issue_list(),
            "--json body": json.dumps({"body": self.previous([stale])}),
        }
        self.touch("clusters/prod-us-east/payments-netpol.yaml")

        self.assertEqual(self.run_finish(make_doc()), 0)

        bodies = self.harness.bodies_for("issue", "comment")
        self.assertTrue(bodies, "the run posted no delta comment at all")
        for body in bodies:
            with self.subTest(body=body):
                self.assertNotIn(" resolved**", body)
                self.assertNotIn(stale, body)

    def test_the_new_findings_are_still_announced(self):
        # `new` is noise, not a false claim of work done, so it is left alone:
        # withholding it too would leave the stream silent about a real finding
        # for a run, which is the failure mode the audit exists to prevent.
        self.harness.replies = {
            "issue list": self.issue_list(),
            "--json body": json.dumps({"body": self.previous(["wra-something-old"])}),
        }
        self.touch("clusters/prod-us-east/payments-netpol.yaml")

        self.assertEqual(self.run_finish(make_doc()), 0)

        self.assertEqual(self.stdout_json()["new"], 1)

    def test_an_empty_previous_block_is_not_a_migration(self):
        # Nothing to join against, so nothing to withhold and nothing to warn
        # about: a first run on a fresh ledger is not a scheme change.
        self.harness.replies = {
            "issue list": self.issue_list(),
            "--json body": json.dumps({"body": self.previous([])}),
        }
        self.touch("clusters/prod-us-east/payments-netpol.yaml")

        self.assertEqual(self.run_finish(make_doc()), 0)

        self.assertNotIn("identity scheme", self.err)

    def test_the_guard_lifts_once_the_block_is_rewritten(self):
        # The run above republished the ledger stamped, so the next one joins
        # normally and a real disappearance reads as resolved.
        self.harness.replies = {
            "issue list": self.issue_list(),
            "--json body": json.dumps(
                {
                    "body": self.previous(
                        [derived_id(), derived_id(fid="gone")],
                        scheme=audit_report.ID_SCHEME,
                    )
                }
            ),
        }
        self.touch("clusters/prod-us-east/payments-netpol.yaml")

        self.assertEqual(self.run_finish(make_doc()), 0)

        self.assertEqual(self.stdout_json()["resolved"], 1)
        self.assertNotIn("identity scheme", self.err)

    def test_a_future_scheme_is_withheld_too(self):
        # Not just "older": a ledger a newer harness wrote is equally
        # unjoinable, and rolling a deployment back must not turn its findings
        # into a page of fixes.
        self.harness.replies = {
            "issue list": self.issue_list(),
            "--json body": json.dumps(
                {
                    "body": self.previous(
                        [derived_id(), derived_id(fid="gone")],
                        scheme=audit_report.ID_SCHEME + 1,
                    )
                }
            ),
        }
        self.touch("clusters/prod-us-east/payments-netpol.yaml")

        self.assertEqual(self.run_finish(make_doc()), 0)

        self.assertEqual(self.stdout_json()["resolved"], 0)


# --------------------------------------------------------------------------- #
# Audit catalogue
# --------------------------------------------------------------------------- #


class TestAuditCatalogue(unittest.TestCase):
    def test_human_names_match_the_cron_watchdogs(self):
        """The PR title must name the same audit the cron catalogue does."""
        jobs_file = (
            Path(__file__).resolve().parents[4] / "platform" / "cron" / "jobs.json"
        )
        if not jobs_file.is_file():  # not shipped alongside the skill at runtime
            self.skipTest(f"{jobs_file} not present")
        jobs = json.loads(jobs_file.read_text(encoding="utf-8"))["jobs"]
        names = {job["id"]: job["name"] for job in jobs}
        for audit_id, spec in audit_report.AUDITS.items():
            if audit_id in names:
                with self.subTest(audit=audit_id):
                    self.assertEqual(
                        spec.title,
                        names[audit_id],
                        f"audit_report.AUDITS[{audit_id!r}].title is "
                        f"{spec.title!r} but cron/jobs.json calls it "
                        f"{names[audit_id]!r}",
                    )

    def test_check_rosters_match_the_sops(self):
        """The roster is the SOP's check list, or it is a lie the validator tells.

        `checks_run` is only worth requiring if the set it is checked against is
        the set the SOP actually defines. Re-derive it from the headings rather
        than trusting the copy in `AUDITS`: a check added to an SOP but not here
        is a check no run is ever obliged to perform, which is precisely the
        silent-coverage-hole this field exists to close.
        """
        sop_dir = Path(__file__).resolve().parents[4] / "platform" / "governance"
        if not sop_dir.is_dir():  # not shipped alongside the skill at runtime
            self.skipTest(f"{sop_dir} not present")
        # The slugs are the backticked tokens in the trailing parenthesis of a
        # `####` check heading. Anchoring on the trailing group and not on every
        # backtick in the line is load-bearing: "2.4 `cluster-admin` bound to
        # non-system subjects (`cluster-admin-binding`)" names one check, not
        # two, and a heading with no trailing group ("4.2 Workload Identity —
        # owned by the Security & RBAC Posture Audit") names none.
        trailing = re.compile(r"\((((?:`[^`]+`)(?:,\s*)?)+)\)\s*$")
        token = re.compile(r"`([^`]+)`")
        for audit_id, spec in audit_report.AUDITS.items():
            sop = sop_dir / SOP_FILENAMES[audit_id]
            with self.subTest(audit=audit_id):
                self.assertTrue(sop.is_file(), f"{sop} is missing")
                found: list[str] = []
                for line in sop.read_text(encoding="utf-8").splitlines():
                    if not line.startswith("#### "):
                        continue
                    match = trailing.search(line)
                    if match:
                        found.extend(token.findall(match.group(1)))
                self.assertEqual(
                    list(spec.checks),
                    found,
                    f"audit_report.AUDITS[{audit_id!r}].checks has drifted from "
                    f"{sop.name}. The SOP defines {found}",
                )

    def test_every_sop_states_the_checks_run_wire_format(self):
        """The SOP is what the cron prompt sends the worker to read.

        If it still describes `checks_run` as a list of slugs, the worker
        writes one, `finish` exits 2, and the audit is spent re-guessing a
        format the SOP could have stated. Prose drifting from the validator is
        how the last incident began, so pin the two fields.
        """
        sop_dir = Path(__file__).resolve().parents[4] / "platform" / "governance"
        if not sop_dir.is_dir():
            self.skipTest("governance SOPs not present")
        for audit_id, spec in audit_report.AUDITS.items():
            with self.subTest(audit=audit_id):
                body = (sop_dir / spec.sop).read_text(encoding="utf-8")
                self.assertIn('"check"', body)
                self.assertIn('"command"', body)

    def test_every_stream_names_the_sop_that_defines_it(self):
        """`AuditSpec.sop` is what a rejection points at instead of the roster.

        A rejection cannot name the valid slugs without becoming an answer key
        (`test_no_rejection_ever_prints_the_roster`), so it names the file that
        does. A wrong filename there sends a worker that already failed once to
        a document that does not exist — the worst possible moment for a broken
        pointer. `SOP_FILENAMES` above is spelled out independently, so this
        compares two hand-written mappings rather than one against itself.
        """
        sop_dir = Path(__file__).resolve().parents[4] / "platform" / "governance"
        for audit_id, spec in audit_report.AUDITS.items():
            with self.subTest(audit=audit_id):
                self.assertEqual(SOP_FILENAMES[audit_id], spec.sop)
                self.assertEqual(spec.sop, audit_report.audit_sop(audit_id))
                if sop_dir.is_dir():
                    self.assertTrue((sop_dir / spec.sop).is_file())

    def test_cron_prompts_cite_the_real_sop_geography(self):
        """A stale line number is worse than no line number.

        Each audit prompt tells the worker how long its SOP is and where the
        checks live, because a read that stops early lands in the preamble and
        produces a confident all-clear over an audit that never ran. That only
        helps while the numbers are true: a citation that has drifted teaches
        the worker it has read enough when it has not. Re-derive both from the
        file so that editing an SOP without re-measuring fails here rather than
        at 06:20 in production.
        """
        root = Path(__file__).resolve().parents[4] / "platform"
        jobs_file = root / "cron" / "jobs.json"
        sop_dir = root / "governance"
        if not jobs_file.is_file() or not sop_dir.is_dir():
            self.skipTest("cron catalogue and governance SOPs not present")
        jobs = {
            job["id"]: job["prompt"]
            for job in json.loads(jobs_file.read_text(encoding="utf-8"))["jobs"]
        }
        total = re.compile(r"all (\d+) lines of it")
        span = re.compile(r"are section (\d+), lines (\d+)-(\d+)")
        for audit_id in audit_report.AUDITS:
            prompt = jobs.get(audit_id)
            if prompt is None:
                continue
            body = (sop_dir / SOP_FILENAMES[audit_id]).read_text(encoding="utf-8")
            lines = body.splitlines()
            with self.subTest(audit=audit_id):
                claimed = total.search(prompt)
                self.assertIsNotNone(
                    claimed, f"{audit_id} prompt no longer states the SOP length"
                )
                self.assertEqual(
                    len(lines),
                    int(claimed.group(1)),
                    f"{audit_id} prompt claims {claimed.group(1)} lines but "
                    f"{SOP_FILENAMES[audit_id]} has {len(lines)}",
                )
                cited = span.search(prompt)
                self.assertIsNotNone(
                    cited, f"{audit_id} prompt no longer locates the checks"
                )
                section, first, last = cited.groups()
                # Sections are `### <n>. Title`; the section ends where the next
                # one begins, so the checks span up to the line before it.
                starts = [
                    n
                    for n, line in enumerate(lines, start=1)
                    if line.startswith("### ")
                ]
                heading = f"### {section}. "
                where = [n for n in starts if lines[n - 1].startswith(heading)]
                self.assertEqual(
                    1,
                    len(where),
                    f"{SOP_FILENAMES[audit_id]} has {len(where)} sections "
                    f"headed {heading!r}; the {audit_id} prompt cites one",
                )
                after = [n for n in starts if n > where[0]]
                self.assertEqual(
                    (where[0], (after[0] - 1) if after else len(lines)),
                    (int(first), int(last)),
                    f"{audit_id} prompt cites lines {first}-{last} for section "
                    f"{section} of {SOP_FILENAMES[audit_id]}, which has moved",
                )

    def test_no_audit_prompt_restates_the_silence_rule(self):
        """`[SILENT]` is the SOP's to define, and the prompt's to stay out of.

        Every audit SOP closes with the full rule: silent iff nothing is new,
        nothing resolved, and coverage is complete. A prompt that adds "reply
        with exactly [SILENT] when the fleet is clean" restates it with the two
        qualifiers dropped, and does so before the run starts — telling the
        worker what its answer looks like while it still decides what to check.
        """
        jobs_file = (
            Path(__file__).resolve().parents[4] / "platform" / "cron" / "jobs.json"
        )
        if not jobs_file.is_file():
            self.skipTest(f"{jobs_file} not present")
        jobs = {
            job["id"]: job["prompt"]
            for job in json.loads(jobs_file.read_text(encoding="utf-8"))["jobs"]
        }
        for audit_id in audit_report.AUDITS:
            if audit_id in jobs:
                with self.subTest(audit=audit_id):
                    self.assertNotIn(
                        "SILENT",
                        jobs[audit_id],
                        f"the {audit_id} cron prompt has picked the silence rule "
                        "back up; it belongs in the SOP's closing section, where "
                        "it is stated in full",
                    )


# --------------------------------------------------------------------------- #
# Protected branches
# --------------------------------------------------------------------------- #


class TestProtectedBranches(unittest.TestCase):
    def test_refuses_to_push_protected_branch(self):
        for branch in ("main", "master", "production", "MAIN", " main "):
            with self.subTest(branch=branch):
                with self.assertRaisesRegex(ValueError, "CRITICAL SECURITY REFUSAL"):
                    audit_report.assert_pushable(branch)

    def test_remediation_branch_is_pushable(self):
        # The audit report branch is gone; the only branch the harness ever
        # pushes is a remediation branch, so that is what the guard must clear.
        for audit_id in audit_report.AUDITS:
            with self.subTest(audit=audit_id):
                branch = audit_report.group_branch_for(
                    audit_id, [manifest_finding("a", "clusters/prod/netpol.yaml")]
                )
                self.assertTrue(branch.startswith(f"platform-agent/fix-{audit_id}-"))
                self.assertEqual(audit_report.assert_pushable(branch), branch)


# --------------------------------------------------------------------------- #
# Staging set
# --------------------------------------------------------------------------- #


class TestStaging(unittest.TestCase):
    def test_distinct_manifest_paths_only(self):
        findings = [
            make_finding(fid="a"),  # clusters/prod-us-east/payments-netpol.yaml
            make_finding(fid="b"),  # same path -> deduplicated
            make_finding(
                fid="c",
                remediation={
                    "kind": "manifest",
                    "path": "clusters/stage-eu/psp.yaml",
                    "note": "n",
                },
            ),
            make_finding(fid="d", remediation={"kind": "gcloud", "note": "gcloud ..."}),
            make_finding(fid="e", remediation={"kind": "manual", "note": "call SRE"}),
        ]
        self.assertEqual(
            audit_report.manifest_paths(findings),
            [
                "clusters/prod-us-east/payments-netpol.yaml",
                "clusters/stage-eu/psp.yaml",
            ],
        )

    def test_git_add_command_is_explicit(self):
        cmd = audit_report.build_git_add_command(["a.yaml", "b.yaml"])
        self.assertEqual(
            cmd,
            ["git", "--literal-pathspecs", "add", "--", "a.yaml", "b.yaml"],
        )

    def test_literal_pathspecs_precedes_the_subcommand(self):
        # `git add --literal-pathspecs` is an error: the flag is git-level, so
        # it has to sit before `add` or the whole guard fails at runtime.
        cmd = audit_report.build_git_add_command(["a.yaml"])
        self.assertLess(cmd.index("--literal-pathspecs"), cmd.index("add"))

    def test_wildcard_pathspecs_refused(self):
        for pathspec in (".", "-A", "--all", "-a", "*", ":/"):
            with self.subTest(pathspec=pathspec):
                with self.assertRaisesRegex(ValueError, "wildcard pathspec"):
                    audit_report.build_git_add_command([pathspec])

    def test_glob_metacharacters_refused_in_a_declared_path(self):
        # --literal-pathspecs makes these harmless to git, but a path with a
        # glob in it is a sign the agent meant to stage a set, not a file.
        for path in (
            "clusters/*.yaml",
            "clusters/prod-?/netpol.yaml",
            "clusters/[ab]/netpol.yaml",
            "clusters/x].yaml",
        ):
            with self.subTest(path=path):
                with self.assertRaises(audit_report.ValidationError):
                    audit_report.validate_findings(
                        make_doc(
                            findings=[
                                make_finding(
                                    remediation={
                                        "kind": "manifest",
                                        "path": path,
                                        "note": "n",
                                    }
                                )
                            ]
                        ),
                        AUDIT,
                    )

    def test_literal_pathspec_flag_defeats_a_glob_against_real_git(self):
        # Defence in depth, measured rather than assumed. The validator above
        # already refuses a glob, so this exercises the *second* layer: it
        # takes the flag prefix the harness actually emits and points it at a
        # repo holding a file literally named '*.yaml' alongside two files the
        # glob would match. Without the flag git stages all three.
        git = shutil.which("git")
        if git is None:  # pragma: no cover - git is present locally and in CI
            self.skipTest("git not on PATH")

        prefix = audit_report.build_git_add_command(["one.yaml"])[1:3]
        self.assertEqual(prefix, ["--literal-pathspecs", "add"])

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            def run(*args, **kw):
                return subprocess.run(
                    [git, *args], cwd=root, check=True, capture_output=True, text=True, **kw
                )

            run("init", "-q", "-b", "main")
            run("config", "user.email", "audit@example.invalid")
            run("config", "user.name", "audit")
            for name in ("*.yaml", "one.yaml", "two.yaml"):
                (root / name).write_text("x\n", encoding="utf-8")

            run(*prefix, "--", "*.yaml")
            staged = run("diff", "--cached", "--name-only").stdout.split()
            self.assertEqual(staged, ["*.yaml"])

    def test_empty_staging_set_refuses_to_build_an_add(self):
        with self.assertRaisesRegex(ValueError, "no explicit paths"):
            audit_report.build_git_add_command([])


# --------------------------------------------------------------------------- #
# finish — end-to-end over the recorded seam
# --------------------------------------------------------------------------- #


class TestFinishWithFindings(HarnessTestCase):
    def test_opens_the_ledger_issue_and_touches_no_branch(self):
        self.harness.replies = {
            "issue list": "[]",
            "issue create": "https://github.com/acme/fleet/issues/7\n",
        }
        self.touch("clusters/prod-us-east/payments-netpol.yaml")
        self.touch("clusters/stage-eu/psp.yaml")
        # Nothing here is auto-promotable — the manifest findings are below
        # `critical` and the one critical is a `gcloud` remediation, which has
        # no file to put in a pull request. That isolates the reporting path,
        # which is what this test is about; auto-promotion has its own.
        doc = make_doc(
            findings=[
                make_finding(fid="a", severity="major"),
                make_finding(fid="b", severity="major"),  # duplicate path
                make_finding(
                    fid="c",
                    severity="minor",
                    remediation={
                        "kind": "manifest",
                        "path": "clusters/stage-eu/psp.yaml",
                        "note": "n",
                    },
                ),
                make_finding(
                    fid="d",
                    remediation={"kind": "gcloud", "note": "gcloud x"},
                ),
            ]
        )

        rc = self.run_finish(doc)
        self.assertEqual(rc, 0)

        # A report is an issue now: no branch, no staging, no commit, no push —
        # and `finish` does not check anything out either. It reattaches to the
        # tree `start` prepared, because the audit's remediation manifests are
        # sitting untracked in it.
        self.assertEqual(self.git_add_calls(self.harness), [])
        self.assertFalse(self.harness.matching("git", "commit"))
        self.assertFalse(self.harness.matching("git", "push"))
        self.assertEqual(self.harness.matching("git", "checkout"), [])

        joined = [" ".join(c) for c in self.harness.calls]
        self.assertNotIn("git clean -fdq", joined)
        self.assertNotIn("git reset --hard --quiet", joined)

        create = self.harness.matching("issue", "create")[0]
        self.assertIn("--label", create)
        self.assertIn("agent:audit", create)
        self.assertIn("audit:compliance-audit", create)
        self.assertIn("--body-file", create)
        self.assertFalse(self.harness.matching("issue", "edit", "--title"))
        # The whole point of the split: reporting never *writes* a pull
        # request. It still reads them — that is how a finding learns whether
        # a fix is already in flight.
        for verb in ("create", "edit", "close", "comment"):
            self.assertEqual(self.harness.gh_calls("pr", verb), [], verb)

    def test_opened_status_json(self):
        self.harness.replies = {
            "issue list": "[]",
            "issue create": "https://github.com/acme/fleet/issues/7\n",
        }
        self.touch("clusters/prod-us-east/payments-netpol.yaml")
        self.run_finish(make_doc())
        self.assertEqual(
            self.stdout_json(),
            {
                "status": "OPENED",
                "issue_url": "https://github.com/acme/fleet/issues/7",
                "new": 1,
                "resolved": 0,
                "prs_opened": [],
                "prs_closed": [],
                "silent_ok": False,
                "partial": False,
                "coverage_gaps": [],
            },
        )

    def test_severity_label_is_applied_to_the_new_issue(self):
        self.harness.replies = {
            "issue list": "[]",
            "issue create": "https://github.com/acme/fleet/issues/7\n",
        }
        self.touch("clusters/prod-us-east/payments-netpol.yaml")
        self.run_finish(make_doc())
        label = self.harness.matching("issue", "edit", "severity:critical")
        self.assertTrue(label)
        self.assertEqual(label[0][:4], ["gh", "issue", "edit", "7"])

    def test_updates_in_place_and_posts_delta(self):
        previous_body = published_body(
            make_doc(
                findings=[
                    make_finding(fid="a", title="Alpha finding"),
                    make_finding(fid="b", title="Bravo finding"),
                ]
            ),
            generated_at=NOW,
        )
        self.harness.replies = {
            "issue list": self.issue_list(),
            "--json body": json.dumps({"body": previous_body}),
        }
        self.touch("clusters/prod-us-east/payments-netpol.yaml")
        doc = make_doc(
            findings=[
                make_finding(fid="b", title="Bravo finding"),
                make_finding(fid="c", title="Charlie finding"),
            ]
        )

        rc = self.run_finish(doc)
        self.assertEqual(rc, 0)

        self.assertFalse(self.harness.matching("issue", "create"))
        edit = self.harness.matching("issue", "edit", "--title")[0]
        self.assertEqual(edit[:4], ["gh", "issue", "edit", "42"])
        self.assertIn("--body-file", edit)

        self.assertTrue(self.harness.gh_calls("issue", "comment", "42"))

        self.assertEqual(
            self.stdout_json(),
            {
                "status": "UPDATED",
                "issue_url": "https://github.com/acme/fleet/issues/42",
                "new": 1,
                "resolved": 1,
                "prs_opened": [],
                "prs_closed": [],
                "silent_ok": False,
                "partial": False,
                "coverage_gaps": [],
            },
        )

    def test_no_comment_when_findings_unchanged(self):
        doc = make_doc()
        previous_body = published_body(doc, generated_at=NOW)
        self.harness.replies = {
            "issue list": self.issue_list(),
            "--json body": json.dumps({"body": previous_body}),
        }
        self.touch("clusters/prod-us-east/payments-netpol.yaml")

        self.run_finish(doc)

        # Body still refreshed, but silence when nothing changed.
        self.assertTrue(self.harness.matching("issue", "edit", "--title"))
        self.assertFalse(self.harness.gh_calls("issue", "comment"))
        result = self.stdout_json()
        self.assertEqual(result["status"], "UPDATED")
        self.assertEqual(result["new"], 0)
        self.assertEqual(result["resolved"], 0)

    def test_unreadable_previous_body_suppresses_the_delta(self):
        # None is not "": an unreadable body makes the delta unknowable, and
        # announcing every live finding as new is worse than announcing none.
        self.harness.replies = {"issue list": self.issue_list()}
        self.harness.failures = {"--json body": 1}
        self.touch("clusters/prod-us-east/payments-netpol.yaml")

        self.assertEqual(self.run_finish(make_doc()), 0)

        self.assertFalse(self.harness.gh_calls("issue", "comment"))
        result = self.stdout_json()
        self.assertEqual(result["status"], "UPDATED")
        self.assertEqual(result["new"], 0)
        self.assertEqual(result["resolved"], 0)
        self.assertIn("unreadable", self.err)

    def test_gcloud_only_run_still_publishes(self):
        self.harness.replies = {
            "issue list": "[]",
            "issue create": "https://github.com/acme/fleet/issues/9\n",
        }
        doc = make_doc(
            findings=[
                make_finding(remediation={"kind": "gcloud", "note": "gcloud ..."})
            ]
        )
        self.assertEqual(self.run_finish(doc), 0)
        self.assertEqual(self.git_add_calls(self.harness), [])
        self.assertTrue(self.harness.matching("issue", "create"))

    def test_a_missing_remediation_file_degrades_one_finding_not_the_report(self):
        # This used to abort the run. One finding whose promised manifest the
        # audit forgot to write would suppress the other nine criticals — the
        # report is the thing with value, and it was the thing thrown away.
        self.harness.replies = {"issue list": "[]"}
        # Deliberately do NOT create the manifest on disk.
        rc = self.run_finish(make_doc())
        self.assertEqual(rc, 0)
        self.assertIn("remediation file is missing", self.err)
        self.assertTrue(self.harness.matching("issue", "create"))
        # Degraded to manual, so it must not become a pull request either.
        self.assertEqual(self.harness.gh_calls("pr", "create"), [])

    def test_a_degraded_finding_says_why_it_has_no_pull_request(self):
        self.harness.replies = {"issue list": "[]"}
        findings = list(make_doc()["findings"])
        audit_report.degrade_missing_remediations(findings, self.workspace)
        self.assertEqual(findings[0]["remediation"]["kind"], "manual")
        self.assertEqual(findings[0]["remediation"]["path"], "")
        self.assertIn("did not write it", findings[0]["remediation"]["note"])

    def test_a_present_remediation_file_is_left_alone(self):
        self.touch("clusters/prod-us-east/payments-netpol.yaml")
        findings = list(make_doc()["findings"])
        self.assertEqual(
            audit_report.degrade_missing_remediations(findings, self.workspace), []
        )
        self.assertEqual(findings[0]["remediation"]["kind"], "manifest")


class TestPublishedBodies(HarnessTestCase):
    """What reaches GitHub, read back off the `--body-file` the harness wrote.

    Every other end-to-end test asserts that `gh` was called with the right
    flags, and every rendering test asserts that a renderer returns the right
    string. Neither connects the two. Replacing `_write_temp`'s payload with an
    empty string — blanking the ledger, every comment and every pull request —
    left the whole suite green, so the feature's actual output was untested.
    These tests are the wire, and they belong to the artifacts rather than to
    the code paths, so a rewrite of the publish path cannot quietly drop them.
    """

    def test_the_created_ledger_carries_the_report(self):
        self.harness.replies = {
            "issue list": "[]",
            "issue create": "https://github.com/acme/fleet/issues/7\n",
        }
        self.touch("clusters/prod-us-east/payments-netpol.yaml")
        self.assertEqual(self.run_finish(make_doc()), 0)

        bodies = self.harness.bodies_for("issue", "create")
        self.assertEqual(len(bodies), 1)
        self.assertIn("## Findings", bodies[0])
        self.assertIn("Namespace has no NetworkPolicy", bodies[0])
        self.assertIn("kubectl get networkpolicy -n payments", bodies[0])
        # The hidden block is the only state the next run has. If it is not in
        # the published document, every finding reads as new forever.
        self.assertIn(
            f'<!-- audit-findings: ["{derived_id()}"] -->', bodies[0]
        )

    def test_the_refreshed_ledger_and_its_delta_carry_their_own_text(self):
        previous_body = published_body(
            make_doc(findings=[make_finding(fid="a", title="Alpha finding")]),
            generated_at=NOW,
        )
        self.harness.replies = {
            "issue list": self.issue_list(),
            "--json body": json.dumps({"body": previous_body}),
        }
        self.touch("clusters/prod-us-east/payments-netpol.yaml")
        doc = make_doc(findings=[make_finding(fid="b", title="Bravo finding")])
        self.assertEqual(self.run_finish(doc), 0)

        edits = self.harness.bodies_for("issue", "edit")
        self.assertEqual(len(edits), 1)
        self.assertIn("Bravo finding", edits[0])
        self.assertNotIn("Alpha finding", edits[0])

        comments = self.harness.bodies_for("issue", "comment")
        self.assertEqual(len(comments), 1)
        self.assertIn(f"`{derived_id(fid='b')}`", comments[0])
        self.assertIn(f"`{derived_id(fid='a')}`", comments[0])

    def test_the_clean_comment_is_published_not_just_rendered(self):
        previous_body = published_body(
            make_doc(findings=[make_finding(fid="a")]), generated_at=NOW
        )
        self.harness.replies = {
            "issue list": self.issue_list(),
            "--json body": json.dumps({"body": previous_body}),
        }
        self.assertEqual(self.run_finish(make_doc(findings=[])), 0)

        comments = self.harness.bodies_for("issue", "comment")
        self.assertEqual(len(comments), 1)
        self.assertIn("is now clean", comments[0])

    def test_the_promoted_pull_request_carries_its_own_body(self):
        self.harness.replies = {
            "issue list": self.issue_list(),
            "pr create": "https://github.com/acme/fleet/pull/8\n",
        }
        self.touch("clusters/prod-us-east/payments-netpol.yaml")
        self.assertEqual(self.run_finish(make_doc()), 0)

        bodies = self.harness.bodies_for("pr", "create")
        self.assertEqual(len(bodies), 1)
        self.assertIn("## Files", bodies[0])
        self.assertIn("clusters/prod-us-east/payments-netpol.yaml", bodies[0])
        self.assertIn("Part of #42", bodies[0])
        # Same self-describing block as the ledger: the next run keys the pull
        # request back to its findings by reading it.
        self.assertIn(
            f'<!-- audit-findings: ["{derived_id()}"] -->', bodies[0]
        )

    def test_no_published_body_is_ever_empty(self):
        # The blanket form of the above, so an artifact added later is covered
        # by default rather than by somebody remembering to add a test.
        previous_body = published_body(
            make_doc(findings=[make_finding(fid="a")]), generated_at=NOW
        )
        self.harness.replies = {
            "issue list": self.issue_list(),
            "--json body": json.dumps({"body": previous_body}),
            "pr create": "https://github.com/acme/fleet/pull/8\n",
        }
        self.touch("clusters/prod-us-east/payments-netpol.yaml")
        self.assertEqual(self.run_finish(make_doc()), 0)

        published = [b for b in self.harness.bodies if b is not None]
        self.assertTrue(published, "the run published nothing at all")
        for index, body in enumerate(published):
            with self.subTest(body=index):
                self.assertTrue(body.strip())


class TestFinishClean(HarnessTestCase):
    def test_clean_run_closes_the_open_ledger_as_completed(self):
        previous_body = published_body(
            make_doc(findings=[make_finding(fid="a"), make_finding(fid="b")]),
            generated_at=NOW,
        )
        self.harness.replies = {
            "issue list": self.issue_list(),
            "--json body": json.dumps({"body": previous_body}),
        }

        rc = self.run_finish(make_doc(findings=[]))
        self.assertEqual(rc, 0)

        self.assertTrue(self.harness.gh_calls("issue", "comment", "42"))
        close = self.harness.matching("issue", "close", "42")
        self.assertTrue(close)
        # "completed", never "not planned": a clean fleet is done, not rejected.
        self.assertIn("--reason", close[0])
        self.assertIn("completed", close[0])
        # Nothing is committed, pushed, or deleted on a clean run.
        self.assertFalse(self.harness.matching("git", "push"))
        self.assertFalse(self.harness.matching("git", "commit"))
        self.assertFalse(self.harness.matching("branch", "-D"))

        self.assertEqual(
            self.stdout_json(),
            {
                "status": "CLEAN",
                "issue_url": "https://github.com/acme/fleet/issues/42",
                "new": 0,
                "resolved": 2,
                "prs_opened": [],
                "prs_closed": [],
                "silent_ok": False,
                "partial": False,
                "coverage_gaps": [],
            },
        )

    def test_a_failed_all_clear_comment_still_closes_the_ledger(self):
        # The close used to sit outside the try/finally, so a 422 on the
        # comment left the ledger open forever with no explanation.
        self.harness.replies = {"issue list": self.issue_list()}
        self.harness.failures = {"issue comment": 1}

        self.assertEqual(self.run_finish(make_doc(findings=[])), 0)

        self.assertTrue(self.harness.matching("issue", "close", "42"))
        self.assertIn("could not post the all-clear comment", self.err)

    def test_clean_run_with_no_open_ledger_is_a_no_op(self):
        self.harness.replies = {"issue list": "[]"}
        self.assertEqual(self.run_finish(make_doc(findings=[])), 0)
        self.assertFalse(self.harness.matching("issue", "close"))
        self.assertFalse(self.harness.gh_calls("issue", "comment"))
        self.assertEqual(
            self.stdout_json(),
            {
                "status": "CLEAN",
                "issue_url": None,
                "new": 0,
                "resolved": 0,
                "prs_opened": [],
                "prs_closed": [],
                "silent_ok": True,
                "partial": False,
                "coverage_gaps": [],
            },
        )

    def test_clean_comment_names_date_and_scope(self):
        comment = audit_report.render_clean_comment(AUDIT, make_doc(findings=[]), NOW)
        self.assertIn("2026-08-01 09:30 UTC", comment)
        self.assertIn("0 findings", comment)
        self.assertIn("`prod-us-east`", comment)
        self.assertIn("`stage-eu`", comment)
        self.assertIn("closed as completed", comment)

    def test_clean_comment_over_a_gap_does_not_announce_a_close(self):
        # The ledger stays open over a coverage gap, so a comment that says it is
        # "being closed as completed" is a statement the reader can check and find
        # false — on the very issue it is posted to.
        doc = make_doc(
            findings=[],
            skipped=[{"cluster": "prod-eu-1", "reason": "API server unreachable"}],
        )
        comment = audit_report.render_clean_comment(AUDIT, doc, NOW)
        self.assertNotIn("closing", comment)
        self.assertNotIn("closed as completed", comment)
        self.assertIn("did not see the whole fleet", comment)
        self.assertIn("the ledger stays open", comment.lower())
        self.assertIn("prod-eu-1", comment)
        self.assertIn("API server unreachable", comment)

    def test_clean_comment_treats_a_limitation_as_a_gap_too(self):
        # `limitations` was invisible to this comment: only `scope.skipped` was
        # rendered, so a cluster that was read but not fully checked produced an
        # unqualified all-clear.
        doc = make_doc(
            findings=[],
            clusters=[
                {
                    "name": "prod-us-east",
                    "location": "us-east1",
                    "project": "acme",
                    "limitations": "Autopilot: node-level checks did not run",
                }
            ],
        )
        comment = audit_report.render_clean_comment(AUDIT, doc, NOW)
        self.assertNotIn("closed as completed", comment)
        self.assertIn("Autopilot: node-level checks did not run", comment)


# --------------------------------------------------------------------------- #
# start
# --------------------------------------------------------------------------- #


class TestStart(HarnessTestCase):
    def setUp(self):
        super().setUp()
        # handle_start pre-creates /opt/data/scratch; keep the tests off the real FS.
        patcher = patch.object(audit_report.os, "makedirs", lambda *a, **k: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.patch_attr(
            "findings_path_for",
            lambda audit_id: str(self.tmp_path / f"findings_{audit_id}.json"),
        )

    def test_emits_one_json_line(self):
        self.harness.replies = {"issue list": self.issue_list()}
        self.assertEqual(self.run_main(["start", "--audit", AUDIT]), 0)

        out = self.out.strip()
        self.assertNotIn("\n", out)
        payload = json.loads(out)
        contract = payload.pop("checks_contract", "")
        self.assertEqual(
            payload,
            {
                "issue": 42,
                "repo": "acme/fleet",
                "workspace": str(self.workspace),
                "findings_path": str(self.tmp_path / "findings_compliance-audit.json"),
                "pending_remediation_requests": [],
                "sop": "governance/compliance_audit_sop.md",
                "checks": list(audit_report.audit_checks(AUDIT)),
            },
        )
        # Popped rather than pinned: the contract is prose, and a test that
        # asserts it verbatim turns every wording improvement into a failure.
        # What must hold is that it states the shape and the consequence.
        self.assertIn("checks_run", contract)
        self.assertIn("command", contract)
        # And that it names the other half of the coverage story. `start` is
        # the only place the worker is told this before it writes the document;
        # a contract that mentions only `checks_run` sends every inapplicable
        # check into `limitations`, which is where the permanently-partial
        # Autopilot fleet came from.
        self.assertIn("checks_not_applicable", contract)
        self.assertIn("reason", contract)

    def test_start_hands_over_the_roster(self):
        """Coverage must not depend on how far into the SOP the worker read.

        The roster is in the SOP and the SOP is required reading, but "it will
        read far enough" is not a mechanism. Hermes's `read_file` defaults to
        500 lines and every audit SOP fits inside that — and the run that
        published five false all-clears still asked for 100 lines of each, on
        files whose checks start past line 60 and run past 270. Printing the
        roster here is free and removes the failure mode outright.

        Safe at `start` in a way it is never safe at `finish`: this is the
        instruction, issued before any work. The same list inside a rejection
        is an answer key — see `test_no_rejection_ever_prints_the_roster`.
        """
        for audit_id in audit_report.AUDITS:
            with self.subTest(audit=audit_id):
                self.out = ""
                self.harness.replies = {"issue list": "[]"}
                self.assertEqual(self.run_main(["start", "--audit", audit_id]), 0)
                payload = json.loads(self.out)
                self.assertEqual(
                    list(audit_report.audit_checks(audit_id)), payload["checks"]
                )
                self.assertEqual(
                    f"governance/{audit_report.audit_sop(audit_id)}", payload["sop"]
                )

    def test_the_workspace_is_named_so_manifests_can_be_written_into_it(self):
        # The agent does not start in a working tree, so a `remediation.path`
        # is meaningless unless `start` says what it is relative to.
        self.harness.replies = {"issue list": "[]"}
        self.run_main(["start", "--audit", AUDIT])
        reported = Path(json.loads(self.out)["workspace"])
        self.assertEqual(reported, self.workspace)
        self.assertTrue((reported / ".git").exists())

    def test_each_audit_gets_its_own_clone(self):
        # Five audits run from one cron file and their schedules collide. They
        # used to share a directory, so whichever one reached `finish` first
        # ran `checkout --force -B` over the other four's untracked manifests.
        self.harness.replies = {"issue list": "[]"}
        self.run_main(["start", "--audit", AUDIT])
        mine = Path(json.loads(self.out)["workspace"])
        self.out = ""
        self.run_main(["start", "--audit", "obtainability-audit"])
        theirs = Path(json.loads(self.out)["workspace"])

        self.assertNotEqual(mine, theirs)
        self.assertEqual(mine.parent.name, AUDIT)
        self.assertEqual(theirs.parent.name, "obtainability-audit")
        self.assertEqual(mine.parent.parent, theirs.parent.parent)

    def test_the_clone_is_marked_as_leased(self):
        # The marker the credential proxy looks for. Without it every git verb
        # that writes a tree is refused, including the audit's own.
        self.harness.replies = {"issue list": "[]"}
        self.run_main(["start", "--audit", AUDIT])
        reported = Path(json.loads(self.out)["workspace"])
        record = gitops_workspace.read_lease(reported.parent)
        self.assertEqual(record["lease"], AUDIT)
        self.assertEqual(record["owner"], f"fleet-audit:{AUDIT}")

    def test_null_issue_when_none_open(self):
        self.harness.replies = {"issue list": "[]"}
        self.run_main(["start", "--audit", "obtainability-audit"])
        self.assertIsNone(json.loads(self.out)["issue"])

    def test_no_report_branch_is_created(self):
        # The report branch is gone. `start` establishes the GitOps clone and
        # leaves it on main; it never cuts a branch of its own and never
        # pushes.
        self.harness.replies = {"issue list": "[]"}
        self.run_main(["start", "--audit", AUDIT])
        checkouts = self.harness.matching("git", "checkout")
        self.assertEqual(checkouts, [["git", "checkout", "-B", "main", "origin/main"]])
        self.assertFalse(self.harness.matching("git", "push"))
        self.assertFalse(self.harness.matching("git", "commit"))

    def test_the_gitops_clone_is_established_before_github_is_read(self):
        # Every git and gh call the harness makes runs inside this clone. It
        # did not exist: nothing in the pod ever cloned the GitOps repository,
        # so `git rev-parse --show-toplevel` failed and no remediation pull
        # request could ever have been opened.
        self.unclone()
        self.harness.replies = {"issue list": "[]"}
        self.run_main(["start", "--audit", AUDIT])
        clones = [c for c in self.harness.calls if c[:2] == ["git", "clone"]]
        self.assertEqual(len(clones), 1)
        self.assertEqual(clones[0][-1], str(self.workspace))
        self.assertTrue((self.workspace / ".git").is_dir())
        self.assertTrue(self.harness.matching("git", "config", "user.email"))

    def test_a_second_run_fetches_instead_of_cloning_again(self):
        self.unclone()
        self.harness.replies = {"issue list": "[]"}
        self.run_main(["start", "--audit", AUDIT])
        self.harness.calls.clear()
        self.run_main(["start", "--audit", AUDIT])
        self.assertFalse([c for c in self.harness.calls if c[:2] == ["git", "clone"]])
        self.assertTrue(self.harness.matching("git", "fetch"))

    def test_a_stale_findings_file_is_removed(self):
        # A crashed run must not leave a document for the next one to publish.
        self.harness.replies = {"issue list": "[]"}
        stale = self.tmp_path / f"findings_{AUDIT}.json"
        stale.write_text('{"audit": "stale"}', encoding="utf-8")
        self.run_main(["start", "--audit", AUDIT])
        self.assertFalse(stale.exists())

    def test_pending_remediate_requests_are_reported(self):
        self.harness.replies = {
            "issue list": self.issue_list(),
            "--json comments": json.dumps(
                {
                    "comments": [
                        comment("/remediate no-network-policy"),
                        comment("/remediate nope", association="NONE"),
                    ]
                }
            ),
        }
        self.run_main(["start", "--audit", AUDIT])
        self.assertEqual(
            json.loads(self.out)["pending_remediation_requests"],
            ["no-network-policy"],
        )

    def test_a_gh_outage_fails_loudly_rather_than_reporting_no_ledger(self):
        # Returning "no issue" on a transport failure would open a duplicate.
        self.harness.failures = {"issue list": 1}
        self.assertEqual(self.run_main(["start", "--audit", AUDIT]), 1)
        self.assertIn("could not list issues", self.err)

    def test_creates_labels(self):
        self.harness.replies = {"issue list": "[]"}
        self.run_main(["start", "--audit", AUDIT])

        created = {c[3] for c in self.harness.matching("label", "create")}
        self.assertEqual(
            created,
            {
                "agent:audit",
                "audit:compliance-audit",
                "audit:remediation",
                # Load-bearing, not decorative: the close path refuses to close
                # without it, because an unlabelled close reads as a human's
                # rejection and retires the finding for good.
                "audit:stale-closed",
                "severity:critical",
                "severity:major",
                "severity:minor",
            },
        )

    def test_unknown_audit_id_touches_nothing(self):
        self.assertEqual(self.run_main(["start", "--audit", "made-up-audit"]), 2)
        self.assertEqual(self.harness.calls, [])


# --------------------------------------------------------------------------- #
# --dry-run
# --------------------------------------------------------------------------- #


class TestDryRun(BaseTestCase):
    """No credential or repo-root stubs here on purpose: --dry-run must not need them."""

    def test_renders_body_without_side_effects(self):
        recorder = Recorder()
        self.patch_attr("run_cmd", recorder)

        def explode(*_args, **_kwargs):
            raise AssertionError("--dry-run must not touch credentials")

        self.patch_attr("refresh_credentials", explode)
        self.patch_attr("resolve_repo", explode)

        rc = self.run_finish(make_doc(), argv_extra=("--dry-run",))
        self.assertEqual(rc, 0)

        self.assertIn("## Findings", self.out)
        self.assertIn("<!-- audit-findings:", self.out)
        self.assertEqual([c for c in recorder.calls if c[0] == "gh"], [])
        for call in recorder.calls:
            self.assertNotEqual(call[:2], ["git", "add"])
            self.assertNotEqual(call[:2], ["git", "push"])
            self.assertNotEqual(call[:2], ["git", "commit"])

    def test_dry_run_still_rejects_bad_findings(self):
        doc = make_doc(clusters=[])
        self.assertEqual(self.run_finish(doc, argv_extra=("--dry-run",)), 2)
        self.assertIn("scope.clusters", self.err)

    def test_dry_run_clean_renders_the_close_comment(self):
        self.patch_attr("run_cmd", Recorder())
        self.assertEqual(
            self.run_finish(make_doc(findings=[]), argv_extra=("--dry-run",)), 0
        )
        self.assertIn("is now clean", self.out)

    def test_dry_run_renders_every_pr_body_it_would_open(self):
        # The pull request is the artifact a person is asked to merge. Printing
        # the ledger alone left the reviewable half visible only in production.
        self.patch_attr("run_cmd", Recorder())
        self.patch_attr("repo_root_best_effort", lambda: self.tmp_path)
        self.touch("clusters/prod-us-east/payments-netpol.yaml")

        rc = self.run_finish(make_doc(), argv_extra=("--dry-run",))
        self.assertEqual(rc, 0)
        self.assertIn(audit_report.DRY_RUN_PR_SEPARATOR, self.out)

        ledger, _, pr = self.out.partition(audit_report.DRY_RUN_PR_SEPARATOR)
        self.assertIn("## Findings", ledger)
        self.assertIn("## Files", pr)
        self.assertIn("clusters/prod-us-east/payments-netpol.yaml", pr)
        self.assertIn("branch: platform-agent/fix-", pr)
        self.assertIn("title: ", pr)
        # No `gh` call on this path, so the ledger number is genuinely unknown;
        # the run says so rather than letting the gap read as a rendering bug.
        self.assertNotIn("Part of #", pr)
        self.assertIn("the 'Part of #N' link is omitted", self.err)

    def test_dry_run_prints_no_pr_body_for_a_manifest_that_is_not_on_disk(self):
        # Degradation runs first, so the dry run must show the same nothing the
        # real run would open — not a pull request for a file that isn't there.
        self.patch_attr("run_cmd", Recorder())
        self.patch_attr("repo_root_best_effort", lambda: self.tmp_path)

        rc = self.run_finish(make_doc(), argv_extra=("--dry-run",))
        self.assertEqual(rc, 0)
        self.assertNotIn(audit_report.DRY_RUN_PR_SEPARATOR, self.out)
        self.assertIn("no remediation pull requests", self.err)

    def test_dry_run_pr_bodies_match_the_branches_it_says_it_would_open(self):
        # `WOULD OPEN` and the bodies are computed from the same group list, so
        # a dry run cannot name one branch and render another's contents.
        self.patch_attr("run_cmd", Recorder())
        self.patch_attr("repo_root_best_effort", lambda: self.tmp_path)
        for path in ("a.yaml", "b.yaml"):
            self.touch(path)
        doc = make_doc(
            findings=[manifest_finding("f-a", "a.yaml"), manifest_finding("f-b", "b.yaml")]
        )

        self.assertEqual(self.run_finish(doc, argv_extra=("--dry-run",)), 0)
        line = re.search(r"WOULD OPEN: (.+)$", self.err, re.M).group(1)
        announced = [name.strip() for name in line.split(",")]
        rendered = re.findall(r"^branch: (\S+)$", self.out, re.M)
        self.assertEqual(len(rendered), 2)
        self.assertEqual(sorted(announced), sorted(rendered))


# --------------------------------------------------------------------------- #
# Size budget — the difference between a stream that publishes and one that 422s
# --------------------------------------------------------------------------- #


def bulk_findings(count, severity="minor", prefix="f"):
    """`count` findings with distinct ids and SOP-shaped prose."""
    return [
        make_finding(
            fid=f"{prefix}-{i:04d}",
            severity=severity,
            title=f"Finding {i}: workload deviates from the baseline",
            namespace=f"ns-{i:04d}",
            obj=f"Deployment/app-{i:04d}",
            command=(
                f"kubectl --context prod-us-east -n ns-{i:04d} get deployment "
                f"app-{i:04d} -o jsonpath='{{.spec.template.spec.containers[*].resources}}'"
            ),
            excerpt="\n".join(f"line {n} of captured output" for n in range(12)),
            remediation={
                "kind": "manifest",
                "path": f"clusters/prod-us-east/ns-{i:04d}-app-{i:04d}.yaml",
                "note": "Apply the corrected manifest.",
            },
        )
        for i in range(count)
    ]


class TestRenderBudget(BaseTestCase):
    def render(self, doc):
        return render_body(doc, generated_at=NOW)

    def test_the_budget_matches_github(self):
        # The one place the two numbers are allowed to meet. Every other size
        # test measures against the literal, so this is what fails — loudly,
        # and on its own — if somebody widens the constant to make a body fit.
        self.assertEqual(audit_report.MAX_BODY_CHARS, GITHUB_BODY_LIMIT)
        # And the working budget has to leave real headroom underneath it: the
        # footer, the delta block and the truncation notice are all appended
        # after selection, so a budget equal to the limit overflows by exactly
        # the amount the selection loop could not see.
        self.assertLess(audit_report.BODY_BUDGET, GITHUB_BODY_LIMIT)

    def test_body_stays_under_the_github_limit_at_250_findings(self):
        body = self.render(make_doc(findings=bulk_findings(250)))
        self.assertLess(len(body), GITHUB_BODY_LIMIT)

    def test_ten_findings_render_untruncated(self):
        findings = bulk_findings(10)
        body = self.render(make_doc(findings=findings))
        # The renderer says "further finding(s) are omitted". Asserting on
        # "further findings omitted" — the phrasing this test used to check —
        # matched nothing the renderer can emit, so it stayed green on a body
        # that *was* truncated. Assert on the same regex the truncation test
        # uses, inverted, so the two cannot drift apart again.
        self.assertNotRegex(body, r"\d+ further finding\(s\) are omitted")
        for finding in findings:
            self.assertIn(finding["id"], body)

    def test_truncation_notice_names_the_omitted_count(self):
        body = self.render(make_doc(findings=bulk_findings(250)))
        self.assertRegex(body, r"\d+ further finding\(s\) are omitted")

    def test_title_carries_the_true_total_even_when_truncated(self):
        findings = bulk_findings(250)
        body = self.render(make_doc(findings=findings))
        title = audit_report.issue_title(AUDIT, findings)
        self.assertIn("250 findings", title)
        # The rendered body must not silently disagree with the title.
        self.assertIn("250", body)

    def test_delta_block_lists_exactly_the_rendered_ids(self):
        findings = bulk_findings(250)
        body = self.render(make_doc(findings=findings))
        recorded = audit_report.parse_delta_block(body)
        ordered = [f["id"] for f in audit_report.sort_findings(findings)]

        self.assertTrue(recorded)
        self.assertLess(len(recorded), len(findings), "fixture must overflow")
        # The recorded set is a prefix of the severity-first order, so
        # truncation only ever eats the least-severe end.
        self.assertEqual(recorded, ordered[: len(recorded)])
        # Every recorded id is genuinely in the body, and the first id that is
        # not recorded is genuinely absent — otherwise the next run reads a
        # truncated finding as resolved and announces a fix that never happened.
        for fid in recorded:
            self.assertIn(fid, body)
        self.assertNotIn(ordered[len(recorded)], body)

    def test_criticals_survive_a_flood_of_minor_findings(self):
        findings = bulk_findings(5, severity="critical", prefix="crit") + bulk_findings(
            300, severity="minor"
        )
        body = self.render(make_doc(findings=findings))
        for i in range(5):
            self.assertIn(f"crit-{i:04d}", body)
        self.assertLess(len(body), GITHUB_BODY_LIMIT)

    def test_scope_only_body_cannot_overflow(self):
        # Zero findings, an enormous fleet: this overflowed at 148,627 chars
        # before the scope tables were capped.
        doc = make_doc(
            findings=[],
            clusters=[
                {"name": f"c-{i:04d}", "location": "us-east1", "project": "acme"}
                for i in range(1200)
            ],
            skipped=[
                {"cluster": f"s-{i:04d}", "reason": "control plane unreachable"}
                for i in range(1200)
            ],
        )
        body = self.render(doc)
        self.assertLess(len(body), GITHUB_BODY_LIMIT)
        self.assertIn("more", body)

    def test_clean_comment_stays_under_the_limit_at_900_skipped(self):
        doc = make_doc(
            findings=[],
            clusters=[
                {"name": f"c-{i:04d}", "location": "us-east1", "project": "acme"}
                for i in range(900)
            ],
            skipped=[
                {"cluster": f"s-{i:04d}", "reason": "unreachable"} for i in range(900)
            ],
        )
        comment = audit_report.render_clean_comment(AUDIT, doc, NOW)
        self.assertLess(len(comment), GITHUB_BODY_LIMIT)

    def test_delta_comment_stays_under_the_limit(self):
        # Newly reachable: capping the body means N is no longer pinned under
        # ~67 by the body failing first, so this path stops being dead code.
        findings = bulk_findings(250)
        comment = audit_report.render_delta_comment(
            AUDIT,
            [f["id"] for f in findings],
            [f"gone-{i:04d}" for i in range(250)],
            findings,
            {f["id"]: f["title"] for f in findings},
            NOW,
        )
        self.assertLess(len(comment), GITHUB_BODY_LIMIT)

    def test_long_command_is_trimmed(self):
        finding = make_finding(command="kubectl get pods " + "x" * 5000)
        rendered = "\n".join(audit_report.render_finding(finding))
        self.assertLess(len(rendered), 4000)
        self.assertIn("truncated", rendered.lower())

    def test_selection_is_a_prefix_of_the_sorted_order(self):
        findings = bulk_findings(3, severity="minor") + bulk_findings(
            2, severity="critical", prefix="c"
        )
        rendered, omitted = audit_report.select_rendered_findings(findings, 1)
        self.assertEqual(len(rendered), 1)
        self.assertEqual(rendered[0]["severity"], "critical")
        self.assertEqual(len(omitted), 4)

    def test_at_least_one_finding_always_renders(self):
        rendered, _ = audit_report.select_rendered_findings(bulk_findings(5), 0)
        self.assertEqual(len(rendered), 1)


# --------------------------------------------------------------------------- #
# The index ↔ detail link
# --------------------------------------------------------------------------- #


class TestFindingAnchors(BaseTestCase):
    """The index is for acting on findings, so a row has to reach its detail.

    Before this, the id column was inert text and the detail block named its id
    only inside an HTML comment — invisible in the rendered issue. A reader who
    had just finished a detail block and wanted to comment `/remediate <id>`
    had to scroll back to the table and match the row by cluster and severity.
    """

    ANCHOR_RE = re.compile(r'<a id="([^"]+)"></a>')
    HREF_RE = re.compile(r"\]\(#([^)]+)\)")

    def body(self, findings, **kwargs):
        states = {str(f["id"]): audit_report.STATE_OPEN for f in findings}
        states.update(kwargs.pop("states", {}))
        return render_body(
            make_doc(findings=findings),
            generated_at=NOW,
            states=states,
            **kwargs,
        )

    def test_every_index_row_reaches_an_anchor_that_exists(self):
        findings = [
            make_finding(fid="a-crit", severity="critical"),
            make_finding(fid="b-major", severity="major"),
        ]
        body = self.body(findings)
        hrefs = set(self.HREF_RE.findall(body))
        anchors = set(self.ANCHOR_RE.findall(body))
        # Every id is reachable, and no href points at a target that is not
        # in the body — a dangling fragment silently does nothing on GitHub.
        for finding in findings:
            self.assertIn(audit_report._anchor_id(finding["id"]), anchors)
        self.assertEqual(hrefs - anchors, set())

    def test_the_detail_block_names_its_own_id_visibly(self):
        # Visibly: not in the HTML comment, which renders to nothing. This is
        # the string an operator retypes after `/remediate`.
        rendered = "\n".join(audit_report.render_finding(make_finding(fid="f-1")))
        without_comments = re.sub(r"<!--.*?-->", "", rendered, flags=re.S)
        self.assertIn("`f-1`", without_comments)

    def test_the_anchor_does_not_break_title_recovery(self):
        """FINDING_MARKER_RE runs to end of line.

        Putting the anchor on the heading instead of above it would stop the
        regex matching, and the failure would surface a run later as a resolved
        finding the delta comment could not name.
        """
        findings = [make_finding(fid="f-1", title="A real title")]
        titles = audit_report.parse_finding_titles(self.body(findings))
        self.assertEqual(titles, {"f-1": "A real title"})

    def test_a_recovered_title_carries_no_markup(self):
        title = audit_report.parse_finding_titles(
            self.body([make_finding(fid="f-1", title="A real title")])
        )["f-1"]
        self.assertNotIn("<a", title)
        self.assertNotIn("href", title)


class TestIndexOverhead(BaseTestCase):
    """The index is reserved out of the budget, not charged to a finding.

    It replaced a flat per-row allowance of 160 characters that a real id had
    already outgrown: ids run to 100 characters and the state cell can carry a
    full pull request URL. Under-reserving spends budget the findings were
    promised, and the body only fails once it crosses GitHub's hard limit.
    """

    def rendered_table(self, body):
        """The contiguous index block only.

        Stopping at the first non-row line matters: the check-evidence appendix
        further down the body is also a Markdown table, and sweeping its rows in
        would measure this reservation against text it does not cover.
        """
        lines = body.splitlines()
        start = lines.index("| Finding | Severity | Cluster | State |")
        rows = []
        for line in lines[start:]:
            if not line.startswith("|"):
                break
            rows.append(line)
        return "\n".join(rows)

    def test_the_reservation_covers_what_the_table_actually_costs(self):
        # Worst realistic row: a 100-character id (the charset ceiling) and a
        # state cell carrying a pull request URL.
        fid = "f" + "-long" * 19 + "-end"
        self.assertLessEqual(len(fid), 100)
        findings = [
            make_finding(fid=f"{fid[:95]}-{i:03d}", severity="critical")
            for i in range(10)
        ]
        states = {f["id"]: audit_report.STATE_PR_OPEN for f in findings}
        urls = {
            f["id"]: "https://github.com/an-org/a-repository/pull/12345"
            for f in findings
        }
        body = render_body(
            make_doc(findings=findings),
            generated_at=NOW,
            states=states,
            pr_urls=urls,
        )
        reserved = audit_report.index_overhead(findings, states, urls)
        self.assertGreaterEqual(reserved, len(self.rendered_table(body)))

    def test_the_reservation_bounds_a_table_that_hits_the_row_cap(self):
        findings = bulk_findings(audit_report.MAX_DELTA_ROWS + 20)
        states = {f["id"]: audit_report.STATE_OPEN for f in findings}
        reserved = audit_report.index_overhead(findings, states, {})
        body = render_body(make_doc(findings=findings), generated_at=NOW, states=states)
        self.assertGreaterEqual(reserved, len(self.rendered_table(body)))


# --------------------------------------------------------------------------- #
# Schema — recommendation, limitations, and the finding-id charset
# --------------------------------------------------------------------------- #


class TestRecommendation(BaseTestCase):
    def assert_rejected(self, recommendation, pattern="recommendation"):
        doc = make_doc(findings=[make_finding(recommendation=recommendation)])
        with self.assertRaisesRegex(audit_report.ValidationError, pattern):
            audit_report.validate_findings(doc, AUDIT)

    def test_missing_recommendation_is_rejected(self):
        doc = make_doc(findings=[make_finding()])
        del doc["findings"][0]["recommendation"]
        with self.assertRaisesRegex(audit_report.ValidationError, "recommendation"):
            audit_report.validate_findings(doc, AUDIT)

    def test_each_sub_field_is_required(self):
        full = {"action": "a", "rationale": "r", "risk": "k"}
        for field in ("action", "rationale", "risk"):
            with self.subTest(missing=field):
                partial = {k: v for k, v in full.items() if k != field}
                self.assert_rejected(partial, field)

    def test_empty_sub_field_is_rejected(self):
        for field in ("action", "rationale", "risk"):
            with self.subTest(empty=field):
                rec = {"action": "a", "rationale": "r", "risk": "k"}
                rec[field] = "   "
                self.assert_rejected(rec, field)

    def test_wrong_type_is_rejected(self):
        self.assert_rejected("just a string")
        self.assert_rejected(["action", "rationale", "risk"])
        self.assert_rejected({"action": 5, "rationale": "r", "risk": "k"}, "action")

    def test_recommendation_renders_all_three_fields(self):
        rendered = "\n".join(
            audit_report.render_finding(
                make_finding(
                    recommendation={
                        "action": "Do the thing.",
                        "rationale": "Because the alternative is worse.",
                        "risk": "Traffic may drop; check flows first.",
                    }
                )
            )
        )
        self.assertIn("Do the thing.", rendered)
        self.assertIn("Because the alternative is worse.", rendered)
        self.assertIn("Traffic may drop; check flows first.", rendered)


class TestScopeLimitations(BaseTestCase):
    def test_limitations_are_accepted(self):
        doc = make_doc(
            clusters=[
                {
                    "name": "prod-us-east",
                    "location": "us-east1",
                    "project": "acme-prod",
                    "limitations": "Autopilot: checks 2.1-2.3 did not run.",
                }
            ]
        )
        self.assertTrue(audit_report.validate_findings(doc, AUDIT))

    def test_empty_limitations_entry_is_rejected(self):
        doc = make_doc(
            clusters=[
                {
                    "name": "prod-us-east",
                    "location": "us-east1",
                    "project": "acme-prod",
                    "limitations": "   ",
                }
            ]
        )
        with self.assertRaisesRegex(audit_report.ValidationError, "limitations"):
            audit_report.validate_findings(doc, AUDIT)

    def test_a_cluster_cannot_be_both_audited_and_skipped(self):
        # The Autopilot false-all-clear: the collision this field exists to end.
        doc = make_doc(
            clusters=[
                {"name": "prod-us-east", "location": "us-east1", "project": "acme"}
            ],
            skipped=[{"cluster": "prod-us-east", "reason": "Autopilot"}],
        )
        with self.assertRaises(audit_report.ValidationError):
            audit_report.validate_findings(doc, AUDIT)

    def test_duplicate_skipped_entries_are_rejected(self):
        doc = make_doc(
            findings=[],
            skipped=[
                {"cluster": "dr-west", "reason": "unreachable"},
                {"cluster": "dr-west", "reason": "unreachable again"},
            ],
        )
        with self.assertRaises(audit_report.ValidationError):
            audit_report.validate_findings(doc, AUDIT)

    def test_a_finding_cannot_name_a_skipped_cluster(self):
        doc = make_doc(
            findings=[make_finding(cluster="dr-west")],
            skipped=[{"cluster": "dr-west", "reason": "control plane unreachable"}],
        )
        with self.assertRaises(audit_report.ValidationError):
            audit_report.validate_findings(doc, AUDIT)


class TestFindingIdCharset(BaseTestCase):
    def test_usable_ids_are_accepted(self):
        for fid in ("a", "netpol-missing-payments", "v1.2.3-drift", "a" * 100):
            with self.subTest(fid=fid):
                self.assertEqual(audit_report.validate_finding_id(fid, "where"), fid)

    def test_ids_git_would_refuse_are_rejected(self):
        # Each of these produces a branch `git check-ref-format` rejects once
        # the id becomes part of platform-agent/fix-<audit>-<id>.
        for fid in (
            "has:colon",
            "has space",
            "has..dots",
            "has*star",
            "ends.lock",
            "UPPERCASE",
            "-leading-dash",
            "trailing-dash-",
            "a" * 101,
            "",
            "has~tilde",
            "has^caret",
            "has?question",
            "has[bracket",
            "has\\backslash",
            "has\tab",
        ):
            with self.subTest(fid=fid):
                with self.assertRaises(audit_report.ValidationError):
                    audit_report.validate_finding_id(fid, "where")

    def test_accepted_ids_survive_git_check_ref_format(self):
        # The rule is only worth anything if git agrees with it.
        git = shutil.which("git")
        if git is None:  # pragma: no cover - git is present locally and in CI
            self.skipTest("git not on PATH")
        for fid in ("a", "netpol-missing-payments", "v1.2.3-drift", "a" * 100):
            with self.subTest(fid=fid):
                branch = audit_report.group_branch_for(AUDIT, [make_finding(fid=fid)])
                proc = subprocess.run(
                    [git, "check-ref-format", f"refs/heads/{branch}"],
                    capture_output=True,
                )
                self.assertEqual(proc.returncode, 0, branch)


# --------------------------------------------------------------------------- #
# Remediation grouping (§5)
# --------------------------------------------------------------------------- #


def manifest_finding(fid, path, severity="critical"):
    return make_finding(
        fid=fid,
        severity=severity,
        remediation={"kind": "manifest", "path": path, "note": "n"},
    )


class TestRemediationGroups(BaseTestCase):
    def ids(self, groups):
        return [[f["id"] for f in group] for group in groups]

    def test_disjoint_paths_are_separate_groups(self):
        groups = audit_report.remediation_groups(
            [manifest_finding("a", "x.yaml"), manifest_finding("b", "y.yaml")]
        )
        self.assertEqual(self.ids(groups), [["a"], ["b"]])

    def test_a_shared_path_merges_two_findings(self):
        # compliance_audit_sop.md points every finding in a namespace at one
        # shared default-sa-automount.yaml, so this is the common case.
        groups = audit_report.remediation_groups(
            [
                manifest_finding("a", "shared.yaml"),
                manifest_finding("b", "shared.yaml"),
                manifest_finding("c", "other.yaml"),
            ]
        )
        self.assertEqual(self.ids(groups), [["a", "b"], ["c"]])

    def test_grouping_is_transitive(self):
        # Today's schema is one path per finding, which makes groups plain
        # equivalence classes and never exercises the union step. The union-find
        # is written to be transitive anyway, so drive it through the path
        # accessor: a—b share x, b—c share y, so all three are one PR.
        paths = {"a": {"x.yaml"}, "b": {"x.yaml", "y.yaml"}, "c": {"y.yaml"}}
        findings = [manifest_finding(fid, f"{fid}.yaml") for fid in ("a", "b", "c")]
        with patch.object(
            audit_report, "_finding_paths", lambda f: paths[f["id"]]
        ):
            groups = audit_report.remediation_groups(findings)
        self.assertEqual(self.ids(groups), [["a", "b", "c"]])

    def test_grouping_is_independent_of_input_order(self):
        findings = [
            manifest_finding("c", "other.yaml"),
            manifest_finding("b", "shared.yaml"),
            manifest_finding("a", "shared.yaml"),
        ]
        self.assertEqual(
            self.ids(audit_report.remediation_groups(findings)),
            [["a", "b"], ["c"]],
        )

    def test_non_manifest_findings_do_not_form_groups(self):
        groups = audit_report.remediation_groups(
            [
                make_finding(fid="a", remediation={"kind": "gcloud", "note": "g"}),
                make_finding(fid="b", remediation={"kind": "manual", "note": "m"}),
            ]
        )
        self.assertEqual(groups, [])

    def test_branch_is_keyed_on_the_paths_not_the_finding_ids(self):
        # Finding ids are regenerated from scratch every run. Keying the branch
        # on one of them means that the day a group's lowest id resolves, the
        # survivors rename their branch, the open pull request is orphaned, and
        # a duplicate opens against the same file. The path set is what makes
        # the group a group, and it is stable across id churn.
        first = audit_report.group_branch_for(
            AUDIT, [manifest_finding("zeta", "s.yaml"), manifest_finding("alpha", "s.yaml")]
        )
        after_alpha_resolved = audit_report.group_branch_for(
            AUDIT, [manifest_finding("zeta", "s.yaml")]
        )
        self.assertEqual(first, after_alpha_resolved)
        self.assertTrue(first.startswith(f"platform-agent/fix-{AUDIT}-s-"))

    def test_a_different_path_set_gets_a_different_branch(self):
        one = audit_report.group_branch_for(AUDIT, [manifest_finding("a", "s.yaml")])
        two = audit_report.group_branch_for(AUDIT, [manifest_finding("a", "t.yaml")])
        self.assertNotEqual(one, two)

    def test_branch_ordering_within_a_group_does_not_change_the_name(self):
        group = [manifest_finding("a", "b.yaml"), manifest_finding("b", "a.yaml")]
        self.assertEqual(
            audit_report.group_branch_for(AUDIT, group),
            audit_report.group_branch_for(AUDIT, list(reversed(group))),
        )

    def test_an_empty_group_cannot_be_named(self):
        with self.assertRaises(ValueError):
            audit_report.group_branch_for(AUDIT, [])

    def test_group_paths_are_deduplicated_and_sorted(self):
        group = [manifest_finding("a", "s.yaml"), manifest_finding("b", "s.yaml")]
        self.assertEqual(audit_report.group_paths(group), ["s.yaml"])


# --------------------------------------------------------------------------- #
# §4 finding states and promotion (§3.1, Q4)
# --------------------------------------------------------------------------- #


ALL_STATES = (
    audit_report.STATE_OPEN,
    audit_report.STATE_PR_OPEN,
    audit_report.STATE_PR_MERGED_PERSISTS,
    audit_report.STATE_RESOLVED_MERGED,
    audit_report.STATE_RESOLVED,
    audit_report.STATE_REFUSED,
    audit_report.STATE_WITHDRAWN,
)


def stale_closed_pr(**extra):
    """A pull request the *harness* closed, not a person."""
    return {"state": "CLOSED", "labels": [{"name": audit_report.STALE_CLOSED_LABEL}], **extra}


class TestFindingState(BaseTestCase):
    def test_all_seven_states(self):
        cases = [
            (True, None, audit_report.STATE_OPEN),
            (True, {"state": "OPEN"}, audit_report.STATE_PR_OPEN),
            (True, {"state": "MERGED"}, audit_report.STATE_PR_MERGED_PERSISTS),
            (True, {"state": "CLOSED"}, audit_report.STATE_REFUSED),
            # The discriminator between the last two is the label, and nothing
            # else: same state, same absence of a merge, opposite meanings.
            (True, stale_closed_pr(), audit_report.STATE_WITHDRAWN),
            (False, None, audit_report.STATE_RESOLVED),
            (False, {"state": "MERGED"}, audit_report.STATE_RESOLVED_MERGED),
        ]
        for reproduces, pr, expected in cases:
            with self.subTest(reproduces=reproduces, pr=pr):
                self.assertEqual(audit_report.derive_finding_state(reproduces, pr), expected)

    def test_merged_at_counts_as_merged_even_without_a_state(self):
        self.assertEqual(
            audit_report.derive_finding_state(True, {"mergedAt": "2026-08-01T00:00:00Z"}),
            audit_report.STATE_PR_MERGED_PERSISTS,
        )

    def test_every_state_has_a_label(self):
        for state in ALL_STATES:
            self.assertIn(state, audit_report.STATE_LABELS)
        # No state may be added to the module without being enumerated here —
        # `withdrawn` was, and went untested in every case below for a release.
        self.assertEqual(set(ALL_STATES), set(audit_report.STATE_LABELS))

    def test_withdrawn_and_refused_do_not_share_a_label(self):
        # Rendering a harness withdrawal as `fix refused` tells the reader a
        # person declined the fix when no person was involved.
        self.assertNotEqual(
            audit_report.STATE_LABELS[audit_report.STATE_WITHDRAWN],
            audit_report.STATE_LABELS[audit_report.STATE_REFUSED],
        )


class TestPromotion(BaseTestCase):
    def test_only_critical_manifest_findings_auto_promote(self):
        findings = [
            manifest_finding("crit", "a.yaml", severity="critical"),
            manifest_finding("maj", "b.yaml", severity="major"),
            make_finding(
                fid="crit-gcloud",
                severity="critical",
                remediation={"kind": "gcloud", "note": "g"},
            ),
        ]
        plan = audit_report.promotion_candidates(findings, {})
        self.assertEqual(plan.promote, ["crit"])
        self.assertEqual(plan.withheld, [])

    def test_a_finding_with_an_existing_pr_is_not_promoted_again(self):
        # Every PR state, because "already has a PR" is not one condition. Only
        # a close the *harness* made is re-promotable; the doc calls that row
        # `withdrawn`, and testing one state left the other three unguarded.
        for label, pr_state, expect_promoted in [
            ("open", {"state": "OPEN"}, False),
            ("merged", {"state": "MERGED"}, False),
            ("closed by a person", {"state": "CLOSED"}, False),
            ("withdrawn by the harness", stale_closed_pr(), True),
        ]:
            with self.subTest(pr=label):
                plan = audit_report.promotion_candidates(
                    [manifest_finding("crit", "a.yaml")], {"crit": pr_state}
                )
                self.assertEqual(plan.promote, ["crit"] if expect_promoted else [])
                self.assertEqual(plan.withheld, [])

    def test_a_request_reopens_a_withdrawn_fix_without_an_age_test(self):
        # A `withdrawn` pull request is treated as no pull request at all, so
        # the after-the-close age test that guards a human's `refused` close
        # must not apply — a finding that flaps would otherwise be fixable
        # exactly once, and never again after its first quiet day.
        plan = audit_report.promotion_candidates(
            [manifest_finding("crit", "a.yaml")],
            {"crit": stale_closed_pr(closedAt="2026-07-01T00:00:00Z")},
            requested=["crit"],
            requested_at={},
        )
        self.assertEqual(plan.promote, ["crit"])
        self.assertEqual(plan.superseded, [])

    def test_auto_promotion_is_capped_and_names_the_withheld(self):
        findings = [
            manifest_finding(f"c-{i:02d}", f"{i}.yaml", severity="critical")
            for i in range(9)
        ]
        plan = audit_report.promotion_candidates(findings, {})
        self.assertEqual(len(plan.promote), audit_report.AUTO_PROMOTION_CAP)
        self.assertEqual(len(plan.withheld), 4)
        self.assertEqual(set(plan.promote) & set(plan.withheld), set())

    def test_an_explicit_request_bypasses_the_cap(self):
        findings = [
            manifest_finding(f"c-{i:02d}", f"{i}.yaml", severity="critical")
            for i in range(9)
        ] + [manifest_finding("asked", "asked.yaml", severity="minor")]
        # Six requested — comfortably past the cap of five, so a request that
        # was merely being counted against it would show here.
        asked = ["asked", "c-08", "c-07", "c-06", "c-05", "c-04"]
        plan = audit_report.promotion_candidates(findings, {}, requested=asked)
        for fid in asked:
            self.assertIn(fid, plan.promote)
        # The six requested are uncapped, and the auto path still sweeps what
        # is left — here the four criticals nobody named, under the cap of five.
        self.assertEqual(len(plan.promote), len(asked) + 4)
        self.assertEqual(set(asked) & set(plan.withheld), set())

    def test_a_wrong_id_refusal_names_the_ids_that_would_have_worked(self):
        # Without the hint the requester's only recourse is to re-read the
        # ledger, on a daily cron: two round trips, two days, for a typo.
        findings = [manifest_finding("real-one", "a.yaml")]
        comment = {
            "id": "IC_1",
            "body": "/remediate rael-one",
            "authorAssociation": "MEMBER",
            "author": {"login": "operator"},
        }
        requests = audit_report.parse_remediate_commands([comment], findings)
        reason = requests.refusals[0]["reasons"][0]
        self.assertIn("`real-one`", reason)

    def test_a_non_manifest_refusal_names_the_ids_that_would_have_worked(self):
        findings = [
            make_finding(fid="g", remediation={"kind": "gcloud", "note": "g"}),
            manifest_finding("real-one", "a.yaml"),
        ]
        comment = {
            "id": "IC_1",
            "body": "/remediate g",
            "authorAssociation": "MEMBER",
            "author": {"login": "operator"},
        }
        requests = audit_report.parse_remediate_commands([comment], findings)
        reason = requests.refusals[0]["reasons"][0]
        self.assertIn("`real-one`", reason)

    def test_a_requested_non_manifest_finding_is_not_promoted(self):
        findings = [
            make_finding(fid="g", remediation={"kind": "gcloud", "note": "g"}),
        ]
        plan = audit_report.promotion_candidates(findings, {}, requested=["g"])
        self.assertEqual(plan.promote, [])


# --------------------------------------------------------------------------- #
# /remediate parsing (§3.1) and idempotency markers
# --------------------------------------------------------------------------- #


def comment(
    body,
    association="MEMBER",
    login="dev",
    node_id="IC_1",
    created_at="2026-07-01T00:00:00Z",
):
    return {
        "id": node_id,
        "body": body,
        "author": {"login": login},
        "authorAssociation": association,
        "createdAt": created_at,
    }


class TestRemediateCommands(BaseTestCase):
    def setUp(self):
        super().setUp()
        self.findings = [
            manifest_finding("netpol-missing", "a.yaml"),
            make_finding(fid="cluster-old", remediation={"kind": "gcloud", "note": "g"}),
        ]

    def parse(self, comments):
        return audit_report.parse_remediate_commands(comments, self.findings)

    def test_an_authorized_request_is_accepted(self):
        targets, refusals, _, _ = self.parse([comment("/remediate netpol-missing")])
        self.assertEqual(targets, ["netpol-missing"])
        self.assertEqual(refusals, [])

    def test_a_commenter_without_write_access_is_refused_once(self):
        targets, refusals, _, _ = self.parse(
            [comment("/remediate netpol-missing", association="NONE", login="drive-by")]
        )
        self.assertEqual(targets, [])
        self.assertEqual(len(refusals), 1)
        reason = refusals[0]["reasons"][0]
        self.assertIn("not recorded as a collaborator", reason)
        self.assertIn("`authorAssociation: NONE`", reason)
        # The refusal reports the association it read, not a permission it
        # never queried. The old wording claimed the commenter "does not have
        # write access", which was untrue of the App that tripped this path.
        self.assertNotIn("does not have write access", reason)
        self.assertEqual(refusals[0]["comment_id"], "IC_1")

    def test_a_non_manifest_target_is_refused(self):
        targets, refusals, _, _ = self.parse([comment("/remediate cluster-old")])
        self.assertEqual(targets, [])
        self.assertEqual(len(refusals), 1)
        self.assertIn("gcloud", refusals[0]["reasons"][0])

    def test_an_unknown_target_is_refused(self):
        _, refusals, _, _ = self.parse([comment("/remediate no-such-finding")])
        self.assertIn("not a finding", refusals[0]["reasons"][0])

    def test_a_fenced_command_never_fires(self):
        body = "Here is how you would ask:\n\n```\n/remediate netpol-missing\n```\n"
        targets, refusals, _, _ = self.parse([comment(body)])
        self.assertEqual(targets, [])
        self.assertEqual(refusals, [])

    def test_remediate_all_expands_to_promotable_targets_only(self):
        targets, refusals, _, _ = self.parse([comment("/remediate all")])
        self.assertEqual(targets, ["netpol-missing"])
        self.assertEqual(refusals, [])

    def test_remediate_all_with_nothing_promotable_is_answered(self):
        # `all` over a report of `gcloud` and `manual` fixes expands to the
        # empty set, which produced neither an acceptance nor a refusal — so
        # nothing was posted and no marker was written, and every later run
        # reached the same silence. The requester waits on an answer that was
        # never coming.
        self.findings = [
            make_finding(fid="cluster-old", remediation={"kind": "gcloud", "note": "g"})
        ]
        targets, refusals, accepted, _ = self.parse([comment("/remediate all")])
        self.assertEqual(targets, [])
        self.assertEqual(accepted, {})
        self.assertEqual(len(refusals), 1)
        self.assertIn("matched nothing", refusals[0]["reasons"][0])
        self.assertIn("nothing to promote", refusals[0]["reasons"][0])

    def test_a_command_must_start_the_line(self):
        targets, _, _, _ = self.parse([comment("maybe we should /remediate netpol-missing")])
        self.assertEqual(targets, [])

    def test_a_mid_sentence_command_is_answered_not_ignored(self):
        # Silence here is indistinguishable from an audit that has not run yet,
        # so the requester waits a day and asks again the same wrong way.
        targets, refusals, _, _ = self.parse(
            [comment("maybe we should /remediate netpol-missing")]
        )
        self.assertEqual(targets, [])
        self.assertEqual(len(refusals), 1)
        self.assertIn("start of its own line", refusals[0]["reasons"][0])
        self.assertIn("`netpol-missing`", refusals[0]["reasons"][0])

    def test_a_mid_sentence_command_from_a_stranger_is_left_alone(self):
        # It would have been refused for write access anyway, and correcting a
        # stranger's syntax on a request they cannot make is pure noise.
        targets, refusals, _, _ = self.parse(
            [
                comment(
                    "maybe we should /remediate netpol-missing",
                    association="NONE",
                    login="drive-by",
                )
            ]
        )
        self.assertEqual(targets, [])
        self.assertEqual(refusals, [])

    def test_the_command_quoted_in_a_code_span_is_not_an_attempt(self):
        # Documenting the syntax must not trip the syntax check. This is also
        # what stops the harness answering its own replies, which backtick
        # every `/remediate` they mention.
        targets, refusals, _, _ = self.parse(
            [comment("You can ask for it with `/remediate <finding-id>` when it lands.")]
        )
        self.assertEqual(targets, [])
        self.assertEqual(refusals, [])

    def test_the_harness_own_reply_does_not_provoke_another_reply(self):
        # The refusal comment is read back off the issue on the next run. If it
        # read as a request, every run would answer the previous run's answer.
        own = audit_report.render_refusal_comment(
            {
                "comment_id": "IC_1",
                "author": "someone",
                "reasons": ["`/remediate` on its own does not say what to fix."],
            },
            datetime(2026, 7, 30, 9, 0, tzinfo=timezone.utc),
        )
        targets, refusals, _, _ = self.parse([comment(own, node_id="IC_2")])
        self.assertEqual(targets, [])
        self.assertEqual(refusals, [])

    def test_a_bare_command_names_the_ids_that_would_work(self):
        targets, refusals, _, _ = self.parse([comment("/remediate")])
        self.assertEqual(targets, [])
        self.assertEqual(len(refusals), 1)
        reason = refusals[0]["reasons"][0]
        # Not the old "`` is not a finding in the current report", which told a
        # requester holding a correct id that their id was wrong.
        self.assertNotIn("not a finding", reason)
        self.assertIn("does not say what to fix", reason)
        self.assertIn("`netpol-missing`", reason)

    def test_a_bare_command_is_not_read_as_all(self):
        # `netpol-missing` is promotable; an empty target must not promote it.
        targets, _, accepted, _ = self.parse([comment("/remediate")])
        self.assertEqual(targets, [])
        self.assertEqual(accepted, {})

    def test_a_bare_command_with_nothing_promotable_says_so(self):
        requests = audit_report.parse_remediate_commands(
            [comment("/remediate")],
            [make_finding(fid="cluster-old", remediation={"kind": "gcloud", "note": "g"})],
        )
        self.assertIn("nothing to promote", requests.refusals[0]["reasons"][0])

    def test_no_comment_this_harness_writes_reads_as_a_request(self):
        # Everything below is posted onto the ledger and read back by the next
        # run. One un-backticked `/remediate` in any of them and the harness
        # answers itself forever, once per run, on a cron.
        findings = [manifest_finding("netpol-missing", "a.yaml")]
        written = {
            "delta": audit_report.render_delta_comment(
                AUDIT, ["netpol-missing"], ["gone"], findings, {"gone": "t"}, NOW
            ),
            "clean": audit_report.render_clean_comment(AUDIT, make_doc(findings=[]), NOW),
            "refusal": audit_report.render_refusal_comment(
                {"comment_id": "IC_1", "author": "a", "reasons": ["nope"]}, NOW
            ),
            "ack": audit_report.render_ack_comment(
                "IC_1", ["netpol-missing"], {"netpol-missing": "opened #4"}, NOW
            ),
            "persists": audit_report.render_persists_comment(AUDIT, findings[0], NOW),
            "stale": audit_report.render_stale_close_comment(
                AUDIT, findings, NOW, pr_number=4
            ),
        }
        for name, body in written.items():
            with self.subTest(comment=name):
                requests = audit_report.parse_remediate_commands(
                    [comment(body, node_id=f"IC_{name}")], findings
                )
                self.assertEqual(requests.targets, [])
                self.assertEqual(requests.refusals, [])

    def test_the_id_list_in_a_refusal_is_capped(self):
        findings = [manifest_finding(f"f-{n:03d}", f"{n}.yaml") for n in range(25)]
        requests = audit_report.parse_remediate_commands([comment("/remediate")], findings)
        reason = requests.refusals[0]["reasons"][0]
        self.assertEqual(reason.count("`f-"), audit_report.MAX_HINT_IDS)
        self.assertIn(f"and {25 - audit_report.MAX_HINT_IDS} more", reason)

    def test_one_refusal_per_comment_not_per_bad_target(self):
        body = "/remediate cluster-old\n/remediate no-such-finding\n"
        _, refusals, _, _ = self.parse([comment(body)])
        self.assertEqual(len(refusals), 1)
        self.assertEqual(len(refusals[0]["reasons"]), 2)

    def test_targets_are_deduplicated_and_sorted(self):
        targets, _, _, _ = self.parse(
            [
                comment("/remediate netpol-missing", node_id="IC_1"),
                comment("/remediate netpol-missing", node_id="IC_2"),
            ]
        )
        self.assertEqual(targets, ["netpol-missing"])


class TestAMachineCannotAuthorizeItself(BaseTestCase):
    """`/remediate` from a bot account, which is what happened on issue #29.

    The audit agent read the ledger it had just written, took the header's
    "comment `/remediate all`" as an instruction to itself, and posted it three
    times under the App credentials it uses to open and merge pull requests.
    The only thing between that and a self-authorized pull request was
    `authorAssociation: NONE` — a field that happens to be empty for App
    comments, not a decision anybody made.
    """

    def setUp(self):
        super().setUp()
        self.findings = [manifest_finding("netpol-missing", "a.yaml")]

    def parse(self, comments):
        return audit_report.parse_remediate_commands(comments, self.findings)

    def bot(self, body="/remediate all", **kw):
        kw.setdefault("login", "kube-agents-minty[bot]")
        kw.setdefault("association", "NONE")
        return comment(body, **kw)

    def test_a_bot_login_is_recognised_as_a_machine(self):
        self.assertTrue(audit_report.is_machine_author(self.bot()))

    def test_a_typed_actor_is_recognised_even_without_the_suffix(self):
        # The GraphQL struct `fetch_issue_comments` returns strips `[bot]` from
        # the login, so the suffix alone is not a complete test.
        for author in (
            {"login": "minty", "__typename": "Bot"},
            {"login": "minty", "is_bot": True},
        ):
            with self.subTest(author=author):
                self.assertTrue(
                    audit_report.is_machine_author(
                        {"author": author, "authorAssociation": "NONE"}
                    )
                )

    def test_the_harness_reading_its_own_comment_is_a_machine(self):
        self.assertTrue(
            audit_report.is_machine_author(
                {
                    "author": {"login": "minty"},
                    "authorAssociation": "NONE",
                    "viewerDidAuthor": True,
                }
            )
        )

    def test_an_operator_running_the_audit_under_their_own_token_is_not(self):
        # `viewerDidAuthor` is true for a human who runs this audit with their
        # own credentials. Their `/remediate` has to keep working, which is why
        # the self-authored signal is paired with the missing association.
        human = {
            "author": {"login": "adamparco"},
            "authorAssociation": "OWNER",
            "viewerDidAuthor": True,
        }
        self.assertFalse(audit_report.is_machine_author(human))

    def test_a_person_without_standing_is_not_mistaken_for_a_machine(self):
        stranger = comment("/remediate all", association="NONE", login="drive-by")
        self.assertFalse(audit_report.is_machine_author(stranger))

    def test_a_bots_command_opens_nothing(self):
        targets, _, accepted, _ = self.parse([self.bot()])
        self.assertEqual(targets, [])
        self.assertEqual(accepted, {})

    def test_a_bots_command_is_ignored_in_silence(self):
        # Refusing it would post a comment addressed to the bot that wrote it,
        # which is one more comment for that bot to read tomorrow. Issue #29
        # carries three such refusals, each talking to nobody.
        _, refusals, _, _ = self.parse([self.bot()])
        self.assertEqual(refusals, [])

    def test_a_bot_is_not_answered_on_a_clean_run_either(self):
        got = audit_report.unanswered_remediate_comments([self.bot()])
        self.assertEqual(got, [])

    def test_a_bot_contributes_no_pending_targets_at_start(self):
        # Belt and braces: a bot user added as a collaborator would clear the
        # association check that stopped the App.
        collaborator_bot = self.bot("/remediate netpol-missing", association="MEMBER")
        self.assertEqual(
            audit_report.pending_remediate_targets([collaborator_bot]), []
        )

    def test_a_collaborator_bot_is_still_refused_promotion(self):
        collaborator_bot = self.bot("/remediate netpol-missing", association="MEMBER")
        targets, refusals, _, _ = self.parse([collaborator_bot])
        self.assertEqual(targets, [])
        self.assertEqual(refusals, [])

    def test_a_person_is_unaffected_by_the_gate(self):
        targets, refusals, _, _ = self.parse(
            [comment("/remediate netpol-missing", association="MEMBER")]
        )
        self.assertEqual(targets, ["netpol-missing"])
        self.assertEqual(refusals, [])


class TestMarkers(BaseTestCase):
    def test_persists_marker_round_trips(self):
        body = f"Some text\n\n{audit_report.persists_marker('abc')}\n"
        self.assertTrue(audit_report.has_marker(body, audit_report.PERSISTS_MARKER_RE, "abc"))
        self.assertFalse(audit_report.has_marker(body, audit_report.PERSISTS_MARKER_RE, "xyz"))

    def test_refused_marker_round_trips(self):
        body = f"Reply\n{audit_report.refused_marker('IC_9')}\n"
        self.assertTrue(audit_report.has_marker(body, audit_report.REFUSED_MARKER_RE, "IC_9"))
        self.assertFalse(audit_report.has_marker(body, audit_report.REFUSED_MARKER_RE, "IC_8"))

    def test_absent_body_has_no_marker(self):
        self.assertFalse(audit_report.has_marker(None, audit_report.PERSISTS_MARKER_RE, "abc"))
        self.assertFalse(audit_report.has_marker("", audit_report.PERSISTS_MARKER_RE, "abc"))


class TestDeltaBlockAnchoring(BaseTestCase):
    def test_a_marker_quoted_inside_an_excerpt_cannot_hijack_the_real_block(self):
        # An opener injected mid-line must not start a match that spans into
        # the real block below it.
        body = (
            "Evidence:\n"
            '    text <!-- audit-findings: ["injected"] and more\n'
            "\n"
            + audit_report.delta_block(["real-one", "real-two"])
            + "\n"
        )
        self.assertEqual(audit_report.parse_delta_block(body), ["real-one", "real-two"])


# --------------------------------------------------------------------------- #
# Remediation pull requests — the Tier 2 half of the two-tier model
# --------------------------------------------------------------------------- #


def pr(number, branch, state="OPEN", merged_at=None, body="", url=None):
    return {
        "number": number,
        "headRefName": branch,
        "state": state,
        "mergedAt": merged_at,
        "url": url or f"https://github.com/acme/fleet/pull/{number}",
        "body": body,
    }


class TestSelectPrByHead(BaseTestCase):
    def test_highest_number_wins(self):
        # A branch reused after its first PR merged must report the live one.
        prs = [pr(3, "b"), pr(11, "b"), pr(7, "b")]
        self.assertEqual(audit_report._select_pr_by_head(prs, "b")["number"], 11)

    def test_no_match_is_none(self):
        self.assertIsNone(audit_report._select_pr_by_head([pr(1, "other")], "b"))
        self.assertIsNone(audit_report._select_pr_by_head([], "b"))
        self.assertIsNone(audit_report._select_pr_by_head(None, "b"))

    def test_a_fork_qualified_head_still_matches(self):
        # gh reports `owner:branch` for a cross-repository PR. Accepting the
        # suffix keeps the lookup working if remediation ever moves to a fork,
        # instead of silently reporting every finding as having no PR.
        prs = [pr(4, "adamparco:platform-agent/fix-x")]
        found = audit_report._select_pr_by_head(prs, "platform-agent/fix-x")
        self.assertEqual(found["number"], 4)

    def test_a_bare_substring_does_not_match(self):
        self.assertIsNone(audit_report._select_pr_by_head([pr(4, "xfix-x")], "fix-x"))


class TestReconcileRemediationPrs(BaseTestCase):
    def setUp(self):
        super().setUp()
        # a and b share a path, so they are one group on one branch; c is alone.
        self.findings = [
            make_finding(fid="a"),
            make_finding(fid="b"),
            make_finding(
                fid="c",
                remediation={
                    "kind": "manifest",
                    "path": "clusters/stage-eu/psp.yaml",
                    "note": "n",
                },
            ),
        ]

    def test_one_pr_fans_out_to_every_member_of_its_group(self):
        branch = audit_report.group_branch_for(AUDIT, self.findings[:2])
        by_finding, urls = audit_report.reconcile_remediation_prs(
            AUDIT, self.findings, [pr(9, branch)]
        )
        self.assertEqual(by_finding["a"]["number"], 9)
        self.assertEqual(by_finding["b"]["number"], 9)
        self.assertIsNone(by_finding["c"])
        self.assertEqual(urls["a"], urls["b"])
        self.assertNotIn("c", urls)

    def test_no_prs_leaves_every_finding_unlinked(self):
        by_finding, urls = audit_report.reconcile_remediation_prs(AUDIT, self.findings, [])
        self.assertEqual(set(by_finding), {"a", "b", "c"})
        self.assertTrue(all(v is None for v in by_finding.values()))
        self.assertEqual(urls, {})


class TestOpenRemediationPr(HarnessTestCase):
    def setUp(self):
        super().setUp()
        self.group = [make_finding(fid="a")]
        self.path = "clusters/prod-us-east/payments-netpol.yaml"
        self.snapshot = {self.path: b"# fix\n"}
        self.branch = audit_report.group_branch_for(AUDIT, self.group)

    def open_it(self, existing=None):
        # Called directly rather than through main(), so redirect the harness's
        # own log lines instead of letting them print over the test output.
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            result = audit_report.open_remediation_pr(
                "acme/fleet",
                AUDIT,
                self.group,
                snapshot=self.snapshot,
                root=self.workspace,
                issue_number=42,
                existing=existing,
                generated_at=NOW,
            )
        self.err = err.getvalue()
        return result

    def test_branch_commit_push_then_create_in_that_order(self):
        self.harness.replies = {"pr create": "https://github.com/acme/fleet/pull/8\n"}
        url = self.open_it()

        self.assertEqual(url, "https://github.com/acme/fleet/pull/8")
        branch = self.branch
        # The base-branch lookup is a read, not a step in the sequence this
        # test is about, and it happens once per workspace rather than once per
        # group. Dropped here; `TestRemediationBaseBranch` is what asserts it.
        order = [
            c
            for c in self.harness.calls
            if c[0] in ("git", "gh") and c[1] not in ("symbolic-ref", "remote")
        ]
        self.assertEqual(order[0], ["git", "fetch", "origin", "main"])
        self.assertEqual(
            order[1], ["git", "checkout", "--force", "-B", branch, "origin/main"]
        )
        self.assertEqual(
            order[2], ["git", "--literal-pathspecs", "add", "--", self.path]
        )
        self.assertEqual(order[3], ["git", "diff", "--cached", "--quiet"])
        self.assertEqual(order[4][:2], ["git", "commit"])
        self.assertEqual(order[5], ["git", "push", "-f", "origin", branch])
        self.assertEqual(order[6][:2], ["gh", "pr"])

        # The file the pull request carries comes from the snapshot, not from
        # whatever survived the forced checkout.
        self.assertEqual((self.workspace / self.path).read_bytes(), b"# fix\n")

    def test_create_carries_all_four_labels(self):
        self.harness.replies = {"pr create": "https://github.com/acme/fleet/pull/8\n"}
        self.open_it()
        create = self.harness.gh_calls("pr", "create")[0]
        for label in (
            "agent:audit",
            f"audit:{AUDIT}",
            "audit:remediation",
            "severity:critical",
        ):
            self.assertIn(label, create, label)
        self.assertIn("--base", create)
        self.assertIn("main", create)

    def test_nothing_to_commit_opens_no_pull_request(self):
        # main already carries the fix. Opening a diff-less PR is the exact
        # mistake the ledger split exists to end.
        self.harness.staged = False
        self.assertIsNone(self.open_it())
        self.assertFalse(self.harness.matching("git", "commit"))
        self.assertFalse(self.harness.matching("git", "push"))
        self.assertEqual(self.harness.gh_calls("pr", "create"), [])

    def test_an_unreadable_index_is_never_read_as_already_fixed(self):
        # rc 0 is "nothing staged" and rc 1 is "there is a commit to make".
        # Anything else means git could not read the index — a missing
        # committer identity, a failed hook, a corrupt .git — and inferring
        # "already fixed on main" from it drops the fix on the floor silently,
        # every run, forever.
        self.harness.failures = {"diff --cached --quiet": 128}
        with self.assertRaises(RuntimeError):
            self.open_it()
        self.assertFalse(self.harness.matching("git", "push"))

    def test_an_open_pr_is_edited_not_duplicated(self):
        existing = pr(8, self.branch)
        url = self.open_it(existing=existing)
        self.assertEqual(url, "https://github.com/acme/fleet/pull/8")
        self.assertEqual(self.harness.gh_calls("pr", "create"), [])
        edit = self.harness.gh_calls("pr", "edit")[0]
        self.assertIn("8", edit)
        self.assertIn("--body-file", edit)

    def test_a_closed_pr_on_the_branch_is_replaced_not_reopened(self):
        self.harness.replies = {"pr create": "https://github.com/acme/fleet/pull/9\n"}
        self.open_it(existing=pr(8, self.branch, state="CLOSED"))
        self.assertEqual(self.harness.gh_calls("pr", "edit"), [])
        self.assertEqual(len(self.harness.gh_calls("pr", "create")), 1)


class TestRemediationBaseBranch(HarnessTestCase):
    """Remediation branches are cut from the repository's default branch.

    Hardcoding `main` did not degrade on a repository that uses something else,
    it aborted: `git fetch origin main` fails, and the remediation half of the
    run dies after the findings have already been written and the ledger
    updated. So these assert the fetch, the checkout *and* the `--base` handed
    to `gh pr create` — all three have to agree, and a fix that only changed
    the fetch would open pull requests against a branch they were never cut
    from.
    """

    def setUp(self):
        super().setUp()
        self.group = [make_finding(fid="a")]
        self.snapshot = {
            "clusters/prod-us-east/payments-netpol.yaml": b"# fix\n",
        }
        self.harness.replies = {"pr create": "https://github.com/acme/fleet/pull/8\n"}

    def open_it(self):
        with contextlib.redirect_stderr(io.StringIO()):
            return audit_report.open_remediation_pr(
                "acme/fleet",
                AUDIT,
                self.group,
                snapshot=self.snapshot,
                root=self.workspace,
                issue_number=42,
                existing=None,
                generated_at=NOW,
            )

    def base_used(self):
        create = self.harness.gh_calls("pr", "create")[0]
        return create[create.index("--base") + 1]

    def test_a_master_repository_is_branched_from_master(self):
        self.harness.origin_head = "origin/master"
        self.open_it()
        self.assertIn(["git", "fetch", "origin", "master"], self.harness.calls)
        self.assertTrue(
            any(c[:2] == ["git", "checkout"] and "origin/master" in c for c in self.harness.calls)
        )
        self.assertEqual(self.base_used(), "master")

    def test_the_env_override_wins_over_origin_head(self):
        # An operator whose GitOps flow merges into a long-running release
        # trunk sets GITOPS_BASE_BRANCH; origin/HEAD still says main, and the
        # override is the whole point.
        self.harness.origin_head = "origin/main"
        with patch.dict(os.environ, {"GITOPS_BASE_BRANCH": "release-1.29"}):
            self.open_it()
        self.assertIn(["git", "fetch", "origin", "release-1.29"], self.harness.calls)
        self.assertEqual(self.base_used(), "release-1.29")

    def test_a_clone_with_no_origin_head_repairs_it_then_falls_back(self):
        # `git symbolic-ref` exits 1 on a clone that never recorded origin/HEAD
        # — `git remote set-head --auto` is the repair, and `main` is the answer
        # when even that turns up nothing. Aborting here would be worse than a
        # guess: the guess is right for almost every repository.
        self.harness.origin_head = None
        self.open_it()
        self.assertIn(
            ["git", "remote", "set-head", "origin", "--auto"], self.harness.calls
        )
        self.assertIn(["git", "fetch", "origin", "main"], self.harness.calls)
        self.assertEqual(self.base_used(), "main")

    def test_the_lookup_is_not_repeated_for_a_second_group(self):
        # Two groups, one workspace: the answer is memoised, so the second
        # group costs a checkout and not another round-trip.
        self.open_it()
        before = len(self.harness.matching("symbolic-ref"))
        self.group = [
            make_finding(
                fid="b",
                remediation={
                    "kind": "manifest",
                    "path": "clusters/stage-eu/psp.yaml",
                    "note": "n",
                },
            )
        ]
        self.snapshot = {"clusters/stage-eu/psp.yaml": b"# fix\n"}
        self.open_it()
        self.assertEqual(len(self.harness.matching("symbolic-ref")), before)


class TestRemediationPrBody(BaseTestCase):
    def test_part_of_not_closes(self):
        # "Closes #N" would retire the ledger the moment one fix merged.
        body = audit_report.render_remediation_pr_body(
            AUDIT, [make_finding(fid="a")], issue_number=42, generated_at=NOW
        )
        self.assertIn("Part of #42", body)
        self.assertNotIn("Closes #42", body)

    def test_body_records_the_findings_it_covers(self):
        group = [make_finding(fid="a"), make_finding(fid="b")]
        body = audit_report.render_remediation_pr_body(
            AUDIT, group, issue_number=42, generated_at=NOW
        )
        self.assertEqual(audit_report.parse_delta_block(body), ["a", "b"])
        self.assertIn("## Files", body)
        self.assertIn("clusters/prod-us-east/payments-netpol.yaml", body)

    def test_title_names_the_head_finding_and_counts_the_rest(self):
        one = [make_finding(fid="a", title="No NetworkPolicy")]
        self.assertEqual(
            audit_report.remediation_pr_title(AUDIT, one),
            "fix(compliance-audit): No NetworkPolicy",
        )
        two = one + [make_finding(fid="b", severity="minor", title="Other")]
        self.assertTrue(
            audit_report.remediation_pr_title(AUDIT, two).endswith("(+1 more)")
        )


class TestStaleCloseEligibility(HarnessTestCase):
    """Which open pull requests a stale sweep may touch at all.

    Named apart from `TestStaleCloseLabelling` below on purpose: the two shared
    a class name, so Python rebound it before unittest collected and these four
    cases never ran — the suite reported them as passing by never executing
    them. Any new stale-close class needs its own name.
    """

    def close(self, prs, current_ids):
        return audit_report.close_stale_remediation_prs(
            "acme/fleet", AUDIT, prs, current_ids, {"a": "Old title"}, {}, NOW
        )

    def test_closes_and_comments_but_never_deletes_the_branch(self):
        stale = pr(8, "platform-agent/fix-x", body=audit_report.delta_block(["a"]))
        closed = self.close([stale], set())

        self.assertEqual(closed, ["https://github.com/acme/fleet/pull/8"])
        comment = self.harness.gh_calls("pr", "comment")[0]
        self.assertIn("8", comment)
        close = self.harness.gh_calls("pr", "close")[0]
        # The branch outlives the pull request: a returning finding pushes to it.
        self.assertNotIn("--delete-branch", close)
        # Comment before close, so the reason is on the PR when it closes.
        self.assertLess(
            self.harness.calls.index(comment), self.harness.calls.index(close)
        )

    def test_a_pr_with_one_live_finding_stays_open(self):
        live = pr(8, "platform-agent/fix-x", body=audit_report.delta_block(["a", "b"]))
        self.assertEqual(self.close([live], {"b"}), [])
        self.assertEqual(self.harness.gh_calls("pr", "close"), [])

    def test_an_already_closed_pr_is_left_alone(self):
        done = pr(
            8, "platform-agent/fix-x", state="MERGED", body=audit_report.delta_block(["a"])
        )
        self.assertEqual(self.close([done], set()), [])
        self.assertEqual(self.harness.gh_calls("pr", "close"), [])

    def test_a_pr_with_no_hidden_block_is_left_alone(self):
        # Hand-opened, or opened by an older harness: it says nothing about
        # which findings it covers, so closing it would be a guess.
        self.assertEqual(self.close([pr(8, "b", body="hello")], set()), [])
        self.assertEqual(self.harness.gh_calls("pr", "close"), [])

    def test_a_pr_stamped_with_another_scheme_is_left_alone(self):
        # It names findings by ids this run cannot join against, so "none of
        # them reproduce any more" is unknowable, not true. Closing it would
        # retire a live fix with a comment saying the problem went away.
        old = pr(
            8,
            "platform-agent/fix-x",
            body='<!-- audit-findings: ["a"] -->\n<!-- audit-id-scheme: 0 -->',
        )
        self.assertEqual(self.close([old], set()), [])
        self.assertEqual(self.harness.gh_calls("pr", "close"), [])

    def test_an_orphaned_branch_closes_whatever_scheme_stamped_it(self):
        # The orphan rule joins on manifest paths, not ids, so the stamp has no
        # bearing on it — and the close comment still names what it covered.
        orphan = pr(
            8,
            "platform-agent/fix-gone",
            body='<!-- audit-findings: ["a"] -->\n<!-- audit-id-scheme: 0 -->',
        )
        closed = audit_report.close_stale_remediation_prs(
            "acme/fleet",
            AUDIT,
            [orphan],
            {"a"},
            {"a": "Old title"},
            {},
            NOW,
            branch_by_finding={"a": "platform-agent/fix-current"},
        )
        self.assertEqual(closed, ["https://github.com/acme/fleet/pull/8"])
        body = "".join(self.harness.bodies_for("pr", "comment"))
        self.assertIn("Old title", body)
        # The ids do not join under this scheme, so "no longer reproduces" is
        # not something this run established. The branch is.
        self.assertIn("lives on a different branch", body)
        self.assertNotIn("no longer reproduces", body)


class TestMergedButPersists(HarnessTestCase):
    def setUp(self):
        super().setUp()
        self.finding = make_finding(fid="a")
        self.merged = pr(
            8,
            "platform-agent/fix-x",
            state="MERGED",
            merged_at="2026-07-01T00:00:00Z",
        )

    def run_it(self, prs_by_finding):
        audit_report.comment_on_merged_but_persisting(
            "acme/fleet", AUDIT, [self.finding], prs_by_finding, NOW
        )

    def test_comments_once_and_never_reopens(self):
        self.harness.replies = {"--json comments": json.dumps({"comments": []})}
        self.run_it({"a": self.merged})
        comment = self.harness.gh_calls("pr", "comment")[0]
        self.assertIn("8", comment)
        self.assertEqual(self.harness.gh_calls("pr", "reopen"), [])

    def test_silent_when_the_marker_is_already_in_the_pr_body(self):
        self.merged["body"] = f"merged\n{audit_report.persists_marker('a')}\n"
        self.harness.replies = {"--json comments": json.dumps({"comments": []})}
        self.run_it({"a": self.merged})
        self.assertEqual(self.harness.gh_calls("pr", "comment"), [])

    def test_silent_when_the_marker_is_already_in_a_pr_comment(self):
        prior = {"body": f"said it\n{audit_report.persists_marker('a')}\n"}
        self.harness.replies = {"--json comments": json.dumps({"comments": [prior]})}
        self.run_it({"a": self.merged})
        self.assertEqual(self.harness.gh_calls("pr", "comment"), [])

    def test_an_open_pr_is_not_the_persists_case(self):
        self.run_it({"a": pr(8, "platform-agent/fix-x")})
        self.assertEqual(self.harness.gh_calls("pr", "comment"), [])


class TestReplyToRefusals(HarnessTestCase):
    def refusal(self, comment_id="IC_1"):
        return {
            "comment_id": comment_id,
            "author": "drive-by",
            "reasons": ["no write access"],
        }

    def test_one_reply_carrying_the_requesting_comment_id(self):
        audit_report.reply_to_refusals("acme/fleet", 42, [self.refusal()], [], NOW)
        self.assertEqual(len(self.harness.gh_calls("issue", "comment")), 1)

    def test_silent_when_that_comment_was_already_answered(self):
        answered = [{"body": f"earlier\n{audit_report.refused_marker('IC_1')}\n"}]
        audit_report.reply_to_refusals("acme/fleet", 42, [self.refusal()], answered, NOW)
        self.assertEqual(self.harness.gh_calls("issue", "comment"), [])

    def test_a_different_comment_still_gets_its_own_reply(self):
        answered = [{"body": f"earlier\n{audit_report.refused_marker('IC_1')}\n"}]
        audit_report.reply_to_refusals(
            "acme/fleet", 42, [self.refusal("IC_2")], answered, NOW
        )
        self.assertEqual(len(self.harness.gh_calls("issue", "comment")), 1)


class TestAutoPromotionInFinish(HarnessTestCase):
    def setUp(self):
        super().setUp()
        self.harness.replies = {
            "issue list": "[]",
            "issue create": "https://github.com/acme/fleet/issues/7\n",
            "pr create": "https://github.com/acme/fleet/pull/8\n",
            "rev-parse --abbrev-ref": "feature-branch\n",
        }

    def test_a_critical_manifest_finding_gets_a_pull_request(self):
        self.touch("clusters/prod-us-east/payments-netpol.yaml")
        self.assertEqual(self.run_finish(make_doc()), 0)
        out = self.stdout_json()
        self.assertEqual(out["prs_opened"], ["https://github.com/acme/fleet/pull/8"])
        self.assertEqual(len(self.harness.gh_calls("pr", "create")), 1)

    def test_the_ledger_is_rewritten_once_the_pull_request_exists(self):
        # The body was rendered before the PR had a number, so it could not
        # have linked it. One extra edit beats making a reader wait a day.
        self.touch("clusters/prod-us-east/payments-netpol.yaml")
        self.run_finish(make_doc())
        self.assertTrue(self.harness.gh_calls("issue", "edit", "7"))

    def test_a_gcloud_critical_is_never_auto_promoted(self):
        doc = make_doc(
            findings=[
                make_finding(fid="a", remediation={"kind": "gcloud", "note": "x"})
            ]
        )
        self.assertEqual(self.run_finish(doc), 0)
        self.assertEqual(self.harness.gh_calls("pr", "create"), [])

    def test_the_cap_holds_and_the_ledger_names_what_it_withheld(self):
        findings = []
        for i in range(7):
            findings.append(
                make_finding(
                    fid=f"crit-{i}",
                    title=f"Crit {i}",
                    remediation={
                        "kind": "manifest",
                        "path": f"clusters/prod-us-east/f{i}.yaml",
                        "note": "n",
                    },
                )
            )
            self.touch(f"clusters/prod-us-east/f{i}.yaml")

        self.assertEqual(self.run_finish(make_doc(findings=findings)), 0)
        self.assertEqual(
            len(self.harness.gh_calls("pr", "create")), audit_report.AUTO_PROMOTION_CAP
        )
        body = render_body(
            make_doc(findings=findings),
            generated_at=NOW,
            audit_id=AUDIT,
            withheld=["crit-5", "crit-6"],
        )
        self.assertIn("crit-5", body)
        self.assertIn("/remediate", body)

    def test_the_working_tree_is_left_as_it_was_found(self):
        target = self.touch("clusters/prod-us-east/payments-netpol.yaml")
        target.write_bytes(b"original\n")
        self.run_finish(make_doc())
        # Forced checkouts happened, but the caller's branch and file survive.
        self.assertEqual(target.read_bytes(), b"original\n")
        self.assertIn(
            ["git", "checkout", "--force", "feature-branch"], self.harness.calls
        )

    def test_dry_run_looks_for_manifests_in_the_clone_not_the_cwd(self):
        # The pod's working directory is the agent profile — the SOPs tell the
        # model it is not in a checkout at all. Resolving remediation paths
        # there degraded every manifest to `manual` and printed no pull request
        # body, so a dry run answered "nothing would happen" for a document
        # whose files were all present, in the right place.
        def not_a_checkout():
            raise RuntimeError("Not inside a git working tree")

        self.patch_attr("repo_root", not_a_checkout)
        self.touch("clusters/prod-us-east/payments-netpol.yaml")

        self.assertEqual(self.run_finish(make_doc(), ["--dry-run"]), 0)

        self.assertNotIn("degrades to a manual remediation", self.err)
        self.assertIn(f"platform-agent/fix-{AUDIT}", self.err)
        self.assertIn("## Files", self.out)
        self.assertEqual(self.harness.gh_calls("issue"), [])
        self.assertEqual(self.harness.gh_calls("pr"), [])

    def test_a_failed_pr_create_does_not_fail_the_run(self):
        # The ledger is already published; the finding shows as having no PR
        # and the next run retries. Losing the report costs more.
        self.touch("clusters/prod-us-east/payments-netpol.yaml")
        self.harness.failures = {"pr create": 1}
        self.assertEqual(self.run_finish(make_doc()), 0)
        self.assertEqual(self.stdout_json()["prs_opened"], [])
        self.assertIn("could not publish the fix", self.err)


class TestRemediateOnACleanRun(HarnessTestCase):
    """A command standing on a ledger the morning the fleet comes back clean.

    The clean branch returned before any comment was read, so the request got
    nothing — and then the issue closed, taking with it the thread the
    requester would have re-asked on. "Never silence" cannot have as its one
    exception the morning the issue disappears.
    """

    def comment(self, body="/remediate a", cid="IC_1", assoc="MEMBER"):
        return {
            "id": cid,
            "body": body,
            "createdAt": "2026-07-01T00:00:00Z",
            "authorAssociation": assoc,
            "author": {"login": "operator"},
        }

    def replies(self, comments):
        return {
            "issue list": self.issue_list(),
            "--json body": json.dumps({"body": "prior"}),
            "--json comments": json.dumps({"comments": comments}),
        }

    def issue_comments(self):
        return self.harness.gh_calls("issue", "comment")

    def test_a_standing_request_is_answered_before_the_ledger_closes(self):
        self.harness.replies = self.replies([self.comment()])

        self.assertEqual(self.run_finish(make_doc(findings=[])), 0)

        bodies = self.harness.bodies_for("issue", "comment")
        answer = [b for b in bodies if audit_report.acked_marker("IC_1") in b]
        self.assertEqual(len(answer), 1, bodies)
        self.assertIn("no longer reproduces", answer[0])
        self.assertIn("closing as completed", answer[0])
        self.assertTrue(self.harness.gh_calls("issue", "close"))

    def test_the_answer_is_said_once_when_the_ledger_stays_open(self):
        # Over a coverage gap the issue survives, so the marker is what stops a
        # second answer tomorrow morning, and the morning after.
        prior = self.comment(
            body=f"answered\n{audit_report.acked_marker('IC_1')}\n", cid="IC_2"
        )
        self.harness.replies = self.replies([self.comment(), prior])
        doc = make_doc(findings=[])
        doc["scope"]["skipped"] = [{"cluster": "prod-eu", "reason": "unreachable"}]

        self.assertEqual(self.run_finish(doc), 0)

        bodies = self.harness.bodies_for("issue", "comment")
        self.assertEqual(
            [b for b in bodies if audit_report.acked_marker("IC_1") in b], []
        )
        self.assertEqual(self.harness.gh_calls("issue", "close"), [])

    def test_a_gap_answer_does_not_promise_a_closure_that_is_not_happening(self):
        self.harness.replies = self.replies([self.comment()])
        doc = make_doc(findings=[])
        doc["scope"]["skipped"] = [{"cluster": "prod-eu", "reason": "unreachable"}]

        self.assertEqual(self.run_finish(doc), 0)

        bodies = self.harness.bodies_for("issue", "comment")
        answer = [b for b in bodies if audit_report.acked_marker("IC_1") in b][0]
        self.assertIn("stays open", answer)
        self.assertNotIn("closing as completed", answer)

    def test_a_comment_with_no_command_is_left_alone(self):
        self.harness.replies = self.replies(
            [self.comment(body="looks good to me, thanks")]
        )
        self.assertEqual(self.run_finish(make_doc(findings=[])), 0)
        bodies = self.harness.bodies_for("issue", "comment")
        self.assertEqual(
            [b for b in bodies if audit_report.acked_marker("IC_1") in b], []
        )


class TestUnansweredRemediateComments(unittest.TestCase):
    def comment(self, body, cid="IC_1"):
        return {"id": cid, "body": body, "author": {"login": "operator"}}

    def test_a_quoted_command_is_not_a_command(self):
        fenced = self.comment("```\n/remediate a\n```")
        self.assertEqual(audit_report.unanswered_remediate_comments([fenced]), [])

    def test_a_mention_still_earns_an_answer_when_nothing_can_be_opened(self):
        # Unlike the findings path, authorization is not consulted: nothing is
        # acted on for anybody, so "it no longer reproduces" is the true answer
        # for a writer and a non-writer alike.
        mention = self.comment("could you /remediate a please")
        got = audit_report.unanswered_remediate_comments([mention])
        self.assertEqual([r["comment_id"] for r in got], ["IC_1"])
        self.assertEqual(got[0]["targets"], [])

    def test_either_marker_counts_as_already_answered(self):
        for marker in (audit_report.acked_marker, audit_report.refused_marker):
            with self.subTest(marker=marker.__name__):
                thread = [
                    self.comment("/remediate a"),
                    self.comment(marker("IC_1"), cid="IC_2"),
                ]
                self.assertEqual(
                    audit_report.unanswered_remediate_comments(thread), []
                )


class TestRemediateSubcommand(HarnessTestCase):
    def run_remediate(self, doc, findings, extra=()):
        path = self.write_findings(doc)
        argv = ["remediate", "--audit", AUDIT, "--findings-file", path]
        for fid in findings:
            argv += ["--finding", fid]
        return self.run_main([*argv, *extra])

    def test_an_unknown_id_is_rejected_before_any_side_effect(self):
        self.touch("clusters/prod-us-east/payments-netpol.yaml")
        self.assertEqual(self.run_remediate(make_doc(), ["nope"]), 2)
        self.assertIn("not in", self.err)
        self.assertEqual(self.harness.calls, [])

    def test_a_non_manifest_target_is_rejected(self):
        doc = make_doc(
            findings=[
                make_finding(fid="a", remediation={"kind": "manual", "note": "x"})
            ]
        )
        self.assertEqual(self.run_remediate(doc, [derived_id(fid="a")]), 2)
        self.assertIn("manifest", self.err)
        self.assertEqual(self.harness.calls, [])

    def test_dry_run_renders_the_body_and_touches_nothing(self):
        rc = self.run_remediate(make_doc(), [derived_id()], ["--dry-run"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.harness.calls, [])
        self.assertIn("## Files", self.out)
        self.assertIn("WOULD OPEN: platform-agent/fix-", self.err)
        # Resolving the ledger number is a gh call, which a dry run may not
        # make — so it says why the link is missing instead of just omitting it.
        self.assertIn("the 'Part of #N' link is omitted", self.err)

    def test_dry_run_links_the_ledger_when_it_is_named(self):
        self.run_remediate(make_doc(), [derived_id()], ["--dry-run", "--issue", "42"])
        self.assertIn("Part of #42", self.out)
        self.assertEqual(self.harness.calls, [])

    def test_it_opens_the_pull_request_and_reports_it(self):
        self.touch("clusters/prod-us-east/payments-netpol.yaml")
        self.harness.replies = {
            "issue list": self.issue_list(),
            "pr create": "https://github.com/acme/fleet/pull/8\n",
        }
        rc = self.run_remediate(make_doc(), [derived_id()])
        self.assertEqual(rc, 0)
        self.assertEqual(
            self.stdout_json(),
            {
                "status": "REMEDIATED",
                "prs_opened": ["https://github.com/acme/fleet/pull/8"],
                "already_open": [],
                "refused": [],
            },
        )

    def test_one_unwritten_manifest_does_not_sink_the_whole_batch(self):
        # `/remediate all` expands to every id in the document. Answering a
        # request for several fixes with zero, because one manifest was never
        # written, is the least useful outcome available.
        doc = make_doc(
            findings=[
                make_finding(
                    fid="written",
                    remediation={
                        "kind": "manifest",
                        "path": "clusters/prod-us-east/written.yaml",
                        "note": "n",
                    },
                ),
                make_finding(
                    fid="unwritten",
                    remediation={
                        "kind": "manifest",
                        "path": "clusters/prod-us-east/unwritten.yaml",
                        "note": "n",
                    },
                ),
            ]
        )
        self.touch("clusters/prod-us-east/written.yaml")
        self.harness.replies = {
            "issue list": self.issue_list(),
            "pr create": "https://github.com/acme/fleet/pull/8\n",
        }
        unwritten = derived_id(fid="unwritten")
        rc = self.run_remediate(doc, [derived_id(fid="written"), unwritten])
        self.assertEqual(rc, 0)
        out = self.stdout_json()
        self.assertEqual(out["prs_opened"], ["https://github.com/acme/fleet/pull/8"])
        self.assertEqual(out["refused"], [unwritten])
        self.assertIn(f"REFUSED {unwritten}", self.err)
        # The refused finding must not reach a branch of its own.
        self.assertFalse(self.harness.matching("checkout", "unwritten"))

    def test_a_batch_with_nothing_left_to_do_is_still_an_error(self):
        # Partial success is worth reporting; total failure reported as exit 0
        # with an empty list would read as "done".
        doc = make_doc(
            findings=[
                make_finding(
                    fid="unwritten",
                    remediation={
                        "kind": "manifest",
                        "path": "clusters/prod-us-east/unwritten.yaml",
                        "note": "n",
                    },
                )
            ]
        )
        self.harness.replies = {"issue list": self.issue_list()}
        self.assertEqual(self.run_remediate(doc, [derived_id(fid="unwritten")]), 2)
        # "not a readable file inside", not "not on disk": a path that exists
        # but resolves outside the clone lands in exactly this refusal, and
        # telling that operator their file is missing sends them to look for a
        # file that is right there.
        self.assertIn("not a readable file inside", self.err)
        self.assertEqual(self.harness.gh_calls("pr", "create"), [])

    def test_an_uncapped_request_beats_the_auto_promotion_cap(self):
        findings = []
        for i in range(7):
            findings.append(
                make_finding(
                    fid=f"crit-{i}",
                    severity="minor",
                    remediation={
                        "kind": "manifest",
                        "path": f"clusters/prod-us-east/f{i}.yaml",
                        "note": "n",
                    },
                )
            )
            self.touch(f"clusters/prod-us-east/f{i}.yaml")
        self.harness.replies = {
            "issue list": self.issue_list(),
            "pr create": "https://github.com/acme/fleet/pull/8\n",
        }
        rc = self.run_remediate(
            make_doc(findings=findings), [derived_id(fid=f"crit-{i}") for i in range(7)]
        )
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.harness.gh_calls("pr", "create")), 7)

    def test_only_the_named_findings_become_pull_requests(self):
        # `remediate` is a person naming ids. The cron's auto-promotion sweep
        # used to ride along on it, so naming one id opened six pull requests —
        # and in the repository the five nobody asked for are indistinguishable
        # from the one they did.
        findings = []
        for i in range(6):
            findings.append(
                make_finding(
                    fid=f"crit-{i}",
                    title=f"Crit {i}",
                    remediation={
                        "kind": "manifest",
                        "path": f"clusters/prod-us-east/f{i}.yaml",
                        "note": "n",
                    },
                )
            )
            self.touch(f"clusters/prod-us-east/f{i}.yaml")
        self.harness.replies = {
            "issue list": self.issue_list(),
            "pr create": "https://github.com/acme/fleet/pull/8\n",
        }

        rc = self.run_remediate(make_doc(findings=findings), [derived_id(fid="crit-3")])

        self.assertEqual(rc, 0)
        self.assertEqual(len(self.harness.gh_calls("pr", "create")), 1)
        # Branch names key on the group's paths, not the id, so the staged file
        # is what proves which finding was acted on.
        staged = " ".join(" ".join(c) for c in self.git_add_calls(self.harness))
        self.assertIn("clusters/prod-us-east/f3.yaml", staged)
        for other in ("f0", "f1", "f2", "f4", "f5"):
            self.assertNotIn(
                f"clusters/prod-us-east/{other}.yaml",
                staged,
                f"{other} was never named and must not be staged",
            )

    def test_dry_run_previews_the_body_even_when_the_manifest_is_unwritten(self):
        # The warning is the point, not suppression: an operator drafting a
        # document before writing its manifests still needs to see what the
        # pull request would say.
        doc = make_doc(
            findings=[
                make_finding(
                    fid="unwritten",
                    remediation={
                        "kind": "manifest",
                        "path": "clusters/prod-us-east/unwritten.yaml",
                        "note": "n",
                    },
                )
            ]
        )
        unwritten = derived_id(fid="unwritten")
        self.assertEqual(self.run_remediate(doc, [unwritten], ["--dry-run"]), 0)
        self.assertIn(f"WOULD REFUSE {unwritten}", self.err)
        self.assertIn("## Files", self.out)
        # Warned about, never rewritten: a dry run that mutated the document it
        # is previewing would show a body the real run never produces.
        self.assertNotIn("did not write it", self.out)


# --------------------------------------------------------------------------- #
# Failure paths — reachable only now that Recorder can fail
# --------------------------------------------------------------------------- #


class TestFailurePaths(HarnessTestCase):
    def test_a_failed_issue_create_is_fatal(self):
        self.harness.replies = {"issue list": "[]"}
        self.harness.failures = {"issue create": 1}
        self.touch("clusters/prod-us-east/payments-netpol.yaml")

        rc = self.run_finish(make_doc())

        self.assertNotEqual(rc, 0)
        self.assertEqual(self.out, "")
        self.assertIn("subprocess failed with exit code 1", self.err)

    def test_a_failed_issue_edit_is_fatal(self):
        self.harness.replies = {"issue list": self.issue_list()}
        self.harness.failures = {"issue edit 42 -R acme/fleet --title": 1}
        self.touch("clusters/prod-us-east/payments-netpol.yaml")

        rc = self.run_finish(make_doc())

        self.assertNotEqual(rc, 0)
        # No delta comment on a ledger whose body was never rewritten.
        self.assertFalse(self.harness.gh_calls("issue", "comment"))

    def test_a_failed_delta_comment_is_survivable(self):
        # Losing the delta comment costs one notification; aborting would
        # leave the ledger correct but the run marked failed to the cron.
        previous_body = published_body(
            make_doc(findings=[make_finding(fid="a")]), generated_at=NOW
        )
        self.harness.replies = {
            "issue list": self.issue_list(),
            "--json body": json.dumps({"body": previous_body}),
        }
        self.harness.failures = {"issue comment": 1}
        self.touch("clusters/prod-us-east/payments-netpol.yaml")

        rc = self.run_finish(make_doc())

        self.assertEqual(rc, 0)
        self.assertIn("could not post the delta comment", self.err)
        self.assertEqual(self.stdout_json()["status"], "UPDATED")

    def test_a_failed_severity_label_is_survivable(self):
        self.harness.replies = {
            "issue list": "[]",
            "issue create": "https://github.com/acme/fleet/issues/7\n",
        }
        # `--add-label` and not the bare `severity:critical`: the remediation
        # `gh pr create` carries the same severity as a plain `--label`, and a
        # substring injection would fire on that instead.
        self.harness.failures = {"--add-label severity:critical": 1}
        self.touch("clusters/prod-us-east/payments-netpol.yaml")

        self.assertEqual(self.run_finish(make_doc()), 0)
        self.assertEqual(self.stdout_json()["status"], "OPENED")

    def test_recorder_raises_on_check_true_and_returns_on_check_false(self):
        # The fault-injection seam itself, so a silently-broken Recorder cannot
        # make every failure test above vacuously pass.
        recorder = Recorder(failures={"gh issue list": 1})
        with self.assertRaises(CalledProcessError):
            recorder(["gh", "issue", "list"])
        result = recorder(["gh", "issue", "list"], check=False)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(recorder(["git", "status"]).returncode, 0)


# --------------------------------------------------------------------------- #
# Credential redaction — the backstop the five SOPs promise
# --------------------------------------------------------------------------- #


class TestRedaction(unittest.TestCase):
    def assertRedacted(self, text, secret):
        out = audit_report.redact_secrets(text)
        self.assertNotIn(secret, out)
        self.assertIn(audit_report.REDACTED, out)
        return out

    def test_a_named_credential_field_is_blanked(self):
        for line in (
            "password: hunter2correcthorse",
            'token: "ghs_liveLiveLiveLiveLive"',
            "  api_key = AKIAIOSFODNN7EXAMPLE",
            "client-key-data: LS0tLS1CRUdJTiBSU0E=",
            # Deliberately not a `user:pass` base64 payload — decodes to
            # "not-a-real-credential" — so secret scanners do not flag the
            # fixture. The redactor keys off the field name and `Basic` prefix,
            # never the payload's contents.
            "- authorization: Basic bm90LWEtcmVhbC1jcmVkZW50aWFs",
        ):
            with self.subTest(line=line):
                out = audit_report.redact_secrets(line)
                self.assertIn(audit_report.REDACTED, out)

    def test_the_field_name_survives_so_the_reader_knows_what_was_hidden(self):
        out = audit_report.redact_secrets("password: hunter2correcthorse")
        self.assertTrue(out.startswith("password: "))

    def test_a_secret_payload_block_is_blanked_whatever_the_keys_are_called(self):
        out = self.assertRedacted(
            "apiVersion: v1\nkind: Secret\ndata:\n  benign-name: c3VwZXJzZWNyZXQ=\n"
            "  another: b3RoZXI=\n",
            "c3VwZXJzZWNyZXQ=",
        )
        self.assertNotIn("b3RoZXI=", out)
        # Structure survives: a reader can still see the shape of the object.
        self.assertIn("kind: Secret", out)
        self.assertIn("benign-name:", out)

    def test_the_secret_block_ends_when_the_indent_does(self):
        out = audit_report.redact_secrets(
            "data:\n  key: c2VjcmV0\nmetadata:\n  name: payments-db\n"
        )
        self.assertIn("name: payments-db", out)

    def test_a_private_key_body_goes_but_the_header_stays(self):
        out = self.assertRedacted(
            "-----BEGIN RSA PRIVATE KEY-----\nMIIEow...\n-----END RSA PRIVATE KEY-----",
            "MIIEow",
        )
        self.assertIn("-----BEGIN RSA PRIVATE KEY-----", out)
        self.assertIn("-----END RSA PRIVATE KEY-----", out)

    def test_self_identifying_tokens_go_wherever_they_appear(self):
        for secret in (
            "ghp_0123456789abcdefghij",
            "github_pat_11ABCDEFG0123456789abcdef",
            "ya29.a0ARrdaM9abcdefghijklmnop",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r",
        ):
            with self.subTest(secret=secret):
                self.assertRedacted(f"log line before {secret} and after", secret)

    def test_a_bearer_header_is_redacted(self):
        self.assertRedacted(
            "Authorization: Bearer abcdefghijklmnopqrstuv", "abcdefghijklmnopqrstuv"
        )

    def test_ordinary_audit_evidence_is_left_intact(self):
        # Over-redaction destroys the artifact. Bare base64 and long opaque ids
        # are normal in audit output and must survive.
        for benign in (
            "No resources found in payments namespace.",
            "nodeVersion: 1.29.4-gke.1043004",
            "image: gcr.io/acme/api@sha256:3f5b1c2d4e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b",
            "sizeGb: 500",
            "c3VwZXJzZWNyZXQK",
        ):
            with self.subTest(benign=benign):
                self.assertEqual(audit_report.redact_secrets(benign), benign)

    def test_a_jsonpath_naming_a_secret_key_is_not_a_secret(self):
        # There is no value after the colon, so the command survives verbatim —
        # the SOPs require pasting the reproducing command exactly.
        command = "kubectl get secret db -o jsonpath='{.data.token}'"
        self.assertEqual(audit_report.redact_secrets(command), command)

    def test_the_renderer_redacts_evidence_it_is_handed(self):
        doc = make_doc(
            findings=[make_finding(excerpt="  token: ghs_abcdefghijklmnopqrstu\n")]
        )
        body = render_body(doc, generated_at=NOW)
        self.assertNotIn("ghs_abcdefghijklmnopqrstu", body)
        self.assertIn(audit_report.REDACTED, body)

    def test_redaction_survives_the_none_and_empty_cases(self):
        self.assertEqual(audit_report.redact_secrets(None), "")
        self.assertEqual(audit_report.redact_secrets(""), "")


class TestClipText(unittest.TestCase):
    def test_a_short_field_is_returned_unchanged(self):
        self.assertEqual(audit_report.clip_text("  hello  ", 40), "hello")

    def test_an_oversized_field_is_clipped_and_says_so(self):
        out = audit_report.clip_text("x" * 500, 40)
        self.assertTrue(out.endswith("…(truncated)"))
        self.assertLess(len(out), 500)

    def test_clipping_redacts_first_so_a_secret_cannot_hide_past_the_limit(self):
        out = audit_report.clip_text("password: hunter2correcthorse", 400)
        self.assertNotIn("hunter2correcthorse", out)

    def test_every_free_text_field_is_capped(self):
        # A single oversized field used to be able to push the body past
        # GitHub's limit on its own, and since at least one finding always
        # renders, that published nothing at all.
        huge = "y" * 40_000
        doc = make_doc(
            findings=[
                make_finding(
                    title=huge,
                    impact=huge,
                    recommendation={"action": huge, "rationale": huge, "risk": huge},
                    remediation={"kind": "manual", "note": huge},
                )
            ]
        )
        body = render_body(doc, generated_at=NOW)
        self.assertLessEqual(len(body), GITHUB_BODY_LIMIT)

    def test_the_identifiers_are_capped_too(self):
        # cluster/namespace/object were the last fields interpolated raw, and
        # the selection loop renders the first finding whatever it costs — so
        # one of these overflowed the body and published *nothing*, every run,
        # for as long as the finding reproduced.
        huge = "z" * 40_000
        doc = make_doc(
            findings=[make_finding(cluster=huge, namespace=huge, obj=huge)]
        )
        body = render_body(doc, generated_at=NOW)
        self.assertLessEqual(len(body), GITHUB_BODY_LIMIT)
        self.assertIn("…(truncated)", body)

    def test_an_identifier_cannot_break_out_of_its_code_span(self):
        # A backtick closes the span and a newline ends it; what follows is
        # rendered as Markdown in the reader's browser.
        doc = make_doc(
            findings=[
                make_finding(obj="Pod/x` <script>alert(1)</script> `y", cluster="a\nb")
            ]
        )
        body = render_body(doc, generated_at=NOW)
        self.assertNotIn("Pod/x`", body)
        self.assertIn("Pod/x' <script>", body)
        self.assertIn("`a b`", body)


class TestNewlineNormalisation(unittest.TestCase):
    def test_a_browser_authored_command_still_fires(self):
        # GitHub's web comment box submits CRLF. Every marker pattern here ends
        # in `[ \t]*$`, and \r is neither — so without folding, a /remediate
        # typed in a browser was silently ignored.
        findings = [manifest_finding("netpol", "a.yaml")]
        parsed = audit_report.parse_remediate_commands(
            [
                {
                    "id": "IC_1",
                    "body": "please fix\r\n/remediate netpol\r\n",
                    "author": {"login": "dev"},
                    "authorAssociation": "MEMBER",
                }
            ],
            findings,
        )
        self.assertEqual(parsed.targets, ["netpol"])

    def test_a_crlf_body_still_yields_its_delta_block(self):
        body = audit_report.delta_block(["a", "b"]).replace("\n", "\r\n")
        self.assertEqual(audit_report.parse_delta_block(body), ["a", "b"])

    def test_a_crlf_marker_is_still_found(self):
        body = f"reply\r\n{audit_report.refused_marker('IC_9')}\r\n"
        self.assertTrue(
            audit_report.has_marker(body, audit_report.REFUSED_MARKER_RE, "IC_9")
        )


class TestFenceScanning(unittest.TestCase):
    def strip(self, text):
        return audit_report.strip_fenced_blocks(text)

    def test_a_command_inside_a_fence_is_removed(self):
        self.assertNotIn("/remediate", self.strip("a\n```\n/remediate x\n```\nb"))

    def test_text_between_two_fenced_blocks_survives(self):
        # The old non-greedy regex paired fence 1 with fence 2 and fence 3 with
        # fence 4, so a command sitting between blocks two and three was
        # swallowed — or, with an odd fence count, a real command inside a
        # block leaked through.
        out = self.strip("```\nin one\n```\n/remediate real\n```\nin two\n```")
        self.assertIn("/remediate real", out)
        self.assertNotIn("in one", out)
        self.assertNotIn("in two", out)

    def test_an_unterminated_fence_swallows_the_rest(self):
        self.assertNotIn("/remediate x", self.strip("```\n/remediate x\n"))

    def test_a_tilde_fence_is_a_fence(self):
        self.assertNotIn("/remediate x", self.strip("~~~\n/remediate x\n~~~"))

    def test_a_shorter_run_inside_a_longer_fence_does_not_close_it(self):
        out = self.strip("````\n```\n/remediate x\n```\n````\n/remediate real")
        self.assertNotIn("/remediate x", out)
        self.assertIn("/remediate real", out)

    def test_a_backtick_fence_is_not_closed_by_tildes(self):
        self.assertNotIn("/remediate x", self.strip("```\n~~~\n/remediate x\n```"))

    def test_a_four_space_indented_run_does_not_close_a_block(self):
        # CommonMark and GitHub both render this as literal text *inside* the
        # block. Reading it as a closer ends the block early and exposes the
        # command the author quoted to talk about.
        out = self.strip("```\n    ```\n/remediate x\n```")
        self.assertNotIn("/remediate x", out)

    def test_a_four_space_indented_run_does_not_open_a_block(self):
        # The mirror image: treating it as an opener swallows every real
        # command after it, so the channel silently stops working.
        out = self.strip("    ```\n/remediate real")
        self.assertIn("/remediate real", out)

    def test_three_spaces_of_indent_is_still_a_fence(self):
        self.assertNotIn("/remediate x", self.strip("   ```\n/remediate x\n   ```"))

    def test_a_tab_indented_run_is_not_a_fence(self):
        # A tab advances to the next four-column stop, so it is indented code.
        self.assertIn("/remediate real", self.strip("\t```\n/remediate real"))

    def test_a_closer_may_carry_trailing_whitespace(self):
        self.assertIn("/remediate real", self.strip("```\nx\n``` \n/remediate real"))


class TestPathContainment(unittest.TestCase):
    def test_a_normalised_path_is_returned_not_just_accepted(self):
        # Grouping, the branch digest, the `git add` pathspec and the existence
        # check all have to see one spelling, or `a/b.yaml` and `./a/b.yaml`
        # become two groups and two pull requests that conflict.
        self.assertEqual(
            audit_report._require_repo_relative("./clusters//prod/x.yaml", "where"),
            "clusters/prod/x.yaml",
        )

    def test_two_spellings_of_one_path_group_together(self):
        findings = [
            manifest_finding("a", "./clusters/prod/x.yaml"),
            manifest_finding("b", "clusters/prod//x.yaml"),
        ]
        audit_report.validate_findings(make_doc(findings=findings), AUDIT)
        groups = audit_report.remediation_groups(findings)
        self.assertEqual(len(groups), 1)

    def test_the_refusals_hold(self):
        for path in (
            "/etc/passwd",
            "../outside.yaml",
            "clusters/../../outside.yaml",
            ".git/config",
            "clusters/*.yaml",
            "clusters/x?.yaml",
            "clusters/[ab].yaml",
            ":(glob)clusters/x.yaml",
            "clusters\\prod\\x.yaml",
            "clusters/prod/",
            "",
            ".",
            "..",
            # `.git` on any part, in any case. `sub/.git/config` rewrites where
            # a submodule points; `.GIT` is the same file on the
            # case-insensitive filesystems this is checked out on.
            ".GIT/config",
            ".Git/config",
            "sub/.git/config",
            "sub/.GIT/hooks/pre-commit",
        ):
            with self.subTest(path=path):
                with self.assertRaises(audit_report.ValidationError):
                    audit_report._require_repo_relative(path, "where")


class TestFilesystemContainment(unittest.TestCase):
    """The string check is not containment; this is.

    Every path here passes `_require_repo_relative` — no `..`, relative, no
    glob — and still reads or writes outside the repository on a real
    filesystem. The exploit is executed rather than argued.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "repo"
        (self.root / "manifests").mkdir(parents=True)
        self.outside = Path(self.tmp.name) / "outside"
        self.outside.mkdir()
        (self.outside / "secret.yaml").write_text("token: hunter2\n")

    def link(self, name="vendor", target=None):
        (self.root / "manifests" / name).symlink_to(
            target or self.outside, target_is_directory=True
        )

    def test_the_string_check_alone_lets_the_exploit_through(self):
        # Not a hypothetical: this asserts the gap the filesystem check exists
        # to close. `_require_repo_relative` accepts it, and the naive
        # `(root / path)` an earlier version used reads the file outside.
        self.link()
        path = "manifests/vendor/secret.yaml"
        self.assertEqual(audit_report._require_repo_relative(path, "where"), path)
        self.assertEqual(
            (self.root / path).read_text(), "token: hunter2\n"
        )

    def test_a_symlinked_directory_component_is_refused(self):
        self.link()
        with self.assertRaises(audit_report.ContainmentError) as caught:
            audit_report.resolve_inside_repo(
                self.root, "manifests/vendor/secret.yaml", "where"
            )
        self.assertIn("symbolic link", str(caught.exception))

    def test_a_symlinked_file_is_refused(self):
        (self.root / "manifests" / "x.yaml").symlink_to(self.outside / "secret.yaml")
        with self.assertRaises(audit_report.ContainmentError):
            audit_report.resolve_inside_repo(self.root, "manifests/x.yaml", "where")

    def test_a_link_that_resolves_back_inside_is_still_refused(self):
        # It is contained today and stops being contained the moment somebody
        # retargets the link. Writing *through* a link is never intended here.
        (self.root / "real").mkdir()
        self.link(target=self.root / "real")
        with self.assertRaises(audit_report.ContainmentError):
            audit_report.resolve_inside_repo(
                self.root, "manifests/vendor/x.yaml", "where"
            )

    def test_a_real_path_resolves_and_is_absolute(self):
        (self.root / "manifests" / "x.yaml").write_text("kind: Namespace\n")
        resolved = audit_report.resolve_inside_repo(
            self.root, "./manifests//x.yaml", "where"
        )
        self.assertTrue(resolved.is_absolute())
        self.assertEqual(resolved.read_text(), "kind: Namespace\n")

    def test_a_path_that_does_not_exist_yet_is_allowed(self):
        # The remediation write creates it after a fresh checkout.
        resolved = audit_report.resolve_inside_repo(
            self.root, "manifests/new/x.yaml", "where"
        )
        self.assertEqual(resolved, self.root.resolve() / "manifests/new/x.yaml")

    def test_the_snapshot_refuses_to_read_through_a_link(self):
        self.link()
        with self.assertRaises(audit_report.ContainmentError):
            audit_report.snapshot_paths(self.root, ["manifests/vendor/secret.yaml"])

    def test_an_escaping_remediation_degrades_instead_of_publishing(self):
        self.link()
        findings = [manifest_finding("leak", "manifests/vendor/secret.yaml")]
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            degraded = audit_report.degrade_missing_remediations(findings, self.root)
        self.assertEqual(degraded, ["leak"])
        self.assertEqual(findings[0]["remediation"]["kind"], "manual")
        self.assertEqual(findings[0]["remediation"]["path"], "")
        self.assertIn("does not resolve to a real file", findings[0]["remediation"]["note"])
        self.assertIn("SECURITY", err.getvalue())


# --------------------------------------------------------------------------- #
# Partial coverage — what a run may not conclude when it could not look
# --------------------------------------------------------------------------- #


class TestCoverageGaps(unittest.TestCase):
    def test_a_skipped_cluster_is_a_gap(self):
        gaps = audit_report.coverage_gaps(
            make_doc(skipped=[{"cluster": "dr-west", "reason": "unreachable"}])
        )
        self.assertEqual(len(gaps), 1)
        self.assertIn("dr-west", gaps[0])
        self.assertIn("not audited", gaps[0])

    def test_a_limitation_on_a_read_cluster_is_also_a_gap(self):
        gaps = audit_report.coverage_gaps(
            make_doc(
                clusters=[
                    {
                        "name": "prod-us-east",
                        "location": "us-east1",
                        "project": "acme-prod",
                        "limitations": "Autopilot: node-level checks did not run",
                    }
                ]
            )
        )
        self.assertEqual(len(gaps), 1)
        self.assertIn("partially audited", gaps[0])

    def test_a_complete_run_has_no_gaps(self):
        self.assertEqual(audit_report.coverage_gaps(make_doc()), [])

    def test_an_unrun_check_is_a_gap_even_with_no_limitations(self):
        """The gap prose cannot catch a run that never admits to one."""
        gaps = audit_report.coverage_gaps(
            make_doc(
                clusters=[
                    {
                        "name": "prod-us-east",
                        "location": "us-east1",
                        "project": "acme-prod",
                        "checks_run": ["privileged-container", "host-namespace"],
                    }
                ]
            )
        )
        self.assertEqual(len(gaps), 1)
        self.assertIn("prod-us-east", gaps[0])
        self.assertIn("9 of 11 applicable checks did not run", gaps[0])
        self.assertIn("netpol-missing", gaps[0])

    def test_a_cluster_reports_one_gap_line_not_two(self):
        """A cluster with both an unrun check and a limitation is one cluster."""
        gaps = audit_report.coverage_gaps(
            make_doc(
                clusters=[
                    {
                        "name": "prod-autopilot",
                        "location": "us-central1",
                        "project": "acme-prod",
                        "limitations": "Autopilot: 2.1-2.3 are admission-enforced.",
                        "checks_run": [
                            check
                            for check in audit_report.audit_checks(AUDIT)
                            if check != "privileged-container"
                        ],
                    }
                ]
            )
        )
        self.assertEqual(len(gaps), 1)
        self.assertIn("1 of 11 applicable checks did not run", gaps[0])
        self.assertIn("Autopilot", gaps[0])


class TestChecksRun(unittest.TestCase):
    """The field that tells an audit that ran from one that merely finished.

    Every test here is a regression on one incident: five audit streams
    reported a clean fleet after four of them ran zero inspection commands. The
    documents they published were valid — populated scope, empty findings — and
    the harness had no way to know the difference.
    """

    def _cluster(self, **extra):
        base = {
            "name": "prod-us-east",
            "location": "us-east1",
            "project": "acme-prod",
        }
        base.update(extra)
        return base

    def _omitting_checks_run(self, **doc_kwargs):
        """A document from before this field existed — the field simply absent.

        `make_doc` back-fills a full roster so unrelated tests are not coverage
        tests, so the omission has to be made deliberately here.
        """
        doc = make_doc(clusters=[self._cluster()], **doc_kwargs)
        del doc["scope"]["clusters"][0]["checks_run"]
        return doc

    def test_an_audit_that_ran_nothing_is_rejected(self):
        """The t_751ffb70 document: clusters enumerated, no checks, no findings."""
        doc = self._omitting_checks_run(findings=[])
        with self.assertRaises(audit_report.ValidationError) as exc:
            audit_report.validate_findings(doc, AUDIT)
        message = str(exc.exception)
        self.assertIn("scope.clusters[0].checks_run", message)
        self.assertIn("audit that did not run", message)

    def test_an_unexplained_empty_checks_run_is_rejected(self):
        doc = make_doc(clusters=[self._cluster(checks_run=[])])
        with self.assertRaises(audit_report.ValidationError) as exc:
            audit_report.validate_findings(doc, AUDIT)
        self.assertIn("scope.clusters[0].checks_run", str(exc.exception))

    def test_an_explained_empty_checks_run_is_allowed_but_partial(self):
        """Drift's "read it, compared nothing" state — honest, and never clean.

        The refusal is aimed at the silent zero. A zero that says why is still a
        coverage gap, so the ledger cannot close on it either way.
        """
        doc = make_doc(
            findings=[],
            clusters=[
                self._cluster(
                    checks_run=[],
                    limitations="cohort below the size floor; no facet compared.",
                )
            ],
        )
        self.assertEqual(audit_report.validate_findings(doc, AUDIT), doc)
        self.assertTrue(audit_report.coverage_gaps(doc))

    def test_checks_run_of_the_wrong_type_is_rejected(self):
        doc = make_doc(clusters=[self._cluster(checks_run="privileged-container")])
        with self.assertRaises(audit_report.ValidationError) as exc:
            audit_report.validate_findings(doc, AUDIT)
        self.assertIn("must be a list", str(exc.exception))

    def test_no_rejection_ever_prints_the_roster(self):
        """A rejection that lists the valid slugs is an answer key.

        This test asserts the opposite of what it used to. Naming the roster
        looked like the helpful thing to do, and it inverted the guard: a run
        that inspected nothing could submit guesses, read the real slugs off
        the `exit 2`, and resubmit the same empty document with the right words
        in it. On 2026-08-03 four of the five streams did exactly that — one of
        them without re-reading its SOP in between, which is how we know where
        the slugs came from — and published a fleet-wide all-clear.

        So: every way of getting `checks_run` rejected, and none of the
        messages may contain a slug. The pointer to the SOP is what replaces
        it, and `start` hands the roster over before any work begins.
        """
        roster = list(audit_report.audit_checks(AUDIT))
        rejected = [
            self._omitting_checks_run(),
            make_doc(clusters=[self._cluster(checks_run=[])]),
            make_doc(clusters=[self._cluster(checks_run="privileged-container")]),
            make_doc(clusters=[self._cluster(checks_run=["not-a-real-check"])]),
        ]
        # A bare slug list — the pre-2026-08-03 wire format — is its own
        # rejection path and must stay just as tight-lipped.
        bare = make_doc()
        bare["scope"]["clusters"][0]["checks_run"] = list(roster)
        rejected.append(bare)

        for i, doc in enumerate(rejected):
            with self.subTest(case=i):
                with self.assertRaises(audit_report.ValidationError) as exc:
                    audit_report.validate_findings(doc, AUDIT)
                message = str(exc.exception)
                leaked = [check for check in roster if check in message]
                self.assertEqual(
                    [],
                    leaked,
                    f"rejection leaked the roster: {leaked}",
                )
                self.assertIn(audit_report.audit_sop(AUDIT), message)

    def test_a_check_outside_the_roster_is_rejected(self):
        doc = make_doc(
            clusters=[self._cluster(checks_run=["privileged-container", "2.4"])]
        )
        with self.assertRaises(audit_report.ValidationError) as exc:
            audit_report.validate_findings(doc, AUDIT)
        self.assertIn("scope.clusters[0].checks_run[1]", str(exc.exception))
        self.assertIn("'2.4'", str(exc.exception))

    def test_a_duplicate_check_is_rejected(self):
        doc = make_doc(
            clusters=[
                self._cluster(
                    checks_run=["privileged-container", "privileged-container"]
                )
            ]
        )
        with self.assertRaises(audit_report.ValidationError) as exc:
            audit_report.validate_findings(doc, AUDIT)
        self.assertIn("duplicate check", str(exc.exception))

    def test_a_full_roster_validates(self):
        doc = make_doc(
            clusters=[self._cluster(checks_run=list(audit_report.audit_checks(AUDIT)))]
        )
        self.assertEqual(audit_report.validate_findings(doc, AUDIT), doc)

    def test_a_partial_roster_validates_but_is_not_complete_coverage(self):
        """A half-run audit publishes — it just may not call the fleet clean."""
        doc = make_doc(
            findings=[],
            clusters=[self._cluster(checks_run=["privileged-container"])],
        )
        self.assertEqual(audit_report.validate_findings(doc, AUDIT), doc)
        self.assertTrue(audit_report.coverage_gaps(doc))

    def test_the_scope_table_records_how_much_of_the_roster_ran(self):
        body = render_body(
            make_doc(findings=[], clusters=[self._cluster()]), generated_at=NOW
        )
        self.assertIn("| Checks |", body)
        self.assertIn("11/11", body)
        self.assertNotIn("⚠", body)

    def test_an_incomplete_cluster_is_flagged_in_the_scope_table(self):
        body = render_body(
            make_doc(
                findings=[],
                clusters=[self._cluster(checks_run=["privileged-container"])],
            ),
            generated_at=NOW,
        )
        self.assertIn("1/11 ⚠", body)

    def test_every_stream_requires_its_own_roster(self):
        """A compliance check named by the cost audit is still a typo."""
        for audit_id in audit_report.AUDITS:
            with self.subTest(audit=audit_id):
                doc = make_doc(
                    audit=audit_id,
                    clusters=[self._cluster(checks_run=["privileged-container"])],
                )
                if audit_id == AUDIT:
                    continue
                with self.assertRaises(audit_report.ValidationError):
                    audit_report.validate_findings(doc, audit_id)


class TestCheckCommands(unittest.TestCase):
    """Every claimed check carries the command that ran it.

    A bare slug list was free to write. The roster is a fixed, guessable set of
    ten or so words, so a run that inspected nothing could type all of them in
    one line and publish an all-clear — which is what happened on 2026-08-03.
    Requiring the command does not *prove* the check ran: this harness is a
    subprocess of the agent and cannot see its tool calls. It buys three other
    things, and the tests below are about those three:

    1. Fabrication gets expensive — a distinct plausible invocation per check
       per cluster, not ten words.
    2. Fabrication gets falsifiable — the commands are published, so a reader
       or the next run can re-run them.
    3. The trivially-cheap path is gone — the document that published five
       clean audits contained no command anywhere.
    """

    def _with(self, entry):
        doc = make_doc(findings=[])
        doc["scope"]["clusters"][0]["checks_run"] = [entry]
        doc["scope"]["clusters"][1]["checks_run"] = [ran("netpol-missing", "stage-eu")]
        return doc

    def _reject(self, entry, fragment):
        with self.assertRaises(audit_report.ValidationError) as exc:
            audit_report.validate_findings(self._with(entry), AUDIT)
        self.assertIn(fragment, str(exc.exception))
        return str(exc.exception)

    def test_a_bare_slug_is_no_longer_a_checks_run_entry(self):
        self._reject("netpol-missing", "expected an object")

    def test_an_entry_without_a_command_is_rejected(self):
        self._reject({"check": "netpol-missing"}, "checks_run[0].command")

    def test_an_empty_command_is_rejected(self):
        self._reject({"check": "netpol-missing", "command": "   "}, "command")

    def test_a_command_that_inspects_nothing_is_rejected(self):
        """`echo`, `cat` and friends cannot read a cluster, so they cannot be how a check ran."""
        for command in (
            "echo checked netpol-missing on prod-us-east",
            "cat /tmp/notes-about-the-netpol-check.txt",
            "printf 'ran the check\\n'",
            "python3 -c \"print('netpol-missing: ok')\"",
            "true  # netpol-missing passed",
        ):
            with self.subTest(command=command):
                self._reject(
                    {"check": "netpol-missing", "command": command}, "cannot inspect"
                )

    def test_a_command_naming_no_inspection_binary_is_rejected(self):
        """The catch-all: prose, or a tool that reads nothing on a cluster."""
        self._reject(
            {
                "check": "netpol-missing",
                "command": "reviewed the NetworkPolicy inventory for every namespace",
            },
            "names none of",
        )

    def test_calling_this_harness_is_not_inspecting_the_fleet(self):
        """`checks_run` records how the fleet was read, not how it was reported."""
        self._reject(
            {
                "check": "netpol-missing",
                "command": "./skills/fleet-audit/scripts/audit_report.py start --audit compliance-audit",
            },
            "call to this harness",
        )

    def test_a_command_too_short_to_be_one_is_rejected(self):
        self._reject({"check": "netpol-missing", "command": "kubectl"}, "too short")

    def test_an_oversized_command_is_rejected(self):
        oversized = "kubectl get networkpolicy -A " + ("x" * audit_report.MAX_COMMAND_CHARS)
        self._reject(
            {"check": "netpol-missing", "command": oversized},
            f"exceeds {audit_report.MAX_COMMAND_CHARS}",
        )

    def test_a_real_invocation_is_accepted(self):
        doc = self._with(
            {
                "check": "netpol-missing",
                "command": (
                    "kubectl --context prod-us-east get networkpolicy -A "
                    "-o custom-columns=NS:.metadata.namespace --no-headers"
                ),
            }
        )
        self.assertEqual(audit_report.validate_findings(doc, AUDIT), doc)

    def test_one_command_may_back_several_checks(self):
        """A single `describe` is honestly how the drift audit reads most facets.

        Duplicate *checks* are rejected; duplicate commands are not, because
        rejecting them would force the consistency audit to invent nine
        distinct invocations for nine fields it read from one JSON blob — which
        is exactly the fabrication this field exists to discourage.
        """
        shared = (
            "gcloud container clusters describe prod-usc1 --location us-central1 "
            "--project acme-prod --format=json"
        )
        doc = make_doc(
            audit="fleet-consistency-drift",
            findings=[],
            clusters=[
                {
                    "name": "prod-usc1",
                    "location": "us-central1",
                    "project": "acme-prod",
                    "checks_run": [
                        {"check": "shielded-nodes", "command": shared},
                        {"check": "secure-boot", "command": shared},
                        {"check": "private-nodes", "command": shared},
                    ],
                }
            ],
        )
        self.assertEqual(
            audit_report.validate_findings(doc, "fleet-consistency-drift"), doc
        )

    def test_checks_ran_reads_the_slugs_back_out(self):
        cluster = {
            "checks_run": [
                ran("netpol-missing"),
                ran("wildcard-rbac"),
                {"check": "", "command": "kubectl get ns"},
                "netpol-missing",
            ]
        }
        self.assertEqual(
            ["netpol-missing", "wildcard-rbac"], audit_report.checks_ran(cluster)
        )
        self.assertEqual([], audit_report.checks_ran(None))
        self.assertEqual([], audit_report.checks_ran({}))

    def test_the_commands_are_published_in_the_ledger(self):
        """Falsifiability is the whole mechanism — an unpublished command proves nothing."""
        command = (
            "kubectl --context prod-us-east get networkpolicy -A "
            "-o custom-columns=NS:.metadata.namespace --no-headers"
        )
        doc = self._with({"check": "netpol-missing", "command": command})
        body = render_body(doc, generated_at=NOW)
        self.assertIn("How this run checked the fleet", body)
        self.assertIn(command, body)
        self.assertIn("netpol-missing", body)
        self.assertIn("prod-us-east", body)

    def test_the_evidence_table_is_dropped_whole_or_not_at_all(self):
        """Half a table reads as a short one, and "we ran three checks" is a worse lie than silence."""
        findings = [
            make_finding(
                fid=f"finding-{i}",
                title=f"Finding {i} " + "padding " * 20,
                impact="x" * 1400,
            )
            for i in range(60)
        ]
        doc = self._with({"check": "netpol-missing", "command": "kubectl get netpol -A"})
        doc["findings"] = findings
        body = render_body(doc, generated_at=NOW)
        self.assertLessEqual(len(body), audit_report.MAX_BODY_CHARS)
        if "How this run checked the fleet" in body:
            self.assertIn("kubectl get netpol -A", body)


class TestPartialCoverageGating(HarnessTestCase):
    """A run that could not look must not conclude anything from absence."""

    PARTIAL = [{"cluster": "dr-west", "reason": "control plane unreachable"}]

    def test_a_clean_but_partial_run_leaves_the_ledger_open(self):
        self.harness.replies = {"issue list": self.issue_list()}
        self.assertEqual(
            self.run_finish(make_doc(findings=[], skipped=self.PARTIAL)), 0
        )
        out = self.stdout_json()
        self.assertEqual(out["status"], "CLEAN")
        self.assertTrue(out["partial"])
        self.assertEqual(len(out["coverage_gaps"]), 1)
        # The all-clear is still said, but the ledger is not retired.
        self.assertTrue(self.harness.gh_calls("issue", "comment", "42"))
        self.assertEqual(self.harness.gh_calls("issue", "close"), [])

    def test_a_clean_but_partial_run_reports_nothing_as_resolved(self):
        self.harness.replies = {"issue list": self.issue_list()}
        self.run_finish(make_doc(findings=[], skipped=self.PARTIAL))
        self.assertEqual(self.stdout_json()["resolved"], 0)

    def test_a_clean_and_complete_run_still_closes_the_ledger(self):
        self.harness.replies = {"issue list": self.issue_list()}
        self.run_finish(make_doc(findings=[]))
        self.assertTrue(self.harness.matching("issue", "close", "42"))
        self.assertFalse(self.stdout_json()["partial"])

    def test_a_partial_run_closes_no_remediation_pull_request(self):
        self.touch("clusters/prod-us-east/payments-netpol.yaml")
        self.harness.replies = {
            "issue list": self.issue_list(),
            "pr list": json.dumps(
                [pr(8, "platform-agent/fix-x-gone", body=audit_report.delta_block(["gone"]))]
            ),
        }
        self.run_finish(make_doc(skipped=self.PARTIAL))
        self.assertEqual(self.harness.gh_calls("pr", "close"), [])
        self.assertIn("no remediation pull request was closed", self.err)

    def test_a_gapped_clean_run_opens_a_ledger_when_the_stream_has_none(self):
        """The quietest failure the harness had: nothing found, nothing looked at, nothing said.

        Zero findings and no open ledger used to mean "nothing to do" — no
        issue, no comment, no artifact of any kind. A stream could report a
        clean fleet every morning for weeks while never having looked at it.
        Four streams did exactly that on 2026-08-03; the only reason it was
        caught is that a fifth happened to have a ledger open from the day
        before. An audit that cannot speak for the fleet has something to say,
        and it must land somewhere durable.
        """
        self.harness.replies = {
            "issue list": "[]",
            "issue create": "https://github.com/acme/fleet/issues/77\n",
        }
        self.assertEqual(
            self.run_finish(make_doc(findings=[], skipped=self.PARTIAL)), 0
        )
        created = self.harness.gh_calls("issue", "create")
        self.assertEqual(1, len(created))
        argv = created[0]
        self.assertIn("coverage incomplete", " ".join(argv))
        self.assertIn("agent:audit", argv)
        self.assertIn(f"audit:{AUDIT}", argv)

        out = self.stdout_json()
        self.assertEqual("CLEAN", out["status"])
        self.assertTrue(out["partial"])
        self.assertEqual("https://github.com/acme/fleet/issues/77", out["issue_url"])

    def test_a_truly_clean_run_with_no_ledger_still_opens_nothing(self):
        """Complete coverage, nothing found, no ledger: there is genuinely nothing to say."""
        self.harness.replies = {"issue list": "[]"}
        self.assertEqual(self.run_finish(make_doc(findings=[])), 0)
        self.assertEqual([], self.harness.gh_calls("issue", "create"))
        self.assertFalse(self.stdout_json()["partial"])

    def test_the_coverage_ledger_is_not_titled_like_an_all_clear(self):
        """`0 findings (0 critical)` is the phrasing this issue exists to avoid."""
        title = audit_report.coverage_issue_title(AUDIT, ["dr-west: unreadable"])
        self.assertIn("coverage incomplete", title)
        self.assertIn("1 gap,", title)
        self.assertNotIn("0 findings (0 critical)", title)
        self.assertIn(
            "2 gaps", audit_report.coverage_issue_title(AUDIT, ["a", "b"])
        )

    def test_a_gapped_clean_body_does_not_call_the_fleet_compliant(self):
        body = render_body(make_doc(findings=[], skipped=self.PARTIAL), generated_at=NOW)
        self.assertNotIn("Every audited cluster is compliant", body)

    def test_a_partial_run_announces_nothing_as_resolved(self):
        self.touch("clusters/prod-us-east/payments-netpol.yaml")
        previous = published_body(
            make_doc(findings=[make_finding(fid="gone"), make_finding()]),
            generated_at=NOW,
        )
        self.harness.replies = {
            "issue list": self.issue_list(),
            "--json body": json.dumps({"body": previous}),
        }
        self.run_finish(make_doc(skipped=self.PARTIAL))
        self.assertEqual(self.stdout_json()["resolved"], 0)
        self.assertTrue(self.stdout_json()["partial"])

    def test_a_truncated_body_is_not_a_coverage_gap(self):
        # `partial` used to be `bool(gaps) or rendered.partial`, which made
        # `partial: true` with an empty `coverage_gaps` reachable — a flag the
        # SOPs tell the agent to explain, with nothing to explain it with.
        #
        # The two are different kinds of incomplete. A gap means the audit did
        # not look, which is why it suppresses the resolved count. Truncation
        # means it looked and could not print it all: the title counts are
        # still true and resolution accounting is untouched, so the run may
        # conclude everything a complete run may. It is surfaced in the body
        # and the log, not here.
        many = [make_finding(fid=f"f-{n:04d}", severity="minor") for n in range(400)]
        self.harness.replies = {"issue list": self.issue_list()}
        self.assertEqual(self.run_finish(make_doc(findings=many)), 0)
        out = self.stdout_json()
        self.assertFalse(out["partial"])
        self.assertEqual(out["coverage_gaps"], [])
        self.assertIn("do not fit", self.err.replace("did not fit", "do not fit"))

    def test_partial_is_true_exactly_when_there_are_coverage_gaps(self):
        # The documented invariant, asserted on both `finish` branches: five
        # SOPs and SKILL.md now say "if and only if", and a reader is entitled
        # to report from either field.
        cases = [
            ("clean, complete", make_doc(findings=[]), False),
            ("clean, gap", make_doc(findings=[], skipped=self.PARTIAL), True),
            ("findings, complete", make_doc(), False),
            ("findings, gap", make_doc(skipped=self.PARTIAL), True),
            (
                "findings, limitation only",
                make_doc(
                    clusters=[
                        {
                            "name": "prod-us-east",
                            "location": "us-east1",
                            "project": "acme",
                            "limitations": "Autopilot: node checks did not run",
                        }
                    ]
                ),
                True,
            ),
        ]
        for label, doc, expected in cases:
            with self.subTest(label):
                self.setUp()
                self.touch("clusters/prod-us-east/payments-netpol.yaml")
                self.harness.replies = {"issue list": self.issue_list()}
                self.run_finish(doc)
                out = self.stdout_json()
                self.assertEqual(out["partial"], expected)
                self.assertEqual(bool(out["coverage_gaps"]), out["partial"])


# --------------------------------------------------------------------------- #
# Close semantics — whose close is final
# --------------------------------------------------------------------------- #


class TestCloseSemantics(unittest.TestCase):
    def test_a_harness_close_is_recognised_by_its_label(self):
        self.assertTrue(
            audit_report.pr_closed_by_harness(
                {"state": "CLOSED", "labels": [{"name": audit_report.STALE_CLOSED_LABEL}]}
            )
        )

    def test_a_human_close_is_not(self):
        self.assertFalse(
            audit_report.pr_closed_by_harness({"state": "CLOSED", "labels": []})
        )

    def test_a_merged_pull_request_is_not_a_harness_close(self):
        self.assertFalse(
            audit_report.pr_closed_by_harness(
                {
                    "state": "MERGED",
                    "mergedAt": "2026-07-01T00:00:00Z",
                    "labels": [{"name": audit_report.STALE_CLOSED_LABEL}],
                }
            )
        )

    def test_a_finding_the_harness_withdrew_can_be_re_promoted(self):
        findings = [manifest_finding("crit", "a.yaml", severity="critical")]
        closed_by_us = {
            "state": "CLOSED",
            "labels": [{"name": audit_report.STALE_CLOSED_LABEL}],
        }
        plan = audit_report.promotion_candidates(findings, {"crit": closed_by_us})
        self.assertEqual(plan.promote, ["crit"])

    def test_a_finding_a_human_closed_is_never_re_promoted(self):
        # Re-opening it would overrule a person, daily, forever.
        findings = [manifest_finding("crit", "a.yaml", severity="critical")]
        plan = audit_report.promotion_candidates(
            findings, {"crit": {"state": "CLOSED", "labels": []}}
        )
        self.assertEqual(plan.promote, [])

    def test_an_explicit_request_over_an_open_pr_is_reported_not_forced(self):
        findings = [manifest_finding("crit", "a.yaml")]
        plan = audit_report.promotion_candidates(
            findings, {"crit": {"state": "OPEN"}}, requested=["crit"]
        )
        self.assertEqual(plan.promote, [])
        self.assertEqual(plan.already_open, ["crit"])


class TestStaleRemediateRequests(BaseTestCase):
    """A `/remediate` is an override, not a standing order.

    Ledger comments are never edited away, so an old command re-reads as fresh
    on every cron run. Without an age it would re-open a pull request a person
    closed, every morning, forever — the exact loop `pr_closed_by_harness`
    exists to prevent, re-entered through the escape hatch.
    """

    def human_closed(self, closed_at="2026-07-15T00:00:00Z"):
        return {"state": "CLOSED", "labels": [], "closedAt": closed_at, "number": 8}

    def plan_for(self, asked_at, closed_at="2026-07-15T00:00:00Z"):
        findings = [manifest_finding("crit", "a.yaml")]
        return audit_report.promotion_candidates(
            findings,
            {"crit": self.human_closed(closed_at)},
            requested=["crit"],
            requested_at={"crit": asked_at} if asked_at is not None else None,
        )

    def test_a_request_older_than_the_close_is_superseded(self):
        plan = self.plan_for("2026-07-01T00:00:00Z")
        self.assertEqual(plan.promote, [])
        self.assertEqual(plan.superseded, ["crit"])

    def test_a_request_newer_than_the_close_overrules_it(self):
        # The escape hatch has to actually open: a human who changed their mind
        # asks again, and asking again is the whole mechanism.
        plan = self.plan_for("2026-07-20T00:00:00Z")
        self.assertEqual(plan.promote, ["crit"])
        self.assertEqual(plan.superseded, [])

    def test_a_request_at_the_same_instant_as_the_close_loses(self):
        # Equal timestamps cannot distinguish cause from effect, and the
        # cheaper mistake is the one a second `/remediate` fixes.
        self.assertEqual(self.plan_for("2026-07-15T00:00:00Z").superseded, ["crit"])

    def test_an_unknown_request_time_never_overrules_a_close(self):
        for asked_at in (None, "", "not-a-date"):
            with self.subTest(asked_at=asked_at):
                self.assertEqual(self.plan_for(asked_at).superseded, ["crit"])

    def test_an_unknown_close_time_still_blocks_a_stale_request(self):
        # A missing `closedAt` is a gh schema change, not evidence the close
        # never happened. Treating it as "no close" force-pushes over a human.
        plan = self.plan_for("2026-07-20T00:00:00Z", closed_at="")
        self.assertEqual(plan.promote, [])
        self.assertEqual(plan.superseded, ["crit"])

    def test_a_harness_close_is_re_promotable_regardless_of_request_age(self):
        findings = [manifest_finding("crit", "a.yaml")]
        plan = audit_report.promotion_candidates(
            findings,
            {
                "crit": {
                    "state": "CLOSED",
                    "labels": [{"name": audit_report.STALE_CLOSED_LABEL}],
                    "closedAt": "2026-07-15T00:00:00Z",
                }
            },
            requested=["crit"],
            requested_at={"crit": "2026-07-01T00:00:00Z"},
        )
        self.assertEqual(plan.promote, ["crit"])
        self.assertEqual(plan.superseded, [])

    def test_the_newest_request_for_a_finding_is_the_one_that_counts(self):
        findings = [manifest_finding("crit", "a.yaml")]
        parsed = audit_report.parse_remediate_commands(
            [
                comment("/remediate crit", node_id="IC_1", created_at="2026-07-01T00:00:00Z"),
                comment("/remediate crit", node_id="IC_2", created_at="2026-07-20T00:00:00Z"),
            ],
            findings,
        )
        self.assertEqual(parsed.requested_at, {"crit": "2026-07-20T00:00:00Z"})
        plan = audit_report.promotion_candidates(
            findings,
            {"crit": self.human_closed()},
            requested=parsed.targets,
            requested_at=parsed.requested_at,
        )
        self.assertEqual(plan.promote, ["crit"])

    def test_remediate_all_carries_the_comment_time_to_every_target(self):
        findings = [
            manifest_finding("a", "a.yaml"),
            manifest_finding("b", "b.yaml"),
        ]
        parsed = audit_report.parse_remediate_commands(
            [comment("/remediate all", created_at="2026-07-20T00:00:00Z")], findings
        )
        self.assertEqual(
            parsed.requested_at,
            {"a": "2026-07-20T00:00:00Z", "b": "2026-07-20T00:00:00Z"},
        )


class TestGhTimestamps(BaseTestCase):
    def test_a_z_suffix_parses_as_utc(self):
        parsed = audit_report.parse_gh_timestamp("2026-07-20T09:14:22Z")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.utcoffset().total_seconds(), 0)

    def test_an_offset_is_honoured_not_dropped(self):
        # 09:00+02:00 is 07:00Z — earlier than 08:00Z, which a naive string
        # compare gets backwards.
        self.assertTrue(
            audit_report.newer_timestamp(
                "2026-07-20T09:00:00+02:00", "2026-07-20T08:00:00Z"
            )
        )

    def test_garbage_is_none_not_an_exception(self):
        for value in (None, "", "   ", "yesterday", "2026-13-45T99:99:99Z"):
            with self.subTest(value=value):
                self.assertIsNone(audit_report.parse_gh_timestamp(value))

    def test_an_unparseable_candidate_never_wins(self):
        self.assertFalse(audit_report.newer_timestamp(None, "yesterday"))
        self.assertFalse(audit_report.newer_timestamp("2026-07-01T00:00:00Z", ""))

    def test_anything_parseable_beats_an_unknown_current(self):
        self.assertTrue(audit_report.newer_timestamp(None, "2026-07-01T00:00:00Z"))

    def test_strictly_after_refuses_an_unknown_on_either_side(self):
        # The asymmetry with newer_timestamp is the point: "unknown" must not
        # read as "infinitely old" when the question is whether to overrule a
        # person.
        known = "2026-07-01T00:00:00Z"
        self.assertFalse(audit_report.timestamp_strictly_after(known, None))
        self.assertFalse(audit_report.timestamp_strictly_after(None, known))
        self.assertFalse(audit_report.timestamp_strictly_after(known, known))
        self.assertTrue(
            audit_report.timestamp_strictly_after("2026-07-02T00:00:00Z", known)
        )


class TestStaleCloseLabelling(HarnessTestCase):
    def close_it(self, prs, current_ids=(), branch_by_finding=None):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            closed = audit_report.close_stale_remediation_prs(
                "acme/fleet",
                AUDIT,
                prs,
                set(current_ids),
                {},
                {},
                NOW,
                branch_by_finding=branch_by_finding,
            )
        self.err = err.getvalue()
        return closed

    def stale_pr(self, number=8, branch="platform-agent/fix-x-old"):
        return pr(number, branch, body=audit_report.delta_block(["gone"]))

    def test_the_label_is_applied_before_the_close(self):
        # A crash between the two leaves a labelled *open* pull request, which
        # is recoverable. The other order leaves an unlabelled closed one,
        # which reads as a human rejection forever.
        self.close_it([self.stale_pr()])
        order = [c for c in self.harness.calls if c[:2] == ["gh", "pr"]]
        label = next(i for i, c in enumerate(order) if "--add-label" in c)
        close = next(i for i, c in enumerate(order) if c[2] == "close")
        self.assertLess(label, close)
        self.assertIn(audit_report.STALE_CLOSED_LABEL, order[label])

    def test_the_branch_is_never_deleted(self):
        self.close_it([self.stale_pr()])
        for call in self.harness.calls:
            self.assertNotIn("--delete-branch", call)

    def test_a_failed_close_is_not_reported_as_closed(self):
        self.harness.failures = {"pr close": 1}
        self.assertEqual(self.close_it([self.stale_pr()]), [])
        self.assertIn("could not close PR #8", self.err)

    def test_an_announced_pull_request_is_closed_again_but_not_re_commented(self):
        # The marker records that the announcement happened, not that the pull
        # request shut. Every PR reaching this function is OPEN — so a marker
        # here means an earlier run commented and then failed to close, and
        # short-circuiting on it leaves the pull request open forever while the
        # ledger and the run summary both claim it closed.
        marked = pr(
            8,
            "platform-agent/fix-x-old",
            body=audit_report.delta_block(["gone"])
            + "\n"
            + audit_report.stale_closed_marker(8),
        )
        self.assertEqual(
            self.close_it([marked]), ["https://github.com/acme/fleet/pull/8"]
        )
        self.assertEqual(len(self.harness.gh_calls("pr", "close")), 1)
        self.assertEqual(self.harness.gh_calls("pr", "comment"), [])
        self.assertIn("retrying the close", self.err)

    def test_a_live_finding_keeps_its_pull_request_open(self):
        self.assertEqual(self.close_it([self.stale_pr()], current_ids={"gone"}), [])

    def test_an_orphaned_branch_is_closed_even_though_its_finding_lives(self):
        # The group's path set changed, so the work moved to a different
        # branch. Left open, this pull request conflicts with the new one.
        closed = self.close_it(
            [self.stale_pr()],
            current_ids={"gone"},
            branch_by_finding={"gone": "platform-agent/fix-x-new"},
        )
        self.assertEqual(len(closed), 1)
        comment = self.harness.gh_calls("pr", "comment")[0]
        self.assertIn("8", comment)

    def test_a_branch_that_is_still_live_is_left_alone(self):
        self.assertEqual(
            self.close_it(
                [self.stale_pr()],
                current_ids={"gone"},
                branch_by_finding={"gone": "platform-agent/fix-x-old"},
            ),
            [],
        )

    def test_a_finding_with_no_branch_at_all_keeps_its_pull_request(self):
        # The finding still reproduces but has dropped out of the manifest
        # groups — `degrade_missing_remediations` turned it `manual` because
        # the model did not write the file this run. There is no replacement
        # branch, so this pull request is the only fix in existence and the
        # orphan rule must not touch it.
        closed = self.close_it(
            [self.stale_pr()],
            current_ids={"gone"},
            branch_by_finding={"other": "platform-agent/fix-y"},
        )
        self.assertEqual(closed, [])
        self.assertEqual(self.harness.gh_calls("pr", "close"), [])
        self.assertIn("no remediation branch this run", self.err)

    def test_a_resolved_finding_on_an_orphaned_branch_is_told_it_resolved(self):
        # Both rules fire at once: nothing this pull request covers still
        # reproduces *and* the surviving groups rearranged onto other branches.
        # "The work now lives on a different branch" would send the reviewer
        # hunting for a replacement that was never opened, for a problem that
        # is already gone.
        self.close_it(
            [self.stale_pr()],
            current_ids={"someone-else"},
            branch_by_finding={"someone-else": "platform-agent/fix-x-new"},
        )
        body = self.harness.bodies_for("pr", "comment")[0]
        self.assertIn("no longer reproduces", body)
        self.assertNotIn("lives on a different branch", body)


# --------------------------------------------------------------------------- #
# Ledger selection and pagination
# --------------------------------------------------------------------------- #


class TestFindExistingIssue(HarnessTestCase):
    def find(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            result = audit_report.find_existing_issue("acme/fleet", AUDIT)
        self.err = err.getvalue()
        return result

    def test_the_highest_number_wins_so_the_choice_converges(self):
        # Duplicates exist because some run created one — and that run wrote
        # this stream's state into the higher number and linked it from every
        # pull request it opened. Preferring the lower one abandons that work
        # every run and the audit alternates between two ledgers forever.
        self.harness.replies = {
            "issue list": json.dumps(
                [
                    {"number": 7, "url": "https://github.com/acme/fleet/issues/7"},
                    {"number": 42, "url": "https://github.com/acme/fleet/issues/42"},
                ]
            )
        }
        number, url = self.find()
        self.assertEqual(number, 42)
        self.assertTrue(url.endswith("/42"))
        self.assertIn("7", self.err)

    def test_no_ledger_is_not_an_error(self):
        self.harness.replies = {"issue list": "[]"}
        self.assertEqual(self.find(), (None, None))

    def test_an_outage_raises_rather_than_reporting_no_ledger(self):
        self.harness.failures = {"issue list": 1}
        with self.assertRaises(audit_report.GitHubLookupError):
            self.find()

    def test_unparseable_output_raises(self):
        self.harness.replies = {"issue list": "not json"}
        with self.assertRaises(audit_report.GitHubLookupError):
            self.find()


class TestRemediationPrPaging(HarnessTestCase):
    def test_a_full_page_raises_rather_than_being_silently_truncated(self):
        # A truncated page reads as "no pull request", so the harness would
        # re-open fixes that already exist and re-promote findings a human
        # closed. Refusing the run is the only safe answer.
        self.harness.replies = {
            "pr list": json.dumps(
                [
                    pr(i, f"platform-agent/fix-{AUDIT}-{i}")
                    for i in range(audit_report.MAX_PR_PAGE)
                ]
            )
        }
        with self.assertRaises(audit_report.GitHubLookupError):
            audit_report.list_remediation_prs("acme/fleet", AUDIT)

    def test_a_short_page_is_returned(self):
        self.harness.replies = {"pr list": json.dumps([pr(1, "b")])}
        self.assertEqual(len(audit_report.list_remediation_prs("acme/fleet", AUDIT)), 1)


# --------------------------------------------------------------------------- #
# Acknowledging a /remediate that worked
# --------------------------------------------------------------------------- #


class TestAcknowledgements(HarnessTestCase):
    def test_an_accepted_request_gets_exactly_one_answer(self):
        # Silence is not an answer to a command: a requester who sees nothing
        # cannot tell "not run yet" from "ignored", so they ask again.
        audit_report.ack_remediate_requests(
            "acme/fleet",
            42,
            {"IC_1": ["netpol"]},
            {"netpol": "pull request opened — https://example.invalid/1"},
            [],
            NOW,
        )
        comments = self.harness.gh_calls("issue", "comment")
        self.assertEqual(len(comments), 1)

    def test_the_same_request_is_never_answered_twice(self):
        answered = [{"body": f"earlier\n{audit_report.acked_marker('IC_1')}\n"}]
        audit_report.ack_remediate_requests(
            "acme/fleet", 42, {"IC_1": ["netpol"]}, {}, answered, NOW
        )
        self.assertEqual(self.harness.gh_calls("issue", "comment"), [])

    def test_the_answer_names_the_outcome_of_each_target(self):
        body = audit_report.render_ack_comment(
            "IC_1", ["a", "b"], {"a": "pull request opened — u"}, NOW
        )
        self.assertIn("`a` — pull request opened — u", body)
        self.assertIn("`b` — no pull request was opened", body)
        self.assertIn(audit_report.acked_marker("IC_1"), body)

    def test_an_accepted_request_is_recorded_against_its_comment(self):
        parsed = audit_report.parse_remediate_commands(
            [
                {
                    "id": "IC_7",
                    "body": "/remediate netpol",
                    "author": {"login": "dev"},
                    "authorAssociation": "MEMBER",
                }
            ],
            [manifest_finding("netpol", "a.yaml")],
        )
        self.assertEqual(parsed.accepted_by_comment, {"IC_7": ["netpol"]})


class TestRemediationOutcomes(unittest.TestCase):
    def plan(self, **kw):
        return audit_report.PromotionPlan(
            kw.get("promote", []), kw.get("withheld", []), kw.get("already_open", [])
        )

    def requests(self, targets):
        return audit_report.RemediateRequests(targets, [], {})

    def test_a_freshly_opened_pull_request_is_named_by_url(self):
        out = audit_report._remediation_outcomes(
            self.requests(["a"]),
            self.plan(promote=["a"]),
            {"a": {"url": "https://example.invalid/1"}},
            ["https://example.invalid/1"],
        )
        self.assertIn("opened", out["a"])
        self.assertIn("https://example.invalid/1", out["a"])

    def test_an_untouched_open_pull_request_says_so(self):
        out = audit_report._remediation_outcomes(
            self.requests(["a"]),
            self.plan(already_open=["a"]),
            {"a": {"url": "https://example.invalid/1"}},
            [],
        )
        self.assertIn("already open", out["a"])
        self.assertIn("force-pushed", out["a"])

    def test_a_failure_is_reported_as_a_retry_not_as_success(self):
        out = audit_report._remediation_outcomes(
            self.requests(["a"]), self.plan(promote=["a"]), {}, []
        )
        self.assertIn("no pull request was opened", out["a"])


# --------------------------------------------------------------------------- #
# Body budget bookkeeping
# --------------------------------------------------------------------------- #


class TestRenderedIssue(unittest.TestCase):
    def flood(self, n):
        return make_doc(
            findings=[
                make_finding(fid=f"f-{i:04d}", severity="minor", title=f"Finding {i}")
                for i in range(n)
            ]
        )

    def test_a_complete_render_reports_nothing_omitted(self):
        rendered = audit_report.render_issue_body(make_doc(), generated_at=NOW)
        self.assertFalse(rendered.partial)
        self.assertEqual(rendered.omitted, [])
        self.assertEqual(rendered.rendered_ids, ["no-network-policy"])

    def test_a_truncated_render_says_which_ids_it_dropped(self):
        rendered = audit_report.render_issue_body(self.flood(400), generated_at=NOW)
        self.assertTrue(rendered.partial)
        self.assertLessEqual(len(rendered.body), GITHUB_BODY_LIMIT)
        self.assertEqual(
            len(rendered.rendered_ids) + len(rendered.omitted), 400
        )

    def test_the_delta_block_carries_only_what_a_reader_can_see(self):
        # The delta compares against the hidden block. If it listed findings
        # the body never rendered, every omitted finding would be announced as
        # newly resolved the moment the body got shorter.
        rendered = audit_report.render_issue_body(self.flood(400), generated_at=NOW)
        self.assertEqual(
            audit_report.parse_delta_block(rendered.body),
            sorted(rendered.rendered_ids),
        )

    def test_the_delta_comment_admits_partial_coverage_of_the_description(self):
        comment = audit_report.render_delta_comment(
            AUDIT, [], [], [], {}, NOW, omitted=12
        )
        self.assertIsNotNone(comment)
        self.assertIn("partial", comment)
        self.assertIn("12", comment)


# --------------------------------------------------------------------------- #
# Dry run — must describe the run that would actually happen
# --------------------------------------------------------------------------- #


class TestDryRunParity(HarnessTestCase):
    def dry(self, doc):
        rc = self.run_finish(doc, argv_extra=["--dry-run"])
        self.assertEqual(rc, 0)
        return self.out, self.err

    def test_it_touches_nothing(self):
        self.touch("clusters/prod-us-east/payments-netpol.yaml")
        self.dry(make_doc())
        self.assertEqual(self.harness.calls, [])

    def test_it_reports_the_branch_the_real_run_would_push(self):
        self.touch("clusters/prod-us-east/payments-netpol.yaml")
        _, err = self.dry(make_doc())
        expected = audit_report.group_branch_for(AUDIT, [make_finding()])
        self.assertIn(expected, err)

    def test_it_degrades_a_missing_manifest_like_the_real_run(self):
        _, err = self.dry(make_doc())
        self.assertIn("degrades to a manual remediation", err)
        self.assertIn("(no remediation pull requests)", err)

    def test_it_names_the_coverage_gaps(self):
        _, err = self.dry(
            make_doc(skipped=[{"cluster": "dr-west", "reason": "unreachable"}])
        )
        self.assertIn("COVERAGE GAP: dr-west", err)

    def test_a_clean_partial_run_does_not_claim_the_ledger_would_close(self):
        _, err = self.dry(
            make_doc(
                findings=[], skipped=[{"cluster": "dr-west", "reason": "unreachable"}]
            )
        )
        self.assertIn("left OPEN, not closed", err)

    def test_a_clean_complete_run_says_the_ledger_would_close(self):
        _, err = self.dry(make_doc(findings=[]))
        self.assertIn("would be closed", err)

    def test_it_prints_the_body_that_would_be_published(self):
        self.touch("clusters/prod-us-east/payments-netpol.yaml")
        out, _ = self.dry(make_doc())
        self.assertIn("## Findings", out)
        self.assertIn("audit-findings:", out)


class TestRepoResolution(BaseTestCase):
    """The repository must be resolvable before a clone exists.

    The old path ran `git config --get remote.origin.url` in the current
    directory. The audit crons start in the agent's profile directory, which is
    not a working tree, so that call returned nothing and the run died before
    it could clone anything — the token it needed to clone is repo-scoped, and
    the repo came from the clone. SETTINGS.md breaks the cycle.
    """

    def settings(self, text):
        path = self.tmp_path / "SETTINGS.md"
        path.write_text(text, encoding="utf-8")
        self.patch_attr("SETTINGS_PATH", str(path))
        return path

    def test_the_operator_written_line_is_parsed(self):
        self.settings(
            "# GKE Scope Configuration\n"
            "- **Git Repo:** https://github.com/acme/fleet.git\n"
        )
        self.assertEqual(audit_report.resolve_repo(), "acme/fleet")

    def test_an_ssh_remote_is_parsed(self):
        self.settings("- **Git Repo:** git@github.com:acme/fleet.git\n")
        self.assertEqual(audit_report.resolve_repo(), "acme/fleet")

    def test_a_bare_owner_name_is_parsed(self):
        self.settings("- **Git Repo:** acme/fleet\n")
        self.assertEqual(audit_report.resolve_repo(), "acme/fleet")

    def test_the_unset_placeholder_is_not_a_repository(self):
        # The operator writes the literal `None` when the CR omits the repo.
        # Treating that as an owner/name would send every gh call to a repo
        # called "None".
        self.settings("- **Git Repo:** None\n")
        self.assertIsNone(audit_report.repo_from_settings())

    def test_a_missing_settings_file_is_not_an_error_on_its_own(self):
        self.patch_attr("SETTINGS_PATH", str(self.tmp_path / "absent.md"))
        self.assertIsNone(audit_report.repo_from_settings())

    def test_it_falls_back_to_the_git_remote(self):
        self.patch_attr("SETTINGS_PATH", str(self.tmp_path / "absent.md"))
        module = type(sys)("github_token_refresh")
        module.get_current_git_repo = lambda: "acme/from-remote"
        with patch.dict(sys.modules, {"github_token_refresh": module}):
            self.assertEqual(audit_report.resolve_repo(), "acme/from-remote")

    def test_both_sources_failing_names_both_sources(self):
        missing = self.tmp_path / "absent.md"
        self.patch_attr("SETTINGS_PATH", str(missing))
        module = type(sys)("github_token_refresh")
        module.get_current_git_repo = lambda: None
        with patch.dict(sys.modules, {"github_token_refresh": module}):
            with self.assertRaises(RuntimeError) as caught:
                audit_report.resolve_repo()
        self.assertIn(str(missing), str(caught.exception))
        self.assertIn("origin remote", str(caught.exception))

    def test_settings_wins_over_whatever_directory_the_agent_is_in(self):
        self.settings("- **Git Repo:** https://github.com/acme/fleet\n")
        module = type(sys)("github_token_refresh")

        def explode():
            raise AssertionError("the git remote must not be consulted first")

        module.get_current_git_repo = explode
        with patch.dict(sys.modules, {"github_token_refresh": module}):
            self.assertEqual(audit_report.resolve_repo(), "acme/fleet")


class TestCredentialOrdering(HarnessTestCase):
    def setUp(self):
        super().setUp()
        self.minted = []
        self.order = []
        self.patch_attr("refresh_credentials", self._refresh)
        self.patch_attr("resolve_repo", self._resolve)
        self.patch_attr(
            "findings_path_for",
            lambda audit_id: str(self.tmp_path / f"findings_{audit_id}.json"),
        )
        patcher = patch.object(audit_report.os, "makedirs", lambda *a, **k: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _resolve(self):
        self.order.append("resolve")
        return "acme/fleet"

    def _refresh(self, repo=None):
        self.order.append("refresh")
        self.minted.append(repo)

    def test_the_repo_is_resolved_before_the_token_is_minted(self):
        self.harness.replies = {"issue list": "[]"}
        self.run_main(["start", "--audit", AUDIT])
        self.assertEqual(self.order[:2], ["resolve", "refresh"])

    def test_the_token_is_minted_for_the_resolved_repository(self):
        # Not for whatever `git config` reports in the current directory —
        # there is no clone there, so the no-argument call raises.
        self.harness.replies = {"issue list": "[]"}
        self.run_main(["start", "--audit", AUDIT])
        self.assertEqual(self.minted, ["acme/fleet"])

    def test_credentials_are_minted_before_the_clone(self):
        self.unclone()
        self.harness.replies = {"issue list": "[]"}
        self.run_main(["start", "--audit", AUDIT])
        clone = next(
            i for i, c in enumerate(self.harness.calls) if c[:2] == ["git", "clone"]
        )
        self.assertLess(self.order.index("refresh"), 2)
        self.assertGreaterEqual(clone, 0)


def na(check, reason="Autopilot — Google manages the node pools here."):
    """One `checks_not_applicable` entry, long enough to satisfy the validator."""
    return {"check": check, "reason": reason}


class TestNotApplicableChecks(unittest.TestCase):
    """A check that cannot apply is not a check nobody ran.

    Before this distinction existed the two were one state. An Autopilot
    cluster has no node pools, so the node-pool checks could never run against
    it and it sat at `6/10 ⚠` on every run forever. Permanent partiality is not
    a warning, it is a broken stream: `resolved` is pinned at 0, no stale
    remediation pull request ever closes, and the ledger cannot retire however
    healthy the fleet gets. Two of the three clusters on the fleet that
    surfaced this were Autopilot.

    The risk the tests below guard is the mirror image: `checks_not_applicable`
    is the only field that can *shrink* the denominator, so it is the obvious
    place to hide a check that simply was not performed.
    """

    def doc(self, na_entries, ran_checks=None, **kwargs):
        roster = list(audit_report.audit_checks(AUDIT))
        excused = {e["check"] for e in na_entries}
        if ran_checks is None:
            ran_checks = [c for c in roster if c not in excused]
        cluster = {
            "name": "prod-autopilot",
            "location": "us-central1",
            "project": "acme-prod",
            "checks_run": [ran(c, "prod-autopilot") for c in ran_checks],
            "checks_not_applicable": na_entries,
        }
        cluster.update(kwargs)
        return make_doc(clusters=[cluster])

    def test_an_inapplicable_check_is_not_a_coverage_gap(self):
        doc = self.doc([na("privileged-container")])
        audit_report.validate_findings(doc, AUDIT)
        self.assertEqual(audit_report.coverage_gaps(doc), [])

    def test_an_unrun_check_is_still_a_gap_beside_an_inapplicable_one(self):
        """Excusing one check does not excuse the one next to it."""
        roster = list(audit_report.audit_checks(AUDIT))
        doc = self.doc(
            [na(roster[0])],
            ran_checks=[c for c in roster if c not in (roster[0], roster[1])],
        )
        audit_report.validate_findings(doc, AUDIT)
        gaps = audit_report.coverage_gaps(doc)
        self.assertEqual(len(gaps), 1)
        self.assertIn(roster[1], gaps[0])
        self.assertNotIn(roster[0], gaps[0])

    def test_the_gap_line_counts_against_applicable_checks_only(self):
        """"1 of 10 applicable" — not "2 of 11", which would double-count."""
        roster = list(audit_report.audit_checks(AUDIT))
        doc = self.doc(
            [na(roster[0])],
            ran_checks=[c for c in roster if c not in (roster[0], roster[1])],
        )
        gaps = audit_report.coverage_gaps(doc)
        self.assertIn(f"1 of {len(roster) - 1} applicable checks did not run", gaps[0])

    def test_a_fully_excused_and_fully_run_cluster_is_not_partial(self):
        """The whole point: an Autopilot cluster can be complete."""
        doc = self.doc([na(c) for c in list(audit_report.audit_checks(AUDIT))[:4]])
        audit_report.validate_findings(doc, AUDIT)
        self.assertEqual(audit_report.coverage_gaps(doc), [])

    def test_a_limitations_note_still_goes_partial(self):
        """`limitations` means impaired. Inapplicability has its own field now."""
        doc = self.doc(
            [na("privileged-container")],
            limitations="Autopilot: node-level checks do not apply.",
        )
        gaps = audit_report.coverage_gaps(doc)
        self.assertEqual(len(gaps), 1)

    def test_a_check_cannot_be_both_run_and_inapplicable(self):
        roster = list(audit_report.audit_checks(AUDIT))
        doc = self.doc([na(roster[0])], ran_checks=roster)
        with self.assertRaises(audit_report.ValidationError) as ctx:
            audit_report.validate_findings(doc, AUDIT)
        self.assertIn("also in this cluster's checks_run", str(ctx.exception))

    def test_an_unknown_slug_is_rejected(self):
        doc = self.doc([na("not-a-real-check")])
        with self.assertRaises(audit_report.ValidationError) as ctx:
            audit_report.validate_findings(doc, AUDIT)
        self.assertIn("not a check in", str(ctx.exception))

    def test_the_unknown_slug_rejection_does_not_print_the_roster(self):
        """Same answer-key problem as `checks_run`, same rule."""
        doc = self.doc([na("not-a-real-check")])
        with self.assertRaises(audit_report.ValidationError) as ctx:
            audit_report.validate_findings(doc, AUDIT)
        message = str(ctx.exception)
        for check in audit_report.audit_checks(AUDIT):
            self.assertNotIn(check, message)
        self.assertIn(audit_report.audit_sop(AUDIT), message)

    def test_a_duplicate_is_rejected(self):
        doc = self.doc([na("privileged-container"), na("privileged-container")])
        with self.assertRaises(audit_report.ValidationError) as ctx:
            audit_report.validate_findings(doc, AUDIT)
        self.assertIn("duplicate check", str(ctx.exception))

    def test_an_abbreviation_is_not_a_reason(self):
        for excuse in ("n/a", "N/A", "-", "skip", "not applicable"):
            with self.subTest(excuse=excuse):
                doc = self.doc([na("privileged-container", excuse)])
                with self.assertRaises(audit_report.ValidationError) as ctx:
                    audit_report.validate_findings(doc, AUDIT)
                self.assertIn("does not say why", str(ctx.exception))

    def test_a_missing_reason_is_rejected(self):
        doc = self.doc([{"check": "privileged-container"}])
        with self.assertRaises(audit_report.ValidationError):
            audit_report.validate_findings(doc, AUDIT)

    def test_a_bare_slug_is_rejected(self):
        doc = self.doc([])
        doc["scope"]["clusters"][0]["checks_not_applicable"] = ["privileged-container"]
        with self.assertRaises(audit_report.ValidationError) as ctx:
            audit_report.validate_findings(doc, AUDIT)
        self.assertIn("expected an object", str(ctx.exception))

    def test_the_field_is_optional(self):
        doc = make_doc()
        doc["scope"]["clusters"][0].pop("checks_not_applicable", None)
        audit_report.validate_findings(doc, AUDIT)
        self.assertEqual(audit_report.coverage_gaps(doc), [])

    def test_the_scope_table_shows_the_denominator_and_the_na_count(self):
        roster = list(audit_report.audit_checks(AUDIT))
        doc = self.doc([na(roster[0]), na(roster[1])])
        body = render_body(doc, generated_at=NOW)
        self.assertIn(f"| {len(roster) - 2}/{len(roster) - 2} (2 n/a) |", body)
        self.assertNotIn("⚠", body.split("## Findings")[0])

    def test_the_reason_is_published_where_a_reader_can_judge_it(self):
        doc = self.doc([na("privileged-container", "Autopilot blocks privileged pods.")])
        body = render_body(doc, generated_at=NOW)
        self.assertIn("Not applicable (1)", body)
        self.assertIn("Autopilot blocks privileged pods.", body)

    def test_no_na_section_when_nothing_is_excused(self):
        body = render_body(make_doc(), generated_at=NOW)
        self.assertNotIn("Not applicable", body)


class TestSilentVerdict(HarnessTestCase):
    """`silent_ok` is computed, not re-derived by the model.

    The rule used to be four clauses of prose evaluated against the model's own
    reading of this JSON. On 2026-08-03 a run with `partial: true` evaluated it
    to `[SILENT]`, suppressed its own delivery, and the operator who had asked
    for the run got a kanban summary that named no issue. The harness holds
    every input; it should hold the verdict.
    """

    def finish_json(self, doc, **replies):
        self.harness.replies = {
            "issue list": self.issue_list(),
            "--json body": json.dumps({"body": published_body(doc, generated_at=NOW)}),
            **replies,
        }
        self.run_finish(doc)
        return self.stdout_json()

    def test_an_unchanged_complete_clean_run_is_silent(self):
        doc = make_doc(findings=[])
        out = self.finish_json(doc)
        self.assertTrue(out["silent_ok"])

    def test_a_partial_run_is_never_silent(self):
        """The exact shape that went silent on 2026-08-03."""
        doc = make_doc(
            findings=[],
            clusters=[
                {
                    "name": "prod-autopilot",
                    "location": "us-central1",
                    "project": "acme-prod",
                    "checks_run": ["privileged-container"],
                }
            ],
        )
        out = self.finish_json(doc)
        self.assertTrue(out["partial"])
        self.assertFalse(out["silent_ok"])

    def test_new_findings_are_never_silent(self):
        self.harness.replies = {
            "issue list": self.issue_list(),
            "--json body": json.dumps(
                {"body": published_body(make_doc(findings=[]), generated_at=NOW)}
            ),
        }
        self.run_finish(make_doc(findings=[make_finding(fid="a")]))
        out = self.stdout_json()
        self.assertEqual(out["new"], 1)
        self.assertFalse(out["silent_ok"])

    def test_the_verdict_agrees_with_the_fields_beside_it(self):
        """Whatever else changes, `silent_ok` stays a function of the JSON."""
        for findings in ([], [make_finding(fid="a")]):
            with self.subTest(findings=len(findings)):
                out = self.finish_json(make_doc(findings=findings))
                self.assertEqual(
                    out["silent_ok"],
                    not (
                        out["new"]
                        or out["resolved"]
                        or out["partial"]
                        or out["prs_opened"]
                        or out["prs_closed"]
                    ),
                )


class TestDispatchAndHandover(unittest.TestCase):
    """The ledger URL has to survive the hop from worker to requester.

    A dispatched run's transcript goes to a log file nothing downstream reads.
    What the requester sees is the kanban card, so the URL has to be on the
    card — and on 2026-08-03 it was not: the card's summary said "the existing
    ledger issue" with no number, and that sentence was the Slack message.
    """

    def read(self, relative):
        agent_dir = Path(__file__).resolve().parents[4] / "platform"
        path = agent_dir / relative
        if not path.is_file():
            self.skipTest(f"{relative} not present")
        return path.read_text(encoding="utf-8")

    def test_the_dispatch_rule_requires_reporting_the_result(self):
        text = self.read("AGENTS.md")
        bullet = next(
            line for line in text.splitlines() if "cronjob(action='run'" in line
        )
        self.assertIn("kanban_complete", bullet)
        self.assertIn("[SILENT]", bullet)

    def test_the_worker_protocol_requires_the_url_in_the_summary(self):
        section = self.read("SOUL.md").split("## 1.")[0]
        self.assertIn("URL", section)

    def test_every_sop_says_an_on_demand_run_is_never_silent(self):
        for audit_id in audit_report.AUDITS:
            with self.subTest(audit=audit_id):
                text = self.read(f"governance/{audit_report.audit_sop(audit_id)}")
                self.assertIn("silent_ok", text)
                self.assertIn("on-demand", text.lower())


if __name__ == "__main__":
    unittest.main()
