# Conformance suite

Deterministic assertions, one per security invariant. No LLM judge, no cluster
for most of them, fast enough to run on every pull request.

This is not the behavioural eval suite. Conflating the two is why neither got
built, so the line is worth stating once:

|                 | Behavioural evals                                  | **Conformance tests (this)**                      |
| --------------- | -------------------------------------------------- | ------------------------------------------------- |
| Question        | does the agent still diagnose the FUSE bottleneck? | does `kubectl delete namespace prod` get refused? |
| Judged by       | an LLM                                             | an assertion                                      |
| Deterministic   | no                                                 | yes                                               |
| Needs a cluster | yes                                                | no, for bucket 1                                  |
| Lives in        | not built yet; `bench/` is the intended home       | here                                              |

## Running it

```
make conformance                                  # bucket 1 only, no cluster
python3 tests/conformance/run.py                  # the same, standalone
python3 tests/conformance/run.py --bucket2        # include the cluster scenarios
```

Bucket 2 is skipped unless `KUBE_AGENTS_CONFORMANCE_CLUSTER` names a kubectl
context. That is an explicit opt-in rather than a kubeconfig probe: those
scenarios attempt mutations, and a suite that starts doing that because a
developer happened to have credentials loaded is worse than one that never
runs.

Bucket 1 runs in CI on every pull request, unconditionally — no paths filter.
A security conformance suite that only runs when someone remembers to add a
file to a filter is a suite that stops running. It is stdlib `unittest` plus
PyYAML and takes under a second.

## The three buckets

**Bucket 1** — no cluster, no credentials, no LLM. The bulk of the value and
all of the CI coverage.

**Bucket 2** — needs a cluster, needs no human. Written and skipped, in
`tests/conformance/bucket2/`. Destined for the `rc` pipeline and not yet
wired into it: nothing under `scripts/release/` runs this suite today.

**Bucket 3** — cannot be automated yet, or needs a decision first. A written
reason rather than a missing test, and recorded in two different places
depending on how much of the invariant is affected.

Three invariants are bucket 3 _outright_ — D3, D5 and D6 have no mechanism to
test at all. Each has a class whose docstring says `BUCKET 3`, why, and what
would make it bucket 1, and `test_harness_selfcheck.py` fails if an invariant
has neither a test nor such a docstring.

The other nine bucket-3 entries in the table below are _aspects_ of an
invariant that also has bucket-1 tests — A2's staleness bound, B4's deploy
credential scoping, D1's read scoping and so on. Nothing in code guards those
reasons, because the invariant they belong to is already covered and the
self-check cannot tell a partial answer from a complete one. The table is
their only record. That is a real weakness and it is the first thing to fix if
this suite is ever used as a coverage claim rather than as a coverage map.

A test that pins a non-control is worse than no test. The git lease is the
cautionary example: a concurrency control whose own docstring says it is not an
ownership check, and pinning it would have encoded that gap as an intention.

## Known violations are expected failures

Twelve assertions currently fail. They are decorated `@known_violation`, which
is `unittest.expectedFailure` plus a registration, so CI is green and the gap
still has a name, a line number and a citation. Fixing the control turns the
expected failure into an _unexpected success_, which unittest reports as a
failure — that is the signal to delete the decorator.

The obvious hole in that scheme is that `expectedFailure` swallows every
exception, including the `FileNotFoundError` from an artifact that moved. Two
things close it: every violation test is paired with a plainly-passing
`..._precondition_...` test asserting the artifact and its anchor are still
there, and `_harness.SOURCES` registers every fixed-path artifact the suite
reads with an anchor string that `test_harness_selfcheck.py` verifies. Rename
a symbol and the self-check goes red before anything gets a chance to pass
quietly.

One set of inputs is not registered: the group-B workflow tests glob
`.github/workflows/*.yml` rather than naming each file, because the assertion
is about the set and a registry would have to be edited every time a workflow
is added. Those tests assert a non-empty glob for the same reason the registry
exists.

## Invariant → test → bucket → historical attack

`A1`–`D6` are the twenty-one invariants in `04_major_requirements.md`.
**KV** marks a known violation: the test exists, asserts the invariant, and
currently fails.

