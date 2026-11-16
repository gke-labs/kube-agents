package main

import (
	"bytes"
	"context"
	"errors"
	"log/slog"
	"strings"
	"testing"
	"time"
)

func TestParseSince(t *testing.T) {
	for in, want := range map[string]time.Duration{"": 0, "7d": 7 * 24 * time.Hour, "36h": 36 * time.Hour} {
		got, err := parseSince(in)
		if err != nil || got != want {
			t.Errorf("parseSince(%q) = %v, %v; want %v", in, got, err, want)
		}
	}
	for _, in := range []string{"0d", "-1d", "xd", "soon", "-2h", "106752d", "213504d"} {
		if _, err := parseSince(in); err == nil {
			t.Errorf("parseSince(%q) must refuse", in)
		}
	}
}

func TestFeedbackCommandIsRecognisedAndNeedsExport(t *testing.T) {
	if !isFeedbackCommand([]string{"feedback", "export"}) || isFeedbackCommand(nil) || isFeedbackCommand([]string{"--help"}) {
		t.Error("only a first argument of feedback selects the subcommand")
	}
	var errOut bytes.Buffer
	err := feedbackMain(context.Background(), []string{"feedback"}, &bytes.Buffer{}, &errOut, slog.Default())
	if !errors.Is(err, errFeedbackUsage) || !strings.Contains(errOut.String(), "feedback export") {
		t.Errorf("feedback alone = %v, %q; want the usage", err, errOut.String())
	}
	errOut.Reset()
	err = feedbackMain(context.Background(), []string{"feedback", "export", "7d"}, &bytes.Buffer{}, &errOut, slog.Default())
	if !errors.Is(err, errFeedbackUsage) {
		t.Errorf("a window without --since = %v; want the usage, not a whole-bucket export", err)
	}
}
