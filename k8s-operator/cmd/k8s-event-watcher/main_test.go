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
	"math/big"
	"os"
	"path/filepath"
	"runtime/debug"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/prometheus/client_golang/prometheus/testutil"
	"golang.org/x/oauth2"
	container "google.golang.org/api/container/v1"

	"github.com/gke-labs/kube-agents/k8s-operator/internal/clusterprofiles"
)

// minimalKubeconfig returns a syntactically valid kubeconfig
// pointing at an unreachable server. Enough for
// clientcmd.BuildConfigFromFlags to parse and kubernetes.NewForConfig
// to construct a client; no real requests are made in these tests.
func minimalKubeconfig(serverURL, contextName string) string {
	return `apiVersion: v1
kind: Config
clusters:
- name: ` + contextName + `
  cluster:
    server: ` + serverURL + `
contexts:
- name: ` + contextName + `
  context:
    cluster: ` + contextName + `
    user: u1
users:
- name: u1
current-context: ` + contextName + `
`
}

// gkeContext is the context name `gcloud container clusters get-credentials`
// writes. Only the --kubeconfig flag's tests need it now; discovery reads
// config.yaml and asks the GKE API.
func gkeContext(project, cluster, location string) string {
	return "gke_" + project + "_" + location + "_" + cluster
}

// testCA is the certificate stubGKE hands back as every cluster's CA, built
// once for the whole package. Generated rather than pasted in: a PEM blob in a
// public repository invites the question of where it came from, and this way
// there is no answer to give.
var (
	testCAOnce sync.Once
	testCA     []byte
	testCAErr  error
)

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
			Subject:               pkix.Name{CommonName: "k8s-event-watcher test CA"},
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

// stubGKE makes discovery answerable without a Google credential or a network
// call: every cluster is described as an ordinary public one, and the token
// source hands back a fixed string. Returns the failures map — put an error in
// it under "<project>/<location>/<cluster>" to make that one lookup fail.
//
// It replaces the package-level discovery seam rather than passing a Discoverer
// in, because buildWatchSet reaches discoverClusterProfiles through two layers
// that have no reason to carry one.
func stubGKE(t *testing.T) map[string]error {
	t.Helper()
	failures := map[string]error{}
	saved := discovery
	t.Cleanup(func() { discovery = saved })
	discovery.Describe = func(_ context.Context, id clusterprofiles.Identity) (*container.Cluster, error) {
		if err, bad := failures[id.String()]; bad {
			return nil, err
		}
		return &container.Cluster{
			Endpoint: id.String() + ".example.invalid",
			// A real certificate, even though no TLS session is established here.
			// ClientConfigForIdentity puts these bytes in rest.Config.CAData and
			// kubernetes.NewForConfig builds the CertPool eagerly, so arbitrary
			// base64 fails at client construction with "unable to parse bytes as
			// PEM block" and every discovery test reports zero clusters. The
			// clusterprofiles package's own tests can use arbitrary base64
			// precisely because they stop before this step.
			MasterAuth: &container.MasterAuth{ClusterCaCertificate: base64.StdEncoding.EncodeToString(testCAPEM(t))},
		}, nil
	}
	discovery.TokenSource = func(context.Context) (oauth2.TokenSource, error) {
		return oauth2.StaticTokenSource(&oauth2.Token{AccessToken: "test-token"}), nil
	}
	return failures
}

// writeClusterProfile creates a Cluster Agent profile directory the way
// cluster_agent_profile.py does: a config.yaml carrying a cluster_identity
// block. No kubeconfig.yaml — since the shell moved into its own pod, the one
// `gcloud container clusters get-credentials` writes lands on the sandbox's
// volume and never appears here.
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

// clusterprofiles.Discoverer owns the scan itself and is tested there; these
// cover what discoverClusterProfiles adds on top of it — a Kubernetes client
// per cluster, and a skipped profile becoming a log line and a metric.

