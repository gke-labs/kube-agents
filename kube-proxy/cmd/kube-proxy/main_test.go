package main

import (
	"log/slog"
	"net/http"
	"net/http/httptest"
	"slices"
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

func envOf(values map[string]string) func(string) string {
	return func(name string) string { return values[name] }
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
