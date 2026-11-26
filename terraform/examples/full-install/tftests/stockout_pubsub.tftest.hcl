# Stockout Investigator Pub/Sub ordering and identity derivation: the publish
# grant names the derived Logging service agent (or override) rather than
# reading writer_identity off the sink, time_sleep timers enforce apply-side
# identity propagation and destroy-side drain waits, and the sink postcondition
# catches any writer_identity mismatch. Providers mocked and gke_cluster fixed,
# as in storage_size.tftest.hcl.

mock_provider "google" {
  mock_resource "google_service_account" {
    defaults = {
      name   = "projects/tftest-project/serviceAccounts/mock-service-account@tftest-project.iam.gserviceaccount.com"
      email  = "mock-service-account@tftest-project.iam.gserviceaccount.com"
      member = "serviceAccount:mock-service-account@tftest-project.iam.gserviceaccount.com"
    }
  }
  mock_resource "google_logging_project_sink" {
    defaults = {
      writer_identity = "serviceAccount:service-123456789012@gcp-sa-logging.iam.gserviceaccount.com"
    }
  }
}
mock_provider "google-beta" {}
mock_provider "helm" {}
mock_provider "http" {}
mock_provider "time" {}
mock_provider "random" {
  mock_resource "random_password" {
    defaults = { result = "mock-password" }
  }
}
mock_provider "tls" {
  mock_resource "tls_private_key" {
    defaults = { private_key_openssh = "mock-private-key", public_key_openssh = "mock-public-key" }
  }
}

override_module {
  target = module.gke_cluster
  outputs = {
    cluster_endpoint        = "10.0.0.1"
    cluster_endpoint_is_dns = false
    cluster_ca_certificate  = "Y2E="
    cluster_location        = "us-central1"
    cluster_name            = "c"
    network_policy_enforced = true
    workload_identity_pool  = "tftest-project.svc.id.goog"
  }
}

variables {
  project_id                   = "tftest-project"
  cluster_name                 = "c"
  location                     = "us-central1"
  api_server_key               = "k"
  enable_stockout_investigator = true
}

override_data {
  target = data.google_project.stockout[0]
  values = {
    number = "123456789012"
  }
}

# data.google_project.stockout omits depends_on on google_project_service.required
# so Terraform resolves the Logging agent identity at plan time even when APIs
# change. Both plan-time resolution and apply-time postcondition / time_sleep
# checks run below with isolated state_key values.
run "stockout_publish_grant_member_is_known_at_plan_time" {
  command = plan

  assert {
    condition     = google_pubsub_topic_iam_member.stockout_sink_writer[0].member == "serviceAccount:service-123456789012@gcp-sa-logging.iam.gserviceaccount.com"
    error_message = "stockout publish grant member must be resolved at plan time: ${google_pubsub_topic_iam_member.stockout_sink_writer[0].member}"
  }
}

run "stockout_publish_grant_derives_logging_agent_from_project_number" {
  command   = apply
  state_key = "derived_grant"

  assert {
    condition     = google_pubsub_topic_iam_member.stockout_sink_writer[0].member == "serviceAccount:service-123456789012@gcp-sa-logging.iam.gserviceaccount.com"
    error_message = "stockout publish grant must name the project's Logging service agent: ${google_pubsub_topic_iam_member.stockout_sink_writer[0].member}"
  }

  assert {
    condition     = google_pubsub_topic_iam_member.stockout_sink_writer[0].role == "roles/pubsub.publisher"
    error_message = "stockout sink writer needs roles/pubsub.publisher, not ${google_pubsub_topic_iam_member.stockout_sink_writer[0].role}"
  }
}

run "stockout_sink_mismatched_writer_identity_fails_postcondition" {
  command   = apply
  state_key = "mismatched_sink"

  override_resource {
    target = google_logging_project_sink.stockout_alerts[0]
    values = {
      writer_identity = "serviceAccount:p123456789012-77@gcp-sa-logging.iam.gserviceaccount.com"
    }
  }

  expect_failures = [google_logging_project_sink.stockout_alerts[0]]
}

