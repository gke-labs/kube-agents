package k8sapiproxy

import (
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"

	"k8s.io/client-go/discovery"
	"k8s.io/client-go/rest"
)

func heldHandler(t *testing.T) (http.Handler, *recordingTransport) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "hold.json")
	writeScope(t, path, holdOf(false, "INC-42 rollback", "payments-staging/us-central1/scratch"))
	return newTestHandler(t, Config{Holds: NewHolds(path, hostID)})
}

// A scope refusal on a discovery path is plain text: client-go's discovery client
// can't decode a Status there and would report "unknown" (kubectl cluster-info).
func TestAScopeRefusalOnADiscoveryPathIsPlainText(t *testing.T) {
	h, upstream := heldHandler(t)
	for _, p := range []string{"/api", "/api/v1", "/apis", "/apis/apps", "/apis/apps/v1", "/version", "/openapi/v2", "/openapi/v3/apis/apps/v1"} {
		rec := serve(h, httptest.NewRequest(http.MethodGet, "/clusters/"+excludedID+p, nil))
		if rec.Code != http.StatusForbidden {
			t.Fatalf("%s: code = %d, want 403", p, rec.Code)
		}
		if ct := rec.Header().Get(headerContentType); ct != contentTypeTextPlain {
			t.Errorf("%s: Content-Type = %q, want %q", p, ct, contentTypeTextPlain)
		}
		if rec.Header().Get(headerNoSniff) != noSniff {
			t.Errorf("%s: no nosniff header", p)
		}
		body := rec.Body.String()
		for _, want := range []string{policyPrefix, "on hold (spec.hold)", "INC-42 rollback", "policy rule: scope.held-cluster"} {
			if !strings.Contains(body, want) {
				t.Errorf("%s: body %q does not contain %q", p, body, want)
			}
		}
	}
	if upstream.calls != 0 {
		t.Errorf("a refused discovery request reached the upstream")
	}
}

// A resource request keeps the JSON Status, which kubectl decodes.
func TestAScopeRefusalOnAResourcePathIsAStatus(t *testing.T) {
	h, _ := heldHandler(t)
	rec := serve(h, get(excludedID))
	if rec.Code != http.StatusForbidden {
		t.Fatalf("code = %d", rec.Code)
	}
	if s := decodeStatus(t, rec); !strings.Contains(s.Message, "policy rule: scope.held-cluster") {
		t.Errorf("message %q", s.Message)
	}
}

// An exclusion takes the same path.
func TestAnExclusionOnADiscoveryPathIsPlainText(t *testing.T) {
	h, _, _ := excludeHandler(t, scopeExcluding("payments-staging/us-central1/scratch"), hostID)
	rec := serve(h, httptest.NewRequest(http.MethodGet, "/clusters/"+excludedID+"/api", nil))
	if ct := rec.Header().Get(headerContentType); rec.Code != http.StatusForbidden || ct != contentTypeTextPlain ||
		!strings.Contains(rec.Body.String(), "policy rule: scope.excluded-cluster") {
		t.Errorf("code %d, Content-Type %q, body %q", rec.Code, ct, rec.Body.String())
	}
}

// The regression guard: a real client-go discovery client, as kubectl cluster-info
// uses, surfaces the hold message rather than "unknown".
func TestTheDiscoveryClientShowsTheHold(t *testing.T) {
	h, _ := heldHandler(t)
	srv := httptest.NewServer(h)
	t.Cleanup(srv.Close)
	client, err := discovery.NewDiscoveryClientForConfig(&rest.Config{Host: srv.URL + "/clusters/" + excludedID})
	if err != nil {
		t.Fatal(err)
	}
	_, err = client.ServerGroups()
	if err == nil {
		t.Fatal("ServerGroups succeeded against a held cluster")
	}
	for _, want := range []string{"on hold (spec.hold)", "INC-42 rollback", "scope.held-cluster"} {
		if !strings.Contains(err.Error(), want) {
			t.Errorf("discovery error %q does not contain %q", err, want)
		}
	}
	if _, err := client.ServerVersion(); err == nil || !strings.Contains(err.Error(), "scope.held-cluster") {
		t.Errorf("ServerVersion error = %v, want the hold", err)
	}
}
