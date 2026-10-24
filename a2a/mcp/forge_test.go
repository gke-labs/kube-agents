package mcp

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

// A proposal-view answer as the broker sends it for GitHub
// (providers/github/translate.py), stamped with forge and repo.
const githubViewJSON = `{
  "proposal": {"number": 12, "title": "Fix the thing", "state": "merged", "draft": false,
    "author": "octocat", "labels": ["bug"], "source": "fix-thing", "sourceRepo": "acme/widgets",
    "sourceRevision": "abc123", "target": "main", "url": "https://github.com/acme/widgets/pull/12",
    "created": "2026-10-01T00:00:00Z", "updated": "2026-10-02T00:00:00Z", "closed": "2026-10-02T00:00:00Z",
    "body": "Fixes the thing."},
  "comments": [
    {"id": 1, "ref": "issue-1", "kind": "issue", "author": "reviewer", "bot": false,
     "created": "2026-10-01T01:00:00Z", "body": "Looks good.", "url": "", "path": "", "line": null},
    {"id": 2, "ref": "review_comment-2", "kind": "review_comment", "author": "kube-agents-bot", "bot": true,
     "created": "2026-10-01T02:00:00Z", "body": "Nit here.", "url": "", "path": "main.go", "line": 7}
  ],
  "commentCount": 2, "commentsTruncated": false,
  "diff": "diff --git a/main.go b/main.go\nindex 1..2 100644\n--- a/main.go\n+++ b/main.go\n@@ -1,2 +1,2 @@\n-old\n+new\n context\n",
  "forge": "github", "repo": "acme/widgets"
}`

// The same for GitLab (providers/gitlab/translate.py): a note on a diff line
// is a review_comment, and a fork's sourceRepo is "".
const gitlabViewJSON = `{
  "proposal": {"number": 7, "title": "Add a widget", "state": "open", "draft": true,
    "author": "tanuki", "labels": [], "source": "add-widget", "sourceRepo": "",
    "sourceRevision": "def456", "target": "main", "url": "https://gitlab.example.com/group/sub/proj/-/merge_requests/7",
    "created": "2026-10-01T00:00:00Z", "updated": "2026-10-01T00:00:00Z", "closed": "",
    "body": ""},
  "comments": [
    {"id": 99, "ref": "review_comment-99", "kind": "review_comment", "author": "maintainer", "bot": false,
     "created": "2026-10-01T03:00:00Z", "body": "Why?", "url": "", "path": "widget.py", "line": 3}
  ],
  "commentCount": 1, "commentsTruncated": true,
  "diff": "diff --git a/widget.py b/widget.py\nnew file mode 100644\n--- /dev/null\n+++ b/widget.py\n@@ -0,0 +1,2 @@\n+a\n+b\n",
  "forge": "gitlab", "repo": "group/sub/proj"
}`

type seenRequest struct {
	method, path, contentType, auth string
	body                            ProposalViewRequest
}

// fakeBroker answers proposal-view with handler, recording each request.
func fakeBroker(t *testing.T, handler func(w http.ResponseWriter, r *http.Request)) (*httptest.Server, func() []seenRequest) {
	t.Helper()
	var mu sync.Mutex
	var seen []seenRequest
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		s := seenRequest{method: r.Method, path: r.URL.Path, contentType: r.Header.Get(headerContentType), auth: r.Header.Get(headerAuthorization)}
		_ = json.Unmarshal(raw, &s.body)
		mu.Lock()
		seen = append(seen, s)
		mu.Unlock()
		handler(w, r)
	}))
	t.Cleanup(srv.Close)
	return srv, func() []seenRequest {
		mu.Lock()
		defer mu.Unlock()
		return append([]seenRequest(nil), seen...)
	}
}

func answer(status int, body string) func(http.ResponseWriter, *http.Request) {
	return func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set(headerContentType, contentTypeJSON)
		w.WriteHeader(status)
		_, _ = io.WriteString(w, body)
	}
}

func newTestClient(t *testing.T, url, tokenFile string, mod func(*BrokerConfig)) *BrokerClient {
	t.Helper()
	cfg := BrokerConfig{URL: url, TokenFile: tokenFile}
	if mod != nil {
		mod(&cfg)
	}
	c, err := NewBrokerClient(cfg)
	if err != nil {
		t.Fatal(err)
	}
	return c
}

var testViewRequest = ProposalViewRequest{Repository: "acme/widgets", Number: 12, Diff: true, Comments: true, Limit: brokerCommentLimit}

