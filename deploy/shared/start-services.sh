#!/usr/bin/env bash
# Entrypoint for the credential-proxy container.
#
# Three peer services live here, not one service with helpers. They share a
# container because they all need credentials, and credentials are deliberately
# kept out of the agent sandbox — not because any of them belongs to another:
#
#   credential_proxy.py   executes credentialed CLIs on behalf of the sandbox
#   envoy                 fronts the credential proxy on loopback
#   k8s-event-watcher     watches cluster API servers and reports events
#
# They differ in how their failure is treated. Envoy and the credential runtime
# are the container's reason to exist: if either dies the agent loses every
# credentialed command, so their exit ends the container and Kubernetes
# restarts it. The watcher is best-effort observability — losing it must not
# take the credential path down with it, so it is supervised and restarted in
# place instead. It is also the only one of the three that can be switched off
# deliberately: EVENT_WATCHER_ENABLED=false skips it entirely, which is the
# emergency stop for an event storm. See event_watcher_disabled below.
set -euo pipefail

# The sandbox runs as a different user (see the UID constants in the operator's
# platformagent_manifests.go) and shares only the agent PVC with this container.
# Proxied commands run here but write there — a clone, a commit, a kubeconfig pin
# in a profile home — and the sandbox has to be able to change what they leave
# behind. The shared fsGroup gives it the group; this gives the group write.
# Credential state lives on this container's own emptyDir volumes, which nothing
# else mounts, so the wider mode does not widen who can read a credential.
umask 0002

# Watcher restart policy. The watcher is retried in place rather than being
# allowed to end the container, so these bound how hard a permanently broken
# one is retried and how long it must survive to count as recovered.
WATCHER_RETRY_MIN_SECONDS="${WATCHER_RETRY_MIN_SECONDS:-10}"
WATCHER_RETRY_MAX_SECONDS="${WATCHER_RETRY_MAX_SECONDS:-120}"
WATCHER_HEALTHY_RUN_SECONDS="${WATCHER_HEALTHY_RUN_SECONDS:-120}"

# Where the watcher keeps its dedup snapshots. Without them the cache starts
# empty on every restart, and an empty cache is not a neutral state: the
# informer's initial LIST replays every event still inside the API server's TTL
# (an hour on GKE by default), so a restart re-reports incidents that were
# already triaged. The supervisor below restarts the watcher in place, which
# makes that a routine occurrence rather than a rare one.
#
# The data volume, not the container's own state directory: that one is a 16Mi
# in-memory emptyDir, so it would lose the cache on exactly the pod restarts
# that matter most. The watcher appends the profile name per cluster, since
# each cluster keeps its own cache and they cannot share a file.
WATCHER_DEDUP_DIR="${WATCHER_DEDUP_DIR:-${CREDENTIAL_PROXY_WORKSPACE_ROOT:-/opt/data}/event-watcher}"

# How long a failure stays suppressed after its last sighting. The window
# SLIDES: every fresh observation pushes the deadline out again, so a workload
# that keeps failing is reported once and then stays quiet. The window only
# expires after a genuine gap, and when it does the incident is rebuilt from
# scratch — new session, new chat thread, count back to 1.
#
# That is why the binary's own 5m default is the wrong value here rather than
# merely a conservative one. The kubelet's image-pull and crash-loop backoffs
# both cap at 300s, so a steadily-failing pod re-reports at almost exactly the
# threshold and clears it or misses it on delivery jitter alone. The customer-
# visible result is the same broken image arriving as an unrelated-looking new
# alert every few minutes, with nothing tying the copies together.
#
# 24h is chosen over anything shorter because a broken deploy is not a
# minutes-scale event. An unresolvable image reference, a missing Secret or a
# node that will not come back stays broken until a human acts, and the useful
# alert cadence for "still broken, nobody has fixed it" is daily, not hourly.
# The cost is the other side of the same coin, and it is real: a failure that
# genuinely clears and returns later the same day is folded into the original
# incident instead of opening a new one, and the agent is not woken for it.
# A fleet whose failures resolve and recur within a shift wants a smaller value.
#
# Overridable because the right value depends on the fleet's failure mix, and
# an operator should not have to rebuild the image to find out.
WATCHER_DEDUP_WINDOW="${WATCHER_DEDUP_WINDOW:-24h}"

# Leading-edge debounce for the crash-loop family: how many times kubelet must
# report the same BackOff before it is treated as an incident rather than a
# startup race that will clear on its own. Passed explicitly even though it
# matches the binary's own default, because the value is the kind of thing an
# operator tunes per install — a cluster with slow-starting workloads wants it
# higher — and threading it through an env var means doing so does not require
# rebuilding the image. Set to 1 to restore firing on the first event.
WATCHER_BACKOFF_MIN_COUNT="${WATCHER_BACKOFF_MIN_COUNT:-3}"

