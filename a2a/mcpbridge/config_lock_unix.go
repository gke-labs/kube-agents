//go:build unix

package mcpbridge

import (
	"os"
	"syscall"
)

// LockFile takes an exclusive advisory lock on path (creating it), so the
// read-modify-write of shared state files is serialized across processes.
// The returned func releases it.
func LockFile(path string) (func(), error) {
	f, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR, FilePermMode)
	if err != nil {
		return nil, err
	}
	if err := syscall.Flock(int(f.Fd()), syscall.LOCK_EX); err != nil {
		_ = f.Close()
		return nil, err
	}
	return func() {
		_ = syscall.Flock(int(f.Fd()), syscall.LOCK_UN)
		_ = f.Close()
	}, nil
}
