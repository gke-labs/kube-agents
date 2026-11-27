//go:build !linux

package workeradapter

// HideFromSameUID is Linux-only; elsewhere (a developer's machine) it does
// nothing.
func HideFromSameUID() error { return nil }

func dumpable() (bool, error) { return true, nil }
