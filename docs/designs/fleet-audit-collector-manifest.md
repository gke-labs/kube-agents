# Fleet Audit — The Collector Manifest

> **STATUS — design of record; the `finish` side is implemented, two collectors ship.**
> `audit_report.py finish` accepts a manifest through `--manifest-file` and applies every rule in
> §3. `agents/platform/skills/fleet-audit/scripts/fleet_drift.py` emits one for the
> `fleet-consistency-drift` stream and `patch_readiness.py` one for `security-patch-orchestrator`;
> each stream's SOP runs its collector and passes the flag, and every other stream
> publishes on the document's own attestation, exactly as it did before the flag existed.

**Scope:** the machine boundary between a per-stream collector script and the fleet-audit harness.
The ledger itself, the delta, coverage gaps, and remediation pull requests are
[`fleet-audit-issue-ledger.md`](fleet-audit-issue-ledger.md); this document adds one input to that
design's `finish` step and says what it changes.

---

## 1. Why a manifest

The findings document's `checks_run` is attestation: the harness cannot observe the agent's tool
calls, so "I ran these eleven checks against these three clusters" is a sentence the model writes and
the harness takes on trust. A collector that executes the commands itself produces a record the
harness can hold the document to — which commands ran where, how each ended, and what the collector
would flag. The manifest is that record.

The claim is not impossibility. The manifest is a file in the agent's filesystem, so a model
determined to fabricate can forge one. The claim is that the failure the ledger design's §7.2
recounts — a ten-word attestation line becoming a published all-clear — now requires forging a
multi-field document whose per-command record is mechanically checkable against a re-collection.

## 2. Shape

One JSON object per run. `finish` reads only the keys marked **read**; the rest are carried for the
status surface and the collectors' own bookkeeping, and `finish` ignores them today.

```json
{
  "version": 1,
  "checks_revision": "sha256 of the collector's check logic",
  "audit": "compliance-audit",
  "started_at": "2026-09-18T06:00:00Z",
  "finished_at": "2026-09-18T06:03:30Z",
  "clusters": [
    {
      "name": "prod-usc1",
      "project": "acme-prod",
      "location": "us-central1",
      "autopilot": true,
      "outcome": "collected",
      "commands": [
        {
          "check": "cluster-admin-binding",
          "command": "KUBECONFIG=… kubectl get clusterrolebindings -o json",
          "rc": 0,
          "duration_s": 8.2,
          "output_sha256": "…"
        }
      ],
      "checks_not_applicable": [
        {
          "check": "hostpath-mount",
          "reason": "Autopilot rejects hostPath volumes at admission"
        }
      ],
      "candidates": [
        {
          "check": "cluster-admin-binding",
          "cluster": "prod-usc1",
          "namespace": "",
          "object": "ClusterRoleBinding/legacy-admin",
          "severity": "critical",
          "excerpt": "subjects: Group/all-engineers",
          "command": "kubectl get clusterrolebinding legacy-admin -o json",
          "impact": "…",
          "impact_authoritative": false,
          "needs_triage": null
        }
      ]
    },
    {
      "name": "dr-west",
      "autopilot": true,
      "outcome": "unreachable",
      "error": "get-credentials rc=1: …"
    }
  ]
}
```

