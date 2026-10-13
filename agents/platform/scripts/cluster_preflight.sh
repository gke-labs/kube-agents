#!/bin/bash
# cluster_preflight.sh - Read-only self-check a Cluster Agent runs before working
# a kanban task, so it can report *what is wrong* instead of crashing silently.
#
# Why this exists: a dispatcher-spawned Cluster Agent whose environment is broken
# (missing/stale KUBECONFIG, unreachable cluster, missing identity) would exit
# without ever calling kanban_complete/kanban_block. Hermes then marks the card
# crashed and the user only sees a generic "the agent crashed on startup" with no
# cause. The Cluster Agent runs this first (see cluster SOUL.md §6); on FAILED it
# blocks the card with the reason below (kanban_block kind="needs_input").
#
# Deploys to /opt/data/scripts/cluster_preflight.sh via the same path as the
# other shared scripts (agents/platform/scripts -> /opt/defaults/scripts ->
# /opt/data/scripts), so it reaches existing cluster profiles on image roll.
#
# The checks answer "am I about to investigate the cluster I was scoped to?", in
# order, stopping at the first failure:
#   1. USER.md records a full project/cluster/location identity.
#   2. A kubeconfig is pinned and non-empty.
#   3. That kubeconfig selects the cluster USER.md declares  <- identity.
#   4. A plain `kubectl` resolves to that same context       <- identity.
#   5. The cluster's API server answers.
# Checks 3 and 4 are the difference between "kubectl works" and "kubectl works on
# the right cluster". Without them 1, 2 and 5 all pass while the agent operates on
# someone else's cluster and reports the results as its own.
#
# Strictly read-only: `kubectl cluster-info` (a GET), `kubectl config` reads
# (never `--raw`, which the credential proxy denies), and local file reads.
#
# Usage:  bash cluster_preflight.sh [--json]
# Exit:   0 = PREFLIGHT OK, non-zero = PREFLIGHT FAILED (reason on stdout).

set -u

