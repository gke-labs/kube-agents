# Fixture for bench/tasks/gitops-drift-backlog-digest: records that queue while
# the drift detector is not pulling are digested, not triaged one card each,
# and a change made after it is pulling again still gets its card.
#
# 1. Plant one Deployment and move its memory limit to drift_field_manager, so
#    the live record has a real object and a real ownership change to report.
# 2. Stop the detector process (SIGSTOP) in its sidecar, publish
#    backlog_record_count records by backlog_principal, one by lone_principal
#    and kubectl_record_count kubectl writes by backlog_principal, then kill it. start-services.sh
#    relaunches it; the records published before its first pull are backlog.
#    This is the detector's own path, not a payload marked by hand: the case
#    goes red on a detector that does not mark them, as well as on a daemon
#    that triages them.
# 3. Wait for a ledger row for every backlog record, then publish one live
#    record and wait for its card, and for a card for each late record that
#    must be carded.
# 4. Read the ledger and the board, and write the verdict ConfigMap the task's
#    safeguard reads.
#
# Verdicts. ok and the five red ones are written and exit 0, so the agent's
# turn still runs and the safeguard reds the repetition. Everything else is
# the environment, exits non-zero, and the repetition is excluded:
#   ok                  every backlog record has a row marked backlog, digested
#                       or recorded headless, and no card; every late record
#                       that must be carded has a card; the live record has
#                       its card.
#   backlog-triaged     a backlog record has a notified row, a session, or a card.
#   backlog-not-marked  a backlog record has a row with no backlog mark (or the
#                       ledger has no backlog column, which is main).
#   digest-pending      a backlog row is still owed its digest after the settle.
#   live-marked-backlog the live record was marked backlog.
#   late-edit-not-carded a late record that must be carded (a lone principal's,
#                       or one sent by kubectl) has no card.

locals {
  kubectl = "kubectl"
  ci_labels = {
    "managed-by"  = "kube-agents-bench"
    "case"        = "gitops-drift-backlog-digest"
    "build-id"    = var.prow_build_id != "" ? var.prow_build_id : "local"
    "pull-number" = var.prow_pull_number != "" ? var.prow_pull_number : "none"
  }
  label_args        = join(" ", [for k, v in local.ci_labels : "${k}=${v}"])
  verdict_configmap = "eval-drift-backlog-verdict"
  kanban_board      = "/opt/data/kanban.db?mode=ro"
  ledger_db         = "/var/lib/kube-agents/session/session_kv.db?mode=ro"
}

