package main

import (
	"bytes"
	"context"
	"errors"
	"io"
	"log"
	"net"
	"net/http"
	"strings"
	"testing"
	"time"
)

const (
	testIntKey       = "KUBE_PROXY_TEST_INT"
	testDrainTimeout = 5 * time.Second
	testEarlyReturn  = 200 * time.Millisecond
	testWaitLimit    = 10 * time.Second
)

// receive fails the test instead of hanging when ch stays empty.
func receive[T any](t *testing.T, ch <-chan T, what string) T {
	t.Helper()
	select {
	case v := <-ch:
		return v
	case <-time.After(testWaitLimit):
		t.Fatalf("no %s within %s", what, testWaitLimit)
		var zero T
		return zero
	}
}

// A request in flight when SIGTERM lands finishes before the process exits:
// the replica's preStop sleep only covers new connections, and a kubectl
// call already inside the proxy must not be cut.
func TestServeUntilDrainedWaitsForTheInFlightRequest(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	arrived, release := make(chan struct{}), make(chan struct{})
	srv := &http.Server{Handler: http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		close(arrived)
		<-release
		_, _ = io.WriteString(w, "done")
	})}

	ctx, stop := context.WithCancel(context.Background())
	defer stop()
	served := make(chan error, 1)
	go func() {
		drainErr, err := serveUntilDrained(ctx, srv, func() error { return srv.Serve(ln) }, testDrainTimeout)
		served <- errors.Join(drainErr, err)
	}()

	answered := make(chan string, 1)
	go func() {
		resp, err := http.Get("http://" + ln.Addr().String())
		if err != nil {
			answered <- "error: " + err.Error()
			return
		}
		defer func() { _ = resp.Body.Close() }()
		body, _ := io.ReadAll(resp.Body)
		answered <- string(body)
	}()
	receive(t, arrived, "request at the server")

	stop()
	select {
	case err := <-served:
		t.Fatalf("returned with a request in flight (err=%v)", err)
	case <-time.After(testEarlyReturn):
	}

	close(release)
	if got := receive(t, answered, "response"); got != "done" {
		t.Errorf("in-flight request got %q, want done", got)
	}
	if err := receive(t, served, "return from serveUntilDrained"); err != nil {
		t.Errorf("serveUntilDrained = %v, want nil", err)
	}
}

func TestServeUntilDrainedReturnsAListenFailureWithoutWaiting(t *testing.T) {
	bound := errors.New("address already in use")
	ctx, stop := context.WithCancel(context.Background())
	defer stop()
	drainErr, err := serveUntilDrained(ctx, &http.Server{}, func() error { return bound }, testDrainTimeout)
	if !errors.Is(err, bound) || drainErr != nil {
		t.Errorf("got (drainErr=%v, err=%v), want (nil, %v)", drainErr, err, bound)
	}
}

func TestEnvIntOrDefault(t *testing.T) {
	cases := []struct {
		name, raw string
		want      int
		alert     bool
	}{
		{name: "unset", raw: "", want: 7},
		{name: "integer", raw: " 9100 ", want: 9100},
		{name: "not an integer", raw: "80x", want: 7, alert: true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Setenv(testIntKey, tc.raw)
			var buf bytes.Buffer
			prev := log.Writer()
			log.SetOutput(&buf)
			t.Cleanup(func() { log.SetOutput(prev) })

			if got := envIntOrDefault(testIntKey, 7); got != tc.want {
				t.Errorf("envIntOrDefault = %d, want %d", got, tc.want)
			}
			out := buf.String()
			if !tc.alert {
				if out != "" {
					t.Errorf("logged %q, want nothing", out)
				}
				return
			}
			// The ALERT names the key it was given and the fallback it used,
			// not the metrics port's variable or 0.
			for _, want := range []string{"ALERT", testIntKey + `="80x"`, "using 7"} {
				if !strings.Contains(out, want) {
					t.Errorf("logged %q, want it to contain %q", out, want)
				}
			}
		})
	}
}
