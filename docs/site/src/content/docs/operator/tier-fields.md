---
title: Tier fields
description: Every spec and status field of the Tier custom resource, generated from the operator's Go API types.
sidebar:
  order: 8
---

<!-- BEGIN GENERATED: crd-fields-tier -->
<!-- Regenerate with: make docs-generate -- do not edit by hand. -->
<!-- prettier-ignore-start -->

Every field of the `Tier` resource's `spec` and `status`, generated from the operator's Go API types. The standard Kubernetes `apiVersion`, `kind` and `metadata` are not listed. A field whose type comes from the Kubernetes API (`corev1`, `metav1`, `networkingv1`, `resource.Quantity`) names that type and is not expanded; the Kubernetes API reference describes it. Paths read `parent.child`, with `[]` for each item of a list and `{}` for each value of a map. Each description is the opening paragraph of the field's documentation, plus its deprecation notice when it has one, and names other fields in their Go spelling (`MaxSessions` for `maxSessions`); `kubectl explain tier.spec.<field>` (`tier.status.<field>` for a row of the Status table) prints the whole text. The Constraints column lists the schema bounds on a field: allowed values, ranges, lengths and patterns. The API server also enforces CEL rules on these fields and on whole sections, which the column does not show; `kubectl get crd tiers.kubeagents.x-k8s.io -o yaml` lists them under `x-kubernetes-validations`, each with the message the API server returns when a value breaks it. For what the fields do together, read the [Tier CRD](/kube-agents/operator/tier-crd/) guide.

## Spec

| Field | Type | Required | Default | Constraints | Description |
| ----- | ---- | -------- | ------- | ----------- | ----------- |
| `mode` | `string` | yes |  | one of `interactive`, `unattended` | Mode is whether people or triggers run at this tier. interactive runs interactive profile tasks; unattended only starts profile tasks. |
| `writeMode` | `string` | no | `propose` | one of `none`, `propose` | WriteMode is what a task at this tier may do with a change it finds: propose it (the default), or explain only (none). Not enforced yet: the VCS broker reads it in a later change, and until then the install-level setting is the only one that applies. |

## Status

| Field | Type | Required | Default | Constraints | Description |
| ----- | ---- | -------- | ------- | ----------- | ----------- |
| `observedGeneration` | `integer` | no |  |  | ObservedGeneration is the .metadata.generation the status was last computed from. |
| `conditions` | `[]metav1.Condition` | no |  |  | Conditions represent the latest available observations of the tier. |

<!-- prettier-ignore-end -->
<!-- END GENERATED: crd-fields-tier -->
