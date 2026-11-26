// Package taskguard recovers a panic raised while one task is handled, so
// the process that runs many tasks fails that one and keeps serving the
// rest. It logs the panic at Error under a stable message, counts it, and
// hands the caller an error to fail the task with. The caller decides what
// failing closed means for its task: the guard publishes nothing.
package taskguard

import (
	"fmt"
	"log/slog"
	"runtime"
	"runtime/debug"
	"sync/atomic"
)

const (
	// LogMessage is the message of the Error record Guard writes for each
	// panic it recovers, kept stable so an operator can alert on it.
	LogMessage = "recovered a panic"
	// ReasonToken is the reason token a task a recovered panic ended carries
	// in its terminal: `reason: internal-panic`.
	ReasonToken = "internal-panic"
	// Reason is ReasonToken in the executors' reason grammar,
	// `reason: <token>[ - detail]`.
	Reason = "reason: " + ReasonToken
)

// recovered counts the panics Guard recovered in this process.
var recovered atomic.Int64

// Count is how many panics Guard has recovered in this process.
func Count() int64 { return recovered.Load() }

// PanicError is the error Guard returns for a recovered panic.
type PanicError struct {
	// What names the work that panicked, as the caller gave it to Guard.
	What string
	// Value is what was passed to panic.
	Value any
}

func (e *PanicError) Error() string {
	return fmt.Sprintf("%s: panic in %s: %s", ReasonToken, e.What, Summary(e.Value))
}

// Summary describes a panic value without carrying task text: a runtime
// error's message, which the Go runtime writes (an index past a slice's end,
// a nil map), and otherwise only the value's type. A value a caller built
// from model or user text (a string, an error wrapping it) could hold a
// secret the scrub was meant to remove, so its contents never reach a log.
func Summary(v any) string {
	if re, ok := v.(runtime.Error); ok {
		return re.Error()
	}
	return fmt.Sprintf("panic value of type %T", v)
}

// Guard runs fn and recovers a panic it raises: logged at Error with
// LogMessage, what, the panic's Summary and the stack, counted (Count), and
// returned as a *PanicError. nil means fn returned normally. attrs are
// slog key-value pairs added to the record (the task's id, say). A nil log
// is slog.Default(). runtime.Goexit is not a panic: it is not recovered,
// logged or counted, and carries on ending the goroutine.
func Guard(log *slog.Logger, what string, attrs []any, fn func()) (err error) {
	returned := false
	defer func() {
		if returned {
			return
		}
		r := recover()
		if r == nil {
			// runtime.Goexit: since Go 1.21 panic(nil) recovers a
			// *runtime.PanicNilError, so nil here is only Goexit, which
			// keeps unwinding once this returns.
			return
		}
		recovered.Add(1)
		if log == nil {
			log = slog.Default()
		}
		args := make([]any, 0, len(attrs)+6)
		args = append(args, attrs...)
		args = append(args, "what", what, "panic", Summary(r), "stack", string(debug.Stack()))
		log.Error(LogMessage, args...)
		err = &PanicError{What: what, Value: r}
	}()
	fn()
	returned = true
	return nil
}
