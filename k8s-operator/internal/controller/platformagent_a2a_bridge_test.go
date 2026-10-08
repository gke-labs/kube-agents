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
	"reflect"
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

func bridgeTestPod(agent *agentv1alpha1.PlatformAgent) corev1.PodTemplateSpec {
	return buildPodTemplateSpec(agent, "h", "h", "h", "h", nil, renderOptions{})
}

// provisionedAgent is a next agent whose bus has been provisioned once, which
// is when the rendered bridge enters the pod.
func provisionedAgent() *agentv1alpha1.PlatformAgent {
	agent := a2aTestAgent()
	setBusProvisionedCondition(agent, true, "provision-job", metav1.Now())
	return agent
}

func containersNamed(pod corev1.PodTemplateSpec, name string) []corev1.Container {
	var out []corev1.Container
	for _, c := range pod.Spec.Containers {
		if c.Name == name {
			out = append(out, c)
		}
	}
	return out
}

func envIndex(c corev1.Container) map[string]corev1.EnvVar {
	out := map[string]corev1.EnvVar{}
	for _, e := range c.Env {
		out[e.Name] = e
	}
	return out
}

// A stock next install, with no sidecar declared, gets a bridge: the executor
// the gateway routes chat to. Its environment is CI's declaration, rendered:
// the bus address, the static bridge principal and its password, the default
// concurrency, the activity secret, shared-state setup skipped, and the
// executor environment every task-executing sidecar gets.
func TestANextInstallWithNoDeclaredBridgeGetsOne(t *testing.T) {
	agent := provisionedAgent()
	pod := bridgeTestPod(agent)

	bridges := containersNamed(pod, a2aBridgeContainerName)
	if len(bridges) != 1 {
		t.Fatalf("got %d %s containers, want 1", len(bridges), a2aBridgeContainerName)
	}
	b := bridges[0]
	agentC := containersNamed(pod, "platform-agent")[0]
	env := envIndex(b)

	want := map[string]string{
		a2aBridgeNATSURLEnvVar:     a2aNATSClientURL(agent),
		a2aBridgeNATSUserEnvVar:    a2aBridgeUser,
		a2aBridgeConcurrencyEnvVar: "2",
		sharedStateSetupEnvVar:     sharedStateSetupSkip,
	}
	for name, value := range want {
		if env[name].Value != value {
			t.Errorf("%s = %q, want %q", name, env[name].Value, value)
		}
	}
	if ref := env[a2aBridgeNATSPasswordEnvVar].ValueFrom; ref == nil || ref.SecretKeyRef == nil ||
		ref.SecretKeyRef.Name != a2aCredsSecretName(agent) || ref.SecretKeyRef.Key != a2aBridgePasswordKey {
		t.Errorf("NATS_PASSWORD = %+v, want the %s key of %s", env[a2aBridgeNATSPasswordEnvVar], a2aBridgePasswordKey, a2aCredsSecretName(agent))
	}
	if ref := env[a2aActivitySecretEnvVar].ValueFrom; ref == nil || ref.SecretKeyRef == nil || ref.SecretKeyRef.Key != a2aBridgeActivityKey {
		t.Errorf("%s = %+v, want the activity key", a2aActivitySecretEnvVar, env[a2aActivitySecretEnvVar])
	}
	for _, name := range []string{"POD_NAMESPACE", a2aCapabilityRequiredEnvVar} {
		if _, ok := env[name]; !ok {
			t.Errorf("the rendered bridge lacks %s, the executor environment a declared bridge gets", name)
		}
	}
	// The agent's own bus identity names the `agent` principal; the bridge
	// is a different principal and must not carry it.
	if _, ok := env[a2aBusUserEnv]; ok {
		t.Errorf("the rendered bridge carries %s, the agent container's bus identity", a2aBusUserEnv)
	}
	if _, ok := env[a2aBridgeExecutorEnvVar]; ok {
		t.Errorf("the rendered bridge pins %s with no operator setting; the shipped default should decide", a2aBridgeExecutorEnvVar)
	}

	// Never the bus token: the pod's ServiceAccount is the agent's principal.
	for _, m := range b.VolumeMounts {
		if a2aIsBusTokenMount(m) {
			t.Error("the rendered bridge mounts the bus token")
		}
	}
	// The agent's state, which is what the bridge runs Hermes against.
	mounted := map[string]bool{}
	for _, m := range b.VolumeMounts {
		mounted[m.Name] = true
	}
	for _, m := range agentC.VolumeMounts {
		if !a2aIsBusTokenMount(m) && !mounted[m.Name] {
			t.Errorf("the rendered bridge lacks the agent's mount %s", m.Name)
		}
	}
	if !reflect.DeepEqual(b.SecurityContext, agentC.SecurityContext) || !reflect.DeepEqual(b.Resources, agentC.Resources) {
		t.Error("the rendered bridge's securityContext or resources differ from the agent container's")
	}
	if b.Image != deriveImageFromOperator(agentC.Image, a2aBridgeImageName) {
		t.Errorf("image = %q, want the agent image's registry and tag under %s", b.Image, a2aBridgeImageName)
	}
}

