package controller

import (
	"strings"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The home-channel record: under mode next the operator owns an empty
// ConfigMap beside the A2A gateway, which a gateway that learns the Slack
// home channel from the first admitted mention in a channel may read and
// patch (spec.integration.slack.homeChannelPolicy auto). The gateway's learn
// (a2a/gateway/slack_home_channel.go) is the caller: it reads and patches the
// record only under an A2A_HOME_CHANNEL_LEARN this operator does not render,
// and the operator reads nothing from it, so homeChannel alone decides what
// the agent and the HomeChannelUnset condition say. The admission that trusts
// the record and the switch that arms the learn ship in one change, so no
// release holds a signal nothing answers. The gateway writes the data keys with a merge patch;
// the operator applies the object with no data, so server-side apply never
// resets a key it did not set.
const (
	// a2aHomeChannelRecordSuffix names the ConfigMap after the agent.
	a2aHomeChannelRecordSuffix = "-home-channel"

	// slackHomeChannelPolicyAuto and slackHomeChannelPolicyManual are the
	// values of spec.integration.slack.homeChannelPolicy; absent reads as auto.
	slackHomeChannelPolicyAuto   = "auto"
	slackHomeChannelPolicyManual = "manual"

	// The gateway's view, rendered into its Deployment: whether it may learn,
	// which ConfigMap it writes, and its own image reference for the record
	// (a container cannot read its own image from the environment otherwise).
	a2aSlackHomePolicyEnvVar   = "A2A_SLACK_HOME_POLICY"
	a2aHomeChannelRecordEnvVar = "A2A_HOME_CHANNEL_RECORD"
	a2aGatewaySelfImageEnvVar  = "A2A_SELF_IMAGE"
)

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
