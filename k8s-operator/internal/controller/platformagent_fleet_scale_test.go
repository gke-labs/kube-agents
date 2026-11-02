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
	"os"
	"strings"
	"testing"

	"github.com/go-logr/logr"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/yaml"

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
// it into the forward check, and that is how the gate records the regression
// on main without holding every other pull request red, and how the fix
// proves itself. A forward check that goes red on main would be turned off;
// an inverted one turns itself into the forward check at the moment the fix
// lands.
//
// Each check first confirms its fixture is the fleet the gate names, and does
// so on the renderer's input (the repositories the sync parses, the profiles
// the map and the dispatch config resolve), never on the rendered shape: a
// check that pinned today's layout would fail before the gate on the day the
// fix changes it, and the message naming the fix issue would never print.
// Today's shape is logged for the reader instead.
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
	// fleetScaleRepoURLFormat names the fixture repositories; four digits
	// keep them sorted the way they are numbered.
	fleetScaleRepoURLFormat = "https://github.com/" + fleetScaleOrg + "/repo-%04d"
	// minterRepoLinePrefix opens one repository line as renderRepoPolicy
	// writes it; the minter test counts them for the log, not the verdict.
	minterRepoLinePrefix = "- 'repo-"
	// A fixture profile is named the way the Platform Agent scaffolds a
	// Cluster Agent's, cluster-<project>-<cluster>-<location>
	// (agents/platform/scripts/cluster_agent_profile.py, profile_name), and
	// the roster half of this gate (agents/chat/scripts/test_agent_roster.py)
	// builds its fleet from the same project and location. The name is in
	// every identity-map entry three times and in every dispatch block
	// twice, so a short stand-in would measure a smaller object than an
	// install renders. At 46 characters it is under agentProfileNameMax.
	fleetScaleProject           = "fleet-project"
	fleetScaleLocation          = "us-central1"
	fleetScaleProfileNameFormat = "cluster-" + fleetScaleProject + "-cluster-%04d-" + fleetScaleLocation
	// The fix issues the inverted checks name: the minter policy, the identity
	// map and the dispatch config shrink under #4431.
	minterConfigMapFixIssue   = "#4431"
	identityMapFixIssue       = "#4431"
	dispatchConfigMapFixIssue = "#4431"
)

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

// configMapSizeVerdict is the gate's judgement on one data size, kept apart
// from the test plumbing so the boundary on each side is itself tested
// (TestTheConfigMapSizeVerdictAtItsBoundaries). fatal is non-empty when the
// gate fails; warning is non-empty when the forward check logs headroom.
//
// With fixIssue empty it is the forward check: the object fits under the
// limit, and a warning is raised once it passes the headroom threshold. With
// fixIssue set it is inverted: the object is over the limit on main today,
// that issue owns the fix, and a render that fits is the fix landing without
// flipping the check. The fix pull request drops the issue from the call in
// the same diff.
func configMapSizeVerdict(size int, fixIssue string) (fatal, warning string) {
	if fixIssue != "" {
		if size <= configMapSizeLimit {
			return fmt.Sprintf("%d bytes of data, under the %d-byte ConfigMap limit. This check is inverted because the render was over the limit on main and %s owns the fix: "+
				"the change that brought it under passes an empty fixIssue to requireConfigMapSizeGate in the same pull request, so the gate holds the object under the limit from here on.",
				size, configMapSizeLimit, fixIssue), ""
		}
		return "", ""
	}
	if size > configMapSizeLimit {
		return fmt.Sprintf("%d bytes of data, over the %d-byte ConfigMap limit: the API server refuses the write and the reconcile cannot land it",
			size, configMapSizeLimit), ""
	}
	if size > configMapSizeWarning {
		return "", fmt.Sprintf("%d bytes of data, past the %d-byte headroom threshold and within %d bytes of the limit",
			size, configMapSizeWarning, configMapSizeLimit-size)
	}
	return "", ""
}

