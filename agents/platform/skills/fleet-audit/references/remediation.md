# fleet-audit — Remediation pull requests

Part of the `fleet-audit` skill (`/opt/data/profiles/platform/skills/fleet-audit/SKILL.md`), which says when to open this file.

## Remediation pull requests

A pull request is opened for a finding only when its remediation is a `manifest` — there is nothing
to put in a diff otherwise. Three paths lead there:

- **Auto-promotion.** A finding graded `critical` — or `major` on one of the checks in
  `MAJOR_SWEEP_CHECKS` (`no-pdb`, `unbound-sa-automount`, `ccc-no-ondemand-floor`) where the
  collector's candidate is at least `major` — that is a `manifest`, has no live pull request on its branch — and, on a run that passed
  `--manifest-file`, is neither uncorroborated nor triage-marked by the collector — is promoted
  automatically by `finish` — **at most five per run** — unless the install's
  `KUBEAGENTS_AUDIT_AUTO_PROMOTE` switch is `false`, when `finish` opens only what `/remediate` named. Every other `major` finding is held to the `critical` floor:
  a SOP's grade says how bad the finding is, not whether its fix is safe to open unasked. The collector marks a fix that scales a controller to zero, writes a
  default-deny NetworkPolicy, resizes a `Guaranteed` pod, needs a new ComputeClass, owes its
  `major` to the Autopilot bump, turns off a namespace's default ServiceAccount token, rewrites
  or deletes a Service, or enforces spreading; the ledger names each with its marker and reason.
  The surplus is named in the ledger as awaiting `/remediate`, and so is every manifest finding
  the floor passed over, as below the floor, so nothing is silently dropped. "Live" excludes a pull
  request the harness itself closed as stale (that one is re-openable) and includes one a human
  closed or merged (those are not).
- **`/remediate <finding-id>`**, or `/remediate all`, commented on the ledger by someone with write
  access to the repository. This path is uncapped: a human asked for that one by name.
- **A direct ask.** A collaborator asking the agent, in the agent's own task, to fix a named
  finding; the agent answers with the `remediate` subcommand. Uncapped for the same reason as
  `/remediate` — and distinct from a comment read on the ledger, which is the harness's to answer
  (`start` reports the ones that passed its gates as `pending_remediation_requests`). Fresh pull
  requests only: a finding whose pull request a human closed is reported as `superseded`, never
  re-proposed — a direct ask carries no GitHub identity, so the after-the-close escape hatch
  ([ledger.md](ledger.md)) stays with the write-gated comment.

Every `/remediate` gets exactly one answer, and the answer is never silence:

- Accepted — one acknowledgement comment on the ledger naming each target and **what happened to
  it**, never a count: the pull request URL, or "already open" with its labels re-asserted and its
  diff untouched, or _superseded_ by a human close written after the request, or that publishing
  failed and the next run will retry. "3 requests processed" is indistinguishable from "3 requests
  silently dropped".
- Refused — one reply saying why, for a commenter without write access, a `/remediate` naming a
  finding that is not in the current document (unless it is held by the collector — deferred,
  below), one naming a non-`manifest` finding, or one naming a posture a repository declaration
  covers, the model's entry or the harness's: the reply names the declaring `repo:path`, on the
  findings branch and the clean branch alike, since neither "typo" nor "no longer reproduces" is
  true of it. Removing the declaration brings the finding back, and a new request then opens it.
