# The Broker Surface of a Profile

Status: draft for review. Nothing here is built. Each grant is a proposal for the security
approver, and no code follows before that decision.

## Summary

A profile worker reaches the credential broker under one role, `profile`. That role reaches four
routes, 11 of the 27 version-control verbs and two MCP tools. The platform agent's shell role
reaches more, and the platform's work uses the difference:
[the platform-as-profile inventory](platform-as-profile-inventory.md), blocker 7.

This document gives one row for each route and verb that the profile role does not reach. Each row
says what work uses it, how a grant would be held, which check changes, and when. It also lists
what stays out.

The recommendation in short:

- Grant one verb or one route in each pull request, each with a conformance row and its mutation.
- Hold each grant with a fact of the task credential: the target, the permit, the trigger, or the
  profile's name. A read verb is the exception: as the eight reads of today, it reads each managed
  repository for each task of each profile.
- Reads first. Writes wait for the unattended write permit, which has its own design.
- Four designs of the same work are named here and are not written yet: the unattended write
  permit, the hooks, the dial and post-merge state, and reads with no cluster.
- Three things stay out: the forge token routes, a credential that leaves the broker, and each
  command that waits or follows.

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

Two checks run at import and refuse a wrong table (`_validate_role_vcs_verbs`):

- a profile write verb that is not in both gated sets;
- `snapshot` or `capabilities` in the profile set, because they answer for a repository that the
  install does not manage.

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
5. **A write needs a permit and an ownership rule.** The import check keeps this true. This
   document does not relax it.

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
today. `vcs.py`, the model's version-control tool, offers each verb, and a copy of it ships in the
`cluster` persona.

### Reads

| Verb                | Work today                                                                                         | Proposal                                                                                                                                                                                                                                      | Check that changes                                                                                      |
| ------------------- | -------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------- |
| `proposal-reviews`  | The post-merge check reads change requests                                                         | Grant. Held as the eight reads: a task credential and the managed list                                                                                                                                                                        | `ROLE_VCS_VERBS`; a row that holds the exact verb set, which does not exist today                       |
| `proposal-closer`   | The decision-memory sweep reads who closed a proposal                                              | Grant, same hold                                                                                                                                                                                                                              | Same                                                                                                    |
| `revision-contains` | The post-merge check asks if a revision contains a commit                                          | Grant, same hold. It fetches into a scratch repository on the broker, with the size ceiling of `clone`                                                                                                                                        | Same                                                                                                    |
| `capabilities`      | `submit-suggestion` reads the install's write mode before a publish                                | Grant only with a new hold: for the profile role the dispatcher resolves the repository and asks the managed list, as for each other verb                                                                                                     | The import check; a new row: a profile's `capabilities` for a repository that is not managed is refused |
| `snapshot`          | `inspect-repository`, the audit collectors and the API deprecation scan read one revision as a tar | Grant for a managed repository only, the rule that the `curator` role has today. That serves the collectors. It does not serve `inspect-repository`, which exists to read a context repository or one that nobody registered. Open question 1 | The import check; a new row (the `curator` rule has a unit test only)                                   |
| `proposal-claim`    | The `pr_opened` plugin claims a proposal with the token of its `proposal-create` answer            | No grant in this document. A profile's `proposal-create` answer carries the token today, but the caller of the claim is the replacement of that plugin, which the hooks design owns                                                           | None now                                                                                                |

### Writes

A profile write of today passes three bars: the permit, the install's own work, and the work of this
task. The inventory names two; the task bar is newer than its snapshot. The ownership bar and the
task bar each refuse a verb that they do not know, and the task record does not outlive its task.
A new write needs a branch in each bar, and two things that do not exist today: a permit that an
unattended run can hold, and a permit check for each verb (the broker checks one permit name for
each gated verb now). The permit is the subject of the unattended write permit design.

Most of the work below is on an object of an earlier run: a ledger issue, a stale proposal, a
review reply, a spent branch. The task bar of today cannot hold that work. Each row names the bar
that can, and open question 3 asks what replaces the task bar.

