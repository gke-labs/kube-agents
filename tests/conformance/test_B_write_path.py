"""Group B -- The write path.

B1  No agent principal holds a credential that can cause a production change.
B2  Assent is human or policy. Agents get veto only.
B3  The enforcement substrate is not agent-authorable.
B4  The executor is a governed principal.
B5  What the approver sees is what will be applied.
B6  No self-approval, and no agent satisfies a required review.

Agents are read-only against clusters and propose changes as pull requests, so
the ceiling this group tests is a ceiling on `kubectl` -- not a ceiling on
impact. An invariant set that only governs API calls governs the wrong API,
which is why most of what follows reads workflows and manifests rather than
argv.
"""

from __future__ import annotations

import re
import sys
import unittest

import yaml

from . import _harness as h
from ._harness import command_policy

WORKFLOWS = sorted((h.REPO_ROOT / ".github" / "workflows").glob("*.yml"))


def _workflow_documents():
    """Every workflow, parsed, with YAML 1.1's `on:` -> True quirk normalised."""
    for path in WORKFLOWS:
        document = yaml.safe_load(path.read_text())
        if True in document:  # `on:` is the YAML 1.1 boolean `y`/`yes`/`on`
            document["on"] = document.pop(True)
        yield path, document


class B1NoAgentCredentialCausesAProductionChange(unittest.TestCase):
    """B1: not "cannot mutate a cluster" -- cannot cause a change, by any route."""

    def test_B1_kubectl_write_verbs_are_refused(self) -> None:
        """The headline question, in the form a reviewer asks it.

        Read-only is an allowlist rather than a denylist here, deliberately:
        over-blocking kubectl breaks a skill and someone files a bug, while
        under-blocking it against a customer's production cluster is the thing
        the model exists to prevent. These are the verbs a denylist author
        would have had to think of, and the point is that they are refused
        without anyone having thought of them.
        """
        writes = (
            ["kubectl", "delete", "namespace", "prod"],
            ["kubectl", "delete", "pod", "web-0"],
            ["kubectl", "apply", "-f", "manifest.yaml"],
            ["kubectl", "create", "deployment", "web", "--image=nginx"],
            ["kubectl", "patch", "deployment", "web", "-p", "{}"],
            ["kubectl", "replace", "-f", "manifest.yaml"],
            ["kubectl", "edit", "deployment", "web"],
            ["kubectl", "scale", "deployment", "web", "--replicas=0"],
            ["kubectl", "annotate", "pod", "web-0", "a=b"],
            ["kubectl", "label", "pod", "web-0", "a=b"],
            ["kubectl", "set", "image", "deployment/web", "web=nginx:2"],
            ["kubectl", "rollout", "restart", "deployment/web"],
            ["kubectl", "rollout", "undo", "deployment/web"],
            ["kubectl", "drain", "node-1"],
            ["kubectl", "cordon", "node-1"],
            ["kubectl", "uncordon", "node-1"],
            ["kubectl", "taint", "nodes", "node-1", "k=v:NoSchedule"],
            ["kubectl", "exec", "web-0", "--", "sh"],
            ["kubectl", "cp", "web-0:/etc/passwd", "/tmp/p"],
            ["kubectl", "port-forward", "web-0", "8080:80"],
            ["kubectl", "attach", "web-0"],
            ["kubectl", "proxy"],
            ["kubectl", "run", "shell", "--image=busybox"],
            ["kubectl", "debug", "web-0", "--image=busybox"],
            ["kubectl", "certificate", "approve", "csr-1"],
        )
        for argv in writes:
            with self.subTest(argv=argv):
                decision = command_policy.evaluate(argv)
                self.assertFalse(decision.allowed, f"{argv} reached the cluster")

    def test_B1_ordinary_reads_still_work(self) -> None:
        """A read-only gate nobody can work behind gets switched off.

        `CREDENTIAL_PROXY_ENFORCE_READ_ONLY` is global, unscoped and has no
        expiry, so the cost of a false refusal is not one failed command -- it
        is an operator disabling the whole posture to get through the day. The
        allowlist's coverage is therefore part of the control.
        """
        reads = (
            ["kubectl", "get", "pods", "-n", "prod"],
            ["kubectl", "describe", "node", "node-1"],
            ["kubectl", "logs", "web-0", "-f"],
            ["kubectl", "top", "pods"],
            ["kubectl", "events", "--for", "pod/web-0"],
            ["kubectl", "auth", "can-i", "delete", "pods"],
            ["kubectl", "rollout", "status", "deployment/web"],
            ["kubectl", "rollout", "-n", "prod", "history", "deployment/web"],
            ["kubectl", "api-resources"],
            ["kubectl", "explain", "pod.spec"],
            ["kubectl", "config", "current-context"],
            ["gcloud", "container", "clusters", "list"],
            ["gcloud", "--project", "p", "container", "clusters", "describe", "c"],
            ["gcloud", "container", "clusters", "get-credentials", "c"],
            ["gcloud", "logging", "read", "resource.type=k8s_cluster"],
            ["gcloud", "projects", "get-iam-policy", "p"],
        )
        for argv in reads:
            with self.subTest(argv=argv):
                decision = command_policy.evaluate(argv)
                self.assertTrue(decision.allowed, f"{argv}: {decision.message}")

    def test_B1_gcloud_write_commands_are_refused(self) -> None:
        """gcloud's grammar puts the verb neither first nor last.

        `gcloud container clusters get-credentials prod` ends in a cluster
        name, so finding the verb by position would mean encoding gcloud's
        whole command tree. The allowlist of read paths is what makes these
        refusals fall out rather than needing to be enumerated.
        """
        writes = (
            ["gcloud", "container", "clusters", "delete", "prod"],
            ["gcloud", "container", "clusters", "create", "prod"],
            ["gcloud", "container", "clusters", "update", "prod", "--enable-autoscaling"],
            ["gcloud", "projects", "add-iam-policy-binding", "p", "--member=user:x"],
            ["gcloud", "projects", "set-iam-policy", "p", "policy.json"],
            ["gcloud", "iam", "service-accounts", "keys", "create", "k.json"],
            ["gcloud", "compute", "instances", "delete", "vm-1"],
            ["gcloud", "container", "node-pools", "delete", "np-1"],
            ["gcloud", "secrets", "versions", "access", "latest", "--secret=s"],
        )
        for argv in writes:
            with self.subTest(argv=argv):
                decision = command_policy.evaluate(argv)
                self.assertFalse(decision.allowed, f"{argv} reached the project")

    def test_B1_the_sandbox_image_ships_no_credentialed_cli(self) -> None:
        """The build gate, asserted against the stage graph rather than a grep.

        A gate in a stage the agent image does not derive from is not a gate,
        and the `credential-proxy` stage deliberately reinstalls all four CLIs
        afterwards -- so "the Dockerfile contains this RUN" is not the
        assertion. This walks `FROM` back from the agent target and requires
        the gate on that path.
        """
        source = h.text("dockerfile")

        # Instruction keywords are matched case-sensitively and continuation
        # lines are skipped, because neither is optional here: `from
        # gateway.kanban_handoff_clip import …`, inside a multi-line RUN that
        # patches a plugin, reads as a stage boundary under the obvious
        # case-insensitive regex and silently splits agent-base in two. The
        # first draft of this test passed for that reason.
        stages: dict[str, str] = {}
        parents: dict[str, str] = {}
        current = None
        continued = False
        for line in source.splitlines():
            match = None if continued else re.match(r"^FROM\s+(\S+)(?:\s+AS\s+(\S+))?", line)
            continued = line.rstrip().endswith("\\")
            if match:
                parent, name = match.group(1), match.group(2)
                current = name or parent
                stages[current] = ""
                parents[current] = parent
                continue
            if current is not None:
                stages[current] += line + "\n"

        self.assertIn("platform", stages, "the agent build target is gone")

        lineage, cursor = [], "platform"
        while cursor in stages:
            lineage.append(cursor)
            parent = parents[cursor]
            if parent == cursor or parent not in stages:
                break
            cursor = parent

        gate = "unexpected credential-aware CLI in sandbox image"
        gated = [stage for stage in lineage if gate in stages[stage]]
        self.assertTrue(
            gated,
            f"no stage on the agent image's lineage {lineage} asserts the "
            f"absence of credentialed CLIs",
        )
        # The gate has to cover all four, not just the one someone remembered.
        gate_stage = stages[gated[0]]
        for binary in ("gcloud", "kubectl", "gh", "git"):
            with self.subTest(binary=binary):
                self.assertRegex(
                    gate_stage,
                    rf"for binary in[^\n]*\b{binary}\b",
                    f"the build gate does not check for {binary}",
                )

    def test_B1_the_shipped_denylist_refuses_credential_disclosure(self) -> None:
        """Read out of the rendered ConfigMap, not out of the Go constant.

        The constant is what someone wrote; the ConfigMap is what the sidecar
        loads. These are the commands that hand the credential to the caller
        rather than using it, which is the one thing the denylist has always
        been for.
        """
        disclosures = (
            ["gcloud", "auth", "print-access-token"],
            ["gcloud", "auth", "print-identity-token"],
            ["gcloud", "config", "config-helper"],
            ["gh", "auth", "token"],
            ["gh", "auth", "status", "--show-token"],
            ["kubectl", "create", "token", "default"],
            ["kubectl", "config", "view", "--raw"],
            ["git", "credential", "fill"],
            ["gcloud", "auth", "login"],
            ["gcloud", "auth", "activate-service-account", "--key-file=k.json"],
            ["gh", "auth", "login"],
            ["gh", "auth", "refresh"],
            ["gcloud", "components", "install", "alpha"],
            ["gh", "extension", "install", "owner/repo"],
        )
        for argv in disclosures:
            with self.subTest(argv=argv):
                self.assertIsNotNone(
                    h.policy_blocks(argv),
                    f"{argv} is not matched by any shipped denylist rule",
                )

    def test_B1_precondition_the_denylist_governs_gh(self) -> None:
        """Guards the expected-failure below: `gh` rules must still exist."""
        rule_ids = {rule["id"] for rule in h.rendered_policy_rules()}
        self.assertIn("github.credential-replacement", rule_ids)
        self.assertIn("gh", h.credential_proxy.CommandExecutor.ALLOWED_EXECUTABLES)

    @h.known_violation("B1", "04_major_requirements.md B1")
    def test_B1_the_agent_cannot_merge_or_approve(self) -> None:
        """KNOWN VIOLATION. `gh pr merge` works today.

        `gh` is in ALLOWED_EXECUTABLES and every denial rule matches only
        `gh auth` or `gh extension`, so nothing stops the agent merging its own
        pull request, approving one, or force-pushing a branch a GitOps
        Application watches. `command_policy` puts `gh` out of scope on
        purpose -- writing to the artifact plane is how the agent is meant to
        act -- and the git workspace lease is a concurrency control that says
        so in its own docstring.

        B1 is not "cannot mutate a cluster", it is "cannot cause a production
        change". Holding a credential that can merge is causing one. The repo's
        own code agrees: the App "does have write access - it opens pull
        requests on this repository and merges them."
        """
        for argv in (
            ["gh", "pr", "merge", "1", "--squash"],
            ["gh", "pr", "review", "1", "--approve"],
            ["gh", "pr", "merge", "--auto", "1"],
            ["git", "push", "--force", "origin", "main"],
        ):
            with self.subTest(argv=argv):
                self.assertIsNotNone(
                    h.policy_blocks(argv),
                    f"{argv} is permitted by the shipped denylist",
                )


