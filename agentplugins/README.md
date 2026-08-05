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
and pushes its image, and installs the chart. Nothing is defaulted to a particular fleet:
the project comes from your `gcloud` config, and values that cannot be guessed are
required, because the failure they cause is silent. See the plugin's own README.

`PLUGIN_IMAGE=<ref>` installs an image that already exists and skips the build — for
environments without Cloud Build, and for pipelines that build once and install many
times.

## Testing

| Kind             | Where                                  | Needs a cluster |
| ---------------- | -------------------------------------- | --------------- |
| Unit             | `<plugin>/tests/test_*.py`             | no              |
| Live deployment  | `<plugin>/tests/*_e2e_test.py`         | yes             |
| Manual scenarios | `gke-stockout-investigator/scenarios/` | yes             |

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
