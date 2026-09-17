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
	"reflect"
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// secretEnvRefEnv is one env var reading a key out of a Secret, the shape every
// credential on the gateway pod takes.
func secretEnvRefEnv(name, secretName, key string) corev1.EnvVar {
	return corev1.EnvVar{
		Name: name,
		ValueFrom: &corev1.EnvVarSource{
			SecretKeyRef: &corev1.SecretKeySelector{
				LocalObjectReference: corev1.LocalObjectReference{Name: secretName},
				Key:                  key,
			},
		},
	}
}

func secretHashTestAgent() *agentv1alpha1.PlatformAgent {
	return &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "test-ns"},
	}
}

// secretHashTestPodSpec consumes two keys of one Secret and one key of another,
// the way a CR that supplies its own SecretKeyRef makes the gateway do.
func secretHashTestPodSpec() *corev1.PodSpec {
	return &corev1.PodSpec{
		InitContainers: []corev1.Container{{
			Name: "agent-api-auth",
			Env:  []corev1.EnvVar{secretEnvRefEnv("API_SERVER_EXTERNAL_KEY", "platform-agent-secrets", "API_SERVER_KEY")},
		}},
		Containers: []corev1.Container{{
			Name: "platform-agent",
			Env: []corev1.EnvVar{
				secretEnvRefEnv("SESSION_KV_API_KEY", "platform-agent-secrets", "SESSION_KV_API_KEY"),
				secretEnvRefEnv("SLACK_BOT_TOKEN", "team-slack", "bot-token"),
			},
		}},
	}
}

func secretHashTestSecret(name string, data map[string][]byte) *corev1.Secret {
	return &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: "test-ns"},
		Data:       data,
	}
}

// secretHashTestReconciler is a reconciler over a fake cluster holding objects.
// APIReader is left nil so secretEnvHash falls back to the client, which is the
// path a test can drive; the production reader is asserted separately by the
// comment on secretEnvHash, not here.
func secretHashTestReconciler(objects ...client.Object) (*PlatformAgentReconciler, client.Client) {
	scheme := setupScheme()
	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(objects...).Build()
	return &PlatformAgentReconciler{Client: cl, Scheme: scheme}, cl
}

func TestPodSpecSecretEnvRefsFindsEveryEnvironmentSourceAndNoMount(t *testing.T) {
	spec := &corev1.PodSpec{
		InitContainers: []corev1.Container{{
			Name: "agent-api-auth",
			Env:  []corev1.EnvVar{secretEnvRefEnv("API_SERVER_EXTERNAL_KEY", "platform-agent-secrets", "API_SERVER_KEY")},
		}},
		Containers: []corev1.Container{{
			Name: "platform-agent",
			Env: []corev1.EnvVar{
				// The same ref the init container has: one entry, not two.
				secretEnvRefEnv("API_SERVER_EXTERNAL_KEY", "platform-agent-secrets", "API_SERVER_KEY"),
				secretEnvRefEnv("SESSION_KV_API_KEY", "platform-agent-secrets", "SESSION_KV_API_KEY"),
				secretEnvRefEnv("SLACK_BOT_TOKEN", "team-slack", "bot-token"),
				{Name: "PLAIN", Value: "not-secret-material"},
				{Name: "FROM_CONFIGMAP", ValueFrom: &corev1.EnvVarSource{
					ConfigMapKeyRef: &corev1.ConfigMapKeySelector{
						LocalObjectReference: corev1.LocalObjectReference{Name: "settings"},
						Key:                  "mode",
					},
				}},
			},
			EnvFrom: []corev1.EnvFromSource{{
				SecretRef: &corev1.SecretEnvSource{LocalObjectReference: corev1.LocalObjectReference{Name: "bulk-env"}},
			}},
		}},
		// A mounted Secret is not environment: the kubelet refreshes it in place,
		// so rolling the pod for it would be a restart the change did not need.
		Volumes: []corev1.Volume{{
			Name:         "authorized-keys",
			VolumeSource: corev1.VolumeSource{Secret: &corev1.SecretVolumeSource{SecretName: "sandbox-keys"}},
		}},
	}

	want := []secretEnvRef{
		{name: "bulk-env"},
		{name: "platform-agent-secrets", key: "API_SERVER_KEY"},
		{name: "platform-agent-secrets", key: "SESSION_KV_API_KEY"},
		{name: "team-slack", key: "bot-token"},
	}
	if got := podSpecSecretEnvRefs(spec); !reflect.DeepEqual(got, want) {
		t.Errorf("podSpecSecretEnvRefs() = %+v, want %+v", got, want)
	}
}

