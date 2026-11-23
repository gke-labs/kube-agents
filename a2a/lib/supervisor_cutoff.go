package lib

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"strings"
	"sync"
	"time"

	"github.com/nats-io/nats.go/jetstream"
)

// The profile supervisor gate (gke-labs/kube-agents#5301). Once an install's
// profile supervisor form has latched to the six-token form
// (ProfileSupervisorSubject), the dispatcher is a profile task's one
// supervisor, and a five-token supervisor message (TaskSupervisorSubject) on
// a profile task stored after the install's cutoff is not that task's
// terminal. The gate holds against the gateway: the gateway's grant still
// reaches the five-token form, so nothing the gateway can write decides it.
// Two inputs decide it, and the operator renders both:
//
//   - the cutoff, EnvProfileSupervisorCutoff, on the reader's Deployment;
//   - the install's profile names, a file the operator renders from the
//     AgentProfiles it binds (EnvProfileNamesFile). An addressee is a profile
//     exactly when its name is in that file, the same names nats.conf denies
//     the gateway the supervisor subjects of.
//
// The time a message was stored is the JetStream server's: a consumer
// delivery's metadata, or on a direct get, the one Nats-Time-Stamp the server
// adds (storedTimeOf). A time that can't be read that way is unknown, and an
// unknown time is refused where it would decide.

// EnvProfileSupervisorCutoff names the time after which a reader stops taking
// a five-token supervisor message as a profile task's terminal: RFC3339,
// rendered by the operator on the gateway and the MCP server once the
// install's profile supervisor form has latched. Absent or empty, the install
// is unlatched and a reader takes both forms for every task, as it did before
// the cutoff existed.
const EnvProfileSupervisorCutoff = "A2A_PROFILE_SUPERVISOR_CUTOFF"

// EnvProfileNamesFile names the file holding the install's profile names
// (ParseProfileNames), rendered by the operator beside the cutoff. Read only
// while a cutoff is set.
const EnvProfileNamesFile = "A2A_PROFILE_NAMES_FILE"

// Where a reader's cutoff came from, as its health signal reports it.
const (
	// CutoffSourceUnset is no cutoff: an unlatched install.
	CutoffSourceUnset = "unset"
	// CutoffSourceEnv is the operator's value, as rendered.
	CutoffSourceEnv = "env"
	// CutoffSourceMalformed is a value that does not parse, read as latched
	// at the process's start (ProfileSupervisorCutoffFromEnv).
	CutoffSourceMalformed = "malformed"
)

// ProfileSupervisorCutoffSetting is the cutoff a reader runs with and where it
// came from (CutoffSource*), for its health signal.
type ProfileSupervisorCutoffSetting struct {
	Cutoff time.Time
	Source string
}

// ProfileSupervisorCutoffFromEnv reads EnvProfileSupervisorCutoff through
// getenv. Absent or empty is unset: unlatched. A value that does not parse as
// RFC3339 fails closed: the operator renders the variable only once the
// install has latched, so a value that is there at all says the install is
// latched, and the reader takes start, its own start time, as the cutoff. A
// five-token terminal stored before the process started is still read, one
// stored after is not. It is logged at Error, and Source says so for the
// reader's health signal.
func ProfileSupervisorCutoffFromEnv(getenv func(string) string, start time.Time, log *slog.Logger) ProfileSupervisorCutoffSetting {
	if log == nil {
		log = slog.Default()
	}
	raw := strings.TrimSpace(getenv(EnvProfileSupervisorCutoff))
	if raw == "" {
		return ProfileSupervisorCutoffSetting{Source: CutoffSourceUnset}
	}
	cutoff, err := time.Parse(time.RFC3339, raw)
	if err != nil {
		log.Error("profile supervisor cutoff does not parse as RFC3339; reading the install as latched at this process's start",
			"env", EnvProfileSupervisorCutoff, "value", raw, "cutoff", start, "err", err)
		return ProfileSupervisorCutoffSetting{Cutoff: start, Source: CutoffSourceMalformed}
	}
	log.Info("profile supervisor cutoff set: a five-token supervisor terminal on a profile task stored after it is not read",
		"env", EnvProfileSupervisorCutoff, "cutoff", cutoff)
	return ProfileSupervisorCutoffSetting{Cutoff: cutoff, Source: CutoffSourceEnv}
}

// WithProfileSupervisorCutoff sets the time after which this client's task
// reads stop taking a five-token supervisor terminal as a profile task's
// (AcceptSupervisorEvent). The zero time, the default, is unlatched: both
// forms count for every task.
func WithProfileSupervisorCutoff(cutoff time.Time) ClientOption {
	return func(o *clientOptions) { o.profileSupervisorCutoff = cutoff }
}

