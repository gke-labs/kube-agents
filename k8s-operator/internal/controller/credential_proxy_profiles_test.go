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
	"encoding/json"
	"reflect"
	"slices"
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

const testTaskSigningPublic = "CTESTTASKSIGNINGPUBLICKEYNOTAREALKEYXXXXXXXXXXXXXXXXXXXX"

func profileBrokerConfig() credentialProxyProfileConfig {
	return credentialProxyProfileConfig{
		TaskSigningPublic: testTaskSigningPublic,
		Identities: []credentialProxyProfileIdentity{
			{ServiceAccount: "system:serviceaccount:test-ns-profiles:agentprofile-auditor-read", Profile: "auditor", Tier: "read"},
			{ServiceAccount: "system:serviceaccount:test-ns-profiles:agentprofile-auditor-unattended", Profile: "auditor", Tier: "unattended"},
		},
	}
}

func brokerEnvCount(env []corev1.EnvVar, name string) int {
	n := 0
	for _, e := range env {
		if e.Name == name {
			n++
		}
	}
	return n
}

// With a profile identity the broker gets the profile audience, the identity
// map, the task-signing public key, and the identities on its callers list.
func TestCredentialProxyProfileEnvIsRendered(t *testing.T) {
	agent := a2aTestAgent()
	env := buildCredentialProxyEnvForPlugins(agent, nil, true, profileBrokerConfig())

	if got, _ := brokerEnvValue(env, credentialProxyProfileAudienceEnvVar); got != credentialProxyProfileAudience {
		t.Errorf("%s = %q, want %q", credentialProxyProfileAudienceEnvVar, got, credentialProxyProfileAudience)
	}
	if got, _ := brokerEnvValue(env, "A2A_TASK_SIGNING_PUBLIC_KEYS"); got != testTaskSigningPublic {
		t.Errorf("A2A_TASK_SIGNING_PUBLIC_KEYS = %q, want the task-signing public key", got)
	}
	raw, _ := brokerEnvValue(env, credentialProxyProfileIdentitiesEnvVar)
	var identities map[string]map[string]string
	if err := json.Unmarshal([]byte(raw), &identities); err != nil {
		t.Fatalf("%s is not JSON: %v (%q)", credentialProxyProfileIdentitiesEnvVar, err, raw)
	}
	want := map[string]map[string]string{
		"system:serviceaccount:test-ns-profiles:agentprofile-auditor-read":       {"profile": "auditor", "tier": "read"},
		"system:serviceaccount:test-ns-profiles:agentprofile-auditor-unattended": {"profile": "auditor", "tier": "unattended"},
	}
	if !reflect.DeepEqual(identities, want) {
		t.Errorf("identity map = %v, want %v", identities, want)
	}
	callers, _ := brokerEnvValue(env, "CREDENTIAL_PROXY_ALLOWED_CALLERS")
	for sa := range want {
		if !slices.Contains(strings.Split(callers, ","), sa) {
			t.Errorf("CREDENTIAL_PROXY_ALLOWED_CALLERS %q does not name %s", callers, sa)
		}
	}
	// The ceiling is #4121's to render; until then the broker's default.
	if _, ok := brokerEnvValue(env, "A2A_TASK_CREDENTIAL_MAX_LIFETIME"); ok {
		t.Error("A2A_TASK_CREDENTIAL_MAX_LIFETIME is rendered with no field to take it from")
	}
}

// Without a profile identity nothing about the role renders, and the callers
// list is what it was.
func TestCredentialProxyProfileEnvIsAbsentWithoutProfiles(t *testing.T) {
	agent := a2aTestAgent()
	env := buildCredentialProxyEnvForPlugins(agent, nil, true, credentialProxyProfileConfig{TaskSigningPublic: testTaskSigningPublic})
	for _, name := range []string{credentialProxyProfileAudienceEnvVar, credentialProxyProfileIdentitiesEnvVar, "A2A_TASK_SIGNING_PUBLIC_KEYS"} {
		if _, ok := brokerEnvValue(env, name); ok {
			t.Errorf("%s is rendered with no profile caller", name)
		}
	}
	callers, _ := brokerEnvValue(env, "CREDENTIAL_PROXY_ALLOWED_CALLERS")
	if callers != allowedBrokerCallers(agent, true) {
		t.Errorf("CREDENTIAL_PROXY_ALLOWED_CALLERS = %q, want the unchanged %q", callers, allowedBrokerCallers(agent, true))
	}
}

// The broker Deployment carries the key: the threading from the Deployment
// builder to the env builder.
func TestCredentialProxyDeploymentCarriesTheTaskSigningKey(t *testing.T) {
	dep := buildCredentialProxyDeploymentForPlugins(a2aTestAgent(), "hash", nil, true, profileBrokerConfig())
	env := dep.Spec.Template.Spec.Containers[0].Env
	if got, _ := brokerEnvValue(env, "A2A_TASK_SIGNING_PUBLIC_KEYS"); got != testTaskSigningPublic {
		t.Errorf("the broker Deployment's A2A_TASK_SIGNING_PUBLIC_KEYS = %q, want the task-signing public key", got)
	}
}

