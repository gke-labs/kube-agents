/*
Copyright 2026.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

// Package memlimit sets the Go runtime's soft memory limit from the memory
// limit of the container a process runs in.
//
// The operator's manifests hand each process its container's limits.memory
// through the Downward API, as a byte count in an environment variable of
// the process's own (the manager's OPERATOR_MEMORY_LIMIT_BYTES, the event
// watcher's EVENT_WATCHER_MEMORY_LIMIT_BYTES), so the soft limit follows
// whatever limit the cluster admitted, an Autopilot clamp included, with no
// second number to keep in step. Each process takes a share of it: the
// manager most of its container, the watcher half of one it shares. A soft
// limit makes the collector work harder as the heap approaches it; it does
// not cap live heap, so a process that genuinely needs more is still
// OOM-killed, later and after more collection.
//
// debug.SetMemoryLimit is the same knob GOMEMLIMIT turns. When GOMEMLIMIT is
// set the runtime has already applied it before main runs, and it is an
// explicit choice this package does not second-guess.
package memlimit

import (
	"fmt"
	"os"
	"runtime/debug"
	"strconv"
)

const (
	// GoMemLimitEnv is the Go runtime's own soft-limit variable.
	GoMemLimitEnv = "GOMEMLIMIT"
	// byteCountBase and byteCountBits are how the Downward API writes a
	// resource limit with the default divisor: a decimal integer of bytes.
	byteCountBase = 10
	byteCountBits = 64
)

// Derive decides the soft memory limit from the two variables that can set
// it: goMemLimit is GOMEMLIMIT's value and containerLimit the byte count the
// Downward API wrote to the variable named envName. It returns the limit in
// bytes and true when the caller should apply it, or zero, false and the
// reason when the runtime's own setting should stand: GOMEMLIMIT already set
// (the runtime applied it and it wins), the container limit absent (running
// outside the operator's manifests), or unparseable or non-positive (the
// value is not a byte count, so nothing is derived from it).
func Derive(envName, goMemLimit, containerLimit string, fraction float64) (int64, bool, string) {
	if goMemLimit != "" {
		return 0, false, fmt.Sprintf("%s=%s is set and takes precedence", GoMemLimitEnv, goMemLimit)
	}
	if containerLimit == "" {
		return 0, false, fmt.Sprintf("%s is not set; the runtime default applies", envName)
	}
	limitBytes, err := strconv.ParseInt(containerLimit, byteCountBase, byteCountBits)
	if err != nil || limitBytes <= 0 {
		return 0, false, fmt.Sprintf("%s=%q is not a positive byte count; the runtime default applies", envName, containerLimit)
	}
	return int64(float64(limitBytes) * fraction), true, ""
}

// Apply reads GOMEMLIMIT and the variable named envName from the environment,
// sets the runtime's soft memory limit to fraction of the container's when
// Derive says to, and returns what it did for the caller's startup log: the
// limit in bytes and true, or false and the reason it left the runtime alone.
func Apply(envName string, fraction float64) (int64, bool, string) {
	limitBytes, apply, reason := Derive(envName, os.Getenv(GoMemLimitEnv), os.Getenv(envName), fraction)
	if !apply {
		return 0, false, reason
	}
	debug.SetMemoryLimit(limitBytes)
	return limitBytes, true, ""
}
