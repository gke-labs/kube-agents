# Fixture for bench/tasks/gitops-drift-own-release-digest: writes to the
# install's own Helm release, made while the drift detector is pulling, are
# digested, not triaged one card each; a kubectl write to a release object and
# a Helm write to an object outside the release still get their cards.
#
# 1. Plant one Deployment outside the release and move its memory limit to
#    drift_field_manager, so the outside-the-release record has a real object
#    and a real ownership change to report.
# 2. Read the install's Helm release off the PlatformAgent's annotations, and
#    list real objects of that release. Nothing is written to them: the
#    records below only name them, and the detector's join reads them.
# 3. With the detector running, publish by installer_principal: the create of
#    a Helm release record Secret (the start of an upgrade), then
#    release_record_count records naming those objects with Helm's user
#    agents, one each naming a Role and a ClusterRole of the release (kinds
#    the detector cannot read), one kubectl write to a kube-agents CRD, one
#    kubectl write to a release object, and one Helm write to the planted
#    Deployment. And by burst_principal, who starts no upgrade:
#    burst_record_count records naming release objects with Helm's user agent.
#    Last, by installer_principal: the update of that release record Secret
#    (the end of the upgrade) and, seconds later, one more write to a release
#    object.
# 4. Wait for the ledger rows and the two cards, read the ledger and the
#    board, and write the verdict ConfigMap the task's safeguard reads.
#
# Verdicts. ok and the red ones are written and exit 0, so the agent's turn
# still runs and the safeguard reds the repetition. Everything else is the
# environment, exits non-zero, and the repetition is excluded:
#   ok                    the upgrade's start, release, RBAC, CRD and end
#                         records each have a row marked digested or recorded
#                         headless, and no card of its own; the digest is one
#                         board card that names the upgrade; the two controls,
#                         every record of the burst and the write after the
#                         upgrade's end have cards.
#   own-release-triaged   a release or CRD record has a notified row, a
#                         session, or a card (which is main).
#   own-release-not-marked a release or CRD record has a row with no digest mark.
#   digest-pending        a row is still owed its digest after the settle.
#   control-digested      a control, a record of the burst or the write after
#                         the end was sent to the digest: the rule is wider
#                         than the release, ignores the client, or digests
#                         with no upgrade under way.
#   control-not-carded    one of those has no card.
#   digest-card-missing   the rows ended as a board card (backlog 3) and exactly
#                         one digest card covering them did not appear; or they
#                         ended as a chat post (backlog 2) and such a card did.
#   digest-names-no-upgrade the digest card does not name the Helm upgrade.
#   digest-mark-unexpected the rows carry a mark that is neither 3 nor 2, or
#                         more than one mark.

locals {
  kubectl = "kubectl"
  ci_labels = {
    "managed-by"  = "kube-agents-bench"
    "case"        = "gitops-drift-own-release-digest"
    "build-id"    = var.prow_build_id != "" ? var.prow_build_id : "local"
    "pull-number" = var.prow_pull_number != "" ? var.prow_pull_number : "none"
  }
  label_args        = join(" ", [for k, v in local.ci_labels : "${k}=${v}"])
  verdict_configmap = "eval-drift-own-release-verdict"
  kanban_board      = "/opt/data/kanban.db?mode=ro"
  ledger_db         = "/var/lib/kube-agents/session/session_kv.db?mode=ro"
  # How the digest card names the upgrade its rows rest on (DRIFT_UPGRADE_LINE
  # in session_kv_server.py).
  upgrade_wording    = "Helm upgrade: ${var.installer_principal} started revision ${var.release_revision} of release"
  helm_user_agents   = ["Go-http-client/2.0", "terraform-provider-helm_v3.3.0_x5"]
  kubectl_user_agent = "kubectl/v1.35.6 (linux/amd64) kubernetes/09fd156"
}

