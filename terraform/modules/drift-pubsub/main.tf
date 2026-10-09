# Cloud Logging -> Pub/Sub delivery path for the drift detector.
#
# GKE audit logs cannot be read from the Kubernetes API. The control plane is
# managed, so the API server's audit backend is not ours to configure, and the
# stream surfaces only in Cloud Logging. This module builds the route out of
# Cloud Logging and into a subscription the detector pulls from.
#
# Why this sink filters what it does -- and deliberately does not filter more --
# is recorded at each decision below, next to the code it explains.

locals {
  # Mutating calls against GKE clusters, from the Admin Activity audit log.
  # Cloud Logging ANDs newline-separated expressions.
  #
  # Principals are broadly NOT filtered here. The detector classifies them
  # itself and needs the unfiltered volume visible to measure its own noise
  # profile; filtering in the sink would discard the denominators and make a
  # mistuned automation allowlist impossible to debug. The lease carve-out
  # below is the one deliberate exception.
  base_filter = <<-EOT
    logName="projects/${var.project_id}/logs/cloudaudit.googleapis.com%2Factivity"
    resource.type="k8s_cluster"
    protoPayload.methodName=~"create|patch|update|delete"
  EOT

  # Leader-election and node-heartbeat Leases dominate this stream and carry no
  # drift signal. A Lease is created at runtime by the controller that holds it,
  # never applied from a manifest, so there is no Git-side object for it to
  # diverge from. Measured over a 15-minute window on a two-cluster project,
  # leases.update plus leases.create were 9,558 of 10,000 sampled mutating
  # calls -- 95.6%.
  #
  # That 10,000 is the query's row cap, not the window's true total, so daily
  # figures do not come from it. They come from a separate untruncated count of
  # the surviving stream: 623 non-lease calls per 15 minutes, about 60k/day,
  # against roughly 1.35M/day unfiltered.
  #
  # The exclusion is scoped by principal rather than dropping leases outright,
  # so that a person running `kubectl patch lease` still reaches the detector.
  # That is not GitOps drift (nothing declared it), but it can knock an active
  # controller off its lock, and silently discarding it is hard to defend.
  #
  # Both principal clauses are load-bearing. "^system:" alone leaves the GKE
  # service agent behind: in the same sample container-engine-robot accounted
  # for 287 lease writes, which would have inflated the surviving stream by 65%.
  # Matching any *.iam.gserviceaccount.com covers it and every future service
  # agent that carries the "iam" label. It does not cover the Google-managed
  # accounts, which do not: the Compute Engine default is
  # "<number>-compute@developer.gserviceaccount.com", and Cloud Build, App
  # Engine and the cloudservices agent use "@cloudbuild.", "@appspot." and
  # "@cloudservices." respectively. Their lease writes therefore survive this
  # exclusion and reach the topic. That costs volume and nothing else -- the
  # detector's own classifier matches the whole ".gserviceaccount.com" domain
  # (gcpServiceAccountSuffix in k8s-operator/cmd/drift-detector/classify.go)
  # and drops them as automation. Widening the suffix here would cut delivered
  # volume; it is left alone deliberately, because changing a sink filter
  # changes what a deployed install receives and belongs in its own change.
  #
  # Kept to a single line on purpose: Cloud Logging treats a newline as an
  # implicit AND, which would break the OR grouping if this were wrapped.
  machine_lease_exclusion = <<-EOT
    NOT (protoPayload.methodName=~"coordination\.v1\.leases" AND (protoPayload.authenticationInfo.principalEmail=~"^system:" OR protoPayload.authenticationInfo.principalEmail=~"\.iam\.gserviceaccount\.com$"))
  EOT

  lease_filter = var.exclude_machine_lease_heartbeats ? trimspace(local.machine_lease_exclusion) : ""

  # An empty cluster_names means every cluster in the project: one sink for the
  # fleet, with the detector routing on resource.labels.cluster_name the way the
  # event watcher already routes on its own per-cluster identity.
  cluster_list   = join(" OR ", [for name in var.cluster_names : "\"${name}\""])
  cluster_filter = length(var.cluster_names) > 0 ? "resource.labels.cluster_name=(${local.cluster_list})" : ""

  sink_filter = join("\n", compact([
    trimspace(local.base_filter),
    local.lease_filter,
    local.cluster_filter,
  ]))

  # The identity the sink below will publish as, named before the sink exists
  # so the grant can precede it. unique_writer_identity makes this the
  # project's Logging service agent, one per project rather than one per sink,
  # so it is derivable from the project number alone.
  #
  # The sink's postcondition checks this against what Logging actually returns,
  # because a wrong guess here is the silently-inert pipeline the grant's own
  # comment describes.
  derived_sink_writer_identity = "serviceAccount:service-${data.google_project.this.number}@gcp-sa-logging.iam.gserviceaccount.com"

  # A project where Logging reports some other identity would fail that
  # postcondition on every plan from then on, taking the whole composition with
  # it and leaving no way out but editing this module. The override is that way
  # out: it moves the grant and the postcondition together, so setting it to
  # what Logging reported makes the apply correct rather than merely quiet.
  expected_sink_writer_identity = coalesce(var.sink_writer_identity_override, local.derived_sink_writer_identity)
}