| Key                                        | Read by `finish` | Meaning                                                                                                                                                                                                                                                                                                                                                                              |
| ------------------------------------------ | ---------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `clusters[]`                               | **read**         | One entry per target the collector enumerated. The `name` is what `scope.clusters[].name` will say — a cluster name, `project/<id>`, or `<project>/<region>/<subnet>`.                                                                                                                                                                                                               |
| `clusters[].outcome`                       | **read**         | `collected` means the collector read the target and vouches for `commands`. `unreachable` and `gate-failed` mean it did not, and `error` says why; the document accounts for such a target or is refused. `out-of-scope` means the target is not this audit's: it is not cross-checked, the document need not list it, and it contributes no gap (logged once at INFO).              |
| `clusters[].commands[]`                    | **read**         | One record per check per target: the check slug, the literal command, its exit code. `rc == 0` is what makes a check "run" for the rules below. `duration_s` and `output_sha256` are carried, not read.                                                                                                                                                                              |
| `clusters[].checks_not_applicable[]`       | **read**         | Checks the collector itself dispositioned as having nothing to run against on this target. The collector is the authority on applicability; §3.1 holds the document to it in both directions.                                                                                                                                                                                        |
| `clusters[].candidates[]`                  | **read**         | What the collector would flag: `(check, namespace, object)` plus `excerpt` and `impact`. `cluster` is optional and defaults to the enclosing entry's `name`. `command`, `impact_authoritative` and `needs_triage` are optional and read in §3. `severity` is carried, not read by `finish`: the stream's SOP says whether the model copies it or re-judges it against fleet context. |
| `audit`                                    | **read**         | The stream the manifest was written for. When present it must equal `--audit`, the way `load_findings` holds the document to it; a mismatch is a validation error naming both. Absent, the manifest is accepted.                                                                                                                                                                     |
| `finished_at`                              | **read**         | When the collector stopped. Compared against the `started_at` the harness records at `start`: a manifest that finished before this run opened is a previous run's collection, and is refused rather than cross-checked, because the fixed path the SOPs name is not scrubbed between runs. Absent or unparseable on either side is "cannot tell" and the manifest is accepted.       |
| `version`, `checks_revision`, `started_at` | carried          | Shape version, digest of the check logic, and the collector's own start. Reserved for a run-over-run comparison and the timing view that a later change adds; `finish` does not read them today.                                                                                                                                                                                     |
| `clusters[].autopilot`, `.project`, …      | carried          | Fleet facts the collector resolved during enumeration, for the SOP to copy rather than re-derive. `clusters[].limitations` is here too: the collector's own sentence saying what it read but did not compare on that target, which the SOP copies into the document where it becomes a coverage gap.                                                                                 |
| `error` (top level)                        | carried          | Set only on a run that produced no cluster entry at all — enumeration itself failed, or every target in scope failed its read. The collector exits non-zero with it, and the SOPs answer it by not calling `finish`, so in practice `finish` never sees a manifest carrying it.                                                                                                      |

Rules for a collector: every enumerated target appears with an `outcome`; a gate failure (zero-byte
or truncated read) is `outcome: "gate-failed"`, never a shorter candidate list; a candidate is
identified by the same four fields as a finding, so
`derive_finding_id({check, cluster, namespace, object})` on a candidate equals the id of the finding it
would become, and wherever `finish` prints or compares it against the ledger it is clipped the way a
finding id is; `excerpt` is cut from the collector's own output under the same credential-projection
rules the SOPs mandate, with the harness redactor as the backstop; a run that enumerated
nothing says so in the top-level `error` rather than emitting an empty `clusters` array, which
would otherwise be indistinguishable from a fleet holding no clusters; and a run that enumerated
_part_ of the fleet carries the rest as a `gate-failed` target, because a scope that silently
narrowed reads as a complete one and lets `finish` resolve every finding outside it. A target name is unique
within the manifest and stable between runs, so a collector sweeping clusters names each one
`<project>/<location>/<name>` — a GKE name is unique only inside one project and location, and a
name qualified only where it collides today moves when the rest of the fleet changes, which is a
finding announced resolved and refiled as new. The drift and patch collectors do this, and their SOPs carry
the qualified form into `scope.clusters[].name`, which is the key §3.1 matches on. The qualification stops at the
target name: a candidate's `object` names the bare resource, because the identity tuple
already carries the qualified cluster and `_shorten_id` spends a duplicate on the segment it
then truncates.

## 3. What `finish` does with it

