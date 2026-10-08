# The fixture for gitops-drift-noise-filtered-triage.
#
# A burst of writes lands across two namespaces: eleven by controllers and
# service accounts, one by a person. Twelve synthetic Cloud Audit Log records
# go to the drift-audit topic, one per write, and the detector pulls them,
# classifies them, and should file a card for the human one alone.
#
# WHY THE RECORDS GO TO PUB/SUB AND NOT THE DAEMON
#
# The sibling stack (../gitops-drift) posts straight to the Session KV
# daemon's `gitops-drift` inject. That starts at the last step of the chain,
# which is sound for a case grading delivery and useless for this one: a
# record handed to the daemon was classified by whoever wrote the fixture.
# Here Classify is the thing under test, so the records arrive the way a real
# audit record does -- on the subscription the detector pulls.
#
# A real cluster write cannot do this job either. The Log Router export is
# minutes on top of the run's budget, and every identity a bench run can
# authenticate as ends .gserviceaccount.com, which Classify is right to drop
# before it ever consults isHuman. So the writes below are real (the
# managedFields join reads the live objects) and the records about them are
# synthetic.
#
# WHAT THE DETECTOR NEEDS EACH RECORD TO CARRY
#
# audit.go's parse, exactly: insertId, timestamp, resource.labels naming the
# HOST cluster, and a protoPayload carrying principalEmail, methodName and
# resourceName. Two of those are easy to get wrong and silent when wrong --
# resource.labels, because a cluster the joiner holds no credentials for is
# forwarded without ownership and the card reads "Field ownership: not read";
# and status, because tally tests Succeeded() before it tests the tier
# (classify.go), so a record marked failed is dropped for the outcome and
# never reaches the human/automation decision this case grades.

locals {
  # Every kubectl runs against a kubeconfig this stack fetches for itself in
  # step 0, never the ambient one: the current-context is not the host cluster
  # by the time this apply runs. The sibling's step 0 says why at length.
  kubectl = "kubectl"

  ci_labels = {
    "managed-by"  = "kube-agents-bench"
    "case"        = "gitops-drift-noise-filtered-triage"
    "build-id"    = var.prow_build_id != "" ? var.prow_build_id : "local"
    "pull-number" = var.prow_pull_number != "" ? var.prow_pull_number : "none"
  }

  label_args = join(" ", [for k, v in local.ci_labels : "${k}=${v}"])

  # The ConfigMap the stack writes its own observations into, read by the
  # fixture-observed-the-filter-held safeguard. A ConfigMap rather than a
  # non-zero exit: a tofu apply that fails under a non-noop deployer is
  # classified as an infrastructure failure and the repetition is excluded
  # from the verdict (scoring.py, _provision_death), which would throw away
  # the one observation of the regression nothing else can make.
  # Name, namespace and schema are the safeguard's, not this stack's choosing:
  # fixture-observed-the-filter-held reads data.verdict on this ConfigMap and
  # reds the case on any token but "ok". Rename either side and the check
  # errors instead of running, which the eval gate treats as an absolute rung
  # failure rather than a result.
  verdict_configmap = "eval-drift-fixture-verdict"
  verdict_namespace = var.churn_namespace

  # Read-only, and the same path and mode the sibling stack opens the board
  # with. `tasks.body` is the column _drift_task_body renders `insertId=<id>`
  # into, which is what makes the poll below run-scoped.
  kanban_board = "/opt/data/kanban.db?mode=ro"
}

