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

# The scenario driver for bench/tasks/gitops-drift-load-bearing-change-triage.
#
# A hardening setting is loosened by hand, and this time the loosening is what
# is holding the workload up. Git declares `runAsNonRoot: true`; the image runs
# as root; under the declared state the kubelet refuses to start the container
# at all and the Deployment has never had a running pod. Somebody flips the
# setting to false by hand and the workload comes up. This stack plants that,
# then hands the Session KV daemon the audit record the edit would have
# produced.
#
# WHY THIS SCENARIO EXISTS, AND WHY IT IS THE EXPENSIVE ONE.
#
# It is the counterweight to its sibling case gitops-drift-deleted-policy-
# triage, where a security control was removed and nothing needed it removed,
# so the answer is revert. Here the same shape of change -- a person loosening
# security outside git -- has the opposite answer, because what git declares
# does not run. Ship only the first and an agent that answers "revert" to every
# security drift scores full marks, which is precisely what CUJ 1 of
# docs/designs/drift-detection.md says we must beat: "Self-heal reverts blindly
# (bad if it was an emergency fix)".
#
# It is also the only drift scenario in the suite where a wrong answer costs
# something. A mistaken "revert" here, followed, takes the service down. A
# mistaken "codify" on the sibling leaves a setting loose for longer.
#
# WHAT THE AGENT HAS TO WORK OUT, AND FROM WHAT.
#
# Nothing states the change was deliberate. There is no annotation, no ticket,
# no note -- because an operator unblocking a deploy at speed leaves none. The
# agent never learns the MOTIVE and is not asked to. What it can establish is
# whether the change is LOAD-BEARING, which is an observable property of the
# cluster, and this stack plants three independent ways to see it:
#
#   1. The pods are healthy NOW, under the loosened setting.
#   2. Warning events from the failed pods, carrying the kubelet's own sentence
#      ("container has runAsNonRoot and image will run as root"). Richest, and
#      the first to go: Kubernetes expires events after about an hour, so a
#      delayed run may not find them.
#   3. The superseded ReplicaSet, still carrying `runAsNonRoot: true` in its
#      pod template and zero ready replicas -- the declared state, recorded,
#      with the fact that it never ran. This one does not expire, and the case
#      is deliberately winnable from it alone, so that an eval delayed past the
#      event TTL fails as a genuine red rather than as a fixture that decayed.
#
# WHY THE RECORD IS PLANTED RATHER THAN OBSERVED. ../gitops-drift's header
# carries the argument in full and it applies unchanged: the detector's
# classifier drops automation principals before anything reaches the daemon and
# every principal a bench fixture can authenticate as is one, and the audit
# route's sink costs minutes of export lag on top of the run's budget.
#
# Isolation class: namespace-scoped. Eligible tier: nightly.

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
  ns       = var.drift_namespace
  workload = var.workload_name

  # The object path the daemon renders from the payload's `resource` block
  # (_drift_resource_path in agents/platform/scripts/session_kv_server.py:
  # namespace/resource/name).
  resource_path = "${var.drift_namespace}/deployments/${var.workload_name}"

  # The fields the out-of-band edits take, as the ownership block renders
  # them. Both are listed because both are taken: steps 3 and 4 patch
  # runAsUser as well as runAsNonRoot, each under the drift field manager, and
  # a server-side Update claims every field whose value it changed. The
  # detector builds `owners` from the live managedFields, so a record naming
  # only runAsNonRoot would describe ownership the agent contradicts the
  # moment it does what the card asks and reads the live object.
  drifted_path      = ".spec.template.spec.securityContext.runAsNonRoot"
  drifted_user_path = ".spec.template.spec.securityContext.runAsUser"

  # Seconds. Hoisted because each bounds a different failure and inline they
  # read as one arbitrary number repeated.
  #
  # leftover_delete_timeout: clear of the ~35s a namespace holding one busybox
  # Deployment takes to go, short enough that a finalizer wedge fails here
  # naming the real problem.
  #
  # failure_observe_timeout: how long the declared state gets to prove it does
  # not run. Scheduling plus one kubelet start attempt is seconds on a warm
  # pool; this is long enough that a cold pool or a slow image pull is not
  # mistaken for a workload that started fine.
  #
  # crashloop_observe_timeout: revision 2's failure is slower than revision
  # 1's. The container starts, EACCES on /etc, exits 1 -- and the back-off
  # state the assertion looks for only appears after the kubelet has retried,
  # so this is a couple of restart intervals rather than one scheduling delay.
  #
  # rollout_ready_timeout: the loosened state has to actually come up, because
  # "the pods are healthy now" is the first of the three evidences.
  #
  # card_wait_timeout: the front-door turn is one kanban_create, so the card
  # appears in seconds when the daemon is working and never when it is not.
  leftover_delete_timeout   = 180
  failure_observe_timeout   = 240
  crashloop_observe_timeout = 300
  rollout_ready_timeout     = 240
  card_wait_timeout         = 300
  poll_interval             = 10

  # What the failure dump prints before tearing the namespace down. This
  # stack's evidence is a rollout history, so the ReplicaSets matter as much
  # as the Deployment.
  failure_resources = "deployments,replicasets,pods"

  ci_labels = {
    "managed-by"  = "kube-agents-bench"
    "case"        = "gitops-drift-load-bearing-change-triage"
    "build-id"    = var.prow_build_id != "" ? var.prow_build_id : "local"
    "pull-number" = var.prow_pull_number != "" ? var.prow_pull_number : "none"
  }
}

