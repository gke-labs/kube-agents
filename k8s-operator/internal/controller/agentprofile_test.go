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
	"errors"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"strings"
	"testing"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"
	"sigs.k8s.io/yaml"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// "Profile" throughout is the A2A AgentProfile resource, not a Hermes profile.

const (
	testOperatorNamespace = "kubeagents-operator"
	testOperatorSA        = "kubeagents-controller-manager"

	// The fixtures the a2a module's callout suite reads: the identity map with
	// the operator and two profiles in it, and the card and tombstone the
	// operator publishes.
	profilesMapFixturePath  = "../../../a2a/authcallout/testdata/rendered-identity-map-profiles.json"
	agentCardFixturePath    = "../../../a2a/authcallout/testdata/rendered-agent-card.json"
	agentClosedFixturePath  = "../../../a2a/authcallout/testdata/rendered-agent-closed.json"
	specDocPath             = "../../../docs/designs/spec-subagent-profiles.md"
	chatProfileExamplePath  = "../../examples/agentprofile-chat.yaml"
	tseProfileExamplePath   = "../../examples/agentprofile-tse.yaml"
	agentProfileCRDBasePath = "../../config/crd/bases/kubeagents.x-k8s.io_agentprofiles.yaml"
)

func testAgentProfile(namespace, name string, mutate ...func(*agentv1alpha1.AgentProfile)) agentv1alpha1.AgentProfile {
	p := agentv1alpha1.AgentProfile{
		TypeMeta:   metav1.TypeMeta{APIVersion: agentv1alpha1.GroupVersion.String(), Kind: "AgentProfile"},
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: namespace, Generation: 1, UID: types.UID("uid-" + name)},
		Spec: agentv1alpha1.AgentProfileSpec{
			Description: "Read-only SRE scoped to one cluster.",
			Persona:     agentv1alpha1.AgentProfilePersona{Image: "registry.example/persona-cluster:1"},
			Harness:     agentv1alpha1.AgentProfileHarness{Image: "registry.example/agent-worker:1", Model: "model-default", MaxTurns: 50},
			Lifecycle:   agentv1alpha1.AgentProfileLifecycle{ActiveDeadlineSeconds: 1800, TTLSecondsAfterFinished: 600},
			Concurrency: 2,
			Resources: agentv1alpha1.AgentProfileResources{
				Requests: agentv1alpha1.AgentProfileResourceList{CPU: resource.MustParse("250m"), Memory: resource.MustParse("512Mi")},
				Limits:   agentv1alpha1.AgentProfileResourceList{CPU: resource.MustParse("1"), Memory: resource.MustParse("2Gi")},
			},
		},
	}
	for _, m := range mutate {
		m(&p)
	}
	return p
}

func withOperatorBusPrincipal(t *testing.T) {
	t.Helper()
	t.Setenv(operatorNamespaceEnvVar, testOperatorNamespace)
	t.Setenv(operatorServiceAccountEnvVar, testOperatorSA)
}

func withoutOperatorBusPrincipal(t *testing.T) {
	t.Helper()
	t.Setenv(operatorNamespaceEnvVar, "")
	t.Setenv(operatorServiceAccountEnvVar, "")
}

func renderedMapEntries(t *testing.T, agent *agentv1alpha1.PlatformAgent, profiles []agentv1alpha1.AgentProfile) map[string]a2aAuthMapIdentity {
	t.Helper()
	doc, err := renderA2AAuthMap(agent, profiles)
	if err != nil {
		t.Fatalf("renderA2AAuthMap: %v", err)
	}
	out := map[string]a2aAuthMapIdentity{}
	for _, id := range doc.Identities {
		out[id.User] = id
	}
	return out
}

// --- the spec block ---------------------------------------------------------

// The CRD is the spec's field table, field for field: promoting the file form
// was a move, and this is what keeps it one. The table's left column is read
// out of the design doc, so a field added to either without the other fails.
func TestAgentProfileFieldsMatchTheSpecTable(t *testing.T) {
	raw, err := os.ReadFile(specDocPath)
	if err != nil {
		t.Fatalf("reading the spec: %v", err)
	}
	cell := regexp.MustCompile("^\\| `([^`]+)`(?: / `([^`]+)`)?(?:, `([^`]+)`)?\\s*\\|")
	start := strings.Index(string(raw), "| Field ")
	if start < 0 {
		t.Fatal("the spec's field table is gone; this test reads it")
	}
	var fromSpec []string
	for _, line := range strings.Split(string(raw)[start:], "\n")[2:] {
		if !strings.HasPrefix(line, "|") {
			break
		}
		m := cell.FindStringSubmatch(line)
		if m == nil {
			t.Fatalf("cannot read a field name from spec row %q", line)
		}
		for _, f := range m[1:] {
			if f == "" {
				continue
			}
			// `harness.model`, `maxTurns` names harness.maxTurns, and
			// `bus.publishTopics` / `subscribeTopics` names
			// bus.subscribeTopics: the second name inherits the first's
			// prefix.
			if !strings.Contains(f, ".") && strings.Contains(m[1], ".") {
				f = m[1][:strings.LastIndex(m[1], ".")+1] + f
			}
			fromSpec = append(fromSpec, f)
		}
	}

	// The CRD's leaves, as the spec writes them: top-level fields, and one
	// level down for persona, harness, bus, identity and lifecycle. clusterRef
	// and resources are rows of their own in the spec.
	fromType := agentProfileSpecPaths()
	slices.Sort(fromSpec)
	slices.Sort(fromType)
	if !slices.Equal(fromSpec, fromType) {
		t.Errorf("the spec's field table and AgentProfileSpec disagree.\nspec: %v\ntype: %v", fromSpec, fromType)
	}
}

// agentProfileSpecPaths reads the JSON shape of a fully populated spec.
func agentProfileSpecPaths() []string {
	p := testAgentProfile("ns", "x", func(p *agentv1alpha1.AgentProfile) {
		p.Spec.Bus = agentv1alpha1.AgentProfileBus{PublishTopics: []string{"shared.a"}, SubscribeTopics: []string{"shared.b"}}
		p.Spec.Identity.ServiceAccountName = "sa"
		p.Spec.ClusterRef = &agentv1alpha1.AgentProfileClusterRef{ProjectID: "p", Cluster: "c", Location: "l"}
		p.Spec.QueueTimeoutSeconds = 1
		p.Spec.Tools = []agentv1alpha1.AgentProfileTool{{Name: "jq", Image: "registry.example/jq:1"}}
		p.Spec.AllowedTiers = []string{agentv1alpha1.AgentProfileTierRead}
		p.Spec.Parameters = []agentv1alpha1.AgentProfileParameter{{Name: "cluster", Required: true}}
	})
	raw, _ := json.Marshal(p.Spec)
	var top map[string]json.RawMessage
	_ = json.Unmarshal(raw, &top)
	var out []string
	for k, v := range top {
		switch k {
		case "persona", "harness", "bus", "identity", "lifecycle":
			var sub map[string]json.RawMessage
			_ = json.Unmarshal(v, &sub)
			for sk := range sub {
				out = append(out, k+"."+sk)
			}
		default:
			out = append(out, k)
		}
	}
	return out
}

// The chat profile moved from a2a/profiles/chat.yaml to a full CR. It must be a
// valid AgentProfile under the same strict decode the file loader used.
func TestTheChatProfileExampleIsAStrictAgentProfile(t *testing.T) {
	raw, err := os.ReadFile(chatProfileExamplePath)
	if err != nil {
		t.Fatalf("reading the chat profile: %v", err)
	}
	var p agentv1alpha1.AgentProfile
	if err := yaml.UnmarshalStrict(raw, &p); err != nil {
		t.Fatalf("the chat profile is not a strict AgentProfile: %v", err)
	}
	if p.Kind != "AgentProfile" || p.Name != "chat" {
		t.Errorf("kind/name = %q/%q, want AgentProfile/chat", p.Kind, p.Name)
	}
	if len(p.Spec.Bus.PublishTopics) != 0 {
		t.Errorf("the chat profile publishes %v; the front door writes no standing state", p.Spec.Bus.PublishTopics)
	}
	if p.Spec.Identity.ServiceAccountName != "" {
		t.Errorf("the chat profile names ServiceAccount %q; it runs with an operator-created one holding nothing", p.Spec.Identity.ServiceAccountName)
	}
	// A grant is only real if it names a topic the deployment provisions: a
	// topic nobody rendered is an authorization violation for a legitimate
	// worker at runtime, not an obvious error. Checked against the provision
	// script the operator actually renders.
	script := a2aProvisionScript(a2aTestAgent())
	for _, grant := range append(append([]string(nil), p.Spec.Bus.PublishTopics...), p.Spec.Bus.SubscribeTopics...) {
		if !strings.Contains(script, "a2a.topics."+grant) {
			t.Errorf("the chat profile grants %q, which the provision script does not provision", grant)
		}
	}
}

// The CRD's topic pattern and the operator's render-time recheck are one
// regular expression written twice (a kubebuilder marker cannot name a Go
// constant). This holds them equal.
func TestTheCRDTopicPatternIsTheOperatorsRecheck(t *testing.T) {
	raw, err := os.ReadFile(agentProfileCRDBasePath)
	if err != nil {
		t.Fatalf("reading the generated CRD: %v", err)
	}
	want := "pattern: " + agentv1alpha1.AgentProfileTopicPattern
	if n := strings.Count(string(raw), want); n != 2 {
		t.Errorf("the generated CRD carries the topic pattern %d times, want 2 (publishTopics and subscribeTopics): the marker and AgentProfileTopicPattern have diverged", n)
	}
	for _, good := range []string{"shared.blueprint", "agent.platform.upgrade-readiness"} {
		if !agentProfileTopicRE.MatchString(good) {
			t.Errorf("the topic pattern refuses %q", good)
		}
	}
	for _, bad := range []string{"shared.>", "shared.*", "a2a.topics.shared.x", "tasks.x", "agent.x", "shared.a.b", "shared.A", ""} {
		if agentProfileTopicRE.MatchString(bad) {
			t.Errorf("the topic pattern accepts %q", bad)
		}
	}
}

// --- the identity map -------------------------------------------------------

