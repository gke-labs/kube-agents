package hermesbridge

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/nats-io/nats.go"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// incidentKV is session-kv's POST /v1/incidents as the bridge calls it,
// answering status for each write and recording the bodies.
type incidentKV struct {
	mu     sync.Mutex
	status int
	bodies []map[string]any
	auth   []string
	// rows is the table as session-kv keeps it: a write with replace
	// true replaces the thread's row, any other keeps the first.
	rows map[string]string
}

func (k *incidentKV) serve(t *testing.T) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || r.URL.Path != incidentStorePath {
			http.NotFound(w, r)
			return
		}
		raw, _ := io.ReadAll(r.Body)
		var body map[string]any
		_ = json.Unmarshal(raw, &body)
		k.mu.Lock()
		if k.rows == nil {
			k.rows = map[string]string{}
		}
		row := fmt.Sprint(body["chat_id"], "|", body["thread_id"])
		if _, have := k.rows[row]; !have || body["replace"] == true {
			k.rows[row], _ = body["report"].(string)
		}
		k.bodies = append(k.bodies, body)
		k.auth = append(k.auth, r.Header.Get("Authorization"))
		status := k.status
		k.mu.Unlock()
		if status == 0 {
			status = http.StatusOK
		}
		w.WriteHeader(status)
		_, _ = io.WriteString(w, `{"status":"stored"}`)
	}))
	t.Cleanup(srv.Close)
	return srv
}

func (k *incidentKV) writes() []map[string]any {
	k.mu.Lock()
	defer k.mu.Unlock()
	return append([]map[string]any(nil), k.bodies...)
}

// incidentBridge is a bridge whose session-kv is kv and which serves the
// Slack home C0HOME and the Google Chat home spaces/HOME.
func incidentBridge(t *testing.T, kv *incidentKV) *Bridge {
	t.Helper()
	srv := kv.serve(t)
	return &Bridge{cfg: Config{RouteURL: srv.URL, RouteKey: "kv-key", Logger: discardLogger(),
		IncidentSlackHome: "C0HOME", IncidentGchatHome: "spaces/HOME"}, routeClient: http.DefaultClient}
}

func keepRequest(t *testing.T, keep lib.IncidentKeep) []byte {
	t.Helper()
	data, err := json.Marshal(keep)
	if err != nil {
		t.Fatal(err)
	}
	return data
}

// TestAnIncidentIsStoredWhereItsThreadsReplyLooksForIt: a report posted in a
// home-channel thread is stored with session-kv's POST /v1/incidents, with the
// bearer and replace: true (the day's newest report is the one a reply reads), under the address the bridge's own lookup reads for a reply in that
// thread: on Slack the channel and the bare ts, on Google Chat the space and
// the thread's full name.
func TestAnIncidentIsStoredWhereItsThreadsReplyLooksForIt(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct {
		name             string
		keep             lib.IncidentKeep
		replyKey         string
		wantChat, wantTh string
	}{
		{"slack bare ts", lib.IncidentKeep{Platform: lib.NotifyPlatformSlack, ChatID: "C0HOME", ThreadID: "1763100000.000100"},
			"slack:C0HOME/1763100000.000100", "C0HOME", "1763100000.000100"},
		{"slack qualified ts", lib.IncidentKeep{Platform: lib.NotifyPlatformSlack, ChatID: "C0HOME", ThreadID: "C0HOME/1763100000.000100"},
			"slack:C0HOME/1763100000.000100", "C0HOME", "1763100000.000100"},
		{"gchat thread name", lib.IncidentKeep{Platform: lib.NotifyPlatformGchat, ChatID: "spaces/HOME", ThreadID: "spaces/HOME/threads/t1"},
			"gchat:spaces/HOME/threads/t1", "spaces/HOME", "spaces/HOME/threads/t1"},
		{"gchat bare thread", lib.IncidentKeep{Platform: lib.NotifyPlatformGchat, ChatID: "spaces/HOME", ThreadID: "t1"},
			"gchat:spaces/HOME/threads/t1", "spaces/HOME", "spaces/HOME/threads/t1"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			kv := &incidentKV{}
			b := incidentBridge(t, kv)
			tc.keep.Report = "Two findings."
			if err := b.keepIncident(keepRequest(t, tc.keep)); err != nil {
				t.Fatal(err)
			}
			got := kv.writes()
			if len(got) != 1 || got[0]["chat_id"] != tc.wantChat || got[0]["thread_id"] != tc.wantTh || got[0]["report"] != "Two findings." ||
				got[0]["replace"] != true {
				t.Fatalf("writes = %v", got)
			}
			if kv.auth[0] != "Bearer kv-key" {
				t.Fatalf("auth = %q", kv.auth[0])
			}
			read, ok := incidentThread(tc.replyKey)
			if !ok || read.chatID != tc.wantChat || read.threadID != tc.wantTh {
				t.Fatalf("a reply in %s reads %+v; the write went to (%s, %s)", tc.replyKey, read, tc.wantChat, tc.wantTh)
			}
		})
	}
}

