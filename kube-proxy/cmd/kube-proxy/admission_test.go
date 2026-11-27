package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"slices"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	authenticationv1 "k8s.io/api/authentication/v1"
	"k8s.io/client-go/kubernetes/scheme"
	"k8s.io/client-go/rest"

	"github.com/gke-labs/kube-agents/a2a/taskcred"
	"github.com/gke-labs/kube-agents/k8s-api-proxy/pkg/k8sapiproxy"
	"github.com/gke-labs/kube-agents/k8s-api-proxy/pkg/proxyserver"
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

	// The server's environment-only settings and its auth mode's variable.
	testSessionAudienceEnv = "CREDENTIAL_PROXY_SESSION_AUDIENCE"
	testSessionCallersEnv  = "CREDENTIAL_PROXY_SESSION_CALLERS"
	testAuthModeEnv        = "KUBE_PROXY_AUTH_MODE"
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

// serverArgs are the flags of a server that reviews caller tokens at apiServerURL, admits
// testNonWorker for testOwnAudience, and forwards the in-cluster route to upstreamURL.
func serverArgs(upstreamURL, apiServerURL string, more ...string) []string {
	return append([]string{
		"--upstream-url=" + upstreamURL,
		"--allowed-callers=" + testNonWorker,
		"--audience=" + testOwnAudience,
		"--kube-api-server=" + apiServerURL,
		"--kube-ca-file=",
		"--kube-token-file=",
		"--listen-addr=127.0.0.1:18443",
	}, more...)
}

// clearServerEnv unsets the variables the server reads beside its flags that these tests
// set or depend on.
func clearServerEnv(t *testing.T) {
	t.Helper()
	for _, name := range []string{testSessionAudienceEnv, testSessionCallersEnv, testAuthModeEnv} {
		t.Setenv(name, "")
	}
}

func testFlagSet() *flag.FlagSet {
	fs := flag.NewFlagSet("kube-proxy", flag.ContinueOnError)
	fs.SetOutput(io.Discard)
	return fs
}

// Through the server itself (proxyserver.NewHandler, the handler Run serves), so that what
// options returns is what the server's authenticator is built from: a profile-audience
// token is admitted only from a ServiceAccount the identity map names, and the worker it
// admits is then met by the gate. The static upstream serves the in-cluster route alone,
// so a worker past the gate gets the upstream's refusal of its cluster, not a 200;
// TestProfileAudienceCallersAreTheGatedWorkers follows an admitted worker to its cluster.
func TestServerAdmitsOnlyTheGatesWorkersForTheProfileAudience(t *testing.T) {
	const (
		workerPod    = "auditor-read-x7k2p"
		workerPodUID = "0b1c2d3e-4f50-6172-8394-a5b6c7d8e9f0"
	)
	clearServerEnv(t)
	logger := slog.New(slog.DiscardHandler)
	kp, err := taskcred.NewSigningKey()
	if err != nil {
		t.Fatal(err)
	}
	pub, err := kp.PublicKey()
	if err != nil {
		t.Fatal(err)
	}
	gateOn := envOf(map[string]string{
		taskgate.IdentitiesEnv:      testIdentityMap,
		taskgate.SigningKeysEnv:     pub,
		taskgate.ProfileAudienceEnv: testAudience,
	})
	api := newTestAPIServer(t, map[string]testCaller{
		testWorkerToken:   {testWorker, testAudience, "agents", workerPod, workerPodUID},
		testUnmappedToken: {testUnmapped, testAudience, "agents", "unmapped-read-0", "unmapped-uid"},
		testPlatformToken: {testNonWorker, testOwnAudience, "kubeagents-system", "platform-agent-0", "platform-uid"},
	})
	var forwarded atomic.Int32
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		forwarded.Add(1)
		w.Header().Set("Content-Type", testContentJSON)
		_, _ = io.WriteString(w, testPodList)
	}))
	t.Cleanup(upstream.Close)
	args := serverArgs(upstream.URL, api.URL)

	build := func(getenv func(string) string) http.Handler {
		opts, err := options(nil, getenv, logger)
		if err != nil {
			t.Fatalf("options: %v", err)
		}
		h, err := proxyserver.NewHandler(context.Background(), testFlagSet(), args, opts)
		if err != nil {
			t.Fatalf("NewHandler: %v", err)
		}
		return h
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
	get := func(h http.Handler, token, path, credential string) int {
		req := httptest.NewRequest(http.MethodGet, path, nil)
		req.Header.Set("Authorization", "Bearer "+token)
		if credential != "" {
			req.Header.Set(taskgate.CredentialHeader, credential)
		}
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, req)
		return rec.Code
	}
	workerPath := "/clusters/" + testClusterID + testPodsPath

	// Gate off: the server accepts no profile audience, whoever presents it.
	off := build(envOf(map[string]string{taskgate.ProfileAudienceEnv: testAudience}))
	for _, token := range []string{testWorkerToken, testUnmappedToken} {
		if got := get(off, token, workerPath, credential); got != http.StatusUnauthorized {
			t.Errorf("gate off, %s: code = %d, want 401", token, got)
		}
	}
	if got := get(off, testPlatformToken, testPodsPath, ""); got != http.StatusOK {
		t.Fatalf("gate off, a caller of the proxy's own audience: code = %d, want 200", got)
	}

	on := build(gateOn)
	before := forwarded.Load()
	for _, tc := range []struct {
		name       string
		token      string
		credential string
		want       int
	}{
		{"a ServiceAccount outside the map", testUnmappedToken, "", http.StatusUnauthorized},
		{"a ServiceAccount outside the map, with a worker's credential", testUnmappedToken, credential, http.StatusUnauthorized},
		{"a worker with no task credential", testWorkerToken, "", http.StatusForbidden},
	} {
		if got := get(on, tc.token, workerPath, tc.credential); got != tc.want {
			t.Errorf("%s: code = %d, want %d", tc.name, got, tc.want)
		}
	}
	// Past authentication and past the gate: what is left to refuse it is the upstream.
	if got := get(on, testWorkerToken, workerPath, credential); got == http.StatusUnauthorized || got == http.StatusForbidden || got == http.StatusOK {
		t.Errorf("a worker with its task's credential: code = %d, want the static upstream's refusal of the cluster", got)
	}
	if got := forwarded.Load(); got != before {
		t.Errorf("the upstream got %d requests from workers and refused callers", got-before)
	}
	if got := get(on, testPlatformToken, testPodsPath, ""); got != http.StatusOK {
		t.Errorf("gate on, a caller of the proxy's own audience: code = %d, want 200", got)
	}
}