func TestBrokerClientReadsAProposal(t *testing.T) {
	for _, tc := range []struct {
		name, body, forge, repo, title string
		comments                       int
	}{
		{"github", githubViewJSON, "github", "acme/widgets", "Fix the thing", 2},
		{"gitlab", gitlabViewJSON, "gitlab", "group/sub/proj", "Add a widget", 1},
	} {
		t.Run(tc.name, func(t *testing.T) {
			srv, seen := fakeBroker(t, answer(http.StatusOK, tc.body))
			c := newTestClient(t, srv.URL+"/", writeToken(t, testToken+"\n"), nil)
			v, err := c.ProposalView(context.Background(), testViewRequest)
			if err != nil {
				t.Fatal(err)
			}
			if v.Forge != tc.forge || v.Repo != tc.repo || v.Proposal.Title != tc.title || len(v.Comments) != tc.comments || v.Diff == "" {
				t.Errorf("view = %+v", v)
			}
			reqs := seen()
			if len(reqs) != 1 {
				t.Fatalf("%d requests, want 1", len(reqs))
			}
			r := reqs[0]
			if r.method != http.MethodPost || r.path != proposalViewPath || r.contentType != contentTypeJSON {
				t.Errorf("request %s %s (%s)", r.method, r.path, r.contentType)
			}
			if r.auth != bearerPrefix+testToken {
				t.Errorf("Authorization = %q, want the trimmed token from the file", r.auth)
			}
			if r.body != testViewRequest {
				t.Errorf("body = %+v, want %+v", r.body, testViewRequest)
			}
		})
	}
}

// A projected token is rewritten in place before it expires; the next call
// presents the new one without a restart.
func TestBrokerClientRereadsTheTokenOnEveryCall(t *testing.T) {
	srv, seen := fakeBroker(t, answer(http.StatusOK, githubViewJSON))
	path := writeToken(t, "first-token")
	c := newTestClient(t, srv.URL, path, nil)
	if _, err := c.ProposalView(context.Background(), testViewRequest); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte("rotated-token\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := c.ProposalView(context.Background(), testViewRequest); err != nil {
		t.Fatal(err)
	}
	reqs := seen()
	if len(reqs) != 2 || reqs[0].auth != "Bearer first-token" || reqs[1].auth != "Bearer rotated-token" {
		t.Errorf("auth headers = %+v", reqs)
	}
}

func TestBrokerClientMakesNoRequestWithoutAToken(t *testing.T) {
	for name, path := range map[string]string{
		"missing": filepath.Join(t.TempDir(), "absent"),
		"empty":   writeToken(t, " \n"),
	} {
		t.Run(name, func(t *testing.T) {
			srv, seen := fakeBroker(t, answer(http.StatusOK, githubViewJSON))
			c := newTestClient(t, srv.URL, path, nil)
			if _, err := c.ProposalView(context.Background(), testViewRequest); err == nil {
				t.Error("read a proposal with no token")
			}
			if n := len(seen()); n != 0 {
				t.Errorf("%d requests made without a token", n)
			}
		})
	}
}