`finish --manifest-file <path>` loads the manifest (a missing file, malformed JSON, a non-object, a
`clusters` that is not a list, another stream's `audit`, or a `finished_at` earlier than this run's
`started_at` is a validation error, exit 2) and applies the following before
anything is rendered or published. Every rule is scoped to what the manifest covers: a target the
manifest never enumerated is governed by the ledger design's ordinary roster rules and nothing here.

### 3.1 Cross-check — `cross_check_manifest`

An entry with a name and no usable `outcome` is skipped with a warning naming it — that target is
not cross-checked — rather than failing the run; an entry marked `out-of-scope` is skipped with an
INFO line, owed nothing by the document and held to nothing. Rejections, each a validation error
before any `gh` call:

- A target the manifest marks `collected` is absent from `scope.clusters`. The collector proved it
  readable, so its absence is a defect in the document, not a coverage gap — a gap would still let
  the run publish a full-fleet all-clear off part of the fleet.
- A target the manifest enumerated with any other outcome is absent from both `scope.clusters` and
  `scope.skipped`. A collector failure is the likeliest place for a finding to be hiding, so it is
  the worst target to lose silently; either surface reports the gap.
- A `collected` target's `checks_run` names a check with no `rc == 0` command for it in the manifest.
- A target with any other outcome claims `checks_run` and carries no `limitations`. Hand-collection
  after a collector failure is allowed; reporting it as an ordinary full read is not. The limitation
  becomes a coverage gap, so the run says it is partial.
- A `collected` target's `checks_run` names a check the manifest's `checks_not_applicable` declares
  inapplicable there, or the document's `checks_not_applicable` names a check the manifest ran to
  `rc == 0` without itself declaring inapplicable. Applicability is corroborated, never prohibited:
  a check the collector never reached still takes the model's judgement.

### 3.2 Evidence — `adopt_collector_evidence`, `adopt_arm_impact`

For each finding whose derived id matches a candidate, the candidate's `excerpt` and command replace
the model's `evidence`, both fields together or neither (a candidate with an empty excerpt, or no
successful command behind it, changes nothing). The command is the candidate's own `command` when
it carries one, otherwise the `commands[]` record for that check. A finding with no matching
candidate keeps the model's evidence: that is the manual fallback, and it stays legal.

`remediate` renders the document as filed. It accepts `--manifest-file` only to refuse a held id as
held rather than as unknown (§3.3) and adopts no evidence from it, so a pull request it opens
carries the model's evidence where one `finish` promoted carries the collector's; a `/remediate`
answered on a later `finish` run is promoted from the adopted document.

A candidate with `impact_authoritative: true` also replaces the finding's `impact`. Multi-arm
checks are the only ones a collector should mark: there the sentence reports which arm fired, which
the model infers from an excerpt and gets wrong. Every other check's `impact` stays the model's,
whose rewrite is usually the better sentence.

### 3.3 Resolution — `still_flagged_ids`, `collector_held_entries`

A previous finding absent from this run's document is not announced as resolved while the collector
still emits a candidate with the same id. The set is built once per run, spelled the way the ledger
spells ids (`published_id`: derived, then clipped at `MAX_FINDING_ID`), because everything it is
compared against was read off a ledger body, and less every entry the document carries under
`declared` — the model's own, and the ones `finish` itself moves there from the repository
declarations the harness filed at `start`, which is why the join runs before the hold is computed:
the collector reads the fleet and not the repository, so it emits a declared posture for
as long as the declaration stands, and holding one would keep its ledger open and its pull request
unretired forever. A `resolved_because` entry releases nothing here — it claims the object is gone,
and the collector says it is not. On a findings run `finish` logs a warning naming the ids it held
back; `resolved` on the JSON line and the delta comment read the filtered set, and the stale-close
pass — on this branch and on the clean one — is handed the whole still-flagged set as present, so no
remediation pull request is retired over one, including a pull request on a finding the last body
never rendered. On a zero-finding run a still-flagged previous finding is unaccounted for the
purposes of the clean-close hold: the result is `HELD`, the ledger stays open, its pull requests
stay open, `resolved` is 0, and the held comment marks each such finding _still flagged by the
collector_ and says what releases it — the collector no longer emitting it, or a `declared` entry.
This is a hold, not a rejection: the model is allowed to reject a candidate as a false positive, so
a dropped candidate cannot be refused the way a dropped cluster is.