// Under today nothing is rendered, which is also what ends the rollback
// crash-loop: there is no bridge left behind to dial a torn-down bus.
func TestATodayInstallGetsNoBridge(t *testing.T) {
	// Provisioned first: on the pass that flips to today the CR still carries
	// BusProvisioned (the end-of-pass status write removes it), so the mode
	// check is the only thing keeping the bridge out.
	agent := provisionedAgent()
	agent.Spec.Mode = ptr.To("today")
	if got := containersNamed(bridgeTestPod(agent), a2aBridgeContainerName); len(got) != 0 {
		t.Errorf("a today install renders %d bridge containers", len(got))
	}
	agent.Spec.Mode = nil
	if got := containersNamed(bridgeTestPod(agent), a2aBridgeContainerName); len(got) != 0 {
		t.Errorf("an install with no mode renders %d bridge containers", len(got))
	}
}

// Version skew freezes a running bus rather than tearing it down, so its
// executor stays, like the agent container's own bus wiring.
func TestVersionSkewKeepsTheRenderedBridge(t *testing.T) {
	agent := provisionedAgent()
	agent.Spec.Mode = ptr.To("later")
	if got := containersNamed(bridgeTestPod(agent), a2aBridgeContainerName); len(got) != 1 {
		t.Errorf("version skew renders %d bridge containers, want 1", len(got))
	}
}

// A CR that declares its own bridge keeps it, and the operator renders none,
// so the pod never carries two containers of one name.
func TestADeclaredBridgeWins(t *testing.T) {
	agent := a2aTestAgent()
	agent.Spec.Deployment = &agentv1alpha1.DeploymentSpec{Sidecars: []corev1.Container{{
		Name: a2aBridgeContainerName, Image: "registry.example/declared-bridge:1",
		Env: []corev1.EnvVar{{Name: a2aBridgeConcurrencyEnvVar, Value: "5"}},
	}}}
	bridges := containersNamed(bridgeTestPod(agent), a2aBridgeContainerName)
	if len(bridges) != 1 || bridges[0].Image != "registry.example/declared-bridge:1" {
		t.Fatalf("bridges = %+v, want the one declared", bridges)
	}
	if n := a2aBridgeConcurrency(agent); n != 5 {
		t.Errorf("the budget reads %d workers, want the declared 5 alone", n)
	}
}