data "google_project" "this" {
  project_id = var.project_id
}

# Creating the first sink in a project materialises the Logging service agent
# as a side effect, which is too late: the grant below has to name the agent
# before the sink is created, and IAM will not bind a service account that does
# not exist. This asks Service Usage for it up front. A project that has ever
# held a sink already has one and this is a no-op there.
#
# Do not delete this as dead weight. generateServiceIdentity returns an *empty*
# identity for logging.googleapis.com -- no email, which is why the provider had
# to be taught not to error on it -- so it looks like it achieves nothing. The
# minting is a server-side side effect of the call, not something the response
# reports. Measured on a project with the API enabled and no sink ever created:
# granting roles/pubsub.publisher to service-<number>@gcp-sa-logging fails with
# "Service account ... does not exist", the same grant succeeds immediately
# after this call, and the call still returns empty. Enabling the API is not
# what creates the agent.
resource "google_project_service_identity" "logging" {
  provider = google-beta

  project = var.project_id
  service = "logging.googleapis.com"
}

# Minting the agent and being able to bind it are not the same moment. The call
# above returns as soon as Service Usage has accepted the request, and on a
# project that did not already have the agent, IAM will not resolve the account
# for some seconds after that. With only a depends_on edge between the call and
# the grant below, the grant is what ran into that:
#
#   Error applying IAM policy for pubsub topic ".../platform-agent-drift-audit":
#   googleapi: Error 400: Service account
#   service-<n>@gcp-sa-logging.iam.gserviceaccount.com does not exist.
#
# Six of 32 projects in the #2424 backfill sweep -- about one in five, and
# exactly the projects whose agent did not pre-exist (#2693). Not a backfill
# problem: a fresh install on a project with no Logging agent hits the same
# ordering. The apply stops there, leaving the topic created and both the grant
# and the sink absent, so the pipeline that results is not the silently-inert
# one the grant's own comment describes -- nothing is exporting at all -- but it
# does take a second run by hand to finish.
#
# There is nothing to wait on: IAM reports a missing account and a
# not-yet-propagated one identically, which is why this is the same kind of
# timer as sink_drain below rather than a poll. Paid on the first apply that
# carries this resource -- a new install pays it on its first apply of the
# module, and an install that already had the ingress pays it on the next
# apply of any kind, because the wait is new to its state even though the
# identity is not -- and after that only when one of the triggers below moves,
# which is a re-minted identity or a changed duration.
# A project whose agent already existed gains nothing from it and cannot be
# told apart from one that needs it.
resource "time_sleep" "logging_identity" {
  create_duration = var.logging_identity_propagation_duration

  # Both keys are load-bearing, and the ordering is a side effect of the first
  # rather than the reason for it: referencing the identity is what puts this
  # after the mint, so there is no depends_on here and adding one would say
  # nothing the reference does not.
  #
  # logging_service_identity re-pays the wait when the identity is re-minted,
  # which an ordering edge alone would not do -- depends_on orders, it does not
  # propagate replacement. Changing project_id is the case: project is ForceNew
  # on the identity and member is ForceNew on the grant, so both are replaced
  # and the race is live again, while a sleep keyed on nothing sits in state
  # contributing no delay.
  #
  # duration is what makes "raise it and re-apply" work, which is the remedy
  # this module's variable, the composition's, and both READMEs all offer for a
  # project that still loses the race. Without it they promise something the
  # resource does not do. Measured against the time provider: raising
  # create_duration from 5s to 60s on an existing time_sleep plans as "updated
  # in-place" and applies in 1s -- "Modifications complete after 0s", no delay
  # at all -- where the same raise with a moved trigger forces replacement and
  # takes the full 60s. The sleep is always already in state by the time a
  # grant can fail, since the grant depends on it, so without this key the
  # re-apply retries the grant with no wait and succeeds or fails on whatever
  # wall-clock passed between the two runs. The cost is that lowering the value
  # also re-pays the wait once, at the new lower figure.
  triggers = {
    logging_service_identity = google_project_service_identity.logging.id
    duration                 = var.logging_identity_propagation_duration
  }
}

resource "google_pubsub_topic" "drift_audit" {
  #checkov:skip=CKV_GCP_83:Drift audit topic uses default Google-managed encryption keys
  project = var.project_id
  name    = var.topic_name
}

