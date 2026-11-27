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
	"reflect"
	"strconv"
	"strings"
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	policyv1 "k8s.io/api/policy/v1"
	"k8s.io/apimachinery/pkg/api/equality"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/labels"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/tools/record"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

func execTierTestAgent(replicas *int32) *agentv1alpha1.PlatformAgent {
	agent := a2aTestAgent()
	agent.Spec.Deployment = &agentv1alpha1.DeploymentSpec{
		CredentialProxy: &agentv1alpha1.CredentialProxySpec{Exec: &agentv1alpha1.CredentialProxyExecSpec{Replicas: replicas}},
	}
	return agent
}

func execTierTestReconciler(t *testing.T, agent *agentv1alpha1.PlatformAgent, objs ...client.Object) (*PlatformAgentReconciler, client.Client) {
	t.Helper()
	scheme := setupScheme()
	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(append([]client.Object{agent}, objs...)...).
		WithInterceptorFuncs(ssaApplyInterceptor()).Build()
	return &PlatformAgentReconciler{Client: cl, Scheme: scheme}, cl
}

func execTierPresent(t *testing.T, cl client.Client, obj client.Object, name string) bool {
	t.Helper()
	err := cl.Get(context.Background(), types.NamespacedName{Name: name, Namespace: "test-ns"}, obj)
	if err != nil && !apierrors.IsNotFound(err) {
		t.Fatal(err)
	}
	return err == nil
}

// Unset and 0 both render no exec tier.
func TestTheExecTierRendersNothingWhenOff(t *testing.T) {
	for name, replicas := range map[string]*int32{"unset": nil, "zero": ptr.To(int32(0))} {
		t.Run(name, func(t *testing.T) {
			agent := execTierTestAgent(replicas)
			r, cl := execTierTestReconciler(t, agent)
			if err := r.reconcileCredentialProxyExecTier(context.Background(), agent, buildCredentialProxyDeployment(agent, "hash"), credentialProxyProfileConfig{}); err != nil {
				t.Fatal(err)
			}
			n := credentialProxyExecTierName(agent)
			for kind, obj := range map[string]client.Object{
				"Deployment": &appsv1.Deployment{}, "Service": &corev1.Service{},
				"NetworkPolicy": &networkingv1.NetworkPolicy{}, "PodDisruptionBudget": &policyv1.PodDisruptionBudget{},
			} {
				if execTierPresent(t, cl, obj, n) {
					t.Errorf("%s rendered with the exec tier off", kind)
				}
			}
			if execTierPresent(t, cl, &corev1.ConfigMap{}, credentialProxyExecTierReplicasName(agent)) {
				t.Error("replica ConfigMap rendered with the exec tier off")
			}
		})
	}
}

// 2 renders every object with the budget; 1 drops the budget and rewrites the
// count without a new pod template; 0 removes the rest.
func TestTheExecTierFollowsItsReplicaCount(t *testing.T) {
	ctx := context.Background()
	agent := execTierTestAgent(ptr.To(int32(2)))
	r, cl := execTierTestReconciler(t, agent)
	stored := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, client.ObjectKeyFromObject(agent), stored); err != nil {
		t.Fatal(err)
	}
	n := credentialProxyExecTierName(agent)
	reconcileAt := func(replicas int32) {
		t.Helper()
		stored.Spec.Deployment.CredentialProxy.Exec.Replicas = ptr.To(replicas)
		if err := r.reconcileCredentialProxyExecTier(ctx, stored, buildCredentialProxyDeployment(stored, "hash"), credentialProxyProfileConfig{}); err != nil {
			t.Fatal(err)
		}
	}
	count := func() string {
		t.Helper()
		cm := &corev1.ConfigMap{}
		if !execTierPresent(t, cl, cm, credentialProxyExecTierReplicasName(agent)) {
			t.Fatal("no replica ConfigMap")
		}
		return cm.Data[credentialProxyExecTierReplicasKey]
	}

	reconcileAt(2)
	dep := &appsv1.Deployment{}
	if !execTierPresent(t, cl, dep, n) || !execTierPresent(t, cl, &corev1.Service{}, n) || !execTierPresent(t, cl, &networkingv1.NetworkPolicy{}, n) {
		t.Fatal("the exec tier's workload, Service or NetworkPolicy is missing at 2 replicas")
	}
	if !execTierPresent(t, cl, &policyv1.PodDisruptionBudget{}, n) {
		t.Error("no PodDisruptionBudget at 2 replicas")
	}
	if got := count(); got != "2" {
		t.Errorf("replica ConfigMap holds %q at 2 replicas", got)
	}
	if *dep.Spec.Replicas != 2 || dep.Spec.Strategy.Type != appsv1.RollingUpdateDeploymentStrategyType {
		t.Errorf("Deployment replicas %d strategy %s, want 2 RollingUpdate", *dep.Spec.Replicas, dep.Spec.Strategy.Type)
	}
	if ru := dep.Spec.Strategy.RollingUpdate; ru == nil || ru.MaxUnavailable == nil || ru.MaxUnavailable.IntValue() != 1 ||
		ru.MaxSurge == nil || ru.MaxSurge.IntValue() != 0 {
		t.Errorf("rollout %+v, want maxUnavailable 1 and maxSurge 0 so a full namespace quota cannot stall it", dep.Spec.Strategy.RollingUpdate)
	}
	templateAt2 := dep.Spec.Template.DeepCopy()

	reconcileAt(1)
	if execTierPresent(t, cl, &policyv1.PodDisruptionBudget{}, n) {
		t.Error("PodDisruptionBudget kept at 1 replica")
	}
	if got := count(); got != "1" {
		t.Errorf("replica ConfigMap holds %q at 1 replica", got)
	}
	if !execTierPresent(t, cl, dep, n) || *dep.Spec.Replicas != 1 {
		t.Fatal("Deployment not scaled to 1")
	}
	if !reflect.DeepEqual(templateAt2, &dep.Spec.Template) {
		t.Error("a scale change changed the pod template, which restarts the tier")
	}

	reconcileAt(0)
	for kind, obj := range map[string]client.Object{
		"Deployment": &appsv1.Deployment{}, "Service": &corev1.Service{}, "NetworkPolicy": &networkingv1.NetworkPolicy{},
	} {
		if execTierPresent(t, cl, obj, n) {
			t.Errorf("%s survived the scale to 0", kind)
		}
	}
	if execTierPresent(t, cl, &corev1.ConfigMap{}, credentialProxyExecTierReplicasName(agent)) {
		t.Error("replica ConfigMap survived the scale to 0")
	}
}