// A profile renders one narrowed entry per tier, keyed on that tier's
// ServiceAccount in the profile namespace, carrying its name, tier and topics
// and no grants: the callout derives the subjects.
func TestAProfileRendersANarrowedMapEntry(t *testing.T) {
	withoutOperatorBusPrincipal(t)
	agent := a2aTestAgent()
	auditor := testAgentProfile(agent.Namespace, "auditor", func(p *agentv1alpha1.AgentProfile) {
		p.Spec.Bus.PublishTopics = []string{"agent.auditor.findings"}
		p.Spec.Bus.SubscribeTopics = []string{"shared.blueprint"}
	})
	entries := renderedMapEntries(t, agent, []agentv1alpha1.AgentProfile{auditor})

	e, ok := entries["profile-auditor-read"]
	if !ok {
		t.Fatalf("no entry for the profile's read tier; got %v", entries)
	}
	if e.ServiceAccount != "system:serviceaccount:test-ns-profiles:agentprofile-auditor-read" {
		t.Errorf("serviceAccount = %q, want the read tier's, in the profile namespace", e.ServiceAccount)
	}
	if e.Tier != agentv1alpha1.AgentProfileTierRead {
		t.Errorf("tier = %q, want read", e.Tier)
	}
	if e.Narrowing != a2aNarrowingProfile || e.Profile != "auditor" {
		t.Errorf("narrowing/profile = %q/%q, want profile/auditor", e.Narrowing, e.Profile)
	}
	if len(e.Grants.Publish) != 0 || len(e.Grants.Subscribe) != 0 {
		t.Errorf("a profile entry carries grants %v; the map must hold no subject a profile pod can reach", e.Grants)
	}
	if e.Topics == nil || !slices.Equal(e.Topics.Publish, auditor.Spec.Bus.PublishTopics) || !slices.Equal(e.Topics.Subscribe, auditor.Spec.Bus.SubscribeTopics) {
		t.Errorf("topics = %+v, want the profile's own", e.Topics)
	}
}

// A profile with no topics (the cluster agent's `bus: {}`) carries no topics
// field at all, so the callout grants it nothing on the blackboard.
func TestAProfileWithNoTopicsRendersNoTopicsField(t *testing.T) {
	withoutOperatorBusPrincipal(t)
	agent := a2aTestAgent()
	entries := renderedMapEntries(t, agent, []agentv1alpha1.AgentProfile{testAgentProfile(agent.Namespace, "cluster-a")})
	if e := entries["profile-cluster-a"]; e.Topics != nil {
		t.Errorf("a no-topics profile rendered topics %+v", e.Topics)
	}
}

// One refused profile is left out; the map still renders for everyone else.
// Each reserved ServiceAccount is refused, and of two profiles naming one
// ServiceAccount the first by name keeps it.
func TestARefusedProfileIsLeftOutAndTheMapStillRenders(t *testing.T) {
	withoutOperatorBusPrincipal(t)
	agent := a2aTestAgent()
	var profiles []agentv1alpha1.AgentProfile
	for sa := range reservedProfileServiceAccounts(agent) {
		name := "reserved-" + strings.ReplaceAll(sa, ".", "-")
		if len(name) > 63 {
			name = name[:63]
		}
		profiles = append(profiles, testAgentProfile(agent.Namespace, strings.TrimRight(name, "-"), func(p *agentv1alpha1.AgentProfile) {
			p.Spec.Identity.ServiceAccountName = sa
		}))
	}
	profiles = append(profiles,
		testAgentProfile(agent.Namespace, "b-second", func(p *agentv1alpha1.AgentProfile) { p.Spec.Identity.ServiceAccountName = "shared-sa" }),
		testAgentProfile(agent.Namespace, "a-first", func(p *agentv1alpha1.AgentProfile) { p.Spec.Identity.ServiceAccountName = "shared-sa" }),
		testAgentProfile(agent.Namespace, "platform"),
		testAgentProfile(agent.Namespace, "fine"),
	)

	entries := renderedMapEntries(t, agent, profiles)
	for user := range entries {
		if strings.HasPrefix(user, "profile-reserved-") {
			t.Errorf("a profile naming a reserved ServiceAccount rendered %q", user)
		}
	}
	if _, ok := entries["profile-platform-read"]; ok {
		t.Error("a profile named platform rendered; that is the bridge's addressee")
	}
	if _, ok := entries["profile-b-second-read"]; ok {
		t.Error("the second profile naming shared-sa rendered; two entries under one key fail the whole map")
	}
	for _, want := range []string{"profile-a-first-read", "profile-fine-read", "session", "provision", "agent", "verifier"} {
		if _, ok := entries[want]; !ok {
			t.Errorf("%q is missing from the map", want)
		}
	}

	resolved := resolveAgentProfileIdentities(agent, profiles)
	for sa := range reservedProfileServiceAccounts(agent) {
		found := false
		for _, r := range resolved {
			if r.refused != nil && strings.Contains(r.refused.Error(), `"`+sa+`"`) {
				found = true
			}
		}
		if !found {
			t.Errorf("reserved ServiceAccount %q was not refused", sa)
		}
	}
}

// A profile naming the operator's ServiceAccount, when the operator runs in
// the agent's namespace, gets a ServiceAccount of that name in the profile
// namespace: a different principal, whose map key can't collide with the
// operator's entry, so both render.
func TestAProfileNamingTheOperatorsServiceAccountIsADifferentPrincipal(t *testing.T) {
	agent := a2aTestAgent()
	t.Setenv(operatorNamespaceEnvVar, agent.Namespace)
	t.Setenv(operatorServiceAccountEnvVar, testOperatorSA)
	sneaky := testAgentProfile(agent.Namespace, "sneaky", func(p *agentv1alpha1.AgentProfile) {
		p.Spec.Identity.ServiceAccountName = testOperatorSA
	})
	entries := renderedMapEntries(t, agent, []agentv1alpha1.AgentProfile{sneaky})
	op, ok := entries[a2aOperatorBusUser]
	if !ok {
		t.Fatal("the operator's own entry is missing")
	}
	prof, ok := entries["profile-sneaky-read"]
	if !ok {
		t.Fatal("the profile's entry is missing")
	}
	if op.ServiceAccount == prof.ServiceAccount {
		t.Errorf("the profile's key %q is the operator's; two entries under one key fail the whole map", prof.ServiceAccount)
	}
	if prof.ServiceAccount != a2aServiceAccountName(profileNamespaceName(agent), testOperatorSA) {
		t.Errorf("the profile's key = %q, want the ServiceAccount of that name in the profile namespace", prof.ServiceAccount)
	}
}

// A profile that sorts first cannot take over another profile's
// operator-created ServiceAccount by naming it: the incumbent keeps its entry.
func TestAProfileCannotTakeOverAnotherProfilesServiceAccount(t *testing.T) {
	withoutOperatorBusPrincipal(t)
	agent := a2aTestAgent()
	yak := testAgentProfile(agent.Namespace, "yak")
	aardvark := testAgentProfile(agent.Namespace, "aardvark", func(p *agentv1alpha1.AgentProfile) {
		p.Spec.Identity.ServiceAccountName = "agentprofile-yak"
	})
	resolved := resolveAgentProfileIdentities(agent, []agentv1alpha1.AgentProfile{yak, aardvark})
	if resolved["aardvark"].refused == nil {
		t.Error("aardvark took yak's operator-created ServiceAccount")
	}
	if resolved["yak"].refused != nil {
		t.Errorf("yak lost its own ServiceAccount: %v", resolved["yak"].refused)
	}
}

// A profile that got past admission with a bad name or topic (an older or
// edited CRD) drops out of the map alone; the render still succeeds.
func TestAMalformedProfileDropsOutWithoutFailingTheMap(t *testing.T) {
	withoutOperatorBusPrincipal(t)
	agent := a2aTestAgent()
	profiles := []agentv1alpha1.AgentProfile{
		testAgentProfile(agent.Namespace, "a.b"),
		testAgentProfile(agent.Namespace, "badtopic", func(p *agentv1alpha1.AgentProfile) { p.Spec.Bus.SubscribeTopics = []string{"tasks.>"} }),
		testAgentProfile(agent.Namespace, "fine"),
	}
	entries := renderedMapEntries(t, agent, profiles)
	if _, ok := entries["profile-fine-read"]; !ok {
		t.Error("the good profile is missing")
	}
	for _, bad := range []string{"profile-a.b-read", "profile-badtopic-read"} {
		if _, ok := entries[bad]; ok {
			t.Errorf("%s rendered", bad)
		}
	}
}

// An existing ServiceAccount under the operator-created name that the profile
// does not control is neither adopted nor given a bus identity.
func TestAForeignServiceAccountIsNotAdopted(t *testing.T) {
	withOperatorBusPrincipal(t)
	agent := a2aTestAgent()
	p := testAgentProfile(agent.Namespace, "auditor")
	foreign := &corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{Name: "agentprofile-auditor-read", Namespace: profileNamespaceName(agent)}}
	h := newProfileHarness(t, agent, &p, foreign)
	h.reconcile(agent.Namespace, "auditor")

	if c := condition(h.profile(agent.Namespace, "auditor"), agentv1alpha1.AgentProfileConditionIdentityReady); c.Reason != reasonAgentProfileRefused {
		t.Errorf("IdentityReady reason = %q, want %q", c.Reason, reasonAgentProfileRefused)
	}
	sa, _ := h.serviceAccount(profileNamespaceName(agent), "agentprofile-auditor-read")
	if sa.Annotations[agentProfileUIDAnnotation] != "" {
		t.Errorf("the foreign ServiceAccount was adopted: %v", sa.Annotations)
	}
	bound, err := boundAgentProfiles(context.Background(), h.c, agent)
	if err != nil || len(bound) != 0 {
		t.Errorf("boundAgentProfiles = %v, %v; want the profile left out of the map", bound, err)
	}
}

// The order of the profile entries does not depend on list order, so an
// unchanged set of profiles never churns the map's version.
func TestProfileEntriesRenderInNameOrder(t *testing.T) {
	withoutOperatorBusPrincipal(t)
	agent := a2aTestAgent()
	a, b := testAgentProfile(agent.Namespace, "alpha"), testAgentProfile(agent.Namespace, "beta")
	one, err := renderA2AAuthMap(agent, []agentv1alpha1.AgentProfile{b, a})
	if err != nil {
		t.Fatal(err)
	}
	two, err := renderA2AAuthMap(agent, []agentv1alpha1.AgentProfile{a, b})
	if err != nil {
		t.Fatal(err)
	}
	if one.Version != two.Version {
		t.Errorf("the map's version depends on the order profiles were listed in: %s vs %s", one.Version, two.Version)
	}
}

