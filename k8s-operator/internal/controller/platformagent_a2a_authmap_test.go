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
	"encoding/json"
	"flag"
	"os"
	"path/filepath"
	"strings"
	"testing"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// calloutFixturePath is the rendered map the a2a module's callout parses in its
// own test. The two modules cannot import each other, so this file is the
// contract between them: the operator writes it, the callout reads it, and a
// field renamed on one side fails on the other.
//
// Regenerate with: go test ./internal/controller/ -run TestRenderedAuthMap -update
const calloutFixturePath = "../../../a2a/authcallout/testdata/rendered-identity-map.json"

var updateAuthMapFixture = flag.Bool("update", false, "rewrite the callout's identity-map fixture from the current render")

func authMapTestAgent() *agentv1alpha1.PlatformAgent {
	return &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "agent", Namespace: "kubeagents-system"},
	}
}

func TestRenderedAuthMapCarriesEveryCalloutPrincipalAndNoStaticOne(t *testing.T) {
	agent := authMapTestAgent()
	cm, version, err := buildA2AAuthMapConfigMap(agent)
	if err != nil {
		t.Fatalf("buildA2AAuthMapConfigMap: %v", err)
	}

	var doc a2aAuthMapDocument
	if err := json.Unmarshal([]byte(cm.Data[a2aAuthMapKey]), &doc); err != nil {
		t.Fatalf("rendered map does not parse: %v", err)
	}

	got := map[string]bool{}
	for _, id := range doc.Identities {
		got[id.User] = true
	}
	for _, id := range calloutIdentities(agent) {
		if !got[id.user] {
			t.Errorf("callout principal %q is missing from the map; it would be refused at connect", id.user)
		}
	}
	// A static principal in the map would be served by BOTH the callout and
	// the config's auth_users exemption, and which one answered would depend
	// on how the client happened to connect.
	for _, id := range staticIdentities(agent) {
		if got[id.user] {
			t.Errorf("static principal %q is in the callout map; it is authenticated by nats.conf and must not be in both", id.user)
		}
	}

	if doc.Version != version {
		t.Errorf("version in the document (%q) differs from the one returned (%q)", doc.Version, version)
	}
	if cm.Annotations[a2aAuthMapVersionAnnotation] != version {
		t.Errorf("version annotation = %q, want %q", cm.Annotations[a2aAuthMapVersionAnnotation], version)
	}
}

// The version has to be a function of the grants and nothing else: stable
// across renders of an unchanged deployment, and different the moment a grant
// moves. A version that churns makes BusCredentialsReady flap; one that does
// not move on a real change makes it lie.
func TestTheMapVersionNamesTheContent(t *testing.T) {
	first, err := renderA2AAuthMap(authMapTestAgent())
	if err != nil {
		t.Fatalf("renderA2AAuthMap: %v", err)
	}
	second, err := renderA2AAuthMap(authMapTestAgent())
	if err != nil {
		t.Fatalf("renderA2AAuthMap: %v", err)
	}
	if first.Version != second.Version {
		t.Errorf("two renders of one deployment gave versions %q and %q", first.Version, second.Version)
	}

	// A different namespace means different ServiceAccount names, which is
	// a real change to who the map authenticates.
	other := authMapTestAgent()
	other.Namespace = "somewhere-else"
	moved, err := renderA2AAuthMap(other)
	if err != nil {
		t.Fatalf("renderA2AAuthMap: %v", err)
	}
	if moved.Version == first.Version {
		t.Error("moving every ServiceAccount to another namespace did not change the version")
	}
}

// The map keys on whatever ServiceAccount the workload actually runs as. Two
// renders of the same name, from two functions, in two files: the map's key and
// the Job's ServiceAccountName. If they drift, the Job authenticates with a
// valid token and the callout answers that it knows nobody by that name — the
// failure that looks like the callout is broken when it is doing exactly what
// it was told.
//
// Asserted against the rendered Job object rather than against the helper both
// sides call, because calling the helper twice would agree with itself no
// matter what either render does.
func TestTheMapKeysOnTheServiceAccountTheProvisionJobRunsAs(t *testing.T) {
	agent := authMapTestAgent()

	job := buildA2AProvisionJob(agent)
	sa := job.Spec.Template.Spec.ServiceAccountName
	if sa == "" {
		t.Fatal("the provision Job renders no ServiceAccountName; the check below would pass on the empty string")
	}
	want := "system:serviceaccount:" + agent.Namespace + ":" + sa

	doc, err := renderA2AAuthMap(agent)
	if err != nil {
		t.Fatalf("renderA2AAuthMap: %v", err)
	}
	for _, id := range doc.Identities {
		if id.User == "provision" {
			if id.ServiceAccount != want {
				t.Errorf("provision principal keys on %q, but the Job runs as %q", id.ServiceAccount, want)
			}
			return
		}
	}
	t.Fatal("no provision principal in the rendered map")
}

