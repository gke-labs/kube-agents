# Agent plugins

Optional capabilities that ship **outside** the agent image. Each directory here is one
plugin: a Helm chart that creates an `AgentPlugin` custom resource, and an OCI image
holding the code the agent loads. The operator mounts the image and enables the plugin in
the profile the CR names; nothing in `deploy/` or `agents/` has to change to add one.

| Plugin                                                    | What it adds                                                                      |
| --------------------------------------------------------- | --------------------------------------------------------------------------------- |
| [`pubsub-platform`](pubsub-platform/)                     | A Pub/Sub ingress adapter: turns filtered Cloud Logging alerts into agent work    |
| [`gke-stockout-investigator`](gke-stockout-investigator/) | A skill that diagnoses GKE scale-up failures and proposes a GitOps remediation PR |

The two are usually installed together — the investigator's alerts arrive through the
adapter — but neither requires the other.

## Installing

Each plugin has an `install.sh` that provisions whatever cloud resources it needs, builds
and pushes its image, and installs the chart. It needs `gcloud`, `kubectl`, `helm`, an
image builder and an agent already deployed to attach to — but no Cloud Build, and no
image to build or push beforehand. Nothing is defaulted to a particular fleet: the
project comes from your `gcloud` config, and values that cannot be guessed are required,
because the failure they cause is silent. See the plugin's own README.

Before it provisions anything, an installer settles its image reference, checks its
builder, creates the registry repository if it is missing and confirms it can get a
credential for it — so anything you have to leave the terminal to fix costs you nothing to
fix. Confirms, not stores: the login that writes a token to `~/.docker/config.json` waits
until the push, so a run that fails partway leaves no credential behind. The one failure
that cannot be brought forward is push _permission_, which nothing short of a push
establishes. Everything after that point is idempotent.

## Images

A plugin image is `FROM scratch` plus a COPY of one directory, so the whole build is a
single tar layer and [`lib/plugin_image.sh`](lib/plugin_image.sh) — shared by both
installers — can produce it locally. Nothing here submits a Cloud Build: that needs the
API, the billing and a credential Cloud Build will accept for quota, none of which a
laptop reliably has, and the installers have to work there.

| Variable           | Default                                                    | What it changes                                       |
| ------------------ | ---------------------------------------------------------- | ----------------------------------------------------- |
| `PLUGIN_IMAGE`     | unset                                                      | Install this exact reference; skip the build          |
| `IMAGE_BUILDER`    | `auto` — `docker` when a daemon answers, otherwise `crane` | How the layer is produced                             |
| `CRANE_BIN`        | `crane`                                                    | Where crane lives                                     |
| `TARGET_PLATFORM`  | `linux/amd64`                                              | The platform stamped on the image                     |
| `AR_LOCATION`      | the agent image's, else `$REGION`, else `us-central1`      | Artifact Registry location                            |
| `AR_PROJECT`       | the agent image's, else the install project                | Artifact Registry project                             |
| `AR_REPOSITORY`    | the agent image's, else `kube-agents`                      | Artifact Registry repository (created if absent)      |
| `PLUGIN_IMAGE_TAG` | a 12-character digest of the source and the build          | The tag published; rebuilt but not rolled — see below |

`AR_LOCATION`, `AR_PROJECT` and `AR_REPOSITORY` each pin one part of the reference and
switch discovery off for that part alone. `REGION` and `GCP_ARTIFACT_REGISTRY_REPO_NAME` —
the variables the provisioning scripts export, and the last fallbacks for the location and
the repository — are deliberately not pins: one left in your shell by `dev_rebuild_agent.sh`
must not outrank the registry the agent is demonstrably being pulled from.

What the image leaves out is the plugin's `.dockerignore`, read once and applied to the
content tag, to `docker build` and to the crane layer alike. It is the only place to write
an exclusion: a builder that had its own list could ship a different set of files under a
tag that claims otherwise, and since a published tag is never rebuilt, whichever builder
got there first would define that image for good. Keeping one reader means keeping to the
patterns it implements — `**/name`, anchored paths, `*` and `?` within a path segment — and
it refuses a `.dockerignore` that uses anything else (`!` re-includes, a `**` anywhere but
the front) rather than matching it differently from Docker.

Two defaults are load-bearing:

- **Artifact Registry, not `gcr.io`.** Container Registry is deprecated and its hosts are
  being turned down. Which Artifact Registry is read off the agent's own image on the
  `PlatformAgent`, rather than guessed: nothing in this repository grants
  `artifactregistry.reader`, so whether the kubelet can pull a plugin image depends on how
  the fleet scoped that role, and the agent's image is the one reference it is already
  known to pull. Publishing beside it needs no new grant; publishing anywhere else may. So
  the whole reference is copied — location, project and repository — and not just the part
  that is convenient: taking the repository from the agent but the project from the install
  would name a repository that satisfies none of the above and most likely does not exist.
  Where that lands the images in a different project from the rest of the install, the
  installer says so. If it has to create a repository it says that too, because a new one
  has no reader binding of its own.

  There is only something to copy when the agent runs from Artifact Registry, which is the
  provisioning scripts' install but not the chart's: `charts/kube-agents` defaults the agent
  to `ghcr.io/gke-labs/kube-agents/platform-agent`, and an agent pulled from ghcr.io says
  nothing about where a plugin image should go. The fallbacks then apply — `$REGION` or
  `us-central1`, `$GCP_ARTIFACT_REGISTRY_REPO_NAME` or `kube-agents`, the install project —
  and that is a guess, so the repository it names may be one the installer has to create.
  This is the case the reader-binding note above is printed for. Set `AR_LOCATION`,
  `AR_PROJECT` and `AR_REPOSITORY` to publish somewhere the nodes are known to be able to
  pull from.

- **A content tag, not `latest`.** With a fixed tag the second install renders a
  byte-identical `AgentPlugin`, so the operator sees no change, never rolls the pod, and
  the edited skill stays unpublished on a deployment that reports healthy. Tagging by
  content makes the CR change exactly when the image did — and re-running the installer
  with nothing changed still republishes nothing. The digest covers how the image is built
  as well as what goes in it — the plugin's `Dockerfile` and `.dockerignore` are hashed
  alongside the source tree — because a published tag is never rebuilt: were the build
  alone to change, every image already published would keep the old behaviour forever.
  `PLUGIN_IMAGE_TAG` opts out of all of this, and it opts out of the safety with it. A tag
  you chose is no evidence of what is behind it, so the installer never skips the build for
  one — it rebuilds and overwrites on every install. That republishes the bytes; it does
  not deliver them. The `AgentPlugin` still renders byte-identical, so the operator still
  sees no change and the pod still never rolls, and `spec.imagePullPolicy` defaults to
  `IfNotPresent`, so even a pod that did roll would mount the copy the node already has
  under that tag. Pin a tag only where something else moves it — a per-build tag from CI,
  or your own `imagePullPolicy: Always` — and reach for `PLUGIN_IMAGE` rather than
  `PLUGIN_IMAGE_TAG` when what you want is to install an image that already exists.

## Testing

| Kind             | Where                                  | Needs a cluster |
| ---------------- | -------------------------------------- | --------------- |
| Unit             | `<plugin>/tests/test_*.py`             | no              |
| Live deployment  | `<plugin>/tests/*_e2e_test.py`         | yes             |
| Manual scenarios | `gke-stockout-investigator/scenarios/` | yes             |

A live-deployment test installs what it tests by running the plugins' own `install.sh`
first, so what it exercises is the chart in this repository rather than whatever happens
to be deployed on the cluster; `SKIP_INSTALL=true` reuses the existing deployment.

CI runs the unit tests only, one plugin at a time
([`agentplugins-test.yml`](../.github/workflows/agentplugins-test.yml)); the rest are run
by hand against a deployment.

## Adding a plugin

A plugin is a chart plus an image, so the minimum is `Chart.yaml`, a `templates/` that
renders an `AgentPlugin`, a `Dockerfile` over the files the agent loads, and an
`install.sh`. Two things are worth copying from the existing pair rather than rediscovering:

- **`metadata.name` is `^[a-z][a-z0-9]*$`.** It is the Helm release, the CR name, the mount
  directory _and_ the Python module Hermes imports, so hyphens are rejected — the chart
  directory may be hyphenated, the release cannot be.
- **Where a plugin is installed decides whether its skills resolve.** A plugin with
  `spec.targetProfile` is loaded only by that profile; a skill it registers is addressed as
  `<plugin>:<skill>` and does not resolve anywhere else. See
  [the AgentPlugin CRD reference](../docs/site/src/content/docs/operator/agentplugin-crd.md).
