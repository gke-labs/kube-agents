//go:build !linux

package workeradapter

// protectProcess has nothing to set off Linux, where the worker never runs.
func protectProcess() error { return nil }
