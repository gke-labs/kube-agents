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

# The scenario driver for bench/tasks/gitops-drift-out-of-band-triage.
#
# It plants a real out-of-band change on the host cluster and then hands the
# Session KV daemon the audit record that change would have produced, as a
# `gitops-drift` inject. The daemon claims its alert quota, writes its ledger
# row, posts the chat alert and starts the front-door turn that files the triage
# card; the task's prompt then reads that card off the board. What is under test
# is the daemon end of the chain and everything downstream of it.
#
# WHY THE RECORD IS PLANTED RATHER THAN OBSERVED. In production the record
# arrives by a longer route: the API server writes an audit entry, a logging
# sink exports it to Pub/Sub, and the drift-detector process pulls it, joins the
# live object's field ownership onto it and posts it here. This stack starts at
# the last step for two reasons, and each of them is a reason the longer route
# could not be driven from a bench fixture at all.
#
# The first is the detector's own classifier. It drops principals it judges to
# be automation before anything reaches the daemon, and every principal a bench
# fixture can authenticate as is a *.gserviceaccount.com one -- the runner's, or
# the node's. A change this stack made with kubectl would be classified as
# automation and correctly dropped, so the scenario would plant a defect the
# pipeline is designed never to report and then time out waiting for a card
# nobody filed. Posting the record directly is not a way around the classifier;
# it is the only way to hand the daemon the kind of record a person makes.
#
# The second is the sink. The audit route needs a logging sink, a Pub/Sub topic
# and a subscription on the leased project, none of which the eval install
# provisions, and an export lag measured in minutes on top of the run's budget.
#
# So the planted change and the planted record are two halves of one fixture and
# they have to agree: step 1 makes the change real -- the Deployment is applied
# by one field manager and its replica count taken by another, in managedFields,
# on the live object -- and step 3 sends the record that change would have
# produced. The card tells the agent to read the live object before concluding
# anything, and when it does it finds exactly what the record describes.
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

  # Kept in one place because the plant, the poll and the teardown all name
  # them, and a rename that reaches two of the three fails as a timeout rather
  # than as an error.
  ns       = var.drift_namespace
  workload = var.workload_name

  # The object path the daemon renders from the payload's `resource` block
  # (_drift_resource_path in agents/platform/scripts/session_kv_server.py:
  # namespace/resource/name). Composed here so the summary this stack writes
  # and the card the daemon renders name the object the same way.
  resource_path = "${var.drift_namespace}/deployments/${var.workload_name}"

  ci_labels = {
    "managed-by"  = "kube-agents-bench"
    "build-id"    = var.prow_build_id != "" ? var.prow_build_id : "local"
    "pull-number" = var.prow_pull_number != "" ? var.prow_pull_number : "none"
  }
}

