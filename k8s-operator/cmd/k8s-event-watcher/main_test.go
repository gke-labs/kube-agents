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
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/prometheus/client_golang/prometheus/testutil"
	"golang.org/x/oauth2"
	container "google.golang.org/api/container/v1"
	"k8s.io/client-go/rest"
	clientcmdapi "k8s.io/client-go/tools/clientcmd/api"
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
func stubGKE(t *testing.T) map[string]error {
	t.Helper()
	failures := map[string]error{}
	describeSaved, tokenSaved := describeCluster, newTokenSource
	t.Cleanup(func() { describeCluster, newTokenSource = describeSaved, tokenSaved })
	describeCluster = func(_ context.Context, id clusterIdentity) (*container.Cluster, error) {
		key := id.Project + "/" + id.Location + "/" + id.Cluster
		if err, bad := failures[key]; bad {
			return nil, err
		}
		return &container.Cluster{
			Endpoint: key + ".example.invalid",
			// A real certificate, even though no TLS session is established here.
			// clientConfigForIdentity puts these bytes in rest.Config.CAData and
			// kubernetes.NewForConfig builds the CertPool eagerly, so arbitrary
			// base64 fails at client construction with "unable to parse bytes as
			// PEM block" and every discovery test reports zero clusters.
			MasterAuth: &container.MasterAuth{ClusterCaCertificate: base64.StdEncoding.EncodeToString(testCAPEM(t))},
		}, nil
	}
	newTokenSource = func(context.Context) (oauth2.TokenSource, error) {
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

func TestDiscoverClusterProfiles_ReadsIdentityNotDirName(t *testing.T) {
	stubGKE(t)
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
	stubGKE(t)
	dir := t.TempDir()
	writeClusterProfile(t, dir, "cluster-p-good-us-central1", "p", "good", "us-central1")
	// "default" and "platform" exist but carry no cluster_identity.
	writeNonClusterProfile(t, dir, "default")
	writeNonClusterProfile(t, dir, "platform")
	// A half-written identity names no cluster the GKE API could be asked
	// about, so it is not a cluster profile either.
	halfHome := filepath.Join(dir, "cluster-p-nolocation")
	if err := os.MkdirAll(halfHome, 0o700); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	if err := os.WriteFile(filepath.Join(halfHome, "config.yaml"),
		[]byte("cluster_identity:\n  project: p\n  cluster: nolocation\n"), 0o600); err != nil {
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
	stubGKE(t)
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
	stubGKE(t)
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
	stubGKE(t)
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

func TestDiscoverClusterProfiles_UndescribableClusterIsSkipped(t *testing.T) {
	// A profile can name a cluster the GKE API will not answer for: deleted
	// between scaffolding and this start, or outside what this pod's identity
	// may read. Guessing an address from the name would produce a watcher
	// reporting events for a control plane nobody confirmed, so drop it and
	// count it — the rest of the fleet is unaffected.
	dir := t.TempDir()
	failures := stubGKE(t)
	failures["p/us-central1/ghost"] = errors.New("clusters.get: 404")
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

func TestClientConfigForIdentity(t *testing.T) {
	ca := base64.StdEncoding.EncodeToString([]byte("ca-bytes"))
	external := &container.DNSEndpointConfig{Endpoint: "gke-abc.us-central1.gke.goog", AllowExternalTraffic: true}
	internal := &container.DNSEndpointConfig{Endpoint: "gke-abc.us-central1.gke.goog"}

	for _, tc := range []struct {
		name    string
		cluster *container.Cluster
		host    string
		ca      string
		wantErr string
	}{{
		// The DNS endpoint terminates on a Google frontend with a WebPKI
		// certificate, so it carries no CA of its own — and it is reachable
		// from pods that cannot route to the IP endpoint, which is why
		// gke_endpoint.py prefers it under exactly this condition.
		name: "external DNS endpoint wins over the IP endpoint",
		cluster: &container.Cluster{
			Endpoint:                    "10.0.0.2",
			MasterAuth:                  &container.MasterAuth{ClusterCaCertificate: ca},
			ControlPlaneEndpointsConfig: &container.ControlPlaneEndpointsConfig{DnsEndpointConfig: external},
		},
		host: "https://gke-abc.us-central1.gke.goog",
	}, {
		// Published but closed to external traffic: reaching it from here
		// would 403, so it is not an address at all.
		name: "a DNS endpoint that refuses external traffic is ignored",
		cluster: &container.Cluster{
			Endpoint:                    "10.0.0.2",
			MasterAuth:                  &container.MasterAuth{ClusterCaCertificate: ca},
			ControlPlaneEndpointsConfig: &container.ControlPlaneEndpointsConfig{DnsEndpointConfig: internal},
		},
		host: "https://10.0.0.2",
		ca:   "ca-bytes",
	}, {
		name:    "IP endpoint with no DNS config",
		cluster: &container.Cluster{Endpoint: "10.0.0.2", MasterAuth: &container.MasterAuth{ClusterCaCertificate: ca}},
		host:    "https://10.0.0.2",
		ca:      "ca-bytes",
	}, {
		// An empty Host would hand rest.Config a relative URL and build a
		// client that talks to nothing without saying so.
		name:    "neither endpoint is an error, not an empty host",
		cluster: &container.Cluster{MasterAuth: &container.MasterAuth{ClusterCaCertificate: ca}},
		wantErr: "neither",
	}, {
		name:    "an IP endpoint with no CA is an error",
		cluster: &container.Cluster{Endpoint: "10.0.0.2"},
		wantErr: "CA certificate",
	}, {
		name:    "no cluster at all",
		wantErr: "no cluster",
	}} {
		t.Run(tc.name, func(t *testing.T) {
			cfg, err := clientConfigForIdentity(tc.cluster)
			if tc.wantErr != "" {
				if err == nil {
					t.Fatalf("expected an error containing %q, got config %+v", tc.wantErr, cfg)
				}
				if !strings.Contains(err.Error(), tc.wantErr) {
					t.Fatalf("error %q does not contain %q", err, tc.wantErr)
				}
				return
			}
			if err != nil {
				t.Fatalf("clientConfigForIdentity: %v", err)
			}
			if cfg.Host != tc.host {
				t.Errorf("host = %q, want %q", cfg.Host, tc.host)
			}
			if got := string(cfg.TLSClientConfig.CAData); got != tc.ca {
				t.Errorf("CA = %q, want %q", got, tc.ca)
			}
			// The credential is attached separately, by useGoogleTokenSource.
			if cfg.BearerToken != "" || cfg.ExecProvider != nil {
				t.Error("clientConfigForIdentity must carry no credential")
			}
		})
	}
}

func TestDiscoverClusterProfiles_MalformedConfigIsSkipped(t *testing.T) {
	stubGKE(t)
	dir := t.TempDir()
	home := filepath.Join(dir, "cluster-broken")
	if err := os.MkdirAll(home, 0o700); err != nil {
		t.Fatalf("mkdir: %v", err)
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

type roundTripperFunc func(*http.Request) (*http.Response, error)

func (f roundTripperFunc) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func TestUseGoogleTokenSource(t *testing.T) {
	// Whatever authentication the config arrived with must be dropped and
	// replaced with a bearer token, while the server address and CA are left
	// alone. The case that matters is a GKE kubeconfig's exec directive: it
	// runs gke-gcloud-auth-plugin, which this image does not ship, so leaving
	// it in place fails at the first request rather than at construction.
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