// requireConfigMapSizeGate applies configMapSizeVerdict to one rendered
// object, logging its size on every run.
func requireConfigMapSizeGate(t *testing.T, cm *corev1.ConfigMap, fixture, fixIssue string) {
	t.Helper()
	size := configMapDataSize(cm)
	t.Logf("%s at %s: %d bytes of data, %d bytes serialized (limit %d, warning above %d)",
		cm.Name, fixture, size, serializedConfigMapSize(t, cm), configMapSizeLimit, configMapSizeWarning)
	fatal, warning := configMapSizeVerdict(size, fixIssue)
	if fatal != "" {
		t.Fatalf("%s at %s is %s", cm.Name, fixture, fatal)
	}
	if warning != "" {
		t.Logf("WARNING: %s at %s is %s", cm.Name, fixture, warning)
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

// shippedProfileSubscribeTopics are the subscribe topics the shipped profile
// carries, read from the example the chart's values mirror
// (k8s-operator/examples/agentprofile-tse.yaml; TestTheTSEProfileExampleIsAStrictAgentProfile
// holds it to the type) rather than copied, so the fixture follows the shipped
// shape. The identity-map verdict depends on them: with no topics the map at
// N=2,500 measures under the limit, so a shipped profile with none is a
// broken fixture here, not a smaller fleet.
func shippedProfileSubscribeTopics(t *testing.T) []string {
	t.Helper()
	raw, err := os.ReadFile(tseProfileExamplePath)
	if err != nil {
		t.Fatalf("reading the shipped profile: %v", err)
	}
	var p agentv1alpha1.AgentProfile
	if err := yaml.UnmarshalStrict(raw, &p); err != nil {
		t.Fatalf("the shipped profile is not a strict AgentProfile: %v", err)
	}
	if len(p.Spec.Bus.SubscribeTopics) == 0 {
		t.Fatalf("the shipped profile %s carries no subscribe topics; the identity-map gate measures entries with the shipped topics, so decide the fixture's topics before measuring", tseProfileExamplePath)
	}
	return append([]string(nil), p.Spec.Bus.SubscribeTopics...)
}

// fleetScaleProfileFixtures builds N bound profiles at the shipped shape (the
// read tier alone, AllowedTiers unset, and the shipped subscribe topics) and
// confirms every one resolves to an identity. The identity map and the
// dispatch config both leave a refused profile out, so a fixture that was
// refused would measure a smaller fleet; the check is on the resolver's
// verdict, the renderers' input, rather than on a count in the rendered
// object, so the fix pull request can change the rendered shape and still
// reach the gate.
func fleetScaleProfileFixtures(t *testing.T, agent *agentv1alpha1.PlatformAgent) []agentv1alpha1.AgentProfile {
	t.Helper()
	topics := shippedProfileSubscribeTopics(t)
	profiles := make([]agentv1alpha1.AgentProfile, 0, fleetScaleProfiles)
	for i := 1; i <= fleetScaleProfiles; i++ {
		profiles = append(profiles, testAgentProfile(agent.Namespace, fmt.Sprintf(fleetScaleProfileNameFormat, i), func(p *agentv1alpha1.AgentProfile) {
			p.Spec.Bus.SubscribeTopics = append([]string(nil), topics...)
		}))
	}
	resolved := resolveAgentProfileIdentities(agent, profiles)
	for i := range profiles {
		if r, ok := resolved[profiles[i].Name]; !ok || r.refused != nil {
			t.Fatalf("fixture profile %s does not resolve to an identity (%v): the fixture is not the fleet the gate measures", profiles[i].Name, r.refused)
		}
	}
	return profiles
}

// The judgement on each side of each threshold, in both modes, so the forward
// branch is tested before the day the fix flips a call onto it.
func TestTheConfigMapSizeVerdictAtItsBoundaries(t *testing.T) {
	cases := []struct {
		name          string
		size          int
		fixIssue      string
		fatal, warned bool
	}{
		{"forward, well under", configMapSizeWarning - 1, "", false, false},
		{"forward, at the warning threshold", configMapSizeWarning, "", false, false},
		{"forward, past the warning threshold", configMapSizeWarning + 1, "", false, true},
		{"forward, one under the limit", configMapSizeLimit - 1, "", false, true},
		{"forward, at the limit", configMapSizeLimit, "", false, true},
		{"forward, one over the limit", configMapSizeLimit + 1, "", true, false},
		{"inverted, well under", configMapSizeWarning - 1, "#0", true, false},
		{"inverted, one under the limit", configMapSizeLimit - 1, "#0", true, false},
		{"inverted, at the limit", configMapSizeLimit, "#0", true, false},
		{"inverted, one over the limit", configMapSizeLimit + 1, "#0", false, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			fatal, warning := configMapSizeVerdict(tc.size, tc.fixIssue)
			if (fatal != "") != tc.fatal {
				t.Errorf("size %d, fixIssue %q: fatal = %q, want fatal %v", tc.size, tc.fixIssue, fatal, tc.fatal)
			}
			if (warning != "") != tc.warned {
				t.Errorf("size %d, fixIssue %q: warning = %q, want warning %v", tc.size, tc.fixIssue, warning, tc.warned)
			}
			if tc.fixIssue != "" && fatal != "" && !strings.Contains(fatal, tc.fixIssue) {
				t.Errorf("the inverted failure %q does not name the fix issue %s", fatal, tc.fixIssue)
			}
		})
	}
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
	// The chart's three-scope default.yaml, as the read-only policy tests
	// already hold it (minterTemplateWithReadScope).
	minterCM := &corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{Name: minterConfigMapName, Namespace: agent.Namespace},
		Data:       map[string]string{minterBaseTemplateKey: minterTemplateWithReadScope},
	}
	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent, minterCM).Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	ctx := context.Background()

	// The fixture is the fleet the gate measures, checked on the sync's
	// input: the list parses to R repositories, every one in the organisation
	// the sync claims, so every one gets a policy. How the sync lays the
	// policies out is what the fix issue changes, so it is logged below and
	// not asserted here.
	managedRepos := fleetScaleManagedRepos(t)
	primaryOrg, skip := minterPrimaryOrg(agent)
	if skip != "" {
		t.Fatalf("the fixture agent's git integration does not resolve: %s", skip)
	}
	parsed, unreadable, err := minterBareRepos(logr.Discard(), managedRepos, primaryOrg, minterGitHubHost(agent), gitopsStateManagedReposKey)
	if err != nil {
		t.Fatalf("parsing the fixture repository list: %v", err)
	}
	if len(parsed) != fleetScaleRepositories || len(unreadable) != 0 {
		t.Fatalf("the sync reads %d repositories (%d unreadable) from a fixture of %d: the fixture is not the fleet the gate measures", len(parsed), len(unreadable), fleetScaleRepositories)
	}

	if err := r.syncGithubTokenMinterConfigMap(ctx, agent, managedRepos, ""); err != nil {
		t.Fatalf("syncing %d repositories: %v", fleetScaleRepositories, err)
	}
	synced := &corev1.ConfigMap{}
	if err := cl.Get(ctx, client.ObjectKey{Name: minterConfigMapName, Namespace: agent.Namespace}, synced); err != nil {
		t.Fatalf("reading the synced ConfigMap: %v", err)
	}
	// The sync ran: it claimed the fixture's repositories rather than
	// skipping the list, which would leave the template alone and measure
	// an empty object as a fix.
	managedKeys := parseManagedKeysAnnotation(synced.Annotations[AnnotationManagedMinterKeys])
	if len(managedKeys) == 0 {
		t.Fatalf("the sync claimed no policy key for %d repositories: the fixture is not the fleet the gate measures", fleetScaleRepositories)
	}
	repoLines := 0
	for key, content := range synced.Data {
		if key != minterBaseTemplateKey {
			repoLines += strings.Count(content, minterRepoLinePrefix)
		}
	}
	t.Logf("%s at R=%d: %d operator-managed policy keys, %d repository lines across them",
		synced.Name, fleetScaleRepositories, len(managedKeys), repoLines)

	requireConfigMapSizeGate(t, synced, fmt.Sprintf("R=%d repositories", fleetScaleRepositories), minterConfigMapFixIssue)
}

