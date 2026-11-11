# The sized test fleet

## High level summary

This stack builds the sized test fleet: GKE clusters with real nodes, sized S, M or L. The design is [`sized-test-fleet.md`](../../../docs/designs/sized-test-fleet.md). The sizes are in [`fleet-sizes.yaml`](../../scale/fleet-sizes.yaml). Size M has 8 clusters and 250 nodes. S halves each count, and L doubles each count. A run builds the fleet, uses it, and deletes it; the fleet does not stand between runs.

## What the stack holds

For one size:

- a network of its own, `sized-fleet-<size>`, with one subnet a cluster, and a router with Cloud NAT, so the nodes need no external address;
- the clusters, `sized-<size>-01` and on, zonal, with private nodes and a public control-plane endpoint;
- one node pool a cluster, of `n1-standard-1` nodes, so the count of node pools is the count of clusters.

Every resource carries `managed-by=kube-agents-sized-fleet`, apart from the bench clusters' and the seeded fleet's labels, and `kube-agents-scale-size` and `kube-agents-scale-run`, by which a run's cost record finds its resources. The workloads are not in the stack: `hack/sized_fleet.py` applies them with `kubectl`, because they number in the thousands.

## Running it

`hack/sized_fleet.py` is the stack's only writer. Run it from a `main` checkout, never `tofu apply` from a branch:

```bash
export SCALE_PROJECT_ID=<the scale project>
python3 hack/sized_fleet.py plan   --size M   # what `up` would change
python3 hack/sized_fleet.py up     --size M   # the clusters, the workloads, and a wait for them
python3 hack/sized_fleet.py verify --size M   # the designed state and the churn; exit 1 when unsound
python3 hack/sized_fleet.py down   --size M   # delete it all
```

Each size is its own state, in the project's `<project>-tf-state` bucket under the prefix `sized-fleet-<size>`, so `down` from any checkout deletes a fleet a crashed run left. `up` writes the clusters' credentials to a private kubeconfig in the temp directory (`--kubeconfig` moves it).

## One-time setup in a project

The nodes run as a service account created once, outside the stack, so the identity that applies the stack needs no right to create service accounts. Its roles are the node-agent minimum, as the seeded fleet's nodes have:

```bash
P=<the scale project>
gcloud iam service-accounts create sized-fleet-nodes --project "$P" \
  --display-name="Node service account for the sized test fleet"
for role in roles/logging.logWriter roles/monitoring.metricWriter roles/monitoring.viewer \
            roles/stackdriver.resourceMetadata.writer roles/artifactregistry.reader; do
  gcloud projects add-iam-policy-binding "$P" --condition=None \
    --member="serviceAccount:sized-fleet-nodes@$P.iam.gserviceaccount.com" --role="$role"
done
```

The state bucket, `<project>-tf-state`, is the one the seeded fleet uses in the same project.

Local validation without credentials: `tofu init -backend=false && tofu validate`. The providers are the ones `.terraform.lock.hcl` pins; to move them, run `tofu providers lock -platform=linux_amd64 -platform=darwin_arm64 -platform=darwin_amd64` here and commit the result.
