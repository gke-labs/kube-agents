/*
Copyright 2026.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

	http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package controller

import (
	"encoding/json"
	"fmt"
	"os"
	"strconv"
	"strings"
	"sync"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	logf "sigs.k8s.io/controller-runtime/pkg/log"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The Hermes bridge, rendered by the operator.
//
// The bridge is the platform agent's executor on the bus: it consumes
// a2a.tasks.platform.*.in, runs Hermes, and publishes the result. Until this
// file, it existed only as a sidecar the CR declared on spec.deployment.sidecars,
// and only CI declared one (hack/ci-deploy.sh), so a stock `next` install had no
// executor and answered nothing. The bridge is release surface now, so the
// operator renders it beside the agent container under `next`, the way the
// agent's other next-only wiring is rendered (a2aAgentSurface: next, and version
// skew, where a frozen bus that is still running keeps its executor). Under
// `today` nothing is rendered.
//
// A CR that declares its own bridge sidecar keeps it: the declared one wins and
// the operator renders none (a2aBridgeDeclared says what counts as declared), so
// an install that already carries one does not get two bridges. Such a declared sidecar is still
// copied into the pod without regard to the mode, so it still crash-loops on a
// flip to `today` (a2a/docs/hermes-bridge.md); the rendered one does not.
//
// The rendered container is the agent container with the bridge's own settings
// on top, exactly as ci-deploy.sh built the declared one: the agent's env and
// mounts (it runs Hermes against the agent's profile state on the agent's PVC),
// its securityContext, resources, pull policy and envFrom; then the bus address,
// the static `bridge` principal's password, the concurrency, the activity
// secret and AGENT_SHARED_STATE_SETUP=skip. It never gets the bus token: the
// pod's ServiceAccount is the agent's principal, and the bridge authenticates
// with its own password for exactly that reason (bridgeIdentity).

const (
	// a2aBridgeContainerName is the bridge's container name, rendered or
	// declared. A CR sidecar under this name is the declared bridge.
	a2aBridgeContainerName = "hermes-bridge"

	// a2aBridgeImageEnvVar overrides the rendered bridge's image. Unset, the
	// image is derived from the agent container's: the bridge is built FROM
	// the platform-agent image of the same commit (a2a/Dockerfile.hermes-bridge),
	// so it takes the agent image's registry and tag.
	a2aBridgeImageEnvVar = "A2A_BRIDGE_IMAGE"
	a2aBridgeImageName   = "hermes-bridge"

	// platformAgentImageName is the release agent image's repository name,
	// the one the bridge image is published beside.
	platformAgentImageName = "platform-agent"

	// a2aAgentContainerName is the agent container the bridge is built from.
	a2aAgentContainerName = "platform-agent"

	// a2aBridgeExecutorCLI is the bridge's subprocess executor, the other
	// value it accepts beside a2aBridgeExecutorAPI.
	a2aBridgeExecutorCLI = "cli"

	// a2aRenderedBridgeDefaultConcurrency is the rendered bridge's worker
	// count when the operator sets none: the size of the pool Hermes's own
	// gateway runs model turns on today (gateway/run.py, ThreadPoolExecutor
	// max_workers=10, at the pinned hermes-agent tag), so moving chat onto the
	// bridge does not narrow it. The bridge binary's own default (2,
	// a2aBridgeDefaultConcurrency) still applies to a sidecar the CR declares
	// without setting it. At the default maxSessions the TASKS budget for 10
	// workers is above the 64-consumer floor, so an install whose TASKS stream
	// was created before this at the floor is refused by the provision Job
	// with the remedy named (recreate TASKS or lower maxSessions).
	a2aRenderedBridgeDefaultConcurrency = 10

	// a2aBridgeResourcesOperatorEnvVar overrides the rendered bridge's
	// resources: a corev1.ResourceRequirements in JSON, used whole. Unset, or
	// not one the operator can read, the bridge gets a2aRenderedBridgeResources'
	// defaults.
	a2aBridgeResourcesOperatorEnvVar = "A2A_BRIDGE_RESOURCES"

	// a2aBridgeConcurrencyOperatorEnvVar sets the rendered bridge's
	// BRIDGE_CONCURRENCY: an operator setting, like the other next-only
	// knobs, since no CR field carries it. Unset, the bridge's own default.
	// Read with the same rule as a declared sidecar's value
	// (a2aBridgeConcurrencyOf).
	a2aBridgeConcurrencyOperatorEnvVar = "A2A_BRIDGE_CONCURRENCY"

	// a2aBridgeExecutorOperatorEnvVar pins the rendered bridge's
	// BRIDGE_EXECUTOR. Unset, the bridge's shipped default decides (api,
	// since the agent's API_SERVER_KEY is copied); CI pins `cli` here until
	// the api executor is graded.
	a2aBridgeExecutorOperatorEnvVar = "A2A_BRIDGE_EXECUTOR"

	// The bridge's own environment contract (a2a/cmd/hermes-bridge/main.go),
	// spelled here because the operator cannot import that module.
	a2aBridgeNATSURLEnvVar      = "NATS_URL"
	a2aBridgeNATSUserEnvVar     = "NATS_USER"
	a2aBridgeNATSPasswordEnvVar = "NATS_PASSWORD"
)

// a2aBridgeDeclared reports whether the CR declares its own bridge sidecar:
// one named hermes-bridge, any sidecar whose env sets BRIDGE_CONCURRENCY (which
// is how every other reader of a declared bridge identifies one: the TASKS
// budget, the activity hook, a2aExecutorSidecarEnv), or any sidecar running the
// hermes-bridge image. The bridge binary doesn't need BRIDGE_CONCURRENCY set,
// so the image catches one that leaves it unset or takes it through envFrom.
// Missing any of these would render a second bridge beside the declared one,
// and the two would fight over the activity door's port.
func a2aBridgeDeclared(agent *agentv1alpha1.PlatformAgent) bool {
	if agent == nil || agent.Spec.Deployment == nil {
		return false
	}
	for _, c := range agent.Spec.Deployment.Sidecars {
		if c.Name == a2aBridgeContainerName {
			return true
		}
		if _, set := a2aBridgeConcurrencyValue(c); set {
			return true
		}
		if imageRepositoryName(c.Image) == a2aBridgeImageName {
			return true
		}
	}
	return false
}

// a2aBridgeRendered reports whether the operator renders the bridge for this
// agent: the agent carries its next-only wiring, and the CR declares no bridge
// of its own.
func a2aBridgeRendered(agent *agentv1alpha1.PlatformAgent) bool {
	return a2aAgentSurface(agent) && !a2aBridgeDeclared(agent)
}

// a2aBridgeInPod reports whether the rendered bridge is in the pod yet: it is
// rendered, and the bus has been provisioned once (BusProvisioned). Before
// that the bridge has no bus to connect to and no runtime-state bucket, exits,
// and would hold the agent pod in CrashLoopBackOff through every install's
// bring-up. Withheld until then, the way the gateway Deployment is withheld
// until the callout serves (a2aGatewayWaitsForCallout). The TASKS budget does
// not wait: it counts the bridge from the first render (a2aBridgeSidecars), so
// the first provisioning Job is already sized for it and the bridge's arrival
// does not re-render the Job.
func a2aBridgeInPod(agent *agentv1alpha1.PlatformAgent) bool {
	return a2aBridgeRendered(agent) && busProvisioned(agent)
}

// a2aRenderedBridgeSettings is the part of the rendered bridge's environment
// that the render-time readers (the TASKS budget, the activity hook) look at,
// built without the agent container: those readers run where the pod has not
// been built. The full container (buildA2ABridgeContainer) carries the same
// values, so the two cannot disagree about what they read.
func a2aRenderedBridgeSettings() []corev1.EnvVar {
	env := []corev1.EnvVar{
		{Name: a2aBridgeConcurrencyEnvVar, Value: a2aRenderedBridgeConcurrency()},
		// Copied from the agent container in the real render; the value
		// is the agent's constant, which is what selects the api executor
		// when BRIDGE_EXECUTOR is unset.
		{Name: a2aBridgeAPIServerKeyEnvVar, Value: loopbackAgentAPIKey},
	}
	if executor := a2aRenderedBridgeExecutor(); executor != "" {
		env = append(env, corev1.EnvVar{Name: a2aBridgeExecutorEnvVar, Value: executor})
	}
	return env
}

// a2aRenderedBridgeExecutor is the operator's A2A_BRIDGE_EXECUTOR when it is
// one the bridge accepts (api or cli), else "". The bridge refuses any other
// value at startup, before it dials the bus, and the container would
// crash-loop the whole agent pod; an unknown value is therefore treated as
// unset, so the shipped default decides. The refused value is logged once, with
// the two the bridge accepts, since the shipped default picks a different
// executor (and so a different persona) than the one the setting asked for.
func a2aRenderedBridgeExecutor() string {
	switch v := os.Getenv(a2aBridgeExecutorOperatorEnvVar); v {
	case "", a2aBridgeExecutorAPI, a2aBridgeExecutorCLI:
		return v
	default:
		if _, seen := a2aRefusedBridgeExecutors.LoadOrStore(v, true); !seen {
			logf.Log.WithName("platformagent-controller").Info(
				"Ignoring "+a2aBridgeExecutorOperatorEnvVar+": the bridge accepts only "+a2aBridgeExecutorAPI+" or "+a2aBridgeExecutorCLI+", so the rendered bridge runs its shipped default",
				"value", v)
		}
		return ""
	}
}

// a2aRefusedBridgeExecutors holds each refused A2A_BRIDGE_EXECUTOR value
// already logged, so a reconcile loop logs a typo once rather than per pass.
var a2aRefusedBridgeExecutors sync.Map

// a2aRenderedBridgeConcurrency is the rendered bridge's BRIDGE_CONCURRENCY:
// the operator setting when it is set, else a2aRenderedBridgeDefaultConcurrency.
// An invalid operator value is passed through as written, so the budget and
// the bridge fall back the same way they do for a declared sidecar's.
func a2aRenderedBridgeConcurrency() string {
	if v := os.Getenv(a2aBridgeConcurrencyOperatorEnvVar); v != "" {
		return v
	}
	return strconv.Itoa(a2aRenderedBridgeDefaultConcurrency)
}

// a2aBridgeSidecars is every sidecar the bridge readers consider: the CR's
// declared ones, plus the rendered bridge's settings when the operator renders
// it. The TASKS budget and the activity hook read this rather than
// spec.deployment.sidecars, so a rendered bridge is budgeted and hooked
// exactly like a declared one.
func a2aBridgeSidecars(agent *agentv1alpha1.PlatformAgent) []corev1.Container {
	return a2aBridgeSidecarsWhen(agent, a2aBridgeRendered(agent))
}

// a2aBridgeSidecarsInPod is a2aBridgeSidecars as the pod has them now: the
// rendered bridge only once it is in the pod (a2aBridgeInPod). The activity
// hook reads this, so the agent is not configured to post to a bridge door
// that is not there yet.
func a2aBridgeSidecarsInPod(agent *agentv1alpha1.PlatformAgent) []corev1.Container {
	return a2aBridgeSidecarsWhen(agent, a2aBridgeInPod(agent))
}

func a2aBridgeSidecarsWhen(agent *agentv1alpha1.PlatformAgent, withRendered bool) []corev1.Container {
	var out []corev1.Container
	if agent != nil && agent.Spec.Deployment != nil {
		out = append(out, agent.Spec.Deployment.Sidecars...)
	}
	if withRendered {
		out = append(out, corev1.Container{Name: a2aBridgeContainerName, Env: a2aRenderedBridgeSettings()})
	}
	return out
}

// a2aBridgeImage is the rendered bridge's image: the operator override; or,
// when the agent runs the release platform-agent image by tag, that image with
// its last path segment swapped for the bridge's (same registry, same commit);
// or else the image the other release A2A images resolve to
// (a2aReleaseImage). The swap is only sound for the stock repository name and
// a tag: a custom repository has no bridge published beside it, and a digest
// names one build the swap cannot carry over (it would fall back to :latest).
func a2aBridgeImage(agentImage string) string {
	if override := os.Getenv(a2aBridgeImageEnvVar); override != "" {
		return override
	}
	if imageRepositoryName(agentImage) == platformAgentImageName && imageRefHasTag(agentImage) {
		return deriveImageFromOperator(agentImage, a2aBridgeImageName)
	}
	return a2aReleaseImage(a2aBridgeImageEnvVar, a2aBridgeImageName)
}

// imageRepositoryName is a reference's last path segment without its tag or
// digest: "platform-agent" for ghcr.io/x/platform-agent:v1.
func imageRepositoryName(ref string) string {
	name := ref
	if i := strings.LastIndex(name, "/"); i >= 0 {
		name = name[i+1:]
	}
	if i := strings.IndexAny(name, ":@"); i >= 0 {
		name = name[:i]
	}
	return name
}

// a2aBridgeOwnEnv is what the bridge sets for itself, on top of the agent's
// env. The names are dropped from the copied agent env first, so the agent's
// own values for them (its bus user, its shared-state role) cannot leak in.
func a2aBridgeOwnEnv(agent *agentv1alpha1.PlatformAgent) []corev1.EnvVar {
	env := []corev1.EnvVar{
		{Name: sharedStateSetupEnvVar, Value: sharedStateSetupSkip},
		{Name: a2aBridgeNATSURLEnvVar, Value: a2aNATSClientURL(agent)},
		{Name: a2aBridgeNATSUserEnvVar, Value: a2aBridgeUser},
		{Name: a2aBridgeNATSPasswordEnvVar, ValueFrom: &corev1.EnvVarSource{SecretKeyRef: &corev1.SecretKeySelector{
			LocalObjectReference: corev1.LocalObjectReference{Name: a2aCredsSecretName(agent)},
			Key:                  a2aBridgePasswordKey,
		}}},
		{Name: a2aBridgeConcurrencyEnvVar, Value: a2aRenderedBridgeConcurrency()},
		a2aActivitySecretEnv(agent),
	}
	if executor := a2aRenderedBridgeExecutor(); executor != "" {
		env = append(env, corev1.EnvVar{Name: a2aBridgeExecutorEnvVar, Value: executor})
	}
	return env
}

// a2aBridgeDroppedAgentEnv are agent env names the bridge must not inherit:
// its own settings (replaced above), and the agent's bus identity, which names
// the `agent` principal and its inbox rather than the bridge's.
var a2aBridgeDroppedAgentEnv = map[string]bool{
	sharedStateSetupEnvVar:      true,
	a2aBridgeNATSURLEnvVar:      true,
	a2aBridgeNATSUserEnvVar:     true,
	a2aBridgeNATSPasswordEnvVar: true,
	a2aBridgeConcurrencyEnvVar:  true,
	a2aBridgeExecutorEnvVar:     true,
	a2aActivitySecretEnvVar:     true,
	a2aBusUserEnv:               true,
}

// buildA2ABridgeContainer renders the bridge from the finished agent
// container. Called after every mount the agent container gets, so the copy is
// of what the agent actually runs with; the bus token is then removed, since
// the bridge never holds it.
func buildA2ABridgeContainer(agent *agentv1alpha1.PlatformAgent, agentContainer corev1.Container) corev1.Container {
	env := make([]corev1.EnvVar, 0, len(agentContainer.Env))
	for _, e := range agentContainer.Env {
		if !a2aBridgeDroppedAgentEnv[e.Name] {
			env = append(env, e)
		}
	}
	env = append(env, a2aBridgeOwnEnv(agent)...)

	mounts := make([]corev1.VolumeMount, 0, len(agentContainer.VolumeMounts))
	for _, m := range agentContainer.VolumeMounts {
		if !a2aIsBusTokenMount(m) {
			mounts = append(mounts, m)
		}
	}

	return corev1.Container{
		Name:            a2aBridgeContainerName,
		Image:           a2aBridgeImage(agentContainer.Image),
		ImagePullPolicy: agentContainer.ImagePullPolicy,
		Env:             env,
		EnvFrom:         agentContainer.EnvFrom,
		VolumeMounts:    mounts,
		SecurityContext: agentContainer.SecurityContext.DeepCopy(),
		Resources:       a2aRenderedBridgeResources(),
	}
}

// a2aRenderedBridgeDefaultResources sizes the rendered bridge for the api
// executor, the default: a Go relay to the agent container's API server, where
// the turn itself runs. Measured idle at 1m CPU and 4-5Mi on three next
// installs (gke-labs#2748); the request leaves headroom for concurrent relays,
// and the limit is generous so a burst doesn't OOM it. The cli executor runs
// a one-shot hermes chat per task (about 430Mi each, up to BRIDGE_CONCURRENCY
// of them) and needs A2A_BRIDGE_RESOURCES set; it doesn't fit these.
func a2aRenderedBridgeDefaultResources() corev1.ResourceRequirements {
	return corev1.ResourceRequirements{
		Requests: corev1.ResourceList{
			corev1.ResourceCPU:    resource.MustParse("50m"),
			corev1.ResourceMemory: resource.MustParse("64Mi"),
		},
		Limits: corev1.ResourceList{
			corev1.ResourceCPU:    resource.MustParse("1"),
			corev1.ResourceMemory: resource.MustParse("512Mi"),
		},
	}
}

// a2aRenderedBridgeResources is the rendered bridge's resources: the
// operator's A2A_BRIDGE_RESOURCES when it reads as a ResourceRequirements with
// no request above its limit, else the defaults. A value it can't use is
// logged once and ignored, like a refused A2A_BRIDGE_EXECUTOR. Unlike before
// gke-labs#2748, the agent container's resources are not copied: that doubled
// the agent pod's requests and left next pods unschedulable on clusters sized
// for today.
func a2aRenderedBridgeResources() corev1.ResourceRequirements {
	raw := os.Getenv(a2aBridgeResourcesOperatorEnvVar)
	if raw == "" {
		return a2aRenderedBridgeDefaultResources()
	}
	var r corev1.ResourceRequirements
	err := json.Unmarshal([]byte(raw), &r)
	if err == nil {
		for name, req := range r.Requests {
			if lim, ok := r.Limits[name]; ok && req.Cmp(lim) > 0 {
				err = fmt.Errorf("the %s request %s is above its limit %s", name, req.String(), lim.String())
				break
			}
		}
	}
	if err != nil {
		if _, seen := a2aRefusedBridgeResources.LoadOrStore(raw, true); !seen {
			logf.Log.WithName("platformagent-controller").Info(
				"Ignoring "+a2aBridgeResourcesOperatorEnvVar+": it is not a usable ResourceRequirements in JSON, so the rendered bridge gets the default resources",
				"value", raw, "error", err.Error())
		}
		return a2aRenderedBridgeDefaultResources()
	}
	return r
}

// a2aRefusedBridgeResources holds each refused A2A_BRIDGE_RESOURCES value
// already logged.
var a2aRefusedBridgeResources sync.Map
