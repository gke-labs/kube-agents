// Package profiles carries the declarative agent profiles as config files and
// the strict loader that holds them to the spec.
//
// The design of record is a CRD - AgentProfile in the operator's
// kubeagents.x-k8s.io/v1alpha1 group, per
// docs/designs/spec-subagent-profiles.md. Stage 3 builds it. Until then the
// profiles are config files carrying the CRD's spec block verbatim, minus the
// CRD wrapping, so that promoting them is a move rather than a rewrite: the
// struct below is the schema the CRD will generate from, and the field set is
// asserted against the spec's field table by the tests.
//
// One mapping the files cannot carry: a CR's name is metadata.name, and a
// config file has no metadata. The file's base name is the profile name
// (chat.yaml is the chat profile), and Load returns it alongside the spec.
package profiles

import (
	"fmt"
	"path/filepath"
	"strings"

	"sigs.k8s.io/yaml"
)

// Spec is the AgentProfile spec block. Every field here appears in the spec's
// field table; nothing here does not (TestFieldsMatchTheSpecTable).
type Spec struct {
	// Description is the routing blurb, rendered into the A2A agent card and
	// the roster.
	Description string `json:"description"`
	// Persona is the OCI artifact holding SOUL.md, AGENTS.md, skills, and the
	// persona config, mounted read-only via image volume.
	Persona Persona `json:"persona"`
	// Harness is the worker image and its model routing.
	Harness Harness `json:"harness"`
	// Bus holds topic grants beyond the task subjects every executor gets.
	Bus Bus `json:"bus,omitempty"`
	// Identity names the KSA the pod runs as. Absent means the operator
	// creates one with zero RBAC bindings, which is the default posture.
	Identity Identity `json:"identity,omitempty"`
	// ClusterRef is set on cluster agents only; the operator renders the
	// scoped read-only kubeconfig from it.
	ClusterRef *ClusterRef `json:"clusterRef,omitempty"`
	// Lifecycle bounds one task's wall clock and how long the finished Job
	// lingers.
	Lifecycle Lifecycle `json:"lifecycle"`
	// QueueTimeoutSeconds is how stale a queued submission may be before the
	// dispatcher refuses to run it, judged by the server ingest timestamp.
	// Zero means the spec's default.
	QueueTimeoutSeconds int `json:"queueTimeoutSeconds,omitempty"`
	// Concurrency is the max simultaneous pods for this profile.
	Concurrency int `json:"concurrency"`
	// Resources is the pod resource class.
	Resources Resources `json:"resources"`
}

// DefaultQueueTimeoutSeconds is the spec's default for QueueTimeoutSeconds.
const DefaultQueueTimeoutSeconds = 3600

type Persona struct {
	Image string `json:"image"`
}

type Harness struct {
	Image    string `json:"image"`
	Model    string `json:"model,omitempty"`
	MaxTurns int    `json:"maxTurns,omitempty"`
}

type Bus struct {
	PublishTopics   []string `json:"publishTopics,omitempty"`
	SubscribeTopics []string `json:"subscribeTopics,omitempty"`
}

type Identity struct {
	ServiceAccountName string `json:"serviceAccountName,omitempty"`
}

type ClusterRef struct {
	ProjectID string `json:"projectId"`
	Cluster   string `json:"cluster"`
	Location  string `json:"location"`
}

type Lifecycle struct {
	ActiveDeadlineSeconds   int `json:"activeDeadlineSeconds"`
	TTLSecondsAfterFinished int `json:"ttlSecondsAfterFinished"`
}

type Resources struct {
	Requests ResourceList `json:"requests"`
	Limits   ResourceList `json:"limits"`
}

type ResourceList struct {
	CPU    string `json:"cpu"`
	Memory string `json:"memory"`
}

// Profile is a named spec: the file's base name plus what it holds.
type Profile struct {
	Name string
	Spec Spec
}

// Load parses one profile config file. Unknown fields are an error, not a
// shrug: a misspelled grant that parses is a grant that silently does not
// exist, and these files become CRs where the API server would reject it.
func Load(path string, data []byte) (*Profile, error) {
	name := strings.TrimSuffix(filepath.Base(path), filepath.Ext(path))
	var spec Spec
	if err := yaml.UnmarshalStrict(data, &spec); err != nil {
		return nil, fmt.Errorf("%s: %w", path, err)
	}
	p := &Profile{Name: name, Spec: spec}
	if err := p.Validate(); err != nil {
		return nil, fmt.Errorf("%s: %w", path, err)
	}
	return p, nil
}

