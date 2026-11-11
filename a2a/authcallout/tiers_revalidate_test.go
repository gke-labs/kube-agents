package authcallout

import (
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"sync"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/capability"
)

// A map whose profile entry names a tier the callout hasn't loaded yet is
// refused when it arrives, and taken once the tier loads: the identity map
// reaches the callout through an informer in milliseconds, the tiers file
// through the kubelet's mount up to a minute later (#3659).
func TestAMapThatArrivesBeforeItsTierIsTakenWhenTheTierLoads(t *testing.T) {
	t.Cleanup(func() { capability.LoadTiers(nil) })
	capability.LoadTiers(nil)
	store, err := NewShardedStore(slog.New(slog.NewTextHandler(io.Discard, nil)), 1)
	if err != nil {
		t.Fatal(err)
	}
	capability.OnTiersChanged(store.Revalidate)
	entry := Identity{ServiceAccount: "system:serviceaccount:x:agentprofile-tse-oncall", User: "profile-tse-oncall",
		Account: AccountApp, Narrowing: NarrowingProfile, Profile: "tse", Tier: "oncall",
		Grants: Grants{Publish: []string{}, Subscribe: []string{}}}
	raw, err := json.Marshal(IdentityMap{Version: "v-oncall", Identities: []Identity{
		{ServiceAccount: "system:serviceaccount:x:gw", User: "gateway", Account: AccountControl,
			Grants: Grants{Publish: []string{"a"}, Subscribe: []string{"b"}}},
		entry,
	}})
	if err != nil {
		t.Fatal(err)
	}
	if err := store.UpdateShard(0, raw); err == nil {
		t.Fatal("a map naming an unloaded tier was taken")
	}
	if v := store.Version(); v == "v-oncall" {
		t.Fatal("the refused map is serving")
	}
	capability.LoadTiers([]capability.TierDefinition{{Name: "oncall", Mode: capability.TierModeInteractive}})
	if v := store.Version(); v != "v-oncall" {
		t.Fatalf("after the tier loaded the store serves %q, want v-oncall", v)
	}
}

// A Revalidate racing an update never puts the older document back: the last
// document delivered is the one serving once both are done.
func TestRevalidateNeverRestoresAnOlderDocument(t *testing.T) {
	store, err := NewShardedStore(slog.New(slog.NewTextHandler(io.Discard, nil)), 1)
	if err != nil {
		t.Fatal(err)
	}
	doc := func(v string) []byte {
		raw, err := json.Marshal(IdentityMap{Version: v, Identities: []Identity{
			{ServiceAccount: "system:serviceaccount:x:gw", User: "gateway", Account: AccountControl,
				Grants: Grants{Publish: []string{"a"}, Subscribe: []string{"b"}}},
		}})
		if err != nil {
			t.Fatal(err)
		}
		return raw
	}
	for round := 0; round < 200; round++ {
		if err := store.UpdateShard(0, doc(fmt.Sprintf("old-%d", round))); err != nil {
			t.Fatal(err)
		}
		want := fmt.Sprintf("new-%d", round)
		var wg sync.WaitGroup
		wg.Add(2)
		go func() { defer wg.Done(); store.Revalidate() }()
		go func() { defer wg.Done(); _ = store.UpdateShard(0, doc(want)) }()
		wg.Wait()
		if v := store.Version(); v != want {
			t.Fatalf("round %d: serving %q after the update to %q", round, v, want)
		}
	}
}
