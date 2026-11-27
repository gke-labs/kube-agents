package controller

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nuid"
	"sigs.k8s.io/controller-runtime/pkg/client"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The AgentSchedule reconciler's bus side (#5617): it submits a run to the
// gateway's trigger ingress, and asks the gateway what state a run is in, both
// as request/reply on the operator's existing bus connection
// (natsCardPublisher). The operator never reads TASKS.
//
// The wire is lib.TriggerSubmit, lib.TriggerStatus and their replies, spelled
// here because this module can't import a2a's. What keeps the two from
// drifting is the fixture: TestTheTriggerSubmitFixtureIsWhatTheOperatorSends
// writes the bytes this file sends into a2a/lib/testdata, and the a2a side
// decodes them strictly into lib's types and validates them.

const (
	// triggerScheduleSource is the trigger source the callout binds to the
	// operator's ServiceAccount (lib.TriggerSourceSchedule).
	triggerScheduleSource = "schedule"
	// triggerSubmitSubject and triggerStatusSubject are where the operator
	// submits and asks (lib.TriggerSubmitSubject, lib.TriggerStatusSubject).
	triggerSubmitSubject = "a2a.trigger." + triggerScheduleSource + ".submit"
	triggerStatusSubject = "a2a.trigger." + triggerScheduleSource + ".status"
	// triggerStopSubject is where the operator stops a run it started
	// (lib.TriggerStopSubject, #6052).
	triggerStopSubject = "a2a.trigger." + triggerScheduleSource + ".stop"
	// triggerReplyPrefix opens the operator's reply subjects
	// (lib.TriggerReplySubject): never its _INBOX.
	triggerReplyPrefix = "a2a.trigger.reply." + triggerScheduleSource + "."
	// triggerRequestTimeout bounds one request: the gateway answers after a
	// KV read, a mint and a publish.
	triggerRequestTimeout = 15 * time.Second

	// The gateway's refusal reasons the reconciler treats apart
	// (lib.TriggerRefused*).
	triggerRefusedRateLimited = "rate_limited"
	triggerRefusedUnavailable = "unavailable"

	// The delivery modes (lib.ScheduleDelivery*), and the A2A states a run's
	// end is read from (lib.State*).
	scheduleDeliveryChat = "chat"
	a2aStateCompleted    = "completed"
	a2aStateCanceled     = "canceled"

	// scriptRunTaskTextFormat is a script run's task text: the gateway
	// requires one on every submission (lib.ValidateTriggerSubmit), and a
	// script run's schedule has no prompt. No model reads it; it is what
	// the run's task shows as its ask, worded as a record, not an
	// instruction, should anything ever hand it to a model.
	scriptRunTaskTextFormat = "Scheduled script run: %s (no model turn)."
)

// triggerSubmitWire is lib.TriggerSubmit.
type triggerSubmitWire struct {
	Trigger               triggerClaimWire     `json:"trigger"`
	IdempotencyKey        string               `json:"idempotencyKey"`
	Schedule              scheduleRefWire      `json:"schedule"`
	Profile               string               `json:"profile"`
	Prompt                string               `json:"prompt"`
	Skills                []string             `json:"skills,omitempty"`
	Parameters            map[string]string    `json:"parameters,omitempty"`
	ActiveDeadlineSeconds int64                `json:"activeDeadlineSeconds,omitempty"`
	MaxTurns              int32                `json:"maxTurns,omitempty"`
	Delivery              scheduleDeliveryWire `json:"delivery"`
}

// triggerClaimWire is lib.TriggerClaim.
type triggerClaimWire struct {
	Name   string `json:"name"`
	Origin string `json:"origin"`
	Stream string `json:"stream,omitempty"`
	Script string `json:"script,omitempty"`
}

// scheduleRefWire is lib.ScheduleRef.
type scheduleRefWire struct {
	Namespace string `json:"namespace"`
	Name      string `json:"name"`
}

// scheduleDeliveryWire is lib.ScheduleDelivery.
type scheduleDeliveryWire struct {
	Mode            string `json:"mode"`
	SilentWhenClean bool   `json:"silentWhenClean,omitempty"`
	JobKey          string `json:"jobKey"`
}

