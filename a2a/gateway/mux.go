package gateway

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"strings"
	"sync"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

const (
	// A chat backend that stops is run again after a delay that doubles
	// from muxRestartBase up to muxRestartMax. A run that lasted at least
	// muxRestartMax before stopping starts the doubling over, so a backend
	// that drops once a day comes back in a second, and one with a bad
	// token settles into one attempt a minute.
	muxRestartBase = time.Second
	muxRestartMax  = time.Minute

	// muxUnknownBackendFormat refuses a direct-message user id whose
	// backend prefix names no backend the mux runs.
	muxUnknownBackendFormat = "multi adapter: no backend %q for a direct message"
)

// errNoPrimaryBackend refuses a bare direct-message user id when the mux runs
// more than one real backend and so has no primary to send it to.
var errNoPrimaryBackend = errors.New("multi adapter: a direct message needs a backend-qualified user id (<backend>:<id>) when no single backend is primary")

// knownBackendNames are the backend prefixes OpenDirect refuses by name when
// the mux does not run them, rather than hand the id whole to the primary.
// TestKnownBackendNamesCoverEveryBackend fails when Config.Backends arms a
// backend this map does not name.
var knownBackendNames = map[string]bool{
	BackendGchat: true, BackendSlack: true, BackendDiscord: true, BackendConsole: true,
}

// MultiAdapter presents several backends to the gateway as one Adapter.
// Conversation ids are backend-qualified by convention (discord:…, gchat:…,
// console:…), so every per-conversation operation dispatches on the prefix.
// OpenDirect takes a backend-qualified user id (slack:U1) and goes to that
// backend with the bare id. A prefix that names a chat backend the mux does
// not run is refused by name. Any other id goes whole to the primary
// backend, the one real backend when the process runs only one, and is
// refused when there is no primary. Run stamps every inbound message that
// names no backend with the key of the backend that delivered it.
//
// One backend is essential: when it stops, Run returns and the process
// restarts. Every other backend is contained: when it stops, Run logs it and
// runs it again after a backoff while the rest keep going. The console is
// the essential one, because it is the way in when chat is broken, and a
// chat backend that cannot connect (a bad or revoked token) must not take it
// down with it.
type MultiAdapter struct {
	primary   string
	essential string
	byPrefix  map[string]Adapter
	log       *slog.Logger

	restartBase, restartMax time.Duration
	after                   func(time.Duration) <-chan time.Time
}

// NewMultiAdapter builds the mux. essential must be a key of byPrefix, and so
// must primary unless it is "", which means no single backend is primary.
func NewMultiAdapter(primary, essential string, byPrefix map[string]Adapter, log *slog.Logger) (*MultiAdapter, error) {
	if _, ok := byPrefix[primary]; primary != "" && !ok {
		return nil, fmt.Errorf("multi adapter: primary backend %q is not configured", primary)
	}
	if _, ok := byPrefix[essential]; !ok {
		return nil, fmt.Errorf("multi adapter: essential backend %q is not configured", essential)
	}
	for name, a := range byPrefix {
		if a == nil {
			return nil, fmt.Errorf("multi adapter: backend %q is nil", name)
		}
	}
	if log == nil {
		log = slog.Default()
	}
	return &MultiAdapter{
		primary: primary, essential: essential, byPrefix: byPrefix, log: log,
		restartBase: muxRestartBase, restartMax: muxRestartMax, after: time.After,
	}, nil
}

// backendPrefix returns the backend name a conversation id carries, or "".
func backendPrefix(conversation string) string {
	prefix, _, ok := strings.Cut(conversation, ":")
	if !ok {
		return ""
	}
	return prefix
}

func (m *MultiAdapter) pick(conversation string) (Adapter, error) {
	prefix := backendPrefix(conversation)
	a, ok := m.byPrefix[prefix]
	if !ok {
		return nil, fmt.Errorf("multi adapter: no backend for conversation %q (prefix %q)", conversation, prefix)
	}
	return a, nil
}

// Run runs every backend until ctx is done or the essential backend stops.
// It returns nil on ctx, and otherwise the essential backend's error. A
// backend that returns nil while ctx is still live has stopped all the same,
// and is treated as a failure: an essential one that returned nil would
// otherwise leave the process running with its door shut.
func (m *MultiAdapter) Run(ctx context.Context, handler func(InboundMessage)) error {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	essentialDone := make(chan error, 1)
	var wg sync.WaitGroup
	for name, a := range m.byPrefix {
		wg.Add(1)
		go func(name string, a Adapter) {
			defer wg.Done()
			stamped := func(msg InboundMessage) {
				if msg.Backend == "" {
					msg.Backend = name
				}
				handler(msg)
			}
			if name == m.essential {
				essentialDone <- stopped(ctx, name, a.Run(ctx, stamped))
				return
			}
			m.runContained(ctx, name, a, stamped)
		}(name, a)
	}
	var err error
	select {
	case <-ctx.Done():
	case err = <-essentialDone:
	}
	cancel()
	wg.Wait()
	return err
}

