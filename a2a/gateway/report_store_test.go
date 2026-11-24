package gateway

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// fakeReportStore answers latest with what answer returns for the audit,
// and records the audits it was asked for.
type fakeReportStore struct {
	mu     sync.Mutex
	asked  []string
	answer func(auditID string) (*reportEnvelope, error)
}

func (f *fakeReportStore) latest(_ context.Context, auditID string) (*reportEnvelope, error) {
	f.mu.Lock()
	f.asked = append(f.asked, auditID)
	f.mu.Unlock()
	return f.answer(auditID)
}

func (f *fakeReportStore) calls() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]string(nil), f.asked...)
}

// goodRunTask is the task id goodScheduleSubmit's run is minted under.
var goodRunTask = triggerTaskID(goodScheduleSubmit().IdempotencyKey, 0)

// storedEnvelope is the envelope goodScheduleSubmit's run's finish stores,
// stamped by the broker as that run's, decoded as the relay decodes the
// broker's answer.
func storedEnvelope(t *testing.T, auditID string, finished time.Time, report string) *reportEnvelope {
	t.Helper()
	return stampedEnvelope(t, reportStoreWriterSchedule+auditID+":"+goodRunTask, auditID, finished, report)
}

// stampedEnvelope is a scheduled-looking envelope with writer as the
// broker's stamp ("" for none).
func stampedEnvelope(t *testing.T, writer, auditID string, finished time.Time, report string) *reportEnvelope {
	t.Helper()
	env := envelopeOf(t, fmt.Sprintf(`{"audit_id":%q,"repo":"acme/gitops","started_at":%q,"finished_at":%q,"scheduled":true,"status":"OK","new_ids":["a"],%s}`,
		auditID, finished.Format(time.RFC3339Nano), finished.Format(time.RFC3339Nano), report))
	env.Writer = writer
	return env
}

// envelopeOf decodes raw as the relay decodes the broker's answer.
func envelopeOf(t *testing.T, raw string) *reportEnvelope {
	t.Helper()
	var env reportEnvelope
	if err := json.Unmarshal([]byte(raw), &env); err != nil {
		t.Fatal(err)
	}
	return &env
}

const (
	storedCard       = `{"schema":"kube-agents.chat/v1","kind":"report","title":"Compliance audit: 2 critical findings"}`
	storedReportJSON = `"report":{"schema":"kube-agents.report/v1","card":` + storedCard + `,"text":"Compliance audit: 2 critical findings — https://github.com/acme/gitops/issues/7","silent":false}`
)

// TestABuiltinAuditRunPostsItsStoredReport: a builtin audit's completed run
// posts the report its finish stored, card and text, in place of the
// model's trailing message, read by the run's own stream.
func TestABuiltinAuditRunPostsItsStoredReport(t *testing.T) {
	t.Parallel()
	tr := startTriggerRig(t, nil)
	home := &fakeHomeReporter{home: "C0HOME"}
	tr.g.setHomeReporters(home)
	store := &fakeReportStore{answer: func(id string) (*reportEnvelope, error) {
		return storedEnvelope(t, id, time.Now().UTC(), storedReportJSON), nil
	}}
	tr.g.reportStore = store
	taskID := runScheduled(t, tr, goodScheduleSubmit(), "Done! I ran the audit and everything went great.", nil)
	rep := waitForScheduledReport(t, tr.rig, "compliance-audit", taskID, "the posted record", func(r *ScheduledReport) bool { return r.Status == scheduledPosted })
	if rep.Source != scheduledSourceStore || !strings.HasPrefix(rep.Text, "Compliance audit: 2 critical findings") || string(rep.Chat) != storedCard {
		t.Fatalf("record = %+v, want the stored report", rep)
	}
	posts := home.calls()
	if len(posts) != 1 || posts[0] != rep.Text {
		t.Fatalf("posted %q, want the stored text", posts)
	}
	home.mu.Lock()
	chat := string(home.chats[0])
	home.mu.Unlock()
	if chat != storedCard {
		t.Fatalf("posted card %s, want the stored card", chat)
	}
	if asked := store.calls(); len(asked) != 1 || asked[0] != "compliance-audit" {
		t.Fatalf("asked the store for %q, want the run's own stream", asked)
	}
}

