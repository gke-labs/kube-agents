# Fixture for `remediation-context-repository-owns-the-fix`

Three parts, planted once per install. No pool install carries them yet, so the case is a
`FIXTURE_NOT_READY` entry in `scripts/validate_bench_cases.py` and runs only on a dev install
that holds all three.

1. **The context repository** (`context-repository/`). A public Git repository, or a private
   one in the same GitHub organization as the GitOps repository with the install's GitHub App
   installed on it (the operator renders the read-only token policy for same-organization
   entries only), whose tree is this directory's content at a pinned ref. Register it in the
   install's `context_repos` with that ref:

   ```json
   [
     {
       "type": "github",
       "url": "https://github.com/<owner>/<name>",
       "ref": "<ref>"
     }
   ]
   ```

   On a chart install that is `spec.integration.repositories[]` with `role: context`; on a
   hand-managed state ConfigMap it is the `context_repos` key of `<agent>-gitops-state`.

2. **The GitOps repository** (`gitops-repository/`). Commit
   `clusters/<cluster>/demo-shop/payments/` to the default branch of the install's `role: gitops`
   repository, with `CLUSTER` renamed to the agent host's cluster name and the three
   `CONTEXT_REPOSITORY_*` words in `source.yaml` replaced by the context repository's owner, name
   and ref. `values.enc.yaml` is SOPS-shaped filler: no key decrypts it, which is the point.
   `legacy-env.yaml` is the decoy: a ConfigMap that carries the variable's name and that nothing
   consumes, so a text search in the writable repository finds a file whose edit changes nothing.

3. **The live workload** (`live/payments-api.yaml`). `kubectl apply -f` it to the agent host
   once, so the Deployment the request names exists with the variable. Delete the `demo-shop`
   namespace to remove it.

The case grades one thing the fixture plants: the path
`services/payments/chart/templates/deploy-api.yaml`, which only a read of the context
repository can produce.
