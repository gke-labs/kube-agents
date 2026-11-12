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

import (
	"testing"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/utils/ptr"
)

// Every pod that dials the broker or the kube-api proxy by its 4-dot service
// name sets ndots:3, so the name is asked as written first (#5590): the shell
// sandbox, the broker and its exec tier, and the profile workers, both the
// Job template and the warm pool. The DNS policy stays the default
// (ClusterFirst), so the cluster's nameservers and search list still apply.
func TestBrokerCallerPodsSetNdots3(t *testing.T) {
	a2a := a2aTestAgent()
	profile := unattendedProfile(a2a, "tse")
	execAgent := execTierTestAgent(ptr.To(int32(2)))
	pods := map[string]corev1.PodSpec{
		"shell sandbox": buildShellSandboxStatefulSet(shellSandboxTestAgent(), "sandbox-ssh", "http://broker:8080", "settings-hash").Spec.Template.Spec,
		"broker":        buildCredentialProxyDeployment(execAgent, "hash").Spec.Template.Spec,
		"exec tier":     buildCredentialProxyExecTierDeployment(execAgent, buildCredentialProxyDeployment(execAgent, "hash")).Spec.Template.Spec,
		"worker Job":    buildProfileJobTemplate(a2a, &profile, false).Spec,
		"warm pool":     buildProfilePool(a2a, &profile, "read", "agentprofile-tse-read", false).Spec.Template.Spec,
	}
	for name, spec := range pods {
		if spec.DNSPolicy != "" && spec.DNSPolicy != corev1.DNSClusterFirst {
			t.Errorf("%s: dnsPolicy %q, want the default ClusterFirst", name, spec.DNSPolicy)
		}
		if spec.DNSConfig == nil {
			t.Errorf("%s: no dnsConfig, so the resolver keeps ndots:5", name)
			continue
		}
		if len(spec.DNSConfig.Nameservers) != 0 || len(spec.DNSConfig.Searches) != 0 {
			t.Errorf("%s: dnsConfig adds nameservers %v or searches %v; it should change only ndots",
				name, spec.DNSConfig.Nameservers, spec.DNSConfig.Searches)
		}
		var ndots []string
		for _, o := range spec.DNSConfig.Options {
			if o.Name == "ndots" {
				ndots = append(ndots, ptr.Deref(o.Value, ""))
			}
		}
		if len(ndots) != 1 || ndots[0] != "3" {
			t.Errorf("%s: ndots options %v, want exactly one, 3", name, ndots)
		}
	}
}
