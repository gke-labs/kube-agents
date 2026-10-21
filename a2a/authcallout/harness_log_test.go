package authcallout

import (
	"fmt"
	"net"
	"strings"
	"sync"
	"testing"
	"time"

	natsserver "github.com/nats-io/nats-server/v2/server"
)

// startNatsServer builds a server from opts, starts it, and waits for it to
// accept connections. A server that is not ready within the wait comes back
// as an error carrying every line the server logged, so the harness failure
// names the cause. nats-server reports a listener it cannot bind through
// Fatalf and returns from Start, and an embedded server has no logger until
// one is installed, so the logger goes in before Start or the line is lost.
func startNatsServer(opts *natsserver.Options, ready time.Duration) (*natsserver.Server, error) {
	srv, err := natsserver.NewServer(opts)
	if err != nil {
		return nil, fmt.Errorf("NewServer: %w", err)
	}
	startup := &startupLog{}
	srv.SetLoggerV2(startup, false, false, false)
	go srv.Start()
	if !srv.ReadyForConnections(ready) {
		srv.Shutdown()
		return nil, fmt.Errorf("nats-server not ready after %s; its log:\n%s", ready, startup.String())
	}
	return srv, nil
}

// startupLog keeps every line the server logs, at every level, so a harness
// whose server never becomes ready can show why. violationLog
// (supervisor_split_test.go) is the sibling that keeps only the error level
// for the tests that read permission violations.
type startupLog struct {
	mu    sync.Mutex
	lines []string
}

// startupLogLimit bounds the buffer: start-up is a few dozen lines, and a
// server that runs on for the rest of a test must not grow it without end.
const startupLogLimit = 256

func (l *startupLog) record(level, format string, v ...any) {
	l.mu.Lock()
	defer l.mu.Unlock()
	if len(l.lines) >= startupLogLimit {
		return
	}
	l.lines = append(l.lines, level+" "+strings.TrimSpace(fmt.Sprintf(format, v...)))
}

func (l *startupLog) Noticef(format string, v ...any) { l.record("[INF]", format, v...) }
func (l *startupLog) Warnf(format string, v ...any)   { l.record("[WRN]", format, v...) }
func (l *startupLog) Fatalf(format string, v ...any)  { l.record("[FTL]", format, v...) }
func (l *startupLog) Errorf(format string, v ...any)  { l.record("[ERR]", format, v...) }
func (l *startupLog) Debugf(format string, v ...any)  { l.record("[DBG]", format, v...) }
func (l *startupLog) Tracef(format string, v ...any)  { l.record("[TRC]", format, v...) }

// String returns the captured lines, one per line, for a failure message.
func (l *startupLog) String() string {
	l.mu.Lock()
	defer l.mu.Unlock()
	return strings.Join(l.lines, "\n")
}

// Keep the compiler honest about the logger interface, as for violationLog.
var _ natsserver.Logger = (*startupLog)(nil)

// unreadyWait is how long the failure test lets a server that cannot bind
// its monitor try to become ready; Start has already returned by then.
const unreadyWait = 2 * time.Second

// A server that cannot start says why. The monitor port is held and the
// override that would move it withheld, which is the failure the harnesses
// used to report as a bare timeout.
func TestAServerThatCannotStartReportsItsOwnReason(t *testing.T) {
	holder, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("holding a port: %v", err)
	}
	t.Cleanup(func() { _ = holder.Close() })
	held := holder.Addr().(*net.TCPAddr).Port

	opts := &natsserver.Options{
		Host: "127.0.0.1", Port: -1,
		HTTPHost: "127.0.0.1", HTTPPort: held,
		NoLog: true, NoSigs: true,
	}
	srv, err := startNatsServer(opts, unreadyWait)
	if err == nil {
		srv.Shutdown()
		t.Fatalf("the server became ready with its monitor port %d held", held)
	}
	for _, want := range []string{"nats-server not ready", "[FTL] Can't start monitoring", "address already in use"} {
		if !strings.Contains(err.Error(), want) {
			t.Errorf("the start-up failure does not say %q:\n%v", want, err)
		}
	}
}

// The buffer stops at its cap and keeps what came first.
func TestTheStartupLogKeepsItsFirstLinesAndStopsAtTheCap(t *testing.T) {
	var l startupLog
	for i := 0; i < startupLogLimit+10; i++ {
		l.Noticef("line %d", i)
	}
	lines := strings.Split(l.String(), "\n")
	if len(lines) != startupLogLimit {
		t.Fatalf("%d lines kept, want %d", len(lines), startupLogLimit)
	}
	if lines[0] != "[INF] line 0" || lines[startupLogLimit-1] != fmt.Sprintf("[INF] line %d", startupLogLimit-1) {
		t.Fatalf("the first and last kept lines are %q and %q", lines[0], lines[startupLogLimit-1])
	}
}
