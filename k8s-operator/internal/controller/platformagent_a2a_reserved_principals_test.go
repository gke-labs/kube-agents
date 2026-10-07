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
	"slices"
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The callout's A2A_RESERVED_PRINCIPALS must name exactly the users nats.conf
// authenticates by password. A name missing from it is a static principal a
// narrowed pod can be named after, and so hold the inbox of; the callout reads
// nothing else that would tell it.

// renderedNATSConfAuthUsers parses auth_users out of the rendered nats.conf
// text, independently of the function that renders it, so the comparison below
// is against the file the server reads rather than against the renderer's own
// list.
func renderedNATSConfAuthUsers(t *testing.T, conf string) []string {
	t.Helper()
	const key = "auth_users:"
	start := strings.Index(conf, key)
	if start < 0 {
		t.Fatal("the rendered nats.conf has no auth_users list")
	}
	rest := conf[start+len(key):]
	open, end := strings.Index(rest, "["), strings.Index(rest, "]")
	if open < 0 || end < open {
		t.Fatal("auth_users is not a bracketed list")
	}
	var names []string
	for _, f := range strings.Split(rest[open+1:end], ",") {
		if n := strings.TrimSpace(f); n != "" {
			names = append(names, n)
		}
	}
	return names
}

func calloutContainer(t *testing.T, agent *agentv1alpha1.PlatformAgent) corev1.Container {
	t.Helper()
	for _, c := range buildA2ACalloutDeployment(agent).Spec.Template.Spec.Containers {
		if c.Name == "callout" {
			return c
		}
	}
	t.Fatal("the callout Deployment has no container named callout")
	return corev1.Container{}
}

func TestTheCalloutReservesExactlyTheNATSConfStaticUsers(t *testing.T) {
	agent := authMapTestAgent()
	conf := string(buildA2ANATSConfigSecret(agent, a2aTestCreds(), a2aTestCalloutKeys(t)).Data["nats.conf"])
	authUsers := renderedNATSConfAuthUsers(t, conf)
	if len(authUsers) == 0 {
		t.Fatal("the rendered auth_users list is empty; the comparison below would pass vacuously")
	}
	for _, want := range []string{"gateway", "web", "console", "bridge", "seed"} {
		if !slices.Contains(authUsers, want) {
			t.Errorf("auth_users %v has no %q", authUsers, want)
		}
	}

	got, found := envValue(calloutContainer(t, agent), a2aCalloutReservedPrincipalsEnvVar)
	if !found {
		t.Fatalf("the callout Deployment does not render %s; the callout refuses to start without it", a2aCalloutReservedPrincipalsEnvVar)
	}
	// Exact, including order and spelling: a bare comma between names is the
	// format the render promises. authcallout.ParseReservedPrincipals trims
	// spaces around a name but refuses an empty element.
	if want := strings.Join(authUsers, ","); got != want {
		t.Errorf("%s = %q, want %q (nats.conf's auth_users); a static user missing here is one a narrowed pod can impersonate",
			a2aCalloutReservedPrincipalsEnvVar, got, want)
	}
}

// The value is names only. nats.conf carries the static users' passwords, and
// the callout's environment must carry none of them.
func TestTheReservedPrincipalsCarryNoCredential(t *testing.T) {
	agent := authMapTestAgent()
	creds := a2aTestCreds()
	got, _ := envValue(calloutContainer(t, agent), a2aCalloutReservedPrincipalsEnvVar)
	for key, pw := range creds.Data {
		if len(pw) > 0 && strings.Contains(got, string(pw)) {
			t.Errorf("%s contains the value of creds key %q", a2aCalloutReservedPrincipalsEnvVar, key)
		}
	}
	for _, env := range calloutContainer(t, agent).Env {
		if env.Name == a2aCalloutReservedPrincipalsEnvVar && env.ValueFrom != nil {
			t.Errorf("%s is rendered from a reference, not as plain names", a2aCalloutReservedPrincipalsEnvVar)
		}
	}
}