// The operator's copy of the callout's map rules covers the profile entry: the
// shapes the callout refuses, refused here too.
func TestTheRenderTimeMapCheckRefusesBadProfileEntries(t *testing.T) {
	good := a2aAuthMapIdentity{ServiceAccount: "system:serviceaccount:ns:sa", User: "profile-x", Account: a2aAccountApp, Narrowing: a2aNarrowingProfile, Profile: "x"}
	if err := validateA2AAuthMapIdentities([]a2aAuthMapIdentity{good}); err != nil {
		t.Fatalf("a well-formed profile entry was refused: %v", err)
	}
	cases := map[string]func(*a2aAuthMapIdentity){
		"grants":               func(e *a2aAuthMapIdentity) { e.Grants.Publish = []string{"a2a.tasks.>"} },
		"no profile":           func(e *a2aAuthMapIdentity) { e.Profile = "" },
		"dotted profile":       func(e *a2aAuthMapIdentity) { e.Profile = "a.b" },
		"wildcard profile":     func(e *a2aAuthMapIdentity) { e.Profile = "*" },
		"non-topic":            func(e *a2aAuthMapIdentity) { e.Topics = &a2aAuthMapTopics{Subscribe: []string{"tasks.x"}} },
		"wildcard topic":       func(e *a2aAuthMapIdentity) { e.Topics = &a2aAuthMapTopics{Publish: []string{"shared.>"}} },
		"profile on pod entry": func(e *a2aAuthMapIdentity) { e.Narrowing = a2aNarrowingPod },
		"topics on static": func(e *a2aAuthMapIdentity) {
			e.Narrowing, e.Profile = "", ""
			e.Grants = a2aAuthMapGrants{Publish: []string{"x"}, Subscribe: []string{"y"}}
			e.Topics = &a2aAuthMapTopics{Publish: []string{"shared.a"}}
		},
	}
	for name, mutate := range cases {
		e := good
		mutate(&e)
		if err := validateA2AAuthMapIdentities([]a2aAuthMapIdentity{e}); err == nil {
			t.Errorf("%s: the render-time check accepted it", name)
		}
	}
}

// --- the operator as a bus principal ---------------------------------------

// Without the downward-API variables the operator renders no entry for itself
// and no fence peer: no guessed ServiceAccount name in the map.
func TestTheOperatorRendersNoBusIdentityWithoutItsOwnServiceAccount(t *testing.T) {
	withoutOperatorBusPrincipal(t)
	agent := a2aTestAgent()
	if _, ok := renderedMapEntries(t, agent, nil)[a2aOperatorBusUser]; ok {
		t.Error("the operator rendered a map entry without knowing its own ServiceAccount")
	}
	if peers := a2aOperatorNATSPeers(); len(peers) != 0 {
		t.Errorf("the NATS fence admits an operator peer without knowing the operator's namespace: %v", peers)
	}
}

// A malformed pair takes the same path as a missing one. The pair is
// process-wide, so a value that failed the map would stop every PlatformAgent's
// render, not one namespace's.
func TestAMalformedOperatorPrincipalRendersNoEntryAndFailsNothing(t *testing.T) {
	for name, pair := range map[string][2]string{
		"map-key spelling":   {testOperatorNamespace, "system:serviceaccount:ns:sa"},
		"colon":              {testOperatorNamespace, "foo:bar"},
		"namespace casing":   {"Kubeagents-System", testOperatorSA},
		"dotted namespace":   {"kube.agents", testOperatorSA},
		"serviceaccount gap": {testOperatorNamespace, "has space"},
	} {
		t.Run(name, func(t *testing.T) {
			t.Setenv(operatorNamespaceEnvVar, pair[0])
			t.Setenv(operatorServiceAccountEnvVar, pair[1])
			agent := a2aTestAgent()
			if _, ok := renderedMapEntries(t, agent, nil)[a2aOperatorBusUser]; ok {
				t.Errorf("the operator rendered a map entry for %q/%q", pair[0], pair[1])
			}
			if peers := a2aOperatorNATSPeers(); len(peers) != 0 {
				t.Errorf("the NATS fence admits an operator peer for namespace %q: %v", pair[0], peers)
			}
		})
	}
}

// With them, its entry is the directory and its inbox, nothing else.
func TestTheOperatorsBusIdentityIsTheDirectoryAndNothingElse(t *testing.T) {
	withOperatorBusPrincipal(t)
	agent := a2aTestAgent()
	e, ok := renderedMapEntries(t, agent, nil)[a2aOperatorBusUser]
	if !ok {
		t.Fatal("the operator rendered no entry for itself")
	}
	if e.ServiceAccount != "system:serviceaccount:"+testOperatorNamespace+":"+testOperatorSA {
		t.Errorf("serviceAccount = %q", e.ServiceAccount)
	}
	wantPub := []string{"a2a.agents.*", "$JS.API.DIRECT.GET.DIRECTORY.a2a.agents.*"}
	wantSub := []string{"_INBOX.operator.>"}
	if !slices.Equal(e.Grants.Publish, wantPub) || !slices.Equal(e.Grants.Subscribe, wantSub) {
		t.Errorf("grants = %+v, want publish %v subscribe %v", e.Grants, wantPub, wantSub)
	}
	if e.Narrowing != "" || e.Profile != "" || e.Topics != nil {
		t.Errorf("the operator's entry narrows or carries profile fields: %+v", e)
	}
}

// The fence admits the operator's pod from the operator's namespace, by the
// bus-client label, and only with a namespace selector: a bare pod selector
// would admit a pod with that label in the agent's own namespace instead.
func TestTheNATSFenceAdmitsTheOperatorFromItsOwnNamespace(t *testing.T) {
	withOperatorBusPrincipal(t)
	np := buildA2ANATSNetworkPolicy(a2aTestAgent())
	var found bool
	for _, peer := range np.Spec.Ingress[0].From {
		if peer.PodSelector == nil || peer.PodSelector.MatchLabels[a2aOperatorBusClientLabel] != a2aOperatorBusClientLabelValue {
			continue
		}
		found = true
		if peer.NamespaceSelector == nil || peer.NamespaceSelector.MatchLabels[corev1.LabelMetadataName] != testOperatorNamespace {
			t.Errorf("the operator peer has namespace selector %v, want %s=%s", peer.NamespaceSelector, corev1.LabelMetadataName, testOperatorNamespace)
		}
	}
	if !found {
		t.Error("the NATS fence has no operator peer; the operator could not reach the bus to publish cards")
	}
}

// The second cross-module fixture: the map with the operator and two profiles
// in it, which the callout suite parses and runs a real server against.
func TestRenderedProfilesAuthMapMatchesTheCalloutFixture(t *testing.T) {
	withOperatorBusPrincipal(t)
	agent := authMapTestAgent()
	cm, _, err := buildA2AAuthMapConfigMap(agent, []agentv1alpha1.AgentProfile{
		testAgentProfile(agent.Namespace, "auditor", func(p *agentv1alpha1.AgentProfile) {
			p.Spec.Bus.PublishTopics = []string{"agent.auditor.findings"}
			p.Spec.Bus.SubscribeTopics = []string{"shared.blueprint"}
		}),
		testAgentProfile(agent.Namespace, "cluster-a"),
	})
	if err != nil {
		t.Fatalf("buildA2AAuthMapConfigMap: %v", err)
	}
	checkFixture(t, profilesMapFixturePath, cm.Data[a2aAuthMapKey])
}

func checkFixture(t *testing.T, path, rendered string) {
	t.Helper()
	path = filepath.Clean(path)
	if *updateAuthMapFixture {
		if err := os.WriteFile(path, []byte(rendered), 0o644); err != nil {
			t.Fatalf("writing fixture: %v", err)
		}
		t.Logf("wrote %s", path)
		return
	}
	want, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("reading %s: %v\nRegenerate with: go test ./internal/controller/ -run 'Fixture' -update", path, err)
	}
	if string(want) != rendered {
		t.Errorf("%s and the render have diverged.\nRegenerate with: go test ./internal/controller/ -run 'Fixture' -update\n--- fixture\n%s\n--- rendered\n%s", path, want, rendered)
	}
}

// --- the card ---------------------------------------------------------------

// The operator's card and tombstone bytes, pinned as fixtures the a2a side
// parses with lib.ParseEnvelope and checks against the directory subject's
// agreement rules: the operator cannot import that library, so this is what
// holds its hand-built envelope to the protocol.
func TestTheAgentCardFixtureIsWhatTheOperatorRenders(t *testing.T) {
	at := time.Date(2026, 10, 6, 0, 0, 0, 0, time.UTC)
	// With declared parameters, so the a2a contract test can hold the
	// routing fields' names to lib.AgentCard's.
	card := desiredAgentCard(ptr.To(testAgentProfile("ns", "auditor", func(p *agentv1alpha1.AgentProfile) {
		p.Spec.Parameters = []agentv1alpha1.AgentProfileParameter{{Name: "project", Required: true}, {Name: "location", Required: true}, {Name: "cluster", Required: true}}
	})))
	body, err := renderDirectoryEnvelope("auditor", &card, at, "000000000000000000000000")
	if err != nil {
		t.Fatal(err)
	}
	checkFixture(t, agentCardFixturePath, string(body))
	body, err = renderDirectoryEnvelope("auditor", nil, at, "000000000000000000000001")
	if err != nil {
		t.Fatal(err)
	}
	checkFixture(t, agentClosedFixturePath, string(body))
}

func TestADirectoryEntryIsCurrentOnlyWhenItIsTheSameCard(t *testing.T) {
	p := testAgentProfile("ns", "auditor")
	want := desiredAgentCard(&p)
	at := time.Now()
	cardBytes, _ := renderDirectoryEnvelope("auditor", &want, at, "a")
	closedBytes, _ := renderDirectoryEnvelope("auditor", nil, at, "b")
	staleCard := want
	staleCard.Description = "an older blurb"
	staleBytes, _ := renderDirectoryEnvelope("auditor", &staleCard, at, "c")
	// A card from before the routing fields, or with other tiers, is stale
	// too, so the operator republishes it.
	oldCard := a2aAgentCard{Name: want.Name, Description: want.Description}
	oldBytes, _ := renderDirectoryEnvelope("auditor", &oldCard, at, "d")
	tierCard := want
	tierCard.Tiers = []string{agentv1alpha1.AgentProfileTierRead, agentv1alpha1.AgentProfileTierUnattended}
	tierBytes, _ := renderDirectoryEnvelope("auditor", &tierCard, at, "e")

	for name, tc := range map[string]struct {
		raw  []byte
		want bool
	}{"card": {cardBytes, true}, "tombstone": {closedBytes, false}, "stale card": {staleBytes, false}, "card without routing fields": {oldBytes, false}, "card with other tiers": {tierBytes, false}} {
		entry, err := parseDirectoryEntry(tc.raw)
		if err != nil {
			t.Fatalf("%s: %v", name, err)
		}
		if got := entry.current(want); got != tc.want {
			t.Errorf("%s: current = %v, want %v", name, got, tc.want)
		}
	}
	if (directoryEntry{}).current(want) {
		t.Error("an absent entry reads as current")
	}
}

// --- the reconciler ---------------------------------------------------------

