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

# shellcheck shell=bash disable=SC2154,SC2034
# Both codes are disabled for the file, and for the same reason: this is a
# sourced fragment, so the drift_* names it reads are set by its caller
# (SC2154) and the ones it sets are read by its caller (SC2034). The contract
# is under "Required of the caller" and "Set by drift_init" below.
# bench/upgrade-scenarios/scenarios/09.sh carries the SC2034 half for the same
# shape; the SC2154 half is this file's, because it reads as well as writes.

# The scaffolding a gitops-drift fixture needs around its own plant -- for the
# three stacks that hand the record straight to the Session KV daemon:
# gitops-drift, gitops-drift-deleted-policy and gitops-drift-load-bearing.
#
# gitops-drift-noise is NOT one of them. It publishes to Pub/Sub and drives the
# real detector, so only step 0 is common ground, and it still carries its own
# copy of that -- with different semantics, refusing a leftover namespace where
# this clears one. A change to the guard or the fetch here does not reach it.
#
# Sourced from a stack's `local-exec`, which keeps one copy on disk rather than
# the whole library in every rendered plan:
#
#   source "${path.module}/../../modules/drift-inject/drift_inject.sh"
#
# `../../modules` resolves from a prebuilt stack at apply time -- gpu-stress-test
# and gitops-fix-cycle both take `source = "../../modules/cluster"` -- and
# gitops-fix-cycle runs `${path.module}/scripts/setup.sh` as a command, so a
# script beside the stack is an established shape here. Sourcing rather than
# inlining the text with `file()` avoids a dedent hazard: a `${file(...)}`
# interpolation placed at a smaller indent than the heredoc body sets the
# `<<-EOT` dedent to zero and leaves every embedded heredoc terminator
# indented, which is an unterminated heredoc.
# bench/tests/test_tf_heredoc_renders.py is what catches that.
#
# This directory is inside the fixture sanitization scan -- SANITIZED_ROOTS in
# scripts/validate_bench_cases.py includes bench/tf/modules -- so nothing here
# may carry a real project, cluster, address or principal. Every such value
# belongs to the caller and arrives as a variable.
#
# Defines functions and a handful of DRIFT_* constants, and runs nothing, so it
# may be sourced before or after the caller's own assignments. The caller then
# calls drift_init, drift_clear_namespace and drift_require_daemon, plants its
# scenario, and calls drift_mint_insert_id, drift_build_payload,
# drift_inject_record and drift_wait_for_card.
#
# What stays with the caller is the scenario: the plant, its assertions, and the
# payload's own field values.
#
# Required of the caller before drift_init:
#   drift_ns                  namespace the plant creates, and the leftover guard's subject
#   drift_cluster             host cluster name
#   drift_location            host cluster location
#   drift_project_id          may be empty; resolved from gcloud when it is
#   drift_managed_by          the managed-by label value the leftover guard requires
#   drift_leftover_timeout    seconds to wait for a leftover namespace to go
#   drift_agent_namespace     namespace of the agent install
#   drift_agent_container     container inside the agent pod
#   drift_agent_deployment    the agent deployment to exec into
#   drift_daemon_url          the Session KV daemon's base URL
#   drift_card_wait_timeout   seconds to wait for the triage card
#   drift_poll_interval       seconds between board polls
#
# Optional:
#   drift_card_finish_timeout seconds to wait, once the card is on the board, for
#                             it to finish before the agent's turn (default
#                             below)
#   drift_failure_resources   comma-separated kinds the failure dump prints
#                             (default below). gitops-drift-deleted-policy sets
#                             networkpolicies, which is the object its scenario
#                             is about; a stack that leaves this at the default
#                             prints nothing about its own subject.
#
# Set by this library and reserved to it. The caller reads them; reassigning
# one changes what the payload reports or what the poll looks for.
#   drift_init              drift_kubeconfig_dir, drift_project,
#                           drift_exec_in_pod, drift_exec_in_pod_stdin
#   drift_mint_insert_id    drift_insert_id, drift_changed_at
#   drift_build_payload     drift_payload_file
#
# Set by the caller when its plant creates the namespace: drift_planted_ns=1,
# which arms the failure-path cleanup. It is guarded rather than unconditional
# because the EXIT trap is armed before KUBECONFIG is exported -- an early
# failure would otherwise delete against whatever cluster the ambient context
# points at -- and because drift_clear_namespace exits non-zero on a namespace
# this stack did not label, where deleting would undo the one refusal it
# exists to make.

