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
    STALE    the file is gone, or the `old` text is not in it, so nothing was
             mutated and nothing was proved. Reads like silence -- always a
             bug in the row, usually a renamed file, a pin, or a neighbouring
             line that moved.
    OVERSHOT a `must_survive` control was caught: the suite goes red on a
             change that weakens nothing.
    SURVIVED (expected)
             a `must_survive` control was not caught, which is its pass.
             Thirteen rows on every run print this, and the SURVIVED line
             above is exactly the wrong reading of them: the suite staying
             green is the property they assert.
    BASELINE POLLUTED
             not a per-row verdict but a line printed after the run: the
             suite is not green once every mutation has been restored, so
             every verdict after whatever caused it is untrustworthy. It
             changes the exit code, and the usual cause is stale bytecode --
             see `_purge_bytecode`.

The summary line's `survived=` count is survivors *and* OVERSHOT controls,
because both are the same news: a row whose verdict is not what it was
written to be.

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
    #: (old, new), applied as a single str.replace of the first occurrence.
    edit: tuple[str, str]
    #: Substring matching the test name that must go red.
    kills: str
    #: What the mutation is pretending to be: a plausible bad change, not noise.
    pretext: str
    #: True for a mutation that must NOT be caught. A suite that goes red on a
    #: harmless change is a suite people learn to override, so a no-op edit is
    #: run as a control on the harness itself: SURVIVED is the pass for these
    #: and KILLED is the failure. Thirteen today, which `--list` is the
    #: authority on rather than this comment -- it named ten on 2026-09-19
    #: when there were eleven, and one of the ten by an id no row had:
    #: A3-fastpath-redundant,
    #: B1-denylist-rule,
    #: B4-pull-request-target-api-gh-pr-interposed-flag-write,
    #: B4-pull-request-target-api-web-link-comment,
    #: B4-pull-request-target-api-gh-issue-comment-write,
    #: B4-pull-request-target-api-gh-argv-vector-write,
    #: B4-pull-request-target-run-env-ordinary-shell,
    #: B4-pull-request-target-shell-ordinary,
    #: B4-pull-request-target-checkout-ref-env-case,
    #: B4-pull-request-target-run-clone-bare,
    #: B4-pull-request-target-run-log-m-flags,
    #: B4-pull-request-target-run-runner-repository-name, and
    #: B4-pull-request-target-container-pinned-image.
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
        # The fast-path `-sVALUE` test and the cluster walk are mutually
        # redundant for the attached spelling — deleting either alone weakens
        # nothing, measured by execution. The kill is therefore the walk's own
        # return, which is the only coverage the clustered spelling
        # (`-As http://host`, boolean then s) has; the corpus carries that
        # spelling so this deletion goes red.
        "A3-attached-shorthand",
        "agents/platform/scripts/command_policy.py",
        ('                if character == "s":\n                    return "-s"\n', ""),
        "test_A3_rejects_attached_shorthand_server",
        "drop the cluster walk's server detection while simplifying the loop; "
        "-As http://host then reaches the server flag unrefused",
    ),
    Mutation(
        # The other half of the redundancy, pinned as deliberate: deleting the
        # `-sVALUE` fast-path must leave the suite green, because the cluster
        # walk catches every spelling the corpus carries. If this ever goes
        # OVERSHOT, the walk lost coverage and the fast-path became the only
        # thing standing — which is worth knowing loudly.
        "A3-fastpath-redundant",
        "agents/platform/scripts/command_policy.py",
        ('        if token.startswith("-s") and token != "-s":\n            return "-s"\n', ""),
        "test_A3_rejects_attached_shorthand_server",
        "remove the redundant fast-path; the cluster walk covers it",
        must_survive=True,
    ),
    Mutation(
        "A3-kubectl-kuberc-env",
        "agents/platform/scripts/credential_proxy.py",
        # Indented to pin the sandbox environment the test reads. The
        # unindented spelling occurs first, in `_GIT_PROBE_ENVIRONMENT`,
        # and a one-shot replace aimed there proves nothing.
        ('            "KUBECTL_KUBERC": "false",',
         '            "KUBECTL_KUBERC_UNUSED": "false",'),
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
        # The original unpinned bind's resourceNames. #387 removed the bind
        # rule outright and this edit became "grant escalate on the RBAC rule
        # the operator holds full CRUD through"; the auth callout has since
        # brought bind back, scoped to one name, and A4-bind-unscoped below
        # is the mutation that covers the scoping. This one stays on escalate.
        "A4-operator-escalate",
        "k8s-operator/config/rbac/role.yaml",
        ("      - roles\n    verbs:\n      - create\n",
         "      - roles\n    verbs:\n      - create\n      - escalate\n"),
        "test_A4_the_operator_cannot_escalate_its_own_grants",
        "grant the operator escalate so a reconcile can widen a role in place "
        "-- the ceiling in C5 becomes advisory",
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
        # The bind grant is only bounded while it names resources. Stripping
        # resourceNames leaves a rule that lets the operator attach ANY
        # existing ClusterRole -- cluster-admin included -- to any subject it
        # can write a binding for, which is escalate without the verb.
        "A4-bind-unscoped",
        "k8s-operator/config/rbac/role.yaml",
        ("    resourceNames:\n      - system:auth-delegator\n", ""),
        "test_A4_the_operator_cannot_escalate_its_own_grants",
        "drop the resourceNames bound on the operator's bind grant so it can "
        "attach any role to any subject -- escalate without the verb",
    ),
    Mutation(
        # The generated block is gated byte-for-byte by `make chart-check`, so
        # the interesting place to hide a grant is just past its end marker:
        # chart-sync will not touch it and a parser that reads only the block
        # never sees it.
        "A4-chart-bind-outside-markers",
        "charts/kube-agents/templates/operator-rbac.yaml",
        ("  # END GENERATED RULES",
         "  # END GENERATED RULES\n  - apiGroups:\n      - rbac.authorization.k8s.io\n"
         "    resources:\n      - clusterroles\n    verbs:\n      - bind"),
        "test_A4_the_chart_grants_the_same_ceiling_as_the_kustomize_role",
        "write an unrestricted bind into the chart BELOW the generated-rules "
        "marker, where chart-sync leaves it and a block-scoped parse misses it",
    ),
    Mutation(
        # Originally unpinned the chart's bind-to-view rule. Both delivery
        # paths now carry a bind again (scoped to system:auth-delegator), so
        # the edit is an UNSCOPED bind appearing in the chart copy alone —
        # the same-ceiling drift A4 exists to catch, in the direction that
        # widens.
        "A4-chart-bind-returns",
        "charts/kube-agents/templates/operator-rbac.yaml",
        ("  # END GENERATED RULES",
         "  - apiGroups:\n      - rbac.authorization.k8s.io\n    resources:\n"
         "      - clusterroles\n    verbs:\n      - bind\n  # END GENERATED RULES"),
        "test_A4_the_chart_grants_the_same_ceiling_as_the_kustomize_role",
        "give the chart's operator role an unrestricted bind the kustomize "
        "role does not carry -- one delivery path quietly grows a ceiling",
    ),
    Mutation(
        # The chart half was three literal string scans until #1319; this is
        # the spelling that walked past them. Flow style is not exotic -- it is
        # what `helm create` scaffolds and what a hand-edit reaches for.
        "A4-chart-impersonate-flow-style",
        "charts/kube-agents/templates/operator-rbac.yaml",
        ("    resources:\n      - events\n    verbs:\n      - create\n      - patch",
         "    resources:\n      - events\n    verbs: [impersonate, create, patch]"),
        "test_A4_the_chart_grants_the_same_ceiling_as_the_kustomize_role",
        "give the chart's leader-election Role impersonate, written in flow "
        "style: the object is outside the generated block so chart-sync leaves "
        "it, and `- impersonate` as a substring does not appear",
    ),
    Mutation(
        # The kustomize half read role.yaml alone. leader_election_role.yaml is
        # listed beside it in the same kustomization and installs just as
        # readily.
        "A4-leader-election-escalate",
        "k8s-operator/config/rbac/leader_election_role.yaml",
        ("    resources:\n      - events\n    verbs:\n      - create\n      - patch",
         "    resources:\n      - events\n    verbs:\n      - escalate\n      - create\n      - patch"),
        "test_A4_the_operator_cannot_escalate_its_own_grants",
        "add escalate to the operator's OTHER Role -- the leader-election one, "
        "which the same kustomization installs and A4 did not read",
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
        ("unexpected cluster CLI in the agent image", "cluster CLI note"),
        "test_B1_the_sandbox_image_ships_no_credentialed_cli",
        "reword the build gate's message, which is what the assertion anchors "
        "on -- checks that the anchor is registered and policed",
    ),
    Mutation(
        "B1-image-gate-binaries",
        "deploy/docker/Dockerfile",
        ("for binary in gcloud kubectl gh git helm k9s yq; do", "for binary in gcloud kubectl; do"),
        "test_B1_the_sandbox_image_ships_no_credentialed_cli",
        "shorten the gate's binary list, the plausible edit when one of them "
        "is legitimately needed at build time",
    ),
            Mutation(
        # The bucket-2 gate check asserts the class-level skip flag by
        # inspection; this is its kill, proven by execution before it was
        # encoded: strip the decorator from one scenario and the check goes
        # red without anything running.
        "bucket2-scenario-ungated",
        "tests/conformance/bucket2/test_cluster_scenarios.py",
        ("@h.requires_cluster\nclass Scenario5", "class Scenario5"),
        "test_bucket_two_is_skipped_for_the_stated_reason",
        "drop the cluster gate from one scenario while renaming the class -- "
        "before the check went structural, the next bucket-1 run would have "
        "executed a mutating kubectl against the developer's ambient context",
    ),
    Mutation(
        # The merge rule is one of the two the wide review found covered by
        # nothing: the original B1 known violation absorbed every subset of
        # its corpus, so deleting this rule left the suite green.
        "B1-merge-rule-weakened",
        "k8s-operator/internal/testing/testdata/platform/expected/platformagent.yaml",
        ("pr\\\\b(?:\\\\s+\\\\S+)*?\\\\s+merge\\\\b",
         "pr\\\\b(?:\\\\s+\\\\S+)*?\\\\s+mergeonly\\\\b"),
        "test_B1_the_denylist_refuses_merge_and_approve",
        "tighten github.merge to a spelling gh never emits; the agent merges "
        "its own pull request while every eyeball diff reads as a refactor",
    ),
    Mutation(
        "B1-assent-rule-weakened",
        "k8s-operator/internal/testing/testdata/platform/expected/platformagent.yaml",
        ("review\\\\b(?:\\\\s+\\\\S+)*?\\\\s+(?:--approve|-a)\\\\b",
         "review\\\\b(?:\\\\s+\\\\S+)*?\\\\s+(?:--approveonly)\\\\b"),
        "test_B1_the_denylist_refuses_merge_and_approve",
        "tighten github.assent the same way; the approval half goes with it",
    ),
