//go:build linux

package workeradapter

import "golang.org/x/sys/unix"

// HideFromSameUID marks this process not dumpable. The harness and every
// shell the agent runs share this process's uid; while this process is
// dumpable they could attach to it (where Yama allows), read or write its
// memory through /proc/<pid>/mem, and open its fds through /proc/<pid>/fd,
// which would let them change the trajectory capture. A process that isn't
// dumpable has those /proc entries owned by root. The harness this process
// starts is dumpable again after its execve, so nothing about it changes.
func HideFromSameUID() error {
	return unix.Prctl(unix.PR_SET_DUMPABLE, 0, 0, 0, 0)
}

func dumpable() (bool, error) {
	v, err := unix.PrctlRetInt(unix.PR_GET_DUMPABLE, 0, 0, 0, 0)
	return v != 0, err
}
