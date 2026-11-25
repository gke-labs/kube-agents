package busmigrate

import (
	"context"
	_ "embed"
	"encoding/json"
	"errors"
	"fmt"
	"maps"
	"reflect"
	"slices"
	"strings"
	"time"

	"github.com/nats-io/nats.go"
)

const (
	errSubjectAppendRefusedText     = "stream subject append refused"
	subjectAppendStreamErrFmt       = "stream %s: %w"
	subjectAppendDiffsErrFmt        = "%w: stream %s: %s"
	subjectAppendMissingErrFmt      = "%w: %s"
	subjectAppendNameErrFmt         = "%w: stream %s: name changed (%q -> %q)"
	subjectAppendRePublishErrFmt    = "%w: stream %s: republish is not permitted"
	subjectAppendSourcesErrFmt      = "%w: stream %s: sources are not permitted"
	subjectAppendMirrorErrFmt       = "%w: stream %s: mirror is not permitted"
	subjectAppendEmptyErrFmt        = "%w: stream %s: empty subject list is not permitted"
	subjectAppendExistingErrFmt     = "%w: stream %s: existing subject %q is not one of %v"
	subjectAppendDupExistingErrFmt  = "%w: stream %s: duplicate existing subject %q"
	subjectAppendRemovedErrFmt      = "%w: stream %s: subject %q was removed"
	subjectAppendAddedErrFmt        = "%w: stream %s: added subject %q is not one of %v"
	subjectAppendDupAddedErrFmt     = "%w: stream %s: duplicate subject %q"
	subjectAppendRetentionErrFmt    = "%w: stream %s: retention changed (%v -> %v)"
	subjectAppendStorageErrFmt      = "%w: stream %s: storage changed (%v -> %v)"
	subjectAppendMaxBytesErrFmt     = "%w: stream %s: max_bytes changed (%d -> %d)"
	subjectAppendMaxAgeErrFmt       = "%w: stream %s: max_age changed (%s -> %s)"
	subjectAppendDiscardErrFmt      = "%w: stream %s: discard changed (%v -> %v)"
	subjectAppendMaxMsgsSubjErrFmt  = "%w: stream %s: max_msgs_per_subject changed (%d -> %d)"
	subjectAppendMaxMsgsErrFmt      = "%w: stream %s: max_msgs changed (%d -> %d)"
	subjectAppendMaxMsgSizeErrFmt   = "%w: stream %s: max_msg_size changed (%d -> %d)"
	subjectAppendMaxConsumersErrFmt = "%w: stream %s: max_consumers changed (%d -> %d)"
	subjectAppendReplicasErrFmt     = "%w: stream %s: num_replicas changed (%d -> %d)"
	subjectAppendFlagsErrFmt        = "%w: stream %s: retention or access flags changed"
	subjectAppendDuplicatesErrFmt   = "%w: stream %s: duplicate_window changed (%s -> %s)"
	subjectAppendOutsideErrFmt      = "%w: stream %s: config changed outside subjects"
	policyDiffSeparator             = "; "
)

// StreamPolicy is the operator's render of one provisioned stream, on the
// fields that decide what the stream captures, keeps and lets clients do.
// A restore sends the snapshot's own config, pinned by the operator's
// digest, and only when that config matches this policy on every field
// here; the restored stream is read back against the same policy and
// deleted on any difference. The operator renders it from the values the
// provision script passes (k8s-operator/internal/controller:
// a2aStreamPolicies, which a test holds to the script's flags).
type StreamPolicy struct {
	Name              string               `json:"name"`
	Subjects          []string             `json:"subjects"`
	Retention         nats.RetentionPolicy `json:"retention"`
	Storage           nats.StorageType     `json:"storage"`
	MaxBytes          int64                `json:"max_bytes"`
	MaxAge            time.Duration        `json:"max_age"`
	Discard           nats.DiscardPolicy   `json:"discard"`
	MaxMsgsPerSubject int64                `json:"max_msgs_per_subject"`
	AllowRollup       bool                 `json:"allow_rollup_hdrs"`
	DenyDelete        bool                 `json:"deny_delete"`
	DenyPurge         bool                 `json:"deny_purge"`
	AllowDirect       bool                 `json:"allow_direct"`
	Replicas          int                  `json:"num_replicas"`
	// MaxConsumers is the max_consumers the render creates the stream with
	// (the provision script's --max-consumers); zero is a stream the script
	// leaves at the server's default, on which Check does not compare it.
	// JetStream never changes a stream's max_consumers after it is created,
	// so a restore is the one moment a stream can take a new one. The
	// forward restore raises a narrower snapshot to it and keeps a wider
	// one (widenedMaxConsumers); the way back keeps the snapshot's own. An
	// older restore pod ignores the field and keeps the snapshot's own,
	// which is today's behaviour.
	MaxConsumers int `json:"max_consumers,omitempty"`
}

