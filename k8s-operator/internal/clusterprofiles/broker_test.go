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
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"

	"golang.org/x/oauth2"
	container "google.golang.org/api/container/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"
)

// fakeBroker stands in for the credential proxy: it records every request's
// method, path and Authorization header, and answers the GKE describe route
// with a Cluster and everything else with an empty EventList.
type fakeBroker struct {
	mu        sync.Mutex
	requests  []string // "METHOD path"
	auths     []string
	autopilot map[string]bool // cluster name -> Autopilot.Enabled
	// unreachable names clusters whose describe carries no usable endpoint:
	// no external DNS endpoint, and an IP endpoint with no CA.
	unreachable map[string]bool
	status      int // non-zero answers every request with it
}

func (f *fakeBroker) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	f.requests = append(f.requests, r.Method+" "+r.URL.Path)
	f.auths = append(f.auths, r.Header.Get("Authorization"))
	status := f.status
	f.mu.Unlock()
	if status != 0 {
		http.Error(w, `{"error":{"code":403,"message":"denied"}}`, status)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	const gkePrefix = "/v1/watcher/gcp/container.googleapis.com/v1/projects/"
	if strings.HasPrefix(r.URL.Path, gkePrefix) {
		name := r.URL.Path[strings.LastIndex(r.URL.Path, "/")+1:]
		cluster := &container.Cluster{
			Name:      name,
			Endpoint:  "10.0.0.1", // must be ignored in broker mode
			Autopilot: &container.Autopilot{Enabled: f.autopilot[name]},
		}
		if !f.unreachable[name] {
			// Usable addressing, which discovery checks and the broker's
			// config then ignores: the broker resolves the control plane.
			cluster.ControlPlaneEndpointsConfig = &container.ControlPlaneEndpointsConfig{
				DnsEndpointConfig: &container.DNSEndpointConfig{
					AllowExternalTraffic: true,
					Endpoint:             name + ".example.internal",
				},
			}
		}
		_ = json.NewEncoder(w).Encode(cluster)
		return
	}
	_, _ = w.Write([]byte(`{"kind":"EventList","apiVersion":"v1","metadata":{},"items":[]}`))
}

// seen returns copies of the recorded requests and Authorization headers,
// under the lock the handler writes them with.
func (f *fakeBroker) seen() (requests, auths []string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]string(nil), f.requests...), append([]string(nil), f.auths...)
}

// newFakeBroker starts a fakeBroker and returns it with a Broker pointed at
// it, whose token file holds "token-1". ADC is made unusable for the test, so
// a broker-mode path that fell back to it would fail rather than quietly reach
// Google with the developer's own credentials.
func newFakeBroker(t *testing.T) (*fakeBroker, *Broker) {
	t.Helper()
	t.Setenv("GOOGLE_APPLICATION_CREDENTIALS", filepath.Join(t.TempDir(), "no-such-adc.json"))
	fb := &fakeBroker{autopilot: map[string]bool{}, unreachable: map[string]bool{}}
	srv := httptest.NewServer(fb)
	t.Cleanup(srv.Close)
	tokenFile := filepath.Join(t.TempDir(), "token")
	writeToken(t, tokenFile, "token-1")
	return fb, &Broker{URL: srv.URL, TokenFile: tokenFile}
}

func writeToken(t *testing.T, path, token string) {
	t.Helper()
	if err := os.WriteFile(path, []byte(token+"\n"), 0o600); err != nil {
		t.Fatalf("write token: %v", err)
	}
}

func TestBrokerFromEnv(t *testing.T) {
	cases := []struct {
		name, url, file string
		want            *Broker
		wantErr         bool
	}{
		{name: "neither set is Workload Identity", want: nil},
		{name: "both set", url: "http://proxy.ns.svc:8765", file: "/var/run/tok",
			want: &Broker{URL: "http://proxy.ns.svc:8765", TokenFile: "/var/run/tok"}},
		{name: "trailing slash is dropped", url: "http://proxy.ns.svc:8765/", file: "/var/run/tok",
			want: &Broker{URL: "http://proxy.ns.svc:8765", TokenFile: "/var/run/tok"}},
		{name: "url alone is an error", url: "http://proxy.ns.svc:8765", wantErr: true},
		{name: "token file alone is an error", file: "/var/run/tok", wantErr: true},
		{name: "not a URL", url: "proxy.ns.svc:8765", file: "/var/run/tok", wantErr: true},
		{name: "wrong scheme", url: "ftp://proxy.ns.svc", file: "/var/run/tok", wantErr: true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Setenv(BrokerURLEnv, tc.url)
			t.Setenv(BrokerTokenFileEnv, tc.file)
			got, err := BrokerFromEnv()
			if tc.wantErr {
				if err == nil {
					t.Fatalf("BrokerFromEnv() = %+v, want an error", got)
				}
				return
			}
			if err != nil {
				t.Fatalf("BrokerFromEnv() error: %v", err)
			}
			if (got == nil) != (tc.want == nil) || (got != nil && *got != *tc.want) {
				t.Errorf("BrokerFromEnv() = %+v, want %+v", got, tc.want)
			}
		})
	}
}

