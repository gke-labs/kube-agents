package lib

import (
	"encoding/json"
	"errors"
	"fmt"
	"time"
)

// The profile dispatcher's hand-off to warm workers and the per-task
// liveness lease (#3655; #3660, "Admission and holding"). Workers have no
// fetch grant on the shared durables: the dispatcher holds each submission
// and hands it to one worker on that worker's own subject, and the worker
// answers on a subject only it may publish, so the dispatcher can take the
// subject's pod token as the answer's identity and never a name in the
// payload. A worker runs one task at a time (it is reset between tasks) and
// renews that task's liveness lease by publishing on its own lease subject
// (DispatchLeaseSubject), and the
// dispatcher, the bucket's only writer, persists it under a key that starts
// with that (profile, tier, pod). Workers hold no grant on the bucket at all:
// a KV bucket allows rollup headers, so a put grant on any key would let a
// worker purge every lease in it. After a restart the dispatcher lists a
// (profile, tier)'s keys to rebuild which workers are busy.

// ProfileLeaseBucket is the KV bucket the leases live in.
const ProfileLeaseBucket = "PROFILE_LEASES"

// DispatchSubject is where the dispatcher hands a task to one worker pod of
// a (profile, tier) pool.
func DispatchSubject(profile, tier, pod string) string {
	return fmt.Sprintf("a2a.dispatch.%s.%s.%s", profile, tier, pod)
}

// DispatchAcceptSubject is where that worker answers.
func DispatchAcceptSubject(profile, tier, pod string) string {
	return DispatchSubject(profile, tier, pod) + ".accept"
}

// DispatchLeaseSubject is where that worker renews the lease of the task it
// runs, task id in the payload.
func DispatchLeaseSubject(profile, tier, pod string) string {
	return DispatchSubject(profile, tier, pod) + ".lease"
}

// ProfileLeaseKey is a task's liveness lease as the dispatcher persists it:
// <profile>.<tier>.<pod>.<taskId>.
func ProfileLeaseKey(profile, tier, pod, taskID string) string {
	return fmt.Sprintf("%s.%s.%s.%s", profile, tier, pod, taskID)
}

// ProfileLeaseSubject is the KV subject a lease key is written on.
func ProfileLeaseSubject(key string) string {
	return "$KV." + ProfileLeaseBucket + "." + key
}

// ProfileLeaseKeyFilter matches a task's lease whichever pod holds it:
// <profile>.<tier>.*.<taskId>. After a restart a redelivered submission
// names the task but not the pod, and its subject gives the profile and tier.
func ProfileLeaseKeyFilter(profile, tier, taskID string) string {
	return fmt.Sprintf("%s.%s.*.%s", profile, tier, taskID)
}

// ProfileDurableName is the shared durable on ProfileSubmitStream for one (profile, tier,
// kind): profile-<profile>-<tier>-<kind>. The operator creates it and the
// dispatcher is its only reader.
func ProfileDurableName(profile, tier, kind string) string {
	return fmt.Sprintf("profile-%s-%s-%s", profile, tier, kind)
}

// ProfileDurableFilter is that durable's filter: the submissions for one
// (profile, tier, kind), whatever the task.
func ProfileDurableFilter(profile, tier, kind string) string {
	return fmt.Sprintf("%s%s.%s.%s.*", SubmitSubjectPrefix, profile, tier, kind)
}

// The kinds of DispatchMessage.
//
// An offer is accepted or declined (DispatchAccept), and an accept is
// answered: confirm, and the worker starts the task; abandon, and it drops
// it. A worker never starts on its own accept. An accept is a core NATS
// publish, so it can be lost or arrive late, and a dispatcher that timed out
// and re-offered would otherwise have two workers running the task. The
// dispatcher confirms only the pod its live offer is with, on that pod's own
// DispatchSubject so no other worker can receive it, and answers any
// other accept, a late one included, with abandon. A worker that hears
// neither within the offer's accept window drops the task. A lost confirm
// therefore costs the task (its lease never starts, and it fails as
// lease-expired), never a second run.
const (
	DispatchOffer   = "offer"
	DispatchConfirm = "confirm"
	DispatchAbandon = "abandon"
)

// DispatchReasonCap bounds the free-text reasons in dispatch messages, in
// bytes.
const DispatchReasonCap = 1024

