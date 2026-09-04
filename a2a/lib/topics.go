package lib

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"sort"
	"strings"

	"github.com/nats-io/nats.go/jetstream"
)

// The topic namespace, per the payload spec:
//
//	a2a.topics.agent.{agent}.{topic}   one owning agent writes, everyone reads
//	a2a.topics.shared.{topic}          shared state with a designated writer set
//
// Which topics exist is provisioned configuration, not something a publisher
// invents at runtime: the streams' configured subject lists are the registry,
// and write access is a connection-time grant on exactly these subjects.
const (
	TopicScopeAgent  = "agent"
	TopicScopeShared = "shared"

	topicPrefix = "a2a.topics."
)

// Retention classes. A publisher does not choose retention; the class the topic
// was provisioned into does.
const (
	// StreamTopicsState holds state-class topics: current answer plus short
	// history (max_msgs_per_subject 8, no age limit).
	StreamTopicsState = "TOPICS-STATE"
	// StreamTopicsJournal holds journal-class topics: append-only, ages out.
	StreamTopicsJournal = "TOPICS-JOURNAL"
)

// ErrTopicEmpty is returned by ReadTopicLatest for a provisioned topic that
// nobody has written yet. It is not an error in the transport sense - an empty
// topic is a legal state - so callers distinguish it from a failed read.
var ErrTopicEmpty = errors.New("topic has no entries")

// TopicAgentSubject is the subject for a topic owned by one agent.
func TopicAgentSubject(agent, topic string) string {
	return topicPrefix + TopicScopeAgent + "." + agent + "." + topic
}

// TopicSharedSubject is the subject for a shared topic.
func TopicSharedSubject(topic string) string {
	return topicPrefix + TopicScopeShared + "." + topic
}

// ParseTopicSubject splits a topic subject into its scope ("agent" or
// "shared"), owning agent (empty for shared), and topic token. ok is false for
// any other subject shape, including tokens that are not dot-free DNS-1123
// labels - a dotted token changes the subject's token count under every
// wildcard filter, so it is not a topic subject at all.
func ParseTopicSubject(subject string) (scope, agent, topic string, ok bool) {
	rest, found := strings.CutPrefix(subject, topicPrefix)
	if !found {
		return "", "", "", false
	}
	parts := strings.Split(rest, ".")
	switch {
	case len(parts) == 3 && parts[0] == TopicScopeAgent:
		if !validDNS1123Label(parts[1]) || !validDNS1123Label(parts[2]) {
			return "", "", "", false
		}
		return TopicScopeAgent, parts[1], parts[2], true
	case len(parts) == 2 && parts[0] == TopicScopeShared:
		if !validDNS1123Label(parts[1]) {
			return "", "", "", false
		}
		return TopicScopeShared, "", parts[1], true
	}
	return "", "", "", false
}

// checkTopicPublish enforces the topic-plane rules Publish applies to any
// a2a.topics.> subject: the subject is well formed, only topic-update rides
// there, and the Artifact's name is the topic the subject names. The last one
// is the topic analogue of assertion 4 - the payload spec says the artifact's
// name *is* the topic, so a disagreement is a protocol error at the source
// rather than a mislabelled entry a reader has to reconcile later.
func checkTopicPublish(subject string, env *Envelope) error {
	_, _, topic, ok := ParseTopicSubject(subject)
	if !ok {
		return &ProtocolError{Msg: fmt.Sprintf("malformed topic subject %q: want a2a.topics.agent.{agent}.{topic} or a2a.topics.shared.{topic} with dot-free DNS-1123 tokens", subject)}
	}
	if env.Kind != KindTopicUpdate {
		return &ProtocolError{Msg: fmt.Sprintf("kind %q on topic subject %q: topics carry topic-update only", env.Kind, subject)}
	}
	var a Artifact
	if err := json.Unmarshal(env.Payload, &a); err != nil {
		return &ProtocolError{Msg: fmt.Sprintf("kind topic-update: malformed payload: %v", err)}
	}
	if a.Name != topic {
		return &ProtocolError{Msg: fmt.Sprintf("artifact name %q disagrees with subject topic %q", a.Name, topic)}
	}
	return nil
}

// NewTopicArtifact builds the Artifact a topic-update carries: the topic name,
// an optional TextPart summary, and the structured state as a DataPart. data
// may be nil for a text-only journal entry.
func NewTopicArtifact(topic, summary string, data any) (Artifact, error) {
	if !validDNS1123Label(topic) {
		return Artifact{}, &ProtocolError{Msg: fmt.Sprintf("topic token %q is not a dot-free DNS-1123 label", topic)}
	}
	a := Artifact{Name: topic}
	if summary != "" {
		a.Parts = append(a.Parts, Part{Kind: "text", Text: summary})
	}
	if data != nil {
		raw, err := json.Marshal(data)
		if err != nil {
			return Artifact{}, fmt.Errorf("marshal topic data: %w", err)
		}
		a.Parts = append(a.Parts, Part{Kind: "data", Data: raw})
	}
	if len(a.Parts) == 0 {
		return Artifact{}, &ProtocolError{Msg: "topic update carries no parts"}
	}
	return a, nil
}

