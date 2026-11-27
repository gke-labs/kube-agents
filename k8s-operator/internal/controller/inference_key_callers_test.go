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
	"context"
	"errors"
	"net/http"
	"strings"
	"sync/atomic"
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"
	"sigs.k8s.io/yaml"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// callerKeysTest is a reconciler over an agent with the curator on, the
// gateway with one Ready pod, and a profile at the read (interactive, warm
// pool) and unattended tiers, whose gateway pods admit the probe key only
// while admit is set.
type callerKeysTest struct {
	r     *PlatformAgentReconciler
	cl    client.Client
	req   types.NamespacedName
	agent *agentv1alpha1.PlatformAgent
	admit atomic.Bool
}

func newCallerKeysTest(t *testing.T) *callerKeysTest {
	t.Helper()
	t.Setenv(a2aCuratorEnvVar, "true")
	agent := inferenceKeysTestAgent()
	profile := testAgentProfile(agent.Namespace, "auditor", func(p *agentv1alpha1.AgentProfile) {
		p.Spec.AllowedTiers = []string{agentv1alpha1.AgentProfileTierRead, agentv1alpha1.AgentProfileTierUnattended}
	})
	r, cl, req := a2aGateTestReconciler(t, agent, platformAgentCRDObject(true), litellmGatewayDeployment(agent), litellmGatewayReplicaSet(agent),
		litellmGatewayPod(agent, "litellm-a", "10.1.0.1"), profileNamespaceObject(agent), &profile)
	tc := &callerKeysTest{r: r, cl: cl, req: req.NamespacedName, agent: agent}
	r.inferenceKeyProbe = &fakeInferenceKeyProber{answer: func(string, string) (int, error) {
		if tc.admit.Load() {
			return http.StatusOK, nil
		}
		return http.StatusUnauthorized, nil
	}}
	return tc
}

func (tc *callerKeysTest) reconcile(t *testing.T) {
	t.Helper()
	if _, err := tc.r.Reconcile(context.Background(), ctrl.Request{NamespacedName: tc.req}); err != nil {
		t.Fatalf("Reconcile: %v", err)
	}
}

func (tc *callerKeysTest) condition(t *testing.T) *metav1.Condition {
	t.Helper()
	got := &agentv1alpha1.PlatformAgent{}
	if err := tc.cl.Get(context.Background(), tc.req, got); err != nil {
		t.Fatal(err)
	}
	return meta.FindStatusCondition(got.Status.Conditions, inferenceKeysServedCondition)
}

// callerRender is what each caller was rendered with: its key variable, nil
// when absent, and the agent's managed model.api_key.
type callerRender struct {
	agentAPIKey   string
	agentEnv      *corev1.EnvVar
	bridgeEnv     *corev1.EnvVar
	curatorEnv    *corev1.EnvVar
	poolEnv       *corev1.EnvVar
	poolBaseURL   string
	sandboxHasKey bool
}

func keyCallerEnv(env []corev1.EnvVar, name string) *corev1.EnvVar {
	for i := range env {
		if env[i].Name == name {
			return &env[i]
		}
	}
	return nil
}

func keyCallerContainer(spec corev1.PodSpec, name string) *corev1.Container {
	for i := range spec.Containers {
		if spec.Containers[i].Name == name {
			return &spec.Containers[i]
		}
	}
	return nil
}

