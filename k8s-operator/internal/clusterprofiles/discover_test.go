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

package clusterprofiles

import (
	"context"
	"encoding/base64"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"golang.org/x/oauth2"
	container "google.golang.org/api/container/v1"
)

// stub makes discovery answerable without a Google credential or a network
// call: every cluster is described as an ordinary public one and the token
// source hands back a fixed string. Skips are recorded rather than logged, so a
// test can assert which profiles were dropped and why. Put an error in failures
// under "<project>/<location>/<cluster>" to make that one lookup fail.
//
// The CA is arbitrary base64 because nothing here parses it:
// ClientConfigForIdentity only decodes it into CAData. A caller that builds a
// real client does parse it, which is why k8s-event-watcher's own stub hands
// back a generated certificate instead.
type stub struct {
	failures map[string]error
	skips    map[string]error
	tokenErr error
}

func newStub() *stub {
	return &stub{failures: map[string]error{}, skips: map[string]error{}}
}

func (s *stub) discoverer() Discoverer {
	return Discoverer{
		Describe: func(_ context.Context, id Identity) (*container.Cluster, error) {
			if err, bad := s.failures[id.String()]; bad {
				return nil, err
			}
			return &container.Cluster{
				Endpoint:   id.String() + ".example.invalid",
				MasterAuth: &container.MasterAuth{ClusterCaCertificate: base64.StdEncoding.EncodeToString([]byte("ca-bytes"))},
			}, nil
		},
		TokenSource: func(context.Context) (oauth2.TokenSource, error) {
			if s.tokenErr != nil {
				return nil, s.tokenErr
			}
			return oauth2.StaticTokenSource(&oauth2.Token{AccessToken: "test-token"}), nil
		},
		OnSkip: func(profile string, err error) { s.skips[profile] = err },
	}
}

// writeClusterProfile creates a Cluster Agent profile directory the way
// cluster_agent_profile.py does: a config.yaml carrying a cluster_identity
// block. No kubeconfig.yaml — since the shell moved into its own pod, the one
// `gcloud container clusters get-credentials` writes lands on the sandbox's
// volume and never appears here.
func writeClusterProfile(t *testing.T, base, profile, project, cluster, location string) {
	t.Helper()
	writeProfile(t, base, profile, "model:\n  provider: custom\ncluster_identity:\n"+
		"  project: "+project+"\n"+
		"  cluster: "+cluster+"\n"+
		"  location: "+location+"\n")
}

// writeNonClusterProfile creates a profile with no cluster_identity — what
// "default" and "platform" look like on disk.
func writeNonClusterProfile(t *testing.T, base, profile string) {
	t.Helper()
	writeProfile(t, base, profile, "model:\n  provider: custom\n")
}

func writeProfile(t *testing.T, base, profile, config string) {
	t.Helper()
	home := filepath.Join(base, profile)
	if err := os.MkdirAll(home, 0o700); err != nil {
		t.Fatalf("mkdir %s: %v", home, err)
	}
	if err := os.WriteFile(filepath.Join(home, profileConfigFile), []byte(config), 0o600); err != nil {
		t.Fatalf("write %s: %v", profileConfigFile, err)
	}
}

func profiles(clusters []Cluster) []string {
	names := make([]string, 0, len(clusters))
	for _, c := range clusters {
		names = append(names, c.Profile+"="+c.Identity.String())
	}
	return names
}

func TestDiscoverReadsIdentityNotDirName(t *testing.T) {
	s := newStub()
	dir := t.TempDir()
	// Profile directory names are sanitized and hash-truncated by the Python
	// side, so the identity must come from config.yaml, not the dir name.
	writeClusterProfile(t, dir, "cluster-projA-prod-us-central1", "projA", "prod", "us-central1")
	writeClusterProfile(t, dir, "cluster-projB-staging-europe-west1", "projB", "staging", "europe-west1")

	clusters, err := s.discoverer().Discover(context.Background(), dir)
	if err != nil {
		t.Fatalf("Discover: %v", err)
	}
	if got, want := len(clusters), 2; got != want {
		t.Fatalf("got %d clusters (%v), want %d", got, profiles(clusters), want)
	}
	byName := make(map[string]Cluster, len(clusters))
	for _, c := range clusters {
		byName[c.Identity.Cluster] = c
	}
	prod, ok := byName["prod"]
	if !ok {
		t.Fatalf("missing cluster %q; got %v", "prod", profiles(clusters))
	}
	if prod.Identity.Project != "projA" || prod.Identity.Location != "us-central1" {
		t.Errorf("prod identity = %s; want projA/us-central1/prod", prod.Identity)
	}
	if prod.Profile != "cluster-projA-prod-us-central1" {
		t.Errorf("prod profile = %q; want the directory name", prod.Profile)
	}
	if prod.Config == nil || prod.Config.Host == "" {
		t.Error("prod has no addressable config")
	}
	if _, ok := byName["staging"]; !ok {
		t.Errorf("missing cluster %q; got %v", "staging", profiles(clusters))
	}
}

