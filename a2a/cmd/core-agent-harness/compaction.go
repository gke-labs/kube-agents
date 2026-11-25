package main

import (
	"context"

	"github.com/go-steer/core-agent/v3/pkg/agent"
)

// defaultCompactOverBytes is the seeded history size past which a turn
// compacts the conversation before it runs. At roughly four bytes a token,
// 512 KiB is about 130k tokens: under the long-context pricing cliffs and
// far under any model's window, so a turn never runs into the window before
// the history is summarised, and the summariser reads a history it can
// hold. The envelope cap is 32 times as large.
const defaultCompactOverBytes = 512 << 10

// historyCompactor compacts when the turn's seeded history is over the
// threshold. core-agent's default trigger reads the process's usage
// tracker, which a process that runs one turn never fills, so the trigger
// here is the size of the history the envelope carried. The summary prompt
// is core-agent's own.
type historyCompactor struct {
	due   bool
	inner agent.Compactor
}

func newHistoryCompactor(historyBytes, overBytes int) *historyCompactor {
	return &historyCompactor{due: historyBytes > overBytes, inner: agent.NewDefaultCompactor()}
}

// ShouldCompact is due once: after the summary, core-agent's own hooks ask
// again within the turn, and the history it now slices from is short.
func (c *historyCompactor) ShouldCompact(context.Context, *agent.Agent) bool {
	due := c.due
	c.due = false
	return due
}

func (c *historyCompactor) SummarizerInstruction(focus string) string {
	return c.inner.SummarizerInstruction(focus)
}