// Check lists every field on which cfg differs from the policy, and names a
// source, mirror or republish, none of which a provisioned stream has.
// Empty means cfg matches.
//
// Subjects are a membership test, not equality: every subject cfg names
// must be, string for string, one of the policy's, and there must be at
// least one. String-exact, never wildcard coverage: `a2a.>` or
// `a2a.topics.>` is refused against a policy listing `a2a.topics.x`, since
// either captures more. Fewer subjects capture less, so a subset is
// accepted; an install provisioned before a subject was added (TOPICS-STATE
// before topology-skew, which the provision Job never edits in) restores
// as it was, and MissingSubjects reports the gap as a warning. An empty
// list is refused, because the server reads it as the stream's own name.
func (p StreamPolicy) Check(cfg nats.StreamConfig) []string {
	var diffs []string
	field := func(name string, want, got any) {
		if !reflect.DeepEqual(want, got) {
			diffs = append(diffs, fmt.Sprintf("%s: want %v, got %v", name, want, got))
		}
	}
	field("name", p.Name, cfg.Name)
	if len(cfg.Subjects) == 0 {
		diffs = append(diffs, "subjects: want some of "+fmt.Sprint(p.Subjects)+", got none, which the server reads as the stream's name")
	}
	for _, subject := range cfg.Subjects {
		if !slices.Contains(p.Subjects, subject) {
			diffs = append(diffs, fmt.Sprintf("subjects: %q is not one of %v", subject, p.Subjects))
		}
	}
	field("retention", p.Retention, cfg.Retention)
	field("storage", p.Storage, cfg.Storage)
	field("max_bytes", p.MaxBytes, cfg.MaxBytes)
	field("max_age", p.MaxAge, cfg.MaxAge)
	field("discard", p.Discard, cfg.Discard)
	field("max_msgs_per_subject", p.MaxMsgsPerSubject, cfg.MaxMsgsPerSubject)
	field("allow_rollup_hdrs", p.AllowRollup, cfg.AllowRollup)
	field("deny_delete", p.DenyDelete, cfg.DenyDelete)
	field("deny_purge", p.DenyPurge, cfg.DenyPurge)
	field("allow_direct", p.AllowDirect, cfg.AllowDirect)
	field("num_replicas", p.Replicas, cfg.Replicas)
	if p.MaxConsumers != 0 {
		field("max_consumers", p.MaxConsumers, cfg.MaxConsumers)
	}
	if len(cfg.Sources) > 0 {
		diffs = append(diffs, fmt.Sprintf("sources: want none, got %d", len(cfg.Sources)))
	}
	if cfg.Mirror != nil {
		diffs = append(diffs, "mirror: want none, got "+cfg.Mirror.Name)
	}
	if cfg.RePublish != nil {
		diffs = append(diffs, "republish: want none, got "+cfg.RePublish.Destination)
	}
	return diffs
}