> **The cited documents are not in this repository.**
> `04_major_requirements.md`, `slice-2a/`, `slice-2b/findings.md` and
> `overnight-b/findings.md` live in a separate working repository that is not
> published, so those citations do not resolve for a reader here. The table
> below is the vendored summary: it states each invariant's assertion in full,
> so nothing in the suite depends on being able to open the source document.
> A citation identifies _where a finding was first written down_, so that the
> suite and the findings record can be reconciled by someone who has both —
> not a link a reader is expected to follow.

| Inv | Assertion                                                        | Bucket   | Test                                                                  | Attack it would have caught                                                                                                                                                                                        |
| --- | ---------------------------------------------------------------- | -------- | --------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| A1  | a refusal names no caller-supplied value                         | 1        | `test_A1_a_refusal_names_no_caller_supplied_value`                    | denial as an existence oracle over another tenant's namespace names                                                                                                                                                |
| A1  | a refusal still names the rule that fired                        | 1        | `test_A1_a_refusal_names_the_rule_that_fired`                         | — (keeps the bound above from being met by emptying the refusal)                                                                                                                                                   |
| A2  | two users with different RBAC get different outcomes             | 2        | `Scenario1`, `Scenario2`                                              | shared-identity execution: every allowlisted chat user wields the agent's full authority                                                                                                                           |
| A2  | the agent ceiling binds a cluster-admin requester                | 2        | `Scenario3`                                                           | —                                                                                                                                                                                                                  |
| A2  | staleness bound N                                                | **3**    | —                                                                     | N is unset. Three unstated Ns (A2, C2, D6), all needing owners.                                                                                                                                                    |
| A3  | caller-supplied `--as` refused, all five flags, both separators  | 1        | `test_A3_rejects_caller_supplied_as`                                  | impersonation asserted by the caller                                                                                                                                                                               |
| A3  | `--kuberc` refused                                               | 1        | `test_A3_rejects_kuberc`                                              | **slice 2a**: a YAML file injecting `as: system:admin` with nothing in argv                                                                                                                                        |
| A3  | `--flags-file` refused                                           | 1        | `test_A3_rejects_gcloud_flags_file`                                   | the same attack in gcloud's spelling, found first                                                                                                                                                                  |
| A3  | default-path kuberc disabled in the subprocess                   | 1        | `test_A3_default_path_kuberc_is_disabled_in_the_subprocess`           | `$HOME/.kube/kuberc` — no flag in argv at all                                                                                                                                                                      |
| A3  | `--server` / `--insecure-skip-tls-verify` refused                | 1        | `test_A3_rejects_credential_redirection`                              | **slice 2a**: bearer token delivered to a localhost listener                                                                                                                                                       |
| A3  | attached shorthand `-shttp://host` refused                       | 1        | `test_A3_rejects_attached_shorthand_server`                           | **slice 2a**: exact-token matching evaded by pflag's attached shorthand                                                                                                                                            |
| A3  | …and `--sort-by`/`--since`/`--selector` still work               | 1        | `test_A3_the_attached_shorthand_rule_does_not_overreach`              | the over-broad fix that breaks reads and gets switched off                                                                                                                                                         |
| A3  | a request with no verified principal is refused                  | 2        | `Scenario4NoVerifiedIdentity`                                         | a session with no principal executed under the agent's own identity, which makes D1 unenforceable                                                                                                                  |
| A3  | the session-inject endpoint authenticates its caller             | 1 **KV** | `test_A3_the_session_inject_endpoint_authenticates_its_caller`        | **slice 2b 1.8**: `/sessions/{id}/inject` on `0.0.0.0:8699`, no auth, triggers a full agent turn                                                                                                                   |
| A4  | the operator cannot escalate its own grants                      | 1        | `test_A4_the_operator_cannot_escalate_its_own_grants`                 | a controller with RBAC CRUD and `escalate` makes every ceiling advisory                                                                                                                                            |
| A4  | the chart grants the same ceiling as the kustomize role          | 1        | `test_A4_the_chart_grants_the_same_ceiling_as_the_kustomize_role`     | a ceiling asserted on one install path only                                                                                                                                                                        |
| A4  | triggering is delegation                                         | 1        | `test_A4_triggering_is_covered_by_the_A3_inject_finding`              | see A3 above — the one instance in this codebase                                                                                                                                                                   |
| B1  | every kubectl write verb refused                                 | 1        | `test_B1_kubectl_write_verbs_are_refused`                             | `kubectl delete namespace prod` reaching the sidecar and running                                                                                                                                                   |
| B1  | ordinary reads still work                                        | 1        | `test_B1_ordinary_reads_still_work`                                   | an over-strict gate that gets globally disabled                                                                                                                                                                    |
| B1  | gcloud write commands refused                                    | 1        | `test_B1_gcloud_write_commands_are_refused`                           | —                                                                                                                                                                                                                  |
| B1  | the sandbox image ships no credentialed CLI                      | 1        | `test_B1_the_sandbox_image_ships_no_credentialed_cli`                 | a gate in a stage the agent image does not derive from                                                                                                                                                             |
| B1  | the shipped denylist refuses credential disclosure               | 1        | `test_B1_the_shipped_denylist_refuses_credential_disclosure`          | `gcloud auth print-access-token`, `gh auth token`, `kubectl config view --raw`                                                                                                                                     |
| B1  | the agent cannot merge, approve or force-push                    | 1 **KV** | `test_B1_the_agent_cannot_merge_or_approve`                           | `gh pr merge` works today: `gh` is allowlisted and only `gh auth`/`gh extension` are denied                                                                                                                        |
| B2  | no workflow approves or merges a pull request                    | 1        | `test_B2_no_workflow_approves_or_merges_a_pull_request`               | a model verdict causing a merge                                                                                                                                                                                    |
| B2  | `pull-requests: write` has exactly one holder                    | 1        | `test_B2_no_workflow_grants_a_bot_the_ability_to_approve`             | —                                                                                                                                                                                                                  |
| B2  | a certified predicate in a human-only path                       | **3**    | —                                                                     | no such mechanism exists. Auto-merge over a certified predicate is a D2 tier that was never built.                                                                                                                 |
| B3  | substrate paths enumerated as code                               | 1 **KV** | `test_B3_the_substrate_paths_are_enumerated_as_code`                  | `failurePolicy: Fail` → `Ignore`, commit message "unblock apply during upgrade window"                                                                                                                             |
| B3  | the agent cannot reach admission or RBAC through kubectl         | 1        | `test_B3_the_agent_cannot_reach_the_admission_policy_through_kubectl` | — (the API half; the artifact half is the KV above)                                                                                                                                                                |
| B4  | every `workflow_run` deploy gates on repo, conclusion and branch | 1        | `test_B4_every_workflow_run_deploy_gates_on_repository_and_branch`    | a fork's completed run reaching a job that mints a deploy credential                                                                                                                                               |
| B4  | no `pull_request_target` workflow checks out the pull request    | 1        | `test_B4_no_pull_request_target_workflow_checks_out_the_pull_request` | arbitrary code execution with the base repository's secrets                                                                                                                                                        |
| B4  | `contents: write` confined to the release path                   | 1        | `test_B4_contents_write_is_confined_to_the_release_path`              | —                                                                                                                                                                                                                  |
| B4  | the deploy credential cannot write RBAC or admission objects     | **3**    | —                                                                     | needs a decision. The deploy path is one WIF identity with no per-object scoping.                                                                                                                                  |
| B5  | the renderer neutralises markdown injection                      | 1        | `test_B5_precondition_the_renderer_still_sanitises_its_inputs`        | backticks and pipes in a "verbatim excerpt" breaking out of the block                                                                                                                                              |
| B5  | rendered evidence carries no bidi or zero-width trickery         | 1 **KV** | `test_B5_rendered_evidence_carries_no_direction_or_width_trickery`    | `U+202E` in a pod log reversing what the approver is shown inside the evidence block                                                                                                                               |
| B5  | approval binds to a content digest                               | **3**    | —                                                                     | there is no approval object to bind. Requires the review gate B3 also wants.                                                                                                                                       |
| B6  | the GitOps template names no automation identity                 | 1        | `test_B6_the_gitops_template_names_no_automation_identity`            | two agent identities defeating self-approval; an App token satisfying a required count                                                                                                                             |
| B6  | every guarded path in the template has an owner                  | 1        | `test_B6_every_guarded_path_in_the_template_has_an_owner`             | a ruleset requiring code-owner review on paths no rule covers                                                                                                                                                      |
| B6  | whether an App token satisfies a required approval               | **3**    | —                                                                     | empirical, needs GitHub. One of the four checks the requirements document wants run.                                                                                                                               |
| C1  | the process namespace is never shared                            | 1        | `test_C1_the_process_namespace_is_never_shared`                       | reaching the broker's backend socket and `/proc/<pid>/environ` at matched UIDs                                                                                                                                     |
| C1  | the agent and the broker run as different users                  | 1        | `test_C1_the_agent_and_the_broker_run_as_different_users`             | the same, one layer down                                                                                                                                                                                           |
| C1  | the split broker Pod holds no sandbox container                  | 1        | `test_C1_the_split_broker_pod_holds_no_sandbox_container`             | —                                                                                                                                                                                                                  |
| C1  | the backend socket is bound _under_ a private umask              | 1        | `test_C1_the_broker_backend_socket_is_bound_private`                  | **slice 2b §3**: `umask 0002` for the shared PVC would have taken the socket from 0600 to group-writable                                                                                                           |
| C1  | the executor never reaches a shell                               | 1        | `test_C1_the_executor_never_reaches_a_shell`                          | `realtime_iam`: a compound command with a read verb first; `#` neutralising appended flags                                                                                                                         |
| C1  | the executor refuses an executable it does not ship              | 1        | `test_C1_the_executor_refuses_an_executable_it_does_not_ship`         | `sh -c` as the landing pad for the two above                                                                                                                                                                       |
| C1  | `git` in the broker cannot execute arbitrary code                | 1 **KV** | `test_C1_git_in_the_broker_cannot_execute_arbitrary_code`             | **slice 2b 1.1**: `git -c protocol.ext.allow=always clone "ext::sh -c"` — RCE in the credential holder                                                                                                             |
| C1  | the rendered egress policy is default-deny                       | 1        | `test_C1_the_rendered_egress_policy_is_default_deny`                  | **slice 2b 1.3**: `0.0.0.0/0 except 169.254.169.254/32` adds the internet rather than subtracting the address                                                                                                      |
| C1  | the rendered egress policy reaches no metadata address           | 1        | `test_C1_the_rendered_egress_policy_reaches_no_metadata_address`      | a guard written against one of the three spellings                                                                                                                                                                 |
| C1  | every operator-supplied CIDR reaches the refusal guards          | 1        | `test_C1_every_operator_supplied_cidr_reaches_the_refusal_guards`     | a new CRD field that accepts a CIDR and never calls the guard                                                                                                                                                      |
| C1  | the metadata server is unreachable from the sandbox              | 2        | `Scenario5`                                                           | the controller deleting the metadata-deny NetworkPolicy; the credential-free sandbox minting the GSA token                                                                                                         |
| C1  | a violating request is rejected by the API server                | 2        | `Scenario6`                                                           | **slice 2b 1.2**: kustomize `namePrefix` leaving the policy applied and silently inert                                                                                                                             |
| C2  | an unparseable argv is refused                                   | 1        | `test_C2_an_unparseable_argv_is_refused`                              | a new kubectl release adding a flag that hides the verb                                                                                                                                                            |
| C2  | an unknown flag cannot swallow a write subcommand                | 1        | `test_C2_an_unknown_flag_cannot_swallow_a_write_subcommand`           | `rollout --someflag status restart x` reading as `rollout status`                                                                                                                                                  |
| C2  | `cluster-info dump` refused by both its guards                   | 1        | `test_C2_cluster_info_dump_is_refused_by_both_of_its_guards`          | `--output-directory=DIR` writing a tree inside the credential sidecar                                                                                                                                              |
| C2  | the read-only gate survives a typo                               | 1        | `test_C2_the_read_only_gate_survives_a_typo`                          | a misspelled ConfigMap value silently disarming the posture                                                                                                                                                        |
| C2  | the agent API proxy refuses to start without its key             | 1        | `test_C2_the_agent_api_proxy_refuses_to_start_without_its_key`        | an empty secret disabling the check instead of stopping the process                                                                                                                                                |
| C2  | the session server fails closed on a missing key                 | 1 **KV** | `test_C2_the_session_server_fails_closed_on_a_missing_api_key`        | `if token:` omitting the Authorization header rather than raising                                                                                                                                                  |
| C2  | shell-quoting does not change the denylist verdict               | 1 **KV** | `test_D15_quoting_does_not_change_the_denylist_verdict`               | **new, found writing this suite** — see `overnight-b/findings.md` 2.5                                                                                                                                              |
| C2  | the grace window on cached entitlements, as a number             | **3**    | —                                                                     | no cache, no stated bound. Second of the three unstated Ns.                                                                                                                                                        |
| C3  | the policy decision reads nothing but its argv                   | 1        | `test_C3_the_policy_decision_reads_nothing_but_its_argv`              | every kuberc/flags-file variant at once: no second input to control, no rewrite-after-check race                                                                                                                   |
| C3  | untrusted output cannot forge a log line                         | 1        | `test_C3_untrusted_output_cannot_forge_a_log_line`                    | anyone who can write a pod log writing to the audit record                                                                                                                                                         |
| C3  | untrusted content cannot derive an approval tier                 | **3**    | —                                                                     | needs the provenance labelling D3 also needs. The fleet-drift attack (an attacker shifting a derived baseline until production reads as drift) is the case to write first.                                         |
| C4  | every third-party action is pinned to a commit                   | 1        | `test_C4_every_third_party_action_is_pinned_to_a_commit`              | a retagged release silently changing what CI runs                                                                                                                                                                  |
| C4  | the agent base image is pinned by digest                         | 1        | `test_C4_the_agent_base_image_is_pinned_by_digest`                    | — (the one reference this repo gets right)                                                                                                                                                                         |
| C4  | upstream skills are pinned and verified                          | 1 **KV** | `test_C4_upstream_skills_are_pinned_and_verified`                     | whatever is at upstream HEAD becoming agent instructions, landing in a preflight hook before the model wakes                                                                                                       |
| C4  | every shipped image is pinned by digest                          | 1 **KV** | `test_C4_every_shipped_image_is_pinned_by_digest`                     | `DefaultPlatformAgentVersion = "latest"`                                                                                                                                                                           |
| C5  | no minted role grants a write verb                               | 1        | `test_C5_no_minted_role_grants_a_write_verb`                          | the blueprints operator minting ClusterRoleBindings from a namespaced CRD with no ceiling                                                                                                                          |
| C5  | the leader role stays confined to coordination                   | 1        | `test_C5_the_leader_role_stays_confined_to_coordination`              | —                                                                                                                                                                                                                  |
| C5  | the tokenreview role is the narrowest form of itself             | 1        | `test_C5_the_tokenreview_role_is_the_narrowest_form_of_itself`        | binding `system:auth-delegator` for one verb, and getting subjectaccessreviews with it                                                                                                                             |
| C5  | no binding names `system:auth-delegator`                         | 1        | `test_C5_no_agent_binding_names_the_auth_delegator_role`              | —                                                                                                                                                                                                                  |
| C5  | the agent is bound to no write-capable built-in role             | 1        | `test_C5_the_agent_is_bound_to_no_write_capable_builtin_role`         | read-only rules next to a binding to `edit`                                                                                                                                                                        |
| C5  | the controller does not reap the metadata-deny guardrail         | 1        | `test_C5_the_controller_does_not_reap_the_metadata_deny_guardrail`    | **slice 2b 1.5**: `deleteLegacyCredentialIsolationResources` garbage-collecting a whole shipped isolation design, with a test holding it down                                                                      |
| C5  | the admission binding names a policy that exists                 | 1        | `test_C5_the_admission_binding_names_a_policy_that_exists`            | **slice 2b 1.2**: `namePrefix` rewriting `metadata.name` and not `spec.policyName`                                                                                                                                 |
| C5  | the admission policy fails closed                                | 1        | `test_C5_the_admission_policy_fails_closed`                           | `failurePolicy: Fail` → `Ignore`                                                                                                                                                                                   |
| D1  | the exec route records a principal                               | 1        | `test_D1_the_exec_route_records_a_principal`                          | unattributed actions                                                                                                                                                                                               |
| D1  | a log hint cannot forge a record                                 | 1        | `test_D1_a_log_hint_cannot_forge_a_record`                            | agent-controlled bytes in the audit trail                                                                                                                                                                          |
| D1  | the refusal log hint names no caller-supplied value              | 1 **KV** | `test_D1_the_refusal_log_hint_names_no_caller_supplied_value`         | **new, found writing this suite** — `gcloud projects delete X` logs `projects.delete.X`. See `overnight-b/findings.md` 2.1                                                                                         |
| D1  | a read through the agent names both principals                   | 2        | `Scenario7AuditAttribution`                                           | the two-principal audit trail asserted rather than verified; how GKE represents `impersonatedUser` is undocumented                                                                                                 |
| D1  | the record lives where the agent cannot reach it                 | **3**    | —                                                                     | Cloud Logging is out of reach today by geometry rather than by a control. Read-side scoping does not exist at all, and a unified audit log is by construction the richest cross-tenant aggregation in the product. |
| D2  | no direct-apply or break-glass mode exists                       | 1        | `test_D2_no_direct_apply_mode_exists`                                 | `workflowMode: Direct` as an agent config option                                                                                                                                                                   |
| D2  | the read-only posture is not a customer-facing knob              | 1        | `test_D2_the_read_only_posture_is_not_a_customer_facing_knob`         | a global autonomy setting                                                                                                                                                                                          |
| D2  | autonomy earned per domain against a track record                | **3**    | —                                                                     | no tier mechanism and no record to earn against.                                                                                                                                                                   |
| D3  | data carries the meet of its inputs                              | **3**    | `D3DataCarriesTheMeetOfItsInputs` (docstring)                         | no labels, no lattice, no declassifier, no named owner for the fleet-aggregate decision                                                                                                                            |
| D4  | every projected token expires                                    | 1        | `test_D4_every_projected_token_expires`                               | a credential outliving the session that caused it                                                                                                                                                                  |
| D4  | the broker token is audience-bound                               | 1        | `test_D4_the_broker_token_is_audience_bound`                          | — (it is also what makes TokenReview a delegation rather than a second parser)                                                                                                                                     |
| D4  | the customer API key is secret-backed                            | 1        | `test_D4_the_customer_api_key_is_secret_backed`                       | —                                                                                                                                                                                                                  |
| D4  | every projected token is audience-bound                          | 1 **KV** | `test_D4_every_projected_token_is_audience_bound`                     | **slice 2b 1.10**: `event-watcher-ksa-token` mounted with the default audience, including into the broker Pod                                                                                                      |
| D4  | no fixed shared secret ships in a manifest                       | 1 **KV** | `test_D4_no_fixed_shared_secret_ships_in_a_manifest`                  | `API_SERVER_KEY="cluster-internal-trusted"`, and the Python default is the same literal                                                                                                                            |
| D5  | availability and cost are security properties                    | **3**    | `D5AvailabilityAndCostAreSecurityProperties` (docstring)              | no budgets, so no exhaustion event to assert the refusal of                                                                                                                                                        |
| D5  | model fallback may not lower the enforcement tier                | 1        | `test_D5_the_enforcement_tier_cannot_be_lowered_by_routing`           | choosing which model evaluates your injection payload by exhausting the good one's budget                                                                                                                          |
| D6  | kill switch                                                      | **3**    | `D6KillSwitch` (docstring)                                            | no halt path; N unset; the nearest candidate needs a Deployment edit, which is the agent plane's own control path                                                                                                  |
| D6  | the read-only switch is not mistaken for a kill switch           | 1        | `test_D6_the_read_only_switch_is_not_mistaken_for_a_kill_switch`      | D6 being marked satisfied by a control that points the opposite way                                                                                                                                                |

