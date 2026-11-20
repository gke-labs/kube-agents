#!/usr/bin/env bash
# Profile one tier of deploy/sandbox/kubectl-costs.json natively, as a Job on the tier's
# reference cluster: linux/amd64, the kubectl the sandbox image pins, nothing else running
# in the pod. Creates a namespace, a read-only ClusterRole and its binding, runs
# scripts/dev/profile_kubectl_costs.py in-cluster, writes the returned table over
# deploy/sandbox/kubectl-costs.json, and deletes everything it created.
#
# Usage: profile_kubectl_costs_job.sh <kubectl context> <tier> "<reference>" [memory]
#   memory: the Job's memory request and limit (default 4Gi; 20Gi for a fleet-tier cluster)
# The procedure around it: .agents/skills/update-kubectl-cost-table/SKILL.md.
set -euo pipefail

CONTEXT=$1
TIER=$2
REFERENCE=$3
MEMORY=${4:-4Gi}
REPO=$(cd "$(dirname "$0")/../.." && pwd)
# A suffix per run, so two runs, or a run beside a namespace still terminating, never
# share an object.
NAME="kubectl-cost-profile-$(date +%s)"
NAMESPACE="$NAME"
TABLE="$REPO/deploy/sandbox/kubectl-costs.json"
BEGIN_MARK="---BEGIN-TABLE---"
END_MARK="---END-TABLE---"
JOB_TIMEOUT_SECONDS=7200
POLL_SECONDS=15
LOG_ATTEMPTS=5
KUBECTL_VERSION=$(sed -n 's/^ARG KUBECTL_VERSION=//p' "$REPO/deploy/sandbox/Dockerfile")
KUBECTL_SHA256=$(sed -n 's/^ARG KUBECTL_SHA256_AMD64=//p' "$REPO/deploy/sandbox/Dockerfile")
PYTHON_IMAGE="python:3.13-slim"

