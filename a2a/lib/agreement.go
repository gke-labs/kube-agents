package lib

import (
	"encoding/json"
	"fmt"
	"strings"
)

// Envelope-subject agreement: the consumer-side half of subject-derived
// identity.
//
// An envelope's publisher is the principal the subject implies - the addressee
// token on `…events`, the render's supervisor on `…supervisor`, a requester on
// `…in`, the profile on `a2a.agents.{profile}`. That implication is
// decision-grade only where the envelope AGREES with the subject it arrived
// on: the kind is one the subject class admits (closed-world, so a
// `topic-update` on an events subject is a disagreement, not a curiosity), the
// taskId matches the subject token, and the writer the envelope names is the
// writer the subject implies. Disagreement is a protocol error and the message
// carries no identity. Nothing here is signed and nothing is looked up; every
// check is a field-to-token comparison, plus the one supervisor name a
// consumer is configured with.
//
// `from` therefore stops being "routing and display only" and becomes a
// refusal input - but never the source of identity. Identity comes from the
// subject; `from` is checked for agreement with it. A publisher can still
// write any `from` it likes on a subject it may write to, and what that buys
// it after this check is nothing: the consumer attributes the bytes to the
// subject's principal, and a `from` that disagrees gets the bytes dropped.

// AgreementPolicy configures the check for one consumer.
type AgreementPolicy struct {
	// Supervisor is the `from.session` the render assigns as supervisor for
	// the addressees this consumer reads - the gateway for chat sessions.
	// Empty means the consumer does not know its supervisor by name, and the
	// `…supervisor` writer check falls back to the negative form: anyone but
	// the addressee.
	Supervisor string

	// StrictEventsWriter makes the `…events` writer-class check a hard
	// disagreement. Off, the check is ADVISORY: reported so the caller can
	// count it, never a refusal. It ships off, because for one TASKS
	// retention window after the supervisor subject split the stream still
	// holds legitimate supervisor terminals on `…events`, written before
	// the split, and a hard check would refuse them - which is a
	// non-terminal fold of every recent task and a second supervisor
	// terminal from the gateway's heal path. Flip it no earlier than 72h
	// after the split reaches an install.
	StrictEventsWriter bool
}

// AgreementError is a disagreement between an envelope and the subject it was
// delivered on. Advisory marks the one check that is counted rather than
// refused for now (AgreementPolicy.StrictEventsWriter); every other
// disagreement is a protocol error.
type AgreementError struct {
	Subject  string
	Msg      string
	Advisory bool
}

func (e *AgreementError) Error() string {
	if e.Advisory {
		return fmt.Sprintf("a2a envelope disagrees with subject %s (advisory): %s", e.Subject, e.Msg)
	}
	return fmt.Sprintf("a2a envelope disagrees with subject %s: %s", e.Subject, e.Msg)
}

// IsAdvisoryDisagreement reports whether err is an agreement check the
// policy currently counts rather than refuses.
func IsAdvisoryDisagreement(err error) bool {
	e, ok := err.(*AgreementError)
	return ok && e.Advisory
}

// CheckSubjectAgreement runs the agreement check for env delivered on
// subject. It returns nil where the envelope agrees or where the subject is
// not identity-bearing in a way an envelope can disagree with (topics,
// heartbeats, anything outside the task plane and the directory), an
// *AgreementError otherwise. The stored subject is the one to pass on replay;
// it is the original.
func CheckSubjectAgreement(subject string, env *Envelope, p AgreementPolicy) error {
	if addressee, taskID, class, ok := ParseTaskSubject(subject); ok {
		return checkTaskAgreement(subject, addressee, taskID, class, env, p)
	}
	if profile, ok := strings.CutPrefix(subject, agentsPrefix); ok && ValidSubjectToken(profile) {
		return checkDirectoryAgreement(subject, profile, env)
	}
	return nil
}

func disagree(subject, format string, args ...any) *AgreementError {
	return &AgreementError{Subject: subject, Msg: fmt.Sprintf(format, args...)}
}

