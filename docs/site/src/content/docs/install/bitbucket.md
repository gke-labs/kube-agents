---
title: Bitbucket as the GitOps forge
description: Installing with the GitOps repository on Bitbucket Cloud or Bitbucket Data Center — the token, the identity, a private CA, the flags, and where the token goes.
---

`install.sh` connects the agent's GitOps repository on GitHub by default. With
`--gitops-forge=bitbucket` the repository is on Bitbucket Cloud (`bitbucket.org`). With
`--gitops-forge=bitbucket-datacenter` it is on a Bitbucket Data Center instance that you operate.
A Bitbucket install has no GitHub App and no token minter. The agent's credential is a Bitbucket
token. You give it to the installer, and the installer stores it in a Kubernetes Secret in the
agent's namespace. Nothing else holds it.

The two products are two providers. They share no host, no token type and no API. This page
describes both, and marks each step that applies to one of them only.

## What is different from GitHub and GitLab

- **No issue tracker and no labels.** Bitbucket Cloud removed its issue tracker, and Data Center
  has none. The fleet audits keep their ledger in the install's report store and post a summary to
  chat, in place of a ledger issue. They still open their fix pull requests in the GitOps
  repository. The agent marks its pull requests with a branch prefix (`platform-agent/`) and text
  in the body, not with labels.