DRIFT_DEFAULT_FAILURE_RESOURCES="deployments,replicasets,pods"
# Resolved rather than assumed: the daemon runs under the Hermes venv's
# interpreter, and whether a bare python3 is also on PATH in the container is an
# image detail no case should fail on.
DRIFT_POD_PYTHON_CANDIDATES="python3 /opt/hermes/.venv/bin/python3"
DRIFT_HEALTHZ_TIMEOUT=10
DRIFT_SESSION_TIMEOUT=10
DRIFT_INJECT_TIMEOUT=30
# The same string as build_payload.py's KIND, and it has to be: this is the
# kind drift_require_daemon demands the daemon advertise, that one is the kind
# the payload stamps. A pair that disagrees leaves the probe approving a daemon
# for a kind the record does not carry -- the failure drift_require_daemon's
# own comment calls the worst way for a fixture to be wrong. Held together by a
# test rather than by reading one from the other at run time, which would put a
# python call and an import path on the critical path of every apply to save
# one literal: bench/tests/test_drift_inject_payload.py asserts they match.
DRIFT_INJECT_KIND="gitops-drift"
DRIFT_SIGTERM_EXIT=143
DRIFT_ENVELOPE_FILE="envelope.json"
DRIFT_PAYLOAD_FILE="payload.json"
DRIFT_PROBE_STDERR="probe.err"
# How long, once the card is on the board, to wait for it to finish before the
# agent's turn starts. A card that is still running at this ceiling is handed
# over unfinished rather than failing the plant -- the case's prompt still
# tells the agent to wait -- so a slow night degrades to the old behaviour
# instead of losing the repetition. Sized above the slowest card the recorder
# nightly has shown these cases, and overridable per stack.
DRIFT_DEFAULT_CARD_FINISH_TIMEOUT=900
# What the probe prints for "finished" when the board has no status column, and
# when the newest card ended failed or cancelled -- settled, with no triage in it.
DRIFT_FINISHED_UNKNOWN=-1
DRIFT_FINISHED_WITHOUT_RESULT=2
DRIFT_BOARD_PROBE="board_probe.py"
DRIFT_BUILD_PAYLOAD="build_payload.py"

