#!/usr/bin/env bash
# bench/hack/run-job.sh -- run one bench case as a self-contained Job.
#
# The agent image runs as a native sidecar with an empty data volume. bench-run
# (init container) sends it the prompt and writes the trajectory to a shared
# in-memory volume; bench-score (main container) grades it and prints
# verdict.json. Only the model endpoint comes from outside the pod. Checks that
# read the cluster or GCP need credentials this Job does not have; ones that
# read the transcript do not.
#
# Usage:
#   docker build -t kube-agents-bench bench/
#   kind load docker-image kube-agents-bench --name kube-agents   # on kind
#   AGENT_IMAGE=kind.local/platform-agent:<tag> \
#     bench/hack/run-job.sh chat-routing-own-cluster-namespaces
#
# Environment:
#   AGENT_IMAGE  the platform-agent image, required
#   CONTEXT      kubectl context, default: current
#   NAMESPACE    default: kubeagents-system
#   IMAGE        default: kube-agents-bench
#   MODEL_URL    OpenAI-compatible endpoint serving model-default,
#                default: the install's inference gateway
#   PROJECT_ID   substituted into the case, default: kind
set -euo pipefail

TASK_ID="$1"
NAMESPACE="${NAMESPACE:-kubeagents-system}"
IMAGE="${IMAGE:-kube-agents-bench}"
MODEL_URL="${MODEL_URL:-http://inference-gateway.${NAMESPACE}.svc.cluster.local/v1}"
KUBECTL=(kubectl -n "${NAMESPACE}" ${CONTEXT:+--context "${CONTEXT}"})
# The agent's API only starts with a key of 16+ characters.
TOKEN="$(openssl rand -hex 16)"

# kubectl exec into the agent sidecar, for the worker transcripts.
"${KUBECTL[@]}" apply -f - >/dev/null <<EOF
apiVersion: v1
kind: ServiceAccount
metadata: {name: bench}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata: {name: bench}
rules:
- {apiGroups: [""], resources: [pods], verbs: [get]}
- {apiGroups: [""], resources: [pods/exec], verbs: [create]}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata: {name: bench}
roleRef: {apiGroup: rbac.authorization.k8s.io, kind: Role, name: bench}
subjects: [{kind: ServiceAccount, name: bench, namespace: ${NAMESPACE}}]
---
apiVersion: v1
kind: ConfigMap
metadata: {name: bench-agent}
data:
  config.yaml: |
    model:
      api_key: none
      api_mode: chat_completions
      base_url: ${MODEL_URL}
      default: model-default
      provider: custom
    terminal:
      backend: local
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
      volumes:
      - {name: shared, emptyDir: {medium: Memory}}
      - {name: data, emptyDir: {}}
      - {name: agent-config, configMap: {name: bench-agent}}
      initContainers:
      - name: agent
        image: ${AGENT_IMAGE}
        imagePullPolicy: IfNotPresent
        restartPolicy: Always
        env:
        - {name: API_SERVER_ENABLED, value: "true"}
        - {name: API_SERVER_HOST, value: 0.0.0.0}  # for the startup probe
        - {name: API_SERVER_KEY, value: ${TOKEN}}
        - {name: API_SERVER_MODEL_NAME, value: model-default}
        - {name: HERMES_MANAGED_DIR, value: /etc/hermes}
        - {name: OTEL_SDK_DISABLED, value: "true"}
        startupProbe:
          tcpSocket: {port: 8642}
          periodSeconds: 5
          failureThreshold: 60
        volumeMounts:
        - {name: data, mountPath: /opt/data}
        - {name: agent-config, mountPath: /etc/hermes}
      - name: run
        image: ${IMAGE}
        imagePullPolicy: IfNotPresent
        command: [bench-run, tasks/${TASK_ID}/task.yaml, /shared]
        env:
        - {name: AGENT_URL, value: "http://127.0.0.1:8642"}
        - name: AGENT_POD
          valueFrom: {fieldRef: {fieldPath: metadata.name}}
        - {name: AGENT_CONTAINER, value: agent}
        - {name: AGENT_NAMESPACE, value: ${NAMESPACE}}
        - {name: PROJECT_ID, value: "${PROJECT_ID:-kind}"}
        - {name: PLATFORM_AGENT_TOKEN, value: ${TOKEN}}
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
