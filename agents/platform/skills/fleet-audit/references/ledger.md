# fleet-audit — What the ledger says, and its size

Part of the `fleet-audit` skill (`/opt/data/profiles/platform/skills/fleet-audit/SKILL.md`), which says when to open this file.

## What the ledger says about each finding

Every finding renders in exactly one state, computed fresh each run from whether it still reproduces
and what pull request sits on its branch. The state itself is never stored; the report store keeps
the previous run's ids and titles, which is how a finding is known to be new or resolved.

A finding whose `evidence` and `severity` are the same as in the previous run publishes with the
previous run's `title` and `impact`, and usually its `recommendation` and `remediation` together. So
new words for an unchanged finding do not reach the ledger. Your `manifest` remediation and its
`recommendation` always stay, and so does a `gcloud` command where the previous run wrote `manual`.

| State                | Rendered as                           | Meaning                                    | What the harness does                                        |
| -------------------- | ------------------------------------- | ------------------------------------------ | ------------------------------------------------------------ |
| `open`               | `open`                                | Reproduces; no pull request                | Nothing, unless it qualifies for auto-promotion              |
| `pr-open`            | `fix proposed`                        | Reproduces; a fix is open on its branch    | **Labels re-asserted.** The pull request itself is untouched |
| `pr-merged-persists` | `⚠ fix merged, still reproduces`      | Reproduces; the fix **merged anyway**      | Comments once on the merged PR; never reopens it             |
| `refused`            | `fix refused`                         | Reproduces; a **human closed** the fix     | Nothing. The close stands until someone says `/remediate`    |
| `withdrawn`          | `fix withdrawn, awaiting re-proposal` | Reproduces; the **harness closed** the fix | Treats it as having no pull request — it is promotable again |

An `open` finding can also sit on a branch whose merged pull request, or one a person closed, does
not name it. Its state line then names that pull request, which blocks the automatic promotion.

Every row above says "reproduces", and that is not an accident: **a finding that stopped reproducing
is not in the document at all**, so it has no row in the ledger to carry a state. Two further states
exist in the code — `resolved` and `resolved-merged` — but neither is ever rendered here. A
resolution is announced in the delta comment, by id and title recovered from the previous run's stored report, and
the finding's open pull request is closed as stale. The compliance stream has a third close: a pull
request whose remaining findings are all shielded (SOP 2.7, a declared workload sharing the `default`
ServiceAccount) is closed with that reason, on a partial run too when a note `start` filed covers the
namespace, since the reason is the declaration and not this run's reading, and those findings stay
on the ledger, not proposed again while
the declaration stands, rather than becoming promotable. A resolution whose fix had already **merged** is
the ordinary, expected ending, so nothing extra is closed and nothing extra is said.

Three of the five are easy to misread:

- **`pr-open` is not refreshed.** An open pull request's diff is left exactly as it is, because a
  reviewer may have pushed onto it and a nightly force-push would silently discard their work. The
  ledger links it; the diff is whatever a human last made it. Its **labels** are the single
  exception, re-asserted on every run that finds it still open: they are the harness's own index of
  what it still owns rather than anything a reviewer authored, and a stripped `agent:audit` or a
  `severity:` frozen at what the group used to be loses the pull request from the views triage works
  from. Labels only — no push, no rewritten body, so the promise above still holds.
- **`refused` is a human decision, not a rejected command.** It means someone closed the remediation
  pull request without merging it. That is a considered "no", and the harness never overrules it by
  re-opening the same fix tomorrow morning.
- **`withdrawn` is the other half of that.** A closed unmerged pull request is two different events,
  and the discriminator is the `audit:stale-closed` label the harness applies when it closes one as
  stale. Its finding is promotable again on the usual terms; a `refused` one is not. Do not strip
  that label — without it the close reads as a human rejection and the finding is never re-proposed.

The escape hatch for a `refused` finding is `/remediate <id>` from someone with write access, and it
must be written **after** the close. An older command in the thread is reported as _superseded_
rather than honoured: comments are never edited away, so a March request would otherwise re-open an
April close every morning forever. Post a fresh one.

`pr-merged-persists` is the state worth reading twice: a fix merged and the deviation is still
there. Either the remediation was incomplete or something outside this repository reverted it.

Below the findings, a ledger whose document carried a `declared` list renders a **Declared intent**
table: one row per entry, with the check, the cluster, the object, `repo:path`, and the title and
excerpt. Rows have no state and no id — nothing in that table is tracked between runs — and the
table is capped at 50 rows and says how many it left out. On a clean run the ledger closes without
being rewritten, so the all-clear comment lists the declared postures instead, with the same
`repo:path` pointers — and that comment is the last ledger record: a later clean run with nothing
but declarations opens no ledger and posts nothing, because the ledger tracks findings and a
standing declaration is the same every morning. From then on the declaration in the repository is
the record, and `finish` reports the count as `declared`.

## Size

GitHub caps an issue body at 65,536 characters, and a pull request body at the same. The harness
targets 60,000 and will truncate the ledger's findings section to stay under it. Three consequences
you must not work around:

- **Findings are rendered severity-first**, so truncation only ever eats the least-severe end.
  Criticals are structurally safe. In a band, the cut takes one finding per target in turn: no
  target gets a second row before every target in the band has one. A band with fewer slots than
  targets still drops the targets late in the alphabet.
- **The title's counts stay true.** If the body omits findings it says so explicitly. Never
  hand-trim your document to make it fit — the counts are how a reader learns the real total.
- **Truncation does not make a run `partial`** — see [Partial coverage](finish.md#partial-coverage), which
  owns that rule.

Every free-text field is clipped on the way out — title 300 characters, impact and each
`recommendation` sub-field 1,500, `remediation.note` 2,000, `evidence.command` 2,000,
`evidence.excerpt` **40 lines and** 2,000 characters, whichever it hits first, and
`cluster` / `namespace` / `object` 320. That last group is not a style rule. The renderer always
emits the **first** finding whatever it costs, so before those three were clipped one oversized
identifier on one finding could overflow the whole body and publish nothing at all, every morning,
until that finding stopped reproducing.

Resolution accounting is unaffected by truncation, because the two halves of the delta are measured
against different sets: **new** is judged against what the body rendered, and **resolved** against
every finding in the document, rendered or not. A finding cut for space still reproduces and is
never reported as fixed.

The **Declared intent** table is measured with the fixed sections, before the findings claim what
is left, so on a body near the limit it is the findings that yield — and it is row-capped, so what
it can cost is bounded.

The ledger's last section, **How this run checked the fleet**, is a collapsed table of every
`checks_run` entry — one row per check and command, naming every cluster it ran on, so a shared
project-wide command is one row. Not-applicable checks collapse the same way, one row per check and
reason. It is rendered last, against whatever budget the
findings left, and is dropped whole rather than half if it does not fit: a partial evidence table
reads as a short one, and "this run ran three checks" is a worse lie than saying nothing. A dropped
table leaves a notice pointing at the run's stored report, where `fleet-audit-reports` reads every
command back. You do not write this section; you supply the commands and the harness publishes them.
