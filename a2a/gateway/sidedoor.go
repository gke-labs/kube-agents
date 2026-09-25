package gateway

import (
	"context"
	"fmt"
	"log/slog"
	"strings"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// sideDoorDrainBound is how long Run waits for the other halves to stop once
// the first has: a door drains in-flight requests for injectShutdownGrace,
// and the chat half's own shutdown needs a moment beyond that. Bounded so a
// half that ignores its context cannot keep a gateway that has decided to
// exit alive.
const sideDoorDrainBound = 3 * injectShutdownGrace

// The side doors beside a real backend: one gateway, several ingresses.
//
// The one-backend guard refuses two real backends because two processes on
// one Chat relay durable split its event deliveries, and the symptom is a
// gateway that answers half the messages. A door has no such failure mode --
// it consumes nothing and competes for nothing -- and stage 2 of the eval
// transport needs the inject door and the Chat relay on one install so the
// two can be compared against it (the A2A owner's reading, 2026-09-17). The
// A2A door sits beside them for the same reason: an external agent and a
// chat user reach the same gateway.
//
// So this composite, rather than a second Deployment: Run delivers from every
// half into the same handleInbound, and everything the gateway sends back is
// routed by the conversation's own key. Backend-qualified keys are what make
// that safe -- a door's conversation is spelled `inject:...` or `a2a:...` and
// a real one is not, so no post can reach the wrong ingress.

// SideDoor is what a door beside the chat backends must be: an Adapter whose
// caller is a program, so it implements all three observer contracts. The
// composite forwards them by conversation prefix, and it can only forward
// what every door receives.
type SideDoor interface {
	Adapter
	TaskObserver
	InboundObserver
	ProbeSink
}

// DoorSpec names one door and the conversation-key prefix it owns.
type DoorSpec struct {
	Name   string
	Prefix string
	Door   SideDoor
}

// sideDoorAdapter pairs a real backend (or the mux in front of several) with
// the doors. It is an Adapter like any half, so the gateway drives it without
// knowing there are several.
type sideDoorAdapter struct {
	primary Adapter // nil when the doors are the whole of the ingress
	doors   []DoorSpec
	log     *slog.Logger
}

// WithSideDoor puts the inject door beside a real backend, or hands back the
// door alone when there is no real backend to pair it with -- an eval install
// with neither a Discord token nor a Chat relay, which is the case that makes
// the door #1660's answer. Kept for the callers that have one door; the
// general form is WithSideDoors.
func WithSideDoor(primary Adapter, door *InjectAdapter, log *slog.Logger) Adapter {
	return WithSideDoors(primary, []DoorSpec{{Name: injectBackend, Prefix: injectKeyPrefix, Door: door}}, log)
}

// WithSideDoors puts every door beside the primary. With no primary and one
// door the door itself is returned, as before; with no primary and several
// doors the composite runs the doors alone and refuses a conversation none
// of them owns.
func WithSideDoors(primary Adapter, doors []DoorSpec, log *slog.Logger) Adapter {
	if primary == nil && len(doors) == 1 {
		return doors[0].Door
	}
	if log == nil {
		log = slog.Default()
	}
	return &sideDoorAdapter{primary: primary, doors: doors, log: log}
}

// doorFor reports which door a conversation belongs to, or nil. The prefix
// is the whole test, and it is why every door's keys are qualified: see
// injectConversationKey and a2aConversationKey.
func (s *sideDoorAdapter) doorFor(conversation string) SideDoor {
	for _, d := range s.doors {
		if strings.HasPrefix(conversation, d.Prefix) {
			return d.Door
		}
	}
	return nil
}

// forDoor reports whether a conversation belongs to the inject door. Kept
// for the tests that ask the question of the single-door composite.
func forDoor(conversation string) bool {
	return strings.HasPrefix(conversation, injectKeyPrefix)
}

// Run delivers from every ingress until ctx is done, or until any stops on
// its own.
//
// Any one returning ends the gateway. That is deliberate rather than
// tolerant: a gateway that kept running with its Chat backend dead would look
// healthy while consuming nothing, which is the failure the one-backend guard
// exists to prevent, and a gateway that kept running with a dead door would
// hang every caller on a listener nothing answers. Exiting lets the
// Deployment restart all of them.
func (s *sideDoorAdapter) Run(ctx context.Context, handler func(InboundMessage)) error {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()

	type half struct {
		name    string
		adapter Adapter
	}
	var halves []half
	if s.primary != nil {
		halves = append(halves, half{"the chat backend", s.primary})
	}
	for _, d := range s.doors {
		halves = append(halves, half{"the " + d.Name + " door", d.Door})
	}

	errs := make(chan error, len(halves))
	for _, h := range halves {
		go func(h half) {
			err := h.adapter.Run(ctx, handler)
			// A half that stopped because it was asked to -- the parent's
			// cancel, or the cancel below after another half failed -- is
			// a clean shutdown, not an error to report.
			if err != nil && ctx.Err() == nil {
				s.log.Error(h.name+" stopped; the gateway is going with it", "err", err)
			}
			errs <- err
		}(h)
	}

	stopped := 0
	var first error
	select {
	case first = <-errs:
		stopped++
	case <-ctx.Done():
	}
	// Stop the others -- all, on a parent cancel -- and wait for them, so
	// the drain each does on its way out (a door's in-flight requests, the
	// chat backend's own shutdown) finishes before the gateway exits on it.
	// Returning on the first stop alone would cut that short: the process
	// exits with Run.
	cancel()
	drain := time.NewTimer(sideDoorDrainBound)
	defer drain.Stop()
	for stopped < len(halves) {
		select {
		case <-errs:
			stopped++
		case <-drain.C:
			s.log.Warn("a half of the gateway did not stop inside the drain bound; exiting without it",
				"bound", sideDoorDrainBound)
			return first
		}
	}
	return first
}

// Post, Edit and Roster route on the conversation's key.
func (s *sideDoorAdapter) Post(conversation, text string) (string, error) {
	if door := s.doorFor(conversation); door != nil {
		return door.Post(conversation, text)
	}
	if s.primary == nil {
		return "", fmt.Errorf("no ingress owns conversation %q", conversation)
	}
	return s.primary.Post(conversation, text)
}

func (s *sideDoorAdapter) Edit(conversation, messageID, text string) error {
	if door := s.doorFor(conversation); door != nil {
		return door.Edit(conversation, messageID, text)
	}
	if s.primary == nil {
		return fmt.Errorf("no ingress owns conversation %q", conversation)
	}
	return s.primary.Edit(conversation, messageID, text)
}

func (s *sideDoorAdapter) Roster(conversation string) ([]string, bool, error) {
	if door := s.doorFor(conversation); door != nil {
		return door.Roster(conversation)
	}
	if s.primary == nil {
		return nil, false, fmt.Errorf("no ingress owns conversation %q", conversation)
	}
	return s.primary.Roster(conversation)
}

// OpenDirect goes to the real backend, because a user id is not qualified the
// way a conversation is and the DM switch is a chat affordance: the caller
// that will one day use it is a classifier deciding to move a reply out of a
// room, which only a room has. With no real backend the first door answers,
// as the inject door alone did when it was the gateway's only ingress.
func (s *sideDoorAdapter) OpenDirect(userID string) (string, error) {
	if s.primary != nil {
		return s.primary.OpenDirect(userID)
	}
	return s.doors[0].Door.OpenDirect(userID)
}

// TaskStarted and TaskTerminal reach the owning door alone. The composite
// implements TaskObserver unconditionally so that the gateway's type
// assertion finds it whatever the primary is; a chat backend is told nothing
// either way, which is what it would have been told if it were the only
// adapter.
func (s *sideDoorAdapter) TaskStarted(conversation, taskID string) {
	if door := s.doorFor(conversation); door != nil {
		door.TaskStarted(conversation, taskID)
	}
}

func (s *sideDoorAdapter) TaskTerminal(conversation, taskID string, state lib.TaskState, source TerminalSource, reason string) {
	if door := s.doorFor(conversation); door != nil {
		door.TaskTerminal(conversation, taskID, state, source, reason)
	}
}

func (s *sideDoorAdapter) TaskAccepted(conversation, taskID string) {
	if door := s.doorFor(conversation); door != nil {
		door.TaskAccepted(conversation, taskID)
	}
}

func (s *sideDoorAdapter) CancelPublished(conversation, taskID string) {
	if door := s.doorFor(conversation); door != nil {
		door.CancelPublished(conversation, taskID)
	}
}

// MessageDropped and TurnFinished reach the owning door alone, for the same
// reason: a chat user reads their own conversation, and a door's caller is
// the one that would otherwise have to guess what became of its message.
func (s *sideDoorAdapter) MessageDropped(conversation, authorID string) {
	if door := s.doorFor(conversation); door != nil {
		door.MessageDropped(conversation, authorID)
	}
}

func (s *sideDoorAdapter) TurnFinished(conversation string) {
	if door := s.doorFor(conversation); door != nil {
		door.TurnFinished(conversation)
	}
}

// SetProbe hands the gateway's probe to every door, which are the halves
// whose callers are programs (ProbeSink). The composite implements it
// unconditionally for the same reason it implements TaskObserver: the
// gateway's type assertion has to find it whatever the primary is.
func (s *sideDoorAdapter) SetProbe(probe ConversationProbe) {
	for _, d := range s.doors {
		d.Door.SetProbe(probe)
	}
}
