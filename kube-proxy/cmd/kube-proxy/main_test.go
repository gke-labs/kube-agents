package main

import (
	"context"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"slices"
	"strings"
	"sync"
	"testing"

	"github.com/gke-labs/kube-agents/k8s-api-proxy/pkg/k8sapiproxy"
	"github.com/gke-labs/kube-agents/kube-proxy/taskgate"
)

const (
	testWorker      = "system:serviceaccount:agents:auditor-read"
	testNonWorker   = "system:serviceaccount:kubeagents-system:platform-agent"
	testIdentityMap = `{"` + testWorker + `": {"profile": "auditor", "tier": "read"}}`
	testBadMap      = `{"alice": {"profile": "auditor", "tier": "read"}}`
	testClusterID   = "gke_proj-a_us-central1_cluster-a"
	testAudience    = "kubeagents-profile-audience"
	testSecondUser  = "system:serviceaccount:agents:fixer-write"
	testTwoWorkers  = `{"` + testSecondUser + `": {"profile": "fixer", "tier": "write"}, "` + testWorker + `": {"profile": "auditor", "tier": "read"}}`
)

const (
	// testUsernameHeader names a test request's caller and testHoldHeader marks one the
	// upstream keeps open; the proxy strips neither.
	testUsernameHeader   = "X-Test-Username"
	testHoldHeader       = "X-Test-Hold"
	testRetryAfterHeader = "Retry-After"
	testWatchQuery       = "?watch=true"
	testPodHeader        = "X-Test-Pod"
	// testPodNameExtra is the TokenReview extra that names a caller's pod.
	testPodNameExtra = "authentication.kubernetes.io/pod-name"
)

func envOf(values map[string]string) func(string) string {
	return func(name string) string { return values[name] }
}

// sameLimit reports whether got is the limit want, where a negative want is any negative.
func sameLimit(got, want int) bool {
	if want < 0 {
		return got < 0
	}
	return got == want
}

func requestFrom(username string) k8sapiproxy.AuthorizeAttributes {
	return k8sapiproxy.AuthorizeAttributes{
		Request:   httptest.NewRequest(http.MethodGet, "/clusters/"+testClusterID+"/api/v1/pods", nil),
		Caller:    &k8sapiproxy.CallerIdentity{Username: username},
		ClusterID: testClusterID,
	}
}

func TestAsksForVersionOrHelp(t *testing.T) {
	for _, args := range [][]string{
		{"--version"},
		{"-version"},
		{"--version=true"},
		{"-version=1"},
		{"--mode", "read-only", "--version"},
		{"-h"},
		{"--help"},
		{"-help"},
		{"--listen-addr=:8443", "-h"},
	} {
		if !asksForVersionOrHelp(args) {
			t.Errorf("asksForVersionOrHelp(%q) = false, want true", args)
		}
	}
	for _, args := range [][]string{
		nil,
		{},
		{"--mode=read-only"},
		{"--version=false"},
		{"-version=0"},
		{"--version=maybe"},
		{"version"},
		{"help"},
		{"--", "--version"},
		{"--", "-h"},
		{"--versions"},
		{"--helpful"},
		{"---version"},
	} {
		if asksForVersionOrHelp(args) {
			t.Errorf("asksForVersionOrHelp(%q) = true, want false", args)
		}
	}
}

