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
	"os"
	"path/filepath"
	"strings"
	"testing"
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

	clusters, err := discoverClusterProfiles(dir)
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

	clusters, err := discoverClusterProfiles(dir)
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

func TestDiscoverClusterProfiles_NoProfilesIsError(t *testing.T) {
	dir := t.TempDir()
	writeNonClusterProfile(t, dir, "platform")

	_, err := discoverClusterProfiles(dir)
	if err == nil {
		t.Fatal("expected error when no cluster profiles exist, got nil")
	}
	if !strings.Contains(err.Error(), "no Cluster Agent profiles found") {
		t.Errorf("expected 'no Cluster Agent profiles found' in error, got: %v", err)
	}
}

func TestDiscoverClusterProfiles_DuplicateClusterIsError(t *testing.T) {
	dir := t.TempDir()
	// Two profiles claiming the same cluster would give it two watchers and
	// two independent dedup caches, so it must fail loudly.
	writeClusterProfile(t, dir, "profile-one", "projA", "prod", "us-central1")
	writeClusterProfile(t, dir, "profile-two", "projA", "prod", "us-central1")

	_, err := discoverClusterProfiles(dir)
	if err == nil {
		t.Fatal("expected error for duplicate cluster, got nil")
	}
	if !strings.Contains(err.Error(), "both claim cluster") {
		t.Errorf("expected 'both claim cluster' in error, got: %v", err)
	}
}

func TestDiscoverClusterProfiles_MalformedConfigIsError(t *testing.T) {
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

	// Unparseable config is a real error, not a silent skip: the profile has
	// a kubeconfig, so it looks like a cluster we are meant to be watching.
	if _, err := discoverClusterProfiles(dir); err == nil {
		t.Fatal("expected error for malformed config.yaml, got nil")
	}
}

func TestDiscoverClusterProfiles_MissingDirIsError(t *testing.T) {
	_, err := discoverClusterProfiles("/nonexistent/definitely/not/here")
	if err == nil {
		t.Fatal("expected error for missing dir, got nil")
	}
}

func TestValidate_ProfilesDirMutualExclusion(t *testing.T) {
	cases := []struct {
		name    string
		f       flags
		wantErr string
	}{
		{
			name: "profiles-dir with kubeconfig",
			f: flags{
				daemonURL:     "http://localhost:8699",
				tokenEnv:      "TOKEN",
				mode:          "per-incident",
				owner:         "watcher",
				dedupWindow:   1,
				profilesDir:   "/some/dir",
				kubeconfig:    "/some/file",
			},
			wantErr: "--profiles-dir and --kubeconfig are mutually exclusive",
		},
		{
			name: "profiles-dir with in-cluster",
			f: flags{
				daemonURL:     "http://localhost:8699",
				tokenEnv:      "TOKEN",
				mode:          "per-incident",
				owner:         "watcher",
				dedupWindow:   1,
				profilesDir:   "/some/dir",
				inCluster:     true,
			},
			wantErr: "--profiles-dir and --in-cluster are mutually exclusive",
		},
		{
			name: "profiles-dir with cluster-name",
			f: flags{
				daemonURL:     "http://localhost:8699",
				tokenEnv:      "TOKEN",
				mode:          "per-incident",
				owner:         "watcher",
				dedupWindow:   1,
				profilesDir:   "/some/dir",
				clusterName:   "explicit",
			},
			wantErr: "--cluster-name must be empty when --profiles-dir is set",
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
			// Regression: per-incident + dry-run used to return early from
			// validate(), skipping every check below the mode switch. That is
			// the default mode and the usual way people try the watcher out,
			// so the mutual-exclusion rules were unenforced exactly where they
			// were most likely to be tripped.
			name: "dry-run does not skip profiles-dir exclusivity",
			f: flags{
				mode:        "per-incident",
				dryRun:      true,
				dedupWindow: 1,
				profilesDir: "/some/dir",
				kubeconfig:  "/some/file",
			},
			wantErr: "--profiles-dir and --kubeconfig are mutually exclusive",
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

func clusterNames(clusters []targetCluster) []string {
	out := make([]string, 0, len(clusters))
	for _, c := range clusters {
		out = append(out, c.Name)
	}
	return out
}
