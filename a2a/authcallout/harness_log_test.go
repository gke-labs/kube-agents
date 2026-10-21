package authcallout

import (
	"fmt"
	"strings"
	"sync"

	natsserver "github.com/nats-io/nats-server/v2/server"
)

// startupLog keeps every line the server logs, at every level, so a harness
// whose server never becomes ready can show why. The harnesses start their
// server with NoLog, which leaves it without a logger, and nats-server reports
// a listener it cannot bind through Fatalf and returns from Start; without a
// logger installed before Start the only symptom is the ReadyForConnections
// timeout. violationLog (supervisor_split_test.go) is the sibling that keeps
// only the error level for the tests that read permission violations.
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
