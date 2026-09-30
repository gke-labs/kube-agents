# shellcheck shell=bash disable=SC2034
# Sourced by run.sh, which reads CHANNEL, START, CREATE_FLAGS and POOL_FLAGS and calls plant, before, break_it and after.
# 08h: scenario 8's hazard held in its before-state (1.32 nodes running a gitRepo volume, which 1.33 refuses)
# through the Recommender's daily refresh, with no upgrade.
. "$H/scenarios/08.sh"; CREATE_FLAGS="--cluster-ipv4-cidr=/19"
HOLD_DAYS=2
hold_exclusion(){ ev hold exclusion G container clusters update "$CLUSTER" --zone "$ZONE" --add-maintenance-exclusion-name hold-recommender --add-maintenance-exclusion-start "$(ts)" --add-maintenance-exclusion-end "$(in_days "$HOLD_DAYS")" --add-maintenance-exclusion-scope no_upgrades --quiet; }
break_it(){ hold_exclusion; note hold "hazard left planted for the Recommender's next daily refresh; no upgrade"; }
after(){ :; }