func TestPodSpecSecretEnvRefsIsEmptyWhenNothingReadsASecret(t *testing.T) {
	spec := &corev1.PodSpec{Containers: []corev1.Container{{
		Name: "platform-agent",
		Env:  []corev1.EnvVar{{Name: "PLAIN", Value: "not-secret-material"}},
	}}}
	if got := podSpecSecretEnvRefs(spec); len(got) != 0 {
		t.Errorf("podSpecSecretEnvRefs() = %+v, want none", got)
	}
	if got := podSpecSecretEnvRefs(nil); got != nil {
		t.Errorf("podSpecSecretEnvRefs(nil) = %+v, want nil", got)
	}
}

// The digest is an annotation on a pod template: one that moved on its own
// would roll the gateway on every reconcile.
func TestSecretEnvHashIsStableAcrossCalls(t *testing.T) {
	agent := secretHashTestAgent()
	r, _ := secretHashTestReconciler(
		secretHashTestSecret("platform-agent-secrets", map[string][]byte{
			"API_SERVER_KEY":     []byte("first"),
			"SESSION_KV_API_KEY": []byte("second"),
			"SESSION_KV_SALT":    []byte("third"),
		}),
		secretHashTestSecret("team-slack", map[string][]byte{"bot-token": []byte("xoxb")}),
	)
	ctx := context.Background()

	first, err := r.secretEnvHash(ctx, agent, secretHashTestPodSpec())
	if err != nil {
		t.Fatalf("secretEnvHash: %v", err)
	}
	if first == "" {
		t.Fatal("secretEnvHash returned no digest for a pod that reads three keys")
	}
	// Ten calls, because the instability this guards against is Go's randomised
	// map iteration order, which one repeat can miss.
	for i := 0; i < 10; i++ {
		again, err := r.secretEnvHash(ctx, agent, secretHashTestPodSpec())
		if err != nil {
			t.Fatalf("secretEnvHash repeat %d: %v", i, err)
		}
		if again != first {
			t.Fatalf("digest moved on repeat %d without the cluster changing: %s then %s", i, first, again)
		}
	}
}

func TestSecretEnvHashChangesWhenAReferencedValueChanges(t *testing.T) {
	agent := secretHashTestAgent()
	r, cl := secretHashTestReconciler(
		secretHashTestSecret("platform-agent-secrets", map[string][]byte{
			"API_SERVER_KEY":     []byte("first"),
			"SESSION_KV_API_KEY": []byte("second"),
		}),
		secretHashTestSecret("team-slack", map[string][]byte{"bot-token": []byte("xoxb")}),
	)
	ctx := context.Background()

	before, err := r.secretEnvHash(ctx, agent, secretHashTestPodSpec())
	if err != nil {
		t.Fatalf("secretEnvHash before: %v", err)
	}

	rotated := &corev1.Secret{}
	if err := cl.Get(ctx, types.NamespacedName{Name: "platform-agent-secrets", Namespace: "test-ns"}, rotated); err != nil {
		t.Fatalf("read the Secret back: %v", err)
	}
	rotated.Data["API_SERVER_KEY"] = []byte("rotated")
	if err := cl.Update(ctx, rotated); err != nil {
		t.Fatalf("rotate the key: %v", err)
	}

	after, err := r.secretEnvHash(ctx, agent, secretHashTestPodSpec())
	if err != nil {
		t.Fatalf("secretEnvHash after: %v", err)
	}
	if after == before {
		t.Errorf("digest %s survived a rotated key, so nothing would roll the pod", before)
	}
}