# Where this library and its two python siblings live. Resolved from
# BASH_SOURCE rather than from the caller's $PWD, which is the Terraform
# working directory and not necessarily this directory.
DRIFT_MODULE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Point kubectl at the cluster the agent is on, and arm the cleanup.
#
# Fetched here rather than inherited: devops-bench calls
# deployer.get_cluster_info() unconditionally after up(), which shells `gcloud
# container clusters get-credentials` with no --kubeconfig, so any deployer:
# tofu task earlier in the matrix leaves its own per-run cluster selected.
drift_init() {
  # A directory, not `mktemp` on its own: get-credentials refuses to load an
  # existing empty file, so it warns, writes a dated `.backup` beside it that
  # nothing then cleans up, and prints a WARNING that reads like a failure in a
  # CI log. Handing it a path that does not exist yet skips all three.
  drift_kubeconfig_dir="$(mktemp -d)"
  drift_failure_resources="${drift_failure_resources:-$DRIFT_DEFAULT_FAILURE_RESOURCES}"

  # A plant that fails must not leave the namespace behind: the leftover is what
  # breaks the NEXT run. The destroy-time provisioner cannot be what prevents it
  # -- Terraform taints a resource whose create-time provisioner failed and
  # skips destroy-time provisioners on a tainted resource, so the destroy
  # reports "1 destroyed" without running a line of it.
  #
  # `set +e` is load-bearing: errexit stays in force inside a trap, so the first
  # command that failed would abort it and skip the delete.
  drift_planted_ns="${drift_planted_ns:-}"
  drift_on_exit() {
    local status=$?
    set +e
    if [ "$status" -ne 0 ] && [ -n "$drift_planted_ns" ]; then
      echo "Plant failed (exit $status). State of $drift_ns before cleanup:" >&2
      kubectl get "$drift_failure_resources" -n "$drift_ns" -o wide >&2
      kubectl get events -n "$drift_ns" --sort-by=.metadata.creationTimestamp >&2
      echo "Deleting $drift_ns so the next run starts from a clean namespace." >&2
      kubectl delete namespace "$drift_ns" --ignore-not-found --wait=false >&2
    fi
    rm -rf "$drift_kubeconfig_dir"
  }
  trap drift_on_exit EXIT

  # A Prow deadline delivers SIGTERM, and bash does not run an EXIT trap when
  # the shell dies from an untrapped signal -- so without this the deadline kill
  # leaks the namespace exactly as a failed plant would.
  # Single-quoted so the exit code is read when the signal arrives rather than
  # baked in now. The two are the same value here, and single quotes are what
  # the linter asks for either way.
  # shellcheck disable=SC2016 # deliberate: expanded by the trap, not now
  trap 'exit $DRIFT_SIGTERM_EXIT' TERM INT

  KUBECONFIG="$drift_kubeconfig_dir/config"
  export KUBECONFIG

  drift_project="$drift_project_id"
  if [ -z "$drift_project" ]; then
    # GCPProvider.resolve_variables injects project_id for every task, so this
    # is the local-run path rather than the CI one.
    drift_project="$(gcloud config get-value project 2>/dev/null || true)"
  fi
  if [ -z "$drift_project" ]; then
    echo "ERROR: no project id. Pass -var project_id=... or set a gcloud default project; this stack needs one to fetch credentials for $drift_cluster." >&2
    exit 1
  fi

  gcloud container clusters get-credentials "$drift_cluster" \
    --location "$drift_location" --project "$drift_project" --quiet

  local pod="deployment/$drift_agent_deployment"
  drift_exec_in_pod=(kubectl exec "$pod" -n "$drift_agent_namespace" -c "$drift_agent_container" --)
  drift_exec_in_pod_stdin=(kubectl exec -i "$pod" -n "$drift_agent_namespace" -c "$drift_agent_container" --)
}

# Clear what an earlier run left behind, refusing a namespace this stack did not
# create.
#
# Gated on the managed-by label the plant writes, not on the name alone: this is
# the only unconditional namespace delete in a drift stack and it runs against
# the shared cluster the Platform Agent install lives on. `--ignore-not-found`
# on the delete because the destroy-time provisioner removes the namespace with
# `--wait=false`, so a previous run's delete may still be in flight.
drift_clear_namespace() {
  kubectl get namespace "$drift_ns" >/dev/null 2>&1 || return 0

  # Not swallowed: the check above has already proved the namespace exists, so a
  # failing read here is a transport or RBAC failure and not an absent label.
  # Swallowed with `|| true`, it becomes an empty value that the branch below
  # reports as "this stack did not create it" -- a refusal with the wrong
  # reason, and indistinguishable from a namespace that genuinely carries no
  # label. Left unswallowed, errexit aborts on the read itself with kubectl's
  # own stderr, so the branch below is only ever reached by a namespace that
  # was read successfully and carries the wrong label.
  local leftover_owner
  leftover_owner="$(kubectl get namespace "$drift_ns" \
    -o jsonpath='{.metadata.labels.managed-by}')"
  if [ "$leftover_owner" != "$drift_managed_by" ]; then
    echo "ERROR: $drift_ns already exists on $drift_cluster but is not labelled managed-by=$drift_managed_by (found '$leftover_owner'). This stack did not create it, so it will not delete it. Remove it by hand if it is stale." >&2
    exit 1
  fi
  echo "Found a leftover $drift_ns from an earlier run; deleting it before planting."
  if ! kubectl delete namespace "$drift_ns" --ignore-not-found --wait=true --timeout="${drift_leftover_timeout}s"; then
    echo "ERROR: could not clear the leftover $drift_ns within ${drift_leftover_timeout}s, so this run cannot plant a fresh scenario. A namespace wedged on a finalizer is the usual cause. Its current state follows." >&2
    kubectl get namespace "$drift_ns" -o yaml >&2 || true
    exit 1
  fi
}