// Every refusal becomes this server's words and the broker's code, never the
// broker's free text (outside the statuses where it says what to fix) and
// never the token, even when the broker echoes it.
func TestBrokerClientMapsRefusals(t *testing.T) {
	for _, tc := range []struct {
		name, body string
		status     int
		code       string
		want       string
	}{
		{"401", `{"error":"caller could not be authenticated ` + testToken + `"}`, 401, "", "did not accept this MCP server's token"},
		{"403 not managed", `{"error":"register it in the gitops-state ConfigMap first","code":"REPOSITORY_NOT_MANAGED"}`, 403, "REPOSITORY_NOT_MANAGED", "does not manage that repository"},
		{"403 refused verb", `{"status":"blocked","error":"verb not available","code":"CALLER_VERB_FORBIDDEN","rule":"x"}`, 403, "CALLER_VERB_FORBIDDEN", "may not read proposals"},
		{"403 forge", `{"error":"the forge said ` + testToken + `","code":"FORGE_FORBIDDEN"}`, 403, "FORGE_FORBIDDEN", "forge refused"},
		{"404 not found", `{"error":"No such repository","code":"FORGE_NOT_FOUND","detail":"Not Found"}`, 404, "FORGE_NOT_FOUND", "no such pull or merge request"},
		{"404 no route", `{"status":"not_found"}`, 404, "", "no such route, repository or proposal"},
		{"500", `{"error":"boom ` + testToken + `"}`, 500, "", "failed (HTTP 500)"},
		{"503 not json", `<html>` + testToken + `</html>`, 503, "", "busy; try again later"},
		{"503 the broker's own busy", `{"status":"busy","code":"CREDENTIAL_PROXY_BUSY","error":"all slots in use"}`, 503, "CREDENTIAL_PROXY_BUSY", "the credential broker is busy with other requests"},
		{"a code of the wrong shape is not repeated", `{"error":"x","code":"<script>` + testToken + `"}`, 502, "", "failed (HTTP 502)"},
		{"400 keeps the broker's words, without the token", `{"error":"'x y' is not a repository URL or owner/name ` + testToken + `"}`, 400, "", "is not a repository URL"},
		{"501 names the gap", `{"error":"names no host, and this install serves more than one forge","code":"FORGE_UNSUPPORTED"}`, 501, "FORGE_UNSUPPORTED", "serves more than one forge"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			srv, _ := fakeBroker(t, answer(tc.status, tc.body))
			c := newTestClient(t, srv.URL, writeToken(t, testToken), nil)
			_, err := c.ProposalView(context.Background(), testViewRequest)
			var be *BrokerError
			if !errors.As(err, &be) {
				t.Fatalf("err = %v, want a BrokerError", err)
			}
			if be.Status != tc.status || be.Code != tc.code {
				t.Errorf("status %d code %q, want %d %q", be.Status, be.Code, tc.status, tc.code)
			}
			msg := err.Error()
			if !strings.Contains(msg, tc.want) {
				t.Errorf("error %q does not say %q", msg, tc.want)
			}
			if strings.Contains(msg, testToken) {
				t.Errorf("error %q echoes the token", msg)
			}
			if strings.Contains(msg, "ConfigMap") || strings.Contains(msg, "<script>") {
				t.Errorf("error %q repeats the broker's text", msg)
			}
		})
	}
}

func TestBrokerClientRefusesAnOversizedAnswer(t *testing.T) {
	big := strings.Replace(githubViewJSON, `"Fixes the thing."`, `"`+strings.Repeat("x", 4096)+`"`, 1)
	srv, _ := fakeBroker(t, answer(http.StatusOK, big))
	c := newTestClient(t, srv.URL, writeToken(t, testToken), func(cfg *BrokerConfig) { cfg.MaxResponseBytes = 2048 })
	_, err := c.ProposalView(context.Background(), testViewRequest)
	if !errors.Is(err, ErrBrokerResponseTooLarge) {
		t.Fatalf("err = %v, want ErrBrokerResponseTooLarge", err)
	}
	if !diffMayBeRefused(err) {
		t.Error("an oversized answer is not offered the retry without the diff")
	}
}

// An answer that declares a length past the cap is refused before any of
// it is read.
func TestBrokerClientRefusesADeclaredOversizedAnswerUnread(t *testing.T) {
	srv, _ := fakeBroker(t, func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Length", "1073741824")
		w.WriteHeader(http.StatusOK)
		_, _ = io.WriteString(w, githubViewJSON)
	})
	c := newTestClient(t, srv.URL, writeToken(t, testToken), nil)
	if _, err := c.ProposalView(context.Background(), testViewRequest); !errors.Is(err, ErrBrokerResponseTooLarge) {
		t.Fatalf("err = %v, want ErrBrokerResponseTooLarge", err)
	}
}

func TestBrokerClientTimesOut(t *testing.T) {
	release := make(chan struct{})
	srv, _ := fakeBroker(t, func(w http.ResponseWriter, r *http.Request) {
		select {
		case <-r.Context().Done():
		case <-release:
		}
	})
	defer close(release)
	c := newTestClient(t, srv.URL, writeToken(t, testToken), func(cfg *BrokerConfig) { cfg.Timeout = 50 * time.Millisecond })
	start := time.Now()
	_, err := c.ProposalView(context.Background(), testViewRequest)
	if err == nil || !strings.Contains(err.Error(), "did not answer") {
		t.Fatalf("err = %v, want a timeout", err)
	}
	if time.Since(start) > 5*time.Second {
		t.Errorf("took %v", time.Since(start))
	}
}