// MissingSubjects names each policy subject cfg lacks: a stream that
// passes Check but captures less than a fresh install would. A warning,
// never a refusal.
func (p StreamPolicy) MissingSubjects(cfg nats.StreamConfig) []string {
	var missing []string
	for _, subject := range p.Subjects {
		if !slices.Contains(cfg.Subjects, subject) {
			missing = append(missing, fmt.Sprintf("stream %s: subject %s is missing; a fresh install has it", cfg.Name, subject))
		}
	}
	return missing
}

// ErrSubjectAppendRefused is returned when a subject-only stream update
// attempts any change other than appending missing policy subjects: a
// removed or unrendered subject, a changed limit or retention, or a
// RePublish, Mirror, or Source.
var ErrSubjectAppendRefused = errors.New(errSubjectAppendRefusedText)

// CheckSubjectAppend verifies that after differs from before only by
// appending subjects from p.Subjects: no subject removed from before, no
// duplicate or unrendered subject, no RePublish (nor Mirror or Sources),
// and no changed limit, retention, or other config field.
func (p StreamPolicy) CheckSubjectAppend(before, after nats.StreamConfig) error {
	if before.Name != p.Name || after.Name != p.Name {
		return fmt.Errorf(subjectAppendNameErrFmt, ErrSubjectAppendRefused, p.Name, before.Name, after.Name)
	}
	if before.RePublish != nil || after.RePublish != nil {
		return fmt.Errorf(subjectAppendRePublishErrFmt, ErrSubjectAppendRefused, p.Name)
	}
	if len(before.Sources) > 0 || len(after.Sources) > 0 {
		return fmt.Errorf(subjectAppendSourcesErrFmt, ErrSubjectAppendRefused, p.Name)
	}
	if before.Mirror != nil || after.Mirror != nil {
		return fmt.Errorf(subjectAppendMirrorErrFmt, ErrSubjectAppendRefused, p.Name)
	}
	if len(before.Subjects) == 0 || len(after.Subjects) == 0 {
		return fmt.Errorf(subjectAppendEmptyErrFmt, ErrSubjectAppendRefused, p.Name)
	}
	for i, s := range before.Subjects {
		if !slices.Contains(p.Subjects, s) {
			return fmt.Errorf(subjectAppendExistingErrFmt, ErrSubjectAppendRefused, p.Name, s, p.Subjects)
		}
		if slices.Contains(before.Subjects[:i], s) {
			return fmt.Errorf(subjectAppendDupExistingErrFmt, ErrSubjectAppendRefused, p.Name, s)
		}
		if !slices.Contains(after.Subjects, s) {
			return fmt.Errorf(subjectAppendRemovedErrFmt, ErrSubjectAppendRefused, p.Name, s)
		}
	}
	for i, s := range after.Subjects {
		if !slices.Contains(p.Subjects, s) {
			return fmt.Errorf(subjectAppendAddedErrFmt, ErrSubjectAppendRefused, p.Name, s, p.Subjects)
		}
		if slices.Contains(after.Subjects[:i], s) {
			return fmt.Errorf(subjectAppendDupAddedErrFmt, ErrSubjectAppendRefused, p.Name, s)
		}
	}
	if before.Retention != after.Retention {
		return fmt.Errorf(subjectAppendRetentionErrFmt, ErrSubjectAppendRefused, p.Name, before.Retention, after.Retention)
	}
	if before.Storage != after.Storage {
		return fmt.Errorf(subjectAppendStorageErrFmt, ErrSubjectAppendRefused, p.Name, before.Storage, after.Storage)
	}
	if before.MaxBytes != after.MaxBytes {
		return fmt.Errorf(subjectAppendMaxBytesErrFmt, ErrSubjectAppendRefused, p.Name, before.MaxBytes, after.MaxBytes)
	}
	if before.MaxAge != after.MaxAge {
		return fmt.Errorf(subjectAppendMaxAgeErrFmt, ErrSubjectAppendRefused, p.Name, before.MaxAge, after.MaxAge)
	}
	if before.Discard != after.Discard {
		return fmt.Errorf(subjectAppendDiscardErrFmt, ErrSubjectAppendRefused, p.Name, before.Discard, after.Discard)
	}
	if before.MaxMsgsPerSubject != after.MaxMsgsPerSubject {
		return fmt.Errorf(subjectAppendMaxMsgsSubjErrFmt, ErrSubjectAppendRefused, p.Name, before.MaxMsgsPerSubject, after.MaxMsgsPerSubject)
	}
	if before.MaxMsgs != after.MaxMsgs {
		return fmt.Errorf(subjectAppendMaxMsgsErrFmt, ErrSubjectAppendRefused, p.Name, before.MaxMsgs, after.MaxMsgs)
	}
	if before.MaxMsgSize != after.MaxMsgSize {
		return fmt.Errorf(subjectAppendMaxMsgSizeErrFmt, ErrSubjectAppendRefused, p.Name, before.MaxMsgSize, after.MaxMsgSize)
	}
	if before.MaxConsumers != after.MaxConsumers {
		return fmt.Errorf(subjectAppendMaxConsumersErrFmt, ErrSubjectAppendRefused, p.Name, before.MaxConsumers, after.MaxConsumers)
	}
	if before.Replicas != after.Replicas {
		return fmt.Errorf(subjectAppendReplicasErrFmt, ErrSubjectAppendRefused, p.Name, before.Replicas, after.Replicas)
	}
	if before.AllowRollup != after.AllowRollup || before.DenyDelete != after.DenyDelete || before.DenyPurge != after.DenyPurge || before.AllowDirect != after.AllowDirect {
		return fmt.Errorf(subjectAppendFlagsErrFmt, ErrSubjectAppendRefused, p.Name)
	}
	if before.Duplicates != after.Duplicates {
		return fmt.Errorf(subjectAppendDuplicatesErrFmt, ErrSubjectAppendRefused, p.Name, before.Duplicates, after.Duplicates)
	}
	afterWithoutSubjects := after
	afterWithoutSubjects.Subjects = before.Subjects
	if !reflect.DeepEqual(before, afterWithoutSubjects) {
		return fmt.Errorf(subjectAppendOutsideErrFmt, ErrSubjectAppendRefused, p.Name)
	}
	livePolicy := p
	if before.Replicas > 0 {
		livePolicy.Replicas = before.Replicas
	}
	if p.MaxConsumers != 0 && before.MaxConsumers >= p.MaxConsumers {
		livePolicy.MaxConsumers = before.MaxConsumers
	}
	if diffs := livePolicy.Check(after); len(diffs) != 0 {
		return fmt.Errorf(subjectAppendDiffsErrFmt, ErrSubjectAppendRefused, p.Name, strings.Join(diffs, policyDiffSeparator))
	}
	return nil
}

