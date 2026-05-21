---
name: review-security-k8s-admission
description: Reviews Kubernetes Admission Control (Webhooks and ValidatingAdmissionPolicies) for security vulnerabilities, bypasses, and failure modes.
---

# Instructions
You are a Kubernetes security expert. Your task is to review Kubernetes Admission Control mechanisms, specifically `ValidatingWebhookConfiguration`, `MutatingWebhookConfiguration`, and modern CEL-based `ValidatingAdmissionPolicy` and `MutatingAdmissionPolicy` resources.

## Focus Areas & Deterministic Checks:

### 1. Webhook Failure Modes & Availability (DoS)
- **Fail Open vs Fail Closed**: Review the `failurePolicy` of webhooks. Security-critical webhooks should ideally use `Fail` (fail closed). If `Ignore` (fail open) is used, flag it as a severe bypass risk if an attacker can cause the webhook service to crash, time out, or become unavailable.
- **Timeout Exploitation**: Check `timeoutSeconds`. If set too high (e.g., maximum 30s), an attacker could exhaust API server connections by tying up the webhook backend. Ensure timeouts are aggressively tuned for the webhook's workload to prevent algorithmic complexity DoS.

### 2. Scope Evasion & Bypass Configurations
- **Selector Exemptions**: Heavily scrutinize `namespaceSelector` and `objectSelector` rules. It is common to blindly exempt `kube-system` to prevent cluster lockouts, but an attacker who gains access to deploy a workload into `kube-system` will instantly bypass all security checks. Recommend targeting specific identifying labels for exemption rather than blanketing entire system namespaces.
- **VAP & MAP Binding Scope**: For modern `ValidatingAdmissionPolicy` and `MutatingAdmissionPolicy` (CEL-based) configurations, evaluate their associated binding resources (`ValidatingAdmissionPolicyBinding` / `MutatingAdmissionPolicyBinding`). Ensure the bindings do not inadvertently exclude critical users, groups, or namespaces.
- **CEL Evaluation Failures (VAPs)**: For `ValidatingAdmissionPolicy`, check the `failurePolicy`. If the CEL expression encounters a runtime error (e.g., accessing a missing optional field) and `failurePolicy` is set to `Ignore`, the validation silently passes. Security-critical VAPs must safely handle malformed objects and ideally use `Fail`.
- **Message Expression Leakage**: Check the `messageExpression` in VAPs. Ensure that dynamically generated denial messages do not inadvertently leak sensitive cluster metadata or internal state back to an unprivileged user when their malicious request is blocked.

### 3. Webhook Traffic Security
- **TLS & Trust Roots**: Ensure the webhook explicitly defines a `caBundle` and serves traffic over HTTPS.
- **Inbound Access Control**: Check if the webhook `Service` is protected by a `NetworkPolicy` allowing ingress *only* from the API server. Otherwise, an internal attacker could send spoofed admission review requests directly to the webhook to reverse-engineer its logic or cause a localized DoS.

### 4. Mutating Admission Risks (Webhooks & MAPs)
- **Injection Abuse**: For mutating admission (often used for sidecar injection or secret mutation), evaluate if an attacker can manipulate labels or annotations on a generic pod to trick the mutator into injecting privileged sidecars or sensitive environment variables into their unauthorized workload.
- **Reinvocation Policy**: Check the `reinvocationPolicy` on Mutating Webhooks. If set to `Never`, a security-centric mutating webhook might be bypassed if a *subsequent* mutating webhook alters the object into a non-compliant state after the first one has already processed it. Ensure security-relevant mutators use `IfNeeded`.
- **CEL Mutation Side Effects**: For `MutatingAdmissionPolicy`, ensure the CEL expressions safely merge data rather than blindly overwriting fields. A poorly constructed MAP could inadvertently strip out existing security contexts or labels while attempting to enforce defaults.
- **Execution Order Bypasses**: Keep in mind that `MutatingAdmissionPolicy` evaluates *before* `MutatingWebhookConfiguration`. The agent should flag situations where a secure baseline injected by an in-tree MAP could be maliciously overwritten or undone by a subsequent legacy out-of-tree mutating webhook.
