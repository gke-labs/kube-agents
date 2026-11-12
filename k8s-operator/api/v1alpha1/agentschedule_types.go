/*
Copyright 2026.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package v1alpha1

import (
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

const (
	// AgentScheduleOriginLabel says who owns a schedule: builtin (rendered by
	// the operator) or user (created through the schedule tool).
	AgentScheduleOriginLabel = "kubeagents.x-k8s.io/origin"
	// AgentScheduleOriginBuiltin is a schedule the operator renders.
	AgentScheduleOriginBuiltin = "builtin"
	// AgentScheduleOriginUser is a schedule a person created.
	AgentScheduleOriginUser = "user"

	// AgentScheduleRunNowAnnotation asks for one run now: setting it to a
	// token the status has not handled yet starts a run, or adopts the one
	// in flight (docs/designs/scheduling-on-profiles.md, section 7).
	AgentScheduleRunNowAnnotation = "kubeagents.x-k8s.io/run-now"

	// AgentScheduleConditionAccepted reports whether the schedule parses and
	// its profile allows the unattended tier.
	AgentScheduleConditionAccepted = "Accepted"

	// AgentScheduleConcurrencyForbid is the one concurrency policy: a run
	// never starts while the last one is still going.
	AgentScheduleConcurrencyForbid = "Forbid"

	// AgentScheduleDeliveryChat posts a run's report to the home channel.
	AgentScheduleDeliveryChat = "chat"
	// AgentScheduleDeliveryLocal keeps a run's report and posts nothing.
	AgentScheduleDeliveryLocal = "local"
)

// The outcomes a run ends with.
const (
	AgentScheduleOutcomeSucceeded = "Succeeded"
	AgentScheduleOutcomeFailed    = "Failed"
	AgentScheduleOutcomeCancelled = "Cancelled"
	// AgentScheduleOutcomeLost is a run whose task vanished or outlived its
	// deadline, so the schedule moved on without its terminal.
	AgentScheduleOutcomeLost = "Lost"
)

// AgentScheduleProfileRef names the AgentProfile a schedule's runs go to.
type AgentScheduleProfileRef struct {
	// Name is the AgentProfile's name, in the schedule's namespace.
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=63
	// +required
	Name string `json:"name"`
}

// AgentScheduleDelivery is where a run's report goes.
type AgentScheduleDelivery struct {
	// Mode is chat (the home channel) or local (kept, not posted).
	// +kubebuilder:validation:Enum=chat;local
	// +kubebuilder:default=chat
	// +optional
	Mode string `json:"mode,omitempty"`

	// SilentWhenClean posts nothing for a run that found nothing.
	// +optional
	SilentWhenClean bool `json:"silentWhenClean,omitempty"`

	// JobKey groups a schedule's posts into one thread per UTC day. It
	// defaults to the schedule's name.
	// +kubebuilder:validation:MaxLength=63
	// +optional
	JobKey string `json:"jobKey,omitempty"`
}

// AgentScheduleSpec is a recurring task: when it runs, which profile runs
// it, and what it asks (docs/designs/scheduling-on-profiles.md, section 1).
// Each run is submitted through the A2A gateway as an Unattended task.
type AgentScheduleSpec struct {
	// Schedule is a 5-field cron expression, read in UTC.
	// +kubebuilder:validation:XValidation:rule="self.trim().split(' ').filter(f, f != '').size() == 5",message="schedule must be a 5-field cron expression (minute hour day-of-month month day-of-week)"
	// +kubebuilder:validation:MaxLength=128
	// +required
	Schedule string `json:"schedule"`

	// Suspend skips every run while true. A slot that passes while
	// suspended is counted, not run later.
	// +optional
	Suspend bool `json:"suspend,omitempty"`

	// ProfileRef is the AgentProfile the runs go to. It must allow the
	// unattended tier.
	// +required
	ProfileRef AgentScheduleProfileRef `json:"profileRef"`

	// Prompt is what each run asks.
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=16384
	// +required
	Prompt string `json:"prompt"`

	// Parameters are passed to each run as task parameters. `stream` names
	// the run's audit report stream.
	// +kubebuilder:validation:MaxProperties=32
	// +optional
	Parameters map[string]string `json:"parameters,omitempty"`

	// Skills the run asks the profile to use.
	// +kubebuilder:validation:MaxItems=16
	// +listType=set
	// +optional
	Skills []string `json:"skills,omitempty"`

	// Delivery is where each run's report goes. Omitted, it is the home
	// channel (chat).
	// +kubebuilder:default={}
	// +optional
	Delivery AgentScheduleDelivery `json:"delivery,omitempty"`

	// ConcurrencyPolicy is Forbid, the one policy: a slot that comes due
	// while a run is still going is skipped and counted.
	// +kubebuilder:validation:Enum=Forbid
	// +kubebuilder:default=Forbid
	// +optional
	ConcurrencyPolicy string `json:"concurrencyPolicy,omitempty"`

	// StartingDeadlineSeconds is how late a slot may still start. A slot
	// missed by more (an operator outage, a gateway that stayed down) is
	// counted and skipped. Defaults to 900.
	// +kubebuilder:validation:Minimum=60
	// +kubebuilder:validation:Maximum=86400
	// +kubebuilder:default=900
	// +optional
	StartingDeadlineSeconds *int64 `json:"startingDeadlineSeconds,omitempty"`

	// ActiveDeadlineSeconds bounds a run. Past it, plus a grace, a run that
	// never reported a terminal is recorded Lost and the schedule moves on.
	// Unset, it is the profile's lifecycle.activeDeadlineSeconds, the
	// ceiling its Job runs under.
	// +kubebuilder:validation:Minimum=60
	// +kubebuilder:validation:Maximum=86400
	// +optional
	ActiveDeadlineSeconds *int64 `json:"activeDeadlineSeconds,omitempty"`

	// MaxTurns caps the run's model turns.
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:validation:Maximum=1000
	// +optional
	MaxTurns *int32 `json:"maxTurns,omitempty"`
}

// AgentSchedulePendingSlot is a run the reconciler has claimed but not yet
// seen accepted: it is written before the submission, so a restart re-sends
// the same idempotency key instead of firing twice or not at all.
type AgentSchedulePendingSlot struct {
	// SlotTime is the slot being run (now, for a run-now).
	SlotTime metav1.Time `json:"slotTime"`
	// IdempotencyKey identifies this run to the gateway.
	IdempotencyKey string `json:"idempotencyKey"`
	// RunNow is the run-now token this slot answers, when it is one.
	// +optional
	RunNow string `json:"runNow,omitempty"`
	// ClaimedAt is when the slot was claimed; the starting deadline is
	// counted from it for a run-now.
	ClaimedAt metav1.Time `json:"claimedAt"`
}

// AgentScheduleSkipCounts counts slots that did not run, by reason.
type AgentScheduleSkipCounts struct {
	// AlreadyRunning: the last run was still going (Forbid).
	AlreadyRunning int64 `json:"alreadyRunning"`
	// MissedWindow: the slot was more than the starting deadline late.
	MissedWindow int64 `json:"missedWindow"`
	// Suspended: the schedule was suspended.
	Suspended int64 `json:"suspended"`
	// GatewayUnavailable: the gateway did not accept the run within the
	// starting deadline.
	GatewayUnavailable int64 `json:"gatewayUnavailable"`
	// RateLimited: the gateway refused the run under the trigger's cap.
	RateLimited int64 `json:"rateLimited"`
}

// AgentScheduleRun is one run in the recent-runs ring.
type AgentScheduleRun struct {
	// TaskID is the run's task.
	TaskID string `json:"taskID"`
	// SlotTime is the slot it ran for.
	SlotTime metav1.Time `json:"slotTime"`
	// CompletionTime is when it ended.
	// +optional
	CompletionTime *metav1.Time `json:"completionTime,omitempty"`
	// Outcome is how it ended.
	Outcome string `json:"outcome"`
}

// AgentScheduleStatus is the schedule's run state. It is the run ledger: no
// state lives in a pod.
type AgentScheduleStatus struct {
	// ObservedGeneration is the .metadata.generation last reconciled.
	// +optional
	ObservedGeneration int64 `json:"observedGeneration,omitempty"`
	// LastScheduleTime is the last slot that was run.
	// +optional
	LastScheduleTime *metav1.Time `json:"lastScheduleTime,omitempty"`
	// NextScheduleTime is the next slot due.
	// +optional
	NextScheduleTime *metav1.Time `json:"nextScheduleTime,omitempty"`
	// Schedule is the cron expression NextScheduleTime was computed from. A
	// spec.schedule that differs moves the next slot to the new schedule's.
	// +optional
	Schedule string `json:"schedule,omitempty"`
	// PendingSlot is a claimed run not yet accepted by the gateway.
	// +optional
	PendingSlot *AgentSchedulePendingSlot `json:"pendingSlot,omitempty"`
	// ActiveTaskID is the run in flight, if any.
	// +optional
	ActiveTaskID string `json:"activeTaskID,omitempty"`
	// ActiveTaskStartedAt is when the run in flight was accepted.
	// +optional
	ActiveTaskStartedAt *metav1.Time `json:"activeTaskStartedAt,omitempty"`
	// LastTaskID is the most recent run's task.
	// +optional
	LastTaskID string `json:"lastTaskID,omitempty"`
	// LastCompletionTime is when the most recent run ended.
	// +optional
	LastCompletionTime *metav1.Time `json:"lastCompletionTime,omitempty"`
	// LastOutcome is how the most recent run ended.
	// +kubebuilder:validation:Enum=Succeeded;Failed;Cancelled;Lost
	// +optional
	LastOutcome string `json:"lastOutcome,omitempty"`
	// LastHandledRunNow is the last run-now token acted on.
	// +optional
	LastHandledRunNow string `json:"lastHandledRunNow,omitempty"`
	// SkipCounts counts slots that did not run.
	// +optional
	SkipCounts AgentScheduleSkipCounts `json:"skipCounts,omitempty"`
	// RecentRuns are the last ten runs, newest first.
	// +kubebuilder:validation:MaxItems=10
	// +optional
	RecentRuns []AgentScheduleRun `json:"recentRuns,omitempty"`
	// Conditions are the latest observations of the schedule.
	// +listType=map
	// +listMapKey=type
	// +optional
	Conditions []metav1.Condition `json:"conditions,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:printcolumn:name="Schedule",type=string,JSONPath=`.spec.schedule`
// +kubebuilder:printcolumn:name="Profile",type=string,JSONPath=`.spec.profileRef.name`
// +kubebuilder:printcolumn:name="Suspend",type=boolean,JSONPath=`.spec.suspend`
// +kubebuilder:printcolumn:name="Next",type=date,JSONPath=`.status.nextScheduleTime`
// +kubebuilder:printcolumn:name="Last",type=string,JSONPath=`.status.lastOutcome`

// AgentSchedule runs a prompt on an AgentProfile on a cron schedule, each run
// an Unattended task submitted through the A2A gateway
// (docs/designs/scheduling-on-profiles.md).
type AgentSchedule struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	Spec   AgentScheduleSpec   `json:"spec,omitempty"`
	Status AgentScheduleStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true

// AgentScheduleList contains a list of AgentSchedule.
type AgentScheduleList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []AgentSchedule `json:"items"`
}

func init() {
	SchemeBuilder.Register(&AgentSchedule{}, &AgentScheduleList{})
}
