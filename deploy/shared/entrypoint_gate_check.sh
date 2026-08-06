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
# because every case writes to a fresh scratch $PLATFORM_AGENT_HOME under /tmp and never
# touches the real one. That is why the environment is cleared with `env -u` per case
# rather than assumed empty: under the operator the variable is already set in the
# container, and inheriting it would silently test something other than what is named.
set -eu

ENTRYPOINT="${ENTRYPOINT:-/usr/local/bin/agent-entrypoint}"
failures=0
checks=0

# The gated steps create these; step 1 above the gate creates neither. This is the marker
# that means the same thing here as on a host, which $TARGET_DIR/logs does not.
setup_ran() {
    [ -d "$1/scripts" ] || [ -f "$1/profiles/platform/profile.yaml" ]
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
    home=$(mktemp -d /tmp/gate-check.XXXXXX)
    out="$home.out"
    err="$home.err"

    if [ -n "$envval" ]; then
        env -u AGENT_SHARED_STATE_SETUP AGENT_SHARED_STATE_SETUP="$envval" \
            PLATFORM_AGENT_HOME="$home" "$ENTRYPOINT" "$@" >"$out" 2>"$err" || true
    else
        env -u AGENT_SHARED_STATE_SETUP \
            PLATFORM_AGENT_HOME="$home" "$ENTRYPOINT" "$@" >"$out" 2>"$err" || true
    fi

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
home=$(mktemp -d /tmp/gate-handoff.XXXXXX)
wrapper="$home.wrapper"
cat > "$wrapper" <<'STUB'
#!/bin/sh
# Stands in for leader_elect.py, and asserts what the real wrapper would find.
[ -n "${HERMES_HOME:-}" ] || { echo "WRAPPER-SAW-NO-HERMES-HOME"; exit 1; }
[ -d "$HERMES_HOME/scripts" ] || { echo "WRAPPER-SAW-UNBUILT-TREE"; exit 1; }
echo "WRAPPER-STARTED-ON-BUILT-TREE"
STUB
chmod 0755 "$wrapper"
env -u AGENT_SHARED_STATE_SETUP AGENT_SHARED_STATE_SETUP=owner \
    PLATFORM_AGENT_HOME="$home" "$ENTRYPOINT" /bin/sh "$wrapper" >"$home.out" 2>"$home.err" || true
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
home=$(mktemp -d /tmp/gate-skeleton.XXXXXX)
env -u AGENT_SHARED_STATE_SETUP AGENT_SHARED_STATE_SETUP=skip \
    PLATFORM_AGENT_HOME="$home" "$ENTRYPOINT" /bin/echo hermes dashboard >/dev/null 2>&1 || true
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

echo
if [ "$failures" -ne 0 ]; then
    echo "$failures of $checks checks FAILED"
    exit 1
fi
echo "all $checks checks passed"
