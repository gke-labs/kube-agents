---
title: A day with kube-agents
description: Three scenarios from trigger to pull request — a scheduled security audit, a crash loop that ends in a GitOps pull request, and a chat question.
---

Each scenario follows one trigger to the chat messages, issues and pull requests it produces. The install runs `spec.mode: next`, the first-install default, has a GitOps repository in `managed_repos`, and has at least one chat platform turned on. Under `next` the A2A gateway holds Google Chat when it is enabled and Slack otherwise, and posts to the home channel set at install; with both platforms enabled, Slack stays on the agent pod, as under `today`. Where the two modes show a scenario differently the text says so; [What carries over from `today`](/kube-agents/concepts/chatops/#what-carries-over-from-today) has the full list. Names such as `shop`, `checkout`, `redis` and `prod-east` are placeholders.

## A scheduled audit finds an exposed database

Earlier in the week, someone edits the manifest of the `redis` Service in your GitOps repository to `type: LoadBalancer`, to reach it from a laptop, and your GitOps tool applies it to namespace `shop` on cluster `prod-east`. Nothing objects: the load balancer gets a public address and the Service is healthy. The same namespace also runs a `checkout` Deployment with `securityContext.privileged: true`.

At 06:20 UTC the next day, the `compliance-audit` job (Security & RBAC Posture Audit) runs on the Platform Agent. The audit is read-only and covers every cluster in the install's scope. Two of its checks fire, and both are `critical`. `lb-world-open` flags the Service, because it publishes port 6379 to the internet with no `loadBalancerSourceRanges` and no internal load balancer annotation. `privileged-container` flags the Deployment.

The run publishes to the GitOps repository:

- One ledger issue, `[audit] Security & RBAC Posture Audit — 2 findings (2 critical)`, with the labels `agent:audit`, `audit:compliance-audit` and `severity:critical`. Each finding carries the command that produced it, so a reviewer can run it again.
- One remediation pull request for the Service, titled `fix(compliance-audit): <finding title>`, on a branch that starts with `platform-agent/fix-compliance-audit-`. It edits the Service's own declaration. The run opens pull requests unasked, at most five per run, only for findings whose fix is a manifest change. These are `critical` findings, and `major` findings on `unbound-sa-automount`. If a fix can break something the audit does not see, it waits in the ledger for `/remediate`.

The privileged container gets no pull request. Its fix is `manual`, because dropping privilege can break a workload that needs one capability, so the ledger gives the steps and the owner decides. The harness opens pull requests and never merges them ([what the agent can and cannot do](/kube-agents/reference/security-and-iam/#what-the-agent-can-and-cannot-do)).

Chat gets a short report on every enabled chat platform. On Google Chat it is the audit line, `Security & RBAC Posture Audit: 2 critical findings, 2 new.`, a line linking the pull request it opened, and the ledger link. On Slack it is a card with the headline "Security & RBAC Posture Audit: 2 critical findings", one row for each critical finding (up to ten), a "Look at the first one" button and a link to the ledger issue ([ChatOps → Proactive alerts](/kube-agents/concepts/chatops/#proactive-alerts-both-channels)). A reply in that thread reaches the Planning Agent with the report attached.

The next morning nothing has changed. The run rewrites the ledger body and posts nothing in chat. After a reviewer merges the pull request and your GitOps tool applies it, the next run no longer finds the Service. It comments on the ledger with **1 resolved**, changes the title to `1 finding (1 critical)`, and posts `1 critical finding, 1 resolved` on Google Chat. On Slack, the card now reads "1 critical finding". When the Deployment is fixed too, the run comments "`compliance-audit` is now clean — closing", closes the ledger as completed, and closes any remediation pull request still open for the audit.

The ledger card needs `KAGE_SLACK_UX` on (the default) and a ledger on github.com; otherwise Slack gets the text lines. On a new install, this audit runs once after the onboarding scan, without waiting for its schedule ([ChatOps → First-run onboarding](/kube-agents/concepts/chatops/#first-run-onboarding)). The weekly cost audit, `fleet-wide-cost-analysis` (Fleet Waste Audit, Mondays at 07:50 UTC), works the same way. It reports orphaned volumes, unattached disks, idle addresses and over-requested workloads in resource units, never in dollars. [Reference → Cron jobs](/kube-agents/reference/cron-jobs/) lists every schedule.

Without kube-agents, the Service stays exposed until someone reviews Services across every cluster by hand or a separate scanner reports it. Kubernetes reports the Service as healthy, because it does what the manifest asks. The privileged container stays the same way, and both fixes wait for someone to write them.

## A crash loop becomes a root cause and a pull request

A config change rolls out to the `checkout` Deployment in namespace `shop` on cluster `prod-east`, and the new pods start to crash. Kubelet emits `BackOff` Warning events. The event watcher in the agent pod watches its own cluster and every cluster that has a Cluster Agent. By default, it forwards a pod's `BackOff` event once kubelet has reported it three times and the failure has lasted two minutes; one that clears sooner is not forwarded.

kube-agents posts one alert in chat. On Google Chat it reads "🟡 **Warning:** Back off `shop/checkout` — _event message_", followed by "🌱 _Digging down to the root cause..._". On Slack it reads "🚨 **checkout in prod-east keeps crashing.** Looking now." The Planning Agent then files one card for the `prod-east` Cluster Agent.

The Cluster Agent checks that its kubeconfig selects `prod-east`, then runs only read commands such as describe, logs and events. It completes the card with a report in three sections: "What's wrong", "Why" and "What to do". For example, the report can show that the container's last termination reason is `OOMKilled` against the memory limit in the manifest. When there are two or more fixes, they are lettered options with one marked as recommended. The report ends with the call to action: reply `apply` to open a GitOps pull request with the recommended fix, or `apply Option A` or `apply Option B`. On Google Chat the report is a reply in the alert's thread. On Slack it replaces the alert: the headline is the "What's wrong" sentence, with one button per option and the evidence in a folded section.

An engineer replies `apply` in the thread, or clicks the recommended option on Slack. The stored report goes with the reply to the Planning Agent, which sends the change to the Platform Agent. The Platform Agent uses the `submit-suggestion` skill. It edits the Deployment's manifest in the GitOps repository on a `platform-agent/` branch and opens a pull request (a merge request on GitLab). On Slack the thread then shows `I opened PR #<n> in <repo>. It's yours to review.` with Open PR and Files changed buttons.

Nothing was written to `prod-east`. The pods keep crashing until a person merges the pull request and your GitOps tool applies it. The agent cannot merge, because the version-control broker has no merge operation. If the reviewer wants a change, they comment on the pull request with a comment that begins with `/agent`. The repository watcher runs every ten minutes, reads that comment from anyone with write access, and wakes the Platform Agent to amend the same branch.

The Slack messages in this scenario (the one-sentence alert, the report that replaces it, and the pull request message with buttons) need `KAGE_SLACK_UX` on, the default. With it off, Slack gets the plain alert and report text. These event reasons can raise an alert: `Failed`, `FailedToDrainNode`, `CrashLoopBackOff`, `BackOff`, `ImagePullBackOff`, `ErrImagePull`, `OOMKilled` and `FailedScheduling`. The watcher also reads the cluster autoscaler's scale-up decisions, but only to time a `FailedScheduling` alert: it waits while a new node comes up, and when the autoscaler declines to add one, it alerts on the scheduler's next retry without waiting for more repeats. Other reasons make no alert. By default, duplicates are suppressed per pod and reason for 24 hours after the last sighting, so three crashing replicas can give three alerts. Across the fleet, chat gets at most 10 Critical and 5 Warning event alerts per UTC day by default, and a kubelet `BackOff` grades as Warning. Past the limit, chat shows nothing, and the weekday 21:00 UTC recap counts the dropped alerts. If both chat platforms are on, the alert goes to Google Chat, and to Slack only if Google Chat does not accept it ([ChatOps → Proactive alerts](/kube-agents/concepts/chatops/#proactive-alerts-both-channels)). The watcher reads the list of Cluster Agents when it starts, so a cluster onboarded later is not watched until the watcher restarts.

Without kube-agents, the crash loop shows in pod status and events until someone looks or a separate alert rule fires. The person who responds then needs credentials for the cluster, runs the same reads by hand, and writes the manifest change themselves.

## A developer asks why a deployment is stuck

In a Slack direct message to the bot, a developer writes "why is my checkout deployment stuck in Pending?". The message gets a 🔍 reaction, picked from its words with no model call. Every chat message reaches the Planning Agent first, and it has no cluster tools. It replaces "my" with the cluster the developer stored in personal memory earlier, here `prod-east`. It files a card for the `prod-east` Cluster Agent and replies with one line, such as `checking checkout.`

The Cluster Agent runs its preflight check, then reads the pods, the namespace events and the Deployment. For a Pending pod, its troubleshooting skill goes to the namespace events and their `FailedScheduling` message, for example `Insufficient memory`. Progress shows in the thread while it works. When the card completes, the gateway posts the Cluster Agent's full result in the thread: the cause, the evidence and a proposed manifest change. On Slack, the first sentence usually leads in bold, with the rest in a folded "why" section. No pull request opens, because the developer asked a question.

The developer replies "open a PR with that fix". The Planning Agent files a card for the Platform Agent, which opens the pull request through `submit-suggestion`. On Slack a separate message shows the pull request with Open PR and Files changed buttons. The credential broker refuses kubectl and gcloud commands that change state, for every agent in the pod, so the pull request is the only change ([Security & IAM](/kube-agents/reference/security-and-iam/#what-the-agent-can-and-cannot-do)).

Personal memory works only in a direct message; in a channel, the Planning Agent asks which cluster. Reactions, buttons, the progress plan message and the folded answer are Slack features and need `KAGE_SLACK_UX` on, the default. On Google Chat under `next` the gateway lays out the agent's answer with its buttons, keeps one status line per task, and posts each progress note of the card it files as its own message. Under `today` the bot shows a "Kage is thinking…" card, and progress is `⏳` lines on one edited message. A cluster with no Cluster Agent yet gets one from the hourly reconcile at minute 11; until then the Platform Agent investigates it itself.

With no chat platform, use the A2A console's chat pane, the Hermes CLI in the agent pod or the [admin console](/kube-agents/reference/admin-console/) ([User interfaces](/kube-agents/reference/user-interfaces/), [ChatOps → When no chat platform is enabled](/kube-agents/concepts/chatops/#when-no-chat-platform-is-enabled)).

Without kube-agents, the developer needs cluster credentials, reads the scheduler events, matches the message to the requests in the manifest, and edits the manifest. A developer without that access waits for someone who has it.

## Where to go next

- [Proactive autonomy](/kube-agents/overview/proactive-autonomy/) — every scheduled audit and what it checks.
- [ChatOps](/kube-agents/concepts/chatops/#proactive-alerts-both-channels) — how alerts and replies work on each chat platform.
- [Declarative workflow](/kube-agents/concepts/declarative-workflow/#the-fleet-audit-skill) — ledger issues and remediation pull requests.
- [Security & IAM](/kube-agents/reference/security-and-iam/#what-the-agent-can-and-cannot-do) — what the agent can and cannot change.