# The same debounce for the half of the image-pull family that self-clears —
# registry rate limits, 5xx, connection timeouts. Only failures the watcher
# positively recognises as transient are held; a bad tag, and any wording the
# classifier does not recognise, still fire on the first event. Worth tuning
# separately from the crash-loop value: an install pulling from a rate-limited
# public registry wants it higher, and one where every pull is from a private
# mirror will rarely see it apply at all. Set to 1 to disable.
WATCHER_IMAGEPULL_TRANSIENT_MIN_COUNT="${WATCHER_IMAGEPULL_TRANSIENT_MIN_COUNT:-3}"

runtime_pid=""
envoy_pid=""
watcher_pid=""

terminate() {
  trap - EXIT INT TERM
  # The supervisor first, so it does not restart the watcher on the way down.
  if [[ -n "${watcher_pid}" ]]; then
    kill "${watcher_pid}" 2>/dev/null || true
    pkill -P "${watcher_pid}" 2>/dev/null || true
  fi
  [[ -z "${runtime_pid}" ]] || kill "${runtime_pid}" 2>/dev/null || true
  [[ -z "${envoy_pid}" ]] || kill "${envoy_pid}" 2>/dev/null || true
}
trap terminate EXIT INT TERM

start_credential_runtime() {
  /opt/hermes/.venv/bin/python3 /opt/defaults/scripts/credential_proxy.py &
  runtime_pid=$!
}

start_envoy() {
  # The baked config binds 127.0.0.1, which is the access control for as long as
  # the broker is a sidecar in the agent's Pod. When the operator puts the broker
  # in a Pod of its own the listener has to accept the Pod IP, and the config is
  # baked into the image rather than rendered by the operator -- so the one line
  # that has to differ is substituted here rather than by shipping two configs
  # that would drift. The value is checked against a character class first: it
  # reaches sed, and sed would happily accept a replacement carrying its own
  # delimiter or newline.
  local envoy_config=/etc/envoy/envoy-credential-proxy.yaml
  if [[ -n "${CREDENTIAL_PROXY_ENVOY_ADDRESS:-}" ]]; then
    if [[ ! "${CREDENTIAL_PROXY_ENVOY_ADDRESS}" =~ ^[0-9a-fA-F.:]+$ ]]; then
      echo "CREDENTIAL_PROXY_ENVOY_ADDRESS is not an IP address" >&2
      exit 1
    fi
    local rendered=/tmp/envoy-credential-proxy.yaml
    sed "s|address: 127\.0\.0\.1|address: ${CREDENTIAL_PROXY_ENVOY_ADDRESS}|" \
      "${envoy_config}" >"${rendered}"
    # A substitution that silently matched nothing would leave the broker bound
    # to loopback in a Pod nothing else can reach, which reads as a hang.
    grep -q "address: ${CREDENTIAL_PROXY_ENVOY_ADDRESS}" "${rendered}"
    envoy_config="${rendered}"
  fi
  /usr/local/bin/envoy --config-path "${envoy_config}" --log-level info &
  envoy_pid=$!
}

# The emergency stop, written by the operator from the PlatformAgent's
# spec.harness.eventWatcher.enabled. Unset means enabled, so that an install
# whose operator predates the field keeps watching rather than going quiet on
# upgrade.
#
# Only a recognised falsey value disables the watcher; anything else unrecognised
# leaves it running and says so. Not for the CR path — `enabled` is a strict
# boolean there and admission rejects anything else before it reaches this
# script — but for the ways a value gets here without passing through the CRD: a
# hand-edited Deployment during an incident, and a container image paired with
# an operator that spells the value differently than this release expects.
#
# It fails towards watching because the two mistakes do not cost the same. A
# value that stops event ingestion is invisible — the container stays Ready, the
# log says nothing more, and the fleet simply never reports another incident —
# while one that leaves the watcher running is obvious the moment the next event
# arrives.
event_watcher_disabled() {
  case "${EVENT_WATCHER_ENABLED:-true}" in
    [Ff][Aa][Ll][Ss][Ee] | 0 | [Nn][Oo] | [Oo][Ff][Ff]) return 0 ;;
    [Tt][Rr][Uu][Ee] | 1 | [Yy][Ee][Ss] | [Oo][Nn]) return 1 ;;
    *)
      echo "start-services: EVENT_WATCHER_ENABLED=${EVENT_WATCHER_ENABLED:-} is not a recognised boolean; starting the k8s-event-watcher anyway. Use 'false' to disable it." >&2
      return 1
      ;;
  esac
}

