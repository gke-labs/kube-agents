terraform {
  required_version = "~> 1.5"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.30, < 8.0"
    }
    # google_project_service_identity is beta-only, as it is in chat-pubsub
    # and gke-cluster. The module takes the root's default google-beta
    # configuration; full-install declares one.
    google-beta = {
      source  = "hashicorp/google-beta"
      version = ">= 5.30, < 8.0"
    }
    # time_sleep, twice: the apply-time wait for the Logging service agent to
    # become bindable, and the destroy-time wait between deleting the sink and
    # deleting the topic it exports to.
    time = {
      source  = "hashicorp/time"
      version = ">= 0.9, < 1.0"
    }
  }
}
