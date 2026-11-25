package workeradapter

import (
	"errors"
	"fmt"
	"strings"
	"time"
)

// procState is the state field of a /proc/<pid>/stat line, which follows
// the parenthesised command name; the name itself may hold spaces and
// parentheses, so the last ')' ends it.
func procState(stat string) (byte, error) {
	i := strings.LastIndexByte(stat, ')')
	if i < 0 || i+2 >= len(stat) {
		return 0, errors.New("no state field")
	}
	return stat[i+2], nil
}

// otherProcesses lists the live processes in the pid namespace besides the
// adapter (leftovers_linux.go); a variable so a test can scope it.
var otherProcesses = listOtherProcesses

// sweepSettle bounds how long sweepAfterTurn waits for processes its kill
// reached to be gone; sweepPoll is the interval.
var (
	sweepSettle = 2 * time.Second
	sweepPoll   = 20 * time.Millisecond
)

// sweepAfterTurn ends every process a turn left behind and then checks
// that none is left, for a harness that runs in the adapter's process,
// where no harness process exits at the end of a turn to trigger
// killLeftovers. Call it after each turn and between the tasks of a reused
// pod; an error means the next turn must not start. It needs the adapter to
// be the container's init: only then does the namespace-wide kill reach a
// process that left its process group (a shell's `nohup x &`, an MCP
// server's own child), and only then is "no other process" the whole
// picture.
func sweepAfterTurn() error {
	if pid := adapterPID(); pid != containerInitPID {
		return fmt.Errorf("ending a turn's processes needs the adapter as the container's init (pid %d, not %d)", containerInitPID, pid)
	}
	killEveryoneButMe()
	reapKilledChildren(leftoverReapBound)
	return checkTurnBoundary()
}

// checkTurnBoundary is the second half of sweepAfterTurn on its own: the
// adapter is the container's init and no other process is alive, waiting
// briefly for any a kill just reached. A driver that takes one turn per
// process calls it before each spawn (PrepareTurn), after the last
// process's exit has run killLeftovers.
func checkTurnBoundary() error {
	if pid := adapterPID(); pid != containerInitPID {
		return fmt.Errorf("starting a turn needs the adapter as the container's init (pid %d, not %d)", containerInitPID, pid)
	}
	var others []int
	for deadline := time.Now().Add(sweepSettle); ; {
		var err error
		if others, err = otherProcesses(); err != nil {
			return fmt.Errorf("checking for processes left from the turn: %w", err)
		}
		if len(others) == 0 || time.Now().After(deadline) {
			break
		}
		time.Sleep(sweepPoll)
	}
	if len(others) > 0 {
		return fmt.Errorf("%d processes outlived the turn (pids %v)", len(others), others)
	}
	return nil
}
