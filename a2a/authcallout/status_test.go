package authcallout

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func get(t *testing.T, h http.Handler, path string) (int, string) {
	t.Helper()
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, path, nil))
	return rec.Code, rec.Body.String()
}

// The DoD item this endpoint exists for: the served map version has to be
// checkable against the running system, not inferred from the rendered object.
func TestStatusReportsTheServedVersionAndUsers(t *testing.T) {
	s := quietStore()
	if err := s.Update([]byte(mapWithVersion("abc123", "gateway"))); err != nil {
		t.Fatalf("Update: %v", err)
	}

	code, body := get(t, StatusHandler(s, nil), StatusPath)
	if code != http.StatusOK {
		t.Fatalf("status = %d, want 200", code)
	}
	var got Status
	if err := json.Unmarshal([]byte(body), &got); err != nil {
		t.Fatalf("status body does not parse: %v\n%s", err, body)
	}
	if got.Version != "abc123" {
		t.Errorf("Version = %q, want abc123", got.Version)
	}
	if !got.Serving {
		t.Error("Serving is false while a map is loaded")
	}
	if len(got.Users) != 1 || got.Users[0] != "gateway" {
		t.Errorf("Users = %v, want [gateway]", got.Users)
	}
	if got.LastError != "" {
		t.Errorf("LastError = %q, want empty", got.LastError)
	}
}

// Readiness is what makes "every replica ready" mean "every replica serving the
// rendered map", which is the only thing that makes BusCredentialsReady true
// rather than hopeful. A callout with no map must fail it.
func TestReadinessFailsUntilAMapIsServed(t *testing.T) {
	s := quietStore()
	h := StatusHandler(s, nil)

	code, body := get(t, h, ReadyPath)
	if code != http.StatusServiceUnavailable {
		t.Errorf("readiness = %d with no map, want 503", code)
	}
	if !strings.Contains(body, "no identity map") {
		t.Errorf("readiness body = %q, want it to say why", body)
	}

	if err := s.Update([]byte(mapWithVersion("v9", "gateway"))); err != nil {
		t.Fatalf("Update: %v", err)
	}
	code, body = get(t, h, ReadyPath)
	if code != http.StatusOK {
		t.Errorf("readiness = %d with a map served, want 200", code)
	}
	if !strings.Contains(body, "v9") {
		t.Errorf("readiness body = %q, want it to name the served version", body)
	}
}

// A refused update must be visible on both surfaces. The state it produces —
// serving an older version than the operator rendered — otherwise looks exactly
// like nothing having been rendered at all.
func TestARefusedUpdateIsVisibleWhileStillServing(t *testing.T) {
	s := quietStore()
	if err := s.Update([]byte(mapWithVersion("good", "gateway"))); err != nil {
		t.Fatalf("Update: %v", err)
	}
	_ = s.Update([]byte(`{"version":"broken","identities":[]}`))

	h := StatusHandler(s, nil)
	code, _ := get(t, h, ReadyPath)
	if code != http.StatusOK {
		t.Errorf("readiness = %d, want 200: the previous map is still being served", code)
	}

	_, body := get(t, h, StatusPath)
	var got Status
	if err := json.Unmarshal([]byte(body), &got); err != nil {
		t.Fatalf("status body does not parse: %v", err)
	}
	if got.Version != "good" {
		t.Errorf("Version = %q, want the previously served good", got.Version)
	}
	if got.LastError == "" {
		t.Error("the refused update is invisible on the status endpoint")
	}
}

// Liveness must not consult the map. A callout that cannot reach the API server
// should be taken out of the Service, not killed: restarting it throws away the
// map it already had and cannot make the API server answer sooner.
func TestLivenessDoesNotDependOnTheMap(t *testing.T) {
	s := quietStore()
	if code, _ := get(t, StatusHandler(s, nil), LivePath); code != http.StatusOK {
		t.Errorf("liveness = %d with no map, want 200", code)
	}
}

// Readiness needs both halves, and the map was the only one it had. A callout
// detached from the bus answers nothing, and answers it while holding a
// perfectly good map -- so nothing about the map can report that state.
func TestReadinessFailsWhenDetachedFromTheBusEvenWithAMap(t *testing.T) {
	s := quietStore()
	if err := s.Update([]byte(mapWithVersion("v9", "gateway"))); err != nil {
		t.Fatalf("Update: %v", err)
	}
	attached := true
	h := StatusHandler(s, func() bool { return attached })

	// The control. Without it a 503 below is consistent with a probe that is
	// simply never ready.
	if code, _ := get(t, h, ReadyPath); code != http.StatusOK {
		t.Fatalf("readiness = %d with a map and a live connection, want 200", code)
	}

	attached = false
	code, body := get(t, h, ReadyPath)
	if code != http.StatusServiceUnavailable {
		t.Errorf("readiness = %d while detached from the bus, want 503", code)
	}
	if !strings.Contains(body, "bus") {
		t.Errorf("readiness body = %q; it must name the bus, since a missing map produces "+
			"the same symptom for every client and is fixed somewhere else", body)
	}
	if !s.Ready() {
		t.Error("precondition lost: the store stopped serving, so this proves nothing " +
			"about the connection")
	}
}

// Liveness stays out of it, for the same reason it stays out of the map: the
// process is running and the operator's answer to a detached callout is a
// restart driven by the process exiting, not by the kubelet killing a pod
// mid-TokenReview.
func TestLivenessDoesNotDependOnTheBusEither(t *testing.T) {
	s := quietStore()
	h := StatusHandler(s, func() bool { return false })
	if code, _ := get(t, h, LivePath); code != http.StatusOK {
		t.Errorf("liveness = %d while detached from the bus, want 200", code)
	}
}
