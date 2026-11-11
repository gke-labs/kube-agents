package lib

import (
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"regexp"
	"slices"
	"strings"
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
// worker purge every lease in it. Before each hand-off the dispatcher reads
// a (profile, tier)'s records to know which workers are busy, held here or
// not (a restart's redeliveries come up to an AckWait later).

// ProfileLeaseBucket is the KV bucket the leases live in.
const ProfileLeaseBucket = "PROFILE_LEASES"

// ProfileSubmitRetention is how long ProfileSubmitStream keeps a
// submission, acked or not: it mirrors the operator's `--max-age=72h` on
// PROFILE_SUBMITS (a limits-retention stream). A durable re-created with
// DeliverAll replays everything inside it, so the dispatcher keeps a
// finished task's tombstone (LeaseRecord.FinishedAt) at least this long
// when the dispatch config carries no SubmitRetentionSeconds, and never
// less than this when it does. The operator renders the live stream's max
// age when it is longer than its own constant (#4948), so a stream an older
// operator created longer is covered by the config, not by this floor.
const ProfileSubmitRetention = 72 * time.Hour

// MaxSubmitRetention is the longest SubmitRetentionSeconds a dispatch
// config may carry. It keeps the retention, and a tombstone's keep-until
// time, well inside a time.Duration.
const MaxSubmitRetention = 365 * 24 * time.Hour

// ProfileLeaseRefusedPod is the pod token of the tombstone the dispatcher
// writes for a submission it refused at admission, which no pod ever held.
// It is not a DNS label, so it can't be a pod's name.
const ProfileLeaseRefusedPod = "_refused"

// DispatcherSession is the `from.session` of the terminals the profile
// dispatcher writes on a profile task's `…supervisor` subject (a refusal at
// admission, a lease or deadline failure). A consumer that names its
// supervisor (AgreementPolicy.Supervisor) lists it in OtherSupervisors to
// read them.
const DispatcherSession = "dispatcher"

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
	// TaskCredential and ProxyToken are the task's own credentials (#3889,
	// a2a/taskcred), minted for the confirmed pod. A confirm only, and only
	// when the dispatcher has a task-signing key; without them the worker
	// keeps its pod-level grants. TaskCredential (audience nats) is what the
	// worker presents on its task connection to the bus. ProxyToken
	// (audience credential-proxy) confines the task to the cluster the
	// dispatcher checked at admission, and is absent when the task names no
	// cluster. Both name the task, its profile and tier, the pod and its UID,
	// and expire at the task's deadline plus a clock-skew allowance, never
	// past the configured maximum lifetime.
	TaskCredential string `json:"taskCredential,omitempty"`
	ProxyToken     string `json:"proxyToken,omitempty"`
}