resource "null_resource" "load_bearing_change" {
  triggers = {
    # Destroy-time provisioners may read only `self`, so everything the
    # teardown names is copied in here -- including the cluster coordinates,
    # because the destroy fetches its own credentials for the same reason
    # drift_init does and cannot reach `var`.
    namespace     = local.ns
    host_cluster  = var.host_cluster_name
    host_location = var.host_cluster_location
    host_project  = var.project_id

    # Re-plant when any of it changes. Without this the resource is inert after
    # the first apply, and a local run that edited the scenario would keep
    # scoring the old one. Inert in CI, where state starts fresh every run.
    scenario = sha256(join("|", [
      local.ns,
      local.workload,
      tostring(var.workload_replicas),
      var.workload_image,
      var.workload_command,
      var.gitops_field_manager,
      var.drift_field_manager,
      var.drift_principal,
      var.host_cluster_name,
    ]))
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
      set -euo pipefail

      # The scaffolding every gitops-drift fixture shares -- the kubeconfig
      # fetch, the labelled-leftover guard, the healthz probe, the session and
      # inject exchange and the card poll -- is one file, and the three stacks
      # that used to carry their own copy now source it. What stays here is the
      # scenario: three observed rollouts, the assertions on them, and the
      # payload's values. The module's header documents every variable below.
      drift_ns="${local.ns}"
      drift_cluster="${var.host_cluster_name}"
      drift_location="${var.host_cluster_location}"
      drift_project_id="${var.project_id}"
      drift_managed_by="${local.ci_labels["managed-by"]}"
      drift_leftover_timeout=${local.leftover_delete_timeout}
      drift_agent_namespace="${var.agent_namespace}"
      drift_agent_container="${var.agent_container}"
      drift_agent_deployment="${var.agent_deployment}"
      drift_daemon_url="${var.daemon_url}"
      drift_card_wait_timeout=${local.card_wait_timeout}
      drift_poll_interval=${local.poll_interval}
      drift_failure_resources="${local.failure_resources}"

      source "${path.module}/../../modules/drift-inject/drift_inject.sh"

      drift_init
      drift_clear_namespace

      # This stack is the reason the probe runs before the plant rather than
      # after: its plant is three observed rollouts, two of them waiting out a
      # failure, and a daemon that cannot take the record is knowable in ten
      # seconds. Probing afterwards would spend those minutes, let the exit
      # trap tear the namespace back down, and then report the condition.
      drift_require_daemon

      # ---- 1. Apply what git declares (revision 1) --------------------------
      # Server-side as ${var.gitops_field_manager}, which is what a GitOps
      # controller reconciling this manifest looks like in managedFields: that
      # manager owns the whole declared spec, the securityContext included.
      #
      # This state DOES NOT RUN, and that is the scenario rather than a defect
      # in the fixture. ${var.workload_image} publishes no USER directive, so
      # its processes are UID 0; a kubelet handed runAsNonRoot: true with no
      # runAsUser refuses to start the container.
      #
      # Three revisions are planted in all, and the sequence is the story the
      # agent has to read: this one, then the uid the operator tried, then the
      # one they settled for. Steps 2 to 4 lay them down in order.
      drift_planted_ns=1
      kubectl create namespace "${local.ns}" --dry-run=client -o yaml | kubectl apply -f -
      kubectl label namespace "${local.ns}" --overwrite \
        managed-by="${local.ci_labels["managed-by"]}" \
        case="${local.ci_labels["case"]}" \
        build-id="${local.ci_labels["build-id"]}" \
        pull-number="${local.ci_labels["pull-number"]}"

      kubectl apply --server-side --field-manager="${var.gitops_field_manager}" -f - <<'MANIFEST'
      apiVersion: apps/v1
      kind: Deployment
      metadata:
        name: ${local.workload}
        namespace: ${local.ns}
        labels:
          app: ${local.workload}
      spec:
        replicas: ${var.workload_replicas}
        # Recreate, not the default RollingUpdate. Two of the three revisions
        # below never have a ready pod, and a rolling update between revisions
        # that cannot become ready is governed by maxSurge/maxUnavailable
        # arithmetic that leaves pods from several revisions alive at once --
        # which makes "is the CURRENT revision failing" ambiguous for the
        # assertions in steps 2 and 3, and leaves the namespace holding
        # CreateContainerConfigError and CrashLoopBackOff pods simultaneously.
        # Recreate tears the previous revision down first, so each step
        # observes one revision's failure and nothing else.
        strategy:
          type: Recreate
        selector:
          matchLabels:
            app: ${local.workload}
        template:
          metadata:
            labels:
              app: ${local.workload}
          spec:
            automountServiceAccountToken: false
            securityContext:
              runAsNonRoot: true
            containers:
              - name: web
                image: ${var.workload_image}
                command: ["sh", "-c", "${var.workload_command}"]
                resources:
                  requests:
                    cpu: 10m
                    memory: 16Mi
                  limits:
                    memory: 32Mi
      MANIFEST

      # ---- 2. Watch the declared state fail to run --------------------------
      # Waited for rather than assumed. If this never happens the whole case is
      # void: the premise is that what git declares cannot run, and an image
      # that starts fine under runAsNonRoot would leave the agent looking at a
      # gratuitous loosening -- the SIBLING case's scenario, with this case's
      # expected answer, which would grade a correct agent as wrong.
      #
      # The pod's own waiting reason rather than the event, because it is the
      # state the kubelet sets rather than a message it emits, and because the
      # events are checked separately below.
      elapsed=0
      while :; do
        reasons="$(kubectl get pods -n "${local.ns}" \
          -o jsonpath='{range .items[*]}{.status.containerStatuses[*].state.waiting.reason}{"\n"}{end}' 2>/dev/null || true)"
        if printf '%s\n' "$reasons" | grep -qxF 'CreateContainerConfigError'; then
          break
        fi
        if [ "$elapsed" -ge ${local.failure_observe_timeout} ]; then
          echo "ERROR: no pod of ${local.workload} reached CreateContainerConfigError within $${elapsed}s, so the state git declares is not demonstrably unrunnable and this case has no scenario. Either ${var.workload_image} no longer runs as root, or the pods never scheduled. Current state follows." >&2
          kubectl get pods -n "${local.ns}" -o wide >&2 || true
          kubectl describe pods -n "${local.ns}" >&2 || true
          exit 1
        fi
        sleep ${local.poll_interval}
        elapsed=$((elapsed + ${local.poll_interval}))
      done

      # Nothing may be ready under the declared state. A partially-available
      # Deployment would mean the setting is not what is blocking it, and the
      # report's reasoning -- reverting takes the service down -- would be
      # false.
      # Not swallowed. The guard below treats an empty value as "nothing
      # ready", so `2>/dev/null || true` would let a failed read SATISFY the
      # one check that proves the declared state does not run -- a silent pass
      # on the case's premise. Removing it is safe for the absent-field case
      # this check is actually about: `kubectl -o jsonpath` on a missing
      # `.status.readyReplicas` prints nothing and exits zero.
      ready_before="$(kubectl get deployment "${local.workload}" -n "${local.ns}" \
        -o jsonpath='{.status.readyReplicas}')"
      if [ -n "$ready_before" ] && [ "$ready_before" != "0" ]; then
        echo "ERROR: ${local.workload} already has $ready_before ready replica(s) under runAsNonRoot: true, so the declared state runs after all and this case's premise is false." >&2
        exit 1
      fi

      # The kubelet's own sentence, which is the richest evidence the agent can
      # find and the first to go: Kubernetes expires events after about an
      # hour. Asserted here so that a plant which produced no event fails
      # loudly now rather than as an unexplainable red later -- but the case
      # does NOT depend on the agent still seeing it, which is what the
      # ReplicaSet assertion in step 5 is for.
      # Read into a variable first, without swallowing, so a transport
      # failure aborts here with kubectl's own stderr. Inside the pipeline it
      # would hand grep an empty stream and the branch below would report a
      # missing kubelet event when nothing about the kubelet was read.
      event_messages="$(kubectl get events -n "${local.ns}" \
        -o jsonpath='{range .items[*]}{.message}{"\n"}{end}')"
      if ! printf '%s\n' "$event_messages" | grep -qi 'runasnonroot'; then
        echo "ERROR: no event in ${local.ns} mentions runAsNonRoot, so the kubelet did not report the reason it refused the container and the richest evidence this scenario offers is missing. Events follow." >&2
        kubectl get events -n "${local.ns}" --sort-by=.metadata.creationTimestamp >&2 || true
        exit 1
      fi
      echo "Declared state confirmed unrunnable: pods at CreateContainerConfigError, 0 ready, kubelet event recorded."

      # ---- 3. Plant the attempt that was tried and failed (revision 2) ------
      # The operator keeps the hardening and gives the container a non-root
      # uid. This is the repair a reader reaches for first, and planting it is
      # what makes this case gradable at all: without it the scenario has a
      # third way out that is better than both revert and codify, and the
      # agent takes it (#3102). With revision 2 in the history the third way
      # is not an open option, it is a recorded failure.
      #
      # Under ${var.drift_field_manager}, because it is the same person at the
      # same keyboard as step 4 -- one operator working the problem, not two.
      kubectl patch deployment "${local.workload}" -n "${local.ns}" \
        --field-manager="${var.drift_field_manager}" --type=merge \
        -p '{"spec":{"template":{"spec":{"securityContext":{"runAsNonRoot":true,"runAsUser":1000}}}}}'

      # Waited for, because an attempt that silently worked would destroy the
      # scenario quietly: the case would then be grading an agent that is
      # right to propose the uid, against a rubric that says it is wrong.
      #
      # Slower than revision 1's refusal. That one is a kubelet validation
      # failure before the container starts; this one starts, takes EACCES on
      # /etc, exits non-zero, and only reaches a back-off state after the
      # kubelet has retried it.
      elapsed=0
      while :; do
        waiting="$(kubectl get pods -n "${local.ns}" \
          -o jsonpath='{range .items[*]}{.status.containerStatuses[*].state.waiting.reason}{"\n"}{end}' 2>/dev/null || true)"
        exits="$(kubectl get pods -n "${local.ns}" \
          -o jsonpath='{range .items[*]}{.status.containerStatuses[*].lastState.terminated.exitCode}{"\n"}{end}' 2>/dev/null || true)"
        # Either signal will do, and they arrive in either order: the non-zero
        # exit is recorded the moment the container dies, the back-off reason
        # only once the kubelet declines to restart it immediately.
        if printf '%s\n' "$waiting" | grep -qxF 'CrashLoopBackOff' \
          || printf '%s\n' "$exits" | grep -qE '^[1-9][0-9]*$'; then
          break
        fi
        if [ "$elapsed" -ge ${local.crashloop_observe_timeout} ]; then
          echo "ERROR: no pod of ${local.workload} crash-looped within $${elapsed}s under runAsNonRoot: true with runAsUser: 1000, so the repair this case needs to be unavailable is in fact available. Check that ${var.workload_command} still fails for a non-root uid -- if it succeeds, the scenario has a third sound remediation and grading revert-against-codify is unsound (#3102). Current state follows." >&2
          kubectl get pods -n "${local.ns}" -o wide >&2 || true
          kubectl describe pods -n "${local.ns}" >&2 || true
          kubectl logs -n "${local.ns}" -l app="${local.workload}" --tail=20 --all-containers --ignore-errors >&2 || true
          exit 1
        fi
        sleep ${local.poll_interval}
        elapsed=$((elapsed + ${local.poll_interval}))
      done
      echo "Revision 2 confirmed failing: runAsUser 1000 cannot run ${var.workload_command}."

      # ---- 4. Make the out-of-band change real (revision 3) -----------------
      # The operator gives up on a non-root uid and pins the container to
      # root. Takes ${local.drifted_path} away from the GitOps manager under
      # ${var.drift_field_manager}; that transfer of ownership IS the drift,
      # and it is why the field manager is passed explicitly rather than left
      # to kubectl's default, whose name is an implementation detail this
      # stack would then be asserting on.
      #
      # runAsUser goes back to 0 in the same patch rather than being dropped.
      # A merge patch could drop it -- `"runAsUser": null` removes the key --
      # but 0 is the better scenario: an operator who has just watched uid
      # 1000 fail pins the container to root explicitly, and the live object
      # then says plainly what it runs as. Leaving 1000 in place is the one
      # option that does not work: the container would still be uid 1000,
      # still unable to write /etc, and the rollout below would time out.
      kubectl patch deployment "${local.workload}" -n "${local.ns}" \
        --field-manager="${var.drift_field_manager}" --type=merge \
        -p '{"spec":{"template":{"spec":{"securityContext":{"runAsNonRoot":false,"runAsUser":0}}}}}'

      if ! kubectl rollout status deployment/"${local.workload}" -n "${local.ns}" \
        --timeout=${local.rollout_ready_timeout}s; then
        echo "ERROR: ${local.workload} did not become available within ${local.rollout_ready_timeout}s after the loosening, so the live state is not healthy and the report's 'reverting would take this down' reasoning has nothing behind it." >&2
        kubectl get pods -n "${local.ns}" -o wide >&2 || true
        kubectl describe pods -n "${local.ns}" >&2 || true
        exit 1
      fi

      # ---- 5. Assert every claim the record is about to make ----------------
      # A server-side apply that silently fell back to a different manager, or
      # a patch that did not register one, would leave the card describing
      # ownership the agent cannot find when it reads the live object -- the
      # one failure that makes this case grade a report against a fiction, and
      # the one that would otherwise surface as a judge disagreeing with a
      # correct answer.
      managers="$(kubectl get deployment "${local.workload}" -n "${local.ns}" \
        -o jsonpath='{range .metadata.managedFields[*]}{.manager}{"\n"}{end}')"
      for expected in "${var.gitops_field_manager}" "${var.drift_field_manager}"; do
        if ! printf '%s\n' "$managers" | grep -qxF "$expected"; then
          echo "ERROR: ${local.workload} does not record field manager '$expected' after the plant, so the ownership the drift record claims is not on the live object. Managers found:" >&2
          printf '%s\n' "$managers" >&2
          exit 1
        fi
      done

      observed="$(kubectl get deployment "${local.workload}" -n "${local.ns}" \
        -o jsonpath='{.spec.template.spec.securityContext.runAsNonRoot}')"
      if [ "$observed" != "false" ]; then
        echo "ERROR: ${local.workload} reports runAsNonRoot='$observed', not the false the out-of-band edit should have left. The task's safeguard asserts false, so this run would red on a change the agent never made." >&2
        exit 1
      fi

      # The durable evidence, and the one the case leans on. Both superseded
      # ReplicaSets still carry their pod templates, and neither ever had a
      # ready replica. Unlike the events, this does not expire, so an eval
      # delayed past the event TTL still has a way to establish both that what
      # git declares did not run AND that the obvious repair was already
      # tried.
      #
      # readyReplicas is absent rather than 0 on a ReplicaSet that never came
      # up, so that field is not tested -- what is asserted is that the two
      # retired templates still exist to be read. The rendering is
      # name|runAsNonRoot|runAsUser per ReplicaSet, which makes the three
      # revisions "...|true|", "...|true|1000" and "...|false|0".
      revisions="$(kubectl get replicasets -n "${local.ns}" \
        -o jsonpath='{range .items[*]}{.metadata.name}{"|"}{.spec.template.spec.securityContext.runAsNonRoot}{"|"}{.spec.template.spec.securityContext.runAsUser}{"\n"}{end}')"

      if ! printf '%s\n' "$revisions" | grep -qE '\|true\|$'; then
        echo "ERROR: no ReplicaSet in ${local.ns} carries runAsNonRoot: true with no runAsUser, so the rollout history holds no record of what git declares and the only evidence left is the events, which expire. revisionHistoryLimit may have been lowered, or step 1 did not cut a ReplicaSet of its own. Revisions found:" >&2
        printf '%s\n' "$revisions" >&2
        exit 1
      fi

      # The one that makes revert-against-codify a real question rather than a
      # false binary: without this revision on the board, a reader proposing
      # runAsUser 1000 is proposing something untried and arguably better than
      # either option the card offers (#3102).
      if ! printf '%s\n' "$revisions" | grep -qF '|true|1000'; then
        echo "ERROR: no ReplicaSet in ${local.ns} carries runAsNonRoot: true with runAsUser: 1000, so the rollout history does not show the non-root repair having been tried, and an agent proposing it is proposing something this scenario has not ruled out. Revisions found:" >&2
        printf '%s\n' "$revisions" >&2
        exit 1
      fi
      echo "Planted ${local.resource_path}: three revisions -- declared (unrunnable), non-root uid (crash-looped), root (healthy)."

      # ---- 6. Hand the daemon the record ------------------------------------
      # Minted before the payload so the id the record carries and the id the
      # poll matches are the same value by construction.
      drift_mint_insert_id eval-drift-privilege

      # The record the detector would have built. `enriched` and the two
      # owners are the claim step 5 asserted: one manager holding the declared
      # spec and one holding the two securityContext fields it took. Every
      # value crosses as an argument, so none is interpolated into JSON text.
      drift_build_payload \
        "${var.drift_principal} patched ${local.resource_path} on cluster ${var.host_cluster_name}, fields owned by ${var.gitops_field_manager}, ${var.drift_field_manager}" \
        "${var.host_cluster_name}" \
        "$drift_project" \
        "${var.host_cluster_location}" \
        "${var.drift_principal}" \
        "kubectl/v1.31.0 (linux/amd64)" \
        "patch" \
        "io.k8s.apps.v1.deployments.patch" \
        "$drift_changed_at" \
        "$drift_insert_id" \
        "apps" "v1" "${local.ns}" "deployments" "${local.workload}" \
        "enriched" "false" \
        "${var.gitops_field_manager}|Apply|.spec.replicas,.spec.selector.matchLabels,.spec.template.spec.containers" \
        "${var.drift_field_manager}|Update|${local.drifted_path},${local.drifted_user_path}"

      drift_inject_record

      # ---- 7. Wait for the card ---------------------------------------------
      drift_wait_for_card
    EOT
  }

  # Namespace-scoped by design, and this is the half that keeps it that way on
  # the success path. It does NOT cover the failure path, which is why the plant
  # above cleans up after itself: Terraform taints a resource whose create-time
  # provisioner failed and skips destroy-time provisioners on a tainted
  # resource, so `teardown: true` reaches a `tofu destroy` that reports
  # "1 destroyed" without running a line of this.
  #
  # The drift record itself needs no teardown. It is a row in
  # `intercepted_events` and a card on the board, both of which belong to the
  # install rather than to the cluster, and both of which the next run's insert
  # id makes invisible to it.
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

      kubectl delete namespace "${self.triggers.namespace}" --ignore-not-found --wait=false
    EOT
  }
}

# Passed straight through: the subject cluster already exists and this stack is
# not building one. devops-bench calls deployer.get_cluster_info()
# unconditionally after up() for any non-noop deployer, and TFDeployer reads
# these two outputs and hands them to GCPProvider.ensure_cluster_credentials --
# omitting them raises ConfigError. Here that call is wanted rather than merely
# tolerated: it leaves the ambient kubeconfig pointed at the host cluster, which
# is the cluster the task's safeguards read.
output "cluster_name" {
  value = var.host_cluster_name
}

output "cluster_location" {
  value = var.host_cluster_location
}