# Refuse a daemon that cannot take the record.
#
# A daemon predating the gitops-drift dispatch takes the payload down its event
# path instead, grades it as a Warning Pod alert for `default/`, answers 200,
# and files a card that has nothing to do with drift. Every objective would then
# fail against a card that looks plausible in the transcript, which is the worst
# way for a fixture to be wrong.
#
# Called BEFORE the plant by every stack. It reads nothing the plant produces --
# only the kubeconfig and four plain variables -- so a stack whose plant waits
# out a failure does not spend those minutes before learning a condition that
# was knowable in ten seconds. /healthz is unauthenticated, so this also runs
# before the API key is needed, which separates "the install is too old" from
# "the key is wrong".
#
# jq runs inside the pod rather than on the runner: the agent image installs it,
# and the Prow runner has no jq in any of hack/'s scripts. `curl -fsS` with the
# body captured before jq sees it. Without -f, curl prints an error body and
# exits 0 on a 404; and inside `sh -c 'curl | jq'` the pipeline's status is jq's,
# which is 0 on empty input -- so a connection refused, a wrong daemon_url or a
# timeout all produced an empty `kinds` with a zero status, and the branch below
# blamed the install's age for a daemon that was simply down. Measured.
drift_require_daemon() {
  local kinds_rc=0 kinds
  # shellcheck disable=SC2016 # $1/$2 are the exec'd sh's arguments, not this shell's
  kinds="$("${drift_exec_in_pod[@]}" sh -c 'body="$(curl -fsS --max-time "$2" "$1/healthz")" || exit $?; printf "%s" "$body" | jq -r ".inject_kinds[]?"' sh "$drift_daemon_url" "$DRIFT_HEALTHZ_TIMEOUT")" || kinds_rc=$?
  if [ "$kinds_rc" -ne 0 ]; then
    echo "ERROR: could not read $drift_daemon_url/healthz in $drift_agent_container (exit $kinds_rc); kubectl's or curl's own error is above. This says nothing about which inject kinds the daemon advertises -- it is an exec, RBAC, connection or HTTP failure, and the install may be perfectly current." >&2
    exit 1
  fi
  if ! printf '%s\n' "$kinds" | grep -qxF "$DRIFT_INJECT_KIND"; then
    echo "ERROR: the Session KV daemon at $drift_daemon_url does not advertise the '$DRIFT_INJECT_KIND' inject kind (it advertises: $kinds). This install predates the drift dispatch, so a record sent to it would be graded as a Warning Pod event and reported back as delivered. Redeploy the agent image before running this case." >&2
    exit 1
  fi
}

# Mint the run's insert id into drift_insert_id, under prefix $1.
#
# Minted per apply, rendered into the card body by _drift_task_body, and matched
# on by drift_wait_for_card -- which is what makes the poll run-scoped without a
# per-run object name. One function so the prefix cannot drift from the value
# the poll looks for; a mismatch surfaces only as a card-wait timeout.
drift_mint_insert_id() {
  drift_insert_id="$1-$(date -u +%s)-$RANDOM"
  drift_changed_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}

# Compose the payload into drift_payload_file. Arguments are build_payload.py's,
# which documents them and validates their count.
drift_build_payload() {
  drift_payload_file="$drift_kubeconfig_dir/$DRIFT_PAYLOAD_FILE"
  python3 "$DRIFT_MODULE_DIR/$DRIFT_BUILD_PAYLOAD" "$drift_payload_file" "$@"
}