func TestDiscoverClusterProfiles_BuildsAClientPerCluster(t *testing.T) {
	stubGKE(t)
	dir := t.TempDir()
	writeClusterProfile(t, dir, "cluster-projA-prod-us-central1", "projA", "prod", "us-central1")
	writeClusterProfile(t, dir, "cluster-projB-staging-europe-west1", "projB", "staging", "europe-west1")
	writeNonClusterProfile(t, dir, "default")

	m := newMetrics()
	clusters, err := discoverClusterProfiles(context.Background(), dir, m)
	if err != nil {
		t.Fatalf("discoverClusterProfiles: %v", err)
	}
	if got, want := len(clusters), 2; got != want {
		t.Fatalf("got %d clusters (%v), want %d", got, clusterNames(clusters), want)
	}
	byName := make(map[string]targetCluster, len(clusters))
	for _, c := range clusters {
		if c.Client == nil {
			t.Errorf("cluster %s has no client; nothing would be watched on it", c.identity())
		}
		byName[c.Name] = c
	}
	// The whole identity is carried through, not just the name: the dedup
	// snapshot filename and the triage payload both read it off targetCluster.
	prod, ok := byName["prod"]
	if !ok {
		t.Fatalf("missing cluster %q; got %v", "prod", clusterNames(clusters))
	}
	if prod.ProjectID != "projA" || prod.Location != "us-central1" {
		t.Errorf("prod identity = %s; want projA/us-central1/prod", prod.identity())
	}
	if prod.Profile != "cluster-projA-prod-us-central1" {
		t.Errorf("prod profile = %q; want the directory name", prod.Profile)
	}
	if _, ok := byName["staging"]; !ok {
		t.Errorf("missing cluster %q; got %v", "staging", clusterNames(clusters))
	}
}

func TestDiscoverClusterProfiles_SkippedProfileIsCounted(t *testing.T) {
	// The counter is the only thing that says a cluster we should be watching
	// is not being watched, so a skip that does not reach it is a silent
	// half-fleet.
	failures := stubGKE(t)
	failures["p/us-central1/ghost"] = errors.New("clusters.get: 404")
	dir := t.TempDir()
	writeClusterProfile(t, dir, "cluster-p-ghost-us-central1", "p", "ghost", "us-central1")
	writeClusterProfile(t, dir, "cluster-p-real-us-central1", "p", "real", "us-central1")

	m := newMetrics()
	clusters, err := discoverClusterProfiles(context.Background(), dir, m)
	if err != nil {
		t.Fatalf("discoverClusterProfiles: %v", err)
	}
	if got, want := len(clusters), 1; got != want {
		t.Fatalf("got %d clusters (%v), want only the real one", got, clusterNames(clusters))
	}
	if clusters[0].Name != "real" {
		t.Errorf("got cluster %q, want %q", clusters[0].Name, "real")
	}
	if got := testutil.ToFloat64(m.clusterDiscoveryErrors.WithLabelValues("cluster-p-ghost-us-central1")); got != 1 {
		t.Errorf("expected the undescribable cluster to be counted once, got %v", got)
	}
}

func TestDiscoverClusterProfiles_MissingDirIsFatal(t *testing.T) {
	// The package returns this as an error rather than a skip, and the watcher
	// has to propagate it: discovery runs only once, so starting successfully
	// without the directory would mean never watching the profile clusters at
	// all, where exiting lets the next start pick them up.
	// Under an existing, traversable parent, so the failure is reliably
	// ErrNotExist. A path whose parent is also missing is not portable: some
	// systems answer EACCES rather than ENOENT for it, which is a different
	// condition and deliberately handled differently.
	m := newMetrics()
	missing := filepath.Join(t.TempDir(), "profiles-not-created-yet")
	_, err := discoverClusterProfiles(context.Background(), missing, m)
	if err == nil {
		t.Fatal("expected an error for a profiles dir that does not exist, got nil")
	}
	if !strings.Contains(err.Error(), "does not exist yet") {
		t.Errorf("expected a 'does not exist yet' error, got: %v", err)
	}
	// Not counted: the counter means "a cluster we should be watching was
	// dropped", and here the process is exiting rather than carrying on
	// without them.
	if got := testutil.ToFloat64(m.clusterDiscoveryErrors.WithLabelValues(clusterprofiles.NoProfile)); got != 0 {
		t.Errorf("expected no discovery-error count when exiting, got %v", got)
	}
}