The ledger body is the persistence. This slice has no report store: `previous_ids` is read back out
of the body's hidden marker, and the body is rewritten from the document on every findings run. A
hold that only kept an id out of `resolved` therefore lasted one run — the next previous body no
longer named the finding, and a clean run closed the ledger over it with its pull request open. So
the held set for a run is defined on the marker: the previous body's marker ids ∩ `still_flagged_ids`,
less the ids the document carries. The identity of each held entry — check, cluster, namespace,
object, and the command that produced it — comes from the manifest candidate that still emits it,
which is what the collector vouches for; the title is the previous body's where its heading
survived and is otherwise built from the check and the object. That set drives the `HELD` merge on
a clean run, the exclusion from `resolved`, and the carried rows on a findings run.

On a findings run every held entry is carried forward into the rendered body under a _Held by the
collector_ heading — anchor, `####` heading with the marker, `Where:` line, composed by the same
helper a finding's heading is — and its id is appended to the hidden marker after the rendered
ones. The marker is what the next run keys on, under every tier; the heading is what gives it a
title. Only the detail lines (the check and the collector's command) are capped, at
`MAX_HELD_DETAIL_ROWS`. The section is measured _after_ the document's findings and before the
evidence appendix, so it can never displace a finding: full rows if they fit, identity lines alone
if not, and failing that a one-line note — never an oversized body. A held entry is not `new` (its
id was already in `previous_ids`), the sweep never sees it (it is not in the document), and it stays
until the collector stops emitting it or a `declared` entry releases it.

A `/remediate` naming a still-flagged id the document does not carry is deferred on the deferred
marker, like one naming a withheld posture, rather than refused as a typo; the test is the
still-flagged set itself, on both branches, independent of coverage gaps and of which tier the last
body rendered. The wording depends on whether the ledger carries the id — "see _Held by the
collector_" — or only the JSON line's `unpublished_candidates` names it. `remediate --finding`
given the same `--manifest-file` refuses it as held rather than as unknown.

Three bounds on the held set, all applied where it is computed, once per run, before the branches
split. "The document carries it" means the document's own ids plus the postures `finish` withheld
this run: a withheld posture is the model's finding taken out for want of a search, and withheld
ids enter no delta block, so it is never held. When the ledger body could not be read, the run has no held set to intersect with — and it does
not derive one from the manifest, because that would turn every candidate the model has been
rejecting into a permanent hold on one transient `gh` failure. The ledger is the persistence, and a
run that cannot read it must not overwrite it, whatever flags it passed: the body, its title, the
severity label, pull-request promotion and the acknowledgements that would follow it all wait for
a run that can read the ledger (the old marker and its held ids survive untouched); `/remediate`
refusals and deferrals are still answered when the run passed a manifest, whose still-flagged set
tells a held id from a typo — a run with neither the body nor a manifest cannot, so it answers no
`/remediate` at all (no refusal, deferral or acknowledgement; the next readable run answers them,
and the deferred marker is what `reply_to_deferrals` guards on, so nothing is lost by waiting); the
stale-close pass still protects the still-flagged set, the JSON line reads `UPDATED` and partial with a coverage gap saying the body was left as it
was, and a clean run does not close. These are the two deliberate changes to manifest-less
behaviour in this slice: main rewrites the body over an unreadable one — a degraded path that
already skips the delta comment — which would drop every held id the marker carried and retire
their pull requests, and every published body now spells a `<!--` arriving in model- or
fleet-authored free text as `&lt;!--` (§3.3), so a run over a document whose text contains a
comment opener renders that text differently from main; the recorded transcripts cover neither
path, and none of the five carries an opener in free text. A marker minted under
another identity scheme is deliberately not this case: the stamp is refreshed only by the rewrite,
so a scheme bump rewrites the body as it always has; the holds survive it by re-derivation from
their rows (below), and only ids with no row are lost. And the set is capped at `MAX_HELD_IDS` in
sorted id order — the ids are a monotone term in the marker that no SOP-side edit can shrink. An id
past the cap leaves the marker for good: the ledger stops tracking it, it stays on each run's JSON
line as an unpublished candidate while the collector flags it, its pull request stays open, and the
dropped ids are logged once with a warning and stated at the end of the section under every tier;
the ids kept are charged to the budget ahead of the findings, so the body cannot raise over them.
The fourth tier, when not even the note fits, is no section at all and the ids in the marker alone.

`--dry-run` previews the hold from the manifest's candidates alone. A clean preview says the run
would be `HELD` if the ledger's marker carries any of the still-flagged candidates and `CLEAN`
otherwise, and prints the comment a `HELD` run would post; a findings preview renders the held
section under a heading that says these are candidates the real run holds only if the marker
carries them, with the candidate's identity and no title.

A run that passes no manifest — no flag, or the waiver — cannot re-evaluate the holds a previous
manifest run left, so it carries exactly what the renderer recorded as held and nothing more.
Inside the held span every tier writes a renderer-owned id list, `<!-- audit-held-ids: [...] -->`
— full rows, identity rows, the note, and a fourth tier that is the two span comments and the
list with nothing visible, charged to the budget ahead of the findings so it always fits — and the
manifest-less held set is that list less the document's ids, the withheld postures and the
`declared` entries. Nothing is inferred from the marker or from which headings the body renders: a
model-written title may hold a newline that moves the finding marker to a line the heading regex
never matches, so "marker ids minus rendered headings" manufactured holds on streams that never
had a manifest. Each id renders with the identity its held row had where the previous body had one
(`parse_held_rows`, the same readers a finding's heading and `Where:` line have) and as an id-only
row otherwise — a heading with the marker and a line saying the location was not recorded, never a
`Where:` line built from the id's segments; the held list carries every one of them, so a hold
survives every tier. The run keeps them out of `resolved`, in the stale-close protection set, in
the marker and in the list, defers a `/remediate` on one, and holds the close on a clean run; the
held comment says the rows are held from a previous run's manifest that this run cannot release. A
hold is released only by a manifest run that no longer emits the id, a `declared` entry, or the
document carrying it. The manifest path keeps marker ∩ still-flagged: the collector is there to
vouch. A body main wrote has no held span, so a flagless run over it carries nothing, and the
byte-for-byte claim needs no premise about what its headings render. The run keeps them out of `resolved`, in the stale-close
protection set and in the marker, defers a `/remediate` on one, and holds the close on a clean
run; the held comment says the rows are held from a previous run's manifest that this run cannot
release. A hold is released only by a manifest run that no longer emits the id, a `declared`
entry, or the document carrying it. This does not touch the byte-for-byte claim for the
manifest-less run: a body main wrote lists in its marker exactly what its findings section
rendered, so the difference is empty and the run is what it was.

The rows a manifest-less run carries render under the same heading with their own wording: held
from a previous run's manifest, this run passed none and cannot release them, and a
_Last recorded_ line naming the collector's last recorded command where the previous row had one
(omitted where none was recorded). The collector wording — "the collector ran … there this run and
still flags this object" — is used only for rows this run's manifest vouches for. The note tier
carries the same three spellings in one paragraph, so a body squeezed out of its rows claims no
more than a roomy one did: a manifest-less carry does not say a collector emits anything this run,
and the dry run's note says it is previewing rather than holding. The readers
locate the held section by two comment markers the renderer owns (`<!-- audit-held:begin -->` /
`<!-- audit-held:end -->`), never by Markdown headings, so a model-written line beginning `## `, an
unbalanced fence, or the held heading's own text inside an excerpt changes nothing. A body main
wrote carries neither comment.

Unlike the `<!-- finding:id -->` markers, the brackets are not extended to the document on trust.
An injected `<!--` was survivable while a body was only ever parsed for the run that read it: at
worst it cost one run's read of one title. A forged held span is durable — the id list it carries
becomes a hold every later run carries forward, and a run without a manifest has no evidence to
release it with — so three rules stand between free text and the span. A bracket counts only on a
line that is exactly that bracket once stripped, the discipline the delta block and the id-scheme
stamp already keep. A body with two begins, two ends, or an end ahead of its begin carries
nothing at all, rather than the reader picking a span out of a shape this renderer never emits.
And `publishable_text`, the gate every piece of model- or fleet-authored text passes on its way
into a published body — titles and impacts, table cells, identity rows, evidence excerpts and
commands, and the coverage-gap sentences, which also leave by the run-summary JSON — replaces
`<!--` with `&lt;!--` after redacting. The first two rules alone would not close it: a multi-line
title can spell a whole begin/list/end trio on lines of its own. The escape is what holds; the
other two are the fallback for a body written before it or edited by hand since. Inside a span
that is read, every entry of the id list must itself be spelled like a finding id — the same
`FINDING_ID_RE` every published id already satisfies, clipped ids included, since `_shorten_id`
ends on a hex digest — because a carried id becomes a `/remediate` deferral, a stale-close
protection and a line in the next marker, and nothing downstream re-checks it. Entries that are
not ids are dropped and their count and spellings logged, the way the other residual warnings
report what a run stopped tracking. The cost is that
a `<!--` inside a fenced evidence block is shown to the reader as `&lt;!--`, because a fence does
not decode entities — free text quoted as raw output, where it is least likely to be prose
someone is reading closely.

An identity-scheme bump re-spells every id, so the previous marker's ids would match nothing. On a
previous body stamped with another scheme, every id the marker names that has a rendered row —
a finding's or a held row's — is re-derived from that row's `Where:` line and the check in its id,
and that re-derived set stands in for the raw marker on the manifest path and for the held list
on the manifest-less one. A bump that qualified a stream's cluster names leaves `Where:` lines
naming the bare cluster, so on the manifest path a row is also spelled with each name that could
qualify it — from the previous body's Scope table, or, for a cluster past its `MAX_SCOPE_ROWS`
rows, from this run's manifest clusters — and the spelling the collector flags is used when it does
not flag the bare one. A name two clusters share takes the one the collector flags, and the first by
id when it flags both: either keeps the ledger open over a finding the collector reports. Only held ids with no row — the note and fourth tiers write none — are
the residual: they leave the ledger unheld with the bump run's rewrite, and the run logs a warning
naming their count. That residual is the cost of a
bump, which is rare and operator-initiated.

### 3.4 The automatic sweep — `uncorroborated_findings`, `triage_marked_findings`

Sets that withhold auto-promotion and nothing else. An explicit `/remediate <id>` ignores both.

- **Uncorroborated:** a finding filed under a check the collector ran to `rc == 0` on a cluster it
  marks `collected`, for an object the collector did not flag. Only a `collected` target vouches —
  the line §3.1 draws — so a hand-collected finding on a `gate-failed` target is never called
  uncorroborated. The collector is exhaustive over a check it ran, so this is a verdict attributed
  to a collector that returned the opposite one. The finding is published,
  named in the ledger's _Awaiting `/remediate`_ section under a paragraph that says to read it
  first, and reported on the JSON line.
- **Needs triage:** a finding whose candidate carries a `needs_triage` value in `NO_SWEEP_TRIAGE`
  (today, `service-fronted`). The collector stands behind the finding; the _fix_ has a consequence
  it could not measure. Named in its own paragraph, worded differently from the one above because
  the two say opposite things about the collector.

Both are computed after every other test the sweep already applies, so they name only what the sweep
would otherwise have opened — in the ledger and on the JSON line alike, which carry the same list.

### 3.5 Disclosure — `unpublished_candidates`, `wholly_unpublished_checks`

Every candidate whose id no finding carries is listed on the JSON line, and every `(cluster, check)`
whose candidates were _all_ dropped is listed separately and logged as a warning. "Carries" counts
the postures `finish` itself withheld for want of a declared-intent search — the model published
those — and every entry under `declared`, which the collector emits for as long as the declaration
stands; neither is a dropped candidate, so a standing declaration does not make the run speak. The warning says the check is reported as having run only where that cluster's `checks_run`
says so. Neither forces anything: rejecting a candidate is the model's to do, and this makes the
rejection visible where it used to be indistinguishable from a check that ran and found nothing.
Either list being non-empty makes `silent_ok` false on both branches: the disclosure is a warning a
scheduled run is told to discard under `[SILENT]`, so the verdict has to carry it.

### 3.6 The JSON line

When `--manifest-file` was given, these keys join the exit contract the ledger design's §6 lists:

```json
"unpublished_candidates": [{"id": "…", "check": "…", "cluster": "…", "object": "…"}],
"wholly_unpublished_checks": [{"cluster": "…", "check": "…", "objects": ["…"]}],
"uncorroborated_findings": ["<finding id>", "…"]
```

`uncorroborated_findings` is the sweep's list from §3.4 — empty on a zero-finding run, since there is
no sweep. Every id on the line, `unpublished_candidates[].id` included, is the ledger spelling
(`published_id`: derived, then clipped at `MAX_FINDING_ID`).

They are absent, not empty, on a run without the flag, so the line a stream without a collector
prints is unchanged.

## 4. The waiver — `--no-collector-manifest <reason>`

For a run where the collector produced no manifest and every check came from the manual fallback.
The reason, passed through the same redactor as a skipped cluster's reason, is appended to
`coverage_gaps` as `the collector manifest was waived — <reason>`, which makes the run `partial`:
nothing is announced resolved, no remediation pull request is retired, and the ledger is not closed,
by the same rule any other gap applies. A document-authored gap shows in the Scope table's rows; the
waiver has no row, so the ledger body lists it under a _Coverage_ heading in the Scope section and
the delta comment, when one is posted, repeats it. The same list carries the other hold the
document cannot express: the ledger body this run could not read and left as it was (§3.3). The waiver and `--manifest-file` are mutually
exclusive, a blank reason is a validation error, and `--dry-run` appends the same gap so the preview
shows the hold the real run will apply.

Neither flag is required. Making the manifest mandatory per stream is the last step of each
collector's rollout, once its SOP passes the flag on every run.

## 5. Where the roster meets the manifest — `AuditSpec.scopes`

Coverage is measured per target, not per stream: `audit_target_checks(audit_id, name)` is the
roster subset a `scope.clusters` entry owes, chosen by the kind its name encodes (`project/<id>`, a
`<project>/<region>/<subnet>` path, or a bare cluster name). A stream declares the partition in
`AuditSpec.scopes`; an empty `scopes` measures every target against the whole roster, and a kind
the run enumerated none of is reported as a stream-wide gap naming the checks it stranded. No
shipped roster declares `scopes` yet; the field, the per-target denominator in `coverage_gaps` and
the scope table, and the test that the partition covers the roster exactly are in place for the
first SOP that does.

## 6. Testing

`agents/platform/skills/fleet-audit/scripts/test_audit_report.py` holds a class per rule above, and
`TestFinishWithoutAManifestIsUnchanged` holds `finish` without either flag to transcripts recorded
from the harness before this contract existed — every `gh` argv, every body, stdout and stderr, byte
for byte, under `testdata/finish_without_manifest/`: the findings path with a delta and a promoted
pull request, the clean close, the clean run held over an unaccounted finding, the clean run held
open by a coverage gap, and the dry run. A change that is meant to alter the manifest-less run re-records them
from the changed harness with `FLEET_AUDIT_RECORD_GOLDEN=1`, and the diff of the fixtures is the
review's record of what moved.
