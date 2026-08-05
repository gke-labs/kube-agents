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
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/prometheus/client_golang/prometheus/testutil"
	"golang.org/x/oauth2"
	"k8s.io/client-go/rest"
	clientcmdapi "k8s.io/client-go/tools/clientcmd/api"
)

// minimalKubeconfig returns a syntactically valid kubeconfig
// pointing at an unreachable server. Enough for
// clientcmd.BuildConfigFromFlags to parse and kubernetes.NewForConfig
// to construct a client; no real requests are made in these tests.
func minimalKubeconfig(serverURL string) string {
	return `apiVersion: v1
kind: Config
clusters:
- name: c1
  cluster:
    server: ` + serverURL + `
contexts:
- name: c1
  context:
    cluster: c1
    user: u1
users:
- name: u1
current-context: c1
`
}

// writeClusterProfile creates a Cluster Agent profile directory the way
// cluster_agent_profile.py does: a kubeconfig.yaml plus a config.yaml carrying
// a cluster_identity block.
func writeClusterProfile(t *testing.T, base, profile, project, cluster, location string) {
	t.Helper()
	home := filepath.Join(base, profile)
	if err := os.MkdirAll(home, 0o700); err != nil {
		t.Fatalf("mkdir %s: %v", home, err)
	}
	if err := os.WriteFile(filepath.Join(home, "kubeconfig.yaml"),
		[]byte(minimalKubeconfig("https://example.invalid")), 0o600); err != nil {
		t.Fatalf("write kubeconfig: %v", err)
	}
	cfg := "model:\n  provider: custom\ncluster_identity:\n" +
		"  project: " + project + "\n" +
		"  cluster: " + cluster + "\n" +
		"  location: " + location + "\n"
	if err := os.WriteFile(filepath.Join(home, "config.yaml"), []byte(cfg), 0o600); err != nil {
		t.Fatalf("write config.yaml: %v", err)
	}
}

// writeNonClusterProfile creates a profile with no cluster_identity — what
// "default" and "platform" look like on disk.
func writeNonClusterProfile(t *testing.T, base, profile string) {
	t.Helper()
	home := filepath.Join(base, profile)
	if err := os.MkdirAll(home, 0o700); err != nil {
		t.Fatalf("mkdir %s: %v", home, err)
	}
	if err := os.WriteFile(filepath.Join(home, "config.yaml"),
		[]byte("model:\n  provider: custom\n"), 0o600); err != nil {
		t.Fatalf("write config.yaml: %v", err)
	}
}

func TestDiscoverClusterProfiles_ReadsIdentityNotDirName(t *testing.T) {
	dir := t.TempDir()
	// Profile directory names are sanitized and hash-truncated by the Python
	// side, so the identity must come from config.yaml, not the dir name.
	writeClusterProfile(t, dir, "cluster-projA-prod-us-central1", "projA", "prod", "us-central1")
	writeClusterProfile(t, dir, "cluster-projB-staging-europe-west1", "projB", "staging", "europe-west1")

	clusters, err := discoverClusterProfiles(context.Background(), dir, newMetrics())
	if err != nil {
		t.Fatalf("discoverClusterProfiles: %v", err)
	}
	if got, want := len(clusters), 2; got != want {
		t.Fatalf("got %d clusters, want %d", got, want)
	}
	byName := make(map[string]targetCluster, len(clusters))
	for _, c := range clusters {
		byName[c.Name] = c
	}
	prod, ok := byName["prod"]
	if !ok {
		t.Fatalf("missing cluster %q; got %v", "prod", clusterNames(clusters))
	}
	if prod.ProjectID != "projA" || prod.Location != "us-central1" {
		t.Errorf("prod identity = project %q location %q; want projA / us-central1", prod.ProjectID, prod.Location)
	}
	if prod.Profile != "cluster-projA-prod-us-central1" {
		t.Errorf("prod profile = %q; want the directory name", prod.Profile)
	}
	if prod.Client == nil {
		t.Error("prod has no client")
	}
	if _, ok := byName["staging"]; !ok {
		t.Errorf("missing cluster %q; got %v", "staging", clusterNames(clusters))
	}
}