// TestAStoredSilentReportOfASilentScheduleIsNotPosted: the stored report's
// silent flag decides, not the trailing message.
func TestAStoredSilentReportOfASilentScheduleIsNotPosted(t *testing.T) {
	t.Parallel()
	tr := startTriggerRig(t, nil)
	home := &fakeHomeReporter{home: "C0HOME"}
	tr.g.setHomeReporters(home)
	tr.g.reportStore = &fakeReportStore{answer: func(id string) (*reportEnvelope, error) {
		return storedEnvelope(t, id, time.Now().UTC(), `"report":{"schema":"kube-agents.report/v1","text":"Compliance audit: clean.","silent":true}`), nil
	}}
	taskID := runScheduled(t, tr, goodScheduleSubmit(), "All clean, nothing to report!", nil)
	rep := waitForScheduledReport(t, tr.rig, "compliance-audit", taskID, "the silent record", func(r *ScheduledReport) bool { return true })
	if rep.Status != scheduledSilent || !rep.Silent || rep.Source != scheduledSourceStore {
		t.Fatalf("record = %+v, want silent from the store", rep)
	}
	waitForRunEnded(t, tr)
	if posts := home.calls(); len(posts) != 0 {
		t.Fatalf("posted %q", posts)
	}
}

// TestAStoredReportThatIsNotTheRunsFallsBackToItsResult: anything short of a
// report this run stored posts the run's result, as before the store.
func TestAStoredReportThatIsNotTheRunsFallsBackToItsResult(t *testing.T) {
	t.Parallel()
	now := time.Now().UTC()
	cases := map[string]func(id string) (*reportEnvelope, error){
		"none stored":           func(string) (*reportEnvelope, error) { return nil, errReportStoreMissing },
		"the store unreachable": func(string) (*reportEnvelope, error) { return nil, errors.New("dial tcp: connection refused") },
		"another audit's report": func(string) (*reportEnvelope, error) {
			return storedEnvelope(t, "cost-audit", now, storedReportJSON), nil
		},
		"an earlier run's": func(id string) (*reportEnvelope, error) {
			return stampedEnvelope(t, reportStoreWriterSchedule+id+":task-0123456789abcdef", id, now.Add(-24*time.Hour), storedReportJSON), nil
		},
		"a stamp naming another audit": func(id string) (*reportEnvelope, error) {
			return stampedEnvelope(t, reportStoreWriterSchedule+"cost-audit:"+goodRunTask, id, now, storedReportJSON), nil
		},
		"an on-demand run that says scheduled": func(id string) (*reportEnvelope, error) {
			// The shell's run passed finish --scheduled inside this run's
			// window: the body says scheduled, the broker says shell.
			return stampedEnvelope(t, "shell", id, now, storedReportJSON), nil
		},
		"no stamp": func(id string) (*reportEnvelope, error) {
			return stampedEnvelope(t, "", id, now, storedReportJSON), nil
		},
		"a body that claims the stamp": func(id string) (*reportEnvelope, error) {
			return envelopeOf(t, fmt.Sprintf(`{"audit_id":%q,"writer":%q,"X-Report-Writer":%q,%s}`, id, reportStoreWriterSchedule+id+":"+goodRunTask, reportStoreWriterSchedule+id+":"+goodRunTask, storedReportJSON)), nil
		},
		"no report object": func(id string) (*reportEnvelope, error) { return storedEnvelope(t, id, now, `"x":1`), nil },
		"another schema": func(id string) (*reportEnvelope, error) {
			return storedEnvelope(t, id, now, `"report":{"schema":"kube-agents.report/v2","text":"v2"}`), nil
		},
		"an empty text": func(id string) (*reportEnvelope, error) {
			return storedEnvelope(t, id, now, `"report":{"schema":"kube-agents.report/v1","text":"  "}`), nil
		},
	}
	for name, answer := range cases {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			tr := startTriggerRig(t, nil)
			home := &fakeHomeReporter{home: "C0HOME"}
			tr.g.setHomeReporters(home)
			tr.g.reportStore = &fakeReportStore{answer: answer}
			taskID := runScheduled(t, tr, goodScheduleSubmit(), "Two findings.", nil)
			rep := waitForScheduledReport(t, tr.rig, "compliance-audit", taskID, "the posted record", func(r *ScheduledReport) bool { return r.Status == scheduledPosted })
			if rep.Source != "" || rep.Text != "Two findings." || len(rep.Chat) != 0 {
				t.Fatalf("record = %+v, want the run's result", rep)
			}
			if posts := home.calls(); len(posts) != 1 || posts[0] != "Two findings." {
				t.Fatalf("posted %q", posts)
			}
		})
	}
}

