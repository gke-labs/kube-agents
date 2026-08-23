terraform {
  required_version = "~> 1.5"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.30, < 8.0"
    }
  }
}

# The default project is the SHARED cache project, not the pool project being
# onboarded, because the only resource this composition writes to lives there.
# The pool project is read from and never written to: the data source in
# main.tf needs nothing beyond resourcemanager.projects.get on it. Keeping the
# provider pointed at the cache project makes the asymmetry visible in the
# plan, and it is the reason applying this needs rights the other pool-project
# compositions do not — see "Who can apply this" in README.md.
provider "google" {
  project = var.cache_project_id
}
