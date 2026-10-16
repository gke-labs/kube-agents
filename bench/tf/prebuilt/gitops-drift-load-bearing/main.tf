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
  # Every kubectl below runs against a kubeconfig this stack fetches for itself
  # in step 0, never the ambient one -- see that step for why the ambient
  # current-context is the wrong cluster by the time this apply runs.
  kubectl = "kubectl --namespace=${var.drift_namespace}"

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
    # step 0 does and cannot reach `var`.
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

      # ---- 0. Point kubectl at the cluster the agent is on ------------------
      # Fetched here rather than inherited: devops-bench calls
      # deployer.get_cluster_info() unconditionally after up(), which shells
      # `gcloud container clusters get-credentials` with no --kubeconfig, so any
      # deployer: tofu task earlier in the matrix leaves its own per-run cluster
      # selected. ../gitops-drift step 0 carries the argument in full.
      kubeconfig_dir="$(mktemp -d)"

      # A plant that fails must not leave ${local.ns} behind: the leftover is
      # what breaks the NEXT run. The teardown at the bottom of this file
      # cannot be what prevents it -- Terraform taints a resource whose
      # create-time provisioner failed and skips destroy-time provisioners on a
      # tainted resource, so the destroy reports "1 destroyed" without running
      # a line of it.
      #
      # `set +e` is load-bearing: errexit stays in force inside a trap, so the
      # first command that failed would abort it and skip the delete.
      planted_ns=""
      on_exit() {
        status=$?
        set +e
        if [ "$status" -ne 0 ] && [ -n "$planted_ns" ]; then
          echo "Plant failed (exit $status). State of ${local.ns} before cleanup:" >&2
          ${local.kubectl} get deployments,replicasets,pods -o wide >&2
          ${local.kubectl} get events --sort-by=.metadata.creationTimestamp >&2
          echo "Deleting ${local.ns} so the next run starts from a clean namespace." >&2
          kubectl delete namespace "${local.ns}" --ignore-not-found --wait=false >&2
        fi
        rm -rf "$kubeconfig_dir"
      }
      trap on_exit EXIT

      # A Prow deadline delivers SIGTERM, and bash does not run an EXIT trap
      # when the shell dies from an untrapped signal -- so without this the
      # deadline kill leaks the namespace exactly as a failed plant would.
      trap 'exit 143' TERM INT

      KUBECONFIG="$kubeconfig_dir/config"
      export KUBECONFIG

      project="${var.project_id}"
      if [ -z "$project" ]; then
        # GCPProvider.resolve_variables injects project_id for every task, so
        # this is the local-run path rather than the CI one.
        project="$(gcloud config get-value project 2>/dev/null || true)"
      fi
      if [ -z "$project" ]; then
        echo "ERROR: no project id. Pass -var project_id=... or set a gcloud default project; this stack needs one to fetch credentials for ${var.host_cluster_name}." >&2
        exit 1
      fi

      gcloud container clusters get-credentials "${var.host_cluster_name}" \
        --location "${var.host_cluster_location}" --project "$project" --quiet

      # ---- 0b. Clear what an earlier run left behind ------------------------
      # This stack is MORE sensitive to a leftover than its siblings, because
      # its evidence is a rollout history. Against a namespace that already
      # holds the finished scenario the apply in step 1 would be a no-op, the
      # patches in steps 3 and 4 would change nothing, no new ReplicaSet would
      # be cut, and the case would grade an agent on the previous run's events --
      # which may by then have expired, leaving a scenario with no evidence in
      # it at all.
      #
      # Gated on the label step 1 writes, not on the name alone: this is the
      # only unconditional namespace delete in the stack and it runs against
      # the shared cluster the Platform Agent install lives on.
      if kubectl get namespace "${local.ns}" >/dev/null 2>&1; then
        # Not swallowed: the enclosing `if` has already proved the namespace
        # exists, so a failing read here is a transport or RBAC failure and
        # not an absent label. Swallowed, it becomes an empty value that the
        # branch below reports as "this stack did not create it" -- a refusal
        # with the wrong reason, and indistinguishable from a namespace that
        # genuinely carries no label.
        leftover_owner="$(kubectl get namespace "${local.ns}" \
          -o jsonpath='{.metadata.labels.managed-by}')"
        if [ "$leftover_owner" != "${local.ci_labels["managed-by"]}" ]; then
          echo "ERROR: ${local.ns} already exists on ${var.host_cluster_name} but is not labelled managed-by=${local.ci_labels["managed-by"]} (found '$leftover_owner'). This stack did not create it, so it will not delete it. Remove it by hand if it is stale." >&2
          exit 1
        fi
        echo "Found a leftover ${local.ns} from an earlier run; deleting it before planting."
        if ! kubectl delete namespace "${local.ns}" --ignore-not-found --wait=true --timeout=${local.leftover_delete_timeout}s; then
          echo "ERROR: could not clear the leftover ${local.ns} within ${local.leftover_delete_timeout}s, so this run cannot plant a fresh rollout history. A namespace wedged on a finalizer is the usual cause. Its current state follows." >&2
          kubectl get namespace "${local.ns}" -o yaml >&2 || true
          exit 1
        fi
      fi

      # ---- 0c. Refuse a daemon that cannot take the record ------------------
      # A daemon predating the `gitops-drift` dispatch takes the payload down
      # its event path instead, grades it as a Warning Pod alert for `default/`,
      # answers 200, and files a card that has nothing to do with drift. Every
      # objective would then fail against a card that looks plausible in the
      # transcript, which is the worst way for a fixture to be wrong.
      #
      # BEFORE THE PLANT, as gitops-drift-deleted-policy also does. This
      # reads nothing the plant produces -- only the kubeconfig from step 0
      # and four plain variables -- and this stack's plant is the expensive
      # half of the file: three observed rollouts, two of them waiting out a
      # failure. Probing afterwards would spend those minutes, let the exit
      # trap tear the namespace back down, and then report a condition that
      # was knowable in ten seconds.
      #
      # The merged ../gitops-drift still probes after its plant, and there it
      # costs little: that plant is an apply, a patch and two reads with no
      # wait. It is not a precedent for a stack that waits.
      pod="deployment/${var.agent_deployment}"
      exec_in_pod=(kubectl exec "$pod" -n "${var.agent_namespace}" -c "${var.agent_container}" --)
      exec_in_pod_stdin=(kubectl exec -i "$pod" -n "${var.agent_namespace}" -c "${var.agent_container}" --)

      # jq runs inside the pod rather than on the runner: the agent image
      # installs it, and the Prow runner has no jq in any of hack/'s scripts.
      # `curl -fsS` and the body captured before jq sees it. Without -f, curl
      # prints an error body and exits 0 on a 404; and inside `sh -c 'curl |
      # jq'` the pipeline's status is JQ's, which is 0 on empty input -- so a
      # connection refused, a wrong daemon_url or a timeout all produced an
      # empty `kinds` with a zero status, and the branch below blamed the
      # install's age for a daemon that was simply down. Measured: that
      # pipeline exits 0 with curl failing.
      kinds_rc=0
      kinds="$("$${exec_in_pod[@]}" sh -c 'body="$(curl -fsS --max-time 10 "$1/healthz")" || exit $?; printf "%s" "$body" | jq -r ".inject_kinds[]?"' sh "${var.daemon_url}")" || kinds_rc=$?
      if [ "$kinds_rc" -ne 0 ]; then
        echo "ERROR: could not read ${var.daemon_url}/healthz in ${var.agent_container} (exit $kinds_rc); kubectl's or curl's own error is above. This says nothing about which inject kinds the daemon advertises -- it is an exec, RBAC, connection or HTTP failure, and the install may be perfectly current." >&2
        exit 1
      fi
      if ! printf '%s\n' "$kinds" | grep -qxF 'gitops-drift'; then
        echo "ERROR: the Session KV daemon at ${var.daemon_url} does not advertise the 'gitops-drift' inject kind (it advertises: $kinds). This install predates the drift dispatch, so a record sent to it would be graded as a Warning Pod event and reported back as delivered. Redeploy the agent image before running this case." >&2
        exit 1
      fi

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
      planted_ns=1
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
      # The key is read from the pod's own environment inside the exec, never
      # passed in: SESSION_KV_API_KEY is on this container and a value that
      # crossed the runner would be one argv away from a CI log.
      session_id="$("$${exec_in_pod[@]}" sh -c \
        'curl -sS --max-time 10 -X POST -H "Authorization: Bearer $SESSION_KV_API_KEY" "$1/sessions" | jq -r ".sessionID // empty"' \
        sh "${var.daemon_url}" || true)"
      if [ -z "$session_id" ]; then
        echo "ERROR: the daemon returned no sessionID. A 401 here means SESSION_KV_API_KEY on ${var.agent_container} does not match what the daemon expects; a 503 means it is unset." >&2
        exit 1
      fi

      # Minted per apply, and the card poll in step 7 matches on it. That is
      # what makes the poll run-scoped without a per-run object name.
      insert_id="eval-drift-privilege-$(date -u +%s)-$RANDOM"
      changed_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

      # The payload the detector would have built (payloadForEvent in
      # k8s-operator/cmd/drift-detector/inject.go). `join: enriched` and the
      # ownership below are the claim step 5 asserted: two managers, one
      # holding the declared spec and one holding the field it took.
      payload_file="$kubeconfig_dir/payload.json"
      cat > "$payload_file" <<PAYLOAD
      {
        "kind": "gitops-drift",
        "summary": "${var.drift_principal} patched ${local.resource_path} on cluster ${var.host_cluster_name}, fields owned by ${var.gitops_field_manager}, ${var.drift_field_manager}",
        "cluster": "${var.host_cluster_name}",
        "project": "$project",
        "location": "${var.host_cluster_location}",
        "principal": "${var.drift_principal}",
        "user_agent": "kubectl/v1.31.0 (linux/amd64)",
        "verb": "patch",
        "method_name": "io.k8s.apps.v1.deployments.patch",
        "timestamp": "$changed_at",
        "insert_id": "$insert_id",
        "resource": {
          "group": "apps",
          "version": "v1",
          "namespace": "${local.ns}",
          "resource": "deployments",
          "name": "${local.workload}"
        },
        "join": "enriched",
        "owners": [
          {
            "manager": "${var.gitops_field_manager}",
            "operation": "Apply",
            "paths": [".spec.replicas", ".spec.selector.matchLabels", ".spec.template.spec.containers"]
          },
          {
            "manager": "${var.drift_field_manager}",
            "operation": "Update",
            "paths": ["${local.drifted_path}", "${local.drifted_user_path}"]
          }
        ],
        "reconciled": false
      }
      PAYLOAD

      # The daemon's envelope carries the payload as a JSON *string*, not as a
      # nested object (injectMessageRequest in the detector's inject.go). Built
      # with python rather than by hand so the escaping is the language's
      # problem and not a quoting bug that surfaces as a 400.
      envelope_file="$kubeconfig_dir/envelope.json"
      python3 -c 'import json,sys; sys.stdout.write(json.dumps({"message": open(sys.argv[1]).read()}))' \
        "$payload_file" > "$envelope_file"

      inject_status="$("$${exec_in_pod_stdin[@]}" sh -c \
        'curl -sS --max-time 30 -X POST -H "Authorization: Bearer $SESSION_KV_API_KEY" -H "Content-Type: application/json" --data-binary @- "$1" | jq -r ".status // empty"' \
        sh "${var.daemon_url}/sessions/$session_id/inject" < "$envelope_file" || true)"

      # `suppressed` is a 200. The daemon accepted the record, spent nothing on
      # it and told nobody, because the day's drift alert ceiling is already
      # gone -- so no card is ever filed and the case would time out looking
      # for one.
      #
      # This does NOT bite in CI: hack/ci-deploy.sh sets
      # EVAL_ALERT_DAILY_LIMIT_DRIFT=0 and _alert_daily_limit treats 0 as off,
      # so an eval install has no drift ceiling at all (the sibling case
      # gitops-drift-noise-filtered-triage carries the same note). It bites on
      # a DEV install carrying the shipped default, where this case shares
      # the bucket with every other drift case -- which is the install the
      # eval-driven loop runs against, and the one the message below is for.
      if [ "$inject_status" = "suppressed" ]; then
        echo "ERROR: the daemon suppressed this record against its daily drift alert ceiling, so no triage card will be filed. This install has already taken its drift alerts for the day -- the other gitops-drift cases spend from the same bucket. Raise ALERT_DAILY_LIMIT_DRIFT or run against a fresh install." >&2
        exit 1
      fi
      if [ "$inject_status" != "injected" ]; then
        echo "ERROR: the daemon answered '$inject_status' rather than 'injected'. An empty status means the request did not reach the drift route at all." >&2
        exit 1
      fi
      echo "Daemon accepted the drift record (insert_id=$insert_id)."

      # ---- 7. Wait for the card ---------------------------------------------
      # The inject returns as soon as the daemon has queued the front-door turn,
      # and that turn -- one kanban_create against the cluster agent scoped to
      # ${var.host_cluster_name} -- is what puts the card on the board. Without
      # this wait the agent turn starts first, finds an empty board, and the
      # case grades a correct agent on a fixture that had not finished.
      #
      # Read out of the board's SQLite file rather than from a log line, which
      # is the one place this stack is coupled to an internal path; there is no
      # external seam, because the daemon logs to a file inside the pod and the
      # front door's kanban_create produces no line of its own.
      #
      # Matched on the run's own insert id, which _drift_task_body renders into
      # the card body verbatim as `insertId=<id>` and which survives the
      # defanging it passes through (_DRIFT_UNSAFE_CHARS_RE removes only
      # backticks and newlines). Not on the title: the front door composes that
      # and could reword it, and it carries no id.
      #
      # The probe searches every text column rather than naming one, because
      # the board's schema is upstream Hermes' and is not in this repository to
      # check a column name against.
      board_probe="$kubeconfig_dir/board_probe.py"
      cat > "$board_probe" <<'PROBE'
      import sqlite3
      import sys

      BOARD = "file:/opt/data/kanban.db?mode=ro"

      # Read-only URI so the probe cannot create the file, add a journal beside
      # it, or block the agent writing to it. A board that is not there yet is
      # a 0, not a traceback: the caller polls.
      try:
          conn = sqlite3.connect(BOARD, uri=True)
          columns = [
              row[1]
              for row in conn.execute("PRAGMA table_info(tasks)")
              if (row[2] or "").upper().startswith("TEXT")
          ]
          if not columns:
              print(0)
              raise SystemExit(0)
          needle = "%" + sys.argv[1] + "%"
          where = " OR ".join(f'"{c}" LIKE ?' for c in columns)
          found = conn.execute(
              f"SELECT COUNT(*) FROM tasks WHERE {where}", [needle] * len(columns)
          ).fetchone()[0]
          print(found)
      except sqlite3.Error:
          print(0)
      PROBE

      # Resolved rather than assumed: the daemon runs under the Hermes venv's
      # interpreter, and whether a bare `python3` is also on PATH in this
      # container is an image detail that should not be what this case fails on.
      pod_python=""
      # `import sqlite3`, not `pass`: the probe's own import sits outside its
      # try block, so an interpreter that satisfies `pass` but has no
      # _sqlite3 module would be picked, fail on every poll, and surface
      # 300 seconds later as "the front-door turn filed nothing".
      for candidate in python3 /opt/hermes/.venv/bin/python3; do
        if "$${exec_in_pod[@]}" "$candidate" -c 'import sqlite3' >/dev/null 2>&1; then
          pod_python="$candidate"
          break
        fi
      done
      if [ -z "$pod_python" ]; then
        echo "ERROR: no python interpreter found in ${var.agent_container} to read the kanban board with." >&2
        exit 1
      fi

      # The count is read into a variable and checked for shape before `-ge`
      # sees it. `[ "" -ge 1 ]` is a syntax error under errexit, and a kubectl
      # exec that fails halfway can return partial output as easily as none.
      elapsed=0
      while :; do
        found="$("$${exec_in_pod_stdin[@]}" "$pod_python" - "$insert_id" < "$board_probe" 2>/dev/null || true)"
        case "$found" in
          "" | *[!0-9]*) found=0 ;;
        esac
        if [ "$found" -ge 1 ]; then
          break
        fi
        if [ "$elapsed" -ge ${local.card_wait_timeout} ]; then
          echo "ERROR: the daemon accepted the record but no kanban card carrying insert_id=$insert_id appeared within $${elapsed}s. The front-door turn the daemon queued either never ran or filed nothing, so there is no triage for the agent to read." >&2
          exit 1
        fi
        sleep ${local.poll_interval}
        elapsed=$((elapsed + ${local.poll_interval}))
      done
      echo "Triage card for $insert_id appeared after $${elapsed}s."

      # The card then takes minutes to run. Waiting for THAT here would spend
      # the wait outside the agent turn for no gain; the task's prompt polls the
      # board with kanban_show instead, which is the read path the product
      # actually offers.
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
