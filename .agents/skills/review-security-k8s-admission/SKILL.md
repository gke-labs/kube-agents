---
name: review-security-k8s-admission
description: Reviews Kubernetes Admission Control (Webhooks, VAP/MAP) for vulnerabilities.
---
# Task
Review Kubernetes Admission Control (`ValidatingWebhookConfiguration`, `MutatingWebhookConfiguration`, `ValidatingAdmissionPolicy`, `MutatingAdmissionPolicy`).

# Checks
## 1. Webhook Failure Modes & DoS
- **Fail Closed**: Flag `failurePolicy: Ignore` on security webhooks as a bypass risk (DoS allows bypass). Prefer `Fail`.
- **Timeouts**: Flag high `timeoutSeconds` (e.g. 30s) as API connection exhaustion risks.

## 2. Scope Evasion
- **Exemptions**: Scrutinize `namespaceSelector`/`objectSelector`. Flag blanket `kube-system` exemptions; recommend specific label targeting.
- **Binding Scope**: Ensure VAP/MAP bindings don't inadvertently exclude critical users/namespaces.
- **CEL Failures**: Flag VAP `failurePolicy: Ignore` for runtime errors. Prefer `Fail`.
- **Message Leakage**: Ensure VAP `messageExpression` hides sensitive cluster metadata.

## 3. Traffic Security
- **TLS**: Require `caBundle` and HTTPS.
- **Ingress**: Require `NetworkPolicy` restricting webhook ingress solely to the API server.

## 4. Mutating Risks
- **Injection Abuse**: Flag if label/annotation manipulation allows unprivileged pods to inject privileged sidecars/env vars.
- **Reinvocation**: Review the inent of the webhook and identify if `reinvocationPolicy: IfNeeded` should be required. This ensures, if later webhooks make mutations that earlier webhooks have an opinion on, that the earlier webhooks are run again to ensure the same policies are applied to those later mutations. In general, if certain mutations are always expected, a validating webhook should also be used to confirm the mutation was successful. Validation always runs after mutation. 
- **CEL Side Effects**: Ensure MAP safely merges data without stripping security contexts.
- **Execution Order**: Flag if legacy mutating webhooks can overwrite secure baselines injected by earlier MAPs.