class B2AssentIsHumanOrPolicy(unittest.TestCase):
    """B2: gatekeepers may block and may never approve."""

    def test_B2_no_workflow_approves_or_merges_a_pull_request(self) -> None:
        """The structural form of "no model verdict causes a merge".

        A veto is monotone in the safe direction -- successful injection
        against one produces a false block, a nuisance on one pull request
        rather than a breach. That property only holds while nothing in CI can
        assent, so this refuses the mechanisms rather than the intent: the
        merge and approve calls, and GitHub's auto-merge.
        """
        assenting = (
            r"gh\s+pr\s+merge",
            r"gh\s+pr\s+review[^\n]*--approve",
            r"enablePullRequestAutoMerge",
            r"pulls/\$?\{?[^\n]*\}?/reviews",
            r"peter-evans/enable-pull-request-automerge",
            r"pascalgn/automerge-action",
            r"hmarr/auto-approve-action",
        )
        offences = []
        for path in WORKFLOWS:
            text = path.read_text()
            for pattern in assenting:
                for match in re.finditer(pattern, text):
                    line = text[: match.start()].count("\n") + 1
                    offences.append(f"{path.name}:{line} {match.group(0)!r}")
        self.assertEqual(
            [],
            offences,
            "a workflow can assent to its own repository's changes",
        )

    def test_B2_no_workflow_grants_a_bot_the_ability_to_approve(self) -> None:
        """`pull-requests: write` is the permission an approval needs.

        Two workflows hold it, and neither can give an approval:

        - auto_request_review, which requests reviewers and does not give them.
        - auto-assign-milestone, added on main after this suite was written.
          It triggers on `pull_request_target: closed` gated on
          `merged == true`, so it runs only after the merge decision has been
          taken, and its one call is `gh pr edit --milestone`.

        The list is an allowlist of holders, not of intents: the permission is
        a capability, and this asserts membership rather than absence so a
        third holder is a red test and a conversation rather than a silent
        addition. Adding a name here means someone read the workflow.
        """
        holders = []
        for path, document in _workflow_documents():
            scopes = [document.get("permissions") or {}]
            scopes += [
                (job or {}).get("permissions") or {}
                for job in (document.get("jobs") or {}).values()
            ]
            for scope in scopes:
                if isinstance(scope, dict) and scope.get("pull-requests") == "write":
                    holders.append(path.name)
                    break
        self.assertEqual(
            ["auto-assign-milestone.yml", "auto_request_review.yml"],
            sorted(set(holders)),
            "an unexpected workflow can write to pull requests",
        )