func TestDiscoverClusterProfiles_SkipsNonClusterProfiles(t *testing.T) {
	dir := t.TempDir()
	writeClusterProfile(t, dir, "cluster-p-good-us-central1", "p", "good", "us-central1")
	// "default" and "platform" exist but carry no cluster_identity.
	writeNonClusterProfile(t, dir, "default")
	writeNonClusterProfile(t, dir, "platform")
	// A profile with an identity but no kubeconfig is not usable either.
	halfHome := filepath.Join(dir, "cluster-p-nokubeconfig-us-central1")
	if err := os.MkdirAll(halfHome, 0o700); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	if err := os.WriteFile(filepath.Join(halfHome, "config.yaml"),
		[]byte("cluster_identity:\n  project: p\n  cluster: nokubeconfig\n  location: us-central1\n"), 0o600); err != nil {
		t.Fatalf("write config.yaml: %v", err)
	}
	// Loose files and dotfiles at the top level are not profiles.
	if err := os.WriteFile(filepath.Join(dir, "kanban.db"), []byte("junk"), 0o600); err != nil {
		t.Fatalf("write loose file: %v", err)
	}
	if err := os.MkdirAll(filepath.Join(dir, ".cache"), 0o700); err != nil {
		t.Fatalf("mkdir dotdir: %v", err)
	}

	clusters, err := discoverClusterProfiles(context.Background(), dir, newMetrics())
	if err != nil {
		t.Fatalf("discoverClusterProfiles: %v", err)
	}
	if got, want := len(clusters), 1; got != want {
		t.Fatalf("got %d clusters (%v), want %d", got, clusterNames(clusters), want)
	}
	if clusters[0].Name != "good" {
		t.Errorf("got cluster %q; want %q", clusters[0].Name, "good")
	}
}

func TestDiscoverClusterProfiles_NoProfilesIsNotAnError(t *testing.T) {
	// A single-cluster install has no Cluster Agent profiles at all —
	// reconcile only creates them for clusters other than the management one —
	// so an empty result is a steady state, not a misconfiguration. Erroring
	// here would crashloop the sidecar on every single-cluster install.
	dir := t.TempDir()
	writeNonClusterProfile(t, dir, "platform")

	clusters, err := discoverClusterProfiles(context.Background(), dir, newMetrics())
	if err != nil {
		t.Fatalf("discoverClusterProfiles: %v", err)
	}
	if len(clusters) != 0 {
		t.Errorf("expected 0 clusters, got %d (%v)", len(clusters), clusterNames(clusters))
	}
}

func TestDiscoverClusterProfiles_SameNameDifferentLocation(t *testing.T) {
	dir := t.TempDir()
	// A GKE cluster name is unique only within a project and location, so this
	// is two real clusters, not a duplicate — and an ordinary fleet layout.
	// Keying identity on the bare name would watch whichever one ReadDir
	// returned first and silently drop the other.
	writeClusterProfile(t, dir, "cluster-p-prod-us-central1", "p", "prod", "us-central1")
	writeClusterProfile(t, dir, "cluster-p-prod-europe-west1", "p", "prod", "europe-west1")

	m := newMetrics()
	clusters, err := discoverClusterProfiles(context.Background(), dir, m)
	if err != nil {
		t.Fatalf("discoverClusterProfiles: %v", err)
	}
	if got, want := len(clusters), 2; got != want {
		t.Fatalf("got %d clusters, want %d — same name in two locations must not collide", got, want)
	}
	locations := map[string]bool{}
	for _, c := range clusters {
		if c.Name != "prod" {
			t.Errorf("expected both clusters named %q, got %q", "prod", c.Name)
		}
		locations[c.Location] = true
	}
	for _, want := range []string{"us-central1", "europe-west1"} {
		if !locations[want] {
			t.Errorf("missing cluster in %s; got locations %v", want, locations)
		}
	}
	// Neither was treated as a duplicate.
	for _, p := range []string{"cluster-p-prod-us-central1", "cluster-p-prod-europe-west1"} {
		if got := testutil.ToFloat64(m.clusterDiscoveryErrors.WithLabelValues(p)); got != 0 {
			t.Errorf("profile %s was counted as an error (%v); it is a distinct cluster", p, got)
		}
	}
}