## D15 — the parser-differential class, and it is open

Every Critical this project has found is the checker and the executor parsing
the same input differently. `test_D15_parser_differentials.py` holds the
class-level tests plus the differentials not already asserted where their
invariant lives; the table says which is which:

| Differential                                               | Where it is asserted                                                                                                                                                      |
| ---------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--kuberc` is a flags file kubectl honours                 | `test_A3_rejects_kuberc`                                                                                                                                                  |
| `-shttp://host` is `--server`                              | `test_A3_rejects_attached_shorthand_server`                                                                                                                               |
| `::ffff:0.0.0.0/96` normalises to `0.0.0.0/0`              | Go: `TestAControlPlaneCIDRCannotBeTheWholeInternet`, `TestExtraRulesCannotReopenTheMetadataServer`. Python asserts the premise and the wiring — it cannot call the guard. |
| the checker joins a shell string, the executor runs a list | `test_D15_quoting_does_not_change_the_denylist_verdict` (**KV**, new)                                                                                                     |
| a refused flag must be refused in any position             | `test_D15_a_refused_flag_is_refused_wherever_it_appears`                                                                                                                  |
| `argv[0]` means the same thing to both layers              | `test_D15_the_two_layers_agree_on_the_governed_tool`                                                                                                                      |

**The class is open.** Four instances now across three slices. The third was
found late, in code two earlier reviews had already passed, and the fourth was
found writing this suite. A list of four differentials is not evidence there is
no fifth; it is evidence the shape recurs. Where we can, delegate to the thing
that enforces rather than parsing — the split broker verifying its caller with
a TokenReview instead of comparing a secret is the model. Where we must parse,
refuse the ambiguous form rather than normalising it.

