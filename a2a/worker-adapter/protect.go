package workeradapter

// ProtectProcess marks the adapter non-dumpable (protectProcess), for the
// worker's entry point to call once it knows the harness: then a process a
// turn starts, as the adapter's user, can't read the adapter's environment
// or memory through /proc, or attach to it.
func ProtectProcess() error { return protectProcess() }
