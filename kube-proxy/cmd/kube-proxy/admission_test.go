package main

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	authenticationv1 "k8s.io/api/authentication/v1"
	"k8s.io/client-go/kubernetes/scheme"
	"k8s.io/client-go/rest"

	"github.com/gke-labs/kube-agents/a2a/taskcred"
	"github.com/gke-labs/kube-agents/k8s-api-proxy/pkg/k8sapiproxy"
	"github.com/gke-labs/kube-agents/k8s-api-proxy/pkg/tokenreview"
	"github.com/gke-labs/kube-agents/kube-proxy/taskgate"
)

const (
	testOwnAudience   = "kubeagents-credential-proxy"
	testUnmapped      = "system:serviceaccount:agents:unmapped-read"
	testOtherCluster  = "gke_proj-a_us-central1_cluster-b"
	testPodsPath      = "/api/v1/namespaces/default/pods"
	testPodPathPrefix = "/api/v1/namespaces/"
	testPodPathInfix  = "/pods/"
	// testPeerIP is the address httptest.NewRequest gives every request's peer.
	testPeerIP      = "192.0.2.1"
	testContentJSON = "application/json"
	testPodList     = `{"kind":"PodList","apiVersion":"v1","metadata":{"resourceVersion":"1"},"items":[]}`
	testRefused     = `{"apiVersion":"authentication.k8s.io/v1","kind":"TokenReview","status":{"authenticated":false}}`
	testTTL         = 10 * time.Minute

	testWorkerToken   = "worker-token"
	testUnmappedToken = "unmapped-token"
	testPlatformToken = "platform-token"
)

// testCaller is what the test API server knows of one ServiceAccount token: who it is,
// the audience it was issued for, and the pod it is bound to.
type testCaller struct {
	username, audience     string
	namespace, pod, podUID string
}

// newTestAPIServer reviews the tokens of callers as the API server does, authenticating a
// token only for an audience the review asks about, and serves each caller's pod at
// testPeerIP, so the proxy's pod binding passes for a request httptest makes.
func newTestAPIServer(t *testing.T, callers map[string]testCaller) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", testContentJSON)
		if r.Method == http.MethodGet {
			for _, c := range callers {
				if r.URL.Path != testPodPathPrefix+c.namespace+testPodPathInfix+c.pod {
					continue
				}
				_ = json.NewEncoder(w).Encode(map[string]any{
					"apiVersion": "v1",
					"kind":       "Pod",
					"metadata":   map[string]any{"name": c.pod, "namespace": c.namespace, "uid": c.podUID},
					"status":     map[string]any{"phase": "Running", "podIP": testPeerIP, "podIPs": []map[string]string{{"ip": testPeerIP}}},
				})
				return
			}
			http.NotFound(w, r)
			return
		}
		body, err := io.ReadAll(r.Body)
		if err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		// The client sends protobuf; this reads either encoding.
		obj, _, err := scheme.Codecs.UniversalDeserializer().Decode(body, nil, nil)
		if err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		review, ok := obj.(*authenticationv1.TokenReview)
		if !ok {
			http.Error(w, "not a TokenReview", http.StatusBadRequest)
			return
		}
		c, known := callers[review.Spec.Token]
		if !known || !slices.Contains(review.Spec.Audiences, c.audience) {
			_, _ = io.WriteString(w, testRefused)
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]any{
			"apiVersion": "authentication.k8s.io/v1",
			"kind":       "TokenReview",
			"status": map[string]any{
				"authenticated": true,
				"audiences":     []string{c.audience},
				"user": map[string]any{
					"username": c.username,
					"extra": map[string][]string{
						"authentication.kubernetes.io/pod-name": {c.pod},
						"authentication.kubernetes.io/pod-uid":  {c.podUID},
					},
				},
			},
		})
	}))
	t.Cleanup(srv.Close)
	return srv
}

// testClusters answers every forwarded request with an empty PodList and keeps the cluster
// each one was sent to.
type testClusters struct {
	mu      sync.Mutex
	reached []string
}

func (u *testClusters) RoundTripCluster(_ context.Context, clusterID string, req *http.Request) (*http.Response, error) {
	u.mu.Lock()
	u.reached = append(u.reached, clusterID)
	u.mu.Unlock()
	return &http.Response{
		StatusCode: http.StatusOK,
		Header:     http.Header{"Content-Type": []string{testContentJSON}},
		Body:       io.NopCloser(strings.NewReader(testPodList)),
		Request:    req,
	}, nil
}

func (u *testClusters) calls() []string {
	u.mu.Lock()
	defer u.mu.Unlock()
	return slices.Clone(u.reached)
}

