package mcp

import (
	"bytes"
	"context"
	"fmt"
	"os/exec"
	"strings"
)

// CommandRunner abstracts external command execution for CLI helpers (e.g. gh) and testing.
type CommandRunner func(ctx context.Context, name string, args ...string) ([]byte, error)

// DefaultCommandRunner executes an external command with context cancellation.
func DefaultCommandRunner(ctx context.Context, name string, args ...string) ([]byte, error) {
	cmd := exec.CommandContext(ctx, name, args...)
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		errText := strings.TrimSpace(stderr.String())
		if errText != "" {
			return nil, fmt.Errorf("%s %s: %w (%s)", name, strings.Join(args, " "), err, errText)
		}
		return nil, fmt.Errorf("%s %s: %w", name, strings.Join(args, " "), err)
	}
	return stdout.Bytes(), nil
}
