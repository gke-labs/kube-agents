# Copyright 2026 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# The scenario driver for bench/tasks/networking-gateway-ingress-port-mismatch.
#
# It plants a one-replica Deployment whose container serves HTTP on 8443 and
# declares that port, a ClusterIP Service whose port 80 maps to the numeric
# targetPort 8080 that no container declares or serves, and an HTTPRoute that
# sends traffic to that Service port. The route's parent Gateway is not
# created: a Gateway would provision a public load balancer in the install's
# project, which a bench fixture may not do, so the route stays unattached and
# the mismatch is a static one the check reads off the three objects. The
# HTTPRoute needs the Gateway API CRDs on the host, which GKE Autopilot
# installs by default. No audit reads the mismatch: the case carries
# expected_fail: true.
#
# Isolation class: namespace-scoped, in its own namespace. The namespace, its
# labels, the leftover pre-clear and the teardown are the namespaced-plant
# module's; this file holds the manifest. The pod is healthy (it serves and
# restarts nothing), so the host's event watcher sees no alertable event.

terraform {
  required_version = ">= 1.5.0"
}

locals {
  ci_labels = {
    "managed-by"  = "kube-agents-bench"
    "build-id"    = var.prow_build_id != "" ? var.prow_build_id : "local"
    "pull-number" = var.prow_pull_number != "" ? var.prow_pull_number : "none"
  }
  manifest = <<-MANIFEST
    apiVersion: apps/v1
    kind: Deployment
    metadata:
      name: "${var.target_resource_name}"
      labels:
        app: "${var.target_resource_name}"
        managed-by: kube-agents-bench
    spec:
      replicas: 1
      selector:
        matchLabels:
          app: "${var.target_resource_name}"
      template:
        metadata:
          labels:
            app: "${var.target_resource_name}"
        spec:
          automountServiceAccountToken: false
          securityContext:
            runAsNonRoot: true
            runAsUser: 65534
            seccompProfile:
              type: RuntimeDefault
          containers:
            - name: "${var.target_resource_name}"
              image: registry.k8s.io/e2e-test-images/agnhost:2.53
              args: ["netexec", "--http-port=8443"]
              ports:
                - name: https
                  containerPort: 8443
              resources:
                requests:
                  cpu: 10m
                  memory: 32Mi
                limits:
                  memory: 64Mi
    ---
    apiVersion: v1
    kind: Service
    metadata:
      name: "${var.target_resource_name}-svc"
      labels:
        app: "${var.target_resource_name}"
        managed-by: kube-agents-bench
    spec:
      type: ClusterIP
      selector:
        app: "${var.target_resource_name}"
      ports:
        - name: http
          port: 80
          targetPort: 8080
    ---
    apiVersion: gateway.networking.k8s.io/v1
    kind: HTTPRoute
    metadata:
      name: "${var.target_resource_name}"
      labels:
        app: "${var.target_resource_name}"
        managed-by: kube-agents-bench
    spec:
      parentRefs:
        - name: bench-gateway
      rules:
        - backendRefs:
            - name: "${var.target_resource_name}-svc"
              port: 80
  MANIFEST
}

module "plant" {
  source = "../../modules/namespaced-plant"

  host_cluster_name     = var.host_cluster_name
  host_cluster_location = var.host_cluster_location
  project_id            = var.project_id
  namespace             = var.namespace
  stack                 = "networking-gateway-ingress-port-mismatch"
  labels                = local.ci_labels
  manifest              = local.manifest
}

output "cluster_name" {
  value = module.plant.cluster_name
}

output "cluster_location" {
  value = module.plant.cluster_location
}
