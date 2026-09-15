package authcallout

import (
	"fmt"
	"strings"
	"testing"
	"time"

	natsserver "github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The supervisor subject split, proven against the operator's real render on
// a real server, from both sides of the boundary.
//
// The session side is in session_integration_test.go: a session's derived
// grants reach its own `…events` and not its own `…supervisor`. This file is
// the other writer. The gateway is a static nats.conf user, so the grant under
// test is the one platformagent_a2a_identities.go renders and the fixture in
// testdata/ carries byte-for-byte; the assertion is the server's refusal, not
// the grant string.
//
// The password is the fixture's placeholder ("pw-gateway"); the operator
// substitutes a generated one at render time and the test harness leaves it,
// because what is under test is the permission block, not the credential.

const renderedGatewayPassword = "pw-gateway"

// violationLog captures the server's own log so the refusal can be shown
// where an operator would look for it, not only on the client's error
// handler. nats-server logs a refused publish as a "Publish Violation" line
// naming the subject.
type violationLog struct {
	lines chan string
}

func (l *violationLog) Noticef(format string, v ...any) {}
func (l *violationLog) Warnf(format string, v ...any)   {}
func (l *violationLog) Fatalf(format string, v ...any)  {}
func (l *violationLog) Debugf(format string, v ...any)  {}
func (l *violationLog) Tracef(format string, v ...any)  {}
func (l *violationLog) Errorf(format string, v ...any) {
	select {
	case l.lines <- strings.TrimSpace(fmt.Sprintf(format, v...)):
	default:
	}
}

// startHarnessWithServerLog is startHarness plus a capturing server logger.
// The rendered config sets no log level of its own, and nats-server reports
// permission violations at error level, so nothing but the logger changes.
func startHarnessWithServerLog(t *testing.T) (*harness, *violationLog) {
	t.Helper()
	h := startHarness(t, sessionMap, sessionTokens())
	vl := &violationLog{lines: make(chan string, 64)}
	h.server.SetLoggerV2(vl, false, false, false)
	return h, vl
}

func (vl *violationLog) sawViolationFor(subject string) bool {
	deadline := time.After(2 * time.Second)
	for {
		select {
		case line := <-vl.lines:
			if strings.Contains(line, "Violation") && strings.Contains(line, subject) {
				return true
			}
		case <-deadline:
			return false
		}
	}
}

func connectStatic(t *testing.T, h *harness, user, password string) (*nats.Conn, chan error) {
	t.Helper()
	violations := make(chan error, 16)
	nc, err := nats.Connect(h.url,
		nats.UserInfo(user, password),
		nats.CustomInboxPrefix("_INBOX."+user),
		nats.Name(user),
		nats.ErrorHandler(func(_ *nats.Conn, _ *nats.Subscription, e error) { violations <- e }),
	)
	if err != nil {
		t.Fatalf("%s could not connect with the rendered static credential: %v", user, err)
	}
	t.Cleanup(nc.Close)
	return nc, violations
}

// The supervisor may write a task's `…supervisor` and may not write its
// `…events`. Both halves against the rendered nats.conf, with the server's own
// log line for the refusal.
func TestTheRenderedGatewayIsRefusedOnTheExecutorsEventsSubject(t *testing.T) {
	h, serverLog := startHarnessWithServerLog(t)
	nc, violations := connectStatic(t, h, "gateway", renderedGatewayPassword)

	events := lib.TaskEventsSubject(podA, "task-1")
	supervisor := lib.TaskSupervisorSubject(podA, "task-1")
	in := lib.TaskInSubject(podA, "task-1")

	checkPublish(t, nc, violations, map[string]bool{
		supervisor: false, // the supervisor's own subject
		in:         false, // it is also the requester
		events:     true,  // the executor's subject; one writer, and not this one
	})
	if !serverLog.sawViolationFor(events) {
		t.Errorf("the server logged no publish violation naming %s; the refusal has to be visible where an operator looks", events)
	}
}

// The executor's grant ends at its own `…events`. Restated here beside the
// gateway's half so the two writer sets are read together: for one task there
// is exactly one principal that may write each subject, and they differ.
func TestTheTwoTaskEventSubjectsHaveDisjointWriters(t *testing.T) {
	h, serverLog := startHarnessWithServerLog(t)
	session, sessionViolations := h.connectAs(t, podA, tokenPodA)
	gateway, gatewayViolations := connectStatic(t, h, "gateway", renderedGatewayPassword)

	events := lib.TaskEventsSubject(podA, "task-1")
	supervisor := lib.TaskSupervisorSubject(podA, "task-1")

	checkPublish(t, session, sessionViolations, map[string]bool{events: false, supervisor: true})
	if !serverLog.sawViolationFor(supervisor) {
		t.Errorf("the server logged no publish violation for the session on %s", supervisor)
	}
	checkPublish(t, gateway, gatewayViolations, map[string]bool{events: true, supervisor: false})
}

// Keep the compiler honest about the logger interface: a nats-server upgrade
// that changes it fails here rather than at SetLoggerV2.
var _ natsserver.Logger = (*violationLog)(nil)