func TestDiscoverClusterProfiles_UnreadableDirIsCountedUnderNoProfile(t *testing.T) {
	// A directory that exists but cannot be read will not be fixed by a
	// restart, so the package degrades — and the watcher has to give that
	// failure a label, since there is no profile name to count it under.
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
	if got := testutil.ToFloat64(m.clusterDiscoveryErrors.WithLabelValues(clusterprofiles.NoProfile)); got != 1 {
		t.Errorf("expected an unreadable profiles dir to be counted, got %v", got)
	}
}

// The management cluster is reached twice: --in-cluster covers it from the
// first second of a fresh install, and cluster_agent_reconcile.py now also gives
// it a Cluster Agent profile. Watching it through both would raise two alerts
// per event — each watched cluster has its own dedup cache and EventKey carries
// no cluster — so one of the two has to go, and it is the profile: its GSA
// credential can be denied by IAM or by master authorized networks, and nothing
// would find out until the informer's initial list, long after the entry that
// could not be denied was discarded.
func TestBuildWatchSet_ProfileDuplicateIsDroppedAndItsIdentityKept(t *testing.T) {
	stubGKE(t)
	dir := t.TempDir()
	writeClusterProfile(t, dir, "cluster-projA-mgmt-us-central1", "projA", "mgmt", "us-central1")
	writeClusterProfile(t, dir, "cluster-projA-prod-us-central1", "projA", "prod", "us-central1")

	kubeconfig := filepath.Join(t.TempDir(), "kubeconfig.yaml")
	if err := os.WriteFile(kubeconfig,
		[]byte(minimalKubeconfig("https://example.invalid", gkeContext("projA", "mgmt", "us-central1"))), 0o600); err != nil {
		t.Fatalf("write kubeconfig: %v", err)
	}

	f := &flags{
		profilesDir: dir,
		kubeconfig:  kubeconfig,
		clusterName: "mgmt",
	}
	clusters, err := buildWatchSet(context.Background(), f, newMetrics())
	if err != nil {
		t.Fatalf("buildWatchSet: %v", err)
	}
	if got, want := len(clusters), 2; got != want {
		t.Fatalf("got %d watched clusters, want %d (mgmt is watched once, prod once)", got, want)
	}

	var mgmt []targetCluster
	for _, c := range clusters {
		if c.Name == "mgmt" {
			mgmt = append(mgmt, c)
		}
	}
	if len(mgmt) != 1 {
		t.Fatalf("got %d entries for mgmt, want 1 — a second one doubles every alert on it", len(mgmt))
	}
	if mgmt[0].Profile != "direct" {
		t.Errorf("mgmt is watched through profile %q, want the direct client: the profile's credential can be refused and this one cannot", mgmt[0].Profile)
	}
	// The whole reason the profile entry looked preferable. Losing the triple
	// would blank the payload's project/location and every metric label.
	if mgmt[0].ProjectID != "projA" || mgmt[0].Location != "us-central1" {
		t.Errorf("direct entry is stamped %s, want projA/us-central1/mgmt from the profile it absorbed", mgmt[0].identity())
	}
	// Absorbing one profile must not disturb the others.
	if clusters[0].Name != "prod" || clusters[0].Profile != "cluster-projA-prod-us-central1" {
		t.Errorf("prod is watched as %s/%s, want it untouched", clusters[0].Name, clusters[0].Profile)
	}
}

