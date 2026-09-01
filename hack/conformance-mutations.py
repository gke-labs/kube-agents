#!/usr/bin/env python3
"""Mutation-verify the conformance suite: delete a control, expect red.

If deleting a control leaves the suite green, the test does not exist. Slice 2a
shipped a whole gate that could be removed with its suite byte-identical, and
only a dedicated task caught it -- so this is the property the conformance
suite is graded on, not test count.

Each mutation names the control it removes and the test it must break. The run
applies one mutation at a time to the working tree, runs the suite, restores
the file with `git checkout`, and reports:

    KILLED   the named test failed. The test is real.
    SURVIVED the named test still passed. The test is theatre -- fix it.
    NOISY    the mutation broke something other than the named test as well.
             Not a failure, but worth reading: it usually means two assertions
             overlap, and occasionally means the mutation was blunter than
             intended.

An expected failure that a mutation turns into an *unexpected success* also
counts as KILLED: the recorded gap moved, which is exactly the signal wanted.

Usage:
    python3 hack/conformance-mutations.py            # every mutation
    python3 hack/conformance-mutations.py --list
    python3 hack/conformance-mutations.py -k C1      # substring filter on the id

The tree must be clean. It edits tracked files in place and restores them, so a
dirty tree risks losing work -- it refuses rather than guessing.
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


@dataclasses.dataclass(frozen=True)
class Mutation:
    """One removed control, and the test that has to notice."""

    id: str
    path: str
    #: (old, new) applied with str.replace, or a callable taking/returning text.
    edit: tuple[str, str]
    #: Substring matching the test name that must go red.
    kills: str
    #: What the mutation is pretending to be: a plausible bad change, not noise.
    pretext: str
    #: True for a mutation that must NOT be caught. A suite that goes red on a
    #: harmless change is a suite people learn to override, so a no-op edit is
    #: run as a control on the harness itself: SURVIVED is the pass for these
    #: and KILLED is the failure. One today, B1-denylist-rule.
    must_survive: bool = False


MUTATIONS: list[Mutation] = [
    # ---- A. Authority ---------------------------------------------------
    Mutation(
        "A3-inject-auth",
        "agents/platform/scripts/session_kv_server.py",
        ('"/sessions/{session_id}/inject", dependencies=[Depends(verify_api_key)])',
         '"/sessions/{session_id}/inject")'),
        "test_A3_the_session_inject_endpoint_authenticates_its_caller",
        "drop the route's auth dependency, restoring the unauthenticated "
        "prompt-injection endpoint gke-labs/kube-agents#616 closed -- the exact "
        "state this assertion recorded as a known violation until main fixed it",
    ),
    Mutation(
        "A3-impersonation-flags",
        "agents/platform/scripts/command_policy.py",
        ('{"--as", "--as-group", "--as-uid", "--as-user-extra", "--impersonate-service-account"}',
         '{"--as-group", "--as-uid", "--as-user-extra", "--impersonate-service-account"}'),
        "test_A3_rejects_caller_supplied_as",
        "drop the plain --as, keeping the others -- the shape a careless "
        "refactor of a set literal takes",
    ),
    Mutation(
        "A3-kuberc",
        "agents/platform/scripts/command_policy.py",
        ('        if name == "--kuberc":\n            return "--kuberc"\n',
         '        if name == "--kuberc-disabled":\n            return "--kuberc"\n'),
        "test_A3_rejects_kuberc",
        "neuter the dedicated kuberc check; the flag stays in "
        "_KUBECTL_IDENTITY_FLAGS, so this tests whether the second guard holds",
    ),
    Mutation(
        "A3-attached-shorthand",
        "agents/platform/scripts/command_policy.py",
        ('        if token.startswith("-s") and token != "-s":\n            return "-s"\n', ""),
        "test_A3_rejects_attached_shorthand_server",
        "remove the attached-shorthand clause, restoring the exact-token "
        "matching the slice 2a Critical defeated",
    ),
    Mutation(
        "A3-kubectl-kuberc-env",
        "agents/platform/scripts/credential_proxy.py",
        ('"KUBECTL_KUBERC": "false"', '"KUBECTL_KUBERC_UNUSED": "false"'),
        "test_A3_default_path_kuberc_is_disabled",
        "rename the env var while 'tidying', leaving the default-path kuberc "
        "feature on and the protection resting on mount geometry alone",
    ),
    Mutation(
        "A3-server-flag",
        "agents/platform/scripts/command_policy.py",
        ('        "-s", "--server",\n        "--token", "--user", "--username", "--password",',
         '        "--token", "--user", "--username", "--password",'),
        "test_A3_rejects_credential_redirection",
        "drop --server from the identity set -- it is also in "
        "_KUBECTL_FLAGS_WITH_VALUE, so it still parses and looks handled",
    ),
    Mutation(
        "A1-refusal-content",
        "agents/platform/scripts/command_policy.py",
        ('                "Identity and API server address belong to the broker. Remove "',
         '                f"Identity belongs to the broker; {argv} was refused. Remove "'),
        "test_A1_a_refusal_names_no_caller_supplied_value",
        "make the refusal more helpful by naming what was refused -- the "
        "obvious improvement that turns a denial into an oracle",
    ),
    Mutation(
        "A4-operator-escalate",
        "k8s-operator/config/rbac/role.yaml",
        ("    resourceNames:\n      - view\n", ""),
        "test_A4_the_operator_cannot_escalate",
        "remove the resourceNames bound on `bind`, so the operator can attach "
        "any existing ClusterRole to an agent",
    ),
    Mutation(
        "A1-refusal-emptied",
        "agents/platform/scripts/command_policy.py",
        ('                message=(\n'
         '                    "Identity and API server address belong to the broker. Remove "\n'
         '                    "--server, --token, --user, --client-certificate, "\n'
         '                    "--insecure-skip-tls-verify and the other credential flags to "\n'
         '                    "use the cluster and identity the proxy configured."\n'
         '                ),\n',
         '                message="",\n'),
        "test_A1_a_refusal_names_the_rule_that_fired",
        "collapse a refusal onto its rule id -- the cheapest way to satisfy "
        "A1's no-caller-supplied-byte bound is to stop saying anything, and an "
        "agent handed an empty body cannot tell policy from an unreachable "
        "cluster",
    ),
    Mutation(
        "A3-gcloud-flags-file",
        "agents/platform/scripts/command_policy.py",
        ('        if name == "--flags-file":\n            return "--flags-file"\n',
         '        if name == "--flags-file-disabled":\n            return "--flags-file"\n'),
        "test_A3_rejects_gcloud_flags_file",
        "neuter the dedicated flags-file check the way A3-kuberc does. Unlike "
        "--kuberc there is no second guard: the command survives only because "
        "the flag's arity is unknown, so it is refused for the wrong reason and "
        "becomes allowed the day _GCLOUD_FLAGS_WITH_VALUE learns about it",
    ),
    Mutation(
        "A3-attached-shorthand-overreach",
        "agents/platform/scripts/command_policy.py",
        ('        if token.startswith("-s") and token != "-s":\n            return "-s"\n',
         '        if token.startswith("-") and token.lstrip("-").startswith("s") '
         'and token != "-s":\n            return "-s"\n'),
        "test_A3_the_attached_shorthand_rule_does_not_overreach",
        "the looser spelling the docstring warns against -- strip the dashes, "
        "test for a leading s. Every -sVALUE is still refused, so the shorthand "
        "test stays green while --sort-by, --since and --selector become "
        "refusals, which is how a control gets switched off in production",
    ),
    Mutation(
        "A4-chart-bind-unpinned",
        "charts/kube-agents/templates/operator-rbac.yaml",
        ("  - apiGroups:\n      - rbac.authorization.k8s.io\n    resourceNames:\n"
         "      - view\n    resources:\n      - clusterroles\n    verbs:\n      - bind\n",
         "  - apiGroups:\n      - rbac.authorization.k8s.io\n    resources:\n"
         "      - clusterroles\n    verbs:\n      - bind\n"),
        "test_A4_the_chart_grants_the_same_ceiling_as_the_kustomize_role",
        "the chart twin of A4-operator-escalate, hand-edited inside the "
        "generated block instead of regenerated: the kustomize role still reads "
        "correct and only Helm installs get an unrestricted bind",
    ),
    Mutation(
        "A4-inject-assertion-renamed",
        "tests/conformance/test_A_authority.py",
        ("    def test_A3_the_session_inject_endpoint_authenticates_its_caller(self) -> None:",
         "    def test_A3_the_inject_endpoint_authenticates_its_caller(self) -> None:"),
        "test_A4_triggering_is_covered_by_the_A3_inject_finding",
        "shorten an over-long test name in a tidy-up. A4's triggering clause has "
        "no assertion of its own -- it looks its coverage up by qualname -- so a "
        "rename uncovers the invariant without deleting a line of assertion",
    ),
    Mutation(
        "A2-ceiling-test-renamed",
        "tests/conformance/test_C_enforcement.py",
        ("    def test_C5_no_minted_role_grants_a_write_verb(self) -> None:",
         "    def test_C5_no_minted_role_grants_write_verbs(self) -> None:"),
        "test_A2_the_agent_ceiling_half_of_the_intersection_is_asserted",
        "rename the minted-RBAC ceiling test. A2 has no mechanism of its own to "
        "assert, so it borrows C5's assertion by name; the borrow is what breaks "
        "first, and it has to break loudly or A2 falls off the map",
    ),
    # ---- B. The write path ----------------------------------------------
    Mutation(
        "B1-read-only-verbs",
        "agents/platform/scripts/command_policy.py",
        ('        ("get",),\n', '        ("get",),\n        ("delete",),\n'),
        "test_B1_kubectl_write_verbs_are_refused",
        "add delete to the read allowlist -- the single-line change an "
        "operator makes to unblock a skill",
    ),
    Mutation(
        "B1-image-gate",
        "deploy/docker/Dockerfile",
        ("unexpected credential-aware CLI in sandbox image", "sandbox image note"),
        "test_B1_the_sandbox_image_ships_no_credentialed_cli",
        "reword the build gate's message, which is what the assertion anchors "
        "on -- checks that the anchor is registered and policed",
    ),
    Mutation(
        "B1-image-gate-binaries",
        "deploy/docker/Dockerfile",
        ("for binary in gcloud kubectl gh git;", "for binary in gcloud kubectl;"),
        "test_B1_the_sandbox_image_ships_no_credentialed_cli",
        "shorten the gate's binary list, the plausible edit when one of them "
        "is legitimately needed at build time",
    ),
    Mutation(
        "B1-denylist-rule",
        "k8s-operator/internal/controller/platformagent_manifests.go",
        ('{"id":"gcp.access-token-disclosure"', '{"id":"gcp.access-token-disclosure-XX"'),
        "test_B1_the_shipped_denylist_refuses_credential_disclosure",
        "renames a rule id without touching its pattern, so nothing is actually "
        "weakened. A control on the harness: the suite must not go red on a "
        "rename, or it becomes a suite people learn to override.",
        must_survive=True,
    ),
    Mutation(
        "B2-automerge",
        ".github/workflows/validate.yml",
        ("jobs:", "jobs:\n  merge:\n    runs-on: ubuntu-latest\n    steps:\n"
                  "      - run: gh pr merge --auto --squash \"$NUMBER\"\n"),
        "test_B2_no_workflow_approves_or_merges",
        "add an auto-merge job, which is the thing B2 exists to forbid",
    ),
    Mutation(
        "B4-workflow-run-gate",
        ".github/workflows/autopush-redeploy-agent.yml",
        ("github.event.workflow_run.head_branch == 'main'", "true"),
        "test_B4_every_workflow_run_deploy_gates",
        "drop the branch predicate while debugging a deploy, which is when it "
        "actually gets dropped",
    ),
    Mutation(
        "B4-pull-request-target-checkout",
        ".github/workflows/auto_request_review.yml",
        ("    steps:", "    steps:\n      - uses: actions/checkout@v4\n        with:\n"
                        "          ref: ${{ github.event.pull_request.head.sha }}"),
        "test_B4_no_pull_request_target_workflow_checks_out",
        "check out the PR head in a pull_request_target workflow -- arbitrary "
        "code execution with the base repository's token",
    ),
    Mutation(
        "B6-codeowners-bot",
        "examples/gitops-repo/CODEOWNERS.example",
        ("@your-org/security", "@kube-agents-bot[bot]"),
        "test_B6_the_gitops_template_names_no_automation_identity",
        "name an automation identity as a code owner, defeating the one rule "
        "a GitHub App cannot satisfy",
    ),
    Mutation(
        "B1-read-path-narrowed",
        "agents/platform/scripts/command_policy.py",
        ('        # Writes a kubeconfig in the sidecar and nothing in the cloud. It is\n'
         '        # also how a Cluster Agent points itself at its target cluster, so\n'
         '        # refusing it would break the read path this module is protecting.\n'
         '        ("container", "clusters", "get-credentials"),\n',
         ""),
        "test_B1_ordinary_reads_still_work",
        "harden the allowlist by dropping the one entry with `credentials` in "
        "its name. The over-strict direction, which costs an operator the whole "
        "posture rather than one command -- the gate that gets globally "
        "disabled a week later",
    ),
    Mutation(
        "B1-gcloud-group-prefix",
        "agents/platform/scripts/command_policy.py",
        ('        ("container", "node-pools", "describe"),\n'
         '        ("container", "node-pools", "list"),\n',
         '        ("container", "node-pools"),\n'),
        "test_B1_gcloud_write_commands_are_refused",
        "collapse two adjacent entries onto their common prefix while tidying "
        "the list -- _gcloud_is_read_only matches on prefix, so the group entry "
        "allows every verb beneath it, `delete` included",
    ),
    Mutation(
        "B2-second-pull-requests-write",
        ".github/workflows/conformance.yml",
        ("permissions:\n  contents: read\n\njobs:\n  conformance:\n",
         "permissions:\n  contents: read\n  pull-requests: write\n\njobs:\n  conformance:\n"),
        "test_B2_no_workflow_grants_a_bot_the_ability_to_approve",
        "let the conformance job post its findings as a pull-request comment. "
        "The scope that buys a comment is the scope that buys an approval, on a "
        "workflow that runs on every pull_request",
    ),
    Mutation(
        "B3-apply-read-verb",
        "agents/platform/scripts/command_policy.py",
        ('        ("get",),\n', '        ("apply",),\n        ("get",),\n'),
        "test_B3_the_agent_cannot_reach_the_admission_policy_through_kubectl",
        "add apply so a manifest-generation skill can preview with "
        "--dry-run=server. The verb is allowed whatever follows it, and what "
        "follows it here is the ClusterRoleBinding that grants the agent write. "
        "NOISY against B1 by construction: B3's corpus is refused by the same "
        "verb allowlist, with no resource-aware layer between them",
    ),
    Mutation(
        "B4-fourth-contents-write-holder",
        ".github/workflows/chart-release.yml",
        ("      contents: read\n      packages: write\n",
         "      contents: write\n      packages: write\n"),
        "test_B4_contents_write_is_confined_to_the_release_path",
        "give the chart publisher contents: write so it can cut a GitHub "
        "release alongside the OCI push -- a fourth holder of the credential "
        "that can push to this repository, added in a one-word diff",
    ),
    Mutation(
        "B6-guarded-path-unowned",
        "examples/gitops-repo/CODEOWNERS.example",
        ("\n# Admission policies (the security backstop itself)\n"
         "/policy/                          @your-org/security\n",
         "\n"),
        "test_B6_every_guarded_path_in_the_template_has_an_owner",
        "drop the rule for the one directory nobody edits often, so the ruleset "
        "requiring code-owner review on /policy/ requires review from nobody "
        "and the admission backstop merges unreviewed",
    ),
    # ---- C. Enforcement --------------------------------------------------
    Mutation(
        "C1-git-ext-transport",
        "agents/platform/scripts/credential_proxy.py",
        ('"GIT_ALLOW_PROTOCOL": "https",', '"GIT_ALLOW_PROTOCOL": "https:ext",'),
        "test_C1_git_in_the_broker_cannot_execute_arbitrary_code",
        "re-admit the ext:: transport, which is the whole of the RCE: "
        "`git clone \'ext::sh -c <cmd>\'` runs <cmd> in the credential holder. "
        "The one-word widening is the shape the real regression would take",
    ),
    Mutation(
        "C1-share-process-namespace",
        "k8s-operator/internal/testing/testdata/platform/expected/platformagent.yaml",
        ("      serviceAccountName: kubeagents-platform-agent",
         "      shareProcessNamespace: true\n      serviceAccountName: kubeagents-platform-agent"),
        "test_C1_the_process_namespace_is_never_shared",
        "a golden fixture regenerated after someone set the field, which is "
        "how it would actually arrive",
    ),
    Mutation(
        "C1-uid-collapse",
        "k8s-operator/internal/testing/testdata/platform/expected/platformagent.yaml",
        ("          runAsUser: 10001", "          runAsUser: 10000"),
        "test_C1_the_agent_and_the_broker_run_as_different_users",
        "collapse the broker onto the sandbox UID, restoring the procfs and "
        "socket reach the split removed",
    ),
    Mutation(
        "C1-socket-umask",
        "agents/platform/scripts/credential_proxy.py",
        ("previous_umask = os.umask(0o177)", "previous_umask = os.umask(0o022)"),
        "test_C1_the_broker_backend_socket_is_bound_private",
        "widen the umask the socket is bound under -- the slice 2b near-miss, "
        "where a umask added for the shared PVC reached the socket",
    ),
    Mutation(
        "C1-shell-true",
        "agents/platform/scripts/credential_proxy.py",
        ("            start_new_session=True,", "            start_new_session=True,\n            shell=True,"),
        "test_C1_the_executor_never_reaches_a_shell",
        "interpose a shell, which is what makes `;` and `#` live again",
    ),
    Mutation(
        "C1-executable-allowlist",
        "agents/platform/scripts/credential_proxy.py",
        ('ALLOWED_EXECUTABLES = ("gcloud", "kubectl", "gh", "git")',
         'ALLOWED_EXECUTABLES = ("gcloud", "kubectl", "gh", "git", "sh")'),
        "test_C1_the_executor_refuses_an_executable_it_does_not_ship",
        "add sh to the allowlist, giving a compound command somewhere to land",
    ),
    # C1-egress-whole-internet is retired, not lost. It injected the exact
    # construction slice 2b 1.3 refused -- `0.0.0.0/0 except metadata`, which
    # adds the internet rather than subtracting an address -- into the
    # allowlist golden, and killed test_C1_the_rendered_egress_policy*.
    #
    # Both of those assertions are known violations as of
    # gke-labs/kube-agents#676: platformagent-gateway-netpol selects the same
    # pods and already allows the whole internet and the metadata addresses,
    # so the tests fail before the mutation is applied and applying it changes
    # nothing. A mutation against an expected failure can only report
    # SURVIVED, which reads as "the test is theatre" and is the wrong
    # diagnosis. A known violation is verified by its precondition instead.
    #
    # Restore this the day the C1 decorators come off.
    Mutation(
        "C1-cidr-guard-inert",
        "k8s-operator/internal/controller/platformagent_egress_policy.go",
        ("\tif reason := ipv4MappedRefusal(prefix, cidr); reason != \"\" {\n\t\treturn reason\n\t}\n", ""),
        "test_C1_every_operator_supplied_cidr_reaches_the_refusal_guards",
        "rename the 4-in-6 guard so every call site misses it; Go would not "
        "compile, but the point is that the conformance suite says so first",
    ),
    Mutation(
        "C2-unknown-flag-fail-open",
        "agents/platform/scripts/command_policy.py",
        ("            if name not in _KUBECTL_FLAGS_WITH_VALUE and name not in _KUBECTL_BOOLEAN_FLAGS:\n"
         "                return None, name\n",
         "            if name not in _KUBECTL_FLAGS_WITH_VALUE and name not in _KUBECTL_BOOLEAN_FLAGS:\n"
         "                index += 1\n                continue\n"),
        "test_C2_an_unparseable_argv_is_refused",
        "skip unknown flags instead of refusing -- fail open, and the reason "
        "the module enumerates arity rather than allowlisting flags",
    ),
    Mutation(
        "C2-read-only-default",
        "agents/platform/scripts/credential_proxy.py",
        ('return os.getenv("CREDENTIAL_PROXY_ENFORCE_READ_ONLY", "true").strip().lower() != "false"',
         'return os.getenv("CREDENTIAL_PROXY_ENFORCE_READ_ONLY", "true").strip().lower() == "true"'),
        "test_C2_the_read_only_gate_survives_a_typo",
        "compare for truth rather than against falsehood -- looks equivalent, "
        "and disarms the gate on every typo",
    ),
    Mutation(
        "C2-external-key-default",
        "agents/platform/scripts/credential_proxy.py",
        ('external_key = os.getenv("API_SERVER_EXTERNAL_KEY", "").strip()',
         'external_key = os.getenv("API_SERVER_EXTERNAL_KEY", "dev").strip()'),
        "test_C2_the_agent_api_proxy_refuses_to_start_without_its_key",
        "give the external key a development default, which is how the "
        "loopback sentinel got there in the first place",
    ),
    Mutation(
        "C3-policy-reads-a-file",
        "agents/platform/scripts/command_policy.py",
        ('    for token in argv[1:]:\n        name, _, _ = token.partition("=")\n        if name == "--kuberc":',
         '    for token in argv[1:]:\n        name, _, value = token.partition("=")\n'
         '        if name == "--kuberc" and value and open(value):\n'
         '            pass\n        if name == "--kuberc":'),
        "test_C3_the_policy_module_imports_nothing_that_can_read",
        "check whether the kuberc file exists before refusing, the "
        "helpful-looking change that reintroduces a rewrite-after-check race",
    ),
    Mutation(
        "C3-log-sanitiser",
        "agents/platform/scripts/credential_proxy.py",
        ("filtered = ''.join(c for c in s if unicodedata.category(c) not in ('Cc', 'Cf', 'Zl', 'Zp'))",
         "filtered = s"),
        "test_C3_untrusted_output_cannot_forge_a_log_line",
        "stop stripping control characters, so tool output can forge a record",
    ),
    Mutation(
        "C4-unpinned-action",
        ".github/workflows/prettier.yml",
        ("actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1",
         "actions/checkout@v7"),
        "test_C4_every_third_party_action_is_pinned_to_a_commit",
        "float one action back to a tag, which a Dependabot conflict "
        "resolution can produce by hand",
    ),
    Mutation(
        "C4-base-image-digest",
        "tags.env",
        ("@sha256:16788311e2fa3035456bdc1bafb8ec2b1777db64ebf020af9bb7eb73c3712c9e", ""),
        "test_C4_the_agent_base_image_is_pinned_by_digest",
        "drop the digest and keep the tag, which reads as equivalent",
    ),
    Mutation(
        "C5-minted-write-verb",
        "k8s-operator/internal/testing/testdata/platform/expected/platformagent.yaml",
        ("      - get\n      - list\n", "      - get\n      - list\n      - patch\n"),
        "test_C5_no_minted_role_grants_a_write_verb",
        "add patch to a minted explorer role, the change a feature request for "
        "annotating resources produces",
    ),
    Mutation(
        "C5-bind-to-edit",
        "k8s-operator/internal/testing/testdata/platform/expected/platformagent.yaml",
        ("kind: ClusterRole\n  name: kubeagents:minimal:kubeagents-system:platformagent\n",
         "kind: ClusterRole\n  name: edit\n"),
        "test_C5_the_agent_is_bound_to_no_write_capable_builtin_role",
        "bind the agent to `edit` while leaving every minted rule read-only, "
        "which the verb-level assertion alone cannot see. Retargeted from "
        "`name: view`: main replaced the built-in binding with a purpose-built "
        "kubeagents:minimal ClusterRole, so there is no `view` left to swap -- "
        "the roleRef is still the thing that has to be attacked",
    ),
    Mutation(
        "C5-reaper-eats-guardrail",
        "k8s-operator/internal/controller/platformagent_controller.go",
        ('&corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{Name: agent.Name + "-sandbox", Namespace: agent.Namespace}},',
         '&corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{Name: agent.Name + "-sandbox", Namespace: agent.Namespace}},\n'
         '\t\t&networkingv1.NetworkPolicy{ObjectMeta: metav1.ObjectMeta{Name: agent.Name + "-sandbox-metadata-deny", Namespace: agent.Namespace}},'),
        "test_C5_the_controller_does_not_reap_the_metadata_deny_guardrail",
        "restore the reaper's reach over the guardrail -- slice 2b 1.5, "
        "verbatim",
    ),
    Mutation(
        "C5-admission-binding-drift",
        "k8s-operator/config/admission/agent-rbac-policy.yaml",
        ("  policyName: kube-agents-agent-readonly", "  policyName: prefixed-kube-agents-agent-readonly"),
        "test_C5_the_admission_binding_names_a_policy_that_exists",
        "the kustomize namePrefix outcome slice 2b 1.2 caught: both objects "
        "exist, the binding points at nothing, and kubectl get looks right",
    ),
    Mutation(
        "C5-admission-fail-open",
        "k8s-operator/config/admission/agent-rbac-policy.yaml",
        ("  failurePolicy: Fail", "  failurePolicy: Ignore"),
        "test_C5_the_admission_policy_fails_closed",
        "the one-line edit B3 names: 'unblock apply during upgrade window'",
    ),
    Mutation(
        "C1-sandbox-back-in-the-broker-pod",
        "k8s-operator/internal/testing/testdata/platform/expected/platformagent-split-broker.yaml",
        ("      containers:\n        - command:\n"
         "            - /usr/local/bin/start-services\n",
         "      containers:\n"
         "        - image: ghcr.io/gke-labs/kube-agents/platform-agent:v9.9.9\n"
         "          imagePullPolicy: IfNotPresent\n"
         "          name: platform-agent\n"
         "          securityContext:\n"
         "            allowPrivilegeEscalation: false\n"
         "            readOnlyRootFilesystem: true\n"
         "            runAsUser: 10000\n"
         "        - command:\n"
         "            - /usr/local/bin/start-services\n"),
        "test_C1_the_split_broker_pod_holds_no_sandbox_container",
        "a golden fixture regenerated after the sandbox was co-located back "
        "into the broker Pod to share the workspace over localhost. Distinct "
        "UIDs are kept, so every UID assertion still passes and only the "
        "network namespace the split exists to separate is shared again",
    ),
    Mutation(
        "C2-phase-two-skips-unknown-flag",
        "agents/platform/scripts/command_policy.py",
        ("            # Stop on unknown flags (arity unknown, could hide the subcommand).\n"
         "            if name not in _KUBECTL_FLAGS_WITH_VALUE and name not in _KUBECTL_BOOLEAN_FLAGS:\n"
         "                break\n",
         "            # Unknown command-specific flags are boolean far more often\n"
         "            # than not, so skip rather than stop.\n"
         "            if name not in _KUBECTL_FLAGS_WITH_VALUE and name not in _KUBECTL_BOOLEAN_FLAGS:\n"
         "                index += 1\n"
         "                continue\n"),
        "test_C2_an_unknown_flag_cannot_swallow_a_write_subcommand",
        "make phase 2 skip an unrecognised command-specific flag instead of "
        "stopping at it -- the symmetry a reader expects with phase 1's loop. "
        "`rollout --someflag status restart web` then reads as `rollout status`",
    ),
    Mutation(
        "C2-cluster-info-dump-allowed",
        "agents/platform/scripts/command_policy.py",
        ('        ("cluster-info", "dump"),\n', ""),
        "test_C2_cluster_info_dump_is_refused_by_both_of_its_guards",
        "empty the refused-subcommand set on the grounds that a dump only "
        "reads. `cluster-info` is allowed alone and evaluate falls back to "
        "verb[:1], so the deletion is silent",
    ),
    Mutation(
        "C2-output-directory-demoted",
        "agents/platform/scripts/command_policy.py",
        ('        "--profile", "--profile-output", "--cache-dir", "--output-directory",\n',
         '        "--profile", "--profile-output", "--cache-dir",\n'),
        "test_C2_cluster_info_dump_is_refused_by_both_of_its_guards",
        "drop --output-directory from a set of kubectl *global* flags because "
        "it belongs to cluster-info -- exactly the tidy-up the comment above it "
        "argues against, and the guard that does not need the verb parse. The "
        "pair with C2-cluster-info-dump-allowed: the test names two guards, so "
        "each is removed on its own",
    ),
    Mutation(
        "C3-policy-opens-the-kuberc-file",
        "agents/platform/scripts/command_policy.py",
        ('    for token in argv[1:]:\n        name, _, _ = token.partition("=")\n'
         '        if name == "--kuberc":\n            return "--kuberc"\n',
         '    import codecs\n\n'
         '    for token in argv[1:]:\n        name, _, value = token.partition("=")\n'
         '        if name == "--kuberc":\n'
         '            if value:\n'
         '                try:\n'
         '                    with codecs.open(value, encoding="utf-8") as preference:\n'
         '                        if "as" not in preference.read():\n'
         '                            return None\n'
         '                except OSError:\n'
         '                    pass\n'
         '            return "--kuberc"\n'),
        "test_C3_the_policy_decision_reads_nothing_but_its_argv",
        "refuse --kuberc only when the file it names actually sets an "
        "impersonation default -- the same helpful-looking check as "
        "C3-policy-reads-a-file, spelled through codecs.open so neither the AST "
        "test's import list nor its builtin-name list is touched. os.stat is "
        "the audit hook's blind spot and an enumerated list is the AST test's; "
        "this is the half only the hook can see",
    ),
    Mutation(
        "C5-tokenreview-gets-subjectaccessreviews",
        "k8s-operator/internal/testing/testdata/platform/expected/platformagent-split-broker.yaml",
        ("    resources:\n      - tokenreviews\n    verbs:\n      - create\n",
         "    resources:\n      - tokenreviews\n      - subjectaccessreviews\n    verbs:\n      - create\n"),
        "test_C5_the_tokenreview_role_is_the_narrowest_form_of_itself",
        "give the broker subjectaccessreviews alongside tokenreviews -- what "
        "binding system:auth-delegator would have handed it in one line, and an "
        "authorization oracle over the whole cluster",
    ),
    Mutation(
        "C5-binds-auth-delegator",
        "k8s-operator/internal/testing/testdata/platform/expected/platformagent-split-broker.yaml",
        ("roleRef:\n  apiGroup: rbac.authorization.k8s.io\n  kind: ClusterRole\n"
         "  name: kubeagents:tokenreview:kubeagents-system:platformagent",
         "roleRef:\n  apiGroup: rbac.authorization.k8s.io\n  kind: ClusterRole\n"
         "  name: system:auth-delegator"),
        "test_C5_no_agent_binding_names_the_auth_delegator_role",
        "bind the built-in system:auth-delegator instead of the minted one-verb "
        "role -- the shortcut every TokenReview how-to recommends. The minted "
        "role is left in place and still narrow, so the rule-level assertions "
        "cannot see it",
    ),
    Mutation(
        "C5-leader-reaches-configmaps",
        "k8s-operator/internal/testing/testdata/platform/expected/platformagent.yaml",
        ("    resources:\n      - pods\n    verbs:\n      - get\n      - patch\n",
         "    resources:\n      - configmaps\n      - pods\n    verbs:\n      - get\n      - patch\n"),
        "test_C5_the_leader_role_stays_confined_to_coordination",
        "add the ConfigMap lock client-go's configmapsleases mode still "
        "supports, which turns a coordination role into a namespace read-write "
        "grant one resource at a time",
    ),
    # ---- D. Accountability ------------------------------------------------
    Mutation(
        "D1-principal-not-logged",
        "agents/platform/scripts/credential_proxy.py",
        ("            _sanitize_for_logging(principal.describe()),", "            \"-\","),
        "test_D1_the_exec_route_records_a_principal",
        "drop the principal from the exec record while refactoring a handler "
        "that does not yet read it",
    ),
    Mutation(
        "D1-hint-not-sanitised",
        "agents/platform/scripts/credential_proxy.py",
        ('safe_hint = _sanitize_for_logging(log_hint) if log_hint else "unknown"',
         'safe_hint = log_hint if log_hint else "unknown"'),
        "test_D1_a_log_hint_cannot_forge_a_record",
        "log the hint raw, which is the state the sanitiser was added to fix",
    ),
    Mutation(
        "D2-workflow-mode",
        "k8s-operator/api/v1alpha1/common_types.go",
        ("type SecuritySpec struct {", "type SecuritySpec struct {\n\tWorkflowMode string `json:\"workflowMode,omitempty\"`"),
        "test_D2_no_direct_apply_mode_exists",
        "add the break-glass field another design document already offers",
    ),
    Mutation(
        "D4-token-never-expires",
        "k8s-operator/internal/testing/testdata/platform/expected/platformagent.yaml",
        ("              expirationSeconds: 3600", "              expirationSeconds: 86400"),
        "test_D4_every_projected_token_expires",
        "stretch the projection to a day to stop a rotation warning",
    ),
    Mutation(
        "D4-audience-dropped",
        "k8s-operator/internal/testing/testdata/platform/expected/platformagent.yaml",
        ("              audience: kubeagents-credential-proxy\n", ""),
        "test_D4_the_broker_token_is_audience_bound",
        "drop the audience, making the broker's token a general-purpose "
        "cluster bearer token and TokenReview a formality",
    ),
    Mutation(
        "D4-secret-becomes-literal",
        "k8s-operator/internal/testing/testdata/platform/expected/platformagent.yaml",
        ("- name: API_SERVER_EXTERNAL_KEY\n              valueFrom:\n                secretKeyRef:\n"
         "                  key: api-key\n                  name: platformagent-secrets",
         "- name: API_SERVER_EXTERNAL_KEY\n              value: hunter2"),
        "test_D4_the_customer_api_key_is_secret_backed",
        "inline the external key as a literal, the way the loopback sentinel "
        "already is",
    ),
    Mutation(
        "D2-read-only-becomes-a-chart-value",
        "charts/kube-agents/values.yaml",
        ("    serviceAccountName: kubeagents-platform-agent\n",
         "    serviceAccountName: kubeagents-platform-agent\n"
         "    # Sets CREDENTIAL_PROXY_ENFORCE_READ_ONLY on the broker. Set to false\n"
         "    # to recover from a bad allowlist without waiting on an image build.\n"
         "    enforceReadOnly: true\n"),
        "test_D2_the_read_only_posture_is_not_a_customer_facing_knob",
        "promote the outage stopgap to a documented chart value, which is how a "
        "global, unscoped, never-expiring autonomy switch actually gets offered "
        "to a customer -- as a helpful comment next to a boolean",
    ),
    Mutation(
        "D5-cross-reference-renamed",
        "tests/conformance/test_C_enforcement.py",
        ("    def test_C3_the_policy_decision_reads_nothing_but_its_argv(self) -> None:",
         "    def test_C3_the_policy_decision_is_a_pure_function_of_argv(self) -> None:"),
        "test_D5_the_enforcement_tier_cannot_be_lowered_by_routing",
        "shorten an over-long test name. D5 owns no control of its own -- its "
        "single assertion is a cross-reference to C3's purity test -- so this "
        "checks the reference is load-bearing rather than decorative, and that "
        "a rename cannot silently empty the invariant",
    ),
    Mutation(
        "D6-switch-renamed-out-from-under-its-name",
        "agents/platform/scripts/credential_proxy.py",
        ('os.getenv("CREDENTIAL_PROXY_ENFORCE_READ_ONLY", "true")',
         'os.getenv("CREDENTIAL_PROXY_READ_ONLY", "true")'),
        "test_D6_the_read_only_switch_is_not_mistaken_for_a_kill_switch",
        "shorten the variable name while tidying. The switch is the only global "
        "control in the product and D6 exists to say what it does not do; a "
        "rename means the documented spelling silently does nothing. NOISY "
        "against C2 by construction -- both invariants read the same call, so "
        "no edit reaches one without the other",
    ),
    Mutation(
        "D3-bucket-marker-tidied-away",
        "tests/conformance/test_D_accountability.py",
        ('"""D3: BUCKET 3 -- no mechanism exists, and a weak test would be worse than none.\n',
         '"""D3: no mechanism exists, and a weak test would be worse than none.\n'),
        "test_D3_is_recorded_as_bucket_three_rather_than_missing",
        "reword a class docstring's opening line, dropping the marker that is "
        "the only thing distinguishing a recorded bucket-3 reason from an "
        "invariant nobody wrote a test for. Harness-class: for bucket 3 the "
        "written reason IS the control, so the suite is the file to mutate",
    ),
    Mutation(
        "D6-bucket-three-exit-criterion-deleted",
        "tests/conformance/test_D_accountability.py",
        ("    What would make this bucket 1: a halt control with a stated N. Then the\n"
         "    assertion is that a halted agent refuses, that the halt survives a restart,\n"
         "    and that setting it does not require touching the agent's own Deployment.\n",
         ""),
        "test_D6_is_recorded_as_bucket_three_rather_than_missing",
        "delete the forward-looking paragraph as speculative, leaving BUCKET 3 "
        "a status with no exit criterion -- the shape in which a gap stops "
        "being a plan and becomes a permanent excuse",
    ),
    # ---- D15 and the harness ----------------------------------------------
    Mutation(
        "D15-guard-normalises",
        "k8s-operator/internal/controller/platformagent_egress_policy.go",
        ("Overlaps(ipv4MappedSpace)", "Contains(ipv4MappedSpace.Addr())"),
        "test_D15_the_guard_refuses_the_ambiguous_form_rather_than_normalising",
        "swap Overlaps for the Contains that produced the finding -- the same "
        "spelling, the same cross-family blind spot",
    ),
    Mutation(
        "D15-executor-absolute-path",
        "agents/platform/scripts/credential_proxy.py",
        ('ALLOWED_EXECUTABLES = ("gcloud", "kubectl", "gh", "git")',
         'ALLOWED_EXECUTABLES = ("gcloud", "kubectl", "gh", "git", "/usr/bin/kubectl")'),
        "test_D15_the_two_layers_agree_on_the_governed_tool",
        "pin kubectl to an absolute path so PATH cannot be shadowed -- a "
        "hardening on its face, and a spelling _GOVERNED_TOOLS matches exactly "
        "and therefore does not govern. `/usr/bin/kubectl delete ns prod` reads "
        "as an ungoverned tool to the policy and as kubectl to the executor",
    ),
    Mutation(
        "D15-kuberc-scan-stops-at-the-verb",
        "agents/platform/scripts/command_policy.py",
        ('    for token in argv[1:]:\n        name, _, _ = token.partition("=")\n'
         '        if name == "--kuberc":\n            return "--kuberc"\n',
         '    for token in argv[1:]:\n        if not token.startswith("-"):\n            break\n'
         '        name, _, _ = token.partition("=")\n'
         '        if name == "--kuberc":\n            return "--kuberc"\n'),
        "test_D15_a_refused_flag_is_refused_wherever_it_appears",
        "stop the kuberc scan at the first bare word, reasoning that a global "
        "flag precedes the verb. cobra does not agree: the post-verb spelling "
        "falls through to the identity check and earns a different rule id, so "
        "the verdict now depends on where the flag sits",
    ),
    Mutation(
        "D15-differential-loses-its-test",
        "tests/conformance/test_A_authority.py",
        ("    def test_A3_rejects_attached_shorthand_server(self) -> None:",
         "    def test_A3_rejects_the_attached_shorthand(self) -> None:"),
        "test_D15_every_known_differential_has_a_test",
        "rename the -shttp:// test. The checklist looks its findings up by "
        "string, which is the only way a differential stops being covered "
        "without a single assertion being deleted",
    ),
    Mutation(
        "D15-readme-closes-the-class",
        "tests/conformance/README.md",
        ("**The class is open.** Four instances now across three slices.",
         "**Four instances now across three slices**, each with a test."),
        "test_D15_the_readme_says_the_class_is_open",
        "rewrite the standing hedge as a coverage claim now that all four "
        "differentials have tests -- the reading the sentence exists to "
        "prevent, and the one a reader of a finished-looking table takes anyway",
    ),
    Mutation(
        "harness-source-moved",
        "agents/platform/scripts/command_policy.py",
        ("def evaluate(", "def evaluate_command("),
        "test_every_anchor_is_still_present",
        "rename the entry point. Nothing here should pass quietly: the "
        "self-check has to be the thing that goes red first",
    ),
    Mutation(
        "harness-mutation-quietly-unhooked",
        "hack/conformance-mutations.py",
        ('"test_C5_the_leader_role_stays_confined_to_coordination",\n'
         '        "add the ConfigMap lock',
         '"test_C5_the_leader_role_stays_bounded",\n'
         '        "add the ConfigMap lock'),
        "test_every_bucket_one_assertion_is_named_by_a_mutation",
        "rename a test and update the mutation's `kills` to something that no "
        "longer matches it. The mutation still runs and still reports a verdict, "
        "so the run stays green-looking while one assertion quietly stops being "
        "attacked -- the exact drift the coverage check exists to catch. The "
        "list is read once at import, so this cannot disturb the run applying it",
    ),
    Mutation(
        "harness-exemption-unargued",
        "tests/conformance/test_harness_selfcheck.py",
        ('            "asserts a property of the ipaddress module: that ::ffff:0.0.0.0/96 "\n'
         '            "unmaps to 0.0.0.0/0 and contains the metadata address. It holds the "\n'
         '            "premise the Go guard rests on as an executable statement rather "\n'
         '            "than a comment, and reads no repository artifact, so any edit that "\n'
         '            "reddens it is an edit to the assertion. The controls the premise "\n'
         '            "underwrites are mutated: D15-guard-normalises and C1-cidr-guard-inert."',
         '            "no in-repo control."'),
        "test_the_exemptions_are_argued_rather_than_listed",
        "shorten an exemption's reason to a note. An exemption list is the only "
        "way out of the coverage floor, so it stays honest exactly as long as "
        "entering it costs an argument",
    ),
    Mutation(
        "harness-fixture-emptied",
        "k8s-operator/internal/testing/testdata/platform/expected/platformagent.yaml",
        ("\nkind: ClusterRole\n", "\nkind: ClusterRoleXX\n"),
        "test_the_golden_fixtures_render_more_than_a_stub",
        "corrupt a fixture's object kinds, which would turn every assertion "
        "that iterates it vacuously green",
    ),
]