- **Deferred**, when the target is a posture this run withheld for want of a declared-intent search
  ([`declared_intent_searched`](findings-document.md#declared_intent_searched)), or an id a collector manifest holds
  that the document does not carry — this run's manifest, or a previous run's when this run passed
  none and the ledger still carries the row. Two wordings there: an id the ledger's hidden block carries is held until the collector
  stops emitting it or a `declared` entry covers it, the model's or the harness's; an id the ledger
  never carried is named only on
  the run's JSON line as an unpublished candidate. Either way there is no finding to open a pull
  request from — on the findings branch and on the clean branch alike, since "no longer reproduces"
  would be false there. A run whose report store has no trusted record of the open ledger and that passed no manifest cannot
  know which ids are held, so it answers no `/remediate` at all; the next run with a trusted record answers
  them. One reply says the request is on hold and why, under its own
  `audit-deferred` marker, which nothing reads as an answer: the same comment is acted on, and
  acknowledged, by the first run that records the search and still sees the posture, or whose
  document carries the held finding again. The refused marker is never written for it, so the
  requester is not told their id was a typo and does not have to ask again.
- Refused **on syntax**, likewise once, because a command the parser will not honour is a person
  waiting for a fix that is never coming. `/remediate` is only read at the start of its own line outside
  block quotes, so one written mid-sentence or rendered inside a block quote / lazy continuation gets a reply
  pointing that out; one written with no target at all gets a reply too, because reading it as `all` would open
  every promotable pull request the cap allows on someone who typed the command and then went to look up the id.
  These replies carry the correct syntax and the promotable ids — up to ten, then "and N more", since a refusal
  is help and not a second copy of the report.
- Overtaken by a **clean run** — answered anyway, and answered _before_ the ledger closes. A run that
  finds nothing still replies to every unanswered `/remediate` in the thread to say the finding no
  longer reproduces, and whether the ledger is closing or staying open on partial coverage. This is
  the one morning the issue disappears, taking with it the thread the requester would have re-asked
  on, so it is the one morning silence is least affordable. Authorization is not consulted here:
  nothing is being acted on for anybody, and "it no longer reproduces" is equally true and equally
  useful to a commenter without write access.

Two deliberate silences. A mid-sentence or quoted `/remediate` from someone _without_ write access gets
nothing: their correctly-typed command would have been refused anyway, and two replies to one
comment that was probably never a command is a bot picking an argument. And a `/remediate` inside a
code span is prose about the command, not an attempt at it — which is why every `/remediate` the
harness itself writes into a comment is backticked. Keep it that way when you quote one: an
unbackticked command in a harness-authored comment is read back on the next run, and a bot that
answers itself never stops.

All of these are guarded by a hidden marker carrying the triggering comment's node id, so a standing
`/remediate` in the thread is answered once rather than every morning forever.

A `/remediate` read on the ledger is never run through the subcommand — `finish` answers those on
the comment's own timestamp, which is what lets a fresh post-close command revive a finding the
subcommand would report as `superseded`. Run a direct ask's targets through it, `--finding` once
per id. Without `--repo` it acts on the repository with `role: gitops`. If the finding is from a run
that a person ran with `--repo`, pass the same `--repo` here. With several managed repositories,
none with `role: gitops`, and no `--repo`, it refuses (`FATAL: Multiple repositories configured …`,
exit 1); report that message to the user, and do not choose a repository:

```bash
/opt/data/profiles/platform/skills/fleet-audit/scripts/audit_report.py remediate --audit <audit-id> \
  --findings-file <findings_path> --finding <id> [--finding <id> …] [--issue <n>] \
  [--repo "<owner>/<repo>"] [--manifest-file <path>]
```

**It opens exactly what you name, and nothing else.** The auto-promotion sweep does not ride along:
one `--finding` produces one pull request (or one, shared, for the group that path belongs to), never
five more for findings the requester never mentioned and cannot tell apart from the one they
did. Auto-promotion happens in `finish`, where the whole fleet is being reported on anyway.

It prints one JSON line — `status`, `prs_opened`, `already_open`, `superseded`, and `refused`:

- `{"status":"REMEDIATED","prs_opened":["…"],"already_open":["cluster-old"],"superseded":[],"refused":["ns-quota"]}`

`superseded` names targets whose pull request a human closed: that close stands, and the
subcommand does not re-propose them. Revival belongs to the write-gated `/remediate` comment
(§ above, honoured by `finish` on the comment's own timestamp) — or to `--override-human-close`,
which exists for the person at the terminal who could have written that comment themselves. An
agent relaying an ask it cannot tie to a GitHub identity never passes that flag.

`refused` names the targets whose remediation is not a readable file inside the workspace — either the
audit promised a manifest and never wrote it, or the path does not resolve inside the repository at
all. Both leave nothing to put in a diff; a `SECURITY:` line in the log says which one happened. A
target is also refused when its fix adds a new file that its kustomization does not list, or the
audit could not check that. Its `REFUSED` line names the `resources:` line to add when the audit saw
the kustomization, and says what it could not check otherwise. The
other targets still open — `/remediate all` expands to every **manifest-remediation** id in the
document, and failing the batch over one unwritten file would answer a request for many fixes with
none. Say which were refused when you acknowledge the command.

Exit 2 means nothing was published, unless the line is `BROKER UNAVAILABLE`: then a pull request
opened before the broker went stands, and re-running the same `remediate` once it answers adopts it
and opens the rest. Otherwise read the message before reporting why: a named id is not in
the document at all; a named id is held by the collector (given the same `--manifest-file` `finish`
had, the message says so instead of "not in the document"); a named target is not a `manifest`; or
_every_ named target was refused because its file is not readable inside the workspace. The first
three are fixed by dropping the bad id and asking again; only the last is about writing manifests.

**Findings whose remediation paths intersect share one pull request.** They have to: separate
branches touching the same file conflict on merge. Promoting any member promotes the whole group —
the pull request names every member. That is why several findings can point at one shared manifest
and still produce one clean diff.

The group's branch is `platform-agent/fix-<audit-id>-<slug>-<digest>`, where the digest is over the
group's **sorted path set** and the slug is a readable fragment of the first path. It is keyed on
the files, not on a finding id, and that is load-bearing: ids are regenerated every run, so a branch
named after one of them gets renamed the day that finding resolves — orphaning the open pull request
and opening a duplicate against the same file.

The branch finds the pull request. For a merged or closed pull request, its delta block limits
which findings it claims (when the body has no delta block under the current `ID_SCHEME`, the pull
request claims its whole group). There is no state file: `finish` reconstructs the entire
finding-to-pull-request mapping from one listing of the stream's pull requests. An open pull request
claims its whole group. A finding that the delta block of a merged pull request, or of one a person
closed, does not name shows on the ledger with no pull request, and its state line links that pull
request: it still blocks the automatic promotion, and a `/remediate` proposes a fix for every finding
in the group that still reproduces. A pull request the harness closed as stale blocks nothing. In
the finding's detail block, a `manifest` remediation path links to the file at the audited repository
commit when the forge's `capabilities` response supplies a `fileUrl` template and the file exists at
that commit, and stays a bare path for a newly written file or when `fileUrl` is absent.

## Kustomize roots

Kustomize renders only the files its kustomization names, and a remediation carries one
file, so it cannot add the `resources:` entry too. Make such a finding `manual`, and put the new
file's content and the `resources:` line it needs in `remediation.note`. When `finish` refuses a
declared `major` 3.3 fix of this kind as unwritten, keep it `manual` that way and pass
`--decline-fix <id> "<why>"` with a reason that names the kustomization; otherwise the refusal
repeats. Where the file is not in the repository and the nearest kustomization above it (in its
directory or a parent) does not name it, `finish` degrades the finding to `manual`. The note names
the kustomization and the `resources:` line to add, and holds your file as a YAML block, capped in
lines and in characters. It also degrades the finding, and says so, when git cannot answer, the broker
refuses a question, or the kustomization cannot be read. A broker outage (no answer, a server
error, or too many requests) stops `finish` with `BROKER UNAVAILABLE`, as at publish. It does not refuse the run for these, and `remediate`
refuses them by name. `finish --dry-run` only warns, only in directory mode, and reads the
kustomization in the working tree, where the real run reads it at the audited commit.
