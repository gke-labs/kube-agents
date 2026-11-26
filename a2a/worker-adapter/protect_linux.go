//go:build linux

package workeradapter

import "syscall"

// prctl options (linux/prctl.h).
const (
	prGetDumpable = 3
	prSetDumpable = 4
)

// protectProcess marks the adapter non-dumpable, so a process a turn starts
// as the same user cannot read the adapter's environment or memory through
// /proc (environ, mem, maps) or attach to it with ptrace: the kernel makes
// those files root's once the process is not dumpable. The adapter calls it
// at startup, before any harness or tool runs.
func protectProcess() error {
	if _, _, errno := syscall.RawSyscall(syscall.SYS_PRCTL, prSetDumpable, 0, 0); errno != 0 {
		return errno
	}
	return nil
}

// processDumpable reports the adapter's dumpable flag, for the test.
func processDumpable() (bool, error) {
	r, _, errno := syscall.RawSyscall(syscall.SYS_PRCTL, prGetDumpable, 0, 0)
	if errno != 0 {
		return false, errno
	}
	return r != 0, nil
}