// WithProfileNames sets the install's profile names this client's reads take
// profile-ness from (ProfileNames). With a cutoff and no names, or names never
// loaded, every addressee reads as a profile: the gate fails closed.
func WithProfileNames(names *ProfileNames) ClientOption {
	return func(o *clientOptions) { o.profileNames = names }
}

// ProfileSupervisorCutoff is the cutoff this client was given, zero when
// unlatched.
func (c *Client) ProfileSupervisorCutoff() time.Time { return c.opts.profileSupervisorCutoff }

// ProfileNames is the install's set of profile names as the operator renders
// it (EnvProfileNamesFile). The zero value holds nothing and is not loaded.
// Safe for concurrent use.
type ProfileNames struct {
	mu     sync.RWMutex
	names  map[string]bool
	loaded bool
	raw    []byte
}

// NewProfileNames is a loaded set holding names.
func NewProfileNames(names ...string) *ProfileNames {
	p := &ProfileNames{}
	p.set(names, nil)
	return p
}

// profileNamesFile is the file's shape.
type profileNamesFile struct {
	Profiles []string `json:"profiles"`
}

// ParseProfileNames reads the operator's profile-names file:
// {"profiles": ["<name>", ...]}. Each name is one subject token, as an
// addressee is; a file naming anything else, or not this shape, is refused
// whole rather than read in part.
func ParseProfileNames(raw []byte) ([]string, error) {
	var f profileNamesFile
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&f); err != nil {
		return nil, fmt.Errorf("profile names: %w", err)
	}
	if f.Profiles == nil {
		return nil, errors.New(`profile names: no "profiles" list`)
	}
	for _, n := range f.Profiles {
		if !validDNS1123Label(n) {
			return nil, fmt.Errorf("profile names: %q is not a DNS-1123 label", n)
		}
	}
	return f.Profiles, nil
}

func (p *ProfileNames) set(names []string, raw []byte) {
	m := make(map[string]bool, len(names))
	for _, n := range names {
		m[n] = true
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	p.names, p.loaded, p.raw = m, true, raw
}

// Has reports whether name is a profile's. A nil set, or one never loaded,
// answers true for every name: an install with a cutoff and no readable
// profile names reads every task as a profile task, so a five-token message
// after the cutoff is refused on all of them, the gateway's sessions' too,
// rather than accepted on a profile's.
func (p *ProfileNames) Has(name string) bool {
	if p == nil {
		return true
	}
	p.mu.RLock()
	defer p.mu.RUnlock()
	return !p.loaded || p.names[name]
}

// Loaded reports whether a file has been read into the set. False is the
// fail-closed state Has describes.
func (p *ProfileNames) Loaded() bool {
	if p == nil {
		return false
	}
	p.mu.RLock()
	defer p.mu.RUnlock()
	return p.loaded
}

// Len is how many names the set holds; zero when not loaded.
func (p *ProfileNames) Len() int {
	if p == nil {
		return 0
	}
	p.mu.RLock()
	defer p.mu.RUnlock()
	return len(p.names)
}

// load reads path into the set when its bytes changed. A file that can't be
// read or parsed changes nothing: a set loaded once keeps its names, and one
// never loaded stays fail-closed.
func (p *ProfileNames) load(path string) (changed bool, err error) {
	raw, err := os.ReadFile(path) // #nosec G304 -- the operator's own mount, named by EnvProfileNamesFile
	if err != nil {
		return false, err
	}
	p.mu.RLock()
	same := p.loaded && bytes.Equal(raw, p.raw)
	p.mu.RUnlock()
	if same {
		return false, nil
	}
	names, err := ParseProfileNames(raw)
	if err != nil {
		return false, err
	}
	p.set(names, raw)
	return true, nil
}

// profileNamesPollInterval is how often the file is re-read: the kubelet
// itself takes up to a minute to update a ConfigMap mount.
const profileNamesPollInterval = 30 * time.Second

// StartProfileNames loads the file EnvProfileNamesFile names, synchronously so
// the first read sees it, then re-reads it on a goroutine until ctx ends. It
// returns nil when the variable is unset (an unlatched install, or a cutoff
// with no names: fail closed, logged at Error by the caller's gate).
func StartProfileNames(ctx context.Context, getenv func(string) string, log *slog.Logger) *ProfileNames {
	path := strings.TrimSpace(getenv(EnvProfileNamesFile))
	if path == "" {
		return nil
	}
	if log == nil {
		log = slog.Default()
	}
	p := &ProfileNames{}
	p.reload(path, log)
	go func() {
		t := time.NewTicker(profileNamesPollInterval)
		defer t.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-t.C:
				p.reload(path, log)
			}
		}
	}()
	return p
}