# Check 3's reader of the pinned kubeconfig, run as `python3 -I -c` with the
# path and the size cap as arguments. It answers with its exit status, and on
# success prints the `current-context` (nothing when the file names none).
#
# PyYAML's `safe_load` on the text decoded as UTF-8 with replacement, because
# that is how the kubectl shim reads the same key out of the same file
# (credential_proxy_client.kubeconfig_context and read_current_context), so the
# two take the same context from it. The sandbox image installs it for the bare python3
# (deploy/sandbox/Dockerfile, `pip install ... pyyaml`). `-I` keeps a `yaml.py`
# in the caller's working directory, or a PYTHONPATH, from standing in for it.
readonly KUBECONFIG_READ_MAX_BYTES=1048576
readonly KUBECONFIG_READ_RC_UNREADABLE=10
readonly KUBECONFIG_READ_RC_INVALID=11
readonly KUBECONFIG_READ_RC_NO_PARSER=12
# The whole preflight has to answer inside the terminal tool call that runs it,
# and no one call should outlast the broker that answers it.
#
# The Cluster Agent runs this script as `bash cluster_preflight.sh --json` with
# no `timeout` argument (agents/cluster/SOUL.md §6, agents/cluster/AGENTS.md),
# so Hermes' terminal tool bounds the run at its own default:
# `_parse_env_var("TERMINAL_TIMEOUT", "180")` in tools/terminal_tool.py at the
# Hermes tags.env pins, with nothing in this repository setting TERMINAL_TIMEOUT
# or terminal.timeout. One thing can lower it: the dispatcher gives a worker
# whose card carries max_runtime_seconds a TERMINAL_TIMEOUT of that figure less
# 30 s (hermes_cli/kanban_db_dispatch.py), and nothing in this repository sets
# max_runtime_seconds on a card. A script killed there prints no JSON, and a Cluster Agent
# with no JSON cannot block its card with a reason, which is the silent crash
# this script exists to prevent. So checks 4 and 5, the two calls that go
# through the credential proxy, share one budget under that bound: the tool's
# default less the margin the file read, the python parse and the JSON need.
# deploy/shared/terminal_env_pin.py --build-check reads the default out of the
# Hermes the image ships and fails the build if this mirror disagrees with it.
#
# Within the budget, one call is capped at what the broker can take to answer
# it: in the sandbox `kubectl` is the credential-proxy shim, and the broker
# holds a request in admission for COMMAND_SLOT_WAIT_SECONDS before refusing
# it, then bounds a one-shot read by DEFAULT_KUBECTL_TIMEOUT_SECONDS (both
# 60s, credential_proxy.py; test_cluster_preflight.py holds the mirrors below
# equal to them). The 15s cap this replaced failed a correct pin whose request
# was merely queued (#2632); a cap at the sum of the broker's two bounds plus
# a margin lets a queued call wait its turn, and past it the broker has
# answered or refused, so waiting longer only delays the JSON. Each call gets
# the smaller of that cap and what the budget has left, and a call that hits
# its cap says which bound it ran into.
#
# An instruction that passes the tool a larger `timeout` can raise the budget
# to match through CLUSTER_PREFLIGHT_BUDGET_SECONDS (today no instruction does,
# and test_cluster_preflight.py is its one user, to drive the budget's
# exhaustion quickly); anything that is not a positive integer keeps the default.
readonly TERMINAL_TOOL_TIMEOUT_SECONDS=180
readonly PREFLIGHT_BUDGET_MARGIN_SECONDS=10
readonly PREFLIGHT_BUDGET_DEFAULT_SECONDS=$((TERMINAL_TOOL_TIMEOUT_SECONDS - PREFLIGHT_BUDGET_MARGIN_SECONDS))
PREFLIGHT_BUDGET_SECONDS="${CLUSTER_PREFLIGHT_BUDGET_SECONDS:-$PREFLIGHT_BUDGET_DEFAULT_SECONDS}"
case "$PREFLIGHT_BUDGET_SECONDS" in
    ''|*[!0-9]*|0*) PREFLIGHT_BUDGET_SECONDS="$PREFLIGHT_BUDGET_DEFAULT_SECONDS" ;;
esac
readonly PREFLIGHT_BUDGET_SECONDS
readonly BROKER_ADMISSION_WAIT_SECONDS=60
readonly BROKER_KUBECTL_RUN_SECONDS=60
readonly CAP_MARGIN_SECONDS=5
readonly KUBECTL_CAP_SECONDS=$((BROKER_ADMISSION_WAIT_SECONDS + BROKER_KUBECTL_RUN_SECONDS + CAP_MARGIN_SECONDS))
# The smallest cap a call gets once the budget is spent: one second, because
# `timeout 0` runs the command uncapped. What `timeout` exits with when it fires.
readonly KUBECTL_CAP_FLOOR_SECONDS=1
readonly RC_TIMED_OUT=124
# What a shell returns for a command it cannot find or cannot execute.
readonly RC_COMMAND_NOT_FOUND=127
readonly RC_COMMAND_NOT_EXECUTABLE=126
# How much of a failing command's stderr goes into the evidence, so it stays one
# readable line on the kanban card.
readonly ERR_MAX_CHARS=500
readonly KUBECONFIG_CONTEXT_READER='
import sys
UNREADABLE, INVALID, NO_PARSER = int(sys.argv[3]), int(sys.argv[4]), int(sys.argv[5])
try:
    import yaml
except ImportError as exc:
    print(exc, file=sys.stderr)
    sys.exit(NO_PARSER)
path, limit = sys.argv[1], int(sys.argv[2])
try:
    with open(path, "rb") as stream:
        raw = stream.read(limit + 1)
except OSError as exc:
    print(exc, file=sys.stderr)
    sys.exit(UNREADABLE)
if len(raw) > limit:
    print(f"larger than {limit} bytes", file=sys.stderr)
    sys.exit(INVALID)
