package main

import (
	"context"

	"github.com/go-steer/core-agent/v3/pkg/agent"
)

// defaultCompactOverBytes is the seeded history size past which a turn
// compacts the conversation before it runs: a quarter of the envelope cap,
// so a conversation compacts long before the cap could refuse it.
const defaultCompactOverBytes = 4 << 20

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

func (c *historyCompactor) ShouldCompact(context.Context, *agent.Agent) bool { return c.due }

func (c *historyCompactor) SummarizerInstruction(focus string) string {
	return c.inner.SummarizerInstruction(focus)
}
