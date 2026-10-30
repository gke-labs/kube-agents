package controller

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/nats-io/nats.go/jetstream"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The profiles' shared durables on PROFILE_SUBMITS (#3660, "Admission and
// holding"; contract on #3655): one per (profile, tier, kind), created and
// owned by the operator, read only by the dispatcher. The stream is the
// profiles' own so that the operator is the only principal that can make or
// edit a consumer on it (a2aProfileSubmitStream). This is lib.EnsureHeldConsumer's
// contract restated here, because this module can't import that one: a
// durable made here must pass SubscribeHeld's checks on the stored config
// (the held-durable fixture test holds that). SubscribeHeld's intake check
// still takes the .in class on this tree; the dispatcher's PR moves it to
// the submit shape these durables filter on.
const (
	// heldMarkerKey/Value are lib's: the metadata SubscribeHeld requires,
	// so the dispatcher binds only a durable made for held delivery, and
	// the mark this operator deletes by, so it never removes a consumer it
	// didn't make.
	a2aHeldMarkerKey   = "kube-agents.held-delivery"
	a2aHeldMarkerValue = "v1"

	// a2aHeldAckWait is lib.DefaultHeldAckWait: the redelivery window the
	// dispatcher's in-progress acks keep open. Fixed once a durable exists;
	// changing it means re-creating the durable (see ensureDurable).
	a2aHeldAckWait = 2 * time.Minute
)

// a2aAllProfileTiers are every tier a profile can allow, so a dropped tier's
// durables can be found by name without listing consumers.
var a2aAllProfileTiers = []string{agentv1alpha1.AgentProfileTierRead, agentv1alpha1.AgentProfileTierUnattended}

// profileDurable is one durable as the operator wants it.
type profileDurable struct {
	name          string
	filter        string
	maxAckPending int
}

// profileDurableFilter is lib.ProfileDurableFilter.
func profileDurableFilter(profile, tier, kind string) string {
	return fmt.Sprintf("%s%s.%s.%s.*", a2aProfileSubmitPrefix, profile, tier, kind)
}

// desiredProfileDurables are the durables a profile with a rendered identity
// has: one per (allowed tier, kind), each capped at the profile's
// concurrency. The child durable has the same cap as the top one but its
// own, so children never queue behind their parents' kind.
func desiredProfileDurables(p *agentv1alpha1.AgentProfile) []profileDurable {
	var out []profileDurable
	for _, tier := range p.AllowedTierList() {
		for _, kind := range a2aSubmitKinds {
			out = append(out, profileDurable{
				name:          profileDurableName(p.Name, tier, kind),
				filter:        profileDurableFilter(p.Name, tier, kind),
				maxAckPending: int(p.Spec.Concurrency),
			})
		}
	}
	return out
}

// allProfileDurableNames is every durable name a profile could have.
func allProfileDurableNames(profile string) []string {
	var out []string
	for _, tier := range a2aAllProfileTiers {
		for _, kind := range a2aSubmitKinds {
			out = append(out, profileDurableName(profile, tier, kind))
		}
	}
	return out
}

// ensureDurable creates the durable, or updates an existing one's
// MaxAckPending, as lib.EnsureHeldConsumer does: only a durable known not to
// exist is created, and an existing one's filter and AckWait are never moved
// under a bound dispatcher, which checked the filter and took its heartbeat
// pace from AckWait when it bound. A consumer under the name that doesn't
// carry the held marker isn't this operator's, and is refused.
func ensureDurable(ctx context.Context, js jetstream.JetStream, d profileDurable) error {
	existing, err := js.Consumer(ctx, a2aProfileSubmitStream, d.name)
	switch {
	case errors.Is(err, jetstream.ErrConsumerNotFound):
	case err != nil:
		return fmt.Errorf("durable %s: look it up: %w", d.name, err)
	default:
		old := existing.CachedInfo().Config
		switch {
		case old.Metadata[a2aHeldMarkerKey] != a2aHeldMarkerValue:
			return fmt.Errorf("a consumer named %s exists on %s without the held-delivery mark; the operator doesn't adopt it", d.name, a2aProfileSubmitStream)
		case old.FilterSubject != d.filter || len(old.FilterSubjects) > 0:
			return fmt.Errorf("durable %s filters %q; refusing to move it to %q under a bound dispatcher (re-create it)", d.name, old.FilterSubject, d.filter)
		case old.AckWait != a2aHeldAckWait:
			return fmt.Errorf("durable %s has AckWait %v; refusing to change it to %v under a bound dispatcher (re-create it)", d.name, old.AckWait, a2aHeldAckWait)
		case heldConfigTampered(old) != "":
			// The mark is a public constant, so it proves nothing on its
			// own: anything this operator would have left at its default
			// and that is set says someone else made the consumer.
			return fmt.Errorf("a consumer named %s exists on %s that %s, which the operator never sets; it doesn't adopt it", d.name, a2aProfileSubmitStream, heldConfigTampered(old))
		case old.MaxAckPending == d.maxAckPending:
			return nil
		}
	}
	_, err = js.CreateOrUpdateConsumer(ctx, a2aProfileSubmitStream, jetstream.ConsumerConfig{
		Durable:       d.name,
		FilterSubject: d.filter,
		AckPolicy:     jetstream.AckExplicitPolicy,
		MaxAckPending: d.maxAckPending,
		AckWait:       a2aHeldAckWait,
		Metadata:      map[string]string{a2aHeldMarkerKey: a2aHeldMarkerValue},
	})
	if errors.Is(err, jetstream.ErrMaximumConsumersLimit) {
		return fmt.Errorf("durable %s: %s has no consumer slot left for it (%w); it holds %d durables, two per allowed tier for each of %d profiles", d.name, a2aProfileSubmitStream, err, a2aProfileDurablesAllowed, a2aProfileDurablesAllowed/4)
	}
	if err != nil {
		return fmt.Errorf("durable %s: %w", d.name, err)
	}
	return nil
}

