#!/bin/sh
# Verify the shared-state gate (step 1.5 of docker-entrypoint.sh) INSIDE the real image.
#
# tests/test_docker_entrypoint.py covers the same decision table, and cannot cover this.
# It runs on a developer host, where every step below the gate is a no-op — each one is
# guarded on /opt/defaults or /opt/hermes, and neither exists there. So the host tests
# prove which BRANCH the gate takes and nothing about what that branch then does. The gap
# is not theoretical: the marker they key on, $TARGET_DIR/logs, is created inside the image
# by step 1, which runs above the gate in EVERY container. On a host that never happens, so
# the marker looks reliable; in a container it reports "the setup ran" for exactly the
# sidecars the gate exists to stop. That was found by probing a live pod, which is not a
# thing a pull request can do.
#
# Run as a build stage (`--target entrypoint-gate-test`), where a failure fails the build.
# It is also safe to run inside a live pod — `kubectl exec ... -- entrypoint-gate-check` —
# and CONFINING it to that scratch tree is what run_entrypoint() below is for. Scoping
# $PLATFORM_AGENT_HOME is not on its own enough, because the owner path writes to two
# things that are not derived from $TARGET_DIR, and inside a pod both are real:
#
#   - Step 4 points $HOME/.hermes/plugins/hermes_otel/config.yaml at the generated config,
#     because hermes-otel resolves its config below ~/.hermes whatever HERMES_HOME says.
#     $HOME in the gateway is /opt/data/home — ON THE DATA PVC. An unscoped run leaves that
#     link pointing into a scratch directory the next line deletes, so every hermes process
#     started in the container afterwards reads a dangling link, and it stays that way
#     until a restart re-runs step 4. Hence a scratch HOME per case, plus a before/after
#     assertion on the real one at the end.
#   - Step 5 starts the Session KV server on port 8699, which is pod-wide and cannot be
#     scoped by a variable at all. Every owner case spawns one, so run_entrypoint reaps it
#     by its scratch path the moment the case returns. Where the pod's real server is up
#     the spawn loses the bind and exits by itself; where it is DOWN — a plausible reason
#     to be in here diagnosing — the scratch one can hold 8699 for the fraction of a second
#     before it is killed. Either way nothing outlives the case, and the last check proves
#     that rather than asserting it.
#
# The environment is cleared with `env -u` per case rather than assumed empty for the same
# family of reasons: under the operator AGENT_SHARED_STATE_SETUP is already set in the
# container, and inheriting it would silently test something other than what is named.
set -eu

ENTRYPOINT="${ENTRYPOINT:-/usr/local/bin/agent-entrypoint}"
SCRATCH_PREFIX=/tmp/gate-
failures=0
checks=0

# The container's own HOME, which no case may touch. Captured before anything runs, because
# every case replaces it — see the header.
REAL_HOME=${HOME:-}

# The gated steps create these; step 1 above the gate creates neither. This is the marker
# that means the same thing here as on a host, which $TARGET_DIR/logs does not.
setup_ran() {
    [ -d "$1/scripts" ] || [ -f "$1/profiles/platform/profile.yaml" ]
}

# What the real compat link looks like right now, as one comparable line. The link, not the
# file it resolves to: pointing it somewhere else is the damage, and a dangling link reads
# as absent to every test that only asks whether the path exists.
otel_compat_state() {
    [ -n "$REAL_HOME" ] || { echo "no HOME set"; return 0; }
    path="$REAL_HOME/.hermes/plugins/hermes_otel/config.yaml"
    if [ -L "$path" ]; then
        echo "symlink -> $(readlink "$path")"
    elif [ -e "$path" ]; then
        echo "regular file"
    else
        echo "absent"
    fi
}

# PIDs whose command line mentions $1. The scratch paths come from mktemp, so they are
# unique to this run and match nothing else in the pod — in particular not the gateway's
# own Session KV server, which is rooted at /opt/data.
scratch_pids() {
    [ -d /proc ] || return 0
    for proc in /proc/[0-9]*; do
        pid=${proc#/proc/}
        if [ "$pid" = "$$" ] || [ "$pid" = 1 ]; then
            continue
        fi
        cmdline=$(tr '\0' ' ' <"$proc/cmdline" 2>/dev/null) || continue
        case "$cmdline" in
            *"$1"*) printf '%s\n' "$pid" ;;
        esac
    done
}

