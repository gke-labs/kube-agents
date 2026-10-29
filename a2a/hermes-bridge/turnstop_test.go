package hermesbridge

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"os"
	"regexp"
	"strconv"
	"sync"
	"testing"
	"time"

	lib "github.com/gke-labs/kube-agents/a2a/lib"
)

// The #3263 race: a cancel that ends a turn's request asks the API server to
// stop the turn running in the session, with the server's key, and the task
// still ends canceled.
func TestTurnStop_ACancelStopsTheTurnInHermes(t *testing.T) {
	_, url := startServer(t)
	started := make(chan struct{})
	stub := newAPIStub(t, func(w http.ResponseWriter, r *http.Request, c apiCall) {
		close(started)
		<-r.Context().Done()
	})
	auth := make(chan string, 1)
	stub.stop = func(w http.ResponseWriter, r *http.Request) {
		auth <- r.Header.Get("Authorization")
		_ = json.NewEncoder(w).Encode(turnStopResponse{Turns: 1, Interrupted: 1, Ended: true})
	}
	startAPIBridge(t, url, stub, nil)
	c := gatewayClient(t, url)

	origin := submitIn(t, c, "task-stop-turn", "ctx-stop-turn", "file a card, slowly")
	select {
	case <-started:
	case <-time.After(10 * time.Second):
		t.Fatal("the request never reached the server")
	}
	publishCancel(t, c, origin)
	if task := waitTerminal(t, c, origin.TaskID); task.State != lib.StateCanceled {
		t.Fatalf("state = %s, want canceled", task.State)
	}
	waitFor(t, 5*time.Second, "the turn stop", func() bool { return len(stub.stopsSeen()) == 1 })
	if got, want := stub.stopsSeen()[0], "/v1/kube-agents/sessions/a2a-ctx-stop-turn/stop"; got != want {
		t.Fatalf("stop path = %q, want %q", got, want)
	}
	if got := <-auth; got != "Bearer "+testAPIKey {
		t.Fatalf("stop Authorization = %q, want the API key", got)
	}
}

// The session's turn is held until the stop answers, so the conversation's
// next turn (or its card stop) starts only once the stopped turn has ended.
func TestTurnStop_TheSessionWaitsForTheStop(t *testing.T) {
	_, url := startServer(t)
	started := make(chan struct{})
	nextSent := make(chan struct{})
	stub := newAPIStub(t, func(w http.ResponseWriter, r *http.Request, c apiCall) {
		if c.prompt == "next" {
			close(nextSent)
			writeCompletion(w, c.sessionID, "next")
			return
		}
		close(started)
		<-r.Context().Done()
	})
	stopping := make(chan struct{})
	release := make(chan struct{})
	var once sync.Once
	open := func() { once.Do(func() { close(release) }) }
	t.Cleanup(open)
	stub.stop = func(w http.ResponseWriter, r *http.Request) {
		close(stopping)
		<-release
		_ = json.NewEncoder(w).Encode(turnStopResponse{Turns: 1, Interrupted: 1, Ended: true})
	}
	startAPIBridge(t, url, stub, func(cfg *Config) { cfg.Concurrency = 2 })
	c := gatewayClient(t, url)

	origin := submitIn(t, c, "task-held", "ctx-held", "first")
	<-started
	publishCancel(t, c, origin)
	select {
	case <-stopping:
	case <-time.After(10 * time.Second):
		t.Fatal("no turn stop was sent")
	}
	submitIn(t, c, "task-next", "ctx-held", "next")
	select {
	case <-nextSent:
		t.Fatal("the next turn started while the stop was still running")
	case <-time.After(500 * time.Millisecond):
	}
	open()
	select {
	case <-nextSent:
	case <-time.After(10 * time.Second):
		t.Fatal("the next turn never started after the stop answered")
	}
}

// A turn that answered, and a turn that failed on its own, leave nothing
// running in Hermes: no stop is sent.
func TestTurnStop_OnlyACancelStops(t *testing.T) {
	_, url := startServer(t)
	stub := newAPIStub(t, func(w http.ResponseWriter, r *http.Request, c apiCall) {
		if c.prompt == "fail" {
			http.Error(w, "boom", http.StatusBadGateway)
			return
		}
		writeCompletion(w, c.sessionID, "done")
	})
	startAPIBridge(t, url, stub, nil)
	c := gatewayClient(t, url)

	for _, prompt := range []string{"answer", "fail"} {
		origin := submitIn(t, c, "task-"+prompt, "ctx-"+prompt, prompt)
		waitTerminal(t, c, origin.TaskID)
	}
	if stops := stub.stopsSeen(); len(stops) != 0 {
		t.Fatalf("stops = %v, want none", stops)
	}
}

