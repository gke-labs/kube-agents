//go:build !unix

package mcp

// lockFile is a no-op where flock is unavailable; the atomic rename on save
// still keeps each file whole, and only concurrent writers can lose updates.
func lockFile(string) (func(), error) {
	return func() {}, nil
}