run "stockout_sink_matching_writer_identity_passes_postcondition" {
  command   = apply
  state_key = "matching_sink"

  override_resource {
    target = google_logging_project_sink.stockout_alerts[0]
    values = {
      writer_identity = "serviceAccount:service-123456789012@gcp-sa-logging.iam.gserviceaccount.com"
    }
  }

  override_resource {
    target = google_project_service_identity.stockout_logging[0]
    values = {
      id = "sentinel-stockout-logging-identity-id"
    }
  }

  assert {
    condition     = google_logging_project_sink.stockout_alerts[0].writer_identity == google_pubsub_topic_iam_member.stockout_sink_writer[0].member
    error_message = "stockout sink writer_identity must match the grant member"
  }

  assert {
    condition     = time_sleep.stockout_logging_identity[0].create_duration == "60s" && time_sleep.stockout_logging_identity[0].destroy_duration == null
    error_message = "stockout logging identity wait must delay apply only for 60s"
  }

  assert {
    condition     = try(time_sleep.stockout_logging_identity[0].triggers["logging_service_identity"], null) == "sentinel-stockout-logging-identity-id"
    error_message = "stockout identity wait must trigger on google_project_service_identity.stockout_logging[0].id"
  }

  assert {
    condition     = try(time_sleep.stockout_logging_identity[0].triggers["duration"], null) == "60s"
    error_message = "stockout identity wait must trigger on stockout_pubsub_logging_identity_propagation_duration"
  }

  assert {
    condition     = time_sleep.stockout_sink_drain[0].destroy_duration == "120s" && time_sleep.stockout_sink_drain[0].create_duration == null
    error_message = "stockout sink drain must delay destroy only for 120s"
  }
}

run "stockout_partitioned_project_derives_partition_service_agent" {
  command   = apply
  state_key = "partitioned_project"

  variables {
    project_id = "part0:my-project"
  }

  override_resource {
    target = google_logging_project_sink.stockout_alerts[0]
    values = {
      writer_identity = "serviceAccount:service-123456789012@gcp-sa-logging.part0-system.iam.gserviceaccount.com"
    }
  }

  assert {
    condition     = google_pubsub_topic_iam_member.stockout_sink_writer[0].member == "serviceAccount:service-123456789012@gcp-sa-logging.part0-system.iam.gserviceaccount.com"
    error_message = "partitioned project must use <partition>-system domain: ${google_pubsub_topic_iam_member.stockout_sink_writer[0].member}"
  }
}

run "stockout_domain_scoped_project_uses_standard_service_agent" {
  command   = apply
  state_key = "domain_scoped_project"

  variables {
    project_id = "example.com:my-project"
  }

  assert {
    condition     = google_pubsub_topic_iam_member.stockout_sink_writer[0].member == "serviceAccount:service-123456789012@gcp-sa-logging.iam.gserviceaccount.com"
    error_message = "domain-scoped project must use standard iam.gserviceaccount.com: ${google_pubsub_topic_iam_member.stockout_sink_writer[0].member}"
  }
}

run "stockout_override_moves_grant_and_postcondition_together" {
  command   = apply
  state_key = "override_moves"

  variables {
    stockout_pubsub_sink_writer_identity_override = "serviceAccount:p123456789012-77@gcp-sa-logging.iam.gserviceaccount.com"
  }

  override_resource {
    target = google_logging_project_sink.stockout_alerts[0]
    values = {
      writer_identity = "serviceAccount:p123456789012-77@gcp-sa-logging.iam.gserviceaccount.com"
    }
  }

  assert {
    condition     = google_pubsub_topic_iam_member.stockout_sink_writer[0].member == "serviceAccount:p123456789012-77@gcp-sa-logging.iam.gserviceaccount.com"
    error_message = "override must redirect grant: ${google_pubsub_topic_iam_member.stockout_sink_writer[0].member}"
  }
}

run "stockout_blank_override_falls_back_to_derived_identity" {
  command   = apply
  state_key = "blank_override"

  variables {
    stockout_pubsub_sink_writer_identity_override = ""
  }

  assert {
    condition     = google_pubsub_topic_iam_member.stockout_sink_writer[0].member == "serviceAccount:service-123456789012@gcp-sa-logging.iam.gserviceaccount.com"
    error_message = "blank override must fall back to derived identity: ${google_pubsub_topic_iam_member.stockout_sink_writer[0].member}"
  }
}

run "stockout_override_without_service_account_prefix_is_refused" {
  command = plan

  variables {
    stockout_pubsub_sink_writer_identity_override = "service-123456789012@gcp-sa-logging.iam.gserviceaccount.com"
  }

  expect_failures = [var.stockout_pubsub_sink_writer_identity_override]
}

run "stockout_multi_unit_and_non_duration_strings_are_refused" {
  command = plan

  variables {
    stockout_pubsub_sink_drain_duration                   = "2m30s"
    stockout_pubsub_logging_identity_propagation_duration = "60"
  }

  expect_failures = [
    var.stockout_pubsub_sink_drain_duration,
    var.stockout_pubsub_logging_identity_propagation_duration,
  ]
}