// The fence outlives the pods it fences, and an object under the tier's name
// that the agent does not own is left alone.
func TestTheExecTierRemovalKeepsTheFenceForRunningPodsAndSkipsUnownedObjects(t *testing.T) {
	ctx := context.Background()
	agent := execTierTestAgent(ptr.To(int32(0)))
	n := credentialProxyExecTierName(agent)
	pod := &corev1.Pod{ObjectMeta: metav1.ObjectMeta{Name: n + "-abc", Namespace: "test-ns", Labels: credentialProxyExecTierSelector(agent)}}
	unowned := &corev1.Service{ObjectMeta: metav1.ObjectMeta{Name: n, Namespace: "test-ns"}}
	r, cl := execTierTestReconciler(t, agent, pod, unowned)
	stored := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, client.ObjectKeyFromObject(agent), stored); err != nil {
		t.Fatal(err)
	}
	np := buildCredentialProxyExecTierNetworkPolicy(stored, "", credentialProxyProfileConfig{})
	np.OwnerReferences = []metav1.OwnerReference{{APIVersion: "kubeagents.x-k8s.io/v1alpha1", Kind: "PlatformAgent", Name: stored.Name, UID: stored.UID, Controller: ptr.To(true)}}
	if err := cl.Create(ctx, np); err != nil {
		t.Fatal(err)
	}
	if err := r.removeCredentialProxyExecTier(ctx, stored); err != nil {
		t.Fatal(err)
	}
	if !execTierPresent(t, cl, &networkingv1.NetworkPolicy{}, n) {
		t.Error("the fence went while an exec pod is still there")
	}
	if !execTierPresent(t, cl, &corev1.Service{}, n) {
		t.Error("an unowned Service under the tier's name was deleted")
	}
	if err := cl.Delete(ctx, pod); err != nil {
		t.Fatal(err)
	}
	if err := r.removeCredentialProxyExecTier(ctx, stored); err != nil {
		t.Fatal(err)
	}
	if execTierPresent(t, cl, &networkingv1.NetworkPolicy{}, n) {
		t.Error("the fence survived once no exec pod is left")
	}
}

// The exec pod carries the broker's image and caller authentication, but no
// secret it never uses, no forge credentials, no relay settings, and not the
// stateful broker's labels.
func TestTheExecTierDeploymentIsTheBrokerWithoutItsSecrets(t *testing.T) {
	agent := execTierTestAgent(ptr.To(int32(3)))
	broker := buildCredentialProxyDeployment(agent, "hash")
	bc := &broker.Spec.Template.Spec.Containers[0]
	bc.Env = append(bc.Env,
		corev1.EnvVar{Name: "SLACK_BOT_TOKEN", ValueFrom: &corev1.EnvVarSource{SecretKeyRef: &corev1.SecretKeySelector{LocalObjectReference: corev1.LocalObjectReference{Name: "s"}, Key: "k"}}},
		corev1.EnvVar{Name: "TOKEN_BROKER_URL", Value: "http://minter"},
		corev1.EnvVar{Name: "GOOGLE_CHAT_SUBSCRIPTION_NAME", Value: "sub"},
		corev1.EnvVar{Name: a2aGoogleChatSubscriptionEnvVar, Value: "a2a-sub"},
		corev1.EnvVar{Name: credentialProxyA2AChatAudienceEnvVar, Value: "aud"},
		corev1.EnvVar{Name: credentialProxyA2AChatCallersEnvVar, Value: "system:serviceaccount:ns:a2a-gateway"},
	)
	bc.EnvFrom = append(bc.EnvFrom, corev1.EnvFromSource{SecretRef: &corev1.SecretEnvSource{LocalObjectReference: corev1.LocalObjectReference{Name: "plugin"}}})
	broker.Spec.Template.Annotations[secretEnvHashAnnotation] = "digest"

	dep := buildCredentialProxyExecTierDeployment(agent, broker)
	if dep.Name != "test-agent-credential-proxy-exec-tier" || *dep.Spec.Replicas != 3 {
		t.Errorf("Deployment %s replicas %d, want test-agent-credential-proxy-exec-tier at 3", dep.Name, *dep.Spec.Replicas)
	}
	podLabels := dep.Spec.Template.Labels
	if _, ok := podLabels["kubeagents.x-k8s.io/has-credential-proxy"]; ok {
		t.Error("exec pod carries has-credential-proxy, which opens the token minter to it")
	}
	if labels.SelectorFromSet(credentialProxySelector(agent)).Matches(labels.Set(podLabels)) {
		t.Error("the stateful broker's selector matches an exec pod")
	}
	if labels.SelectorFromSet(credentialProxyExecTierSelector(agent)).Matches(labels.Set(broker.Spec.Template.Labels)) {
		t.Error("the exec tier's selector matches the stateful broker's pod")
	}
	if _, ok := dep.Spec.Template.Annotations[secretEnvHashAnnotation]; ok {
		t.Error("exec pod template carries the Secret env hash, so a chat secret rotation would roll it")
	}
	if dep.Spec.Template.Annotations["kubeagents.x-k8s.io/proxy-policy-hash"] != "hash" {
		t.Error("exec pod template lost the policy hash, so a policy change would not roll it")
	}
	c := dep.Spec.Template.Spec.Containers[0]
	if c.Image != bc.Image {
		t.Errorf("exec image %s, broker image %s", c.Image, bc.Image)
	}
	env := map[string]corev1.EnvVar{}
	for _, e := range c.Env {
		if _, dup := env[e.Name]; dup {
			t.Errorf("env %s set twice", e.Name)
		}
		env[e.Name] = e
		if e.ValueFrom != nil && e.ValueFrom.SecretKeyRef != nil {
			t.Errorf("exec pod reads %s from a Secret", e.Name)
		}
	}
	for _, name := range []string{"TOKEN_BROKER_URL", "GOOGLE_CHAT_SUBSCRIPTION_NAME", a2aGoogleChatSubscriptionEnvVar, credentialProxyA2AChatAudienceEnvVar} {
		if _, ok := env[name]; ok {
			t.Errorf("exec pod carries %s", name)
		}
	}
	// The A2A gateway stays an allowed caller on the exec tier while Chat is
	// armed, so the binding that holds it to the a2a-chat audience (which
	// this tier never confers) comes along: here it refuses the gateway's
	// identity every audience (#6090).
	if got := env[credentialProxyA2AChatCallersEnvVar].Value; got != "system:serviceaccount:ns:a2a-gateway" {
		t.Errorf("exec pod %s = %q, want the A2A gateway's binding carried over", credentialProxyA2AChatCallersEnvVar, got)
	}
	if env[credentialProxyRoleEnv].Value != credentialProxyExecRole {
		t.Errorf("role %q, want exec", env[credentialProxyRoleEnv].Value)
	}
	if env[credentialProxyExecTierReplicasFileEnv].Value != "/etc/credential-proxy-exec-tier/replicas" {
		t.Errorf("replicas file %q", env[credentialProxyExecTierReplicasFileEnv].Value)
	}
	if _, ok := env["CREDENTIAL_PROXY_ALLOWED_CALLERS"]; !ok {
		t.Error("exec pod lost the broker's caller authentication")
	}
	for _, src := range c.EnvFrom {
		if src.SecretRef != nil {
			t.Errorf("exec pod takes env from Secret %s", src.SecretRef.Name)
		}
	}
	mounted := false
	for _, m := range c.VolumeMounts {
		if m.Name == credentialProxyExecTierReplicasVolume && m.MountPath == "/etc/credential-proxy-exec-tier" && m.SubPath == "" {
			mounted = true
		}
	}
	if !mounted {
		t.Error("replica ConfigMap not mounted as a directory at /etc/credential-proxy-exec-tier")
	}
	want := resource.MustParse("2")
	if !c.Resources.Requests.Cpu().Equal(want) || !c.Resources.Limits.Cpu().Equal(want) {
		t.Errorf("cpu %v/%v, want 2/2", c.Resources.Requests.Cpu(), c.Resources.Limits.Cpu())
	}
	wantMem := resource.MustParse("2Gi")
	if !c.Resources.Requests.Memory().Equal(wantMem) || !c.Resources.Limits.Memory().Equal(wantMem) {
		t.Errorf("memory %v/%v, want 2Gi/2Gi", c.Resources.Requests.Memory(), c.Resources.Limits.Memory())
	}
	// Ephemeral storage is the tier's own, and declared: a namespace quota on
	// it refuses a pod that declares none.
	if want := resource.MustParse(credentialProxyExecTierEphemeralStorageLimit); !c.Resources.Limits.StorageEphemeral().Equal(want) {
		t.Errorf("ephemeral-storage limit %v, want the tier's %v", c.Resources.Limits.StorageEphemeral(), want)
	}
	if broker.Spec.Template.Labels["kubeagents.x-k8s.io/has-credential-proxy"] != "true" || len(bc.EnvFrom) == 0 {
		t.Error("deriving the exec tier changed the broker's Deployment")
	}
}