func (tc *callerKeysTest) render(t *testing.T) callerRender {
	t.Helper()
	ctx := context.Background()
	agent := tc.agent
	var out callerRender

	cm := &corev1.ConfigMap{}
	if err := tc.cl.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: agent.Name + "-config"}, cm); err != nil {
		t.Fatal(err)
	}
	var managed struct {
		Model struct {
			APIKey string `json:"api_key"`
		} `json:"model"`
	}
	if err := yaml.Unmarshal([]byte(cm.Data[managedConfigKey]), &managed); err != nil {
		t.Fatal(err)
	}
	out.agentAPIKey = managed.Model.APIKey

	dep := &appsv1.Deployment{}
	if err := tc.cl.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: agent.Name + "-gateway"}, dep); err != nil {
		t.Fatal(err)
	}
	if c := keyCallerContainer(dep.Spec.Template.Spec, platformAgentContainerName); c != nil {
		out.agentEnv = keyCallerEnv(c.Env, agentInferenceKeyEnv)
	} else {
		t.Fatal("no platform-agent container")
	}
	if c := keyCallerContainer(dep.Spec.Template.Spec, a2aBridgeContainerName); c != nil {
		out.bridgeEnv = keyCallerEnv(c.Env, agentInferenceKeyEnv)
	}

	curator := &appsv1.Deployment{}
	if err := tc.cl.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: a2aCuratorName(agent)}, curator); err != nil {
		t.Fatal(err)
	}
	out.curatorEnv = keyCallerEnv(curator.Spec.Template.Spec.Containers[0].Env, curatorInferenceKeyEnv)

	pool := &appsv1.Deployment{}
	if err := tc.cl.Get(ctx, types.NamespacedName{Namespace: profileNamespaceName(agent), Name: profilePoolName("auditor", agentv1alpha1.AgentProfileTierRead)}, pool); err != nil {
		t.Fatal(err)
	}
	worker := keyCallerContainer(pool.Spec.Template.Spec, profileWorkerContainer)
	out.poolEnv = keyCallerEnv(worker.Env, workerInferenceKeyEnv)
	if base := keyCallerEnv(worker.Env, "ANTHROPIC_BASE_URL"); base != nil {
		out.poolBaseURL = base.Value
	}

	sandbox := &appsv1.StatefulSet{}
	if err := tc.cl.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: agent.Name + "-shell"}, sandbox); err != nil {
		t.Fatal(err)
	}
	for _, c := range append(sandbox.Spec.Template.Spec.InitContainers, sandbox.Spec.Template.Spec.Containers...) {
		for _, e := range c.Env {
			if e.ValueFrom != nil && e.ValueFrom.SecretKeyRef != nil && strings.Contains(e.ValueFrom.SecretKeyRef.Name, inferenceKeySecretInfix) {
				out.sandboxHasKey = true
			}
		}
	}
	return out
}

// wantKeyRef checks env reads key api-key of secret, not optionally.
func wantKeyRef(t *testing.T, who string, env *corev1.EnvVar, secret string) {
	t.Helper()
	if env == nil || env.ValueFrom == nil || env.ValueFrom.SecretKeyRef == nil {
		t.Errorf("%s: no Secret reference for its key: %+v", who, env)
		return
	}
	ref := env.ValueFrom.SecretKeyRef
	if env.Value != "" || ref.Name != secret || ref.Key != inferenceKeyDataKey || ref.Optional != nil {
		t.Errorf("%s: reference %+v (value %q), want key %s of %s, not optional", who, ref, env.Value, inferenceKeyDataKey, secret)
	}
}

func (r callerRender) wantPlaceholders(t *testing.T) {
	t.Helper()
	if r.agentAPIKey != agentPlaceholderAPIKey {
		t.Errorf("agent model.api_key = %q, want the placeholder %q", r.agentAPIKey, agentPlaceholderAPIKey)
	}
	for who, env := range map[string]*corev1.EnvVar{"agent": r.agentEnv, "bridge": r.bridgeEnv, "curator": r.curatorEnv, "warm pool": r.poolEnv} {
		if env != nil {
			t.Errorf("%s carries a key variable before the gateway serves it: %+v", who, env)
		}
	}
}

func (r callerRender) wantKeys(t *testing.T, agent *agentv1alpha1.PlatformAgent) {
	t.Helper()
	if r.agentAPIKey != agentInferenceKeyConfigRef {
		t.Errorf("agent model.api_key = %q, want %q", r.agentAPIKey, agentInferenceKeyConfigRef)
	}
	wantKeyRef(t, "agent", r.agentEnv, agent.Name+"-inference-key-agent")
	// The bridge is a Hermes caller of the gateway under its cli executor and
	// reads the same managed config.
	wantKeyRef(t, "bridge", r.bridgeEnv, agent.Name+"-inference-key-agent")
	wantKeyRef(t, "curator", r.curatorEnv, agent.Name+"-inference-key-curator")
	wantKeyRef(t, "warm pool", r.poolEnv, agent.Name+"-inference-key-tier-read")
}