// heldConfigTampered is lib's check of the same name: the first setting the
// operator leaves at its default that a stored consumer has changed, or "".
// The dispatcher refuses to bind such a durable, and the operator refuses to
// adopt one (lib.heldConfigTampered holds the list; the durable-config
// fixture test holds the two equal).
func heldConfigTampered(cc jetstream.ConsumerConfig) string {
	switch {
	case cc.DeliverSubject != "":
		return "pushes to " + cc.DeliverSubject
	case cc.HeadersOnly:
		return "delivers headers only"
	case cc.InactiveThreshold != 0:
		return "is deleted after " + cc.InactiveThreshold.String() + " inactive"
	case cc.MaxDeliver > 0:
		return fmt.Sprintf("stops redelivering after %d deliveries", cc.MaxDeliver)
	case cc.DeliverPolicy != jetstream.DeliverAllPolicy:
		return "does not start at the first submission"
	case cc.ReplayPolicy != jetstream.ReplayInstantPolicy:
		return "replays at the original rate"
	case len(cc.BackOff) > 0:
		return "backs off redeliveries"
	case cc.RateLimit != 0:
		return "is rate limited"
	case cc.PauseUntil != nil && !cc.PauseUntil.IsZero():
		return "is paused until " + cc.PauseUntil.String()
	case cc.PriorityPolicy != jetstream.PriorityPolicyNone || len(cc.PriorityGroups) > 0:
		return "pins or overflows deliveries to a priority group"
	case cc.MaxRequestBatch != 0 || cc.MaxRequestExpires != 0 || cc.MaxRequestMaxBytes != 0:
		return "limits pull requests"
	case cc.MemoryStorage:
		return "keeps its state in memory, lost on a server restart"
	}
	return ""
}

// deleteDurable deletes the named durable if it exists and carries the held
// marker. Deleting it drops whatever submissions it held unacknowledged; a
// profile or tier that goes away takes its queue with it.
func deleteDurable(ctx context.Context, js jetstream.JetStream, name string) error {
	existing, err := js.Consumer(ctx, a2aProfileSubmitStream, name)
	switch {
	case errors.Is(err, jetstream.ErrConsumerNotFound):
		return nil
	case err != nil:
		return fmt.Errorf("durable %s: look it up: %w", name, err)
	}
	if existing.CachedInfo().Config.Metadata[a2aHeldMarkerKey] != a2aHeldMarkerValue {
		return nil
	}
	if err := js.DeleteConsumer(ctx, a2aProfileSubmitStream, name); err != nil && !errors.Is(err, jetstream.ErrConsumerNotFound) {
		return fmt.Errorf("deleting durable %s: %w", name, err)
	}
	return nil
}

// SyncDurables makes the profile's durables on the bus exactly want: each
// one created or its cap updated, and every other name the profile could
// have deleted if this operator made it. want empty deletes them all.
func (n *natsCardPublisher) SyncDurables(ctx context.Context, agent *agentv1alpha1.PlatformAgent, profile string, want []profileDurable) error {
	nc, err := n.conn(agent)
	if err != nil {
		return err
	}
	js, err := jetstream.New(nc)
	if err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(ctx, cardBusTimeout)
	defer cancel()
	return syncDurables(ctx, js, profile, want)
}

// syncDurables is SyncDurables on a JetStream handle.
func syncDurables(ctx context.Context, js jetstream.JetStream, profile string, want []profileDurable) error {
	keep := map[string]bool{}
	for _, d := range want {
		keep[d.name] = true
	}
	// Dropped names first: on a stream with no spare consumer slot, a tier
	// swap needs the old tier's slots before the new tier's durables fit.
	for _, name := range allProfileDurableNames(profile) {
		if keep[name] {
			continue
		}
		if err := deleteDurable(ctx, js, name); err != nil {
			return err
		}
	}
	for _, d := range want {
		if err := ensureDurable(ctx, js, d); err != nil {
			return err
		}
	}
	return nil
}
