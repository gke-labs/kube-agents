package main

import (
	"context"
	"io"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/prometheus/client_golang/prometheus/testutil"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes/fake"
)

// The listener the entrypoint opens on every deployed install: /metrics serves
// the watcher's own registry in the text exposition, /healthz answers 200, and
// a series appears under the name a scrape config or an alert would use.
func TestStartMetrics_ServesTheRegistryAndHealthz(t *testing.T) {
	m := newMetrics()
	m.eventsSeen.WithLabelValues("c", "p", "l", "BackOff").Inc()
	srv, err := startMetrics("127.0.0.1:0", m)
	if err != nil {
		t.Fatalf("startMetrics: %v", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- srv.Run(ctx) }()
	base := "http://" + srv.ln.Addr().String()

	body := httpGet(t, base+"/metrics")
	want := `k8s_event_watcher_events_seen_total{cluster="c",location="l",project="p",reason="BackOff"} 1`
	if !strings.Contains(body, want) {
		t.Errorf("/metrics does not carry %q:\n%s", want, body)
	}
	if got := httpGet(t, base+"/healthz"); got != "ok\n" {
		t.Errorf("/healthz = %q, want ok", got)
	}

	cancel()
	select {
	case err := <-done:
		if err != nil {
			t.Errorf("Run returned %v after cancellation", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("Run did not return within 5s of cancellation")
	}
}

func httpGet(t *testing.T, url string) string {
	t.Helper()
	resp, err := http.Get(url)
	if err != nil {
		t.Fatalf("GET %s: %v", url, err)
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatalf("read %s: %v", url, err)
	}
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("GET %s = %d, want 200: %s", url, resp.StatusCode, body)
	}
	return string(body)
}

// An empty address is the flag's default and means no listener at all: a nil
// server, which Run treats as "wait for shutdown".
func TestStartMetrics_EmptyAddressOpensNothing(t *testing.T) {
	srv, err := startMetrics("", newMetrics())
	if err != nil || srv != nil {
		t.Fatalf("startMetrics(\"\") = %v, %v; want nil, nil", srv, err)
	}
}

// A port that is already taken is reported as an error naming the address, and
// no server comes back with it, so the caller cannot serve on a listener that
// does not exist.
func TestStartMetrics_AnOccupiedPortIsAnError(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	srv, err := startMetrics(ln.Addr().String(), newMetrics())
	if err == nil || srv != nil {
		t.Fatalf("startMetrics on an occupied port = %v, %v; want an error and no server", srv, err)
	}
	if !strings.Contains(err.Error(), ln.Addr().String()) {
		t.Errorf("error %q does not name the address", err)
	}
}

// The listener is opened on every deployed install now, so a port it cannot
// bind must not take the watcher down with it. realMain runs on past the
// failure to the kubeconfig step, which this test makes fail so the run stops
// there, and the log names the consequence.
func TestRealMain_AnOccupiedMetricsPortDoesNotStopTheWatcher(t *testing.T) {
	logs := captureLog(t)
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	badKubeconfig := filepath.Join(t.TempDir(), "kubeconfig")
	if err := os.WriteFile(badKubeconfig, []byte("not: [a kubeconfig"), 0o600); err != nil {
		t.Fatal(err)
	}

	err = realMain([]string{"--dry-run", "--kubeconfig", badKubeconfig, "--cluster-name", "x", "--metrics-addr", ln.Addr().String()})
	if err == nil || !strings.Contains(err.Error(), "kubeconfig") {
		t.Fatalf("want realMain to run past the metrics listener and stop on the kubeconfig, got err=%v", err)
	}
	if strings.Contains(err.Error(), "metrics") {
		t.Errorf("the metrics listener failure came back as the run's error: %v", err)
	}
	if got := logs.String(); !strings.Contains(got, "ALERT") || !strings.Contains(got, "/metrics") {
		t.Errorf("log does not carry the ALERT line for the missing listener:\n%s", got)
	}
}

// events_seen_total counts every event the informer hands over, before any
// filter looks at it: the one series that says the watcher is receiving events
// at all. Driven through Run and a fake API server rather than by calling
// Dispatch, so the path under test is the one from the watch to the counter.
func TestRun_AnEventReachingTheInformerIsCountedAsSeen(t *testing.T) {
	captureLog(t)
	client := fake.NewClientset()
	allowPreflight(client)
	ev := &corev1.Event{
		ObjectMeta: metav1.ObjectMeta{Name: "billing-service.1", Namespace: "default"},
		Type:       corev1.EventTypeWarning,
		Reason:     "BackOff",
		Message:    "Back-off restarting failed container",
		InvolvedObject: corev1.ObjectReference{
			Kind: "Pod", Namespace: "default", Name: "billing-service", UID: "pod-1",
		},
		Count:         1,
		LastTimestamp: metav1.Now(),
	}
	if _, err := client.CoreV1().Events("default").Create(context.Background(), ev, metav1.CreateOptions{}); err != nil {
		t.Fatal(err)
	}
	disp, m, _, _ := newCountingDispatcher(t, filterThresholds{})
	w := newWatcher(client, disp, targetCluster{Name: "seen", ProjectID: "p", Location: "us-central1"}, 0)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- w.Run(ctx, func(bool) {}) }()
	eventually(t, 10*time.Second, func() bool {
		return testutil.ToFloat64(m.eventsSeen.WithLabelValues("seen", "p", "us-central1", "BackOff")) >= 1
	}, "events_seen_total to count the Warning event")

	cancel()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("Run did not return within 5s of cancellation")
	}
}

// The address the process opened is in its log, so a scrape target that is
// down can be read against it: the entrypoint forwards whatever port it was
// given, and a port other than the declared one binds fine.
func TestRealMain_LogsTheMetricsAddressItOpened(t *testing.T) {
	logs := captureLog(t)
	badKubeconfig := filepath.Join(t.TempDir(), "kubeconfig")
	if err := os.WriteFile(badKubeconfig, []byte("not: [a kubeconfig"), 0o600); err != nil {
		t.Fatal(err)
	}
	err := realMain([]string{"--dry-run", "--kubeconfig", badKubeconfig, "--cluster-name", "x", "--metrics-addr", "127.0.0.1:0"})
	if err == nil || !strings.Contains(err.Error(), "kubeconfig") {
		t.Fatalf("want realMain to stop on the kubeconfig after opening the listener, got err=%v", err)
	}
	if got := logs.String(); !strings.Contains(got, "/metrics listening on 127.0.0.1:") {
		t.Errorf("log does not name the address the listener opened:\n%s", got)
	}
}
