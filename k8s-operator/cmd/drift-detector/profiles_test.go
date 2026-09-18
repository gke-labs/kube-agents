// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package main

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/base64"
	"encoding/pem"
	"errors"
	"fmt"
	"log"
	"math/big"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"golang.org/x/oauth2"
	container "google.golang.org/api/container/v1"

	"github.com/gke-labs/kube-agents/k8s-operator/internal/clusterprofiles"
)

var (
	testCA     []byte
	testCAErr  error
	testCAOnce sync.Once
)

// testCAPEM is a self-signed CA in PEM form, generated once per test binary.
//
// It has to be a real certificate even though nothing here opens a TLS session.
// ClientConfigForIdentity puts the decoded bytes in rest.Config.CAData and
// dynamic.NewForConfig builds the CertPool eagerly, so arbitrary base64 fails at
// client construction with "unable to parse bytes as PEM block" and every
// profile in the test reports as skipped. That failure is real enough to be
// worth exercising on purpose -- see TestDiscoverProfileClustersSkipsAProfileWhoseClientWillNotBuild
// -- so it needs the working case to be distinguishable from it.
func testCAPEM(t *testing.T) []byte {
	t.Helper()
	testCAOnce.Do(func() {
		key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
		if err != nil {
			testCAErr = err
			return
		}
		template := &x509.Certificate{
			SerialNumber:          big.NewInt(1),
			Subject:               pkix.Name{CommonName: "drift-detector test CA"},
			NotBefore:             time.Now().Add(-time.Hour),
			NotAfter:              time.Now().Add(time.Hour),
			IsCA:                  true,
			BasicConstraintsValid: true,
		}
		der, err := x509.CreateCertificate(rand.Reader, template, template, &key.PublicKey, key)
		if err != nil {
			testCAErr = err
			return
		}
		testCA = pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})
	})
	if testCAErr != nil {
		t.Fatalf("generating the test CA: %v", testCAErr)
	}
	return testCA
}

// stubGKE makes the profile scan answerable without a Google credential or a
// network call. It returns the failure map: putting an error under a cluster's
// Identity.String() makes Describe fail for that one cluster.
//
// The same stub k8s-event-watcher's tests install, against the same seam --
// which is the payoff of internal/clusterprofiles being a package rather than
// two copies of a directory walk.
func stubGKE(t *testing.T) map[string]error {
	t.Helper()
	failures := map[string]error{}
	saved := profilesDiscovery
	t.Cleanup(func() { profilesDiscovery = saved })
	profilesDiscovery.Describe = func(_ context.Context, id clusterprofiles.Identity) (*container.Cluster, error) {
		if err, bad := failures[id.String()]; bad {
			return nil, err
		}
		return &container.Cluster{
			Endpoint:   id.String() + ".example.invalid",
			MasterAuth: &container.MasterAuth{ClusterCaCertificate: base64.StdEncoding.EncodeToString(testCAPEM(t))},
		}, nil
	}
	profilesDiscovery.TokenSource = func(context.Context) (oauth2.TokenSource, error) {
		return oauth2.StaticTokenSource(&oauth2.Token{AccessToken: "test-token"}), nil
	}
	return failures
}

// writeClusterProfile creates a Cluster Agent profile directory the way
// cluster_agent_profile.py does: a config.yaml carrying a cluster_identity
// block, and nothing else.
func writeClusterProfile(t *testing.T, base, profile, project, cluster, location string) {
	t.Helper()
	home := filepath.Join(base, profile)
	if err := os.MkdirAll(home, 0o700); err != nil {
		t.Fatalf("mkdir %s: %v", home, err)
	}
	cfg := "model:\n  provider: custom\ncluster_identity:\n" +
		"  project: " + project + "\n" +
		"  cluster: " + cluster + "\n" +
		"  location: " + location + "\n"
	if err := os.WriteFile(filepath.Join(home, "config.yaml"), []byte(cfg), 0o600); err != nil {
		t.Fatalf("write config.yaml: %v", err)
	}
}