// fakeDirectory is the directory as a map, with a switch to fail every call.
type fakeDirectory struct {
	entries   map[string]directoryEntry
	publishes []string
	down      bool
	// forgets records each namespace whose connection the reconciler
	// dropped, as forgetBus would on the NATS publisher.
	forgets []string
	// durables is each profile's last SyncDurables; durablesRefused makes
	// it fail as a connection opened under an older map would.
	durables        map[string][]profileDurable
	durablesRefused bool
}

func (f *fakeDirectory) forget(namespace string) { f.forgets = append(f.forgets, namespace) }

func (f *fakeDirectory) SyncDurables(_ context.Context, _ *agentv1alpha1.PlatformAgent, profile string, want []profileDurable) error {
	if f.down || f.durablesRefused {
		return errors.New("permissions violation")
	}
	if f.durables == nil {
		f.durables = map[string][]profileDurable{}
	}
	f.durables[profile] = want
	return nil
}

func newFakeDirectory() *fakeDirectory { return &fakeDirectory{entries: map[string]directoryEntry{}} }

func (f *fakeDirectory) Read(_ context.Context, _ *agentv1alpha1.PlatformAgent, profile string) (directoryEntry, error) {
	if f.down {
		return directoryEntry{}, errors.New("bus down")
	}
	return f.entries[profile], nil
}

func (f *fakeDirectory) Publish(_ context.Context, _ *agentv1alpha1.PlatformAgent, profile string, card *a2aAgentCard) error {
	if f.down {
		return errors.New("bus down")
	}
	if card == nil {
		f.entries[profile] = directoryEntry{present: true, kind: a2aKindAgentClosed}
		f.publishes = append(f.publishes, "closed:"+profile)
		return nil
	}
	f.entries[profile] = directoryEntry{present: true, kind: a2aKindAgentCard, card: *card}
	f.publishes = append(f.publishes, "card:"+profile)
	return nil
}

type profileHarness struct {
	t   *testing.T
	c   client.Client
	r   *AgentProfileReconciler
	dir *fakeDirectory
}

func newProfileHarness(t *testing.T, objs ...client.Object) *profileHarness {
	t.Helper()
	scheme := setupScheme()
	// The chart makes the profile namespace on a next install; a harness
	// with an agent gets it, as the profile's ServiceAccounts live there.
	for _, o := range objs {
		if agent, ok := o.(*agentv1alpha1.PlatformAgent); ok {
			objs = append(objs, chartProfileNamespace(agent))
			break
		}
	}
	c := fake.NewClientBuilder().WithScheme(scheme).WithObjects(objs...).
		WithStatusSubresource(&agentv1alpha1.AgentProfile{}).Build()
	dir := newFakeDirectory()
	return &profileHarness{t: t, c: c, r: &AgentProfileReconciler{Client: c, Scheme: scheme, Cards: dir}, dir: dir}
}

// reconcile runs the reconciler until it stops asking for an immediate requeue
// (the finalizer add is one).
func (h *profileHarness) reconcile(ns, name string) ctrl.Result {
	h.t.Helper()
	for i := 0; i < 3; i++ {
		res, err := h.r.Reconcile(context.Background(), ctrl.Request{NamespacedName: types.NamespacedName{Namespace: ns, Name: name}})
		if err != nil {
			h.t.Fatalf("Reconcile: %v", err)
		}
		if !res.Requeue {
			return res
		}
	}
	h.t.Fatal("the reconciler never stopped requeueing")
	return ctrl.Result{}
}

func (h *profileHarness) profile(ns, name string) *agentv1alpha1.AgentProfile {
	h.t.Helper()
	var p agentv1alpha1.AgentProfile
	if err := h.c.Get(context.Background(), types.NamespacedName{Namespace: ns, Name: name}, &p); err != nil {
		h.t.Fatalf("get profile: %v", err)
	}
	return &p
}

func (h *profileHarness) serviceAccount(ns, name string) (*corev1.ServiceAccount, bool) {
	var sa corev1.ServiceAccount
	err := h.c.Get(context.Background(), types.NamespacedName{Namespace: ns, Name: name}, &sa)
	if apierrors.IsNotFound(err) {
		return nil, false
	}
	if err != nil {
		h.t.Fatalf("get sa: %v", err)
	}
	return &sa, true
}

func condition(p *agentv1alpha1.AgentProfile, typ string) metav1.Condition {
	if c := meta.FindStatusCondition(p.Status.Conditions, typ); c != nil {
		return *c
	}
	return metav1.Condition{}
}

// On a today install the same CR renders nothing: no ServiceAccount, no card,
// and conditions that say why.
func TestAnAgentProfileOnATodayInstallRendersNothing(t *testing.T) {
	withOperatorBusPrincipal(t)
	agent := a2aTestAgent()
	agent.Spec.Mode = ptr.To("today")
	p := testAgentProfile(agent.Namespace, "auditor")
	h := newProfileHarness(t, agent, &p)

	h.reconcile(agent.Namespace, "auditor")

	if _, ok := h.serviceAccount(profileNamespaceName(agent), "agentprofile-auditor-read"); ok {
		t.Error("a today install created the profile's ServiceAccount")
	}
	if len(h.dir.publishes) != 0 {
		t.Errorf("a today install published %v", h.dir.publishes)
	}
	got := h.profile(agent.Namespace, "auditor")
	if c := condition(got, agentv1alpha1.AgentProfileConditionIdentityReady); c.Status != metav1.ConditionFalse || c.Reason != reasonAgentProfileNotNext {
		t.Errorf("IdentityReady = %s/%s, want False/%s", c.Status, c.Reason, reasonAgentProfileNotNext)
	}
	if slices.Contains(got.Finalizers, agentProfileFinalizer) {
		t.Error("a today install holds the profile with a finalizer it will never need")
	}
}

// On a next install: the ServiceAccount (owned, no automount), the card, the
// finalizer, and both conditions True. A second reconcile publishes nothing.
func TestAnAgentProfileOnANextInstallRendersItsServiceAccountAndCard(t *testing.T) {
	withOperatorBusPrincipal(t)
	agent := a2aTestAgent()
	p := testAgentProfile(agent.Namespace, "auditor")
	h := newProfileHarness(t, agent, &p)

	res := h.reconcile(agent.Namespace, "auditor")
	if res.RequeueAfter != agentProfileResync {
		t.Errorf("RequeueAfter = %v, want the resync %v so a lost card is republished", res.RequeueAfter, agentProfileResync)
	}

	sa, ok := h.serviceAccount(profileNamespaceName(agent), "agentprofile-auditor-read")
	if !ok {
		t.Fatal("no ServiceAccount for the profile's read tier in the profile namespace")
	}
	if sa.AutomountServiceAccountToken == nil || *sa.AutomountServiceAccountToken {
		t.Error("the profile's ServiceAccount automounts its token; the bus token is a projected volume the pod names")
	}
	got := h.profile(agent.Namespace, "auditor")
	if !agentProfileOwnsServiceAccount(sa, got) || sa.Labels[agentProfileTierLabel] != agentv1alpha1.AgentProfileTierRead {
		t.Error("the profile's ServiceAccount doesn't carry its name, tier and UID, so the operator can't tell it's the profile's to delete")
	}
	if !slices.Contains(got.Finalizers, agentProfileFinalizer) {
		t.Error("no finalizer: deleting the profile would not wait for its tombstone")
	}
	for _, typ := range []string{agentv1alpha1.AgentProfileConditionIdentityReady, agentv1alpha1.AgentProfileConditionCardPublished} {
		if c := condition(got, typ); c.Status != metav1.ConditionTrue {
			t.Errorf("%s = %s/%s %q, want True", typ, c.Status, c.Reason, c.Message)
		}
	}
	if got.Status.ServiceAccountName != "agentprofile-auditor-read" || got.Status.AgentRef != agent.Name {
		t.Errorf("status = %+v", got.Status)
	}
	if !slices.Equal(h.dir.publishes, []string{"card:auditor"}) {
		t.Errorf("publishes = %v, want one card", h.dir.publishes)
	}

	h.reconcile(agent.Namespace, "auditor")
	if len(h.dir.publishes) != 1 {
		t.Errorf("a second reconcile republished an unchanged card: %v", h.dir.publishes)
	}
}

// Level-triggered: a missing, tombstoned or stale entry is republished.
func TestTheCardIsRepublishedWhenTheDirectoryLosesOrStalesIt(t *testing.T) {
	withOperatorBusPrincipal(t)
	agent := a2aTestAgent()
	p := testAgentProfile(agent.Namespace, "auditor")
	h := newProfileHarness(t, agent, &p)
	h.reconcile(agent.Namespace, "auditor")

	for name, entry := range map[string]directoryEntry{
		"missing":    {},
		"tombstoned": {present: true, kind: a2aKindAgentClosed},
		"stale":      {present: true, kind: a2aKindAgentCard, card: a2aAgentCard{Name: "auditor", Description: "old"}},
	} {
		h.dir.entries["auditor"] = entry
		before := len(h.dir.publishes)
		h.reconcile(agent.Namespace, "auditor")
		if len(h.dir.publishes) != before+1 || !h.dir.entries["auditor"].current(desiredAgentCard(&p)) {
			t.Errorf("%s: the card was not republished (publishes %v)", name, h.dir.publishes)
		}
	}
}

// Deletion publishes the tombstone and then releases the finalizer; with the
// bus down the finalizer holds.
func TestDeletingAProfileTombstonesItsCardBeforeReleasingIt(t *testing.T) {
	withOperatorBusPrincipal(t)
	agent := a2aTestAgent()
	p := testAgentProfile(agent.Namespace, "auditor")
	h := newProfileHarness(t, agent, &p)
	h.reconcile(agent.Namespace, "auditor")

	if err := h.c.Delete(context.Background(), h.profile(agent.Namespace, "auditor")); err != nil {
		t.Fatal(err)
	}

	h.dir.down = true
	res := h.reconcile(agent.Namespace, "auditor")
	if res.RequeueAfter != agentProfileBusRetry {
		t.Errorf("RequeueAfter = %v, want %v while the tombstone cannot be published", res.RequeueAfter, agentProfileBusRetry)
	}
	if got := h.profile(agent.Namespace, "auditor"); !slices.Contains(got.Finalizers, agentProfileFinalizer) {
		t.Fatal("the finalizer came off with the bus down; the card would outlive the profile")
	}

	h.dir.down = false
	h.reconcile(agent.Namespace, "auditor")
	if e := h.dir.entries["auditor"]; e.kind != a2aKindAgentClosed {
		t.Errorf("after deletion the directory holds %+v, want the tombstone", e)
	}
	var gone agentv1alpha1.AgentProfile
	if err := h.c.Get(context.Background(), types.NamespacedName{Namespace: agent.Namespace, Name: "auditor"}, &gone); !apierrors.IsNotFound(err) {
		t.Errorf("the profile is still there after its tombstone was published: %v", err)
	}
}