resource "null_resource" "drift_noise" {
  triggers = {
    # Destroy-time provisioners may read only `self`, so everything the
    # teardown names is copied in here -- including the cluster coordinates,
    # because the destroy fetches its own credentials for the same reason
    # step 0 does and cannot reach `var`.
    human_namespace = var.human_namespace
    churn_namespace = var.churn_namespace
    host_cluster    = var.host_cluster_name
    host_location   = var.host_cluster_location
    host_project    = var.project_id
    agent_container = var.agent_container
    verdict_cm      = local.verdict_configmap

    # Re-plant when the scenario changes. Without this the resource is inert
    # after the first apply and a local run that edited the scenario would
    # keep scoring the old one. Inert in CI, where state starts fresh.
    scenario = sha256(join("|", concat([
      var.human_namespace,
      var.churn_namespace,
      var.human_workload,
      var.human_container,
      var.gitops_field_manager,
      var.drift_field_manager,
      var.declared_memory,
      var.drifted_memory,
      var.human_principal,
      tostring(var.churn_record_count),
      var.host_cluster_name,
    ], var.churn_workloads, var.churn_principals)))
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
      set -euo pipefail

      # ---- 0. Point kubectl at the cluster the agent is on ------------------
      # Fetched rather than inherited: devops-bench moves the ambient
      # current-context after every `deployer: tofu` task in the matrix
      # (evalharness/default.py calls get_cluster_info() unconditionally after
      # up()). The sibling's step 0 carries the full argument.
      #
      # A directory rather than `mktemp` on its own: get-credentials refuses to
      # load an existing empty file, warns, and writes a stray .backup beside
      # it. Handing it a path that does not exist yet skips all of that.
      kubeconfig_dir="$(mktemp -d)"

      planted=""
      on_exit() {
        status=$?
        set +e
        if [ "$status" -ne 0 ] && [ -n "$planted" ]; then
          echo "Plant failed (exit $status). Namespaces before cleanup:" >&2
          ${local.kubectl} get deployments -n "${var.human_namespace}" -o wide >&2
          ${local.kubectl} get deployments -n "${var.churn_namespace}" -o wide >&2
          echo "Deleting both namespaces so the next run starts clean." >&2
          ${local.kubectl} delete namespace "${var.human_namespace}" "${var.churn_namespace}" \
            --ignore-not-found --wait=false >&2
        fi
        rm -rf "$kubeconfig_dir"
      }
      trap on_exit EXIT
      # A Prow deadline delivers SIGTERM, and bash runs no EXIT trap when the
      # shell dies from an untrapped signal, so without this the deadline kill
      # leaks both namespaces exactly as a failed plant would.
      trap 'exit 143' TERM INT

      KUBECONFIG="$kubeconfig_dir/config"
      export KUBECONFIG

      project="${var.project_id}"
      if [ -z "$project" ]; then
        project="$(gcloud config get-value project 2>/dev/null || true)"
      fi
      if [ -z "$project" ]; then
        echo "ERROR: no project_id variable and no gcloud default project." >&2
        exit 1
      fi

      gcloud container clusters get-credentials "${var.host_cluster_name}" \
        --region "${var.host_cluster_location}" --project "$project" --quiet

      # ---- 0b. Refuse a namespace this run did not make ---------------------
      # A leftover from a previous run holds objects whose managedFields and
      # memory limit are already what this run is about to assert, so the
      # grading would pass on last run's state. Deleting someone else's
      # namespace is worse, so this refuses rather than cleans.
      for ns in "${var.human_namespace}" "${var.churn_namespace}"; do
        if ${local.kubectl} get namespace "$ns" >/dev/null 2>&1; then
          owner="$(${local.kubectl} get namespace "$ns" \
            -o jsonpath='{.metadata.labels.managed-by}' 2>/dev/null || true)"
          if [ "$owner" != "kube-agents-bench" ]; then
            echo "ERROR: namespace $ns exists and is not labelled managed-by=kube-agents-bench." >&2
            echo "       Refusing to touch it. Remove it by hand if it is stale." >&2
            exit 1
          fi
          ${local.kubectl} delete namespace "$ns" --wait=true --timeout=120s
        fi
      done

      # ---- 1. Plant the namespaces and the three Deployments ----------------
      planted=1
      for ns in "${var.human_namespace}" "${var.churn_namespace}"; do
        ${local.kubectl} create namespace "$ns"
        ${local.kubectl} label namespace "$ns" ${local.label_args} --overwrite
      done

      # The declared spec is applied server-side AS ${var.gitops_field_manager},
      # which is what puts that name in the object's managedFields. The report
      # has to name it as the manager the drifting write took the field from,
      # and the join reads it off the live object rather than off the record.
      plant_deployment() {
        ns="$1"; name="$2"; container="$3"; memory="$4"
        cat <<YAML | ${local.kubectl} apply --server-side \
          --field-manager="${var.gitops_field_manager}" -n "$ns" -f -
      apiVersion: apps/v1
      kind: Deployment
      metadata:
        name: $name
        labels:
          managed-by: kube-agents-bench
      spec:
        replicas: 1
        selector:
          matchLabels:
            app: $name
        template:
          metadata:
            labels:
              app: $name
          spec:
            containers:
              - name: $container
                image: registry.k8s.io/pause:3.10
                resources:
                  limits:
                    memory: "$memory"
      YAML
      }

      plant_deployment "${var.human_namespace}" "${var.human_workload}" \
        "${var.human_container}" "${var.declared_memory}"
      %{for w in var.churn_workloads~}
      plant_deployment "${var.churn_namespace}" "${w}" "app" "${var.declared_memory}"
      %{endfor~}

      # ---- 2. The one human change ------------------------------------------
      # A server-side apply, not a patch: --force-conflicts is an apply flag
      # (`kubectl patch` rejects it outright), and taking the field from
      # ${var.gitops_field_manager} is the point rather than a side effect.
      # The manifest carries only the field being claimed, so SSA leaves the
      # rest of the spec owned by the GitOps manager and the join can say who
      # took what from whom -- which is what expected_output requires the
      # report to name.
      cat <<YAML | ${local.kubectl} apply --server-side \
        --field-manager="${var.drift_field_manager}" --force-conflicts \
        -n "${var.human_namespace}" -f -
      apiVersion: apps/v1
      kind: Deployment
      metadata:
        name: ${var.human_workload}
      spec:
        template:
          spec:
            containers:
              - name: ${var.human_container}
                resources:
                  limits:
                    memory: "${var.drifted_memory}"
      YAML

      # ---- 3. Twelve synthetic audit records --------------------------------
      # Every insertId is minted per apply. Distinct-within-the-run is NOT
      # enough: driftInjectHandler.Handle calls seen.Add(record.InsertID)
      # before the send (inject.go) on a set that lives as long as the detector
      # process, so fixed churn ids would be marked seen on repetition 1 and
      # dropped by the detector itself from repetition 2 on -- which turns a
      # broken filter green on two repetitions out of three. The sibling's
      # form, eval-drift-$(date -u +%s)-$RANDOM.
      mint_id() { echo "eval-drift-$(date -u +%s)-$RANDOM"; }

      publish_record() {
        insert_id="$1"; principal="$2"; ns="$3"; workload="$4"; agent="$5"
        # status.code 0 is a success. tally tests Succeeded() before it tests
        # the tier (classify.go), so a record that looks failed is dropped for
        # the outcome and never reaches the decision this case grades.
        payload="$(cat <<JSON
      {
        "insertId": "$insert_id",
        "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
        "resource": {
          "type": "k8s_cluster",
          "labels": {
            "project_id": "$project",
            "location": "${var.host_cluster_location}",
            "cluster_name": "${var.host_cluster_name}"
          }
        },
        "protoPayload": {
          "serviceName": "k8s.io",
          "methodName": "io.k8s.apps.v1.deployments.patch",
          "resourceName": "apps/v1/namespaces/$ns/deployments/$workload",
          "authenticationInfo": { "principalEmail": "$principal" },
          "requestMetadata": { "callerSuppliedUserAgent": "$agent" },
          "status": { "code": 0 }
        }
      }
      JSON
      )"
        gcloud pubsub topics publish "${var.drift_topic}" --project "$project" \
          --message="$payload" >/dev/null
      }

      # The human record first, so its id is the oldest of the twelve. The
      # poll below is keyed on it either way; publishing it first is what makes
      # an arrival-ordering assumption unnecessary.
      human_insert_id="$(mint_id)"
      publish_record "$human_insert_id" "${var.human_principal}" \
        "${var.human_namespace}" "${var.human_workload}" "kubectl-edit/v1.31.0"
      echo "human record: insertId=$human_insert_id"

      churn_principals=(%{for p in var.churn_principals}"${p}" %{endfor})
      churn_workloads=(%{for w in var.churn_workloads}"${w}" %{endfor})
      churn_ids=()
      i=0
      while [ "$i" -lt "${var.churn_record_count}" ]; do
        id="$(mint_id)"
        churn_ids+=("$id")
        publish_record "$id" \
          "$${churn_principals[$(( i % $${#churn_principals[@]} ))]}" \
          "${var.churn_namespace}" \
          "$${churn_workloads[$(( i % $${#churn_workloads[@]} ))]}" \
          "kube-controller-manager/v1.31.0"
        i=$(( i + 1 ))
        # Distinct seconds in the minted ids, and a gentler publish rate than
        # a tight loop would give the subscription.
        sleep 1
      done
      echo "churn records: $${#churn_ids[@]} published"

      # ---- 4. Record what the fixture itself observed -----------------------
      # Written before the poll, with verdict=ok, so the safeguard always has
      # something to read: a check that cannot find its ConfigMap errors
      # rather than runs, and the eval gate treats an errored verifier as an
      # absolute rung failure instead of a result. Step 5 rewrites the verdict
      # if the poll finds nothing.
      #
      # The churn ids ride along because the quota-independent check keys on
      # them: after the settle, any intercepted_events row whose object_uid is
      # one of THIS run's churn insertIds proves Classify forwarded churn,
      # whatever the alert bucket held.
      write_verdict() {
        ${local.kubectl} create configmap "${local.verdict_configmap}" \
          -n "${local.verdict_namespace}" \
          --from-literal=verdict="$1" \
          --from-literal=human_insert_id="$human_insert_id" \
          --from-literal=churn_insert_ids="$(IFS=,; echo "$${churn_ids[*]}")" \
          --from-literal=drifted_memory="${var.drifted_memory}" \
          --dry-run=client -o yaml | ${local.kubectl} apply -f - >/dev/null
        ${local.kubectl} label configmap "${local.verdict_configmap}" \
          -n "${local.verdict_namespace}" ${local.label_args} --overwrite >/dev/null
      }
      write_verdict ok

      # ---- 5. Wait for the card carrying the human record's insertId --------
      # The board is shared and persists between runs, and the plant is
      # identical every repetition, so without a run-scoped poll the agent can
      # reproduce the PREVIOUS repetition's card and pass every check on the
      # repetition this case exists to red. _drift_task_body renders the id
      # into the card body verbatim (`insertId=<id>`, session_kv_server.py),
      # which is what makes the literal run-scoped.
      pod="$(${local.kubectl} get pod -n kubeagents-system \
        -l app=platform-agent-gateway -o jsonpath='{.items[0].metadata.name}')"
      if [ -z "$pod" ]; then
        echo "ERROR: no platform-agent-gateway pod to read the kanban board from." >&2
        exit 1
      fi

      # The board, not the filesystem. An earlier revision grepped /opt/data
      # and returned as soon as anything there mentioned the id -- which the
      # daemon's own request dump and state.db both do the moment the inject
      # is ACCEPTED, minutes before the front-door turn files a card. That is
      # a false positive on exactly the failure this poll exists to catch: the
      # inject landing and no card following it. Query kanban.db instead, the
      # way the sibling stack does, so "found" means a card and nothing else.
      deadline=$(( SECONDS + ${var.poll_timeout_seconds} ))
      found=""
      while [ "$SECONDS" -lt "$deadline" ]; do
        # One line on purpose. A multi-line `python3 -c` here would sit at
        # column 0, and Terraform's <<- strips the SMALLEST indentation it
        # finds across the whole template -- so a single unindented line
        # cancels the dedent for every other line, leaving the YAML
        # terminators above indented and bash reading to end-of-file looking
        # for them. That failure is invisible to `terraform validate`, which
        # does not parse the shell this block generates.
        # Terminal status, not mere existence. The agent is one replica, and
        # the card's own worker holds it for the minutes it takes to produce
        # the report. Returning as soon as the card appears hands the task to
        # a harness that then opens its turn against a busy agent, and the
        # agent API answers 502 -- which the runner classifies as
        # KUBE_AGENTS_INFRA_FAILURE and excludes the repetition, so the case
        # scores nothing rather than failing. Waiting for the worker to let go
        # costs the agent's own wait loop (the prompt's step 2 finds the card
        # already finished) and buys a repetition that actually counts. The
        # run-scoping the poll exists for is unaffected: it is still this
        # run's insertId that is being waited on.
        if ${local.kubectl} exec -n kubeagents-system "$pod" \
          -c "${var.agent_container}" -- python3 -c "import sqlite3,sys; sys.exit(0 if sqlite3.connect('file:${local.kanban_board}',uri=True).execute(\"select count(*) from tasks where body like ? and status in ('done','blocked','archived')\", ('%'+sys.argv[1]+'%',)).fetchone()[0] else 1)" \
          "$human_insert_id" >/dev/null 2>&1; then
          found=1
          break
        fi
        sleep 15
      done

      if [ -z "$found" ]; then
        # Record the finding and exit 0, rather than failing the apply. A tofu
        # apply that exits non-zero under a non-noop deployer is classified as
        # an infrastructure failure (scoring.py, _provision_death) and the
        # repetition is EXCLUDED from the verdict -- which would throw away
        # the one observation nothing else can make. The safeguard reads the
        # token below and reds the case properly instead.
        #
        # Which failure it was: no card splits two ways and one of them is the
        # regression this case exists to catch, so the detector's own
        # --log-dropped line is what separates "the classifier refused it"
        # from "it never arrived". That flag is off on eval installs by
        # design -- one line per dropped record is tens of thousands per
        # lease, on every lease -- so the operator is told to turn it on and
        # re-run rather than it being left on for everyone.
        write_verdict "no-card-for-human-record"
        echo "no card carrying insertId=$human_insert_id after ${var.poll_timeout_seconds}s." >&2
        echo "Detector lines mentioning this run's human record:" >&2
        ${local.kubectl} logs -n kubeagents-system "$pod" -c agent-api-auth --tail=2000 2>/dev/null \
          | grep -F "$human_insert_id" >&2 || echo "  (none -- the record did not reach the detector)" >&2
        echo "A 'dropped tier=... reason=...' line naming this id is the classifier refusing it," >&2
        echo "and that is the regression. No line at all means the record never arrived." >&2
        echo "Re-run with DRIFT_DETECTOR_LOG_DROPPED=true on the install for that line to exist." >&2
        exit 0
      fi

      echo "card carrying insertId=$human_insert_id is on the board"
    EOT
  }

  # ---- Teardown -------------------------------------------------------------
  # Reads only `self`, which is why the triggers above carry the coordinates.
  provisioner "local-exec" {
    when        = destroy
    on_failure  = continue
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
      set -uo pipefail
      kubeconfig_dir="$(mktemp -d)"
      trap 'rm -rf "$kubeconfig_dir"' EXIT
      KUBECONFIG="$kubeconfig_dir/config"
      export KUBECONFIG
      gcloud container clusters get-credentials "${self.triggers.host_cluster}" \
        --region "${self.triggers.host_location}" --project "${self.triggers.host_project}" \
        --quiet || exit 0
      kubectl delete namespace "${self.triggers.human_namespace}" \
        "${self.triggers.churn_namespace}" --ignore-not-found --wait=false
    EOT
  }
}

# What the harness reads back. TFDeployer calls get_cluster_info() after up()
# and hands these to GCPProvider.ensure_cluster_credentials, so a stack
# without them fails the run before the agent is ever asked anything
# ("Failed to retrieve 'cluster_name' from TF outputs"). They echo the host
# cluster this stack was pointed at rather than naming one of their own: this
# fixture plants into the install the runner deployed and creates no cluster.
output "cluster_name" {
  value = var.host_cluster_name
}

output "cluster_location" {
  value = var.host_cluster_location
}