resource "null_resource" "drift" {
  triggers = {
    # Destroy-time provisioners may read only `self`, so everything the
    # teardown needs is copied in here -- including the cluster coordinates,
    # because the destroy has to fetch its own credentials for the same reason
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
      tostring(var.declared_replicas),
      tostring(var.live_replicas),
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
      # Fetched here rather than inherited, because the ambient current-context
      # is NOT the host cluster by the time this apply runs. hack/ci-eval-pr.sh
      # points it there once before the task loop, and then devops-bench moves
      # it: evalharness/default.py calls deployer.get_cluster_info()
      # unconditionally after up(), TFDeployer's implementation hands the
      # stack's cluster_name output to GCPProvider.ensure_cluster_credentials,
      # and that shells `gcloud container clusters get-credentials` with no
      # --kubeconfig. Any deployer: tofu task earlier in the matrix therefore
      # leaves its own per-run cluster selected. The same argument and the same
      # remedy as bench/tf/prebuilt/autoops-incident step 0.
      #
      # A directory, not `mktemp` on its own: get-credentials refuses to load an
      # existing empty file, so it warns, writes a dated `.backup` beside it that
      # nothing then cleans up, and prints a WARNING that reads like a failure in
      # a CI log. Handing it a path that does not exist yet skips all three.
      kubeconfig_dir="$(mktemp -d)"

      # A plant that fails must not leave ${local.ns} behind, because the
      # leftover is what breaks the NEXT run -- see step 0b. The teardown at the
      # bottom of this file cannot be what prevents it: Terraform taints a
      # resource whose create-time provisioner failed and skips destroy-time
      # provisioners on a tainted resource, so the destroy reports "1 destroyed"
      # without running a line of it.
      #
      # `set +e` is load-bearing for the reason hack/ci-eval-pr.sh gives at its
      # own EXIT trap -- errexit stays in force inside a trap, so the first
      # command that failed would abort it and skip the delete. Capturing
      # `status` first keeps the script's own exit code intact.
      #
      # Guarded on `planted_ns`, which step 1 sets immediately before it creates
      # the namespace, so this cleans up only a namespace this run made. Until
      # KUBECONFIG points at the host cluster a kubectl here would run against
      # the ambient context, which is often another task's per-run cluster; and
      # step 0b exits non-zero on a ${local.ns} it found already there and
      # unlabelled, so deleting from here would undo the one refusal 0b exists
      # to make.
      planted_ns=""
      on_exit() {
        status=$?
        set +e
        if [ "$status" -ne 0 ] && [ -n "$planted_ns" ]; then
          echo "Plant failed (exit $status). State of ${local.ns} before cleanup:" >&2
          ${local.kubectl} get deployments -o wide >&2
          echo "Deleting ${local.ns} so the next run starts from a clean namespace." >&2
          kubectl delete namespace "${local.ns}" --ignore-not-found --wait=false >&2
        fi
        rm -rf "$kubeconfig_dir"
      }
      trap on_exit EXIT

      # A Prow deadline delivers SIGTERM, and bash does not run an EXIT trap
      # when the shell dies from an untrapped signal -- so without this the
      # deadline kill leaks the namespace exactly as a failed plant would.
      # Converting the signal to an exit is the same one-liner
      # hack/ci-eval-pr.sh uses at its own trap, for the same reason.
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
      # Step 1 is idempotent, so against a namespace that already holds an
      # identical Deployment the server-side apply reports no change and the
      # managedFields assertions below pass on the PREVIOUS run's ownership --
      # a plant that appears to succeed while having changed nothing. Deleting
      # first is what makes the case recover on its own, and it is also the only
      # thing that can: the run that leaked the namespace is over, and nothing
      # else visits these clusters between runs.
      #
      # 180s is well clear of the ~35s a namespace holding one busybox
      # Deployment takes to go, and short enough that a namespace genuinely
      # wedged on a finalizer fails here, naming the real problem, rather than
      # later as a mystery timeout.
      #
      # Gated on the label step 1 writes, not on the name alone. This is the
      # only unconditional namespace delete in the stack and it runs against the
      # shared cluster the Platform Agent install lives on, so it has to be able
      # to say the namespace is ours. A namespace of this name that we did not
      # plant is a stop, not a target.
      if kubectl get namespace "${local.ns}" >/dev/null 2>&1; then
        leftover_owner="$(kubectl get namespace "${local.ns}" \
          -o jsonpath='{.metadata.labels.managed-by}' 2>/dev/null || true)"
        if [ "$leftover_owner" != "${local.ci_labels["managed-by"]}" ]; then
          echo "ERROR: ${local.ns} already exists on ${var.host_cluster_name} but is not labelled managed-by=${local.ci_labels["managed-by"]} (found '$leftover_owner'). This stack did not create it, so it will not delete it. Remove it by hand if it is stale." >&2
          exit 1
        fi
        echo "Found a leftover ${local.ns} from an earlier run; deleting it before planting."
        # --ignore-not-found because the destroy provisioner deletes with
        # --wait=false, so a namespace can be mid-deletion when the get above
        # sees it and gone by the time this runs. Without it that race reports
        # as the finalizer wedge below, which it is not.
        if ! kubectl delete namespace "${local.ns}" --ignore-not-found --wait=true --timeout=180s; then
          echo "ERROR: could not clear the leftover ${local.ns} within 180s, so this run cannot plant fresh field ownership. A namespace wedged on a finalizer is the usual cause; an RBAC or API failure lands here too. Its current state follows." >&2
          kubectl get namespace "${local.ns}" -o yaml >&2 || true
          exit 1
        fi
      fi

      # ---- 1. Make the change real ------------------------------------------
      # Two writes, deliberately by two field managers.
      #
      # The first is a server-side apply as ${var.gitops_field_manager}, which
      # is what a GitOps controller reconciling this manifest would look like in
      # managedFields: that manager owns the whole declared spec, replica count
      # included.
      #
      # The second takes `.spec.replicas` away from it under
      # ${var.drift_field_manager}. That transfer of ownership IS the drift --
      # it is the thing the detector's join reads off the live object and the
      # thing the report has to weigh -- and it is why the replica count is
      # changed with an explicit --field-manager rather than with `kubectl
      # scale`, whose manager name is a kubectl implementation detail this stack
      # would then be asserting on.
      #
      # Set before the create, not after: a create that half-succeeds, or a
      # label call that fails behind one that did not, still leaves a namespace
      # this run is responsible for removing.
      planted_ns=1
      kubectl create namespace "${local.ns}" --dry-run=client -o yaml | kubectl apply -f -
      kubectl label namespace "${local.ns}" --overwrite \
        managed-by="${local.ci_labels["managed-by"]}" \
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
        replicas: ${var.declared_replicas}
        selector:
          matchLabels:
            app: ${local.workload}
        template:
          metadata:
            labels:
              app: ${local.workload}
          spec:
            automountServiceAccountToken: false
            containers:
              - name: web
                image: busybox:1.36
                command: ["sh", "-c", "sleep 3600"]
                resources:
                  requests:
                    cpu: 10m
                    memory: 16Mi
                  limits:
                    memory: 32Mi
      MANIFEST

      kubectl patch deployment "${local.workload}" -n "${local.ns}" \
        --field-manager="${var.drift_field_manager}" --type=merge \
        -p '{"spec":{"replicas":${var.live_replicas}}}'

      # The record this stack is about to send claims both managers hold fields
      # on this object. Asserted rather than assumed, because a server-side
      # apply that silently fell back to a different manager, or a patch that
      # did not register one, would leave the card describing ownership the
      # agent cannot find when it reads the live object -- the one failure that
      # makes this case grade a report against a fiction, and the one that would
      # otherwise surface as a judge disagreeing with a correct answer.
      managers="$(kubectl get deployment "${local.workload}" -n "${local.ns}" \
        -o jsonpath='{range .metadata.managedFields[*]}{.manager}{"\n"}{end}')"
      for expected in "${var.gitops_field_manager}" "${var.drift_field_manager}"; do
        if ! printf '%s\n' "$managers" | grep -qxF "$expected"; then
          echo "ERROR: ${local.workload} does not record field manager '$expected' after the plant, so the ownership the drift record claims is not on the live object. Managers found:" >&2
          printf '%s\n' "$managers" >&2
          exit 1
        fi
      done

      observed_replicas="$(kubectl get deployment "${local.workload}" -n "${local.ns}" \
        -o jsonpath='{.spec.replicas}')"
      if [ "$observed_replicas" != "${var.live_replicas}" ]; then
        echo "ERROR: ${local.workload} is at $observed_replicas replicas, not the ${var.live_replicas} the out-of-band edit should have left. The task's safeguards assert ${var.live_replicas}, so this run would red on a change the agent never made." >&2
        exit 1
      fi
      echo "Planted ${local.resource_path}: declared ${var.declared_replicas} by ${var.gitops_field_manager}, live ${var.live_replicas} under ${var.drift_field_manager}."

      # ---- 2. Refuse a daemon that cannot take the record -------------------
      # The same check the detector makes at startup, and for the same reason:
      # a daemon predating the `gitops-drift` dispatch takes the payload down
      # its event path instead, grades it as a Warning Pod alert for `default/`,
      # answers 200, and files a card that has nothing to do with drift. Every
      # objective below would then fail against a card that looks plausible in
      # the transcript, which is the worst way for a fixture to be wrong.
      #
      # /healthz is unauthenticated, so this runs before the key is needed and
      # separates "the install is too old" from "the key is wrong".
      pod="deployment/${var.agent_deployment}"
      exec_in_pod=(kubectl exec "$pod" -n "${var.agent_namespace}" -c "${var.agent_container}" --)
      exec_in_pod_stdin=(kubectl exec -i "$pod" -n "${var.agent_namespace}" -c "${var.agent_container}" --)

      # jq runs inside the pod rather than on the runner, here and in the two
      # calls below. The agent image installs it (deploy/docker/Dockerfile's
      # agent-base layer); the Prow runner this stack's local-exec runs on has
      # no jq in any of hack/'s scripts, so assuming one would be a dependency
      # nothing else in bench/tf takes.
      kinds="$("$${exec_in_pod[@]}" sh -c 'curl -sS --max-time 10 "$1/healthz" | jq -r ".inject_kinds[]?"' sh "${var.daemon_url}" || true)"
      if ! printf '%s\n' "$kinds" | grep -qxF 'gitops-drift'; then
        echo "ERROR: the Session KV daemon at ${var.daemon_url} does not advertise the 'gitops-drift' inject kind (it advertises: $kinds). This install predates the drift dispatch, so a record sent to it would be graded as a Warning Pod event and reported back as delivered. Redeploy the agent image before running this case." >&2
        exit 1
      fi

      # ---- 3. Hand the daemon the record ------------------------------------
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

      # Minted per apply, and the card poll in step 4 matches on it. That is
      # what makes the poll run-scoped without a per-run object name: the card
      # body renders `insertId=<this>`, so a card left on the board by an
      # earlier run against this same namespace cannot satisfy it.
      insert_id="eval-drift-$(date -u +%s)-$RANDOM"
      changed_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

      # The payload the detector would have built (payloadForEvent in
      # k8s-operator/cmd/drift-detector/inject.go), including the one-line
      # `summary` it composes -- written out here rather than left to the
      # daemon's fallback, because a real record always carries one and the
      # fallback renders a different sentence.
      #
      # `join: enriched` and the ownership below are the claim step 1 asserted:
      # two managers, one holding the declared spec and one holding the replica
      # count it took.
      payload_file="$kubeconfig_dir/payload.json"
      cat > "$payload_file" <<PAYLOAD
      {
        "kind": "gitops-drift",
        "summary": "${var.drift_principal} patch ${local.resource_path} on cluster ${var.host_cluster_name}, fields owned by ${var.gitops_field_manager}, ${var.drift_field_manager}",
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
            "paths": [".spec.template.spec.containers", ".spec.selector.matchLabels", ".metadata.labels.app"]
          },
          {
            "manager": "${var.drift_field_manager}",
            "operation": "Update",
            "paths": [".spec.replicas"]
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
      # it and told nobody, because the day's drift alert ceiling
      # (ALERT_DAILY_LIMIT_DRIFT, 5 by default) is already gone -- so no card is
      # ever filed and the case would time out looking for one. Three
      # repetitions fit under the ceiling; a fourth run against the same install
      # on the same day does not, which is what this message is for.
      if [ "$inject_status" = "suppressed" ]; then
        echo "ERROR: the daemon suppressed this record against its daily drift alert ceiling, so no triage card will be filed. This install has already taken its drift alerts for the day; raise ALERT_DAILY_LIMIT_DRIFT or run against a fresh install." >&2
        exit 1
      fi
      if [ "$inject_status" != "injected" ]; then
        echo "ERROR: the daemon answered '$inject_status' rather than 'injected'. An empty status means the request did not reach the drift route at all." >&2
        exit 1
      fi
      echo "Daemon accepted the drift record (insert_id=$insert_id)."

      # ---- 4. Wait for the card ---------------------------------------------
      # The inject returns as soon as the daemon has queued the front-door turn,
      # and that turn -- one kanban_create against the cluster agent scoped to
      # ${var.host_cluster_name} -- is what puts the card on the board. Without
      # this wait the agent turn starts first, finds an empty board, and the
      # case grades a correct agent on a fixture that had not finished.
      #
      # Read out of the board's SQLite file rather than from a log line, which
      # is the one place this stack is coupled to an internal path.
      # prebuilt/autoops-incident does not have to be: the event watcher logs
      # its `fire` to the container's stdout, so `kubectl logs` is a real seam
      # one layer earlier. There is no equivalent here -- the daemon logs to a
      # file inside the pod rather than to stdout, and the front door's
      # kanban_create produces no external line -- so the alternatives were this
      # or a fixed sleep, and a sleep cannot say what was missing when it is
      # wrong.
      #
      # Matched on the run's own insert id, which _drift_task_body renders into
      # the card body verbatim as `insertId=<id>` -- and which survives that
      # rendering, because the defanging it passes through removes only
      # backticks and newlines (_DRIFT_UNSAFE_CHARS_RE). Not on the title: the
      # front door composes that and could reword it, and it carries no id.
      #
      # The probe searches every text column rather than naming one, because
      # the board's schema is upstream Hermes' (`hermes_cli/kanban_db.py`) and
      # is not in this repository to check a column name against. A column
      # rename upstream would turn a named query into a poll that never matches
      # and reports as a five-minute timeout; a rename cannot hide the string
      # from this one.
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
      for candidate in python3 /opt/hermes/.venv/bin/python3; do
        if "$${exec_in_pod[@]}" "$candidate" -c 'pass' >/dev/null 2>&1; then
          pod_python="$candidate"
          break
        fi
      done
      if [ -z "$pod_python" ]; then
        echo "ERROR: no python interpreter found in ${var.agent_container} to read the kanban board with." >&2
        exit 1
      fi

      # 300s, and the shape of the wait rather than its length is what matters:
      # the front-door turn is one kanban_create, so the card appears in
      # seconds when the daemon is working and never when it is not. Anything
      # past a minute here is already the failure, and the ceiling only decides
      # how long the run pays for it before saying so.
      #
      # The count is read into a variable and checked for shape before `-ge`
      # sees it. `[ "" -ge 1 ]` is a syntax error under errexit, and a
      # kubectl exec that fails halfway -- an evicted pod, an API blip -- can
      # return partial output as easily as none, so a bare command
      # substitution in the test would turn a transport failure into an abort
      # with no message about what was actually being waited for.
      elapsed=0
      while :; do
        found="$("$${exec_in_pod_stdin[@]}" "$pod_python" - "$insert_id" < "$board_probe" 2>/dev/null || true)"
        case "$found" in
          "" | *[!0-9]*) found=0 ;;
        esac
        if [ "$found" -ge 1 ]; then
          break
        fi
        if [ "$elapsed" -ge 300 ]; then
          echo "ERROR: the daemon accepted the record but no kanban card carrying insert_id=$insert_id appeared within $${elapsed}s. The front-door turn the daemon queued either never ran or filed nothing, so there is no triage for the agent to read." >&2
          exit 1
        fi
        sleep 10
        elapsed=$((elapsed + 10))
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
  # It fetches its own credentials because get_cluster_info() runs after up()
  # and can itself fail; on that path the create-time provisioner succeeded, so
  # the resource is not tainted and this block does run, with whatever cluster
  # the previous task left selected. Deleting a namespace by name on the wrong
  # cluster is the kind of thing --ignore-not-found makes survivable rather than
  # safe.
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
