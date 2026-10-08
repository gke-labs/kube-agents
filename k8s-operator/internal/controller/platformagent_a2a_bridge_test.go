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
	"strconv"
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
		a2aBridgeConcurrencyEnvVar: strconv.Itoa(a2aRenderedBridgeDefaultConcurrency),
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

// With no operator setting the rendered bridge runs the api executor, the
// shipped default and what the next eval lane measures (hack/ci-deploy.sh sets
// no A2A_BRIDGE_EXECUTOR): it carries no BRIDGE_EXECUTOR, and it keeps the
// agent container's non-blank API_SERVER_KEY, which is what the bridge's
// bridgeExecutor reads to pick api over its keyless cli fallback. The pod's
// actual container is checked, not a2aRenderedBridgeSettings, so a copy that
// dropped the key would show here even if the settings still listed it.
func TestARenderedBridgeWithNoExecutorSettingRunsTheAPIExecutor(t *testing.T) {
	t.Setenv(a2aBridgeExecutorOperatorEnvVar, "")
	b := containersNamed(bridgeTestPod(provisionedAgent()), a2aBridgeContainerName)[0]
	env := envIndex(b)
	if e, ok := env[a2aBridgeExecutorEnvVar]; ok {
		t.Errorf("the rendered bridge sets %s=%q with no operator setting", a2aBridgeExecutorEnvVar, e.Value)
	}
	if key := env[a2aBridgeAPIServerKeyEnvVar]; strings.TrimSpace(key.Value) == "" && key.ValueFrom == nil {
		t.Errorf("the rendered bridge has no %s, so the bridge would fall back to the cli executor", a2aBridgeAPIServerKeyEnvVar)
	}
	if !a2aBridgeRunsAPIExecutor(b) {
		t.Error("the operator reads the rendered bridge as a cli bridge, so it would render no activity hook for it")
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

// With no operator setting the rendered bridge runs
// a2aRenderedBridgeDefaultConcurrency workers, Hermes's own gateway pool, and
// the TASKS budget reads the same number rather than the module default a CR
// with no bridge at all would get.
func TestARenderedBridgeAtItsDefaultIsBudgetedAtTheRenderedDefault(t *testing.T) {
	agent := a2aTestAgent()
	if n, capped, defaulted := a2aBridgeWorkers(agent); n != a2aRenderedBridgeDefaultConcurrency || capped || defaulted {
		t.Errorf("a2aBridgeWorkers = %d,%v,%v; want the rendered default %d with no flags", n, capped, defaulted, a2aRenderedBridgeDefaultConcurrency)
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

// The refusal an install created before the rendered default meets: no
// operator setting, ten workers, a TASKS stream made at the floor. Its remedy
// must not say that unsetting the setting gets the bridge's own default of 2;
// unset is what it already is, and it runs the rendered default.
func TestARefusalAtTheRenderedDefaultDoesNotOfferUnsetAsALowerCount(t *testing.T) {
	t.Setenv(a2aBridgeConcurrencyOperatorEnvVar, "")
	agent := a2aTestAgent()
	status := a2aProvisionRefusalStatus(agent)
	ten := strconv.Itoa(a2aRenderedBridgeDefaultConcurrency)
	for _, want := range []string{"its default of " + ten, "unset, the rendered bridge runs " + ten} {
		if !strings.Contains(status, want) {
			t.Errorf("refusal status lacks %q:\n%s", want, status)
		}
	}
	if strings.Contains(status, "bridge's default of 2") {
		t.Errorf("refusal status offers the bridge's own default of 2 to a rendered bridge:\n%s", status)
	}
	script := a2aProvisionScript(agent)
	if !strings.Contains(script, "unset, it runs "+ten) || !strings.Contains(script, "or "+ten+" when that is unset") {
		t.Error("the provision script's refusal does not name the rendered default for an unset operator setting")
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

// The bridge runs Hermes against the agent's profile state, so it inherits the
// agent container's env: every entry but the dropped names reaches it once,
// value or valueFrom as written. Checked entry by entry, so a duplicated or
// shadowed name fails rather than collapsing in an index.
func TestTheRenderedBridgeCarriesTheAgentsEnvExceptTheDroppedNames(t *testing.T) {
	pod := bridgeTestPod(provisionedAgent())
	b := containersNamed(pod, a2aBridgeContainerName)[0]
	agentC := containersNamed(pod, "platform-agent")[0]
	dropped := a2aBridgeDroppedAgentEnv
	carried := 0
	for _, want := range agentC.Env {
		var got []corev1.EnvVar
		for _, e := range b.Env {
			if e.Name == want.Name {
				got = append(got, e)
			}
		}
		if dropped[want.Name] {
			// The bridge sets some of these itself (NATS_URL, the activity
			// secret), with values that may match the agent's; the drop is
			// held by the tests that name each one.
			continue
		}
		if len(got) != 1 || !reflect.DeepEqual(got[0], want) {
			t.Errorf("the agent's %s reaches the bridge as %+v, want it once as %+v", want.Name, got, want)
			continue
		}
		carried++
	}
	if carried == 0 {
		t.Fatal("no agent env entry reached the bridge; the probe is vacuous")
	}
}

// The bridge binary doesn't need BRIDGE_CONCURRENCY set, so a sidecar running
// the hermes-bridge image under another name, with the key unset or arriving
// through envFrom, is a declared bridge too. Rendering a second one beside it
// would put two listeners on the activity door's port.
func TestABridgeImageUnderAnotherNameWithNoConcurrencyStillWins(t *testing.T) {
	for name, sidecar := range map[string]corev1.Container{
		"key unset": {Name: "my-bridge", Image: "registry.example/hermes-bridge:1"},
		"envFrom": {Name: "my-bridge", Image: "registry.example/kube-agents/hermes-bridge@sha256:" + strings.Repeat("a", 64),
			EnvFrom: []corev1.EnvFromSource{{ConfigMapRef: &corev1.ConfigMapEnvSource{LocalObjectReference: corev1.LocalObjectReference{Name: "bridge-env"}}}}},
	} {
		t.Run(name, func(t *testing.T) {
			agent := provisionedAgent()
			agent.Spec.Deployment = &agentv1alpha1.DeploymentSpec{Sidecars: []corev1.Container{sidecar}}
			if got := containersNamed(bridgeTestPod(agent), a2aBridgeContainerName); len(got) != 0 {
				t.Errorf("the operator rendered a bridge beside a hermes-bridge image declared as %s (%d containers)", sidecar.Name, len(got))
			}
		})
	}
	// A sidecar on some other image, setting nothing, is not a bridge.
	agent := provisionedAgent()
	agent.Spec.Deployment = &agentv1alpha1.DeploymentSpec{Sidecars: []corev1.Container{{Name: "sidecar", Image: "registry.example/log-shipper:1"}}}
	if got := containersNamed(bridgeTestPod(agent), a2aBridgeContainerName); len(got) != 1 {
		t.Errorf("an unrelated sidecar suppressed the rendered bridge (%d containers)", len(got))
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

// A refused executor value is recorded as logged, once per value, so the
// typo shows up in the operator log instead of only as the bridge's start line.
// Unset and the two accepted values are not refusals.
func TestARefusedExecutorSettingIsLoggedOnce(t *testing.T) {
	for _, v := range []string{"", a2aBridgeExecutorAPI, a2aBridgeExecutorCLI} {
		t.Setenv(a2aBridgeExecutorOperatorEnvVar, v)
		a2aRenderedBridgeExecutor()
		if _, logged := a2aRefusedBridgeExecutors.Load(v); logged {
			t.Errorf("%q was logged as refused", v)
		}
	}
	t.Setenv(a2aBridgeExecutorOperatorEnvVar, "Cli-refused-once")
	if got := a2aRenderedBridgeExecutor(); got != "" {
		t.Fatalf("a refused value rendered %q", got)
	}
	if _, logged := a2aRefusedBridgeExecutors.Load("Cli-refused-once"); !logged {
		t.Error("the refused value was not logged")
	}
}
