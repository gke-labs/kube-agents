//go:build !race

package dispatch

// raceEnabled says the tests run under the race detector, which slows a
// bus round trip several times over.
const raceEnabled = false
