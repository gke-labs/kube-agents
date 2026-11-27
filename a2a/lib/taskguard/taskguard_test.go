package taskguard

import (
	"bytes"
	"encoding/json"
	"errors"
	"log/slog"
	"runtime"
	"strings"
	"sync"
	"testing"
)

// jsonLogger records each log line as a decoded JSON object.
func jsonLogger() (*slog.Logger, *bytes.Buffer) {
	var buf bytes.Buffer
	return slog.New(slog.NewJSONHandler(&buf, &slog.HandlerOptions{Level: slog.LevelDebug})), &buf
}

func records(t *testing.T, buf *bytes.Buffer) []map[string]any {
	t.Helper()
	var out []map[string]any
	for _, line := range strings.Split(strings.TrimSpace(buf.String()), "\n") {
		if line == "" {
			continue
		}
		var m map[string]any
		if err := json.Unmarshal([]byte(line), &m); err != nil {
			t.Fatalf("log line %q: %v", line, err)
		}
		out = append(out, m)
	}
	return out
}

func TestGuard_NoPanicIsNilAndQuiet(t *testing.T) {
	log, buf := jsonLogger()
	before := Count()
	ran := false
	if err := Guard(log, "work", nil, func() { ran = true }); err != nil {
		t.Fatalf("Guard = %v, want nil", err)
	}
	if !ran {
		t.Fatal("fn did not run")
	}
	if Count() != before {
		t.Fatalf("Count moved from %d to %d without a panic", before, Count())
	}
	if buf.Len() != 0 {
		t.Fatalf("logged without a panic: %s", buf.String())
	}
}

func panicsHere() { panic("boom in the scrub") }

func TestGuard_PanicIsRecoveredLoggedCountedAndReturned(t *testing.T) {
	log, buf := jsonLogger()
	before := Count()
	err := Guard(log, "answer scrub", []any{"task", "task-1"}, panicsHere)
	var pe *PanicError
	if !errors.As(err, &pe) {
		t.Fatalf("Guard = %v, want a *PanicError", err)
	}
	if pe.What != "answer scrub" || pe.Value != "boom in the scrub" {
		t.Fatalf("PanicError = %+v", pe)
	}
	if !strings.Contains(err.Error(), ReasonToken) {
		t.Fatalf("error %q does not name %s", err, ReasonToken)
	}
	if got := Count(); got != before+1 {
		t.Fatalf("Count = %d, want %d", got, before+1)
	}
	recs := records(t, buf)
	if len(recs) != 1 {
		t.Fatalf("%d log records, want 1: %s", len(recs), buf.String())
	}
	r := recs[0]
	if r["level"] != "ERROR" || r["msg"] != LogMessage {
		t.Fatalf("record level/msg = %v/%v, want ERROR/%q", r["level"], r["msg"], LogMessage)
	}
	if r["what"] != "answer scrub" || r["panic"] != "panic value of type string" || r["task"] != "task-1" {
		t.Fatalf("record = %v", r)
	}
	// The panic's own text (which a caller may have built from task text)
	// reaches neither the log nor the error.
	if strings.Contains(buf.String(), "boom in the scrub") || strings.Contains(err.Error(), "boom in the scrub") {
		t.Fatalf("the panic's text leaked: log %s, error %q", buf.String(), err)
	}
	if stack, _ := r["stack"].(string); !strings.Contains(stack, "panicsHere") {
		t.Fatalf("stack does not reach the panicking frame: %q", stack)
	}
}

func TestGuard_RuntimeErrorAndNilPanicAreRecovered(t *testing.T) {
	log, _ := jsonLogger()
	var m map[string]int
	if err := Guard(log, "nil map", nil, func() { m["x"] = 1 }); err == nil {
		t.Fatal("a runtime error panic was not recovered")
	}
	err := Guard(log, "nil panic", nil, func() { panic(nil) })
	var pe *PanicError
	if !errors.As(err, &pe) {
		t.Fatalf("panic(nil): Guard = %v, want a *PanicError", err)
	}
	var pn *runtime.PanicNilError
	if v, ok := pe.Value.(error); !ok || !errors.As(v, &pn) {
		t.Fatalf("panic(nil) value = %#v, want *runtime.PanicNilError", pe.Value)
	}
}

func TestGuard_NilLoggerUsesDefault(t *testing.T) {
	if err := Guard(nil, "work", nil, panicsHere); err == nil {
		t.Fatal("panic not recovered with a nil logger")
	}
}

func TestGuard_GoexitIsNotSwallowed(t *testing.T) {
	log, buf := jsonLogger()
	before := Count()
	afterGuard := false
	var wg sync.WaitGroup
	wg.Add(1)
	go func() {
		defer wg.Done()
		_ = Guard(log, "goexit", nil, runtime.Goexit)
		afterGuard = true
	}()
	wg.Wait()
	if afterGuard {
		t.Fatal("Guard returned after runtime.Goexit; the goroutine should have ended")
	}
	if Count() != before {
		t.Fatalf("Goexit was counted: %d -> %d", before, Count())
	}
	if buf.Len() != 0 {
		t.Fatalf("Goexit was logged: %s", buf.String())
	}
}

func TestGuard_AttrsAreNotAliased(t *testing.T) {
	log, _ := jsonLogger()
	attrs := make([]any, 2, 8)
	attrs[0], attrs[1] = "task", "t"
	_ = Guard(log, "work", attrs, panicsHere)
	if got := attrs[:cap(attrs)][2]; got != nil {
		t.Fatalf("Guard wrote into the caller's attrs backing array: %v", got)
	}
}

// A runtime error's message is the Go runtime's, not task text, so it is
// logged as is: it says what went wrong (an index past a slice's end).
func TestGuard_ARuntimeErrorIsLoggedWithItsMessage(t *testing.T) {
	log, buf := jsonLogger()
	_ = Guard(log, "work", nil, func() {
		var s []int
		_ = s[3]
	})
	recs := records(t, buf)
	if len(recs) != 1 {
		t.Fatalf("%d log records, want 1", len(recs))
	}
	if p, _ := recs[0]["panic"].(string); !strings.Contains(p, "index out of range") {
		t.Fatalf("panic = %q, want the runtime's message", p)
	}
}
