//go:build race

package answerscrub

// raceEnabled reports a -race build, whose instrumentation slows tight loops
// by an order of magnitude; time budgets in tests scale by it.
const raceEnabled = true
