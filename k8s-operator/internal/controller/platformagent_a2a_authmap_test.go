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
	"slices"
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

// The render is the operator's last chance to notice it is about to publish a
// map the callout will throw away.
//
// The failure this closes is not a refused connection — it is a condition that
// lies. The callout keeps serving the previous map when a new one fails to
// parse, so its readiness probe stays green, so the Deployment stays Ready, so
// setBusCredentialsReady sets True and names the version this reconcile
// rendered. The operator reports it is serving a map that was rejected, and
// goes on reporting it until someone reads the callout's own /status.
func TestTheRenderedMapSatisfiesTheCalloutsOwnRules(t *testing.T) {
	doc, err := renderA2AAuthMap(authMapTestAgent())
	if err != nil {
		t.Fatalf("the map this operator renders today does not satisfy the callout's rules: %v", err)
	}
	if len(doc.Identities) == 0 {
		t.Fatal("the render produced no identities, so the check below proves nothing")
	}
}

// One case per rule, each written as the edit a maintainer would plausibly
// make. The narrowed-entry case is the one that motivated the check: adding a
// publish grant to the session principal looks like every other grant edit in
// the file, and is the one edit that turns per-session narrowing back into a
// shared credential.
func TestTheRenderTimeMapCheckRefusesWhatTheCalloutWouldRefuse(t *testing.T) {
	ok := func() []a2aAuthMapIdentity {
		return []a2aAuthMapIdentity{
			{
				ServiceAccount: "system:serviceaccount:kubeagents-system:agent-a2a-gateway",
				User:           "gateway",
				Account:        a2aAccountApp,
				Grants:         a2aAuthMapGrants{Publish: []string{"a2a.tasks.>"}},
			},
			{
				ServiceAccount: "system:serviceaccount:kubeagents-system:agent-a2a-session",
				User:           "session",
				Account:        a2aAccountApp,
				Narrowing:      a2aNarrowingPod,
			},
		}
	}

	if err := validateA2AAuthMapIdentities(ok()); err != nil {
		t.Fatalf("the baseline map was refused, so every case below is untrustworthy: %v", err)
	}

	cases := map[string]func([]a2aAuthMapIdentity) []a2aAuthMapIdentity{
		"a narrowed entry that also carries grants": func(ids []a2aAuthMapIdentity) []a2aAuthMapIdentity {
			ids[1].Grants.Publish = []string{"a2a.tasks.>"}
			return ids
		},
		"a narrowed entry carrying only a subscribe": func(ids []a2aAuthMapIdentity) []a2aAuthMapIdentity {
			ids[1].Grants.Subscribe = []string{"a2a.tasks.>"}
			return ids
		},
		"a narrowing the callout does not implement": func(ids []a2aAuthMapIdentity) []a2aAuthMapIdentity {
			ids[1].Narrowing = "namespace"
			return ids
		},
		"an entry with neither grants nor narrowing": func(ids []a2aAuthMapIdentity) []a2aAuthMapIdentity {
			ids[0].Grants = a2aAuthMapGrants{}
			return ids
		},
		"an entry in the system account": func(ids []a2aAuthMapIdentity) []a2aAuthMapIdentity {
			ids[0].Account = a2aAccountSys
			return ids
		},
		"a username that is not a ServiceAccount": func(ids []a2aAuthMapIdentity) []a2aAuthMapIdentity {
			ids[0].ServiceAccount = "kubernetes-admin"
			return ids
		},
		"two entries for one ServiceAccount": func(ids []a2aAuthMapIdentity) []a2aAuthMapIdentity {
			ids[1].ServiceAccount = ids[0].ServiceAccount
			return ids
		},
		"two entries sharing a NATS user": func(ids []a2aAuthMapIdentity) []a2aAuthMapIdentity {
			ids[1].User = ids[0].User
			return ids
		},
		"no identities at all": func([]a2aAuthMapIdentity) []a2aAuthMapIdentity {
			return nil
		},
	}

	for name, mutate := range cases {
		t.Run(name, func(t *testing.T) {
			if err := validateA2AAuthMapIdentities(mutate(ok())); err == nil {
				t.Error("the render accepted a map the callout refuses, so BusCredentialsReady would report a version that was never served")
			}
		})
	}
}

