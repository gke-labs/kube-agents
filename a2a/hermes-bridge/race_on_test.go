//go:build race

package hermesbridge

// raceEnabled reports a -race build, whose instrumentation slows tight loops
// by an order of magnitude; wall-clock budgets in tests scale by it.
const raceEnabled = true