func TestDiscoverClusterProfiles_DuplicateClusterIsSkipped(t *testing.T) {
	dir := t.TempDir()
	// Two profiles claiming the same cluster would give it two watchers and two
	// independent dedup caches. Take the first, count the second.
	writeClusterProfile(t, dir, "profile-one", "projA", "prod", "us-central1")
	writeClusterProfile(t, dir, "profile-two", "projA", "prod", "us-central1")

	m := newMetrics()
	clusters, err := discoverClusterProfiles(context.Background(), dir, m)
	if err != nil {
		t.Fatalf("discoverClusterProfiles: %v", err)
	}
	if got, want := len(clusters), 1; got != want {
		t.Fatalf("got %d clusters (%v), want %d", got, clusterNames(clusters), want)
	}
	if clusters[0].Profile != "profile-one" {
		t.Errorf("expected the first profile to win, got %q", clusters[0].Profile)
	}
	if got := testutil.ToFloat64(m.clusterDiscoveryErrors.WithLabelValues("profile-two")); got != 1 {
		t.Errorf("expected the duplicate to be counted once, got %v", got)
	}
}

func TestDiscoverClusterProfiles_MalformedConfigIsSkipped(t *testing.T) {
	dir := t.TempDir()
	home := filepath.Join(dir, "cluster-broken")
	if err := os.MkdirAll(home, 0o700); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	if err := os.WriteFile(filepath.Join(home, "kubeconfig.yaml"),
		[]byte(minimalKubeconfig("https://example.invalid")), 0o600); err != nil {
		t.Fatalf("write kubeconfig: %v", err)
	}
	if err := os.WriteFile(filepath.Join(home, "config.yaml"),
		[]byte("cluster_identity: [this is not a mapping\n"), 0o600); err != nil {
		t.Fatalf("write config.yaml: %v", err)
	}

	// A good profile alongside the broken one, to prove the broken one does not
	// take the rest of the fleet down with it.
	writeClusterProfile(t, dir, "cluster-ok", "projA", "healthy", "us-central1")

	m := newMetrics()
	clusters, err := discoverClusterProfiles(context.Background(), dir, m)
	if err != nil {
		t.Fatalf("discoverClusterProfiles: %v", err)
	}
	if got, want := len(clusters), 1; got != want {
		t.Fatalf("got %d clusters (%v), want only the healthy one", got, clusterNames(clusters))
	}
	if clusters[0].Name != "healthy" {
		t.Errorf("got cluster %q, want %q", clusters[0].Name, "healthy")
	}
	if got := testutil.ToFloat64(m.clusterDiscoveryErrors.WithLabelValues("cluster-broken")); got != 1 {
		t.Errorf("expected the broken profile to be counted once, got %v", got)
	}
}

func TestDiscoverClusterProfiles_MissingDirIsFatal(t *testing.T) {
	// Deliberately fatal, unlike every other discovery failure. The directory is
	// written by another process, so a restart is what fixes this — and since
	// discovery runs only once, starting successfully without it would mean
	// never watching the profile clusters at all.
	m := newMetrics()
	_, err := discoverClusterProfiles(context.Background(), "/nonexistent/definitely/not/here", m)
	if err == nil {
		t.Fatal("expected an error for a profiles dir that does not exist, got nil")
	}
	if !strings.Contains(err.Error(), "does not exist yet") {
		t.Errorf("expected a 'does not exist yet' error, got: %v", err)
	}
	// Not counted: the counter means "a cluster we should be watching was
	// dropped", and here the process is exiting rather than carrying on
	// without them.
	if got := testutil.ToFloat64(m.clusterDiscoveryErrors.WithLabelValues("-")); got != 0 {
		t.Errorf("expected no discovery-error count when exiting, got %v", got)
	}
}