// captureLog collects what the code under test logs, so a test can assert on
// the operator-facing half of a skip rather than only on the count.
func captureLog(t *testing.T) *strings.Builder {
	t.Helper()
	var buf strings.Builder
	saved := log.Writer()
	savedFlags := log.Flags()
	log.SetOutput(&buf)
	log.SetFlags(0)
	t.Cleanup(func() {
		log.SetOutput(saved)
		log.SetFlags(savedFlags)
	})
	return &buf
}

// The transposition this conversion exists to prevent. clusterprofiles.Identity
// orders its fields Project, Cluster, Location and clusterIdentity orders them
// Project, Location, Cluster, so a positional composite literal compiles, passes
// go vet, and reads every record's location against a cluster name -- which
// matches nothing and reports the whole fleet unreachable.
func TestIdentityFromProfileDoesNotTransposeLocationAndCluster(t *testing.T) {
	got := identityFromProfile(clusterprofiles.Identity{
		Project:  "example-project",
		Cluster:  "prod-a",
		Location: "us-central1",
	})
	want := clusterIdentity{Project: "example-project", Location: "us-central1", Cluster: "prod-a"}
	if got != want {
		t.Errorf("identityFromProfile = %+v, want %+v", got, want)
	}
}

// --profiles-dir unset must not be the fatal missing-directory case: Discover
// calls os.ReadDir, "" is ENOENT, and ENOENT is the one condition the package
// treats as fatal. Without the guard a detector that simply did not ask for the
// fan-in would refuse to start.
func TestDiscoverProfileClustersWithNoDirectoryConfigured(t *testing.T) {
	clusters, skipped, err := discoverProfileClusters(context.Background(), "", "example-project")
	if err != nil {
		t.Fatalf("discoverProfileClusters(\"\") returned error: %v -- an unset --profiles-dir is not a missing directory", err)
	}
	if len(clusters) != 0 || skipped != 0 {
		t.Errorf("clusters = %d skipped = %d, want 0 and 0", len(clusters), skipped)
	}
}

// A --profiles-dir that was given and is not there is fatal, matching the
// watcher: discovery runs once at startup, so a directory the Platform Agent
// has not created yet is fixed by the next start and not by carrying on.
func TestDiscoverProfileClustersFatalOnAMissingDirectory(t *testing.T) {
	stubGKE(t)
	missing := filepath.Join(t.TempDir(), "not-created-yet")

	_, _, err := discoverProfileClusters(context.Background(), missing, "example-project")
	if err == nil {
		t.Fatal("discoverProfileClusters returned no error for a missing --profiles-dir")
	}
	if !errors.Is(err, os.ErrNotExist) {
		t.Errorf("error = %v, want one wrapping os.ErrNotExist", err)
	}
}

func TestDiscoverProfileClustersBuildsOneReaderPerProfile(t *testing.T) {
	stubGKE(t)
	dir := t.TempDir()
	writeClusterProfile(t, dir, "cluster-example-project-prod-a-us-central1", "example-project", "prod-a", "us-central1")
	writeClusterProfile(t, dir, "cluster-example-project-prod-b-europe-west1", "example-project", "prod-b", "europe-west1")

	clusters, skipped, err := discoverProfileClusters(context.Background(), dir, "example-project")
	if err != nil {
		t.Fatalf("discoverProfileClusters returned error: %v", err)
	}
	if skipped != 0 {
		t.Errorf("skipped = %d, want 0", skipped)
	}
	if len(clusters) != 2 {
		t.Fatalf("discovered %d clusters, want 2", len(clusters))
	}

	got := map[clusterIdentity]string{}
	for _, c := range clusters {
		if c.Getter == nil {
			t.Errorf("profile %s has no getter", c.Profile)
		}
		got[c.Identity] = c.Profile
	}
	for _, want := range []clusterIdentity{
		{Project: "example-project", Location: "us-central1", Cluster: "prod-a"},
		{Project: "example-project", Location: "europe-west1", Cluster: "prod-b"},
	} {
		if _, ok := got[want]; !ok {
			t.Errorf("discovered %v, missing %v", got, want)
		}
	}
}