// A key the pod does not read is not the pod's environment, and restarting the
// agent for it is a gratuitous outage.
func TestSecretEnvHashIgnoresAKeyThePodDoesNotRead(t *testing.T) {
	agent := secretHashTestAgent()
	r, cl := secretHashTestReconciler(
		secretHashTestSecret("platform-agent-secrets", map[string][]byte{
			"API_SERVER_KEY":     []byte("first"),
			"SESSION_KV_API_KEY": []byte("second"),
			"TEAMS_APP_PASSWORD": []byte("unused-here"),
		}),
		secretHashTestSecret("team-slack", map[string][]byte{"bot-token": []byte("xoxb")}),
	)
	ctx := context.Background()

	before, err := r.secretEnvHash(ctx, agent, secretHashTestPodSpec())
	if err != nil {
		t.Fatalf("secretEnvHash before: %v", err)
	}

	untouched := &corev1.Secret{}
	if err := cl.Get(ctx, types.NamespacedName{Name: "platform-agent-secrets", Namespace: "test-ns"}, untouched); err != nil {
		t.Fatalf("read the Secret back: %v", err)
	}
	untouched.Data["TEAMS_APP_PASSWORD"] = []byte("rotated-but-unreferenced")
	if err := cl.Update(ctx, untouched); err != nil {
		t.Fatalf("rotate the unreferenced key: %v", err)
	}

	after, err := r.secretEnvHash(ctx, agent, secretHashTestPodSpec())
	if err != nil {
		t.Fatalf("secretEnvHash after: %v", err)
	}
	if after != before {
		t.Errorf("digest moved for a key the pod never reads: %s then %s", before, after)
	}
}

// An install whose Slack credentials are created after the agent still has to
// pick them up, so absence is a digest rather than an error or a skip.
func TestSecretEnvHashTracksASecretThatAppearsLater(t *testing.T) {
	agent := secretHashTestAgent()
	r, cl := secretHashTestReconciler(
		secretHashTestSecret("platform-agent-secrets", map[string][]byte{
			"API_SERVER_KEY":     []byte("first"),
			"SESSION_KV_API_KEY": []byte("second"),
		}),
	)
	ctx := context.Background()

	missing, err := r.secretEnvHash(ctx, agent, secretHashTestPodSpec())
	if err != nil {
		t.Fatalf("secretEnvHash with the Secret absent: %v", err)
	}
	if missing == "" {
		t.Fatal("an absent Secret produced no digest, so its creation would not roll the pod")
	}

	if err := cl.Create(ctx, secretHashTestSecret("team-slack", map[string][]byte{"bot-token": []byte("xoxb")})); err != nil {
		t.Fatalf("create the Secret: %v", err)
	}
	present, err := r.secretEnvHash(ctx, agent, secretHashTestPodSpec())
	if err != nil {
		t.Fatalf("secretEnvHash with the Secret present: %v", err)
	}
	if present == missing {
		t.Error("the digest did not move when the Secret appeared")
	}
}

func TestSecretEnvHashDistinguishesAnAbsentKeyFromAnAbsentSecret(t *testing.T) {
	agent := secretHashTestAgent()
	ctx := context.Background()

	withoutKey, _ := secretHashTestReconciler(
		secretHashTestSecret("platform-agent-secrets", map[string][]byte{
			"API_SERVER_KEY":     []byte("first"),
			"SESSION_KV_API_KEY": []byte("second"),
		}),
		secretHashTestSecret("team-slack", map[string][]byte{"other": []byte("not-the-key")}),
	)
	keyAbsent, err := withoutKey.secretEnvHash(ctx, agent, secretHashTestPodSpec())
	if err != nil {
		t.Fatalf("secretEnvHash with the key absent: %v", err)
	}

	withoutSecret, _ := secretHashTestReconciler(
		secretHashTestSecret("platform-agent-secrets", map[string][]byte{
			"API_SERVER_KEY":     []byte("first"),
			"SESSION_KV_API_KEY": []byte("second"),
		}),
	)
	secretAbsent, err := withoutSecret.secretEnvHash(ctx, agent, secretHashTestPodSpec())
	if err != nil {
		t.Fatalf("secretEnvHash with the Secret absent: %v", err)
	}
	if keyAbsent == secretAbsent {
		t.Error("an empty Secret and a missing Secret digest the same, so creating one without the key looks like a rotation")
	}
}

