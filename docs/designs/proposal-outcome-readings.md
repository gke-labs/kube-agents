# Proposal Outcomes and Human Touches as Readings from the Forge

**Status:** implemented 2026-10-29. Epic: gke-labs/kube-agents#4120, Workstreams 1
and 2. Owner of the producer side: [`usage-counters-producer.md`](usage-counters-producer.md), which
this document amends; the rows there for `remediationsMergedTotal` and
`remediationsClosedUnmergedTotal` point here. [`human-interaction-readings.md`](human-interaction-readings.md) (proposed) generalises the readings slot of section 6 to a target per reading.

## 1. Intent

A person at a high level wants three numbers for an install: how many pull requests the agent
opened, how many of them were merged, and how many were closed without a merge. From the first two
they read "how many of its PRs were good enough to merge". For each merged PR they also want to
know how much human work it took: reviews, comments, commits a person added. Latency of minutes
to an hour is fine. Missing a merge is not: a ratio that under-counts merges makes the agent look
worse than it is, and that error compounds over a quarter.

Before this design, the merged and closed counters were event counts the credential broker kept in
memory. A merge was counted only when one of the broker's own reads, a `proposal-list` or
`proposal-view`, happened to show it, only once per broker process, and only when it happened after
the process started. On 2026-10-28 on the gkedemos3 install both series stayed at zero after a merge
and a close until a person issued a `proposal view` by hand (#4120, first comment). A watch list
would have fixed that with a store, and the broker keeps no store by design: PR #2919 rejected a
persisted seen-set because "the broker keeps no state across requests by design", and the
`VcsBroker` class docstring then named the in-memory seen-sets and identity cache as the one
cross-request memory, on which no answer depended.

The decision this document records: stop counting events and read the state. The forge already
holds every proposal the install opened, authored by the install's own login, in a repository the
install manages. The broker asks the forge, a few minutes apart, how many of those are open,
merged and closed, and how many human events each carries, and publishes the answers as readings.
Nothing is remembered between readings, so nothing is missed, and a restart changes nothing. The
less state kept in the system the better; a reading is the least.

## 2. Goals, non-goals, anti-goals

Goals:

- Every merge and every close of a proposal the install opened reaches `status.usage` without a
  person asking the agent about the proposal, within two operator poll intervals plus the forge's
  own lag: the reading is taken on the broker's timer, and the next poll reports it. Two bounds: the
  close count returns after a broker restart once the detail cache is warm, and in a repository
  past a thousand own proposals the detail series cover the thousand most recently updated.
- Every human event on those proposals is counted and labelled by kind in Prometheus, so a business
  graph can choose which kinds count later.
- No new state in the install beyond a cache the next reading rebuilds; no new port, no new RBAC,
  no write to any ConfigMap from the broker.
- The `status.usage` field names stay. Alerting rules and the feedback summary keep working.

Non-goals:

- Per-proposal series in Prometheus (a PR number as a label). The per-proposal shape is a
  bucketed distribution, not a series per PR.
- Reactions (👍 on a comment). They need one call per comment and are left for a later reading.
- Counting proposals opened by anyone other than the install's own login.
- Moving the agent's own verbs off the write token. Every API read a verb makes on a managed
  GitHub repository rides the write token today and keeps doing so; only the sweep gets a
  read-only token of its own (section 5).

Anti-goals, what would make this a failure even if it works:

- A number that silently goes stale: a forge outage must show as an absent field, not a frozen one.
- A reading that is wrong because one repository of several failed to answer and the sum was
  published anyway.
- Unbounded forge traffic as the install's proposal history grows.

## 3. Constraints the design answers

1. **The broker makes no scheduled forge read and keeps no store.** Issue #2728 preferred counting
   where the broker already learns of merges "because it adds no scheduled forge traffic"; PR #2919
   rejected a persisted seen-set because "the broker keeps no state across requests by design".
   This design keeps the second rule and gives up the first on purpose: the requirement that
   every merge be counted cannot be met by reads the agent happens to make, so the broker takes
   on one scheduled forge read, a daemon timer that runs the sweep every `VCS_READINGS_TTL_SECONDS`
   while the install has a managed repository, bounded to one attempt per interval whatever the
   outcome. A timer rather than a scrape-triggered read, which an earlier draft of this document
   chose: a read every few minutes on a stock install is scheduled traffic whichever event starts
   it, the scrape trigger made freshness depend on which scrapers an install happens to run, and
   it changed the metrics listener's security property, that it "holds no route, credential or
   policy of its own", in three security documents and the docstring, an exception that would
   cost a second security edit to undo. The timer leaves the listener as it is, serves the same
   series on the same cadence, and its cost bound is the TTL's alone.
2. **A reading can go down.** A repository removed from `managed_repos`, or a proposal deleted,
   lowers the count. The operator's counter fold treats a fall as a restart or a foreign body and
   never lets a total fall (`usage_counters_fold.go`, `usageBranch`). Readings therefore bypass the
   fold, as `clustersRegistered` and `clustersMonitored` do, and are projected as the latest poll's
   reading.
3. **Forge cost.** GitHub's search index answers a count in one call per terminal state and one for every state together; GitLab and Gitea
   answer with a total header on a one-item page. Per-proposal detail costs about seven calls per
   proposal and must not be paid for every proposal on every sweep.
4. **Identity.** The install's login is the credential's `whoami`, cached per repository for the
   life of the process as the removed `_merged_viewer_for` did, but the sweep does not ask through
   that path: `Binding.whoami()` runs the forge credential's `ensure()` first, which on github.com
   refreshes the write credential and on GitHub Enterprise Server mints a write token when the
   cached one is old, so the first sweep after a restart would mint the write token the sweep
   exists to leave alone. The sweep resolves its login with its own read token over the HTTP
   transport's GraphQL `viewer { login }` query, the one the Enterprise construction already sets
   as `whoami_graphql`, which the github.com construction of section 5 sets too; GitLab and Gitea
   answer `whoami` with their static token as every verb does. On gkedemos3 on 2026-10-28 the
   `identity` verb answered `kage-token-minter[bot]`, the GitHub App form, which `_login_key`
   strips for comparisons. A
   credential that cannot name itself produces no reading for its repositories and one INFO line
   per process, as today. Such a repository is not attempted again in that process, counts
   `reason="identity"` once, and leaves the set of repositories a reading requires, so one
   nameless credential silences its own repositories and not the install.

## 4. What the broker publishes

All on the existing metrics-only listener, port 8766, rendered by `ProxyMetrics` beside the
families it already serves. No login, repository name, URL or proposal number appears in any
label.

| Series                                         | Type    | Labels                                                                                               | Meaning                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| ---------------------------------------------- | ------- | ---------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `kubeagents_vcs_proposals`                     | gauge   | `state` = `open` \| `merged` \| `closed`                                                             | Proposals authored by the install's login in its managed repositories, by state, as the forge reports now. `closed` is closed without a merge.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| `kubeagents_vcs_proposals_closed_by`           | gauge   | `actor` = `install` \| `human` \| `bot`                                                              | The `closed` proposals split by who closed them. A close the install made itself (the fleet audit tidying a stale proposal) is `install`.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| `kubeagents_vcs_proposal_touches`              | gauge   | `kind`, `actor` = `human` \| `bot`                                                                   | Events on the install's proposals by anyone other than the install's own login, summed over every proposal.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| `kubeagents_vcs_proposal_touches_per_proposal` | gauge   | `outcome` = `merged` \| `closed`, `le`                                                               | How many human touches each terminal proposal carried, rebuilt from scratch each sweep: one gauge family with a cumulative `le` label over the edges `0, 1, 2, 3, 5, 8, 13, 21, +Inf`, the `+Inf` sample being the count of such proposals. A plain gauge with its own name and no `_bucket` or `_count` suffix, because the text format the broker renders attaches `histogram` samples by suffix and a gauge family would not own them; `histogram_quantile()` needs only `le`. A proposal with zero touches is in the first bucket, so "merged without any human touch" is `le="0"` on `outcome="merged"`.                                                                                                                                                                                                                                                             |
| `kubeagents_vcs_proposals_ignored`             | gauge   | none                                                                                                 | Open proposals of the install older than `VCS_READINGS_IGNORED_AFTER_SECONDS` (fourteen days) that carry no human touch of any kind: proposals nobody is looking at. Part of the details reading.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| `kubeagents_vcs_readings_age_seconds`          | gauge   | `reading` = `outcomes` \| `details`                                                                  | Seconds since that reading was last taken in full. Two readings: `outcomes` is step 1 (`kubeagents_vcs_proposals`), `details` is step 2 (`closed_by`, the touch series, the per-proposal gauge). Each is absent until its first reading and never omitted after it, however old.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| `kubeagents_vcs_readings_failures_total`       | counter | `reason` = `forge` \| `identity` \| `truncated` \| `detail_truncated` \| `rate_limited` \| `warming` | Failures recorded while a sweep reads a repository, by cause; a repository records each cause it produced, so one past the walk bound that is still warming records `truncated` and `warming`, and a failed tally beside them. `warming` is a partial reading (state counts, details not yet taken); `truncated` is a partial reading too (state counts from the tally, details from the thousand most recently updated proposals and the cache); `detail_truncated` is counted per proposal whose conversation passed its bound, and the details reading is still published from what was read; `identity` leaves that repository out, once per process; `forge` and `rate_limited` withhold the reading whose step failed: both readings when the listing failed, the outcomes reading alone when the tally failed. Two repositories rate-limited in one sweep are two. |

`kind` is a closed vocabulary:

| `kind`                     | What it is                                                                                                                                                                          | Source on GitHub                        |
| -------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------- |
| `review_approved`          | A review with state approved, body or not                                                                                                                                           | `GET /pulls/{n}/reviews`                |
| `review_changes_requested` | A review asking for changes                                                                                                                                                         | same                                    |
| `review_commented`         | A review submitted with the comment state                                                                                                                                           | same                                    |
| `review_comment`           | An inline comment on the diff                                                                                                                                                       | `GET /pulls/{n}/comments`               |
| `comment`                  | A comment on the conversation                                                                                                                                                       | `GET /issues/{n}/comments`              |
| `commit`                   | A commit on the source branch whose author is not the install                                                                                                                       | `GET /pulls/{n}/commits`                |
| `agent_address`            | A conversation comment that begins with `/agent`, the form the `pr_comments` sweep accepts as a person steering the agent on its own proposal; counted here and not under `comment` | `GET /issues/{n}/comments`, body prefix |

`actor` is `human` unless the forge marks the account as automation (GitHub `type == "Bot"` or
the `[bot]` suffix, GitLab's `bot` flag or project-bot login, Gitea's negative user id), then `bot`.
The comment readers already carry that as a `bot` flag beside `author`; the commit reader, the
proposal listing and the closer lookup do not, and the design adds it there. The install's own
events are not touches and appear under neither.

A commit needs its own rule, because the install's commits carry no login. The forge links a
commit to an account only through its author email, and the install's git identity is an address
the forge cannot link by design: the broker stamps proxied commits `platform-agent@kube-agents.invalid`
(`CREDENTIAL_PROXY_GIT_AUTHOR_EMAIL`), the workspace path `platform-agent@users.noreply.<host>`, and
the sandbox client `kube-agents@users.noreply.invalid` (`KUBE_AGENTS_VCS_AUTHOR_EMAIL`), so GitHub
returns no author account for them, the commit translator falls back to the git author name, and
a login comparison would count every one of the install's own commits as a human touch and make
"merged without any human touch" structurally zero. So a commit is the install's when its linked
login is the install's, or when its git author email is one of the install's addresses: the three
defaults above, whatever the two variables are set to, and the App's own noreply form
`<id>+<slug>[bot]@users.noreply.github.com`. The commit readers on all three forges return the
raw author email and the linked login beside the translated author; GitLab's returns a name and
an email and no login at all, so there the email rule is the only one. A commit by anyone else
is `human`, or `bot` when the linked account is automation.

Bodiless reviews count. Today's `proposal-view` drops a review summary with an empty body
(`github/forge.py`, the conversation reader), which hides every plain approval; the reading reads
reviews itself and does not use that path.

The event counter `kubeagents_vcs_requests_total{verb="proposal-create"}` and
`status.usage.remediationsProposedTotal` stay as they are: the install's own record of proposals it
opened, exact by construction. The merge ratio for a business graph is computed from one source,
`kubeagents_vcs_proposals`: `state="merged"` over the sum of its three states, which is every
proposal the install authored that the forge still holds, the install's own closes included. In
`status.usage` that sum is its own field, `proposalsAuthored`, so a fourteen-day window needs no
expression over several fields. The ratio is never merged-reading over created-counter, because
the two can legitimately diverge (a repository removed from the managed list takes its proposals
out of the reading and not out of the counter).

`kubeagents_vcs_proposals_merged_total` and `kubeagents_vcs_proposals_closed_unmerged_total` are
removed, with `_count_outcomes`, the seen-sets, `started_at`, the observers and their tests. The
`proposal_close` own-close marking goes too; who closed a proposal is read from the forge.

## 5. How a reading is taken

**Trigger.** A daemon thread the broker starts beside its listeners runs the sweep every
`VCS_READINGS_TTL_SECONDS` (240) while the install has at least one managed repository, as the
Slack relay's bootstrap thread runs today. One at a time: an attempt still running when the
interval elapses is not joined by a second, and a failed attempt sets the same clock as a
successful one, so a forge outage or a rate limit costs one attempt per interval. `MetricsHandler`
serves `GET /metrics` from the registry as today and nothing about a scrape reaches the sweep.
A reading is therefore at most one interval old when the operator's five-minute poll reads it,
so a merge reaches `status.usage` within two poll intervals end to end.

**Per managed repository**, with the repository's `whoami` login as `author`:

1. The install's own proposals, listed by author through a new provider method
   `proposal_list_by_author(api, repo, payload)` that returns, per proposal, `number`, `state`
   (`open`, `merged`, `closed`), `author` with its account kind, `updated` and `closed`, plus the
   forge's total for the query.
   The listing is author-filtered at the forge, never filtered on the broker's side out of a
   listing of everyone's proposals: the usual GitOps repository holds far more proposals by people
   than by the install, and a bound spent on theirs would truncate every sweep.
   - GitHub: `GET /search/issues?q=repo:{r} is:pr author:{a}&sort=updated&order=desc&per_page=100&page={n}`,
     newest activity first so that a bounded walk holds the proposals step 2 needs and the same
     set from sweep to sweep (the other two forges are asked for the same order), where `{a}` is
     `app/{slug}` for an App installation token, which the broker knows from the credential's kind
     (a minted or brokered credential is one; a configured static identity is not), and the bare
     login otherwise; the `[bot]` marker on the login is the fallback signal, not the primary one,
     since the REST and GraphQL APIs disagree on whether an App login carries it. Each item
     carries `state`, `updated_at`, `closed_at` and `pull_request.merged_at`, which is how merged
     is told from closed. `total_count` is the forge's own count for the author. Search lags a
     write by seconds, which the latency budget absorbs; GitHub Enterprise Server serves the same
     route on the instance.
   - GitLab: `GET /projects/{id}/merge_requests?author_id={id}&state=all&per_page=100&page={n}`;
     the author id from `GET /user` once per process; the total from the `X-Total` header.
   - Gitea: `GET /repos/{r}/pulls?state=all&poster={id}&limit=50&page={n}`; the total from
     `X-Total-Count`; merged is told from closed by the item's `merged` flag.
   - **Every item's author is compared to the install's login.** A forge that ignores the author
     parameter (a Gitea release from before `poster`, say) returns everyone's proposals and the
     walk would truncate on theirs; the first foreign author ends the sweep for that repository
     with `reason="forge"` and nothing published, rather than a wrong count.
   - **A walk that ends inside its bound holds the forge's total.** The walk goes on while the
     total says more remain, whatever a page's `truncated` says, since a clamped Gitea page can
     look like the last. A walk that still ends short of the total (a proposal that updated
     mid-walk and moved to a page already read, or a GitHub page marked `incomplete_results`) is
     walked once more; short again, the repository's outcomes reading is withheld with
     `reason="forge"`. A page that adds no new proposal (a server that ignores `page`) ends the walk short at
     once, and a clamped walk also stops at a page ceiling read from its smallest page.
   - The walk is bounded by `VCS_READINGS_MAX_PAGES` (10), which on GitHub is also the search
     API's own ceiling of a thousand results, so it cannot be raised there. A repository whose
     own proposals exceed the bound is `truncated`, and truncation is a property of the
     repository, so it recurs every sweep and must not silence anything for good. Its state counts
     are read with one query per terminal state and one for every state together, open being the
     remainder, since separate queries are not one snapshot (`is:merged`,
     `is:unmerged is:closed` and the unqualified query on GitHub; `state=merged|closed|all` on
     GitLab), queries a complete walk does not need and does not make, so the outcomes reading is
     whole. Gitea's `state` is open or closed and
     merged is a flag on the item, so a header total cannot split merged from closed, and Gitea
     has no search ceiling either: its walk bound is therefore stated and enforced in proposals,
     not pages, `VCS_READINGS_MAX_PROPOSALS_GITEA` (1000): the walk continues while fewer than
     that have been read and the last batch was not empty, since Gitea clamps `limit` to the
     server's `MAX_RESPONSE_ITEMS` and a page can be shorter than asked, as the provider's own
     pager records. A Gitea repository past that bound has not completed step 1, so the
     install's outcomes reading is withheld with `reason="truncated"`, as the publishing rule
     below says for any repository that fails a step. A Gitea install with more than a thousand
     own proposals in one managed repository gets no outcomes reading, and this document says so
     rather than publishing a sum one repository is missing from. For the details reading a truncated repository contributes the
     thousand most recently updated proposals, which the `updated` sort puts in the walk, and
     the cache keeps the records of proposals that fell off the end (below), so a terminal
     proposal's touches and closer stay counted for the life of the process; after a restart the
     older ones are unknown, an under-count that begins only past a thousand own proposals per
     repository. `kubeagents_vcs_readings_failures_total{reason="truncated"}` moves every sweep
     the repository is past the bound, which is the signal. A forge that raises
     `ForgeUnsupported` for the listing publishes nothing and counts `reason="forge"`; one that
     raises it for the tally withholds the outcomes reading alone.
2. For each proposal whose `(number, updated)` is not in the detail cache: reviews, review
   comments, conversation comments and commits, through a new provider method `proposal_reviews`,
   the existing `proposal_commits`, and the comment readers the providers already have, plus, for
   a closed-unmerged proposal, who closed it (GitHub: the actor of the last `closed` event on
   `GET /issues/{n}/events`; GitLab: the merge request's `closed_by`; Gitea: the poster of the
   last close comment). The conversation readers already page to completion when given a
   `limit`, bounded by `MAX_CONVERSATION_SIZE` (1000 rows), and return `truncated` when more
   remained, on all three forges; the sweep calls them with that limit and reads their
   `truncated` as `kubeagents_vcs_readings_failures_total{reason="detail_truncated"}`, a
   per-proposal signal distinct from the repository walk's `truncated`, counting the proposal
   from what was read. `proposal_reviews` is the existing review pager with the bodiless-review
   filter left off. The commit readers are capped at `COMMIT_CAP` (250) on all three forges:
   GitLab's and Gitea's already page to it, GitHub's reads one page and gains paging to the same
   cap, and no forge serves a proposal's commits past it, so a proposal with more than 250 commits
   is counted from 250 and moves `detail_truncated` by comparing the count read to the cap, since
   the readers' own `truncated` cannot fire there. The commit reader and the closer lookup return
   the account kind beside the translated
   author, as the comment readers already do, since the `actor` label needs it, and the commit
   reader returns the author email (section 4). The result is a small record: touch counts by
   `(kind, actor)` and the closer class. A proposal whose `updated` has not moved is read from
   the cache.

**The cache** is a dict in the broker process keyed by `(forge host, repository, number)` holding
`updated` and the record. It is written per proposal as each is read, whatever the sweep's
outcome, so an attempt cut short by a rate limit keeps what it read and the next one continues
rather than starting over. It is derived data: a restart empties it and the sweeps after refill
it at the cost of step 2 for every proposal once, about seven core-API calls for an ordinary
proposal and more for one whose conversation runs past a page, against the installation token's
hourly quota, which the agent's own verbs share. So step 2 is budgeted by the hour and in calls,
not in proposals: `VCS_READINGS_DETAIL_CALLS_PER_HOUR` (1000, a fifth of GitHub's five-thousand-call
installation quota) spread evenly over the sweeps in an hour, about sixty calls a sweep at the
default TTL, about nine ordinary proposals or fewer long ones, so a refill of a thousand proposals
takes about seven hours and never starves the agent's own verbs. The details reading is taken only
once every listed proposal is current in the cache; until then its series keep their previous
values, or stay absent after a restart, with `reason="warming"` on the failure counter, while
the outcomes reading is taken from the first complete listing. The cache's eviction rule is one
rule with two cases. In a repository within the walk bound, a proposal absent from a complete
listing was deleted, and its record is evicted, so the cache holds at most the listing. In a
repository past the bound, a proposal absent from the walk may have been deleted or may have
fallen off the end, and the forge gives no cheap way to tell them apart, so the record is kept
for the life of the process: a terminal proposal's touches and closer stay counted, and a deleted
one's stay counted until the next restart, an over-count bounded by the proposals deleted since
the process started, on a repository already past a thousand own proposals. The cache therefore
holds the listing plus, on a truncated repository, every proposal that has surfaced since the
process started. This is the only cross-request memory the design adds, and no answer the broker
gives depends on it.

**Publishing.** There are two readings, and each is all or nothing over the repositories that
require it, which is every managed repository except one whose credential cannot name itself.
The outcomes reading is taken when every such repository completed step 1 and publishes
`kubeagents_vcs_proposals`; the details reading is taken when every such repository completed
step 2 with a warm cache and publishes `closed_by`, the touch series and the per-proposal gauge.
A repository that fails a step withholds the reading that step feeds and not the other: a
rate-limited step 2 leaves the outcomes reading current and the details reading as it was.
A withheld reading keeps its previous series values, its `kubeagents_vcs_readings_age_seconds`
grows, and `kubeagents_vcs_readings_failures_total{reason}` moves. When a reading's age passes
`VCS_READINGS_STALE_SECONDS` (3600) its series are omitted from the exposition until it is taken
again, so the operator clears the fields rather than reporting an hour-old number as current.
The failure counter is never omitted, and an age gauge is never omitted once its reading has been
taken, so a stale reading is visible as such in Prometheus. The CR carries no age, so a reader of
`status.usage` alone sees the last reading as current for up to an hour plus two polls; that is
accepted, and the operator's two-miss clear bounds it.

**Cost.** Steady state per managed repository per sweep: one author-filtered page per hundred of
the install's own proposals, and step 2 only for proposals that changed since the last sweep. A
repository where the install has opened 300 proposals and people add two reviews an hour costs
three listing calls a sweep and, on the sweeps where a proposal changed, about seven more.
GitHub's search quota is thirty calls a minute per token, so the sweep paces its search calls at
`VCS_READINGS_SEARCH_CALLS_PER_MINUTE` (25): an install with many managed repositories gets a
sweep longer than the TTL and older readings, not a rate-limited tail that never completes.

**The token.** The sweep does not ride the write token: an idle install would otherwise mint a
write-capable installation token every TTL on the CLI transport, whose refresh runs before every
credentialed verb and coalesces over thirty seconds only, and every thirty minutes on the HTTP
transport, to keep a token fresh that nothing else uses. The sweep mints its own read token
through a Minty scope of its own, `platform-agent-sweep-scope`, declared in the rule template's
`default.yaml` beside the two scopes there with `contents: read`, `pull_requests: read` and
`issues: read`, the least that lists proposals, reviews, comments and close events. A scope of
its own rather than a widened `platform-agent-read-scope`, because that scope is shared: the
operator carries it into every context repository's policy, and the read-only clone token of a
private context repository would gain permissions nothing consumes, against the pins that hold
that scope at `contents: read` alone. The managed repositories' policies copy `default.yaml`
whole (`renderRepoPolicy`), so the new scope reaches every managed repository on the next
reconcile with no operator change, and the context-only reduction (`renderReadOnlyPolicy`) drops
it from context policies as it drops the write scope today. Two things in the tree stand in the
way and the implementing change moves them: the read mint (`mint_read_credential`) refuses a
repository that is not a context repository, because a managed one "already has the write
credential and must keep riding it", so it gains a sweep caller that the role check admits and
that names the sweep scope; and the helper scopes a minted token to one repository and returns
no coverage list, so the sweep's reuse is per managed repository for the same thirty minutes the
write token is reused, not per organization. On github.com the forge's transport is the
`gh` CLI with the stored write credential, so the sweep cannot use it: it uses the in-process
HTTP transport the GitHub Enterprise Server path already has, pointed at `api.github.com` with
the read token, a construction the forge does not make today. On GitLab and Gitea, whose
credential is a static token, the sweep reads with that token as every verb does. A sweep
therefore costs one read mint per thirty minutes per managed repository, holds no write
capability, and leaves the write token's lifetime to the agent's own work. Rate-limit answers
(`FORGE_RATE_LIMITED`) end the sweep for that repository without a retry inside the sweep; the
next interval tries again. The sweep runs without a request slot, as the forge-refresh route does,
because a slot's deadline would cap a whole first refill at one request's length; it reserves
transient child memory the same way.

## 6. What the operator does

A new reading slot beside the cluster gauges, generic rather than a third special case: a declared
list of reading series (`usageReadingSpecs`) with, for each, the status field it lands in, its
label filter, the reading it belongs to (`outcomes` or `details`) and the aggregation across pods
(`max`, the cluster gauges' rule; the broker is one pod by design, with a `Recreate` strategy, so
the rule is for form). The scrape parses them from the broker body into `usageReading.Readings`;
`foldUsage` never sees them; `projectStatus` writes them as the latest poll's reading. The rules
are the cluster gauges' rules, applied to the broker target, with one difference: the cluster
gauges share one miss streak, and here each reading has its own, because a warming sweep, or a
sweep where one repository failed step 2 alone, serves the outcomes series and not the details
series in one body.

- One poll that cannot read the broker, or reads it without a reading's series, leaves that
  reading's fields as they were. The second in a row clears them to absent. The broker's own
  staleness rule above makes an hour-old reading absent on the broker side first.
- The fields are pointers, so a zero is written as a reading.
- They do not move `lastActiveTime` or `lastHumanActiveTime`. This is a change: before this design
  a merge the broker happened to read moved `lastActiveTime`. A reading cannot say when a merge happened,
  only that a count differs from the last poll, which is a poor timestamp, and a merge is a
  person's or a forge rule's act, where the field's meaning is the agent at work. The field
  comments, the CRD docs and the producer design's `lastActiveTime` row say so in the
  implementing pull request.
- A field the install's CRD does not carry takes the existing pruning record path with its own
  record name.

Fields, in `AgentUsageStatus`:

| Field                             | From                                                                    | Kept name?                                                                                  |
| --------------------------------- | ----------------------------------------------------------------------- | ------------------------------------------------------------------------------------------- |
| `remediationsMergedTotal`         | `kubeagents_vcs_proposals{state="merged"}`                              | yes; the comment and docs change from "counted once per process" to "the forge's count now" |
| `remediationsClosedUnmergedTotal` | `kubeagents_vcs_proposals_closed_by{actor="human"}` + `{actor="bot"}`   | yes; a close the install made itself stays out, as today                                    |
| `remediationsOpen`                | `kubeagents_vcs_proposals{state="open"}`                                | new                                                                                         |
| `proposalsAuthored`               | `kubeagents_vcs_proposals` summed over `state`                          | new; the denominator of the merge rate, the install's own closes included                   |
| `proposalHumanTouches`            | `kubeagents_vcs_proposal_touches{actor="human"}` summed over `kind`     | new                                                                                         |
| `proposalsMergedUntouched`        | `kubeagents_vcs_proposal_touches_per_proposal{outcome="merged",le="0"}` | new                                                                                         |

The two kept fields change type from `int64` to `*int64`. Their CRD schema (an integer) does not
change; the operator's gauge export (`usage_gauges.go`) gets a `pointer: true` row for each of the
six, and `TestUsageGauges_CoverEveryNumericAndTimeFieldOfTheStatus` holds the list complete.
`remediationsMergedTotal`, `remediationsOpen` and `proposalsAuthored` belong to the outcomes
reading; `remediationsClosedUnmergedTotal`, `proposalHumanTouches` and `proposalsMergedUntouched`
to the details reading.

Field names are this document's call: the two kept names carry `Total` for a reading because
renaming them breaks the chart's health rules and the feedback summary for nothing a reader
gains; the four new ones do not say `Total`, because they are not.

**Health rules.** Before this design `charts/kube-agents/templates/platform-agent-health-rules.yaml` computed
`proposals_rejected_ratio_14d` and `proposals_merge_rate_14d` with `increase()` over the operator's
usage gauges, and the merge rate divided by `remediationsProposedTotal`, the event counter.
`increase()` reads a fall in a reading as a counter reset and inflates the result, and a reading
over a counter is the mixed-source ratio section 4 rules out. Both recording rules and the
alerts' minimum-count guards are rewritten over readings alone, keeping their fourteen-day
window: the `KubeAgentsProposalsRejected` guards, which apply `increase()` to the two outcome
fields, and the `KubeAgentsProposalsUnmerged` guard, which applies it to
`remediationsProposedTotal` and moves to `proposalsAuthored`, keeping the threshold's name and
its meaning of proposals opened in the window. An operator roll-over splits a window across two
series of the usage gauge, which is why the template sums `increase()` and maxes instants; a
`delta()` has neither aggregation right, so one recording rule (`usage_reading:max`, keyed by
`field`) first takes `max by (agent_namespace, agent, field)` of the gauge over the reading fields,
and the window deltas are taken over that recorded series. With `merged_14d = clamp_min(delta(<recorded merged>[14d]), 0)`, `closed_14d`
and `authored_14d` likewise: `proposals_rejected_ratio_14d = closed_14d / (closed_14d + merged_14d)`,
the share of decided proposals a person rejected, as today; and
`proposals_merge_rate_14d = merged_14d / authored_14d`, the share of proposals opened that merged,
which keeps the published alert text and the threshold's calibration against proposals opened
and keeps the two series from being each other's complement. A fall in a reading within the
window, a repository removed from `managed_repos` or proposals deleted, is the case `clamp_min`
alone handles badly: it clamps the fallen series to zero and hides a fortnight of the other
repositories' merges, so `proposals_rejected_ratio_14d` could read one and fire
`KubeAgentsProposalsRejected` falsely. So the rules carry a fall guard, and it is narrow on
purpose: it reads `resets(<recorded proposalsAuthored>[14d]) > 0`, that series alone.
`proposalsAuthored` is the one reading that falls only on a removal or a deletion: a person who
closes a proposal and reopens it an hour later moves `closed` and `open` and not their sum, and a
merged proposal cannot be unmerged, so a reopen, an ordinary forge action, does not silence the
alerts; it reaches `closed_14d` as a one-unit under-count that `clamp_min` already tolerates. The
guard reads `resets()`, not the window's net `delta()`: a `delta()` is end minus start and goes
blind to a fall the moment later rises outrun it, where `resets()` sees any decrease between two
samples anywhere in the window whatever follows. When the authored series shows a reset in the
window, both ratios are absent and neither alert can fire, for the fourteen days the fall stays
in the window. A removal, or a deleted proposal, silences the two proposal alerts for a fortnight
rather than misfiring, and the chart README's upgrade paragraph says so beside the upward step. The
template's header contract and `tests/test_chart_platform_agent_health_rules.py`, which pin every
`increase()` under `sum by` and every `field=` to the operator's export list, learn the
recorded-series `delta()` form and the `resets()` guard. The first reading after an install
upgrades replaces a per-process
count with the forge's all-time count, and the window reads that step as fourteen days of
outcomes, so both rules are distorted for two weeks after the upgrade, and the merge rate can
read above one in that time. The same step happens whenever a repository with a history enters
`managed_repos`, for the fourteen days after. Both are accepted and said in a new upgrade
paragraph in the chart's README, which has no metrics upgrade note today, since the alternative,
dropping the window, loses the recent-behaviour signal the health cohorts exist for. The usage
ConfigMap document drops the two removed counter totals and their pod entries in
`migrateUsageDocument` (`usage_counters_fold.go`), the existing migration step, so the other
counters keep their baselines; a re-seed would reset every baseline and lose a poll's worth of
each.

**Feedback summary.** The field list lives in two mirrored copies, `USAGE_COUNTERS` in
`agents/platform/scripts/feedback_summary.py` behind `scripts/feedback-summary.sh`, and
`usageSummaryCounters` in `a2a/gateway/feedback_summary.go` behind the gateway's `/feedback
summary`. Both print `remediationsMergedTotal` and keep doing so; both add `proposalsAuthored`,
`remediationsOpen`, `remediationsClosedUnmergedTotal`, `proposalHumanTouches` and `proposalsMergedUntouched` so the one block
answers the three questions. The wording in the block says "as the forge reports" for those lines, and a field with
no reading prints as "no reading", never as 0, which changes the "a missing one prints 0" contract
of both copies; their vocabulary audits are computed from a render and follow.

## 7. Security and privacy

No new listener, port, NetworkPolicy rule or RBAC. The broker still never writes a ConfigMap and
still mounts gitops-state read-only. Labels are closed vocabularies; no login, repository, URL or
number leaves the pod in a metric. The sweep reads with a read-only token of its own on GitHub,
scoped to contents, pull requests and issues read, and with the static token every verb uses on
GitLab and Gitea; it asks for nothing the broker's own verbs could not already read on request,
and it never keeps a write-capable token fresh on the agent's behalf.

The metrics listener's contract does not change: it still "holds no route, credential or policy
of its own", a scrape still returns the registry and nothing else, and no peer that reaches the
port can cause a forge read. The sweep runs on the broker's own clock, with a read-only
credential the broker holds, and the three security documents that state the listener's property
stay as written.

## 8. Tests and validation

Python, beside the broker in `agents/platform/scripts/` (`test_vcs_broker.py`,
`test_credential_proxy_metrics.py`, `test_providers_contract.py` and the per-forge
`test_providers_*.py`):

- a sweep over a fake forge with proposals in all three states and events of every kind by the
  install, a human and a bot, asserting every series and every bucket, where the install's own
  commits carry no linked login and only its author email, as on a real forge, and land under
  neither actor;
- a repository past the walk bound publishes its state counts from the tally (merged and closed as counted, open the remainder of the one count) and its details from the
  thousand most recently updated proposals and the cache; a Gitea repository past its own bound,
  counted in proposals under a server that clamps the page size, withholds the install's
  outcomes reading;
- a proposal with more than 250 commits is counted from 250 and moves `detail_truncated`;
- a closed proposal reopened within the window does not silence the alerts; a removed repository
  does;
- a proposal whose conversation runs past a page is read through the readers' own paging, and
  one past their bound is counted from what was read with `reason="detail_truncated"`;
- a proposal whose `updated` did not move is served from the cache and costs no detail call; one
  that moved is re-read;
- one repository failing withholds the reading and moves the failure counter; the previous reading
  stays; past the stale bound the reading series are omitted and the age gauge and failure counter
  are not;
- the timer runs at most one attempt per interval, a failed attempt included, and a scrape of
  the listener starts nothing;
- a forge that returns another author's proposal under the author filter ends the repository's
  attempt with nothing published;
- a cache being warmed takes the outcomes reading and not the details reading until every listed
  proposal is current, keeps what a cut-short attempt read, and spends no more than the hourly
  detail budget;
- the sweep mints a read-scope token and never a write one, and reuses it for the reuse window;
- a nameless credential on one repository leaves the other repositories' readings whole;
- a credential that cannot name itself publishes nothing and logs once;
- bodiless approvals count as `review_approved`; a comment beginning `/agent` counts as
  `agent_address` and not as `comment`; an open proposal older than fourteen days with no human
  touch counts in `kubeagents_vcs_proposals_ignored` and one with a touch does not;
- the removed counting path has no test left referring to it.

Go, beside the poller: the reading slot parsed from a broker body; one miss keeps, two clear, per
reading, with a body that serves one reading and not the other; and, beside the chart, the
health-rules test rendering a window with a fall later outrun by rises and asserting both ratios
absent;
pointer zero written; the fields never move `lastActiveTime`; export rows; pruning record; the
status key count. `tests/test_usage_counters_series_names.py` pins the new series names across
Python and Go and loses the two removed ones.

Eval: exempt under `.agents/rules/eval_driven_development.md`, the metrics listeners, the poller
and the status schema; no prompt, skill or SOP changes.

Live validation on gkedemos3, the forge-backed install: open a proposal through the shell pod's
`vcs.py`, merge it with `gh` as a person, add one review and one comment to a second proposal and
close it, and watch the broker series and the six fields move within two poll intervals with no
`proposal view` issued by anyone, and the broker's log show a read mint and no write mint; and
read `kubeagents_vcs_proposal_touches_per_proposal{outcome="merged",le="0"}` as one for the
merged proposal nobody touched, which is the check the commit rule exists for. The record goes
in the pull request and in #4120.

## 9. Left to the builder

- The exact `payload` shape and error vocabulary of `proposal_list_by_author` and
  `proposal_reviews`, in the style of `base.py`; whether the transport exposes response headers
  today or needs a `headers=` return path for the GitLab and Gitea totals.
- How the per-repository work is spread across sweeps on an install with many managed
  repositories, within GitHub's search quota.
- Whether the detail cache is one dict or one per repository, and its eviction mechanics.
- Log lines, their levels, and the INFO once-per-process guards.
- The order of removal versus addition inside the pull request, and whether the operator change
  and the broker change ship as one pull request or two (one is preferred: the series-name test
  binds them).

Files the implementing pull request touches beyond the broker, the providers and the poller, so
none is missed: the field comments in `k8s-operator/api/v1alpha1/common_types.go` and the two CRD
copies regenerated from them (`k8s-operator/config/crd/bases/` and `charts/kube-agents/crds/`,
held in step by `make chart-check`); the counter lists and the poll-interval comment in
`usage_counters_fold.go` and `usage_counters_poller.go`; the generated
`operator/platformagent-fields.md` (`make docs-generate`); the prose in site `concepts/observability.md`
and `operator/platformagent-crd.md`, including every line that says a merge moves
`lastActiveTime`; the rows, the summary, the `lastActiveTime` row and the testing record in
`usage-counters-producer.md`; the chart's health rules, their header contract,
`tests/test_chart_platform_agent_health_rules.py` and the chart README; the metrics listener's
docstring in `credential_proxy.py`, which gains a sentence saying the sweep's timer is not the
listener's; the series constants in `usage_counters_scrape.go` and `migrateUsageDocument` in
`usage_counters_fold.go`; Minty's rule template `default.yaml` in the chart (`github-minter.yaml`) and
`k8s-operator/config/integrations/github/configmap.yaml.template`, where the sweep scope is
declared, with `tests/test_chart_minter_read_scope.py`, whose `assert_two_scopes` asserts the
scopes a rendered policy carries and learns the third, and every other statement in the tree that
a policy carries two scopes (the site's `deploy/token-minter.md`, the integration's `README.md`,
the chart comment above the template, and the `contents: read` docstrings in
`github_token_refresh.py` and `MintedReadCredential`), each of which gains the third scope or is
left true by naming the read scope alone; the commit translators of the three providers, which
gain the author email; `mint_read_credential`'s role check and the
read helper's single-repository scope in `credential_proxy.py` and `github_token_refresh.py`,
`MintedReadCredential`, which backs clones only today, and the github.com forge's transport
construction in `providers/github/forge.py`; the two feedback-summary copies and their tests;
`tests/test_usage_counters_series_names.py`.

## 10. Calls made in this document rather than by the requester

- The listing is author-filtered at the forge, with a per-repository bound over the install's own
  proposals; a GitHub or GitLab repository past the bound still publishes exact state counts, a
  Gitea one withholds the install's outcomes reading, and a truncated repository's detail series
  cover the thousand most recently updated proposals plus whatever the process has cached.
- The two kept `status.usage` names carry `Total` though they are readings; the four new fields
  do not.
- The health rules move to readings on both sides of each ratio and keep their window; the
  two-week distortion after an upgrade or a registration is accepted, and a fall of
  `proposalsAuthored` within the window, seen through `resets()`, silences the two proposal
  alerts for a fortnight rather than letting them misfire; a reopen is absorbed as a one-unit
  under-count of closes.
- The broker owns one timer, the sweep's, at the TTL; an earlier draft triggered the read from
  a scrape of the metrics listener, which the reviewer of this document showed costs the
  listener's security property for no saving in forge traffic.
- The epic's Workstream 2 named a counter, `kubeagents_vcs_proposal_touches_total{kind}`, counted
  per event per broker process. This document ships a reading, `kubeagents_vcs_proposal_touches`,
  with an `actor` label, plus the bucket series, for the same reason the outcomes are readings.
- The epic's other two Workstream 2 items are both kept, in Prometheus and not in `status.usage`:
  a `/agent` address is its own touch kind, `agent_address`, read from the comment body the
  readers already return; and an open proposal untouched for fourteen days is counted by
  `kubeagents_vcs_proposals_ignored`, a reading taken on the same sweep, rather than a counter.
  Neither gets a status field until a reader of the CR asks for one.
- The install's own close is `actor="install"` and stays out of `remediationsClosedUnmergedTotal`.
- A merge no longer moves `lastActiveTime`.
- A commit is the install's by linked login or by its git author email against the install's
  configured addresses, since the install's commits carry no login on any forge.
- After a broker restart the details reading, the close count included, waits until the detail
  cache is warm under an hourly call budget, while the outcomes reading is published at once.
- The sweep mints a token of its own per managed repository under a new Minty scope with
  contents, pull-request and issue read, rather than riding the write token or widening the
  context repositories' read scope.
- The merge rate's denominator is `proposalsAuthored`, every proposal the install authored that
  the forge still holds, including the ones the install closed itself.
- Reactions are out of scope for this reading.
