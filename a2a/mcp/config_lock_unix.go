//go:build unix

package mcp

import (
	"os"
	"syscall"
)

// lockFile takes an exclusive advisory lock on path (creating it), so the
// read-modify-write of the config and event files is serialized across the
// processes that share them: every Antigravity conversation's MCP server and each
// `kube-agents-mcp --hook` run. The returned func releases it.
func lockFile(path string) (func(), error) {
	f, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR, filePermMode)
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