# SIGKILL rather than a polite SIGTERM and a wait: what this reaps is step 5's Session KV
# server, spawned against a scratch tree that is deleted a line or two later, so there is
# no state worth flushing and no reason to make the caller pay a second per case for it.
reap_scratch() {
    for pid in $(scratch_pids "$1"); do
        kill -9 "$pid" 2>/dev/null || true
    done
}

# The single door to the entrypoint. Every case goes through it, so the scratch HOME and
# the reap cannot be forgotten at a call site — which is exactly how the real ~/.hermes
# came to be in scope in the first place.
run_entrypoint() {
    scratch=$1
    envval=$2
    out=$3
    err=$4
    shift 4

    if [ -n "$envval" ]; then
        env -u AGENT_SHARED_STATE_SETUP AGENT_SHARED_STATE_SETUP="$envval" \
            PLATFORM_AGENT_HOME="$scratch" HOME="$scratch/home" \
            "$ENTRYPOINT" "$@" >"$out" 2>"$err" || true
    else
        env -u AGENT_SHARED_STATE_SETUP \
            PLATFORM_AGENT_HOME="$scratch" HOME="$scratch/home" \
            "$ENTRYPOINT" "$@" >"$out" 2>"$err" || true
    fi

    reap_scratch "$scratch"
}

# want: "owner" or "skip" — the decision the gate should reach and announce.
#
# Every case must exec /bin/echo. The gate reads argv, so a case can spell out any command
# it likes without that command being the one that runs — and it must not be. Passing a
# real `hermes gateway run` here starts a SECOND gateway inside a live pod, next to the
# one already serving, and the entrypoint never returns; the check hangs rather than
# failing. Asserting the stand-in makes that unwritable instead of merely discouraged.
check() {
    name=$1
    want=$2
    envval=$3
    shift 3

    if [ "$#" -gt 0 ] && [ "$1" != "/bin/echo" ]; then
        printf 'FAIL  %-44s case must exec /bin/echo, not %s\n' "$name" "$1"
        failures=$((failures + 1))
        checks=$((checks + 1))
        return 0
    fi

    checks=$((checks + 1))
    home=$(mktemp -d "${SCRATCH_PREFIX}check.XXXXXX")
    out="$home.out"
    err="$home.err"

    run_entrypoint "$home" "$envval" "$out" "$err" "$@"

    # "does not own the shared state" contains "own the", not "owns the", so at most one
    # of these matches. Zero matches is itself a failure: a gate that decides silently is
    # one nothing can check.
    said_owner=no
    said_skip=no
    grep -q "owns the shared state" "$err" && said_owner=yes
    grep -q "does not own the shared state" "$err" && said_skip=yes

    if [ "$said_owner" = "$said_skip" ]; then
        printf 'FAIL  %-44s the gate announced no decision (or both)\n' "$name"
        sed 's/^/        /' "$err" | head -5
        failures=$((failures + 1))
        rm -rf "$home" "$out" "$err"
        return 0
    fi

    [ "$said_owner" = yes ] && said=owner || said=skip
    setup_ran "$home" && did=owner || did=skip

    if [ "$said" != "$want" ]; then
        printf 'FAIL  %-44s announced %s, wanted %s\n' "$name" "$said" "$want"
        failures=$((failures + 1))
    elif [ "$did" != "$want" ]; then
        # The announcement and the behaviour disagreeing is worse than either being wrong,
        # because the logs would then exonerate the container that did the damage.
        printf 'FAIL  %-44s announced %s but actually %s\n' "$name" "$said" "$did"
        failures=$((failures + 1))
    else
        printf 'ok    %-44s %s\n' "$name" "$said"
    fi

    rm -rf "$home" "$out" "$err"
}

echo "== shared-state gate, in-image =="

otel_compat_before=$(otel_compat_state)

# The operator's two containers, decided by the variable it sets on each.
check "operator gateway (owner)"            owner owner /bin/echo hermes gateway run
check "operator dashboard (skip)"           skip  skip  /bin/echo hermes dashboard