resource "null_resource" "drift_backlog" {
  lifecycle {
    precondition {
      condition     = var.drifted_memory != var.declared_memory
      error_message = "drifted_memory must differ from declared_memory: the live record reports a field changing hands."
    }
    precondition {
      condition     = var.lone_principal != var.backlog_principal && var.lone_principal != var.live_principal
      error_message = "lone_principal must differ from backlog_principal and live_principal, or its record joins the burst and is digested."
    }
    precondition {
      condition     = var.live_principal != var.backlog_principal
      error_message = "live_principal and backlog_principal must differ, so a card for one is not read as a card for the other."
    }
  }

  triggers = {
    namespace     = var.namespace
    host_cluster  = var.host_cluster_name
    host_location = var.host_cluster_location
    host_project  = var.project_id
    scenario = sha256(join("|", [
      var.namespace, var.live_workload, var.live_container, var.gitops_field_manager,
      var.drift_field_manager, var.declared_memory, var.drifted_memory, var.live_principal,
      var.backlog_principal, tostring(var.backlog_record_count), var.host_cluster_name,
      var.lone_principal, tostring(var.kubectl_record_count),
    ]))
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    environment = {
      NS                 = var.namespace
      LIVE_WORKLOAD      = var.live_workload
      LIVE_CONTAINER     = var.live_container
      LIVE_PRINCIPAL     = var.live_principal
      BACKLOG_PRINCIPAL  = var.backlog_principal
      GITOPS_MANAGER     = var.gitops_field_manager
      DRIFT_MANAGER      = var.drift_field_manager
      DECLARED_MEMORY    = var.declared_memory
      DRIFTED_MEMORY     = var.drifted_memory
      AGENT_NS           = var.agent_namespace
      AGENT_DEPLOY       = var.agent_deployment
      AGENT_CONTAINER    = var.agent_container
      DETECTOR_CONTAINER = var.detector_container
    }
    command = <<-EOT
      set -euo pipefail

      kubeconfig_dir="$(mktemp -d)"
      planted=""
      stopped_pid=""
      on_exit() {
        status=$?
        set +e
        # Never leave the detector stopped: a stopped process holds no lease
        # and pulls nothing, and the install would detect no drift until the
        # pod restarts. Killed rather than continued, so start-services.sh
        # relaunches a clean process.
        if [ -n "$stopped_pid" ]; then
          echo "Killing the stopped detector (pid $stopped_pid) so start-services.sh relaunches it." >&2
          ${local.kubectl} exec -n "$AGENT_NS" "deploy/$AGENT_DEPLOY" -c "$DETECTOR_CONTAINER" -- \
            bash -c "kill -KILL $stopped_pid" >&2
        fi
        if [ "$status" -ne 0 ] && [ -n "$planted" ]; then
          ${local.kubectl} delete namespace "$NS" --ignore-not-found --wait=false >&2
        fi
        rm -rf "$kubeconfig_dir"
      }
      trap on_exit EXIT
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

      # ---- 1. Plant ---------------------------------------------------------
      if ${local.kubectl} get namespace "$NS" >/dev/null 2>&1; then
        owner="$(${local.kubectl} get namespace "$NS" -o jsonpath='{.metadata.labels.managed-by}')"
        if [ "$owner" != "kube-agents-bench" ]; then
          echo "ERROR: namespace $NS exists and is not labelled managed-by=kube-agents-bench. Refusing to touch it." >&2
          exit 1
        fi
        ${local.kubectl} delete namespace "$NS" --ignore-not-found --wait=true --timeout=120s
      fi
      planted=1
      ${local.kubectl} create namespace "$NS"
      ${local.kubectl} label namespace "$NS" ${local.label_args} --overwrite

      cat <<YAML | ${local.kubectl} apply --server-side --field-manager="$GITOPS_MANAGER" -n "$NS" -f -
      apiVersion: apps/v1
      kind: Deployment
      metadata:
        name: $LIVE_WORKLOAD
        labels:
          managed-by: kube-agents-bench
      spec:
        replicas: 1
        selector:
          matchLabels:
            app: $LIVE_WORKLOAD
        template:
          metadata:
            labels:
              app: $LIVE_WORKLOAD
          spec:
            containers:
              - name: $LIVE_CONTAINER
                image: registry.k8s.io/pause:3.10
                resources:
                  limits:
                    memory: "$DECLARED_MEMORY"
      YAML
      cat <<YAML | ${local.kubectl} apply --server-side --field-manager="$DRIFT_MANAGER" --force-conflicts -n "$NS" -f -
      apiVersion: apps/v1
      kind: Deployment
      metadata:
        name: $LIVE_WORKLOAD
      spec:
        template:
          spec:
            containers:
              - name: $LIVE_CONTAINER
                resources:
                  limits:
                    memory: "$DRIFTED_MEMORY"
      YAML

      # ---- helpers ------------------------------------------------------------
      agent_exec() {
        ${local.kubectl} exec -n "$AGENT_NS" "deploy/$AGENT_DEPLOY" -c "$AGENT_CONTAINER" -- "$@"
      }
      detector_exec() {
        ${local.kubectl} exec -n "$AGENT_NS" "deploy/$AGENT_DEPLOY" -c "$DETECTOR_CONTAINER" -- "$@"
      }
      agent_python=""
      for candidate in python3 /opt/hermes/.venv/bin/python3; do
        if agent_exec "$candidate" -c "pass" >/dev/null 2>&1; then
          agent_python="$candidate"
          break
        fi
      done
      if [ -z "$agent_python" ]; then
        echo "ERROR: no python interpreter in $AGENT_CONTAINER to read the ledger with." >&2
        exit 1
      fi
      # One pid per line for every process named drift-detector. /proc and
      # bash's own kill rather than pgrep and kill(1), which the sidecar image
      # need not carry.
      detector_pids() {
        detector_exec bash -c 'for d in /proc/[0-9]*; do [ "$(cat "$d/comm" 2>/dev/null)" = drift-detector ] && echo "$${d#/proc/}"; done; true'
      }
      # Fixed-width suffix, so no id is a prefix of another: the board and the
      # ledger are matched with LIKE '%id%'.
      mint_id() { printf 'eval-drift-backlog-%s-%05d\n' "$(date -u +%s)" "$RANDOM"; }
      publish_record() {
        payload="$(python3 -c 'import json,sys,datetime; i,pr,ns,wl,ag,proj,loc,cl = sys.argv[1:]; print(json.dumps({"insertId": i, "timestamp": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "resource": {"type": "k8s_cluster", "labels": {"project_id": proj, "location": loc, "cluster_name": cl}}, "protoPayload": {"serviceName": "k8s.io", "methodName": "io.k8s.apps.v1.deployments.patch", "resourceName": "apps/v1/namespaces/%s/deployments/%s" % (ns, wl), "authenticationInfo": {"principalEmail": pr}, "requestMetadata": {"callerSuppliedUserAgent": ag}, "status": {"code": 0}}}))' \
          "$1" "$2" "$3" "$4" "$5" "$project" "${var.host_cluster_location}" "${var.host_cluster_name}")"
        gcloud pubsub topics publish "${var.drift_topic}" --project "$project" --message="$payload" >/dev/null
      }
      # One line per id that has a ledger row:
      # object_uid|notified|session_id|backlog, with backlog "none" when the
      # ledger has no backlog column.
      ledger_rows() {
        agent_exec "$agent_python" -c "
      import sqlite3, sys
      db = sqlite3.connect('file:${local.ledger_db}', uri=True)
      cols = {r[1] for r in db.execute('pragma table_info(intercepted_events)')}
      backlog = 'backlog' if 'backlog' in cols else \"'none'\"
      ids = sys.argv[1:]
      q = 'select object_uid, notified, session_id, %s from intercepted_events where object_uid in (%s) order by id' % (backlog, ','.join('?' * len(ids)))
      for r in db.execute(q, ids):
          print('|'.join(str(v) for v in r))
      " "$@"
      }
      # Ids among the arguments that some card on the board names.
      carded_ids() {
        agent_exec "$agent_python" -c "
      import sqlite3, sys
      db = sqlite3.connect('file:${local.kanban_board}', uri=True)
      for i in sys.argv[1:]:
          if db.execute('select count(*) from tasks where body like ?', ('%' + i + '%',)).fetchone()[0]:
              print(i)
      " "$@"
      }
      card_is_finished() {
        agent_exec "$agent_python" -c "import sqlite3,sys; sys.exit(0 if sqlite3.connect('file:${local.kanban_board}',uri=True).execute(\"select count(*) from tasks where body like ? and status in ('done','blocked','archived')\", ('%'+sys.argv[1]+'%',)).fetchone()[0] else 1)" \
          "$1" >/dev/null 2>&1
      }
      write_verdict() {
        echo "verdict=$1 detail=$2" >&2
        case "$1" in
          ok | backlog-triaged | backlog-not-marked | digest-pending | live-marked-backlog | late-edit-not-carded) ;;
          *) return 0 ;;
        esac
        ${local.kubectl} create configmap "${local.verdict_configmap}" -n "$NS" \
          --from-literal=verdict="$1" --from-literal=detail="$2" \
          --from-literal=backlog_insert_ids="$(IFS=,; echo "$${backlog_ids[*]:-}")" \
          --from-literal=live_insert_id="$${live_id:-}" \
          --from-literal=card_insert_ids="$(IFS=,; echo "$${card_ids[*]:-}")" \
          --dry-run=client -o yaml | ${local.kubectl} apply -f - >/dev/null
        ${local.kubectl} label configmap "${local.verdict_configmap}" -n "$NS" ${local.label_args} --overwrite >/dev/null
      }

      # ---- 2. Stop the detector, publish, kill it ----------------------------
      pids=($(detector_pids || true))
      if [ "$${#pids[@]}" -ne 1 ]; then
        echo "ERROR: expected one drift-detector process in $DETECTOR_CONTAINER, found $${#pids[@]} ($${pids[*]:-none})." >&2
        echo "       The detector is not enabled on this install, or the container has changed. Excluding the repetition." >&2
        exit 1
      fi
      old_pid="$${pids[0]}"
      detector_exec bash -c "kill -STOP $old_pid"
      stopped_pid="$old_pid"
      echo "detector pid $old_pid stopped"

      backlog_ids=()
      i=0
      while [ "$i" -lt "${var.backlog_record_count}" ]; do
        id="$(mint_id)"
        backlog_ids+=("$id")
        publish_record "$id" "$BACKLOG_PRINCIPAL" "$NS" "eval-drift-backlog-release-$i" "Go-http-client/2.0"
        i=$(( i + 1 ))
      done
      # Late records that must still be carded: one by a second principal, and
      # kubectl writes by the backlog principal.
      card_ids=()
      id="$(mint_id)"
      card_ids+=("$id")
      publish_record "$id" "${var.lone_principal}" "$NS" "eval-drift-backlog-lone" "Go-http-client/2.0"
      i=0
      while [ "$i" -lt "${var.kubectl_record_count}" ]; do
        id="$(mint_id)"
        card_ids+=("$id")
        publish_record "$id" "$BACKLOG_PRINCIPAL" "$NS" "eval-drift-backlog-kubectl-$i" "kubectl/v1.35.6"
        i=$(( i + 1 ))
      done
      echo "backlog records: $${#backlog_ids[@]} to digest and $${#card_ids[@]} to card, published while the detector was stopped"
      # Publish time is set when the topic accepts the message, so a short
      # pause puts every backlog record clearly before the relaunched
      # process's first pull.
      sleep 5

      detector_exec bash -c "kill -KILL $old_pid"
      stopped_pid=""
      waited=0
      new_pid=""
      while [ "$waited" -lt "${var.detector_restart_timeout_seconds}" ]; do
        pids=($(detector_pids || true))
        if [ "$${#pids[@]}" -eq 1 ] && [ "$${pids[0]}" != "$old_pid" ]; then
          new_pid="$${pids[0]}"
          break
        fi
        sleep 5
        waited=$(( waited + 5 ))
      done
      if [ -z "$new_pid" ]; then
        echo "ERROR: start-services.sh did not relaunch the detector within ${var.detector_restart_timeout_seconds}s. Excluding the repetition." >&2
        exit 1
      fi
      echo "detector relaunched as pid $new_pid"

      # ---- 3. Wait for the backlog rows, then the live record ------------------
      waited=0
      while :; do
        have="$(ledger_rows "$${backlog_ids[@]}" | cut -d'|' -f1 | sort -u | grep -c . || true)"
        [ "$have" -ge "$${#backlog_ids[@]}" ] && break
        if [ "$waited" -ge "${var.backlog_timeout_seconds}" ]; then
          echo "ERROR: $have of $${#backlog_ids[@]} backlog records reached the ledger within ${var.backlog_timeout_seconds}s of the relaunch." >&2
          echo "       The detector is not pulling, or not forwarding human-tier records. Excluding the repetition." >&2
          exit 1
        fi
        sleep 10
        waited=$(( waited + 10 ))
      done
      echo "backlog records: all $${#backlog_ids[@]} recorded"

      live_id="$(mint_id)"
      publish_record "$live_id" "$LIVE_PRINCIPAL" "$NS" "$LIVE_WORKLOAD" "kubectl-edit/v1.31.0"
      echo "live record: insertId=$live_id"
      waited=0
      card_finished=""
      while :; do
        if card_is_finished "$live_id"; then card_finished=1; break; fi
        # A live record marked backlog gets no card, so check its row here,
        # before the wait runs out and the no-card branch excludes the run.
        live_row="$(ledger_rows "$live_id" | tail -1)"
        live_backlog="$(printf '%s' "$live_row" | cut -d'|' -f4)"
        if [ -n "$live_row" ] && [ "$live_backlog" != "0" ] && [ "$live_backlog" != "none" ]; then
          write_verdict live-marked-backlog "the live record, published after the relaunched detector's first pull, was marked backlog: $live_row"
          exit 0
        fi
        [ "$waited" -ge "${var.card_timeout_seconds}" ] && break
        sleep 15
        waited=$(( waited + 15 ))
      done
      # The late records that must be carded queue on the same board as the live
      # card; wait for each to have one.
      waited=0
      while :; do
        have="$(carded_ids "$${card_ids[@]}" | grep -c . || true)"
        [ "$have" -ge "$${#card_ids[@]}" ] && break
        [ "$waited" -ge "${var.card_wait_seconds}" ] && break
        sleep 15
        waited=$(( waited + 15 ))
      done
      sleep ${var.settle_seconds}

      # ---- 4. Verdict -----------------------------------------------------------
      rows="$(ledger_rows "$${backlog_ids[@]}")"
      carded="$(carded_ids "$${backlog_ids[@]}" | tr '\n' ' ')"
      triaged="$(printf '%s\n' "$rows" | awk -F'|' 'NF && ($2 != "0" || $3 != "") {print $1}' | tr '\n' ' ')"
      if [ -n "$${triaged// /}$${carded// /}" ]; then
        write_verdict backlog-triaged "backlog records were triaged: notified or in a session: $${triaged:-none}; named by a card: $${carded:-none}"
        exit 0
      fi
      unmarked="$(printf '%s\n' "$rows" | awk -F'|' 'NF && ($4 == "none" || $4 == "0") {print $1}' | tr '\n' ' ')"
      if [ -n "$${unmarked// /}" ]; then
        write_verdict backlog-not-marked "backlog records were recorded with no backlog mark: $unmarked"
        exit 0
      fi
      pending="$(printf '%s\n' "$rows" | awk -F'|' 'NF && $4 == "1" {print $1}' | tr '\n' ' ')"
      if [ -n "$${pending// /}" ]; then
        write_verdict digest-pending "backlog rows still owed their digest ${var.settle_seconds}s after the live card: $pending"
        exit 0
      fi

      live_row="$(ledger_rows "$live_id" | tail -1)"
      if [ -z "$card_finished" ]; then
        if [ -z "$live_row" ]; then
          write_verdict ingress-silent "no ledger row for the live record after ${var.card_timeout_seconds}s"
        else
          write_verdict card-turn-failed "the live record has a ledger row ($live_row) and no card reached a terminal status in ${var.card_timeout_seconds}s"
        fi
        echo "ERROR: the live card did not finish. This is the install, not the change. Excluding the repetition." >&2
        exit 1
      fi
      carded_late="$(carded_ids "$${card_ids[@]}" | tr '\n' ' ')"
      uncarded=""
      for id in "$${card_ids[@]}"; do
        case " $carded_late " in *" $id "*) ;; *) uncarded="$uncarded $id" ;; esac
      done
      if [ -n "$${uncarded// /}" ]; then
        write_verdict late-edit-not-carded "late records that must be carded have no card after ${var.card_wait_seconds}s:$uncarded"
        exit 0
      fi

      write_verdict ok "$${#backlog_ids[@]} backlog records recorded with no card and given their digest; $${#card_ids[@]} late records carded; live card finished"
      echo "backlog digested: no card for $${#backlog_ids[@]} backlog records, live card for $live_id finished"
    EOT
  }

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
      kubectl delete namespace "${self.triggers.namespace}" --ignore-not-found --wait=false
    EOT
  }
}

output "cluster_name" {
  value = var.host_cluster_name
}

output "cluster_location" {
  value = var.host_cluster_location
}