// The operator settings reach the rendered bridge, and the budget reads the
// same concurrency the bridge runs with.
func TestTheOperatorSettingsReachTheRenderedBridge(t *testing.T) {
	t.Setenv(a2aBridgeImageEnvVar, "registry.example/hermes-bridge:pinned")
	t.Setenv(a2aBridgeConcurrencyOperatorEnvVar, "6")
	t.Setenv(a2aBridgeExecutorOperatorEnvVar, "cli")
	agent := provisionedAgent()
	b := containersNamed(bridgeTestPod(agent), a2aBridgeContainerName)[0]
	env := envIndex(b)
	if b.Image != "registry.example/hermes-bridge:pinned" {
		t.Errorf("image = %q, want the override", b.Image)
	}
	if env[a2aBridgeConcurrencyEnvVar].Value != "6" || env[a2aBridgeExecutorEnvVar].Value != "cli" {
		t.Errorf("concurrency/executor = %q/%q, want 6/cli", env[a2aBridgeConcurrencyEnvVar].Value, env[a2aBridgeExecutorEnvVar].Value)
	}
	if n := a2aBridgeConcurrency(agent); n != 6 {
		t.Errorf("the TASKS budget reads %d workers, the bridge runs 6", n)
	}
}

// The budget's default and the rendered bridge's default are one number, so a
// stock install's TASKS sizing is unchanged by the render.
func TestARenderedBridgeAtItsDefaultLeavesTheBudgetAlone(t *testing.T) {
	agent := a2aTestAgent()
	if n, capped, defaulted := a2aBridgeWorkers(agent); n != a2aBridgeDefaultConcurrency || capped || defaulted {
		t.Errorf("a2aBridgeWorkers = %d,%v,%v; want the default %d with no flags", n, capped, defaulted, a2aBridgeDefaultConcurrency)
	}
}

// Before the bus is provisioned the bridge has nothing to connect to and would
// crash-loop the agent pod through bring-up, so it is withheld from the pod.
// The TASKS budget counts it anyway, so the first provisioning Job is sized for
// the bridge that will arrive and does not re-render when it does.
func TestTheRenderedBridgeWaitsForTheBusButIsBudgetedFromTheStart(t *testing.T) {
	t.Setenv(a2aBridgeConcurrencyOperatorEnvVar, "6")
	agent := a2aTestAgent()
	if got := containersNamed(bridgeTestPod(agent), a2aBridgeContainerName); len(got) != 0 {
		t.Errorf("the bridge is in the pod before the bus is provisioned (%d containers)", len(got))
	}
	if n := a2aBridgeConcurrency(agent); n != 6 {
		t.Errorf("before provisioning the budget reads %d workers, want the 6 the bridge will run", n)
	}
	before := a2aProvisionScript(agent)
	setBusProvisionedCondition(agent, true, "provision-job", metav1.Now())
	if got := containersNamed(bridgeTestPod(agent), a2aBridgeContainerName); len(got) != 1 {
		t.Errorf("after provisioning the pod has %d bridge containers, want 1", len(got))
	}
	if a2aProvisionScript(agent) != before {
		t.Error("the provision script changed when the bridge arrived; the Job would re-render")
	}
}

// The bridge image is swapped in beside the agent's only for the release
// platform-agent repository by tag; a custom repository or a digest pin falls
// back to the image the other release A2A images resolve to.
func TestTheBridgeImageFollowsTheAgentOnlyWhereItCan(t *testing.T) {
	t.Setenv(operatorImageEnvVar, "registry.example/kube-agents/k8s-operator:v9")
	cases := map[string]string{
		"ghcr.io/gke-labs/kube-agents/platform-agent:abc":      "ghcr.io/gke-labs/kube-agents/hermes-bridge:abc",
		"registry.example/mirror/my-agent:v1":                  "registry.example/kube-agents/hermes-bridge:v9",
		"ghcr.io/gke-labs/kube-agents/platform-agent@sha256:0": "registry.example/kube-agents/hermes-bridge:v9",
	}
	for agentImage, want := range cases {
		if got := a2aBridgeImage(agentImage); got != want {
			t.Errorf("a2aBridgeImage(%q) = %q, want %q", agentImage, got, want)
		}
	}
}

