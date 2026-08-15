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
	"encoding/json"
	"testing"

	corev1 "k8s.io/api/core/v1"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The exit criterion this file exists for is that the cluster-to-account
// mapping is visible in a rendered manifest rather than inferred from the
// broker's behaviour. So these assertions are about what an operator can read
// off `kubectl get configmap` and `kubectl get deployment`, not about internal
// helpers.

func scopedAgent(accounts ...agentv1alpha1.ScopedServiceAccount) *agentv1alpha1.PlatformAgent {
	agent := splitBrokerAgent(false)
	if agent.Spec.Security == nil {
		agent.Spec.Security = &agentv1alpha1.SecuritySpec{}
	}
	agent.Spec.Security.ScopedServiceAccounts = accounts
	return agent
}

func account(project, location, cluster, email string) agentv1alpha1.ScopedServiceAccount {
	return agentv1alpha1.ScopedServiceAccount{
		ProjectID:           project,
		Location:            location,
		ClusterName:         cluster,
		ServiceAccountEmail: email,
	}
}

type renderedPool struct {
	Version         int `json:"version"`
	ServiceAccounts []struct {
		ProjectID           string `json:"projectId"`
		Location            string `json:"location"`
		ClusterName         string `json:"clusterName"`
		ServiceAccountEmail string `json:"serviceAccountEmail"`
	} `json:"serviceAccounts"`
}

// Counts as well as reads, unlike the (string, bool) envValue beside it: the
// plugin-override case is specifically about a second copy of a variable being
// appended, and a helper returning the first match would report success on
// exactly the input that matters.
func envValueCount(envVars []corev1.EnvVar, name string) (string, int) {
	value, count := "", 0
	for _, env := range envVars {
		if env.Name == name {
			value, count = env.Value, count+1
		}
	}
	return value, count
}

func TestTheMappingIsRenderedIntoAConfigMapTheBrokerCanRead(t *testing.T) {
	agent := scopedAgent(
		account("proj", "us-central1", "cluster", "ka-cluster-1a2b3c4d@proj.iam.gserviceaccount.com"),
	)

	cm := buildCredentialProxyPolicyConfigMap(agent)
	raw, ok := cm.Data[scopedSAPoolKey]
	if !ok {
		t.Fatalf("no %s key in the rendered ConfigMap; keys were %v", scopedSAPoolKey, cm.Data)
	}

	var pool renderedPool
	if err := json.Unmarshal([]byte(raw), &pool); err != nil {
		t.Fatalf("the rendered mapping is not the JSON the broker parses: %v (%s)", err, raw)
	}
	if pool.Version != 1 {
		t.Errorf("version = %d, want 1", pool.Version)
	}
	if len(pool.ServiceAccounts) != 1 {
		t.Fatalf("got %d accounts, want 1", len(pool.ServiceAccounts))
	}
	entry := pool.ServiceAccounts[0]
	if entry.ProjectID != "proj" || entry.Location != "us-central1" || entry.ClusterName != "cluster" {
		t.Errorf("the cluster tuple did not survive rendering: %+v", entry)
	}
	if entry.ServiceAccountEmail != "ka-cluster-1a2b3c4d@proj.iam.gserviceaccount.com" {
		t.Errorf("serviceAccountEmail = %q", entry.ServiceAccountEmail)
	}
}

func TestTheMappingRendersInAStableOrder(t *testing.T) {
	// The ConfigMap is hashed into the Pod template annotation, so an unstable
	// render would roll the broker every reconcile — and because the broker
	// reads this file only at startup, a rollout loop here is not cosmetic.
	forward := scopedAgent(
		account("a-proj", "us-central1", "a-cluster", "ka-a-11111111@a-proj.iam.gserviceaccount.com"),
		account("b-proj", "europe-west1", "b-cluster", "ka-b-22222222@b-proj.iam.gserviceaccount.com"),
		account("a-proj", "us-central1", "z-cluster", "ka-z-33333333@a-proj.iam.gserviceaccount.com"),
	)
	reversed := scopedAgent(
		agentv1alpha1.ScopedServiceAccount(forward.Spec.Security.ScopedServiceAccounts[2]),
		agentv1alpha1.ScopedServiceAccount(forward.Spec.Security.ScopedServiceAccounts[1]),
		agentv1alpha1.ScopedServiceAccount(forward.Spec.Security.ScopedServiceAccounts[0]),
	)

	first := buildCredentialProxyPolicyConfigMap(forward).Data[scopedSAPoolKey]
	second := buildCredentialProxyPolicyConfigMap(reversed).Data[scopedSAPoolKey]
	if first != second {
		t.Errorf("reordering the CR changed the rendered mapping:\n %s\n %s", first, second)
	}

	var pool renderedPool
	if err := json.Unmarshal([]byte(first), &pool); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	want := []string{"a-cluster", "z-cluster", "b-cluster"}
	for i, cluster := range want {
		if pool.ServiceAccounts[i].ClusterName != cluster {
			t.Errorf("entry %d is %q, want %q (sorted by scope key, so project orders before cluster)",
				i, pool.ServiceAccounts[i].ClusterName, cluster)
		}
	}
}

func TestTheScopeKeyMatchesTheOneTheBrokerAndTerraformUse(t *testing.T) {
	// Three implementations of one string: here, `scoped_sa_pool.scope_key` in
	// the broker, and the IAM Condition operand in the Terraform module. The
	// Python suite compares its two; this pins the third. Written as a literal
	// rather than by calling the function under test, which would only assert
	// the function agrees with itself.
	if got := scopedSAPoolScopeKey("proj", "us-central1", "cluster"); got != "projects/proj/locations/us-central1/clusters/cluster" {
		t.Errorf("scope key = %q; the broker will not find this entry", got)
	}
}

