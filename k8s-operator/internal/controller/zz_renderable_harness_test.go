package controller

// The worker image carries no harness until another is available (#6707), so
// renderableHarness is empty and the operator renders no profile worker. The
// package's tests exercise the rest of the profile render, so they run with
// one; TestNoProfileRendersAWorkerWithoutAnAvailableHarness clears it.
func init() {
	renderableHarness = "claude-code"
}
