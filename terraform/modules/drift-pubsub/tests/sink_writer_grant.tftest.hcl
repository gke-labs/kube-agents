# The sink's publish grant has to name the Logging service agent from the
# project number rather than read it off the sink, because reading it off the
# sink orders the grant after the sink and leaves Logging exporting to a topic
# it cannot write (#2426).
#
# The ordering itself is what cannot be asserted here: `terraform test` offers
# no way to observe which resource was created first, and none at all to
# observe a destroy-time wait. The resource graph is pinned instead by
# tests/test_drift_pubsub_ordering.py, which reads the depends_on edges out of
# the HCL. What this file pins is everything downstream of that -- the derived
# identity, the override that relaxes it, the postcondition that guards it, and
# the shape of each of the two waits, one apply-side and one destroy-side --
# all of which a later simplification would undo quietly.
#
# The providers are mocked, so no project is read and nothing is granted.

mock_provider "google" {}
mock_provider "google-beta" {}
mock_provider "time" {}

variables {
  project_id = "drift-project-1"
}

override_data {
  target = data.google_project.this
  values = {
    number = "123456789012"
  }
}

run "the_publish_grant_names_the_logging_service_agent_from_the_project_number" {
  command = plan

  assert {
    condition     = google_pubsub_topic_iam_member.sink_writer.member == "serviceAccount:service-123456789012@gcp-sa-logging.iam.gserviceaccount.com"
    error_message = "the sink's publish grant must name the project's Logging service agent, derived from the project number so it can precede the sink: ${google_pubsub_topic_iam_member.sink_writer.member}"
  }

  assert {
    condition     = google_pubsub_topic_iam_member.sink_writer.role == "roles/pubsub.publisher"
    error_message = "the sink writer needs roles/pubsub.publisher, not ${google_pubsub_topic_iam_member.sink_writer.role}"
  }
}

# A sink that publishes as something other than the granted identity is the
# silently-inert pipeline the module warns about, so the sink carries a
# postcondition comparing the two. Reaching it needs a known writer_identity,
# which only override_resource can supply under a mocked provider.
#
# These stay on `plan`. An `apply` run leaves its state behind for the runs
# after it, so the sink the first one creates is not recreated by the next,
# whose override_resource is then silently ignored -- the failing run passes
# and the suite reports a guard it never reached.
run "a_sink_publishing_as_the_granted_identity_passes_the_postcondition" {
  command = plan

  override_resource {
    target          = google_logging_project_sink.drift_audit
    override_during = plan
    values = {
      writer_identity = "serviceAccount:service-123456789012@gcp-sa-logging.iam.gserviceaccount.com"
    }
  }

  assert {
    condition     = google_logging_project_sink.drift_audit.writer_identity == google_pubsub_topic_iam_member.sink_writer.member
    error_message = "the sink must publish as the identity the grant names, or the pipeline is inert"
  }
}

run "a_sink_publishing_as_anything_else_fails_the_postcondition" {
  command = plan

  override_resource {
    target          = google_logging_project_sink.drift_audit
    override_during = plan
    values = {
      writer_identity = "serviceAccount:p123456789012-77@gcp-sa-logging.iam.gserviceaccount.com"
    }
  }

  expect_failures = [google_logging_project_sink.drift_audit]
}

# The way out of the failure above: the override moves the grant and the
# postcondition together, so the same sink now applies.
run "the_override_moves_the_grant_and_the_postcondition_together" {
  command = plan

  variables {
    sink_writer_identity_override = "serviceAccount:p123456789012-77@gcp-sa-logging.iam.gserviceaccount.com"
  }

  override_resource {
    target          = google_logging_project_sink.drift_audit
    override_during = plan
    values = {
      writer_identity = "serviceAccount:p123456789012-77@gcp-sa-logging.iam.gserviceaccount.com"
    }
  }

  assert {
    condition     = google_pubsub_topic_iam_member.sink_writer.member == "serviceAccount:p123456789012-77@gcp-sa-logging.iam.gserviceaccount.com"
    error_message = "the override must redirect the grant, not just relax the check: ${google_pubsub_topic_iam_member.sink_writer.member}"
  }
}

run "an_override_without_the_serviceAccount_prefix_is_refused" {
  command = plan

  variables {
    sink_writer_identity_override = "service-123456789012@gcp-sa-logging.iam.gserviceaccount.com"
  }

  expect_failures = [var.sink_writer_identity_override]
}

# "" is how the override gets switched off again. The composition reaches this
# variable through a TF_VAR_ line in install.env, and an operator who blanks
# that line rather than deleting it exports "" -- so "" has to mean "no
# override" and land back on the derived identity. Refusing it would answer
# someone turning the override off by telling them to add a prefix to it.
run "a_blanked_override_falls_back_to_the_derived_identity" {
  command = plan

  variables {
    sink_writer_identity_override = ""
  }

  assert {
    condition     = google_pubsub_topic_iam_member.sink_writer.member == "serviceAccount:service-123456789012@gcp-sa-logging.iam.gserviceaccount.com"
    error_message = "a blanked override must switch the override off, not shift the grant to \"\": ${google_pubsub_topic_iam_member.sink_writer.member}"
  }
}

# The mirror of the drain: this one is paid on apply and never on destroy, so
# the two durations must not be "simplified" onto one resource. Waiting for the
# Logging agent at destroy time delays a teardown for nothing; waiting for the
# Log Router at apply time does not help the grant that races it (#2693).
run "the_identity_wait_delays_only_the_apply" {
  command = plan

  assert {
    condition     = time_sleep.logging_identity.create_duration == "60s"
    error_message = "the Logging-agent wait's default must be the apply-side wait: ${time_sleep.logging_identity.create_duration}"
  }

  assert {
    condition     = time_sleep.logging_identity.destroy_duration == null
    error_message = "the Logging-agent wait must not delay a destroy; only create_duration is set"
  }
}