// triggerRefusalWire is lib.TriggerRefusal.
type triggerRefusalWire struct {
	Reason string `json:"reason"`
	Detail string `json:"detail,omitempty"`
}

// triggerSubmitReplyWire is lib.TriggerSubmitReply.
type triggerSubmitReplyWire struct {
	TaskID  string              `json:"taskId,omitempty"`
	Refused *triggerRefusalWire `json:"refused,omitempty"`
}

// triggerStatusWire is lib.TriggerStatus.
type triggerStatusWire struct {
	Schedule scheduleRefWire `json:"schedule"`
	TaskID   string          `json:"taskId"`
}

// triggerStopReplyWire is lib.TriggerStopReply. The stop itself is
// triggerStatusWire, as lib.TriggerStop is lib.TriggerStatus.
type triggerStopReplyWire struct {
	Result  string              `json:"result,omitempty"`
	Refused *triggerRefusalWire `json:"refused,omitempty"`
}

// The stop's outcomes (lib.TriggerStop*).
const (
	triggerStopStopped      = "stopped"
	triggerStopAlreadyFinal = "already_final"
	triggerStopUnknownRun   = "unknown_run"
)

// triggerStatusReplyWire is lib.TriggerStatusReply.
type triggerStatusReplyWire struct {
	Found    bool                `json:"found"`
	State    string              `json:"state,omitempty"`
	Terminal bool                `json:"terminal,omitempty"`
	EndedAt  string              `json:"endedAt,omitempty"`
	Refused  *triggerRefusalWire `json:"refused,omitempty"`
	Delivery string              `json:"delivery,omitempty"`
	Reason   string              `json:"reason,omitempty"`
}

// errTriggerStopRefused is a stop the gateway refused for good: the stop
// itself is wrong (a malformed one), so asking again won't help.
var errTriggerStopRefused = errors.New("the gateway refused the stop")

// errTriggerRefused is a refusal that isn't worth retrying at the next slot:
// the submission itself is wrong (InvalidSchedule).
var errTriggerRefused = errors.New("the gateway refused the schedule's run")

// natsScheduleSubmitter is the reconciler's ScheduleSubmitter and
// ScheduleTaskTracker over the bus.
type natsScheduleSubmitter struct {
	bus    *natsCardPublisher
	reader client.Reader
	// now is the clock a run's end is stamped with when the gateway gives
	// none; a seam for tests.
	now func() time.Time
}

func newNATSScheduleSubmitter(bus *natsCardPublisher, reader client.Reader) *natsScheduleSubmitter {
	return &natsScheduleSubmitter{bus: bus, reader: reader, now: time.Now}
}

// agentFor is the one PlatformAgent in namespace, whose bus the schedule's
// runs go to.
func (s *natsScheduleSubmitter) agentFor(ctx context.Context, namespace string) (*agentv1alpha1.PlatformAgent, error) {
	var agents agentv1alpha1.PlatformAgentList
	if err := s.reader.List(ctx, &agents, client.InNamespace(namespace)); err != nil {
		return nil, fmt.Errorf("%w: listing PlatformAgents: %v", ErrScheduleGatewayUnavailable, err)
	}
	if len(agents.Items) != 1 {
		return nil, fmt.Errorf("%w: %d PlatformAgents in namespace %s, want 1", ErrScheduleGatewayUnavailable, len(agents.Items), namespace)
	}
	return &agents.Items[0], nil
}