// A CR's spec.deployment.env can't choose the trusted key, the profile
// audience, the identity map or the lifetime, whether or not the operator
// renders them.
func TestCredentialProxyProfileEnvCannotBeOverridden(t *testing.T) {
	names := []string{
		"A2A_TASK_SIGNING_PUBLIC_KEYS",
		"A2A_TASK_CREDENTIAL_MAX_LIFETIME",
		credentialProxyProfileAudienceEnvVar,
		credentialProxyProfileIdentitiesEnvVar,
	}
	for _, cfg := range []credentialProxyProfileConfig{profileBrokerConfig(), {}} {
		agent := a2aTestAgent()
		agent.Spec.Deployment = &agentv1alpha1.DeploymentSpec{}
		for _, name := range names {
			agent.Spec.Deployment.Env = append(agent.Spec.Deployment.Env, corev1.EnvVar{Name: name, Value: "from-the-cr"})
		}
		env := buildCredentialProxyEnvForPlugins(agent, nil, true, cfg)
		for _, name := range names {
			if got, ok := brokerEnvValue(env, name); ok && got == "from-the-cr" {
				t.Errorf("profiles enabled=%v: spec.deployment.env set %s", cfg.enabled(), name)
			}
			if brokerEnvCount(env, name) > 1 {
				t.Errorf("profiles enabled=%v: %s rendered %d times", cfg.enabled(), name, brokerEnvCount(env, name))
			}
		}
		if cfg.enabled() {
			if got, _ := brokerEnvValue(env, "A2A_TASK_SIGNING_PUBLIC_KEYS"); got != testTaskSigningPublic {
				t.Errorf("the rendered key was replaced: %q", got)
			}
		}
	}
}

// The broker's fence admits the profile namespace only with a profile caller.
func TestCredentialProxyFenceAdmitsTheProfileNamespace(t *testing.T) {
	agent := a2aTestAgent()
	peer := profileNamespacePeer(agent)
	with := withCredentialProxyProfilePeer(buildCredentialProxyNetworkPolicy(agent), agent, profileBrokerConfig())
	if !slices.ContainsFunc(with.Spec.Ingress[0].From, func(p networkingv1.NetworkPolicyPeer) bool { return reflect.DeepEqual(p, peer) }) {
		t.Errorf("the broker's credentialed-port rule does not admit the profile namespace: %+v", with.Spec.Ingress[0].From)
	}
	if got := with.Spec.Ingress[0].Ports; len(got) != 1 || got[0].Port.IntVal != credentialProxyPort {
		t.Errorf("the profile peer is admitted on %+v, want the credentialed port alone", got)
	}
	without := withCredentialProxyProfilePeer(buildCredentialProxyNetworkPolicy(agent), agent, credentialProxyProfileConfig{})
	if !reflect.DeepEqual(without, buildCredentialProxyNetworkPolicy(agent)) {
		t.Error("the broker's fence changed with no profile caller")
	}
}

// The profile namespace's fence lets its pods out to the broker's
// credentialed port and nothing else of the broker's.
func TestProfileFenceReachesTheBroker(t *testing.T) {
	agent := a2aTestAgent()
	np := buildProfileNetworkPolicy(agent, nil)
	var found bool
	for _, rule := range np.Spec.Egress {
		for _, to := range rule.To {
			if to.PodSelector != nil && reflect.DeepEqual(to.PodSelector.MatchLabels, credentialProxySelector(agent)) {
				found = true
				if to.NamespaceSelector == nil || to.NamespaceSelector.MatchLabels["kubernetes.io/metadata.name"] != agent.Namespace {
					t.Errorf("the broker peer is not pinned to the agent's namespace: %+v", to.NamespaceSelector)
				}
				if len(rule.Ports) != 1 || rule.Ports[0].Port.IntVal != credentialProxyPort {
					t.Errorf("the broker rule opens %+v, want the credentialed port alone", rule.Ports)
				}
			}
		}
	}
	if !found {
		t.Errorf("the profile fence has no egress to the broker: %+v", np.Spec.Egress)
	}
}

// The broker's identity map comes from the callout map's profile entries, so
// the two name the same ServiceAccounts with the same (profile, tier).
func TestBrokerProfileIdentitiesMatchTheCalloutMap(t *testing.T) {
	agent := a2aTestAgent()
	profiles := []agentv1alpha1.AgentProfile{
		testAgentProfile(agent.Namespace, "auditor"),
		testAgentProfile(agent.Namespace, "fixer", func(p *agentv1alpha1.AgentProfile) {
			p.Spec.Identity.ServiceAccountName = "custom-fixer"
		}),
	}
	got := brokerProfileIdentities(agent, profiles)
	entries := agentProfileMapEntries(agent, profiles)
	if len(got) == 0 || len(got) != len(entries) {
		t.Fatalf("broker identities %+v, callout entries %+v", got, entries)
	}
	for i, e := range entries {
		if got[i] != (credentialProxyProfileIdentity{ServiceAccount: e.ServiceAccount, Profile: e.Profile, Tier: e.Tier}) {
			t.Errorf("identity %d = %+v, callout entry %+v", i, got[i], e)
		}
	}
	// A user-set ServiceAccount name is carried as named, not parsed.
	if !slices.ContainsFunc(got, func(id credentialProxyProfileIdentity) bool {
		return id.ServiceAccount == "system:serviceaccount:test-ns-profiles:custom-fixer" && id.Profile == "fixer"
	}) {
		t.Errorf("the user-named ServiceAccount is missing: %+v", got)
	}
}

