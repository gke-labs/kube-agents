package authcallout

import (
	"net"
	"os"
	"strings"
	"testing"
)

// The harness must come up whatever else is listening on the CI runner.
//
// startHarness starts a real nats-server from the operator's rendered
// nats.conf and moves every port that render pins to a free one. A port it
// forgets is a port the test shares with everything else on the host, and
// nats-server reports a listener it cannot bind through Fatalf and returns
// from Start, so the only symptom is the ReadyForConnections timeout: the
// bare "nats-server not ready" the flaky-check issues quote.

// renderedMonitorPort is the monitoring port the fixture pins (`http: 8222`).
// The test holds it for its duration and asserts the harness does not need it.
const renderedMonitorPort = "8222"

func TestTheHarnessStartsWhileTheRenderedMonitorPortIsTaken(t *testing.T) {
	rendered, err := os.ReadFile("testdata/rendered-nats.conf")
	if err != nil {
		t.Fatalf("reading the operator's rendered nats.conf: %v", err)
	}
	if !strings.Contains(string(rendered), "http: "+renderedMonitorPort) {
		t.Fatalf("the rendered nats.conf no longer pins http: %s; move this test's holder to the port it pins now", renderedMonitorPort)
	}

	holder, err := net.Listen("tcp", net.JoinHostPort("127.0.0.1", renderedMonitorPort))
	if err != nil {
		t.Skipf("port %s is already taken on this host, so the collision cannot be staged: %v", renderedMonitorPort, err)
	}
	t.Cleanup(func() { _ = holder.Close() })

	h := startHarness(t, twoIdentityMap, defaultTokens())

	monitor := h.server.MonitorAddr()
	if monitor == nil {
		t.Fatal("the harness started with no monitor listener; the render has one and the fixture should keep its shape")
	}
	if got := monitor.Port; got == holder.Addr().(*net.TCPAddr).Port {
		t.Fatalf("the monitor reports port %d, the one this test is holding", got)
	}

	nc, _ := h.connectAs(t, "gateway", gatewayToken)
	if !nc.IsConnected() {
		t.Fatal("a client could not connect to the harness")
	}
}
