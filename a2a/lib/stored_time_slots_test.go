package lib

// The package's tests read many pre-cutoff terminals back to back, each on a
// fresh server, so a stored-time read slot goes back as soon as its read
// returns rather than after the five-second threshold a live consumer needs
// (lastBySubjectHold). The tests of the bound itself set their own hold.
func init() {
	lastBySubjectHold = func(release func()) { release() }
}