// TestDiscoverThroughBroker is the broker mode end to end: the describe goes to
// the proxy's GKE route with the projected token, the config addresses the
// proxy's route for the cluster rather than the endpoint the describe reported,
// the Autopilot bit still travels, and no Google token is minted.
func TestDiscoverThroughBroker(t *testing.T) {
	fb, broker := newFakeBroker(t)
	fb.autopilot["auto"] = true
	dir := t.TempDir()
	writeClusterProfile(t, dir, "cluster-a", "proj", "auto", "us-central1")
	writeClusterProfile(t, dir, "cluster-b", "proj", "std", "europe-west1-b")

	skips := map[string]error{}
	d := Discoverer{
		Broker: broker,
		TokenSource: func(context.Context) (oauth2.TokenSource, error) {
			t.Error("TokenSource called in broker mode")
			return nil, nil
		},
		OnSkip: func(p string, err error) { skips[p] = err },
	}
	clusters, err := d.Discover(context.Background(), dir)
	if err != nil {
		t.Fatalf("Discover: %v", err)
	}
	if len(skips) != 0 || len(clusters) != 2 {
		t.Fatalf("got %d clusters, skips %v; want 2 and none", len(clusters), skips)
	}

	requests, auths := fb.seen()
	wantRequests := []string{
		"GET /v1/watcher/gcp/container.googleapis.com/v1/projects/proj/locations/us-central1/clusters/auto",
		"GET /v1/watcher/gcp/container.googleapis.com/v1/projects/proj/locations/europe-west1-b/clusters/std",
	}
	if strings.Join(requests, "\n") != strings.Join(wantRequests, "\n") {
		t.Errorf("requests:\n%s\nwant:\n%s", strings.Join(requests, "\n"), strings.Join(wantRequests, "\n"))
	}
	for i, a := range auths {
		if a != "Bearer token-1" {
			t.Errorf("request %d Authorization = %q, want the token file's contents", i, a)
		}
	}

	byProfile := map[string]Cluster{}
	for _, c := range clusters {
		byProfile[c.Profile] = c
	}
	for profile, want := range map[string]struct {
		host      string
		autopilot bool
	}{
		"cluster-a": {broker.URL + "/v1/k8s/proj/us-central1/auto", true},
		"cluster-b": {broker.URL + "/v1/k8s/proj/europe-west1-b/std", false},
	} {
		c := byProfile[profile]
		if c.Config.Host != want.host {
			t.Errorf("%s Host = %q, want %q", profile, c.Config.Host, want.host)
		}
		if c.Config.BearerTokenFile != broker.TokenFile || c.Config.BearerToken != "" {
			t.Errorf("%s credential = file %q token %q, want the projected token file only", profile, c.Config.BearerTokenFile, c.Config.BearerToken)
		}
		if len(c.Config.CAData) != 0 || c.Config.WrapTransport != nil {
			t.Errorf("%s carries a CA or a wrapped (Google token) transport in broker mode", profile)
		}
		if c.Autopilot != want.autopilot {
			t.Errorf("%s Autopilot = %v, want %v", profile, c.Autopilot, want.autopilot)
		}
	}
}

// TestDiscoverThroughBrokerSkipsAClusterWithNoUsableEndpoint keeps the skip the
// direct path has: the broker refuses every request for a cluster whose describe
// gives no usable endpoint, so discovery reports it to OnSkip (the discovery
// error counter) rather than returning a cluster whose informer never syncs.
func TestDiscoverThroughBrokerSkipsAClusterWithNoUsableEndpoint(t *testing.T) {
	fb, broker := newFakeBroker(t)
	fb.unreachable["dark"] = true
	dir := t.TempDir()
	writeClusterProfile(t, dir, "cluster-dark", "proj", "dark", "us-central1")
	writeClusterProfile(t, dir, "cluster-ok", "proj", "ok", "us-central1")

	skips := map[string]error{}
	d := Discoverer{Broker: broker, OnSkip: func(p string, err error) { skips[p] = err }}
	clusters, err := d.Discover(context.Background(), dir)
	if err != nil {
		t.Fatalf("Discover: %v", err)
	}
	if len(clusters) != 1 || clusters[0].Profile != "cluster-ok" {
		t.Fatalf("clusters = %v, want only cluster-ok", clusters)
	}
	if skips["cluster-dark"] == nil || len(skips) != 1 {
		t.Fatalf("skips = %v, want cluster-dark alone", skips)
	}
}

