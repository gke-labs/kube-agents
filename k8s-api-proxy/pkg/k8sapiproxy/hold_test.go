package k8sapiproxy

import (
	"net/http"
	"path/filepath"
	"strings"
	"testing"
)

func holdOf(all bool, reason string, entries ...string) string {
	clusters := make([]string, 0, len(entries))
	for _, entry := range entries {
		parts := strings.Split(entry, "/")
		clusters = append(clusters, `{"projectId":"`+parts[0]+`","location":"`+parts[1]+`","clusterName":"`+parts[2]+`"}`)
	}
	doc := `{"clusters":[` + strings.Join(clusters, ",") + `]`
	if all {
		doc += `,"all":true`
	}
	if reason != "" {
		doc += `,"reason":"` + reason + `"`
	}
	return doc + "}"
}

func holdHandler(t *testing.T, hold, host string) (http.Handler, *recordingTransport, string) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "hold.json")
	if hold != "" {
		writeScope(t, path, hold)
	}
	h, upstream := newTestHandler(t, Config{Holds: NewHolds(path, host)})
	return h, upstream, path
}

func TestAHeldClusterIsRefusedWithTheHoldAndItsReason(t *testing.T) {
	h, upstream, _ := holdHandler(t, holdOf(false, "INC-42 rollback in progress", "payments-staging/us-central1/scratch"), hostID)
	assertExcluded(t, h, upstream, excludedID, ReasonHeldCluster)
	rec := serve(h, get(excludedID))
	msg := decodeStatus(t, rec).Message
	for _, want := range []string{"spec.hold", "INC-42 rollback in progress", "report this and stop", "scratch (payments-staging, us-central1)"} {
		if !strings.Contains(msg, want) {
			t.Errorf("message %q does not contain %q", msg, want)
		}
	}
	assertForwarded(t, h, upstream, allowedID)
	assertForwarded(t, h, upstream, "gke_payments-staging_us-central1-a_scratch")
	assertForwarded(t, h, upstream, InClusterID)
	assertForwarded(t, h, upstream, "kind-local")
}

func TestHoldAllRefusesEveryCluster(t *testing.T) {
	h, upstream, _ := holdHandler(t, holdOf(true, "change freeze"), hostID)
	for _, id := range []string{excludedID, allowedID, hostID, InClusterID, "kind-local"} {
		assertExcluded(t, h, upstream, id, ReasonHeldCluster)
	}
	if msg := decodeStatus(t, serve(h, get(allowedID))).Message; !strings.Contains(msg, "change freeze") {
		t.Errorf("message %q does not carry the reason", msg)
	}
}

func TestInClusterIsHeldAsTheHostCluster(t *testing.T) {
	h, upstream, _ := holdHandler(t, holdOf(false, "", "kagents-dev/us-east4/mgmt"), hostID)
	assertExcluded(t, h, upstream, InClusterID, ReasonHeldCluster)
	unnamed, unnamedUpstream, _ := holdHandler(t, holdOf(false, "", "payments-staging/us-central1/scratch"), "")
	assertExcluded(t, unnamed, unnamedUpstream, InClusterID, ReasonHeldCluster)
	none, noneUpstream, _ := holdHandler(t, "{}", "")
	assertForwarded(t, none, noneUpstream, InClusterID)
}

func TestProjectNumbersAreRefusedWhileAnyClusterIsHeld(t *testing.T) {
	h, upstream, _ := holdHandler(t, holdOf(false, "", "payments-staging/us-central1/scratch"), hostID)
	assertExcluded(t, h, upstream, "gke_123456789012_us-central1_other", ReasonHeldCluster)
	numbered, numberedUpstream, _ := holdHandler(t, holdOf(false, "", "123456789012/us-central1/scratch"), hostID)
	assertExcluded(t, numbered, numberedUpstream, excludedID, ReasonHeldCluster)
	assertForwarded(t, numbered, numberedUpstream, allowedID)
}

func TestAnUnreadableHoldRefusesEveryCluster(t *testing.T) {
	missing, missingUpstream, _ := holdHandler(t, "", hostID)
	assertExcluded(t, missing, missingUpstream, allowedID, ReasonHoldUnreadable)
	for _, body := range []string{
		"{not json",
		`{"clusters":[{"projectId":"payments-staging","location":"us-central1"}]}`,
		`{"clusters":[{"projectId":"payments-staging","location":"us-central1","clusterName":"Scratch"}]}`,
		`{"clusters":{}}`,
		`{"all":"yes"}`,
	} {
		h, upstream, _ := holdHandler(t, body, hostID)
		assertExcluded(t, h, upstream, allowedID, ReasonHoldUnreadable)
	}
}