// TestOnlyABuiltinScheduleRunReadsTheStore: a run with no builtin trigger
// stream (a user schedule, an entry from before the fields, a gateway with
// no reader) never reads the store, and its report is its result.
func TestOnlyABuiltinScheduleRunReadsTheStore(t *testing.T) {
	t.Parallel()
	store := &fakeReportStore{answer: func(id string) (*reportEnvelope, error) {
		return storedEnvelope(t, id, time.Now().UTC(), storedReportJSON), nil
	}}
	g := &Gateway{reportStore: store, scheduleClock: time.Now, log: slog.New(slog.NewTextHandler(io.Discard, nil))}
	started := time.Now().UTC().Add(-time.Minute)
	for name, ref := range map[string]TaskRef{
		"a user schedule":         {TriggerOrigin: "user", TriggerStream: "compliance-audit", StartedAt: started, ID: goodRunTask},
		"an entry with no stream": {TriggerOrigin: lib.TriggerOriginBuiltin, StartedAt: started, ID: goodRunTask},
		"an entry with no origin": {TriggerStream: "compliance-audit", StartedAt: started, ID: goodRunTask},
	} {
		if got := g.storedReportFor(context.Background(), ref, nil); got != nil {
			t.Errorf("%s: read %+v from the store", name, got)
		}
	}
	if asked := store.calls(); len(asked) != 0 {
		t.Fatalf("asked the store for %q", asked)
	}
	builtin := TaskRef{TriggerOrigin: lib.TriggerOriginBuiltin, TriggerStream: "compliance-audit", StartedAt: started, ID: goodRunTask}
	if got := g.storedReportFor(context.Background(), builtin, nil); got == nil || string(got.Card) != storedCard {
		t.Fatalf("builtin run read %+v, want the stored report", got)
	}
	g.reportStore = nil
	if got := g.storedReportFor(context.Background(), builtin, nil); got != nil {
		t.Fatalf("no reader read %+v", got)
	}
}

// TestAStoredCardOverItsCapPostsTheTextAlone: the card is bounded on the
// run's record; one over the cap is dropped and the text stands.
func TestAStoredCardOverItsCapPostsTheTextAlone(t *testing.T) {
	t.Parallel()
	big := `{"schema":"kube-agents.chat/v1","kind":"report","title":"` + strings.Repeat("x", reportStoreCardMax) + `"}`
	g := &Gateway{scheduleClock: time.Now, log: slog.New(slog.NewTextHandler(io.Discard, nil))}
	g.reportStore = &fakeReportStore{answer: func(id string) (*reportEnvelope, error) {
		return storedEnvelope(t, id, time.Now().UTC(), `"report":{"schema":"kube-agents.report/v1","card":`+big+`,"text":"Two findings."}`), nil
	}}
	got := g.storedReportFor(context.Background(), TaskRef{TriggerOrigin: lib.TriggerOriginBuiltin, TriggerStream: "compliance-audit", StartedAt: time.Now().Add(-time.Minute), ID: goodRunTask}, nil)
	if got == nil || got.Text != "Two findings." || len(got.Card) != 0 {
		t.Fatalf("read %+v, want the text with no card", got)
	}
}

