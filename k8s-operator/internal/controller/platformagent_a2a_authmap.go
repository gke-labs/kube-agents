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
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The identity-to-permissions map: what the operator renders and the auth
// callout serves.
//
// It is read through an API informer rather than a volume mount, which is why
// the object being a ConfigMap costs nothing. The deployment spec's reason for
// forbidding the mount is kubelet ConfigMap sync to a VOLUME, which lags up to
// a minute — long enough that a workload spawned seconds after its entry lands
// hits an Authorization Violation while holding a perfectly good token. An
// informer sees the write on the watch stream, so the lag is gone and the
// object stays the plain, diffable, non-secret thing it should be.
//
// It holds no secret. Under the callout a principal's credential is a token the
// cluster mints, so the map carries only ServiceAccount names and subject
// lists, and the passwords that remain live where they always did.

const (
	// a2aAuthMapKey is the ConfigMap key holding the rendered map. The
	// callout reads this key by name.
	a2aAuthMapKey = "identities.json"

	// a2aAuthMapVersionLength is how much of the content digest becomes the
	// version. Sixteen hex characters matches the config hash the NATS
	// StatefulSet already rides and is far past collision concerns for a
	// value whose whole job is "did this change".
	a2aAuthMapVersionLength = 16

	// a2aAuthMapVersionAnnotation carries the rendered version on the
	// ConfigMap.
	a2aAuthMapVersionAnnotation = "kubeagents.x-k8s.io/a2a-authmap-version"

	// a2aAuthMapVersionUnknown is what the condition reports when the
	// ConfigMap cannot be read or carries no version. A placeholder rather
	// than an empty string, because a message ending in "serving identity
	// map " reads as a truncated log line rather than as a thing that was
	// looked for and not found.
	a2aAuthMapVersionUnknown = "(unknown)"
)

// a2aAuthMapName is the ConfigMap holding the identity map.
func a2aAuthMapName(agent *agentv1alpha1.PlatformAgent) string {
	return agent.Name + "-a2a-authmap"
}

// The wire shape of the map. This mirrors the types in the a2a module's
// authcallout package, which is the consumer; the two modules cannot import
// each other, so the contract is the JSON and a shared fixture keeps the two
// sides honest (see TestRenderedAuthMapMatchesTheCalloutFixture).
//
// Field names and tags are the contract. Changing one here without changing it
// there produces a callout that refuses the map at startup — noisily, and
// before it serves anything, which is the failure mode to prefer.
type a2aAuthMapGrants struct {
	Publish   []string `json:"publish"`
	Subscribe []string `json:"subscribe"`
}

type a2aAuthMapIdentity struct {
	ServiceAccount string           `json:"serviceAccount"`
	User           string           `json:"user"`
	Account        string           `json:"account"`
	Grants         a2aAuthMapGrants `json:"grants"`
}

type a2aAuthMapDocument struct {
	Version    string               `json:"version"`
	Identities []a2aAuthMapIdentity `json:"identities"`
}

// renderA2AAuthMap builds the map document and its version.
//
// The version is a digest of the identities alone, computed before it is
// stamped in, so it names the content rather than itself. It is what
// BusCredentialsReady is asserted against: the operator knows which version it
// rendered, the callout reports which version it is serving, and the condition
// is the two agreeing. Without a version the ordering question — "is the
// callout serving the entry this workload is about to need?" — has no
// answerable form, and the alternative is the race the spec set out to remove.
func renderA2AAuthMap(agent *agentv1alpha1.PlatformAgent) (a2aAuthMapDocument, error) {
	identities := make([]a2aAuthMapIdentity, 0)
	for _, id := range calloutIdentities(agent) {
		identities = append(identities, a2aAuthMapIdentity{
			ServiceAccount: id.serviceAccount,
			User:           id.user,
			Account:        id.account,
			Grants: a2aAuthMapGrants{
				Publish:   id.publish,
				Subscribe: id.subscribe,
			},
		})
	}

	// Digest the identities in render order. a2aIdentities() is a fixed
	// slice rather than a map, so the same inputs always produce the same
	// bytes and an unchanged deployment never churns the version.
	body, err := encodeA2AJSON(identities, "")
	if err != nil {
		return a2aAuthMapDocument{}, fmt.Errorf("digesting the identity map: %w", err)
	}
	sum := sha256.Sum256(body)

	return a2aAuthMapDocument{
		Version:    hex.EncodeToString(sum[:])[:a2aAuthMapVersionLength],
		Identities: identities,
	}, nil
}

// buildA2AAuthMapConfigMap renders the map into the object the callout watches,
// and returns the version it rendered.
func buildA2AAuthMapConfigMap(agent *agentv1alpha1.PlatformAgent) (*corev1.ConfigMap, string, error) {
	doc, err := renderA2AAuthMap(agent)
	if err != nil {
		return nil, "", err
	}
	// Indented on purpose: this object is read by people during an incident
	// at least as often as by the callout, and "which grants does the bus
	// think this workload has" should be answerable with kubectl get -o
	// yaml rather than by piping through a formatter.
	body, err := encodeA2AJSON(doc, "  ")
	if err != nil {
		return nil, "", fmt.Errorf("rendering the identity map: %w", err)
	}

	return &corev1.ConfigMap{
		TypeMeta: metav1.TypeMeta{APIVersion: "v1", Kind: "ConfigMap"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      a2aAuthMapName(agent),
			Namespace: agent.Namespace,
			Labels:    a2aLabels(agent, "authmap"),
			Annotations: map[string]string{
				// The version on the object as well as inside it, so an
				// operator can compare what was rendered against what
				// the callout reports without parsing the payload.
				a2aAuthMapVersionAnnotation: doc.Version,
			},
		},
		Data: map[string]string{a2aAuthMapKey: string(body) + "\n"},
	}, doc.Version, nil
}

// encodeA2AJSON marshals with HTML escaping off and no trailing newline.
//
// Go's default encoder rewrites the three HTML-significant characters as
// numeric escapes, and one of them is the NATS wildcard. Nearly every subject
// in this map ends in that wildcard, so the default turns the one object an
// operator reads during an incident into a wall of escapes — valid JSON that
// decodes correctly and cannot be skimmed. Escaping also has to be off for the
// digest, or the version would depend on an encoder setting rather than on the
// grants.
func encodeA2AJSON(v any, indent string) ([]byte, error) {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if indent != "" {
		enc.SetIndent("", indent)
	}
	if err := enc.Encode(v); err != nil {
		return nil, err
	}
	// Encode appends a newline; the callers that need one add it where the
	// value is used, so strip it here to keep the digest and the file body
	// deciding their own trailing bytes.
	return bytes.TrimRight(buf.Bytes(), "\n"), nil
}