// The server's refusal of the gate's workers reaches the operator naming the two variables
// they set, with the server's own text kept.
func TestStartupErrorNamesTheGatesVariables(t *testing.T) {
	logger := slog.New(slog.DiscardHandler)
	gateOn := envOf(map[string]string{taskgate.IdentitiesEnv: testIdentityMap, taskgate.ProfileAudienceEnv: testAudience})
	const unused = "http://127.0.0.1:18080"

	for _, tc := range []struct {
		name string
		args []string
		env  map[string]string
		want string
	}{
		{name: "auth mode none", args: []string{"--auth-mode=none", "--listen-addr=127.0.0.1:18443", "--upstream-url=" + unused}, want: "--auth-mode=none"},
		{
			name: "auth mode jwt alone",
			args: []string{"--auth-mode=jwt", "--upstream-url=" + unused, "--jwt-issuer=https://issuer.test", "--jwt-audiences=kube-proxy"},
			want: `requires "token-review"`,
		},
		{name: "the proxy's own audience", args: serverArgs(unused, unused, "--audience="+testAudience), want: "already configured by --audience"},
		{
			name: "the session audience",
			args: serverArgs(unused, unused),
			env:  map[string]string{testSessionAudienceEnv: testAudience, testSessionCallersEnv: testNonWorker + "-session"},
			want: "already configured by " + testSessionAudienceEnv,
		},
		{name: "a worker in the proxy's own callers", args: serverArgs(unused, unused, "--allowed-callers="+testNonWorker+","+testWorker), want: "already admits"},
		{
			name: "a worker in the session callers",
			args: serverArgs(unused, unused),
			env:  map[string]string{testSessionAudienceEnv: "session-audience", testSessionCallersEnv: testWorker},
			want: "which " + testSessionCallersEnv + " already admits",
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			clearServerEnv(t)
			for name, value := range tc.env {
				t.Setenv(name, value)
			}
			opts, err := options(nil, gateOn, logger)
			if err != nil {
				t.Fatalf("options: %v", err)
			}
			_, serverErr := proxyserver.NewHandler(context.Background(), testFlagSet(), tc.args, opts)
			if serverErr == nil {
				t.Fatal("the server built a handler, want it to refuse the gate's workers")
			}
			got := startupError(serverErr, opts)
			for _, want := range []string{taskgate.ProfileAudienceEnv, taskgate.IdentitiesEnv, serverErr.Error(), tc.want} {
				if !strings.Contains(got.Error(), want) {
					t.Errorf("startup error = %q, want it to contain %q", got, want)
				}
			}
			if !errors.Is(got, serverErr) {
				t.Errorf("startup error %q does not wrap the server's", got)
			}

			// The same flags and environment start with the gate off.
			stock, err := options(nil, envOf(nil), logger)
			if err != nil {
				t.Fatalf("options: %v", err)
			}
			if _, err := proxyserver.NewHandler(context.Background(), testFlagSet(), tc.args, stock); err != nil {
				t.Errorf("gate off: NewHandler: %v", err)
			}
		})
	}

	// Errors that are not about the gate's workers are the server's own, gate on or off.
	opts, err := options(nil, gateOn, logger)
	if err != nil {
		t.Fatalf("options: %v", err)
	}
	other := errors.New("failed to load the TLS server certificate")
	if got := startupError(other, opts); got != other {
		t.Errorf("startupError(%q) = %q, want it unchanged", other, got)
	}
	refused := &proxyserver.AudienceCallersError{Reason: "refused"}
	if got := startupError(refused, proxyserver.Options{}); got != error(refused) {
		t.Errorf("startupError with no gate audience = %q, want the server's error unchanged", got)
	}
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
	// Options.AudienceCallers beside them. That the server hands the option to its
	// authenticator is TestServerAdmitsOnlyTheGatesWorkersForTheProfileAudience's to show;
	// this test needs an upstream that serves a task's cluster, which the server's flags
	// offer only against real clusters.
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