func (r callerRender) wantFixed(t *testing.T, agent *agentv1alpha1.PlatformAgent) {
	t.Helper()
	if want := "http://" + inferenceGatewayServiceName + "." + agent.Namespace + ".svc.cluster.local"; r.poolBaseURL != want {
		t.Errorf("warm pool ANTHROPIC_BASE_URL = %q, want %q set explicitly", r.poolBaseURL, want)
	}
	if r.sandboxHasKey {
		t.Error("the shell sandbox carries an inference gateway key")
	}
}

// A caller gets its key only from the pass after one that found the keys
// served, and keeps it while the condition goes False or Unknown, as it does
// whenever the map changes or the gateway rolls; the sandbox never gets one,
// and a worker's base URL is set whether it sends a key or not.
func TestCallersSendTheirKeyOnlyOnceTheGatewayServesIt(t *testing.T) {
	tc := newCallerKeysTest(t)
	agent := tc.agent

	tc.reconcile(t)
	tc.reconcile(t)
	if c := tc.condition(t); c == nil || c.Status != metav1.ConditionFalse {
		t.Fatalf("condition %+v, want False while the pod refuses", c)
	}
	before := tc.render(t)
	before.wantPlaceholders(t)
	before.wantFixed(t, agent)
	for _, class := range []string{inferenceKeyClassAgent, inferenceKeyClassCurator, inferenceKeyClassSession} {
		s := readInferenceKey(t, tc.cl, installInferenceKeyClass(agent, class).secret)
		if _, ok := s.Annotations[inferenceKeyServedAnnotation]; ok {
			t.Errorf("the %s key is marked served before any pod admitted the map", class)
		}
	}

	// Served: this pass marks the keys and asks for the next one soon; its
	// own render stays on the placeholders, so the config and the env
	// agree within the pass.
	tc.admit.Store(true)
	result, err := tc.r.Reconcile(context.Background(), ctrl.Request{NamespacedName: tc.req})
	if err != nil {
		t.Fatal(err)
	}
	if result.RequeueAfter == 0 || result.RequeueAfter > inferenceKeysMarkedRecheck {
		t.Errorf("requeue %s after marking, want at most %s", result.RequeueAfter, inferenceKeysMarkedRecheck)
	}
	tc.render(t).wantPlaceholders(t)
	keys := allInferenceKeys(t, tc.cl, agent, "read", "unattended")
	for _, class := range []string{inferenceKeyClassAgent, inferenceKeyClassCurator, inferenceKeyClassSession} {
		s := readInferenceKey(t, tc.cl, installInferenceKeyClass(agent, class).secret)
		if got := s.Annotations[inferenceKeyServedAnnotation]; got != inferenceKeyDigest(keys[class]) {
			t.Errorf("the %s key's mark is %q, want its key's digest", class, got)
		}
	}
	for _, tier := range []string{"read", "unattended"} {
		s := readInferenceKey(t, tc.cl, tierInferenceKeyClass(agent, tier).secret)
		if s.Annotations[inferenceKeyServedAnnotation] != inferenceKeyDigest(keys[tierInferenceKeyClassName(tier)]) {
			t.Errorf("the %s tier's key is not marked", tier)
		}
	}

	tc.reconcile(t)
	after := tc.render(t)
	after.wantKeys(t, agent)
	after.wantFixed(t, agent)

	// The map moves on and a pod refuses the new probe key: the condition
	// goes False, and no caller is taken off its key.
	tc.admit.Store(false)
	tc.r.inferenceKeysServedFor.Delete(client.ObjectKeyFromObject(agent))
	probe := installInferenceKeyClass(agent, inferenceKeyClassProbe).secret
	s := readInferenceKey(t, tc.cl, probe)
	s.Data[inferenceKeyDataKey] = []byte("not-a-key")
	if err := tc.cl.Update(context.Background(), s); err != nil {
		t.Fatal(err)
	}
	tc.reconcile(t)
	if c := tc.condition(t); c == nil || c.Status != metav1.ConditionFalse {
		t.Fatalf("condition %+v, want False", c)
	}
	tc.render(t).wantKeys(t, agent)
}

