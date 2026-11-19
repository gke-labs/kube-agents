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

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

func heldCluster(project, location, name string) agentv1alpha1.ScopeClusterRef {
	return agentv1alpha1.ScopeClusterRef{ProjectID: project, Location: location, ClusterName: name}
}

func TestTheHoldRendersItsOwnFile(t *testing.T) {
	agent := newTestPlatformAgent()
	if got := buildHoldConfigMap(agent).Data[holdKey]; got != "{}\n" {
		t.Errorf("no hold: hold.json = %q, want {}", got)
	}
	agent.Spec.Hold = &agentv1alpha1.HoldSpec{
		Clusters: []agentv1alpha1.ScopeClusterRef{heldCluster("p2", "us-east1", "b"), heldCluster("p1", "us-east1", "a")},
		Reason:   "INC-123",
	}
	cm := buildHoldConfigMap(agent)
	if cm.Name != agent.Name+"-hold" {
		t.Errorf("ConfigMap name = %q", cm.Name)
	}
	want := `{"clusters":[{"projectId":"p1","location":"us-east1","clusterName":"a"},{"projectId":"p2","location":"us-east1","clusterName":"b"}],"reason":"INC-123"}` + "\n"
	if got := cm.Data[holdKey]; got != want {
		t.Errorf("hold.json = %q, want %q", got, want)
	}
	agent.Spec.Hold = &agentv1alpha1.HoldSpec{All: true}
	if got := buildHoldConfigMap(agent).Data[holdKey]; got != `{"all":true}`+"\n" {
		t.Errorf("hold all: hold.json = %q", got)
	}
}

// A hold is not part of the config ConfigMap, which the agent pod's config-hash
// covers, nor of scope.json.
func TestTheHoldIsNotInTheConfigMap(t *testing.T) {
	for name, scope := range map[string]*agentv1alpha1.ScopeSpec{
		"no scope block": nil,
		"a scope":        {Projects: []string{"payments-prod"}},
	} {
		t.Run(name, func(t *testing.T) {
			without := newTestPlatformAgent()
			without.Spec.Scope = scope.DeepCopy()
			held := without.DeepCopy()
			held.Spec.Hold = &agentv1alpha1.HoldSpec{All: true, Reason: "incident"}
			if got, want := buildConfigMap(held, nil).Data, buildConfigMap(without, nil).Data; !reflect.DeepEqual(got, want) {
				t.Error("the config ConfigMap changes with a hold")
			}
		})
	}
}

func TestKubeProxyAndTheBrokerReadTheHold(t *testing.T) {
	agent := scopePublisherAgent()
	check := func(t *testing.T, who string, env []corev1.EnvVar, envName string, mounts []corev1.VolumeMount, volumes []corev1.Volume) {
		t.Helper()
		if file, _ := brokerEnvValue(env, envName); file != "/etc/kube-agents/hold/hold.json" {
			t.Errorf("%s: %s = %q", who, envName, file)
		}
		mounted, volume := false, false
		for _, m := range mounts {
			mounted = mounted || (m.Name == holdVolumeName && m.MountPath == "/etc/kube-agents/hold" && m.ReadOnly && m.SubPath == "")
		}
		for _, v := range volumes {
			volume = volume || (v.Name == holdVolumeName && v.ConfigMap != nil && v.ConfigMap.Name == agent.Name+"-hold" &&
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
func TestTheHoldLeavesTheProxyTemplates(t *testing.T) {
	without := scopePublisherAgent()
	held := without.DeepCopy()
	held.Spec.Hold = &agentv1alpha1.HoldSpec{Clusters: []agentv1alpha1.ScopeClusterRef{heldCluster("p1", "us-east1", "a")}, Reason: "INC-1"}
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