def _purge_bytecode() -> None:
    """Delete every __pycache__ the suite could import from.

    CPython decides a .pyc is current by comparing the source's mtime *in whole
    seconds* and its size. A mutation that preserves file size -- renaming a
    symbol to another of the same length is the obvious one -- and is restored
    by `git checkout` inside the same second produces a source file that is
    byte-identical to HEAD and a cache entry compiled from the mutated text,
    with no way to tell them apart. That leaks into every subsequent mutation:
    the baseline is no longer the tree, and a later mutation can be credited
    with a kill that belongs to the leftover.

    Found the hard way -- see overnight-b/findings.md.
    """
    for cache in REPO.rglob("__pycache__"):
        if ".git" in cache.parts:
            continue
        for entry in cache.glob("*.pyc"):
            entry.unlink()


def _run_suite() -> tuple[set[str], set[str]]:
    """(failed test names, unexpectedly-successful test names)."""
    _purge_bytecode()
    process = subprocess.run(
        # -B: write no bytecode at all, so nothing survives to go stale. The
        # purge above covers caches written before this ran.
        [sys.executable, "-B", "tests/conformance/run.py"],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=300,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    output = process.stdout + process.stderr
    failed = set(re.findall(r"^(?:FAIL|ERROR): (\S+)", output, re.MULTILINE))
    # An expected failure that starts passing is reported as an unexpected
    # success, which is also the suite noticing the mutation.
    unexpected = set(re.findall(r"^UNEXPECTED SUCCESS: (\S+)", output, re.MULTILINE))
    if "unexpected successes" in output:
        unexpected |= set(re.findall(r"(\S+) \(.*\) \.\.\. unexpected success", output))
    return failed, unexpected


def _git_clean() -> bool:
    return not subprocess.run(
        ["git", "status", "--porcelain"], cwd=REPO, capture_output=True, text=True
    ).stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true")
    parser.add_argument("-k", "--filter", default="")
    arguments = parser.parse_args()

    selected = [m for m in MUTATIONS if arguments.filter in m.id]
    if arguments.list:
        for mutation in selected:
            print(f"{mutation.id:38} {mutation.path}")
        return 0

    if not _git_clean():
        print(
            "the working tree is dirty. This edits tracked files in place and "
            "restores them with git checkout; refusing rather than risking it.",
            file=sys.stderr,
        )
        return 2

    baseline_failed, baseline_unexpected = _run_suite()
    if baseline_failed or baseline_unexpected:
        print(f"baseline is not green: {sorted(baseline_failed | baseline_unexpected)}")
        return 2
    print(f"baseline green. {len(selected)} mutations.\n")

    verdicts = []
    for mutation in selected:
        path = REPO / mutation.path
        original = path.read_text()
        old, new = mutation.edit
        if old not in original:
            verdicts.append((mutation, "STALE", []))
            print(f"STALE    {mutation.id}: the text it edits is not in {mutation.path}")
            continue
        try:
            path.write_text(original.replace(old, new, 1))
            failed, unexpected = _run_suite()
        finally:
            subprocess.run(["git", "checkout", "--", mutation.path], cwd=REPO, check=True)

        noticed = failed | unexpected
        killers = {name for name in noticed if mutation.kills in name}
        others = sorted(name.split(".")[-1] for name in noticed - killers)
        if mutation.must_survive:
            verdict = "OVERSHOT" if noticed else "SURVIVED (expected)"
        elif killers:
            verdict = "NOISY" if others else "KILLED"
        else:
            verdict = "SURVIVED"
        verdicts.append((mutation, verdict, others))
        detail = f"  (also: {', '.join(others[:3])}{'…' if len(others) > 3 else ''})" if others else ""
        print(f"{verdict:8} {mutation.id}{detail}")

    # Re-baseline. Every mutation is restored in a `finally`, so the tree is
    # clean by construction -- but "the tree is clean" and "the suite is back
    # where it started" are different claims, and the second is the one the
    # verdicts above rest on. A run that ends dirty has been scoring later
    # mutations against a polluted baseline.
    closing_failed, closing_unexpected = _run_suite()
    leaked = sorted(closing_failed | closing_unexpected)
    if leaked:
        print(
            f"\nBASELINE POLLUTED: the suite is not green after restoring "
            f"every mutation: {leaked}. Verdicts after the mutation that "
            f"caused it are not trustworthy."
        )

    survived = [m.id for m, verdict, _ in verdicts if verdict in ("SURVIVED", "OVERSHOT")]
    stale = [m.id for m, verdict, _ in verdicts if verdict == "STALE"]
    print(
        f"\nkilled={sum(1 for _, v, _ in verdicts if v == 'KILLED')} "
        f"noisy={sum(1 for _, v, _ in verdicts if v == 'NOISY')} "
        f"survived={len(survived)} stale={len(stale)}"
    )
    if survived:
        print(
            f"UNRESOLVED: {survived} -- a SURVIVED mutation means the test does "
            f"not test what it claims to; an OVERSHOT one means the suite goes "
            f"red on a change that weakens nothing"
        )
    if stale:
        print(f"STALE: {stale} -- the mutation no longer applies; rewrite it")
    return 1 if survived or stale or leaked else 0


if __name__ == "__main__":
    raise SystemExit(main())
