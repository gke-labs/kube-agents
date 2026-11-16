#!/usr/bin/env bash
# Run one test command several times while busy loops load the same cores, and
# print the one line the pull request's Testing section carries, for example:
#
#   10 of 10 passed under load (4 cores, 12 busy loops): go test -C a2a ./mcp/ -run TestX -race
#
# Usage:
#   scripts/dev/repeat_under_load.sh [-n RUNS] [-c CORES] [-l LOOPS_PER_CORE] -- <command...>
#
# The command and the busy loops are pinned with taskset to the first CORES cpus this
# process may use, so the load lands on the test rather than on idle cores. Go's test result cache
# is turned off through GOFLAGS, otherwise every run after the first replays the
# first. The command reads stdin from /dev/null, so every run sees the same input.
# Each run's output is kept as run-<k>.log in a temporary directory named on
# stderr. A run that fails makes the exit code non-zero and is named on stderr.
set -euo pipefail

readonly DEFAULT_RUNS=10
readonly DEFAULT_CORES=4
readonly DEFAULT_LOOPS_PER_CORE=3
readonly LOG_DIR_TEMPLATE="repeat-under-load.XXXXXX"
readonly LOAD_PIDS_FILE="load.pids"
readonly RUN_LOG_PREFIX="run-"
readonly RUN_LOG_SUFFIX=".log"
readonly GO_NO_CACHE_FLAG="-count=1"
readonly NO_PINNING_NOTE="no pinning: taskset missing"
readonly CANNOT_BIND_NOTE="no pinning: taskset cannot bind cpus"
# More digits than any count a person types.
readonly MAX_COUNT_DIGITS=6
# More busy loops than any machine should be asked for; a mistyped -l stops here.
readonly MAX_BUSY_LOOPS=256
readonly CPUS_ALLOWED_FILE="/proc/self/status"
readonly CPUS_ALLOWED_KEY="Cpus_allowed_list:"
readonly USAGE_EXIT=2
# Each loop spins while its parent, the script, is alive, so a script killed outright
# (SIGKILL, the OOM killer) does not leave loops burning; kill -0 is a builtin, no fork.
readonly BUSY_LOOP='while kill -0 "$PPID" 2>/dev/null; do :; done'
# What the script exits with when a signal stops it, as a shell would.
readonly SIGHUP_EXIT=129
readonly SIGINT_EXIT=130
readonly SIGTERM_EXIT=143
# How long a stopped run may take to print its own interrupt report before it is
# terminated, then how long it may take to leave before it is killed: so many sleeps of
# STOP_GRACE_TICK seconds each.
readonly STOP_GRACE_TICKS=50
readonly KILL_GRACE_TICKS=20
readonly STOP_GRACE_TICK=0.1

# Prints the comment block under the shebang, up to the first line that is not one,
# then exits with the code given: 0, on stdout, for help that was asked for; USAGE_EXIT,
# on stderr, otherwise.
usage() {
  local code=${1:-$USAGE_EXIT}
  if [ "$code" -eq 0 ]; then
    awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "${BASH_SOURCE[0]}"
  else
    awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "${BASH_SOURCE[0]}" >&2
  fi
  exit "$code"
}

# The cpus this process may run on, one per line, from the kernel's allowed list, so
# a container whose cpuset does not start at cpu 0 still pins where it can and the
# count does not follow OMP_NUM_THREADS the way nproc does. Prints nothing when the
# list cannot be read.
allowed_cpus() {
  local list range lo hi cpu
  list=$(grep "^$CPUS_ALLOWED_KEY" "$CPUS_ALLOWED_FILE" 2>/dev/null | awk '{print $2}' || true)
  [ -n "$list" ] || return 0
  for range in ${list//,/ }; do
    lo=${range%-*}
    hi=${range#*-}
    for ((cpu = lo; cpu <= hi; cpu++)); do
      echo "$cpu"
    done
  done
}

# The option's value, or usage when the flag is the last word or is followed by
# another flag.
value_of() {
  [ $# -ge 2 ] || usage
  case "$2" in -*) echo "$1 needs a value" >&2; usage ;; esac
  printf '%s' "$2"
}

runs=$DEFAULT_RUNS
cores=$DEFAULT_CORES
loops_per_core=$DEFAULT_LOOPS_PER_CORE
while [ $# -gt 0 ]; do
  case "$1" in
    -n) runs=$(value_of "$@"); shift 2 ;;
    -c) cores=$(value_of "$@"); shift 2 ;;
    -l) loops_per_core=$(value_of "$@"); shift 2 ;;
    --) shift; break ;;
    -h|--help) usage 0 ;;
    *) echo "unknown option: $1" >&2; usage ;;
  esac