try:
    document = yaml.safe_load(raw.decode("utf-8", errors="replace"))
# The safe constructors raise plain exceptions, not YAMLError, for a scalar they
# cannot build: an unquoted 2001-13-45 anywhere in the file is a ValueError, and
# an explicit tag on a value that does not fit it can be a ValueError, KeyError,
# IndexError or AttributeError. Only safe_load runs in this try, so each is the
# file, not the reader.
except (yaml.YAMLError, RecursionError, ValueError, LookupError, AttributeError) as exc:
    print(f"not parseable as YAML: {exc}", file=sys.stderr)
    sys.exit(INVALID)
if not isinstance(document, dict):
    print("the top level is not a mapping", file=sys.stderr)
    sys.exit(INVALID)
context = document.get("current-context")
if context is None:
    sys.exit(0)
if not isinstance(context, str):
    print(f"current-context is a {type(context).__name__}, not a string", file=sys.stderr)
    sys.exit(INVALID)
print(context.strip())
'

JSON=0
[ "${1:-}" = "--json" ] && JSON=1

HERMES_HOME="${HERMES_HOME:-/opt/data}"
# HERMES_HOME is the profile home, where the scaffold pins kubeconfig.yaml and
# writes USER.md. On the dispatch path the worker sets it; this script runs in
# the shell sandbox, which sshd starts with the data root instead, so
# deploy/sandbox/session-command.sh narrows it back to the profile from the
# session's working directory. Fall back to the pinned kubeconfig when
# KUBECONFIG is not already exported.
#
# Remember whether the fallback was needed. The variable being absent is not a
# cosmetic difference: checks 2 and 3 read the file by path either way, but the
# skills run a plain `kubectl`, which can only see the pin through the
# environment. So an unexported KUBECONFIG means the file is fine and every
# real command still misses it — check 4 is where that is caught.
if [ -n "${KUBECONFIG:-}" ]; then
    KUBECONFIG_EXPORTED=1
else
    KUBECONFIG_EXPORTED=0
    KUBECONFIG="$HERMES_HOME/kubeconfig.yaml"
fi
USER_MD="$HERMES_HOME/USER.md"

STATUS="ok"
CHECK=""
REASON=""
REMEDIATION=""
EVIDENCE=""

# Hard wall-clock cap for anything that touches the network: a broker that never
# answers, or a black-holed API endpoint stalling a TCP connect past kubectl's own
# --request-timeout, would otherwise hang the preflight past the terminal tool's
# deadline, which looks to the dispatcher exactly like the silent crash this
# script exists to prevent. Each call is capped at the smaller of the per-call
# cap and what the shared budget has left (`call_cap`; the budget is counted
# from the shell's start by $SECONDS), so the JSON is printed before the tool's
# deadline whatever the calls do. `timeout` is coreutils and always present in
# the agent image; off-image (a developer shell, a test harness) run uncapped
# rather than failing every check with "command not found". The cap is computed
# by the caller and passed as $1, because `capped` runs inside a command
# substitution and could not report it back.
call_cap() {
    local cap=$((PREFLIGHT_BUDGET_SECONDS - SECONDS))
    [ "$cap" -gt "$KUBECTL_CAP_SECONDS" ] && cap="$KUBECTL_CAP_SECONDS"
    [ "$cap" -lt "$KUBECTL_CAP_FLOOR_SECONDS" ] && cap="$KUBECTL_CAP_FLOOR_SECONDS"
    printf '%s' "$cap"
}
if command -v timeout >/dev/null 2>&1; then
    capped() { timeout "$1" "${@:2}"; }
else
    capped() { "${@:2}"; }