func profileNamespaceObject(agent *agentv1alpha1.PlatformAgent) *corev1.Namespace {
	return &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{
		Name:        profileNamespaceName(agent),
		Labels:      map[string]string{profileNamespaceComponentLabel: profileNamespaceComponent},
		Annotations: map[string]string{helmReleaseNamespaceAnnotation: agent.Namespace},
	}}
}

// The reconciler reads the bound profiles and ensures the task-signing key,
// and the public half it renders is the one in the Secret.
func TestCredentialProxyProfileConfigFor(t *testing.T) {
	scheme := setupScheme()
	agent := a2aTestAgent()
	agent.UID = "agent-uid"
	profile := testAgentProfile(agent.Namespace, "auditor")
	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent, &profile, profileNamespaceObject(agent)).Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	ctx := context.Background()

	cfg, err := r.credentialProxyProfileConfigFor(ctx, agent)
	if err != nil {
		t.Fatalf("credentialProxyProfileConfigFor: %v", err)
	}
	if !cfg.enabled() {
		t.Fatalf("no identities for a bound profile: %+v", cfg)
	}
	var secret corev1.Secret
	if err := cl.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: a2aTaskSigningKeyName(agent)}, &secret); err != nil {
		t.Fatalf("the task-signing key Secret was not ensured: %v", err)
	}
	key, ok := readA2ATaskSigningKey(&secret)
	if !ok || key.Public != cfg.TaskSigningPublic || !strings.HasPrefix(cfg.TaskSigningPublic, "C") {
		t.Errorf("rendered key %q, Secret's public half %+v", cfg.TaskSigningPublic, key)
	}
}

// No profile namespace, no profiles, or a today install: nothing to render.
func TestCredentialProxyProfileConfigForRendersNothingWithoutProfiles(t *testing.T) {
	scheme := setupScheme()
	ctx := context.Background()

	agent := a2aTestAgent()
	profile := testAgentProfile(agent.Namespace, "auditor")
	noNamespace := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent, &profile).Build()
	if cfg, err := (&PlatformAgentReconciler{Client: noNamespace, Scheme: scheme}).credentialProxyProfileConfigFor(ctx, agent); err != nil || cfg.enabled() {
		t.Errorf("no profile namespace: %+v, %v", cfg, err)
	}

	noProfiles := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent, profileNamespaceObject(agent)).Build()
	if cfg, err := (&PlatformAgentReconciler{Client: noProfiles, Scheme: scheme}).credentialProxyProfileConfigFor(ctx, agent); err != nil || cfg.enabled() {
		t.Errorf("no profiles: %+v, %v", cfg, err)
	}

	today := a2aTestAgent()
	today.Spec.Mode = ptr.To("today")
	todayClient := fake.NewClientBuilder().WithScheme(scheme).WithObjects(today, &profile, profileNamespaceObject(today)).Build()
	if cfg, err := (&PlatformAgentReconciler{Client: todayClient, Scheme: scheme}).credentialProxyProfileConfigFor(ctx, today); err != nil || cfg.enabled() {
		t.Errorf("a today install: %+v, %v", cfg, err)
	}
}

// The broker's reconcile (step 11) ensures the task-signing key ahead of
// every other A2A write, so a next render that dies between the broker and
// reconcileA2A leaves that Secret alone. The flip to today must still tear it
// down: it is one of cleanupA2A's sentinels.
func TestTheFlipToTodayRemovesALoneTaskSigningKey(t *testing.T) {
	scheme := setupScheme()
	agent := a2aTestAgent()
	agent.UID = "agent-uid"
	profile := testAgentProfile(agent.Namespace, "auditor")
	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent, &profile, profileNamespaceObject(agent)).Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	ctx := context.Background()

	if _, err := r.credentialProxyProfileConfigFor(ctx, agent); err != nil {
		t.Fatalf("credentialProxyProfileConfigFor: %v", err)
	}
	key := types.NamespacedName{Namespace: agent.Namespace, Name: a2aTaskSigningKeyName(agent)}
	if err := cl.Get(ctx, key, &corev1.Secret{}); err != nil {
		t.Fatalf("the task-signing key Secret was not ensured: %v", err)
	}
	if err := r.cleanupA2A(ctx, agent); err != nil {
		t.Fatalf("cleanupA2A: %v", err)
	}
	if err := cl.Get(ctx, key, &corev1.Secret{}); !apierrors.IsNotFound(err) {
		t.Errorf("the task-signing key Secret survived the flip to today (err=%v): the early exit stepped over it", err)
	}
}