// request sends body on subject with a reply subject in the operator's reply
// namespace, and decodes the answer into out.
func (s *natsScheduleSubmitter) request(ctx context.Context, namespace, subject string, body, out any) error {
	agent, err := s.agentFor(ctx, namespace)
	if err != nil {
		return err
	}
	nc, err := s.bus.conn(agent)
	if err != nil {
		return fmt.Errorf("%w: %v", ErrScheduleGatewayUnavailable, err)
	}
	data, err := json.Marshal(body)
	if err != nil {
		return err
	}
	reply := triggerReplyPrefix + nuid.Next()
	sub, err := nc.SubscribeSync(reply)
	if err != nil {
		return fmt.Errorf("%w: subscribing for the reply: %v", ErrScheduleGatewayUnavailable, err)
	}
	defer func() { _ = sub.Unsubscribe() }()
	if err := nc.PublishMsg(&nats.Msg{Subject: subject, Reply: reply, Data: data}); err != nil {
		return fmt.Errorf("%w: %v", ErrScheduleGatewayUnavailable, err)
	}
	wait, cancel := context.WithTimeout(ctx, triggerRequestTimeout)
	defer cancel()
	msg, err := sub.NextMsgWithContext(wait)
	if err != nil {
		return fmt.Errorf("%w: no answer on %s: %v", ErrScheduleGatewayUnavailable, subject, err)
	}
	if err := json.Unmarshal(msg.Data, out); err != nil {
		return fmt.Errorf("%w: the answer doesn't decode: %v", ErrScheduleGatewayUnavailable, err)
	}
	return nil
}

// Submit sends the schedule's run to the gateway and returns its task id.
func (s *natsScheduleSubmitter) Submit(ctx context.Context, sub ScheduleSubmission) (string, error) {
	var reply triggerSubmitReplyWire
	if err := s.request(ctx, sub.Schedule.Namespace, triggerSubmitSubject, triggerSubmitFor(sub), &reply); err != nil {
		return "", err
	}
	if r := reply.Refused; r != nil {
		switch r.Reason {
		case triggerRefusedRateLimited:
			return "", fmt.Errorf("%w: %s", ErrScheduleRateLimited, r.Detail)
		case triggerRefusedUnavailable:
			return "", fmt.Errorf("%w: %s", ErrScheduleGatewayUnavailable, r.Detail)
		}
		return "", fmt.Errorf("%w (%s): %s", errTriggerRefused, r.Reason, r.Detail)
	}
	if reply.TaskID == "" {
		return "", fmt.Errorf("%w: the answer carries no task id", ErrScheduleGatewayUnavailable)
	}
	return reply.TaskID, nil
}

// State asks the gateway what state the schedule's run is in.
// Stop asks the gateway to end one of the schedule's runs, and returns its
// outcome (triggerStop*). A gateway that doesn't answer, an older one without
// the stop, say, is ErrScheduleGatewayUnavailable.
func (s *natsScheduleSubmitter) Stop(ctx context.Context, sched *agentv1alpha1.AgentSchedule, taskID string) (string, error) {
	var reply triggerStopReplyWire
	q := triggerStatusWire{Schedule: scheduleRefWire{Namespace: sched.Namespace, Name: sched.Name}, TaskID: taskID}
	if err := s.request(ctx, sched.Namespace, triggerStopSubject, q, &reply); err != nil {
		return "", err
	}
	if r := reply.Refused; r != nil {
		if r.Reason == triggerRefusedUnavailable {
			return "", fmt.Errorf("%w: stop refused (%s): %s", ErrScheduleGatewayUnavailable, r.Reason, r.Detail)
		}
		return "", fmt.Errorf("%w (%s): %s", errTriggerStopRefused, r.Reason, r.Detail)
	}
	switch reply.Result {
	case triggerStopStopped, triggerStopAlreadyFinal, triggerStopUnknownRun:
		return reply.Result, nil
	}
	return "", fmt.Errorf("%w: the stop's answer %q is not one this operator knows", ErrScheduleGatewayUnavailable, reply.Result)
}

func (s *natsScheduleSubmitter) State(ctx context.Context, sched *agentv1alpha1.AgentSchedule, taskID string) (ScheduleTaskState, error) {
	var reply triggerStatusReplyWire
	q := triggerStatusWire{Schedule: scheduleRefWire{Namespace: sched.Namespace, Name: sched.Name}, TaskID: taskID}
	if err := s.request(ctx, sched.Namespace, triggerStatusSubject, q, &reply); err != nil {
		return ScheduleTaskState{}, err
	}
	if r := reply.Refused; r != nil {
		return ScheduleTaskState{}, fmt.Errorf("%w: status refused (%s): %s", ErrScheduleGatewayUnavailable, r.Reason, r.Detail)
	}
	state := ScheduleTaskState{Found: reply.Found, Terminal: reply.Terminal, Delivery: reply.Delivery}
	if reply.Terminal {
		state.Outcome = scheduleOutcomeFor(reply.State)
		state.Reason = reply.Reason
		state.CompletedAt = s.now()
		if t, err := time.Parse(time.RFC3339Nano, reply.EndedAt); err == nil {
			state.CompletedAt = t
		}
	}
	return state, nil
}

