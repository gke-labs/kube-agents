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

	"k8s.io/client-go/kubernetes"
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

func writeKubeconfig(t *testing.T, dir, name string) {
	t.Helper()
	path := filepath.Join(dir, name)
	if err := os.WriteFile(path, []byte(minimalKubeconfig("https://example.invalid")), 0o600); err != nil {
		t.Fatalf("write %s: %v", path, err)
	}
}

func TestBuildKubeClientsFromDir_LoadsAndNames(t *testing.T) {
	dir := t.TempDir()
	writeKubeconfig(t, dir, "prod-us-central1.yaml")
	writeKubeconfig(t, dir, "staging-europe-west1.yaml")

	clients, err := buildKubeClientsFromDir(dir)
	if err != nil {
		t.Fatalf("buildKubeClientsFromDir: %v", err)
	}
	if got, want := len(clients), 2; got != want {
		t.Fatalf("got %d clients, want %d", got, want)
	}
	for _, want := range []string{"prod-us-central1", "staging-europe-west1"} {
		if _, ok := clients[want]; !ok {
			t.Errorf("missing cluster %q; got keys %v", want, keys(clients))
		}
	}
}

func TestBuildKubeClientsFromDir_SkipsDotfilesAndSubdirs(t *testing.T) {
	dir := t.TempDir()
	writeKubeconfig(t, dir, "good.yaml")
	// Dotfile (e.g. .DS_Store) — must be skipped.
	if err := os.WriteFile(filepath.Join(dir, ".DS_Store"), []byte("junk"), 0o600); err != nil {
		t.Fatalf("write dotfile: %v", err)
	}
	// Subdirectory — must be skipped.
	if err := os.Mkdir(filepath.Join(dir, "nested"), 0o700); err != nil {
		t.Fatalf("mkdir: %v", err)
	}

	clients, err := buildKubeClientsFromDir(dir)
	if err != nil {
		t.Fatalf("buildKubeClientsFromDir: %v", err)
	}
	if got, want := len(clients), 1; got != want {
		t.Fatalf("got %d clients, want %d (dotfile + subdir should not count)", got, want)
	}
	if _, ok := clients["good"]; !ok {
		t.Errorf("missing cluster %q; got keys %v", "good", keys(clients))
	}
}

func TestBuildKubeClientsFromDir_EmptyDirIsError(t *testing.T) {
	dir := t.TempDir()
	_, err := buildKubeClientsFromDir(dir)
	if err == nil {
		t.Fatal("expected error for empty dir, got nil")
	}
	if !strings.Contains(err.Error(), "no kubeconfig files found") {
		t.Errorf("expected 'no kubeconfig files found' in error, got: %v", err)
	}
}

func TestBuildKubeClientsFromDir_DuplicateNameIsError(t *testing.T) {
	dir := t.TempDir()
	// Two files that strip to the same cluster name.
	writeKubeconfig(t, dir, "prod.yaml")
	writeKubeconfig(t, dir, "prod.yml")

	_, err := buildKubeClientsFromDir(dir)
	if err == nil {
		t.Fatal("expected error for duplicate cluster names, got nil")
	}
	if !strings.Contains(err.Error(), "duplicate cluster name") {
		t.Errorf("expected 'duplicate cluster name' in error, got: %v", err)
	}
}

func TestBuildKubeClientsFromDir_MissingDirIsError(t *testing.T) {
	_, err := buildKubeClientsFromDir("/nonexistent/definitely/not/here")
	if err == nil {
		t.Fatal("expected error for missing dir, got nil")
	}
}

func TestValidate_KubeconfigDirMutualExclusion(t *testing.T) {
	cases := []struct {
		name    string
		f       flags
		wantErr string
	}{
		{
			name: "kubeconfig-dir with kubeconfig",
			f: flags{
				daemonURL:     "http://localhost:8699",
				tokenEnv:      "TOKEN",
				mode:          "per-incident",
				owner:         "watcher",
				dedupWindow:   1,
				kubeconfigDir: "/some/dir",
				kubeconfig:    "/some/file",
			},
			wantErr: "--kubeconfig-dir and --kubeconfig are mutually exclusive",
		},
		{
			name: "kubeconfig-dir with in-cluster",
			f: flags{
				daemonURL:     "http://localhost:8699",
				tokenEnv:      "TOKEN",
				mode:          "per-incident",
				owner:         "watcher",
				dedupWindow:   1,
				kubeconfigDir: "/some/dir",
				inCluster:     true,
			},
			wantErr: "--kubeconfig-dir and --in-cluster are mutually exclusive",
		},
		{
			name: "kubeconfig-dir with cluster-name",
			f: flags{
				daemonURL:     "http://localhost:8699",
				tokenEnv:      "TOKEN",
				mode:          "per-incident",
				owner:         "watcher",
				dedupWindow:   1,
				kubeconfigDir: "/some/dir",
				clusterName:   "explicit",
			},
			wantErr: "--cluster-name must be empty when --kubeconfig-dir is set",
		},
		{
			name: "kubeconfig-dir alone is valid",
			f: flags{
				daemonURL:     "http://localhost:8699",
				tokenEnv:      "TOKEN",
				mode:          "per-incident",
				owner:         "watcher",
				dedupWindow:   1,
				kubeconfigDir: "/some/dir",
			},
			wantErr: "",
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

func keys(m map[string]kubernetes.Interface) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	return out
}