| Verb                                           | Work today                                                                                                                                   | Bars that the grant needs                                                                                                                                                                                                                                                                                         | When                  |
| ---------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------- |
| `issue-create`                                 | The audit ledger opens one issue for each stream. Two scripts with no model open one too: the chat-delivery watch and the first-run delivery | Only in the repository that the install publishes to, the rule of the report store for the same trigger. The labels are those of the stream that the trigger names; the broker writes them, not the caller. The two scripts name no stream: they are script runs, and this bar does not fit them                  | After the permit      |
| `issue-comment`, `issue-update`, `issue-close` | The audit ledger; the chat-delivery watch; the issue resolver                                                                                | For the ledger: the issue is the install's own, opened by the credential's login, with the stream label of the trigger, in the publish repository. The issue resolver is not covered: it works on issues that people open and must not touch the install's own, so it needs a different bar and its own row later | After the permit      |
| `label-ensure`                                 | The audit ledger, the resolver, the chat-delivery watch and the first-run delivery make their labels                                         | Only in the publish repository. The label name is in a closed set for the stream. A profile cannot make the auto-merge label                                                                                                                                                                                      | With the issue verbs  |
| `proposal-comment`                             | Replies on a proposal; the post-merge follow-ups                                                                                             | The ownership bar: the install's own proposal (`is_agent_pull_request`). The task bar passes only a proposal of the same task, so a reply or a follow-up in a later run needs what open question 3 decides                                                                                                        | After the issue verbs |
| `proposal-close`, `proposal-acknowledge`       | The audit closes a stale proposal; a reply marks a review as seen                                                                            | Same                                                                                                                                                                                                                                                                                                              | After the issue verbs |
| `branch-delete`                                | `submit-suggestion` uses a branch name again; the audit and the auto-merge switch delete a spent branch                                      | The broker's rule of today: under the install's branch prefix, and the proposal is done. A spent branch is from an earlier run, so the task bar cannot hold it. Open question 3                                                                                                                                   | After the issue verbs |
| `proposal-practice-label`                      | The audit labels an auto-merge candidate                                                                                                     | No grant in this document. It belongs to the dial and auto-merge state                                                                                                                                                                                                                                            | With the dial design  |

## Routes

| Route                          | Work today                                                                                                     | Proposal                                                                                                                                                                                                                                                                                                                                                                                                            | Check that changes                                                                                         |
| ------------------------------ | -------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------- |
| `gke` MCP server on `/v1/mcp/` | Control-plane reads in the cluster audits                                                                      | Grant the control-plane tools only (cluster, node pool and operation reads), with a per-task target check: each resource argument must resolve to the task's target; a wildcard, a missing argument or an unknown shape is refused; a tool with no argument map is refused. The in-cluster tools stay out: they read any kind, and kubectl on `/v1/exec` is the profile's read of the cluster with its own refusals | `PROFILE_SERVER_TOOLS`; a per-tool argument table; rows for each argument shape and for an in-cluster tool |
| `/v1/gcp/` (Cloud API relay)   | Monitoring, Trace, GKE cluster and Logging reads for the cost audit, the health sweep, the observability skill | Grant with a per-task target check: the project in the path or the body is the task's project, and a cluster in the path is the task's cluster. A credential with no target is refused                                                                                                                                                                                                                              | `ROUTE_ROLES`; the relay handler; rows: other project, other cluster, no task                              |
| `/v1/exec/admission`           | A fleet sweep sizes its pool                                                                                   | No grant now. A task reads one cluster, so it has no pool to size. Decide again with the design for reads with no cluster                                                                                                                                                                                                                                                                                           | None                                                                                                       |
| `/v1/feedback/`                | The feedback skill files one confirmed report                                                                  | No grant now. It is a write to a form outside the install on a person's confirm. It needs its own permit, minted for a confirmed request. Open question 2                                                                                                                                                                                                                                                           | None                                                                                                       |
| `/v1/forge/`, `/v1/github/`    | The sandbox makes its forge credential current before `git` and `gh` use it                                    | Stays out. A profile's version control runs in the broker, so a profile has no credential to refresh                                                                                                                                                                                                                                                                                                                | None                                                                                                       |

A task with no cluster carries a report credential or none. The broker refuses a report credential
on each route outside a fixed list, and `/v1/vcs/` is not on that list. So a run that a schedule
starts with no cluster reaches no version-control verb today, whatever the verb set says. A grant
that the trigger holds, such as the issue verbs for an audit, also needs an entry in that list for
the one verb, with its own row.

The watcher, Pub/Sub, chat, Teams and dial routes are not in this document. Each belongs to the
work that moves its caller.

## `/v1/exec` Rules

The text of the group issue names routes and verbs only. The inventory also lists what `/v1/exec`
refuses to a profile and the platform's skills use. These rows keep that list in one place.

| Refused today                                                   | Used by                                             | Proposal                                                                                                           |
| --------------------------------------------------------------- | --------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------ |
| `kubectl wait`, `rollout status`, watch and follow flags        | App onboarding, upgrades, inference skills          | Stays refused. Each holds a broker slot for an unknown time. A skill for a profile reads the state again           |
| Server-side dry-run                                             | Config Connector and manifest checks                | Stays refused. The command policy counts it as a write                                                             |
| `gcloud auth`, `gcloud config` writes                           | The PromQL check scripts of three skills            | Stays refused. A token must not leave the broker. The Cloud API relay row gives those scripts their read           |
| A `gcloud` read that is not in the confined list                | Backup, Workload Identity, recommender reads        | One list entry in each pull request, with how the command names its project. No entry is planned here              |
| A read with no project (`projects list`, billing, organization) | Fleet and cost work                                 | Not here. It is the subject of the design for reads with no cluster                                                |
| `bq`, `helm`, `tofu`, `leeway`                                  | Cost analysis, chart and Terraform skills, topology | Not here. No role runs them through the broker. They need a tools image or a relay entry, and no design holds that |