// The silent failure joinClientQPS is written at length about, on the path that
// introduced it: internal/clusterprofiles leaves QPS at zero, which client-go
// reads as DefaultQPS=5 rather than as unlimited, so a profile cluster missed by
// applyJoinThrottle would throttle at the eleventh record of a batch and surface
// as a bare "context deadline exceeded".
func TestDiscoverProfileClustersLiftsTheClientSideThrottle(t *testing.T) {
	stubGKE(t)
	dir := t.TempDir()
	writeClusterProfile(t, dir, "cluster-example-project-prod-a-us-central1", "example-project", "prod-a", "us-central1")

	clusters, _, err := discoverProfileClusters(context.Background(), dir, "example-project")
	if err != nil {
		t.Fatalf("discoverProfileClusters returned error: %v", err)
	}
	if len(clusters) != 1 {
		t.Fatalf("discovered %d clusters, want 1", len(clusters))
	}
	cfg := clusters[0].Config
	if cfg == nil {
		t.Fatal("profileCluster.Config is nil, so the throttle cannot be checked at all")
	}
	if cfg.QPS != joinClientQPS {
		t.Errorf("QPS = %v, want %v -- zero here is client-go's DefaultQPS=5, not unlimited", cfg.QPS, joinClientQPS)
	}
	if cfg.Burst != joinClientBurst {
		t.Errorf("Burst = %d, want %d", cfg.Burst, joinClientBurst)
	}
}

// A project-level sink carries records from one project, so a profile for a
// cluster elsewhere -- which the Platform Agent legitimately writes, since a
// fleet can span projects -- can never match a record. Registering it would mint
// a token for a cluster that is unreachable by definition, and hide the case
// worth seeing: --project pointed at the wrong project.
func TestDiscoverProfileClustersDropsClustersOutsideTheProject(t *testing.T) {
	stubGKE(t)
	logs := captureLog(t)
	dir := t.TempDir()
	writeClusterProfile(t, dir, "cluster-example-project-prod-a-us-central1", "example-project", "prod-a", "us-central1")
	writeClusterProfile(t, dir, "cluster-other-project-prod-b-us-central1", "other-project", "prod-b", "us-central1")

	clusters, skipped, err := discoverProfileClusters(context.Background(), dir, "example-project")
	if err != nil {
		t.Fatalf("discoverProfileClusters returned error: %v", err)
	}
	if len(clusters) != 1 {
		t.Fatalf("discovered %d clusters, want 1 -- the other project's profile was registered", len(clusters))
	}
	if clusters[0].Identity.Project != "example-project" {
		t.Errorf("registered %v, want the --project cluster", clusters[0].Identity)
	}
	if skipped != 1 {
		t.Errorf("skipped = %d, want 1 -- a dropped profile has to be counted, or a fan-in that reached one of two looks like a fleet of one", skipped)
	}
	for _, want := range []string{"cluster-other-project-prod-b-us-central1", "outside --project"} {
		if !strings.Contains(logs.String(), want) {
			t.Errorf("log does not mention %q:\n%s", want, logs.String())
		}
	}
}

