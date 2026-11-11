package lib

import (
	"encoding/json"
	"fmt"
	"hash/fnv"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

// The subjects one profile tier gives the dispatcher are exactly the ones the
// operator's dispatcherIdentity spelled for it before #5381: the durables'
// INFO, MSG.NEXT and ACK per (tier, kind), the supervisor publish, the stop
// record's direct get, and the stop subscribe. Spelled out here by hand, so a
// change to a lib helper shows up as a diff, not as a silent agreement.
func TestDispatcherGrantsSpellTodaysSubjects(t *testing.T) {
	got := DispatcherGrants([]DispatcherGrantProfile{{Name: "cluster", Tiers: []string{"read", "unattended"}}})
	want := DispatcherSubjects{
		Publish: []string{
			"$JS.API.CONSUMER.INFO.PROFILE_SUBMITS.profile-cluster-read-top",
			"$JS.API.CONSUMER.MSG.NEXT.PROFILE_SUBMITS.profile-cluster-read-top",
			"$JS.ACK.PROFILE_SUBMITS.profile-cluster-read-top.>",
			"$JS.API.CONSUMER.INFO.PROFILE_SUBMITS.profile-cluster-read-child",
			"$JS.API.CONSUMER.MSG.NEXT.PROFILE_SUBMITS.profile-cluster-read-child",
			"$JS.ACK.PROFILE_SUBMITS.profile-cluster-read-child.>",
			"$JS.API.CONSUMER.INFO.PROFILE_SUBMITS.profile-cluster-unattended-top",
			"$JS.API.CONSUMER.MSG.NEXT.PROFILE_SUBMITS.profile-cluster-unattended-top",
			"$JS.ACK.PROFILE_SUBMITS.profile-cluster-unattended-top.>",
			"$JS.API.CONSUMER.INFO.PROFILE_SUBMITS.profile-cluster-unattended-child",
			"$JS.API.CONSUMER.MSG.NEXT.PROFILE_SUBMITS.profile-cluster-unattended-child",
			"$JS.ACK.PROFILE_SUBMITS.profile-cluster-unattended-child.>",
			"a2a.tasks.cluster.*.supervisor",
			"$JS.API.DIRECT.GET.DISPATCH_STOPS.a2a.stop.cluster.*",
		},
		Subscribe: []string{"a2a.stop.cluster.*"},
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("DispatcherGrants =\n%#v\nwant\n%#v", got, want)
	}
}

// A draining profile has no durables left (the operator deleted them), so it
// keeps only what its in-flight tasks need: the supervisor publish and the
// stop grants. The same shape the operator rendered for a draining profile.
func TestADrainingProfileGetsNoDurable(t *testing.T) {
	got := DispatcherGrants([]DispatcherGrantProfile{{Name: "old", Draining: true}})
	want := DispatcherSubjects{
		Publish:   []string{"a2a.tasks.old.*.supervisor", "$JS.API.DIRECT.GET.DISPATCH_STOPS.a2a.stop.old.*"},
		Subscribe: []string{"a2a.stop.old.*"},
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("DispatcherGrants(draining) =\n%#v\nwant\n%#v", got, want)
	}
}

// Every subject is exact-name or ends at one task-id or ack wildcard after
// the profile's own name: none puts a wildcard where a profile name goes.
func TestDispatcherGrantsPutNoWildcardInTheProfilePosition(t *testing.T) {
	got := DispatcherGrants([]DispatcherGrantProfile{{Name: "p", Tiers: []string{"read"}}, {Name: "q", Draining: true}})
	for _, s := range append(append([]string{}, got.Publish...), got.Subscribe...) {
		if !strings.Contains(s, "p") && !strings.Contains(s, "q") {
			t.Errorf("%q names no profile", s)
		}
		for _, bad := range []string{"a2a.tasks.*", "a2a.stop.*", "PROFILE_SUBMITS.*", "PROFILE_SUBMITS.>"} {
			if strings.HasPrefix(s, bad) || strings.Contains(s, "."+bad) {
				t.Errorf("%q has a wildcard in the profile position", s)
			}
		}
	}
}

func TestDispatcherBucketIsFNV32aModK(t *testing.T) {
	for _, name := range []string{"cluster", "tse", "a", strings.Repeat("x", 44)} {
		h := fnv.New32a()
		_, _ = h.Write([]byte(name))
		if got, want := DispatcherBucket(name), int(h.Sum32()%DispatcherBuckets); got != want {
			t.Errorf("DispatcherBucket(%q) = %d, want %d", name, got, want)
		}
	}
	if DispatcherBuckets != 32 {
		t.Fatalf("DispatcherBuckets = %d; changing K moves every profile's bucket and must change both sides together", DispatcherBuckets)
	}
}

func TestTheBucketCredentialRoundTripsAndRefusesEverythingElse(t *testing.T) {
	for b := 0; b < DispatcherBuckets; b++ {
		got, err := ParseDispatcherBucketCredential(DispatcherBucketCredential(b))
		if err != nil || got != b {
			t.Fatalf("round trip of bucket %d = %d, %v", b, got, err)
		}
	}
	for _, bad := range []string{
		"", "7", "7/", "/32", "7/31", "7/64", "32/32", "-1/32", "07/32", "+7/32", "7/032",
		"7/32/1", " 7/32", "7/32 ", "a/32", "7/3a", fmt.Sprintf("%d/32", 1<<40),
	} {
		if b, err := ParseDispatcherBucketCredential(bad); err == nil {
			t.Errorf("ParseDispatcherBucketCredential(%q) = %d, want an error", bad, b)
		}
	}
}

// The profiles of one bucket are the ones that hash there, and every profile
// lands in exactly one bucket.
func TestProfilesInBucketPartitionsTheProfiles(t *testing.T) {
	var all []DispatcherGrantProfile
	for i := 0; i < 500; i++ {
		all = append(all, DispatcherGrantProfile{Name: fmt.Sprintf("p%d", i), Tiers: []string{"read"}})
	}
	seen := map[string]int{}
	for b := 0; b < DispatcherBuckets; b++ {
		for _, p := range ProfilesInBucket(all, b) {
			if DispatcherBucket(p.Name) != b {
				t.Errorf("bucket %d holds %s, which hashes to %d", b, p.Name, DispatcherBucket(p.Name))
			}
			seen[p.Name]++
		}
	}
	for _, p := range all {
		if seen[p.Name] != 1 {
			t.Errorf("%s is in %d buckets", p.Name, seen[p.Name])
		}
	}
}

// The operator's flag-off spelling of the same subjects, at a small N
// (k8s-operator's TestDispatcherGrantsGoldenFixture writes it). The two
// modules can't import each other, so this file holds them equal until the
// flag-off render is deleted (#5381).
func TestDispatcherGrantsMatchTheOperatorsGolden(t *testing.T) {
	raw, err := os.ReadFile(filepath.Join("testdata", "dispatcher-grants-golden.json"))
	if err != nil {
		t.Fatal(err)
	}
	var golden struct {
		Profiles []struct {
			Name     string   `json:"name"`
			Tiers    []string `json:"tiers"`
			Draining bool     `json:"draining"`
		} `json:"profiles"`
		Publish   []string `json:"publish"`
		Subscribe []string `json:"subscribe"`
	}
	if err := json.Unmarshal(raw, &golden); err != nil {
		t.Fatal(err)
	}
	var profiles []DispatcherGrantProfile
	for _, p := range golden.Profiles {
		profiles = append(profiles, DispatcherGrantProfile{Name: p.Name, Tiers: p.Tiers, Draining: p.Draining})
	}
	got := DispatcherGrants(profiles)
	if !reflect.DeepEqual(got.Publish, golden.Publish) || !reflect.DeepEqual(got.Subscribe, golden.Subscribe) {
		t.Fatalf("lib and the operator spell the dispatcher's grants differently:\nlib publish %v\nop  publish %v\nlib subscribe %v\nop  subscribe %v",
			got.Publish, golden.Publish, got.Subscribe, golden.Subscribe)
	}
}
