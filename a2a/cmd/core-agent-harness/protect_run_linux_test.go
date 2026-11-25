//go:build linux

package main

import (
	"bytes"
	"context"
	"strings"
	"testing"

	"golang.org/x/sys/unix"
)

// run makes the process non-dumpable before it reads its envelope.
func TestRunProtectsTheProcessFirst(t *testing.T) {
	if err := unix.Prctl(unix.PR_SET_DUMPABLE, 1, 0, 0, 0); err != nil {
		t.Skip("cannot reset dumpable:", err)
	}
	var out, errOut bytes.Buffer
	run(context.Background(), []string{"--model", "m"}, strings.NewReader("not json\n"), &out, &errOut)
	d, err := unix.PrctlRetInt(unix.PR_GET_DUMPABLE, 0, 0, 0, 0)
	if err != nil {
		t.Fatal(err)
	}
	if d != 0 {
		t.Errorf("dumpable = %d after run; want 0", d)
	}
}