// A profile naming an existing ServiceAccount gets none created, and one that
// names a missing ServiceAccount says so.
func TestANamedServiceAccountIsUsedNotCreated(t *testing.T) {
	withOperatorBusPrincipal(t)
	agent := a2aTestAgent()
	p := testAgentProfile(agent.Namespace, "auditor", func(p *agentv1alpha1.AgentProfile) {
		p.Spec.Identity.ServiceAccountName = "auditor-sa"
	})
	h := newProfileHarness(t, agent, &p)
	h.reconcile(agent.Namespace, "auditor")

	if _, ok := h.serviceAccount(profileNamespaceName(agent), "agentprofile-auditor-read"); ok {
		t.Error("the operator created a ServiceAccount for a profile that names its own")
	}
	got := h.profile(agent.Namespace, "auditor")
	if c := condition(got, agentv1alpha1.AgentProfileConditionIdentityReady); c.Reason != reasonAgentProfileSANotFound || c.Status != metav1.ConditionFalse {
		t.Errorf("IdentityReady = %s/%s, want False/%s", c.Status, c.Reason, reasonAgentProfileSANotFound)
	}
	if got.Status.ServiceAccountName != "auditor-sa" {
		t.Errorf("status.serviceAccountName = %q", got.Status.ServiceAccountName)
	}
}

// A refused profile renders no ServiceAccount and has its card withdrawn.
func TestARefusedProfileRendersNothingAndWithdrawsItsCard(t *testing.T) {
	withOperatorBusPrincipal(t)
	agent := a2aTestAgent()
	p := testAgentProfile(agent.Namespace, "sneaky", func(p *agentv1alpha1.AgentProfile) {
		p.Spec.Identity.ServiceAccountName = a2aSessionServiceAccountName(agent)
	})
	h := newProfileHarness(t, agent, &p)
	h.dir.entries["sneaky"] = directoryEntry{present: true, kind: a2aKindAgentCard, card: a2aAgentCard{Name: "sneaky"}}

	h.reconcile(agent.Namespace, "sneaky")

	got := h.profile(agent.Namespace, "sneaky")
	if c := condition(got, agentv1alpha1.AgentProfileConditionIdentityReady); c.Reason != reasonAgentProfileRefused {
		t.Errorf("IdentityReady reason = %q, want %q", c.Reason, reasonAgentProfileRefused)
	}
	if e := h.dir.entries["sneaky"]; e.kind != a2aKindAgentClosed {
		t.Errorf("a refused profile's card is still on the directory: %+v", e)
	}
}

// A flip to today removes the operator-created ServiceAccount and nothing it
// does not own.
func TestAFlipToTodayRemovesTheProfilesServiceAccount(t *testing.T) {
	withOperatorBusPrincipal(t)
	agent := a2aTestAgent()
	p := testAgentProfile(agent.Namespace, "auditor")
	h := newProfileHarness(t, agent, &p)
	h.reconcile(agent.Namespace, "auditor")
	if _, ok := h.serviceAccount(profileNamespaceName(agent), "agentprofile-auditor-read"); !ok {
		t.Fatal("precondition: no ServiceAccount on next")
	}

	var live agentv1alpha1.PlatformAgent
	if err := h.c.Get(context.Background(), types.NamespacedName{Namespace: agent.Namespace, Name: agent.Name}, &live); err != nil {
		t.Fatal(err)
	}
	live.Spec.Mode = ptr.To("today")
	if err := h.c.Update(context.Background(), &live); err != nil {
		t.Fatal(err)
	}
	h.reconcile(agent.Namespace, "auditor")
	if _, ok := h.serviceAccount(profileNamespaceName(agent), "agentprofile-auditor-read"); ok {
		t.Error("the profile's ServiceAccount survived a flip to today")
	}
	// today tears the bus down, so the connection held for it goes too.
	if !slices.Contains(h.dir.forgets, agent.Namespace) {
		t.Errorf("the bus connection was kept after a flip to today: forgets = %v", h.dir.forgets)
	}
}

// With no PlatformAgent, or two, a profile cannot bind and renders nothing.
func TestAProfileWithNoSinglePlatformAgentRendersNothing(t *testing.T) {
	withOperatorBusPrincipal(t)
	p := testAgentProfile("lonely", "auditor")
	h := newProfileHarness(t, &p)
	h.reconcile("lonely", "auditor")
	if c := condition(h.profile("lonely", "auditor"), agentv1alpha1.AgentProfileConditionIdentityReady); c.Reason != reasonAgentProfileNoAgent {
		t.Errorf("reason = %q, want %q", c.Reason, reasonAgentProfileNoAgent)
	}

	a1, a2 := a2aTestAgent(), a2aTestAgent()
	a2.Name = "second"
	p2 := testAgentProfile(a1.Namespace, "auditor")
	h2 := newProfileHarness(t, a1, a2, &p2)
	h2.reconcile(a1.Namespace, "auditor")
	if c := condition(h2.profile(a1.Namespace, "auditor"), agentv1alpha1.AgentProfileConditionIdentityReady); c.Reason != reasonAgentProfileManyAgents {
		t.Errorf("reason = %q, want %q", c.Reason, reasonAgentProfileManyAgents)
	}
	if _, ok := h2.serviceAccount(a1.Namespace, "agentprofile-auditor"); ok {
		t.Error("a profile that cannot choose between two PlatformAgents created a ServiceAccount")
	}
	if entries := renderedMapEntries(t, a1, nil); len(entries) == 0 {
		t.Fatal("precondition")
	}
	profiles, err := boundAgentProfiles(context.Background(), h2.c, a1)
	if err != nil || len(profiles) != 0 {
		t.Errorf("boundAgentProfiles with two agents = %v, %v; want none", profiles, err)
	}
}

// Without its own ServiceAccount configured the operator still renders the
// ServiceAccount and says why there is no card.
func TestWithoutABusIdentityTheCardConditionSaysWhy(t *testing.T) {
	withoutOperatorBusPrincipal(t)
	agent := a2aTestAgent()
	p := testAgentProfile(agent.Namespace, "auditor")
	h := newProfileHarness(t, agent, &p)
	h.reconcile(agent.Namespace, "auditor")
	if c := condition(h.profile(agent.Namespace, "auditor"), agentv1alpha1.AgentProfileConditionCardPublished); c.Reason != reasonAgentProfileBusUnconfigured {
		t.Errorf("CardPublished reason = %q, want %q", c.Reason, reasonAgentProfileBusUnconfigured)
	}
	if len(h.dir.publishes) != 0 {
		t.Errorf("published %v without a bus identity", h.dir.publishes)
	}
}

// ensureServiceAccount's Get goes through the cache, which can miss a
// ServiceAccount that has just landed; the Create then collides. The
// interceptor makes that exact. Whose ServiceAccount it is gets settled by the
// uncached reader: one created by hand under the operator-created name is not
// adopted, and the operator's own (its create trailing in the cache) is not
// called foreign.
func TestEnsureServiceAccountSettlesACreateCollisionUncached(t *testing.T) {
	for _, ours := range []bool{false, true} {
		agent := a2aTestAgent()
		p := testAgentProfile(agent.Namespace, "auditor")
		scheme := setupScheme()
		landed := &corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{
			Name: "agentprofile-auditor-read", Namespace: profileNamespaceName(agent),
			Labels: map[string]string{agentProfileNameLabel: p.Name},
		}}
		if ours {
			landed.Annotations = map[string]string{agentProfileUIDAnnotation: string(p.UID)}
		}
		base := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent, &p, landed).Build()
		var created bool
		cached := interceptor.NewClient(base, interceptor.Funcs{
			Get: func(ctx context.Context, cl client.WithWatch, key client.ObjectKey, obj client.Object, opts ...client.GetOption) error {
				if _, ok := obj.(*corev1.ServiceAccount); ok && key.Name == landed.Name {
					return apierrors.NewNotFound(corev1.Resource("serviceaccounts"), key.Name)
				}
				return cl.Get(ctx, key, obj, opts...)
			},
			Create: func(ctx context.Context, cl client.WithWatch, obj client.Object, opts ...client.CreateOption) error {
				if _, ok := obj.(*corev1.ServiceAccount); ok {
					created = true
				}
				return cl.Create(ctx, obj, opts...)
			},
			Patch: func(ctx context.Context, cl client.WithWatch, obj client.Object, patch client.Patch, opts ...client.PatchOption) error {
				if _, ok := obj.(*corev1.ServiceAccount); ok {
					t.Error("ensureServiceAccount patched a ServiceAccount after a collision")
				}
				return cl.Patch(ctx, obj, patch, opts...)
			},
		})
		r := &AgentProfileReconciler{Client: cached, APIReader: base, Scheme: scheme, Cards: newFakeDirectory()}
		err := r.ensureServiceAccount(context.Background(), &p, profileNamespaceName(agent), agentProfileTierIdentity{tier: agentv1alpha1.AgentProfileTierRead, serviceAccount: landed.Name})
		switch {
		case !created:
			t.Error("the Create path was never reached; the test is not exercising the race")
		case ours && err != nil:
			t.Errorf("the operator's own ServiceAccount: ensureServiceAccount = %v, want nil", err)
		case !ours && !errors.Is(err, errForeignServiceAccount):
			t.Errorf("a hand-made ServiceAccount: ensureServiceAccount = %v, want errForeignServiceAccount", err)
		}
	}
}

// An agent-scoped publish topic must be the profile's own; reading another
// agent's topic is fine.
func TestAProfileMayPublishOnlyItsOwnAgentTopics(t *testing.T) {
	withoutOperatorBusPrincipal(t)
	agent := a2aTestAgent()
	thief := testAgentProfile(agent.Namespace, "auditor", func(p *agentv1alpha1.AgentProfile) {
		p.Spec.Bus.PublishTopics = []string{"agent.platform.upgrade-readiness"}
	})
	reader := testAgentProfile(agent.Namespace, "reader", func(p *agentv1alpha1.AgentProfile) {
		p.Spec.Bus.PublishTopics = []string{"agent.reader.findings", "shared.blueprint"}
		p.Spec.Bus.SubscribeTopics = []string{"agent.platform.upgrade-readiness"}
	})
	resolved := resolveAgentProfileIdentities(agent, []agentv1alpha1.AgentProfile{thief, reader})
	if resolved["auditor"].refused == nil {
		t.Error("a profile publishing the platform agent's topic was not refused")
	}
	if resolved["reader"].refused != nil {
		t.Errorf("a profile publishing its own and a shared topic was refused: %v", resolved["reader"].refused)
	}
	bad := a2aAuthMapIdentity{ServiceAccount: "system:serviceaccount:ns:sa", User: "profile-auditor", Account: a2aAccountApp, Narrowing: a2aNarrowingProfile, Profile: "auditor",
		Topics: &a2aAuthMapTopics{Publish: []string{"agent.platform.upgrade-readiness"}}}
	if err := validateA2AAuthMapIdentities([]a2aAuthMapIdentity{bad}); err == nil {
		t.Error("the render-time check accepted a publish on another agent's topic")
	}
}

