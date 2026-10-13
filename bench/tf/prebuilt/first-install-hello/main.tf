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
# this stack writes the plugin's eval seam instead: a request file, named by
# the EVAL_GREET_MARKER prefix plus this case's suffix, in the chat profile's
# home inside the agent pod. The plugin injects the greeting for the request's
# variant on every turn whose message contains its phrase, so a transport retry
# of the opening turn greets too. It binds no delivery and touches no onboarding
# marker, so the install's real onboarding state is the same before and after
# the case.
#
# One file per case, each with its own phrase, because the nightly runs cases
# concurrently against one install: a shared file would be overwritten by the
# other variant's apply, and a phrase-less one taken by any case's first turn.
#
# Destroy removes the file and reads back, failing when it is still there;
# until then it stays armed for its phrase. The request also carries the time
# it was written, and the plugin refuses one older than an hour, so a file a
# failed destroy leaves behind disarms on its own. Nothing else is planted, so
# nothing else is torn down.

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
  request     = jsonencode({ phrase = var.phrase, variant = var.variant, home_channel_set = var.home_channel_set })
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
    environment = {
      GREET_REQUEST = local.request
      MARKER_NAME   = local.marker_name
    }
    command = <<-EOT
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

      # Stamped here rather than in local.request, which is a trigger: a time
      # there would re-create the resource on every plan. jsonencode ends the
      # object on its closing brace, which the key goes in front of.
      request="$${GREET_REQUEST%?},\"written_at\":$(date -u +%s)}"

      # The home is read inside the pod the way the entrypoint derives HERMES_HOME
      # (PLATFORM_AGENT_HOME from the operator, then the plugin's own default), so
      # the file lands where the chat profile's hook looks for it.
      printf '%s' "$request" | "$${exec_in_pod[@]}" sh -c \
        'home="$${PLATFORM_AGENT_HOME:-$${HERMES_HOME:-/opt/data}}"; cat > "$home/$1.tmp" && mv "$home/$1.tmp" "$home/$1"' \
        sh "$MARKER_NAME"

      written="$("$${exec_in_pod[@]}" sh -c 'cat "$${PLATFORM_AGENT_HOME:-$${HERMES_HOME:-/opt/data}}/$1"' sh "$MARKER_NAME" </dev/null)"
      if [ "$written" != "$request" ]; then
        echo "ERROR: ${local.marker_name} in ${var.agent_container} does not hold the request this stack wrote, so the greeting would not fire. Found: $written" >&2
        exit 1
      fi
      echo "Wrote ${local.marker_name} (variant=${var.variant})."
    EOT
  }

  # No on_failure = continue: a destroy that cannot confirm the file is gone
  # fails the run, rather than leaving a request Terraform records as removed.
  provisioner "local-exec" {
    when        = destroy
    interpreter = ["/bin/bash", "-c"]
    environment = {
      MARKER_NAME = self.triggers.marker_name
    }
    command = <<-EOT
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

      exec_in_pod=(kubectl exec "deployment/${self.triggers.deployment}" -n "${self.triggers.namespace}" -c "${self.triggers.container}" --)

      "$${exec_in_pod[@]}" sh -c 'rm -f "$${PLATFORM_AGENT_HOME:-$${HERMES_HOME:-/opt/data}}/$1"' sh "$MARKER_NAME" </dev/null

      # A separate read, so a remove that silently did nothing is caught. An
      # exec that fails outright fails the destroy through set -e.
      remaining="$("$${exec_in_pod[@]}" sh -c 'if [ -e "$${PLATFORM_AGENT_HOME:-$${HERMES_HOME:-/opt/data}}/$1" ]; then echo present; else echo absent; fi' sh "$MARKER_NAME" </dev/null)"
      if [ "$remaining" != absent ]; then
        echo "ERROR: $MARKER_NAME is still in ${self.triggers.container} after destroy (read back: $remaining). Every API-server turn carrying its phrase gets the canned greeting until it is removed or an hour after it was written." >&2
        exit 1
      fi
      echo "Removed $MARKER_NAME."
    EOT
  }
}

output "cluster_name" {
  value = var.host_cluster_name
}

output "cluster_location" {
  value = var.host_cluster_location
}