// The tier is derived from the broker as rendered, not from the object the
// apply hands back: a field another manager set on the broker, such as a
// rollout restart's annotation, does not reach the exec pods.
func TestTheExecTierIgnoresFieldsOthersSetOnTheBroker(t *testing.T) {
	const restartedAt = "kubectl.kubernetes.io/restartedAt"
	agent := execTierTestAgent(ptr.To(int32(2)))
	apply := ssaApplyInterceptor()
	scheme := setupScheme()
	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent).
		WithInterceptorFuncs(interceptor.Funcs{
			Patch: func(ctx context.Context, c client.WithWatch, obj client.Object, patch client.Patch, opts ...client.PatchOption) error {
				if err := apply.Patch(ctx, c, obj, patch, opts...); err != nil {
					return err
				}
				// The server's merged answer for the broker carries the annotation.
				if dep, ok := obj.(*appsv1.Deployment); ok && dep.Name == credentialProxyName(agent) {
					if dep.Spec.Template.Annotations == nil {
						dep.Spec.Template.Annotations = map[string]string{}
					}
					dep.Spec.Template.Annotations[restartedAt] = "2026-11-04T00:00:00Z"
				}
				return nil
			},
		}).Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	if _, err := r.reconcileCredentialProxy(context.Background(), agent, brokerTestPolicyHash, nil, allBrokerCallersOwned); err != nil {
		t.Fatal(err)
	}
	dep := &appsv1.Deployment{}
	if err := cl.Get(context.Background(), types.NamespacedName{Name: credentialProxyExecTierName(agent), Namespace: agent.Namespace}, dep); err != nil {
		t.Fatal(err)
	}
	if v, ok := dep.Spec.Template.Annotations[restartedAt]; ok {
		t.Errorf("exec pod template carries the broker's %s=%s, so a broker restart rolls the tier", restartedAt, v)
	}
}

// A CR cannot give the exec pod another role: spec.deployment.env does not
// reach the broker's role, and the exec pod sets its own once.
func TestTheExecTierRoleCannotBeSetFromTheCR(t *testing.T) {
	agent := execTierTestAgent(ptr.To(int32(2)))
	agent.Spec.Deployment.Env = []corev1.EnvVar{{Name: credentialProxyRoleEnv, Value: "api-proxy"}}
	dep := buildCredentialProxyExecTierDeployment(agent, buildCredentialProxyDeployment(agent, "hash"))
	var roles []string
	for _, e := range dep.Spec.Template.Spec.Containers[0].Env {
		if e.Name == credentialProxyRoleEnv {
			roles = append(roles, e.Value)
		}
	}
	if len(roles) != 1 || roles[0] != credentialProxyExecRole {
		t.Errorf("exec pod roles %v, want exactly [exec]", roles)
	}
}

