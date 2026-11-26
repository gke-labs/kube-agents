# Inbound Alert Webhook

- **Author:** @Fuxiao-Gao
- **Date:** 2026-11-26
- **Status:** proposal for decision. Part of #2002 (epic #1994).
- **Companions:** [`scheduling-on-profiles.md`](scheduling-on-profiles.md) §3, the trigger ingress that this design submits on; [`spec-chatops-gateway.md`](spec-chatops-gateway.md), the Teams inbound receiver and the contract of an ingress that takes a turn; [`spec-profiles-dispatch.md`](spec-profiles-dispatch.md), the `Unattended` tier; [`autoops-architecture.md`](../../agents/platform/docs/autoops-architecture.md), the five contracts of the alert path.

## 1. Purpose and scope

The end-state workflow model names alerts that arrive over Pub/Sub or an HTTP webhook as an event trigger ([`04-workflow-model.md`](../architecture/04-workflow-model.md)). Its text on how a trigger fires is older than the trigger ingress, and this design uses the ingress. An alert that starts outside the cluster reaches the agent only by hand today: a person uses the Slack message shortcut "Ask about this" on the alert message.

This design adds one receiver that takes alert notices from Prometheus Alertmanager, Grafana Alerting, Cloud Monitoring, Datadog monitors and a generic JSON sender. The receiver normalizes each notice, makes one incident key, and starts one read-only triage for each incident.

In scope: the receiver, one adapter for each format, the normalized alert, the incident key, the authentication of a sender, and the mapping to the session model.

Out of scope:

- A read of the source ([§10](#10-the-border-with-the-read-path)).
- An answer to the source. Nothing acknowledges or closes the alert in the sender's system.
- A fix. The triage run is read-only.

The first deliverable is the start of a triage. Later steps add the result in a chat thread, and the follow-up and the resolution on the same conversation ([§8](#8-order-of-the-work)).

## 2. What exists today

Facts of `main` on the date above. The later sections refer to these and do not repeat the numbers.

- **The trigger ingress is built.** An unattended source asks the gateway for a task on `a2a.trigger.<source>.submit` (`a2a/gateway/trigger_ingress.go`, [`scheduling-on-profiles.md`](scheduling-on-profiles.md) §3). The auth callout binds the source to a ServiceAccount. An event source names its trigger `<source>/<kind>` and its run `<source>/<kind>/<token>`. The gateway admits only the kinds in `A2A_TRIGGER_EVENT_KINDS`, which is empty by default. Each kind has a cap of 30 runs an hour; the cap is one value in the gateway's code, and no install setting changes it. The run has the `unattended` tier.
- **An event key is answered for as long as its record lives.** Each event run has its own conversation, `trigger:<key>`. A submission with a key that already ran gets the task of that run, in each state of the run, also a failed one. The record lives until it had no activity for the session time to live (`A2A_SESSION_TTL`, 7 days by default). A repeat is not activity. So the record is gone 7 days after the last activity of the run, and the next notice with that key starts a second run.
- **No event source uses the ingress.** Its code says "No event source has that grant yet". The Kubernetes event watcher and the drift detector still post to the Session KV server (`agents/platform/scripts/session_kv_server.py`, `/sessions` and `/sessions/{id}/inject`) with one shared bearer token. Their move to the ingress is #6572, and its plan moves the triage gating with each watcher.
- **The incident state lives in the Session KV server.** The fold of related events on a workload, the follow-up into the alert's thread, the note when an incident goes quiet, the severity gate, the daily quotas and the ledger are there, and they have the shape of Kubernetes events. The ingress has no follow-up path and no resolution path.
- **A trigger run's result does not reach chat.** The gateway records the delivery that a submission asks for and delivers nothing. The result is on the task and on its conversation. [`scheduling-on-profiles.md`](scheduling-on-profiles.md) §4 is the design of that delivery.
- **The `cluster` profile does not take unattended work.** The profile is off by default. Its default `allowedTiers` is `read`, and an unattended task needs a Job runner for its profile. [`scheduling-on-profiles.md`](scheduling-on-profiles.md) plans the addition in its order of work.
- **One public endpoint exists, and the admin owns its Ingress.** The Teams inbound receiver is its own pod behind an Ingress that the install admin makes ([`spec-chatops-gateway.md`](spec-chatops-gateway.md)). Each other way in needs no public endpoint: Slack uses Socket Mode, and Google Chat and the drift detector pull from Pub/Sub subscriptions (`terraform/modules/chat-pubsub`, `terraform/modules/drift-pubsub`). The gateway's inject door and the A2A door are dev and eval doors with a ClusterIP Service.

## 3. The senders

What each sender gives, from the vendor's public documentation on the date above. The names are the vendor's own.

| Sender                                                             | One request holds                          | The name of one episode (`dedupeId`)                              | State                                                                                                                     | Authentication that the sender can add                                                                                                     | Where the alert is                                        |
| ------------------------------------------------------------------ | ------------------------------------------ | ----------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------- |
| Alertmanager, `webhook_config` (payload `version` 4)               | One group, with a list `alerts[]`          | `groupKey`, with the episode that the receiver counts             | `status`: `firing`, `resolved`                                                                                            | Basic authentication, an `Authorization` header, a client certificate. No signature of the body.                                           | `groupLabels`, `commonLabels`                             |
| Grafana Alerting, webhook contact point                            | One group, with a list `alerts[]`          | `orgId` and `groupKey`, with the episode that the receiver counts | `status`: `firing`, `resolved`                                                                                            | Basic authentication, an `Authorization` header, a client certificate, and an HMAC-SHA256 signature of the body with an optional timestamp | `groupLabels`, `commonLabels`                             |
| Cloud Monitoring, webhook channel and Pub/Sub channel (schema 1.2) | One incident                               | `incident.incident_id`                                            | `incident.state`: `open`, `closed`                                                                                        | Webhook: basic authentication, or a token in the query string. Pub/Sub: the IAM policy of the topic.                                       | `incident.resource.labels`, `incident.scoping_project_id` |
| Datadog monitor, Webhooks integration                              | One notice for each group of a multi alert | `$ALERT_CYCLE_KEY`                                                | `$ALERT_TRANSITION`: `Triggered`, `Warn`, `No Data`, their `Re-` forms and `Renotify` are firing; `Recovered` is resolved | Basic authentication in the URL, a custom header, an OAuth 2.0 bearer token. No signature of the body.                                     | `$TAGS`, `$HOSTNAME`                                      |
| Generic JSON                                                       | One alert                                  | The sender sets `dedupeId`                                        | The sender sets `state`                                                                                                   | A custom header or an `Authorization` header                                                                                               | The sender sets `target`                                  |

Notes for the adapters:

- **Lists.** An Alertmanager or Grafana notice is one group. The group is the incident, so the notice gives one record. `target` comes from the labels that each alert of the group has (`commonLabels`). The sender can cut the list (`max_alerts`, "Max Alerts") and then gives the count of the cut alerts in `truncatedAlerts`.
- **A group key is not an episode.** `groupKey` names the group for as long as the route exists, and a group can resolve and fire again. The key of a run must name one episode ([§5](#5-the-incident-key)), so the receiver counts the episodes of a group: an episode starts with the first firing notice of the group and ends with the notice whose `status` is `resolved`. `incident.incident_id` and `$ALERT_CYCLE_KEY` name one episode by themselves.
- **The Datadog payload is a template that the user writes.** The adapter ships one template that names the variables above, with `$ALERT_ID` as `source.monitorId`, `$INCIDENT_PUBLIC_ID` as `source.incidentId` and `$LINK` as `source.url`. The install admin pastes it into the webhook's payload field.
- **The generic format is the normalized alert of [§4](#4-the-normalized-alert).** A sender that can write its own payload sends that form directly.
- **Resolved notices.** Alertmanager and Grafana send them by default. Datadog sends a recovery notice. Cloud Monitoring sends a closed notice for a policy made with the API, and for a policy made in the console only when the user turns it on.
- **Repeats.** Alertmanager and Grafana repeat a group after an interval (4 hours by default). Cloud Monitoring and Datadog repeat only when the user sets a re-notify interval.
- **Retries.** Datadog stops a request after 15 seconds and tries again up to 5 times, only after a `5XX` answer or an error of its own. The pages of the other three give no retry rule.
- **Cloud Monitoring webhooks reach public endpoints only.** Its documentation names the Pub/Sub channel as the way to an endpoint that is not public.
- **Not stated by the vendors.** The pages of Alertmanager, Grafana and Cloud Monitoring do not say in words that the identifier above is the same in the resolved notice. The recorded payloads of [§12](#12-fixture-and-eval) hold that for each adapter.

Sources: [Alertmanager configuration](https://prometheus.io/docs/alerting/latest/configuration/), [Grafana webhook contact point](https://grafana.com/docs/grafana/latest/alerting/configure-notifications/manage-contact-points/integrations/webhook-notifier/), [Cloud Monitoring notification channels](https://docs.cloud.google.com/monitoring/support/notification-options), [Datadog Webhooks integration](https://docs.datadoghq.com/integrations/webhooks/), [Datadog IP ranges](https://docs.datadoghq.com/api/latest/ip-ranges/).

## 4. The normalized alert

Each adapter turns one notice into one record of this form.

```json
{
  "schema": "kube-agents.alert/v1",
  "sender": "datadog-prod",
  "format": "datadog",
  "state": "firing",
  "dedupeId": "<the sender's name of one episode of the incident>",
  "title": "...",
  "summary": "...",
  "severity": "critical",
  "startsAt": "2026-11-26T10:00:00Z",
  "labels": { "team": "payments" },
  "target": {
    "project": "",
    "location": "",
    "cluster": "",
    "namespace": "",
    "workload": "",
    "pod": ""
  },
  "source": {
    "site": "datadoghq.com",
    "monitorId": "",
    "incidentId": "",
    "url": ""
  }
}
```

- `sender` is the configured name of the sender. It comes from the request path and the secret that the request passed, never from the payload.
- `state` is `firing` or `resolved`. Each other state of a sender (a warning, a re-notify, no data) maps to one of the two in the adapter's table.
- `severity` is `critical`, `warning`, `info` or `unknown`.
- `target` names where the alert is. The adapter reads it from the label names that the sender's configuration gives.
- `source` holds the identifiers that a later read of the source needs. Its names are shared with the read path ([§10](#10-the-border-with-the-read-path)).
- Each text field has a length limit, and the label map has a limit on its count. The adapter removes control characters. A field over its limit is cut and marked as cut. The task text and the record together must fit the ingress limit of 16384 characters for a prompt.

## 5. The incident key

The acceptance rule of #2002 is that a burst of related alerts gives one session. The failure to avoid is on record: with a key for each pod, a fault on N pods became N incidents ([`agent-harness-bottlenecks.md`](agent-harness-bottlenecks.md)). The key has two levels.

1. **One episode of the sender's incident.** The run's key is `alert-webhook/<sender>/<token>`. The token is a digest of the sender name and `dedupeId`. Each repeat notice of the same episode has the same key, and the gateway answers it with the task of the first run. An incident that resolves and fires again is a new episode with a new key.
2. **The workload fold.** Two incidents of one sender system can name the same workload, for example one alert for each pod of a Deployment. The receiver keeps, in memory, the workloads for which it started a run in the last `foldSeconds` (default 3600, the default of the Session KV server's workload fold). A firing notice whose `target` names such a workload (cluster, namespace, workload) starts no new run. A `target` with a pod and no workload folds on the pod name without its generated suffix. A critical alert folds only into a run that a critical alert started, as on the event path.

A sender can also group at its own side (the `group_by` of an Alertmanager route, the group of a Datadog multi alert). The fold is for the senders that do not.

### The state of the receiver

The receiver keeps four things in memory, each for one sender: the open episode of each group (for Alertmanager and Grafana), the keys that it submitted with their tasks, the fold memory, and the queue of [§6](#limits-and-answers). The reply of the ingress holds a task and does not say if the run is new, so the receiver needs its own list of keys to tell a start from a repeat.

The receiver runs as one replica, and a restart empties the state. The cost has a bound: the next notice of an open group starts one more triage, a workload can get one more triage, and the held notices are lost. A durable state is a later option ([§13](#13-open-questions)).

### Limits

- **The fold can hide a second fault.** A different fault on the same workload in the fold time starts no run, and before step 3 nothing adds it to the first triage. `foldSeconds: 0` turns the fold off for a sender.
- **A long episode gets one triage in about a week.** While the record of the key lives ([§2](#2-what-exists-today)), a repeat notice starts no second run. After the record is gone, the next repeat notice starts one.
- **A failed first run stays the answer.** A repeat notice of an episode whose run failed gets that failed task. The receiver cannot ask for the state of a run, because the status subject of the ingress serves schedules only.
- **The sender's grouping decides what one incident is.** While one alert of an Alertmanager or Grafana group fires, the group is one episode. Each later alert of that group, also a critical one, starts no run. A route that groups by the workload's labels keeps one incident to one workload.
- **A storm has two bounds:** the cap of the sender's kind, and `maxRunsPerDay` of the sender (default 100), which the receiver counts. The cap of a kind alone is 720 runs a day, and a failover of the gateway fills its bucket again. [§6](#limits-and-answers) says what happens to a notice over a bound.
- **An optional settle time** (`settleSeconds`, default 0) holds a firing notice before the submit, and a resolved notice for the same key in that time cancels it. The default is 0 because the sender's own rule already has a pending time. Set it for a sender that flaps.

## 6. The door

### Where the receiver runs

The receiver is its own Deployment, `alert-receiver`, with its own ServiceAccount, as the Teams inbound receiver is its own pod. Its one grant on the bus is the trigger source `alert-webhook`: publish on `a2a.trigger.alert-webhook.submit`, and subscribe on its own reply subjects. It gets no grant on the status and stop subjects, which serve schedules only. It holds no cluster credential. It holds no cloud credential in step 1; the Pub/Sub inlet adds one grant, subscriber on its subscription, as the drift detector has. It parses text from outside the install, so it does not share a pod with the credential proxy, which holds credentials, or with the gateway, which mints grants.

The operator renders the receiver, its identity map entry, and one `A2A_TRIGGER_EVENT_KINDS` entry `alert-webhook/<sender>` for each configured sender. So each sender has its own rate cap, and one loud sender cannot use the budget of another.

Each sender's configuration lists the clusters that the sender can name, each with its project and its location. The receiver does not read the install's resolved scope for them: the operator fills the `<name>-resolved-scope` ConfigMap only for a scope with a folder, an organization, a Shared VPC host or a metrics scope.

A proposal for the configuration, beside the other inbound integrations of the `PlatformAgent`:

```yaml
spec:
  integration:
    alertWebhook:
      enabled: true
      senders:
        - name: datadog-prod # a DNS-1123 label of 40 characters at most: the request path and the trigger kind
          format: datadog # alertmanager | grafana | cloud-monitoring | datadog | generic
          auth:
            method: header # bearer | basic | header | query | hmac | pubsub
            header: X-Alert-Token
            secretRef: { name: alert-webhook-datadog-prod, key: token }
          site: datadoghq.com
          targetLabels:
            {
              cluster: kube_cluster_name,
              namespace: kube_namespace,
              workload: kube_deployment,
            }
          clusters: # the clusters that this sender can name
            - { project: my-project, location: us-central1, name: prod-1 }
          maxRunsPerDay: 100
          foldSeconds: 3600
          settleSeconds: 0
```

### How a sender reaches it

The receiver has two inlets, and the code after the inlet is the same for both.

- **HTTP:** `POST /v1/alerts/<sender>` on a ClusterIP Service. The operator renders no Ingress and no LoadBalancer.
- **Pub/Sub pull:** the receiver pulls from a subscription, as the Google Chat backend and the drift detector do.

| Option                                                       | What it is                                                                                                    | Cost                                                                                                                                                         |
| ------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| A. HTTP inlet only, the operator publishes it                | The operator renders an external HTTPS endpoint                                                               | The operator would open a public endpoint by itself, which no part of the install does today. Each install gets it, also those with only in-cluster senders. |
| B. Pub/Sub inlet only                                        | Each sender publishes to a topic                                                                              | Only Cloud Monitoring publishes to Pub/Sub by itself. Each other sender needs a bridge.                                                                      |
| C. Both inlets, the operator publishes neither (recommended) | A ClusterIP Service and an optional subscription. The install admin decides how a sender reaches the Service. | Two inlets to test. The admin has one step for a SaaS sender.                                                                                                |

The recommendation is C. It is the form of the Teams inbound receiver: the operator renders the pod, and the admin owns the Ingress. Each sender gets the shortest path that it can use:

- **A sender in the cluster or in the VPC** (Alertmanager, a self-hosted Grafana): the ClusterIP Service, or an internal load balancer that the admin adds. Nothing is public.
- **Cloud Monitoring:** a Pub/Sub notification channel and the pull inlet. Nothing is public.
- **A SaaS sender** (Datadog, Grafana Cloud, the webhook channel of Cloud Monitoring): the sender can only call a public HTTPS URL. A customer with no public endpoint must set up one of these two, and there is no third way:
  1. Publish the receiver's Service through the customer's own Gateway or Ingress, with TLS. The receiver still authenticates each request. The admin can also allow only the vendor's published source addresses.
  2. Run a small bridge outside the cluster that takes the vendor's request and publishes it to the Pub/Sub topic. The cluster then has no inbound path.

The falsifier of the recommendation: if most installs turn out to need a SaaS sender, the admin step of C is paid by most installs, and an operator-rendered endpoint (option A, as an opt-in field) is the better default.

### How the receiver authenticates a sender

The trigger ingress has a rule of its own, and the contract of [`spec-chatops-gateway.md`](spec-chatops-gateway.md) is for an ingress that takes a turn. The receiver still applies the first condition of that contract by choice: something that the receiver authenticated names the sender, never the message. The request path selects the configured sender, and the request must pass that sender's method:

- `bearer`, `basic` or `header`: a secret value that the sender adds to each request. The receiver compares it in constant time. Two values can be valid at the same time, so a rotation needs no gap.
- `query`: the same secret as a query parameter. It is for the webhook channel of Cloud Monitoring, which can send basic authentication or a query token and nothing else. The receiver does not log the query string, but an Ingress or a load balancer in front of it records the full URL in its access log, so the secret is in that log. Prefer `basic` for that channel; `query` is for a setup that cannot use it.
- `hmac`: an HMAC-SHA256 signature of the body with a timestamp. The receiver requires the timestamp and accepts it inside a window of 5 minutes. A replay inside the window is accepted, and the key rule bounds it. Of the four vendors only Grafana can send this method. Use it where the sender has it.
- `pubsub`: the IAM policy of the topic. Only the configured publisher can publish to it. For Cloud Monitoring that publisher is the notification service account of the project.

The secret is a Kubernetes Secret that only the receiver mounts. No session, no profile worker and no model gets it. The payload never selects the sender, the profile or the task text. It can name a cluster only inside the install's scope and inside the sender's `clusters` list.

A sender with a shared secret in a header has the strength of a bearer token: a party that reads the token can send alerts. [§9](#9-security) says what that party can do.

### Limits and answers

The receiver owns the retry of a notice that it accepted. A sender's own retry rule is not a base for it: only Datadog documents one, and Cloud Monitoring and Datadog do not repeat a notice by default.

| Case                                                                                        | Answer                      | What the receiver does                                                                                                                                                                                                                               |
| ------------------------------------------------------------------------------------------- | --------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| The secret is wrong, or the sender is unknown                                               | `401`, no detail            | Counts it                                                                                                                                                                                                                                            |
| The body is over the proposed limit of 256 KiB                                              | `413`                       | Counts it                                                                                                                                                                                                                                            |
| The request passed authentication and cannot be parsed                                      | `400`                       | Counts it for that sender                                                                                                                                                                                                                            |
| A run started; a repeat of a run; a fold                                                    | `202`, the body names which | Nothing more                                                                                                                                                                                                                                         |
| A resolved notice                                                                           | `202`                       | Cancels a notice that the queue holds for the same key. Ends the episode of a group. Else it records the notice.                                                                                                                                     |
| The alert names no cluster, or a cluster that is not in the sender's `clusters` list        | `202`                       | Records it. No run.                                                                                                                                                                                                                                  |
| The sender's count for the day is at `maxRunsPerDay`                                        | `202`                       | Records it. No run.                                                                                                                                                                                                                                  |
| The gateway refuses with `rate_limited` or `unavailable`, or does not answer                | `202`                       | Holds the notice in the sender's queue and submits it again, with a longer wait each time, for up to one hour. Then it drops the notice and counts it.                                                                                               |
| The gateway refuses because the target routes to a pinned profile                           | `202`                       | Records "no run: pinned cluster" at once. No hold.                                                                                                                                                                                                   |
| The gateway refuses with another `invalid`, or with `profile_unknown` or `tier_not_allowed` | `202`                       | Holds the notice and submits it again for up to 10 minutes: a new leader of the gateway, or a scope that is not current, gives these answers for a short time. Then it drops the notice and records the reason, because the fault is in the install. |
| The sender's queue is full                                                                  | `503`                       | Counts it. A sender that retries tries again.                                                                                                                                                                                                        |

- A notice that the settle time holds is in the same queue.
- Each sender has its own queue, with a bound on its length.
- The queue is in memory. A restart of the receiver loses the held notices. A sender that repeats covers the loss. For Cloud Monitoring and Datadog the admin sets a re-notify interval, or that incident gets no triage.
- The receiver answers in less than 10 seconds, which is inside the 15 seconds that Datadog gives a request.
- For a Datadog sender, the admin can allow only the addresses that Datadog publishes for its webhooks (`webhooks.prefixes_ipv4` of the site's IP ranges).

## 7. The mapping to the session model

For each firing notice that passes the key rules, the receiver submits one `TriggerSubmit`:

- `trigger.name` is `alert-webhook/<sender>`, `trigger.origin` is `builtin`, and `idempotencyKey` is `alert-webhook/<sender>/<token>`.
- `profile` is `cluster`, with its three required parameters `project`, `location` and `cluster`. The cluster that the alert names selects one entry of the sender's `clusters` list, and that entry gives the three values. The gateway's route check then admits only a target that the install's scope admits.
- `prompt` is a fixed task text of the receiver, and then the normalized alert as one data block. The submission has one text field, so the alert is in the same string as the instruction ([§9](#9-security)). The namespace and the workload are in the data block, because the profile takes no other parameter.
- `delivery.mode` is `chat`. `delivery.jobKey` is one label for each incident (the sender name and a short form of the token), because the delivery groups the posts of one job key and one UTC day into one thread. So an incident has one thread for each UTC day: an incident that crosses midnight UTC, or that gets a follow-up a day later, starts a second thread. A key with no date is a point for the delivery design. A job key is a label of 63 characters at most, which gives the limit of 40 characters for a sender name.

The run is an `Unattended` task: it reads cluster state and logs, and it can only propose. Its result is the triage report.

One case is open: a cluster with a pinned profile. The `cluster` profile yields to the pin, and the ingress refuses a submission whose target routes to another profile than the one that it names. Step 1 covers the clusters with no pin. That refusal does not change with time, so the receiver does not hold such a notice: it records "no run: pinned cluster" at once. The rule for a pin is a point for the owner of the ingress ([§13](#13-open-questions)).

What each step does with a notice:

| Notice                     | Step 1                                                                                                                    | Later                                               |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------- |
| Firing, a new key          | Starts one triage                                                                                                         | The same                                            |
| Firing, a repeat or a fold | No run. The answer names the task.                                                                                        | A follow-up on the conversation of the key (step 3) |
| Resolved                   | Cancels a notice that the queue holds for the same key, and ends the episode of a group. Else it is recorded and counted. | A resolution note on the same conversation (step 3) |

## 8. Order of the work

| Step | What                                                                                                                                                       | Depends on                                                                                                                                                                                                                 |
| ---- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1    | The receiver with the HTTP inlet, the five adapters, the key rules, the identity grant and the kinds. The start of one read-only triage for each incident. | The `cluster` profile is on and takes unattended work (`unattended` in its allowed tiers, and a Job runner), which [`scheduling-on-profiles.md`](scheduling-on-profiles.md) plans. Not the move of the watchers (#6572).   |
| 2    | The result in a chat thread                                                                                                                                | The gateway's delivery of a trigger run's result ([`scheduling-on-profiles.md`](scheduling-on-profiles.md) §4), which is not on `main`. Before it, the result is on the task and on its `trigger:<key>` conversation only. |
| 3    | A follow-up notice and the resolution go to the conversation of the key                                                                                    | A way for a source to reach the conversation of a key that already ran. The ingress has none. The plan of #6572 moves the gating with each source, so the follow-up path is a part of that work.                           |
| 4    | The Pub/Sub inlet                                                                                                                                          | Nothing in the bus. A Terraform module in the form of `drift-pubsub`.                                                                                                                                                      |

Steps 2 and 4 are independent of each other and of step 3. The site page that lists the chat limits says that no generic webhook ingress exists; step 1 changes that line.

### The option that this design does not take

| Option                                                                         | What works today                                                                               | Cost                                                                                                                                                                              |
| ------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| The Session KV server's inject route with a new `kind`, as the stall watch did | The alert post, the thread, the follow-up, the quotas, the ledger                              | One shared bearer token and no identity for each caller. The envelope has the shape of a Kubernetes event, so this is one more kind and one more branch. #6572 retires the route. |
| The trigger ingress (recommended)                                              | The start of a run with its own identity, an idempotent key, a rate cap and the read-only tier | The chat result waits for step 2, and the follow-up for step 3                                                                                                                    |

A door that takes text from outside the install gets an identity of its own and a grant for one subject. The old route cannot give that.

## 9. Security

### A text that asks the agent to act

The alert text is untrusted data for a read-only run. A party that can make an alert (a person with a monitor, a log line in an alert message, a workload that sets a label) can put text in front of the model. Two controls stop a text that asks the agent to act, and they do not depend on the model:

1. **The authority of the run.** The grant of the run has the tier `unattended`. That tier reads cluster state and logs and can only propose. Each child of the run has the same tier or `read` with no trigger. A change to a cluster is refused by the credential path, whatever the model decided.
2. **No pull request.** The ingress submits a trigger run with no change request, and a task gets a write permit only with one. [`unattended-write-permit.md`](unattended-write-permit.md) proposes an opt-in for the run of a built-in schedule. An alert run is not a built-in schedule, and this design asks for no permit.

One mitigation depends on the model: the form of the task. The instruction is a fixed text of the receiver, and the alert is a data block with limits on its size. The block uses the data-only wrapper and the sanitizer that the tree already has for untrusted text (the memory plugin's wrapper for recalled data), with the same tests, and no marker scheme of its own. The model can still follow a text in the block.

The two controls bound what follows. An alert run writes its own report, which can be wrong or misleading, and nothing else unless a later design names it. It cannot change a cluster, it cannot open a pull request, and it cannot write a report stream, because the broker holds that write to the trigger of a schedule. [§12](#12-fixture-and-eval) has a conformance row for each refused write.

From step 2 the report is in a chat thread, and a person can answer it. The report is then in front of a turn that has a person's authority, in the wrapper for an untrusted report that the delivery design names. So an alert text can reach an interactive turn through the report. The person who answers reads the report first; this design adds no control there.

### A party with a sender's secret, or with the power to write alert content

Such a party can start read-only triages, up to the sender's cap and its count for the day. It can also stop a triage of a real incident:

- It sends a notice with the `dedupeId` of a real episode first, where it can predict that name. The real notice then gets the task of the false one.
- It sends a false `target` for a workload, and the fold takes the real alert.
- It uses the sender's cap or its count for the day. Real notices wait in the queue for up to one hour and are then dropped, or they start no run for the rest of the day.
- It sends a resolved notice against a notice that the settle time holds.

Each of these needs the secret of that sender, or the power to write labels in the sender's system. The effect is a missed or late triage for that sender, never a change, and the alert still reaches people on the sender's own channels. A second sender is not affected: it has its own secret, its own key space, its own cap, its own queue and its own fold memory. The counters of [§11](#11-what-a-person-sees-when-it-breaks) show a rise of folds and of notices over a bound.

### Other points

- **Replay.** A method with no signature cannot stop a replay of a captured request. A replay of a firing notice has the key of its episode. It starts no run while the record of that key lives ([§2](#2-what-exists-today)), and after that it starts one run, inside the two bounds. A replay of a resolved notice matters only against a held notice, and from step 3.
- **Which cluster.** The alert can name a cluster, and a party that writes a label can aim a triage at each cluster that the sender can name. The sender's `clusters` list and the install's scope bound that. An alert cannot widen the scope.
- **Disclosure.** An alert text can ask the run to put what it reads into its report: the state and the logs of each cluster that the sender can name. From step 2 the report is in a chat thread, and the members of that channel see it. The sender's `clusters` list is the bound, so a sender gets only the clusters whose alerts it sends.
- **The secrets.** Only the receiver mounts them.
- **The cap.** The cap of the kinds is one value in the gateway's code. Step 1 adds an install setting for it if an install needs another value.

## 10. The border with the read path

The design for external MCP servers (#1989) is the read path to a vendor. This design is the inbound path. They share no code.

- The receiver takes only what the source pushes, and it makes no call to the source. A read of Datadog (monitor details, logs, incidents) is a tool call of a session through the external MCP path, for a caller that the read path admits.
- The two paths hold credentials of different kinds. The receiver holds a secret that verifies the sender. The read path holds the vendor's API credential in the credential proxy.
- The shared names are the four fields of `source` in the normalized alert: `site`, `monitorId`, `incidentId` and `url`. `site` has the form of the Datadog site domain (`datadoghq.com`, `datadoghq.eu`, `us5.datadoghq.com`), which is also the value that the read path configures. A session gives `monitorId` and `incidentId` to the vendor's own tools with no change.
- Which caller gets the read is a decision of the read path's design, not of this one. A triage run of this design has the `unattended` tier and a trigger, and the read path can refuse such a caller. This design does not depend on the read: the triage works from the alert's own content and from cluster state. With no read for the triage run, the read of the source is for the session of a person who follows up.
- Where a session has the read, it works only when the alert's `source.site` is the configured site of the read path, because that path has a credential and an egress rule for one site. This design adds no check of its own.

## 11. What a person sees when it breaks

- **The sender's operator** sees the HTTP answer: `401` for a wrong secret, `400` for a body that the adapter cannot parse, `413` for a body over the limit, `503` when the sender's queue is full.
- **The install admin** sees one counter for each sender and outcome (started, repeat, fold, resolved, held, dropped after the hold, queue full, over the day's count, no cluster, refused, not parsed, over size, not authenticated) and a log line for each notice that started no run, with the reason. A sender whose notices all fail to parse after a vendor changed its payload shows as a rising count for that sender.
- **A person in chat** sees nothing in step 1. From step 2 the triage report is in a thread for the incident and the UTC day.

## 12. Fixture and eval

- **Unit tests:** each adapter against recorded payloads of its sender (firing, repeat, resolved, a second episode); the key rules (the same episode, the fold, a different workload, a critical alert after a warning); each row of the answer table of §6.
- **A replay harness:** it sends the same episode as a firing notice two times and then as a resolved notice, in the Alertmanager, Grafana, Cloud Monitoring and Datadog formats, to a receiver with a stand-in gateway.
- **Conformance rows:** one for each write that an alert run must not make: a change to a cluster, a pull request, and a write to a report stream. Each row shows the refusal.
- **A bench case:** `inbound-alert-webhook-dedup-single-session` (domain `incident-triage`). It replays a burst for one incident against an install and asserts that the trigger `alert-webhook/<sender>` started one task in that time. Each key has its own conversation, so a count inside one conversation cannot fail. From step 3 the case also asserts that the resolved notice reached the conversation of the key.

## 13. Open questions

1. How does a source reach the conversation of a key that already ran, for a follow-up and a resolution? Step 3 follows the answer.
2. How does an event source name a cluster that has a pinned profile?
3. Does the first user's alert path go from the monitor to chat only, or can the monitor also call a webhook? The answer decides which of the two ways of §6 that user needs.
4. Is one triage for an episode right when the first run failed, or when the incident stays open for days? A second run needs a way to see the state of the first one.
5. Does the receiver need a durable state, so that a restart starts no extra triage and loses no held notice?
6. Does the read path of #1989 admit a triage run that a trigger started? Without it, the triage of a Datadog alert reads no Datadog data.