// AppendMissingSubjects returns before with any subjects from p.Subjects that
// before lacks appended in policy order, plus the list of added subjects. It
// refuses via CheckSubjectAppend if before or the resulting config differs on
// any field other than added policy subjects.
func (p StreamPolicy) AppendMissingSubjects(before nats.StreamConfig) (nats.StreamConfig, []string, error) {
	if err := p.CheckSubjectAppend(before, before); err != nil {
		return nats.StreamConfig{}, nil, err
	}
	var added []string
	for _, subject := range p.Subjects {
		if !slices.Contains(before.Subjects, subject) {
			added = append(added, subject)
		}
	}
	after := before
	after.Subjects = append(slices.Clone(before.Subjects), added...)
	if err := p.CheckSubjectAppend(before, after); err != nil {
		return nats.StreamConfig{}, nil, err
	}
	return after, added, nil
}

// AppendMissingSubjects reads policy.Name from the bus, appends any missing
// subjects from policy.Subjects, and updates the stream only when subjects
// were added and CheckSubjectAppend passes before and after the update.
func AppendMissingSubjects(ctx context.Context, nc *nats.Conn, policy StreamPolicy) ([]string, error) {
	info, err := streamInfo(ctx, nc, policy.Name)
	if err != nil {
		return nil, fmt.Errorf(subjectAppendStreamErrFmt, policy.Name, err)
	}
	after, added, err := policy.AppendMissingSubjects(info.Config)
	if err != nil {
		return nil, err
	}
	if len(added) == 0 {
		return nil, nil
	}
	if err := updateStream(ctx, nc, policy.Name, after); err != nil {
		return nil, fmt.Errorf(subjectAppendStreamErrFmt, policy.Name, err)
	}
	updated, err := streamInfo(ctx, nc, policy.Name)
	if err != nil {
		return nil, fmt.Errorf(subjectAppendStreamErrFmt, policy.Name, err)
	}
	if err := policy.CheckSubjectAppend(info.Config, updated.Config); err != nil {
		return nil, err
	}
	if missing := policy.MissingSubjects(updated.Config); len(missing) != 0 {
		return nil, fmt.Errorf(subjectAppendMissingErrFmt, ErrSubjectAppendRefused, strings.Join(missing, policyDiffSeparator))
	}
	return added, nil
}