// The forge credentials and configuration stay on the stateful broker.
func TestTheExecTierMountsNoForgeCredentials(t *testing.T) {
	agent := gitlabAgent("gitlab-token")
	agent.Spec.Deployment = &agentv1alpha1.DeploymentSpec{
		CredentialProxy: &agentv1alpha1.CredentialProxySpec{Exec: &agentv1alpha1.CredentialProxyExecSpec{Replicas: ptr.To(int32(1))}},
	}
	broker := buildCredentialProxyDeployment(agent, "hash")
	forgeVolumes := buildVCSForgesVolumes(agent)
	forgeMounts := buildVCSForgesVolumeMounts(agent)
	if len(forgeVolumes) == 0 || len(forgeMounts) < 2 {
		t.Fatalf("test agent renders %d forge volumes and %d forge mounts; the test needs a forge with a credential", len(forgeVolumes), len(forgeMounts))
	}
	dep := buildCredentialProxyExecTierDeployment(agent, broker)
	for _, fv := range forgeVolumes {
		for _, v := range dep.Spec.Template.Spec.Volumes {
			if v.Name == fv.Name {
				t.Errorf("exec pod has forge volume %s", v.Name)
			}
		}
	}
	c := dep.Spec.Template.Spec.Containers[0]
	for _, fm := range forgeMounts {
		for _, m := range c.VolumeMounts {
			if m.MountPath == fm.MountPath {
				t.Errorf("exec pod mounts %s", m.MountPath)
			}
		}
	}
	for _, e := range buildVCSForgesEnv(agent) {
		for _, got := range c.Env {
			if got.Name == e.Name {
				t.Errorf("exec pod carries %s", e.Name)
			}
		}
	}
}

// A Secret the broker gains later, as a volume or a projected source, does not
// reach the exec pods: they are dropped by source, not by name.
func TestTheExecTierDropsEverySecretBackedVolume(t *testing.T) {
	agent := gitlabAgent("gitlab-token")
	agent.Spec.Deployment = &agentv1alpha1.DeploymentSpec{
		CredentialProxy: &agentv1alpha1.CredentialProxySpec{Exec: &agentv1alpha1.CredentialProxyExecSpec{Replicas: ptr.To(int32(2))}},
	}
	broker := buildCredentialProxyDeployment(agent, "hash")
	spec := &broker.Spec.Template.Spec
	spec.Volumes = append(spec.Volumes,
		corev1.Volume{Name: "future-secret", VolumeSource: corev1.VolumeSource{Secret: &corev1.SecretVolumeSource{SecretName: "x"}}},
		corev1.Volume{Name: "future-projected", VolumeSource: corev1.VolumeSource{Projected: &corev1.ProjectedVolumeSource{
			Sources: []corev1.VolumeProjection{{Secret: &corev1.SecretProjection{LocalObjectReference: corev1.LocalObjectReference{Name: "y"}}}},
		}}},
	)
	c := &spec.Containers[0]
	c.VolumeMounts = append(c.VolumeMounts,
		corev1.VolumeMount{Name: "future-secret", MountPath: "/future/secret"},
		corev1.VolumeMount{Name: "future-projected", MountPath: "/future/projected"},
	)
	dep := buildCredentialProxyExecTierDeployment(agent, broker)
	kept := map[string]bool{}
	for _, v := range dep.Spec.Template.Spec.Volumes {
		kept[v.Name] = true
		if volumeHoldsSecret(v) {
			t.Errorf("exec pod has Secret-backed volume %s", v.Name)
		}
	}
	for _, ctr := range dep.Spec.Template.Spec.Containers {
		for _, m := range ctr.VolumeMounts {
			if !kept[m.Name] {
				t.Errorf("container %s mounts %s, a volume the exec pod does not have", ctr.Name, m.Name)
			}
		}
		for _, e := range ctr.Env {
			if e.ValueFrom != nil && e.ValueFrom.SecretKeyRef != nil {
				t.Errorf("container %s reads %s from a Secret", ctr.Name, e.Name)
			}
		}
	}
	// By name, not through volumeHoldsSecret, which the filter itself uses.
	if kept["future-secret"] || kept["future-projected"] {
		t.Errorf("exec pod kept a Secret-backed volume: %v", kept)
	}
	if !kept["credential-proxy-policy"] {
		t.Error("exec pod lost a volume that holds no Secret")
	}
	// The minter's token is projected, not a Secret, and the exec role has no
	// use for it; the broker keeps it.
	if kept["credential-proxy-ksa-token"] {
		t.Error("exec pod keeps the github-token-minter token volume")
	}
	for _, ctr := range dep.Spec.Template.Spec.Containers {
		for _, m := range ctr.VolumeMounts {
			if m.MountPath == "/var/run/secrets/kubeagents/serviceaccount" {
				t.Errorf("container %s mounts the minter token at %s", ctr.Name, m.MountPath)
			}
		}
		for _, e := range ctr.Env {
			if e.Name == "KSA_TOKEN_FILE" {
				t.Errorf("container %s carries KSA_TOKEN_FILE", ctr.Name)
			}
		}
	}
	brokerKeeps := false
	for _, v := range broker.Spec.Template.Spec.Volumes {
		brokerKeeps = brokerKeeps || v.Name == "credential-proxy-ksa-token"
	}
	if !brokerKeeps {
		t.Error("deriving the exec tier took the minter token from the broker")
	}
}

