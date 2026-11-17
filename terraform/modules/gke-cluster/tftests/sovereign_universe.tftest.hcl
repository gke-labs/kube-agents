# Sovereign / Google Cloud Dedicated cluster topology: partitioned project IDs
# map workload_pool to <slug>.<partition>.svc.id.goog, u- region/zone locations
# classify regional Autopilot vs zonal Standard accurately, custom VPC network
# and subnetwork wire onto cluster creation, and sovereign DNS control-plane
# endpoints (*.gke.<universe_domain>) set cluster_endpoint_is_dns = true.

mock_provider "google" {}
mock_provider "google-beta" {}

variables {
  project_id                 = "part0:sample-project"
  cluster_name               = "sovereign-host"
  location                   = "u-region-north1"
  cluster_mode               = "autopilot"
  network                    = "platform-net"
  subnetwork                 = "platform-subnet"
  enable_database_encryption = false
}

run "partitioned_project_and_custom_vpc_wire_autopilot_cluster" {
  command = plan

  assert {
    condition     = output.workload_identity_pool == "sample-project.part0.svc.id.goog"
    error_message = "partitioned project ID must map workload_identity_pool to sample-project.part0.svc.id.goog, got: ${output.workload_identity_pool}"
  }

  assert {
    condition     = google_container_cluster.autopilot[0].network == "platform-net" && google_container_cluster.autopilot[0].subnetwork == "platform-subnet"
    error_message = "custom VPC network and subnetwork must wire onto google_container_cluster.autopilot[0]"
  }
}

run "u_prefixed_zone_is_rejected_for_autopilot_and_accepted_for_standard" {
  command = plan

  variables {
    location     = "u-region-north1-a"
    cluster_mode = "autopilot"
  }

  expect_failures = [
    google_container_cluster.autopilot,
  ]
}

run "u_prefixed_zone_builds_standard_cluster_with_partitioned_workload_pool" {
  command = plan

  variables {
    location     = "u-region-north1-a"
    cluster_mode = "standard"
  }

  assert {
    condition     = google_container_cluster.standard[0].workload_identity_config[0].workload_pool == "sample-project.part0.svc.id.goog"
    error_message = "standard cluster must render sample-project.part0.svc.id.goog"
  }

  assert {
    condition     = google_container_cluster.standard[0].network == "platform-net" && google_container_cluster.standard[0].subnetwork == "platform-subnet"
    error_message = "custom VPC network and subnetwork must wire onto google_container_cluster.standard[0]"
  }
}

run "sovereign_dns_endpoint_sets_cluster_endpoint_is_dns" {
  command = plan

  variables {
    create_cluster = false
  }

  override_data {
    target = data.google_container_cluster.existing[0]
    values = {
      endpoint                 = "gke-1234abcd-9876543210.u-region-north1.gke-universe.example.test"
      master_auth              = [{ cluster_ca_certificate = "Y2E=" }]
      workload_identity_config = [{ workload_pool = "sample-project.part0.svc.id.goog" }]
      datapath_provider        = "ADVANCED_DATAPATH"
      network_policy           = []
    }
  }

  assert {
    condition     = output.cluster_endpoint_is_dns == true
    error_message = "sovereign DNS endpoint (gke-<hash>-<project-number>.<region>.gke-<suffix>) must set cluster_endpoint_is_dns = true"
  }
}
