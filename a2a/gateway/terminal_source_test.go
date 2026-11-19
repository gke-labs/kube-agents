package gateway

import (
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// A terminal on either supervisor form is the supervisor's (#5301), as the
// relay and the fan-out heal both book it; one on .events is the executor's.
func TestTerminalSourceReadsEitherSupervisorForm(t *testing.T) {
	for subject, want := range map[string]TerminalSource{
		lib.TaskSupervisorSubject("tse", "t1"):    TerminalFromSupervisor,
		lib.ProfileSupervisorSubject("tse", "t1"): TerminalFromSupervisor,
		lib.TaskEventsSubject("tse", "t1"):        TerminalFromExecutor,
	} {
		if got := terminalSourceOf(subject); got != want {
			t.Errorf("terminalSourceOf(%q) = %q, want %q", subject, got, want)
		}
	}
}