func TestDiscoverAttachesTheCredentialToEveryCluster(t *testing.T) {
	// The address and the credential come from two different places, and only
	// the address has a value a test can read back. A config whose WrapTransport
	// is nil was never handed to UseGoogleTokenSource, which is a client that
	// would reach the control plane and be refused by it.
	s := newStub()
	dir := t.TempDir()
	writeClusterProfile(t, dir, "cluster-one", "p", "one", "us-central1")
	writeClusterProfile(t, dir, "cluster-two", "p", "two", "us-central1")

	clusters, err := s.discoverer().Discover(context.Background(), dir)
	if err != nil {
		t.Fatalf("Discover: %v", err)
	}
	if len(clusters) != 2 {
		t.Fatalf("got %d clusters (%v), want 2", len(clusters), profiles(clusters))
	}
	for _, c := range clusters {
		if c.Config.WrapTransport == nil {
			t.Errorf("%s carries no credential", c.Identity)
		}
	}
}

func TestDiscoverSkipsNonClusterProfiles(t *testing.T) {
	s := newStub()
	dir := t.TempDir()
	writeClusterProfile(t, dir, "cluster-p-good-us-central1", "p", "good", "us-central1")
	// "default" and "platform" exist but carry no cluster_identity.
	writeNonClusterProfile(t, dir, "default")
	writeNonClusterProfile(t, dir, "platform")
	// A half-written identity names no cluster the GKE API could be asked
	// about, so it is not a cluster profile either.
	writeProfile(t, dir, "cluster-p-nolocation", "cluster_identity:\n  project: p\n  cluster: nolocation\n")
	// Loose files and dotfiles at the top level are not profiles.
	if err := os.WriteFile(filepath.Join(dir, "kanban.db"), []byte("junk"), 0o600); err != nil {
		t.Fatalf("write loose file: %v", err)
	}
	if err := os.MkdirAll(filepath.Join(dir, ".cache"), 0o700); err != nil {
		t.Fatalf("mkdir dotdir: %v", err)
	}

	clusters, err := s.discoverer().Discover(context.Background(), dir)
	if err != nil {
		t.Fatalf("Discover: %v", err)
	}
	if got, want := len(clusters), 1; got != want {
		t.Fatalf("got %d clusters (%v), want %d", got, profiles(clusters), want)
	}
	if clusters[0].Identity.Cluster != "good" {
		t.Errorf("got cluster %q; want %q", clusters[0].Identity.Cluster, "good")
	}
	// Not a cluster profile is not a failure, so none of these is reported as a
	// cluster that will go unreached.
	if len(s.skips) != 0 {
		t.Errorf("skips = %v; want none", s.skips)
	}
}

func TestDiscoverNoProfilesIsNotAnError(t *testing.T) {
	// A single-cluster install has no Cluster Agent profiles at all —
	// reconcile only creates them for clusters other than the management one —
	// so an empty result is a steady state, not a misconfiguration. Erroring
	// here would crashloop the caller on every single-cluster install.
	s := newStub()
	dir := t.TempDir()
	writeNonClusterProfile(t, dir, "platform")

	clusters, err := s.discoverer().Discover(context.Background(), dir)
	if err != nil {
		t.Fatalf("Discover: %v", err)
	}
	if len(clusters) != 0 {
		t.Errorf("expected 0 clusters, got %d (%v)", len(clusters), profiles(clusters))
	}
}