## Conformance: Refusals With No Row

A "row" is a test in `tests/conformance/`. A "mutation" is an entry of
`hack/conformance-mutations.py` that removes a refusal and names the row that must go red. The rows
for the profile role hold the route table, the permit, the ownership bar, the task bar, the MCP
set and the audit-report rules, and several of them drive the handler. Most of the other refusals
of the role, such as the kubectl flag allowlist and most confinement rules, have unit tests only.

Four gaps touch the grants of this document:

1. No row holds the exact verb set of the profile role. The row of today asserts the three writes
   and that the set has no other write. A read verb added to the set turns no row red.
2. Three checks have a row that does not go through the handler. The route row reads
   `ROUTE_ROLES`. The permit row calls `profile_vcs_refusal`, and the rows that drive the
   version-control dispatch use a permitted verb and a credential with the permit. The target rows
   call `task_confinement.confine`. If the handler stops its call to one of these checks, the
   suite stays green.
3. The two import checks have unit tests only: no row and no mutation.
4. The checks on the task credential (signature, expiry, and the binding to the pod, the profile
   and the tier) have unit tests only. The conformance suite does not import that module, because
   the module needs `cryptography`.

Proposal: one pull request with tests only adds the rows and mutations for gaps 1 to 3 before the
first grant. It adds no authority. Gap 4 gives the suite a new dependency, which is open
question 5.

## What Stays Out

- The forge token routes. A profile never holds or refreshes a forge credential.
- A command or route that returns a credential to the caller.
- A command that waits, watches or follows.
- A read of a repository that nobody registered (`snapshot` outside the managed list).
- A grant by request field. Each hold reads the verified task credential.

## Order of the Work

1. The conformance rows for gaps 1 to 3, with tests only.
2. The three read verbs, one pull request for each verb.
3. The `gke` MCP server with its target check.
4. The Cloud API relay with its target check.
5. `capabilities` and `snapshot`, only if the approver accepts the changed import check.
6. The issue verbs and `label-ensure`, after the unattended write permit.
7. The proposal verbs and `branch-delete`.

The work goes one pull request at a time on `credential_proxy.py`.

## Done When

The group issue for blocker 7 is done when:

- [ ] Each route and verb in the tables of this document has a decision: a grant with its hold, or
      "stays out" with a reason.
- [ ] Each grant is merged as its own pull request, with a conformance row and a mutation.
- [ ] Each of the four conformance gaps has a row and a mutation, or a written decision.
- [ ] The site page for what the agent is permitted to do names each grant.
- [ ] For each verb family, a paired eval case passes on the profile side.

## What Was Checked, and What Was Not

Checked in the code at `b5f4f11f2`: the role table, the verb sets, the two gated sets, the import
checks, the task bar, the report credential's route list, the tools of the `gke` server, the
`curator` rule for `snapshot`, the claim token of `proposal-claim`, the MCP profile set, the
audit-report rules, the entries of the Cloud API relay, and the callers of each verb under
`agents/`.

Not checked:

- If a Monitoring read for a cluster must name a project other than the cluster's project.
- How the audit ledger's labels map to a trigger's stream in each case.
- What the in-cluster `gke` tools return under the broker's IAM roles.
- Which scheduled runs carry a cluster target.
- The Hermes side of the callers, and each caller outside `agents/`.
- Each refusal of the profile role against the suite. The four gaps above were read in the
  tests; the sentence about unit tests is from one reading of the broker.
- No install was read for this document. The conformance suite was run once, with no change, to
  confirm that its rows pass.

## Open Questions

1. `capabilities` and `snapshot`: is a grant with a managed-list hold acceptable, or do the two stay
   out of the profile role? For `snapshot`, does a profile also read a context repository, which
   the operator registered for reading?
2. The feedback route: does a profile file feedback, or does that stay with the session?
3. Work on an object of an earlier run (a ledger issue, a stale proposal, a review reply, a spent
   branch): the task bar cannot hold it, and the task record does not outlive its task. Is the
   ownership bar with the trigger's stream label sufficient, or must the broker keep a durable
   record of what each stream opened?
4. The Cloud API relay: one grant for the eight entries, or one grant for each entry?
5. The task credential checks: does the conformance suite take `cryptography` as a dependency, or do
   those checks stay with the unit tests?