class B3TheSubstrateIsNotAgentAuthorable(unittest.TestCase):
    """B3: the biggest thing the first draft of the invariants missed."""

    def test_B3_precondition_the_customer_gitops_template_still_exists(self) -> None:
        self.assertIn("/clusters/", h.text("codeowners_example"))

    @h.known_violation("B3", "overnight-b/findings.md 2.3")
    def test_B3_the_substrate_paths_are_enumerated_as_code(self) -> None:
        """KNOWN VIOLATION. The human-only path set exists only in prose.

        B3's own test text is "substrate paths enumerated as code. A PR
        touching them takes a different, human-only path than a PR changing a
        replica count." There is no such enumeration anywhere in this
        repository -- not a checker, not a workflow, not a data file. The
        nearest artifact is `examples/gitops-repo/CODEOWNERS.example`, which is
        a template for the *customer's* repository and is not enforced here,
        and `branch-protection.md`, which documents a `review-gate.yml`
        workflow that does not exist.

        Two consequences worth separating. Nothing gates a change to the VAP,
        the operator ClusterRole or the workflows in this repo differently from
        a change to a replica count. And path-based gating would not be enough
        even if it existed -- Kubernetes does not care what directory a
        manifest lives in, so a ClusterRoleBinding committed under
        `clusters/*/namespaces/team-x/` matches the namespace glob and gets
        approved by the wrong humans. The invariant wants the rendered object
        set gated, not the path.
        """
        candidates = [
            h.REPO_ROOT / ".github" / "CODEOWNERS",
            h.REPO_ROOT / "CODEOWNERS",
            h.REPO_ROOT / "docs" / "CODEOWNERS",
            h.REPO_ROOT / ".github" / "workflows" / "review-gate.yml",
        ]
        present = [path for path in candidates if path.is_file()]
        self.assertTrue(
            present,
            "no substrate enumeration and no gate that reads one",
        )

    def test_B3_the_agent_cannot_reach_the_admission_policy_through_kubectl(self) -> None:
        """One half of B3 that *is* enforced, and worth pinning.

        The VAP and the RBAC it guards live in the repository the agent
        proposes into, so the artifact-plane half is open. The API half is not:
        every verb that would edit an admission policy or a NetworkPolicy in
        place is refused by the read-only allowlist.
        """
        for argv in (
            ["kubectl", "delete", "validatingadmissionpolicy", "kube-agents-agent-readonly"],
            ["kubectl", "patch", "validatingadmissionpolicybinding", "b", "-p", "{}"],
            ["kubectl", "delete", "networkpolicy", "platformagent-sandbox-metadata-deny"],
            ["kubectl", "apply", "-f", "clusterrolebinding.yaml"],
            ["kubectl", "delete", "clusterrole", "kubeagents:explorer"],
        ):
            with self.subTest(argv=argv):
                self.assertFalse(command_policy.evaluate(argv).allowed)