// With the gate on, the binary's options put a worker behind both checks. The proxy's
// TokenReview authenticator, given the audiences options names, admits a profile-audience
// token only from a ServiceAccount the identity map names, and the gate then holds that
// worker to its task's cluster. A ServiceAccount with a valid profile-audience token that
// the map does not name is refused at authentication and reaches no cluster.
func TestProfileAudienceCallersAreTheGatedWorkers(t *testing.T) {
	const (
		workerPod    = "auditor-read-x7k2p"
		workerPodUID = "0b1c2d3e-4f50-6172-8394-a5b6c7d8e9f0"
	)
	logger := slog.New(slog.DiscardHandler)
	kp, err := taskcred.NewSigningKey()
	if err != nil {
		t.Fatal(err)
	}
	pub, err := kp.PublicKey()
	if err != nil {
		t.Fatal(err)
	}
	opts, err := options(nil, envOf(map[string]string{
		taskgate.IdentitiesEnv:      testIdentityMap,
		taskgate.SigningKeysEnv:     pub,
		taskgate.ProfileAudienceEnv: testAudience,
	}), logger)
	if err != nil {
		t.Fatalf("options: %v", err)
	}

	api := newTestAPIServer(t, map[string]testCaller{
		testWorkerToken:   {testWorker, testAudience, "agents", workerPod, workerPodUID},
		testUnmappedToken: {testUnmapped, testAudience, "agents", "unmapped-read-0", "unmapped-uid"},
		testPlatformToken: {testNonWorker, testOwnAudience, "kubeagents-system", "platform-agent-0", "platform-uid"},
	})
	// The authenticator proxyserver builds: its own audience and callers from the flags, and
	// Options.AudienceCallers beside them.
	authn, err := tokenreview.NewAuthenticator(tokenreview.Config{
		Kubernetes:      &rest.Config{Host: api.URL},
		Audience:        testOwnAudience,
		AllowedCallers:  []string{testNonWorker},
		AudienceCallers: opts.AudienceCallers,
		Logger:          logger,
	})
	if err != nil {
		t.Fatalf("NewAuthenticator: %v", err)
	}
	clusters := &testClusters{}
	h, err := k8sapiproxy.NewHandler(k8sapiproxy.Config{
		Authenticator:      authn,
		Authorize:          opts.Authorize,
		ExtraCallerHeaders: opts.ExtraCallerHeaders,
		Upstream:           clusters,
		Policy:             k8sapiproxy.Policy{Mode: k8sapiproxy.ModeReadOnlyWithDryRun, RedactSecrets: true},
		Logger:             logger,
	})
	if err != nil {
		t.Fatalf("NewHandler: %v", err)
	}

	credential, err := taskcred.Mint(kp, taskcred.Claims{
		Task:     "task-1",
		Profile:  "auditor",
		Tier:     "read",
		Pod:      workerPod,
		PodUID:   workerPodUID,
		Audience: taskcred.AudienceCredentialProxy,
		Expires:  time.Now().Add(testTTL),
		Target:   &taskcred.Target{Project: "proj-a", Location: "us-central1", Cluster: "cluster-a"},
	})
	if err != nil {
		t.Fatalf("Mint: %v", err)
	}

	get := func(token, clusterID, credential string) int {
		req := httptest.NewRequest(http.MethodGet, "/clusters/"+clusterID+testPodsPath, nil)
		req.Header.Set("Authorization", "Bearer "+token)
		if credential != "" {
			req.Header.Set(taskgate.CredentialHeader, credential)
		}
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, req)
		return rec.Code
	}

	for _, tc := range []struct {
		name       string
		token      string
		clusterID  string
		credential string
		want       int
	}{
		{"a worker with no task credential", testWorkerToken, testClusterID, "", http.StatusForbidden},
		{"a worker with its task's credential, on another cluster", testWorkerToken, testOtherCluster, credential, http.StatusForbidden},
		{"a ServiceAccount outside the map", testUnmappedToken, testClusterID, "", http.StatusUnauthorized},
		{"a ServiceAccount outside the map, with a worker's credential", testUnmappedToken, testClusterID, credential, http.StatusUnauthorized},
		{"a token nobody issued", "no-such-token", testClusterID, credential, http.StatusUnauthorized},
	} {
		if got := get(tc.token, tc.clusterID, tc.credential); got != tc.want {
			t.Errorf("%s: code = %d, want %d", tc.name, got, tc.want)
		}
		if reached := clusters.calls(); len(reached) != 0 {
			t.Fatalf("%s: a refused request reached %v", tc.name, reached)
		}
	}

	if got := get(testWorkerToken, testClusterID, credential); got != http.StatusOK {
		t.Fatalf("a worker with its task's credential, on its cluster: code = %d, want 200", got)
	}
	// A caller the proxy's own audience admits is outside the map, and not asked for one.
	if got := get(testPlatformToken, testOtherCluster, ""); got != http.StatusOK {
		t.Fatalf("a caller of the proxy's own audience: code = %d, want 200", got)
	}
	if reached, want := clusters.calls(), []string{testClusterID, testOtherCluster}; !slices.Equal(reached, want) {
		t.Errorf("clusters reached = %v, want %v", reached, want)
	}
}
