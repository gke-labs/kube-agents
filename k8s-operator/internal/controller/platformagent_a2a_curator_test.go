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
	"slices"
	"strings"
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
)

func TestBuildA2ACuratorDeploymentIsCredentialFreeAndHardened(t *testing.T) {
	agent := a2aTestAgent()
	dep := buildA2ACuratorDeployment(agent)

	if dep.Name != agent.Name+"-curator" {
		t.Fatalf("Deployment name = %q, want %q", dep.Name, agent.Name+"-curator")
	}
	if dep.Spec.Strategy.Type != appsv1.RecreateDeploymentStrategyType {
		t.Errorf("Strategy = %q, want Recreate", dep.Spec.Strategy.Type)
	}
	if got := dep.Spec.Template.Labels["kubeagents.x-k8s.io/component"]; got != "curator" {
		t.Errorf("pod component label = %q, want curator", got)
	}
	if got := dep.Spec.Template.Labels["app"]; got != dep.Name {
		t.Errorf("pod app label = %q, want %q", got, dep.Name)
	}

	pod := dep.Spec.Template.Spec
	if pod.ServiceAccountName != dep.Name {
		t.Errorf("ServiceAccountName = %q, want %q", pod.ServiceAccountName, dep.Name)
	}
	if pod.AutomountServiceAccountToken == nil || *pod.AutomountServiceAccountToken {
		t.Errorf("AutomountServiceAccountToken = %v, want false", pod.AutomountServiceAccountToken)
	}
	for _, vol := range pod.Volumes {
		if vol.Secret != nil {
			t.Errorf("curator pod mounts Secret volume %q (%s); curator must hold zero Secrets", vol.Name, vol.Secret.SecretName)
		}
		if vol.Projected != nil {
			for _, src := range vol.Projected.Sources {
				if src.Secret != nil {
					t.Errorf("curator pod projects Secret %s; curator must hold zero Secrets", src.Secret.Name)
				}
				if src.ServiceAccountToken != nil && src.ServiceAccountToken.Audience != a2aBusTokenAudience {
					t.Errorf("curator projected token audience = %q, want %q (never credential-proxy)", src.ServiceAccountToken.Audience, a2aBusTokenAudience)
				}
			}
		}
	}

	if len(pod.Containers) != 1 {
		t.Fatalf("containers = %d, want 1", len(pod.Containers))
	}
	ctr := pod.Containers[0]
	wantCmd := []string{"/curator", "daemon", "--repo", "/var/lib/curator/knowledge-store"}
	if !slices.Equal(ctr.Command, wantCmd) {
		t.Errorf("Command = %v, want %v", ctr.Command, wantCmd)
	}
	if ctr.WorkingDir != "/" {
		t.Errorf("WorkingDir = %q, want /", ctr.WorkingDir)
	}
	if ctr.ReadinessProbe == nil || ctr.ReadinessProbe.HTTPGet == nil || ctr.ReadinessProbe.HTTPGet.Path != "/readyz" {
		t.Errorf("ReadinessProbe = %+v, want HTTPGet /readyz", ctr.ReadinessProbe)
	}
	if ctr.LivenessProbe == nil || ctr.LivenessProbe.HTTPGet == nil || ctr.LivenessProbe.HTTPGet.Path != "/healthz" {
		t.Errorf("LivenessProbe = %+v, want HTTPGet /healthz", ctr.LivenessProbe)
	}
	for _, env := range ctr.Env {
		if env.ValueFrom != nil && env.ValueFrom.SecretKeyRef != nil {
			t.Errorf("env %s references Secret %s", env.Name, env.ValueFrom.SecretKeyRef.Name)
		}
	}
}

