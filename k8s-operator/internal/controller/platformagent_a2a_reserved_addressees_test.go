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

// The callout's reserved addressees, held to the render and to the a2a module.
//
// A narrowed pod named after an addressee is handed that addressee's task
// subjects. The callout refuses such a pod by name, from A2A_RESERVED_ADDRESSEES,
// so that list has to be every fixed-name addressee the install routes to. Those
// are read from three places: the subjects the bridge identity's grants name,
// which the operator renders, and two defaults it does not render, the
// gateway's (A2A_DEFAULT_ADDRESSEE) and the bridge's profile (BRIDGE_PROFILE).
// The defaults live in the a2a module, which this module cannot import, so the
// test reads them out of the source. Every extraction fails the test rather than falling
// back, so a moved or reshaped default reds here instead of passing vacuously.

import (
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"testing"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

const (
	// reservedAddresseesFixturePath is the rendered list the a2a module's
	// callout tests refuse against. The operator writes it, the callout reads
	// it. Regenerate with:
	// go test ./internal/controller/ -run TestRenderedReservedAddresseesMatchTheCalloutFixture -update
	reservedAddresseesFixturePath = "../../../a2a/authcallout/testdata/rendered-reserved-addressees.txt"

	a2aGatewayConfigSource = "../../../a2a/gateway/config.go"
	a2aGatewaySpawnSource  = "../../../a2a/gateway/spawn.go"

	// gatewayRouteSessionConst names the A2A_DEFAULT_ADDRESSEE value that
	// routes each conversation to a session pod of its own. It is a sentinel,
	// not an addressee: the gateway never publishes to a literal "session"
	// addressee (it refuses to start with it and no spawner), and each session
	// pod's task subjects are keyed on its own minted name.
	gatewayRouteSessionConst = "RouteSession"

	// The env names whose defaults the a2a module holds.
	gatewayDefaultAddresseeEnv = "A2A_DEFAULT_ADDRESSEE"
	bridgeProfileEnv           = "BRIDGE_PROFILE"
)

// envDefaultInSource finds the one `envOr("<env>", <default>)` call in a Go
// file and returns the default. A string literal is returned as is; an
// identifier is resolved to the string constant of that name in the same file.
// Anything else, zero calls or more than one, fails the test.
func envDefaultInSource(t *testing.T, path, env string) string {
	t.Helper()
	fset := token.NewFileSet()
	f, err := parser.ParseFile(fset, path, nil, 0)
	if err != nil {
		t.Fatalf("parse %s: %v (if it moved, this test's path must move with it)", path, err)
	}
	consts := map[string]string{}
	var defaults []ast.Expr
	ast.Inspect(f, func(n ast.Node) bool {
		switch n := n.(type) {
		case *ast.ValueSpec:
			for i, name := range n.Names {
				if i >= len(n.Values) {
					continue
				}
				if lit, ok := n.Values[i].(*ast.BasicLit); ok && lit.Kind == token.STRING {
					if v, err := strconv.Unquote(lit.Value); err == nil {
						consts[name.Name] = v
					}
				}
			}
		case *ast.CallExpr:
			fn, ok := n.Fun.(*ast.Ident)
			if !ok || fn.Name != "envOr" || len(n.Args) != 2 {
				return true
			}
			key, ok := n.Args[0].(*ast.BasicLit)
			if !ok || key.Kind != token.STRING || key.Value != strconv.Quote(env) {
				return true
			}
			defaults = append(defaults, n.Args[1])
		}
		return true
	})
	if len(defaults) != 1 {
		t.Fatalf("found %d `envOr(%q, ...)` calls in %s, want exactly 1; the reserved addressees are held to that default", len(defaults), env, path)
	}
	switch d := defaults[0].(type) {
	case *ast.BasicLit:
		v, err := strconv.Unquote(d.Value)
		if err != nil || d.Kind != token.STRING {
			t.Fatalf("the %s default in %s is %s, not a string literal", env, path, d.Value)
		}
		return v
	case *ast.Ident:
		v, ok := consts[d.Name]
		if !ok {
			t.Fatalf("the %s default in %s is %s, which is not a string constant in that file", env, path, d.Name)
		}
		return v
	default:
		t.Fatalf("the %s default in %s is neither a string literal nor a constant", env, path)
	}
	return ""
}

// stringConstInSource returns the string constant of that name in a Go file.
// No such constant, or an empty one, fails the test.
func stringConstInSource(t *testing.T, path, name string) string {
	t.Helper()
	f, err := parser.ParseFile(token.NewFileSet(), path, nil, 0)
	if err != nil {
		t.Fatalf("parse %s: %v (if it moved, this test's path must move with it)", path, err)
	}
	var found []string
	ast.Inspect(f, func(n ast.Node) bool {
		spec, ok := n.(*ast.ValueSpec)
		if !ok {
			return true
		}
		for i, id := range spec.Names {
			if id.Name != name || i >= len(spec.Values) {
				continue
			}
			if lit, ok := spec.Values[i].(*ast.BasicLit); ok && lit.Kind == token.STRING {
				if v, err := strconv.Unquote(lit.Value); err == nil {
					found = append(found, v)
				}
			}
		}
		return true
	})
	if len(found) != 1 || found[0] == "" {
		t.Fatalf("found %d non-empty string constants named %s in %s (%q), want exactly 1", len(found), name, path, found)
	}
	return found[0]
}

func renderedCalloutReservedAddressees(t *testing.T, agent *agentv1alpha1.PlatformAgent) []string {
	t.Helper()
	raw, ok := envValue(calloutContainer(t, agent), a2aCalloutReservedAddresseesEnvVar)
	if !ok {
		t.Fatalf("the callout Deployment does not render %s; the callout refuses to start without it", a2aCalloutReservedAddresseesEnvVar)
	}
	if raw == "" {
		t.Fatalf("%s renders empty; every refusal test would pass vacuously", a2aCalloutReservedAddresseesEnvVar)
	}
	return strings.Split(raw, a2aReservedAddresseesSeparator)
}

// bridgeGrantAddressees is every addressee the bridge identity's grants name,
// read off the subjects themselves rather than off a constant: the `<x>` in
// `a2a.tasks.<x>.*.in` and `a2a.tasks.<x>.*.events`, in `a2a.cap.verify.<x>`
// and in `a2a.cap.reply.<x>.>`. Widening the grant to a second addressee adds
// it here with no test edit. See grantAddressees for what fails the test.
func bridgeGrantAddressees(t *testing.T) []string {
	t.Helper()
	id := bridgeIdentity()
	out, err := grantAddressees(slices.Concat(id.publish, id.subscribe))
	if err != nil {
		t.Fatal(err)
	}
	return out
}

// addresseeSubjectFamilies are the subject shapes that carry an addressee:
// the literal tokens before it, and so its position.
var addresseeSubjectFamilies = [][]string{
	{"a2a", "tasks"},
	{"a2a", "cap", "verify"},
	{"a2a", "cap", "reply"},
}

// grantAddressees reads the addressees a list of grant subjects names. A grant
// that leaves the addressee position open is an error, because no name list
// can reserve it: any `*` or `>` at or before the addressee position of one of
// addresseeSubjectFamilies, whatever the subject's token count, so a short
// `a2a.tasks.>` fails as surely as `a2a.tasks.*.*.in`. Finding no addressee at
// all is an error too, which would make every check on the result pass
// vacuously.
func grantAddressees(subjects []string) ([]string, error) {
	var out []string
	for _, subject := range subjects {
		tokens := strings.Split(subject, ".")
		for _, family := range addresseeSubjectFamilies {
			if i := wildcardAtOrBefore(tokens, family); i >= 0 {
				return nil, fmt.Errorf("the grant %q has a wildcard at token %d, at or before the addressee position of `%s.<addressee>`; "+
					"a wildcard addressee cannot be reserved by name", subject, i, strings.Join(family, "."))
			}
		}
		var addressee string
		switch {
		case len(tokens) == 5 && tokens[0] == "a2a" && tokens[1] == "tasks" &&
			(tokens[4] == "in" || tokens[4] == "events"):
			addressee = tokens[2]
		case len(tokens) == 4 && tokens[0] == "a2a" && tokens[1] == "cap" && tokens[2] == "verify":
			addressee = tokens[3]
		case len(tokens) == 5 && tokens[0] == "a2a" && tokens[1] == "cap" && tokens[2] == "reply" && tokens[4] == ">":
			addressee = tokens[3]
		default:
			continue
		}
		if addressee == "" || strings.ContainsAny(addressee, "*>") {
			return nil, fmt.Errorf("the grant %q names addressee %q; a wildcard addressee cannot be reserved by name", subject, addressee)
		}
		out = append(out, addressee)
	}
	if len(out) == 0 {
		return nil, fmt.Errorf("found no addressee in the grants %v; the subject shapes this test reads have moved", subjects)
	}
	slices.Sort(out)
	return slices.Compact(out), nil
}

// wildcardAtOrBefore returns the index of the first `*` or `>` in tokens at or
// before the addressee position that follows family's literal prefix, or -1.
// A literal token that differs from the prefix means the subject is not of
// that family; a subject that ends before the addressee position, with no
// wildcard, names no addressee subject at all.
func wildcardAtOrBefore(tokens, family []string) int {
	for i := 0; i <= len(family) && i < len(tokens); i++ {
		if tokens[i] == "*" || tokens[i] == ">" {
			return i
		}
		if i < len(family) && tokens[i] != family[i] {
			return -1
		}
	}
	return -1
}

// A grant whose wildcard covers the addressee position fails, whatever its
// token count: a shorter `>` swallows the addressee as surely as a `*` in it.
// Each row keeps the bridge's real subjects alongside, so the "no addressee at
// all" guard cannot be what catches it.
func TestReservedAddresseesRefuseAWildcardGrant(t *testing.T) {
	id := bridgeIdentity()
	real := slices.Concat(id.publish, id.subscribe)
	for _, wide := range []string{
		">",
		"a2a.>",
		"*.tasks.platform.*.in",
		"a2a.*.platform.*.in",
		"a2a.tasks.>",
		"a2a.tasks.*.>",
		"a2a.tasks.*.*.in",
		"a2a.tasks.*.*.events",
		"a2a.cap.>",
		"a2a.cap.verify.*",
		"a2a.cap.verify.>",
		"a2a.cap.reply.>",
		"a2a.cap.reply.*.>",
	} {
		if got, err := grantAddressees(append(slices.Clone(real), wide)); err == nil {
			t.Errorf("a grant of %q leaves the addressee open and was read as addressees %v; it must fail", wide, got)
		}
	}
	// The real grants pass, and leave-alone subjects outside the three
	// addressee-bearing families do not trip the guard.
	if got, err := grantAddressees(append(slices.Clone(real), "_INBOX.x.>", "$KV.runtime-state.>", "a2a.tasks")); err != nil || !slices.Contains(got, a2aBridgeAddressee) {
		t.Errorf("grantAddressees(bridge grants) = %v, %v; want %q and no error", got, err, a2aBridgeAddressee)
	}
}

// Every fixed-name addressee the install routes to is reserved: each one the
// bridge's grants name, the gateway's default addressee (unless it is the
// RouteSession sentinel, see configuredAddressees), and the bridge's profile
// default. The check is containment, not equality, so the edit
// a2a/docs/hermes-bridge.md prescribes for an install that overrides
// BRIDGE_PROFILE (widen bridgeIdentity() and add the addressee to
// a2aReservedAddressees() in the same change) passes without a test edit, and
// widening the grant without the reservation reds here.
//
// BRIDGE_PROFILE is read from the a2a module's default only. The operator never
// renders it on the bridge container (it is a CR-declared sidecar env), so
// there is no rendered value to prefer. An install that overrides it is held
// through the grant instead: the override only works once the grant names the
// new addressee, and then the grant check above covers it.
func TestTheCalloutReservesTheConfiguredAddressees(t *testing.T) {
	agent := a2aTestAgent()
	got := renderedCalloutReservedAddressees(t, agent)

	// Today's value, pinned so the derivation below cannot drift to a set
	// that no longer includes the addressee every stock turn goes to.
	if !slices.Contains(got, a2aBridgeAddressee) {
		t.Errorf("%s = %v does not contain %q, the addressee every stock turn is routed to", a2aCalloutReservedAddresseesEnvVar, got, a2aBridgeAddressee)
	}

	gateway := buildA2AGatewayDeployment(agent).Spec.Template.Spec.Containers[0]
	gatewayAddressee, rendered := envValue(gateway, gatewayDefaultAddresseeEnv)
	if !rendered {
		gatewayAddressee = envDefaultInSource(t, a2aGatewayConfigSource, gatewayDefaultAddresseeEnv)
	}
	bridgeProfile := envDefaultInSource(t, a2aBridgeMainSource, bridgeProfileEnv)

	for _, w := range configuredAddressees(t, gatewayAddressee, bridgeProfile) {
		if !slices.Contains(got, w.addressee) {
			t.Errorf("%s = %v does not reserve %q (%s); a narrowed pod named %q would be handed its task subjects. "+
				"Add it to a2aReservedAddressees() in the same change.",
				a2aCalloutReservedAddresseesEnvVar, got, w.addressee, w.from, w.addressee)
		}
	}
}

type configuredAddressee struct{ addressee, from string }

// configuredAddressees is every fixed-name addressee the install routes to:
// each one the bridge's grants name, the gateway's default addressee and the
// bridge's profile default. A gateway default of the RouteSession sentinel is
// left out: it routes to session pods under minted names, not to an addressee.
func configuredAddressees(t *testing.T, gatewayAddressee, bridgeProfile string) []configuredAddressee {
	t.Helper()
	var want []configuredAddressee
	for _, a := range bridgeGrantAddressees(t) {
		want = append(want, configuredAddressee{a, "named by the bridge's grants"})
	}
	if gatewayAddressee != stringConstInSource(t, a2aGatewaySpawnSource, gatewayRouteSessionConst) {
		want = append(want, configuredAddressee{gatewayAddressee, "the gateway's " + gatewayDefaultAddresseeEnv})
	}
	want = append(want, configuredAddressee{bridgeProfile, "the bridge's " + bridgeProfileEnv + " default"})
	return want
}

// The day the gateway's default flips to the session route, the derivation
// must not demand that the sentinel be reserved: it is not an addressee, and a
// list that grew to carry it would refuse a pod for a name nothing routes to.
func TestTheSessionRouteIsNotAConfiguredAddressee(t *testing.T) {
	routeSession := stringConstInSource(t, a2aGatewaySpawnSource, gatewayRouteSessionConst)
	bridgeProfile := envDefaultInSource(t, a2aBridgeMainSource, bridgeProfileEnv)
	var got []string
	for _, a := range configuredAddressees(t, routeSession, bridgeProfile) {
		got = append(got, a.addressee)
	}
	if slices.Contains(got, routeSession) {
		t.Errorf("with %s=%q the configured addressees are %v; %q is the session-route sentinel, not an addressee",
			gatewayDefaultAddresseeEnv, routeSession, got, routeSession)
	}
	if !slices.Contains(got, a2aBridgeAddressee) {
		t.Errorf("with %s=%q the configured addressees are %v and lost %q, which the bridge's grants still name",
			gatewayDefaultAddresseeEnv, routeSession, got, a2aBridgeAddressee)
	}
}

// The a2a module's callout tests take their reserved addressees from this
// fixture, so a render change reaches them only if the fixture moves with it.
func TestRenderedReservedAddresseesMatchTheCalloutFixture(t *testing.T) {
	got := strings.Join(renderedCalloutReservedAddressees(t, a2aTestAgent()), a2aReservedAddresseesSeparator) + "\n"
	path := filepath.Clean(reservedAddresseesFixturePath)
	if *updateAuthMapFixture {
		if err := os.WriteFile(path, []byte(got), 0o644); err != nil {
			t.Fatalf("writing fixture: %v", err)
		}
		t.Logf("wrote %s", path)
		return
	}
	want, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("reading the reserved addressees fixture: %v\nRegenerate with: go test ./internal/controller/ -run TestRenderedReservedAddresseesMatchTheCalloutFixture -update", err)
	}
	if string(want) != got {
		t.Errorf("the rendered %s is %q and the fixture the callout suite refuses against is %q.\n"+
			"Regenerate with: go test ./internal/controller/ -run TestRenderedReservedAddresseesMatchTheCalloutFixture -update",
			a2aCalloutReservedAddresseesEnvVar, got, want)
	}
}