func (p *ProfileNames) reload(path string, log *slog.Logger) {
	changed, err := p.load(path)
	switch {
	case err != nil && !p.Loaded():
		log.Error("profile names unreadable; every addressee reads as a profile for the supervisor cutoff",
			"env", EnvProfileNamesFile, "path", path, "err", err)
	case err != nil:
		log.Error("profile names unreadable; keeping the names last read", "env", EnvProfileNamesFile, "path", path, "err", err)
	case changed:
		log.Info("profile names loaded", "env", EnvProfileNamesFile, "path", path, "profiles", p.Len())
	}
}

// ProfileSupervisorGate is a reader's gate configuration from its env: the
// cutoff and the profile names, both rendered by the operator. Names is nil
// when the cutoff is unset.
type ProfileSupervisorGate struct {
	ProfileSupervisorCutoffSetting
	Names *ProfileNames
}

// ProfileSupervisorGateFromEnv reads the cutoff and, when one is set, starts
// the profile names (StartProfileNames). A cutoff with no names file is
// logged at Error: every addressee then reads as a profile.
func ProfileSupervisorGateFromEnv(ctx context.Context, getenv func(string) string, start time.Time, log *slog.Logger) ProfileSupervisorGate {
	if log == nil {
		log = slog.Default()
	}
	g := ProfileSupervisorGate{ProfileSupervisorCutoffSetting: ProfileSupervisorCutoffFromEnv(getenv, start, log)}
	if g.Cutoff.IsZero() {
		return g
	}
	g.Names = StartProfileNames(ctx, getenv, log)
	if g.Names == nil {
		log.Error("profile supervisor cutoff set with no profile names file; every addressee reads as a profile",
			"env", EnvProfileNamesFile)
	}
	return g
}

// ClientOptions are the gate's options for lib.Connect.
func (g ProfileSupervisorGate) ClientOptions() []ClientOption {
	return []ClientOption{WithProfileSupervisorCutoff(g.Cutoff), WithProfileNames(g.Names)}
}

// IsFiveTokenSupervisorSubject reports whether subject is a supervisor
// subject in the five-token form, a2a.tasks.<addressee>.<task>.supervisor
// (TaskSupervisorSubject), whoever it names.
func IsFiveTokenSupervisorSubject(subject string) bool {
	_, ok := fiveTokenSupervisorAddressee(subject)
	return ok
}

// fiveTokenSupervisorAddressee is the addressee a five-token supervisor
// subject names.
func fiveTokenSupervisorAddressee(subject string) (string, bool) {
	rest, ok := strings.CutPrefix(subject, "a2a.tasks.")
	if !ok {
		return "", false
	}
	parts := strings.Split(rest, ".")
	if len(parts) != 3 || parts[0] == "" || parts[1] == "" || parts[2] != TaskClassSupervisor {
		return "", false
	}
	return parts[0], true
}

// AcceptsSupervisorEvent reports whether a reader takes the message stored at
// storedAt on subject as part of a task's history, given the install's cutoff
// and whether the subject's addressee is a profile. Only one case is refused:
// cutoff set, a profile's addressee, the five-token supervisor form, stored
// at or after the cutoff, or at a time the server's metadata didn't give
// (zero). That message is not the dispatcher's (it writes the six-token form
// from the latch on, and the cutoff trails the latch by a margin), so it is
// not the task's terminal. Everything else is accepted: an unlatched install
// (zero cutoff), an addressee that is no profile (the gateway is its
// supervisor, on the five-token form), the six-token form and every other
// class, and a five-token terminal stored before the cutoff, which the
// dispatcher wrote before it switched and which history and heals still
// need.
func AcceptsSupervisorEvent(cutoff time.Time, subject string, storedAt time.Time, profile bool) bool {
	if cutoff.IsZero() || !profile || !IsFiveTokenSupervisorSubject(subject) {
		return true
	}
	return !storedAt.IsZero() && storedAt.Before(cutoff)
}

// IsProfileAddressee reports whether addressee is a profile's under this
// client's profile names (ProfileNames.Has): with none, or none loaded, every
// addressee is.
func (c *Client) IsProfileAddressee(addressee string) bool {
	return c.opts.profileNames.Has(addressee)
}

// AcceptSupervisorEvent is AcceptsSupervisorEvent under this client's cutoff
// and profile names, the addressee read off the subject, counting each
// refusal (ProfileSupervisorDrops). Nothing a caller holds about the task
// enters it: the gate is the operator's two inputs and the server's stored
// time.
func (c *Client) AcceptSupervisorEvent(subject string, storedAt time.Time) bool {
	cutoff := c.opts.profileSupervisorCutoff
	if cutoff.IsZero() {
		return true
	}
	addressee, ok := fiveTokenSupervisorAddressee(subject)
	if !ok || AcceptsSupervisorEvent(cutoff, subject, storedAt, c.IsProfileAddressee(addressee)) {
		return true
	}
	c.profileSupervisorDrops.Add(1)
	return false
}

