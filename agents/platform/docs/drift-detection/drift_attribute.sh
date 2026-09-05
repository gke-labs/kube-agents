#!/usr/bin/env bash
# drift_attribute.sh — Spike A join: attribute out-of-band changes in a namespace
# usage: drift_attribute.sh <namespace> <project> <cluster> [freshness]
set -euo pipefail
NS="$1"; PROJECT="$2"; CLUSTER="$3"; FRESH="${4:-1h}"

# GitOps/controller managers to treat as "not human". Extend as needed.
GITOPS_RE='argocd|flux|helm|kube-controller-manager|kube-scheduler|.*-controller$'

echo "### managedFields: fields owned by a NON-GitOps manager (operation=Update) ###"
for r in $(kubectl get svc,deploy,networkpolicy,rolebinding -n "$NS" -o name 2>/dev/null); do
  kubectl get "$r" -n "$NS" --show-managed-fields -o json 2>/dev/null | jq -r --arg r "$r" --arg re "$GITOPS_RE" '
    .metadata.managedFields[]?
    | select(.operation=="Update" and (.manager | test($re) | not))
    | "  \($r)  ->  owner: \(.manager) (\(.operation))  fields: \((.fieldsV1 // {}) | keys | join(","))"'
done

echo
echo "### audit log: non-system principals mutating $NS (who / verb / when) ###"
gcloud logging read "
  logName=\"projects/$PROJECT/logs/cloudaudit.googleapis.com%2Factivity\"
  AND resource.type=\"k8s_cluster\"
  AND resource.labels.cluster_name=\"$CLUSTER\"
  AND protoPayload.resourceName:\"$NS\"
  AND protoPayload.methodName=~\"create|patch|update|delete\"
  AND NOT protoPayload.authenticationInfo.principalEmail=~\"^system:\"
" --project "$PROJECT" --limit 50 --format=json --freshness="$FRESH" 2>/dev/null \
  | jq -r '.[] | "  \(.timestamp)  \(.protoPayload.authenticationInfo.principalEmail)  \(.protoPayload.methodName|sub("io.k8s.";"")|sub(".v1";""))  \(.protoPayload.resourceName)"' | sort -u