// Callers on the credentialed port: the sandbox always, the profile namespace
// only with a profile caller, and never the gateway.
func TestTheExecTierFenceAdmitsItsCallersOnly(t *testing.T) {
	agent := execTierTestAgent(ptr.To(int32(1)))
	np := buildCredentialProxyExecTierNetworkPolicy(agent, "", credentialProxyProfileConfig{})
	if !reflect.DeepEqual(np.Spec.PodSelector.MatchLabels, credentialProxyExecTierSelector(agent)) {
		t.Errorf("fence selects %v", np.Spec.PodSelector.MatchLabels)
	}
	from := np.Spec.Ingress[0].From
	if len(from) != 1 || !reflect.DeepEqual(from[0].PodSelector.MatchLabels, shellSandboxSelector(agent)) {
		t.Errorf("callers without profiles = %v, want the sandbox only", from)
	}
	withProfiles := buildCredentialProxyExecTierNetworkPolicy(agent, "", credentialProxyProfileConfig{Identities: []credentialProxyProfileIdentity{{}}})
	if got := withProfiles.Spec.Ingress[0].From; len(got) != 2 || !reflect.DeepEqual(got[1], credentialProxyProfilePeer(agent)) {
		t.Errorf("callers with a profile = %v, want the sandbox and the profile namespace", got)
	}
	for _, peer := range withProfiles.Spec.Ingress[0].From {
		if peer.PodSelector != nil && peer.PodSelector.MatchLabels["app"] == agent.Name+"-gateway" {
			t.Error("fence admits the gateway")
		}
	}
	// The session pods only under the cluster view, which points them at the
	// tier.
	for _, view := range []string{"true", ""} {
		t.Setenv(a2aSessionClusterViewEnvVar, view)
		admitted := false
		for _, peer := range buildCredentialProxyExecTierNetworkPolicy(agent, "", credentialProxyProfileConfig{}).Spec.Ingress[0].From {
			if peer.PodSelector != nil && reflect.DeepEqual(peer.PodSelector.MatchLabels, a2aSessionPodSelector()) {
				admitted = true
			}
		}
		if admitted != (view == "true") {
			t.Errorf("with %s=%q the fence admits the session pods: %v", a2aSessionClusterViewEnvVar, view, admitted)
		}
	}
}

// With the tier on, the agent's name must leave room for the suffix in a
// 63-character Service name: 36 characters render, 37 are refused and render
// nothing, and 37 with the tier off is not refused.
func TestTheExecTierRefusesAnAgentNameTooLongForItsObjects(t *testing.T) {
	ctx := context.Background()
	for _, tc := range []struct {
		nameLen  int
		replicas int32
		refused  bool
	}{
		{36, 2, false},
		{37, 2, true},
		{37, 0, false},
	} {
		agent := execTierTestAgent(ptr.To(tc.replicas))
		agent.Name = strings.Repeat("a", tc.nameLen)
		if got := len(ValidateCredentialProxyExec(agent, nil)) > 0; got != tc.refused {
			t.Errorf("name of %d with %d replicas: refused %v, want %v", tc.nameLen, tc.replicas, got, tc.refused)
		}
		if tc.replicas == 0 {
			continue
		}
		r, cl := execTierTestReconciler(t, agent)
		if err := r.reconcileCredentialProxyExecTier(ctx, agent, buildCredentialProxyDeployment(agent, "hash"), credentialProxyProfileConfig{}); err != nil {
			t.Fatal(err)
		}
		dep := &appsv1.Deployment{}
		err := cl.Get(ctx, types.NamespacedName{Name: credentialProxyExecTierName(agent), Namespace: agent.Namespace}, dep)
		if rendered := err == nil; rendered == tc.refused {
			t.Errorf("name of %d: exec Deployment rendered %v, want %v", tc.nameLen, rendered, !tc.refused)
		}
		if !tc.refused && len(credentialProxyExecTierName(agent)) != 63 {
			t.Errorf("the longest accepted name gives a %d-character Service name, want 63", len(credentialProxyExecTierName(agent)))
		}
	}
}

// Without the webhook, a refused name is on the CR: Degraded with the reason
// on every pass until it is corrected, while Ready and the phase keep what the
// workloads say, as for the three resource refusals.
func TestAnAgentNameTooLongForTheExecTierIsDegraded(t *testing.T) {
	scheme := setupScheme()
	agent := execTierTestAgent(ptr.To(int32(2)))
	agent.Name = strings.Repeat("a", credentialProxyExecTierMaxAgentName+1)
	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent, shellSandboxKeysSecret(agent)).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme, Recorder: record.NewFakeRecorder(64)}
	req := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(agent)}
	for i := range 2 {
		if _, err := r.Reconcile(context.Background(), req); err != nil {
			t.Fatalf("Reconcile %d: %v", i+1, err)
		}
	}
	updated := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(context.Background(), req.NamespacedName, updated); err != nil {
		t.Fatal(err)
	}
	degraded := meta.FindStatusCondition(updated.Status.Conditions, "Degraded")
	if degraded == nil || degraded.Status != metav1.ConditionTrue || degraded.Reason != conditionReasonInvalidCredentialProxyExec {
		t.Fatalf("Degraded = %v, want True/%s", degraded, conditionReasonInvalidCredentialProxyExec)
	}
	if !strings.Contains(degraded.Message, "must be at most 36") {
		t.Errorf("Degraded message %q does not name the limit", degraded.Message)
	}
	if updated.Status.Phase == "Degraded" {
		t.Error("Status.Phase = Degraded; the refusal is reported on the Degraded condition only")
	}
}

// brokerEgressPeers returns the peers of the egress rule on the broker's port.
func brokerEgressPeers(t *testing.T, np *networkingv1.NetworkPolicy) []networkingv1.NetworkPolicyPeer {
	t.Helper()
	for _, rule := range np.Spec.Egress {
		for _, port := range rule.Ports {
			if port.Port != nil && port.Port.IntValue() == credentialProxyPort {
				return rule.To
			}
		}
	}
	t.Fatalf("%s has no egress rule on %d", np.Name, credentialProxyPort)
	return nil
}

// hasExecTierPeer reports whether the peers select the exec tier's pods.
func hasExecTierPeer(agent *agentv1alpha1.PlatformAgent, peers []networkingv1.NetworkPolicyPeer) bool {
	for _, p := range peers {
		if p.PodSelector != nil && reflect.DeepEqual(p.PodSelector.MatchLabels, credentialProxyExecTierSelector(agent)) {
			return true
		}
	}
	return false
}

// hasNamespacedExecTierPeer reports whether the peers select the exec tier's
// pods in the agent's namespace, for a caller in another namespace: a peer
// that lost its namespace selector would match nothing there.
func hasNamespacedExecTierPeer(agent *agentv1alpha1.PlatformAgent, peers []networkingv1.NetworkPolicyPeer) bool {
	want := namespacedPodPeer(agent.Namespace, credentialProxyExecTierSelector(agent))
	for _, p := range peers {
		if reflect.DeepEqual(p, want) {
			return true
		}
	}
	return false
}

