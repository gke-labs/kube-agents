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
  activity_filter = <<-EOT
    resource.type="k8s_cluster"
    protoPayload.methodName=~"create|patch|update|delete"
  EOT

  # The logName clause names the project the sink sits in, so it is written
  # per sink: the host's here, each source project's below.
  activity_log_name = {
    for project in setunion(local.source_projects, toset([var.project_id])) :
    project => "logName=\"projects/${project}/logs/cloudaudit.googleapis.com%2Factivity\""
  }

  base_filter = "${local.activity_log_name[var.project_id]}\n${trimspace(local.activity_filter)}"

  # Cloud Logging's bound on a sink's name; the source sinks join two names.
  sink_name_max_chars = 100

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

  # The scope's other projects (the section below the host's sink). Never the
  # host, whose sink is the one above, whatever the caller passed.
  source_projects = setsubtract(toset(var.source_projects), toset([var.project_id]))

  # One name for every source sink, the host's sink name with the host project
  # appended. Sink names are unique per project, and a source project may hold
  # a sink of another install's: its own, named sink_name, when it is that
  # install's management project, or another install's source sink when two
  # installs list it in their scopes. The host project is what tells them apart.
  source_sink_name = "${var.sink_name}-${var.project_id}"

  # The source sinks carry the shared clauses and the lease carve-out, and not
  # cluster_names: those are bare names in the host project, and a cluster of
  # the same name elsewhere is another cluster.
  source_sink_filter = {
    for project in local.source_projects :
    project => join("\n", compact([
      local.activity_log_name[project],
      trimspace(local.activity_filter),
      local.lease_filter,
    ]))
  }

  # Each source project's Logging service agent, from its own number, for the
  # same reason as the host's: the grant has to precede the sink. The same
  # override exists per project, for the same dead end.
  source_derived_sink_writer_identity = {
    for project, source in data.google_project.source :
    project => "serviceAccount:service-${source.number}@gcp-sa-logging.iam.gserviceaccount.com"
  }
  source_expected_sink_writer_identity = {
    for project in local.source_projects :
    project => coalesce(lookup(var.source_sink_writer_identity_overrides, project, null), local.source_derived_sink_writer_identity[project])
  }
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

# The scope's other projects.
#
# spec.scope lets one install discover clusters in projects beyond the one it
# runs in (docs/designs/multi-project-scope.md). Admin Activity audit logs are
# per project by construction -- a project-level sink exports its own
# project's entries and nothing else -- so each project the scope lists needs
# a sink of its own, routed into this install's one topic. One topic, one
# subscription and one detector: the detector routes every record by the
# project, location and cluster it names and reads the cluster through the
# Cluster Agent profile the reconcile wrote for it, so what it joins is
# bounded by which projects' records reach the subscription, which is what
# these sinks decide.
#
# Each source project gets the four pieces the host has, in the host's
# order: its Logging service agent minted up front, roles/pubsub.publisher on
# the host topic for that agent, derived from the source project's number so
# the grant precedes the sink, a drain of its own, and the sink last. A drain
# per source project rather than the host's shared one, because a source sink
# is destroyed on its own: a project removed from the scope, or excluded,
# takes its sink and its grant out of the plan while the host's drain stays,
# and a drain that stays waits for nothing.
# Keyed on the project, a drain leaves with the sink it guards: that project's
# sink is deleted, its drain waits, and only then is its grant revoked -- the
# order a shrink needs in the SOURCE project, whose owners the mail would
# otherwise reach. On a full destroy every drain runs, each behind its own
# sink.
#
# A folder's or organisation's members are never among these: the composition
# feeds this list from the declaration and the selectors (kube-agents-iam's
# scope_export_projects), not from the Cloud Asset Inventory listing the
# scoped service account pool uses, whose gaps would churn a sink. A
# container's clusters are discovered at runtime and their audit logs are not
# exported here. An
# aggregated sink on the container would cover them in one piece and is its
# own design: Logging documents no writer-identity form for a folder or
# organisation sink, so its grant could not precede it the way these do, and
# include_children exports every project under the container, the excluded
# ones included.
data "google_project" "source" {
  for_each = local.source_projects

  project_id = each.key
}

# As google_project_service_identity.logging above, per source project: the
# first sink in a project is what would otherwise mint its agent, after the
# grant that has to name it.
resource "google_project_service_identity" "source_logging" {
  for_each = local.source_projects
  provider = google-beta

  project = each.key
  service = "logging.googleapis.com"
}

# As time_sleep.logging_identity above, per source project, for the same race:
# the grant below names an agent Service Usage may not have bound yet, and a
# source project whose agent did not pre-exist fails the apply with "Service
# account ... does not exist" exactly as the host did (#2693). Same duration,
# same two trigger keys, for the same reasons; one per project, so a project
# added to the scope pays its own wait and a re-minted agent re-pays its own.
resource "time_sleep" "source_logging_identity" {
  for_each = local.source_projects

  create_duration = var.logging_identity_propagation_duration

  triggers = {
    logging_service_identity = google_project_service_identity.source_logging[each.key].id
    duration                 = var.logging_identity_propagation_duration
  }
}

