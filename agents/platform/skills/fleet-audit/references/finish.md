# fleet-audit — `finish`, partial coverage, and no forge

Part of the `fleet-audit` skill (`/opt/data/profiles/platform/skills/fleet-audit/SKILL.md`), which says when to open this file.

### Step 3 — `finish`

```bash
/opt/data/profiles/platform/skills/fleet-audit/scripts/audit_report.py finish \
  --audit <audit-id> \
  --findings-file <findings_path> \
  [--repo "<owner>/<repo>"] \
  [--manifest-file <path> | --no-collector-manifest "<why>"] \
  [--decline-fix <finding-id> "<why>" ...]
```

The last pair belongs to a stream whose SOP runs a collector (the repository's collector-manifest
design says what the manifest holds), and on such a stream one of the two is **required**:
`--manifest-file` names the manifest the collector wrote and `--no-collector-manifest` publishes
without one, reporting the reason as a coverage gap. A collector stream given neither exits 2, dry
run included. An SOP that mentions neither runs `finish` without them.

A stream with a collector runs it before Step 2's inspection, not after: the SOP names the script
and the path to write its manifest to, the manifest's `commands` are that cluster's `checks_run`
and its `candidates` are the findings the collector vouches for, and Step 3 passes the same file as
`--manifest-file`. Today that is the drift stream — `/opt/data/profiles/platform/governance/fleet_consistency_drift_sop.md` §4
says how to read its manifest and what is still yours to write — the upgrade and patch readiness
stream, whose `/opt/data/profiles/platform/governance/security_patch_orchestrator_sop.md` §3 does the same, and the three
streams `collect.py` covers: compliance (`/opt/data/profiles/platform/governance/compliance_audit_sop.md` §2), obtainability
(`/opt/data/profiles/platform/governance/obtainability_audit_sop.md` §2) and AI security (`/opt/data/profiles/platform/governance/ai_security_audit_sop.md`
§3), the cost stream (`fleet_waste.py`, `/opt/data/profiles/platform/governance/fleet_wide_cost_analysis_sop.md` §2), the
stockout stream (`fleet_stockout.py`, `/opt/data/profiles/platform/governance/stockout_prevention_sop.md` §3), the GCE
compute stream (`compute_fleet_audit.py`, `/opt/data/profiles/platform/governance/gce_compute_fleet_sop.md` §2) and the networking
stream (`networking_audit.py`, `/opt/data/profiles/platform/governance/gcp_networking_fabric_sop.md` §2).
The compliance, obtainability, stockout, cost, GCE compute, networking and upgrade collectors may also give a target `checks_unevaluated`,
`{check, reason}` for a check whose own read failed or whose inputs could not decide it: it did not run and is not inapplicable, so it
goes in neither `checks_run` nor `checks_not_applicable` but in that target's `limitations`, which
keeps the run partial and leaves open every finding that check filed there. `finish` rejects the
slug anywhere else.