func execTierEnvValue(env []corev1.EnvVar, name string) (string, bool) {
	for _, e := range env {
		if e.Name == name {
			return e.Value, true
		}
	}
	return "", false
}

// The sandbox gets the tier's URL and an egress peer to it at a positive
// count, and neither at 0.
func TestTheSandboxReachesTheExecTierOnlyWhileItRenders(t *testing.T) {
	for _, replicas := range []int32{0, 2} {
		agent := execTierTestAgent(ptr.To(replicas))
		sts := buildShellSandboxStatefulSet(agent, "keys", credentialProxySandboxURL(agent), "")
		var env []corev1.EnvVar
		for _, c := range sts.Spec.Template.Spec.Containers {
			env = append(env, c.Env...)
		}
		// The literal name: the shim reads CREDENTIAL_PROXY_EXEC_URL (§12).
		url, set := execTierEnvValue(env, "CREDENTIAL_PROXY_EXEC_URL")
		if replicas > 0 && url != "http://test-agent-credential-proxy-exec-tier.test-ns.svc.cluster.local:8765" {
			t.Errorf("at %d replicas the sandbox's %s = %q", replicas, credentialProxyExecURLEnv, url)
		}
		if replicas == 0 && set {
			t.Errorf("at 0 replicas the sandbox has %s", credentialProxyExecURLEnv)
		}
		peers := brokerEgressPeers(t, buildShellSandboxNetworkPolicy(agent, []string{"10.96.0.10"}))
		if hasExecTierPeer(agent, peers) != (replicas > 0) {
			t.Errorf("at %d replicas the sandbox's egress to the exec tier is %v", replicas, hasExecTierPeer(agent, peers))
		}
		if len(peers) == 0 || !reflect.DeepEqual(peers[0].PodSelector.MatchLabels, credentialProxySelector(agent)) {
			t.Errorf("at %d replicas the sandbox lost its egress to the broker", replicas)
		}
	}
}

// The A2A gateway is told the tier's URL only under the cluster view and at a
// positive count; the session fence opens to the tier on the same terms.
func TestSessionPodsReachTheExecTierOnlyUnderTheClusterView(t *testing.T) {
	for name, tc := range map[string]struct {
		view     string
		replicas int32
		want     bool
	}{
		"view, tier":    {view: "true", replicas: 2, want: true},
		"view, no tier": {view: "true", replicas: 0},
		"no view, tier": {view: "", replicas: 2},
	} {
		t.Run(name, func(t *testing.T) {
			t.Setenv(a2aSessionClusterViewEnvVar, tc.view)
			agent := execTierTestAgent(ptr.To(tc.replicas))
			// The literal name a2a/gateway's FromEnv reads.
			url, set := execTierEnvValue(buildA2AGatewayDeployment(agent).Spec.Template.Spec.Containers[0].Env, "A2A_CREDENTIAL_PROXY_EXEC_URL")
			if set != tc.want || (tc.want && url != credentialProxyExecTierURL(agent)) {
				t.Errorf("%s = %q (set %v), want set %v", a2aCredentialProxyExecURLEnv, url, set, tc.want)
			}
			np := buildA2ASessionNetworkPolicy(agent, []string{"10.96.0.10"})
			if tc.view == "" {
				for _, rule := range np.Spec.Egress {
					if hasExecTierPeer(agent, rule.To) {
						t.Error("session fence opens to the exec tier without the cluster view")
					}
				}
				return
			}
			if got := hasNamespacedExecTierPeer(agent, brokerEgressPeers(t, np)); got != tc.want {
				t.Errorf("session egress to the exec tier in the agent's namespace = %v, want %v", got, tc.want)
			}
		})
	}
}

// Profile workers get the tier's URL, and the profile namespace's fence opens
// to the tier, at a positive count only.
func TestProfileWorkersReachTheExecTierOnlyWhileItRenders(t *testing.T) {
	for _, replicas := range []int32{0, 2} {
		agent := execTierTestAgent(ptr.To(replicas))
		profile := testAgentProfile(agent.Namespace, "auditor")
		for _, mode := range []profileWorkerMode{profileWorkerPool, profileWorkerOneShot} {
			spec := buildProfileWorkerPodSpec(agent, &profile, "read", "agentprofile-auditor-read", mode, false)
			// The literal name: the shim reads CREDENTIAL_PROXY_EXEC_URL (§12).
			url, set := execTierEnvValue(spec.Containers[0].Env, "CREDENTIAL_PROXY_EXEC_URL")
			if replicas > 0 && url != "http://test-agent-credential-proxy-exec-tier.test-ns.svc.cluster.local:8765" {
				t.Errorf("at %d replicas the profile worker's %s = %q (mode %v)", replicas, credentialProxyExecURLEnv, url, mode)
			}
			if replicas == 0 && set {
				t.Errorf("at 0 replicas the profile worker has %s (mode %v)", credentialProxyExecURLEnv, mode)
			}
		}
		peers := brokerEgressPeers(t, buildProfileNetworkPolicy(agent, []string{"10.96.0.10"}))
		if hasNamespacedExecTierPeer(agent, peers) != (replicas > 0) {
			t.Errorf("at %d replicas the profile egress to the exec tier in the agent's namespace is %v", replicas, hasNamespacedExecTierPeer(agent, peers))
		}
	}
}