// PublishTopic writes one entry to a topic. taskID and contextID are the
// originating task's when the write happened in the course of one - which is
// the audit thread from a user's question to the standing state it changed -
// and empty for a write on the agent's own schedule.
func (c *Client) PublishTopic(ctx context.Context, subject string, from Party, taskID, contextID, correlationID string, a Artifact, opts ...EnvelopeOption) error {
	payload, err := json.Marshal(a)
	if err != nil {
		return fmt.Errorf("marshal artifact: %w", err)
	}
	env, err := NewTopicUpdateEnvelope(from, taskID, contextID, correlationID, payload, opts...)
	if err != nil {
		return err
	}
	return c.Publish(ctx, subject, env)
}

// ReadTopicLatest returns the newest entry on a state-class topic subject
// (assertion 17): a direct last-message-for-subject read, not a consumer and
// not a replay. A state topic keeps a short history, and a reader that had to
// drain it to find the current answer would pay for the history on every
// question.
func (c *Client) ReadTopicLatest(ctx context.Context, stream, subject string) (*Envelope, error) {
	_, js := c.conn()
	st, err := js.Stream(ctx, stream)
	if err != nil {
		return nil, fmt.Errorf("stream %s: %w", stream, err)
	}
	msg, err := st.GetLastMsgForSubject(ctx, subject)
	if err != nil {
		if errors.Is(err, jetstream.ErrMsgNotFound) {
			return nil, fmt.Errorf("%s: %w", subject, ErrTopicEmpty)
		}
		return nil, fmt.Errorf("read %s: %w", subject, err)
	}
	env, err := ParseEnvelope(msg.Data)
	if err != nil {
		return nil, err
	}
	return env, nil
}

// TopicEntry is one provisioned topic: where it lives and how it is retained.
type TopicEntry struct {
	Subject string
	Stream  string
	// Class is "state" or "journal", derived from the holding stream.
	Class string
	Scope string
	Agent string
	Topic string
}

// TopicRegistry reports the provisioned topics as the server holds them. The
// registry is the streams' own configured subject lists - topics are
// provisioned configuration, so the deployment that rendered those lists is
// the source of truth, and a client that kept its own copy would be one
// rollout behind. Subjects the stream config names as wildcards contribute
// whatever concrete subjects currently hold entries, so a wildcard-provisioned
// deployment still resolves the topics it actually has.
func (c *Client) TopicRegistry(ctx context.Context) ([]TopicEntry, error) {
	_, js := c.conn()
	seen := map[string]TopicEntry{}
	var streamErr error
	for _, s := range []struct{ name, class string }{
		{StreamTopicsState, "state"},
		{StreamTopicsJournal, "journal"},
	} {
		st, err := js.Stream(ctx, s.name)
		if err != nil {
			// A deployment may render only one of the two classes; note the
			// failure and keep going, so one missing stream does not make the
			// registry unreadable.
			streamErr = errors.Join(streamErr, fmt.Errorf("stream %s: %w", s.name, err))
			continue
		}
		info, err := st.Info(ctx, jetstream.WithSubjectFilter(topicPrefix+">"))
		if err != nil {
			streamErr = errors.Join(streamErr, fmt.Errorf("stream info %s: %w", s.name, err))
			continue
		}
		subjects := append([]string(nil), info.Config.Subjects...)
		for subj := range info.State.Subjects {
			subjects = append(subjects, subj)
		}
		for _, subj := range subjects {
			scope, agent, topic, ok := ParseTopicSubject(subj)
			if !ok {
				continue // a wildcard or a non-topic subject; not a topic itself
			}
			if _, dup := seen[subj]; dup {
				continue
			}
			seen[subj] = TopicEntry{
				Subject: subj, Stream: s.name, Class: s.class,
				Scope: scope, Agent: agent, Topic: topic,
			}
		}
	}
	if len(seen) == 0 && streamErr != nil {
		return nil, streamErr
	}
	out := make([]TopicEntry, 0, len(seen))
	for _, e := range seen {
		out = append(out, e)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Subject < out[j].Subject })
	return out, nil
}

// ResolveTopic finds the provisioned topic a name refers to. The name may be a
// full subject, a scope-qualified name ("agent.platform.upgrade-readiness",
// "shared.blueprint"), or a bare topic token. A bare token that names more
// than one provisioned topic is an error rather than a guess.
func ResolveTopic(registry []TopicEntry, name string) (TopicEntry, error) {
	var matches []TopicEntry
	for _, e := range registry {
		qualified := e.Scope + "." + e.Topic
		if e.Scope == TopicScopeAgent {
			qualified = e.Scope + "." + e.Agent + "." + e.Topic
		}
		if name == e.Subject || name == qualified || name == e.Topic {
			matches = append(matches, e)
		}
	}
	switch len(matches) {
	case 1:
		return matches[0], nil
	case 0:
		return TopicEntry{}, fmt.Errorf("no provisioned topic %q (known: %s)", name, topicNames(registry))
	default:
		return TopicEntry{}, fmt.Errorf("%q is ambiguous across %d provisioned topics; name one of: %s",
			name, len(matches), topicNames(matches))
	}
}

func topicNames(registry []TopicEntry) string {
	if len(registry) == 0 {
		return "none provisioned"
	}
	names := make([]string, 0, len(registry))
	for _, e := range registry {
		names = append(names, e.Subject)
	}
	return strings.Join(names, ", ")
}