// The wildcard readability check, asserted because the default JSON encoder
// silently undoes it. Every subject list here is full of > wildcards and the
// escaped form is what an operator would be reading at 3 AM.
func TestTheRenderedMapIsReadable(t *testing.T) {
	cm, _, err := buildA2AAuthMapConfigMap(authMapTestAgent())
	if err != nil {
		t.Fatalf("buildA2AAuthMapConfigMap: %v", err)
	}
	body := cm.Data[a2aAuthMapKey]

	// The escape Go's default encoder would emit for the NATS wildcard,
	// spelled as its six literal characters rather than written out, so that
	// nothing between here and the file can quietly turn it back into the
	// character it is standing in for.
	escapedWildcard := `\u` + `003e`
	if strings.Contains(body, escapedWildcard) {
		t.Errorf("rendered map contains %s escapes; SetEscapeHTML(false) was lost", escapedWildcard)
	}
	if !strings.Contains(body, "_INBOX.provision.>") {
		t.Error("rendered map does not carry the provision inbox grant as a plain wildcard")
	}
}

// The cross-module contract. The a2a module's callout parses this exact file in
// its own test suite, with unknown fields refused, so a field renamed here
// without being renamed there fails on the other side of the repo.
func TestRenderedAuthMapMatchesTheCalloutFixture(t *testing.T) {
	cm, _, err := buildA2AAuthMapConfigMap(authMapTestAgent())
	if err != nil {
		t.Fatalf("buildA2AAuthMapConfigMap: %v", err)
	}
	rendered := cm.Data[a2aAuthMapKey]

	path := filepath.Clean(calloutFixturePath)
	if *updateAuthMapFixture {
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			t.Fatalf("creating fixture directory: %v", err)
		}
		if err := os.WriteFile(path, []byte(rendered), 0o644); err != nil {
			t.Fatalf("writing fixture: %v", err)
		}
		t.Logf("wrote %s", path)
		return
	}

	want, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("reading the callout fixture: %v\nRegenerate with: go test ./internal/controller/ -run TestRenderedAuthMap -update", err)
	}
	if string(want) != rendered {
		t.Errorf("the rendered map and the fixture the callout parses have diverged.\nRegenerate with: go test ./internal/controller/ -run TestRenderedAuthMap -update\n--- fixture\n%s\n--- rendered\n%s", want, rendered)
	}
}

// natsConfFixturePath is the operator's real rendered nats.conf, which the a2a
// module's integration test starts an actual nats-server from.
//
// This is the other half of the same contract as the identity-map fixture, and
// it closes the larger gap: a callout test written against a hand-written
// config proves the callout works against THAT config, not against the one this
// operator ships. With the real render in the loop, a malformed auth_callout
// block, a missing max_control_line, or a static user left out of auth_users
// fails over in the a2a suite instead of on a cluster.
//
// Only public halves are written. The seeds stay out of the repository, and the
// consuming test substitutes its own keypair's public values so it can hold the
// matching seeds.
//
// Regenerate with: go test ./internal/controller/ -run TestRenderedNATSConf -update
const natsConfFixturePath = "../../../a2a/authcallout/testdata/rendered-nats.conf"

func TestRenderedNATSConfMatchesTheCalloutFixture(t *testing.T) {
	conf := string(buildA2ANATSConfigSecret(authMapTestAgent(), a2aTestCreds(), a2aTestCalloutKeys(t)).Data["nats.conf"])

	// The generated keys differ on every run, so the committed fixture is
	// normalised to a stable placeholder. The consuming test replaces these
	// with real values anyway; what has to stay byte-stable here is the
	// structure around them.
	conf = a2aNormaliseKeyLine(conf, "issuer: ", "A")
	conf = a2aNormaliseKeyLine(conf, "xkey: ", "X")

	path := filepath.Clean(natsConfFixturePath)
	if *updateAuthMapFixture {
		if err := os.WriteFile(path, []byte(conf), 0o644); err != nil {
			t.Fatalf("writing fixture: %v", err)
		}
		t.Logf("wrote %s", path)
		return
	}

	want, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("reading the nats.conf fixture: %v\nRegenerate with: go test ./internal/controller/ -run TestRenderedNATSConf -update", err)
	}
	if string(want) != conf {
		t.Errorf("the rendered nats.conf and the fixture the callout suite starts a server from have diverged.\nRegenerate with: go test ./internal/controller/ -run TestRenderedNATSConf -update")
	}
}

// a2aNormaliseKeyLine replaces a generated public key with a stable
// placeholder of the same prefix, so the fixture diffs on structure rather than
// on entropy.
func a2aNormaliseKeyLine(conf, prefix, keyPrefix string) string {
	i := strings.Index(conf, prefix+keyPrefix)
	if i < 0 {
		return conf
	}
	start := i + len(prefix)
	end := start
	for end < len(conf) && conf[end] != '\n' {
		end++
	}
	return conf[:start] + keyPrefix + "PLACEHOLDERPUBLICKEYSUBSTITUTEDBYTHECALLOUTTESTSUITE" + conf[end:]
}
