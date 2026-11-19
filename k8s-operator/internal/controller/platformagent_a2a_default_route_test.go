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

// spec.harness.tuning.defaultRoute: one resolver, two containers.
//
// The gateway routes a plain conversation's first message by
// A2A_DEFAULT_ADDRESSEE, and the platform agent container carries the same
// value so the agent knows which route the gateway takes. Both are rendered
// from resolveA2ADefaultRoute; these tests fail if either side stops reading it.

import (
	"slices"
	"testing"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// defaultRouteAgent is the A2A test agent with defaultRoute set, or left
// unset when route is nil.
func defaultRouteAgent(route *string) *agentv1alpha1.PlatformAgent {
	agent := a2aTestAgent()
	agent.Spec.Harness = &agentv1alpha1.HarnessSpec{Tuning: &agentv1alpha1.TuningSpec{DefaultRoute: route}}
	return agent
}

func TestResolveA2ADefaultRoute(t *testing.T) {
	for _, tc := range []struct {
		name  string
		agent *agentv1alpha1.PlatformAgent
		want  string
	}{
		{"no harness", a2aTestAgent(), agentv1alpha1.DefaultRoutePlatform},
		{"no tuning", func() *agentv1alpha1.PlatformAgent {
			a := a2aTestAgent()
			a.Spec.Harness = &agentv1alpha1.HarnessSpec{}
			return a
		}(), agentv1alpha1.DefaultRoutePlatform},
		{"unset", defaultRouteAgent(nil), agentv1alpha1.DefaultRoutePlatform},
		{"platform", defaultRouteAgent(ptr.To(agentv1alpha1.DefaultRoutePlatform)), agentv1alpha1.DefaultRoutePlatform},
		{"session", defaultRouteAgent(ptr.To(agentv1alpha1.DefaultRouteSession)), agentv1alpha1.DefaultRouteSession},
		// Past the CRD's enum (an older CRD, a direct etcd write): never an
		// addressee no executor owns.
		{"out of schema", defaultRouteAgent(ptr.To("someone-else")), agentv1alpha1.DefaultRoutePlatform},
		{"empty", defaultRouteAgent(ptr.To("")), agentv1alpha1.DefaultRoutePlatform},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if got := resolveA2ADefaultRoute(tc.agent); got != tc.want {
				t.Errorf("resolveA2ADefaultRoute = %q, want %q", got, tc.want)
			}
		})
	}
}

// The two values the field takes are the a2a module's: `session` is the
// gateway's RouteSession sentinel and `platform` its own default for
// A2A_DEFAULT_ADDRESSEE. Read from the source, since this module cannot
// import that one, so a renamed sentinel or a moved default reds here rather
// than rendering a value the gateway reads as a literal addressee.
func TestDefaultRouteValuesAreTheGatewaysOwn(t *testing.T) {
	if got := stringConstInSource(t, a2aGatewaySpawnSource, gatewayRouteSessionConst); got != agentv1alpha1.DefaultRouteSession {
		t.Errorf("the gateway's %s is %q, DefaultRouteSession is %q", gatewayRouteSessionConst, got, agentv1alpha1.DefaultRouteSession)
	}
	if got := envDefaultInSource(t, a2aGatewayConfigSource, a2aDefaultAddresseeEnvVar); got != agentv1alpha1.DefaultRoutePlatform {
		t.Errorf("the gateway's %s default is %q, DefaultRoutePlatform is %q; unset defaultRoute must keep the gateway where it was",
			a2aDefaultAddresseeEnvVar, got, agentv1alpha1.DefaultRoutePlatform)
	}
}

// renderedDefaultRoutes reads A2A_DEFAULT_ADDRESSEE off the gateway container
// and the platform agent container, failing unless each carries it exactly once.
func renderedDefaultRoutes(t *testing.T, agent *agentv1alpha1.PlatformAgent, plugins []*agentv1alpha1.AgentPlugin) (gateway, platformAgent string) {
	t.Helper()
	gw := buildA2AGatewayDeployment(agent).Spec.Template.Spec.Containers[0]
	pa := containerNamed(t, buildDeployment(agent, "h1", "h2", "h3", "h4", plugins, renderOptions{imageVolumeSupported: true}), platformAgentContainerName)
	for _, c := range []corev1.Container{gw, pa} {
		v, n := envValueCount(c.Env, a2aDefaultAddresseeEnvVar)
		if n != 1 {
			t.Fatalf("container %s carries %s %d times, want once", c.Name, a2aDefaultAddresseeEnvVar, n)
		}
		if c.Name == gw.Name {
			gateway = v
		} else {
			platformAgent = v
		}
	}
	return gateway, platformAgent
}

