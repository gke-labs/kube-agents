package lib

import (
	"fmt"
	"strings"
	"unicode/utf8"
)

// A trigger's submission (#4685, Track 2): an unattended source asks the
// gateway for one task on a profile, over request/reply on
// TriggerSubmitSubject(source). The callout binds the subject's source to
// the caller's identity, so the source is the gateway's to trust, not the
// request's to claim. The gateway checks the request (ValidateTriggerSubmit),
// mints the task's grant with the trigger bound (capability.Entry.Trigger),
// writes the trigger into the envelope's authority block, and answers with
// the task's id or a refusal (TriggerSubmitReply).

// TriggerSubmitSubjectPrefix and TriggerSubmitSubjectSuffix frame
// a2a.trigger.<source>.submit.
const (
	TriggerSubmitSubjectPrefix = "a2a.trigger."
	TriggerSubmitSubjectSuffix = ".submit"
)

// TriggerReplySubjectPrefix opens the subjects the gateway answers a
// submission on: a2a.trigger.reply.<source>.<id>. A namespace of its own,
// not the caller's _INBOX, for the reason the verifier answers on
// a2a.cap.reply.>: a grant to publish into callers' inboxes would reach
// where they read their JetStream replies.
const TriggerReplySubjectPrefix = "a2a.trigger.reply."

// TriggerReplySubject is where source's submission id is answered.
func TriggerReplySubject(source, id string) string {
	return TriggerReplySubjectPrefix + source + "." + id
}

// ValidTriggerReplySubject reports whether reply is
// a2a.trigger.reply.<source>.<id>, with id one subject token.
func ValidTriggerReplySubject(source, reply string) bool {
	id, ok := strings.CutPrefix(reply, TriggerReplySubjectPrefix+source+".")
	return ok && id != "" && !strings.ContainsAny(id, ".*> \t\r\n")
}

// TriggerSourceSchedule is the source whose triggers are AgentSchedules,
// published by the operator's reconciler.
const TriggerSourceSchedule = triggerScheduleSource

// TriggerRunNowKeyPrefix opens a schedule's run-now idempotency key
// (run-now/<ns>/<name>/<token>). No event source may take it as its name,
// or its keys could spell a schedule's.
const TriggerRunNowKeyPrefix = "run-now"

// TriggerSubmitSubject is where source submits.
func TriggerSubmitSubject(source string) string {
	return TriggerSubmitSubjectPrefix + source + TriggerSubmitSubjectSuffix
}

// ParseTriggerSubmitSubject returns the source of a2a.trigger.<source>.submit.
func ParseTriggerSubmitSubject(subject string) (string, error) {
	rest, ok := strings.CutPrefix(subject, TriggerSubmitSubjectPrefix)
	if !ok {
		return "", fmt.Errorf("subject %q is not %s<source>%s", subject, TriggerSubmitSubjectPrefix, TriggerSubmitSubjectSuffix)
	}
	source, ok := strings.CutSuffix(rest, TriggerSubmitSubjectSuffix)
	if !ok || !ValidSubjectToken(source) {
		return "", fmt.Errorf("subject %q is not %s<source>%s", subject, TriggerSubmitSubjectPrefix, TriggerSubmitSubjectSuffix)
	}
	return source, nil
}

// ScheduleDelivery is where an unattended run's result goes (the design's
// section 4): the AgentSchedule's spec.delivery, carried on the submission
// and recorded on the task's session record for the relay (Track 3).
type ScheduleDelivery struct {
	// Mode is chat (the install's home channel) or local (kept, not posted).
	Mode string `json:"mode"`
	// SilentWhenClean posts nothing for a run that found nothing.
	SilentWhenClean bool `json:"silentWhenClean,omitempty"`
	// JobKey groups a schedule's posts into one thread per UTC day; it is
	// part of KV keys and subjects, so a DNS-1123 label.
	JobKey string `json:"jobKey"`
}

// The two delivery modes.
const (
	ScheduleDeliveryChat  = "chat"
	ScheduleDeliveryLocal = "local"
)

// ValidateScheduleDelivery is the check the gateway makes at ingress and a
// reader of the recorded copy may repeat: a mode that is exactly chat or
// local, never empty, and a job key that is a DNS-1123 label.
func ValidateScheduleDelivery(d ScheduleDelivery) error {
	if d.Mode != ScheduleDeliveryChat && d.Mode != ScheduleDeliveryLocal {
		return fmt.Errorf("delivery mode %q is not %q or %q", d.Mode, ScheduleDeliveryChat, ScheduleDeliveryLocal)
	}
	if !validDNS1123Label(d.JobKey) {
		return fmt.Errorf("delivery job key %q is not a DNS-1123 label", d.JobKey)
	}
	return nil
}

