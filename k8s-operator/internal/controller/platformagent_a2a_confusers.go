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

// The separators the two renders of the static principal names join with.
// auth_users is nats.conf syntax and its spacing is hashed into
// a2aConfigRolloutHash, so it keeps the spacing it always had; the callout's
// list is parsed by authcallout.ParseReservedPrincipals, which splits on a bare
// comma.
const (
	a2aAuthUsersSeparator          = ", "
	a2aReservedPrincipalsSeparator = ","
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

	// Three cases, and only the middle one is a judgement call.
	//
	// NEITHER side: no permissions block at all. Note what this is NOT: an
	// empty "permissions {}" block would not close the user down. It parses to
	// a non-nil *Permissions with both sides nil (parseUserPermissions,
	// server/opts.go, builds &Permissions{} up front and fills only the keys it
	// finds), setPermissions then sets c.perms from it, and
	// pubAllowedFullCheck's second disjunct reads nil/nil as unrestricted. An
	// absent block and an empty block both mean "no restrictions", by two
	// different routes. Omitting it is therefore not a denial; it is how sys
	// gets the $SYS account's own privileges, which is what sys is for. That is
	// safe because sys is the only identity with neither side, and
	// TestSysIsTheOnlyStaticIdentityWithNoSubjectsOfItsOwn keeps it that way --
	// a second one would be handed the whole subject space silently, which is
	// exactly the defect the rest of this function closes.
	//
	// BOTH sides: render both allow lists, which is every static user today.
	//
	// Exactly ONE side: the empty side gets an explicit deny rather than
	// nothing. An absent key is not an empty allow list -- parseUserPermissions
	// (server/opts.go) sets only the side it finds, and a side it never set is
	// unrestricted, so rendering publish-only would hand that user the whole
	// subject space to subscribe to. This is the same defect the callout's
	// identity map validator refuses, except that this half of the render has
	// no validator at all, so it is written closed here instead of caught
	// later. No static identity is one-sided today, so this changes nothing
	// about what ships; it changes what a future one-sided identity would ship.
	if len(id.publish) > 0 || len(id.subscribe) > 0 {
		b.WriteString(a2aUserBlockIndent + "  permissions {\n")
		b.WriteString(renderA2APermission(a2aSubjectListIndent, "publish", id.publish, id.denyPublish))
		b.WriteString(renderA2APermission(a2aSubjectListIndent, "subscribe", id.subscribe, id.denySubscribe))
		b.WriteString(a2aUserBlockIndent + "  }\n")
	}
	b.WriteString(a2aUserBlockIndent + "}\n")
	return b.String()
}

// renderA2APermission renders one direction's allow list and, when the
// principal has one, the deny list subtracted from it.
//
// An empty allow list renders `deny = [">"]`, not an absent key and not an
// empty block: parseUserPermissions (server/opts.go) sets only the side it
// finds, so a side it never set is unrestricted, and an empty `permissions {}`
// parses to a non-nil *Permissions whose nil/nil sides read the same way. The
// server's only spelling of "nothing" is an explicit deny of ">".
//
// That also disposes of the deny-without-allow case. Rendering the principal's
// own deny list alone would leave the allow side absent, widening it to the
// whole subject space minus a few names -- the exact opposite of what writing
// a deny means. Denying ">" is the stricter reading and the correct one: no
// allow means nothing is allowed.
//
// The branch that added TestNoA2AIdentityDeniesWhatItDoesNotFirstAllow wrote a
// deny list without an allow and rendered nothing at all, which was the widest
// of the readings rather than the narrowest. That hole is closed here by the
// deny-of-">" above, but the test is kept: with no allow the principal's own
// deny list is dropped on the floor, so a deny-without-allow is still an
// authoring mistake, just no longer a security one. Refusing it at build time
// says so where the author can see it. Every deny list in the identity table
// is an operator-authored constant, so that test sees all of them.
//
// Why `deny = [">"]` and not `allow = []`: an empty allow is not a denial
// either. parsePermSubjects (server/opts.go) starts from a nil []string and
// appends, so `allow = []` yields nil rather than an empty slice;
// setPermissions builds the allow sublist only when the slice is non-nil; and
// the check passes everything when the sublist is nil. `publish { }` goes the
// same way, parseSubjectPermission returning nil for an empty map. All three
// spellings of "nothing" -- absent block, absent key, empty allow -- read as
// unrestricted, which is why the only one written here is an explicit deny.
// Read against v2.15.0, the version both modules build; the original
// measurement was taken on v2.14.6 before #2232's bump and the nil-slice path
// is unchanged.
func renderA2APermission(indent, kind string, allow, deny []string) string {
	if len(allow) == 0 {
		return indent + kind + ` { deny = [">"] }` + "\n"
	}
	var b strings.Builder
	b.WriteString(indent + kind + " {\n")
	b.WriteString(renderA2ASubjectList(indent+"  ", "allow", allow))
	if len(deny) > 0 {
		b.WriteString(renderA2ASubjectList(indent+"  ", "deny", deny))
	}
	b.WriteString(indent + "}\n")
	return b.String()
}

func renderA2ASubjectList(indent, kind string, subjects []string) string {
	if len(subjects) == 0 {
		return ""
	}
	var b strings.Builder
	b.WriteString(indent + kind + " = [\n")
	for i, s := range subjects {
		comma := ","
		if i == len(subjects)-1 {
			comma = ""
		}
		b.WriteString(indent + `  "` + s + `"` + comma + "\n")
	}
	b.WriteString(indent + "]\n")
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
	return strings.Join(a2aStaticPrincipalNames(agent), a2aAuthUsersSeparator)
}

// a2aStaticPrincipalNames is every user nats.conf authenticates by password,
// in render order: the callout's own user, then the static identities. It is
// the one list both auth_users and the callout's reserved principal names are
// rendered from, so the two cannot disagree.
func a2aStaticPrincipalNames(agent *agentv1alpha1.PlatformAgent) []string {
	names := []string{a2aCalloutConfUser}
	for _, id := range staticIdentities(agent) {
		names = append(names, id.user)
	}
	return names
}

// renderA2AReservedPrincipals is the callout's A2A_RESERVED_PRINCIPALS value:
// the static principal names a narrowed pod may not be named after, because a
// narrowed user's name is its inbox prefix. Plain env rather than a mount of
// nats.conf, which carries every static user's password.
func renderA2AReservedPrincipals(agent *agentv1alpha1.PlatformAgent) string {
	return strings.Join(a2aStaticPrincipalNames(agent), a2aReservedPrincipalsSeparator)
}
