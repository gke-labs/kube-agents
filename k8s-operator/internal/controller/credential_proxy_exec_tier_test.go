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
	"strings"
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	policyv1 "k8s.io/api/policy/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/labels"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

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
	if ru := dep.Spec.Strategy.RollingUpdate; ru == nil || ru.MaxUnavailable == nil || ru.MaxUnavailable.IntValue() != 1 {
		t.Errorf("rollout %+v, want maxUnavailable 1 so a full namespace quota cannot stall it", dep.Spec.Strategy.RollingUpdate)
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
	for _, name := range []string{"TOKEN_BROKER_URL", "GOOGLE_CHAT_SUBSCRIPTION_NAME", "CREDENTIAL_PROXY_CONTENT_WORKSPACE"} {
		if _, ok := env[name]; ok {
			t.Errorf("exec pod carries %s", name)
		}
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
	if broker.Spec.Template.Labels["kubeagents.x-k8s.io/has-credential-proxy"] != "true" || len(bc.EnvFrom) == 0 {
		t.Error("deriving the exec tier changed the broker's Deployment")
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
	if !kept["credential-proxy-ksa-token"] || !kept["credential-proxy-policy"] {
		t.Error("exec pod lost a volume that holds no Secret")
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