// The identity map at N=2,500: one entry per (profile, tier), each carrying
// its ServiceAccount, user, profile and topics, indented for reading.
func TestTheIdentityMapConfigMapAtFleetScale(t *testing.T) {
	// The shipped shape: no install sets the dispatcher image today, and
	// its identity would add every durable's name to the map on top.
	t.Setenv(a2aDispatcherImageEnvVar, "")
	agent := a2aTestAgent()
	profiles := fleetScaleProfileFixtures(t, agent)

	cm, _, err := buildA2AAuthMapConfigMap(agent, profiles)
	if err != nil {
		t.Fatalf("rendering the identity map for %d profiles: %v", fleetScaleProfiles, err)
	}
	var doc a2aAuthMapDocument
	if err := json.Unmarshal([]byte(cm.Data[a2aAuthMapKey]), &doc); err != nil {
		t.Fatalf("decoding the rendered map: %v", err)
	}
	t.Logf("%s at N=%d: %d identities rendered", cm.Name, fleetScaleProfiles, len(doc.Identities))

	requireConfigMapSizeGate(t, cm, fmt.Sprintf("N=%d profiles", fleetScaleProfiles), identityMapFixIssue)
}

// The dispatch config at N=2,500, rendered only when the dispatcher image is
// set (no release sets it yet): one profile block per profile with its tiers,
// parameters and a durable per (tier, kind), indented.
func TestTheDispatchConfigMapAtFleetScale(t *testing.T) {
	withDispatcher(t)
	agent := a2aTestAgent()
	profiles := fleetScaleProfileFixtures(t, agent)

	cm, err := buildA2ADispatchConfigMap(agent, profiles)
	if err != nil {
		t.Fatalf("rendering the dispatch config for %d profiles: %v", fleetScaleProfiles, err)
	}
	var cfg a2aDispatchConfig
	if err := json.Unmarshal([]byte(cm.Data[a2aDispatchConfigKey]), &cfg); err != nil {
		t.Fatalf("decoding the rendered dispatch config: %v", err)
	}
	t.Logf("%s at N=%d: %d profile blocks rendered", cm.Name, fleetScaleProfiles, len(cfg.Profiles))

	requireConfigMapSizeGate(t, cm, fmt.Sprintf("N=%d profiles", fleetScaleProfiles), dispatchConfigMapFixIssue)
}