// secretReadFailsReconciler is a reconciler whose Secret reads all fail and
// whose other reads work, so a test can drive the recovery path without
// breaking the live-workload read it depends on.
func secretReadFailsReconciler(objects ...client.Object) *PlatformAgentReconciler {
	scheme := setupScheme()
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(objects...).
		WithInterceptorFuncs(interceptor.Funcs{
			Get: func(ctx context.Context, cl client.WithWatch, key client.ObjectKey, obj client.Object, opts ...client.GetOption) error {
				if _, isSecret := obj.(*corev1.Secret); isSecret {
					return apierrors.NewInternalError(errors.New("the API server is having a moment"))
				}
				return cl.Get(ctx, key, obj, opts...)
			},
		}).
		Build()
	return &PlatformAgentReconciler{Client: cl, Scheme: scheme}
}

// secretHashTestDeployment is a live workload carrying a digest, the thing the
// recovery path reads when it cannot compute a fresh one.
func secretHashTestDeployment(digest string) *appsv1.Deployment {
	deployment := &appsv1.Deployment{
		ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "test-ns"},
		Spec: appsv1.DeploymentSpec{
			Selector: &metav1.LabelSelector{MatchLabels: map[string]string{"app": "test-agent"}},
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{Labels: map[string]string{"app": "test-agent"}},
				Spec:       *secretHashTestPodSpec(),
			},
		},
	}
	if digest != "" {
		deployment.Spec.Template.Annotations = map[string]string{secretEnvHashAnnotation: digest}
	}
	return deployment
}

// Guessing a digest on a read error would roll the gateway on an API blip,
// which is the outage this change exists to avoid causing.
func TestSecretEnvHashFailsRatherThanGuessingOnAReadError(t *testing.T) {
	r := secretReadFailsReconciler()
	if _, err := r.secretEnvHash(context.Background(), secretHashTestAgent(), secretHashTestPodSpec()); err == nil {
		t.Fatal("a failed read produced a digest instead of an error")
	}
}

// The apply is server-side and the operator owns this field, so rendering the
// template without the annotation deletes it and rolls the pod. A failed read
// has to render what the last good pass rendered.
func TestStampCarriesTheLiveDigestForwardWhenTheSecretCannotBeRead(t *testing.T) {
	live := secretHashTestDeployment("the-digest-from-the-last-good-pass")
	r := secretReadFailsReconciler(live)

	rendered := secretHashTestDeployment("")
	if err := r.stampSecretEnvHash(context.Background(), secretHashTestAgent(), rendered, &rendered.Spec.Template); err != nil {
		t.Fatalf("a failed read stopped the pass instead of keeping the previous digest: %v", err)
	}
	if got := rendered.Spec.Template.Annotations[secretEnvHashAnnotation]; got != "the-digest-from-the-last-good-pass" {
		t.Errorf("the previous digest was not carried forward, so the apply would delete it and roll the pod: %q", got)
	}
}

// Nothing to carry forward — the first apply — leaves the template unannotated,
// which deletes nothing and rolls nothing.
func TestStampLeavesTheTemplateAloneWhenThereIsNoDigestToCarryForward(t *testing.T) {
	r := secretReadFailsReconciler()
	rendered := secretHashTestDeployment("")
	if err := r.stampSecretEnvHash(context.Background(), secretHashTestAgent(), rendered, &rendered.Spec.Template); err != nil {
		t.Fatalf("a failed read with nothing live stopped the pass: %v", err)
	}
	if got, stamped := rendered.Spec.Template.Annotations[secretEnvHashAnnotation]; stamped {
		t.Errorf("a digest appeared out of a failed read: %q", got)
	}
}

func TestStampSecretEnvHashAnnotatesWithoutDisturbingTheRest(t *testing.T) {
	agent := secretHashTestAgent()
	r, _ := secretHashTestReconciler(
		secretHashTestSecret("platform-agent-secrets", map[string][]byte{
			"API_SERVER_KEY":     []byte("first"),
			"SESSION_KV_API_KEY": []byte("second"),
		}),
		secretHashTestSecret("team-slack", map[string][]byte{"bot-token": []byte("xoxb")}),
	)

	template := &corev1.PodTemplateSpec{
		ObjectMeta: metav1.ObjectMeta{Annotations: map[string]string{"kubeagents.x-k8s.io/config-hash": "abcd1234"}},
		Spec:       *secretHashTestPodSpec(),
	}
	if err := r.stampSecretEnvHash(context.Background(), agent, nil, template); err != nil {
		t.Fatalf("stampSecretEnvHash: %v", err)
	}
	if template.Annotations[secretEnvHashAnnotation] == "" {
		t.Error("no digest was stamped on a template that reads three keys")
	}
	if got := template.Annotations["kubeagents.x-k8s.io/config-hash"]; got != "abcd1234" {
		t.Errorf("config-hash was disturbed: %q", got)
	}
}

