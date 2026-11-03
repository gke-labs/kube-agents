package gateway

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net"
	"net/http"
	"strings"
	"testing"
	"time"

	"k8s.io/client-go/kubernetes/fake"
)

// freeLoopback is a loopback address nothing listens on.
func freeLoopback(t *testing.T) string {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	addr := ln.Addr().String()
	_ = ln.Close()
	return addr
}

func standbyGet(t *testing.T, addr string) (*http.Response, standbyAnswer) {
	t.Helper()
	resp, err := http.Post("http://"+addr+injectPath, "application/json", strings.NewReader(`{"text":"hi"}`))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	var ans standbyAnswer
	if err := json.Unmarshal(body, &ans); err != nil {
		t.Fatalf("body %q: %v", body, err)
	}
	return resp, ans
}

// TestAStandbyDoorNamesTheHolderAndThePortForward: a request that lands on
// the standby is told which pod leads and the exact command that reaches
// it, in the body and in a header a client can re-target on.
func TestAStandbyDoorNamesTheHolderAndThePortForward(t *testing.T) {
	addr := freeLoopback(t)
	_, port, _ := net.SplitHostPort(addr)
	view := &LeaseView{}
	view.set("platform-agent-a2a-gateway-7d9f-abcde")
	doors, err := StartStandbyDoors([]string{addr}, "kubeagents-system", view, slog.Default())
	if err != nil {
		t.Fatal(err)
	}
	defer doors.Stop()

	resp, ans := standbyGet(t, addr)
	if resp.StatusCode != http.StatusServiceUnavailable {
		t.Fatalf("status %d, want 503", resp.StatusCode)
	}
	if h := resp.Header.Get(StandbyHolderHeader); h != "platform-agent-a2a-gateway-7d9f-abcde" {
		t.Fatalf("holder header %q", h)
	}
	if resp.Header.Get("Retry-After") == "" {
		t.Fatal("no Retry-After")
	}
	want := "kubectl -n kubeagents-system port-forward pod/platform-agent-a2a-gateway-7d9f-abcde " + port
	if ans.Command != want || !strings.Contains(ans.Error, want) || ans.Holder != "platform-agent-a2a-gateway-7d9f-abcde" {
		t.Fatalf("answer %+v, want the command %q", ans, want)
	}

	// The lease moves: the next answer names the new holder.
	view.set("platform-agent-a2a-gateway-7d9f-fghij")
	if resp, _ := standbyGet(t, addr); resp.Header.Get(StandbyHolderHeader) != "platform-agent-a2a-gateway-7d9f-fghij" {
		t.Fatalf("holder after the move %q", resp.Header.Get(StandbyHolderHeader))
	}
}

// TestAStandbyDoorWithNoHolderSaysToRetry: between leaders there is no pod
// to point at, so the answer says to retry and names nobody.
func TestAStandbyDoorWithNoHolderSaysToRetry(t *testing.T) {
	addr := freeLoopback(t)
	view := &LeaseView{}
	doors, err := StartStandbyDoors([]string{addr}, "kubeagents-system", view, slog.Default())
	if err != nil {
		t.Fatal(err)
	}
	defer doors.Stop()
	// Before the first read: nothing to say about the holder.
	if _, ans := standbyGet(t, addr); !strings.Contains(ans.Error, "hasn't read the lease yet") {
		t.Fatalf("answer before the first read %+v", ans)
	}
	view.set("")
	resp, ans := standbyGet(t, addr)
	if resp.StatusCode != http.StatusServiceUnavailable || resp.Header.Get(StandbyHolderHeader) != "" || ans.Command != "" || !strings.Contains(ans.Error, "no replica holds the lease") {
		t.Fatalf("status %d, header %q, answer %+v", resp.StatusCode, resp.Header.Get(StandbyHolderHeader), ans)
	}
}

// TestStoppingTheStandbyFreesTheDoorsAddresses: once the replica leads, its
// doors bind the same addresses, so Stop must have closed them on return.
func TestStoppingTheStandbyFreesTheDoorsAddresses(t *testing.T) {
	a, b := freeLoopback(t), freeLoopback(t)
	doors, err := StartStandbyDoors([]string{a, b}, "kubeagents-system", &LeaseView{}, slog.Default())
	if err != nil {
		t.Fatal(err)
	}
	doors.Stop()
	doors.Stop()
	for _, addr := range []string{a, b} {
		ln, err := net.Listen("tcp", addr)
		if err != nil {
			t.Fatalf("%s still bound after Stop: %v", addr, err)
		}
		_ = ln.Close()
	}
}

// TestAStandbyDoorThatCantBindFailsTheStart: a door address already taken
// is the same failure the door itself would hit on leading, said at boot.
func TestAStandbyDoorThatCantBindFailsTheStart(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	free := freeLoopback(t)
	if _, err := StartStandbyDoors([]string{free, ln.Addr().String()}, "kubeagents-system", &LeaseView{}, slog.Default()); err == nil {
		t.Fatal("started on a taken address")
	}
	// The first address it did bind is released again.
	again, err := net.Listen("tcp", free)
	if err != nil {
		t.Fatalf("%s left bound after the failed start: %v", free, err)
	}
	_ = again.Close()
}

// TestTheElectionKeepsTheStandbysViewOfTheHolder: a standby's view follows
// the lease it keeps reading.
func TestTheElectionKeepsTheStandbysViewOfTheHolder(t *testing.T) {
	cs := fake.NewClientset(heldLease("pod-a", 3600))
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	view := &LeaseView{}
	e := Election{
		Client: cs, Namespace: testNamespace, Lease: testLease, Identity: "pod-b", View: view,
		LeaseDuration: testLeaseDuration, RenewDeadline: testRenewDeadline, RetryPeriod: testRetryPeriod,
	}
	done := make(chan error, 1)
	go func() { done <- e.Run(ctx, &fakeLeader{}, func(context.Context, RunOptions) error { return nil }) }()
	waitFor(t, "the standby's view of pod-a", func() bool { return view.Holder() == "pod-a" })
	cancel()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("the standby didn't stop")
	}
}
