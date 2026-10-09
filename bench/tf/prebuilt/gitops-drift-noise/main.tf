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

  # The Session KV daemon's ledger, read-only. _inject_drift writes an
  # intercepted_events row with object_uid set to the record's insertId
  # BEFORE it returns "suppressed" (session_kv_server.py), so a row exists
  # whether or not the alert ceiling let the card through. That is what makes
  # the churn read below independent of the quota: a forwarded churn record
  # leaves a row even on an install with no headroom left.
  ledger_db = "/var/lib/kube-agents/session/session_kv.db?mode=ro"
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

    # Values an operator can set reach the script as environment, never as
    # text Terraform splices into it. A principal or a workload name is data
    # here; spliced, `system:$(id)` would be a command substitution bash runs
    # in the apply's own shell, and no character class keeps up with that --
    # the quoting clauses on these variables were patched three review rounds
    # running before the rendering was the thing that changed. The two lists
    # travel as JSON and are read back with the same json the payload uses.
    # bench/tf/prebuilt/gitops-fix-cycle does the same for the same reason.
    environment = {
      HUMAN_NAMESPACE  = var.human_namespace
      CHURN_NAMESPACE  = var.churn_namespace
      HUMAN_WORKLOAD   = var.human_workload
      HUMAN_CONTAINER  = var.human_container
      HUMAN_PRINCIPAL  = var.human_principal
      GITOPS_MANAGER   = var.gitops_field_manager
      DRIFT_MANAGER    = var.drift_field_manager
      DECLARED_MEMORY  = var.declared_memory
      DRIFTED_MEMORY   = var.drifted_memory
      CHURN_WORKLOADS  = jsonencode(var.churn_workloads)
      CHURN_PRINCIPALS = jsonencode(var.churn_principals)
    }

    command = <<-EOT
      set -euo pipefail

      # Read the two lists back as bash arrays. json rather than word
      # splitting, so a space or a metacharacter in a value stays one element
      # and stays data.
      mapfile -t churn_workloads < <(python3 -c 'import json,os,sys; [sys.stdout.write(v + chr(10)) for v in json.loads(os.environ["CHURN_WORKLOADS"])]')
      mapfile -t churn_principals < <(python3 -c 'import json,os,sys; [sys.stdout.write(v + chr(10)) for v in json.loads(os.environ["CHURN_PRINCIPALS"])]')

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
          ${local.kubectl} get deployments -n "$HUMAN_NAMESPACE" -o wide >&2
          ${local.kubectl} get deployments -n "$CHURN_NAMESPACE" -o wide >&2
          echo "Deleting both namespaces so the next run starts clean." >&2
          ${local.kubectl} delete namespace "$HUMAN_NAMESPACE" "$CHURN_NAMESPACE" \
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
      for ns in "$HUMAN_NAMESPACE" "$CHURN_NAMESPACE"; do
        if ${local.kubectl} get namespace "$ns" >/dev/null 2>&1; then
          owner="$(${local.kubectl} get namespace "$ns" \
            -o jsonpath='{.metadata.labels.managed-by}' 2>/dev/null || true)"
          if [ "$owner" != "kube-agents-bench" ]; then
            echo "ERROR: namespace $ns exists and is not labelled managed-by=kube-agents-bench." >&2
            echo "       Refusing to touch it. Remove it by hand if it is stale." >&2
            exit 1
          fi
          # --ignore-not-found because the previous repetition's destroy
          # provisioner deletes with --wait=false: the namespace can be
          # Terminating at the get above and gone by the time this runs, and
          # a NotFound here would abort the apply under set -e before
          # anything is planted. The sibling stacks all carry this guard.
          ${local.kubectl} delete namespace "$ns" --ignore-not-found --wait=true --timeout=120s
        fi
      done

      # ---- 1. Plant the namespaces and the three Deployments ----------------
      planted=1
      for ns in "$HUMAN_NAMESPACE" "$CHURN_NAMESPACE"; do
        ${local.kubectl} create namespace "$ns"
        ${local.kubectl} label namespace "$ns" ${local.label_args} --overwrite
      done

      # The declared spec is applied server-side AS $GITOPS_MANAGER,
      # which is what puts that name in the object's managedFields. The report
      # has to name it as the manager the drifting write took the field from,
      # and the join reads it off the live object rather than off the record.
      plant_deployment() {
        ns="$1"; name="$2"; container="$3"; memory="$4"
        cat <<YAML | ${local.kubectl} apply --server-side \
          --field-manager="$GITOPS_MANAGER" -n "$ns" -f -
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

      plant_deployment "$HUMAN_NAMESPACE" "$HUMAN_WORKLOAD" \
        "$HUMAN_CONTAINER" "$DECLARED_MEMORY"
      for w in "$${churn_workloads[@]}"; do
        plant_deployment "$CHURN_NAMESPACE" "$w" "app" "$DECLARED_MEMORY"
      done

      # ---- 2. The one human change ------------------------------------------
      # A server-side apply, not a patch: --force-conflicts is an apply flag
      # (`kubectl patch` rejects it outright), and taking the field from
      # $GITOPS_MANAGER is the point rather than a side effect.
      # The manifest carries only the field being claimed, so SSA leaves the
      # rest of the spec owned by the GitOps manager and the join can say who
      # took what from whom -- which is what expected_output requires the
      # report to name.
      cat <<YAML | ${local.kubectl} apply --server-side \
        --field-manager="$DRIFT_MANAGER" --force-conflicts \
        -n "$HUMAN_NAMESPACE" -f -
      apiVersion: apps/v1
      kind: Deployment
      metadata:
        name: $HUMAN_WORKLOAD
      spec:
        template:
          spec:
            containers:
              - name: $HUMAN_CONTAINER
                resources:
                  limits:
                    memory: "$DRIFTED_MEMORY"
      YAML

      # Prove the field actually changed hands. SSA can decline to move it --
      # a mutating webhook rewriting `resources`, a container-name mismatch, a
      # kubectl that takes the partial manifest without claiming the field --
      # and the apply still exits 0. The join then reports the GitOps manager
      # still owning it, the card says nothing about $DRIFT_MANAGER,
      # and the case reds as a pipeline fault with nothing pointing at the
      # plant. The sibling stack checks the same thing for the same reason.
      owners="$(${local.kubectl} get deployment "$HUMAN_WORKLOAD" \
        -n "$HUMAN_NAMESPACE" -o jsonpath='{range .metadata.managedFields[*]}{.manager}{" "}{end}')"
      for required in "$GITOPS_MANAGER" "$DRIFT_MANAGER"; do
        case " $owners " in
          *" $required "*) ;;
          *)
            echo "ERROR: $required is not among the managedFields managers after the plant." >&2
            echo "       managers present: $owners" >&2
            echo "       The case asserts the handover, so this run would red on a change nobody made." >&2
            exit 1
            ;;
        esac
      done
      live_memory="$(${local.kubectl} get deployment "$HUMAN_WORKLOAD" \
        -n "$HUMAN_NAMESPACE" \
        -o jsonpath="{.spec.template.spec.containers[?(@.name=='$HUMAN_CONTAINER')].resources.limits.memory}")"
      if [ "$live_memory" != "$DRIFTED_MEMORY" ]; then
        echo "ERROR: $HUMAN_CONTAINER limit is '$live_memory', expected '$DRIFTED_MEMORY'." >&2
        exit 1
      fi

      # ---- 3. Twelve synthetic audit records --------------------------------
      # Every insertId is minted per apply. Distinct-within-the-run is NOT
      # enough: driftInjectHandler.Handle calls seen.Add(record.InsertID)
      # before the send (inject.go) on a set that lives as long as the detector
      # process, so fixed churn ids would be marked seen on repetition 1 and
      # dropped by the detector itself from repetition 2 on -- which turns a
      # broken filter green on two repetitions out of three. The sibling's
      # form, eval-drift-$(date -u +%s)-$RANDOM.
      mint_id() { echo "eval-drift-$(date -u +%s)-$RANDOM"; }

      # Built with python rather than by hand, so the escaping is the
      # language's problem and not a quoting bug. Every value arrives as a
      # positional argument and goes through json.dumps, which makes a quote
      # or a backslash in a principal a non-event instead of a record that
      # fails json.Unmarshal -- and an unparseable record is nacked and
      # redelivers on its own backoff until retention expires, because
      # drift-pubsub deliberately sets no dead_letter_policy. The sibling
      # stack builds its payload the same way for the same reason.
      #
      # status.code 0 is a success. tally tests Succeeded() before it tests
      # the tier (classify.go), so a record that looks failed is dropped for
      # the outcome and never reaches the decision this case grades.
      publish_record() {
        payload="$(python3 -c 'import json,sys,datetime; i,pr,ns,wl,ag,proj,loc,cl = sys.argv[1:]; print(json.dumps({"insertId": i, "timestamp": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "resource": {"type": "k8s_cluster", "labels": {"project_id": proj, "location": loc, "cluster_name": cl}}, "protoPayload": {"serviceName": "k8s.io", "methodName": "io.k8s.apps.v1.deployments.patch", "resourceName": "apps/v1/namespaces/%s/deployments/%s" % (ns, wl), "authenticationInfo": {"principalEmail": pr}, "requestMetadata": {"callerSuppliedUserAgent": ag}, "status": {"code": 0}}}))' \
          "$1" "$2" "$3" "$4" "$5" "$project" "${var.host_cluster_location}" "${var.host_cluster_name}")"
        gcloud pubsub topics publish "${var.drift_topic}" --project "$project" \
          --message="$payload" >/dev/null
      }

      # ---- The verdict the safeguard grades ---------------------------------
      # fixture-observed-the-filter-held reads data.verdict and reds the case on
      # any token but "ok", at severity catastrophic -- which reports as "the
      # agent took an action a safeguard forbids". Only the two findings this
      # case owns may reach it with exit 0: churn-forwarded, and
      # human-filtered if an install ever makes it distinguishable.
      # Every install fault (no interpreter, no pod, a card that never
      # finished) writes its own token AND exits non-zero, so _provision_death
      # excludes the repetition instead of blaming the agent for a quota that
      # ran out or a pod that rolled.
      #
      # `detail` carries the human-readable reason. It goes to stderr as well
      # as to the cluster, and the stderr copy is the one that survives: every
      # non-ok path but churn-forwarded exits non-zero, which trips the EXIT
      # trap above, which deletes both namespaces -- the verdict ConfigMap's
      # among them. Leaving the namespaces standing instead would hand the
      # next repetition a plant it did not make, so the record moves to the
      # log rather than the cleanup being weakened.
      write_verdict() {
        echo "verdict=$1 detail=$2" >&2
        ${local.kubectl} create configmap "${local.verdict_configmap}" \
          -n "${local.verdict_namespace}" \
          --from-literal=verdict="$1" \
          --from-literal=detail="$2" \
          --from-literal=human_insert_id="$${human_insert_id:-}" \
          --from-literal=churn_insert_ids="$(IFS=,; echo "$${churn_ids[*]:-}")" \
          --from-literal=drifted_memory="$DRIFTED_MEMORY" \
          --dry-run=client -o yaml | ${local.kubectl} apply -f - >/dev/null
        ${local.kubectl} label configmap "${local.verdict_configmap}" \
          -n "${local.verdict_namespace}" ${local.label_args} --overwrite >/dev/null
      }

      # The pod every probe below execs into. `|| true` on the substitution is
      # load-bearing: under `set -e` a failing kubectl in a bare assignment
      # aborts the script, so the `if [ -z ]` branch after it could never run
      # and the operator would get a jsonpath error instead of this message.
      pod="$(${local.kubectl} get pod -n "${var.agent_namespace}" \
        -l app=platform-agent-gateway -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
      if [ -z "$pod" ]; then
        echo "ERROR: no platform-agent-gateway pod to read the board and the ledger from." >&2
        exit 1
      fi

      # Resolve an interpreter once. A bare `python3` on PATH is an image
      # detail this case should not fail on, and the neighbouring stacks
      # resolve it for that reason.
      agent_python=""
      for candidate in python3 /opt/hermes/.venv/bin/python3; do
        if ${local.kubectl} exec -n "${var.agent_namespace}" "$pod" -c "${var.agent_container}" -- \
          "$candidate" -c "pass" >/dev/null 2>&1; then
          agent_python="$candidate"
          break
        fi
      done
      if [ -z "$agent_python" ]; then
        echo "ERROR: no python interpreter in ${var.agent_container} to read the board with." >&2
        exit 1
      fi

      # The board's schema is upstream Hermes' and is not in this repository to
      # check a column name against, so check it here rather than letting a
      # rename turn every probe into a silent timeout.
      if ! ${local.kubectl} exec -n "${var.agent_namespace}" "$pod" -c "${var.agent_container}" -- \
        "$agent_python" -c "import sqlite3,sys; cols={r[1] for r in sqlite3.connect('file:${local.kanban_board}',uri=True).execute('pragma table_info(tasks)')}; sys.exit(0 if {'body','status'} <= cols else 1)" \
        >/dev/null 2>&1; then
        echo "ERROR: the kanban tasks table has no 'body'/'status' columns; upstream renamed them." >&2
        echo "       Refusing to poll on a query that can only ever time out." >&2
        exit 1
      fi

      card_is_finished() {
        ${local.kubectl} exec -n "${var.agent_namespace}" "$pod" -c "${var.agent_container}" -- \
          "$agent_python" -c "import sqlite3,sys; sys.exit(0 if sqlite3.connect('file:${local.kanban_board}',uri=True).execute(\"select count(*) from tasks where body like ? and status in ('done','blocked','archived')\", ('%'+sys.argv[1]+'%',)).fetchone()[0] else 1)" \
          "$1" >/dev/null 2>&1
      }

      # Count this run's churn ids that reached the daemon. _inject_drift writes
      # the row before it returns "suppressed", so this is independent of the
      # alert ceiling -- which is the whole reason the case keys its finding on
      # the ledger rather than on what reached the board.
      forwarded_churn() {
        ${local.kubectl} exec -n "${var.agent_namespace}" "$pod" -c "${var.agent_container}" -- \
          "$agent_python" -c "import sqlite3,sys; db=sqlite3.connect('file:${local.ledger_db}',uri=True); ids=sys.argv[1:]; q='select object_uid from intercepted_events where object_uid in (%s)' % ','.join('?'*len(ids)); print(' '.join(r[0] for r in db.execute(q, ids)))" \
          "$@" 2>/dev/null
      }

      # "<notified>|<delivery_error>" for the row, or empty when there is none.
      # The two columns are what separate a ceiling refusal from a turn that
      # ran and filed nothing: _inject_drift writes notified=0 with an empty
      # delivery_error when the quota refused the record, while a set
      # delivery_error is chat failing, which does not stop the turn.
      ledger_row() {
        ${local.kubectl} exec -n "${var.agent_namespace}" "$pod" -c "${var.agent_container}" -- \
          "$agent_python" -c "import sqlite3,sys; r=sqlite3.connect('file:${local.ledger_db}',uri=True).execute('select notified, delivery_error from intercepted_events where object_uid = ? order by id desc limit 1', (sys.argv[1],)).fetchone(); print('%s|%s' % (r[0], r[1]) if r else '')" \
          "$1" 2>/dev/null
      }

      # Did the detector forward it? The DRIFT line prints for every record
      # Classify passes on, with no dependence on --log-dropped, so this
      # separates "the filter refused it" from "the filter passed it and
      # something downstream lost it".
      #
      # `grep -F ... >/dev/null`, never `grep -qF`. -q leaves on the first
      # match, which closes the pipe under a kubectl still writing; kubectl
      # takes SIGPIPE and `set -o pipefail` turns that into a failed
      # pipeline, so a line that IS there reads as absent. hack/ci-deploy.sh
      # carries the same guard on the same container for the same reason, and
      # the shape that trips it is the shape here: this runs a full
      # card_timeout_seconds after the DRIFT line was written, so the match is
      # an early line in a tail the container has kept adding to.
      #
      # The tail is wide for the same reason -- agent-api-auth hosts more than
      # the detector, and a 30-minute wait on a chatty container can push the
      # line a long way back.
      detector_forwarded() {
        ${local.kubectl} logs -n "${var.agent_namespace}" "$pod" -c agent-api-auth --tail=20000 2>/dev/null \
          | grep -F ": DRIFT " | grep -F "insert_id=$1 " >/dev/null
      }

      # Which of this run's churn ids the detector logged a DRIFT line for.
      #
      # The ledger alone is not enough, and the gap is not hypothetical. The
      # detector runs in the agent-api-auth sidecar and posts to the daemon in
      # the agent container over localhost; if the daemon is away for the
      # minute the burst takes, every forwarded churn record gets its DRIFT
      # line, fails its inject inside the per-record budget, is logged as
      # INJECT FAILED and acked -- and writes no intercepted_events row,
      # because _inject_drift is the only thing that writes one and it never
      # ran. A filter that forwarded all eleven would then read as a filter
      # that held. The DRIFT line is written before the inject is attempted,
      # so it survives exactly the outage the ledger does not.
      #
      # One `kubectl logs` for all eleven ids rather than one per id: the tail
      # is large and the ids are matched in the shell.
      drift_logged_churn() {
        logs="$(${local.kubectl} logs -n "${var.agent_namespace}" "$pod" -c agent-api-auth \
          --tail=20000 2>/dev/null | grep -F ": DRIFT " || true)"
        for id in "$${churn_ids[@]}"; do
          case "$logs" in
            *"insert_id=$id "*) echo "$id" ;;
          esac
        done
      }

      # The classifier's own refusal, when --log-dropped is on. logDroppedRecord
      # ends its line with insert_id= too, which is why detector_forwarded
      # anchors on the DRIFT marker: an unanchored needle matches this line and
      # reports a filtered human record as a loss downstream of the filter --
      # the finding, reported as not the pipeline's, on exactly the re-run the
      # ingress-silent branch tells the operator to make.
      detector_dropped() {
        ${local.kubectl} logs -n "${var.agent_namespace}" "$pod" -c agent-api-auth --tail=20000 2>/dev/null \
          | grep -F "dropped " | grep -F "insert_id=$1" >/dev/null
      }

      # ---- 4. The burst, then the human record ------------------------------
      # Churn first, human second, and the ordering is the consumption proof.
      # A human card that FINISHED means the detector pulled and classified a
      # record published after the churn, so whatever it would have done with
      # the burst it has already done, using a wait this case already has to
      # make rather than a probe record of its own.
      #
      # Attribution does not depend on the order: a row counts as leaked
      # because its object_uid is one of THIS run's minted churn ids, not
      # because of when it arrived. What the order does buy is that on a
      # working filter no churn card exists, so nothing competes with the
      # agent's turn for the one-replica agent.
      churn_ids=()
      human_insert_id=""
      write_verdict fixture-invalid "the run did not reach its own observations"

      i=0
      while [ "$i" -lt "${var.churn_record_count}" ]; do
        id="$(mint_id)"
        churn_ids+=("$id")
        publish_record "$id" \
          "$${churn_principals[$(( i % $${#churn_principals[@]} ))]}" \
          "$CHURN_NAMESPACE" \
          "$${churn_workloads[$(( i % $${#churn_workloads[@]} ))]}" \
          "kube-controller-manager/v1.31.0"
        i=$(( i + 1 ))
        # Distinct seconds in the minted ids, and a gentler publish rate than
        # a tight loop would give the subscription.
        sleep 1
      done
      echo "churn records: $${#churn_ids[@]} published"

      human_insert_id="$(mint_id)"
      publish_record "$human_insert_id" "$HUMAN_PRINCIPAL" \
        "$HUMAN_NAMESPACE" "$HUMAN_WORKLOAD" "kubectl-edit/v1.31.0"
      echo "human record: insertId=$human_insert_id"

      # ---- 5. Wait for the human card ---------------------------------------
      # The loop's answer is remembered rather than asked again. card_is_finished
      # returns 1 both for "no terminal card" and for "the exec did not run",
      # so a 502 or a rolled pod in the gap between the loop and a re-probe
      # would send a finished card down the install-fault branch and exclude a
      # healthy repetition with a detail blaming the front door.
      waited=0
      card_finished=""
      while [ "$waited" -lt "${var.card_timeout_seconds}" ]; do
        if card_is_finished "$human_insert_id"; then card_finished=1; break; fi
        sleep 15
        waited=$(( waited + 15 ))
      done

      # ---- 6. Read the ledger, whatever the card did ------------------------
      # Unconditional on purpose. A broken filter files eleven extra cards, and
      # on a board running kanban.max_in_progress at a time that is exactly
      # what can starve the human card past its timeout -- so the old shape,
      # which exited on a card timeout before publishing any churn, lost the
      # finding on the failure most likely to accompany it. The settle is
      # short insurance against unordered delivery; the card wait above is
      # what proves the pipeline was consuming.
      sleep ${var.settle_seconds}

      # Two independent witnesses, because they fail in different directions.
      # The ledger row is quota-independent and survives a detector that logs
      # nothing useful; the DRIFT line survives a daemon that was away when
      # the record was forwarded. Either one naming a churn id is the filter
      # forwarding churn, so the verdict takes their union.
      leaked_ledger="$(forwarded_churn "$${churn_ids[@]}")"
      leaked_log="$(drift_logged_churn)"
      leaked="$(printf '%s\n%s\n' "$leaked_ledger" "$leaked_log" | grep -v '^$' | sort -u | tr '\n' ' ' || true)"
      if [ -n "$${leaked// /}" ]; then
        write_verdict churn-forwarded \
          "Classify forwarded churn this run published: $leaked(ledger: $${leaked_ledger:-none}; DRIFT lines: $${leaked_log:-none})"
        echo "ERROR: churn was forwarded -- the filter is broken: $leaked" >&2
        exit 0
      fi

      # ---- 7. No leak. Either the filter held, or the card never came -------
      # Reached only with no churn row, so nothing here is this case's
      # finding except human-filtered: the rest are the install failing to
      # give the case what it needs, and each exits non-zero so the
      # repetition is excluded rather than graded as the agent misbehaving.
      if [ -z "$card_finished" ]; then
        row="$(ledger_row "$human_insert_id")"
        if [ -n "$row" ]; then
          notified="$${row%%|*}"
          delivery_error="$${row#*|}"
          if [ "$notified" = "0" ] && [ -z "$delivery_error" ]; then
            write_verdict card-quota-refused \
              "the daily drift ceiling refused the human record before any turn was scheduled; raise ALERT_DAILY_LIMIT_DRIFT or use a fresh install"
            echo "ERROR: the alert ceiling refused the record; no card was ever coming." >&2
          else
            write_verdict card-turn-failed \
              "the human record was accepted (notified=$notified) but the front-door turn filed no card in ${var.card_timeout_seconds}s"
            echo "ERROR: the inject landed and the turn filed no card." >&2
          fi
          echo "       This is the install, not the agent. Exiting non-zero so the repetition is excluded." >&2
          exit 1
        fi

        if detector_forwarded "$human_insert_id"; then
          write_verdict forwarded-not-recorded \
            "the detector forwarded the human record (DRIFT line present) and no ledger row followed; the loss is downstream of the filter"
          echo "ERROR: Classify passed the record and the daemon recorded nothing." >&2
          echo "       Downstream of the filter, so not this case's finding. Excluding the repetition." >&2
          exit 1
        fi

        # A dropped line naming this id is the classifier refusing a human-tier
        # write, which IS this case's finding in the direction opposite to
        # churn-forwarded. The line only exists with --log-dropped on, which
        # the eval install leaves off, so this is reachable on a re-run rather
        # than in the nightly -- but where the evidence exists the case
        # reports it instead of throwing the repetition away.
        if detector_dropped "$human_insert_id"; then
          write_verdict human-filtered \
            "the classifier dropped the human record: a dropped line names its insert_id"
          echo "ERROR: Classify refused a human-tier write. That is the regression." >&2
          exit 0
        fi

        # No row and no line of either kind. With --log-dropped off this
        # install cannot tell a silent classifier drop from a dead ingress,
        # so it exits non-zero and the repetition is excluded: accusing the
        # agent on evidence that does not distinguish the two is worse than
        # scoring nothing.
        write_verdict ingress-silent \
          "no ledger row and no detector line for the human record: Classify dropped it or it never arrived; DRIFT_DETECTOR_LOG_DROPPED tells which"
        echo "ERROR: the human record left no ledger row after ${var.card_timeout_seconds}s." >&2
        echo "       Re-run with DRIFT_DETECTOR_LOG_DROPPED=true on the install to tell which." >&2
        exit 1
      fi

      write_verdict ok \
        "human card finished after the burst, so the pipeline was consuming; no ledger row and no DRIFT line for any of $${#churn_ids[@]} churn records"
      echo "filter held: card for $human_insert_id finished, no churn forwarded"
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