func TestOptions(t *testing.T) {
	logger := slog.New(slog.DiscardHandler)
	version = "test-build"
	t.Cleanup(func() { version = "" })

	t.Run("the gate is off without an identity map", func(t *testing.T) {
		opts, err := options(nil, envOf(nil), logger)
		if err != nil {
			t.Fatalf("options: %v", err)
		}
		if opts.Authorize != nil {
			t.Error("Authorize is set with no identity map, want the stock server's nil")
		}
		if opts.Version != "test-build" || !slices.Equal(opts.ExtraCallerHeaders, taskgate.CallerHeaders()) {
			t.Errorf("options = (version %q, headers %v), want the build's version and %v", opts.Version, opts.ExtraCallerHeaders, taskgate.CallerHeaders())
		}
		if opts.AudienceCallers != nil {
			t.Errorf("AudienceCallers = %v with no identity map, want the stock server's nil", opts.AudienceCallers)
		}
		if opts.CallerLimits != nil {
			t.Error("CallerLimits is set with no identity map, want the stock server's nil")
		}
	})

	t.Run("worker limits without workers are not read", func(t *testing.T) {
		for name, env := range map[string]map[string]string{
			"limits alone":           {taskgate.WorkerMaxInFlightEnv: "2", taskgate.WorkerMaxLongRunningEnv: "2"},
			"malformed limits alone": {taskgate.WorkerMaxInFlightEnv: "many", taskgate.WorkerMaxLongRunningEnv: "-1"},
			"an empty map":           {taskgate.IdentitiesEnv: "{}", taskgate.WorkerMaxInFlightEnv: "many"},
		} {
			opts, err := options(nil, envOf(env), logger)
			if err != nil {
				t.Errorf("%s: options: %v", name, err)
				continue
			}
			if opts.CallerLimits != nil || opts.Authorize != nil {
				t.Errorf("%s: options = (limits set=%v, gate set=%v), want the stock server's", name, opts.CallerLimits != nil, opts.Authorize != nil)
			}
		}
	})

	t.Run("the gate's workers are limited and nobody else", func(t *testing.T) {
		gate := map[string]string{taskgate.IdentitiesEnv: testTwoWorkers, taskgate.ProfileAudienceEnv: testAudience}
		defaults := [4]int{taskgate.DefaultWorkerMaxInFlight, taskgate.DefaultWorkerMaxLongRunning, taskgate.DefaultWorkerGroupMaxInFlight, taskgate.DefaultWorkerGroupMaxLongRunning}
		for name, tc := range map[string]struct {
			env map[string]string
			// want is the pod's two limits and the group's two; -1 stands for any negative.
			want [4]int
		}{
			"the defaults": {nil, defaults},
			"blank values": {map[string]string{taskgate.WorkerMaxInFlightEnv: " ", taskgate.WorkerGroupMaxLongRunningEnv: ""}, defaults},
			"set values": {map[string]string{
				taskgate.WorkerMaxInFlightEnv: " 4\n", taskgate.WorkerMaxLongRunningEnv: "2",
				taskgate.WorkerGroupMaxInFlightEnv: "12", taskgate.WorkerGroupMaxLongRunningEnv: "6",
			}, [4]int{4, 2, 12, 6}},
			// Negative, so that --max-in-flight-per-caller does not limit a worker instead.
			"zero is no limit": {map[string]string{
				taskgate.WorkerMaxInFlightEnv: "0", taskgate.WorkerMaxLongRunningEnv: "0",
				taskgate.WorkerGroupMaxInFlightEnv: "0", taskgate.WorkerGroupMaxLongRunningEnv: "0",
			}, [4]int{-1, -1, 0, 0}},
		} {
			env := map[string]string{}
			for k, v := range gate {
				env[k] = v
			}
			for k, v := range tc.env {
				env[k] = v
			}
			opts, err := options(nil, envOf(env), logger)
			if err != nil {
				t.Errorf("%s: options: %v", name, err)
				continue
			}
			if opts.CallerLimits == nil {
				t.Errorf("%s: CallerLimits is nil with the gate on", name)
				continue
			}
			var groups, keys []string
			for _, worker := range []string{testWorker, testSecondUser} {
				got := opts.CallerLimits(&k8sapiproxy.CallerIdentity{Username: worker, Extra: map[string][]string{testPodNameExtra: {"pod-0"}}})
				if !sameLimit(got.MaxInFlight, tc.want[0]) || !sameLimit(got.MaxLongRunning, tc.want[1]) ||
					got.GroupMaxInFlight != tc.want[2] || got.GroupMaxLongRunning != tc.want[3] {
					t.Errorf("%s: limits of %s = %+v, want %v", name, worker, got, tc.want)
				}
				groups, keys = append(groups, got.Group), append(keys, got.Key)
			}
			if groups[0] == "" || groups[0] != groups[1] {
				t.Errorf("%s: groups = %q, want the two workers in one named group", name, groups)
			}
			if keys[0] == "" || keys[1] == "" || keys[0] == keys[1] {
				t.Errorf("%s: keys = %q, want one for each worker's pod", name, keys)
			}
			if got := opts.CallerLimits(&k8sapiproxy.CallerIdentity{Username: testNonWorker}); got != (k8sapiproxy.CallerLimits{}) {
				t.Errorf("%s: limits of a caller outside the map = %+v, want none", name, got)
			}
		}
	})

	t.Run("a profile audience without workers is not accepted", func(t *testing.T) {
		for name, env := range map[string]map[string]string{
			"no map":       {taskgate.ProfileAudienceEnv: testAudience},
			"a blank map":  {taskgate.ProfileAudienceEnv: testAudience, taskgate.IdentitiesEnv: " "},
			"an empty map": {taskgate.ProfileAudienceEnv: testAudience, taskgate.IdentitiesEnv: "{}"},
		} {
			opts, err := options(nil, envOf(env), logger)
			if err != nil {
				t.Errorf("%s: options: %v", name, err)
				continue
			}
			if opts.Authorize != nil || opts.AudienceCallers != nil {
				t.Errorf("%s: options = (gate set=%v, audiences %v), want the stock server's", name, opts.Authorize != nil, opts.AudienceCallers)
			}
		}
	})

	t.Run("an identity map turns the gate on", func(t *testing.T) {
		opts, err := options(nil, envOf(map[string]string{taskgate.IdentitiesEnv: testIdentityMap, taskgate.ProfileAudienceEnv: testAudience}), logger)
		if err != nil {
			t.Fatalf("options: %v", err)
		}
		if opts.Authorize == nil {
			t.Fatal("Authorize is nil with an identity map set")
		}
		if len(opts.AudienceCallers) != 1 || !slices.Equal(opts.AudienceCallers[testAudience], []string{testWorker}) {
			t.Errorf("AudienceCallers = %v, want %s admitting the map's worker alone", opts.AudienceCallers, testAudience)
		}
		if err := opts.Authorize(requestFrom(testWorker)); err == nil {
			t.Error("a worker with no credential was admitted")
		}
		if err := opts.Authorize(requestFrom(testNonWorker)); err != nil {
			t.Errorf("a caller outside the map was refused: %v", err)
		}
		if !slices.Equal(opts.ExtraCallerHeaders, taskgate.CallerHeaders()) {
			t.Errorf("headers = %v, want %v", opts.ExtraCallerHeaders, taskgate.CallerHeaders())
		}
	})

	t.Run("the profile audience admits every worker of the map and nobody else", func(t *testing.T) {
		opts, err := options(nil, envOf(map[string]string{taskgate.IdentitiesEnv: testTwoWorkers, taskgate.ProfileAudienceEnv: " " + testAudience + "\n"}), logger)
		if err != nil {
			t.Fatalf("options: %v", err)
		}
		if len(opts.AudienceCallers) != 1 || !slices.Equal(opts.AudienceCallers[testAudience], []string{testWorker, testSecondUser}) {
			t.Errorf("AudienceCallers = %v, want %s admitting the map's two workers, sorted", opts.AudienceCallers, testAudience)
		}
		for _, username := range opts.AudienceCallers[testAudience] {
			if err := opts.Authorize(requestFrom(username)); err == nil {
				t.Errorf("%s is admitted for the profile audience and passed the gate with no credential", username)
			}
		}
	})

	// A malformed map, and workers with no audience to admit them for.
	malformed := map[string]map[string]string{
		"a malformed map":     {taskgate.IdentitiesEnv: testBadMap, taskgate.ProfileAudienceEnv: testAudience},
		"no profile audience": {taskgate.IdentitiesEnv: testIdentityMap},
		"a blank audience":    {taskgate.IdentitiesEnv: testIdentityMap, taskgate.ProfileAudienceEnv: "  "},
		"a worded limit":      {taskgate.IdentitiesEnv: testIdentityMap, taskgate.ProfileAudienceEnv: testAudience, taskgate.WorkerMaxInFlightEnv: "many"},
		"a worded group":      {taskgate.IdentitiesEnv: testIdentityMap, taskgate.ProfileAudienceEnv: testAudience, taskgate.WorkerGroupMaxInFlightEnv: "many"},
		"a negative limit":    {taskgate.IdentitiesEnv: testIdentityMap, taskgate.ProfileAudienceEnv: testAudience, taskgate.WorkerMaxLongRunningEnv: "-1"},
	}

	t.Run("a malformed gate stops a serving invocation", func(t *testing.T) {
		for name, env := range malformed {
			for _, args := range [][]string{nil, {"--mode=read-only"}, {"--version=false"}, {"--", "--version"}} {
				if opts, err := options(args, envOf(env), logger); err == nil {
					t.Errorf("%s: options(%q) = %+v, want an error", name, args, opts)
				}
			}
		}
	})

	t.Run("a malformed gate does not stop --version or --help, and refuses everyone", func(t *testing.T) {
		for name, env := range malformed {
			for _, args := range [][]string{{"--version"}, {"-h"}, {"--help"}} {
				opts, err := options(args, envOf(env), logger)
				if err != nil {
					t.Errorf("%s: options(%q): %v", name, args, err)
					continue
				}
				if opts.Authorize == nil {
					t.Errorf("%s: options(%q): Authorize is nil, so a server that started would be ungated", name, args)
					continue
				}
				if opts.AudienceCallers != nil {
					t.Errorf("%s: options(%q): AudienceCallers = %v, want no audience from a gate that did not load", name, args, opts.AudienceCallers)
				}
				if opts.CallerLimits != nil {
					t.Errorf("%s: options(%q): CallerLimits is set by a gate that did not load", name, args)
				}
				if opts.Version != "test-build" || !slices.Equal(opts.ExtraCallerHeaders, taskgate.CallerHeaders()) {
					t.Errorf("%s: options(%q) = (version %q, headers %v), want the build's version and %v", name, args, opts.Version, opts.ExtraCallerHeaders, taskgate.CallerHeaders())
				}
				for _, username := range []string{testWorker, testNonWorker} {
					if err := opts.Authorize(requestFrom(username)); err == nil {
						t.Errorf("%s: options(%q): %s was admitted by a binary whose gate did not load", name, args, username)
					}
				}
			}
		}
	})
}

