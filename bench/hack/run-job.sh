#!/usr/bin/env bash
# bench/hack/run-job.sh -- run one bench case as a Job next to the agent.
#
# bench-run (init container) sends the prompt to the agent's Service and writes
# the trajectory to a shared in-memory volume; bench-score (main container)
# grades it and prints verdict.json. Checks that read the cluster or GCP need
# credentials this Job does not have; ones that read the transcript do not.
#
# Usage:
#   docker build -t kube-agents-bench bench/
#   kind load docker-image kube-agents-bench --name kube-agents   # on kind
#   bench/hack/run-job.sh chat-routing-own-cluster-namespaces
#
# Environment:
#   CONTEXT      kubectl context, default: current
#   IMAGE        default: kube-agents-bench
#   PROJECT_ID   substituted into the case, default: kind
set -euo pipefail

TASK_ID="$1"
NAMESPACE="kubeagents-system"
IMAGE="${IMAGE:-kube-agents-bench}"
KUBECTL=(kubectl -n "${NAMESPACE}" ${CONTEXT:+--context "${CONTEXT}"})

# kubectl exec into the agent, for the worker transcripts.
"${KUBECTL[@]}" apply -f - >/dev/null <<EOF
apiVersion: v1
kind: ServiceAccount
metadata: {name: bench}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata: {name: bench}
rules:
- {apiGroups: [""], resources: [services, pods], verbs: [get, list]}
- {apiGroups: [""], resources: [pods/exec], verbs: [create]}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata: {name: bench}
roleRef: {apiGroup: rbac.authorization.k8s.io, kind: Role, name: bench}
subjects: [{kind: ServiceAccount, name: bench, namespace: ${NAMESPACE}}]
EOF

JOB=$("${KUBECTL[@]}" create -o name -f - <<EOF
apiVersion: batch/v1
kind: Job
metadata: {generateName: bench-${TASK_ID:0:40}-}
spec:
  backoffLimit: 0
  template:
    spec:
      serviceAccountName: bench
      restartPolicy: Never
      volumes: [{name: shared, emptyDir: {medium: Memory}}]
      initContainers:
      - name: run
        image: ${IMAGE}
        imagePullPolicy: IfNotPresent
        command: [bench-run, tasks/${TASK_ID}/task.yaml, /shared]
        env:
        - {name: AGENT_URL, value: "http://platform-agent.${NAMESPACE}:8642"}
        - {name: AGENT_NAMESPACE, value: ${NAMESPACE}}
        - {name: PROJECT_ID, value: "${PROJECT_ID:-kind}"}
        - name: PLATFORM_AGENT_TOKEN
          valueFrom: {secretKeyRef: {name: platform-agent-secrets, key: API_SERVER_KEY}}
        volumeMounts: [{name: shared, mountPath: /shared}]
      containers:
      - name: score
        image: ${IMAGE}
        imagePullPolicy: IfNotPresent
        command: [sh, -c, 'bench-score tasks/${TASK_ID}/task.yaml /shared; s=\$?; cat /shared/verdict.json; exit \$s']
        env: [{name: PROJECT_ID, value: "${PROJECT_ID:-kind}"}]
        volumeMounts: [{name: shared, mountPath: /shared}]
EOF
)
echo "${JOB}"

until status=$("${KUBECTL[@]}" get "${JOB}" -o jsonpath='{.status.succeeded}{.status.failed}') && [[ -n "${status}" ]]; do
  sleep 5
done
"${KUBECTL[@]}" logs "${JOB}" -c run --tail=20
"${KUBECTL[@]}" logs "${JOB}" -c score || true
[[ "$("${KUBECTL[@]}" get "${JOB}" -o jsonpath='{.status.succeeded}')" == 1 ]]
