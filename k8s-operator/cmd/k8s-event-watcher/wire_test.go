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
	"encoding/base64"
	"encoding/pem"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"

	"golang.org/x/oauth2"
	container "google.golang.org/api/container/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/rest"

	"github.com/gke-labs/kube-agents/k8s-operator/internal/clusterprofiles"
)

// emptyEventListJSON is what the fake API servers below answer every Event
// list with, in JSON: the fallback a server that cannot speak protobuf takes,
// which a client asking for protobuf first must still decode.
const emptyEventListJSON = `{"kind":"EventList","apiVersion":"v1","metadata":{"resourceVersion":"1"},"items":[]}`

// acceptRecordingServer serves an empty Event list as JSON and records the
// Accept header of the last request.
func acceptRecordingServer(t *testing.T, tls bool) (*httptest.Server, *atomic.Value) {
	t.Helper()
	var accept atomic.Value
	h := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		accept.Store(r.Header.Get("Accept"))
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(emptyEventListJSON))
	})
	var srv *httptest.Server
	if tls {
		srv = httptest.NewTLSServer(h)
	} else {
		srv = httptest.NewServer(h)
	}
	t.Cleanup(srv.Close)
	return srv, &accept
}

// The direct client, built by buildKubeClient from a kubeconfig, asks for
// protobuf first too: its last line is newClusterClient, and this is the one
// test that reaches it, since the realMain tests point it at a port where
// nothing listens and nothing records a header. It pins the header, not the
// call: client-go's Event client asks for the same pair when a config names
// no content type, so the test fails when a config built here comes to name
// another, not when the explicit preference alone is removed.
func TestBuildKubeClient_AsksForProtobuf(t *testing.T) {
	srv, accept := acceptRecordingServer(t, false)
	client, err := buildKubeClient(&flags{kubeconfig: writeKubeconfig(t, srv.URL)})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := client.CoreV1().Events(metav1.NamespaceAll).List(context.Background(), metav1.ListOptions{}); err != nil {
		t.Fatalf("list through the direct client: %v", err)
	}
	if got := accept.Load(); got != wireAcceptContentTypes {
		t.Errorf("the direct client's Accept = %q; want %q", got, wireAcceptContentTypes)
	}
}

// The header a config built by newClusterClient sends: protobuf first, JSON
// second, so a control plane serves the compact form and anything in between
// that cannot (a proxy that strips it, an older server) still gets an answer
// the client decodes.
func TestNewClusterClient_AsksForProtobufAndDecodesTheJSONFallback(t *testing.T) {
	srv, accept := acceptRecordingServer(t, false)
	client, err := newClusterClient(&rest.Config{Host: srv.URL})
	if err != nil {
		t.Fatal(err)
	}
	list, err := client.CoreV1().Events(metav1.NamespaceAll).List(context.Background(), metav1.ListOptions{})
	if err != nil {
		t.Fatalf("list against a JSON-only server: %v", err)
	}
	if list.ResourceVersion != "1" {
		t.Errorf("list.ResourceVersion = %q; want the JSON fallback decoded", list.ResourceVersion)
	}
	if got := accept.Load(); got != wireAcceptContentTypes {
		t.Errorf("Accept = %q; want %q", got, wireAcceptContentTypes)
	}
	if wireContentType != "application/vnd.kubernetes.protobuf" || wireAcceptContentTypes != "application/vnd.kubernetes.protobuf,application/json" {
		t.Errorf("content types = %q / %q; the protobuf-first pair is what the credential proxy forwards and the API server negotiates on", wireContentType, wireAcceptContentTypes)
	}
}

// The same header on a client discovery built from a profile: the describe
// names a TLS server standing in for the control plane, with its certificate
// as the cluster CA, and the first request that client makes asks for
// protobuf. This is the path every profile cluster is read through.
func TestDiscoverClusterProfiles_BuiltClientsAskForProtobuf(t *testing.T) {
	srv, accept := acceptRecordingServer(t, true)
	saved := discovery
	t.Cleanup(func() { discovery = saved })
	caPEM := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: srv.Certificate().Raw})
	discovery.Describe = func(_ context.Context, _ clusterprofiles.Identity) (*container.Cluster, error) {
		return &container.Cluster{
			Endpoint:   srv.Listener.Addr().String(),
			MasterAuth: &container.MasterAuth{ClusterCaCertificate: base64.StdEncoding.EncodeToString(caPEM)},
		}, nil
	}
	discovery.TokenSource = func(context.Context) (oauth2.TokenSource, error) {
		return oauth2.StaticTokenSource(&oauth2.Token{AccessToken: "test-token"}), nil
	}
	dir := t.TempDir()
	writeClusterProfile(t, dir, "cluster-a", "proj", "a", "us-central1")
	clusters, err := discoverClusterProfiles(context.Background(), dir, newMetrics())
	if err != nil {
		t.Fatal(err)
	}
	if len(clusters) != 1 {
		t.Fatalf("discovered %d clusters; want 1", len(clusters))
	}
	if _, err := clusters[0].Client.CoreV1().Events(metav1.NamespaceAll).List(context.Background(), metav1.ListOptions{}); err != nil {
		t.Fatalf("list through the discovered client: %v", err)
	}
	if got := accept.Load(); got != wireAcceptContentTypes {
		t.Errorf("Accept from the discovered client = %q; want %q", got, wireAcceptContentTypes)
	}
}

func TestStripManagedFields(t *testing.T) {
	ev := &corev1.Event{ObjectMeta: metav1.ObjectMeta{
		Name:          "e1",
		ManagedFields: []metav1.ManagedFieldsEntry{{Manager: "kubelet"}},
	}}
	out, err := stripManagedFields(ev)
	if err != nil {
		t.Fatal(err)
	}
	if got := out.(*corev1.Event); got.ManagedFields != nil || got.Name != "e1" {
		t.Errorf("stripped event = %+v; want managedFields nil and the rest untouched", got.ObjectMeta)
	}
	// Something the accessor cannot read passes through as it is.
	other := struct{ X int }{X: 1}
	if out, err := stripManagedFields(other); err != nil || out != other {
		t.Errorf("non-object = (%v, %v); want it returned unchanged", out, err)
	}
}