# The HA shape. argv carries no bare `gateway` because leader_elect.py starts the gateway
# as a CHILD, so the declaration is the only thing that gets this right. Setting Command
# instead of Args once removed the entrypoint from the chain entirely and left an HA pod
# with no container building the tree at all.
check "HA gateway, declared owner"          owner owner /bin/echo /opt/data/leader_elect.py
check "HA gateway, argv alone (known gap)"  skip  ""    /bin/echo /opt/data/leader_elect.py

# Auto-detection, for deployments with no operator to ask.
check "auto: hermes gateway run"            owner ""    /bin/echo hermes gateway run
check "auto: hermes dashboard"              skip  ""    /bin/echo hermes dashboard
check "auto: absolute path to hermes"       owner ""    /bin/echo /opt/hermes/.venv/bin/hermes gateway run
check "auto: gateway only as a value"       skip  ""    /bin/echo hermes kanban ls --board gateway-x
check "auto: unknown future sidecar"        skip  ""    /bin/echo hermes some-future-subcommand

# A typo in the escape hatch must not read as having set nothing.
check "typo 'Owner' falls back to auto"     skip  Owner /bin/echo hermes dashboard

# Empty argv. `exec` with no operands returns instead of replacing the shell, so an
# explicit skip used to fall through into the setup it exists to prevent.
check "skip + empty argv"                   skip  skip
check "setup-only invocation"               owner ""

# The HA handoff, which is the regression that started all of this. At more than one
# replica the operator gives the gateway `Args: [python3, <home>/leader_elect.py]` and no
# Command, so the image ENTRYPOINT still runs and leader_elect.py becomes its `exec "$@"`
# target. Setting Command instead removed the entrypoint from the chain entirely: the
# wrapper started against a $HERMES_HOME nobody had built — no scripts/router_server.py
# for the MCP server its own config.yaml names, no platform profile, no Session KV server.
#
# The cases above prove the tree exists once the entrypoint is done. This proves it exists
# AT THE MOMENT OF HANDOFF, which is the ordering the wrapper depends on and the only part
# a manifest test cannot see. A stub stands in for leader_elect.py deliberately: the real
# one hardcodes Popen(["hermes","gateway","run"]) and would leave a second gateway running
# beside the first. Its own behaviour is unchanged by this and is covered by
# k8s-operator/internal/controller/test_leader_elect.py.
checks=$((checks + 1))
home=$(mktemp -d "${SCRATCH_PREFIX}handoff.XXXXXX")
wrapper="$home.wrapper"
cat > "$wrapper" <<'STUB'
#!/bin/sh
# Stands in for leader_elect.py, and asserts what the real wrapper would find.
[ -n "${HERMES_HOME:-}" ] || { echo "WRAPPER-SAW-NO-HERMES-HOME"; exit 1; }
[ -d "$HERMES_HOME/scripts" ] || { echo "WRAPPER-SAW-UNBUILT-TREE"; exit 1; }
echo "WRAPPER-STARTED-ON-BUILT-TREE"
STUB
chmod 0755 "$wrapper"
run_entrypoint "$home" owner "$home.out" "$home.err" /bin/sh "$wrapper"
if grep -q WRAPPER-STARTED-ON-BUILT-TREE "$home.out" 2>/dev/null; then
    printf 'ok    %-44s %s\n' "HA handoff: wrapper execs onto a built tree" "owner"
else
    echo "FAIL  HA handoff: the wrapper did not start on a built tree"
    sed 's/^/        /' "$home.out" "$home.err" 2>/dev/null | head -6
    failures=$((failures + 1))
fi
rm -rf "$home" "$wrapper" "$home.out" "$home.err"

# The trap this file exists for, asserted rather than described: a skipped container must
# leave none of the gated artifacts behind, whatever else step 1 put there.
home=$(mktemp -d "${SCRATCH_PREFIX}skeleton.XXXXXX")
run_entrypoint "$home" skip /dev/null /dev/null /bin/echo hermes dashboard
checks=$((checks + 1))
if setup_ran "$home"; then
    echo "FAIL  a skipped container produced gated artifacts in \$PLATFORM_AGENT_HOME"
    failures=$((failures + 1))