// DispatchMessage is what the dispatcher sends a worker on DispatchSubject.
// It is one subject, the only one the worker may read there, so the message
// says what it is.
type DispatchMessage struct {
	Type   string `json:"type"`
	TaskID string `json:"taskId"`
	// Kind is the submission's kind (SubmitKindTop or SubmitKindChild); an
	// offer only.
	Kind string `json:"kind,omitempty"`
	// Envelope is the submission, verbatim: the worker has no grant on
	// PROFILE_SUBMITS, so this is how it reads the task. An offer only.
	Envelope json.RawMessage `json:"envelope,omitempty"`
	// Deadline is when the task must have ended (the profile's
	// lifecycle.activeDeadlineSeconds from acceptance). An offer only.
	Deadline time.Time `json:"deadline,omitzero"`
	// LeaseSeconds and RenewSeconds are the dispatcher's intervals, sent
	// with each offer so the worker renews on the number the dispatcher
	// enforces and the intervals are set in one place.
	LeaseSeconds int `json:"leaseSeconds,omitempty"`
	RenewSeconds int `json:"renewSeconds,omitempty"`
	// AcceptSeconds is how long the worker waits, after accepting, for the
	// dispatcher's confirm or abandon; with neither it drops the task. The
	// dispatcher's acceptTimeoutSeconds, sent because workers don't read the
	// dispatch config. An offer only.
	AcceptSeconds int `json:"acceptSeconds,omitempty"`
	// Reason says why a task is abandoned. An abandon only.
	Reason string `json:"reason,omitempty"`
}

// Validate reports the first way m is malformed, or nil.
func (m DispatchMessage) Validate() error {
	if !ValidSubjectToken(m.TaskID) {
		return fmt.Errorf("dispatch %s: task id %q is not one subject token", m.Type, m.TaskID)
	}
	switch m.Type {
	case DispatchOffer:
		switch {
		case m.Kind != SubmitKindTop && m.Kind != SubmitKindChild:
			return fmt.Errorf("dispatch offer: kind %q is not %s or %s", m.Kind, SubmitKindTop, SubmitKindChild)
		case len(m.Envelope) == 0:
			return errors.New("dispatch offer: no envelope")
		case m.Deadline.IsZero():
			return errors.New("dispatch offer: no deadline")
		case m.RenewSeconds <= 0 || m.LeaseSeconds < 3*m.RenewSeconds:
			return fmt.Errorf("dispatch offer: lease %ds must be at least three renewals of %ds", m.LeaseSeconds, m.RenewSeconds)
		case m.AcceptSeconds <= 0:
			return errors.New("dispatch offer: no accept window")
		}
	case DispatchConfirm:
	case DispatchAbandon:
		if m.Reason == "" || len(m.Reason) > DispatchReasonCap {
			return fmt.Errorf("dispatch abandon: needs a reason of at most %d bytes", DispatchReasonCap)
		}
	default:
		return fmt.Errorf("dispatch message type %q is not %s, %s or %s", m.Type, DispatchOffer, DispatchConfirm, DispatchAbandon)
	}
	return nil
}

// DispatchAccept is a worker's answer to an offer, on
// DispatchAcceptSubject. The pod is the subject's, never a field.
type DispatchAccept struct {
	TaskID   string `json:"taskId"`
	Accepted bool   `json:"accepted"`
	// Reason says why an offer is declined.
	Reason string `json:"reason,omitempty"`
}

// Validate reports the first way a is malformed, or nil.
func (a DispatchAccept) Validate() error {
	switch {
	case !ValidSubjectToken(a.TaskID):
		return fmt.Errorf("dispatch accept: task id %q is not one subject token", a.TaskID)
	case !a.Accepted && (a.Reason == "" || len(a.Reason) > DispatchReasonCap):
		return fmt.Errorf("dispatch accept: a decline needs a reason of at most %d bytes", DispatchReasonCap)
	}
	return nil
}

// LeaseRenewal is a worker renewing one task's lease, on
// DispatchLeaseSubject, every DispatchMessage.RenewSeconds from acceptance
// until it publishes the task's terminal, and then once more with Final.
type LeaseRenewal struct {
	TaskID string `json:"taskId"`
	// Final says the worker has published the task's terminal on .events.
	// The dispatcher has no read on .events, so this is how it learns the
	// task ended: it releases the slot, acks the submission and deletes the
	// lease at once, rather than holding the slot for a lease and then
	// failing a task that finished. (A worker that dies between the two
	// still gets a lease-expired terminal, which the fold drops as a write
	// after the final event.)
	Final bool `json:"final,omitempty"`
}