// Validate checks what the field table says and what the subject grammar
// requires. It is the admission validation the CRD will get for free, run
// early so a profile that cannot be granted is caught at authoring time.
func (p *Profile) Validate() error {
	// The profile name is an addressee token in a2a.tasks.{addressee}.… and
	// the pod-name stem, so it lives under the same dot-free rule as every
	// other subject token.
	if !validToken(p.Name) {
		return fmt.Errorf("profile name %q is not a dot-free DNS-1123 label", p.Name)
	}
	s := &p.Spec
	if strings.TrimSpace(s.Description) == "" {
		return fmt.Errorf("description is required: it is the routing blurb the agent card is rendered from")
	}
	if s.Persona.Image == "" {
		return fmt.Errorf("persona.image is required")
	}
	if s.Harness.Image == "" {
		return fmt.Errorf("harness.image is required")
	}
	if s.Harness.MaxTurns < 0 {
		return fmt.Errorf("harness.maxTurns must not be negative")
	}
	for _, grant := range s.Bus.PublishTopics {
		if err := validTopicGrant(grant); err != nil {
			return fmt.Errorf("bus.publishTopics: %w", err)
		}
	}
	for _, grant := range s.Bus.SubscribeTopics {
		if err := validTopicGrant(grant); err != nil {
			return fmt.Errorf("bus.subscribeTopics: %w", err)
		}
	}
	if s.ClusterRef != nil {
		if s.ClusterRef.ProjectID == "" || s.ClusterRef.Cluster == "" || s.ClusterRef.Location == "" {
			return fmt.Errorf("clusterRef needs projectId, cluster, and location: the whole point is that cluster identity is structured data, not a parsed name")
		}
	}
	if s.Lifecycle.ActiveDeadlineSeconds <= 0 {
		return fmt.Errorf("lifecycle.activeDeadlineSeconds must be positive: a task with no ceiling is the orphan case with extra steps")
	}
	if s.Lifecycle.TTLSecondsAfterFinished < 0 {
		return fmt.Errorf("lifecycle.ttlSecondsAfterFinished must not be negative")
	}
	if s.QueueTimeoutSeconds < 0 {
		return fmt.Errorf("queueTimeoutSeconds must not be negative")
	}
	if s.Concurrency <= 0 {
		return fmt.Errorf("concurrency must be positive")
	}
	for label, r := range map[string]ResourceList{
		"resources.requests": s.Resources.Requests,
		"resources.limits":   s.Resources.Limits,
	} {
		if r.CPU == "" || r.Memory == "" {
			return fmt.Errorf("%s needs both cpu and memory", label)
		}
	}
	return nil
}

// QueueTimeout reports the effective queue deadline, applying the spec's
// default when the field is absent.
func (p *Profile) QueueTimeout() int {
	if p.Spec.QueueTimeoutSeconds == 0 {
		return DefaultQueueTimeoutSeconds
	}
	return p.Spec.QueueTimeoutSeconds
}

// validTopicGrant checks a topic grant's shape. Grants are written the way the
// spec's worked profiles write them - scope-qualified, without the
// a2a.topics. prefix the operator adds when it renders the grant into the
// identity-to-permissions map.
func validTopicGrant(grant string) error {
	parts := strings.Split(grant, ".")
	switch {
	case len(parts) == 2 && parts[0] == "shared":
	case len(parts) == 3 && parts[0] == "agent":
	default:
		return fmt.Errorf("grant %q: want shared.{topic} or agent.{agent}.{topic}", grant)
	}
	for _, tok := range parts[1:] {
		if !validToken(tok) {
			return fmt.Errorf("grant %q: token %q is not a dot-free DNS-1123 label", grant, tok)
		}
	}
	return nil
}

// validToken is the subject-token rule: a DNS-1123 label, which is dot-free by
// construction. Kept local rather than imported from a2a/lib - a profile file
// is validated by tooling that has no bus connection, and the rule is three
// lines.
func validToken(s string) bool {
	if len(s) == 0 || len(s) > 63 {
		return false
	}
	for i := 0; i < len(s); i++ {
		c := s[i]
		switch {
		case c >= 'a' && c <= 'z', c >= '0' && c <= '9':
		case c == '-':
			if i == 0 || i == len(s)-1 {
				return false
			}
		default:
			return false
		}
	}
	return true
}
