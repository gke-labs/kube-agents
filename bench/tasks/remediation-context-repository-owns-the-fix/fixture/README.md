# Fixture for `remediation-context-repository-owns-the-fix`

Three parts. The stack `bench/tf/prebuilt/context-repository-owns-the-fix` plants parts 2 and 3
and the registration of part 1 on the install a run grades, for that run only, and removes
them at its destroy; the case therefore has no standing pool fixture and runs in the nightly
from its registration. The one standing piece is the public chart repository of part 1.

1. **The context repository** (`context-repository/`). A public Git repository whose tree is
   this directory's content on one branch: the chart under `services/payments/chart/`. The
   stack's `context_repository_*` defaults name it; `publish_context_repository.py` writes the
   tree to a repository as one commit and points the branch at it (run it again after a change
   to this directory). The broker reads a public repository with no credential, so no pool
   install needs a credential for it. For each run the stack registers it, with the branch as
   `ref`, in the install's `context_repos`:

   ```json
   [
     {
       "type": "github",
       "url": "https://github.com/<owner>/<name>",
       "ref": "<branch>"
     }
   ]
   ```

   The registration goes in the `context_repos` key of the `<agent>-gitops-state` ConfigMap,
   which the operator seeds from the PlatformAgent and keeps administrator-added entries of (the
   CR's `spec.integration.repositories[]` carries no `ref`). The ConfigMap reaches the broker and
   the shell sandbox as a mounted directory that kubelet refreshes about once a minute, so the
   stack waits until both mounts carry the entry, then the broker's cache window, before the
   prompt is sent. A private repository would have to be in the same GitHub organization as the
   GitOps repository with the install's GitHub App installed on it (the operator renders the
   read-only token policy for same-organization entries only); the fixture repository is public
   so that it is not.

2. **The GitOps repository** (`gitops-repository/`). The stack commits
   `clusters/<host cluster>/demo-shop/payments/` to the default branch of the install's
   `role: gitops` repository, with `CLUSTER` renamed to the agent host's cluster name and the
   three `CONTEXT_REPOSITORY_*` words in `source.yaml` replaced by the context repository's
   owner, name and branch. Each file carries `managed-by: kube-agents-bench` in its labels; that
   line is how the stack's teardown, a later plant and the repository reset
   (`hack/ci_reset_agent_pulls.py`, `PLANTED_STACK_FILES`) know a file is the stack's, and a
   file without it is never written over or deleted. The reset knows the pool's paths only (host
   cluster `platform-agent-host`); on a dev install the teardown is the one remover. `values.enc.yaml` is SOPS-shaped filler: no
   key decrypts it, which is the point. `legacy-env.yaml` is the decoy: a ConfigMap that carries
   the variable's name and that nothing consumes, so a text search in the writable repository
   finds a file whose edit changes nothing.

3. **The live workload** (`live/payments-api.yaml`). The stack applies it to the agent host, so
   the Deployment the request names exists with the variable, in a `demo-shop` namespace the
   stack creates and labels `managed-by=kube-agents-bench` first; a namespace of that name
   without the label stops the plant. The teardown deletes the namespace when it carries the
   label.

To plant the parts by hand on a dev install, run the stack's script directly with the
`CONTEXT_FIX_*` settings `main.tf` lists (a file holding a GitHub token with write access to the
GitOps repository, the host cluster, its location and project, the install's namespace and agent
name, this directory, and the context repository's owner, name and branch):
`python3 bench/tf/prebuilt/context-repository-owns-the-fix/plant.py plant`, and `teardown` to
remove them.

The case grades one thing the fixture plants: the path
`services/payments/chart/templates/deploy-api.yaml`, which only a read of the context
repository can produce.
