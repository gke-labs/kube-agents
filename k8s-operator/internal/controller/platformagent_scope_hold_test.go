/*
Copyright 2026 Google LLC.

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
	"testing"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/utils/ptr"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

func heldCluster(project, location, name string) agentv1alpha1.ScopeClusterRef {
	return agentv1alpha1.ScopeClusterRef{ProjectID: project, Location: location, ClusterName: name}
}

func TestTheScopeHoldRendersItsOwnFile(t *testing.T) {
	agent := newTestPlatformAgent()
	if got := buildScopeHoldConfigMap(agent).Data[scopeHoldKey]; got != "{}\n" {
		t.Errorf("no scope block: hold.json = %q, want {}", got)
	}
	agent.Spec.Scope = &agentv1alpha1.ScopeSpec{Hold: &agentv1alpha1.ScopeHoldSpec{
		Clusters: []agentv1alpha1.ScopeClusterRef{heldCluster("p2", "us-east1", "b"), heldCluster("p1", "us-east1", "a")},
		Reason:   "INC-123",
	}}
	cm := buildScopeHoldConfigMap(agent)
	if cm.Name != agent.Name+"-scope-hold" {
		t.Errorf("ConfigMap name = %q", cm.Name)
	}
	want := `{"clusters":[{"projectId":"p1","location":"us-east1","clusterName":"a"},{"projectId":"p2","location":"us-east1","clusterName":"b"}],"reason":"INC-123"}` + "\n"
	if got := cm.Data[scopeHoldKey]; got != want {
		t.Errorf("hold.json = %q, want %q", got, want)
	}
	agent.Spec.Scope.Hold = &agentv1alpha1.ScopeHoldSpec{All: true}
	if got := buildScopeHoldConfigMap(agent).Data[scopeHoldKey]; got != `{"all":true}`+"\n" {
		t.Errorf("hold all: hold.json = %q", got)
	}
}

// A hold is not part of scope.json: it would move the agent pod's
// config-hash and roll it. A hold patched onto a CR with no scope block
// (where the API server also defaults maxProjects) stays "no declaration".
func TestTheScopeHoldIsNotInTheScopeDeclaration(t *testing.T) {
	hold := &agentv1alpha1.ScopeHoldSpec{All: true, Reason: "incident"}
	for name, scope := range map[string]*agentv1alpha1.ScopeSpec{
		"no block, the cap defaulted": nil,
		"projects":                    {Projects: []string{"payments-prod"}},
		"exclusions":                  {Exclude: &agentv1alpha1.ScopeExcludeSpec{Projects: []string{}, Clusters: []agentv1alpha1.ScopeClusterRef{}}},
		"a cap off the default":       {MaxProjects: ptr.To(int32(250))},
		"the installer's render":      {Projects: []string{}, Exclude: &agentv1alpha1.ScopeExcludeSpec{}},
	} {
		t.Run(name, func(t *testing.T) {
			without := newTestPlatformAgent()
			without.Spec.Scope = scope.DeepCopy()
			held := newTestPlatformAgent()
			if scope == nil {
				held.Spec.Scope = &agentv1alpha1.ScopeSpec{MaxProjects: ptr.To(defaultScopeMaxProjects)}
			} else {
				held.Spec.Scope = scope.DeepCopy()
			}
			held.Spec.Scope.Hold = hold.DeepCopy()
			if got, want := renderScopeJSON(held), renderScopeJSON(without); got != want {
				t.Errorf("scope.json with a hold:\n%s\nwithout:\n%s", got, want)
			}
			if got, want := buildConfigMap(held, nil).Data, buildConfigMap(without, nil).Data; !reflect.DeepEqual(got, want) {
				t.Error("the config ConfigMap, which the agent pod's config-hash covers, changes with a hold")
			}
			if (scopeForDeclaration(held) == nil) != (scope == nil) {
				t.Errorf("declared = %v, want %v", scopeForDeclaration(held) != nil, scope != nil)
			}
		})
	}
}

func TestKubeProxyAndTheBrokerReadTheScopeHold(t *testing.T) {
	agent := scopePublisherAgent()
	check := func(t *testing.T, who string, env []corev1.EnvVar, envName string, mounts []corev1.VolumeMount, volumes []corev1.Volume) {
		t.Helper()
		if file, _ := brokerEnvValue(env, envName); file != "/etc/kube-agents/hold/hold.json" {
			t.Errorf("%s: %s = %q", who, envName, file)
		}
		mounted, volume := false, false
		for _, m := range mounts {
			mounted = mounted || (m.Name == scopeHoldVolumeName && m.MountPath == "/etc/kube-agents/hold" && m.ReadOnly && m.SubPath == "")
		}
		for _, v := range volumes {
			volume = volume || (v.Name == scopeHoldVolumeName && v.ConfigMap != nil && v.ConfigMap.Name == agent.Name+"-scope-hold" &&
				v.ConfigMap.Optional != nil && *v.ConfigMap.Optional)
		}
		if !mounted || !volume {
			t.Errorf("%s: hold mounted %v, volume %v", who, mounted, volume)
		}
	}
	proxy := buildKubeProxyDeployment(agent, "", true, "tls-hash")
	pc := proxy.Spec.Template.Spec.Containers[0]
	check(t, "kube-proxy", pc.Env, "KUBE_PROXY_HOLD_FILE", pc.VolumeMounts, proxy.Spec.Template.Spec.Volumes)
	broker := buildCredentialProxyDeploymentForPlugins(agent, "hash", nil, allBrokerCallersOwned, credentialProxyProfileConfig{}, false)
	bc := broker.Spec.Template.Spec.Containers[0]
	check(t, "broker", bc.Env, "CREDENTIAL_PROXY_HOLD_FILE", bc.VolumeMounts, broker.Spec.Template.Spec.Volumes)
}

// Setting, changing or lifting a hold changes neither kube-proxy's nor the
// broker's pod template.
func TestTheScopeHoldLeavesTheProxyTemplates(t *testing.T) {
	without := scopePublisherAgent()
	held := scopePublisherAgent()
	held.Spec.Scope.Hold = &agentv1alpha1.ScopeHoldSpec{Clusters: []agentv1alpha1.ScopeClusterRef{heldCluster("p1", "us-east1", "a")}, Reason: "INC-1"}
	if !reflect.DeepEqual(buildKubeProxyDeployment(held, "", true, "h").Spec.Template, buildKubeProxyDeployment(without, "", true, "h").Spec.Template) {
		t.Error("kube-proxy's pod template changes with a hold")
	}
	a := buildCredentialProxyDeploymentForPlugins(held, "hash", nil, allBrokerCallersOwned, credentialProxyProfileConfig{}, false)
	b := buildCredentialProxyDeploymentForPlugins(without, "hash", nil, allBrokerCallersOwned, credentialProxyProfileConfig{}, false)
	if !reflect.DeepEqual(a.Spec.Template, b.Spec.Template) {
		t.Error("the broker's pod template changes with a hold")
	}
}

func TestTheReservedListNamesTheHoldFile(t *testing.T) {
	merged := mergeCredentialProxyEnv(nil, []corev1.EnvVar{{Name: "CREDENTIAL_PROXY_HOLD_FILE", Value: "/dev/null"}})
	if _, count := envValueCount(merged, "CREDENTIAL_PROXY_HOLD_FILE"); count != 0 {
		t.Error("CREDENTIAL_PROXY_HOLD_FILE survived the merge from spec.deployment.env")
	}
}

// The shell sandbox's collectors read a hold-only block as no declaration too.
func TestTheShellSandboxReadsAHoldOnlyBlockAsUndeclared(t *testing.T) {
	agent := shellSandboxTestAgent()
	agent.Spec.Scope = &agentv1alpha1.ScopeSpec{MaxProjects: ptr.To(defaultScopeMaxProjects), Hold: &agentv1alpha1.ScopeHoldSpec{All: true}}
	for _, env := range buildShellSandboxStatefulSet(agent, "sandbox-ssh", "", "settings-hash").Spec.Template.Spec.Containers[0].Env {
		if env.Name == envScopeDeclared && env.Value != "false" {
			t.Errorf("%s = %q for a block that holds nothing but a hold, want false", envScopeDeclared, env.Value)
		}
	}
}