start_event_watcher() {
  if event_watcher_disabled; then
    # Loud, and worded so it cannot be mistaken for the ALERT lines below: those
    # mean the watcher tried and failed, this one means somebody turned it off.
    # The pod log is where a reader of the container finds that out — the
    # readiness probe covers only the credential proxy, so a container with no
    # watcher in it looks exactly like a healthy one from outside.
    echo "start-services: k8s-event-watcher is DISABLED by configuration (EVENT_WATCHER_ENABLED=${EVENT_WATCHER_ENABLED:-}) — NO cluster events are being watched and no autonomous triage sessions will start. Set spec.harness.eventWatcher.enabled=true on the PlatformAgent to start watching again." >&2
    return 0
  fi

  # Flags are set here rather than passed as container arguments: they describe
  # how processes inside this container reach each other over loopback, which is
  # implementation detail rather than deployment configuration. The one value
  # that varies per install — the cluster's name — comes from the operator via
  # EVENT_WATCHER_CLUSTER_NAME, which it always sets. No default is applied
  # here on purpose: guessing a name would mislabel every payload and metric,
  # so an unset value should fail loudly in the watcher's own validation.
  #
  # --token-env names SESSION_KV_API_KEY rather than API_SERVER_KEY: the latter
  # is the loopback sentinel `cluster-internal-trusted`, which authenticates
  # nothing. The watcher refuses to start when the named variable is empty,
  # which is the behaviour we want — the Session KV server fails closed too.

  # Said once, up front, and in terms of the consequence: the watcher's own
  # error ("bearer token env var ... is empty") names a variable, not what
  # stops working, and it only reaches the ALERT below after three short exits.
  # An install upgraded from before this key existed is exactly the case that
  # lands here — see the backfill in upgrade.sh.
  if [ -z "${SESSION_KV_API_KEY:-}" ]; then
    echo "start-services: ALERT SESSION_KV_API_KEY is empty, so k8s-event-watcher cannot authenticate to the Session KV server and will exit on every start — NO cluster events are being watched. Add the key to the agent Secret (upgrade.sh backfills it; the chart and the Terraform composition generate it on a fresh install) and restart the pod." >&2
  fi

  # An empty value disables persistence, which is what should happen if the
  # directory cannot be created: the watcher still dedups in memory, and losing
  # snapshots must not cost us the watcher itself. Creating it here rather than
  # in the watcher keeps the failure at startup, where it is logged once,
  # instead of on every snapshot tick.
  dedup_persist=""
  if mkdir -p "${WATCHER_DEDUP_DIR}" 2>/dev/null; then
    dedup_persist="${WATCHER_DEDUP_DIR}/dedup.json"
  else
    echo "start-services: cannot create ${WATCHER_DEDUP_DIR}; the dedup cache will not survive a watcher restart, so recent incidents may be reported twice" >&2
  fi

  (
    delay="${WATCHER_RETRY_MIN_SECONDS}"
    consecutive=0
    while true; do
      started=$SECONDS
      /usr/local/bin/k8s-event-watcher \
        --cluster-name="${EVENT_WATCHER_CLUSTER_NAME:-}" \
        --profiles-dir="${CREDENTIAL_PROXY_WORKSPACE_ROOT:-/opt/data}/profiles" \
        --dedup-persist="${dedup_persist}" \
        --dedup-window="${WATCHER_DEDUP_WINDOW}" \
        --in-cluster \
        --daemon-url=http://127.0.0.1:8699 \
        --token-env=SESSION_KV_API_KEY \
        --owner=platform \
        --reason=Failed,FailedToDrainNode,CrashLoopBackOff,BackOff,ImagePullBackOff,ErrImagePull,OOMKilled \
        --backoff-min-count="${WATCHER_BACKOFF_MIN_COUNT}" \
        --imagepull-transient-min-count="${WATCHER_IMAGEPULL_TRANSIENT_MIN_COUNT}" || true
      ran=$(( SECONDS - started ))

      # A run long enough to have synced and served is treated as a fresh
      # start, so an occasional crash after hours of work does not inherit a
      # backoff earned days earlier. Anything shorter is a failure to start.
      if [[ "${ran}" -ge "${WATCHER_HEALTHY_RUN_SECONDS}" ]]; then
        delay="${WATCHER_RETRY_MIN_SECONDS}"
        consecutive=0
      else
        consecutive=$(( consecutive + 1 ))
      fi

      echo "start-services: k8s-event-watcher exited after ${ran}s (consecutive short exits: ${consecutive}); retrying in ${delay}s" >&2
      if [[ "${consecutive}" -ge 3 ]]; then
        # Loud, greppable, and states the consequence rather than the symptom.
        # Nothing else reports this: the container stays Ready by design, so a
        # watcher that can never start is otherwise indistinguishable from a
        # fleet with no incidents.
        echo "start-services: ALERT k8s-event-watcher has failed to start ${consecutive} times in a row — NO cluster events are being watched" >&2
      fi

      sleep "${delay}"
      # Exponential, capped: a permanent failure (bad RBAC, missing profiles
      # directory) should not hammer the API server every 10s forever.
      delay=$(( delay * 2 ))
      [[ "${delay}" -le "${WATCHER_RETRY_MAX_SECONDS}" ]] || delay="${WATCHER_RETRY_MAX_SECONDS}"
    done
  ) &
  watcher_pid=$!
}

start_credential_runtime
start_envoy
start_event_watcher

# Only the two credential-path services are waited on. The watcher is absent
# from this list deliberately — see the header.
wait -n "${runtime_pid}" "${envoy_pid}"
