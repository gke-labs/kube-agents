package authcallout

import (
	"net"
	"os"
	"strconv"
	"strings"
	"testing"
)

// The harness must come up whatever else is listening on the CI runner.
//
// startHarness starts a real nats-server from the operator's rendered
// nats.conf and moves every port that render pins to a free one. A port it
// forgets is a port the test shares with everything else on the host, and
// nats-server reports a listener it cannot bind through Fatalf and returns
// from Start, so without the log startNatsServer keeps the only symptom is
// the ReadyForConnections timeout: the bare "nats-server not ready" the
// flaky-check issues quote.

// renderedMonitorPort is the monitoring port the fixture pins (`http: 8222`).
// The test holds it for its duration and asserts the harness does not need it.
const renderedMonitorPort = 8222

func TestTheHarnessStartsWhileTheRenderedMonitorPortIsTaken(t *testing.T) {
	rendered, err := os.ReadFile("testdata/rendered-nats.conf")
	if err != nil {
		t.Fatalf("reading the operator's rendered nats.conf: %v", err)
	}
	if !strings.Contains(string(rendered), "http: "+strconv.Itoa(renderedMonitorPort)) {
		t.Fatalf("the rendered nats.conf no longer pins http: %d; move this test's holder to the port it pins now", renderedMonitorPort)
	}

	// Whoever holds the port stages the collision. When another process on
	// the host already has it, that process is the holder and the test runs
	// the same; a listener on any address of the port refuses the render's
	// wildcard bind.
	holder, err := net.Listen("tcp", net.JoinHostPort("127.0.0.1", strconv.Itoa(renderedMonitorPort)))
	if err != nil {
		t.Logf("port %d is already held on this host, which stages the collision without this test's own holder: %v", renderedMonitorPort, err)
	} else {
		t.Cleanup(func() { _ = holder.Close() })
	}

	h := startHarness(t, twoIdentityMap, defaultTokens())

	monitor := h.server.MonitorAddr()
	if monitor == nil {
		t.Fatal("the harness started with no monitor listener; the render has one and the fixture should keep its shape")
	}
	if monitor.Port == renderedMonitorPort {
		t.Fatalf("the monitor reports port %d, the one the render pins and this test holds", monitor.Port)
	}

	nc, _ := h.connectAs(t, "gateway", gatewayToken)
	if !nc.IsConnected() {
		t.Fatal("a client could not connect to the harness")
	}
}
