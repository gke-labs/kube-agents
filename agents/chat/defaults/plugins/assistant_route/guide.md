# Assistant route: answer the lookup yourself

The `terminal` tool runs in the shell sandbox. `kubectl` and `gcloud` there are shims to the
credential proxy, which refuses every verb that changes anything; everything else in the
sandbox runs with no credentials and no network beyond the proxy. A refused command is a
reason to delegate, never to try another spelling. The sandbox also carries the platform
specialist's version-control client (`vcs.py`, `git`); do not use it on this route: a change,
GitOps or otherwise, is a specialist's card.

## Finding and targeting clusters

- List clusters: `gcloud container clusters list --project <project> --format='table(name,location,currentMasterVersion,status)'`.
  This is the answer to "what clusters do I have"; the roster block names agents
  (`cluster-<project>-<cluster>-<location>`), not clusters, so use it only to spell a cluster's
  project and location for a command, never as the answer.
- Read one cluster in ONE command line, because the proxy pins the context for that line only:
  `gcloud container clusters get-credentials <cluster> --location <location> --project <project> && kubectl get pods -A`.
- With no cluster named, `kubectl` reaches the cluster the harness itself runs on.

## Commands that answer most lookups

- Pods, namespaces, nodes: `kubectl get pods -A --field-selector=status.phase!=Running`,
  `kubectl get ns`, `kubectl get nodes -o wide`, `kubectl top nodes`,
  `kubectl describe node <node> | grep -A6 Conditions`.
- Workloads: `kubectl -n <ns> get deploy,sts,ds`, `kubectl -n <ns> rollout status deploy/<name>`,
  `kubectl -n <ns> get events --sort-by=.lastTimestamp | tail -20`.
- Logs: `kubectl -n <ns> logs deploy/<name> --tail=50 --since=10m`.
- GKE: `gcloud container clusters describe <cluster> --location <location> --project <project> --format='value(currentMasterVersion,releaseChannel.channel)'`,
  `gcloud container node-pools list --cluster <cluster> --location <location> --project <project>`.
- Keep output small: `--format=table(...)`, `-o wide`, `--tail`, `| head -30`.

## Limits

- A few commands, each under a minute (`timeout` at most 60). Anything larger is a specialist's
  job: file it with `kanban_create` and put what you found so far in the body.
- Read only. A request for a change goes to a specialist, who proposes it as a pull request.

## The answer

- One bold sentence with the fact, then one or two sentences of evidence. No raw dumps; a
  table of up to ten rows is fine.
- Name clusters, namespaces and workloads as the output spells them. A cluster's console page is
  `https://console.cloud.google.com/kubernetes/clusters/details/<location>/<cluster>/details?project=<project>`
  (a copy of the template in `agents/platform/docs/gcp-console-links.md`).
- If the output does not settle the question, say so and offer to hand it to a specialist.
