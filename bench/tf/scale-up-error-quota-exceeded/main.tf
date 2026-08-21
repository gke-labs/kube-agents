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
  source          = "git::https://github.com/kubernetes-sigs/devops-bench.git//tf/modules/cluster?ref=4670d76dcc497e8f515d51c2bb6bad6ced7100b6"
  infra_provider  = var.infra_provider
  cluster_name    = var.cluster_name
  location        = var.location
  node_count      = var.node_count
  machine_type    = var.machine_type
  project_id      = var.project_id
  kubeconfig_path = var.kubeconfig_path
}

# Dedicated autoscaling node pool (min: 1, max: 10) for GPU workloads
resource "google_container_node_pool" "autoscaling_pool" {
  count      = var.infra_provider == "gcp" ? 1 : 0
  name       = "autoscaling-node-pool"
  cluster    = module.cluster.cluster_name
  location   = module.cluster.location
  project    = var.project_id

  initial_node_count = 0

  autoscaling {
    min_node_count = 0
    max_node_count = 10
  }

  node_config {
    machine_type = "a2-ultragpu-1g"
    labels = {
      devops-bench-eval = "true"
    }
  }

  depends_on = [module.cluster]
}

# Deploy workload requesting GPUs to force Cluster Autoscaler scale up
resource "null_resource" "request_gpu_workload" {
  provisioner "local-exec" {
    command = <<EOT
      if [ "${var.infra_provider}" = "gcp" ]; then
        gcloud container clusters get-credentials ${module.cluster.cluster_name} --location=${module.cluster.location} --project=${var.project_id}
      fi

      kubectl apply -f - <<'EOF'
apiVersion: apps/v1
kind: Deployment
metadata:
  name: scale-target
  namespace: default
  labels:
    app: scale-target
    devops-bench-eval: "true"
spec:
  replicas: 5
  selector:
    matchLabels:
      app: scale-target
  template:
    metadata:
      labels:
        app: scale-target
        devops-bench-eval: "true"
    spec:
      containers:
      - name: cuda-container
        image: registry.k8s.io/pause:3.9
        resources:
          limits:
            nvidia.com/gpu: "1"
          requests:
            nvidia.com/gpu: "1"
EOF
    EOT
  }

  depends_on = [
    google_container_node_pool.autoscaling_pool
  ]
}

output "cluster_name" {
  value = module.cluster.cluster_name
}

output "cluster_location" {
  value = module.cluster.location
}
