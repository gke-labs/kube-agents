# shellcheck shell=bash disable=SC2034
# Sourced by run.sh, which reads CHANNEL, START, CREATE_FLAGS and POOL_FLAGS and calls plant, before, break_it and after.
# 16h: scenario 16's hazard held in its before-state: a default-deny NetworkPolicy on a cluster with no policy
# enforcement, which a dataplane change or enforcement switch would start applying. No upgrade.
. "$H/scenarios/16.sh"; CREATE_FLAGS="--cluster-ipv4-cidr=/19"
before(){ ev dataplane enforcement G container clusters describe "$CLUSTER" --zone "$ZONE" --format='value(networkPolicy,addonsConfig.networkPolicyConfig,networkConfig.datapathProvider)'
  ev dataplane policies K -n scen get networkpolicy; ev dataplane connect-unenforced connect; }
HOLD_DAYS=2
hold_exclusion(){ ev hold exclusion G container clusters update "$CLUSTER" --zone "$ZONE" --add-maintenance-exclusion-name hold-recommender --add-maintenance-exclusion-start "$(ts)" --add-maintenance-exclusion-end "$(in_days "$HOLD_DAYS")" --add-maintenance-exclusion-scope no_upgrades --quiet; }
break_it(){ hold_exclusion; note hold "hazard left planted for the Recommender's next daily refresh; no upgrade"; }
after(){ :; }
