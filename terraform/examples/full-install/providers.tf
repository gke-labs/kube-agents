terraform {
  required_version = "~> 1.5"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.30, < 8.0"
    }
    google-beta = {
      source  = "hashicorp/google-beta"
      version = ">= 5.30, < 8.0"
    }
    random = {
      source  = "hashicorp/random"
      version = ">= 3.5, < 4.0"
    }
    helm = {
      source = "hashicorp/helm"
      # The kubernetes = { ... } attribute syntax below is helm-provider-3.x
      # only, so the floor must exclude 2.x.
      version = ">= 3.0, < 4.0"
    }
  }
}

provider "google" {
  project = var.project_id
}

# The chat-pubsub module's service-identity resources (Workspace Add-ons and
# the Chat API) require the google-beta provider; it inherits this default
# configuration.
provider "google-beta" {
  project = var.project_id
}

# The helm provider authenticates against the cluster this configuration
# itself creates, using the caller's ADC token.
data "google_client_config" "default" {}

provider "helm" {
  kubernetes = {
    host                   = "https://${module.gke_cluster.cluster_endpoint}"
    token                  = data.google_client_config.default.access_token
    cluster_ca_certificate = base64decode(module.gke_cluster.cluster_ca_certificate)
  }
}
