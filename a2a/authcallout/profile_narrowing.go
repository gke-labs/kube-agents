package authcallout

import (
	"fmt"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// Profile narrowing: the map entry for an A2A AgentProfile's pods.
//
// "Profile" here is the AgentProfile custom resource, not a Hermes profile
// directory. The operator renders one entry per AgentProfile, keyed on the
// ServiceAccount that profile's pods run as, and every pod of the profile
// connects through it.
//
// A static entry cannot express what those pods need. They all publish as the
// profile — the task subjects are a2a.tasks.<profile>.… — but a profile with
// concurrency above one runs several pods at once, and each needs its own
// three consumers on TASKS and its own reply inbox. An exact consumer name in
// a static entry is shared by every pod of the profile, so the second pod's
// CreateOrUpdateConsumer rewrites the first one's; a wildcard name lets any
// pod INFO, pull from or DELETE every consumer on TASKS, the gateway's durable
// and every session's included. So the consumer names and the inbox come from
// the attested pod name, as for a session, and the subjects from the profile
// the entry names.
//
// What the map carries for such an entry is the profile name and the profile's
// topic grants, written the way the AgentProfile writes them (shared.{topic},
// agent.{agent}.{topic}), and still no grants. The callout composes the
// subjects. An edit to the map can therefore name a different profile or
// different topics, which is what the operator renders anyway, but it cannot
// put an arbitrary subject in front of a profile pod: the topic grammar below
// refuses anything that is not a topic.
//
// Deliberately absent, as for sessions: STREAM.INFO and message get on TASKS
// (neither is subject-scoped), $JS.ACK and $JS.FC (the adapter's consumers are
// ack-none pulls), and any subscribe on the task subjects. Also absent, and
// different from a session: the capability verify and reply subjects. A
// session's caller identity at the verifier is its pod name, which is also its
// addressee; a profile pod's addressee is the profile, and which name a profile
// pod asks the verifier in is the dispatcher's design to settle. A grant for a
// question nobody has decided how to ask would be a standing authorization.

// TopicGrants are an AgentProfile's blackboard grants as the profile spells
// them, without the a2a.topics. prefix.
type TopicGrants struct {
	Publish   []string `json:"publish,omitempty"`
	Subscribe []string `json:"subscribe,omitempty"`
}

// topicStreams are the two streams a topic can live in. A reader cannot tell
// from the topic which one holds it, so a read grant names both, each scoped
// to the one subject.
var topicStreams = []string{lib.StreamTopicsState, lib.StreamTopicsJournal}

// topicPrefix is how a profile's topic grant becomes a subject.
const topicPrefix = "a2a.topics."

// profileGrants is what one pod of a profile may do on the bus: the executor
// task plane for the profile's addressee with the pod's own consumers and
// inbox, plus the profile's topics.
//
// A topic the profile publishes is a plain publish; the JetStream ack comes
// back on the pod's inbox. A topic it subscribes to gets the core subscribe and
// the two reads lib.ReadTopicLatest makes: STREAM.INFO on each topic stream
// (the js.Stream handle) and DIRECT.GET scoped to the topic's own subject.
// STREAM.INFO on a topic stream is the one grant here wider than its topic: an
// info call with a subjects filter lists the topic names the stream holds.
// Names, not contents, and the platform agent's own grant has the same reach.
func profileGrants(profile, pod string, topics TopicGrants) (Grants, error) {
	g := executorGrants(profile, pod)
	for _, t := range topics.Publish {
		subject, err := topicSubject(t)
		if err != nil {
			return Grants{}, err
		}
		g.Publish = append(g.Publish, subject)
	}
	if len(topics.Subscribe) > 0 {
		for _, stream := range topicStreams {
			g.Publish = append(g.Publish, "$JS.API.STREAM.INFO."+stream)
		}
	}
	for _, t := range topics.Subscribe {
		subject, err := topicSubject(t)
		if err != nil {
			return Grants{}, err
		}
		g.Subscribe = append(g.Subscribe, subject)
		for _, stream := range topicStreams {
			g.Publish = append(g.Publish, "$JS.API.DIRECT.GET."+stream+"."+subject)
		}
	}
	g.Publish = append(g.Publish, "_INBOX."+pod+".>")
	return g, nil
}

// topicSubject turns a profile's topic grant into its subject, refusing
// anything lib.ParseTopicSubject would not accept as a topic. This is what
// stops a map entry from smuggling a subject in through the topics field.
func topicSubject(grant string) (string, error) {
	subject := topicPrefix + grant
	if _, _, _, ok := lib.ParseTopicSubject(subject); !ok {
		return "", fmt.Errorf("topic grant %q is not shared.{topic} or agent.{agent}.{topic} with dot-free DNS-1123 tokens", grant)
	}
	return subject, nil
}

// validateProfileEntry is the map-time check for a NarrowingProfile entry. The
// mint repeats the topic check, so a map that got past this function by some
// other route still cannot mint a non-topic subject.
func validateProfileEntry(id Identity) error {
	if !lib.ValidSubjectToken(id.Profile) {
		return fmt.Errorf("user %q narrows on %q but names profile %q, which is not a dot-free DNS-1123 label", id.User, id.Narrowing, id.Profile)
	}
	if id.Topics == nil {
		return nil
	}
	for _, t := range append(append([]string{}, id.Topics.Publish...), id.Topics.Subscribe...) {
		if _, err := topicSubject(t); err != nil {
			return fmt.Errorf("user %q: %w", id.User, err)
		}
	}
	return nil
}