// Validate reports the first way m is malformed, or nil.
func (m DispatchMessage) Validate() error {
	if !ValidSubjectToken(m.TaskID) {
		return fmt.Errorf("dispatch %s: task id %q is not one subject token", m.Type, m.TaskID)
	}
	if m.Type != DispatchConfirm && (m.TaskCredential != "" || m.ProxyToken != "") {
		return fmt.Errorf("dispatch %s: task credentials ride on a confirm only", m.Type)
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
	// task ended: it releases the slot, acks the submission and turns the
	// lease into a tombstone at once, rather than holding the slot for a lease and then
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
	// FinishedAt, set, makes the record a tombstone: the task ended (its
	// final renewal came, or the dispatcher wrote its terminal: failed, or
	// rejected at admission) at this time. A submission stream keeps acked
	// submissions for its max age (DispatchConfig.SubmitRetentionSeconds,
	// or ProfileSubmitRetention), so a durable re-created with
	// DeliverAll replays finished tasks; the tombstone, kept under the same
	// key at least that long, is how the dispatcher knows a replay is one
	// and acks it without running or failing it again. A tombstone holds no
	// worker and is never re-adopted or renewed.
	FinishedAt time.Time `json:"finishedAt,omitzero"`
}

// Finished reports whether the record is a tombstone.
func (l LeaseRecord) Finished() bool { return !l.FinishedAt.IsZero() }

// Expired reports whether the lease has run out at now, given grace on
// top of ExpiresAt (zero in steady state; the restart grace while a
// restarted dispatcher re-adopts). A tombstone is expired at every time,
// whatever its ExpiresAt says: its task is over, so it keeps no pod busy
// and is never re-adopted. Expired is never how a tombstone is told from a
// running lease (an expired running lease is still a task to fail); that
// is Finished.
func (l LeaseRecord) Expired(now time.Time, grace time.Duration) bool {
	if l.Finished() {
		return true
	}
	return now.After(l.ExpiresAt.Add(grace))
}

// Validate reports the first way l can't be written, or nil: every record
// names its task and its pod (a tombstone of a refusal, its
// ProfileLeaseRefusedPod), and a running lease has an expiry.
func (l LeaseRecord) Validate() error {
	switch {
	case !ValidSubjectToken(l.TaskID):
		return fmt.Errorf("lease record: task id %q is not one subject token", l.TaskID)
	case !ValidSubjectToken(l.Pod) && !(l.Finished() && l.Pod == ProfileLeaseRefusedPod):
		return fmt.Errorf("lease record for %s: pod %q is not one subject token", l.TaskID, l.Pod)
	case !l.Finished() && l.ExpiresAt.IsZero():
		return fmt.Errorf("lease record for %s: a running lease needs an expiry", l.TaskID)
	}
	return nil
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
	// BreakerThreshold lease expiries in a row on one (profile, tier) open
	// its breaker, which holds new submissions there queued for
	// BreakerCooldownSeconds. 0 takes the dispatcher's defaults (3 and 300).
	BreakerThreshold       int `json:"breakerThreshold,omitempty"`
	BreakerCooldownSeconds int `json:"breakerCooldownSeconds,omitempty"`
	// JobStartSeconds is how long an unattended task's Job may take to have
	// a Ready worker pod before the task fails no-worker. 0 takes the
	// dispatcher's default (300).
	JobStartSeconds int `json:"jobStartSeconds,omitempty"`
	// StopGraceSeconds is how long a running task's executor has to stop on
	// its own after a stop record (contract addendum 9) before the
	// dispatcher ends it. 0 takes the dispatcher's default (30).
	StopGraceSeconds int `json:"stopGraceSeconds,omitempty"`
	// SubmitRetentionSeconds is how long ProfileSubmitStream keeps a
	// submission (the operator renders it from the same value as the
	// stream's max age). A durable re-created over the stream replays
	// everything inside it, so the dispatcher keeps a finished task's
	// tombstone at least this long. Zero is from an operator that predates
	// the field; the dispatcher then uses ProfileSubmitRetention.
	SubmitRetentionSeconds int `json:"submitRetentionSeconds,omitempty"`
	// JobLabels go on every Job the dispatcher creates: the operator's
	// agent labels, by which a teardown deletes only this install's Jobs,
	// and DispatchJobLabel. Set when some profile has a Job template
	// (HasJobTemplate).
	JobLabels map[string]string `json:"jobLabels,omitempty"`
	// Profiles are the profiles bound to this install with a rendered
	// identity, and those draining after they left it (DrainingUntil). A
	// profile absent here has no durables to read.
	Profiles []DispatchProfile `json:"profiles"`
}

// DispatchJobTemplateRef is where a profile's Job template is, and which
// rendering of it this config means.
type DispatchJobTemplateRef struct {
	// Name is the DispatchJobTemplate's name in the profile namespace. An
	// operator from #5479 on leaves it out: it is always the profile's own
	// param, DispatchJobTemplateName (NameFor).
	Name string `json:"name,omitempty"`
	// Hash is the rendering's hash, as the object's
	// DispatchJobTemplateHashAnnotation carries it.
	Hash string `json:"hash"`
}

// dispatchJobTemplatePrefix is a profile's DispatchJobTemplate name, less the
// profile's name: the operator's agentProfileJobTemplateName.
const dispatchJobTemplatePrefix = "kube-agents-dispatch-job-profile-"

// DispatchJobTemplateName is the DispatchJobTemplate that holds profile's Job
// template in the profile namespace.
func DispatchJobTemplateName(profile string) string { return dispatchJobTemplatePrefix + profile }

// NameFor is the DispatchJobTemplate r names for profile: its Name when it
// carries one, else the profile's own param.
func (r *DispatchJobTemplateRef) NameFor(profile string) string {
	if r.Name != "" {
		return r.Name
	}
	return DispatchJobTemplateName(profile)
}

// DerivedDurables are the shared durables p is read on: one per kind for each
// of its live tiers and each tier it drains, so what is queued at a draining
// tier still reaches a terminal (tier-withdrawn). A draining profile has none.
// The operator creates the same set (its profileDurableName over
// AllowedTierList), pinned by the shared fixture testdata/rendered-dispatch-durables.json.
func (p DispatchProfile) DerivedDurables() []DispatchDurable {
	if p.Draining() {
		return nil
	}
	tiers := append([]string(nil), p.Tiers...)
	for _, d := range p.DrainingTiers {
		tiers = append(tiers, d.Tier)
	}
	out := make([]DispatchDurable, 0, 2*len(tiers))
	for _, tier := range tiers {
		for _, kind := range []string{SubmitKindTop, SubmitKindChild} {
			out = append(out, DispatchDurable{Tier: tier, Kind: kind, Name: ProfileDurableName(p.Name, tier, kind)})
		}
	}
	return out
}

// sameDurables reports whether a and b name the same set of durables.
func sameDurables(a, b []DispatchDurable) bool {
	if len(a) != len(b) {
		return false
	}
	set := map[DispatchDurable]int{}
	for _, d := range a {
		set[d]++
	}
	for _, d := range b {
		if set[d] == 0 {
			return false
		}
		set[d]--
	}
	return true
}

// DispatchJobTemplateHashAnnotation is the annotation on a DispatchJobTemplate
// that says which rendering of the profile's template it holds, matched
// against DispatchJobTemplateRef.Hash.
const DispatchJobTemplateHashAnnotation = "kubeagents.x-k8s.io/dispatch-template-hash"

// HasJobTemplate says the profile's unattended tasks run as Jobs: the config
// names a template, inline or by reference.
func (p DispatchProfile) HasJobTemplate() bool {
	return len(p.JobTemplate) > 0 || p.JobTemplateRef != nil
}

// DispatchProfile is one profile's limits, from its AgentProfile.
type DispatchProfile struct {
	Name  string   `json:"name"`
	Tiers []string `json:"tiers"`
	// UnattendedTier names the profile's unattended-mode tier when it isn't
	// the built-in unattended: a Tier resource of that mode (#3659). The
	// dispatcher treats it as it treats unattended (Job mode, the
	// unattended side of the interactive reserve). Empty for the built-in.
	UnattendedTier string `json:"unattendedTier,omitempty"`
	// Concurrency is the profile's own ceiling (spec.concurrency).
	Concurrency           int                `json:"concurrency"`
	QueueTimeoutSeconds   int                `json:"queueTimeoutSeconds"`
	ActiveDeadlineSeconds int                `json:"activeDeadlineSeconds"`
	Parameters            []ProfileParameter `json:"parameters,omitempty"`
	// Durables, when listed, are the shared durables the operator created
	// for it, one per (tier, kind). An operator from #5479 on no longer lists
	// them: the dispatcher derives them (DerivedDurables). For one release a
	// listed set is still accepted, and Validate holds it to exactly the
	// derived one. A draining profile lists none.
	Durables []DispatchDurable `json:"durables,omitempty"`
	// JobTemplate is the pod template (a core/v1 PodTemplateSpec) the
	// profile's unattended tasks run on as Jobs, rendered by the operator
	// only where the chart installed the Job admission policy, which holds
	// each Job to it exactly. The dispatcher uses it as it is. Absent, the
	// profile's unattended tasks have no worker (the operator builds warm
	// pools for the read tier only) and the dispatcher refuses them
	// no-worker at admission.
	JobTemplate json.RawMessage `json:"jobTemplate,omitempty"`
	// JobTemplateRef names the DispatchJobTemplate in the profile namespace
	// that holds the profile's Job template, in place of JobTemplate. The
	// operator writes that object, the admission policy's param, before the
	// config that names it. The dispatcher reads the template from there
	// only when the object's DispatchJobTemplateHashAnnotation equals Hash,
	// so it never builds a Job from a template the config has moved past.
	// An operator that predates the field sends JobTemplate instead.
	JobTemplateRef *DispatchJobTemplateRef `json:"jobTemplateRef,omitempty"`
	// DrainingUntil, set, makes the profile draining (#5074): it has left
	// the install, so it has no durables and nothing new is dispatched for
	// it, but it stays listed, and keeps its .supervisor grant, until then,
	// so the terminals of the tasks it still holds reach their requesters.
	DrainingUntil time.Time `json:"drainingUntil,omitzero"`
	// DrainingTiers are tiers that left a live profile and still drain
	// (#3659, 2b-ii): dropped from allowedTiers, or their Tier deleted. Their
	// durables stay listed, so what is queued at them is read and refused
	// tier-withdrawn rather than left with no terminal, and nothing new
	// starts at them; a task already held at one runs to its end. Not in
	// Tiers.
	DrainingTiers []DrainingTier `json:"drainingTiers,omitempty"`
}

// DrainingTier is one tier a live profile drains, until Until.
type DrainingTier struct {
	Tier string `json:"tier"`
	// Mode is the tier's mode when it left, interactive or unattended: what
	// the operator keeps rendering for it while it drains.
	Mode  string    `json:"mode"`
	Until time.Time `json:"until"`
}

// DrainsTier reports whether tier is one p drains (DrainingTiers).
func (p DispatchProfile) DrainsTier(tier string) bool {
	for _, d := range p.DrainingTiers {
		if d.Tier == tier {
			return true
		}
	}
	return false
}

// Draining reports whether the profile is draining (DrainingUntil).
func (p DispatchProfile) Draining() bool { return !p.DrainingUntil.IsZero() }

// DispatchJobLabel marks a Job the dispatcher created (the operator's
// dispatchJobLabel).
const DispatchJobLabel = "kubeagents.x-k8s.io/dispatch-job"

// DispatchDurable names one shared durable on ProfileSubmitStream.
type DispatchDurable struct {
	Tier string `json:"tier"`
	Kind string `json:"kind"`
	Name string `json:"name"`
}

// MaxDispatchSeconds bounds every second count in a DispatchConfig, so each
// converts to a time.Duration, and a sum of three to a Job's deadline,
// without overflow.
const MaxDispatchSeconds = math.MaxInt32

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
	case c.JobStartSeconds < 0 || c.StopGraceSeconds < 0:
		return fmt.Errorf("dispatch config: job start %ds and stop grace %ds may not be negative", c.JobStartSeconds, c.StopGraceSeconds)
	case c.BreakerThreshold < 0 || c.BreakerCooldownSeconds < 0:
		return fmt.Errorf("dispatch config: breaker threshold %d and cooldown %ds may not be negative", c.BreakerThreshold, c.BreakerCooldownSeconds)
	case c.AcceptTimeoutSeconds <= 0:
		return fmt.Errorf("dispatch config: acceptTimeoutSeconds %d", c.AcceptTimeoutSeconds)
	case max(c.LeaseSeconds, c.RenewSeconds, c.RestartGraceSeconds, c.AcceptTimeoutSeconds,
		c.BreakerCooldownSeconds, c.JobStartSeconds, c.StopGraceSeconds) > MaxDispatchSeconds:
		return fmt.Errorf("dispatch config: a second count is over %d", MaxDispatchSeconds)
	case c.SubmitRetentionSeconds < 0 || c.SubmitRetentionSeconds > int(MaxSubmitRetention/time.Second):
		return fmt.Errorf("dispatch config: submitRetentionSeconds %d is outside 0 to %d", c.SubmitRetentionSeconds, int(MaxSubmitRetention/time.Second))
	}
	for k, v := range c.JobLabels {
		if !validLabelKey(k) || !labelValue.MatchString(v) {
			return fmt.Errorf("dispatch config: jobLabels %q=%q is not a valid label", k, v)
		}
	}
	seen := map[string]bool{}
	for _, p := range c.Profiles {
		if !ValidSubjectToken(p.Name) || seen[p.Name] {
			return fmt.Errorf("dispatch config: profile %q is not a subject token, or is listed twice", p.Name)
		}
		seen[p.Name] = true
		if len(p.JobTemplate) > 0 && !PodTemplateWithContainers(p.JobTemplate) {
			return fmt.Errorf("dispatch config: profile %s's jobTemplate is not a pod template with containers", p.Name)
		}
		if r := p.JobTemplateRef; r != nil && (len(p.JobTemplate) > 0 || (r.Name != "" && !objectName.MatchString(r.Name)) || r.Hash == "" || !labelValue.MatchString(r.Hash)) {
			return fmt.Errorf("dispatch config: profile %s's jobTemplateRef needs a hash (and, if it names one, a valid name), and no inline jobTemplate beside it", p.Name)
		}
		if max(p.QueueTimeoutSeconds, p.ActiveDeadlineSeconds) > MaxDispatchSeconds {
			return fmt.Errorf("dispatch config: profile %s has a second count over %d", p.Name, MaxDispatchSeconds)
		}
		if p.Concurrency < 1 || len(p.Tiers) == 0 {
			return fmt.Errorf("dispatch config: profile %s needs a concurrency and a tier", p.Name)
		}
		if p.Draining() && len(p.Durables) > 0 {
			return fmt.Errorf("dispatch config: draining profile %s lists %d durables; a draining one has none", p.Name, len(p.Durables))
		}
		drains := map[string]bool{}
		for _, d := range p.DrainingTiers {
			if !ValidSubjectToken(d.Tier) || drains[d.Tier] || slices.Contains(p.Tiers, d.Tier) || d.Until.IsZero() {
				return fmt.Errorf("dispatch config: profile %s drains tier %q, which is not a subject token, is listed twice or live, or has no end", p.Name, d.Tier)
			}
			drains[d.Tier] = true
		}
		for _, d := range p.Durables {
			if d.Name != ProfileDurableName(p.Name, d.Tier, d.Kind) {
				return fmt.Errorf("dispatch config: profile %s durable %q is not %s", p.Name, d.Name, ProfileDurableName(p.Name, d.Tier, d.Kind))
			}
		}
		// An entry that still lists its durables (an operator from before
		// #5479) lists exactly the derived set; neither side is preferred.
		if len(p.Durables) > 0 && !sameDurables(p.Durables, p.DerivedDurables()) {
			return fmt.Errorf("dispatch config: profile %s lists durables that are not the ones its tiers derive", p.Name)
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

// PodTemplateWithContainers reports whether raw is a JSON pod template with
// at least one container: a null, an empty object or a scalar puts no
// profile in Job mode.
func PodTemplateWithContainers(raw json.RawMessage) bool {
	var t struct {
		Spec *struct {
			Containers []json.RawMessage `json:"containers"`
		} `json:"spec"`
	}
	return json.Unmarshal(raw, &t) == nil && t.Spec != nil && len(t.Spec.Containers) > 0
}

// labelValue and labelName are Kubernetes' label value and name syntax.
var (
	// objectName is a DNS-1123 subdomain, an object's name.
	objectName  = regexp.MustCompile(`^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$`)
	labelValue  = regexp.MustCompile(`^(([A-Za-z0-9][-A-Za-z0-9_.]{0,61})?[A-Za-z0-9])?$`)
	labelName   = regexp.MustCompile(`^([A-Za-z0-9][-A-Za-z0-9_.]{0,61})?[A-Za-z0-9]$`)
	labelPrefix = regexp.MustCompile(`^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$`)
)

// validLabelKey reports whether k is a Kubernetes label key: a name, with
// an optional DNS-subdomain prefix and a slash.
func validLabelKey(k string) bool {
	prefix, name, found := strings.Cut(k, "/")
	if !found {
		return labelName.MatchString(k)
	}
	return len(prefix) <= 253 && labelPrefix.MatchString(prefix) && labelName.MatchString(name)
}