fi
# The evidence for a call that hit its cap, naming the bound it ran into: the
# per-call cap, or what was left of the budget, with the tool's default named
# only when the budget is the default derived from it. $2 names the second
# half of the per-call cap: the broker's command bound for check 4, and for
# check 5, which names its own --request-timeout and so opts out of the
# broker's, the same seconds as room for that.
timed_out_text() {
    if [ "$1" -eq "$KUBECTL_CAP_SECONDS" ]; then
        printf 'timed out after %ss, the preflight'"'"'s cap on one brokered call (the credential proxy'"'"'s %ss admission wait plus %s)' \
            "$1" "$BROKER_ADMISSION_WAIT_SECONDS" "${2:-its ${BROKER_KUBECTL_RUN_SECONDS}s command bound}"
    elif [ "$PREFLIGHT_BUDGET_SECONDS" -eq "$PREFLIGHT_BUDGET_DEFAULT_SECONDS" ]; then
        printf 'timed out after %ss, what was left of the preflight'"'"'s %ss budget (the terminal tool'"'"'s %ss default less a margin)' \
            "$1" "$PREFLIGHT_BUDGET_SECONDS" "$TERMINAL_TOOL_TIMEOUT_SECONDS"
    else
        printf 'timed out after %ss, what was left of the preflight'"'"'s %ss budget (CLUSTER_PREFLIGHT_BUDGET_SECONDS)' \
            "$1" "$PREFLIGHT_BUDGET_SECONDS"
    fi
}

# The check number is reported, not merely used for ordering: two checks can share a
# remediation ("Re-scaffold the profile") while meaning different things to the caller,
# and the single-cluster inventory-audit SOP routes on which check failed.
fail() {
    STATUS="failed"
    CHECK="$1"
    REASON="$2"
    REMEDIATION="$3"
    EVIDENCE="${4:-}"
}

# Read one `- <field>: <value>` line out of USER.md, as written by
# cluster_agent_profile.create_profile. Lowercased first so a hand-edited
# `- Cluster:` still matches; GKE project/cluster/location names are themselves
# lowercase by construction, so the values survive the fold unchanged.
user_md_field() {
    tr '[:upper:]' '[:lower:]' <"$USER_MD" 2>/dev/null \
        | sed -n "s/^[[:space:]]*-[[:space:]]*$1:[[:space:]]*//p" | head -n1 | tr -d '[:space:]'
}

PROJECT=""
CLUSTER=""
LOCATION=""
EXPECTED_CONTEXT=""

# 1. Fixed cluster identity present (project/cluster/location live in USER.md).
#    All three are required: check 3 reconstructs the expected kubeconfig context
#    name from them, and a partial identity cannot be checked against anything.
if [ "$STATUS" = "ok" ]; then
    if [ ! -f "$USER_MD" ]; then
        fail "1" "This Cluster Agent has no identity file (USER.md missing at $USER_MD)." \
             "Re-scaffold the profile via the Platform Agent (cluster_agent_profile.py create)." \
             "expected identity file not found: $USER_MD"
    else
        PROJECT="$(user_md_field project)"
        CLUSTER="$(user_md_field cluster)"
        LOCATION="$(user_md_field location)"
        MISSING=""
        [ -z "$PROJECT" ] && MISSING="$MISSING project"
        [ -z "$CLUSTER" ] && MISSING="$MISSING cluster"
        [ -z "$LOCATION" ] && MISSING="$MISSING location"
        if [ -n "$MISSING" ]; then
            fail "1" "This Cluster Agent's identity file is present but incomplete (no cluster identity)." \
                 "Re-scaffold the profile so USER.md records its project/cluster/location." \
                 "USER.md at $USER_MD is missing:$MISSING"
        else
            # The name `gcloud container clusters get-credentials` gives the context
            # it writes. The operator builds KUBE_CONTEXT_NAME the same way
            # (buildCredentialProxyEnv), so both ends agree on this convention.
            EXPECTED_CONTEXT="gke_${PROJECT}_${LOCATION}_${CLUSTER}"
        fi
    fi
fi