class B4TheExecutorIsAGovernedPrincipal(unittest.TestCase):
    """B4: CI/CD holds the only production write credential, so it is in scope."""

    def test_B4_every_workflow_run_deploy_gates_on_repository_and_branch(self) -> None:
        """`workflow_run` fires from the default branch with the *triggering* run's context.

        Without all three predicates a fork's completed run, or a run from a
        non-default branch, reaches a job that mints a deployment credential.
        The three are asserted individually so that dropping one -- the
        plausible edit, made while debugging a deploy -- is red.
        """
        deploys = [
            (path, document)
            for path, document in _workflow_documents()
            if "workflow_run" in (document.get("on") or {})
        ]
        self.assertTrue(deploys, "no workflow_run deploys found; the filter is wrong")

        for path, document in deploys:
            for job_name, job in (document.get("jobs") or {}).items():
                condition = str((job or {}).get("if", ""))
                with self.subTest(workflow=path.name, job=job_name):
                    self.assertIn("github.repository ==", condition)
                    self.assertIn("workflow_run.conclusion == 'success'", condition)
                    self.assertIn("workflow_run.head_branch == 'main'", condition)

    def test_B4_no_pull_request_target_workflow_checks_out_the_pull_request(self) -> None:
        """`pull_request_target` runs with the base repository's secrets.

        Checking out the head is how that becomes arbitrary code execution with
        write credentials. One workflow uses the trigger, it has no checkout
        step, and this is what keeps it that way.
        """
        for path, document in _workflow_documents():
            triggers = document.get("on") or {}
            if "pull_request_target" not in triggers:
                continue
            text = path.read_text()
            with self.subTest(workflow=path.name):
                self.assertNotIn("actions/checkout", text)
                for scope in [document.get("permissions") or {}] + [
                    (job or {}).get("permissions") or {}
                    for job in (document.get("jobs") or {}).values()
                ]:
                    if isinstance(scope, dict):
                        self.assertNotEqual("write", scope.get("contents"))
                        self.assertNotEqual("write", scope.get("id-token"))

    def test_B4_contents_write_is_confined_to_the_release_path(self) -> None:
        """The credential that can push to this repository, and where it lives.

        Release tagging is the only thing that needs it. Asserting the set
        rather than the absence keeps the grant reviewable: adding a fifth
        holder is a decision someone makes on purpose.
        """
        holders = set()
        for path, document in _workflow_documents():
            scopes = [document.get("permissions") or {}] + [
                (job or {}).get("permissions") or {}
                for job in (document.get("jobs") or {}).values()
            ]
            for scope in scopes:
                if isinstance(scope, dict) and scope.get("contents") == "write":
                    holders.add(path.name)
        self.assertEqual(
            {
                "rc-create-tag.yml",
                "rc-tag-validated.yml",
                "rc-release-pipeline.yml",
            },
            holders,
        )