func TestWithNoAccountsThereIsNoMappingAndTheFlagSaysSo(t *testing.T) {
	agent := scopedAgent()

	if _, ok := buildCredentialProxyPolicyConfigMap(agent).Data[scopedSAPoolKey]; ok {
		t.Errorf("a mapping was rendered for an agent that configured none")
	}

	envVars := buildCredentialProxyEnv(agent)
	// Explicitly "0", not absent. The broker arms the pool by default, so an
	// absent variable would mean "refuse to start" — correct behaviour, but
	// diagnosed by reading Python rather than by reading the Deployment.
	if value, count := envValueCount(envVars, "CREDENTIAL_PROXY_SCOPED_SA_POOL"); value != "0" || count != 1 {
		t.Errorf("CREDENTIAL_PROXY_SCOPED_SA_POOL = %q (x%d), want exactly one \"0\"", value, count)
	}
	if _, count := envValueCount(envVars, "CREDENTIAL_PROXY_SCOPED_SA_POOL_FILE"); count != 0 {
		t.Errorf("a pool file was named for an agent with no pool")
	}

	sidecar := buildCredentialProxySidecar(agent, "/opt/data")
	for _, mount := range sidecar.VolumeMounts {
		if mount.SubPath == scopedSAPoolKey {
			t.Errorf("the pool is mounted by SubPath but the ConfigMap has no such key; the container cannot start")
		}
	}
}

func TestWithAccountsTheFlagAndTheMountAppearTogether(t *testing.T) {
	agent := scopedAgent(
		account("proj", "us-central1", "cluster", "ka-cluster-1a2b3c4d@proj.iam.gserviceaccount.com"),
	)

	envVars := buildCredentialProxyEnv(agent)
	if value, count := envValueCount(envVars, "CREDENTIAL_PROXY_SCOPED_SA_POOL"); value != "1" || count != 1 {
		t.Errorf("CREDENTIAL_PROXY_SCOPED_SA_POOL = %q (x%d), want exactly one \"1\"", value, count)
	}
	path, count := envValueCount(envVars, "CREDENTIAL_PROXY_SCOPED_SA_POOL_FILE")
	if count != 1 || path != scopedSAPoolMountPath {
		t.Errorf("CREDENTIAL_PROXY_SCOPED_SA_POOL_FILE = %q (x%d), want %q", path, count, scopedSAPoolMountPath)
	}

	sidecar := buildCredentialProxySidecar(agent, "/opt/data")
	var mounted *corev1.VolumeMount
	for i := range sidecar.VolumeMounts {
		if sidecar.VolumeMounts[i].MountPath == path {
			mounted = &sidecar.VolumeMounts[i]
		}
	}
	if mounted == nil {
		t.Fatalf("the broker is told to read %s and nothing mounts it there", path)
	}
	if mounted.SubPath != scopedSAPoolKey || mounted.Name != "credential-proxy-policy" {
		t.Errorf("mount = %+v, want SubPath %q on the policy ConfigMap volume", *mounted, scopedSAPoolKey)
	}
	if !mounted.ReadOnly {
		t.Errorf("the mapping is mounted writable; it is the list of identities the broker may become")
	}
}

func TestAPluginCannotDisableTheScopedServiceAccountPool(t *testing.T) {
	// Same argument as TestAPluginCannotDisableCallerAuthentication. A plugin
	// that could set these would put the broker back on the agent's own
	// project-wide identity, or point it at a mapping naming an account it
	// would rather be — either of which is the whole control, switched off by
	// a field further down the same CR.
	agent := scopedAgent(
		account("proj", "us-central1", "cluster", "ka-cluster-1a2b3c4d@proj.iam.gserviceaccount.com"),
	)
	agent.Spec.Deployment = &agentv1alpha1.DeploymentSpec{Env: []corev1.EnvVar{
		{Name: "CREDENTIAL_PROXY_SCOPED_SA_POOL", Value: "0"},
		{Name: "CREDENTIAL_PROXY_SCOPED_SA_POOL_FILE", Value: "/tmp/mine.json"},
	}}

	envVars := buildCredentialProxyEnv(agent)
	for name, want := range map[string]string{
		"CREDENTIAL_PROXY_SCOPED_SA_POOL":      "1",
		"CREDENTIAL_PROXY_SCOPED_SA_POOL_FILE": scopedSAPoolMountPath,
	} {
		value, count := envValueCount(envVars, name)
		if count != 1 || value != want {
			t.Errorf("%s = %q (x%d), want exactly one %q", name, value, count, want)
		}
	}
}

func TestTheSplitBrokerAlsoGetsTheMapping(t *testing.T) {
	// The split moves the broker into its own Pod, and it is the layout the
	// security model is heading for. A control that only landed on the sidecar
	// would be one nobody noticed missing after the migration.
	agent := splitBrokerAgent(true)
	agent.Spec.Security.ScopedServiceAccounts = []agentv1alpha1.ScopedServiceAccount{
		account("proj", "us-central1", "cluster", "ka-cluster-1a2b3c4d@proj.iam.gserviceaccount.com"),
	}

	envVars := buildCredentialProxyEnv(agent)
	if value, _ := envValueCount(envVars, "CREDENTIAL_PROXY_SCOPED_SA_POOL"); value != "1" {
		t.Errorf("the split broker does not arm the pool: %q", value)
	}
	sidecar := buildCredentialProxySidecar(agent, "/opt/data")
	found := false
	for _, mount := range sidecar.VolumeMounts {
		if mount.SubPath == scopedSAPoolKey {
			found = true
		}
	}
	if !found {
		t.Errorf("the split broker is armed with no mapping mounted; it will refuse to start")
	}
}