// A profile whose name is not a subject token (only reachable past
// admission) is deleted without any directory call, so a bus that cannot be
// asked about it does not hold the finalizer.
func TestAProfileWithAnInvalidNameIsDeletedWithoutTheBus(t *testing.T) {
	withOperatorBusPrincipal(t)
	agent := a2aTestAgent()
	p := testAgentProfile(agent.Namespace, "a.b", func(p *agentv1alpha1.AgentProfile) {
		p.Finalizers = []string{agentProfileFinalizer}
	})
	h := newProfileHarness(t, agent, &p)
	h.dir.down = true
	if err := h.c.Delete(context.Background(), h.profile(agent.Namespace, "a.b")); err != nil {
		t.Fatal(err)
	}
	h.reconcile(agent.Namespace, "a.b")
	var gone agentv1alpha1.AgentProfile
	if err := h.c.Get(context.Background(), types.NamespacedName{Namespace: agent.Namespace, Name: "a.b"}, &gone); !apierrors.IsNotFound(err) {
		t.Errorf("the profile with an invalid name is still held: %v", err)
	}
}

// An entry that does not decode is content, not an outage: the card replaces
// it, and deletion tombstones it.
func TestAnUndecodableDirectoryEntryIsOverwritten(t *testing.T) {
	for _, raw := range []string{"not json", `{"kind":"agent-card","payload":"x"}`} {
		entry, err := parseDirectoryEntry([]byte(raw))
		if err != nil || !entry.present || entry.current(a2aAgentCard{Name: "auditor"}) {
			t.Errorf("%q: entry=%+v err=%v; want present, not current, no error", raw, entry, err)
		}
	}
	withOperatorBusPrincipal(t)
	agent := a2aTestAgent()
	p := testAgentProfile(agent.Namespace, "auditor")
	h := newProfileHarness(t, agent, &p)
	h.dir.entries["auditor"] = directoryEntry{present: true}
	h.reconcile(agent.Namespace, "auditor")
	if !h.dir.entries["auditor"].current(desiredAgentCard(&p)) {
		t.Errorf("the undecodable entry was not replaced by the card: %+v", h.dir.entries["auditor"])
	}
	h.dir.entries["auditor"] = directoryEntry{present: true}
	if err := h.c.Delete(context.Background(), h.profile(agent.Namespace, "auditor")); err != nil {
		t.Fatal(err)
	}
	h.reconcile(agent.Namespace, "auditor")
	if e := h.dir.entries["auditor"]; e.kind != a2aKindAgentClosed {
		t.Errorf("deletion left the undecodable entry instead of a tombstone: %+v", e)
	}
}

// forgettingDirectory records forget calls.
type forgettingDirectory struct {
	*fakeDirectory
	forgot []string
}

func (f *forgettingDirectory) forget(namespace string) { f.forgot = append(f.forgot, namespace) }

// The namespace-only request (a PlatformAgent changed and no profile is left)
// drops the namespace's bus connection.
func TestTheNamespaceOnlyRequestDropsTheBusConnection(t *testing.T) {
	h := newProfileHarness(t)
	dir := &forgettingDirectory{fakeDirectory: newFakeDirectory()}
	h.r.Cards = dir
	if _, err := h.r.Reconcile(context.Background(), ctrl.Request{NamespacedName: types.NamespacedName{Namespace: "ns"}}); err != nil {
		t.Fatal(err)
	}
	if !slices.Equal(dir.forgot, []string{"ns"}) {
		t.Errorf("forget calls = %v, want [ns]", dir.forgot)
	}
}

// A malformed serviceAccountName past admission is refused alone; the map
// still renders for everyone else.
func TestAMalformedServiceAccountNameDropsOutWithoutFailingTheMap(t *testing.T) {
	withoutOperatorBusPrincipal(t)
	agent := a2aTestAgent()
	bad := testAgentProfile(agent.Namespace, "bad", func(p *agentv1alpha1.AgentProfile) { p.Spec.Identity.ServiceAccountName = "foo:bar" })
	entries := renderedMapEntries(t, agent, []agentv1alpha1.AgentProfile{bad, testAgentProfile(agent.Namespace, "fine")})
	if _, ok := entries["profile-bad-read"]; ok {
		t.Error("a profile with a malformed serviceAccountName rendered")
	}
	if _, ok := entries["profile-fine-read"]; !ok {
		t.Error("the good profile is missing")
	}
}

// Of two profiles naming one ServiceAccount, the older keeps it, whatever the
// names sort to.
func TestTheIncumbentKeepsAContestedServiceAccount(t *testing.T) {
	agent := a2aTestAgent()
	old := testAgentProfile(agent.Namespace, "team", func(p *agentv1alpha1.AgentProfile) {
		p.Spec.Identity.ServiceAccountName = "team-sa"
		p.CreationTimestamp = metav1.NewTime(time.Date(2026, 9, 1, 0, 0, 0, 0, time.UTC))
	})
	newer := testAgentProfile(agent.Namespace, "aaa", func(p *agentv1alpha1.AgentProfile) {
		p.Spec.Identity.ServiceAccountName = "team-sa"
		p.CreationTimestamp = metav1.NewTime(time.Date(2026, 10, 1, 0, 0, 0, 0, time.UTC))
	})
	r := resolveAgentProfileIdentities(agent, []agentv1alpha1.AgentProfile{newer, old})
	if r["team"].refused != nil || r["aaa"].refused == nil {
		t.Errorf("team=%v aaa=%v; want the older team to keep team-sa", r["team"].refused, r["aaa"].refused)
	}
}

// A malformed profile is reported as InvalidProfile, not as a ServiceAccount
// problem.
func TestAMalformedProfileIsReportedAsInvalid(t *testing.T) {
	withOperatorBusPrincipal(t)
	agent := a2aTestAgent()
	p := testAgentProfile(agent.Namespace, "auditor", func(p *agentv1alpha1.AgentProfile) {
		p.Spec.Bus.PublishTopics = []string{"agent.platform.upgrade-readiness"}
	})
	h := newProfileHarness(t, agent, &p)
	h.reconcile(agent.Namespace, "auditor")
	if c := condition(h.profile(agent.Namespace, "auditor"), agentv1alpha1.AgentProfileConditionIdentityReady); c.Reason != reasonAgentProfileInvalid {
		t.Errorf("reason = %q, want %q", c.Reason, reasonAgentProfileInvalid)
	}
}

// Under version skew the bus is frozen, not torn down: a deleting profile
// still gets its tombstone and its finalizer released.
func TestAProfileDeletedUnderVersionSkewIsStillFinalized(t *testing.T) {
	withOperatorBusPrincipal(t)
	agent := a2aTestAgent()
	p := testAgentProfile(agent.Namespace, "auditor")
	h := newProfileHarness(t, agent, &p)
	h.reconcile(agent.Namespace, "auditor")

	var live agentv1alpha1.PlatformAgent
	if err := h.c.Get(context.Background(), types.NamespacedName{Namespace: agent.Namespace, Name: agent.Name}, &live); err != nil {
		t.Fatal(err)
	}
	live.Spec.Mode = ptr.To("later")
	if err := h.c.Update(context.Background(), &live); err != nil {
		t.Fatal(err)
	}
	if err := h.c.Delete(context.Background(), h.profile(agent.Namespace, "auditor")); err != nil {
		t.Fatal(err)
	}
	h.dir.forgets = nil
	h.reconcile(agent.Namespace, "auditor")
	if e := h.dir.entries["auditor"]; e.kind != a2aKindAgentClosed {
		t.Errorf("no tombstone under skew: %+v", e)
	}
	// The frozen bus is still running and the tombstone needs it, so the
	// connection is kept; dropping it here redials on every retry.
	if len(h.dir.forgets) != 0 {
		t.Errorf("the bus connection was dropped under skew: %v", h.dir.forgets)
	}
	var gone agentv1alpha1.AgentProfile
	if err := h.c.Get(context.Background(), types.NamespacedName{Namespace: agent.Namespace, Name: "auditor"}, &gone); !apierrors.IsNotFound(err) {
		t.Errorf("the profile is still held under skew: %v", err)
	}
}

// With no profile left in a namespace, an event there enqueues the
// namespace-only request that drops the bus connection.
func TestAnEmptyNamespaceEnqueuesTheConnectionDrop(t *testing.T) {
	agent := a2aTestAgent()
	h := newProfileHarness(t, agent)
	got := agentProfileRequestsIn(context.Background(), h.c, agent.Namespace)
	if len(got) != 1 || got[0].Name != "" || got[0].Namespace != agent.Namespace {
		t.Errorf("requests = %v, want the one namespace-only request", got)
	}
	p := testAgentProfile(agent.Namespace, "auditor")
	h2 := newProfileHarness(t, agent, &p)
	got = agentProfileRequestsIn(context.Background(), h2.c, agent.Namespace)
	if len(got) != 1 || got[0].Name != "auditor" {
		t.Errorf("requests = %v, want the profile", got)
	}
}

// Each malformed name gets the refusal that says what is wrong with it: a
// dotted name (an older or edited CRD admitted it) is not a label, and only a
// valid label that is too long once prefixed is refused on length.
func TestAProfileNameRefusalSaysWhatIsWrong(t *testing.T) {
	withoutOperatorBusPrincipal(t)
	agent := a2aTestAgent()
	dotted := testAgentProfile(agent.Namespace, "a.b")
	long := testAgentProfile(agent.Namespace, strings.Repeat("a", agentProfileNameMax+1))
	got := resolveAgentProfileIdentities(agent, []agentv1alpha1.AgentProfile{dotted, long})
	for name, want := range map[string]string{dotted.Name: "not a dot-free DNS-1123 label", long.Name: "too long for tier read"} {
		if r := got[name]; r.refused == nil || !strings.Contains(r.refused.Error(), want) {
			t.Errorf("profile %q refused with %v, want a message containing %q", name, r.refused, want)
		}
	}
}