// TestBrokerRereadsTheTokenFile covers the kubelet rotating the projected
// token in place: the next request carries the new one, with no restart.
func TestBrokerRereadsTheTokenFile(t *testing.T) {
	fb, broker := newFakeBroker(t)
	id := Identity{Project: "p", Location: "l", Cluster: "c"}
	if _, err := broker.describeCluster(context.Background(), id); err != nil {
		t.Fatalf("first describe: %v", err)
	}
	writeToken(t, broker.TokenFile, "token-2")
	if _, err := broker.describeCluster(context.Background(), id); err != nil {
		t.Fatalf("second describe: %v", err)
	}
	_, auths := fb.seen()
	if got := strings.Join(auths, ","); got != "Bearer token-1,Bearer token-2" {
		t.Errorf("Authorization headers = %s, want token-1 then token-2", got)
	}

	writeToken(t, broker.TokenFile, "")
	if _, err := broker.describeCluster(context.Background(), id); err == nil {
		t.Error("describe with an empty token file succeeded; want an error rather than an unauthenticated request")
	}
	if requests, _ := fb.seen(); len(requests) != 2 {
		t.Errorf("an empty token file still reached the proxy (%d requests)", len(requests))
	}
}

// TestBrokerClientConfigKeepsThePathPrefix checks client-go keeps Host's path
// as a prefix (rest.DefaultServerURL), so an events list lands on the proxy's
// route for the cluster and not on the proxy's root.
func TestBrokerClientConfigKeepsThePathPrefix(t *testing.T) {
	fb, broker := newFakeBroker(t)
	cfg, err := broker.ClientConfig(Identity{Project: "p", Location: "l", Cluster: "c"})
	if err != nil {
		t.Fatalf("ClientConfig: %v", err)
	}
	client, err := kubernetes.NewForConfig(cfg)
	if err != nil {
		t.Fatalf("NewForConfig: %v", err)
	}
	if _, err := client.CoreV1().Events("").List(context.Background(), metav1.ListOptions{}); err != nil {
		t.Fatalf("list events: %v", err)
	}
	requests, auths := fb.seen()
	if len(requests) != 1 || requests[0] != "GET /v1/k8s/p/l/c/api/v1/events" {
		t.Fatalf("requests = %v, want [GET /v1/k8s/p/l/c/api/v1/events]", requests)
	}
	if auths[0] != "Bearer token-1" {
		t.Errorf("Authorization = %q, want the token file's contents", auths[0])
	}
}

func TestDiscoverThroughBrokerSkips(t *testing.T) {
	cases := []struct {
		name     string
		location string
		status   int
		wantSkip string
		wantReqs int
	}{
		{name: "proxy refuses the describe", location: "us-central1", status: http.StatusForbidden,
			wantSkip: "through the credential proxy", wantReqs: 1},
		{name: "identity that would escape its path segment", location: "us/../../v1/other",
			wantSkip: "cannot be a proxy path segment", wantReqs: 0},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			fb, broker := newFakeBroker(t)
			fb.status = tc.status
			dir := t.TempDir()
			writeClusterProfile(t, dir, "cluster-x", "proj", "x", tc.location)
			skips := map[string]error{}
			d := Discoverer{Broker: broker, OnSkip: func(p string, err error) { skips[p] = err }}
			clusters, err := d.Discover(context.Background(), dir)
			if err != nil {
				t.Fatalf("Discover: %v", err)
			}
			if len(clusters) != 0 {
				t.Errorf("got %d clusters, want the profile skipped", len(clusters))
			}
			if err := skips["cluster-x"]; err == nil || !strings.Contains(err.Error(), tc.wantSkip) {
				t.Errorf("skip = %v, want one mentioning %q", err, tc.wantSkip)
			}
			if strings.Contains(skips["cluster-x"].Error(), "no Google credentials") {
				t.Errorf("broker-mode skip blames Google credentials: %v", skips["cluster-x"])
			}
			if requests, _ := fb.seen(); len(requests) != tc.wantReqs {
				t.Errorf("proxy saw %d requests, want %d: %v", len(requests), tc.wantReqs, requests)
			}
		})
	}
}
