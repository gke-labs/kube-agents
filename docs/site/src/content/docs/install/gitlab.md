---
title: GitLab as the GitOps forge
description: Installing with the GitOps repository on gitlab.com or a self-managed GitLab — the token, a private CA, the flags, and where the token goes.
---

`install.sh` connects the agent's GitOps repository on GitHub by default. With
`--gitops-forge=gitlab` the repository is a GitLab project instead, on gitlab.com or on a
self-managed instance. A GitLab install has no GitHub App and no token minter: the agent's
credential is a GitLab access token, which you hand to the installer and which it stores in
a Kubernetes Secret in the agent's namespace. Nothing else ever holds it.

## Before you start

- **The release.** GitLab support starts in release 0.9.0. An earlier release has no
  `--gitops-forge` flag. The latest release is on
  [GitHub Releases](https://github.com/gke-labs/kube-agents/releases/latest). To check a
  release, run
  `curl -fsSL https://raw.githubusercontent.com/gke-labs/kube-agents/<RELEASE_VERSION>/install.sh | bash -s -- --help | grep -- --gitops-forge`,
  or `./install.sh --help | grep -- --gitops-forge` in an unpacked release.
- **The GitLab version.** GitLab 16.11 or later. Tested with 16.11 and 19.4. On GitLab 16.x,
  use a release that includes [#2801](https://github.com/gke-labs/kube-agents/pull/2801). An
  older release cannot show the diff of a merge request on GitLab 16.x.
- **The project.** Create the GitOps project first. The installer does not create it. Note its
  full path with its nested groups, for example `platform/infra/gitops`. The project needs a
  default branch with at least one commit: the agent opens its merge requests against it.
- **The host.** gitlab.com needs no host. For a self-managed instance, the host is a lowercase
  DNS name with at least two labels, such as `gitlab.example.com` or `gitlab.corp.internal`.
  It has no scheme, port or path. The broker connects to it with HTTPS on port 443. The
  installer refuses a GitHub hostname.
- **The token.** Use a group or project access token with the `api` and `write_repository`
  scopes and the Developer role. On a GitLab tier without group or project access tokens, use
  a personal access token of a dedicated account that is a Developer member of the project.
  Do not use a person's own token. The Developer role is enough: the agent pushes its own
  branches and opens merge requests, and it never merges or pushes to a protected branch. The
  agent's branch names start with `platform-agent/`. A GitLab
  [push rule](https://docs.gitlab.com/user/project/repository/push_rules/) or a protected-branch
  wildcard that matches these names blocks the agent. Two
  limits apply:
  - The broker uses the token only for the declared repositories. Another project in the
    same group gets no write token. `inspect-repository` reads a public project with no
    token. It reads a private project only when the project is a context repository and you
    stored a read-only token, as
    [Private context repositories](#private-context-repositories) shows. A project outside
    those groups is refused. For `platform/infra/gitops`, the group is `platform/infra`.
  - The token limits what GitLab lets the broker do. Give it the smallest reach: a project
    access token on the GitOps project, or a group access token on the project's own group
    (`platform/infra`, not `platform`).
- **Token expiry.** GitLab access tokens expire, by default after at most one year. An
  administrator of a self-managed instance can change that limit. Record the expiry date.
  Replace the token before it expires, as [Rotating the token](#rotating-the-token) shows.
- **Network.** Only the credential broker pod (`<agent>-credential-proxy`) connects to GitLab,
  with HTTPS on port 443. Its shipped NetworkPolicy limits ingress only, so its egress is open,
  to private ranges too. The agent pod's egress policy excludes private ranges, but the agent
  never connects to GitLab. If you add a policy that limits egress in the agent's namespace,
  let the broker pod reach the GitLab host on 443. The broker resolves the hostname through the
  cluster DNS. For a private hostname on GKE, a Cloud DNS private zone that is attached to the
  cluster's VPC works. A name in an on-premises DNS needs a Cloud DNS
  [forwarding zone or peering](https://cloud.google.com/dns/docs/zones/zones-overview) to that
  DNS. The on-premises DNS must accept queries from Cloud DNS's source range
  `35.199.192.0/19`, with a route back over the Cloud VPN or Cloud Interconnect
  ([forwarding zones](https://cloud.google.com/dns/docs/zones/forwarding-zones)). The VPC firewall must let the cluster's node and Pod ranges reach the
  host on 443. For a public instance, the cluster also needs Cloud NAT or public IP addresses.
  A route to a private network outside the VPC (Cloud VPN or Cloud Interconnect) must carry the
  Pod range: advertise that range with
  [Cloud Router custom advertisements](https://cloud.google.com/network-connectivity/docs/router/how-to/advertising-overview),
  or masquerade it with
  [the IP masquerade agent](https://cloud.google.com/kubernetes-engine/docs/how-to/ip-masquerade-agent).
- **An HTTPS proxy.** If the cluster reaches GitLab through an HTTPS proxy, set `HTTPS_PROXY`,
  and `NO_PROXY` with the cluster's own addresses (for example `.svc,.cluster.local` and the
  Kubernetes API address), in the chart value `platformAgent.deployment.env`. On the Terraform
  install, set that value through `extra_helm_values`, as [Operator settings](/kube-agents/install/quickstart-gke/#operator-settings) shows for `install.sh`. Do not edit the
  PlatformAgent's `spec.deployment.env` directly, because a chart apply can replace it. These
  variables reach the credential broker container: its GitLab API client and the git commands that
  it runs use them.
- **The installer host.** `install.sh` never connects to GitLab. The machine that runs it needs
  no route to the GitLab host and no trust for its certificate.
- **The certificate.** On a self-managed instance, the broker must trust the certificate. A
  certificate from a public CA, or from a proxy that terminates TLS with such a certificate,
  needs nothing. A public CA cannot issue a certificate for an internal-only name such as
  `gitlab.corp.internal`, so such an instance normally has a private CA. If a private CA signed
  it, give the CA with `--gitops-ca-file`, as
  [A self-managed GitLab with a private CA](#a-self-managed-gitlab-with-a-private-ca) shows.
  Use a release whose `install.sh --help` lists `--gitops-ca-file`. The certificate must name
  the host in its subjectAltName. To see it, run this from a machine with a route to the host:

  ```bash
  openssl s_client -connect gitlab.example.com:443 -servername gitlab.example.com </dev/null |
    openssl x509 -noout -ext subjectAltName
  ```

- **`gh`.** The installer checks for the GitHub CLI on every install, and installs it when it
  is missing (a dry run stops instead). A GitLab install does not use it to connect to GitLab.

### Check the token

Run these checks on a machine that has a route to the GitLab host. It does not have to be the
installer host. Each command sends the token in a header from a pipe, so the token is not in a
process's arguments.
For an instance behind a private CA, run them on a machine that holds the CA file, and add
`--cacert "$HOME/secure/internal-ca.pem"` to each `curl` command.

The token's user, scopes and expiry date:

```bash
pass show gitlab/agent-token | head -n1 | sed 's/^/PRIVATE-TOKEN: /' |
  curl -sS -H @- https://gitlab.example.com/api/v4/personal_access_tokens/self
```

The answer has `scopes`, `user_id`, `active` and `expires_at`. This works for personal, project
and group access tokens (tested on GitLab 19.4). For the user name, use
`https://gitlab.example.com/api/v4/user` with the same pipe.

The token's access to the project. The path is URL-encoded:

```bash
pass show gitlab/agent-token | head -n1 | sed 's/^/PRIVATE-TOKEN: /' |
  curl -sS -H @- https://gitlab.example.com/api/v4/projects/platform%2Finfra%2Fgitops |
  jq 'if has("permissions") then ([.permissions.project_access.access_level, .permissions.group_access.access_level] | map(. // 0) | max) else . end'
```

- `30` or more: the token can write (Developer or higher).
- A lower number, or `0`: the token can only read, or has no membership. On a public project,
  GitLab answers the read with no membership, so a successful read alone proves nothing.
- `{"message":"404 Project Not Found"}`: the token cannot see the project, or the path is
  wrong.
- `{"message":"401 Unauthorized"}`: the token is not valid, has expired, or is revoked.

### Check the network path

The test runs a short-lived Pod with `curl` in the cluster. `<install-context>` is the kubectl
context that `gcloud container clusters get-credentials` writes for the cluster:
`gke_<project>_<location>_<cluster>`. `<namespace>` is one of these:

- `default`: on a cluster that already exists, before the install. `kubeagents-system` does
  not exist yet, and the policies of `default` can differ from those of `kubeagents-system`.
- `kubeagents-system`: on a new cluster, after the install, together with
  [Check the connection](#check-the-connection).

```bash
kubectl --context <install-context> -n <namespace> run gitlab-reach --rm -i --restart=Never \
  --image=curlimages/curl -- curl -sS https://gitlab.example.com/api/v4/version
```

An answer of `401 Unauthorized` or a version object shows that DNS, the route and TLS work. A
DNS, timeout or certificate error shows which part does not.

A cluster that pulls only from an approved registry, or a private cluster without Cloud NAT,
cannot pull `curlimages/curl`. Use any image with `curl` from a registry that the cluster can
pull from. The install's own shell sandbox image has `curl`:
`<registry-prefix>/agent-sandbox:<RELEASE_VERSION>` (the default prefix is
`ghcr.io/gke-labs/kube-agents`). Give `--command` before `curl`, so that the image's entrypoint
does not run.

For an instance behind a private CA, send the CA file to the Pod and give it to `curl`. Use the
same namespace as above:

```bash
kubectl --context <install-context> -n <namespace> run gitlab-reach --rm -i --restart=Never \
  --image=curlimages/curl --command -- \
  sh -c 'cat > /tmp/ca.pem && curl -sS --cacert /tmp/ca.pem https://gitlab.example.com/api/v4/version' \
  < "$HOME/secure/internal-ca.pem"
```

## Run the installer

Interactively, choose **GitLab** when the installer asks where the GitOps repository is. Then
give the host (press Enter for gitlab.com), the project path, and the name of the Secret to
create. The installer asks for the token only after the cluster is up, at the health check
step. The prompt does not echo.

Non-interactively, the token comes from a file, or from a process substitution such as a
password manager. No flag or variable takes the token itself:

```bash
./install.sh -y \
  --gcp-project-id="YOUR_GCP_PROJECT_ID" \
  --gke-cluster-name="platform-agent-host" \
  --gcp-region="us-central1" \
  --model-provider="gemini" \
  --permission-set="read-only" \
  --gitops-forge=gitlab \
  --gitops-host=gitlab.example.com \
  --gitops-repo=platform/infra/gitops \
  --gitlab-token-file=<(pass show gitlab/agent-token | head -n1)
```

For an instance behind a private CA, add `--gitops-ca-file`, as
[A self-managed GitLab with a private CA](#a-self-managed-gitlab-with-a-private-ca) shows.

| Flag                    | `install.env` key                      | Meaning                                                                                                                                                                                                                                |
| ----------------------- | -------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--gitops-forge`        | `GITOPS_FORGE`                         | `github` (default) or `gitlab`.                                                                                                                                                                                                        |
| `--gitops-host`         | `GITOPS_HOST`                          | The GitLab hostname, with no scheme, port or path. Omit it for gitlab.com. The installer refuses a GitHub host.                                                                                                                        |
| `--gitops-repo`         | `GITOPS_REPO`                          | The project's full path, `group/project` or `group/subgroup/project`, or its `https://` URL on that host. `--gitops-org` is not used.                                                                                                  |
| `--gitlab-token-file`   | _never recorded_                       | A file, `/dev/stdin` (not under `curl … \| bash`) or `<(command)` that holds the token. The installer checks a regular file before the apply, reads the token once after the apply, and writes it only into the Secret.                |
| _none_                  | `GITLAB_READ_TOKEN_FILE`               | Optional. A file, `/dev/stdin` or `<(command)` that holds a read-only token for private context repositories, read as `--gitlab-token-file` is, and never recorded. See [Private context repositories](#private-context-repositories). |
| `--gitlab-token-secret` | `GITLAB_TOKEN_SECRET`                  | The Secret's name, default `gitlab-forge-token`.                                                                                                                                                                                       |
| `--gitops-ca-file`      | `GITOPS_CA_SECRET` (the Secret's name) | A regular PEM file with the CA certificates of a self-managed instance behind a private CA. The installer stores it in Secret `gitlab-forge-ca`. Its path is not recorded. See below.                                                  |

The installer refuses `--github-app-id` and `--github-pem-path` with `--gitops-forge=gitlab`.

**The token source.** The installer reads the whole source and removes all whitespace from it,
newlines inside it too. The source must hold only the token. A `pass` entry with more lines (a
user name, a URL) needs its first line only, as in the example above.

**Process substitution and `pass`.** The file descriptor of a process substitution stays open
for the whole run, and the installer reads it after the apply. `pass` runs when the run starts.
Unlock `gpg-agent` first, for example with `pass show gitlab/agent-token >/dev/null`. If `pass`
cannot decrypt the entry, the source is empty. The installer then stops after the apply with
"The GitLab token source … is empty; nothing was stored." and exit code 1, and creates no token
Secret. Then create the missing Secrets yourself. Create the token Secret, as
[Rotating the token](#rotating-the-token) shows. For a private CA, check with
`kubectl get secret gitlab-forge-ca -n kubeagents-system` whether the CA Secret exists. If it
does not, create it, as [Replacing the CA](#replacing-the-ca) shows.
`/dev/stdin` works only with `./install.sh` from an unpacked release: under `curl … | bash`,
standard input is the script itself.

**No token file.** In a non-interactive run without `--gitlab-token-file`, the install
completes and prints the command that creates the Secret. Until the Secret exists, every GitLab
call answers `FORGE_CREDENTIAL_UNAVAILABLE`. The broker reads the Secret on each call, so you
do not have to restart anything after you create it.

### Dry run and generate-only

`--dry-run` and `--generate-only` check that the token source is readable. When the source is
a regular file, they also stop the run if the file is empty (whitespace only). They store no
token. `/dev/stdin`, `/dev/fd/N` and a process substitution are not read in these runs. A real
run reads the token once, after the apply, and writes it only into the Secret.

`--dry-run` and `--generate-only` check the content of a regular CA file.

`--generate-only` writes `terraform.tfvars`. It never creates the token Secret or the CA Secret:

- On a first install, it writes `install.env` with the GitLab keys (`GITOPS_FORGE`,
  `GITOPS_HOST`, `GITOPS_REPO`, `GITLAB_TOKEN_SECRET`, and `GITOPS_CA_SECRET` for a private CA).
- Over an existing `install.env`, it does not change those keys. Before you run
  `lifecycle.sh apply`, set them in `install.env` yourself. Otherwise the next `upgrade.sh`
  renders the forge that the file records, and removes the GitLab forge.

After `lifecycle.sh apply`, create both Secrets in the agent's namespace: the token Secret, as
[Rotating the token](#rotating-the-token) shows, and the CA Secret, as
[Replacing the CA](#replacing-the-ca) shows.

## Check the connection

After the install, ask the broker for the token's user, from the shell sandbox:

```bash
kubectl exec -n kubeagents-system -c shell platform-agent-shell-0 -- \
  python3 /opt/data/skills/version-control/scripts/vcs.py identity \
  --repo https://gitlab.example.com/platform/infra/gitops
```

It prints one JSON object with the token's user name. Other answers:

- `FORGE_CREDENTIAL_UNAVAILABLE`: the Secret does not exist or has no `token` key, or the token
  is empty, has more than one line, or has a character that is not printable ASCII.
- `FORGE_UNAUTHENTICATED`: GitLab refused the token. It expired, or someone revoked it.
- `FORGE_TLS_UNTRUSTED`: the broker does not trust the instance's certificate, or the CA Secret
  is missing. The message names the cause. See
  [A self-managed GitLab with a private CA](#a-self-managed-gitlab-with-a-private-ca).

The PlatformAgent status does not report these, so this check is the way to see them. When the
broker starts, it logs the forges that it serves:

```bash
kubectl logs -n kubeagents-system deploy/platform-agent-credential-proxy \
  -c envoy-credential-proxy | grep "version control enabled"
```

On a GitLab install, the line ends with `forges=github,gitlab`: the broker always serves the
GitHub forge too.

To see the forge that the install declared:

```bash
kubectl get platformagent platform-agent -n kubeagents-system \
  -o jsonpath='{.spec.integration.forges}{"\n"}{.spec.integration.repositories}{"\n"}'
```

## Where the token goes

The installer pipes the token straight into `kubectl create secret … --from-file=token=…`
and a server-side `kubectl apply`, so it never appears in a process's arguments, an exported environment variable, the
installer's output, `install.env`, `terraform.tfvars` or the Terraform state. Terraform and
the Helm release name the Secret and nothing more. The operator mounts the Secret's `token`
key into the credential broker's pod only; the agent and its shell sandbox never see it. A
read-only token for context repositories takes the same path, under the key `read-token`.

## Rotating the token

Replace the token before it expires. An expired token gives `FORGE_UNAUTHENTICATED` on every
GitLab call, and nothing else reports it. Create the new token in GitLab, put it in the Secret,
and then revoke the old token. The broker reads the new token on its next call:

```bash
kubectl create secret generic gitlab-forge-token -n kubeagents-system \
  --from-file=token=<(pass show gitlab/agent-token | head -n1) --dry-run=client -o yaml |
  kubectl apply --server-side --force-conflicts -f -
```

Use `--server-side`. A plain `kubectl apply` stores the whole object, token included, in the
Secret's `kubectl.kubernetes.io/last-applied-configuration` annotation. The broker removes
whitespace around the token when it reads it, so a trailing newline does no harm.

## Private context repositories

A context repository is a repository that the agent only reads, for example a Terraform
repository that an audit reads for declared intent. The broker never presents the write token
for it. Without a read-only token, the broker clones a context repository with no credential,
so a private project refuses the clone. The read-only token needs a release whose
`install.env.example` lists `GITLAB_READ_TOKEN_FILE`; an older operator does not mount the key.

To read a private context repository, store a read-only token in the same Secret as the write
token, under the key `read-token`. Use a project access token on the context project, or a
group access token on its group, with the Reporter role and only the `read_repository` scope.
Create it on the host of the GitOps forge: gitlab.com, or your self-managed instance. The
broker presents it only when it clones a context repository. It never presents it on a commit,
a push, an API call or a token refresh. A Secret without the key works as before.

The project must be in the `context_repos` key of the `platform-agent-gitops-state`
ConfigMap, with `"type": "gitlab"`. It must also be in a group that the forge serves: for
`platform/infra/gitops`, the group `platform/infra` or a subgroup of it. This command replaces
the value of `context_repos`, so if the key already holds entries, keep them in the list:

```bash
kubectl patch configmap platform-agent-gitops-state -n kubeagents-system --type merge \
  -p '{"data":{"context_repos":"[{\"type\":\"gitlab\",\"url\":\"https://gitlab.example.com/platform/infra/terraform\"}]"}}'
```

### On a new install

Set `GITLAB_READ_TOKEN_FILE` to a file that holds the read token, in `install.env` or in the
environment of `install.sh`. The installer reads it as it reads `--gitlab-token-file`, and does
not record the path. It writes the token into the token Secret under the key `read-token`:

```bash
GITLAB_READ_TOKEN_FILE=<(pass show gitlab/read-token | head -n1) ./install.sh -y \
  --gitops-forge=gitlab \
  --gitops-host=gitlab.example.com \
  --gitops-repo=platform/infra/gitops \
  --gitlab-token-file=<(pass show gitlab/agent-token | head -n1)
```

Add the other flags from [Run the installer](#run-the-installer). For gitlab.com, omit
`--gitops-host`. For an instance behind a private CA, add `--gitops-ca-file`: the broker
trusts the CA for the read token's clones too. Set `GITLAB_READ_TOKEN_FILE` only for the run
that stores or replaces the read token, then remove it from `install.env`: a run with it set
writes the file again, over a token you rotated since, and stops if the file is gone. A later run
without `GITLAB_READ_TOKEN_FILE` keeps the `read-token` key.

### On an existing install

Add the key to the existing Secret with a server-side apply. The command is the same for
gitlab.com and for a self-managed instance. An instance behind a private CA needs no other
step, because the CA Secret is already in place:

```bash
kubectl create secret generic gitlab-forge-token -n kubeagents-system \
  --from-file=read-token=<(pass show gitlab/read-token | head -n1) --dry-run=client -o yaml |
  kubectl apply --server-side --force-conflicts \
    --field-manager=kube-agents-installer-read-token -f -
```

Keep `--field-manager`. The apply then owns only `read-token`, so it keeps the `token` key, and
the command in [Rotating the token](#rotating-the-token) keeps the `read-token` key. Use the
same command to replace the read token. The broker checks for the file on each clone, so the
change needs no restart. kubelet can take about a minute to write the file.

To check it, open the repository from the shell sandbox:

```bash
kubectl exec -n kubeagents-system -c shell platform-agent-shell-0 -- \
  python3 /opt/data/skills/inspect-repository/scripts/inspect_repository.py open \
  --repo https://gitlab.example.com/platform/infra/terraform --depth 1
```

The answer has a `handle` and a `sha`. Close the workspace with `close --handle <handle>`. If
the clone fails, the broker's log shows whether it had a read token:

```bash
kubectl logs -n kubeagents-system deploy/platform-agent-credential-proxy \
  -c envoy-credential-proxy | grep "role=context"
```

A line that says the forge "has no usable read-only token" means that the `read-token` file is
missing, unreadable or empty, or does not hold one line of printable ASCII: check the key for a space, a
byte-order mark or a second line. A line that says the broker's config "names no readTokenPath"
means that the operator predates the read-only token: upgrade the operator.

## The `install.env` keys

The installer reads `install.env` from the first of these places:

1. the path in `KUBE_AGENTS_INSTALL_ENV`;
2. an `install.env` beside `install.sh`;
3. an `install.env` in the current directory;
4. the source directory that the install uses: the unpacked release or checkout that it runs
   from, or `$HOME/kube-agents` under `curl … | bash`.

A first install writes the file in that same place.

`install.sh` records the forge, host, project and Secret name in `install.env`. It does this
on a first install, and on a later run that goes on to the apply with a different forge, host,
project or Secret name. Then `upgrade.sh` and the Day-2 menu render what was applied. A run
that you decline, or that ends with `--generate-only`, does not change these keys in an
existing file. A switch to GitLab drops `GITOPS_ORG`, `GITHUB_APP_ID` and `GITHUB_PEM_PATH`. A
switch back to GitHub drops the GitLab keys. Once `install.env` exists, the installer ignores a
`GITOPS_FORGE`, `GITOPS_HOST`, `GITLAB_TOKEN_SECRET` or `GITOPS_CA_SECRET` exported in your
shell. Use the flags.

A re-run of `install.sh` keeps an existing Secret, unless you give it a new token file or
choose to replace the Secret at the prompt. The installer refuses an empty token (whitespace
only), and stores nothing. It checks a regular token file before the apply, also in a dry run,
so an empty file stops the run before the install changes anything. It checks `/dev/stdin`,
`/dev/fd/N` and a process substitution only after the apply, because a read can use them up.

## A self-managed GitLab with a private CA

Tested with GitLab 16.11 and 19.4.

Many self-managed instances use a TLS certificate that a private CA signed. The
agent does not trust that CA by default. Without it, each call to the instance
fails with `FORGE_TLS_UNTRUSTED`, and the detail names the host.

A private CA is for a self-managed host only. `--gitops-ca-file` needs a
`--gitops-host` that is not gitlab.com: gitlab.com presents a certificate that
the public CAs sign, and a private CA must never vouch for it.

Put the CA certificates in one PEM file: the root CA, and also the intermediate
CAs if the server does not send them in its chain. A file can hold more than one
`-----BEGIN CERTIFICATE-----` block. Use a regular file, because a CA certificate
is public. Do not use `/dev/stdin` or a process substitution for the CA: the
installer can check those sources only after the apply. If the content is bad
then, the install completes with no CA Secret, and every GitLab call answers
`FORGE_TLS_UNTRUSTED` until you create the Secret.

Before the install, check the file on your machine. For one certificate:

```bash
openssl x509 -in "$HOME/secure/internal-ca.pem" -noout -subject -enddate
```

For a file with more than one certificate:

```bash
openssl crl2pkcs7 -nocrl -certfile "$HOME/secure/internal-ca.pem" |
  openssl pkcs7 -print_certs -noout
```

Then check that the CA verifies the server's chain, from a machine with a route to the host:

```bash
openssl s_client -connect gitlab.example.com:443 -servername gitlab.example.com \
  -CAfile "$HOME/secure/internal-ca.pem" -verify_return_error </dev/null
```

The output has the line `Verify return code: 0 (ok)` when the chain is correct.

Then add this flag to the command in [Run the installer](#run-the-installer):

```bash
--gitops-ca-file="$HOME/secure/internal-ca.pem"
```

The installer then does these steps:

1. It checks the file before the apply, and in a dry run too. It refuses a file
   that holds no `-----BEGIN CERTIFICATE-----` block, and a file that holds a
   private key.
2. It reads the file again after the apply.
3. It writes the certificates into the Secret `gitlab-forge-ca` (type Opaque),
   under the key `ca.crt`, with a server-side apply, so no copy is kept in an
   annotation. A CA certificate is public, but whoever can change it chooses
   which servers the agent sends the GitLab token to. In a Secret, changing it
   needs the same rights as changing the token.
4. It sets the forge's `caBundleRef` to that Secret, and records
   `GITOPS_CA_SECRET` in `install.env`. The path of the file is not recorded.

In the interactive interview, the installer asks for the CA file after the
token Secret, for a self-managed host only. It does not ask when `install.env`
already records a CA Secret: the Secret stays as it is. To replace the CA, give
`--gitops-ca-file`, or update the Secret as shown below. A later run with
gitlab.com as the host drops the recorded Secret.

The operator mounts the Secret into the credential broker's pod only. The
broker trusts the CA for this forge's host and for no other host: github.com and
gitlab.com keep the system CAs. Both of the broker's clients use it, beside the
system CAs: the client for the GitLab API, and git for clones and pushes. git
follows no HTTP redirect from this host, so a redirect cannot carry the CA to
another host.

### Replacing the CA

Update the Secret. The broker reads the file on each call, so the change needs no
restart. kubelet can take about a minute to write a new version of the file. Use
the same command to create the Secret after `--generate-only`:

```bash
kubectl create secret generic gitlab-forge-ca -n kubeagents-system \
  --from-file=ca.crt="$HOME/secure/internal-ca.pem" --dry-run=client -o yaml |
  kubectl apply --server-side --force-conflicts -f -
```

To move the instance to a new root CA, do these steps:

1. Put the old root and the new root in the file, one PEM block after the other,
   and update the Secret. The broker trusts every certificate in the file.
2. Change the server's certificate to one that the new root signs.
3. Remove the old root from the file, and update the Secret again.

A new intermediate CA needs no change to the file when the server sends the intermediate in its
chain. Put an intermediate in the file only when the server does not send it.

Notes:

- **Strict X.509 checks are off for this host only.** Python 3.13 and later
  refuse a CA certificate without a Key Usage extension, and a server
  certificate without an Authority Key Identifier. Many private CAs issue such
  certificates. For a forge that names its own CA, the broker turns off these
  strict checks. It still verifies the certificate chain and the hostname.
  Every other host keeps the strict checks.
- **Private DNS works with no change.** A hostname in a private DNS zone that the
  cluster can resolve, for example a Cloud DNS private zone on the cluster's
  VPC, needs no other setting.
- **A missing Secret does not stop the agent.** Until the Secret exists, each
  call to the instance fails with `FORGE_TLS_UNTRUSTED`, from the API client and
  from git. The detail names what to create: "the Secret gitlab-forge-ca or its
  key ca.crt is missing". A wrong key looks the same as a missing Secret.
- **`FORGE_TLS_UNTRUSTED` names its cause.** The text says which of these it is:
  a certificate that does not chain to a trusted CA, a certificate that has
  expired or is not valid yet, a certificate for another hostname, a CA bundle
  that is not mounted, or a CA bundle that could not be loaded (not PEM, or no
  certificate in it). No retry fixes any of them. In git, a file with no
  certificate in it at all reads as a certificate that does not chain to a
  trusted CA.
- **Terraform and Helm.** Terraform takes `gitlab_ca_secret_name`. The chart
  takes `platformAgent.integration.forges[].caBundleRef.name` (and an optional
  `key`). Both only name the Secret. You create it.

## Upgrade and uninstall

- `upgrade.sh` reads the forge, host, project and Secret names from `install.env`. It needs no
  token and no CA file, and it keeps both Secrets.
- The token Secret, and the CA Secret if there is one, are not part of the Helm release.
  Deleting the PlatformAgent or uninstalling the release leaves them. A full teardown that
  deletes the cluster deletes them too. On a cluster that the teardown keeps, delete the
  namespace, or only these Secrets with the command in
  [What a teardown leaves](/kube-agents/install/uninstall/#what-a-teardown-leaves). That command reads the
  Secret names and the namespace from `install.env`.
- After an uninstall, revoke the token in GitLab, and the read token if you made one.

## Where the audits report

The fleet audits put their ledger issue and their fix merge requests in the `--gitops-repo`
project, which the install declares with `role: gitops`. They never use a context repository. If
you register a second project, for example a Terraform project as `managed`, the audits still use
the `gitops` project. [Which repository gets the ledger](/kube-agents/concepts/declarative-workflow/#which-repository-gets-the-ledger)
says how to change it, and what happens with several projects and no `role: gitops`, with no
project, or with a project that the token cannot reach.

To register a second project, for example `infra/terraform` as `managed`, set the full repository
list in the chart value `platformAgent.integration.repositories`. Helm replaces a list and does not
merge it, so the list must also hold the `--gitops-repo` project with `role: gitops`. If it does
not, the install loses that project. Use the forge name `gitlab`, which is the name that the
installer gives the forge. On an install made with `install.sh`, put the list in the
`TF_VAR_extra_helm_values` line in `install.env`, as
[Operator settings](/kube-agents/install/quickstart-gke/#operator-settings) shows, and run
`install.sh` again. If `install.env` already has that line, add the key to the same object,
because a second line replaces the first. Replace `platform/infra/gitops` with your
`--gitops-repo` project:

```bash
TF_VAR_extra_helm_values='{platformAgent={integration={repositories=[{forge="gitlab",repository="platform/infra/gitops",role="gitops"},{forge="gitlab",repository="infra/terraform",role="managed"}]}}}'
```

The broker then also serves the group of the second project. The token must reach that project
with the Developer role: a project access token reaches one project only, and a group access token
reaches its group and the subgroups of that group. For two
projects with no common parent group, use the token of a dedicated account that is a Developer
member of both. Run the access-level check in
[Check the token](#check-the-token) once for each project. Then make sure that the install has both
projects:

```bash
kubectl get platformagent platform-agent -n kubeagents-system -o jsonpath='{.spec.integration.repositories}'
```

## Limits

- The skills read GitLab entries in `managed_repos`, but only GitHub entries in `context_repos`.
  A GitLab repository that you add as a context repository is skipped with a warning, so the
  audit does not search it for declared intent. The clone itself works: with a read token,
  `inspect-repository` reads a private GitLab context repository, as
  [Private context repositories](#private-context-repositories) shows.

## What the install declares

The installer writes the forge into the `PlatformAgent` as `spec.integration.forges` and
`spec.integration.repositories`, not the `github` alias: one forge with `provider: gitlab`,
your host and the Secret as its `credentialsRef`, the CA Secret as its
`caBundleRef` when `--gitops-ca-file` gave one, and the project as the `gitops`
repository. The [PlatformAgent CRD reference](/kube-agents/operator/platformagent-crd/)
describes those fields, and a GitLab forge's limits, in full.