// A refused name sends no caller to a tier that is not rendered.
func TestARefusedNameGivesCallersNoExecTier(t *testing.T) {
	agent := execTierTestAgent(ptr.To(int32(2)))
	agent.Name = strings.Repeat("a", 37)
	sts := buildShellSandboxStatefulSet(agent, "keys", credentialProxySandboxURL(agent), "")
	for _, c := range sts.Spec.Template.Spec.Containers {
		if _, set := execTierEnvValue(c.Env, credentialProxyExecURLEnv); set {
			t.Errorf("container %s gets %s for a refused tier", c.Name, credentialProxyExecURLEnv)
		}
	}
	if hasExecTierPeer(agent, brokerEgressPeers(t, buildShellSandboxNetworkPolicy(agent, []string{"10.96.0.10"}))) {
		t.Error("the sandbox's egress opens to a refused tier")
	}
	t.Setenv(a2aSessionClusterViewEnvVar, "true")
	if _, set := execTierEnvValue(buildA2AGatewayDeployment(agent).Spec.Template.Spec.Containers[0].Env, "A2A_CREDENTIAL_PROXY_EXEC_URL"); set {
		t.Error("the A2A gateway gets the URL of a refused tier")
	}
	if hasNamespacedExecTierPeer(agent, brokerEgressPeers(t, buildA2ASessionNetworkPolicy(agent, []string{"10.96.0.10"}))) {
		t.Error("the session fence opens to a refused tier")
	}
	if hasNamespacedExecTierPeer(agent, brokerEgressPeers(t, buildProfileNetworkPolicy(agent, []string{"10.96.0.10"}))) {
		t.Error("the profile fence opens to a refused tier")
	}
	profile := testAgentProfile(agent.Namespace, "auditor")
	if _, set := execTierEnvValue(buildProfileWorkerPodSpec(agent, &profile, "read", "agentprofile-auditor-read", profileWorkerPool, false).Containers[0].Env, credentialProxyExecURLEnv); set {
		t.Error("the profile worker gets the URL of a refused tier")
	}
}

// TestTheExecTierCarriesItsOwnEphemeralStorage pins the tier's own figures: the
// two emptyDir caps, the watermark gate told the same caps, the limit holding
// both with room for the layer and the logs, and the broker left at its own.
func TestTheExecTierCarriesItsOwnEphemeralStorage(t *testing.T) {
	agent := execTierTestAgent(ptr.To(int32(1)))
	broker := buildCredentialProxyDeployment(agent, brokerTestPolicyHash)
	brokerBefore := broker.DeepCopy()
	dep := buildCredentialProxyExecTierDeployment(agent, broker)

	sizes := map[string]*resource.Quantity{}
	for _, v := range dep.Spec.Template.Spec.Volumes {
		if v.EmptyDir != nil {
			sizes[v.Name] = v.EmptyDir.SizeLimit
		}
	}
	state, tmp := resource.MustParse(credentialProxyExecTierStateSizeLimit), resource.MustParse(credentialProxyExecTierTmpSizeLimit)
	for name, want := range map[string]resource.Quantity{credentialProxyStateVolumeName: state, credentialProxyTmpVolumeName: tmp} {
		if got := sizes[name]; got == nil || !got.Equal(want) {
			t.Errorf("exec %s sizeLimit = %v, want %v", name, got, want)
		}
	}
	var c *corev1.Container
	for i := range dep.Spec.Template.Spec.Containers {
		if dep.Spec.Template.Spec.Containers[i].Name == credentialProxyContainerName {
			c = &dep.Spec.Template.Spec.Containers[i]
		}
	}
	if c == nil {
		t.Fatal("exec Deployment has no credential proxy container")
	}
	env := map[string]string{}
	for _, e := range c.Env {
		env[e.Name] = e.Value
	}
	if got, want := env[credentialProxyStateLimitEnv], strconv.FormatInt(state.Value(), 10); got != want {
		t.Errorf("%s = %q, want %q, the state cap the kubelet evicts on", credentialProxyStateLimitEnv, got, want)
	}
	if got, want := env[credentialProxyTmpLimitEnv], strconv.FormatInt(tmp.Value(), 10); got != want {
		t.Errorf("%s = %q, want %q, the /tmp cap the kubelet evicts on", credentialProxyTmpLimitEnv, got, want)
	}
	limit := c.Resources.Limits.StorageEphemeral()
	both := state.DeepCopy()
	both.Add(tmp)
	if limit.Cmp(both) <= 0 {
		t.Errorf("exec ephemeral-storage limit %v does not hold the two caps (%v) with room for the layer and the logs", limit, &both)
	}
	if !equality.Semantic.DeepEqual(broker, brokerBefore) {
		t.Error("building the exec tier changed the broker's Deployment")
	}
}

// TestABrokerEphemeralStorageOverrideDoesNotReachTheExecTier: the tier's figures
// are fixed, as its cpu and memory are, so an override that raises, lowers or
// only requests the broker's ephemeral storage leaves the exec pod at its own
// caps, its gate at the same caps, and its request no larger than its limit.
func TestABrokerEphemeralStorageOverrideDoesNotReachTheExecTier(t *testing.T) {
	for name, res := range map[string]corev1.ResourceRequirements{
		"raised": {
			Requests: corev1.ResourceList{corev1.ResourceEphemeralStorage: resource.MustParse("10Gi")},
			Limits:   corev1.ResourceList{corev1.ResourceEphemeralStorage: resource.MustParse("12Gi")},
		},
		"lowered":      {Limits: corev1.ResourceList{corev1.ResourceEphemeralStorage: resource.MustParse("1Gi")}},
		"request only": {Requests: corev1.ResourceList{corev1.ResourceEphemeralStorage: resource.MustParse("6Gi")}},
	} {
		t.Run(name, func(t *testing.T) {
			agent := execTierTestAgent(ptr.To(int32(1)))
			agent.Spec.Deployment.CredentialProxy.Resources = &res
			dep := buildCredentialProxyExecTierDeployment(agent, buildCredentialProxyDeployment(agent, brokerTestPolicyHash))
			want := resource.MustParse(credentialProxyExecTierEphemeralStorageLimit)
			for _, c := range dep.Spec.Template.Spec.Containers {
				if c.Name != credentialProxyContainerName {
					continue
				}
				if got := c.Resources.Limits.StorageEphemeral(); !got.Equal(want) {
					t.Errorf("exec ephemeral-storage limit = %v, want %v", got, &want)
				}
				if req, ok := c.Resources.Requests[corev1.ResourceEphemeralStorage]; ok && req.Cmp(want) > 0 {
					t.Errorf("exec ephemeral-storage request %v is above its %v limit", &req, &want)
				}
				for _, e := range c.Env {
					if e.Name == credentialProxyStateLimitEnv && e.Value != credentialProxyExecTierLimitBytes(credentialProxyExecTierStateSizeLimit) {
						t.Errorf("%s = %s, want the tier's state cap", e.Name, e.Value)
					}
					if e.Name == credentialProxyTmpLimitEnv && e.Value != credentialProxyExecTierLimitBytes(credentialProxyExecTierTmpSizeLimit) {
						t.Errorf("%s = %s, want the tier's /tmp cap", e.Name, e.Value)
					}
				}
			}
			for _, v := range dep.Spec.Template.Spec.Volumes {
				if v.EmptyDir == nil {
					continue
				}
				switch v.Name {
				case credentialProxyStateVolumeName:
					if !v.EmptyDir.SizeLimit.Equal(resource.MustParse(credentialProxyExecTierStateSizeLimit)) {
						t.Errorf("exec state sizeLimit = %v", v.EmptyDir.SizeLimit)
					}
				case credentialProxyTmpVolumeName:
					if !v.EmptyDir.SizeLimit.Equal(resource.MustParse(credentialProxyExecTierTmpSizeLimit)) {
						t.Errorf("exec /tmp sizeLimit = %v", v.EmptyDir.SizeLimit)
					}
				}
			}
		})
	}
}

