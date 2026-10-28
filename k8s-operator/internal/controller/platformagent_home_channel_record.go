package controller

import (
	"context"
	"fmt"
	"strings"
	"time"
	"unicode"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The home-channel record: under mode next the operator owns an empty
// ConfigMap beside the A2A gateway. With Slack on the gateway,
// spec.integration.slack.homeChannel empty and homeChannelPolicy auto, the
// gateway learns the home channel from the first admitted mention in a
// channel (a2a/gateway/slack_home_channel.go, armed by the
// A2A_HOME_CHANNEL_LEARN this operator renders) and patches it into the
// record. The operator reads the record and renders every agent-side
// home-channel signal from "the CR, else the admitted record", so the agent,
// the HomeChannelUnset condition and the gateway say the same thing. The
// gateway writes the data keys with a merge patch; the operator applies the
// object with no data, so server-side apply never resets a key it did not
// set.
const (
	// a2aHomeChannelRecordSuffix names the ConfigMap after the agent.
	a2aHomeChannelRecordSuffix = "-home-channel"

	// The record's data keys the operator reads. The gateway also writes
	// slack.setBy, slack.setAt and writer, for audit; nothing here reads them.
	homeChannelRecordSlackKey   = "slack"
	homeChannelRecordSetAtKey   = "slack.setAt"
	homeChannelRecordRefusedKey = "slack.refused"
	// homeChannelRecordReaderKey is the gateway image that last confirmed
	// the recorded channel, stamped at every learn and every boot adopt. A
	// record another image confirmed is stale: the gateway this operator
	// renders has not stood behind it yet, so it arms nothing and no post is
	// dropped under a clear condition. Compared in full; a floating tag such
	// as latest can name two images under one reference, which the compare
	// cannot tell.
	homeChannelRecordReaderKey = "reader"

	// slackHomeChannelPolicyAuto and slackHomeChannelPolicyManual are the
	// values of spec.integration.slack.homeChannelPolicy; absent reads as auto.
	slackHomeChannelPolicyAuto   = "auto"
	slackHomeChannelPolicyManual = "manual"

	// The gateway's view, rendered into its Deployment: whether it may learn,
	// which ConfigMap it writes, its own image reference for the record's
	// reader key (a container cannot read its own image from the environment
	// otherwise), and the switch that arms the learn at all. The switch is
	// true whenever Slack is on the gateway, which is also when this operator
	// reads the record; the gateway still learns only with the CR home empty
	// and the policy auto (Config.HomeLearnArmed).
	a2aSlackHomePolicyEnvVar    = "A2A_SLACK_HOME_POLICY"
	a2aHomeChannelRecordEnvVar  = "A2A_HOME_CHANNEL_RECORD"
	a2aGatewaySelfImageEnvVar   = "A2A_SELF_IMAGE"
	a2aHomeChannelLearnEnvVar   = "A2A_HOME_CHANNEL_LEARN"
	a2aHomeChannelLearnEnvValue = "true"

	// The condition's message is bounded (a status field), so no record value
	// is echoed verbatim: the learned id is named only when it is a channel id
	// within homeChannelRecordIDMax runes (Slack ids are about eleven, and the
	// grammar alone has no upper bound), the reader image is cut to
	// homeChannelRecordImageMax runes for display, and a refusal is one of the
	// fixed words below, mapped to a fixed sentence.
	homeChannelRecordIDMax     = 32
	homeChannelRecordImageMax  = 160
	homeChannelRecordMalformed = "<not a channel id>"

	// homeChannelAdmitDelay is how long after the learn (slack.setAt) an
	// otherwise admitted record reaches the agent. The channel reaches the
	// agent as pod env, so the admission replaces the agent pod, and the
	// conversation that learned the channel runs through the bridge in that
	// pod: admitted at once, the roll failed the user's own first message
	// ("bridge-shutdown", 2026-10-28). The delay lets that conversation
	// finish first; the reconcile requeues for the remainder. A record with
	// no readable timestamp was not written by a learn and applies at once.
	homeChannelAdmitDelay = 5 * time.Minute

	// The refusal vocabulary the gateway writes into slack.refused.
	homeChannelRefusedShared    = "shared"
	homeChannelRefusedArchived  = "archived"
	homeChannelRefusedNotMember = "not-member"
	homeChannelRefusedMalformed = "malformed"
	homeChannelRefusedUnknown   = "a reason this operator does not know"

	// The HomeChannelUnset sentences that follow the base message
	// (homeChannelUnsetMessageFormat) for Slack, at most one per message.
	homeChannelUnsetLearnSuffix            = " Or mention the agent in the channel you want: the gateway makes the channel of the first top-level mention after it started the home channel."
	homeChannelUnsetManualSuffix           = " homeChannelPolicy is manual, so the gateway does not learn one from a mention."
	homeChannelUnsetMalformedSuffix        = " The recorded channel is not a channel id (C... or G...), so it is not used."
	homeChannelUnsetRefusedRecordedFormat  = " The recorded channel %s was refused: %s, so it is not used; the next admitted mention in a channel replaces it."
	homeChannelUnsetRefusedCandidateFormat = " The last mention that could have set it was refused: %s."
	homeChannelUnsetPendingFormat          = " A learned channel %s is recorded and is applied to the agent at %s, %s after it was learned, so the conversation that learned it finishes before the agent restarts."
	homeChannelUnsetStaleFormat            = " A learned channel %s was last confirmed by gateway image %s, not the %s this operator renders, so it is not used until that gateway confirms it;" + homeChannelUnsetStaleExit
	homeChannelUnsetStaleNoReaderFormat    = " A learned channel %s is recorded with no confirming gateway image, so it is not used until the gateway confirms it;" + homeChannelUnsetStaleExit
	// homeChannelUnsetStaleExit names the one way out of a stale record: the
	// gateway checks and re-stamps the record when it starts, and nothing
	// retries a boot check that could not be made or a re-stamp that failed.
	homeChannelUnsetStaleExit = " the gateway checks the record when it starts (kubectl rollout restart deployment/%s -n %s)."
)

// homeChannelRefusedReasons maps the gateway's refusal vocabulary to the
// sentence the condition carries; any other value reads as an unknown reason.
var homeChannelRefusedReasons = map[string]string{
	homeChannelRefusedShared:    "the channel is shared with another workspace (Slack Connect)",
	homeChannelRefusedArchived:  "the channel is archived",
	homeChannelRefusedNotMember: "the app is not a member of the channel",
	homeChannelRefusedMalformed: "the recorded value is not a channel id",
}

// homeChannelRecord is what the operator read from the record ConfigMap.
type homeChannelRecord struct {
	// channel is the admitted learned channel id: "" when there is none, the
	// id is malformed, the policy is manual, the gateway refused it, or the
	// reader is stale.
	channel string
	// recorded is the raw learned id, admitted or not, or
	// homeChannelRecordMalformed when it is not a channel id.
	recorded string
	// reader is the image that last confirmed the record, compared in full;
	// stale says it is not the one this operator renders for the gateway.
	// readerShown is the copy the condition prints: printable runes only,
	// cut for display.
	reader      string
	readerShown string
	stale       bool
	// refused is the condition's sentence for the gateway's refusal, "" when
	// none is recorded.
	refused string
	// pending says the record is admissible but younger than
	// homeChannelAdmitDelay; appliesAt is when it is admitted and appliesIn
	// how long from the read that is, for the reconcile's requeue.
	pending   bool
	appliesAt time.Time
	appliesIn time.Duration
}

// slackHomeChannelPolicy is spec.integration.slack.homeChannelPolicy with
// absent read as auto.
func slackHomeChannelPolicy(agent *agentv1alpha1.PlatformAgent) string {
	if !slackEnabled(agent) {
		return slackHomeChannelPolicyAuto
	}
	if strings.TrimSpace(agent.Spec.Integration.Slack.HomeChannelPolicy) == slackHomeChannelPolicyManual {
		return slackHomeChannelPolicyManual
	}
	return slackHomeChannelPolicyAuto
}

// a2aHomeChannelRecordName is the record ConfigMap's name.
func a2aHomeChannelRecordName(agent *agentv1alpha1.PlatformAgent) string {
	return agent.Name + a2aHomeChannelRecordSuffix
}

// buildHomeChannelRecord is the record with no data: the gateway owns the
// keys, the operator the object.
func buildHomeChannelRecord(agent *agentv1alpha1.PlatformAgent) *corev1.ConfigMap {
	return &corev1.ConfigMap{
		TypeMeta:   metav1.TypeMeta{APIVersion: "v1", Kind: "ConfigMap"},
		ObjectMeta: metav1.ObjectMeta{Name: a2aHomeChannelRecordName(agent), Namespace: agent.Namespace, Labels: a2aLabels(agent, "gateway")},
	}
}

// readHomeChannelRecord interprets a record for this agent at time now.
// gatewayImage is the image this operator renders for the gateway. A channel
// is admitted only under policy auto, with no refusal recorded beside it,
// with the reader equal to gatewayImage, and once homeChannelAdmitDelay has
// passed since slack.setAt.
func readHomeChannelRecord(agent *agentv1alpha1.PlatformAgent, cm *corev1.ConfigMap, gatewayImage string, now time.Time) homeChannelRecord {
	var rec homeChannelRecord
	if cm == nil {
		return rec
	}
	raw := strings.TrimSpace(cm.Data[homeChannelRecordSlackKey])
	rec.reader = strings.TrimSpace(cm.Data[homeChannelRecordReaderKey])
	rec.readerShown = boundedRunes(rec.reader, homeChannelRecordImageMax)
	if reason := strings.TrimSpace(cm.Data[homeChannelRecordRefusedKey]); reason != "" {
		rec.refused = homeChannelRefusedReasons[reason]
		if rec.refused == "" {
			rec.refused = homeChannelRefusedUnknown
		}
	}
	if raw == "" {
		return rec
	}
	if len([]rune(raw)) <= homeChannelRecordIDMax {
		rec.recorded = slackChannelID(raw)
	}
	if rec.recorded == "" {
		rec.recorded = homeChannelRecordMalformed
		return rec
	}
	rec.stale = rec.reader != gatewayImage
	if slackHomeChannelPolicy(agent) != slackHomeChannelPolicyAuto || rec.stale || rec.refused != "" {
		return rec
	}
	if setAt, err := time.Parse(time.RFC3339, strings.TrimSpace(cm.Data[homeChannelRecordSetAtKey])); err == nil && now.Before(setAt.Add(homeChannelAdmitDelay)) {
		rec.pending = true
		rec.appliesAt = setAt.Add(homeChannelAdmitDelay)
		// A timestamp ahead of this clock (a skewed writer, a hand edit)
		// waits the delay from now, never longer.
		if cap := now.Add(homeChannelAdmitDelay); rec.appliesAt.After(cap) {
			rec.appliesAt = cap
		}
		rec.appliesIn = rec.appliesAt.Sub(now)
		return rec
	}
	rec.channel = rec.recorded
	return rec
}

// boundedRunes is s with its non-printable runes dropped (a hand-edited
// record can carry a newline or a terminal escape) and cut to at most n runes.
func boundedRunes(s string, n int) string {
	r := make([]rune, 0, n)
	for _, c := range s {
		if !unicode.IsPrint(c) {
			continue
		}
		if len(r) == n {
			break
		}
		r = append(r, c)
	}
	return string(r)
}

// homeChannelRecordFor reads the record from the cache, once per reconcile
// (the caller carries it to the render and to both condition writers), or an
// empty record where nothing reads it: a stack that is not rendered, or a
// gateway that does not hold Slack. NotFound is an empty record: the pass
// that creates the object has not run yet.
func (r *PlatformAgentReconciler) homeChannelRecordFor(ctx context.Context, agent *agentv1alpha1.PlatformAgent) (homeChannelRecord, error) {
	if !a2aStackRendering(agent) || !a2aSlackArmed(agent) {
		return homeChannelRecord{}, nil
	}
	var cm corev1.ConfigMap
	key := types.NamespacedName{Name: a2aHomeChannelRecordName(agent), Namespace: agent.Namespace}
	if err := r.Get(ctx, key, &cm); err != nil {
		if apierrors.IsNotFound(err) {
			return homeChannelRecord{}, nil
		}
		return homeChannelRecord{}, fmt.Errorf("read the home-channel record %s: %w", key.Name, err)
	}
	now := time.Now()
	if r.now != nil {
		now = r.now()
	}
	return readHomeChannelRecord(agent, &cm, a2aGatewayImage(), now), nil
}

// homeChannelRequeue is the reconcile's steady-state requeue, brought forward
// to the moment a pending record is admitted: nothing else wakes that pass,
// because the record does not change again after the learn.
func homeChannelRequeue(after time.Duration, rec homeChannelRecord) time.Duration {
	if rec.appliesIn > 0 {
		return min(after, rec.appliesIn)
	}
	return after
}

// a2aSlackHomeEffective is the Slack home channel the agent is told about:
// the CR's when it sets one, else the admitted record's. A CR value that is
// set but malformed falls through to nothing, not to the record: the gateway
// Deployment carries the raw CR value and refuses to arm on it, so arming the
// agent from the record would have it post into a route nothing answers.
func a2aSlackHomeEffective(agent *agentv1alpha1.PlatformAgent, rec homeChannelRecord) string {
	if !slackEnabled(agent) {
		return ""
	}
	if strings.TrimSpace(agent.Spec.Integration.Slack.HomeChannel) != "" {
		return a2aSlackHomeChannel(agent)
	}
	return rec.channel
}