func TestStampSecretEnvHashAddsNothingWhenNoEnvironmentReadsASecret(t *testing.T) {
	r, _ := secretHashTestReconciler()
	template := &corev1.PodTemplateSpec{
		Spec: corev1.PodSpec{Containers: []corev1.Container{{Name: "platform-agent"}}},
	}
	if err := r.stampSecretEnvHash(context.Background(), secretHashTestAgent(), nil, template); err != nil {
		t.Fatalf("stampSecretEnvHash: %v", err)
	}
	if _, stamped := template.Annotations[secretEnvHashAnnotation]; stamped {
		t.Errorf("stamped a digest of nothing: %v", template.Annotations)
	}
}

// The wiring, end to end through Reconcile: rotating a key the gateway reads
// has to change the rendered Deployment, which is what makes Kubernetes roll
// the pod. Reverting the caller in reconcileWorkload has to fail this.
func TestRotatingAGatewaySecretChangesTheRenderedDeployment(t *testing.T) {
	scheme := setupScheme()
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "test-ns"},
	}
	secret := secretHashTestSecret("platform-agent-secrets", map[string][]byte{
		"API_SERVER_KEY":     []byte("before-rotation"),
		"SESSION_KV_API_KEY": []byte("session-key"),
		"SESSION_KV_SALT":    []byte("salt"),
	})
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent, secret).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	req := ctrl.Request{NamespacedName: types.NamespacedName{Name: "test-agent", Namespace: "test-ns"}}
	ctx := context.Background()

	// First pass adds the finalizer; the second renders the workload.
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("Reconcile 1: %v", err)
	}
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("Reconcile 2: %v", err)
	}

	gatewayKey := types.NamespacedName{Name: "test-agent-gateway", Namespace: "test-ns"}
	dep := &appsv1.Deployment{}
	if err := cl.Get(ctx, gatewayKey, dep); err != nil {
		t.Fatalf("read the gateway Deployment: %v", err)
	}
	before := dep.Spec.Template.Annotations[secretEnvHashAnnotation]
	if before == "" {
		t.Fatalf("the rendered gateway carries no %s; nothing would roll it", secretEnvHashAnnotation)
	}

	// A pass that changes nothing must not roll the pod either.
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("Reconcile 3: %v", err)
	}
	if err := cl.Get(ctx, gatewayKey, dep); err != nil {
		t.Fatalf("re-read the gateway Deployment: %v", err)
	}
	if unchanged := dep.Spec.Template.Annotations[secretEnvHashAnnotation]; unchanged != before {
		t.Errorf("the digest moved on an idle pass: %s then %s", before, unchanged)
	}

	rotated := &corev1.Secret{}
	if err := cl.Get(ctx, types.NamespacedName{Name: "platform-agent-secrets", Namespace: "test-ns"}, rotated); err != nil {
		t.Fatalf("read the Secret back: %v", err)
	}
	rotated.Data["API_SERVER_KEY"] = []byte("after-rotation")
	if err := cl.Update(ctx, rotated); err != nil {
		t.Fatalf("rotate the key: %v", err)
	}

	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("Reconcile 4: %v", err)
	}
	if err := cl.Get(ctx, gatewayKey, dep); err != nil {
		t.Fatalf("re-read the gateway Deployment: %v", err)
	}
	after := dep.Spec.Template.Annotations[secretEnvHashAnnotation]
	if after == before {
		t.Errorf("the rendered gateway is unchanged after the rotation (%s), so the running pod keeps the old key", before)
	}
}

// Nothing enqueues this agent when its Secret changes, so a healthy pass has to
// ask to be woken; without the requeue the digest above is recomputed only when
// something unrelated happens.
func TestAHealthyPassAsksToBeWokenForTheSecretReRead(t *testing.T) {
	scheme := setupScheme()
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "test-ns"},
	}
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		// The sandbox keys Secret keeps the agent out of Degraded: that path
		// requeues at 30s and would mask the interval under test.
		WithObjects(
			agent,
			shellSandboxKeysSecret(agent),
			secretHashTestSecret("platform-agent-secrets", map[string][]byte{"API_SERVER_KEY": []byte("k")}),
		).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	req := ctrl.Request{NamespacedName: types.NamespacedName{Name: "test-agent", Namespace: "test-ns"}}
	ctx := context.Background()

	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("Reconcile 1: %v", err)
	}
	result, err := r.Reconcile(ctx, req)
	if err != nil {
		t.Fatalf("Reconcile 2: %v", err)
	}
	if result.RequeueAfter != secretEnvReprobeInterval {
		t.Errorf("RequeueAfter = %v, want %v", result.RequeueAfter, secretEnvReprobeInterval)
	}
}

