package k8sapiproxy

import (
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

const (
	excludedID = "gke_payments-staging_us-central1_scratch"
	allowedID  = "gke_payments-staging_us-central1_other"
	hostID     = "gke_kagents-dev_us-east4_mgmt"
)

func writeScope(t *testing.T, path, body string) {
	t.Helper()
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	// A distinct mtime per write, as a ConfigMap update's new file has.
	stamp := time.Now().Add(time.Duration(len(body)) * time.Millisecond)
	if err := os.Chtimes(path, stamp, stamp); err != nil {
		t.Fatal(err)
	}
}

func scopeExcluding(entries ...string) string {
	clusters := make([]string, 0, len(entries))
	for _, entry := range entries {
		parts := strings.Split(entry, "/")
		clusters = append(clusters, `{"projectId":"`+parts[0]+`","location":"`+parts[1]+`","clusterName":"`+parts[2]+`"}`)
	}
	return `{"present":true,"projects":[],"exclude":{"projects":[],"clusters":[` + strings.Join(clusters, ",") + `]}}`
}

func excludeHandler(t *testing.T, scope, host string) (http.Handler, *recordingTransport, string) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "scope.json")
	if scope != "" {
		writeScope(t, path, scope)
	}
	h, upstream := newTestHandler(t, Config{Exclusions: NewExclusions(path, host)})
	return h, upstream, path
}

func get(clusterID string) *http.Request {
	return httptest.NewRequest(http.MethodGet, "/clusters/"+clusterID+"/api/v1/namespaces/default/pods", nil)
}

func assertExcluded(t *testing.T, h http.Handler, upstream *recordingTransport, clusterID string, reason RejectionReason) {
	t.Helper()
	before := upstream.calls
	rec := serve(h, get(clusterID))
	if rec.Code != http.StatusForbidden {
		t.Fatalf("%s: code = %d, want 403: %s", clusterID, rec.Code, rec.Body.String())
	}
	if s := decodeStatus(t, rec); !strings.Contains(s.Message, "policy rule: "+string(reason)) {
		t.Errorf("%s: message %q does not name %s", clusterID, s.Message, reason)
	}
	if upstream.calls != before {
		t.Errorf("%s: a refused request reached the upstream", clusterID)
	}
}

func assertForwarded(t *testing.T, h http.Handler, upstream *recordingTransport, clusterID string) {
	t.Helper()
	before := upstream.calls
	if rec := serve(h, get(clusterID)); rec.Code != http.StatusOK {
		t.Fatalf("%s: code = %d, want 200: %s", clusterID, rec.Code, rec.Body.String())
	}
	if upstream.calls != before+1 {
		t.Errorf("%s: the request didn't reach the upstream", clusterID)
	}
}

func TestAnExcludedClusterIsRefusedBeforeTheUpstream(t *testing.T) {
	h, upstream, _ := excludeHandler(t, scopeExcluding("payments-staging/us-central1/scratch"), hostID)
	assertExcluded(t, h, upstream, excludedID, ReasonExcludedCluster)
	assertForwarded(t, h, upstream, allowedID)
	// A zonal name never matches a regional entry.
	assertForwarded(t, h, upstream, "gke_payments-staging_us-central1-a_scratch")
	// A watch is refused at its first request like any read.
	before := upstream.calls
	rec := serve(h, httptest.NewRequest(http.MethodGet, "/clusters/"+excludedID+"/api/v1/pods?watch=true", nil))
	if rec.Code != http.StatusForbidden || upstream.calls != before {
		t.Errorf("watch on an excluded cluster: code %d, upstream calls %d -> %d", rec.Code, before, upstream.calls)
	}
}

func TestInClusterIsCheckedAsTheHostCluster(t *testing.T) {
	h, upstream, _ := excludeHandler(t, scopeExcluding("kagents-dev/us-east4/mgmt"), hostID)
	assertExcluded(t, h, upstream, InClusterID, ReasonExcludedCluster)
	assertExcluded(t, h, upstream, hostID, ReasonExcludedCluster)

	other, otherUpstream, _ := excludeHandler(t, scopeExcluding("payments-staging/us-central1/scratch"), hostID)
	assertForwarded(t, other, otherUpstream, InClusterID)
}