resource "google_pubsub_subscription" "drift_audit" {
  project = var.project_id
  name    = var.subscription_name
  topic   = google_pubsub_topic.drift_audit.id

  ack_deadline_seconds       = var.ack_deadline_seconds
  message_retention_duration = var.message_retention_duration

  # Pub/Sub deletes a subscription after 31 days without pull activity. That is
  # harmless while the detector runs and quietly destructive when it does not:
  # a paused rollout, or a topic provisioned ahead of the consumer that reads
  # from it, should not take the subscription with it.
  expiration_policy {
    ttl = ""
  }

  # The detector nacks what it cannot parse, so a payload-shape change from GCP
  # is loud rather than silently acked away. Backoff keeps that from becoming a
  # hot redelivery loop.
  #
  # There is deliberately no dead_letter_policy. Without message ordering a pull
  # subscription has no head-of-line blocking, so an unparseable message cannot
  # stall the pipeline; it redelivers on its own backoff until retention expires
  # while everything else flows past. A dead-letter topic would make that message
  # inspectable, at the cost of two further IAM grants (the Pub/Sub service agent
  # needs publisher on the dead-letter topic and subscriber here) that render the
  # policy silently inert when missed. Revisit if the detector's parse-failure
  # counter ever moves.
  retry_policy {
    minimum_backoff = var.retry_minimum_backoff
    maximum_backoff = var.retry_maximum_backoff
  }
}

# IMPORTANT: without this grant the sink is silently inert. Log Router surfaces
# no error, the topic receives nothing, and the only trace is an export-error
# metric nobody is watching. It is the most likely reason a freshly applied
# drift pipeline delivers zero messages, and it looks identical to "no drift
# happened" from the consumer's side.
#
# This grant used to read google_logging_project_sink.drift_audit.writer_identity,
# which ordered it *after* the sink: Logging starts routing the moment the sink
# exists, so every apply had a window -- measured at 0.7s in the audit log on
# #2426 -- where the sink published to a topic it could not write, and Logging
# mailed every project owner an "[ACTION REQUIRED] Cloud Logging sink
# configuration error" with topic_permission_denied. Naming the identity from
# the project number instead inverts the order to grant-then-sink and closes
# the window. local.expected_sink_writer_identity carries the "serviceAccount:" prefix,
# as writer_identity did.
#
# What deriving it costs, which reading it off the sink did not: the member is
# now a function of a data source, so a caller that defers that read defers the
# member too. full-install calls this module with
# depends_on = [google_project_service.required], and a module-level depends_on
# defers every data source in the module whenever a target has a planned
# change. Measured on a real project: with the grant already in state, adding
# one unrelated API to that for_each set plans
#
#   data.google_project.this will be read during apply
#   ~ member = "serviceAccount:service-<n>@gcp-sa-logging..." -> (known after
#     apply) # forces replacement
#
# -- member is ForceNew, so the binding is destroyed and recreated while the
# sink stays live, which is this file's own window reopened on another trigger.
# Narrowing the caller's depends_on to the three APIs this module needs does
# not help; Terraform resolves an indexed depends_on reference to the whole
# resource, and the same plan results. The fix is a plan-time-known project
# number passed in from the caller, which is #2487; until then setting
# sink_writer_identity_override pins the member and sidesteps it.
#
# `.id`, never `.name`. A replaced topic or subscription loses its whole IAM
# policy in GCP, and a binding keyed on the plan-time-known `.name` is left out
# of the plan that replaced it — green apply, empty policy. chat-pubsub's
# main.tf carries the full account of that failure.
resource "google_pubsub_topic_iam_member" "sink_writer" {
  project = var.project_id
  topic   = google_pubsub_topic.drift_audit.id
  role    = "roles/pubsub.publisher"
  member  = local.expected_sink_writer_identity

  # time_sleep.logging_identity, not google_project_service_identity.logging:
  # the identity resource returning is not the point at which IAM will bind
  # what it minted. The comment above that wait has the measurement.
  depends_on = [time_sleep.logging_identity]
}

# Deleting a sink does not stop the export at the same instant. The Log Router
# holds the sink's configuration on a fleet that converges over minutes, and
# the audit log on #2426 has Terraform deleting the sink and the topic 0.6s
# apart -- so the router kept exporting to a topic that was already gone and
# Logging mailed every project owner a topic_not_found error, on every destroy.
#
# Terraform has no way to wait for that convergence, and Logging exposes no
# signal to wait on, so this is a timer. It sits between the sink and both the
# grant and the topic: created after the grant and before the sink, it is
# destroyed after the sink and before them, which is the order the Log Router
# needs. Create costs nothing (no create_duration), and the whole delay is
# paid once per destroy.
#
# Keeping the grant on the far side of the wait matters as much as the topic
# does. Revoking publish while the router is still exporting trades
# topic_not_found for topic_permission_denied -- the same email.
resource "time_sleep" "sink_drain" {
  destroy_duration = var.sink_drain_duration

  depends_on = [google_pubsub_topic_iam_member.sink_writer]
}

