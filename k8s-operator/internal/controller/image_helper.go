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
	"fmt"
	"strings"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

const (
	defaultPlatformAgentImage = "ghcr.io/gke-labs/kube-agents/platform-agent:latest"
	defaultOperatorAgentImage = "ghcr.io/gke-labs/kube-agents/operator-agent:latest"
	defaultDevTeamAgentImage  = "ghcr.io/gke-labs/kube-agents/devteam-agent:latest"
)

// resolveAgentImage determines the full image reference using the optional deployment spec and a fallback default.
func resolveAgentImage(deployment *agentv1alpha1.DeploymentSpec, defaultImage string) string {
	image := defaultImage
	if deployment != nil && deployment.Image != "" {
		image = deployment.Image
		hasTagOrDigest := false
		lastSlash := strings.LastIndex(image, "/")
		refPart := image
		if lastSlash != -1 {
			refPart = image[lastSlash+1:]
		}
		if strings.Contains(refPart, ":") || strings.Contains(refPart, "@") {
			hasTagOrDigest = true
		}

		if !hasTagOrDigest {
			tag := "latest"
			if deployment.Tag != nil && *deployment.Tag != "" {
				tag = *deployment.Tag
			}
			image = fmt.Sprintf("%s:%s", image, tag)
		}
	}
	return image
}