func TestInClusterIsRefusedWhileAnyClusterIsExcludedAndTheHostIsUnnamed(t *testing.T) {
	h, upstream, _ := excludeHandler(t, scopeExcluding("payments-staging/us-central1/scratch"), "")
	assertExcluded(t, h, upstream, InClusterID, ReasonExcludedCluster)
	none, noneUpstream, _ := excludeHandler(t, scopeExcluding(), "")
	assertForwarded(t, none, noneUpstream, InClusterID)
}

func TestProjectNumbersAreRefusedWhileAnyClusterIsExcluded(t *testing.T) {
	h, upstream, _ := excludeHandler(t, scopeExcluding("payments-staging/us-central1/scratch"), hostID)
	assertExcluded(t, h, upstream, "gke_123456789012_us-central1_scratch", ReasonExcludedCluster)
	// An entry spelled by number refuses its location and name in any project.
	numbered, numberedUpstream, _ := excludeHandler(t, scopeExcluding("123456789012/us-central1/scratch"), hostID)
	assertExcluded(t, numbered, numberedUpstream, excludedID, ReasonExcludedCluster)
	assertForwarded(t, numbered, numberedUpstream, allowedID)
}

func TestAnUnreadableScopeRefusesEveryCluster(t *testing.T) {
	missing, missingUpstream, _ := excludeHandler(t, "", hostID)
	assertExcluded(t, missing, missingUpstream, allowedID, ReasonExcludeUnreadable)
	for _, body := range []string{
		"{not json",
		`{"exclude":{"clusters":[{"projectId":"payments-staging","location":"us-central1"}]}}`,
		`{"exclude":{"clusters":[{"projectId":"payments-staging","location":"us-central1","clusterName":"Scratch"}]}}`,
		`{"exclude":{"clusters":{}}}`,
	} {
		h, upstream, _ := excludeHandler(t, body, hostID)
		assertExcluded(t, h, upstream, allowedID, ReasonExcludeUnreadable)
	}
}

func TestNoScopeFileExcludesNothing(t *testing.T) {
	h, upstream := newTestHandler(t, Config{Exclusions: NewExclusions("", "")})
	assertForwarded(t, h, upstream, excludedID)
	plain, plainUpstream := newTestHandler(t, Config{})
	assertForwarded(t, plain, plainUpstream, excludedID)
}

func TestAnEditToTheScopeAppliesOnTheNextRequest(t *testing.T) {
	h, upstream, path := excludeHandler(t, scopeExcluding(), hostID)
	assertForwarded(t, h, upstream, excludedID)
	writeScope(t, path, scopeExcluding("payments-staging/us-central1/scratch"))
	assertExcluded(t, h, upstream, excludedID, ReasonExcludedCluster)
	writeScope(t, path, scopeExcluding())
	assertForwarded(t, h, upstream, excludedID)
}

// A refusal for an excluded cluster is counted under its own reason, not
// "other", so the rejected-requests metric shows the exclusion working.
func TestAnExcludedClusterRefusalIsCountedUnderItsReason(t *testing.T) {
	path := filepath.Join(t.TempDir(), "scope.json")
	writeScope(t, path, scopeExcluding("payments-staging/us-central1/scratch"))
	metrics := NewMetrics()
	h, upstream := newTestHandler(t, Config{Exclusions: NewExclusions(path, hostID), Metrics: metrics})
	assertExcluded(t, h, upstream, excludedID, ReasonExcludedCluster)
	if exposition := scrapeMetricsText(t, metrics); !strings.Contains(exposition, `reason="scope.excluded-cluster"`) {
		t.Errorf("no rejected-requests sample under reason scope.excluded-cluster:\n%s", exposition)
	}
}
