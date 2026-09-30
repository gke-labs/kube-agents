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

# The scenario driver for bench/tasks/first-install-hello-running and
# first-install-hello-done.
#
# The first-install greeting fires once per deployment, on the first turn from
# a chat platform with durable delivery (the bootstrap_onboarding plugin in
# agents/chat/defaults/plugins/). The bench reaches the agent over the API
# server, which that plugin excludes, on an install that greeted long ago. So
# this stack writes the plugin's eval seam instead: a one-shot request file,
# named by the EVAL_GREET_MARKER prefix plus this case's suffix, in the chat
# profile's home inside the agent pod. The plugin injects the greeting for the
# request's variant on the first turn whose message contains its phrase, and
# unlinks the file. It binds no delivery and touches no onboarding marker, so
# the install's real onboarding state is the same before and after the case.
#
# One file per case, each with its own phrase, because the nightly runs cases
# concurrently against one install: a shared file would be overwritten by the
# other variant's apply, and a phrase-less one taken by any case's first turn.
#
# Destroy removes the file, in case the turn that should have taken it never
# arrived. Nothing else is planted, so nothing else is torn down.

terraform {
  required_version = ">= 1.5.0"
  required_providers {
    null = {
      source  = "hashicorp/null"
      version = ">= 3.0.0"
    }
  }
}

locals {
  marker_name = ".bootstrap_greet_eval-${var.case_suffix}"
  request     = jsonencode({ phrase = var.phrase, variant = var.variant })
}

resource "null_resource" "greet_request" {
  triggers = {
    host_cluster  = var.host_cluster_name
    host_location = var.host_cluster_location
    host_project  = var.project_id
    namespace     = var.agent_namespace
    deployment    = var.agent_deployment
    container     = var.agent_container
    marker_name   = local.marker_name
    request       = local.request
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
      set -euo pipefail

      kubeconfig_dir="$(mktemp -d)"
      trap 'rm -rf "$kubeconfig_dir"' EXIT
      trap 'exit 143' TERM INT
      KUBECONFIG="$kubeconfig_dir/config"
      export KUBECONFIG

      project="${var.project_id}"
      if [ -z "$project" ]; then
        project="$(gcloud config get-value project 2>/dev/null || true)"
      fi
      if [ -z "$project" ]; then
        echo "ERROR: no project id. Pass -var project_id=... or set a gcloud default project; this stack needs one to fetch credentials for ${var.host_cluster_name}." >&2
        exit 1
      fi

      gcloud container clusters get-credentials "${var.host_cluster_name}" \
        --location "${var.host_cluster_location}" --project "$project" --quiet

      pod="deployment/${var.agent_deployment}"
      exec_in_pod=(kubectl exec -i "$pod" -n "${var.agent_namespace}" -c "${var.agent_container}" --)

      # The home is read inside the pod the way the entrypoint derives HERMES_HOME
      # (PLATFORM_AGENT_HOME from the operator, then the plugin's own default), so
      # the file lands where the chat profile's hook looks for it.
      printf '%s' '${local.request}' | "$${exec_in_pod[@]}" sh -c \
        'home="$${PLATFORM_AGENT_HOME:-$${HERMES_HOME:-/opt/data}}"; cat > "$home/$1.tmp" && mv "$home/$1.tmp" "$home/$1"' \
        sh "${local.marker_name}"

      written="$("$${exec_in_pod[@]}" sh -c 'cat "$${PLATFORM_AGENT_HOME:-$${HERMES_HOME:-/opt/data}}/$1"' sh "${local.marker_name}" </dev/null)"
      if [ "$written" != '${local.request}' ]; then
        echo "ERROR: ${local.marker_name} in ${var.agent_container} does not hold the request this stack wrote, so the greeting would not fire. Found: $written" >&2
        exit 1
      fi
      echo "Wrote ${local.marker_name} (variant=${var.variant})."
    EOT
  }

  provisioner "local-exec" {
    when        = destroy
    on_failure  = continue
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
      set -euo pipefail
      kubeconfig_dir="$(mktemp -d)"
      trap 'rm -rf "$kubeconfig_dir"' EXIT
      KUBECONFIG="$kubeconfig_dir/config"
      export KUBECONFIG

      project="${self.triggers.host_project}"
      if [ -z "$project" ]; then
        project="$(gcloud config get-value project 2>/dev/null || true)"
      fi

      gcloud container clusters get-credentials "${self.triggers.host_cluster}" \
        --location "${self.triggers.host_location}" --project "$project" --quiet

      kubectl exec "deployment/${self.triggers.deployment}" -n "${self.triggers.namespace}" \
        -c "${self.triggers.container}" -- \
        sh -c 'rm -f "$${PLATFORM_AGENT_HOME:-$${HERMES_HOME:-/opt/data}}/$1"' sh "${self.triggers.marker_name}"
    EOT
  }
}

output "cluster_name" {
  value = var.host_cluster_name
}

output "cluster_location" {
  value = var.host_cluster_location
}