done
[ $# -gt 0 ] || usage
command=("$@")
for n in "$runs" "$cores" "$loops_per_core"; do
  case "$n" in
    ''|*[!0-9]*) echo "runs, cores and loops per core must be whole numbers above zero" >&2; usage ;;
  esac
  if [ ${#n} -gt "$MAX_COUNT_DIGITS" ]; then
    echo "runs, cores and loops per core take at most $MAX_COUNT_DIGITS digits" >&2; usage
  fi
done
# Base ten whatever the spelling: printf '%d' would read a leading zero as octal.
runs=$((10#$runs))
cores=$((10#$cores))
loops_per_core=$((10#$loops_per_core))
for n in "$runs" "$cores" "$loops_per_core"; do
  [ "$n" -gt 0 ] || { echo "runs, cores and loops per core must be above zero" >&2; usage; }
done

mapfile -t allowed < <(allowed_cpus)
if [ ${#allowed[@]} -gt 0 ]; then
  available_cores=${#allowed[@]}
else
  available_cores=$(nproc 2>/dev/null || echo "$cores")
fi
if [ "$cores" -gt "$available_cores" ]; then
  cores=$available_cores
fi
loops=$((cores * loops_per_core))
if [ "$loops" -gt "$MAX_BUSY_LOOPS" ]; then
  echo "$loops busy loops is above the ceiling of $MAX_BUSY_LOOPS" >&2; usage
fi

pin=()
pinning_note=""
if [ ${#allowed[@]} -gt 0 ]; then
  cpu_list=$(IFS=,; echo "${allowed[*]:0:$cores}")
else
  cpu_list="0-$((cores - 1))"
fi
if ! command -v taskset >/dev/null 2>&1; then
  pinning_note=", $NO_PINNING_NOTE"
elif taskset -c "$cpu_list" true 2>/dev/null; then
  pin=(taskset -c "$cpu_list")
else
  pinning_note=", $CANNOT_BIND_NOTE $cpu_list"
fi

log_dir=$(mktemp -d -t "$LOG_DIR_TEMPLATE")
echo "logs: $log_dir" >&2

# The busy loops die with the script: the EXIT trap ends them, and each ends itself
# when the script is gone without one.
load_pids=()
stop_load() {
  if [ ${#load_pids[@]} -gt 0 ]; then
    kill "${load_pids[@]}" 2>/dev/null || true
    wait "${load_pids[@]}" 2>/dev/null || true
  fi
}
trap stop_load EXIT
# A test command that handles Ctrl-C itself (go test and pytest both do) returns
# normally, and bash would go on to the next run; these traps end the script instead.
# The run in flight is stopped first: it runs in the background under `wait`, which a
# trapped signal interrupts at once, where a foreground command would be waited out.
# With setsid each run is its own process group, so the signal reaches the whole tree
# (go test's pkg.test binary, pytest's workers) and not just the pid; the handler
# forwards the signal there, since the terminal's Ctrl-C no longer does, waits a grace
# period for the run's own interrupt report, then terminates what is left.
run_group=()
if command -v setsid >/dev/null 2>&1; then
  run_group=(setsid)
fi
cmd_pid=""
# Signals the run's process group, or its pid where there is no group yet: a signal
# can land in the instant before the child's setsid has run.
signal_run() {
  if [ ${#run_group[@]} -gt 0 ] && kill "-$1" -- "-$cmd_pid" 2>/dev/null; then
    return 0
  fi
  kill "-$1" "$cmd_pid" 2>/dev/null || true
}
run_is_alive() {
  { [ ${#run_group[@]} -gt 0 ] && kill -0 -- "-$cmd_pid" 2>/dev/null; } || kill -0 "$cmd_pid" 2>/dev/null
}
# Waits up to so many ticks for the run to be gone: the whole process group where
# there is one, so a child that outlives the leader gets the same grace.
wait_for_run() {
  local ticks=$1 tick
  for ((tick = 0; tick < ticks; tick++)); do
    run_is_alive || return 0
    sleep "$STOP_GRACE_TICK"
  done
}
# Stops the run in flight, if one is recorded, and exits. A signal that lands in the
# instant between starting a run and recording its pid leaves that run unsignalled; it
# is a foreign command in its own session, and guessing at it from the job table can
# pick a busy loop instead and wait on it forever.
stop_run_and_exit() {
  local code=$1 sig=$2
  # A second Ctrl-C during the grace would restart the escalation; let it run through.
  trap '' INT TERM HUP
  if [ -n "$cmd_pid" ]; then
    signal_run "$sig"
    wait_for_run "$STOP_GRACE_TICKS"
    signal_run TERM
    wait_for_run "$KILL_GRACE_TICKS"
    signal_run KILL
    wait "$cmd_pid" 2>/dev/null || true
  fi
  exit "$code"
}
trap 'stop_run_and_exit $SIGINT_EXIT INT' INT
trap 'stop_run_and_exit $SIGTERM_EXIT TERM' TERM
trap 'stop_run_and_exit $SIGHUP_EXIT HUP' HUP

for _ in $(seq "$loops"); do
  ${pin[@]+"${pin[@]}"} bash -c "$BUSY_LOOP" </dev/null >/dev/null 2>&1 &
  load_pids+=("$!")
done
printf '%s\n' "${load_pids[@]}" >"$log_dir/$LOAD_PIDS_FILE"

export GOFLAGS="${GOFLAGS:+$GOFLAGS }$GO_NO_CACHE_FLAG"

passed=0
failed_runs=()
for k in $(seq "$runs"); do
  echo "run $k of $runs" >&2
  # An async command in a non-interactive shell inherits SIGINT and SIGQUIT ignored,
  # which would hide Ctrl-C from the test; the subshell restores them before exec.
  (
    trap - INT QUIT
    exec ${run_group[@]+"${run_group[@]}"} ${pin[@]+"${pin[@]}"} "${command[@]}"
  ) </dev/null >"$log_dir/$RUN_LOG_PREFIX$k$RUN_LOG_SUFFIX" 2>&1 &
  cmd_pid=$!
  if wait "$cmd_pid"; then
    passed=$((passed + 1))
  else
    failed_runs+=("$k")
  fi
  cmd_pid=""
done

# The command is quoted so the line re-runs when pasted; a bare word stays bare.
command_text=$(printf '%q ' "${command[@]}")
printf '%d of %d passed under load (%d cores, %d busy loops%s): %s\n' \
  "$passed" "$runs" "$cores" "$loops" "$pinning_note" "${command_text% }"
if [ ${#failed_runs[@]} -gt 0 ]; then
  echo "failed runs: ${failed_runs[*]} (logs in $log_dir)" >&2
  exit 1
fi
