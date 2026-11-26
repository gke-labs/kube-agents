//go:build !linux

package workeradapter

import (
	"errors"
	"runtime"
)

// listOtherProcesses has no way to see the pid namespace off Linux, so a
// driver that needs it refuses to prepare a turn there.
func listOtherProcesses() ([]int, error) {
	return nil, errors.New("listing the pid namespace's processes is not supported on " + runtime.GOOS)
}
