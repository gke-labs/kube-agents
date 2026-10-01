# shellcheck shell=bash disable=SC2034
# Sourced by run.sh, which reads CHANNEL, START, CREATE_FLAGS and POOL_FLAGS and calls plant, before, break_it and after.
# 18i: scenario 18 on a T4 in us-east1-d (launch with ZONE=us-east1-d), because L4 stocked out in five zones on 2026-09-29.
# GKE picks the default driver by GKE version, and a T4 (Turing) runs both CUDA 12.4 and 13.0 when the driver
# allows, so the probes test the same thing. Everything but the accelerator and machine type comes from 18.sh.
. "$H/scenarios/18.sh"
POOL_FLAGS="--num-nodes 1 --machine-type n1-standard-4 --disk-size 200 --max-surge-upgrade 0 --max-unavailable-upgrade 1 --accelerator type=nvidia-tesla-t4,count=1,gpu-driver-version=default,gpu-sharing-strategy=time-sharing,max-shared-clients-per-gpu=2"