# The two trigger keys are the whole of what makes the wait re-payable, and
# nothing else here would notice their loss: a sleep keyed on nothing is still
# created, still waits its 60s on the apply that creates it, and never waits
# again. Drop the identity key and a project_id change replaces the identity
# and the grant and races them exactly as before. Drop the duration key and
# raising the value -- the remedy both READMEs and both variable descriptions
# offer a project that still loses the race -- becomes an in-place update that
# runs no delay, so the re-apply retries the grant with no wait at all.
#
# The identity key also carries the ordering, which is why the wait declares no
# depends_on and tests/test_drift_pubsub_ordering.py does not list that link.
#
# The identity's id is unknown under a mocked google-beta, so override_resource
# supplies one -- and supplies a sentinel rather than the real
# projects/<project>/services/<service> shape on purpose. With the real shape
# the assertion passes for any expression that evaluates to it, including a
# hand-built "projects/${var.project_id}/services/logging.googleapis.com" that
# reads nothing off the resource: measured, that rewrite kept all 17 runs and
# both ordering tests green while deleting the only thing that orders the wait
# after the mint. A value the configuration cannot construct can only arrive
# here by reference, so this asserts the reference and not a string.
run "the_identity_wait_is_keyed_on_the_identity_and_the_duration" {
  command = plan

  override_resource {
    target          = google_project_service_identity.logging
    override_during = plan
    values = {
      id = "sentinel-only-the-resource-can-supply-this"
    }
  }

  # try(), because the whole point is a deleted block: indexing a null triggers
  # map is an evaluation error, which halts the file and reports "Attempt to
  # index null value" instead of the message below -- and skips every run after
  # this one. try() turns the deletion into an ordinary failed assertion.
  assert {
    condition     = try(time_sleep.logging_identity.triggers["logging_service_identity"], null) == "sentinel-only-the-resource-can-supply-this"
    error_message = "the Logging-agent wait must read google_project_service_identity.logging.id itself, not a string that happens to match its format: the reference is what orders the wait after the mint and what re-pays it on a re-mint, and only a reference can carry the sentinel this run overrides the identity with"
  }

  assert {
    condition     = try(time_sleep.logging_identity.triggers["duration"], null) == "60s"
    error_message = "the Logging-agent wait must be keyed on its own duration, or raising logging_identity_propagation_duration is an in-place update that runs no delay and the documented remedy for a project that still races does nothing"
  }
}

run "a_propagation_duration_that_is_not_a_duration_is_refused" {
  command = plan

  variables {
    logging_identity_propagation_duration = "60"
  }

  expect_failures = [var.logging_identity_propagation_duration]
}

# Both pinned for the reason the drain's own pair is pinned below: time_sleep
# refuses the multi-unit form and the sub-millisecond units, so a validation
# widened to accept either only moves the failure inside the provider, where
# the message does not name the variable. "1m30s" is the form this variable's
# own error message names, which makes it the one most likely to be let
# through by someone correcting the regex to match the message.
run "a_multi_unit_propagation_duration_is_refused" {
  command = plan

  variables {
    logging_identity_propagation_duration = "1m30s"
  }

  expect_failures = [var.logging_identity_propagation_duration]
}

run "a_sub_millisecond_propagation_duration_is_refused" {
  command = plan

  variables {
    logging_identity_propagation_duration = "500us"
  }

  expect_failures = [var.logging_identity_propagation_duration]
}

run "a_minutes_propagation_duration_reaches_the_identity_wait" {
  command = plan

  variables {
    logging_identity_propagation_duration = "2m"
  }

  assert {
    condition     = time_sleep.logging_identity.create_duration == "2m"
    error_message = "an accepted duration must reach time_sleep unchanged: ${time_sleep.logging_identity.create_duration}"
  }
}

# Paid once per destroy and never on apply, so it has to stay a destroy_duration.
run "the_drain_waits_only_on_destroy" {
  command = plan

  assert {
    condition     = time_sleep.sink_drain.destroy_duration == "120s"
    error_message = "the drain's default must be the destroy-side wait: ${time_sleep.sink_drain.destroy_duration}"
  }

  assert {
    condition     = time_sleep.sink_drain.create_duration == null
    error_message = "the drain must not delay an apply; only destroy_duration is set"
  }
}

run "a_drain_duration_that_is_not_a_duration_is_refused" {
  command = plan

  variables {
    sink_drain_duration = "120"
  }

  expect_failures = [var.sink_drain_duration]
}

# The validation is narrower than a Go duration because time_sleep is: it
# refuses "2m30s" and the sub-millisecond units. Letting either through the
# variable only moves the same failure inside the provider, where the message
# does not name the variable that caused it. Both forms are pinned so the
# validation is not "corrected" to accept what the provider will not.
run "a_multi_unit_drain_duration_is_refused_because_time_sleep_refuses_it" {
  command = plan

  variables {
    sink_drain_duration = "2m30s"
  }

  expect_failures = [var.sink_drain_duration]
}

run "a_sub_millisecond_drain_duration_is_refused" {
  command = plan

  variables {
    sink_drain_duration = "500us"
  }

  expect_failures = [var.sink_drain_duration]
}

run "a_minutes_drain_duration_reaches_the_drain" {
  command = plan

  variables {
    sink_drain_duration = "5m"
  }

  assert {
    condition     = time_sleep.sink_drain.destroy_duration == "5m"
    error_message = "an accepted duration must reach time_sleep unchanged: ${time_sleep.sink_drain.destroy_duration}"
  }
}