// A profile name too long for its tiers would make a map user
// (profile-<name>-<tier>) longer than one DNS-1123 label, which the callout
// refuses for the whole map. It is refused alone; the map still renders.
func TestAProfileNameTooLongForItsBusIdentityDropsOut(t *testing.T) {
	withoutOperatorBusPrincipal(t)
	agent := a2aTestAgent()
	long := testAgentProfile(agent.Namespace, strings.Repeat("a", agentProfileNameMax+1))
	entries := renderedMapEntries(t, agent, []agentv1alpha1.AgentProfile{long, testAgentProfile(agent.Namespace, "fine")})
	if _, ok := entries["profile-fine-read"]; !ok {
		t.Error("the good profile is missing")
	}
	if len(entries) == 0 {
		t.Fatal("the map did not render")
	}
	for user := range entries {
		if len(user) > 63 {
			t.Errorf("the map carries user %q, longer than a DNS-1123 label", user)
		}
	}
	at := testAgentProfile(agent.Namespace, strings.Repeat("a", agentProfileNameMax))
	if _, ok := renderedMapEntries(t, agent, []agentv1alpha1.AgentProfile{at})["profile-"+at.Name+"-read"]; !ok {
		t.Errorf("a %d-character profile name, the most that fits, was refused", agentProfileNameMax)
	}
}

func TestEnabledAgentProfileNamesAndPodEnvWiring(t *testing.T) {
	withoutOperatorBusPrincipal(t)
	agent := a2aTestAgent()
	chat := testAgentProfile(agent.Namespace, "chat")
	auditor := testAgentProfile(agent.Namespace, "auditor")
	if got := enabledAgentProfileNames(agent, []agentv1alpha1.AgentProfile{chat, auditor}); len(got) != 0 {
		t.Fatalf("enabledAgentProfileNames(chat, auditor) = %v, want empty (non-optional Hermes profiles must not roll the gateway)", got)
	}

	tse := testAgentProfile(agent.Namespace, "tse", func(p *agentv1alpha1.AgentProfile) {
		p.Spec.Bus.PublishTopics = []string{"agent.tse.findings"}
		p.Spec.Bus.SubscribeTopics = []string{"shared.blueprint", "agent.platform.upgrade-readiness"}
	})
	now := metav1.Now()
	deletingTSE := testAgentProfile(agent.Namespace, "tse", func(p *agentv1alpha1.AgentProfile) {
		p.DeletionTimestamp = &now
		p.Finalizers = []string{"test"}
	})
	if got := enabledAgentProfileNames(agent, []agentv1alpha1.AgentProfile{deletingTSE}); len(got) != 0 {
		t.Fatalf("enabledAgentProfileNames(deleting tse) = %v, want empty", got)
	}
	refusedTSE := testAgentProfile(agent.Namespace, "tse", func(p *agentv1alpha1.AgentProfile) {
		p.Spec.Bus.PublishTopics = []string{"agent.platform.x"}
	})
	if got := enabledAgentProfileNames(agent, []agentv1alpha1.AgentProfile{refusedTSE}); len(got) != 0 {
		t.Fatalf("enabledAgentProfileNames(refused tse) = %v, want empty", got)
	}

	refused := testAgentProfile(agent.Namespace, "bad.name")
	deleting := testAgentProfile(agent.Namespace, "closing", func(p *agentv1alpha1.AgentProfile) {
		p.DeletionTimestamp = &now
	})
	names := enabledAgentProfileNames(agent, []agentv1alpha1.AgentProfile{chat, auditor, deleting, refused, tse})
	if !slices.Equal(names, []string{"tse"}) {
		t.Fatalf("enabledAgentProfileNames = %v, want [tse]", names)
	}

	dep := buildDeployment(agent, "cfg", "fb", "set", "pol", nil, renderOptions{enabledProfiles: names})
	if got, _ := envValue(containerNamed(t, dep, appNamePlatformAgent), enabledProfilesEnvVar); got != "tse" {
		t.Errorf("%s = %q, want %q", enabledProfilesEnvVar, got, "tse")
	}

	depEmpty := buildDeployment(agent, "cfg", "fb", "set", "pol", nil, renderOptions{})
	if got, found := envValue(containerNamed(t, depEmpty, appNamePlatformAgent), enabledProfilesEnvVar); found {
		t.Errorf("%s rendered when enabledProfiles is empty: %q", enabledProfilesEnvVar, got)
	}

	// 1. Healthy projection path: seed the bound `tse` AgentProfile (with no pre-existing
	// gateway Deployment) and drive reconcileWorkload on the un-skewed, readable agent so
	// boundAgentProfiles -> enabledAgentProfileNames projects KUBE_AGENTS_ENABLED_PROFILES=tse.
	h := newProfileHarness(t, agent, &tse)
	reconciler := &PlatformAgentReconciler{Client: h.c, Scheme: h.c.Scheme()}
	if err := reconciler.reconcileWorkload(context.Background(), agent, "cfg", "fb", "set", "pol", nil, "", false); err != nil {
		t.Fatalf("reconcileWorkload with bound tse AgentProfile: %v", err)
	}
	var liveDep appsv1.Deployment
	if err := h.c.Get(context.Background(), client.ObjectKey{Namespace: agent.Namespace, Name: agent.Name + gatewayAppSuffix}, &liveDep); err != nil {
		t.Fatalf("getting applied gateway Deployment after healthy reconcile: %v", err)
	}
	if got, _ := envValue(containerNamed(t, &liveDep, appNamePlatformAgent), enabledProfilesEnvVar); got != "tse" {
		t.Fatalf("gateway Deployment %s after healthy reconcileWorkload = %q, want %q", enabledProfilesEnvVar, got, "tse")
	}
	held, err := reconciler.heldEnabledProfiles(context.Background(), agent)
	if err != nil || !slices.Equal(held, []string{"tse"}) {
		t.Errorf("heldEnabledProfiles = %v, %v; want [tse], nil", held, err)
	}

	// 2. Delete the `tse` AgentProfile from the fake client, then verify both hold branches
	// (mode skew and agentProfilesUnreadable) preserve KUBE_AGENTS_ENABLED_PROFILES=tse on
	// the live gateway Deployment before the healthy removal pass clears it.
	if err := h.c.Delete(context.Background(), &tse); err != nil {
		t.Fatalf("deleting tse AgentProfile: %v", err)
	}
	skewedAgent := agent.DeepCopy()
	skewedAgent.Spec.Mode = ptr.To("quantum")
	if err := reconciler.reconcileWorkload(context.Background(), skewedAgent, "cfg", "fb", "set", "pol", nil, "", false); err != nil {
		t.Fatalf("reconcileWorkload under mode skew: %v", err)
	}
	if err := h.c.Get(context.Background(), client.ObjectKey{Namespace: agent.Namespace, Name: agent.Name + gatewayAppSuffix}, &liveDep); err != nil {
		t.Fatalf("getting applied gateway Deployment after skew reconcile: %v", err)
	}
	if got, _ := envValue(containerNamed(t, &liveDep, appNamePlatformAgent), enabledProfilesEnvVar); got != "tse" {
		t.Errorf("gateway Deployment %s after mode-skew reconcileWorkload = %q, want %q", enabledProfilesEnvVar, got, "tse")
	}

	reconciler.agentProfilesUnreadable = true
	if err := reconciler.reconcileWorkload(context.Background(), agent, "cfg", "fb", "set", "pol", nil, "", false); err != nil {
		t.Fatalf("reconcileWorkload under agentProfilesUnreadable: %v", err)
	}
	if err := h.c.Get(context.Background(), client.ObjectKey{Namespace: agent.Namespace, Name: agent.Name + gatewayAppSuffix}, &liveDep); err != nil {
		t.Fatalf("getting applied gateway Deployment after unreadable-profiles reconcile: %v", err)
	}
	if got, _ := envValue(containerNamed(t, &liveDep, appNamePlatformAgent), enabledProfilesEnvVar); got != "tse" {
		t.Errorf("gateway Deployment %s after agentProfilesUnreadable reconcileWorkload = %q, want %q", enabledProfilesEnvVar, got, "tse")
	}

	// 3. Healthy removal path: once AgentProfile reads succeed again with `tse` deleted,
	// reconcileWorkload removes KUBE_AGENTS_ENABLED_PROFILES from the gateway Deployment.
	reconciler.agentProfilesUnreadable = false
	if err := reconciler.reconcileWorkload(context.Background(), agent, "cfg", "fb", "set", "pol", nil, "", false); err != nil {
		t.Fatalf("reconcileWorkload after deleting tse AgentProfile: %v", err)
	}
	if err := h.c.Get(context.Background(), client.ObjectKey{Namespace: agent.Namespace, Name: agent.Name + gatewayAppSuffix}, &liveDep); err != nil {
		t.Fatalf("getting applied gateway Deployment after removal reconcile: %v", err)
	}
	if got, found := envValue(containerNamed(t, &liveDep, appNamePlatformAgent), enabledProfilesEnvVar); found {
		t.Errorf("gateway Deployment %s still set after deleting tse AgentProfile: %q", enabledProfilesEnvVar, got)
	}
}

func TestTheTSEProfileExampleIsAStrictAgentProfile(t *testing.T) {
	raw, err := os.ReadFile(tseProfileExamplePath)
	if err != nil {
		t.Fatalf("reading the tse profile: %v", err)
	}
	var p agentv1alpha1.AgentProfile
	if err := yaml.UnmarshalStrict(raw, &p); err != nil {
		t.Fatalf("the tse profile is not a strict AgentProfile: %v", err)
	}
	if p.Kind != "AgentProfile" || p.Name != "tse" {
		t.Errorf("kind/name = %q/%q, want AgentProfile/tse", p.Kind, p.Name)
	}
	if p.Spec.Identity.ServiceAccountName != "" {
		t.Errorf("the tse profile names ServiceAccount %q; it runs with an operator-created one holding nothing", p.Spec.Identity.ServiceAccountName)
	}
	script := a2aProvisionScript(a2aTestAgent())
	for _, grant := range append(append([]string(nil), p.Spec.Bus.PublishTopics...), p.Spec.Bus.SubscribeTopics...) {
		if !strings.Contains(script, "a2a.topics."+grant) {
			t.Errorf("the tse profile grants %q, which the provision script does not provision", grant)
		}
	}
}

// A per-tier ServiceAccount has no owner reference, so a change to one has to
// map back to its profile by label and namespace, or a hand delete waits for
// the resync.
func TestAProfileServiceAccountChangeWakesItsProfile(t *testing.T) {
	sa := func(ns string, labels map[string]string) *corev1.ServiceAccount {
		return &corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{Name: "agentprofile-auditor-read", Namespace: ns, Labels: labels}}
	}
	named := map[string]string{agentProfileNameLabel: "auditor"}
	got := profileServiceAccountRequest(sa("kubeagents-system-profiles", named))
	if len(got) != 1 || got[0].Namespace != "kubeagents-system" || got[0].Name != "auditor" {
		t.Errorf("requests = %v, want kubeagents-system/auditor", got)
	}
	for _, other := range []*corev1.ServiceAccount{
		sa("kubeagents-system", named),
		sa("-profiles", named),
		sa("kubeagents-system-profiles", nil),
	} {
		if got := profileServiceAccountRequest(other); len(got) != 0 {
			t.Errorf("%s/%v mapped to %v, want nothing", other.Namespace, other.Labels, got)
		}
	}
}

