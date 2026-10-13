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

- **The project.** Create the GitOps project first; the installer does not create it. Note
  its full path, nested groups included: `platform/infra/gitops`.
- **The token.** A group or project access token with the `api` and `write_repository`
  scopes and the Developer role. On a GitLab tier without group or project access tokens,
  use a personal access token of a dedicated account that is a Developer member of the
  project, never a person's own. The agent can reach every project in the GitOps project's
  group that the token can reach, so a group token's group is the boundary you are setting.
- **Egress.** The cluster must reach the GitLab host over HTTPS. A self-managed instance on
  a private network needs a route from the cluster.

## Run the installer

Interactively, choose **GitLab** when the installer asks where the GitOps repository lives,
then give the host (enter for gitlab.com), the project path, and the name of the Secret to
create. The installer asks for the token itself only after the cluster is up, at the health
check step. The prompt does not echo.

Non-interactively, the token comes from a file you name, or anything readable that a
shell can name, such as a password manager through process substitution:
`--gitlab-token-file=<(pass show gitlab/agent-token)`. There is no flag or variable that
takes the token's value:

```bash
./install.sh -y \
  --gitops-forge=gitlab \
  --gitops-host=gitlab.example.com \
  --gitops-repo=platform/infra/gitops \
  --gitlab-token-file="$HOME/secure/gitlab-token"
```

| Flag                    | `install.env` key                      | Meaning                                                                                                                                                         |
| ----------------------- | -------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--gitops-forge`        | `GITOPS_FORGE`                         | `github` (default) or `gitlab`.                                                                                                                                 |
| `--gitops-host`         | `GITOPS_HOST`                          | The GitLab hostname, with no scheme, port or path. Omit for gitlab.com. A GitHub host is refused.                                                               |
| `--gitops-repo`         | `GITOPS_REPO`                          | The project's full path, `group/project` or `group/subgroup/project`, or its `https://` URL on that host. `--gitops-org` is not used.                           |
| `--gitlab-token-file`   | _never recorded_                       | A file (or `/dev/stdin`, or `<(command)`) holding the token. Read once, piped into the Secret, and neither its path nor its contents is written anywhere else.  |
| `--gitlab-token-secret` | `GITLAB_TOKEN_SECRET`                  | The Secret's name, default `gitlab-forge-token`.                                                                                                                |
| `--gitops-ca-file`      | `GITOPS_CA_SECRET` (the Secret's name) | A PEM file with the private CA that signed a self-managed instance's certificate. Read once into Secret `gitlab-forge-ca`. Its path is not recorded. See below. |

`--github-app-id` and `--github-pem-path` are refused with `--gitops-forge=gitlab`.

Without a token file in a non-interactive run, the install completes and prints the
command that creates the Secret. Until it exists the agent answers every GitLab call with
`FORGE_CREDENTIAL_UNAVAILABLE`; it reads the Secret on each call, so creating it later needs
no restart.

## Where the token goes

The installer pipes the token straight into `kubectl create secret … --from-file=token=…`
and a server-side `kubectl apply`, so it never appears in a process's arguments, an exported environment variable, the
installer's output, `install.env`, `terraform.tfvars` or the Terraform state. Terraform and
the Helm release name the Secret and nothing more. The operator mounts the Secret's `token`
key into the credential broker's pod only; the agent and its shell sandbox never see it.

## Rotating the token

Replace the Secret's value; the broker picks the new token up on its next call:

```bash
kubectl create secret generic gitlab-forge-token -n <namespace> \
  --from-file=token="$HOME/secure/gitlab-token" --dry-run=client -o yaml \
  | kubectl apply --server-side --force-conflicts -f -
```

Use `--server-side`. A plain `kubectl apply` stores the whole object, token
included, in the Secret's `kubectl.kubernetes.io/last-applied-configuration`
annotation.

`install.sh` records the forge, host, project and Secret name in `install.env`: on a
first install, and on any later run that goes on to apply with a forge, host, project or
Secret name different from the file's. So `upgrade.sh` and the Day-2 menu go on rendering
what was applied, and on a GitLab install a `--gitops-repo`, `--gitops-host` or
`--gitlab-token-secret` is recorded, not a one-run override. A run you decline or end with
`--generate-only` records nothing. A switch to GitLab drops the file's `GITOPS_ORG`,
`GITHUB_APP_ID` and `GITHUB_PEM_PATH`; a switch back to GitHub drops the GitLab keys. Once
`install.env` exists, a `GITOPS_FORGE`, `GITOPS_HOST` or `GITLAB_TOKEN_SECRET` exported in
your shell is ignored; use the flags.

A re-run of `install.sh` keeps an existing Secret unless you give it a new token file, or
choose to replace it at the prompt. An empty token (whitespace only) is refused, and
nothing is stored.

## A self-managed GitLab with a private CA

Tested with GitLab 16.11 and 19.4.

Many self-managed instances use a TLS certificate that a private CA signed. The
agent does not trust that CA by default. Without it, each call to the instance
fails with `FORGE_TLS_UNTRUSTED`, and the detail names the host.

A private CA is for a self-managed host only. `--gitops-ca-file` needs a
`--gitops-host` that is not gitlab.com: gitlab.com presents a certificate that
the public CAs sign, and a private CA must never vouch for it.

Give the installer the CA certificate in PEM format:

```bash
./install.sh -y \
  --gitops-forge=gitlab \
  --gitops-host=gitlab.internal.example \
  --gitops-repo=platform/infra/gitops \
  --gitlab-token-file="$HOME/secure/gitlab-token" \
  --gitops-ca-file="$HOME/secure/internal-ca.pem"
```

The installer then does these steps:

1. It reads the file once, after the apply. The file can also be `/dev/stdin` or
   a process substitution.
2. It refuses a file that holds no `-----BEGIN CERTIFICATE-----` block, and a
   file that holds a private key.
3. It writes the certificates into the Secret `gitlab-forge-ca` (type generic),
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

To replace the CA, update the Secret. The broker reads the file on each call, so
the change needs no restart. kubelet can take about a minute to write a new
version of the file:

```bash
kubectl create secret generic gitlab-forge-ca -n <namespace> \
  --from-file=ca.crt="$HOME/secure/internal-ca.pem" --dry-run=client -o yaml \
  | kubectl apply --server-side --force-conflicts -f -
```

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

## What the install declares

The installer writes the forge into the `PlatformAgent` as `spec.integration.forges` and
`spec.integration.repositories`, not the `github` alias: one forge with `provider: gitlab`,
your host and the Secret as its `credentialsRef`, the CA Secret as its
`caBundleRef` when `--gitops-ca-file` gave one, and the project as the `gitops`
repository. The [PlatformAgent CRD reference](/kube-agents/operator/platformagent-crd/)
describes those fields, and a GitLab forge's limits, in full.
