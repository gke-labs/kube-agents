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

// Bucketed grants are on by default (#5381). Only "false" turns
// them off, which restores today's entry; any other value, unset included, is
// on. The dispatcher's own Deployment carries the same switch.
func TestBucketedGrantsAreOnUnlessFalse(t *testing.T) {
	withDispatcher(t)
	withOperatorBusPrincipal(t)
	agent := authMapTestAgent()
	tse := testAgentProfile(agent.Namespace, "tse")
	for _, v := range []string{"", "true", "TRUE", "1", "False", "false", " false "} {
		t.Setenv(a2aDispatcherBucketedGrantsEnvVar, v)
		doc, err := renderA2AAuthMap(agent, []agentv1alpha1.AgentProfile{tse})
		if err != nil {
			t.Fatal(err)
		}
		id := dispatcherEntryOf(t, doc)
		want := strings.TrimSpace(v) != "false"
		if got := id.Narrowing == a2aNarrowingDispatcher; got != want {
			t.Errorf("%s=%q: bucketed = %v, want %v", a2aDispatcherBucketedGrantsEnvVar, v, got, want)
		}
		if !want && (len(id.Draining) != 0 || !slices.Contains(id.Grants.Subscribe, "a2a.stop.tse.*")) {
			t.Errorf("%s=%q rendered %+v; want today's entry", a2aDispatcherBucketedGrantsEnvVar, v, id)
		}
		env := map[string]string{}
		for _, e := range buildA2ADispatcherDeployment(agent).Spec.Template.Spec.Containers[0].Env {
			env[e.Name] = e.Value
		}
		if env[a2aDispatcherBucketedGrantsEnvVar] != fmt.Sprint(want) {
			t.Errorf("%s=%q: the dispatcher's env says %q, want %v; the process must open its buckets exactly when the map is split",
				a2aDispatcherBucketedGrantsEnvVar, v, env[a2aDispatcherBucketedGrantsEnvVar], want)
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

	// The entry itself: renderA2AAuthMap also applies #5394's ceiling, and
	// at this N the dispatch config, not the entry, is past its budget
	// (#5479), so the map would carry no dispatcher entry to compare.
	a, _ := json.Marshal(dispatcherMapEntry(agent, profiles[:1], nil, nil))
	b, _ := json.Marshal(dispatcherMapEntry(agent, profiles, nil, nil))
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

// A static principal's name (gateway, bridge, ...) is not carried into the
// draining set, though a live profile may hold one: the callout would leave
// it out of every bucket's grants anyway. The mirror refuses a draining name
// that is an entry's user, so the operator never publishes such a map as
// healthy (#5553, round 1).
func TestAStaticPrincipalIsNeverADrainingProfile(t *testing.T) {
	withDispatcher(t)
	withBucketedGrants(t)
	withOperatorBusPrincipal(t)
	agent := authMapTestAgent()
	principals := a2aStaticPrincipalNames(agent)
	if len(principals) < 2 {
		t.Fatalf("static principals %v", principals)
	}
	named := principals[1] // a static identity's user
	now := time.Date(2026, 11, 12, 12, 0, 0, 0, time.UTC)
	applied := &a2aDispatchConfig{Profiles: []a2aDispatchProfile{
		{Name: named, Tiers: []string{"read"}},
		{Name: "gone", Tiers: []string{"read"}},
	}}
	got := drainingDispatchProfiles(applied, map[string]bool{}, now, principals...)
	if len(got) != 1 || got[0].Name != "gone" {
		t.Fatalf("draining = %+v, want gone alone", got)
	}
	doc, err := renderA2AAuthMap(agent, []agentv1alpha1.AgentProfile{testAgentProfile(agent.Namespace, "tse")})
	if err != nil {
		t.Fatal(err)
	}
	ids := doc.Identities
	i := slices.IndexFunc(ids, func(id a2aAuthMapIdentity) bool { return id.User == a2aDispatcherUser })
	ids[i].Draining = []string{named}
	if err := validateA2AAuthMapIdentities(ids, principals...); err == nil {
		t.Fatalf("the mirror accepted draining principal %q", named)
	}
}

// With bucketed grants, a profile named for a dispatcher bucket user is
// refused on its own, with a condition, instead of failing the map for every
// principal. With them off, the name is free (#5553, round 1).
func TestAProfileNamedForABucketUserIsRefusedAlone(t *testing.T) {
	for name, want := range map[string]bool{
		"dispatcher-b0": true, "dispatcher-b31": true, "dispatcher-b32": false,
		"dispatcher-b07": false, "dispatcher-b": false, "dispatcher-bx": false, "dispatcher": false,
	} {
		if got := isDispatcherBucketUser(name); got != want {
			t.Errorf("isDispatcherBucketUser(%q) = %v, want %v", name, got, want)
		}
	}
	withOperatorBusPrincipal(t)
	agent := authMapTestAgent()
	profiles := []agentv1alpha1.AgentProfile{testAgentProfile(agent.Namespace, "dispatcher-b5"), testAgentProfile(agent.Namespace, "tse")}
	withDispatcher(t)
	t.Setenv(a2aDispatcherBucketedGrantsEnvVar, "false") // on unless "false" since this PR
	if res := resolveAgentProfileIdentities(agent, profiles)["dispatcher-b5"]; res.refused != nil {
		t.Errorf("with bucketed grants off, dispatcher-b5 was refused: %v", res.refused)
	}
	withBucketedGrants(t)
	resolved := resolveAgentProfileIdentities(agent, profiles)
	if res := resolved["dispatcher-b5"]; res.refused == nil || !strings.Contains(res.refused.Error(), "bucket users") {
		t.Errorf("with bucketed grants on, dispatcher-b5: %+v", res)
	}
	if resolved["tse"].refused != nil {
		t.Errorf("a neighbour was refused: %v", resolved["tse"].refused)
	}
	if _, err := renderA2AAuthMap(agent, profiles); err != nil {
		t.Errorf("the map failed for every principal: %v", err)
	}
}

// Every name the map's mirror refuses as a draining name is also one
// drainingDispatchProfiles doesn't carry: a callout-authenticated map user
// (agent, session, the dispatcher itself), a static principal, the callout's
// user, and a bucket user whatever the switch says. So a profile that held
// one while live leaves without failing the map for its drain window
// (#5553, round 2).
func TestNoNameCarriedIntoDrainingFailsTheMap(t *testing.T) {
	withDispatcher(t)
	withBucketedGrants(t)
	withOperatorBusPrincipal(t)
	agent := authMapTestAgent()
	excluded := a2aDrainingNameExclusions(agent)
	var applied a2aDispatchConfig
	for _, name := range append(append([]string{}, excluded...), "dispatcher-b3", "gone") {
		if isDNS1123LabelToken(name) {
			applied.Profiles = append(applied.Profiles, a2aDispatchProfile{Name: name, Tiers: []string{"read"}})
		}
	}
	draining := drainingDispatchProfiles(&applied, map[string]bool{}, time.Now(), excluded...)
	if len(draining) != 1 || draining[0].Name != "gone" {
		var got []string
		for _, d := range draining {
			got = append(got, d.Name)
		}
		t.Fatalf("draining = %v, want gone alone", got)
	}
	for _, want := range []string{a2aDispatcherUser, "dispatcher-b3"} {
		if a2aCanDrainAs(want, excluded) {
			t.Errorf("%q can drain", want)
		}
	}
	if _, err := renderA2AAuthMapDraining(agent, []agentv1alpha1.AgentProfile{testAgentProfile(agent.Namespace, "tse")}, draining); err != nil {
		t.Errorf("the map failed with the carried draining set: %v", err)
	}
}

// With bucketed grants, #5394's ceiling no longer cuts on the dispatcher's
// identity entry, which doesn't grow: only the dispatch config's budget can
// leave a profile out (#5381).
func TestBucketedGrantsLiftTheIdentityCeiling(t *testing.T) {
	withDispatcher(t)
	agent := a2aTestAgent()
	profiles := fleetScaleProfileFixtures(t, agent)
	var bound []*agentv1alpha1.AgentProfile
	for i := range profiles {
		bound = append(bound, &profiles[i])
	}
	t.Setenv(a2aDispatcherBucketedGrantsEnvVar, "")
	off, err := dispatcherCeilingCut(agent, bound)
	if err != nil {
		t.Fatal(err)
	}
	withBucketedGrants(t)
	on, err := dispatcherCeilingCut(agent, bound)
	if err != nil {
		t.Fatal(err)
	}
	if on.limit == dispatcherCeilingLimitIdentity {
		t.Errorf("with bucketed grants the cut stopped on the identity entry at %d", on.fit)
	}
	if on.fit < off.fit {
		t.Errorf("bucketed grants fit %d profiles, fewer than the %d without", on.fit, off.fit)
	}
	t.Logf("fit %d (%s) without bucketed grants, %d (%s) with", off.fit, off.limit, on.fit, on.limit)
}

// A draining profile whose name happens to be a live profile's map user
// (profile-<p>-<t>) drains without failing the map: the mirror applies the
// same predicate drainingDispatchProfiles does, and such a name is no
// addressee (#5553, round 3).
func TestADrainingNameThatIsAProfileEntryUserDoesNotFailTheMap(t *testing.T) {
	withDispatcher(t)
	withBucketedGrants(t)
	withOperatorBusPrincipal(t)
	agent := authMapTestAgent()
	live := testAgentProfile(agent.Namespace, "tse")
	name := agentProfileMapUser(&live, agentv1alpha1.AgentProfileTierRead)
	applied := &a2aDispatchConfig{Profiles: []a2aDispatchProfile{{Name: name, Tiers: []string{"read"}}}}
	draining := drainingDispatchProfiles(applied, map[string]bool{}, time.Now(), a2aDrainingNameExclusions(agent)...)
	if len(draining) != 1 {
		t.Fatalf("draining = %+v, want %s", draining, name)
	}
	if _, err := renderA2AAuthMapDraining(agent, []agentv1alpha1.AgentProfile{live}, draining); err != nil {
		t.Errorf("the map failed with draining %q beside live tse: %v", name, err)
	}
}
