/*
Copyright 2026 Google LLC

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
)

// fakeGateway answers /v1/chat/completions the way LiteLLM does, with the
// content a test chooses, and records the request it received.
func fakeGateway(t *testing.T, status int, content string) (*httptest.Server, *map[string]any) {
	t.Helper()
	var seen map[string]any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/chat/completions" {
			http.NotFound(w, r)
			return
		}
		raw, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(raw, &seen)
		w.Header().Set("Content-Type", contentTypeJSON)
		w.WriteHeader(status)
		if status != http.StatusOK {
			_, _ = w.Write([]byte(`{"error":{"message":"model overloaded"}}`))
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]any{
			"model":   "vertex_ai/gemini-test",
			"choices": []map[string]any{{"message": map[string]string{"role": "assistant", "content": content}}},
		})
	}))
	t.Cleanup(srv.Close)
	return srv, &seen
}

func assistReq(body string) *http.Request {
	req := httptest.NewRequest(http.MethodPost, "http://localhost:8080/api/assist", strings.NewReader(body))
	req.Header.Set("Content-Type", contentTypeJSON)
	req.Header.Set(consoleHeader, consoleHeaderValue)
	return req
}

func TestAssistEditsBothFieldsAndReportsModel(t *testing.T) {
	gw, seen := fakeGateway(t, http.StatusOK, `{"title":"Reply stopped at a stub","body":"## What happened\nThe agent said checking and stopped.","notes":["Fixed spelling."]}`)
	_, s := setupServer(t)
	s.cfg.LiteLLMURL = gw.URL
	rec := serve(s.routes(), assistReq(`{"kind":"bug","title":"Reply stoped at stub","body":"## What happened\nthe agnet said checking and stoped","scope":"all"}`))
	if rec.Code != http.StatusOK {
		t.Fatalf("status %d: %s", rec.Code, rec.Body.String())
	}
	out := decode[assistResponse](t, rec)
	if out.Title != "Reply stopped at a stub" || !strings.Contains(out.Body, "stopped.") {
		t.Fatalf("edited draft not returned: %+v", out)
	}
	if out.Model != "vertex_ai/gemini-test" || len(out.Notes) != 1 {
		t.Fatalf("model/notes missing: %+v", out)
	}
	if (*seen)["model"] != defaultAssistModel {
		t.Fatalf("gateway asked for model %v, want %s", (*seen)["model"], defaultAssistModel)
	}
	if rf, _ := (*seen)["response_format"].(map[string]any); rf["type"] != "json_object" {
		t.Fatalf("response_format not requested: %v", (*seen)["response_format"])
	}
}

func TestAssistScopeKeepsTheOtherFieldAsSent(t *testing.T) {
	gw, seen := fakeGateway(t, http.StatusOK, "```json\n{\"title\":\"Model changed title\",\"body\":\"Edited body.\",\"notes\":[]}\n```")
	_, s := setupServer(t)
	s.cfg.LiteLLMURL = gw.URL
	rec := serve(s.routes(), assistReq(`{"title":"keep me","body":"edit me","scope":"body"}`))
	if rec.Code != http.StatusOK {
		t.Fatalf("status %d: %s", rec.Code, rec.Body.String())
	}
	out := decode[assistResponse](t, rec)
	if out.Title != "keep me" {
		t.Fatalf("title changed outside scope: %q", out.Title)
	}
	if out.Body != "Edited body." {
		t.Fatalf("fenced JSON not parsed: %q", out.Body)
	}
	msgs, _ := (*seen)["messages"].([]any)
	user, _ := msgs[1].(map[string]any)
	if content, _ := user["content"].(string); strings.Contains(content, "keep me") {
		t.Fatalf("title sent to the model although scope was body: %q", content)
	}
}

func TestAssistRejectsBadInputAndUpstreamFailure(t *testing.T) {
	_, s := setupServer(t)
	if rec := serve(s.routes(), assistReq(`{"title":"x"}`)); rec.Code != http.StatusNotImplemented {
		t.Fatalf("without a gateway want 501, got %d", rec.Code)
	}
	gw, _ := fakeGateway(t, http.StatusServiceUnavailable, "")
	s.cfg.LiteLLMURL = gw.URL
	cases := map[string]int{
		`{"title":"","body":""}`:                  http.StatusBadRequest,
		`{"title":"x","scope":"everything"}`:      http.StatusBadRequest,
		`not json`:                                http.StatusBadRequest,
		`{"title":"x","body":"ok","scope":"all"}`: http.StatusBadGateway,
	}
	for body, want := range cases {
		if rec := serve(s.routes(), assistReq(body)); rec.Code != want {
			t.Errorf("%s: want %d, got %d (%s)", body, want, rec.Code, rec.Body.String())
		}
	}
	noHeader := assistReq(`{"title":"x","body":"ok","scope":"all"}`)
	noHeader.Header.Del(consoleHeader)
	if rec := serve(s.routes(), noHeader); rec.Code != http.StatusForbidden {
		t.Fatalf("request without the console header: want 403, got %d (%s)", rec.Code, rec.Body.String())
	}
}

func TestAssistRefusesAnOversizedDraft(t *testing.T) {
	gw, _ := fakeGateway(t, http.StatusOK, `{}`)
	_, s := setupServer(t)
	s.cfg.LiteLLMURL = gw.URL
	body, _ := json.Marshal(map[string]string{"title": "t", "body": strings.Repeat("a", assistMaxChars)})
	if rec := serve(s.routes(), assistReq(string(body))); rec.Code != http.StatusRequestEntityTooLarge {
		t.Fatalf("want 413, got %d", rec.Code)
	}
}

// --- Release check ---

// fakeGitHub serves the release list (newest first) and, for a build from
// source, the compare answer for each tag.
func fakeGitHub(t *testing.T, tags []string, compare map[string]map[string]any) (*httptest.Server, *int32) {
	t.Helper()
	var hits int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		atomic.AddInt32(&hits, 1)
		if r.Header.Get("User-Agent") != gitHubUserAgent {
			t.Errorf("User-Agent %q", r.Header.Get("User-Agent"))
		}
		switch {
		case strings.HasSuffix(r.URL.Path, "/releases"):
			var list []map[string]any
			for _, tag := range tags {
				list = append(list, map[string]any{
					"tag_name":     tag,
					"html_url":     "https://github.com/gke-labs/kube-agents/releases/tag/" + tag,
					"published_at": "2026-10-09T12:00:00Z",
				})
			}
			_ = json.NewEncoder(w).Encode(list)
		case strings.Contains(r.URL.Path, "/compare/"):
			spec := r.URL.Path[strings.LastIndex(r.URL.Path, "/")+1:]
			tag := spec[:strings.Index(spec, "...")]
			ans, ok := compare[tag]
			if !ok {
				http.NotFound(w, r)
				return
			}
			_ = json.NewEncoder(w).Encode(ans)
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(srv.Close)
	return srv, &hits
}

func releaseFor(t *testing.T, s *server) releaseResponse {
	t.Helper()
	rec := serve(s.routes(), httptest.NewRequest(http.MethodGet, "http://localhost:8080/api/release", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("status %d: %s", rec.Code, rec.Body.String())
	}
	return decode[releaseResponse](t, rec)
}

func releaseServer(t *testing.T, gh *httptest.Server, imageTag, chart string) *server {
	t.Helper()
	_, s := setupServer(t)
	s.cfg.GitHubAPIBase, s.cfg.ReleaseCheck, s.cfg.ReleaseRepo = gh.URL, true, defaultReleaseRepo
	s.cfg.ImageTag, s.cfg.ChartVersion = imageTag, chart
	return s
}

const sourceSHA = "06e1ca652695b0f88cad7b0b55df67a092bed888"

func TestRunningVersionClassifiesTags(t *testing.T) {
	cases := []struct {
		tag, chart, wantRunning, wantKind, wantChart string
	}{
		{"0.9.0", "0.9.0", "0.9.0", "release", "0.9.0"},
		{"v0.8.0", "0.1.0", "0.8.0", "release", ""},
		{sourceSHA, "0.1.0", sourceSHA, "commit", ""},
		{"", "0.7.1", "0.7.1", "release", "0.7.1"},
		{"latest", "0.1.0", "latest", "unknown", ""},
		{"", "", "unknown", "unknown", ""},
	}
	for _, c := range cases {
		running, kind, chart := runningVersion(c.tag, c.chart)
		if running != c.wantRunning || kind != c.wantKind || chart != c.wantChart {
			t.Errorf("runningVersion(%q,%q) = %q,%q,%q; want %q,%q,%q", c.tag, c.chart, running, kind, chart, c.wantRunning, c.wantKind, c.wantChart)
		}
	}
}

func TestCompareReleases(t *testing.T) {
	cases := []struct {
		a, b string
		want int
	}{
		{"0.9.0", "0.9.0", 0}, {"0.8.0", "0.9.0", -1}, {"0.10.0", "0.9.0", 1}, {"1.0.0", "0.99.9", 1}, {"v0.9.0", "0.9.0", 0},
	}
	for _, c := range cases {
		if got := compareReleases(c.a, c.b); got != c.want {
			t.Errorf("compareReleases(%q,%q)=%d want %d", c.a, c.b, got, c.want)
		}
	}
}

func TestReleaseForAReleaseInstall(t *testing.T) {
	gh, _ := fakeGitHub(t, []string{"0.9.0", "0.8.0"}, nil)
	s := releaseServer(t, gh, "0.8.0", "0.8.0")
	out := releaseFor(t, s)
	if out.Running != "0.8.0" || out.RunningKind != "release" || out.Latest != "0.9.0" || out.Status != "behind" {
		t.Fatalf("unexpected: %+v", out)
	}
	if !strings.Contains(out.Detail, "0.9.0 is available") || out.LatestURL == "" {
		t.Fatalf("detail/url: %+v", out)
	}
	s.cfg.ImageTag = "0.9.0"
	s.releaseInfo = releaseCache{}
	if out := releaseFor(t, s); out.Status != "latest" {
		t.Fatalf("want latest, got %+v", out)
	}
	s.cfg.ImageTag = "0.10.0"
	s.releaseInfo = releaseCache{}
	if out := releaseFor(t, s); out.Status != "ahead" {
		t.Fatalf("want ahead, got %+v", out)
	}
}

// A build between two releases is named by the older one it contains and
// flagged behind the newer; the commit never appears in the answer.
func TestReleaseNamesASourceBuildByTheReleaseItContains(t *testing.T) {
	gh, hits := fakeGitHub(t, []string{"0.9.0", "0.8.0", "0.7.0"}, map[string]map[string]any{
		// As GitHub answers for a main build between two releases: an
		// ancestor of 0.9.0, and one stamp commit short of 0.8.0.
		"0.9.0": {"status": "behind", "ahead_by": 0, "behind_by": 127},
		"0.8.0": {"status": "diverged", "ahead_by": 57, "behind_by": 1},
	})
	s := releaseServer(t, gh, sourceSHA, chartPlaceholderVersion)
	out := releaseFor(t, s)
	if out.Running != "0.8.0" || out.RunningKind != "source" || out.ChartVersion != "" {
		t.Fatalf("running: %+v", out)
	}
	if out.Status != "behind" || !strings.Contains(out.Detail, "after release 0.8.0 (57 commits later); release 0.9.0 is available") {
		t.Fatalf("status: %+v", out)
	}
	raw, _ := json.Marshal(out)
	if strings.Contains(string(raw), sourceSHA[:7]) {
		t.Fatalf("the commit leaked into the answer: %s", raw)
	}
	// The second read within the hour comes from the cache.
	before := atomic.LoadInt32(hits)
	releaseFor(t, s)
	if atomic.LoadInt32(hits) != before {
		t.Fatalf("second read hit GitHub again")
	}
}

func TestReleaseFlagsASourceBuildNewerThanTheLatestRelease(t *testing.T) {
	gh, _ := fakeGitHub(t, []string{"0.9.0", "0.8.0"}, map[string]map[string]any{
		"0.9.0": {"status": "ahead", "ahead_by": 5, "behind_by": 0},
	})
	out := releaseFor(t, releaseServer(t, gh, sourceSHA, chartPlaceholderVersion))
	if out.Running != "0.9.0" || out.Status != "ahead" || !strings.Contains(out.Detail, "pre-release build") {
		t.Fatalf("unexpected: %+v", out)
	}
	exact, _ := fakeGitHub(t, []string{"0.9.0"}, map[string]map[string]any{"0.9.0": {"status": "identical"}})
	if out := releaseFor(t, releaseServer(t, exact, sourceSHA, chartPlaceholderVersion)); out.Running != "0.9.0" || out.Status != "latest" {
		t.Fatalf("identical: %+v", out)
	}
}

func TestReleaseForASourceBuildOlderThanEveryRelease(t *testing.T) {
	gh, _ := fakeGitHub(t, []string{"0.9.0", "0.8.0"}, map[string]map[string]any{
		"0.9.0": {"status": "behind", "behind_by": 90},
		// A branch build that lacks more than the stamp commit does not
		// contain the release.
		"0.8.0": {"status": "diverged", "ahead_by": 4, "behind_by": 40},
	})
	out := releaseFor(t, releaseServer(t, gh, sourceSHA, chartPlaceholderVersion))
	if out.Running != "older than 0.8.0" || out.Status != "behind" {
		t.Fatalf("unexpected: %+v", out)
	}
}

func TestReleaseWhenDisabledOrUnreachable(t *testing.T) {
	_, s := setupServer(t)
	s.cfg.ReleaseCheck, s.cfg.ImageTag = false, "0.8.0"
	if out := releaseFor(t, s); out.Status != "disabled" || out.Running != "0.8.0" {
		t.Fatalf("disabled: %+v", out)
	}
	s.cfg.ImageTag = sourceSHA
	s.releaseInfo = releaseCache{}
	if out := releaseFor(t, s); out.Running != "unknown" || out.RunningKind != "source" {
		t.Fatalf("disabled source build: %+v", out)
	}
	down := httptest.NewServer(http.NotFoundHandler())
	down.Close()
	out := releaseFor(t, releaseServer(t, down, "0.8.0", "0.8.0"))
	if out.Status != "unknown" || out.Running != "0.8.0" || !strings.Contains(out.Detail, "Could not read the releases") {
		t.Fatalf("unreachable: %+v", out)
	}
}

func TestEnvBool(t *testing.T) {
	t.Setenv(envReleaseCheck, "off")
	if envBool(envReleaseCheck, true) {
		t.Fatal("off read as true")
	}
	t.Setenv(envReleaseCheck, "")
	if !envBool(envReleaseCheck, true) {
		t.Fatal("unset did not give the fallback")
	}
	t.Setenv(envReleaseCheck, "maybe")
	if envBool(envReleaseCheck, false) {
		t.Fatal("unrecognised value did not give the fallback")
	}
}

// A model that refuses response_format (Gemma on Vertex, some MaaS
// endpoints) gets a second request without it; the fenced JSON it returns
// is still read.
func TestAssistRetriesWithoutJSONModeWhenTheModelRefusesIt(t *testing.T) {
	var calls []bool
	gw := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var req map[string]any
		_ = json.NewDecoder(r.Body).Decode(&req)
		_, jsonMode := req["response_format"]
		calls = append(calls, jsonMode)
		if jsonMode {
			w.WriteHeader(http.StatusBadRequest)
			_, _ = w.Write([]byte(`{"error":{"message":"response_format is not supported by this model"}}`))
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]any{
			"model":   "vertex_ai/gemma-3-27b-it",
			"choices": []map[string]any{{"message": map[string]string{"content": "```json\n{\"title\":\"Fixed title\",\"body\":\"Fixed body.\",\"notes\":[\"Spelling.\"]}\n```"}}},
		})
	}))
	t.Cleanup(gw.Close)
	_, s := setupServer(t)
	s.cfg.LiteLLMURL = gw.URL
	rec := serve(s.routes(), assistReq(`{"title":"fixd title","body":"fixd body","scope":"all"}`))
	if rec.Code != http.StatusOK {
		t.Fatalf("status %d: %s", rec.Code, rec.Body.String())
	}
	out := decode[assistResponse](t, rec)
	if out.Title != "Fixed title" || out.Model != "vertex_ai/gemma-3-27b-it" {
		t.Fatalf("unexpected: %+v", out)
	}
	if len(calls) != 2 || !calls[0] || calls[1] {
		t.Fatalf("want one call with response_format then one without, got %v", calls)
	}
}

func TestReleaseLatestIsTheHighestVersionNotTheFirstListed(t *testing.T) {
	// A patch to an older line, published last, is listed first.
	gh, _ := fakeGitHub(t, []string{"v0.8.3", "v0.9.1", "v0.9.0"}, nil)
	got := releaseFor(t, releaseServer(t, gh, "0.9.0", "0.9.0"))
	if got.Latest != "0.9.1" || got.Status != "behind" {
		t.Errorf("latest = %q status = %q, want 0.9.1 behind", got.Latest, got.Status)
	}
}

// A body past the request reader's limit is a draft too long (413), not
// malformed JSON (400).
func TestAssistReportsAnOverLimitBodyAsTooLong(t *testing.T) {
	gw, _ := fakeGateway(t, http.StatusOK, `{}`)
	_, s := setupServer(t)
	s.cfg.LiteLLMURL = gw.URL
	body, _ := json.Marshal(map[string]string{"title": "t", "body": strings.Repeat("a", maxRequestBodyBytes)})
	rec := serve(s.routes(), assistReq(string(body)))
	if rec.Code != http.StatusRequestEntityTooLarge || !strings.Contains(rec.Body.String(), errCodeDraftTooLong) {
		t.Fatalf("want 413 %s, got %d %s", errCodeDraftTooLong, rec.Code, rec.Body.String())
	}
}

// The draft limit counts characters: a non-ASCII draft under the limit in
// characters but over it in UTF-8 bytes is edited, not refused.
func TestAssistCountsCharactersNotBytes(t *testing.T) {
	gw, _ := fakeGateway(t, http.StatusOK, `{"title":"t","body":"b","notes":[]}`)
	_, s := setupServer(t)
	s.cfg.LiteLLMURL = gw.URL
	draft := strings.Repeat("界", assistMaxChars/2)
	if len(draft) <= assistMaxChars {
		t.Fatalf("fixture is not over the limit in bytes: %d", len(draft))
	}
	body, _ := json.Marshal(map[string]string{"title": "t", "body": draft})
	if rec := serve(s.routes(), assistReq(string(body))); rec.Code != http.StatusOK {
		t.Fatalf("want 200 for %d characters, got %d %s", assistMaxChars/2, rec.Code, rec.Body.String())
	}
}

// Only a 400 can be a refusal of response_format; any other 4xx (an
// unknown model, an auth refusal, a rate limit) is not sent a second time.
func TestAssistDoesNotRetryANon400Rejection(t *testing.T) {
	for _, status := range []int{http.StatusUnauthorized, http.StatusNotFound, http.StatusTooManyRequests} {
		var calls int32
		gw := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			atomic.AddInt32(&calls, 1)
			w.WriteHeader(status)
			_, _ = w.Write([]byte(`{"error":{"message":"refused"}}`))
		}))
		_, s := setupServer(t)
		s.cfg.LiteLLMURL = gw.URL
		rec := serve(s.routes(), assistReq(`{"title":"x","body":"y"}`))
		gw.Close()
		if rec.Code != http.StatusBadGateway || atomic.LoadInt32(&calls) != 1 {
			t.Errorf("HTTP %d: want one gateway call and 502, got %d calls and %d", status, calls, rec.Code)
		}
	}
}

// GitHub answers compare/<tag>...<sha> for the commit a release was cut
// from (the stamp commit's parent) as behind by one, ahead by none.
func TestReleaseNamesTheCommitAReleaseWasCutFrom(t *testing.T) {
	gh, _ := fakeGitHub(t, []string{"0.9.0", "0.8.0"}, map[string]map[string]any{
		"0.9.0": {"status": "behind", "ahead_by": 0, "behind_by": 1},
		"0.8.0": {"status": "diverged", "ahead_by": 40, "behind_by": 1},
	})
	out := releaseFor(t, releaseServer(t, gh, sourceSHA, chartPlaceholderVersion))
	if out.Running != "0.9.0" || out.Status != "latest" || !strings.Contains(out.Detail, "the commit release 0.9.0 was cut from") {
		t.Fatalf("want 0.9.0 latest, got %+v", out)
	}
}

// A failed GitHub read is served for releaseFailureCacheTTL, not the hour a
// good answer is, and a read the caller abandoned is not kept at all.
func TestReleaseDoesNotPinAFailedRead(t *testing.T) {
	var fail atomic.Bool
	fail.Store(true)
	good, _ := fakeGitHub(t, []string{"0.9.0"}, nil)
	gh := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if fail.Load() {
			w.WriteHeader(http.StatusForbidden)
			return
		}
		good.Config.Handler.ServeHTTP(w, r)
	}))
	t.Cleanup(gh.Close)
	s := releaseServer(t, gh, "0.9.0", "0.9.0")
	if out := releaseFor(t, s); out.Status != "unknown" || !strings.Contains(out.Detail, "HTTP 403") {
		t.Fatalf("first read: %+v", out)
	}
	fail.Store(false)
	if out := releaseFor(t, s); out.Status != "unknown" {
		t.Fatalf("a failure inside releaseFailureCacheTTL should still be served: %+v", out)
	}
	s.releaseInfo.mu.Lock()
	s.releaseInfo.readAt = s.releaseInfo.readAt.Add(-releaseFailureCacheTTL)
	s.releaseInfo.mu.Unlock()
	if out := releaseFor(t, s); out.Status != "latest" {
		t.Fatalf("a failure older than releaseFailureCacheTTL was still served: %+v", out)
	}
	// A good answer the same age is still inside the hour.
	s.releaseInfo.mu.Lock()
	s.releaseInfo.readAt = s.releaseInfo.readAt.Add(-releaseFailureCacheTTL)
	s.releaseInfo.mu.Unlock()
	fail.Store(true)
	if out := releaseFor(t, s); out.Status != "latest" {
		t.Fatalf("a good answer was dropped before releaseCacheTTL: %+v", out)
	}

	abandoned := releaseServer(t, gh, "0.9.0", "0.9.0")
	ctx, cancel := context.WithCancel(t.Context())
	cancel()
	abandoned.currentRelease(ctx)
	if abandoned.releaseInfo.value != nil {
		t.Fatalf("a read under a cancelled request was cached: %+v", *abandoned.releaseInfo.value)
	}
}

// fakePlatformAgentAPI answers the PlatformAgent read with the tag a test
// chooses and 404s everything else.
func fakePlatformAgentAPI(t *testing.T, tag *atomic.Value) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != fmt.Sprintf(platformAgentsAPIPath, "kubeagents-system", "platform-agent") {
			http.NotFound(w, r)
			return
		}
		v := tag.Load().(string)
		if v == "ERROR" {
			w.WriteHeader(http.StatusInternalServerError)
			return
		}
		w.Header().Set("Content-Type", contentTypeJSON)
		fmt.Fprintf(w, `{"spec":{"deployment":{"tag":%q}}}`, v)
	}))
	t.Cleanup(srv.Close)
	return srv
}

// /api/release classifies the live PlatformAgent tag, places a source build
// by that tag (not KUBE_AGENTS_IMAGE_TAG), and a tag that moves outside the
// console shows on the next request rather than after the hour.
func TestReleaseFollowsTheLivePlatformAgentTag(t *testing.T) {
	var heads []string
	var mu sync.Mutex
	gh := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case strings.HasSuffix(r.URL.Path, "/releases"):
			_ = json.NewEncoder(w).Encode([]map[string]any{{"tag_name": "0.9.0"}, {"tag_name": "0.8.0"}})
		case strings.Contains(r.URL.Path, "/compare/"):
			mu.Lock()
			heads = append(heads, r.URL.Path[strings.LastIndex(r.URL.Path, "...")+len("..."):])
			mu.Unlock()
			_ = json.NewEncoder(w).Encode(map[string]any{"status": "ahead", "ahead_by": 3})
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(gh.Close)
	var tag atomic.Value
	tag.Store("0.9.0")
	api := fakePlatformAgentAPI(t, &tag)
	s := releaseServer(t, gh, "0.8.0", "0.8.0")
	s.cfg.PodNamespace, s.cfg.PlatformAgentName = "kubeagents-system", "platform-agent"
	s.kube = &kubeClient{base: api.URL, client: api.Client()}

	if out := releaseFor(t, s); out.Running != "0.9.0" || out.Status != "latest" {
		t.Fatalf("want the PlatformAgent's 0.9.0 over cfg's 0.8.0, got %+v", out)
	}
	// A transient PlatformAgent API failure must preserve the last live tag
	// rather than re-keying the cache back to cfg.ImageTag (0.8.0).
	tag.Store("ERROR")
	if out := releaseFor(t, s); out.Running != "0.9.0" || out.Status != "latest" {
		t.Fatalf("transient API failure flipped release back to startup tag: %+v", out)
	}
	tag.Store(sourceSHA)
	out := releaseFor(t, s)
	if out.RunningKind != "source" || out.Running != "0.9.0" || out.Status != "ahead" {
		t.Fatalf("a tag edited in place was not picked up on the next request: %+v", out)
	}
	mu.Lock()
	defer mu.Unlock()
	if len(heads) == 0 || heads[0] != sourceSHA {
		t.Fatalf("compare placed %v, want the live tag %s", heads, sourceSHA)
	}
}