// The other half of the same rule: before reconcile has created the management
// cluster's profile, --in-cluster is the only thing watching it, so the direct
// entry must survive.
func TestBuildWatchSet_DirectClusterSurvivesWhenNoProfileCoversIt(t *testing.T) {
	stubGKE(t)
	dir := t.TempDir()
	writeClusterProfile(t, dir, "cluster-projA-prod-us-central1", "projA", "prod", "us-central1")

	kubeconfig := filepath.Join(t.TempDir(), "kubeconfig.yaml")
	if err := os.WriteFile(kubeconfig,
		[]byte(minimalKubeconfig("https://example.invalid", gkeContext("projA", "mgmt", "us-central1"))), 0o600); err != nil {
		t.Fatalf("write kubeconfig: %v", err)
	}

	f := &flags{
		profilesDir: dir,
		kubeconfig:  kubeconfig,
		clusterName: "mgmt",
	}
	clusters, err := buildWatchSet(context.Background(), f, newMetrics())
	if err != nil {
		t.Fatalf("buildWatchSet: %v", err)
	}
	var direct []targetCluster
	for _, c := range clusters {
		if c.Profile == "direct" {
			direct = append(direct, c)
		}
	}
	if len(direct) != 1 {
		t.Fatalf("got %d direct entries in %d clusters, want 1 — nothing else is watching mgmt", len(direct), len(clusters))
	}
	// No profile to take an identity from, so the entry carries only its name.
	// That is the pre-existing single-cluster shape, not a regression.
	if direct[0].ProjectID != "" || direct[0].Location != "" {
		t.Errorf("direct entry is stamped %s; nothing supplied a project or location", direct[0].identity())
	}
}

// GKE names are unique per (project, location), so two profiles can answer to
// one name and the direct entry — which knows only a name — cannot tell them
// apart. Absorbing a guess would unwatch the other cluster and mis-stamp this
// one, so nothing is absorbed and no direct entry is added.
func TestBuildWatchSet_AmbiguousNameAbsorbsNothing(t *testing.T) {
	stubGKE(t)
	dir := t.TempDir()
	writeClusterProfile(t, dir, "cluster-projA-mgmt-us-central1", "projA", "mgmt", "us-central1")
	writeClusterProfile(t, dir, "cluster-projA-mgmt-us-east1", "projA", "mgmt", "us-east1")

	kubeconfig := filepath.Join(t.TempDir(), "kubeconfig.yaml")
	if err := os.WriteFile(kubeconfig,
		[]byte(minimalKubeconfig("https://example.invalid", gkeContext("projA", "mgmt", "us-central1"))), 0o600); err != nil {
		t.Fatalf("write kubeconfig: %v", err)
	}

	f := &flags{
		profilesDir: dir,
		kubeconfig:  kubeconfig,
		clusterName: "mgmt",
	}
	clusters, err := buildWatchSet(context.Background(), f, newMetrics())
	if err != nil {
		t.Fatalf("buildWatchSet: %v", err)
	}
	if got, want := len(clusters), 2; got != want {
		t.Fatalf("got %d watched clusters, want %d — both same-named clusters keep their profile", got, want)
	}
	byIdentity := map[string]string{}
	for _, c := range clusters {
		if c.Profile == "direct" {
			t.Errorf("a direct entry was added for an ambiguous name; it would have taken one of the two identities at random")
		}
		byIdentity[c.identity()] = c.Profile
	}
	// The dropped-cluster case: neither may go missing.
	for _, want := range []string{"projA/us-central1/mgmt", "projA/us-east1/mgmt"} {
		if _, ok := byIdentity[want]; !ok {
			t.Errorf("%s is not watched; the watch set is %v", want, byIdentity)
		}
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

func clusterNames(clusters []targetCluster) []string {
	out := make([]string, 0, len(clusters))
	for _, c := range clusters {
		out = append(out, c.Name)
	}
	return out
}

// The soft memory limit is half of what the operator reports as the container's
// limit, and only when nothing else has already set one.
func TestDeriveMemoryLimit(t *testing.T) {
	tests := []struct {
		name           string
		goMemLimit     string
		containerLimit string
		wantApply      bool
		wantBytes      int64
		wantReason     string
	}{
		{name: "container limit set", containerLimit: "2147483648", wantApply: true, wantBytes: 1073741824},
		{name: "explicit GOMEMLIMIT wins", goMemLimit: "512MiB", containerLimit: "2147483648", wantReason: "GOMEMLIMIT=512MiB is set and takes precedence"},
		{name: "nothing set", wantReason: "EVENT_WATCHER_MEMORY_LIMIT_BYTES is not set"},
		{name: "garbage", containerLimit: "2Gi", wantReason: `EVENT_WATCHER_MEMORY_LIMIT_BYTES="2Gi" is not a positive byte count`},
		{name: "zero", containerLimit: "0", wantReason: `EVENT_WATCHER_MEMORY_LIMIT_BYTES="0" is not a positive byte count`},
		{name: "negative", containerLimit: "-5", wantReason: `EVENT_WATCHER_MEMORY_LIMIT_BYTES="-5" is not a positive byte count`},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			gotBytes, gotApply, gotReason := deriveMemoryLimit(tt.goMemLimit, tt.containerLimit)
			if gotApply != tt.wantApply {
				t.Fatalf("apply = %v, want %v (reason %q)", gotApply, tt.wantApply, gotReason)
			}
			if gotBytes != tt.wantBytes {
				t.Errorf("bytes = %d, want %d", gotBytes, tt.wantBytes)
			}
			if !strings.Contains(gotReason, tt.wantReason) {
				t.Errorf("reason = %q, want it to contain %q", gotReason, tt.wantReason)
			}
		})
	}
}