// A redirect would hand the bearer token to wherever it points.
func TestBrokerClientDoesNotFollowRedirects(t *testing.T) {
	var elsewhereHits int
	var mu sync.Mutex
	elsewhere := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		elsewhereHits++
		mu.Unlock()
		_, _ = io.WriteString(w, githubViewJSON)
	}))
	defer elsewhere.Close()
	srv, _ := fakeBroker(t, func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, elsewhere.URL+proposalViewPath, http.StatusTemporaryRedirect)
	})
	c := newTestClient(t, srv.URL, writeToken(t, testToken), nil)
	_, err := c.ProposalView(context.Background(), testViewRequest)
	var be *BrokerError
	if !errors.As(err, &be) || be.Status != http.StatusTemporaryRedirect || !strings.Contains(err.Error(), "not followed") {
		t.Errorf("err = %v, want the redirect refused", err)
	}
	mu.Lock()
	defer mu.Unlock()
	if elsewhereHits != 0 {
		t.Errorf("the redirect target was called %d times", elsewhereHits)
	}
}

func TestNewBrokerClientValidatesItsConfig(t *testing.T) {
	token := writeToken(t, testToken)
	for name, cfg := range map[string]BrokerConfig{
		"no URL":                     {TokenFile: token},
		"no scheme":                  {URL: "broker:8080", TokenFile: token},
		"another scheme":             {URL: "unix:///run/broker.sock", TokenFile: token},
		"no host":                    {URL: "http://", TokenFile: token},
		"credentials":                {URL: "http://user:pw@broker:8080", TokenFile: token},
		"a port and no host":         {URL: "http://:8080", TokenFile: token},
		"a query":                    {URL: "http://broker:8080?x=1", TokenFile: token},
		"an empty query":             {URL: "http://broker:8080?", TokenFile: token},
		"a slash and an empty query": {URL: "http://broker:8080/?", TokenFile: token},
		"a fragment":                 {URL: "http://broker:8080#x", TokenFile: token},
		"an empty fragment":          {URL: "http://broker:8080#", TokenFile: token},
		"a path":                     {URL: "http://broker:8080/api", TokenFile: token},
		"a path and a slash":         {URL: "http://broker:8080/api/", TokenFile: token},
		"no token file":              {URL: "http://broker:8080"},
	} {
		t.Run(name, func(t *testing.T) {
			if _, err := NewBrokerClient(cfg); err == nil {
				t.Errorf("accepted %+v", cfg)
			}
		})
	}
	for _, base := range []string{"http://broker.ns.svc:8080/", "http://broker.ns.svc:8080", " http://broker.ns.svc:8080 "} {
		c, err := NewBrokerClient(BrokerConfig{URL: base, TokenFile: token})
		if err != nil || c.endpoint != "http://broker.ns.svc:8080"+proposalViewPath {
			t.Errorf("%q: endpoint %q err %v", base, c.endpoint, err)
		}
	}
}

// envProxyChild marks the re-executed test binary that runs with HTTP_PROXY
// set from its start: Go reads the proxy variables once per process, so a
// t.Setenv in this one would be read too late, or not at all.
const envProxyChild = "MCP_TEST_PROXY_CHILD"

// An HTTP_PROXY in the pod's environment must not carry the broker request,
// and the bearer token on it, through the proxy. The broker's host here is
// one that does not resolve, so the direct request fails at the dial, and the
// recording proxy must see nothing. (A loopback broker would prove nothing:
// Go never proxies a request to loopback.)
func TestBrokerClientIgnoresHTTPProxy(t *testing.T) {
	if os.Getenv(envProxyChild) == "1" {
		c := newTestClient(t, "http://broker.invalid:8080", writeToken(t, testToken), func(cfg *BrokerConfig) { cfg.Timeout = 10 * time.Second })
		_, err := c.ProposalView(context.Background(), testViewRequest)
		var be *BrokerError
		if err == nil || errors.As(err, &be) || !strings.Contains(err.Error(), "broker.invalid") {
			t.Fatalf("err = %v, want a failure to reach broker.invalid itself", err)
		}
		return
	}
	var mu sync.Mutex
	var hits []string
	proxy := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		hits = append(hits, r.Method+" "+r.URL.String()+" auth="+r.Header.Get(headerAuthorization))
		mu.Unlock()
		w.WriteHeader(http.StatusBadGateway)
	}))
	defer proxy.Close()
	exe, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, exe, "-test.run=^TestBrokerClientIgnoresHTTPProxy$", "-test.count=1") // #nosec G204 -- this test binary
	cmd.Env = append(os.Environ(), envProxyChild+"=1",
		"HTTP_PROXY="+proxy.URL, "http_proxy="+proxy.URL, "NO_PROXY=", "no_proxy=")
	out, runErr := cmd.CombinedOutput()
	mu.Lock()
	defer mu.Unlock()
	if len(hits) != 0 {
		t.Errorf("the broker request went through HTTP_PROXY: %q", hits)
	}
	if runErr != nil {
		t.Errorf("child: %v\n%s", runErr, out)
	}
}
