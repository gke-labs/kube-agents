package controller

import (
	"context"
	"fmt"
	"strings"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/utils/ptr"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The home-channel record: under mode next the operator owns an empty
// ConfigMap beside the A2A gateway. With Slack on the gateway,
// spec.integration.slack.homeChannel empty and homeChannelPolicy auto, the
// gateway learns the home channel from the first admitted mention in a
// channel (a2a/gateway/slack_home_channel.go, armed by the
// A2A_HOME_CHANNEL_LEARN this operator renders) and patches it into the
// record. The operator reads the record, admits it, and projects the home
// channel it stands behind, "the CR, else the admitted record", into a
// second ConfigMap it mounts as a directory in the agent pod and the
// gateway pod. Both read the file where it is mounted, and the kubelet
// refreshes it in place, so a learned channel reaches them with no pod
// replaced and no copy to drift: the agent, the HomeChannelUnset condition
// and the gateway follow one read. The gateway writes the record's data keys
// with a merge patch; the operator applies the record with no data, so
// server-side apply never resets a key it did not set.
const (
	// a2aHomeChannelRecordSuffix names the ConfigMap after the agent.
	a2aHomeChannelRecordSuffix = "-home-channel"

	// The record's data keys the operator reads: the channel and the
	// refusal. The gateway also writes slack.setBy, slack.setAt and writer,
	// for audit; nothing here reads them.
	homeChannelRecordSlackKey   = "slack"
	homeChannelRecordRefusedKey = "slack.refused"

	// The projection: the home channel the operator admitted, in a ConfigMap
	// it owns and mounts as a directory in the agent pod and the gateway pod.
	// One key, the Slack channel id or empty, always present, so a reader
	// never sees two keys torn across a kubelet refresh and a pod with the
	// mount always finds the file. Mounted without subPath, so the kubelet
	// refreshes it in place, and kept out of every pod-template hash, so a
	// learn rolls no pod.
	a2aHomeChannelProjectionSuffix  = "-chat-home"
	homeChannelProjectionKey        = "slack_home"
	homeChannelProjectionVolumeName = "chat-home"
	// World-readable: the agent container runs as its own user, not root.
	homeChannelProjectionFileMode int32 = 0444
	agentChatHomeDir                    = "/etc/kube-agents/chat-home"
	a2aGatewayChatHomeDir               = "/etc/a2a/chat-home"

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
	// grammar alone has no upper bound), and a refusal is one of the fixed
	// words below, mapped to a fixed sentence.
	homeChannelRecordIDMax     = 32
	homeChannelRecordMalformed = "<not a channel id>"

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
	// id is malformed, the policy is manual, or the gateway refused it.
	channel string
	// recorded is the raw learned id, admitted or not, or
	// homeChannelRecordMalformed when it is not a channel id.
	recorded string
	// refused is the condition's sentence for the gateway's refusal, "" when
	// none is recorded.
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

// buildHomeChannelRecord is the record with no data: the gateway owns the
// keys, the operator the object.
func buildHomeChannelRecord(agent *agentv1alpha1.PlatformAgent) *corev1.ConfigMap {
	return &corev1.ConfigMap{
		TypeMeta:   metav1.TypeMeta{APIVersion: "v1", Kind: "ConfigMap"},
		ObjectMeta: metav1.ObjectMeta{Name: a2aHomeChannelRecordName(agent), Namespace: agent.Namespace, Labels: a2aLabels(agent, "gateway")},
	}
}

// readHomeChannelRecord interprets a record for this agent. A channel is
// admitted only under policy auto and with no refusal recorded beside it;
// the gateway that posts into it reads the same projection, so nothing
// confirms the record beyond the gateway's own check at the learn.
func readHomeChannelRecord(agent *agentv1alpha1.PlatformAgent, cm *corev1.ConfigMap) homeChannelRecord {
	var rec homeChannelRecord
	if cm == nil {
		return rec
	}
	raw := strings.TrimSpace(cm.Data[homeChannelRecordSlackKey])
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
	if slackHomeChannelPolicy(agent) != slackHomeChannelPolicyAuto || rec.refused != "" {
		return rec
	}
	rec.channel = rec.recorded
	return rec
}

// a2aHomeChannelProjectionName is the projection ConfigMap's name.
func a2aHomeChannelProjectionName(agent *agentv1alpha1.PlatformAgent) string {
	return agent.Name + a2aHomeChannelProjectionSuffix
}

// buildHomeChannelProjection is the projection for this pass: the one key,
// the home channel the agent and the gateway are to use (a2aSlackHomeEffective)
// or empty. The operator owns the data too, so a hand edit of this object
// is rewritten on the next pass; the record is where a hand edit belongs.
func buildHomeChannelProjection(agent *agentv1alpha1.PlatformAgent, rec homeChannelRecord) *corev1.ConfigMap {
	return &corev1.ConfigMap{
		TypeMeta:   metav1.TypeMeta{APIVersion: "v1", Kind: "ConfigMap"},
		ObjectMeta: metav1.ObjectMeta{Name: a2aHomeChannelProjectionName(agent), Namespace: agent.Namespace, Labels: a2aLabels(agent, "gateway")},
		Data:       map[string]string{homeChannelProjectionKey: a2aSlackHomeEffective(agent, rec)},
	}
}

// homeChannelProjectionVolume mounts the projection as a directory: Optional,
// so a pod under an operator that has not written it yet (or a stack torn
// down) starts with an empty directory instead of waiting in
// ContainerCreating; no subPath, so the kubelet refreshes the file in place.
func homeChannelProjectionVolume(agent *agentv1alpha1.PlatformAgent) corev1.Volume {
	return corev1.Volume{
		Name: homeChannelProjectionVolumeName,
		VolumeSource: corev1.VolumeSource{
			ConfigMap: &corev1.ConfigMapVolumeSource{
				LocalObjectReference: corev1.LocalObjectReference{Name: a2aHomeChannelProjectionName(agent)},
				Optional:             ptr.To(true),
				DefaultMode:          ptr.To(homeChannelProjectionFileMode),
			},
		},
	}
}

// homeChannelProjectionMount is the read-only directory mount of the
// projection at dir, in the agent container and in the gateway's.
func homeChannelProjectionMount(dir string) corev1.VolumeMount {
	return corev1.VolumeMount{Name: homeChannelProjectionVolumeName, MountPath: dir, ReadOnly: true}
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
	return readHomeChannelRecord(agent, &cm), nil
}

// a2aSlackHomeEffective is the Slack home channel the projection carries,
// which the agent and the gateway read: the CR's when it sets one, else the
// admitted record's. A CR value that is set but malformed falls through to
// nothing, not to the record: the gateway refuses to arm on the raw CR value
// it carries, so a projection from the record would have the agent post into
// a route nothing answers.
func a2aSlackHomeEffective(agent *agentv1alpha1.PlatformAgent, rec homeChannelRecord) string {
	if !slackEnabled(agent) {
		return ""
	}
	if strings.TrimSpace(agent.Spec.Integration.Slack.HomeChannel) != "" {
		return a2aSlackHomeChannel(agent)
	}
	return rec.channel
}
