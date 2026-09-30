# shellcheck shell=bash disable=SC2034
# Sourced by run.sh, which reads CHANNEL, START, CREATE_FLAGS and POOL_FLAGS and calls plant, before, break_it and after.
# 11: the zonal control plane during its own upgrade, polled every second
CHANNEL=REGULAR; START=1.34
plant(){ pause_deploy bystander 1 "      nodeSelector: {}"; }
before(){ ev zonal before K get --raw /version; }
break_it(){ V=$(newest_patch REGULAR 1.35); ( f="$EVID/zonal-api-1s.txt"; echo "# $(ts) 1 s poll start" >"$f"; while [ -n "$(ops_running)" ] || [ ! -f /tmp/upg-11-started ]; do if K --request-timeout=3s get --raw /version >/dev/null 2>&1; then echo "$(ts) up" >>"$f"; else echo "$(ts) DOWN" >>"$f"; fi; sleep 1; done ) & P=$!; ev zonal upgrade G container clusters upgrade "$CLUSTER" --master --cluster-version "$V" --zone "$ZONE" --quiet --async; touch /tmp/upg-11-started; wait_ops; sleep 5; kill $P 2>/dev/null; }
after(){ ev zonal summary sh -c "grep -c DOWN '$EVID/zonal-api-1s.txt'; grep -c ' up' '$EVID/zonal-api-1s.txt'; grep DOWN '$EVID/zonal-api-1s.txt' | head -3; grep DOWN '$EVID/zonal-api-1s.txt' | tail -1"; ev zonal version G container clusters describe "$CLUSTER" --zone "$ZONE" --format='value(currentMasterVersion)'; }
