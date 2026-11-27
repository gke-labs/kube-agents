package k8sapiproxy

import "sync"

const (
	// ReasonCallerInFlightLimit is the rejection reason of a request answered 429 because its
	// caller already had CallerLimits.MaxInFlight requests in flight, and
	// ReasonCallerLongRunningLimit of one answered 429 because its caller already had
	// CallerLimits.MaxLongRunning watches and long-running subresource requests open. A
	// request the shared MaxInFlight cap turns away carries no reason.
	ReasonCallerInFlightLimit    RejectionReason = "caller_in_flight_limit"
	ReasonCallerLongRunningLimit RejectionReason = "caller_long_running_limit"

	callerThrottledMessage   = messagePrefix + "this caller has too many requests in flight; retry"
	callerLongRunningMessage = messagePrefix + "this caller has too many watches and other long-running requests open; close one and retry"
)

// CallerLimits bounds what one caller may have open at once. The two counts are separate: a
// caller following a watch for an hour keeps its whole MaxInFlight for ordinary requests,
// and a burst of ordinary requests cannot close its streams out.
type CallerLimits struct {
	// MaxInFlight is the most requests of the kind Config.MaxInFlight counts (every request
	// that is not long-running) the caller may have in flight. Zero or negative is no limit.
	MaxInFlight int
	// MaxLongRunning is the most watches and long-running subresource requests (log, exec,
	// attach, portforward, proxy) the caller may have open, which Config.MaxInFlight does
	// not count at all. Zero or negative is no limit.
	MaxLongRunning int
}

// callerLoad is what one caller has open.
type callerLoad struct {
	inFlight    int
	longRunning int
}

// callerLimiter counts each limited caller's open requests. It holds an entry only for a
// caller with a request open under a limit, so it is as large as the number of such callers
// and no larger: an entry is deleted when its counts return to zero.
type callerLimiter struct {
	limits func(*CallerIdentity) CallerLimits

	mu    sync.Mutex
	loads map[string]*callerLoad
}

// newCallerLimiter returns nil for a nil limits, and a nil limiter admits everything.
func newCallerLimiter(limits func(*CallerIdentity) CallerLimits) *callerLimiter {
	if limits == nil {
		return nil
	}
	return &callerLimiter{limits: limits, loads: map[string]*callerLoad{}}
}

// acquire counts one request of caller's, long-running or not, without waiting. ok is false
// when the caller is at its limit for that kind, and reason then says which. release gives
// the count back; it is safe to call more than once. A caller with no limit for the kind is
// not counted.
func (l *callerLimiter) acquire(caller *CallerIdentity, longRunning bool) (release func(), reason RejectionReason, ok bool) {
	if l == nil {
		return func() {}, ReasonNone, true
	}
	limits := l.limits(caller)
	limit, reason := limits.MaxInFlight, ReasonCallerInFlightLimit
	if longRunning {
		limit, reason = limits.MaxLongRunning, ReasonCallerLongRunningLimit
	}
	if limit <= 0 {
		return func() {}, ReasonNone, true
	}

	// The username alone is the key. See Config.CallerLimits for why the partition is not
	// part of it.
	key := caller.Username
	l.mu.Lock()
	defer l.mu.Unlock()
	load := l.loads[key]
	if load == nil {
		load = &callerLoad{}
	}
	count := &load.inFlight
	if longRunning {
		count = &load.longRunning
	}
	if *count >= limit {
		return nil, reason, false
	}
	*count++
	l.loads[key] = load
	return sync.OnceFunc(func() {
		l.mu.Lock()
		defer l.mu.Unlock()
		*count--
		if load.inFlight == 0 && load.longRunning == 0 {
			delete(l.loads, key)
		}
	}), ReasonNone, true
}

// callers is how many callers have a counted request open.
func (l *callerLimiter) callers() int {
	if l == nil {
		return 0
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	return len(l.loads)
}
