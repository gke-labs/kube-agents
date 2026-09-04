package main

import (
	"flag"
	"testing"
)

// The first shape of this CLI parsed flags with the topic name still in the
// argument list, so `topics write upgrade-readiness --text ...` silently
// parsed zero flags: Go's flag package stops at the first non-flag argument.
// It failed on the venue, in the seed Job, which is a slow way to find out.
func TestTopicNameParsesFromEitherPosition(t *testing.T) {
	cases := []struct {
		name     string
		args     []string
		wantName string
		wantText string
	}{
		{"name first", []string{"upgrade-readiness", "--text", "hello"}, "upgrade-readiness", "hello"},
		{"flags first", []string{"--text", "hello", "upgrade-readiness"}, "upgrade-readiness", "hello"},
		{"name only", []string{"blueprint"}, "blueprint", ""},
		{"single-dash flag", []string{"annotations", "-text", "hi"}, "annotations", "hi"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			pre, rest := splitTopicName(tc.args)
			fs := flag.NewFlagSet("test", flag.ContinueOnError)
			text := fs.String("text", "", "")
			if err := fs.Parse(rest); err != nil {
				t.Fatalf("parse: %v", err)
			}
			got, err := topicName(pre, fs, "test")
			if err != nil {
				t.Fatalf("topicName: %v", err)
			}
			if got != tc.wantName {
				t.Errorf("topic name %q, want %q", got, tc.wantName)
			}
			if *text != tc.wantText {
				t.Errorf("--text %q, want %q", *text, tc.wantText)
			}
		})
	}
}

func TestTopicNameRejectsAmbiguity(t *testing.T) {
	for _, args := range [][]string{
		{},                                 // no name at all
		{"blueprint", "upgrade-readiness"}, // two names
		{"--text", "hi"},                   // flags but no name
	} {
		pre, rest := splitTopicName(args)
		fs := flag.NewFlagSet("test", flag.ContinueOnError)
		fs.String("text", "", "")
		if err := fs.Parse(rest); err != nil {
			t.Fatalf("parse %v: %v", args, err)
		}
		if _, err := topicName(pre, fs, "test"); err == nil {
			t.Errorf("expected %v to be refused", args)
		}
	}
}