// Close closes every backend that holds a connection of its own to close.
// Gateway.Run calls it after the relay's stop drain, so a batch still
// rendering when ingress stopped can post through the backend.
func (m *MultiAdapter) Close() {
	for _, a := range m.byPrefix {
		closeAdapter(a)
	}
}

// closeAdapter closes an adapter that holds a connection of its own; the
// rest have nothing to close.
func closeAdapter(a Adapter) {
	if c, ok := a.(interface{ Close() }); ok {
		c.Close()
	}
}

// stopped names why a backend's Run returned: nil if ctx ended it, its own
// error otherwise, and an error of our own if it returned nil early.
func stopped(ctx context.Context, name string, err error) error {
	if ctx.Err() != nil {
		return nil
	}
	if err == nil {
		err = errors.New("stopped while running")
	}
	return fmt.Errorf("%s adapter: %w", name, err)
}

// runContained runs one non-essential backend until ctx is done, running it
// again after a backoff whenever it stops.
func (m *MultiAdapter) runContained(ctx context.Context, name string, a Adapter, handler func(InboundMessage)) {
	delay := m.restartBase
	for {
		start := time.Now()
		err := stopped(ctx, name, a.Run(ctx, handler))
		if err == nil {
			return
		}
		if time.Since(start) >= m.restartMax {
			delay = m.restartBase
		}
		m.log.Error("chat backend stopped; restarting it, the console stays up",
			"backend", name, "err", err, "retryIn", delay)
		select {
		case <-ctx.Done():
			return
		case <-m.after(delay):
		}
		delay = min(delay*2, m.restartMax)
	}
}

func (m *MultiAdapter) Post(conversation, text string) (string, error) {
	a, err := m.pick(conversation)
	if err != nil {
		return "", err
	}
	return a.Post(conversation, text)
}

func (m *MultiAdapter) Edit(conversation, messageID, text string) error {
	a, err := m.pick(conversation)
	if err != nil {
		return err
	}
	return a.Edit(conversation, messageID, text)
}

func (m *MultiAdapter) PostMessage(conversation string, msg OutMessage) (string, error) {
	a, err := m.pick(conversation)
	if err != nil {
		return "", err
	}
	return a.PostMessage(conversation, msg)
}

func (m *MultiAdapter) EditMessage(conversation, messageID string, msg OutMessage) error {
	a, err := m.pick(conversation)
	if err != nil {
		return err
	}
	return a.EditMessage(conversation, messageID, msg)
}

func (m *MultiAdapter) Roster(conversation string) ([]string, bool, error) {
	a, err := m.pick(conversation)
	if err != nil {
		return nil, false, err
	}
	return a.Roster(conversation)
}

// OpenDirect opens a direct message with a backend-qualified user id
// (<backend>:<id>) on that backend, with the bare id. A prefix that names a
// chat backend the mux knows (gchat, slack, discord, console) but does not
// run (slack:U1 on a gateway without Slack) is refused by name, on any mux. On a mux with several real backends a
// caller must qualify the id: a bare id, or one whose prefix names no
// backend the mux runs, is refused. A mux with a single real backend takes
// any id that backend understands: an id whose prefix is no backend name (a
// Slack guest id from another workspace is itself T…:U…) goes whole to the
// primary, as a bare id does.
func (m *MultiAdapter) OpenDirect(userID string) (string, error) {
	backend, id, qualified := strings.Cut(userID, ":")
	if qualified {
		if a, armed := m.byPrefix[backend]; armed {
			return a.OpenDirect(id)
		}
		if knownBackendNames[backend] {
			return "", fmt.Errorf(muxUnknownBackendFormat, backend)
		}
	}
	if m.primary == "" {
		if qualified {
			return "", fmt.Errorf(muxUnknownBackendFormat, backend)
		}
		return "", errNoPrimaryBackend
	}
	return m.byPrefix[m.primary].OpenDirect(userID)
}

func (m *MultiAdapter) React(conversation, messageID, token string) error {
	a, err := m.pick(conversation)
	if err != nil {
		return err
	}
	return a.React(conversation, messageID, token)
}

func (m *MultiAdapter) RemoveReaction(conversation, messageID, token string) error {
	a, err := m.pick(conversation)
	if err != nil {
		return err
	}
	return a.RemoveReaction(conversation, messageID, token)
}

// MarkChanged and InboundVerified route like the observer calls below: to
// the backend that owns the conversation, when it implements the interface.
func (m *MultiAdapter) MarkChanged(conversation, ask, settle string) {
	if a, err := m.pick(conversation); err == nil {
		if observer, ok := a.(MarkObserver); ok {
			observer.MarkChanged(conversation, ask, settle)
		}
	}
}

// PostTerminal goes to the backend that owns the conversation
// (TerminalPoster); one that does not implement it answers
// errNoTerminalFrames, which the caller treats as nothing to do.
func (m *MultiAdapter) PostTerminal(conversation string, t ConsoleTerminal) error {
	a, err := m.pick(conversation)
	if err != nil {
		return err
	}
	poster, ok := a.(TerminalPoster)
	if !ok {
		return errNoTerminalFrames
	}
	return poster.PostTerminal(conversation, t)
}