// withoutPolicyFields is cfg with every field a StreamPolicy decides cleared,
// so what is left is the config outside the policy.
func withoutPolicyFields(cfg nats.StreamConfig) nats.StreamConfig {
	cfg.Name, cfg.Subjects = "", nil
	cfg.Retention, cfg.Storage = 0, 0
	cfg.MaxBytes, cfg.MaxAge, cfg.Discard, cfg.MaxMsgsPerSubject = 0, 0, 0, 0
	cfg.AllowRollup, cfg.DenyDelete, cfg.DenyPurge, cfg.AllowDirect = false, false, false, false
	cfg.Replicas = 0
	cfg.Sources, cfg.Mirror, cfg.RePublish = nil, nil, nil
	return cfg
}

// freshStreamsJSON is every provisioned stream's config as a fresh install
// creates it: the provision script's lines, run by the nats CLI the
// provision image ships, against the bus image's server, then read back
// with STREAM.INFO. See fresh_streams.json's provenance field for the
// versions and how to regenerate it.
//
//go:embed fresh_streams.json
var freshStreamsJSON []byte

// FreshStreams is the decoded fresh-install reference.
type FreshStreams struct {
	Provenance string                       `json:"provenance"`
	Streams    map[string]nats.StreamConfig `json:"streams"`
}

// LoadFreshStreams decodes the embedded fresh-install reference.
func LoadFreshStreams() (FreshStreams, error) {
	var f FreshStreams
	if err := json.Unmarshal(freshStreamsJSON, &f); err != nil {
		return FreshStreams{}, fmt.Errorf("decoding the fresh-install reference: %w", err)
	}
	return f, nil
}

// OutsidePolicyDrift lists how cfg differs from a fresh install of the same
// stream on the fields no policy decides. These are warnings, not refusals:
// the restore goes ahead and the operator reports each one, so nothing
// outside the policy is accepted silently. A stream the reference does not
// know is itself a warning.
func (f FreshStreams) OutsidePolicyDrift(cfg nats.StreamConfig) []string {
	ref, ok := f.Streams[cfg.Name]
	if !ok {
		return []string{fmt.Sprintf("stream %s: no fresh-install reference to compare with", cfg.Name)}
	}
	got, want := withoutPolicyFields(cfg), withoutPolicyFields(ref)
	if reflect.DeepEqual(got, want) {
		return nil
	}
	var g, w map[string]any
	gb, _ := json.Marshal(got)
	wb, _ := json.Marshal(want)
	_ = json.Unmarshal(gb, &g)
	_ = json.Unmarshal(wb, &w)
	keys := map[string]bool{}
	for k := range g {
		keys[k] = true
	}
	for k := range w {
		keys[k] = true
	}
	var drift []string
	for _, k := range slices.Sorted(maps.Keys(keys)) {
		if !reflect.DeepEqual(g[k], w[k]) {
			drift = append(drift, fmt.Sprintf("stream %s: %s is %v; a fresh install has %v", cfg.Name, k, g[k], w[k]))
		}
	}
	return drift
}
