package k8sapiproxy

import (
	"log/slog"
	"sync"
)

const (
	// ReasonCallerInFlightLimit is the rejection reason of a request answered 429 because its
	// caller already had CallerLimits.MaxInFlight requests in flight, and
	// ReasonCallerLongRunningLimit of one answered 429 because its caller already had
	// CallerLimits.MaxLongRunning watches and long-running subresource requests open.
	// ReasonCallerGroupInFlightLimit and ReasonCallerGroupLongRunningLimit are the same two
	// for the caller's CallerLimits.Group: the caller had room and its group had none. A
	// request the shared MaxInFlight cap turns away carries no reason.
	ReasonCallerInFlightLimit         RejectionReason = "caller_in_flight_limit"
	ReasonCallerLongRunningLimit      RejectionReason = "caller_long_running_limit"
	ReasonCallerGroupInFlightLimit    RejectionReason = "caller_group_in_flight_limit"
	ReasonCallerGroupLongRunningLimit RejectionReason = "caller_group_long_running_limit"

	callerThrottledMessage        = messagePrefix + "this caller has too many requests in flight; retry"
	callerLongRunningMessage      = messagePrefix + "this caller has too many watches and other long-running requests open; close one and retry"
	callerGroupThrottledMessage   = messagePrefix + "the callers this one is limited with have too many requests in flight; retry"
	callerGroupLongRunningMessage = messagePrefix + "the callers this one is limited with have too many watches and other long-running requests open; retry"

	// groupFillsCapWarning is logged once for a group whose in-flight limit is not below
	// the shared cap: nothing is then held back from that group for other callers.
	groupFillsCapWarning = "a caller group's in-flight limit is not below the shared in-flight cap, so the group's callers can take every shared slot and leave none for other callers; lower the group's limit or raise the cap"
	logKeyGroup          = "group"
	logKeyGroupLimit     = "group_max_in_flight"
	logKeySharedCap      = "max_in_flight"
	// maxWarnedGroups bounds the group names remembered as warned about, so an embedder that
	// makes up group names without end grows nothing; groups past it are not warned about.
	maxWarnedGroups = 64
)

// CallerLimits bounds what one caller, and optionally a group of callers it belongs to, may
// have open at once. Requests in flight and long-running requests are counted apart: a
// caller following a watch for an hour keeps its whole MaxInFlight for ordinary requests,
// and a burst of ordinary requests cannot close its streams out.
type CallerLimits struct {
	// Key names the count MaxInFlight and MaxLongRunning apply to. Empty is the caller's
	// CallerIdentity.Username, so every request under one username shares one count; an
	// embedder sets it to count more finely, such as per pod of one ServiceAccount, or
	// more coarsely. Callers that return the same Key share its counts. A Key is never the
	// count of a caller that returns none, even when it equals that caller's username:
	// keys and usernames are separate namespaces.
	Key string
	// MaxInFlight is the most requests of the kind Config.MaxInFlight counts (every request
	// that is not long-running) the Key may have in flight. Zero or negative is no limit.
	MaxInFlight int
	// MaxLongRunning is the most watches and long-running subresource requests (log,
	// followed or not, exec, attach, portforward, proxy) the Key may have open, which
	// Config.MaxInFlight does not count at all. Zero or negative is no limit.
	MaxLongRunning int

	// Group names a second pair of counts, shared by every caller that returns the same
	// Group, whatever its Key: a request is admitted only when its Key and its Group both
	// have room, and it counts for both until it ends. Empty is no group. Group names are a
	// namespace of their own.
	Group string
	// GroupMaxInFlight and GroupMaxLongRunning are MaxInFlight and MaxLongRunning for the
	// Group. Zero or negative is no limit. The limits in force are the ones returned for
	// the request being admitted: when callers of one Group return different limits, each
	// request is held to its own caller's, against the count they all share. A
	// GroupMaxInFlight that is not below a non-zero Config.MaxInFlight reserves nothing for
	// callers outside the group, and the handler logs a warning once for the group's name.
	GroupMaxInFlight    int
	GroupMaxLongRunning int
}

// callerLoad is what one key or one group has open.
type callerLoad struct {
	inFlight    int
	longRunning int
}

// count is the counter for a request of the given kind.
func (c *callerLoad) count(longRunning bool) *int {
	if longRunning {
		return &c.longRunning
	}
	return &c.inFlight
}

func (c *callerLoad) idle() bool {
	return c.inFlight == 0 && c.longRunning == 0
}

// callerLimiter counts the open requests of each limited caller and group. It holds an
// entry only while a request is open under a limit, so it is as large as the number of such
// callers and groups and no larger: an entry is deleted when its counts return to zero.
type callerLimiter struct {
	limits func(*CallerIdentity) CallerLimits
	// sharedCap is Config.MaxInFlight, which a group's in-flight limit is compared with.
	sharedCap int
	logger    *slog.Logger

	mu sync.Mutex
	// usernames counts callers whose limits name no Key, keys those that name one, and
	// groups the groups: three namespaces, so a Key equal to another caller's username is
	// not that caller's count.
	usernames map[string]*callerLoad
	keys      map[string]*callerLoad
	groups    map[string]*callerLoad
	// warned holds the groups groupFillsCapWarning was logged for, at most maxWarnedGroups.
	warned map[string]struct{}
}