// ScheduleRef names the AgentSchedule a submission is for, for the record
// and the audit log: the gateway does not read it back from Kubernetes.
type ScheduleRef struct {
	Namespace string `json:"namespace"`
	Name      string `json:"name"`
}

// TriggerSubmit is a trigger's request for one task.
type TriggerSubmit struct {
	// Trigger is the trigger the task's grant binds. A schedule's: name
	// schedule/<stream>, origin builtin, stream the schedule's report
	// stream, and script the profile script a script run executes, absent on
	// a prompt run. An event source's (#6572): name <source>/<kind>, origin
	// builtin, no stream or script.
	Trigger TriggerClaim `json:"trigger"`
	// IdempotencyKey names the run: schedule/<ns>/<name>/<slot-unix> or
	// run-now/<ns>/<name>/<token> for a schedule, <source>/<kind>/<token>
	// for an event source. A repeat answers with the same task.
	IdempotencyKey string `json:"idempotencyKey"`
	// Schedule is the AgentSchedule the run is for; empty for an event
	// source.
	Schedule ScheduleRef `json:"schedule"`
	// Profile is the AgentProfile that runs the task, at the unattended tier.
	Profile string `json:"profile"`
	// Prompt is the task's text.
	Prompt string `json:"prompt"`
	// Skills are skill names the run is asked to load.
	Skills []string `json:"skills,omitempty"`
	// Parameters are the task's parameters, as any submission's.
	Parameters map[string]string `json:"parameters,omitempty"`
	// ActiveDeadlineSeconds bounds the run; zero takes the profile's.
	ActiveDeadlineSeconds int64 `json:"activeDeadlineSeconds,omitempty"`
	// MaxTurns bounds the run's turns; zero takes the profile's.
	MaxTurns int32 `json:"maxTurns,omitempty"`
	// Delivery is where the result goes.
	Delivery ScheduleDelivery `json:"delivery"`
}

// TriggerClaim is the trigger as a submission names it: the fields
// capability.Trigger binds (lib can't import capability).
type TriggerClaim struct {
	Name   string `json:"name"`
	Origin string `json:"origin"`
	Stream string `json:"stream,omitempty"`
	// Script names the profile script a built-in schedule's run executes in
	// place of a model turn (#5622): one of the profile's spec.scripts, by
	// name, never a path. Empty for a prompt run. The gateway mints it into
	// the task's grant and authority, and the dispatcher resolves it against
	// the profile's allowlist at admission.
	Script string `json:"script,omitempty"`
}

// Bounds on a submission, the AgentSchedule CRD's own: what the reconciler
// can send is what the gateway accepts.
const (
	TriggerSubmitPromptMaxLen     = 16384
	TriggerSubmitSkillsMax        = 16
	TriggerSubmitDeadlineMin      = 60
	TriggerSubmitDeadlineMax      = 86400
	TriggerSubmitMaxTurnsMax      = 1000
	triggerSubmitIdempotencyMaxLn = 512
)

// ValidateTriggerSubmit reports the first way s is malformed for source, or
// nil. It checks the shape and the trigger's binding to the source: for the
// schedule source, to its schedule (validateScheduleSubmit); for any other
// source, an event source, to an event kind (validateEventSubmit). Whether
// the gateway admits the source at all, and whether the profile exists and
// admits the unattended tier, is the gateway's to answer.
func ValidateTriggerSubmit(source string, s TriggerSubmit) error {
	if !ValidSubjectToken(source) {
		return fmt.Errorf("trigger source %q is not a dot-free DNS-1123 label", source)
	}
	if err := ValidateTrigger(s.Trigger.Name, s.Trigger.Origin, s.Trigger.Stream, s.Trigger.Script); err != nil {
		return err
	}
	bind := validateEventSubmit
	if source == TriggerSourceSchedule {
		bind = validateScheduleSubmit
	}
	if err := bind(source, s); err != nil {
		return err
	}
	if !ValidSubjectToken(s.Profile) {
		return fmt.Errorf("profile %q is not a DNS-1123 label", s.Profile)
	}
	// Characters, as the CRD's maxLength counts them, not bytes: a prompt
	// of non-ASCII text the API server admitted would otherwise be refused
	// at every slot.
	if strings.TrimSpace(s.Prompt) == "" || utf8.RuneCountInString(s.Prompt) > TriggerSubmitPromptMaxLen {
		return fmt.Errorf("prompt is empty or longer than %d characters", TriggerSubmitPromptMaxLen)
	}
	if len(s.Skills) > TriggerSubmitSkillsMax {
		return fmt.Errorf("%d skills, at most %d", len(s.Skills), TriggerSubmitSkillsMax)
	}
	for _, sk := range s.Skills {
		if !validDNS1123Label(sk) {
			return fmt.Errorf("skill %q is not a DNS-1123 label", sk)
		}
	}
	if err := ValidateTaskParameters(s.Parameters); err != nil {
		return err
	}
	if d := s.ActiveDeadlineSeconds; d != 0 && (d < TriggerSubmitDeadlineMin || d > TriggerSubmitDeadlineMax) {
		return fmt.Errorf("active deadline %ds is outside %d-%d", d, TriggerSubmitDeadlineMin, TriggerSubmitDeadlineMax)
	}
	if t := s.MaxTurns; t < 0 || t > TriggerSubmitMaxTurnsMax {
		return fmt.Errorf("max turns %d is outside 0-%d", t, TriggerSubmitMaxTurnsMax)
	}
	return ValidateScheduleDelivery(s.Delivery)
}