// An image without the route answers 404: the task still ends canceled, and
// the miss is named rather than mistaken for a failed stop.
func TestTurnStop_AServerWithoutTheRouteIsNamed(t *testing.T) {
	stub := newAPIStub(t, nil)
	b := &Bridge{cfg: Config{APIURL: stub.srv.URL + "/v1/chat/completions"}, apiClient: newAPIClient()}
	if _, err := b.postTurnStop("a2a-ctx-x"); err != errTurnStopUnsupported {
		t.Fatalf("err = %v, want errTurnStopUnsupported", err)
	}
}

// A profile-prefixed URL keeps its prefix; a URL of another shape names why
// the stop was not sent.
func TestTurnStop_TheStopAddressFollowsTheAPIURL(t *testing.T) {
	stub := newAPIStub(t, nil)
	stub.stop = func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode(turnStopResponse{Ended: true})
	}
	b := &Bridge{cfg: Config{APIURL: stub.srv.URL + "/p/chat/v1/chat/completions/", APIKey: testAPIKey}, apiClient: newAPIClient()}
	if _, err := b.postTurnStop("a2a-ctx-p"); err != nil {
		t.Fatal(err)
	}
	if got, want := stub.stopsSeen(), "/p/chat/v1/kube-agents/sessions/a2a-ctx-p/stop"; len(got) != 1 || got[0] != want {
		t.Fatalf("stops = %v, want [%s]", got, want)
	}
	b.cfg.APIURL = stub.srv.URL + "/v1/responses"
	if _, err := b.postTurnStop("a2a-ctx-p"); err == nil {
		t.Fatal("a URL with no chat-completions suffix sent a stop")
	}
}

// A request that never reached the server started nothing to stop.
func TestTurnStop_NothingSentStopsNothing(t *testing.T) {
	stub := newAPIStub(t, nil)
	b := &Bridge{cfg: Config{APIURL: stub.srv.URL + "/v1/chat/completions", Logger: slog.New(slog.NewTextHandler(io.Discard, nil))},
		apiClient: newAPIClient()}
	run := &taskRun{origin: &lib.Envelope{TaskID: "t"}}
	run.canceled.Store(true)
	b.stopCanceledTurn(run, "a2a-ctx-n", errNeverConnected)
	if stops := stub.stopsSeen(); len(stops) != 0 {
		t.Fatalf("stops = %v, want none", stops)
	}
}

// A shutdown ends a stop already in flight rather than waiting out its
// timeout: the pod's turns go with the bridge.
func TestTurnStop_AShutdownEndsAStopInFlight(t *testing.T) {
	stub := newAPIStub(t, nil)
	release := make(chan struct{})
	t.Cleanup(func() { close(release) })
	stub.stop = func(w http.ResponseWriter, r *http.Request) {
		select {
		case <-release:
		case <-r.Context().Done():
		}
	}
	stopping, stopStopping := context.WithCancel(context.Background())
	b := &Bridge{cfg: Config{APIURL: stub.srv.URL + "/v1/chat/completions"}, apiClient: newAPIClient(), stopping: stopping}
	time.AfterFunc(200*time.Millisecond, stopStopping)
	started := time.Now()
	if _, err := b.postTurnStop("a2a-ctx-s"); err == nil {
		t.Fatal("a stop the shutdown ended reported an answer")
	}
	if took := time.Since(started); took > 5*time.Second {
		t.Fatalf("the stop held the shutdown for %s", took)
	}
}

// The three waits nest: the API server's route answers inside the bridge's
// request bound, and the card stop's wait for the session's turn outlasts
// both, or a slow turn stop would end the card stop card-stop-unavailable.
func TestTurnStop_TheWaitsNest(t *testing.T) {
	src, err := os.ReadFile("../../deploy/docker/patches/api_session_stop.py")
	if err != nil {
		t.Fatal(err)
	}
	m := regexp.MustCompile(`(?m)^WAIT_SECONDS = ([0-9.]+)$`).FindSubmatch(src)
	if m == nil {
		t.Fatal("WAIT_SECONDS not found in api_session_stop.py")
	}
	route, err := strconv.ParseFloat(string(m[1]), 64)
	if err != nil {
		t.Fatal(err)
	}
	if routeWait := time.Duration(route * float64(time.Second)); routeWait >= turnStopTimeout {
		t.Fatalf("the route's wait %s is not under turnStopTimeout %s", routeWait, turnStopTimeout)
	}
	if turnStopTimeout >= cardStopTurnWait {
		t.Fatalf("turnStopTimeout %s is not under cardStopTurnWait %s", turnStopTimeout, cardStopTurnWait)
	}
}