k() { kubectl --context "$CONTEXT" "$@"; }
# A string as a YAML scalar: JSON is YAML, so operator prose never enters the manifest's grammar.
yaml_str() { python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$1"; }
# Set once the Job has succeeded and until its table is on disk: the Job's log is then the only
# copy, so the namespace that holds it is kept for the operator to read.
KEEP_NAMESPACE=0
cleanup() {
  if [ "$KEEP_NAMESPACE" = 1 ]; then
    echo "kept namespace $NAMESPACE: its Job holds the measured table; read it with" \
      "kubectl --context $CONTEXT -n $NAMESPACE logs job/$NAME, then delete the namespace" >&2
  else
    k delete namespace "$NAMESPACE" --ignore-not-found --wait=false >/dev/null 2>&1 || true
  fi
  k delete clusterrolebinding "$NAME" --ignore-not-found >/dev/null 2>&1 || true
  k delete clusterrole "$NAME" --ignore-not-found >/dev/null 2>&1 || true
}
trap cleanup EXIT

k create namespace "$NAMESPACE"
k apply -f - <<EOF
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata: {name: $NAME}
rules:
  - {apiGroups: [""], resources: [pods, nodes, events, configmaps, endpoints, services, serviceaccounts, persistentvolumeclaims, persistentvolumes, namespaces], verbs: [get, list]}
  - {apiGroups: [apps], resources: [deployments, statefulsets, daemonsets, replicasets], verbs: [get, list]}
  - {apiGroups: [batch], resources: [jobs, cronjobs], verbs: [get, list]}
  - {apiGroups: [networking.k8s.io], resources: [networkpolicies], verbs: [get, list]}
  - {apiGroups: [rbac.authorization.k8s.io], resources: [roles, rolebindings, clusterroles, clusterrolebindings], verbs: [get, list]}
  - {nonResourceURLs: ["/api", "/api/*", "/apis", "/apis/*", "/version"], verbs: [get]}
---
apiVersion: v1
kind: ServiceAccount
metadata: {name: $NAME, namespace: $NAMESPACE}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata: {name: $NAME}
roleRef: {apiGroup: rbac.authorization.k8s.io, kind: ClusterRole, name: $NAME}
subjects: [{kind: ServiceAccount, name: $NAME, namespace: $NAMESPACE}]
EOF
k -n "$NAMESPACE" create configmap "$NAME" \
  --from-file=profile_kubectl_costs.py="$REPO/scripts/dev/profile_kubectl_costs.py" \
  --from-file=kubectl-admit.py="$REPO/deploy/sandbox/kubectl-admit.py" \
  --from-file=kubectl-costs.json="$TABLE"
k apply -f - <<EOF
apiVersion: batch/v1
kind: Job
metadata: {name: $NAME, namespace: $NAMESPACE}
spec:
  backoffLimit: 0
  template:
    spec:
      serviceAccountName: $NAME
      restartPolicy: Never
      nodeSelector: {kubernetes.io/arch: amd64}
      tolerations: [{operator: Exists}]
      affinity:
        nodeAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
            nodeSelectorTerms: [{matchExpressions: [{key: type, operator: NotIn, values: [kwok]}]}]
      containers:
        - name: profile
          image: $PYTHON_IMAGE
          resources:
            requests: {cpu: "500m", memory: $MEMORY}
            limits: {memory: $MEMORY}
          volumeMounts: [{name: src, mountPath: /src}]
          env:
            - {name: PROFILE_TIER, value: $(yaml_str "$TIER")}
            - {name: PROFILE_REFERENCE, value: $(yaml_str "$REFERENCE")}
          command: ["sh", "-c"]
          args:
            - |
              set -e
              mkdir -p /repo/scripts/dev /repo/deploy/sandbox
              cp /src/profile_kubectl_costs.py /repo/scripts/dev/
              cp /src/kubectl-admit.py /src/kubectl-costs.json /repo/deploy/sandbox/
              python3 -c "import hashlib,urllib.request; d=urllib.request.urlopen('https://dl.k8s.io/release/v$KUBECTL_VERSION/bin/linux/amd64/kubectl').read(); assert hashlib.sha256(d).hexdigest()=='$KUBECTL_SHA256', 'kubectl checksum'; open('/usr/local/bin/kubectl','wb').write(d)"
              chmod 0755 /usr/local/bin/kubectl
              python3 /repo/scripts/dev/profile_kubectl_costs.py --tier "\$PROFILE_TIER" --reference "\$PROFILE_REFERENCE" \
                --kubectl /usr/local/bin/kubectl --keep-higher >&2
              echo '$BEGIN_MARK'; cat /repo/deploy/sandbox/kubectl-costs.json; echo '$END_MARK'
      volumes: [{name: src, configMap: {name: $NAME}}]
EOF
deadline=$(( $(date +%s) + JOB_TIMEOUT_SECONDS ))
while :; do
  # A failed read is "not yet": an API server blip must not end a run of half an hour.
  succeeded=$(k -n "$NAMESPACE" get "job/$NAME" -o jsonpath='{.status.succeeded}' 2>/dev/null || true)
  failed=$(k -n "$NAMESPACE" get "job/$NAME" -o jsonpath='{.status.failed}' 2>/dev/null || true)
  [ "${succeeded:-0}" -ge 1 ] && break
  if [ "${failed:-0}" -ge 1 ] || [ "$(date +%s)" -ge "$deadline" ]; then
    k -n "$NAMESPACE" logs "job/$NAME" >&2 || true
    echo "profile job did not complete" >&2
    exit 1
  fi
  sleep "$POLL_SECONDS"
done
KEEP_NAMESPACE=1
logs=""
for _ in $(seq "$LOG_ATTEMPTS"); do
  logs=$(k -n "$NAMESPACE" logs "job/$NAME" 2>/dev/null) && [ -n "$logs" ] && break
  sleep "$POLL_SECONDS"
done
[ -n "$logs" ] || { echo "could not read the Job's log" >&2; exit 1; }
printf '%s\n' "$logs" | sed -n "/^$BEGIN_MARK\$/q;p" >&2
printf '%s\n' "$logs" | sed -n "/^$BEGIN_MARK\$/,/^$END_MARK\$/p" | sed '1d;$d' >"$TABLE.new"
python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$TABLE.new"
mv "$TABLE.new" "$TABLE"
KEEP_NAMESPACE=0
echo "wrote tier $TIER into $TABLE" >&2