Mutation(
        # The genuine attack for the disclosure assertion. policy_blocks reads
        # the rendered policy out of the default golden fixture, so the
        # weakening that matters is there: one character appended to the
        # pattern and `gcloud auth print-access-token` no longer matches.
        "B1-denylist-pattern",
        "k8s-operator/internal/testing/testdata/platform/expected/platformagent.yaml",
        ("print-(?:access|identity)-token\\\\b",
         "print-(?:access|identity)-tokensonly\\\\b"),
        "test_B1_the_shipped_denylist_refuses_credential_disclosure",
        "tighten the disclosure pattern to a spelling nothing uses while "
        "refactoring the escapes -- the rule survives every eyeball diff and "
        "matches nothing",
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
        # autopush-redeploy-agent.yml was deleted by #1199, which consolidated
        # the autopush deploys; every run of the whole sweep has crashed on
        # the missing path since. autopush-deploy.yml is the replacement and
        # carries the same predicate, once. This branch and #1310 found and
        # fixed that independently -- hence the missing-file case handled
        # below, which turns a crash into one stale mutation.
        "B4-workflow-run-gate",
        ".github/workflows/autopush-deploy.yml",
        ("github.event.workflow_run.head_branch == 'main'", "true"),
        "test_B4_every_workflow_run_deploy_gates",
        "drop the branch predicate while debugging a deploy, which is when it "
        "actually gets dropped",
    ),
    Mutation(
        # Originally inserted a checkout into auto_request_review.yml, which
        # has since moved off pull_request_target -- read its `on:` block: it
        # is `check_run: [completed]`, plus an `issue_comment` escape hatch --
        # so the insertion landed outside the test's trigger filter and proved
        # nothing. risk_classify.yml carries the trigger and runs a checkout
        # whose safety is exactly the pinned ref (three workflows carry
        # `pull_request_target` and two of them check anything out), so the
        # mutation is the one-line flip an author debugging the classifier
        # against their own PR would make.
        "B4-pull-request-target-checkout",
        ".github/workflows/risk_classify.yml",
        ("          ref: ${{ github.event.repository.default_branch }}",
         "          ref: ${{ github.event.pull_request.head.sha }}"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "point the classifier's checkout at the pull request so it classifies "
        "the new rules too -- arbitrary code execution with a writable token",
    ),
    Mutation(
        # GitHub resolves `uses:` case-insensitively, so `Actions/checkout`
        # runs the identical action, and the `.lower()` on that filter is what
        # keeps it matching its own name. This row pins that `.lower()`, and
        # it did not until 2026-09-19. It used to capitalise the name *and*
        # repoint the `ref:`, which measures nothing: the ref allowlist sits
        # outside the `actions/checkout` guard and reads every step whatever
        # it is called, so a repointed ref is RED with the `.lower()` and RED
        # without it. What the guard actually gates is the rule that a
        # checkout must carry a `ref:` at all, and `saw_a_checkout` with it.
        # So the mutation deletes the ref line instead. Measured: RED with the
        # `.lower()`, GREEN without. `persist-credentials: false` stays under
        # `with:`, so the block is still valid YAML and still a real checkout.
        #
        # Pins the action SHA because the name and the `with:` block are not
        # contiguous otherwise -- a pin bump reports STALE here, and the fix
        # is to paste the new SHA in.
        "B4-pull-request-target-checkout-case",
        ".github/workflows/risk_classify.yml",
        ("        uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1\n"
         "        with:\n"
         "          ref: ${{ github.event.repository.default_branch }}\n",
         "        uses: Actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1\n"
         "        with:\n"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "drop a checkout's ref while capitalising the action name, so the "
        "step falls back to the implicit ref under a name the rule about "
        "implicit refs cannot see",
    ),
    Mutation(
        # The third route to the same code: no action at all. `git fetch
        # origin pull/N/head` is the checkout action's own documented manual
        # equivalent, and a rule that reads `uses:` steps cannot see it.
        "B4-pull-request-target-run-fetch",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        run: |\n"
         '          git fetch --depth=1 origin "pull/${{ github.event.number }}/head"\n'
         "          git checkout FETCH_HEAD\n"
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "fetch the pull request by hand to get around a rule that only reads "
        "the checkout action's ref",
    ),
    Mutation(
        "B4-pull-request-target-run-fetch-sha",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        run: |\n"
         '          git fetch --depth=1 origin "${{ github.event.pull_request.head.sha }}"\n'
         "          git checkout FETCH_HEAD\n"
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "fetch the pull request head by SHA rather than by refspec, which the "
        "pull/N/head pattern does not match",
    ),
    Mutation(
        # The row that started the allowlist. `github.event.after` is the head
        # SHA delivered on every `synchronize`, so it names the pull request's
        # code using none of the words a denylist over `pull_request`, `head`
        # and `merge` looks for. Found live: this one line went in and the
        # whole suite stayed green.
        "B4-pull-request-target-checkout-after",
        ".github/workflows/risk_classify.yml",
        ("          ref: ${{ github.event.repository.default_branch }}",
         "          ref: ${{ github.event.after }}"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "check out the pull request under an event field that does not spell "
        "out what it is",
    ),
    Mutation(
        # Laundering the same ref through `env:`, which is the dodge the
        # allowlist closes as a side effect: an expression the test cannot
        # resolve to a known-safe ref is refused rather than read as innocent
        # text.
        #
        # The declaration order is deliberate and was wrong here until this
        # row was re-read. `_expand_env` substitutes in `env` insertion
        # order, so with TARGET_REV declared first its own pass rewrites the
        # ref to `${{ env.UPSTREAM_REV }}` and UPSTREAM_REV's pass -- later
        # in the same loop -- finishes the job in one go. Declaring
        # UPSTREAM_REV first inverts that: pass one substitutes UPSTREAM_REV
        # into a text that does not mention it yet, then TARGET_REV, and only
        # pass two reaches the SHA. The kill does not depend on which: a ref
        # still holding `${{ env.UPSTREAM_REV }}` is not on the allowlist
        # either, which is the allowlist's whole point. What the order buys
        # is that the fixed-point loop is exercised rather than accidentally
        # satisfied -- but only in the assertion message. Crippling
        # `_expand_env` to a single pass leaves this row KILLED either way,
        # measured: the ref is still unresolved, an unresolved ref is still
        # off the allowlist, and the runner records verdicts rather than
        # message text. So this row does not pin that loop and the order does
        # not make it pin it. Nor does anything else: the pickup's loop was
        # credited to B4-pull-request-target-run-fetch-verb-chain and is
        # measured unpinned as of 2026-09-19, for the reason set out there.
        "B4-pull-request-target-checkout-env",
        ".github/workflows/risk_classify.yml",
        ("        with:\n"
         "          ref: ${{ github.event.repository.default_branch }}",
         "        env:\n"
         "          UPSTREAM_REV: ${{ github.event.pull_request.head.sha }}\n"
         "          TARGET_REV: ${{ env.UPSTREAM_REV }}\n"
         "        with:\n"
         "          ref: ${{ env.TARGET_REV }}"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "pass the pull request head through two step-level env values so the "
        "checkout ref reads as a variable name",
    ),
    Mutation(
        # A `ref:` on something that is not `actions/checkout`. The action
        # fetches the same code; a rule written about one vendor is a rule
        # about that vendor. The fictitious action is SHA-pinned with a
        # version comment so it satisfies C4's sweep: an unpinned one breaks
        # two rules at once and the verdict stops saying which rule caught it.
        "B4-pull-request-target-checkout-third-party",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Check out the pull request with somebody else's action\n"
         "        uses: some-vendor/checkout-action"
         "@1b0c5f0f0f0e5ec9b0f4a2e6d7c8b9a0f1e2d3c4 # v1.2.3\n"
         "        with:\n"
         "          ref: ${{ github.event.pull_request.head.sha }}\n"
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "reach the pull request through a checkout action the rule does not "
        "name",
    ),
    Mutation(
        # A job whose steps live in another file. The called workflow is keyed
        # `workflow_call`, so it is not in the trigger filter either, and the
        # test would have inspected nothing while reporting ok. Refusing the
        # shape is the only answer that does not require following the call.
        "B4-pull-request-target-reusable-job",
        ".github/workflows/risk_classify.yml",
        ("jobs:\n"
         "  classify:",
         "jobs:\n"
         "  prepare:\n"
         "    uses: ./.github/workflows/e2e-run.yml\n"
         "\n"
         "  classify:"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "move the checkout into a called workflow, where a test that reads "
        "`steps:` cannot see it",
    ),
    Mutation(
        # The run half of B4-pull-request-target-checkout-after. The existing
        # by-SHA row fetches `github.event.pull_request.head.sha`; this is the
        # adjacent spelling, and the `env:` indirection is this repository's
        # own house style rather than an exotic dodge.
        "B4-pull-request-target-run-fetch-after",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        env:\n"
         "          REV: ${{ github.event.after }}\n"
         "        run: |\n"
         '          git fetch --depth=1 origin "$REV"\n'
         "          git checkout FETCH_HEAD\n"
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "fetch the same SHA under the event field the by-SHA row does not "
        "cover, through the environment",
    ),
    Mutation(
        # `ref:` is resolved inside `repository:`, so the ref allowlist on its
        # own does not say what gets checked out. The ref here is left exactly
        # as the carrier ships it -- the one expression the allowlist holds --
        # so every assertion in the ref half passes, and what gets checked out
        # is the fork's own default branch, which the fork wrote.
        "B4-pull-request-target-checkout-repository",
        ".github/workflows/risk_classify.yml",
        ("          ref: ${{ github.event.repository.default_branch }}",
         "          repository: ${{ github.event.pull_request.head.repo.full_name }}\n"
         "          ref: ${{ github.event.repository.default_branch }}"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "redirect the one allowlisted ref into the fork, which is the same "
        "code the ref allowlist exists to keep out",
    ),
    Mutation(
        # The allowlist is one entry, and this row is why it stays that way.
        # `github.base_ref` is the pull request's base branch, which its
        # *author* picks from the branches that already exist here: a stale
        # unprotected one is not the default branch and is not necessarily
        # code anybody has read this year. No fork can write it -- that needs
        # push access -- so what this row keeps out is the pull request's
        # author rather than the fork the test is named for. It is not the
        # ref an implicit checkout takes: since 2025-12-08 that is the default
        # branch, which is what the test's own assertion message says.
        "B4-pull-request-target-checkout-base-ref",
        ".github/workflows/risk_classify.yml",
        ("          ref: ${{ github.event.repository.default_branch }}",
         "          ref: ${{ github.base_ref }}"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "name the base branch outright, which is a ref the pull request's "
        "author chooses and nobody re-reviews",
    ),
    Mutation(
        # The literal spelling of B4-pull-request-target-checkout-repository
        # -- named rather than described as "the row above", which it stopped
        # being when the base-ref row was inserted between them. Neither value
        # carries an expression for either allowlist, and the ref half sees
        # only `main`, which is an ordinary ref and passes. `someone/else`
        # never reaches the ref half's `pull`/`head`/`merge` scan at all: it
        # is the `repository:` value, and what kills the row is the
        # repository half's own assertion, which refuses a literal outright
        # because nothing here can tell this repository's name from a
        # lookalike.
        "B4-pull-request-target-checkout-repository-literal",
        ".github/workflows/risk_classify.yml",
        ("          ref: ${{ github.event.repository.default_branch }}",
         "          repository: someone/else\n"
         "          ref: main"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "check out somebody else's repository by name, with a ref that looks "
        "like the most ordinary ref there is",
    ),
    Mutation(
        # Input keys are case-insensitive: the runner passes `with.Ref` to an
        # action as `INPUT_REF`, which is the same input `ref` sets. On
        # `actions/checkout` the uppercase spelling was caught by accident --
        # a `with.get("ref")` read nothing, and the must-carry-a-ref rule
        # fired. On any other action there is no such rule, so this step went
        # green. Same fictitious SHA-pinned vendor as the third-party row, so
        # C4's pin sweep is not the thing that catches it.
        "B4-pull-request-target-checkout-ref-case",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Check out the pull request under an upper-case input\n"
         "        uses: some-vendor/checkout-action"
         "@1b0c5f0f0f0e5ec9b0f4a2e6d7c8b9a0f1e2d3c4 # v1.2.3\n"
         "        with:\n"
         "          Ref: ${{ github.event.pull_request.head.sha }}\n"
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "spell the input key in a case a lowercase lookup does not read, "
        "which GitHub resolves to the same input",
    ),
    Mutation(
        # The run half's version of the `env:` dodge, named the way GitHub
        # names it. `${{ env.REV }}` is substituted before the shell starts,
        # so the script never contains a `$REV` for a shell-style pickup to
        # find, and the value is never folded in.
        "B4-pull-request-target-run-fetch-interpolated",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        env:\n"
         "          REV: ${{ github.event.after }}\n"
         "        run: |\n"
         '          git fetch --depth=1 origin "${{ env.REV }}"\n'
         "          git checkout FETCH_HEAD\n"
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "name the laundered ref the way GitHub names it rather than the way "
        "the shell does",
    ),
    Mutation(
        # Two hops through `env:`, both in GitHub's own syntax. Written to
        # pin the expansion inside the pickup -- `${{ env.A }}` becomes the
        # SHA before the haystack is rebuilt -- and it stopped pinning it on
        # 2026-09-19, when the rules over a fetching step started reading
        # every `env:` value in scope rather than the ones the pickup found:
        # A and REV are now both refused where they stand. Kept as the
        # two-hop spelling of the `env:` dodge. The loop it used to be about
        # is pinned by nothing and is gone as of 2026-09-19: it computed a
        # haystack no verdict read, which is what three rounds of review kept
        # finding and what the docstring kept promising to remove.
        "B4-pull-request-target-run-fetch-chained",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        env:\n"
         "          A: ${{ github.event.after }}\n"
         "          REV: ${{ env.A }}\n"
         '        run: git fetch --depth=1 origin "$REV" && git checkout FETCH_HEAD\n'
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "put one env value in front of another so the value the script names "
        "is itself a name",
    ),
    Mutation(
        # `git pull` is a fetch and a merge in one verb, and the verb list it
        # dodges had only `fetch`, `checkout` and `clone`. Matched as a
        # command rather than as the word, which the haystack is full of.
        "B4-pull-request-target-run-pull",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        env:\n"
         "          REV: ${{ github.event.after }}\n"
         '        run: git pull origin "$REV"\n'
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "merge the pull request's head into the working tree with the one "
        "git verb the fetch list did not name",
    ),
    Mutation(
        # The allowlist reads `${{ ... }}` with a regex, and a regex without
        # `re.DOTALL` cannot see an expression with a newline in it. A block
        # scalar keeps the line break exactly as written, so `findall`
        # returned nothing, the allowlist loop never ran, `sub` removed
        # nothing, and the whole expression arrived at the literal scan as
        # text -- where `github.event.after` spells none of `pull`, `head` or
        # `merge`. The same ref in a double-quoted scalar folds to one line
        # and was always caught, which is what made this one quiet.
        "B4-pull-request-target-checkout-ref-newline",
        ".github/workflows/risk_classify.yml",
        ("          ref: ${{ github.event.repository.default_branch }}",
         "          ref: |\n"
         "            ${{ format('{0}',\n"
         "            github.event.after) }}"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "reflow a long interpolated ref across two lines, which a block "
        "scalar makes look like ordinary YAML tidying",
    ),
    Mutation(
        # A ref that is present and says nothing. `ref:` with no value is
        # valid YAML and parses to None; `str(None)` is `"None"`, which is
        # truthy, so the must-carry-a-`ref:` rule was satisfied by a checkout
        # that carries no ref, and `"none"` holds none of the three words the
        # literal scan looks for. The honest spelling, `ref: ""`, reddened.
        "B4-pull-request-target-checkout-ref-null",
        ".github/workflows/risk_classify.yml",
        ("          ref: ${{ github.event.repository.default_branch }}",
         "          ref:"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "delete a ref's value without deleting its key, which is what a "
        "half-finished edit leaves behind and what the default-branch "
        "fallback quietly restores",
    ),
    Mutation(
        # The `git pull` alternative was matched on a single line by
        # construction, and one backslash is all it takes to spell the same
        # command over two. A continuation is how anybody writes a git
        # command with more flags than fit, so this is not even a dodge.
        "B4-pull-request-target-run-pull-continued",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        env:\n"
         "          REV: ${{ github.event.after }}\n"
         "        run: |\n"
         "          git \\\n"
         '            pull origin "$REV"\n'
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "wrap a `git pull` over two lines with a backslash, which reads as "
        "line-length housekeeping and unmatches a single-line pattern",
    ),
    Mutation(
        # A refspec that names no ref. `+refs/pull/${N}/*:refs/remotes/pr/*`
        # copies the whole namespace onto the runner and the checkout of
        # `pr/head` happens on the next line, where a pattern needing
        # `pull/` and `head` on one line can never join them. Refused on the
        # namespace now rather than on the two ref names.
        "B4-pull-request-target-run-refspec-wildcard",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        env:\n"
         "          PR_NUMBER: ${{ github.event.pull_request.number }}\n"
         "        run: |\n"
         "          git fetch origin"
         " \"+refs/pull/${PR_NUMBER}/*:refs/remotes/pr/*\"\n"
         "          git checkout pr/head\n"
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "fetch the pull request namespace with one wildcard refspec, which "
        "is how anybody mirrors several pull requests at once, and check the "
        "branch out by its local name on the next line",
    ),
    Mutation(
        # The glob without the `refs/` prefix, which is the third alternative
        # of _PULL_REQUEST_REF and the only thing that reads it.
        # `git ls-remote` matches a pattern against the tail of a refname, so
        # `pull/N/h*` resolves `refs/pull/N/head` while spelling neither the
        # namespace nor the ref, and what comes back is the SHA the fetch on
        # the next line wants.
        "B4-pull-request-target-run-refspec-ls-remote",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        env:\n"
         "          PR_NUMBER: ${{ github.event.pull_request.number }}\n"
         "        run: |\n"
         "          SHA=$(git ls-remote origin"
         " \"pull/${PR_NUMBER}/h*\" | cut -f1)\n"
         "          git fetch origin \"$SHA\" && git checkout FETCH_HEAD\n"
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "resolve the head with `ls-remote` before fetching it, which is the "
        "careful way to write a fetch that must not fail on a closed pull "
        "request",
    ),
    Mutation(
        # The same glob with no enumeration verb on the line, which is the
        # row that pins the third alternative. The row above does not, and
        # says it does: `_REMOTE_REF_ENUMERATION` reads `ls-remote` and
        # decides that one first, and deleting the glob alternative outright
        # left all four refspec rows KILLED and the whole sweep
        # byte-identical -- the same class round 11 found once and this is
        # twice. `git rev-parse --glob=` prepends `refs/` itself, so
        # `pull/N/h*` resolves `refs/pull/N/head` out of whatever refs the
        # runner's clone already has, with no `refs/` prefix, no `head`, and
        # nothing that asks the remote anything. Measured against git: the
        # short globbed form is a pattern `git ls-remote` and `git rev-parse
        # --glob` both take and `git fetch` does not -- a `+pull/N/*:...`
        # refspec is accepted and silently matches nothing -- so this is the
        # spelling, not a second one.
        "B4-pull-request-target-run-refspec-glob-rev-parse",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        env:\n"
         "          PR_NUMBER: ${{ github.event.pull_request.number }}\n"
         "        run: |\n"
         "          REV=$(git rev-parse --glob=\"pull/${PR_NUMBER}/h*\")\n"
         "          git checkout --detach \"$REV\" && ./ci.sh\n"
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "resolve the head out of the refs already on the runner with a glob "
        "rather than by asking the remote, which is the form somebody "
        "writes when the fetch is somebody else's step",
    ),
    Mutation(
        # The short-form refspec split across a backslash continuation, and
        # the only row that reads `_join_continuations`. The namespace
        # alternative does not see this one: there is no `refs/` prefix, and
        # `pull/${N}/` and `head` are on different lines until the join puts
        # them back together the way the shell does. Distinct from
        # B4-pull-request-target-run-pull-continued, which wraps the *verb*
        # and is caught by the expression allowlist instead.
        "B4-pull-request-target-run-refspec-continued",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        env:\n"
         "          PR_NUMBER: ${{ github.event.pull_request.number }}\n"
         "        run: |\n"
         "          git fetch origin pull/${PR_NUMBER}/\\\n"
         "          head\n"
         "          git checkout FETCH_HEAD\n"
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "wrap a long refspec over two lines with a backslash, which is "
        "line-length housekeeping everywhere else in this repository",
    ),
    Mutation(
        # The namespace reached without being spelled, and the first of the
        # two rows over `_REMOTE_REF_ENUMERATION`. Every refspec row above
        # writes `pull` somewhere, which is what the three alternatives of
        # `_PULL_REQUEST_REF` read. A bare `git ls-remote origin` writes
        # none of it: the remote advertises `refs/pull/N/head` to anyone who
        # asks, `grep` picks this pull request's line out of the listing, and
        # `git fetch origin "$SHA"` is a fetch by object name the server
        # serves. Distinct from B4-pull-request-target-run-refspec-ls-remote,
        # which passes the remote a `pull/N/h*` pattern and is caught by the
        # ref half for spelling it -- delete the enumeration rule and that
        # row still kills while this one survives, which is what separates
        # the pair.
        "B4-pull-request-target-run-ls-remote-unfiltered",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        env:\n"
         "          PR_NUMBER: ${{ github.event.pull_request.number }}\n"
         "        run: |\n"
         "          SHA=$(git ls-remote origin"
         " | grep \"/$PR_NUMBER/head\" | cut -f1)\n"
         "          git fetch origin \"$SHA\" && git checkout FETCH_HEAD\n"
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "list what the remote advertises and filter it locally, which is "
        "what somebody writes when they do not want to depend on the "
        "server's pattern matching",
    ),
    Mutation(
        # The same reach with the enumeration done by the fetch. `+refs/*`
        # copies every namespace the remote has onto the runner, the pull
        # namespace among them, and `git for-each-ref` then reads the head
        # out of the local copy -- so the only thing naming `pull` is a
        # `grep` pattern built from the number. This is the row that pins the
        # second alternative: the rule refuses a refspec that globs *before*
        # the namespace is fixed, and `refs/heads/*` a line away is an
        # ordinary mirror fetch it leaves alone.
        "B4-pull-request-target-run-wildcard-namespace-refspec",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        env:\n"
         "          PR_NUMBER: ${{ github.event.pull_request.number }}\n"
         "        run: |\n"
         "          git fetch origin \"+refs/*:refs/remotes/all/*\"\n"
         "          SHA=$(git for-each-ref"
         " | grep \"/$PR_NUMBER/head\" | cut -d' ' -f1)\n"
         "          git checkout \"$SHA\"\n"
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "mirror the remote's whole ref space in one fetch, which is the "
        "shape of a workflow that wants notes, tags and branches together, "
        "and read the head back out of it",
    ),
    Mutation(
        # The advertisement asked for without `git`. `info/refs` with
        # `service=git-upload-pack` is the wire protocol underneath
        # `ls-remote`: unauthenticated, and it answers with the same listing,
        # so `grep "/$PR_NUMBER/head"` reads the head out of it and the fetch
        # by object name follows. Refusing the command while conceding the
        # request it makes would have been a rule about which program is
        # installed rather than about what the step reaches, which is why the
        # path and the service name are their own alternatives.
        "B4-pull-request-target-run-smart-http-advertisement",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        env:\n"
         "          PR_NUMBER: ${{ github.event.pull_request.number }}\n"
         "        run: |\n"
         "          SHA=$(curl -fsSL"
         " \"https://github.com/${{ github.repository }}.git"
         "/info/refs?service=git-upload-pack\""
         " | grep \"/$PR_NUMBER/head\" | cut -c5-44)\n"
         "          git fetch origin \"$SHA\" && git checkout FETCH_HEAD\n"
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "ask the remote for its ref advertisement over plain HTTP, which "
        "needs no token, no `gh` and no `git` subcommand the rule above "
        "knows by name",
    ),
    Mutation(
        # `printenv NAME` is a read of NAME that never writes `$NAME`. The
        # pickup was keyed on the sigil, so the value was never folded in and
        # the fetch read as innocent.
        "B4-pull-request-target-run-printenv",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        env:\n"
         "          REV: ${{ github.event.after }}\n"
         "        run: |\n"
         '          git fetch --depth=1 origin "$(printenv REV)"\n'
         "          git checkout FETCH_HEAD\n"
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "read the laundered ref with `printenv` rather than with a sigil, "
        "which is the same read spelled as a command",
    ),
    Mutation(
        # Indirect expansion: `${!PTR}` is the value of the variable *named*
        # by PTR. Following it is a hop the pickup does not take -- it folds
        # in PTR, whose value is the string `REV`, and nothing in the haystack
        # then names the head.
        #
        # This comment used to say the row pinned the environment refusal,
        # and it does not. Measured 2026-09-19: the expression allowlist
        # fires first, on `github.event.after` inside REV's value, and
        # deleting the environment refusal outright leaves this row KILLED.
        # The shape is still worth a row, because it is a read no pickup
        # keyed on a name can follow -- but the pin it was credited with
        # belongs to the row below, which is the only one here whose `env:`
        # carries the head with no expression in it for the allowlist to see.
        "B4-pull-request-target-run-indirect-expansion",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        env:\n"
         "          REV: ${{ github.event.after }}\n"
         "          PTR: REV\n"
         "        run: |\n"
         '          git fetch --depth=1 origin "${!PTR}"\n'
         "          git checkout FETCH_HEAD\n"
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "name the variable that names the ref, so the script mentions the "
        "pointer and never the pull request",
    ),
    Mutation(
        # The environment refusal, and the only row that pins it. Every other
        # candidate carries the head as `${{ ... }}`, which the expression
        # allowlist refuses before this rule is reached, so deleting the rule
        # leaves all of them KILLED and the pin is imaginary. Here the value
        # is a literal refspec -- no expression at all -- and the script never
        # names REV, so the pickup folds nothing in and the haystack the two
        # literal backstops read holds one line of shell that mentions
        # nothing. What is left is the question the refusal exists for: this
        # step's environment holds the pull request's head, and whether the
        # Python file it runs reads it is not a thing this file can know.
        # Measured both ways -- KILLED as it stands, SURVIVED with the
        # `carried` assertion deleted.
        "B4-pull-request-target-run-carried-literal",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        env:\n"
         "          REV: refs/pull/42/head\n"
         "        run: python3 .ci/fetch.py\n"
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "hand the refspec to a helper script through the environment, which "
        "is tidier than interpolating it and hides it from every scan here",
    ),
    Mutation(
        # The pull request over HTTP, which is the channel the number opened.
        # Deleting the fetch gate meant every step is read, and two
        # expressions had to go on the script allowlist for the carriers to
        # stay green; one of them is the pull request's number, and the number
        # plus the token is all `gh pr checkout` takes. This row and the three
        # below are the four spellings `_PULL_REQUEST_API` reads. They exist
        # because the concession is a real one: without that backstop these
        # three were refused at 866fa939 and passed after it, which is a
        # coverage loss a row has to be able to see.
        "B4-pull-request-target-api-gh-pr-checkout",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        env:\n"
         "          GH_TOKEN: ${{ github.token }}\n"
         "          N: ${{ github.event.pull_request.number }}\n"
         "        run: gh pr checkout \"$N\"\n"
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "let the CLI do the fetch and the checkout in one word, so the step "
        "names no ref and no remote",
    ),
    Mutation(
        # The same call spelled as a URL rather than as a subcommand, which is
        # why the backstop reads `/pulls/` as well as the four verbs. `curl`
        # against api.github.com is this row with the client swapped.
        "B4-pull-request-target-api-gh-api-pulls",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        env:\n"
         "          GH_TOKEN: ${{ github.token }}\n"
         "        run: |\n"
         "          REV=$(gh api \"repos/${{ github.repository }}"
         "/pulls/${{ github.event.pull_request.number }}\" --jq .head.sha)\n"
         "          git fetch --depth=1 origin \"$REV\"\n"
         "          git reset --hard FETCH_HEAD\n"
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "ask the API for the head rather than reading it off the event, so "
        "the SHA arrives over HTTP and no expression names it",
    ),
    Mutation(
        # The fork's code without its history. A diff applied to the base tree
        # is the same arbitrary code arriving, and it was green before this
        # round as well as after the number went on the allowlist -- the one
        # shape here the backstop closed rather than reopened.
        "B4-pull-request-target-api-gh-pr-diff",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        env:\n"
         "          GH_TOKEN: ${{ github.token }}\n"
         "        run: |\n"
         "          gh pr diff ${{ github.event.pull_request.number }}"
         " | git apply\n"
         "          ./run.sh\n"
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "apply the diff instead of checking the branch out, which touches no "
        "remote at all",
    ),
    Mutation(
        # The head without the code, which is enough: the SHA is what the
        # fetch on the next line needs. `gh pr view` is on the backstop for
        # this and not because viewing is dangerous.
        "B4-pull-request-target-api-gh-pr-view",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        env:\n"
         "          GH_TOKEN: ${{ github.token }}\n"
         "        run: |\n"
         "          REV=$(gh pr view ${{ github.event.pull_request.number }}"
         " --json headRefOid --jq .headRefOid)\n"
         "          git fetch origin \"$REV\"\n"
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "read the head out of the CLI's JSON, which is the API call with a "
        "friendlier front end",
    ),
    Mutation(
        # The subcommand the three-verb backstop did not name. `gh pr list`
        # returns every open pull request with `--json headRefOid`, and
        # filtering the array by number is one `--jq` away -- so this is `gh
        # pr view` with the lookup done client-side, past a pattern that read
        # `checkout`, `diff` and `view` and nothing else. The four rows here
        # and below are why that pattern is now an allowlist: this one, the
        # collection endpoint, the interposed flag and the client library
        # were all green at 5863df72.
        "B4-pull-request-target-api-gh-pr-list",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        env:\n"
         "          GH_TOKEN: ${{ github.token }}\n"
         "          PR_NUMBER: ${{ github.event.pull_request.number }}\n"
         "        run: |\n"
         "          REV=$(gh pr list --state all --json number,headRefOid"
         " --jq \".[] | select(.number == $PR_NUMBER) | .headRefOid\")\n"
         "          git fetch origin \"$REV\" && git checkout FETCH_HEAD\n"
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "ask the list endpoint for the head rather than the item endpoint, "
        "which is one subcommand's difference and reads as a survey of open "
        "pull requests rather than a lookup of this one",
    ),
    Mutation(
        # The same request as B4-pull-request-target-api-gh-api-pulls with
        # the trailing slash gone. `/pulls?state=all` is the collection, and
        # it hands back every head the item endpoint would -- the pattern
        # wanted `/pulls/`, so a `?` where a `/` was expected was the whole
        # evasion. The path segment is matched on its own boundary now.
        "B4-pull-request-target-api-pulls-collection",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        env:\n"
         "          GH_TOKEN: ${{ github.token }}\n"
         "        run: |\n"
         "          REV=$(gh api \"repos/${{ github.repository }}"
         "/pulls?state=all\" --jq '.[0].head.sha')\n"
         "          git fetch origin \"$REV\" && git checkout FETCH_HEAD\n"
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "page the pulls collection instead of naming a number, which is what "
        "a workflow that handles several pull requests at once would write",
    ),
    Mutation(
        # `gh pr checkout` with a flag in front of the verb. `-R` is a
        # persistent flag and the CLI strips flags before it resolves the
        # subcommand, so this runs exactly what
        # B4-pull-request-target-api-gh-pr-checkout runs -- but a pattern
        # anchored on `pr\s+checkout` sees `pr -R` and reports nothing. This
        # row is the argument for walking the words rather than matching
        # them, and it is the one that has to stay green if anybody puts the
        # regex back.
        "B4-pull-request-target-api-gh-pr-interposed-flag",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        env:\n"
         "          GH_TOKEN: ${{ github.token }}\n"
         "          PR_NUMBER: ${{ github.event.pull_request.number }}\n"
         "        run: gh pr -R \"${{ github.repository }}\" checkout"
         " \"$PR_NUMBER\"\n"
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "name the repository explicitly on a checkout that already worked, "
        "which is the tidying a reviewer asks for when a step runs `gh` "
        "outside a checked-out tree",
    ),
    Mutation(
        # The namespace indexed rather than accessed. One quote is the whole
        # difference from B4-pull-request-target-api-octokit-pulls, which is
        # why _PULL_REQUEST_API reads `rest` and the punctuation after it
        # instead of a literal `rest.pulls`. Without that this row survives
        # and the row above still kills, so the pair is what pins the
        # difference rather than either one alone.
        "B4-pull-request-target-api-octokit-pulls-indexed",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        uses: actions/github-script"
         "@60a0d83039c74a4aee543508d2ffcb1c3799cdea # v7.0.1\n"
         "        with:\n"
         "          script: |\n"
         "            const { data } = await github.rest['pulls'].get({\n"
         "              ...context.repo,\n"
         "              pull_number: ${{ github.event.pull_request.number }}"
         "\n"
         "            });\n"
         "            await exec.exec('git', ['fetch','origin',"
         " data.head.sha]);\n"
         "            await exec.exec('git', ['checkout','FETCH_HEAD']);\n"
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "reach the same namespace through a string index, which is ordinary "
        "JavaScript and defeats a rule written against a dot",
    ),
    Mutation(
        # The flag-value skip, from the other side: this is a *legitimate*
        # label write with `-R` interposed, and the suite must stay green on
        # it. Delete _GH_VALUE_FLAGS or the expression collapse and the walk
        # reads the repository as the subcommand, refuses it, and this row
        # reds -- which is backwards for a mutation, so the row inverts: the
        # edit is the safe spelling and `must_survive` says the suite has no
        # business objecting to it. Without this, nothing pins the skip and a
        # future round could delete it and see a clean sweep.
        "B4-pull-request-target-api-gh-pr-interposed-flag-write",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Label the pull request\n"
         "        env:\n"
         "          GH_TOKEN: ${{ github.token }}\n"
         "        run: |\n"
         "          gh pr -R \"${{ github.repository }}\" edit"
         " ${{ github.event.pull_request.number }} --add-label triage\n"
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "name the repository explicitly on a label write, which is the same "
        "command the live carrier runs with one persistent flag in front of "
        "the verb",
        must_survive=True,
    ),
    Mutation(
        # The same endpoint from the language the shell rules cannot read.
        # `actions/github-script` hands the step an authenticated Octokit as
        # `github`, so this names no `gh`, no `/pulls` path and no refused
        # expression: `context.repo` is on _SAFE_SCRIPT_CONTEXTS and the
        # number is on _SAFE_SCRIPT_EXPRESSIONS. `data.head.sha` is a
        # property of the response rather than of the event, so
        # _PULL_REQUEST_HEAD does not see it either. Pinned to the real
        # action's SHA, like B4-pull-request-target-script-input: an unpinned
        # one would trip C4's sweep and the verdict would stop saying which
        # rule caught this.
        "B4-pull-request-target-api-octokit-pulls",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        uses: actions/github-script"
         "@60a0d83039c74a4aee543508d2ffcb1c3799cdea # v7.0.1\n"
         "        with:\n"
         "          script: |\n"
         "            const { data } = await github.rest.pulls.get({\n"
         "              ...context.repo,\n"
         "              pull_number: ${{ github.event.pull_request.number }}"
         "\n"
         "            });\n"
         "            await exec.exec('git', ['fetch','origin',"
         " data.head.sha]);\n"
         "            await exec.exec('git', ['checkout','FETCH_HEAD']);\n"
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "use the client the action already hands the script, rather than "
        "shelling out to `gh` -- which is the idiomatic way to write this "
        "step and the way that names none of the nouns the shell rules read",
    ),
    Mutation(
        # The program name in quotes, which is what `_GH_COMMAND`'s
        # whitespace lookahead never saw. A shell strips the quotes off a
        # bare word before it execs, so this runs exactly what
        # B4-pull-request-target-api-gh-pr-checkout runs -- but the character
        # after `gh` is an apostrophe, the pattern wanted a space, and the
        # walk was never handed the invocation at all. `gh'' pr checkout` is
        # the same trick with the quotes empty and is not a second row: both
        # die on the lookahead and nothing separates them.
        "B4-pull-request-target-api-gh-quoted-command",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        env:\n"
         "          GH_TOKEN: ${{ github.token }}\n"
         "          PR_NUMBER: ${{ github.event.pull_request.number }}\n"
         "        run: |\n"
         "          'gh' pr checkout \"$PR_NUMBER\"\n"
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "quote the program name, which a shell-quoting pass over a script "
        "does to every word it is not sure about",
    ),
    Mutation(
        # The same command as an argument vector, which is the ordinary way
        # to run a program from a `script:` input rather than an
        # obfuscation: `actions/github-script` hands the body an `exec` and
        # this is what its documentation shows. What catches it is the
        # widened `_GH_COMMAND` lookahead alone -- the character after `gh`
        # is a quote followed by a comma, and without that the walk is never
        # handed the invocation. Measured, because the obvious reading is
        # wrong: revert `_GH_WORD_PUNCTUATION` to the old quote-only strip
        # and this row still KILLS, because `['pr',` is then read as the verb
        # and refused for not being on `_SAFE_GH_VERBS`. The punctuation
        # strip earns its place on the safe side instead, at
        # B4-pull-request-target-api-gh-argv-vector-write. Pinned to the real
        # action's SHA, like the two rows above: an unpinned one would trip
        # C4's sweep and the verdict would stop saying which rule caught this.
        "B4-pull-request-target-api-gh-argv-vector",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        uses: actions/github-script"
         "@60a0d83039c74a4aee543508d2ffcb1c3799cdea # v7.0.1\n"
         "        env:\n"
         "          GH_TOKEN: ${{ github.token }}\n"
         "        with:\n"
         "          script: |\n"
         "            await exec.exec('gh', ['pr', 'checkout',"
         " '${{ github.event.pull_request.number }}']);\n"
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "run the CLI the way the action's own documentation runs a program, "
        "as a name and a list of arguments rather than as a command line",
    ),
    Mutation(
        # The program name written the way four of this repository's own
        # `scripts/release/*.sh` write one. `command -v gh` resolves the path
        # and the command substitution runs it, so the character after the
        # letters `gh` is a `)` -- where `_GH_COMMAND`'s lookahead wants
        # whitespace, a quote or a comma -- and the walk over `gh` is never
        # started at all. Nothing over the program *name* can close this: a
        # shell has unlimited ways to spell one, and `"$GH"` and `g'h'` in the
        # two rows below are two more. What closes it is
        # `_GH_PULL_REQUEST_WORD`, which reads the argument shape instead and
        # holds `pr checkout` to the same subcommand allowlist whatever ran
        # it.
        "B4-pull-request-target-api-gh-program-path",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        env:\n"
         "          GH_TOKEN: ${{ github.token }}\n"
         "          PR_NUMBER: ${{ github.event.pull_request.number }}\n"
         "        run: |\n"
         "          \"$(command -v gh)\" pr checkout \"$PR_NUMBER\"\n"
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "resolve the CLI's path before running it, which is how the release "
        "scripts in this repository invoke a tool they are not sure is on "
        "PATH",
    ),
    Mutation(
        # The same reach with the name in the environment, and the residual
        # round 9 wrote down and left open: `$C pr checkout` with `C: gh` is
        # not a `gh` invocation to any rule keyed on the word. The `env:`
        # value carries no head, so the wholesale environment refusal does
        # not reach it either. It is closed now for the same reason the row
        # above is -- the arguments say `pr checkout` whoever runs them.
        "B4-pull-request-target-api-gh-program-in-env",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        env:\n"
         "          GH_TOKEN: ${{ github.token }}\n"
         "          C: gh\n"
         "          PR_NUMBER: ${{ github.event.pull_request.number }}\n"
         "        run: |\n"
         "          $C pr checkout \"$PR_NUMBER\"\n"
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "put the program name in the environment with the rest of the step's "
        "configuration, which is this repository's house style for "
        "everything else a step needs",
    ),
    Mutation(
        # A `gh` whose arguments this file cannot read at all, and the row
        # for the other half of the round-10 `gh` fix. The vector is in a
        # data file, so the step names no verb, no subcommand and no path --
        # `_GH_PULL_REQUEST_WORD` sees no `pr` either, because there is none
        # in the workflow. The walk used to skip an invocation with no words
        # after it, on the reasoning that a bare `gh` runs nothing; a `gh`
        # taking its argv off a pipe runs whatever arrives, so an unreadable
        # invocation is refused for being unreadable, which is the answer
        # this file gives an unresolvable ref one field along. This row is
        # the only thing pinning that, because every other stdin spelling
        # writes `pr` somewhere the backstop reads it.
        "B4-pull-request-target-api-gh-arguments-unreadable",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        env:\n"
         "          GH_TOKEN: ${{ github.token }}\n"
         "          PR_NUMBER: ${{ github.event.pull_request.number }}\n"
         "        run: |\n"
         "          jq -r '.argv[]' .github/gh-argv.json | xargs gh\n"
         "          git checkout FETCH_HEAD\n"
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "keep the CLI's arguments in a data file beside the workflow, which "
        "is the tidying somebody does to a step that runs the same command "
        "with a long argument list",
    ),
    Mutation(
        # The verb renamed, and the row the verb allowlist exists for. `gh
        # alias set` writes a user-level alias into the CLI's config, so the
        # second line *is* `gh pr checkout` to the CLI and is `gh co` to any
        # reader of the file -- a word GitHub never shipped and no denylist
        # over subcommands can be written to contain. A walk that skips every
        # invocation whose verb is not `pr` reads neither line: the first is
        # `alias`, the second is `co`. Both are refused now for not being on
        # `_SAFE_GH_VERBS`, which is the same move one noun up that
        # `_SAFE_GH_PULL_REQUEST_SUBCOMMANDS` made a round earlier.
        "B4-pull-request-target-api-gh-alias-verb",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        env:\n"
         "          GH_TOKEN: ${{ github.token }}\n"
         "          PR_NUMBER: ${{ github.event.pull_request.number }}\n"
         "        run: |\n"
         "          gh alias set co 'pr checkout'\n"
         "          gh co \"$PR_NUMBER\"\n"
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "alias the checkout to a short name first, which is the tidying "
        "somebody does to a script that runs the same subcommand repeatedly",
    ),
    Mutation(
        # The fork's changes over HTTP with no token in the request at all.
        # `https://github.com/O/R/pull/N.diff` is served off the pull
        # request's web page, so this is
        # B4-pull-request-target-api-gh-pr-diff with the client swapped for
        # `curl` and the credential dropped -- and it named nothing any rule
        # read: not `/pulls`, which is the API path and this is not; not
        # `refs/pull`, which the web URL has no prefix for; not `gh`. The
        # `.patch` sibling through `git am` is the same alternative and is
        # not a second row.
        "B4-pull-request-target-api-web-diff-endpoint",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        env:\n"
         "          PR_NUMBER: ${{ github.event.pull_request.number }}\n"
         "        run: |\n"
         "          curl -fsSL \"https://github.com/"
         "${{ github.repository }}/pull/${PR_NUMBER}.diff\" | git apply\n"
         "          ./run.sh\n"
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "fetch the diff off the pull request's web page, which needs no "
        "token and no authentication step ahead of it",
    ),
    Mutation(
        # The head through the noun this file used to leave open. A pull
        # request is an issue to the REST API, and `/issues/N/timeline`
        # returns its `committed` events, each carrying a head SHA -- so the
        # verb is `api`, which is allowlisted, and the path says `issues`.
        # This row was the reason `_PULL_REQUEST_API` carried two named issues
        # endpoints; as of round 10 it carries the namespace instead, because
        # the item endpoint one segment shorter hands back `patch_url` and no
        # list of endpoints was ever going to be finished. Kept as the
        # timeline spelling of a reach the namespace now covers whole.
        "B4-pull-request-target-api-issues-timeline",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        env:\n"
         "          GH_TOKEN: ${{ github.token }}\n"
         "          PR_NUMBER: ${{ github.event.pull_request.number }}\n"
         "          REPO: ${{ github.repository }}\n"
         "        run: |\n"
         "          SHA=$(gh api \"repos/$REPO/issues/$PR_NUMBER/timeline\""
         " --jq '.[] | select(.event==\"committed\") | .sha' | tail -1)\n"
         "          git fetch origin \"$SHA\" && git checkout FETCH_HEAD\n"
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "read the head off the issue timeline rather than off the pull "
        "request, which is one endpoint over and returns the same commits",
    ),
    Mutation(
        # The shortest URL in the namespace, and the round-10 finding the
        # comment above `_PULL_REQUEST_API` was wrong about. `GET
        # /repos/O/R/issues/N` is the *item* endpoint, and for an issue that
        # is a pull request it returns `pull_request: {url, html_url,
        # diff_url, patch_url}` -- so the fork's patch is one `--jq` away, the
        # request names no `/pulls`, the `gh` verb is allowlisted, the only
        # expression in the step is the number, and `git am` puts the fork's
        # commits on the default-branch checkout with `make test` running them
        # under the write token. Two rounds of rules over this namespace read
        # `/issues/N/timeline` and `/issues/N/events` and let the endpoint
        # they are both suffixes of straight through. This row is why the rule
        # is the namespace.
        "B4-pull-request-target-api-issues-item-patch-url",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        env:\n"
         "          GH_TOKEN: ${{ github.token }}\n"
         "          PR_NUMBER: ${{ github.event.pull_request.number }}\n"
         "          REPO: ${{ github.repository }}\n"
         "        run: |\n"
         "          URL=$(gh api \"repos/$REPO/issues/$PR_NUMBER\""
         " --jq .pull_request.patch_url)\n"
         "          curl -sL \"$URL\" | git am\n"
         "          make test\n"
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "read the pull request off the issue it also is, which is one "
        "endpoint the API documents as returning the same object and the "
        "spelling a workflow that handles issues and pull requests together "
        "already has to hand",
    ),
    Mutation(
        # The same namespace with no number in it at all. `search/issues`
        # takes a query rather than an identifier, so `repo:O/R+type:pr`
        # returns every open pull request here, each item carrying the same
        # `pull_request.patch_url` the row above reads -- which means a step
        # does not even need `github.event.pull_request.number` on the
        # expression allowlist to reach a fork's code. Written with `type:pr`
        # rather than the more usual `is:pr` so that the row isolates the
        # path alternative: `is:pr` is refused twice over, because the `:` is
        # not a word character and `_GH_PULL_REQUEST_WORD` reads the rest of
        # the query as a subcommand.
        "B4-pull-request-target-api-search-issues",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        env:\n"
         "          GH_TOKEN: ${{ github.token }}\n"
         "          REPO: ${{ github.repository }}\n"
         "        run: |\n"
         "          U=$(gh api \"search/issues?q=repo:$REPO+type:pr\""
         " --jq '.items[0].pull_request.patch_url')\n"
         "          curl -sL \"$U\" | git am\n"
         "          make test\n"
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "search for the repository's open pull requests rather than naming "
        "one, which is what a step that sweeps them all would write",
    ),
    Mutation(
        # The same endpoint from the language the shell rules cannot read,
        # and the pair to the row above the way
        # B4-pull-request-target-api-octokit-pulls is the pair to
        # B4-pull-request-target-api-gh-api-pulls. `listEventsForTimeline` is
        # the client method for `/issues/N/timeline`, so the path alternative
        # never sees it -- there is no slash anywhere in the call. The method
        # name was its own alternative until round 10 and is not one now: what
        # matches is `rest.issues`, which every route to that namespace writes
        # once, a destructured one included. Pinned to the real action's SHA
        # for the same reason as its neighbours.
        "B4-pull-request-target-api-octokit-issues-timeline",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        uses: actions/github-script"
         "@60a0d83039c74a4aee543508d2ffcb1c3799cdea # v7.0.1\n"
         "        with:\n"
         "          script: |\n"
         "            const ev = await"
         " github.rest.issues.listEventsForTimeline({\n"
         "              ...context.repo,\n"
         "              issue_number: ${{ github.event.pull_request.number }}"
         "\n"
         "            });\n"
         "            const sha = ev.data.pop().sha;\n"
         "            await exec.exec('git', ['fetch','origin', sha]);\n"
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "ask the same timeline through the client the action already hands "
        "the script, which names no path for a path rule to read",
    ),
    Mutation(
        # The web link the diff rule must not catch, and the control that
        # says so. `_PULL_REQUEST_API` now reads the `pull/` web path, and
        # the ref half has always conceded a bare `.../pull/1781` on purpose:
        # it is a link to a page, and a workflow that comments on a pull
        # request has every reason to write one down. Widen the new
        # alternative past the diff extension -- drop the `\.(?:diff|patch)`
        # and match `pull/` -- and this row reds, which is backwards for a
        # mutation, so the row inverts. Without it nothing pins the
        # difference between the endpoint that serves the fork's code and the
        # page a human reads.
        "B4-pull-request-target-api-web-link-comment",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Link the pull request\n"
         "        env:\n"
         "          GH_TOKEN: ${{ github.token }}\n"
         "        run: |\n"
         "          # convention:"
         " https://github.com/gke-labs/kube-agents/pull/1781\n"
         "          gh pr comment"
         " ${{ github.event.pull_request.number }} --body \"see above\"\n"
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "cite a pull request by its web link in a comment the workflow "
        "posts, which is what every reference to one in this repository "
        "looks like",
        must_survive=True,
    ),
    Mutation(
        # The `issue` verb, from the safe side. A pull request is an issue to
        # GitHub, so `gh issue comment 1781` comments on this pull request --
        # the same write `gh pr comment` does, through the other noun, and
        # the reason `issue` is on `_SAFE_GH_VERBS` at all. Take it off and
        # this row reds: the suite would be refusing an ordinary labelling
        # write and telling its author to allowlist a verb whose subcommands
        # are already governed. So the row inverts, and it is the only thing
        # pinning the third entry of that list against a future round
        # tightening it to `pr` and `api` and seeing a clean sweep.
        "B4-pull-request-target-api-gh-issue-comment-write",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Comment on the pull request\n"
         "        env:\n"
         "          GH_TOKEN: ${{ github.token }}\n"
         "        run: |\n"
         "          gh issue comment"
         " ${{ github.event.pull_request.number }} --body \"triaged\"\n"
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "post the comment through the issues noun rather than the pull "
        "requests one, which is the same API call and the spelling somebody "
        "reaches for when the workflow handles issues too",
        must_survive=True,
    ),
    Mutation(
        # The argument vector from the safe side, and the only thing that
        # pins `_GH_WORD_PUNCTUATION`. This is the live milestone carrier's
        # label write spelled the way a `script:` input spells it, and it has
        # to stay green. Strip only quotes off each word, as the walk did
        # before this round, and the verb reads as `['pr',` -- which is on no
        # allowlist, so the suite refuses an ordinary label write and tells
        # its author to allowlist a fragment of JavaScript. The killer row
        # above cannot see that: refusing the fragment is the right verdict
        # there for the wrong reason, so it kills either way. Pinned to the
        # real action's SHA for the same reason as its neighbours.
        "B4-pull-request-target-api-gh-argv-vector-write",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Label the pull request\n"
         "        uses: actions/github-script"
         "@60a0d83039c74a4aee543508d2ffcb1c3799cdea # v7.0.1\n"
         "        env:\n"
         "          GH_TOKEN: ${{ github.token }}\n"
         "        with:\n"
         "          script: |\n"
         "            await exec.exec('gh', ['pr', 'edit',"
         " '${{ github.event.pull_request.number }}',"
         " '--add-label', 'triage']);\n"
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "write the label through the `exec` the action already hands the "
        "script, which is one step fewer than shelling out and is the same "
        "command the live carrier runs",
        must_survive=True,
    ),
    Mutation(
        # Not a `run:` step at all. `actions/github-script` takes JavaScript
        # as an input and runs it in the job with the same token and the same
        # working directory, so the fetch is the identical hazard in another
        # language -- and a haystack built from `run:` alone reads none of it.
        # Pinned to the real action's SHA: an unpinned one would trip C4's
        # sweep and the verdict would stop saying which rule caught this.
        "B4-pull-request-target-script-input",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        uses: actions/github-script"
         "@60a0d83039c74a4aee543508d2ffcb1c3799cdea # v7.0.1\n"
         "        with:\n"
         "          script: |\n"
         "            await exec.exec('git', ['fetch','origin',\n"
         "              context.payload.pull_request.head.sha]);\n"
         "            await exec.exec('git', ['checkout','FETCH_HEAD']);\n"
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "move the fetch into a `script:` input, where the shell the test "
        "reads is not the language that runs",
    ),
    Mutation(
        # The widest grant GitHub offers, spelled as a string rather than as
        # a mapping. Both halves of this test filtered on `isinstance(scope,
        # dict)`, so `write-all` at the workflow level granted `contents:
        # write` and `id-token: write` without either assertion seeing a
        # field. The job-level spelling reddened -- but through
        # test_B2_no_workflow_grants_a_bot_the_ability_to_approve, which is a
        # neighbour asking a different question, and a neighbour's red is not
        # this assertion working. NOISY by construction now that both halves
        # read the shorthand: `write-all` includes `contents: write`, so
        # test_B4_contents_write_is_confined_to_the_release_path gains a
        # holder too. That second red is the other half of the same fix.
        "B4-pull-request-target-permissions-write-all",
        ".github/workflows/risk_classify.yml",
        ("permissions: {}", "permissions: write-all"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "widen the top-level grant to `write-all` while adding a step that "
        "needs one more scope, rather than naming the scope -- the tidying "
        "that hands a pull-request-triggered job the push credential",
    ),
    Mutation(
        # A fetch whose arguments are assembled out of a second env value,
        # so the name the script reads is one hop from the name that matters.
        # This was written as the row for the pickup's outer loop and it has
        # not been that for two rounds; the correction it then got --
        # crediting the environment refusal -- was wrong too. Measured
        # 2026-09-19: the expression allowlist kills it, on
        # `github.event.after` in B's value, ahead of both. Reduce the pickup
        # to a single iteration, or delete the environment refusal, and this
        # row still dies either way. It pins the expression allowlist over a
        # chained `env:`, which is worth a row, and it pins nothing else.
        "B4-pull-request-target-run-fetch-shell-chain",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        env:\n"
         "          B: ${{ github.event.after }}\n"
         "          A: origin $B\n"
         '        run: git fetch --depth=1 $A && git checkout FETCH_HEAD\n'
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "build the fetch's arguments out of a second env value, so the name "
        "the script reads is one hop from the name that matters",
    ),
    Mutation(
        # The same hazard as B4-pull-request-target-script-input written on
        # one line. `_step_scripts` folded in only the `with:` values spanning
        # more than one line, on the reasoning that a program has newlines in
        # it, so a `script:` short enough to fit on one was never read at all.
        # The real action at its real SHA, so C4's pin sweep is not the thing
        # that catches this.
        "B4-pull-request-target-script-input-one-line",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        uses: actions/github-script"
         "@60a0d83039c74a4aee543508d2ffcb1c3799cdea # v7.0.1\n"
         "        with:\n"
         "          script: await exec.exec('git', ['fetch', 'origin', "
         "context.payload.pull_request.head.sha])\n"
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "write the fetch as a one-line `script:`, which is how anybody writes "
        "a script that fits on one line",
    ),
    Mutation(
        # The same one-line value wearing a multi-line coat. A `>-` scalar is
        # folded to one line by the YAML parser -- folding it is what the
        # scalar means -- so a filter keyed on the newline read it as an
        # ordinary input while it looks like a program in the file, which is
        # the worse half of the pair: a reviewer sees a program and the test
        # does not.
        "B4-pull-request-target-script-input-folded",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        uses: actions/github-script"
         "@60a0d83039c74a4aee543508d2ffcb1c3799cdea # v7.0.1\n"
         "        with:\n"
         "          script: >-\n"
         "            await exec.exec('git', ['fetch', 'origin',\n"
         "            context.payload.pull_request.head.sha])\n"
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "reflow the same one-line script across two lines with a folded "
        "scalar, which reads as line-length housekeeping",
    ),
    Mutation(
        # `context.payload.after` is `${{ github.event.after }}` in the
        # language `actions/github-script` actually runs, and it interpolates
        # nothing, so no expression allowlist ever sees it. Distinct from the
        # two rows above on purpose: their scripts say `pull_request.head`,
        # which the literal backstop matches, and this one says none of the
        # three words. What kills it is the `context` allowlist and nothing
        # else.
        "B4-pull-request-target-script-context-payload",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        uses: actions/github-script"
         "@60a0d83039c74a4aee543508d2ffcb1c3799cdea # v7.0.1\n"
         "        with:\n"
         "          script: |\n"
         "            await exec.exec('git', ['fetch', 'origin',\n"
         "              context.payload.after]);\n"
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "name the head through the payload object the script is handed, "
        "rather than through an expression anything here can read",
    ),
    Mutation(
        # A step whose `shell:` is not a shell. `os.environ["REV"]` is a read
        # of REV that writes no `$REV` and calls no `printenv`, so the pickup
        # folds nothing in and every scan over the script comes back empty.
        # The answer is not to learn Python -- what this step reaches is
        # answerable without reading the program.
        #
        # Which rule answers it was miscredited here until 2026-09-19. The
        # comment claimed the environment refusal; measured, the expression
        # allowlist gets there first, on
        # `github.event.pull_request.head.sha` in REV's value. The
        # environment refusal is pinned by
        # B4-pull-request-target-run-carried-literal and by nothing else.
        "B4-pull-request-target-run-fetch-python-shell",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        shell: python\n"
         "        env:\n"
         "          REV: ${{ github.event.pull_request.head.sha }}\n"
         "        run: |\n"
         "          import os, subprocess\n"
         '          subprocess.run(["git", "fetch", "origin", os.environ["REV"]])\n'
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "write the fetch in Python, which `shell:` makes a supported thing to "
        "do and which no scan over shell syntax reads",
    ),
    Mutation(
        # The same read without declaring a `shell:` at all: one `python3 -c`
        # under the default bash and the environment is reached by a program
        # this file does not parse. It is the row that says the rule cannot be
        # about which shell the step names -- any program a script starts
        # inherits the whole environment without naming a field of it.
        "B4-pull-request-target-run-fetch-inline-interpreter",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        env:\n"
         "          REV: ${{ github.event.pull_request.head.sha }}\n"
         "        run: |\n"
         "          python3 -c 'import os, subprocess; "
         "subprocess.run([\"git\", \"fetch\", \"origin\", os.environ[\"REV\"]])'\n"
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "reach the environment through an inline interpreter, under the "
        "default shell, with nothing in the step declaring anything unusual",
    ),
    Mutation(
        # GitHub reads an index into a context as the property access it is,
        # so this is `github.event.pull_request.head.sha` spelled so that
        # `pull_request\.head` does not match it. One pair of brackets.
        "B4-pull-request-target-run-fetch-index-syntax",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        run: |\n"
         "          git fetch --depth=1 origin "
         "\"${{ github.event.pull_request['head'].sha }}\"\n"
         "          git checkout FETCH_HEAD\n"
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "index the property rather than naming it, which GitHub resolves to "
        "the same field and a pattern over dots does not match",
    ),
    Mutation(
        # The payload as a file. `GITHUB_EVENT_PATH` holds the whole webhook
        # event on disk, so the head SHA is reachable with no expression for
        # the script allowlist to read and no `context` property for the
        # JavaScript rule -- the same field, in the one language neither of
        # them parses. Found by an adversarial pass over the allowlist that
        # replaced the denylist, not by the review that prompted it.
        "B4-pull-request-target-run-fetch-event-file",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        run: |\n"
         "          REV=$(jq -r .after \"$GITHUB_EVENT_PATH\")\n"
         "          git fetch --depth=1 origin \"$REV\"\n"
         "          git checkout FETCH_HEAD\n"
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "read the head out of the event file, which names no expression and "
        "no context property and so reaches neither allowlist",
    ),
    Mutation(
        # The same file read from a `script:` input, where `process.env`
        # rather than a shell gets at the path. Separate row because the two
        # go through different halves of the read: this one is only visible
        # at all because `_step_scripts` folds `with:` values in.
        "B4-pull-request-target-script-event-file",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        uses: actions/github-script"
         "@60a0d83039c74a4aee543508d2ffcb1c3799cdea # v7.0.1\n"
         "        with:\n"
         "          script: |\n"
         "            const fs = require('fs');\n"
         "            const ev = JSON.parse(fs.readFileSync("
         "process.env.GITHUB_EVENT_PATH, 'utf8'));\n"
         "            await exec.exec('git', ['fetch', 'origin', ev.after]);\n"
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "read the head out of the event file from JavaScript, which builds "
        "its own path to the payload rather than using the one it is handed",
    ),
    Mutation(
        # Expressions are case-insensitive to GitHub and a Python regex is
        # not. Same field as B4-pull-request-target-run-fetch-after, same
        # runner behaviour, shifted key.
        "B4-pull-request-target-run-fetch-upper-case",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        run: |\n"
         "          git fetch --depth=1 origin \"${{ GITHUB.EVENT.AFTER }}\"\n"
         "          git checkout FETCH_HEAD\n"
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "shift the case of an event field, which changes nothing about what "
        "the runner resolves and everything about what a regex matches",
    ),
    Mutation(
        # `github.event.before` is the commit the push moved *from* on a
        # `synchronize`. It is the pull request's code one commit back, which
        # the fork also wrote, and it names none of `pull_request`, `head` or
        # `merge`. This row and the one below are the two fields that made
        # widening the denylist look like the fix.
        "B4-pull-request-target-run-fetch-before",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        run: |\n"
         "          git fetch --depth=1 origin \"${{ github.event.before }}\"\n"
         "          git checkout FETCH_HEAD\n"
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "fetch the commit the push moved from, which is the same fork's code "
        "under a field name that sounds like the base",
    ),
    Mutation(
        # The merge commit GitHub computes for the pull request: the fork's
        # code merged into the base, which is the fork's code. Spelled through
        # `pull_request` but not through `pull_request.head`, so the literal
        # backstop does not match it either.
        "B4-pull-request-target-run-fetch-merge-commit",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        run: |\n"
         "          git fetch --depth=1 origin "
         "\"${{ github.event.pull_request.merge_commit_sha }}\"\n"
         "          git checkout FETCH_HEAD\n"
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "fetch the computed merge commit, which contains the fork's code and "
        "is not spelled `head` anywhere",
    ),
    Mutation(
        # The fetch verb built two hops deep out of `env:`, so that nothing
        # in the script says `fetch` at all. This was the row credited with
        # pinning the loop around the pickup, and that was true only while a
        # gate stood in front of this half: the loop had to run twice for the
        # gate to open. The gate is gone, and measured 2026-09-19 so is the
        # pin -- the head is in the `run:` line in plain sight, the expression
        # allowlist reads it there, and neutering the pickup entirely leaves
        # this row KILLED. Nothing pins that loop now, which the test's
        # docstring says in as many words rather than leaving a row to imply
        # otherwise. The shape stays because two-hop verb laundering is a
        # thing somebody will write.
        "B4-pull-request-target-run-fetch-verb-chain",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        env:\n"
         "          VERB: fetch\n"
         "          CMD: git $VERB\n"
         '        run: $CMD --depth=1 origin "${{ github.event.after }}"\n'
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "build the fetch verb itself out of two env values, so the step's "
        "script never contains a word this test is watching for",
    ),
    Mutation(
        # The fetch verb itself, laundered through an index into `env`. The
        # gate that used to stand in front of this half matched four words,
        # and `${{ env['CMD'] }}` is none of them: GitHub substitutes the verb
        # in before the shell starts, so the YAML this file reads says `fetch`
        # nowhere. Two rows already launder the verb through a shell variable;
        # this one launders it through an expression, which no pickup keyed on
        # `$NAME` can follow because there is no `$CMD` to find. Killed by the
        # expression allowlist, which reads `env['CMD']` as `env.cmd` -- not a
        # recognised expression -- and the head on the same line.
        "B4-pull-request-target-run-verb-indexed-env",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        env:\n"
         "          CMD: git fetch origin\n"
         "        run: ${{ env['CMD'] }} ${{ github.event.after }} && "
         "git reset --hard FETCH_HEAD\n"
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "assemble the command out of an env value indexed by name, which is "
        "the tidying that keeps a long `run:` line under the margin",
    ),
    Mutation(
        # The same laundering with an interpreter instead of an expression.
        # `os.environ["CMD"].split()` builds the argv out of the environment in
        # a language nothing here parses, so neither the verb nor the ref is in
        # any text a pattern could read. It is
        # B4-pull-request-target-run-fetch-python-shell one field further along
        # -- there the ref was in Python and the verb was in the open, here both
        # are -- and it dies on the same assertion, which is the answer to the
        # whole family: the rule never needed to know what the program does.
        "B4-pull-request-target-run-verb-python-env",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        shell: python\n"
         "        env:\n"
         "          CMD: git fetch origin\n"
         "          REV: ${{ github.event.after }}\n"
         "        run: |\n"
         "          import os, subprocess\n"
         '          subprocess.run(os.environ["CMD"].split() + '
         '[os.environ["REV"]])\n'
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "move both halves of the command into the environment and assemble "
        "them in Python, which `shell:` makes a supported thing to do",
    ),
    Mutation(
        # The payload file reached by its path rather than by its variable.
        # `GITHUB_EVENT_PATH` is a convenience the runner sets;
        # `$RUNNER_TEMP/_github_workflow/event.json` is where it points, and a
        # step that writes the path out reads the same bytes while naming the
        # variable nowhere. Separate from B4-pull-request-target-run-fetch-
        # event-file for the reason that row exists at all: the same read, one
        # spelling along, is how this half has been wrong every round.
        "B4-pull-request-target-run-event-file-path",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        run: |\n"
         '          REV=$(jq -r .after "$RUNNER_TEMP/_github_workflow/'
         'event.json")\n'
         '          git fetch --depth=1 origin "$REV"\n'
         "          git reset --hard FETCH_HEAD\n"
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "read the event file by its path, which is what a script does when "
        "it wants the payload from somewhere the variable is not set",
    ),
    Mutation(
        # And the variable's name split in two. `'GITHUB' + '_EVENT_PATH'` is
        # one string to JavaScript and no string to a regex, which puts every
        # row that pins the name two characters from useless. The answer is not
        # a longer pattern over names: `process.env` is refused whole, because
        # a `script:` is handed `context`, `github`, `core` and its own inputs
        # and has no business in the process environment at all. Pinned to the
        # real action's SHA so C4's sweep is not what catches this.
        #
        # Split before the underscore rather than after `GITHUB_EVENT_`, which
        # is where it was until 2026-09-19 and which aimed this row at the
        # wrong rule: `GITHUB_EVENT_` is a reserved-prefix word and
        # `_SAFE_RUNNER_VARIABLES` refuses it, so the row died on the
        # runner-variable allowlist and would have died there with
        # `_EVENT_PAYLOAD_FILE` deleted outright. `'GITHUB'` alone is not a
        # reserved-prefix word, so the accessor is now the only thing deciding.
        "B4-pull-request-target-script-event-file-split",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        uses: actions/github-script"
         "@60a0d83039c74a4aee543508d2ffcb1c3799cdea # v7.0.1\n"
         "        with:\n"
         "          script: |\n"
         "            const fs = require('fs');\n"
         "            const p = JSON.parse(fs.readFileSync("
         "process.env['GITHUB' + '_EVENT_PATH']));\n"
         "            await exec.exec('git', ['fetch', 'origin', p.after]);\n"
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "build the environment variable's name out of two literals, which is "
        "what a minifier does and what nothing reading for the name sees",
    ),
    Mutation(
        # The accessor itself indexed rather than dotted. `process['env']` is
        # `process.env` to JavaScript and not `process.env` to a pattern that
        # wanted a literal dot, so the row above could be repaired by writing
        # one more bracket. This is the row that says the rule is the accessor
        # and not its punctuation: `process` reaching `env` by either route is
        # refused, and the name it goes on to build is not read at all. The
        # name is split off the reserved prefix for the reason the row above
        # gives, so that this one is decided by the accessor too.
        "B4-pull-request-target-script-event-file-indexed-accessor",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        uses: actions/github-script"
         "@60a0d83039c74a4aee543508d2ffcb1c3799cdea # v7.0.1\n"
         "        with:\n"
         "          script: |\n"
         "            const fs = require('fs');\n"
         "            const p = JSON.parse(fs.readFileSync("
         "process['env']['GITHUB' + '_EVENT_PATH']));\n"
         "            await exec.exec('git', ['fetch', 'origin', p.after]);\n"
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "index the accessor instead of dotting it, which is the same read "
        "with the one character a pattern over `process.env` required",
    ),
    Mutation(
        # The directory half globbed. `_github_*` is the runner's payload
        # directory and is not the string `_github_workflow`, so the path row
        # above could be repaired with a wildcard and no string arithmetic
        # anywhere. This pins the prefix and `RUNNER_TEMP` together: the
        # payload lives under that variable and nowhere else, so naming the
        # variable is naming the file whatever the rest of the path says.
        "B4-pull-request-target-run-event-file-glob",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        run: |\n"
         '          REV=$(jq -r .after "$RUNNER_TEMP"/_github_*/*.json)\n'
         '          git fetch --depth=1 origin "$REV"\n'
         "          git reset --hard FETCH_HEAD\n"
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "glob the runner's payload directory, which reaches the file without "
        "writing any of the names the path was pinned by",
    ),
    Mutation(
        # The payload named by neither variable. `GITHUB_EVENT_PATH` and
        # `RUNNER_TEMP` are both off `_SAFE_RUNNER_VARIABLES`, so every row
        # above that reaches the file through one of them dies on the
        # runner-variable allowlist and would die there with
        # `_EVENT_PAYLOAD_FILE` deleted -- measured by neutering that
        # assertion, at which all seven still went red. This is the row that
        # pins the rule: the path a GitHub-hosted Linux runner actually uses,
        # written out, with no `GITHUB_`- or `RUNNER_`-prefixed word anywhere
        # for the allowlist to read. The directory and the file name are what
        # is left, and they are what the rule refuses.
        "B4-pull-request-target-run-event-file-literal-path",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        run: |\n"
         "          REV=$(jq -r .after /home/runner/work/_temp/"
         "_github_workflow/event.json)\n"
         '          git fetch --depth=1 origin "$REV"\n'
         "          git reset --hard FETCH_HEAD\n"
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "read the payload at the literal path the runner puts it at, which "
        "names no variable and so reaches no allowlist over names",
    ),
    Mutation(
        # That path with the directory component globbed, which is the row
        # above repaired by one character. `_github_workflow` is the only
        # thing under `$RUNNER_TEMP` and `*` is shorter to write, so this
        # reads the same bytes while naming neither variable, neither
        # prefix, nor `event.json`. It is the reason the directory half of
        # `_EVENT_PAYLOAD_FILE` stopped claiming to be complete: the claim
        # was that the payload lives under `RUNNER_TEMP` and nowhere else,
        # which is true, and that a step therefore has to name the variable
        # to reach it, which is not -- the variable has a value and the
        # value can be typed out.
        "B4-pull-request-target-run-event-file-temp-glob",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        run: |\n"
         "          REV=$(jq -r .after /home/runner/work/_temp/*/*.json)\n"
         '          git fetch --depth=1 origin "$REV"\n'
         "          git reset --hard FETCH_HEAD\n"
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "glob one directory further up the runner's temporary tree, which "
        "is the same read with the only component a pattern was watching "
        "for replaced by a star",
    ),
    Mutation(
        # And the same directory seen from inside a container, where the
        # path has no underscore in it anywhere. A `container:` job gets the
        # runner's `_github_workflow` directory bind-mounted at
        # `/github/workflow`, so the payload is at `/github/workflow/
        # event.json` and a glob over it names nothing the runner's own path
        # spelled. Separate row from the one above because it pins a
        # separate alternative: they share no substring.
        "B4-pull-request-target-run-event-file-container-mount",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        run: |\n"
         "          REV=$(jq -r .after /github/workflow/*.json)\n"
         '          git fetch --depth=1 origin "$REV"\n'
         "          git reset --hard FETCH_HEAD\n"
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "read the payload at the path a container job sees it at, which is "
        "the same file the runner mounted and none of the words the "
        "runner's own path is written in",
    ),
    Mutation(
        # And the same reach in the other language a step can be written in.
        # An earlier round claimed `shell: python` was handled while reading
        # only the JavaScript accessor, so `os.environ["GITHUB_EVENT_" +
        # "PATH"]` was the split name with nothing watching for it. Python's
        # accessors are refused for the reason JavaScript's are: a `run:` step
        # is handed its `env:` block as ordinary variables, so a body that
        # goes to the process environment programmatically is going around
        # the route it was given.
        "B4-pull-request-target-python-event-file-environ",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        shell: python\n"
         "        run: |\n"
         "          import json, os, subprocess\n"
         '          rev = json.load(open(os.environ["GITHUB" + '
         '"_EVENT_PATH"]))["after"]\n'
         '          subprocess.run(["git", "fetch", "origin", rev])\n'
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "read the payload from Python, where the variable's name splits off "
        "the reserved prefix the same way and the accessor is `os.environ`",
    ),
    Mutation(
        # The accessor imported under another name, which is the row above
        # repaired by an import. `from os import environ as e` binds the
        # mapping to a one-letter name and reads it as `e.items()`, so
        # `os.environ` appears nowhere, the subscript a pattern wanted is on
        # a name this file cannot predict, and the word `environ` survives
        # only at the import. The rule reads the bare word for that reason.
        "B4-pull-request-target-python-environ-aliased-import",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        shell: python\n"
         "        run: |\n"
         "          from os import environ as e\n"
         '          rev = [v for k, v in e.items() '
         'if k.endswith("HEAD_REF")][0]\n'
         "          import subprocess\n"
         '          subprocess.run(["git", "fetch", "origin", rev])\n'
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "alias the environment mapping on import and match its keys by "
        "suffix, which spells neither the accessor nor the variable",
    ),
    Mutation(
        # The environment read whole, in the shell. `env` hands the step
        # every variable the runner set, `GITHUB_HEAD_REF` among them, and
        # the only spelling of that name on the line is lower case -- which
        # is not the name the shell would expand and is exactly the name
        # `grep -i` matches. The allowlist over the names reads nothing
        # here, and this was green until 2026-09-19. It is the row
        # `_ENVIRONMENT_ENUMERATION` exists for, and the six that follow are
        # its other spellings, one row each because they share no substring.
        "B4-pull-request-target-run-env-dump-grep",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        run: |\n"
         "          B=$(env | grep -i '^github_head_ref=' | sed -e 's/.*=//')\n"
         '          git fetch origin "$B" && git checkout FETCH_HEAD\n'
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "dump the environment and pick the branch out of it by a "
        "case-insensitive match, which is the shortest way to read a "
        "variable without writing the name the shell knows it by",
    ),
    Mutation(
        # The same dump from the program this file already refuses *with* an
        # operand. `printenv GITHUB_HEAD_REF` names the variable and dies on
        # the allowlist over the names; `printenv` alone names nothing and
        # prints the same value. The pair is the whole argument for this
        # rule in two rows.
        "B4-pull-request-target-run-printenv-dump",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        run: |\n"
         "          B=$(printenv | grep -i '^github_head_ref=' "
         "| sed -e 's/.*=//')\n"
         '          git fetch origin "$B" && git checkout FETCH_HEAD\n'
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "use the other program that prints the environment, with the "
        "operand that would have named a variable left off",
    ),
    Mutation(
        # The shell builtin that prints the table, rather than the program
        # that prints the environment. `declare -p` is a dump and `declare
        # -a xs` is a declaration, which is why the rule is anchored on what
        # follows the word rather than on the word.
        "B4-pull-request-target-run-declare-dump",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        run: |\n"
         "          B=$(declare -p | grep -i 'github_head_ref=' "
         "| sed -e 's/.*=//')\n"
         '          git fetch origin "$B" && git checkout FETCH_HEAD\n'
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "print the shell's own variable table, which is the environment "
        "plus the shell's and is one flag away from an assignment",
    ),
    Mutation(
        # And the builtin whose job is setting a variable, printing them
        # instead. `export -p` and `export PATH=x` are the same word in
        # opposite directions, and one of them is a read.
        "B4-pull-request-target-run-export-dump",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        run: |\n"
         "          B=$(export -p | grep -i 'github_head_ref=' "
         "| sed -e 's/.*=//')\n"
         '          git fetch origin "$B" && git checkout FETCH_HEAD\n'
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "print the exported variables, which is the environment under the "
        "name of the builtin that sets it",
    ),
    Mutation(
        # The word every script in this repository already writes, with the
        # options left off. `set -euo pipefail` is a directive and a bare
        # `set` is a dump of everything the shell has, and a rule that
        # cannot tell them apart is a rule nobody can keep.
        "B4-pull-request-target-run-set-dump",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        run: |\n"
         "          B=$(set | grep -i '^github_head_ref=' | sed -e 's/.*=//')\n"
         '          git fetch origin "$B" && git checkout FETCH_HEAD\n'
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "print everything the shell has, which is what `set` with no "
        "operands does and what `set -euo pipefail` does not",
    ),
    Mutation(
        # The names alone, expanded indirectly afterwards. `compgen -e`
        # prints what is exported and `${!N}` reads it, so neither the dump
        # nor the expansion has the variable's name written in it anywhere.
        "B4-pull-request-target-run-compgen-dump",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        run: |\n"
         "          for N in $(compgen -e); do\n"
         "            case $N in [Gg][Ii][Tt][Hh][Uu][Bb]_[Hh][Ee][Aa][Dd]*)"
         ' B="${!N}";; esac\n'
         "          done\n"
         '          git fetch origin "$B" && git checkout FETCH_HEAD\n'
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "list the names of the exported variables and expand the match "
        "indirectly, which reaches the value with neither half written down",
    ),
    Mutation(
        # The safe side of the rule above, and the reason it is anchored on
        # a terminator rather than on the words. Every one of these lines
        # contains a word the enumeration rule reads and none of them is a
        # read: `set -euo pipefail` is a directive, `export PATH=...` and
        # `declare -a` are assignments, `env FOO=1 cmd` sets a variable for
        # one command, and `/usr/bin/env` is a shebang's program. KILLED
        # here means the rule has been widened into a ban on the words,
        # which is a suite that reds on the first ordinary shell script
        # somebody writes.
        "B4-pull-request-target-run-env-ordinary-shell",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Build the tools\n"
         "        run: |\n"
         "          set -euo pipefail\n"
         '          export PATH="$PWD/bin:$PATH"\n'
         "          declare -a tools=(jq yq)\n"
         '          echo "${tools[@]}" > tools.txt\n'
         "          env GOFLAGS=-mod=readonly ./bin/build\n"
         "          /usr/bin/env python3 ./bin/check.py\n"
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "write an ordinary shell script that sets a variable, exports a "
        "path, declares an array and runs a command under `env`, naming "
        "nothing the pull request decides",
        must_survive=True,
    ),
    Mutation(
        # `shell:` is a command line, and nothing here opened it until
        # 2026-09-19. The runner builds the step's command by substituting
        # the script's temporary file in at `{0}`, so everything before that
        # placeholder runs first -- and a `run:` of `true` is enough to make
        # the step look like it does nothing. Every scan in the test reads
        # `run:` and the `with:` values, and this field carried a checkout
        # of the pull request past all of them.
        "B4-pull-request-target-step-shell-command-line",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Lint\n"
         '        shell: bash -c "gh pr checkout $PR_NUMBER && ./ci.sh" {0}\n'
         "        env:\n"
         "          PR_NUMBER: ${{ github.event.pull_request.number }}\n"
         "          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}\n"
         '        run: "true"\n'
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "put the checkout in the step's `shell:` and leave the `run:` doing "
        "nothing, which reads as a step that configures its interpreter",
    ),
    Mutation(
        # And the same command line set once for the whole job, which is
        # where a reader is least likely to look for a program. A job's
        # `defaults.run.shell` is the command line every step of it runs
        # under, so this is the row above applied to steps that do not
        # mention `shell:` at all -- and a rule that read only
        # `steps[*].shell` would be a rule about where the author put it.
        "B4-pull-request-target-job-defaults-shell-command-line",
        ".github/workflows/risk_classify.yml",
        ("    runs-on: ubuntu-latest",
         "    runs-on: ubuntu-latest\n"
         "    env:\n"
         "      PR_NUMBER: ${{ github.event.pull_request.number }}\n"
         "    defaults:\n"
         "      run:\n"
         '        shell: bash -c "gh pr checkout $PR_NUMBER && ./ci.sh" {0}'),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "set the job's default shell to a command line that checks the pull "
        "request out, which runs once per step and appears in none of them",
    ),
    Mutation(
        # The safe side of both rows above. `shell: bash` and a
        # `defaults.run` block naming a shell and a working directory are
        # ordinary workflow, and folding those values into the scripts must
        # not red them. KILLED here means `shell:` has been turned into a
        # field a `pull_request_target` job may not set, which is not what
        # any of this is about.
        "B4-pull-request-target-shell-ordinary",
        ".github/workflows/risk_classify.yml",
        ("    runs-on: ubuntu-latest",
         "    runs-on: ubuntu-latest\n"
         "    defaults:\n"
         "      run:\n"
         "        shell: bash\n"
         "        working-directory: scripts"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "name the job's default shell and working directory, which is the "
        "ordinary use of the block the rule above reads",
        must_survive=True,
    ),
    Mutation(
        # `git remote add` and `git remote update`, which put the fork's code
        # on the runner using none of the four words the old gate watched for.
        # This is the row that says that gate could not have been repaired by
        # adding a fifth: `remote update` is a fetch spelled as configuration,
        # and the three rows after it are three more spellings from three more
        # tools. What catches it reads what the step names -- the fork's clone
        # URL, and its head branch -- rather than what the step does.
        "B4-pull-request-target-run-remote-update",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        run: |\n"
         '          git remote add pr "${{ github.event.pull_request.head'
         '.repo.clone_url }}"\n'
         "          git remote update pr\n"
         '          git reset --hard "pr/${{ github.event.pull_request.head'
         '.ref }}"\n'
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "add the fork as a remote and update it, which is how anybody who "
        "wants the branch rather than the commit writes this",
    ),
    Mutation(
        # The fork's tree as a tarball over HTTP. No git verb at all: the API
        # serves a commit as an archive, `tar xz` unpacks it, and the next line
        # runs something out of it. A reviewer scanning for `git` sees nothing,
        # and the gate saw nothing either. Killed by the two expressions in the
        # URL, which is the point -- the URL has to say which repository and
        # which commit, and saying that is naming the pull request.
        "B4-pull-request-target-run-tarball",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        run: |\n"
         '          curl -sL "https://api.github.com/repos/${{ github.event'
         '.pull_request.head.repo.full_name }}/tarball/${{ github.event.after '
         '}}" | tar xz\n'
         "          ./kube-agents/ci/run.sh\n"
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "download the commit as an archive instead of cloning it, which is "
        "the fast way to get one tree without a git history",
    ),
    Mutation(
        # The same code arriving through a package manager. `pip install
        # "git+https://...@<sha>"` clones the fork and runs its build backend
        # -- arbitrary code at install time, on a runner holding a writable
        # token -- and the word `git` appears only inside a URL scheme. A gate
        # over verbs would have to know pip's requirement grammar to see it.
        # The allowlist only has to notice that the requirement names a
        # repository the pull request chose.
        "B4-pull-request-target-run-pip-vcs",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        run: |\n"
         '          pip install "git+https://github.com/${{ github.event'
         '.pull_request.head.repo.full_name }}@${{ github.event.after }}"\n'
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "install the pull request as a package, which reads as testing the "
        "change the way a consumer would get it",
    ),
    Mutation(
        # And through a build context. `docker build "https://host/repo.git
        # #<sha>"` is a documented remote context: the daemon clones the
        # repository at that ref and builds it, so every `RUN` in the fork's
        # Dockerfile executes. Fourth tool, fourth grammar, same one-line
        # answer. Four rows for four tools rather than one representative one,
        # because "there are more of these than the gate knew" is a claim that
        # has to be able to fail.
        "B4-pull-request-target-run-docker-context",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        run: |\n"
         '          docker build "https://github.com/fork/kube-agents.git'
         '#${{ github.event.after }}"\n'
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "hand docker a remote build context, which is one line shorter than "
        "checking the tree out and building it",
    ),
    Mutation(
        # An `env:` block that is an expression rather than a mapping. GitHub
        # evaluates `env: ${{ fromJSON(...) }}` into the environment at run
        # time, and `_env_values` returned {} for it -- so every rule about
        # what a step's environment may carry read a whole environment built
        # out of the head as "this step carries nothing". Folded in whole under
        # a placeholder name now, which puts it in front of the expression
        # allowlist, where `fromJSON(...)` is refused for being unresolvable.
        # Same answer `with:` gets when it is not a mapping either.
        "B4-pull-request-target-env-expression",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        env: ${{ fromJSON(format('{{\"REV\":\"{0}\"}}', "
         "github.event.after)) }}\n"
         '        run: git fetch --depth=1 origin "$REV" && '
         "git reset --hard FETCH_HEAD\n"
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "build the whole `env:` block from one expression, which is how a "
        "workflow shares one environment between several steps",
    ),
    Mutation(
        # A control on the substitution rather than on a hole. GitHub resolves
        # `env.SAFE`, `env.safe` and `Env.SAFE` to one value, and this is the
        # allowlisted ref reached through the third spelling -- the same ref,
        # the same checkout, nothing weakened. A case-sensitive substitution
        # left it unexpanded, and an unexpanded expression is off the ref
        # allowlist, so the suite reddened on a workflow that had done nothing
        # wrong. That is the failure mode a `must_survive` row exists for: a
        # suite that reds on a harmless change is a suite people learn to
        # override. KILLED here means the substitution has gone
        # case-sensitive again.
        "B4-pull-request-target-checkout-ref-env-case",
        ".github/workflows/risk_classify.yml",
        ("        with:\n"
         "          ref: ${{ github.event.repository.default_branch }}",
         "        env:\n"
         "          SAFE: ${{ github.event.repository.default_branch }}\n"
         "        with:\n"
         "          ref: ${{ Env.SAFE }}"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "name an env value in the case GitHub accepts and this file did not, "
        "carrying the one ref the allowlist holds",
        must_survive=True,
    ),
    Mutation(
        # The ref space copied rather than enumerated. `git clone --mirror`
        # sets up a refmap of `+refs/*:refs/*`, so it brings
        # `refs/pull/N/head` onto the runner while writing no refspec, no
        # `ls-remote` and no HTTP path -- none of the four spellings the
        # enumeration rule was built out of. `for-each-ref` in the clone
        # reads the head SHA back out.
        "B4-pull-request-target-run-clone-mirror",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        env:\n"
         "          PR_NUMBER: ${{ github.event.pull_request.number }}\n"
         "        run: |\n"
         '          git clone --mirror "https://github.com/'
         '$GITHUB_REPOSITORY" m\n'
         "          SHA=$(git -C m for-each-ref --format='%(objectname)"
         " %(refname)' | grep \"/$PR_NUMBER/head\" | cut -d' ' -f1)\n"
         '          git fetch origin "$SHA"\n'
         "          git reset --hard FETCH_HEAD\n"
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "take a mirror clone and read the head out of it, which writes none "
        "of the four spellings the enumeration rule collected",
    ),
    Mutation(
        # The same clone with the option abbreviated. Git takes any
        # unambiguous prefix of a long option, and `--mirror` is the only
        # `git clone` long option beginning with `--m`, so `git clone --m` is
        # `git clone --mirror` and `--mir` and `--mirr` are too -- all of them
        # green against a pattern that wanted the full spelling. `--m` rather
        # than `--mir` because it is the shortest rung git accepts: a row at
        # the bottom of the ladder dies wherever the pattern is cut, and a row
        # further up survives a pattern narrowed beneath it.
        "B4-pull-request-target-run-clone-mirror-abbreviated",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        env:\n"
         "          PR_NUMBER: ${{ github.event.pull_request.number }}\n"
         "        run: |\n"
         '          git clone --m "https://github.com/'
         '$GITHUB_REPOSITORY" m\n'
         "          SHA=$(git -C m for-each-ref --format='%(objectname)"
         " %(refname)' | grep \"/$PR_NUMBER/head\" | cut -d' ' -f1)\n"
         '          git fetch origin "$SHA"\n'
         "          git reset --hard FETCH_HEAD\n"
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "abbreviate the mirror flag, which git resolves to the same option "
        "and a pattern over its full spelling does not see",
    ),
    Mutation(
        # And the neighbouring flag, from the safe side. `git clone --bare`
        # copies the branches an ordinary clone does and not the pull
        # namespace, so refusing it would be a rule about the shape of a
        # clone rather than about what the clone reaches. KILLED here means
        # the mirror pattern has been widened to `--(?:mirror|bare)`, and a
        # release step that takes a bare clone of this repository now reds.
        # The prefix ladder the pattern carries is not that widening: it runs
        # `--m` to `--mirror` and `--bare` is on none of its rungs.
        "B4-pull-request-target-run-clone-bare",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Archive the default branch\n"
         "        run: |\n"
         '          git clone --bare "https://github.com/'
         '$GITHUB_REPOSITORY" archive.git\n'
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "take a bare clone of this repository, which is what an archiving "
        "step does and which reaches no ref an ordinary clone does not",
        must_survive=True,
    ),
    Mutation(
        # And the ladder's neighbours, from the safe side. The mirror rule
        # refuses every prefix of `--mirror` down to `--m`, and the price of
        # that is a pattern that has to stop at the end of the option: `--m`
        # also begins `--merges`, `--max-count` and `--milestone`. KILLED here
        # means the ladder lost its anchor and every `--m...` option a
        # workflow writes now reds.
        #
        # Today the anchor has a live control as well, which is worth saying
        # so that this row is not read as the only one: `auto-assign-
        # milestone.yml` writes `gh pr edit --milestone` on this same trigger,
        # so replacing the ladder with a bare `--m` reds that carrier before a
        # single mutation runs and the harness refuses the sweep outright.
        # This row is what remains when a carrier changes, and that is
        # measured rather than asserted -- with the carrier's flag renamed and
        # the anchor dropped in the same tree, the baseline goes green again
        # and this row reports OVERSHOT.
        "B4-pull-request-target-run-log-m-flags",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Summarise the default branch\n"
         "        run: |\n"
         "          git log --merges --max-count=1 --format='%H'\n"
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "log one merge commit from the checkout, whose two flags begin `--m` "
        "and are no abbreviation of `--mirror`",
        must_survive=True,
    ),
    Mutation(
        # The head branch with nothing naming it. `GITHUB_HEAD_REF` is set by
        # the runner on this trigger and holds the pull request's branch, so
        # this fetches the fork's code with no expression, no `context`, no
        # `env:` block, and none of the words `pull`, `head` or `merge`
        # reaching a pattern that reads for them. It was green.
        "B4-pull-request-target-run-runner-head-ref",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        run: |\n"
         '          git fetch origin "$GITHUB_HEAD_REF"\n'
         "          git reset --hard FETCH_HEAD\n"
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "fetch the branch the runner already put in the environment, which "
        "is the shortest way to write this and names nothing",
    ),
    Mutation(
        # And the fork itself, assembled out of two more of them.
        # `GITHUB_ACTOR` is whoever opened the pull request and is therefore
        # the owner half of the fork's clone URL, and `GITHUB_REPOSITORY`
        # trimmed at the slash is the other half. Separate row from the one
        # above because it pins a different entry off the list: repair the
        # allowlist by adding `GITHUB_HEAD_REF` back to it and this row still
        # dies.
        "B4-pull-request-target-run-runner-actor-fork",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Fetch the pull request\n"
         "        run: |\n"
         '          git fetch "https://github.com/$GITHUB_ACTOR/'
         '${GITHUB_REPOSITORY#*/}" "$GITHUB_HEAD_REF"\n'
         "          git reset --hard FETCH_HEAD\n"
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "build the fork's clone URL out of the author's login, which the "
        "runner hands every step whether or not the workflow asks",
    ),
    Mutation(
        # The runner's environment from the safe side. `GITHUB_REPOSITORY`
        # and `GITHUB_SHA` are what the base repository decides -- this
        # repository, and the head commit of its default branch on this
        # trigger -- and naming them in a log line is ordinary. KILLED here
        # means the allowlist above has been replaced by a refusal of the
        # prefix, which reds on a step doing nothing wrong.
        "B4-pull-request-target-run-runner-repository-name",
        ".github/workflows/risk_classify.yml",
        ("      - name: Set up Python",
         "      - name: Describe the build\n"
         "        run: |\n"
         '          echo "building ${GITHUB_REPOSITORY} at ${GITHUB_SHA}"\n'
         "\n"
         "      - name: Set up Python"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "log which repository and commit the job is building, naming two "
        "variables the pull request has no say in",
        must_survive=True,
    ),
    Mutation(
        # The image the job runs in, which is the job's code. Nothing read
        # `container:` until 2026-09-19: the steps below it are the live
        # carrier's own innocent ones, every rule over `run:`, `uses:` and
        # `ref:` passes, and the job runs inside something the fork pushed.
        # This is the row that says the rule is over the block rather than
        # over the steps.
        "B4-pull-request-target-container-image-fork",
        ".github/workflows/risk_classify.yml",
        ("    runs-on: ubuntu-latest",
         "    runs-on: ubuntu-latest\n"
         "    container:\n"
         "      image: ghcr.io/${{ github.event.pull_request.head.repo"
         ".full_name }}/runner:latest"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "run every step of the job inside an image the fork pushed, which "
        "no rule over the steps can see",
    ),
    Mutation(
        # The same image in GitHub's string shorthand. `container: <image>`
        # is `container: {image: <image>}`, and a rule that reads only the
        # mapping is a rule the shorthand walks past while looking like the
        # form the rule reads. Separate row because it pins
        # `_container_mapping` rather than the block walk: normalise the
        # string away and the row above still dies while this one lives.
        "B4-pull-request-target-container-image-shorthand",
        ".github/workflows/risk_classify.yml",
        ("    runs-on: ubuntu-latest",
         "    runs-on: ubuntu-latest\n"
         "    container: ghcr.io/${{ github.event.pull_request.head.repo"
         ".full_name }}/runner:latest"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "name the fork's image in the one-line form, which is the form "
        "somebody writes when the image is the only thing they are setting",
    ),
    Mutation(
        # The head in the container's `env:`. This row is decided by the
        # block rule above it and not by the merge into the steps: `_flatten`
        # holds the whole container block against the expression allowlist
        # before any step is read, so `${{ github.event.after }}` reds here
        # wherever in the block it sits. The row below is the one that pins
        # the merge.
        "B4-pull-request-target-container-env-head",
        ".github/workflows/risk_classify.yml",
        ("    runs-on: ubuntu-latest",
         "    runs-on: ubuntu-latest\n"
         "    container:\n"
         "      image: ubuntu:24.04\n"
         "      env:\n"
         "        REV: ${{ github.event.after }}"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "put the head in the container's environment, one level below the "
        "job's, where every step of the job still has it",
    ),
    Mutation(
        # The merge itself, which is the fourth level `_env_values` claimed
        # to consult and which nothing pinned until now. A container `env:`
        # is set in the container every step of the job runs in, so it
        # reaches the steps exactly as the job's own does -- but the rules
        # over the block are an expression allowlist and two literal
        # backstops, and `$GITHUB_HEAD_REF` is none of the three. It carries
        # no expression, it is not `pull_request.head`, and it names no ref
        # namespace, so the block passes it. What refuses it is the
        # runner-variable allowlist, which runs over the steps' environment
        # and is not applied to the container block's text -- so it sees this
        # value only because the merge put it there. Delete the merge and
        # this row lives while the one above still dies.
        "B4-pull-request-target-container-env-runner-head-ref",
        ".github/workflows/risk_classify.yml",
        ("    runs-on: ubuntu-latest",
         "    runs-on: ubuntu-latest\n"
         "    container:\n"
         "      image: ubuntu:24.04\n"
         "      env:\n"
         "        HEAD_BRANCH: $GITHUB_HEAD_REF"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "hand every step the head branch through the container's "
        "environment, in the one spelling the rules over the block itself "
        "do not read",
    ),
    Mutation(
        # And a service container, which is the same choice of image made
        # once per entry under a different key. It starts before the steps,
        # on the job's network, with the job's secrets available to whatever
        # the workflow hands it.
        "B4-pull-request-target-service-image-fork",
        ".github/workflows/risk_classify.yml",
        ("    runs-on: ubuntu-latest",
         "    runs-on: ubuntu-latest\n"
         "    services:\n"
         "      db:\n"
         "        image: ghcr.io/${{ github.event.pull_request.head.repo"
         ".full_name }}/db:latest"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "start a service container from the fork's image, which runs beside "
        "the steps rather than under them",
    ),
    Mutation(
        # The container from the safe side, and the reason the rule over it
        # is an expression allowlist rather than a refusal. A pinned public
        # image is the ordinary way to run a job with a toolchain in it, and
        # both live forms -- the mapping and the string -- have to stay
        # green. KILLED here means the block rule has been tightened into a
        # ban on `container:`, which is a suite that reds on a workflow doing
        # nothing wrong.
        "B4-pull-request-target-container-pinned-image",
        ".github/workflows/risk_classify.yml",
        ("    runs-on: ubuntu-latest",
         "    runs-on: ubuntu-latest\n"
         "    container:\n"
         "      image: ubuntu:24.04\n"
         "    services:\n"
         "      cache: redis:7"),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "run the job in a pinned public image with a pinned public service "
        "beside it, naming nothing that came from the pull request",
        must_survive=True,
    ),
    Mutation(
        "B4-pull-request-target-checkout-guard",
        "tests/conformance/test_B_write_path.py",
        ('if uses.startswith("actions/checkout"):',
         'if uses.startswith("actions/checkout-nonesuch"):'),
        "test_B4_no_pull_request_target_workflow_checks_out_the_pull_request",
        "stop `actions/checkout` matching its own name. The ref allowlist "
        "sits outside this guard and goes on reading every step, so what "
        "this removes is `saw_a_checkout` and the must-carry-a-`ref:` rule -- "
        "which is the vacuity `saw_a_checkout` exists to refuse",
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
        # Originally aimed at chart-release.yml, which main has since deleted;
        # the ghcr publisher has the identical permissions shape and the same
        # story -- an image pusher quietly gaining the credential that can
        # push to this repository.
        "B4-extra-contents-write-holder",
        ".github/workflows/docker-publish-ghcr.yml",
        ("      contents: read\n      packages: write\n",
         "      contents: write\n      packages: write\n"),
        "test_B4_contents_write_is_confined_to_the_release_path",
        "give the ghcr image publisher contents: write so it can cut a GitHub "
        "release alongside the OCI push -- an extra holder of the credential "
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
        # Indented for the same reason as A3-kubectl-kuberc-env above.
        ('            "GIT_ALLOW_PROTOCOL": "https",',
         '            "GIT_ALLOW_PROTOCOL": "https:ext",'),
        "test_C1_git_in_the_broker_cannot_execute_arbitrary_code",
        "re-admit the ext:: transport, which is the whole of the RCE: "
        "`git clone \'ext::sh -c <cmd>\'` runs <cmd> in the credential holder. "
        "The one-word widening is the shape the real regression would take",
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
        ('return ("gcloud", "kubectl", "git", *providers.Registry().executables)',
         'return ("gcloud", "kubectl", "git", "sh", *providers.Registry().executables)'),
        "test_C1_the_executor_refuses_an_executable_it_does_not_ship",
        "add sh to the allowlist, giving a compound command somewhere to land",
    ),
    # C1-share-process-namespace and C1-uid-collapse are retired WITH their
    # tests, not orphaned. Both attacked same-Pod mitigations the split-broker
    # layout needed -- a shared PID namespace, and the credential holder running
    # at the sandbox UID. #913 removed the thing they mitigated: nothing in the
    # agent Pod holds a credential any more, and the shell runs in a Pod of its
    # own. The replacement property (ShareProcessNamespace stays unset) is
    # asserted in the operator's own suite; duplicating it here would pin
    # someone else's invariant. What C1 asserts instead is the sandbox
    # ServiceAccount's missing Workload Identity annotation, which has its own
    # mutation above.
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
    #
    # The SHAPE half is no longer in that bind. It used to share a method with
    # the whole-internet violation, so it inherited the expectedFailure and no
    # mutation could report anything but SURVIVED against it. It is its own
    # test now, passing, and so has a real kill below.
    Mutation(
        "C1-egress-rule-without-destination",
        "k8s-operator/internal/testing/testdata/platform/expected/"
        "platformagent-egress-allowlist.yaml",
        ("""    - ports:
        - port: 443
          protocol: TCP
      to:
        - ipBlock:
            cidr: 140.82.112.0/20
""",
         """    - ports:
        - port: 443
          protocol: TCP
"""),
        "test_C1_the_rendered_egress_rules_are_shaped_to_deny_by_default",
        "strip the `to` from a rendered egress rule, which opens 443 to every "
        "destination while still looking like an allowlist entry",
    ),
    Mutation(
        "C1-cidr-guard-inert",
        "k8s-operator/internal/controller/platformagent_egress_policy.go",
        ("\tif reason := ipv4MappedRefusal(prefix, cidr); reason != \"\" {\n\t\treturn reason\n\t}\n", ""),
        "test_C1_every_operator_supplied_cidr_reaches_the_refusal_guards",
        "rename the 4-in-6 guard so every call site misses it; Go would not "
        "compile, but the point is that the conformance suite says so first",
    ),
    Mutation(
        "C1-gateway-oauth-shape",
        "charts/kube-agents/files/redactor.py",
        ('        text = cls.GCP_OAUTH_TOKEN_PATTERN.sub("[REDACTED_SECRET]", text)\n', ""),
        "test_C1_the_gateway_redactor_matches_the_leaked_credential_shapes",
        "drop the ya29 substitution from the chain while reordering it -- the "
        "pattern constant stays, so anything that greps for it is satisfied, "
        "and the shape gke-labs/kube-agents#603 measured leaves for the "
        "provider in the clear",
    ),
    Mutation(
        "C1-gateway-sa-exemption",
        "charts/kube-agents/files/redactor.py",
        ('r"[a-zA-Z0-9._%+\\-]+@(?!(?:[a-zA-Z0-9\\-]+\\.)*gserviceaccount\\.com(?!\\.?[\\w\\-]))"',
         'r"[a-zA-Z0-9._%+\\-]+@"'),
        "test_C1_the_gateway_redactor_leaves_ordinary_manifest_content_alone",
        "simplify the e-mail pattern by dropping the service-account exemption; "
        "every IAM principal in a tool result then reaches the model as "
        "[REDACTED_EMAIL], which is the over-eager shape that gets redaction "
        "turned off",
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
        ("    filtered = ''.join(\n"
         "        c for c in s if unicodedata.category(c) not in ('Cc', 'Cf', 'Cs', 'Zl', 'Zp')\n"
         "    )\n",
         "    filtered = s\n"),
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
        ("@sha256:3811ed13da874fba2ac99b6d492db9a203d34cb6dccf90d886948c00d0ccec09", ""),
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
        "C1-broker-colocation-flag-restored",
        "k8s-operator/internal/controller/platformagent_manifests.go",
        ("// buildPodTemplateSpec generates the shared PodTemplateSpec for Deployment and StatefulSet\n",
         "// buildPodTemplateSpec generates the shared PodTemplateSpec for Deployment and StatefulSet\n"
         "// TODO: honour splitCredentialBrokerPod again for single-node installs.\n"),
        "test_C1_the_credential_broker_is_its_own_deployment",
        "reintroduce a co-location switch by name. #913 made the broker's own "
        "Deployment unconditional, so the separation is topology rather than a "
        "setting; a flag coming back is the regression that turns it into a "
        "setting again, and it starts life looking like a harmless TODO",
    ),
    Mutation(
        "C1-sandbox-sa-annotated-for-workload-identity",
        "k8s-operator/internal/controller/shell_sandbox_manifests.go",
        ("""			Name:      shellSandboxServiceAccountName(agent),
			Namespace: agent.Namespace,
			Labels:    shellSandboxSelector(agent),
""",
         """			Name:      shellSandboxServiceAccountName(agent),
			Namespace: agent.Namespace,
			Labels:    shellSandboxSelector(agent),
			Annotations: map[string]string{
				"iam.gke.io/gcp-service-account": agentGSAEmail(agent),
			},
"""),
        "test_C1_the_sandbox_identity_carries_no_cloud_annotation",
        "annotate the sandbox ServiceAccount for Workload Identity, the one "
        "edit an operator would plausibly make to 'let the shell use gcloud'. "
        "GKE resolves WI by pod IP, so this hands every model-authored command "
        "a GSA token from the metadata server with no proxy in front of it -- "
        "and nothing else in the suite would notice",
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
        "k8s-operator/internal/testing/testdata/platform/expected/platformagent-scoped-sa.yaml",
        ("    resources:\n      - tokenreviews\n    verbs:\n      - create\n",
         "    resources:\n      - tokenreviews\n      - subjectaccessreviews\n    verbs:\n      - create\n"),
        "test_C5_the_tokenreview_role_is_the_narrowest_form_of_itself",
        "give the broker subjectaccessreviews alongside tokenreviews -- what "
        "binding system:auth-delegator would have handed it in one line, and an "
        "authorization oracle over the whole cluster",
    ),
    Mutation(
        "C5-binds-auth-delegator",
        "k8s-operator/internal/testing/testdata/platform/expected/platformagent-scoped-sa.yaml",
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
        # platformagent-ha.yaml, not platformagent.yaml: the leader Role's pods
        # rule renders only above one replica, and the HA fixture is the only
        # golden that sets it.
        "k8s-operator/internal/testing/testdata/platform/expected/platformagent-ha.yaml",
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
        ("            _sanitize_for_logging(principal.describe(), max_length=512),", "            \"-\","),
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
        ('return ("gcloud", "kubectl", "git", *providers.Registry().executables)',
         'return ("gcloud", "kubectl", "git", "/usr/bin/kubectl", *providers.Registry().executables)'),
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
        "C1-session-fence-selector-drift",
        "a2a/gateway/spawn.go",
        ('\tsessionRole = "a2a-session"', '\tsessionRole = "a2a-worker"'),
        "test_C1_the_session_fence_selects_the_pods_the_spawner_stamps",
        "rename the session pod's component label on the spawner side only -- "
        "the shape a rename that misses the other Go module takes. Both Go "
        "suites stay green and the operator's NetworkPolicy then selects no "
        "pod, which the API server reports as success",
    ),
    Mutation(
        "C1-session-pod-gets-a-second-token",
        "a2a/gateway/spawn.go",
        ("AutomountServiceAccountToken: ptr.To(false),",
         "AutomountServiceAccountToken: ptr.To(true),"),
        "test_C1_a_session_pod_carries_no_kubernetes_identity",
        "automount a SECOND token into a session pod, beside the bus token it "
        "is supposed to have. A session pod now names a ServiceAccount -- the "
        "callout resolves a Kubernetes identity, so it has to -- and this is "
        "the flip that turns that identity from inert into a cluster "
        "credential: the automounted token carries the API server's default "
        "audience, so unlike the projected bus token it authenticates against "
        "the API server, which the session fence's rule set does not account "
        "for",
    ),
    Mutation(
        "C1-session-token-loses-its-audience",
        "a2a/gateway/spawn.go",
        ("Audience:          lib.BusTokenAudience,", ""),
        "test_C1_a_session_pod_carries_no_kubernetes_identity",
        "drop the audience from the session pod's projected token. An "
        "audience-less projection is a default-audience token by another name, "
        "so automount staying off would stop meaning anything -- and this is "
        "the quiet version, because the pod keeps exactly one token file at "
        "exactly the path the worker reads",
    ),
    Mutation(
        "C1-session-account-gets-rbac",
        "k8s-operator/internal/controller/platformagent_a2a_callout.go",
        ("""\t\tRoleRef:    rbacv1.RoleRef{APIGroup: "rbac.authorization.k8s.io", Kind: "Role", Name: a2aCalloutName(agent)},
\t\tSubjects: []rbacv1.Subject{{
\t\t\tKind:      "ServiceAccount",
\t\t\tName:      a2aCalloutName(agent),""",
         """\t\tRoleRef:    rbacv1.RoleRef{APIGroup: "rbac.authorization.k8s.io", Kind: "Role", Name: a2aCalloutName(agent)},
\t\tSubjects: []rbacv1.Subject{{
\t\t\tKind:      "ServiceAccount",
\t\t\tName:      a2aSessionServiceAccountName(agent),"""),
        "test_C1_a_session_pod_carries_no_kubernetes_identity",
        "point an RBAC binding at the session ServiceAccount instead of the "
        "callout's. The session account holds no permissions, which is the "
        "third thing keeping a session pod's token inert; this is the "
        "cross-module half, because the account is named by the gateway "
        "(module a2a) and granted by the operator (module k8s-operator) and no "
        "Go test in either can see both",
    ),
    Mutation(
        "C1-session-account-gets-rbac-in-the-sibling-file",
        "k8s-operator/internal/controller/platformagent_a2a_manifests.go",
        ("""\t\tSubjects:   []rbacv1.Subject{{Kind: "ServiceAccount", Name: name, Namespace: agent.Namespace}},""",
         """\t\tSubjects:   []rbacv1.Subject{{Kind: "ServiceAccount", Name: a2aSessionServiceAccountName(agent), Namespace: agent.Namespace}},"""),
        "test_C1_a_session_pod_carries_no_kubernetes_identity",
        "the same grant as the mutation above, in the other file that renders "
        "A2A RBAC. The scan used to read only the callout's file and to match "
        "one literal space after `Subjects:`, so a binding added here -- where "
        "gofmt aligns the field -- passed it twice over. Both halves of that "
        "hole are what this mutation holds shut",
    ),
    Mutation(
        "A3-supervisor-terminal-back-on-events",
        "k8s-operator/internal/controller/platformagent_a2a_identities.go",
        ('\t\t\t"a2a.tasks.*.*.in",\n\t\t\t"a2a.tasks.*.*.supervisor",',
         '\t\t\t"a2a.tasks.*.*.in",\n\t\t\t"a2a.tasks.*.*.events",'),
        "test_A3_the_supervisor_holds_no_publish_on_the_executors_events_subject",
        "move the gateway's supervisor publish back onto the executors' events "
        "subject -- the pre-split render, and the change a rollback of the "
        "relay durable would tempt. Every executor's subject is two-writer "
        "again and a forged supervisor terminal is indistinguishable on replay",
    ),
    Mutation(
        "A3-second-supervisor-writer",
        "k8s-operator/internal/controller/platformagent_a2a_identities.go",
        ('\t\t"a2a.tasks." + a2aBridgeAddressee + ".*.events",\n\t\t"$KV.runtime-state.>",',
         '\t\t"a2a.tasks." + a2aBridgeAddressee + ".*.events",\n\t\t"a2a.tasks.*.*.supervisor",\n\t\t"$KV.runtime-state.>",'),
        "test_A3_the_supervisor_subject_has_exactly_one_writer",
        "grant the static bridge publish on the supervisor subject, the shape "
        "a bridge-side janitor would take -- finalising a task it executed "
        "reads like the executor's own business. The subject then no longer "
        "says who wrote there. Retargeted from `worker` when A5 retired that "
        "user; the static credential it names is the half the bridge inherited",
    ),
    Mutation(
        "A3-session-writes-its-own-supervisor-subject",
        "a2a/authcallout/session.go",
        ('\t\tPublish: []string{\n\t\t\tlib.TaskEventsSubject(pod, "*"),\n\t\t},',
         '\t\tPublish: []string{\n\t\t\tlib.TaskEventsSubject(pod, "*"),\n\t\t\tlib.TaskSupervisorSubject(pod, "*"),\n\t\t},'),
        "test_A3_the_supervisor_subject_has_exactly_one_writer",
        "derive a session a grant on its own supervisor subject -- the "
        "helpful-looking change that lets a worker adapter finalise itself "
        "after a harness crash. An executor can then declare itself dead as "
        "infrastructure",
    ),
    Mutation(
        "A3-session-per-task-wildcard",
        "a2a/authcallout/session.go",
        ('\t\tPublish: []string{\n\t\t\tlib.TaskEventsSubject(pod, "*"),\n\t\t},',
         '\t\tPublish: []string{\n\t\t\tlib.TaskEventsSubject(pod, "*"),\n\t\t\tlib.TaskInSubject(pod, "*"),\n\t\t},'),
        "test_A3_the_executors_grant_does_not_reach_its_own_in_subject",
        "widen the session's task-plane grant toward the per-task wildcard the "
        "cards sketched, which puts the executor in its own in-subject writer "
        "set: it can steer and cancel itself as if from the user",
    ),
    Mutation(
        "A3-bridge-events-grant-rewildcarded",
        "k8s-operator/internal/controller/platformagent_a2a_identities.go",
        ('"a2a.tasks." + a2aBridgeAddressee + ".*.events",',
         '"a2a.tasks.*.*.events",'),
        "test_A3_the_events_subject_has_no_rendered_writer",
        "put the addressee wildcard back on the bridge's events grant, which "
        "is what `worker` held and the one edit that reopens the violation A5 "
        "closed. It reads as a generalisation -- one bridge build serving any "
        "addressee -- and it costs every chat session's `…events` its writer "
        "set, so a forged terminal from the shared credential is "
        "indistinguishable from the executor's on replay",
    ),
    Mutation(
        "C1-bus-user-env-renamed-on-one-side",
        "a2a/lib/credentials.go",
        ('EnvBusUser = "A2A_BUS_USER"', 'EnvBusUser = "A2A_BUS_PRINCIPAL"'),
        "test_C1_the_agent_containers_bus_identity_env_is_spelled_the_same_in_both_modules",
        "rename the bus identity env var in a2a/lib without touching the "
        "operator that renders it -- the shape a rename takes when the two "
        "literals live in modules that cannot import each other. Both modules "
        "build and both Go suites stay green, because no test binary links "
        "them. What breaks is every `a2a` invocation in the agent container: "
        "busUser() reads the new name, finds nothing, falls back to NATS_USER "
        "which A5 stopped rendering, and connect() refuses with `no bus "
        "identity` before it dials. Loud where it runs and invisible where it "
        "is reviewed, and the half a reviewer has to think to check is the "
        "operator's render rather than this file",
    ),
    Mutation(
        "C1-bus-token-path-moved-on-the-operator-side",
        "k8s-operator/internal/controller/platformagent_a2a_callout.go",
        ('a2aBusTokenPath      = "/var/run/secrets/a2a-bus"',
         'a2aBusTokenPath      = "/var/run/secrets/kubeagents/a2a-bus"'),
        "test_C1_the_bus_token_path_and_audience_agree_across_the_module_boundary",
        "tidy the projected token under a vendor-prefixed directory, touching "
        "only the module that renders the mount. The client half of the "
        "contract lives in a2a/lib and is not rebuilt by this edit, so it keeps "
        "os.Stat-ing the old path, finds nothing, and falls back to a password "
        "this change stopped rendering -- an agent container that offers the "
        "empty string to the callout and loses the bus entirely, with both Go "
        "suites green because no test binary links both modules",
    ),
    Mutation(
        "C1-bus-token-file-env-renamed-on-the-client-side",
        "a2a/lib/credentials.go",
        ('EnvBusTokenFile = "A2A_BUS_TOKEN_FILE"',
         'EnvBusTokenFile = "A2A_BUS_TOKEN_PATH"'),
        "test_C1_the_reserved_bus_token_file_env_is_spelled_the_same_in_both_modules",
        "tidy the client's override variable to match BusTokenPath beside it, "
        "in the module that reads it. Nothing in a2a notices, because a2a is "
        "the only module that consumes this name -- and the operator, which "
        "does not consume it but RESERVES it, is not rebuilt by this edit. It "
        "goes on refusing A2A_BUS_TOKEN_FILE in spec.deployment.env and in an "
        "AgentPlugin's spec.env, and A2A_BUS_TOKEN_PATH is reserved nowhere: "
        "a plugin sets it, connect() prefers it over the projection with no "
        "fallback, and the agent container presents a file the plugin chose",
    ),
    Mutation(
        "C1-bus-token-file-reservation-spelled-by-hand",
        "k8s-operator/internal/controller/platformagent_manifests.go",
        ('\t\t\t\t\te.Name == a2aBusTokenFileEnv ||',
         '\t\t\t\t\te.Name == "A2A_BUS_TOKEN_FILE" ||'),
        "test_C1_the_reserved_bus_token_file_env_is_spelled_the_same_in_both_modules",
        "inline the constant at the plugin-env drop, which changes no "
        "behaviour today and is the shape a reviewer waves through. It costs "
        "the cross-module comparison its subject: a2aBusTokenFileEnv is what "
        "the conformance suite pins against a2a/lib, and after this edit the "
        "name the operator actually refuses is a literal no test reads. The "
        "next rename moves the constant and leaves the drop behind",
    ),
    Mutation(
        "C1-agent-principal-gets-a-static-password",
        "k8s-operator/internal/controller/platformagent_a2a_identities.go",
        ('\t\tuser:           a2aAgentBusUser,',
         '\t\tuser:           a2aAgentBusUser,\n\t\tcredsKey:       a2aBridgePasswordKey,'),
        "test_C1_the_agent_principal_carries_no_static_bus_password",
        "give the agent's callout principal a Secret key as well, so the same "
        "name is answered for by both the callout and nats.conf's auth_users "
        "exemption and a client is authenticated by whichever path it happened "
        "to take. This is how the retired `worker` credential comes back: one "
        "field, added by someone wiring up a local test that could not present "
        "a token",
    ),
    Mutation(
        "C1-bridge-principal-keyed-on-a-service-account",
        "k8s-operator/internal/controller/platformagent_a2a_identities.go",
        ('\t\tuser:     a2aBridgeUser,',
         '\t\tuser:     a2aBridgeUser,\n'
         '\t\tserviceAccount: a2aServiceAccountName(ns, agentServiceAccountName(agent)),'),
        "test_C1_the_agent_principal_carries_no_static_bus_password",
        "move the bridge sidecar onto the callout, which reads as tightening "
        "and is the exact opposite. A sidecar shares its pod's ServiceAccount, "
        "so the bridge's entry and the agent's would key on one username and "
        "each workload would hold the union of the two grant sets -- the task "
        "plane and the blackboard in one credential, which is `worker` rebuilt "
        "by the mechanism meant to retire it",
    ),
    Mutation(
        "harness-fixture-emptied",
        "k8s-operator/internal/testing/testdata/platform/expected/platformagent.yaml",
        ("\nkind: StatefulSet\n", "\nkind: StatefulSetXX\n"),
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
        if not path.exists():
            verdicts.append((mutation, "STALE", []))
            print(f"STALE    {mutation.id}: {mutation.path} does not exist")
            continue
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
