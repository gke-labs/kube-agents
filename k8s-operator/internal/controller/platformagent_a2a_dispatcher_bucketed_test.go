package controller

import (
	"encoding/json"
	"fmt"
	"slices"
	"strings"
	"testing"
	"time"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// dispatcherMapBucketedFixturePath is the identity map with the dispatcher's
// entry narrowing on "dispatcher" (#5381), a live profile and a draining
// one. The callout suite parses it and connects a bucket against it.
const dispatcherMapBucketedFixturePath = "../../../a2a/authcallout/testdata/rendered-identity-map-dispatcher-bucketed.json"

func withBucketedGrants(t *testing.T) {
	t.Helper()
	t.Setenv(a2aDispatcherBucketedGrantsEnvVar, "true")
}

func dispatcherEntryOf(t *testing.T, doc a2aAuthMapDocument) a2aAuthMapIdentity {
	t.Helper()
	for _, id := range doc.Identities {
		if id.User == a2aDispatcherUser {
			return id
		}
	}
	t.Fatal("the map has no dispatcher entry")
	return a2aAuthMapIdentity{}
}

// With bucketed grants on, the dispatcher's entry narrows on "dispatcher",
// lands in TRUSTED, and carries exactly the fixed grants (the control
// connection's) and the draining profiles' names. No subject names a
// profile, live or draining.
func TestBucketedGrantsRenderTheFixedSetAndTheDrainingNames(t *testing.T) {
	withDispatcher(t)
	withBucketedGrants(t)
	withOperatorBusPrincipal(t)
	agent := authMapTestAgent()
	tse := testAgentProfile(agent.Namespace, "tse", func(p *agentv1alpha1.AgentProfile) {
		p.Spec.AllowedTiers = []string{agentv1alpha1.AgentProfileTierRead, agentv1alpha1.AgentProfileTierUnattended}
	})
	draining := []a2aDispatchProfile{{Name: "gone", Concurrency: 2, Tiers: []string{"read"}, ActiveDeadlineSeconds: 1800,
		Durables: []a2aDispatchDurable{}, DrainingUntil: time.Date(2026, 11, 6, 12, 45, 0, 0, time.UTC)}}

	doc, err := renderA2AAuthMapDraining(agent, []agentv1alpha1.AgentProfile{tse}, draining)
	if err != nil {
		t.Fatal(err)
	}
	id := dispatcherEntryOf(t, doc)
	fixed := dispatcherIdentity(agent, agent.Namespace, nil, nil)
	if id.Narrowing != a2aNarrowingDispatcher || id.Account != a2aAccountControl {
		t.Fatalf("narrowing/account = %q/%q", id.Narrowing, id.Account)
	}
	if !slices.Equal(id.Grants.Publish, fixed.publish) || !slices.Equal(id.Grants.Subscribe, fixed.subscribe) {
		t.Fatalf("grants = %+v, want the fixed set %v / %v", id.Grants, fixed.publish, fixed.subscribe)
	}
	if !slices.Equal(id.Draining, []string{"gone"}) {
		t.Fatalf("draining = %v, want [gone]", id.Draining)
	}
	for _, s := range append(append([]string{}, id.Grants.Publish...), id.Grants.Subscribe...) {
		if strings.Contains(s, "tse") || strings.Contains(s, "gone") {
			t.Errorf("the bucketed entry carries %q, which names a profile", s)
		}
	}

	shards, _, err := buildA2AAuthMapConfigMaps(agent, []agentv1alpha1.AgentProfile{tse}, draining)
	if err != nil || len(shards) != 1 {
		t.Fatalf("the bucketed map: %d shards, %v", len(shards), err)
	}
	checkFixture(t, dispatcherMapBucketedFixturePath, shards[0].Data[a2aAuthMapKey])
}

// Off is today's render, byte for byte: the flag changes nothing until set,
// and only the exact string "true" sets it.
func TestBucketedGrantsOffIsTodaysEntry(t *testing.T) {
	withDispatcher(t)
	withOperatorBusPrincipal(t)
	agent := authMapTestAgent()
	tse := testAgentProfile(agent.Namespace, "tse")
	for _, v := range []string{"", "false", "TRUE", "1", "yes", " true1"} {
		t.Setenv(a2aDispatcherBucketedGrantsEnvVar, v)
		doc, err := renderA2AAuthMap(agent, []agentv1alpha1.AgentProfile{tse})
		if err != nil {
			t.Fatal(err)
		}
		id := dispatcherEntryOf(t, doc)
		if id.Narrowing != "" || len(id.Draining) != 0 || !slices.Contains(id.Grants.Subscribe, "a2a.stop.tse.*") {
			t.Errorf("%s=%q rendered %+v; want today's entry", a2aDispatcherBucketedGrantsEnvVar, v, id)
		}
	}
}

// The operator refuses what the callout would: the dispatcher's entry in
// APP, another narrowing in TRUSTED, draining on any other entry, an empty
// side, two dispatchers, and a bucket user's name held by an entry or a
// profile.
func TestTheOperatorRefusesAnUnsoundDispatcherEntry(t *testing.T) {
	withDispatcher(t)
	withBucketedGrants(t)
	withOperatorBusPrincipal(t)
	agent := authMapTestAgent()
	doc, err := renderA2AAuthMap(agent, []agentv1alpha1.AgentProfile{testAgentProfile(agent.Namespace, "tse")})
	if err != nil {
		t.Fatal(err)
	}
	base := doc.Identities
	disp := dispatcherEntryOf(t, doc)
	edit := func(f func(ids []a2aAuthMapIdentity) []a2aAuthMapIdentity) []a2aAuthMapIdentity {
		ids := make([]a2aAuthMapIdentity, len(base))
		copy(ids, base)
		return f(ids)
	}
	idx := slices.IndexFunc(base, func(id a2aAuthMapIdentity) bool { return id.User == a2aDispatcherUser })
	prof := slices.IndexFunc(base, func(id a2aAuthMapIdentity) bool { return id.Narrowing == a2aNarrowingProfile })
	cases := map[string][]a2aAuthMapIdentity{
		"in APP": edit(func(ids []a2aAuthMapIdentity) []a2aAuthMapIdentity { ids[idx].Account = a2aAccountApp; return ids }),
		"empty publish": edit(func(ids []a2aAuthMapIdentity) []a2aAuthMapIdentity {
			ids[idx].Grants.Publish = nil
			return ids
		}),
		"profile in TRUSTED": edit(func(ids []a2aAuthMapIdentity) []a2aAuthMapIdentity { ids[prof].Account = a2aAccountControl; return ids }),
		"draining on a profile": edit(func(ids []a2aAuthMapIdentity) []a2aAuthMapIdentity {
			ids[prof].Draining = []string{"x"}
			return ids
		}),
		"a bad draining name": edit(func(ids []a2aAuthMapIdentity) []a2aAuthMapIdentity {
			ids[idx].Draining = []string{"a.b"}
			return ids
		}),
		"two dispatchers": edit(func(ids []a2aAuthMapIdentity) []a2aAuthMapIdentity {
			second := disp
			second.ServiceAccount, second.User = disp.ServiceAccount+"2", "dispatcher2"
			return append(ids, second)
		}),
		"a profile named for a bucket user": edit(func(ids []a2aAuthMapIdentity) []a2aAuthMapIdentity {
			ids[prof].Profile = fmt.Sprintf("%s-b7", a2aDispatcherUser)
			return ids
		}),
	}
	for name, ids := range cases {
		if err := validateA2AAuthMapIdentities(ids); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
	if err := validateA2AAuthMapIdentities(base); err != nil {
		t.Fatalf("the sound render was refused: %v", err)
	}
}

// At N=2,500 with bucketed grants on, the dispatcher's entry is its fixed
// size, whatever the profile count, and the whole map stays under the
// ConfigMap gate (the dispatcher-off shape this issue's table measured).
func TestTheIdentityMapAtFleetScaleWithBucketedGrants(t *testing.T) {
	withDispatcher(t)
	withBucketedGrants(t)
	agent := a2aTestAgent()
	profiles := fleetScaleProfileFixtures(t, agent)

	small, err := renderA2AAuthMap(agent, profiles[:1])
	if err != nil {
		t.Fatal(err)
	}
	full, err := renderA2AAuthMap(agent, profiles)
	if err != nil {
		t.Fatal(err)
	}
	a, _ := json.Marshal(dispatcherEntryOf(t, small))
	b, _ := json.Marshal(dispatcherEntryOf(t, full))
	if len(a) != len(b) {
		t.Fatalf("the dispatcher's entry is %d bytes at 1 profile and %d at %d; with bucketed grants it must not grow", len(a), len(b), fleetScaleProfiles)
	}
	cms, _, err := buildA2AAuthMapConfigMaps(agent, profiles, nil)
	if err != nil {
		t.Fatalf("rendering the identity map for %d profiles: %v", fleetScaleProfiles, err)
	}
	for i, cm := range cms {
		requireConfigMapSizeGate(t, cm, fmt.Sprintf("bucketed, N=%d profiles, shard %d of %d", fleetScaleProfiles, i, len(cms)), "")
	}
	t.Logf("dispatcher entry %d bytes at N=%d; map in %d shards", len(b), fleetScaleProfiles, len(cms))
}

// An applied dispatch config naming a reserved addressee as a profile (the
// bridge's) is not one this operator wrote: it is dropped from the draining
// set rather than granted that addressee's .supervisor subject, with the flag
// on or off. The mirror refuses it on the entry too.
func TestAReservedAddresseeIsNeverADrainingProfile(t *testing.T) {
	now := time.Date(2026, 11, 6, 12, 0, 0, 0, time.UTC)
	applied := &a2aDispatchConfig{Profiles: []a2aDispatchProfile{
		{Name: a2aBridgeAddressee, Tiers: []string{"read"}},
		{Name: "gone", Tiers: []string{"read"}},
	}}
	got := drainingDispatchProfiles(applied, map[string]bool{}, now)
	if len(got) != 1 || got[0].Name != "gone" {
		t.Fatalf("draining = %+v, want gone alone", got)
	}

	withDispatcher(t)
	withBucketedGrants(t)
	withOperatorBusPrincipal(t)
	agent := authMapTestAgent()
	doc, err := renderA2AAuthMap(agent, []agentv1alpha1.AgentProfile{testAgentProfile(agent.Namespace, "tse")})
	if err != nil {
		t.Fatal(err)
	}
	ids := doc.Identities
	i := slices.IndexFunc(ids, func(id a2aAuthMapIdentity) bool { return id.User == a2aDispatcherUser })
	ids[i].Draining = []string{a2aBridgeAddressee}
	if err := validateA2AAuthMapIdentities(ids); err == nil {
		t.Fatal("the mirror accepted a draining reserved addressee")
	}
}
