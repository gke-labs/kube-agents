package gateway

import (
	"archive/tar"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"slices"
	"strings"
	"time"

	"github.com/nats-io/nats.go/jetstream"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The export: every rating the feedback bucket holds, newest first, as a
// draft eval case an operator copies into bench/tasks/: a 👎 as a case the
// rated answer should fail, a 👍 as a good answer, a case it should pass (`/gateway feedback export`,
// run in the gateway's pod, which holds the bucket's only credentials). Each
// draft is a directory in a tar on the writer: task.yaml, a case skeleton per
// docs/designs/bench-case-format.md whose prompt is the rater's ask, and
// transcript.md, the exchange that was rated. A line per draft goes to the
// log writer.
const (
	feedbackCaseIDPrefix  = "feedback-"
	feedbackCaseDateForm  = "20060102"
	feedbackRatedDateForm = "2006-01-02"
	// feedbackCaseIDHash is how many hex digits of the record key's hash
	// tell two drafts from one day apart, and feedbackCaseIDHashLong how
	// many a draft takes when its short id is already in the export.
	feedbackCaseIDHash     = 4
	feedbackCaseIDHashLong = 16
	feedbackTaskFile       = "task.yaml"
	feedbackTranscript     = "transcript.md"
	feedbackFileMode       = 0o600
	feedbackDirMode        = 0o700
	// feedbackSummaryAskMax clips the ask on a draft's summary line.
	feedbackSummaryAskMax = 28
	feedbackNoNote        = "(no note)"
	feedbackUpMark        = "👍"
	feedbackDownMark      = "👎"
	// feedbackNoAsk stands in for an ask the session record no longer held
	// when the answer was rated (past AskTTL, or a reaped session).
	feedbackNoAsk = "TODO: the question was no longer on record when this answer was rated; take it from the conversation"

	// The characters JSON writes raw and YAML refuses in a stream, and the
	// two Unicode separators YAML reads as line breaks.
	yamlDEL      = '\u007f'
	yamlC1First  = '\u0080'
	yamlC1Last   = '\u009f'
	yamlNonchar1 = '\ufffe'
	yamlNonchar2 = '\uffff'
	yamlLineSep  = '\u2028'
	yamlParaSep  = '\u2029'
)

// yamlUnprintable escapes DEL and the C1 controls inside a double-quoted
// scalar.
var yamlUnprintable = func() *strings.Replacer {
	var pairs []string
	for r := yamlDEL; r <= yamlC1Last; r++ {
		pairs = append(pairs, string(r), fmt.Sprintf("\\u%04x", r))
	}
	for _, r := range []rune{yamlNonchar1, yamlNonchar2} {
		pairs = append(pairs, string(r), fmt.Sprintf("\\u%04x", r))
	}
	return strings.NewReplacer(pairs...)
}()

// ExportFeedback writes every rating kept in the last since (all of them when
// since is zero) to w as a tar of draft eval cases, and a line per draft to
// log. It returns how many it wrote.
func ExportFeedback(ctx context.Context, c *lib.Client, since time.Duration, now time.Time, w, log io.Writer) (int, error) {
	kv, err := c.KV(ctx, lib.FeedbackBucket)
	if err != nil {
		return 0, fmt.Errorf("feedback bucket: %w", err)
	}
	keys, err := kv.ListKeys(ctx)
	if err != nil {
		return 0, fmt.Errorf("feedback bucket: %w", err)
	}
	type kept struct {
		key string
		rec FeedbackRecord
	}
	var recs []kept
	for key := range keys.Keys() {
		rec, err := getFeedback(ctx, kv, key)
		if errors.Is(err, jetstream.ErrKeyNotFound) {
			continue
		}
		if err != nil {
			return 0, err
		}
		if since > 0 && rec.RatedAt.Before(now.Add(-since)) {
			continue
		}
		recs = append(recs, kept{key: key, rec: rec})
	}
	slices.SortFunc(recs, func(a, b kept) int { return b.rec.RatedAt.Compare(a.rec.RatedAt) })
	tw := tar.NewWriter(w)
	ids := map[string]bool{}
	for i, k := range recs {
		id := feedbackCaseID(k.key, k.rec.RatedAt, feedbackCaseIDHash)
		if ids[id] {
			// Two drafts would extract into one directory, the second
			// overwriting the first.
			id = feedbackCaseID(k.key, k.rec.RatedAt, feedbackCaseIDHashLong)
		}
		ids[id] = true
		files := []struct{ name, body string }{
			{id + "/" + feedbackTaskFile, feedbackCaseYAML(id, k.rec)},
			{id + "/" + feedbackTranscript, feedbackTranscriptMD(k.rec)},
		}
		if err := tw.WriteHeader(&tar.Header{Typeflag: tar.TypeDir, Name: id + "/", Mode: feedbackDirMode, ModTime: now}); err != nil {
			return i, err
		}
		for _, f := range files {
			if err := tw.WriteHeader(&tar.Header{Name: f.name, Mode: feedbackFileMode, Size: int64(len(f.body)), ModTime: now}); err != nil {
				return i, err
			}
			if _, err := io.WriteString(tw, f.body); err != nil {
				return i, err
			}
		}
		fmt.Fprintf(log, "%d %s · %s · “%s”\n%s\n%s\n", i+1, feedbackMark(k.rec), k.rec.RatedAt.Format(feedbackRatedDateForm),
			clipWords(k.rec.Ask, feedbackSummaryAskMax), files[0].name, files[1].name)
	}
	return len(recs), tw.Close()
}

// feedbackCaseID is a draft's id and directory: the day it was rated and
// digits hex digits of its record key's hash.
func feedbackCaseID(key string, ratedAt time.Time, digits int) string {
	sum := sha256.Sum256([]byte(key))
	return feedbackCaseIDPrefix + ratedAt.UTC().Format(feedbackCaseDateForm) + "-" + hex.EncodeToString(sum[:])[:digits]
}

// feedbackMark is the record's rating as the export shows it.
func feedbackMark(rec FeedbackRecord) string {
	if rec.Rating == FeedbackUp {
		return feedbackUpMark
	}
	return feedbackDownMark
}

// feedbackCaseYAML is the draft's task.yaml. The strings are written as
// JSON, which YAML reads as double-quoted scalars, so no ask needs escaping
// by hand.
func feedbackCaseYAML(id string, rec FeedbackRecord) string {
	good := rec.Rating == FeedbackUp
	note := rec.Note
	if note == "" {
		note = feedbackNoNote
	}
	var b strings.Builder
	fmt.Fprintf(&b, "# A candidate eval case from a %s on an answer, exported from an install\n", feedbackMark(rec))
	b.WriteString("# (`/gateway feedback export`). It is a draft: fill in every TODO before\n")
	b.WriteString("# submitting it, per bench/CONTRIBUTING.md and\n")
	b.WriteString("# docs/designs/bench-case-format.md. transcript.md beside it is the\n")
	b.WriteString("# exchange that was rated.\n#\n")
	if good {
		fmt.Fprintf(&b, "# Rated %s as a good answer: the checks should pass on it.\n", rec.RatedAt.UTC().Format(feedbackRatedDateForm))
	} else {
		fmt.Fprintf(&b, "# Rated %s. What the rater said was wrong:\n", rec.RatedAt.UTC().Format(feedbackRatedDateForm))
		for _, line := range strings.Split(note, "\n") {
			b.WriteString("#   " + yamlComment(line) + "\n")
		}
	}
	b.WriteString("#\n# Before submitting: make the prompt install-neutral (a cluster the\n")
	b.WriteString("# install named becomes a fixture role or {{CLUSTER_NAME}}), and leave\n")
	b.WriteString("# transcript.md out of the pull request: it holds the install's data.\n")
	fmt.Fprintf(&b, "id: %s\n", id)
	fmt.Fprintf(&b, "name: %s\n", yamlString("TODO: what a right answer does"))
	fmt.Fprintf(&b, "prompt: %s\n", yamlString(feedbackAskOrTODO(rec)))
	expected := "TODO: what a right answer says. The rater said: " + note
	if good {
		expected = "TODO: what a right answer says. The rated answer in transcript.md was marked good."
	}
	fmt.Fprintf(&b, "expected_output: %s\n", yamlString(expected))
	b.WriteString("infrastructure:\n  deployer: noop\nvalidated: false\n\n")
	b.WriteString("# TODO: a slug from docs/designs/domains.yaml.\n# domain:\n\n")
	b.WriteString("# TODO: who answers when this case flakes (bench/CONTRIBUTING.md).\n# owner:\n\n")
	if good {
		b.WriteString("# TODO: the checks the rated answer passes and a wrong one fails\n")
	} else {
		b.WriteString("# TODO: the checks that red on the rated answer and pass on a right one\n")
	}
	b.WriteString("# (docs/designs/bench-case-format.md, \"The check types\").\n")
	b.WriteString("verification_spec: []\n")
	return b.String()
}

// feedbackTranscriptMD is the draft's transcript.md.
func feedbackTranscriptMD(rec FeedbackRecord) string {
	note := rec.Note
	if note == "" {
		note = feedbackNoNote
	}
	var b strings.Builder
	b.WriteString("# The rated exchange\n\n")
	if rec.Rating == FeedbackUp {
		fmt.Fprintf(&b, "Rated 👍 on %s (%s), a good answer.\n\n", rec.RatedAt.UTC().Format(feedbackRatedDateForm), rec.Platform)
	} else {
		fmt.Fprintf(&b, "Rated 👎 on %s (%s). What the rater said was wrong: %s\n\n",
			rec.RatedAt.UTC().Format(feedbackRatedDateForm), rec.Platform, note)
	}
	fmt.Fprintf(&b, "- Task: `%s`\n", rec.TaskID)
	if rec.ContextID != "" {
		fmt.Fprintf(&b, "- Context: `%s`\n", rec.ContextID)
	}
	fmt.Fprintf(&b, "- Conversation: `%s`, message `%s`\n\n", rec.Conversation, rec.MessageID)
	b.WriteString("## The ask\n\n" + feedbackAskOrTODO(rec) + "\n\n## The rated answer\n\n" + rec.Answer + "\n")
	return b.String()
}

// yamlString is s as a YAML double-quoted scalar. JSON's escaping is YAML's
// but for DEL and the C1 controls, which JSON writes raw and YAML refuses
// anywhere in a stream; those are escaped too.
func yamlString(s string) string {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	_ = enc.Encode(s)
	return yamlUnprintable.Replace(strings.TrimSuffix(buf.String(), "\n"))
}

// yamlComment is s fit for a YAML comment line: the characters YAML refuses
// in a stream, and the ones it reads as a line break (which would end the
// comment and make the rest of the note YAML), dropped; tabs kept, and
// line feeds left to the caller.
func yamlComment(s string) string {
	return strings.Map(func(r rune) rune {
		switch {
		case r == '\t':
			return r
		case r < ' ', r == yamlDEL, r >= yamlC1First && r <= yamlC1Last,
			r == yamlLineSep, r == yamlParaSep, r == yamlNonchar1, r == yamlNonchar2:
			return -1
		}
		return r
	}, s)
}

// feedbackAskOrTODO is the record's ask, or a TODO saying it was not on
// record, so a draft never ships an empty prompt as if it were the ask.
func feedbackAskOrTODO(rec FeedbackRecord) string {
	if strings.TrimSpace(rec.Ask) == "" {
		return feedbackNoAsk
	}
	return rec.Ask
}

// clipWords is s on one line, clipped to n code points with an ellipsis.
func clipWords(s string, n int) string {
	s = strings.Join(strings.Fields(s), " ")
	r := []rune(s)
	if len(r) <= n {
		return s
	}
	return strings.TrimRight(string(r[:n]), " ") + slackClipEllipsis
}