class B5WhatTheApproverSeesIsWhatWillBeApplied(unittest.TestCase):
    """B5: the invariant we never stated, on the input to the one we did."""

    def setUp(self) -> None:
        scripts = h.REPO_ROOT / "agents/platform/skills/fleet-audit/scripts"
        if str(scripts) not in sys.path:
            sys.path.insert(0, str(scripts))
        import audit_report

        self.audit_report = audit_report

    def test_B5_precondition_the_renderer_still_sanitises_its_inputs(self) -> None:
        """The markdown-injection hardening that *does* exist, pinned.

        `_ident` flattens newlines and replaces backticks because either one
        ends an inline code span and renders the rest of an attacker's value as
        markup. `_cell` escapes the pipe that would otherwise forge a table
        column. Both are real controls and neither should be lost while the
        expected failure below is being fixed.
        """
        self.assertEqual("a'b", self.audit_report._ident("a`b"))
        self.assertEqual("a b", self.audit_report._ident("a\nb"))
        self.assertEqual("a\\|b", self.audit_report._cell("a|b"))
        self.assertEqual("a b", self.audit_report._cell("a\nb"))

    @h.known_violation("B5", "overnight-b/findings.md 2.4")
    def test_B5_rendered_evidence_carries_no_direction_or_width_trickery(self) -> None:
        """KNOWN VIOLATION. Bidi and zero-width characters survive into the body.

        The fleet audit requires every finding to carry "the exact read-only
        command that produced it and a verbatim excerpt of the output". Those
        are attacker-controlled bytes rendered inside a block the system labels
        evidence -- anyone who can write a Pod log can write to it.

        The renderer is careful about markdown: backticks, pipes and newlines
        are all neutralised, and the fence is chosen to be longer than any
        run inside the content. What it does not handle is the class B5 names
        explicitly: `U+202E` reverses the displayed order of everything after
        it, and `U+200B`/`U+FEFF` hide inside an identifier. So a reader can be
        shown `kubectl get pods` for a value that is not that, inside the
        block the document calls evidence.

        This is B5's whole point. B2 makes the human the decision point; without
        B5 that boundary has no integrity requirement on its only input.
        """
        trickery = {
            "‮": "right-to-left override",
            "‭": "left-to-right override",
            "⁦": "left-to-right isolate",
            "​": "zero-width space",
            "﻿": "zero-width no-break space",
            "‎": "left-to-right mark",
        }
        for character, description in trickery.items():
            payload = f"kubectl get pods{character} --all-namespaces"
            for name, function in (
                ("_ident", self.audit_report._ident),
                ("_cell", self.audit_report._cell),
                ("trim_command", self.audit_report.trim_command),
                ("trim_excerpt", self.audit_report.trim_excerpt),
            ):
                with self.subTest(character=description, renderer=name):
                    self.assertNotIn(
                        character,
                        function(payload),
                        f"{name} passes {description} through to the approver",
                    )