// The third call site, and the one the gateway tests cannot reach: Slack and
// Teams credentials are on the credential proxy, not the gateway, and they are
// the credentials an install rotates most often.
//
// Slack has to be enabled for this to test anything. The proxy names a Secret
// only when an integration is configured (buildCredentialProxyEnv), so on a
// default fixture it is stamped with nothing and reverting the call in
// reconcileCredentialProxy leaves the suite green — which is exactly what
// review found.
func TestRotatingASlackTokenChangesTheRenderedCredentialProxy(t *testing.T) {
	scheme := setupScheme()
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "test-ns"},
		Spec: agentv1alpha1.PlatformAgentSpec{
			Integration: &agentv1alpha1.PlatformAgentIntegrationSpec{
				Slack: &agentv1alpha1.SlackSpec{Enabled: ptr.To(true)},
			},
		},
	}
	secret := secretHashTestSecret("platform-agent-secrets", map[string][]byte{
		"SLACK_BOT_TOKEN": []byte("xoxb-before-rotation"),
		"SLACK_APP_TOKEN": []byte("xapp-unchanged"),
		"API_SERVER_KEY":  []byte("gateway-key"),
	})
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent, secret).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	req := ctrl.Request{NamespacedName: types.NamespacedName{Name: "test-agent", Namespace: "test-ns"}}
	ctx := context.Background()

	// First pass adds the finalizer; the second renders the workloads.
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("Reconcile 1: %v", err)
	}
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("Reconcile 2: %v", err)
	}

	proxyKey := types.NamespacedName{Name: "test-agent-credential-proxy", Namespace: "test-ns"}
	proxy := &appsv1.Deployment{}
	if err := cl.Get(ctx, proxyKey, proxy); err != nil {
		t.Fatalf("read the credential-proxy Deployment: %v", err)
	}
	if !podReadsSecretKey(&proxy.Spec.Template.Spec, "SLACK_BOT_TOKEN") {
		t.Fatal("the fixture no longer gives the proxy a Slack secretKeyRef, so this proves nothing")
	}
	before := proxy.Spec.Template.Annotations[secretEnvHashAnnotation]
	if before == "" {
		t.Fatalf("the rendered credential proxy carries no %s; a rotated Slack token would reach nothing", secretEnvHashAnnotation)
	}

	rotated := &corev1.Secret{}
	if err := cl.Get(ctx, types.NamespacedName{Name: "platform-agent-secrets", Namespace: "test-ns"}, rotated); err != nil {
		t.Fatalf("read the Secret back: %v", err)
	}
	rotated.Data["SLACK_BOT_TOKEN"] = []byte("xoxb-after-rotation")
	if err := cl.Update(ctx, rotated); err != nil {
		t.Fatalf("rotate the token: %v", err)
	}

	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("Reconcile 3: %v", err)
	}
	if err := cl.Get(ctx, proxyKey, proxy); err != nil {
		t.Fatalf("re-read the credential-proxy Deployment: %v", err)
	}
	if after := proxy.Spec.Template.Annotations[secretEnvHashAnnotation]; after == before {
		t.Errorf("the rendered credential proxy is unchanged after the rotation (%s), so the running pod keeps the old Slack token", before)
	}
}

// podReadsSecretKey reports whether any container takes this environment
// variable from a Secret, so a fixture can say so rather than assume it.
func podReadsSecretKey(spec *corev1.PodSpec, name string) bool {
	for _, containers := range [][]corev1.Container{spec.InitContainers, spec.Containers} {
		for _, container := range containers {
			for _, env := range container.Env {
				if env.Name == name && env.ValueFrom != nil && env.ValueFrom.SecretKeyRef != nil {
					return true
				}
			}
		}
	}
	return false
}

