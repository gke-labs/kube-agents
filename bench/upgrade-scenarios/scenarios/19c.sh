# shellcheck shell=bash disable=SC2034
# Sourced by run.sh, which reads CHANNEL, START, CREATE_FLAGS and POOL_FLAGS and calls plant, before, break_it and after.
# 19c: scenario 19 re-run with the disable guard, in us-central1-c (launch with ZONE=us-central1-c).
. "$H/scenarios/19.sh"