// The kubectl cost tier reaches the broker's container on both tiers, the
// stateful broker and the exec pods, normalised as the shell sandbox's copy
// is: the broker's kubectl cost gate charges the same column the sandbox's
// admission does. A value the normalisation refuses reaches neither, raw or
// otherwise.
func TestTheKubectlCostTierReachesBothBrokerTiers(t *testing.T) {
	tiers := func(spec corev1.PodSpec) []corev1.EnvVar {
		var out []corev1.EnvVar
		for _, c := range spec.Containers {
			if c.Name != credentialProxyContainerName {
				continue
			}
			for _, e := range c.Env {
				if e.Name == envKubectlCostTier {
					out = append(out, e)
				}
			}
		}
		return out
	}
	render := func(env ...corev1.EnvVar) (broker, exec []corev1.EnvVar) {
		agent := execTierTestAgent(ptr.To(int32(2)))
		agent.Spec.Deployment.Env = env
		dep := buildCredentialProxyDeployment(agent, "hash")
		return tiers(dep.Spec.Template.Spec), tiers(buildCredentialProxyExecTierDeployment(agent, dep).Spec.Template.Spec)
	}

	if broker, exec := render(); len(broker) != 0 || len(exec) != 0 {
		t.Errorf("%s set with no spec.deployment.env entry: broker %v, exec %v", envKubectlCostTier, broker, exec)
	}
	for value, want := range map[string]string{"fleet": "fleet", " Fleet ": "fleet", "large-2": "large-2"} {
		broker, exec := render(corev1.EnvVar{Name: envKubectlCostTier, Value: value})
		for tier, got := range map[string][]corev1.EnvVar{"broker": broker, "exec": exec} {
			if len(got) != 1 || got[0].Value != want || got[0].ValueFrom != nil {
				t.Errorf("%s on the %s tier = %v for the CR value %q; want exactly %q", envKubectlCostTier, tier, got, value, want)
			}
		}
	}
	for _, value := range []string{"", "../etc", "fleet tier", "\"fleet\"", "9fleet"} {
		if broker, exec := render(corev1.EnvVar{Name: envKubectlCostTier, Value: value}); len(broker) != 0 || len(exec) != 0 {
			t.Errorf("%s reached the broker for the CR value %q: broker %v, exec %v", envKubectlCostTier, value, broker, exec)
		}
	}
	fromConfigMap := corev1.EnvVar{Name: envKubectlCostTier, ValueFrom: &corev1.EnvVarSource{
		ConfigMapKeyRef: &corev1.ConfigMapKeySelector{LocalObjectReference: corev1.LocalObjectReference{Name: "c"}, Key: "k"},
	}}
	if broker, exec := render(fromConfigMap); len(broker) != 0 || len(exec) != 0 {
		t.Errorf("%s with a valueFrom reached the broker: broker %v, exec %v", envKubectlCostTier, broker, exec)
	}
}

func TestTheExecTierPinsSlotCapAcrossSizingProfiles(t *testing.T) {
	for _, tc := range []struct {
		name, profile, effectiveTier string
		wantStatefulSlots            string
	}{
		{"small", sizingProfileSmall, "", credentialProxyMaxConcurrentCommands},
		{"medium", sizingProfileMedium, "", credentialProxyMaxConcurrentCommands},
		{"fleet", sizingProfileFleet, "", credentialProxyMaxConcurrentCommands},
		{"auto-medium", sizingProfileAuto, sizingProfileMedium, credentialProxyMediumMaxConcurrentCommands},
		{"auto-fleet", sizingProfileAuto, sizingProfileFleet, credentialProxyFleetMaxConcurrentCommands},
	} {
		t.Run(tc.name, func(t *testing.T) {
			agent := execTierTestAgent(ptr.To(int32(1)))
			agent.Spec.Deployment.SizingProfile = tc.profile
			agent.Status.EffectiveSizingTier = tc.effectiveTier
			statefulDep := buildCredentialProxyDeployment(agent, brokerTestPolicyHash)
			var gotStateful string
			for _, c := range statefulDep.Spec.Template.Spec.Containers {
				if c.Name != credentialProxyContainerName {
					continue
				}
				for _, e := range c.Env {
					if e.Name == credentialProxyMaxConcurrentCommandsEnv {
						gotStateful = e.Value
					}
				}
			}
			if gotStateful != tc.wantStatefulSlots {
				t.Fatalf("stateful %s at %s = %q, want %q", credentialProxyMaxConcurrentCommandsEnv, tc.name, gotStateful, tc.wantStatefulSlots)
			}
			dep := buildCredentialProxyExecTierDeployment(agent, statefulDep)
			for _, c := range dep.Spec.Template.Spec.Containers {
				if c.Name != credentialProxyContainerName {
					continue
				}
				var found string
				for _, e := range c.Env {
					if e.Name == credentialProxyMaxConcurrentCommandsEnv {
						found = e.Value
					}
				}
				if found != credentialProxyMaxConcurrentCommands {
					t.Fatalf("exec %s at %s = %q, want %q", credentialProxyMaxConcurrentCommandsEnv, tc.name, found, credentialProxyMaxConcurrentCommands)
				}
			}
		})
	}
}