## Mutation results

Every assertion here is verified by deleting or weakening the control it tests
and confirming the suite goes red. If deleting the control leaves the suite
green, the test does not exist — slice 2a shipped a whole gate that could be
deleted with its suite byte-identical, and only a dedicated task caught it.

`hack/conformance-mutations.py` is how that is checked, and re-running it is
how it stays true:

```
python3 hack/conformance-mutations.py          # every mutation
python3 hack/conformance-mutations.py --list
python3 hack/conformance-mutations.py -k C1    # substring filter on the id
```

74 mutations: 55 KILLED, 18 NOISY, one deliberately harmless as a control on
the harness itself, zero genuine survivors, zero stale. Each names the control
it removes, the test that must notice, and the plausible bad change it
imitates. It is not run in CI — it edits tracked files in place — so it is a
thing to run when adding a test, which step 4 below says to do.

**The coverage of that set is checked by the suite, because it rotted once.**
The first version of this file claimed every test had been mutation-verified
while 29 passing assertions were named by no mutation at all. A manual harness
outside CI is exactly the thing that drifts, so
`test_every_bucket_one_assertion_is_named_by_a_mutation` reads the mutation set
out of the harness's AST and fails when an assertion is attacked by nothing.

Three carve-outs, each for a stated reason rather than by convention. A known
violation has no control to delete and is verified by its precondition pair
instead. A precondition test asserts that an artifact and its anchor are still
present, and that whole mechanism is mutated once by `harness-source-moved`
rather than once per test. And one assertion — that `::ffff:0.0.0.0/96` unmaps
to `0.0.0.0/0` — reads no repository artifact, so any edit that reddens it is
an edit to the assertion; that one is an entry in an exemption list whose own
test requires each entry to carry an argument.