// TestTheBrokerReaderAsksForThePublishKeyWithAFreshToken: the reader names
// no repository (the broker resolves `publish`), presents the projected
// token re-read on every call, and maps the broker's answers.
func TestTheBrokerReaderAsksForThePublishKeyWithAFreshToken(t *testing.T) {
	t.Parallel()
	var mu sync.Mutex
	var seen []string
	status, body := http.StatusOK, `{"audit_id":"compliance-audit","finished_at":"2026-11-16T12:00:00.5+00:00","report":{"schema":"kube-agents.report/v1","text":"t"}}`
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		seen = append(seen, r.Method+" "+r.URL.EscapedPath()+" "+r.Header.Get("Authorization"))
		st, b := status, body
		mu.Unlock()
		if st == http.StatusTemporaryRedirect {
			w.Header().Set("Location", "/elsewhere")
		}
		w.Header().Set("X-Report-Writer", "builtin-schedule:compliance-audit:task-abc")
		w.WriteHeader(st)
		_, _ = io.WriteString(w, b)
	}))
	t.Cleanup(srv.Close)
	tokenFile := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(tokenFile, []byte("tok-1\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	s := newBrokerReportStore(srv.URL+"/", tokenFile)
	if s == nil {
		t.Fatal("no reader for a URL and a token file")
	}
	env, err := s.latest(context.Background(), "compliance-audit")
	if err != nil || env.AuditID != "compliance-audit" || env.Report == nil || env.Report.Text != "t" || env.Writer != "builtin-schedule:compliance-audit:task-abc" {
		t.Fatalf("latest = %+v, %v", env, err)
	}
	if err := os.WriteFile(tokenFile, []byte("tok-2"), 0o600); err != nil {
		t.Fatal(err)
	}
	mu.Lock()
	status = http.StatusNotFound
	mu.Unlock()
	if _, err := s.latest(context.Background(), "compliance-audit"); !errors.Is(err, errReportStoreMissing) {
		t.Fatalf("404 = %v, want errReportStoreMissing", err)
	}
	for _, st := range []int{http.StatusForbidden, http.StatusBadGateway, http.StatusServiceUnavailable, http.StatusGatewayTimeout, http.StatusTemporaryRedirect} {
		mu.Lock()
		status = st
		mu.Unlock()
		if _, err := s.latest(context.Background(), "compliance-audit"); err == nil || errors.Is(err, errReportStoreMissing) {
			t.Fatalf("%d = %v, want a failure", st, err)
		}
	}
	mu.Lock()
	status, body = http.StatusOK, `{"audit_id":"`+strings.Repeat("x", reportStoreBodyMax)+`"}`
	mu.Unlock()
	if _, err := s.latest(context.Background(), "compliance-audit"); err == nil {
		t.Fatal("an envelope over the cap was read")
	}
	mu.Lock()
	defer mu.Unlock()
	want := []string{
		"GET /v1/audit-reports/compliance-audit/publish/latest Bearer tok-1",
		"GET /v1/audit-reports/compliance-audit/publish/latest Bearer tok-2",
	}
	if len(seen) != 8 {
		t.Fatalf("%d requests, want 8 (a redirect is not followed)", len(seen))
	}
	for i, w := range want {
		if seen[i] != w {
			t.Fatalf("request %d = %q, want %q", i, seen[i], w)
		}
	}
}

// TestNoBrokerReaderWithoutItsURLAndToken: an install the operator renders
// no a2a-reports pair for reads nothing, and an empty token file is no
// token.
func TestNoBrokerReaderWithoutItsURLAndToken(t *testing.T) {
	t.Parallel()
	if newBrokerReportStore("", "/var/run/secrets/a2a-reports/token") != nil || newBrokerReportStore("http://broker:8080", " ") != nil {
		t.Fatal("a reader with half its configuration")
	}
	empty := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(empty, []byte("\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := newBrokerReportStore("http://127.0.0.1:1", empty).latest(context.Background(), "compliance-audit"); err == nil || !strings.Contains(err.Error(), "empty") {
		t.Fatalf("empty token = %v", err)
	}
}

// TestASilentStoredReportNeedsNoText: a clean run's report may carry no
// text; it is silent, not the model's chatter, and a null card is no card.
func TestASilentStoredReportNeedsNoText(t *testing.T) {
	t.Parallel()
	g := &Gateway{scheduleClock: time.Now, log: slog.New(slog.NewTextHandler(io.Discard, nil))}
	g.reportStore = &fakeReportStore{answer: func(id string) (*reportEnvelope, error) {
		return storedEnvelope(t, id, time.Now().UTC(), `"report":{"schema":"kube-agents.report/v1","card":null,"text":"","silent":true}`), nil
	}}
	got := g.storedReportFor(context.Background(), TaskRef{TriggerOrigin: lib.TriggerOriginBuiltin, TriggerStream: "compliance-audit", StartedAt: time.Now().Add(-time.Minute), ID: goodRunTask}, nil)
	if got == nil || !got.Silent || got.Text != scheduledSilentMarker || len(got.Card) != 0 {
		t.Fatalf("read %+v, want a silent report with no card", got)
	}
}

// TestARetriedScheduledPostKeepsItsFirstStoredReport: the store is read on
// the run's first try only; a retry posts the record's report, not what
// the store holds by then.
func TestARetriedScheduledPostKeepsItsFirstStoredReport(t *testing.T) {
	t.Parallel()
	tr := startTriggerRig(t, nil)
	tr.g.scheduledRetry = 100 * time.Millisecond
	home := &fakeHomeReporter{home: "C0HOME", fail: func(call int) bool { return call == 1 }}
	tr.g.setHomeReporters(home)
	var mu sync.Mutex
	reads := 0
	store := &fakeReportStore{answer: func(id string) (*reportEnvelope, error) {
		mu.Lock()
		reads++
		n := reads
		mu.Unlock()
		return storedEnvelope(t, id, time.Now().UTC(), fmt.Sprintf(`"report":{"schema":"kube-agents.report/v1","text":"read %d"}`, n)), nil
	}}
	tr.g.reportStore = store
	taskID := runScheduled(t, tr, goodScheduleSubmit(), "Two findings.", nil)
	rep := waitForScheduledReport(t, tr.rig, "compliance-audit", taskID, "the posted record", func(r *ScheduledReport) bool { return r.Status == scheduledPosted })
	posts := home.calls()
	if len(posts) != 2 || posts[0] != "read 1" || posts[1] != "read 1" || rep.Text != "read 1" {
		t.Fatalf("posted %q, record %q; want the first read both times", posts, rep.Text)
	}
	if asked := store.calls(); len(asked) != 1 {
		t.Fatalf("read the store %d times, want once", len(asked))
	}
}
