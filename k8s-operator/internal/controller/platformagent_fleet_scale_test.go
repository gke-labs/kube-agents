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
	"fmt"
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The fleet-scale size gate (#4440): the operator-rendered ConfigMaps whose
// size grows with the fleet, measured at the fleet the gate names, against the
// API server's object limit.
//
// Three objects grow past the limit today, so the checks below are inverted:
// each asserts its object is STILL over the limit and fails, naming the fix
// issue, the moment a change brings it under. The fix pull request drops the
// issue from the requireConfigMapSizeGate call in the same diff, which turns
// it into the forward check, and that is how the gate records the regression on main without
// holding every other pull request red, and how the fix proves itself. A
// forward check that goes red on main would be turned off; an inverted one
// turns itself into the forward check at the moment the fix lands.
//
// The renderers are called directly rather than through a full reconcile:
// 2,500 profiles through the reconciler mean thousands of ServiceAccounts and
// bindings in the fake client, and the size of a rendered object is a
// property of the renderer alone.
const (
	// fleetScaleRepositories is R: the registered repository count the
	// minter policy ConfigMap is measured at.
	fleetScaleRepositories = 500
	// fleetScaleProfiles is N: the bound AgentProfile count the identity map
	// and the dispatch config are measured at.
	fleetScaleProfiles = 2500
	// configMapSizeLimit is the API server's bound on a ConfigMap's data
	// (core.MaxSecretSize in kube-apiserver's validation, applied to the sum
	// of every data value): a larger object is refused on write.
	configMapSizeLimit = 1 << 20
	// configMapSizeWarning is where the forward check starts logging: the
	// headroom the gate asks for below the limit.
	configMapSizeWarning = 900 << 10
	// fleetScaleOrg is the one GitHub organisation every fixture repository
	// belongs to, so the minter claims every one of them.
	fleetScaleOrg = "fleet-org"
	// fleetScaleRepoURLFormat and fleetScaleProfileNameFormat name the fixtures;
	// four digits keep the names sorted the way they are numbered.
	fleetScaleRepoURLFormat     = "https://github.com/" + fleetScaleOrg + "/repo-%04d"
	fleetScaleProfileNameFormat = "cluster-%04d"
	// The fix issues the inverted checks name: the minter policy, the identity
	// map and the dispatch config shrink under #4431.
	minterConfigMapFixIssue   = "#4431"
	identityMapFixIssue       = "#4431"
	dispatchConfigMapFixIssue = "#4431"
	// fleetScaleBaseTemplate is default.yaml as the chart renders it
	// (charts/kube-agents/templates/github-minter.yaml): three scopes, each
	// with a repository list the sync rewrites to the full managed set.
	fleetScaleBaseTemplate = `version: 'minty.abcxyz.dev/v2'
rule:
  if: "assertion.iss == 'https://accounts.google.com'"
scope:
  platform-agent-scope:
    rule:
      if: "assertion.email in ['kubeagents-platform-gsa@fleet-project.iam.gserviceaccount.com']"
    repositories:
      - 'fleet-gitops'
    permissions:
      contents: 'write'
      pull_requests: 'write'
      issues: 'write'
  platform-agent-read-scope:
    rule:
      if: "assertion.email in ['kubeagents-platform-gsa@fleet-project.iam.gserviceaccount.com']"
    repositories:
      - 'fleet-gitops'
    permissions:
      contents: 'read'
  platform-agent-sweep-scope:
    rule:
      if: "assertion.email in ['kubeagents-platform-gsa@fleet-project.iam.gserviceaccount.com']"
    repositories:
      - 'fleet-gitops'
    permissions:
      contents: 'read'
      pull_requests: 'read'
      issues: 'read'
`
)

// fleetScaleSubscribeTopics are the subscribe topics the chart's shipped
// profile carries (charts/kube-agents/values.yaml, agentProfiles.tse.bus), so
// the identity map is measured at the entry shape an install renders. The
// verdict depends on it: with no topics the map at N=2,500 measures under the
// limit, so a change to this fixture is a change to what the check says.
var fleetScaleSubscribeTopics = []string{"shared.blueprint", "agent.platform.upgrade-readiness"}

// configMapDataSize is what the API server measures a ConfigMap by: the sum of
// its data values. The serialized object is larger by its metadata and keys,
// and is logged beside it for the reader; the limit applies to this number.
func configMapDataSize(cm *corev1.ConfigMap) int {
	size := 0
	for _, v := range cm.Data {
		size += len(v)
	}
	for _, v := range cm.BinaryData {
		size += len(v)
	}
	return size
}

// serializedConfigMapSize is the object as the API server would receive it.
func serializedConfigMapSize(t *testing.T, cm *corev1.ConfigMap) int {
	t.Helper()
	body, err := json.Marshal(cm)
	if err != nil {
		t.Fatalf("serializing %s: %v", cm.Name, err)
	}
	return len(body)
}

// requireConfigMapSizeGate is the gate on one rendered object. With fixIssue
// empty it is the forward check: the object fits under the limit, and a
// warning is logged once it passes the headroom threshold. With fixIssue set
// it is inverted: the object is over the limit on main today, that issue owns
// the fix, and a render that fits is the fix landing without flipping the
// check. The fix pull request drops the issue from the call in the same diff.
func requireConfigMapSizeGate(t *testing.T, cm *corev1.ConfigMap, fixture, fixIssue string) {
	t.Helper()
	size := configMapDataSize(cm)
	t.Logf("%s at %s: %d bytes of data, %d bytes serialized (limit %d, warning above %d)",
		cm.Name, fixture, size, serializedConfigMapSize(t, cm), configMapSizeLimit, configMapSizeWarning)
	if fixIssue != "" {
		if size <= configMapSizeLimit {
			t.Fatalf("%s at %s is %d bytes of data, under the %d-byte ConfigMap limit. This check is inverted because the render was over the limit on main and %s owns the fix: "+
				"the change that brought it under passes an empty fixIssue to requireConfigMapSizeGate in the same pull request, so the gate holds the object under the limit from here on.",
				cm.Name, fixture, size, configMapSizeLimit, fixIssue)
		}
		return
	}
	if size > configMapSizeLimit {
		t.Fatalf("%s at %s is %d bytes of data, over the %d-byte ConfigMap limit: the API server refuses the write and the reconcile cannot land it",
			cm.Name, fixture, size, configMapSizeLimit)
	}
	if size > configMapSizeWarning {
		t.Logf("WARNING: %s at %s is %d bytes of data, past the %d-byte headroom threshold and within %d bytes of the limit",
			cm.Name, fixture, size, configMapSizeWarning, configMapSizeLimit-size)
	}
}

// fleetScaleManagedRepos is the managed_repos list at R repositories, as the
// gitops-state ConfigMap carries it.
func fleetScaleManagedRepos(t *testing.T) string {
	t.Helper()
	entries := make([]map[string]string, 0, fleetScaleRepositories)
	for i := 1; i <= fleetScaleRepositories; i++ {
		entries = append(entries, map[string]string{
			"type": string(agentv1alpha1.GitProviderGitHub),
			"url":  fmt.Sprintf(fleetScaleRepoURLFormat, i),
		})
	}
	body, err := json.Marshal(entries)
	if err != nil {
		t.Fatalf("building managed_repos: %v", err)
	}
	return string(body)
}

// fleetScaleProfileFixtures builds N bound profiles at the shipped shape: the read
// tier alone (AllowedTiers unset) and the chart's subscribe topics.
func fleetScaleProfileFixtures(namespace string) []agentv1alpha1.AgentProfile {
	profiles := make([]agentv1alpha1.AgentProfile, 0, fleetScaleProfiles)
	for i := 1; i <= fleetScaleProfiles; i++ {
		profiles = append(profiles, testAgentProfile(namespace, fmt.Sprintf(fleetScaleProfileNameFormat, i), func(p *agentv1alpha1.AgentProfile) {
			p.Spec.Bus.SubscribeTopics = append([]string(nil), fleetScaleSubscribeTopics...)
		}))
	}
	return profiles
}

// The minter policy ConfigMap at R=500: the sync writes one policy key per
// repository and lists every repository under each key, so the object grows
// with R squared.
func TestTheMinterPolicyConfigMapAtFleetScale(t *testing.T) {
	scheme := setupScheme()
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "test-ns"},
		Spec: agentv1alpha1.PlatformAgentSpec{
			Integration: &agentv1alpha1.PlatformAgentIntegrationSpec{
				IntegrationSpec: agentv1alpha1.IntegrationSpec{
					GitHub: &agentv1alpha1.GitHubSpec{Org: fleetScaleOrg},
				},
			},
		},
	}
	minterCM := &corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{Name: minterConfigMapName, Namespace: agent.Namespace},
		Data:       map[string]string{minterBaseTemplateKey: fleetScaleBaseTemplate},
	}
	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent, minterCM).Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	ctx := context.Background()

	if err := r.syncGithubTokenMinterConfigMap(ctx, agent, fleetScaleManagedRepos(t), ""); err != nil {
		t.Fatalf("syncing %d repositories: %v", fleetScaleRepositories, err)
	}
	synced := &corev1.ConfigMap{}
	if err := cl.Get(ctx, client.ObjectKey{Name: minterConfigMapName, Namespace: agent.Namespace}, synced); err != nil {
		t.Fatalf("reading the synced ConfigMap: %v", err)
	}
	// The fixture did what it says: a policy per repository, each naming them all.
	policies := 0
	for key, content := range synced.Data {
		if key == minterBaseTemplateKey {
			continue
		}
		policies++
		if n := strings.Count(content, "- 'repo-"); n < fleetScaleRepositories {
			t.Fatalf("policy %s names %d repositories, want every one of the %d: the fixture is not the shape the gate measures", key, n, fleetScaleRepositories)
		}
	}
	if policies != fleetScaleRepositories {
		t.Fatalf("the sync wrote %d policies for %d repositories", policies, fleetScaleRepositories)
	}

	requireConfigMapSizeGate(t, synced, fmt.Sprintf("R=%d repositories", fleetScaleRepositories), minterConfigMapFixIssue)
}