else
    printf 'ok    %-44s left: %s\n' "skipped container writes no gated artifact" \
        "$(ls -A "$home" 2>/dev/null | tr '\n' ' ')"
    echo "      (^ that listing is step 1 running above the gate in every container;"
    echo "         it is why \$PLATFORM_AGENT_HOME/logs is not evidence the setup ran)"
fi
rm -rf "$home"

# Step 4's endpoint sweep, which only the image can prove. hermes_otel does not read
# OTEL_EXPORTER_OTLP_ENDPOINT — its backend URL is baked into the plugin config at build
# time — so "the env var is set on the pod" says nothing about where spans actually go.
# What has to hold is that every copy of that config was rewritten, including the platform
# profile's, which is a separate file the sweep has to find rather than one it is handed.
#
# Through run_entrypoint like every other case: a direct call would put the pod's real
# ~/.hermes compat link in scope, which is the hazard the header documents.
checks=$((checks + 1))
home=$(mktemp -d "${SCRATCH_PREFIX}otel.XXXXXX")
sentinel="http://gate-check-sentinel.otel-collector.svc.cluster.local:4318"
# Exported rather than prefixed onto the call: a prefixed assignment on a *function*
# persists after it returns in several POSIX shells, which would leak into every case
# below this one.
OTEL_EXPORTER_OTLP_ENDPOINT="$sentinel"
OTEL_SERVICE_NAME=gate-check-gateway
export OTEL_EXPORTER_OTLP_ENDPOINT OTEL_SERVICE_NAME
run_entrypoint "$home" owner "$home.out" "$home.err" /bin/echo hermes gateway run
unset OTEL_EXPORTER_OTLP_ENDPOINT OTEL_SERVICE_NAME

otel_missed=""
for cfg in "$home/plugins/hermes_otel/config.yaml" "$home/profiles/platform/plugins/hermes_otel/config.yaml"; do
    [ -f "$cfg" ] || { otel_missed="$otel_missed $cfg(absent)"; continue; }
    grep -q "gate-check-sentinel" "$cfg" || otel_missed="$otel_missed $cfg"
done
if [ -n "$otel_missed" ]; then
    echo "FAIL  step 4 left a hermes_otel config on the baked endpoint:$otel_missed"
    failures=$((failures + 1))
else
    printf 'ok    %-44s %s\n' "otel endpoint reaches root + platform profile" "$sentinel"
fi
rm -rf "$home" "$home.out" "$home.err"

# The two ways a run escapes its scratch tree, checked instead of promised. Both matter
# most in the place the header recommends running this — a live pod — and neither is
# visible from the decision table above.
#
# The compat link first. Step 4 derives it from $HOME, not $TARGET_DIR, so before
# run_entrypoint scoped HOME every owner case repointed the pod's real link at a scratch
# directory and then deleted it. Comparing the rendered state, not just existence: a
# dangling symlink is absent to `[ -e ]` and present to `[ -L ]`, and the second is what
# it becomes.
checks=$((checks + 1))
otel_compat_after=$(otel_compat_state)
if [ "$otel_compat_before" != "$otel_compat_after" ]; then
    echo "FAIL  a case escaped its scratch tree and changed the real \$HOME otel compat link"
    echo "        before: $otel_compat_before"
    echo "        after:  $otel_compat_after"
    failures=$((failures + 1))
else
    printf 'ok    %-44s %s\n' "real \$HOME otel compat link untouched" "$otel_compat_before"
fi

# And the port. Nothing scopes 8699, so the owner cases each start a Session KV server
# against their scratch tree; run_entrypoint kills each one as its case returns. This is
# the receipt for that, and the only check here that can fail because of something an
# earlier case left running rather than something the gate decided.
checks=$((checks + 1))
stray=$(scratch_pids "$SCRATCH_PREFIX" | tr '\n' ' ')
if [ -n "$stray" ]; then
    echo "FAIL  processes from this run are still alive against deleted scratch trees: $stray"
    echo "      (step 5's Session KV server holds pod-wide port 8699; reaping missed it)"
    failures=$((failures + 1))
else
    printf 'ok    %-44s %s\n' "no scratch process left running" "8699 released"
fi

echo
if [ "$failures" -ne 0 ]; then
    echo "$failures of $checks checks FAILED"
    exit 1
fi
echo "all $checks checks passed"