func TestDiscoverClusterProfiles_UnreadableDirIsNotFatal(t *testing.T) {
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

	m := newMetrics()
	clusters, err := discoverClusterProfiles(context.Background(), dir, m)
	if err != nil {
		t.Fatalf("expected an unreadable dir to degrade, not fail: %v", err)
	}
	if len(clusters) != 0 {
		t.Errorf("expected 0 clusters, got %d", len(clusters))
	}
	if got := testutil.ToFloat64(m.clusterDiscoveryErrors.WithLabelValues("-")); got != 1 {
		t.Errorf("expected an unreadable profiles dir to be counted, got %v", got)
	}
}

func TestValidate_ProfilesDirFlagRules(t *testing.T) {
	cases := []struct {
		name    string
		f       flags
		wantErr string
	}{
		{
			// The combination the operator passes: watch every profile cluster
			// plus the management cluster, which never gets a profile.
			name: "profiles-dir with in-cluster and a name is valid",
			f: flags{
				daemonURL:   "http://localhost:8699",
				tokenEnv:    "TOKEN",
				mode:        "per-incident",
				owner:       "watcher",
				dedupWindow: 1,
				profilesDir: "/some/dir",
				inCluster:   true,
				clusterName: "platform-agent-host",
			},
			wantErr: "",
		},
		{
			name: "profiles-dir with kubeconfig and a name is valid",
			f: flags{
				daemonURL:   "http://localhost:8699",
				tokenEnv:    "TOKEN",
				mode:        "per-incident",
				owner:       "watcher",
				dedupWindow: 1,
				profilesDir: "/some/dir",
				kubeconfig:  "/some/file",
				clusterName: "host",
			},
			wantErr: "",
		},
		{
			// Without a name the direct cluster would report an empty cluster
			// label alongside properly-named profile clusters.
			name: "profiles-dir with in-cluster but no name",
			f: flags{
				daemonURL:   "http://localhost:8699",
				tokenEnv:    "TOKEN",
				mode:        "per-incident",
				owner:       "watcher",
				dedupWindow: 1,
				profilesDir: "/some/dir",
				inCluster:   true,
			},
			wantErr: "--cluster-name is required when combining --profiles-dir",
		},
		{
			name: "profiles-dir alone is valid",
			f: flags{
				daemonURL:   "http://localhost:8699",
				tokenEnv:    "TOKEN",
				mode:        "per-incident",
				owner:       "watcher",
				dedupWindow: 1,
				profilesDir: "/some/dir",
			},
			wantErr: "",
		},
		{
			// No profiles means no cluster_identity to fall back on, so an
			// unset name would label every payload and metric series with the
			// empty string.
			name: "single-cluster mode requires a name",
			f: flags{
				daemonURL:   "http://localhost:8699",
				tokenEnv:    "TOKEN",
				mode:        "per-incident",
				owner:       "watcher",
				dedupWindow: 1,
				inCluster:   true,
			},
			wantErr: "--cluster-name is required (it labels",
		},
		{
			// Regression: per-incident + dry-run used to return early from
			// validate(), skipping every check below the mode switch. That is
			// the default mode and the usual way people try the watcher out,
			// so the mutual-exclusion rules were unenforced exactly where they
			// were most likely to be tripped.
			name: "dry-run does not skip profiles-dir rules",
			f: flags{
				mode:        "per-incident",
				dryRun:      true,
				dedupWindow: 1,
				profilesDir: "/some/dir",
				kubeconfig:  "/some/file",
			},
			wantErr: "--cluster-name is required when combining --profiles-dir",
		},
		{
			// --owner is the one thing dry-run legitimately exempts: it only
			// becomes a header on daemon requests, which dry-run never makes.
			name: "dry-run does not require owner",
			f: flags{
				mode:        "per-incident",
				dryRun:      true,
				dedupWindow: 1,
				profilesDir: "/some/dir",
			},
			wantErr: "",
		},
		{
			name: "dry-run still validates dedup-window",
			f: flags{
				mode:        "per-incident",
				dryRun:      true,
				dedupWindow: 0,
			},
			wantErr: "--dedup-window must be > 0",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			err := tc.f.validate()
			if tc.wantErr == "" {
				if err != nil {
					t.Fatalf("unexpected error: %v", err)
				}
				return
			}
			if err == nil {
				t.Fatalf("expected error containing %q, got nil", tc.wantErr)
			}
			if !strings.Contains(err.Error(), tc.wantErr) {
				t.Errorf("expected error containing %q, got: %v", tc.wantErr, err)
			}
		})
	}
}