// validateScheduleSubmit checks a schedule source's binding: a trigger named
// schedule/<name> whose stream is its own name, the schedule it names, and a
// key for a run of that schedule.
func validateScheduleSubmit(source string, s TriggerSubmit) error {
	src, name, _ := strings.Cut(s.Trigger.Name, "/")
	if src != source || name == "" {
		return fmt.Errorf("trigger name %q is not %s/<schedule> for source %q", s.Trigger.Name, source, source)
	}
	if s.Trigger.Stream != name {
		return fmt.Errorf("trigger stream %q is not the schedule's own name %q", s.Trigger.Stream, name)
	}
	if !validDNS1123Label(s.Schedule.Namespace) || s.Schedule.Name == "" || len(s.Schedule.Name) > 253 {
		return fmt.Errorf("schedule %s/%s is not a namespace and a name", s.Schedule.Namespace, s.Schedule.Name)
	}
	if s.Schedule.Name != name {
		return fmt.Errorf("trigger %q does not name schedule %q", s.Trigger.Name, s.Schedule.Name)
	}
	return validateIdempotencyKey(s.IdempotencyKey, s.Schedule)
}

// validateEventSubmit checks an event source's binding (#6572): a trigger
// named <source>/<kind>, the kind of event it reports; no schedule, and so
// no stream or script, which only a schedule's trigger binds
// (ValidateTrigger); and a key <source>/<kind>/<token>, the token the
// source's own dedupe already names the event by, one segment.
func validateEventSubmit(source string, s TriggerSubmit) error {
	src, kind, _ := strings.Cut(s.Trigger.Name, "/")
	if src != source || kind == "" {
		return fmt.Errorf("trigger name %q is not %s/<kind> for source %q", s.Trigger.Name, source, source)
	}
	if s.Schedule != (ScheduleRef{}) {
		return fmt.Errorf("an event source's submission names no schedule")
	}
	prefix := s.Trigger.Name + "/"
	token, ok := strings.CutPrefix(s.IdempotencyKey, prefix)
	if !ok || token == "" {
		return fmt.Errorf("idempotency key %q is not %s<token>", s.IdempotencyKey, prefix)
	}
	// The token is one segment of letters, digits, '.', '_' and '-': the
	// key names one run's own record (the gateway's trigger:<key>), so it
	// can't reach into another key's shape.
	for i := 0; i < len(token); i++ {
		if c := token[i]; !(c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9' || c == '.' || c == '_' || c == '-') {
			return fmt.Errorf("idempotency key %q has a token that is not letters, digits, '.', '_' and '-'", s.IdempotencyKey)
		}
	}
	return validateIdempotencyToken(s.IdempotencyKey)
}

// validateIdempotencyKey checks key names a run of sched:
// schedule/<ns>/<name>/<slot-unix> or run-now/<ns>/<name>/<token>.
func validateIdempotencyKey(key string, sched ScheduleRef) error {
	if len(key) > triggerSubmitIdempotencyMaxLn {
		return fmt.Errorf("idempotency key longer than %d bytes", triggerSubmitIdempotencyMaxLn)
	}
	parts := strings.Split(key, "/")
	if len(parts) != 4 || (parts[0] != TriggerSourceSchedule && parts[0] != TriggerRunNowKeyPrefix) {
		return fmt.Errorf("idempotency key %q is not schedule/<ns>/<name>/<slot> or run-now/<ns>/<name>/<token>", key)
	}
	if parts[1] != sched.Namespace || parts[2] != sched.Name {
		return fmt.Errorf("idempotency key %q is not for schedule %s/%s", key, sched.Namespace, sched.Name)
	}
	if parts[3] == "" {
		return fmt.Errorf("idempotency key %q has no run token", key)
	}
	return validateIdempotencyToken(key)
}