// triggerSubmitFor builds the run's submission from the schedule.
func triggerSubmitFor(sub ScheduleSubmission) triggerSubmitWire {
	sched := sub.Schedule
	w := triggerSubmitWire{
		Trigger:        triggerClaimWire{Name: sub.TriggerName, Origin: sub.Origin},
		IdempotencyKey: sub.IdempotencyKey,
		Schedule:       scheduleRefWire{Namespace: sched.Namespace, Name: sched.Name},
		Profile:        sched.Spec.ProfileRef.Name,
		Prompt:         sched.Spec.Prompt,
		Skills:         sched.Spec.Skills,
		Parameters:     sched.Spec.Parameters,
		Delivery:       scheduleDeliveryFor(sched),
	}
	// A built-in schedule binds its own report stream, the trigger's name
	// after schedule/ (lib.ValidateTrigger).
	if stream, ok := strings.CutPrefix(sub.TriggerName, triggerScheduleSource+"/"); ok && sub.Origin == agentv1alpha1.AgentScheduleOriginBuiltin {
		w.Trigger.Stream = stream
	}
	// A built-in schedule's script rides on its trigger, which the gateway
	// mints into the run's grant and authority (lib.TriggerClaim.Script).
	// accept() keeps a script off any other schedule; a submission for one
	// that got here anyway carries no script and no prompt, and the gateway
	// refuses it.
	if script := sched.Spec.Script; script != "" && sub.Origin == agentv1alpha1.AgentScheduleOriginBuiltin {
		w.Trigger.Script = script
		w.Prompt = fmt.Sprintf(scriptRunTaskTextFormat, script)
	}
	if d := sched.Spec.ActiveDeadlineSeconds; d != nil {
		w.ActiveDeadlineSeconds = *d
	}
	if t := sched.Spec.MaxTurns; t != nil {
		w.MaxTurns = *t
	}
	// A script run has no model turn, so skills and a turn cap mean nothing
	// to it: they stay off the wire even if a schedule stored without the
	// CRD's rule set them (accept() refuses that schedule too).
	if w.Trigger.Script != "" {
		w.Skills, w.MaxTurns = nil, 0
	}
	return w
}

// scheduleDeliveryFor is the run's delivery: the schedule's, with its mode
// defaulted to chat and its job key to the schedule's name folded into a
// DNS-1123 label (a name may hold dots; a job key keys KV entries).
func scheduleDeliveryFor(sched *agentv1alpha1.AgentSchedule) scheduleDeliveryWire {
	d := scheduleDeliveryWire{Mode: sched.Spec.Delivery.Mode, SilentWhenClean: sched.Spec.Delivery.SilentWhenClean, JobKey: sched.Spec.Delivery.JobKey}
	if d.Mode == "" {
		d.Mode = scheduleDeliveryChat
	}
	if d.JobKey == "" {
		// The name is a DNS-1123 label: accept() holds a schedule that
		// runs to one, since its trigger is schedule/<name>.
		d.JobKey = sched.Name
	}
	return d
}

// scheduleOutcomeFor is a run's outcome from its terminal A2A state.
func scheduleOutcomeFor(state string) string {
	switch state {
	case a2aStateCompleted:
		return agentv1alpha1.AgentScheduleOutcomeSucceeded
	case a2aStateCanceled:
		return agentv1alpha1.AgentScheduleOutcomeCancelled
	default:
		return agentv1alpha1.AgentScheduleOutcomeFailed
	}
}

// forget closes the bus connection held for namespace's PlatformAgent.
func (s *natsScheduleSubmitter) forget(namespace string) { s.bus.forget(namespace) }
