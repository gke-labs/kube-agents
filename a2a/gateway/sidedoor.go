package gateway

import (
	"context"
	"log/slog"
	"strings"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The side door beside a real backend: one gateway, two ingresses.
//
// The one-backend guard refuses two real backends because two processes on
// one Chat relay durable split its event deliveries, and the symptom is a
// gateway that answers half the messages. The inject door has no such failure
// mode -- it consumes nothing and competes for nothing -- and stage 2 of the
// eval transport needs the door and the Chat relay on one install so the two
// can be compared against it (the A2A owner's reading, 2026-09-17).
//
// So this composite, rather than a second Deployment: Run delivers from both
// into the same handleInbound, and everything the gateway sends back is
// routed by the conversation's own key. Backend-qualified keys are what make
// that safe -- a synthetic conversation is spelled `inject:...` and a real
// one is not, so no post can reach the wrong ingress.

// sideDoorAdapter pairs a real backend with the inject door. It is an Adapter
// like either half, so the gateway drives it without knowing there are two.
type sideDoorAdapter struct {
	primary Adapter
	door    *InjectAdapter
	log     *slog.Logger
}

// WithSideDoor puts the inject door beside a real backend, or hands back the
// door alone when there is no real backend to pair it with -- an eval install
// with neither a Discord token nor a Chat relay, which is the case that makes
// the door #1660's answer.
func WithSideDoor(primary Adapter, door *InjectAdapter, log *slog.Logger) Adapter {
	if primary == nil {
		return door
	}
	if log == nil {
		log = slog.Default()
	}
	return &sideDoorAdapter{primary: primary, door: door, log: log}
}

// forDoor reports whether a conversation belongs to the door. The prefix is
// the whole test, and it is why the door's keys are qualified: see
// injectConversationKey, which refuses a key that could spell itself as
// another backend's.
func forDoor(conversation string) bool {
	return strings.HasPrefix(conversation, injectKeyPrefix)
}

// Run delivers from both ingresses until ctx is done, or until either stops
// on its own.
//
// Either one returning ends the gateway. That is deliberate rather than
// tolerant: a gateway that kept running with its Chat backend dead would look
// healthy while consuming nothing, which is the failure the one-backend guard
// exists to prevent, and a gateway that kept running with a dead door would
// hang every eval on a listener nothing answers. Exiting lets the Deployment
// restart both.
func (s *sideDoorAdapter) Run(ctx context.Context, handler func(InboundMessage)) error {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()

	errs := make(chan error, 2)
	go func() {
		err := s.primary.Run(ctx, handler)
		if err != nil {
			s.log.Error("the chat backend stopped; the gateway is going with it", "err", err)
		}
		errs <- err
	}()
	go func() {
		err := s.door.Run(ctx, handler)
		if err != nil {
			s.log.Error("the inject door stopped; the gateway is going with it", "err", err)
		}
		errs <- err
	}()

	select {
	case err := <-errs:
		// Cancel through the deferred cancel above, so the other half stops
		// too rather than being left serving into a gateway that has exited.
		return err
	case <-ctx.Done():
		return nil
	}
}

// Post, Edit and Roster route on the conversation's key.
func (s *sideDoorAdapter) Post(conversation, text string) (string, error) {
	if forDoor(conversation) {
		return s.door.Post(conversation, text)
	}
	return s.primary.Post(conversation, text)
}

func (s *sideDoorAdapter) Edit(conversation, messageID, text string) error {
	if forDoor(conversation) {
		return s.door.Edit(conversation, messageID, text)
	}
	return s.primary.Edit(conversation, messageID, text)
}

func (s *sideDoorAdapter) Roster(conversation string) ([]string, bool, error) {
	if forDoor(conversation) {
		return s.door.Roster(conversation)
	}
	return s.primary.Roster(conversation)
}

// OpenDirect goes to the real backend, because a user id is not qualified the
// way a conversation is and the DM switch is a chat affordance: the caller
// that will one day use it is a classifier deciding to move a reply out of a
// room, which only a room has. The door's own OpenDirect answers when the
// door is the gateway's only ingress and there is no room to move out of.
func (s *sideDoorAdapter) OpenDirect(userID string) (string, error) {
	return s.primary.OpenDirect(userID)
}

// TaskStarted and TaskTerminal reach the door alone, and only for its own
// conversations. The composite implements TaskObserver unconditionally so
// that the gateway's type assertion finds it whatever the primary is; a chat
// backend is told nothing either way, which is what it would have been told
// if it were the only adapter.
func (s *sideDoorAdapter) TaskStarted(conversation, taskID string) {
	if forDoor(conversation) {
		s.door.TaskStarted(conversation, taskID)
	}
}

func (s *sideDoorAdapter) TaskTerminal(conversation, taskID string, state lib.TaskState, source TerminalSource, reason string) {
	if forDoor(conversation) {
		s.door.TaskTerminal(conversation, taskID, state, source, reason)
	}
}

func (s *sideDoorAdapter) TaskAccepted(conversation, taskID string) {
	if forDoor(conversation) {
		s.door.TaskAccepted(conversation, taskID)
	}
}

func (s *sideDoorAdapter) CancelPublished(conversation, taskID string) {
	if forDoor(conversation) {
		s.door.CancelPublished(conversation, taskID)
	}
}

// MessageDropped and TurnFinished reach the door alone, for the same reason:
// a chat user reads their own conversation, and the door's caller is the one
// that would otherwise have to guess what became of its message.
func (s *sideDoorAdapter) MessageDropped(conversation, authorID string) {
	if forDoor(conversation) {
		s.door.MessageDropped(conversation, authorID)
	}
}

func (s *sideDoorAdapter) TurnFinished(conversation string) {
	if forDoor(conversation) {
		s.door.TurnFinished(conversation)
	}
}

// SetProbe hands the gateway's probe to the door, which is the only half
// whose caller is a program (ProbeSink). The composite implements it
// unconditionally for the same reason it implements TaskObserver: the
// gateway's type assertion has to find it whatever the primary is.
func (s *sideDoorAdapter) SetProbe(probe ConversationProbe) {
	s.door.SetProbe(probe)
}
