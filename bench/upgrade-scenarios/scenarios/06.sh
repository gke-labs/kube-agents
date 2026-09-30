# shellcheck shell=bash disable=SC2034
# Sourced by run.sh, which reads CHANNEL, START, CREATE_FLAGS and POOL_FLAGS and calls plant, before, break_it and after.
# 6: a caller of an API the next minor removes (flowcontrol/v1beta3 on 1.31), broken by the control-plane move to 1.32
CHANNEL=EXTENDED; START=1.31
plant(){ K apply -f "$H/manifests/deprecated-api-caller.yaml" >/dev/null 2>&1 || { K create ns kubeagents-system --dry-run=client -o yaml | K apply -f - >/dev/null; K apply -f "$H/manifests/deprecated-api-caller.yaml"; }; K -n kubeagents-system delete job first-run --ignore-not-found >/dev/null; K -n kubeagents-system create job --from=cronjob/legacy-flowcontrol-tuner first-run >/dev/null; sleep 30; }
before(){ ev removed-api before K -n kubeagents-system logs job/first-run; ev removed-api audit G logging read "resource.type=k8s_cluster AND resource.labels.cluster_name=$CLUSTER AND labels.\"k8s.io/removed-release\"=\"1.32\"" --freshness 1h --limit 2 --format='value(timestamp,protoPayload.methodName,protoPayload.requestMetadata.callerSuppliedUserAgent)'; }
break_it(){ V=$(newest_patch EXTENDED 1.32); upgrade_master "$V"; }
after(){ K -n kubeagents-system create job --from=cronjob/legacy-flowcontrol-tuner after-upgrade >/dev/null; sleep 40; ev removed-api after K -n kubeagents-system logs job/after-upgrade; ev removed-api v1beta3-discovery K get --raw /apis/flowcontrol.apiserver.k8s.io/v1beta3; ev removed-api object-via-v1 K get flowschema.v1.flowcontrol.apiserver.k8s.io legacy-batch-lane -o jsonpath='{.metadata.name}{"\n"}'; }