func TestDedupPersistPath(t *testing.T) {
	// Each cluster keeps its own cache, so they must not all snapshot to the
	// same file — the last writer would otherwise clobber the fleet's state.
	cases := []struct {
		base    string
		cluster string
		want    string
	}{
		{"/var/lib/w/dedup.json", "prod-us-central1", "/var/lib/w/dedup-prod-us-central1.json"},
		{"/var/lib/w/dedup", "prod", "/var/lib/w/dedup-prod"},
		{"dedup.json", "a", "dedup-a.json"},
		{"", "prod", ""}, // persistence disabled stays disabled
	}
	for _, tc := range cases {
		if got := dedupPersistPath(tc.base, tc.cluster); got != tc.want {
			t.Errorf("dedupPersistPath(%q, %q) = %q; want %q", tc.base, tc.cluster, got, tc.want)
		}
	}

	// Distinct clusters must never collide on the same base path.
	a := dedupPersistPath("/var/lib/w/dedup.json", "cluster-a")
	b := dedupPersistPath("/var/lib/w/dedup.json", "cluster-b")
	if a == b {
		t.Errorf("two clusters resolved to the same persist path: %q", a)
	}
}

type roundTripperFunc func(*http.Request) (*http.Response, error)

func (f roundTripperFunc) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func TestUseGoogleTokenSource(t *testing.T) {
	// Profile kubeconfigs authenticate by running gke-gcloud-auth-plugin, which
	// is not in this image. The exec directive must be dropped and replaced
	// with a bearer token, while the server address and CA are left alone.
	cfg := &rest.Config{
		Host:         "https://example.invalid",
		ExecProvider: &clientcmdapi.ExecConfig{Command: "gke-gcloud-auth-plugin"},
	}
	useGoogleTokenSource(cfg, oauth2.StaticTokenSource(&oauth2.Token{AccessToken: "test-token"}))

	if cfg.ExecProvider != nil {
		t.Error("ExecProvider still set; the missing plugin would still be invoked")
	}
	if cfg.Host != "https://example.invalid" {
		t.Errorf("Host = %q; the server address must survive untouched", cfg.Host)
	}
	if cfg.WrapTransport == nil {
		t.Fatal("WrapTransport not set; no credential would be attached")
	}

	// The wrapper must actually put the token on the wire.
	var got string
	rt := cfg.WrapTransport(roundTripperFunc(func(r *http.Request) (*http.Response, error) {
		got = r.Header.Get("Authorization")
		return &http.Response{StatusCode: 200, Body: http.NoBody, Request: r}, nil
	}))
	req, err := http.NewRequest(http.MethodGet, "https://example.invalid/api/v1/events", nil)
	if err != nil {
		t.Fatalf("new request: %v", err)
	}
	if _, err := rt.RoundTrip(req); err != nil {
		t.Fatalf("round trip: %v", err)
	}
	if want := "Bearer test-token"; got != want {
		t.Errorf("Authorization = %q; want %q", got, want)
	}
}

func clusterNames(clusters []targetCluster) []string {
	out := make([]string, 0, len(clusters))
	for _, c := range clusters {
		out = append(out, c.Name)
	}
	return out
}