resource "google_logging_project_sink" "drift_audit" {
  project     = var.project_id
  name        = var.sink_name
  destination = "pubsub.googleapis.com/${google_pubsub_topic.drift_audit.id}"
  filter      = local.sink_filter

  # Without this the sink publishes as cloud-logs@system.gserviceaccount.com,
  # an identity shared across every Google Cloud customer. With it the sink
  # publishes as this project's own logging service agent,
  # service-<project-number>@gcp-sa-logging.iam.gserviceaccount.com, so the
  # grant above admits only sinks belonging to this project.
  #
  # "Unique" means unique per project, not per sink: every sink here with this
  # flag set shares the identity, so the grant is not narrower than the project.
  unique_writer_identity = true

  # The grant above names this identity rather than reading it from here, so
  # nothing else would notice a project where the guess is wrong -- the apply
  # would go green over a sink that cannot publish, which is the failure the
  # grant's comment calls the hardest one to see. Checking it here turns that
  # into a failed apply naming both identities.
  #
  # A postcondition is evaluated after the resource is created and does not
  # roll it back, so when this fails the sink is live in GCP and exporting as
  # an identity that holds no publisher role -- the owner-wide mail this
  # module exists to prevent, now continuous rather than a sub-second window.
  # The message has to say so and give the operator a way to stop it now,
  # because an override they cannot apply immediately leaves them reading
  # this text while the mail goes out.
  lifecycle {
    postcondition {
      condition     = self.writer_identity == local.expected_sink_writer_identity
      error_message = "sink ${var.sink_name} publishes as ${self.writer_identity}, not the ${local.expected_sink_writer_identity} that roles/pubsub.publisher was granted to, so it cannot write to topic ${var.topic_name}. The sink already exists and is exporting: this check runs after it is created and does not remove it, so until you resolve this every export fails with topic_permission_denied and Cloud Logging mails an \"[ACTION REQUIRED] sink configuration error\" to every principal holding roles/owner on ${var.project_id}. To stop that now, either delete the sink (gcloud logging sinks delete ${var.sink_name} --project=${var.project_id}) or grant roles/pubsub.publisher on ${var.topic_name} to ${self.writer_identity} by hand -- the hand grant stops the mail but will NOT clear this check, which compares identities rather than grants. The fix that clears it is sink_writer_identity_override = \"${self.writer_identity}\", which moves the grant and this check onto the identity Logging reported. On the full-install composition the variable is drift_pubsub_sink_writer_identity_override, and where you set it decides whether it survives: through the install.sh / upgrade.sh front doors it is a passthrough line in install.env, TF_VAR_drift_pubsub_sink_writer_identity_override=\"${self.writer_identity}\", because those regenerate terraform.tfvars wholesale on every run and a key added there by hand is gone on the next one -- which brings this failure and the mail back. A hand-driven apply sets it in terraform.tfvars instead. Then open an issue: the module derives the identity from the project number and this project does not follow that form."
    }
  }

  depends_on = [time_sleep.sink_drain]
}

// Nobody, on an install: the sink above is the only publisher, which is what
// makes a record on this topic evidence that the API server recorded the call.
// The evaluation pool is the exception and the variable's description says why
// the exception is confined to it. Topic-scoped and for_each'd over the
// members, so a project that sets it grants publish on this one topic and the
// list is the whole of what holds it.
//
// `.id` rather than `.name`, for the reason the sink_writer binding above
// gives.
resource "google_pubsub_topic_iam_member" "extra_publishers" {
  for_each = toset(var.topic_publishers)

  project = var.project_id
  topic   = google_pubsub_topic.drift_audit.id
  role    = "roles/pubsub.publisher"
  member  = each.value
}

# The detector's own subscriber and viewer grants are deliberately NOT here,
# and this module no longer takes the service account that would need them.
#
# They are the only thing that made this module depend on whoever creates that
# GSA, and a caller composing the two gets that dependency transitively: in
# full-install, kube-agents-iam depends on the cluster, so this module did too.
# Terraform destroys dependents before dependencies, which put the whole
# ingress -- topic included -- ahead of the cluster on a destroy, while the
# control plane went on emitting matching audit records for minutes after the
# topic was gone. That is #2426's destroy half, and no drain can close it,
# because the thing being waited for is a teardown whose length varies with the
# cluster.
#
# So the grants belong to the caller, downstream of its own GSA, which lets the
# caller order the cluster ahead of this module instead. The split is also the
# honest one: the module does not create the detector's service account (see
# the README's "What this module does not do"), and granting on an identity it
# does not own was the odd half of that. full-install's main.tf makes both
# grants next to the module call, with `moved` blocks so an existing install
# sees no change; `subscription_id` is the output they bind to.
