# How the Standard cluster's two node pools are sized. The default, and the
# "medium" profile with it, render the shape the module produced before
# sizing_profile existed, so an existing caller plans no change; "fleet"
# renders the fleet-tier shapes; an explicit machine type or disk value wins
# over the profile; "" on a string input means unset; and the validations
# refuse an unknown profile, a disk under GKE's 10 GB floor, an unknown disk
# type, and an autoscaling floor above the ceiling. Providers mocked, plan
# only: nothing here reaches a cloud.

mock_provider "google" {}
mock_provider "google-beta" {}

variables {
  project_id                 = "tftest-project"
  cluster_name               = "c"
  location                   = "us-central1"
  cluster_mode               = "standard"
  enable_gvisor_node_pool    = true
  enable_database_encryption = false
}

run "the_default_renders_the_small_shape_on_both_pools" {
  command = plan

  assert {
    condition     = google_container_cluster.standard[0].node_config[0].machine_type == "e2-standard-4"
    error_message = "the default pool's machine type must stay e2-standard-4 at the defaults: ${google_container_cluster.standard[0].node_config[0].machine_type}"
  }

  assert {
    condition     = google_container_cluster.standard[0].node_config[0].disk_size_gb == 100 && google_container_cluster.standard[0].node_config[0].disk_type == "pd-balanced"
    error_message = "the default pool's boot disk must stay 100 GB pd-balanced, GKE's own default, at the defaults: ${google_container_cluster.standard[0].node_config[0].disk_size_gb} ${google_container_cluster.standard[0].node_config[0].disk_type}"
  }

  assert {
    condition     = google_container_node_pool.gvisor[0].node_config[0].machine_type == "e2-standard-4"
    error_message = "the gVisor pool's machine type must stay e2-standard-4 at the defaults: ${google_container_node_pool.gvisor[0].node_config[0].machine_type}"
  }

  assert {
    condition     = google_container_node_pool.gvisor[0].node_config[0].disk_size_gb == 100 && google_container_node_pool.gvisor[0].node_config[0].disk_type == "pd-balanced"
    error_message = "the gVisor pool's boot disk must stay 100 GB pd-balanced at the defaults: ${google_container_node_pool.gvisor[0].node_config[0].disk_size_gb} ${google_container_node_pool.gvisor[0].node_config[0].disk_type}"
  }

  assert {
    condition     = google_container_node_pool.gvisor[0].initial_node_count == 1 && google_container_node_pool.gvisor[0].autoscaling[0].min_node_count == 1 && google_container_node_pool.gvisor[0].autoscaling[0].max_node_count == 2
    error_message = "the gVisor pool must start at one node per zone and autoscale between 1 and 2 at the defaults"
  }

  assert {
    condition     = output.sizing_profile == "small" && output.standard_machine_type == "e2-standard-4" && output.standard_disk_size_gb == 100 && output.standard_disk_type == "pd-balanced" && output.gvisor_machine_type == "e2-standard-4" && output.gvisor_disk_size_gb == 100 && output.gvisor_disk_type == "pd-balanced"
    error_message = "the resolved-shape outputs must report the small shape at the defaults"
  }
}

run "medium_renders_like_small_until_it_has_a_shape_of_its_own" {
  command = plan

  variables {
    sizing_profile = "medium"
  }

  assert {
    condition     = google_container_cluster.standard[0].node_config[0].machine_type == "e2-standard-4" && google_container_cluster.standard[0].node_config[0].disk_size_gb == 100
    error_message = "medium must render the default pool as small does: ${google_container_cluster.standard[0].node_config[0].machine_type} / ${google_container_cluster.standard[0].node_config[0].disk_size_gb}"
  }

  assert {
    condition     = google_container_node_pool.gvisor[0].node_config[0].machine_type == "e2-standard-4" && google_container_node_pool.gvisor[0].node_config[0].disk_size_gb == 100
    error_message = "medium must render the gVisor pool as small does: ${google_container_node_pool.gvisor[0].node_config[0].machine_type} / ${google_container_node_pool.gvisor[0].node_config[0].disk_size_gb}"
  }

  assert {
    condition     = output.sizing_profile == "medium"
    error_message = "the sizing_profile output must report the profile asked for"
  }
}