// A key that changes loses its mark: a repaired key is sent only once a pass
// finds the map holding it served, and its callers go back to the
// placeholder meanwhile.
func TestARewrittenKeyIsNotSentUntilServedAgain(t *testing.T) {
	tc := newCallerKeysTest(t)
	agent := tc.agent
	tc.admit.Store(true)
	for range 3 {
		tc.reconcile(t)
	}
	tc.render(t).wantKeys(t, agent)

	curator := installInferenceKeyClass(agent, inferenceKeyClassCurator).secret
	s := readInferenceKey(t, tc.cl, curator)
	s.Data[inferenceKeyDataKey] = []byte("malformed")
	if err := tc.cl.Update(context.Background(), s); err != nil {
		t.Fatal(err)
	}
	tc.admit.Store(false)
	tc.r.inferenceKeysServedFor.Delete(client.ObjectKeyFromObject(agent))
	tc.reconcile(t) // repairs the key, still marked for the old one
	tc.reconcile(t)
	got := tc.render(t)
	if got.curatorEnv != nil {
		t.Errorf("the curator sends a repaired key no pod has admitted: %+v", got.curatorEnv)
	}
	wantKeyRef(t, "agent", got.agentEnv, agent.Name+"-inference-key-agent")

	tc.admit.Store(true)
	tc.reconcile(t)
	tc.reconcile(t)
	wantKeyRef(t, "curator", tc.render(t).curatorEnv, agent.Name+"-inference-key-curator")
}

// What a pass reads as marked: only the recorded Secret, holding a
// well-formed key, marked for that key, and only while the condition exists.
func TestOnlyARecordedSecretMarkedForItsKeyIsSent(t *testing.T) {
	ctx := context.Background()
	agent := inferenceKeysTestAgent()
	const goodKey = "sk-" + "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
	if !wellFormedInferenceKey(goodKey) {
		t.Fatal("test key is not well formed")
	}
	secret := func(class, uid, apiKey, mark string) *corev1.Secret {
		labels := a2aLabels(agent, inferenceKeyComponent)
		labels[inferenceKeyClassLabel] = class
		s := &corev1.Secret{ObjectMeta: metav1.ObjectMeta{
			Name: inferenceKeySecretName(agent, class), Namespace: agent.Namespace, UID: types.UID(uid), Labels: labels,
		}, Data: map[string][]byte{inferenceKeyDataKey: []byte(apiKey)}}
		if mark != "" {
			s.Annotations = map[string]string{inferenceKeyServedAnnotation: mark}
		}
		return s
	}
	agent.Status.InferenceKeys = &agentv1alpha1.InferenceKeysStatus{SecretUIDs: map[string]string{
		inferenceKeySecretName(agent, inferenceKeyClassAgent):   "uid-agent",
		inferenceKeySecretName(agent, inferenceKeyClassCurator): "uid-curator",
		inferenceKeySecretName(agent, inferenceKeyClassSession): "uid-session",
	}}
	agent.Status.Conditions = []metav1.Condition{{Type: inferenceKeysServedCondition, Status: metav1.ConditionFalse, Reason: inferenceKeysReasonNotServed}}
	objs := []client.Object{
		secret(inferenceKeyClassAgent, "uid-agent", goodKey, inferenceKeyDigest(goodKey)),
		// Marked, but not the recorded Secret.
		secret(inferenceKeyClassCurator, "uid-planted", goodKey, inferenceKeyDigest(goodKey)),
		// Recorded, marked for another key.
		secret(inferenceKeyClassSession, "uid-session", goodKey, inferenceKeyDigest("sk-other")),
	}
	r, _, _ := a2aGateTestReconciler(t, agent, objs...)
	got, err := r.servedInferenceKeyCallers(ctx, agent)
	if err != nil {
		t.Fatal(err)
	}
	if !got.sends(inferenceKeyClassAgent) || got.sends(inferenceKeyClassCurator) || got.sends(inferenceKeyClassSession) {
		t.Errorf("sent %v, want the agent class alone", got.secrets)
	}

	noCondition := agent.DeepCopy()
	noCondition.Status.Conditions = nil
	if got, _ := r.servedInferenceKeyCallers(ctx, noCondition); got.sends(inferenceKeyClassAgent) {
		t.Error("a key is sent with no InferenceKeysServed condition, as when the gateway has gone")
	}
	if got := (inferenceKeyCallers{}); got.sends(inferenceKeyClassAgent) {
		t.Error("the zero value sends a key")
	}
}