# 2. Kubeconfig pinned and non-empty.
if [ "$STATUS" = "ok" ]; then
    if [ ! -f "$KUBECONFIG" ]; then
        fail "2" "Kubeconfig is not pinned for this cluster (no file at $KUBECONFIG)." \
             "Re-scaffold the profile; scaffolding runs 'gcloud container clusters get-credentials' and pins KUBECONFIG via <home>/.env." \
             "missing kubeconfig: $KUBECONFIG"
    elif [ ! -s "$KUBECONFIG" ]; then
        fail "2" "Kubeconfig at $KUBECONFIG is empty." \
             "Re-scaffold the profile to re-fetch cluster credentials." \
             "empty kubeconfig: $KUBECONFIG"
    fi
fi

# 2b. kubectl present. Checks 4 and 5 need it, so establish it once. Check 3
#     does not, but this stays ahead of it so a missing kubectl reports as 2b
#     whatever the pin says.
if [ "$STATUS" = "ok" ] && ! command -v kubectl >/dev/null 2>&1; then
    fail "2b" "kubectl is not available in this agent's environment." \
         "This indicates a broken image/toolset; escalate to the Platform Agent." \
         "kubectl not found on PATH"
fi

# 3. The pinned kubeconfig actually describes the cluster USER.md declares.
#
#    Checks 2 and 5 together are not enough: a kubeconfig pointing at some *other*
#    reachable cluster passes both, and the agent then does its whole investigation
#    against the wrong cluster and reports the answer with full confidence. Nothing
#    else in the pipeline compares the credentials to the identity, so this is the
#    only place a mis-targeted pin can be caught.
#
#    Read from the file itself, not through kubectl. The question is what the
#    pinned file says, independent of whatever the ambient context resolves to,
#    and check 4 is what tests the environment path. In the sandbox `kubectl` is
#    the credential-proxy shim, which sends even `config current-context` to the
#    broker as an exec request; on a busy install that request queued behind
#    fleet sweeps for longer than the then-15s cap, and a correct pin failed here.
#    The shim reads this key out of the file locally before it sends anything,
#    with the same parser as below, so the round trip added a queue and no
#    information.
#
#    Each way the read can fail gets its own report, because they call for
#    different remediations: a file the agent cannot read, a file that is not a
#    kubeconfig, an image with no parser, and a kubeconfig that names no context.
#    Only the second and the last are a broken pin that re-scaffolding fixes.
if [ "$STATUS" = "ok" ]; then
    CTX_ERR_FILE="$(mktemp 2>/dev/null || printf '%s' "${TMPDIR:-/tmp}/preflight_ctx_err.$$")"
    PINNED_CONTEXT="$(python3 -I -c "$KUBECONFIG_CONTEXT_READER" "$KUBECONFIG" \
        "$KUBECONFIG_READ_MAX_BYTES" "$KUBECONFIG_READ_RC_UNREADABLE" \
        "$KUBECONFIG_READ_RC_INVALID" "$KUBECONFIG_READ_RC_NO_PARSER" 2>"$CTX_ERR_FILE")"
    CTX_RC=$?
    CTX_ERR="$(tr '\n' ' ' <"$CTX_ERR_FILE" 2>/dev/null | sed 's/  */ /g' | cut -c1-"$ERR_MAX_CHARS")"
    rm -f "$CTX_ERR_FILE"

    if [ "$CTX_RC" -eq "$KUBECONFIG_READ_RC_UNREADABLE" ]; then
        fail "3" "This agent cannot read the pinned kubeconfig at $KUBECONFIG." \
             "Check the file's owner and mode: the agent user must be able to read it. Re-scaffold the profile if it cannot be repaired in place." \
             "reading $KUBECONFIG failed: ${CTX_ERR:-no error output}"
    elif [ "$CTX_RC" -eq "$KUBECONFIG_READ_RC_INVALID" ]; then
        fail "3" "The pinned kubeconfig at $KUBECONFIG is not a valid kubeconfig." \
             "Re-scaffold the profile to re-fetch cluster credentials." \
             "$KUBECONFIG: ${CTX_ERR:-no error output}"
    elif [ "$CTX_RC" -eq "$KUBECONFIG_READ_RC_NO_PARSER" ] \
        || [ "$CTX_RC" -eq "$RC_COMMAND_NOT_FOUND" ] \
        || [ "$CTX_RC" -eq "$RC_COMMAND_NOT_EXECUTABLE" ]; then
        fail "3" "Preflight cannot parse the kubeconfig: python3 with PyYAML is not available." \
             "This indicates a broken image/toolset; escalate to the Platform Agent." \
             "python3 -I could not load a YAML parser (exit $CTX_RC): ${CTX_ERR:-no error output}"
    elif [ "$CTX_RC" -ne 0 ]; then
        fail "3" "Could not read the pinned kubeconfig: the reader failed unexpectedly." \
             "Re-run preflight; if it fails the same way, escalate to the Platform Agent with this evidence." \
             "reading $KUBECONFIG exited $CTX_RC: ${CTX_ERR:-no error output}"
    elif [ -z "$PINNED_CONTEXT" ]; then
        fail "3" "The pinned kubeconfig does not select a cluster (no current-context)." \
             "Re-scaffold the profile to re-fetch cluster credentials." \
             "$KUBECONFIG has no current-context"
    elif [ "$PINNED_CONTEXT" != "$EXPECTED_CONTEXT" ]; then
        fail "3" "The pinned kubeconfig is for a different cluster than this agent is scoped to." \
             "Do not proceed: any finding would describe the wrong cluster. Escalate to the Platform Agent to re-pin this profile's kubeconfig for $CLUSTER." \
             "USER.md declares $EXPECTED_CONTEXT, but $KUBECONFIG selects $PINNED_CONTEXT"
    fi