// newCallerLimiter returns nil for a nil limits, and a nil limiter admits everything.
// sharedCap is Config.MaxInFlight and logger receives groupFillsCapWarning.
func newCallerLimiter(limits func(*CallerIdentity) CallerLimits, sharedCap int, logger *slog.Logger) *callerLimiter {
	if limits == nil {
		return nil
	}
	return &callerLimiter{
		limits:    limits,
		sharedCap: sharedCap,
		logger:    logger,
		usernames: map[string]*callerLoad{},
		keys:      map[string]*callerLoad{},
		groups:    map[string]*callerLoad{},
		warned:    map[string]struct{}{},
	}
}

// acquire counts one request of caller's, long-running or not, without waiting. ok is false
// when the caller's key or its group is at its limit for that kind, and reason then says
// which; the key is checked first. An admitted request is counted for the key and the group
// in one step, and release gives both back; it is safe to call more than once. A count with
// no limit for the kind is not kept.
func (l *callerLimiter) acquire(caller *CallerIdentity, longRunning bool) (release func(), reason RejectionReason, ok bool) {
	if l == nil {
		return func() {}, ReasonNone, true
	}
	limits := l.limits(caller)
	keyLimit, groupLimit := limits.MaxInFlight, limits.GroupMaxInFlight
	keyReason, groupReason := ReasonCallerInFlightLimit, ReasonCallerGroupInFlightLimit
	if longRunning {
		keyLimit, groupLimit = limits.MaxLongRunning, limits.GroupMaxLongRunning
		keyReason, groupReason = ReasonCallerLongRunningLimit, ReasonCallerGroupLongRunningLimit
	}
	if limits.Group == "" {
		groupLimit = 0
	}
	if limits.Group != "" {
		l.warnIfGroupFillsCap(limits.Group, limits.GroupMaxInFlight)
	}
	if keyLimit <= 0 && groupLimit <= 0 {
		return func() {}, ReasonNone, true
	}
	callers, key := l.keys, limits.Key
	if key == "" {
		callers, key = l.usernames, caller.Username
	}

	l.mu.Lock()
	defer l.mu.Unlock()
	if keyLimit > 0 && l.open(callers, key, longRunning) >= keyLimit {
		return nil, keyReason, false
	}
	if groupLimit > 0 && l.open(l.groups, limits.Group, longRunning) >= groupLimit {
		return nil, groupReason, false
	}
	if keyLimit > 0 {
		l.add(callers, key, longRunning, 1)
	}
	if groupLimit > 0 {
		l.add(l.groups, limits.Group, longRunning, 1)
	}
	return sync.OnceFunc(func() {
		l.mu.Lock()
		defer l.mu.Unlock()
		if keyLimit > 0 {
			l.add(callers, key, longRunning, -1)
		}
		if groupLimit > 0 {
			l.add(l.groups, limits.Group, longRunning, -1)
		}
	}), ReasonNone, true
}

// warnIfGroupFillsCap logs groupFillsCapWarning the first time group is seen with an
// in-flight limit that is not below a non-zero shared cap. A group with no in-flight limit
// is not a group for requests in flight, and is not warned about.
func (l *callerLimiter) warnIfGroupFillsCap(group string, groupMaxInFlight int) {
	if l.sharedCap <= 0 || groupMaxInFlight < l.sharedCap {
		return
	}
	l.mu.Lock()
	_, warned := l.warned[group]
	if !warned && len(l.warned) < maxWarnedGroups {
		l.warned[group] = struct{}{}
	} else {
		warned = true
	}
	l.mu.Unlock()
	if !warned {
		l.logger.Warn(groupFillsCapWarning, logKeyGroup, group, logKeyGroupLimit, groupMaxInFlight, logKeySharedCap, l.sharedCap)
	}
}

// open is the count of one kind under name. The caller holds mu.
func (l *callerLimiter) open(loads map[string]*callerLoad, name string, longRunning bool) int {
	load := loads[name]
	if load == nil {
		return 0
	}
	return *load.count(longRunning)
}

// add moves the count of one kind under name by delta, and drops the entry when nothing is
// left open under it. The caller holds mu.
func (l *callerLimiter) add(loads map[string]*callerLoad, name string, longRunning bool, delta int) {
	load := loads[name]
	if load == nil {
		load = &callerLoad{}
		loads[name] = load
	}
	*load.count(longRunning) += delta
	if load.idle() {
		delete(loads, name)
	}
}

// entries is how many usernames, keys and groups have a counted request open.
func (l *callerLimiter) entries() int {
	if l == nil {
		return 0
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	return len(l.usernames) + len(l.keys) + len(l.groups)
}

// callerLimitMessage is what the caller reads for a limit's reason.
func callerLimitMessage(reason RejectionReason) string {
	switch reason {
	case ReasonCallerLongRunningLimit:
		return callerLongRunningMessage
	case ReasonCallerGroupInFlightLimit:
		return callerGroupThrottledMessage
	case ReasonCallerGroupLongRunningLimit:
		return callerGroupLongRunningMessage
	}
	return callerThrottledMessage
}
