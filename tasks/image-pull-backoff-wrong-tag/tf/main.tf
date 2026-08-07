terraform {
  required_version = ">= 1.5.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.0.0"
    }
    kind = {
      source  = "tehcyx/kind"
      version = ">= 0.5.0"
    }
    null = {
      source  = "hashicorp/null"
      version = ">= 3.0.0"
    }
  }
}

provider "google" {
  project = var.project_id != "" ? var.project_id : null
  region  = var.location != "" && var.location != "local" ? var.location : null
}

provider "kind" {}

module "cluster" {
  source          = "git::https://github.com/gke-labs/devops-bench.git//tf/modules/cluster?ref=main"
  infra_provider  = var.infra_provider
  cluster_name    = var.cluster_name
  location        = var.location
  node_count      = var.node_count
  machine_type    = var.machine_type
  project_id      = var.project_id
  kubeconfig_path = var.kubeconfig_path
}

resource "null_resource" "seed_broken_deployment" {
  provisioner "local-exec" {
    command = <<EOT
      if [ "${var.infra_provider}" = "gcp" ]; then
        gcloud container clusters get-credentials ${module.cluster.cluster_name} --location=${module.cluster.location} --project=${var.project_id}
      fi
      kubectl apply -f - <<'EOF'
apiVersion: apps/v1
kind: Deployment
metadata:
  name: my-app
  namespace: default
  labels:
    app: my-app
    devops-bench-eval: "true"
spec:
  replicas: 2
  selector:
    matchLabels:
      app: my-app
  template:
    metadata:
      labels:
        app: my-app
        devops-bench-eval: "true"
    spec:
      containers:
      - name: my-app
        image: nginx:invalid-tag
        ports:
        - containerPort: 80
EOF
    EOT
  }

  depends_on = [
    module.cluster
  ]
}

output "cluster_name" {
  value = module.cluster.cluster_name
}

output "cluster_location" {
  value = module.cluster.location
}