// A failed read in the agent's namespace fails the pass rather than taking
// the callers off their keys; one in the profile namespace costs that tier
// its key on the pass and nothing else.
func TestAFailedMarkReadFailsThePassOnlyInTheAgentsNamespace(t *testing.T) {
	ctx := context.Background()
	agent := inferenceKeysTestAgent()
	agent.Status.InferenceKeys = &agentv1alpha1.InferenceKeysStatus{SecretUIDs: map[string]string{
		inferenceKeySecretName(agent, inferenceKeyClassAgent):                  "uid-agent",
		inferenceKeySecretName(agent, tierInferenceKeyClassName("unattended")): "uid-tier",
	}}
	agent.Status.Conditions = []metav1.Condition{{Type: inferenceKeysServedCondition, Status: metav1.ConditionTrue, Reason: inferenceKeysReasonServed}}
	failIn := ""
	r, _, _ := a2aGateTestReconciler(t, agent)
	cl := interceptor.NewClient(r.Client.(client.WithWatch), interceptor.Funcs{
		Get: func(ctx context.Context, c client.WithWatch, key client.ObjectKey, obj client.Object, opts ...client.GetOption) error {
			if _, ok := obj.(*corev1.Secret); ok && key.Namespace == failIn {
				return apierrors.NewServiceUnavailable("etcd is sad")
			}
			return c.Get(ctx, key, obj, opts...)
		},
	})
	r.Client = cl

	failIn = profileNamespaceName(agent)
	if _, err := r.servedInferenceKeyCallers(ctx, agent); err != nil {
		t.Errorf("a profile namespace failure failed the pass: %v", err)
	}
	failIn = agent.Namespace
	if _, err := r.servedInferenceKeyCallers(ctx, agent); err == nil || !apierrors.IsServiceUnavailable(errors.Unwrap(err)) {
		t.Errorf("an agent namespace failure: %v, want the read's error", err)
	}
}

// Every Job template carries its own tier's key once sent, so the dispatch
// config and the policy's param, one object, carry the same reference; an
// unsent tier's carries none.
func TestJobTemplatesCarryTheirTiersKey(t *testing.T) {
	t.Setenv(a2aDispatcherJobPolicyEnvVar, "true")
	agent := inferenceKeysTestAgent()
	profile := testAgentProfile(agent.Namespace, "auditor", func(p *agentv1alpha1.AgentProfile) {
		p.Spec.AllowedTiers = []string{agentv1alpha1.AgentProfileTierRead, agentv1alpha1.AgentProfileTierUnattended}
	})
	templates := dispatchJobTemplates(agent, []agentv1alpha1.AgentProfile{profile}, false, "")
	if len(templates["auditor"]) != 2 {
		t.Fatalf("templates %v, want one per tier", templates)
	}
	unattended := tierInferenceKeyClass(agent, agentv1alpha1.AgentProfileTierUnattended)
	callers := inferenceKeyCallers{secrets: map[string]types.NamespacedName{unattended.name: unattended.secret}}
	withTierInferenceKeys(templates, callers)
	for name, tmpl := range templates["auditor"] {
		env := keyCallerEnv(keyCallerContainer(tmpl.Spec, profileWorkerContainer).Env, workerInferenceKeyEnv)
		switch tier := tmpl.Labels[agentProfileTierLabel]; tier {
		case agentv1alpha1.AgentProfileTierUnattended:
			wantKeyRef(t, name, env, agent.Name+"-inference-key-tier-unattended")
		default:
			if env != nil {
				t.Errorf("%s (tier %s) carries a key it isn't sent: %+v", name, tier, env)
			}
		}
		if keyCallerEnv(keyCallerContainer(tmpl.Spec, profileWorkerContainer).Env, "ANTHROPIC_BASE_URL") == nil {
			t.Errorf("%s sets no ANTHROPIC_BASE_URL", name)
		}
	}
}

// The webhook's reservation and the operator's names are the same names.
func TestTheWebhookReservesTheOperatorsKeySecretNames(t *testing.T) {
	agent := inferenceKeysTestAgent()
	if callerKeysSecretName != agentv1alpha1.InferenceCallerKeysSecretName {
		t.Errorf("map Secret %q, the webhook reserves %q", callerKeysSecretName, agentv1alpha1.InferenceCallerKeysSecretName)
	}
	for _, class := range append(append([]string{inferenceKeyClassProbe}, installInferenceKeyClasses...), tierInferenceKeyClassName("read")) {
		if name := inferenceKeySecretName(agent, class); !agentv1alpha1.IsInferenceKeySecretName(agent.Name, name) {
			t.Errorf("the webhook doesn't reserve %s", name)
		}
	}
}

