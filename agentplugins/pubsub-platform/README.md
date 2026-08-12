# Pub/Sub platform adapter

Alert ingress. The adapter pulls Cloud Logging messages from a Pub/Sub subscription,
filters them, deduplicates them, and turns what survives into agent work. It is what lets
a GKE autoscaler failure become an investigation without a human in between.

The adapter itself is documented where it ships, in
[`files/platforms/pubsub/README.md`](files/platforms/pubsub/README.md) — subscription
config, response delivery, and the message flow diagram. This file covers installing it and
the suppression behaviour that decides whether an alert becomes work at all.

## Install

```bash
GCP_PROJECT_ID=<project> KUBECTL_CONTEXT=<context> ./install.sh
```

The installer builds the image locally and pushes it to Artifact Registry — see
[Images](../README.md#images) for the reference it derives, the builders it picks from and
the variables that override them. `PLUGIN_IMAGE` installs one that already exists and skips
the build.

`HERMES_NAMESPACE` (default `kubeagents-system`) and `AGENT_REF` (default `platform-agent`)
say which agent to attach to, and any plugin whose routes this adapter is to serve must be
installed with the same pair.

This plugin has no `targetProfile`. Platform adapters are gateway singletons: only the
default profile runs the listener, so the operator keeps the `platforms` subtree there even
when the work it produces belongs elsewhere.

## Routes

A route is one subscription plus the rules for what to do with its messages. Consumers
configure them under `platforms.pubsub.extra.subscriptions.<route>` in their own
`AgentPlugin` — see [the stockout investigator's chart](../gke-stockout-investigator/templates/agentplugin.yaml)
for a worked example.

| Key                  | Effect                                                                        |
| -------------------- | ----------------------------------------------------------------------------- |
| `filter`             | Boolean expression over payload paths; a message that fails it is dropped     |
| `threshold_count`    | Ignore until this many matching messages arrive inside the window             |
| `deduplicate_fields` | Paths whose values identify one incident; repeats are suppressed              |
| `agent_profile`      | Which profile does the work                                                   |
| `dispatch`           | `api` runs a turn in the gateway; `kanban` files a task owned by that profile |
| `skills`             | Skills to apply; `plugin:skill` names are inlined, bare names are invoked     |
| `require_skills`     | `dispatch: api` only — refuse to dispatch when a configured skill won't load  |
| `deliver`            | Where the answer goes (`log`, or another chat platform)                       |
| `sink`               | Name of the log sink feeding the topic; recorded, not read (see below)        |

Three of these decide, silently, whether anything happens at all, so they are worth stating
plainly:

- **A filter that names a path the payload does not have evaluates to empty**, and an
  always-empty filter means something other than what it reads like. Paths may index lists
  (`unhandledPodGroups.0.…`).
- **`deduplicate_fields` may list alternatives** — `[a.b, c.d]` takes the first non-empty —
  because one route often carries several payload shapes. If _every_ field resolves empty
  the message is not deduplicated at all, rather than filed under a key that identifies
  nothing and would then suppress every later message of that shape.
- **`dispatch: kanban` exists because a plugin skill only resolves in the profile that has
  the plugin.** A turn run in the gateway is the default profile's turn, so it cannot open a
  skill belonging to a `targetProfile` plugin; the board's dispatcher spawns the worker as
  the assignee profile, where it can.
- **`require_skills` does nothing under `dispatch: kanban`**, and that is not an oversight.
  Skills are only loaded in the gateway for turns that run in the gateway; a kanban worker
  loads its own, in its own profile, so the adapter never sees one go missing and has
  nothing to refuse. Pairing the two is logged as a warning at startup rather than left to
  read like a guarantee.

**The startup sink presence check is currently disabled, so `sink` is recorded but not
read.** It is the only thing `sink` was ever used for. The check needs
`google-cloud-logging`, which the agent image does not carry; supplying it means either
adding it to the image or enabling Hermes' lazy installs, and that decision is pending.
Running the check without the library just logs
`Could not verify Log Sink … cannot import name 'logging'` on every connect, which is the
same false alarm the configured-name fix removed.

Keep setting `sink` to the name your installer created — it cannot be derived, because the
sink belongs to whoever set the route up, and it is plumbed end to end so that re-enabling
the check is a one-line change in the adapter rather than a reinstall.

`DISABLE_PUBSUB_DEDUP=true` in the pod switches off both dedup and the threshold gate. It
is invisible from the CRs and survives reconciles, so check for it before concluding that
dedup is broken.

## Tests

```bash
python3 -m unittest discover -s tests -p 'test_*.py'      # no cluster needed

KUBE_CONTEXT=<ctx> GCP_PROJECT_ID=<project> \
TARGET_CLUSTER_NAME=<cluster> TARGET_CLUSTER_LOCATION=<region> \
  python3 tests/dedup_e2e_test.py                          # against a live deployment
```

The e2e publishes real messages to the real topic and asserts what the running adapter did
with them: a new incident is filed, repeats and retry variants are suppressed, a different
workload is its own incident, and an alert naming no workload is filtered out. It archives
every task it caused and restores the dedup registry it found.