class B6NoSelfApproval(unittest.TestCase):
    """B6: an approver must hold authority sufficient to make the change directly."""

    def test_B6_the_gitops_template_names_no_automation_identity(self) -> None:
        """The one thing GitHub can actually enforce.

        Self-approval is blocked on account identity, so two agent identities
        defeat it, and approval *count* can probably be satisfied by a GitHub
        App token -- undocumented, and Minty already mints these. The two rules
        a bot cannot satisfy are CODEOWNERS review and the ruleset
        required-reviewer team rule, because apps are not eligible code owners
        and cannot be team members. So the mechanism has to be a named human
        team containing no automation identity, and this asserts the template
        we hand customers says that.
        """
        rules = [
            line
            for line in h.text("codeowners_example").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        owners = [
            owner for line in rules for owner in re.findall(r"@[\w.-]+(?:/[\w.-]+)?", line)
        ]
        self.assertTrue(owners, "the template names no owners at all")
        for owner in owners:
            with self.subTest(owner=owner):
                self.assertNotIn("[bot]", owner)
                self.assertFalse(
                    owner.endswith("-agent"),
                    "an agent identity is named as a code owner",
                )
                self.assertIn(
                    "/",
                    owner,
                    "a code owner must be a team rather than an individual "
                    "account, so that the required review cannot be satisfied "
                    "by whoever happens to be on call",
                )

    def test_B6_every_guarded_path_in_the_template_has_an_owner(self) -> None:
        """A CODEOWNERS entry that covers nothing is the trap this avoids.

        The four path classes the branch-protection note calls guarded --
        provisioning, agents, namespaces and policy -- each need a rule, or
        the ruleset that requires code-owner review on them requires review
        from nobody.
        """
        text = h.text("codeowners_example")
        rules = [
            line.split()[0]
            for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        for guarded in ("provisioning", "agents", "namespaces", "policy"):
            with self.subTest(path=guarded):
                self.assertTrue(
                    any(guarded in rule for rule in rules),
                    f"no CODEOWNERS rule covers {guarded}",
                )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