// A plugin's entry of the same name gives way to the operator's, so the
// agent container never holds the name twice (server-side apply refuses a
// duplicate env key).
func TestTheAgentKeyVariableOutranksAPlugins(t *testing.T) {
	agent := inferenceKeysTestAgent()
	plugin := &agentv1alpha1.AgentPlugin{
		ObjectMeta: metav1.ObjectMeta{Name: "p", Namespace: agent.Namespace},
		Spec:       agentv1alpha1.AgentPluginSpec{Env: []corev1.EnvVar{{Name: agentInferenceKeyEnv, Value: "chosen-by-a-plugin"}}},
	}
	plugin.SetGroupVersionKind(schema.GroupVersionKind{Group: agentv1alpha1.GroupVersion.Group, Version: agentv1alpha1.GroupVersion.Version, Kind: "AgentPlugin"})
	c := installInferenceKeyClass(agent, inferenceKeyClassAgent)
	opts := renderOptions{inferenceKeys: inferenceKeyCallers{secrets: map[string]types.NamespacedName{c.name: c.secret}}}
	tmpl := buildPodTemplateSpec(agent, "", "", "", "", []*agentv1alpha1.AgentPlugin{plugin}, opts)
	ctr := keyCallerContainer(tmpl.Spec, platformAgentContainerName)
	n := 0
	for _, e := range ctr.Env {
		if e.Name == agentInferenceKeyEnv {
			n++
		}
	}
	if n != 1 {
		t.Fatalf("%d %s entries, want 1", n, agentInferenceKeyEnv)
	}
	wantKeyRef(t, "agent", keyCallerEnv(ctr.Env, agentInferenceKeyEnv), c.secret.Name)

	plain := buildPodTemplateSpec(agent, "", "", "", "", nil, renderOptions{})
	if keyCallerEnv(keyCallerContainer(plain.Spec, platformAgentContainerName).Env, agentInferenceKeyEnv) != nil {
		t.Error("the default render carries the agent key variable")
	}
}

// The dispatcher step gives the policy's param its tier's key once sent:
// the param and the dispatch config's rendering are the same template, so
// the Jobs the dispatcher copies from it carry the reference the policy
// compares against.
func TestTheDispatchersParamsCarryTheTiersKey(t *testing.T) {
	agent := a2aTestAgent()
	r, c := jobTemplateTestReconciler(t, agent)
	unattended := tierInferenceKeyClass(agent, agentv1alpha1.AgentProfileTierUnattended)
	ctx := withInferenceKeyCallers(context.Background(), inferenceKeyCallers{secrets: map[string]types.NamespacedName{unattended.name: unattended.secret}})
	profiles := []agentv1alpha1.AgentProfile{unattendedProfile(agent, "tse")}
	for pass := 1; pass <= 2; pass++ {
		if profileErr, err := r.reconcileA2ADispatcher(ctx, agent, profiles, true); err != nil || profileErr != nil {
			t.Fatalf("pass %d: %v / %v", pass, err, profileErr)
		}
	}
	pns := profileNamespaceName(agent)
	for name, want := range map[string]string{
		dispatchJobTemplatePrefix + "tse":          unattended.secret.Name,
		dispatchJobTierTemplatePrefix + "tse.read": "",
	} {
		var param agentv1alpha1.DispatchJobTemplate
		if err := c.Get(ctx, types.NamespacedName{Namespace: pns, Name: name}, &param); err != nil {
			t.Fatalf("param %s: %v", name, err)
		}
		held, ok := dispatchJobParamTemplate(&param)
		if !ok {
			t.Fatalf("%s holds no template", name)
		}
		env := keyCallerEnv(keyCallerContainer(held.Spec, profileWorkerContainer).Env, workerInferenceKeyEnv)
		if want == "" {
			if env != nil {
				t.Errorf("%s carries a key its tier isn't sent: %+v", name, env)
			}
			continue
		}
		wantKeyRef(t, name, env, want)
	}
}