// Two tiers get two ServiceAccounts in the profile namespace, and dropping a
// tier deletes its ServiceAccount and leaves the other.
func TestEachTierGetsAServiceAccountAndADroppedTierLosesIt(t *testing.T) {
	withOperatorBusPrincipal(t)
	agent := a2aTestAgent()
	p := testAgentProfile(agent.Namespace, "auditor", func(p *agentv1alpha1.AgentProfile) {
		p.Spec.AllowedTiers = []string{agentv1alpha1.AgentProfileTierRead, agentv1alpha1.AgentProfileTierUnattended}
	})
	h := newProfileHarness(t, agent, &p)
	h.reconcile(agent.Namespace, "auditor")
	ns := profileNamespaceName(agent)
	for _, tier := range []string{"read", "unattended"} {
		sa, ok := h.serviceAccount(ns, "agentprofile-auditor-"+tier)
		if !ok {
			t.Fatalf("no ServiceAccount for tier %s", tier)
		}
		if sa.Labels[agentProfileTierLabel] != tier {
			t.Errorf("tier %s ServiceAccount has tier label %q", tier, sa.Labels[agentProfileTierLabel])
		}
	}
	if got := h.profile(agent.Namespace, "auditor").Status.ServiceAccountName; got != "agentprofile-auditor-read, agentprofile-auditor-unattended" {
		t.Errorf("status.serviceAccountName = %q", got)
	}

	live := h.profile(agent.Namespace, "auditor")
	live.Spec.AllowedTiers = []string{agentv1alpha1.AgentProfileTierRead}
	if err := h.c.Update(context.Background(), live); err != nil {
		t.Fatal(err)
	}
	h.reconcile(agent.Namespace, "auditor")
	if _, ok := h.serviceAccount(ns, "agentprofile-auditor-unattended"); ok {
		t.Error("the dropped tier's ServiceAccount survived")
	}
	if _, ok := h.serviceAccount(ns, "agentprofile-auditor-read"); !ok {
		t.Error("the kept tier's ServiceAccount was deleted")
	}
}

// A profile deleted after its PlatformAgent still removes its ServiceAccounts
// from the profile namespace, which outlives the agent. Left behind, they'd
// carry the old UID, and a re-created profile of the same name would find
// them foreign and be refused until someone deleted them by hand.
func TestAProfileDeletedWithoutItsAgentRemovesItsServiceAccounts(t *testing.T) {
	withOperatorBusPrincipal(t)
	agent := a2aTestAgent()
	p := testAgentProfile(agent.Namespace, "auditor")
	h := newProfileHarness(t, agent, &p)
	h.reconcile(agent.Namespace, "auditor")
	ns := profileNamespaceName(agent)
	if _, ok := h.serviceAccount(ns, "agentprofile-auditor-read"); !ok {
		t.Fatal("precondition: no ServiceAccount")
	}
	ctx := context.Background()
	if err := h.c.Delete(ctx, agent); err != nil {
		t.Fatal(err)
	}
	if err := h.c.Delete(ctx, h.profile(agent.Namespace, "auditor")); err != nil {
		t.Fatal(err)
	}
	if _, err := h.r.Reconcile(ctx, ctrl.Request{NamespacedName: types.NamespacedName{Namespace: agent.Namespace, Name: "auditor"}}); err != nil {
		t.Fatalf("Reconcile: %v", err)
	}
	if _, ok := h.serviceAccount(ns, "agentprofile-auditor-read"); ok {
		t.Error("the profile's ServiceAccount survived its deletion with no PlatformAgent")
	}
	var gone agentv1alpha1.AgentProfile
	if err := h.c.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: "auditor"}, &gone); !apierrors.IsNotFound(err) {
		t.Errorf("the profile is still there (finalizers %v, err %v)", gone.Finalizers, err)
	}
}

// The single ServiceAccount #2469 made in the agent's namespace is deleted
// once the per-tier ones replace it.
func TestTheAgentNamespaceServiceAccountIsDeleted(t *testing.T) {
	withOperatorBusPrincipal(t)
	agent := a2aTestAgent()
	p := testAgentProfile(agent.Namespace, "auditor")
	legacy := &corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{Name: "agentprofile-auditor", Namespace: agent.Namespace}}
	if err := ctrl.SetControllerReference(&p, legacy, setupScheme()); err != nil {
		t.Fatal(err)
	}
	h := newProfileHarness(t, agent, &p, legacy)
	h.reconcile(agent.Namespace, "auditor")
	if _, ok := h.serviceAccount(agent.Namespace, "agentprofile-auditor"); ok {
		t.Error("the agent-namespace ServiceAccount survived")
	}
}

// With no profile namespace there is nowhere for the ServiceAccounts, so the
// profile renders none and says why.
func TestAProfileWithNoProfileNamespaceRendersNoIdentity(t *testing.T) {
	withOperatorBusPrincipal(t)
	agent := a2aTestAgent()
	p := testAgentProfile(agent.Namespace, "auditor")
	h := newProfileHarness(t, agent, &p)
	if err := h.c.Delete(context.Background(), chartProfileNamespace(agent)); err != nil {
		t.Fatal(err)
	}
	h.reconcile(agent.Namespace, "auditor")
	if c := condition(h.profile(agent.Namespace, "auditor"), agentv1alpha1.AgentProfileConditionIdentityReady); c.Status != metav1.ConditionFalse || c.Reason != reasonAgentProfileNamespaceAbsent {
		t.Errorf("IdentityReady = %s/%s, want False/%s", c.Status, c.Reason, reasonAgentProfileNamespaceAbsent)
	}
	if _, ok := h.serviceAccount(profileNamespaceName(agent), "agentprofile-auditor-read"); ok {
		t.Error("a ServiceAccount was created with no profile namespace")
	}
}

// The operator repeats the CRD's unattended name bound: 45 characters with
// unattended is refused alone, and the map still renders.
func TestAProfileNameTooLongForUnattendedDropsOut(t *testing.T) {
	agent := a2aTestAgent()
	long := testAgentProfile(agent.Namespace, strings.Repeat("u", 45), func(p *agentv1alpha1.AgentProfile) {
		p.Spec.AllowedTiers = []string{agentv1alpha1.AgentProfileTierRead, agentv1alpha1.AgentProfileTierUnattended}
	})
	entries := renderedMapEntries(t, agent, []agentv1alpha1.AgentProfile{long, testAgentProfile(agent.Namespace, "fine")})
	if _, ok := entries["profile-fine-read"]; !ok {
		t.Error("the good profile is missing")
	}
	for user := range entries {
		if strings.HasPrefix(user, "profile-uuu") {
			t.Errorf("the too-long profile rendered %s", user)
		}
	}
	at := testAgentProfile(agent.Namespace, strings.Repeat("u", 44), func(p *agentv1alpha1.AgentProfile) {
		p.Spec.AllowedTiers = []string{agentv1alpha1.AgentProfileTierUnattended}
	})
	if _, ok := renderedMapEntries(t, agent, []agentv1alpha1.AgentProfile{at})["profile-"+at.Name+"-unattended"]; !ok {
		t.Error("a 44-character unattended profile was refused")
	}
}

// The PlatformAgent's ServiceAccount watch maps a profile namespace to the
// agent beside it, so a foreign ServiceAccount appearing there re-renders the
// map without waiting for the periodic requeue.
func TestAProfileServiceAccountChangeWakesTheAgentBesideIt(t *testing.T) {
	if got, ok := agentNamespaceOfProfileServiceAccount("kubeagents-system-profiles"); !ok || got != "kubeagents-system" {
		t.Errorf("kubeagents-system-profiles maps to %q, %v; want kubeagents-system", got, ok)
	}
	// Nothing else maps, and never to "", which would enqueue every agent.
	for _, ns := range []string{"kubeagents-system", "-profiles"} {
		if got, ok := agentNamespaceOfProfileServiceAccount(ns); ok {
			t.Errorf("%s maps to %q, want nothing", ns, got)
		}
	}
}

// The card carries the profile's tiers, its pinned cluster and its declared
// parameters, which is what the gateway routes on.
func TestTheCardCarriesWhatTheGatewayRoutesOn(t *testing.T) {
	p := testAgentProfile("ns", "prod-a", func(p *agentv1alpha1.AgentProfile) {
		p.Spec.AllowedTiers = []string{agentv1alpha1.AgentProfileTierRead, agentv1alpha1.AgentProfileTierUnattended}
		p.Spec.ClusterRef = &agentv1alpha1.AgentProfileClusterRef{ProjectID: "p", Location: "us-central1", Cluster: "a"}
		p.Spec.Parameters = []agentv1alpha1.AgentProfileParameter{{Name: "namespace"}}
	})
	card := desiredAgentCard(&p)
	if !slices.Equal(card.Tiers, []string{"read", "unattended"}) {
		t.Errorf("tiers = %v", card.Tiers)
	}
	if card.ClusterRef == nil || *card.ClusterRef != (a2aCardClusterRef{ProjectID: "p", Location: "us-central1", Cluster: "a"}) {
		t.Errorf("clusterRef = %+v", card.ClusterRef)
	}
	if len(card.Parameters) != 1 || card.Parameters[0] != (a2aCardParameter{Name: "namespace"}) {
		t.Errorf("parameters = %+v", card.Parameters)
	}
	// A profile with neither: read alone, no pin, no parameters.
	bare := desiredAgentCard(ptr.To(testAgentProfile("ns", "bare")))
	if !slices.Equal(bare.Tiers, []string{"read"}) || bare.ClusterRef != nil || bare.Parameters != nil {
		t.Errorf("bare card = %+v", bare)
	}
}

// A durable sync that fails drops the bus connection, so the retry reconnects
// under the map the callout serves now: grants are fixed when a connection
// opens, and one opened before the map granted the operator its durables
// would be refused for as long as it stayed open.
func TestAFailedDurableSyncDropsTheBusConnection(t *testing.T) {
	withOperatorBusPrincipal(t)
	t.Setenv(a2aDispatcherImageEnvVar, "registry.example/a2a-dispatcher:test")
	agent := a2aTestAgent()
	p := testAgentProfile(agent.Namespace, "auditor")
	h := newProfileHarness(t, agent, &p)
	h.dir.durablesRefused = true
	h.reconcile(agent.Namespace, "auditor")
	if !slices.Contains(h.dir.forgets, agent.Namespace) {
		t.Errorf("a refused durable sync kept the connection: forgets = %v", h.dir.forgets)
	}
	h.dir.durablesRefused = false
	h.reconcile(agent.Namespace, "auditor")
	if got := h.dir.durables["auditor"]; len(got) != 2 {
		t.Errorf("after the reconnect the durables = %+v, want read top and child", got)
	}
}
