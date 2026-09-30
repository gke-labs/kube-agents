terraform {
  required_version = "~> 1.5"
  required_providers {
    # data.google_client_config: the provider's own access token, so every
    # read is answered for the identity that applies.
    google = {
      source  = "hashicorp/google"
      version = ">= 5.30, < 8.0"
    }
    # The google provider has no data source that lists a host's service
    # projects or a Metrics Scope's monitored projects, so the reads are made
    # directly.
    http = {
      source  = "hashicorp/http"
      version = ">= 3.4, < 4.0"
    }
  }
}
