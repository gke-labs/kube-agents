package lib

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"strings"
	"time"

	"github.com/nats-io/nats.go/jetstream"
)

// EnvProfileSupervisorCutoff names the time after which a reader stops taking
// the five-token supervisor subject (TaskSupervisorSubject) as a profile
// task's terminal (gke-labs/kube-agents#5301): RFC3339, rendered by the
// operator on the gateway and the MCP server once the install's profile
// supervisor form has latched to the six-token form
// (ProfileSupervisorSubject). Absent or empty, the install is unlatched and a
// reader takes both forms for every task, as it did before the cutoff
// existed.
const EnvProfileSupervisorCutoff = "A2A_PROFILE_SUPERVISOR_CUTOFF"

// ProfileSupervisorCutoffFromEnv reads EnvProfileSupervisorCutoff through
// getenv. Absent or empty is the zero time: unlatched. A value that does not
// parse as RFC3339 is logged at Error and read as unlatched too. The operator
// renders the value, so a malformed one is a bug or a hand edit, and the two
// ways to fail are not equal: refusing to start takes the gateway (or the MCP
// server) down for a reader-side filter that only narrows what is read, while
// unlatched is exactly what every reader did before the cutoff existed, and
// what an install whose latch has not set does now. The value is not
// guessed at either: a cutoff read wrong would drop terminals that are real.
func ProfileSupervisorCutoffFromEnv(getenv func(string) string, log *slog.Logger) time.Time {
	raw := strings.TrimSpace(getenv(EnvProfileSupervisorCutoff))
	if raw == "" {
		return time.Time{}
	}
	cutoff, err := time.Parse(time.RFC3339, raw)
	if err != nil {
		if log == nil {
			log = slog.Default()
		}
		log.Error("profile supervisor cutoff does not parse as RFC3339; reading both supervisor forms as an unlatched install does",
			"env", EnvProfileSupervisorCutoff, "value", raw, "err", err)
		return time.Time{}
	}
	if log == nil {
		log = slog.Default()
	}
	log.Info("profile supervisor cutoff set: a five-token supervisor terminal on a profile task stored after it is not read",
		"env", EnvProfileSupervisorCutoff, "cutoff", cutoff)
	return cutoff
}

// WithProfileSupervisorCutoff sets the time after which this client's task
// reads stop taking a five-token supervisor terminal as a profile task's
// (AcceptsSupervisorEvent). The zero time, the default, is unlatched: both
// forms count for every task.
func WithProfileSupervisorCutoff(cutoff time.Time) ClientOption {
	return func(o *clientOptions) { o.profileSupervisorCutoff = cutoff }
}

// ProfileSupervisorCutoff is the cutoff this client was given, zero when
// unlatched.
func (c *Client) ProfileSupervisorCutoff() time.Time { return c.opts.profileSupervisorCutoff }

// ReadOption qualifies one task read (TasksGet and its variants,
// TaskReplayHeads).
type ReadOption func(*readOptions)

type readOptions struct {
	profileTask bool
}

// ProfileTask marks the read as one of a task addressed to an AgentProfile,
// whose supervisor is the dispatcher. On a client with a cutoff, a five-token
// supervisor terminal stored after it is then not the task's terminal.
func ProfileTask() ReadOption { return ProfileTaskIf(true) }

// ProfileTaskIf is ProfileTask when profile is true, and a read of a session
// or platform task (the default) when it is false: for a caller that decides
// per task.
func ProfileTaskIf(profile bool) ReadOption {
	return func(o *readOptions) { o.profileTask = profile }
}

func readOptionsOf(opts []ReadOption) readOptions {
	var o readOptions
	for _, opt := range opts {
		opt(&o)
	}
	return o
}

// IsFiveTokenSupervisorSubject reports whether subject is a supervisor
// subject in the five-token form, a2a.tasks.<addressee>.<task>.supervisor
// (TaskSupervisorSubject), whoever it names.
func IsFiveTokenSupervisorSubject(subject string) bool {
	rest, ok := strings.CutPrefix(subject, "a2a.tasks.")
	if !ok {
		return false
	}
	parts := strings.Split(rest, ".")
	return len(parts) == 3 && parts[0] != "" && parts[1] != "" && parts[2] == TaskClassSupervisor
}

// AcceptsSupervisorEvent reports whether a reader takes the message stored at
// storedAt on subject as part of a task's history, given the install's cutoff
// and whether the task is a profile task. Only one case is refused: cutoff
// set, a profile task, the five-token supervisor form, stored at or after
// the cutoff. That message is not the dispatcher's (it writes the six-token
// form from the latch on, and the cutoff trails the latch by a margin), so it
// is not the task's terminal. Everything else is accepted: an unlatched
// install (zero cutoff), a session or platform task (the gateway is their
// supervisor, on the five-token form), the six-token form and every other
// class, and a five-token terminal stored before the cutoff, which the
// dispatcher wrote before it switched and which history and heals still
// need. storedAt is the JetStream server's timestamp, which a publisher
// cannot set; a zero one (no metadata) is accepted, as every read did before
// the cutoff.
func AcceptsSupervisorEvent(cutoff time.Time, subject string, storedAt time.Time, profileTask bool) bool {
	if cutoff.IsZero() || !profileTask || storedAt.IsZero() || !IsFiveTokenSupervisorSubject(subject) {
		return true
	}
	return storedAt.Before(cutoff)
}

// AcceptSupervisorEvent is AcceptsSupervisorEvent under this client's cutoff,
// counting each refusal (ProfileSupervisorDrops).
func (c *Client) AcceptSupervisorEvent(subject string, storedAt time.Time, profileTask bool) bool {
	if AcceptsSupervisorEvent(c.opts.profileSupervisorCutoff, subject, storedAt, profileTask) {
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

// TaskReplayHeads reads the newest message on each of the task's replay
// subjects (TaskReplaySubjects) with a direct get, opening no consumer, and
// returns those the read takes (AcceptSupervisorEvent), in subject order. No
// heads is the task having nothing in the retention window, the answer the
// replay gives as TaskNotFound. stream is the TASKS handle, or nil for the
// call to fetch it. An error is the read failing, not the subjects being
// empty.
//
// It sees only the newest message per subject. A profile task's five-token
// subject whose newest message is refused can hold an older one stored
// before the cutoff, which the replay folds; the direct get reads that
// subject as empty. Only a second five-token terminal for the same profile
// task does that (one written by a rolled-back dispatcher's fallback after a
// pre-latch one), and the reading it gives, not final, is the conservative
// one for the callers that use it (a busy count, a no-first-event notice).
func (c *Client) TaskReplayHeads(ctx context.Context, stream jetstream.Stream, addressee, taskID string, opts ...ReadOption) ([]ReplayHead, error) {
	ro := readOptionsOf(opts)
	if stream == nil {
		_, js := c.conn()
		s, err := js.Stream(ctx, TasksStream)
		if err != nil {
			return nil, fmt.Errorf("stream %s: %w", TasksStream, err)
		}
		stream = s
	}
	var heads []ReplayHead
	for _, subject := range TaskReplaySubjects(addressee, taskID) {
		msg, err := stream.GetLastMsgForSubject(ctx, subject)
		if errors.Is(err, jetstream.ErrMsgNotFound) {
			continue
		}
		if err != nil {
			return nil, fmt.Errorf("newest message on %s: %w", subject, err)
		}
		if !c.AcceptSupervisorEvent(subject, msg.Time, ro.profileTask) {
			continue
		}
		heads = append(heads, ReplayHead{Subject: subject, Data: msg.Data, StoredAt: msg.Time})
	}
	return heads, nil
}