// ShowsTaskStatus answers for the backend that owns the conversation
// (StatusShower); one that does not implement it keeps the rolling line.
func (m *MultiAdapter) ShowsTaskStatus(conversation string) bool {
	a, err := m.pick(conversation)
	if err != nil {
		return false
	}
	shower, ok := a.(StatusShower)
	return ok && shower.ShowsTaskStatus(conversation)
}

// PostPrivate goes to the backend that owns the conversation
// (PrivatePoster); one that has no private post answers errNoPrivatePosts.
func (m *MultiAdapter) PostPrivate(conversation, viewer, text string) error {
	a, err := m.pick(conversation)
	if err != nil {
		return err
	}
	poster, ok := a.(PrivatePoster)
	if !ok {
		return errNoPrivatePosts
	}
	return poster.PostPrivate(conversation, viewer, text)
}

// AnswersOnLine answers for the backend that owns the conversation
// (LineAnswerer); one that does not implement it keeps the post and the
// terminal edit.
func (m *MultiAdapter) AnswersOnLine(conversation string) bool {
	a, err := m.pick(conversation)
	if err != nil {
		return false
	}
	answerer, ok := a.(LineAnswerer)
	return ok && answerer.AnswersOnLine(conversation)
}

func (m *MultiAdapter) InboundVerified(conversation, messageID string, verified bool) {
	if a, err := m.pick(conversation); err == nil {
		if observer, ok := a.(VerifiedObserver); ok {
			observer.InboundVerified(conversation, messageID, verified)
		}
	}
}

// The gateway finds TaskObserver and SessionLookupSink by type assertion on
// the top of the adapter stack, and with a real chat backend the mux is that
// top (or the primary under the inject door, which asserts on it in turn).
// So the mux implements both unconditionally and passes each call on: the
// observer calls to the backend that owns the conversation's prefix, when it
// implements TaskObserver, and the session lookup to every backend that
// implements SessionLookupSink. A backend that implements neither is told
// nothing, as it would be alone. The Slack adapter is the one that needs
// both: without them it never learns which threads are sessions and drops
// every unmentioned reply.
func (m *MultiAdapter) observerFor(conversation string) (TaskObserver, bool) {
	a, err := m.pick(conversation)
	if err != nil {
		return nil, false
	}
	observer, ok := a.(TaskObserver)
	return observer, ok
}

func (m *MultiAdapter) TaskStarted(conversation, taskID string) {
	if observer, ok := m.observerFor(conversation); ok {
		observer.TaskStarted(conversation, taskID)
	}
}

func (m *MultiAdapter) TaskTerminal(conversation, taskID string, state lib.TaskState, source TerminalSource, reason string) {
	if observer, ok := m.observerFor(conversation); ok {
		observer.TaskTerminal(conversation, taskID, state, source, reason)
	}
}

func (m *MultiAdapter) TaskAccepted(conversation, taskID string) {
	if observer, ok := m.observerFor(conversation); ok {
		observer.TaskAccepted(conversation, taskID)
	}
}

func (m *MultiAdapter) CancelPublished(conversation, taskID string) {
	if observer, ok := m.observerFor(conversation); ok {
		observer.CancelPublished(conversation, taskID)
	}
}

func (m *MultiAdapter) SetHome(admits HomeAdmits, board BoardReader) {
	for _, a := range m.byPrefix {
		if sink, ok := a.(HomeSink); ok {
			sink.SetHome(admits, board)
		}
	}
}

func (m *MultiAdapter) SetBoardActor(actor BoardActor) {
	for _, a := range m.byPrefix {
		if sink, ok := a.(BoardActSink); ok {
			sink.SetBoardActor(actor)
		}
	}
}

func (m *MultiAdapter) SetBoardAdmits(admits BoardAdmits) {
	for _, a := range m.byPrefix {
		if sink, ok := a.(BoardAdmitSink); ok {
			sink.SetBoardAdmits(admits)
		}
	}
}

func (m *MultiAdapter) SetSessionLookup(lookup SessionLookup, idleTTL time.Duration) {
	for _, a := range m.byPrefix {
		if sink, ok := a.(SessionLookupSink); ok {
			sink.SetSessionLookup(lookup, idleTTL)
		}
	}
}

// SetSenderAdmission hands the gateway's admission gate to every backend
// that reads it (SenderAdmissionSink), as SetSessionLookup does.
func (m *MultiAdapter) SetSenderAdmission(admits SenderAdmission) {
	for _, a := range m.byPrefix {
		if sink, ok := a.(SenderAdmissionSink); ok {
			sink.SetSenderAdmission(admits)
		}
	}
}

// SetFeedbackRecorder hands the answer-feedback recorder to every backend
// that draws rating controls (FeedbackSink), as SetSessionLookup does.
func (m *MultiAdapter) SetFeedbackRecorder(r FeedbackRecorder) {
	for _, a := range m.byPrefix {
		if sink, ok := a.(FeedbackSink); ok {
			sink.SetFeedbackRecorder(r)
		}
	}
}
