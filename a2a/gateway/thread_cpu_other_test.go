//go:build !linux

package gateway

import "time"

// threadCPU has no per-thread CPU clock to read off Linux; callers skip.
func threadCPU(func()) (time.Duration, bool) { return 0, false }