func TestDiscoverSameNameDifferentLocation(t *testing.T) {
	s := newStub()
	dir := t.TempDir()
	// A GKE cluster name is unique only within a project and location, so this
	// is two real clusters, not a duplicate — and an ordinary fleet layout.
	// Keying identity on the bare name would keep whichever one ReadDir
	// returned first and silently drop the other.
	writeClusterProfile(t, dir, "cluster-p-prod-us-central1", "p", "prod", "us-central1")
	writeClusterProfile(t, dir, "cluster-p-prod-europe-west1", "p", "prod", "europe-west1")

	clusters, err := s.discoverer().Discover(context.Background(), dir)
	if err != nil {
		t.Fatalf("Discover: %v", err)
	}
	if got, want := len(clusters), 2; got != want {
		t.Fatalf("got %d clusters, want %d — same name in two locations must not collide", got, want)
	}
	locations := map[string]bool{}
	for _, c := range clusters {
		if c.Identity.Cluster != "prod" {
			t.Errorf("expected both clusters named %q, got %q", "prod", c.Identity.Cluster)
		}
		locations[c.Identity.Location] = true
	}
	for _, want := range []string{"us-central1", "europe-west1"} {
		if !locations[want] {
			t.Errorf("missing cluster in %s; got locations %v", want, locations)
		}
	}
	// Neither was treated as a duplicate.
	if len(s.skips) != 0 {
		t.Errorf("skips = %v; both are distinct clusters", s.skips)
	}
}

func TestDiscoverDuplicateClusterIsSkipped(t *testing.T) {
	s := newStub()
	dir := t.TempDir()
	// Two profiles claiming the same cluster would give it two clients and, in
	// the event watcher, two independent dedup caches. Take the first, report
	// the second.
	writeClusterProfile(t, dir, "profile-one", "projA", "prod", "us-central1")
	writeClusterProfile(t, dir, "profile-two", "projA", "prod", "us-central1")

	clusters, err := s.discoverer().Discover(context.Background(), dir)
	if err != nil {
		t.Fatalf("Discover: %v", err)
	}
	if got, want := len(clusters), 1; got != want {
		t.Fatalf("got %d clusters (%v), want %d", got, profiles(clusters), want)
	}
	if clusters[0].Profile != "profile-one" {
		t.Errorf("expected the first profile to win, got %q", clusters[0].Profile)
	}
	// The message has to name both the profile that won and the cluster they
	// are fighting over: without the winner an operator cannot tell which of
	// the two is being watched, and without the cluster they cannot tell which
	// cluster the losing profile was pointed at.
	err = s.skips["profile-two"]
	if err == nil {
		t.Fatalf("expected the duplicate to be reported, skips = %v", s.skips)
	}
	for _, want := range []string{"profile-one", "projA/us-central1/prod"} {
		if !strings.Contains(err.Error(), want) {
			t.Errorf("skip reason %q does not mention %q", err, want)
		}
	}
}

func TestDiscoverUndescribableClusterIsSkipped(t *testing.T) {
	// A profile can name a cluster the GKE API will not answer for: deleted
	// between scaffolding and this start, or outside what this pod's identity
	// may read. Guessing an address from the name would produce a client
	// talking to a control plane nobody confirmed, so drop it and report it —
	// the rest of the fleet is unaffected.
	s := newStub()
	dir := t.TempDir()
	s.failures["p/us-central1/ghost"] = errors.New("clusters.get: 404")
	writeClusterProfile(t, dir, "cluster-p-ghost-us-central1", "p", "ghost", "us-central1")
	writeClusterProfile(t, dir, "cluster-p-real-us-central1", "p", "real", "us-central1")

	clusters, err := s.discoverer().Discover(context.Background(), dir)
	if err != nil {
		t.Fatalf("Discover: %v", err)
	}
	if got, want := len(clusters), 1; got != want {
		t.Fatalf("got %d clusters (%v), want only the real one", got, profiles(clusters))
	}
	if clusters[0].Identity.Cluster != "real" {
		t.Errorf("got cluster %q, want %q", clusters[0].Identity.Cluster, "real")
	}
	if s.skips["cluster-p-ghost-us-central1"] == nil {
		t.Errorf("expected the undescribable cluster to be reported, skips = %v", s.skips)
	}
}

func TestDiscoverWithoutCredentialsSkipsEveryCluster(t *testing.T) {
	// One token source is shared across the fleet, so failing to mint it is not
	// one cluster's problem. Each profile is still reported separately: the
	// caller counts per profile, and a fleet that silently halved would look
	// the same as one that was always this size.
	s := newStub()
	s.tokenErr = errors.New("no application default credentials")
	dir := t.TempDir()
	writeClusterProfile(t, dir, "cluster-one", "p", "one", "us-central1")
	writeClusterProfile(t, dir, "cluster-two", "p", "two", "us-central1")

	clusters, err := s.discoverer().Discover(context.Background(), dir)
	if err != nil {
		t.Fatalf("expected missing credentials to degrade, not fail: %v", err)
	}
	if len(clusters) != 0 {
		t.Errorf("got %d clusters (%v), want none", len(clusters), profiles(clusters))
	}
	if len(s.skips) != 2 {
		t.Errorf("skips = %v; want one per profile", s.skips)
	}
}

