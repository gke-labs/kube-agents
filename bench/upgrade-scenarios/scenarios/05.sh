# shellcheck shell=bash disable=SC2034
# Sourced by run.sh, which reads CHANNEL, START, CREATE_FLAGS and POOL_FLAGS and calls plant, before, break_it and after.
# 5: a maintenance window and an exclusion; the exclusion half is measurable (a manual upgrade ignores it), the window half is observe-only
CHANNEL=REGULAR; START=1.34; POOL_FLAGS="--num-nodes 1 --machine-type e2-standard-2"
EXCLUSION_DAYS=3
CREATE_FLAGS="--maintenance-window-start 2000-01-01T03:00:00Z --maintenance-window-end 2000-01-01T07:00:00Z --maintenance-window-recurrence FREQ=DAILY"
plant(){ pause_deploy quiet 1; has_exclusion hold-minor && return; END=$(in_days "$EXCLUSION_DAYS"); ev window exclusion G container clusters update "$CLUSTER" --zone "$ZONE" --add-maintenance-exclusion-name hold-minor --add-maintenance-exclusion-start "$(ts)" --add-maintenance-exclusion-end "$END" --add-maintenance-exclusion-scope no_minor_upgrades --quiet; }
before(){ ev window policy G container clusters describe "$CLUSTER" --zone "$ZONE" --format='yaml(maintenancePolicy)'; }
break_it(){ V=$(newest_patch REGULAR 1.35); note window "manual master upgrade to $V outside the 03:00-07:00Z window and inside a NO_MINOR_UPGRADES exclusion"; upgrade_master "$V"; }
after(){ ev window after G container clusters describe "$CLUSTER" --zone "$ZONE" --format='value(currentMasterVersion,maintenancePolicy.window.maintenanceExclusions)'; note window "the window half cannot be forced: GKE decides when an automatic upgrade starts; observe-only"; }