// Validate reports whether r names a task.
func (r LeaseRenewal) Validate() error {
	if !ValidSubjectToken(r.TaskID) {
		return fmt.Errorf("lease renewal: task id %q is not one subject token", r.TaskID)
	}
	return nil
}

// LeaseRecord is a lease as the dispatcher persists it under
// ProfileLeaseKey. The dispatcher writes it when it confirms the task to a
// worker, not when the worker accepts: a worker that accepted but never heard
// the confirm hasn't started, and must not look like it is running. Expiry is
// read from ExpiresAt, not from a bucket TTL, so a dispatcher that restarts
// can read every record and give a lease that went quiet only while it was
// down the restart grace.
type LeaseRecord struct {
	TaskID      string    `json:"taskId"`
	Pod         string    `json:"pod"`
	Kind        string    `json:"kind"`
	ConfirmedAt time.Time `json:"confirmedAt"`
	RenewedAt   time.Time `json:"renewedAt"`
	ExpiresAt   time.Time `json:"expiresAt"`
	// Deadline, LeaseSeconds and RenewSeconds are the offer's, kept so a
	// restarted dispatcher re-adopts the task on the terms its worker was
	// given, not on a config that may have changed since.
	Deadline     time.Time `json:"deadline"`
	LeaseSeconds int       `json:"leaseSeconds"`
	RenewSeconds int       `json:"renewSeconds"`
}

// Expired reports whether the lease has run out at now, given grace on
// top of ExpiresAt (zero in steady state; the restart grace while a
// restarted dispatcher re-adopts).
func (l LeaseRecord) Expired(now time.Time, grace time.Duration) bool {
	return now.After(l.ExpiresAt.Add(grace))
}

// DispatchConfigKey is the key in the operator-rendered dispatch ConfigMap
// (<agent>-a2a-dispatch) that holds a DispatchConfig as JSON. The dispatcher
// watches that one ConfigMap, the way the callout watches the identity map,
// so a tuning or profile change applies without a restart.
const DispatchConfigKey = "dispatch.json"

// DispatchConfig is everything the dispatcher reads about the install's
// profiles: the shared caps and intervals and each bound profile's own
// limits. The operator writes it; the dispatcher never reads an AgentProfile.
//
// Every value is resolved: defaults applied, the ceiling already
// maxInProgress's value when spec.harness.tuning.profiles.ceiling is unset,
// the reserves already derived. The dispatcher uses the numbers as they stand
// and never re-derives one.
//
// A change applies to new offers only. A task already handed out keeps the
// LeaseSeconds and RenewSeconds its offer carried, which is what its worker
// renews on, so a live tuning change never expires a lease the worker is
// keeping on the old interval.
type DispatchConfig struct {
	// Version changes whenever anything below does.
	Version string `json:"version"`
	// Namespace is the install's namespace (the PlatformAgent's). A task of
	// a profile that declares no target parameters is checked against
	// namespace/<Namespace>; CheckProfileRun's target is never empty.
	Namespace string `json:"namespace"`
	// ProfileNamespace is where the worker pods run, <Namespace>-profiles;
	// the dispatcher watches pods there (ProfilePoolSelector).
	ProfileNamespace string `json:"profileNamespace"`
	// Ceiling caps running profile tasks of every kind, install-wide.
	// InteractiveReserve is kept for interactive tiers and ChildReserve for
	// child tasks: disjoint shares of the ceiling that together leave at
	// least one slot reserved for neither, so top-level unattended work can
	// always run. A task fills its own kind's reserve first, then the shared
	// slots. At a ceiling of 1 both are 0: every task runs alone, and a parent
	// can never start a child.
	Ceiling            int `json:"ceiling"`
	InteractiveReserve int `json:"interactiveReserve"`
	ChildReserve       int `json:"childReserve"`
	// The intervals; see DispatchMessage and LeaseRecord.
	LeaseSeconds         int `json:"leaseSeconds"`
	RenewSeconds         int `json:"renewSeconds"`
	RestartGraceSeconds  int `json:"restartGraceSeconds"`
	AcceptTimeoutSeconds int `json:"acceptTimeoutSeconds"`
	// Profiles are the profiles bound to this install with a rendered
	// identity. A profile absent here has no durables to read.
	Profiles []DispatchProfile `json:"profiles"`
}