// secretEnvReprobeInterval has to stay different from rbacReprobeInterval:
// TestReconcileReportsAnOutOfDateClusterRoleAndKeepsGoing reads the requeue to
// tell that the RBAC poll has stopped, and two reasons to come back that share a duration cannot
// be told apart from the result. Sharing otelRediscoverAfter is deliberate and
// is the case this does not object to.
func TestTheSecretAndRBACReprobeIntervalsStayDistinguishable(t *testing.T) {
	if secretEnvReprobeInterval == rbacReprobeInterval {
		t.Errorf("secretEnvReprobeInterval and rbacReprobeInterval are both %v", secretEnvReprobeInterval)
	}
}

// reconcileWorkload renders a StatefulSet instead of a Deployment above one
// replica with RWO storage, and stamps it from a second call site. Without this
// the Deployment test alone passes with that call reverted, and a multi-replica
// install keeps the old key.
func TestTheStatefulSetGatewayIsStampedToo(t *testing.T) {
	scheme := setupScheme()
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "test-ns"},
		Spec: agentv1alpha1.PlatformAgentSpec{
			Deployment: &agentv1alpha1.DeploymentSpec{
				Availability: &agentv1alpha1.AvailabilitySpec{Replicas: ptr.To(int32(2))},
				Storages: []agentv1alpha1.StorageSpec{{
					Name:        "scratch",
					AccessModes: []corev1.PersistentVolumeAccessMode{corev1.ReadWriteOnce},
					StorageSize: "5Gi",
					MountPath:   "/scratch",
				}},
			},
		},
	}
	if !useStatefulSet(agent) {
		t.Fatal("this fixture no longer renders a StatefulSet, so it does not reach the second call site")
	}
	secret := secretHashTestSecret("platform-agent-secrets", map[string][]byte{
		"API_SERVER_KEY":     []byte("before-rotation"),
		"SESSION_KV_API_KEY": []byte("session-key"),
	})
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent, secret).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	req := ctrl.Request{NamespacedName: types.NamespacedName{Name: "test-agent", Namespace: "test-ns"}}
	ctx := context.Background()

	// First pass adds the finalizer; the second renders the workload.
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("Reconcile 1: %v", err)
	}
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("Reconcile 2: %v", err)
	}

	gatewayKey := types.NamespacedName{Name: "test-agent-gateway", Namespace: "test-ns"}
	sts := &appsv1.StatefulSet{}
	if err := cl.Get(ctx, gatewayKey, sts); err != nil {
		t.Fatalf("read the gateway StatefulSet: %v", err)
	}
	if sts.Spec.Template.Annotations[secretEnvHashAnnotation] == "" {
		t.Fatalf("the rendered StatefulSet carries no %s; nothing would roll it", secretEnvHashAnnotation)
	}
}

// envFrom takes every key in the Secret, so rotating any of them has to move
// the digest. Digesting only the keys named by a secretKeyRef would leave a
// sidecar that pulls its environment in bulk stranded on the old values.
func TestRotatingAKeyInsideAnEnvFromSecretMovesTheDigest(t *testing.T) {
	agent := secretHashTestAgent()
	spec := &corev1.PodSpec{
		Containers: []corev1.Container{{
			Name: "sidecar",
			EnvFrom: []corev1.EnvFromSource{{
				SecretRef: &corev1.SecretEnvSource{LocalObjectReference: corev1.LocalObjectReference{Name: "bulk-env"}},
			}},
		}},
	}
	ctx := context.Background()

	before, _ := secretHashTestReconciler(
		secretHashTestSecret("bulk-env", map[string][]byte{"TOKEN": []byte("first"), "OTHER": []byte("same")}),
	)
	first, err := before.secretEnvHash(ctx, agent, spec)
	if err != nil {
		t.Fatalf("secretEnvHash before the rotation: %v", err)
	}

	after, _ := secretHashTestReconciler(
		secretHashTestSecret("bulk-env", map[string][]byte{"TOKEN": []byte("second"), "OTHER": []byte("same")}),
	)
	second, err := after.secretEnvHash(ctx, agent, spec)
	if err != nil {
		t.Fatalf("secretEnvHash after the rotation: %v", err)
	}
	if first == second {
		t.Error("a key inside an envFrom Secret rotated and the digest did not move, so the pod keeps the old environment")
	}
}