# Open a session and hand the daemon the payload drift_build_payload wrote.
#
# The key is read from the pod's own environment inside the exec, never passed
# in: SESSION_KV_API_KEY is on this container and a value that crossed the
# runner would be one argv away from a CI log.
drift_inject_record() {
  local session_id envelope_file inject_body inject_status

  # shellcheck disable=SC2016 # $SESSION_KV_API_KEY must expand in the pod, not here
  session_id="$("${drift_exec_in_pod[@]}" sh -c \
    'curl -sS --max-time "$2" -X POST -H "Authorization: Bearer $SESSION_KV_API_KEY" "$1/sessions" | jq -r ".sessionID // empty"' \
    sh "$drift_daemon_url" "$DRIFT_SESSION_TIMEOUT" || true)"
  if [ -z "$session_id" ]; then
    echo "ERROR: the daemon returned no sessionID. A 401 here means SESSION_KV_API_KEY on $drift_agent_container does not match what the daemon expects; a 503 means it is unset." >&2
    exit 1
  fi

  # The daemon's envelope carries the payload as a JSON *string*, not as a
  # nested object (injectMessageRequest in the detector's inject.go). Built with
  # python so the escaping is the language's problem and not a quoting bug that
  # surfaces as a 400.
  envelope_file="$drift_kubeconfig_dir/$DRIFT_ENVELOPE_FILE"
  python3 -c 'import json,sys; sys.stdout.write(json.dumps({"message": open(sys.argv[1]).read()}))' \
    "$drift_payload_file" > "$envelope_file"

  # The whole body is kept, not just .status. Without -f, curl exits 0 on a 400,
  # so reading only `.status` yielded an empty string and the stacks reported
  # "the request did not reach the drift route at all" while discarding the
  # `detail` that named the real cause.
  # shellcheck disable=SC2016 # $SESSION_KV_API_KEY must expand in the pod, not here
  inject_body="$("${drift_exec_in_pod_stdin[@]}" sh -c \
    'curl -sS --max-time "$2" -X POST -H "Authorization: Bearer $SESSION_KV_API_KEY" -H "Content-Type: application/json" --data-binary @- "$1"' \
    sh "$drift_daemon_url/sessions/$session_id/inject" "$DRIFT_INJECT_TIMEOUT" < "$envelope_file" || true)"
  inject_status="$(printf '%s' "$inject_body" | python3 -c 'import json,sys
try:
    sys.stdout.write(str(json.load(sys.stdin).get("status", "")))
except Exception:
    pass' || true)"

  # `suppressed` is a 200. The daemon accepted the record, spent nothing on it
  # and told nobody, because the day's drift alert ceiling is already gone -- so
  # no card is ever filed and the case would time out looking for one.
  #
  # This does NOT bite in CI: hack/ci-deploy.sh sets
  # EVAL_ALERT_DAILY_LIMIT_DRIFT=0 and _alert_daily_limit treats 0 as off, so an
  # eval install has no drift ceiling at all. It bites on a DEV install carrying
  # the shipped default, where every drift case shares the bucket -- which is
  # the install the eval-driven loop runs against, and the one this is for.
  if [ "$inject_status" = "suppressed" ]; then
    echo "ERROR: the daemon suppressed this record against its daily drift alert ceiling, so no triage card will be filed. This install has already taken its drift alerts for the day -- the other gitops-drift cases spend from the same bucket. Raise ALERT_DAILY_LIMIT_DRIFT or run against a fresh install." >&2
    exit 1
  fi
  if [ "$inject_status" != "injected" ]; then
    echo "ERROR: the daemon answered '$inject_status' rather than 'injected'. Its full response was: $inject_body" >&2
    exit 1
  fi
  echo "Daemon accepted the drift record (insert_id=$drift_insert_id)."
}

