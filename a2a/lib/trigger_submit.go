package lib

import (
	"fmt"
	"regexp"
	"strings"
	"unicode"
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
	// part of KV keys and subjects, so a DNS-1123 label. A thread delivery
	// keeps it as the run's record key and opens no day thread.
	JobKey string `json:"jobKey"`
	// Platform, ChatID and ThreadID name, for mode thread alone, the
	// existing thread the result posts into: a thread the gateway itself
	// opened with a keyed home post (NotifyThreadsBucket). The gateway
	// refuses the mode at ingress unless it trusts the submitting source
	// for it, the chat is its home channel and the thread is on its record.
	Platform string `json:"platform,omitempty"`
	ChatID   string `json:"chatId,omitempty"`
	ThreadID string `json:"threadId,omitempty"`
	// IncidentSource, for mode thread alone, names the writer on the
	// incident row the posted result is kept under: a2a- and up to 28 of
	// a-z, 0-9 and - (ValidIncidentSource). Empty is
	// ScheduleDeliveryThreadSource.
	IncidentSource string `json:"incidentSource,omitempty"`
}

// The three delivery modes.
const (
	ScheduleDeliveryChat   = "chat"
	ScheduleDeliveryLocal  = "local"
	ScheduleDeliveryThread = "thread"
)

// ScheduleDeliveryThreadSource is a thread delivery's incident source when
// the submission names none.
const ScheduleDeliveryThreadSource = "a2a-trigger"

// scheduleDeliveryTargetMaxLen bounds a thread delivery's platform, chat
// and thread ids: a Google Chat thread name is the longest, well under it.
const scheduleDeliveryTargetMaxLen = 256

// incidentSourcePattern is the gateway's incident sources: session-kv's rule
// (1-32 of a-z, 0-9 and -) inside an a2a- prefix, so a source a trigger names
// can only ever be one of the gateway's own writers, and session-kv's
// replace (same source only) never reaches a row another writer stored.
var incidentSourcePattern = regexp.MustCompile(`^a2a-[a-z0-9-]{1,28}$`)

// IncidentSourceSchedule is the scheduled reports' incident source, the
// bridge's default (a2a/hermes-bridge/report_incident.go): reserved, so a
// thread delivery's rows never replace a scheduled report's.
const IncidentSourceSchedule = "a2a-schedule"

// ValidIncidentSource reports whether s is an incident source a thread
// delivery may write under: one of the gateway's, never the scheduled
// reports'.
func ValidIncidentSource(s string) bool {
	return s != IncidentSourceSchedule && incidentSourcePattern.MatchString(s)
}

// IncidentSourceOf is d's incident source: its own, or the default.
func (d ScheduleDelivery) IncidentSourceOf() string {
	if d.IncidentSource != "" {
		return d.IncidentSource
	}
	return ScheduleDeliveryThreadSource
}

// ValidateScheduleDelivery is the check the gateway makes at ingress and a
// reader of the recorded copy may repeat: a mode that is exactly chat,
// local or thread, never empty, and a job key that is a DNS-1123 label. A
// thread delivery names its platform, chat and thread, and the others name
// none of them. Whether the gateway takes a thread delivery from a given
// source, chat and thread is its own check, beyond this one.
func ValidateScheduleDelivery(d ScheduleDelivery) error {
	if d.Mode != ScheduleDeliveryChat && d.Mode != ScheduleDeliveryLocal && d.Mode != ScheduleDeliveryThread {
		return fmt.Errorf("delivery mode %q is not %q, %q or %q", d.Mode, ScheduleDeliveryChat, ScheduleDeliveryLocal, ScheduleDeliveryThread)
	}
	if !validDNS1123Label(d.JobKey) {
		return fmt.Errorf("delivery job key %q is not a DNS-1123 label", d.JobKey)
	}
	if d.Mode != ScheduleDeliveryThread {
		if d.Platform != "" || d.ChatID != "" || d.ThreadID != "" || d.IncidentSource != "" {
			return fmt.Errorf("delivery mode %q names a thread; only mode %q does", d.Mode, ScheduleDeliveryThread)
		}
		return nil
	}
	for _, f := range []struct{ name, v string }{{"platform", d.Platform}, {"chat", d.ChatID}, {"thread", d.ThreadID}} {
		if f.v == "" || len(f.v) > scheduleDeliveryTargetMaxLen || strings.TrimSpace(f.v) != f.v || strings.ContainsFunc(f.v, unicode.IsControl) {
			return fmt.Errorf("thread delivery %s %q is empty, longer than %d bytes, or not a plain id", f.name, f.v, scheduleDeliveryTargetMaxLen)
		}
	}
	if d.IncidentSource != "" && !ValidIncidentSource(d.IncidentSource) {
		return fmt.Errorf("thread delivery incident source %q is not a2a- and up to 28 of a-z, 0-9 and -", d.IncidentSource)
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
	// Trigger is the trigger the task's grant binds: name schedule/<stream>,
	// origin builtin, stream the schedule's report stream, and script the
	// profile script a script run executes, absent on a prompt run.
	Trigger TriggerClaim `json:"trigger"`
	// IdempotencyKey names the run: schedule/<ns>/<name>/<slot-unix> or
	// run-now/<ns>/<name>/<token>. A repeat answers with the same task.
	IdempotencyKey string `json:"idempotencyKey"`
	// Schedule is the AgentSchedule the run is for.
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
// nil. It checks the shape and the trigger's binding to the source and the
// schedule; whether the profile exists and admits the unattended tier is
// the gateway's to answer from its directory.
func ValidateTriggerSubmit(source string, s TriggerSubmit) error {
	if source != TriggerSourceSchedule {
		return fmt.Errorf("trigger source %q is not %q, the one source that submits", source, TriggerSourceSchedule)
	}
	if err := ValidateTrigger(s.Trigger.Name, s.Trigger.Origin, s.Trigger.Stream, s.Trigger.Script); err != nil {
		return err
	}
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
	if err := validateIdempotencyKey(s.IdempotencyKey, s.Schedule); err != nil {
		return err
	}
	if !ValidSubjectToken(s.Profile) {
		return fmt.Errorf("profile %q is not a DNS-1123 label", s.Profile)
	}
	if strings.TrimSpace(s.Prompt) == "" || len(s.Prompt) > TriggerSubmitPromptMaxLen {
		return fmt.Errorf("prompt is empty or longer than %d bytes", TriggerSubmitPromptMaxLen)
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

// validateIdempotencyKey checks key names a run of sched:
// schedule/<ns>/<name>/<slot-unix> or run-now/<ns>/<name>/<token>.
func validateIdempotencyKey(key string, sched ScheduleRef) error {
	if len(key) > triggerSubmitIdempotencyMaxLn {
		return fmt.Errorf("idempotency key longer than %d bytes", triggerSubmitIdempotencyMaxLn)
	}
	parts := strings.Split(key, "/")
	if len(parts) != 4 || (parts[0] != "schedule" && parts[0] != "run-now") {
		return fmt.Errorf("idempotency key %q is not schedule/<ns>/<name>/<slot> or run-now/<ns>/<name>/<token>", key)
	}
	if parts[1] != sched.Namespace || parts[2] != sched.Name {
		return fmt.Errorf("idempotency key %q is not for schedule %s/%s", key, sched.Namespace, sched.Name)
	}
	if parts[3] == "" {
		return fmt.Errorf("idempotency key %q has no run token", key)
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