The check is a floor, not a proof: it knows a mutation exists, not that the
mutation removes the right thing. It also walks the five invariant modules and
not `test_harness_selfcheck.py`, so it does not police its own coverage.

**Restoring the tree is not the same as restoring the suite.** A mutation that
renames a symbol to another of the same length leaves the file size unchanged;
restore it with `git checkout` inside the same second and CPython's
mtime-plus-size check cannot distinguish the cached bytecode compiled from the
mutated source, and keeps using it. That happened, and nine verdicts in the
first full run were collateral from a leak rather than from any mutation. The
harness now runs with `-B`, purges `.pyc` before each run, and re-runs the
suite once at the end: a run that ends red says so and exits non-zero.

## Adding a test

1. Register every artifact you read in `_harness.SOURCES`, with an anchor
   substring whose loss makes your assertion meaningless.
2. Name the test after the invariant: `test_A3_rejects_caller_supplied_as`.
   `test_harness_selfcheck.py` reads that prefix to check coverage.
3. Assert the _refusal_, never the presence of the control. Twice in this
   project an object's existence has been mistaken for its enforcement.
4. Add a mutation that removes the control, confirm your test goes red, and
   record the verdict. This is not optional and not on the honour system —
   the suite fails until the mutation exists.
5. If it fails against current code, decorate it `@known_violation` with the
   invariant and the document that records the finding — and give its class a
   `..._precondition_...` test, which the self-check requires.
