# shellcheck shell=bash disable=SC2034
# Sourced by run.sh, which reads CHANNEL, START, CREATE_FLAGS and POOL_FLAGS and calls plant, before, break_it and after.
# 20d: scenario 20 re-run with the node service account granted pull and the cache control, in us-central1-c.
. "$H/scenarios/20.sh"