- **Context repositories and the fleet audits.** The agent can read a Bitbucket context
  repository: `inspect-repository` gets a snapshot of its files through the broker, with the
  read-only token if you stored one (see [Private context repositories](#private-context-repositories)).
  The fleet audits are different. Their declared-intent step reads only `github` entries in
  `context_repos` and skips every other entry with a warning, so a Bitbucket context repository
  gives an audit no declared intent in this release.

## Before you start

- **The release.** Bitbucket support is in release 0.29.0 and later. The latest release is on
  [GitHub Releases](https://github.com/gke-labs/kube-agents/releases/latest).
- **The Data Center version.** Tested with Bitbucket Data Center 8.19.
- **The repository.** Create the GitOps repository first. The installer does not create it. The
  repository needs a default branch with at least one commit: the agent opens its pull requests
  against it. Note its path:
  - Bitbucket Cloud: `workspace/repo-slug`, for example `acme/gitops`. The workspace is the
    workspace ID, in lowercase, from the repository's URL.
  - Bitbucket Data Center: `PROJECTKEY/repo-slug`, for example `INFRA/gitops`. A personal
    repository (`~user/repo`) is not supported.
- **The host.**
  - Bitbucket Cloud needs no host. It is `bitbucket.org` only.
  - Bitbucket Data Center needs a host: `--gitops-host` in a non-interactive run, or the answer to
    the hostname question in an interactive one. It is a lowercase DNS name with at least two labels,
    such as `bitbucket.example.com`. The instance must serve at the root path on port 443. An
    instance under a context path (`https://example.com/bitbucket`) or on another port (for
    example `:7990`) is not supported in this release. Put a reverse proxy in front of such an
    instance that serves it at the root on 443, as
    [A Data Center instance behind a reverse proxy](#a-data-center-instance-behind-a-reverse-proxy)
    shows. The installer refuses a host under
    `bitbucket.org`, `bitbucket.io`, `atlassian.com`, `atlassian.net`, a GitHub domain or
    `gitlab.com`.
- **The token.** Give the token the smallest reach that works. The agent pushes its own branches
  (`platform-agent/...`) and opens, comments on and declines pull requests. It never merges and
  never pushes to the default branch. A branch permission that blocks `platform-agent/*` blocks
  the agent.
  - **Bitbucket Cloud, Free or Standard plan:** an API token of an Atlassian account that exists
    only for this install. Add the account to the workspace with write access to the repository.
    Do not use a person's own account: every pull request and comment shows that account's name.
    Create the token at id.atlassian.com, under **Security**, **API tokens**, **Create API token
    with scopes**, for the app **Bitbucket**, with these scopes: `read:user:bitbucket`,
    `read:workspace:bitbucket`, `read:repository:bitbucket`, `write:repository:bitbucket`,
    `read:pullrequest:bitbucket`, `write:pullrequest:bitbucket`. The broker sends an API token as
    `Authorization: Bearer`, with no email address, which Atlassian supports and recommends. So
    you give the installer the token only, never the account's email. See
    [Using API tokens](https://support.atlassian.com/bitbucket-cloud/docs/using-api-tokens/).
  - **Bitbucket Cloud, Premium plan:** a workspace access token with **Repositories: Read,
    Write** and **Pull requests: Read, Write**. Workspace access tokens need Premium.
  - **Bitbucket Cloud, one repository:** a repository access token (in the repository's
    **Repository settings**, **Access tokens**) with the same permissions. It reaches that one
    repository only, and the broker holds one token for each host, so use it only for an install
    with one repository. See
    [Using access tokens](https://support.atlassian.com/bitbucket-cloud/docs/using-access-tokens/).
  - **Bitbucket Data Center:** a project HTTP access token on the GitOps repository's project,
    with **Repository write** permission. A repository HTTP access token works for an install with
    one repository.
- **The identity (Bitbucket Cloud access tokens only).** A workspace, project or repository access
  token cannot tell the broker which account it acts as. Without that, the broker refuses the
  calls that check who owns a pull request (for example, the agent closing its own stale pull
  request) with `FORGE_IDENTITY_UNKNOWN`. Give the account's UUID with `--gitops-identity`, as
  [Find the identity of an access token](#find-the-identity-of-an-access-token) shows. An API token
  names its own account, so it needs no identity. A Data Center project or repository token needs
  none either: the instance names the token's user in the `X-AUSERNAME` header of every answer.
- **Token expiry.** Bitbucket tokens expire. Record the expiry date, and replace the token before
  it expires, as [Rotating the token](#rotating-the-token) shows.
- **Network.** Only the credential broker pod (`<agent>-credential-proxy`) connects to Bitbucket,
  with HTTPS on port 443: to `bitbucket.org` and `api.bitbucket.org` for Cloud, and to the
  instance's host for Data Center. The agent pod never connects to Bitbucket. If you add a policy
  that limits egress in the agent's namespace, let the broker pod reach these hosts on 443. For a
  Data Center instance on a private network, the cluster needs a route and DNS to it. The
  requirements are the same as for a self-managed GitLab: see the **Network** and **An HTTPS
  proxy** items in [GitLab as the GitOps forge](/kube-agents/install/gitlab/#before-you-start),
  and [Check the network path](#check-the-network-path) below.
- **The certificate (Data Center only).** The broker must trust the instance's certificate. A
  certificate from a public CA needs nothing. If a private CA signed it, give the CA with
  `--gitops-ca-file`, as [A Data Center instance with a private CA](#a-data-center-instance-with-a-private-ca)
  shows. Bitbucket Cloud uses the public CAs, and the installer refuses `--gitops-ca-file` with
  `--gitops-forge=bitbucket`.
- **The installer host.** `install.sh` never connects to Bitbucket. The machine that runs it needs
  no route to the Bitbucket host.

### Check the token

Run these checks on a machine that has a route to the Bitbucket host. Each command sends the token
in a header from a pipe, so the token is not in a process's arguments.

Bitbucket Cloud. The repository, as the token sees it:

```bash
pass show bitbucket/agent-token | head -n1 | sed 's/^/Authorization: Bearer /' |
  curl -sS -H @- https://api.bitbucket.org/2.0/repositories/acme/gitops | jq '.full_name'
```

An API token can also name its account:

```bash
pass show bitbucket/agent-token | head -n1 | sed 's/^/Authorization: Bearer /' |
  curl -sS -H @- https://api.bitbucket.org/2.0/user | jq '{uuid, display_name}'
```

Bitbucket Data Center. The repository, and the token's user in the `X-AUSERNAME` header:

```bash
pass show bitbucket/agent-token | head -n1 | sed 's/^/Authorization: Bearer /' |
  curl -sS -D - -o /dev/null -H @- \
    https://bitbucket.example.com/rest/api/latest/projects/INFRA/repos/gitops | grep -i '^x-ausername'
```

For an instance behind a private CA, add `--cacert "$HOME/secure/internal-ca.pem"`. The answers:

- The repository's name, or an `X-AUSERNAME` line: the token works.
- `401`: Bitbucket refused the token. It is not valid, it expired, someone revoked it, or it is
  not complete (check for a missing character). On Cloud, check that the API token was made with
  **Create API token with scopes**, for the app **Bitbucket**, with the scopes above.
- `403`: the token is valid but has no permission for the repository. Check its scopes or
  permissions.
- `404`: the token cannot see the repository, or the path is wrong.

### Check the network path

For Data Center, check from inside the cluster that the broker can reach the instance. The test
runs a short-lived Pod with `curl`. Use the namespace `default` on a cluster that already exists,
before the install, or `kubeagents-system` after it:

```bash
kubectl --context <install-context> -n <namespace> run bitbucket-reach --rm -i --restart=Never \
  --image=curlimages/curl -- curl -sS https://bitbucket.example.com/rest/api/latest/application-properties
```

An answer with `version` shows that DNS, the route and TLS work. For an instance behind a private
CA, and for a cluster that cannot pull `curlimages/curl`, use the variants in
[Check the network path](/kube-agents/install/gitlab/#check-the-network-path) for GitLab, with this
URL.

## Run the installer

Interactively, give `--gitops-forge=bitbucket` or `--gitops-forge=bitbucket-datacenter`. The
interview's forge question does not offer Bitbucket, so the flag is how you choose it. The
installer then asks for what the flags do not give: the host (Data Center), the repository path
and the name of the Secret. For Data Center, it also asks for the PEM CA file: leave it empty when
a public CA signed the certificate. It asks for the token only after the cluster is up, at the health check
step. The prompt does not echo.

Non-interactively, the token comes from a file, or from a process substitution such as a password
manager. No flag or variable takes the token itself.

Bitbucket Cloud:

```bash
./install.sh -y \
  --gcp-project-id="YOUR_GCP_PROJECT_ID" \
  --gke-cluster-name="platform-agent-host" \
  --gcp-region="us-central1" \
  --model-provider="gemini" \
  --permission-set="read-only" \
  --gitops-forge=bitbucket \
  --gitops-repo=acme/gitops \
  --gitops-token-file=<(pass show bitbucket/agent-token | head -n1)
```

With a workspace or repository access token, add `--gitops-identity='{<account-uuid>}'`.

Bitbucket Data Center:

```bash
./install.sh -y \
  --gcp-project-id="YOUR_GCP_PROJECT_ID" \
  --gke-cluster-name="platform-agent-host" \
  --gcp-region="us-central1" \
  --model-provider="gemini" \
  --permission-set="read-only" \
  --gitops-forge=bitbucket-datacenter \
  --gitops-host=bitbucket.example.com \
  --gitops-repo=INFRA/gitops \
  --gitops-token-file=<(pass show bitbucket/agent-token | head -n1)
```

For an instance behind a private CA, add `--gitops-ca-file`.

| Flag                    | `install.env` key                      | Meaning                                                                                                                                                                                                                                                                                                                               |
| ----------------------- | -------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--gitops-forge`        | `GITOPS_FORGE`                         | `bitbucket` (Cloud) or `bitbucket-datacenter`.                                                                                                                                                                                                                                                                                        |
| `--gitops-host`         | `GITOPS_HOST`                          | Data Center only: the instance's hostname, with no scheme, port or path. Omit it for Cloud (`bitbucket.org` is accepted and dropped).                                                                                                                                                                                                 |
| `--gitops-repo`         | `GITOPS_REPO`                          | Cloud: `workspace/repo-slug`, or the repository's `https://bitbucket.org/...` URL. Data Center: `PROJECTKEY/repo-slug`, or its `https://<host>/scm/...` clone URL or `https://<host>/projects/...` page URL. The installer reduces a URL to `workspace/repo-slug` or `PROJECTKEY/repo-slug`, the only form the PlatformAgent accepts. |
| `--gitops-token-file`   | _never recorded_                       | A file, `/dev/stdin` (not under `curl … \| bash`) or `<(command)` that holds the token. The installer reads it once after the apply and writes it only into the Secret. Alias: `--gitlab-token-file`.                                                                                                                                 |
| _none_                  | `GITOPS_READ_TOKEN_FILE`               | Optional. A file or `<(command)` that holds a read-only token for private context repositories, read as `--gitops-token-file` is, and never recorded. Alias: `GITLAB_READ_TOKEN_FILE`.                                                                                                                                                |
| `--gitops-token-secret` | `GITLAB_TOKEN_SECRET`                  | The Secret's name, default `bitbucket-forge-token`. Alias: `--gitlab-token-secret`. `install.env` records the name under the `GITLAB_TOKEN_SECRET` key for every forge with a token.                                                                                                                                                  |
| `--gitops-identity`     | `GITOPS_IDENTITY`                      | Cloud access tokens only: the account UUID the token acts as, with or without braces. See [Find the identity of an access token](#find-the-identity-of-an-access-token).                                                                                                                                                              |
| `--gitops-ca-file`      | `GITOPS_CA_SECRET` (the Secret's name) | Data Center only: a PEM file with the CA certificates of an instance behind a private CA. The installer stores it in Secret `bitbucket-forge-ca`. Its path is not recorded.                                                                                                                                                           |

The installer refuses `--github-app-id` and `--github-pem-path` with a Bitbucket forge.

**The token source.** The installer reads the whole source and removes all whitespace from it. The
source must hold only the token. A `pass` entry with more lines needs its first line only, as in
the examples above.

**A switch of forge.** When `install.env` records another forge with a token (GitLab, or the other
Bitbucket product), the recorded token Secret holds that forge's token. The installer does not keep
it. Give the new token with `--gitops-token-file`, or name the Secret that holds it with
`--gitops-token-secret`. A non-interactive run, or a run with no terminal, stops before it changes
anything when it has neither. An interactive run asks for the new token after the apply, does not
offer to keep the old one, and stops if you enter none.

A switch also drops the recorded repository path, because the GitLab, Cloud and Data Center path
forms often fit each other: give `--gitops-repo` (an interactive run asks for it). If a switch stops
before the new token is stored (an empty answer, an interrupt or a failed apply), `install.env` keeps
the old forge in `GITOPS_SWITCH_FROM_FORGE` and `GITOPS_SWITCH_FROM_SECRET`, and the next run applies
the same rules. The installer removes the two keys after it stores the new token.

When the run keeps the recorded Secret without naming it, and gives no read token, the installer removes the Secret's `read-token` key, which holds the old
forge's read token, and says so. Give the new read token with `GITOPS_READ_TOKEN_FILE`.

**No token file.** In a non-interactive run without `--gitops-token-file`, the install completes
and prints the command that creates the Secret. Until the Secret exists, every Bitbucket call
answers `FORGE_CREDENTIAL_UNAVAILABLE`. The broker reads the Secret on each call, so you do not
have to restart anything after you create it.

### Dry run and generate-only

`--dry-run` and `--generate-only` work as for GitLab: they check that the token source is readable,
and store no token. `--generate-only` never creates the token Secret or the CA Secret. Over an
existing `install.env` it does not change the forge keys (`GITOPS_FORGE`, `GITOPS_HOST`,
`GITOPS_REPO`, `GITLAB_TOKEN_SECRET`, `GITOPS_CA_SECRET` and `GITOPS_IDENTITY`): set them yourself
before `lifecycle.sh apply`. See
[Dry run and generate-only](/kube-agents/install/gitlab/#dry-run-and-generate-only) for GitLab.

### Terraform

The `full-install` composition takes the same settings as variables: `gitops_forge`
(`bitbucket` or `bitbucket-datacenter`), `gitops_host` (Data Center), `gitops_repo_path`,
`gitops_token_secret_name` (default `bitbucket-forge-token`), `gitops_ca_secret_name` (Data
Center) and `gitops_identity`. Terraform only names the Secrets. Create them in the agent's
namespace yourself, as [Rotating the token](#rotating-the-token) and
[A Data Center instance with a private CA](#a-data-center-instance-with-a-private-ca) show.

### The chart

With Helm alone, declare the forge in `platformAgent.integration.forges` and the repository in
`platformAgent.integration.repositories`. `values.yaml` has an example for each provider:

```yaml
platformAgent:
  integration:
    forges:
      - name: bitbucket
        provider: bitbucket # or bitbucket-datacenter, with host:
        namespace: acme # the workspace ID, or the project key
        credentialsRef:
          name: bitbucket-forge-token
        # identity: "{<account-uuid>}"  # Cloud access tokens only
    repositories:
      - forge: bitbucket
        repository: gitops
        role: gitops
```

On a PlatformAgent, a Bitbucket repository is always `workspace/repo-slug` (Cloud) or
`PROJECTKEY/repo-slug` (Data Center), or the slug alone under the forge's `namespace`. A Data
Center clone URL (`/scm/...`) or page URL (`/projects/...`) is refused there. Only `install.sh`
accepts those URLs, and it reduces them to `PROJECTKEY/repo-slug` first.

The chart refuses what the operator refuses. On a Cloud forge, it refuses another host, a port, a
CA, and an identity that is not a UUID. On a Data Center forge, it refuses a missing host, a port and
plain HTTP. On any other provider, it refuses `identity`. `helm upgrade` does not update CRDs: apply
`charts/kube-agents/crds/` first.

## Check the connection

After the install, ask the broker for the token's user, from the shell sandbox:

```bash
kubectl exec -n kubeagents-system -c shell platform-agent-shell-0 -- \
  python3 /opt/data/skills/version-control/scripts/vcs.py identity \
  --repo https://bitbucket.org/acme/gitops
```

For Data Center, use `--repo https://bitbucket.example.com/INFRA/gitops`. It prints one JSON object
with the token's user: an account UUID on Cloud, a username such as
`access-token-user/1/1` for a Data Center project token. Other answers:

- `FORGE_IDENTITY_UNKNOWN`: a Cloud access token with no identity. Set it, as
  [Find the identity of an access token](#find-the-identity-of-an-access-token) shows.
- `FORGE_CREDENTIAL_UNAVAILABLE`: the Secret does not exist or has no `token` key, or the token is
  empty or has more than one line.
- `FORGE_UNAUTHENTICATED`: Bitbucket refused the token. It expired, or someone revoked it.
- `FORGE_TLS_UNTRUSTED` (Data Center): the broker does not trust the instance's certificate, or
  the CA Secret is missing.

When the broker starts, it logs the forges that it serves:

```bash
kubectl logs -n kubeagents-system deploy/platform-agent-credential-proxy \
  -c envoy-credential-proxy | grep "version control enabled"
```

## Find the identity of an access token

Do this only for a Bitbucket Cloud workspace, project or repository access token.

1. Install without `--gitops-identity`. The agent can open pull requests, but `vcs.py identity`
   answers `FORGE_IDENTITY_UNKNOWN`.
2. Let the agent open one pull request, or open one with the token yourself. Read its author's
   UUID:

   ```bash
   pass show bitbucket/agent-token | head -n1 | sed 's/^/Authorization: Bearer /' |
     curl -sS -H @- 'https://api.bitbucket.org/2.0/repositories/acme/gitops/pullrequests?state=OPEN&state=MERGED&state=DECLINED' |
     jq -r '.values[0].author.uuid'
   ```

3. Run the installer again with `--gitops-identity='{<that-uuid>}'`, or set
   `GITOPS_IDENTITY` in `install.env` and run `./upgrade.sh`. Quote the value: the braces are part
   of it.
4. Run the check in [Check the connection](#check-the-connection) again. It now prints the UUID.

## A Data Center instance with a private CA

Give the CA's PEM file with `--gitops-ca-file`. The installer stores it in Secret
`bitbucket-forge-ca`, under the key `ca.crt`, after the apply. The broker trusts it for that host
only. To replace the CA later:

```bash
kubectl create secret generic bitbucket-forge-ca -n kubeagents-system \
  --from-file=ca.crt="$HOME/secure/internal-ca.pem" --dry-run=client -o yaml |
  kubectl apply --server-side --force-conflicts -f -
```

## A Data Center instance behind a reverse proxy

If the instance serves under a context path or on a port other than 443, put a reverse proxy in
front of it that serves it at `https://<host>/` on port 443. Then:

- Set the instance's **Base URL** (Administration, **Server settings**) to the proxy's URL, for
  example `https://bitbucket.example.com`. Bitbucket builds the links in its answers from it.
- The proxy must pass git's smart-HTTP requests under `/scm/` (`info/refs`, `git-upload-pack`,
  `git-receive-pack`) with their bodies unchanged and without a short body-size limit, and it
  must pass the `Authorization` header.
- See Atlassian's guide to
  [proxying and securing Bitbucket](https://confluence.atlassian.com/bitbucketserver/proxy-and-secure-bitbucket-776640099.html).

Before the install, check git through the proxy, from a machine with a route to it. With no
token, git must answer with an authentication error, not a `404` or an HTML page:

```bash
GIT_TERMINAL_PROMPT=0 GIT_SSL_CAINFO="$HOME/secure/internal-ca.pem" \
  git ls-remote https://bitbucket.example.com/scm/INFRA/gitops.git
```

Leave out `GIT_SSL_CAINFO` when a public CA signed the certificate.

## Where the token goes

The installer pipes the token straight into `kubectl create secret … --from-file=token=…` and a
server-side `kubectl apply`. So the token never appears in a process's arguments, an exported
environment variable, the installer's output, `install.env`, `terraform.tfvars` or the Terraform
state. Terraform and the Helm release name the Secret and nothing more. The operator mounts the
Secret's `token` key into the credential broker's pod only. The agent and its shell sandbox never
see it. The broker sends the token as `Authorization: Bearer`. git gets it with the username
`x-token-auth` on Cloud, and as a bearer on Data Center.

## Rotating the token

Create the new token, put it in the Secret, and then revoke the old token. The broker reads the new
token on its next call:

```bash
kubectl create secret generic bitbucket-forge-token -n kubeagents-system \
  --from-file=token=<(pass show bitbucket/agent-token | head -n1) --dry-run=client -o yaml |
  kubectl apply --server-side --force-conflicts -f -
```

Use `--server-side`. A plain `kubectl apply` stores the whole object, token included, in the
Secret's `kubectl.kubernetes.io/last-applied-configuration` annotation.

## Private context repositories

A context repository is a repository that the agent only reads. The broker never presents the write
token for it. Without a read-only token, the broker reads a context repository with no credential,
so a private repository refuses the read.

To read a private context repository, store a read-only token in the same Secret, under the key
`read-token`. It is a second key, not a second credential for the host: the broker presents it only
when it reads a context repository. So a repository access token as the read token does not break
the one-token-per-host rule, and it needs no identity, because the broker never checks the
ownership of a read.

- Bitbucket Cloud: a repository access token on the context repository with **Repositories:
  Read** only, or an API token with `read:repository:bitbucket` only.
- Bitbucket Data Center: a project or repository HTTP access token with **Repository read**.

On a new install, set `GITOPS_READ_TOKEN_FILE` for the run. On an existing install:

```bash
kubectl create secret generic bitbucket-forge-token -n kubeagents-system \
  --from-file=read-token=<(pass show bitbucket/read-token | head -n1) --dry-run=client -o yaml |
  kubectl apply --server-side --force-conflicts \
    --field-manager=kube-agents-installer-read-token -f -
```

Keep `--field-manager`, so the apply keeps the `token` key. Then, on a new install and on an
existing one, register the repository in the
`context_repos` key of the `platform-agent-gitops-state` ConfigMap, with `"type": "bitbucket"` or
`"type": "bitbucket-datacenter"`, in the same workspace or project as the forge serves. The installer
does not register context repositories, so this step is necessary in both cases. This command
replaces the value of `context_repos`, so keep any entries that are already there:

```bash
kubectl patch configmap platform-agent-gitops-state -n kubeagents-system --type merge \
  -p '{"data":{"context_repos":"[{\"type\":\"bitbucket\",\"url\":\"https://bitbucket.org/acme/terraform\"}]"}}'
```

## Upgrade and uninstall

- `upgrade.sh` reads the forge, host, repository, identity and Secret names from `install.env`.
  It needs no token and no CA file, and it keeps the Secrets.
- The token Secret, and the CA Secret if there is one, are not part of the Helm release.
  Deleting the PlatformAgent or uninstalling the release leaves them. A full teardown that deletes
  the cluster deletes them too. On a cluster that the teardown keeps, delete them with the
  namespace, as [What a teardown leaves](/kube-agents/install/uninstall/#what-a-teardown-leaves)
  shows.
- After an uninstall, revoke the tokens in Bitbucket, the read token too if you made one.
