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
everything downstream uses `renderMode`. Without the pair, fail-closed and
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
today's stack, and requeues. (Same pattern as `RuntimeClassNotFound`.)

## What the operator renders

- `today`: exactly what it renders now. A normal install cannot tell this feature exists.
- `next`: everything above, plus the NATS component and the gateway skeleton. Next is
  additive - today's path keeps running until stage 4 starts retiring pieces.

The operator also writes the mode into the managed settings it already renders
(`reconcileSettingsConfigMap`), as a single key: `KUBEAGENTS_MODE`. That is the only way
the mode reaches the agent runtime.

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