# On the host topic, for the source project's agent: the sink in that project
# publishes across projects as that project's identity, so the grant lives
# where the topic does and names an identity from elsewhere. `.id`, as the
# host's grant says. Behind the wait above, as the host's grant is behind its
# wait. The member is a function of data.google_project.source,
# so the replacement the host grant's comment describes -- a caller's
# depends_on deferring the module's data sources, member read as unknown,
# ForceNew -- reaches these too, one per source project, with the window's
# mail going to that project's owners; source_sink_writer_identity_overrides
# pins a member against it as the host's override does.
resource "google_pubsub_topic_iam_member" "source_sink_writer" {
  for_each = local.source_projects

  project = var.project_id
  topic   = google_pubsub_topic.drift_audit.id
  role    = "roles/pubsub.publisher"
  member  = local.source_expected_sink_writer_identity[each.key]

  depends_on = [time_sleep.source_logging_identity]
}

# One per source project, for the reason the section comment gives; the same
# duration as the host's, read from state on destroy like the host's.
resource "time_sleep" "source_sink_drain" {
  for_each = local.source_projects

  destroy_duration = var.sink_drain_duration

  depends_on = [google_pubsub_topic_iam_member.source_sink_writer]
}

resource "google_logging_project_sink" "source_drift_audit" {
  for_each = local.source_projects

  project     = each.key
  name        = local.source_sink_name
  destination = "pubsub.googleapis.com/${google_pubsub_topic.drift_audit.id}"
  filter      = local.source_sink_filter[each.key]

  # The source project's own Logging service agent, as for the host; without
  # it every source sink would publish as the one identity shared across
  # every Google Cloud customer, and the grant above could not be narrower.
  unique_writer_identity = true

  lifecycle {
    # Sink names are capped at 100 characters, and this one is two names
    # joined; a refusal here names both halves where the API would name
    # neither.
    precondition {
      condition     = length(local.source_sink_name) <= local.sink_name_max_chars
      error_message = "the source sinks would be named ${local.source_sink_name} (sink_name with project_id appended), ${length(local.source_sink_name)} characters, over Cloud Logging's ${local.sink_name_max_chars}; shorten sink_name."
    }
    # As the host sink's postcondition: a source sink publishing as anything
    # but the granted identity is live and exporting as an identity with no
    # publish role, and the mail goes to every owner of the SOURCE project.
    postcondition {
      condition     = self.writer_identity == local.source_expected_sink_writer_identity[each.key]
      error_message = "sink ${local.source_sink_name} in ${each.key} publishes as ${self.writer_identity}, not the ${local.source_expected_sink_writer_identity[each.key]} that roles/pubsub.publisher on ${var.topic_name} was granted to, so it cannot write to the topic. The sink already exists and is exporting: this check runs after it is created and does not remove it, so until you resolve this every export fails with topic_permission_denied and Cloud Logging mails every principal holding roles/owner on ${each.key}. To stop that now, delete the sink (gcloud logging sinks delete ${local.source_sink_name} --project=${each.key}) or grant roles/pubsub.publisher on ${var.topic_name} in ${var.project_id} to ${self.writer_identity} by hand -- the hand grant stops the mail but will NOT clear this check. The fix that clears it is source_sink_writer_identity_overrides = { \"${each.key}\" = \"${self.writer_identity}\" }, which moves the grant and this check onto the identity Logging reported; on the full-install composition the variable is drift_pubsub_source_sink_writer_identity_overrides, set as a TF_VAR_ line in install.env through the front doors (they regenerate terraform.tfvars on every run) or in terraform.tfvars for a hand-driven apply. Then open an issue: the module derives the identity from the project number and this project does not follow that form."
    }
  }

  depends_on = [time_sleep.source_sink_drain]
}

// Nobody, on an install: the sinks above are the only publishers -- the
// host's and, on a scoped install, each source project's, each publishing as
// its project's Logging service agent -- which is what makes a record on this
// topic evidence that an API server recorded the call. The boundary that
// draws is per project, not per sink: unique_writer_identity is unique per
// project, so whoever can create a sink in the host project, or in a source
// project, can publish here as that project's agent; the module README's
// section on the source projects says so, since a scope widens that set.
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

resource "google_pubsub_subscription_iam_member" "detector_subscriber" {
  project      = var.project_id
  subscription = google_pubsub_subscription.drift_audit.id
  role         = "roles/pubsub.subscriber"
  member       = "serviceAccount:${var.detector_service_account_email}"
}

# roles/pubsub.subscriber covers consuming messages but not reading the
# subscription's own metadata. It grants subscriptions.consume, snapshots.seek,
# and topics.attachSubscription -- notably not subscriptions.get. A client that
# confirms the subscription exists before pulling (the chat adapter's
# _check_subscription_exists) needs viewer as well, and without it fails with a
# PermissionDenied that reads nothing like a missing grant.
#
# The drift detector now makes one: a startup subscriptions.get reading the
# configured ackDeadlineSeconds, so it can warn when --batch-join-budget would
# hold a batch past it. This grant is what keeps that call from failing. It is
# advisory on the detector's side -- a probe that is denied logs that the budget
# went unchecked and the loop pulls anyway -- so removing viewer degrades the
# warning rather than breaking ingestion. Viewer would stay regardless: `gcloud
# pubsub subscriptions describe` needs it, and that is the first command anyone
# runs against an empty topic.
resource "google_pubsub_subscription_iam_member" "detector_viewer" {
  project      = var.project_id
  subscription = google_pubsub_subscription.drift_audit.id
  role         = "roles/pubsub.viewer"
  member       = "serviceAccount:${var.detector_service_account_email}"
}