// TestAnIncidentOutsideTheServedChannelsIsRefused: a write naming a channel
// this install's chat backend does not serve is refused and stores nothing,
// so the gateway cannot attach a report to an arbitrary thread; the Slack
// home a gateway learned, read from its projected file, is served.
func TestAnIncidentOutsideTheServedChannelsIsRefused(t *testing.T) {
	t.Parallel()
	kv := &incidentKV{}
	b := incidentBridge(t, kv)
	for name, keep := range map[string]lib.IncidentKeep{
		"another slack channel":   {Platform: lib.NotifyPlatformSlack, ChatID: "C0OTHER", ThreadID: "1763100000.000100", Report: "x"},
		"another space":           {Platform: lib.NotifyPlatformGchat, ChatID: "spaces/OTHER", ThreadID: "spaces/OTHER/threads/t1", Report: "x"},
		"a platform with no home": {Platform: "discord", ChatID: "C0HOME", ThreadID: "1", Report: "x"},
	} {
		if err := b.keepIncident(keepRequest(t, keep)); err == nil {
			t.Errorf("%s: kept", name)
		}
	}
	for name, raw := range map[string]string{
		"a thread of another channel": `{"platform":"slack","chat_id":"C0HOME","thread_id":"C0OTHER/1763100000.000100","report":"x"}`,
		"a thread of another space":   `{"platform":"google_chat","chat_id":"spaces/HOME","thread_id":"spaces/OTHER/threads/t1","report":"x"}`,
		"not a ts":                    `{"platform":"slack","chat_id":"C0HOME","thread_id":"abc","report":"x"}`,
		"no report":                   `{"platform":"slack","chat_id":"C0HOME","thread_id":"1763100000.000100","report":" "}`,
		"a report over the cap":       `{"platform":"slack","chat_id":"C0HOME","thread_id":"1763100000.000100","report":"` + strings.Repeat("x", incidentReportMaxBytes+1) + `"}`,
	} {
		if err := b.keepIncident([]byte(raw)); err == nil {
			t.Errorf("%s: kept", name)
		}
	}
	if got := kv.writes(); len(got) != 0 {
		t.Fatalf("stored %v", got)
	}

	// The learned home, projected into the pod.
	file := filepath.Join(t.TempDir(), "slack_home")
	if err := os.WriteFile(file, []byte("C0LEARNED\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	b.cfg.IncidentSlackHome, b.cfg.IncidentSlackHomeFile = "", file
	learned := lib.IncidentKeep{Platform: lib.NotifyPlatformSlack, ChatID: "C0LEARNED", ThreadID: "1763100000.000100", Report: "x"}
	if err := b.keepIncident(keepRequest(t, learned)); err != nil {
		t.Fatalf("the learned home was refused: %v", err)
	}
	b.cfg.IncidentSlackHomeFile = ""
	if err := b.keepIncident(keepRequest(t, learned)); err == nil {
		t.Fatal("kept with no served channel at all")
	}
}

// TestTheIncidentRouteAnswersOverTheBus: the gateway's request on
// lib.IncidentSubject, its reply in the gateway's namespace, is answered ok
// once session-kv stores it, with session-kv's refusal when it does not, and
// a request whose reply is outside the namespace gets no answer.
func TestTheIncidentRouteAnswersOverTheBus(t *testing.T) {
	t.Parallel()
	_, url := startServer(t)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	client, err := lib.Connect(ctx, url, lib.WithName("bridge-incident-test"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(client.Close)
	kv := &incidentKV{}
	b := incidentBridge(t, kv)
	b.c = client
	sub := b.serveIncidents()
	if sub == nil {
		t.Fatal("the route was not bound")
	}
	t.Cleanup(sub.Stop)

	nc, err := nats.Connect(url)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(nc.Close)
	ask := func(reply string) (lib.IncidentKeepResult, error) {
		in, err := nc.SubscribeSync(reply)
		if err != nil {
			t.Fatal(err)
		}
		defer func() { _ = in.Unsubscribe() }()
		body := keepRequest(t, lib.IncidentKeep{Platform: lib.NotifyPlatformSlack, ChatID: "C0HOME", ThreadID: "1763100000.000100", Report: "Two findings."})
		if err := nc.PublishRequest(lib.IncidentSubject, reply, body); err != nil {
			t.Fatal(err)
		}
		msg, err := in.NextMsg(2 * time.Second)
		if err != nil {
			return lib.IncidentKeepResult{}, err
		}
		var r lib.IncidentKeepResult
		if err := json.Unmarshal(msg.Data, &r); err != nil {
			t.Fatal(err)
		}
		return r, nil
	}
	if r, err := ask(lib.IncidentReplyPrefix + "r1"); err != nil || !r.OK {
		t.Fatalf("answer = %+v (%v), want ok", r, err)
	}
	kv.mu.Lock()
	kv.status = http.StatusServiceUnavailable
	kv.mu.Unlock()
	if r, err := ask(lib.IncidentReplyPrefix + "r2"); err != nil || r.OK || r.Error == "" {
		t.Fatalf("answer = %+v (%v), want session-kv's refusal", r, err)
	}
	if _, err := ask("_INBOX.elsewhere"); err == nil {
		t.Fatal("a request with a reply outside the gateway's namespace was answered")
	}
}

// TestTheNewestReportInAThreadIsTheOneKept: two reports posted into one
// thread leave the second stored, since the bridge asks session-kv to
// replace; and a request carrying a field this bridge does not know (a newer
// gateway) is still kept.
func TestTheNewestReportInAThreadIsTheOneKept(t *testing.T) {
	t.Parallel()
	kv := &incidentKV{}
	b := incidentBridge(t, kv)
	for _, report := range []string{"The day's first report.", "The day's second report."} {
		keep := lib.IncidentKeep{Platform: lib.NotifyPlatformSlack, ChatID: "C0HOME", ThreadID: "1763100000.000100", Report: report}
		if err := b.keepIncident(keepRequest(t, keep)); err != nil {
			t.Fatal(err)
		}
	}
	kv.mu.Lock()
	got := kv.rows["C0HOME|1763100000.000100"]
	kv.mu.Unlock()
	if got != "The day's second report." {
		t.Fatalf("stored %q, want the second report", got)
	}
	newer := `{"platform":"slack","chat_id":"C0HOME","thread_id":"1763100000.000200","report":"x","priority":"high"}`
	if err := b.keepIncident([]byte(newer)); err != nil {
		t.Fatalf("a request with an unknown field was refused: %v", err)
	}
}

// TestIncidentWritesPastTheCapAreAnsweredBusy: with every slot taken, a
// write is answered busy at once and stores nothing.
func TestIncidentWritesPastTheCapAreAnsweredBusy(t *testing.T) {
	t.Parallel()
	_, url := startServer(t)
	nc, err := nats.Connect(url)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(nc.Close)
	kv := &incidentKV{}
	b := incidentBridge(t, kv)
	b.incidentSlots = make(chan struct{}, incidentKeepsInFlight)
	for range incidentKeepsInFlight {
		b.incidentSlots <- struct{}{}
	}
	if _, err := nc.Subscribe(lib.IncidentSubject, b.answerIncident); err != nil {
		t.Fatal(err)
	}
	body := keepRequest(t, lib.IncidentKeep{Platform: lib.NotifyPlatformSlack, ChatID: "C0HOME", ThreadID: "1763100000.000100", Report: "x"})
	reply := lib.IncidentReplyPrefix + "busy"
	in, err := nc.SubscribeSync(reply)
	if err != nil {
		t.Fatal(err)
	}
	if err := nc.PublishRequest(lib.IncidentSubject, reply, body); err != nil {
		t.Fatal(err)
	}
	msg, err := in.NextMsg(2 * time.Second)
	if err != nil {
		t.Fatal(err)
	}
	var r lib.IncidentKeepResult
	if err := json.Unmarshal(msg.Data, &r); err != nil || r.OK || r.Error != errIncidentBusy.Error() {
		t.Fatalf("answer = %s (%v), want busy", msg.Data, err)
	}
	if got := kv.writes(); len(got) != 0 {
		t.Fatalf("stored %v past the cap", got)
	}
}