fi

# 4. A plain `kubectl` — the form every skill and every ad-hoc command uses —
#    resolves to that same context.
#
#    The pin reaches a proxied kubectl through KUBECONFIG in the environment, and an
#    environment that silently fails to carry it is indistinguishable from a correct
#    one until the output is wrong: commands run against whatever context the
#    credential-proxy sidecar last had, which is the host cluster. Check 3 passes in
#    that case, because check 3 reads the file directly. This compares the two.
#
#    Deliberately no `env KUBECONFIG=...` prefix. Re-injecting the variable would
#    test the proxy transport while assuming away the very thing being checked:
#    on a profile whose .env never got the pin, the fallback above still finds the
#    file, checks 2 and 3 pass on it, and a re-injected check 4 passes too — while
#    every unprefixed `kubectl` the agent actually runs lands on the sidecar's own
#    context. This has to run exactly as a skill would.
if [ "$STATUS" = "ok" ]; then
    if [ "$KUBECONFIG_EXPORTED" = "0" ]; then
        fail "4" "This agent's environment does not export KUBECONFIG." \
             "Do not proceed: the pinned kubeconfig exists but no command will use it, so every kubectl resolves to the credential proxy's own cluster. Escalate to the Platform Agent to re-pin the profile (cluster_agent_profile.py writes KUBECONFIG into <home>/.env)." \
             "$KUBECONFIG selects $PINNED_CONTEXT, but KUBECONFIG is unset in the environment; preflight only found the file by falling back to \$HERMES_HOME/kubeconfig.yaml"
    else
        # A kubectl that failed is reported as such, not as a mismatch: an empty
        # answer from a command that never ran said "talking to another cluster"
        # about what is really the credential proxy failing to answer. With check 3
        # reading the file, this is the preflight's first call through the proxy,
        # so a saturated broker shows up here.
        EFFECTIVE_ERR_FILE="$(mktemp 2>/dev/null || printf '%s' "${TMPDIR:-/tmp}/preflight_eff_err.$$")"
        CAP_4="$(call_cap)"
        EFFECTIVE_RAW="$(capped "$CAP_4" kubectl config current-context 2>"$EFFECTIVE_ERR_FILE")"
        EFFECTIVE_RC=$?
        EFFECTIVE_CONTEXT="$(printf '%s' "$EFFECTIVE_RAW" | tr -d '[:space:]')"
        EFFECTIVE_ERR="$(tr '\n' ' ' <"$EFFECTIVE_ERR_FILE" 2>/dev/null | sed 's/  */ /g' | cut -c1-"$ERR_MAX_CHARS")"
        rm -f "$EFFECTIVE_ERR_FILE"
        [ "$EFFECTIVE_RC" -eq "$RC_TIMED_OUT" ] && EFFECTIVE_ERR="$(timed_out_text "$CAP_4")"
        if [ "$EFFECTIVE_RC" -ne 0 ]; then
            fail "4" "Could not ask kubectl which context it uses: kubectl itself failed." \
                 "This is not a bad pin; the command did not run. The pinned kubeconfig passed check 3. The credential proxy is the usual cause: check it is up and not saturated, then re-run preflight. Do not re-scaffold: that runs through the same proxy." \
                 "kubectl config current-context exited $EFFECTIVE_RC: ${EFFECTIVE_ERR:-no error output}"
        elif [ "$EFFECTIVE_CONTEXT" != "$PINNED_CONTEXT" ]; then
            fail "4" "kubectl in this environment does not use this agent's pinned kubeconfig." \
                 "Do not proceed: plain kubectl is talking to another cluster. Escalate to the Platform Agent — the agent image or its credential proxy is not carrying KUBECONFIG through to the command." \
                 "KUBECONFIG=$KUBECONFIG selects ${EFFECTIVE_CONTEXT:-<none>}, but the file itself selects $PINNED_CONTEXT"
        fi
    fi