func TestBuildA2ACuratorNetworkPolicyRestrictsIngressAndEgress(t *testing.T) {
	agent := a2aTestAgent()
	np := buildA2ACuratorNetworkPolicy(agent, []string{"10.96.0.10"})

	if np.Name != agent.Name+"-curator-netpol" {
		t.Fatalf("NetworkPolicy name = %q, want %q", np.Name, agent.Name+"-curator-netpol")
	}
	if len(np.Spec.Ingress) != 0 {
		t.Errorf("Ingress rules = %d, want 0 (deny all ingress)", len(np.Spec.Ingress))
	}
	wantTypes := []networkingv1.PolicyType{networkingv1.PolicyTypeIngress, networkingv1.PolicyTypeEgress}
	if !slices.Equal(np.Spec.PolicyTypes, wantTypes) {
		t.Errorf("PolicyTypes = %v, want %v", np.Spec.PolicyTypes, wantTypes)
	}
	if len(np.Spec.Egress) != 4 {
		t.Fatalf("Egress rules = %d, want 4 (DNS, NATS, LiteLLM, Hindsight)", len(np.Spec.Egress))
	}
	for _, rule := range np.Spec.Egress {
		for _, p := range rule.Ports {
			if p.Port != nil && p.Port.IntVal == 443 {
				t.Errorf("curator NetworkPolicy grants port 443 egress; curator must not dial K8s API or external HTTPS")
			}
		}
		for _, peer := range rule.To {
			if peer.PodSelector != nil {
				labels := peer.PodSelector.MatchLabels
				if strings.Contains(labels["app"], "credential-proxy") || strings.Contains(labels["app"], "kube-proxy") {
					t.Errorf("curator NetworkPolicy grants egress to %v; curator must not reach credential-proxy or kube-proxy", labels)
				}
			}
		}
	}
}

func TestCuratorFlagControlsAuthMapAndNATSPeers(t *testing.T) {
	agent := a2aTestAgent()

	t.Setenv(a2aCuratorEnvVar, "")
	if peers := a2aCuratorNATSPeers(agent); len(peers) != 0 {
		t.Errorf("flag off: a2aCuratorNATSPeers = %v, want empty", peers)
	}
	docOff, err := renderA2AAuthMap(agent, nil)
	if err != nil {
		t.Fatalf("renderA2AAuthMap (flag off): %v", err)
	}
	for _, id := range docOff.Identities {
		if id.User == a2aCuratorUser {
			t.Errorf("flag off: authmap unexpectedly includes %q", a2aCuratorUser)
		}
	}

	t.Setenv(a2aCuratorEnvVar, "true")
	if peers := a2aCuratorNATSPeers(agent); len(peers) != 1 {
		t.Fatalf("flag on: a2aCuratorNATSPeers = %d, want 1", len(peers))
	}
	natsNP := buildA2ANATSNetworkPolicy(agent)
	foundPeer := false
	for _, p := range natsNP.Spec.Ingress[0].From {
		if p.PodSelector != nil && p.PodSelector.MatchLabels["app"] == a2aCuratorName(agent) {
			foundPeer = true
		}
	}
	if !foundPeer {
		t.Errorf("flag on: buildA2ANATSNetworkPolicy 4222 From does not include curator pod selector")
	}

	docOn, err := renderA2AAuthMap(agent, nil)
	if err != nil {
		t.Fatalf("renderA2AAuthMap (flag on): %v", err)
	}
	var cur *a2aAuthMapIdentity
	for i := range docOn.Identities {
		if docOn.Identities[i].User == a2aCuratorUser {
			cur = &docOn.Identities[i]
			break
		}
	}
	if cur == nil {
		t.Fatalf("flag on: authmap missing %q identity", a2aCuratorUser)
	}
	if cur.Account != a2aAccountApp {
		t.Errorf("curator account = %q, want %q", cur.Account, a2aAccountApp)
	}
	wantSA := a2aServiceAccountName(agent.Namespace, a2aCuratorName(agent))
	if cur.ServiceAccount != wantSA {
		t.Errorf("curator serviceAccount = %q, want %q", cur.ServiceAccount, wantSA)
	}
	for _, g := range cur.Grants.Publish {
		if strings.Contains(g, "CONSUMER.CREATE") || strings.HasPrefix(g, "a2a.tasks.") {
			t.Errorf("curator holds forbidden publish grant %q", g)
		}
	}

	// Verify ServiceAccount has zero RBAC bindings and is reserved from AgentProfiles.
	sa := buildA2ACuratorServiceAccount(agent)
	if sa.Name != a2aCuratorName(agent) || sa.Kind != (&corev1.ServiceAccount{TypeMeta: sa.TypeMeta}).Kind {
		t.Errorf("ServiceAccount = %+v", sa)
	}
	if _, reserved := reservedProfileServiceAccounts(agent)[a2aCuratorName(agent)]; !reserved {
		t.Errorf("reservedProfileServiceAccounts does not reserve %q", a2aCuratorName(agent))
	}
}