// namesAddressee reports whether the party names the addressee - by session
// for a chat session (the pod name is the addressee), by profile for a
// profile-addressed executor (the bridge publishes as session
// `platform-bridge`, profile `platform`). At least one must match; a party
// naming the addressee in neither field is not its executor.
func namesAddressee(p Party, addressee string) bool {
	return p.Session == addressee || p.Profile == addressee
}

func checkTaskAgreement(subject, addressee, taskID, class string, env *Envelope, p AgreementPolicy) error {
	if env.TaskID != taskID {
		return disagree(subject, "taskId %q does not match the subject's %q", env.TaskID, taskID)
	}
	// Class-independent, and that IS assertion 4's second clause - it says
	// "an envelope whose `to` disagrees with its subject's addressee token",
	// with no class qualifier. An earlier version of this comment said the
	// docs scoped the assertion to `…in` and that the library was
	// deliberately wider; that was a misreading, and the spec's own `…in`
	// row briefly grew a matching `to`-is-REQUIRED claim off the back of it.
	// Nothing anywhere requires `to` to be PRESENT, on any class, and this
	// function does not check presence either. What is scoped to `…in` is
	// only the convention that a requester tends to set it.
	// Events carry no `to` at all, so this is unreachable for them in
	// practice; that is the point.
	if env.To != nil && env.To.Session != addressee {
		return disagree(subject, "to %q disagrees with the subject's addressee %q", env.To.Session, addressee)
	}
	switch class {
	case TaskClassEvents:
		if env.Kind != KindStatusUpdate && env.Kind != KindArtifactUpdate {
			return disagree(subject, "kind %q is not an event kind", env.Kind)
		}
		if !namesAddressee(env.From, addressee) {
			e := disagree(subject, "from %s does not name the addressee %q", describeParty(env.From), addressee)
			e.Advisory = !p.StrictEventsWriter
			return e
		}
	case TaskClassSupervisor:
		if env.Kind != KindStatusUpdate {
			return disagree(subject, "kind %q on a supervisor subject; supervisors emit only terminal status-update", env.Kind)
		}
		var s StatusUpdate
		if err := json.Unmarshal(env.Payload, &s); err != nil {
			return disagree(subject, "malformed status-update: %v", err)
		}
		if !s.Final {
			return disagree(subject, "non-final status-update on a supervisor subject; supervisors emit only terminal events")
		}
		switch {
		case p.Supervisor != "" && env.From.Session != p.Supervisor:
			return disagree(subject, "from.session %q is not the supervisor %q", env.From.Session, p.Supervisor)
		case p.Supervisor == "" && namesAddressee(env.From, addressee):
			return disagree(subject, "from %s names the addressee; an executor is not its own supervisor", describeParty(env.From))
		}
	case TaskClassIn:
		if env.Kind != KindMessage && env.Kind != KindCancel {
			return disagree(subject, "kind %q is not an in kind", env.Kind)
		}
		// `to` is checked above, for every class. The negative form. The subject names the addressee, not the
		// requester, so "a requester" is not computable from the tokens;
		// what is computable is that the executor is not one.
		if namesAddressee(env.From, addressee) {
			return disagree(subject, "from %s names the addressee; an executor does not write its own in subject", describeParty(env.From))
		}
	}
	return nil
}

// checkDirectoryAgreement: kinds are closed-world here too, and the profile
// binding is `from.profile` equal to the subject token - a card relocated onto
// another profile's subject fails it. Cards carry no taskId by design, so a
// legitimate card must never be refused for lacking one.
func checkDirectoryAgreement(subject, profile string, env *Envelope) error {
	if env.Kind != KindAgentCard && env.Kind != KindAgentClosed {
		return disagree(subject, "kind %q is not a directory kind", env.Kind)
	}
	if env.From.Profile != profile {
		return disagree(subject, "from.profile %q does not name the directory subject's profile %q", env.From.Profile, profile)
	}
	return nil
}

func describeParty(p Party) string {
	if p.Profile != "" {
		return fmt.Sprintf("{session %q, profile %q}", p.Session, p.Profile)
	}
	return fmt.Sprintf("{session %q}", p.Session)
}