run "fleet_renders_the_fleet_shapes" {
  command = plan

  variables {
    sizing_profile = "fleet"
  }

  assert {
    condition     = google_container_cluster.standard[0].node_config[0].machine_type == "n2-standard-8" && google_container_cluster.standard[0].node_config[0].disk_size_gb == 200 && google_container_cluster.standard[0].node_config[0].disk_type == "pd-balanced"
    error_message = "fleet must render the default pool as n2-standard-8 with a 200 GB pd-balanced disk: ${google_container_cluster.standard[0].node_config[0].machine_type} / ${google_container_cluster.standard[0].node_config[0].disk_size_gb} / ${google_container_cluster.standard[0].node_config[0].disk_type}"
  }

  assert {
    condition     = google_container_node_pool.gvisor[0].node_config[0].machine_type == "n2-standard-4" && google_container_node_pool.gvisor[0].node_config[0].disk_size_gb == 150 && google_container_node_pool.gvisor[0].node_config[0].disk_type == "pd-balanced"
    error_message = "fleet must render the gVisor pool as n2-standard-4 with a 150 GB pd-balanced disk: ${google_container_node_pool.gvisor[0].node_config[0].machine_type} / ${google_container_node_pool.gvisor[0].node_config[0].disk_size_gb} / ${google_container_node_pool.gvisor[0].node_config[0].disk_type}"
  }

  assert {
    condition     = output.standard_machine_type == "n2-standard-8" && output.standard_disk_size_gb == 200 && output.gvisor_machine_type == "n2-standard-4" && output.gvisor_disk_size_gb == 150
    error_message = "the resolved-shape outputs must report the fleet shape under fleet"
  }
}

# The explicit value wins even when it equals the small shape: a tfvars that
# writes e2-standard-4 beside sizing_profile = "fleet" asked for e2-standard-4.
run "an_explicit_value_beside_fleet_wins_over_the_profile" {
  command = plan

  variables {
    sizing_profile        = "fleet"
    standard_machine_type = "e2-standard-4"
    standard_disk_size_gb = 50
    standard_disk_type    = "pd-ssd"
    gvisor_machine_type   = "n2-standard-2"
    gvisor_disk_size_gb   = 100
    gvisor_disk_type      = "pd-standard"
    gvisor_min_node_count = 2
    gvisor_max_node_count = 2
  }

  assert {
    condition     = google_container_cluster.standard[0].node_config[0].machine_type == "e2-standard-4" && google_container_cluster.standard[0].node_config[0].disk_size_gb == 50 && google_container_cluster.standard[0].node_config[0].disk_type == "pd-ssd"
    error_message = "an explicit default-pool shape must win over fleet: ${google_container_cluster.standard[0].node_config[0].machine_type} / ${google_container_cluster.standard[0].node_config[0].disk_size_gb} / ${google_container_cluster.standard[0].node_config[0].disk_type}"
  }

  assert {
    condition     = google_container_node_pool.gvisor[0].node_config[0].machine_type == "n2-standard-2" && google_container_node_pool.gvisor[0].node_config[0].disk_size_gb == 100 && google_container_node_pool.gvisor[0].node_config[0].disk_type == "pd-standard"
    error_message = "an explicit gVisor shape must win over fleet: ${google_container_node_pool.gvisor[0].node_config[0].machine_type} / ${google_container_node_pool.gvisor[0].node_config[0].disk_size_gb} / ${google_container_node_pool.gvisor[0].node_config[0].disk_type}"
  }

  assert {
    condition     = google_container_node_pool.gvisor[0].initial_node_count == 2 && google_container_node_pool.gvisor[0].autoscaling[0].min_node_count == 2 && google_container_node_pool.gvisor[0].autoscaling[0].max_node_count == 2
    error_message = "equal bounds must render a fixed-size pool of that many nodes per zone"
  }

  assert {
    condition     = output.standard_machine_type == "e2-standard-4" && output.standard_disk_size_gb == 50 && output.standard_disk_type == "pd-ssd" && output.gvisor_machine_type == "n2-standard-2" && output.gvisor_disk_size_gb == 100 && output.gvisor_disk_type == "pd-standard"
    error_message = "the resolved-shape outputs must report the explicit values"
  }
}