// a2aIdentityMapSchemaKeys is the JSON shape each schema version promises: every
// key the renderer may emit anywhere in the document, sorted.
//
// The callout parses with DisallowUnknownFields, so a key added here that a
// running callout does not know is a map it refuses -- while keeping its
// readiness probe green and its previous map in service. The pod template
// carries a2aIdentityMapSchema so that addition rolls the callout; this table
// is what makes the constant load-bearing rather than decorative, since a field
// added without a bump is otherwise invisible until an upgrade in the field.
var a2aIdentityMapSchemaKeys = map[string][]string{
	"1": {"account", "grants", "identities", "publish", "serviceAccount", "subscribe", "user", "version"},
	"2": {"account", "grants", "identities", "narrowing", "publish", "serviceAccount", "subscribe", "user", "version"},
}

// a2aJSONKeys walks a decoded document and collects every object key in it.
func a2aJSONKeys(v any, into map[string]bool) {
	switch t := v.(type) {
	case map[string]any:
		for k, sub := range t {
			into[k] = true
			a2aJSONKeys(sub, into)
		}
	case []any:
		for _, sub := range t {
			a2aJSONKeys(sub, into)
		}
	}
}

// The rendered map's shape matches the schema version the callout pod template
// pins, and that version rolls the callout when it changes.
//
// Adding a field to the identity map is expected to fail this test once. The
// failure is the review: either the new key is safe for an old callout to
// ignore -- it is not, DisallowUnknownFields means every key is breaking -- or
// a2aIdentityMapSchema needs a bump so the upgrade rolls the pods that cannot
// read it.
func TestTheIdentityMapShapeMatchesTheSchemaTheCalloutPodTemplatePins(t *testing.T) {
	agent := a2aTestAgent()

	cm, _, err := buildA2AAuthMapConfigMap(agent)
	if err != nil {
		t.Fatalf("rendering the auth map: %v", err)
	}
	var doc any
	if err := json.Unmarshal([]byte(cm.Data[a2aAuthMapKey]), &doc); err != nil {
		t.Fatalf("decoding the rendered auth map: %v", err)
	}
	seen := map[string]bool{}
	a2aJSONKeys(doc, seen)
	got := make([]string, 0, len(seen))
	for k := range seen {
		got = append(got, k)
	}
	slices.Sort(got)

	want, ok := a2aIdentityMapSchemaKeys[a2aIdentityMapSchema]
	if !ok {
		t.Fatalf("a2aIdentityMapSchema is %q, which a2aIdentityMapSchemaKeys does not describe; a bump needs its key set recorded beside it", a2aIdentityMapSchema)
	}
	if !slices.Equal(got, want) {
		t.Errorf("the rendered identity map's keys are %v, and schema %s promises %v.\n"+
			"A key the running callout does not know is a map it refuses while its readiness probe stays green, "+
			"so a field addition needs a2aIdentityMapSchema bumped (which rolls the callout) and a new row in a2aIdentityMapSchemaKeys.",
			got, a2aIdentityMapSchema, want)
	}

	// The constant is only worth anything if it reaches the pod template,
	// which is the thing that actually rolls.
	dep := buildA2ACalloutDeployment(agent)
	if got := dep.Spec.Template.ObjectMeta.Annotations[a2aIdentityMapSchemaAnnotation]; got != a2aIdentityMapSchema {
		t.Errorf("the callout pod template's %s annotation is %q, want %q; without it on the TEMPLATE a schema bump changes no pod spec and the old callout is never rolled",
			a2aIdentityMapSchemaAnnotation, got, a2aIdentityMapSchema)
	}
}

// The annotation is on the pod template and not merely on the Deployment: only
// the template is part of the pod spec the Deployment controller diffs, so an
// annotation one level up would look like a fix and roll nothing.
func TestTheSchemaAnnotationIsOnThePodTemplateNotTheDeployment(t *testing.T) {
	dep := buildA2ACalloutDeployment(a2aTestAgent())
	if _, onDeployment := dep.ObjectMeta.Annotations[a2aIdentityMapSchemaAnnotation]; onDeployment {
		if _, onTemplate := dep.Spec.Template.ObjectMeta.Annotations[a2aIdentityMapSchemaAnnotation]; !onTemplate {
			t.Error("the schema annotation is on the Deployment but not its pod template; changing it would roll nothing")
		}
	}
	if len(dep.Spec.Template.ObjectMeta.Annotations) == 0 {
		t.Fatal("the callout pod template carries no annotations at all, so no schema change can roll it")
	}
}