// ProfileSupervisorDrops counts the five-token supervisor messages this
// client's reads have refused as a profile task's terminal: each read that
// met one counts it, so a task read three times counts three.
func (c *Client) ProfileSupervisorDrops() int64 { return c.profileSupervisorDrops.Load() }

// ReplayHead is the newest message on one of a task's replay subjects, as a
// direct get returns it.
type ReplayHead struct {
	Subject  string
	Data     []byte
	StoredAt time.Time
}

// storedTimeOf is when the server stored msg, from what the server itself
// said, or zero when that can't be told. A stream message read without
// direct get carries the time in the API's JSON reply, which only the server
// writes. A direct get carries it as a Nats-Time-Stamp header the server
// appends to the stored message's own headers; a publisher can put a header
// of that name on its message first, and nats.go reads the first. So on a
// direct get the time is taken only when the header has exactly one value,
// the server's; a message that carried its own has two, and its time is
// unknown. direct is whether the read was a direct get: the stream's
// allow_direct, which is what the client checks to choose
// (jetstream.Stream.CachedInfo).
func storedTimeOf(msg *jetstream.RawStreamMsg, direct bool) time.Time {
	if !direct {
		return msg.Time
	}
	stamps := msg.Header.Values(jetstream.TimeStampHeaer)
	if len(stamps) != 1 {
		return time.Time{}
	}
	at, err := time.Parse(time.RFC3339Nano, stamps[0])
	if err != nil {
		return time.Time{}
	}
	return at
}

// directGets reports whether stream answers GetLastMsgForSubject with a direct
// get, as the client decides it.
func directGets(stream jetstream.Stream) bool {
	info := stream.CachedInfo()
	return info != nil && info.Config.AllowDirect
}

// tasksStream is stream, or the TASKS handle when stream is nil.
func (c *Client) tasksStream(ctx context.Context, stream jetstream.Stream) (jetstream.Stream, error) {
	if stream != nil {
		return stream, nil
	}
	_, js := c.conn()
	s, err := js.Stream(ctx, TasksStream)
	if err != nil {
		return nil, fmt.Errorf("stream %s: %w", TasksStream, err)
	}
	return s, nil
}

// TaskReplayHeads reads the newest message on each of the task's replay
// subjects (TaskReplaySubjects) with a direct get, opening no consumer, and
// returns those the read takes (AcceptSupervisorEvent, on the server's stored
// time: storedTimeOf), in subject order. No heads is the task having nothing
// in the retention window, the answer the replay gives as TaskNotFound.
// stream is the TASKS handle, or nil for the call to fetch it. An error is
// the read failing, not the subjects being empty.
//
// It sees only the newest message per subject. A profile task's five-token
// subject whose newest message is refused can hold an older one stored
// before the cutoff, which the replay folds; the direct get reads that
// subject as empty. Only a second five-token terminal for the same profile
// task does that (one written by a rolled-back dispatcher's fallback after a
// pre-latch one), and the reading it gives, not final, is the conservative
// one for the callers that use it (a busy count, a no-first-event notice).
func (c *Client) TaskReplayHeads(ctx context.Context, stream jetstream.Stream, addressee, taskID string) ([]ReplayHead, error) {
	return c.taskReplayHeads(ctx, stream, addressee, taskID, false)
}

// TaskReplayEmpty reports whether TaskReplayHeads would find no heads, and
// stops at the first head it takes. The events subject is read first, so a
// task with a first event costs one direct get, as before the cutoff; a task
// with none reads all three subjects, and with a cutoff, so does one whose
// only messages are refused.
func (c *Client) TaskReplayEmpty(ctx context.Context, stream jetstream.Stream, addressee, taskID string) (bool, error) {
	heads, err := c.taskReplayHeads(ctx, stream, addressee, taskID, true)
	return len(heads) == 0, err
}

func (c *Client) taskReplayHeads(ctx context.Context, stream jetstream.Stream, addressee, taskID string, first bool) ([]ReplayHead, error) {
	stream, err := c.tasksStream(ctx, stream)
	if err != nil {
		return nil, err
	}
	direct := directGets(stream)
	var heads []ReplayHead
	for _, subject := range TaskReplaySubjects(addressee, taskID) {
		msg, err := stream.GetLastMsgForSubject(ctx, subject)
		if errors.Is(err, jetstream.ErrMsgNotFound) {
			continue
		}
		if err != nil {
			return nil, fmt.Errorf("newest message on %s: %w", subject, err)
		}
		storedAt := storedTimeOf(msg, direct)
		if !c.AcceptSupervisorEvent(subject, storedAt) {
			continue
		}
		heads = append(heads, ReplayHead{Subject: subject, Data: msg.Data, StoredAt: storedAt})
		if first {
			break
		}
	}
	return heads, nil
}