The script validates the document, reconciles every finding against the pull requests already open
for this stream, rewrites (or opens) the ledger issue, comments the delta, opens pull requests for
the fixes that qualify, and closes the ones whose findings have stopped reproducing. It prints one
JSON line — `status`, `issue_url`, `new`, `resolved`, `prs_opened`, `prs_still_open` (remediation
pull requests already open on findings this run still carries; empty on a clean run), `prs_closed`,
`partial`, `coverage_gaps`, `unreported_under_gap` (only when not empty), `silent_ok`, `declared`, the number of postures a repository
declaration kept off the ledger (it never decides silence), `postures_withheld`, the ids of the
posture findings `finish` held back because the document recorded no complete declared-intent
search (empty everywhere but on a declaring stream that skipped the step; see
[`declared_intent_searched`](findings-document.md#declared_intent_searched)), and `unaccounted`, the ids of the previous
findings a clean run was refused its close over (empty on every other outcome; see
[The clean run](clean-run.md#the-clean-run)). A `finish` that follows an earlier `finish` of the same
repository in the run also carries `refinish: true`; when that earlier `finish` wrote the report
store, `new` and `resolved` count against it. When that earlier `finish` reported news or opened a
pull request, the line also carries `silent_floor: true`: its `silent_ok` stays `false` and its
sweep opens no automatic pull request, so the findings it would have opened are listed as
awaiting `/remediate`. Report it as any other line. With `--manifest-file` the line also carries
`unpublished_candidates`, `wholly_unpublished_checks` and `uncorroborated_findings` — which are
absent on every other run. The line also carries timing data in seconds, which decides nothing:
`publish_s`, the time `finish` used to publish; `inspect_s`, the time from `start` to `finish`, when
the run has the note `start` wrote; and `collect_s`, the collector's own time, when the manifest
gives its `started_at` and `finished_at`. A timer without its input is absent. The lines look like this:

- `{"status":"OPENED","issue_url":"…","new":7,"resolved":0,"prs_opened":["…"],"prs_still_open":[],"prs_closed":[],"partial":false,"coverage_gaps":[],"silent_ok":false,"declared":0,"postures_withheld":[],"unaccounted":[],"publish_s":41.2}`
  — the stream had no open ledger.
- `{"status":"UPDATED","issue_url":"…","new":2,"resolved":3,"prs_opened":[],"prs_still_open":["…"],"prs_closed":["…"],"partial":false,"coverage_gaps":[],"silent_ok":false,"declared":0,"postures_withheld":[],"unaccounted":[],"publish_s":41.2}`
  — the existing ledger was rewritten.
- `{"status":"CLEAN","issue_url":"…","new":0,"resolved":5,"prs_opened":[],"prs_still_open":[],"prs_closed":["…"],"partial":false,"coverage_gaps":[],"silent_ok":false,"declared":0,"postures_withheld":[],"unaccounted":[],"publish_s":41.2}`
  — zero findings; the ledger closed as completed and its open fixes closed with it.
- `{"status":"HELD","issue_url":"…","new":0,"resolved":0,"prs_opened":[],"prs_still_open":[],"prs_closed":[],"partial":false,"coverage_gaps":[],"silent_ok":false,"declared":0,"postures_withheld":[],"unaccounted":["cluster-admin-binding.acme-prod-us-east1-prod-us-east._.clusterrolebinding-debug-binding"],"publish_s":41.2}`
  — zero findings, but the ledger was **not** closed, for one of two reasons. Either it carried
  findings whose checks this run's own `checks_run` says ran again, and the document neither reports
  nor explains them (their ids are in `unaccounted`); or `unaccounted` is empty and the issue's
  comments could not be read, so a `/remediate` may be standing unanswered there (stderr says
  `comments could not be read`). Not a clean result; report it as
  [The clean run](clean-run.md#the-clean-run) says.
- `{"status":"NO_FORGE","issue_url":null,…,"findings":4,"critical":2,"no_forge":"…","summary":"…"}`
  — no forge is configured, or the broker cannot reach the repository, so nothing was published; see
  [No forge configured](#no-forge-configured).
- `{"status":"STORE_LEDGER","issue_url":null,…,"prs_opened":["…"],"ledger":"store","ledger_note":"…","summary":"…"}`
  — the forge has no issue tracker, or no labels: the ledger is in the report store and chat, and
  the remediation pull requests still open on the forge; see
  [A forge with no issue tracker](#a-forge-with-no-issue-tracker).

Add `--dry-run` to validate and print the rendered ledger body — and every PR body it _would_ open —
to stdout with **zero** git or forge side effects. It applies the same grouping and the same
degradation as the real run, so the branch names it names are the branch names it would create,
except a PodDisruptionBudget `finish` would write for a declared `no-pdb` finding, which only the
real run plans. It
resolves every `remediation.path` against the same `workspace` directory the real run uses, not against
the directory you happen to be standing in, so "the manifest is missing" is a finding of the dry run
and not a surprise at publish time. Use it whenever you are unsure your document is well formed.
With no forge configured, the dry run still validates the document the same way, but its preview
shows the ledger and pull-request bodies a forge would get; the real run publishes neither.

Exit 0 means published. **Exit 2 means the run was rejected before publishing anything** — fix what
the message names and re-run; never delete the finding that tripped it. The one exception is a
`BROKER UNAVAILABLE` line, below: the broker can go away partway through, so what the command had
already published stands, and the re-run finishes the rest. What reaches exit 2: the
document failed a field rule, the file named by `--findings-file` is missing or is not valid JSON,
`--audit` is not one of the registered ids in the skill's stream table, the document contradicts the collector manifest
named by `--manifest-file`, that manifest is missing or malformed, `--manifest-file` was given an
empty path, `--no-collector-manifest` was given a blank reason, a collector stream was given
neither, a `--decline-fix` named a fix the refusal did not list, or, on a run `start` opened, a fix
the sweep would open is neither written nor declined (see
"Write every `manifest` remediation file" in the skill's red lines). A manifest that finished before
this run's `start` opened reaches exit 2 too: the collector writes to a fixed path that is not
scrubbed between runs, so a run whose collector never ran finds the previous one's manifest sitting
there, and cross-checking against a week-old reading of the fleet is worse than cross-checking
against nothing. Re-run the collector. Exit 1 is fatal and means
something else broke. Two exit 2s are not a document to fix. A `START REFUSED` line from `start`
means the stream's in-flight guard held (see [Running a stream on demand](on-demand.md#running-a-stream-on-demand)); there is nothing to edit
and nothing to re-run until that run's `finish`. A `BROKER UNAVAILABLE` line, from any command, names
`the broker at` an address: the credential proxy is down, unreachable, refusing this sandbox's token or
on an older build, so check the credential-proxy pod and re-run the same command once it answers.
From `start`, nothing is in flight. From `finish`, the run is still in flight and its document is
untouched: re-run `finish`, never `start`. Do not report that nothing reached GitHub: the ledger, a
label or a pull request may already have landed, and the re-run picks up from them. On a run `start`
opened, re-run within two hours of its last step, a re-run after the sweep opened a pull request
opens no further automatic ones (`silent_floor`, above).

### Partial coverage

`partial` is `true` when the run cannot vouch for the ledger's state: it missed part of the fleet,
its collector manifest was waived, or, on a clean run, the report store lost its record of the
ledger. A findings run over a lost record stays `partial: false`; its delta is withheld and it is
never `silent_ok` instead. Four sources are in the
document: any entry in `scope.skipped`, any cluster carrying a `limitations`
note, any cluster whose `checks_run` is short of the checks that _apply_ to it, or — on a stream
with a declared-intent step — posture checks that ran without a complete search record
([`declared_intent_searched`](findings-document.md#declared_intent_searched)). The rest belong to the run rather than
to the document, so a document that reads as complete can still produce them: a collector manifest
waived with `--no-collector-manifest`, whose reason becomes the gap, and a clean run over an open
ledger the report store has no trusted record of, on any stream, with or without a manifest (see
[The clean run](clean-run.md#the-clean-run)). `coverage_gaps` says which, and why — so
`partial` is `true` if and only if `coverage_gaps` is non-empty, and you can report from either.

A check the cluster's shape rules out is not a gap. Declaring it in that cluster's
`checks_not_applicable` ([findings-document.md](findings-document.md)) takes it out of the denominator, so a cluster that ran everything
that _can_ apply to it is a fully covered cluster. Without that, a fleet of Autopilot clusters is
permanently partial: the ledger never closes, `resolved` is pinned at `0`, and no stale remediation
pull request is ever cleaned up, the shield's close (SOP 2.7) aside.

It does not mean "the description was truncated." A ledger too long for GitHub's body limit says so
in its own body and still carries true totals in its title; the audit saw everything, so nothing
about what the run may conclude changes. `partial` tracks coverage and, on a clean run, a lost store
record, never the body's length.

A gap changes what the run is _allowed to conclude_, because a finding's absence from an unread
cluster is not evidence that it was fixed. Over a partial run the harness:

- reports `resolved: 0` and posts no "resolved" delta, rather than announcing fixes it cannot see;
- closes **no** remediation pull request as stale, so a fix survives to the next complete run, except the shield's close (SOP 2.7), which rests on the declaration rather than on this run's reading;
- does **not** close the ledger, even with zero findings — the issue stays open and gains a comment
  naming the gaps (not again on the next clean run if the gaps did not change). `status` is `CLEAN` where the run accounted for every finding the previous
  ledger held, and `HELD` where it did not, which is a separate refusal that a gap neither causes
  nor prevents (see [The clean run](clean-run.md#the-clean-run)). A coverage gap self-heals the day the fleet
  is fully readable again; a lost store record does not, and clears only on a run that reports
  findings or when a maintainer who has checked them closes the ledger.

A partial run with a new or changed gap is never `[SILENT]` — `finish` returns `silent_ok: false`
for it. A gap that is the same as on the stored previous run does not stop `silent_ok` on its own.
Report the issue URL and say which clusters were not covered. See [The clean run](clean-run.md#the-clean-run)
for the full rule.

### No forge configured

An install with no GitOps repository registered in `$GITOPS_STATE_CONFIGMAP` (and no `--repo`) has
no forge to write a ledger to. A run whose repository the broker cannot reach (the forge says that it
does not exist, or that the credential cannot read it) is the same: its `no_forge` note names the
repository and the reason, and the JSON line of `finish` carries them under `unreachable_repo`. The
check is one issue listing that `start` makes through the broker (one pull request listing when the
ledger goes to the report store: a forge with no issue tracker, or with issues and no labels), and
`finish` follows its result.
The run does not choose another repository, and it is never silent (`silent_ok: false`). A run for
which several repositories are managed and none can be chosen (none has `role: gitops`) is the same
too: its note gives the reason and the fix, and the JSON line carries it under
`no_repository_chosen`. The audit runs anyway:

- `start` skips the credential, the labels and the ledger lookup, and prints `"mode": "none"`,
  `"repo": null`, `"issue": null`, a `no_forge` note, and a `workspace` path to an empty directory. Pass that `workspace` to the collector as the SOP says. There is no repository, so
  `declared_intent_repos` lists only `context_repos`, and `fetch`, `list`, `grep` and `remediate`
  have nothing to act on: do not run them.
- Run every check as the SOP says and write the findings document as usual. A `manifest`
  remediation is still a valid way to describe a fix, but nothing opens it as a pull request.
- `finish` validates the document as usual, then writes it to the report store instead of a ledger
  and prints `status: "NO_FORGE"`, `issue_url: null`, `new`, `resolved`, `findings`, `critical`,
  the `no_forge` note, and `summary`. If the store write fails, the note and the summary say so.
- On an install with no repository, `new` and `resolved` are against the previous no-forge run, and
  a previous finding the collector still flags is held, as on a ledger: it stays in the store and is
  not resolved.
- On a run whose repository could not be reached, or for which no repository could be chosen,
  the change is unknown: `new` and `resolved` are 0, nothing is held, and `silent_ok` is false. An
  unreachable repository's report is stored under that repository; a run with no repository chosen
  is stored as a run with no forge.

**Report.** `status: "NO_FORGE"` replaces the SOP's report lines, which all name a ledger URL that
does not exist. `silent_ok` decides silence as usual: on a scheduled run, `silent_ok: true` → exactly
`[SILENT]`. Otherwise, and on every on-demand run, your final response is `summary` verbatim. It
names the most severe findings with the object each one is about, the ids held for the collector,
the coverage gaps, and that no forge is configured or which repository could not be reached. Do not report the run as
failed, and do not invent an issue URL.

### A forge with no issue tracker

A repository on a forge that has no issue tracker (Bitbucket), or that has issues and no labels, has
no ledger issue: the ledger is found by its label. `start` asks the forge's `capabilities` and
decides. The ledger goes to the report store and to chat, as with no forge, and the remediation pull
requests still open on the forge. `start` writes the decision into the run record, and `finish`
follows it. A `capabilities` answer that cannot be read keeps the ledger issue.

- `start` clones the repository as usual and prints `"issue": null`, the `repo`, the usual `mode`
  and `workspace`, `"ledger": "store"`, and a `ledger_note` that says why. `carried` lists the
  findings the stored ledger carries. `pending_remediation_requests` is always empty: there is no
  ledger thread.
- Write the findings and the `manifest` fixes as usual. `finish` applies the same refusals and the
  same Kustomize and declaration checks as with a ledger issue, and the sweep opens the same pull
  requests, at most five a run. On a forge with no labels they carry the stream in their
  description and their branch name instead of labels.
- `finish` prints `status: "STORE_LEDGER"`, `issue_url: null`, `new` and `resolved` against the
  previous such run, `prs_opened`, `prs_still_open`, `prs_closed`, `unaccounted`, `findings`,
  `critical`, `"ledger": "store"`, the `ledger_note`, and `summary`. A clean run closes a stale pull
  request only when coverage is complete and every finding the stored ledger carries is reported or
  explained under `resolved_because` and not still flagged by the collector, as with a ledger issue.
  If the forge refuses the listing of the remediation pull requests (too many pull requests in the
  repository, or an unknown login), `finish` still stores the findings, opens, updates and closes
  no pull request, and says why in `remediation_refused` and in `summary`. Such a run is never
  silent. A previous finding the collector
  still flags is held on the stored ledger, and the next `start` lists it in `carried`.
- `silent_ok` follows the ledger-issue rule: nothing is wrong with the repository, so a scheduled
  run with nothing new is silent. A run the store does not hold is never silent.
- If the store lost the previous ledger, a clean run closes no pull request, and its
  `coverage_gaps` says why. The store loses it when its last write failed, or when its record cannot
  be read. A store that does not exist, or that holds only records of runs that could not reach the
  repository in a ring that is not full, is a first run when no remediation pull request of the
  stream is open, and a lost ledger when one is open. A new identity scheme does not lose it. It stores a record that the next run also reads as lost, so each clean
  run after it closes nothing until a run that reports findings stores the ledger again. Such a
  run is never silent.
- A `/remediate` cannot be commented on a ledger. A person asks in chat, and you run `remediate` for
  the ids they name ([remediation.md](remediation.md)).

**Report.** As for `NO_FORGE`: `silent_ok` decides silence, and otherwise your final response is
`summary` verbatim. It names the findings, the pull request links, and why the ledger is in the
report store. Do not invent an issue URL.