// validateIdempotencyToken checks key's length and that it holds no space or
// control character.
func validateIdempotencyToken(key string) error {
	if len(key) > triggerSubmitIdempotencyMaxLn {
		return fmt.Errorf("idempotency key longer than %d bytes", triggerSubmitIdempotencyMaxLn)
	}
	for i := 0; i < len(key); i++ {
		if c := key[i]; c <= ' ' || c == 0x7f {
			return fmt.Errorf("idempotency key %q holds a space or control character", key)
		}
	}
	return nil
}

// TriggerSubmitReply is the gateway's answer: the task's id, or a refusal.
type TriggerSubmitReply struct {
	TaskID  string          `json:"taskId,omitempty"`
	Refused *TriggerRefusal `json:"refused,omitempty"`
}

// TriggerRefusal says why no task was started.
type TriggerRefusal struct {
	Reason string `json:"reason"`
	Detail string `json:"detail,omitempty"`
}

// The refusal reasons. rate_limited and unavailable are worth retrying at a
// later slot; the rest say the submission itself is wrong.
const (
	TriggerRefusedRateLimited    = "rate_limited"
	TriggerRefusedInvalid        = "invalid"
	TriggerRefusedProfileUnknown = "profile_unknown"
	TriggerRefusedTierNotAllowed = "tier_not_allowed"
	TriggerRefusedUnavailable    = "unavailable"
)

// A trigger's status query (#5617): what state is one task the gateway
// started for this source in? The operator's reconciler asks on
// TriggerStatusSubject(source), with a reply subject in the source's reply
// namespace (TriggerReplySubject), and the gateway answers from its fold of
// TASKS, for a task it recorded on that source's trigger records and no
// other. The source never reads TASKS itself.

// TriggerStatusSubjectSuffix ends a2a.trigger.<source>.status.
const TriggerStatusSubjectSuffix = ".status"

// TriggerStatusSubject is where source asks after a task.
func TriggerStatusSubject(source string) string {
	return TriggerSubmitSubjectPrefix + source + TriggerStatusSubjectSuffix
}

// ParseTriggerStatusSubject returns the source of a2a.trigger.<source>.status.
func ParseTriggerStatusSubject(subject string) (string, error) {
	rest, ok := strings.CutPrefix(subject, TriggerSubmitSubjectPrefix)
	if !ok {
		return "", fmt.Errorf("subject %q is not %s<source>%s", subject, TriggerSubmitSubjectPrefix, TriggerStatusSubjectSuffix)
	}
	source, ok := strings.CutSuffix(rest, TriggerStatusSubjectSuffix)
	if !ok || !ValidSubjectToken(source) {
		return "", fmt.Errorf("subject %q is not %s<source>%s", subject, TriggerSubmitSubjectPrefix, TriggerStatusSubjectSuffix)
	}
	return source, nil
}

// TriggerStatus asks after one task of one schedule.
type TriggerStatus struct {
	Schedule ScheduleRef `json:"schedule"`
	TaskID   string      `json:"taskId"`
}

// ValidateTriggerStatus reports the first way s is malformed for source.
func ValidateTriggerStatus(source string, s TriggerStatus) error {
	if source != TriggerSourceSchedule {
		return fmt.Errorf("trigger source %q is not %q", source, TriggerSourceSchedule)
	}
	if !validDNS1123Label(s.Schedule.Namespace) || s.Schedule.Name == "" || len(s.Schedule.Name) > 253 {
		return fmt.Errorf("schedule %s/%s is not a namespace and a name", s.Schedule.Namespace, s.Schedule.Name)
	}
	if !ValidSubjectToken(s.TaskID) {
		return fmt.Errorf("task id %q is not one subject token", s.TaskID)
	}
	return nil
}

// TriggerStatusReply is the gateway's answer: the task's state, or a
// refusal (the reasons of TriggerRefusal; unknown for a task the gateway
// never started for this schedule, or whose record is gone).
type TriggerStatusReply struct {
	// Found is false when no record of the task remains: the reconciler
	// reads that as Lost.
	Found bool `json:"found"`
	// State is the task's A2A state, and Terminal whether it has ended.
	State    TaskState `json:"state,omitempty"`
	Terminal bool      `json:"terminal,omitempty"`
	// EndedAt is when it ended, when Terminal.
	EndedAt string          `json:"endedAt,omitempty"`
	Refused *TriggerRefusal `json:"refused,omitempty"`
	// Delivery is how the run's report was delivered, from the gateway's
	// scheduled-reports store: no_delivery, local, silent, stored_no_home,
	// posting (still in flight), posted, post_failed or post_unknown (a post
	// started or timed out with no outcome, never retried). Omitted while there
	// is no record. The reconciler keeps asking while it reads posting.
	Delivery string `json:"delivery,omitempty"`
}