fi

# 5. Cluster reachable (read-only GET). Captures the real API error verbatim
#    (403 access denied, cluster not found, connection timeout, ...).
if [ "$STATUS" = "ok" ]; then
    CAP_5="$(call_cap)"
    ERR="$(capped "$CAP_5" env KUBECONFIG="$KUBECONFIG" kubectl cluster-info --request-timeout=8s 2>&1 >/dev/null)"
    rc=$?
    [ "$rc" -eq "$RC_TIMED_OUT" ] && ERR="$(timed_out_text "$CAP_5" "${BROKER_KUBECTL_RUN_SECONDS}s of room for a command that names its own --request-timeout") contacting the cluster API server"
    if [ "$rc" -ne 0 ]; then
        # Collapse to a single line so it reads cleanly on the kanban card.
        ERR_ONE="$(printf '%s' "$ERR" | tr '\n' ' ' | sed 's/  */ /g' | cut -c1-500)"
        fail "5" "Cannot reach the target cluster's API server." \
             "The cluster may be deleted, unreachable, or the agent's credentials lack access. Verify the cluster exists and the agent's service account has GKE access; then re-scaffold if needed." \
             "kubectl cluster-info: $ERR_ONE"
    fi
fi

# ---- Output -----------------------------------------------------------------
json_escape() { printf '%s' "$1" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))' 2>/dev/null || printf '"%s"' "$(printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g')"; }

if [ "$JSON" = "1" ]; then
    printf '{"status": %s, "check": %s, "reason": %s, "remediation": %s, "evidence": %s}\n' \
        "$(json_escape "$STATUS")" \
        "$(json_escape "$CHECK")" \
        "$(json_escape "$REASON")" \
        "$(json_escape "$REMEDIATION")" \
        "$(json_escape "$EVIDENCE")"
else
    if [ "$STATUS" = "ok" ]; then
        echo "PREFLIGHT: OK"
    else
        echo "PREFLIGHT: FAILED"
        echo "check: $CHECK"
        echo "reason: $REASON"
        echo "remediation: $REMEDIATION"
        [ -n "$EVIDENCE" ] && echo "evidence: $EVIDENCE"
    fi
fi

[ "$STATUS" = "ok" ]
