//go:build !linux

package workeradapter

// unprotectProcess is a no-op where protectProcess is one.
func unprotectProcess() error { return nil }
