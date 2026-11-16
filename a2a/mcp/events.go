package mcp

import (
	"cmp"
	"context"
	"encoding/json"
	"fmt"
	"slices"
	"strings"
	"sync"
	"time"
	"unicode"
	"unicode/utf8"

	"github.com/nats-io/nats.go"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The frames a console conversation is posted that are not about the turn
// being sent: a card's reports (kind "notify", a2a/gateway/notify.go), which
// can come minutes after the turn that filed the card ended. The gateway
// publishes them whether or not anyone listens, so this server listens to
// every conversation it knows and keeps them, per conversation and bounded,
// for send_message to gather and poll_events to read. A delegation chain's
// terminal result (kind "result") is posted there too, and not kept: the
// chain's tasks hold it, and send_message and task_control read it there.
// Live only: a frame published before the server heard of the conversation,
// or before a restart, is lost.
const (
	frameKindNotify = "notify"
	frameKindResult = "result"

	// maxEventsPerConversation and maxEventBytesPerConversation bound one
	// conversation's buffer; past either the oldest frames are dropped, and
	// counted.
	maxEventsPerConversation     = 256
	maxEventBytesPerConversation = 1 << 20
	// maxEventTextRunes caps one frame's text before it is kept: the gateway
	// caps what it sends, but the buffer does not take its word for it.
	maxEventTextRunes = 16 * 1024
	// maxEventIDBytes bounds a frame's message id, and maxEventStateBytes its
	// state; a frame whose id is past it is refused, a state past it unset.
	maxEventIDBytes    = 256
	maxEventStateBytes = 32
	// eventRetention is how long a conversation no name holds keeps its
	// listener and buffer after it was last used or last posted to.
	eventRetention = time.Hour
	// maxEventConversations bounds the conversations listened to, one
	// subscription each: as many as the alias table holds.
	maxEventConversations = maxConversations

	textShortened = "\n…(shortened)"
)

// maxEventBytesTotal bounds every conversation's buffers together: past it
// the oldest frames across all of them, by cursor, are dropped, and counted
// in their conversation's log. The per-conversation bounds alone would let
// every listener fill its own. A variable so tests can make it small.
var maxEventBytesTotal = 32 << 20

// coreSubscriber is what Events subscribes through: lib.Client's core
// subscriptions, which survive a connection rebuild and run their handler on
// their own goroutine, so a listener is always drained.
type coreSubscriber interface {
	SubscribeCore(subject string, handler nats.MsgHandler) (lib.Subscription, error)
}

// EventFrame is one buffered frame, under the cursor it was last written at.
type EventFrame struct {
	ConsoleOutFrame
	Cursor uint64
	// Edited is a frame that an edit wrote: it replaced the frame first
	// heard, or it came for one this server never heard.
	Edited bool
}

// EventPage is a conversation's buffered frames newer than a cursor.
type EventPage struct {
	Frames []EventFrame
	// Cursor is the latest cursor across every conversation; reading since
	// it returns only what comes after.
	Cursor uint64
	// Dropped counts the conversation's frames dropped for a bound, and
	// DroppedNewest is the newest cursor among them: a reader whose since
	// is below it missed one.
	Dropped       uint64
	DroppedNewest uint64
	Rejected      uint64
}

// Events holds every known conversation's listener and buffer. Its cursor is
// one count across all of them, so a cursor read on a conversation stays
// meaningful when that conversation's listener goes and comes back.
type Events struct {
	client coreSubscriber
	mu     sync.Mutex
	logs   map[string]*eventLog
	cursor uint64
	// bytes is what every log's frames hold together.
	bytes int
	// now is the clock retention is measured by; tests set it.
	now func() time.Time
}

// eventLog is one conversation's listener and buffer. frames is in arrival
// order: an edit keeps its frame's place and takes a new cursor.
type eventLog struct {
	token string
	sub   lib.Subscription
	// ready is closed once the subscription is live, or failed with err.
	ready chan struct{}
	err   error
	// named is a conversation the alias table holds, which keeps its
	// listener whatever its age.
	named bool
	// pins counts the turns gathering from this log; a pinned log is never
	// swept, unnamed away or evicted.
	pins          int
	frames        []EventFrame
	bytes         int
	dropped       uint64
	droppedNewest uint64
	rejected      uint64
	// active is the last use or the last frame, which retention counts from.
	active time.Time
	// changed is closed, and replaced, on every frame kept.
	changed chan struct{}
}

// NewEvents returns an empty set of listeners on client.
func NewEvents(client coreSubscriber) *Events {
	return &Events{client: client, logs: map[string]*eventLog{}, now: time.Now}
}

// Watch listens to the conversation's frames, if it does not already, and
// marks it used; named says the alias table holds it. It reports whether the
// server was already listening. Once it returns, every frame published after
// is heard.
func (e *Events) Watch(token string, named bool) (bool, error) {
	heard, _, err := e.watch(token, named, false)
	return heard, err
}

// Pin is Watch for a turn that gathers from the conversation: until unpin is
// called, its listener and buffer stay, whatever drops its name or fills the
// set of listeners.
func (e *Events) Pin(token string) (unpin func(), err error) {
	_, unpin, err = e.watch(token, false, true)
	return unpin, err
}

func (e *Events) watch(token string, named, pin bool) (bool, func(), error) {
	if e == nil {
		return false, func() {}, nil
	}
	if !lib.ValidSubjectToken(token) {
		return false, nil, fmt.Errorf("%q is not a conversation token", token)
	}
	e.mu.Lock()
	if l, ok := e.logs[token]; ok {
		l.named = l.named || named
		l.active = e.now()
		unpin := e.pinLocked(l, pin)
		e.mu.Unlock()
		<-l.ready
		if l.err != nil {
			unpin()
		}
		return true, unpin, l.err
	}
	stale, err := e.sweepLocked()
	if err != nil {
		e.mu.Unlock()
		stopAll(stale)
		return false, nil, err
	}
	l := &eventLog{token: token, ready: make(chan struct{}), named: named, active: e.now(), changed: make(chan struct{})}
	unpin := e.pinLocked(l, pin)
	e.logs[token] = l
	e.mu.Unlock()
	stopAll(stale)

	// Subscribed outside the lock: the subscribe waits on the server, and
	// every listener's handler takes the lock.
	sub, err := e.client.SubscribeCore(fmt.Sprintf(consoleOutSubject, token), func(m *nats.Msg) { e.receive(l, m.Data) })
	e.mu.Lock()
	current := e.logs[token] == l
	switch {
	case err != nil:
		l.err = err
		if current {
			e.removeLocked(l)
		}
	case !current:
		// Dropped while it subscribed: its frames would go nowhere.
		l.err = fmt.Errorf("the listener for conversation %s was dropped while it subscribed; try again", token)
	default:
		l.sub = sub
	}
	err = l.err
	close(l.ready)
	e.mu.Unlock()
	if sub != nil && !current {
		sub.Stop()
	}
	if err != nil {
		return false, nil, err
	}
	return false, unpin, nil
}

// pinLocked pins l when pin is set, and returns what unpins it, once.
func (e *Events) pinLocked(l *eventLog, pin bool) func() {
	if !pin {
		return func() {}
	}
	l.pins++
	var once sync.Once
	return func() {
		once.Do(func() {
			e.mu.Lock()
			l.pins--
			l.active = e.now()
			e.mu.Unlock()
		})
	}
}

// Unname records that the alias table no longer holds the conversation. Its
// listener goes now if nothing is buffered and no turn is gathering from it,
// else once retention passes.
func (e *Events) Unname(token string) {
	if e == nil || token == "" {
		return
	}
	e.mu.Lock()
	l, ok := e.logs[token]
	var stale []lib.Subscription
	if ok {
		l.named = false
		l.active = e.now()
		if len(l.frames) == 0 && l.pins == 0 {
			stale = e.removeLocked(l)
		}
	}
	e.mu.Unlock()
	stopAll(stale)
}

// sweepLocked drops the listeners retention has passed, then, while the set
// is full, the least recently active, one no name holds first, so that one
// more fits. A pinned listener is never dropped; a full set of them is an
// error. It returns the subscriptions to stop once the lock is released.
func (e *Events) sweepLocked() ([]lib.Subscription, error) {
	var stale []lib.Subscription
	now := e.now()
	for _, l := range e.logs {
		if !l.named && l.pins == 0 && now.Sub(l.active) > eventRetention {
			stale = append(stale, e.removeLocked(l)...)
		}
	}
	for len(e.logs) >= maxEventConversations {
		var oldest *eventLog
		for _, l := range e.logs {
			if l.pins > 0 {
				continue
			}
			if oldest == nil || (oldest.named && !l.named) ||
				(oldest.named == l.named && l.active.Before(oldest.active)) {
				oldest = l
			}
		}
		if oldest == nil {
			return stale, fmt.Errorf("all %d conversation listeners are in use by running turns; try again when one ends", len(e.logs))
		}
		stale = append(stale, e.removeLocked(oldest)...)
	}
	return stale, nil
}

// removeLocked drops l and its frames. A listener still subscribing is
// stopped by Watch.
func (e *Events) removeLocked(l *eventLog) []lib.Subscription {
	delete(e.logs, l.token)
	e.bytes -= l.bytes
	// A poll waiting on this log wakes and reads afresh, rather than waiting
	// out its wait on a log no longer listened to.
	close(l.changed)
	l.changed = make(chan struct{})
	if l.sub == nil {
		return nil
	}
	sub := l.sub
	l.sub = nil
	return []lib.Subscription{sub}
}

func stopAll(subs []lib.Subscription) {
	for _, s := range subs {
		s.Stop()
	}
}

// receive keeps one frame heard for l, if it is a report.
func (e *Events) receive(l *eventLog, data []byte) {
	f, keep, valid := parseEventFrame(data)
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.logs[l.token] != l {
		return
	}
	switch {
	case !valid:
		l.rejected++
	case keep:
		e.addLocked(l, f)
		l.active = e.now()
	}
}

// parseEventFrame reads a frame off .out. keep is a report, shaped to be
// kept: text capped and valid UTF-8, ids that are not a task id's shape
// unset. valid is false for a frame no gateway sends: not JSON, or a report
// without a usable message id. A notice about a turn, or a chain's result,
// is valid and not kept.
func parseEventFrame(data []byte) (f ConsoleOutFrame, keep, valid bool) {
	if json.Unmarshal(data, &f) != nil {
		return f, false, false
	}
	if f.Kind != frameKindNotify {
		return f, false, true
	}
	if f.MessageID == "" || len(f.MessageID) > maxEventIDBytes || !utf8.ValidString(f.MessageID) {
		return f, false, false
	}
	for _, id := range []*string{&f.TaskID, &f.ReplyTo, &f.Addressee} {
		if !lib.ValidSubjectToken(*id) {
			*id = ""
		}
	}
	if len(f.State) > maxEventStateBytes || !lib.ValidSubjectToken(f.State) {
		f.State = ""
	}
	f.Text = capRunes(strings.ToValidUTF8(f.Text, "�"), maxEventTextRunes)
	return f, true, true
}

func capRunes(s string, n int) string {
	if utf8.RuneCountInString(s) <= n {
		return s
	}
	return string([]rune(s)[:n]) + textShortened
}

func frameSize(f ConsoleOutFrame) int {
	return len(f.MessageID) + len(f.Text) + len(f.Kind) + len(f.State) + len(f.Addressee) + len(f.TaskID) + len(f.ReplyTo)
}

// addLocked buffers f in l under the next cursor, then holds every buffer
// within the total bound by dropping the oldest frames across all of them,
// never the one just written. Pinned logs are not spared: the total bound is
// the server's memory.
func (e *Events) addLocked(l *eventLog, f ConsoleOutFrame) {
	before := l.bytes
	if !l.add(f, e.cursor+1) {
		return
	}
	e.cursor++
	e.bytes += l.bytes - before
	for e.bytes > maxEventBytesTotal {
		var from *eventLog
		at := -1
		for _, o := range e.logs {
			for i, b := range o.frames {
				if b.Cursor != e.cursor && (from == nil || b.Cursor < from.frames[at].Cursor) {
					from, at = o, i
				}
			}
		}
		if from == nil {
			return
		}
		e.bytes -= from.drop(at)
	}
}

// add buffers f under cursor: an edit replaces the frame with its message id
// in place, anything else appends. A frame that is not an edit, for a message
// an edit already wrote, is older than what is kept and is not. Then the
// oldest frames go until the buffer is within its bounds, never the one just
// written. It reports whether f was kept.
func (l *eventLog) add(f ConsoleOutFrame, cursor uint64) bool {
	at := slices.IndexFunc(l.frames, func(b EventFrame) bool { return b.MessageID == f.MessageID })
	if !f.Edit && at >= 0 && l.frames[at].Edited {
		return false
	}
	if !f.Edit {
		at = -1
	}
	if at >= 0 {
		l.bytes += frameSize(f) - frameSize(l.frames[at].ConsoleOutFrame)
		l.frames[at] = EventFrame{ConsoleOutFrame: f, Cursor: cursor, Edited: true}
	} else {
		l.frames = append(l.frames, EventFrame{ConsoleOutFrame: f, Cursor: cursor, Edited: f.Edit})
		l.bytes += frameSize(f)
		at = len(l.frames) - 1
	}
	// Past a bound, the frame with the oldest cursor goes, not the first
	// in place: an edit keeps its place but takes a new cursor. The frame
	// just written stays.
	for len(l.frames) > maxEventsPerConversation || l.bytes > maxEventBytesPerConversation {
		i := -1
		for j := range l.frames {
			if j != at && (i < 0 || l.frames[j].Cursor < l.frames[i].Cursor) {
				i = j
			}
		}
		if i < 0 {
			break
		}
		l.drop(i)
		if i < at {
			at--
		}
	}
	close(l.changed)
	l.changed = make(chan struct{})
	return true
}

// drop removes frame i for a bound, counts it, and returns its size.
func (l *eventLog) drop(i int) int {
	f := l.frames[i]
	n := frameSize(f.ConsoleOutFrame)
	l.bytes -= n
	l.frames = slices.Delete(l.frames, i, i+1)
	l.dropped++
	l.droppedNewest = max(l.droppedNewest, f.Cursor)
	return n
}

// snapshot is the conversation's buffer in arrival order, the latest cursor,
// the conversation's counters, and a channel closed on its next frame. A
// conversation not listened to has no frames or counters, and a nil channel.
func (e *Events) snapshot(token string) (EventPage, <-chan struct{}) {
	if e == nil {
		return EventPage{}, nil
	}
	e.mu.Lock()
	defer e.mu.Unlock()
	l, ok := e.logs[token]
	if !ok {
		return EventPage{Cursor: e.cursor}, nil
	}
	return EventPage{Frames: slices.Clone(l.frames), Cursor: e.cursor, Dropped: l.dropped, DroppedNewest: l.droppedNewest,
		Rejected: l.rejected}, l.changed
}

// Cursor is the latest cursor across every conversation: 0 before the first
// frame this server kept.
func (e *Events) Cursor() uint64 {
	if e == nil {
		return 0
	}
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.cursor
}

// Since returns the conversation's frames newer than since, in cursor order,
// waiting up to wait for one when there are none yet.
func (e *Events) Since(ctx context.Context, token string, since uint64, wait time.Duration) EventPage {
	var timeout <-chan time.Time
	if wait > 0 {
		t := time.NewTimer(wait)
		defer t.Stop()
		timeout = t.C
	}
	for {
		p, changed := e.snapshot(token)
		p.Frames = slices.DeleteFunc(p.Frames, func(f EventFrame) bool { return f.Cursor <= since })
		if len(p.Frames) > 0 || timeout == nil || changed == nil {
			slices.SortFunc(p.Frames, func(a, b EventFrame) int { return cmp.Compare(a.Cursor, b.Cursor) })
			return p
		}
		select {
		case <-changed:
		case <-timeout:
			timeout = nil
		case <-ctx.Done():
			timeout = nil
		}
	}
}

// inertBlock renders text written by a model or an agent so it reads as
// text and nothing else: in a fence longer than any backtick run it holds,
// without control characters (bar newlines and tabs) or invisible format
// characters, which could hide or reorder what the caller is shown.
func inertBlock(text string) string {
	text = strings.Map(func(r rune) rune {
		switch {
		case r == '\n' || r == '\t':
			return r
		case unicode.IsControl(r), unicode.Is(unicode.Cf, r):
			return -1
		}
		return r
	}, text)
	run, longest := 0, 0
	for _, r := range text {
		if r == '`' {
			run++
			longest = max(longest, run)
		} else {
			run = 0
		}
	}
	fence := strings.Repeat("`", max(minFenceLen, longest+1))
	return fence + fenceText + "\n" + strings.TrimRight(text, "\n") + "\n" + fence + "\n"
}
