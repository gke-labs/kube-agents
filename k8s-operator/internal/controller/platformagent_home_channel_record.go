package controller

import (
	"context"
	"fmt"
	"strings"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The home-channel record: under mode next with Slack on the A2A gateway and
// spec.integration.slack.homeChannel empty, the gateway may learn the home
// channel from the first admitted mention in a channel (slack.homeChannelPolicy
// auto, the default) and record it in this ConfigMap. The operator owns the
// object and renders every agent-side home-channel signal from "the CR, else
// the record", so the agent, the condition and the gateway say the same thing.
// The gateway writes the data keys with a merge patch; the operator applies the
// object with no data, so server-side apply never resets a key it did not set.
const (
	// a2aHomeChannelRecordSuffix names the ConfigMap after the agent.
	a2aHomeChannelRecordSuffix = "-home-channel"
	// homeChannelRecordSlackKey is the learned Slack channel id.
	homeChannelRecordSlackKey = "slack"
	// homeChannelRecordSlackSetByKey is the member id whose mention set it.
	homeChannelRecordSlackSetByKey = "slack.setBy"
	// homeChannelRecordSlackRefusedKey is why the last candidate was refused
	// (a Slack Connect channel, an archived one, one the app is not in).
	homeChannelRecordSlackRefusedKey = "slack.refused"
	// homeChannelRecordWriterKey is the gateway image that wrote the record.
	// A record written by another image than the one this operator renders
	// is stale (a gateway rollback) and arms nothing, so no post is lost
	// under a clear condition.
	homeChannelRecordWriterKey = "writer"

	// slackHomeChannelPolicyAuto and slackHomeChannelPolicyManual are the
	// values of spec.integration.slack.homeChannelPolicy; absent reads as auto.
	slackHomeChannelPolicyAuto   = "auto"
	slackHomeChannelPolicyManual = "manual"

	// The gateway's view of the policy and the record, rendered into its
	// Deployment: whether it may learn, which ConfigMap it writes, and its
	// own image reference for the writer key (a container cannot read its
	// own image from the environment otherwise).
	a2aSlackHomePolicyEnvVar     = "A2A_SLACK_HOME_POLICY"
	a2aHomeChannelRecordEnvVar   = "A2A_HOME_CHANNEL_RECORD"
	a2aGatewaySelfImageEnvVar    = "A2A_SELF_IMAGE"
	homeChannelUnsetManualSuffix = " homeChannelPolicy is manual, so the gateway does not learn one from a mention."
	// The condition's message is bounded (a status field), so no record value
	// is echoed verbatim: the learned id is named only when it is a channel
	// id, the writer is cut to homeChannelRecordWriterMax runes, and a refusal
	// is one of the fixed reasons below, mapped to a fixed sentence.
	homeChannelUnsetStaleFormat     = " A learned channel %s is recorded by gateway image %s, not the %s this operator renders, so it is not used until the gateway writes it again."
	homeChannelUnsetStaleNoWriter   = " A learned channel %s is recorded with no gateway image, so it is not used until the gateway writes it again."
	homeChannelUnsetMalformedSuffix = " The recorded channel is not a channel id (C... or G...), so it is not used."
	homeChannelUnsetRefusedFormat   = " The last mention that could have set it was refused: %s."
	homeChannelRecordWriterMax      = 160
	homeChannelRecordMalformed      = "<not a channel id>"
	// The refusal vocabulary the gateway writes into slack.refused.
	homeChannelRefusedShared    = "shared"
	homeChannelRefusedArchived  = "archived"
	homeChannelRefusedNotMember = "not-member"
	homeChannelRefusedDM        = "direct-message"
)

// homeChannelRefusedReasons maps the gateway's refusal vocabulary to the
// sentence the condition carries; any other value reads as an unknown reason.
var homeChannelRefusedReasons = map[string]string{
	homeChannelRefusedShared:    "the channel is shared with another workspace (Slack Connect)",
	homeChannelRefusedArchived:  "the channel is archived",
	homeChannelRefusedNotMember: "the app is not a member of the channel",
	homeChannelRefusedDM:        "the mention was in a direct message, which cannot be the home channel",
}

const homeChannelRefusedUnknown = "a reason this operator does not know"

// homeChannelRecord is what the operator read from the record ConfigMap.
type homeChannelRecord struct {
	// channel is the admitted learned channel id, "" when there is none,
	// the id is malformed, the policy is manual, or the writer is stale.
	channel string
	// recorded is the raw learned id, admitted or not.
	recorded string
	// writer is the image that wrote the record; stale says it is not the
	// one this operator renders for the gateway.
	writer string
	stale  bool
	// refused is the gateway's reason for refusing the last candidate.
	refused string
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

// homeChannelRecordWanted is whether the record exists: whenever the next
// stack renders, Slack or not, so the teardown list holds for every rendered
// pass and a Slack enabled later, or a policy flipped later, needs no new
// object. The record is read only where Slack is on the gateway.
func homeChannelRecordWanted(agent *agentv1alpha1.PlatformAgent) bool {
	return a2aStackRendering(agent)
}

// buildHomeChannelRecord is the record with no data: the gateway owns the
// keys, the operator the object.
func buildHomeChannelRecord(agent *agentv1alpha1.PlatformAgent) *corev1.ConfigMap {
	return &corev1.ConfigMap{
		TypeMeta:   metav1.TypeMeta{APIVersion: "v1", Kind: "ConfigMap"},
		ObjectMeta: metav1.ObjectMeta{Name: a2aHomeChannelRecordName(agent), Namespace: agent.Namespace, Labels: a2aLabels(agent, "gateway")},
	}
}

// readHomeChannelRecord interprets a record for this agent. gatewayImage is
// the image this operator renders for the gateway.
func readHomeChannelRecord(agent *agentv1alpha1.PlatformAgent, cm *corev1.ConfigMap, gatewayImage string) homeChannelRecord {
	var rec homeChannelRecord
	if cm == nil {
		return rec
	}
	raw := strings.TrimSpace(cm.Data[homeChannelRecordSlackKey])
	rec.writer = boundedRunes(strings.TrimSpace(cm.Data[homeChannelRecordWriterKey]), homeChannelRecordWriterMax)
	if reason := strings.TrimSpace(cm.Data[homeChannelRecordSlackRefusedKey]); reason != "" {
		rec.refused = homeChannelRefusedReasons[reason]
		if rec.refused == "" {
			rec.refused = homeChannelRefusedUnknown
		}
	}
	if raw == "" {
		return rec
	}
	rec.recorded = slackChannelID(raw)
	if rec.recorded == "" {
		rec.recorded = homeChannelRecordMalformed
		return rec
	}
	rec.stale = rec.writer != gatewayImage
	if slackHomeChannelPolicy(agent) == slackHomeChannelPolicyAuto && !rec.stale {
		rec.channel = rec.recorded
	}
	return rec
}

// boundedRunes is s cut to at most n runes.
func boundedRunes(s string, n int) string {
	r := []rune(s)
	if len(r) <= n {
		return s
	}
	return string(r[:n])
}

// homeChannelRecordFor reads the record from the cache, or an empty record
// when there is none to read.
func (r *PlatformAgentReconciler) homeChannelRecordFor(ctx context.Context, agent *agentv1alpha1.PlatformAgent) (homeChannelRecord, error) {
	if !homeChannelRecordWanted(agent) || !a2aSlackArmed(agent) {
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
	return readHomeChannelRecord(agent, &cm, a2aGatewayImage()), nil
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
