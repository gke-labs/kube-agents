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

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// This module is the only reader of PlatformAgentSpec.Mode. Everything else —
// the rest of the operator, and especially agent-side code — asks resolveMode
// or renderMode. Design: docs/designs/spec-mode-switch.md.

// Mode names a component stack the operator can render.
type Mode string

const (
	// ModeToday is the current architecture — what a normal install runs.
	ModeToday Mode = "today"
	// ModeNext additionally renders the NATS and A2A gateway components,
	// which are otherwise dark.
	ModeNext Mode = "next"
)

// resolveMode validates the spec's mode. Absent is (ModeToday, nil). A value
// this binary does not recognize returns an error — the reconciler's cue to go
// Degraded (reason ModeNotRecognized) rather than silently render something
// else. Enum validation rejects bad values at admission, so an error here means
// version skew: a newer CRD added a mode this operator build has never heard of.
func resolveMode(agent *agentv1alpha1.PlatformAgent) (Mode, error) {
	if agent == nil || agent.Spec.Mode == nil {
		return ModeToday, nil
	}
	switch mode := Mode(*agent.Spec.Mode); mode {
	case ModeToday, ModeNext:
		return mode, nil
	default:
		return ModeToday, fmt.Errorf("spec.mode %q is not recognized by this operator build", *agent.Spec.Mode)
	}
}

// renderMode reports the mode for one component. Nil-safe and fail-closed:
// absent or unrecognized is ModeToday, so the dark stack stays dark. For call
// sites past the reconciler's validation gate, which only need the answer.
//
// The component argument is ignored today — renderMode returns the global mode
// regardless. It exists so that per-feature graduation (the modeOverrides
// sketch in the spec) changes this helper and not the call sites.
func renderMode(agent *agentv1alpha1.PlatformAgent, _ string) Mode {
	mode, err := resolveMode(agent)
	if err != nil {
		return ModeToday
	}
	return mode
}

// a2aAgentSurface reports whether the AGENT pod carries its half of the A2A
// wiring: the bus credentials in its env and the 4222 egress rule on its
// NetworkPolicy. Next, yes; today, no — and on version skew, yes, which is
// where this differs from renderMode and why it exists.
//
// The reconciler freezes the A2A objects on skew rather than cleaning them up
// (spec-mode-switch.md: "fail closed to today" must not mean "clean up next",
// or an operator rollback kills a live bus). Fail-closed on the agent side
// would break the same promise from the other end: a frozen, running bus with
// an agent pod that just lost its credentials and its egress rule, hanging to
// the dial timeout — the least diagnosable shape this failure has.
//
// The asymmetry is deliberate. Rendering the surface on a today install that
// hit skew costs two inert env vars and an egress rule to pods that do not
// exist, on a CR already reporting Degraded/ModeNotRecognized; NOT rendering it
// on a next install that hit skew breaks a working bus. The dark-stack promise
// is not weakened either way: skew needs a newer CRD with a mode this build has
// never heard of, which no normal install has.
func a2aAgentSurface(agent *agentv1alpha1.PlatformAgent) bool {
	mode, err := resolveMode(agent)
	return err != nil || mode == ModeNext
}