func TestAnEmptyHoldOrNoHoldFileHoldsNothing(t *testing.T) {
	for _, body := range []string{"{}", "{}\n", `{"clusters":[]}`, `{"all":false,"reason":"lifted"}`} {
		h, upstream, _ := holdHandler(t, body, hostID)
		assertForwarded(t, h, upstream, excludedID)
		assertForwarded(t, h, upstream, InClusterID)
	}
	h, upstream := newTestHandler(t, Config{Holds: NewHolds("", "")})
	assertForwarded(t, h, upstream, excludedID)
}

// The kill switch's point: a hold applies, and lifts, on the next request, with no restart.
func TestAHoldAppliesAndLiftsOnTheNextRequest(t *testing.T) {
	h, upstream, path := holdHandler(t, "{}", hostID)
	assertForwarded(t, h, upstream, excludedID)
	writeScope(t, path, holdOf(false, "INC-1", "payments-staging/us-central1/scratch"))
	assertExcluded(t, h, upstream, excludedID, ReasonHeldCluster)
	writeScope(t, path, holdOf(true, "INC-1"))
	assertExcluded(t, h, upstream, allowedID, ReasonHeldCluster)
	writeScope(t, path, "{}")
	assertForwarded(t, h, upstream, excludedID)
	assertForwarded(t, h, upstream, allowedID)
}

// Exclusions and holds are independent: each refuses under its own rule.
func TestExclusionsAndHoldsBothApply(t *testing.T) {
	dir := t.TempDir()
	scope, hold := filepath.Join(dir, "scope.json"), filepath.Join(dir, "hold.json")
	writeScope(t, scope, scopeExcluding("payments-staging/us-central1/scratch"))
	writeScope(t, hold, holdOf(false, "", "payments-staging/us-central1/other"))
	h, upstream := newTestHandler(t, Config{Exclusions: NewExclusions(scope, hostID), Holds: NewHolds(hold, hostID)})
	assertExcluded(t, h, upstream, excludedID, ReasonExcludedCluster)
	assertExcluded(t, h, upstream, allowedID, ReasonHeldCluster)
	assertForwarded(t, h, upstream, "gke_payments-staging_us-central1_third")
}

func TestAHeldClusterRefusalIsCountedUnderItsReason(t *testing.T) {
	path := filepath.Join(t.TempDir(), "hold.json")
	writeScope(t, path, holdOf(false, "", "payments-staging/us-central1/scratch"))
	metrics := NewMetrics()
	h, upstream := newTestHandler(t, Config{Holds: NewHolds(path, hostID), Metrics: metrics})
	assertExcluded(t, h, upstream, excludedID, ReasonHeldCluster)
	if exposition := scrapeMetricsText(t, metrics); !strings.Contains(exposition, `reason="scope.held-cluster"`) {
		t.Errorf("no rejected-requests sample under reason scope.held-cluster:\n%s", exposition)
	}
}

// The reason is the operator's text: kept to one printable line of at most 256 runes,
// so it can't break the message's last line ("policy rule: ...") a client reads.
func TestTheHoldReasonIsPrintableAndCapped(t *testing.T) {
	path := filepath.Join(t.TempDir(), "hold.json")
	writeScope(t, path, `{"all":true,"reason":"line one\nline two\u001b[31m `+strings.Repeat("é", 400)+`"}`)
	state, err := parseHolds(path)
	if err != nil {
		t.Fatal(err)
	}
	if strings.ContainsAny(state.reason, "\n\x1b") {
		t.Errorf("reason %q keeps a control character", state.reason)
	}
	if !strings.HasPrefix(state.reason, "line oneline two[31m ") {
		t.Errorf("reason = %q", state.reason)
	}
	if n := len([]rune(state.reason)); n != holdReasonMaxRunes {
		t.Errorf("reason is %d runes, want %d", n, holdReasonMaxRunes)
	}
	if normaliseHoldReason("  INC-1  ") != "INC-1" {
		t.Error("surrounding space is not trimmed")
	}
}