resource "null_resource" "drift_own_release" {
  lifecycle {
    precondition {
      condition     = var.drifted_memory != var.declared_memory
      error_message = "drifted_memory must differ from declared_memory: the outside-the-release record reports a field changing hands."
    }
    precondition {
      condition     = var.namespace != var.agent_namespace
      error_message = "namespace must differ from agent_namespace: the planted Deployment has to be outside the install's own namespace."
    }
  }

  triggers = {
    namespace     = var.namespace
    host_cluster  = var.host_cluster_name
    host_location = var.host_cluster_location
    host_project  = var.project_id
    scenario = sha256(join("|", [
      var.namespace, var.live_workload, var.live_container, var.gitops_field_manager,
      var.drift_field_manager, var.declared_memory, var.drifted_memory, var.installer_principal,
      tostring(var.release_record_count), var.product_crd, var.host_cluster_name,
      tostring(var.release_revision), var.burst_principal, tostring(var.burst_record_count),
    ]))
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    environment = {
      NS                 = var.namespace
      LIVE_WORKLOAD      = var.live_workload
      LIVE_CONTAINER     = var.live_container
      PRINCIPAL          = var.installer_principal
      BURST_PRINCIPAL    = var.burst_principal
      GITOPS_MANAGER     = var.gitops_field_manager
      DRIFT_MANAGER      = var.drift_field_manager
      DECLARED_MEMORY    = var.declared_memory
      DRIFTED_MEMORY     = var.drifted_memory
      PRODUCT_CRD        = var.product_crd
      AGENT_NS           = var.agent_namespace
      AGENT_DEPLOY       = var.agent_deployment
      AGENT_CONTAINER    = var.agent_container
      DETECTOR_CONTAINER = var.detector_container
      HELM_AGENT_A       = local.helm_user_agents[0]
      HELM_AGENT_B       = local.helm_user_agents[1]
      KUBECTL_AGENT      = local.kubectl_user_agent
    }
    command = <<-EOT
      set -euo pipefail

      kubeconfig_dir="$(mktemp -d)"
      planted=""
      on_exit() {
        status=$?
        set +e
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
      # One pid per line for every process named drift-detector, as
      # gitops-drift-backlog reads them.
      detector_pids() {
        detector_exec bash -c 'for d in /proc/[0-9]*; do [ "$(cat "$d/comm" 2>/dev/null)" = drift-detector ] && echo "$${d#/proc/}"; done; true'
      }
      # Fixed-width suffix, so no id is a prefix of another: the board and the
      # ledger are matched with LIKE '%id%'.
      mint_id() { printf 'eval-drift-own-release-%s-%05d\n' "$(date -u +%s)" "$RANDOM"; }
      # publish_record <insert id> <user agent> <audit resourceName> <audit methodName> [principal]
      publish_record() {
        payload="$(python3 -c 'import json,sys,datetime; i,ag,res,method,pr,proj,loc,cl = sys.argv[1:]; print(json.dumps({"insertId": i, "timestamp": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "resource": {"type": "k8s_cluster", "labels": {"project_id": proj, "location": loc, "cluster_name": cl}}, "protoPayload": {"serviceName": "k8s.io", "methodName": method, "resourceName": res, "authenticationInfo": {"principalEmail": pr}, "requestMetadata": {"callerSuppliedUserAgent": ag}, "status": {"code": 0}}}))' \
          "$1" "$2" "$3" "$4" "$${5:-$PRINCIPAL}" "$project" "${var.host_cluster_location}" "${var.host_cluster_name}")"
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
      # The digest cards that cover this run's rows, told by the ledger id range
      # in the card key, as gitops-drift-backlog tells them. Prints the card
      # count, the principal's count in the newest such card, and that card's
      # title and body on one line.
      digest_cards() {
        agent_exec "$agent_python" -c "
      import re, sqlite3, sys
      principal, uids = sys.argv[1], sys.argv[2:]
      ledger = sqlite3.connect('file:${local.ledger_db}', uri=True)
      lo, hi = ledger.execute('select min(id), max(id) from intercepted_events where object_uid in (%s)' % ','.join('?' * len(uids)), uids).fetchone()
      board = sqlite3.connect('file:${local.kanban_board}', uri=True)
      cards = []
      for key, title, body in board.execute(\"select idempotency_key, title, body from tasks where idempotency_key like 'drift-backlog-%' order by created_at, rowid\"):
          m = re.fullmatch(r'drift-backlog-(\d+)-(\d+)', key or '')
          if m and lo is not None and int(m.group(1)) <= lo and int(m.group(2)) >= hi:
              cards.append((title or '') + ' | ' + (body or ''))
      newest = cards[-1] if cards else ''
      n = re.search(re.escape(principal) + r' \((\d+)\)', newest)
      print(len(cards))
      print(n.group(1) if n else 0)
      print(newest.replace(chr(10), ' '))
      " "$@" 2>/dev/null
      }
      card_is_finished() {
        agent_exec "$agent_python" -c "import sqlite3,sys; sys.exit(0 if sqlite3.connect('file:${local.kanban_board}',uri=True).execute(\"select count(*) from tasks where body like ? and status in ('done','blocked','archived')\", ('%'+sys.argv[1]+'%',)).fetchone()[0] else 1)" \
          "$1" >/dev/null 2>&1
      }
      write_verdict() {
        echo "verdict=$1 detail=$2" >&2
        case "$1" in
          ok | own-release-triaged | own-release-not-marked | digest-pending | control-digested | control-not-carded | digest-card-missing | digest-names-no-upgrade | digest-mark-unexpected) ;;
          *) return 0 ;;
        esac
        ${local.kubectl} create configmap "${local.verdict_configmap}" -n "$NS" \
          --from-literal=verdict="$1" --from-literal=detail="$2" \
          --from-literal=release="$${release:-}" \
          --from-literal=release_insert_ids="$(IFS=,; echo "$${release_ids[*]:-}")" \
          --from-literal=kubectl_control_insert_id="$${kubectl_id:-}" \
          --from-literal=burst_insert_ids="$(IFS=,; echo "$${burst_ids[*]:-}")" \
          --from-literal=after_end_insert_id="$${after_id:-}" \
          --from-literal=live_insert_id="$${live_id:-}" \
          --dry-run=client -o yaml | ${local.kubectl} apply -f - >/dev/null
        ${local.kubectl} label configmap "${local.verdict_configmap}" -n "$NS" ${local.label_args} --overwrite >/dev/null
      }

      # ---- 2. The install's own release and its objects -----------------------
      release="$(${local.kubectl} get platformagents.kubeagents.x-k8s.io -n "$AGENT_NS" -o json | python3 -c '
      import json, sys
      for item in json.load(sys.stdin).get("items", []):
          a = item.get("metadata", {}).get("annotations", {})
          ns, name = a.get("meta.helm.sh/release-namespace", ""), a.get("meta.helm.sh/release-name", "")
          if ns and name:
              print(ns + "/" + name)
              break
      ')"
      if [ -z "$release" ]; then
        echo "ERROR: no PlatformAgent in $AGENT_NS carries Helm release annotations. This install was not made by Helm, so it has no own release to recognise. Excluding the repetition." >&2
        exit 1
      fi
      release_ns="$${release%%/*}"
      release_name="$${release#*/}"
      # resourceName|methodName, one line per release object. Kinds the
      # detector's ServiceAccount reads, so the join enriches the record as it
      # does in a real upgrade; the release namespace is what marks it.
      release_objects=($(${local.kubectl} get serviceaccounts,configmaps,services -n "$release_ns" -o json | python3 -c '
      import json, sys
      ns, name = sys.argv[1], sys.argv[2]
      plural = {"ServiceAccount": "serviceaccounts", "ConfigMap": "configmaps", "Service": "services"}
      for item in json.load(sys.stdin).get("items", []):
          a = item.get("metadata", {}).get("annotations", {})
          if a.get("meta.helm.sh/release-namespace") == ns and a.get("meta.helm.sh/release-name") == name and item.get("kind") in plural:
              p = plural[item["kind"]]
              print("core/v1/namespaces/%s/%s/%s|io.k8s.core.v1.%s.patch" % (ns, p, item["metadata"]["name"], p))
      ' "$release_ns" "$release_name"))
      if [ "$${#release_objects[@]}" -lt 1 ]; then
        echo "ERROR: release $release has no ServiceAccount, ConfigMap or Service carrying its annotations in $release_ns. Excluding the repetition." >&2
        exit 1
      fi
      # A Role in the release namespace and a ClusterRole of the release: kinds
      # the detector's ServiceAccount cannot read, which it has to tell by the
      # namespace and by the name.
      rbac_object() {
        python3 -c '
      import json, sys
      ns, name, path, method = sys.argv[1:]
      for item in json.load(sys.stdin).get("items", []):
          a = item.get("metadata", {}).get("annotations", {})
          if a.get("meta.helm.sh/release-namespace") == ns and a.get("meta.helm.sh/release-name") == name:
              print("%s/%s|%s" % (path, item["metadata"]["name"], method))
              break
      ' "$release_ns" "$release_name" "$1" "$2"
      }
      unreadable_objects=(
        $(${local.kubectl} get roles -n "$release_ns" -o json |
          rbac_object "rbac.authorization.k8s.io/v1/namespaces/$release_ns/roles" "io.k8s.authorization.rbac.v1.roles.patch")
        $(${local.kubectl} get clusterroles -o json |
          rbac_object "rbac.authorization.k8s.io/v1/clusterroles" "io.k8s.authorization.rbac.v1.clusterroles.patch")
      )
      if [ "$${#unreadable_objects[@]}" -ne 2 ]; then
        echo "ERROR: release $release has no Role in $release_ns or no ClusterRole carrying its annotations ($${unreadable_objects[*]:-none}). Excluding the repetition." >&2
        exit 1
      fi
      echo "own release: $release, $${#release_objects[@]} readable object(s) and $${#unreadable_objects[@]} RBAC object(s) to name"

      pids=($(detector_pids || true))
      if [ "$${#pids[@]}" -ne 1 ]; then
        echo "ERROR: expected one drift-detector process in $DETECTOR_CONTAINER, found $${#pids[@]} ($${pids[*]:-none})." >&2
        echo "       The detector is not enabled on this install, or it is restarting. Excluding the repetition." >&2
        exit 1
      fi
      detector_pid="$${pids[0]}"

      # ---- 3. Publish, with the detector running --------------------------------
      release_ids=()
      # First, as Helm writes it first: the record that starts the upgrade.
      id="$(mint_id)"
      release_ids+=("$id")
      publish_record "$id" "$HELM_AGENT_B" \
        "core/v1/namespaces/$release_ns/secrets/sh.helm.release.v1.$release_name.v${var.release_revision}" \
        "io.k8s.core.v1.secrets.create"
      i=0
      while [ "$i" -lt "${var.release_record_count}" ]; do
        object="$${release_objects[$(( i % $${#release_objects[@]} ))]}"
        agent="$HELM_AGENT_A"
        [ $(( i % 2 )) -eq 1 ] && agent="$HELM_AGENT_B"
        id="$(mint_id)"
        release_ids+=("$id")
        publish_record "$id" "$agent" "$${object%%|*}" "$${object#*|}"
        i=$(( i + 1 ))
      done
      for object in "$${unreadable_objects[@]}"; do
        id="$(mint_id)"
        release_ids+=("$id")
        publish_record "$id" "$HELM_AGENT_A" "$${object%%|*}" "$${object#*|}"
      done
      id="$(mint_id)"
      release_ids+=("$id")
      publish_record "$id" "$KUBECTL_AGENT" "apiextensions.k8s.io/v1/customresourcedefinitions/$PRODUCT_CRD" \
        "io.k8s.apiextensions.v1.customresourcedefinitions.patch"
      # The controls: kubectl on a release object, and Helm's client on an
      # object outside the release. Each must get a card.
      kubectl_id="$(mint_id)"
      object="$${release_objects[0]}"
      publish_record "$kubectl_id" "$KUBECTL_AGENT" "$${object%%|*}" "$${object#*|}"
      live_id="$(mint_id)"
      publish_record "$live_id" "$HELM_AGENT_A" "apps/v1/namespaces/$NS/deployments/$LIVE_WORKLOAD" "io.k8s.apps.v1.deployments.patch"
      # A burst on release objects by a principal with no upgrade under way.
      burst_ids=()
      i=0
      while [ "$i" -lt "${var.burst_record_count}" ]; do
        object="$${release_objects[$(( i % $${#release_objects[@]} ))]}"
        id="$(mint_id)"
        burst_ids+=("$id")
        publish_record "$id" "$HELM_AGENT_A" "$${object%%|*}" "$${object#*|}" "$BURST_PRINCIPAL"
        i=$(( i + 1 ))
      done
      # The upgrade ends: Helm updates its record. A write to a release object
      # after that is a person's, and must get a card. The pause puts its time
      # clearly after the end's: the daemon allows two seconds either way.
      id="$(mint_id)"
      release_ids+=("$id")
      publish_record "$id" "$HELM_AGENT_B" \
        "core/v1/namespaces/$release_ns/secrets/sh.helm.release.v1.$release_name.v${var.release_revision}" \
        "io.k8s.core.v1.secrets.update"
      sleep 5
      after_id="$(mint_id)"
      object="$${unreadable_objects[0]}"
      publish_record "$after_id" "$HELM_AGENT_A" "$${object%%|*}" "$${object#*|}"
      echo "published: $${#release_ids[@]} own-release records, kubectl control $kubectl_id, outside-the-release control $live_id, $${#burst_ids[@]} burst records with no upgrade, after-the-end control $after_id"

      # ---- 4. Wait for the rows and the cards -----------------------------------
      waited=0
      while :; do
        have="$(ledger_rows "$${release_ids[@]}" | cut -d'|' -f1 | sort -u | grep -c . || true)"
        [ "$have" -ge "$${#release_ids[@]}" ] && break
        if [ "$waited" -ge "${var.record_timeout_seconds}" ]; then
          echo "ERROR: $have of $${#release_ids[@]} own-release records reached the ledger within ${var.record_timeout_seconds}s." >&2
          echo "       The detector is not pulling, or not forwarding human-tier records. Excluding the repetition." >&2
          exit 1
        fi
        sleep 10
        waited=$(( waited + 10 ))
      done
      echo "own-release records: all $${#release_ids[@]} recorded"

      # A detector that restarted during the publish read some records as
      # backlog, and the backlog rule digests those whatever this rule does.
      pids=($(detector_pids || true))
      if [ "$${#pids[@]}" -ne 1 ] || [ "$${pids[0]}" != "$detector_pid" ]; then
        echo "ERROR: the drift-detector restarted during the run (pid $detector_pid, now $${pids[*]:-none}), so the records were not all live. Excluding the repetition." >&2
        exit 1
      fi

      waited=0
      card_finished=""
      while :; do
        if card_is_finished "$live_id"; then card_finished=1; break; fi
        [ "$waited" -ge "${var.card_timeout_seconds}" ] && break
        # A control sent to the digest gets no card; stop waiting for one.
        live_row="$(ledger_rows "$live_id" | tail -1)"
        live_backlog="$(printf '%s' "$live_row" | cut -d'|' -f4)"
        if [ -n "$live_row" ] && [ "$live_backlog" != "0" ] && [ "$live_backlog" != "none" ]; then break; fi
        sleep 15
        waited=$(( waited + 15 ))
      done
      # The kubectl control and the burst queue on the same board; wait for each
      # to have a card. The daemon cards the burst when its digest closes.
      waited=0
      while :; do
        have="$(carded_ids "$kubectl_id" "$after_id" "$${burst_ids[@]}" | grep -c . || true)"
        [ "$have" -ge $(( $${#burst_ids[@]} + 2 )) ] && break
        [ "$waited" -ge "${var.card_wait_seconds}" ] && break
        sleep 15
        waited=$(( waited + 15 ))
      done
      sleep ${var.settle_seconds}

      # ---- 5. Verdict -----------------------------------------------------------
      rows="$(ledger_rows "$${release_ids[@]}")"
      carded="$(carded_ids "$${release_ids[@]}" | tr '\n' ' ')"
      triaged="$(printf '%s\n' "$rows" | awk -F'|' 'NF && ($2 != "0" || $3 != "") {print $1}' | tr '\n' ' ')"
      if [ -n "$${triaged// /}$${carded// /}" ]; then
        write_verdict own-release-triaged "own-release records were triaged: notified or in a session: $${triaged:-none}; named by a card: $${carded:-none}"
        exit 0
      fi
      unmarked="$(printf '%s\n' "$rows" | awk -F'|' 'NF && ($4 == "none" || $4 == "0") {print $1}' | tr '\n' ' ')"
      if [ -n "$${unmarked// /}" ]; then
        write_verdict own-release-not-marked "own-release records were recorded with no digest mark: $unmarked"
        exit 0
      fi
      pending="$(printf '%s\n' "$rows" | awk -F'|' 'NF && $4 == "1" {print $1}' | tr '\n' ' ')"
      if [ -n "$${pending// /}" ]; then
        write_verdict digest-pending "own-release rows still owed their digest ${var.settle_seconds}s after the cards: $pending"
        exit 0
      fi

      # 4 is a row the daemon carded out of the digest route, which is where
      # the burst's rows go; 1, 2 and 3 are rows owed or given a digest.
      digested_controls=""
      for id in "$kubectl_id" "$live_id" "$after_id" "$${burst_ids[@]}"; do
        mark="$(ledger_rows "$id" | tail -1 | cut -d'|' -f4)"
        case "$mark" in "" | 0 | none | 4) ;; *) digested_controls="$digested_controls $id(backlog=$mark)" ;; esac
      done
      if [ -n "$digested_controls" ]; then
        write_verdict control-digested "a control, a burst record with no upgrade or the write after the upgrade's end was sent to the digest instead of a card:$digested_controls"
        exit 0
      fi
      live_row="$(ledger_rows "$live_id" | tail -1)"
      if [ -z "$card_finished" ]; then
        if [ -z "$live_row" ]; then
          write_verdict ingress-silent "no ledger row for the outside-the-release record after ${var.card_timeout_seconds}s"
        else
          write_verdict card-turn-failed "the outside-the-release record has a ledger row ($live_row) and no card reached a terminal status in ${var.card_timeout_seconds}s"
        fi
        echo "ERROR: the outside-the-release card did not finish. This is the install, not the change. Excluding the repetition." >&2
        exit 1
      fi
      carded_controls="$(carded_ids "$kubectl_id" "$after_id" "$${burst_ids[@]}" | tr '\n' ' ')"
      uncarded=""
      for id in "$kubectl_id" "$after_id" "$${burst_ids[@]}"; do
        case " $carded_controls " in *" $id "*) ;; *) uncarded="$uncarded $id" ;; esac
      done
      if [ -n "$${uncarded// /}" ]; then
        write_verdict control-not-carded "the kubectl write to a release object, a burst record or the write after the upgrade's end has no card after ${var.card_wait_seconds}s:$uncarded"
        exit 0
      fi

      # The ledger says how the digest ended: 3 is a board card (no chat platform
      # enabled, which is every eval install), 2 is a chat post. The card check
      # follows that mark, so an install with chat is not red for having no card.
      digest_mark="$(printf '%s\n' "$rows" | awk -F'|' 'NF {print $4}' | sort -u | tr '\n' ' ')"
      digest_after="$(digest_cards "$PRINCIPAL" "$${release_ids[@]}")"
      digest_card_count="$(printf '%s\n' "$digest_after" | sed -n 1p)"
      digest_card_count="$${digest_card_count:-0}"
      digest_principal_count="$(printf '%s\n' "$digest_after" | sed -n 2p)"
      digest_principal_count="$${digest_principal_count:-0}"
      digest_text="$(printf '%s\n' "$digest_after" | sed -n 3p)"
      case "$${digest_mark// /}" in
        3)
          # The principal's count may exceed this run's rows when an earlier
          # repetition's leftover rows were digested with them.
          if [ "$digest_card_count" -ne 1 ] || [ "$digest_principal_count" -lt "$${#release_ids[@]}" ]; then
            write_verdict digest-card-missing "the rows ended as a board card (3) but exactly one digest card covering this run's rows and counting $PRINCIPAL ($${#release_ids[@]}) or more did not appear: $digest_card_count card(s), count $digest_principal_count; newest: $${digest_text:0:200}"
            exit 0
          fi
          case "$digest_text" in
            *"${local.upgrade_wording}"*) ;;
            *)
              write_verdict digest-names-no-upgrade "the digest card does not name the Helm upgrade its rows rest on: $${digest_text:0:400}"
              exit 0
              ;;
          esac
          ;;
        2)
          if [ "$digest_card_count" -ne 0 ]; then
            write_verdict digest-card-missing "the rows ended as a chat post (2) yet $digest_card_count digest card(s) cover this run's rows"
            exit 0
          fi
          ;;
        *)
          write_verdict digest-mark-unexpected "the rows carry a mark that is neither a board card (3) nor a chat post (2): '$digest_mark'"
          exit 0
          ;;
      esac

      write_verdict ok "$${#release_ids[@]} own-release records recorded with no card and given their digest; the kubectl control, the outside-the-release control, $${#burst_ids[@]} burst records and the write after the upgrade's end carded"
      echo "own release digested: no card for $${#release_ids[@]} records, cards for $kubectl_id, $live_id, $after_id and the burst"
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