// A refusal for a rendered bridge points at the operator setting that sized it,
// not at a CR sidecar nobody declared.
func TestARefusalForARenderedBridgeNamesTheOperatorSetting(t *testing.T) {
	t.Setenv(a2aBridgeConcurrencyOperatorEnvVar, "8")
	agent := a2aTestAgent()
	status := a2aProvisionRefusalStatus(agent)
	if !strings.Contains(status, a2aBridgeConcurrencyOperatorEnvVar) || strings.Contains(status, "spec.deployment.sidecars") {
		t.Errorf("refusal status = %q; want it to name %s and not a CR sidecar", status, a2aBridgeConcurrencyOperatorEnvVar)
	}
	if !strings.Contains(a2aProvisionScript(agent), a2aBridgeConcurrencyOperatorEnvVar) {
		t.Error("the provision script's notes do not name the operator setting for a rendered bridge")
	}
}

// An operator setting the render cannot read as a count is reported against
// that setting, not against a CR sidecar.
func TestAnUnreadableOperatorSettingIsReportedAgainstItself(t *testing.T) {
	t.Setenv(a2aBridgeConcurrencyOperatorEnvVar, "lots")
	status := a2aProvisionRefusalStatus(a2aTestAgent())
	if !strings.Contains(status, a2aBridgeConcurrencyOperatorEnvVar) || strings.Contains(status, "spec.deployment.sidecars") {
		t.Errorf("refusal status = %q; want the operator setting named, not a CR sidecar", status)
	}
}

// A bridge declared under another name is still a declared bridge, by the
// same rule every other reader uses (it sets BRIDGE_CONCURRENCY), so the
// operator renders no second one beside it.
func TestABridgeDeclaredUnderAnotherNameStillWins(t *testing.T) {
	agent := provisionedAgent()
	agent.Spec.Deployment = &agentv1alpha1.DeploymentSpec{Sidecars: []corev1.Container{{
		Name: "my-bridge", Image: "registry.example/bridge:1",
		Env: []corev1.EnvVar{{Name: a2aBridgeConcurrencyEnvVar, Value: "3"}},
	}}}
	if got := containersNamed(bridgeTestPod(agent), a2aBridgeContainerName); len(got) != 0 {
		t.Errorf("the operator rendered a bridge beside one declared as my-bridge (%d containers)", len(got))
	}
	if n := a2aBridgeConcurrency(agent); n != 3 {
		t.Errorf("the budget reads %d workers, want the declared 3 alone", n)
	}
}

// An executor value the bridge refuses would crash-loop the whole agent pod, so
// the operator passes through only api or cli; anything else leaves the
// shipped default to decide.
func TestAnUnknownExecutorSettingIsNotRendered(t *testing.T) {
	for value, want := range map[string]string{"api": "api", "cli": "cli", "CLI": "", "subprocess": "", " cli": ""} {
		t.Run(value, func(t *testing.T) {
			t.Setenv(a2aBridgeExecutorOperatorEnvVar, value)
			b := containersNamed(bridgeTestPod(provisionedAgent()), a2aBridgeContainerName)[0]
			got, ok := envIndex(b)[a2aBridgeExecutorEnvVar]
			if want == "" && ok {
				t.Errorf("%s=%q rendered %s=%q; the bridge refuses it", a2aBridgeExecutorOperatorEnvVar, value, a2aBridgeExecutorEnvVar, got.Value)
			}
			if want != "" && got.Value != want {
				t.Errorf("%s = %q, want %q", a2aBridgeExecutorEnvVar, got.Value, want)
			}
		})
	}
}

// An unreadable operator concurrency is reported in the provision script's
// note against the operator setting, not against a CR sidecar.
func TestTheProvisionNoteForAnUnreadableSettingNamesIt(t *testing.T) {
	t.Setenv(a2aBridgeConcurrencyOperatorEnvVar, "lots")
	script := a2aProvisionScript(a2aTestAgent())
	if !strings.Contains(script, "NOTE: the operator's "+a2aBridgeConcurrencyOperatorEnvVar) || strings.Contains(script, "NOTE: a spec.deployment.sidecars entry sets") {
		t.Error("the provision script's read note does not name the operator setting for a rendered bridge")
	}
}