func TestDiscoverMalformedConfigIsSkipped(t *testing.T) {
	s := newStub()
	dir := t.TempDir()
	writeProfile(t, dir, "cluster-broken", "cluster_identity: [this is not a mapping\n")
	// A good profile alongside the broken one, to prove the broken one does not
	// take the rest of the fleet down with it.
	writeClusterProfile(t, dir, "cluster-ok", "projA", "healthy", "us-central1")

	clusters, err := s.discoverer().Discover(context.Background(), dir)
	if err != nil {
		t.Fatalf("Discover: %v", err)
	}
	if got, want := len(clusters), 1; got != want {
		t.Fatalf("got %d clusters (%v), want only the healthy one", got, profiles(clusters))
	}
	if clusters[0].Identity.Cluster != "healthy" {
		t.Errorf("got cluster %q, want %q", clusters[0].Identity.Cluster, "healthy")
	}
	if s.skips["cluster-broken"] == nil {
		t.Errorf("expected the broken profile to be reported, skips = %v", s.skips)
	}
}

func TestDiscoverMissingDirIsFatal(t *testing.T) {
	// Deliberately fatal, unlike every other discovery failure. The directory is
	// written by another process, so a restart is what fixes this — and since
	// discovery runs only once, starting successfully without it would mean
	// never reaching the profile clusters at all.
	// Under an existing, traversable parent, so the failure is reliably
	// ErrNotExist. A path whose parent is also missing is not portable: some
	// systems answer EACCES rather than ENOENT for it, which is a different
	// condition and deliberately handled differently.
	s := newStub()
	missing := filepath.Join(t.TempDir(), "profiles-not-created-yet")
	_, err := s.discoverer().Discover(context.Background(), missing)
	if err == nil {
		t.Fatal("expected an error for a profiles dir that does not exist, got nil")
	}
	if !strings.Contains(err.Error(), "does not exist yet") {
		t.Errorf("expected a 'does not exist yet' error, got: %v", err)
	}
	// Not reported as a skip: a skip means "a cluster we should be reaching was
	// dropped", and here the caller is expected to exit rather than carry on
	// without them.
	if len(s.skips) != 0 {
		t.Errorf("expected no skips when returning fatal, got %v", s.skips)
	}
}

func TestDiscoverUnreadableDirIsNotFatal(t *testing.T) {
	// A directory that exists but cannot be read will not be fixed by a
	// restart, so this degrades instead of crashlooping forever.
	dir := filepath.Join(t.TempDir(), "profiles")
	if err := os.MkdirAll(dir, 0o000); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	t.Cleanup(func() { _ = os.Chmod(dir, 0o700) })
	if os.Geteuid() == 0 {
		t.Skip("running as root, an unreadable directory is still readable")
	}

	s := newStub()
	clusters, err := s.discoverer().Discover(context.Background(), dir)
	if err != nil {
		t.Fatalf("expected an unreadable dir to degrade, not fail: %v", err)
	}
	if len(clusters) != 0 {
		t.Errorf("expected 0 clusters, got %d", len(clusters))
	}
	if s.skips[NoProfile] == nil {
		t.Errorf("expected an unreadable profiles dir to be reported under %q, skips = %v", NoProfile, s.skips)
	}
}

func TestZeroDiscovererDiscardsSkipsRatherThanPanicking(t *testing.T) {
	// OnSkip is optional, and the path that would notice is the one no caller
	// exercises on a healthy fleet. A nil callback here would panic at the
	// first malformed profile, in production, long after this package was
	// reviewed.
	dir := t.TempDir()
	writeProfile(t, dir, "cluster-broken", "cluster_identity: [this is not a mapping\n")

	clusters, err := Discoverer{Describe: func(context.Context, Identity) (*container.Cluster, error) {
		return nil, errors.New("should not be reached")
	}}.Discover(context.Background(), dir)
	if err != nil {
		t.Fatalf("Discover: %v", err)
	}
	if len(clusters) != 0 {
		t.Errorf("got %d clusters, want none", len(clusters))
	}
}
