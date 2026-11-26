# The Broker Surface of a Profile

Status: draft for review. Nothing here is built. Each grant is a proposal for the security
approver, and no code follows before that decision.

## Summary

A profile worker reaches the credential broker under one role, `profile`. That role reaches four
routes, 11 of the 27 version-control verbs and two MCP tools. The platform agent's shell role, and
for the MCP relay its chat role, reach more, and the platform's work uses the difference:
[the platform-as-profile inventory](platform-as-profile-inventory.md), blocker 7.

This document gives one row for each verb that the profile role does not reach and for each route
in the tables below. Each row says what work uses it, who would get it, how a grant would be held,
which check changes, and when. It also lists what stays out.

The recommendation in short:

- Grant one verb or one route in each pull request, each with a conformance row and its mutation.
- Hold each grant with a fact of the task credential: the target, the permit, the trigger, or the
  profile's name.
- Give a verb to each profile only when the model's work in a profile calls it. Four reads that
  no model tool calls get no grant here: three that only scripts call, and `proposal-claim`, which
  a plugin calls.
- Writes wait for the unattended write permit.
- Four other designs own parts of this work. Three are written: the unattended write permit
  (#7130), the state homes with the dial and post-merge state (#7123), and the hooks (#7253). One
  is not written: reads with no cluster (#6667).
- [What Stays Out](#what-stays-out) lists five things that get no grant.

## What a Profile Reaches Today

[The profile specification](spec-subagent-profiles.md#how-a-profile-becomes-a-pod) owns the
statement of what a profile pod reaches. This table is the short form that the rows below refer to,
read from `agents/platform/scripts/` at commit `b5f4f11f2`.

| Surface              | What the profile role gets                                                                           | What holds it                                                                                                               | Where                                                                                      |
| -------------------- | ---------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| `/v1/exec`           | `kubectl` and `gcloud`, read-only                                                                    | The task credential's one cluster and project; a flag allowlist                                                             | `ROUTE_ROLES`, `ROLE_EXECUTABLES` in `credential_proxy.py`                                 |
| `/v1/mcp/`           | `developer_knowledge`: `get_documents`, `search_documents`                                           | A task in hand                                                                                                              | `PROFILE_SERVER_TOOLS` in `mcp_policy.py`                                                  |
| `/v1/audit-reports`  | Read; write of one stream                                                                            | Read: the profile's name is on the operator's allowlist. Write: also the trigger of that stream's schedule                  | `report_read_permitted`, `report_write_permitted`                                          |
| `/v1/vcs/`, 8 reads  | `clone`, `proposal-view`, `-list`, `-commits`, `branch-view`, `identity`, `issue-list`, `issue-view` | A task credential; the managed list; plain words in an `issue-list` query                                                   | `ROLE_VCS_VERBS`, `profile_vcs_refusal`, `profile_issue_query_refusal`                     |
| `/v1/vcs/`, 3 writes | `publish`, `proposal-create`, `proposal-update`                                                      | Three bars: the `vcs:proposal-create` permit; the install's own branch and proposals; the branch and proposals of this task | `PROFILE_PERMIT_GATED_VCS_VERBS`, `PROFILE_OWNERSHIP_GATED_VCS_VERBS`, `_profile_task_bar` |

Three checks run at import and refuse a table that widens the profile role
(`_validate_role_vcs_verbs`):

- a profile write verb that is not in the permit-gated set;
- a profile write verb that is not in the ownership-gated set;
- `snapshot` or `capabilities` in the profile set, because they answer for a repository that the
  install does not manage.

The same function refuses a verb that the route table does not serve.

`identity` with no repository answers the names of the managed repositories to a profile. That is
the profile's read of the managed list. A script that reads the list from a file on the platform's
pod, such as `gitops_preflight.py`, needs a change of the script and no new grant.

## Rules for a Grant

1. **One role, each profile.** The broker gives each profile worker the same role. A verb added to
   the role's set is a grant to each profile of the install. Thus a grant for one kind of work
   needs a hold that only that work passes.
2. **The hold is a fact of the task credential.** The credential names the task's profile, tier,
   target, trigger and permits. The broker verifies it, and the model cannot compose it. A grant
   uses one or more of these facts. It does not use a request field.
3. **One grant, one pull request.** Each has a conformance row in `tests/conformance/` and a
   mutation in `hack/conformance-mutations.py` that removes the hold and must go red.
4. **A read is still a grant.** A read verb reads each managed repository, not only the one of the
   task. That is true for the eight reads of today, and it stays true for a new read.
5. **A write of a version-control verb needs a permit and an ownership rule.** The import check
   keeps this true. This document does not relax it. The report and findings routes have their own
   write rule, which the state homes design owns.

## Options

| Option                                                                     | For                                                                | Against                                                                                  |
| -------------------------------------------------------------------------- | ------------------------------------------------------------------ | ---------------------------------------------------------------------------------------- |
| A. Add to the `profile` role, held by the task credential                  | One role to reason about; the pattern of the three writes of today | Each profile gets the verb; the hold must be exact                                       |
| B. A second role for platform work (its own token audience)                | A route can be closed to the `cluster` profile by role             | A new identity for the operator to render and to verify; two tables that can drift apart |
| C. Hold by the profile's name, from an allowlist that the operator renders | The pattern of the audit-report store; no new identity             | A list in the broker's environment for each such route                                   |
| D. The work stays with another caller (the session, a script run)          | No new grant                                                       | Only valid where that caller keeps the work after the platform agent goes                |

Recommendation: A for a grant that the task credential can hold exactly. C where only one kind of
work has a reason to pass. D where the work does not need the model. Not B.

## Version-Control Verbs

Sixteen verbs are outside the profile set. "Work" names the main platform code that calls the verb
today. `vcs.py`, the model's version-control tool, offers 10 of the 16 (`capabilities`, the four
issue writes, `label-ensure`, `proposal-comment`, `proposal-close`, `proposal-acknowledge`,
`branch-delete`), and a copy of it ships in the `cluster` persona. The other six (`snapshot`,
`proposal-reviews`, `proposal-closer`, `revision-contains`, `proposal-claim`,
`proposal-practice-label`) are library verbs that only scripts call.

### Reads

| Verb                | Work today                                                                                                                                        | Who gets it  | Proposal                                                                                                                                                                                                                                                                                                                                                   | Check that changes                                                                                                                             |
| ------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- | ------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| `proposal-reviews`  | The post-merge check reads change requests                                                                                                        | None         | No grant in this document. The caller is a script with no model, and `vcs.py` has no command for the verb. Option D: the work stays with the script run. The state homes design (`platform-as-profile-state-homes.md`) decides who runs the post-merge work, and this document adds the verb row when that decision is made                                | None                                                                                                                                           |
| `revision-contains` | The post-merge check asks if a revision contains a commit                                                                                         | None         | Same                                                                                                                                                                                                                                                                                                                                                       | None                                                                                                                                           |
| `proposal-closer`   | The decision-memory sweep reads who closed a proposal                                                                                             | None         | No grant in this document, for the same reason. Decide with the move of the watcher script                                                                                                                                                                                                                                                                 | None                                                                                                                                           |
| `capabilities`      | `submit-suggestion` reads the install's write mode before a publish; the audit ledger, the chat-delivery watch and the issue resolver call it too | Each profile | Grant only with a new hold: for the profile role the broker's dispatch (`_dispatch_vcs_post`) resolves the repository and asks the managed list, as for each other verb. Open question 1                                                                                                                                                                   | The import check; the row that holds the exact verb set; a new row: a profile's `capabilities` for a repository that is not managed is refused |
| `snapshot`          | `inspect-repository`, the audit collectors and the API deprecation scan read one revision as a tar                                                | Each profile | Grant for a managed repository only, the rule that the `curator` role has today. That serves the collectors' reads of the GitOps repository. It does not serve the audit's declared-intent search, which reads each context repository through `inspect_repository.py clone`, and it does not serve the `inspect-repository` skill. Open questions 2 and 3 | The import check; the row that holds the exact verb set; a new row (the `curator` rule has a unit test only)                                   |
| `proposal-claim`    | The `pr_opened` plugin claims a proposal with the token of its `proposal-create` answer                                                           | None         | No grant in this document. A profile's `proposal-create` answer carries no token: by row 7 of the hooks design (#7253) the broker itself logs each write of a profile task that succeeds                                                                                                                                                                   | None                                                                                                                                           |

### Writes

A profile write of today passes three bars: the permit, the install's own work, and the work of this
task. The inventory names two; the task bar is newer than its snapshot. The ownership bar and the
task bar each refuse a verb that they do not know, and the task record does not outlive its task.
A new write needs a branch in each bar, and two things that do not exist today: a permit that an
unattended run can hold, and a permit check for each verb (the broker checks one permit name for
each gated verb now). The permit is the subject of the unattended write permit design.

Most of the work below is on an object of an earlier run: a ledger issue, a stale proposal, a
review reply, a spent branch. The task bar of today cannot hold that work. The ownership bar alone
is wider than the task: it passes each proposal and branch of the install. Each row names the bar
that the work needs, and open questions 5 and 6 ask what replaces the task bar.

The install's write mode (`PROPOSE_VERBS`) refuses branch and proposal writes for each caller
before these bars. It does not refuse the issue verbs or `label-ensure`.

Each grant in this table changes the row
`test_A3_a_profile_caller_publishes_a_change_only_under_the_permit`, the row that holds the exact
verb set, and the two gated sets that the import check reads.

| Verb                                           | Work today                                                                                                                                   | Who gets it                      | Bars that the grant needs                                                                                                                                                                                                                                                                                                                                                                                                                                                               | When                  |
| ---------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------- |
| `issue-create`                                 | The audit ledger opens one issue for each stream. Two scripts with no model open one too: the chat-delivery watch and the first-run delivery | A profile on the operator's list | A permit for a write of the issue family, as the unattended write permit design decides, and the four conditions of the report store's write rule: the profile is on the operator's list; the trigger has origin `builtin`; its name is `schedule/<stream>`; the repository is the one that the install publishes to. The labels are those of that stream; the broker writes them, not the caller. The two scripts name no stream: they are script runs, and this bar does not fit them | After the permit      |
| `issue-comment`, `issue-update`, `issue-close` | The audit ledger; the chat-delivery watch; the issue resolver                                                                                | A profile on the operator's list | For the ledger: the same four conditions, and the issue is the install's own, opened by the credential's login, with the stream label of the trigger. The issue resolver is not covered: it works on issues that people open and must not touch the install's own, so it needs a different bar and its own row later                                                                                                                                                                    | After the permit      |
| `label-ensure`                                 | The audit ledger, the resolver, the chat-delivery watch and the first-run delivery make their labels                                         | A profile on the operator's list | The same four conditions. The label name is in a closed set for the stream. A profile cannot make the auto-merge label                                                                                                                                                                                                                                                                                                                                                                  | With the issue verbs  |
| `proposal-comment`                             | Replies on a proposal; the post-merge follow-ups                                                                                             | None until open question 6       | The ownership bar: the install's own proposal (`is_agent_pull_request`). The task bar passes only a proposal of the same task, so a reply or a follow-up in a later run needs what open question 6 decides                                                                                                                                                                                                                                                                              | After the issue verbs |
| `proposal-close`, `proposal-acknowledge`       | The audit and the auto-merge switch close a stale proposal; a reply marks a review as seen                                                   | None until open question 6       | Same                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    | After the issue verbs |
| `branch-delete`                                | `submit-suggestion` uses a branch name again; the audit and the auto-merge switch delete a spent branch                                      | None until open question 6       | The broker's rule of today: under the install's branch prefix, and the proposal is done. A spent branch is from an earlier run, so the task bar cannot hold it                                                                                                                                                                                                                                                                                                                          | After the issue verbs |
| `proposal-practice-label`                      | The audit labels an auto-merge candidate                                                                                                     | None                             | No grant in this document. The state homes design decides the dial and auto-merge state, and this document adds the verb row when that decision is made                                                                                                                                                                                                                                                                                                                                 | After that decision   |

## Routes

| Route                              | Work today                                                                                                                                         | Who gets it                      | Proposal                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             | Check that changes                                                                                                                                                        |
| ---------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `gke` MCP server on `/v1/mcp/`     | Control-plane settings in the cluster inventory audit                                                                                              | Each profile, if granted         | The six control-plane tools only: `get_cluster`, `get_node_pool`, `get_operation`, `list_clusters`, `list_node_pools`, `list_operations`. Each resource argument must resolve to the task's target; for the two list tools that take no cluster, the project and the location equal the target's; `-`, a missing argument or an unknown shape is refused; a tool with no argument map is refused. The nine in-cluster tools stay out: two of them (`get_k8s_resource`, `describe_k8s_resource`) read any kind, Secrets included, and kubectl on `/v1/exec` is the profile's read of the cluster with its own refusals. The gain is small: `gcloud container clusters describe` and `node-pools describe` give a profile the settings that the audit reads, and only `get_operation` has no confined gcloud command. Open question 10 | `PROFILE_SERVER_TOOLS`; a per-tool argument table; `test_A3_a_profile_worker_reaches_no_cluster_through_mcp`; new rows for each argument shape and for an in-cluster tool |
| `/v1/gcp/` (Cloud API relay)       | Monitoring, Trace and Logging reads for the cost audit, the health sweep, the observability skill, the stockout collector and the readiness report | Each profile                     | Grant the six Monitoring, Trace and Logging entries with a per-task target check: the project in the path or the body equals the task's project id; a project number is refused; a credential with no target is refused. These reads are held to the task's project and not to its cluster, the limit that `gcloud logging read` has on `/v1/exec` today. Open question 8. The two GKE cluster entries stay out: their callers are the Cluster Agent reconcile, the get-credentials check and the stall watch, and `gcloud container clusters describe` and `list` give a profile that read, held to the target's location                                                                                                                                                                                                           | `ROUTE_ROLES`; the relay handler; `test_A3_the_profile_role_reaches_exec_mcp_vcs_and_the_audit_reports_alone`; new rows: other project, a project number, no task         |
| `/v1/vcs/` for a report credential | An audit with no cluster writes its ledger issue                                                                                                   | A profile on the operator's list | A task with no cluster carries a report credential or none. The broker refuses a report credential on each route outside a fixed list, and `/v1/vcs/` is not on that list. A report credential also carries no permit today, because the permit needs a cluster target. So a ledger write from a run with no cluster needs two changes: one entry in the route list for the one verb, and a permit on a credential with no target. The worker's client also withholds a report credential outside the audit-report routes, so its list changes too. The unattended write permit design decides this; its approver accepted it there as scoped (the verbs are enumerated and gated by the permit and the broker's table, with no `/v1/exec` or MCP), on two conditions that are that design's work                                    | `REPORT_CREDENTIAL_ROUTES`; the client's `_is_report_credential` check; `test_A3_a_report_credential_reaches_the_audit_report_routes_alone`                               |
| `/v1/exec/admission`               | A fleet sweep sizes its pool                                                                                                                       | None                             | No grant now. A task reads one cluster, so it has no pool to size. It stays out until the design for reads with no cluster                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           | None                                                                                                                                                                      |
| `/v1/feedback/`                    | The feedback skill files one confirmed report                                                                                                      | None                             | No grant now. It is a write to a form outside the install on a person's confirm. It needs its own permit, minted for a confirmed request. Open question 9                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            | None                                                                                                                                                                      |
| `/v1/forge/`, `/v1/github/`        | The sandbox makes its forge credential current before `git` and `gh` use it                                                                        | None                             | Stays out. A profile's version control runs in the broker, so a profile has no credential to refresh                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 | None                                                                                                                                                                      |

The watcher, Pub/Sub, chat, Teams and dial routes are not in this document. Each belongs to the
work that moves its caller.

## `/v1/exec` Rules

The text of the group issue names routes and verbs only. The inventory also lists what `/v1/exec`
refuses to a profile and the platform's skills use. These rows keep that list in one place.

| Refused today                                                         | Used by                                             | Proposal                                                                                                                                                      |
| --------------------------------------------------------------------- | --------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `kubectl wait`, `rollout status`, the watch, follow and timeout flags | App onboarding, upgrades, inference skills          | Stays refused. Each holds a broker slot for an unknown time. A skill for a profile reads the state again                                                      |
| Server-side dry-run                                                   | Config Connector and manifest checks                | Stays refused. The command policy counts it as a write                                                                                                        |
| `gcloud auth` and `gcloud config`, reads included                     | The PromQL check scripts of three skills            | Stays refused. A token must not leave the broker. The Cloud API relay row gives those scripts their read, and the scripts must take the project from the task |
| A `gcloud` read that is not in the confined list                      | Backup, Workload Identity, recommender reads        | One list entry in each pull request, with how the command names its project. No entry is planned here                                                         |
| A read with no project (`projects list`, billing, organization)       | Fleet and cost work                                 | Not here. It stays out until the design for reads with no cluster                                                                                             |
| `bq`, `helm`, `tofu`, `leeway`                                        | Cost analysis, chart and Terraform skills, topology | Not here. No role runs them through the broker. They need a tools image or a relay entry, and no design holds that                                            |

## Conformance: Refusals With No Row

A "row" is a test in `tests/conformance/`. A "mutation" is an entry of
`hack/conformance-mutations.py` that removes a refusal and names the row that must go red. The rows
for the profile role hold the route table, the permit, the ownership bar, the task bar, the MCP
set and the audit-report rules, and several of them drive the handler. Most of the other refusals
of the role, such as the kubectl flag allowlist and most confinement rules, have unit tests only.

Four gaps touch the grants of this document:

1. No row holds the exact verb set of the profile role. The row of today asserts the three writes
   and that the set has no other write. A read verb added to the set turns no row red.
2. Four checks have a row that does not go through the handler. The route row reads
   `ROUTE_ROLES`. The verb allowlist has no row at the dispatch: no row calls
   `vcs_verb_permitted`, and no row sends a verb outside the set through `_dispatch_vcs_post`.
   The permit row calls `profile_vcs_refusal`, and no row sends a write without the permit
   through the dispatch. The target rows call `task_confinement.confine`. If the handler
   stops its call to one of these checks, the suite stays green.
3. The import checks have unit tests only: no row and no mutation.
4. The checks on the task credential (signature, expiry, and the binding to the pod, the profile
   and the tier) have unit tests only. The conformance suite does not import that module, because
   the module needs `cryptography`.

Proposal: one pull request with tests only adds rows and mutations for gaps 1 to 3 before the
first grant: the exact verb set, a handler row for the route table and the task gate, a handler row
with a refused verb and with no permit, and the import checks. It adds no authority. The handler
row for the confinement call and gap 4 need a verified credential in the suite, which is open
question 11.

## What Stays Out

- The forge token routes. A profile never holds or refreshes a forge credential.
- A command or route that returns a credential to the caller.
- A command that waits, watches or follows.
- A read of a repository that nobody registered (`snapshot` of an unregistered repository). The
  context repository is open question 3.
- A grant by request field. Each hold reads the verified task credential.

## Order of the Work

1. The conformance rows for gaps 1 to 3, with tests only.
2. The six Monitoring, Trace and Logging entries of the Cloud API relay, with the target check.
3. `capabilities` and `snapshot`, only if the approver accepts the changed import check.
4. The issue verbs and `label-ensure`, after the unattended write permit. The ledger also calls
   `capabilities`, so this step depends on open question 1.
5. The proposal verbs and `branch-delete`, after open question 6.

The work goes one pull request at a time on `credential_proxy.py`. The tests-only change, the
permit gate and the findings route of the state homes design take turns in the same queue.

## Done When

The group issue for blocker 7 is done when:

- [ ] Each row in the tables of this document has one of three results, recorded here by the
      approver: a grant with its hold; "stays out" with a reason; or "moved" to a named design.
- [ ] Each grant is merged as its own pull request, with a conformance row and a mutation. A write
      verb that waits for the permit of another issue is "moved" to that issue's design until the
      permit exists.
- [ ] Each of the four conformance gaps has a row and a mutation, or a written decision.
- [ ] The site page `reference/credential-isolation.md` names each grant.
- [ ] Each grant has its proof. For a verb that the model's tool calls: a paired eval case that
      passes three times on the profile side. For a route, and for a verb that no model tool
      calls: the conformance row and its mutation.

## What Was Checked, and What Was Not

Checked in the code at `b5f4f11f2`: the role table, the verb sets, the two gated sets, the import
checks, the task bar, the report credential's route list, the tools of the `gke` server, the
`curator` rule for `snapshot`, the claim token of `proposal-claim`, the MCP profile set, the
audit-report rules, the entries of the Cloud API relay and their callers, the commands of `vcs.py`,
and the callers of each verb under `agents/`.

Not checked:

- If a Monitoring read on a project that is a metrics scope returns series of other projects.
- How the audit ledger's labels map to a trigger's stream in each case.
- What the in-cluster `gke` tools return under the broker's IAM roles.
- Which scheduled runs carry a cluster target, and which audit streams read a context repository.
- The Hermes side of the callers, and each caller outside `agents/`.
- Each refusal of the profile role against the suite. The four gaps above were read in the
  tests; the sentence about unit tests is from one reading of the broker.

## Open Questions

Each question takes "yes", "no" or "later", and our recommendation follows it. Question 7 is
the exception: another design owns it.

1. May each profile call `capabilities`, held to the managed list? Recommendation: yes.
2. May each profile call `snapshot` for a managed repository? Recommendation: yes.
3. May a profile call `snapshot` for a context repository, which the operator registered for
   reading? The audit's declared-intent search and the `inspect-repository` skill need it.
   Recommendation: later, with the move of the audits.
4. Does a new read verb go to a named profile from an operator list, and not to each profile as
   the eight reads of today? Recommendation: yes, when one kind of work needs the verb.
5. For a ledger issue of an earlier run, is this bar sufficient: opened by the credential's login,
   with the stream label of the trigger, in the publish repository? Recommendation: yes.
6. For a proposal or a branch of an earlier run, which has no stream label and whose caller can be
   a task that a person started: do the verbs stay out until a bar holds the work of an earlier
   run? The unattended write permit design decides that bar for an unattended run, and this
   document follows that answer. Recommendation: yes.
7. Which permit holds the ledger's issue writes? The unattended write permit design decides this,
   and this document follows that answer.
8. Monitoring, Trace and Logging through the relay are held to the task's project and not to its
   cluster, as `gcloud logging read` is today. Acceptable? Recommendation: yes.
9. Does a profile file feedback? Recommendation: no; it stays with the session.
10. Do the six control-plane tools of the `gke` server get a grant? Recommendation: later; gcloud
    gives a profile each of those reads but one.
11. Does the conformance suite take `cryptography` as a dependency, so that rows can verify a task
    credential? Recommendation: yes.