// The identity map at N=2,500: one entry per (profile, tier), each carrying
// its ServiceAccount, user, profile and topics, indented for reading.
func TestTheIdentityMapConfigMapAtFleetScale(t *testing.T) {
	// The shipped shape: no install sets the dispatcher image today, and
	// its identity would add every durable's name to the map on top.
	t.Setenv(a2aDispatcherImageEnvVar, "")
	agent := a2aTestAgent()
	profiles := fleetScaleProfileFixtures(agent.Namespace)

	cm, _, err := buildA2AAuthMapConfigMap(agent, profiles)
	if err != nil {
		t.Fatalf("rendering the identity map for %d profiles: %v", fleetScaleProfiles, err)
	}
	var doc a2aAuthMapDocument
	if err := json.Unmarshal([]byte(cm.Data[a2aAuthMapKey]), &doc); err != nil {
		t.Fatalf("decoding the rendered map: %v", err)
	}
	// Every profile resolved: a refused one is left out of the map, and a
	// fixture that silently lost profiles would measure a smaller fleet.
	entries := 0
	for _, id := range doc.Identities {
		if id.Profile != "" {
			entries++
		}
	}
	if entries != fleetScaleProfiles {
		t.Fatalf("the map carries %d profile entries for %d profiles: the fixture is not the fleet the gate measures", entries, fleetScaleProfiles)
	}

	requireConfigMapSizeGate(t, cm, fmt.Sprintf("N=%d profiles", fleetScaleProfiles), identityMapFixIssue)
}

// The dispatch config at N=2,500, rendered only when the dispatcher image is
// set (no release sets it yet): one profile block per profile with its tiers,
// parameters and a durable per (tier, kind), indented.
func TestTheDispatchConfigMapAtFleetScale(t *testing.T) {
	withDispatcher(t)
	agent := a2aTestAgent()
	profiles := fleetScaleProfileFixtures(agent.Namespace)

	cm, err := buildA2ADispatchConfigMap(agent, profiles)
	if err != nil {
		t.Fatalf("rendering the dispatch config for %d profiles: %v", fleetScaleProfiles, err)
	}
	var cfg a2aDispatchConfig
	if err := json.Unmarshal([]byte(cm.Data[a2aDispatchConfigKey]), &cfg); err != nil {
		t.Fatalf("decoding the rendered dispatch config: %v", err)
	}
	if len(cfg.Profiles) != fleetScaleProfiles {
		t.Fatalf("the dispatch config lists %d profiles for %d: the fixture is not the fleet the gate measures", len(cfg.Profiles), fleetScaleProfiles)
	}

	requireConfigMapSizeGate(t, cm, fmt.Sprintf("N=%d profiles", fleetScaleProfiles), dispatchConfigMapFixIssue)
}