// applyMemoryLimit turns the derived value into the runtime's soft limit —
// the same setting GOMEMLIMIT controls — and leaves it alone otherwise.
func TestApplyMemoryLimit_SetsTheRuntimeSoftLimit(t *testing.T) {
	// SetMemoryLimit(-1) reads the current limit without changing it.
	prev := debug.SetMemoryLimit(-1)
	t.Cleanup(func() { debug.SetMemoryLimit(prev) })

	t.Setenv("GOMEMLIMIT", "")
	t.Setenv("EVENT_WATCHER_MEMORY_LIMIT_BYTES", "2147483648")
	applyMemoryLimit()
	if got := debug.SetMemoryLimit(-1); got != 1073741824 {
		t.Errorf("runtime soft limit = %d, want 1073741824", got)
	}

	// An explicit GOMEMLIMIT is respected: the limit set above must not move.
	debug.SetMemoryLimit(prev)
	t.Setenv("GOMEMLIMIT", "256MiB")
	applyMemoryLimit()
	if got := debug.SetMemoryLimit(-1); got != prev {
		t.Errorf("runtime soft limit = %d with GOMEMLIMIT set, want it untouched at %d", got, prev)
	}
}

// The process applies the limit, not just the helper: realMain has to reach
// applyMemoryLimit before anything that can fail. A kubeconfig that does not
// parse stops the run right after it, and the runtime's limit shows whether
// the call happened.
func TestRealMain_AppliesTheMemoryLimitBeforeStarting(t *testing.T) {
	prev := debug.SetMemoryLimit(-1)
	t.Cleanup(func() { debug.SetMemoryLimit(prev) })
	t.Setenv("GOMEMLIMIT", "")
	t.Setenv("EVENT_WATCHER_MEMORY_LIMIT_BYTES", "2147483648")

	badKubeconfig := filepath.Join(t.TempDir(), "kubeconfig")
	if err := os.WriteFile(badKubeconfig, []byte("not: [a kubeconfig"), 0o600); err != nil {
		t.Fatal(err)
	}

	err := realMain([]string{"--dry-run", "--kubeconfig", badKubeconfig, "--cluster-name", "x"})
	if err == nil || !strings.Contains(err.Error(), "kubeconfig") {
		t.Fatalf("want realMain to stop on the unparseable kubeconfig, got err=%v", err)
	}
	if got := debug.SetMemoryLimit(-1); got != 1073741824 {
		t.Errorf("runtime soft limit after realMain = %d, want 1073741824", got)
	}
}
