# ComputeFirewall

`apiVersion: compute.cnrm.cloud.google.com/v1beta1`. Project comes from the
`cnrm.cloud.google.com/project-id` annotation (resource or namespace); there
is no `projectRef`. Immutable list from the reference page as published in
September 2026; `kubectl explain` on the customer's CRDs decides.

## Fields

- **Required:** `spec.networkRef` — `name:` when the network is a
  `ComputeNetwork` object in the repository, otherwise
  `external: projects/<project>/global/networks/<network>`.
- **Immutable (create-only):** `direction` (`INGRESS`, the default, or
  `EGRESS`) and `resourceID`. `networkRef` carries no `Immutable.` marker on
  the ref fields, but a rule cannot move between networks; treat it as
  create-only.
- **Mutable:** `priority` (0–65535, default 1000, lower wins; a deny beats an
  allow at equal priority), `allow[]` and `deny[]` (`protocol` required per
  entry; `ports` only for `tcp`/`udp`, as strings, `"443"` or
  `"8000-8080"`; no `ports` means every port), `sourceRanges`,
  `destinationRanges`, `sourceTags`, `targetTags`,
  `sourceServiceAccounts[].external`, `targetServiceAccounts[].external`,
  `disabled`, `logConfig.metadata` (`INCLUDE_ALL_METADATA` or
  `EXCLUDE_ALL_METADATA`; presence of `logConfig` turns logging on),
  `description`. `enableLogging` is deprecated; use `logConfig`.

## Authoring rules

- An `INGRESS` rule needs one of `sourceRanges`, `sourceTags`, or
  `sourceServiceAccounts`. Tags and service accounts are exclusive on each
  side of the rule.
- No `targetTags` and no `targetServiceAccounts` means every instance on
  the network. State a target unless the user asked for the whole network,
  and say so in the PR body when they did.
- `0.0.0.0/0` as a source only when the request says so, and the PR body
  names it.
- Leave GKE's own rules alone: names starting `gke-<cluster>-` are created
  and reconciled by GKE, and acquiring one hands two controllers the same
  resource.
- Prefer a new, narrowly scoped rule over widening an existing one; the
  diff is smaller to review and the rollback is a delete of one manifest by
  a human.

## `describe` output to spec

From `gcloud compute firewall-rules describe ... --format=json`:

| `describe` field                                | spec path                                       | Note                                                  |
| ----------------------------------------------- | ----------------------------------------------- | ----------------------------------------------------- |
| `name`                                          | `metadata.name`, `spec.resourceID`              |                                                       |
| `network` (a URL ending `/global/networks/<n>`) | `spec.networkRef.external`                      | rewrite as `projects/<project>/global/networks/<n>`   |
| `direction`                                     | `spec.direction`                                | immutable                                             |
| `priority`                                      | `spec.priority`                                 |                                                       |
| `allowed[].IPProtocol`, `allowed[].ports`       | `spec.allow[].protocol`, `spec.allow[].ports`   |                                                       |
| `denied[].IPProtocol`, `denied[].ports`         | `spec.deny[].protocol`, `spec.deny[].ports`     |                                                       |
| `sourceRanges`, `destinationRanges`             | `spec.sourceRanges`, `spec.destinationRanges`   |                                                       |
| `sourceTags`, `targetTags`                      | `spec.sourceTags`, `spec.targetTags`            |                                                       |
| `sourceServiceAccounts`, `targetServiceAccounts` | `spec.sourceServiceAccounts[].external`, `spec.targetServiceAccounts[].external` | the account email |
| `disabled`                                      | `spec.disabled`                                 |                                                       |
| `logConfig.enable`, `logConfig.metadata`        | `spec.logConfig.metadata`                       | state `logConfig` only when `enable` is true          |
| `description`                                   | `spec.description`                              |                                                       |
| `id`, `selfLink`, `creationTimestamp`, `kind`   | not spec                                        |                                                       |

## Create: allow HTTPS from the corporate range to tagged instances

```yaml
apiVersion: compute.cnrm.cloud.google.com/v1beta1
kind: ComputeFirewall
metadata:
  name: allow-corp-https
  namespace: infra-prod
  annotations:
    cnrm.cloud.google.com/deletion-policy: abandon
    cnrm.cloud.google.com/state-into-spec: absent
spec:
  networkRef:
    external: projects/acme-prod/global/networks/prod-vpc
  direction: INGRESS
  priority: 1000
  sourceRanges:
    - 203.0.113.0/24
  targetTags:
    - web
  allow:
    - protocol: tcp
      ports:
        - "443"
  logConfig:
    metadata: EXCLUDE_ALL_METADATA
```

## Acquire: narrow an existing rule's source range

`describe` reported `direction: INGRESS`, `network: .../global/networks/prod-vpc`,
`sourceRanges: ["0.0.0.0/0"]`, `targetTags: ["ssh-bastion"]`,
`allowed: [{IPProtocol: tcp, ports: ["22"]}]`.

```yaml
apiVersion: compute.cnrm.cloud.google.com/v1beta1
kind: ComputeFirewall
metadata:
  name: allow-ssh-bastion
  namespace: infra-prod
  annotations:
    cnrm.cloud.google.com/deletion-policy: abandon
    cnrm.cloud.google.com/state-into-spec: absent
spec:
  resourceID: allow-ssh-bastion
  networkRef:
    external: projects/acme-prod/global/networks/prod-vpc
  direction: INGRESS
  sourceRanges:
    - 198.51.100.0/24
  targetTags:
    - ssh-bastion
  allow:
    - protocol: tcp
      ports:
        - "22"
```

`allow` and `targetTags` are restated even though they do not change: they
are list fields, and stating them keeps the rule's intent readable in one
file. `priority` is omitted and stays externally managed.
