# shellcheck shell=bash disable=SC2034
# Sourced by run.sh, which reads CHANNEL, START, CREATE_FLAGS and POOL_FLAGS and calls plant, before, break_it and after.
# 7: a fail-closed webhook whose Service has no endpoints, matching pod creation in the workload's namespace
CHANNEL=REGULAR; START=1.34; POOL_FLAGS="--num-nodes 2 --machine-type e2-standard-2"
plant(){ K label ns scen webhook=demo --overwrite >/dev/null; pause_deploy guarded 2; K apply -f - <<'Y'
apiVersion: v1
kind: Service
metadata: {name: absent-hook, namespace: scen}
spec: {ports: [{port: 443, targetPort: 8443}]}
---
apiVersion: admissionregistration.k8s.io/v1
kind: ValidatingWebhookConfiguration
metadata: {name: fail-closed-gate}
webhooks:
  - name: gate.scen.example.com
    admissionReviewVersions: [v1]
    sideEffects: None
    failurePolicy: Fail
    timeoutSeconds: 5
    namespaceSelector: {matchLabels: {webhook: demo}}
    rules: [{apiGroups: [""], apiVersions: [v1], operations: [CREATE], resources: [pods]}]
    clientConfig: {service: {name: absent-hook, namespace: scen, path: /validate}}
Y
}
before(){ ev webhook before-pods K -n scen get pods -l app=guarded -o wide; ev webhook create-by-hand K -n scen run probe --image=registry.k8s.io/pause:3.9 --restart=Never; }
break_it(){ V=$(newest_patch REGULAR 1.35); upgrade_master "$V"; upgrade_pool work-pool "$V" webhook:scen:app=guarded; }
after(){ ev webhook after-pods K -n scen get pods -l app=guarded -o wide; ev webhook deploy K -n scen get deploy guarded; ev webhook events K -n scen get events --sort-by=.lastTimestamp -o custom-columns='T:.lastTimestamp,R:.reason,O:.involvedObject.name,M:.message'; ev webhook fix K delete validatingwebhookconfiguration fail-closed-gate; sleep 45; ev webhook after-fix K -n scen get pods -l app=guarded -o wide; }
