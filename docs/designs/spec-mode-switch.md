# Mode switch: `spec.mode`

- **Author:** [@bnaylor]
- **Date:** 2026-08-24
- **Status:** draft - stage 0.6, unblocks stage 1 packaging

## Purpose

Stage 1 lands the new stack dark: the NATS component, the A2A gateway skeleton, and the
client library all ship in the repo disabled. This spec defines the one switch that turns
them on. There is no feature-flag framework in this codebase, and this doc is not the
excuse to build one - the mechanism is one optional CRD field and one helper.

## The field

One optional enum on `PlatformAgentSpec`, next to `Harness` and `Integration`:

```go
// Mode selects which component stack the operator renders.  "today" is the
// current architecture.  "next" additionally renders the NATS and A2A
// gateway components, which are otherwise dark.  Absent means "today".
// +kubebuilder:validation:Enum=today;next
// +optional
Mode *string `json:"mode,omitempty"`
```

`mode: next` is a dev toggle, not a supported configuration. Same shape as the other
opt-in toggles on this CRD: optional pointer field, nil-safe helper, deliberately not
surfaced in the Helm chart. Naming note: the CRD already has a `mode` field on the
Google Chat integration spec (display verbosity, `spec.integration.googleChat.mode`).
Different path, no schema collision - named here so nobody conflates them.

## The helper

All reads go through one module. Nothing touches `Spec.Mode` outside it - not the rest
of the operator, and especially not agent-side code (below). The module exposes a pair:

```go
// resolveMode validates the spec's mode.  Absent is (ModeToday, nil).  A value
// this binary does not recognize returns an error - the reconciler's cue to go
// Degraded rather than silently render something else.
func resolveMode(agent *agentv1alpha1.PlatformAgent) (Mode, error)

// renderMode reports the mode for one component.  Nil-safe and fail-closed:
// absent or unrecognized is ModeToday.  For call sites past the reconciler's
// validation gate, which only need the answer.
func renderMode(agent *agentv1alpha1.PlatformAgent, component string) Mode
```

The reconciler calls `resolveMode` once at the top and handles the error (below);
everything downstream uses `renderMode`, with one deliberate carve-out defined with
the skew behavior below: the agent's A2A surface takes its skew answer from the
reconciler's error handling, not from `renderMode` - fail-closed rendering at those
call sites would tear down a live bus. Without the pair, fail-closed and
Degraded-on-skew are mutually exclusive - a single helper that maps unrecognized to
`ModeToday` leaves the reconciler no way to notice the skew without reading `Spec.Mode`
itself. Fail-closed here means the dark stack stays dark. The `component` argument is
ignored today - `renderMode` returns the global mode regardless. It exists so that
per-feature graduation (sketched below) changes the helper and not the call sites.

**An unrecognized value is refused, not swallowed.** Enum validation rejects bad values
at admission, so the only way `resolveMode` sees one is version skew - a newer CRD adds a
third mode, an older operator binary reads it. Silently rendering `today` at that point means
the cluster runs something other than what the spec asks, with nothing in
`kubectl describe` to say so. Instead the reconciler goes Degraded through the existing
`updateStatusDegraded` path with a named reason, `ModeNotRecognized`, keeps rendering
today's stack, and requeues. It reaches Degraded by a different route from
`RuntimeClassNotFound`, and the difference is the point: that check returns early, so
nothing downstream of it renders, while the mode check is evaluated at the top and its
error CARRIED - every render step still runs, including the workload, and Degraded is
reported at the end instead of Ready. A skew that returned early would neither pin the
managed `.env` nor move the config hash, leaving the running fleet on `next` behavior
with only a status message to say otherwise. And the
two layers the mode touches are split deliberately on skew. The mode DELIVERED to the
agent fails closed: the managed `.env` pins `today`, the config hash moves, and the
fleet rolls to today's behavior - the skill is withdrawn, which is what fail-closed
means. The RENDERED surface is preserved, and not by accident of the helper contract:
`renderMode`'s fail-closed answer would stop emitting the bus env and the egress
rule, and this operator deletes policies it stops rendering - so the reconciler, the
one place that sees the skew through `resolveMode`'s error, arms the preservation
carve-out named in the helper section, and the A2A objects and the agent's bus
surface (container-env credentials, the 4222 egress rule) render through the skew
rather than drop. The preservation matters most on the `next` side: "fail closed to
today" must not mean "clean up next," or a one-version operator rollback against a
live `next` install kills the bus while dutifully reporting Degraded. The behavior
rollout does replace the agent pods; what preservation guarantees is that the
replacements keep the credential and the route, so the bridge reconnects instead of
hanging at the dial. Found live during stage 1 bring-up (8/26).

## What the operator renders

- `today`: exactly what it renders now. A normal install cannot tell this feature exists.
- `next`: everything above, plus the NATS component and the gateway skeleton. Next is
  additive - today's path keeps running until stage 4 starts retiring pieces.

One thing `next` does not ship: long-term audit. The stream is a 72h ring buffer and
the audit exporter is stage 2 scope, so `next` has no archive - the NATS spec's audit
section describes the design, not what this toggle turns on. Dev posture; don't run
traffic that matters on it and expect audit to exist.

The operator also writes the mode into the managed settings it already renders, as a
single key: `KUBEAGENTS_MODE`. (Amended 8/26: the draft said
`reconcileSettingsConfigMap`, but the surface with env semantics and agent-write
protection is the managed `.env` - `renderManagedEnv`, applied last, refused by
`save_env_value` - so the key rides that and the config-hash rollout annotation. The
agent cannot fake its own mode, which the draft's route would not have given.) That is
the only way the mode reaches the agent runtime.

A mode change is a rollout, not a hot reload. Kubernetes does not restart running pods
when a ConfigMap changes, so the operator stamps the rendered config's hash onto the
agent pod template (the `kubeagents.x-k8s.io/config-hash` annotation, which already
covers the ConfigMap carrying the managed `.env`) - flipping the mode rolls the Deployment,
and no agent keeps running in a mode the spec no longer asks for. Without the stamp,
`mode: next` would produce a silent split-brain: NATS up, the running fleet still on
today's path until something happens to kill its pods.

## Agent-side rule

Agent-side code asks one helper in the shared settings module - `runtime_mode.is_next()`
or equivalent - and that helper reads the managed key. No component reads its own env
var. A grep for `KUBEAGENTS_MODE` should hit exactly two places: the operator builder
that writes it and the helper that reads it. A third hit is a review comment.

"What mode am I in" gets one answer per agent, computed in one place. This also means the
delivery mechanism can change later without touching call sites.

## Not in the Helm chart

The chart does not template `mode` until graduation (stage 4, when `next` becomes the
default posture). Until then, flipping it is a `kubectl patch` on the PlatformAgent CR.
Helm 3's three-way merge leaves fields the chart never sets alone, so a patched mode
should survive chart upgrades.

## Per-feature overrides - sketched, not built

If a component later needs to graduate separately, the shape is a sibling map consulted by
the same helper, override beats global:

```yaml
mode: next
modeOverrides:
  subagents: today
```

Keys are component names (`nats`, `gateway`, `subagents`, `cron`). We are not building
this now - no field, no CRD change. The sketch exists to justify `renderMode`'s component
argument, and to stop a second mechanism getting invented when the need shows up. If the
need never shows up, `mode` stays a single switch and the map never exists.
