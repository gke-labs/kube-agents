/*
Copyright 2026.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package controller

// The A2A gateway's report reader (#5618).
//
// The gateway's relay reads finished fleet-audit reports through the
// credential broker's /v1/audit-reports routes, under a broker role of its
// own, `a2a-reports`: GET one report (latest, or one run) of any audit, on
// the install's publish key alone, and no other route
// (credential_proxy.report_a2a_reports_read_permitted). What this file
// renders, wherever the A2A gateway renders, which is every install:
//
//   - on the gateway pod, a projected token bound to
//     credentialProxyA2AReportsAudience, and the two variables naming the
//     broker and the token for the relay. Not gated on Google Chat: the
//     a2a-chat token is, and the reader is needed without it;
//   - on the broker, the audience and the gateway's ServiceAccount bound to
//     it both ways (CREDENTIAL_PROXY_A2A_REPORTS_CALLERS): only that account
//     may present the audience, and that account may present only this
//     audience or the a2a-chat one. The account joins
//     CREDENTIAL_PROXY_ALLOWED_CALLERS and the broker's ingress fence on every
//     install for the same reason (allowedBrokerCallers,
//     buildCredentialProxyNetworkPolicy).
//
// The exec tier drops the audience (credentialProxyExecTierEnvDropped), so an
// exec pod confers no a2a-reports role, and keeps the callers binding, so the
// gateway's account reaches no other role there either.

import (
	"path"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/utils/ptr"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

const (
	// credentialProxyA2AReportsAudience is the report reader's audience.
	// Spelled identically in credential_proxy.py, whose default it is.
	credentialProxyA2AReportsAudience = "kubeagents-credential-proxy-a2a-reports" // #nosec G101 -- Token audience name, not a credential
	// credentialProxyA2AReportsAudienceEnvVar names it to the broker.
	credentialProxyA2AReportsAudienceEnvVar = "CREDENTIAL_PROXY_A2A_REPORTS_AUDIENCE" // #nosec G101 -- Environment variable name, not a credential
	// credentialProxyA2AReportsCallersEnvVar binds the role to the gateway's
	// ServiceAccount, as CREDENTIAL_PROXY_MCP_CALLERS binds the mcp role:
	// unset confers the role to nobody.
	credentialProxyA2AReportsCallersEnvVar = "CREDENTIAL_PROXY_A2A_REPORTS_CALLERS" // #nosec G101 -- Environment variable name, not a credential

	// a2aReportStoreURLEnvVar and a2aReportStoreTokenFileEnvVar tell the
	// gateway's relay where the broker is and which token to present.
	a2aReportStoreURLEnvVar       = "A2A_REPORT_STORE_URL"
	a2aReportStoreTokenFileEnvVar = "A2A_REPORT_STORE_TOKEN_FILE" // #nosec G101 -- Env var name, not a credential

	a2aReportsTokenVolume   = "a2a-reports-token"            // #nosec G101 -- Volume name, not a credential
	a2aReportsTokenMountDir = "/var/run/secrets/a2a-reports" // #nosec G101 -- Mount path, not a credential
	a2aReportsTokenKey      = "token"                        // #nosec G101 -- Projected file name, not a credential
	// One hour, like every broker token.
	a2aReportsTokenTTLSeconds = 3600
)

// a2aReportsEnvNames are reserved in mergeCredentialProxyEnv: one that could
// set the audience or the callers would decide who holds the a2a-reports
// role, and the gateway's pair would point the reader's token elsewhere.
var a2aReportsEnvNames = []string{
	credentialProxyA2AReportsAudienceEnvVar,
	credentialProxyA2AReportsCallersEnvVar,
	a2aReportStoreURLEnvVar,
	a2aReportStoreTokenFileEnvVar,
}

// a2aReportsBrokerEnv is the broker's half: the audience and its binding.
func a2aReportsBrokerEnv(agent *agentv1alpha1.PlatformAgent) []corev1.EnvVar {
	return []corev1.EnvVar{
		{Name: credentialProxyA2AReportsAudienceEnvVar, Value: credentialProxyA2AReportsAudience},
		{Name: credentialProxyA2AReportsCallersEnvVar, Value: a2aGatewayBrokerCaller(agent)},
	}
}

// a2aReportsGatewayAccess is the gateway's half: the projected token's
// volume, its mount, and the two variables naming the broker and the token.
func a2aReportsGatewayAccess(agent *agentv1alpha1.PlatformAgent) ([]corev1.Volume, []corev1.VolumeMount, []corev1.EnvVar) {
	volumes := []corev1.Volume{{
		Name: a2aReportsTokenVolume,
		VolumeSource: corev1.VolumeSource{Projected: &corev1.ProjectedVolumeSource{
			DefaultMode: ptr.To(int32(0400)),
			Sources: []corev1.VolumeProjection{{ServiceAccountToken: &corev1.ServiceAccountTokenProjection{
				Audience:          credentialProxyA2AReportsAudience,
				ExpirationSeconds: ptr.To(int64(a2aReportsTokenTTLSeconds)),
				Path:              a2aReportsTokenKey,
			}}},
		}},
	}}
	mounts := []corev1.VolumeMount{{Name: a2aReportsTokenVolume, MountPath: a2aReportsTokenMountDir, ReadOnly: true}}
	env := []corev1.EnvVar{
		{Name: a2aReportStoreURLEnvVar, Value: credentialProxyBaseURL(agent)},
		{Name: a2aReportStoreTokenFileEnvVar, Value: path.Join(a2aReportsTokenMountDir, a2aReportsTokenKey)},
	}
	return volumes, mounts, env
}