// One profile whose client will not build must not cost the rest of the fleet.
// The CA is the lever: ClientConfigForIdentity only base64-decodes it, so valid
// base64 that is not a PEM block passes discovery and fails at
// dynamic.NewForConfig -- the one reachable way to get a described cluster whose
// client construction fails.
func TestDiscoverProfileClustersSkipsAProfileWhoseClientWillNotBuild(t *testing.T) {
	stubGKE(t)
	logs := captureLog(t)
	broken := clusterprofiles.Identity{Project: "example-project", Cluster: "prod-b", Location: "europe-west1"}
	saved := profilesDiscovery.Describe
	profilesDiscovery.Describe = func(ctx context.Context, id clusterprofiles.Identity) (*container.Cluster, error) {
		if id == broken {
			return &container.Cluster{
				Endpoint:   id.String() + ".example.invalid",
				MasterAuth: &container.MasterAuth{ClusterCaCertificate: base64.StdEncoding.EncodeToString([]byte("not-a-pem-block"))},
			}, nil
		}
		return saved(ctx, id)
	}

	dir := t.TempDir()
	writeClusterProfile(t, dir, "good", "example-project", "prod-a", "us-central1")
	writeClusterProfile(t, dir, "bad", "example-project", "prod-b", "europe-west1")

	clusters, skipped, err := discoverProfileClusters(context.Background(), dir, "example-project")
	if err != nil {
		t.Fatalf("discoverProfileClusters returned error: %v -- one bad profile is a skip, not a fatal", err)
	}
	if len(clusters) != 1 {
		t.Fatalf("discovered %d clusters, want 1", len(clusters))
	}
	if clusters[0].Profile != "good" {
		t.Errorf("kept profile %q, want \"good\"", clusters[0].Profile)
	}
	if skipped != 1 {
		t.Errorf("skipped = %d, want 1", skipped)
	}
	// Named, and named as not joined: the operator's action is to fix that
	// profile, and a count alone does not say which one to look at.
	for _, want := range []string{"skipping profile bad", "will NOT be joined"} {
		if !strings.Contains(logs.String(), want) {
			t.Errorf("log does not mention %q:\n%s", want, logs.String())
		}
	}
}

// An unreadable directory degrades where a missing one is fatal, and the skip
// is reported against clusterprofiles.NoProfile rather than against a profile
// name, because it is the directory that failed. The branch exists so that a
// bad mount does not take the process down; this is the only thing that
// exercises it.
func TestDiscoverProfileClustersDegradesOnAnUnreadableDirectory(t *testing.T) {
	if os.Geteuid() == 0 {
		t.Skip("running as root, which can read a 0o000 directory")
	}
	stubGKE(t)
	logs := captureLog(t)
	dir := filepath.Join(t.TempDir(), "profiles")
	if err := os.Mkdir(dir, 0o000); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	t.Cleanup(func() { _ = os.Chmod(dir, 0o700) })

	clusters, skipped, err := discoverProfileClusters(context.Background(), dir, "example-project")
	if err != nil {
		t.Fatalf("discoverProfileClusters returned error: %v -- an unreadable dir degrades, it is not fatal", err)
	}
	if len(clusters) != 0 {
		t.Errorf("discovered %d clusters from an unreadable directory", len(clusters))
	}
	// Not 1: an unreadable directory is an unknown number of clusters, not one
	// straggler, so it does not move the per-profile skip count that an operator
	// would otherwise read as "everything but one profile came up".
	if skipped != 0 {
		t.Errorf("skipped = %d, want 0", skipped)
	}
	// No profile name in the line, because no profile was read -- and nothing
	// of the form "skipping profile -", which is what a NoProfile skip rendered
	// as a profile name would produce.
	if strings.Contains(logs.String(), "skipping profile") {
		t.Errorf("a directory-level failure was reported as a profile skip:\n%s", logs.String())
	}
	if !strings.Contains(logs.String(), "cannot read profiles dir") {
		t.Errorf("log does not say the directory could not be read:\n%s", logs.String())
	}
}

