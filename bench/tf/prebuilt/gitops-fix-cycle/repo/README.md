# Platform manifests

Kubernetes manifests for the platform team's services, one directory per
service group under `tasks/`. Argo CD syncs each directory to the cluster it
belongs to. Changes reach a synced branch through a pull request; the
`GitOps run-branch check` workflow validates the change and merges it when it
passes.