// podHeaderAuthenticator admits a request as the caller its testUsernameHeader names, in
// the pod its testPodHeader names, which it reports as a TokenReview does.
type podHeaderAuthenticator struct{}

func (podHeaderAuthenticator) AuthenticateRequest(r *http.Request) (*k8sapiproxy.CallerIdentity, error) {
	return &k8sapiproxy.CallerIdentity{
		Username: r.Header.Get(testUsernameHeader),
		Extra:    map[string][]string{testPodNameExtra: {r.Header.Get(testPodHeader)}},
	}, nil
}

// heldCluster keeps the response of a request marked with testHoldHeader open until
// release, and says on entered that the request arrived.
type heldCluster struct {
	entered chan struct{}
	gate    chan struct{}
}

func (u *heldCluster) RoundTripCluster(_ context.Context, _ string, req *http.Request) (*http.Response, error) {
	var body io.ReadCloser = io.NopCloser(strings.NewReader(testPodList))
	if req.Header.Get(testHoldHeader) != "" {
		pr, pw := io.Pipe()
		go func() {
			<-u.gate
			_ = pw.Close()
		}()
		body = pr
		u.entered <- struct{}{}
	}
	return &http.Response{
		StatusCode: http.StatusOK,
		Header:     http.Header{"Content-Type": []string{testContentJSON}},
		Body:       body,
		Request:    req,
	}, nil
}