# Wait for the triage card carrying drift_insert_id to reach the board, and
# then to finish.
#
# The inject returns as soon as the daemon has queued the front-door turn, and
# that turn -- one kanban_create against the cluster agent scoped to the host
# cluster -- is what puts the card on the board. Without this wait the agent
# turn starts first, finds an empty board, and the case grades a correct agent
# on a fixture that had not finished.
#
# Matched on the run's own insert id, which _drift_task_body renders into the
# card body verbatim as `insertId=<id>` and which survives the defanging it
# passes through (_DRIFT_UNSAFE_CHARS_RE removes only backticks and newlines).
# Not on the title: the front door composes that and could reword it, and it
# carries no id.
drift_wait_for_card() {
  local board_probe pod_python candidate elapsed started bound found finished out probe_rc probe_err finish_timeout

  board_probe="$DRIFT_MODULE_DIR/$DRIFT_BOARD_PROBE"
  probe_err="$drift_kubeconfig_dir/$DRIFT_PROBE_STDERR"

  # `import sqlite3`, not `pass`: the probe's own import sits outside its try
  # block, so an interpreter that satisfies `pass` but has no _sqlite3 module
  # would be picked, fail on every poll, and surface at the timeout as "the
  # front-door turn filed nothing".
  pod_python=""
  for candidate in $DRIFT_POD_PYTHON_CANDIDATES; do
    if "${drift_exec_in_pod[@]}" "$candidate" -c 'import sqlite3' >/dev/null 2>&1; then
      pod_python="$candidate"
      break
    fi
  done
  if [ -z "$pod_python" ]; then
    echo "ERROR: no python interpreter found in $drift_agent_container to read the kanban board with." >&2
    exit 1
  fi

  # Integers, checked before either loop rather than discovered inside one. A
  # non-integer bound makes `[ "$elapsed" -ge "$bound" ]` an error, and an error
  # in an `if` condition is false rather than fatal under errexit, so the loop
  # would never reach its bound and would poll for ever. A digit string too wide
  # for the shell's integers does the same -- `[` reports it as "integer
  # expression expected" and the comparison is false even against itself -- so
  # the width is capped too: ten characters or more is refused, which is past
  # any wait here and well inside what `[ -ge ]` can compare.
  finish_timeout="${drift_card_finish_timeout:-$DRIFT_DEFAULT_CARD_FINISH_TIMEOUT}"
  for bound in "$drift_card_wait_timeout" "$finish_timeout" "$drift_poll_interval"; do
    case "$bound" in
      "" | *[!0-9]* | ??????????*)
        echo "ERROR: drift_card_wait_timeout, drift_card_finish_timeout and drift_poll_interval must be whole seconds; got '$bound'." >&2
        exit 1
        ;;
    esac
  done

  # Elapsed time is the clock's, not the sum of the sleeps. Each poll is a
  # kubectl exec that starts an interpreter in the pod, which is not free, so
  # counting only the sleeps would let a bound run several times its value and
  # would misreport how long the card took.
  started=$SECONDS
  while :; do
    drift_probe_board
    elapsed=$((SECONDS - started))
    if [ "$found" -ge 1 ]; then
      break
    fi
    if [ "$elapsed" -ge "$drift_card_wait_timeout" ]; then
      echo "ERROR: the daemon accepted the record but no kanban card carrying insert_id=$drift_insert_id appeared within ${elapsed}s. The front-door turn the daemon queued either never ran or filed nothing, so there is no triage for the agent to read." >&2
      drift_report_probe_error
      exit 1
    fi
    sleep "$drift_poll_interval"
  done
  echo "Triage card for $drift_insert_id appeared after ${elapsed}s."

  # Then wait for it to finish, here rather than in the agent's turn.
  #
  # This used to be left to the agent, on the grounds that polling kanban_show
  # is the read path the product offers. It does not hold up: the agent runtime
  # filters a tool call identical to an earlier one with the same result, and
  # tells the agent to "proceed with what you have". Waiting on a running card
  # is exactly that call, so on the recorder nightly of 2026-10-22 seven cases
  # each lost a repetition to an agent that obeyed the filter and reported a
  # card it had not seen finish -- two of them CUJ 1's, after 12 and 14 polls,
  # where every repetition that passed had needed at least 15. The cases grade
  # the triage, not the polling, so the wait moves here and the agent's turn
  # becomes the read it was meant to be. The runtime gap is #3761.
  #
  # Inside the unit either way: a repetition that would have passed already
  # spent this wait in the agent's turn, so the typical case costs the
  # infra-lock chain nothing. A card still running at the ceiling adds up to
  # that ceiling on top of whatever the agent then waits.
  #
  # The appearance loop's last read already carries `finished`, so it is
  # judged before the first sleep rather than re-probed straight away.
  started=$SECONDS
  while :; do
    elapsed=$((SECONDS - started))
    if [ "$finished" -eq "$DRIFT_FINISHED_UNKNOWN" ]; then
      echo "WARNING: the board has no status column to read, so this run cannot wait for the card to finish; the agent's turn starts with the card in whatever state it is in." >&2
      return 0
    fi
    if [ "$finished" -eq "$DRIFT_FINISHED_WITHOUT_RESULT" ]; then
      # The pipeline failed rather than the agent: the front-door worker's card
      # ended with nothing in it, so there is no triage to read. Failing the
      # plant excludes the repetition instead of grading an agent against it,
      # which is what the sibling fixtures that wait on a card do and what the
      # appearance timeout above does for the same reason.
      echo "ERROR: the triage card for $drift_insert_id ended failed or cancelled ${elapsed}s after it appeared, so there is no triage for the agent to read. This is the install, not the agent; failing the plant so the repetition is excluded rather than graded." >&2
      drift_report_probe_error
      exit 1
    fi
    if [ "$finished" -eq 1 ]; then
      echo "Triage card for $drift_insert_id finished after a further ${elapsed}s."
      return 0
    fi
    if [ "$elapsed" -ge "$finish_timeout" ]; then
      echo "WARNING: the triage card for $drift_insert_id was not seen to finish within ${elapsed}s of appearing. Handing it to the agent's turn as it is; the case's prompt tells the agent to wait for it." >&2
      drift_report_probe_error
      return 0
    fi
    sleep "$drift_poll_interval"
    drift_probe_board
  done
}

