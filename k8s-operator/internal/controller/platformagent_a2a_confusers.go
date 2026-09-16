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
	"strings"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// Rendering the principals that still authenticate from nats.conf.
//
// These come from the same list the callout's identity map is rendered from, so
// a principal cannot end up in both renders or in neither, and its grant list
// cannot say one thing here and another there.

// a2aCalloutConfUser is the NATS user the callout service itself connects as.
// It is in AUTH rather than APP, and it is exempt from the callout because it
// cannot authenticate through the thing it is.
const a2aCalloutConfUser = "callout"

// The columns the rendered blocks sit at. nats.conf nests a user block three
// levels deep - accounts { <ACCOUNT> { users [ ... - and a permissions block's
// publish/subscribe lists two levels further in. Only the parser's whitespace,
// but a2aConfigRolloutHash digests this render byte for byte, so either value
// changing changes the hash and rolls the NATS StatefulSet. That is a reason to
// name them once here rather than to leave them mid-function where a reader
// fixing the layout cannot see what else it moves.
const (
	a2aUserBlockIndent   = "      "
	a2aSubjectListIndent = a2aUserBlockIndent + "    "
)

// renderA2AStaticUsers renders the user blocks for one account.
//
// pw rather than the creds Secret, and that is a constraint rather than a
// style: renderA2ANATSConf takes every password through pw so a2aConfigRolloutHash
// can walk the same template with placeholders and get a digest that covers
// every non-secret byte without covering a credential. Reading the Secret here
// would put five real passwords back into the hashed input by a route the
// digest's own guard test cannot see.
func renderA2AStaticUsers(agent *agentv1alpha1.PlatformAgent, pw func(key string) string, account string) string {
	var b strings.Builder
	for _, id := range staticIdentities(agent) {
		if id.account != account {
			continue
		}
		b.WriteString(renderA2AStaticUser(id, pw(id.credsKey)))
	}
	return b.String()
}

func renderA2AStaticUser(id a2aIdentity, password string) string {
	var b strings.Builder

	for _, line := range strings.Split(id.comment, "\n") {
		b.WriteString(a2aUserBlockIndent + "# " + line + "\n")
	}
	b.WriteString(a2aUserBlockIndent + "{\n")
	b.WriteString(a2aUserBlockIndent + "  user: " + id.user + "\n")
	b.WriteString(a2aUserBlockIndent + `  password: "` + password + "\"\n")

	// $SYS's user holds the system account's own privileges and carries no
	// subject lists; a permissions block with empty allow lists would deny it
	// everything.
	if len(id.publish) > 0 || len(id.subscribe) > 0 {
		b.WriteString(a2aUserBlockIndent + "  permissions {\n")
		b.WriteString(renderA2ASubjectList(a2aSubjectListIndent, "publish", id.publish))
		b.WriteString(renderA2ASubjectList(a2aSubjectListIndent, "subscribe", id.subscribe))
		b.WriteString(a2aUserBlockIndent + "  }\n")
	}
	b.WriteString(a2aUserBlockIndent + "}\n")
	return b.String()
}

func renderA2ASubjectList(indent, kind string, subjects []string) string {
	if len(subjects) == 0 {
		return ""
	}
	var b strings.Builder
	b.WriteString(indent + kind + " { allow = [\n")
	for i, s := range subjects {
		comma := ","
		if i == len(subjects)-1 {
			comma = ""
		}
		b.WriteString(indent + `  "` + s + `"` + comma + "\n")
	}
	b.WriteString(indent + "] }\n")
	return b.String()
}

// renderA2AAuthUsers renders the auth_users exemption list.
//
// It is built from the static set rather than written out, because the two must
// agree exactly and the failure when they do not is asymmetric. A static user
// missing from this list is handed to a callout that has never heard of it and
// is refused at connect — that one is loud. A name here with no matching user
// block is the quiet one: the server accepts the config, and the exemption
// simply covers nothing.
func renderA2AAuthUsers(agent *agentv1alpha1.PlatformAgent) string {
	names := []string{a2aCalloutConfUser}
	for _, id := range staticIdentities(agent) {
		names = append(names, id.user)
	}
	return strings.Join(names, ", ")
}
