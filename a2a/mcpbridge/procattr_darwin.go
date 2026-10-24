//go:build darwin

package mcpbridge

import "syscall"

// childProcAttr is the non-Linux variant: a private process group only (no parent-death signal).
func childProcAttr() *syscall.SysProcAttr {
	return &syscall.SysProcAttr{Setpgid: true}
}