// Through the proxy's handler with the limits options returns, each limit set to its
// smallest telling value: one per pod, two for the workers together. A worker pod at its
// own limit is answered 429 while another pod of the same ServiceAccount is served; with
// two pods busy the group is full and a third pod, of another worker, is refused; a caller
// outside the map holds more than any of that and is still served; and when the held
// requests end the workers are served again. The same holds for watches, counted apart.
func TestWorkersAreLimitedAndOtherCallersAreNot(t *testing.T) {
	logger := slog.New(slog.DiscardHandler)
	opts, err := options(nil, envOf(map[string]string{
		taskgate.IdentitiesEnv:                testTwoWorkers,
		taskgate.ProfileAudienceEnv:           testAudience,
		taskgate.WorkerMaxInFlightEnv:         "1",
		taskgate.WorkerMaxLongRunningEnv:      "1",
		taskgate.WorkerGroupMaxInFlightEnv:    "2",
		taskgate.WorkerGroupMaxLongRunningEnv: "2",
	}), logger)
	if err != nil {
		t.Fatalf("options: %v", err)
	}
	cluster := &heldCluster{entered: make(chan struct{}, 16), gate: make(chan struct{})}
	release := sync.OnceFunc(func() { close(cluster.gate) })
	t.Cleanup(release)
	// Without the gate's Authorize hook: the limits are what is under test, and a worker
	// here carries no task credential.
	h, err := k8sapiproxy.NewHandler(k8sapiproxy.Config{
		Authenticator: podHeaderAuthenticator{},
		Upstream:      cluster,
		Policy:        k8sapiproxy.Policy{Mode: k8sapiproxy.ModeReadOnlyWithDryRun, RedactSecrets: true},
		Logger:        logger,
		CallerLimits:  opts.CallerLimits,
	})
	if err != nil {
		t.Fatalf("NewHandler: %v", err)
	}
	request := func(username, pod, query string, hold bool) *http.Request {
		req := httptest.NewRequest(http.MethodGet, "/clusters/"+testClusterID+testPodsPath+query, nil)
		req.Header.Set(testUsernameHeader, username)
		req.Header.Set(testPodHeader, pod)
		if hold {
			req.Header.Set(testHoldHeader, "1")
		}
		return req
	}
	get := func(username, pod, query string) *httptest.ResponseRecorder {
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, request(username, pod, query, false))
		return rec
	}
	hold := func(username, pod, query string) <-chan int {
		done := make(chan int, 1)
		go func() {
			rec := httptest.NewRecorder()
			h.ServeHTTP(rec, request(username, pod, query, true))
			done <- rec.Code
		}()
		<-cluster.entered
		return done
	}
	wantThrottled := func(what string, rec *httptest.ResponseRecorder) {
		t.Helper()
		if rec.Code != http.StatusTooManyRequests || rec.Header().Get(testRetryAfterHeader) == "" {
			t.Errorf("%s: code = %d, Retry-After = %q, want 429 with the header", what, rec.Code, rec.Header().Get(testRetryAfterHeader))
		}
	}

	var held []<-chan int
	for name, query := range map[string]string{"list": "", "watch": testWatchQuery} {
		held = append(held, hold(testWorker, "pod-a", query))
		wantThrottled("a worker pod's second "+name, get(testWorker, "pod-a", query))
		// The same ServiceAccount's other pod has a count of its own.
		held = append(held, hold(testWorker, "pod-b", query))
		// Two pods busy: the group is full for every worker, this one's own count empty.
		wantThrottled("a third worker pod's "+name+" with the group full", get(testSecondUser, "pod-c", query))

		held = append(held, hold(testNonWorker, "shell-0", query), hold(testNonWorker, "shell-0", query), hold(testNonWorker, "shell-0", query))
		if rec := get(testNonWorker, "shell-0", query); rec.Code != http.StatusOK {
			t.Errorf("a fourth %s from a caller outside the map: code = %d, want 200", name, rec.Code)
		}
	}

	release()
	for _, done := range held {
		if code := <-done; code != http.StatusOK {
			t.Errorf("held request: code = %d, want 200", code)
		}
	}
	for name, query := range map[string]string{"list": "", "watch": testWatchQuery} {
		for _, pod := range []string{"pod-a", "pod-c"} {
			if rec := get(testSecondUser, pod, query); rec.Code != http.StatusOK {
				t.Errorf("a worker's %s after the held ones finished: code = %d, want 200", name, rec.Code)
			}
		}
	}
}
