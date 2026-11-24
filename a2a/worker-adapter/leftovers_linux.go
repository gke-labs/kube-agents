//go:build linux

package workeradapter

import (
	"errors"
	"fmt"
	"io/fs"
	"os"
	"strconv"
)

// procFS is where otherProcesses reads the pid namespace's processes.
const procFS = "/proc"

// listOtherProcesses returns every live process in this pid namespace
// other than this one: zombies are already dead and are left out. Every
// process is listed whatever its user, since /proc shows a namespace's
// processes to all of them.
func listOtherProcesses() ([]int, error) {
	ents, err := os.ReadDir(procFS)
	if err != nil {
		return nil, err
	}
	self := os.Getpid()
	var out []int
	for _, e := range ents {
		pid, err := strconv.Atoi(e.Name())
		if err != nil || pid == self {
			continue
		}
		stat, err := os.ReadFile(procFS + "/" + e.Name() + "/stat") // #nosec G304 -- a /proc entry named by a pid
		if errors.Is(err, fs.ErrNotExist) {
			continue // exited between the listing and the read
		}
		if err != nil {
			return nil, fmt.Errorf("read %s/%d/stat: %w", procFS, pid, err)
		}
		state, err := procState(string(stat))
		if err != nil {
			return nil, fmt.Errorf("%s/%d/stat: %w", procFS, pid, err)
		}
		if state != 'Z' && state != 'X' {
			out = append(out, pid)
		}
	}
	return out, nil
}