# A blanked `TF_VAR_` line in install.env exports "", and "" means unset on
# every string input: the disk types fall back to the profile's type the way
# the machine types already did through coalesce, rather than failing the
# disk-type validation.
run "a_blank_string_input_takes_the_profile_s_value" {
  command = plan

  variables {
    sizing_profile        = "fleet"
    standard_machine_type = ""
    standard_disk_type    = ""
    gvisor_machine_type   = ""
    gvisor_disk_type      = ""
  }

  assert {
    condition     = google_container_cluster.standard[0].node_config[0].machine_type == "n2-standard-8" && google_container_cluster.standard[0].node_config[0].disk_type == "pd-balanced"
    error_message = "a blank default-pool machine type or disk type must take the profile's value: ${google_container_cluster.standard[0].node_config[0].machine_type} / ${google_container_cluster.standard[0].node_config[0].disk_type}"
  }

  assert {
    condition     = google_container_node_pool.gvisor[0].node_config[0].machine_type == "n2-standard-4" && google_container_node_pool.gvisor[0].node_config[0].disk_type == "pd-balanced"
    error_message = "a blank gVisor machine type or disk type must take the profile's value: ${google_container_node_pool.gvisor[0].node_config[0].machine_type} / ${google_container_node_pool.gvisor[0].node_config[0].disk_type}"
  }
}

# A blanked `TF_VAR_sizing_profile=` line exports "" too, and it means the
# default profile rather than a refused plan, the way INSTALL.md's example
# line reads when an operator blanks it to go back to the default.
run "a_blank_profile_takes_the_default" {
  command = plan

  variables {
    sizing_profile = ""
  }

  assert {
    condition     = google_container_cluster.standard[0].node_config[0].machine_type == "e2-standard-4" && google_container_cluster.standard[0].node_config[0].disk_size_gb == 100
    error_message = "a blank profile must render the default pool as small does: ${google_container_cluster.standard[0].node_config[0].machine_type} / ${google_container_cluster.standard[0].node_config[0].disk_size_gb}"
  }

  assert {
    condition     = google_container_node_pool.gvisor[0].node_config[0].machine_type == "e2-standard-4" && google_container_node_pool.gvisor[0].node_config[0].disk_size_gb == 100
    error_message = "a blank profile must render the gVisor pool as small does: ${google_container_node_pool.gvisor[0].node_config[0].machine_type} / ${google_container_node_pool.gvisor[0].node_config[0].disk_size_gb}"
  }

  assert {
    condition     = output.sizing_profile == "small"
    error_message = "the sizing_profile output must report the profile a blank resolved to, not the blank: ${output.sizing_profile}"
  }
}

run "an_unknown_profile_is_refused" {
  command = plan

  variables {
    sizing_profile = "large"
  }

  expect_failures = [var.sizing_profile]
}

run "a_disk_under_ten_gb_is_refused" {
  command = plan

  variables {
    standard_disk_size_gb = 9
    gvisor_disk_size_gb   = 0
  }

  expect_failures = [var.standard_disk_size_gb, var.gvisor_disk_size_gb]
}

run "an_unknown_disk_type_is_refused" {
  command = plan

  variables {
    standard_disk_type = "pd-extreme"
    gvisor_disk_type   = "local-ssd"
  }

  expect_failures = [var.standard_disk_type, var.gvisor_disk_type]
}

run "a_node_count_below_one_is_refused" {
  command = plan

  variables {
    gvisor_min_node_count = 0
    gvisor_max_node_count = 0
  }

  expect_failures = [var.gvisor_min_node_count, var.gvisor_max_node_count]
}

run "a_floor_above_the_ceiling_is_refused" {
  command = plan

  variables {
    gvisor_min_node_count = 3
    gvisor_max_node_count = 2
  }

  expect_failures = [google_container_node_pool.gvisor]
}
