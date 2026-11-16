package gateway

import (
	"runtime"
	"syscall"
	"time"
)

// threadCPU runs f on one locked OS thread and returns the CPU time that
// thread spent in it: the work, without the time f waited for a core.
func threadCPU(f func()) (time.Duration, bool) {
	runtime.LockOSThread()
	defer runtime.UnlockOSThread()
	var before, after syscall.Rusage
	if syscall.Getrusage(syscall.RUSAGE_THREAD, &before) != nil {
		return 0, false
	}
	f()
	if syscall.Getrusage(syscall.RUSAGE_THREAD, &after) != nil {
		return 0, false
	}
	used := func(r syscall.Rusage) time.Duration {
		return time.Duration(r.Utime.Nano() + r.Stime.Nano())
	}
	return used(after) - used(before), true
}