// DispatchProfile is one profile's limits, from its AgentProfile.
type DispatchProfile struct {
	Name  string   `json:"name"`
	Tiers []string `json:"tiers"`
	// Concurrency is the profile's own ceiling (spec.concurrency).
	Concurrency           int                `json:"concurrency"`
	QueueTimeoutSeconds   int                `json:"queueTimeoutSeconds"`
	ActiveDeadlineSeconds int                `json:"activeDeadlineSeconds"`
	Parameters            []ProfileParameter `json:"parameters,omitempty"`
	// Durables are the shared durables the operator created for it, one
	// per (tier, kind).
	Durables []DispatchDurable `json:"durables"`
}

// DispatchDurable names one shared durable on ProfileSubmitStream.
type DispatchDurable struct {
	Tier string `json:"tier"`
	Kind string `json:"kind"`
	Name string `json:"name"`
}

// Validate reports the first way c can't be dispatched on, or nil. The
// operator renders only configs that pass; the dispatcher checks again and
// keeps its last good config rather than run on a broken one.
func (c DispatchConfig) Validate() error {
	switch {
	case !ValidSubjectToken(c.Namespace) || c.ProfileNamespace == "":
		return fmt.Errorf("dispatch config: namespace %q, profile namespace %q", c.Namespace, c.ProfileNamespace)
	case c.Ceiling < 1:
		return fmt.Errorf("dispatch config: ceiling %d", c.Ceiling)
	case c.ChildReserve < 0 || c.InteractiveReserve < 0 || c.ChildReserve+c.InteractiveReserve > max(0, c.Ceiling-1):
		return fmt.Errorf("dispatch config: childReserve %d and interactiveReserve %d must leave a slot of ceiling %d reserved for neither", c.ChildReserve, c.InteractiveReserve, c.Ceiling)
	case c.RenewSeconds <= 0 || c.LeaseSeconds < 3*c.RenewSeconds:
		return fmt.Errorf("dispatch config: lease %ds must be at least three renewals of %ds", c.LeaseSeconds, c.RenewSeconds)
	case c.RestartGraceSeconds < 2*c.RenewSeconds:
		return fmt.Errorf("dispatch config: restart grace %ds must be at least two renewals of %ds", c.RestartGraceSeconds, c.RenewSeconds)
	case c.AcceptTimeoutSeconds <= 0:
		return fmt.Errorf("dispatch config: acceptTimeoutSeconds %d", c.AcceptTimeoutSeconds)
	}
	seen := map[string]bool{}
	for _, p := range c.Profiles {
		if !ValidSubjectToken(p.Name) || seen[p.Name] {
			return fmt.Errorf("dispatch config: profile %q is not a subject token, or is listed twice", p.Name)
		}
		seen[p.Name] = true
		if p.Concurrency < 1 || len(p.Tiers) == 0 || len(p.Durables) == 0 {
			return fmt.Errorf("dispatch config: profile %s needs a concurrency, a tier and a durable", p.Name)
		}
		for _, d := range p.Durables {
			if d.Name != ProfileDurableName(p.Name, d.Tier, d.Kind) {
				return fmt.Errorf("dispatch config: profile %s durable %q is not %s", p.Name, d.Name, ProfileDurableName(p.Name, d.Tier, d.Kind))
			}
		}
	}
	return nil
}

// The labels on a warm worker pod, which the dispatcher selects a pool by
// (ProfilePoolSelector) when it looks for a ready pod to offer a task to.
// The operator's ServiceAccounts in the profile namespace carry the same
// profile and tier labels.
const (
	ProfileComponentLabel  = "kubeagents.x-k8s.io/a2a-component"
	ProfileWorkerComponent = "agentprofile-worker"
	ProfileNameLabel       = "kubeagents.x-k8s.io/agentprofile"
	ProfileTierLabel       = "kubeagents.x-k8s.io/agentprofile-tier"
)

// ProfilePoolSelector is the label selector for one (profile, tier)'s warm
// worker pods. Ready pods among them, less the ones holding a lease, are the
// ones a task can be offered to.
func ProfilePoolSelector(profile, tier string) string {
	return fmt.Sprintf("%s=%s,%s=%s,%s=%s", ProfileComponentLabel, ProfileWorkerComponent, ProfileNameLabel, profile, ProfileTierLabel, tier)
}