// Both containers carry the resolved value, for every setting. A change that
// moved only one side (a literal on the gateway, a second resolver for the
// agent) reds here on the row where the two would part.
func TestTheGatewayAndThePlatformAgentCarryTheSameDefaultRoute(t *testing.T) {
	for _, route := range []*string{nil, ptr.To(agentv1alpha1.DefaultRoutePlatform), ptr.To(agentv1alpha1.DefaultRouteSession)} {
		agent := defaultRouteAgent(route)
		want := resolveA2ADefaultRoute(agent)
		gateway, platformAgent := renderedDefaultRoutes(t, agent, nil)
		if gateway != want || platformAgent != want {
			t.Errorf("defaultRoute %v: gateway %s = %q, platform agent %s = %q; both must be %q",
				ptr.Deref(route, "<unset>"), a2aDefaultAddresseeEnvVar, gateway, a2aDefaultAddresseeEnvVar, platformAgent, want)
		}
	}
	// The flip is visible on both: unset and session render differently.
	unsetGW, unsetPA := renderedDefaultRoutes(t, defaultRouteAgent(nil), nil)
	sessGW, sessPA := renderedDefaultRoutes(t, defaultRouteAgent(ptr.To(agentv1alpha1.DefaultRouteSession)), nil)
	if unsetGW == sessGW || unsetPA == sessPA {
		t.Errorf("unset renders (%q, %q) and session renders (%q, %q); the field moves neither side", unsetGW, unsetPA, sessGW, sessPA)
	}
}

// spec.mode is deprecated and ignored: every install runs the gateway, so the
// value is rendered whatever the field says, and a stored `today` (which the
// operator patches to `next` on its first pass) renders the same as `next`.
// There is no mode under which the gateway exists and the env does not.
func TestTheDefaultRouteIsRenderedWhateverSpecModeSays(t *testing.T) {
	for _, mode := range []*string{nil, ptr.To(agentv1alpha1.PlatformAgentModeNext), ptr.To("today")} {
		agent := defaultRouteAgent(ptr.To(agentv1alpha1.DefaultRouteSession))
		agent.Spec.Mode = mode
		gateway, platformAgent := renderedDefaultRoutes(t, agent, nil)
		if gateway != agentv1alpha1.DefaultRouteSession || platformAgent != agentv1alpha1.DefaultRouteSession {
			t.Errorf("mode %v: gateway %q, platform agent %q; want %q on both", ptr.Deref(mode, "<unset>"), gateway, platformAgent, agentv1alpha1.DefaultRouteSession)
		}
	}
}

// An AgentPlugin's spec.env reaches the agent container verbatim. One naming
// A2A_DEFAULT_ADDRESSEE must not tell the agent a route the gateway does not
// take: the operator's entry is appended after the merge and wins.
func TestAnAgentPluginCannotOverrideTheAgentsDefaultRoute(t *testing.T) {
	plugins := []*agentv1alpha1.AgentPlugin{{
		ObjectMeta: metav1.ObjectMeta{Name: "route-writer"},
		Spec: agentv1alpha1.AgentPluginSpec{
			Image: "gcr.io/route-writer:v1",
			Env:   []corev1.EnvVar{{Name: a2aDefaultAddresseeEnvVar, Value: agentv1alpha1.DefaultRouteSession}},
		},
	}}
	gateway, platformAgent := renderedDefaultRoutes(t, defaultRouteAgent(nil), plugins)
	if platformAgent != gateway {
		t.Errorf("with a plugin naming %s, the platform agent carries %q and the gateway %q", a2aDefaultAddresseeEnvVar, platformAgent, gateway)
	}
}

// The session route's name is reserved beside the bridge's addressee: the
// callout refuses a narrowed pod named for it. The name is read from the
// gateway's source, so a renamed sentinel moves the reservation with it.
func TestTheSessionRouteIsAReservedAddressee(t *testing.T) {
	routeSession := stringConstInSource(t, a2aGatewaySpawnSource, gatewayRouteSessionConst)
	if !slices.Contains(a2aReservedAddressees(), routeSession) {
		t.Errorf("a2aReservedAddressees() = %v does not reserve the session route %q", a2aReservedAddressees(), routeSession)
	}
	got := renderedCalloutReservedAddressees(t, a2aTestAgent())
	if !slices.Contains(got, routeSession) {
		t.Errorf("%s = %v does not reserve the session route %q", a2aCalloutReservedAddresseesEnvVar, got, routeSession)
	}
	if a2aCanDrainAs(routeSession, nil) {
		t.Errorf("a2aCanDrainAs(%q) = true; a reserved addressee is never a draining profile's name", routeSession)
	}
}