func TestBuildClusterSet(t *testing.T) {
	direct := &stubGetter{}
	directID := clusterIdentity{Project: "example-project", Location: "us-central1", Cluster: "prod-a"}
	other := clusterIdentity{Project: "example-project", Location: "europe-west1", Cluster: "prod-b"}

	t.Run("the two sources are additive", func(t *testing.T) {
		set, absorbed := buildClusterSet(direct, directID, []profileCluster{
			{Identity: other, Profile: "prod-b", Getter: &stubGetter{}},
		})
		if len(set) != 2 {
			t.Fatalf("set has %d entries, want 2", len(set))
		}
		if len(absorbed) != 0 {
			t.Errorf("absorbed = %v, want none", absorbed)
		}
	})

	t.Run("profiles alone are a valid set", func(t *testing.T) {
		set, absorbed := buildClusterSet(nil, directID, []profileCluster{
			{Identity: other, Profile: "prod-b", Getter: &stubGetter{}},
		})
		// One entry, not two: a nil direct getter is not registered, or the
		// lookup would find it and panic on Get.
		if len(set) != 1 {
			t.Fatalf("set has %d entries, want 1", len(set))
		}
		if _, ok := set[directID]; ok {
			t.Error("a nil direct getter was registered under the flags' identity")
		}
		if len(absorbed) != 0 {
			t.Errorf("absorbed = %v, want none", absorbed)
		}
	})

	t.Run("neither source is a valid empty set", func(t *testing.T) {
		set, absorbed := buildClusterSet(nil, directID, nil)
		if len(set) != 0 {
			t.Errorf("set has %d entries, want 0", len(set))
		}
		if len(absorbed) != 0 {
			t.Errorf("absorbed = %v, want none", absorbed)
		}
	})

	// The overlap that happens on every install: reconcile gives the management
	// cluster a profile like any other cluster, so the pod's own cluster arrives
	// from both sources. The direct credential wins -- see newObjectGetter for
	// why that one and not the profile's.
	t.Run("the direct cluster wins a duplicate", func(t *testing.T) {
		profileGetter := &stubGetter{}
		set, absorbed := buildClusterSet(direct, directID, []profileCluster{
			{Identity: directID, Profile: "cluster-example-project-prod-a-us-central1", Getter: profileGetter},
			{Identity: other, Profile: "prod-b", Getter: &stubGetter{}},
		})
		if len(set) != 2 {
			t.Fatalf("set has %d entries, want 2 -- the duplicate added a third", len(set))
		}
		if set[directID] != objectGetter(direct) {
			t.Error("the profile's getter displaced the direct one")
		}
		if len(absorbed) != 1 || absorbed[0] != "cluster-example-project-prod-a-us-central1" {
			t.Errorf("absorbed = %v, want the duplicate profile named", absorbed)
		}
	})
}

// Two clusters, two getters: the fan-in is only real if a record is served by
// its own cluster's client and not by whichever one the map happened to hold.
// A single-cluster join passes every other test in this file.
func TestJoinRoutesEachRecordToItsOwnCluster(t *testing.T) {
	a := clusterIdentity{Project: "example-project", Location: "us-central1", Cluster: "prod-a"}
	b := clusterIdentity{Project: "example-project", Location: "europe-west1", Cluster: "prod-b"}
	getterA := &stubGetter{obj: managedFieldsObject(entry("kubectl-edit", "Update", "", `{"f:spec":{}}`, nil))}
	getterB := &stubGetter{obj: managedFieldsObject(entry("kubectl-edit", "Update", "", `{"f:spec":{}}`, nil))}

	j := newJoiner(map[clusterIdentity]objectGetter{a: getterA, b: getterB}, nil,
		func(context.Context, DriftEvent) {})

	recordA := joinRecord()
	recordB := joinRecord()
	recordB.Location = "europe-west1"
	recordB.Cluster = "prod-b"
	recordB.Resource.Name = "api-b"

	j.Handle(context.Background(), recordA)
	j.Handle(context.Background(), recordB)

	if len(getterA.refs) != 1 || getterA.refs[0].Name != "api" {
		t.Errorf("cluster prod-a saw %v, want one lookup for \"api\"", getterA.refs)
	}
	if len(getterB.refs) != 1 || getterB.refs[0].Name != "api-b" {
		t.Errorf("cluster prod-b saw %v, want one lookup for \"api-b\"", getterB.refs)
	}
	if got := j.Counts().Enriched; got != 2 {
		t.Errorf("enriched = %d, want 2", got)
	}
}