# One read of the board into `found` and `finished`. Internal to
# drift_wait_for_card: it reads and writes that function's locals through
# bash's dynamic scope, so called from anywhere else it would set globals and
# read names nobody set.
#
# Only a reply of exactly two integers counts. `[ "" -ge 1 ]` is a syntax error
# under errexit, and a kubectl exec that fails halfway can return partial output
# as easily as none -- including a single token, which a naive split would read
# as both numbers at once. Anything else reads as "nothing yet" and the caller
# keeps polling. The exec's stderr is kept for drift_report_probe_error, so a
# refused exec is not taken for a verdict about the front door or the card.
drift_probe_board() {
  probe_rc=0
  out="$("${drift_exec_in_pod_stdin[@]}" "$pod_python" - "$drift_insert_id" < "$board_probe" 2>"$probe_err")" || probe_rc=$?
  found=0
  finished=0
  case "$out" in
    *" "*" "*) return 0 ;;
    *" "*) ;;
    *) return 0 ;;
  esac
  case "${out%% *}" in
    "" | *[!0-9]*) return 0 ;;
  esac
  case "${out#* }" in
    "$DRIFT_FINISHED_UNKNOWN") ;;
    "" | *[!0-9]*) return 0 ;;
  esac
  found="${out%% *}"
  finished="${out#* }"
}

# Say what the last board read failed with, if it failed. Internal to
# drift_wait_for_card, for the same reason as drift_probe_board. Without it a
# pod that rolled or an exec that was refused would read as a card that never
# appeared, or one that never finished.
drift_report_probe_error() {
  if [ "$probe_rc" -ne 0 ] || [ -s "$probe_err" ]; then
    echo "The board probe's last run exited $probe_rc and said:" >&2
    cat "$probe_err" >&2
  fi
}
