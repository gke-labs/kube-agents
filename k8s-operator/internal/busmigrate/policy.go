package busmigrate

import (
	_ "embed"
	"encoding/json"
	"fmt"
	"maps"
	"reflect"
	"slices"
	"sort"
	"time"

	"github.com/nats-io/nats.go"
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
}

// Check lists every field on which cfg differs from the policy, and names a
// source, mirror or republish, none of which a provisioned stream has.
// Empty means cfg matches. Subjects compare as a set.
func (p StreamPolicy) Check(cfg nats.StreamConfig) []string {
	var diffs []string
	field := func(name string, want, got any) {
		if !reflect.DeepEqual(want, got) {
			diffs = append(diffs, fmt.Sprintf("%s: want %v, got %v", name, want, got))
		}
	}
	field("name", p.Name, cfg.Name)
	field("subjects", sortedCopy(p.Subjects), sortedCopy(cfg.Subjects))
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

func sortedCopy(s []string) []string {
	c := slices.Clone(s)
	sort.Strings(c)
	if c == nil {
		c = []string{}
	}
	return c
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