// complete() is belt and braces against a partial identity that is also a key.
// Nothing builds such a set today -- validation requires both cluster flags with
// the direct credentials, and a profile carries all three parts -- so this is
// the only thing holding the guard in place, and without it a record carrying
// only a project would be enriched from whatever cluster shared that key.
func TestJoinRefusesAPartialIdentityEvenWhenItIsAKey(t *testing.T) {
	partial := clusterIdentity{Project: "example-project"}
	getter := &stubGetter{obj: managedFieldsObject()}
	j := newJoiner(map[clusterIdentity]objectGetter{partial: getter}, nil, func(context.Context, DriftEvent) {})

	record := joinRecord()
	record.Location = ""
	record.Cluster = ""

	j.Handle(context.Background(), record)

	if len(getter.refs) != 0 {
		t.Errorf("the getter was called %d time(s) for a partially identified record", len(getter.refs))
	}
	if got := j.Counts().Unreachable; got != 1 {
		t.Errorf("unreachable = %d, want 1", got)
	}
	// The count moves but the list stays empty: "example-project//" is not a cluster
	// anyone could go and onboard, so naming it would only add noise to the report.
	if got := j.UnreachableClusters(); len(got) != 0 {
		t.Errorf("UnreachableClusters() = %v, want no entries for a partial identity", got)
	}
}

func TestUnreachableClustersNamesThemMostFrequentFirst(t *testing.T) {
	j := newJoiner(nil, nil, func(context.Context, DriftEvent) {})

	record := joinRecord()
	j.Handle(context.Background(), record)
	record.Cluster = "prod-b"
	j.Handle(context.Background(), record)
	j.Handle(context.Background(), record)

	got := j.UnreachableClusters()
	want := []string{`"example-project/us-central1/prod-b"=2`, `"example-project/us-central1/prod-a"=1`}
	if len(got) != len(want) {
		t.Fatalf("UnreachableClusters() = %v, want %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Errorf("UnreachableClusters()[%d] = %s, want %s", i, got[i], want[i])
		}
	}
}

// An outcome that is not unreachable must not name a cluster: a lookup that
// failed on RBAC is a cluster the join reached, and listing it under
// "no credentials and no Cluster Agent profile" would send an operator to
// onboard a cluster that is already onboarded.
func TestUnreachableClustersExcludesTheClustersTheJoinReached(t *testing.T) {
	j := newJoiner(joinSet(&stubGetter{err: errors.New("forbidden")}), nil, func(context.Context, DriftEvent) {})
	j.Handle(context.Background(), joinRecord())

	if got := j.Counts().Failed; got != 1 {
		t.Fatalf("failed = %d, want 1", got)
	}
	if got := j.UnreachableClusters(); len(got) != 0 {
		t.Errorf("UnreachableClusters() = %v, want empty -- a failed lookup reached its cluster", got)
	}
}

// The key comes from the record, so the number of distinct values is set by what
// arrives rather than by anything this process controls. Same cap and same
// behaviour as classify.go's principal list: a cluster already counted keeps
// counting past the cap, and only a new name folds into the overflow bucket.
func TestUnreachableClustersCapsTheNameSet(t *testing.T) {
	j := newJoiner(nil, nil, func(context.Context, DriftEvent) {})

	record := joinRecord()
	for i := 0; i < maxUnreachableClusters; i++ {
		record.Cluster = fmt.Sprintf("cluster-%03d", i)
		j.Handle(context.Background(), record)
	}
	// The first one again, to show a known name still counts past the cap.
	record.Cluster = "cluster-000"
	j.Handle(context.Background(), record)

	const overflowSightings = 3
	for i := 0; i < overflowSightings; i++ {
		record.Cluster = fmt.Sprintf("past-the-cap-%d", i)
		j.Handle(context.Background(), record)
	}

	got := j.UnreachableClusters()
	if len(got) != maxUnreachableClusters+1 {
		t.Fatalf("UnreachableClusters() has %d entries, want %d (the cap plus one overflow bucket)",
			len(got), maxUnreachableClusters+1)
	}
	joined := strings.Join(got, unreachableListSeparator)
	if !strings.Contains(joined, `"example-project/us-central1/cluster-000"=2`) {
		t.Errorf("a named cluster stopped counting past the cap:\n%s", joined)
	}
	if !strings.Contains(joined, fmt.Sprintf("%q=%d", unreachableOverflowLabel, overflowSightings)) {
		t.Errorf("the overflow bucket does not carry every sighting past the cap:\n%s", joined)
	}
	if strings.Contains(joined, "past-the-cap-") {
		t.Errorf("a new name past the cap was stored:\n%s", joined)
	}
}
