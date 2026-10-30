package authcallout

import (
	"fmt"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/taskcred"
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
// different from a session: the capability verify and reply subjects, and any
// fetch on the shared submission durables. #3660 settles both: the dispatcher
// is the only fetcher and holder of submissions and the one that asks the
// verifier, and a worker gets its task from the dispatcher.
//
// Per (profile, tier) since #3655: the operator renders one entry per tier
// a profile allows, so the tier is the entry's. What a worker does with it is
// the hand-off and the lease, both scoped to its own (profile, tier, pod):
// the dispatcher's request on lib.DispatchSubject, its answer on
// lib.DispatchAcceptSubject, and its lease renewals on
// lib.DispatchLeaseSubject. No grant on the lease bucket itself: a KV bucket
// allows rollup headers, so a put on any key would let a worker purge every
// lease, and the dispatcher is the bucket's only writer.
//
// Per task with #3889, staged. The task subjects carry no tier (#3660,
// "Subjects"), so a grant on the profile's .events and .in reaches every task
// of the profile at any tier. The fix is two kinds of connection. A task
// connection, opened after the worker accepts a task, presents the
// ServiceAccount token plus the task credential the dispatcher signed
// (a2a/taskcred), and gets that one task's executor grants and nothing else.
// The pod connection, presenting the token alone, gets the hand-off, the lease
// and the topics. It also keeps the profile-wide task reach it had before, for
// now: this PR adds the task connection, and the worker moves onto it with
// #3889's worker PR, which removes the pod connection's task reach
// (profileWorkerGrants). That removal must land before any tier stronger than
// Read goes live (#3659). See Service.authorize for how the two connections
// are told apart on CONNECT and what the credential must match.

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

// profileTiers are the tiers a profile entry may name, the AgentProfile
// CRD's allowedTiers values. Duplicated here because the modules cannot
// import each other, and kept equal by hand: the rendered fixture the
// contract test reads carries only read.
var profileTiers = map[string]bool{"read": true, "unattended": true}

// topicPrefix is how a profile's topic grant becomes a subject.
const topicPrefix = "a2a.topics."

// profileGrants is what one pod of a profile may do on its pod connection:
// the hand-off, the lease, the pod's inbox and, until #3889's worker PR, the
// profile-wide task plane (profileWorkerGrants), plus the profile's topics. A
// task connection gets one task's subjects instead (profileTaskGrants).
//
// Any profile with a topic gets STREAM.INFO on both topic streams, a writer as
// much as a reader: `a2a topics write` resolves the topic through
// Client.TopicRegistry, which opens a js.Stream handle on each stream before it
// publishes. A topic the profile publishes is then the publish itself (the
// JetStream ack comes back on the pod's inbox). A topic it subscribes to adds
// the core subscribe and DIRECT.GET scoped to the topic's own subject, which is
// lib.ReadTopicLatest's read. STREAM.INFO on a topic stream is the one grant
// here wider than its topic: an info call with a subjects filter lists the
// topic names the stream holds. Names, not contents, and the platform agent's
// own grant has the same reach.
//
// An agent-scoped topic the profile publishes must be its own,
// agent.<profile>.<topic>: the payload spec gives each agent namespace one
// writer. Another agent's topic, or a shared one, is fine to read.
func profileGrants(profile, tier, pod string, topics TopicGrants) (Grants, error) {
	g := profileWorkerGrants(profile, tier, pod)
	for _, t := range topics.Publish {
		subject, err := topicSubject(t)
		if err != nil {
			return Grants{}, err
		}
		if err := ownsAgentTopic(profile, t); err != nil {
			return Grants{}, err
		}
		g.Publish = append(g.Publish, subject)
	}
	if len(topics.Publish) > 0 || len(topics.Subscribe) > 0 {
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

// ownsAgentTopic refuses a publish grant on another agent's topic. Shared
// topics have no owner and pass.
func ownsAgentTopic(profile, grant string) error {
	_, agent, _, _ := lib.ParseTopicSubject(topicPrefix + grant)
	if agent != "" && agent != profile {
		return fmt.Errorf("profile %q may not publish agent.%s topic %q: an agent-scoped topic has one writer, the agent it names", profile, agent, grant)
	}
	return nil
}

// profileWorkerGrants is a worker pod's own connection: an executor's task
// plane over every task of the profile, the profile's .events publish and the
// pod's own three consumers (origin, follow-ups, and the .events reader the
// adapter uses to see whether a task already has a terminal before it runs
// it); then, with a tier, the hand-off and lease grants above. An entry from
// an operator before per-tier identities names no tier and gets none of the
// hand-off, so a pod under it can't take dispatched work.
//
// TODO(#3889): drop the executor grants here, leaving the inbox, the hand-off
// and the lease. The removal lands with the worker's per-task connection
// (#3889's worker PR), which moves the adapter onto profileTaskGrants, and
// must land before any tier stronger than Read goes live (#3659): until then
// a worker at any tier reaches every task of its profile on this connection.
// TestATokenlessWorkerKeepsProfileWideTaskReachUntilTheWorkerPR pins today's
// shape and flips with it.
func profileWorkerGrants(profile, tier, pod string) Grants {
	g := executorGrants(profile, pod)
	if tier == "" {
		return g
	}
	g.Subscribe = append(g.Subscribe, lib.DispatchSubject(profile, tier, pod))
	g.Publish = append(g.Publish,
		lib.DispatchAcceptSubject(profile, tier, pod),
		lib.DispatchLeaseSubject(profile, tier, pod),
	)
	return g
}

// profileTaskGrants is a worker's task connection: an executor's grants exact
// on one task of its profile, the events publish and the task's own three
// consumers (origin, follow-ups, and the events reader the adapter uses to see
// whether a task already has a terminal before it runs it), and the task's
// inbox, all named under the task's stem (below). No hand-off, lease or topic grant: the pod connection holds those.
// The task comes from a verified credential (Service.verifyTaskCredential) and
// the profile from the attested identity's map entry, never from the
// credential alone.
//
// The consumer names and inbox are the task's own, under
// taskcred.ConsumerStem(pod, task), not the pod's: a consumer's INFO, MSG.NEXT
// and DELETE carry only its name, so a pod-named consumer would be reachable
// from the next task's connection on the same warm pod.
func profileTaskGrants(profile, task, pod string) Grants {
	stem := taskcred.ConsumerStem(pod, task)
	g := executorTaskGrants(profile, task, stem)
	g.Publish = append(g.Publish, "_INBOX."+stem+".>")
	return g
}

// validateProfileEntry is the map-time check for a NarrowingProfile entry. The
// mint repeats the topic check, so a map that got past this function by some
// other route still cannot mint a non-topic subject.
func validateProfileEntry(id Identity) error {
	if !lib.ValidSubjectToken(id.Profile) {
		return fmt.Errorf("user %q narrows on %q but names profile %q, which is not a dot-free DNS-1123 label", id.User, id.Narrowing, id.Profile)
	}
	if id.Tier != "" && !profileTiers[id.Tier] {
		return fmt.Errorf("user %q narrows on %q but names tier %q, which is not one of the profile tiers", id.User, id.Narrowing, id.Tier)
	}
	if id.Topics == nil {
		return nil
	}
	for _, t := range append(append([]string{}, id.Topics.Publish...), id.Topics.Subscribe...) {
		if _, err := topicSubject(t); err != nil {
			return fmt.Errorf("user %q: %w", id.User, err)
		}
	}
	for _, t := range id.Topics.Publish {
		if err := ownsAgentTopic(id.Profile, t); err != nil {
			return fmt.Errorf("user %q: %w", id.User, err)
		}
	}
	return nil
}
